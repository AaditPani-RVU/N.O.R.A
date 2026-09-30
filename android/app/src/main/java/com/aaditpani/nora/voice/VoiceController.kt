package com.aaditpani.nora.voice

import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.media.AudioAttributes
import android.media.AudioFocusRequest
import android.media.AudioManager
import android.os.Bundle
import android.speech.RecognitionListener
import android.speech.RecognitionService
import android.speech.RecognizerIntent
import android.speech.SpeechRecognizer
import android.util.Log
import com.aaditpani.nora.link.AudioSpec
import com.aaditpani.nora.link.LatencyStats
import com.aaditpani.nora.link.SpeechQueue
import com.aaditpani.nora.link.VoiceTiming
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import org.json.JSONObject
import java.util.Locale

enum class VoicePhase { OFF, LISTENING, THINKING, SPEAKING }

data class VoiceUi(
    val phase: VoicePhase = VoicePhase.OFF,
    /** What the recogniser has heard so far, while listening. */
    val partial: String = "",
    /** Microphone level, 0..1, while listening. */
    val level: Float = 0f,
    /** Why the last session ended, when it wasn't simply finished. */
    val note: String? = null,
)

/**
 * A voice session: listen, send what was heard, speak the answer, and listen
 * again for a follow-up until the user goes quiet (plan §6, Phase 6).
 *
 *  - **Hearing** is the phone's own on-device recogniser. Only its text
 *    leaves the phone, as an ordinary turn marked as spoken.
 *  - **Speaking** is NORA's own voice from the core, streamed as it is made,
 *    or the phone's text-to-speech (a setting, and the fallback whenever the
 *    core sends no audio for a line).
 *  - **Barge-in**: while she speaks, the microphone listens for the user
 *    talking over her; that, or a tap, stops her at once, tells the core to
 *    stop making audio, and listens.
 *
 * A session only starts from something the user did on a visible screen
 * (the app, the quick-settings tile, the assistant gesture or a headset
 * button, all of which open the app), which is what Android 14+ requires
 * of a microphone service, and what keeps this from ever being an open mic.
 */
class VoiceController(private val app: Context, private val host: Host) {
    /** What the session needs from the link. */
    interface Host {
        /** Send a spoken turn; its id, or null when the link is down. */
        fun sendVoice(text: String, tts: String): String?
        fun bargeIn(utteranceId: String)
        fun emitTiming(data: JSONObject)
        val connected: Boolean
        var tts: String
        var bargeInOn: Boolean
        var followUp: Boolean
        var savedStats: String?
    }

