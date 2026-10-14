"""Monitoring engine: owns the monitoring session and the worker thread.

The engine is GTK-free.  It runs checks on a dedicated worker thread (with a
small bounded pool for parallel per-target checks), persists every round in a
single transaction and publishes events to listeners.  Listeners are called
on the worker thread; the GTK application marshals them to the main loop.

Lifecycle (STOPPED/STARTING/RUNNING/PAUSED/ERROR) is kept separate from the
connection state (ONLINE/NO_INTERNET/...).
"""

from __future__ import annotations

import concurrent.futures
import logging
import queue
import threading
import time
from dataclasses import replace
from typing import Callable, Optional

from netwatch import __version__
from netwatch.models import (
    DOWN_STATES,
    CheckResult,
    ConnState,
    EngineSnapshot,
    ErrorCategory,
    Lifecycle,
    LocalNetworkStatus,
    Target,
    Transition,
)
from netwatch.monitor.adapters import CheckAdapter
from netwatch.monitor.classifier import StateClassifier
from netwatch.monitor.netstate import LocalNetworkProbe
from netwatch.monitor.process import ProcessRunner
from netwatch.settings import SettingsStore
from netwatch.storage.repository import Repository

log = logging.getLogger(__name__)

Listener = Callable[[str, object], None]


def _result_evidence(results: list[CheckResult]) -> list[dict]:
    return [
        {
            "target": r.address,
            "ok": r.success,
            "latency_ms": round(r.latency_ms, 2) if r.latency_ms is not None else None,
            "error": r.error.value,
            "detail": r.detail,
        }
        for r in results
    ]


