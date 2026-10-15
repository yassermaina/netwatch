package io.github.netwatch.android.ui

import android.content.Intent
import android.content.res.ColorStateList
import android.os.PowerManager
import android.provider.Settings
import android.view.View
import android.widget.HorizontalScrollView
import android.widget.ImageView
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import io.github.netwatch.android.R
import io.github.netwatch.android.core.ConnState
import io.github.netwatch.android.core.EngineSnapshot
import io.github.netwatch.android.core.Fmt
import io.github.netwatch.android.core.Lifecycle
import io.github.netwatch.android.core.PeriodSummary
import io.github.netwatch.android.core.ReliabilityService
import io.github.netwatch.android.core.Segment

/** Live status, controls and today's numbers. */
class OverviewScreen(a: MainActivity) : Screen(a) {
    override val title = "Overview"
    private var snap = EngineSnapshot()
    private var gen = 0
    private var lastSuccessDb: Double? = null

    private val statusIcon = ImageView(a)
    private val statusTitle = ui.text("Not monitoring", 26f, bold = true, lines = 1)
    private val lifecyclePill = ui.pill("Stopped", ui.p.muted)
    private val outagePill = ui.pill("", ui.p.noInternet)
    private val statusSub = ui.muted("", 13f)
    private val statusIface = ui.muted("", 12f)
    private val statusCard = ui.vbox()
    private val controls = ui.hbox(8)
    private val grid = Grid(ui, 2)
    private val mLatency = MetricTile(ui, R.drawable.ic_wifi, "Latency", small = true)
    private val mLoss = MetricTile(ui, R.drawable.ic_warning, "Packet loss (5 min)", small = true)
    private val mLast = MetricTile(ui, R.drawable.ic_check, "Last successful check", small = true)
    private val mSession = MetricTile(ui, R.drawable.ic_history, "Monitoring for", small = true)
    private val mOutages = MetricTile(ui, R.drawable.ic_close, "Outages today", small = true)
    private val mDown = MetricTile(ui, R.drawable.ic_warning, "Downtime today", ui.p.noInternet, small = true)
    private val live = TimelineView(a, ui, barDp = 30)
    private val liveCaption = ui.muted("")
    private val targets = ui.vbox()
    private val batteryHint = ui.card()
    private val targetViews = mutableMapOf<Long, Pair<StateDot, TextView>>()

    override val view: View = ScrollView(a).apply {
        val col = ui.vbox().apply { setPadding(ui.dp(16), ui.dp(12), ui.dp(16), ui.dp(28)) }
        statusCard.background = ui.rounded(withAlpha(ui.p.fg, 0x0A), 18)
        statusCard.setPadding(ui.dp(18), ui.dp(18), ui.dp(18), ui.dp(18))
        val head = ui.hbox(14)
        statusIcon.layoutParams = LinearLayout.LayoutParams(ui.dp(44), ui.dp(44))
        head.addView(statusIcon)
        val titles = ui.vbox()
        titles.addView(statusTitle)
        val pills = ui.hbox(6)
        pills.addView(lifecyclePill)
        pills.addView(outagePill)
        titles.addView(pills, lp(top = ui.dp(4)))
        head.addView(titles, lp(0, weight = 1f))
        statusCard.addView(head)
        statusCard.addView(statusSub, lp(top = ui.dp(10)))
        statusCard.addView(statusIface, lp(top = ui.dp(2)))
        col.addView(statusCard)
        col.addView(HorizontalScrollView(a).apply { isHorizontalScrollBarEnabled = false; addView(controls) }, lp(top = ui.dp(12)))
        batteryHint.addView(ui.text("Allow background activity", 15f, bold = true))
        batteryHint.addView(ui.muted("Android may pause NetWatch to save battery. Exempting it from battery optimisation keeps " +
            "checks on schedule; paused periods are recorded as unmonitored, never as online.").apply { setPadding(0, ui.dp(4), 0, ui.dp(8)) })
        batteryHint.addView(ui.button("Battery settings") {
            runCatching { a.startActivity(Intent(Settings.ACTION_IGNORE_BATTERY_OPTIMIZATION_SETTINGS)) }
        })
        col.addView(batteryHint, lp(top = ui.dp(12)))
        col.addView(ui.title("Now"))
        listOf(mLatency, mLoss, mLast, mSession, mOutages, mDown).forEach { grid.add(ui.card(it)) }
        col.addView(grid, lp(top = ui.dp(6)))
        col.addView(ui.title("Last hour"))
        col.addView(liveCaption)
        live.onSelect = { seg -> a.navigate("history"); (a.screenHistory())?.selectSegment(seg) }
        live.onInspect = { s -> liveCaption.text = s ?: "" }
        col.addView(live, lp(top = ui.dp(8)))
        col.addView(ui.title("Monitoring targets"))
        col.addView(ui.muted("Connectivity is Online while at least one enabled target responds."))
        targets.background = ui.rounded(ui.p.surface, 14, if (ui.p.dark) 0x0DFFFFFF else 0x14000000)
        col.addView(targets, lp(top = ui.dp(8)))
        col.addView(ui.button("Edit targets", R.drawable.ic_edit) { a.navigate("settings") }.apply {
            layoutParams = lp(LinearLayout.LayoutParams.WRAP_CONTENT, top = ui.dp(10))
        })
        addView(col)
    }

