package io.github.netwatch.android.platform

import android.app.Application
import android.content.Context
import android.content.Intent
import android.os.Build
import android.os.Handler
import android.os.Looper
import android.os.PowerManager
import android.os.SystemClock
import android.text.format.DateFormat
import io.github.netwatch.android.core.AlertManager
import io.github.netwatch.android.core.Clocks
import io.github.netwatch.android.core.Defaults
import io.github.netwatch.android.core.EngineEvent
import io.github.netwatch.android.core.Fmt
import io.github.netwatch.android.core.Lifecycle
import io.github.netwatch.android.core.MonitorEngine
import io.github.netwatch.android.core.ReliabilityService
import io.github.netwatch.android.core.Repository
import io.github.netwatch.android.core.Schema
import io.github.netwatch.android.core.SettingsStore
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.asCoroutineDispatcher
import kotlinx.coroutines.launch
import kotlinx.coroutines.runBlocking
import java.io.InputStream
import java.util.concurrent.CopyOnWriteArrayList
import java.util.concurrent.Executors

/**
 * Process-wide container: owns the database, engine, alerts and notifications. The engine runs on its own
 * single thread; engine events are marshalled to the main thread before touching alerts/notifications/UI.
 */
class NetWatchApp : Application() {
    lateinit var db: AndroidSqlDb; private set
    lateinit var repo: Repository; private set
    lateinit var settings: SettingsStore; private set
    lateinit var stats: ReliabilityService; private set
    lateinit var engine: MonitorEngine; private set
    lateinit var alerts: AlertManager; private set
    lateinit var notifier: Notifier; private set
    lateinit var probe: AndroidNetworkProbe; private set
    lateinit var sound: AndroidSoundPlayer; private set
    var recovered: List<Repository.Recovered> = emptyList(); private set

    val main = Handler(Looper.getMainLooper())
    private val engineThread = Executors.newSingleThreadExecutor { r -> Thread(r, "netwatch-monitor") }.asCoroutineDispatcher()
    val engineScope = CoroutineScope(SupervisorJob() + engineThread)
    val ioScope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private var outageStart: Double? = null
    private var wakeLock: PowerManager.WakeLock? = null
    private val historyListeners = CopyOnWriteArrayList<() -> Unit>()

    val clocks = object : Clocks {
        override fun now() = System.currentTimeMillis() / 1000.0
        override fun mono() = SystemClock.elapsedRealtime() / 1000.0 // includes deep sleep
    }

    override fun onCreate() {
        super.onCreate()
        Fmt.use24h = DateFormat.is24HourFormat(this)
        db = AndroidSqlDb(this)
        Schema.migrate(db, clocks.now())
        repo = Repository(db, clocks::now)
        settings = SettingsStore(db, clocks::now)
        stats = ReliabilityService(repo)
        repo.ensureDefaultTargets(Defaults.targets)
        recovered = repo.recoverInterruptedSessions()
        repo.logEvent("app_started", "NetWatch for Android started")
        probe = AndroidNetworkProbe(this)
        sound = AndroidSoundPlayer(this) { settings.bool("vibrate") }
        if (settings.bool("start_muted")) settings.set("muted", true)
        alerts = AlertManager(sound, settings, clocks::now)
        notifier = Notifier(this, settings).also { it.createChannels() }
        engine = MonitorEngine(repo, settings, buildAdapters(), probe, clocks, versionName())
        engine.addListener { e -> main.post { onEngineEvent(e) } }
        main.postDelayed(::tick, 1000)
        main.postDelayed({ prune() }, 60_000)
    }

    fun versionName(): String = runCatching { packageManager.getPackageInfo(packageName, 0).versionName ?: "" }.getOrDefault("")

    // ------------------------------------------------------------ control (called by the service)
    fun startEngine() {
        if (engine.isActive) return
        probe.register()
        acquireWakeLock()
        engine.start(engineScope)
    }

