"""Where the time goes: python -m nora.evals.latency [--days N].

Reads the per-turn trace (nora_turn_trace.jsonl) and the phone's own timings
(nora_voice_latency.jsonl), and prints p50/p90 for each stage by route, model
calls by candidate, and for voice turns on the phone, recognition, the core's
first words and the first audio, each from the end of speech.
"""
from __future__ import annotations

import argparse
import json
import statistics
import time
from collections import defaultdict
from pathlib import Path

from nora.hub.voice import LATENCY_PATH
from nora.trace import TRACE_PATH


def _rows(path: Path, since: float) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if r.get("ts", 0) >= since and not r.get("test"):
            rows.append(r)
    return rows


def _q(values: list[int]) -> str:
    if not values:
        return "-"
    if len(values) == 1:
        return f"{values[0]}"
    qs = statistics.quantiles(values, n=10, method="inclusive")
    return f"{round(statistics.median(values))}/{round(qs[8])}"


def report(days: float = 7.0, out=print) -> dict:
    since = time.time() - days * 86400
    turns = _rows(TRACE_PATH, since)
    voice = _rows(LATENCY_PATH, since)
    out(f"{len(turns)} turns traced in the last {days:g} days (ms, p50/p90)")
    summary: dict = {"turns": len(turns), "routes": {}, "models": {}}

    by_route: dict[str, list[dict]] = defaultdict(list)
    for t in turns:
        by_route[t.get("route") or "?"].append(t)
    stages = ("routed", "model", "first_say", "done")
    out(f"  {'route':<8}{'turns':>6}  " + "".join(f"{s:>14}" for s in stages))
    for route, rows in sorted(by_route.items(), key=lambda kv: -len(kv[1])):
        cells = {s: [r["marks"][s] for r in rows if s in r.get("marks", {})] for s in stages}
        out(f"  {route:<8}{len(rows):>6}  " + "".join(f"{_q(cells[s]):>14}" for s in stages))
        summary["routes"][route] = {"turns": len(rows), **{s: _q(v) for s, v in cells.items()}}
    no_model = sum(len(v) for k, v in by_route.items() if k in ("fast", "stop", "wake"))
    if turns:
        out(f"  answered without a model call: {no_model}/{len(turns)} ({100 * no_model // len(turns)}%)")

    calls: dict[str, list[dict]] = defaultdict(list)
    for t in turns:
        for m in t.get("models", []):
            calls[f"{m['role']}:{m['candidate']}"].append(m)
    if calls:
        out("model calls (ms p50/p90, prompt tokens mean, failures):")
    for name, ms in sorted(calls.items(), key=lambda kv: -len(kv[1])):
        ok = [m for m in ms if m["ok"]]
        prompt = [m["prompt_tokens"] for m in ok if m.get("prompt_tokens")]
        fails = defaultdict(int)
        for m in ms:
            if not m["ok"]:
                fails[m.get("error", "error")] += 1
        out(f"  {name:<40}{len(ms):>4}  {_q([m['ms'] for m in ok]):>11}  "
            f"{round(statistics.mean(prompt)) if prompt else '-':>6}  "
            + (", ".join(f"{k} x{v}" for k, v in fails.items()) or "-"))
        summary["models"][name] = {"calls": len(ms), "failed": len(ms) - len(ok)}

    if voice:
        cols = ("stt_ms", "core_first_say_ms", "first_audio_ms")
        cells = {c: [v[c] for v in voice if isinstance(v.get(c), int)] for c in cols}
        out(f"phone voice turns: {len(voice)} (ms from the end of speech, p50/p90): "
            + ", ".join(f"{c.removesuffix('_ms')} {_q(cells[c])}" for c in cols))
        summary["voice"] = {"turns": len(voice), **{c: _q(v) for c, v in cells.items()}}
        # The phone says "One sec" when the core is slow: the first thing
        # heard is then the acknowledgement, reported apart from the answer.
        acked = [v for v in voice if isinstance(v.get("ack_ms"), int)]
        if acked:
            heard = [min(t for t in (v.get("ack_ms"), v.get("first_audio_ms")) if isinstance(t, int))
                     for v in voice
                     if isinstance(v.get("ack_ms"), int) or isinstance(v.get("first_audio_ms"), int)]
            out(f"  acknowledged {len(acked)}/{len(voice)}; first sound counting it {_q(heard)}")
            summary["voice"]["acknowledged"] = len(acked)
            summary["voice"]["first_sound_ms"] = _q(heard)
    return summary


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m nora.evals.latency")
    ap.add_argument("--days", type=float, default=7.0)
    report(ap.parse_args(argv).days)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
