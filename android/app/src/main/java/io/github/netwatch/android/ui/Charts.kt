package io.github.netwatch.android.ui

import android.annotation.SuppressLint
import android.content.Context
import android.graphics.Canvas
import android.graphics.Paint
import android.graphics.Path
import android.graphics.RectF
import android.view.KeyEvent
import android.view.MotionEvent
import android.view.View
import io.github.netwatch.android.core.Bucket
import io.github.netwatch.android.core.ConnState
import io.github.netwatch.android.core.DayStats
import io.github.netwatch.android.core.Fmt
import io.github.netwatch.android.core.Segment
import java.time.DayOfWeek
import java.time.format.TextStyle
import kotlin.math.abs
import kotlin.math.floor
import kotlin.math.log10
import kotlin.math.max
import kotlin.math.min
import kotlin.math.pow

private val UNMONITORED = setOf(ConnState.NOT_RUNNING, ConnState.PAUSED)

/** Fill a rectangle with a light diagonal hatch (unmonitored time). */
fun hatch(c: Canvas, r: RectF, p: Palette, paint: Paint, spacing: Float, alpha: Float = 1f) {
    c.save()
    c.clipRect(r)
    paint.style = Paint.Style.FILL
    paint.color = p.unmonitored
    paint.alpha = (paint.alpha * alpha).toInt()
    c.drawRect(r, paint)
    paint.color = p.hatch
    paint.alpha = (paint.alpha * alpha).toInt()
    paint.strokeWidth = max(1f, spacing / 5)
    var x = r.left - r.height()
    while (x < r.right + r.height()) {
        c.drawLine(x, r.bottom, x + r.height(), r.top, paint)
        x += spacing
    }
    c.restore()
}

/**
 * Representative state per pixel column: any downtime inside a column wins (so a 20 s outage in a
 * 30-day view is still visible as a thin line, as in the reference design); otherwise the majority state.
 */
fun columnStates(segments: List<Segment>, start: Double, end: Double, columns: Int): Array<ConnState?> {
    val width = max(1e-9, end - start) / columns
    val out = arrayOfNulls<ConnState>(columns)
    val acc = Array(columns) { DoubleArray(ConnState.entries.size) }
    for (seg in segments) {
        val s = max(seg.start, start)
        val e = min(seg.end, end)
        if (e <= s) continue
        val c0 = ((s - start) / width).toInt().coerceIn(0, columns - 1)
        val c1 = ((e - start) / width - 1e-9).toInt().coerceIn(0, columns - 1)
        for (c in c0..c1) {
            val cs = start + c * width
            val overlap = min(e, cs + width) - max(s, cs)
            if (overlap > 0) acc[c][seg.state.ordinal] += overlap
        }
    }
    for (c in 0 until columns) {
        val a = acc[c]
        val down = listOf(ConnState.NO_INTERNET, ConnState.NOT_CONNECTED).filter { a[it.ordinal] > 0 }
        out[c] = if (down.isNotEmpty()) down.maxBy { a[it.ordinal] }
        else ConnState.entries.filter { a[it.ordinal] > 0 }.maxByOrNull { a[it.ordinal] }
    }
    return out
}

/** Horizontal event timeline; tap selects a period, drag to inspect, D-pad left/right to step. */
@SuppressLint("ViewConstructor")
class TimelineView(context: Context, private val ui: Ui, private val barDp: Int = 56, private val axis: Boolean = true) : View(context) {
    var segments: List<Segment> = emptyList(); private set
    private var starts = DoubleArray(0)
    var start = 0.0; private set
    var end = 1.0; private set
    private var now: Double? = null
    var selectedKey: String? = null
        set(v) { field = v; invalidate() }
    private var touchX: Float? = null
    private var cursor: Int? = null
    var onSelect: ((Segment) -> Unit)? = null
    var onInspect: ((String?) -> Unit)? = null
    private val paint = Paint(Paint.ANTI_ALIAS_FLAG)
    private val textPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply { textSize = ui.dpf(12); color = ui.p.muted }
    private val bar get() = ui.dpf(barDp)

