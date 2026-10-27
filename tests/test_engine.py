"""Monitoring engine and state model scenarios (mocked network, no internet needed)."""

import threading
import time

from netwatch.models import ConnState, ErrorCategory, Lifecycle, LocalNetworkStatus
from netwatch.monitor.classifier import StateClassifier, classify_round
from netwatch.models import CheckResult


def outages(repo):
    return repo.db.query("SELECT * FROM outages ORDER BY id")


def states(repo):
    return repo.db.query("SELECT * FROM connection_states ORDER BY id")


def test_successful_checks_result_in_online(harness):
    harness.engine.start(threaded=False)
    t = harness.run(1)
    assert t and t[0].new == ConnState.ONLINE
    assert harness.engine.snapshot.state == ConnState.ONLINE
    assert harness.engine.lifecycle == Lifecycle.RUNNING
    assert harness.repo.db.query_one("SELECT COUNT(*) n FROM measurements")["n"] == 3


def test_one_failed_target_is_not_an_outage(harness):
    harness.engine.start(threaded=False)
    harness.run(1)
    harness.adapter.set(harness.addresses[0], False)
    harness.run(10)
    assert harness.engine.snapshot.state == ConnState.ONLINE
    assert outages(harness.repo) == []


def test_consecutive_failures_produce_single_outage_per_threshold(harness):
    harness.settings.update({"failure_threshold": 3})
    harness.engine.start(threaded=False)
    harness.run(2)
    harness.all_fail()
    harness.run(2)
    assert harness.engine.snapshot.state == ConnState.ONLINE  # below threshold
    assert outages(harness.repo) == []
    harness.run(1)
    assert harness.engine.snapshot.state == ConnState.NO_INTERNET
    rows = outages(harness.repo)
    assert len(rows) == 1 and rows[0]["status"] == "ongoing" and rows[0]["start_observed"] == 1


def test_repeated_failures_do_not_duplicate_outages(harness):
    harness.engine.start(threaded=False)
    harness.run(1)
    harness.all_fail()
    harness.run(50)
    rows = outages(harness.repo)
    assert len(rows) == 1
    ot = harness.repo.outage_targets(rows[0]["id"])
    assert all(r["failure_count"] >= 49 for r in ot)


def test_recovery_closes_outage_with_correct_duration(harness):
    harness.settings.update({"failure_threshold": 2, "recovery_threshold": 2})
    harness.engine.start(threaded=False)
    harness.run(1)  # t0 online
    first_fail = harness.clock()
    harness.all_fail()
    harness.run(6)  # 30 s of failure
    harness.all_ok()
    first_ok = harness.clock()
    harness.run(2)
    rows = outages(harness.repo)
    assert len(rows) == 1
    o = rows[0]
    assert o["status"] == "recovered" and o["end_observed"] == 1
    assert o["started_at"] == first_fail
    assert o["ended_at"] == first_ok
    assert abs(o["duration"] - 30.0) < 1e-6
    assert o["recovery_detected_at"] == first_ok + 5
    # state intervals are contiguous and non-overlapping
    st = states(harness.repo)
    for a, b in zip(st, st[1:]):
        assert a["ended_at"] == b["started_at"]


def test_not_connected_classification_and_switch_within_outage(harness):
    harness.engine.start(threaded=False)
    harness.run(1)
    harness.all_fail(ErrorCategory.NETWORK_DOWN)
    harness.probe.status = LocalNetworkStatus(connected=False, source="test", detail="no default route")
    harness.run(2)
    assert harness.engine.snapshot.state == ConnState.NOT_CONNECTED
    harness.probe.status = LocalNetworkStatus(connected=True, source="test")
    harness.all_fail()
    harness.run(1)
    assert harness.engine.snapshot.state == ConnState.NO_INTERNET
    assert len(outages(harness.repo)) == 1


