package com.aaditpani.nora.voice

import android.content.Context
import android.media.AudioAttributes
import android.media.AudioFormat
import android.media.AudioTrack
import android.os.Bundle
import android.speech.tts.TextToSpeech
import android.speech.tts.UtteranceProgressListener
import android.util.Log
import com.aaditpani.nora.link.VoiceOutput
import java.util.Locale
import java.util.concurrent.Executors
import java.util.concurrent.atomic.AtomicInteger

/**
 * Plays a voice turn on the phone: NORA's PCM through an [AudioTrack], and
 * lines the core didn't voice through the phone's own [TextToSpeech].
 *
 * Both play as the assistant (`USAGE_ASSISTANT`), so the system routes them
 * like any assistant's speech: to a connected Bluetooth or wired headset,
 * else the speaker, and ducks music underneath.
 *
 * PCM writes happen on one thread, non-blocking in small steps, so [stop]
 * (barge-in) cuts the sound at once even mid-write. Every segment has a
 * generation; a stop bumps it, and nothing from an older generation reports
 * done.
 */
class PhoneVoiceOutput(context: Context, private val events: Events) : VoiceOutput {
    interface Events {
        fun soundStarted(voice: String)
        fun segmentDone()
    }

    private val attrs = AudioAttributes.Builder()
        .setUsage(AudioAttributes.USAGE_ASSISTANT)
        .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH)
        .build()
    private val writer = Executors.newSingleThreadExecutor { Thread(it, "nora-voice-out") }
    private val gen = AtomicInteger()

    // Owned by the writer thread.
    private var track: AudioTrack? = null
    private var trackRate = 0
    private var framesWritten = 0L
    private var heard = false

    private var tts: TextToSpeech? = null
    @Volatile private var ttsReady = false
    @Volatile private var pendingSpeak: Pair<Int, String>? = null
    @Volatile private var speakingId: String? = null

    init {
        tts = TextToSpeech(context.applicationContext) { status ->
            val engine = tts ?: return@TextToSpeech
            if (status != TextToSpeech.SUCCESS) {
                Log.w(TAG, "phone TTS unavailable ($status)")
                return@TextToSpeech
            }
            engine.setAudioAttributes(attrs)
            // NORA is British (en-GB Sonia/Emma on the core). Prefer an
            // on-device en-GB voice; the network ones add a round trip.
            val gb = engine.voices?.filter { it.locale == Locale.UK && !it.isNetworkConnectionRequired }
                ?.minByOrNull { it.latency }
            if (gb != null) engine.voice = gb else engine.language = Locale.UK
            engine.setSpeechRate(1.1f)
            engine.setOnUtteranceProgressListener(object : UtteranceProgressListener() {
                override fun onStart(id: String) {
                    if (id == speakingId) events.soundStarted("phone")
                }
                override fun onDone(id: String) = finished(id)
                @Deprecated("Deprecated in Java")
                override fun onError(id: String) = finished(id)
                override fun onError(id: String, errorCode: Int) = finished(id)
            })
            ttsReady = true
            pendingSpeak?.let { (g, text) -> if (g == gen.get()) speakNow(text) }
            pendingSpeak = null
        }
    }

    private fun finished(id: String) {
        if (id == speakingId) {
            speakingId = null
            events.segmentDone()
        }
    }

    // ── PCM ──────────────────────────────────────────────────────────────────

    override fun startPcm(rate: Int) {
        val g = gen.get()
        writer.execute {
            if (g != gen.get()) return@execute
            if (track == null || trackRate != rate) {
                releaseTrack()
                val min = AudioTrack.getMinBufferSize(rate, AudioFormat.CHANNEL_OUT_MONO, AudioFormat.ENCODING_PCM_16BIT)
                track = AudioTrack.Builder()
                    .setAudioAttributes(attrs)
                    .setAudioFormat(AudioFormat.Builder()
                        .setEncoding(AudioFormat.ENCODING_PCM_16BIT)
                        .setSampleRate(rate)
                        .setChannelMask(AudioFormat.CHANNEL_OUT_MONO).build())
                    .setTransferMode(AudioTrack.MODE_STREAM)
                    .setBufferSizeInBytes(maxOf(min * 2, rate / 5 * 2))   // ~200 ms
                    .setPerformanceMode(AudioTrack.PERFORMANCE_MODE_LOW_LATENCY)
                    .build()
                trackRate = rate
                framesWritten = 0
            }
            heard = false
            track?.play()
        }
    }

    override fun writePcm(pcm: ByteArray) {
        val g = gen.get()
        writer.execute {
            val t = track ?: return@execute
            var off = 0
            while (off < pcm.size && g == gen.get()) {
                val n = t.write(pcm, off, pcm.size - off, AudioTrack.WRITE_NON_BLOCKING)
                if (n < 0) return@execute
                if (n > 0 && !heard) {
                    heard = true
                    events.soundStarted("core")
                }
                off += n
                if (n == 0) Thread.sleep(8)
            }
            framesWritten += off / 2
        }
    }

    override fun finishPcm() {
        val g = gen.get()
        writer.execute {
            val t = track
            if (t != null) {
                // Wait for what was written to be heard, not just handed over.
                val deadline = System.currentTimeMillis() + 60_000
                while (g == gen.get() && System.currentTimeMillis() < deadline &&
                    (t.playbackHeadPosition.toLong() and 0xFFFFFFFFL) < framesWritten) Thread.sleep(15)
            }
            if (g == gen.get()) events.segmentDone()
        }
    }

    // ── phone TTS ────────────────────────────────────────────────────────────

    override fun speak(text: String) {
        if (ttsReady) speakNow(text) else pendingSpeak = gen.get() to text
    }

    private fun speakNow(text: String) {
        val id = "nora-${System.nanoTime()}"
        speakingId = id
        val r = tts?.speak(text, TextToSpeech.QUEUE_FLUSH, Bundle(), id)
        if (r != TextToSpeech.SUCCESS) finished(id)
    }

    // ── both ─────────────────────────────────────────────────────────────────

    override fun stop() {
        gen.incrementAndGet()
        pendingSpeak = null
        speakingId = null
        tts?.stop()
        // Silence now, from this thread; the writer notices the new generation.
        runCatching { track?.pause(); track?.flush() }
        writer.execute { releaseTrack() }
    }

    private fun releaseTrack() {
        runCatching { track?.stop() }
        track?.release()
        track = null
        framesWritten = 0
    }

    fun shutdown() {
        stop()
        tts?.shutdown()
        tts = null
        writer.shutdown()
    }

    companion object {
        private const val TAG = "NoraVoiceOut"
    }
}
