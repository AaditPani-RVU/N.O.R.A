package com.aaditpani.nora.phone

import android.content.ComponentName
import android.content.Context
import android.media.AudioManager
import android.media.MediaMetadata
import android.media.session.MediaController
import android.media.session.MediaSessionManager
import android.media.session.PlaybackState
import com.aaditpani.nora.link.Capability
import com.aaditpani.nora.link.CapabilityResult
import com.aaditpani.nora.link.ErrorCode
import com.aaditpani.nora.link.VolumeMath
import kotlinx.coroutines.delay
import org.json.JSONObject

/** Other apps' media sessions. Seeing them needs notification access (plan §6). */
object Media {
    fun hasAccess(context: Context): Boolean = NoraNotificationListener.hasAccess(context)

    fun sessions(context: Context): List<MediaController> = try {
        context.getSystemService(MediaSessionManager::class.java)
            .getActiveSessions(ComponentName(context, NoraNotificationListener::class.java))
    } catch (e: SecurityException) {
        emptyList()
    }

    fun controllerFor(context: Context, pkg: String): MediaController? =
        sessions(context).firstOrNull { it.packageName == pkg }

    /** The session a "pause" means: whatever is playing, else the most recent one. */
    fun current(context: Context): MediaController? {
        val all = sessions(context)
        return all.firstOrNull { it.playbackState?.state == PlaybackState.STATE_PLAYING } ?: all.firstOrNull()
    }

    fun title(c: MediaController): String? {
        val m = c.metadata ?: return null
        val title = m.getString(MediaMetadata.METADATA_KEY_TITLE)?.takeIf { it.isNotBlank() } ?: return null
        val artist = m.getString(MediaMetadata.METADATA_KEY_ARTIST)?.takeIf { it.isNotBlank() }
        return if (artist != null) "$title by $artist" else title
    }

    fun appLabel(context: Context, pkg: String): String = try {
        val pm = context.packageManager
        pm.getApplicationLabel(pm.getApplicationInfo(pkg, 0)).toString()
    } catch (e: Exception) {
        pkg
    }

    val noAccess = CapabilityResult.Failed(ErrorCode.PERMISSION_DENIED,
        "NORA needs notification access on the phone to see what's playing. " +
            "Switch it on from the NORA app.", userAction = "grant_notification_access")
}

/** Play, pause, skip in whatever app is playing. */
class MediaControl(private val context: Context) : Capability {
    override val name = "phone.media_control"
    override val description = "Whatever is playing on the phone; status says what it is"
    override val tier = 1
    override val paramsSchema: JSONObject = JSONObject("""
        {"type":"object","required":["action"],
         "properties":{"action":{"type":"string",
                                 "enum":["play","pause","toggle","next","previous","stop","status"]}}}""")

    override suspend fun execute(params: JSONObject): CapabilityResult {
        if (!Media.hasAccess(context)) return Media.noAccess
        val c = Media.current(context)
            ?: return CapabilityResult.Failed(ErrorCode.EXECUTION_FAILED, "Nothing is playing on the phone.")
        val app = Media.appLabel(context, c.packageName)
        val playing = c.playbackState?.state == PlaybackState.STATE_PLAYING
        val t = c.transportControls
        val action = params.getString("action").let { if (it == "toggle") (if (playing) "pause" else "play") else it }
        when (action) {
            "play" -> t.play()
            "pause" -> t.pause()
            "next" -> t.skipToNext()
            "previous" -> t.skipToPrevious()
            "stop" -> t.stop()
            "status" -> {
                val title = Media.title(c)
                val what = when {
                    title == null -> "$app is open but I can't see what's loaded"
                    playing -> "Playing $title on $app"
                    else -> "$title is paused on $app"
                }
                return CapabilityResult.Ok(JSONObject().put("message", "$what.")
                    .put("app", app).put("playing", playing).put("title", title ?: JSONObject.NULL))
            }
        }
        if (action == "next" || action == "previous") delay(700)   // let the new title land
        val now = Media.title(c)
        val message = when (action) {
            "play" -> "Resumed $app" + (now?.let { ": $it" } ?: "")
            "pause" -> "Paused $app"
            "stop" -> "Stopped $app"
            else -> (if (action == "next") "Skipped" else "Went back") + (now?.let { " to $it" } ?: " on $app")
        }
        return CapabilityResult.Ok(JSONObject().put("message", "$message.").put("app", app))
    }
}

/**
 * Media or ring volume. Android 17 ignores volume changes from apps in the
 * background (plan §8), so the level is read back afterwards: a change that
 * didn't take is reported as such, not as done.
 */
class Volume(private val context: Context) : Capability {
    override val name = "phone.volume"
    override val description = "The phone's volume, media unless stream=ring"
    override val tier = 1
    override val paramsSchema: JSONObject = JSONObject("""
        {"type":"object","required":["action"],
         "properties":{"action":{"type":"string","enum":["up","down","set","mute","unmute"]},
                       "level":{"type":"integer","minimum":0,"maximum":100},
                       "stream":{"type":"string","enum":["media","ring"]}}}""")

    override suspend fun execute(params: JSONObject): CapabilityResult {
        val am = context.getSystemService(AudioManager::class.java)
        val streamName = params.optString("stream", "media")
        val stream = if (streamName == "ring") AudioManager.STREAM_RING else AudioManager.STREAM_MUSIC
        val max = am.getStreamMaxVolume(stream)
        val before = am.getStreamVolume(stream)
        val mutedBefore = am.isStreamMute(stream)
        val action = params.getString("action")
        val step = (max / 10).coerceAtLeast(1)
        val target = when (action) {
            "up" -> (before + step).coerceAtMost(max)
            "down" -> (before - step).coerceAtLeast(0)
            "set" -> {
                if (!params.has("level")) return CapabilityResult.Failed(ErrorCode.INVALID_PARAMS, "Set it to what level?")
                VolumeMath.toIndex(params.getInt("level"), max)
            }
            else -> before
        }
        try {
            when (action) {
                "mute" -> am.adjustStreamVolume(stream, AudioManager.ADJUST_MUTE, 0)
                "unmute" -> am.adjustStreamVolume(stream, AudioManager.ADJUST_UNMUTE, 0)
                else -> am.setStreamVolume(stream, target, 0)
            }
        } catch (e: SecurityException) {
            // Ring volume while Do Not Disturb is on.
            return CapabilityResult.Failed(ErrorCode.PERMISSION_DENIED,
                "The phone won't change the $streamName volume right now (Do Not Disturb?).")
        }
        delay(150)
        val after = am.getStreamVolume(stream)
        val mutedAfter = am.isStreamMute(stream)
        val expectedChange = when (action) {
            "mute" -> !mutedBefore
            "unmute" -> mutedBefore
            else -> target != before
        }
        val changed = after != before || mutedAfter != mutedBefore
        if (expectedChange && !changed) {
            return CapabilityResult.Failed(ErrorCode.BACKGROUND_RESTRICTED,
                "The phone ignored the volume change. Android only lets NORA change it " +
                    "while the NORA app is on screen.", userAction = "open_app")
        }
        val pct = VolumeMath.toPercent(after, max)
        val words = when {
            mutedAfter -> "Phone $streamName volume is muted."
            else -> "Phone $streamName volume is at $pct%."
        }
        return CapabilityResult.Ok(JSONObject().put("message", words).put("percent", pct).put("muted", mutedAfter))
    }
}
