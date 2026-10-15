package io.github.netwatch.android.core

/** Raw state of one check round, before thresholds are applied. */
fun classifyRound(results: List<CheckResult>, local: LocalNetworkStatus?): Pair<ConnState, String> {
    val successes = results.count { it.success }
    if (successes > 0) {
        // Any responding target proves internet reachability, whatever the local probe believes.
        return ConnState.ONLINE to "$successes/${results.size} targets responded"
    }
    if (local?.blocked == true) {
        // Doze / data saver: the system blocks our sockets, which says nothing about the network.
        return ConnState.UNKNOWN to "network access is restricted by the system (power saving or data saver)"
    }
    if (local?.connected == false) {
        return ConnState.NOT_CONNECTED to local.detail.ifBlank { "no active network connection" }
    }
    if (results.isEmpty()) return ConnState.UNKNOWN to "no enabled targets to check"
    if (results.all { it.error.isDiagnostic }) {
        val cats = results.map { it.error.label }.toSortedSet().joinToString(", ")
        return ConnState.UNKNOWN to "checks could not run: $cats"
    }
    val measured = results.filterNot { it.error.isDiagnostic }
    val cats = measured.map { it.error.label }.toSortedSet().joinToString(", ")
    return ConnState.NO_INTERNET to "all ${measured.size} measurable targets failed ($cats)"
}

/** Applies failure / recovery thresholds (hysteresis) before confirming a state change. */
class StateClassifier(failureThreshold: Int = 2, recoveryThreshold: Int = 2) {
    var failureThreshold = failureThreshold.coerceAtLeast(1); private set
    var recoveryThreshold = recoveryThreshold.coerceAtLeast(1); private set
    var state = ConnState.UNKNOWN; private set
    var pending: ConnState? = null; private set
    var lastReason = ""; private set
    private var everConfirmed = false
    private var pendingCount = 0
    private var pendingSince: Double? = null

    fun configure(failure: Int, recovery: Int) {
        failureThreshold = failure.coerceAtLeast(1)
        recoveryThreshold = recovery.coerceAtLeast(1)
    }

    fun reset() {
        state = ConnState.UNKNOWN
        everConfirmed = false
        clearPending()
    }

    private fun clearPending() {
        pending = null
        pendingCount = 0
        pendingSince = null
    }

    private fun required(raw: ConnState): Int = when {
        state.isDown && raw.isDown -> 1 // switching failure kind inside one outage
        raw == ConnState.ONLINE -> if (state == ConnState.UNKNOWN && !everConfirmed) 1 else recoveryThreshold
        else -> failureThreshold
    }

    fun observe(results: List<CheckResult>, local: LocalNetworkStatus?, now: Double): Transition? {
        val (raw, reason) = classifyRound(results, local)
        lastReason = reason
        if (raw == state) {
            clearPending()
            return null
        }
        if (raw != pending) {
            pending = raw
            pendingCount = 0
            pendingSince = now
        }
        pendingCount++
        if (pendingCount < required(raw)) return null
        val t = Transition(state, raw, pendingSince ?: now, now, reason)
        state = raw
        if (raw != ConnState.UNKNOWN) everConfirmed = true
        clearPending()
        return t
    }
}
