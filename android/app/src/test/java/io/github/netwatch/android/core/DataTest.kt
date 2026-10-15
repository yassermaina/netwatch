package io.github.netwatch.android.core

import kotlinx.coroutines.runBlocking
import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertThrows
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import java.io.File

class DataTest {
    @get:Rule val tmp = TemporaryFolder()
    private val day = 86400.0

    private fun h() = Harness(tmp.newFolder())

    /** 1h online, 10 min outage, 50 min online; 2 h gap; 1 h online (ongoing). */
    private fun seed(h: Harness, t0: Double) {
        val r = h.repo
        val s1 = r.createSession(t0, JSONObject(), "t")
        r.closeState(r.openState(s1, ConnState.ONLINE, t0, null, null, null, null), t0 + 3600)
        r.closeState(r.openState(s1, ConnState.NO_INTERNET, t0 + 3600, null, null, null, null), t0 + 4200)
        val o = r.createOutage(s1, ConnState.NO_INTERNET, t0 + 3600, t0 + 3610, true, JSONArray())
        r.closeOutage(o, t0 + 4200, "recovered", true, t0 + 4205)
        r.closeState(r.openState(s1, ConnState.ONLINE, t0 + 4200, null, null, null, null), t0 + 7200)
        r.endSession(s1, t0 + 7200)
        val s2 = r.createSession(t0 + 14400, JSONObject(), "t")
        r.openState(s2, ConnState.ONLINE, t0 + 14400, null, null, null, null)
    }

    @Test fun uptimeUsesMonitoredTimeDenominator() {
        val h = h()
        val t0 = 1_700_000_000.0
        seed(h, t0)
        val now = t0 + 18000
        val s = h.stats.summary(t0, now, now)
        assertEquals(10800.0, s.monitored, 1e-6)
        assertEquals(600.0, s.downtime, 1e-6)
        assertEquals(10200.0 / 10800 * 100, s.uptimePct!!, 1e-9)
        assertEquals(7200.0, s.notRunning, 1e-6)
        assertEquals(60.0, s.coveragePct!!, 1e-9)
        assertEquals(1, s.outageCount)
        assertEquals(600.0, s.longestOutage!!.durationInRange, 1e-6)
    }

    @Test fun periodChangeUpdatesMetrics() {
        val h = h()
        val now = System.currentTimeMillis() / 1000.0
        val t0 = now - 40 * day
        val r = h.repo
        val s = r.createSession(t0, JSONObject(), "t")
        r.closeState(r.openState(s, ConnState.ONLINE, t0, null, null, null, null), t0 + 20 * day)
        r.closeState(r.openState(s, ConnState.NO_INTERNET, t0 + 20 * day, null, null, null, null), t0 + 20 * day + 3600)
        val o = r.createOutage(s, ConnState.NO_INTERNET, t0 + 20 * day, t0 + 20 * day, true, JSONArray())
        r.closeOutage(o, t0 + 20 * day + 3600, "recovered", true)
        r.openState(s, ConnState.ONLINE, t0 + 20 * day + 3600, null, null, null, null)
        val d1 = h.stats.history("24h", now)
        val d30 = h.stats.history("30d", now)
        val all = h.stats.history("all", now)
        assertEquals(0, d1.summary.outageCount)
        assertEquals(100.0, d1.summary.uptimePct!!, 1e-9)
        assertEquals(1, d30.summary.outageCount)
        assertEquals(1, all.summary.outageCount)
        assertTrue(all.days.size > d30.days.size && d30.days.size > d1.days.size)
        assertEquals(t0, all.segments.first().start, 1e-6)
    }

