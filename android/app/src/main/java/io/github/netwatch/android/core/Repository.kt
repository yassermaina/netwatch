package io.github.netwatch.android.core

import org.json.JSONArray
import org.json.JSONObject

class DuplicateTargetException(message: String) : Exception(message)

/** Data access layer; all SQL is parameterised. Ported from netwatch/storage/repository.py. */
class Repository(val db: SqlDb, private val clock: () -> Double) {

    // ------------------------------------------------------------ targets
    fun ensureDefaultTargets(defaults: List<Triple<String, String, String>>) {
        if ((db.queryOne("SELECT COUNT(*) AS n FROM targets")?.l("n") ?: 0) > 0) return
        db.tx {
            defaults.forEachIndexed { pos, (name, address, kind) ->
                db.insert(
                    "INSERT INTO targets(name, address, check_type, enabled, position, created_at) VALUES (?, ?, ?, 1, ?, ?)",
                    listOf(name, address, kind, pos, clock()),
                )
            }
        }
    }

    private fun target(r: Row) = Target(r.l("id"), r.s("name") ?: "", r.s("address") ?: "", r.s("check_type") ?: "icmp",
        r.b("enabled"), r.l("position")?.toInt() ?: 0)

    fun listTargets(enabledOnly: Boolean = false): List<Target> =
        db.query("SELECT * FROM targets WHERE deleted_at IS NULL" + (if (enabledOnly) " AND enabled = 1" else "") +
            " ORDER BY position, id").map(::target)

    /** All targets including deleted ones (history still references them). */
    fun targetsById(): Map<Long, Target> = db.query("SELECT * FROM targets").associate { it.l("id")!! to target(it) }

    fun addTarget(name: String, address: String, checkType: String): Long {
        val pos = db.queryOne("SELECT COALESCE(MAX(position), -1) + 1 AS p FROM targets WHERE deleted_at IS NULL")?.l("p") ?: 0
        try {
            return db.insert(
                "INSERT INTO targets(name, address, check_type, enabled, position, created_at) VALUES (?, ?, ?, 1, ?, ?)",
                listOf(name.trim(), address.trim(), checkType, pos, clock()),
            )
        } catch (e: Exception) {
            if (isConstraint(e)) throw DuplicateTargetException("$address is already monitored with this check type.")
            throw e
        }
    }

    fun updateTarget(t: Target) {
        try {
            db.update("UPDATE targets SET name = ?, address = ?, check_type = ?, enabled = ? WHERE id = ?",
                listOf(t.name.trim(), t.address.trim(), t.checkType, if (t.enabled) 1 else 0, t.id))
        } catch (e: Exception) {
            if (isConstraint(e)) throw DuplicateTargetException("${t.address} is already monitored with this check type.")
            throw e
        }
    }

    fun deleteTarget(id: Long) {
        db.update("UPDATE targets SET deleted_at = ?, enabled = 0 WHERE id = ?", listOf(clock(), id))
    }

    private fun isConstraint(e: Exception): Boolean =
        e.javaClass.simpleName.contains("Constraint") || (e.message ?: "").contains("UNIQUE", ignoreCase = true)

    // ------------------------------------------------------------ sessions
    fun createSession(startedAt: Double, config: JSONObject, version: String): Long = db.insert(
        "INSERT INTO monitoring_sessions(started_at, last_heartbeat, status, config_snapshot, app_version) VALUES (?, ?, 'running', ?, ?)",
        listOf(startedAt, startedAt, config.toString(), version),
    )

    fun heartbeat(sessionId: Long, ts: Double) {
        db.update("UPDATE monitoring_sessions SET last_heartbeat = ? WHERE id = ?", listOf(ts, sessionId))
    }

    fun endSession(sessionId: Long, endedAt: Double, status: String = "stopped", reason: String = "normal") {
        db.update("UPDATE monitoring_sessions SET ended_at = ?, last_heartbeat = ?, status = ?, end_reason = ? WHERE id = ?",
            listOf(endedAt, endedAt, status, reason, sessionId))
    }

    data class Recovered(val sessionId: Long, val startedAt: Double, val endedAt: Double)

