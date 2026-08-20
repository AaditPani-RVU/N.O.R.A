"""Tests for the Spotify backend (mpris / spotify_api / commands.spotify).

Stdlib unittest only — run with:  python -m unittest tests.test_spotify -v

No network and no live D-Bus: the session bus and the Web API are both stubbed.
What's under test is the GVariant parsing, the metadata normalisation, the
search fall-through order and the command routing — not Spotify itself.
"""
from __future__ import annotations

import unittest
from unittest import mock

from nora import fast_path, spotify_api
from nora.commands import spotify as sp
from nora.platform.linux import mpris


# ── GVariant text parser ──────────────────────────────────────────────────────

class TestGVariantParser(unittest.TestCase):
    """The gdbus fallback parses GVariant literals by hand, so pin the shapes."""

    def test_scalar_in_tuple_is_unwrapped(self):
        self.assertEqual(mpris._gv_parse("(<'Playing'>,)"), "Playing")

    def test_booleans(self):
        self.assertIs(mpris._gv_parse("(<true>,)"), True)
        self.assertIs(mpris._gv_parse("(<false>,)"), False)

    def test_typed_numbers_drop_their_prefix(self):
        self.assertEqual(mpris._gv_parse("(<uint64 285906000>,)"), 285906000)
        self.assertEqual(mpris._gv_parse("(<int64 -42>,)"), -42)
        self.assertAlmostEqual(mpris._gv_parse("(<0.45>,)"), 0.45)

    def test_string_array(self):
        self.assertEqual(mpris._gv_parse("(<['Slowdive', 'Mojave 3']>,)"),
                         ["Slowdive", "Mojave 3"])

    def test_full_metadata_dict(self):
        raw = (
            "(<{'mpris:trackid': <'/com/spotify/track/0oxYB9GoOIDrdzniNdKC44'>, "
            "'mpris:length': <uint64 285906000>, "
            "'xesam:album': <'Souvlaki'>, "
            "'xesam:artist': <['Slowdive']>, "
            "'xesam:autoRating': <0.80000000000000004>, "
            "'xesam:title': <'When the Sun Hits'>, "
            "'xesam:trackNumber': <7>}>,)"
        )
        meta = mpris._gv_parse(raw)
        self.assertEqual(meta["xesam:title"], "When the Sun Hits")
        self.assertEqual(meta["xesam:artist"], ["Slowdive"])
        self.assertEqual(meta["mpris:length"], 285906000)
        self.assertEqual(meta["xesam:trackNumber"], 7)

    def test_escaped_quote_in_title(self):
        meta = mpris._gv_parse(r"(<{'xesam:title': <'Don\'t Stop'>}>,)")
        self.assertEqual(meta["xesam:title"], "Don't Stop")

    def test_empty_and_malformed_input_never_raise(self):
        for raw in ("", "()", "(<>,)", "{{{", "garbage"):
            mpris._gv_parse(raw)  # must not raise


# ── Metadata normalisation ────────────────────────────────────────────────────

class TestNowPlaying(unittest.TestCase):
    def _with_meta(self, meta, status="Playing"):
        with mock.patch.object(mpris, "metadata", return_value=meta), \
             mock.patch.object(mpris, "playback_status", return_value=status):
            return mpris.now_playing()

    def test_normalises_a_full_track(self):
        snap = self._with_meta({
            "mpris:trackid": "/com/spotify/track/0oxYB9GoOIDrdzniNdKC44",
            "mpris:length": 285906000,
            "xesam:album": "Souvlaki",
            "xesam:artist": ["Slowdive"],
            "xesam:title": "When the Sun Hits",
        })
        self.assertEqual(snap["title"], "When the Sun Hits")
        self.assertEqual(snap["artist"], "Slowdive")
        self.assertEqual(snap["album"], "Souvlaki")
        self.assertEqual(snap["uri"], "spotify:track:0oxYB9GoOIDrdzniNdKC44")
        self.assertAlmostEqual(snap["length_sec"], 285.906, places=2)

    def test_joins_multiple_artists(self):
        snap = self._with_meta({"xesam:artist": ["Jay-Z", "Kanye West"]})
        self.assertEqual(snap["artist"], "Jay-Z, Kanye West")

    def test_accepts_a_bare_string_artist(self):
        snap = self._with_meta({"xesam:artist": "Slowdive"})
        self.assertEqual(snap["artist"], "Slowdive")

    def test_empty_metadata_yields_blank_keys_not_errors(self):
        snap = self._with_meta({}, status="")
        for key in ("title", "artist", "album", "uri", "url", "art_url"):
            self.assertEqual(snap[key], "")
        self.assertEqual(snap["length_sec"], 0.0)

    def test_non_numeric_length_is_tolerated(self):
        self.assertEqual(self._with_meta({"mpris:length": "nonsense"})["length_sec"], 0.0)


