"""Pick the few actions an utterance could need (Sharp Phase C).

The intent prompt used to carry every registered action: 223 names, and their
signatures cut off at 4,000 characters, so the model saw the parameters of
the first sixth and only the names of the rest. That cost ~5k tokens a turn
on a free tier allowing 8k a minute, and the cut-off tools were planned
blind. Now the prompt carries the actions this module picks: about fifteen,
each in full.

Each action is scored two ways against the utterance:

  * words: its name, its aliases below, its description and signature,
    BM25-weighted so a rare word ("bluetooth") outweighs a common one;
  * meaning: MiniLM cosine between the utterance and the same text, when the
    embedder cognitive memory already loads is available (it is optional, and
    the word score works alone).

The top `K` by the blend are joined by `CORE`, which every turn gets: the
actions the prompt's own rules name as the answer to open questions, so a
question no tool matches still has somewhere to go. Follow-ups ("no, make
it one minute") keep the actions of the turn before.

`python -m nora.evals` checks the result: for each labelled case, is the
expected action among those picked (`retrieval` in the report).
"""
from __future__ import annotations

import logging
import math
import re
import threading
from collections import Counter

logger = logging.getLogger("nora.tool_retrieval")

# Picked by retrieval, on top of CORE.
K = 12

# The rules in the prompt route open questions, places, weather, the screen
# and "stop" to these; without them every unmatched question would be stuck.
CORE = ("ask_claude", "tell_me_about", "show_location", "deep_reasoning",
        "read_screen", "stop_all", "get_weather")

# Words people say for an action that its name and description don't contain.
# Mostly the actions registered without a description, and slang.
ALIASES: dict[str, str] = {
    "get_time": "time clock hour date day today what time is it",
    "lock_screen": "lock computer laptop screen",
    "close_app": "close quit exit kill app window",
    "open_app": "open launch start run app program",
    "take_screenshot": "screenshot screen capture grab",
    "list_files": "files folder directory show list",
    "delete_file": "delete remove erase trash file folder everything",
    "move_file": "move rename file",
    "create_file": "create new make file write",
    "media_play_pause": "pause resume play stop song music track spotify",
    "media_next": "next skip song track",
    "media_previous": "previous back last song track",
    "pause_music": "pause stop music song",
    "resume_music": "resume continue play again music song",
    "play_music": "play music song track artist band put on lay",
    "play_on_phone": "play music song phone spotify playlist shuffle downloads lay",
    "phone.media_control": "pause resume play skip next song phone music",
    "spotify_play_song": "play song track",
    "spotify_play_artist": "play artist band singer",
    "spotify_play_playlist": "play playlist",
    "now_playing": "what song playing now current track",
    "set_volume": "volume loud louder quiet quieter sound",
    "adjust_volume": "volume up down louder quieter",
    "mute_audio": "mute unmute silence sound",
    "remind_me": "remind reminder minutes later alert",
    "schedule_task": "schedule every daily morning evening at oclock remind",
    "add_task": "task todo to-do remember assignment deadline homework submit",
    "agenda": "agenda roster plan today schedule what do we have to do",
    "show_briefing": "news headlines briefing world issues going on caught up",
    "inject_knowledge": "remember know information fact about me my name",
    "recall": "recall remember said earlier past note what did i say name personal fact",
    "semantic_recall": "recall remember name personal fact",
    "click_element": "click press tap button bar field",
    "fill_field": "type fill enter field form text",
    "explain_error": "explain error bug traceback exception what went wrong",
    "denoise_mic": "denoise denoising noise suppression microphone mic",
    "open_url": "open link url website page guide",
    "phone.open_url": "open link url website page phone",
    "phone.read_notifications": "notifications messages texts alerts important",
    "device.status": "phone battery charging network ringer",
    "navigate_to": "go to going directions route trip drive take me",
    "web_search": "search google look up browser",
    "stop_all": "stop cancel shut up quiet enough halt",
}

_STOP = frozenset((
    "a an the to of and or in on at for with my me i you your it is are be can do does "
    "please nora just now this that what whats some any about tell want would could should "
    "will im its it's let lets hey ok okay so then but set get make".split()))

_WORD = re.compile(r"[a-z0-9]+")


def _stem(w: str) -> str:
    for suf in ("ing", "ies", "es", "ed", "s"):
        if len(w) > len(suf) + 2 and w.endswith(suf):
            return w[: -len(suf)] + ("y" if suf == "ies" else "")
    return w


def _words(text: str) -> list[str]:
    return [_stem(w) for w in _WORD.findall(text.lower().replace("_", " ").replace(".", " "))
            if len(w) > 1 and w not in _STOP]


