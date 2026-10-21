package com.aaditpani.nora.ui

import android.app.SearchManager
import android.content.ContentResolver
import android.content.Context
import android.content.Intent
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.media.MediaMetadata
import android.media.session.MediaController
import android.media.session.MediaSession
import android.media.session.PlaybackState
import android.net.Uri
import android.os.Bundle
import android.os.SystemClock
import android.provider.Settings
import androidx.compose.animation.animateColorAsState
import androidx.compose.animation.core.Animatable
import androidx.compose.animation.core.LinearEasing
import androidx.compose.animation.core.animateFloat
import androidx.compose.animation.core.animateFloatAsState
import androidx.compose.animation.core.infiniteRepeatable
import androidx.compose.animation.core.rememberInfiniteTransition
import androidx.compose.animation.core.tween
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.ExperimentalFoundationApi
import androidx.compose.foundation.Image
import androidx.compose.foundation.background
import androidx.compose.foundation.basicMarquee
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.gestures.detectHorizontalDragGestures
import androidx.compose.foundation.gestures.detectTapGestures
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ExperimentalLayoutApi
import androidx.compose.foundation.layout.FlowRow
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.aspectRatio
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.offset
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clipToBounds
import androidx.compose.ui.draw.drawBehind
import androidx.compose.ui.draw.drawWithContent
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.ColorFilter
import androidx.compose.ui.graphics.ColorMatrix
import androidx.compose.ui.graphics.asImageBitmap
import androidx.compose.ui.graphics.lerp
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.IntOffset
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.LifecycleEventObserver
import androidx.lifecycle.compose.LocalLifecycleOwner
import com.aaditpani.nora.phone.LockPlayer
import com.aaditpani.nora.phone.Media
import com.aaditpani.nora.phone.NoraNotificationListener
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.withContext

/**
 * The dashboard's Spotify card, as a phone screen.
 *
 * It reads and drives the playing app through Android's media session — the
 * same access the phone.media_control capability uses — so Spotify does the
 * streaming and NORA is only the face on it. No Spotify SDK, no login.
 *
 * Drawn for OLED: the ground is true black so unlit pixels stay off, light
 * comes from thin lines and the sleeve, the accent is the cover's colour
 * pushed to full brightness so it holds against black, and the whole screen
 * wanders a couple of pixels so nothing static burns in.
 */

/** What the playing session says, captured at one instant. */
private data class NowPlaying(
    val pkg: String,
    val app: String,
    val title: String?,
    val artist: String?,
    val album: String?,
    val art: Bitmap?,
    val durationMs: Long,
    val state: Int,
    val positionMs: Long,
    val updatedAt: Long,
    val speed: Float,
    val actions: Long,
    val custom: List<PlaybackState.CustomAction>,
    val queue: List<MediaSession.QueueItem>,
    val activeQueueId: Long,
) {
    val playing get() = state == PlaybackState.STATE_PLAYING || state == PlaybackState.STATE_BUFFERING

    /** The position now, carried forward from the last report at the session's speed. */
    fun positionAt(elapsed: Long): Long {
        if (!playing || updatedAt <= 0) return positionMs
        val p = positionMs + ((elapsed - updatedAt) * speed).toLong()
        return if (durationMs > 0) p.coerceIn(0, durationMs) else p.coerceAtLeast(0)
    }
}

private const val SPOTIFY = "com.spotify.music"

/** Whatever is playing; failing that Spotify, paused; failing that anything with a session. */
private fun pick(sessions: List<MediaController>): MediaController? =
    sessions.firstOrNull { it.playbackState?.state == PlaybackState.STATE_PLAYING }
        ?: sessions.firstOrNull { it.packageName == SPOTIFY }
        ?: sessions.firstOrNull()

