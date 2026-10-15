package io.github.netwatch.android.platform

import android.content.Context
import android.net.ConnectivityManager
import android.net.Network
import android.net.NetworkCapabilities
import android.net.wifi.WifiInfo
import android.net.wifi.WifiManager
import android.os.Build
import android.os.PowerManager
import io.github.netwatch.android.core.LocalNetworkStatus
import io.github.netwatch.android.core.NetworkProbe
import io.github.netwatch.android.core.WifiDetails

/**
 * Local network status from ConnectivityManager. Wi-Fi details are only reported when the system exposes
 * them; the SSID needs location permission, which NetWatch does not request, so it is normally omitted.
 */
class AndroidNetworkProbe(context: Context) : NetworkProbe {
    private val app = context.applicationContext
    private val cm = app.getSystemService(ConnectivityManager::class.java)
    private val wifi = app.getSystemService(WifiManager::class.java)
    private val power = app.getSystemService(PowerManager::class.java)

    @Volatile private var blockedBySystem = false
    private val callback = object : ConnectivityManager.NetworkCallback() {
        override fun onBlockedStatusChanged(network: Network, blocked: Boolean) { blockedBySystem = blocked }
        override fun onLost(network: Network) { blockedBySystem = false }
    }

    fun register() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) runCatching { cm.registerDefaultNetworkCallback(callback) }
    }

    fun unregister() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) runCatching { cm.unregisterNetworkCallback(callback) }
    }

    private fun idleRestricted(): Boolean =
        power.isDeviceIdleMode && !power.isIgnoringBatteryOptimizations(app.packageName)

    override fun probe(): LocalNetworkStatus {
        val blocked = blockedBySystem || idleRestricted()
        val net = cm.activeNetwork
            ?: return LocalNetworkStatus(connected = false, source = "connectivity_manager", blocked = blocked,
                detail = "no active network")
        val caps = cm.getNetworkCapabilities(net)
            ?: return LocalNetworkStatus(connected = null, source = "connectivity_manager", blocked = blocked,
                detail = "network capabilities unavailable")
        val transport = when {
            caps.hasTransport(NetworkCapabilities.TRANSPORT_VPN) -> "vpn"
            caps.hasTransport(NetworkCapabilities.TRANSPORT_WIFI) -> "wifi"
            caps.hasTransport(NetworkCapabilities.TRANSPORT_CELLULAR) -> "cellular"
            caps.hasTransport(NetworkCapabilities.TRANSPORT_ETHERNET) -> "ethernet"
            else -> "other"
        }
        val validated = caps.hasCapability(NetworkCapabilities.NET_CAPABILITY_VALIDATED)
        val internet = caps.hasCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET)
        return LocalNetworkStatus(
            connected = internet,
            source = "connectivity_manager",
            transport = transport,
            validated = validated,
            blocked = blocked,
            wifi = if (transport == "wifi") wifiDetails(caps) else null,
            detail = "Android reports ${if (validated) "a validated" else "an unvalidated"} $transport network",
        )
    }

    @Suppress("DEPRECATION")
    private fun wifiDetails(caps: NetworkCapabilities): WifiDetails? {
        val info: WifiInfo? = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) caps.transportInfo as? WifiInfo
        else runCatching { wifi.connectionInfo }.getOrNull()
        info ?: return null
        val ssid = info.ssid?.trim('"')?.takeIf { it.isNotBlank() && it != WifiManager.UNKNOWN_SSID.trim('<', '>') && !it.startsWith("<") }
        val standard = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) when (info.wifiStandard) {
            4 -> "802.11n"; 5 -> "802.11ac"; 6 -> "802.11ax"; 8 -> "802.11be"; 1 -> "802.11a/b/g"; else -> null
        } else null
        return WifiDetails(
            ssid = ssid,
            frequencyMhz = info.frequency.takeIf { it > 0 },
            rssiDbm = info.rssi.takeIf { it in -126..-1 },
            standard = standard,
            linkSpeedMbps = info.linkSpeed.takeIf { it > 0 },
        )
    }
}
