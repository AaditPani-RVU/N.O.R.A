package com.aaditpani.nora.phone

import android.Manifest
import android.content.Context
import android.content.pm.PackageManager
import android.location.Geocoder
import android.location.Location
import android.location.LocationManager
import android.os.CancellationSignal
import com.aaditpani.nora.link.Capability
import com.aaditpani.nora.link.CapabilityResult
import com.aaditpani.nora.link.ErrorCode
import kotlinx.coroutines.suspendCancellableCoroutine
import kotlinx.coroutines.withTimeoutOrNull
import org.json.JSONObject
import kotlin.coroutines.resume

/**
 * One position fix, when the user asks ("where am I", "I'm going home").
 * Never streamed, never on a timer (plan §7.9). Live user only: a job or an
 * automation can't ask for it — geofences, when they come in Phase 8, are
 * evaluated on the phone.
 *
 * Only while NORA is on screen, for now: Android gives an app in the
 * background its location only with "Allow all the time", which this app
 * doesn't ask for until geofences need it.
 */
class GetLocation(private val context: Context) : Capability {
    override val name = "phone.get_location"
    override val description = "Where the phone is now: coordinates and nearest address"
    override val tier = 0
    override val requiresLiveUser = true
    override val paramsSchema: JSONObject = JSONObject("""{"type":"object","properties":{}}""")

    override suspend fun execute(params: JSONObject): CapabilityResult {
        val fine = granted(Manifest.permission.ACCESS_FINE_LOCATION)
        if (!fine && !granted(Manifest.permission.ACCESS_COARSE_LOCATION)) {
            return CapabilityResult.Failed(ErrorCode.PERMISSION_DENIED,
                "NORA doesn't have location permission on the phone. Allow it from the NORA app.",
                userAction = "grant_permission")
        }
        val lm = context.getSystemService(LocationManager::class.java)
        if (!lm.isLocationEnabled) {
            return CapabilityResult.Failed(ErrorCode.EXECUTION_FAILED, "Location is switched off on the phone.")
        }
        val background = !Foreground.visible && !granted(Manifest.permission.ACCESS_BACKGROUND_LOCATION)
        val fix = try {
            current(lm) ?: lastKnown(lm)
        } catch (e: SecurityException) {
            null
        }
        if (fix == null) {
            return if (background) CapabilityResult.Failed(ErrorCode.BACKGROUND_RESTRICTED,
                "The phone only shares its location with NORA while the NORA app is on screen.",
                userAction = "open_app")
            else CapabilityResult.Failed(ErrorCode.EXECUTION_FAILED, "The phone couldn't get a location fix.",
                retryable = true)
        }
        val address = withTimeoutOrNull(2000) { reverse(fix) }
        val accuracy = fix.accuracy.toInt()
        val ageSec = (System.currentTimeMillis() - fix.time) / 1000
        val where = address ?: "%.5f, %.5f".format(fix.latitude, fix.longitude)
        return CapabilityResult.Ok(JSONObject()
            .put("lat", fix.latitude).put("lon", fix.longitude)
            .put("accuracy_m", accuracy).put("age_s", ageSec)
            .put("address", address ?: JSONObject.NULL)
            .put("message", "You're near $where (within $accuracy m)."))
    }

    private fun granted(p: String) = context.checkSelfPermission(p) == PackageManager.PERMISSION_GRANTED

    private suspend fun current(lm: LocationManager): Location? {
        val provider = when {
            lm.isProviderEnabled(LocationManager.FUSED_PROVIDER) -> LocationManager.FUSED_PROVIDER
            lm.isProviderEnabled(LocationManager.GPS_PROVIDER) -> LocationManager.GPS_PROVIDER
            else -> LocationManager.NETWORK_PROVIDER
        }
        return withTimeoutOrNull(4000) {
            suspendCancellableCoroutine { cont ->
                val cancel = CancellationSignal()
                cont.invokeOnCancellation { cancel.cancel() }
                @Suppress("MissingPermission")
                lm.getCurrentLocation(provider, cancel, context.mainExecutor) { cont.resume(it) }
            }
        }
    }

    /** A fix from the last two minutes is as good as a new one for "where am I". */
    @Suppress("MissingPermission")
    private fun lastKnown(lm: LocationManager): Location? =
        listOf(LocationManager.FUSED_PROVIDER, LocationManager.GPS_PROVIDER, LocationManager.NETWORK_PROVIDER)
            .mapNotNull { runCatching { lm.getLastKnownLocation(it) }.getOrNull() }
            .filter { System.currentTimeMillis() - it.time < 2 * 60_000 }
            .minByOrNull { it.accuracy }

    private suspend fun reverse(fix: Location): String? {
        if (!Geocoder.isPresent()) return null
        return suspendCancellableCoroutine { cont ->
            Geocoder(context).getFromLocation(fix.latitude, fix.longitude, 1,
                object : Geocoder.GeocodeListener {
                    override fun onGeocode(addresses: MutableList<android.location.Address>) {
                        val a = addresses.firstOrNull()
                        val line = a?.let {
                            listOfNotNull(it.featureName?.takeIf { f -> !f.matches(Regex("[0-9A-Z+]+")) },
                                it.thoroughfare, it.subLocality, it.locality)
                                .distinct().joinToString(", ").ifBlank { it.getAddressLine(0) }
                        }
                        if (cont.isActive) cont.resume(line)
                    }

                    override fun onError(errorMessage: String?) {
                        if (cont.isActive) cont.resume(null)
                    }
                })
        }
    }
}
