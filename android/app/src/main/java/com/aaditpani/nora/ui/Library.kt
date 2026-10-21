package com.aaditpani.nora.ui

import android.content.Context
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.net.Uri
import android.util.LruCache
import androidx.activity.compose.BackHandler
import androidx.compose.foundation.Image
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.LazyListScope
import androidx.compose.foundation.lazy.itemsIndexed
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateMapOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.drawBehind
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.ImageBitmap
import androidx.compose.ui.graphics.asImageBitmap
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.aaditpani.nora.phone.Spotify
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import okhttp3.OkHttpClient
import okhttp3.Request

/**
 * NORA Player's LIBRARY screen: the user's playlists, liked songs and saved
 * albums, and search, read from the Spotify Web API (see [Spotify]) and
 * drawn like the rest of the player — true black, thin lines, the sleeve's
 * colour. Tapping plays through Spotify; nothing streams through NORA.
 */

private enum class Shelf(val label: String) { PLAYLISTS("PLAYLISTS"), LIKED("LIKED"), ALBUMS("ALBUMS"), SEARCH("SEARCH") }

/**
 * What was loaded, kept for the life of the process so flipping between
 * NOW PLAYING and LIBRARY doesn't refetch. REFRESH clears it.
 */
private object Shelves {
    val lists = mutableStateMapOf<String, List<Spotify.Item>>()
    fun clear() = lists.clear()
}

/** Covers off Spotify's CDN, a few MB of them kept in memory. */
private object Covers {
    private val http = OkHttpClient()
    private val cache = object : LruCache<String, Bitmap>(24 * 1024 * 1024) {
        override fun sizeOf(key: String, value: Bitmap) = value.byteCount
    }

    fun cached(url: String): ImageBitmap? = cache.get(url)?.asImageBitmap()

    suspend fun load(url: String): ImageBitmap? = cached(url) ?: withContext(Dispatchers.IO) {
        runCatching {
            http.newCall(Request.Builder().url(url).build()).execute().use { r ->
                r.body?.bytes()?.let { BitmapFactory.decodeByteArray(it, 0, it.size) }
            }
        }.getOrNull()?.also { cache.put(url, it) }?.asImageBitmap()
    }
}

/**
 * [callback] is the browser's return from sign-in, handed over by
 * PlayerActivity; [onCallbackDone] clears it once it has been used.
 */
@Composable
fun LibraryTab(accent: Color, callback: Uri?, onCallbackDone: () -> Unit) {
    val ctx = LocalContext.current
    val scope = rememberCoroutineScope()
    var linked by remember { mutableStateOf(Spotify.linked(ctx)) }
    var status by remember { mutableStateOf<String?>(null) }
    var busy by remember { mutableStateOf(false) }

    LaunchedEffect(callback) {
        val uri = callback ?: return@LaunchedEffect
        busy = true
        status = try {
            Spotify.finishLogin(ctx, uri); linked = true; "SIGNED IN"
        } catch (e: Spotify.Problem) { e.message }
        busy = false
        onCallbackDone()
    }

    Column(Modifier.fillMaxSize().burnInShift(rememberMinute())) {
        Spacer(Modifier.height(2.dp))
        if (!linked) Enter(true) { SignIn(accent, status, busy) }
        else Browser(accent, status, { status = it }, onSignOut = {
            Spotify.logout(ctx); Shelves.clear(); linked = false; status = "SIGNED OUT"
        }, run = { block -> scope.launch { block() } })
    }
}

// ── sign in ──────────────────────────────────────────────────────────────────

