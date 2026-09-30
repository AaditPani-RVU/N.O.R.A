"""Music on the phone: the core picks what to play, the phone plays it.

Spotify's play-from-search opens its search page and starts nothing, so the
core resolves "my workout playlist" to a URI — the user's own playlist through
their Spotify login, else the public catalogue — and hands that to the phone.
"""
from __future__ import annotations

import asyncio
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from nora import spotify_user
from nora.commands import phone_music
from nora.schemas import StepResult

MINE = [
    {"name": "Workout 🔥", "uri": "spotify:playlist:aaaaaaaaaaaaaaaaaaaaaa", "mine": True},
    {"name": "Offline Backup", "uri": "spotify:playlist:bbbbbbbbbbbbbbbbbbbbbb", "mine": True},
    {"name": "Lofi beats to study to", "uri": "spotify:playlist:cccccccccccccccccccccc", "mine": False},
    {"name": "Workout Mix Vol. 7 Extended", "uri": "spotify:playlist:dddddddddddddddddddddd", "mine": False},
    {"name": "Road trip", "uri": "spotify:playlist:eeeeeeeeeeeeeeeeeeeeee", "mine": True},
]


class MatchTest(unittest.TestCase):
    def _name(self, q):
        hit = spotify_user.match(q, MINE)
        return hit["name"] if hit else None

    def test_spoken_names_find_the_users_playlist(self):
        self.assertEqual(self._name("workout"), "Workout 🔥")
        self.assertEqual(self._name("my workout playlist"), "Workout 🔥")
        self.assertEqual(self._name("lofi"), "Lofi beats to study to")
        self.assertEqual(self._name("roadtrip"), "Road trip")          # close enough

    def test_downloads_mean_offline_backup(self):
        for q in ("downloads", "downloaded songs", "offline", "offline backup"):
            with self.subTest(q=q):
                self.assertEqual(self._name(q), "Offline Backup")

    def test_no_near_miss_is_invented(self):
        self.assertIsNone(self._name("jazz"))
        self.assertIsNone(self._name(""))


class TokenTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "tok.json"
        self.patch = mock.patch.object(spotify_user, "TOKEN_PATH", self.path)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.dir.cleanup()

    def test_token_file_is_private_and_refreshes(self):
        spotify_user._save({"access_token": "old", "refresh_token": "r1", "expires_at": 0})
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)
        with mock.patch.object(spotify_user, "_token_request",
                               return_value={"access_token": "new", "expires_in": 3600,
                                             "refresh_token": "r2"}) as req:
            self.assertEqual(spotify_user._access_token(), "new")
            self.assertEqual(spotify_user._access_token(), "new")      # cached, one request
        self.assertEqual(req.call_count, 1)
        self.assertEqual(json.loads(self.path.read_text())["refresh_token"], "r2")

    def test_not_linked_says_so(self):
        self.assertFalse(spotify_user.is_logged_in())
        with self.assertRaises(spotify_user.NotLoggedIn):
            spotify_user.find_playlist("workout")


