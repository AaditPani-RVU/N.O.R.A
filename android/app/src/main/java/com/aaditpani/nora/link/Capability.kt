package com.aaditpani.nora.link

import org.json.JSONArray
import org.json.JSONObject

/**
 * Something the core may ask this device to do. The core can only invoke
 * what is compiled in as a [Capability] *and* switched on in the app — there
 * is no generic intent, shell or reflection capability (plan §6).
 */
interface Capability {
    val name: String
    val version: Int get() = 1
    /** Read by the core's LLM when it picks an action. At most 200 characters. */
    val description: String
    /** This device's own tier for it. The core may raise it, never lower it. */
    val tier: Int
    val paramsSchema: JSONObject
    val requiresLiveUser: Boolean get() = false
    /**
     * Its result carries text someone other than the user wrote (a
     * notification). The core summarises it without tools, doesn't keep it,
     * and asks before acting on anything decided after reading it (plan §7.5).
     */
    val untrustedOutput: Boolean get() = false

    suspend fun execute(params: JSONObject): CapabilityResult
}

sealed class CapabilityResult {
    /** [result] should carry a `message`: it is what NORA reads to answer. */
    data class Ok(val result: JSONObject) : CapabilityResult()
    data class Failed(
        val code: String,
        val message: String,
        val retryable: Boolean = false,
        val userAction: String? = null,
    ) : CapabilityResult()
}

fun Capability.manifestEntry(): JSONObject = JSONObject()
    .put("name", name)
    .put("version", version)
    .put("description", description)
    .put("params_schema", paramsSchema)
    .put("tier", tier)
    .put("requires_live_user", requiresLiveUser)
    .put("untrusted_output", untrustedOutput)
    .put("available", true)

/**
 * The subset of JSON Schema this app's own capabilities use: an object of
 * string / integer / boolean properties, `required`, `maxLength`, `enum`,
 * `minimum`/`maximum`, and no unknown fields. The core validates too; this is
 * the device not taking the core's word for it (plan §7.6).
 */
object ParamCheck {
    fun validate(schema: JSONObject, params: JSONObject): String? {
        val props = schema.optJSONObject("properties") ?: JSONObject()
        for (key in params.keys()) {
            if (!props.has(key)) return "unknown field '$key'"
        }
        val required = schema.optJSONArray("required") ?: JSONArray()
        for (i in 0 until required.length()) {
            val key = required.getString(i)
            if (!params.has(key) || params.isNull(key)) return "'$key' is required"
        }
        for (key in params.keys()) {
            val spec = props.getJSONObject(key)
            val value = params.get(key)
            when (spec.optString("type")) {
                "string" -> {
                    if (value !is String) return "'$key' must be a string"
                    val max = spec.optInt("maxLength", Int.MAX_VALUE)
                    if (value.length > max) return "'$key' is longer than $max"
                }
                "integer" -> {
                    if (value !is Int && value !is Long) return "'$key' must be an integer"
                    val n = (value as Number).toLong()
                    if (spec.has("minimum") && n < spec.getLong("minimum")) return "'$key' is too small"
                    if (spec.has("maximum") && n > spec.getLong("maximum")) return "'$key' is too large"
                }
                "boolean" -> if (value !is Boolean) return "'$key' must be true or false"
                else -> return "'$key' has a type this device does not accept"
            }
            val allowed = spec.optJSONArray("enum")
            if (allowed != null && (0 until allowed.length()).none { allowed.get(it) == value }) {
                return "'$key' is not one of the allowed values"
            }
        }
        return null
    }
}
