"""
Fast-path deterministic intent resolver.

Matches common voice commands with regex before the LLM is ever called.
Returns an IntentResponse on a confident match, or None to escalate to the LLM.

Design principles:
- Bias toward action: attempt the most obvious interpretation, never ask for context.
- Handle Whisper filler artifacts ("uh open chrome" → open_app("chrome")).
- Rules are ordered most-specific first within each category.
"""
from __future__ import annotations

import re

from nora.schemas import ActionStep, IntentResponse


# ── Helpers ────────────────────────────────────────────────────────────────────

def _step(action: str, params: dict | None = None) -> ActionStep:
    return ActionStep(action=action, parameters=params or {})


def _intent(
    intent: str,
    action: str,
    params: dict | None = None,
    confirm: bool = False,
) -> IntentResponse:
    return IntentResponse(
        intent=intent,
        steps=[_step(action, params or {})],
        requires_confirmation=confirm,
    )


def _chat(response: str) -> IntentResponse:
    return IntentResponse(intent="chat", steps=[], response=response)


def _chat_varied(category: str, fallback: str) -> IntentResponse:
    """Chat reply drawn from a phrasing pool.

    The fast path exists for latency, and that's worth keeping for greetings —
    but a fixed string means the hundredth "hey" gets a byte-identical answer
    to the first, which is the most audible tell that nothing is home. Pools
    keep the speed and lose the loop.
    """
    from nora import phrasing
    return _chat(phrasing.get(category, fallback))


# ── Text normalisation ─────────────────────────────────────────────────────────

# Whisper often adds leading filler words; strip them before matching.
_FILLER_RE = re.compile(
    r"^(?:"
    r"uh+\s+|um+\s+|er+\s+|ah+\s+|"
    # "jarvis" too: the wake word in use is hey_jarvis, and the phrase
    # sometimes lands in the transcript, which then matched no rule at all.
    r"(?:(?:hey|hi|okay|ok)\s+)?(?:nora|jarvis)[,.\s]+"
    r"(?:ok(?:ay)?\s+|right\s+|so\s+|well\s+)?"
    r"|(?:(?:can|could|would)\s+you\s+(?:please\s+)?)"
    r"|(?:(?:i(?:'d|\s+would)?(?:\s+like(?:\s+you)?)?|i\s+need(?:\s+you)?)\s+to\s+)"
    r"|please\s+"
    r")+",
    re.I,
)
_SUFFIX_RE = re.compile(
    r"\s+(?:for\s+me|please|right\s+now|now|quickly|asap)\s*$", re.I
)


def _normalise(text: str) -> str:
    """Strip leading fillers, trailing noise, and punctuation."""
    # Phone keyboards type curly apostrophes; every rule is written with '.
    t = _FILLER_RE.sub("", text.strip().replace("\u2019", "'"))
    t = _SUFFIX_RE.sub("", t)
    return t.rstrip(".,!?;").strip()


def _clean_name(raw: str) -> str:
    """Remove leading articles from an app/file name."""
    return re.sub(r"^(?:the|a|an)\s+", "", raw.strip(), flags=re.I).strip()


def _device_offers(action: str) -> bool:
    """True while a connected device has registered `action`."""
    from nora import command_engine
    meta = command_engine.get_action_meta(action)
    return meta is not None and bool(meta.device)


def _from_phone(action: str) -> bool:
    """True when this turn came from the device that offers `action` — "play
    X" said to the phone means the phone, not the laptop in another room."""
    from nora import channel, command_engine
    ch = channel.current()
    meta = command_engine.get_action_meta(action)
    return (ch is not None and meta is not None and bool(meta.device)
            and meta.device == ch.device_id)


_NO_PHONE = "Your phone isn't connected right now."


def _on_phone(action: str, intent: IntentResponse) -> IntentResponse:
    """The request named the phone: its capability, or say it isn't there —
    never quietly run the laptop's command of the same name instead."""
    return intent if _device_offers(action) else _chat(_NO_PHONE)


def _media_query(raw: str) -> dict:
    """"my workout playlist" → {query: "workout", kind: "playlist"}."""
    q = re.sub(r"\s+", " ", raw.strip())
    kind = "any"
    for pattern, k in (
        (r"^(?:my\s+|the\s+)?(.+?)\s+playlist$", "playlist"),
        (r"^(?:my\s+|the\s+)?playlist\s+(.+)$", "playlist"),
        (r"^(?:the\s+)?album\s+(.+)$", "album"),
        (r"^(?:something|anything|songs|music|stuff)\s+by\s+(.+)$", "artist"),
        (r"^(?:the\s+)?(?:song|track)\s+(.+)$", "track"),
    ):
        m = re.match(pattern, q, re.I)
        if m:
            q, kind = m.group(1), k
            break
    q = re.sub(r"^(?:my|some|the)\s+", "", q, flags=re.I).strip()
    return {"query": q[:100], "kind": kind}


