package com.aaditpani.nora.phone

import android.Manifest
import android.app.NotificationManager
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.provider.ContactsContract.CommonDataKinds.Phone
import android.util.Log
import com.aaditpani.nora.link.Capability
import com.aaditpani.nora.link.CapabilityResult
import com.aaditpani.nora.link.ContactNumber
import com.aaditpani.nora.link.ContactPick
import com.aaditpani.nora.link.ErrorCode
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.launch
import org.json.JSONObject

/**
 * NORA calling this phone. It rings like an incoming call, and answering
 * opens a voice conversation that starts with why she called. No SIM and no
 * phone network: the ring arrives over the link like any other invoke, so
 * it needs the phone online and the link up.
 *
 * It rings for [RING_MS]. Unanswered or declined, the reason stays behind
 * as a missed-call notification, so nothing NORA called about is lost.
 */
object Calls {
    /** [say] is what she says on answering, when it's more than the ringing line. */
    data class Call(val reason: String, val startedAt: Long, val say: String = reason)

    const val RING_MS = 30_000L
    private const val TAG = "NoraCall"

    private val _ringing = MutableStateFlow<Call?>(null)
    /** The call ringing now, if one is. */
    val ringing: StateFlow<Call?> = _ringing
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main)
    private var timeout: Job? = null

    /** Start ringing. False when a call is already ringing. */
    @Synchronized
    fun ring(context: Context, reason: String, say: String = reason): Boolean {
        if (_ringing.value != null) return false
        val call = Call(reason, System.currentTimeMillis(), say)
        _ringing.value = call
        Notifications.incomingCall(context, call)
        timeout = scope.launch {
            delay(RING_MS)
            end(context, call, "missed")
        }
        Log.i(TAG, "ringing")
        return true
    }

    /** The user picked up. The call, or null when it had already stopped ringing. */
    @Synchronized
    fun answer(context: Context): Call? {
        val call = _ringing.value ?: return null
        stopRinging(context)
        Log.i(TAG, "answered after ${System.currentTimeMillis() - call.startedAt} ms")
        report(context, call, "answered")
        return call
    }

    @Synchronized
    fun decline(context: Context) {
        val call = _ringing.value ?: return
        end(context, call, "declined")
    }

    @Synchronized
    private fun end(context: Context, call: Call, outcome: String) {
        if (_ringing.value !== call) return
        stopRinging(context)
        Log.i(TAG, outcome)
        Notifications.message(context, "Missed call from NORA", call.reason)
        report(context, call, outcome)
    }

    private fun stopRinging(context: Context) {
        timeout?.cancel()
        timeout = null
        _ringing.value = null
        Notifications.cancelCall(context)
    }

    /** How it went, to the core's log. Not the reason: the core already has it. */
    private fun report(context: Context, call: Call, outcome: String) {
        context.controller.event("call.ended", JSONObject()
            .put("outcome", outcome)
            .put("ring_ms", System.currentTimeMillis() - call.startedAt))
    }
}

/** The Decline button on the ringing notification. */
class CallReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action == ACTION_DECLINE) Calls.decline(context)
    }

    companion object {
        const val ACTION_DECLINE = "com.aaditpani.nora.DECLINE_CALL"
    }
}

/**
 * The core ringing the phone. Tier 0 like a notification: it can't act on
 * anything, only get the user's attention, and the user decides to answer.
 */
class CallUser(private val context: Context) : Capability {
    override val name = "phone.call"
    override val description =
        "Ring the user's own phone like a call. `reason` is the full sentence NORA says on answering " +
            "(\"Time to take the laundry out.\"). For what needs them now. Not for calling other people"
    override val tier = 0
    // `say`, when the core sends it, is spoken on answering instead of `reason`
    // (a whole briefing); `reason` is still what the ringing screen shows.
    override val paramsSchema: JSONObject = JSONObject("""
        {"type":"object","required":["reason"],
         "properties":{"reason":{"type":"string","maxLength":300},
                       "say":{"type":"string","maxLength":3000}}}""")

