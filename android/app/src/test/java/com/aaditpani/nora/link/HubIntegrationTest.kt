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
            { killed }, outbox, audit::add, { _, _, _ -> }, scope)
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
}
