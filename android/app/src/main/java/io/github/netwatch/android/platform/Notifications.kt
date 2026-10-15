package io.github.netwatch.android.platform

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.graphics.Color
import android.os.Build
import io.github.netwatch.android.R
import io.github.netwatch.android.core.ConnState
import io.github.netwatch.android.core.EngineSnapshot
import io.github.netwatch.android.core.Fmt
import io.github.netwatch.android.core.Lifecycle
import io.github.netwatch.android.core.SettingsStore
import io.github.netwatch.android.ui.MainActivity

/** Notification channels, the ongoing monitoring notification and outage/recovery alerts. */
class Notifier(private val context: Context, private val settings: SettingsStore) {
    companion object {
        const val CHANNEL_MONITOR = "monitoring"
        const val CHANNEL_ALERTS = "alerts"
        const val ID_MONITOR = 1
        const val ID_OUTAGE = 2
        const val ID_RECOVERY = 3
        const val ID_ERROR = 4
    }

    private val nm = context.getSystemService(NotificationManager::class.java)

    fun createChannels() {
        nm.createNotificationChannel(NotificationChannel(CHANNEL_MONITOR, "Monitoring status", NotificationManager.IMPORTANCE_LOW).apply {
            description = "Ongoing notification while NetWatch monitors in the background"
            setShowBadge(false)
        })
        nm.createNotificationChannel(NotificationChannel(CHANNEL_ALERTS, "Outages and recovery", NotificationManager.IMPORTANCE_HIGH).apply {
            description = "Outage detected, connection restored and monitoring problems"
            setSound(null, null) // NetWatch plays its own subtle alert sound (configurable, mutable)
            enableVibration(false)
        })
    }

    private fun openApp(page: String? = null): PendingIntent {
        val i = Intent(context, MainActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_SINGLE_TOP)
        page?.let { i.putExtra(MainActivity.EXTRA_PAGE, it) }
        return PendingIntent.getActivity(context, page.hashCode(), i, PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
    }

    private fun serviceAction(action: String, code: Int): PendingIntent {
        val i = Intent(context, MonitorService::class.java).setAction(action)
        return if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) PendingIntent.getForegroundService(context, code, i, PendingIntent.FLAG_IMMUTABLE)
        else PendingIntent.getService(context, code, i, PendingIntent.FLAG_IMMUTABLE)
    }

    fun monitoring(snap: EngineSnapshot, muted: Boolean): Notification {
        val now = System.currentTimeMillis() / 1000.0
        val (title, color) = when {
            snap.lifecycle == Lifecycle.PAUSED -> "Monitoring paused" to Color.GRAY
            snap.lifecycle == Lifecycle.ERROR -> "Monitoring error" to Color.RED
            snap.lifecycle != Lifecycle.RUNNING -> "Starting…" to Color.GRAY
            snap.state == ConnState.ONLINE -> "Online" to Color.rgb(52, 199, 89)
            snap.state == ConnState.NO_INTERNET -> "No Internet" to Color.rgb(255, 159, 10)
            snap.state == ConnState.NOT_CONNECTED -> "Not connected" to Color.GRAY
            else -> "Checking…" to Color.GRAY
        }
        val text = buildString {
            snap.stateSince?.let { if (snap.lifecycle == Lifecycle.RUNNING) append("Since ${Fmt.clock(it)} (${Fmt.duration(now - it)})") }
            snap.lastLatencyMs?.takeIf { snap.state == ConnState.ONLINE }?.let { append(" · ${"%.0f".format(it)} ms") }
            if (muted) append(" · alerts muted")
        }.ifBlank { "NetWatch is monitoring your connection" }
        val paused = snap.lifecycle == Lifecycle.PAUSED
        return Notification.Builder(context, CHANNEL_MONITOR)
            .setSmallIcon(if (snap.state.isDown) R.drawable.ic_stat_offline else R.drawable.ic_stat_online)
            .setContentTitle("NetWatch · $title")
            .setContentText(text)
            .setColor(color)
            .setOngoing(true)
            .setOnlyAlertOnce(true)
            .setShowWhen(false)
            .setContentIntent(openApp())
            .addAction(Notification.Action.Builder(null, if (paused) "Resume" else "Pause",
                serviceAction(if (paused) MonitorService.ACTION_RESUME else MonitorService.ACTION_PAUSE, 11)).build())
            .addAction(Notification.Action.Builder(null, if (muted) "Unmute" else "Mute", serviceAction(MonitorService.ACTION_TOGGLE_MUTE, 12)).build())
            .addAction(Notification.Action.Builder(null, "Stop", serviceAction(MonitorService.ACTION_STOP, 13)).build())
            .apply { if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) setForegroundServiceBehavior(Notification.FOREGROUND_SERVICE_IMMEDIATE) }
            .build()
    }

    private fun alert(id: Int, kind: String, title: String, text: String, page: String?) {
        val key = when (kind) { "outage" -> "notify_outage"; "recovery" -> "notify_recovery"; else -> "notify_errors" }
        if (!settings.bool(key)) return
        if (Build.VERSION.SDK_INT >= 33 && context.checkSelfPermission(android.Manifest.permission.POST_NOTIFICATIONS) !=
            android.content.pm.PackageManager.PERMISSION_GRANTED) return
        val n = Notification.Builder(context, CHANNEL_ALERTS)
            .setSmallIcon(if (kind == "outage") R.drawable.ic_stat_offline else R.drawable.ic_stat_online)
            .setContentTitle(title)
            .setContentText(text)
            .setStyle(Notification.BigTextStyle().bigText(text))
            .setAutoCancel(true)
            .setCategory(if (kind == "error") Notification.CATEGORY_ERROR else Notification.CATEGORY_STATUS)
            .setContentIntent(openApp(page))
            .build()
        nm.notify(id, n)
    }

    fun outage(state: ConnState) {
        nm.cancel(ID_RECOVERY)
        if (state == ConnState.NO_INTERNET) alert(ID_OUTAGE, "outage", "Internet connection lost",
            "Your network is connected but no internet target responds.", "history")
        else alert(ID_OUTAGE, "outage", "Network disconnected", "There is no active network connection.", "history")
    }

    fun recovery(duration: Double?) {
        nm.cancel(ID_OUTAGE)
        alert(ID_RECOVERY, "recovery", "Connection restored",
            if (duration != null) "Back online after ${Fmt.duration(duration, true)}." else "Back online.", "history")
    }

    fun error(message: String) = alert(ID_ERROR, "error", "NetWatch monitoring problem", message, "about")

    fun update(n: Notification) = nm.notify(ID_MONITOR, n)
}