    init {
        isFocusable = true
        contentDescription = "Event timeline"
    }

    fun setData(segs: List<Segment>, s: Double, e: Double, n: Double?) {
        segments = segs
        starts = DoubleArray(segs.size) { segs[it].start }
        start = s
        end = max(e, s + 1)
        now = n
        cursor = null
        val downs = segs.count { it.state.isDown }
        contentDescription = "Event timeline, ${segs.size} periods from ${Fmt.timestamp(s, n ?: e)} to ${Fmt.timestamp(e, n ?: e)}, " +
            "$downs without connectivity. Double-tap a period in the event log for details."
        invalidate()
    }

    fun segmentAt(t: Double): Segment? {
        var i = starts.binarySearch(t).let { if (it >= 0) it else -it - 2 }
        if (i < 0 || i >= segments.size) return null
        val s = segments[i]
        if (t > s.end && i + 1 < segments.size) i++
        return segments[i].takeIf { t >= it.start && t <= it.end }
    }

    private fun timeAt(x: Float) = start + x / max(1, width) * (end - start)

    override fun onMeasure(widthMeasureSpec: Int, heightMeasureSpec: Int) {
        setMeasuredDimension(MeasureSpec.getSize(widthMeasureSpec), (bar + if (axis) ui.dpf(22) else 0f).toInt())
    }

    override fun onDraw(c: Canvas) {
        val w = width
        if (w <= 0) return
        val p = ui.p
        val r = RectF(0f, 0f, w.toFloat(), bar)
        val clip = Path().apply { addRoundRect(r, ui.dpf(8), ui.dpf(8), Path.Direction.CW) }
        c.save()
        c.clipPath(clip)
        val cols = columnStates(segments, start, end, w)
        var runStart = 0
        var runState = cols.firstOrNull()
        for (x in 1..cols.size) {
            val st = if (x < cols.size) cols[x] else null
            if (x == cols.size || st != runState) {
                val rr = RectF(runStart.toFloat(), 0f, x.toFloat(), bar)
                if (runState == null || runState in UNMONITORED) hatch(c, rr, p, paint, ui.dpf(6), if (runState == null) 0.6f else 1f)
                else { paint.style = Paint.Style.FILL; paint.color = p.state(runState); c.drawRect(rr, paint) }
                runStart = x
                runState = st
            }
        }
        selectedKey?.let { key ->
            segments.firstOrNull { it.key == key }?.let { seg ->
                val x0 = ((seg.start - start) / (end - start) * w).toFloat()
                val x1 = ((seg.end - start) / (end - start) * w).toFloat()
                paint.style = Paint.Style.STROKE; paint.strokeWidth = ui.dpf(2.5f); paint.color = p.accent
                c.drawRect(x0 + ui.dpf(1.25f), ui.dpf(1.25f), max(x0 + ui.dpf(3), x1 - ui.dpf(1.25f)), bar - ui.dpf(1.25f), paint)
            }
        }
        touchX?.let { paint.style = Paint.Style.FILL; paint.color = p.fg; c.drawRect(it, 0f, it + ui.dpf(1.5f), bar, paint) }
        c.restore()
        if (axis) {
            val span = end - start
            val n = if (w > ui.dp(520)) 5 else 3
            for (i in 0 until n) {
                val frac = i / (n - 1f)
                val t = start + frac * span
                val label = if (i == n - 1 && now != null && abs(end - now!!) < 120) "now" else Fmt.axisLabel(t, span)
                val tw = textPaint.measureText(label)
                val x = frac * w - when (i) { 0 -> 0f; n - 1 -> tw; else -> tw / 2 }
                c.drawText(label, x.coerceIn(0f, w - tw), bar + ui.dpf(17), textPaint)
            }
        }
    }

    fun describe(seg: Segment): String {
        val n = now ?: end
        val endS = if (seg.ongoing) "now" else Fmt.timestamp(seg.end, n, seg.duration < 600)
        return "${seg.state.label}: ${Fmt.timestamp(seg.start, n, seg.duration < 600)} – $endS (${Fmt.duration(seg.duration, true)})"
    }