    /** Close sessions left 'running' by a crash or process kill, at their last committed heartbeat. */
    fun recoverInterruptedSessions(): List<Recovered> {
        val out = mutableListOf<Recovered>()
        for (row in db.query("SELECT * FROM monitoring_sessions WHERE status = 'running'")) {
            val id = row.l("id")!!
            val end = row.d("last_heartbeat") ?: row.d("started_at")!!
            db.tx {
                db.update("UPDATE monitoring_sessions SET ended_at = ?, status = 'interrupted', end_reason = 'interrupted' WHERE id = ?", listOf(end, id))
                db.update("DELETE FROM connection_states WHERE session_id = ? AND started_at > ?", listOf(id, end))
                db.update("UPDATE connection_states SET ended_at = MAX(started_at, ?) WHERE session_id = ? AND (ended_at IS NULL OR ended_at > ?)",
                    listOf(end, id, end))
                for (o in db.query("SELECT * FROM outages WHERE session_id = ? AND status = 'ongoing'", listOf(id))) {
                    val stop = maxOf(o.d("started_at")!!, end)
                    db.update("UPDATE outages SET ended_at = ?, duration = ?, status = 'monitoring_interrupted', end_observed = 0 WHERE id = ?",
                        listOf(stop, stop - o.d("started_at")!!, o.l("id")))
                }
                logEvent("session_interrupted", "Monitoring stopped unexpectedly (app or device restarted); the time after the last check is treated as unmonitored.",
                    id, JSONObject().put("last_heartbeat", end))
            }
            out += Recovered(id, row.d("started_at")!!, end)
        }
        return out
    }

    // ------------------------------------------------------------ states
    fun openState(sessionId: Long, state: ConnState, startedAt: Double, detectedAt: Double?, targets: String?,
                  iface: String?, reason: String?): Long = db.insert(
        "INSERT INTO connection_states(session_id, state, started_at, detected_at, targets, interface, reason) VALUES (?, ?, ?, ?, ?, ?, ?)",
        listOf(sessionId, state.value, startedAt, detectedAt, targets, iface, reason),
    )

    fun closeState(stateId: Long, endedAt: Double) {
        db.update("UPDATE connection_states SET ended_at = MAX(started_at, ?) WHERE id = ?", listOf(endedAt, stateId))
    }

    fun updateStateInterface(stateId: Long, iface: String) {
        db.update("UPDATE connection_states SET interface = ? WHERE id = ?", listOf(iface, stateId))
    }

    fun statesBetween(start: Double, end: Double): List<Row> = db.query(
        "SELECT * FROM connection_states WHERE started_at < ? AND (ended_at IS NULL OR ended_at > ?) ORDER BY started_at, id",
        listOf(end, start),
    )

    fun earliestRecord(): Double? = db.queryOne(
        "SELECT MIN(t) AS t FROM (SELECT MIN(started_at) AS t FROM connection_states UNION ALL SELECT MIN(started_at) FROM monitoring_sessions)"
    )?.d("t")

    // ------------------------------------------------------------ outages
    fun createOutage(sessionId: Long, classification: ConnState, startedAt: Double, detectedAt: Double,
                     startObserved: Boolean, evidence: JSONArray): Long = db.insert(
        "INSERT INTO outages(session_id, classification, started_at, detected_at, status, start_observed, evidence) VALUES (?, ?, ?, ?, 'ongoing', ?, ?)",
        listOf(sessionId, classification.value, startedAt, detectedAt, if (startObserved) 1 else 0, evidence.toString()),
    )

    private fun appendEvidence(outageId: Long, items: JSONArray) {
        val row = db.queryOne("SELECT evidence FROM outages WHERE id = ?", listOf(outageId)) ?: return
        val arr = try { JSONArray(row.s("evidence") ?: "[]") } catch (e: Exception) { JSONArray() }
        for (i in 0 until items.length()) arr.put(items.get(i))
        val trimmed = JSONArray()
        for (i in maxOf(0, arr.length() - 200) until arr.length()) trimmed.put(arr.get(i))
        db.update("UPDATE outages SET evidence = ? WHERE id = ?", listOf(trimmed.toString(), outageId))
    }

    fun reclassifyOutage(outageId: Long, classification: ConnState, evidence: JSONArray) {
        db.update("UPDATE outages SET classification = ? WHERE id = ?", listOf(classification.value, outageId))
        appendEvidence(outageId, evidence)
    }

