package com.aaditpani.nora.ui

import androidx.compose.animation.core.Animatable
import androidx.compose.animation.core.CubicBezierEasing
import androidx.compose.animation.core.LinearEasing
import androidx.compose.animation.core.RepeatMode
import androidx.compose.animation.animateColorAsState
import androidx.compose.animation.core.animateFloat
import androidx.compose.animation.core.animateFloatAsState
import androidx.compose.animation.core.infiniteRepeatable
import androidx.compose.animation.core.rememberInfiniteTransition
import androidx.compose.animation.core.tween
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.interaction.MutableInteractionSource
import androidx.compose.foundation.interaction.collectIsPressedAsState
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.BoxScope
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ColumnScope
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.BasicTextField
import androidx.compose.foundation.text.KeyboardActions
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.remember
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.drawBehind
import androidx.compose.ui.draw.drawWithContent
import androidx.compose.ui.draw.rotate
import androidx.compose.ui.geometry.CornerRadius
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.Shadow
import androidx.compose.ui.graphics.SolidColor
import androidx.compose.ui.graphics.drawscope.DrawScope
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.graphics.graphicsLayer
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.Font
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.TextUnit
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.compose.material3.Text
import com.aaditpani.nora.R

/**
 * The dashboard's visual language (`nora/static/index.html`), in Compose.
 *
 * What makes it NORA rather than a stock dark theme: one accent that retints
 * with state (cyan idle, violet thinking, teal speaking, amber when something
 * needs you); Rajdhani for names and numbers and Share Tech Mono for
 * everything else, small and widely tracked; glass panels with a bright
 * top-left corner bracket and a dim bottom-right one; a diamond before every
 * title; a hairline of light sweeping along an active panel; and a drifting
 * grid, scanlines and a vignette under all of it.
 */
object Hud {
    val Cyan = Color(0xFF00D4FF)
    val Light = Color(0xFF8ED8FF)
    val Violet = Color(0xFFC084FC)
    val Teal = Color(0xFF00FFC8)
    val Amber = Color(0xFFFFAA00)
    val Green = Color(0xFF00FF88)
    val Red = Color(0xFFFF4460)
    val Bg = Color(0xFF00070E)
    val Txt = Light.copy(alpha = 0.92f)
    val Dim = Light.copy(alpha = 0.55f)
    val Faint = Light.copy(alpha = 0.38f)

    val Disp = FontFamily(
        Font(R.font.rajdhani_regular, FontWeight.Normal),
        Font(R.font.rajdhani_medium, FontWeight.Medium),
        Font(R.font.rajdhani_semibold, FontWeight.SemiBold),
        Font(R.font.rajdhani_bold, FontWeight.Bold),
    )
    val Mono = FontFamily(Font(R.font.share_tech_mono))

    /** The dashboard's --ease-out and --ease-spring. */
    val EaseOut = CubicBezierEasing(0f, 0f, 0.2f, 1f)
    val Ease = CubicBezierEasing(0.25f, 0.46f, 0.45f, 0.94f)

    fun glow(c: Color, r: Float = 14f) = Shadow(c.copy(alpha = 0.45f), Offset.Zero, r)
}

// ── type ─────────────────────────────────────────────────────────────────────

/** Small, uppercase, widely tracked mono: every label on the dashboard. */
@Composable
fun Tracked(
    text: String,
    modifier: Modifier = Modifier,
    color: Color = Hud.Dim,
    size: TextUnit = 10.sp,
    spacing: TextUnit = 2.sp,
    glow: Boolean = false,
    font: FontFamily = Hud.Mono,
    weight: FontWeight = FontWeight.Normal,
    align: TextAlign? = null,
    upper: Boolean = true,
) {
    Text(if (upper) text.uppercase() else text, modifier, color = color, fontSize = size, letterSpacing = spacing,
        fontFamily = font, fontWeight = weight, maxLines = 1, textAlign = align,
        style = TextStyle(shadow = if (glow) Hud.glow(color) else null))
}

/** "N . O . R . A", shimmering like the header logotype. */
@Composable
fun Logotype(size: TextUnit = 20.sp, spacing: TextUnit = 8.sp, accent: Color = Hud.Cyan) {
    val t = rememberInfiniteTransition(label = "logo")
    val x by t.animateFloat(1.2f, -0.4f, infiniteRepeatable(tween(7500, easing = Hud.Ease)), label = "shimmer")
    Text("N.O.R.A", fontFamily = Hud.Disp, fontWeight = FontWeight.Bold, fontSize = size, letterSpacing = spacing,
        style = TextStyle(
            brush = Brush.linearGradient(
                0f to accent.copy(alpha = 0.85f), 0.4f to Color(0xFFD8F8FF), 0.5f to Color.White,
                0.6f to Color(0xFFD8F8FF), 1f to accent.copy(alpha = 0.85f),
                start = Offset(x * 900f - 450f, 0f), end = Offset(x * 900f + 450f, 0f)),
            shadow = Shadow(accent.copy(alpha = 0.55f), Offset.Zero, 22f)))
}

