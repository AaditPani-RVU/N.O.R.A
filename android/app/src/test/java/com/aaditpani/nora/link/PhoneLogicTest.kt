package com.aaditpani.nora.link

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class PhoneLogicTest {
    private val apps = listOf(
        AppEntry("Spotify", "com.spotify.music"),
        AppEntry("Spotify Lite", "com.spotify.lite"),
        AppEntry("WhatsApp", "com.whatsapp"),
        AppEntry("WhatsApp Business", "com.whatsapp.w4b"),
        AppEntry("Maps", "com.google.android.apps.maps"),
        AppEntry("Google", "com.google.android.googlequicksearchbox"),
        AppEntry("YouTube Music", "com.google.android.apps.youtube.music"),
        AppEntry("YouTube", "com.google.android.youtube"),
        AppEntry("Camera", "com.google.android.GoogleCamera"),
    )

    private fun pick(spoken: String) = AppMatch.best(spoken, apps)?.packageName

    @Test
    fun appNamesResolveToTheObviousApp() {
        assertEquals("com.spotify.music", pick("Spotify"))
        assertEquals("com.spotify.music", pick("the spotify app"))
        assertEquals("com.whatsapp", pick("whatsapp"))
        assertEquals("com.whatsapp", pick("Whats App"))
        assertEquals("com.google.android.apps.maps", pick("maps"))
        assertEquals("com.google.android.youtube", pick("youtube"))
        assertEquals("com.google.android.apps.youtube.music", pick("youtube music"))
        assertEquals("com.google.android.GoogleCamera", pick("camera"))
    }

    @Test
    fun noGuessForAnAppThatIsNotThere() {
        assertNull(pick("instagram"))
        assertNull(pick(""))
        assertNull(pick("go"))    // too short to be a prefix of anything
    }

    @Test
    fun onlyPlainWebLinksOpen() {
        assertEquals("https://example.com/a?b=1", UrlPolicy.normalise("https://example.com/a?b=1"))
        assertEquals("https://github.com/aadit", UrlPolicy.normalise("github.com/aadit"))
        assertEquals("http://example.com", UrlPolicy.normalise("http://example.com"))
        for (bad in listOf("javascript:alert(1)", "intent://scan/#Intent;scheme=zxing;end",
                "file:///sdcard/secret", "content://contacts/people", "tel:123", "https://",
                "https://google.com@evil.example/login", "https://exa mple.com", "localhostonly",
                "ftp://example.com", "https://" + "a".repeat(2100) + ".com")) {
            assertNull(bad, UrlPolicy.normalise(bad))
        }
    }

    @Test
    fun directionsUrlEncodesTheDestination() {
        val url = Directions.url("12.923400,77.499700", "walking")
        assertEquals("https://www.google.com/maps/dir/?api=1&destination=12.923400%2C77.499700" +
            "&travelmode=walking&dir_action=navigate", url)
        assertTrue(Directions.url("Phoenix Marketcity & more", "rocket").contains("travelmode=driving"))
        assertTrue(Directions.url("a&b", "driving").contains("destination=a%26b&"))
    }

    @Test
    fun mediaKindsMapToSearchFocus() {
        assertEquals("vnd.android.cursor.item/playlist", MediaFocus.of("playlist"))
        assertNull(MediaFocus.of("any"))
        assertEquals("com.spotify.music", MediaFocus.packageFor("Spotify"))
        assertNull(MediaFocus.packageFor("winamp"))
    }

    @Test
    fun onlyPlainSpotifyUrisArePlayed() {
        assertTrue(SpotifyUri.isValid("spotify:playlist:37i9dQZF1DX76Wlfdnj7AP"))
        assertTrue(SpotifyUri.isValid("spotify:track:0VjIjW4GlUZAMYd2vXMi3b"))
        assertFalse(SpotifyUri.isValid("spotify:playlist:37i9dQZF1DX76Wlfdnj7AP:play"))
        assertFalse(SpotifyUri.isValid("spotify:user:someone:collection"))
        assertFalse(SpotifyUri.isValid("https://open.spotify.com/playlist/37i9dQZF1DX76Wlfdnj7AP"))
        assertFalse(SpotifyUri.isValid("intent://evil#Intent;end"))
        assertEquals("spotify:track:0VjIjW4GlUZAMYd2vXMi3b:play",
            SpotifyUri.autoplay("spotify:track:0VjIjW4GlUZAMYd2vXMi3b"))
    }

    @Test
    fun oldTrackResumingIsNotSuccess() {
        // What the Pixel did: nothing switched, and the old song was playing.
        val w = PlayWatch("spotify:track:bruno", "Risk It All by Bruno Mars", "spotify:track:deftones")
        assertEquals(PlayWatch.Next.Wait, w.step(300, "spotify:track:bruno", "Risk It All by Bruno Mars", true))
        assertEquals(PlayWatch.Next.Resend, w.step(4000, "spotify:track:bruno", "Risk It All by Bruno Mars", true))
        assertEquals(PlayWatch.Next.Wait, w.step(4300, "spotify:track:bruno", "Risk It All by Bruno Mars", true))
        assertEquals(PlayWatch.Next.GiveUp(false),
            w.step(10000, "spotify:track:bruno", "Risk It All by Bruno Mars", true))
    }

    @Test
    fun playIsPressedOnlyOnceTheNewTrackIsIn() {
        val w = PlayWatch("spotify:track:bruno", "Risk It All by Bruno Mars", "spotify:track:deftones")
        assertEquals(PlayWatch.Next.Wait, w.step(300, "spotify:track:bruno", "Risk It All by Bruno Mars", false))
        assertEquals(PlayWatch.Next.Wait, w.step(2000, "spotify:track:bruno", "Risk It All by Bruno Mars", false))
        assertEquals(PlayWatch.Next.Wait, w.step(2300, "spotify:track:deftones", "Risk by Deftones", false))
        assertEquals(PlayWatch.Next.PressPlay, w.step(2900, "spotify:track:deftones", "Risk by Deftones", false))
        assertEquals(PlayWatch.Next.Wait, w.step(3200, "spotify:track:deftones", "Risk by Deftones", false))
        assertEquals(PlayWatch.Next.Done("Risk by Deftones"),
            w.step(3500, "spotify:track:deftones", "Risk by Deftones", true))
    }

    @Test
    fun aPlaylistCountsAsSwitchedWhenTheItemChanges() {
        val w = PlayWatch("spotify:track:bruno", "Risk It All by Bruno Mars", null)
        assertEquals(PlayWatch.Next.Done("Comfortably Numb by Pink Floyd"),
            w.step(1200, "spotify:track:floyd", "Comfortably Numb by Pink Floyd", true))
        // No media ids at all: fall back to the title.
        val t = PlayWatch(null, "Risk It All by Bruno Mars", null)
        assertFalse(t.switched(null, "Risk It All by Bruno Mars"))
        assertTrue(t.switched(null, "Money by Pink Floyd"))
    }

    @Test
    fun volumeRoundTrips() {
        assertEquals(6, VolumeMath.toIndex(40, 15))
        assertEquals(0, VolumeMath.toIndex(-5, 15))
        assertEquals(15, VolumeMath.toIndex(250, 15))
        assertEquals(40, VolumeMath.toPercent(6, 15))
        assertEquals(0, VolumeMath.toPercent(3, 0))
    }

    private val now = 10_000_000_000L
    private fun note(app: String, title: String, text: String, minsAgo: Long, key: String = "$app$title$text$minsAgo") =
        Note(key, "pkg.${app.lowercase()}", app, title, text, now - minsAgo * 60_000)

    @Test
    fun notificationsNewestFirstWithRepeatsCollapsed() {
        val notes = listOf(
            note("WhatsApp", "Mom", "Dinner at 8?", 30),
            note("WhatsApp", "Mom", "Dinner at 8?", 5),          // re-posted thread
            note("Gmail", "GitHub", "Build passed", 10),
            note("Swiggy", "Offer", "50% off", 60 * 30),          // older than a day
        )
        val picked = NoteDigest.select(notes, now, 24 * 60 * 60_000L, null, 10)
        assertEquals(listOf("WhatsApp", "Gmail"), picked.map { it.app })
        assertEquals(now - 5 * 60_000, picked[0].postedAt)

        val onlyMail = NoteDigest.select(notes, now, 24 * 60 * 60_000L, "gmail", 10)
        assertEquals(listOf("Gmail"), onlyMail.map { it.app })
        assertEquals(1, NoteDigest.select(notes, now, 24 * 60 * 60_000L, null, 1).size)
    }

    @Test
    fun notificationListingReadsNaturally() {
        val msg = NoteDigest.message(listOf(note("WhatsApp", "Mom", "Dinner\n at 8?", 5),
            note("Gmail", "", "Build passed", 120)), now, null)
        assertEquals("2 notifications. WhatsApp, Mom: Dinner at 8? (5m ago) · Gmail: Build passed (2h ago)", msg)
        assertEquals("No notifications on the phone right now.", NoteDigest.message(emptyList(), now, null))
        assertEquals("No notifications from Slack.", NoteDigest.message(emptyList(), now, "Slack"))
        assertEquals("ab…", NoteDigest.clip("abcdef", 3))
    }

    @Test
    fun untrustedOutputIsAdvertised() {
        val cap = object : Capability {
            override val name = "t.read"
            override val description = "reads"
            override val tier = 0
            override val untrustedOutput = true
            override val paramsSchema = JSONObject("""{"type":"object","properties":{}}""")
            override suspend fun execute(params: JSONObject) = CapabilityResult.Ok(JSONObject())
        }
        assertTrue(cap.manifestEntry().getBoolean("untrusted_output"))
        val plain = object : Capability {
            override val name = "t.plain"
            override val description = "plain"
            override val tier = 0
            override val paramsSchema = JSONObject("""{"type":"object","properties":{}}""")
            override suspend fun execute(params: JSONObject) = CapabilityResult.Ok(JSONObject())
        }
        assertFalse(plain.manifestEntry().getBoolean("untrusted_output"))
    }
}

