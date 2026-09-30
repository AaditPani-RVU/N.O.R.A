package com.aaditpani.nora.link

import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.runBlocking
import org.json.JSONObject
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Assume.assumeTrue
import org.junit.Before
import org.junit.Test
import java.io.File
import java.security.KeyPair
import java.security.KeyPairGenerator
import java.security.Signature
import java.security.spec.ECGenParameterSpec
import java.util.Collections

/**
 * The Kotlin client against the real Python hub (`android/tools/hub_harness.py`),
 * on a throwaway store. Skipped when the repo's `.venv` isn't there.
 *
 * Covers Phase 3's protocol half of the exit criteria: pairing, pending until
 * approved, the signed handshake, an invocation round trip with its audit row,
 * the kill switch cutting invocation on both sides, an event outbox that
 * survives being offline, a declined confirmation, and revocation.
 */
class HubIntegrationTest {
    private lateinit var harness: Harness
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)

    private class JvmSigner : Signer {
        val pair: KeyPair = KeyPairGenerator.getInstance("EC")
            .apply { initialize(ECGenParameterSpec("secp256r1")) }.generateKeyPair()
        override fun publicKeyDer(): ByteArray = pair.public.encoded
        override fun sign(payload: ByteArray): ByteArray = Signature.getInstance("SHA256withECDSA")
            .run { initSign(pair.private); update(payload); sign() }
    }

    private class Harness(repo: File) : AutoCloseable {
        private val proc = ProcessBuilder(File(repo, ".venv/bin/python").path,
            File(repo, "android/tools/hub_harness.py").path)
            .redirectError(File(System.getProperty("java.io.tmpdir"), "nora-hub-harness.log")).start()
        private val reader = proc.inputStream.bufferedReader()
        private val writer = proc.outputStream.bufferedWriter()
        val hello = JSONObject(reader.readLine())

        fun cmd(line: String): JSONObject {
            writer.write(line + "\n")
            writer.flush()
            return JSONObject(reader.readLine())
        }

        override fun close() {
            runCatching { cmd("quit") }
            proc.waitFor()
        }
    }

    private class MemOutbox : Outbox {
        val frames: MutableMap<String, String> = Collections.synchronizedMap(LinkedHashMap())
        override fun add(id: String, frame: String) { frames[id] = frame }
        override fun remove(id: String) { frames.remove(id) }
        override fun pending() = synchronized(frames) { frames.toList() }
    }

    private val ping = object : Capability {
        override val name = "test.ping"
        override val description = "Answer a ping"
        override val tier = 1
        override val paramsSchema = JSONObject("""{"type":"object","required":["text"],
            "properties":{"text":{"type":"string","maxLength":50}}}""")
        override suspend fun execute(params: JSONObject) =
            CapabilityResult.Ok(JSONObject().put("message", "pong: " + params.getString("text")))
    }

    @Before
    fun setUp() {
        val repo = File(System.getProperty("nora.repo") ?: "../..")
        assumeTrue("needs the repo's .venv", File(repo, ".venv/bin/python").exists())
        harness = Harness(repo)
    }

    @After
    fun tearDown() {
        scope.cancel()
        if (::harness.isInitialized) harness.close()
    }

    private fun until(what: String, timeoutMs: Long = 15_000, cond: () -> Boolean) {
        val end = System.currentTimeMillis() + timeoutMs
        while (!cond()) {
            if (System.currentTimeMillis() > end) throw AssertionError("timed out waiting for $what")
            Thread.sleep(50)
        }
    }

    private fun caps(): JSONObject = harness.cmd("caps").getJSONObject("caps")

    @Test
    fun pairApproveInvokeKillRevoke() = runBlocking {
        val url = harness.hello.getString("url")
        val signer = JvmSigner()
        val paired = DeviceLink.pair(url, harness.hello.getString("code"), "jvm-test", signer.publicKeyDer())
        assertTrue(paired.toString(), paired is PairResult.Paired)
        val deviceId = (paired as PairResult.Paired).deviceId
        assertEquals("pending", paired.status)

        // The code was single use.
        assertTrue(DeviceLink.pair(url, harness.hello.getString("code"), "again", JvmSigner().publicKeyDer())
            is PairResult.Refused)

        var killed = false
        val outbox = MemOutbox()
        val audit = Collections.synchronizedList(mutableListOf<AuditEntry>())
        val link = DeviceLink(LinkConfig(url, deviceId, "test", "17"), signer, { listOf(ping) },
            { killed }, outbox, audit::add, null, scope)
        link.start()

        // Pending: refused until approved on the core. Events wait meanwhile.
        until("refusal") { link.state.value is LinkState.NotAuthorised }
        link.emitEvent("device.status", JSONObject().put("battery", 80))
        assertEquals(1, outbox.pending().size)

        assertTrue(harness.cmd("approve $deviceId").getBoolean("ok"))
        link.kick()
        until("connected") { link.state.value is LinkState.Connected }
        until("capability registered") { caps().optString("test.ping") == deviceId }
        until("outbox acked") { outbox.pending().isEmpty() }

        // A live-user invocation round trip, audited on the device.
        val r = harness.cmd("""invoke $deviceId test.ping {"text":"hi"}""")
        assertTrue(r.toString(), r.getBoolean("success"))
        assertEquals("pong: hi", r.getString("message"))
        until("audit row") { audit.any { it.capability == "test.ping" && it.outcome == "ok" } }
        assertEquals("live_user", audit.first { it.capability == "test.ping" }.origin)

        // From a job the tier goes up one, to 2: the core asks the phone,
        // and this version declines rather than approving unseen.
        val job = harness.cmd("""invoke $deviceId test.ping {"text":"hi"} job""")
        assertEquals("USER_DECLINED", job.getString("error_code"))

        // Kill switch: the core drops the capability, and the device would refuse anyway.
        killed = true
        link.sendKill(true)
        until("capability withdrawn") { !caps().has("test.ping") }
        val blocked = harness.cmd("""invoke $deviceId test.ping {"text":"hi"}""")
        assertTrue(blocked.toString(), !blocked.getBoolean("success"))
        killed = false
        link.sendKill(false)
        until("capability back") { caps().has("test.ping") }

        val rows = harness.cmd("invocations").getJSONArray("rows")
        assertTrue(rows.toString(), (0 until rows.length()).any {
            rows.getJSONArray(it).let { row -> row.getString(0) == "test.ping" && row.getString(3) == "ok" }
        })

        // Revocation cuts it off live, and it stays out.
        assertTrue(harness.cmd("revoke $deviceId").getBoolean("ok"))
        until("revoked") { link.state.value is LinkState.NotAuthorised }
        link.stop()
    }
    private class Recorder : ChatListener {
        val said = Collections.synchronizedList(mutableListOf<Pair<String?, String>>())
        val done = Collections.synchronizedList(mutableListOf<Pair<String?, String>>())
        val notes = Collections.synchronizedList(mutableListOf<Triple<String, String, String>>())
        val asked = Collections.synchronizedList(mutableListOf<ConfirmPrompt>())
        @Volatile var answer: Boolean? = true   // null: never answers
        val audioFor = Collections.synchronizedMap(HashMap<String, AudioSpec?>())   // line -> its audio
        val pcm = Collections.synchronizedMap(HashMap<Int, java.io.ByteArrayOutputStream>())
        val ended = Collections.synchronizedMap(HashMap<Int, Pair<Boolean, Boolean>>())

        override fun onSay(replyTo: String?, text: String) { said += replyTo to text }
        override fun onSay(replyTo: String?, text: String, audio: AudioSpec?) {
            audioFor[text] = audio
            onSay(replyTo, text)
        }
        override fun onAudio(stream: Int, pcm: ByteArray) {
            this.pcm.getOrPut(stream) { java.io.ByteArrayOutputStream() }.write(pcm)
        }
        override fun onAudioEnd(stream: Int, ok: Boolean, sent: Boolean) { ended[stream] = ok to sent }
        override fun onTurnDone(replyTo: String?, outcome: String) { done += replyTo to outcome }
        override fun onNotify(title: String, body: String, kind: String) { notes += Triple(title, body, kind) }
        override suspend fun onConfirm(prompt: ConfirmPrompt): Boolean {
            asked += prompt
            return answer ?: kotlinx.coroutines.awaitCancellation()
        }
    }

    /**
     * Phase 5: typed chat. A message goes up as a turn and its answer lines
     * come back tagged with its id; a confirmation is asked and answered on
     * the phone, and an approval is held to the exact step shown; jobs and
     * reminders arrive as messages.
     */
    @Test
    fun chatConfirmAndDeliver() = runBlocking {
        val url = harness.hello.getString("url")
        val signer = JvmSigner()
        val deviceId = (DeviceLink.pair(url, harness.hello.getString("code"), "chat-test",
            signer.publicKeyDer()) as PairResult.Paired).deviceId
        assertTrue(harness.cmd("approve $deviceId").getBoolean("ok"))
        val chat = Recorder()
        val audit = Collections.synchronizedList(mutableListOf<AuditEntry>())
        val link = DeviceLink(LinkConfig(url, deviceId, "test", "17"), signer, { listOf(ping) },
            { false }, MemOutbox(), audit::add, chat, scope)
        assertEquals(null, link.sendUtterance("too early"))
        link.start()
        until("connected") { link.state.value is LinkState.Connected }

        val id = link.sendUtterance("hello")!!
        until("turn done") { chat.done.any { it.first == id } }
        assertEquals(listOf(id to "You said: hello", id to "That's all."), chat.said.toList())
        assertEquals("chat", chat.done.first { it.first == id }.second)
        assertEquals(listOf("hello"), harness.cmd("turns").getJSONArray("turns").let { a ->
            (0 until a.length()).map { a.getString(it) } })

        // The turn asks first; a yes lets it go ahead, a no stops it.
        val yes = link.sendUtterance("confirm one")!!
        until("approved turn done") { chat.done.any { it.first == yes } }
        assertTrue(chat.said.contains(yes to "Done."))
        val prompt = chat.asked.single()
        assertEquals("test.ping", prompt.steps.single().first)
        assertEquals("one", prompt.steps.single().second.getString("text"))
        chat.answer = false
        val no = link.sendUtterance("confirm two")!!
        until("declined turn done") { chat.done.any { it.first == no } }
        assertTrue(chat.said.contains(no to "Left it."))
        until("confirm audit rows") { audit.count { it.capability == "confirm" } == 2 }
        assertEquals(listOf("ok", ErrorCode.USER_DECLINED), audit.filter { it.capability == "confirm" }.map { it.outcome })

        // A tier-2 invocation (tier 1 from a job) is asked on the phone now,
        // and runs once approved: the gate holds it to the approved step.
        chat.answer = true
        val job = harness.cmd("""invoke $deviceId test.ping {"text":"hi"} job""")
        assertTrue(job.toString(), job.getBoolean("success"))
        assertEquals("pong: hi", job.getString("message"))

        // Unprompted: a reminder and a job's answer.
        assertTrue(harness.cmd("deliver $deviceId reminder Reminder, sir: call mom").getBoolean("ok"))
        assertTrue(harness.cmd("deliver $deviceId answer Back to the weather — sunny").getBoolean("ok"))
        until("messages") { chat.notes.size == 2 }
        assertEquals(Triple("Reminder", "Reminder, sir: call mom", "reminder"), chat.notes[0])
        assertEquals("NORA", chat.notes[1].first)
        link.stop()
    }

    /**
     * Phase 6: a spoken turn asking for NORA's own voice gets each answer
     * line announced with its stream and then the PCM for it, as binary
     * frames through the same socket; asking for the phone's voice gets text
     * only. The harness's fake synthesiser makes one sample per character.
     */
    @Test
    fun voiceTurnStreamsPcm() = runBlocking {
        val url = harness.hello.getString("url")
        val signer = JvmSigner()
        val deviceId = (DeviceLink.pair(url, harness.hello.getString("code"), "voice-test",
            signer.publicKeyDer()) as PairResult.Paired).deviceId
        assertTrue(harness.cmd("approve $deviceId").getBoolean("ok"))
        val chat = Recorder()
        val link = DeviceLink(LinkConfig(url, deviceId, "test", "17"), signer, { listOf(ping) },
            { false }, MemOutbox(), {}, chat, scope)
        link.start()
        until("connected") { link.state.value is LinkState.Connected }

        val id = link.sendUtterance("what's up", voiceTts = "core")!!
        until("turn done") { chat.done.any { it.first == id } }
        val lines = listOf("You said: what's up", "That's all.")
        assertEquals(lines.map { id to it }, chat.said.toList())
        val specs = lines.map { chat.audioFor[it]!! }
        assertEquals(24_000, specs[0].rate)
        until("audio ended") { specs.all { chat.ended[it.stream] != null } }
        for ((line, spec) in lines.zip(specs)) {
            assertEquals(true to true, chat.ended[spec.stream])
            val bytes = chat.pcm[spec.stream]!!.toByteArray()
            // Little-endian 16-bit, one sample per character of the line
            // (split into chunks, rejoined without the spaces between them).
            val heard = (bytes.indices step 2).map {
                ((bytes[it].toInt() and 0xFF) or (bytes[it + 1].toInt() shl 8)).toChar() }.joinToString("")
            assertEquals(line.replace(" ", ""), heard.replace(" ", ""))
        }

        // The phone's own voice: text only.
        val plain = link.sendUtterance("again", voiceTts = "phone")!!
        until("phone-voice turn done") { chat.done.any { it.first == plain } }
        assertEquals(null, chat.audioFor["You said: again"])
        link.sendBargeIn(plain)   // harmless after the turn; the socket stays up
        link.emitEvent("voice.turn", JSONObject().put("first_audio_ms", 900).put("tts", "phone"))
        Thread.sleep(200)
        assertTrue(link.state.value is LinkState.Connected)
        link.stop()
    }
}
