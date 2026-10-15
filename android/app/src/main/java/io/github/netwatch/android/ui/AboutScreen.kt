package io.github.netwatch.android.ui

import android.content.ClipData
import android.content.ClipboardManager
import android.os.Build
import android.os.PowerManager
import android.view.View
import android.widget.ScrollView
import io.github.netwatch.android.R
import io.github.netwatch.android.core.Schema
import io.github.netwatch.android.platform.IcmpAdapter
import io.github.netwatch.android.platform.MonitorService

/** Versions, integrations, data location and a live connectivity test. */
class AboutScreen(a: MainActivity) : Screen(a) {
    override val title = "About & Diagnostics"
    private val col = ui.vbox().apply { setPadding(ui.dp(16), ui.dp(8), ui.dp(16), ui.dp(28)) }
    private var report = ""
    override val view: View = ScrollView(a).apply { addView(col) }

    override fun onShow() = build()

    private fun build() {
        col.removeAllViews()
        col.addView(ui.text("NetWatch", 24f, bold = true).apply { setPadding(0, ui.dp(12), 0, 0) })
        col.addView(ui.muted("Local-first network connectivity and outage monitor. No account, cloud service or tracking — " +
            "all data stays on this device. Version ${app.versionName()}.", 13f))
        val snap = app.engine.snapshot.value
        val pm = a.getSystemService(PowerManager::class.java)
        val counts = app.repo.counts()
        val local = snap.local ?: runCatching { app.probe.probe() }.getOrNull()
        val groups = listOf(
            "Monitoring" to listOf(
                "State" to "${snap.lifecycle.label} · ${snap.state.label}",
                "Background service" to if (MonitorService.running) "running (foreground)" else "not running",
                "Last classification" to snap.reason.ifBlank { "—" },
                "ICMP ping" to if (IcmpAdapter().available) "system ping available" else "not available — use TCP/HTTP targets",
                "Network" to (local?.let { (it.describe().ifBlank { "none" }) + " · connected=${it.connected} · validated=${it.validated} · restricted=${it.blocked}" } ?: "unknown"),
            ),
            "Device" to listOf(
                "Android" to "${Build.VERSION.RELEASE} (API ${Build.VERSION.SDK_INT})",
                "Device" to "${Build.MANUFACTURER} ${Build.MODEL}",
                "Battery optimisation" to if (pm.isIgnoringBatteryOptimizations(a.packageName)) "exempt" else "optimised (checks may be delayed while asleep)",
                "Notifications" to if (Build.VERSION.SDK_INT < 33 || a.checkSelfPermission(android.Manifest.permission.POST_NOTIFICATIONS) == 0) "allowed" else "not allowed",
            ),
            "Data" to listOf(
                "Database" to app.db.file.path,
                "Size" to "${app.db.file.length() / 1024} KiB",
                "Schema version" to Schema.version(app.db).toString(),
                "Records" to "${counts["monitoring_sessions"]} sessions · ${counts["connection_states"]} states · ${counts["outages"]} outages · ${counts["measurements"]} measurements",
            ),
        )
        val sb = StringBuilder("NetWatch for Android ${app.versionName()}\n")
        for ((title, rows) in groups) {
            col.addView(ui.title(title))
            val card = ui.card(padding = 12)
            sb.append("[$title]\n")
            rows.forEachIndexed { i, (k, v) ->
                if (i > 0) card.addView(ui.divider())
                val r = ui.vbox().apply { setPadding(0, ui.dp(6), 0, ui.dp(6)) }
                r.addView(ui.muted(k, 12f))
                r.addView(ui.text(v, 14f).apply { setTextIsSelectable(true) })
                card.addView(r)
                sb.append("$k: $v\n")
            }
            col.addView(card, lp(top = ui.dp(6)))
        }
        report = sb.toString()
        col.addView(ui.title("Connectivity test"))
        col.addView(ui.muted("Runs one check against every enabled target now, without recording it in the history.", 12f))
        val results = ui.vbox()
        val buttons = ui.hbox(8)
        buttons.addView(ui.button("Run test", R.drawable.ic_play, primary = true) { runTest(results) })
        buttons.addView(ui.button("Copy diagnostics") {
            a.getSystemService(ClipboardManager::class.java).setPrimaryClip(ClipData.newPlainText("NetWatch diagnostics", report))
            a.toast("Diagnostics copied")
        })
        col.addView(buttons, lp(top = ui.dp(8)))
        col.addView(results, lp(top = ui.dp(8)))
        col.addView(ui.muted("Icons: Material Design icons (Apache License 2.0). NetWatch is MIT licensed.", 11f).apply { setPadding(0, ui.dp(24), 0, 0) })
    }

    private fun runTest(out: android.widget.LinearLayout) {
        out.removeAllViews()
        out.addView(ui.muted("Testing…"))
        a.bg({
            val adapters = io.github.netwatch.android.platform.buildAdapters()
            val timeout = app.settings.int("timeout_seconds").toDouble()
            app.probe.probe() to app.repo.listTargets(true).map { t -> t to adapters[t.checkType]?.check(t, timeout) }
        }) { (local, rows) ->
            out.removeAllViews()
            out.addView(ui.text("Local network: ${local.describe().ifBlank { local.detail }} (connected=${local.connected})", 13f))
            for ((t, r) in rows) {
                val ok = r?.success == true
                out.addView(ui.text("${if (ok) "✓" else "✗"} ${t.display}: " + when {
                    r == null -> "no adapter"
                    ok -> r.latencyMs?.let { "%.1f ms".format(it) } ?: "OK"
                    else -> "${r.error.label}: ${r.detail}"
                }, 13f, if (ok) ui.p.online else ui.p.noInternet))
            }
        }
    }
}