class MonitorEngine:
    MAX_CONSECUTIVE_ERRORS = 5

    def __init__(
        self,
        repo: Repository,
        settings: SettingsStore,
        adapters: dict[str, CheckAdapter],
        probe: LocalNetworkProbe,
        runner: ProcessRunner,
        clock: Callable[[], float] = time.time,
        mono: Callable[[], float] = time.monotonic,
    ):
        self.repo = repo
        self.settings = settings
        self.adapters = adapters
        self.probe = probe
        self.runner = runner
        self.clock = clock
        self.mono = mono
        self.classifier = StateClassifier(settings.get("failure_threshold"), settings.get("recovery_threshold"))
        self._listeners: list[Listener] = []
        self._lock = threading.RLock()
        self._snapshot = EngineSnapshot()
        self._queue: queue.Queue = queue.Queue()
        self._thread: Optional[threading.Thread] = None
        self._pool: Optional[concurrent.futures.ThreadPoolExecutor] = None
        self._stopping = threading.Event()
        self._targets: list[Target] = []
        # session state (owned by the worker thread)
        self.session_id: Optional[int] = None
        self._state_id: Optional[int] = None
        self._state: ConnState = ConnState.UNKNOWN
        self._state_started: float = 0.0
        self._interface_json: Optional[str] = None
        self._outage_id: Optional[int] = None
        self._paused = False
        self._last_wall: Optional[float] = None
        self._last_mono: Optional[float] = None
        self._errors = 0
        self.tick_count = 0

    # ------------------------------------------------------------ listeners
    def add_listener(self, listener: Listener) -> None:
        self._listeners.append(listener)

    def _emit(self, kind: str, payload: object) -> None:
        for listener in list(self._listeners):
            try:
                listener(kind, payload)
            except Exception:  # a broken listener must not kill monitoring
                log.exception("engine listener failed")

    @property
    def snapshot(self) -> EngineSnapshot:
        with self._lock:
            return replace(self._snapshot, last_results=list(self._snapshot.last_results))

    @property
    def lifecycle(self) -> Lifecycle:
        with self._lock:
            return self._snapshot.lifecycle

    def _set_lifecycle(self, lifecycle: Lifecycle, message: str = "") -> None:
        with self._lock:
            self._snapshot.lifecycle = lifecycle
            self._snapshot.message = message
        self._emit("lifecycle", (lifecycle, message))
        self._emit("snapshot", self.snapshot)

    # ------------------------------------------------------------ public API
    @property
    def is_active(self) -> bool:
        return self.lifecycle in (Lifecycle.STARTING, Lifecycle.RUNNING, Lifecycle.PAUSED)

    def start(self, threaded: bool = True) -> None:
        """Begin a new monitoring session."""
        if self.is_active:
            return
        self._stopping.clear()
        self.runner.reopen()
        self._set_lifecycle(Lifecycle.STARTING)
        self._pool = concurrent.futures.ThreadPoolExecutor(max_workers=8, thread_name_prefix="nw-check")
        self._queue = queue.Queue()
        if threaded:
            self._thread = threading.Thread(target=self._run, name="netwatch-monitor", daemon=True)
            self._thread.start()
        else:
            self._begin_session(self.clock())

    def pause(self) -> None:
        self._command("pause")

    def resume(self) -> None:
        self._command("resume")

    def reload(self) -> None:
        """Re-read targets (settings are read live on every tick)."""
        self._command("reload")

    def stop(self, reason: str = "normal", timeout: float = 10.0) -> None:
        if not self.is_active and self._thread is None:
            return
        if self._thread is None:  # synchronous mode
            self._end_session(self.clock(), reason)
            self._shutdown_pool()
            return
        self._stopping.set()
        self._queue.put(("stop", reason))
        self.runner.terminate_all()  # abort in-flight pings; their results are discarded
        self._thread.join(timeout)
        if self._thread.is_alive():
            log.error("monitor thread did not stop within %ss", timeout)
        self._thread = None
        self._shutdown_pool()

    def _command(self, name: str, arg: object = None) -> None:
        if self._thread is None:
            if self.session_id is not None:
                self._handle(name, arg)
            return
        self._queue.put((name, arg))

    def _shutdown_pool(self) -> None:
        if self._pool:
            self._pool.shutdown(wait=True, cancel_futures=True)
            self._pool = None

    # ------------------------------------------------------------ worker
    def _run(self) -> None:
        try:
            self._begin_session(self.clock())
        except Exception as exc:
            log.exception("could not start monitoring session")
            self._set_lifecycle(Lifecycle.ERROR, f"Could not start monitoring: {exc}")
            return
        next_due = self.mono()
        while True:
            wait = 30.0 if self._paused else max(0.0, next_due - self.mono())
            try:
                name, arg = self._queue.get(timeout=wait)
            except queue.Empty:
                name, arg = None, None
            if name == "stop":
                try:
                    self._end_session(self.clock(), str(arg))
                except Exception:
                    log.exception("error while ending session")
                    self._set_lifecycle(Lifecycle.STOPPED, "Stopped with errors")
                return
            if name is not None:
                self._handle(name, arg)
                if name == "resume":
                    next_due = self.mono()
                continue
            if self._paused:
                self._safe(lambda: self.repo.heartbeat(self.session_id, self.clock()))
                continue
            self.tick()
            interval = float(self.settings.get("interval_seconds"))
            next_due += interval
            if next_due < self.mono():
                next_due = self.mono()

    def _safe(self, fn: Callable[[], None]) -> None:
        try:
            fn()
        except Exception:
            log.exception("monitoring error")

    def _handle(self, name: str, arg: object) -> None:
        now = self.clock()
        if name == "pause" and not self._paused:
            self._pause(now)
        elif name == "resume" and self._paused:
            self._resume(now)
        elif name == "reload":
            self._targets = self.repo.list_targets(enabled_only=True)
            self.repo.log_event("settings_changed", "Monitoring configuration changed", self.session_id)

    # ------------------------------------------------------------ session lifecycle
    def _config_snapshot(self) -> dict:
        s = self.settings.all()
        return {
            "interval_seconds": s["interval_seconds"],
            "timeout_seconds": s["timeout_seconds"],
            "failure_threshold": s["failure_threshold"],
            "recovery_threshold": s["recovery_threshold"],
            "targets": [
                {"id": t.id, "name": t.name, "address": t.address, "check_type": t.check_type} for t in self._targets
            ],
        }

    def _begin_session(self, now: float) -> None:
        self._targets = self.repo.list_targets(enabled_only=True)
        self.classifier.reset()
        with self.repo.tx():
            self.session_id = self.repo.create_session(now, self._config_snapshot(), __version__)
            self._open_state(ConnState.UNKNOWN, now, now, reason="Waiting for the first checks")
            self.repo.log_event("session_started", "Monitoring started", self.session_id, ts=now)
        self._paused = False
        self._outage_id = None
        self._last_wall, self._last_mono = now, self.mono()
        with self._lock:
            self._snapshot.session_id = self.session_id
            self._snapshot.session_started = now
            self._snapshot.state = ConnState.UNKNOWN
            self._snapshot.state_since = now
            self._snapshot.outage_id = None
        self._set_lifecycle(Lifecycle.RUNNING)

    def _end_session(self, now: float, reason: str) -> None:
        if self.session_id is None:
            return
        with self.repo.tx():
            self._close_current(
                now, outage_status="monitoring_stopped", note="Monitoring stopped before recovery was observed"
            )
            self.repo.end_session(self.session_id, now, "stopped", reason)
            self.repo.log_event("session_stopped", f"Monitoring stopped ({reason})", self.session_id, ts=now)
        self.session_id = None
        self._paused = False
        with self._lock:
            self._snapshot.session_id = None
            self._snapshot.state = ConnState.UNKNOWN
            self._snapshot.state_since = None
            self._snapshot.outage_id = None
            self._snapshot.pending_state = None
        self._set_lifecycle(Lifecycle.STOPPED)

    def _pause(self, now: float) -> None:
        with self.repo.tx():
            self._close_current(
                now, outage_status="monitoring_paused", note="Monitoring paused before recovery was observed"
            )
            self._open_state(ConnState.PAUSED, now, now, reason="Monitoring paused by the user")
            self.repo.heartbeat(self.session_id, now)
            self.repo.log_event("paused", "Monitoring paused", self.session_id, ts=now)
        self._paused = True
        self.classifier.reset()
        self._publish_state(ConnState.PAUSED, now)
        self._set_lifecycle(Lifecycle.PAUSED)

    def _resume(self, now: float) -> None:
        with self.repo.tx():
            self._close_current(now)
            self._open_state(ConnState.UNKNOWN, now, now, reason="Waiting for the first checks after resuming")
            self.repo.log_event("resumed", "Monitoring resumed", self.session_id, ts=now)
        self._paused = False
        self.classifier.reset()
        self._last_wall, self._last_mono = now, self.mono()
        self._publish_state(ConnState.UNKNOWN, now)
        self._set_lifecycle(Lifecycle.RUNNING)

    def _open_state(
        self, state: ConnState, started: float, detected: float, targets: Optional[str] = None, reason: str = ""
    ) -> None:
        self._state_id = self.repo.open_state(
            self.session_id, state, started, detected, targets, self._interface_json, reason
        )
        self._state = state
        self._state_started = started

    def _close_current(self, at: float, outage_status: str = "monitoring_stopped", note: str = "") -> None:
        if self._state_id is not None:
            self.repo.close_state(self._state_id, at)
            self._state_id = None
        if self._outage_id is not None:
            self.repo.close_outage(
                self._outage_id,
                at,
                outage_status,
                end_observed=False,
                evidence=[{"ts": at, "event": outage_status, "note": note}],
            )
            self._outage_id = None
            with self._lock:
                self._snapshot.outage_id = None

    def _publish_state(self, state: ConnState, since: float) -> None:
        with self._lock:
            self._snapshot.state = state
            self._snapshot.state_since = since
            self._snapshot.pending_state = None

    # ------------------------------------------------------------ ticks
    def _run_checks(self, targets: list[Target], timeout: float) -> list[CheckResult]:
        if not targets:
            return []

        def one(t: Target) -> CheckResult:
            adapter = self.adapters.get(t.check_type)
            if adapter is None:
                return CheckResult(
                    t.id,
                    t.address,
                    t.check_type,
                    False,
                    time.time(),
                    error=ErrorCategory.MISSING_DEPENDENCY,
                    detail=f"no adapter for {t.check_type}",
                )
            try:
                return adapter.check(t, timeout)
            except Exception as exc:  # adapter bug: record, do not crash
                log.exception("adapter %s failed", t.check_type)
                return CheckResult(
                    t.id, t.address, t.check_type, False, time.time(), error=ErrorCategory.OTHER, detail=str(exc)[:200]
                )

        if self._pool is None or len(targets) == 1:
            return [one(t) for t in targets]
        return list(self._pool.map(one, targets))

    def _handle_discontinuity(self, now: float, mono: float) -> None:
        """Detect suspend/resume, clock changes and stalls between ticks."""
        if self._last_wall is None or self._last_mono is None:
            return
        interval = float(self.settings.get("interval_seconds"))
        timeout = float(self.settings.get("timeout_seconds"))
        mono_gap = mono - self._last_mono
        expected = self._last_wall + mono_gap
        drift = now - expected
        stalled = mono_gap > max(3 * interval + timeout + 5, 60)
        if abs(drift) <= max(10.0, interval) and not stalled:
            return
        gap_start = min(self._last_wall + interval, now) if drift > 0 or stalled else self._last_wall
        kind = "clock_change" if abs(drift) > max(10.0, interval) else "monitoring_stalled"
        message = (
            f"Wall clock moved {drift:+.0f}s relative to the monotonic clock (suspend, resume or clock change)"
            if kind == "clock_change"
            else f"No checks ran for {mono_gap:.0f}s"
        )
        with self.repo.tx():
            self._close_current(
                max(gap_start, self._state_started),
                outage_status="monitoring_interrupted",
                note="Monitoring was interrupted (suspend, clock change or stall)",
            )
            self._open_state(ConnState.UNKNOWN, now, now, reason=message)
            self.repo.log_event(kind, message, self.session_id, data={"drift": drift, "mono_gap": mono_gap}, ts=now)
        self.classifier.reset()
        self._publish_state(ConnState.UNKNOWN, now)

    def tick(self, now: Optional[float] = None) -> Optional[Transition]:
        """Run one round of checks and persist it.  Returns a transition if any."""
        if self.session_id is None or self._paused:
            return None
        try:
            transition = self._tick(now)
            self._errors = 0
            return transition
        except Exception as exc:
            self._errors += 1
            log.exception("tick failed")
            self._emit("error", f"Monitoring error: {exc}")
            if self._errors >= self.MAX_CONSECUTIVE_ERRORS:
                self._set_lifecycle(Lifecycle.ERROR, f"Repeated monitoring errors: {exc}")
            return None

    def _tick(self, now: Optional[float]) -> Optional[Transition]:
        mono = self.mono()
        now = self.clock() if now is None else now
        self._handle_discontinuity(now, mono)
        self.classifier.configure(self.settings.get("failure_threshold"), self.settings.get("recovery_threshold"))
        try:
            local = self.probe.probe()
        except Exception as exc:
            local = LocalNetworkStatus(connected=None, detail=f"local probe failed: {exc}")
        results = self._run_checks(self._targets, float(self.settings.get("timeout_seconds")))
        if self._stopping.is_set():
            return None  # checks were aborted by shutdown; do not record them
        interface_json = local.to_json() if local.connected else None
        transition = None
        with self.repo.tx():
            self.repo.insert_measurements(self.session_id, results)
            self.repo.heartbeat(self.session_id, now)
            transition = self.classifier.observe(results, local, now)
            if transition is not None:
                self._interface_json = interface_json
                self._apply_transition(transition, results)
            else:
                if self._outage_id is not None:
                    self.repo.record_outage_failures(self._outage_id, results)
                if interface_json and interface_json != self._interface_json and self._state == ConnState.ONLINE:
                    self._interface_json = interface_json
                    if self._state_id is not None:
                        self.repo.update_state_interface(self._state_id, interface_json)
        self._last_wall, self._last_mono = now, mono
        self.tick_count += 1
        successes = [r for r in results if r.success and r.latency_ms is not None]
        with self._lock:
            snap = self._snapshot
            snap.last_check_at = now
            snap.local = local
            snap.last_results = [
                {
                    "target_id": r.target_id,
                    "address": r.address,
                    "name": next((t.name for t in self._targets if t.id == r.target_id), r.address),
                    "success": r.success,
                    "latency_ms": r.latency_ms,
                    "error": r.error.label if not r.success else "",
                    "detail": r.detail,
                }
                for r in results
            ]
            if successes:
                snap.last_success_at = now
                snap.last_latency_ms = min(r.latency_ms for r in successes)
            snap.pending_state = self.classifier.pending
            if transition is not None:
                snap.state = transition.new
                snap.state_since = self._state_started
                snap.outage_id = self._outage_id
        if transition is not None:
            self._emit("transition", transition)
        self._emit("snapshot", self.snapshot)
        return transition

    def _apply_transition(self, t: Transition, results: list[CheckResult]) -> None:
        effective = max(t.effective_at, self._state_started)
        if self._state_id is not None:
            self.repo.close_state(self._state_id, effective)
        if t.new == ConnState.ONLINE:
            targets = ", ".join(r.address for r in results if r.success)
        else:
            targets = ", ".join(r.address for r in results if not r.success)
        self._open_state(t.new, effective, t.detected_at, targets or None, t.reason)
        evidence = [
            {
                "ts": t.detected_at,
                "event": "",
                "state": t.new.value,
                "reason": t.reason,
                "results": _result_evidence(results),
            }
        ]
        if t.new in DOWN_STATES and t.previous not in DOWN_STATES:
            evidence[0]["event"] = "detected"
            self._outage_id = self.repo.create_outage(
                self.session_id,
                t.new,
                effective,
                t.detected_at,
                start_observed=t.previous == ConnState.ONLINE,
                evidence=evidence,
            )
            self.repo.record_outage_failures(self._outage_id, results)
        elif t.new in DOWN_STATES and self._outage_id is not None:
            evidence[0]["event"] = "reclassified"
            self.repo.reclassify_outage(self._outage_id, t.new, evidence)
            self.repo.record_outage_failures(self._outage_id, results)
        elif self._outage_id is not None:
            recovered = t.new == ConnState.ONLINE
            evidence[0]["event"] = "recovered" if recovered else "evidence_lost"
            self.repo.close_outage(
                self._outage_id,
                effective,
                "recovered" if recovered else "evidence_lost",
                end_observed=recovered,
                recovery_detected_at=t.detected_at if recovered else None,
                evidence=evidence,
            )
            self._outage_id = None