    @Test fun calendarUsesRealRecordsAndEmptyDaysAreNotOnline() {
        val h = h()
        val now = System.currentTimeMillis() / 1000.0
        val t0 = now - 3 * day
        val s = h.repo.createSession(t0, JSONObject(), "t")
        h.repo.closeState(h.repo.openState(s, ConnState.ONLINE, t0, null, null, null, null), t0 + 3600)
        h.repo.endSession(s, t0 + 3600)
        val hist = h.stats.history("7d", now)
        val firstDay = Fmt.localDate(t0)
        val cats = hist.days.associate { it.day to it.category }
        assertEquals("online", cats[firstDay])
        val others = cats.filter { it.key != firstDay && it.value != "outside" }.values
        assertTrue(others.isNotEmpty() && others.all { it == "unmonitored" })
        assertEquals(3600.0, hist.days.sumOf { it.durations.getValue(ConnState.ONLINE) }, 1e-6)
    }

    @Test fun segmentsLinkToOutagesAndGapsAreSynthetic() {
        val h = h()
        val t0 = 1_700_000_000.0
        seed(h, t0)
        val hist = h.stats.history("custom", t0 + 18000, t0 to t0 + 18000)
        val down = hist.segments.filter { it.state == ConnState.NO_INTERNET }
        assertTrue(down.single().outageId in hist.outagesById)
        val gap = hist.segments.first { it.synthetic }
        assertEquals(ConnState.NOT_RUNNING, gap.state)
        assertEquals(7200.0, gap.duration, 1e-6)
        hist.segments.zipWithNext().forEach { (a, b) -> assertTrue(a.end <= b.start + 1e-9) }
    }

    @Test fun overlappingRecordsAreNotDoubleCounted() {
        val h = h()
        val t0 = 1_700_000_000.0
        val s = h.repo.createSession(t0, JSONObject(), "t")
        h.repo.closeState(h.repo.openState(s, ConnState.ONLINE, t0, null, null, null, null), t0 + 1000)
        h.repo.closeState(h.repo.openState(s, ConnState.ONLINE, t0 + 500, null, null, null, null), t0 + 1500)
        assertEquals(1500.0, h.stats.summary(t0, t0 + 1500, t0 + 1500).online, 1e-6)
    }

    @Test fun emptyHistory() {
        val hist = h().stats.history("all", System.currentTimeMillis() / 1000.0)
        assertFalse(hist.summary.hasData)
        assertNull(hist.summary.uptimePct)
        assertTrue(hist.segments.all { it.synthetic })
    }

    @Test fun reportsRepresentPartialCoverage() {
        val h = h()
        val t0 = 1_700_000_000.0
        seed(h, t0)
        val rep = Reports.build(h.repo, h.stats, h.settings, custom = t0 to t0 + 18000, now = t0 + 18000, device = "test")
        val txt = Reports.txt(rep)
        assertTrue(txt.contains("60.0% coverage"))
        assertTrue(txt.contains("App Not Running"))
        assertTrue(rep.limitations.any { it.contains("covered") })
        val html = Reports.html(rep)
        assertTrue(html.contains("<svg") && html.contains("Limitations") && !html.contains("<script"))
        val lines = Reports.csv(rep).trim().split("\r\n")
        assertEquals(Reports.csvColumns.joinToString(","), lines.first())
        val outage = lines.first { it.startsWith("outage,") }.split(",")
        assertEquals("600", outage[6])
        assertEquals("yes", outage[11])
        assertTrue(setOf("summary", "state", "unmonitored", "outage").all { t -> lines.any { it.startsWith("$t,") } })
    }

    @Test fun reportFlagsUnobservedOutageStart() {
        val h = h()
        val t0 = 1_700_000_000.0
        val s = h.repo.createSession(t0, JSONObject(), "t")
        h.repo.openState(s, ConnState.NO_INTERNET, t0, null, null, null, null)
        h.repo.createOutage(s, ConnState.NO_INTERNET, t0, t0, false, JSONArray())
        val txt = Reports.txt(Reports.build(h.repo, h.stats, h.settings, custom = t0 to t0 + 600, now = t0 + 600, device = "t"))
        assertTrue(txt.contains("start not observed") && txt.contains("ongoing"))
    }

