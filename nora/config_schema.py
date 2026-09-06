"""Typed, validated views over the sections of `config.yaml` that get read most.

`config.yaml` is 34 KB across 46 top-level sections and reaches the program as a
bare dict, which means every read is an unchecked guess: `cfg.get("temprature")`
returns None and the caller quietly uses its own default forever. A typo in a
config file should be a startup error, not a behaviour change nobody can trace.

This is deliberately partial. Only the five sections the plan named — the ones
read on nearly every turn — are typed here; the other forty-one still come back
as dicts through `get_config()`, which keeps working unchanged. That is the
whole point of the design: there is no cutover, no broken intermediate state,
and a section becomes typed when someone next has reason to touch it. Adding one
means a dataclass here and a line in `SECTIONS`; nothing else moves.

Two rules make the unknown-key check safe to run against a live config:

  - Only declared sections are checked. An unknown key inside `speaker` is an
    error; the entire `ambient` section, still untyped, is not.
  - Unknown keys are an error, missing keys are not. Every field has a default,
    so a config that omits a section is valid and gets the defaults — which is
    what makes it possible to type a section without editing anyone's config.

Validation is about ranges and enums, not just types: `temperature: 10` parses
as a float and still ruins every completion, and `backend: kokro` is a valid
string that silently loses you speech.
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import Any, Mapping


class ConfigError(ValueError):
    """Raised for a config that cannot be used, with every problem at once.

    Every problem, not the first: fixing a config file one startup crash at a
    time is a bad way to spend a morning, and the errors are independent.
    """


# ── Sections ─────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class TimeoutsConfig:
    """Upper bounds on the three things that can hang a turn."""
    transcribe_sec: float = 30.0
    llm_sec: float = 45.0
    command_sec: float = 45.0


@dataclass(frozen=True)
class LLMConfig:
    """The single-provider settings, still used where the router is bypassed."""
    provider: str = "groq"
    model: str = "openai/gpt-oss-120b"
    base_url: str = "http://localhost:11434"
    json_mode: bool = True
    structured_output: bool = True
    temperature: float = 0.1
    max_tokens: int = 512


@dataclass(frozen=True)
class KokoroConfig:
    """Paths to the local Kokoro voice model. Only read when backend=kokoro."""
    model_path: str = "~/models/kokoro-v1.0.onnx"
    voices_path: str = "~/models/voices-v1.0.bin"
    voice: str = "bf_emma"


@dataclass(frozen=True)
class SpeakerConfig:
    """Voice output. `voice`/`rate` are edge-tts's; kokoro carries its own."""
    voice: str = "en-GB-SoniaNeural"
    rate: str = "+20%"
    backend: str = "kokoro"
    kokoro: KokoroConfig = field(default_factory=KokoroConfig)

    BACKENDS = ("kokoro", "edge", "piper", "pyttsx3")


@dataclass(frozen=True)
class TranscriberConfig:
    """Speech in, either through a remote API or a local faster-whisper."""
    backend: str = "remote"
    remote_model: str = "whisper-large-v3-turbo"
    base_url: str = "https://api.groq.com/openai/v1"
    api_key_env: str = "GROQ_API_KEY"
    timeout_sec: float = 10.0
    model_size: str = "distil-small.en"
    device: str = "cuda"
    compute_type: str = "float16"

    BACKENDS = ("remote", "local", "faster_whisper")


@dataclass(frozen=True)
class ModelEndpoint:
    """One model the router may call, as listed under a role.

    `name` is the label that shows up in the router log, so it has to be unique
    within a role or the log stops being able to tell you which one answered.
    """
    name: str = ""
    provider: str = ""
    base_url: str = ""
    api_key_env: str = ""
    model: str = ""
    extra_body: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class LLMRouterConfig:
    """Ordered fallback chains, one per role.

    Order is meaningful — it is the failover order — which is why roles hold
    tuples rather than sets, and why an empty chain is an error rather than a
    role that silently never answers.
    """
    roles: Mapping[str, tuple[ModelEndpoint, ...]] = field(default_factory=dict)


