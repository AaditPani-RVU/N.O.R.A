package com.aaditpani.nora.link

import java.net.URI
import java.net.URLEncoder

/**
 * The decisions behind Phase 4's capabilities that don't need Android: which
 * installed app "Spotify" means, which URLs may be opened, how directions are
 * addressed, how notifications are read back. Kept here so the JVM tests
 * cover them; `phone/` only does the Android calls.
 */

/** An installed app with a launcher entry. */
data class AppEntry(val label: String, val packageName: String)

object AppMatch {
    /**
     * The app a spoken name most likely means, or null. Exact label first,
     * then the label without "app"/"the", then a label that starts with the
     * name ("maps" → "Maps", not "Google Maps Go"), then whole-word
     * containment, then the package name. Ties go to the shorter label.
     */
    fun best(spoken: String, apps: List<AppEntry>): AppEntry? {
        val want = norm(spoken)
        if (want.isEmpty()) return null
        val scored = apps.mapNotNull { app ->
            val label = norm(app.label)
            val pkg = app.packageName.lowercase()
            val score = when {
                label == want -> 100
                label.replace(" ", "") == want.replace(" ", "") -> 95
                label.startsWith("$want ") -> 80
                " $label ".contains(" $want ") -> 70
                pkg == want || pkg.endsWith(".$want") || pkg.split('.').contains(want.replace(" ", "")) -> 60
                want.length >= 4 && label.startsWith(want) -> 50
                else -> return@mapNotNull null
            }
            Triple(score, app.label.length, app)
        }
        return scored.sortedWith(compareByDescending<Triple<Int, Int, AppEntry>> { it.first }
            .thenBy { it.second }).firstOrNull()?.third
    }

    private fun norm(s: String): String = s.lowercase()
        .replace(Regex("[^a-z0-9 ]"), " ")
        .replace(Regex("\\b(the|app|application)\\b"), " ")
        .replace(Regex("\\s+"), " ").trim()
}

object UrlPolicy {
    const val MAX_LENGTH = 2000

    /**
     * An http(s) URL safe to hand to the browser, or null. A bare domain gets
     * https://. No other scheme: `intent:`, `file:`, `content:` and
     * `javascript:` are how "open a link" turns into "run something".
     */
    fun normalise(raw: String): String? {
        val s = raw.trim()
        if (s.isEmpty() || s.length > MAX_LENGTH || s.any { it.isWhitespace() || it.isISOControl() }) return null
        val withScheme = when {
            s.startsWith("http://", ignoreCase = true) || s.startsWith("https://", ignoreCase = true) -> s
            s.contains("://") || s.contains(":") && !s.substringBefore(":").contains(".") -> return null
            else -> "https://$s"
        }
        val uri = try {
            URI(withScheme)
        } catch (e: Exception) {
            return null
        }
        val scheme = uri.scheme?.lowercase() ?: return null
        if (scheme != "http" && scheme != "https") return null
        val host = uri.host ?: return null
        if (!host.contains('.') && host != "localhost") return null
        if (uri.userInfo != null) return null   // "https://google.com@evil.example" reads as google
        return withScheme
    }
}

object Directions {
    val MODES = listOf("driving", "walking", "bicycling", "transit")

    /**
     * Google Maps' cross-platform directions URL. `dir_action=navigate`
     * starts turn-by-turn straight away; Maps handles it as an app link, and a
     * browser does too when Maps isn't installed.
     */
    fun url(destination: String, mode: String): String {
        val m = if (mode in MODES) mode else "driving"
        return "https://www.google.com/maps/dir/?api=1" +
            "&destination=" + URLEncoder.encode(destination.trim(), "UTF-8") +
            "&travelmode=" + m +
            "&dir_action=navigate"
    }
}

