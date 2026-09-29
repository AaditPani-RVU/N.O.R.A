package com.aaditpani.nora.link

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class InvocationGateTest {
    private var now = 1_000_000L
    private val gate = InvocationGate { now }

    private class Cap(override val name: String, override val tier: Int,
                      override val requiresLiveUser: Boolean = false) : Capability {
        override val description = "test"
        override val paramsSchema: JSONObject = JSONObject("""
            {"type":"object","required":["text"],
             "properties":{"text":{"type":"string","maxLength":5},
                           "n":{"type":"integer","minimum":0},
                           "mode":{"type":"string","enum":["a","b"]}}}""")
        override suspend fun execute(params: JSONObject) = CapabilityResult.Ok(JSONObject())
    }

    private val caps = listOf(Cap("t.act", 1), Cap("t.confirm", 2), Cap("t.live", 0, requiresLiveUser = true))
        .associateBy { it.name }

    private fun invoke(cap: String, params: String = """{"text":"hi"}""", origin: String = "live_user",
                       ts: Long = now, confirmation: JSONObject? = null, id: String = Protocol.newId()) =
        JSONObject().put("id", id).put("ts", ts).put("body", JSONObject()
            .put("capability", cap).put("params", JSONObject(params))
            .put("deadline_ms", 8000).put("origin", origin)
            .put("confirmation", confirmation ?: JSONObject.NULL))

    private fun refusal(d: InvocationGate.Decision) = (d as? InvocationGate.Decision.Refuse)?.code

    @Test
    fun runsAnOrdinaryInvocation() {
        assertTrue(gate.check(invoke("t.act"), caps, killed = false) is InvocationGate.Decision.Run)
    }

    @Test
    fun killSwitchRefusesEverything() {
        assertEquals(ErrorCode.POLICY_BLOCKED, refusal(gate.check(invoke("t.act"), caps, killed = true)))
    }

    @Test
    fun unknownOrSwitchedOffCapabilityIsUnavailable() {
        assertEquals(ErrorCode.CAPABILITY_UNAVAILABLE, refusal(gate.check(invoke("t.nope"), caps, false)))
    }

    @Test
    fun lateInvocationExpires() {
        assertEquals(ErrorCode.EXPIRED, refusal(gate.check(invoke("t.act", ts = now - 9000), caps, false)))
    }

    @Test
    fun liveOnlyCapabilityRefusesJobs() {
        assertEquals(ErrorCode.POLICY_BLOCKED, refusal(gate.check(invoke("t.live", origin = "job"), caps, false)))
        assertTrue(gate.check(invoke("t.live"), caps, false) is InvocationGate.Decision.Run)
    }

    @Test
    fun tierTwoNeedsThisDevicesApprovalOfThisExactStep() {
        val conf = JSONObject().put("id", "c1")
        // The core claiming a confirmation proves nothing on its own.
        assertEquals(ErrorCode.POLICY_BLOCKED, refusal(gate.check(invoke("t.confirm", confirmation = conf), caps, false)))

        gate.approve("c1", listOf("t.confirm" to JSONObject("""{"text":"hi"}""")))
        assertTrue(gate.check(invoke("t.confirm", confirmation = conf), caps, false) is InvocationGate.Decision.Run)
        // Same id, different params: not what was approved.
        assertEquals(ErrorCode.POLICY_BLOCKED,
            refusal(gate.check(invoke("t.confirm", """{"text":"bye"}""", confirmation = conf), caps, false)))
    }

    @Test
    fun paramsAreCheckedAgainstTheSchema() {
        for (bad in listOf("{}", """{"text":"too long"}""", """{"text":"hi","extra":1}""",
                           """{"text":3}""", """{"text":"hi","n":-1}""", """{"text":"hi","mode":"c"}""")) {
            assertEquals(bad, ErrorCode.INVALID_PARAMS, refusal(gate.check(invoke("t.act", bad), caps, false)))
        }
        assertTrue(gate.check(invoke("t.act", """{"text":"hi","n":2,"mode":"a"}"""), caps, false)
            is InvocationGate.Decision.Run)
    }

    @Test
    fun replayReturnsTheStoredReplyForTenMinutes() {
        val msg = invoke("t.act", id = "inv1")
        val reply = JSONObject().put("success", true)
        gate.remember("inv1", reply)
        val again = gate.check(msg, caps, killed = true)
        assertTrue(again is InvocationGate.Decision.Replay)
        assertEquals(reply, (again as InvocationGate.Decision.Replay).reply)

        now += InvocationGate.REPLAY_WINDOW_MS + 1
        assertNull((gate.check(invoke("t.act", id = "inv1"), caps, false) as? InvocationGate.Decision.Replay))
    }
}
