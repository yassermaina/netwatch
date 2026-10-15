package io.github.netwatch.android.ui

import android.Manifest
import android.app.Activity
import android.app.AlertDialog
import android.content.Intent
import android.content.pm.PackageManager
import android.content.res.ColorStateList
import android.content.res.Configuration
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.view.Gravity
import android.view.View
import android.view.ViewGroup
import android.view.WindowInsetsController
import android.widget.FrameLayout
import android.widget.ImageView
import android.widget.LinearLayout
import android.widget.TextView
import android.widget.Toast
import io.github.netwatch.android.R
import io.github.netwatch.android.core.EngineSnapshot
import io.github.netwatch.android.platform.NetWatchApp
import io.github.netwatch.android.platform.netwatch
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

/** Base class for the app's screens (Overview, History, Performance, Reports, Settings, About). */
abstract class Screen(val a: MainActivity) {
    val ui: Ui get() = a.ui
    val app: NetWatchApp get() = a.app
    abstract val title: String
    abstract val view: View
    open fun onShow() {}
    open fun onHide() {}
    open fun onSnapshot(s: EngineSnapshot) {}
    open fun onHistoryChanged() {}
    open fun onTick() {}
    open fun actions(bar: LinearLayout) {}
    open fun onWidthChanged(widthDp: Int) {}
    var visible = false
}

class MainActivity : Activity() {
    companion object {
        const val EXTRA_PAGE = "page"
        private const val REQ_NOTIFICATIONS = 1
        private const val REQ_DOCUMENT = 100
        private val NAV = listOf(
            Triple("overview", "Overview", R.drawable.ic_home),
            Triple("history", "History", R.drawable.ic_history),
            Triple("performance", "Performance", R.drawable.ic_chart),
            Triple("reports", "Reports", R.drawable.ic_report),
            Triple("settings", "Settings", R.drawable.ic_tune),
        )
    }