class TestPlayerResolution(unittest.TestCase):
    def test_service_for_expands_a_short_name(self):
        self.assertEqual(mpris.service_for("spotify"),
                         "org.mpris.MediaPlayer2.spotify")

    def test_service_for_passes_a_full_name_through(self):
        full = "org.mpris.MediaPlayer2.vlc"
        self.assertEqual(mpris.service_for(full), full)

    def test_resolve_matches_an_instance_suffix(self):
        # Some clients register org.mpris.MediaPlayer2.spotify.instance1234
        names = ["org.mpris.MediaPlayer2.spotify.instance42"]
        with mock.patch.object(mpris, "list_players", return_value=names):
            self.assertEqual(mpris.resolve("spotify"), names[0])
            self.assertTrue(mpris.is_running("spotify"))

    def test_resolve_returns_none_when_absent(self):
        with mock.patch.object(mpris, "list_players", return_value=[]):
            self.assertIsNone(mpris.resolve("spotify"))
            self.assertFalse(mpris.is_running("spotify"))


# ── Web API client ────────────────────────────────────────────────────────────

class TestCredentials(unittest.TestCase):
    def setUp(self):
        spotify_api.reset_token()

    def test_env_wins_over_config(self):
        with mock.patch.dict("os.environ",
                             {"SPOTIFY_CLIENT_ID": "envid",
                              "SPOTIFY_CLIENT_SECRET": "envsecret"}), \
             mock.patch.object(spotify_api, "_cfg",
                               return_value={"client_id": "cfgid",
                                             "client_secret": "cfgsecret"}):
            self.assertEqual(spotify_api.credentials(), ("envid", "envsecret"))

    def test_falls_back_to_config(self):
        with mock.patch.dict("os.environ", {}, clear=True), \
             mock.patch.object(spotify_api, "_cfg",
                               return_value={"client_id": "cfgid",
                                             "client_secret": "cfgsecret"}):
            self.assertEqual(spotify_api.credentials(), ("cfgid", "cfgsecret"))
            self.assertTrue(spotify_api.is_configured())

    def test_not_configured_when_blank(self):
        with mock.patch.dict("os.environ", {}, clear=True), \
             mock.patch.object(spotify_api, "_cfg", return_value={}):
            self.assertFalse(spotify_api.is_configured())

    def test_missing_credentials_raise_auth_error(self):
        with mock.patch.dict("os.environ", {}, clear=True), \
             mock.patch.object(spotify_api, "_cfg", return_value={}):
            with self.assertRaises(spotify_api.SpotifyAuthError):
                spotify_api._access_token()


class TestSearch(unittest.TestCase):
    def setUp(self):
        spotify_api.reset_token()

    def test_find_track_prefers_the_field_qualified_query(self):
        queries = []

        def fake_search(q, kind="track", limit=5):
            queries.append(q)
            return [{"uri": "spotify:track:abc", "name": "Blinding Lights",
                     "artists": [{"name": "The Weeknd"}],
                     "album": {"name": "After Hours"}}]

        with mock.patch.object(spotify_api, "search", fake_search):
            found = spotify_api.find_track("Blinding Lights", "The Weeknd")

        self.assertEqual(queries[0], 'track:"Blinding Lights" artist:"The Weeknd"')
        self.assertEqual(found["uri"], "spotify:track:abc")
        self.assertEqual(found["artist"], "The Weeknd")
        self.assertEqual(found["album"], "After Hours")

    def test_find_track_falls_back_to_a_loose_query(self):
        queries = []

        def fake_search(q, kind="track", limit=5):
            queries.append(q)
            if q.startswith("track:"):
                return []                      # strict lookup misses
            return [{"uri": "spotify:track:xyz", "name": "Cico Buff",
                     "artists": [{"name": "Cocteau Twins"}], "album": {}}]

        with mock.patch.object(spotify_api, "search", fake_search):
            found = spotify_api.find_track("Cico Buff", "Cocteau Twins")

        self.assertEqual(len(queries), 2)
        self.assertEqual(found["uri"], "spotify:track:xyz")

    def test_find_track_returns_none_when_nothing_matches(self):
        with mock.patch.object(spotify_api, "search", lambda *a, **k: []):
            self.assertIsNone(spotify_api.find_track("no such song"))

    def test_find_track_ignores_empty_input(self):
        self.assertIsNone(spotify_api.find_track("   "))

    def test_search_skips_null_playlist_entries(self):
        body = {"playlists": {"items": [None, {"uri": "spotify:playlist:1",
                                               "name": "Deep Focus",
                                               "owner": {"display_name": "Spotify"}}]}}
        with mock.patch.object(spotify_api, "_get", return_value=body):
            items = spotify_api.search("deep focus", "playlist")
        self.assertEqual(len(items), 1)

    def test_search_of_blank_query_makes_no_request(self):
        with mock.patch.object(spotify_api, "_get") as get:
            self.assertEqual(spotify_api.search("  "), [])
        get.assert_not_called()


