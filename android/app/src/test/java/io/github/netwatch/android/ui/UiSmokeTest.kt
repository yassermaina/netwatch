package io.github.netwatch.android.ui

import android.app.NotificationManager
import android.content.Intent
import android.graphics.Bitmap
import android.graphics.Canvas
import android.os.Looper
import android.view.View
import io.github.netwatch.android.core.ConnState
import io.github.netwatch.android.core.Lifecycle
import io.github.netwatch.android.platform.MonitorService
import io.github.netwatch.android.platform.NetWatchApp
import io.github.netwatch.android.platform.netwatch
import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.Robolectric
import org.robolectric.RobolectricTestRunner
import org.robolectric.RuntimeEnvironment
import org.robolectric.Shadows.shadowOf
import org.robolectric.annotation.Config
import org.robolectric.annotation.GraphicsMode
import java.io.File
import java.io.FileOutputStream
import kotlin.random.Random

/** Runs the real activity, screens and service under Robolectric (Android 15 runtime). */
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [35], qualifiers = "w411dp-h891dp-xxhdpi")
@GraphicsMode(GraphicsMode.Mode.NATIVE)
class UiSmokeTest {
    private val day = 86400.0

    private fun app(): NetWatchApp = RuntimeEnvironment.getApplication().netwatch

    /** Synthetic history: ~40 days, several sessions with gaps, short outages. */
    private fun seed(app: NetWatchApp, days: Int = 40, seed: Int = 3) {
        val r = Random(seed)
        val repo = app.repo
        val now = System.currentTimeMillis() / 1000.0
        var t = now - days * day
        val wifi = io.github.netwatch.android.core.LocalNetworkStatus(true, "connectivity_manager", "wifi", true, false,
            io.github.netwatch.android.core.WifiDetails(null, 5180, -47, "802.11ax", 866)).toJson()
        app.db.tx {
            while (t < now - 3 * 3600) {
                val sEnd = minOf(now - 3600, t + r.nextDouble(2.0, 9.0) * day)
                val sid = repo.createSession(t, JSONObject(), "seed")
                var cur = t
                var st = ConnState.ONLINE
                while (cur < sEnd) {
                    val dur = if (st == ConnState.ONLINE) r.nextDouble(3600.0, 1.2 * day) else r.nextDouble(20.0, 400.0)
                    val end = minOf(sEnd, cur + dur)
                    val id = repo.openState(sid, st, cur, cur, if (st == ConnState.ONLINE) "1.1.1.1" else "1.1.1.1, 8.8.8.8, 9.9.9.9",
                        if (st == ConnState.NOT_CONNECTED) null else wifi, if (st == ConnState.ONLINE) null else "all targets failed (Timed out)")
                    repo.closeState(id, end)
                    if (st.isDown) {
                        val o = repo.createOutage(sid, st, cur, cur + 10, true, JSONArray())
                        repo.closeOutage(o, end, if (end < sEnd) "recovered" else "monitoring_stopped", end < sEnd)
                    }
                    cur = end
                    st = if (st == ConnState.ONLINE) (if (r.nextDouble() < 0.85) ConnState.NO_INTERNET else ConnState.NOT_CONNECTED) else ConnState.ONLINE
                }
                repo.endSession(sid, sEnd)
                t = sEnd + r.nextDouble(1800.0, 2.5 * day)
            }
        }
    }

    private fun waitUntil(timeoutMs: Long = 10_000, cond: () -> Boolean) {
        val deadline = System.currentTimeMillis() + timeoutMs
        while (System.currentTimeMillis() < deadline) {
            shadowOf(Looper.getMainLooper()).idle()
            if (cond()) return
            Thread.sleep(20)
        }
        shadowOf(Looper.getMainLooper()).idle()
        assertTrue("condition not met within $timeoutMs ms", cond())
    }

    private fun settle(ms: Long = 400) {
        val end = System.currentTimeMillis() + ms
        while (System.currentTimeMillis() < end) { shadowOf(Looper.getMainLooper()).idle(); Thread.sleep(20) }
    }

    private fun screenshot(a: MainActivity, name: String) {
        val v: View = a.window.decorView
        val bmp = Bitmap.createBitmap(v.width, v.height, Bitmap.Config.ARGB_8888)
        v.draw(Canvas(bmp))
        val dir = File(System.getProperty("netwatch.screenshots") ?: "build/screenshots").apply { mkdirs() }
        FileOutputStream(File(dir, "$name.png")).use { bmp.compress(Bitmap.CompressFormat.PNG, 100, it) }
    }

