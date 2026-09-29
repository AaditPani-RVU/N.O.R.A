package com.aaditpani.nora.link

import org.json.JSONException
import org.json.JSONObject
import java.math.BigInteger
import java.security.SecureRandom

/**
 * Protocol v1 between the core and a device (NORA_DISTRIBUTED_PLAN.md §5).
 *
 * Mirrors `nora/hub/protocol.py` on the core. Nothing in this package uses
 * Android APIs, so the JVM tests run it against the real Python hub.
 */
object Protocol {
    const val VERSION = 1
    const val PATH = "/v1/device"
    const val MAX_FRAME = 256 * 1024

    /** What a signature covers besides the nonce and device id. */
    private val AUTH_CONTEXT = "nora-v1".toByteArray(Charsets.UTF_8)

    // Tiers (plan §6): 0 read · 1 act, logged · 2 on-device confirm ·
    // 3 confirm + biometric · 4 never implemented.
    const val TIER_CONFIRM = 2
    const val TIER_FORBIDDEN = 4

    const val ORIGIN_LIVE_USER = "live_user"

    private const val CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
    private val random = SecureRandom()

    /** A ULID: 48-bit millisecond time + 80 random bits, Crockford base32. */
    fun newId(nowMs: Long = System.currentTimeMillis()): String {
        val bytes = ByteArray(16)
        for (i in 0 until 6) bytes[i] = (nowMs ushr (8 * (5 - i))).toByte()
        ByteArray(10).also(random::nextBytes).copyInto(bytes, 6)
        var value = BigInteger(1, bytes)
        val out = CharArray(26)
        val mask = BigInteger.valueOf(31)
        for (i in 25 downTo 0) {
            out[i] = CROCKFORD[value.and(mask).toInt()]
            value = value.shiftRight(5)
        }
        return String(out)
    }

    fun envelope(
        type: String,
        body: JSONObject = JSONObject(),
        corr: String? = null,
        seq: Long = 0,
        id: String = newId(),
    ): JSONObject = JSONObject()
        .put("v", VERSION)
        .put("id", id)
        .put("type", type)
        .put("ts", System.currentTimeMillis())
        .put("corr", corr ?: JSONObject.NULL)
        .put("seq", seq)
        .put("body", body)

    /** Parse and structurally check one frame. Throws [ProtocolException]. */
    fun decode(frame: String): JSONObject {
        if (frame.length > MAX_FRAME) throw ProtocolException("frame too large")
        val msg = try {
            JSONObject(frame)
        } catch (e: JSONException) {
            throw ProtocolException("not JSON: ${e.message}")
        }
        if (msg.optInt("v", -1) != VERSION) throw ProtocolException("unsupported protocol version")
        val id = msg.optString("id", "")
        if (id.isEmpty() || id.length > 64) throw ProtocolException("missing id")
        if (msg.opt("type") !is String) throw ProtocolException("missing type")
        val body = msg.opt("body")
        if (body == null || body == JSONObject.NULL) msg.put("body", JSONObject())
        else if (body !is JSONObject) throw ProtocolException("body is not an object")
        return msg
    }

    fun corr(msg: JSONObject): String? =
        if (msg.isNull("corr")) null else msg.optString("corr").ifEmpty { null }

    fun authPayload(nonce: ByteArray, deviceId: String): ByteArray =
        nonce + deviceId.toByteArray(Charsets.UTF_8) + AUTH_CONTEXT

    fun hexToBytes(hex: String): ByteArray {
        require(hex.length % 2 == 0) { "odd hex length" }
        return ByteArray(hex.length / 2) { hex.substring(it * 2, it * 2 + 2).toInt(16).toByte() }
    }
}

class ProtocolException(message: String) : Exception(message)

/** Error codes (plan §5). DEVICE_OFFLINE is core-side only. */
object ErrorCode {
    const val INVALID_PARAMS = "INVALID_PARAMS"
    const val PERMISSION_DENIED = "PERMISSION_DENIED"
    const val CAPABILITY_UNAVAILABLE = "CAPABILITY_UNAVAILABLE"
    const val POLICY_BLOCKED = "POLICY_BLOCKED"
    const val USER_DECLINED = "USER_DECLINED"
    /** Android wouldn't let it happen from the background; the message says what was done instead. */
    const val BACKGROUND_RESTRICTED = "BACKGROUND_RESTRICTED"
    const val EXPIRED = "EXPIRED"
    const val EXECUTION_FAILED = "EXECUTION_FAILED"
}

/** The pairing QR from `python -m nora.hub pair`: {"nora":1,"url":…,"code":…,"exp":…}. */
data class PairingInvite(val url: String, val code: String, val expiresAtSec: Long?) {
    companion object {
        fun parse(text: String): PairingInvite? = try {
            val o = JSONObject(text)
            val url = o.optString("url")
            val code = o.optString("code")
            if (o.optInt("nora") != 1 || !isAcceptableUrl(url) || code.isBlank()) null
            else PairingInvite(url, code, if (o.has("exp")) o.optLong("exp") else null)
        } catch (e: JSONException) {
            null
        }

        /**
         * The core is reached through `tailscale serve`, which is always TLS.
         * Plain ws:// only to a loopback address, which is what the tests use.
         */
        fun isAcceptableUrl(url: String): Boolean {
            if (!url.endsWith(Protocol.PATH)) return false
            if (url.startsWith("wss://")) return true
            return url.startsWith("ws://127.0.0.1:") || url.startsWith("ws://localhost:")
        }
    }
}
