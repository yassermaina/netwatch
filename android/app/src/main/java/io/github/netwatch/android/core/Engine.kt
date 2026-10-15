package io.github.netwatch.android.core

import kotlinx.coroutines.CoroutineDispatcher
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.async
import kotlinx.coroutines.awaitAll
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.coroutineScope
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.launch
import kotlinx.coroutines.withTimeoutOrNull
import org.json.JSONArray
import org.json.JSONObject
import java.util.concurrent.CopyOnWriteArrayList

interface CheckAdapter {
    val checkType: String

    /** Blocking check; called on an IO dispatcher. Must not throw for network failures. */
    fun check(target: Target, timeoutSeconds: Double): CheckResult
}

interface NetworkProbe {
    fun probe(): LocalNetworkStatus
}

interface Clocks {
    fun now(): Double // wall clock, UTC epoch seconds

    fun mono(): Double // monotonic seconds; on Android includes deep sleep (elapsedRealtime)
}

sealed interface EngineEvent {
    data class StateChanged(val transition: Transition) : EngineEvent
    data class LifecycleChanged(val lifecycle: Lifecycle, val message: String) : EngineEvent
    data class Error(val message: String) : EngineEvent
}

private fun evidence(results: List<CheckResult>): JSONArray = JSONArray().apply {
    results.forEach {
        put(JSONObject().put("target", it.address).put("ok", it.success)
            .put("latency_ms", it.latencyMs ?: JSONObject.NULL).put("error", it.error.value).put("detail", it.detail))
    }
}

/**
 * Owns the monitoring session. Lifecycle (STOPPED/RUNNING/PAUSED/ERROR) is separate from the connection
 * state. All database writes for a round happen in one transaction. Port of netwatch/monitor/engine.py.
 */
