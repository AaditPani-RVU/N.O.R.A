"""Pre-demo preflight — actually exercises every stage, then tells you if it's good.

``nora.health`` answers "is this installed?" in under a second: imports, file
presence, API keys.  That is the right check to run on every boot and the
wrong one to run before a demo, because everything it reports can be green on
a machine that cannot hear you.

This module answers the harder question — "would a real turn work, right now,
in this room?" — and pays for the answer.  It records you, transcribes what it
heard, synthesises speech, and calls each LLM candidate.  Budget ten to twenty
seconds.

Two things it does beyond reporting:

* **Warms the cold paths.**  Kokoro's ONNX session and the Whisper model are
  both loaded lazily on first use, so without this the first spoken turn of a
  demo pays a load it never pays again.  Running preflight moves that cost
  before the audience arrives.
* **Trips the router cooldowns.**  ``llm_router.roles.chat`` lists three cloud
  candidates ahead of the local model.  On a dead network the first turn walks
  that chain from the top, failing each in turn.  ``model_router`` remembers
  failures, so probing here means the demo's first turn goes straight to
  whatever actually answers.

Run it five minutes before you present:

    python -m nora.preflight
"""
from __future__ import annotations

import argparse
import logging
import math
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger("nora.preflight")

# The gate that decides whether NORA thinks you are talking. Hardcoded in
# Listener._record; mirrored here so the mic check measures you against the
# number that actually matters rather than an invented one.
VAD_RMS_THRESHOLD = 0.01

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"

_REPO_ROOT = Path(__file__).resolve().parent.parent


def load_env() -> None:
    """Load .env the way main.py does before probing anything.

    Preflight is run standalone (``python -m nora.preflight``), so it never
    goes through main()'s startup. Without this every cloud candidate reports
    "API key not set" and you go hunting for a problem that does not exist —
    a preflight that lies about your models is worse than no preflight.
    """
    import os

    env_file = _REPO_ROOT / ".env"
    if not env_file.exists():
        return
    try:
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())
    except Exception as e:
        logger.warning("preflight: could not read .env (%s)", e)

_MARK = {PASS: "  ok  ", WARN: " warn ", FAIL: " FAIL "}


class Result:
    """One stage's outcome, plus the lines to print under it."""

    def __init__(self, name: str, status: str, headline: str,
                 detail: list[str] | None = None, advice: str = "") -> None:
        self.name = name
        self.status = status
        self.headline = headline
        self.detail = detail or []
        self.advice = advice

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name, "status": self.status,
            "headline": self.headline, "detail": self.detail, "advice": self.advice,
        }


def _print(result: Result) -> None:
    print(f"[{_MARK[result.status]}] {result.name}: {result.headline}", flush=True)
    for line in result.detail:
        print(f"           {line}", flush=True)
    if result.advice:
        print(f"           -> {result.advice}", flush=True)


def _db(ratio: float) -> float:
    """Ratio to dB, floored so a digital-silence buffer can't return -inf."""
    return 20.0 * math.log10(max(ratio, 1e-9))


# ── 1. Microphone ────────────────────────────────────────────────────────────

