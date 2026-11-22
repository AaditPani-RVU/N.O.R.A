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
    /** False while what it needs is switched off (the accessibility service, Phase 9): the core leaves it out. */
    val available: Boolean get() = true

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
    .put("available", available)

/**
 * The subset of JSON Schema this app's own capabilities use: objects of
 * string / integer / number / boolean properties, arrays of those or of
 * objects (`items`, `maxItems`), `required`, `maxLength`, `enum`,
 * `minimum`/`maximum`, and no unknown fields at any depth. The core validates
 * too; this is the device not taking the core's word for it (plan §7.6).
 */
object ParamCheck {
    private const val MAX_DEPTH = 4

    fun validate(schema: JSONObject, params: JSONObject): String? = checkObject("", schema, params, 0)

    private fun checkObject(path: String, schema: JSONObject, obj: JSONObject, depth: Int): String? {
        val props = schema.optJSONObject("properties") ?: JSONObject()
        for (key in obj.keys()) {
            if (!props.has(key)) return "unknown field '${path + key}'"
        }
        val required = schema.optJSONArray("required") ?: JSONArray()
        for (i in 0 until required.length()) {
            val key = required.getString(i)
            if (!obj.has(key) || obj.isNull(key)) return "'${path + key}' is required"
        }
        for (key in obj.keys()) {
            checkValue(path + key, props.getJSONObject(key), obj.get(key), depth)?.let { return it }
        }
        return null
    }

    private fun checkValue(key: String, spec: JSONObject, value: Any, depth: Int): String? {
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
            "number" -> {
                if (value !is Number) return "'$key' must be a number"
                val n = value.toDouble()
                if (n.isNaN() || n.isInfinite()) return "'$key' must be a number"
                if (spec.has("minimum") && n < spec.getDouble("minimum")) return "'$key' is too small"
                if (spec.has("maximum") && n > spec.getDouble("maximum")) return "'$key' is too large"
            }
            "boolean" -> if (value !is Boolean) return "'$key' must be true or false"
            "array" -> {
                if (value !is JSONArray) return "'$key' must be a list"
                if (depth >= MAX_DEPTH) return "'$key' is nested too deeply"
                val max = spec.optInt("maxItems", Int.MAX_VALUE)
                if (value.length() > max) return "'$key' has more than $max items"
                val items = spec.optJSONObject("items") ?: return "'$key' has a type this device does not accept"
                for (i in 0 until value.length()) {
                    if (value.isNull(i)) return "'$key[$i]' is empty"
                    checkValue("$key[$i]", items, value.get(i), depth + 1)?.let { return it }
                }
            }
            "object" -> {
                if (value !is JSONObject) return "'$key' must be an object"
                if (depth >= MAX_DEPTH) return "'$key' is nested too deeply"
                checkObject("$key.", spec, value, depth + 1)?.let { return it }
            }
            else -> return "'$key' has a type this device does not accept"
        }
        val allowed = spec.optJSONArray("enum")
        if (allowed != null && (0 until allowed.length()).none { allowed.get(it) == value }) {
            return "'$key' is not one of the allowed values"
        }
        return null
    }
}
