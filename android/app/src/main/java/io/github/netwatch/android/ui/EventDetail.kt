package io.github.netwatch.android.ui

import android.app.AlertDialog
import android.content.ClipData
import android.content.Intent
import android.os.Build
import android.widget.ScrollView
import io.github.netwatch.android.core.ConnState
import io.github.netwatch.android.core.ErrorCategory
import io.github.netwatch.android.core.Fmt
import io.github.netwatch.android.core.OutageInfo
import io.github.netwatch.android.core.Reports
import io.github.netwatch.android.core.Segment
import io.github.netwatch.android.platform.ReportsProvider
import java.io.File

/** Details for one history event: measurements, failure reasons and recovery evidence. */
object EventDetail {
    fun show(a: MainActivity, seg: Segment, outage: OutageInfo?) {
        val ui = a.ui
        val now = System.currentTimeMillis() / 1000.0
        val col = ui.vbox().apply { setPadding(ui.dp(20), ui.dp(8), ui.dp(20), ui.dp(8)) }
        fun row(k: String, v: String) {
            val r = ui.vbox().apply { setPadding(0, ui.dp(6), 0, ui.dp(6)) }
            r.addView(ui.muted(k, 12f))
            r.addView(ui.text(v, 14f).apply { setTextIsSelectable(true) })
            col.addView(r)
        }
        val head = ui.hbox(10)
        head.addView(StateDot(a, ui.p, seg.state))
        head.addView(ui.text(seg.state.label, 18f, bold = true))
        col.addView(head)
        row("Period", "${Fmt.timestamp(seg.start, now, true)} – ${if (seg.ongoing) "now" else Fmt.timestamp(seg.end, now, true)}")
        row("Duration", Fmt.duration(seg.duration, true) + if (seg.ongoing) " (ongoing)" else "")
        if (seg.synthetic) row("Records", "No monitoring records exist for this period; NetWatch was not running.")
        seg.reason?.let { row("Reason", it) }
        seg.targets?.let { row(if (seg.state == ConnState.ONLINE) "Responding targets" else "Failing targets", it) }
        seg.local?.let { l -> l.describe().takeIf { it.isNotBlank() }?.let { row("Network", it + (l.validated?.let { v -> if (v) " · validated by Android" else " · not validated" } ?: "")) } }
        val measurements = ui.vbox()
        col.addView(ui.title("Measurements").apply { textSize = 16f })
        col.addView(measurements)
        if (outage != null && seg.state.isDown) {
            col.addView(ui.title("Outage #${outage.id}").apply { textSize = 16f })
            row("Status", outage.statusLabel)
            row("Start", Fmt.timestamp(outage.start, now, true) + if (outage.startObserved) "" else " (monitoring started during the outage — real start unknown)")
            row("Detected", Fmt.timestamp(outage.detectedAt, now, true))
            if (!outage.ongoing) row("End", Fmt.timestamp(outage.end, now, true))
            outage.recoveryDetectedAt?.let { row("Recovery confirmed", Fmt.timestamp(it, now, true)) }
            row("Total outage duration", Fmt.duration(outage.fullDuration, true))
            outage.targets.forEach { t -> row(t["address"].toString(), "${t["failure_count"]} failed checks, last: ${t["last_error"]}") }
            val ev = outage.evidence
            for (i in maxOf(0, ev.length() - 6) until ev.length()) {
                val item = ev.getJSONObject(i)
                val results = item.optJSONArray("results")
                val summary = if (results != null) (0 until results.length()).joinToString(", ") { j ->
                    val r = results.getJSONObject(j)
                    "${r.getString("target")} " + if (r.getBoolean("ok")) "ok ${r.opt("latency_ms")} ms" else r.optString("error")
                } else item.optString("note")
                val label = when (item.optString("event")) { "detected" -> "Failure detected"; "recovered" -> "Recovery evidence"; "reclassified" -> "State changed"
                    else -> item.optString("event").replace('_', ' ').replaceFirstChar { it.uppercase() } }
                row("$label · ${Fmt.timestamp(item.optDouble("ts", now), now, true)}", summary)
            }
        }
        val dialog = AlertDialog.Builder(a)
            .setView(ScrollView(a).apply { addView(col) })
            .setPositiveButton("Close", null)
            .setNeutralButton("Export…") { _, _ -> ReportExport.choose(a, "custom", null, seg) }
            .create()
        dialog.show()

        if (seg.synthetic || seg.state == ConnState.PAUSED) {
            measurements.addView(ui.muted("None — nothing was measured during this period."))
            return
        }
        measurements.addView(ui.muted("Loading…"))
        a.bg({
            val perf = a.app.stats.performance(seg.start, seg.end + 1, buckets = 60)
            Triple(perf, a.app.repo.errorBreakdown(seg.start, seg.end + 1), a.app.repo.targetsById())
        }) { (perf, errors, targets) ->
            measurements.removeAllViews()
            val o = perf.overall
            if (o.checks == 0) {
                measurements.addView(ui.muted("No raw measurements are stored for this period (they may have been pruned by the retention policy)."))
                return@bg
            }
            measurements.addView(ui.text("${o.checks} checks, ${o.checks - o.ok} failed", 14f))
            o.avg?.let { measurements.addView(ui.muted("avg %.1f ms · median %.1f ms · min %.1f · max %.1f ms".format(it, o.median, o.min, o.max))) }
            val chart = LatencyChartView(a, ui)
            val series = perf.series.entries.sortedBy { it.key }.mapIndexed { i, (tid, pts) ->
                Series(targets[tid]?.name ?: "$tid", ui.p.series[i % ui.p.series.size], pts)
            }
            chart.setData(perf.start, perf.end, perf.bucket, series, a.app.settings.int("interval_seconds").toDouble())
            val inspect = ui.muted("Drag across the chart to inspect values.", 12f)
            chart.onInspect = { inspect.text = it ?: "" }
            measurements.addView(chart, lp(top = ui.dp(8)))
            measurements.addView(inspect)
            for ((tid, p) in perf.perTarget) {
                val t = targets[tid]
                measurements.addView(ui.muted("${t?.display ?: tid}: " + (p.avg?.let { "%.1f ms avg".format(it) } ?: "no replies") +
                    " · ${p.lossPct?.let { "%.0f%% loss".format(it) } ?: ""} · ${p.checks} checks", 12f))
            }
            if (errors.isNotEmpty()) {
                measurements.addView(ui.text("Failure reasons", 14f, bold = true).apply { setPadding(0, ui.dp(10), 0, ui.dp(2)) })
                errors.take(10).forEach { r ->
                    measurements.addView(ui.muted("${targets[(r["target_id"] as Number).toLong()]?.address}: " +
                        "${ErrorCategory.of(r["error_category"]?.toString()).label} × ${r["n"]}", 12f))
                }
            }
        }
    }
}