    override fun onWidthChanged(widthDp: Int) = grid.setColumns(if (widthDp >= 600) 3 else 2)

    override fun onShow() {
        reloadTargets()
        refreshStats()
        val pm = a.getSystemService(PowerManager::class.java)
        batteryHint.visibility = if (pm.isIgnoringBatteryOptimizations(a.packageName)) View.GONE else View.VISIBLE
    }

    override fun onHistoryChanged() { if (visible) refreshStats() }

    override fun onTick() = updateStatus()

    private fun reloadTargets() {
        targets.removeAllViews()
        targetViews.clear()
        val list = app.repo.listTargets()
        list.forEachIndexed { i, t ->
            if (i > 0) targets.addView(ui.divider())
            val row = ui.hbox(12).apply { setPadding(ui.dp(14), ui.dp(10), ui.dp(14), ui.dp(10)) }
            val dot = StateDot(a, ui.p, ConnState.UNKNOWN)
            row.addView(dot)
            val texts = ui.vbox()
            texts.addView(ui.text(t.name, lines = 1))
            texts.addView(ui.muted("${t.address} · ${t.checkType.uppercase()}" + if (t.enabled) "" else " · disabled", 12f, 1))
            row.addView(texts, lp(0, weight = 1f))
            val value = ui.muted("—", 13f, 1)
            row.addView(value)
            targets.addView(row)
            t.id?.let { targetViews[it] = dot to value }
        }
    }

    override fun onSnapshot(s: EngineSnapshot) {
        val prevChecks = snap.lastCheckAt
        snap = s
        for (r in s.lastResults) {
            val (dot, value) = targetViews[r.targetId ?: continue] ?: continue
            dot.state = if (r.success) ConnState.ONLINE else ConnState.NO_INTERNET
            value.text = if (r.success) r.latencyMs?.let { "%.1f ms".format(it) } ?: "OK" else r.error
        }
        updateControls()
        updateStatus()
        if (visible && s.lastCheckAt != prevChecks) refreshStats()
    }

    private fun updateControls() {
        controls.removeAllViews()
        val lc = snap.lifecycle
        val running = lc == Lifecycle.RUNNING || lc == Lifecycle.PAUSED || lc == Lifecycle.STARTING
        if (!running) controls.addView(ui.button("Start monitoring", R.drawable.ic_play, primary = true) { app.requestStart(a) })
        else {
            if (lc == Lifecycle.PAUSED) controls.addView(ui.button("Resume", R.drawable.ic_play, primary = true) { app.engine.resume() })
            else controls.addView(ui.button("Pause", R.drawable.ic_pause) { app.engine.pause() })
            controls.addView(ui.button("Stop", R.drawable.ic_stop) {
                a.confirm("Stop monitoring?", "The monitoring session is closed and history is kept. Nothing is recorded until you start again.",
                    "Stop", destructive = false) { app.requestStop(a) }
            })
        }
        val muted = app.settings.bool("muted")
        controls.addView(ui.button(if (muted) "Unmute" else "Mute", if (muted) R.drawable.ic_volume_off else R.drawable.ic_volume) {
            app.setMuted(!app.settings.bool("muted")); updateControls()
        })
        controls.addView(ui.button("History", R.drawable.ic_history) { a.navigate("history") })
        controls.addView(ui.button("Reports", R.drawable.ic_report) { a.navigate("reports") })
    }

    private fun ago(ts: Double?, now: Double): String = when {
        ts == null -> "never"
        now - ts < 2 -> "just now"
        else -> "${Fmt.duration(now - ts)} ago"
    }

