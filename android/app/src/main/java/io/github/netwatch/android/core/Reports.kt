package io.github.netwatch.android.core

import java.util.Locale

/**
 * TXT / CSV / HTML reports (port of netwatch/reports.py). Reports only state what the stored records show:
 * outages whose start or recovery was not observed are labelled, and coverage gaps are listed.
 */
object Reports {
    val formats = linkedMapOf("html" to "HTML (offline web page)", "csv" to "CSV (spreadsheet)", "txt" to "Plain text")
    val mimeTypes = mapOf("html" to "text/html", "csv" to "text/csv", "txt" to "text/plain")

    private val colors = mapOf(
        ConnState.ONLINE to "#34c759", ConnState.NO_INTERNET to "#ff9f0a", ConnState.NOT_CONNECTED to "#8e8e93",
        ConnState.UNKNOWN to "#5e5ce6", ConnState.PAUSED to "#c7c7cc", ConnState.NOT_RUNNING to "#d1d1d6",
    )

    class ReportData(
        val title: String,
        val generatedAt: Double,
        val history: HistoryData,
        val settings: Map<String, Any>,
        val targets: Map<Long, Target>,
        val interruptions: List<Segment>,
        val limitations: List<String>,
        val device: String,
        val focus: Segment?,
    ) {
        val summary get() = history.summary
    }

    fun build(repo: Repository, stats: ReliabilityService, settings: SettingsStore, period: String = "all",
              custom: Pair<Double, Double>? = null, focus: Segment? = null, now: Double, device: String): ReportData {
        val range = if (focus != null) focus.start to focus.end else custom
        val history = stats.history(period, now, range)
        val s = history.summary
        val titles = mapOf("24h" to "Last 24 hours", "7d" to "Last 7 days", "30d" to "Last 30 days", "all" to "All retained history")
        val title = when {
            focus != null -> "Event report"
            custom != null -> "Custom range"
            else -> titles[period] ?: "Custom range"
        }
        val cfg = settings.all()
        val lim = mutableListOf<String>()
        s.coveragePct?.let {
            if (it < 99.9) lim += "Monitoring covered ${Fmt.percent(it, 1)} of the requested range (${Fmt.durationLong(s.monitored)} of " +
                "${Fmt.durationLong(s.rangeDuration)}). Uptime and downtime are calculated over monitored time only; nothing is " +
                "assumed about unmonitored periods."
        }
        if (s.unknown > 0) lim += "${Fmt.durationLong(s.unknown)} were recorded as Unknown (monitoring ran but had insufficient evidence, " +
            "e.g. right after start, or while the system restricted network access to save power)."
        if (s.outages.any { !it.startObserved }) lim += "Some outages were already in progress when monitoring started; their real start is unknown."
        if (s.outages.any { !it.endObserved && !it.ongoing }) lim += "Some outages ended because monitoring stopped, paused or was interrupted; recovery was not observed."
        if (s.outages.any { it.ongoing }) lim += "An outage is still ongoing at report time."
        lim += "Times are detection times. Checks ran every ${cfg["interval_seconds"]}s with a ${cfg["timeout_seconds"]}s timeout; a failure " +
            "was confirmed after ${cfg["failure_threshold"]} consecutive failed round(s) and recovery after ${cfg["recovery_threshold"]} " +
            "successful round(s), so boundaries are accurate to about one interval."
        lim += "On Android the system may delay or suspend background work (Doze, battery optimisation); such gaps are recorded as unmonitored."
        val retention = (cfg["retention_days"] as? Number)?.toInt() ?: 0
        if (retention > 0) lim += "Raw measurements older than $retention days are pruned; state and outage history is kept."
        return ReportData(title, now, history, cfg, repo.targetsById(),
            history.segments.filter { it.state == ConnState.NOT_RUNNING || it.state == ConnState.PAUSED || it.state == ConnState.UNKNOWN },
            lim, device, focus)
    }

    private fun segDetails(seg: Segment): String {
        val bits = mutableListOf<String>()
        seg.targets?.let { bits += (if (seg.state == ConnState.ONLINE) "responding: " else "failing: ") + it }
        seg.local?.describe()?.takeIf { it.isNotBlank() }?.let { bits += it }
        if (seg.reason != null && seg.state != ConnState.ONLINE) bits += seg.reason
        return bits.joinToString("; ")
    }

    private fun outageTargets(o: OutageInfo) = o.targets.joinToString(", ") {
        "${it.s("address")} (${it.l("failure_count")} failed, last: ${it.s("last_error")})"
    }

    fun render(r: ReportData, fmt: String): String = when (fmt) {
        "txt" -> txt(r)
        "csv" -> csv(r)
        else -> html(r)
    }