    @SuppressLint("ClickableViewAccessibility")
    override fun onTouchEvent(e: MotionEvent): Boolean {
        when (e.actionMasked) {
            MotionEvent.ACTION_DOWN, MotionEvent.ACTION_MOVE -> {
                parent?.requestDisallowInterceptTouchEvent(e.actionMasked == MotionEvent.ACTION_MOVE)
                touchX = e.x.coerceIn(0f, width.toFloat())
                onInspect?.invoke(segmentAt(timeAt(touchX!!))?.let(::describe) ?: "No records")
                invalidate()
            }
            MotionEvent.ACTION_UP -> {
                segmentAt(timeAt(e.x))?.let { seg -> selectedKey = seg.key; onSelect?.invoke(seg) }
                touchX = null
                invalidate()
                performClick()
            }
            MotionEvent.ACTION_CANCEL -> { touchX = null; onInspect?.invoke(null); invalidate() }
        }
        return true
    }

    override fun performClick(): Boolean = super.performClick()

    override fun onKeyDown(keyCode: Int, event: KeyEvent): Boolean {
        if (segments.isEmpty()) return super.onKeyDown(keyCode, event)
        when (keyCode) {
            KeyEvent.KEYCODE_DPAD_LEFT, KeyEvent.KEYCODE_DPAD_RIGHT -> {
                val cur = cursor ?: segments.size
                cursor = (cur + if (keyCode == KeyEvent.KEYCODE_DPAD_LEFT) -1 else 1).coerceIn(0, segments.size - 1)
                val seg = segments[cursor!!]
                selectedKey = seg.key
                announceForAccessibility(describe(seg))
                onInspect?.invoke(describe(seg))
                return true
            }
            KeyEvent.KEYCODE_DPAD_CENTER, KeyEvent.KEYCODE_ENTER -> {
                cursor?.let { onSelect?.invoke(segments[it]) }
                return true
            }
        }
        return super.onKeyDown(keyCode, event)
    }
}

/** Calendar heat map of daily reliability, Monday-first weeks as columns. Tap a day to filter the log. */
@SuppressLint("ViewConstructor")
class CalendarView(context: Context, private val ui: Ui, private val showLabels: Boolean = true) : View(context) {
    var days: List<DayStats> = emptyList(); private set
    var selected: java.time.LocalDate? = null
        set(v) { field = v; invalidate() }
    var onSelect: ((DayStats?) -> Unit)? = null
    var onInspect: ((String?) -> Unit)? = null
    private val cell get() = ui.dpf(18)
    private val gap get() = ui.dpf(4)
    private val labelW get() = if (showLabels) ui.dpf(38) else 0f
    private val paint = Paint(Paint.ANTI_ALIAS_FLAG)
    private val textPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply { textSize = ui.dpf(11.5f); color = ui.p.muted }
    private var cursor: Int? = null

    init { isFocusable = true; contentDescription = "Outage calendar" }

    fun setDays(d: List<DayStats>) {
        days = d
        if (selected != null && d.none { it.day == selected }) selected = null
        cursor = null
        val counts = d.filter { it.inRange }.groupingBy { it.category }.eachCount()
        contentDescription = "Outage calendar, ${counts.values.sum()} days: " +
            counts.entries.joinToString(", ") { "${it.value} ${categoryText(it.key).lowercase()}" }
        requestLayout()
        invalidate()
    }

    private val weeks get() = (days.size + 6) / 7

    override fun onMeasure(widthMeasureSpec: Int, heightMeasureSpec: Int) {
        val step = cell + gap
        setMeasuredDimension((labelW + max(1, weeks) * step).toInt(), (7 * step + ui.dpf(20)).toInt())
    }

    private fun cellRect(i: Int): RectF {
        val step = cell + gap
        val x = labelW + (i / 7) * step
        val y = (i % 7) * step
        return RectF(x, y, x + cell, y + cell)
    }

    private fun indexAt(x: Float, y: Float): Int? {
        val step = cell + gap
        if (x < labelW) return null
        val week = floor((x - labelW) / step).toInt()
        val dow = floor(y / step).toInt()
        if (dow !in 0..6 || week < 0) return null
        return (week * 7 + dow).takeIf { it < days.size }
    }

