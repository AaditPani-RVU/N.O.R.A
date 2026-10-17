package com.aaditpani.nora.link

import com.aaditpani.nora.link.Screening.Action
import com.aaditpani.nora.link.Screening.Mode
import org.junit.Assert.assertEquals
import org.junit.Test

class ScreeningTest {
    private val blocked = setOf(Screening.key("+91 98450 12345"))

    @Test
    fun `one number however it is written`() {
        for (n in listOf("+91 98450 12345", "098450-12345", "9845012345", "+91 (98450) 12345")) {
            assertEquals(n, "9845012345", Screening.key(n))
        }
        assertEquals("", Screening.key("Unknown"))
    }

    @Test
    fun `a blocked number is rejected in either mode`() {
        for (mode in Mode.values()) {
            assertEquals(Action.REJECTED, Screening.decide("098450 12345", mode, blocked))
        }
    }

    @Test
    fun `everything else follows the mode`() {
        assertEquals(Action.RANG, Screening.decide("+91 80 2222 3333", Mode.RING, blocked))
        assertEquals(Action.SILENCED, Screening.decide("+91 80 2222 3333", Mode.SILENCE, blocked))
    }

    @Test
    fun `a withheld number can't be blocked, only silenced`() {
        assertEquals(Action.RANG, Screening.decide(null, Mode.RING, blocked))
        assertEquals(Action.SILENCED, Screening.decide(null, Mode.SILENCE, blocked))
    }

    @Test
    fun `an empty block list entry never matches a number without digits`() {
        assertEquals(Action.RANG, Screening.decide("Unknown", Mode.RING, setOf("")))
    }

    @Test
    fun `the mode survives the round trip and defaults to ringing`() {
        for (mode in Mode.values()) assertEquals(mode, Screening.modeOf(Screening.wire(mode)))
        assertEquals(Mode.RING, Screening.modeOf(null))
        assertEquals(Mode.RING, Screening.modeOf("something else"))
    }
}
