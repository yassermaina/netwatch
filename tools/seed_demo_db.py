#!/usr/bin/env python3
"""Developer tool: create a SYNTHETIC demo database for screenshots and load testing.

This never touches the real NetWatch database: the output path is required.
    python3 tools/seed_demo_db.py /tmp/demo.db --days 200
    netwatch --db /tmp/demo.db
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from netwatch.models import ConnState, LocalNetworkStatus, WifiInfo  # noqa: E402
from netwatch.settings import DEFAULT_TARGETS  # noqa: E402
from netwatch.storage.db import Database  # noqa: E402
from netwatch.storage.repository import Repository  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("path")
    ap.add_argument("--days", type=int, default=200)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--measure-days", type=int, default=7, help="days of raw measurements to synthesise")
    ap.add_argument("--leave-running", action="store_true", help="leave the last session open (simulates a crash)")
    args = ap.parse_args()
    out = Path(args.path)
    if out.exists():
        sys.exit(f"{out} exists; refusing to overwrite")
    rnd = random.Random(args.seed)
    db = Database(out)
    db.migrate()
    repo = Repository(db)
    repo.ensure_default_targets(DEFAULT_TARGETS)
    targets = repo.list_targets()
    now = time.time()
    t = now - args.days * 86400
    wifi = LocalNetworkStatus(True, "networkmanager", "wlp2s0", "wifi", "Home WiFi", "full",
                              WifiInfo("Home WiFi", 5180, 36, 82, -45, "802.11ax", "WPA2")).to_json()
    states, outages = [], []
    session_rows = []
    while t < now - 600:
        # a session lasting 1-20 days, followed by a gap of 0.5h-10 days (app not running)
        length = rnd.uniform(1, 20) * 86400
        s_end = min(now - 300, t + length)
        session_rows.append((t, s_end))
        cur = t
        state = ConnState.ONLINE
        while cur < s_end:
            if state == ConnState.ONLINE:
                dur = rnd.expovariate(1 / (3 * 86400 / 4))
                nxt = ConnState.NO_INTERNET if rnd.random() < 0.85 else ConnState.NOT_CONNECTED
            else:
                dur = rnd.choice([rnd.uniform(15, 120), rnd.uniform(60, 300), rnd.uniform(5, 40)])
                nxt = ConnState.ONLINE
            end = min(s_end, cur + max(10.0, dur))
            states.append((len(session_rows), state, cur, end))
            if state != ConnState.ONLINE:
                outages.append((len(session_rows), state, cur, end, end < s_end))
            cur, state = end, nxt
        t = s_end + rnd.choice([rnd.uniform(1800, 4 * 3600), rnd.uniform(86400, 10 * 86400), rnd.uniform(600, 7200)])
    with repo.tx() as conn:
        ids = []
        for i, (s, e) in enumerate(session_rows):
            last = i == len(session_rows) - 1
            running = last and args.leave_running
            cur = conn.execute(
                "INSERT INTO monitoring_sessions(started_at, ended_at, last_heartbeat, status, end_reason, "
                "config_snapshot, app_version) VALUES (?,?,?,?,?,?,?)",
                (s, None if running else e, e, "running" if running else "stopped", None if running else "normal",
                 json.dumps({"demo": True}), "demo"))
            ids.append(cur.lastrowid)
        conn.executemany(
            "INSERT INTO connection_states(session_id, state, started_at, ended_at, detected_at, targets, interface, reason) "
            "VALUES (?,?,?,?,?,?,?,?)",
            [(ids[sidx - 1], st.value, s, e, s, "1.1.1.1" if st == ConnState.ONLINE else "1.1.1.1, 8.8.8.8, 9.9.9.9",
              wifi if st != ConnState.NOT_CONNECTED else None,
              None if st == ConnState.ONLINE else "all targets failed (Timed out)") for sidx, st, s, e in states])
        for sidx, st, s, e, recovered in outages:
            cur = conn.execute(
                "INSERT INTO outages(session_id, classification, started_at, detected_at, ended_at, recovery_detected_at, "
                "duration, status, start_observed, end_observed, evidence) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (ids[sidx - 1], st.value, s, s + 10, e, e + 10 if recovered else None, e - s,
                 "recovered" if recovered else "monitoring_stopped", 1, int(recovered), "[]"))
            for tg in targets:
                conn.execute("INSERT INTO outage_targets VALUES (?,?,?,?)",
                             (cur.lastrowid, tg.id, max(1, int((e - s) / 5)), "timeout"))
        # raw measurements for the most recent days, every 30 s
        rows = []
        m_start = now - args.measure_days * 86400
        for sidx, st, s, e in states:
            if e < m_start:
                continue
            ts = max(s, m_start)
            while ts < e:
                for i, tg in enumerate(targets):
                    ok = st == ConnState.ONLINE and rnd.random() > 0.004
                    lat = (9 + i * 6) + rnd.gammavariate(2, 2.5) + (40 if rnd.random() < 0.01 else 0)
                    rows.append((ts, ids[sidx - 1], tg.id, "icmp", int(ok), lat if ok else None, 1, int(ok),
                                 "ok" if ok else "timeout", None))
                ts += 30
        conn.executemany(
            "INSERT INTO measurements(ts, session_id, target_id, check_type, success, latency_ms, packets_sent, "
            "packets_received, error_category, detail) VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
    print(f"wrote {out}: {len(session_rows)} sessions, {len(states)} states, {len(outages)} outages, "
          f"{len(rows)} measurements (synthetic demo data)")
    db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
