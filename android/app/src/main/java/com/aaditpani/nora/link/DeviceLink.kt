package com.aaditpani.nora.link

import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Job
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.currentCoroutineContext
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.withTimeout
import kotlinx.coroutines.withTimeoutOrNull
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import okio.ByteString
import org.json.JSONArray
import org.json.JSONObject
import java.util.Base64
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicLong
import kotlin.random.Random

/** Signs the handshake challenge. On the phone: a non-exportable Keystore key. */
interface Signer {
    /** SubjectPublicKeyInfo DER — what the core's `load_public_key` reads. */
    fun publicKeyDer(): ByteArray
    /** ECDSA-SHA256, DER-encoded signature. */
    fun sign(payload: ByteArray): ByteArray
}

/** Events the core hasn't acked yet. They survive a reconnect and a restart. */
interface Outbox {
    fun add(id: String, frame: String)
    fun remove(id: String)
    /** Oldest first. */
    fun pending(): List<Pair<String, String>>
}

data class AuditEntry(
    val ts: Long,
    val invocationId: String,
    val capability: String,
    val params: String,
    val origin: String,
    val tier: Int,
    /** "ok", or the error code. */
    val outcome: String,
    val message: String,
    val durationMs: Long,
)

/** This device's own log of what the core asked of it (plan §7.7). */
fun interface AuditSink {
    fun record(entry: AuditEntry)
}

sealed class LinkState {
    data object Stopped : LinkState()
    data object Connecting : LinkState()
    data class Connected(val sessionId: String) : LinkState()
    /** The core refused this device: not yet approved, or revoked. */
    data class NotAuthorised(val message: String, val retryAtMs: Long) : LinkState()
    data class Waiting(val reason: String, val retryAtMs: Long) : LinkState()
}

data class LinkConfig(
    val url: String,
    val deviceId: String,
    val appVersion: String,
    val os: String,
    val platform: String = "android",
)

sealed class PairResult {
    data class Paired(val deviceId: String, val status: String) : PairResult()
    data class Refused(val message: String) : PairResult()
    data class Failed(val message: String) : PairResult()
}

/**
 * One device's connection to the core: handshake, reconnect, invocations,
 * events. Runs the same way on the phone and in the JVM tests.
 *
 * The socket is kept open while the process is (the app holds a foreground
 * service for that). When it drops, it retries with exponential backoff and
 * full jitter, 1 s → 60 s, and [kick] skips the wait — the app kicks when the
 * network comes back, so leaving airplane mode reconnects at once.
 */
