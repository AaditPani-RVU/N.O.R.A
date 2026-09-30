package com.aaditpani.nora.phone

import android.app.SearchManager
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.media.MediaMetadata
import android.media.session.PlaybackState
import android.net.Uri
import android.os.Bundle
import android.os.SystemClock
import android.util.Log
import android.provider.AlarmClock
import android.provider.MediaStore
import com.aaditpani.nora.link.AppEntry
import com.aaditpani.nora.link.AppMatch
import com.aaditpani.nora.link.Capability
import com.aaditpani.nora.link.CapabilityResult
import com.aaditpani.nora.link.Directions
import com.aaditpani.nora.link.ErrorCode
import com.aaditpani.nora.link.MediaFocus
import com.aaditpani.nora.link.PlayWatch
import com.aaditpani.nora.link.SpotifyUri
import com.aaditpani.nora.link.UrlPolicy
import kotlinx.coroutines.delay
import org.json.JSONArray
import org.json.JSONObject

/** Launch an installed app. Tier 1: acts, logged. */
class OpenApp(private val context: Context) : Capability {
    override val name = "phone.open_app"
    override val description = "Open an app on the phone by its name"
    override val tier = 1
    override val paramsSchema: JSONObject = JSONObject("""
        {"type":"object","required":["app"],
         "properties":{"app":{"type":"string","maxLength":100}}}""")

    override suspend fun execute(params: JSONObject): CapabilityResult {
        val pm = context.packageManager
        val apps = launchable(context)
        val app = AppMatch.best(params.getString("app"), apps)
            ?: return CapabilityResult.Failed(ErrorCode.EXECUTION_FAILED,
                "There's no app called \"${params.getString("app")}\" on the phone.")
        val intent = pm.getLaunchIntentForPackage(app.packageName)
            ?: return CapabilityResult.Failed(ErrorCode.EXECUTION_FAILED, "${app.label} can't be opened directly.")
        return Launcher.open(context, intent, app.label, "Opened ${app.label} on the phone.",
            JSONObject().put("package", app.packageName))
    }

    companion object {
        fun launchable(context: Context): List<AppEntry> {
            val pm = context.packageManager
            return pm.queryIntentActivities(
                Intent(Intent.ACTION_MAIN).addCategory(Intent.CATEGORY_LAUNCHER), 0)
                .map { AppEntry(it.loadLabel(pm).toString(), it.activityInfo.packageName) }
                .filter { it.packageName != context.packageName }
                .distinctBy { it.packageName }
        }
    }
}

/** A web page in the browser. http(s) only. */
class OpenUrl(private val context: Context) : Capability {
    override val name = "phone.open_url"
    override val description = "Open an http(s) link on the phone"
    override val tier = 1
    override val paramsSchema: JSONObject = JSONObject("""
        {"type":"object","required":["url"],
         "properties":{"url":{"type":"string","maxLength":${UrlPolicy.MAX_LENGTH}}}}""")

    override suspend fun execute(params: JSONObject): CapabilityResult {
        val url = UrlPolicy.normalise(params.getString("url"))
            ?: return CapabilityResult.Failed(ErrorCode.INVALID_PARAMS,
                "Only ordinary web links (http or https) can be opened on the phone.")
        val intent = Intent(Intent.ACTION_VIEW, Uri.parse(url)).addCategory(Intent.CATEGORY_BROWSABLE)
        val host = Uri.parse(url).host.orEmpty()
        return Launcher.open(context, intent, host, "Opened $host on the phone.")
    }
}

/** The dialler with a number filled in. The user presses call. */
class Dial(private val context: Context) : Capability {
    override val name = "phone.dial"
    override val description = "The phone's dialler with a number filled in; the user presses call"
    override val tier = 1
    override val paramsSchema: JSONObject = JSONObject("""
        {"type":"object","required":["number"],
         "properties":{"number":{"type":"string","maxLength":32}}}""")

