package io.github.netwatch.android.core

import kotlinx.coroutines.runBlocking
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder

class EngineTest {
    @get:Rule val tmp = TemporaryFolder()

    private fun h() = Harness(tmp.newFolder())

    @Test fun successfulChecksResultInOnline() = runBlocking {
        val h = h()
        h.engine.beginSession(h.clock.now())
        val t = h.run(1)
        assertEquals(ConnState.ONLINE, t.single().new)
        assertEquals(ConnState.ONLINE, h.engine.snapshot.value.state)
        assertEquals(Lifecycle.RUNNING, h.engine.lifecycle)
        assertEquals(3L, h.db.queryOne("SELECT COUNT(*) n FROM measurements")!!.l("n"))
    }

    @Test fun oneFailedTargetIsNotAnOutage() = runBlocking {
        val h = h()
        h.engine.beginSession(h.clock.now())
        h.run(1)
        h.adapter.outcomes[h.addresses[0]] = false
        h.run(10)
        assertEquals(ConnState.ONLINE, h.engine.snapshot.value.state)
        assertTrue(h.outages().isEmpty())
    }

    @Test fun consecutiveFailuresProduceOneOutageAtThreshold() = runBlocking {
        val h = h()
        h.settings.update(mapOf("failure_threshold" to 3))
        h.engine.beginSession(h.clock.now())
        h.run(2)
        h.allFail()
        h.run(2)
        assertEquals(ConnState.ONLINE, h.engine.snapshot.value.state)
        assertTrue(h.outages().isEmpty())
        h.run(1)
        assertEquals(ConnState.NO_INTERNET, h.engine.snapshot.value.state)
        val o = h.outages().single()
        assertEquals("ongoing", o.s("status"))
        assertTrue(o.b("start_observed"))
    }

    @Test fun repeatedFailuresDoNotDuplicateOutages() = runBlocking {
        val h = h()
        h.engine.beginSession(h.clock.now())
        h.run(1)
        h.allFail()
        h.run(40)
        assertEquals(1, h.outages().size)
        val counts = h.db.query("SELECT failure_count FROM outage_targets").map { it.l("failure_count")!! }
        assertTrue(counts.all { it >= 39 })
    }

    @Test fun recoveryClosesOutageWithCorrectDuration() = runBlocking {
        val h = h()
        h.engine.beginSession(h.clock.now())
        h.run(1)
        val firstFail = h.clock.now()
        h.allFail()
        h.run(6)
        h.allOk()
        val firstOk = h.clock.now()
        h.run(2)
        val o = h.outages().single()
        assertEquals("recovered", o.s("status"))
        assertTrue(o.b("end_observed"))
        assertEquals(firstFail, o.d("started_at")!!, 1e-6)
        assertEquals(firstOk, o.d("ended_at")!!, 1e-6)
        assertEquals(30.0, o.d("duration")!!, 1e-6)
        val st = h.states()
        st.zipWithNext().forEach { (a, b) -> assertEquals(a.d("ended_at")!!, b.d("started_at")!!, 1e-9) }
    }

    @Test fun notConnectedAndSwitchWithinOutage() = runBlocking {
        val h = h()
        h.engine.beginSession(h.clock.now())
        h.run(1)
        h.allFail(ErrorCategory.NETWORK_DOWN)
        h.probe.status = LocalNetworkStatus(connected = false, source = "test", detail = "no network")
        h.run(2)
        assertEquals(ConnState.NOT_CONNECTED, h.engine.snapshot.value.state)
        h.probe.status = LocalNetworkStatus(connected = true, source = "test")
        h.allFail()
        h.run(1)
        assertEquals(ConnState.NO_INTERNET, h.engine.snapshot.value.state)
        assertEquals(1, h.outages().size)
    }

    @Test fun systemNetworkRestrictionIsUnknownNotOutage() = runBlocking {
        // Doze / data saver blocks sockets: must not be reported as an internet outage.
        val h = h()
        h.engine.beginSession(h.clock.now())
        h.run(1)
        h.allFail(ErrorCategory.TIMEOUT)
        h.probe.status = LocalNetworkStatus(connected = true, source = "test", blocked = true)
        h.run(5)
        assertEquals(ConnState.UNKNOWN, h.engine.snapshot.value.state)
        assertTrue(h.outages().isEmpty())
    }

