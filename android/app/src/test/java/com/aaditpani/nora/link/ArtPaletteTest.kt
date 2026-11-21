package com.aaditpani.nora.link

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class ArtPaletteTest {
    private fun hueOf(argb: Int): Float = FloatArray(3).also { ArtPalette.toHsv(argb, it) }[0]
    private fun cover(vararg parts: Pair<Int, Int>): IntArray =
        parts.flatMap { (colour, n) -> List(n) { colour } }.toIntArray()

    private val red = 0xFFD02020.toInt()
    private val darkRed = 0xFF801010.toInt()
    private val blue = 0xFF2050D0.toInt()
    private val yellow = 0xFFE0C020.toInt()
    private val grey = 0xFF808080.toInt()
    private val black = 0xFF050505.toInt()

    @Test
    fun `the biggest colour comes first, then the others`() {
        val p = ArtPalette.of(cover(blue to 500, red to 300, yellow to 150, black to 74))!!
        assertEquals(hueOf(blue), hueOf(p.primary), 3f)
        assertEquals(hueOf(red), hueOf(p.secondary!!), 3f)
        assertEquals(hueOf(yellow), hueOf(p.tertiary!!), 3f)
    }

    @Test
    fun `shades of one hue are one colour, not three`() {
        val p = ArtPalette.of(cover(red to 400, darkRed to 400, blue to 200))!!
        assertEquals(hueOf(red), hueOf(p.primary), 5f)
        assertEquals(hueOf(blue), hueOf(p.secondary!!), 3f)
        assertNull(p.tertiary)
    }

    @Test
    fun `a single-colour cover gives one colour`() {
        val p = ArtPalette.of(cover(blue to 800, black to 224))!!
        assertNull(p.secondary)
    }

    @Test
    fun `a speck is not a colour`() {
        val p = ArtPalette.of(cover(blue to 990, red to 10))!!
        assertNull(p.secondary)
    }

    @Test
    fun `grey and black covers have nothing to give`() {
        assertNull(ArtPalette.of(cover(grey to 600, black to 424)))
        assertNull(ArtPalette.of(IntArray(0)))
    }

    @Test
    fun `colours are lifted to full brightness for a black screen`() {
        val p = ArtPalette.of(cover(darkRed to 1000))!!
        val hsv = FloatArray(3).also { ArtPalette.toHsv(p.primary, it) }
        assertEquals(1f, hsv[2], 0.01f)
        assertTrue(hsv[1] in 0.49f..0.86f)
    }

    @Test
    fun `hue distance wraps around the wheel`() {
        assertEquals(20f, ArtPalette.hueDistance(350f, 10f), 0.01f)
        assertNotNull(ArtPalette.hsvToArgb(359f, 1f, 1f))
    }
}