    override suspend fun execute(params: JSONObject): CapabilityResult {
        val number = params.getString("number").trim()
        if (number.isEmpty() || !number.all { it.isDigit() || it in "+*#-() " }) {
            return CapabilityResult.Failed(ErrorCode.INVALID_PARAMS, "That isn't a phone number.")
        }
        val intent = Intent(Intent.ACTION_DIAL, Uri.parse("tel:" + Uri.encode(number)))
        return Launcher.open(context, intent, "the dialler",
            "The dialler is open with $number. Press call when you're ready.")
    }
}

/** An alarm in the phone's clock app. */
class SetAlarm(private val context: Context) : Capability {
    override val name = "phone.set_alarm"
    override val description = "An alarm in the phone's clock, 24-hour time"
    override val tier = 1
    override val paramsSchema: JSONObject = JSONObject("""
        {"type":"object","required":["hour"],
         "properties":{"hour":{"type":"integer","minimum":0,"maximum":23},
                       "minute":{"type":"integer","minimum":0,"maximum":59},
                       "label":{"type":"string","maxLength":80}}}""")

    override suspend fun execute(params: JSONObject): CapabilityResult {
        val hour = params.getInt("hour")
        val minute = params.optInt("minute", 0)
        val intent = Intent(AlarmClock.ACTION_SET_ALARM)
            .putExtra(AlarmClock.EXTRA_HOUR, hour)
            .putExtra(AlarmClock.EXTRA_MINUTES, minute)
            .putExtra(AlarmClock.EXTRA_SKIP_UI, true)
        params.optString("label").takeIf { it.isNotBlank() }?.let { intent.putExtra(AlarmClock.EXTRA_MESSAGE, it) }
        val at = "%d:%02d".format(hour, minute)
        return Launcher.open(context, intent, "the clock", "Alarm set for $at on the phone.")
    }
}

/** A countdown in the phone's clock app. */
class SetTimer(private val context: Context) : Capability {
    override val name = "phone.set_timer"
    override val description = "A countdown timer on the phone"
    override val tier = 1
    override val paramsSchema: JSONObject = JSONObject("""
        {"type":"object","required":["seconds"],
         "properties":{"seconds":{"type":"integer","minimum":1,"maximum":86400},
                       "label":{"type":"string","maxLength":80}}}""")

    override suspend fun execute(params: JSONObject): CapabilityResult {
        val seconds = params.getInt("seconds")
        val intent = Intent(AlarmClock.ACTION_SET_TIMER)
            .putExtra(AlarmClock.EXTRA_LENGTH, seconds)
            .putExtra(AlarmClock.EXTRA_SKIP_UI, true)
        params.optString("label").takeIf { it.isNotBlank() }?.let { intent.putExtra(AlarmClock.EXTRA_MESSAGE, it) }
        val words = if (seconds % 60 == 0) "${seconds / 60} minute${if (seconds == 60) "" else "s"}"
        else "$seconds seconds"
        return Launcher.open(context, intent, "the clock", "Timer for $words started on the phone.")
    }
}

/** Turn-by-turn directions in Maps. The core decides where; the phone opens it. */
class Navigate(private val context: Context) : Capability {
    override val name = "phone.navigate"
    override val description = "Maps directions on the phone. Prefer navigate_to, which knows saved places"
    override val tier = 1
    override val paramsSchema: JSONObject = JSONObject("""
        {"type":"object","required":["destination"],
         "properties":{"destination":{"type":"string","maxLength":200},
                       "mode":{"type":"string","enum":["driving","walking","bicycling","transit"]},
                       "label":{"type":"string","maxLength":60}}}""")

    override suspend fun execute(params: JSONObject): CapabilityResult {
        val dest = params.getString("destination").trim()
        if (dest.isEmpty()) return CapabilityResult.Failed(ErrorCode.INVALID_PARAMS, "Where to?")
        val label = params.optString("label").ifBlank { dest }
        val intent = Intent(Intent.ACTION_VIEW, Uri.parse(Directions.url(dest, params.optString("mode", "driving"))))
        if (installed(context, MAPS)) intent.setPackage(MAPS)
        return Launcher.open(context, intent, "Maps", "Directions to $label are up in Maps.")
    }

    private companion object {
        const val MAPS = "com.google.android.apps.maps"
    }
}

