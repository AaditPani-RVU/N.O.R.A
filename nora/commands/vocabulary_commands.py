"""Teaching NORA words she mishears (NORA_ASSIST_PLAN A)."""
from __future__ import annotations

from nora.command_engine import register


@register(
    "learn_word",
    sig="learn_word(word: str, heard: str = '')",
    description="Teach NORA a word she mishears. `word` is what the user meant, `heard` what "
                "she heard instead, if said: 'you misheard, I said Hormuz not vermouth' → "
                "learn_word('Hormuz', 'vermouth'). 'learn the word Deftones' → learn_word('Deftones')",
    category="system",
)
def learn_word(word: str, heard: str = "") -> str:
    from nora import vocabulary
    if word and word.islower() and not heard:
        word = word.title()
    return vocabulary.learn(word, heard)


@register(
    "forget_word",
    sig="forget_word(word: str)",
    description="Stop correcting or listening for a word taught with learn_word",
    category="system",
)
def forget_word(word: str) -> str:
    from nora import vocabulary
    return vocabulary.forget(word)


@register(
    "list_words",
    sig="list_words()",
    description="The words the user has taught NORA to hear",
    category="system",
)
def list_words() -> str:
    from nora import store
    rows = store.query("SELECT word, heard FROM vocabulary ORDER BY added_at DESC LIMIT 12")
    if not rows:
        return "You haven't taught me any words yet."
    parts = [f"{r['word']} (not {r['heard']})" if r["heard"] else r["word"] for r in rows]
    return "I've learned: " + ", ".join(parts) + "."
