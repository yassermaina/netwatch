package io.github.netwatch.android.ui

import android.app.AlertDialog
import android.content.Intent
import android.net.Uri
import android.os.PowerManager
import android.provider.Settings
import android.view.View
import android.widget.ArrayAdapter
import android.widget.EditText
import android.widget.FrameLayout
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.Spinner
import io.github.netwatch.android.R
import io.github.netwatch.android.core.Defaults
import io.github.netwatch.android.core.DuplicateTargetException
import io.github.netwatch.android.core.SettingsValidator
import io.github.netwatch.android.core.Target

/** Settings with explicit Save/Revert, validation messages and a targets editor. */
class SettingsScreen(a: MainActivity) : Screen(a) {
    override val title = "Settings"
    private val pending = mutableMapOf<String, Any>()
    private val form = ui.vbox().apply { setPadding(ui.dp(16), ui.dp(4), ui.dp(16), ui.dp(96)) }
    private val bar = ui.hbox(8)
    private val barText = ui.text("", 13f)

    override val view: View = FrameLayout(a).apply {
        addView(ScrollView(a).apply { addView(form) })
        bar.background = ui.rounded(ui.p.surface, 14, ui.p.line)
        bar.setPadding(ui.dp(14), ui.dp(8), ui.dp(8), ui.dp(8))
        bar.addView(barText, lp(0, weight = 1f))
        bar.addView(ui.button("Revert") { pending.clear(); build() })
        bar.addView(ui.button("Save", primary = true) { save() })
        bar.elevation = ui.dpf(6)
        addView(bar, FrameLayout.LayoutParams(FrameLayout.LayoutParams.MATCH_PARENT, FrameLayout.LayoutParams.WRAP_CONTENT,
            android.view.Gravity.BOTTOM).apply { setMargins(ui.dp(12), 0, ui.dp(12), ui.dp(12)) })
        bar.visibility = View.GONE
    }

    fun hasUnsaved() = pending.any { (k, v) -> app.settings.all()[k] != v }

    fun confirmDiscard(then: () -> Unit) {
        AlertDialog.Builder(a).setTitle("Save changes?").setMessage("Some settings have not been saved.")
            .setNegativeButton("Discard") { _, _ -> pending.clear(); then() }
            .setNeutralButton("Cancel", null)
            .setPositiveButton("Save") { _, _ -> if (save()) then() }
            .show()
    }

    override fun onShow() { if (!hasUnsaved()) pending.clear(); build() }

    private fun value(key: String): Any = pending[key] ?: app.settings.all().getValue(key)
    private fun int(key: String) = (value(key) as Number).toInt()
    private fun bool(key: String) = value(key) as Boolean

    private fun changed(key: String, v: Any) {
        pending[key] = v
        updateBar()
    }

    private fun updateBar() {
        val changes = pending.filter { (k, v) -> app.settings.all()[k] != v }
        val errors = SettingsValidator.validate(app.settings.all() + pending)
        bar.visibility = if (changes.isNotEmpty() || errors.isNotEmpty()) View.VISIBLE else View.GONE
        barText.text = errors.firstOrNull()?.message ?: "${changes.size} unsaved change${if (changes.size == 1) "" else "s"}"
        barText.setTextColor(if (errors.isEmpty()) ui.p.fg else ui.p.error)
    }

    private fun save(): Boolean {
        val changes = pending.filter { (k, v) -> app.settings.all()[k] != v }
        if (changes.isEmpty()) { pending.clear(); updateBar(); return true }
        val errors = app.settings.update(changes)
        if (errors.isNotEmpty()) { updateBar(); return false }
        pending.clear()
        if ("muted" in changes || "start_muted" in changes) app.alerts.setMuted(app.settings.bool("muted"))
        if ("retention_days" in changes) app.prune()
        app.repo.logEvent("settings_changed", "Settings changed: ${changes.keys.joinToString()}", app.engine.sessionId)
        a.toast("Settings saved")
        if ("theme" in changes) a.rebuild() else build()
        return true
    }

    private fun section(title: String, note: String? = null): LinearLayout {
        form.addView(ui.title(title))
        note?.let { form.addView(ui.muted(it, 12f)) }
        val card = ui.card(padding = 12)
        form.addView(card, lp(top = ui.dp(8)))
        return card
    }