    override fun onDraw(c: Canvas) {
        val p = ui.p
        val step = cell + gap
        if (showLabels) drawWeekdayLabels(c, ui, textPaint, labelW, step, cell)
        var lastMonth = -1
        var lastLabelX = -1000f
        days.forEachIndexed { i, d ->
            val r = cellRect(i)
            val cat = d.category
            val rr = ui.dpf(4)
            paint.style = Paint.Style.FILL
            when (cat) {
                "outside" -> { paint.color = withAlpha(p.fg, 0x0A); c.drawRoundRect(r, rr, rr, paint) }
                "unmonitored" -> {
                    c.save(); c.clipPath(Path().apply { addRoundRect(r, rr, rr, Path.Direction.CW) })
                    hatch(c, r, p, paint, ui.dpf(4)); c.restore()
                }
                else -> { paint.color = p.category(cat); c.drawRoundRect(r, rr, rr, paint) }
            }
            if (d.day == selected || i == cursor) {
                paint.style = Paint.Style.STROKE; paint.strokeWidth = ui.dpf(2); paint.color = p.accent
                val o = ui.dpf(1.5f)
                c.drawRoundRect(RectF(r.left - o, r.top - o, r.right + o, r.bottom + o), rr + o, rr + o, paint)
            }
            if (i % 7 == 0) {
                val monthDay = (i until min(i + 7, days.size)).map { days[it].day }.firstOrNull { it.dayOfMonth == 1 }
                    ?: d.day.takeIf { lastMonth == -1 && d.inRange }
                if (monthDay != null && monthDay.monthValue != lastMonth && r.left - lastLabelX > ui.dpf(30)) {
                    c.drawText(monthDay.month.getDisplayName(TextStyle.SHORT, Fmt.locale), r.left, 7 * step + ui.dpf(13), textPaint)
                    lastMonth = monthDay.monthValue
                    lastLabelX = r.left
                }
            }
        }
    }

    fun describe(d: DayStats): String {
        val head = Fmt.dateLong(d.day)
        return when {
            d.category == "outside" -> "$head: outside the selected range"
            d.monitored <= 0 -> "$head: not monitored"
            else -> "$head: monitored ${Fmt.durationLong(d.monitored)} (${"%.0f".format(d.coveragePct)}% of the day), " +
                "${d.outageCount} outage${if (d.outageCount == 1) "" else "s"}, ${Fmt.duration(d.downtime, true)} downtime"
        }
    }

    private fun toggle(i: Int) {
        val d = days[i]
        if (d.category == "outside") return
        selected = if (selected == d.day) null else d.day
        onSelect?.invoke(if (selected == null) null else d)
    }

    @SuppressLint("ClickableViewAccessibility")
    override fun onTouchEvent(e: MotionEvent): Boolean {
        val i = indexAt(e.x, e.y)
        when (e.actionMasked) {
            MotionEvent.ACTION_DOWN -> { onInspect?.invoke(i?.let { describe(days[it]) }) }
            MotionEvent.ACTION_UP -> { i?.let { toggle(it); onInspect?.invoke(describe(days[it])) }; performClick() }
        }
        return true
    }

    override fun performClick(): Boolean = super.performClick()

    override fun onKeyDown(keyCode: Int, event: KeyEvent): Boolean {
        if (days.isEmpty()) return super.onKeyDown(keyCode, event)
        val moves = mapOf(KeyEvent.KEYCODE_DPAD_LEFT to -7, KeyEvent.KEYCODE_DPAD_RIGHT to 7, KeyEvent.KEYCODE_DPAD_UP to -1, KeyEvent.KEYCODE_DPAD_DOWN to 1)
        moves[keyCode]?.let { m ->
            cursor = ((cursor ?: days.indexOfLast { it.inRange }) + m).coerceIn(0, days.size - 1)
            announceForAccessibility(describe(days[cursor!!]))
            onInspect?.invoke(describe(days[cursor!!]))
            invalidate()
            return true
        }
        if ((keyCode == KeyEvent.KEYCODE_DPAD_CENTER || keyCode == KeyEvent.KEYCODE_ENTER) && cursor != null) { toggle(cursor!!); return true }
        return super.onKeyDown(keyCode, event)
    }

