package io.github.netwatch.android.core

import org.json.JSONObject

/**
 * Connection / coverage state of a period of time (same model as NetWatch for Linux).
 *
 * ONLINE, NO_INTERNET and NOT_CONNECTED are network measurements. UNKNOWN means monitoring ran without
 * enough evidence. PAUSED and NOT_RUNNING are coverage states: nothing was measured.
 */
enum class ConnState(val value: String, val label: String, val shortLabel: String) {
    ONLINE("online", "Online", "Online"),
    NO_INTERNET("no_internet", "No Internet (network connected)", "No Internet"),
    NOT_CONNECTED("not_connected", "Not Connected", "Not Connected"),
    UNKNOWN("unknown", "Unknown", "Unknown"),
    PAUSED("paused", "Monitoring Paused", "Paused"),
    NOT_RUNNING("not_running", "App Not Running", "App Not Running");

    val isDown: Boolean get() = this == NO_INTERNET || this == NOT_CONNECTED
    val isMonitored: Boolean get() = this == ONLINE || isDown

    companion object {
        fun of(value: String?): ConnState = entries.firstOrNull { it.value == value } ?: UNKNOWN
    }
}

/** State of the monitoring engine itself, independent of connectivity. */
enum class Lifecycle(val label: String) { STOPPED("Stopped"), STARTING("Starting"), RUNNING("Running"), PAUSED("Paused"), ERROR("Error") }

enum class ErrorCategory(val value: String, val label: String) {
    OK("ok", "OK"),
    TIMEOUT("timeout", "Timed out"),
    UNREACHABLE("unreachable", "Destination unreachable"),
    DNS("dns", "Name resolution failed"),
    NETWORK_DOWN("network_down", "Network unreachable"),
    PERMISSION("permission", "Permission denied"),
    MISSING_DEPENDENCY("missing_dependency", "Check not available on this device"),
    BLOCKED("blocked", "Network access restricted by the system"),
    HTTP_ERROR("http_error", "HTTP error"),
    OTHER("other", "Other error");

    /** Errors that say nothing about the network, only about this device. */
    val isDiagnostic: Boolean get() = this == PERMISSION || this == MISSING_DEPENDENCY || this == BLOCKED

    companion object {
        fun of(value: String?): ErrorCategory = entries.firstOrNull { it.value == value } ?: OTHER
    }
}

data class Target(
    val id: Long?,
    val name: String,
    val address: String,
    val checkType: String = "icmp",
    val enabled: Boolean = true,
    val position: Int = 0,
) {
    val display: String get() = if (name.isNotBlank() && name != address) "$name ($address)" else address
}

data class CheckResult(
    val targetId: Long?,
    val address: String,
    val checkType: String,
    val success: Boolean,
    val timestamp: Double,
    val latencyMs: Double? = null,
    val error: ErrorCategory = ErrorCategory.OK,
    val detail: String = "",
)

data class WifiDetails(
    val ssid: String? = null,
    val frequencyMhz: Int? = null,
    val rssiDbm: Int? = null,
    val standard: String? = null,
    val linkSpeedMbps: Int? = null,
) {
    val channel: Int? get() = frequencyMhz?.let { frequencyToChannel(it) }
    val band: String? get() = frequencyMhz?.let { if (it < 3000) "2.4 GHz" else if (it < 5925) "5 GHz" else "6 GHz" }
}

fun frequencyToChannel(f: Int): Int? = when {
    f in 2412..2472 -> (f - 2407) / 5
    f == 2484 -> 14
    f in 5150..5925 -> (f - 5000) / 5
    f in 5955..7115 -> (f - 5950) / 5
    else -> null
}