/** How `phone.play_media`'s `kind` maps onto Android's play-from-search focus. */
object MediaFocus {
    fun of(kind: String): String? = when (kind) {
        "playlist" -> "vnd.android.cursor.item/playlist"
        "artist" -> "vnd.android.cursor.item/artist"
        "album" -> "vnd.android.cursor.item/album"
        "track" -> "vnd.android.cursor.item/audio"
        else -> null
    }

    /** Package names for the apps `play_media` knows by name. */
    fun packageFor(app: String): String? = when (app.lowercase().trim()) {
        "spotify" -> "com.spotify.music"
        "youtube music", "yt music", "youtube_music" -> "com.google.android.apps.youtube.music"
        else -> null
    }
}

/**
 * The Spotify URIs the core may hand `play_media`: a playlist, album, artist
 * or track by its base-62 id. Anything else (a web link, `spotify:user:…`,
 * a URI with extra segments) is refused rather than passed to Spotify.
 */
object SpotifyUri {
    private val SHAPE = Regex("^spotify:(playlist|album|artist|track):[A-Za-z0-9]{10,40}$")

    fun isValid(uri: String): Boolean = SHAPE.matches(uri)

    /** The deep link that opens it *and* starts it, for when Spotify has no session yet. */
    fun autoplay(uri: String): String = "$uri:play"
}

object VolumeMath {
    /** 0–100 → a stream index in 0..max, rounding to nearest. */
    fun toIndex(percent: Int, max: Int): Int =
        ((percent.coerceIn(0, 100) * max + 50) / 100).coerceIn(0, max)

    fun toPercent(index: Int, max: Int): Int = if (max <= 0) 0 else (index * 100 + max / 2) / max
}

/** One notification as NORA may read it. */
data class Note(
    val key: String,
    val packageName: String,
    val app: String,
    val title: String,
    val text: String,
    val postedAt: Long,
)

object NoteDigest {
    const val MAX_TEXT = 300
    const val MAX_TITLE = 100

    /**
     * What `phone.read_notifications` returns: the newest first, within
     * [sinceMs], optionally one app, with repeats of the same text collapsed
     * (a chat app re-posts the whole thread on every message).
     */
    fun select(notes: List<Note>, now: Long, sinceMs: Long, app: String?, limit: Int): List<Note> {
        val want = app?.trim()?.lowercase()?.takeIf { it.isNotEmpty() }
        val seen = HashSet<String>()
        return notes.asSequence()
            .filter { now - it.postedAt <= sinceMs }
            .filter { want == null || it.app.lowercase().contains(want) || it.packageName.lowercase().contains(want) }
            .sortedByDescending { it.postedAt }
            .filter { seen.add(it.app + "\u0000" + it.title + "\u0000" + it.text) }
            .take(limit.coerceIn(1, 50))
            .toList()
    }

    /** The spoken fallback, and what the core's summariser reads. */
    fun message(notes: List<Note>, now: Long, app: String?): String {
        if (notes.isEmpty()) {
            return if (app.isNullOrBlank()) "No notifications on the phone right now."
            else "No notifications from $app."
        }
        val head = if (notes.size == 1) "1 notification." else "${notes.size} notifications."
        val lines = notes.joinToString(" · ") { n ->
            buildString {
                append(n.app)
                if (n.title.isNotBlank()) append(", ").append(clip(n.title, MAX_TITLE))
                if (n.text.isNotBlank()) append(": ").append(clip(n.text, MAX_TEXT))
                append(" (").append(ago(now - n.postedAt)).append(")")
            }
        }
        return "$head $lines"
    }

    fun ago(ms: Long): String {
        val min = ms / 60_000
        return when {
            min < 1 -> "just now"
            min < 60 -> "${min}m ago"
            min < 24 * 60 -> "${min / 60}h ago"
            else -> "${min / (24 * 60)}d ago"
        }
    }

    fun clip(s: String, max: Int): String {
        val flat = s.replace(Regex("\\s+"), " ").trim()
        return if (flat.length <= max) flat else flat.take(max - 1).trimEnd() + "…"
    }
}
