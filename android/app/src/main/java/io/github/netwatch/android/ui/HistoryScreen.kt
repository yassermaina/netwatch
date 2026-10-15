package io.github.netwatch.android.ui

import android.app.AlertDialog
import android.text.Editable
import android.text.TextWatcher
import android.view.View
import android.view.ViewGroup
import android.widget.AdapterView
import android.widget.ArrayAdapter
import android.widget.BaseAdapter
import android.widget.EditText
import android.widget.HorizontalScrollView
import android.widget.LinearLayout
import android.widget.ListView
import android.widget.Spinner
import android.widget.TextView
import io.github.netwatch.android.R
import io.github.netwatch.android.core.ConnState
import io.github.netwatch.android.core.DayStats
import io.github.netwatch.android.core.Fmt
import io.github.netwatch.android.core.HistoryData
import io.github.netwatch.android.core.OutageInfo
import io.github.netwatch.android.core.Segment

/** Outage History — the primary screen, following the reference design. */
class HistoryScreen(a: MainActivity) : Screen(a) {
    override val title = "Outage History"
    private val periods = listOf("24h" to "24 Hours", "7d" to "7 Days", "30d" to "30 Days", "all" to "All Time")
    private val stateFilters: List<Pair<String, Set<ConnState>?>> = listOf(
        "All states" to null,
        "Outages only" to setOf(ConnState.NO_INTERNET, ConnState.NOT_CONNECTED),
        "Online" to setOf(ConnState.ONLINE),
        "No Internet" to setOf(ConnState.NO_INTERNET),
        "Not Connected" to setOf(ConnState.NOT_CONNECTED),
        "Not monitored" to setOf(ConnState.NOT_RUNNING, ConnState.PAUSED, ConnState.UNKNOWN),
    )
    var period = "all"; private set
    var data: HistoryData? = null; private set
    private var events: List<Segment> = emptyList()
    var filtered: List<Segment> = emptyList(); private set
    private var dayFilter: DayStats? = null
    private var selectedKey: String? = null
    private var gen = 0
    private var dirty = true

    private val segmented = Segmented(ui, periods, period) { k -> period = k; dayFilter = null; calendar.selected = null; refresh() }
    private val coverage = ui.muted("", 13f)
    private val grid = Grid(ui, 2, 16)
    private val tUptime = MetricTile(ui, R.drawable.ic_check, "Uptime", ui.p.online)
    private val tDown = MetricTile(ui, R.drawable.ic_warning, "Total downtime", ui.p.noInternet)
    private val tOutages = MetricTile(ui, R.drawable.ic_close, "Outages")
    private val tLongest = MetricTile(ui, R.drawable.ic_history, "Longest outage")
    private val calendar = CalendarView(a, ui, showLabels = false)
    private val calScroll = HorizontalScrollView(a).apply { addView(calendar); isHorizontalScrollBarEnabled = false }
    private val calInspect = ui.muted("Tap a day to show its events.", 12f)
    val timeline = TimelineView(a, ui)
    private val tlInspect = ui.muted("Drag across the timeline to inspect, tap to select an event.", 12f)
    private val countLabel = ui.muted("")
    private val search = EditText(a)
    private val stateSpinner = Spinner(a)
    private val dayChip = ui.hbox(6)
    private val dayChipLabel = ui.text("", 13f)
    private val emptyView = ui.vbox()
    private val sections = ui.vbox()
    private val logEmpty = ui.muted("", 14f)
    private val list = ListView(a)
    private val adapter = EventAdapter()

    override val view: View = list

