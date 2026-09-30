package com.aaditpani.nora.voice

import android.annotation.SuppressLint
import android.media.AudioFormat
import android.media.AudioRecord
import android.media.MediaRecorder
import android.media.audiofx.AcousticEchoCanceler
import android.util.Log
import kotlin.math.log10
import kotlin.math.sqrt

/**
 * Listens while NORA speaks, for the user talking over her.
 *
 * The microphone is opened as a call would open it (`VOICE_COMMUNICATION`),
 * which is what gets the platform's echo canceller, plus [AcousticEchoCanceler]
 * where offered. What's left of NORA's own voice is learnt as the floor over
 * the first moments; the user counts as talking when the level stays [margin]
 * dB above that floor for [holdFrames] frames of 20 ms.
 *
 * Opening a speaker phone's microphone next to its own speaker is the case
 * this can get wrong, so the margin is larger there (see [VoiceController]).
 */
class BargeIn(private val margin: Double, private val onSpeech: () -> Unit) {
    @Volatile private var running = false
    private var thread: Thread? = null

    @SuppressLint("MissingPermission")   // the voice session only starts with RECORD_AUDIO
    fun start() {
        if (running) return
        running = true
        thread = Thread({ loop() }, "nora-barge-in").also { it.start() }
    }

    /** Stop and wait (briefly) for the microphone to be released, so the recogniser can have it. */
    fun stop() {
        running = false
        thread?.join(150)
        thread = null
    }

    @SuppressLint("MissingPermission")
    private fun loop() {
        val rate = 16_000
        val frame = rate / 50                       // 20 ms
        val min = AudioRecord.getMinBufferSize(rate, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT)
        val rec = try {
            AudioRecord(MediaRecorder.AudioSource.VOICE_COMMUNICATION, rate,
                AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT, maxOf(min, frame * 2 * 10))
        } catch (e: Exception) {
            Log.w(TAG, "no microphone for barge-in: ${e.message}")
            return
        }
        if (rec.state != AudioRecord.STATE_INITIALIZED) {
            rec.release()
            return
        }
        val aec = if (AcousticEchoCanceler.isAvailable())
            runCatching { AcousticEchoCanceler.create(rec.audioSessionId)?.apply { enabled = true } }.getOrNull()
        else null
        val buf = ShortArray(frame)
        val detector = Detector(margin)
        try {
            rec.startRecording()
            while (running) {
                val n = rec.read(buf, 0, frame)
                if (n <= 0) continue
                if (detector.feed(dbfs(buf, n))) {
                    running = false
                    onSpeech()
                }
            }
        } finally {
            runCatching { rec.stop() }
            aec?.release()
            rec.release()
        }
    }

    /** The decision, apart from the microphone, so it can be tested on the JVM. */
    class Detector(private val margin: Double, private val holdFrames: Int = 8,
                   private val warmupFrames: Int = 15, private val absoluteMin: Double = -42.0) {
        private var floor = Double.NaN
        private var seen = 0
        private var above = 0

        /** Feed one frame's level in dBFS; true when the user is talking. */
        fun feed(db: Double): Boolean {
            seen++
            floor = when {
                floor.isNaN() -> db
                // Learn fast downward, slowly upward: NORA's echo sets the floor,
                // a word from the user must not.
                db < floor -> floor * 0.7 + db * 0.3
                else -> floor * 0.98 + db * 0.02
            }
            if (seen <= warmupFrames) return false
            above = if (db > absoluteMin && db > floor + margin) above + 1 else 0
            return above >= holdFrames
        }
    }

    companion object {
        private const val TAG = "NoraBargeIn"

        fun dbfs(buf: ShortArray, n: Int): Double {
            var sum = 0.0
            for (i in 0 until n) sum += buf[i].toDouble() * buf[i]
            val rms = sqrt(sum / n) / 32768.0
            return if (rms <= 1e-9) -120.0 else 20 * log10(rms)
        }
    }
}