    private fun launch(): MainActivity {
        app().settings.update(mapOf("start_monitoring_on_launch" to false), validateAll = false)
        return Robolectric.buildActivity(MainActivity::class.java).setup().get()
    }

    @Test fun allScreensOpenWithoutErrors() {
        val a = launch()
        for (page in listOf("overview", "history", "performance", "reports", "settings", "about")) {
            a.navigate(page)
            settle(200)
            assertEquals(page, a.current)
        }
        a.onBackPressed()
        assertEquals("settings", a.current)
    }

    @Test fun emptyHistoryShowsEmptyState() {
        val a = launch()
        a.navigate("history")
        val h = a.screenHistory()!!
        waitUntil { h.data != null }
        assertFalse(h.data!!.summary.hasData)
        assertTrue(h.filtered.all { it.synthetic })
    }

    @Test fun periodChangeUpdatesAllHistoryViews() {
        seed(app())
        val a = launch()
        a.navigate("history")
        val h = a.screenHistory()!!
        waitUntil { h.data != null }
        val all = h.data!!
        assertTrue(all.summary.hasData && all.summary.outageCount > 3)
        assertEquals(all.summary.rangeStart, h.timeline.start, 1.0)
        val segmentedField = HistoryScreen::class.java.getDeclaredField("segmented").apply { isAccessible = true }
        (segmentedField.get(h) as Segmented).select("24h", notify = true)
        waitUntil { h.period == "24h" && h.data !== all && h.data!!.summary.rangeDuration < day + 60 }
        val d1 = h.data!!
        assertTrue(d1.days.size < all.days.size)
        assertTrue(d1.summary.outageCount <= all.summary.outageCount)
        assertEquals(d1.summary.rangeStart, h.timeline.start, 1.0)
    }

    @Test fun timelineSelectionAndCalendarFilterReferToRealEvents() {
        seed(app())
        val a = launch()
        a.navigate("history")
        val h = a.screenHistory()!!
        waitUntil { h.data != null }
        val down = h.data!!.segments.first { it.state.isDown }
        h.selectSegment(down)
        settle()
        assertEquals(down.key, h.timeline.selectedKey)
        assertTrue(h.filtered.any { it.key == down.key })
        assertNotNull(down.outageId)
        assertTrue(down.outageId in h.data!!.outagesById)
    }

    @Test fun foregroundServiceStartsAndStopsMonitoring() {
        val app = app()
        val controller = Robolectric.buildService(MonitorService::class.java, Intent().setAction(MonitorService.ACTION_START)).create()
        controller.startCommand(0, 1)
        waitUntil { app.engine.lifecycle == Lifecycle.RUNNING }
        assertTrue(MonitorService.running)
        val nm = app.getSystemService(NotificationManager::class.java)
        assertTrue(shadowOf(nm).allNotifications.isNotEmpty())
        app.engine.pause()
        waitUntil { app.engine.lifecycle == Lifecycle.PAUSED }
        app.engine.resume()
        waitUntil { app.engine.lifecycle == Lifecycle.RUNNING }
        app.stopEngineBlocking()
        assertEquals(Lifecycle.STOPPED, app.engine.lifecycle)
        val sessions = app.db.query("SELECT status FROM monitoring_sessions")
        assertEquals("stopped", sessions.single()["status"])
        assertEquals(1L, app.db.queryOne("SELECT COUNT(*) n FROM connection_states WHERE state = 'paused'")!!["n"])
        controller.destroy()
    }

    @Test fun renderScreenshots() {
        seed(app(), days = 120)
        val a = launch()
        for (page in listOf("history", "overview", "performance", "settings", "reports", "about")) {
            a.navigate(page)
            settle(1200)
            screenshot(a, "phone-light-$page")
        }
    }

    @Test @Config(qualifiers = "w411dp-h891dp-night-xxhdpi")
    fun renderDarkScreenshots() {
        seed(app(), days = 120)
        val a = launch()
        for (page in listOf("history", "overview", "settings")) {
            a.navigate(page)
            settle(1200)
            screenshot(a, "phone-dark-$page")
        }
    }
}