def check_microphone(seconds: float = 4.0) -> tuple[Result, Any]:
    """Record the user and measure them against NORA's own speech gate.

    Returns the result and the captured audio, so the STT stage can transcribe
    the same take rather than asking the user to repeat themselves.
    """
    try:
        import numpy as np
        import sounddevice as sd
    except ImportError as e:
        return Result("microphone", FAIL, f"audio stack unavailable ({e})"), None

    try:
        device = sd.query_devices(kind="input")
        device_name = device.get("name", "unknown")
    except Exception as e:
        return Result("microphone", FAIL, "no usable input device",
                      advice=f"sounddevice could not open an input: {e}"), None

    from nora.config import get_config
    sample_rate = int(get_config().get("listener", {}).get("sample_rate", 16000))

    print(f"\n  Recording {seconds:.0f}s from {device_name!r}.")
    print("  Talk to it the way you will during the demo — same distance, same volume.\n")
    for n in (3, 2, 1):
        print(f"    {n}...", end="\r", flush=True)
        time.sleep(1.0)
    print("    speak now      ", flush=True)

    try:
        audio = sd.rec(int(seconds * sample_rate), samplerate=sample_rate,
                       channels=1, dtype="float32")
        sd.wait()
    except Exception as e:
        return Result("microphone", FAIL, "recording failed", advice=str(e)), None

    audio = audio.flatten()
    if audio.size == 0:
        return Result("microphone", FAIL, "captured no samples"), None

    # Block-wise RMS at the same 1024-frame granularity the listener uses, so
    # "fraction above threshold" means the same thing here as it does there.
    block = 1024
    n_blocks = audio.size // block
    if n_blocks == 0:
        return Result("microphone", FAIL, "recording too short to measure"), None
    blocks = audio[:n_blocks * block].reshape(n_blocks, block)
    rms_per_block = np.sqrt(np.mean(blocks ** 2, axis=1))

    peak = float(np.max(np.abs(audio)))
    above = rms_per_block >= VAD_RMS_THRESHOLD
    pct_above = 100.0 * float(np.mean(above))

    # Speech level = the loudest quartile of blocks. Averaging the whole take
    # would fold in the silence around the words and understate how loud you
    # actually are.
    loud = np.sort(rms_per_block)[max(0, int(n_blocks * 0.75)):]
    speech_rms = float(np.mean(loud)) if loud.size else 0.0
    headroom = _db(speech_rms / VAD_RMS_THRESHOLD)

    detail = [
        f"device        {device_name}",
        f"peak          {peak:.3f}" + ("   <- clipping" if peak >= 0.99 else ""),
        f"speech RMS    {speech_rms:.4f}  (gate is {VAD_RMS_THRESHOLD})",
        f"headroom      {headroom:+.1f} dB over the gate",
        f"registered    {pct_above:.0f}% of blocks counted as speech",
    ]

    if speech_rms < VAD_RMS_THRESHOLD:
        return Result(
            "microphone", FAIL,
            f"too quiet — {headroom:+.1f} dB, below NORA's speech gate", detail,
            "NORA will not hear this. Move closer, raise the input gain "
            "(pavucontrol / `wpctl set-volume`), or use a wired USB mic.",
        ), audio

    if headroom < 6.0 or pct_above < 25.0:
        return Result(
            "microphone", WARN,
            f"marginal — only {headroom:+.1f} dB over the gate", detail,
            "It will work in a quiet room and fail in a loud one. A wired USB "
            "lavalier is the reliable fix for a demo.",
        ), audio

    if peak >= 0.99:
        return Result(
            "microphone", WARN, "loud enough, but clipping", detail,
            "Lower the input gain — clipped audio transcribes worse than quiet audio.",
        ), audio

    return Result("microphone", PASS,
                  f"good — {headroom:+.1f} dB over the gate", detail), audio


# ── 2. Speech to text ────────────────────────────────────────────────────────

def check_stt(audio: Any, mic: Result | None = None) -> Result:
    """Transcribe the take from the mic check, and show what NORA heard.

    Also absorbs the Whisper model load, which is lazy in ``transcriber``.

    ``mic`` is used only to aim the advice when nothing comes back. A silent
    transcript means something different depending on whether the level was
    low or high, and a diagnostic that gives the wrong fix is worse than one
    that gives none.
    """
    if audio is None:
        return Result("speech-to-text", WARN, "skipped — no audio captured")

    try:
        from nora import transcriber
    except ImportError as e:
        return Result("speech-to-text", FAIL, f"transcriber unavailable ({e})")

    from nora.config import get_config
    model_size = get_config().get("transcriber", {}).get("model_size", "base.en")

    started = time.monotonic()
    try:
        text = transcriber.transcribe(audio)
    except Exception as e:
        return Result("speech-to-text", FAIL, "transcription raised",
                      [f"model  {model_size}"], str(e))
    elapsed = (time.monotonic() - started) * 1000

    detail = [f"model         {model_size}", f"took          {elapsed:.0f} ms"]

    if not text.strip():
        if mic is not None and mic.status == PASS:
            # Plenty of signal, no words in it. Being closer would not help.
            advice = ("Signal was strong but unintelligible. Either nobody spoke, "
                      "or the input device is picking up noise rather than you — "
                      "check what 'default' actually resolves to in pavucontrol.")
        else:
            advice = ("Level was already marginal, and Whisper got nothing from it. "
                      "Move closer, raise the gain, or use a wired USB mic.")
        return Result("speech-to-text", FAIL, "heard nothing", detail, advice)

    detail.append(f'heard         "{text}"')
    return Result("speech-to-text", PASS, f"transcribed in {elapsed:.0f} ms", detail,
                  "Read that back — if it is not what you said, fix the mic before the demo.")