    fun filename(fmt: String, now: Double, event: Boolean = false): String {
        val stamp = Fmt.localDateTime(now).let { String.format(Locale.US, "%04d-%02d-%02d_%02d%02d", it.year, it.monthValue, it.dayOfMonth, it.hour, it.minute) }
        return "netwatch-${if (event) "event" else "report"}-$stamp.$fmt"
    }

    // ------------------------------------------------------------ TXT
    fun txt(r: ReportData): String = buildString {
        val s = r.summary
        appendLine("NetWatch connectivity report — ${r.title}")
        appendLine("=".repeat(60))
        appendLine("Generated:        ${Fmt.reportTime(r.generatedAt)} (${Fmt.zone.id}) on ${r.device}")
        appendLine("Requested range:  ${Fmt.reportTime(s.rangeStart)} → ${Fmt.reportTime(s.rangeEnd)} (${Fmt.durationLong(s.rangeDuration)})")
        appendLine("Monitored:        ${Fmt.durationLong(s.monitored)} (${Fmt.percent(s.coveragePct, 1)} coverage)")
        appendLine("Uptime:           ${Fmt.percent(s.uptimePct)} of monitored time")
        appendLine("Total downtime:   ${Fmt.duration(s.downtime, true)} (${Fmt.percent(s.downtimePct)} of monitored time)")
        appendLine("Outages:          ${s.outageCount}")
        s.longestOutage?.let { appendLine("Longest outage:   ${Fmt.duration(it.durationInRange, true)} (${it.statusLabel}, started ${Fmt.reportTime(it.start)})") }
        appendLine("Not monitored:    app not running ${Fmt.durationLong(s.notRunning)}, paused ${Fmt.durationLong(s.paused)}, unknown ${Fmt.durationLong(s.unknown)}")
        appendLine("Targets:          ${r.targets.values.filter { it.enabled }.joinToString(", ") { it.address }.ifBlank { "none" }}")
        appendLine()
        appendLine("Outages\n-------")
        if (s.outages.isEmpty()) appendLine("No outages recorded in this range.")
        for (o in s.outages) {
            val start = Fmt.reportTime(o.start) + if (o.startObserved) "" else " (start not observed)"
            val end = if (o.ongoing) "ongoing" else Fmt.reportTime(o.end)
            appendLine("#${o.id}  ${o.classification.shortLabel.padEnd(14)} $start → $end  ${Fmt.duration(o.fullDuration, true)}  [${o.statusLabel}]")
            if (o.targets.isNotEmpty()) appendLine("      targets: ${outageTargets(o)}")
        }
        appendLine("\nMonitoring interruptions\n------------------------")
        if (r.interruptions.isEmpty()) appendLine("None.")
        r.interruptions.forEach {
            appendLine("${it.state.shortLabel.padEnd(16)} ${Fmt.reportTime(it.start)} → ${if (it.ongoing) "now" else Fmt.reportTime(it.end)}  ${Fmt.duration(it.duration)}")
        }
        appendLine("\nEvent timeline\n--------------")
        r.history.segments.forEach {
            val d = segDetails(it)
            appendLine("${Fmt.reportTime(it.start)} → ${if (it.ongoing) "now" else Fmt.reportTime(it.end)}  ${it.state.shortLabel.padEnd(16)} " +
                Fmt.duration(it.duration).padStart(8) + if (d.isNotBlank()) "  $d" else "")
        }
        appendLine("\nLimitations\n-----------")
        r.limitations.forEach { appendLine("* $it") }
        appendLine("\nGenerated by NetWatch for Android. All times local; durations computed from stored records.")
    }

    // ------------------------------------------------------------ CSV
    val csvColumns = listOf("record_type", "state", "start_utc", "end_utc", "start_local", "end_local", "duration_seconds", "duration",
        "ongoing", "outage_id", "start_observed", "end_observed", "targets", "interface", "details")

    private fun csvLine(cells: List<Any?>): String = cells.joinToString(",") { c ->
        val v = c?.toString() ?: ""
        if (v.any { it == ',' || it == '"' || it == '\n' || it == '\r' }) "\"" + v.replace("\"", "\"\"") + "\"" else v
    } + "\r\n"