_SHUFFLE_RE = re.compile(r"\s+(?:on\s+shuffle|shuffled|in\s+shuffle(?:\s+mode)?)$", re.I)


def _play_on_phone(raw: str, shuffle: bool = False) -> IntentResponse:
    """play_on_phone for "my workout playlist (on shuffle)": the core finds the
    user's playlist, the phone plays it."""
    q = raw.strip()
    if _SHUFFLE_RE.search(q):
        q, shuffle = _SHUFFLE_RE.sub("", q), True
    params = _media_query(q)
    if shuffle:
        params["shuffle"] = True
    return _intent("play on phone", "play_on_phone", params)


# ── Rule table ─────────────────────────────────────────────────────────────────

_RULES: list[tuple[re.Pattern, object]] = []
_BUILT = False


def _rule(pattern: str, fn):
    """Register a compiled, anchored, case-insensitive rule."""
    _RULES.append((re.compile("^(?:" + pattern + ")$", re.I), fn))


def _phone_rules() -> None:
    """Requests for the paired phone (Phase 4). First in the table: "play X on
    my phone" must not reach the laptop's "play X", and "open Spotify and play
    …" must not become open_app("spotify and play …")."""
    phone = r"(?:on|from|using)\s+(?:my|the)\s+(?:phone|mobile|pixel)"

    # "shuffle my downloads": Spotify keeps the songs downloaded on the phone
    # in a playlist it makes itself, "Offline Backup".
    _rule(
        r"(?:(?P<sh>shuffle)(?:\s+play)?|play|put\s+on|start)\s+(?:my\s+|the\s+)?"
        r"(?:downloads|downloaded\s+(?:songs|music|tracks)|offline\s+(?:songs|music|tracks)"
        r"|offline\s+backup(?:\s+playlist)?)(?P<sh2>\s+on\s+shuffle|\s+shuffled)?(?:\s+" + phone + r")?",
        lambda m: _on_phone("phone.play_media", _intent(
            "play on phone", "play_on_phone",
            {"query": "offline backup", "kind": "playlist", "shuffle": True})),
    )
    # "open Spotify (on my phone) and play my workout playlist". The core finds
    # the user's own playlist (nora.spotify_user); the phone plays it.
    _rule(
        r"(?:open|launch|start)\s+(?:up\s+)?spotify(?:\s+" + phone + r")?\s+and\s+play\s+(?P<q>.+?)"
        r"(?:\s+" + phone + r")?",
        lambda m: (_on_phone("phone.play_media", _play_on_phone(m.group("q")))
            if re.search(phone, m.group(0), re.I) or _from_phone("phone.play_media")
            or _yours(m.group("q")) else None),
    )
    # "play my workout playlist": the user's own playlists, which only the
    # user's Spotify login can see.
    _rule(
        r"(?P<sh>shuffle(?:\s+play)?|play)\s+(?P<q>my\s+.+?\s+playlist(?:\s+on\s+shuffle|\s+shuffled)?"
        r"|my\s+playlist\s+.+)",
        lambda m: (_play_on_phone(m.group("q"), m.group("sh").lower().startswith("shuffle"))
                   if _yours(m.group("q")) else None),
    )
    _rule(
        r"play\s+(?P<q>.+?)\s+(?:on\s+spotify\s+)?" + phone + r"(?:\s+on\s+spotify)?",
        lambda m: _on_phone("phone.play_media", _play_on_phone(m.group("q"))),
    )
    _rule(
        r"(?:open(?:\s+up)?|launch|start|go\s+to)\s+(?P<app>.+?)\s+" + phone,
        lambda m: (_on_phone("phone.open_url", _intent(
            "open link on phone", "phone.open_url", {"url": m.group("app").strip()}))
            if _looks_like_url(m.group("app")) else _on_phone("phone.open_app", _intent(
            "open app on phone", "phone.open_app", {"app": _clean_name(m.group("app"))}))),
    )
    _rule(
        r"what(?:'?s|\s+is)\s+(?:this\s+|the\s+)?(?:song\s+|track\s+|music\s+)?(?:that'?s\s+)?"
        r"(?:playing|on)\s+" + phone
        + r"|what\s+(?:song|track)\s+is\s+(?:this|playing)\s+" + phone,
        lambda m: _on_phone("phone.media_control", _intent(
            "phone now playing", "phone.media_control", {"action": "status"})),
    )
    _rule(
        r"turn\s+(?:my\s+|the\s+)?phone(?:'?s)?\s+ringer\s+(?P<d>up|down)"
        r"|turn\s+(?P<d2>up|down)\s+(?:my\s+|the\s+)?phone(?:'?s)?\s+ringer"
        r"|turn\s+(?:the\s+)?ringer\s+(?P<d3>up|down)\s+" + phone,
        lambda m: _on_phone("phone.volume", _intent(
            "phone ringer", "phone.volume",
            {"action": (m.group("d") or m.group("d2") or m.group("d3")).lower(), "stream": "ring"})),
    )
    _media_verbs = {"pause": "pause", "resume": "play", "unpause": "play", "play": "play",
                    "stop": "stop", "skip": "next", "next": "next", "previous": "previous"}
    _rule(
        r"(?P<verb>pause|resume|unpause|stop|skip|next|previous)"
        r"(?:\s+(?:the\s+)?(?:music|song|track|playback|this))?\s+" + phone,
        lambda m: _on_phone("phone.media_control", _intent(
            "phone media", "phone.media_control",
            {"action": _media_verbs[m.group("verb").lower()]})),
    )
    _rule(
        r"(?:set\s+)?(?:the\s+)?(?:(?:my\s+)?phone(?:'?s)?\s+volume|volume\s+" + phone + r")"
        r"\s+(?:to\s+)?(?P<n>\d{1,3})\s*(?:%|percent)?"
        r"|(?:set\s+)?(?:the\s+)?volume\s+(?:to\s+)?(?P<n2>\d{1,3})\s*(?:%|percent)?\s+" + phone,
        lambda m: _on_phone("phone.volume", _intent(
            "phone volume", "phone.volume",
            {"action": "set", "level": min(100, int(m.group("n") or m.group("n2")))})),
    )
    _rule(
        r"(?:turn\s+)?(?:the\s+)?(?:(?:my\s+)?phone(?:'?s)?\s+)?volume\s+(?P<d>up|down)\s*(?:" + phone + r")?"
        r"|turn\s+(?:up|down)\s+(?:the\s+)?volume\s+" + phone
        + r"|turn\s+(?:my\s+)?phone\s+(?P<d2>up|down)",
        lambda m: (_on_phone("phone.volume", _intent(
            "phone volume", "phone.volume",
            {"action": (m.group("d") or m.group("d2")
                        or ("up" if " up " in f" {m.group(0).lower()} " else "down")).lower()}))
            if "phone" in m.group(0).lower() or _from_phone("phone.volume") else None),
    )

    # Spec example 2. Only the phone has notifications NORA can read.
    _rule(
        r"(?:what(?:'?s|\s+is|\s+are)?\s+(?:on\s+|in\s+)?my\s+notifications"
        r"|(?:read|show|tell)\s+(?:me\s+)?my\s+notifications"
        r"|(?:check|go\s+through)\s+my\s+notifications"
        r"|(?:do\s+i\s+have|did\s+i\s+get|are\s+there|got)\s+any\s+(?:new\s+)?(?:notifications|messages)"
        r"|any\s+(?:new\s+)?notifications"
        r"|did\s+i\s+(?:get|miss)\s+anything(?:\s+important)?"
        r"|anything\s+important(?:\s+on\s+my\s+phone)?"
        r"|what\s+did\s+i\s+miss)"
        r"(?:\s+" + phone + r"|\s+today|\s+lately|\s+recently)?",
        lambda m: _on_phone("phone.read_notifications", _intent(
            "read phone notifications", "phone.read_notifications", {})),
    )

    _rule(
        r"where\s+am\s+i(?:\s+right\s+now)?|what(?:'?s|\s+is)\s+my\s+(?:current\s+)?location"
        r"|where(?:'?s|\s+is)\s+my\s+phone",
        lambda m: _on_phone("phone.get_location", _intent(
            "phone location", "phone.get_location", {})),
    )

    # Spec example 4: "I'm going home".
    how = r"(?:\s+(?P<how>by\s+car|on\s+foot|by\s+bike|by\s+metro|by\s+bus|walking|driving|cycling))?"
    _rule(
        r"(?:i'?m|i\s+am)\s+(?:going|heading|off|leaving|driving|walking|cycling)\s+(?:back\s+)?(?:to\s+)?"
        r"(?P<place>home|college|work|the\s+office|uni(?:versity)?)" + how + r"(?:\s+now)?"
        r"|(?:take\s+me|get\s+me|navigate(?:\s+me)?|directions|drive\s+me|route\s+me)\s+(?:back\s+)?(?:to\s+)?"
        r"(?P<place2>home|college|work|the\s+office|uni(?:versity)?)" + how.replace("how>", "how2>"),
        lambda m: _intent("navigate", "navigate_to", {
            "destination": m.group("place") or m.group("place2"),
            "mode": _travel_mode(m.group(0), m.group("how") or m.group("how2") or ""),
        }),
    )
    _rule(
        r"how\s+(?:long|far)\s+(?:will\s+it\s+take\s+(?:me\s+)?|does\s+it\s+take\s+(?:me\s+)?|is\s+(?:it\s+)?)?"
        r"(?:to\s+(?:get|go|walk|drive|cycle|bike)\s+)?(?:back\s+)?(?:to\s+)?"
        r"(?P<place>home|college|work|the\s+office|uni(?:versity)?)" + how + r"(?:\s+from\s+here)?",
        lambda m: _intent("travel time", "travel_time", {
            "destination": m.group("place"),
            "mode": _travel_mode(m.group(0), m.group("how") or ""),
        }),
    )
    # Setting a place, so "I'm going home" has somewhere to go.
    _rule(
        r"(?:remember\s+(?:that\s+)?)?my\s+(?P<place>home|house|college|university|uni|office|work|gym)"
        r"(?:'s)?\s+(?:address\s+is(?:\s+at)?|is\s+at)\s+(?P<addr>.{4,200})",
        lambda m: _intent("save place", "save_place",
                          {"name": m.group("place"), "address": m.group("addr").strip()}),
    )
    _rule(
        r"(?:save|remember|mark)\s+(?:this|here|this\s+place|this\s+spot|where\s+i\s+am)"
        r"\s+as\s+(?:my\s+|the\s+)?(?P<place>[\w' -]{2,40})"
        r"|this\s+is\s+(?:my\s+)?(?P<place2>home|college|work|gym|office)",
        lambda m: _intent("save place", "save_place",
                          {"name": (m.group("place") or m.group("place2")).strip()}),
    )
    _rule(
        r"(?:navigate(?:\s+me)?|take\s+me|directions|give\s+me\s+directions|get\s+me\s+directions)"
        r"\s+to\s+(?P<dest>.{2,100}?)" + how,
        lambda m: _intent("navigate", "navigate_to", {
            "destination": _clean_name(m.group("dest")),
            "mode": _travel_mode(m.group(0), m.group("how") or ""),
        }),
    )

    # Said to the phone, a bare "play X" / "open X" / "pause" means the phone.
    _rule(
        r"play\s+(?P<q>.{2,100})",
        lambda m: (_play_on_phone(m.group("q")) if _from_phone("phone.play_media") else None),
    )
    _rule(
        r"(?:open(?:\s+up)?|launch|start)\s+(?P<app>.+)",
        lambda m: (_intent("open app on phone", "phone.open_app",
                           {"app": _clean_name(m.group("app"))})
                   if _from_phone("phone.open_app") and " and " not in m.group("app") else None),
    )
    _rule(
        r"(?P<verb>pause|resume|unpause|skip|next|previous)(?:\s+(?:the\s+)?(?:music|song|track|this))?",
        lambda m: (_intent("phone media", "phone.media_control",
                           {"action": _media_verbs[m.group("verb").lower()]})
                   if _from_phone("phone.media_control") else None),
    )


