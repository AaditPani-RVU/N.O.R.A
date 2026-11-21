package com.aaditpani.nora.phone

import android.Manifest
import android.annotation.SuppressLint
import android.app.PendingIntent
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.util.Log
import com.aaditpani.nora.link.Capability
import com.aaditpani.nora.link.CapabilityResult
import com.aaditpani.nora.link.ErrorCode
import com.google.android.gms.common.api.ApiException
import com.google.android.gms.location.Geofence
import com.google.android.gms.location.GeofenceStatusCodes
import com.google.android.gms.location.GeofencingEvent
import com.google.android.gms.location.GeofencingRequest
import com.google.android.gms.location.LocationServices
import com.google.android.gms.tasks.Task
import kotlinx.coroutines.suspendCancellableCoroutine
import org.json.JSONObject
import kotlin.coroutines.resume

/**
 * The saved places ("home", "college") as geofences, watched by the phone
 * itself (plan §6, Phase 8). The core never streams the phone's position:
 * it hands over the fences, and the phone says when it crossed one, with a
 * `geofence.transition` event, even locked in a pocket.
 *
 * The core sends the whole list on every connect, so a reboot or reinstall
 * (which clears Android's fences) is mended the next time the link comes up,
 * and the phone keeps nothing of its own. No initial trigger: setting the
 * fences while at home must not read as arriving home.
 *
 * Not offered to the model (the core keeps it out of the tool list): which
 * places are fenced follows the saved places, not a turn's say-so.
 */
class SetGeofences(private val context: Context) : Capability {
    override val name = NAME
    override val description = "Replace the phone's geofences with these saved places"
    override val tier = 0
    override val paramsSchema: JSONObject = JSONObject("""
        {"type":"object","required":["fences"],
         "properties":{"fences":{"type":"array","maxItems":50,"items":{
           "type":"object","required":["id","lat","lon"],
           "properties":{"id":{"type":"string","maxLength":40},
                         "lat":{"type":"number","minimum":-90,"maximum":90},
                         "lon":{"type":"number","minimum":-180,"maximum":180},
                         "radius_m":{"type":"number","minimum":50,"maximum":5000}}}}}}""")

    @SuppressLint("MissingPermission")   // checked just below
    override suspend fun execute(params: JSONObject): CapabilityResult {
        if (!granted(Manifest.permission.ACCESS_FINE_LOCATION) ||
            !granted(Manifest.permission.ACCESS_BACKGROUND_LOCATION)) {
            return CapabilityResult.Failed(ErrorCode.PERMISSION_DENIED,
                "To notice you arriving and leaving, NORA needs location set to \"Allow all the time\" " +
                    "on the phone (Settings → Apps → NORA → Permissions → Location).",
                userAction = "grant_permission")
        }
        val list = params.getJSONArray("fences")
        val fences = (0 until list.length()).map { i ->
            val f = list.getJSONObject(i)
            Geofence.Builder()
                .setRequestId(f.getString("id"))
                .setCircularRegion(f.getDouble("lat"), f.getDouble("lon"), f.optDouble("radius_m", 150.0).toFloat())
                .setExpirationDuration(Geofence.NEVER_EXPIRE)
                .setTransitionTypes(Geofence.GEOFENCE_TRANSITION_ENTER or Geofence.GEOFENCE_TRANSITION_EXIT)
                // Within two minutes is soon enough to lock a laptop, and lets
                // the phone batch location work instead of waking for it.
                .setNotificationResponsiveness(RESPONSIVENESS_MS)
                .build()
        }
        val client = LocationServices.getGeofencingClient(context)
        await(client.removeGeofences(pendingIntent(context)))
        if (fences.isEmpty()) return CapabilityResult.Ok(JSONObject().put("fenced", 0))
        val request = GeofencingRequest.Builder().setInitialTrigger(0).addGeofences(fences).build()
        val error = await(client.addGeofences(request, pendingIntent(context)))
        if (error != null) {
            Log.w(TAG, "adding geofences failed: $error")
            return CapabilityResult.Failed(ErrorCode.EXECUTION_FAILED,
                if ((error as? ApiException)?.statusCode == GeofenceStatusCodes.GEOFENCE_NOT_AVAILABLE)
                    "The phone's location is off, or Google location accuracy is turned off, so it can't watch places."
                else "The phone couldn't set its geofences.", retryable = true)
        }
        Log.i(TAG, "watching ${fences.size} place(s)")
        return CapabilityResult.Ok(JSONObject().put("fenced", fences.size))
    }

    private fun granted(p: String) = context.checkSelfPermission(p) == PackageManager.PERMISSION_GRANTED

    /** Null on success, else what went wrong. */
    private suspend fun await(task: Task<Void>): Exception? = suspendCancellableCoroutine { cont ->
        task.addOnSuccessListener { if (cont.isActive) cont.resume(null) }
            .addOnFailureListener { if (cont.isActive) cont.resume(it) }
    }

    companion object {
        const val NAME = "phone.set_geofences"
        const val TAG = "NoraGeofence"
        private const val RESPONSIVENESS_MS = 120_000

        fun pendingIntent(context: Context): PendingIntent = PendingIntent.getBroadcast(
            context, 0, Intent(context, GeofenceReceiver::class.java),
            // Mutable: Play services fills in which fence and which way.
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_MUTABLE)
    }
}

/** A fence was crossed: tell the core, through the outbox, so it isn't lost offline. */
class GeofenceReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        val event = GeofencingEvent.fromIntent(intent) ?: return
        if (event.hasError()) {
            Log.w(SetGeofences.TAG, "geofence error ${event.errorCode}")
            return
        }
        val transition = when (event.geofenceTransition) {
            Geofence.GEOFENCE_TRANSITION_ENTER -> "enter"
            Geofence.GEOFENCE_TRANSITION_EXIT -> "exit"
            else -> return
        }
        val controller = context.controller
        if (!controller.paired.value) return
        // The app may have been asleep: bring the link up so the event goes
        // into its outbox now and to the core as soon as it can. Starting the
        // service from a geofence transition is allowed in the background.
        runCatching { LinkService.start(context) }
        controller.startLink()
        for (fence in event.triggeringGeofences.orEmpty()) {
            Log.i(SetGeofences.TAG, "${fence.requestId}: $transition")
            controller.event("geofence.transition", JSONObject()
                .put("place", fence.requestId)
                .put("transition", transition)
                .put("accuracy_m", event.triggeringLocation?.accuracy?.toInt() ?: -1))
        }
    }
}