@Composable
private fun SignIn(accent: Color, status: String?, busy: Boolean) {
    val ctx = LocalContext.current
    Panel("SPOTIFY LINK", accent = accent, oled = true, badge = { Badge(if (busy) "LINKING" else "OFFLINE", accent, live = busy) }) {
        Text("Sign in once to browse your playlists, liked songs and albums here. NORA Player only reads " +
            "your library and starts playback; the sign-in stays on this phone.",
            color = ink(accent, 0.7f), fontFamily = Hud.Mono, fontSize = 12.sp, lineHeight = 17.sp)
        Spacer(Modifier.height(12.dp))
        if (!Spotify.configured) {
            Tracked("NO CLIENT ID BUILT IN — SET SPOTIFY_CLIENT_ID IN .ENV", color = Hud.Amber, size = 9.sp, spacing = 1.5.sp)
        } else {
            HudButton("SIGN IN WITH SPOTIFY", { ctx.startActivity(Spotify.loginIntent(ctx)) },
                Modifier.fillMaxWidth(), color = accent, strong = true, enabled = !busy)
            Spacer(Modifier.height(10.dp))
            Tracked(status?.let { "› $it" } ?: "› REDIRECT: ${Spotify.REDIRECT}", color = if (status != null) Hud.Amber else Hud.Faint,
                size = 8.sp, spacing = 1.2.sp, upper = status != null)
        }
    }
}

// ── browsing ─────────────────────────────────────────────────────────────────

@Composable
private fun Browser(
    accent: Color,
    status: String?,
    setStatus: (String?) -> Unit,
    onSignOut: () -> Unit,
    run: (suspend () -> Unit) -> Unit,
) {
    val ctx = LocalContext.current
    var shelf by remember { mutableStateOf(Shelf.PLAYLISTS) }
    var open by remember { mutableStateOf<Spotify.Item?>(null) }
    var query by remember { mutableStateOf("") }
    var results by remember { mutableStateOf<Spotify.Results?>(null) }
    var loading by remember { mutableStateOf(false) }
    var reload by remember { mutableStateOf(0) }
    /** Why an opened playlist has no rows, shown in its panel rather than the one-line status. */
    var note by remember { mutableStateOf<String?>(null) }
    val listState = rememberLazyListState()

    BackHandler(open != null) { open = null }

    val key = open?.let { "${it.kind}:${it.id}" } ?: shelf.name
    LaunchedEffect(key, reload) {
        listState.scrollToItem(0)
        note = null
        if (shelf == Shelf.SEARCH && open == null) return@LaunchedEffect
        if (Shelves.lists[key] != null) return@LaunchedEffect
        loading = true
        try {
            Shelves.lists[key] = load(ctx, shelf, open)
            setStatus(null)
        } catch (e: Spotify.Problem) {
            if (e.message == Spotify.HIDDEN) note = e.message else setStatus(e.message)
        }
        loading = false
    }

    // Search as you type, once typing pauses.
    LaunchedEffect(query, shelf) {
        if (shelf != Shelf.SEARCH) return@LaunchedEffect
        val q = query.trim()
        if (q.length < 2) { results = null; return@LaunchedEffect }
        delay(450)
        loading = true
        try { results = Spotify.search(ctx, q); setStatus(null) } catch (e: Spotify.Problem) { setStatus(e.message) }
        loading = false
    }

    fun play(context: String? = null, uris: List<String>? = null, start: String? = null, shuffle: Boolean = false) = run {
        setStatus("STARTING…")
        setStatus(try { Spotify.play(ctx, context, uris, start, shuffle) } catch (e: Spotify.Problem) { e.message })
    }

    LazyColumn(Modifier.fillMaxSize(), listState, verticalArrangement = Arrangement.spacedBy(0.dp)) {
        item {
            Header(accent, shelf, { shelf = it; open = null }, status, loading,
                onRefresh = { Shelves.lists.remove(key); reload++ }, onSignOut = onSignOut)
            Spacer(Modifier.height(12.dp))
        }
        val o = open
        when {
            o != null -> {
                item { Detail(o, accent, note, onBack = { open = null }, onPlay = { play(context = o.uri, shuffle = it) }); Spacer(Modifier.height(6.dp)) }
                val tracks = Shelves.lists[key].orEmpty()
                rows(tracks, accent) { play(context = o.uri, start = it.uri) }
            }
            shelf == Shelf.SEARCH -> {
                item {
                    HudField(query, { query = it }, "SONG, ARTIST, ALBUM, PLAYLIST", Modifier.fillMaxWidth(), singleLine = true, color = accent)
                    Spacer(Modifier.height(10.dp))
                }
                results?.let { r ->
                    section("TRACKS", r.tracks, accent) { play(uris = listOf(it.uri)) }
                    section("ARTISTS", r.artists, accent) { play(context = it.uri) }
                    section("ALBUMS", r.albums, accent) { open = it }
                    section("PLAYLISTS", r.playlists, accent) { open = it }
                }
            }
            else -> {
                val list = Shelves.lists[key].orEmpty()
                if (shelf == Shelf.LIKED && list.isNotEmpty()) item {
                    Row(Modifier.padding(bottom = 8.dp), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                        HudButton("PLAY", { play(uris = list.map { it.uri }) }, Modifier.weight(1f), color = accent, strong = true, textSize = 10.sp)
                        HudButton("SHUFFLE", { play(uris = list.map { it.uri }.shuffled()) }, Modifier.weight(1f), color = accent, textSize = 10.sp)
                    }
                }
                rows(list, accent) { item ->
                    when (shelf) {
                        Shelf.LIKED -> play(uris = list.map { it.uri }, start = item.uri)
                        else -> open = item
                    }
                }
            }
        }
        item { Spacer(Modifier.height(24.dp)) }
    }
}