    fun csv(r: ReportData): String = buildString {
        val s = r.summary
        append(csvLine(csvColumns))
        fun summary(metric: String, value: String) = append(csvLine(listOf("summary", metric) + List(12) { "" } + value))
        summary("report", r.title)
        summary("generated_local", Fmt.isoLocal(r.generatedAt))
        summary("range_start_local", Fmt.isoLocal(s.rangeStart))
        summary("range_end_local", Fmt.isoLocal(s.rangeEnd))
        summary("monitored_seconds", "%.0f".format(Locale.US, s.monitored))
        summary("coverage_percent", s.coveragePct?.let { "%.3f".format(Locale.US, it) } ?: "")
        summary("uptime_percent", s.uptimePct?.let { "%.4f".format(Locale.US, it) } ?: "")
        summary("downtime_seconds", "%.0f".format(Locale.US, s.downtime))
        summary("outage_count", s.outageCount.toString())
        summary("longest_outage_seconds", "%.0f".format(Locale.US, s.longestOutage?.durationInRange ?: 0.0))
        for (seg in r.history.segments) {
            append(csvLine(listOf(
                if (seg.synthetic) "unmonitored" else if (seg.state == ConnState.PAUSED) "paused" else "state",
                seg.state.shortLabel, Fmt.isoUtc(seg.start), if (seg.ongoing) "" else Fmt.isoUtc(seg.end),
                Fmt.isoLocal(seg.start), if (seg.ongoing) "" else Fmt.isoLocal(seg.end), "%.0f".format(Locale.US, seg.duration),
                Fmt.duration(seg.duration, true), if (seg.ongoing) "yes" else "no", seg.outageId ?: "", "", "", seg.targets ?: "",
                seg.local?.describe() ?: "", seg.reason ?: "",
            )))
        }
        for (o in s.outages) {
            append(csvLine(listOf(
                "outage", o.classification.shortLabel, Fmt.isoUtc(o.start), if (o.ongoing) "" else Fmt.isoUtc(o.end),
                Fmt.isoLocal(o.start), if (o.ongoing) "" else Fmt.isoLocal(o.end), "%.0f".format(Locale.US, o.fullDuration),
                Fmt.duration(o.fullDuration, true), if (o.ongoing) "yes" else "no", o.id, if (o.startObserved) "yes" else "no",
                if (o.endObserved) "yes" else "no", outageTargets(o), "", o.statusLabel,
            )))
        }
    }