    init {
        val header = ui.vbox().apply { setPadding(ui.dp(16), ui.dp(12), ui.dp(16), 0) }
        header.addView(HorizontalScrollView(a).apply { isHorizontalScrollBarEnabled = false; addView(segmented) })
        header.addView(ui.title("Period Summary"))
        header.addView(coverage)
        listOf(tUptime, tDown, tOutages, tLongest).forEach { grid.add(it) }
        header.addView(grid, lp(top = ui.dp(14)))
        // empty state
        emptyView.addView(ui.text("No history yet", 18f, bold = true).apply { setPadding(0, ui.dp(28), 0, ui.dp(6)) })
        emptyView.addView(ui.muted("NetWatch records connectivity while it is monitoring. Start monitoring and this screen fills in " +
            "with real measurements.", 14f))
        emptyView.addView(ui.button("Start monitoring", R.drawable.ic_play, primary = true) { app.requestStart(a) }.apply {
            layoutParams = lp(LinearLayout.LayoutParams.WRAP_CONTENT, top = ui.dp(14))
        })
        header.addView(emptyView)
        // calendar
        sections.addView(ui.title("Outage Calendar"))
        sections.addView(ui.hbox(center = false).apply {
            addView(WeekdayLabels(a, ui))
            addView(calScroll, lp(0, weight = 1f))
        }, lp(top = ui.dp(6)))
        sections.addView(legend(listOf("online" to "Online", "minor" to "Brief outages", "no_internet" to "No Internet",
            "major" to "Major outage", "not_connected" to "Not Connected", "unmonitored" to "Not monitored"), calendar = true), lp(top = ui.dp(8)))
        sections.addView(calInspect, lp(top = ui.dp(4)))
        calendar.onSelect = { d -> setDayFilter(d) }
        calendar.onInspect = { s -> calInspect.text = s ?: "Tap a day to show its events." }
        // timeline
        sections.addView(ui.title("Event Timeline"))
        sections.addView(timeline, lp(top = ui.dp(8)))
        sections.addView(legend(listOf("online" to "Online", "no_internet" to "No Internet (network connected)",
            "not_connected" to "Not Connected", "unknown" to "Unknown", "unmonitored" to "App Not Running / Paused"), calendar = false), lp(top = ui.dp(8)))
        sections.addView(tlInspect, lp(top = ui.dp(4)))
        timeline.onSelect = { seg -> selectSegment(seg) }
        timeline.onInspect = { s -> tlInspect.text = s ?: "Drag across the timeline to inspect, tap to select an event." }
        // event log header + filters
        val logHead = ui.hbox()
        logHead.addView(ui.title("Event Log"), lp(0, weight = 1f))
        logHead.addView(countLabel)
        sections.addView(logHead)
        search.hint = "Search events"
        search.setSingleLine()
        search.setTextColor(ui.p.fg)
        search.setHintTextColor(ui.p.muted)
        search.background = ui.rounded(withAlpha(ui.p.fg, 0x12), 10)
        search.setPadding(ui.dp(12), ui.dp(8), ui.dp(12), ui.dp(8))
        search.addTextChangedListener(object : TextWatcher {
            override fun beforeTextChanged(s: CharSequence?, st: Int, c: Int, after: Int) {}
            override fun onTextChanged(s: CharSequence?, st: Int, b: Int, c: Int) {}
            override fun afterTextChanged(s: Editable?) = applyFilters()
        })
        stateSpinner.adapter = object : ArrayAdapter<String>(a, android.R.layout.simple_spinner_item, stateFilters.map { it.first }) {
            override fun getView(position: Int, convertView: View?, parent: ViewGroup): View =
                (super.getView(position, convertView, parent) as TextView).apply { setTextColor(ui.p.fg) }
        }.also { it.setDropDownViewResource(android.R.layout.simple_spinner_dropdown_item) }
        stateSpinner.contentDescription = "Filter by state"
        stateSpinner.onItemSelectedListener = object : AdapterView.OnItemSelectedListener {
            override fun onItemSelected(parent: AdapterView<*>?, v: View?, pos: Int, id: Long) = applyFilters()
            override fun onNothingSelected(parent: AdapterView<*>?) {}
        }
        val filters = ui.hbox(8)
        filters.addView(search, lp(0, weight = 1f))
        filters.addView(stateSpinner)
        sections.addView(filters, lp(top = ui.dp(4)))
        dayChip.background = ui.rounded(withAlpha(ui.p.accent, 0x33), 99)
        dayChip.setPadding(ui.dp(12), ui.dp(2), ui.dp(2), ui.dp(2))
        dayChip.addView(dayChipLabel)
        dayChip.addView(ui.iconButton(R.drawable.ic_close, "Show all days") { setDayFilter(null) }.apply {
            layoutParams = LinearLayout.LayoutParams(ui.dp(32), ui.dp(32)); background = null
        })
        dayChip.visibility = View.GONE
        sections.addView(dayChip, lp(LinearLayout.LayoutParams.WRAP_CONTENT, top = ui.dp(8)))
        logEmpty.setPadding(0, ui.dp(18), 0, ui.dp(18))
        sections.addView(logEmpty)
        header.addView(sections)
        list.addHeaderView(header, null, false)
        list.adapter = adapter
        list.divider = null
        list.selector = ui.ripple(null)
        list.setOnItemClickListener { _, _, pos, _ ->
            val seg = adapter.getItem(pos - list.headerViewsCount) ?: return@setOnItemClickListener
            selectedKey = seg.key
            timeline.selectedKey = seg.key
            adapter.notifyDataSetChanged()
            EventDetail.show(a, seg, data?.outagesById?.get(seg.outageId))
        }
        list.setPadding(0, 0, 0, ui.dp(24))
        list.clipToPadding = false
    }

