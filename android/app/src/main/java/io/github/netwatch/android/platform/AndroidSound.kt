package io.github.netwatch.android.platform

import android.content.Context
import android.media.AudioAttributes
import android.media.AudioFormat
import android.media.AudioTrack
import android.os.Build
import android.os.VibrationEffect
import android.os.Vibrator
import android.os.VibratorManager
import io.github.netwatch.android.core.SoundPlayer
import io.github.netwatch.android.core.Synth

/** Plays the synthesized heartbeat / recovery sounds through AudioTrack (notification stream). */
class AndroidSoundPlayer(private val context: Context, private val vibrateEnabled: () -> Boolean) : SoundPlayer {
    override val available = true
    private var track: AudioTrack? = null
    private val cache = HashMap<String, ShortArray>()

    @Synchronized override fun play(kind: String, volume: Int) {
        if (volume <= 0) return
        stop()
        val key = "$kind-${volume / 5 * 5}"
        val pcm = cache.getOrPut(key) { Synth.samples(kind, volume) }
        val t = AudioTrack.Builder()
            .setAudioAttributes(AudioAttributes.Builder().setUsage(AudioAttributes.USAGE_NOTIFICATION_EVENT)
                .setContentType(AudioAttributes.CONTENT_TYPE_SONIFICATION).build())
            .setAudioFormat(AudioFormat.Builder().setEncoding(AudioFormat.ENCODING_PCM_16BIT).setSampleRate(Synth.RATE)
                .setChannelMask(AudioFormat.CHANNEL_OUT_MONO).build())
            .setTransferMode(AudioTrack.MODE_STATIC)
            .setBufferSizeInBytes(pcm.size * 2)
            .build()
        t.write(pcm, 0, pcm.size)
        t.play()
        track = t
        if (kind == "outage" && vibrateEnabled()) vibrate()
    }

    @Synchronized override fun stop() {
        track?.let {
            runCatching { it.stop() }
            it.release()
        }
        track = null
    }

    @Synchronized override fun isPlaying(): Boolean {
        val t = track ?: return false
        val playing = t.playState == AudioTrack.PLAYSTATE_PLAYING && t.playbackHeadPosition < t.bufferSizeInFrames
        if (!playing) stop()
        return playing
    }

    private fun vibrate() {
        val v: Vibrator = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
            context.getSystemService(VibratorManager::class.java).defaultVibrator
        } else {
            @Suppress("DEPRECATION") context.getSystemService(Vibrator::class.java)
        }
        if (v.hasVibrator()) v.vibrate(VibrationEffect.createWaveform(longArrayOf(0, 90, 120, 120), -1))
    }
}