class ContactPickTest {
    private val book = listOf(
        ContactNumber(1, "Mom", "+91 98000 00001", mobile = true),
        ContactNumber(1, "Mom", "080 2000 0001"),
        ContactNumber(2, "Mohan Kumar", "+91 98000 00002", mobile = true),
        ContactNumber(3, "Rahul Sharma", "+91 98000 00003", primary = true),
        ContactNumber(3, "Rahul Sharma", "+91 98000 00004", mobile = true),
        ContactNumber(4, "Rahul Verma", "+91 98000 00005", mobile = true),
    )

    @Test fun exactNameWinsOverAPrefix() {
        assertEquals(ContactPick.Result.One("Mom", "+91 98000 00001"), ContactPick.pick("mom", book))
    }

    @Test fun twoPeopleIsAQuestionNotAGuess() {
        assertEquals(ContactPick.Result.Several(listOf("Rahul Sharma", "Rahul Verma")), ContactPick.pick("Rahul", book))
    }

    @Test fun theDefaultNumberBeatsTheMobile() {
        assertEquals(ContactPick.Result.One("Rahul Sharma", "+91 98000 00003"), ContactPick.pick("rahul sharma", book))
    }

    @Test fun nobody() {
        assertEquals(ContactPick.Result.None, ContactPick.pick("Priya", book))
        assertEquals(ContactPick.Result.None, ContactPick.pick("  ", book))
    }