    private fun legend(items: List<Pair<String, String>>, calendar: Boolean): View {
        val flow = FlowLayout(a, ui.dp(16), ui.dp(6))
        for ((key, label) in items) {
            val row = ui.hbox(6)
            row.addView(object : View(a) {
                val paint = android.graphics.Paint(android.graphics.Paint.ANTI_ALIAS_FLAG)
                override fun onMeasure(w: Int, h: Int) = setMeasuredDimension(ui.dp(10), ui.dp(10))
                override fun onDraw(c: android.graphics.Canvas) {
                    val r = android.graphics.RectF(0f, 0f, width.toFloat(), height.toFloat())
                    when (key) {
                        "unmonitored" -> if (calendar) hatch(c, r, ui.p, paint, ui.dpf(3)) else {
                            paint.color = withAlpha(ui.p.fg, 0x80); c.drawRect(0f, r.centerY() - 2, r.right, r.centerY() + 2, paint)
                        }
                        "unknown" -> { paint.color = ui.p.unknown; c.drawCircle(r.centerX(), r.centerY(), r.width() / 2, paint) }
                        else -> {
                            paint.color = ui.p.category(key)
                            if (calendar) c.drawRoundRect(r, 3f, 3f, paint) else c.drawCircle(r.centerX(), r.centerY(), r.width() / 2, paint)
                        }
                    }
                }
            })
            row.addView(ui.text(label, 13f))
            flow.addView(row)
        }
        return flow
    }

    override fun onWidthChanged(widthDp: Int) = grid.setColumns(if (widthDp >= 600) 4 else 2)

    override fun actions(bar: LinearLayout) {
        bar.addView(ui.iconButton(R.drawable.ic_share, "Export or share a report for this period") {
            ReportExport.choose(a, period, null, null)
        })
        bar.addView(ui.iconButton(R.drawable.ic_delete, "Delete history") { deleteMenu() })
    }

    override fun onShow() { if (dirty) refresh() }
    override fun onHistoryChanged() { dirty = true; if (visible) refresh(soft = true) }
    private var tickCount = 0
    override fun onTick() { if (++tickCount % 30 == 0) refresh(soft = true) }

    fun refresh(soft: Boolean = false) {
        val token = ++gen
        dirty = false
        val now = System.currentTimeMillis() / 1000.0
        val p = period
        a.bg({ app.stats.history(p, now) }) { d -> if (token == gen) apply(d, now, soft) }
    }

    private fun apply(d: HistoryData, now: Double, soft: Boolean) {
        val prevKeys = events.map { it.key }
        data = d
        val s = d.summary
        emptyView.visibility = if (s.hasData) View.GONE else View.VISIBLE
        sections.visibility = if (s.hasData) View.VISIBLE else View.GONE
        grid.visibility = sections.visibility
        val extra = listOfNotNull(
            Fmt.durationLong(s.unknown).takeIf { s.unknown >= 60 }?.let { "$it unknown" },
            Fmt.durationLong(s.paused).takeIf { s.paused >= 60 }?.let { "$it paused" },
        )
        coverage.text = "Monitored for ${Fmt.durationLong(s.monitored)} out of ${Fmt.durationLong(s.rangeDuration)} " +
            "(${Fmt.percent(s.coveragePct, 1)})" + if (extra.isNotEmpty()) " · " + extra.joinToString(", ") else ""
        if (s.uptimePct == null) { tUptime.set("—", "no monitored time", true); tDown.set("—", muted = true) }
        else {
            tUptime.set(Fmt.percent(s.uptimePct), "of monitored time")
            val txt = android.text.SpannableString("${Fmt.duration(s.downtime)} (${Fmt.percent(s.downtimePct, 1)})")
            txt.setSpan(android.text.style.RelativeSizeSpan(0.7f), Fmt.duration(s.downtime).length, txt.length, 0)
            tDown.set(txt, "of monitored time offline")
        }
        tOutages.set(s.outageCount.toString(), "distinct outages")
        val lo = s.longestOutage
        if (lo == null) tLongest.set("—", "no outages", true)
        else tLongest.set(Fmt.duration(lo.durationInRange),
            (if (lo.ongoing) "ongoing" else if (lo.endObserved) "completed" else "recovery not observed") +
                if (!lo.startObserved) ", start not observed" else "")
        calendar.setDays(d.days)
        calScroll.post { calScroll.fullScroll(View.FOCUS_RIGHT) }
        timeline.setData(d.segments, s.rangeStart, s.rangeEnd, now)
        timeline.selectedKey = selectedKey
        events = d.segments.asReversed()
        if (soft && events.map { it.key } == prevKeys) adapter.notifyDataSetChanged() else applyFilters()
    }

