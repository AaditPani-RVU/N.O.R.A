package com.aaditpani.nora.phone

import android.Manifest
import android.app.AlarmManager
import android.app.Notification
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.RemoteInput
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Bundle
import android.telephony.SmsManager
import android.telephony.TelephonyManager
import android.util.Log
import com.aaditpani.nora.link.Capability
import com.aaditpani.nora.link.CapabilityResult
import com.aaditpani.nora.link.ContactPick
import com.aaditpani.nora.link.ErrorCode
import com.aaditpani.nora.link.ReplyPick
import com.aaditpani.nora.link.WaNumber
import org.json.JSONObject

/*
 * Phase 8d: what reaches other people, or changes how the phone interrupts.
 * Every one of these asks on the phone first (tier ≥ 2; SMS needs the
 * fingerprint), and the phone renders that prompt from the parameters
 * itself (`ConfirmSteps.describe`), so what's shown is what runs.
 */

private const val TAG = "NoraMessaging"

/** Who [name] or [number] is, by the phone's contacts. A failure to return, or (name, number). */
internal fun resolveContact(context: Context, name: String, number: String): Pair<CapabilityResult.Failed?, Pair<String, String>> {
    val none = "" to ""
    if (number.isNotEmpty()) {
        if (!ContactPick.isNumber(number)) {
            return CapabilityResult.Failed(ErrorCode.INVALID_PARAMS, "That isn't a phone number.") to none
        }
        return null to (name.ifEmpty { number } to number)
    }
    if (name.isEmpty()) return CapabilityResult.Failed(ErrorCode.INVALID_PARAMS, "Who to send it to is missing.") to none
    if (context.checkSelfPermission(Manifest.permission.READ_CONTACTS) != PackageManager.PERMISSION_GRANTED) {
        return CapabilityResult.Failed(ErrorCode.PERMISSION_DENIED,
            "NORA can't see the phone's contacts. Turn on 'Text people you name' under Phone access in the NORA app.",
            userAction = "grant_permission") to none
    }
    return when (val r = ContactPick.pick(name, Contacts.search(context, name))) {
        is ContactPick.Result.One -> null to (r.name to r.number)
        is ContactPick.Result.Several -> CapabilityResult.Failed(ErrorCode.INVALID_PARAMS,
            "More than one contact matches $name: ${r.names.joinToString(", ")}. Which one?") to none
        ContactPick.Result.None -> CapabilityResult.Failed(ErrorCode.INVALID_PARAMS,
            "There's no contact called $name on the phone.") to none
    }
}

/**
 * Reply to a conversation through its notification's own reply box — the
 * way a smartwatch does, with no UI automation: "reply to Mom on WhatsApp:
 * on my way". Only notifications still on screen with a reply action count.
 */
class ReplyNotification(private val context: Context, private val blocked: () -> Set<String>) : Capability {
    override val name = "phone.reply_notification"
    override val description =
        "Reply to a message notification on the phone (WhatsApp, Messages, Telegram…) through its reply box. " +
            "`to` is the conversation as the notification titles it; `app` narrows it. Asks on the phone first"
    override val tier = 2
    override val paramsSchema: JSONObject = JSONObject("""
        {"type":"object","required":["to","text"],
         "properties":{"to":{"type":"string","maxLength":80},
                       "app":{"type":"string","maxLength":40},
                       "text":{"type":"string","minLength":1,"maxLength":1000}}}""")