    companion object {
        fun categoryText(c: String) = when (c) {
            "online" -> "Online all monitored time"
            "minor" -> "Mostly online, brief outages"
            "no_internet" -> "No-internet outages"
            "major" -> "Major outage time"
            "not_connected" -> "Mostly not connected"
            "unmonitored" -> "Not monitored"
            else -> "Outside the selected range"
        }
    }
}

internal fun drawWeekdayLabels(c: Canvas, ui: Ui, paint: Paint, right: Float, step: Float, cell: Float) {
    for (dow in listOf(0, 2, 4)) {
        val label = DayOfWeek.of(dow + 1).getDisplayName(TextStyle.SHORT, Fmt.locale)
        c.drawText(label, right - paint.measureText(label) - ui.dpf(6), dow * step + cell * 0.75f, paint)
    }
}

/** Mon/Wed/Fri labels kept outside the calendar's horizontal scroll so they stay visible. */
@SuppressLint("ViewConstructor")
class WeekdayLabels(context: Context, private val ui: Ui) : View(context) {
    private val paint = Paint(Paint.ANTI_ALIAS_FLAG).apply { textSize = ui.dpf(11.5f); color = ui.p.muted }
    init { importantForAccessibility = IMPORTANT_FOR_ACCESSIBILITY_NO }
    override fun onMeasure(w: Int, h: Int) = setMeasuredDimension(ui.dp(38), (7 * ui.dpf(22) + ui.dpf(20)).toInt())
    override fun onDraw(c: Canvas) = drawWeekdayLabels(c, ui, paint, width.toFloat(), ui.dpf(22), ui.dpf(18))
}

fun niceCeiling(v: Double): Double {
    if (v <= 0) return 1.0
    val exp = 10.0.pow(floor(log10(v)))
    for (m in listOf(1.0, 2.0, 2.5, 5.0, 10.0)) if (v <= m * exp) return m * exp
    return 10 * exp
}

/** Shared axis drawing and touch inspection for the Performance charts. */
abstract class ChartView(context: Context, protected val ui: Ui, heightDp: Int) : View(context) {
    protected var start = 0.0
    protected var end = 1.0
    protected var touchT: Double? = null
    var onInspect: ((String?) -> Unit)? = null
    protected val paint = Paint(Paint.ANTI_ALIAS_FLAG)
    protected val textPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply { textSize = ui.dpf(11); color = ui.p.muted }
    private val h = ui.dp(heightDp)
    protected val padL get() = ui.dpf(46)
    protected val padR get() = ui.dpf(8)
    protected val padT get() = ui.dpf(8)
    protected val padB get() = ui.dpf(22)

    init { isFocusable = true }

    override fun onMeasure(widthMeasureSpec: Int, heightMeasureSpec: Int) = setMeasuredDimension(MeasureSpec.getSize(widthMeasureSpec), h)

    protected fun plot() = RectF(padL, padT, width - padR, height - padB)
    protected fun x(t: Double, r: RectF) = (r.left + (t - start) / max(1e-9, end - start) * r.width()).toFloat()

    protected fun axes(c: Canvas, ymax: Double, fmt: (Double) -> String) {
        val r = plot()
        paint.style = Paint.Style.STROKE
        paint.strokeWidth = 1f
        for (i in 0..4) {
            val y = r.bottom - r.height() * i / 4
            paint.color = withAlpha(ui.p.fg, if (i == 0) 0x40 else 0x14)
            c.drawLine(r.left, y, r.right, y, paint)
            val label = fmt(ymax * i / 4)
            c.drawText(label, r.left - textPaint.measureText(label) - ui.dpf(6), y + ui.dpf(4), textPaint)
        }
        val span = end - start
        val n = if (r.width() > ui.dp(400)) 5 else 3
        for (i in 0 until n) {
            val t = start + span * i / (n - 1)
            val label = Fmt.axisLabel(t, span)
            val tw = textPaint.measureText(label)
            val xx = x(t, r) - when (i) { 0 -> 0f; n - 1 -> tw; else -> tw / 2 }
            c.drawText(label, xx, r.bottom + ui.dpf(16), textPaint)
        }
    }