def _looks_like_url(text: str) -> bool:
    t = text.strip().lower()
    return bool(re.match(r"^(?:https?://)?[a-z0-9-]+(?:\.[a-z0-9-]+)+(?:/\S*)?$", t))


def _yours(query: str) -> bool:
    """"my X playlist" while a phone that can play it is connected."""
    return (bool(re.match(r"my\s", query.strip(), re.I))
            and "playlist" in query.lower() and _device_offers("phone.play_media"))


def _travel_mode(text: str, how: str) -> str:
    t = f"{text} {how}".lower()
    if "walk" in t or "on foot" in t:
        return "walking"
    if "bike" in t or "cycl" in t:
        return "bicycling"
    if "metro" in t or "bus" in t:
        return "transit"
    return "driving"


def _build_rules() -> None:
    global _BUILT

    _phone_rules()

    # ─── Music: no-arg commands ───────────────────────────────────────────
    _rule(
        r"(?:play\s+(?:some\s+)?music|play\s+something|start\s+(?:some\s+)?music"
        r"|put\s+on\s+(?:some\s+)?music|music\s*(?:please)?|some\s+music)",
        lambda m: _intent("play preferred music", "play_music", {"track": "", "artist": ""}),
    )
    _rule(
        r"(?:pause\s+(?:the\s+)?(?:music|song|track)|pause\s+music|pause)",
        lambda m: _intent("pause music", "pause_music", {}),
    )
    _rule(
        r"(?:stop\s+(?:the\s+)?(?:music|song|track|playing)|stop\s+music)",
        lambda m: _intent("stop music", "stop_music", {}),
    )
    _rule(
        r"(?:resume\s+(?:the\s+)?(?:music|song|track)?|unpause(?:\s+music)?"
        r"|continue\s+(?:the\s+)?(?:music|song)?)",
        lambda m: _intent("resume music", "resume_music", {}),
    )
    _rule(
        r"(?:next\s+(?:song|track|one)?|skip(?:\s+(?:this|(?:the\s+)?(?:song|track)))?)",
        lambda m: _intent("skip track", "next_track", {}),
    )
    _rule(
        r"(?:(?:go\s+)?(?:back|previous)\s*(?:song|track)?|prev(?:ious)?\s*(?:song|track)?"
        r"|last\s+(?:song|track)|go\s+back)",
        lambda m: _intent("previous track", "previous_track", {}),
    )
    # "what's playing" / "what song is this"
    _rule(
        r"(?:what(?:\'s|\s+is)?\s+(?:this\s+)?(?:song|track|playing|music)"
        r"(?:\s+(?:is\s+)?(?:this|playing|called))?"
        r"|who(?:\'s|\s+is)\s+(?:this|singing)"
        r"|now\s+playing|current\s+(?:song|track))",
        lambda m: _intent("now playing", "now_playing", {}),
    )

    # ─── Music: shuffle / repeat ──────────────────────────────────────────
    _rule(
        r"(?:turn\s+)?shuffle\s*(?:on|off)?|(?:turn\s+)?(?:on|off)\s+shuffle",
        lambda m: _intent(
            "set shuffle", "spotify_shuffle",
            {"enabled": "off" not in m.group(0).lower()},
        ),
    )
    _rule(
        r"(?:set\s+)?repeat\s+(off|none|track|song|one|this|all|playlist|album)",
        lambda m: _intent("set repeat", "spotify_repeat", {"mode": m.group(1).lower()}),
    )
    _rule(
        r"(?:turn\s+)?repeat\s*(on|off)",
        lambda m: _intent(
            "set repeat", "spotify_repeat",
            {"mode": "all" if m.group(1).lower() == "on" else "off"},
        ),
    )

    # ─── Music: parameterised (most-specific first) ───────────────────────
    # "play the album X (by Y)"
    _rule(
        r"play\s+(?:the\s+)?album\s+(.+?)(?:\s+by\s+(.+))?",
        lambda m: _intent(
            f"play album {m.group(1).strip()}",
            "spotify_play_album",
            {"album": m.group(1).strip(), "artist": (m.group(2) or "").strip()},
        ),
    )
    # "play the X playlist" / "play playlist X"
    _rule(
        r"play\s+(?:the\s+)?playlist\s+(.+)|play\s+(?:the\s+)?(.+?)\s+playlist",
        lambda m: _intent(
            "play playlist",
            "spotify_play_playlist",
            {"name": (m.group(1) or m.group(2) or "").strip()},
        ),
    )
    # "play something/anything by Y" — artist radio, not a specific song
    _rule(
        r"play\s+(?:something|anything|some|more)\s+by\s+(.+)",
        lambda m: _intent(
            f"play music by {m.group(1).strip()}",
            "spotify_play_artist",
            {"artist": m.group(1).strip()},
        ),
    )
    # "play some <artist>" — e.g. "play some slowdive"
    _rule(
        r"play\s+(?:some|more)\s+(.{2,60})",
        lambda m: _intent(
            f"play {m.group(1).strip()}",
            "spotify_play_artist",
            {"artist": m.group(1).strip()},
        ),
    )
    # "play X by Y"
    _rule(
        r"play\s+(.+?)\s+by\s+(.+)",
        lambda m: _intent(
            f"play {m.group(1).strip()} by {m.group(2).strip()}",
            "play_music",
            {"track": m.group(1).strip(), "artist": m.group(2).strip()},
        ),
    )
    # generic "play X" — song name (2–80 chars, not already matched above)
    _rule(
        r"play\s+(.{2,80})",
        lambda m: _intent(
            f"play {m.group(1).strip()}",
            "spotify_play_song",
            {"song": m.group(1).strip()},
        ),
    )

    # ─── Volume ───────────────────────────────────────────────────────────
    _rule(
        r"(?:set\s+(?:the\s+)?volume\s+to\s+|volume\s+(?:to\s+)?)(\d{1,3})(?:\s*(?:%|percent))?",
        lambda m: _intent(
            f"set volume to {m.group(1)}", "set_volume", {"level": int(m.group(1))}
        ),
    )
    # Relative, not absolute: "turn it up" jumping to a fixed 80% quietly turned
    # the volume *down* whenever it was already above that.
    _rule(
        r"(?:volume\s+up|turn\s+(?:(?:the\s+)?volume\s+)?up|louder|increase\s+(?:the\s+)?volume)",
        lambda m: _intent("volume up", "adjust_volume", {"delta": 10}),
    )
    _rule(
        r"(?:volume\s+down|turn\s+(?:(?:the\s+)?volume\s+)?down|quieter"
        r"|lower\s+(?:the\s+)?volume|decrease\s+(?:the\s+)?volume)",
        lambda m: _intent("volume down", "adjust_volume", {"delta": -10}),
    )
    _rule(
        r"(?:unmute(?:\s+(?:the\s+)?(?:sound|audio|volume|music))?)",
        lambda m: _intent("unmute", "mute_audio", {"muted": False}),
    )
    _rule(
        r"(?:mute(?:\s+(?:the\s+)?(?:sound|audio|volume|music))?|silence(?:\s+everything)?)",
        lambda m: _intent("mute", "mute_audio", {"muted": True}),
    )

    # ─── System info ──────────────────────────────────────────────────────
    _rule(
        r"(?:what(?:'s| is)\s+(?:the\s+)?(?:current\s+)?time"
        r"|time\s*(?:please)?|(?:tell\s+me\s+)?(?:the\s+)?(?:current\s+)?time"
        r"|clock|what\s+time(?:\s+is\s+it)?)",
        lambda m: _intent("get current time", "get_time", {}),
    )
    _rule(
        r"(?:(?:take\s+a?\s+)?screenshot|grab\s+a?\s+screenshot"
        r"|capture\s+(?:the\s+)?screen(?:shot)?)",
        lambda m: _intent("take screenshot", "take_screenshot", {}),
    )
    _rule(
        r"(?:lock\s+(?:the\s+)?(?:screen|computer|pc|workstation)|screen\s+lock)",
        lambda m: _intent("lock screen", "lock_screen", {}),
    )
    _rule(
        r"(?:system\s+(?:info|status|stats|information)"
        r"|how(?:'s| is)\s+(?:the\s+)?(?:system|cpu|ram|memory)"
        r"|cpu\s+(?:usage|stats?)|ram\s+(?:usage|stats?)|resource\s+usage)",
        lambda m: _intent("get system info", "get_system_info", {}),
    )

    # ─── The paired phone's state ─────────────────────────────────────────
    # A question about the phone's battery, charge or ringer, asked in any
    # order ("what's my phone battery looking like", "how much charge does my
    # phone have"). Deterministic because the model, shown both commands,
    # answered with get_system_info and read out the *laptop's* battery.
    # Only while a phone offering device.status is connected; otherwise the
    # rule steps aside and the model says the phone isn't there.
    _q = r"(?:what|how|is|does|has|did|check|tell\s+me|give\s+me)\b"
    _thing = r"(?:battery|charge|charged|charging|plugged\s+in|silent|vibrate|ringer)"
    _rule(
        _q + r".*\bphone(?:'?s)?\b.*\b" + _thing + r"\b.*"
        + r"|" + _q + r".*\b" + _thing + r"\b.*\bphone\b.*",
        lambda m: (_intent("phone status", "device.status", {})
                   if _device_offers("device.status") else None),
    )

    # ─── App open / close ─────────────────────────────────────────────────
    # "open X and play Y" is two steps: the planner splits it, so it is not
    # fast-pathed into open_app("x and play y").
    _rule(
        r"(?:open(?:\s+up)?|launch|start)\s+(.+)",
        lambda m: (None if re.search(r"\s(?:and|then)\s", m.group(1), re.I) else _intent(
            f"open {_clean_name(m.group(1))}",
            "open_app",
            {"name": _clean_name(m.group(1))},
        )),
    )
    _rule(
        r"(?:close|quit|exit|kill)\s+(.+)",
        lambda m: _intent(
            f"close {_clean_name(m.group(1))}",
            "close_app",
            {"name": _clean_name(m.group(1))},
        ),
    )

    # ─── PTT ──────────────────────────────────────────────────────────────
    _rule(
        r"(?:enable|turn\s+on|activate)\s+(?:push[\s-]?to[\s-]?talk|ptt)",
        lambda m: _intent("enable PTT", "set_ptt_mode", {"enabled": True}),
    )
    _rule(
        r"(?:disable|turn\s+off|deactivate)\s+(?:push[\s-]?to[\s-]?talk|ptt)",
        lambda m: _intent("disable PTT", "set_ptt_mode", {"enabled": False}),
    )

    # ─── Web / research ───────────────────────────────────────────────────
    _rule(
        r"(?:search(?:\s+(?:the\s+)?(?:web|internet|online|google))?\s+for\s+"
        r"|google\s+|look\s+up\s+|search\s+)(.{3,})",
        lambda m: _intent(
            f"search for {m.group(1).strip()}",
            "web_search",
            {"query": m.group(1).strip()},
        ),
    )

    # ─── Keyboard shortcuts ───────────────────────────────────────────────
    _rule(
        r"(?:press\s+|hit\s+)?alt[\s-]?tab",
        lambda m: _intent("switch window", "press_keys", {"keys": "alt+tab"}),
    )
    _rule(
        r"(?:press\s+)?(?:escape|esc)",
        lambda m: _intent("press escape", "press_keys", {"keys": "escape"}),
    )
    _rule(
        r"(?:press\s+)?enter",
        lambda m: _intent("press enter", "press_keys", {"keys": "enter"}),
    )

    # ─── Tasks and the day's agenda (Phase 5) ─────────────────────────────
    # "Remember I have to submit the assignment tomorrow", typed on the phone,
    # has to be a dated task that "what do I need to do today" finds the next
    # day on the laptop. Left to the model, "remember" went to chat or
    # quick_note (a file nothing reads back), and the day stayed a word.
    _rule(
        r"(?:remember|don'?t\s+(?:let\s+me\s+)?forget|note\s+down|make\s+a\s+note)\s+(?:that\s+)?"
        r"(?:i(?:\s+have|\s+need|\s+got|'ve\s+got|\s+ve\s+got|\s+have\s+got)\s+to\s+"
        r"|i\s+(?:must|should|gotta)\s+|to\s+)"
        r"(?P<what>.{3,200})",
        lambda m: _intent("add task", "add_task", {"title": m.group("what").strip()}),
    )
    _rule(
        r"add\s+(?:a\s+)?(?:task|to-?do)\s*(?::|to\s+)?\s*(?P<what>.{3,200})"
        r"|(?:add|put)\s+(?P<what2>.{3,200}?)\s+(?:to|on)\s+my\s+(?:to-?do\s+|task\s+)?list",
        lambda m: _intent("add task", "add_task",
                          {"title": (m.group("what") or m.group("what2")).strip()}),
    )
    # "Remind me in 1 minute to stretch": the model sent {"duration": "1 minute"}
    # here, which remind_me dropped, and the reminder landed at 5 minutes.
    n = r"(?P<n>\d+(?:\.\d+)?|an?|one|two|three|four|five|six|seven|eight|nine|ten|fifteen|twenty|thirty|forty-five)"
    unit = r"(?P<unit>seconds?|secs?|minutes?|mins?|hours?|hrs?)"
    _rule(
        r"(?:remind\s+me|set\s+(?:a\s+|an\s+)?(?:reminder|alarm\s+reminder))\s+(?:in|for|after)\s+"
        + n + r"\s+" + unit + r"\s+(?:to|that|about)\s+(?P<msg>.{2,200})",
        lambda m: _intent("set reminder", "remind_me",
                          {"message": m.group("msg").strip(), "duration": f"{m.group('n')} {m.group('unit')}"}),
    )
    _rule(
        r"remind\s+me\s+(?:to|about)\s+(?P<msg>.{2,200}?)\s+in\s+" + n + r"\s+" + unit,
        lambda m: _intent("set reminder", "remind_me",
                          {"message": m.group("msg").strip(), "duration": f"{m.group('n')} {m.group('unit')}"}),
    )
    from nora.days import _TOMORROW
    day = (r"(?:\s+(?:for\s+|on\s+)?(?P<day>today|tonight|" + _TOMORROW + r"|this\s+(?:morning|afternoon|evening)"
           r"|monday|tuesday|wednesday|thursday|friday|saturday|sunday))")
    # (head, whether the day may be left out, meaning today)
    for head, optional in (
        (r"what\s+do\s+i\s+(?:need|have|got)\s+to\s+(?:do|get\s+done|finish)", True),
        (r"what(?:'?s|\s+is)\s+on\s+(?:my\s+)?(?:plate|agenda|to-?do\s+list|list)", True),
        (r"what(?:'?s|\s+is)\s+(?:my\s+|the\s+)?(?:agenda|plan)", False),
        (r"(?:what(?:'?s|\s+is)|is\s+anything|anything)\s+due", False),
        (r"what\s+(?:have\s+i\s+got|do\s+i\s+have)(?:\s+on)?", False),
        (r"what\s+are\s+my\s+(?:tasks|to-?dos)", False),
    ):
        _rule(head + day + ("?" if optional else ""),
              lambda m: _intent("agenda", "agenda", {"day": (m.group("day") or "today").lower()}))

    # ─── Briefing ─────────────────────────────────────────────────────────
    # Deterministic because the planner would not hold still: asked to route
    # "what's new in the car world" it picked ask_claude, and "what's new in
    # quantum computing" tell_me_about, while routing the near-identical
    # "what's new in music" to show_briefing. These phrasings are a closed
    # set and mean exactly one thing, which is what this layer is for.
    #
    # The topic is passed through raw — briefing._core_topic() strips the
    # carrier words, so "the car world" and "cars" both land on the same
    # interest and an unknown subject still reaches Google News intact.
    _rule(
        r"(?:what(?:'?s|\s+is)?\s+(?:going\s+on|happening)(?:\s+in\s+the\s+world)?"
        r"|what(?:'?s|\s+is)?\s+(?:the\s+)?news(?:\s+today)?"
        r"|what(?:'?s|\s+is)?\s+(?:the\s+)?(?:latest\s+)?headlines?"
        r"|catch\s+me\s+up(?:\s+on\s+(?:the\s+)?(?:news|world))?"
        r"|bring\s+me\s+up\s+to\s+speed"
        r"|any\s+news|(?:the\s+)?(?:latest\s+)?headlines?)",
        lambda m: _intent("news briefing", "show_briefing", {"topic": ""}),
    )

    _rule(
        r"(?:what(?:'?s|\s+is)?\s+(?:new|happening|going\s+on)\s+(?:in|with|on|for|around)"
        r"|what(?:'?s|\s+is)?\s+the\s+latest\s+(?:in|with|on|from)"
        r"|any(?:thing)?\s+(?:new|news|happening)\s+(?:in|with|on|about|from))"
        r"\s+(?!you\b|u\b|ya\b|yourself\b|your\b)(?P<topic>.+)",
        lambda m: _intent("news briefing", "show_briefing",
                          {"topic": m.group("topic").strip()}),
    )
    # ─── Conversational ───────────────────────────────────────────────────
    _rule(
        r"(?:hey|hi|hello)(?:\s+(?:nora|there))?(?:\s+how\s+are\s+you)?[.!?]*",
        lambda m: _chat_varied("greeting", "Hello, sir."),
    )
    _rule(
        r"how\s+are\s+you(?:\s+doing)?[.?!]*",
        lambda m: _chat_varied("how_are_you", "Doing well, sir. What do you need?"),
    )
    _rule(
        r"(?:are\s+you\s+(?:there|awake|online|listening|ready)|you\s+there\??)[.?]*",
        lambda m: _chat_varied("presence", "Always here, sir."),
    )
    _rule(
        r"(?:thanks?\s*(?:you|a\s+lot|so\s+much)?|thank\s+you(?:\s+(?:very|so)\s+much)?"
        r"|cheers|appreciate\s+(?:it|that))[.!]*",
        lambda m: _chat_varied("thanks_reply", "Of course, sir."),
    )
    _rule(
        r"(?:never\s+mind(?:\s+that)?|forget\s+(?:it|that)|cancel\s+that|ignore\s+that)[.!]*",
        lambda m: _chat_varied("acknowledged", "Understood."),
    )

    _BUILT = True


def resolve(text: str) -> IntentResponse | None:
    """
    Attempt a deterministic resolution of *text* without calling the LLM.
    Returns IntentResponse on a confident match, or None to escalate to the LLM.
    """
    global _BUILT
    if not _BUILT:
        _build_rules()

    clean = _normalise(text)
    if len(clean) < 2:
        return None

    for pattern, fn in _RULES:
        m = pattern.match(clean)
        if m:
            try:
                result = fn(m)
                if result is not None:
                    return result
            except Exception:
                return None

    return None