// ── glyphs ───────────────────────────────────────────────────────────────────

/** The diamond before a panel title, and NORA's avatar in the transcript. */
@Composable
fun Diamond(size: Dp = 9.dp, color: Color = Hud.Cyan, filled: Boolean = false) {
    Box(Modifier.size(size).rotate(45f)
        .then(if (filled) Modifier.background(color.copy(alpha = 0.16f)) else Modifier)
        .border(1.dp, color))
}

/** A status dot that breathes, as in the header pills. */
@Composable
fun Dot(color: Color, blink: Boolean = true, size: Dp = 6.dp) {
    val t = rememberInfiniteTransition(label = "dot")
    val a by t.animateFloat(1f, 0.25f, infiniteRepeatable(tween(1200, easing = Hud.Ease), RepeatMode.Reverse), label = "a")
    Canvas(Modifier.size(size)) {
        val alpha = if (blink) a else 1f
        drawCircle(color.copy(alpha = 0.35f * alpha), radius = this.size.minDimension)
        drawCircle(color.copy(alpha = alpha))
    }
}

/** The header sigil: two arcs turning against each other round a blinking point. */
@Composable
fun Sigil(accent: Color, size: Dp = 22.dp) {
    val t = rememberInfiniteTransition(label = "sigil")
    val a by t.animateFloat(0f, 360f, infiniteRepeatable(tween(3400, easing = LinearEasing)), label = "a")
    val b by t.animateFloat(360f, 0f, infiniteRepeatable(tween(2200, easing = LinearEasing)), label = "b")
    val p by t.animateFloat(1f, 0.25f, infiniteRepeatable(tween(1300), RepeatMode.Reverse), label = "p")
    Canvas(Modifier.size(size)) {
        val s = this.size.minDimension
        drawArc(accent.copy(alpha = 0.8f), a - 45f, 90f, false, style = Stroke(1.dp.toPx()))
        val inset = 4.dp.toPx()
        drawArc(accent.copy(alpha = 0.45f), b + 135f, 90f, false, Offset(inset, inset),
            Size(s - 2 * inset, s - 2 * inset), style = Stroke(1.dp.toPx()))
        drawCircle(accent.copy(alpha = 0.35f * p), radius = 4.5.dp.toPx())
        drawCircle(accent.copy(alpha = p), radius = 2.5.dp.toPx())
    }
}

// ── surfaces ─────────────────────────────────────────────────────────────────

/** Top-left bright, bottom-right dim: the widget corners. All four makes a reticle. */
fun Modifier.brackets(color: Color, arm: Dp = 13.dp, all: Boolean = false, width: Dp = 1.5.dp) = drawWithContent {
    drawContent()
    val a = arm.toPx()
    val w = width.toPx()
    fun corner(x: Float, y: Float, dx: Float, dy: Float, c: Color) {
        drawRect(c, Offset(if (dx > 0) x else x - a, y - (if (dy > 0) 0f else w)), Size(a, w))
        drawRect(c, Offset(x - (if (dx > 0) 0f else w), if (dy > 0) y else y - a), Size(w, a))
    }
    corner(0f, 0f, 1f, 1f, color)
    corner(size.width, size.height, -1f, -1f, color.copy(alpha = if (all) color.alpha else 0.3f * color.alpha))
    if (all) {
        corner(size.width, 0f, -1f, 1f, color)
        corner(0f, size.height, 1f, -1f, color)
    }
}

/** Glass: a sheen at the top edge over a deep blue fall-off. */
fun Modifier.glass(accent: Color, border: Float = 0.15f) = this
    .drawBehind {
        drawRect(Brush.linearGradient(listOf(Color(0xB8021630), Color(0xE6000A18), Color(0xF2000712)),
            start = Offset.Zero, end = Offset(size.width * 0.4f, size.height)))
        drawRect(Brush.radialGradient(listOf(accent.copy(alpha = 0.07f), Color.Transparent),
            center = Offset(size.width / 2, -size.height * 0.25f), radius = size.width * 0.75f))
        drawLine(accent.copy(alpha = 0.10f), Offset(0f, 0.5f), Offset(size.width, 0.5f))
    }
    .border(1.dp, accent.copy(alpha = border), RoundedCornerShape(3.dp))