/** Export a report as HTML/CSV/TXT: save through the system file picker, or share. */
object ReportExport {
    private val formats = Reports.formats.entries.toList()

    fun choose(a: MainActivity, period: String, custom: Pair<Double, Double>?, focus: Segment?) {
        var selected = 0
        AlertDialog.Builder(a)
            .setTitle(if (focus != null) "Export event report" else "Export report")
            .setSingleChoiceItems(formats.map { it.value }.toTypedArray(), 0) { _, w -> selected = w }
            .setNegativeButton("Cancel", null)
            .setNeutralButton("Share…") { _, _ -> share(a, formats[selected].key, period, custom, focus) }
            .setPositiveButton("Save…") { _, _ -> save(a, formats[selected].key, period, custom, focus) }
            .show()
    }

    private fun render(a: MainActivity, fmt: String, period: String, custom: Pair<Double, Double>?, focus: Segment?): String {
        val app = a.app
        val data = Reports.build(app.repo, app.stats, app.settings, period, custom, focus, System.currentTimeMillis() / 1000.0,
            "${Build.MANUFACTURER} ${Build.MODEL}")
        val text = Reports.render(data, fmt)
        return if (fmt == "csv") "﻿" + text else text // BOM so spreadsheet apps detect UTF-8
    }

    fun save(a: MainActivity, fmt: String, period: String, custom: Pair<Double, Double>?, focus: Segment?) {
        val name = Reports.filename(fmt, System.currentTimeMillis() / 1000.0, focus != null)
        // The system picker never silently overwrites: an existing name gets a numbered copy.
        a.createDocument(name, Reports.mimeTypes.getValue(fmt)) { uri ->
            a.bg({
                val content = render(a, fmt, period, custom, focus)
                a.contentResolver.openOutputStream(uri, "wt")!!.use { it.write(content.toByteArray()) }
                a.app.repo.logEvent("report_exported", "Exported ${fmt.uppercase()} report")
            }) { a.toast("Report saved") }
        }
    }

    fun share(a: MainActivity, fmt: String, period: String, custom: Pair<Double, Double>?, focus: Segment?) {
        a.bg({
            val file = File(ReportsProvider.dir(a), Reports.filename(fmt, System.currentTimeMillis() / 1000.0, focus != null))
            ReportsProvider.dir(a).listFiles()?.filter { it != file }?.forEach { it.delete() }
            file.writeText(render(a, fmt, period, custom, focus))
            file
        }) { file ->
            val uri = ReportsProvider.uriFor(file)
            val send = Intent(Intent.ACTION_SEND).setType(Reports.mimeTypes.getValue(fmt))
                .putExtra(Intent.EXTRA_STREAM, uri)
                .putExtra(Intent.EXTRA_SUBJECT, "NetWatch connectivity report")
                .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
            send.clipData = ClipData.newRawUri(file.name, uri)
            a.startActivity(Intent.createChooser(send, "Share report"))
        }
    }
}

