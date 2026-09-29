package com.aaditpani.nora.phone

import android.Manifest
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.content.pm.PackageManager
import android.media.AudioManager
import android.net.ConnectivityManager
import android.net.NetworkCapabilities
import android.os.BatteryManager
import android.os.PowerManager
import com.aaditpani.nora.link.Capability
import com.aaditpani.nora.link.CapabilityResult
import com.aaditpani.nora.link.ErrorCode
import org.json.JSONObject

/** Everything compiled into this build. The user can switch each one off. */
fun allCapabilities(context: Context, prefs: LinkPrefs): List<Capability> {
    val app = context.applicationContext
    return listOf(
        DeviceStatus(app), PhoneNotify(app),
        // Phase 4
        OpenApp(app), OpenUrl(app), PlayMedia(app), MediaControl(app), Volume(app),
        ReadNotifications(app) { prefs.hiddenNoteApps }, GetLocation(app), Navigate(app),
        SetAlarm(app), SetTimer(app), Dial(app),
    )
}

/** Battery, network and ringer. Tier 0: it reads, it doesn't act. */
class DeviceStatus(private val context: Context) : Capability {
    override val name = "device.status"
    override val description =
        "The user's phone: battery level, whether it is charging, network (Wi-Fi or mobile data) and ringer mode"
    override val tier = 0
    override val paramsSchema: JSONObject = JSONObject("""{"type":"object","properties":{}}""")

    override suspend fun execute(params: JSONObject): CapabilityResult =
        CapabilityResult.Ok(read(context))

    companion object {
        fun read(context: Context): JSONObject {
            val battery = context.getSystemService(BatteryManager::class.java)
            val level = battery.getIntProperty(BatteryManager.BATTERY_PROPERTY_CAPACITY)
            val sticky = context.registerReceiver(null, IntentFilter(Intent.ACTION_BATTERY_CHANGED))
            val status = sticky?.getIntExtra(BatteryManager.EXTRA_STATUS, -1) ?: -1
            val charging = status == BatteryManager.BATTERY_STATUS_CHARGING ||
                status == BatteryManager.BATTERY_STATUS_FULL
            val source = when (sticky?.getIntExtra(BatteryManager.EXTRA_PLUGGED, 0) ?: 0) {
                BatteryManager.BATTERY_PLUGGED_AC -> "charger"
                BatteryManager.BATTERY_PLUGGED_USB -> "USB"
                BatteryManager.BATTERY_PLUGGED_WIRELESS -> "wireless"
                else -> ""
            }
            val fullInMin = if (charging) battery.computeChargeTimeRemaining().takeIf { it > 0 }
                ?.let { it / 60_000 } else null

            val cm = context.getSystemService(ConnectivityManager::class.java)
            val caps = cm.getNetworkCapabilities(cm.activeNetwork)
            // With Tailscale up the active network is the VPN, which reports the
            // transport underneath it too.
            val network = when {
                caps == null -> "offline"
                caps.hasTransport(NetworkCapabilities.TRANSPORT_WIFI) -> "wifi"
                caps.hasTransport(NetworkCapabilities.TRANSPORT_CELLULAR) -> "mobile"
                caps.hasTransport(NetworkCapabilities.TRANSPORT_ETHERNET) -> "ethernet"
                else -> "other"
            }
            val ringer = when (context.getSystemService(AudioManager::class.java).ringerMode) {
                AudioManager.RINGER_MODE_SILENT -> "silent"
                AudioManager.RINGER_MODE_VIBRATE -> "vibrate"
                else -> "normal"
            }
            val saver = context.getSystemService(PowerManager::class.java).isPowerSaveMode

            val words = buildList {
                add(buildString {
                    append("Phone battery is at $level%")
                    if (charging) {
                        append(", charging")
                        if (source.isNotEmpty()) append(" ($source)")
                        if (fullInMin != null) append(", full in about $fullInMin min")
                    }
                    if (saver) append(", battery saver on")
                })
                add(when (network) {
                    "wifi" -> "On Wi-Fi"
                    "mobile" -> "On mobile data"
                    "offline" -> "No network"
                    else -> "On $network"
                })
                add("Ringer $ringer")
            }
            return JSONObject()
                .put("battery_percent", level)
                .put("charging", charging)
                .put("charge_source", source)
                .put("full_in_minutes", fullInMin ?: JSONObject.NULL)
                .put("battery_saver", saver)
                .put("network", network)
                .put("ringer", ringer)
                .put("message", words.joinToString(". ") + ".")
        }
    }
}

/** A notification on the phone: how the core reaches you when you're not at the laptop. */
class PhoneNotify(private val context: Context) : Capability {
    override val name = "phone.notify"
    override val description = "Show a notification on the user's phone"
    override val tier = 0
    override val paramsSchema: JSONObject = JSONObject("""
        {"type":"object","required":["body"],
         "properties":{"title":{"type":"string","maxLength":80},
                       "body":{"type":"string","maxLength":500}}}""")

    override suspend fun execute(params: JSONObject): CapabilityResult {
        if (context.checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) {
            return CapabilityResult.Failed(ErrorCode.PERMISSION_DENIED,
                "Notifications are turned off for NORA on the phone", userAction = "grant_permission")
        }
        Notifications.message(context, params.optString("title").ifEmpty { "NORA" }, params.getString("body"))
        return CapabilityResult.Ok(JSONObject().put("message", "Notification shown on the phone."))
    }
}
