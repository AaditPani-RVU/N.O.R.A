"""Run the eval set: python -m nora.evals [--model NAME] [options].

Offline, every case goes through the deterministic gates; a case they
resolve is scored, and a case they pass on is counted as "needs the model".
With --model, those cases are sent to that intent candidate one by one,
spaced so a run never starves the live NORA sharing the same free tier.

Exit status is 1 when a case fails that is not marked `known`, so the
offline run can gate a change the way a test does.

`python -m nora.evals nightly` is what nora-evals.timer runs: the offline
pass, the whole set on the NVIDIA fallback (rate-limited, not metered), and a
slice of about ten cases on the Groq primary, whose free tier is shared with
the live NORA. The slice moves on each night, so the primary sees every case
over a couple of weeks. Then nora.scout scans for new models (weekly) and
advances model trials.
"""
from __future__ import annotations

import argparse
import json
import logging
import shutil
import statistics
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

from nora.evals import CASES_PATH, NAMES_PATH, REPORTS_DIR
from nora.evals.cases import Case, expected_actions, load, matches
from nora.evals.harness import on_phone, phone_connected, route_model, route_offline

# Seconds between model calls and the token budget for one run, per provider.
# Groq's free tier is 8k tokens a minute and 200k a day, shared with the live
# NORA: one call a minute and a third of the day at most. NVIDIA's free tier
# counts requests (about 40 a minute), not tokens.
_PACE = {"groq": (65.0, 60_000), "nvidia": (3.0, 0)}
_DEFAULT_PACE = (5.0, 0)


def _candidate(name: str) -> dict:
    from nora.intent_parser import _intent_candidates
    for c in _intent_candidates():
        if c.get("name") == name:
            return c
    names = ", ".join(c.get("name", "?") for c in _intent_candidates())
    raise SystemExit(f"no intent candidate named {name!r} (have: {names})")


def _pct(n: int, d: int) -> str:
    return f"{100 * n / d:.0f}%" if d else "-"


def _quantile(values: list[int], q: float) -> int | None:
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    return round(statistics.quantiles(values, n=100, method="inclusive")[int(q * 100) - 1])


def run(cases: list[Case], *, model: str = "", candidate: dict | None = None, limit: int = 0,
        space: float | None = None, budget: int | None = None, skip: set[str] = frozenset(),
        verbose: bool = False, out=print) -> dict:
    """Score `cases`. `candidate` is a router-style candidate dict for a model
    that isn't in config.yaml (a trial); `model` names one that is. Cases in
    `skip` are not sent to the model (already scored on an earlier night)."""
    if candidate is None and model:
        candidate = _candidate(model)
    model = model or (candidate or {}).get("name", "")
    pace, cap = _PACE.get((candidate or {}).get("provider", ""), _DEFAULT_PACE)
    space = pace if space is None else space
    budget = cap if budget is None else budget

    from nora import command_engine, tool_retrieval
    command_engine.discover_commands()

    results: list[dict] = []
    spent = 0
    sent = 0
    unavailable_run = 0          # consecutive calls the provider didn't answer
    last_call = 0.0
    with phone_connected():
        for case in cases:
            with on_phone(case.via):
                route, sent_text = route_offline(case.text, prior=case.prior)
            # Would the intent model have been shown the right action? Asked
            # of every case, not only the model's: it is the same question
            # for any phrasing (Sharp Phase C).
            wanted = expected_actions(case.expect)
            covered = None
            if wanted:
                picked = set(tool_retrieval.select(sent_text))
                covered = any(w <= picked for w in wanted)
            call: dict = {}
            served = "offline"
            if route is None:
                served = "model"
                if (candidate is None or case.id in skip or (limit and sent >= limit)
                        or (budget and spent >= budget) or unavailable_run >= 3):
                    results.append({"case": case, "route": None, "served": "pending",
                                            "covered": covered})
                    continue
                wait = space - (time.monotonic() - last_call)
                if sent and wait > 0:
                    time.sleep(wait)
                with on_phone(case.via):
                    route, call = route_model(sent_text, candidate)
                last_call = time.monotonic()
                sent += 1
                unavailable_run = unavailable_run + 1 if call.get("unavailable") else 0
                spent += call["prompt_tokens"] + call["completion_tokens"]
            ok = route.kind != "error" and matches(case.expect, route)
            results.append({"case": case, "route": route, "served": served, "ok": ok, "call": call,
                            "covered": covered})
            if verbose or not ok:
                mark = "ok  " if ok else ("KNOWN" if case.known else "FAIL")
                out(f"{mark} {case.id:<22} {served:<7} {route.describe()[:70]:<70} | {case.text[:60]}")

    return _summarise(results, model=model, spent=spent, out=out)