private fun snapshot(ctx: Context, c: MediaController): NowPlaying {
    val m = c.metadata
    val s = c.playbackState
    return NowPlaying(
        pkg = c.packageName,
        app = Media.appLabel(ctx, c.packageName),
        title = m?.getString(MediaMetadata.METADATA_KEY_TITLE)?.takeIf { it.isNotBlank() },
        artist = (m?.getString(MediaMetadata.METADATA_KEY_ARTIST)
            ?: m?.getString(MediaMetadata.METADATA_KEY_ALBUM_ARTIST))?.takeIf { it.isNotBlank() },
        album = m?.getString(MediaMetadata.METADATA_KEY_ALBUM)?.takeIf { it.isNotBlank() },
        art = m?.let { artOf(ctx, it) },
        durationMs = m?.getLong(MediaMetadata.METADATA_KEY_DURATION) ?: 0L,
        state = s?.state ?: PlaybackState.STATE_NONE,
        positionMs = s?.position ?: 0L,
        updatedAt = s?.lastPositionUpdateTime ?: 0L,
        speed = s?.playbackSpeed?.takeIf { it > 0f } ?: 1f,
        actions = s?.actions ?: 0L,
        custom = s?.customActions.orEmpty(),
        queue = c.queue.orEmpty(),
        activeQueueId = s?.activeQueueItemId ?: MediaSession.QueueItem.UNKNOWN_ID.toLong(),
    )
}

/** The bitmap in the metadata, else a local URI it points at. Remote URIs are left alone. */
private fun artOf(ctx: Context, m: MediaMetadata): Bitmap? {
    m.getBitmap(MediaMetadata.METADATA_KEY_ALBUM_ART)?.let { return it }
    m.getBitmap(MediaMetadata.METADATA_KEY_ART)?.let { return it }
    val uri = (m.getString(MediaMetadata.METADATA_KEY_ALBUM_ART_URI)
        ?: m.getString(MediaMetadata.METADATA_KEY_ART_URI))?.let(Uri::parse) ?: return null
    if (uri.scheme != ContentResolver.SCHEME_CONTENT && uri.scheme != ContentResolver.SCHEME_ANDROID_RESOURCE) return null
    return runCatching { ctx.contentResolver.openInputStream(uri)?.use(BitmapFactory::decodeStream) }.getOrNull()
}

/**
 * The cover's most vivid colour, at full brightness: on black, a dim accent
 * reads as mud. Grey covers have nothing to give and get null.
 */
private fun accentOf(b: Bitmap): Color? {
    val small = Bitmap.createScaledBitmap(b.copy(Bitmap.Config.ARGB_8888, false), 24, 24, true)
    val hsv = FloatArray(3)
    var best: FloatArray? = null
    var bestScore = 0f
    for (y in 0 until small.height) for (x in 0 until small.width) {
        android.graphics.Color.colorToHSV(small.getPixel(x, y), hsv)
        if (hsv[2] < 0.22f) continue
        val score = hsv[1] * (0.35f + hsv[2])
        if (score > bestScore) { bestScore = score; best = hsv.copyOf() }
    }
    val h = best ?: return null
    if (h[1] < 0.22f) return null
    return Color.hsv(h[0], h[1].coerceIn(0.5f, 0.85f), 1f)
}

/** Body text for the sleeve's colour: near-white leaning toward it, so it sits with the accent instead of NORA's blue. */
internal fun ink(accent: Color, alpha: Float = 0.9f) = lerp(Color.White, accent, 0.3f).copy(alpha = alpha)

internal fun clock(ms: Long): String {
    val s = (ms / 1000).coerceAtLeast(0)
    return if (s >= 3600) "%d:%02d:%02d".format(s / 3600, s / 60 % 60, s % 60) else "%d:%02d".format(s / 60, s % 60)
}

// ── the tab ──────────────────────────────────────────────────────────────────

