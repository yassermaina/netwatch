package io.github.netwatch.android.core

import kotlin.math.PI
import kotlin.math.sin

interface SoundPlayer {
    val available: Boolean

    fun play(kind: String, volume: Int)
    fun stop()
    fun isPlaying(): Boolean
}

/** Decides when to play sounds (driven by state changes and a 1 s tick). Never affects monitoring. */
class AlertManager(private val player: SoundPlayer, private val settings: SettingsStore, private val clock: () -> Double) {
    var muted = settings.bool("start_muted") || settings.bool("muted"); private set
    var outageActive = false; private set
    private var nextRepeat: Double? = null
    val plays = mutableListOf<String>()

    private fun audible() = settings.bool("sound_enabled") && !muted && player.available

    private fun play(kind: String) {
        plays += kind
        player.play(kind, settings.int("sound_volume"))
    }

    private fun scheduleRepeat(now: Double) {
        nextRepeat = if (settings.bool("sound_repeat")) now + settings.int("sound_repeat_seconds") else null
    }

    fun outageStarted() {
        if (outageActive) return // one alert per outage, not per failed check
        outageActive = true
        if (audible()) play("outage")
        scheduleRepeat(clock())
    }

    fun outageEnded(recovered: Boolean = true) {
        if (!outageActive) return
        outageActive = false
        nextRepeat = null
        player.stop()
        if (recovered && audible() && settings.bool("sound_recovery")) play("recovery")
    }

    fun setMuted(value: Boolean) {
        if (value == muted) return
        muted = value
        if (value) {
            player.stop()
            return
        }
        if (outageActive && settings.bool("resume_alert_on_unmute")) {
            if (audible()) play("outage")
            scheduleRepeat(clock())
        } else if (outageActive) {
            nextRepeat = null // stay quiet for the rest of this outage
        }
    }

    fun tick() {
        val due = nextRepeat ?: return
        if (!outageActive) return
        val now = clock()
        if (now < due) return
        if (audible() && !player.isPlaying()) play("outage")
        scheduleRepeat(now)
    }

    fun shutdown() {
        outageActive = false
        nextRepeat = null
        player.stop()
    }
}

/** 16-bit mono PCM: soft low "heartbeat" double thump for outages, gentle rising chime for recovery. */
object Synth {
    const val RATE = 22050

    fun samples(kind: String, volume: Int): ShortArray {
        val gain = volume.coerceIn(0, 100) / 100.0 * 0.9
        val out = ArrayList<Double>()
        fun tone(freqs: List<Double>, seconds: Double, amp: Double, release: Double = 0.6) {
            val n = (RATE * seconds).toInt()
            val a = (n * 0.02).toInt()
            val r = (n * release).toInt()
            for (i in 0 until n) {
                val t = i.toDouble() / RATE
                val env = when {
                    i < a -> i.toDouble() / maxOf(1, a)
                    i > n - r -> maxOf(0.0, (n - i).toDouble() / maxOf(1, r))
                    else -> 1.0
                }
                out += freqs.sumOf { sin(2 * PI * it * t) } / freqs.size * amp * env
            }
        }
        fun silence(seconds: Double) = repeat((RATE * seconds).toInt()) { out += 0.0 }
        when (kind) {
            "outage" -> { tone(listOf(70.0, 140.0), 0.13, 1.0, 0.8); silence(0.09); tone(listOf(62.0, 124.0), 0.16, 0.8, 0.85); silence(0.15) }
            "recovery" -> { tone(listOf(660.0, 1320.0), 0.12, 0.35); silence(0.03); tone(listOf(880.0, 1760.0), 0.22, 0.35, 0.8) }
            else -> tone(listOf(523.0), 0.2, 0.4)
        }
        return ShortArray(out.size) { (out[it] * gain).coerceIn(-1.0, 1.0).times(32767).toInt().toShort() }
    }
}
