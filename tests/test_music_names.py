"""Sharp D: misheard music names, repaired against the user's own library."""
from __future__ import annotations

import json

import pytest

from nora import music_names

ARTISTS = ["Deftones", "Pink Floyd", "Daft Punk", "Childish Gambino", "Radiohead", "Fleetwood Mac",
           "Travis Scott", "Eminem", "The Weeknd", "Alan Walker", "Gunna", "Lil Baby", "Logic",
           "Faydee", "The Police", "Tove Lo", "Calvin Harris", "Linkin Park", "Ed Sheeran", "Drake"]
TRACKS = {"Starboy": ["The Weeknd"], "Change (In the House of Flies)": ["Deftones"],
          "Comfortably Numb": ["Pink Floyd"], "Redbone": ["Childish Gambino"],
          "RISE": ["League of Legends"], "Knockout": ["Lil Baby"]}


@pytest.fixture(autouse=True)
def names(tmp_path):
    path = tmp_path / "names.json"
    path.write_text(json.dumps({"artists": {a: 1 for a in ARTISTS}, "tracks": TRACKS}))
    with music_names.frozen(path):
        yield


@pytest.mark.parametrize("action, heard, want", [
    ("play_on_phone", {"query": "risk by deaf tools", "kind": "any"}, {"query": "risk by Deftones"}),
    ("play_on_phone", {"query": "Risk by Deptones", "kind": "any"}, {"query": "Risk by Deftones"}),
    ("play_on_phone", {"query": "pink fluid", "kind": "any"}, {"query": "Pink Floyd"}),
    ("play_on_phone", {"query": "some pink fluid", "kind": "any"}, {"query": "Pink Floyd"}),
    ("play_music", {"track": "Voyager", "artist": "Darkpunk"}, {"track": "Voyager", "artist": "Daft Punk"}),
    ("play_music", {"track": "pink fluid", "artist": ""}, {"track": "Pink Floyd"}),
    ("spotify_play_artist", {"artist": "charles gambino"}, {"artist": "Childish Gambino"}),
    ("spotify_play_artist", {"artist": "radio head"}, {"artist": "Radiohead"}),
    ("spotify_play_album", {"album": "Lateralus", "artist": "fleet wood mac"}, {"artist": "Fleetwood Mac"}),
])
def test_misheard_names_are_repaired(action, heard, want):
    got, _ = music_names.repair(action, heard)
    for k, v in want.items():
        assert got[k] == v, f"{heard} → {got}"


@pytest.mark.parametrize("action, heard", [
    # Names not in the library are left as heard: they're for Spotify to find.
    ("play_on_phone", {"query": "taylor swift", "kind": "any"}),
    ("play_on_phone", {"query": "queen", "kind": "any"}),
    ("play_on_phone", {"query": "lofi", "kind": "any"}),
    ("play_on_phone", {"query": "coldplay", "kind": "any"}),
    ("play_on_phone", {"query": "blinding lights", "kind": "any"}),
    ("play_on_phone", {"query": "risk", "kind": "any"}),
    ("spotify_play_artist", {"artist": "adele"}),
    ("spotify_play_artist", {"artist": "stomay"}),
    # Right already, or a playlist (matched against playlists, not artists).
    ("play_on_phone", {"query": "starboy", "kind": "any"}),
    ("play_on_phone", {"query": "workout", "kind": "playlist"}),
    ("spotify_play_artist", {"artist": "the weeknd"}),
    # Another action entirely.
    ("set_volume", {"level": 40}),
])
def test_other_names_are_left_alone(action, heard):
    got, note = music_names.repair(action, dict(heard))
    assert got == heard and note == ""


def test_an_unsure_repair_is_said_and_a_sure_one_is_not():
    _, unsure = music_names.repair("play_on_phone", {"query": "deaf tools", "kind": "any"})
    assert unsure == "Taking 'deaf tools' as Deftones."
    _, sure = music_names.repair("play_on_phone", {"query": "pink fluid", "kind": "any"})
    assert sure == ""


def test_nothing_is_repaired_without_names(tmp_path):
    with music_names.frozen(tmp_path / "missing.json"):
        heard = {"query": "deaf tools", "kind": "any"}
        assert music_names.repair("play_on_phone", dict(heard)) == (heard, "")


def test_refresh_builds_the_cache_from_the_library(tmp_path, monkeypatch):
    from nora import spotify_user
    monkeypatch.setattr(music_names, "CACHE_PATH", tmp_path / "names.json")
    monkeypatch.setattr(spotify_user, "library", lambda: [
        {"track": "Digital Bath", "artists": ["Deftones"]},
        {"track": "", "artists": ["Deftones"]},
        {"track": "Time", "artists": ["Pink Floyd"]},
    ])
    assert music_names.refresh() == 2
    with music_names.frozen(tmp_path / "names.json"):
        assert music_names.artists() == ["Deftones", "Pink Floyd"]
        assert music_names.tracks() == {"Digital Bath": ["Deftones"], "Time": ["Pink Floyd"]}


def test_play_on_phone_plays_the_repaired_name_and_says_so(monkeypatch):
    import asyncio
    from nora.commands import phone_music
    seen = {}

    def resolve(query, kind):
        seen["query"] = query
        return "spotify:artist:x", "Deftones", ""

    monkeypatch.setattr(phone_music, "_resolve", resolve)
    monkeypatch.setattr(phone_music, "_via_connect", lambda uri, label, shuffle: (True, "Playing Deftones on your phone."))
    res = asyncio.run(phone_music.play_on_phone("deaf tools"))
    assert seen["query"] == "Deftones"
    assert res.message == "Taking 'deaf tools' as Deftones. Playing Deftones on your phone."
