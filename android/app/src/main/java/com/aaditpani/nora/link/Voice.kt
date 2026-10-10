package com.aaditpani.nora.link

import org.json.JSONArray
import org.json.JSONObject

/** Which stream of PCM carries a `say` line in NORA's voice (core `nora/hub/voice.py`). */
data class AudioSpec(val stream: Int, val rate: Int) {
    companion object {
        const val FORMAT = "pcm_s16le"

        /** Null for a line with no audio, or audio in a format this app can't play. */
        fun parse(obj: JSONObject?): AudioSpec? {
            if (obj == null) return null
            val stream = obj.optInt("stream", -1)
            val rate = obj.optInt("rate", 0)
            if (stream < 0 || obj.optString("format") != FORMAT || rate !in 8_000..48_000) return null
            return AudioSpec(stream, rate)
        }
    }
}

/** `0x01 · stream (u32 BE) · PCM s16le mono`. */
object PcmFrame {
    const val TAG_PCM: Byte = 0x01

    fun parse(frame: ByteArray): Pair<Int, ByteArray>? {
        if (frame.size < 5 || frame[0] != TAG_PCM) return null
        var stream = 0
        for (i in 1..4) stream = (stream shl 8) or (frame[i].toInt() and 0xFF)
        if (stream < 0) return null
        return stream to frame.copyOfRange(5, frame.size)
    }
}

/**
 * Where a voice turn's answer is actually played. One segment at a time:
 * either a stream of PCM ([startPcm], [writePcm]…, [finishPcm]) or a line
 * for the phone's own text-to-speech ([speak]). The implementation calls
 * [SpeechQueue.segmentDone] when the segment has finished playing, and
 * [SpeechQueue.soundStarted] when the first of it is heard.
 */
interface VoiceOutput {
    fun startPcm(rate: Int)
    fun writePcm(pcm: ByteArray)
    /** No more PCM for this segment; report done once it has drained. */
    fun finishPcm()
    fun speak(text: String)
    /** Cut off whatever is playing, now. No segmentDone follows. */
    fun stop()
}

/**
 * A voice turn's answer, in the order NORA said it. Each `say` line becomes a
 * segment: PCM from the core when it sends it, the phone's own voice when it
 * doesn't. PCM that arrives before its line's turn to play is held.
 *
 * A line whose audio ends with nothing sent (synthesis failed on the core) is
 * spoken by the phone instead, so an answer is never silently lost. After
 * [stop] (barge-in) nothing more of this turn is played.
 *
 * Thread-safe: frames arrive on the link's thread, completions on the
 * player's.
 */
class SpeechQueue(private val out: VoiceOutput, private val clock: () -> Long = System::currentTimeMillis) {
    private sealed class Segment {
        abstract val text: String
        class Pcm(val spec: AudioSpec, override val text: String) : Segment() {
            val held = ArrayList<ByteArray>()
            var ended = false
            var sent = false
            /** Ended with nothing sent: once it has (emptily) finished, speak it here. */
            var fallBack = false
        }
        class Tts(override val text: String) : Segment()
        /** The phone's own "One sec" while the core is slow: not the answer. */
        class Ack(override val text: String) : Segment()
    }

    private val queue = ArrayDeque<Segment>()
    private val byStream = HashMap<Int, Segment.Pcm>()
    private var playing: Segment? = null
    private var stopped = false
    private var turnDone = false

    /** When the first sound of this turn was heard; null until then. */
    @Volatile var firstSoundAt: Long? = null
        private set
    /** Which voice was heard first: "core" or "phone". */
    @Volatile var firstVoice: String? = null
        private set
    /** When the acknowledgement was heard, if one was; never counts as first sound. */
    @Volatile var ackSoundAt: Long? = null
        private set
    private var answered = false
    /** Called once everything said has been played and the turn is over. */
    var onIdle: (() -> Unit)? = null

    @Synchronized
    fun line(text: String, audio: AudioSpec?) {
        if (stopped || text.isBlank()) return
        answered = true
        val seg = if (audio != null) Segment.Pcm(audio, text).also { byStream[audio.stream] = it }
        else Segment.Tts(text)
        queue.addLast(seg)
        if (playing == null) next()
    }

    @Synchronized
    fun audio(stream: Int, pcm: ByteArray) {
        if (stopped) return
        val seg = byStream[stream] ?: return
        if (seg.ended) return
        seg.sent = true
        if (playing === seg) out.writePcm(pcm) else seg.held.add(pcm)
    }

    @Synchronized
    fun audioEnd(stream: Int, ok: Boolean, sent: Boolean) {
        val seg = byStream.remove(stream) ?: return
        seg.ended = true
        // Nothing arrived at all: the core couldn't make it. Say it here instead.
        val fallBack = !seg.sent && !sent
        if (playing === seg) {
            seg.fallBack = fallBack
            out.finishPcm()
        } else if (fallBack) {
            val i = queue.indexOf(seg)
            if (i >= 0) queue[i] = Segment.Tts(seg.text)
        }
    }