def test_public_ip_failure_alone_is_not_local_disconnection():
    results = [CheckResult(1, "1.1.1.1", "icmp", False, 0, error=ErrorCategory.TIMEOUT)]
    state, _ = classify_round(results, LocalNetworkStatus(connected=None))
    assert state == ConnState.NO_INTERNET
    state, _ = classify_round(results, LocalNetworkStatus(connected=True))
    assert state == ConnState.NO_INTERNET


def test_missing_dependency_gives_unknown_diagnostic_state(harness):
    harness.engine.start(threaded=False)
    harness.all_fail(ErrorCategory.MISSING_DEPENDENCY)
    harness.run(5)
    snap = harness.engine.snapshot
    assert snap.state == ConnState.UNKNOWN  # never claimed online
    assert outages(harness.repo) == []
    assert "Required tool missing" in harness.engine.classifier.last_reason
    harness.all_fail(ErrorCategory.PERMISSION)
    harness.run(1)
    assert "Permission denied" in harness.engine.classifier.last_reason


def test_no_measurements_is_never_online():
    c = StateClassifier()
    assert c.observe([], None, 0) is None or c.state != ConnState.ONLINE
    assert c.state != ConnState.ONLINE


def test_pause_creates_identifiable_unmonitored_interval(harness, stats):
    harness.engine.start(threaded=False)
    harness.run(3)
    harness.engine.pause()
    assert harness.engine.lifecycle == Lifecycle.PAUSED
    assert harness.engine.tick() is None  # no checks while paused
    harness.clock.advance(600)
    harness.engine.resume()
    harness.run(2)
    paused = [r for r in states(harness.repo) if r["state"] == "paused"]
    assert len(paused) == 1 and abs((paused[0]["ended_at"] - paused[0]["started_at"]) - 600) < 1e-6
    events = [r["kind"] for r in harness.repo.db.query("SELECT kind FROM application_events")]
    assert "paused" in events and "resumed" in events
    s = stats.summary(harness.clock() - 2000, harness.clock(), harness.clock())
    assert s.paused >= 600 and s.durations[ConnState.ONLINE] < 100


def test_pause_during_outage_closes_it_as_unobserved(harness):
    harness.engine.start(threaded=False)
    harness.run(1)
    harness.all_fail()
    harness.run(3)
    harness.engine.pause()
    o = outages(harness.repo)[0]
    assert o["status"] == "monitoring_paused" and o["end_observed"] == 0


def test_stop_closes_session(harness):
    harness.engine.start(threaded=False)
    harness.run(3)
    harness.engine.stop()
    sess = harness.repo.db.query("SELECT * FROM monitoring_sessions")
    assert len(sess) == 1 and sess[0]["status"] == "stopped" and sess[0]["ended_at"] is not None
    assert harness.repo.db.query_one("SELECT COUNT(*) n FROM connection_states WHERE ended_at IS NULL")["n"] == 0
    assert harness.engine.lifecycle == Lifecycle.STOPPED


def test_interrupted_session_detected_after_restart(harness, stats):
    harness.engine.start(threaded=False)
    harness.run(1)
    harness.all_fail()
    harness.run(4)
    last_beat = harness.clock() - 5
    # simulate a crash: the engine object disappears without stop()
    harness.clock.advance(3600)
    recovered = harness.repo.recover_interrupted_sessions()
    assert len(recovered) == 1 and recovered[0]["ended_at"] == last_beat
    sess = harness.repo.db.query_one("SELECT * FROM monitoring_sessions")
    assert sess["status"] == "interrupted"
    assert harness.repo.db.query_one("SELECT COUNT(*) n FROM connection_states WHERE ended_at IS NULL")["n"] == 0
    o = outages(harness.repo)[0]
    assert o["status"] == "monitoring_interrupted" and o["end_observed"] == 0
    now = harness.clock()
    s = stats.summary(now - 7200, now, now)
    assert s.not_running >= 3600  # the crash gap is not claimed as monitored


def test_clock_jump_is_not_counted_as_monitored(harness, stats):
    harness.engine.start(threaded=False)
    harness.run(3)
    harness.clock.t += 7200  # suspend: wall clock jumps, monotonic does not
    harness.run(2)
    events = [r["kind"] for r in harness.repo.db.query("SELECT kind FROM application_events")]
    assert "clock_change" in events
    now = harness.clock()
    s = stats.summary(now - 10000, now, now)
    assert s.online < 100
    assert s.not_running > 7000


