package com.aaditpani.nora.phone

import android.app.AlarmManager
import android.app.PendingIntent
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.util.Log
import com.aaditpani.nora.link.Capability
import com.aaditpani.nora.link.CapabilityResult
import com.aaditpani.nora.link.ErrorCode
import com.aaditpani.nora.ui.MainActivity
import org.json.JSONArray
import org.json.JSONObject

/**
 * The morning alarm NORA sets the night before from the calendar (Phase 8c).
 *
 * The core keeps the list and sends all of it on every connect, like the
 * geofences; this replaces whatever was set before. The alarms are
 * AlarmManager alarm clocks, so they go off with the link down, show in the
 * status bar like any alarm, and are kept in prefs so a reboot re-arms them
 * before the link is back. When one goes off, the phone rings like a call
 * from NORA, on the alarm stream (so silent mode doesn't mute it), and
 * answering says the morning's plan. Missed, it snoozes [SNOOZE_MS], twice.
 *
 * Not offered to the model: the core sets these from its calendar rule.
 */
class SetWakeAlarms(private val context: Context) : Capability {
    override val name = NAME
    override val description = "Replace NORA's morning alarms on the phone with these"
    override val tier = 0
    override val paramsSchema: JSONObject = JSONObject("""
        {"type":"object","required":["alarms"],
         "properties":{"alarms":{"type":"array","maxItems":7,"items":{
           "type":"object","required":["id","at_ms"],
           "properties":{"id":{"type":"string","maxLength":20},
                         "at_ms":{"type":"integer","minimum":0},
                         "title":{"type":"string","maxLength":120},
                         "say":{"type":"string","maxLength":600}}}}}}""")

    override suspend fun execute(params: JSONObject): CapabilityResult {
        val am = context.getSystemService(AlarmManager::class.java)
        if (!am.canScheduleExactAlarms()) {
            return CapabilityResult.Failed(ErrorCode.PERMISSION_DENIED,
                "NORA can't set alarms on the phone. Allow \"Alarms & reminders\" for NORA in Settings → Apps → NORA.",
                userAction = "grant_permission")
        }
        val set = WakeAlarms.replace(context, params.getJSONArray("alarms"))
        return CapabilityResult.Ok(JSONObject().put("set", set))
    }

    companion object {
        const val NAME = "phone.wake_alarm"
    }
}

object WakeAlarms {
    private const val TAG = "NoraWake"
    private const val PREFS = "nora_wake_alarms"
    const val SNOOZE_MS = 5 * 60_000L
    const val MAX_SNOOZES = 2
    const val EXTRA_ID = "id"
    const val EXTRA_TITLE = "title"
    const val EXTRA_SAY = "say"
    const val EXTRA_SNOOZES = "snoozes"

    /** Cancel what was set, set these; returns how many are set. */
    @Synchronized
    fun replace(context: Context, alarms: JSONArray): Int {
        val prefs = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
        val old = runCatching { JSONArray(prefs.getString("alarms", "[]")) }.getOrDefault(JSONArray())
        for (i in 0 until old.length()) cancel(context, old.getJSONObject(i).getString("id"))
        val now = System.currentTimeMillis()
        val kept = JSONArray()
        for (i in 0 until alarms.length()) {
            val a = alarms.getJSONObject(i)
            if (a.getLong("at_ms") <= now) continue
            arm(context, a)
            kept.put(a)
        }
        prefs.edit().putString("alarms", kept.toString()).apply()
        Log.i(TAG, "${kept.length()} alarm(s) set")
        return kept.length()
    }

    /** After a reboot: AlarmManager forgot them, the prefs didn't. */
    fun rearm(context: Context) {
        val prefs = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
        val list = runCatching { JSONArray(prefs.getString("alarms", "[]")) }.getOrDefault(JSONArray())
        if (list.length() > 0 && context.getSystemService(AlarmManager::class.java).canScheduleExactAlarms()) {
            replace(context, list)
        }
    }