# ── Command layer ─────────────────────────────────────────────────────────────

class _FakeMpris:
    """Stand-in for the mpris module, recording the calls made against it."""

    ROOT_IFACE = mpris.ROOT_IFACE

    def __init__(self, running=True, snapshot=None, status="Playing"):
        self.running = running
        self.snapshot = snapshot or {
            "title": "Cico Buff", "artist": "Cocteau Twins", "album": "Blue Bell Knoll",
            "uri": "spotify:track:xyz", "url": "", "art_url": "",
            "length_sec": 226.0, "status": status,
        }
        self.calls = []
        self.props = {}

    def is_running(self, player="spotify"):
        return self.running

    def launch(self, command="spotify", player="spotify", timeout=15.0):
        self.calls.append(("launch", command))
        return self.running

    def call(self, method, args=None, player="spotify", interface=None):
        self.calls.append((method, args))
        return True, None

    def now_playing(self, player="spotify"):
        return self.snapshot

    def playback_status(self, player="spotify"):
        return self.snapshot.get("status", "")

    def set_prop(self, name, value, signature, player="spotify", interface=None):
        self.props[name] = value
        return True

    def methods(self):
        return [c[0] for c in self.calls]


class _SpotifyCommandTest(unittest.TestCase):
    """Base class that stubs the bus and isolates preference memory."""

    def setUp(self):
        self.fake = _FakeMpris()
        self._patches = [
            mock.patch.object(sp, "_mpris", return_value=self.fake),
            mock.patch.object(sp, "_sync_context",
                              side_effect=lambda status="playing", changed_from=None,
                                                 timeout=2.0: self.fake.snapshot),
            mock.patch.object(sp.memory, "remember_music"),
            mock.patch.object(sp.context, "update_music"),
            mock.patch.object(sp.context, "wake_triggered", False),
        ]
        for p in self._patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self._patches])


class TestTransport(_SpotifyCommandTest):
    def test_pause_sends_pause(self):
        self.assertEqual(sp.pause_music(), "Paused.")
        self.assertIn("Pause", self.fake.methods())

    def test_next_reports_the_new_track(self):
        self.assertEqual(sp.next_track(), "Next: Cico Buff by Cocteau Twins.")
        self.assertIn("Next", self.fake.methods())

    def test_previous_reports_the_new_track(self):
        self.assertIn("Back to", sp.previous_track())
        self.assertIn("Previous", self.fake.methods())

    def test_toggle_pauses_when_playing(self):
        self.assertEqual(sp.toggle_music(), "Paused.")
        self.assertIn("PlayPause", self.fake.methods())

    def test_toggle_plays_when_paused(self):
        self.fake.snapshot["status"] = "Paused"
        self.assertEqual(sp.toggle_music(), "Playing.")

    def test_transport_is_a_no_op_when_spotify_is_closed(self):
        self.fake.running = False
        self.assertEqual(sp.pause_music(), "Nothing is playing.")
        self.assertEqual(sp.next_track(), "Nothing is playing.")
        self.assertEqual(self.fake.methods(), [])

    def test_stop_reports_stopped(self):
        self.assertEqual(sp.stop_music(), "Music stopped.")
        self.assertIn("Stop", self.fake.methods())


