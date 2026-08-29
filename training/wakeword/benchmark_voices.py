"""Score wake-word models on real neural speech, not on the training generator.

`train.py`'s reported recall is measured against held-out Piper/LibriTTS clips —
the same generator that produced the training data. That number turned out to be
badly pessimistic here: transcribing a sample of those clips showed only about a
third survive as the intended phrase, because the generator swallows the "hey"
("a nora", "aye nora", "i'm nora"). Models scoring 0.40 recall by that measure
detected 7 of 8 real utterances.

More importantly, the trainer never measures the thing that actually breaks a
wake word in a room: whether it can tell the target from phrases that rhyme with
it. So this scores three groups separately —

  positive    the wake word. Should fire.
  collision   near-misses sharing the target's rime. Must not fire, and these
              are where the failures live: "hey dora" scored 0.97 against
              "hey nora" 0.97 on the first models trained.
  control     unrelated speech and other assistants' wake words. Should be
              silent, and was, even on the bad models — which is why "it works"
              is such an easy wrong conclusion to reach.

Audio comes from edge-tts across several accents and both genders, so it is
genuinely out of the training distribution. Clips are cached on disk; delete the
directory to regenerate.

Scoring runs the full openWakeWord pipeline (melspectrogram, embedding,
classifier) in 80 ms frames, exactly as `nora.wakeword` does at runtime, and
takes the peak score over the utterance — a wake word fires on its best frame,
not its average.
"""
from __future__ import annotations

import argparse
import asyncio
import collections
import glob
import os
import sys
from pathlib import Path

import numpy as np

VOICES = [
    "en-GB-SoniaNeural", "en-GB-RyanNeural", "en-US-AriaNeural",
    "en-US-GuyNeural", "en-US-JennyNeural", "en-IN-NeerjaNeural",
    "en-AU-NatashaNeural", "en-CA-LiamNeural",
]
POSITIVE = ["Hey Nora", "Hi Nora", "Hey Nora, are you there", "Okay Nora"]
COLLISION = ["Hey Dora", "Hey Laura", "Aurora", "An aura", "Hey Cora",
             "Hey Flora", "Nora", "Explore a bit", "In an hour"]
CONTROL = ["Hey there", "What's the weather", "Hey Siri", "Okay Google",
           "Hey Jarvis", "Turn on the lights"]
GROUPS = {"positive": POSITIVE, "collision": COLLISION, "control": CONTROL}


async def _synth(out: Path) -> None:
    import edge_tts
    out.mkdir(parents=True, exist_ok=True)
    for group, phrases in GROUPS.items():
        for phrase in phrases:
            slug = phrase.lower().replace(" ", "_").replace("'", "").replace(",", "")
            for voice in VOICES:
                f = out / f"{group}__{slug}__{voice}.mp3"
                if f.exists():
                    continue
                await edge_tts.Communicate(phrase, voice).save(str(f))


def _load(out: Path) -> dict[str, np.ndarray]:
    import soundfile as sf
    from scipy.signal import resample_poly
    clips = {}
    for f in sorted(glob.glob(str(out / "*.mp3"))):
        a, sr = sf.read(f, dtype="float32", always_2d=True)
        a = a.mean(axis=1)
        if sr != 16000:
            a = resample_poly(a, 16000, sr)
        clips[os.path.basename(f)] = (a * 32767).astype(np.int16)
    return clips


def _peak(model, pcm: np.ndarray) -> float:
    """Highest score any 80 ms frame reaches — a wake word fires on its best frame."""
    model.reset()
    best = 0.0
    for i in range(0, max(1, len(pcm) - 1280), 1280):
        for v in model.predict(pcm[i:i + 1280]).values():
            best = max(best, float(v))
    return best


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("models", nargs="+", type=Path)
    ap.add_argument("--clips", type=Path, default=Path("./voice_bench"))
    ap.add_argument("--thresholds", default="0.5,0.7,0.9")
    ap.add_argument("--detail", action="store_true", help="per-phrase peaks for each model")
    a = ap.parse_args()

    if not a.clips.exists() or not list(a.clips.glob("*.mp3")):
        print(f"synthesising benchmark clips into {a.clips} ...")
        asyncio.run(_synth(a.clips))
    clips = _load(a.clips)
    print(f"{len(clips)} clips, {len(VOICES)} voices\n")

    from openwakeword.model import Model
    thresholds = [float(t) for t in a.thresholds.split(",")]

    for mp in a.models:
        if not mp.is_file():
            print(f"!! missing: {mp}")
            continue
        model = Model(wakeword_models=[str(mp)], inference_framework="onnx")
        scores: dict[str, list] = collections.defaultdict(list)
        per_phrase: dict[tuple, list] = collections.defaultdict(list)
        for name, pcm in clips.items():
            group, slug, _ = name.split("__", 2)
            s = _peak(model, pcm)
            scores[group].append(s)
            per_phrase[(group, slug)].append(s)

        print(f"=== {mp.name}")
        for t in thresholds:
            pos = np.mean([s >= t for s in scores["positive"]])
            col = np.mean([s >= t for s in scores["collision"]])
            ctl = np.mean([s >= t for s in scores["control"]])
            # Margin is the number that matters: how much daylight is there
            # between the wake word and the phrases that rhyme with it.
            print(f"    @{t:.2f}  wake {pos:5.1%}   collision {col:5.1%}   "
                  f"control {ctl:5.1%}   margin {pos - col:+.1%}")
        if a.detail:
            for (g, slug), v in sorted(per_phrase.items(), key=lambda x: (x[0][0], -max(x[1]))):
                print(f"      {g:9s} {slug:24s} max={max(v):.3f} mean={np.mean(v):.3f}")
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