    private fun build() {
        form.removeAllViews()
        var c = section("Monitoring", "A failure is confirmed only after every target fails for the failure threshold.")
        c.addView(ui.stepper("Check interval", "Seconds between check rounds", int("interval_seconds"), 2, 3600, 1, "s") { changed("interval_seconds", it) })
        c.addView(ui.stepper("Timeout", "Must be shorter than the interval", int("timeout_seconds"), 1, 60, 1, "s") { changed("timeout_seconds", it) })
        c.addView(ui.stepper("Failure threshold", "Failed rounds before an outage", int("failure_threshold"), 1, 20) { changed("failure_threshold", it) })
        c.addView(ui.stepper("Recovery threshold", "Successful rounds before recovery", int("recovery_threshold"), 1, 20) { changed("recovery_threshold", it) })
        c.addView(ui.stepper("High latency", "Shown as degraded on Performance", int("latency_warning_ms"), 10, 10000, 10, " ms") { changed("latency_warning_ms", it) })
        c.addView(ui.switch("Start monitoring when the app opens", null, bool("start_monitoring_on_launch")) { changed("start_monitoring_on_launch", it) })

        c = section("Targets", "Connectivity is Online while at least one enabled target responds. Changes apply immediately.")
        app.repo.listTargets().forEachIndexed { i, t ->
            if (i > 0) c.addView(ui.divider())
            val row = ui.hbox(4)
            val texts = ui.vbox()
            texts.addView(ui.text(t.name, lines = 1))
            texts.addView(ui.muted("${t.address} · ${Defaults.checkTypes[t.checkType] ?: t.checkType}", 12f, 1))
            row.addView(texts, lp(0, weight = 1f))
            row.addView(android.widget.Switch(a).apply {
                isChecked = t.enabled
                contentDescription = "Enable ${t.name}"
                setOnCheckedChangeListener { sw, v ->
                    if (!v && app.repo.listTargets(true).none { it.id != t.id }) { sw.isChecked = true; a.toast("At least one target must stay enabled.") }
                    else { app.repo.updateTarget(t.copy(enabled = v)); app.engine.reload() }
                }
            })
            row.addView(ui.iconButton(R.drawable.ic_edit, "Edit ${t.name}") { editTarget(t) })
            row.addView(ui.iconButton(R.drawable.ic_delete, "Remove ${t.name}") {
                if (t.enabled && app.repo.listTargets(true).none { it.id != t.id }) a.toast("Add or enable another target first.")
                else a.confirm("Remove ${t.name}?", "It stops being monitored. Its past measurements stay in the history.", "Remove") {
                    app.repo.deleteTarget(t.id!!); app.engine.reload(); build()
                }
            })
            row.setPadding(0, ui.dp(4), 0, ui.dp(4))
            c.addView(row)
        }
        c.addView(ui.button("Add target", R.drawable.ic_add) { editTarget(null) }.apply { layoutParams = lp(LinearLayout.LayoutParams.WRAP_CONTENT, top = ui.dp(8)) })

        c = section("Sound alerts", "A soft heartbeat while an outage lasts. Muting never affects monitoring or the history.")
        c.addView(ui.switch("Sound alerts", null, bool("sound_enabled")) { changed("sound_enabled", it) })
        c.addView(ui.stepper("Volume", null, int("sound_volume"), 0, 100, 5, "%") { changed("sound_volume", it) })
        c.addView(ui.button("Test sound", R.drawable.ic_volume) { app.sound.play("outage", int("sound_volume")) }.apply { layoutParams = lp(LinearLayout.LayoutParams.WRAP_CONTENT) })
        c.addView(ui.switch("Repeat during outages", null, bool("sound_repeat")) { changed("sound_repeat", it) })
        c.addView(ui.stepper("Repeat every", null, int("sound_repeat_seconds"), 3, 3600, 1, "s") { changed("sound_repeat_seconds", it) })
        c.addView(ui.switch("Recovery sound", null, bool("sound_recovery")) { changed("sound_recovery", it) })
        c.addView(ui.switch("Resume alert when unmuting during an outage", null, bool("resume_alert_on_unmute")) { changed("resume_alert_on_unmute", it) })
        c.addView(ui.switch("Vibrate on outage", null, bool("vibrate")) { changed("vibrate", it) })
        c.addView(ui.switch("Start muted", null, bool("start_muted")) { changed("start_muted", it) })

        c = section("Notifications")
        c.addView(ui.switch("When an outage is detected", null, bool("notify_outage")) { changed("notify_outage", it) })
        c.addView(ui.switch("When connectivity recovers", null, bool("notify_recovery")) { changed("notify_recovery", it) })
        c.addView(ui.switch("When a monitoring problem occurs", null, bool("notify_errors")) { changed("notify_errors", it) })
        c.addView(ui.button("System notification settings") {
            a.startActivity(Intent(Settings.ACTION_APP_NOTIFICATION_SETTINGS).putExtra(Settings.EXTRA_APP_PACKAGE, a.packageName))
        }.apply { layoutParams = lp(LinearLayout.LayoutParams.WRAP_CONTENT, top = ui.dp(6)) })

        c = section("Appearance")
        c.addView(Segmented(ui, listOf("system" to "System", "light" to "Light", "dark" to "Dark"), value("theme") as String) { changed("theme", it) })

        val pm = a.getSystemService(PowerManager::class.java)
        val exempt = pm.isIgnoringBatteryOptimizations(a.packageName)
        c = section("Background", "Monitoring runs in a foreground service with a persistent notification.")
        c.addView(ui.switch("Start monitoring after the phone restarts", null, bool("start_on_boot")) { changed("start_on_boot", it) })
        c.addView(ui.muted(if (exempt) "Battery optimisation: NetWatch is exempt, checks stay on schedule."
        else "Battery optimisation is on: Android may delay checks while the phone sleeps. Such gaps are recorded as unmonitored.", 12f).apply { setPadding(0, ui.dp(8), 0, ui.dp(6)) })
        if (!exempt) c.addView(ui.button("Battery settings") {
            runCatching { a.startActivity(Intent(Settings.ACTION_IGNORE_BATTERY_OPTIMIZATION_SETTINGS)) }
        }.apply { layoutParams = lp(LinearLayout.LayoutParams.WRAP_CONTENT) })

        c = section("Data", "All data stays on this device in a SQLite database (same format as NetWatch for Linux).")
        c.addView(ui.stepper("Keep raw measurements", "Days (0 = forever). Outage history is always kept.", int("retention_days"), 0, 3650, 30, "d") { changed("retention_days", it) })
        val dataButtons = ui.hbox(8)
        dataButtons.addView(ui.button("Back up…", R.drawable.ic_save) { backup() })
        dataButtons.addView(ui.button("Restore…") { restore() })
        c.addView(dataButtons, lp(top = ui.dp(8)))

        form.addView(ui.button("About & diagnostics", R.drawable.ic_info) { a.navigate("about") }.apply {
            layoutParams = lp(LinearLayout.LayoutParams.WRAP_CONTENT, top = ui.dp(20))
        })
        updateBar()
    }

