#!/usr/bin/env python3
"""
NORA Remote Mic Client — run this on your Mac.

Captures microphone audio and sends it to the NORA Windows machine for
transcription, intent parsing, and execution. TTS plays on Windows.

Usage
-----
    python nora_remote.py                  # Enter-to-speak mode (no extra deps)
    python nora_remote.py --ptt            # Hold Space to record
    python nora_remote.py --wakeword       # Always-on, say "hey Jarvis" to activate

IP is read from .env (NORA_HOST=192.168.0.3). No --host flag needed.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
DEPENDENCIES  (install on Mac with pip)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Always required:
    pip install sounddevice numpy requests

PTT mode (--ptt):
    pip install pynput
    ↳ Then grant Terminal Accessibility access:
      System Settings → Privacy & Security → Accessibility → Terminal ✓

Wakeword mode (--wakeword):
    pip install openwakeword
    ↳ Downloads the hey_jarvis model (~8 MB) on first run automatically.
    ↳ Grant Terminal Microphone access if macOS prompts you.

If sounddevice install fails on Mac:
    brew install portaudio
    pip install sounddevice
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""
from __future__ import annotations

import argparse
import os
import queue
import sys
import threading
import time
from pathlib import Path

import numpy as np
import requests

# Load .env from the same directory as this script (works on both Windows and Mac)
def _load_env() -> None:
    env_file = Path(__file__).parent / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, val = line.partition("=")
            os.environ.setdefault(key.strip(), val.strip())

_load_env()

try:
    import sounddevice as sd
except ImportError:
    print("sounddevice not installed.  Run:  pip install sounddevice")
    sys.exit(1)

# ── Audio constants ───────────────────────────────────────────────────────────
TARGET_RATE = 16_000        # Whisper expects 16 kHz
SILENCE_TIMEOUT = 0.70      # seconds of silence before auto-stop
SPEECH_THRESHOLD = 0.015    # RMS level to count as speech
SPEECH_START_TIMEOUT = 2.0  # give up if no speech within this window
MAX_DURATION = 15.0         # hard cap on recording length

# ── Stage display map ─────────────────────────────────────────────────────────
_STAGE = {
    "idle":        "Ready",
    "listening":   "Listening…",
    "transcribing":"Transcribing…",
    "thinking":    "Thinking…",
    "guarding":    "Checking…",
    "acting":      "Executing…",
    "speaking":    "Speaking…",
}


# ── Audio helpers ─────────────────────────────────────────────────────────────

def _native_device() -> tuple[int, int]:
    """Return (sample_rate, channels) for the default input device."""
    try:
        dev = sd.query_devices(kind="input")
        return int(dev["default_samplerate"]), max(1, dev["max_input_channels"])
    except Exception:
        return 44100, 1


def _to_16k_mono(audio: np.ndarray, native_rate: int, native_ch: int) -> np.ndarray:
    """Convert native recording to float32 mono 16 kHz."""
    # Mix to mono
    if native_ch > 1:
        audio = audio[:, 0] if audio.ndim > 1 else audio
    else:
        audio = audio.flatten()
    # Resample if needed
    if native_rate != TARGET_RATE:
        ratio = TARGET_RATE / native_rate
        new_len = int(len(audio) * ratio)
        indices = np.linspace(0, len(audio) - 1, new_len).astype(int)
        audio = audio[indices]
    return audio.astype(np.float32)


def record_with_vad() -> np.ndarray | None:
    """Record mic until VAD silence; return float32 mono 16 kHz or None."""
    native_rate, native_ch = _native_device()
    frames: list[np.ndarray] = []
    silence_start: float | None = None
    speech_detected = False
    start = time.time()

    def cb(indata: np.ndarray, *_):
        frames.append(indata.copy())

    try:
        with sd.InputStream(
            samplerate=native_rate,
            channels=native_ch,
            dtype="float32",
            callback=cb,
            blocksize=1024,
        ):
            while True:
                time.sleep(0.05)
                elapsed = time.time() - start

                if elapsed >= MAX_DURATION:
                    break

                if frames:
                    chunk = frames[-1]
                    mono = chunk[:, 0] if chunk.ndim > 1 else chunk.flatten()
                    rms = float(np.sqrt(np.mean(mono ** 2)))

                    if rms >= SPEECH_THRESHOLD:
                        speech_detected = True
                        silence_start = None
                    else:
                        if not speech_detected:
                            if elapsed >= SPEECH_START_TIMEOUT:
                                return None
                        else:
                            if silence_start is None:
                                silence_start = time.time()
                            elif time.time() - silence_start >= SILENCE_TIMEOUT:
                                break
    except sd.PortAudioError as exc:
        _mic_error(exc)
        return None

    if not frames or not speech_detected:
        return None

    raw = np.concatenate(frames, axis=0)
    return _to_16k_mono(raw, native_rate, native_ch)


def _mic_error(exc: Exception) -> None:
    print(f"\n  Microphone error: {exc}")
    print("  Make sure Terminal has microphone access:")
    print("  System Preferences → Privacy & Security → Microphone → Terminal ✓")


# ── Network helpers ───────────────────────────────────────────────────────────

def send_audio(audio: np.ndarray, host: str, port: int) -> str | None:
    """POST raw float32 PCM to NORA. Returns transcription string or None."""
    try:
        resp = requests.post(
            f"http://{host}:{port}/audio",
            data=audio.tobytes(),
            headers={"Content-Type": "application/octet-stream"},
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json().get("transcription")
    except requests.ConnectionError:
        print(f"\n  Cannot reach NORA at {host}:{port}.")
        print("  Check the IP address and that NORA is running.")
        return None
    except requests.RequestException as exc:
        print(f"  Network error: {exc}")
        return None


def get_stage(host: str, port: int) -> str:
    try:
        resp = requests.get(f"http://{host}:{port}/ping", timeout=2)
        return resp.json().get("stage", "idle")
    except Exception:
        return "?"


def wait_for_idle(host: str, port: int, timeout: float = 30.0) -> None:
    """Poll until NORA returns to idle (done speaking/acting)."""
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        stage = get_stage(host, port)
        if stage != last:
            label = _STAGE.get(stage, stage)
            print(f"  NORA → {label}          ", end="\r", flush=True)
            last = stage
        if stage == "idle":
            print(" " * 40, end="\r")
            return
        time.sleep(0.2)


# ── Enter mode (no extra permissions needed) ──────────────────────────────────

def run_enter_mode(host: str, port: int) -> None:
    print("\n  Mode: Press Enter to speak — stops automatically on silence.")
    print("  Ctrl+C to quit.\n")
    try:
        while True:
            try:
                input("  [Enter to speak] ")
            except EOFError:
                break

            stage = get_stage(host, port)
            if stage not in ("idle", "?"):
                print(f"  NORA is busy ({_STAGE.get(stage, stage)}), please wait…")
                continue

            print("  Recording…  (speak now)", end="\r", flush=True)
            audio = record_with_vad()

            if audio is None:
                print("  (nothing recorded)           ")
                continue

            dur = len(audio) / TARGET_RATE
            print(f"  Sending {dur:.1f}s…              ", end="\r", flush=True)
            transcription = send_audio(audio, host, port)

            if transcription:
                print(f"  Heard: \"{transcription}\"          ")
                wait_for_idle(host, port)
            else:
                print("  (no speech recognised)        ")
            print()

    except KeyboardInterrupt:
        print("\n  Disconnected.")


# ── PTT mode (requires pynput + Accessibility permission) ────────────────────

def run_ptt_mode(host: str, port: int) -> None:
    try:
        from pynput import keyboard as kb
    except ImportError:
        print("  pynput not installed.  Run:  pip install pynput")
        print("  Falling back to Enter mode…\n")
        run_enter_mode(host, port)
        return

    print("\n  Mode: Hold SPACE to record — release to send.")
    print("  (If keys feel unresponsive, grant Terminal Accessibility access.)")
    print("  Ctrl+C to quit.\n")

    _held = threading.Event()
    _stop = threading.Event()
    native_rate, native_ch = _native_device()
    frames: list[np.ndarray] = []
    _lock = threading.Lock()

    def on_press(key):
        if key == kb.Key.space:
            _held.set()

    def on_release(key):
        if key == kb.Key.space:
            _held.clear()

    def audio_loop():
        def cb(indata: np.ndarray, *_):
            if _held.is_set():
                with _lock:
                    frames.append(indata.copy())
        try:
            with sd.InputStream(
                samplerate=native_rate,
                channels=native_ch,
                dtype="float32",
                callback=cb,
                blocksize=1024,
            ):
                _stop.wait()
        except sd.PortAudioError as exc:
            _mic_error(exc)

    t = threading.Thread(target=audio_loop, daemon=True)
    t.start()

    prev_held = False
    try:
        with kb.Listener(on_press=on_press, on_release=on_release, suppress=False):
            while True:
                now = _held.is_set()

                if now and not prev_held:
                    with _lock:
                        frames.clear()
                    print("  Recording…  (hold Space)", end="\r", flush=True)

                elif not now and prev_held:
                    with _lock:
                        captured = list(frames)
                    print(" " * 40, end="\r")

                    if not captured:
                        prev_held = now
                        continue

                    raw = np.concatenate(captured, axis=0)
                    audio = _to_16k_mono(raw, native_rate, native_ch)
                    rms = float(np.sqrt(np.mean(audio ** 2)))

                    if rms < SPEECH_THRESHOLD:
                        print("  (too quiet, not sent)          ")
                    else:
                        dur = len(audio) / TARGET_RATE
                        print(f"  Sending {dur:.1f}s…", end="\r", flush=True)
                        transcription = send_audio(audio, host, port)
                        if transcription:
                            print(f"  Heard: \"{transcription}\"          ")
                            wait_for_idle(host, port)
                        else:
                            print("  (nothing recognised)           ")
                    print()

                prev_held = now
                time.sleep(0.03)

    except KeyboardInterrupt:
        print("\n  Disconnected.")
    finally:
        _stop.set()


# ── Wakeword mode (always-on, no key press needed) ───────────────────────────

def run_wakeword_mode(host: str, port: int) -> None:
    try:
        from openwakeword.model import Model as WakeModel
    except ImportError:
        print("  openwakeword not installed.  Run:  pip install openwakeword")
        sys.exit(1)

    WAKEWORD_MODEL = "hey_jarvis_v0.1"
    SENSITIVITY    = 0.5
    COOLDOWN       = 1.5   # seconds between triggers
    CHUNK          = 1280  # 80 ms at 16 kHz — required frame size for openWakeWord

    print(f"\n  Mode: Wakeword — say \"hey Jarvis\" to activate.")
    print(f"  Loading model ({WAKEWORD_MODEL})…", end=" ", flush=True)
    try:
        oww = WakeModel(wakeword_models=[WAKEWORD_MODEL], inference_framework="onnx")
    except Exception as exc:
        print(f"\n  Failed to load wakeword model: {exc}")
        sys.exit(1)
    print("ready.")
    print("  Ctrl+C to quit.\n")

    # States
    WAITING   = "waiting"
    RECORDING = "recording"

    state         = WAITING
    frames: list  = []
    silence_start: float | None = None
    speech_seen   = False
    last_trigger  = 0.0
    record_start  = 0.0
    audio_q: queue.Queue = queue.Queue(maxsize=50)

    def callback(indata: np.ndarray, *_) -> None:
        mono = indata[:, 0] if indata.ndim > 1 else indata.flatten()
        try:
            audio_q.put_nowait(mono.copy())
        except queue.Full:
            pass

    try:
        # Open a single always-on 16 kHz stream — PortAudio/CoreAudio resamples
        # from the device's native rate transparently on macOS.
        with sd.InputStream(
            samplerate=16_000,
            channels=1,
            dtype="float32",
            blocksize=CHUNK,
            callback=callback,
        ):
            print("  Listening…  (say \"hey Jarvis\")\n")
            while True:
                try:
                    chunk = audio_q.get(timeout=0.5)
                except queue.Empty:
                    continue

                if state == WAITING:
                    # Feed to wakeword model (expects int16)
                    chunk_i16 = (chunk * 32767).astype(np.int16)
                    try:
                        pred = oww.predict(chunk_i16)
                    except Exception:
                        continue

                    now = time.monotonic()
                    triggered = any(
                        (float(v) if not hasattr(v, "__iter__") else float(max(v))) >= SENSITIVITY
                        for v in pred.values()
                    )
                    if triggered and (now - last_trigger) >= COOLDOWN:
                        last_trigger  = now
                        state         = RECORDING
                        frames        = []
                        silence_start = None
                        speech_seen   = False
                        record_start  = now
                        print("  Wakeword!  Recording…", end="\r", flush=True)

                elif state == RECORDING:
                    frames.append(chunk)
                    rms     = float(np.sqrt(np.mean(chunk ** 2)))
                    elapsed = time.monotonic() - record_start

                    if rms >= SPEECH_THRESHOLD:
                        speech_seen   = True
                        silence_start = None
                    else:
                        if not speech_seen:
                            if elapsed >= SPEECH_START_TIMEOUT:
                                print("  (nothing heard)              ")
                                state = WAITING
                                continue
                        else:
                            if silence_start is None:
                                silence_start = time.monotonic()
                            elif time.monotonic() - silence_start >= SILENCE_TIMEOUT:
                                _dispatch(frames, host, port)
                                state = WAITING
                                continue

                    if elapsed >= MAX_DURATION:
                        _dispatch(frames, host, port)
                        state = WAITING

    except sd.PortAudioError as exc:
        _mic_error(exc)
    except KeyboardInterrupt:
        print("\n  Disconnected.")


def _dispatch(frames: list, host: str, port: int) -> None:
    """Concatenate recorded chunks, send to NORA, wait for idle."""
    audio = np.concatenate(frames).astype(np.float32)
    dur   = len(audio) / 16_000
    print(f"  Sending {dur:.1f}s…              ", end="\r", flush=True)
    transcription = send_audio(audio, host, port)
    if transcription:
        print(f"  Heard: \"{transcription}\"          ")
        wait_for_idle(host, port)
    else:
        print("  (nothing recognised)           ")
    print()
    print("  Listening…  (say \"hey Jarvis\")\n")


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    p = argparse.ArgumentParser(
        description="NORA Remote Mic — send Mac microphone to NORA on Windows",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument(
        "--host", default=os.environ.get("NORA_HOST", ""),
        help="Windows machine IP (default: $NORA_HOST from .env)",
    )
    p.add_argument(
        "--port", type=int,
        default=int(os.environ.get("NORA_REMOTE_PORT", "8767")),
        help="Remote mic server port (default: $NORA_REMOTE_PORT or 8767)",
    )
    p.add_argument(
        "--ptt", action="store_true",
        help="PTT mode: hold Space to record (requires pynput + Accessibility permission)",
    )
    p.add_argument(
        "--wakeword", action="store_true",
        help="Always-on wakeword mode: say \"hey Jarvis\" to activate (requires openwakeword)",
    )
    args = p.parse_args()

    if not args.host:
        print("  ERROR: No host specified.")
        print("  Either set NORA_HOST=192.168.0.3 in .env or pass --host 192.168.0.3")
        sys.exit(1)

    print(f"\n  NORA Remote Mic")
    print(f"  Connecting to {args.host}:{args.port}…")

    stage = get_stage(args.host, args.port)
    if stage == "?":
        print(f"\n  ERROR: Could not reach NORA at {args.host}:{args.port}")
        print("  Checklist:")
        print("    1. NORA is running on the Windows machine")
        print("    2. remote_mic.enabled: true  in config.yaml")
        print("    3. Windows Firewall allows inbound on port 8767")
        print(f"    4. Both machines are on the same network")
        sys.exit(1)

    print(f"  Connected.  NORA is {_STAGE.get(stage, stage)}.\n")

    if args.wakeword:
        run_wakeword_mode(args.host, args.port)
    elif args.ptt:
        run_ptt_mode(args.host, args.port)
    else:
        run_enter_mode(args.host, args.port)


if __name__ == "__main__":
    main()
