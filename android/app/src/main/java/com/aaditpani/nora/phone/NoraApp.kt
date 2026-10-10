package com.aaditpani.nora.phone

import android.app.Application
import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.os.Build
import android.service.quicksettings.TileService
import com.aaditpani.nora.BuildConfig
import com.aaditpani.nora.link.AudioSpec
import com.aaditpani.nora.link.Capability
import com.aaditpani.nora.link.ChatListener
import com.aaditpani.nora.link.ChatLog
import com.aaditpani.nora.link.ConfirmPrompt
import com.aaditpani.nora.link.Delivery
import com.aaditpani.nora.link.DeviceLink
import com.aaditpani.nora.link.LinkConfig
import com.aaditpani.nora.link.LinkState
import com.aaditpani.nora.link.PairResult
import com.aaditpani.nora.link.PairingInvite
import com.aaditpani.nora.link.Protocol
import com.aaditpani.nora.voice.VoiceController
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import org.json.JSONObject
import java.util.concurrent.ConcurrentHashMap

class NoraApp : Application() {
    lateinit var controller: LinkController
        private set

    override fun onCreate() {
        super.onCreate()
        Notifications.createChannels(this)
        registerActivityLifecycleCallbacks(Foreground)
        controller = LinkController(this)
    }
}

val Context.controller: LinkController get() = (applicationContext as NoraApp).controller

/**
 * The one owner of the link. The service keeps the process alive, the UI and
 * the quick-settings tile read and flip state here, and nothing else touches
 * the socket.
 */
class LinkController(private val app: Context) {
    val prefs = LinkPrefs(app)
    val db = PhoneDb(app)
    val signer = KeystoreSigner()
    val capabilities: List<Capability> = allCapabilities(app, prefs)

    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private var link: DeviceLink? = null
    private var mirror: Job? = null

    private val _state = MutableStateFlow<LinkState>(LinkState.Stopped)
    val state: StateFlow<LinkState> = _state
    private val _killed = MutableStateFlow(prefs.killed)
    val killed: StateFlow<Boolean> = _killed
    private val _disabled = MutableStateFlow(prefs.disabled)
    val disabled: StateFlow<Set<String>> = _disabled
    private val _hiddenNoteApps = MutableStateFlow(prefs.hiddenNoteApps)
    val hiddenNoteApps: StateFlow<Set<String>> = _hiddenNoteApps
    private val _paired = MutableStateFlow(prefs.deviceId != null && signer.hasKey())
    val paired: StateFlow<Boolean> = _paired
    /** Bumped on every audit row, so the audit screen re-reads. */
    val auditTick = MutableStateFlow(0)

    val chat = ChatLog(db)
    /** True while the chat is on screen: answers and messages then don't also notify. */
    val chatVisible = MutableStateFlow(false)
    private val _confirms = MutableStateFlow<List<ConfirmPrompt>>(emptyList())
    /** Questions from the core waiting for a yes or no, oldest first. */
    val confirms: StateFlow<List<ConfirmPrompt>> = _confirms
    private val answers = ConcurrentHashMap<String, CompletableDeferred<Boolean>>()

    val voice = VoiceController(app, object : VoiceController.Host {
        override fun sendVoice(text: String, tts: String): String? {
            val t = text.trim().take(ChatLog.MAX_TEXT)
            val id = link?.sendUtterance(t, tts) ?: return null
            chat.mine(id, t, Delivery.SENT)
            return id
        }
        override fun bargeIn(utteranceId: String) { link?.sendBargeIn(utteranceId) }
        override fun emitTiming(data: JSONObject) { link?.emitEvent("voice.turn", data) }
        override val connected: Boolean get() = _state.value is LinkState.Connected
        override var tts: String by prefs::voiceTts
        override var bargeInOn: Boolean by prefs::bargeIn
        override var followUp: Boolean by prefs::followUp
        override var ackOn: Boolean by prefs::ack
        override var savedStats: String? by prefs::voiceStats
    })