    @Test fun missingDependencyGivesUnknown() = runBlocking {
        val h = h()
        h.engine.beginSession(h.clock.now())
        h.allFail(ErrorCategory.MISSING_DEPENDENCY)
        h.run(5)
        assertEquals(ConnState.UNKNOWN, h.engine.snapshot.value.state)
        assertTrue(h.outages().isEmpty())
        assertTrue(h.engine.classifier.lastReason.contains("not available"))
    }

    @Test fun publicIpFailureAloneIsNotLocalDisconnection() {
        val r = listOf(CheckResult(1, "1.1.1.1", "icmp", false, 0.0, error = ErrorCategory.TIMEOUT))
        assertEquals(ConnState.NO_INTERNET, classifyRound(r, LocalNetworkStatus(connected = null)).first)
        assertEquals(ConnState.NO_INTERNET, classifyRound(r, LocalNetworkStatus(connected = true)).first)
        assertNotEquals(ConnState.ONLINE, classifyRound(emptyList(), null).first)
    }

    @Test fun pauseCreatesIdentifiableUnmonitoredInterval() = runBlocking {
        val h = h()
        h.engine.beginSession(h.clock.now())
        h.run(3)
        h.engine.pause()
        assertEquals(Lifecycle.PAUSED, h.engine.lifecycle)
        assertNull(h.engine.tick())
        h.clock.advance(600.0)
        h.engine.resume()
        h.run(2)
        val paused = h.states().filter { it.s("state") == "paused" }
        assertEquals(600.0, paused.single().d("ended_at")!! - paused.single().d("started_at")!!, 1e-6)
        val kinds = h.db.query("SELECT kind FROM application_events").map { it.s("kind") }
        assertTrue("paused" in kinds && "resumed" in kinds)
        val s = h.stats.summary(h.clock.now() - 2000, h.clock.now(), h.clock.now())
        assertTrue(s.paused >= 600 && s.online < 100)
    }

    @Test fun pauseDuringOutageClosesItAsUnobserved() = runBlocking {
        val h = h()
        h.engine.beginSession(h.clock.now())
        h.run(1)
        h.allFail()
        h.run(3)
        h.engine.pause()
        val o = h.outages().single()
        assertEquals("monitoring_paused", o.s("status"))
        assertTrue(!o.b("end_observed"))
    }

    @Test fun stopClosesSession() = runBlocking {
        val h = h()
        h.engine.beginSession(h.clock.now())
        h.run(3)
        h.engine.stop()
        val s = h.db.query("SELECT * FROM monitoring_sessions").single()
        assertEquals("stopped", s.s("status"))
        assertEquals(0L, h.db.queryOne("SELECT COUNT(*) n FROM connection_states WHERE ended_at IS NULL")!!.l("n"))
        assertEquals(Lifecycle.STOPPED, h.engine.lifecycle)
    }

    @Test fun interruptedSessionDetectedAfterRestart() = runBlocking {
        val h = h()
        h.engine.beginSession(h.clock.now())
        h.run(1)
        h.allFail()
        h.run(4)
        val lastBeat = h.clock.now() - 5
        h.clock.advance(3600.0) // process killed, nothing recorded
        val rec = h.repo.recoverInterruptedSessions()
        assertEquals(lastBeat, rec.single().endedAt, 1e-6)
        assertEquals("interrupted", h.db.queryOne("SELECT status FROM monitoring_sessions")!!.s("status"))
        assertEquals(0L, h.db.queryOne("SELECT COUNT(*) n FROM connection_states WHERE ended_at IS NULL")!!.l("n"))
        assertEquals("monitoring_interrupted", h.outages().single().s("status"))
        val now = h.clock.now()
        assertTrue(h.stats.summary(now - 7200, now, now).notRunning >= 3600)
    }

    @Test fun deepSleepGapIsNotCountedAsMonitored() = runBlocking {
        val h = h()
        h.engine.beginSession(h.clock.now())
        h.run(3)
        h.clock.advance(7200.0) // device slept: no rounds for two hours
        h.run(2)
        val kinds = h.db.query("SELECT kind FROM application_events").map { it.s("kind") }
        assertTrue("monitoring_stalled" in kinds)
        val now = h.clock.now()
        val s = h.stats.summary(now - 10000, now, now)
        assertTrue(s.online < 100)
        assertTrue(s.notRunning > 7000)
    }

    @Test fun clockChangeIsDetected() = runBlocking {
        val h = h()
        h.engine.beginSession(h.clock.now())
        h.run(3)
        h.clock.t += 3600 // wall clock jumps, monotonic does not
        h.run(1)
        assertTrue("clock_change" in h.db.query("SELECT kind FROM application_events").map { it.s("kind") })
    }
}