class DeviceLink(
    private val config: LinkConfig,
    private val signer: Signer,
    private val capabilities: () -> List<Capability>,
    private val isKilled: () -> Boolean,
    private val outbox: Outbox,
    private val audit: AuditSink,
    /** Typed chat, confirmations and messages. Without one, confirmations are declined. */
    private val chat: ChatListener?,
    private val scope: CoroutineScope,
    private val client: OkHttpClient = defaultClient(),
) {
    private val _state = MutableStateFlow<LinkState>(LinkState.Stopped)
    val state: StateFlow<LinkState> = _state

    private val gate = InvocationGate()
    private val kicks = Channel<Unit>(Channel.CONFLATED)
    private var job: Job? = null

    @Volatile private var current: Socket? = null

    fun start() {
        if (job?.isActive == true) return
        job = scope.launch { runLoop() }
    }

    fun stop() {
        job?.cancel()
        job = null
        current?.ws?.close(1000, "stopped")
        current = null
        _state.value = LinkState.Stopped
    }

    /** Reconnect now instead of waiting out the backoff. */
    fun kick() {
        kicks.trySend(Unit)
    }

    /** Tell the core the kill switch changed. The local refusal doesn't depend on this. */
    fun sendKill(active: Boolean) {
        current?.send("kill", JSONObject().put("active", active))
    }

    /** Re-advertise capabilities after the user switches one on or off. */
    fun refreshManifest() {
        current?.send("manifest", manifest())
    }

    /**
     * Send a typed message as a turn. Returns its id, which NORA's answer lines
     * and `turn.done` carry as `corr`; null when not connected. Not queued: a
     * command run minutes after it was typed is a surprise, not a service.
     */
    fun sendUtterance(text: String, voiceTts: String? = null): String? {
        val sock = current ?: return null
        val body = JSONObject().put("text", text.take(ChatLog.MAX_TEXT))
        // Spoken, not typed: "core" asks for the answer in NORA's own voice as well.
        if (voiceTts != null) body.put("voice", JSONObject().put("tts", voiceTts))
        val env = Protocol.envelope("utterance", body)
        return if (sock.resend(env)) env.getString("id") else null
    }

    /** The user talked over NORA's answer to [utteranceId]: the core stops making audio for it. */
    fun sendBargeIn(utteranceId: String) {
        current?.send("voice.barge_in", corr = utteranceId)
    }

    /** Queue an event; it is sent now if connected, else on the next connection. */
    fun emitEvent(name: String, data: JSONObject) {
        val env = Protocol.envelope("event", JSONObject()
            .put("name", name)
            .put("occurred_at", System.currentTimeMillis())
            .put("data", data))
        val id = env.getString("id")
        outbox.add(id, env.toString())
        current?.resend(env)
    }

    // ── connection loop ──────────────────────────────────────────────────────

    private sealed class Outcome {
        data class Refused(val message: String) : Outcome()
        data class Dropped(val reason: String, val wasConnected: Boolean) : Outcome()
    }

    private suspend fun runLoop() {
        var attempt = 0
        while (currentCoroutineContext().isActive) {
            _state.value = LinkState.Connecting
            val outcome = try {
                session()
            } catch (e: CancellationException) {
                throw e
            } catch (e: Exception) {
                Outcome.Dropped(e.message ?: e.javaClass.simpleName, wasConnected = false)
            }
            attempt = if (outcome is Outcome.Dropped && outcome.wasConnected) 1 else attempt + 1
            val wait = backoffMs(attempt)
            val retryAt = System.currentTimeMillis() + wait
            _state.value = when (outcome) {
                is Outcome.Refused -> LinkState.NotAuthorised(outcome.message, retryAt)
                is Outcome.Dropped -> LinkState.Waiting(outcome.reason, retryAt)
            }
            withTimeoutOrNull(wait) { kicks.receive() }
        }
    }

    private suspend fun session(): Outcome {
        val sock = Socket.open(client, config.url)
        try {
            sock.send("hello", JSONObject()
                .put("device_id", config.deviceId)
                .put("app_version", config.appVersion)
                .put("protocols", JSONArray().put(Protocol.VERSION))
                .put("platform", config.platform)
                .put("os", config.os))
            val challenge = sock.next(HANDSHAKE_MS)
            refusal(challenge)?.let { return it }
            if (challenge.getString("type") != "challenge") throw ProtocolException("expected challenge")
            val nonce = Protocol.hexToBytes(challenge.getJSONObject("body").getString("nonce"))
            val signature = signer.sign(Protocol.authPayload(nonce, config.deviceId))
            sock.send("auth", JSONObject().put("signature", Base64.getEncoder().encodeToString(signature)),
                corr = challenge.getString("id"))
            val welcome = sock.next(HANDSHAKE_MS)
            refusal(welcome)?.let { return it }
            if (welcome.getString("type") != "welcome") throw ProtocolException("expected welcome")
            val sessionId = welcome.getJSONObject("body").optString("session_id")

            sock.send("manifest", manifest())
            // The core's kill flag lives in the session, so it starts clear on
            // every connection; restate it before anything can be invoked.
            if (isKilled()) sock.send("kill", JSONObject().put("active", true))
            current = sock
            _state.value = LinkState.Connected(sessionId)
            for ((_, frame) in outbox.pending()) sock.resend(JSONObject(frame))

            while (true) {
                when (val m = sock.nextAny()) {
                    is ByteArray -> audio(m)
                    is JSONObject -> dispatch(sock, m)
                }
            }
        } catch (e: LinkClosed) {
            return Outcome.Dropped(e.message ?: "closed", wasConnected = current === sock)
        } finally {
            if (current === sock) current = null
            sock.ws.cancel()
        }
    }

    private fun refusal(msg: JSONObject): Outcome.Refused? =
        if (msg.getString("type") == "error") {
            Outcome.Refused(msg.getJSONObject("body").optString("message", "refused by the core"))
        } else null

    private fun manifest(): JSONObject = JSONObject().put("capabilities",
        JSONArray().also { arr -> capabilities().forEach { arr.put(it.manifestEntry()) } })

    private fun dispatch(sock: Socket, msg: JSONObject) {
        val body = msg.getJSONObject("body")
        when (msg.getString("type")) {
            "invoke" -> scope.launch { invoke(sock, msg) }
            "confirm_request" -> scope.launch { confirm(sock, msg) }
            "notify" -> chat?.onNotify(body.optString("title", "NORA"), body.optString("body"),
                body.optString("kind", "answer"))
            "say" -> chat?.onSay(Protocol.corr(msg), body.optString("text"), AudioSpec.parse(body.optJSONObject("audio")))
            "audio.end" -> chat?.onAudioEnd(body.optInt("stream", -1), body.optBoolean("ok"), body.optBoolean("sent"))
            "turn.done" -> chat?.onTurnDone(Protocol.corr(msg), body.optString("outcome"))
            "ping" -> sock.send("pong", corr = msg.getString("id"))
            "ack" -> Protocol.corr(msg)?.let(outbox::remove)
            else -> Unit
        }
    }

    /** A binary frame: a piece of NORA's voice. Anything else is dropped. */
    private fun audio(frame: ByteArray) {
        val (stream, pcm) = PcmFrame.parse(frame) ?: return
        chat?.onAudio(stream, pcm)
    }

    /**
     * Ask the user, on this phone, whether the core may go ahead. Declined on
     * expiry, on a malformed request, and when nothing here can ask. An
     * approval is recorded in the gate against exactly these steps, so the
     * invocations that follow are held to what was shown.
     */
    private suspend fun confirm(sock: Socket, msg: JSONObject) {
        val body = msg.getJSONObject("body")
        val ref = body.optString("invocation_id")
        val tier = body.optInt("tier", Protocol.TIER_CONFIRM)
        val started = System.currentTimeMillis()
        val steps = ConfirmSteps.parse(body.optJSONArray("steps"))
        val expiresMs = (body.optDouble("expires_in", 60.0) * 1000).toLong().coerceIn(5_000, 300_000)
        val listener = chat
        val (approved, method) = when {
            listener == null -> false to "unsupported"
            steps == null || ref.isEmpty() -> false to "malformed"
            else -> {
                val prompt = ConfirmPrompt(msg.getString("id"), ref, steps,
                    body.optString("rendered").take(300), tier, started + expiresMs)
                val answer = try {
                    withTimeoutOrNull(expiresMs) { listener.onConfirm(prompt) }
                } catch (e: CancellationException) {
                    if (!currentCoroutineContext().isActive) throw e
                    null
                }
                when (answer) {
                    true -> true to "on_device"
                    false -> false to "declined"
                    null -> false to "expired"
                }
            }
        }
        if (approved) gate.approve(ref, steps!!)
        sock.send("confirm_response", JSONObject()
            .put("invocation_id", ref).put("approved", approved).put("method", method),
            corr = msg.getString("id"))
        audit.record(AuditEntry(started, ref, "confirm",
            steps?.joinToString("; ") { "${it.first} ${it.second}" }?.take(500) ?: "", "", tier,
            if (approved) "ok" else ErrorCode.USER_DECLINED,
            if (approved) "Approved on this phone: ${body.optString("rendered").take(200)}"
            else "Not approved ($method): ${body.optString("rendered").take(200)}",
            System.currentTimeMillis() - started))
    }

    private suspend fun invoke(sock: Socket, msg: JSONObject) {
        val id = msg.getString("id")
        val body = msg.getJSONObject("body")
        val name = body.optString("capability")
        val started = System.currentTimeMillis()

        val caps = capabilities().associateBy { it.name }
        val reply = when (val d = gate.check(msg, caps, isKilled())) {
            is InvocationGate.Decision.Replay -> {
                sock.send("result", d.reply, corr = id)
                return
            }
            is InvocationGate.Decision.Refuse -> errorBody(name, d.code, d.message, false, null)
            is InvocationGate.Decision.Run -> run(d, body.optLong("deadline_ms", 8000))
        }
        reply.put("duration_ms", System.currentTimeMillis() - started)
        gate.remember(id, reply)
        sock.send("result", reply, corr = id)

        val ok = reply.optBoolean("success")
        val result = reply.optJSONObject("result")
        // Notification text is read back, not kept: not in the core's logs, and
        // not in this one either.
        val kept = if (ok && caps[name]?.untrustedOutput == true)
            "${result?.optJSONArray("items")?.length() ?: 0} item(s) read; text not kept"
        else if (ok) result?.optString("message").orEmpty()
        else reply.getJSONObject("error").optString("message")
        audit.record(AuditEntry(
            ts = started, invocationId = id, capability = name,
            params = (body.optJSONObject("params") ?: JSONObject()).toString(),
            origin = body.optString("origin"), tier = body.optInt("tier", -1),
            outcome = if (ok) "ok" else reply.getJSONObject("error").getString("code"),
            message = kept,
            durationMs = System.currentTimeMillis() - started))
    }

    private suspend fun run(d: InvocationGate.Decision.Run, deadlineMs: Long): JSONObject {
        val name = d.capability.name
        return try {
            when (val r = withTimeout(deadlineMs.coerceAtLeast(1000)) { d.capability.execute(d.params) }) {
                is CapabilityResult.Ok -> JSONObject()
                    .put("success", true).put("device", config.deviceId)
                    .put("action", name).put("result", r.result)
                is CapabilityResult.Failed -> errorBody(name, r.code, r.message, r.retryable, r.userAction)
            }
        } catch (e: CancellationException) {
            if (!currentCoroutineContext().isActive) throw e
            errorBody(name, ErrorCode.EXECUTION_FAILED, "$name ran out of time on the phone", true, null)
        } catch (e: Exception) {
            errorBody(name, ErrorCode.EXECUTION_FAILED, "$name failed: ${e.message}", false, null)
        }
    }

    private fun errorBody(name: String, code: String, message: String, retryable: Boolean,
                          userAction: String?): JSONObject {
        val err = JSONObject().put("code", code).put("message", message).put("retryable", retryable)
        if (userAction != null) err.put("user_action", userAction)
        return JSONObject().put("success", false).put("device", config.deviceId)
            .put("action", name).put("error", err)
    }

    // ── socket ───────────────────────────────────────────────────────────────

    private class LinkClosed(reason: String) : Exception(reason)

    private sealed class Incoming {
        data class Text(val text: String) : Incoming()
        class Binary(val bytes: ByteArray) : Incoming()
        data class Closed(val reason: String) : Incoming()
    }

    private class Socket(val ws: WebSocket, private val incoming: Channel<Incoming>) {
        private val seq = AtomicLong()

        fun send(type: String, body: JSONObject = JSONObject(), corr: String? = null) {
            ws.send(Protocol.envelope(type, body, corr, seq.incrementAndGet()).toString())
        }

        /** An outbox frame keeps its id — the core acks by it — and gets a fresh seq. */
        fun resend(frame: JSONObject): Boolean =
            ws.send(frame.put("seq", seq.incrementAndGet()).toString())

        suspend fun next(timeoutMs: Long?): JSONObject {
            val item = if (timeoutMs == null) incoming.receive()
            else withTimeoutOrNull(timeoutMs) { incoming.receive() } ?: throw LinkClosed("core did not answer")
            return when (item) {
                is Incoming.Binary -> {
                    ws.close(1008, "protocol error")
                    throw LinkClosed("binary frame from core during the handshake")
                }
                is Incoming.Text -> try {
                    Protocol.decode(item.text)
                } catch (e: ProtocolException) {
                    ws.close(1008, "protocol error")
                    throw LinkClosed("bad frame from core: ${e.message}")
                }
                is Incoming.Closed -> throw LinkClosed(item.reason)
            }
        }

        /** The next frame once connected: a JSONObject, or a ByteArray for binary. */
        suspend fun nextAny(): Any = when (val item = incoming.receive()) {
            is Incoming.Binary -> item.bytes
            is Incoming.Text -> try {
                Protocol.decode(item.text)
            } catch (e: ProtocolException) {
                ws.close(1008, "protocol error")
                throw LinkClosed("bad frame from core: ${e.message}")
            }
            is Incoming.Closed -> throw LinkClosed(item.reason)
        }

        companion object {
            fun open(client: OkHttpClient, url: String): Socket {
                val incoming = Channel<Incoming>(Channel.UNLIMITED)
                val ws = client.newWebSocket(Request.Builder().url(url).build(), object : WebSocketListener() {
                    override fun onMessage(webSocket: WebSocket, text: String) {
                        incoming.trySend(Incoming.Text(text))
                    }

                    override fun onMessage(webSocket: WebSocket, bytes: ByteString) {
                        // NORA's voice (Phase 6), in order with the text frames around it.
                        incoming.trySend(Incoming.Binary(bytes.toByteArray()))
                    }

                    override fun onClosing(webSocket: WebSocket, code: Int, reason: String) {
                        webSocket.close(1000, null)
                        incoming.trySend(Incoming.Closed(reason.ifEmpty { "closed ($code)" }))
                    }

                    override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
                        incoming.trySend(Incoming.Closed(t.message ?: t.javaClass.simpleName))
                    }
                })
                return Socket(ws, incoming)
            }
        }
    }

    companion object {
        const val HANDSHAKE_MS = 10_000L
        private const val BACKOFF_MIN_MS = 1_000L
        private const val BACKOFF_MAX_MS = 60_000L

        /** Exponential, full jitter, but never a tight loop. */
        fun backoffMs(attempt: Int, random: Random = Random.Default): Long {
            val cap = (BACKOFF_MIN_MS shl (attempt - 1).coerceIn(0, 6)).coerceAtMost(BACKOFF_MAX_MS)
            return random.nextLong(BACKOFF_MIN_MS / 2, cap + 1)
        }

        fun defaultClient(): OkHttpClient = OkHttpClient.Builder()
            .connectTimeout(15, TimeUnit.SECONDS)
            .pingInterval(25, TimeUnit.SECONDS)
            .build()

        /** Spend a pairing code. The device is pending until approved on the core. */
        suspend fun pair(url: String, code: String, deviceName: String, publicKeyDer: ByteArray,
                         platform: String = "android", client: OkHttpClient = defaultClient()): PairResult {
            val sock = Socket.open(client, url)
            return try {
                sock.send("pair", JSONObject()
                    .put("pairing_code", code)
                    .put("device_name", deviceName)
                    .put("platform", platform)
                    .put("public_key", Base64.getEncoder().encodeToString(publicKeyDer)))
                val reply = sock.next(HANDSHAKE_MS)
                val body = reply.getJSONObject("body")
                when (reply.getString("type")) {
                    "paired" -> PairResult.Paired(body.getString("device_id"), body.optString("status"))
                    "error" -> PairResult.Refused(body.optString("message", "refused"))
                    else -> PairResult.Failed("unexpected reply ${reply.getString("type")}")
                }
            } catch (e: LinkClosed) {
                PairResult.Failed(e.message ?: "connection closed")
            } catch (e: Exception) {
                PairResult.Failed(e.message ?: e.javaClass.simpleName)
            } finally {
                sock.ws.close(1000, null)
            }
        }
    }
}
