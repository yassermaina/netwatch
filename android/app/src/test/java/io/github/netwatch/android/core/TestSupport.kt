package io.github.netwatch.android.core

import kotlinx.coroutines.Dispatchers
import java.io.File
import java.sql.Connection
import java.sql.DriverManager
import java.sql.PreparedStatement
import java.sql.Statement

/** JDBC implementation of [SqlDb] so repository SQL is tested against a real SQLite engine. */
class JdbcSqlDb(file: File) : SqlDb {
    private val conn: Connection = DriverManager.getConnection("jdbc:sqlite:${file.path}").apply {
        createStatement().use { it.execute("PRAGMA foreign_keys = ON") }
        autoCommit = true
    }
    private var depth = 0

    private fun prepare(sql: String, args: List<Any?>, keys: Boolean = false): PreparedStatement {
        val st = if (keys) conn.prepareStatement(sql, Statement.RETURN_GENERATED_KEYS) else conn.prepareStatement(sql)
        args.forEachIndexed { i, a ->
            when (a) {
                null -> st.setObject(i + 1, null)
                is Boolean -> st.setInt(i + 1, if (a) 1 else 0)
                is Int -> st.setInt(i + 1, a)
                is Long -> st.setLong(i + 1, a)
                is Double -> st.setDouble(i + 1, a)
                is Float -> st.setDouble(i + 1, a.toDouble())
                else -> st.setString(i + 1, a.toString())
            }
        }
        return st
    }

    @Synchronized override fun exec(sql: String, args: List<Any?>) { prepare(sql, args).use { it.execute() } }

    @Synchronized override fun insert(sql: String, args: List<Any?>): Long = prepare(sql, args, true).use {
        it.executeUpdate()
        it.generatedKeys.use { k -> if (k.next()) k.getLong(1) else -1 }
    }

    @Synchronized override fun update(sql: String, args: List<Any?>): Int = prepare(sql, args).use { it.executeUpdate() }

    @Synchronized override fun query(sql: String, args: List<Any?>): List<Row> = prepare(sql, args).use { st ->
        st.executeQuery().use { rs ->
            val md = rs.metaData
            val out = mutableListOf<Row>()
            while (rs.next()) {
                out += (1..md.columnCount).associate { i -> md.getColumnLabel(i) to rs.getObject(i) }
            }
            out
        }
    }

    @Synchronized override fun <T> tx(block: () -> T): T {
        if (depth == 0) conn.autoCommit = false
        depth++
        try {
            val r = block()
            depth--
            if (depth == 0) { conn.commit(); conn.autoCommit = true }
            return r
        } catch (e: Throwable) {
            depth--
            if (depth == 0) { conn.rollback(); conn.autoCommit = true }
            throw e
        }
    }

    override fun close() = conn.close()
}

class TestClock(var t: Double = 1_700_000_000.0) : Clocks {
    var m = 1000.0
    override fun now() = t
    override fun mono() = m
    fun advance(s: Double) { t += s; m += s }
}

/** Scripted results per address: true = success, or an ErrorCategory. */
class FakeAdapter(private val clock: TestClock, override val checkType: String = "icmp") : CheckAdapter {
    val outcomes = mutableMapOf<String, Any>()
    override fun check(target: Target, timeoutSeconds: Double): CheckResult {
        val o = outcomes[target.address] ?: true
        return if (o == true) CheckResult(target.id, target.address, checkType, true, clock.now(), latencyMs = 12.5)
        else {
            val e = o as? ErrorCategory ?: ErrorCategory.TIMEOUT
            CheckResult(target.id, target.address, checkType, false, clock.now(), error = e, detail = e.label)
        }
    }
}

class FakeProbe : NetworkProbe {
    var status = LocalNetworkStatus(connected = true, source = "test", transport = "wifi")
    override fun probe() = status
}

class Harness(dir: File) {
    val clock = TestClock()
    val db = JdbcSqlDb(File(dir, "test.db")).also { Schema.migrate(it, clock.now()) }
    val repo = Repository(db) { clock.now() }.also { it.ensureDefaultTargets(Defaults.targets.take(3)) }
    val settings = SettingsStore(db) { clock.now() }
    val stats = ReliabilityService(repo)
    val adapter = FakeAdapter(clock)
    val probe = FakeProbe()
    val engine = MonitorEngine(repo, settings, mapOf("icmp" to adapter), probe, clock, "test", Dispatchers.Unconfined)
    val addresses get() = repo.listTargets().map { it.address }

    suspend fun run(n: Int = 1, step: Double = 5.0): List<Transition> {
        val out = mutableListOf<Transition>()
        repeat(n) {
            engine.tick()?.let { out += it }
            clock.advance(step)
        }
        return out
    }

    fun allFail(outcome: Any = false) = addresses.forEach { adapter.outcomes[it] = outcome }
    fun allOk() = addresses.forEach { adapter.outcomes[it] = true }
    fun outages() = db.query("SELECT * FROM outages ORDER BY id")
    fun states() = db.query("SELECT * FROM connection_states ORDER BY id")
}