    @Test fun numbers() {
        assertTrue(ContactPick.isNumber("+91 98000-00001"))
        assertFalse(ContactPick.isNumber("mom"))
        assertFalse(ContactPick.isNumber("()"))
    }
}

class MissedCallsTest {
    private val now = 10_000_000_000L
    private val dialer = "com.google.android.dialer"
    private fun call(title: String, text: String, minsAgo: Long, key: String = "$title$minsAgo",
                     category: String = MissedCalls.CATEGORY, pkg: String = dialer) =
        Note(key, pkg, "Phone", title, text, now - minsAgo * 60_000, category)

    @Test
    fun onePerCallerNewestFirst() {
        val notes = listOf(
            call("Mom", "Missed call", 40) to true,
            call("Mom", "Missed call", 25) to true,
            call("+91 98450 12345", "Missed call", 10) to false,
            Note("w", "com.whatsapp", "WhatsApp", "Mom", "Missed voice call", now - 5 * 60_000) to true,
        )
        val calls = MissedCalls.select(notes, now, 24 * 3_600_000L, includeSeen = true)
        assertEquals(listOf("+91 98450 12345", "Mom"), calls.map { it.caller })
        assertEquals(2, calls[1].count)
        assertEquals(now - 40 * 60_000, calls[1].firstAt)
        assertEquals(now - 25 * 60_000, calls[1].lastAt)
        assertFalse(calls[0].onScreen)

        val unseen = MissedCalls.select(notes, now, 24 * 3_600_000L, includeSeen = false)
        assertEquals(listOf("Mom"), unseen.map { it.caller })
        assertEquals(1, MissedCalls.select(notes, now, 15 * 60_000L, includeSeen = true).size)
    }

    @Test
    fun readsEitherLayoutAndCounts() {
        assertEquals("Dad" to 1, MissedCalls.read(call("Missed call", "Dad", 1)))
        assertEquals("Dad" to 3, MissedCalls.read(call("3 missed calls", "Dad", 1)))
        assertEquals("Priya" to 1, MissedCalls.read(call("Priya", "Missed work call", 1)))
        assertEquals("An unknown caller" to 1, MissedCalls.read(call("", "Missed call", 1)))
        // No category, but the dialer's own words.
        assertTrue(MissedCalls.isMissedCall(call("Dad", "Missed call", 1, category = "")))
        assertFalse(MissedCalls.isMissedCall(call("Dad", "Call me back", 1, category = "")))
        assertFalse(MissedCalls.isMissedCall(call("Dad", "Missed call", 1, category = "", pkg = "com.whatsapp")))
    }

    @Test
    fun messageReadsNaturally() {
        val calls = listOf(MissedCall("Mom", 2, now - 40 * 60_000, now - 25 * 60_000, true),
            MissedCall("Dad", 1, now - 120 * 60_000, now - 120 * 60_000, false))
        assertEquals("Mom called 2 times (25m ago). Dad called (2h ago).", MissedCalls.message(calls, now, 0))
        assertEquals("No missed calls on the phone in the last 12 hours.",
            MissedCalls.message(emptyList(), now, 12 * 3_600_000L))
    }
}