def _summarise(results: list[dict], *, model: str, spent: int, out=print) -> dict:
    scored = [r for r in results if r["served"] != "pending"]
    offline = [r for r in scored if r["served"] == "offline"]
    by_model = [r for r in scored if r["served"] == "model"]
    no_call = [r for r in offline if r["route"].kind in ("stop", "wake", "fast")]
    failed = [r for r in scored if not r["ok"]]
    regressions = [r for r in failed if not r["case"].known]
    known_fixed = [r for r in scored if r["ok"] and r["case"].known]

    total = len(results)
    out("")
    pending = total - len(scored)
    out(f"cases {total}: {len(offline)} decided offline"
        + (f", {len(by_model)} by {model}" if model else "")
        + (f", {pending} need the model{'' if model else ' (run with --model)'}" if pending else ""))
    out(f"answered without a model call: {len(no_call)}/{total} ({_pct(len(no_call), total)})")
    out(f"accuracy, offline decisions: {sum(r['ok'] for r in offline)}/{len(offline)} "
        f"({_pct(sum(r['ok'] for r in offline), len(offline))})")
    report: dict = {
        "ts": time.time(), "model": model, "cases": total,
        "decided_offline": len(offline), "no_model_call": len(no_call),
        "offline_correct": sum(r["ok"] for r in offline),
        "pending": pending,
        "failures": [{"id": r["case"].id, "text": r["case"].text, "known": r["case"].known,
                      "expect": r["case"].expect,
                      "got": r["route"].describe() if r["route"] else None} for r in failed],
        "known_fixed": [r["case"].id for r in known_fixed],
        # Per case, for the model's turns: what trials compare night by night.
        "by_case": {r["case"].id: {"ok": r["ok"], "ms": r["call"].get("ms"),
                                   "prompt_tokens": r["call"].get("prompt_tokens"),
                                   "error": r["route"].kind == "error",
                                   "json_retry": r["call"].get("json_retry", False),
                                   "rate_limited": r["call"].get("rate_limited", False),
                                   "unavailable": r["call"].get("unavailable", False)}
                    for r in by_model},
    }
    if by_model:
        calls = [r["call"] for r in by_model]
        ms = [c["ms"] for c in calls if c]
        prompt = [c["prompt_tokens"] for c in calls if c.get("prompt_tokens")]
        model_ok = sum(r["ok"] for r in by_model)
        errors = sum(r["route"].kind == "error" for r in by_model)
        out(f"accuracy, {model}: {model_ok}/{len(by_model)} ({_pct(model_ok, len(by_model))}); "
            f"{errors} errors, {sum(c.get('json_retry', False) for c in calls)} JSON retries")
        out(f"latency p50 {_quantile(ms, .5)} ms, p90 {_quantile(ms, .9)} ms; "
            f"prompt {round(statistics.mean(prompt)) if prompt else '?'} tokens on average; "
            f"{spent} tokens spent")
        report.update({"model_correct": model_ok, "model_cases": len(by_model), "model_errors": errors,
                       "json_retries": sum(c.get("json_retry", False) for c in calls),
                       "p50_ms": _quantile(ms, .5), "p90_ms": _quantile(ms, .9),
                       "prompt_tokens_mean": round(statistics.mean(prompt)) if prompt else None,
                       "tokens_spent": spent})
    if scored:
        overall = sum(r["ok"] for r in scored)
        out(f"routing accuracy, all scored: {overall}/{len(scored)} ({_pct(overall, len(scored))})")
        report["accuracy"] = overall / len(scored)

    retrieval = [r for r in results if r.get("covered") is not None]
    if retrieval:
        hit = sum(r["covered"] for r in retrieval)
        out(f"tool retrieval: the expected action was offered in {hit}/{len(retrieval)} "
            f"({_pct(hit, len(retrieval))})")
        report["retrieval"] = [hit, len(retrieval)]
        report["retrieval_misses"] = [r["case"].id for r in retrieval if not r["covered"]]

    fams: dict[str, list[bool]] = defaultdict(list)
    for r in scored:
        for tag in r["case"].tags or ["untagged"]:
            fams[tag].append(r["ok"])
    weak = sorted((t for t, v in fams.items() if not all(v)), key=lambda t: sum(fams[t]) / len(fams[t]))
    if weak:
        out("weakest families: " + ", ".join(f"{t} {sum(fams[t])}/{len(fams[t])}" for t in weak[:8]))
    report["families"] = {t: [sum(v), len(v)] for t, v in sorted(fams.items())}
    routes = Counter(r["route"].kind for r in scored)
    report["routes"] = dict(routes)
    if known_fixed:
        out("known misses now passing (drop their `known`): " + ", ".join(report["known_fixed"]))
    out(f"regressions: {len(regressions)}" + (f" ({len(failed) - len(regressions)} known)" if failed else ""))
    report["regressions"] = len(regressions)
    return report


