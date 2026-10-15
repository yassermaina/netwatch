package io.github.netwatch.android.ui

import android.content.Context
import android.content.res.Configuration
import android.graphics.Color
import io.github.netwatch.android.core.ConnState

/** Colours from the NetWatch reference design (dark navy first), plus a light variant. */
data class Palette(
    val dark: Boolean,
    val bg: Int,
    val surface: Int,
    val bar: Int,
    val fg: Int,
    val muted: Int,
    val line: Int,
    val accent: Int,
    val onAccent: Int,
    val online: Int,
    val minor: Int,
    val noInternet: Int,
    val major: Int,
    val notConnected: Int,
    val unknown: Int,
    val unmonitored: Int,
    val hatch: Int,
    val error: Int,
) {
    fun state(s: ConnState): Int = when (s) {
        ConnState.ONLINE -> online
        ConnState.NO_INTERNET -> noInternet
        ConnState.NOT_CONNECTED -> notConnected
        ConnState.UNKNOWN -> unknown
        ConnState.PAUSED, ConnState.NOT_RUNNING -> unmonitored
    }

    fun category(c: String): Int = when (c) {
        "online" -> online
        "minor" -> minor
        "no_internet" -> noInternet
        "major" -> major
        "not_connected" -> notConnected
        else -> unmonitored
    }

    val series: List<Int> get() = listOf(Color.rgb(59, 141, 246), online, noInternet, Color.rgb(191, 90, 242), Color.rgb(0, 191, 202), Color.rgb(255, 69, 109))

    companion object {
        val DARK = Palette(
            dark = true, bg = 0xFF1F2231.toInt(), surface = 0xFF262A3A.toInt(), bar = 0xFF191C29.toInt(),
            fg = 0xFFF2F2F7.toInt(), muted = 0xFF9A9BA8.toInt(), line = 0x1FFFFFFF, accent = 0xFF3B7CF6.toInt(),
            onAccent = Color.WHITE, online = 0xFF34C759.toInt(), minor = 0xFFA0C93B.toInt(), noInternet = 0xFFFF9F0A.toInt(),
            major = 0xFFF4781A.toInt(), notConnected = 0xFF8E8E93.toInt(), unknown = 0xFF7D7AFF.toInt(),
            unmonitored = 0x38DCDCE0, hatch = 0x8C9E9EA8.toInt(), error = 0xFFFF453A.toInt(),
        )
        val LIGHT = Palette(
            dark = false, bg = 0xFFFAFAFC.toInt(), surface = Color.WHITE, bar = Color.WHITE,
            fg = 0xFF1C1C1E.toInt(), muted = 0xFF6E6E73.toInt(), line = 0x1A000000, accent = 0xFF1C6BE0.toInt(),
            onAccent = Color.WHITE, online = 0xFF26A644.toInt(), minor = 0xFF8BB423.toInt(), noInternet = 0xFFE88A00.toInt(),
            major = 0xFFE0620A.toInt(), notConnected = 0xFF808087.toInt(), unknown = 0xFF5E5CE6.toInt(),
            unmonitored = 0x33A0A0A8, hatch = 0x99999AA2.toInt(), error = 0xFFD70015.toInt(),
        )

        /** Resolve from the user's theme setting (system | light | dark). */
        fun resolve(context: Context, setting: String): Palette = when (setting) {
            "dark" -> DARK
            "light" -> LIGHT
            else -> if ((context.resources.configuration.uiMode and Configuration.UI_MODE_NIGHT_MASK) == Configuration.UI_MODE_NIGHT_YES) DARK else LIGHT
        }
    }
}
