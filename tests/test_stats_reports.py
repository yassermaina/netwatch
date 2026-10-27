"""Statistics, history views, reports, persistence and settings validation."""

import csv
import io
import time

import pytest

from netwatch.models import ConnState
from netwatch.reports import build_report, render, write_report
from netwatch.settings import validate, validate_target
from netwatch.storage.db import Database, DatabaseError
from netwatch.storage.repository import DuplicateTargetError, Repository

DAY = 86400.0


def seed(repo, t0):
    """Session with: 1h online, 10 min no-internet outage, 50 min online; then 2h gap; 1h online (ongoing)."""
    s1 = repo.create_session(t0, {}, "test")
    a = repo.open_state(s1, ConnState.ONLINE, t0)
    repo.close_state(a, t0 + 3600)
    b = repo.open_state(s1, ConnState.NO_INTERNET, t0 + 3600)
    repo.close_state(b, t0 + 4200)
    o = repo.create_outage(s1, ConnState.NO_INTERNET, t0 + 3600, t0 + 3610, True, [])
    repo.close_outage(o, t0 + 4200, "recovered", True, t0 + 4205)
    c = repo.open_state(s1, ConnState.ONLINE, t0 + 4200)
    repo.close_state(c, t0 + 7200)
    repo.end_session(s1, t0 + 7200)
    s2 = repo.create_session(t0 + 14400, {}, "test")
    repo.open_state(s2, ConnState.ONLINE, t0 + 14400)  # ongoing
    return s1, s2


def test_uptime_uses_monitored_time_denominator(repo, stats):
    t0 = 1_700_000_000.0
    seed(repo, t0)
    now = t0 + 18000
    s = stats.summary(t0, now, now)
    assert s.monitored == pytest.approx(3600 + 600 + 3000 + 3600)
    assert s.downtime == pytest.approx(600)
    assert s.uptime_pct == pytest.approx((3600 + 3000 + 3600) / 10800 * 100)
    assert s.not_running == pytest.approx(7200)
    assert s.coverage_pct == pytest.approx(10800 / 18000 * 100)
    assert s.outage_count == 1
    assert s.longest_outage.duration_in_range == pytest.approx(600)


def test_period_change_updates_all_metrics(repo, stats):
    t0 = time.time() - 40 * DAY
    s1 = repo.create_session(t0, {}, "test")
    st = repo.open_state(s1, ConnState.ONLINE, t0)
    repo.close_state(st, t0 + 20 * DAY)
    o = repo.create_outage(s1, ConnState.NO_INTERNET, t0 + 20 * DAY, t0 + 20 * DAY, True, [])
    st = repo.open_state(s1, ConnState.NO_INTERNET, t0 + 20 * DAY)
    repo.close_state(st, t0 + 20 * DAY + 3600)
    repo.close_outage(o, t0 + 20 * DAY + 3600, "recovered", True)
    repo.open_state(s1, ConnState.ONLINE, t0 + 20 * DAY + 3600)
    now = time.time()
    h24 = stats.history("24h", now)
    hall = stats.history("all", now)
    h30 = stats.history("30d", now)
    assert h24.summary.outage_count == 0 and h24.summary.uptime_pct == 100
    assert hall.summary.outage_count == 1 and h30.summary.outage_count == 1
    assert hall.summary.range_duration > h30.summary.range_duration > h24.summary.range_duration
    assert len(hall.days) > len(h30.days) > len(h24.days)
    assert hall.segments[0].start == pytest.approx(t0)


def test_calendar_uses_database_records(repo, stats):
    now = time.time()
    t0 = now - 3 * DAY
    s1 = repo.create_session(t0, {}, "test")
    st = repo.open_state(s1, ConnState.ONLINE, t0)
    repo.close_state(st, t0 + 3600)
    repo.end_session(s1, t0 + 3600)
    h = stats.history("7d", now)
    cats = {d.day: d.category for d in h.days}
    from datetime import datetime

    first_day = datetime.fromtimestamp(t0).date()
    assert cats[first_day] in ("online",)
    other = [c for d, c in cats.items() if d != first_day and c != "outside"]
    assert other and all(c == "unmonitored" for c in other)  # empty days are NOT shown as online
    assert sum(d.durations[ConnState.ONLINE] for d in h.days) == pytest.approx(3600)


def test_segments_link_to_outages_for_timeline_selection(repo, stats):
    t0 = 1_700_000_000.0
    seed(repo, t0)
    h = stats.history("custom", t0 + 18000, custom=(t0, t0 + 18000))
    down = [s for s in h.segments if s.state == ConnState.NO_INTERNET]
    assert len(down) == 1 and down[0].outage_id in h.outages_by_id
    gap = [s for s in h.segments if s.synthetic]
    assert gap and gap[0].state == ConnState.NOT_RUNNING and gap[0].duration == pytest.approx(7200)
    # chronological, non-overlapping
    for a, b in zip(h.segments, h.segments[1:]):
        assert a.end <= b.start + 1e-9