class _Index:
    """Word and meaning vectors for one set of actions. Rebuilt when the
    registry changes (a phone connecting adds its capabilities)."""

    def __init__(self, docs: dict[str, tuple[str, str]]):
        # docs: action -> (name-ish text weighted up, the rest)
        self.names = sorted(docs)
        self.tf: dict[str, Counter] = {}
        df: Counter = Counter()
        for name in self.names:
            strong, rest = docs[name]
            tf = Counter(_words(strong) * 3 + _words(rest))
            self.tf[name] = tf
            df.update(tf.keys())
        n = len(self.names)
        self.idf = {w: math.log(1 + (n - d + 0.5) / (d + 0.5)) for w, d in df.items()}
        self.lens = {name: sum(tf.values()) for name, tf in self.tf.items()}
        self.avg = (sum(self.lens.values()) / n) if n else 1.0
        self.texts = {name: f"{docs[name][0]}. {docs[name][1]}" for name in self.names}
        self.vecs = None                        # filled lazily by _semantic

    def lexical(self, query: list[str]) -> dict[str, float]:
        k1, b = 1.2, 0.75
        out: dict[str, float] = {}
        q = set(query)
        for name in self.names:
            tf, s = self.tf[name], 0.0
            for w in q:
                f = tf.get(w)
                if f:
                    s += self.idf[w] * f * (k1 + 1) / (f + k1 * (1 - b + b * self.lens[name] / self.avg))
            if s:
                out[name] = s
        return out


_lock = threading.Lock()
_index: _Index | None = None
_index_key: tuple = ()
_embed_ok = True
_vec_cache: dict = {}


def _docs() -> dict[str, tuple[str, str]]:
    from nora import command_engine
    docs = {}
    for name in command_engine.get_available_actions():
        if name.startswith("mcp_"):
            continue
        meta = command_engine.get_action_meta(name)
        sig = meta.sig if meta else ""
        desc = meta.description if meta else ""
        strong = name.replace("_", " ").replace(".", " ") + " " + ALIASES.get(name, "")
        if meta and meta.device:
            strong += " phone"
        docs[name] = (strong, f"{desc} {sig} {meta.category if meta else ''}")
    return docs


def _get_index() -> _Index:
    global _index, _index_key
    from nora import command_engine
    key = tuple(command_engine.get_available_actions())
    with _lock:
        if _index is None or key != _index_key:
            _index, _index_key = _Index(_docs()), key
        return _index


def _embedder():
    global _embed_ok
    if not _embed_ok:
        return None
    try:
        from nora import cognitive_memory
        model = cognitive_memory._get_embedder()
    except Exception as e:
        logger.debug("embedder unavailable: %s", e)
        model = None
    if model is None:
        _embed_ok = False
    return model


def _semantic(index: _Index, text: str) -> dict[str, float]:
    model = _embedder()
    if model is None:
        return {}
    try:
        if index.vecs is None:
            # Kept across rebuilds: a phone connecting re-indexes, and only
            # its own capabilities need embedding, not all 220 again.
            missing = [index.texts[n] for n in index.names if index.texts[n] not in _vec_cache]
            if missing:
                for t, v in zip(missing, model.encode(missing, normalize_embeddings=True,
                                                      show_progress_bar=False)):
                    _vec_cache[t] = v
            import numpy as np
            index.vecs = np.stack([_vec_cache[index.texts[n]] for n in index.names])
        q = model.encode([text], normalize_embeddings=True, show_progress_bar=False)[0]
        sims = index.vecs @ q
        return {name: float(s) for name, s in zip(index.names, sims)}
    except Exception as e:
        logger.warning("tool retrieval: embedding failed (%s), words only", e)
        return {}


def scores(text: str) -> dict[str, float]:
    """Blended score per action, highest first. Meaning in [0, 1] (cosine),
    words scaled so the best match counts as much as a strong cosine."""
    index = _get_index()
    lex = index.lexical(_words(text))
    sem = _semantic(index, text)
    top = max(lex.values(), default=0.0) or 1.0
    out = {}
    for name in index.names:
        out[name] = sem.get(name, 0.0) + 0.6 * lex.get(name, 0.0) / top
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))


def select(text: str, *, previous: list[str] | None = None,
           previous_text: str = "", k: int = K) -> list[str]:
    """The actions to show the intent model for `text`: CORE, the `k` best
    matches, and the actions of the turn before (`previous`), plus the best
    few for what was said before (`previous_text`), for follow-ups."""
    from nora import command_engine
    available = set(command_engine.get_available_actions())
    picked: list[str] = [a for a in CORE if a in available]
    for name in scores(text):
        if len(picked) >= len(CORE) + k:
            break
        if name not in picked:
            picked.append(name)
    for name in previous or []:
        if name in available and name not in picked:
            picked.append(name)
    if previous_text:
        for name in list(scores(previous_text))[:3]:
            if name not in picked:
                picked.append(name)
    return picked


def warm() -> None:
    """Build the index and embed the actions ahead of the first turn."""
    try:
        _semantic(_get_index(), "warm up")
    except Exception as e:
        logger.debug("tool retrieval warm-up failed: %s", e)
