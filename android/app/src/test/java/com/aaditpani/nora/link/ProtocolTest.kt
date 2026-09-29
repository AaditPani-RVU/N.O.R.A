package com.aaditpani.nora.link

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import kotlin.random.Random

class ProtocolTest {
    @Test
    fun idsAreUlidsThatSortByTime() {
        val a = Protocol.newId(1_789_650_000_000)
        val b = Protocol.newId(1_789_650_000_001)
        assertEquals(26, a.length)
        assertTrue(a.all { it in "0123456789ABCDEFGHJKMNPQRSTVWXYZ" })
        assertTrue(a < b)
        // The same instant's time prefix, whatever the random tail.
        assertEquals(a.take(10), Protocol.newId(1_789_650_000_000).take(10))
    }

    @Test
    fun envelopeRoundTripsThroughDecode() {
        val env = Protocol.envelope("hello", JSONObject().put("x", 1), corr = "abc", seq = 3)
        val back = Protocol.decode(env.toString())
        assertEquals("hello", back.getString("type"))
        assertEquals("abc", Protocol.corr(back))
        assertEquals(1, back.getJSONObject("body").getInt("x"))
    }

    @Test
    fun decodeRefusesOtherVersionsAndJunk() {
        for (bad in listOf("""{"v":2,"id":"a","type":"x","body":{}}""", "[]", "nope",
                           """{"v":1,"type":"x"}""", """{"v":1,"id":"a","type":"x","body":[]}""")) {
            try {
                Protocol.decode(bad)
                throw AssertionError("accepted $bad")
            } catch (_: ProtocolException) {
            }
        }
        assertNull(Protocol.corr(Protocol.decode("""{"v":1,"id":"a","type":"x","corr":null}""")))
    }

    @Test
    fun authPayloadMatchesTheCore() {
        // nora/hub/protocol.py: nonce + device_id.encode() + b"nora-v1"
        val p = Protocol.authPayload(byteArrayOf(1, 2), "d_1")
        assertEquals(listOf<Byte>(1, 2) + "d_1nora-v1".toByteArray().toList(), p.toList())
    }

    @Test
    fun pairingInviteNeedsTlsUnlessLoopback() {
        val ok = PairingInvite.parse("""{"nora":1,"url":"wss://core.ts.net:8443/v1/device","code":"ABCD-EFGH","exp":5}""")
        assertNotNull(ok)
        assertEquals("ABCD-EFGH", ok!!.code)
        assertNull(PairingInvite.parse("""{"nora":1,"url":"ws://192.168.1.4:8770/v1/device","code":"A"}"""))
        assertNull(PairingInvite.parse("""{"nora":1,"url":"wss://core.ts.net/other","code":"A"}"""))
        assertNull(PairingInvite.parse("""{"url":"wss://core.ts.net:8443/v1/device","code":"A"}"""))
        assertNull(PairingInvite.parse("hello"))
        assertTrue(PairingInvite.isAcceptableUrl("ws://127.0.0.1:9/v1/device"))
    }

    @Test
    fun backoffGrowsToAMinuteAndNeverSpins() {
        val r = Random(1)
        repeat(200) {
            val first = DeviceLink.backoffMs(1, r)
            assertTrue(first in 500..1000)
            val late = DeviceLink.backoffMs(30, r)
            assertTrue(late in 500..60_000)
        }
    }
}
