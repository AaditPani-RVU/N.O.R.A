"""Autonomy tiering — Codex integration 2.1 + 5.12 (see CODEX_INTEGRATION.md).

Replaces NORA's binary allowed/confirm split with five tiers taken from
Codex's Autonomy Charter. The tier is a function of aggregated risk
(nora.risk), per-tool reliability (nora.tool_trust), the user's consent
pattern (nora.consent_memory), and attention state (nora.focus).

Safety invariants:
  - This layer can only ESCALATE confirmation on its own.
  - Skipping a confirmation via learned consent requires the explicit
    config opt-in autonomy.allow_consent_skip, and never applies to
    high-risk or destructive-set actions.
  - Undo-frequency alert: if the user reverses an action class more than
    N times in a session, that class is demoted to NEEDS_CONSENT for the
    rest of the session.
"""
from __future__ import annotations

import logging
import threading
from enum import Enum

from nora.config import get_config
from nora.risk import Risk
from nora.schemas import IntentResponse

logger = logging.getLogger("nora.autonomy")

_lock = threading.Lock()
# action -> reversal count this session (5.12)
_session_reversals: dict[str, int] = {}

# Actions that must never lose their confirmation prompt, no matter how
# often the user has approved them. Mirrors the pipeline's destructive set.
_HARD_CONFIRM = {
    "delete_file", "shutdown", "close_all_apps", "patch_file",
    "git_smart_commit", "move_file", "undo_actions_since",
}


class AutonomyTier(Enum):
    OBSERVE = 0        # log intent, never execute
    SUGGEST = 1        # present the plan, wait for a verbal go-ahead
    ANNOUNCE = 2       # execute, then say "I did X"
    TRUSTED = 3        # execute silently; still audit-logged
    NEEDS_CONSENT = 4  # explicit confirmation before anything runs


def _enabled() -> bool:
    return bool(get_config().get("autonomy", {}).get("enabled", True))


def note_reversal(action: str) -> None:
    """Called when the user undoes an action. Demotes repeat offenders."""
    limit = int(get_config().get("autonomy", {}).get("session_reversal_limit", 3))
    with _lock:
        _session_reversals[action] = _session_reversals.get(action, 0) + 1
        count = _session_reversals[action]
    if count >= limit:
        logger.info("autonomy: %s demoted to NEEDS_CONSENT for this session "
                    "(%d reversals)", action, count)


def _session_demoted(action: str) -> bool:
    limit = int(get_config().get("autonomy", {}).get("session_reversal_limit", 3))
    with _lock:
        return _session_reversals.get(action, 0) >= limit


def classify(intent: IntentResponse, risk: Risk) -> AutonomyTier:
    """Pick the autonomy tier for this intent. Called once per command."""
    if not _enabled():
        # Legacy behavior: the pipeline's own confirmation logic decides.
        return AutonomyTier.ANNOUNCE

    from nora import consent_memory, endpoint_trust, tool_trust

    actions = [s.action for s in intent.steps]
    level = risk.level()

    if any(_session_demoted(a) for a in actions):
        return AutonomyTier.NEEDS_CONSENT
    if level >= 3:
        return AutonomyTier.NEEDS_CONSENT
    if any(consent_memory.always_denies(a) for a in actions):
        return AutonomyTier.SUGGEST

    # dbus_call() is one generic action reaching any D-Bus service — gate on
    # the target service's own trust, not just the action name (2.4 → 5.7).
    unproven_endpoints = any(
        s.action == "dbus_call" and not endpoint_trust.is_proven(s.parameters.get("service", ""))
        for s in intent.steps
    )

    unproven = [a for a in actions if not tool_trust.is_proven(a)]
    if (unproven or unproven_endpoints) and level >= 2:
        # new-tool caution: medium-risk work by an unproven tool asks first
        return AutonomyTier.NEEDS_CONSENT

    # Second-opinion mode (5.8): an independent rule-based sanity pass on
    # every high-stakes plan (level >= 2). Disagreement escalates — it
    # never downgrades a tier the risk/trust checks above already picked.
    if level >= 2:
        from nora import second_opinion

        flagged, reason = second_opinion.review(intent)
        if flagged:
            logger.info("autonomy: second opinion disagreed — %s", reason)
            return AutonomyTier.NEEDS_CONSENT

    if level <= 1 and not unproven and not unproven_endpoints:
        return AutonomyTier.TRUSTED
    return AutonomyTier.ANNOUNCE


def may_skip_confirmation(intent: IntentResponse) -> bool:
    """Learned-consent skip: only with config opt-in, only for safe classes."""
    if not _enabled():
        return False
    if not get_config().get("autonomy", {}).get("allow_consent_skip", False):
        return False

    from nora import command_engine, consent_memory, tool_trust

    for step in intent.steps:
        action = step.action
        if action in _HARD_CONFIRM or _session_demoted(action):
            return False
        meta = command_engine.get_action_meta(action)
        if meta is None or meta.risk == "high":
            return False
        if not (consent_memory.always_approves(action) and tool_trust.is_proven(action)):
            return False
    logger.info("autonomy: confirmation skipped via learned consent for %s",
                [s.action for s in intent.steps])
    return True


def status() -> str:
    """Natural-language summary for the voice interface."""
    if not _enabled():
        return "Autonomy tiering is disabled; I confirm based on the fixed rules only."
    cfg = get_config().get("autonomy", {})
    skip = "on" if cfg.get("allow_consent_skip", False) else "off"
    with _lock:
        demoted = [a for a, c in _session_reversals.items()
                   if c >= int(cfg.get("session_reversal_limit", 3))]
    parts = [f"Autonomy tiering is active. Learned-consent skipping is {skip}."]
    if demoted:
        parts.append(
            "Demoted to always-confirm this session after repeated undos: "
            + ", ".join(demoted) + "."
        )
    return " ".join(parts)