    override suspend fun execute(params: JSONObject): CapabilityResult {
        if (!NoraNotificationListener.hasAccess(context)) {
            return CapabilityResult.Failed(ErrorCode.PERMISSION_DENIED,
                "NORA needs notification access on the phone to reply. Switch it on from the NORA app.",
                userAction = "grant_notification_access")
        }
        val to = params.getString("to").trim()
        val app = params.optString("app").trim()
        val text = params.getString("text").trim()
        if (text.isEmpty()) return CapabilityResult.Failed(ErrorCode.INVALID_PARAMS, "The reply is empty.")
        val off = blocked()
        val open = NoraNotificationListener.replyable().filter { it.first.packageName !in off }
        val note = when (val r = ReplyPick.pick(to, app, open.map { it.first })) {
            is ReplyPick.Result.One -> r.note
            is ReplyPick.Result.Several -> return CapabilityResult.Failed(ErrorCode.INVALID_PARAMS,
                "More than one conversation matches $to: ${r.titles.joinToString(", ")}. Which one?")
            ReplyPick.Result.None -> return CapabilityResult.Failed(ErrorCode.INVALID_PARAMS,
                "There's no notification from $to${if (app.isNotEmpty()) " in $app" else ""} on the phone to reply to. " +
                    "It may have been dismissed.")
        }
        val action = open.first { it.first.key == note.key }.second
        val inputs = action.remoteInputs ?: return CapabilityResult.Failed(ErrorCode.EXECUTION_FAILED,
            "That notification can't be replied to.")
        val results = Bundle().apply { inputs.forEach { putCharSequence(it.resultKey, text) } }
        val fill = Intent()
        RemoteInput.addResultsToIntent(inputs, fill, results)
        return try {
            action.actionIntent.send(context, 0, fill)
            Log.i(TAG, "replied through ${note.packageName}")
            val who = ReplyPick.conversation(note.title)
            CapabilityResult.Ok(JSONObject().put("app", note.app).put("to", who)
                .put("message", "Replied to $who on ${note.app}."))
        } catch (e: PendingIntent.CanceledException) {
            CapabilityResult.Failed(ErrorCode.EXECUTION_FAILED,
                "${note.app} no longer takes replies on that notification.", retryable = false)
        }
    }
}

/**
 * A text message from the phone's own SIM. Tier 3 (fingerprint) and only
 * when asked live: it costs money and speaks for the user. Without the SMS
 * permission it opens the messages app with the text filled in instead.
 */
class SendSms(private val context: Context) : Capability {
    override val name = "phone.send_sms"
    override val description =
        "Send a text message (SMS) from the user's phone to a contact by `name` or a `number`. " +
            "The user approves it with their fingerprint first"
    override val tier = 3
    override val requiresLiveUser = true
    override val paramsSchema: JSONObject = JSONObject("""
        {"type":"object","required":["text"],
         "properties":{"name":{"type":"string","maxLength":80},
                       "number":{"type":"string","maxLength":32},
                       "text":{"type":"string","minLength":1,"maxLength":1000}}}""")

    override suspend fun execute(params: JSONObject): CapabilityResult {
        val text = params.getString("text").trim()
        if (text.isEmpty()) return CapabilityResult.Failed(ErrorCode.INVALID_PARAMS, "The message is empty.")
        val (problem, who) = resolveContact(context, params.optString("name").trim(), params.optString("number").trim())
        if (problem != null) return problem
        val (label, number) = who
        if (context.checkSelfPermission(Manifest.permission.SEND_SMS) != PackageManager.PERMISSION_GRANTED) {
            val compose = Intent(Intent.ACTION_SENDTO, Uri.fromParts("smsto", number, null)).putExtra("sms_body", text)
            return Launcher.open(context, compose, "Messages",
                "Messages is open with your text to $label. Press send when you're ready.", JSONObject().put("sent", false))
        }
        return try {
            val sms = context.getSystemService(SmsManager::class.java)
            sms.sendMultipartTextMessage(number, null, sms.divideMessage(text), null, null)
            Log.i(TAG, "sms sent")
            CapabilityResult.Ok(JSONObject().put("sent", true).put("message", "Texted $label."))
        } catch (e: Exception) {
            Log.w(TAG, "sms failed: $e")
            CapabilityResult.Failed(ErrorCode.EXECUTION_FAILED, "The phone couldn't send the text.", retryable = true)
        }
    }
}

/**
 * A WhatsApp message, opened ready to send: the user presses send in
 * WhatsApp themselves, so it is human-in-the-loop by construction. Opening
 * an app needs NORA on screen; otherwise it is a tap-to-open notification.
 */
class SendWhatsApp(private val context: Context) : Capability {
    override val name = "phone.send_whatsapp"
    override val description =
        "Open WhatsApp on the phone with a message to a contact (`name` or `number`) ready to send; the user presses send"
    override val tier = 2
    override val requiresLiveUser = true
    override val paramsSchema: JSONObject = JSONObject("""
        {"type":"object","required":["text"],
         "properties":{"name":{"type":"string","maxLength":80},
                       "number":{"type":"string","maxLength":32},
                       "text":{"type":"string","minLength":1,"maxLength":1000}}}""")

