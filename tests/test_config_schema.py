"""Tests for the typed config layer (nora/config_schema.py).

The value of a declared schema is entirely in what it rejects, so most of this
file is about rejection. `cfg.get("temprature")` returning None and the caller
falling back to its own default is the failure being designed out: nothing
crashes, nothing logs, and the setting the user edited has no effect.

Three properties matter and each is easy to lose in a refactor:

  - unknown keys in a *typed* section are errors (the typo case)
  - unknown keys in an *untyped* section are not (the incremental-migration
    case — forty-one sections are still dicts, and typing one must not require
    typing all of them)
  - every problem is reported at once, not the first one

The real config.yaml is also built here, because a schema that rejects the
config the program actually ships with is worse than no schema.

Stdlib unittest only — run with:  python -m unittest tests.test_config_schema -v
"""
from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nora.config_schema import (  # noqa: E402
    ConfigError,
    SpeakerConfig,
    build_config,
)

ROOT = Path(__file__).resolve().parent.parent


def _real() -> dict:
    with open(ROOT / "config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


class RealConfigTest(unittest.TestCase):
    def test_the_shipped_config_is_valid(self):
        # If this fails, either the config or the schema is wrong — and the
        # schema is the newer of the two, so suspect it first.
        cfg = build_config(_real())
        self.assertGreater(cfg.timeouts.llm_sec, 0)
        self.assertIn(cfg.speaker.backend, SpeakerConfig.BACKENDS)
        self.assertTrue(cfg.llm_router.roles)

    def test_an_empty_config_yields_defaults_rather_than_errors(self):
        # A section that is absent is not a mistake: this is what allows a
        # section to be typed without editing anyone's existing config.
        cfg = build_config({})
        self.assertEqual(cfg.timeouts.llm_sec, 45.0)
        self.assertEqual(cfg.speaker.kokoro.voice, "bf_emma")

    def test_none_is_treated_as_empty(self):
        self.assertEqual(build_config(None).llm.max_tokens, 512)


class RejectionTest(unittest.TestCase):
    def _err(self, mutate) -> str:
        raw = copy.deepcopy(_real())
        mutate(raw)
        with self.assertRaises(ConfigError) as ctx:
            build_config(raw)
        return str(ctx.exception)

    def test_misspelled_key_is_an_error_with_a_suggestion(self):
        msg = self._err(lambda r: r["llm"].update({"temprature": 0.5}))
        self.assertIn("llm.temprature: unknown setting", msg)
        self.assertIn("did you mean 'temperature'", msg)

    def test_unknown_key_in_an_untyped_section_is_still_allowed(self):
        raw = copy.deepcopy(_real())
        raw.setdefault("ambient", {})["not_a_real_setting"] = 1
        build_config(raw)  # must not raise

    def test_out_of_range_temperature_is_rejected(self):
        self.assertIn("between 0.0 and 2.0", self._err(
            lambda r: r["llm"].update({"temperature": 10})))

    def test_non_positive_timeout_is_rejected(self):
        self.assertIn("greater than 0", self._err(
            lambda r: r["timeouts"].update({"llm_sec": 0})))

    def test_unknown_backend_is_rejected_with_the_valid_options(self):
        msg = self._err(lambda r: r["speaker"].update({"backend": "kokro"}))
        self.assertIn("is not one of", msg)
        self.assertIn("kokoro", msg)

    def test_wrong_type_is_rejected(self):
        self.assertIn("expected int", self._err(
            lambda r: r["llm"].update({"max_tokens": "lots"})))

    def test_bool_does_not_satisfy_an_int_field(self):
        # bool subclasses int, so this passes a naive isinstance check.
        self.assertIn("got a boolean", self._err(
            lambda r: r["llm"].update({"max_tokens": True})))

    def test_int_widens_to_a_float_field(self):
        raw = copy.deepcopy(_real())
        raw["transcriber"]["timeout_sec"] = 10  # int in YAML, float in schema
        self.assertEqual(build_config(raw).transcriber.timeout_sec, 10.0)

    def test_every_problem_is_reported_not_just_the_first(self):
        msg = self._err(lambda r: (
            r["llm"].update({"temprature": 1, "temperature": 9}),
            r["speaker"].update({"backend": "nope"}),
        ))
        self.assertIn("3 problem(s)", msg)
        self.assertIn("temprature", msg)
        self.assertIn("temperature:", msg)
        self.assertIn("speaker.backend", msg)

    def test_a_section_that_is_not_a_mapping_is_rejected(self):
        self.assertIn("expected a mapping", self._err(
            lambda r: r.update({"timeouts": "45"})))


class RouterTest(unittest.TestCase):
    def _err(self, mutate) -> str:
        raw = copy.deepcopy(_real())
        mutate(raw)
        with self.assertRaises(ConfigError) as ctx:
            build_config(raw)
        return str(ctx.exception)

    def test_endpoint_order_is_preserved_because_it_is_the_failover_order(self):
        roles = build_config(_real()).llm_router.roles
        names = [e.name for e in roles["intent"]]
        self.assertEqual(names, [e["name"] for e in _real()["llm_router"]["roles"]["intent"]])

    def test_a_role_with_no_endpoints_is_an_error(self):
        # Not a way to disable a role: it fails every call with nothing to fall
        # back to, which is never what was meant.
        self.assertIn("no endpoints", self._err(
            lambda r: r["llm_router"]["roles"].update({"intent": []})))

    def test_an_endpoint_without_a_model_is_an_error(self):
        self.assertIn("has no model", self._err(
            lambda r: r["llm_router"]["roles"]["intent"][0].pop("model")))

    def test_duplicate_endpoint_names_within_a_role_are_an_error(self):
        # The name is what the router log reports, so duplicates make the log
        # unable to say which endpoint answered.
        self.assertIn("duplicate endpoint name", self._err(
            lambda r: r["llm_router"]["roles"]["intent"].append(
                dict(r["llm_router"]["roles"]["intent"][0]))))

    def test_unknown_role_names_are_allowed(self):
        # Roles are named by whoever adds a call site — open-ended by design,
        # unlike settings.
        raw = copy.deepcopy(_real())
        raw["llm_router"]["roles"]["brand_new_role"] = [
            {"name": "x", "provider": "p", "model": "m"}
        ]
        self.assertIn("brand_new_role", build_config(raw).llm_router.roles)

    def test_endpoints_misplaced_outside_roles_are_caught(self):
        self.assertIn("belong under 'roles'", self._err(
            lambda r: r["llm_router"].update({"endpoints": []})))

    def test_extra_body_passes_through_as_a_mapping(self):
        eps = build_config(_real()).llm_router.roles["chat"]
        self.assertEqual(eps[0].extra_body.get("reasoning_format"), "hidden")


if __name__ == "__main__":
    unittest.main()
