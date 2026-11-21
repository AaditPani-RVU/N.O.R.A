package com.aaditpani.nora.link

import kotlin.math.abs
import kotlin.math.atan2
import kotlin.math.cos
import kotlin.math.max
import kotlin.math.min
import kotlin.math.sin

/**
 * The two or three colours a cover is made of, for theming the Player.
 *
 * Pixels are grouped into hue families; a family's weight is how much of the
 * cover it covers, scaled by how vivid it is, so a big wash of colour beats a
 * small bright speck but a dull brown ground doesn't beat the red on it.
 * Families closer than [MIN_APART] degrees count as one colour, so a red cover
 * gives red plus whatever else is there, not three reds.
 *
 * Every colour is lifted to full brightness: the Player is drawn on black,
 * where a dark colour reads as mud. Pure Kotlin on ARGB ints, so it runs in a
 * JVM test without a Bitmap.
 */
object ArtPalette {
    /** ARGB colours, most important first; [secondary] and [tertiary] may be absent. */
    data class Colours(val primary: Int, val secondary: Int?, val tertiary: Int?)

    private const val BINS = 24                 // 15 degrees each
    private const val MIN_SAT = 0.22f           // below this a pixel is grey
    private const val MIN_VAL = 0.22f           // below this it is black
    private const val MIN_SHARE = 0.04f         // of the colourful pixels, for a family to count
    const val MIN_APART = 35f

    fun of(pixels: IntArray): Colours? {
        val weight = FloatArray(BINS)
        val count = IntArray(BINS)
        val sx = FloatArray(BINS); val sy = FloatArray(BINS)   // hue as a vector, for a circular mean
        val sSum = FloatArray(BINS)
        var colourful = 0
        val hsv = FloatArray(3)
        for (p in pixels) {
            if ((p ushr 24) < 128) continue                     // transparent
            toHsv(p, hsv)
            if (hsv[1] < MIN_SAT || hsv[2] < MIN_VAL) continue
            val b = (hsv[0] / (360f / BINS)).toInt().coerceIn(0, BINS - 1)
            val w = hsv[1] * (0.35f + hsv[2])
            weight[b] += w; count[b]++; sSum[b] += hsv[1]
            val r = Math.toRadians(hsv[0].toDouble())
            sx[b] += (cos(r) * w).toFloat(); sy[b] += (sin(r) * w).toFloat()
            colourful++
        }
        if (colourful == 0) return null

        // A family is a bin with its two neighbours, so a hue on a bin edge isn't split in half.
        data class Family(val hue: Float, val sat: Float, val weight: Float, val share: Float)
        val families = (0 until BINS).mapNotNull { b ->
            if (count[b] == 0) return@mapNotNull null
            var w = 0f; var n = 0; var x = 0f; var y = 0f; var s = 0f
            for (d in -1..1) {
                val i = (b + d + BINS) % BINS
                w += weight[i]; n += count[i]; x += sx[i]; y += sy[i]; s += sSum[i]
            }
            val hue = ((Math.toDegrees(atan2(y, x).toDouble()).toFloat()) + 360f) % 360f
            Family(hue, s / n, w, n.toFloat() / colourful)
        }.filter { it.share >= MIN_SHARE }.sortedByDescending { it.weight }

        val picked = mutableListOf<Family>()
        for (f in families) {
            if (picked.size == 3) break
            if (picked.all { hueDistance(it.hue, f.hue) >= MIN_APART }) picked += f
        }
        if (picked.isEmpty()) return null
        val colours = picked.map { hsvToArgb(it.hue, it.sat.coerceIn(0.5f, 0.85f), 1f) }
        return Colours(colours[0], colours.getOrNull(1), colours.getOrNull(2))
    }

    fun hueDistance(a: Float, b: Float): Float {
        val d = abs(a - b) % 360f
        return if (d > 180f) 360f - d else d
    }

    fun toHsv(argb: Int, out: FloatArray) {
        val r = (argb shr 16 and 0xFF) / 255f
        val g = (argb shr 8 and 0xFF) / 255f
        val b = (argb and 0xFF) / 255f
        val mx = max(r, max(g, b)); val mn = min(r, min(g, b)); val d = mx - mn
        val h = when {
            d == 0f -> 0f
            mx == r -> 60f * (((g - b) / d) % 6f)
            mx == g -> 60f * ((b - r) / d + 2f)
            else -> 60f * ((r - g) / d + 4f)
        }
        out[0] = (h + 360f) % 360f
        out[1] = if (mx == 0f) 0f else d / mx
        out[2] = mx
    }

    fun hsvToArgb(h: Float, s: Float, v: Float): Int {
        val c = v * s
        val x = c * (1 - abs((h / 60f) % 2f - 1))
        val m = v - c
        val (r, g, b) = when ((h / 60f).toInt() % 6) {
            0 -> Triple(c, x, 0f); 1 -> Triple(x, c, 0f); 2 -> Triple(0f, c, x)
            3 -> Triple(0f, x, c); 4 -> Triple(x, 0f, c); else -> Triple(c, 0f, x)
        }
        fun ch(f: Float) = ((f + m) * 255f + 0.5f).toInt().coerceIn(0, 255)
        return (0xFF shl 24) or (ch(r) shl 16) or (ch(g) shl 8) or ch(b)
    }
}