def test_overlapping_records_not_double_counted(repo, stats):
    t0 = 1_700_000_000.0
    s = repo.create_session(t0, {}, "t")
    a = repo.open_state(s, ConnState.ONLINE, t0)
    repo.close_state(a, t0 + 1000)
    b = repo.open_state(s, ConnState.ONLINE, t0 + 500)  # backwards clock change overlap
    repo.close_state(b, t0 + 1500)
    sm = stats.summary(t0, t0 + 1500, t0 + 1500)
    assert sm.online == pytest.approx(1500)


def test_empty_history(stats):
    now = time.time()
    h = stats.history("all", now)
    assert not h.summary.has_data
    assert h.summary.uptime_pct is None and h.summary.outage_count == 0
    assert all(s.synthetic for s in h.segments)


def test_reports_represent_partial_coverage(repo, stats, settings):
    t0 = 1_700_000_000.0
    seed(repo, t0)
    rep = build_report(repo, stats, settings, custom=(t0, t0 + 18000), now=t0 + 18000)
    txt = render(rep, "txt")
    assert "60.0% coverage" in txt or "60% coverage" in txt
    assert "App Not Running" in txt
    assert any("covered" in item for item in rep.limitations)
    html = render(rep, "html")
    assert "<svg" in html and "Limitations" in html and "<script" not in html
    data = render(rep, "csv")
    rows = list(csv.DictReader(io.StringIO(data)))
    assert {r["record_type"] for r in rows} >= {"summary", "state", "unmonitored", "outage"}
    outage_row = next(r for r in rows if r["record_type"] == "outage")
    assert outage_row["duration_seconds"] == "600" and outage_row["end_observed"] == "yes"


def test_report_flags_unobserved_outage_start(repo, stats, settings):
    t0 = 1_700_000_000.0
    s = repo.create_session(t0, {}, "t")
    repo.open_state(s, ConnState.NO_INTERNET, t0)
    repo.create_outage(s, ConnState.NO_INTERNET, t0, t0, False, [])
    rep = build_report(repo, stats, settings, custom=(t0, t0 + 600), now=t0 + 600)
    txt = render(rep, "txt")
    assert "start not observed" in txt and "ongoing" in txt


def test_write_report_csv_has_bom(tmp_path, repo, stats, settings):
    rep = build_report(repo, stats, settings, period="24h")
    path = tmp_path / "r.csv"
    write_report(str(path), render(rep, "csv"), "csv")
    assert path.read_bytes().startswith(b"\xef\xbb\xbf")


def test_migration_and_backup_restore(tmp_path):
    db = Database(tmp_path / "a.db")
    assert db.migrate() == [1, 2]
    assert db.migrate() == []  # idempotent
    repo = Repository(db)
    sid = repo.create_session(1.0, {}, "t")
    backup = tmp_path / "backup.db"
    db.backup_to(backup)
    assert Database.validate_backup(backup) == 2
    repo.create_session(2.0, {}, "t")
    assert db.query_one("SELECT COUNT(*) n FROM monitoring_sessions")["n"] == 2
    db.restore_from(backup)
    rows = db.query("SELECT id FROM monitoring_sessions")
    assert [r["id"] for r in rows] == [sid]
    bad = tmp_path / "bad.db"
    bad.write_text("not a database")
    with pytest.raises(DatabaseError):
        Database.validate_backup(bad)
    db.close()


def test_newer_schema_is_rejected(tmp_path):
    db = Database(tmp_path / "n.db")
    db.migrate()
    db.conn.execute("INSERT INTO schema_migrations VALUES (999, 'future', 0)")
    with pytest.raises(DatabaseError):
        db.migrate()
    db.close()


def test_retention_prunes_measurements_but_keeps_outages(repo):
    from netwatch.models import CheckResult

    sid = repo.create_session(0, {}, "t")
    tid = repo.list_targets()[0].id
    repo.insert_measurements(sid, [CheckResult(tid, "x", "icmp", True, ts, latency_ms=1) for ts in (10, 20, 30, 1000)])
    repo.create_outage(sid, ConnState.NO_INTERNET, 15, 15, True, [])
    assert repo.prune_measurements(100) == 3
    assert repo.db.query_one("SELECT COUNT(*) n FROM measurements")["n"] == 1
    assert repo.db.query_one("SELECT COUNT(*) n FROM outages")["n"] == 1


def test_settings_validation_messages(settings):
    errors = settings.update({"interval_seconds": 5, "timeout_seconds": 10})
    assert errors and "shorter than the check interval" in errors[0].message
    assert settings.get("timeout_seconds") == 2  # nothing persisted
    errors = validate(dict(settings.all(), interval_seconds=0))
    assert any("between 1 and 3600" in e.message for e in errors)
    assert validate_target("x", "999.1.1.1.1", "icmp")
    assert validate_target("", "1.1.1.1", "icmp")
    assert validate_target("DNS", "1.1.1.1", "dns")
    assert not validate_target("Router", "192.168.1.1", "icmp")
    assert not validate_target("v6", "2606:4700:4700::1111", "icmp")
    assert not validate_target("site", "https://example.com/health", "http")


