"""SQLite connection management, migrations and backup.

Connection ownership: every thread gets its own ``sqlite3.Connection`` (kept
in thread-local storage).  The database runs in WAL mode so the UI can read
while the monitoring thread writes; writes are additionally serialised with
``write_lock`` so multi-statement transactions never interleave.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from netwatch.storage.migrations import LATEST_VERSION, MIGRATIONS, POST_MIGRATION

log = logging.getLogger(__name__)


class DatabaseError(Exception):
    pass


class Database:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self._local = threading.local()
        self._all: list[sqlite3.Connection] = []
        self._all_lock = threading.Lock()
        self.write_lock = threading.RLock()
        self._closed = False
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)

    # -- connections -------------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=15, isolation_level=None, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 15000")
        try:
            conn.execute("PRAGMA journal_mode = WAL")
        except sqlite3.OperationalError:  # e.g. network filesystems
            log.warning("WAL mode unavailable for %s", self.path)
        conn.execute("PRAGMA synchronous = NORMAL")
        return conn

    @property
    def conn(self) -> sqlite3.Connection:
        if self._closed:
            raise DatabaseError("database is closed")
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._connect()
            self._local.conn = conn
            with self._all_lock:
                self._all.append(conn)
        return conn

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Serialised write transaction (BEGIN IMMEDIATE ... COMMIT)."""
        with self.write_lock:
            conn = self.conn
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            else:
                conn.execute("COMMIT")

    def query(self, sql: str, params: tuple | dict = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, params).fetchall()

    def query_one(self, sql: str, params: tuple | dict = ()):
        return self.conn.execute(sql, params).fetchone()

    def close(self) -> None:
        with self._all_lock:
            for conn in self._all:
                try:
                    conn.close()
                except sqlite3.Error:
                    pass
            self._all.clear()
        self._local = threading.local()
        self._closed = True

    def reopen(self) -> None:
        self.close()
        self._closed = False

    # -- schema --------------------------------------------------------------
    def schema_version(self) -> int:
        try:
            row = self.query_one("SELECT MAX(version) AS v FROM schema_migrations")
        except sqlite3.OperationalError:
            return 0
        return int(row["v"] or 0)

    def migrate(self) -> list[int]:
        """Apply pending migrations; return the versions applied."""
        applied: list[int] = []
        with self.write_lock:
            conn = self.conn
            conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                " version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at REAL NOT NULL)"
            )
            current = self.schema_version()
            if current > LATEST_VERSION:
                raise DatabaseError(
                    f"Database schema version {current} is newer than this NetWatch "
                    f"supports ({LATEST_VERSION}). Please upgrade NetWatch."
                )
            for version, name, script in MIGRATIONS:
                if version <= current:
                    continue
                log.info("Applying migration %s: %s", version, name)
                conn.execute("BEGIN IMMEDIATE")
                try:
                    for statement in _split_sql(script):
                        conn.execute(statement)
                    if version in POST_MIGRATION:
                        POST_MIGRATION[version](conn)
                    conn.execute(
                        "INSERT INTO schema_migrations(version, name, applied_at) VALUES (?, ?, ?)",
                        (version, name, time.time()),
                    )
                except BaseException:
                    conn.execute("ROLLBACK")
                    raise
                conn.execute("COMMIT")
                applied.append(version)
        return applied

    # -- backup ---------------------------------------------------------------
    def backup_to(self, destination: Path | str) -> None:
        """Consistent online backup using SQLite's backup API."""
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        tmp = destination.with_name(destination.name + ".partial")
        if tmp.exists():
            tmp.unlink()
        dst = sqlite3.connect(str(tmp))
        try:
            with self.write_lock:
                self.conn.backup(dst)
            dst.execute("PRAGMA journal_mode = DELETE")
        finally:
            dst.close()
        os.replace(tmp, destination)

    @staticmethod
    def validate_backup(source: Path | str) -> int:
        """Return the schema version of a backup file or raise DatabaseError."""
        source = Path(source)
        if not source.is_file():
            raise DatabaseError(f"{source} does not exist")
        try:
            conn = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
            try:
                if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise DatabaseError("The backup failed SQLite's integrity check.")
                row = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
                for table in ("monitoring_sessions", "connection_states", "outages"):
                    conn.execute(f"SELECT 1 FROM {table} LIMIT 1")
            finally:
                conn.close()
        except sqlite3.DatabaseError as exc:
            raise DatabaseError(f"Not a NetWatch database: {exc}") from exc
        version = int(row[0] or 0)
        if version > LATEST_VERSION:
            raise DatabaseError("The backup was made by a newer version of NetWatch.")
        return version

    def restore_from(self, source: Path | str) -> None:
        """Replace the live database content with a validated backup."""
        self.validate_backup(source)
        src = sqlite3.connect(f"file:{Path(source)}?mode=ro", uri=True)
        try:
            with self.write_lock:
                src.backup(self.conn)
        finally:
            src.close()
        self.migrate()


def _split_sql(script: str) -> list[str]:
    statements, buf = [], []
    for line in script.splitlines():
        stripped = line.strip()
        if stripped.startswith("--") or not stripped:
            continue
        buf.append(line)
        if stripped.endswith(";") and sqlite3.complete_statement("\n".join(buf)):
            statements.append("\n".join(buf))
            buf = []
    if buf:
        statements.append("\n".join(buf))
    return statements
