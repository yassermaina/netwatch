import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from netwatch.models import CheckResult, ConnState, ErrorCategory, LocalNetworkStatus  # noqa: E402
from netwatch.monitor.adapters import CheckAdapter  # noqa: E402
from netwatch.monitor.engine import MonitorEngine  # noqa: E402
from netwatch.monitor.process import ProcessRunner  # noqa: E402
from netwatch.settings import DEFAULT_TARGETS, SettingsStore  # noqa: E402
from netwatch.stats import ReliabilityService  # noqa: E402
from netwatch.storage.db import Database  # noqa: E402
from netwatch.storage.repository import Repository  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_xdg(tmp_path, monkeypatch):
    for var in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME"):
        monkeypatch.setenv(var, str(tmp_path / var.lower()))
    monkeypatch.delenv("NETWATCH_DB", raising=False)


class Clock:
    def __init__(self, start=1_700_000_000.0):
        self.t = start
        self.m = 1000.0

    def __call__(self):
        return self.t

    def mono(self):
        return self.m

    def advance(self, seconds):
        self.t += seconds
        self.m += seconds


class FakeAdapter(CheckAdapter):
    """Scripted results per address: True / False / ErrorCategory."""

    check_type = "icmp"

    def __init__(self, clock):
        self.clock = clock
        self.outcomes = {}

    def set(self, address, outcome):
        self.outcomes[address] = outcome

    def check(self, target, timeout):
        outcome = self.outcomes.get(target.address, True)
        if outcome is True:
            return CheckResult(target.id, target.address, "icmp", True, self.clock(), latency_ms=12.5, packets_received=1)
        error = outcome if isinstance(outcome, ErrorCategory) else ErrorCategory.TIMEOUT
        return CheckResult(target.id, target.address, "icmp", False, self.clock(), error=error, detail=error.label)


class FakeProbe:
    def __init__(self):
        self.status = LocalNetworkStatus(connected=True, source="test", interface="eth0", kind="ethernet")

    def probe(self):
        return self.status

    def describe_backend(self):
        return "test"


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "test.db")
    database.migrate()
    yield database
    database.close()


@pytest.fixture
def repo(db):
    r = Repository(db)
    r.ensure_default_targets(DEFAULT_TARGETS)
    return r


@pytest.fixture
def settings(db):
    return SettingsStore(db)


@pytest.fixture
def stats(repo):
    return ReliabilityService(repo)


@pytest.fixture
def harness(repo, settings, clock):
    adapter = FakeAdapter(clock)
    probe = FakeProbe()
    engine = MonitorEngine(repo, settings, {"icmp": adapter}, probe, ProcessRunner(), clock=clock, mono=clock.mono)

    class H:
        pass

    h = H()
    h.engine, h.adapter, h.probe, h.clock, h.repo, h.settings = engine, adapter, probe, clock, repo, settings

    def run(n=1, step=5.0):
        transitions = []
        for _ in range(n):
            t = engine.tick()
            if t:
                transitions.append(t)
            clock.advance(step)
        return transitions

    h.run = run
    h.addresses = [t.address for t in repo.list_targets()]

    def all_fail(outcome=False):
        for a in h.addresses:
            adapter.set(a, outcome)

    def all_ok():
        for a in h.addresses:
            adapter.set(a, True)

    h.all_fail, h.all_ok = all_fail, all_ok
    return h


__all__ = ["ConnState", "ErrorCategory", "LocalNetworkStatus", "os"]
