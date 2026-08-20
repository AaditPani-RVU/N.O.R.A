"""Tests for the stack-solidity layer: health checks, local TTS fallback,
and LLM JSON hardening (see STACK.md).

Stdlib unittest only — run with:  python -m unittest tests.test_stack -v
"""
from __future__ import annotations

import json
import unittest
from unittest import mock

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
        # With the edge backend selected the hook must decline instantly.
        # Patched rather than read from config.yaml: this asserts the hook's
        # behaviour, not whichever backend the running machine happens to use.
        with mock.patch.object(tts_local, "get_config",
                               return_value={"speaker": {"backend": "edge"}}):
            self.assertFalse(tts_local.enabled())
            self.assertFalse(tts_local.synth_if_enabled("hello", "+20%", "/dev/null"))

    def test_enabled_follows_the_configured_backend(self):
        with mock.patch.object(tts_local, "get_config",
                               return_value={"speaker": {"backend": "kokoro"}}):
            self.assertTrue(tts_local.enabled())

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

    def test_json_mode_defaults_on_and_is_tracked_per_endpoint(self):
        # Was a single process-wide flag. Once intent parsing began failing
        # over between Groq and NVIDIA, one fallback model refusing
        # response_format would have disabled JSON mode for the primary
        # provider too — a healthy path degraded by a broken one.
        from nora import intent_parser
        self.assertEqual(intent_parser._json_mode_unsupported, set())

        groq = ("https://api.groq.com/openai/v1", "openai/gpt-oss-120b")
        nvidia = ("https://integrate.api.nvidia.com/v1", "meta/llama-3.1-8b-instruct")
        intent_parser._json_mode_unsupported.add(nvidia)
        try:
            self.assertIn(nvidia, intent_parser._json_mode_unsupported)
            self.assertNotIn(groq, intent_parser._json_mode_unsupported)
        finally:
            intent_parser._json_mode_unsupported.discard(nvidia)


if __name__ == "__main__":
    unittest.main()