    @Test fun migrationsAreIdempotentAndNewerSchemaRejected() {
        val db = JdbcSqlDb(File(tmp.newFolder(), "m.db"))
        assertEquals(listOf(1), Schema.migrate(db, 0.0))
        assertEquals(emptyList<Int>(), Schema.migrate(db, 0.0))
        db.exec("INSERT INTO schema_migrations VALUES (999, 'future', 0)")
        assertThrows(DatabaseException::class.java) { Schema.migrate(db, 0.0) }
    }

    @Test fun retentionPrunesMeasurementsButKeepsOutages() {
        val h = h()
        val sid = h.repo.createSession(0.0, JSONObject(), "t")
        val tid = h.repo.listTargets().first().id
        h.repo.insertMeasurements(sid, listOf(10.0, 20.0, 30.0, 1000.0).map { CheckResult(tid, "x", "icmp", true, it, 1.0) })
        h.repo.createOutage(sid, ConnState.NO_INTERNET, 15.0, 15.0, true, JSONArray())
        assertEquals(3, h.repo.pruneMeasurements(100.0))
        assertEquals(1L, h.db.queryOne("SELECT COUNT(*) n FROM measurements")!!.l("n"))
        assertEquals(1L, h.db.queryOne("SELECT COUNT(*) n FROM outages")!!.l("n"))
    }

    @Test fun settingsValidationAndPersistence() {
        val h = h()
        val errors = h.settings.update(mapOf("interval_seconds" to 5, "timeout_seconds" to 10))
        assertTrue(errors.first().message.contains("shorter than the check interval"))
        assertEquals(3, h.settings.int("timeout_seconds")) // nothing persisted
        assertTrue(h.settings.update(mapOf("interval_seconds" to 20, "theme" to "dark")).isEmpty())
        val again = SettingsStore(h.db) { 0.0 }
        assertEquals(20, again.int("interval_seconds"))
        assertEquals("dark", again.string("theme"))
        assertTrue(SettingsValidator.validateTarget("x", "999.1.1.1.1", "icmp").isNotEmpty())
        assertTrue(SettingsValidator.validateTarget("", "1.1.1.1", "icmp").isNotEmpty())
        assertTrue(SettingsValidator.validateTarget("d", "1.1.1.1", "dns").isNotEmpty())
        assertTrue(SettingsValidator.validateTarget("t", "1.1.1.1", "tcp").isNotEmpty()) // port missing
        assertTrue(SettingsValidator.validateTarget("t", "1.1.1.1:443", "tcp").isEmpty())
        assertTrue(SettingsValidator.validateTarget("v6", "2606:4700:4700::1111", "icmp").isEmpty())
        assertTrue(SettingsValidator.validateTarget("r", "192.168.1.1", "icmp").isEmpty())
        assertTrue(SettingsValidator.validateTarget("w", "https://example.com/health", "http").isEmpty())
    }

    @Test fun duplicateTargetsRejectedAndDeletedTargetsKeptForHistory() {
        val h = h()
        assertThrows(DuplicateTargetException::class.java) { h.repo.addTarget("dup", "1.1.1.1", "icmp") }
        val t = h.repo.listTargets().first()
        h.repo.deleteTarget(t.id!!)
        assertTrue(t.id in h.repo.targetsById())
        h.repo.addTarget("again", t.address, "icmp")
    }

    @Test fun largeHistoryAggregatesQuickly() {
        val h = h()
        val now = System.currentTimeMillis() / 1000.0
        var t = now - 365 * day
        val sid = h.repo.createSession(t, JSONObject(), "t")
        h.db.tx {
            for (i in 0 until 20000) {
                val st = if (i % 2 == 0) ConnState.ONLINE else ConnState.NO_INTERNET
                val d = if (st == ConnState.ONLINE) 1500.0 else 60.0
                h.db.insert("INSERT INTO connection_states(session_id, state, started_at, ended_at) VALUES (?,?,?,?)", listOf(sid, st.value, t, t + d))
                t += d
            }
        }
        val started = System.nanoTime()
        val hist = h.stats.history("all", now)
        val elapsed = (System.nanoTime() - started) / 1e9
        assertTrue(hist.segments.size >= 20000)
        assertTrue("took $elapsed s", elapsed < 3.0)
    }

