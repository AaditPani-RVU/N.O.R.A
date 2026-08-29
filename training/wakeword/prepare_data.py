"""Fetch the augmentation data openWakeWord's train.py expects.

The upstream notebook cannot be followed literally any more. It pulls
background audio from `AudioSet/data/bal_train09.tar`, and that path now 404s:
the dataset was restructured into parquet shards under `data/bal_train/NN.parquet`.
It also fetches the MIT impulse responses through `datasets.load_dataset(...)`
with streaming, decoding and rewriting every clip, when the repository already
holds them as 16 kHz wavs that can simply be downloaded.

So this does both jobs against the layouts that exist today:

  data/mit_rirs/     room impulse responses, for reverberation
  data/audioset_16k/ background noise to mix under the wake word

Both are idempotent — anything already on disk is left alone, so an
interrupted run is resumed by running it again rather than restarted.
"""
from __future__ import annotations

import argparse
import io
import json
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HF = "https://huggingface.co"
RIR_REPO = "davidscripka/MIT_environmental_impulse_responses"
AUDIOSET_REPO = "agkphysics/AudioSet"


def _listing(repo: str) -> list[str]:
    url = f"{HF}/api/datasets/{repo}"
    with urllib.request.urlopen(url, timeout=120) as r:
        return [s["rfilename"] for s in json.load(r).get("siblings", [])]


def _download(repo: str, rfile: str, dest: Path) -> str | None:
    if dest.exists() and dest.stat().st_size > 0:
        return None
    url = f"{HF}/datasets/{repo}/resolve/main/{rfile}"
    tmp = dest.with_suffix(dest.suffix + ".part")
    try:
        with urllib.request.urlopen(url, timeout=300) as r, open(tmp, "wb") as f:
            while chunk := r.read(1 << 20):
                f.write(chunk)
        tmp.rename(dest)          # rename last, so a killed run leaves no
        return None               # half-file that the next run trusts
    except Exception as exc:
        tmp.unlink(missing_ok=True)
        return f"{rfile}: {type(exc).__name__} {exc}"


def fetch_rirs(out: Path, workers: int) -> None:
    out.mkdir(parents=True, exist_ok=True)
    files = [f for f in _listing(RIR_REPO) if f.startswith("16khz/") and f.endswith(".wav")]
    print(f"[rirs] {len(files)} impulse responses -> {out}")
    with ThreadPoolExecutor(max_workers=workers) as ex:
        errs = [e for e in ex.map(lambda f: _download(RIR_REPO, f, out / Path(f).name), files) if e]
    for e in errs[:5]:
        print("  !", e)
    print(f"[rirs] {len(list(out.glob('*.wav')))} on disk, {len(errs)} failed")


def fetch_audioset(out: Path, shards: int, per_shard: int) -> None:
    """Decode a few AudioSet parquet shards into 16 kHz mono wavs.

    Only a handful of shards: this is background noise for augmentation, and
    train.py mixes it under the positives at random, so breadth of *kind* of
    noise matters far more than total hours.
    """
    import numpy as np
    import pyarrow.parquet as pq
    import soundfile as sf
    from scipy.signal import resample_poly

    out.mkdir(parents=True, exist_ok=True)
    have = len(list(out.glob("*.wav")))
    want = shards * per_shard
    if have >= want:
        print(f"[audioset] {have} wavs already present, skipping")
        return

    cache = out.parent / "_audioset_parquet"
    cache.mkdir(parents=True, exist_ok=True)
    names = [f"data/bal_train/{i:02d}.parquet" for i in range(shards)]

    written = have
    for rfile in names:
        # Skip a shard already decoded. Without this, raising --shards to widen
        # the background set re-downloads every earlier shard — 688 MB apiece —
        # only to find every wav it would write is already on disk.
        if list(out.glob(f"{Path(rfile).stem}_*.wav")):
            print(f"[audioset] {Path(rfile).name}: already decoded, skipping")
            continue
        local = cache / Path(rfile).name
        if err := _download(AUDIOSET_REPO, rfile, local):
            print("  !", err)
            continue
        table = pq.read_table(local)
        col = "audio" if "audio" in table.column_names else table.column_names[-1]
        rows = table.column(col).to_pylist()[:per_shard]
        for i, row in enumerate(rows):
            raw = row["bytes"] if isinstance(row, dict) else row
            if not raw:
                continue
            dest = out / f"{Path(rfile).stem}_{i:04d}.wav"
            if dest.exists():
                continue
            try:
                audio, sr = sf.read(io.BytesIO(raw), dtype="float32", always_2d=True)
                audio = audio.mean(axis=1)
                if sr != 16000:
                    audio = resample_poly(audio, 16000, sr)
                sf.write(dest, (audio * 32767).astype(np.int16), 16000, subtype="PCM_16")
                written += 1
            except Exception as exc:
                print(f"  ! {dest.name}: {type(exc).__name__} {exc}")
        print(f"[audioset] {Path(rfile).name}: {written} wavs total")
        local.unlink(missing_ok=True)   # the wavs are the artifact, not the shard


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dir", default="./data", type=Path)
    # Two shards at 1000 clips each lands on roughly the same 2000 background
    # clips the upstream notebook got from its single tar, for 1.4 GB of
    # download. Shards are ~688 MB apiece, so taking a small slice of many
    # shards costs bandwidth without buying variety.
    ap.add_argument("--shards", type=int, default=2, help="AudioSet parquet shards to decode")
    ap.add_argument("--per-shard", type=int, default=1000, help="clips to take from each shard")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--skip-audioset", action="store_true")
    a = ap.parse_args()

    fetch_rirs(a.dir / "mit_rirs", a.workers)
    if not a.skip_audioset:
        fetch_audioset(a.dir / "audioset_16k", a.shards, a.per_shard)
    print("done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
