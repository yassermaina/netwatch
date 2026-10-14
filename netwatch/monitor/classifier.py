"""Connectivity state classification with failure / recovery thresholds.

Pure logic, no I/O: given the results of one round of checks plus the local
network status, decide the raw state for that round, then apply hysteresis
before confirming a transition.
"""

from __future__ import annotations

from typing import Optional

from netwatch.models import DOWN_STATES, CheckResult, ConnState, LocalNetworkStatus, Transition


def classify_round(results: list[CheckResult], local: Optional[LocalNetworkStatus]) -> tuple[ConnState, str]:
    """Raw state of one check round (before thresholds)."""
    successes = [r for r in results if r.success]
    if successes:
        # Any responding target proves internet reachability, whatever the
        # local probe believes (it can be wrong with VPNs / policy routing).
        return ConnState.ONLINE, f"{len(successes)}/{len(results)} targets responded"
    if local is not None and local.connected is False:
        return ConnState.NOT_CONNECTED, local.detail or "no active network connection"
    if not results:
        return ConnState.UNKNOWN, "no enabled targets to check"
    if all(r.error.is_diagnostic for r in results):
        cats = sorted({r.error.label for r in results})
        return ConnState.UNKNOWN, "checks could not run: " + ", ".join(cats)
    measured = [r for r in results if not r.error.is_diagnostic]
    cats = sorted({r.error.label for r in measured})
    return ConnState.NO_INTERNET, f"all {len(measured)} measurable targets failed ({', '.join(cats)})"


class StateClassifier:
    def __init__(self, failure_threshold: int = 2, recovery_threshold: int = 2):
        self.failure_threshold = max(1, int(failure_threshold))
        self.recovery_threshold = max(1, int(recovery_threshold))
        self.state = ConnState.UNKNOWN
        self.ever_confirmed = False
        self.pending: Optional[ConnState] = None
        self.pending_count = 0
        self.pending_since: Optional[float] = None
        self.last_reason = ""

    def configure(self, failure_threshold: int, recovery_threshold: int) -> None:
        self.failure_threshold = max(1, int(failure_threshold))
        self.recovery_threshold = max(1, int(recovery_threshold))

    def reset(self) -> None:
        """Forget all evidence (used after pause/resume or start)."""
        self.state = ConnState.UNKNOWN
        self.ever_confirmed = False
        self._clear_pending()

    def _clear_pending(self) -> None:
        self.pending = None
        self.pending_count = 0
        self.pending_since = None

    def _required(self, raw: ConnState) -> int:
        if self.state in DOWN_STATES and raw in DOWN_STATES:
            return 1  # switching failure kind inside one outage
        if raw == ConnState.ONLINE:
            # No outage to recover from at startup: confirm on first success.
            return 1 if self.state == ConnState.UNKNOWN and not self.ever_confirmed else self.recovery_threshold
        return self.failure_threshold

    def observe(
        self, results: list[CheckResult], local: Optional[LocalNetworkStatus], now: float
    ) -> Optional[Transition]:
        raw, reason = classify_round(results, local)
        self.last_reason = reason
        if raw == self.state:
            self._clear_pending()
            return None
        if raw != self.pending:
            self.pending = raw
            self.pending_count = 0
            self.pending_since = now
        self.pending_count += 1
        if self.pending_count < self._required(raw):
            return None
        transition = Transition(
            previous=self.state,
            new=raw,
            effective_at=self.pending_since if self.pending_since is not None else now,
            detected_at=now,
            reason=reason,
        )
        self.state = raw
        if raw != ConnState.UNKNOWN:
            self.ever_confirmed = True
        self._clear_pending()
        return transition