private suspend fun load(ctx: Context, shelf: Shelf, open: Spotify.Item?): List<Spotify.Item> = when {
    open?.kind == "playlist" -> Spotify.playlistTracks(ctx, open.id)
    open?.kind == "album" -> Spotify.albumTracks(ctx, open)
    shelf == Shelf.PLAYLISTS -> Spotify.playlists(ctx)
    shelf == Shelf.LIKED -> Spotify.liked(ctx)
    shelf == Shelf.ALBUMS -> Spotify.albums(ctx)
    else -> emptyList()
}

@Composable
private fun Header(
    accent: Color,
    shelf: Shelf,
    onShelf: (Shelf) -> Unit,
    status: String?,
    loading: Boolean,
    onRefresh: () -> Unit,
    onSignOut: () -> Unit,
) {
    Panel("LIBRARY", accent = accent, live = loading, oled = true,
        badge = { Badge(if (loading) "SYNC" else "LINKED", accent, live = loading) }) {
        Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
            Shelf.entries.forEach { s ->
                val on = s == shelf
                Column(Modifier.clickable { onShelf(s) }.padding(vertical = 4.dp, horizontal = 2.dp),
                    horizontalAlignment = Alignment.CenterHorizontally) {
                    Tracked(s.label, color = if (on) accent else ink(accent, 0.45f), size = 10.sp, spacing = 2.sp, glow = on)
                    Spacer(Modifier.height(5.dp))
                    Box(Modifier.width(26.dp).height(1.5.dp).background(if (on) accent else Color.Transparent))
                }
            }
        }
        Spacer(Modifier.height(10.dp))
        Row(verticalAlignment = Alignment.CenterVertically) {
            Tracked("› " + (status ?: if (loading) "READING LIBRARY…" else "TAP TO PLAY ON SPOTIFY"), Modifier.weight(1f),
                color = if (status != null && status != "PLAYING") Hud.Amber else ink(accent, 0.45f), size = 8.sp, spacing = 1.5.sp)
            Tracked("REFRESH", Modifier.clickable(onClick = onRefresh).padding(6.dp), color = ink(accent, 0.6f), size = 8.sp, spacing = 1.5.sp)
            Tracked("SIGN OUT", Modifier.clickable(onClick = onSignOut).padding(6.dp), color = ink(accent, 0.4f), size = 8.sp, spacing = 1.5.sp)
        }
    }
}

