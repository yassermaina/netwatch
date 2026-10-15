package io.github.netwatch.android.platform

import io.github.netwatch.android.core.CheckAdapter
import io.github.netwatch.android.core.CheckResult
import io.github.netwatch.android.core.ErrorCategory
import io.github.netwatch.android.core.SettingsValidator
import io.github.netwatch.android.core.Target
import java.io.File
import java.net.ConnectException
import java.net.HttpURLConnection
import java.net.InetAddress
import java.net.InetSocketAddress
import java.net.NoRouteToHostException
import java.net.Socket
import java.net.SocketTimeoutException
import java.net.URL
import java.net.UnknownHostException
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit
import java.util.concurrent.TimeoutException
import kotlin.math.ceil

private fun now() = System.currentTimeMillis() / 1000.0

private fun result(t: Target, type: String, ok: Boolean, started: Double, latency: Double? = null,
                   error: ErrorCategory = ErrorCategory.OK, detail: String = "") =
    CheckResult(t.id, t.address, type, ok, started, latency, if (ok) ErrorCategory.OK else error, detail.take(200))

/** Maps socket exceptions to error categories; EPERM usually means the system blocks our network access. */
internal fun classifySocketError(e: Throwable): Pair<ErrorCategory, String> {
    val msg = (e.message ?: e.javaClass.simpleName)
    val lower = msg.lowercase()
    return when {
        e is UnknownHostException -> ErrorCategory.DNS to "could not resolve host name"
        e is SocketTimeoutException || e is TimeoutException -> ErrorCategory.TIMEOUT to "no reply before timeout"
        e is SecurityException -> ErrorCategory.PERMISSION to "INTERNET permission missing"
        "eperm" in lower || "operation not permitted" in lower -> ErrorCategory.BLOCKED to "network access restricted by the system"
        "enetunreach" in lower || "network is unreachable" in lower -> ErrorCategory.NETWORK_DOWN to "network is unreachable"
        e is NoRouteToHostException || "ehostunreach" in lower -> ErrorCategory.UNREACHABLE to "no route to host"
        else -> ErrorCategory.OTHER to msg
    }
}

private val timeRe = Regex("time[=<]([\\d.]+)\\s*ms")

/** Classify a failed ping run from its exit code and output. */
internal fun classifyPingFailure(code: Int, output: String): Pair<ErrorCategory, String> {
    val text = output.lowercase()
    return when {
        "operation not permitted" in text || "permission denied" in text -> ErrorCategory.BLOCKED to "ping is not permitted (system restriction)"
        "unknown host" in text || "name or service not known" in text -> ErrorCategory.DNS to "could not resolve host name"
        "network is unreachable" in text -> ErrorCategory.NETWORK_DOWN to "network is unreachable"
        "unreachable" in text -> ErrorCategory.UNREACHABLE to "destination unreachable"
        code == 1 -> ErrorCategory.TIMEOUT to "no reply before timeout"
        else -> ErrorCategory.OTHER to (output.trim().lines().lastOrNull() ?: "ping exited with status $code")
    }
}

/** ICMP echo through the system ping binary (available to apps on Android). */
class IcmpAdapter : CheckAdapter {
    override val checkType = "icmp"
    private val ping = listOf("/system/bin/ping", "/system/xbin/ping").firstOrNull { File(it).exists() }
    private val ping6 = listOf("/system/bin/ping6", "/system/xbin/ping6").firstOrNull { File(it).exists() }

    val available: Boolean get() = ping != null

    override fun check(target: Target, timeoutSeconds: Double): CheckResult {
        val started = now()
        val v6 = target.address.contains(':')
        val bin = (if (v6) ping6 ?: ping else ping)
            ?: return result(target, checkType, false, started, error = ErrorCategory.MISSING_DEPENDENCY, detail = "ping not available on this device")
        val wait = maxOf(1, ceil(timeoutSeconds).toInt())
        val t0 = System.nanoTime()
        val proc = try {
            ProcessBuilder(bin, "-n", "-c", "1", "-W", wait.toString(), target.address).redirectErrorStream(true).start()
        } catch (e: Exception) {
            return result(target, checkType, false, started, error = ErrorCategory.MISSING_DEPENDENCY, detail = "cannot run ping: ${e.message}")
        }
        try {
            if (!proc.waitFor(wait + 3L, TimeUnit.SECONDS)) {
                return result(target, checkType, false, started, error = ErrorCategory.TIMEOUT, detail = "ping process timed out")
            }
            val out = proc.inputStream.bufferedReader().readText()
            val elapsed = (System.nanoTime() - t0) / 1e6
            if (proc.exitValue() == 0) {
                val latency = timeRe.find(out)?.groupValues?.get(1)?.toDoubleOrNull() ?: elapsed
                return result(target, checkType, true, started, latency)
            }
            val (cat, detail) = classifyPingFailure(proc.exitValue(), out)
            return result(target, checkType, false, started, error = cat, detail = detail)
        } finally {
            proc.destroy()
        }
    }
}