class PlayOnPhoneTest(unittest.TestCase):
    def setUp(self):
        self.sent: list[dict] = []

        async def phone(params):
            self.sent.append(params)
            return StepResult(action="phone.play_media", success=True,
                              message=f"Playing {params.get('label')} on Spotify.")
        self.patches = [mock.patch.object(phone_music, "_phone", phone),
                        # The phone path; ConnectTest covers Spotify Connect.
                        mock.patch.object(spotify_user, "can_play", return_value=False)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def run_(self, **kw) -> StepResult:
        return asyncio.run(phone_music.play_on_phone(**kw))

    def test_own_playlist_goes_to_the_phone_as_a_uri(self):
        with mock.patch.object(spotify_user, "playlists", return_value=MINE):
            r = self.run_(query="workout", kind="playlist")
        self.assertTrue(r.success)
        self.assertEqual(self.sent[0]["uri"], "spotify:playlist:aaaaaaaaaaaaaaaaaaaaaa")
        self.assertEqual(self.sent[0]["label"], "your Workout 🔥 playlist")
        self.assertNotIn("shuffle", self.sent[0])

    def test_downloads_shuffle(self):
        with mock.patch.object(spotify_user, "playlists", return_value=MINE):
            self.run_(query="offline backup", kind="playlist", shuffle=True)
        self.assertEqual(self.sent[0]["uri"], "spotify:playlist:bbbbbbbbbbbbbbbbbbbbbb")
        self.assertTrue(self.sent[0]["shuffle"])

    def test_missing_offline_backup_is_said_not_substituted(self):
        with mock.patch.object(spotify_user, "playlists", return_value=MINE[:1]), \
             mock.patch.object(phone_music.spotify_api, "find_playlist") as public:
            r = self.run_(query="offline backup", kind="playlist", shuffle=True)
        self.assertFalse(r.success)
        self.assertIn("Offline Backup", r.message)
        public.assert_not_called()
        self.assertEqual(self.sent, [])

    def test_not_linked_falls_back_to_public_and_says_why(self):
        with mock.patch.object(spotify_user, "find_playlist", side_effect=spotify_user.NotLoggedIn), \
             mock.patch.object(phone_music.spotify_api, "find_playlist",
                               return_value={"uri": "spotify:playlist:pppppppppppppppppppppp",
                                             "name": "Workout", "owner": "x"}):
            r = self.run_(query="workout", kind="playlist")
        self.assertEqual(self.sent[0]["uri"], "spotify:playlist:pppppppppppppppppppppp")
        self.assertIn("isn't linked", r.message)

    TRACK = {"uri": "spotify:track:0VjIjW4GlUZAMYd2vXMi3b", "title": "Blinding Lights",
             "artist": "The Weeknd", "album": "After Hours"}

    def test_a_song_is_found_in_the_catalogue(self):
        with mock.patch.object(spotify_user, "playlists", return_value=MINE), \
             mock.patch.object(phone_music.spotify_api, "find_artist", return_value=None), \
             mock.patch.object(phone_music.spotify_api, "find_track", return_value=self.TRACK):
            self.run_(query="blinding lights", kind="any")
        self.assertEqual(self.sent[0]["uri"], "spotify:track:0VjIjW4GlUZAMYd2vXMi3b")
        self.assertEqual(self.sent[0]["label"], "Blinding Lights by The Weeknd")

    def test_song_by_artist_is_split(self):
        """"Risk by Deftones" searched as one title found Bruno Mars's "Risk It All"."""
        with mock.patch.object(spotify_user, "playlists", return_value=MINE) as own, \
             mock.patch.object(phone_music.spotify_api, "find_track", return_value=self.TRACK) as ft:
            self.run_(query="Risk by Deftones", kind="any")
        ft.assert_called_once_with("Risk", "Deftones")
        own.assert_not_called()

    def test_an_artist_name_plays_the_artist(self):
        with mock.patch.object(spotify_user, "playlists", return_value=MINE), \
             mock.patch.object(phone_music.spotify_api, "find_artist",
                               return_value={"uri": "spotify:artist:0k17h0D3J5VfsdmQ1iZtE9",
                                             "name": "Pink Floyd"}), \
             mock.patch.object(phone_music.spotify_api, "find_track") as ft:
            self.run_(query="pink floyd", kind="any")
        self.assertEqual(self.sent[0]["uri"], "spotify:artist:0k17h0D3J5VfsdmQ1iZtE9")
        ft.assert_not_called()

    def test_a_bare_song_name_does_not_fuzzy_match_a_playlist(self):
        # "roadtrip" is close to "Road trip"; said as "play roadtrip" it's a song.
        with mock.patch.object(spotify_user, "playlists", return_value=MINE), \
             mock.patch.object(phone_music.spotify_api, "find_artist", return_value=None), \
             mock.patch.object(phone_music.spotify_api, "find_track", return_value=self.TRACK):
            self.run_(query="roadtrip", kind="any")
        self.assertEqual(self.sent[0]["uri"], self.TRACK["uri"])

    def test_nothing_found_still_asks_the_phone_to_search(self):
        with mock.patch.object(spotify_user, "playlists", return_value=MINE), \
             mock.patch.object(phone_music.spotify_api, "find_artist", return_value=None), \
             mock.patch.object(phone_music.spotify_api, "find_track", return_value=None):
            self.run_(query="some obscure thing", kind="any")
        self.assertNotIn("uri", self.sent[0])


class _Resp:
    def __init__(self, status: int, body: dict | None = None):
        self.status_code, self._body = status, body
        self.content = b"x" if body is not None else b""

    def json(self):
        return self._body


class ConnectTest(unittest.TestCase):
    """Spotify's Android session ignored play-from-URI from NORA's app, so a
    phone that is a Connect device is played through Spotify's servers."""

    PHONE = {"id": "dev-pixel", "type": "Smartphone", "name": "Pixel 10", "is_active": False}
    LAPTOP = {"id": "dev-laptop", "type": "Computer", "name": "laptop", "is_active": True}
    DEFTONES = "spotify:track:0lqHgjNrXmtFroWDqwV1iQ"

    def setUp(self):
        self.calls: list[tuple] = []
        self.player: dict = {}

        def send(method, path, params=None, body=None):
            self.calls.append((method, path, params, body))
            if path == "/me/player/devices":
                return _Resp(200, {"devices": [self.LAPTOP, self.PHONE]})
            if path == "/me/player" and method == "GET":
                return _Resp(200, self.player) if self.player else _Resp(204)
            return _Resp(204)
        self.p = mock.patch.object(spotify_user, "_send", send)
        self.p.start()
        self.sleep = mock.patch.object(spotify_user.time, "sleep")
        self.sleep.start()

    def tearDown(self):
        self.p.stop()
        self.sleep.stop()

    def test_the_phone_is_the_smartphone(self):
        self.assertEqual(spotify_user.phone_device()["id"], "dev-pixel")

    def test_a_track_plays_and_is_confirmed(self):
        self.player = {"is_playing": True, "device": {"id": "dev-pixel"},
                       "item": {"uri": self.DEFTONES, "name": "Risk",
                                "artists": [{"name": "Deftones"}]}}
        self.assertEqual(spotify_user.play_on_device(self.DEFTONES, "dev-pixel"), "Risk by Deftones")
        put = [c for c in self.calls if c[0] == "PUT"]
        self.assertEqual(put[0], ("PUT", "/me/player/play", {"device_id": "dev-pixel"},
                                  {"uris": [self.DEFTONES]}))

    def test_a_playlist_is_a_context_and_shuffles_after_play(self):
        pl = "spotify:playlist:1dqEsSy2VZ5q5u7SKVbm07"
        self.player = {"is_playing": True, "device": {"id": "dev-pixel"}, "context": {"uri": pl},
                       "item": {"uri": "spotify:track:x", "name": "Song", "artists": []}}
        spotify_user.play_on_device(pl, "dev-pixel", shuffle=True)
        put = [(c[1], c[3]) for c in self.calls if c[0] == "PUT"]
        self.assertEqual(put, [("/me/player/play", {"context_uri": pl}), ("/me/player/shuffle", None)])

    def test_the_old_song_is_not_success(self):
        self.player = {"is_playing": True, "device": {"id": "dev-pixel"},
                       "item": {"uri": "spotify:track:bruno", "name": "Risk It All",
                                "artists": [{"name": "Bruno Mars"}]}}
        with mock.patch.object(spotify_user.time, "time", side_effect=[0, 0, 1, 2, 5, 5]):
            with self.assertRaises(spotify_user.PlaybackError) as e:
                spotify_user.play_on_device(self.DEFTONES, "dev-pixel")
        self.assertIn("Risk It All by Bruno Mars", str(e.exception))

    def test_play_on_phone_uses_connect_and_skips_the_phone(self):
        self.player = {"is_playing": True, "device": {"id": "dev-pixel"},
                       "item": {"uri": self.DEFTONES, "name": "Risk",
                                "artists": [{"name": "Deftones"}]}}
        phone = mock.AsyncMock()
        with mock.patch.object(spotify_user, "can_play", return_value=True), \
             mock.patch.object(phone_music, "_phone", phone), \
             mock.patch.object(phone_music.spotify_api, "find_track",
                               return_value={"uri": self.DEFTONES, "title": "Risk",
                                             "artist": "Deftones", "album": "x"}):
            r = asyncio.run(phone_music.play_on_phone("Risk by Deftones"))
        self.assertTrue(r.success, r.message)
        self.assertEqual(r.message, "Playing Risk by Deftones on your phone.")
        phone.assert_not_called()

    def test_no_connect_device_falls_back_to_the_phone(self):
        self.PHONE = dict(self.PHONE, type="Computer")          # phone's Spotify not running
        phone = mock.AsyncMock(return_value=StepResult(action="phone.play_media", success=False,
                                                       message="notification"))
        with mock.patch.object(spotify_user, "can_play", return_value=True), \
             mock.patch.object(phone_music, "_phone", phone), \
             mock.patch.object(phone_music.spotify_api, "find_track",
                               return_value={"uri": self.DEFTONES, "title": "Risk",
                                             "artist": "Deftones", "album": "x"}):
            asyncio.run(phone_music.play_on_phone("Risk by Deftones"))
        phone.assert_called_once()
        self.assertEqual(phone.call_args[0][0]["uri"], self.DEFTONES)


if __name__ == "__main__":
    unittest.main()
