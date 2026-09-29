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
