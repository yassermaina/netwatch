package io.github.netwatch.android.core

import org.json.JSONArray
import org.json.JSONObject
import java.util.concurrent.CopyOnWriteArrayList

object Defaults {
    val values: Map<String, Any> = linkedMapOf(
        // Monitoring
        "interval_seconds" to 10,
        "timeout_seconds" to 3,
        "failure_threshold" to 2,
        "recovery_threshold" to 2,
        "latency_warning_ms" to 150,
        "loss_warning_pct" to 5,
        "start_monitoring_on_launch" to true,
        // Alerts
        "sound_enabled" to true,
        "sound_volume" to 30,
        "sound_repeat" to true,
        "sound_repeat_seconds" to 15,
        "sound_recovery" to true,
        "resume_alert_on_unmute" to true,
        "vibrate" to true,
        "start_muted" to false,
        "muted" to false,
        // Notifications
        "notify_outage" to true,
        "notify_recovery" to true,
        "notify_errors" to true,
        // Appearance: system | light | dark
        "theme" to "system",
        // Background
        "start_on_boot" to false,
        // Data
        "retention_days" to 90,
    )

    val targets = listOf(
        Triple("Cloudflare DNS", "1.1.1.1", "icmp"),
        Triple("Google DNS", "8.8.8.8", "icmp"),
        Triple("Quad9 DNS", "9.9.9.9", "icmp"),
        Triple("Cloudflare (TCP 443)", "1.1.1.1:443", "tcp"),
    )

    val checkTypes = linkedMapOf(
        "icmp" to "ICMP ping",
        "tcp" to "TCP connect (host:port)",
        "dns" to "DNS lookup",
        "http" to "HTTP(S) request",
    )
}

data class ValidationError(val key: String, val message: String)

object SettingsValidator {
    private val ranges = mapOf(
        "interval_seconds" to Triple(2, 3600, "Check interval"),
        "timeout_seconds" to Triple(1, 60, "Timeout"),
        "failure_threshold" to Triple(1, 20, "Failure threshold"),
        "recovery_threshold" to Triple(1, 20, "Recovery threshold"),
        "latency_warning_ms" to Triple(1, 10000, "High latency threshold"),
        "loss_warning_pct" to Triple(1, 100, "Packet loss threshold"),
        "sound_volume" to Triple(0, 100, "Volume"),
        "sound_repeat_seconds" to Triple(3, 3600, "Alert repeat interval"),
        "retention_days" to Triple(0, 3650, "Measurement retention"),
    )

    fun validate(values: Map<String, Any?>): List<ValidationError> {
        val errors = mutableListOf<ValidationError>()
        for ((key, r) in ranges) {
            val v = (values[key] as? Number)?.toInt()
            if (v == null || v < r.first || v > r.second) errors += ValidationError(key, "${r.third} must be between ${r.first} and ${r.second}.")
        }
        if (values["theme"] !in setOf("system", "light", "dark")) errors += ValidationError("theme", "Invalid theme.")
        val interval = (values["interval_seconds"] as? Number)?.toInt()
        val timeout = (values["timeout_seconds"] as? Number)?.toInt()
        if (interval != null && timeout != null && timeout >= interval) {
            errors += ValidationError("timeout_seconds",
                "Timeout (${timeout}s) must be shorter than the check interval (${interval}s), otherwise checks would overlap.")
        }
        return errors
    }

    private val hostname = Regex("^(?=.{1,253}$)(?!-)[A-Za-z0-9-]{1,63}(?<!-)(\\.(?!-)[A-Za-z0-9-]{1,63}(?<!-))*\\.?$")
    private val ipv4 = Regex("^((25[0-5]|2[0-4]\\d|1\\d\\d|[1-9]?\\d)\\.){3}(25[0-5]|2[0-4]\\d|1\\d\\d|[1-9]?\\d)$")

    fun isIp(v: String): Boolean = ipv4.matches(v) || (v.contains(':') && v.all { it.isLetterOrDigit() || it == ':' || it == '.' } &&
        v.count { it == ':' } >= 2)

    fun validHost(v: String): Boolean = when {
        isIp(v) -> true
        Regex("[\\d.]+").matches(v) -> false // looks like IPv4 but is not one
        else -> hostname.matches(v)
    }