/** [settings] shows the lock-screen switch; off when the tab is itself over the lock screen. */
@Composable
fun MusicTab(settings: Boolean = true, onAccent: (Color?) -> Unit) {
    val ctx = LocalContext.current
    var access by remember { mutableStateOf(Media.hasAccess(ctx)) }
    var controller by remember { mutableStateOf<MediaController?>(null) }
    var np by remember { mutableStateOf<NowPlaying?>(null) }
    var accent by remember { mutableStateOf<Color?>(null) }

    val lifecycle = LocalLifecycleOwner.current.lifecycle
    DisposableEffect(lifecycle) {
        val observer = LifecycleEventObserver { _, e -> if (e == Lifecycle.Event.ON_RESUME) access = Media.hasAccess(ctx) }
        lifecycle.addObserver(observer)
        onDispose { lifecycle.removeObserver(observer) }
    }

    // Which session to show can change without any callback on the current
    // one (another app starts playing), so it is re-picked on a slow beat.
    LaunchedEffect(access) {
        while (access) {
            val next = pick(Media.sessions(ctx))
            if (next?.sessionToken != controller?.sessionToken) controller = next
            if (next == null) np = null
            delay(1500)
        }
    }

    DisposableEffect(controller) {
        val c = controller
        val cb = object : MediaController.Callback() {
            override fun onMetadataChanged(metadata: MediaMetadata?) { if (c != null) np = snapshot(ctx, c) }
            override fun onPlaybackStateChanged(state: PlaybackState?) { if (c != null) np = snapshot(ctx, c) }
            override fun onQueueChanged(queue: MutableList<MediaSession.QueueItem>?) { if (c != null) np = snapshot(ctx, c) }
            override fun onSessionDestroyed() { controller = null; np = null }
        }
        if (c != null) { np = snapshot(ctx, c); c.registerCallback(cb) }
        onDispose { c?.unregisterCallback(cb) }
    }

    val art = np?.art
    LaunchedEffect(art) {
        accent = art?.let { withContext(Dispatchers.Default) { runCatching { accentOf(it) }.getOrNull() } }
        onAccent(accent)
    }
    DisposableEffect(Unit) { onDispose { onAccent(null) } }

    val tint by animateColorAsState(accent ?: Hud.Cyan, tween(900), label = "tint")

    Column(Modifier.fillMaxSize().burnInShift(rememberMinute()).verticalScroll(rememberScrollState()).padding(bottom = 20.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp)) {
        Spacer(Modifier.height(2.dp))
        when {
            !access -> Enter(true) { NoAccess(ctx) }
            else -> {
                Enter(true) { NowPlayingPanel(np, controller, tint) }
                Enter(true, 120) { PlayAnything(controller, np, tint) }
                val upNext = np?.let { n -> upNext(n) }.orEmpty()
                if (upNext.isNotEmpty()) Enter(true, 200) { UpNext(upNext, controller, tint) }
                if (settings) Enter(true, 260) { LockScreenPanel(tint) }
            }
        }
    }
}

/**
 * A slow walk round a 2 dp square, a step a minute. Too small and too slow to
 * notice, enough that no edge sits on the same OLED pixels for hours.
 */
internal fun Modifier.burnInShift(minute: Long) = this.then(Modifier.offset {
    val path = intArrayOf(0, 0, 1, 0, 1, 1, 0, 1, -1, 1, -1, 0, -1, -1, 0, -1, 1, -1)
    val i = (minute % (path.size / 2)).toInt() * 2
    val step = 1.dp.roundToPx()
    IntOffset(path[i] * step, path[i + 1] * step)
})

@Composable
internal fun rememberMinute(): Long {
    var m by remember { mutableStateOf(System.currentTimeMillis() / 60_000) }
    LaunchedEffect(Unit) { while (true) { delay(60_000 - System.currentTimeMillis() % 60_000); m = System.currentTimeMillis() / 60_000 } }
    return m
}

private fun upNext(n: NowPlaying): List<MediaSession.QueueItem> {
    val at = n.queue.indexOfFirst { it.queueId == n.activeQueueId }
    return n.queue.drop(if (at >= 0) at + 1 else 0).take(8)
}

// ── now playing ──────────────────────────────────────────────────────────────

@Composable
private fun NowPlayingPanel(np: NowPlaying?, c: MediaController?, accent: Color) {
    val ctx = LocalContext.current
    val playing = np?.playing == true
    Panel(np?.app ?: "SPOTIFY", accent = accent, live = playing, oled = true,
        badge = { Badge(if (np == null) "NO SIGNAL" else if (playing) "LIVE" else "HOLD", accent, live = playing) }) {
        Sleeve(np, c, accent)
        Spacer(Modifier.height(14.dp))
        TrackIdentity(np, accent)
        Spacer(Modifier.height(14.dp))
        if (np == null) {
            HudButton("OPEN SPOTIFY", {
                ctx.packageManager.getLaunchIntentForPackage(SPOTIFY)?.let(ctx::startActivity)
            }, Modifier.fillMaxWidth(), color = accent, strong = true)
        } else {
            Transport(np, c, accent)
            if (np.custom.isNotEmpty()) { Spacer(Modifier.height(10.dp)); CustomActions(np, c, accent) }
            Spacer(Modifier.height(12.dp))
            VolumeRow(c, accent)
        }
    }
}

