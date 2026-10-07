"""Keep up: notice new free models, try them on the eval set, propose the winners.

    python -m nora.scout                     status: providers, trials, proposals
    python -m nora.scout scan                list every provider's models now
    python -m nora.scout trial PROVIDER MODEL   queue a model for trial by hand
    python -m nora.scout run                 scan if due, then advance trials
    python -m nora.scout promote MODEL       put a proposed model first in its role
    python -m nora.scout rollback MODEL      take a promoted model back out

Sharp Phase G. A new model ships every couple of weeks; NORA should not be
pinned to the ones she launched with, and should not switch on hype either.

  Scout    once a week, list the models of every provider NORA can use (the
           ones in llm_router, plus model_scout.providers) through their
           free /models endpoints. A model not seen before is new; a
           configured model that has gone missing twice running is withdrawn,
           and the router stops trying it (`withdrawn()`), with no edit.
  Trial    a new chat model runs the eval set's model cases for the intent
           role, a slice a night, paced for its provider's free tier. Results
           accumulate per case until there are enough to judge.
  Judge    against the role's current first choice, on the cases both have
           answered: accuracy, cases it gets wrong that the incumbent gets
           right, invalid-JSON rate and latency.
  Propose  a winner is proposed once, with its numbers, on the phone. Nothing
           changes until `promote`, and `rollback` undoes it.

The first scan of a provider only records what is there: the trial queue is
for models released from then on (or queued by hand).
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
from pathlib import Path

from nora.config import get_config

logger = logging.getLogger("nora.scout")

ROOT = Path(__file__).resolve().parent.parent
STATE_PATH = ROOT / "nora_model_scout.json"
CONFIG_PATH = ROOT / "config.yaml"

SCAN_EVERY_SEC = 6.5 * 86400
TRIAL_ROLE = "intent"
MIN_OVERLAP = 30            # cases both models answered before a verdict
TRIALS_PER_NIGHT = 2
# Model calls per trial per night, by provider: Groq's daily tokens are shared
# with the live NORA; NVIDIA's free tier counts requests a minute, not tokens.
NIGHTLY_CALLS = {"groq": 8, "nvidia": 60}
_DEFAULT_CALLS = 20

# Not chat models, or not ones that could parse an intent.
_NOT_CHAT = re.compile(
    r"embed|rerank|retriev|guard|safety|reward|parse|whisper|tts|speech|audio|"
    r"transcri|vision|-vl\b|-vl-|ocr|clip|detector|pii|translat|playai|orpheus|"
    r"content-safety|deplot|kosmos|fuyu|neva|paligemma|nemoretriever|bge|e5-|"
    # code-only, domain-only, image or world models: not intent parsers
    r"code|coder|starcoder|-med-|-fin-|palmyra-med|palmyra-fin|vila|cosmos|diffusion|"
    r"calibration|chatqa", re.I)
_SIZE = re.compile(r"(\d+(?:\.\d+)?)b\b", re.I)
MIN_PARAMS_B = 7.0


# ── state ────────────────────────────────────────────────────────────────────

def _load() -> dict:
    try:
        return json.loads(STATE_PATH.read_text())
    except (OSError, ValueError):
        return {"providers": {}, "trials": {}}


def _save(state: dict) -> None:
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1, sort_keys=True))
    tmp.replace(STATE_PATH)


def _pkey(base_url: str, key_env: str) -> str:
    return f"{base_url.rstrip('/')}|{key_env}"


def providers() -> dict[str, dict]:
    """{key: {name, base_url, api_key_env}} for every provider NORA can call."""
    out: dict[str, dict] = {}
    roles = get_config().get("llm_router", {}).get("roles", {}) or {}
    extra = (get_config().get("model_scout", {}) or {}).get("providers", []) or []
    for c in [c for cands in roles.values() for c in cands or []] + list(extra):
        if c.get("provider") == "ollama" or not c.get("base_url"):
            continue
        k = _pkey(c["base_url"], c.get("api_key_env", ""))
        out.setdefault(k, {"name": c.get("provider") or c.get("name", "?"),
                           "base_url": c["base_url"].rstrip("/"),
                           "api_key_env": c.get("api_key_env", "")})
    return out


def is_chat_candidate(model: str) -> bool:
    """A model worth an intent trial: a chat model of a useful size."""
    if _NOT_CHAT.search(model):
        return False
    sizes = [float(x) for x in _SIZE.findall(model)]
    return not sizes or max(sizes) >= MIN_PARAMS_B


def _tkey(provider_key: str, model: str) -> str:
    return f"{provider_key}|{model}"


# ── scout ────────────────────────────────────────────────────────────────────

def _list_models(p: dict) -> set[str] | None:
    import requests
    key = os.environ.get(p["api_key_env"], "")
    if not key:
        return None
    try:
        r = requests.get(p["base_url"] + "/models", headers={"Authorization": f"Bearer {key}"},
                         timeout=20)
        if r.status_code != 200:
            return None
        return {m.get("id", "") for m in r.json().get("data", []) if m.get("id")}
    except Exception as e:
        logger.info("scout: %s unreachable: %s", p["name"], e)
        return None


def scan(state: dict | None = None, *, lister=_list_models) -> dict:
    """List every provider's models; record new and withdrawn ones; queue
    trials for new chat models. Returns {new, withdrawn, back} as lists of
    (provider, model)."""
    state = state if state is not None else _load()
    now = time.time()
    news: dict[str, list] = {"new": [], "withdrawn": [], "back": []}
    for k, p in providers().items():
        listed = lister(p)
        if listed is None:
            continue                     # unreachable or no key: decide nothing
        rec = state["providers"].setdefault(k, {"name": p["name"], "models": {}})
        first = not rec["models"]
        rec["last_scan"] = now
        for m in listed:
            seen = rec["models"].setdefault(m, {"first_seen": now})
            if seen.get("missing"):
                news["back"].append((p["name"], m))
            seen.update(last_seen=now, missing=0)
            seen.pop("gone_since", None)
            if not first and seen["first_seen"] == now:
                news["new"].append((p["name"], m))
                if is_chat_candidate(m):
                    state["trials"].setdefault(_tkey(k, m), {
                        "provider": p["name"], "provider_key": k, "model": m,
                        "role": TRIAL_ROLE, "status": "pending", "queued": now, "results": {}})
        for m, seen in rec["models"].items():
            if m in listed:
                continue
            seen["missing"] = seen.get("missing", 0) + 1
            # Twice running, so one bad listing never takes a model away.
            if seen["missing"] == 2:
                seen["gone_since"] = now
                news["withdrawn"].append((p["name"], m))
    state["last_scan"] = now
    return news


def withdrawn() -> set[tuple[str, str]]:
    """(base_url, model) pairs the scout has seen withdrawn. The router skips them."""
    try:
        st = os.stat(STATE_PATH)
    except OSError:
        return set()
    global _wd_cache
    if _wd_cache[0] != (st.st_mtime_ns, st.st_size):
        gone = set()
        for k, rec in _load().get("providers", {}).items():
            base = k.split("|", 1)[0]
            gone |= {(base, m) for m, s in rec.get("models", {}).items() if s.get("gone_since")}
        _wd_cache = ((st.st_mtime_ns, st.st_size), gone)
    return _wd_cache[1]


_wd_cache: tuple[tuple[int, int] | None, set] = (None, set())


def live(candidates: list[dict]) -> list[dict]:
    """`candidates` without the ones the scout has seen withdrawn."""
    gone = withdrawn()
    if not gone:
        return candidates
    kept = [c for c in candidates
            if (str(c.get("base_url", "")).rstrip("/"), c.get("model")) not in gone]
    return kept or candidates          # never leave a role with nothing to try


# ── trials ───────────────────────────────────────────────────────────────────

def _slug(model: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", model.lower()).strip("_")


def _trial_candidate(t: dict) -> dict:
    p = providers().get(t["provider_key"]) or {}
    return {"name": f"trial_{_slug(t['model'])}", "provider": t["provider"],
            "base_url": p.get("base_url") or t["provider_key"].split("|")[0],
            "api_key_env": p.get("api_key_env") or t["provider_key"].split("|")[1],
            "model": t["model"]}


def incumbent(role: str = TRIAL_ROLE) -> dict | None:
    cands = get_config().get("llm_router", {}).get("roles", {}).get(role, []) or []
    return live(cands)[0] if cands else None


def scored(name: str) -> dict[str, dict]:
    """Every per-case result recorded for candidate `name`, newest last."""
    from nora.evals import REPORTS_DIR
    out: dict[str, dict] = {}
    for path in sorted(REPORTS_DIR.glob("*.json")):
        try:
            rep = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if rep.get("model") == name:
            out.update({i: r for i, r in rep.get("by_case", {}).items()
                        if not r.get("rate_limited") and not r.get("unavailable")})
    return out


def judge(cand: dict[str, dict], inc: dict[str, dict]) -> dict:
    """Compare two models on the cases both answered."""
    import statistics
    both = sorted(set(cand) & set(inc))
    v: dict = {"overlap": len(both)}
    if len(both) < MIN_OVERLAP:
        v["verdict"] = "waiting"
        return v

    def stats(res: dict, prefix: str) -> None:
        rows = [res[i] for i in both]
        ms = sorted(r["ms"] for r in rows if r.get("ms"))
        v[f"{prefix}_correct"] = sum(r["ok"] for r in rows)
        v[f"{prefix}_errors"] = sum(r.get("error", False) for r in rows)
        v[f"{prefix}_p90_ms"] = ms[int(0.9 * (len(ms) - 1))] if ms else None
        v[f"{prefix}_p50_ms"] = round(statistics.median(ms)) if ms else None
        toks = [r["prompt_tokens"] for r in rows if r.get("prompt_tokens")]
        v[f"{prefix}_prompt_tokens"] = round(statistics.mean(toks)) if toks else None

    stats(cand, "cand")
    stats(inc, "inc")
    v["regressions"] = sorted(i for i in both if inc[i]["ok"] and not cand[i]["ok"])
    n = len(both)
    better = v["cand_correct"] - v["inc_correct"]
    faster = (v["cand_p90_ms"] and v["inc_p90_ms"] and v["cand_p90_ms"] <= 0.7 * v["inc_p90_ms"])
    regress_ok = len(v["regressions"]) <= max(1, round(0.03 * n))
    errors_ok = v["cand_errors"] <= v["inc_errors"] + max(1, round(0.02 * n))
    slow = v["cand_p90_ms"] and v["inc_p90_ms"] and v["cand_p90_ms"] > 1.5 * v["inc_p90_ms"] \
        and v["cand_p90_ms"] > 3000
    if regress_ok and errors_ok and not slow and (better >= max(2, round(0.02 * n))
                                                  or (better >= 0 and faster)):
        v["verdict"] = "better"
    elif better <= -round(0.05 * n) or v["cand_errors"] > 0.1 * n:
        v["verdict"] = "worse"
    else:
        v["verdict"] = "no better"
    return v


def run_trials(state: dict | None = None, *, nights_calls: dict | None = None, out=print) -> list[dict]:
    """Advance up to TRIALS_PER_NIGHT trials by one night's slice each; judge
    the ones with enough cases. Returns the trials that became proposals."""
    from nora.evals import CASES_PATH
    from nora.evals.__main__ import run
    from nora.evals.cases import load

    state = state if state is not None else _load()
    calls = nights_calls or NIGHTLY_CALLS
    inc = incumbent()
    inc_scores = scored(inc["name"]) if inc else {}
    cases = load(CASES_PATH) if CASES_PATH.exists() else []
    proposed = []
    active = [t for t in state["trials"].values() if t["status"] in ("pending", "running")]
    for t in sorted(active, key=lambda t: t["queued"])[:TRIALS_PER_NIGHT]:
        cand = _trial_candidate(t)
        t["status"] = "running"
        out(f"\n== trial {t['provider']}:{t['model']} ==")
        report = run(cases, candidate=cand, limit=calls.get(t["provider"], _DEFAULT_CALLS),
                     skip=set(t["results"]), out=out)
        _keep_report(report, cand["name"])
        # A rate limit or a timeout says nothing about the model's answers:
        # those cases are asked again on a later night.
        night = report.get("by_case", {})
        usable = {i: r for i, r in night.items()
                  if not r.get("rate_limited") and not r.get("unavailable")}
        t["results"].update(usable)
        if night and not usable:
            t["nights_unavailable"] = t.get("nights_unavailable", 0) + 1
        elif usable:
            t["nights_unavailable"] = 0
        model_cases = report["cases"] - report["decided_offline"]
        if t.get("nights_unavailable", 0) >= 3:
            t["status"] = "unavailable"
            out(f"trial {t['model']}: listed, but no answer three nights running; dropped")
            continue
        done = len(t["results"]) >= model_cases
        t["verdict"] = judge(t["results"], inc_scores)
        t["vs"] = inc["name"] if inc else None
        out(f"trial {t['model']}: {len(t['results'])} cases so far, verdict {t['verdict']['verdict']}")
        if t["verdict"]["verdict"] == "better":
            t["status"] = "proposed"
            proposed.append(t)
        elif t["verdict"]["verdict"] in ("worse", "no better") and done:
            t["status"] = "rejected" if t["verdict"]["verdict"] == "worse" else "not better"
    return proposed


def _keep_report(report: dict, name: str) -> None:
    from nora.evals import REPORTS_DIR
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORTS_DIR / f"{time.strftime('%Y-%m-%d_%H%M')}_{name}.json"
    path.write_text(json.dumps(report, indent=1, default=str))


def _describe(t: dict) -> str:
    v = t["verdict"]
    n = v["overlap"]
    def sec(ms):
        return f"{ms / 1000:.1f} s" if ms else "?"
    return (f"{t['model']} ({t['provider']}) beat {t.get('vs')} on {n} eval cases: "
            f"{v['cand_correct']}/{n} right against {v['inc_correct']}/{n}, "
            f"p90 {sec(v['cand_p90_ms'])} against {sec(v['inc_p90_ms'])}")


def tell(proposed: list[dict], withdrawn_now: list[tuple[str, str]], speak=None) -> str:
    lines = [f"A new model looks better: {_describe(t)}. "
             f"To switch: python -m nora.scout promote {t['model']}." for t in proposed]
    lines += [f"{p} withdrew {m}; I've stopped calling it." for p, m in withdrawn_now]
    if not lines:
        return ""
    text = " ".join(lines)
    from nora import delivery
    if not delivery.broadcast(text, kind="notice") and speak is not None:
        speak(text, mood="info")
    logger.warning("scout: %s", text)
    return text


def nightly(out=print) -> int:
    """What nora-evals.timer runs after the evals: scan weekly, trial nightly."""
    state = _load()
    news = {"withdrawn": []}
    if time.time() - state.get("last_scan", 0) >= SCAN_EVERY_SEC:
        news = scan(state)
        out(f"scout: {len(news['new'])} new, {len(news['withdrawn'])} withdrawn, "
            f"{len(news['back'])} back")
        _save(state)
    proposed = run_trials(state, out=out)
    _save(state)
    configured = {(c.get("base_url", "").rstrip("/"), c.get("model"))
                  for cands in (get_config().get("llm_router", {}).get("roles", {}) or {}).values()
                  for c in cands or []}
    base_of = {p["name"]: p["base_url"] for p in providers().values()}
    used = [(p, m) for p, m in news["withdrawn"] if (base_of.get(p), m) in configured]
    tell(proposed, used)
    return 0


# ── promote / rollback ───────────────────────────────────────────────────────

_MARK = "# promoted by nora.scout"


def promote(model: str, state: dict | None = None) -> str:
    """Insert a proposed model at the top of its role in config.yaml."""
    state = state if state is not None else _load()
    trials = [t for t in state["trials"].values() if t["model"] == model]
    if not trials:
        raise SystemExit(f"no trial for {model}; see python -m nora.scout")
    t = trials[0]
    if t["status"] not in ("proposed", "promoted"):
        raise SystemExit(f"{model} is {t['status']}, not proposed")
    cand = _trial_candidate(t)
    name = f"{_slug(t['provider'])}_{_slug(model)}"
    text = CONFIG_PATH.read_text()
    role_line = f"    {t['role']}:\n"
    at = text.index(role_line, text.index("llm_router:")) + len(role_line)
    v = t["verdict"]
    block = (f"      {_MARK} {time.strftime('%Y-%m-%d')}: {v['cand_correct']}/{v['overlap']} "
             f"vs {v['inc_correct']}/{v['overlap']} for {t.get('vs')} on the eval set.\n"
             f"      # Roll back: python -m nora.scout rollback {model}\n"
             f"      - name: {name}\n"
             f"        provider: {cand['provider']}\n"
             f"        base_url: \"{cand['base_url']}\"\n"
             f"        api_key_env: \"{cand['api_key_env']}\"\n"
             f"        model: \"{model}\"\n")
    if f'model: "{model}"\n' in text[at:at + len(block) + 200]:
        return f"{model} is already first in {t['role']}."
    CONFIG_PATH.write_text(text[:at] + block + text[at:])
    t["status"] = "promoted"
    t["promoted"] = time.time()
    _save(state)
    return f"{model} is now first for {t['role']}; restart NORA to use it."


def rollback(model: str) -> str:
    text = CONFIG_PATH.read_text()
    pat = re.compile(r"      " + re.escape(_MARK) + r"[^\n]*\n      # Roll back: python -m nora\.scout "
                     r"rollback " + re.escape(model) + r"\n(?:        [^\n]*\n|      - name:[^\n]*\n)+?"
                     r"        model: \"" + re.escape(model) + r"\"\n")
    new, n = pat.subn("", text, count=1)
    if not n:
        return f"{model} wasn't promoted by the scout; nothing to roll back."
    CONFIG_PATH.write_text(new)
    state = _load()
    for t in state["trials"].values():
        if t["model"] == model and t["status"] == "promoted":
            t["status"] = "rolled back"
    _save(state)
    return f"{model} is out of config.yaml; restart NORA to go back."


# ── command line ─────────────────────────────────────────────────────────────

def status(out=print) -> None:
    state = _load()
    for k, rec in state.get("providers", {}).items():
        models = rec.get("models", {})
        gone = [m for m, s in models.items() if s.get("gone_since")]
        out(f"{rec['name']}: {len(models) - len(gone)} models"
            + (f", withdrawn: {', '.join(sorted(gone))}" if gone else "")
            + (f"; scanned {time.strftime('%Y-%m-%d', time.localtime(rec['last_scan']))}"
               if rec.get("last_scan") else ""))
    for t in sorted(state.get("trials", {}).values(), key=lambda t: t["queued"]):
        v = t.get("verdict", {})
        out(f"trial {t['provider']}:{t['model']} [{t['status']}] {len(t['results'])} cases"
            + (f", vs {t.get('vs')}: {v.get('cand_correct')}/{v.get('overlap')} against "
               f"{v.get('inc_correct')}/{v.get('overlap')}" if v.get("overlap", 0) >= MIN_OVERLAP else ""))
    if not state.get("providers"):
        out("not scanned yet: python -m nora.scout scan")


def main(argv: list[str] | None = None) -> int:
    from nora.doctor import _load_env
    argv = sys.argv[1:] if argv is None else argv
    _load_env()
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    cmd = argv[0] if argv else "status"
    if cmd == "scan":
        state = _load()
        news = scan(state)
        _save(state)
        for kind in ("new", "withdrawn", "back"):
            for p, m in news[kind]:
                print(f"{kind}: {p} {m}")
        status()
    elif cmd == "trial" and len(argv) == 3:
        state = _load()
        k = next((k for k, p in providers().items() if p["name"] == argv[1]), None)
        if k is None:
            raise SystemExit(f"no provider {argv[1]!r}; have {[p['name'] for p in providers().values()]}")
        state["trials"].setdefault(_tkey(k, argv[2]), {
            "provider": argv[1], "provider_key": k, "model": argv[2], "role": TRIAL_ROLE,
            "status": "pending", "queued": time.time(), "results": {}})
        _save(state)
        print(f"queued {argv[1]}:{argv[2]} for the next nightly run")
    elif cmd == "run":
        return nightly()
    elif cmd == "promote" and len(argv) == 2:
        print(promote(argv[1]))
    elif cmd == "rollback" and len(argv) == 2:
        print(rollback(argv[1]))
    elif cmd == "status":
        status()
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
