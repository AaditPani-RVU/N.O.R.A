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
    r"hey\s+nora[,.\s]+|okay\s+nora[,.\s]+|ok\s+nora[,.\s]+|nora[,.\s]+"
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
    t = _FILLER_RE.sub("", text.strip())
    t = _SUFFIX_RE.sub("", t)
    return t.rstrip(".,!?;").strip()


def _clean_name(raw: str) -> str:
    """Remove leading articles from an app/file name."""
    return re.sub(r"^(?:the|a|an)\s+", "", raw.strip(), flags=re.I).strip()


# ── Rule table ─────────────────────────────────────────────────────────────────

_RULES: list[tuple[re.Pattern, object]] = []
_BUILT = False


def _rule(pattern: str, fn):
    """Register a compiled, anchored, case-insensitive rule."""
    _RULES.append((re.compile("^(?:" + pattern + ")$", re.I), fn))


def _build_rules() -> None:
    global _BUILT

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

    # ─── App open / close ─────────────────────────────────────────────────
    _rule(
        r"(?:open(?:\s+up)?|launch|start)\s+(.+)",
        lambda m: _intent(
            f"open {_clean_name(m.group(1))}",
            "open_app",
            {"name": _clean_name(m.group(1))},
        ),
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