/**
 * The artwork as the head of the card: scanlines and a vignette so it reads
 * as a display, a read head crossing it while it plays, a bright band wiping
 * down on a track change, and the scrub bar riding its bottom edge.
 */
@Composable
private fun Sleeve(np: NowPlaying?, c: MediaController?, accent: Color) {
    val playing = np?.playing == true
    val t = rememberInfiniteTransition(label = "sleeve")
    val scan by t.animateFloat(-0.35f, 1.35f, infiniteRepeatable(tween(7000, easing = LinearEasing)), label = "scan")

    // Paused drains the colour and drops the brightness: you can feel the
    // state without reading the badge, and a paused screen draws less.
    val sat by animateFloatAsState(if (playing) 1f else 0.6f, tween(700), label = "sat")
    val lum by animateFloatAsState(if (playing) 1f else 0.72f, tween(700), label = "lum")

    val wipe = remember { Animatable(1f) }
    LaunchedEffect(np?.title, np?.artist) {
        if (np?.title != null) { wipe.snapTo(0f); wipe.animateTo(1f, tween(620, easing = Hud.EaseOut)) }
    }

    Box(Modifier.fillMaxWidth().padding(horizontal = 0.dp)) {
        // Light coming off the sleeve: a faint pool of its colour behind it.
        Box(Modifier.matchParentSize().drawBehind {
            drawRect(Brush.radialGradient(listOf(accent.copy(alpha = if (np?.art != null) 0.16f else 0.06f), Color.Transparent),
                center = Offset(size.width / 2, size.height * 0.55f), radius = size.width * 0.75f),
                topLeft = Offset(-size.width * 0.2f, -size.height * 0.1f), size = Size(size.width * 1.4f, size.height * 1.3f))
        })
        Box(Modifier.fillMaxWidth().aspectRatio(1f).clipToBounds()
            .background(Color.Black)
            .border(1.dp, accent.copy(alpha = 0.32f))) {
            val art = np?.art
            if (art != null) {
                Image(art.asImageBitmap(), null, Modifier.fillMaxSize(), contentScale = ContentScale.Crop,
                    colorFilter = ColorFilter.colorMatrix(ColorMatrix().apply {
                        setToSaturation(sat)
                        val k = lum
                        timesAssign(ColorMatrix(floatArrayOf(k, 0f, 0f, 0f, 0f, 0f, k, 0f, 0f, 0f,
                            0f, 0f, k, 0f, 0f, 0f, 0f, 0f, 1f, 0f)))
                    }))
            } else {
                NoArt(accent, if (np == null) "NO SIGNAL" else "NO ARTWORK")
            }

            Canvas(Modifier.fillMaxSize()) {
                val w = size.width
                val h = size.height
                var y = 0f
                while (y < h) { drawRect(Color(0x2B000000), Offset(0f, y), Size(w, 1f)); y += 3f }
                drawRect(Brush.radialGradient(listOf(Color.Transparent, Color.Transparent, Color(0x99000000)),
                    center = Offset(w / 2, h * 0.4f), radius = w * 0.78f))
                if (playing) {
                    val band = h * 0.3f
                    val top = scan * h - band / 2
                    drawRect(Brush.verticalGradient(listOf(Color.Transparent, accent.copy(alpha = 0.16f),
                        Color.White.copy(alpha = 0.12f), Color.Transparent), top, top + band),
                        Offset(0f, top), Size(w, band))
                }
                if (wipe.value < 1f) {
                    val band = h * 0.26f
                    val top = -band + wipe.value * (h + band * 2)
                    val a = if (wipe.value < 0.18f) wipe.value / 0.18f else 1f - (wipe.value - 0.18f) / 0.82f
                    drawRect(Brush.verticalGradient(listOf(Color.Transparent, accent.copy(alpha = 0.55f * a),
                        Color.White.copy(alpha = 0.5f * a), Color.Transparent), top, top + band),
                        Offset(0f, top), Size(w, band))
                }
                // Readability floor for the times and the scrub bar.
                drawRect(Brush.verticalGradient(listOf(Color.Transparent, Color(0x73000000), Color(0xEE000000)),
                    h * 0.62f, h), Offset(0f, h * 0.62f), Size(w, h * 0.38f))
            }

            StatusChip(np, Modifier.align(Alignment.TopStart).padding(10.dp))
            if (playing) LevelMeter(Modifier.align(Alignment.TopEnd).padding(top = 14.dp, end = 14.dp))
            if (np != null) Scrub(np, c, accent, Modifier.align(Alignment.BottomCenter))
            Box(Modifier.matchParentSize().brackets(Color(0xBFE9F6FF), arm = 15.dp))
        }
    }
}

