package io.github.netwatch.android.ui

import android.content.Context
import android.content.res.ColorStateList
import android.graphics.Canvas
import android.graphics.Paint
import android.graphics.Typeface
import android.graphics.drawable.GradientDrawable
import android.graphics.drawable.RippleDrawable
import android.text.TextUtils
import android.util.TypedValue
import android.view.Gravity
import android.view.View
import android.view.ViewGroup
import android.widget.Button
import android.widget.ImageView
import android.widget.LinearLayout
import android.widget.Switch
import android.widget.TextView
import io.github.netwatch.android.core.ConnState

/** Tiny view-building DSL so screens stay readable without XML layouts. */
class Ui(val context: Context, val p: Palette) {
    fun dp(v: Number): Int = TypedValue.applyDimension(TypedValue.COMPLEX_UNIT_DIP, v.toFloat(), context.resources.displayMetrics).toInt()
    fun dpf(v: Number): Float = TypedValue.applyDimension(TypedValue.COMPLEX_UNIT_DIP, v.toFloat(), context.resources.displayMetrics)

    fun text(s: CharSequence = "", size: Float = 15f, color: Int = p.fg, bold: Boolean = false, lines: Int = 0): TextView =
        TextView(context).apply {
            text = s
            setTextSize(TypedValue.COMPLEX_UNIT_SP, size)
            setTextColor(color)
            if (bold) typeface = Typeface.create(Typeface.DEFAULT, Typeface.BOLD)
            if (lines > 0) { maxLines = lines; ellipsize = TextUtils.TruncateAt.END }
        }

    fun muted(s: CharSequence = "", size: Float = 13f, lines: Int = 0) = text(s, size, p.muted, lines = lines)
    fun title(s: String) = text(s, 20f, bold = true).apply { setPadding(0, dp(22), 0, dp(4)); accessibilityHeading() }

    fun vbox(padding: Int = 0, spacing: Int = 0): LinearLayout = LinearLayout(context).apply {
        orientation = LinearLayout.VERTICAL
        if (padding > 0) setPadding(dp(padding), dp(padding), dp(padding), dp(padding))
        if (spacing > 0) { showDividers = LinearLayout.SHOW_DIVIDER_MIDDLE; dividerDrawable = spacer(spacing) }
    }

    fun hbox(spacing: Int = 0, center: Boolean = true): LinearLayout = LinearLayout(context).apply {
        orientation = LinearLayout.HORIZONTAL
        if (center) gravity = Gravity.CENTER_VERTICAL
        if (spacing > 0) { showDividers = LinearLayout.SHOW_DIVIDER_MIDDLE; dividerDrawable = spacer(spacing) }
    }

    private fun spacer(size: Int) = GradientDrawable().apply { setSize(dp(size), dp(size)); setColor(0) }

    fun rounded(color: Int, radius: Number = 12, stroke: Int = 0): GradientDrawable = GradientDrawable().apply {
        cornerRadius = dpf(radius)
        setColor(color)
        if (stroke != 0) setStroke(dp(1), stroke)
    }

    fun ripple(bg: GradientDrawable?): RippleDrawable =
        RippleDrawable(ColorStateList.valueOf(withAlpha(p.fg, 0x22)), bg, bg ?: rounded(0xFFFFFFFF.toInt()))

    fun card(child: View? = null, padding: Int = 14): LinearLayout = vbox().apply {
        background = rounded(p.surface, 14, if (p.dark) 0x0DFFFFFF else 0x14000000)
        setPadding(dp(padding), dp(padding), dp(padding), dp(padding))
        child?.let { addView(it) }
    }

    fun button(label: String, icon: Int? = null, primary: Boolean = false, onClick: () -> Unit): Button = Button(context).apply {
        text = label
        isAllCaps = false
        setTextColor(if (primary) p.onAccent else p.fg)
        setTextSize(TypedValue.COMPLEX_UNIT_SP, 14f)
        background = ripple(rounded(if (primary) p.accent else withAlpha(p.fg, 0x14), 10))
        minHeight = dp(40); minimumHeight = dp(40)
        setPadding(dp(14), 0, dp(14), 0)
        icon?.let {
            val d = context.getDrawable(it)!!.mutate()
            d.setTint(if (primary) p.onAccent else p.fg)
            d.setBounds(0, 0, dp(18), dp(18))
            setCompoundDrawablesRelative(d, null, null, null)
            compoundDrawablePadding = dp(6)
        }
        stateListAnimator = null
        setOnClickListener { onClick() }
    }