class TestNowPlayingCommand(_SpotifyCommandTest):
    def test_reports_track_artist_and_album(self):
        self.assertEqual(
            sp.now_playing(),
            "Cico Buff by Cocteau Twins, from Blue Bell Knoll.",
        )

    def test_flags_a_paused_track(self):
        self.fake.snapshot["status"] = "Paused"
        self.assertTrue(sp.now_playing().endswith("— paused."))

    def test_handles_nothing_playing(self):
        self.fake.snapshot["title"] = ""
        self.assertEqual(sp.now_playing(), "Nothing is playing right now.")

    def test_handles_spotify_closed(self):
        self.fake.running = False
        self.assertEqual(sp.now_playing(), "Spotify isn't running.")


class TestSettings(_SpotifyCommandTest):
    def test_volume_is_clamped_and_scaled(self):
        sp.spotify_set_volume(150)
        self.assertEqual(self.fake.props["Volume"], 1.0)
        sp.spotify_set_volume(-10)
        self.assertEqual(self.fake.props["Volume"], 0.0)
        sp.spotify_set_volume(45)
        self.assertAlmostEqual(self.fake.props["Volume"], 0.45)

    def test_non_numeric_volume_is_rejected(self):
        self.assertIn("between 0 and 100", sp.spotify_set_volume("loud"))

    def test_repeat_modes_map_to_mpris_loop_status(self):
        for spoken, expected in [("off", "None"), ("none", "None"),
                                 ("track", "Track"), ("song", "Track"),
                                 ("all", "Playlist"), ("playlist", "Playlist")]:
            sp.spotify_repeat(spoken)
            self.assertEqual(self.fake.props["LoopStatus"], expected, spoken)

    def test_unknown_repeat_mode_is_refused(self):
        self.assertIn('"off", "track", or "all"', sp.spotify_repeat("sideways"))
        self.assertNotIn("LoopStatus", self.fake.props)

    def test_shuffle_sets_the_property(self):
        sp.spotify_shuffle(True)
        self.assertIs(self.fake.props["Shuffle"], True)
        sp.spotify_shuffle(False)
        self.assertIs(self.fake.props["Shuffle"], False)