    private fun matches(seg: Segment, states: Set<ConnState>?, text: String): Boolean {
        if (states != null && seg.state !in states) return false
        dayFilter?.let { d ->
            val ds = Fmt.dayStart(d.day)
            val de = Fmt.dayStart(d.day.plusDays(1))
            if (seg.end <= ds || seg.start >= de) return false
        }
        if (text.isNotBlank()) {
            val now = System.currentTimeMillis() / 1000.0
            val hay = listOfNotNull(seg.state.label, seg.targets, seg.reason, seg.local?.describe(), Fmt.timestamp(seg.start, now),
                Fmt.timestamp(seg.end, now), seg.outageId?.let { "outage #$it" }).joinToString(" ").lowercase()
            if (text.lowercase() !in hay) return false
        }
        return true
    }

    fun applyFilters() {
        val states = stateFilters.getOrNull(stateSpinner.selectedItemPosition)?.second
        val text = search.text?.toString()?.trim() ?: ""
        filtered = events.filter { matches(it, states, text) }
        adapter.notifyDataSetChanged()
        countLabel.text = if (filtered.size != events.size) "${filtered.size} of ${events.size} events" else "${events.size} events"
        logEmpty.text = if (events.isEmpty()) "No events recorded." else "No events match the current filters."
        logEmpty.visibility = if (filtered.isEmpty()) View.VISIBLE else View.GONE
    }

    private fun setDayFilter(d: DayStats?) {
        dayFilter = d
        if (d == null) calendar.selected = null
        dayChipLabel.text = d?.let { Fmt.dateLong(it.day) } ?: ""
        dayChip.visibility = if (d == null) View.GONE else View.VISIBLE
        applyFilters()
    }

    /** Select the event for a timeline segment, clearing filters if they hide it. */
    fun selectSegment(seg: Segment) {
        selectedKey = seg.key
        timeline.selectedKey = seg.key
        var idx = filtered.indexOfFirst { it.key == seg.key }
        if (idx < 0) {
            search.setText("")
            stateSpinner.setSelection(0)
            setDayFilter(null)
            idx = filtered.indexOfFirst { it.key == seg.key }
        }
        adapter.notifyDataSetChanged()
        if (idx >= 0) list.post { list.setSelectionFromTop(idx + list.headerViewsCount, ui.dp(80)) }
    }

    private fun deleteMenu() {
        val d = data ?: return
        val options = arrayOf("Delete selected event…", "Delete history in this period…", "Clear all history…")
        AlertDialog.Builder(a).setTitle("Delete history").setItems(options) { _, which ->
            when (which) {
                0 -> {
                    val seg = filtered.firstOrNull { it.key == selectedKey }
                    when {
                        seg == null -> a.toast("Select an event in the Event Log first.")
                        seg.synthetic || seg.ongoing || seg.stateId == null -> a.toast("Only completed, recorded events can be deleted.")
                        else -> a.confirm("Delete this event?", "The ${seg.state.label} period from ${Fmt.timestamp(seg.start, seg.end)} and its " +
                            "measurements will be permanently removed. This cannot be undone.", "Delete") {
                            a.bg({ app.repo.deleteState(seg.stateId) }) { a.toast("Event deleted"); app.notifyHistoryChanged() }
                        }
                    }
                }
                1 -> a.confirm("Delete history in this period?", "All completed records that lie entirely within the selected period will be " +
                    "permanently deleted, including measurements and outages. The current state is kept. Consider exporting a report first.",
                    "Delete period") {
                    val (s, e) = d.summary.rangeStart to d.summary.rangeEnd
                    a.bg({ app.repo.deleteRange(s, e) }) { r -> a.toast("Deleted ${r.states} events and ${r.outages} outages"); app.notifyHistoryChanged() }
                }
                2 -> a.confirm("Clear all history?", "Every recorded event, outage, measurement and session will be permanently deleted. " +
                    "Monitoring continues and starts a fresh record now. This cannot be undone.", "Clear all") {
                    val keep = app.engine.sessionId
                    a.bg({ app.repo.deleteAll(keep, System.currentTimeMillis() / 1000.0) }) { a.toast("All history cleared"); app.notifyHistoryChanged() }
                }
            }
        }.show()
    }