    @Test fun deleteRangeAndClearAll() {
        val h = h()
        val t0 = 1_700_000_000.0
        seed(h, t0)
        val res = h.repo.deleteRange(t0, t0 + 7200)
        assertEquals(3, res.states)
        assertEquals(1, res.outages)
        val live = h.db.queryOne("SELECT id FROM monitoring_sessions WHERE ended_at IS NULL")!!.l("id")
        h.repo.deleteAll(live, t0 + 20000)
        assertEquals(1L, h.db.queryOne("SELECT COUNT(*) n FROM connection_states")!!.l("n"))
        assertEquals(t0 + 20000, h.db.queryOne("SELECT started_at FROM connection_states")!!.d("started_at")!!, 1e-6)
    }

    @Test fun localStatusJsonRoundTripsWithoutInventingFields() {
        val s = LocalNetworkStatus(true, "connectivity_manager", "wifi", true, false, WifiDetails(null, 5180, -45, "802.11ax", null))
        val back = LocalNetworkStatus.fromJson(s.toJson())!!
        assertEquals(s, back)
        assertEquals("Wi-Fi (Ch 36, 5 GHz, 802.11ax, -45 dBm)", back.describe())
        assertEquals("Wi-Fi", LocalNetworkStatus(true, transport = "wifi").describe())
    }
}

class AlertsTest {
    @get:Rule val tmp = TemporaryFolder()

    class FakePlayer : SoundPlayer {
        override val available = true
        val played = mutableListOf<String>()
        var stops = 0
        override fun play(kind: String, volume: Int) { played += kind }
        override fun stop() { stops++ }
        override fun isPlaying() = false
    }

    private fun make(over: Map<String, Any> = emptyMap()): Triple<AlertManager, FakePlayer, DoubleArray> {
        val h = Harness(tmp.newFolder())
        h.settings.update(over, validateAll = false)
        val now = doubleArrayOf(0.0)
        val p = FakePlayer()
        return Triple(AlertManager(p, h.settings) { now[0] }, p, now)
    }

    @Test fun outageAlertPlaysOnceThenRepeats() {
        val (m, p, now) = make(mapOf("sound_repeat_seconds" to 10))
        m.outageStarted()
        m.outageStarted()
        assertEquals(listOf("outage"), p.played)
        for (s in 1..9) { now[0] = s.toDouble(); m.tick() }
        assertEquals(1, p.played.size)
        now[0] = 10.0
        m.tick()
        assertEquals(2, p.played.size)
        m.outageEnded()
        assertEquals("recovery", p.played.last())
    }

    @Test fun mutedIsSilentAndUnmutePolicyIsRespected() = runBlocking {
        val (m, p, _) = make(mapOf("muted" to true))
        m.outageStarted()
        assertTrue(p.played.isEmpty())
        m.setMuted(false)
        assertEquals(listOf("outage"), p.played)
        val (m2, p2, _) = make(mapOf("muted" to true, "resume_alert_on_unmute" to false))
        m2.outageStarted()
        m2.setMuted(false)
        m2.tick()
        assertTrue(p2.played.isEmpty())
    }

    @Test fun synthesizedSoundsAreNonEmpty() {
        assertTrue(Synth.samples("outage", 30).size > 5000)
        assertTrue(Synth.samples("recovery", 30).any { it != 0.toShort() })
        assertTrue(Synth.samples("outage", 0).all { it == 0.toShort() })
    }
}
