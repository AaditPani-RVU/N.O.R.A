"""Eval set: real utterances with the route each one should take.

Sharp Phase A. Every later phase (more fast path, smaller prompts, a new
model) is judged against this set instead of a feeling.

    python -m nora.evals                  offline: fast path, stop and wake
                                          handling, conversation routing.
                                          No network, a second or two.
    python -m nora.evals --model NAME     also send the turns that need the
                                          intent model to candidate NAME
                                          (from llm_router.roles.intent),
                                          spaced to stay inside its budget.
    python -m nora.evals.harvest          collect candidate utterances from
                                          the journal, nora.log and the
                                          session index for labelling.

The labelled set lives in evals/cases.jsonl and reports in evals/reports/.
Both are gitignored: they are built from what was really said (plan §5.5).
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
EVAL_DIR = ROOT / "evals"
CASES_PATH = EVAL_DIR / "cases.jsonl"
REPORTS_DIR = EVAL_DIR / "reports"
# The music names misheard requests are repaired against, frozen when the
# cases were labelled: `python -m nora.evals --freeze-names` refreshes it.
NAMES_PATH = EVAL_DIR / "music_names.json"
