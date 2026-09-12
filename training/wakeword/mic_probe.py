"""Show what the wake-word model actually hears from your microphone.

The benchmark scores neural speech from a file. That answers "can this model
tell 'hey nora' from 'hey dora'" and it cannot answer "does it hear *you*" —
which is the question when the detector is running, the mic stream is open, and
nothing ever fires.

Two failures look identical in the log (silence) and are fixed by opposite
things, so this separates them:

  rms stays ~0.000 while you talk   the stream is open on the wrong device, or
                                    the mic is muted. Nothing about the model
                                    or the threshold will help.
  rms moves, peak stays low         audio is arriving and your voice scores
                                    below the threshold. That is a sensitivity
                                    number, and the peak column says which one.

Opens the same 16 kHz mono stream as `nora.wakeword`, in the same 80 ms frames,
through the same model — so a score here is the score the running detector saw.
Safe to run while NORA is running; PipeWire allows both to capture at once.

    .venv/bin/python training/wakeword/mic_probe.py
    .venv/bin/python training/wakeword/mic_probe.py --seconds 60
"""
from __future__ import annotations

import argparse
import queue
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

CHUNK = 1280  # 80 ms at 16 kHz — openwakeword's required frame size


def _speech_share(x: np.ndarray, sr: int) -> tuple[float, float, float]:
    """Fraction of energy in the speech band, plus rumble share and level.

    The band matters more than the level here. A dead or mis-clocked capture can
    be loud — "is it loud" says nothing on its own. What separates a working
    microphone from a broken one is whether the energy lands where voices live
    (300 Hz-4 kHz) or piles up below 150 Hz with nothing above 4 kHz.

    The mean comes off first, and that line is the difference between this
    function working and this function lying. A DC offset is a spike in the zero
    bin, and the zero bin is inside "below 150 Hz": the ACP digital microphone
    on this machine sits at a constant +0.16, which alone reads as 96% rumble
    and buries a perfectly good speech band under 4%. On that reading the probe
    told us the only working input in the machine was broken, and the config
    pinned NORA to a dead one for it. `level` was always DC-free -- it is a
    standard deviation -- which is why a dead input still showed 0.0000 there
    while its share of nothing looked healthy. Both columns have to be read
    together, and the verdict below does that.
    """
    if len(x) < sr // 4:
        return 0.0, 0.0, 0.0
    x = x - np.mean(x)
    S = np.abs(np.fft.rfft(x * np.hanning(len(x)))) ** 2
    f = np.fft.rfftfreq(len(x), 1 / sr)
    total = float(np.sum(S)) or 1e-20
    speech = float(np.sum(S[(f >= 300) & (f < 4000)])) / total
    rumble = float(np.sum(S[f < 150])) / total
    return speech, rumble, float(x.std())


def _speech_level(x: np.ndarray, sr: int) -> float:
    """Absolute RMS of just the 300 Hz-4 kHz band.

    The share of energy in that band answers "what is this microphone mostly
    hearing", which is the wrong question for a microphone that hears a voice
    and a lot of low-frequency noise at the same time -- the share stays small
    however clearly you are picked up. This asks "how much voice is there", in
    units that compare across inputs, and it is the number the verdict turns on.
    A dead input scores near zero here no matter how clean its spectrum looks.
    """
    if len(x) < sr // 4:
        return 0.0
    x = x - np.mean(x)
    X = np.fft.rfft(x)
    f = np.fft.rfftfreq(len(x), 1 / sr)
    X[(f < 300) | (f >= 4000)] = 0
    return float(np.sqrt(np.mean(np.fft.irfft(X, n=len(x)) ** 2)))


def _read_f32_wav(path: Path) -> np.ndarray:
    b = path.read_bytes()
    i = b.find(b"data")
    if i < 0:
        return np.zeros(0)
    n = int.from_bytes(b[i + 4:i + 8], "little")
    return np.frombuffer(b[i + 8:i + 8 + n], dtype=np.float32).astype(np.float64)


