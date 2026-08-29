"""Generate the near-miss negatives that `custom_negative_phrases` cannot.

`train.py` builds its negative text pool as

    adversarial_texts = config["custom_negative_phrases"]
    for phrase in config["target_phrase"]:
        adversarial_texts.extend(generate_adversarial_texts(N=n_samples//len(target_phrase)))

so with n_samples=30000 and two target phrases the pool is 17 hand-written
entries plus 30,000 generated ones. `generate_samples` then cycles that pool
for 30,000 clips — about one clip per hand-written phrase, or 0.06% of the
negative set. In other words the phrases you most need the model to reject are
the ones it effectively never sees.

The consequence was measured rather than assumed. Against neural TTS in four
voices, the first models fired on "hey dora" (0.97), "hey laura" (0.97) and
"aurora" (0.95) about as readily as on "hey nora" (0.97), while correctly
ignoring unrelated speech like "what's the weather" (0.00). The model had
learned the "-ora" rime and essentially nothing about the onset.

So this generates a dedicated block of collision clips and writes them straight
into the negative training directory, where augmentation and training pick them
up like any other negative. Everything here shares the target's stressed vowel
and differs mainly in the consonant before it, because that is the distinction
that was not being taught.
"""
from __future__ import annotations

import argparse
import sys
import uuid
from pathlib import Path

# Name neighbours: same rime, different onset. These are what actually fired.
NEIGHBOURS = ["dora", "laura", "aurora", "cora", "flora", "nova", "maura",
              "zora", "lora", "norah jones", "sonora", "pandora", "fedora"]
GREETINGS = ["hey", "hi", "okay", "hello"]

# Run-ons: "nora" as it appears inside ordinary speech, where no wake word was
# intended at all.
RUN_ONS = [
    "an aura", "the aura", "nor a", "nor any", "explore a", "for a moment",
    "before a", "ignore a", "restore a", "more of a", "or a bit", "know a",
    "no writer", "gnaw at", "in an hour", "and aura",
]


def phrases() -> list[str]:
    out = [f"{g} {n}" for n in NEIGHBOURS for g in GREETINGS]
    out += NEIGHBOURS                      # bare, no greeting
    out += RUN_ONS
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--generator", type=Path, default=Path("./piper-sample-generator"))
    ap.add_argument("--out", type=Path, default=Path("./output/hey_nora/negative_train"),
                    help="written straight into the negative training set")
    ap.add_argument("--n", type=int, default=6000, help="clips to generate")
    ap.add_argument("--batch-size", type=int, default=25)
    a = ap.parse_args()

    sys.path.insert(0, str(a.generator.resolve()))
    from generate_samples import generate_samples  # noqa: E402

    texts = phrases()
    a.out.mkdir(parents=True, exist_ok=True)
    print(f"{len(texts)} collision phrases -> {a.n} clips in {a.out}")

    # Same noise and length scales train.py uses for its own negatives, so these
    # clips sit in the same acoustic distribution as the rest of the set and the
    # model cannot separate them on production artefacts instead of on content.
    generate_samples(
        text=texts,
        max_samples=a.n,
        batch_size=a.batch_size,
        noise_scales=[0.98],
        noise_scale_ws=[0.98],
        length_scales=[0.75, 1.0, 1.25],
        output_dir=str(a.out),
        auto_reduce_batch_size=True,
        file_names=[uuid.uuid4().hex + ".wav" for _ in range(a.n)],
    )
    print(f"negative_train now holds {len(list(a.out.glob('*.wav')))} clips")
    return 0


if __name__ == "__main__":
    sys.exit(main())