    private val chatListener = object : ChatListener {
        override fun onSay(replyTo: String?, text: String) {
            if (text.isNotBlank()) chat.nora(replyTo, text)
        }

        override fun onSay(replyTo: String?, text: String, audio: AudioSpec?) {
            onSay(replyTo, text)
            if (text.isNotBlank()) voice.onSay(replyTo, text, audio)
        }

        override fun onAudio(stream: Int, pcm: ByteArray) = voice.onAudio(stream, pcm)

        override fun onAudioEnd(stream: Int, ok: Boolean, sent: Boolean) = voice.onAudioEnd(stream, ok, sent)

        override fun onTurnDone(replyTo: String?, outcome: String) {
            chat.done(replyTo)
            // A spoken answer was heard; it doesn't need a notification as well.
            if (voice.onTurnDone(replyTo)) return
            if (replyTo == null || chatVisible.value) return
            val said = chat.answersTo(replyTo)
            if (said.isNotEmpty()) Notifications.reply(app, said.joinToString(" "))
        }

        override fun onNotify(title: String, body: String, kind: String) {
            chat.notice(body, kind)
            if (!chatVisible.value) Notifications.message(app, title, body)
        }

        override suspend fun onConfirm(prompt: ConfirmPrompt): Boolean {
            val answer = CompletableDeferred<Boolean>()
            answers[prompt.requestId] = answer
            _confirms.update { it + prompt }
            if (!chatVisible.value) Notifications.confirm(app, prompt)
            return try {
                answer.await()
            } finally {
                answers.remove(prompt.requestId)
                _confirms.update { list -> list.filterNot { it.requestId == prompt.requestId } }
                Notifications.cancelConfirm(app, prompt)
            }
        }
    }

    init {
        db.onAudit = { auditTick.value++ }
    }

    val deviceId: String? get() = prefs.deviceId
    val url: String? get() = prefs.url

    @Synchronized
    fun startLink() {
        if (link != null) return
        val url = prefs.url ?: return
        val id = prefs.deviceId ?: return
        if (!signer.hasKey()) return
        val l = DeviceLink(
            LinkConfig(url, id, BuildConfig.VERSION_NAME, Build.VERSION.RELEASE),
            signer,
            capabilities = { capabilities.filter { it.name !in _disabled.value } },
            isKilled = { _killed.value },
            outbox = db, audit = db, chat = chatListener,
            scope = scope)
        link = l
        mirror = scope.launch {
            l.state.collect {
                _state.value = it
                if (it !is LinkState.Connected) {
                    chat.dropInFlight()
                    voice.linkDropped()
                }
            }
        }
        l.start()
    }

    @Synchronized
    fun stopLink() {
        link?.stop()
        link = null
        mirror?.cancel()
        _state.value = LinkState.Stopped
    }

    fun kick() = link?.kick()

    /** Type a message to NORA. False when it couldn't be sent (not connected). */
    fun send(text: String): Boolean {
        val t = text.trim().take(ChatLog.MAX_TEXT)
        if (t.isEmpty()) return false
        val id = link?.sendUtterance(t)
        if (id == null) {
            chat.mine(Protocol.newId(), t, Delivery.FAILED)
            return false
        }
        chat.mine(id, t, Delivery.SENT)
        return true
    }

    /** Send a failed message again, in place of the failed copy. */
    fun resend(id: String) {
        val m = chat.get(id) ?: return
        chat.remove(id)
        send(m.text)
    }

    /** The user's answer to a confirmation on screen. */
    fun answer(requestId: String, approved: Boolean) {
        answers[requestId]?.complete(approved)
    }

    fun setKilled(active: Boolean) {
        prefs.killed = active
        _killed.value = active
        link?.sendKill(active)
        TileService.requestListeningState(app, ComponentName(app, KillTileService::class.java))
        LinkService.refresh(app)
    }

    fun setEnabled(name: String, on: Boolean) {
        val next = if (on) _disabled.value - name else _disabled.value + name
        prefs.disabled = next
        _disabled.value = next
        link?.refreshManifest()
    }

    fun setNoteAppHidden(pkg: String, hidden: Boolean) {
        val next = if (hidden) _hiddenNoteApps.value + pkg else _hiddenNoteApps.value - pkg
        prefs.hiddenNoteApps = next
        _hiddenNoteApps.value = next
    }

    fun emitStatus() {
        link?.emitEvent("device.status", DeviceStatus.read(app))
    }

    suspend fun pair(invite: PairingInvite, deviceName: String): PairResult = withContext(Dispatchers.IO) {
        stopLink()
        signer.deleteKey()   // a new pairing is a new identity
        signer.ensureKey()
        val result = DeviceLink.pair(invite.url, invite.code, deviceName, signer.publicKeyDer())
        if (result is PairResult.Paired) {
            prefs.url = invite.url
            prefs.deviceId = result.deviceId
            _paired.value = true
            LinkService.start(app)
        }
        result
    }

    fun unpair() {
        voice.stop()
        voice.clearStats()
        LinkService.stop(app)
        stopLink()
        signer.deleteKey()
        prefs.clear()
        db.clear()
        chat.clear()
        answers.values.forEach { it.complete(false) }
        _killed.value = false
        _disabled.value = emptySet()
        _hiddenNoteApps.value = emptySet()
        NoraNotificationListener.clear()
        _paired.value = false
    }

    fun auditLog() = db.recentAudit()

    fun keyHardware(): String = runCatching { signer.hardware() }.getOrDefault("unknown")

    fun statusSnapshot(): JSONObject = DeviceStatus.read(app)
}