    override suspend fun execute(params: JSONObject): CapabilityResult {
        if (context.checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) {
            return CapabilityResult.Failed(ErrorCode.PERMISSION_DENIED,
                "Notifications are turned off for NORA on the phone, so it can't ring", userAction = "grant_permission")
        }
        val reason = params.getString("reason").trim()
        if (reason.isEmpty()) return CapabilityResult.Failed(ErrorCode.INVALID_PARAMS, "'reason' is empty")
        if (context.controller.voice.active) {
            return CapabilityResult.Ok(JSONObject().put("rang", false)
                .put("message", "Didn't ring: the user is already talking to NORA on the phone. Tell them now."))
        }
        val say = params.optString("say").trim().ifEmpty { reason }
        if (!Calls.ring(context, reason, say)) {
            return CapabilityResult.Ok(JSONObject().put("rang", false)
                .put("message", "The phone is already ringing with another call from NORA."))
        }
        val fullScreen = context.getSystemService(NotificationManager::class.java).canUseFullScreenIntent()
        return CapabilityResult.Ok(JSONObject().put("rang", true).put("message", buildString {
            append("Ringing the phone. If it isn't answered in ${Calls.RING_MS / 1000} seconds it's left as a missed call.")
            if (!fullScreen) append(" Full-screen calls are off for NORA, so it rings as a banner.")
        }))
    }
}

/**
 * A real phone call, through the phone's own SIM and plan, to someone the
 * user names ("call Mom") or a number. Tier 2: it reaches another person, so
 * the user approves it on the phone first, and only ever when they asked for
 * it themselves. Without the call permission it opens the dialler instead,
 * with the number filled in.
 */
class CallContact(private val context: Context) : Capability {
    override val name = "phone.call_contact"
    override val description =
        "Place a real phone call from the user's phone to a contact by `name` (\"call Mom\") or a `number`. " +
            "The user approves it on the phone first"
    override val tier = 2
    override val requiresLiveUser = true
    override val paramsSchema: JSONObject = JSONObject("""
        {"type":"object",
         "properties":{"name":{"type":"string","maxLength":80},
                       "number":{"type":"string","maxLength":32}}}""")

    override suspend fun execute(params: JSONObject): CapabilityResult {
        val name = params.optString("name").trim()
        val number = params.optString("number").trim()
        val (who, dial) = when {
            number.isNotEmpty() -> {
                if (!ContactPick.isNumber(number)) {
                    return CapabilityResult.Failed(ErrorCode.INVALID_PARAMS, "That isn't a phone number.")
                }
                (name.ifEmpty { number }) to number
            }
            name.isEmpty() -> return CapabilityResult.Failed(ErrorCode.INVALID_PARAMS, "Who to call is missing.")
            context.checkSelfPermission(Manifest.permission.READ_CONTACTS) != PackageManager.PERMISSION_GRANTED ->
                return CapabilityResult.Failed(ErrorCode.PERMISSION_DENIED,
                    "NORA can't see the phone's contacts. Turn on 'Call people you name' under System, Phone access.",
                    userAction = "grant_permission")
            else -> when (val r = ContactPick.pick(name, Contacts.search(context, name))) {
                is ContactPick.Result.One -> r.name to r.number
                is ContactPick.Result.Several -> return CapabilityResult.Failed(ErrorCode.INVALID_PARAMS,
                    "More than one contact matches $name: ${r.names.joinToString(", ")}. Which one?")
                ContactPick.Result.None -> return CapabilityResult.Failed(ErrorCode.INVALID_PARAMS,
                    "There's no contact called $name on the phone.")
            }
        }
        val canCall = context.checkSelfPermission(Manifest.permission.CALL_PHONE) == PackageManager.PERMISSION_GRANTED
        val intent = Intent(if (canCall) Intent.ACTION_CALL else Intent.ACTION_DIAL, Uri.fromParts("tel", dial, null))
        Log.i(TAG, if (canCall) "placing a call" else "opening the dialler")
        return Launcher.open(context, intent, "the phone app",
            if (canCall) "Calling $who." else "The dialler is open with $who's number. Press call when you're ready.",
            JSONObject().put("called", canCall))
    }

    private companion object {
        const val TAG = "NoraCall"
    }
}

/** The phone's contacts, read only for [CallContact], only when asked. */
object Contacts {
    fun search(context: Context, name: String): List<ContactNumber> {
        val uri = Uri.withAppendedPath(Phone.CONTENT_FILTER_URI, Uri.encode(name))
        val cols = arrayOf(Phone.CONTACT_ID, Phone.DISPLAY_NAME_PRIMARY, Phone.NUMBER, Phone.TYPE, Phone.IS_SUPER_PRIMARY)
        val out = mutableListOf<ContactNumber>()
        context.contentResolver.query(uri, cols, null, null, null)?.use { c ->
            while (c.moveToNext() && out.size < 50) {
                val number = c.getString(2) ?: continue
                out += ContactNumber(c.getLong(0), c.getString(1).orEmpty(), number,
                    mobile = c.getInt(3) == Phone.TYPE_MOBILE, primary = c.getInt(4) != 0)
            }
        }
        return out
    }
}