/** A light running along the bottom edge while the panel is live. */
@Composable
fun FlowLine(color: Color, modifier: Modifier = Modifier) {
    val t = rememberInfiniteTransition(label = "flow")
    val x by t.animateFloat(-0.25f, 1.25f, infiniteRepeatable(tween(3500, easing = LinearEasing)), label = "x")
    Canvas(modifier.fillMaxWidth().height(1.dp)) {
        val w = size.width * 0.22f
        val cx = x * size.width
        drawRect(Brush.horizontalGradient(listOf(Color.Transparent, color.copy(alpha = 0.6f), Color.Transparent),
            startX = cx - w / 2, endX = cx + w / 2), Offset(cx - w / 2, 0f), Size(w, size.height))
    }
}

/**
 * A dashboard widget: diamond, tracked title, badge on the right, a hairline
 * under the head, brackets, and the flow line when [live].
 */
@Composable
fun Panel(
    title: String,
    modifier: Modifier = Modifier,
    accent: Color = Hud.Cyan,
    live: Boolean = false,
    badge: (@Composable () -> Unit)? = null,
    content: @Composable ColumnScope.() -> Unit,
) {
    val border by animateFloatAsState(if (live) 0.32f else 0.15f, tween(700), label = "border")
    Box(modifier.fillMaxWidth().glass(accent, border).brackets(accent)) {
        Column(Modifier.padding(horizontal = 14.dp, vertical = 12.dp)) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Diamond(9.dp, accent)
                Spacer(Modifier.width(9.dp))
                Tracked(title, Modifier.weight(1f), color = accent.copy(alpha = 0.95f), size = 11.sp,
                    spacing = 4.sp, glow = true, font = Hud.Disp, weight = FontWeight.SemiBold)
                badge?.invoke()
            }
            Box(Modifier.padding(top = 8.dp, bottom = 10.dp).fillMaxWidth().height(1.dp).background(
                Brush.horizontalGradient(listOf(Color.Transparent, accent.copy(alpha = 0.17f),
                    accent.copy(alpha = 0.17f), Color.Transparent))))
            content()
        }
        if (live) FlowLine(accent, Modifier.align(Alignment.BottomCenter))
    }
}

/** A bordered tag: OFFLINE, RUNNING, TIER 2. [live] lights it. */
@Composable
fun Badge(text: String, color: Color = Hud.Cyan, live: Boolean = false) {
    Box(Modifier
        .then(if (live) Modifier.background(color.copy(alpha = 0.08f)) else Modifier)
        .border(1.dp, color.copy(alpha = if (live) 0.45f else 0.18f), RoundedCornerShape(2.dp))
        .padding(horizontal = 7.dp, vertical = 3.dp)) {
        Tracked(text, color = if (live) color else Hud.Dim, size = 9.sp, spacing = 2.sp)
    }
}

/** A header pill with a breathing dot: ONLINE, LINKING, OFFLINE. */
@Composable
fun Pill(text: String, dot: Color, modifier: Modifier = Modifier) {
    Row(modifier
        .background(Hud.Cyan.copy(alpha = 0.03f), RoundedCornerShape(3.dp))
        .border(1.dp, Hud.Cyan.copy(alpha = 0.15f), RoundedCornerShape(3.dp))
        .padding(horizontal = 8.dp, vertical = 4.dp),
        verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(6.dp)) {
        Dot(dot)
        Tracked(text, size = 9.sp)
    }
}

// ── controls ─────────────────────────────────────────────────────────────────

/** The square, tracked button of the command bar and the widgets. */
@Composable
fun HudButton(
    text: String,
    onClick: () -> Unit,
    modifier: Modifier = Modifier,
    color: Color = Hud.Cyan,
    enabled: Boolean = true,
    strong: Boolean = false,
    textSize: androidx.compose.ui.unit.TextUnit = 11.sp,
) {
    val src = remember { MutableInteractionSource() }
    val pressed by src.collectIsPressedAsState()
    val c = if (enabled) color else Hud.Faint
    val fill by animateFloatAsState(if (pressed) 0.18f else if (strong) 0.10f else 0.04f, tween(80), label = "fill")
    Box(modifier
        .graphicsLayer { scaleX = if (pressed) 0.98f else 1f; scaleY = if (pressed) 0.98f else 1f }
        .background(c.copy(alpha = fill), RoundedCornerShape(2.dp))
        .border(1.dp, c.copy(alpha = if (strong) 0.7f else 0.42f), RoundedCornerShape(2.dp))
        .clickable(src, null, enabled = enabled, onClick = onClick)
        .padding(horizontal = 14.dp, vertical = 11.dp),
        contentAlignment = Alignment.Center) {
        Tracked(text, color = c, size = textSize, spacing = 3.sp, glow = enabled && strong)
    }
}

