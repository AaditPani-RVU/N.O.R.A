package com.aaditpani.nora.phone

import android.app.Application
import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.os.Build
import android.service.quicksettings.TileService
import com.aaditpani.nora.BuildConfig
import com.aaditpani.nora.link.Capability
import com.aaditpani.nora.link.DeviceLink
import com.aaditpani.nora.link.LinkConfig
import com.aaditpani.nora.link.LinkState
import com.aaditpani.nora.link.PairResult
import com.aaditpani.nora.link.PairingInvite
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import org.json.JSONObject

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
            outbox = db, audit = db,
            onNotify = { title, body, _ -> Notifications.message(app, title, body) },
            scope = scope)
        link = l
        mirror = scope.launch { l.state.collect { _state.value = it } }
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
        LinkService.stop(app)
        stopLink()
        signer.deleteKey()
        prefs.clear()
        db.clear()
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