class TestPlayRouting(_SpotifyCommandTest):
    def test_uses_the_web_api_when_configured(self):
        found = {"uri": "spotify:track:abc", "title": "Blinding Lights",
                 "artist": "The Weeknd", "album": "After Hours"}
        with mock.patch.object(sp.spotify_api, "is_configured", return_value=True), \
             mock.patch.object(sp.spotify_api, "find_track", return_value=found):
            sp.play_music("Blinding Lights", "The Weeknd")

        self.assertIn(("OpenUri", ["spotify:track:abc"]), self.fake.calls)

    def test_falls_back_to_the_search_uri_without_credentials(self):
        with mock.patch.object(sp.spotify_api, "is_configured", return_value=False):
            sp.play_music("Blinding Lights", "The Weeknd")

        uris = [a[0] for m, a in self.fake.calls if m == "OpenUri" and a]
        self.assertEqual(uris, ["spotify:search:Blinding%20Lights%20The%20Weeknd"])

    def test_falls_back_to_the_search_uri_when_the_api_finds_nothing(self):
        with mock.patch.object(sp.spotify_api, "is_configured", return_value=True), \
             mock.patch.object(sp.spotify_api, "find_track", return_value=None):
            sp.play_music("obscure b-side")

        uris = [a[0] for m, a in self.fake.calls if m == "OpenUri" and a]
        self.assertTrue(uris[0].startswith("spotify:search:"))

    def test_contexts_use_load_context_uri(self):
        found = {"uri": "spotify:album:xyz", "name": "Souvlaki", "artist": "Slowdive"}
        with mock.patch.object(sp.spotify_api, "is_configured", return_value=True), \
             mock.patch.object(sp.spotify_api, "find_album", return_value=found):
            sp.spotify_play_album("Souvlaki", "Slowdive")

        self.assertEqual(self.fake.calls[0], ("LoadContextUri", ["spotify:album:xyz"]))

    def test_artist_lookup_routes_to_the_artist_context(self):
        found = {"uri": "spotify:artist:abc", "name": "Slowdive"}
        with mock.patch.object(sp.spotify_api, "is_configured", return_value=True), \
             mock.patch.object(sp.spotify_api, "find_artist", return_value=found):
            sp.spotify_play_artist("Slowdive")

        self.assertEqual(self.fake.calls[0], ("LoadContextUri", ["spotify:artist:abc"]))

    def test_empty_track_replays_the_remembered_preference(self):
        self.fake.snapshot["status"] = "Paused"   # else the already-playing guard fires
        pref = {"track": "Cico Buff", "artist": "Cocteau Twins", "source": "spotify"}
        found = {"uri": "spotify:track:xyz", "title": "Cico Buff",
                 "artist": "Cocteau Twins", "album": ""}
        with mock.patch.object(sp.memory, "get_preferred_music", return_value=pref), \
             mock.patch.object(sp.spotify_api, "is_configured", return_value=True), \
             mock.patch.object(sp.spotify_api, "find_track", return_value=found) as ft:
            sp.play_music()
        ft.assert_called_once_with("Cico Buff", "Cocteau Twins")

    def test_empty_track_with_no_preference_just_presses_play(self):
        with mock.patch.object(sp.memory, "get_preferred_music",
                               return_value={"track": "", "artist": ""}):
            sp.play_music()
        self.assertIn("Play", self.fake.methods())

    def test_does_not_restart_a_track_already_playing(self):
        reply = sp.play_music("Cico Buff")
        self.assertEqual(reply, "Cico Buff is already playing.")
        self.assertEqual(self.fake.methods(), [])

    def test_launches_spotify_when_it_is_closed(self):
        self.fake.running = False
        with mock.patch.object(sp, "_autostart", return_value=True):
            reply = sp.play_music("anything")
        self.assertIn(("launch", "spotify"), self.fake.calls)
        self.assertIn("isn't running", reply)

    def test_respects_autostart_disabled(self):
        self.fake.running = False
        with mock.patch.object(sp, "_autostart", return_value=False):
            reply = sp.play_music("anything")
        self.assertNotIn("launch", [c[0] for c in self.fake.calls])
        self.assertIn("isn't running", reply)

    def test_empty_arguments_are_asked_about_not_guessed(self):
        self.assertIn("Which song", sp.spotify_play_song(""))
        self.assertIn("Which artist", sp.spotify_play_artist(""))
        self.assertIn("Which album", sp.spotify_play_album(""))
        self.assertIn("Which playlist", sp.spotify_play_playlist(""))


class TestWakeTriggeredPlayback(_SpotifyCommandTest):
    """An explicit song request is never truncated.

    NORA sets context.wake_triggered for the first command after startup, and
    playback used to be auto-paused after music.wake_playback_limit_seconds.
    That cap belongs to the wake-word entrance clip in commands/music.py — not
    to a song the user actually asked for, which was cut off mid-track.
    """

    def _play(self):
        found = {"uri": "spotify:track:abc", "title": "When the Sun Hits",
                 "artist": "Slowdive", "album": "Souvlaki"}
        with mock.patch.object(sp.spotify_api, "is_configured", return_value=True), \
             mock.patch.object(sp.spotify_api, "find_track", return_value=found):
            return sp.play_music("When the Sun Hits", "Slowdive")

    def test_wake_triggered_playback_is_not_capped(self):
        self.fake.snapshot["status"] = "Paused"
        with mock.patch.object(sp.context, "wake_triggered", True):
            reply = self._play()
        self.assertEqual(reply, "Playing Cico Buff by Cocteau Twins.")
        self.assertNotIn("preview", reply)

    def test_wake_triggered_playback_schedules_no_pause(self):
        self.fake.snapshot["status"] = "Paused"
        with mock.patch.object(sp.context, "wake_triggered", True):
            self._play()
        # Nothing may queue a Pause behind the user's back.
        self.assertNotIn("Pause", self.fake.methods())

    def test_normal_playback_is_not_capped_either(self):
        self.fake.snapshot["status"] = "Paused"
        with mock.patch.object(sp.context, "wake_triggered", False):
            reply = self._play()
        self.assertNotIn("preview", reply)
        self.assertNotIn("Pause", self.fake.methods())

    def test_the_module_no_longer_reads_the_wake_limit(self):
        # The cap is the entrance clip's business; playback must not consult it.
        import inspect
        src = inspect.getsource(sp)
        self.assertNotIn("wake_playback_limit_seconds", src)
        self.assertNotIn("wake_triggered", src)


