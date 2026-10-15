package io.github.netwatch.android.core

import org.json.JSONArray
import java.time.DayOfWeek
import java.time.LocalDate
import java.time.temporal.TemporalAdjusters

/*
 * Reliability calculation rules — identical to NetWatch for Linux (netwatch/stats.py):
 *  - Monitored time = ONLINE + NO_INTERNET + NOT_CONNECTED.
 *  - UNKNOWN, PAUSED and NOT_RUNNING are not monitored: neither uptime nor downtime; reported as coverage.
 *  - Uptime % = ONLINE / monitored; downtime % = (NO_INTERNET + NOT_CONNECTED) / monitored.
 *  - Degraded connectivity (slow but reachable) counts as ONLINE; quality is shown on the Performance screen.
 *  - Overlapping records (backwards clock change) are trimmed so no instant is counted twice.
 */

const val MIN_GAP_SECONDS = 1.0

data class Segment(
    val state: ConnState,
    val start: Double,
    val end: Double,
    val ongoing: Boolean = false,
    val stateId: Long? = null,
    val sessionId: Long? = null,
    val targets: String? = null,
    val iface: String? = null,
    val reason: String? = null,
    val synthetic: Boolean = false, // generated for a gap (app not running)
    var outageId: Long? = null,
) {
    val duration: Double get() = maxOf(0.0, end - start)
    val local: LocalNetworkStatus? by lazy { LocalNetworkStatus.fromJson(iface) }
    val key: String get() = if (stateId != null) "s$stateId" else "g%.3f".format(java.util.Locale.US, start)
}

data class OutageInfo(
    val id: Long,
    val classification: ConnState,
    val start: Double,
    val end: Double,
    val ongoing: Boolean,
    val status: String,
    val startObserved: Boolean,
    val endObserved: Boolean,
    val detectedAt: Double,
    val recoveryDetectedAt: Double?,
    val durationInRange: Double,
    val fullDuration: Double,
    val evidence: JSONArray,
    val targets: List<Row>,
) {
    val statusLabel: String
        get() = when {
            ongoing -> "ongoing"
            endObserved -> "recovered"
            else -> when (status) {
                "monitoring_stopped" -> "monitoring stopped before recovery"
                "monitoring_paused" -> "monitoring paused before recovery"
                "monitoring_interrupted" -> "monitoring interrupted before recovery"
                "evidence_lost" -> "evidence lost before recovery"
                else -> status
            }
        }
}

class PeriodSummary(
    val rangeStart: Double,
    val rangeEnd: Double,
    val durations: Map<ConnState, Double>,
    val outages: List<OutageInfo>,
    val hasData: Boolean,
) {
    val rangeDuration get() = maxOf(0.0, rangeEnd - rangeStart)
    val online get() = durations[ConnState.ONLINE] ?: 0.0
    val downtime get() = (durations[ConnState.NO_INTERNET] ?: 0.0) + (durations[ConnState.NOT_CONNECTED] ?: 0.0)
    val monitored get() = online + downtime
    val unknown get() = durations[ConnState.UNKNOWN] ?: 0.0
    val paused get() = durations[ConnState.PAUSED] ?: 0.0
    val notRunning get() = durations[ConnState.NOT_RUNNING] ?: 0.0
    val coveragePct: Double? get() = if (rangeDuration > 0) monitored / rangeDuration * 100 else null
    val uptimePct: Double? get() = if (monitored > 0) online / monitored * 100 else null
    val downtimePct: Double? get() = if (monitored > 0) downtime / monitored * 100 else null
    val outageCount get() = outages.size
    val longestOutage: OutageInfo? get() = outages.maxByOrNull { it.durationInRange }
}

class DayStats(val day: LocalDate, val start: Double, val end: Double, val durations: MutableMap<ConnState, Double>,
               var outageCount: Int, val inRange: Boolean) {
    val monitored get() = ConnState.entries.filter { it.isMonitored }.sumOf { durations[it] ?: 0.0 }
    val downtime get() = (durations[ConnState.NO_INTERNET] ?: 0.0) + (durations[ConnState.NOT_CONNECTED] ?: 0.0)
    val span get() = maxOf(0.0, end - start)
    val coveragePct get() = if (span > 0) monitored / span * 100 else 0.0

    /** online | minor | no_internet | major | not_connected | unmonitored | outside */
    val category: String
        get() {
            if (!inRange || span <= 0) return "outside"
            val mon = monitored
            if (mon <= 0) return "unmonitored"
            val down = downtime
            if (down <= 0) return "online"
            val frac = down / mon
            val ni = durations[ConnState.NO_INTERNET] ?: 0.0
            val nc = durations[ConnState.NOT_CONNECTED] ?: 0.0
            return when {
                nc > ni && frac >= 0.5 -> "not_connected"
                frac < 0.01 -> "minor"
                frac >= 0.10 -> "major"
                ni >= nc -> "no_internet"
                else -> "not_connected"
            }
        }
}