/** A suggestion chip under the transcript. */
@Composable
fun Chip(text: String, onClick: () -> Unit, enabled: Boolean = true) {
    Box(Modifier
        .background(Hud.Cyan.copy(alpha = 0.03f), RoundedCornerShape(50))
        .border(1.dp, Hud.Cyan.copy(alpha = 0.15f), RoundedCornerShape(50))
        .clickable(enabled = enabled, onClick = onClick)
        .padding(horizontal = 12.dp, vertical = 7.dp)) {
        Tracked(text, color = if (enabled) Hud.Dim else Hud.Faint, size = 10.sp, spacing = 1.8.sp)
    }
}

/** A rectangular switch with a glowing block for a knob. */
@Composable
fun HudToggle(checked: Boolean, onChange: (Boolean) -> Unit, color: Color = Hud.Cyan) {
    val x by animateFloatAsState(if (checked) 1f else 0f, tween(180, easing = Hud.EaseOut), label = "knob")
    val c by animateColorAsState(if (checked) color else Hud.Faint, tween(180), label = "c")
    Canvas(Modifier.size(44.dp, 22.dp).clickable { onChange(!checked) }) {
        val r = CornerRadius(2.dp.toPx())
        drawRoundRect(c.copy(alpha = 0.10f), cornerRadius = r)
        drawRoundRect(c.copy(alpha = 0.55f), cornerRadius = r, style = Stroke(1.dp.toPx()))
        val k = size.height - 6.dp.toPx()
        val left = 3.dp.toPx() + x * (size.width - k - 6.dp.toPx())
        drawRoundRect(c.copy(alpha = 0.25f * x), Offset(left - 3.dp.toPx(), 0f), Size(k + 6.dp.toPx(), size.height), r)
        drawRoundRect(c, Offset(left, 3.dp.toPx()), Size(k, k), CornerRadius(1.dp.toPx()))
    }
}

/** A mono field with the dashboard's blinking block cursor in the placeholder. */
@Composable
fun HudField(
    value: String,
    onChange: (String) -> Unit,
    placeholder: String,
    modifier: Modifier = Modifier,
    singleLine: Boolean = false,
    onSend: (() -> Unit)? = null,
    enabled: Boolean = true,
) {
    val t = rememberInfiniteTransition(label = "caret")
    val blink by t.animateFloat(0f, 1f, infiniteRepeatable(tween(800, easing = LinearEasing)), label = "b")
    BasicTextField(value, onChange, modifier, enabled = enabled, singleLine = singleLine, maxLines = if (singleLine) 1 else 4,
        textStyle = TextStyle(color = Hud.Cyan, fontFamily = Hud.Mono, fontSize = 14.sp, letterSpacing = 0.5.sp),
        cursorBrush = SolidColor(Hud.Cyan),
        keyboardOptions = KeyboardOptions(imeAction = if (onSend != null) ImeAction.Send else ImeAction.Default),
        keyboardActions = KeyboardActions(onSend = { onSend?.invoke() }),
        decorationBox = { inner ->
            Box(Modifier
                .background(Hud.Cyan.copy(alpha = 0.04f), RoundedCornerShape(2.dp))
                .border(1.dp, Hud.Cyan.copy(alpha = if (value.isEmpty()) 0.15f else 0.45f), RoundedCornerShape(2.dp))
                .padding(horizontal = 12.dp, vertical = 12.dp)) {
                if (value.isEmpty()) {
                    Row {
                        Text(placeholder, color = Hud.Dim, fontFamily = Hud.Mono, fontSize = 13.sp, letterSpacing = 1.5.sp)
                        Text("_", color = Hud.Cyan.copy(alpha = if (blink < 0.5f) 0.9f else 0f),
                            fontFamily = Hud.Mono, fontSize = 13.sp)
                    }
                }
                inner()
            }
        })
}

/** A hairline that drains: time left to answer. */
@Composable
fun Countdown(fraction: Float, color: Color, modifier: Modifier = Modifier) {
    Canvas(modifier.fillMaxWidth().height(2.dp)) {
        drawRect(color.copy(alpha = 0.12f))
        drawRect(Brush.horizontalGradient(listOf(color.copy(alpha = 0.3f), color)),
            size = Size(size.width * fraction.coerceIn(0f, 1f), size.height))
    }
}