def test_settings_persist(db):
    from netwatch.settings import SettingsStore

    s = SettingsStore(db)
    assert s.update({"interval_seconds": 10, "theme": "dark"}) == []
    s2 = SettingsStore(db)
    assert s2.get("interval_seconds") == 10 and s2.get("theme") == "dark"


def test_duplicate_target_rejected(repo):
    with pytest.raises(DuplicateTargetError):
        repo.add_target("dup", "1.1.1.1", "icmp")
    tid = repo.list_targets()[0].id
    repo.delete_target(tid)
    assert tid in repo.targets_by_id()  # history keeps the reference
    repo.add_target("again", repo.targets_by_id()[tid].address, "icmp")


def test_large_history_is_fast(repo, stats):
    """~20k state changes over a year must aggregate quickly (kept off the UI thread anyway)."""
    t0 = time.time() - 365 * DAY
    sid = repo.create_session(t0, {}, "t")
    rows = []
    t = t0
    for i in range(20000):
        state = ConnState.ONLINE if i % 2 == 0 else ConnState.NO_INTERNET
        dur = 1500.0 if state == ConnState.ONLINE else 60.0
        rows.append((sid, state.value, t, t + dur))
        t += dur
    with repo.tx() as conn:
        conn.executemany(
            "INSERT INTO connection_states(session_id, state, started_at, ended_at) VALUES (?,?,?,?)", rows
        )
    started = time.perf_counter()
    h = stats.history("all", time.time())
    elapsed = time.perf_counter() - started
    assert len(h.segments) >= 20000
    assert elapsed < 3.0, elapsed


def test_delete_range_and_clear_all(repo, stats):
    t0 = 1_700_000_000.0
    seed(repo, t0)
    res = repo.delete_range(t0, t0 + 7200)
    assert res["states"] == 3 and res["outages"] == 1
    live = repo.db.query_one("SELECT id FROM monitoring_sessions WHERE ended_at IS NULL")["id"]
    repo.delete_all(live, t0 + 20000)
    assert repo.db.query_one("SELECT COUNT(*) n FROM connection_states")["n"] == 1
    assert repo.db.query_one("SELECT started_at FROM connection_states")["started_at"] == t0 + 20000


def test_migration_backfills_network_for_existing_history(tmp_path):
    """Databases from 1.0 get the network column filled from the stored interface details."""
    import sqlite3

    from netwatch.models import LocalNetworkStatus, WifiInfo
    from netwatch.storage.migrations import MIGRATIONS

    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at REAL NOT NULL)")
    from netwatch.storage.db import _split_sql

    for stmt in _split_sql(MIGRATIONS[0][2]):
        conn.execute(stmt)
    conn.execute("INSERT INTO schema_migrations VALUES (1, 'initial schema', 0)")
    conn.execute("INSERT INTO monitoring_sessions(id, started_at, status, config_snapshot) VALUES (1, 0, 'stopped', '{}')")
    iface = LocalNetworkStatus(True, "networkmanager", "wlan0", "wifi", wifi=WifiInfo(ssid="Home WiFi")).to_json()
    conn.execute("INSERT INTO connection_states(session_id, state, started_at, ended_at, interface) VALUES (1, 'online', 0, 10, ?)", (iface,))
    conn.execute("INSERT INTO outages(session_id, classification, started_at, detected_at, status, start_observed) "
                 "VALUES (1, 'no_internet', 5, 5, 'recovered', 1)")
    conn.commit()
    conn.close()
    db = Database(path)
    assert db.migrate() == [2]
    assert db.query_one("SELECT network FROM connection_states")["network"] == "Home WiFi"
    assert db.query_one("SELECT network FROM outages")["network"] == "Home WiFi"
    db.close()


def test_reports_name_the_network_of_each_entry(repo, stats, settings):
    t0 = 1_700_000_000.0
    s = repo.create_session(t0, {}, "t")
    a = repo.open_state(s, ConnState.ONLINE, t0, network="Home WiFi")
    repo.close_state(a, t0 + 600)
    b = repo.open_state(s, ConnState.NO_INTERNET, t0 + 600, network="Office")
    repo.create_outage(s, ConnState.NO_INTERNET, t0 + 600, t0 + 600, True, [], network="Office")
    repo.close_state(b, t0 + 900)
    rep = build_report(repo, stats, settings, custom=(t0, t0 + 900), now=t0 + 900)
    txt = render(rep, "txt")
    assert "Networks" in txt and "[Home WiFi]" in txt and "network: Office" in txt
    rows = list(csv.DictReader(io.StringIO(render(rep, "csv"))))
    assert {r["network"] for r in rows if r["record_type"] == "state"} == {"Home WiFi", "Office"}
    assert next(r for r in rows if r["record_type"] == "outage")["network"] == "Office"
    html = render(rep, "html")
    assert "<th>Network</th>" in html and "Home WiFi" in html