    fun recordOutageFailures(outageId: Long, results: List<CheckResult>) {
        for (r in results) {
            if (r.success || r.targetId == null) continue
            // No UPSERT: Android 8-10 ship SQLite < 3.24.
            val changed = db.update(
                "UPDATE outage_targets SET failure_count = failure_count + 1, last_error = ? WHERE outage_id = ? AND target_id = ?",
                listOf(r.error.value, outageId, r.targetId),
            )
            if (changed == 0) {
                db.insert("INSERT INTO outage_targets(outage_id, target_id, failure_count, last_error) VALUES (?, ?, 1, ?)",
                    listOf(outageId, r.targetId, r.error.value))
            }
        }
    }

    fun closeOutage(outageId: Long, endedAt: Double, status: String, endObserved: Boolean,
                    recoveryDetectedAt: Double? = null, evidence: JSONArray? = null) {
        db.update(
            "UPDATE outages SET ended_at = MAX(started_at, ?), duration = MAX(0, ? - started_at), status = ?, end_observed = ?, recovery_detected_at = ? WHERE id = ?",
            listOf(endedAt, endedAt, status, if (endObserved) 1 else 0, recoveryDetectedAt, outageId),
        )
        if (evidence != null) appendEvidence(outageId, evidence)
    }

    fun outagesBetween(start: Double, end: Double): List<Row> = db.query(
        "SELECT * FROM outages WHERE started_at < ? AND (ended_at IS NULL OR ended_at > ?) ORDER BY started_at",
        listOf(end, start),
    )

    fun outageTargets(outageIds: List<Long>): Map<Long, List<Row>> {
        val out = mutableMapOf<Long, MutableList<Row>>()
        outageIds.chunked(500).forEach { chunk ->
            val marks = chunk.joinToString(",") { "?" }
            db.query(
                "SELECT ot.*, t.name, t.address FROM outage_targets ot JOIN targets t ON t.id = ot.target_id WHERE outage_id IN ($marks)",
                chunk,
            ).forEach { out.getOrPut(it.l("outage_id")!!) { mutableListOf() }.add(it) }
        }
        return out
    }

    // ------------------------------------------------------------ measurements
    fun insertMeasurements(sessionId: Long, results: List<CheckResult>) {
        for (r in results) {
            if (r.targetId == null) continue
            db.insert(
                "INSERT INTO measurements(ts, session_id, target_id, check_type, success, latency_ms, packets_sent, packets_received, error_category, detail) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?)",
                listOf(r.timestamp, sessionId, r.targetId, r.checkType, if (r.success) 1 else 0, r.latencyMs,
                    if (r.success) 1 else 0, r.error.value, r.detail.take(500).ifBlank { null }),
            )
        }
    }

    fun measurementBuckets(start: Double, end: Double, bucket: Double): List<Row> = db.query(
        "SELECT target_id, CAST((ts - ?) / ? AS INTEGER) AS b, COUNT(*) AS n, SUM(success) AS ok, " +
            "AVG(CASE WHEN success THEN latency_ms END) AS avg_ms, MIN(CASE WHEN success THEN latency_ms END) AS min_ms, " +
            "MAX(CASE WHEN success THEN latency_ms END) AS max_ms FROM measurements WHERE ts >= ? AND ts < ? " +
            "GROUP BY target_id, b ORDER BY b",
        listOf(start, bucket, start, end),
    )

    fun latencyValues(start: Double, end: Double): List<Row> = db.query(
        "SELECT target_id, latency_ms FROM measurements WHERE ts >= ? AND ts < ? AND success = 1 AND latency_ms IS NOT NULL",
        listOf(start, end),
    )

    fun checkCounts(start: Double, end: Double): List<Row> = db.query(
        "SELECT target_id, COUNT(*) AS n, SUM(success) AS ok, MAX(CASE WHEN success THEN ts END) AS last_ok FROM measurements WHERE ts >= ? AND ts < ? GROUP BY target_id",
        listOf(start, end),
    )

    fun errorBreakdown(start: Double, end: Double): List<Row> = db.query(
        "SELECT target_id, error_category, COUNT(*) AS n FROM measurements WHERE ts >= ? AND ts < ? AND success = 0 GROUP BY target_id, error_category ORDER BY n DESC",
        listOf(start, end),
    )

    fun lastSuccess(): Double? = db.queryOne("SELECT MAX(ts) AS t FROM measurements WHERE success = 1")?.d("t")