    private val main = CoroutineScope(SupervisorJob() + Dispatchers.Main)
    private val route = AudioRoute(app)
    private val am = app.getSystemService(AudioManager::class.java)
    private val focus = AudioFocusRequest.Builder(AudioManager.AUDIOFOCUS_GAIN_TRANSIENT)
        .setAudioAttributes(AudioAttributes.Builder()
            .setUsage(AudioAttributes.USAGE_ASSISTANT)
            .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH).build())
        .build()

    private val _ui = MutableStateFlow(VoiceUi())
    val ui: StateFlow<VoiceUi> = _ui
    val stats = LatencyStats().also { it.load(host.savedStats) }
    private val _statsTick = MutableStateFlow(0)
    val statsTick: StateFlow<Int> = _statsTick

    private var recognizer: SpeechRecognizer? = null
    private var onDeviceFailed = false
    private var bargeIn: BargeIn? = null
    private var output: PhoneVoiceOutput? = null

    /** One spoken turn in flight. */
    private inner class Turn(val id: String, val speechEnd: Long, val recognised: Long, val tts: String) {
        var firstSay: Long? = null
        var recorded = false
        val queue = SpeechQueue(outputFor())
    }
    @Volatile private var turn: Turn? = null
    private var followingUp = false

    val active: Boolean get() = _ui.value.phase != VoicePhase.OFF

    // ── the session ──────────────────────────────────────────────────────────

    /** Start listening. From a visible screen only (see the class note). */
    fun start() = main.launch {
        if (active) return@launch
        if (!host.connected) {
            _ui.value = VoiceUi(note = "Not connected to the core.")
            return@launch
        }
        VoiceService.start(app)
        outputFor()   // the phone's TTS engine takes a moment to start; not on the first answer
        am.requestAudioFocus(focus)
        _ui.value = VoiceUi(VoicePhase.LISTENING)
        followingUp = false
        listen()
    }

    /** The mic button: start, interrupt NORA, or stop listening. */
    fun toggle() = main.launch {
        when (_ui.value.phase) {
            VoicePhase.OFF -> start()
            VoicePhase.SPEAKING -> interrupt()
            else -> end(null)
        }
    }

    /** Stop NORA mid-answer and listen: a tap, or the user talking over her. */
    fun interrupt() = main.launch {
        val t = turn ?: return@launch
        if (_ui.value.phase != VoicePhase.SPEAKING && _ui.value.phase != VoicePhase.THINKING) return@launch
        stopBargeIn()
        t.queue.stop()
        host.bargeIn(t.id)
        record(t)
        turn = null
        followingUp = true
        listen()
    }

    fun stop() = main.launch { end(null) }

    private suspend fun listen() {
        _ui.update { it.copy(phase = VoicePhase.LISTENING, partial = "", level = 0f, note = null) }
        route.holdHeadsetMic()
        if (!active) return
        val rec = if (!onDeviceFailed && SpeechRecognizer.isOnDeviceRecognitionAvailable(app))
            SpeechRecognizer.createOnDeviceSpeechRecognizer(app)
        else SpeechRecognizer.createSpeechRecognizer(app, otherRecogniser())
        recognizer?.destroy()
        recognizer = rec
        var lastLoud = 0L
        var lastPartial = 0L
        var endOfSpeech = 0L
        var heardAny = false
        rec.setRecognitionListener(object : RecognitionListener {
            override fun onReadyForSpeech(params: Bundle?) {}
            override fun onBeginningOfSpeech() { heardAny = true }
            override fun onRmsChanged(rmsdB: Float) {
                _ui.update { it.copy(level = ((rmsdB + 2f) / 12f).coerceIn(0f, 1f)) }
                if (heardAny && rmsdB > LOUD_RMS_DB) lastLoud = System.currentTimeMillis()
            }
            override fun onBufferReceived(buffer: ByteArray?) {}
            override fun onEndOfSpeech() { endOfSpeech = System.currentTimeMillis() }
            override fun onPartialResults(partialResults: Bundle?) {
                val text = partialResults?.getStringArrayList(SpeechRecognizer.RESULTS_RECOGNITION)?.firstOrNull().orEmpty()
                if (text.isNotBlank() && text != _ui.value.partial) {
                    lastPartial = System.currentTimeMillis()
                    heardAny = true
                    _ui.update { it.copy(partial = text) }
                }
            }
            override fun onResults(results: Bundle?) {
                val now = System.currentTimeMillis()
                val text = results?.getStringArrayList(SpeechRecognizer.RESULTS_RECOGNITION)?.firstOrNull().orEmpty().trim()
                // The last moment speech was heard: the loud frames if there
                // were any, else the last new words, else the recogniser's end.
                val speechEnd = listOf(lastLoud, lastPartial, endOfSpeech, now).first { it > 0 }
                main.launch { heard(text, speechEnd, now) }
            }
            override fun onError(error: Int) {
                main.launch { recogniserFailed(error) }
            }
            override fun onEvent(eventType: Int, params: Bundle?) {}
        })
        rec.startListening(Intent(RecognizerIntent.ACTION_RECOGNIZE_SPEECH)
            .putExtra(RecognizerIntent.EXTRA_LANGUAGE_MODEL, RecognizerIntent.LANGUAGE_MODEL_FREE_FORM)
            .putExtra(RecognizerIntent.EXTRA_LANGUAGE, Locale.getDefault().toLanguageTag())
            .putExtra(RecognizerIntent.EXTRA_PARTIAL_RESULTS, true)
            .putExtra(RecognizerIntent.EXTRA_PREFER_OFFLINE, true)
            .putExtra(RecognizerIntent.EXTRA_SPEECH_INPUT_COMPLETE_SILENCE_LENGTH_MILLIS, SILENCE_MS)
            .putExtra(RecognizerIntent.EXTRA_SPEECH_INPUT_POSSIBLY_COMPLETE_SILENCE_LENGTH_MILLIS, SILENCE_MS))
    }

    /**
     * Some other app's recogniser, for when the on-device one can't be used.
     * Not the default: with NORA as the digital assistant, the default is
     * NORA's own stub, which declines everything.
     */
    private fun otherRecogniser(): ComponentName? {
        val found = app.packageManager.queryIntentServices(Intent(RecognitionService.SERVICE_INTERFACE), 0)
            .map { it.serviceInfo }.filter { it.packageName != app.packageName }
        val best = found.firstOrNull { it.packageName == "com.google.android.as" }
            ?: found.firstOrNull { it.packageName == "com.google.android.googlequicksearchbox" }
            ?: found.firstOrNull()
        return best?.let { ComponentName(it.packageName, it.name) }
    }

    private fun heard(text: String, speechEnd: Long, recognised: Long) {
        route.release()
        recognizer?.destroy()
        recognizer = null
        if (!active) return
        if (text.isEmpty()) {
            end(if (followingUp) null else "Didn't catch that.")
            return
        }
        val tts = host.tts
        val id = host.sendVoice(text, tts)
        if (id == null) {
            end("Not connected to the core.")
            return
        }
        val t = Turn(id, speechEnd, recognised, tts)
        t.queue.onIdle = { main.launch { finished(t) } }
        turn = t
        _ui.update { it.copy(phase = VoicePhase.THINKING, level = 0f) }
    }

    private fun recogniserFailed(error: Int) {
        route.release()
        recognizer?.destroy()
        recognizer = null
        if (!active) return
        Log.i(TAG, "recogniser error $error")
        when (error) {
            // Silence: the end of a conversation, not a failure.
            SpeechRecognizer.ERROR_NO_MATCH, SpeechRecognizer.ERROR_SPEECH_TIMEOUT ->
                end(if (followingUp) null else "Didn't catch that.")
            SpeechRecognizer.ERROR_INSUFFICIENT_PERMISSIONS -> end("NORA needs the microphone permission.")
            SpeechRecognizer.ERROR_LANGUAGE_NOT_SUPPORTED, SpeechRecognizer.ERROR_LANGUAGE_UNAVAILABLE,
            SpeechRecognizer.ERROR_CANNOT_CHECK_SUPPORT -> if (!onDeviceFailed) {
                // On-device models missing for this language: use the regular one once.
                onDeviceFailed = true
                main.launch { listen() }
            } else end("Speech recognition isn't available in this language.")
            else -> end("Speech recognition failed ($error).")
        }
    }

    /** Everything NORA said has been played and the turn is over. */
    private fun finished(t: Turn) {
        if (turn !== t) return
        stopBargeIn()
        record(t)
        turn = null
        if (!active) return
        if (host.followUp) {
            followingUp = true
            main.launch {
                delay(150)   // let the last syllable clear the room before the mic opens
                if (active && turn == null) listen()
            }
        } else end(null)
    }

    private fun end(note: String?) {
        recognizer?.cancel()
        recognizer?.destroy()
        recognizer = null
        stopBargeIn()
        turn?.let { t ->
            t.queue.stop()
            record(t)
        }
        turn = null
        route.release()
        am.abandonAudioFocusRequest(focus)
        VoiceService.stop(app)
        _ui.value = VoiceUi(VoicePhase.OFF, note = note)
    }

    // ── from the link ────────────────────────────────────────────────────────

    /** A line of an answer. True when it belongs to this session's turn. */
    fun onSay(replyTo: String?, text: String, audio: AudioSpec?): Boolean {
        val t = turn ?: return false
        if (replyTo != t.id) return false
        if (t.firstSay == null) t.firstSay = System.currentTimeMillis()
        // The phone voice was asked for: ignore any audio (there shouldn't be any).
        t.queue.line(text, if (t.tts == TTS_CORE) audio else null)
        return true
    }

    fun onAudio(stream: Int, pcm: ByteArray) {
        turn?.queue?.audio(stream, pcm)
    }

    fun onAudioEnd(stream: Int, ok: Boolean, sent: Boolean) {
        turn?.queue?.audioEnd(stream, ok, sent)
    }

    fun onTurnDone(replyTo: String?): Boolean {
        val t = turn ?: return false
        if (replyTo != t.id) return false
        t.queue.turnDone()
        return true
    }

    fun isVoiceTurn(replyTo: String?): Boolean = replyTo != null && turn?.id == replyTo

    fun linkDropped() = main.launch {
        if (turn != null) end("The link to the core dropped.")
    }

    // ── playing ──────────────────────────────────────────────────────────────

    private fun outputFor(): PhoneVoiceOutput = output ?: PhoneVoiceOutput(app, object : PhoneVoiceOutput.Events {
        override fun soundStarted(voice: String) {
            turn?.queue?.soundStarted(voice)
            main.launch {
                if (turn != null && _ui.value.phase == VoicePhase.THINKING) {
                    _ui.update { it.copy(phase = VoicePhase.SPEAKING) }
                    startBargeIn()
                }
            }
        }

        override fun segmentDone() {
            turn?.queue?.segmentDone()
        }
    }).also { output = it }

    private fun startBargeIn() {
        if (!host.bargeInOn || bargeIn != null) return
        val margin = if (route.onSpeaker()) SPEAKER_MARGIN_DB else HEADSET_MARGIN_DB
        bargeIn = BargeIn(margin) { interrupt() }.also { it.start() }
    }

    private fun stopBargeIn() {
        bargeIn?.stop()
        bargeIn = null
    }

    // ── measuring ────────────────────────────────────────────────────────────

    private fun record(t: Turn) {
        if (t.recorded) return
        t.recorded = true
        val timing = VoiceTiming(t.speechEnd, t.recognised, t.firstSay, t.queue.firstSoundAt,
            t.queue.firstVoice ?: t.tts, route.describe())
        host.emitTiming(timing.toEvent())
        if (timing.firstAudioMs != null) {
            stats.add(timing)
            host.savedStats = stats.save()
            _statsTick.update { it + 1 }
        }
    }

    fun clearStats() {
        stats.clear()
        host.savedStats = stats.save()
        _statsTick.update { it + 1 }
    }

    companion object {
        private const val TAG = "NoraVoice"
        const val TTS_CORE = "core"
        const val TTS_PHONE = "phone"
        /** Recogniser RMS (roughly -2..10 dB) above which the user is talking. */
        private const val LOUD_RMS_DB = 4f
        /** How long a pause ends what was said. Recognisers may ignore it. */
        private const val SILENCE_MS = 700
        private const val SPEAKER_MARGIN_DB = 20.0
        private const val HEADSET_MARGIN_DB = 12.0
    }
}