    fun iconButton(icon: Int, description: String, onClick: () -> Unit): ImageView = ImageView(context).apply {
        setImageResource(icon)
        imageTintList = ColorStateList.valueOf(p.fg)
        contentDescription = description
        tooltipText = description
        background = ripple(rounded(withAlpha(p.fg, 0x12), 10))
        setPadding(dp(9), dp(9), dp(9), dp(9))
        layoutParams = LinearLayout.LayoutParams(dp(42), dp(42))
        setOnClickListener { onClick() }
    }

    fun switch(label: String, sub: String? = null, checked: Boolean, onChange: (Boolean) -> Unit): View {
        val row = hbox(12)
        val texts = vbox()
        texts.addView(text(label))
        sub?.let { texts.addView(muted(it, 12f)) }
        row.addView(texts, LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f))
        val sw = Switch(context).apply { isChecked = checked; setOnCheckedChangeListener { _, v -> onChange(v) }; contentDescription = label }
        row.addView(sw)
        row.setPadding(0, dp(8), 0, dp(8))
        row.setOnClickListener { sw.toggle() }
        return row
    }

    /** Label + [−] value [+] stepper for integer settings. */
    fun stepper(label: String, sub: String?, value: Int, min: Int, max: Int, step: Int = 1, unit: String = "", onChange: (Int) -> Unit): View {
        var current = value
        val row = hbox(8)
        val texts = vbox()
        texts.addView(text(label))
        sub?.let { texts.addView(muted(it, 12f)) }
        row.addView(texts, LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f))
        val valueView = text("$current$unit", 15f, bold = true).apply { minWidth = dp(44); gravity = Gravity.CENTER }
        fun set(v: Int) {
            current = v.coerceIn(min, max)
            valueView.text = "$current$unit"
            row.contentDescription = "$label: $current$unit"
            onChange(current)
        }
        row.addView(iconButton(io.github.netwatch.android.R.drawable.ic_close, "Decrease $label") { set(current - step) }.apply {
            setImageDrawable(MinusDrawable(p.fg, dpf(2)))
        })
        row.addView(valueView)
        row.addView(iconButton(io.github.netwatch.android.R.drawable.ic_add, "Increase $label") { set(current + step) })
        row.setPadding(0, dp(6), 0, dp(6))
        row.contentDescription = "$label: $current$unit"
        return row
    }

    fun divider(): View = View(context).apply {
        setBackgroundColor(p.line)
        layoutParams = LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, dp(1))
    }

    fun badge(s: String, color: Int): TextView = text(s, 11f, color, bold = true).apply {
        background = rounded(withAlpha(color, 0x2E), 6)
        setPadding(dp(7), dp(1), dp(7), dp(1))
    }

    fun pill(s: String, color: Int): TextView = text(s, 12f, color, bold = true).apply {
        background = rounded(withAlpha(color, 0x2E), 99)
        setPadding(dp(10), dp(2), dp(10), dp(2))
    }
}

fun withAlpha(color: Int, alpha: Int): Int = (color and 0x00FFFFFF) or (alpha shl 24)

fun View.accessibilityHeading() { if (android.os.Build.VERSION.SDK_INT >= 28) isAccessibilityHeading = true }

fun lp(w: Int = ViewGroup.LayoutParams.MATCH_PARENT, h: Int = ViewGroup.LayoutParams.WRAP_CONTENT, weight: Float = 0f,
       top: Int = 0, bottom: Int = 0) = LinearLayout.LayoutParams(w, h, weight).apply { topMargin = top; bottomMargin = bottom }

private class MinusDrawable(private val color: Int, private val stroke: Float) : android.graphics.drawable.Drawable() {
    private val paint = Paint(Paint.ANTI_ALIAS_FLAG).apply { this.color = color; strokeWidth = stroke; strokeCap = Paint.Cap.ROUND }
    override fun draw(c: Canvas) { val b = bounds; c.drawLine(b.left + b.width() * 0.22f, b.exactCenterY(), b.right - b.width() * 0.22f, b.exactCenterY(), paint) }
    override fun setAlpha(alpha: Int) {}
    override fun setColorFilter(cf: android.graphics.ColorFilter?) {}
    @Deprecated("Deprecated in Java") override fun getOpacity() = android.graphics.PixelFormat.TRANSLUCENT
    override fun getIntrinsicWidth() = 48
    override fun getIntrinsicHeight() = 48
}