    // ------------------------------------------------------------ HTML
    private fun esc(v: String) = v.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\"", "&quot;")

    private fun timelineSvg(r: ReportData, width: Int = 960, height: Int = 36): String {
        val span = maxOf(1.0, r.summary.rangeEnd - r.summary.rangeStart)
        val rects = r.history.segments.joinToString("") { seg ->
            val x = (seg.start - r.summary.rangeStart) / span * width
            val w = maxOf(0.75, seg.duration / span * width)
            val fill = if (seg.state == ConnState.NOT_RUNNING || seg.state == ConnState.PAUSED) "url(#hatch)" else colors.getValue(seg.state)
            val title = esc("${seg.state.label}: ${Fmt.isoLocal(seg.start)} – ${if (seg.ongoing) "now" else Fmt.isoLocal(seg.end)} (${Fmt.duration(seg.duration)})")
            "<rect x=\"${"%.2f".format(Locale.US, x)}\" y=\"0\" width=\"${"%.2f".format(Locale.US, w)}\" height=\"$height\" fill=\"$fill\"><title>$title</title></rect>"
        }
        return "<svg viewBox=\"0 0 $width $height\" class=\"timeline\" role=\"img\" aria-label=\"Event timeline\" preserveAspectRatio=\"none\">" +
            "<defs><pattern id=\"hatch\" width=\"6\" height=\"6\" patternUnits=\"userSpaceOnUse\" patternTransform=\"rotate(45)\">" +
            "<rect width=\"6\" height=\"6\" fill=\"#e5e5ea\"/><line x1=\"0\" y1=\"0\" x2=\"0\" y2=\"6\" stroke=\"#b0b0b8\" stroke-width=\"2\"/></pattern></defs>$rects</svg>"
    }

    fun html(r: ReportData): String {
        val s = r.summary
        val lo = s.longestOutage
        fun card(label: String, value: String, cls: String = "") =
            "<div class=\"card\"><div class=\"label\">${esc(label)}</div><div class=\"value $cls\">${esc(value)}</div></div>"
        val outageRows = s.outages.joinToString("") { o ->
            "<tr><td>#${o.id}</td><td><span class='dot' style='background:${colors[o.classification]}'></span>${esc(o.classification.shortLabel)}</td>" +
                "<td>${esc(Fmt.isoLocal(o.start))}${if (o.startObserved) "" else " <em>(start not observed)</em>"}</td>" +
                "<td>${if (o.ongoing) "ongoing" else esc(Fmt.isoLocal(o.end))}</td><td class='num'>${esc(Fmt.duration(o.fullDuration, true))}</td>" +
                "<td>${esc(o.statusLabel)}</td><td>${esc(outageTargets(o))}</td></tr>"
        }.ifBlank { "<tr><td colspan='7' class='empty'>No outages recorded in this range.</td></tr>" }
        val segRows = r.history.segments.asReversed().joinToString("") { seg ->
            "<tr><td><span class='dot' style='background:${colors[seg.state]}'></span>${esc(seg.state.label)}</td>" +
                "<td>${esc(Fmt.isoLocal(seg.start))}</td><td>${if (seg.ongoing) "now" else esc(Fmt.isoLocal(seg.end))}</td>" +
                "<td class='num'>${esc(Fmt.duration(seg.duration, true))}</td><td>${esc(segDetails(seg))}</td></tr>"
        }
        val legend = listOf(ConnState.ONLINE, ConnState.NO_INTERNET, ConnState.NOT_CONNECTED, ConnState.UNKNOWN, ConnState.NOT_RUNNING)
            .joinToString("") { "<span><i style='background:${colors[it]}'></i>${esc(it.label)}</span>" }
        val limitations = r.limitations.joinToString("") { "<li>${esc(it)}</li>" }
        return """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>NetWatch report — ${esc(r.title)}</title>
<style>
:root { color-scheme: light dark; --bg:#ffffff; --fg:#1d1d1f; --muted:#6e6e73; --card:#f5f5f7; --line:#e5e5ea; }
@media (prefers-color-scheme: dark) { :root { --bg:#1e2130; --fg:#f2f2f7; --muted:#9a9aa5; --card:#272b3b; --line:#353a4d; } }
body { font: 14px/1.5 system-ui, -apple-system, Roboto, "Segoe UI", sans-serif; background: var(--bg); color: var(--fg); max-width: 1040px; margin: 0 auto; padding: 24px 16px; }
h1 { font-size: 22px; margin: 0 0 4px; } h2 { font-size: 17px; margin: 28px 0 10px; } .muted { color: var(--muted); }
.cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 12px; margin-top: 18px; }
.card { background: var(--card); border-radius: 12px; padding: 14px 16px; } .label { color: var(--muted); font-size: 13px; }
.value { font-size: 24px; font-weight: 700; font-variant-numeric: tabular-nums; } .good { color: #28a745; } .warn { color: #f08c00; }
.timeline { width: 100%; height: 36px; border-radius: 8px; overflow: hidden; display: block; }
.axis { display: flex; justify-content: space-between; color: var(--muted); font-size: 12px; margin-top: 4px; }
.legend { display: flex; flex-wrap: wrap; gap: 14px; margin-top: 10px; font-size: 13px; }
.legend i { display: inline-block; width: 10px; height: 10px; border-radius: 50%; margin-right: 6px; }
.table { overflow-x: auto; } table { width: 100%; border-collapse: collapse; font-size: 13px; }
th, td { text-align: left; padding: 7px 8px; border-bottom: 1px solid var(--line); vertical-align: top; }
th { color: var(--muted); font-weight: 600; } td.num { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }
.dot { display: inline-block; width: 9px; height: 9px; border-radius: 50%; margin-right: 7px; } .empty { color: var(--muted); text-align: center; padding: 18px; }
footer { margin-top: 36px; color: var(--muted); font-size: 12px; }
</style></head><body>
<h1>NetWatch connectivity report</h1>
<div class="muted">${esc(r.title)} · ${esc(Fmt.isoLocal(s.rangeStart))} → ${esc(Fmt.isoLocal(s.rangeEnd))}<br>Generated ${esc(Fmt.isoLocal(r.generatedAt))} on ${esc(r.device)}</div>
<p class="muted">Monitored for ${esc(Fmt.durationLong(s.monitored))} out of ${esc(Fmt.durationLong(s.rangeDuration))} (${esc(Fmt.percent(s.coveragePct, 1))} coverage).</p>
<div class="cards">
${card("Uptime", Fmt.percent(s.uptimePct), "good")}
${card("Total downtime", "${Fmt.duration(s.downtime)} (${Fmt.percent(s.downtimePct, 1)})", "warn")}
${card("Outages", s.outageCount.toString())}
${card("Longest outage", lo?.let { Fmt.duration(it.durationInRange) + if (it.ongoing) " (ongoing)" else "" } ?: "—")}
</div>
<h2>Event timeline</h2>
${timelineSvg(r)}
<div class="axis"><span>${esc(Fmt.isoLocal(s.rangeStart))}</span><span>${esc(Fmt.isoLocal(s.rangeEnd))}</span></div>
<div class="legend">$legend</div>
<h2>Outages</h2>
<div class="table"><table><thead><tr><th>ID</th><th>Type</th><th>Start</th><th>End</th><th>Duration</th><th>Status</th><th>Targets</th></tr></thead><tbody>$outageRows</tbody></table></div>
<h2>Events</h2>
<div class="table"><table><thead><tr><th>State</th><th>Start</th><th>End</th><th>Duration</th><th>Details</th></tr></thead><tbody>$segRows</tbody></table></div>
<h2>Limitations of the collected data</h2>
<ul>$limitations</ul>
<footer>Generated by NetWatch for Android. Uptime = online time ÷ monitored time; unmonitored time is excluded from both uptime and downtime.</footer>
</body></html>
"""
    }
}