    override suspend fun execute(params: JSONObject): CapabilityResult {
        val text = params.getString("text").trim()
        val (problem, who) = resolveContact(context, params.optString("name").trim(), params.optString("number").trim())
        if (problem != null) return problem
        val (label, number) = who
        val country = countryCode()
        val wa = WaNumber.of(number, country) ?: return CapabilityResult.Failed(ErrorCode.INVALID_PARAMS,
            "$label's number has no country code, so WhatsApp can't find it.")
        val intent = Intent(Intent.ACTION_VIEW, Uri.parse("https://wa.me/$wa?text=${Uri.encode(text)}"))
        if (installed("com.whatsapp")) intent.setPackage("com.whatsapp")
        return Launcher.open(context, intent, "WhatsApp",
            "WhatsApp is open with your message to $label. Press send when it looks right.")
    }

    private fun installed(pkg: String) = runCatching { context.packageManager.getPackageInfo(pkg, 0) }.isSuccess

    /** The calling code for the SIM's country, for numbers saved without one. */
    private fun countryCode(): String? {
        val iso = context.getSystemService(TelephonyManager::class.java)?.let { tm ->
            tm.simCountryIso.ifBlank { tm.networkCountryIso }
        }?.uppercase().orEmpty()
        return CALLING_CODES[iso]
    }

    private companion object {
        // Enough for this phone's SIM and its neighbours; anything else needs a + in the contact.
        val CALLING_CODES = mapOf("IN" to "91", "US" to "1", "CA" to "1", "GB" to "44", "AE" to "971",
            "SG" to "65", "AU" to "61", "DE" to "49", "FR" to "33", "NP" to "977", "LK" to "94", "BD" to "880")
    }
}

/**
 * Do Not Disturb on or off, optionally for so many minutes. Tier 2: it
 * decides who can reach the user. Notification access counts as Do Not
 * Disturb access on Android, so there's usually nothing more to allow.
 */
class Dnd(private val context: Context) : Capability {
    override val name = "phone.dnd"
    override val description = "Turn Do Not Disturb on the phone on or off; `minutes` turns it back off after that long"
    override val tier = 2
    override val paramsSchema: JSONObject = JSONObject("""
        {"type":"object","required":["on"],
         "properties":{"on":{"type":"boolean"},
                       "minutes":{"type":"integer","minimum":1,"maximum":720}}}""")

    override suspend fun execute(params: JSONObject): CapabilityResult {
        val nm = context.getSystemService(NotificationManager::class.java)
        if (!nm.isNotificationPolicyAccessGranted) {
            return CapabilityResult.Failed(ErrorCode.PERMISSION_DENIED,
                "NORA isn't allowed to change Do Not Disturb. Allow it in Settings → Notifications → Do Not Disturb → Apps, " +
                    "or give NORA notification access.", userAction = "grant_permission")
        }
        val on = params.getBoolean("on")
        val minutes = params.optInt("minutes", 0)
        nm.setInterruptionFilter(if (on) NotificationManager.INTERRUPTION_FILTER_PRIORITY else NotificationManager.INTERRUPTION_FILTER_ALL)
        DndOff.schedule(context, if (on && minutes > 0) System.currentTimeMillis() + minutes * 60_000L else null)
        val message = when {
            !on -> "Do Not Disturb is off."
            minutes > 0 -> "Do Not Disturb is on for $minutes minute${if (minutes == 1) "" else "s"}."
            else -> "Do Not Disturb is on."
        }
        return CapabilityResult.Ok(JSONObject().put("on", on).put("message", message))
    }
}

/** Turns Do Not Disturb back off when its minutes are up. */
class DndOff : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        val nm = context.getSystemService(NotificationManager::class.java)
        if (nm.isNotificationPolicyAccessGranted) nm.setInterruptionFilter(NotificationManager.INTERRUPTION_FILTER_ALL)
        Log.i(TAG, "do not disturb ended")
    }

    companion object {
        fun schedule(context: Context, atMs: Long?) {
            val am = context.getSystemService(AlarmManager::class.java)
            val pi = PendingIntent.getBroadcast(context, 41, Intent(context, DndOff::class.java), PendingIntent.FLAG_IMMUTABLE)
            am.cancel(pi)
            if (atMs != null) am.setAndAllowWhileIdle(AlarmManager.RTC_WAKEUP, atMs, pi)
        }
    }
}

/** The reply action of a notification, if it has a free-text one. */
internal fun replyAction(n: Notification): Notification.Action? =
    n.actions?.firstOrNull { a -> a.remoteInputs?.any { it.allowFreeFormInput } == true }