# ── Voice routing ─────────────────────────────────────────────────────────────

class TestFastPathRouting(unittest.TestCase):
    def _action(self, phrase):
        intent = fast_path.resolve(phrase)
        self.assertIsNotNone(intent, f"{phrase!r} did not match a fast-path rule")
        self.assertTrue(intent.steps, f"{phrase!r} produced no step")
        return intent.steps[0].action, intent.steps[0].parameters

    def test_transport_phrases(self):
        for phrase, action in [
            ("pause", "pause_music"), ("pause the music", "pause_music"),
            ("stop music", "stop_music"), ("resume music", "resume_music"),
            ("next song", "next_track"), ("skip", "next_track"),
            ("previous track", "previous_track"), ("go back", "previous_track"),
        ]:
            self.assertEqual(self._action(phrase)[0], action, phrase)

    def test_now_playing_phrases(self):
        for phrase in ("what's playing", "what song is this", "now playing",
                       "who is singing", "current track"):
            self.assertEqual(self._action(phrase)[0], "now_playing", phrase)

    def test_bare_play_uses_the_preference(self):
        for phrase in ("play music", "play something", "put on some music"):
            action, params = self._action(phrase)
            self.assertEqual(action, "play_music", phrase)
            self.assertEqual(params, {"track": "", "artist": ""})

    def test_song_with_artist_splits_on_by(self):
        action, params = self._action("play Blinding Lights by The Weeknd")
        self.assertEqual(action, "play_music")
        self.assertEqual(params, {"track": "Blinding Lights", "artist": "The Weeknd"})

    def test_bare_song_name(self):
        action, params = self._action("play Blinding Lights")
        self.assertEqual(action, "spotify_play_song")
        self.assertEqual(params["song"], "Blinding Lights")

    def test_artist_only_phrasings(self):
        for phrase, artist in [("play some slowdive", "slowdive"),
                               ("play something by The Weeknd", "The Weeknd")]:
            action, params = self._action(phrase)
            self.assertEqual(action, "spotify_play_artist", phrase)
            self.assertEqual(params["artist"], artist)

    def test_album_phrasings(self):
        action, params = self._action("play the album Souvlaki by Slowdive")
        self.assertEqual(action, "spotify_play_album")
        self.assertEqual(params, {"album": "Souvlaki", "artist": "Slowdive"})

    def test_playlist_phrasings(self):
        for phrase, name in [("play the chill vibes playlist", "chill vibes"),
                             ("play playlist deep focus", "deep focus")]:
            action, params = self._action(phrase)
            self.assertEqual(action, "spotify_play_playlist", phrase)
            self.assertEqual(params["name"], name)

    def test_shuffle_and_repeat(self):
        self.assertEqual(self._action("shuffle on"),
                         ("spotify_shuffle", {"enabled": True}))
        self.assertEqual(self._action("turn off shuffle"),
                         ("spotify_shuffle", {"enabled": False}))
        self.assertEqual(self._action("repeat track"),
                         ("spotify_repeat", {"mode": "track"}))
        self.assertEqual(self._action("repeat off"),
                         ("spotify_repeat", {"mode": "off"}))


# ── Registry ──────────────────────────────────────────────────────────────────

class TestRegistry(unittest.TestCase):
    def test_apple_music_is_gone_and_spotify_is_registered(self):
        from nora import command_engine
        command_engine.discover_commands()
        actions = command_engine.get_available_actions()

        self.assertEqual([a for a in actions if "apple" in a.lower()], [])
        for expected in ("play_music", "resume_music", "pause_music", "stop_music",
                         "toggle_music", "next_track", "previous_track", "now_playing",
                         "spotify_play_song", "spotify_play_artist",
                         "spotify_play_album", "spotify_play_playlist",
                         "spotify_set_volume", "spotify_shuffle", "spotify_repeat",
                         "open_spotify"):
            self.assertIn(expected, actions)

    def test_every_music_action_is_in_the_music_category(self):
        from nora import command_engine
        command_engine.discover_commands()
        for name in ("play_music", "next_track", "now_playing", "spotify_shuffle"):
            self.assertEqual(command_engine.get_action_meta(name).category, "music")


if __name__ == "__main__":
    unittest.main()