class HistoryData(val summary: PeriodSummary, val segments: List<Segment>, val days: List<DayStats>,
                  val outagesById: Map<Long, OutageInfo>)

data class Bucket(val t: Double, val n: Int, val ok: Int, val avg: Double?, val min: Double?, val max: Double?)

data class LatencyStats(val avg: Double?, val median: Double?, val min: Double?, val max: Double?, val p95: Double?,
                        val jitter: Double?, val checks: Int, val ok: Int) {
    val lossPct: Double? get() = if (checks > 0) (checks - ok) * 100.0 / checks else null
}

class Performance(val start: Double, val end: Double, val bucket: Double, val series: Map<Long, List<Bucket>>,
                  val perTarget: Map<Long, LatencyStats>, val overall: LatencyStats)

class ReliabilityService(private val repo: Repository) {

    fun resolveRange(period: String, now: Double): Pair<Double, Double> = when (period) {
        "24h" -> now - 86400 to now
        "7d" -> now - 7 * 86400 to now
        "30d" -> now - 30 * 86400 to now
        else -> {
            val e = repo.earliestRecord()
            (if (e != null && e < now) e else now - 86400) to now
        }
    }

    /** Chronological, non-overlapping segments clipped to [start, end], with gaps as NOT_RUNNING. */
    fun segments(start: Double, endIn: Double, now: Double, includeGaps: Boolean = true): List<Segment> {
        val end = minOf(endIn, now)
        val out = mutableListOf<Segment>()
        var cursor = start
        for (row in repo.statesBetween(start, end)) {
            var s = maxOf(row.d("started_at")!!, start)
            val rawEnd = row.d("ended_at")
            val ongoing = rawEnd == null
            val e = minOf(if (ongoing) now else rawEnd!!, end)
            if (out.isNotEmpty()) s = maxOf(s, out.last().end)
            if (e <= s) continue
            if (includeGaps && s - cursor >= MIN_GAP_SECONDS) out += Segment(ConnState.NOT_RUNNING, cursor, s, synthetic = true)
            out += Segment(ConnState.of(row.s("state")), s, e, ongoing && e >= now - 1e-6, row.l("id"), row.l("session_id"),
                row.s("targets"), row.s("interface"), row.s("reason"))
            cursor = maxOf(cursor, e)
        }
        if (includeGaps && end - cursor >= MIN_GAP_SECONDS) out += Segment(ConnState.NOT_RUNNING, cursor, end, ongoing = true, synthetic = true)
        return out
    }

    fun durations(segments: List<Segment>): Map<ConnState, Double> {
        val totals = ConnState.entries.associateWith { 0.0 }.toMutableMap()
        segments.forEach { totals[it.state] = totals.getValue(it.state) + it.duration }
        return totals
    }

    fun outages(start: Double, end: Double, now: Double): List<OutageInfo> {
        val rows = repo.outagesBetween(start, minOf(end, now))
        val targets = repo.outageTargets(rows.map { it.l("id")!! })
        return rows.map { r ->
            val ongoing = r.d("ended_at") == null
            val oEnd = if (ongoing) now else r.d("ended_at")!!
            val oStart = r.d("started_at")!!
            OutageInfo(
                id = r.l("id")!!, classification = ConnState.of(r.s("classification")), start = oStart, end = oEnd,
                ongoing = ongoing, status = r.s("status") ?: "", startObserved = r.b("start_observed"),
                endObserved = r.b("end_observed"), detectedAt = r.d("detected_at") ?: oStart,
                recoveryDetectedAt = r.d("recovery_detected_at"),
                durationInRange = maxOf(0.0, minOf(oEnd, end) - maxOf(oStart, start)),
                fullDuration = maxOf(0.0, oEnd - oStart),
                evidence = try { JSONArray(r.s("evidence") ?: "[]") } catch (e: Exception) { JSONArray() },
                targets = targets[r.l("id")!!] ?: emptyList(),
            )
        }
    }

    fun summary(start: Double, endIn: Double, now: Double, segs: List<Segment>? = null): PeriodSummary {
        val end = minOf(endIn, now)
        val s = segs ?: segments(start, end, now)
        return PeriodSummary(start, end, durations(s), outages(start, end, now), s.any { !it.synthetic })
    }

