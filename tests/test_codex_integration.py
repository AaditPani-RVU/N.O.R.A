"""Tests for the Codex integration layer (CODEX_INTEGRATION.md).

Stdlib unittest only — run with:  python -m unittest tests.test_codex_integration -v
State-file paths are redirected to a temp dir so real ledgers are untouched.
"""
from __future__ import annotations

import tempfile
import time
import unittest
from datetime import datetime
from unittest import mock
from pathlib import Path

from nora import (
    autonomy, cognitive_memory, confidence, consent_memory, endpoint_trust,
    focus, post_action_cards, reflection, reversible, risk, second_opinion,
    silent_hours, tool_trust, user_model,
)
from nora.schemas import ActionStep, IntentResponse, StepResult


def _intent(*actions: str, params: dict | None = None) -> IntentResponse:
    return IntentResponse(
        intent="test",
        steps=[ActionStep(action=a, parameters=params or {}) for a in actions],
    )


class IsolatedStateMixin(unittest.TestCase):
    """Redirect every ledger to a temp dir and reset module caches."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        tool_trust._STORE_PATH = tmp / "trust.json"
        tool_trust._stats = {}
        tool_trust._loaded = False
        consent_memory._STORE_PATH = tmp / "consent.json"
        consent_memory._events = []
        consent_memory._loaded = False
        autonomy._session_reversals = {}
        cognitive_memory._USER_MODEL_PATH = tmp / "user_model.json"
        cognitive_memory._user_model = None
        user_model._PREFS_PATH = tmp / "prefs.json"
        user_model._prefs = None
        post_action_cards._cards = []
        reversible._STORE_PATH = tmp / "reversible.json"
        reversible._entries = []
        reversible._loaded = False

    def tearDown(self) -> None:
        self._tmp.cleanup()


class TestRiskAggregation(IsolatedStateMixin):
    def test_max_rule_not_average(self):
        # One critical dimension escalates the whole action (Codex rule)
        r = risk.Risk(destructiveness=1, scale=4, uncertainty=0)
        self.assertEqual(r.level(), 4)

    def test_unknown_action_is_high_risk(self):
        r = risk.assess(_intent("definitely_not_registered_xyz"))
        self.assertGreaterEqual(r.level(), 3)

    def test_registered_low_risk_action(self):
        from nora import command_engine

        @command_engine.register("codex_test_noop", risk="low", category="system")
        def _noop() -> str:
            return "ok"

        r = risk.assess(_intent("codex_test_noop"))
        self.assertLessEqual(r.destructiveness, 1)

    def test_scale_dimension_from_params(self):
        from nora import command_engine

        @command_engine.register("codex_test_bulk", risk="low", category="system")
        def _bulk(count: int = 1) -> str:
            return "ok"

        r = risk.assess(_intent("codex_test_bulk", params={"count": 500}))
        self.assertGreaterEqual(r.scale, 3)
        r2 = risk.assess(_intent("codex_test_bulk", params={"target": "all"}))
        self.assertGreaterEqual(r2.scale, 3)


class TestToolTrust(IsolatedStateMixin):
    def test_score_rises_with_success(self):
        base = tool_trust.score("t1")
        for _ in range(10):
            tool_trust.record("t1", True)
        self.assertGreater(tool_trust.score("t1"), base)
        self.assertTrue(tool_trust.is_proven("t1"))

    def test_reversal_hits_harder_than_failure(self):
        for _ in range(5):
            tool_trust.record("t_fail", True)
            tool_trust.record("t_rev", True)
        tool_trust.record("t_fail", False)
        tool_trust.record_reversal("t_rev")
        self.assertLess(tool_trust.score("t_rev"), tool_trust.score("t_fail"))

    def test_new_tool_is_unproven(self):
        self.assertFalse(tool_trust.is_proven("brand_new_tool"))


class TestConsentMemory(IsolatedStateMixin):
    def test_always_approves_after_consistent_yes(self):
        for _ in range(6):
            consent_memory.record(["open_app"], approved=True)
        self.assertTrue(consent_memory.always_approves("open_app"))
        self.assertFalse(consent_memory.always_denies("open_app"))

    def test_always_denies_after_consistent_no(self):
        for _ in range(5):
            consent_memory.record(["send_email"], approved=False)
        self.assertTrue(consent_memory.always_denies("send_email"))

    def test_consent_decay_ages_out_old_yes(self):
        # Six-month-old approvals decay to negligible effective sample size
        old_ts = time.time() - 180 * 86400
        consent_memory._load()
        for _ in range(6):
            consent_memory._events.insert(
                0, {"ts": old_ts, "action": "old_thing", "approved": True}
            )
        rate, n = consent_memory.approval_stats("old_thing")
        self.assertLess(n, 1.0)
        self.assertFalse(consent_memory.always_approves("old_thing"))


class TestAutonomy(IsolatedStateMixin):
    def test_high_risk_needs_consent(self):
        tier = autonomy.classify(_intent("anything"), risk.Risk(destructiveness=3))
        self.assertIs(tier, autonomy.AutonomyTier.NEEDS_CONSENT)

    def test_proven_low_risk_is_trusted(self):
        from nora import command_engine

        @command_engine.register("codex_test_trusted", risk="low", category="system")
        def _t() -> str:
            return "ok"

        for _ in range(10):
            tool_trust.record("codex_test_trusted", True)
        tier = autonomy.classify(_intent("codex_test_trusted"), risk.Risk(destructiveness=1))
        self.assertIs(tier, autonomy.AutonomyTier.TRUSTED)

    def test_always_denied_becomes_suggest(self):
        for _ in range(5):
            consent_memory.record(["risky_thing"], approved=False)
        tier = autonomy.classify(_intent("risky_thing"), risk.Risk(destructiveness=1))
        self.assertIs(tier, autonomy.AutonomyTier.SUGGEST)

    def test_undo_frequency_demotion(self):
        for _ in range(10):
            tool_trust.record("flaky", True)
        for _ in range(3):
            autonomy.note_reversal("flaky")
        tier = autonomy.classify(_intent("flaky"), risk.Risk(destructiveness=1))
        self.assertIs(tier, autonomy.AutonomyTier.NEEDS_CONSENT)

    def test_consent_skip_requires_opt_in(self):
        # Default config has allow_consent_skip: false — never skip
        for _ in range(10):
            tool_trust.record("open_app", True)
            consent_memory.record(["open_app"], approved=True)
        self.assertFalse(autonomy.may_skip_confirmation(_intent("open_app")))

    def test_hard_destructive_never_skips(self):
        cfg = __import__("nora.config", fromlist=["get_config"]).get_config()
        cfg.setdefault("autonomy", {})["allow_consent_skip"] = True
        try:
            for _ in range(10):
                tool_trust.record("delete_file", True)
                consent_memory.record(["delete_file"], approved=True)
            self.assertFalse(autonomy.may_skip_confirmation(_intent("delete_file")))
        finally:
            cfg["autonomy"]["allow_consent_skip"] = False


class TestFocus(IsolatedStateMixin):
    def test_fails_soft_to_available(self):
        focus.note_activity()
        focus._cache = None
        state = focus.current()
        self.assertIn(state, (focus.FocusState.AVAILABLE, focus.FocusState.MEDIA,
                              focus.FocusState.CALL))

    def test_away_after_inactivity(self):
        focus._last_activity_ts = time.time() - 100000
        self.assertIs(focus.current(), focus.FocusState.AWAY)
        focus.note_activity()
        self.assertIsNot(focus.current(), focus.FocusState.AWAY)

    def test_gated_speech_defers_when_away(self):
        spoken: list[str] = []
        gated = focus.gated(spoken.append)
        focus._last_activity_ts = time.time() - 100000
        gated("suggestion while away")
        self.assertEqual(spoken, [])
        # The flush asks the machine whether it may speak: PipeWire (a mic
        # stream open right now reads as CALL) and the learned quiet hours.
        # Pin both, or this passes or fails depending on what the box is doing.
        with mock.patch.object(focus, "_pipewire_state",
                               return_value=focus.FocusState.AVAILABLE), \
             mock.patch("nora.silent_hours.is_silent_now", return_value=False):
            focus._cache = None
            focus.note_activity()  # flush on return
        self.assertEqual(spoken, ["suggestion while away"])

    def test_deferred_speech_survives_a_still_busy_return(self):
        """Coming back mid-call must re-time the suggestion, not bin it."""
        spoken: list[str] = []
        gated = focus.gated(spoken.append)
        focus._last_activity_ts = time.time() - 100000
        gated("suggestion while away")

        with mock.patch.object(focus, "_pipewire_state",
                               return_value=focus.FocusState.CALL):
            focus._cache = None
            focus.note_activity()
        self.assertEqual(spoken, [])
        self.assertEqual(focus._deferred, ["suggestion while away"])

        with mock.patch.object(focus, "_pipewire_state",
                               return_value=focus.FocusState.AVAILABLE), \
             mock.patch("nora.silent_hours.is_silent_now", return_value=False):
            focus._cache = None
            focus.note_activity()
        self.assertEqual(spoken, ["suggestion while away"])


class TestReflection(IsolatedStateMixin):
    def test_report_on_empty_logs(self):
        reflection._AUDIT_PATH = Path(self._tmp.name) / "audit.jsonl"
        reflection._REVERSIBLE_PATH = Path(self._tmp.name) / "rev.json"
        self.assertIn("no recorded actions", reflection.report(7))

    def test_flags_reversal_pattern(self):
        import json

        tmp = Path(self._tmp.name)
        reflection._AUDIT_PATH = tmp / "audit.jsonl"
        reflection._REVERSIBLE_PATH = tmp / "rev.json"
        now = time.time()
        with reflection._AUDIT_PATH.open("w") as f:
            for _ in range(4):
                f.write(json.dumps({"ts": now, "action": "rename_file", "success": True}) + "\n")
        reflection._REVERSIBLE_PATH.write_text(json.dumps([
            {"ts": now, "action": "rename_file", "undone": True},
            {"ts": now, "action": "rename_file", "undone": True},
        ]))
        result = reflection.analyze(7)
        self.assertTrue(any(c["action"] == "rename_file" and c["kind"] == "reversal"
                            for c in result["concerns"]))


class TestReversible(IsolatedStateMixin):
    def test_bundle_undo_reverses_group(self):
        from nora import command_engine

        calls: list[str] = []

        @command_engine.register("codex_test_inverse", risk="low", category="system")
        def _inv() -> str:
            calls.append("undone")
            return "reverted"

        reversible.record_action("codex_test_fwd", {}, "codex_test_inverse", {}, "did A", bundle_tag="batch")
        reversible.record_action("codex_test_fwd", {}, "codex_test_inverse", {}, "did B", bundle_tag="batch")
        result = reversible.undo_bundle("batch")
        self.assertEqual(len(calls), 2)
        self.assertIn("Undid 2", result)

    def test_preview_does_not_execute(self):
        from nora import command_engine

        calls: list[str] = []

        @command_engine.register("codex_test_inverse2", risk="low", category="system")
        def _inv() -> str:
            calls.append("undone")
            return "reverted"

        reversible.record_action("codex_test_fwd2", {}, "codex_test_inverse2", {}, "did C")
        preview = reversible.preview_undo()
        self.assertIn("codex_test_inverse2", preview)
        self.assertEqual(calls, [])

    def test_announcement_empty_for_irreversible(self):
        entry = reversible.record_action("codex_test_irrev", {}, None, {}, "typed some text")
        self.assertEqual(reversible.format_undo_announcement(entry), "")

    def test_announcement_quotes_window(self):
        entry = reversible.record_action("codex_test_fwd3", {}, "codex_test_inverse3", {}, "did D")
        self.assertIn("30 seconds", reversible.format_undo_announcement(entry))


class TestUserModelVersioning(IsolatedStateMixin):
    def test_proposal_holds_until_confirmed(self):
        user_model.propose_preference("tone", "concise", reason="short replies observed")
        self.assertIsNone(user_model.get_preference("tone"))
        user_model.confirm_preference("tone")
        self.assertEqual(user_model.get_preference("tone"), "concise")

    def test_reject_keeps_prior_value(self):
        user_model.propose_preference("tone", "concise", reason="a")
        user_model.confirm_preference("tone")
        user_model.propose_preference("tone", "verbose", reason="b")
        user_model.reject_preference("tone")
        self.assertEqual(user_model.get_preference("tone"), "concise")

    def test_rollback_restores_previous_version(self):
        user_model.propose_preference("tone", "concise", reason="a")
        user_model.confirm_preference("tone")
        user_model.propose_preference("tone", "verbose", reason="b")
        user_model.confirm_preference("tone")
        user_model.rollback_preference("tone")
        self.assertEqual(user_model.get_preference("tone"), "concise")

    def test_apply_pending_after_window(self):
        entry = user_model.propose_preference("tone", "concise", reason="a")
        self.assertFalse(entry["applied"])
        applied = user_model.apply_pending(now=time.time() + 999999)
        self.assertIn("tone", applied)
        self.assertEqual(user_model.get_preference("tone"), "concise")


class TestConfidence(IsolatedStateMixin):
    def test_error_intent_is_low_confidence(self):
        bad = IntentResponse(intent="x", steps=[], error="couldn't parse")
        self.assertLess(confidence.estimate(bad, "asdf"), 0.2)

    def test_unknown_action_is_low_confidence(self):
        i = _intent("definitely_not_registered_xyz")
        self.assertLess(confidence.estimate(i, "do the thing"), 0.3)

    def test_ambiguous_short_referent_lowers_confidence(self):
        from nora import command_engine

        @command_engine.register("codex_test_conf", risk="low", category="system")
        def _f() -> str:
            return "ok"

        i = _intent("codex_test_conf")
        high = confidence.estimate(i, "please run codex_test_conf now for me")
        low = confidence.estimate(i, "delete it")
        self.assertLess(low, high)

    def test_needs_clarification_threshold(self):
        self.assertTrue(confidence.needs_clarification(0.1))
        self.assertFalse(confidence.needs_clarification(0.9))

    def test_uncertainty_template_mentions_confidence_and_hint(self):
        msg = confidence.uncertainty_response("Chrome", "it was the last app you opened", 0.3, "asking which app")
        self.assertIn("Confidence: low", msg)
        self.assertIn("asking which app", msg)


class TestEndpointTrust(IsolatedStateMixin):
    def test_well_known_service_is_proven_by_default(self):
        self.assertTrue(endpoint_trust.is_proven("org.freedesktop.Notifications"))

    def test_new_service_is_unproven(self):
        self.assertFalse(endpoint_trust.is_proven("com.example.RandomPlugin"))

    def test_proven_after_clean_runs(self):
        for _ in range(10):
            endpoint_trust.record("com.example.RandomPlugin", True)
        self.assertTrue(endpoint_trust.is_proven("com.example.RandomPlugin"))


class TestPostActionCards(IsolatedStateMixin):
    def test_build_and_explain(self):
        i = _intent("open_app")
        results = [StepResult(action="open_app", success=True, message="opened")]
        post_action_cards.build("open chrome", i, results, 0.9)
        text = post_action_cards.explain_last()
        self.assertIn("open chrome", text)
        self.assertIn("open_app", text)

    def test_explain_with_no_cards(self):
        self.assertIn("haven't done anything", post_action_cards.explain_last())


class TestSecondOpinion(IsolatedStateMixin):
    def test_flags_unexpected_argument(self):
        from nora import command_engine

        @command_engine.register("codex_test_so", risk="low", category="system")
        def _f(path: str) -> str:
            return "ok"

        i = _intent("codex_test_so", params={"path": "/tmp/x", "bogus_arg": 1})
        flagged, reason = second_opinion.review(i)
        self.assertTrue(flagged)
        self.assertIn("bogus_arg", reason)

    def test_flags_missing_required_argument(self):
        from nora import command_engine

        @command_engine.register("codex_test_so2", risk="low", category="system")
        def _f(path: str) -> str:
            return "ok"

        i = _intent("codex_test_so2")
        flagged, _reason = second_opinion.review(i)
        self.assertTrue(flagged)

    def test_clean_call_not_flagged(self):
        from nora import command_engine

        @command_engine.register("codex_test_so3", risk="low", category="system")
        def _f(path: str = "/tmp") -> str:
            return "ok"

        i = _intent("codex_test_so3")
        flagged, _reason = second_opinion.review(i)
        self.assertFalse(flagged)

    def test_flags_destructive_name_registered_low_risk(self):
        from nora import command_engine

        @command_engine.register("codex_test_delete_thing", risk="low", category="system")
        def _f() -> str:
            return "ok"

        i = _intent("codex_test_delete_thing")
        flagged, _reason = second_opinion.review(i)
        self.assertTrue(flagged)


class TestSilentHours(IsolatedStateMixin):
    def test_no_history_never_silent(self):
        self.assertFalse(silent_hours.is_silent_now())

    def test_quiet_bin_detected_with_history(self):
        from nora.cognitive_memory import _load_user_model, _save_user_model, _time_bin

        m = _load_user_model()
        now = datetime.now()
        quiet_tb = _time_bin(now)
        quiet_dow = str(now.weekday())
        for tb in m["activity_heatmap"]:
            for dow in m["activity_heatmap"][tb]:
                if tb == quiet_tb and dow == quiet_dow:
                    continue
                m["activity_heatmap"][tb][dow] = [f"action_{i}" for i in range(30)]
        _save_user_model()
        self.assertTrue(silent_hours.is_silent_now(now))


class TestCommandRegistration(unittest.TestCase):
    def test_voice_commands_registered(self):
        import nora.commands.autonomy_commands  # noqa: F401  (registers on import)
        from nora import command_engine

        actions = command_engine.get_available_actions()
        for name in (
            "autonomy_status", "focus_status", "trust_report", "reflection_report",
            "preference_status", "confirm_preference", "reject_preference", "rollback_preference",
            "endpoint_trust_report", "explain_last_action", "silent_hours_status",
        ):
            self.assertIn(name, actions)

        from nora import reversible as _rv  # noqa: F401  (registers on import)
        for name in ("undo_bundle", "preview_undo"):
            self.assertIn(name, actions)


if __name__ == "__main__":
    unittest.main()