    fun stopEngineBlocking() {
        runBlocking(engineThread) { engine.stop() }
        alerts.outageEnded(recovered = false)
        probe.unregister()
        releaseWakeLock()
        notifyHistoryChanged()
    }

    fun setMuted(muted: Boolean) {
        settings.set("muted", muted)
        alerts.setMuted(muted)
        MonitorService.refreshNotification(this)
    }

    /** Ask the foreground service to start/stop; the service owns the engine lifecycle. */
    fun requestStart(context: Context = this) {
        val i = Intent(context, MonitorService::class.java).setAction(MonitorService.ACTION_START)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) context.startForegroundService(i) else context.startService(i)
    }

    fun requestStop(context: Context = this) {
        context.startService(Intent(context, MonitorService::class.java).setAction(MonitorService.ACTION_STOP))
    }

    @Suppress("WakelockTimeout")
    private fun acquireWakeLock() {
        if (wakeLock?.isHeld == true) return
        val pm = getSystemService(PowerManager::class.java)
        // Keeps checks on schedule while the screen is off. The system may still defer work in Doze;
        // such gaps are detected and recorded as unmonitored.
        wakeLock = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "NetWatch::monitor").apply { setReferenceCounted(false); acquire() }
    }

    private fun releaseWakeLock() {
        wakeLock?.let { if (it.isHeld) it.release() }
        wakeLock = null
    }

    // ------------------------------------------------------------ events (main thread)
    private fun onEngineEvent(e: EngineEvent) {
        when (e) {
            is EngineEvent.StateChanged -> {
                val t = e.transition
                if (t.new.isDown && !t.previous.isDown) {
                    outageStart = t.effectiveAt
                    alerts.outageStarted()
                    notifier.outage(t.new)
                } else if (t.previous.isDown && !t.new.isDown) {
                    val recovered = t.new == io.github.netwatch.android.core.ConnState.ONLINE
                    alerts.outageEnded(recovered)
                    if (recovered) notifier.recovery(outageStart?.let { t.effectiveAt - it })
                    outageStart = null
                }
                notifyHistoryChanged()
            }
            is EngineEvent.LifecycleChanged -> {
                if (e.lifecycle == Lifecycle.PAUSED || e.lifecycle == Lifecycle.STOPPED) {
                    alerts.outageEnded(recovered = false)
                    outageStart = null
                }
                if (e.lifecycle == Lifecycle.ERROR) notifier.error(e.message)
                notifyHistoryChanged()
            }
            is EngineEvent.Error -> {
                val now = clocks.now()
                if (now - lastErrorNotice > 600) {
                    lastErrorNotice = now
                    notifier.error(e.message)
                }
            }
        }
        MonitorService.refreshNotification(this)
    }

    private var lastErrorNotice = 0.0

    private fun tick() {
        alerts.tick()
        main.postDelayed(::tick, 1000)
    }

    fun prune() {
        val days = settings.int("retention_days")
        if (days > 0) ioScope.launch { runCatching { repo.pruneMeasurements(clocks.now() - days * 86400.0) } }
        main.postDelayed({ prune() }, 6 * 3600_000L)
    }

    fun addHistoryListener(l: () -> Unit) { historyListeners += l }
    fun removeHistoryListener(l: () -> Unit) { historyListeners -= l }
    fun notifyHistoryChanged() = historyListeners.forEach { it() }

    // ------------------------------------------------------------ data management
    fun restore(input: InputStream, onDone: (Throwable?) -> Unit) {
        val wasActive = engine.isActive
        ioScope.launch {
            val error = runCatching {
                if (wasActive) stopEngineBlocking()
                db.restoreFrom(input, cacheDir, clocks.now())
                repo.recoverInterruptedSessions()
                repo.logEvent("restored", "Database restored from a backup")
                settings.reload()
            }.exceptionOrNull()
            main.post {
                if (wasActive) requestStart()
                notifyHistoryChanged()
                onDone(error)
            }
        }
    }
}

val Context.netwatch: NetWatchApp get() = applicationContext as NetWatchApp