@Composable
private fun NoArt(accent: Color, label: String) {
    Box(Modifier.fillMaxSize().drawBehind {
        val step = 22.dp.toPx()
        val c = accent.copy(alpha = 0.07f)
        var x = 0f
        while (x < size.width) { drawLine(c, Offset(x, 0f), Offset(x, size.height)); x += step }
        var y = 0f
        while (y < size.height) { drawLine(c, Offset(0f, y), Offset(size.width, y)); y += step }
    }, contentAlignment = Alignment.Center) {
        Column(horizontalAlignment = Alignment.CenterHorizontally) {
            Diamond(14.dp, accent.copy(alpha = 0.6f))
            Spacer(Modifier.height(12.dp))
            Tracked(label, color = Hud.Dim, size = 10.sp, spacing = 4.sp)
        }
    }
}

@Composable
private fun StatusChip(np: NowPlaying?, modifier: Modifier) {
    val (label, dot) = when {
        np == null -> "IDLE" to Hud.Faint
        np.state == PlaybackState.STATE_BUFFERING -> "BUFFERING" to Hud.Amber
        np.playing -> "PLAYING" to Hud.Green
        else -> "PAUSED" to Hud.Amber
    }
    Row(modifier.background(Color(0xB8000000)).border(1.dp, Color.White.copy(alpha = 0.16f))
        .padding(horizontal = 8.dp, vertical = 4.dp), verticalAlignment = Alignment.CenterVertically) {
        Dot(dot, blink = np?.playing == true, size = 5.dp)
        Spacer(Modifier.width(6.dp))
        Tracked(label, color = Color(0xFFDDEFFF), size = 9.sp, spacing = 2.sp)
    }
}

/** Four white bars beside the chip. Not wired to the audio — nothing exposes it — just a running light. */
@Composable
private fun LevelMeter(modifier: Modifier) {
    val t = rememberInfiniteTransition(label = "lvl")
    val p by t.animateFloat(0f, 1f, infiniteRepeatable(tween(1500, easing = LinearEasing)), label = "p")
    Canvas(modifier.size(20.dp, 15.dp)) {
        val bw = 3.dp.toPx()
        val gap = (size.width - bw * 4) / 3
        floatArrayOf(0f, 180f, 360f, 90f).forEachIndexed { i, d ->
            val ph = ((p - d / 1500f) % 1f + 1f) % 1f
            val s = 0.3f + 0.7f * ((1 - kotlin.math.cos(ph * 2 * Math.PI).toFloat()) / 2)
            val h = size.height * s
            drawRect(Brush.verticalGradient(listOf(Color.White, Color(0x8CCFE9FF)), size.height - h, size.height),
                Offset(i * (bw + gap), size.height - h), Size(bw, h))
        }
    }
}