/** Seven bars breathing slowly: the transcript waiting for input. */
@Composable
fun Equalizer(color: Color, modifier: Modifier = Modifier) {
    val t = rememberInfiniteTransition(label = "eq")
    val phase by t.animateFloat(0f, 1f, infiniteRepeatable(tween(2400, easing = LinearEasing)), label = "p")
    Canvas(modifier.size(52.dp, 30.dp)) {
        val delays = floatArrayOf(0f, 140f, 280f, 420f, 280f, 140f, 0f)
        val bw = 4.dp.toPx()
        val gap = (size.width - bw * 7) / 6
        delays.forEachIndexed { i, d ->
            val p = ((phase - d / 2400f) % 1f + 1f) % 1f
            val s = 0.18f + 0.67f * ((1 - kotlin.math.cos(p * 2 * Math.PI).toFloat()) / 2)
            val h = size.height * s
            drawRoundRect(Brush.verticalGradient(listOf(color, color.copy(alpha = 0.18f)),
                startY = size.height - h, endY = size.height),
                Offset(i * (bw + gap), size.height - h), Size(bw, h), CornerRadius(2.dp.toPx()), alpha = 0.75f)
        }
    }
}

/** Something new sliding up into place, as the dashboard's turns and cards do. */
@Composable
fun Enter(animate: Boolean, delayMs: Int = 0, content: @Composable BoxScope.() -> Unit) {
    val p = remember { Animatable(if (animate) 0f else 1f) }
    LaunchedEffect(Unit) {
        if (animate) {
            kotlinx.coroutines.delay(delayMs.toLong())
            p.animateTo(1f, tween(420, easing = Hud.EaseOut))
        }
    }
    Box(Modifier.graphicsLayer { alpha = p.value; translationY = (1 - p.value) * 9.dp.toPx() }, content = content)
}

// ── the room ─────────────────────────────────────────────────────────────────

/**
 * The ground everything sits on: a deep radial fall-off, two slow aurora
 * blooms in the accent, a drifting 52 dp grid, then (over the content)
 * scanlines and a vignette.
 */
@Composable
fun HudBackground(accent: Color, content: @Composable BoxScope.() -> Unit) {
    val t = rememberInfiniteTransition(label = "room")
    val drift by t.animateFloat(0f, 1f, infiniteRepeatable(tween(28000, easing = LinearEasing)), label = "grid")
    val aur by t.animateFloat(0f, 1f, infiniteRepeatable(tween(19000, easing = Hud.Ease), RepeatMode.Reverse), label = "aurora")
    Box(Modifier.fillMaxSize()
        .drawBehind {
            drawRect(Brush.radialGradient(listOf(Color(0xFF04141F), Color(0xFF00090F), Color(0xFF000407)),
                center = Offset(size.width / 2, -size.height * 0.12f), radius = size.height * 1.1f))
            aurora(accent, aur)
            grid(accent, drift)
        }
        .drawWithContent {
            drawContent()
            scanlines()
            drawRect(Brush.radialGradient(listOf(Color.Transparent, Color.Transparent, Color(0x75000207)),
                center = Offset(size.width / 2, size.height * 0.46f), radius = size.maxDimension * 0.75f))
        }, content = content)
}

private fun DrawScope.aurora(accent: Color, p: Float) {
    val w = size.width
    val h = size.height
    fun bloom(cx: Float, cy: Float, r: Float, c: Color) = drawCircle(
        Brush.radialGradient(listOf(c, Color.Transparent), Offset(cx, cy), r), r, Offset(cx, cy))
    bloom(w * (0.15f + 0.08f * p), h * (0.14f - 0.03f * p), w * 0.75f, accent.copy(alpha = 0.13f))
    bloom(w * (0.9f - 0.06f * p), h * (0.82f + 0.03f * p), w * 0.8f, accent.copy(alpha = 0.09f))
    bloom(w * 0.62f, h * 0.04f, w * 0.55f, Color(0x1C6E3CFF))
}

private fun DrawScope.grid(accent: Color, drift: Float) {
    val step = 52.dp.toPx()
    val off = drift * step
    val c = accent.copy(alpha = 0.035f)
    var x = off - step
    while (x < size.width) { drawLine(c, Offset(x, 0f), Offset(x, size.height)); x += step }
    var y = off - step
    while (y < size.height) { drawLine(c, Offset(0f, y), Offset(size.width, y)); y += step }
}

private fun DrawScope.scanlines() {
    val c = Color(0x0B000000)
    var y = 2f
    while (y < size.height) { drawRect(c, Offset(0f, y), Size(size.width, 2f)); y += 4f }
}