    protected fun emptyText(c: Canvas) {
        val r = plot()
        val s = "No measurements in this period"
        c.drawText(s, r.centerX() - textPaint.measureText(s) / 2, r.centerY(), textPaint)
    }

    abstract fun inspect(t: Double): String

    @SuppressLint("ClickableViewAccessibility")
    override fun onTouchEvent(e: MotionEvent): Boolean {
        val r = plot()
        when (e.actionMasked) {
            MotionEvent.ACTION_DOWN, MotionEvent.ACTION_MOVE -> {
                parent?.requestDisallowInterceptTouchEvent(e.actionMasked == MotionEvent.ACTION_MOVE)
                touchT = start + ((e.x.coerceIn(r.left, r.right) - r.left) / r.width()) * (end - start)
                onInspect?.invoke(inspect(touchT!!))
                invalidate()
            }
            MotionEvent.ACTION_UP, MotionEvent.ACTION_CANCEL -> { performClick() }
        }
        return true
    }

    override fun performClick() = super.performClick()

    override fun onKeyDown(keyCode: Int, event: KeyEvent): Boolean {
        if (keyCode != KeyEvent.KEYCODE_DPAD_LEFT && keyCode != KeyEvent.KEYCODE_DPAD_RIGHT) return super.onKeyDown(keyCode, event)
        val step = (end - start) / 60
        touchT = ((touchT ?: (end - step / 2)) + if (keyCode == KeyEvent.KEYCODE_DPAD_LEFT) -step else step).coerceIn(start, end)
        val text = inspect(touchT!!)
        onInspect?.invoke(text)
        announceForAccessibility(text)
        invalidate()
        return true
    }

    protected fun whenLabel(t: Double): String {
        val span = end - start
        return if (span <= 2 * 86400) Fmt.clock(t, span <= 7200) else Fmt.timestamp(t, end)
    }
}

data class Series(val name: String, val color: Int, val points: List<Bucket>)

/** Latency per target (bucket averages + min–max band). Gaps are breaks; failed checks are red ticks. */
@SuppressLint("ViewConstructor")
class LatencyChartView(context: Context, ui: Ui) : ChartView(context, ui, 230) {
    private var series: List<Series> = emptyList()
    private var bucket = 60.0
    private var maxGap = 150.0

    init { contentDescription = "Latency over time" }

    fun setData(s: Double, e: Double, b: Double, data: List<Series>, interval: Double) {
        start = s; end = e; bucket = b; series = data
        maxGap = 2.5 * max(b, interval)
        touchT = null
        contentDescription = "Latency chart with ${data.size} targets and ${data.sumOf { it.points.size }} data points"
        invalidate()
    }

    override fun onDraw(c: Canvas) {
        val values = series.flatMap { s -> s.points.mapNotNull { it.max ?: it.avg } }
        val ymax = if (values.isEmpty()) 100.0 else niceCeiling(values.max() * 1.1)
        axes(c, ymax) { "%.0f ms".format(it) }
        val r = plot()
        if (series.all { it.points.isEmpty() }) { emptyText(c); return }
        fun y(v: Double) = (r.bottom - min(1.0, v / ymax) * r.height()).toFloat()
        val bw = max(1f, (bucket / (end - start) * r.width()).toFloat())
        for (s in series) {
            paint.style = Paint.Style.FILL
            paint.color = withAlpha(s.color, 0x26)
            for (pt in s.points) {
                if (pt.avg == null || pt.min == null || pt.max == null) continue
                val xx = x(pt.t, r)
                c.drawRect(xx - bw / 2, y(pt.max), xx + bw / 2, max(y(pt.min), y(pt.max) + 1), paint)
            }
            paint.style = Paint.Style.STROKE
            paint.strokeWidth = ui.dpf(1.8f)
            paint.color = s.color
            val path = Path()
            var prev: Double? = null
            for (pt in s.points) {
                val avg = pt.avg
                if (avg == null) { prev = null; continue }
                val xx = x(pt.t, r)
                if (prev == null || pt.t - prev > maxGap) {
                    path.moveTo(xx, y(avg))
                    path.addCircle(xx, y(avg), ui.dpf(1.2f), Path.Direction.CW)
                    path.moveTo(xx, y(avg))
                } else path.lineTo(xx, y(avg))
                prev = pt.t
            }
            c.drawPath(path, paint)
        }
        paint.style = Paint.Style.FILL
        paint.color = ui.p.error
        for (s in series) for (pt in s.points) if (pt.ok < pt.n) {
            val xx = x(pt.t, r)
            c.drawRect(xx - 1.5f, r.bottom - ui.dpf(6), xx + 1.5f, r.bottom, paint)
        }
        touchT?.let { t ->
            paint.color = withAlpha(ui.p.fg, 0x80)
            c.drawRect(x(t, r), r.top, x(t, r) + 1.5f, r.bottom, paint)
            for (s in series) nearest(s, t)?.avg?.let { v -> paint.color = s.color; c.drawCircle(x(nearest(s, t)!!.t, r), y(v), ui.dpf(3.5f), paint) }
        }
    }

