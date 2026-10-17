package com.aaditpani.nora.phone

import android.Manifest
import android.app.NotificationManager
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.util.Log
import com.aaditpani.nora.link.Capability
import com.aaditpani.nora.link.CapabilityResult
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
    data class Call(val reason: String, val startedAt: Long)

    const val RING_MS = 30_000L
    private const val TAG = "NoraCall"

    private val _ringing = MutableStateFlow<Call?>(null)
    /** The call ringing now, if one is. */
    val ringing: StateFlow<Call?> = _ringing
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main)
    private var timeout: Job? = null

    /** Start ringing. False when a call is already ringing. */
    @Synchronized
    fun ring(context: Context, reason: String): Boolean {
        if (_ringing.value != null) return false
        val call = Call(reason, System.currentTimeMillis())
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
        "Ring the user's phone like a call. `reason` is the full sentence NORA says when they answer " +
            "(\"It's time to take the laundry out.\"). For what needs them now, not routine messages"
    override val tier = 0
    override val paramsSchema: JSONObject = JSONObject("""
        {"type":"object","required":["reason"],
         "properties":{"reason":{"type":"string","maxLength":300}}}""")

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
        if (!Calls.ring(context, reason)) {
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