/** Times above a 3 dp bar with a 24 dp hit area; tap or drag to seek. */
@Composable
private fun Scrub(np: NowPlaying, c: MediaController?, accent: Color, modifier: Modifier) {
    var now by remember { mutableStateOf(SystemClock.elapsedRealtime()) }
    LaunchedEffect(np.playing) { while (np.playing) { now = SystemClock.elapsedRealtime(); delay(250) } }
    var drag by remember { mutableStateOf<Float?>(null) }
    val canSeek = np.durationMs > 0 && np.actions and PlaybackState.ACTION_SEEK_TO != 0L && c != null
    val frac = drag ?: if (np.durationMs > 0) np.positionAt(now).toFloat() / np.durationMs else 0f
    val shown = if (drag != null) (drag!! * np.durationMs).toLong() else np.positionAt(now)
    fun seek(f: Float) { c?.transportControls?.seekTo((f.coerceIn(0f, 1f) * np.durationMs).toLong()) }

    Column(modifier.fillMaxWidth()) {
        Row(Modifier.fillMaxWidth().padding(horizontal = 12.dp)) {
            Tracked(clock(shown), color = Color(0xFFCFE6F8), size = 10.sp, spacing = 1.5.sp)
            Spacer(Modifier.weight(1f))
            Tracked(if (np.durationMs > 0) clock(np.durationMs) else "--:--", color = Color(0xFFCFE6F8), size = 10.sp, spacing = 1.5.sp)
        }
        Box(Modifier.fillMaxWidth().height(24.dp)
            .then(if (canSeek) Modifier
                .pointerInput(np.durationMs) {
                    detectTapGestures { o -> seek(o.x / size.width) }
                }
                .pointerInput(np.durationMs) {
                    detectHorizontalDragGestures(
                        onDragStart = { o -> drag = (o.x / size.width).coerceIn(0f, 1f) },
                        onDragEnd = { drag?.let(::seek); drag = null },
                        onDragCancel = { drag = null },
                        onHorizontalDrag = { ch, _ -> drag = (ch.position.x / size.width).coerceIn(0f, 1f) })
                } else Modifier)
            .drawWithContent {
                val barH = (if (drag != null) 5 else 3).dp.toPx()
                val y = size.height - barH - 6.dp.toPx()
                drawRect(Color(0x8C000000), Offset(0f, y), Size(size.width, barH))
                val fw = size.width * frac.coerceIn(0f, 1f)
                drawRect(accent.copy(alpha = 0.35f), Offset(0f, y - 2.dp.toPx()), Size(fw, barH + 4.dp.toPx()))
                drawRect(Brush.horizontalGradient(listOf(accent.copy(alpha = 0.55f), accent), 0f, fw.coerceAtLeast(1f)),
                    Offset(0f, y), Size(fw, barH))
                if (drag != null || canSeek) {
                    val r = (if (drag != null) 6 else 4).dp.toPx()
                    drawCircle(accent.copy(alpha = 0.45f), r * 1.9f, Offset(fw, y + barH / 2))
                    drawCircle(Color(0xFFEAF6FF), r, Offset(fw, y + barH / 2))
                }
            })
    }
}

@OptIn(ExperimentalFoundationApi::class)
@Composable
private fun TrackIdentity(np: NowPlaying?, accent: Color) {
    Column {
        Text((np?.title ?: "NO TRACK").uppercase(), Modifier.fillMaxWidth().basicMarquee(iterations = Int.MAX_VALUE),
            color = if (np == null) Hud.Dim else accent, fontFamily = Hud.Disp, fontWeight = FontWeight.SemiBold,
            fontSize = 24.sp, letterSpacing = 1.5.sp, maxLines = 1,
            style = TextStyle(shadow = if (np == null) null else Hud.glow(accent, 18f)))
        Spacer(Modifier.height(2.dp))
        Tracked(np?.artist ?: "—", color = ink(accent), size = 12.sp, spacing = 2.5.sp)
        np?.album?.let {
            Spacer(Modifier.height(3.dp))
            Tracked(it, color = ink(accent, 0.45f), size = 10.sp, spacing = 2.sp)
        }
    }
}

@Composable
private fun Transport(np: NowPlaying, c: MediaController?, accent: Color) {
    val t = c?.transportControls
    Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(6.dp)) {
        HudButton("«", { t?.skipToPrevious() }, Modifier.weight(1f), color = accent, textSize = 15.sp)
        HudButton(if (np.playing) "PAUSE" else "PLAY", { if (np.playing) t?.pause() else t?.play() },
            Modifier.weight(2f), color = accent, strong = true, textSize = 13.sp)
        HudButton("»", { t?.skipToNext() }, Modifier.weight(1f), color = accent, textSize = 15.sp)
    }
}

/** The app's own extra buttons — Spotify's like, shuffle and repeat arrive this way. */
@OptIn(ExperimentalLayoutApi::class)
@Composable
private fun CustomActions(np: NowPlaying, c: MediaController?, accent: Color) {
    FlowRow(horizontalArrangement = Arrangement.spacedBy(6.dp), verticalArrangement = Arrangement.spacedBy(6.dp)) {
        np.custom.take(6).forEach { a ->
            Box(Modifier.border(1.dp, accent.copy(alpha = 0.22f), RoundedCornerShape(2.dp))
                .background(accent.copy(alpha = 0.04f))
                .clickable { c?.transportControls?.sendCustomAction(a, Bundle()) }
                .padding(horizontal = 10.dp, vertical = 7.dp)) {
                Tracked(a.name.toString(), color = ink(accent, 0.8f), size = 9.sp, spacing = 1.8.sp)
            }
        }
    }
}