    /** Splits "host:port" / "[v6]:port". Returns null when the port is missing or invalid. */
    fun hostPort(v: String): Pair<String, Int>? {
        val m = Regex("^\\[(.+)]:(\\d+)$").find(v) ?: Regex("^([^:]+):(\\d+)$").find(v) ?: return null
        val port = m.groupValues[2].toIntOrNull() ?: return null
        return if (port in 1..65535) m.groupValues[1] to port else null
    }

    fun validateTarget(name: String, address: String, checkType: String): List<String> {
        val problems = mutableListOf<String>()
        val a = address.trim()
        if (name.isBlank()) problems += "Give the target a name."
        if (checkType !in Defaults.checkTypes) problems += "Unknown check type."
        if (a.isEmpty()) {
            problems += "Enter an address."
        } else when (checkType) {
            "http" -> if (!Regex("^https?://[^\\s/]+.*").matches(a) && !validHost(a)) problems += "Enter a URL such as https://example.com or a host name."
            "dns" -> if (!validHost(a) || isIp(a)) problems += "DNS checks need a host name to resolve, such as example.com."
            "tcp" -> {
                val hp = hostPort(a)
                if (hp == null || !validHost(hp.first)) problems += "Enter host:port, for example 1.1.1.1:443."
            }
            else -> if (!validHost(a)) problems += "Enter a valid IPv4/IPv6 address or host name."
        }
        return problems
    }
}

/** Thread-safe cached access to the settings table (values stored as JSON, same as the Linux app). */
class SettingsStore(private val db: SqlDb, private val clock: () -> Double) {
    private val lock = Any()
    private var cache: Map<String, Any> = Defaults.values
    private val listeners = CopyOnWriteArrayList<(Set<String>) -> Unit>()

    init { reload() }

    fun reload() {
        val values = LinkedHashMap(Defaults.values)
        for (row in db.query("SELECT key, value FROM settings")) {
            val key = row.s("key") ?: continue
            val default = Defaults.values[key] ?: continue
            val parsed = decode(row.s("value"), default) ?: continue
            values[key] = parsed
        }
        synchronized(lock) { cache = values }
    }

    private fun decode(raw: String?, default: Any): Any? = try {
        val v = JSONArray("[$raw]").get(0)
        when (default) {
            is Boolean -> v as? Boolean
            is Int -> (v as? Number)?.toInt()
            is String -> v as? String
            else -> v
        }
    } catch (e: Exception) {
        null
    }

    private fun encode(v: Any?): String = JSONArray().put(v).toString().removePrefix("[").removeSuffix("]")

    fun all(): Map<String, Any> = synchronized(lock) { HashMap(cache) }
    fun int(key: String): Int = (all()[key] as? Number)?.toInt() ?: (Defaults.values[key] as Int)
    fun bool(key: String): Boolean = all()[key] as? Boolean ?: (Defaults.values[key] as Boolean)
    fun string(key: String): String = all()[key] as? String ?: (Defaults.values[key] as String)

    fun addListener(l: (Set<String>) -> Unit) { listeners += l }
    fun removeListener(l: (Set<String>) -> Unit) { listeners -= l }

    /** Persist [changes] atomically if the resulting settings are valid. */
    fun update(changes: Map<String, Any>, validateAll: Boolean = true): List<ValidationError> {
        val unknown = changes.keys - Defaults.values.keys
        require(unknown.isEmpty()) { "Unknown settings: $unknown" }
        val merged = all() + changes
        if (validateAll) {
            val errors = SettingsValidator.validate(merged)
            if (errors.isNotEmpty()) return errors
        }
        val now = clock()
        db.tx {
            for ((k, v) in changes) {
                // INSERT OR REPLACE instead of UPSERT for SQLite < 3.24 (Android 8-10)
                db.exec("INSERT OR REPLACE INTO settings(key, value, updated_at) VALUES (?, ?, ?)", listOf(k, encode(v), now))
            }
        }
        synchronized(lock) { cache = cache + changes }
        listeners.forEach { it(changes.keys) }
        return emptyList()
    }

    fun set(key: String, value: Any) { update(mapOf(key to value), validateAll = false) }

    fun snapshotJson(): JSONObject = JSONObject().apply { all().forEach { (k, v) -> put(k, v) } }
}