/**
 * Play something in Spotify (or YouTube Music).
 *
 * With a `uri` — the core resolves "my workout playlist" to the user's own
 * playlist through their Spotify login — it goes to Spotify's media session
 * as play-from-URI, which works with NORA in the background. Without a
 * session yet, the `…:play` deep link opens and starts it.
 *
 * Without one it falls back to play-from-search. Spotify's current build
 * treats that as "open the search page" and starts nothing, so success is
 * only claimed once the session reports something new playing.
 */
class PlayMedia(private val context: Context) : Capability {
    override val name = "phone.play_media"
    override val description = "Play a Spotify link on the phone (NORA finds it with play_on_phone)"
    override val tier = 1
    override val paramsSchema: JSONObject = JSONObject("""
        {"type":"object","required":["query"],
         "properties":{"query":{"type":"string","maxLength":100},
                       "kind":{"type":"string","enum":["any","track","artist","album","playlist"]},
                       "app":{"type":"string","enum":["spotify","youtube music"]},
                       "uri":{"type":"string","maxLength":60},
                       "shuffle":{"type":"boolean"},
                       "label":{"type":"string","maxLength":100}}}""")

    override suspend fun execute(params: JSONObject): CapabilityResult {
        val query = params.getString("query").trim()
        val kind = params.optString("kind", "any")
        val appName = params.optString("app", "spotify")
        val pkg = MediaFocus.packageFor(appName)!!
        val label = if (pkg == SPOTIFY) "Spotify" else "YouTube Music"
        if (!installed(context, pkg)) {
            return CapabilityResult.Failed(ErrorCode.EXECUTION_FAILED, "$label isn't installed on the phone.")
        }
        val what = params.optString("label").ifBlank {
            if (kind == "playlist") "your $query playlist" else query
        }
        val uri = params.optString("uri")
        if (uri.isNotEmpty() && pkg == SPOTIFY) {
            if (!SpotifyUri.isValid(uri)) {
                return CapabilityResult.Failed(ErrorCode.INVALID_PARAMS, "That isn't a Spotify link I can play.")
            }
            return playUri(uri, what, params.optBoolean("shuffle", false))
        }

        val extras = searchExtras(query, kind)
        val controller = Media.controllerFor(context, pkg)
        if (controller != null &&
            (controller.playbackState?.actions ?: 0L) and PlaybackState.ACTION_PLAY_FROM_SEARCH != 0L) {
            val (title, trace) = watch(controller, null) {
                controller.transportControls.playFromSearch(query, extras)
            }
            if (title == null) {
                return CapabilityResult.Failed(ErrorCode.EXECUTION_FAILED,
                    "$label didn't start playing $what. ${trace.optString("summary")}".trim())
            }
            return CapabilityResult.Ok(JSONObject()
                .put("message", "Playing $title on $label.").put("title", title).put("trace", trace))
        }

        val intent = Intent(MediaStore.INTENT_ACTION_MEDIA_PLAY_FROM_SEARCH).setPackage(pkg).putExtras(extras)
        if (intent.resolveActivity(context.packageManager) == null) {
            // Older app builds: at least land on the search.
            intent.action = Intent.ACTION_VIEW
            intent.data = Uri.parse("spotify:search:" + Uri.encode(query))
            intent.replaceExtras(null as Bundle?)
        }
        return Launcher.open(context, intent, label, "Opened $label's search for $what.")
    }

    private suspend fun playUri(uri: String, what: String, shuffle: Boolean): CapabilityResult {
        val controller = Media.controllerFor(context, SPOTIFY)
        if (controller == null ||
            (controller.playbackState?.actions ?: 0L) and PlaybackState.ACTION_PLAY_FROM_URI == 0L) {
            // Spotify isn't running: the deep link starts it and plays. Shuffle
            // can't ride along; say so rather than pretend.
            val intent = Intent(Intent.ACTION_VIEW, Uri.parse(SpotifyUri.autoplay(uri))).setPackage(SPOTIFY)
            val done = "Playing $what on Spotify." + if (shuffle) " Turn shuffle on in Spotify if you want it." else ""
            return Launcher.open(context, intent, "Spotify", done)
        }
        if (shuffle) setShuffle(controller)
        val send = { controller.transportControls.playFromUri(Uri.parse(uri), Bundle()) }
        val expect = if (uri.startsWith("spotify:track:")) uri else null
        val (title, trace) = watch(controller, expect, send)
        if (title == null) {
            return CapabilityResult.Failed(ErrorCode.EXECUTION_FAILED,
                "Spotify didn't switch to $what. ${trace.optString("summary")}".trim())
        }
        if (shuffle) setShuffle(controller)    // some builds reset it when the context changes
        val how = if (shuffle) "Shuffling" else "Playing"
        return CapabilityResult.Ok(JSONObject()
            .put("message", "$how $what on Spotify, starting with $title.")
            .put("title", title).put("trace", trace))
    }