    val app: NetWatchApp get() = netwatch
    lateinit var ui: Ui; private set
    private lateinit var content: FrameLayout
    private lateinit var titleView: TextView
    private lateinit var actionBar: LinearLayout
    private lateinit var navItems: Map<String, LinearLayout>
    private lateinit var screens: Map<String, Screen>
    var current = "overview"; private set
    private val back = ArrayDeque<String>()
    val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main)
    private var snapshotJob: Job? = null
    private val tick = object : Runnable { override fun run() { screens[current]?.onTick(); app.main.postDelayed(this, 1000) } }
    private val historyListener: () -> Unit = { runOnUiThread { screens.values.forEach { it.onHistoryChanged() } } }
    private var pendingDocument: ((Uri) -> Unit)? = null

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        build()
        current = intent?.getStringExtra(EXTRA_PAGE) ?: savedInstanceState?.getString("page") ?: "overview"
        navigate(current, record = false)
        app.addHistoryListener(historyListener)
        if (app.recovered.isNotEmpty()) {
            toast("NetWatch was stopped unexpectedly (app or phone restarted). That time is shown as App Not Running.")
        }
        if (app.settings.bool("start_monitoring_on_launch") && !app.engine.isActive) app.requestStart(this)
        requestNotificationPermission()
    }

    /** (Re)build the whole UI with the current palette (also used when the theme setting changes). */
    fun build() {
        ui = Ui(this, Palette.resolve(this, app.settings.string("theme")))
        val p = ui.p
        window.decorView.setBackgroundColor(p.bg)
        @Suppress("DEPRECATION") run {
            window.statusBarColor = p.bg
            window.navigationBarColor = p.bar
        }
        if (Build.VERSION.SDK_INT >= 30) {
            val light = WindowInsetsController.APPEARANCE_LIGHT_STATUS_BARS or WindowInsetsController.APPEARANCE_LIGHT_NAVIGATION_BARS
            window.insetsController?.setSystemBarsAppearance(if (p.dark) 0 else light, light)
        } else {
            @Suppress("DEPRECATION")
            window.decorView.systemUiVisibility = if (p.dark) 0 else View.SYSTEM_UI_FLAG_LIGHT_STATUS_BAR or
                (if (Build.VERSION.SDK_INT >= 26) View.SYSTEM_UI_FLAG_LIGHT_NAVIGATION_BAR else 0)
        }
        val root = ui.vbox()
        val top = ui.hbox(8).apply { setPadding(ui.dp(18), ui.dp(10), ui.dp(10), ui.dp(10)); minimumHeight = ui.dp(56) }
        titleView = ui.text("", 20f, bold = true, lines = 1).apply { accessibilityHeading() }
        top.addView(titleView, lp(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f))
        actionBar = ui.hbox(6)
        top.addView(actionBar)
        root.addView(top)
        root.addView(ui.divider())
        content = FrameLayout(this)
        root.addView(content, lp(h = 0, weight = 1f))
        root.addView(ui.divider())
        val nav = ui.hbox().apply { setBackgroundColor(p.bar); setPadding(0, ui.dp(6), 0, ui.dp(6)) }
        navItems = NAV.associate { (key, label, icon) ->
            val item = ui.vbox().apply {
                gravity = Gravity.CENTER
                setPadding(0, ui.dp(4), 0, ui.dp(4))
                contentDescription = label
                setOnClickListener { navigate(key) }
                background = ui.ripple(null)
            }
            item.addView(ImageView(this).apply { setImageResource(icon); layoutParams = LinearLayout.LayoutParams(ui.dp(24), ui.dp(24)) })
            item.addView(ui.text(label, 11f, lines = 1).apply { gravity = Gravity.CENTER })
            nav.addView(item, lp(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f))
            key to item
        }
        root.addView(nav)
        setContentView(root)
        screens = linkedMapOf(
            "overview" to OverviewScreen(this),
            "history" to HistoryScreen(this),
            "performance" to PerformanceScreen(this),
            "reports" to ReportsScreen(this),
            "settings" to SettingsScreen(this),
            "about" to AboutScreen(this),
        )
        screens.values.forEach { it.onWidthChanged(resources.configuration.screenWidthDp) }
    }

    fun screenHistory(): HistoryScreen? = screens["history"] as? HistoryScreen

    fun rebuild() {
        screens.values.forEach { if (it.visible) it.onHide() }
        build()
        navigate(current, record = false)
    }

    fun navigate(key: String, record: Boolean = true) {
        val screen = screens[key] ?: return
        val old = screens[current]
        if (old != null && old !== screen && old.visible) { old.visible = false; old.onHide() }
        if (record && key != current) back.addLast(current)
        current = key
        titleView.text = screen.title
        content.removeAllViews()
        (screen.view.parent as? ViewGroup)?.removeView(screen.view)
        content.addView(screen.view, FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT))
        actionBar.removeAllViews()
        screen.actions(actionBar)
        if (key != "about" && key != "settings") actionBar.addView(ui.iconButton(R.drawable.ic_info, "About & diagnostics") { navigate("about") })
        for ((k, item) in navItems) {
            val on = k == key || (key == "about" && k == "settings")
            val color = if (on) ui.p.accent else ui.p.muted
            (item.getChildAt(0) as ImageView).imageTintList = ColorStateList.valueOf(color)
            (item.getChildAt(1) as TextView).setTextColor(color)
            item.isSelected = on
        }
        screen.visible = true
        screen.onShow()
        screen.onSnapshot(app.engine.snapshot.value)
    }

    override fun onStart() {
        super.onStart()
        snapshotJob = scope.launch { app.engine.snapshot.collect { s -> screens[current]?.onSnapshot(s) } }
        app.main.postDelayed(tick, 1000)
    }

    override fun onStop() {
        snapshotJob?.cancel()
        app.main.removeCallbacks(tick)
        super.onStop()
    }

    override fun onDestroy() {
        app.removeHistoryListener(historyListener)
        scope.cancel()
        super.onDestroy()
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        intent.getStringExtra(EXTRA_PAGE)?.let { navigate(it) }
    }

    override fun onSaveInstanceState(outState: Bundle) {
        super.onSaveInstanceState(outState)
        outState.putString("page", current)
    }

    override fun onConfigurationChanged(newConfig: Configuration) {
        super.onConfigurationChanged(newConfig)
        if (app.settings.string("theme") == "system" && Palette.resolve(this, "system").dark != ui.p.dark) rebuild()
        else screens.values.forEach { it.onWidthChanged(newConfig.screenWidthDp) }
    }

    @Deprecated("Deprecated in Java")
    override fun onBackPressed() {
        val settings = screens["settings"] as? SettingsScreen
        if (current == "settings" && settings?.hasUnsaved() == true) {
            settings.confirmDiscard { goBack() }
            return
        }
        goBack()
    }

    private fun goBack() {
        if (back.isNotEmpty()) navigate(back.removeLast(), record = false)
        else @Suppress("DEPRECATION") super.onBackPressed()
    }

    // ------------------------------------------------------------ helpers
    fun toast(msg: String) = Toast.makeText(this, msg, Toast.LENGTH_LONG).show()

    /** Run [work] off the main thread, deliver the result on it. */
    fun <T> bg(work: () -> T, done: (T) -> Unit) {
        scope.launch {
            val result = runCatching { withContext(Dispatchers.IO) { work() } }
            result.onSuccess(done).onFailure { toast("Error: ${it.message}") }
        }
    }

    fun confirm(title: String, message: String, action: String, destructive: Boolean = true, onConfirm: () -> Unit) {
        val d = AlertDialog.Builder(this).setTitle(title).setMessage(message)
            .setNegativeButton("Cancel", null)
            .setPositiveButton(action) { _, _ -> onConfirm() }
            .create()
        d.show()
        if (destructive) d.getButton(AlertDialog.BUTTON_POSITIVE).setTextColor(ui.p.error)
    }

    /** Storage Access Framework: create (save) or open a document, then call [then] with its URI. */
    fun createDocument(name: String, mime: String, then: (Uri) -> Unit) {
        pendingDocument = then
        startActivityForResult(Intent(Intent.ACTION_CREATE_DOCUMENT).addCategory(Intent.CATEGORY_OPENABLE).setType(mime)
            .putExtra(Intent.EXTRA_TITLE, name), REQ_DOCUMENT)
    }

    fun openDocument(then: (Uri) -> Unit) {
        pendingDocument = then
        startActivityForResult(Intent(Intent.ACTION_OPEN_DOCUMENT).addCategory(Intent.CATEGORY_OPENABLE).setType("*/*"), REQ_DOCUMENT)
    }

    @Deprecated("Deprecated in Java")
    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        @Suppress("DEPRECATION") super.onActivityResult(requestCode, resultCode, data)
        if (requestCode == REQ_DOCUMENT) {
            val cb = pendingDocument
            pendingDocument = null
            val uri = data?.data
            if (resultCode == RESULT_OK && uri != null) cb?.invoke(uri)
        }
    }

    private fun requestNotificationPermission() {
        if (Build.VERSION.SDK_INT >= 33 && checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(arrayOf(Manifest.permission.POST_NOTIFICATIONS), REQ_NOTIFICATIONS)
        }
    }

    override fun onRequestPermissionsResult(requestCode: Int, permissions: Array<out String>, grantResults: IntArray) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        if (requestCode == REQ_NOTIFICATIONS && grantResults.firstOrNull() == PackageManager.PERMISSION_DENIED) {
            toast("Notifications are off: outages will still be recorded, but you won't be alerted outside the app.")
        }
    }
}
