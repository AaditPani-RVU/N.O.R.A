package com.aaditpani.nora.phone

import android.app.Activity
import android.app.Application
import android.content.ActivityNotFoundException
import android.content.Context
import android.content.Intent
import android.os.Bundle
import com.aaditpani.nora.link.CapabilityResult
import com.aaditpani.nora.link.ErrorCode
import org.json.JSONObject
import java.util.concurrent.atomic.AtomicInteger

/**
 * Whether one of NORA's own screens is visible. Android only lets an app
 * start activities — open Spotify, show the dialler, start Maps — while it
 * has a visible window (plan §8, "Background activity launch"). The link's
 * foreground service doesn't count.
 */
object Foreground : Application.ActivityLifecycleCallbacks {
    private val started = AtomicInteger(0)

    val visible: Boolean get() = started.get() > 0

    override fun onActivityStarted(activity: Activity) {
        started.incrementAndGet()
    }

    override fun onActivityStopped(activity: Activity) {
        started.updateAndGet { (it - 1).coerceAtLeast(0) }
    }

    override fun onActivityCreated(activity: Activity, savedInstanceState: Bundle?) = Unit
    override fun onActivityResumed(activity: Activity) = Unit
    override fun onActivityPaused(activity: Activity) = Unit
    override fun onActivitySaveInstanceState(activity: Activity, outState: Bundle) = Unit
    override fun onActivityDestroyed(activity: Activity) = Unit
}

object Launcher {
    /**
     * Start [intent] if Android allows it right now. If NORA isn't on screen
     * it won't, and trying fails silently — so don't try: post a notification
     * that opens it on a tap, and say so. The core reports that as withheld,
     * not failed (`BACKGROUND_RESTRICTED`).
     */
    fun open(context: Context, intent: Intent, what: String, done: String,
             extra: JSONObject = JSONObject()): CapabilityResult {
        intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        if (Foreground.visible) {
            return try {
                context.startActivity(intent)
                CapabilityResult.Ok(extra.put("message", done).put("opened", true))
            } catch (e: ActivityNotFoundException) {
                CapabilityResult.Failed(ErrorCode.EXECUTION_FAILED, "Nothing on the phone can open $what.")
            } catch (e: SecurityException) {
                CapabilityResult.Failed(ErrorCode.PERMISSION_DENIED, "The phone wouldn't let NORA open $what.")
            }
        }
        Notifications.tapToOpen(context, "Tap to open $what", "NORA can't open it while the app isn't on screen.", intent)
        return CapabilityResult.Failed(ErrorCode.BACKGROUND_RESTRICTED,
            "Android won't let me open $what while NORA isn't on the phone's screen, " +
                "so there's a notification on it you can tap.",
            userAction = "tap_notification")
    }
}
