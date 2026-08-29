"""Skills — procedural memory in Markdown, not Python.

Every capability NORA has is currently a Python function: 33 modules in
`nora/commands/` and a handful of plugins, each one a decorated handler that
had to be written, imported and registered. That is the right shape for *new
verbs* — talking to Spotify's API, driving AT-SPI — and the wrong shape for
*procedures*, which are almost always just existing verbs in a particular
order with particular judgement about the order.

"Wind down for the night" is not a new capability. It is: check tomorrow's
calendar, set the alarm from the first meeting, pause music, dim the screen,
turn on Do Not Disturb. Everything in that list already exists. Writing it as
a command means writing Python to do nothing but call other Python.

A skill is that procedure as a Markdown file, in the agentskills.io format —
YAML frontmatter plus prose — which is the same format already in this repo's
`.agents/skills` and `skills-lock.json`, so a skill written for Claude Code and
a skill written for NORA are the same artifact.

Loading is progressive, which is the part that matters for a voice assistant.
Only `name` and `description` ever go into the intent-parser prompt — a couple
of dozen tokens each. The body is read off disk *after* the model has chosen
the skill, then handed back for execution as a tool script. Fifty skills cost
one short list in the prompt rather than fifty procedures.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("nora.skills")

_ROOT = Path(__file__).resolve().parent.parent

# Searched in order; the first hit for a given name wins. `~/.nora/skills` comes
# first so a user's own version of a skill shadows a shared one rather than
# colliding with it, and it is where newly written skills land.
#
# `.agents/skills` is included so a procedure written for Claude Code and one
# written for NORA are the same file. The cost of that sharing is that
# Claude-Code-only skills (design, code review) also appear in the voice
# catalogue as one line each — harmless, since their descriptions don't match
# spoken commands, but `skills.dirs` in config.yaml can drop it.
_DEFAULT_SKILL_DIRS = ("~/.nora/skills", str(_ROOT / ".agents" / "skills"))


def skill_dirs() -> tuple[Path, ...]:
    """Directories to search, from config, falling back to the defaults."""
    try:
        from nora.config import get_config
        configured = get_config().get("skills", {}).get("dirs")
    except Exception:
        configured = None
    raw = configured or _DEFAULT_SKILL_DIRS
    # Relative entries in config.yaml resolve against the repo, not the working
    # directory — NORA is routinely started from elsewhere (systemd, autostart).
    return tuple(
        path if (path := Path(str(d)).expanduser()).is_absolute() else _ROOT / path
        for d in raw
    )


# Kept as a module attribute for tests to point at a temporary directory.
SKILL_DIRS: tuple[Path, ...] | None = None

_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n(.*)\Z", re.DOTALL)
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


@dataclass
class Skill:
    name: str
    description: str
    body: str
    path: Path

    def prompt_line(self) -> str:
        return f"- {self.name}: {self.description}"


_cache: dict[str, Skill] | None = None


def _parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Pull `key: value` pairs out of a SKILL.md header.

    Deliberately not a YAML parse. The frontmatter of a skill is a flat
    string-to-string map by convention, and reaching for PyYAML here would mean
    a skill file could execute arbitrary constructors on load — which is a poor
    trade for a format whose entire schema is two keys.
    """
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}, text
    meta: dict[str, str] = {}
    for line in m.group(1).splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, _, value = line.partition(":")
        meta[key.strip().lower()] = value.strip().strip("'\"")
    return meta, m.group(2).strip()


def _load_one(path: Path) -> Skill | None:
    try:
        text = path.read_text(encoding="utf-8")
    except Exception as e:
        logger.warning("Could not read skill %s: %s", path, e)
        return None

    meta, body = _parse_frontmatter(text)
    name = meta.get("name") or path.parent.name
    description = meta.get("description", "").strip()

    if not _NAME_RE.match(name):
        logger.warning("Skipping skill with unusable name %r (%s)", name, path)
        return None
    if not description:
        logger.warning("Skipping skill %r: no description, so the model can't choose it", name)
        return None
    if not body:
        logger.warning("Skipping skill %r: empty body", name)
        return None
    return Skill(name=name, description=description, body=body, path=path)


def discover(force: bool = False) -> dict[str, Skill]:
    """Find every skill on disk. Cached; pass force=True after writing one."""
    global _cache
    if _cache is not None and not force:
        return _cache

    found: dict[str, Skill] = {}
    for base in (SKILL_DIRS if SKILL_DIRS is not None else skill_dirs()):
        if not base.is_dir():
            continue
        for skill_md in sorted(base.glob("*/SKILL.md")):
            skill = _load_one(skill_md)
            if skill is None or skill.name in found:
                continue
            found[skill.name] = skill
    _cache = found
    logger.info("Discovered %d skill(s)", len(found))
    return found


def get(name: str) -> Skill | None:
    """Look up a skill by exact name, then by fuzzy spoken match.

    The fuzzy pass exists because these names arrive through Whisper. A skill
    called `wind-down` will be transcribed "wind down" every time.
    """
    skills = discover()
    if name in skills:
        return skills[name]

    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    if slug in skills:
        return skills[slug]

    words = {w for w in re.split(r"[^a-z0-9]+", name.lower()) if w}
    best: tuple[int, Skill] | None = None
    for skill in skills.values():
        skill_words = set(skill.name.split("-"))
        overlap = len(words & skill_words)
        if overlap and (best is None or overlap > best[0]):
            best = (overlap, skill)
    return best[1] if best else None


def catalogue() -> str:
    """The name+description block for the intent-parser prompt.

    This is the *only* thing skills contribute to the prompt. Bodies stay on
    disk until one is chosen.
    """
    skills = discover()
    if not skills:
        return ""
    lines = [s.prompt_line() for s in sorted(skills.values(), key=lambda s: s.name)]
    return "Skills (invoke with run_skill(name)):\n" + "\n".join(lines)


def write(name: str, description: str, body: str) -> Skill | None:
    """Create or overwrite a skill on disk.

    This is the half that compounds: NORA works out how to do something once,
    then writes down the procedure so the next time costs a lookup instead of
    a fresh derivation.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    if not _NAME_RE.match(slug):
        return None

    base = (SKILL_DIRS if SKILL_DIRS is not None else skill_dirs())[0]
    try:
        (base / slug).mkdir(parents=True, exist_ok=True)
        path = base / slug / "SKILL.md"
        path.write_text(
            f"---\nname: {slug}\ndescription: {description.strip()}\n---\n\n"
            f"{body.strip()}\n",
            encoding="utf-8",
        )
    except Exception as e:
        logger.error("Could not write skill %s: %s", slug, e)
        return None

    discover(force=True)
    logger.info("Wrote skill %s to %s", slug, path)
    return get(slug)
