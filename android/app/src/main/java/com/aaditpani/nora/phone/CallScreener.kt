package com.aaditpani.nora.phone

import android.app.role.RoleManager
import android.content.Context
import android.telecom.Call
import android.telecom.CallScreeningService
import android.telecom.TelecomManager
import android.util.Log
import com.aaditpani.nora.link.Capability
import com.aaditpani.nora.link.CapabilityResult
import com.aaditpani.nora.link.ContactPick
import com.aaditpani.nora.link.ErrorCode
import com.aaditpani.nora.link.Screening
import org.json.JSONObject
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/**
 * Android asks this about each incoming call from a number not in the
 * contacts, once NORA holds the call-screening role. It answers on its own,
 * from what's on the phone, so screening works with the core unreachable;
 * the core hears of each call afterwards (`call.screened`, through the
 * outbox). It can let a call ring, keep it quiet, or reject it. It can't
 * hear or talk to the caller: no installed app can.
 */
class NoraCallScreeningService : CallScreeningService() {
    override fun onScreenCall(details: Call.Details) {
        if (details.callDirection != Call.Details.DIRECTION_INCOMING) {
            respondToCall(details, CallResponse.Builder().build())
            return
        }
        val prefs = controller.prefs
        val shown = details.handlePresentation == TelecomManager.PRESENTATION_ALLOWED
        val number = if (shown) details.handle?.schemeSpecificPart?.trim().orEmpty() else ""
        val action = Screening.decide(number.ifEmpty { null }, Screening.modeOf(prefs.screenMode), prefs.blockedNumbers)
        respondToCall(details, when (action) {
            Screening.Action.REJECTED -> CallResponse.Builder()
                .setDisallowCall(true).setRejectCall(true).setSkipNotification(true).build()
            Screening.Action.SILENCED -> CallResponse.Builder().setSilenceCall(true).build()
            Screening.Action.RANG -> CallResponse.Builder().build()
        })
        Log.i(TAG, "screened a call: ${action.wire}")
        if (action == Screening.Action.SILENCED) {
            val at = SimpleDateFormat("h:mm a", Locale.getDefault()).format(Date())
            Notifications.message(this, "Unknown number, silenced",
                "${number.ifEmpty { "A withheld number" }} called at $at. It's in your missed calls.")
        }
        controller.event("call.screened", JSONObject().put("number", number).put("action", action.wire))
    }

    companion object {
        private const val TAG = "NoraScreen"

        fun holdsRole(context: Context): Boolean =
            context.getSystemService(RoleManager::class.java).isRoleHeld(RoleManager.ROLE_CALL_SCREENING)
    }
}

/**
 * "Block the last number that called." The list lives on the phone, which
 * is what rejects the calls. Tier 1: it changes what the phone does, but
 * only for numbers not in the contacts, and unblocking undoes it.
 */
class BlockNumber(private val context: Context, private val prefs: LinkPrefs) : Capability {
    override val name = "phone.block_number"
    override val description =
        "Block a phone `number` that isn't a contact, so its calls are rejected without ringing; " +
            "`unblock` true undoes it. Needs NORA screening calls on the phone"
    override val tier = 1
    override val paramsSchema: JSONObject = JSONObject("""
        {"type":"object","required":["number"],
         "properties":{"number":{"type":"string","maxLength":32},
                       "unblock":{"type":"boolean"}}}""")

    override suspend fun execute(params: JSONObject): CapabilityResult {
        val number = params.getString("number").trim()
        val key = Screening.key(number)
        if (!ContactPick.isNumber(number) || key.isEmpty()) {
            return CapabilityResult.Failed(ErrorCode.INVALID_PARAMS, "That isn't a phone number.")
        }
        val unblock = params.optBoolean("unblock", false)
        prefs.blockedNumbers = if (unblock) prefs.blockedNumbers - key else prefs.blockedNumbers + key
        val message = when {
            unblock -> "Unblocked $number."
            NoraCallScreeningService.holdsRole(context) -> "Blocked $number. Its calls won't ring."
            else -> "Added $number to the block list, but NORA isn't screening calls yet: " +
                "turn on 'Screen unknown callers' under System, Phone access."
        }
        return CapabilityResult.Ok(JSONObject().put("blocked", !unblock).put("message", message))
    }
}