    private fun editTarget(t: Target?) {
        val box = ui.vbox().apply { setPadding(ui.dp(20), ui.dp(8), ui.dp(20), 0) }
        fun field(hint: String, v: String) = EditText(a).apply { this.hint = hint; setText(v); setSingleLine() }
        val name = field("Name", t?.name ?: "")
        val address = field("Address (IP, host, host:port or URL)", t?.address ?: "")
        val kinds = Defaults.checkTypes.entries.toList()
        val type = Spinner(a).apply {
            adapter = ArrayAdapter(a, android.R.layout.simple_spinner_dropdown_item, kinds.map { it.value })
            setSelection(maxOf(0, kinds.indexOfFirst { it.key == (t?.checkType ?: "icmp") }))
        }
        val err = ui.text("", 13f, ui.p.error)
        box.addView(name); box.addView(address); box.addView(type); box.addView(err)
        val d = AlertDialog.Builder(a).setTitle(if (t == null) "Add target" else "Edit target").setView(box)
            .setNegativeButton("Cancel", null).setPositiveButton(if (t == null) "Add" else "Save", null).create()
        d.setOnShowListener {
            d.getButton(AlertDialog.BUTTON_POSITIVE).setOnClickListener {
                val k = kinds[type.selectedItemPosition].key
                val problems = SettingsValidator.validateTarget(name.text.toString(), address.text.toString(), k)
                if (problems.isNotEmpty()) { err.text = problems.joinToString(" "); return@setOnClickListener }
                try {
                    if (t == null) app.repo.addTarget(name.text.toString(), address.text.toString(), k)
                    else app.repo.updateTarget(t.copy(name = name.text.toString(), address = address.text.toString(), checkType = k))
                } catch (e: DuplicateTargetException) { err.text = e.message; return@setOnClickListener }
                app.engine.reload()
                d.dismiss()
                build()
            }
        }
        d.show()
    }

    private fun backup() {
        val stamp = java.time.LocalDateTime.now().toString().replace(':', '-').substringBefore('.')
        a.createDocument("netwatch-backup-$stamp.db", "application/vnd.sqlite3") { uri: Uri ->
            a.bg({ a.contentResolver.openOutputStream(uri, "wt")!!.use { app.db.backupTo(it) } }) { a.toast("Backup saved") }
        }
    }

    private fun restore() {
        a.openDocument { uri ->
            a.confirm("Replace all history with this backup?", "The current database will be replaced. Monitoring restarts afterwards. " +
                "Consider backing up the current data first.", "Restore") {
                val input = a.contentResolver.openInputStream(uri) ?: return@confirm
                app.restore(input) { err ->
                    input.close()
                    if (err == null) { a.toast("Backup restored"); build() }
                    else AlertDialog.Builder(a).setTitle("Restore failed").setMessage(err.message).setPositiveButton("OK", null).show()
                }
            }
        }
    }
}
