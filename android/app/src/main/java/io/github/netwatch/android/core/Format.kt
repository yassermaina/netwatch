package io.github.netwatch.android.core

import java.time.Instant
import java.time.LocalDate
import java.time.LocalDateTime
import java.time.ZoneId
import java.time.ZoneOffset
import java.time.format.DateTimeFormatter
import java.time.format.FormatStyle
import java.util.Locale
import kotlin.math.roundToLong

/** Display formatting. Persistence uses UTC epoch seconds; these convert to local time for display only. */
object Fmt {
    var zone: ZoneId = ZoneId.systemDefault()
    var locale: Locale = Locale.getDefault()
    var use24h: Boolean = true

    fun duration(seconds: Double?, precise: Boolean = false): String {
        if (seconds == null) return "—"
        val s = maxOf(0L, seconds.roundToLong())
        val d = s / 86400
        val h = (s % 86400) / 3600
        val m = (s % 3600) / 60
        val sec = s % 60
        return when {
            d > 0 -> "${d}d ${h}h" + if (precise && m > 0) " ${m}m" else ""
            h > 0 -> if (m > 0 || !precise) "${h}h ${m}m" else "${h}h"
            m > 0 -> "${m}m" + if (precise && sec > 0) " ${sec}s" else ""
            else -> "${sec}s"
        }
    }

    fun durationLong(seconds: Double?): String {
        if (seconds == null) return "—"
        val s = maxOf(0L, seconds.roundToLong())
        val d = s / 86400
        val h = (s % 86400) / 3600
        val m = (s % 3600) / 60
        return when {
            d > 0 -> "${d}d ${h}h"
            h > 0 -> "${h}h ${m}m"
            m > 0 -> "${m}m"
            else -> "${s}s"
        }
    }

    fun percent(v: Double?, digits: Int = 2): String = when {
        v == null -> "—"
        v >= 100 || v <= 0 -> String.format(Locale.US, "%.0f%%", v)
        else -> String.format(Locale.US, "%.${digits}f%%", v)
    }

    fun localDateTime(ts: Double): LocalDateTime = LocalDateTime.ofInstant(Instant.ofEpochMilli((ts * 1000).toLong()), zone)
    fun localDate(ts: Double): LocalDate = localDateTime(ts).toLocalDate()

    fun clock(ts: Double, seconds: Boolean = false): String {
        val pattern = if (use24h) (if (seconds) "HH:mm:ss" else "HH:mm") else (if (seconds) "h:mm:ss a" else "h:mm a")
        return localDateTime(ts).format(DateTimeFormatter.ofPattern(pattern, locale))
    }

    fun day(ts: Double, now: Double): String {
        val d = localDate(ts)
        val today = localDate(now)
        return when {
            d == today -> "Today"
            d == today.minusDays(1) -> "Yesterday"
            d.year == today.year -> d.format(DateTimeFormatter.ofPattern("MMM d", locale))
            else -> d.format(DateTimeFormatter.ofPattern("MMM d, yyyy", locale))
        }
    }

    /** "Today, 4:50 PM" / "Mar 27, 8:39 AM"; null means an ongoing period. */
    fun timestamp(ts: Double?, now: Double, seconds: Boolean = false): String =
        if (ts == null) "now" else "${day(ts, now)}, ${clock(ts, seconds)}"

    fun isoLocal(ts: Double?): String = ts?.let {
        localDateTime(it).atZone(zone).toOffsetDateTime().withNano(0).toString()
    } ?: ""

    fun isoUtc(ts: Double?): String = ts?.let {
        LocalDateTime.ofEpochSecond(it.toLong(), 0, ZoneOffset.UTC).toString() + "Z"
    } ?: ""

    fun reportTime(ts: Double?): String = ts?.let {
        localDateTime(it).format(DateTimeFormatter.ofPattern("yyyy-MM-dd HH:mm:ss"))
    } ?: "now"

    fun dateLong(d: LocalDate): String = d.format(DateTimeFormatter.ofLocalizedDate(FormatStyle.FULL).withLocale(locale))

    fun axisLabel(ts: Double, span: Double): String =
        if (span <= 2 * 86400) clock(ts) else localDate(ts).format(DateTimeFormatter.ofPattern("MMM d", locale))

    fun dayStart(d: LocalDate): Double = d.atStartOfDay(zone).toEpochSecond().toDouble()
    fun localDayStart(ts: Double): Double = dayStart(localDate(ts))
}