    private fun nearest(s: Series, t: Double): Bucket? = s.points.minByOrNull { abs(it.t - t) }?.takeIf { abs(it.t - t) <= maxGap / 2 }

    override fun inspect(t: Double): String = whenLabel(t) + " — " + series.joinToString(" · ") { s ->
        val pt = nearest(s, t)
        when {
            pt == null -> "${s.name}: no data"
            pt.avg == null -> "${s.name}: all ${pt.n} checks failed"
            else -> "${s.name}: ${"%.1f".format(pt.avg)} ms" + if (pt.ok < pt.n) " (${pt.n - pt.ok} failed)" else ""
        }
    }
}

/** Share of failed checks per bucket across all targets; buckets without checks stay empty. */
@SuppressLint("ViewConstructor")
class LossChartView(context: Context, ui: Ui) : ChartView(context, ui, 150) {
    private var buckets: List<Bucket> = emptyList()
    private var bucket = 60.0

    init { contentDescription = "Packet loss over time" }

    fun setData(s: Double, e: Double, b: Double, data: List<Bucket>) {
        start = s; end = e; bucket = b; buckets = data.sortedBy { it.t }
        touchT = null
        contentDescription = "Packet loss chart, ${buckets.size} intervals with data, ${buckets.count { it.ok < it.n }} with loss"
        invalidate()
    }

    override fun onDraw(c: Canvas) {
        axes(c, 100.0) { "%.0f%%".format(it) }
        val r = plot()
        if (buckets.isEmpty()) { emptyText(c); return }
        val bw = max(1f, (bucket / (end - start) * r.width()).toFloat() - 1)
        paint.style = Paint.Style.FILL
        for (b in buckets) {
            val xx = x(b.t, r) - bw / 2
            val loss = if (b.n > 0) (b.n - b.ok) * 100.0 / b.n else 0.0
            paint.color = withAlpha(ui.p.online, 0x5A)
            c.drawRect(xx, r.bottom - ui.dpf(2), xx + bw, r.bottom, paint)
            if (loss > 0) {
                paint.color = if (loss < 100) ui.p.noInternet else ui.p.error
                c.drawRect(xx, r.bottom - max(ui.dpf(2), (loss / 100 * r.height()).toFloat()), xx + bw, r.bottom, paint)
            }
        }
        touchT?.let { t -> paint.color = withAlpha(ui.p.fg, 0x80); c.drawRect(x(t, r), r.top, x(t, r) + 1.5f, r.bottom, paint) }
    }

    override fun inspect(t: Double): String {
        val b = buckets.minByOrNull { abs(it.t - t) }
        if (b == null || abs(b.t - t) > bucket) return "${whenLabel(t)} — no checks recorded"
        return "${whenLabel(t)} — ${"%.1f".format((b.n - b.ok) * 100.0 / b.n)}% loss (${b.n - b.ok} of ${b.n} checks failed)"
    }
}