NIGHTLY = [
    # (candidate, extra args)
    ("", []),
    ("nvidia_nemotron_super_120b", ["--space", "5"]),
    ("groq_gpt_oss_120b", ["--limit", "10", "--rotate"]),
]


def nightly() -> int:
    from nora.doctor import _load_env
    _load_env()
    worst = 0
    for model, extra in NIGHTLY:
        print(f"\n== {model or 'offline'} ==")
        worst = max(worst, main((["--model", model] if model else []) + extra))
    # Sharp G: scan for new models weekly, advance trials nightly.
    from nora import scout
    print("\n== scout ==")
    scout.nightly()
    return worst


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["nightly"]:
        return nightly()
    ap = argparse.ArgumentParser(prog="python -m nora.evals", description=__doc__.splitlines()[0])
    ap.add_argument("--cases", type=Path, default=CASES_PATH)
    ap.add_argument("--model", default="", help="intent candidate name from config.yaml")
    ap.add_argument("--limit", type=int, default=0, help="at most this many model calls")
    ap.add_argument("--space", type=float, default=None, help="seconds between model calls")
    ap.add_argument("--budget", type=int, default=None, help="stop calling past this many tokens (0: none)")
    ap.add_argument("--tag", action="append", default=[], help="only cases with this tag")
    ap.add_argument("--rotate", action="store_true",
                    help="start at a different case each day (with --limit: a moving slice)")
    ap.add_argument("--no-report", action="store_true", help="don't save the report")
    ap.add_argument("-v", "--verbose", action="store_true", help="print every case, not just failures")
    ap.add_argument("--freeze-names", action="store_true",
                    help="copy NORA's current music names into the eval set and exit")
    args = ap.parse_args(argv)

    if args.freeze_names:
        from nora import music_names
        if not music_names.CACHE_PATH.exists():
            print("no music names yet: NORA builds them once Spotify is linked")
            return 2
        shutil.copyfile(music_names.CACHE_PATH, NAMES_PATH)
        print(f"froze {len(music_names.artists())} artists into {NAMES_PATH}")
        return 0

    if not args.cases.exists():
        print(f"no eval set at {args.cases}; build one from python -m nora.evals.harvest")
        return 2
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    if args.model:
        # The keys live in .env, as NORA reads them; without this every call
        # failed on a missing key and was scored as a wrong answer.
        from nora.doctor import _load_env
        _load_env()
    cases = load(args.cases)
    if args.tag:
        cases = [c for c in cases if set(c.tags) & set(args.tag)]
    if args.rotate and cases:
        # About half the set needs the model, so moving the start by twice the
        # limit each day moves the slice of model cases by about the limit.
        k = (int(time.time() // 86400) * max(args.limit, 1) * 2) % len(cases)
        cases = cases[k:] + cases[:k]
    report = run(cases, model=args.model, limit=args.limit, space=args.space,
                 budget=args.budget, verbose=args.verbose)
    if not args.no_report:
        REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y-%m-%d_%H%M")
        path = REPORTS_DIR / f"{stamp}{'_' + args.model if args.model else ''}.json"
        path.write_text(json.dumps(report, indent=1, default=str), encoding="utf-8")
        print(f"report: {path}")
    return 1 if report["regressions"] else 0


if __name__ == "__main__":
    sys.exit(main())
