package com.aaditpani.nora.phone

import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.os.BatteryManager
import android.os.PowerManager
import org.json.JSONObject

/**
 * Phase 10's exit criterion is battery drain under 3 % a day while idle.
 * The phone can't judge "idle" over a day by itself without waking up to
 * look, so it reports a `battery.sample` event whenever something changes
 * anyway — the level moves a percent, the charger, the screen goes on or
 * off — and the core does the arithmetic (`nora/battery_stats.py`). No
 * timers, no wakeups of its own: the samples ride broadcasts the phone
 * sends regardless, and the outbox holds them while offline.
 */
object BatterySampler {
    private var last: Triple<Int, Boolean, Boolean>? = null

    @Synchronized
    fun onBroadcast(context: Context, intent: Intent) {
        val sticky = if (intent.action == Intent.ACTION_BATTERY_CHANGED) intent
        else context.registerReceiver(null, IntentFilter(Intent.ACTION_BATTERY_CHANGED)) ?: return
        val level = sticky.getIntExtra(BatteryManager.EXTRA_LEVEL, -1)
        val scale = sticky.getIntExtra(BatteryManager.EXTRA_SCALE, 100).coerceAtLeast(1)
        if (level < 0) return
        val pct = level * 100 / scale
        val status = sticky.getIntExtra(BatteryManager.EXTRA_STATUS, -1)
        val charging = status == BatteryManager.BATTERY_STATUS_CHARGING || status == BatteryManager.BATTERY_STATUS_FULL
        val screenOn = when (intent.action) {
            Intent.ACTION_SCREEN_ON -> true
            Intent.ACTION_SCREEN_OFF -> false
            else -> context.getSystemService(PowerManager::class.java).isInteractive
        }
        val now = Triple(pct, charging, screenOn)
        if (now == last) return            // BATTERY_CHANGED also fires for temperature and voltage
        last = now
        val c = context.controller
        if (!c.paired.value) return
        c.event("battery.sample", JSONObject().put("level", pct).put("charging", charging).put("screen_on", screenOn))
    }
}