    /**
     * Say [text] in the phone's voice while waiting for the core. Only before
     * any of the answer has arrived; the answer queues behind it. True if said.
     */
    @Synchronized
    fun ack(text: String): Boolean {
        if (stopped || answered || turnDone || playing != null) return false
        playing = Segment.Ack(text).also { out.speak(it.text) }
        return true
    }

    /** The core finished the turn. Idle comes once the last segment has played. */
    @Synchronized
    fun turnDone() {
        turnDone = true
        if (playing == null && queue.isEmpty()) fireIdle()
    }

    @Synchronized
    fun soundStarted(voice: String) {
        if (playing is Segment.Ack) {
            if (ackSoundAt == null) ackSoundAt = clock()
            return
        }
        if (firstSoundAt == null) {
            firstSoundAt = clock()
            firstVoice = voice
        }
    }

    @Synchronized
    fun segmentDone() {
        val done = playing
        if (!stopped && done is Segment.Pcm && done.fallBack) {
            playing = Segment.Tts(done.text).also { out.speak(it.text) }
            return
        }
        playing = null
        next()
    }

    /** Barge-in: stop now, drop the rest. True if anything was playing or waiting. */
    @Synchronized
    fun stop(): Boolean {
        val busy = playing != null || queue.isNotEmpty()
        stopped = true
        queue.clear()
        byStream.clear()
        playing = null
        out.stop()
        return busy
    }

    @get:Synchronized
    val busy: Boolean get() = playing != null || queue.isNotEmpty()

    @get:Synchronized
    val ackPlaying: Boolean get() = playing is Segment.Ack

    private fun next() {
        if (stopped) return
        val seg = queue.removeFirstOrNull()
        playing = seg
        when (seg) {
            null -> if (turnDone) fireIdle()
            is Segment.Tts, is Segment.Ack -> out.speak(seg.text)
            is Segment.Pcm -> {
                out.startPcm(seg.spec.rate)
                seg.held.forEach(out::writePcm)
                seg.held.clear()
                if (seg.ended) out.finishPcm()
            }
        }
    }

    private fun fireIdle() {
        val cb = onIdle
        onIdle = null
        cb?.invoke()
    }
}

/**
 * One spoken turn's timestamps, all from the phone's clock. [speechEnd] is
 * the last moment the microphone heard speech (the recogniser's own
 * end-of-speech comes later, after its silence timeout, which the user still
 * waits through, so it is not the start of the measurement).
 */
data class VoiceTiming(
    val speechEnd: Long,
    val recognised: Long,
    val firstSay: Long?,
    val firstSound: Long?,
    val tts: String,
    val route: String,
    val ackSound: Long? = null,
) {
    val firstAudioMs: Long? get() = firstSound?.let { it - speechEnd }

    fun toEvent(): JSONObject = JSONObject()
        .put("speech_end_ms", 0)
        .put("stt_ms", recognised - speechEnd)
        .put("core_first_say_ms", firstSay?.let { it - speechEnd } ?: JSONObject.NULL)
        .put("first_audio_ms", firstAudioMs ?: JSONObject.NULL)
        .put("ack_ms", ackSound?.let { it - speechEnd } ?: JSONObject.NULL)
        .put("tts", tts)
        .put("route", route)
}

/** The last [keep] measured turns, and their median: the Phase 6 exit number. */
class LatencyStats(private val keep: Int = 20) {
    private val rows = ArrayDeque<JSONObject>()

    @Synchronized
    fun add(t: VoiceTiming) {
        if (t.firstAudioMs == null) return
        rows.addLast(t.toEvent().put("at", t.speechEnd))
        while (rows.size > keep) rows.removeFirst()
    }

    @Synchronized
    fun count(): Int = rows.size

    /** Median first-audio over the kept turns, or null with none. */
    @Synchronized
    fun medianFirstAudio(): Long? = median(rows.map { it.getLong("first_audio_ms") })

    @Synchronized
    fun medianOf(field: String): Long? = median(rows.mapNotNull { r -> r.optLong(field, -1).takeIf { it >= 0 } })

    @Synchronized
    fun last(): JSONObject? = rows.lastOrNull()

    @Synchronized
    fun save(): String = JSONArray().also { a -> rows.forEach { a.put(it) } }.toString()

    @Synchronized
    fun load(json: String?) {
        rows.clear()
        val arr = runCatching { JSONArray(json ?: "[]") }.getOrNull() ?: return
        for (i in 0 until arr.length()) {
            val r = arr.optJSONObject(i) ?: continue
            if (r.has("first_audio_ms") && !r.isNull("first_audio_ms")) rows.addLast(r)
        }
        while (rows.size > keep) rows.removeFirst()
    }

    @Synchronized
    fun clear() = rows.clear()

    companion object {
        fun median(values: List<Long>): Long? {
            if (values.isEmpty()) return null
            val s = values.sorted()
            val n = s.size
            return if (n % 2 == 1) s[n / 2] else (s[n / 2 - 1] + s[n / 2]) / 2
        }
    }
}
