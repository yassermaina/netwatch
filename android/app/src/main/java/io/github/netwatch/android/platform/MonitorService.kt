package io.github.netwatch.android.platform

import android.app.Service
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.IBinder
import io.github.netwatch.android.core.Lifecycle
import kotlinx.coroutines.flow.collectLatest
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.launch

/**
 * Foreground service that keeps monitoring running while the app is in the background.
 * Type "specialUse" (network monitoring); restarted by the system after it kills the process (START_STICKY),
 * in which case the interrupted session is detected and the gap is recorded as unmonitored.
 */
class MonitorService : Service() {
    companion object {
        const val ACTION_START = "io.github.netwatch.android.START"
        const val ACTION_STOP = "io.github.netwatch.android.STOP"
        const val ACTION_PAUSE = "io.github.netwatch.android.PAUSE"
        const val ACTION_RESUME = "io.github.netwatch.android.RESUME"
        const val ACTION_TOGGLE_MUTE = "io.github.netwatch.android.TOGGLE_MUTE"

        @Volatile var running = false; private set

        fun refreshNotification(context: Context) {
            if (!running) return
            val app = context.netwatch
            app.notifier.update(app.notifier.monitoring(app.engine.snapshot.value, app.settings.bool("muted")))
        }
    }

    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main)
    private var watch: Job? = null

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        goForeground()
        running = true
        val app = netwatch
        watch = scope.launch {
            app.engine.snapshot.collectLatest { snap ->
                app.notifier.update(app.notifier.monitoring(snap, app.settings.bool("muted")))
            }
        }
    }

    private fun goForeground() {
        val app = netwatch
        val n = app.notifier.monitoring(app.engine.snapshot.value, app.settings.bool("muted"))
        if (Build.VERSION.SDK_INT >= 34) {
            startForeground(Notifier.ID_MONITOR, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE)
        } else {
            startForeground(Notifier.ID_MONITOR, n)
        }
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        val app = netwatch
        when (intent?.action ?: ACTION_START) { // null intent = restarted by the system after a kill
            ACTION_START -> app.startEngine()
            ACTION_PAUSE -> app.engine.pause()
            ACTION_RESUME -> if (app.engine.lifecycle == Lifecycle.PAUSED) app.engine.resume() else app.startEngine()
            ACTION_TOGGLE_MUTE -> app.setMuted(!app.settings.bool("muted"))
            ACTION_STOP -> {
                app.ioScope.launch {
                    app.stopEngineBlocking()
                    app.main.post {
                        stopForeground(STOP_FOREGROUND_REMOVE)
                        stopSelf()
                    }
                }
            }
        }
        return START_STICKY
    }

    override fun onDestroy() {
        running = false
        watch?.cancel()
        scope.cancel()
        val app = netwatch
        if (app.engine.isActive) app.stopEngineBlocking() // e.g. system stopped the service
        super.onDestroy()
    }
}

/** Starts monitoring after boot when the user enabled "Start on boot". */
class BootReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action != Intent.ACTION_BOOT_COMPLETED && intent.action != "android.intent.action.MY_PACKAGE_REPLACED") return
        val app = context.netwatch
        if (app.settings.bool("start_on_boot")) app.requestStart(context)
    }
}