def test_threaded_engine_runs_and_stops_without_orphans(repo, settings):
    from tests.conftest import FakeAdapter, FakeProbe
    from netwatch.monitor.engine import MonitorEngine
    from netwatch.monitor.process import ProcessRunner

    settings.update({"interval_seconds": 1, "timeout_seconds": 1}, validate_all=False)
    runner = ProcessRunner()
    adapter = FakeAdapter(time.time)
    engine = MonitorEngine(repo, settings, {"icmp": adapter}, FakeProbe(), runner)
    seen = []
    engine.add_listener(lambda kind, payload: seen.append(kind))
    engine.start()
    deadline = time.time() + 5
    while engine.tick_count < 2 and time.time() < deadline:
        time.sleep(0.05)
    engine.pause()
    time.sleep(0.2)
    assert engine.lifecycle == Lifecycle.PAUSED
    engine.resume()
    time.sleep(0.2)
    engine.stop()
    assert engine.lifecycle == Lifecycle.STOPPED
    assert runner.active_count() == 0
    assert not [t for t in threading.enumerate() if t.name == "netwatch-monitor"]
    assert "snapshot" in seen and "lifecycle" in seen


def test_stop_kills_in_flight_ping(repo, settings, tmp_path):
    """A hanging check process is terminated on stop and not recorded."""
    import sys
    from tests.conftest import FakeProbe
    from netwatch.monitor.adapters import IcmpAdapter
    from netwatch.monitor.engine import MonitorEngine
    from netwatch.monitor.process import ProcessRunner

    fake_ping = tmp_path / "ping"
    fake_ping.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(30)\n")
    fake_ping.chmod(0o755)
    runner = ProcessRunner()
    engine = MonitorEngine(repo, settings, {"icmp": IcmpAdapter(runner, str(fake_ping))}, FakeProbe(), runner)
    engine.start()
    deadline = time.time() + 5
    while runner.active_count() == 0 and time.time() < deadline:
        time.sleep(0.05)
    assert runner.active_count() > 0
    started = time.time()
    engine.stop()
    assert time.time() - started < 5
    assert runner.active_count() == 0
    assert repo.db.query_one("SELECT COUNT(*) n FROM measurements")["n"] == 0


def wifi(ssid):
    from netwatch.models import WifiInfo

    return LocalNetworkStatus(connected=True, source="test", interface="wlan0", kind="wifi", wifi=WifiInfo(ssid=ssid))


def test_history_entries_record_the_network_and_split_on_change(harness):
    harness.probe.status = wifi("Home WiFi")
    harness.engine.start(threaded=False)
    harness.run(3)
    harness.probe.status = wifi("Office")
    harness.run(3)
    harness.all_fail()
    harness.run(3)
    rows = [r for r in states(harness.repo) if r["state"] != "unknown"]
    assert [(r["state"], r["network"]) for r in rows] == [
        ("online", "Home WiFi"), ("online", "Office"), ("no_internet", "Office")
    ]
    assert outages(harness.repo)[0]["network"] == "Office"
    assert harness.repo.networks() == ["Office", "Home WiFi"]
    kinds = [r["kind"] for r in harness.repo.db.query("SELECT kind FROM application_events")]
    assert "network_changed" in kinds


def test_outage_when_disconnected_remembers_last_network(harness):
    harness.probe.status = wifi("Cafe")
    harness.engine.start(threaded=False)
    harness.run(2)
    harness.probe.status = LocalNetworkStatus(connected=False, source="test")
    harness.all_fail(ErrorCategory.NETWORK_DOWN)
    harness.run(3)
    o = outages(harness.repo)[0]
    assert o["classification"] == "not_connected" and o["network"] == "Cafe"
    nc = [r for r in states(harness.repo) if r["state"] == "not_connected"][0]
    assert nc["network"] is None
