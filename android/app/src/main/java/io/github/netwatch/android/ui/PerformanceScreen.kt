package io.github.netwatch.android.ui

import android.view.Gravity
import android.view.View
import android.widget.HorizontalScrollView
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TableLayout
import android.widget.TableRow
import io.github.netwatch.android.R
import io.github.netwatch.android.core.Bucket
import io.github.netwatch.android.core.Performance
import io.github.netwatch.android.core.Target

/** Latency and packet loss over time, with a per-target comparison. */
class PerformanceScreen(a: MainActivity) : Screen(a) {
    override val title = "Performance"
    private val ranges = listOf(Triple("15m", "15 Min", 900.0), Triple("1h", "1 Hour", 3600.0), Triple("24h", "24 Hours", 86400.0),
        Triple("7d", "7 Days", 7 * 86400.0))
    private var range = "1h"
    private var gen = 0
    private val segmented = Segmented(ui, ranges.map { it.first to it.second }, range) { range = it; refresh() }
    private val caption = ui.muted("")
    private val grid = Grid(ui, 3, 10)
    private val tAvg = MetricTile(ui, R.drawable.ic_chart, "Average", small = true)
    private val tMed = MetricTile(ui, R.drawable.ic_chart, "Median", small = true)
    private val tMin = MetricTile(ui, R.drawable.ic_chart, "Minimum", small = true)
    private val tMax = MetricTile(ui, R.drawable.ic_chart, "Maximum", small = true)
    private val tLoss = MetricTile(ui, R.drawable.ic_warning, "Packet loss", small = true)
    private val tChecks = MetricTile(ui, R.drawable.ic_check, "Checks", small = true)
    private val latency = LatencyChartView(a, ui)
    private val latInspect = ui.muted("Drag across the chart to inspect values.", 12f)
    private val legend = FlowLayout(a, ui.dp(14), ui.dp(4))
    private val loss = LossChartView(a, ui)
    private val lossInspect = ui.muted("", 12f)
    private val table = TableLayout(a)

    override val view: View = ScrollView(a).apply {
        val col = ui.vbox().apply { setPadding(ui.dp(16), ui.dp(12), ui.dp(16), ui.dp(28)) }
        col.addView(HorizontalScrollView(a).apply { isHorizontalScrollBarEnabled = false; addView(segmented) })
        col.addView(ui.title("Latency summary"))
        col.addView(caption)
        listOf(tAvg, tMed, tMin, tMax, tLoss, tChecks).forEach { grid.add(ui.card(it, 12)) }
        col.addView(grid, lp(top = ui.dp(10)))
        col.addView(ui.title("Latency over time"))
        col.addView(ui.muted("Average per interval with min–max band. Gaps mean no successful measurement; red ticks mark failed " +
            "checks. Missing data is never drawn as zero.", 12f))
        val c1 = ui.card(padding = 10)
        c1.addView(latency)
        c1.addView(legend, lp(top = ui.dp(6)))
        c1.addView(latInspect, lp(top = ui.dp(4)))
        latency.onInspect = { latInspect.text = it ?: "" }
        col.addView(c1, lp(top = ui.dp(8)))
        col.addView(ui.title("Packet loss over time"))
        col.addView(ui.muted("Share of failed checks across all targets per interval.", 12f))
        val c2 = ui.card(padding = 10)
        c2.addView(loss)
        c2.addView(lossInspect)
        loss.onInspect = { lossInspect.text = it ?: "" }
        col.addView(c2, lp(top = ui.dp(8)))
        col.addView(ui.title("Per-target comparison"))
        col.addView(HorizontalScrollView(a).apply { addView(table) }, lp(top = ui.dp(6)))
        addView(col)
    }

    override fun onWidthChanged(widthDp: Int) = grid.setColumns(if (widthDp >= 600) 6 else 3)
    override fun onShow() = refresh()
    private var ticks = 0
    override fun onTick() { if (++ticks % 15 == 0) refresh() }

    fun refresh() {
        val token = ++gen
        val span = ranges.first { it.first == range }.third
        val now = System.currentTimeMillis() / 1000.0
        a.bg({ app.stats.performance(now - span, now, 120) to app.repo.targetsById() }) { (perf, targets) -> if (token == gen) apply(perf, targets) }
    }

    private fun ms(v: Double?) = v?.let { "%.1f ms".format(it) } ?: "—"

    private fun apply(perf: Performance, targets: Map<Long, Target>) {
        val o = perf.overall
        val none = o.checks == 0
        tAvg.set(ms(o.avg), muted = o.avg == null)
        tMed.set(ms(o.median), muted = o.median == null)
        tMin.set(ms(o.min), muted = o.min == null)
        tMax.set(ms(o.max), o.p95?.let { "p95 ${ms(it)}" } ?: "", o.max == null)
        tLoss.set(o.lossPct?.let { "%.1f%%".format(it) } ?: "—", muted = none)
        tChecks.set(o.checks.toString(), if (o.checks > 0) "${o.checks - o.ok} failed" else "", none)
        caption.text = if (none) "No measurements in this range. Start monitoring to collect data."
        else "Based on ${o.ok} successful replies. Jitter (std. dev.) ${ms(o.jitter)}."
        val ordered = perf.series.entries.sortedBy { targets[it.key]?.position ?: 99 }
        val series = ordered.mapIndexed { i, (tid, pts) -> Series(targets[tid]?.name ?: "target $tid", ui.p.series[i % ui.p.series.size], pts) }
        latency.setData(perf.start, perf.end, perf.bucket, series, app.settings.int("interval_seconds").toDouble())
        legend.removeAllViews()
        series.forEach { s ->
            val row = ui.hbox(6)
            row.addView(View(a).apply { background = ui.rounded(s.color, 99); layoutParams = LinearLayout.LayoutParams(ui.dp(10), ui.dp(10)) })
            row.addView(ui.text(s.name, 12f))
            legend.addView(row)
        }
        val merged = perf.series.values.flatten().groupBy { it.t }.map { (t, b) -> Bucket(t, b.sumOf { it.n }, b.sumOf { it.ok }, null, null, null) }
        loss.setData(perf.start, perf.end, perf.bucket, merged)
        table.removeAllViews()
        val headers = listOf("Target", "Avg", "Median", "Min", "Max", "p95", "Loss", "Checks")
        table.addView(TableRow(a).apply { headers.forEachIndexed { i, h -> addView(cell(h, i, header = true)) } })
        val rows = perf.perTarget.entries.sortedBy { targets[it.key]?.position ?: 99 }
        if (rows.isEmpty()) table.addView(TableRow(a).apply { addView(ui.muted("No data for this range.")) })
        for ((tid, p) in rows) {
            val cells = listOf(targets[tid]?.name ?: "$tid", ms(p.avg), ms(p.median), ms(p.min), ms(p.max), ms(p.p95),
                p.lossPct?.let { "%.1f%%".format(it) } ?: "—", p.checks.toString())
            table.addView(TableRow(a).apply { cells.forEachIndexed { i, c -> addView(cell(c, i)) } })
        }
    }

    private fun cell(text: String, col: Int, header: Boolean = false) =
        (if (header) ui.muted(text, 12f) else ui.text(text, 13f)).apply {
            setPadding(if (col == 0) 0 else ui.dp(14), ui.dp(5), 0, ui.dp(5))
            gravity = if (col == 0) Gravity.START else Gravity.END
            fontFeatureSettings = "tnum"
        }
}
