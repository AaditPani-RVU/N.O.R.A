package com.aaditpani.nora.phone

import android.accessibilityservice.AccessibilityService
import android.content.Context
import android.content.Intent
import android.net.Uri
import android.util.Log
import android.view.accessibility.AccessibilityEvent
import android.view.accessibility.AccessibilityNodeInfo
import com.aaditpani.nora.link.Capability
import com.aaditpani.nora.link.CapabilityResult
import com.aaditpani.nora.link.ErrorCode
import com.aaditpani.nora.link.UiMatch
import com.aaditpani.nora.link.UiNode
import com.aaditpani.nora.link.UiRecipes
import com.aaditpani.nora.link.UiTarget
import com.aaditpani.nora.link.WaNumber
import kotlinx.coroutines.delay
import org.json.JSONObject

/**
 * Phase 9, optional and off by default: an accessibility service that runs
 * **named recipes only** — "send this WhatsApp", "call on WhatsApp", "start
 * navigation in Maps" — and nothing else. There is no tap(x, y), no "press
 * whatever has this label", no reading of the screen back to the core.
 *
 *  - The system only shows the service WhatsApp's and Maps' windows (its
 *    config lists those packages), and every press is matched again against
 *    the recipe's own package ([UiMatch] ignores nodes of any other app).
 *  - Each recipe is its own capability at tier ≥ 2, live user only, so the
 *    phone asks first (a fingerprint to send a message) every time.
 *  - The recipes report unavailable until the user switches the service on
 *    in Settings → Accessibility, and again if they switch it off.
 *
 * Brittle by nature: a WhatsApp or Maps update can move a button, and then
 * the recipe says it couldn't find it rather than pressing something else.
 */
class NoraAccessibilityService : AccessibilityService() {
    override fun onServiceConnected() {
        instance = this
        Log.i(TAG, "connected")
        runCatching { applicationContext.controller.refreshManifest() }
    }

    override fun onUnbind(intent: Intent?): Boolean {
        gone()
        return super.onUnbind(intent)
    }

    override fun onDestroy() {
        gone()
        super.onDestroy()
    }

    private fun gone() {
        if (instance === this) {
            instance = null
            runCatching { applicationContext.controller.refreshManifest() }
        }
    }

    // Recipes look when they need to; events are not watched or kept.
    override fun onAccessibilityEvent(event: AccessibilityEvent?) = Unit
    override fun onInterrupt() = Unit

    /** Wait up to [timeoutMs] for [target] in [pkg]'s window in front, then press it. */
    suspend fun press(pkg: String, target: UiTarget, timeoutMs: Long): Boolean {
        require(pkg in UiRecipes.ALLOWED)
        val until = System.currentTimeMillis() + timeoutMs
        while (System.currentTimeMillis() < until) {
            val root = rootInActiveWindow
            if (root != null && root.packageName?.toString() == pkg) {
                val hit = UiMatch.find(tree(root, pkg, 0, IntArray(1)), pkg, target)
                val node = hit?.ref as? AccessibilityNodeInfo
                if (node != null && node.performAction(AccessibilityNodeInfo.ACTION_CLICK)) {
                    Log.i(TAG, "pressed in $pkg")
                    return true
                }
            }
            delay(POLL_MS)
        }
        return false
    }

    /** The window as [UiNode]s, bounded in depth and size. */
    private fun tree(n: AccessibilityNodeInfo, pkg: String, depth: Int, count: IntArray): UiNode {
        count[0]++
        val kids = if (depth >= 30 || count[0] > 2000) emptyList()
        else (0 until n.childCount).mapNotNull { i -> n.getChild(i)?.let { tree(it, pkg, depth + 1, count) } }
        return UiNode(n.packageName?.toString().orEmpty(), n.viewIdResourceName, n.contentDescription?.toString(),
            n.text?.toString(), n.isClickable, n.isEnabled, kids, n)
    }

    companion object {
        private const val TAG = "NoraUi"
        private const val POLL_MS = 250L

        @Volatile
        var instance: NoraAccessibilityService? = null
            private set
    }
}

/** Shared by the recipes: the service, or why there isn't one. */
private fun service(): Pair<NoraAccessibilityService?, CapabilityResult.Failed?> {
    val s = NoraAccessibilityService.instance
    return if (s != null) s to null else null to CapabilityResult.Failed(ErrorCode.CAPABILITY_UNAVAILABLE,
        "NORA's accessibility service is off. Switch it on in Settings → Accessibility → NORA to let her press buttons.",
        userAction = "grant_permission")
}

private fun openWhatsApp(context: Context, wa: String, text: String?): Boolean {
    val url = "https://wa.me/$wa" + (text?.let { "?text=${Uri.encode(it)}" } ?: "")
    val intent = Intent(Intent.ACTION_VIEW, Uri.parse(url)).setPackage(UiRecipes.WHATSAPP)
        .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TOP)
    // An app with a bound accessibility service may start activities from the background.
    return runCatching { context.startActivity(intent) }.isSuccess
}

