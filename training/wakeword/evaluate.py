"""Score trained wake-word models across detection thresholds.

`train.py` prints accuracy, recall and false-positives-per-hour at a single
operating point and then throws the curve away. That is the wrong shape for the
decision actually facing us, which is two decisions: *which* model to ship, and
what to set `wakeword.sensitivity` to in NORA's config — because sensitivity is
precisely that threshold, and the default 0.5 is a guess until measured.

The false-positive rate is computed the same way `train.py` does it, so numbers
here are comparable to the ones it logs: sliding 16-frame windows at stride 1
over the 11.3-hour validation set, counting windows that cross the threshold,
divided by 11.3.

Recall comes from the held-out positive clips — synthetic speech, so treat it
as a way to rank models against each other rather than as the rate you will see
in your own room with your own voice.

Runs on CPU via onnxruntime so it does not contend with a training run on the
GPU.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np

VAL_SET_HOURS = 11.3   # matches train.py's hardcoded val_set_hrs
WINDOW = 16


def _scores(session, x: np.ndarray) -> np.ndarray:
    """Score every window, one at a time.

    train.py exports the classifier with a fixed batch dimension of 1, so a
    batched feed is rejected outright rather than being merely slower. The head
    is tiny enough that it does not matter: ~0.018 ms per window, so the full
    11-hour validation set scores in about nine seconds.
    """
    name = session.get_inputs()[0].name
    out = np.empty(len(x), dtype=np.float32)
    for i in range(len(x)):
        window = np.ascontiguousarray(x[i], dtype=np.float32)[None, ...]
        out[i] = session.run(None, {name: window})[0].reshape(-1)[0]
    return out


def evaluate(model_path: Path, pos: np.ndarray, fp_windows: np.ndarray,
             thresholds: list[float]) -> list[dict]:
    import onnxruntime as ort

    opts = ort.SessionOptions()
    opts.inter_op_num_threads = 1
    sess = ort.InferenceSession(str(model_path), sess_options=opts,
                                providers=["CPUExecutionProvider"])

    pos_scores = _scores(sess, pos)
    fp_scores = _scores(sess, fp_windows)

    rows = []
    for t in thresholds:
        recall = float((pos_scores >= t).mean())
        fp_per_hour = float((fp_scores >= t).sum()) / VAL_SET_HOURS
        rows.append({"threshold": t, "recall": recall, "fp_per_hour": fp_per_hour})
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("models", nargs="+", type=Path, help=".onnx files to score")
    ap.add_argument("--features-dir", type=Path, default=Path("output/hey_nora"))
    ap.add_argument("--validation", type=Path, default=Path("data/validation_set_features.npy"))
    ap.add_argument("--thresholds", default="0.2,0.3,0.4,0.5,0.6,0.7,0.8")
    ap.add_argument("--max-fp", type=float, default=0.5,
                    help="highest acceptable false positives per hour when picking a winner")
    a = ap.parse_args()

    thresholds = [float(t) for t in a.thresholds.split(",")]

    pos = np.load(a.features_dir / "positive_features_test.npy")
    raw = np.load(a.validation, mmap_mode="r")
    # A view, not a copy: materialising every window as its own array is ~2.9 GB
    # for an 11-hour set, and each batch is copied out of it as it is scored.
    fp_windows = np.lib.stride_tricks.sliding_window_view(raw, WINDOW, axis=0).transpose(0, 2, 1)

    print(f"positives: {pos.shape}   fp windows: {fp_windows.shape}   "
          f"({VAL_SET_HOURS} h)\n")

    best = None
    for m in a.models:
        if not m.is_file():
            print(f"!! missing: {m}")
            continue
        rows = evaluate(m, pos, fp_windows, thresholds)
        print(f"=== {m.name}")
        print(f"    {'thresh':>7} {'recall':>8} {'fp/hour':>9}")
        for r in rows:
            mark = ""
            if r["fp_per_hour"] <= a.max_fp:
                # Best = highest recall among thresholds that meet the FP budget.
                # Recall is what the user feels every time they speak; the FP
                # budget is a constraint, not something to optimise past.
                if best is None or r["recall"] > best[1]["recall"]:
                    best = (m, r)
                mark = "  <- within fp budget"
            print(f"    {r['threshold']:>7.2f} {r['recall']:>8.3f} {r['fp_per_hour']:>9.3f}{mark}")
        print()

    if best:
        m, r = best
        print(f"BEST: {m.name} at sensitivity {r['threshold']:.2f} "
              f"-> recall {r['recall']:.3f}, {r['fp_per_hour']:.3f} fp/hour")
    else:
        print(f"No model/threshold combination stayed under {a.max_fp} fp/hour.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
