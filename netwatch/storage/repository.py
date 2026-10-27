"""Data access layer.  All SQL lives here and is parameterised.

Write helpers execute on the calling thread's connection; wrap related writes
in ``repo.tx()`` to make them atomic.
"""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from typing import Iterable, Iterator, Optional

from netwatch.models import CheckResult, ConnState, Target
from netwatch.storage.db import Database


class DuplicateTargetError(Exception):
    pass


class Repository:
    def __init__(self, db: Database):
        self.db = db

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        with self.db.transaction() as conn:
            yield conn

    def _w(self, sql: str, params: tuple | dict = ()) -> sqlite3.Cursor:
        with self.db.write_lock:
            return self.db.conn.execute(sql, params)

    # ------------------------------------------------------------------ targets
    def ensure_default_targets(self, defaults: Iterable[tuple[str, str, str]]) -> None:
        row = self.db.query_one("SELECT COUNT(*) AS n FROM targets")
        if row["n"]:
            return
        with self.tx():
            for pos, (name, address, kind) in enumerate(defaults):
                self._w(
                    "INSERT INTO targets(name, address, check_type, enabled, position, created_at) "
                    "VALUES (?, ?, ?, 1, ?, ?)",
                    (name, address, kind, pos, time.time()),
                )

    @staticmethod
    def _target(row) -> Target:
        return Target(
            id=row["id"],
            name=row["name"],
            address=row["address"],
            check_type=row["check_type"],
            enabled=bool(row["enabled"]),
            position=row["position"],
        )

    def list_targets(self, enabled_only: bool = False) -> list[Target]:
        sql = "SELECT * FROM targets WHERE deleted_at IS NULL"
        if enabled_only:
            sql += " AND enabled = 1"
        sql += " ORDER BY position, id"
        return [self._target(r) for r in self.db.query(sql)]

    def targets_by_id(self) -> dict[int, Target]:
        """All targets including deleted ones (history still references them)."""
        return {r["id"]: self._target(r) for r in self.db.query("SELECT * FROM targets")}

    def add_target(self, name: str, address: str, check_type: str = "icmp", enabled: bool = True) -> int:
        row = self.db.query_one("SELECT COALESCE(MAX(position), -1) + 1 AS p FROM targets WHERE deleted_at IS NULL")
        try:
            cur = self._w(
                "INSERT INTO targets(name, address, check_type, enabled, position, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (name.strip(), address.strip(), check_type, int(enabled), row["p"], time.time()),
            )
        except sqlite3.IntegrityError as exc:
            raise DuplicateTargetError(f"{address} is already monitored with this check type.") from exc
        return int(cur.lastrowid)

    def update_target(self, target: Target) -> None:
        try:
            self._w(
                "UPDATE targets SET name = ?, address = ?, check_type = ?, enabled = ? WHERE id = ?",
                (target.name.strip(), target.address.strip(), target.check_type, int(target.enabled), target.id),
            )
        except sqlite3.IntegrityError as exc:
            raise DuplicateTargetError(f"{target.address} is already monitored with this check type.") from exc

    def delete_target(self, target_id: int) -> None:
        """Soft delete so historical measurements keep their reference."""
        self._w("UPDATE targets SET deleted_at = ?, enabled = 0 WHERE id = ?", (time.time(), target_id))

    # ----------------------------------------------------------------- sessions
    def create_session(self, started_at: float, config: dict, version: str) -> int:
        cur = self._w(
            "INSERT INTO monitoring_sessions(started_at, last_heartbeat, status, config_snapshot, app_version) "
            "VALUES (?, ?, 'running', ?, ?)",
            (started_at, started_at, json.dumps(config), version),
        )
        return int(cur.lastrowid)

    def heartbeat(self, session_id: int, ts: float) -> None:
        self._w("UPDATE monitoring_sessions SET last_heartbeat = ? WHERE id = ?", (ts, session_id))

    def end_session(self, session_id: int, ended_at: float, status: str = "stopped", reason: str = "normal") -> None:
        self._w(
            "UPDATE monitoring_sessions SET ended_at = ?, last_heartbeat = ?, status = ?, end_reason = ? WHERE id = ?",
            (ended_at, ended_at, status, reason, session_id),
        )

    def get_session(self, session_id: int):
        return self.db.query_one("SELECT * FROM monitoring_sessions WHERE id = ?", (session_id,))

    def sessions_between(self, start: float, end: float) -> list[sqlite3.Row]:
        return self.db.query(
            "SELECT * FROM monitoring_sessions WHERE started_at < ? AND COALESCE(ended_at, last_heartbeat, started_at) > ? "
            "ORDER BY started_at",
            (end, start),
        )

    def recover_interrupted_sessions(self) -> list[dict]:
        """Close sessions left 'running' by a crash or kill.

        The session is considered to have ended at its last heartbeat (the last
        committed tick); nothing after that is claimed as monitored.
        """
        recovered = []
        rows = self.db.query("SELECT * FROM monitoring_sessions WHERE status = 'running'")
        for row in rows:
            end = row["last_heartbeat"] or row["started_at"]
            with self.tx():
                self._w(
                    "UPDATE monitoring_sessions SET ended_at = ?, status = 'interrupted', end_reason = 'interrupted' "
                    "WHERE id = ?",
                    (end, row["id"]),
                )
                self._w(
                    "UPDATE connection_states SET ended_at = MAX(started_at, ?) WHERE session_id = ? AND ended_at IS NULL",
                    (end, row["id"]),
                )
                self._w(
                    "UPDATE connection_states SET ended_at = ? WHERE session_id = ? AND ended_at > ?",
                    (end, row["id"], end),
                )
                self._w(
                    "DELETE FROM connection_states WHERE session_id = ? AND ended_at <= started_at AND ended_at IS NOT NULL "
                    "AND started_at > ?",
                    (row["id"], end),
                )
                for outage in self.db.query(
                    "SELECT * FROM outages WHERE session_id = ? AND status = 'ongoing'", (row["id"],)
                ):
                    stop = max(outage["started_at"], end)
                    self._w(
                        "UPDATE outages SET ended_at = ?, duration = ?, status = 'monitoring_interrupted', "
                        "end_observed = 0 WHERE id = ?",
                        (stop, stop - outage["started_at"], outage["id"]),
                    )
                self.log_event(
                    "session_interrupted",
                    "Monitoring session ended unexpectedly; history after the last check is treated as unmonitored.",
                    session_id=row["id"],
                    data={"last_heartbeat": end},
                    ts=time.time(),
                )
            recovered.append({"session_id": row["id"], "ended_at": end, "started_at": row["started_at"]})
        return recovered

    # ------------------------------------------------------------------- states
    def open_state(
        self,
        session_id: int,
        state: ConnState,
        started_at: float,
        detected_at: Optional[float] = None,
        targets: Optional[str] = None,
        interface: Optional[str] = None,
        reason: Optional[str] = None,
        network: Optional[str] = None,
    ) -> int:
        cur = self._w(
            "INSERT INTO connection_states(session_id, state, started_at, detected_at, targets, interface, reason, network) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (session_id, state.value, started_at, detected_at, targets, interface, reason, network),
        )
        return int(cur.lastrowid)

    def close_state(self, state_id: int, ended_at: float) -> None:
        self._w(
            "UPDATE connection_states SET ended_at = MAX(started_at, ?) WHERE id = ?",
            (ended_at, state_id),
        )

    def set_state_network(self, state_id: int, network: str) -> None:
        self._w("UPDATE connection_states SET network = ? WHERE id = ? AND network IS NULL", (network, state_id))

    def networks(self) -> list[str]:
        """All network names that appear in the history, most recently used first."""
        rows = self.db.query(
            "SELECT network, MAX(started_at) AS last FROM connection_states WHERE network IS NOT NULL "
            "GROUP BY network ORDER BY last DESC"
        )
        return [r["network"] for r in rows]

    def update_state_interface(self, state_id: int, interface: str) -> None:
        self._w("UPDATE connection_states SET interface = ? WHERE id = ?", (interface, state_id))

    def states_between(self, start: float, end: float) -> list[sqlite3.Row]:
        return self.db.query(
            "SELECT * FROM connection_states WHERE started_at < ? AND (ended_at IS NULL OR ended_at > ?) "
            "ORDER BY started_at, id",
            (end, start),
        )

    def get_state(self, state_id: int):
        return self.db.query_one("SELECT * FROM connection_states WHERE id = ?", (state_id,))

    def earliest_record(self) -> Optional[float]:
        row = self.db.query_one(
            "SELECT MIN(t) AS t FROM (SELECT MIN(started_at) AS t FROM connection_states "
            "UNION ALL SELECT MIN(started_at) FROM monitoring_sessions)"
        )
        return row["t"] if row and row["t"] is not None else None

    # ------------------------------------------------------------------ outages
    def create_outage(
        self,
        session_id: int,
        classification: ConnState,
        started_at: float,
        detected_at: float,
        start_observed: bool,
        evidence: list[dict],
        network: Optional[str] = None,
    ) -> int:
        cur = self._w(
            "INSERT INTO outages(session_id, classification, started_at, detected_at, status, start_observed, evidence, "
            "network) VALUES (?, ?, ?, ?, 'ongoing', ?, ?, ?)",
            (session_id, classification.value, started_at, detected_at, int(start_observed), json.dumps(evidence),
             network),
        )
        return int(cur.lastrowid)

    def _append_evidence(self, outage_id: int, items: list[dict]) -> None:
        row = self.db.query_one("SELECT evidence FROM outages WHERE id = ?", (outage_id,))
        if row is None:
            return
        try:
            evidence = json.loads(row["evidence"] or "[]")
        except ValueError:
            evidence = []
        evidence.extend(items)
        self._w("UPDATE outages SET evidence = ? WHERE id = ?", (json.dumps(evidence[-200:]), outage_id))

    def reclassify_outage(self, outage_id: int, classification: ConnState, evidence: list[dict]) -> None:
        self._w("UPDATE outages SET classification = ? WHERE id = ?", (classification.value, outage_id))
        self._append_evidence(outage_id, evidence)

    def record_outage_failures(self, outage_id: int, results: list[CheckResult]) -> None:
        for r in results:
            if r.success or r.target_id is None:
                continue
            self._w(
                "INSERT INTO outage_targets(outage_id, target_id, failure_count, last_error) VALUES (?, ?, 1, ?) "
                "ON CONFLICT(outage_id, target_id) DO UPDATE SET failure_count = failure_count + 1, "
                "last_error = excluded.last_error",
                (outage_id, r.target_id, r.error.value),
            )

    def close_outage(
        self,
        outage_id: int,
        ended_at: float,
        status: str,
        end_observed: bool,
        recovery_detected_at: Optional[float] = None,
        evidence: Optional[list[dict]] = None,
    ) -> None:
        self._w(
            "UPDATE outages SET ended_at = MAX(started_at, ?), duration = MAX(0, ? - started_at), status = ?, "
            "end_observed = ?, recovery_detected_at = ? WHERE id = ?",
            (ended_at, ended_at, status, int(end_observed), recovery_detected_at, outage_id),
        )
        if evidence:
            self._append_evidence(outage_id, evidence)

    def outages_between(self, start: float, end: float) -> list[sqlite3.Row]:
        return self.db.query(
            "SELECT * FROM outages WHERE started_at < ? AND (ended_at IS NULL OR ended_at > ?) ORDER BY started_at",
            (end, start),
        )

    def get_outage(self, outage_id: int):
        return self.db.query_one("SELECT * FROM outages WHERE id = ?", (outage_id,))

    def outage_targets(self, outage_id: int) -> list[sqlite3.Row]:
        return self.db.query(
            "SELECT ot.*, t.name, t.address FROM outage_targets ot JOIN targets t ON t.id = ot.target_id "
            "WHERE outage_id = ? ORDER BY t.position",
            (outage_id,),
        )

    def all_outage_targets(self, outage_ids: list[int]) -> dict[int, list[sqlite3.Row]]:
        result: dict[int, list] = {}
        for chunk_start in range(0, len(outage_ids), 500):
            chunk = outage_ids[chunk_start : chunk_start + 500]
            marks = ",".join("?" * len(chunk))
            for row in self.db.query(
                f"SELECT ot.*, t.name, t.address FROM outage_targets ot JOIN targets t ON t.id = ot.target_id "
                f"WHERE outage_id IN ({marks})",
                tuple(chunk),
            ):
                result.setdefault(row["outage_id"], []).append(row)
        return result

    # ------------------------------------------------------------- measurements
    def insert_measurements(self, session_id: int, results: list[CheckResult]) -> None:
        with self.db.write_lock:
            self.db.conn.executemany(
                "INSERT INTO measurements(ts, session_id, target_id, check_type, success, latency_ms, "
                "packets_sent, packets_received, error_category, detail) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        r.timestamp,
                        session_id,
                        r.target_id,
                        r.check_type,
                        int(r.success),
                        r.latency_ms,
                        r.packets_sent,
                        r.packets_received,
                        r.error.value,
                        r.detail[:500] if r.detail else None,
                    )
                    for r in results
                    if r.target_id is not None
                ],
            )

    def measurements_between(self, start: float, end: float, target_id: Optional[int] = None, limit: int = 20000):
        sql = "SELECT * FROM measurements WHERE ts >= ? AND ts < ?"
        params: list = [start, end]
        if target_id is not None:
            sql += " AND target_id = ?"
            params.append(target_id)
        sql += " ORDER BY ts LIMIT ?"
        params.append(limit)
        return self.db.query(sql, tuple(params))

    def measurement_buckets(self, start: float, end: float, bucket: float) -> list[sqlite3.Row]:
        """Downsampled latency/loss per target and time bucket."""
        return self.db.query(
            "SELECT target_id, CAST((ts - :start) / :bucket AS INTEGER) AS b, COUNT(*) AS n, "
            "SUM(success) AS ok, AVG(CASE WHEN success THEN latency_ms END) AS avg_ms, "
            "MIN(CASE WHEN success THEN latency_ms END) AS min_ms, MAX(CASE WHEN success THEN latency_ms END) AS max_ms "
            "FROM measurements WHERE ts >= :start AND ts < :end GROUP BY target_id, b ORDER BY b",
            {"start": start, "end": end, "bucket": bucket},
        )

    def latency_values(self, start: float, end: float) -> list[sqlite3.Row]:
        return self.db.query(
            "SELECT target_id, latency_ms FROM measurements WHERE ts >= ? AND ts < ? AND success = 1 "
            "AND latency_ms IS NOT NULL",
            (start, end),
        )

    def check_counts(self, start: float, end: float) -> list[sqlite3.Row]:
        return self.db.query(
            "SELECT target_id, COUNT(*) AS n, SUM(success) AS ok, MAX(CASE WHEN success THEN ts END) AS last_ok "
            "FROM measurements WHERE ts >= ? AND ts < ? GROUP BY target_id",
            (start, end),
        )

    def error_breakdown(self, start: float, end: float) -> list[sqlite3.Row]:
        return self.db.query(
            "SELECT target_id, error_category, COUNT(*) AS n FROM measurements WHERE ts >= ? AND ts < ? "
            "AND success = 0 GROUP BY target_id, error_category ORDER BY n DESC",
            (start, end),
        )

    def last_success(self) -> Optional[float]:
        row = self.db.query_one("SELECT MAX(ts) AS t FROM measurements WHERE success = 1")
        return row["t"] if row else None

    def prune_measurements(self, cutoff: float, batch: int = 5000) -> int:
        """Delete raw measurements older than ``cutoff``.  Outages/states are kept."""
        total = 0
        while True:
            with self.tx():
                cur = self._w(
                    "DELETE FROM measurements WHERE id IN (SELECT id FROM measurements WHERE ts < ? LIMIT ?)",
                    (cutoff, batch),
                )
            total += cur.rowcount
            if cur.rowcount < batch:
                return total

    # ------------------------------------------------------------------- events
    def log_event(
        self,
        kind: str,
        message: str = "",
        session_id: Optional[int] = None,
        data: Optional[dict] = None,
        ts: Optional[float] = None,
    ) -> None:
        self._w(
            "INSERT INTO application_events(ts, kind, session_id, message, data) VALUES (?, ?, ?, ?, ?)",
            (ts if ts is not None else time.time(), kind, session_id, message, json.dumps(data) if data else None),
        )

    def events_between(self, start: float, end: float, kinds: Optional[list[str]] = None) -> list[sqlite3.Row]:
        sql = "SELECT * FROM application_events WHERE ts >= ? AND ts < ?"
        params: list = [start, end]
        if kinds:
            sql += f" AND kind IN ({','.join('?' * len(kinds))})"
            params.extend(kinds)
        return self.db.query(sql + " ORDER BY ts", tuple(params))

    # ------------------------------------------------------------------ deletes
    def delete_range(self, start: float, end: float) -> dict:
        """Delete closed records lying entirely within [start, end]."""
        with self.tx():
            s = self._w(
                "DELETE FROM connection_states WHERE ended_at IS NOT NULL AND started_at >= ? AND ended_at <= ?",
                (start, end),
            ).rowcount
            o = self._w(
                "DELETE FROM outages WHERE ended_at IS NOT NULL AND started_at >= ? AND ended_at <= ?", (start, end)
            ).rowcount
            m = self._w("DELETE FROM measurements WHERE ts >= ? AND ts <= ?", (start, end)).rowcount
            self._w(
                "DELETE FROM monitoring_sessions WHERE status != 'running' AND started_at >= ? AND ended_at <= ?",
                (start, end),
            )
            self.log_event("history_deleted", "Deleted history in a time range", data={"start": start, "end": end})
        return {"states": s, "outages": o, "measurements": m}

    def delete_all(self, keep_session_id: Optional[int], now: float) -> None:
        """Clear all history.  The live session (if any) restarts its record at ``now``."""
        with self.tx():
            if keep_session_id is not None:
                self._w("DELETE FROM measurements WHERE session_id = ?", (keep_session_id,))
                self._w(
                    "DELETE FROM connection_states WHERE session_id = ? AND ended_at IS NOT NULL", (keep_session_id,)
                )
                self._w(
                    "UPDATE connection_states SET started_at = ? WHERE session_id = ? AND ended_at IS NULL",
                    (now, keep_session_id),
                )
                self._w("DELETE FROM outages WHERE status != 'ongoing'")
                self._w("UPDATE outages SET started_at = ?, start_observed = 0 WHERE status = 'ongoing'", (now,))
                self._w("UPDATE monitoring_sessions SET started_at = ? WHERE id = ?", (now, keep_session_id))
                self._w("DELETE FROM monitoring_sessions WHERE id != ?", (keep_session_id,))
            else:
                self._w("DELETE FROM outages")
                self._w("DELETE FROM monitoring_sessions")
            self._w("DELETE FROM application_events")
            self.log_event("history_cleared", "All history was cleared by the user", ts=now)

    def delete_state(self, state_id: int) -> None:
        with self.tx():
            row = self.get_state(state_id)
            if row is None or row["ended_at"] is None:
                return
            self._w(
                "DELETE FROM measurements WHERE session_id = ? AND ts >= ? AND ts < ?",
                (row["session_id"], row["started_at"], row["ended_at"]),
            )
            self._w(
                "DELETE FROM outages WHERE ended_at IS NOT NULL AND started_at >= ? AND ended_at <= ?",
                (row["started_at"], row["ended_at"]),
            )
            self._w("DELETE FROM connection_states WHERE id = ?", (state_id,))
            self.log_event("history_deleted", "Deleted one event", data={"state_id": state_id})

    # -------------------------------------------------------------- diagnostics
    def counts(self) -> dict[str, int]:
        result = {}
        for table in (
            "monitoring_sessions",
            "measurements",
            "connection_states",
            "outages",
            "application_events",
            "targets",
        ):
            result[table] = self.db.query_one(f"SELECT COUNT(*) AS n FROM {table}")["n"]
        return result
