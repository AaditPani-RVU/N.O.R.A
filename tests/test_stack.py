"""Tests for the stack-solidity layer: health checks, local TTS fallback,
and LLM JSON hardening (see STACK.md).

Stdlib unittest only — run with:  python -m unittest tests.test_stack -v
"""
from __future__ import annotations

import json
import unittest

from nora import health, tts_local


class TestHealth(unittest.TestCase):
    def test_run_checks_never_raises_and_shapes_are_right(self):
        results = health.run_checks()
        self.assertGreaterEqual(len(results), 8)
        for r in results:
            self.assertIn("name", r)
            self.assertIsInstance(r["ok"], bool)
            self.assertIsInstance(r["detail"], str)

    def test_report_is_speakable(self):
        text = health.report()
        self.assertTrue(text.endswith(".") and ("healthy" in text))

    def test_command_registered(self):
        import nora.commands.health_commands  # noqa: F401  (registers on import)
        from nora import command_engine
        self.assertIn("health_check", command_engine.get_available_actions())


class TestTTSLocal(unittest.TestCase):
    def test_disabled_backend_is_a_fast_noop(self):
        # default config backend is edge — the hook must decline instantly
        self.assertFalse(tts_local.enabled())
        self.assertFalse(tts_local.synth_if_enabled("hello", "+20%", "/dev/null"))

    def test_rate_to_speed_mapping(self):
        self.assertEqual(tts_local._rate_to_speed("+20%"), 1.2)
        self.assertEqual(tts_local._rate_to_speed("-15%"), 0.85)
        self.assertEqual(tts_local._rate_to_speed("garbage"), 1.0)
        self.assertEqual(tts_local._rate_to_speed("+150%"), 2.0)  # clamped


class TestIntentSchemaHardening(unittest.TestCase):
    def test_schema_is_valid_json_and_matches_pydantic(self):
        from nora.intent_parser import _intent_json_schema
        from nora.schemas import IntentResponse

        schema = _intent_json_schema()
        json.dumps(schema)  # serializable — Ollama gets it verbatim
        self.assertEqual(schema["required"], ["intent", "steps"])
        # A document following the constrained shape must validate as IntentResponse
        IntentResponse.model_validate({
            "intent": "open an app",
            "steps": [{"action": "open_app", "parameters": {"name": "chrome"}}],
            "requires_confirmation": False,
        })

    def test_json_mode_flag_defaults_on(self):
        from nora import intent_parser
        self.assertTrue(intent_parser._json_mode_ok)


if __name__ == "__main__":
    unittest.main()