/** What the device knows about its own network attachment. Only observed fields are filled in. */
data class LocalNetworkStatus(
    val connected: Boolean?,
    val source: String = "unknown",
    val transport: String? = null, // wifi | cellular | ethernet | vpn | other
    val validated: Boolean? = null,
    val blocked: Boolean = false, // system restricts this app's network access (Doze, data saver)
    val wifi: WifiDetails? = null,
    val detail: String = "",
) {
    fun describe(): String = when (transport) {
        "wifi" -> {
            val w = wifi ?: WifiDetails()
            val parts = listOfNotNull(
                w.ssid, w.channel?.let { "Ch $it" }, w.band, w.standard, w.rssiDbm?.let { "$it dBm" },
            )
            if (parts.isEmpty()) "Wi-Fi" else "Wi-Fi (${parts.joinToString(", ")})"
        }
        "cellular" -> "Mobile data"
        "ethernet" -> "Ethernet"
        "vpn" -> "VPN"
        null -> ""
        else -> transport.replaceFirstChar { it.uppercase() }
    }

    fun toJson(): String = JSONObject().apply {
        put("connected", connected ?: JSONObject.NULL)
        put("source", source)
        put("transport", transport ?: JSONObject.NULL)
        put("validated", validated ?: JSONObject.NULL)
        put("blocked", blocked)
        put("detail", detail)
        wifi?.let { w ->
            put("wifi", JSONObject().apply {
                put("ssid", w.ssid ?: JSONObject.NULL)
                put("frequency_mhz", w.frequencyMhz ?: JSONObject.NULL)
                put("rssi_dbm", w.rssiDbm ?: JSONObject.NULL)
                put("standard", w.standard ?: JSONObject.NULL)
                put("link_speed_mbps", w.linkSpeedMbps ?: JSONObject.NULL)
            })
        }
    }.toString()

    companion object {
        fun fromJson(raw: String?): LocalNetworkStatus? {
            if (raw.isNullOrBlank()) return null
            return try {
                val o = JSONObject(raw)
                fun JSONObject.optStr(k: String) = if (isNull(k) || !has(k)) null else getString(k)
                fun JSONObject.optIntOrNull(k: String) = if (isNull(k) || !has(k)) null else getInt(k)
                fun JSONObject.optBoolOrNull(k: String) = if (isNull(k) || !has(k)) null else getBoolean(k)
                val w = if (o.has("wifi") && !o.isNull("wifi")) o.getJSONObject("wifi") else null
                LocalNetworkStatus(
                    connected = o.optBoolOrNull("connected"),
                    source = o.optString("source", "unknown"),
                    transport = o.optStr("transport"),
                    validated = o.optBoolOrNull("validated"),
                    blocked = o.optBoolean("blocked", false),
                    detail = o.optString("detail", ""),
                    wifi = w?.let {
                        WifiDetails(it.optStr("ssid"), it.optIntOrNull("frequency_mhz"), it.optIntOrNull("rssi_dbm"),
                            it.optStr("standard"), it.optIntOrNull("link_speed_mbps"))
                    },
                )
            } catch (e: Exception) {
                null
            }
        }
    }
}

data class Transition(
    val previous: ConnState,
    val new: ConnState,
    val effectiveAt: Double, // first round supporting the new state
    val detectedAt: Double, // when the threshold was met
    val reason: String = "",
)

data class TargetLive(
    val targetId: Long?,
    val name: String,
    val address: String,
    val success: Boolean,
    val latencyMs: Double?,
    val error: String,
)

/** Live view of the engine published to the UI after every round. */
data class EngineSnapshot(
    val lifecycle: Lifecycle = Lifecycle.STOPPED,
    val state: ConnState = ConnState.UNKNOWN,
    val stateSince: Double? = null,
    val sessionId: Long? = null,
    val sessionStarted: Double? = null,
    val lastCheckAt: Double? = null,
    val lastSuccessAt: Double? = null,
    val lastLatencyMs: Double? = null,
    val lastResults: List<TargetLive> = emptyList(),
    val local: LocalNetworkStatus? = null,
    val outageId: Long? = null,
    val pendingState: ConnState? = null,
    val reason: String = "",
    val message: String = "",
)