/** An opened playlist or album: big sleeve, title, PLAY and SHUFFLE. */
@Composable
private fun Detail(item: Spotify.Item, accent: Color, note: String?, onBack: () -> Unit, onPlay: (shuffle: Boolean) -> Unit) {
    Panel(item.kind.uppercase(), accent = accent, oled = true, badge = {
        Box(Modifier.clickable(onClick = onBack)) { Badge("‹ BACK", accent) }
    }) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            Cover(item.bigImage ?: item.image, 104.dp, accent)
            Spacer(Modifier.width(14.dp))
            Column(Modifier.weight(1f)) {
                Text(item.title.uppercase(), color = accent, fontFamily = Hud.Disp, fontWeight = FontWeight.SemiBold,
                    fontSize = 18.sp, letterSpacing = 1.5.sp, lineHeight = 21.sp, maxLines = 3)
                Spacer(Modifier.height(4.dp))
                Tracked(item.sub, color = ink(accent, 0.55f), size = 9.sp, spacing = 1.5.sp)
            }
        }
        Spacer(Modifier.height(12.dp))
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            HudButton("PLAY", { onPlay(false) }, Modifier.weight(1f), color = accent, strong = true, textSize = 10.sp)
            HudButton("SHUFFLE", { onPlay(true) }, Modifier.weight(1f), color = accent, textSize = 10.sp)
        }
        note?.let {
            Spacer(Modifier.height(12.dp))
            Text(it, color = ink(accent, 0.5f), fontFamily = Hud.Mono, fontSize = 10.sp, letterSpacing = 1.sp, lineHeight = 15.sp)
        }
    }
}

private fun LazyListScope.section(title: String, list: List<Spotify.Item>, accent: Color, onTap: (Spotify.Item) -> Unit) {
    if (list.isEmpty()) return
    item {
        Row(Modifier.padding(top = 10.dp, bottom = 2.dp), verticalAlignment = Alignment.CenterVertically) {
            Diamond(7.dp, accent)
            Spacer(Modifier.width(8.dp))
            Tracked(title, color = accent, size = 9.sp, spacing = 3.sp, font = Hud.Disp, weight = FontWeight.SemiBold)
        }
    }
    rows(list, accent, onTap)
}

private fun LazyListScope.rows(list: List<Spotify.Item>, accent: Color, onTap: (Spotify.Item) -> Unit) {
    itemsIndexed(list, key = { i, it -> "$i:${it.uri}" }) { i, item -> EntryRow(i, item, accent, onTap) }
}

@Composable
private fun EntryRow(i: Int, item: Spotify.Item, accent: Color, onTap: (Spotify.Item) -> Unit) {
    Column {
        Row(Modifier.fillMaxWidth().clickable { onTap(item) }.padding(vertical = 7.dp, horizontal = 2.dp),
            verticalAlignment = Alignment.CenterVertically) {
            Tracked("%02d".format(i + 1), Modifier.width(28.dp), color = accent.copy(alpha = 0.6f), size = 9.sp)
            Cover(item.image, 42.dp, accent)
            Spacer(Modifier.width(11.dp))
            Column(Modifier.weight(1f)) {
                Tracked(item.title, color = ink(accent), size = 12.sp, spacing = 1.sp, upper = false)
                Spacer(Modifier.height(2.dp))
                Tracked(item.sub, color = ink(accent, 0.45f), size = 9.sp, spacing = 1.4.sp)
            }
            if (item.durationMs > 0) Tracked(clock(item.durationMs.toLong()), color = ink(accent, 0.4f), size = 9.sp, spacing = 1.sp)
            else if (item.kind != "track") Tracked("›", color = accent.copy(alpha = 0.6f), size = 14.sp)
        }
        Box(Modifier.fillMaxWidth().height(1.dp).background(
            Brush.horizontalGradient(listOf(accent.copy(alpha = 0.08f), Color.Transparent))))
    }
}

/** A sleeve on black: thin frame, scanlines, and a dark placeholder until it arrives. */
@Composable
private fun Cover(url: String?, size: Dp, accent: Color) {
    var bmp by remember(url) { mutableStateOf(url?.let(Covers::cached)) }
    LaunchedEffect(url) { if (bmp == null && url != null) bmp = Covers.load(url) }
    Box(Modifier.size(size).border(1.dp, accent.copy(alpha = 0.22f)).background(accent.copy(alpha = 0.04f))) {
        val b = bmp
        if (b != null) Image(b, null, Modifier.fillMaxSize(), contentScale = ContentScale.Crop)
        else Box(Modifier.align(Alignment.Center)) { Diamond(8.dp, accent.copy(alpha = 0.35f)) }
        Box(Modifier.fillMaxSize().drawBehind {
            var y = 0f
            val step = 3.dp.toPx()
            while (y < this.size.height) { drawRect(Color.Black.copy(alpha = 0.16f), Offset(0f, y), Size(this.size.width, 1f)); y += step }
        })
    }
}