# ── 3. Text to speech ────────────────────────────────────────────────────────

def check_tts(play: bool = True) -> Result:
    """Synthesise a line, timing the cold load, and optionally play it.

    The load is the point as much as the timing: Kokoro's ONNX session is
    built on first use, so this is what stops the demo's opening line from
    being the slowest one.
    """
    from nora.config import get_config
    cfg = get_config().get("speaker", {})
    backend = cfg.get("backend", "edge")
    rate = cfg.get("rate", "+20%")
    line = "Preflight complete. Ready when you are."

    out = str(Path(tempfile.gettempdir()) / "nora_preflight_tts.wav")

    if backend != "kokoro":
        return Result(
            "text-to-speech", WARN, f"backend is {backend!r}, not local",
            [f"backend       {backend}"],
            "edge-tts needs the network on every single reply. Switch "
            "speaker.backend to 'kokoro' before demoing.",
        )

    try:
        from nora import tts_local
    except ImportError as e:
        return Result("text-to-speech", FAIL, f"tts_local unavailable ({e})")

    # Kokoro can decline for two very different reasons and the fix differs,
    # so establish which before synthesising rather than guessing after.
    try:
        import kokoro_onnx  # noqa: F401
    except ImportError:
        return Result(
            "text-to-speech", FAIL, "kokoro_onnx is not installed",
            [f"backend       {backend} (configured)"],
            "pip install kokoro-onnx — or you are running the wrong "
            "interpreter. NORA's deps live in .venv.",
        )

    kcfg = get_config().get("speaker", {}).get("kokoro", {})
    missing = [
        str(Path(kcfg.get(key, default)).expanduser())
        for key, default in (("model_path", "~/models/kokoro-v1.0.onnx"),
                             ("voices_path", "~/models/voices-v1.0.bin"))
        if not Path(kcfg.get(key, default)).expanduser().exists()
    ]
    if missing:
        return Result(
            "text-to-speech", FAIL, "kokoro model files missing",
            [f"missing       {m}" for m in missing],
            "Download the Kokoro model and voices (see STACK.md), or set "
            "speaker.backend to 'edge' and accept the network dependency.",
        )

    started = time.monotonic()
    try:
        ok = tts_local.synth_if_enabled(line, rate, out)
    except Exception as e:
        return Result("text-to-speech", FAIL, "synthesis raised", advice=str(e))
    cold_ms = (time.monotonic() - started) * 1000

    if not ok:
        return Result(
            "text-to-speech", FAIL, "kokoro declined despite deps and models being present",
            [f"backend       {backend}"],
            "Check the nora.tts_local log line for the underlying error — "
            "every reply would fall back to edge-tts and need the network.",
        )

    # Second pass with the session already built — this is the number that
    # represents every reply after the first.
    started = time.monotonic()
    tts_local.synth_if_enabled(line, rate, out)
    warm_ms = (time.monotonic() - started) * 1000

    detail = [
        f"backend       kokoro (local, no network)",
        f"cold synth    {cold_ms:.0f} ms   <- paid once, now paid",
        f"warm synth    {warm_ms:.0f} ms   <- every reply after that",
    ]

    if play:
        try:
            import pygame
            pygame.mixer.init()
            sound = pygame.mixer.Sound(out)
            sound.play()
            while pygame.mixer.get_busy():
                pygame.time.wait(50)
            detail.append("played        yes — if you heard that, output works")
        except Exception as e:
            detail.append(f"played        no ({e})")

    return Result("text-to-speech", PASS, f"warm in {warm_ms:.0f} ms", detail)


# ── 4. LLM chain ─────────────────────────────────────────────────────────────