    /** Delete raw measurements older than [cutoff]; outage and state history is kept. */
    fun pruneMeasurements(cutoff: Double): Int {
        var total = 0
        while (true) {
            val n = db.update("DELETE FROM measurements WHERE id IN (SELECT id FROM measurements WHERE ts < ? LIMIT 5000)", listOf(cutoff))
            total += n
            if (n < 5000) return total
        }
    }

    // ------------------------------------------------------------ events
    fun logEvent(kind: String, message: String = "", sessionId: Long? = null, data: JSONObject? = null, ts: Double? = null) {
        db.insert("INSERT INTO application_events(ts, kind, session_id, message, data) VALUES (?, ?, ?, ?, ?)",
            listOf(ts ?: clock(), kind, sessionId, message, data?.toString()))
    }

    fun eventsBetween(start: Double, end: Double): List<Row> =
        db.query("SELECT * FROM application_events WHERE ts >= ? AND ts < ? ORDER BY ts", listOf(start, end))

    // ------------------------------------------------------------ deletes
    data class DeleteResult(val states: Int, val outages: Int, val measurements: Int)

    fun deleteRange(start: Double, end: Double): DeleteResult = db.tx {
        val s = db.update("DELETE FROM connection_states WHERE ended_at IS NOT NULL AND started_at >= ? AND ended_at <= ?", listOf(start, end))
        val o = db.update("DELETE FROM outages WHERE ended_at IS NOT NULL AND started_at >= ? AND ended_at <= ?", listOf(start, end))
        val m = db.update("DELETE FROM measurements WHERE ts >= ? AND ts <= ?", listOf(start, end))
        db.update("DELETE FROM monitoring_sessions WHERE status != 'running' AND started_at >= ? AND ended_at <= ?", listOf(start, end))
        logEvent("history_deleted", "Deleted history in a time range", data = JSONObject().put("start", start).put("end", end))
        DeleteResult(s, o, m)
    }

    fun deleteAll(keepSessionId: Long?, now: Double) {
        db.tx {
            if (keepSessionId != null) {
                db.update("DELETE FROM measurements WHERE session_id = ?", listOf(keepSessionId))
                db.update("DELETE FROM connection_states WHERE session_id = ? AND ended_at IS NOT NULL", listOf(keepSessionId))
                db.update("UPDATE connection_states SET started_at = ? WHERE session_id = ? AND ended_at IS NULL", listOf(now, keepSessionId))
                db.update("DELETE FROM outages WHERE status != 'ongoing'")
                db.update("UPDATE outages SET started_at = ?, start_observed = 0 WHERE status = 'ongoing'", listOf(now))
                db.update("UPDATE monitoring_sessions SET started_at = ? WHERE id = ?", listOf(now, keepSessionId))
                db.update("DELETE FROM measurements WHERE session_id != ?", listOf(keepSessionId))
                db.update("DELETE FROM connection_states WHERE session_id != ?", listOf(keepSessionId))
                db.update("DELETE FROM monitoring_sessions WHERE id != ?", listOf(keepSessionId))
            } else {
                db.update("DELETE FROM measurements")
                db.update("DELETE FROM connection_states")
                db.update("DELETE FROM outages")
                db.update("DELETE FROM monitoring_sessions")
            }
            db.update("DELETE FROM application_events")
            logEvent("history_cleared", "All history was cleared by the user", ts = now)
        }
    }

    fun deleteState(stateId: Long) {
        db.tx {
            val row = db.queryOne("SELECT * FROM connection_states WHERE id = ?", listOf(stateId)) ?: return@tx
            val end = row.d("ended_at") ?: return@tx
            val start = row.d("started_at")!!
            db.update("DELETE FROM measurements WHERE session_id = ? AND ts >= ? AND ts < ?", listOf(row.l("session_id"), start, end))
            db.update("DELETE FROM outages WHERE ended_at IS NOT NULL AND started_at >= ? AND ended_at <= ?", listOf(start, end))
            db.update("DELETE FROM connection_states WHERE id = ?", listOf(stateId))
            logEvent("history_deleted", "Deleted one event", data = JSONObject().put("state_id", stateId))
        }
    }

    fun counts(): Map<String, Long> = listOf("monitoring_sessions", "measurements", "connection_states", "outages",
        "application_events", "targets").associateWith { db.queryOne("SELECT COUNT(*) AS n FROM $it")?.l("n") ?: 0 }
}
