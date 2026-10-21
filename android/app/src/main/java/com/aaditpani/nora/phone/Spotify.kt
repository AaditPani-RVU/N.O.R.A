package com.aaditpani.nora.phone

import android.content.Context
import android.content.Intent
import android.media.session.PlaybackState
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.util.Base64
import com.aaditpani.nora.BuildConfig
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.withContext
import okhttp3.FormBody
import okhttp3.HttpUrl.Companion.toHttpUrl
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONArray
import org.json.JSONObject
import java.security.MessageDigest
import java.security.SecureRandom
import java.util.concurrent.TimeUnit

/**
 * The user's Spotify library, for NORA Player's LIBRARY screen.
 *
 * Sign-in is Authorization Code with PKCE, like the core's
 * `nora.spotify_user`, but with its own redirect so the browser hands the
 * code straight back to the player: nora-player://spotify-callback must be
 * on the app in the Spotify dashboard. Only the Client ID is on the phone —
 * PKCE needs no secret — and the tokens stay in this app's private storage.
 *
 * Reading is the Web API. Playing goes through Spotify Connect to the active
 * device, else this phone, so a track tapped inside a playlist plays the
 * playlist from that track. When Spotify isn't running here, it is opened on
 * the item and Connect is tried again once it has come up.
 */
object Spotify {
    const val REDIRECT = "nora-player://spotify-callback"
    private const val AUTHORIZE = "https://accounts.spotify.com/authorize"
    private const val TOKEN = "https://accounts.spotify.com/api/token"
    private const val API = "https://api.spotify.com/v1"
    private const val SCOPES = "playlist-read-private playlist-read-collaborative user-library-read " +
        "user-read-playback-state user-modify-playback-state"
    private const val PREFS = "spotify"
    private const val PKG = "com.spotify.music"

    private val http = OkHttpClient.Builder().callTimeout(12, TimeUnit.SECONDS).build()
    private val refreshing = Mutex()

    /** A row in the library: a track, album, playlist or artist. */
    data class Item(
        val uri: String,
        val kind: String,
        val title: String,
        val sub: String,
        val image: String?,
        val bigImage: String?,
        val count: Int = 0,
        val durationMs: Int = 0,
    ) {
        val id get() = uri.substringAfterLast(':')
    }

    /** Something the screen can say: no login, Spotify refused, the network is down. */
    class Problem(message: String, val code: Int = 0) : Exception(message)

    val configured get() = BuildConfig.SPOTIFY_CLIENT_ID.isNotBlank()

