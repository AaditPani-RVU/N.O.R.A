package com.aaditpani.nora.ui

import androidx.compose.animation.animateColorAsState
import androidx.compose.animation.core.tween
import androidx.compose.foundation.Canvas
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableFloatStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberUpdatedState
import androidx.compose.runtime.withFrameNanos
import androidx.compose.ui.Modifier
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.Path
import androidx.compose.ui.graphics.PathEffect
import androidx.compose.ui.graphics.drawscope.DrawScope
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.graphics.drawscope.rotate
import androidx.compose.ui.unit.Dp
import androidx.compose.foundation.layout.size
import kotlin.math.PI
import kotlin.math.cos
import kotlin.math.sin

/** What the orb shows, as on the dashboard, plus the phone's own "no link". */
enum class OrbState(val color: Color, val label: String, val speed: Float, val pulseMs: Float, val swell: Float) {
    IDLE(Hud.Cyan, "STANDBY", 1f, 3200f, 0.18f),
    LISTENING(Color(0xFF2ACDFF), "LISTENING", 1.4f, 1800f, 0.22f),
    THINKING(Hud.Violet, "PROCESSING", 2.25f, 1200f, 0.10f),
    SPEAKING(Hud.Teal, "RESPONDING", 1.8f, 900f, 0.28f),
    OFFLINE(Color(0xFF3E6378), "LINK DOWN", 0.35f, 4200f, 0.06f),
}

/**
 * The dashboard's orb (`.r1`–`.r5`, `.tri-ring`, `.orb-core`, the canvas
 * sweep), drawn in one Canvas. Designed at 340 units and scaled to [size].
 * Ring speed and colour follow [state]; speeds change by integrating a phase,
 * so a state change never makes the rings jump.
 */
@Composable
fun Orb(state: OrbState, size: Dp, modifier: Modifier = Modifier) {
    val color by animateColorAsState(state.color, tween(700), label = "orb")
    val current by rememberUpdatedState(state)
    val spin = remember { mutableFloatStateOf(0f) }     // seconds of ring travel at speed 1
    val pulse = remember { mutableFloatStateOf(0f) }    // 0..1 through one breath
    LaunchedEffect(Unit) {
        var last = 0L
        while (true) {
            withFrameNanos { now ->
                if (last != 0L) {
                    val dt = (now - last) / 1e9f
                    spin.floatValue += dt * current.speed
                    pulse.floatValue = (pulse.floatValue + dt * 1000f / current.pulseMs) % 1f
                }
                last = now
            }
        }
    }
    Canvas(modifier.size(size)) {
        val u = this.size.minDimension / 340f
        val t = spin.floatValue
        val c = center
        fun deg(period: Float, cw: Boolean = true) = (t / period * 360f % 360f) * if (cw) 1 else -1

        ring(c, 169 * u, color.copy(alpha = 0.10f), 1f, deg(55f), dashed = true)
        ticks(c, 150 * u, color, u, deg(55f))
        arcs(c, 148 * u, deg(9f), listOf(-90f to color.copy(alpha = 0.55f), 0f to color.copy(alpha = 0.35f)), 1.5f * u)
        rotate(deg(14f, cw = false), c) {
            drawArc(color.copy(alpha = 0.18f), 90f, 270f, false, Offset(c.x - 126 * u, c.y - 126 * u),
                Size(252 * u, 252 * u), style = Stroke(1f, pathEffect = PathEffect.dashPathEffect(floatArrayOf(5f * u, 5f * u))))
        }
        arcs(c, 103 * u, deg(6f, cw = false), listOf(-90f to color.copy(alpha = 0.5f), 180f to color.copy(alpha = 0.3f)), 1f)
        ring(c, 79 * u, color.copy(alpha = 0.24f), 1f, deg(8f), dashed = true)

        // The sweep and the triangle from the dashboard's canvas orb.
        rotate(deg(4f), c) {
            drawArc(Brush.sweepGradient(0f to Color.Transparent, 0.11f to color.copy(alpha = 0.22f),
                0.111f to Color.Transparent, center = c), -40f, 40f, true,
                Offset(c.x - 96 * u, c.y - 96 * u), Size(192 * u, 192 * u))
        }
        triangle(c, 62 * u, deg(20f, cw = false), color.copy(alpha = 0.45f), u)

        // Three satellites on the r2 track.
        rotate(deg(9f), c) {
            for (a in listOf(-120f, 20f, 140f)) {
                val r = (a * PI / 180).toFloat()
                val p = Offset(c.x + cos(r) * 148 * u, c.y + sin(r) * 148 * u)
                drawCircle(color.copy(alpha = 0.25f), 7 * u, p)
                drawCircle(Color.White.copy(alpha = 0.9f), 2.5f * u, p)
                drawCircle(color, 3.5f * u, p, alpha = 0.8f)
            }
        }

        // The core breathes: brighter, larger, and a wider halo at the peak.
        val b = ((1 - cos(pulse.floatValue * 2 * PI)) / 2).toFloat()
        val core = 18 * u * (1 + state.swell * b)
        drawCircle(Brush.radialGradient(listOf(color.copy(alpha = 0.30f + 0.15f * b), Color.Transparent), c,
            core * (3.2f + 1.6f * b)), core * (3.2f + 1.6f * b), c)
        drawCircle(Brush.radialGradient(listOf(Color.White, lerpWhite(color), color.copy(alpha = 0f)), c, core), core, c)
    }
}

private fun lerpWhite(c: Color) = Color(0.55f + c.red * 0.45f, 0.55f + c.green * 0.45f, 0.55f + c.blue * 0.45f, 0.95f)

private fun DrawScope.ring(c: Offset, r: Float, color: Color, w: Float, angle: Float, dashed: Boolean) {
    rotate(angle, c) {
        drawCircle(color, r, c, style = Stroke(w,
            pathEffect = if (dashed) PathEffect.dashPathEffect(floatArrayOf(r * 0.035f, r * 0.045f)) else null))
    }
}

/** Two quarter arcs of one ring, lit differently: `border-top-color` / `border-right-color`. */
private fun DrawScope.arcs(c: Offset, r: Float, angle: Float, parts: List<Pair<Float, Color>>, w: Float) {
    rotate(angle, c) {
        for ((start, color) in parts) {
            drawArc(color, start - 45f, 90f, false, Offset(c.x - r, c.y - r), Size(2 * r, 2 * r), style = Stroke(w))
        }
    }
}

private fun DrawScope.ticks(c: Offset, r: Float, color: Color, u: Float, angle: Float) {
    rotate(angle, c) {
        for (i in 0 until 72) {
            val a = (i * 5 * PI / 180).toFloat()
            val long = i % 6 == 0
            val len = (if (long) 7f else 3.5f) * u
            drawLine(color.copy(alpha = if (long) 0.35f else 0.16f),
                Offset(c.x + cos(a) * r, c.y + sin(a) * r),
                Offset(c.x + cos(a) * (r - len), c.y + sin(a) * (r - len)), strokeWidth = 1f)
        }
    }
}

private fun DrawScope.triangle(c: Offset, r: Float, angle: Float, color: Color, u: Float) {
    val path = Path().apply {
        for (i in 0..2) {
            val a = (angle + i * 120f - 90f) * PI.toFloat() / 180f
            val p = Offset(c.x + cos(a) * r, c.y + sin(a) * r)
            if (i == 0) moveTo(p.x, p.y) else lineTo(p.x, p.y)
        }
        close()
    }
    drawPath(path, color, style = Stroke(1.2f * u))
    drawPath(path, Brush.radialGradient(listOf(color.copy(alpha = 0.12f), Color.Transparent), c, r))
}