/** TCP handshake to host:port. A refused connection still proves the host answered. */
class TcpAdapter : CheckAdapter {
    override val checkType = "tcp"

    override fun check(target: Target, timeoutSeconds: Double): CheckResult {
        val started = now()
        val (host, port) = SettingsValidator.hostPort(target.address)
            ?: return result(target, checkType, false, started, error = ErrorCategory.OTHER, detail = "address must be host:port")
        val timeoutMs = (timeoutSeconds * 1000).toInt()
        val address = try {
            resolve(host, timeoutMs)
        } catch (e: Exception) {
            val (cat, d) = classifySocketError(if (e is TimeoutException) UnknownHostException("DNS timeout") else e)
            return result(target, checkType, false, started, error = cat, detail = d)
        }
        val t0 = System.nanoTime()
        return try {
            Socket().use { it.connect(InetSocketAddress(address, port), timeoutMs) }
            result(target, checkType, true, started, (System.nanoTime() - t0) / 1e6)
        } catch (e: ConnectException) {
            if ((e.message ?: "").contains("ECONNREFUSED") || (e.message ?: "").contains("refused", true)) {
                result(target, checkType, true, started, (System.nanoTime() - t0) / 1e6, detail = "connection refused (host reachable)")
            } else {
                val (cat, d) = classifySocketError(e)
                result(target, checkType, false, started, error = cat, detail = d)
            }
        } catch (e: Exception) {
            val (cat, d) = classifySocketError(e)
            result(target, checkType, false, started, error = cat, detail = d)
        }
    }
}

private val resolverPool = Executors.newFixedThreadPool(4) { r -> Thread(r, "nw-dns").apply { isDaemon = true } }

/** System resolver with a real timeout (InetAddress has none). */
internal fun resolve(host: String, timeoutMs: Int): InetAddress =
    try {
        resolverPool.submit<InetAddress> { InetAddress.getByName(host) }.get(timeoutMs.toLong(), TimeUnit.MILLISECONDS)
    } catch (e: java.util.concurrent.ExecutionException) {
        throw (e.cause as? Exception) ?: e
    }

class DnsAdapter : CheckAdapter {
    override val checkType = "dns"

    override fun check(target: Target, timeoutSeconds: Double): CheckResult {
        val started = now()
        val t0 = System.nanoTime()
        return try {
            resolve(target.address, (timeoutSeconds * 1000).toInt())
            result(target, checkType, true, started, (System.nanoTime() - t0) / 1e6)
        } catch (e: TimeoutException) {
            result(target, checkType, false, started, error = ErrorCategory.TIMEOUT, detail = "DNS lookup timed out")
        } catch (e: Exception) {
            val (cat, d) = classifySocketError(e)
            result(target, checkType, false, started, error = cat, detail = d)
        }
    }
}

/** HEAD request; any HTTP response below 500 proves reachability. */
class HttpAdapter : CheckAdapter {
    override val checkType = "http"

    override fun check(target: Target, timeoutSeconds: Double): CheckResult {
        val started = now()
        val url = if ("://" in target.address) target.address else "https://${target.address}"
        val t0 = System.nanoTime()
        var conn: HttpURLConnection? = null
        return try {
            conn = (URL(url).openConnection() as HttpURLConnection).apply {
                requestMethod = "HEAD"
                connectTimeout = (timeoutSeconds * 1000).toInt()
                readTimeout = (timeoutSeconds * 1000).toInt()
                instanceFollowRedirects = false
                useCaches = false
                setRequestProperty("User-Agent", "NetWatch connectivity check")
            }
            val code = conn.responseCode
            val latency = (System.nanoTime() - t0) / 1e6
            if (code >= 500) result(target, checkType, false, started, latency, ErrorCategory.HTTP_ERROR, "HTTP $code")
            else result(target, checkType, true, started, latency, detail = "HTTP $code")
        } catch (e: Exception) {
            val (cat, d) = classifySocketError(e)
            result(target, checkType, false, started, error = cat, detail = d)
        } finally {
            conn?.disconnect()
        }
    }
}

fun buildAdapters(): Map<String, CheckAdapter> =
    listOf(IcmpAdapter(), TcpAdapter(), DnsAdapter(), HttpAdapter()).associateBy { it.checkType }