class MonitorEngine(
    private val repo: Repository,
    private val settings: SettingsStore,
    private val adapters: Map<String, CheckAdapter>,
    private val probe: NetworkProbe,
    private val clocks: Clocks,
    private val version: String,
    private val ioDispatcher: CoroutineDispatcher = Dispatchers.IO,
) {
    val classifier = StateClassifier(settings.int("failure_threshold"), settings.int("recovery_threshold"))
    private val _snapshot = MutableStateFlow(EngineSnapshot())
    val snapshot: StateFlow<EngineSnapshot> = _snapshot
    private val listeners = CopyOnWriteArrayList<(EngineEvent) -> Unit>()

    var sessionId: Long? = null; private set
    var tickCount = 0; private set
    private var targets: List<Target> = emptyList()
    private var stateId: Long? = null
    private var state = ConnState.UNKNOWN
    private var stateStarted = 0.0
    private var ifaceJson: String? = null
    private var outageId: Long? = null
    private var paused = false
    private var lastWall: Double? = null
    private var lastMono: Double? = null
    private var errors = 0

    private var loopJob: Job? = null
    private var commands = Channel<String>(Channel.UNLIMITED)

    val lifecycle: Lifecycle get() = _snapshot.value.lifecycle
    val isActive: Boolean get() = lifecycle == Lifecycle.STARTING || lifecycle == Lifecycle.RUNNING || lifecycle == Lifecycle.PAUSED

    fun addListener(l: (EngineEvent) -> Unit) { listeners += l }
    fun removeListener(l: (EngineEvent) -> Unit) { listeners -= l }

    private fun emit(e: EngineEvent) = listeners.forEach { runCatching { it(e) } }

    private fun setLifecycle(lc: Lifecycle, message: String = "") {
        _snapshot.value = _snapshot.value.copy(lifecycle = lc, message = message)
        emit(EngineEvent.LifecycleChanged(lc, message))
    }

    // ------------------------------------------------------------ background loop
    /** Starts a session and the check loop in [scope] (use a single-threaded dispatcher). */
    fun start(scope: CoroutineScope) {
        if (isActive) return
        commands = Channel(Channel.UNLIMITED)
        setLifecycle(Lifecycle.STARTING)
        loopJob = scope.launch {
            try {
                beginSession(clocks.now())
            } catch (e: Exception) {
                setLifecycle(Lifecycle.ERROR, "Could not start monitoring: ${e.message}")
                return@launch
            }
            var nextDue = clocks.mono()
            while (true) {
                val wait = if (paused) 30.0 else maxOf(0.0, nextDue - clocks.mono())
                val cmd = withTimeoutOrNull((wait * 1000).toLong().coerceAtLeast(1)) { commands.receive() }
                when (cmd) {
                    "stop" -> {
                        runCatching { endSession(clocks.now(), "user") }
                        return@launch
                    }
                    null -> {
                        if (paused) {
                            sessionId?.let { id -> runCatching { repo.heartbeat(id, clocks.now()) } }
                        } else {
                            tick()
                            nextDue += settings.int("interval_seconds")
                            if (nextDue < clocks.mono()) nextDue = clocks.mono()
                        }
                    }
                    else -> {
                        handle(cmd)
                        if (cmd == "resume") nextDue = clocks.mono()
                    }
                }
            }
        }
    }

    fun pause() = command("pause")
    fun resume() = command("resume")
    fun reload() = command("reload")

    /** Stop and wait for the session to be closed. */
    suspend fun stop() {
        val job = loopJob
        if (job == null) {
            if (sessionId != null) endSession(clocks.now(), "user")
            return
        }
        commands.trySend("stop")
        job.join()
        loopJob = null
    }

    private fun command(name: String) {
        if (loopJob == null) {
            if (sessionId != null) handle(name)
        } else {
            commands.trySend(name)
        }
    }

    private fun handle(name: String) {
        val now = clocks.now()
        when {
            name == "pause" && !paused -> pauseAt(now)
            name == "resume" && paused -> resumeAt(now)
            name == "reload" -> {
                targets = repo.listTargets(enabledOnly = true)
                repo.logEvent("settings_changed", "Monitoring configuration changed", sessionId)
            }
        }
    }

    // ------------------------------------------------------------ session lifecycle (also used directly by tests)
    private fun configSnapshot(): JSONObject = JSONObject()
        .put("interval_seconds", settings.int("interval_seconds")).put("timeout_seconds", settings.int("timeout_seconds"))
        .put("failure_threshold", settings.int("failure_threshold")).put("recovery_threshold", settings.int("recovery_threshold"))
        .put("targets", JSONArray().apply {
            targets.forEach { put(JSONObject().put("id", it.id).put("name", it.name).put("address", it.address).put("check_type", it.checkType)) }
        })

    fun beginSession(now: Double) {
        targets = repo.listTargets(enabledOnly = true)
        classifier.reset()
        repo.db.tx {
            sessionId = repo.createSession(now, configSnapshot(), version)
            openState(ConnState.UNKNOWN, now, now, null, "Waiting for the first checks")
            repo.logEvent("session_started", "Monitoring started", sessionId, ts = now)
        }
        paused = false
        outageId = null
        lastWall = now
        lastMono = clocks.mono()
        _snapshot.value = _snapshot.value.copy(sessionId = sessionId, sessionStarted = now, state = ConnState.UNKNOWN,
            stateSince = now, outageId = null, pendingState = null)
        setLifecycle(Lifecycle.RUNNING)
    }

    fun endSession(now: Double, reason: String) {
        val id = sessionId ?: return
        repo.db.tx {
            closeCurrent(now, "monitoring_stopped", "Monitoring stopped before recovery was observed")
            repo.endSession(id, now, "stopped", reason)
            repo.logEvent("session_stopped", "Monitoring stopped ($reason)", id, ts = now)
        }
        sessionId = null
        paused = false
        _snapshot.value = _snapshot.value.copy(sessionId = null, state = ConnState.UNKNOWN, stateSince = null, outageId = null,
            pendingState = null)
        setLifecycle(Lifecycle.STOPPED)
    }

    fun pauseAt(now: Double) {
        val id = sessionId ?: return
        repo.db.tx {
            closeCurrent(now, "monitoring_paused", "Monitoring paused before recovery was observed")
            openState(ConnState.PAUSED, now, now, null, "Monitoring paused by the user")
            repo.heartbeat(id, now)
            repo.logEvent("paused", "Monitoring paused", id, ts = now)
        }
        paused = true
        classifier.reset()
        publishState(ConnState.PAUSED, now)
        setLifecycle(Lifecycle.PAUSED)
    }

    fun resumeAt(now: Double) {
        sessionId ?: return
        repo.db.tx {
            closeCurrent(now)
            openState(ConnState.UNKNOWN, now, now, null, "Waiting for the first checks after resuming")
            repo.logEvent("resumed", "Monitoring resumed", sessionId, ts = now)
        }
        paused = false
        classifier.reset()
        lastWall = now
        lastMono = clocks.mono()
        publishState(ConnState.UNKNOWN, now)
        setLifecycle(Lifecycle.RUNNING)
    }

    private fun openState(s: ConnState, started: Double, detected: Double, targetsDesc: String?, reason: String) {
        stateId = repo.openState(sessionId!!, s, started, detected, targetsDesc, ifaceJson, reason)
        state = s
        stateStarted = started
    }

    private fun closeCurrent(at: Double, outageStatus: String = "monitoring_stopped", note: String = "") {
        stateId?.let { repo.closeState(it, at) }
        stateId = null
        outageId?.let {
            repo.closeOutage(it, at, outageStatus, false,
                evidence = JSONArray().put(JSONObject().put("ts", at).put("event", outageStatus).put("note", note)))
        }
        outageId = null
        _snapshot.value = _snapshot.value.copy(outageId = null)
    }

    private fun publishState(s: ConnState, since: Double) {
        _snapshot.value = _snapshot.value.copy(state = s, stateSince = since, pendingState = null)
    }

    // ------------------------------------------------------------ rounds
    private suspend fun runChecks(timeout: Double): List<CheckResult> = coroutineScope {
        targets.map { t ->
            async(ioDispatcher) {
                val adapter = adapters[t.checkType]
                if (adapter == null) {
                    CheckResult(t.id, t.address, t.checkType, false, clocks.now(), error = ErrorCategory.MISSING_DEPENDENCY,
                        detail = "no adapter for ${t.checkType}")
                } else {
                    try {
                        adapter.check(t, timeout)
                    } catch (e: Exception) {
                        CheckResult(t.id, t.address, t.checkType, false, clocks.now(), error = ErrorCategory.OTHER,
                            detail = (e.message ?: e.javaClass.simpleName).take(200))
                    }
                }
            }
        }.awaitAll()
    }

    /** Detect deep sleep / Doze, clock changes and stalls between rounds; record the gap as unmonitored. */
    private fun handleDiscontinuity(now: Double, mono: Double) {
        val lw = lastWall ?: return
        val lm = lastMono ?: return
        val interval = settings.int("interval_seconds").toDouble()
        val timeout = settings.int("timeout_seconds").toDouble()
        val monoGap = mono - lm
        val drift = now - (lw + monoGap)
        val stalled = monoGap > maxOf(3 * interval + timeout + 5, 60.0)
        val clockChange = Math.abs(drift) > maxOf(10.0, interval)
        if (!clockChange && !stalled) return
        val gapStart = if (drift > 0 || stalled) minOf(lw + interval, now) else lw
        val kind = if (clockChange) "clock_change" else "monitoring_stalled"
        val message = if (clockChange) "Wall clock moved ${"%+.0f".format(drift)}s relative to the monotonic clock (clock or time zone change)"
        else "No checks ran for ${"%.0f".format(monoGap)}s (device sleep, power saving or app suspended)"
        repo.db.tx {
            closeCurrent(maxOf(gapStart, stateStarted), "monitoring_interrupted", "Monitoring was interrupted ($kind)")
            openState(ConnState.UNKNOWN, now, now, null, message)
            repo.logEvent(kind, message, sessionId, JSONObject().put("drift", drift).put("mono_gap", monoGap), now)
        }
        classifier.reset()
        publishState(ConnState.UNKNOWN, now)
    }

    /** Run one round of checks and persist it. Returns a transition if any. */
    suspend fun tick(nowOverride: Double? = null): Transition? {
        if (sessionId == null || paused) return null
        return try {
            val t = doTick(nowOverride)
            errors = 0
            t
        } catch (e: Exception) {
            errors++
            emit(EngineEvent.Error("Monitoring error: ${e.message}"))
            if (errors >= 5) setLifecycle(Lifecycle.ERROR, "Repeated monitoring errors: ${e.message}")
            null
        }
    }

    private suspend fun doTick(nowOverride: Double?): Transition? {
        val mono = clocks.mono()
        val now = nowOverride ?: clocks.now()
        handleDiscontinuity(now, mono)
        classifier.configure(settings.int("failure_threshold"), settings.int("recovery_threshold"))
        val local = try { probe.probe() } catch (e: Exception) { LocalNetworkStatus(null, detail = "local probe failed: ${e.message}") }
        val results = runChecks(settings.int("timeout_seconds").toDouble())
        val id = sessionId ?: return null // stopped while checks ran
        val newIface = if (local.connected == true) local.toJson() else null
        var transition: Transition? = null
        repo.db.tx {
            repo.insertMeasurements(id, results)
            repo.heartbeat(id, now)
            transition = classifier.observe(results, local, now)
            val t = transition
            if (t != null) {
                ifaceJson = newIface
                applyTransition(t, results)
            } else {
                outageId?.let { repo.recordOutageFailures(it, results) }
                if (newIface != null && newIface != ifaceJson && state == ConnState.ONLINE) {
                    ifaceJson = newIface
                    stateId?.let { repo.updateStateInterface(it, newIface) }
                }
            }
        }
        lastWall = now
        lastMono = mono
        tickCount++
        val ok = results.filter { it.success && it.latencyMs != null }
        val names = targets.associate { it.id to it.name }
        val prev = _snapshot.value
        _snapshot.value = prev.copy(
            lastCheckAt = now,
            local = local,
            lastResults = results.map { TargetLive(it.targetId, names[it.targetId] ?: it.address, it.address, it.success, it.latencyMs,
                if (it.success) "" else it.error.label) },
            lastSuccessAt = if (ok.isNotEmpty()) now else prev.lastSuccessAt,
            lastLatencyMs = ok.minOfOrNull { it.latencyMs!! } ?: prev.lastLatencyMs,
            pendingState = classifier.pending,
            reason = classifier.lastReason,
            state = transition?.new ?: prev.state,
            stateSince = if (transition != null) stateStarted else prev.stateSince,
            outageId = outageId,
        )
        transition?.let { emit(EngineEvent.StateChanged(it)) }
        return transition
    }

    private fun applyTransition(t: Transition, results: List<CheckResult>) {
        val effective = maxOf(t.effectiveAt, stateStarted)
        stateId?.let { repo.closeState(it, effective) }
        val desc = (if (t.new == ConnState.ONLINE) results.filter { it.success } else results.filter { !it.success })
            .joinToString(", ") { it.address }.ifBlank { null }
        openState(t.new, effective, t.detectedAt, desc, t.reason)
        val ev = JSONObject().put("ts", t.detectedAt).put("state", t.new.value).put("reason", t.reason).put("results", evidence(results))
        val oid = outageId
        when {
            t.new.isDown && !t.previous.isDown -> {
                ev.put("event", "detected")
                outageId = repo.createOutage(sessionId!!, t.new, effective, t.detectedAt, t.previous == ConnState.ONLINE, JSONArray().put(ev))
                repo.recordOutageFailures(outageId!!, results)
            }
            t.new.isDown && oid != null -> {
                ev.put("event", "reclassified")
                repo.reclassifyOutage(oid, t.new, JSONArray().put(ev))
                repo.recordOutageFailures(oid, results)
            }
            oid != null -> {
                val recovered = t.new == ConnState.ONLINE
                ev.put("event", if (recovered) "recovered" else "evidence_lost")
                repo.closeOutage(oid, effective, if (recovered) "recovered" else "evidence_lost", recovered,
                    if (recovered) t.detectedAt else null, JSONArray().put(ev))
                outageId = null
            }
        }
    }
}