/** The session's own volume: the phone's media stream, or the speaker it's casting to. */
@Composable
private fun VolumeRow(c: MediaController?, accent: Color) {
    if (c == null) return
    val info = c.playbackInfo ?: return
    val max = info.maxVolume.coerceAtLeast(1)
    var level by remember(c) { mutableIntStateOf(info.currentVolume) }
    LaunchedEffect(c) { while (true) { delay(1000); c.playbackInfo?.let { level = it.currentVolume } } }
    val remote = info.playbackType == MediaController.PlaybackInfo.PLAYBACK_TYPE_REMOTE
    fun set(f: Float) {
        val v = (f.coerceIn(0f, 1f) * max).toInt()
        level = v
        c.setVolumeTo(v, 0)
    }
    Row(verticalAlignment = Alignment.CenterVertically) {
        Tracked(if (remote) "CAST" else "VOL", Modifier.width(38.dp), size = 9.sp, spacing = 2.sp)
        Box(Modifier.weight(1f).height(26.dp)
            .pointerInput(c) { detectTapGestures { o -> set(o.x / size.width) } }
            .pointerInput(c) { detectHorizontalDragGestures { ch, _ -> set(ch.position.x / size.width) } }
            .drawBehind {
                val y = size.height / 2
                val f = level.toFloat() / max
                drawRect(accent.copy(alpha = 0.12f), Offset(0f, y - 1.5f), Size(size.width, 3f))
                drawRect(Brush.horizontalGradient(listOf(accent.copy(alpha = 0.4f), accent), 0f, (size.width * f).coerceAtLeast(1f)),
                    Offset(0f, y - 1.5f), Size(size.width * f, 3f))
                drawCircle(accent.copy(alpha = 0.35f), 9.dp.toPx(), Offset(size.width * f, y))
                drawCircle(accent, 5.dp.toPx(), Offset(size.width * f, y))
            })
        Spacer(Modifier.width(10.dp))
        Tracked("${level * 100 / max}%", Modifier.width(38.dp), color = accent, size = 10.sp, spacing = 1.sp)
    }
}

// ── play anything ────────────────────────────────────────────────────────────

/**
 * Names a song, artist or playlist and Spotify plays it: the session's
 * play-from-search when it offers one, else Spotify's own search intent.
 */