    private fun updateStatus() {
        val now = System.currentTimeMillis() / 1000.0
        val lc = snap.lifecycle
        val state = when (lc) {
            Lifecycle.PAUSED -> ConnState.PAUSED
            Lifecycle.RUNNING -> snap.state
            else -> ConnState.NOT_RUNNING
        }
        val color = when (state) {
            ConnState.ONLINE -> ui.p.online
            ConnState.NO_INTERNET -> ui.p.noInternet
            ConnState.NOT_CONNECTED -> ui.p.notConnected
            else -> ui.p.muted
        }
        statusCard.background = ui.rounded(if (state.isMonitored) withAlpha(color, 0x24) else withAlpha(ui.p.fg, 0x0A), 18)
        statusIcon.setImageResource(when (state) {
            ConnState.ONLINE -> R.drawable.ic_wifi
            ConnState.NO_INTERNET, ConnState.NOT_CONNECTED -> R.drawable.ic_wifi_off
            ConnState.PAUSED -> R.drawable.ic_pause
            ConnState.NOT_RUNNING -> R.drawable.ic_stop
            else -> R.drawable.ic_info
        })
        statusIcon.imageTintList = ColorStateList.valueOf(if (state.isMonitored) color else ui.p.fg)
        val title = when {
            lc == Lifecycle.ERROR -> "Monitoring error"
            state == ConnState.UNKNOWN -> "Checking…"
            state == ConnState.PAUSED -> "Monitoring paused"
            state == ConnState.NOT_RUNNING -> "Not monitoring"
            state == ConnState.NO_INTERNET -> "No Internet"
            else -> state.label
        }
        statusTitle.text = title
        statusTitle.setTextColor(if (state.isMonitored) color else ui.p.fg)
        lifecyclePill.text = lc.label
        val pillColor = when (lc) { Lifecycle.RUNNING -> ui.p.online; Lifecycle.PAUSED -> ui.p.noInternet; Lifecycle.ERROR -> ui.p.error; else -> ui.p.muted }
        lifecyclePill.setTextColor(pillColor)
        lifecyclePill.background = ui.rounded(withAlpha(pillColor, 0x2E), 99)
        outagePill.visibility = if (snap.outageId != null) View.VISIBLE else View.GONE
        outagePill.text = "Outage #${snap.outageId}"
        statusSub.text = when (lc) {
            Lifecycle.ERROR -> snap.message
            Lifecycle.STOPPED -> "Tap Start to begin monitoring. Nothing is recorded while stopped."
            Lifecycle.PAUSED -> "Paused since ${snap.stateSince?.let { Fmt.clock(it) } ?: "—"}. This period is recorded as unmonitored."
            else -> listOfNotNull(
                snap.stateSince?.takeIf { state != ConnState.UNKNOWN }?.let { "Since ${Fmt.clock(it)} (${Fmt.duration(now - it)})" },
                snap.reason.takeIf { state == ConnState.UNKNOWN && it.isNotBlank() },
                "last check ${ago(snap.lastCheckAt, now)}",
                snap.pendingState?.takeIf { it != snap.state }?.let { "verifying possible change to ${it.shortLabel}" },
            ).joinToString(" · ")
        }
        val local = snap.local
        statusIface.visibility = if (local != null && lc == Lifecycle.RUNNING) View.VISIBLE else View.GONE
        if (local != null) {
            val desc = local.describe().ifBlank { if (local.connected == false) "No active connection" else "Network status unknown" }
            statusIface.text = desc + (if (local.validated == false) " · Android has not validated this network" else "") +
                (if (local.blocked) " · system restricts background network access" else "")
        }
        statusCard.contentDescription = "Connection status: $title. ${statusSub.text}"
        val last = snap.lastLatencyMs
        if (last != null) mLatency.set("%.1f ms".format(last), if (snap.lastSuccessAt != null && now - snap.lastSuccessAt!! < 120) "best responding target" else "most recent reply")
        else mLatency.set("—", "no replies yet", muted = true)
        val lastOk = snap.lastSuccessAt ?: lastSuccessDb
        mLast.set(ago(lastOk, now), lastOk?.let { Fmt.clock(it, true) } ?: "", muted = lastOk == null)
        if (snap.sessionStarted != null && (lc == Lifecycle.RUNNING || lc == Lifecycle.PAUSED)) {
            mSession.set(Fmt.durationLong(now - snap.sessionStarted!!), "since ${Fmt.clock(snap.sessionStarted!!)}")
        } else mSession.set("—", "not monitoring", muted = true)
    }

    private fun refreshStats() {
        val token = ++gen
        val now = System.currentTimeMillis() / 1000.0
        a.bg({
            val st = app.stats
            Stats(st.summary(Fmt.localDayStart(now), now, now), st.segments(now - 3600, now, now), st.recentQuality(now), app.repo.lastSuccess())
        }) { r ->
            if (token != gen) return@bg
            lastSuccessDb = r.lastOk
            val t = r.today
            mOutages.set(t.outageCount.toString(), if (t.monitored > 0) "monitored ${Fmt.durationLong(t.monitored)} today" else "no monitored time today", t.monitored <= 0)
            if (t.monitored > 0) mDown.set(Fmt.duration(t.downtime, true), "%.2f%% of monitored time".format(t.downtimePct ?: 0.0))
            else mDown.set("—", "no monitored time today", true)
            val q = r.quality
            if (q.lossPct == null) mLoss.set("—", "no checks in the last 5 minutes", true) else mLoss.set("%.1f%%".format(q.lossPct), "${q.checks} checks")
            live.setData(r.hour, now - 3600, now, now)
            val online = r.hour.filter { it.state == ConnState.ONLINE }.sumOf { it.duration }
            val mon = r.hour.filter { it.state.isMonitored }.sumOf { it.duration }
            liveCaption.text = if (mon > 0) "Online %.1f%% of %s monitored in the last hour".format(online / mon * 100, Fmt.durationLong(mon))
            else "Nothing monitored in the last hour"
            updateStatus()
        }
    }

    private class Stats(val today: PeriodSummary, val hour: List<Segment>, val quality: ReliabilityService.Quality, val lastOk: Double?)
}
