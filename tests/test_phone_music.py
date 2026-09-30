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
        self.patches = [mock.patch.object(phone_music, "_phone", phone)]
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


if __name__ == "__main__":
    unittest.main()