def check_llm(role: str = "chat", timeout_sec: float = 12.0) -> Result:
    """Probe every candidate for a role in order, and report who answers.

    ``model_router.complete`` stops at the first success, which is right for
    serving and useless for diagnosis — it cannot tell you the three ahead of
    it are dead. This walks the whole chain so you know what the demo will
    actually be talking to, and leaves the failures in cooldown so the first
    real turn skips them.
    """
    try:
        from nora import model_router
    except ImportError as e:
        return Result("llm", FAIL, f"model_router unavailable ({e})")

    candidates = model_router._candidates_for(role)
    if not candidates:
        return Result("llm", FAIL, f"no candidates configured for role {role!r}")

    messages = [
        {"role": "system", "content": "Reply with exactly one short sentence."},
        {"role": "user", "content": "Say ready."},
    ]

    # Must match what a real chat turn budgets. Probing with a small number
    # looks thrifty and is actively misleading: the gpt-oss and nemotron
    # models spend tokens on hidden reasoning before emitting any content, so
    # a tight ceiling returns an empty string and the probe reports a healthy
    # endpoint as dead. Measured on this machine: at 24 tokens both Groq
    # candidates return "", at 128 they return "Ready." in ~515 ms.
    max_tokens = 240

    detail: list[str] = []
    winner: str | None = None
    winner_ms = 0.0

    for candidate in candidates:
        name = candidate.get("name", candidate.get("model", "?"))
        started = time.monotonic()
        try:
            if candidate.get("provider") == "ollama":
                text = model_router._call_ollama(
                    candidate, messages, max_tokens, 0.3, timeout_sec)
            else:
                text = model_router._call_openai_compatible(
                    candidate, messages, max_tokens, 0.3, timeout_sec)
            elapsed = (time.monotonic() - started) * 1000
            if not text.strip():
                detail.append(
                    f"{name:24} reachable but returned nothing  ({elapsed:.0f} ms)")
                continue
            detail.append(f"{name:24} ok  {elapsed:.0f} ms" +
                          ("   <- the demo will use this" if winner is None else ""))
            if winner is None:
                winner, winner_ms = name, elapsed
        except Exception as e:
            elapsed = (time.monotonic() - started) * 1000
            reason = str(e).splitlines()[0][:60]
            detail.append(f"{name:24} FAILED ({elapsed:.0f} ms) {reason}")

    if winner is None:
        return Result(
            "llm", FAIL, "every candidate failed", detail,
            "Nothing will answer. Check the network, or start Ollama "
            "(`ollama serve`) so the local candidate can carry the demo.",
        )

    is_local = any(c.get("provider") == "ollama"
                   for c in candidates if c.get("name") == winner)

    if winner_ms > 3000:
        return Result("llm", WARN, f"{winner} answers, but in {winner_ms:.0f} ms", detail,
                      "That is slow enough to feel broken on stage.")

    if is_local:
        return Result("llm", PASS, f"{winner} (local) in {winner_ms:.0f} ms", detail,
                      "Running local — network problems on the day cannot touch this.")

    providers = {c.get("provider") for c in candidates if c.get("name") in
                 {line.split()[0] for line in detail if " ok " in line}}
    if len(providers) < 2:
        return Result("llm", WARN, f"{winner} in {winner_ms:.0f} ms", detail,
                      "Only one provider is answering, so this chain has no real "
                      "fallback — a single outage takes the whole demo down.")

    return Result("llm", PASS, f"{winner} in {winner_ms:.0f} ms", detail,
                  "Cloud-only chain, but more than one provider is answering, so "
                  "a single outage will not take the demo down. It still needs "
                  "the network — tether rather than trust venue wifi.")


# ── 5. Model catalogue ───────────────────────────────────────────────────────