@Composable
private fun PlayAnything(c: MediaController?, np: NowPlaying?, accent: Color) {
    val ctx = LocalContext.current
    var q by remember { mutableStateOf("") }
    var sent by remember { mutableStateOf<String?>(null) }
    fun go() {
        val query = q.trim().takeIf { it.isNotEmpty() } ?: return
        val spotify = c?.takeIf { it.packageName == SPOTIFY }
        if (spotify != null && np != null && np.actions and PlaybackState.ACTION_PLAY_FROM_SEARCH != 0L) {
            spotify.transportControls.playFromSearch(query, Bundle())
        } else {
            runCatching {
                ctx.startActivity(Intent(android.provider.MediaStore.INTENT_ACTION_MEDIA_PLAY_FROM_SEARCH)
                    .setPackage(SPOTIFY)
                    .putExtra(SearchManager.QUERY, query)
                    .putExtra(android.provider.MediaStore.EXTRA_MEDIA_FOCUS, "vnd.android.cursor.item/*")
                    .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
            }
        }
        sent = query
        q = ""
    }
    Panel("PLAY ANYTHING", accent = accent, oled = true) {
        HudField(q, { q = it }, "SONG, ARTIST, PLAYLIST", Modifier.fillMaxWidth(), singleLine = true, onSend = ::go, color = accent)
        Spacer(Modifier.height(8.dp))
        Row(verticalAlignment = Alignment.CenterVertically) {
            Tracked(sent?.let { "› $it" } ?: "› SPOTIFY PLAYS THE BEST MATCH", Modifier.weight(1f),
                color = if (sent != null) accent else Hud.Faint, size = 9.sp, spacing = 1.8.sp)
            Spacer(Modifier.width(8.dp))
            HudButton("PLAY", ::go, color = accent, enabled = q.isNotBlank(), textSize = 10.sp)
        }
    }
}

// ── up next ──────────────────────────────────────────────────────────────────

@Composable
private fun UpNext(items: List<MediaSession.QueueItem>, c: MediaController?, accent: Color) {
    Panel("UP NEXT", accent = accent, oled = true, badge = { Badge("${items.size}", accent) }) {
        items.forEachIndexed { i, item ->
            val d = item.description
            Row(Modifier.fillMaxWidth()
                .clickable { c?.transportControls?.skipToQueueItem(item.queueId) }
                .padding(vertical = 8.dp), verticalAlignment = Alignment.CenterVertically) {
                Tracked("%02d".format(i + 1), Modifier.width(30.dp), color = accent.copy(alpha = 0.7f), size = 10.sp)
                d.iconBitmap?.let {
                    Image(it.asImageBitmap(), null, Modifier.size(34.dp).border(1.dp, accent.copy(alpha = 0.2f)),
                        contentScale = ContentScale.Crop)
                    Spacer(Modifier.width(10.dp))
                }
                Column(Modifier.weight(1f)) {
                    Tracked(d.title?.toString() ?: "—", color = ink(accent), size = 11.sp, spacing = 1.2.sp, upper = false)
                    d.subtitle?.let { Tracked(it.toString(), color = ink(accent, 0.45f), size = 9.sp, spacing = 1.5.sp) }
                }
            }
            if (i < items.lastIndex) Box(Modifier.fillMaxWidth().height(1.dp).background(accent.copy(alpha = 0.06f)))
        }
    }
}

// ── lock screen ──────────────────────────────────────────────────────────────

@Composable
private fun LockScreenPanel(accent: Color) {
    val ctx = LocalContext.current
    var on by remember { mutableStateOf(LockPlayer.enabled(ctx)) }
    var allowed by remember { mutableStateOf(LockPlayer.canShow(ctx)) }
    val lifecycle = LocalLifecycleOwner.current.lifecycle
    DisposableEffect(lifecycle) {
        val observer = LifecycleEventObserver { _, e -> if (e == Lifecycle.Event.ON_RESUME) allowed = LockPlayer.canShow(ctx) }
        lifecycle.addObserver(observer)
        onDispose { lifecycle.removeObserver(observer) }
    }
    Panel("LOCK SCREEN", accent = accent, oled = true) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            Column(Modifier.weight(1f)) {
                Tracked("PLAYER ON WAKE", color = ink(accent), size = 10.sp, spacing = 2.sp)
                Spacer(Modifier.height(3.dp))
                Tracked(if (!allowed) "NEEDS FULL-SCREEN PERMISSION" else if (on) "OPENS WHEN MUSIC PLAYS" else "OFF",
                    color = if (!allowed) Hud.Amber else ink(accent, 0.45f), size = 8.sp, spacing = 1.6.sp)
            }
            if (allowed) HudToggle(on, { on = it; LockPlayer.setEnabled(ctx, it) }, accent)
            else HudButton("GRANT", {
                ctx.startActivity(Intent(Settings.ACTION_MANAGE_APP_USE_FULL_SCREEN_INTENT,
                    Uri.parse("package:${ctx.packageName}")))
            }, color = Hud.Amber, textSize = 10.sp)
        }
    }
}

// ── no access ────────────────────────────────────────────────────────────────

@Composable
private fun NoAccess(ctx: Context) {
    Panel("SPOTIFY", accent = Hud.Amber, oled = true, badge = { Badge("LOCKED", Hud.Amber, live = true) }) {
        Text("NORA reads what's playing through notification access, the same way the lock screen's " +
            "player does. Nothing leaves the phone.", color = Hud.Dim, fontFamily = Hud.Mono,
            fontSize = 12.sp, lineHeight = 17.sp)
        Spacer(Modifier.height(12.dp))
        HudButton("GRANT ACCESS", {
            ctx.startActivity(Intent(Settings.ACTION_NOTIFICATION_LISTENER_DETAIL_SETTINGS)
                .putExtra(Settings.EXTRA_NOTIFICATION_LISTENER_COMPONENT_NAME,
                    NoraNotificationListener.component(ctx).flattenToString()))
        }, Modifier.fillMaxWidth(), color = Hud.Amber, strong = true)
    }
}