private fun waNumber(context: Context, params: JSONObject): Pair<CapabilityResult.Failed?, Pair<String, String>> {
    val (problem, who) = resolveContact(context, params.optString("name").trim(), params.optString("number").trim())
    if (problem != null) return problem to ("" to "")
    val country = context.getSystemService(android.telephony.TelephonyManager::class.java)
        ?.let { it.simCountryIso.ifBlank { it.networkCountryIso } }?.uppercase()
    val wa = WaNumber.of(who.second, if (country == "IN") "91" else null)
        ?: return CapabilityResult.Failed(ErrorCode.INVALID_PARAMS,
            "${who.first}'s number has no country code, so WhatsApp can't find it.") to ("" to "")
    return null to (who.first to wa)
}

private val PERSON = """"name":{"type":"string","maxLength":80},"number":{"type":"string","maxLength":32}"""

/** Recipe 1: send a WhatsApp message, pressing Send. Tier 3: it speaks for the user, untouched. */
class UiWhatsAppSend(private val context: Context) : Capability {
    override val name = "phone.ui.whatsapp_send"
    override val description =
        "Send a WhatsApp message to a contact (`name` or `number`) and press send, using the accessibility service. " +
            "Fingerprint first"
    override val tier = 3
    override val requiresLiveUser = true
    override val available get() = NoraAccessibilityService.instance != null
    override val paramsSchema: JSONObject = JSONObject("""
        {"type":"object","required":["text"],
         "properties":{$PERSON,"text":{"type":"string","minLength":1,"maxLength":1000}}}""")

    override suspend fun execute(params: JSONObject): CapabilityResult {
        val (svc, off) = service()
        if (svc == null) return off!!
        val text = params.getString("text").trim()
        val (problem, who) = waNumber(context, params)
        if (problem != null) return problem
        if (!openWhatsApp(context, who.second, text)) {
            return CapabilityResult.Failed(ErrorCode.EXECUTION_FAILED, "WhatsApp wouldn't open.")
        }
        if (!svc.press(UiRecipes.WHATSAPP, UiRecipes.WHATSAPP_SEND, 10_000)) {
            return CapabilityResult.Failed(ErrorCode.EXECUTION_FAILED,
                "WhatsApp opened with the message but I couldn't find its send button. It's waiting there for you.")
        }
        return CapabilityResult.Ok(JSONObject().put("sent", true).put("message", "Sent to ${who.first} on WhatsApp."))
    }
}

/** Recipe 2: a WhatsApp voice call. */
class UiWhatsAppCall(private val context: Context) : Capability {
    override val name = "phone.ui.whatsapp_call"
    override val description =
        "Start a WhatsApp voice call to a contact (`name` or `number`), using the accessibility service. Asks first"
    override val tier = 2
    override val requiresLiveUser = true
    override val available get() = NoraAccessibilityService.instance != null
    override val paramsSchema: JSONObject = JSONObject("""{"type":"object","properties":{$PERSON}}""")

    override suspend fun execute(params: JSONObject): CapabilityResult {
        val (svc, off) = service()
        if (svc == null) return off!!
        val (problem, who) = waNumber(context, params)
        if (problem != null) return problem
        if (!openWhatsApp(context, who.second, null)) {
            return CapabilityResult.Failed(ErrorCode.EXECUTION_FAILED, "WhatsApp wouldn't open.")
        }
        if (!svc.press(UiRecipes.WHATSAPP, UiRecipes.WHATSAPP_VOICE_CALL, 10_000)) {
            return CapabilityResult.Failed(ErrorCode.EXECUTION_FAILED,
                "WhatsApp opened ${who.first}'s chat but I couldn't find the call button.")
        }
        // Some contacts get a "Start voice call?" check; press it if it shows.
        svc.press(UiRecipes.WHATSAPP, UiRecipes.WHATSAPP_CALL_CONFIRM, 2_500)
        return CapabilityResult.Ok(JSONObject().put("message", "Calling ${who.first} on WhatsApp."))
    }
}

/** Recipe 3: press Start on the directions Maps is showing (after phone.navigate). */
class UiMapsStart(private val context: Context) : Capability {
    override val name = "phone.ui.maps_start"
    override val description =
        "Press Start on the directions Google Maps is showing, to begin turn-by-turn navigation (after navigate_to)"
    override val tier = 2
    override val available get() = NoraAccessibilityService.instance != null
    override val paramsSchema: JSONObject = JSONObject("""{"type":"object","properties":{}}""")

    override suspend fun execute(params: JSONObject): CapabilityResult {
        val (svc, off) = service()
        if (svc == null) return off!!
        if (!svc.press(UiRecipes.MAPS, UiRecipes.MAPS_START, 6_000)) {
            return CapabilityResult.Failed(ErrorCode.EXECUTION_FAILED,
                "Maps isn't showing directions with a Start button. Ask for directions first.")
        }
        return CapabilityResult.Ok(JSONObject().put("message", "Navigation started."))
    }
}