/** Coloured state indicator; a dash for unmonitored periods (as in the reference design). */
class StateDot(context: Context, private val p: Palette, state: ConnState) : View(context) {
    var state: ConnState = state
        set(v) { field = v; contentDescription = v.label; invalidate() }
    private val paint = Paint(Paint.ANTI_ALIAS_FLAG)
    init { contentDescription = state.label }
    override fun onMeasure(w: Int, h: Int) { val s = (12 * resources.displayMetrics.density).toInt(); setMeasuredDimension(s, s) }
    override fun onDraw(c: Canvas) {
        val r = width / 2f
        if (state == ConnState.NOT_RUNNING || state == ConnState.PAUSED) {
            paint.color = withAlpha(p.fg, 0x80)
            c.drawRoundRect(0f, r - 1.5f * resources.displayMetrics.density, width.toFloat(), r + 1.5f * resources.displayMetrics.density, 2f, 2f, paint)
        } else {
            paint.color = p.state(state)
            c.drawCircle(r, r, r, paint)
        }
    }
}

/** Segmented control (period selector) with the selected option highlighted in the accent colour. */
class Segmented(val ui: Ui, options: List<Pair<String, String>>, selected: String, private val onSelect: (String) -> Unit) : LinearLayout(ui.context) {
    private val views = mutableMapOf<String, TextView>()
    var selected: String = selected; private set

    init {
        orientation = HORIZONTAL
        background = ui.rounded(withAlpha(ui.p.fg, 0x12), 10)
        setPadding(ui.dp(3), ui.dp(3), ui.dp(3), ui.dp(3))
        for ((key, label) in options) {
            val t = ui.text(label, 14f, bold = true).apply {
                gravity = Gravity.CENTER
                setPadding(ui.dp(12), ui.dp(7), ui.dp(12), ui.dp(7))
                setOnClickListener { select(key, notify = true) }
                contentDescription = label
            }
            views[key] = t
            addView(t, LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f))
        }
        select(selected, notify = false)
    }

    fun select(key: String, notify: Boolean) {
        selected = key
        for ((k, v) in views) {
            val on = k == key
            v.background = if (on) ui.rounded(ui.p.accent, 8) else null
            v.setTextColor(if (on) ui.p.onAccent else ui.p.fg)
            v.isSelected = on
        }
        if (notify) onSelect(key)
    }
}

/** Icon + label above a large value (Period Summary style). */
class MetricTile(val ui: Ui, icon: Int, private val label: String, private val valueColor: Int? = null, small: Boolean = false) : LinearLayout(ui.context) {
    private val value: TextView
    private val note: TextView

    init {
        orientation = VERTICAL
        val head = ui.hbox(6)
        head.addView(ImageView(context).apply {
            setImageResource(icon); imageTintList = ColorStateList.valueOf(ui.p.muted)
            layoutParams = LayoutParams(ui.dp(16), ui.dp(16))
        })
        head.addView(ui.muted(label, 13f, 1))
        addView(head)
        value = ui.text("—", if (small) 20f else 26f, valueColor ?: ui.p.fg, bold = true, lines = 1)
        value.fontFeatureSettings = "tnum"
        addView(value, lp(top = ui.dp(2)))
        note = ui.muted("", 12f, 1)
        addView(note)
    }

    fun set(v: CharSequence, n: String = "", muted: Boolean = false) {
        value.text = v
        value.setTextColor(if (muted) ui.p.muted else valueColor ?: ui.p.fg)
        note.text = n
        note.visibility = if (n.isBlank()) GONE else VISIBLE
        contentDescription = "$label: $v. $n"
    }
}

/** Lays out children in rows of [columns] equal-width cells (responsive metric grids). */
class Grid(val ui: Ui, private var columns: Int, private val gap: Int = 12) : LinearLayout(ui.context) {
    private val cells = mutableListOf<View>()
    init { orientation = VERTICAL }

    fun add(v: View) { cells += v; relayout() }

    fun setColumns(n: Int) { if (n != columns) { columns = n; relayout() } }

    private fun relayout() {
        cells.forEach { (it.parent as? ViewGroup)?.removeView(it) }
        removeAllViews()
        cells.chunked(columns).forEachIndexed { i, chunk ->
            val row = LinearLayout(context).apply { orientation = HORIZONTAL }
            chunk.forEachIndexed { j, v ->
                row.addView(v, LayoutParams(0, ViewGroup.LayoutParams.MATCH_PARENT, 1f).apply { if (j > 0) marginStart = ui.dp(gap) })
            }
            repeat(columns - chunk.size) { row.addView(View(context), LayoutParams(0, 0, 1f).apply { marginStart = ui.dp(gap) }) }
            addView(row, lp(top = if (i > 0) ui.dp(gap) else 0))
        }
    }
}