def check_models(timeout_sec: float = 8.0) -> Result:
    """Check every configured model name still exists on its provider.

    Providers retire models without warning — `llama-3.3-70b-versatile`
    vanished from Groq and the first anyone heard of it was NORA saying "I
    couldn't summarise the results right now" out loud, mid-conversation. One
    catalogue call per provider catches the whole class of failure before the
    demo does. Roles that check_llm never probes (live_search, research,
    vision) are exactly where a dead model hides longest.
    """
    import os
    import requests

    from nora.config import get_config

    cfg = get_config()
    # model name -> where it is configured, so the failure names the fix
    wanted: dict[tuple[str, str, str], list[str]] = {}

    def want(base_url: str, key_env: str, model: str, where: str) -> None:
        if model:
            wanted.setdefault((base_url, key_env, model), []).append(where)

    llm = cfg.get("llm", {})
    if llm.get("provider") == "groq":
        want(llm.get("api_base") or "https://api.groq.com/openai/v1",
             llm.get("api_key_env", "GROQ_API_KEY"), llm.get("model", ""), "llm.model")

    for role, candidates in (cfg.get("llm_router", {}).get("roles", {}) or {}).items():
        for c in candidates or []:
            want(c.get("base_url", ""), c.get("api_key_env", ""), c.get("model", ""),
                 f"llm_router.{role}")

    want("https://api.groq.com/openai/v1", "GROQ_API_KEY",
         cfg.get("screen_intelligence", {}).get("vision_model", ""), "screen_intelligence")
    want("https://api.groq.com/openai/v1", "GROQ_API_KEY",
         cfg.get("transcriber", {}).get("remote_model", ""), "transcriber")

    # One catalogue fetch per (host, key) — not one per model.
    catalogues: dict[tuple[str, str], set[str] | None] = {}
    for base_url, key_env, _model in wanted:
        host = (base_url, key_env)
        if host in catalogues:
            continue
        key = os.environ.get(key_env, "") if key_env else ""
        if key_env and not key:
            catalogues[host] = None
            continue
        try:
            resp = requests.get(f"{base_url.rstrip('/')}/models",
                                headers={"Authorization": f"Bearer {key}"},
                                timeout=timeout_sec)
            resp.raise_for_status()
            catalogues[host] = {m["id"] for m in resp.json().get("data", [])}
        except Exception as e:
            logger.debug("preflight: model list failed for %s (%s)", base_url, e)
            catalogues[host] = None

    detail: list[str] = []
    missing: list[str] = []
    unchecked = 0
    for (base_url, key_env, model), where in sorted(wanted.items(), key=lambda kv: kv[0][2]):
        known = catalogues.get((base_url, key_env))
        used_by = ", ".join(sorted(set(where)))
        if known is None:
            unchecked += 1
            continue
        if model in known:
            detail.append(f"{model:34} ok        {used_by}")
        else:
            detail.append(f"{model:34} RETIRED   {used_by}")
            missing.append(f"{model} ({used_by})")

    if missing:
        return Result(
            "models", FAIL, f"{len(missing)} configured model(s) no longer exist", detail,
            "Pick a replacement from the provider's catalogue and update "
            "config.yaml. Every call routed to these fails with a 404 that "
            "NORA reports as a vague apology.",
        )
    if not detail:
        return Result("models", WARN, "no provider catalogue could be read", detail,
                      "Check the API keys in .env — model names went unverified.")
    if unchecked:
        return Result("models", WARN, f"{len(detail)} model(s) exist, {unchecked} unverified",
                      detail, "Some providers had no key or did not answer.")
    return Result("models", PASS, f"all {len(detail)} configured models exist", detail)


# ── Runner ───────────────────────────────────────────────────────────────────

def run(seconds: float = 4.0, play: bool = True, skip_audio: bool = False) -> list[Result]:
    """Run the full sweep in pipeline order and print as it goes."""
    load_env()
    print("\nNORA preflight — exercising each stage for real. Takes ~20s.\n")
    results: list[Result] = []

    if skip_audio:
        print("[" + _MARK[WARN] + "] microphone: skipped (--no-audio)", flush=True)
        print("[" + _MARK[WARN] + "] speech-to-text: skipped (--no-audio)", flush=True)
        audio = None
    else:
        mic, audio = check_microphone(seconds)
        _print(mic)
        results.append(mic)
        stt = check_stt(audio, mic)
        _print(stt)
        results.append(stt)

    tts = check_tts(play=play and not skip_audio)
    _print(tts)
    results.append(tts)

    llm = check_llm()
    _print(llm)
    results.append(llm)

    models = check_models()
    _print(models)
    results.append(models)

    return results


def verdict(results: list[Result]) -> str:
    failed = [r for r in results if r.status == FAIL]
    warned = [r for r in results if r.status == WARN]

    print()
    if failed:
        names = ", ".join(r.name for r in failed)
        print(f"NOT READY — {names} will break the demo. Fix before presenting.")
        print("Fallback if you run out of time: `text_input` reads stdin, so you")
        print("can type commands into the same queue the mic feeds.")
        return FAIL
    if warned:
        names = ", ".join(r.name for r in warned)
        print(f"USABLE, with risk — {names}. It will work in a quiet room.")
        return WARN
    print("READY — every stage exercised and healthy. Models are warm.")
    return PASS


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="python -m nora.preflight",
        description="Exercise every NORA stage for real before a demo.",
    )
    parser.add_argument("--seconds", type=float, default=4.0,
                        help="how long to record for the mic check (default 4)")
    parser.add_argument("--no-play", action="store_true",
                        help="synthesise but do not play the test line")
    parser.add_argument("--no-audio", action="store_true",
                        help="skip mic and playback; just warm the models and probe the LLMs")
    args = parser.parse_args()

    logging.basicConfig(level=logging.ERROR, format="%(message)s")

    results = run(seconds=args.seconds, play=not args.no_play, skip_audio=args.no_audio)
    return 1 if verdict(results) == FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