@dataclass(frozen=True)
class NoraConfig:
    """The typed sections. Untyped ones stay reachable via `get_config()`."""
    timeouts: TimeoutsConfig = field(default_factory=TimeoutsConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    speaker: SpeakerConfig = field(default_factory=SpeakerConfig)
    transcriber: TranscriberConfig = field(default_factory=TranscriberConfig)
    llm_router: LLMRouterConfig = field(default_factory=LLMRouterConfig)


SECTIONS = {
    "timeouts": TimeoutsConfig,
    "llm": LLMConfig,
    "speaker": SpeakerConfig,
    "transcriber": TranscriberConfig,
}


# ── Building ─────────────────────────────────────────────────────────────────

def _build(cls, raw: Any, path: str, errors: list[str]):
    """Instantiate a dataclass from a mapping, collecting problems as it goes.

    Returns the defaults on anything it cannot use, because the caller reports
    every error at the end and raising here would hide the rest of them.
    """
    if raw is None:
        return cls()
    if not isinstance(raw, Mapping):
        errors.append(f"{path}: expected a mapping, got {type(raw).__name__}")
        return cls()

    fields = {f.name: f for f in dataclasses.fields(cls)}
    unknown = [k for k in raw if k not in fields]
    for key in sorted(unknown):
        near = _closest(str(key), fields)
        hint = f" — did you mean {near!r}?" if near else ""
        errors.append(f"{path}.{key}: unknown setting{hint}")

    kwargs: dict[str, Any] = {}
    for name, f in fields.items():
        if name not in raw:
            continue
        value = raw[name]
        if dataclasses.is_dataclass(f.type) or name == "kokoro":
            kwargs[name] = _build(KokoroConfig, value, f"{path}.{name}", errors)
            continue
        coerced = _coerce(value, f, f"{path}.{name}", errors)
        if coerced is not _UNSET:
            kwargs[name] = coerced
    return cls(**kwargs)


_UNSET = object()


def _coerce(value: Any, f: dataclasses.Field, path: str, errors: list[str]):
    """Convert a YAML scalar to the field's declared type, or record why not.

    YAML types are close to Python's but not identical — `timeout_sec: 10` is an
    int where a float is wanted, and `30` and `30.0` should not behave
    differently — so ints widen to floats. Nothing else is coerced: a string
    where a number belongs is a mistake in the file, and quietly parsing it
    would defeat the purpose of declaring the type.
    """
    want = f.type if isinstance(f.type, type) else type(f.default)
    if want is float and isinstance(value, int) and not isinstance(value, bool):
        return float(value)
    # bool is a subclass of int, so it has to be rejected explicitly or
    # `json_mode: true` would satisfy an int field.
    if want is int and isinstance(value, bool):
        errors.append(f"{path}: expected an integer, got a boolean")
        return _UNSET
    if want in (str, int, float, bool) and not isinstance(value, want):
        errors.append(
            f"{path}: expected {want.__name__}, got {type(value).__name__} ({value!r})"
        )
        return _UNSET
    return value


def _closest(key: str, fields: Mapping[str, Any]) -> str | None:
    """Best single suggestion for a misspelled key, or None if none is close.

    A suggestion that is wrong is worse than no suggestion, so this only fires
    on a genuine near-miss rather than always naming the least-distant field.
    """
    import difflib
    matches = difflib.get_close_matches(key, list(fields), n=1, cutoff=0.7)
    return matches[0] if matches else None


def _build_router(raw: Any, errors: list[str]) -> LLMRouterConfig:
    """Build the role → endpoint-chain mapping.

    Roles are open-ended on purpose: they are named by whoever adds a call site,
    so an unrecognised role name is not an error the way an unrecognised
    *setting* is. The endpoints inside one are checked normally.
    """
    if raw is None:
        return LLMRouterConfig()
    if not isinstance(raw, Mapping):
        errors.append(f"llm_router: expected a mapping, got {type(raw).__name__}")
        return LLMRouterConfig()

    unknown = [k for k in raw if k != "roles"]
    for key in sorted(unknown):
        errors.append(f"llm_router.{key}: unknown setting — endpoints belong under 'roles'")

    roles_raw = raw.get("roles") or {}
    if not isinstance(roles_raw, Mapping):
        errors.append(f"llm_router.roles: expected a mapping, got {type(roles_raw).__name__}")
        return LLMRouterConfig()

    roles: dict[str, tuple[ModelEndpoint, ...]] = {}
    for role, entries in roles_raw.items():
        where = f"llm_router.roles.{role}"
        if not isinstance(entries, (list, tuple)):
            errors.append(f"{where}: expected a list of endpoints")
            continue
        if not entries:
            # An empty chain is a role that fails every call with no fallback
            # left to try — always a mistake, never a way to disable a role.
            errors.append(f"{where}: no endpoints — remove the role or give it one")
            continue
        built = tuple(
            _build(ModelEndpoint, e, f"{where}[{i}]", errors)
            for i, e in enumerate(entries)
        )
        seen: set[str] = set()
        for ep in built:
            if not ep.model:
                errors.append(f"{where}: endpoint {ep.name or '<unnamed>'!r} has no model")
            if ep.name and ep.name in seen:
                errors.append(f"{where}: duplicate endpoint name {ep.name!r}")
            seen.add(ep.name)
        roles[str(role)] = built
    return LLMRouterConfig(roles=roles)


def _validate(cfg: NoraConfig, errors: list[str]) -> None:
    """Range and enum checks — the mistakes that type-check and still break.

    Kept separate from building so the messages can say what a good value looks
    like rather than only what was wrong with this one.
    """
    t = cfg.timeouts
    for name, value in (("transcribe_sec", t.transcribe_sec),
                        ("llm_sec", t.llm_sec),
                        ("command_sec", t.command_sec)):
        if value <= 0:
            errors.append(f"timeouts.{name}: must be greater than 0, got {value}")

    if not 0.0 <= cfg.llm.temperature <= 2.0:
        errors.append(
            f"llm.temperature: must be between 0.0 and 2.0, got {cfg.llm.temperature}"
        )
    if cfg.llm.max_tokens <= 0:
        errors.append(f"llm.max_tokens: must be greater than 0, got {cfg.llm.max_tokens}")

    if cfg.speaker.backend not in SpeakerConfig.BACKENDS:
        errors.append(
            f"speaker.backend: {cfg.speaker.backend!r} is not one of "
            f"{', '.join(SpeakerConfig.BACKENDS)}"
        )
    if cfg.transcriber.backend not in TranscriberConfig.BACKENDS:
        errors.append(
            f"transcriber.backend: {cfg.transcriber.backend!r} is not one of "
            f"{', '.join(TranscriberConfig.BACKENDS)}"
        )
    if cfg.transcriber.timeout_sec <= 0:
        errors.append(
            f"transcriber.timeout_sec: must be greater than 0, "
            f"got {cfg.transcriber.timeout_sec}"
        )


def build_config(raw: Mapping[str, Any] | None) -> NoraConfig:
    """Turn the parsed YAML into `NoraConfig`, or raise `ConfigError`.

    Raises once with every problem listed, because these are independent and
    finding them one restart at a time is the slow way.
    """
    raw = raw or {}
    errors: list[str] = []
    built = {
        name: _build(cls, raw.get(name), name, errors)
        for name, cls in SECTIONS.items()
    }
    cfg = NoraConfig(llm_router=_build_router(raw.get("llm_router"), errors), **built)
    _validate(cfg, errors)
    if errors:
        listed = "\n".join(f"  - {e}" for e in sorted(errors))
        raise ConfigError(f"config.yaml has {len(errors)} problem(s):\n{listed}")
    return cfg