    /** Per local calendar day statistics, padded to whole Monday–Sunday weeks. */
    fun daily(start: Double, end: Double, segs: List<Segment>, outages: List<OutageInfo>, now: Double): List<DayStats> {
        val first = Fmt.localDate(start)
        val last = Fmt.localDate(minOf(end, now))
        val gridStart = first.with(TemporalAdjusters.previousOrSame(DayOfWeek.MONDAY))
        val gridEnd = last.with(TemporalAdjusters.nextOrSame(DayOfWeek.SUNDAY))
        val days = mutableListOf<DayStats>()
        var d = gridStart
        while (!d.isAfter(gridEnd)) {
            val ds = Fmt.dayStart(d)
            val de = Fmt.dayStart(d.plusDays(1))
            days += DayStats(d, maxOf(ds, start), minOf(de, end, now), ConnState.entries.associateWith { 0.0 }.toMutableMap(), 0,
                !d.isBefore(first) && !d.isAfter(last))
            d = d.plusDays(1)
        }
        val index = days.withIndex().associate { it.value.day to it.index }
        for (seg in segs) {
            var t = seg.start
            while (t < seg.end) {
                val day = Fmt.localDate(t)
                val boundary = Fmt.dayStart(day.plusDays(1))
                val chunkEnd = minOf(seg.end, boundary)
                index[day]?.let { days[it].durations[seg.state] = days[it].durations.getValue(seg.state) + (chunkEnd - t) }
                t = chunkEnd
            }
        }
        for (o in outages) index[Fmt.localDate(maxOf(o.start, start))]?.let { days[it].outageCount++ }
        return days
    }

    fun history(period: String, now: Double, custom: Pair<Double, Double>? = null): HistoryData {
        val (start, end) = custom ?: resolveRange(period, now)
        val segs = segments(start, end, now)
        val summary = summary(start, end, now, segs)
        val byId = summary.outages.associateBy { it.id }
        val starts = summary.outages.map { it.start }
        for (seg in segs) {
            if (!seg.state.isDown || summary.outages.isEmpty()) continue
            var i = starts.binarySearch(seg.start + 1e-6).let { if (it >= 0) it else -it - 2 }
            if (i >= starts.size) i = starts.size - 1
            if (i >= 0 && summary.outages[i].end >= seg.start) seg.outageId = summary.outages[i].id
        }
        return HistoryData(summary, segs, daily(start, end, segs, summary.outages, now), byId)
    }

    fun performance(start: Double, end: Double, buckets: Int = 120): Performance {
        val span = maxOf(1.0, end - start)
        val bucket = maxOf(1.0, span / buckets)
        val series = mutableMapOf<Long, MutableList<Bucket>>()
        for (r in repo.measurementBuckets(start, end, bucket)) {
            series.getOrPut(r.l("target_id")!!) { mutableListOf() } += Bucket(
                start + ((r.l("b") ?: 0) + 0.5) * bucket, r.l("n")!!.toInt(), (r.l("ok") ?: 0).toInt(),
                r.d("avg_ms"), r.d("min_ms"), r.d("max_ms"))
        }
        val values = mutableMapOf<Long, MutableList<Double>>()
        repo.latencyValues(start, end).forEach { values.getOrPut(it.l("target_id")!!) { mutableListOf() } += it.d("latency_ms")!! }
        val counts = repo.checkCounts(start, end).associateBy { it.l("target_id")!! }
        val per = (values.keys + counts.keys).associateWith { tid ->
            val c = counts[tid]
            latencyStats(values[tid] ?: emptyList(), c?.l("n")?.toInt() ?: 0, c?.l("ok")?.toInt() ?: 0)
        }
        val overall = latencyStats(values.values.flatten(), per.values.sumOf { it.checks }, per.values.sumOf { it.ok })
        return Performance(start, end, bucket, series, per, overall)
    }

    data class Quality(val checks: Int, val lossPct: Double?, val medianMs: Double?)

    fun recentQuality(now: Double, window: Double = 300.0): Quality {
        val counts = repo.checkCounts(now - window, now + 1)
        val n = counts.sumOf { it.l("n")!!.toInt() }
        val ok = counts.sumOf { (it.l("ok") ?: 0).toInt() }
        val values = repo.latencyValues(now - window, now + 1).mapNotNull { it.d("latency_ms") }
        return Quality(n, if (n > 0) (n - ok) * 100.0 / n else null, median(values.sorted()))
    }

    companion object {
        fun median(sorted: List<Double>): Double? = when {
            sorted.isEmpty() -> null
            sorted.size % 2 == 1 -> sorted[sorted.size / 2]
            else -> (sorted[sorted.size / 2 - 1] + sorted[sorted.size / 2]) / 2
        }

        fun latencyStats(values: List<Double>, checks: Int, ok: Int): LatencyStats {
            if (values.isEmpty()) return LatencyStats(null, null, null, null, null, null, checks, ok)
            val s = values.sorted()
            val avg = s.average()
            val p95 = s[minOf(s.size - 1, Math.round(0.95 * (s.size - 1)).toInt())]
            val jitter = if (s.size > 1) Math.sqrt(s.sumOf { (it - avg) * (it - avg) } / s.size) else 0.0
            return LatencyStats(avg, median(s), s.first(), s.last(), p95, jitter, checks, ok)
        }
    }
}