    private fun arm(context: Context, a: JSONObject, at: Long = a.getLong("at_ms"), snoozes: Int = 0) {
        val am = context.getSystemService(AlarmManager::class.java)
        val show = PendingIntent.getActivity(context, 0, Intent(context, MainActivity::class.java),
            PendingIntent.FLAG_IMMUTABLE)
        try {
            am.setAlarmClock(AlarmManager.AlarmClockInfo(at, show), fire(context, a, snoozes))
        } catch (e: SecurityException) {
            Log.w(TAG, "exact alarms not allowed: $e")
        }
    }

    private fun fire(context: Context, a: JSONObject, snoozes: Int = 0): PendingIntent {
        val intent = Intent(context, WakeAlarmReceiver::class.java)
            .setAction(WakeAlarmReceiver.ACTION_FIRE)
            .putExtra(EXTRA_ID, a.getString("id"))
            .putExtra(EXTRA_TITLE, a.optString("title"))
            .putExtra(EXTRA_SAY, a.optString("say"))
            .putExtra(EXTRA_SNOOZES, snoozes)
        return PendingIntent.getBroadcast(context, requestCode(a.getString("id")), intent,
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
    }

    private fun cancel(context: Context, id: String) {
        val intent = Intent(context, WakeAlarmReceiver::class.java).setAction(WakeAlarmReceiver.ACTION_FIRE)
        val pi = PendingIntent.getBroadcast(context, requestCode(id), intent,
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_NO_CREATE) ?: return
        context.getSystemService(AlarmManager::class.java).cancel(pi)
        pi.cancel()
    }

    private fun requestCode(id: String) = 3_000 + (id.hashCode() and 0xFFFF)

    /** Ring again in [SNOOZE_MS], unless it has already snoozed [MAX_SNOOZES] times. */
    fun snooze(context: Context, call: Calls.Call) {
        if (call.alarmId == null || call.snoozes >= MAX_SNOOZES) return
        val a = JSONObject().put("id", call.alarmId).put("title", call.reason).put("say", call.say)
        arm(context, a, System.currentTimeMillis() + SNOOZE_MS, call.snoozes + 1)
        Log.i(TAG, "snoozed")
    }

    /** It went off: drop it from the list kept for reboots. */
    @Synchronized
    fun fired(context: Context, id: String) {
        val prefs = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
        val old = runCatching { JSONArray(prefs.getString("alarms", "[]")) }.getOrDefault(JSONArray())
        val kept = JSONArray()
        for (i in 0 until old.length()) if (old.getJSONObject(i).getString("id") != id) kept.put(old.getJSONObject(i))
        prefs.edit().putString("alarms", kept.toString()).apply()
    }
}

/** An alarm went off: ring like a call from NORA, on the alarm stream. */
class WakeAlarmReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action != ACTION_FIRE) return
        val id = intent.getStringExtra(WakeAlarms.EXTRA_ID) ?: return
        val title = intent.getStringExtra(WakeAlarms.EXTRA_TITLE).orEmpty().ifBlank { "Time to get up" }
        val say = intent.getStringExtra(WakeAlarms.EXTRA_SAY).orEmpty().ifBlank { title }
        val snoozes = intent.getIntExtra(WakeAlarms.EXTRA_SNOOZES, 0)
        WakeAlarms.fired(context, id)
        Log.i("NoraWake", "alarm $id going off (snoozed $snoozes)")
        if (!Calls.ring(context, title, say, alarmId = id, snoozes = snoozes)) {
            // Another call from NORA is ringing: go off again once it's done.
            WakeAlarms.snooze(context, Calls.Call(title, System.currentTimeMillis(), say,
                alarmId = id, snoozes = snoozes))
        }
        // The morning's link may be down; get it up for the conversation.
        if (context.controller.paired.value) runCatching { LinkService.start(context) }
    }

    companion object {
        const val ACTION_FIRE = "com.aaditpani.nora.WAKE_ALARM"
    }
}
