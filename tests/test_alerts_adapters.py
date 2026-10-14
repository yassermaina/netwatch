"""Alert policy, ping parsing and local network probe fallbacks."""

import os
import wave

from netwatch.alerts import AlertManager, SoundPlayer
from netwatch.models import ErrorCategory, Target
from netwatch.monitor.adapters import IcmpAdapter, classify_ping_failure
from netwatch.monitor.netstate import LocalNetworkProbe, frequency_to_channel, parse_iw_link
from netwatch.monitor.process import ProcessResult
from netwatch.settings import DEFAULTS


class FakePlayer:
    available = True

    def __init__(self):
        self.played, self.stops, self.playing = [], 0, False

    def play(self, kind, volume):
        self.played.append(kind)

    def stop(self):
        self.stops += 1

    def is_playing(self):
        return self.playing


def make(settings_over=None, start=0.0):
    cfg = dict(DEFAULTS, **(settings_over or {}))
    t = {"now": start}
    player = FakePlayer()
    mgr = AlertManager(player, cfg.get, lambda: t["now"])
    return mgr, player, t


def test_outage_alert_plays_once_then_repeats_on_schedule():
    mgr, player, t = make({"sound_repeat_seconds": 10})
    mgr.outage_started()
    mgr.outage_started()  # repeated failure reports do not restart the alert
    assert player.played == ["outage"]
    for step in range(1, 10):
        t["now"] = step
        mgr.tick()
    assert player.played == ["outage"]
    t["now"] = 10
    mgr.tick()
    assert player.played == ["outage", "outage"]
    mgr.outage_ended()
    assert player.played[-1] == "recovery"


def test_muted_outage_is_silent_but_engine_records_it(harness):
    mgr, player, t = make({"muted": True})
    harness.engine.start(threaded=False)
    harness.run(1)
    harness.all_fail()
    harness.run(3)
    mgr.outage_started()
    t["now"] = 100
    mgr.tick()
    assert player.played == []
    assert harness.repo.db.query_one("SELECT COUNT(*) n FROM outages")["n"] == 1


def test_unmute_during_outage_resumes_when_configured():
    mgr, player, t = make({"muted": True, "resume_alert_on_unmute": True})
    mgr.outage_started()
    mgr.set_muted(False)
    assert player.played == ["outage"]
    mgr2, player2, _ = make({"muted": True, "resume_alert_on_unmute": False})
    mgr2.outage_started()
    mgr2.set_muted(False)
    mgr2.tick()
    assert player2.played == []


def test_mute_stops_current_sound_and_recovery_respects_setting():
    mgr, player, t = make({"sound_recovery": False})
    mgr.outage_started()
    mgr.set_muted(True)
    assert player.stops == 1
    mgr.set_muted(False)
    mgr.outage_ended()
    assert "recovery" not in player.played


def test_no_backend_is_graceful(tmp_path, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: None)
    p = SoundPlayer(tmp_path)
    assert not p.available
    p.play("outage", 50)  # no exception
    mgr = AlertManager(p, DEFAULTS.get, lambda: 0)
    mgr.outage_started()
    mgr.outage_ended()


def test_generated_sound_files_are_valid(tmp_path):
    p = SoundPlayer(tmp_path)
    path = p.sound_file("outage", 30)
    with wave.open(str(path)) as w:
        assert w.getnchannels() == 1 and w.getnframes() > 1000


def test_ping_failure_classification():
    assert classify_ping_failure(1, "1 packets transmitted, 0 received", "")[0] == ErrorCategory.TIMEOUT
    assert (
        classify_ping_failure(1, "From 10.0.0.1 icmp_seq=1 Destination Host Unreachable", "")[0]
        == ErrorCategory.UNREACHABLE
    )
    assert classify_ping_failure(2, "", "ping: connect: Network is unreachable")[0] == ErrorCategory.NETWORK_DOWN
    assert classify_ping_failure(2, "", "ping: example.invalid: Name or service not known")[0] == ErrorCategory.DNS
    assert classify_ping_failure(2, "", "ping: socket: Operation not permitted")[0] == ErrorCategory.PERMISSION


class StubRunner:
    def __init__(self, result):
        self.result = result
        self.argv = None

    def run(self, argv, timeout):
        self.argv = argv
        return self.result


def test_icmp_adapter_parses_latency():
    out = "64 bytes from 1.1.1.1: icmp_seq=1 ttl=57 time=14.7 ms\n"
    runner = StubRunner(ProcessResult(0, out, ""))
    r = IcmpAdapter(runner, "/usr/bin/ping").check(Target(1, "c", "1.1.1.1"), 2)
    assert r.success and r.latency_ms == 14.7
    assert runner.argv[-1] == "1.1.1.1" and "-W" in runner.argv


def test_icmp_adapter_missing_ping():
    a = IcmpAdapter(StubRunner(None))
    a.ping_path = None
    r = a.check(Target(1, "c", "1.1.1.1"), 2)
    assert not r.success and r.error == ErrorCategory.MISSING_DEPENDENCY
    assert not a.availability()[0]


def test_sysfs_probe_fallback(tmp_path):
    net = tmp_path / "sys/class/net"
    for name, oper, carrier in (("lo", "unknown", "1"), ("wlan0", "up", "1"), ("eth0", "down", "0")):
        (net / name).mkdir(parents=True)
        (net / name / "operstate").write_text(oper)
        (net / name / "carrier").write_text(carrier)
        (net / name / "type").write_text("1")
    (net / "wlan0" / "wireless").mkdir()
    proc = tmp_path / "proc/net"
    proc.mkdir(parents=True)
    route = "Iface\tDestination\tGateway\tFlags\nwlan0\t00000000\t0101A8C0\t0003\n"
    (proc / "route").write_text(route)
    probe = LocalNetworkProbe(sys_root=str(tmp_path))
    probe._bus_failed = True
    s = probe.probe()
    assert s.connected is True and s.interface == "wlan0" and s.kind == "wifi"
    assert s.describe() == "Wi-Fi"  # nothing fabricated
    (proc / "route").write_text("Iface\tDestination\tGateway\tFlags\n")
    assert probe.probe().connected is False


def test_wifi_helpers():
    assert frequency_to_channel(5180) == 36 and frequency_to_channel(2437) == 6
    info = parse_iw_link(
        "Connected to aa:bb\n\tsignal: -45 dBm\n\trx bitrate: 1200.9 MBit/s 80MHz HE-MCS 11 HE-NSS 2\n"
    )
    assert info == {"signal_dbm": -45, "standard": "802.11ax"}


def test_autostart_entry_is_explicit_and_removable():
    from netwatch.autostart import autostart_enabled, autostart_file, set_autostart

    assert not autostart_enabled()
    set_autostart(True)
    text = autostart_file().read_text()
    assert "--background" in text and "X-GNOME-Autostart-enabled=true" in text
    assert str(autostart_file()).startswith(os.environ["XDG_CONFIG_HOME"])  # XDG path, never system-wide
    set_autostart(False)
    assert not autostart_enabled()