    /**
     * Send the request, then poll the session until it plays what was asked
     * ([PlayWatch]). Returns (the new title or null, a trace for the core's
     * log). Every poll is also logged under NORA.play for `adb logcat`.
     */
    private suspend fun watch(
        controller: android.media.session.MediaController, expectId: String?, send: () -> Unit,
    ): Pair<String?, JSONObject> {
        fun id() = controller.metadata?.getString(MediaMetadata.METADATA_KEY_MEDIA_ID)
        fun playing() = controller.playbackState?.state == PlaybackState.STATE_PLAYING
        val beforeId = id()
        val beforeTitle = Media.title(controller)
        val w = PlayWatch(beforeId, beforeTitle, expectId)
        val steps = JSONArray()
        val start = SystemClock.elapsedRealtime()
        fun note(what: String) {
            val ms = SystemClock.elapsedRealtime() - start
            val line = "${ms}ms $what id=${id()} title=${Media.title(controller)} playing=${playing()}"
            Log.i(TAG, line)
            if (steps.length() < 12) steps.put(line)
        }
        note("before (expect=$expectId)")
        send()
        while (true) {
            delay(300)
            val ms = SystemClock.elapsedRealtime() - start
            when (val next = w.step(ms, id(), Media.title(controller), playing())) {
                is PlayWatch.Next.Wait -> Unit
                is PlayWatch.Next.PressPlay -> { note("press play"); controller.transportControls.play() }
                is PlayWatch.Next.Resend -> { note("resend"); send() }
                is PlayWatch.Next.Done -> {
                    note("done")
                    return next.title to JSONObject().put("steps", steps)
                }
                is PlayWatch.Next.GiveUp -> {
                    note("give up")
                    val summary = if (next.switched) "It loaded it but wouldn't start playing."
                                  else "It stayed on ${beforeTitle ?: "what it had"}."
                    return null to JSONObject().put("steps", steps).put("summary", summary)
                }
            }
        }
    }

    /**
     * Shuffle on, not toggled. The framework controller has no setShuffleMode;
     * this is the custom action MediaControllerCompat sends for it, which
     * compat and media3 sessions (Spotify's) answer.
     */
    private fun setShuffle(controller: android.media.session.MediaController) {
        controller.transportControls.sendCustomAction(
            "android.support.v4.media.session.action.SET_SHUFFLE_MODE",
            Bundle().apply { putInt("android.support.v4.media.session.action.ARGUMENT_SHUFFLE_MODE", 1) })
    }


    private fun searchExtras(query: String, kind: String): Bundle = Bundle().apply {
        putString(SearchManager.QUERY, query)
        MediaFocus.of(kind)?.let { putString(MediaStore.EXTRA_MEDIA_FOCUS, it) }
        when (kind) {
            "playlist" -> putString("android.intent.extra.playlist", query)
            "artist" -> putString(MediaStore.EXTRA_MEDIA_ARTIST, query)
            "album" -> putString(MediaStore.EXTRA_MEDIA_ALBUM, query)
            "track" -> putString(MediaStore.EXTRA_MEDIA_TITLE, query)
        }
    }

    private companion object {
        const val SPOTIFY = "com.spotify.music"
        const val TAG = "NORA.play"
    }
}

fun installed(context: Context, pkg: String): Boolean = try {
    context.packageManager.getPackageInfo(pkg, 0)
    true
} catch (e: PackageManager.NameNotFoundException) {
    false
}
