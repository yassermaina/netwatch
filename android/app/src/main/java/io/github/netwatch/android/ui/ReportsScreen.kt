package io.github.netwatch.android.ui

import android.app.DatePickerDialog
import android.graphics.Typeface
import android.view.View
import android.widget.HorizontalScrollView
import android.widget.ScrollView
import io.github.netwatch.android.R
import io.github.netwatch.android.core.Fmt
import io.github.netwatch.android.core.Reports
import java.time.LocalDate

/** Choose a range and format, preview, then save or share a report. */
class ReportsScreen(a: MainActivity) : Screen(a) {
    override val title = "Reports"
    private var period = "7d"
    private var format = "html"
    private var from: LocalDate = LocalDate.now().minusDays(7)
    private var to: LocalDate = LocalDate.now()
    private var gen = 0
    private val customRow = ui.hbox(8)
    private val fromBtn = ui.button("") { pick(true) }
    private val toBtn = ui.button("") { pick(false) }
    private val error = ui.text("", 13f, ui.p.error)
    private val preview = ui.text("", 11.5f).apply { typeface = Typeface.MONOSPACE; setTextIsSelectable(true) }

    override val view: View = ScrollView(a).apply {
        val col = ui.vbox().apply { setPadding(ui.dp(16), ui.dp(12), ui.dp(16), ui.dp(28)) }
        col.addView(ui.muted("Reports include coverage, uptime, downtime, outages, the event timeline, monitoring interruptions " +
            "and the limitations of the collected data.", 13f))
        col.addView(ui.title("Time range"))
        col.addView(HorizontalScrollView(a).apply {
            isHorizontalScrollBarEnabled = false
            addView(Segmented(ui, listOf("24h" to "24 Hours", "7d" to "7 Days", "30d" to "30 Days", "all" to "All", "custom" to "Custom"), period) {
                period = it; customRow.visibility = if (it == "custom") View.VISIBLE else View.GONE; refresh()
            })
        })
        customRow.addView(ui.muted("From")); customRow.addView(fromBtn); customRow.addView(ui.muted("to")); customRow.addView(toBtn)
        customRow.visibility = View.GONE
        col.addView(customRow, lp(top = ui.dp(10)))
        col.addView(ui.title("Format"))
        col.addView(Segmented(ui, listOf("html" to "HTML", "csv" to "CSV", "txt" to "Text"), format) { format = it })
        col.addView(error, lp(top = ui.dp(8)))
        val buttons = ui.hbox(8)
        buttons.addView(ui.button("Save to file…", R.drawable.ic_save, primary = true) { if (valid()) ReportExport.save(a, format, period, custom(), null) })
        buttons.addView(ui.button("Share…", R.drawable.ic_share) { if (valid()) ReportExport.share(a, format, period, custom(), null) })
        col.addView(buttons, lp(top = ui.dp(4)))
        col.addView(ui.title("Preview"))
        col.addView(ui.card(HorizontalScrollView(a).apply { addView(preview) }, 12), lp(top = ui.dp(6)))
        addView(col)
        syncDates()
    }

    private fun syncDates() {
        fromBtn.text = Fmt.dateLong(from).substringAfter(", ")
        toBtn.text = Fmt.dateLong(to).substringAfter(", ")
    }

    private fun pick(isFrom: Boolean) {
        val d = if (isFrom) from else to
        DatePickerDialog(a, { _, y, m, day ->
            val v = LocalDate.of(y, m + 1, day)
            if (isFrom) from = v else to = v
            syncDates(); refresh()
        }, d.year, d.monthValue - 1, d.dayOfMonth).show()
    }

    private fun custom(): Pair<Double, Double>? = if (period != "custom") null
    else Fmt.dayStart(from) to minOf(Fmt.dayStart(to.plusDays(1)), System.currentTimeMillis() / 1000.0)

    private fun valid(): Boolean {
        val c = custom()
        val msg = if (c != null && c.second <= c.first) "The end date must be on or after the start date, and not in the future." else ""
        error.text = msg
        error.visibility = if (msg.isBlank()) View.GONE else View.VISIBLE
        return msg.isBlank()
    }

    override fun onShow() = refresh()

    private fun refresh() {
        if (!valid()) return
        val token = ++gen
        val p = period
        val c = custom()
        a.bg({
            val text = Reports.txt(Reports.build(app.repo, app.stats, app.settings, p, c, null, System.currentTimeMillis() / 1000.0,
                "${android.os.Build.MANUFACTURER} ${android.os.Build.MODEL}"))
            text.lines().take(300).joinToString("\n")
        }) { if (token == gen) preview.text = it }
    }
}