def _node_names() -> dict[str, str]:
    """Map PipeWire node id -> node.name, for every node pw-dump reports.

    The ids are what pw-record targets and what wpctl prints, but they are
    assigned in graph order and move whenever the graph changes — plug in a
    headset before boot and every internal microphone is renumbered. Pinning
    NORA to an id that has drifted is indistinguishable, from the logs, from a
    broken microphone, so the scan recommends the name and this is where it
    comes from. Empty dict if pw-dump is unavailable; the caller falls back to
    recommending the id with a caveat.
    """
    import json
    import shutil
    import subprocess

    if not shutil.which("pw-dump"):
        return {}
    try:
        out = subprocess.run(["pw-dump"], capture_output=True, text=True,
                             timeout=10).stdout
        names = {}
        for obj in json.loads(out):
            if obj.get("type") != "PipeWire:Interface:Node":
                continue
            name = (obj.get("info", {}).get("props", {}) or {}).get("node.name")
            if name:
                names[str(obj["id"])] = name
        return names
    except Exception:
        return {}


def scan() -> int:
    """Record every PipeWire source in turn and say which one can hear you.

    PortAudio only exposes "default", "pipewire" and the raw ALSA devices, so a
    laptop with several internal microphones looks like one input through
    sounddevice. pw-record can target each source node directly, which is the
    only way to find out that the default is the broken one.
    """
    import shutil
    import subprocess
    import tempfile

    if not shutil.which("pw-record"):
        print("pw-record not found — this scan needs PipeWire's tools (pipewire-utils).")
        return 1

    out = subprocess.run(["wpctl", "status"], capture_output=True, text=True).stdout
    sources: list[tuple[str, str, bool]] = []
    in_sources = False
    for line in out.splitlines():
        if "Sources:" in line:
            in_sources = True
            continue
        if in_sources:
            body = line.lstrip("│ ├─└").strip()
            if not body or body.endswith(":"):
                in_sources = False
                continue
            default = body.startswith("*")
            body = body.lstrip("* ").strip()
            nid, _, rest = body.partition(".")
            if nid.strip().isdigit():
                name = rest.split("[")[0].strip()
                if "V4L2" not in name:  # cameras are listed as sources too
                    sources.append((nid.strip(), name, default))

    if not sources:
        print("No PipeWire audio sources found.")
        return 1

    seconds = 5
    print("Each microphone gets 5 seconds. KEEP TALKING through all of them —")
    print('say "hey nora, hey nora, hey nora" over and over, normally, from where you sit.')
    print()

    results = []
    tmp = Path(tempfile.mkdtemp())
    for nid, name, default in sources:
        tag = " (system default)" if default else ""
        print(f"  → recording [{nid}] {name}{tag} ... ", end="", flush=True)
        wav = tmp / f"{nid}.wav"
        proc = subprocess.Popen(
            ["pw-record", "--target", nid, "--rate", "16000", "--channels", "1",
             "--format", "f32", str(wav)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        time.sleep(seconds)
        proc.terminate()
        proc.wait(timeout=5)
        audio = _read_f32_wav(wav)
        speech, rumble, level = _speech_share(audio, 16000)
        voice = _speech_level(audio, 16000)
        results.append((nid, name, default, speech, rumble, level, voice))
        print(f"voice level {voice:.4f}")

    print()
    print("   id    voice  speech  rumble   level   microphone")
    print("   ───  ──────  ──────  ──────  ──────   ──────────")
    for nid, name, default, speech, rumble, level, voice in sorted(
            results, key=lambda r: r[6], reverse=True):
        flag = " *" if default else "  "
        print(f"  {nid:>4}{flag} {voice:.4f}  {speech:5.1%}  {rumble:5.1%}  {level:.4f}   {name}")
    print("   (* = current system default; voice = RMS in the 300 Hz-4 kHz band)")
    print()

    # The verdict turns on absolute voice level, not on share of energy.
    #
    # It used to require a high speech *share*, and that reading is what pointed
    # this project at a dead microphone for weeks. Two ways it goes wrong, and
    # this machine has one of each. A silent input carries no voice but has a
    # flat noise floor, so its share looks excellent -- that is how an input
    # reading level 0.0000 wins a scan it should lose. And an input with a real
    # voice on it plus strong low-frequency noise has a poor share however
    # clearly it hears you, so the one microphone that works gets condemned.
    #
    # Band-limited RMS answers the question being asked: how much voice is on
    # this input. `level` is kept as a second gate so a dead input cannot pass
    # on band noise alone.
    AUDIBLE = 0.005
    VOICE = 0.002
    heard = [r for r in results if r[5] >= AUDIBLE and r[6] >= VOICE]
    if not heard:
        loud = [r for r in results if r[5] >= AUDIBLE]
        print("VERDICT: no source heard you clearly.")
        if loud:
            print(f"[{loud[0][0]}] {loud[0][1]} has signal ({loud[0][5]:.3f}) but almost "
                  f"none of it is voice ({loud[0][6]:.4f} in the 300 Hz-4 kHz band).")
            print("The rest are at the noise floor, i.e. muted or not picking up.")
        else:
            print("Every input is at the noise floor. Check the mic is unmuted in your")
            print("desktop sound settings, and that you were speaking during the scan.")
        return 0

    best = max(heard, key=lambda r: r[6])

    print(f"VERDICT: [{best[0]}] {best[1]} is the one that hears you "
          f"(voice level {best[6]:.4f}).")
    if best[2]:
        print("It is already the system default, so NORA is on the right microphone.")
        return 0

    print("It is NOT the system default — which is why the wake word never fired.")
    print()
    print("Fix it system-wide (fixes every app, not just NORA):")
    print()
    print(f"    wpctl set-default {best[0]}")
    print()
    print("Then re-run this scan to confirm the * moved. It does not always take:")
    print("WirePlumber can keep handing out a different source than the configured")
    print("default (a device with api.acp.auto-port = false will do this). If the *")
    print("has not moved, pin NORA alone instead — this routes only NORA and leaves")
    print("every other app on the system default:")
    print()
    node_name = _node_names().get(best[0])
    print("    audio:")
    if node_name:
        print(f'      pipewire_node: "{node_name}"')
        print()
        print("Use that name, not the id. Ids are assigned in graph order and are")
        print("reassigned on reboot or replug — an id that drifts onto another input")
        print("looks exactly like a dead microphone in the logs. The name is derived")
        print("from the PCI address and ALSA profile and stays put.")
    else:
        print(f"      pipewire_node: {best[0]}        # in config.yaml")
        print()
        print("pw-dump was unavailable, so this is the node id — which is reassigned on")
        print("reboot and replug. Re-run this scan if the wake word goes deaf later, and")
        print("install pipewire-utils to get the stable node name instead.")
    print()

    # PortAudio does not expose PipeWire node names, so `input_device` cannot be
    # set to the name printed above — it would silently fall back to the default
    # and look like the fix did not take. Only offer the config route when an
    # actual sounddevice entry exists to name.
    #
    # Existing is not the same as usable. The raw ALSA devices are the only ones
    # left after the aliases are dropped, and a raw device runs at the card's own
    # rate — hw:2,0 on this machine refuses 16 kHz, which is the only rate the
    # detector opens. Recommending it would trade a wake word that hears the
    # wrong mic for one that never starts, so each candidate has to prove it
    # accepts the detector's format before it goes on screen.
    try:
        import sounddevice as sd
        inputs = []
        for i, d in enumerate(sd.query_devices()):
            if d["max_input_channels"] <= 0:
                continue
            if d["name"] in ("default", "pipewire", "sysdefault"):
                continue
            try:
                sd.check_input_settings(
                    device=i, samplerate=16000, channels=1, dtype="float32",
                )
            except Exception:
                continue
            inputs.append((i, d["name"]))
    except Exception:
        inputs = []
    if not inputs:
        print("There is no per-app route on this machine: every input NORA can open")
        print("directly refuses 16 kHz, so PipeWire's default is the only thing that")
        print("picks the microphone. Use the wpctl command above.")
    if inputs:
        print("Or pin NORA to one input without touching the system default. These are")
        print("the devices NORA itself can see (names here are ALSA's, not PipeWire's):")
        print()
        for i, name in inputs:
            print(f"      [{i}] {name}")
        print()
        print("    wakeword:")
        print(f'      input_device: {inputs[0][0]}      # or "{inputs[0][1].split(":")[0]}"')
        print()
        print("Re-run this probe without --scan afterwards to confirm the scores move.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seconds", type=float, default=30.0)
    ap.add_argument("--model", default=None,
                    help="default: whatever wakeword.model in config.yaml resolves to")
    ap.add_argument("--device", default=None,
                    help="input device index or name; default is the system default")
    ap.add_argument("--scan", action="store_true",
                    help="record every input source in turn and report which one hears speech")
    a = ap.parse_args()

    if a.scan:
        return scan()

    import sounddevice as sd
    from openwakeword.model import Model
    from nora.config import get_config
    from nora import wakeword as ww

    cfg = get_config().get("wakeword", {})
    models = [a.model] if a.model else ww._resolve_models(cfg.get("model", "hey_jarvis_v0.1"))
    threshold = float(cfg.get("sensitivity", 0.5))
    if not models:
        print("No wakeword model resolved from config.yaml — nothing to probe.")
        return 1

    device = a.device
    if device is not None and device.isdigit():
        device = int(device)
    info = sd.query_devices(device, kind="input")
    print(f"device     : {info['name']}  (native {int(info['default_samplerate'])} Hz)")
    print(f"model      : {', '.join(ww._display(m) for m in models)}")
    print(f"threshold  : {threshold:.2f}   (wakeword.sensitivity in config.yaml)")
    print()
    print("Say the wake word a few times. Speak normally, from where you usually sit.")
    print()
    print("   time   rms          level              peak    best phrase score")
    print("   ────   ─────  ────────────────────  ────────  ─────────────────")

    model = Model(wakeword_models=models, inference_framework="onnx")
    q: queue.Queue[np.ndarray] = queue.Queue(maxsize=50)

    def _cb(indata, frames, time_info, status) -> None:
        mono = indata[:, 0] if indata.ndim > 1 else indata.flatten()
        try:
            q.put_nowait(mono.copy())
        except queue.Full:
            pass

    fired = 0
    session_peak = 0.0
    silent_frames = 0
    total_frames = 0
    start = time.monotonic()
    # Report on a fixed cadence rather than per frame: 12 lines a second is not
    # readable, and the peak within each window is the number that matters.
    window_peak = 0.0
    window_rms = 0.0
    last_report = start

    with sd.InputStream(samplerate=16000, channels=1, dtype="float32",
                        blocksize=CHUNK, device=device, callback=_cb):
        while time.monotonic() - start < a.seconds:
            try:
                chunk = q.get(timeout=0.5)
            except queue.Empty:
                continue

            rms = float(np.sqrt(np.mean(chunk ** 2)))
            total_frames += 1
            if rms < 0.0005:
                silent_frames += 1

            scores = model.predict((chunk * 32767).astype(np.int16))
            peak = max(float(v) for v in scores.values())
            session_peak = max(session_peak, peak)
            window_peak = max(window_peak, peak)
            window_rms = max(window_rms, rms)

            if peak >= threshold:
                fired += 1

            now = time.monotonic()
            if now - last_report >= 0.5:
                bar = "█" * min(20, int(window_rms * 100))
                mark = "  ← WOULD FIRE" if window_peak >= threshold else ""
                print(f"   {now - start:4.1f}s  {window_rms:.3f}  {bar:<20s}  {window_peak:.3f}{mark}")
                window_peak = 0.0
                window_rms = 0.0
                last_report = now

    print()
    quiet = silent_frames / max(1, total_frames)
    print(f"highest score this session : {session_peak:.3f}")
    print(f"frames over threshold      : {fired}")
    print(f"frames that were silence   : {quiet:.0%}")
    print()

    # The diagnosis, stated rather than left to the reader — the whole point of
    # the probe is to end with one of these three sentences.
    if quiet > 0.95:
        print("VERDICT: the stream is open but no audio is arriving. This is a device or")
        print("mute problem, not a model problem — check the input NORA is capturing from")
        print("(pavucontrol → Recording) and whether the mic is muted in hardware.")
    elif session_peak >= threshold:
        print(f"VERDICT: your voice reaches the threshold ({session_peak:.3f} ≥ {threshold:.2f}).")
        print("The model hears you. If the running detector still is not firing, the")
        print("difference is between this probe and NORA's own stream, not the model.")
    elif session_peak >= 0.5:
        print(f"VERDICT: audio is arriving and your voice peaks at {session_peak:.3f}, under the")
        print(f"{threshold:.2f} threshold. Lower wakeword.sensitivity to about {max(0.5, session_peak - 0.08):.2f} —")
        print("staying above 0.85 keeps 'hey laura' and 'hey dora' out.")
    else:
        print(f"VERDICT: audio is arriving but the wake word barely registers ({session_peak:.3f}).")
        print("That is not a threshold you can tune around. Likely the mic is very quiet,")
        print("far away, or heavily processed — say so and we will look at gain and distance.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
