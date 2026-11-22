package com.aaditpani.nora

import com.aaditpani.nora.link.Note
import com.aaditpani.nora.link.ReplyPick
import com.aaditpani.nora.link.WaNumber
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class ReplyPickTest {
    private fun note(key: String, title: String, app: String = "WhatsApp", at: Long = 1L) =
        Note(key, "com.whatsapp", app, title, "hi", at, "msg")

    @Test fun oneConversationIsTheNewestOfItsNotifications() {
        val r = ReplyPick.pick("mom", "", listOf(note("a", "Mom", at = 1), note("b", "Mom (2 messages)", at = 5)))
        assertEquals("b", (r as ReplyPick.Result.One).note.key)
    }

    @Test fun anExactTitleBeatsALongerOne() {
        val r = ReplyPick.pick("Mom", "whatsapp", listOf(note("a", "Mom"), note("b", "Mom's book club")))
        assertEquals("a", (r as ReplyPick.Result.One).note.key)
    }

    @Test fun twoPeopleIsAQuestion() {
        val r = ReplyPick.pick("rahul", "", listOf(note("a", "Rahul K"), note("b", "Rahul S")))
        assertEquals(listOf("Rahul K", "Rahul S"), (r as ReplyPick.Result.Several).titles)
    }

    @Test fun theAppHasToMatchWhenNamed() {
        assertTrue(ReplyPick.pick("mom", "telegram", listOf(note("a", "Mom"))) is ReplyPick.Result.None)
    }

    @Test fun waNumbersGetTheirCountryCode() {
        assertEquals("919876543210", WaNumber.of("+91 98765 43210", "91"))
        assertEquals("919876543210", WaNumber.of("098765 43210", "91"))
        assertEquals("447700900123", WaNumber.of("0044 7700 900123", "91"))
        assertNull(WaNumber.of("98765 43210", null))
        assertNull(WaNumber.of("123", "91"))
    }
}