    private fun prefs(ctx: Context) = ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE)

    fun linked(ctx: Context) = prefs(ctx).getString("refresh", null) != null

    fun logout(ctx: Context) = prefs(ctx).edit().clear().apply()

    // ── sign-in ─────────────────────────────────────────────────────────────

    private fun random(bytes: Int): String = ByteArray(bytes).also(SecureRandom()::nextBytes)
        .let { Base64.encodeToString(it, Base64.URL_SAFE or Base64.NO_PADDING or Base64.NO_WRAP) }

    /** The browser page that asks the user to approve. Remembers the verifier for [finishLogin]. */
    fun loginIntent(ctx: Context): Intent {
        val verifier = random(64)
        val state = random(16)
        prefs(ctx).edit().putString("verifier", verifier).putString("state", state).apply()
        val challenge = MessageDigest.getInstance("SHA-256").digest(verifier.toByteArray())
            .let { Base64.encodeToString(it, Base64.URL_SAFE or Base64.NO_PADDING or Base64.NO_WRAP) }
        val url = Uri.parse(AUTHORIZE).buildUpon()
            .appendQueryParameter("client_id", BuildConfig.SPOTIFY_CLIENT_ID)
            .appendQueryParameter("response_type", "code")
            .appendQueryParameter("redirect_uri", REDIRECT)
            .appendQueryParameter("code_challenge_method", "S256")
            .appendQueryParameter("code_challenge", challenge)
            .appendQueryParameter("scope", SCOPES)
            .appendQueryParameter("state", state)
            .build()
        return Intent(Intent.ACTION_VIEW, url)
    }

    fun isCallback(uri: Uri?) = uri != null && uri.toString().startsWith(REDIRECT)

    /** The browser came back to [REDIRECT]: trade the code for tokens. */
    suspend fun finishLogin(ctx: Context, uri: Uri) {
        val p = prefs(ctx)
        uri.getQueryParameter("error")?.let { throw Problem(if (it == "access_denied") "SIGN-IN CANCELLED" else "SPOTIFY SAID: $it") }
        val code = uri.getQueryParameter("code") ?: throw Problem("NO CODE IN THE REPLY")
        val verifier = p.getString("verifier", null)
        if (verifier == null || uri.getQueryParameter("state") != p.getString("state", null)) {
            throw Problem("SIGN-IN EXPIRED — TRY AGAIN")
        }
        val tok = tokenRequest(FormBody.Builder()
            .add("grant_type", "authorization_code")
            .add("code", code)
            .add("redirect_uri", REDIRECT)
            .add("code_verifier", verifier))
        p.edit().remove("verifier").remove("state").apply()
        save(ctx, tok)
    }

    private suspend fun tokenRequest(form: FormBody.Builder): JSONObject = withContext(Dispatchers.IO) {
        val req = Request.Builder().url(TOKEN)
            .post(form.add("client_id", BuildConfig.SPOTIFY_CLIENT_ID).build()).build()
        val (code, text) = try {
            http.newCall(req).execute().use { it.code to it.body?.string().orEmpty() }
        } catch (e: Exception) {
            throw Problem("OFFLINE — CAN'T REACH SPOTIFY")
        }
        if (code != 200) {
            val why = runCatching { JSONObject(text).optString("error_description") }.getOrNull().orEmpty()
            throw Problem(when {
                why.contains("redirect", true) -> "ADD $REDIRECT TO THE APP'S REDIRECT URIS"
                why.contains("revoked", true) || why.contains("invalid", true) -> "SIGN IN AGAIN"
                else -> "SPOTIFY REFUSED SIGN-IN ($code)"
            })
        }
        JSONObject(text)
    }

    private fun save(ctx: Context, tok: JSONObject) {
        val e = prefs(ctx).edit()
            .putString("access", tok.getString("access_token"))
            .putLong("expires", System.currentTimeMillis() + tok.optLong("expires_in", 3600) * 1000 - 60_000)
        // Spotify may rotate the refresh token; keep whichever is newest.
        tok.optString("refresh_token").takeIf { it.isNotEmpty() }?.let { e.putString("refresh", it) }
        e.apply()
    }

    private suspend fun token(ctx: Context, force: Boolean = false): String = refreshing.withLock {
        val p = prefs(ctx)
        val refresh = p.getString("refresh", null) ?: throw Problem("NOT SIGNED IN")
        val access = p.getString("access", null)
        if (!force && access != null && System.currentTimeMillis() < p.getLong("expires", 0)) return access
        try {
            save(ctx, tokenRequest(FormBody.Builder().add("grant_type", "refresh_token").add("refresh_token", refresh)))
        } catch (e: Problem) {
            if (e.message == "SIGN IN AGAIN") logout(ctx)
            throw e
        }
        p.getString("access", null)!!
    }

    // ── web api ─────────────────────────────────────────────────────────────

    /** One authorised call; a 401 gets one retry on a fresh token. Returns (status, body). */
    private suspend fun call(ctx: Context, method: String, url: String, body: JSONObject? = null): Pair<Int, String> {
        suspend fun once(force: Boolean): Pair<Int, String> {
            val auth = token(ctx, force)
            return withContext(Dispatchers.IO) {
                val rb = body?.toString()?.toRequestBody("application/json".toMediaType())
                    ?: if (method == "GET") null else ByteArray(0).toRequestBody()
                val req = Request.Builder().url(url).header("Authorization", "Bearer $auth").method(method, rb).build()
                try {
                    http.newCall(req).execute().use { it.code to it.body?.string().orEmpty() }
                } catch (e: Exception) {
                    throw Problem("OFFLINE — CAN'T REACH SPOTIFY")
                }
            }
        }
        val r = once(false)
        return if (r.first == 401) once(true) else r
    }

    private suspend fun get(ctx: Context, url: String): JSONObject {
        val (code, text) = call(ctx, "GET", url)
        if (code == 200) return JSONObject(text)
        throw Problem(when (code) {
            403 -> "SPOTIFY REFUSED (403)"
            429 -> "RATE-LIMITED — WAIT A MINUTE"
            else -> "SPOTIFY ERROR $code"
        }, code)
    }

    private fun url(path: String, vararg q: Pair<String, Any>): String =
        "$API$path".toHttpUrl().newBuilder().apply { q.forEach { (k, v) -> addQueryParameter(k, v.toString()) } }.build().toString()

    /** Follows `next` until [max] rows; Spotify pages are 50 at most. */
    private suspend fun pages(ctx: Context, first: String, max: Int, row: (JSONObject) -> Item?): List<Item> {
        val out = mutableListOf<Item>()
        var next: String? = first
        while (next != null && out.size < max) {
            val page = get(ctx, next)
            val items = page.optJSONArray("items") ?: break
            for (i in 0 until items.length()) items.optJSONObject(i)?.let(row)?.let(out::add)
            next = page.optString("next").takeIf { it.isNotEmpty() && it != "null" }
        }
        return out.take(max)
    }

    suspend fun playlists(ctx: Context) = pages(ctx, url("/me/playlists", "limit" to 50), 300, ::playlist)

    suspend fun liked(ctx: Context) = pages(ctx, url("/me/tracks", "limit" to 50), 200) { track(it.optJSONObject("track")) }

    suspend fun albums(ctx: Context) = pages(ctx, url("/me/albums", "limit" to 50), 200) { album(it.optJSONObject("album")) }

    /**
     * A playlist's tracks. Since 2026 the row is under "item"; older replies say "track".
     * A development-mode app only gets the tracks of playlists the user owns
     * or collaborates on — anyone else's is a 403, though it still plays.
     */
    suspend fun playlistTracks(ctx: Context, id: String) = try {
        pages(ctx, url("/playlists/$id/items", "limit" to 100), 500) { track(it.optJSONObject("item") ?: it.optJSONObject("track")) }
    } catch (e: Problem) {
        throw if (e.code == 403) Problem(HIDDEN, 403) else e
    }

    const val HIDDEN = "SPOTIFY ONLY LISTS THE TRACKS OF YOUR OWN PLAYLISTS. PLAY AND SHUFFLE STILL WORK."


    /** An album's tracks carry no art of their own, so they borrow the album's. */
    suspend fun albumTracks(ctx: Context, album: Item) =
        pages(ctx, url("/albums/${album.id}/tracks", "limit" to 50), 200) { track(it, album) }

    data class Results(val tracks: List<Item>, val artists: List<Item>, val albums: List<Item>, val playlists: List<Item>)

    suspend fun search(ctx: Context, q: String): Results {
        val r = get(ctx, url("/search", "q" to q, "type" to "track,artist,album,playlist", "limit" to 8))
        fun <T> list(key: String, f: (JSONObject?) -> T?) =
            r.optJSONObject(key)?.optJSONArray("items")?.let { a -> (0 until a.length()).mapNotNull { f(a.optJSONObject(it)) } }.orEmpty()
        return Results(list("tracks") { track(it) }, list("artists", ::artist), list("albums", ::album), list("playlists", ::playlist))
    }

    // ── parsing ─────────────────────────────────────────────────────────────

    /** Smallest picture that still looks sharp in a 40 dp row, and the largest for a header. */
    private fun images(a: JSONArray?): Pair<String?, String?> {
        if (a == null || a.length() == 0) return null to null
        val all = (0 until a.length()).mapNotNull { a.optJSONObject(it) }
            .map { it.optString("url") to it.optInt("width", 0) }.filter { it.first.isNotEmpty() }
        if (all.isEmpty()) return null to null
        val bySize = all.sortedBy { it.second }
        val small = bySize.firstOrNull { it.second >= 120 } ?: bySize.last()
        val big = bySize.lastOrNull { it.second in 1..640 } ?: bySize.last()
        return small.first to big.first
    }

    private fun names(a: JSONArray?) =
        a?.let { (0 until it.length()).mapNotNull { i -> it.optJSONObject(i)?.optString("name") } }.orEmpty().joinToString(", ")

    private fun track(o: JSONObject?, album: Item? = null): Item? {
        if (o == null || o.optBoolean("is_local")) return null
        val uri = o.optString("uri").takeIf { it.startsWith("spotify:") } ?: return null
        val (small, big) = album?.let { it.image to it.bigImage } ?: images(o.optJSONObject("album")?.optJSONArray("images"))
        val by = names(o.optJSONArray("artists")).ifEmpty { o.optJSONObject("show")?.optString("name").orEmpty() }
        return Item(uri, "track", o.optString("name"), by, small, big, durationMs = o.optInt("duration_ms"))
    }

    private fun playlist(o: JSONObject?): Item? {
        val uri = o?.optString("uri")?.takeIf { it.startsWith("spotify:") } ?: return null
        val (small, big) = images(o.optJSONArray("images"))
        val total = (o.optJSONObject("items") ?: o.optJSONObject("tracks"))?.optInt("total") ?: 0
        val owner = o.optJSONObject("owner")?.optString("display_name").orEmpty()
        return Item(uri, "playlist", o.optString("name"), listOf(owner, "$total TRACKS").filter { it.isNotBlank() }.joinToString(" · "),
            small, big, total)
    }

    private fun album(o: JSONObject?): Item? {
        val uri = o?.optString("uri")?.takeIf { it.startsWith("spotify:") } ?: return null
        val (small, big) = images(o.optJSONArray("images"))
        return Item(uri, "album", o.optString("name"), names(o.optJSONArray("artists")), small, big, o.optInt("total_tracks"))
    }

    private fun artist(o: JSONObject?): Item? {
        val uri = o?.optString("uri")?.takeIf { it.startsWith("spotify:") } ?: return null
        val (small, big) = images(o.optJSONArray("images"))
        return Item(uri, "artist", o.optString("name"), "ARTIST", small, big)
    }

    // ── playing ─────────────────────────────────────────────────────────────

    /**
     * Where Connect should play: whatever is already playing (a speaker, the
     * laptop), else this phone. Null when Spotify isn't running anywhere.
     */
    private suspend fun device(ctx: Context): String? {
        val list = get(ctx, url("/me/player/devices")).optJSONArray("devices") ?: return null
        val all = (0 until list.length()).mapNotNull { list.optJSONObject(it) }.filter { !it.optBoolean("is_restricted") }
        return (all.firstOrNull { it.optBoolean("is_active") }
            ?: all.firstOrNull { it.optString("type").equals("Smartphone", true) && it.optString("name").equals(Build.MODEL, true) }
            ?: all.firstOrNull { it.optString("type").equals("Smartphone", true) })
            ?.optString("id")?.takeIf { it.isNotEmpty() }
    }

    private suspend fun connect(ctx: Context, deviceId: String, body: JSONObject, shuffle: Boolean): Boolean {
        val (code, _) = call(ctx, "PUT", url("/me/player/play", "device_id" to deviceId), body)
        if (code !in 200..299) return false
        // After play, not before: shuffle applies to the device that is playing.
        call(ctx, "PUT", url("/me/player/shuffle", "state" to shuffle, "device_id" to deviceId))
        return true
    }

    /**
     * Plays [context] (a playlist, album or artist) from [start], or just the
     * [uris] list from [start]. Returns a line for the status readout.
     */
    suspend fun play(ctx: Context, context: String? = null, uris: List<String>? = null,
                     start: String? = null, shuffle: Boolean = false): String {
        val body = JSONObject()
        if (context != null) body.put("context_uri", context) else body.put("uris", JSONArray(uris.orEmpty().take(100)))
        if (start != null && !shuffle) body.put("offset", JSONObject().put("uri", start))

        device(ctx)?.let { if (connect(ctx, it, body, shuffle)) return "PLAYING" }

        // Spotify is here but not a Connect device yet: its own session can play a URI.
        val target = start ?: context ?: uris?.firstOrNull() ?: return "NOTHING TO PLAY"
        Media.sessions(ctx).firstOrNull { it.packageName == PKG }?.let { s ->
            if ((s.playbackState?.actions ?: 0L) and PlaybackState.ACTION_PLAY_FROM_URI != 0L) {
                s.transportControls.playFromUri(Uri.parse(target), Bundle())
                return "PLAYING"
            }
        }

        // Not running: open it on the item, then hand it the play once it's up.
        runCatching {
            ctx.startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(context ?: target)).setPackage(PKG)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
        }.onFailure { throw Problem("SPOTIFY ISN'T INSTALLED") }
        repeat(10) {
            delay(1000)
            device(ctx)?.let { if (connect(ctx, it, body, shuffle)) return "PLAYING" }
        }
        return "OPENED IN SPOTIFY"
    }
}