    private inner class EventAdapter : BaseAdapter() {
        override fun getCount() = filtered.size
        override fun getItem(position: Int): Segment? = filtered.getOrNull(position)
        override fun getItemId(position: Int) = position.toLong()

        override fun getView(position: Int, convertView: View?, parent: ViewGroup?): View {
            val holder = (convertView?.tag as? RowHolder) ?: RowHolder()
            holder.bind(filtered[position], data?.outagesById?.get(filtered[position].outageId))
            return holder.root
        }
    }

    private inner class RowHolder {
        val dot = StateDot(a, ui.p, ConnState.UNKNOWN)
        val title = ui.text("", 15f, lines = 1)
        val sub = ui.muted("", 13f, 1)
        val detail = ui.muted("", 12f, 1).apply { alpha = 0.8f }
        val badge = ui.badge("", ui.p.noInternet)
        val duration = ui.text("", 16f, lines = 1).apply { fontFeatureSettings = "tnum" }
        val root: LinearLayout = ui.hbox(14).apply {
            setPadding(ui.dp(18), ui.dp(10), ui.dp(18), ui.dp(10))
            addView(LinearLayout(a).apply { gravity = android.view.Gravity.CENTER; addView(dot) }, LinearLayout.LayoutParams(ui.dp(16), ViewGroup.LayoutParams.WRAP_CONTENT))
            val texts = ui.vbox()
            texts.addView(title); texts.addView(sub); texts.addView(detail)
            texts.addView(badge, lp(LinearLayout.LayoutParams.WRAP_CONTENT, top = ui.dp(3)))
            addView(texts, lp(0, weight = 1f))
            addView(duration)
            tag = this@RowHolder
        }

        fun bind(seg: Segment, outage: OutageInfo?) {
            val now = System.currentTimeMillis() / 1000.0
            dot.state = seg.state
            val end = if (seg.ongoing) "now" else Fmt.timestamp(seg.end, now)
            title.text = "${Fmt.timestamp(seg.start, now)} – $end"
            val targets = seg.targets?.split(", ")
            sub.text = seg.state.shortLabel + (targets?.let { " · " + if (seg.state == ConnState.ONLINE) it.first() else it.take(3).joinToString(", ") } ?: "")
            val d = seg.local?.describe()?.takeIf { it.isNotBlank() }
                ?: if (seg.synthetic) "No monitoring records for this period"
                else seg.reason.takeIf { seg.state == ConnState.UNKNOWN || seg.state == ConnState.PAUSED }
            detail.text = d ?: ""
            detail.visibility = if (d.isNullOrBlank()) View.GONE else View.VISIBLE
            if (outage != null) {
                badge.visibility = View.VISIBLE
                badge.text = "Outage #${outage.id}" + when {
                    !outage.startObserved -> " · start not observed"
                    !outage.endObserved && !outage.ongoing -> " · recovery not observed"
                    else -> ""
                }
            } else badge.visibility = View.GONE
            duration.text = Fmt.duration(seg.duration)
            root.setBackgroundColor(if (seg.key == selectedKey) withAlpha(ui.p.accent, 0x30) else 0)
            root.contentDescription = "${seg.state.label}, ${title.text}, ${Fmt.duration(seg.duration, true)}" +
                (outage?.let { ", outage ${it.id}" } ?: "") + ". Double-tap for details."
        }
    }
}

/** Simple wrapping row layout for legends. */
class FlowLayout(context: android.content.Context, private val hGap: Int, private val vGap: Int) : ViewGroup(context) {
    override fun onMeasure(widthMeasureSpec: Int, heightMeasureSpec: Int) {
        val maxW = MeasureSpec.getSize(widthMeasureSpec)
        var x = 0; var y = 0; var rowH = 0
        for (i in 0 until childCount) {
            val c = getChildAt(i)
            measureChild(c, MeasureSpec.makeMeasureSpec(maxW, MeasureSpec.AT_MOST), heightMeasureSpec)
            if (x > 0 && x + c.measuredWidth > maxW) { x = 0; y += rowH + vGap; rowH = 0 }
            x += c.measuredWidth + hGap
            rowH = maxOf(rowH, c.measuredHeight)
        }
        setMeasuredDimension(maxW, y + rowH)
    }

    override fun onLayout(changed: Boolean, l: Int, t: Int, r: Int, b: Int) {
        var x = 0; var y = 0; var rowH = 0
        val maxW = r - l
        for (i in 0 until childCount) {
            val c = getChildAt(i)
            if (x > 0 && x + c.measuredWidth > maxW) { x = 0; y += rowH + vGap; rowH = 0 }
            c.layout(x, y, x + c.measuredWidth, y + c.measuredHeight)
            x += c.measuredWidth + hGap
            rowH = maxOf(rowH, c.measuredHeight)
        }
    }
}
