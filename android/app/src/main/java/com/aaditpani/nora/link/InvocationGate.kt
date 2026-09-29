package com.aaditpani.nora.link

import org.json.JSONArray
import org.json.JSONObject

/**
 * The device's side of every `invoke`: what it refuses, whatever the core
 * says. Mirrors the checks in `nora/hub/fake_device.py`, in the same order:
 *
 *  1. a replayed invocation id returns the stored reply instead of acting
 *     twice (kept 10 minutes);
 *  2. kill switch on → refused;
 *  3. capability not compiled in or switched off → unavailable;
 *  4. arrived past its deadline → expired;
 *  5. `requires_live_user` and the origin is not a live user → refused;
 *  6. tier ≥ 2 without an approval given on this device for this exact step → refused;
 *  7. params that don't fit the schema → invalid.
 */
class InvocationGate(private val clock: () -> Long = System::currentTimeMillis) {
    private val done = LinkedHashMap<String, Pair<Long, JSONObject>>()

    /** Confirmation id → the steps approved under it, as `{action, params}` strings. */
    private val approved = HashMap<String, Set<String>>()

    sealed class Decision {
        data class Replay(val reply: JSONObject) : Decision()
        data class Refuse(val code: String, val message: String) : Decision()
        data class Run(val capability: Capability, val params: JSONObject) : Decision()
    }

    @Synchronized
    fun check(msg: JSONObject, capabilities: Map<String, Capability>, killed: Boolean): Decision {
        val now = clock()
        done.entries.removeAll { now - it.value.first > REPLAY_WINDOW_MS }
        done[msg.getString("id")]?.let { return Decision.Replay(it.second) }

        val body = msg.getJSONObject("body")
        val name = body.optString("capability")
        val params = body.optJSONObject("params") ?: JSONObject()
        val cap = capabilities[name]
        return when {
            killed -> Decision.Refuse(ErrorCode.POLICY_BLOCKED, "Remote control is off on this phone")
            cap == null -> Decision.Refuse(ErrorCode.CAPABILITY_UNAVAILABLE, "$name is not available on this phone")
            now - msg.optLong("ts", 0) > body.optLong("deadline_ms", 0) ->
                Decision.Refuse(ErrorCode.EXPIRED, "The request arrived past its deadline")
            cap.requiresLiveUser && body.optString("origin") != Protocol.ORIGIN_LIVE_USER ->
                Decision.Refuse(ErrorCode.POLICY_BLOCKED, "$name needs you to ask for it directly")
            cap.tier >= Protocol.TIER_CONFIRM && !wasApproved(body, name, params) ->
                Decision.Refuse(ErrorCode.POLICY_BLOCKED, "$name was not confirmed on this phone")
            else -> ParamCheck.validate(cap.paramsSchema, params)
                ?.let { Decision.Refuse(ErrorCode.INVALID_PARAMS, it) }
                ?: Decision.Run(cap, params)
        }
    }

    @Synchronized
    fun remember(invocationId: String, reply: JSONObject) {
        done[invocationId] = clock() to reply
    }

    /** Record what the user approved on this device, for [check] to hold invocations to. */
    @Synchronized
    fun approve(confirmationId: String, steps: List<Pair<String, JSONObject>>) {
        approved[confirmationId] = steps.map { stepKey(it.first, it.second) }.toSet()
    }

    private fun wasApproved(body: JSONObject, name: String, params: JSONObject): Boolean {
        val id = body.optJSONObject("confirmation")?.optString("id").orEmpty()
        return approved[id]?.contains(stepKey(name, params)) == true
    }

    private fun stepKey(action: String, params: JSONObject): String =
        action + "\u0000" + canonical(params)

    private fun canonical(o: JSONObject): String =
        o.keys().asSequence().sorted().joinToString(",", "{", "}") { k ->
            val v = o.get(k)
            JSONObject.quote(k) + ":" + if (v is JSONObject) canonical(v) else JSONArray().put(v).toString()
        }

    companion object {
        const val REPLAY_WINDOW_MS = 10 * 60 * 1000L
    }
}
