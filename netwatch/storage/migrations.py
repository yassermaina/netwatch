"""Versioned schema migrations.

Each migration is applied in its own transaction and recorded in
``schema_migrations``.  Never edit a released migration; append a new one.
"""

from __future__ import annotations

MIGRATIONS: list[tuple[int, str, str]] = [
    (
        1,
        "initial schema",
        """
        CREATE TABLE settings (
            key        TEXT PRIMARY KEY,
            value      TEXT NOT NULL,
            updated_at REAL NOT NULL
        );

        CREATE TABLE targets (
            id         INTEGER PRIMARY KEY,
            name       TEXT NOT NULL,
            address    TEXT NOT NULL,
            check_type TEXT NOT NULL DEFAULT 'icmp',
            enabled    INTEGER NOT NULL DEFAULT 1,
            position   INTEGER NOT NULL DEFAULT 0,
            created_at REAL NOT NULL,
            deleted_at REAL
        );
        CREATE UNIQUE INDEX idx_targets_active
            ON targets(address, check_type) WHERE deleted_at IS NULL;

        CREATE TABLE monitoring_sessions (
            id              INTEGER PRIMARY KEY,
            started_at      REAL NOT NULL,
            ended_at        REAL,
            last_heartbeat  REAL,
            status          TEXT NOT NULL
                            CHECK (status IN ('running', 'stopped', 'interrupted')),
            end_reason      TEXT,
            config_snapshot TEXT NOT NULL,
            app_version     TEXT
        );
        CREATE INDEX idx_sessions_started ON monitoring_sessions(started_at);

        CREATE TABLE measurements (
            id               INTEGER PRIMARY KEY,
            ts               REAL NOT NULL,
            session_id       INTEGER NOT NULL
                             REFERENCES monitoring_sessions(id) ON DELETE CASCADE,
            target_id        INTEGER NOT NULL REFERENCES targets(id),
            check_type       TEXT NOT NULL,
            success          INTEGER NOT NULL,
            latency_ms       REAL,
            packets_sent     INTEGER,
            packets_received INTEGER,
            error_category   TEXT,
            detail           TEXT
        );
        CREATE INDEX idx_measurements_ts ON measurements(ts);
        CREATE INDEX idx_measurements_target_ts ON measurements(target_id, ts);
        CREATE INDEX idx_measurements_session ON measurements(session_id);

        -- Contiguous periods of one connection/coverage state.  ended_at is
        -- NULL only for the live state of the running session.
        CREATE TABLE connection_states (
            id          INTEGER PRIMARY KEY,
            session_id  INTEGER NOT NULL
                        REFERENCES monitoring_sessions(id) ON DELETE CASCADE,
            state       TEXT NOT NULL,
            started_at  REAL NOT NULL,
            ended_at    REAL,
            detected_at REAL,
            targets     TEXT,
            interface   TEXT,
            reason      TEXT
        );
        CREATE INDEX idx_states_started ON connection_states(started_at);
        CREATE INDEX idx_states_ended ON connection_states(ended_at);
        CREATE INDEX idx_states_session ON connection_states(session_id);

        CREATE TABLE outages (
            id                   INTEGER PRIMARY KEY,
            session_id           INTEGER
                                 REFERENCES monitoring_sessions(id) ON DELETE SET NULL,
            classification       TEXT NOT NULL,
            started_at           REAL NOT NULL,
            detected_at          REAL NOT NULL,
            ended_at             REAL,
            recovery_detected_at REAL,
            duration             REAL,
            status               TEXT NOT NULL CHECK (status IN (
                                     'ongoing', 'recovered', 'monitoring_stopped',
                                     'monitoring_paused', 'monitoring_interrupted',
                                     'evidence_lost')),
            start_observed       INTEGER NOT NULL,
            end_observed         INTEGER NOT NULL DEFAULT 0,
            evidence             TEXT
        );
        CREATE INDEX idx_outages_started ON outages(started_at);
        CREATE INDEX idx_outages_ended ON outages(ended_at);

        CREATE TABLE outage_targets (
            outage_id     INTEGER NOT NULL REFERENCES outages(id) ON DELETE CASCADE,
            target_id     INTEGER NOT NULL REFERENCES targets(id),
            failure_count INTEGER NOT NULL DEFAULT 0,
            last_error    TEXT,
            PRIMARY KEY (outage_id, target_id)
        );

        CREATE TABLE application_events (
            id         INTEGER PRIMARY KEY,
            ts         REAL NOT NULL,
            kind       TEXT NOT NULL,
            session_id INTEGER REFERENCES monitoring_sessions(id) ON DELETE SET NULL,
            message    TEXT,
            data       TEXT
        );
        CREATE INDEX idx_app_events_ts ON application_events(ts);
        """,
    ),
    (
        2,
        "network name on history entries and outages",
        """
        ALTER TABLE connection_states ADD COLUMN network TEXT;
        ALTER TABLE outages ADD COLUMN network TEXT;
        CREATE INDEX idx_states_network ON connection_states(network);
        """,
    ),
]

LATEST_VERSION = MIGRATIONS[-1][0]


def backfill_network(conn) -> None:
    """Fill the new network column from the interface details recorded by earlier versions."""
    from netwatch.models import LocalNetworkStatus  # noqa: PLC0415

    rows = conn.execute("SELECT id, interface FROM connection_states WHERE interface IS NOT NULL").fetchall()
    for state_id, raw in rows:
        status = LocalNetworkStatus.from_json(raw)
        name = status.network_name if status else None
        if name:
            conn.execute("UPDATE connection_states SET network = ? WHERE id = ?", (name, state_id))
    # an outage belongs to the network of the entry that was active when it was detected
    conn.execute(
        "UPDATE outages SET network = (SELECT cs.network FROM connection_states cs "
        "WHERE cs.started_at <= outages.started_at AND cs.network IS NOT NULL "
        "ORDER BY cs.started_at DESC LIMIT 1) WHERE network IS NULL"
    )


# Python steps run in the same transaction, right after a migration's SQL.
POST_MIGRATION = {2: backfill_network}
