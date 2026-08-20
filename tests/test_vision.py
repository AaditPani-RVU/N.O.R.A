"""Tests for the vision module (NORA-vision-plan.md Phases 1 and 2).

Stdlib unittest only — run with:  python -m unittest tests.test_vision -v

No camera and no ONNX model are required: detection and embedding are
stubbed so the tracking, greeting-cadence, and privacy logic can be
exercised deterministically. The face store is redirected to a temp dir
so a real ~/.nora/faces.json is never touched.
"""
from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from nora import context
from nora.vision import camera, faces, perception, presence


def _bbox(x: float, y: float = 0.0, size: float = 100.0) -> tuple:
    return (x, y, x + size, y + size)


def _det(x: float, y: float = 0.0, size: float = 100.0) -> dict:
    return {"bbox": _bbox(x, y, size), "kps": None, "score": 0.9}


class FaceStoreTest(unittest.TestCase):
    """Persistence, matching, and the deletion guarantees Privacy rule 4 requires."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = faces._PATH
        faces._PATH = Path(self._tmp.name) / "faces.json"

    def tearDown(self) -> None:
        faces._PATH = self._orig
        self._tmp.cleanup()

    def test_enroll_writes_expected_schema(self) -> None:
        faces.enroll("Aadit", [[1.0, 0.0, 0.0]])
        data = json.loads(faces._PATH.read_text())
        person = data["people"]["aadit"]
        self.assertEqual(person["display_name"], "Aadit")
        self.assertEqual(person["embeddings"], [[1.0, 0.0, 0.0]])
        self.assertFalse(person["trusted"])
        self.assertIn("enrolled_at", person)

    def test_enroll_is_additive_and_capped(self) -> None:
        for _ in range(25):
            faces.enroll("aadit", [[1.0, 0.0, 0.0]])
        stored = json.loads(faces._PATH.read_text())["people"]["aadit"]["embeddings"]
        self.assertEqual(len(stored), 20)

    def test_enroll_rejects_empty_input(self) -> None:
        with self.assertRaises(ValueError):
            faces.enroll("", [[1.0, 0.0]])
        with self.assertRaises(ValueError):
            faces.enroll("aadit", [])

    def test_identify_matches_above_threshold_only(self) -> None:
        faces.enroll("aadit", [[1.0, 0.0, 0.0]])
        name, score = faces.identify([1.0, 0.0, 0.0])
        self.assertEqual(name, "aadit")
        self.assertAlmostEqual(score, 1.0, places=5)

        name, score = faces.identify([0.0, 1.0, 0.0])   # orthogonal → no match
        self.assertIsNone(name)
        self.assertLess(score, 0.5)

    def test_identify_on_empty_store(self) -> None:
        self.assertEqual(faces.identify([1.0, 0.0, 0.0]), (None, 0.0))
        self.assertEqual(faces.identify(None), (None, 0.0))

    def test_store_survives_a_new_read(self) -> None:
        faces.enroll("Aadit", [[1.0, 0.0, 0.0]])
        # Nothing is cached in memory — identify() re-reads the file every call.
        self.assertEqual(faces.identify([1.0, 0.0, 0.0])[0], "aadit")
        self.assertEqual([p["name"] for p in faces.list_people()], ["Aadit"])

    def test_forget_actually_rewrites_the_file(self) -> None:
        faces.enroll("aadit", [[1.0, 0.0, 0.0]])
        faces.enroll("guest", [[0.0, 1.0, 0.0]])

        self.assertFalse(faces.forget("nobody"))
        self.assertTrue(faces.forget("Aadit"))

        people = json.loads(faces._PATH.read_text())["people"]
        self.assertNotIn("aadit", people)      # gone, not tombstoned
        self.assertIn("guest", people)
        self.assertIsNone(faces.identify([1.0, 0.0, 0.0])[0])

    def test_forget_all_wipes_the_file(self) -> None:
        faces.enroll("aadit", [[1.0, 0.0, 0.0]])
        faces.enroll("guest", [[0.0, 1.0, 0.0]])
        self.assertEqual(faces.forget_all(), 2)
        self.assertEqual(json.loads(faces._PATH.read_text())["people"], {})
        self.assertEqual(faces.count(), 0)

    def test_corrupt_store_does_not_raise(self) -> None:
        faces._PATH.parent.mkdir(parents=True, exist_ok=True)
        faces._PATH.write_text("{ not json")
        self.assertEqual(faces.list_people(), [])
        faces.enroll("aadit", [[1.0, 0.0, 0.0]])       # recovers by rewriting
        self.assertEqual(faces.count(), 1)

    def test_trusted_defaults_false(self) -> None:
        """A face must never grant trust on its own — a photo defeats it."""
        faces.enroll("aadit", [[1.0, 0.0, 0.0]])
        self.assertFalse(faces.is_trusted("aadit"))


class TrackingTest(unittest.TestCase):
    """Identity is resolved once per track and carried by bbox continuity."""

    def setUp(self) -> None:
        perception._tracks = []
        perception._greeted_at.clear()

    def test_embedding_runs_once_per_track_not_per_frame(self) -> None:
        with mock.patch.object(faces, "detect", return_value=[_det(0)]), \
             mock.patch.object(faces, "embed", return_value=[1.0]) as embed, \
             mock.patch.object(faces, "identify", return_value=("aadit", 0.9)):
            for _ in range(10):
                perception._process_frame(object())

        self.assertEqual(embed.call_count, 1, "embedded more than once for a stable face")

    def test_presence_requires_consecutive_confirmations(self) -> None:
        with mock.patch.object(faces, "detect", return_value=[_det(0)]), \
             mock.patch.object(faces, "embed", return_value=[1.0]), \
             mock.patch.object(faces, "identify", return_value=("aadit", 0.9)):
            seen = [perception._process_frame(object())[0] for _ in range(4)]

        self.assertEqual(seen[0], [], "declared presence on the very first frame")
        self.assertEqual(seen[-1], ["aadit"])

    def test_brief_dropout_does_not_withdraw_presence(self) -> None:
        """A blink or a turned head must not end the visit."""
        with mock.patch.object(faces, "embed", return_value=[1.0]), \
             mock.patch.object(faces, "identify", return_value=("aadit", 0.9)):
            with mock.patch.object(faces, "detect", return_value=[_det(0)]):
                for _ in range(4):
                    perception._process_frame(object())

            with mock.patch.object(faces, "detect", return_value=[]):
                present = perception._process_frame(object())[0]
            self.assertEqual(present, ["aadit"], "dropped presence after a single miss")

    def test_sustained_absence_withdraws_presence(self) -> None:
        with mock.patch.object(faces, "embed", return_value=[1.0]), \
             mock.patch.object(faces, "identify", return_value=("aadit", 0.9)):
            with mock.patch.object(faces, "detect", return_value=[_det(0)]):
                for _ in range(4):
                    perception._process_frame(object())
            with mock.patch.object(faces, "detect", return_value=[]):
                for _ in range(perception._ABSENT_FRAMES + 1):
                    present = perception._process_frame(object())[0]
        self.assertEqual(present, [])

    def test_recovered_track_is_re_embedded(self) -> None:
        """A track that lost its detection may be a different person."""
        with mock.patch.object(faces, "embed", return_value=[1.0]) as embed, \
             mock.patch.object(faces, "identify", return_value=("aadit", 0.9)):
            with mock.patch.object(faces, "detect", return_value=[_det(0)]):
                perception._process_frame(object())
            with mock.patch.object(faces, "detect", return_value=[]):
                perception._process_frame(object())
            with mock.patch.object(faces, "detect", return_value=[_det(0)]):
                perception._process_frame(object())

        self.assertEqual(embed.call_count, 2)

    def test_two_faces_tracked_independently(self) -> None:
        dets = [_det(0), _det(300)]
        embeddings = {0.0: "aadit", 300.0: None}

        def fake_embed(frame, det):
            return det["bbox"][0]

        def fake_identify(emb):
            name = embeddings[emb]
            return (name, 0.9) if name else (None, 0.1)

        with mock.patch.object(faces, "detect", return_value=dets), \
             mock.patch.object(faces, "embed", side_effect=fake_embed), \
             mock.patch.object(faces, "identify", side_effect=fake_identify):
            for _ in range(4):
                present, unknown = perception._process_frame(object())

        self.assertEqual(present, ["aadit"])
        self.assertEqual(unknown, 1)

    def test_unknown_face_persists_nothing(self) -> None:
        """Privacy rule 2: an unrecognized person leaves no trace on disk."""
        tmp = tempfile.TemporaryDirectory()
        orig = faces._PATH
        faces._PATH = Path(tmp.name) / "faces.json"
        try:
            with mock.patch.object(faces, "detect", return_value=[_det(0)]), \
                 mock.patch.object(faces, "embed", return_value=[1.0]), \
                 mock.patch.object(faces, "identify", return_value=(None, 0.2)), \
                 mock.patch.object(faces, "enroll") as enroll:
                for _ in range(5):
                    present, unknown = perception._process_frame(object())

            self.assertEqual(present, [])
            self.assertEqual(unknown, 1)
            enroll.assert_not_called()
            self.assertFalse(faces._PATH.exists(), "an unknown face created a store file")
        finally:
            faces._PATH = orig
            tmp.cleanup()


class GreetingCadenceTest(unittest.TestCase):
    """Greet on arrival, not once per process — the long-lived-daemon case."""

    def setUp(self) -> None:
        perception._greeted_at.clear()
        self.spoken: list[str] = []
        perception.set_speak_hook(self.spoken.append)
        self._cfg = {
            "enabled": True,
            "face": {"greet_on_first_sight": True, "regreet_after_seconds": 1800},
        }
        self._patch = mock.patch.object(perception, "_cfg", return_value=self._cfg)
        self._patch.start()
        self._name = mock.patch.object(faces, "display_name", side_effect=str.title)
        self._name.start()

    def tearDown(self) -> None:
        self._patch.stop()
        self._name.stop()
        perception.set_speak_hook(None)

    def _greet(self, last_seen: dict) -> None:
        # _maybe_greet dispatches TTS on a thread; the hook is called there.
        perception._maybe_greet(["aadit"], last_seen)
        time.sleep(0.05)

    def test_greets_on_arrival(self) -> None:
        self._greet({})
        self.assertEqual(self.spoken, ["Hey Aadit."])

    def test_does_not_repeat_while_still_in_frame(self) -> None:
        now = time.time()
        self._greet({})
        for _ in range(5):
            self._greet({"aadit": now})      # continuously visible
        self.assertEqual(self.spoken, ["Hey Aadit."])

    def test_does_not_repeat_after_a_brief_absence(self) -> None:
        self._greet({})
        self._greet({"aadit": time.time() - 60})   # away one minute
        self.assertEqual(len(self.spoken), 1)

    def test_greets_again_after_a_long_absence(self) -> None:
        """Greeted at 09:00, away all day, back at 18:00 — must greet again."""
        self._greet({})
        self._greet({"aadit": time.time() - 9 * 3600})
        self.assertEqual(self.spoken, ["Hey Aadit.", "Hey Aadit."])

    def test_regreet_zero_is_strict_once_per_process(self) -> None:
        self._cfg["face"]["regreet_after_seconds"] = 0
        self._greet({})
        self._greet({"aadit": time.time() - 9 * 3600})
        self.assertEqual(len(self.spoken), 1)

    def test_greeting_can_be_disabled(self) -> None:
        self._cfg["face"]["greet_on_first_sight"] = False
        self._greet({})
        self.assertEqual(self.spoken, [])


class VisionContextTest(unittest.TestCase):
    def test_update_stamps_last_seen_for_present_people(self) -> None:
        context.update_vision(present=["aadit"], unknown_count=0, camera_active=True)
        state = context.get_vision()
        self.assertEqual(state["present"], ["aadit"])
        self.assertTrue(state["camera_active"])
        self.assertAlmostEqual(state["last_seen"]["aadit"], time.time(), delta=5)

    def test_last_seen_is_retained_after_departure(self) -> None:
        context.update_vision(present=["aadit"])
        context.update_vision(present=[], unknown_count=0)
        self.assertIn("aadit", context.get_vision()["last_seen"])


class CameraGatingTest(unittest.TestCase):
    """Off means off: an explicit close outranks the reference count."""

    def setUp(self) -> None:
        camera.resume()

    def tearDown(self) -> None:
        camera.resume()

    def test_force_off_suspends_further_acquires(self) -> None:
        camera.force_off()
        self.assertTrue(camera.is_suspended())
        self.assertFalse(camera.acquire("perception"),
                         "perception reopened the camera after an explicit close")
        self.assertFalse(camera.is_active())
        self.assertEqual(camera.holders(), [])

    def test_resume_clears_the_latch_without_opening(self) -> None:
        camera.force_off()
        camera.resume()
        self.assertFalse(camera.is_suspended())
        self.assertFalse(camera.is_active(), "resume() opened the device on its own")

    def test_missing_opencv_fails_soft(self) -> None:
        with mock.patch.dict("sys.modules", {"cv2": None}):
            self.assertFalse(camera.acquire("test"))
        self.assertFalse(camera.is_active())


class CommandRegistrationTest(unittest.TestCase):
    def test_all_phase_one_actions_are_registered(self) -> None:
        from nora import command_engine
        import nora.commands.vision_commands  # noqa: F401

        expected = {
            "open_camera", "close_camera", "what_do_you_see", "who_is_there",
            "learn_face", "forget_face", "forget_all_faces", "list_known_faces",
        }
        self.assertTrue(expected.issubset(set(command_engine.get_available_actions())))

    def test_phase_two_actions_are_registered(self) -> None:
        from nora import command_engine
        import nora.commands.vision_commands  # noqa: F401

        expected = {"set_owner", "set_face_greeting", "set_face_persona", "guest_mode_status"}
        self.assertTrue(expected.issubset(set(command_engine.get_available_actions())))

    def test_set_owner_requires_confirmation(self) -> None:
        from nora import command_engine, security
        import nora.commands.vision_commands  # noqa: F401

        meta = command_engine.get_action_meta("set_owner")
        self.assertTrue(meta.requires_confirmation)
        self.assertTrue(security.needs_confirmation("set_owner"),
                        "set_owner missing from security.destructive_actions")

    def test_a_guest_cannot_promote_themselves(self) -> None:
        from nora import security
        self.assertIn("set_owner", security.guest_restricted_actions())

    def test_deletion_actions_require_confirmation(self) -> None:
        from nora import command_engine, security
        import nora.commands.vision_commands  # noqa: F401

        for action in ("forget_face", "forget_all_faces"):
            meta = command_engine.get_action_meta(action)
            self.assertEqual(meta.risk, "high", action)
            self.assertTrue(meta.requires_confirmation, action)
            self.assertTrue(security.needs_confirmation(action),
                            f"{action} missing from security.destructive_actions")

    def test_vision_actions_appear_in_the_llm_prompt(self) -> None:
        from nora import command_engine
        import nora.commands.vision_commands  # noqa: F401

        block = command_engine.get_action_signatures()
        self.assertIn("Vision & Camera:", block)
        self.assertIn("learn_face(name)", block)


# ── Phase 2: profiles, ownership, guest mode ─────────────────────────────────

def _with_config(**vision_overrides):
    """Patch the parsed config with vision overrides, deep-merged one level.

    Every consumer reads through nora.config.get_config(), which returns the
    module-level _config, so swapping that reaches security, presence, and
    faces at once.
    """
    from nora import config as config_module

    base = dict(config_module.get_config())
    vision = dict(base.get("vision") or {})
    for key, value in vision_overrides.items():
        if isinstance(value, dict) and isinstance(vision.get(key), dict):
            merged = dict(vision[key])
            merged.update(value)
            vision[key] = merged
        else:
            vision[key] = value
    base["vision"] = vision
    return mock.patch.object(config_module, "_config", base)


class _StoreCase(unittest.TestCase):
    """Redirects the face store to a temp file so ~/.nora is never touched."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = faces._PATH
        faces._PATH = Path(self._tmp.name) / "faces.json"

    def tearDown(self) -> None:
        faces._PATH = self._orig
        self._tmp.cleanup()


class ProfileTest(_StoreCase):
    def test_set_owner_is_exclusive(self) -> None:
        faces.enroll("Aadit", [[1.0, 0.0, 0.0]])
        faces.enroll("Sam", [[0.0, 1.0, 0.0]])

        faces.set_owner("aadit")
        self.assertEqual(faces.owner(), "aadit")

        faces.set_owner("sam")
        self.assertEqual(faces.owner(), "sam")
        self.assertFalse(faces.is_trusted("aadit"), "the previous owner was not demoted")
        trusted = [p["key"] for p in faces.list_people() if p["trusted"]]
        self.assertEqual(trusted, ["sam"])

    def test_set_owner_rejects_a_stranger(self) -> None:
        with self.assertRaises(KeyError):
            faces.set_owner("nobody")

    def test_owner_is_none_until_designated(self) -> None:
        faces.enroll("Aadit", [[1.0, 0.0, 0.0]])
        self.assertIsNone(faces.owner())
        self.assertFalse(faces.is_trusted("aadit"))

    def test_config_owner_outranks_the_stored_flag(self) -> None:
        faces.enroll("Aadit", [[1.0, 0.0, 0.0]])
        faces.enroll("Sam", [[0.0, 1.0, 0.0]])
        faces.set_owner("sam")
        with _with_config(face={"owner": "Aadit"}):
            self.assertEqual(faces.owner(), "aadit")
            self.assertTrue(faces.is_trusted("aadit"))
            self.assertFalse(faces.is_trusted("sam"))

    def test_greeting_defaults_then_customizes(self) -> None:
        faces.enroll("Aadit", [[1.0, 0.0, 0.0]])
        self.assertEqual(faces.greeting_for("aadit"), "Hey Aadit.")

        faces.set_profile("aadit", greeting="Welcome back, sir.")
        self.assertEqual(faces.greeting_for("aadit"), "Welcome back, sir.")

        faces.set_profile("aadit", greeting="   ")
        self.assertEqual(faces.greeting_for("aadit"), "Hey Aadit.",
                         "clearing the greeting did not fall back to the default")

    def test_persona_hints_are_validated_and_merged(self) -> None:
        faces.enroll("Aadit", [[1.0, 0.0, 0.0]])
        faces.set_profile("aadit", persona={"tone": "casual", "nonsense": "x"})
        self.assertEqual(faces.persona_for("aadit"), {"tone": "casual"})

        faces.set_profile("aadit", persona={"verbosity": "concise"})
        self.assertEqual(faces.persona_for("aadit"),
                         {"tone": "casual", "verbosity": "concise"})

    def test_profiles_survive_enrollment_and_deletion(self) -> None:
        faces.enroll("Aadit", [[1.0, 0.0, 0.0]])
        faces.set_profile("aadit", greeting="Welcome back.")
        faces.enroll("Aadit", [[0.9, 0.1, 0.0]])
        self.assertEqual(faces.greeting_for("aadit"), "Welcome back.")

        faces.forget("aadit")
        self.assertIsNone(faces.get_profile("aadit"))

    def test_set_profile_cannot_grant_ownership(self) -> None:
        faces.enroll("Aadit", [[1.0, 0.0, 0.0]])
        with self.assertRaises(TypeError):
            faces.set_profile("aadit", trusted=True)      # type: ignore[call-arg]


class GuestPresenceTest(_StoreCase):
    """Who counts as a guest — the decision guest mode rests on."""

    def setUp(self) -> None:
        super().setUp()
        faces.enroll("Aadit", [[1.0, 0.0, 0.0]])
        faces.enroll("Sam", [[0.0, 1.0, 0.0]])
        context.vision.present = []
        context.vision.unknown_count = 0

    def tearDown(self) -> None:
        context.vision.present = []
        context.vision.unknown_count = 0
        super().tearDown()

    def test_unknown_face_is_a_guest_even_with_no_owner_set(self) -> None:
        context.update_vision(present=[], unknown_count=1)
        with _with_config(enabled=True):
            self.assertTrue(presence.guest_present())

    def test_enrolled_person_is_not_a_guest_until_an_owner_exists(self) -> None:
        # Enrolling a face must not lock the user out of their own recall.
        context.update_vision(present=["aadit"], unknown_count=0)
        with _with_config(enabled=True):
            self.assertFalse(presence.guest_present())

    def test_non_owner_is_a_guest_once_an_owner_is_designated(self) -> None:
        faces.set_owner("aadit")
        context.update_vision(present=["sam"], unknown_count=0)
        with _with_config(enabled=True):
            self.assertTrue(presence.guest_present())

    def test_owner_alone_is_not_a_guest(self) -> None:
        faces.set_owner("aadit")
        context.update_vision(present=["aadit"], unknown_count=0)
        with _with_config(enabled=True):
            snap = presence.snapshot()
            self.assertTrue(snap["owner_present"])
            self.assertFalse(snap["guest_present"])

    def test_empty_frame_is_not_a_guest(self) -> None:
        faces.set_owner("aadit")
        context.update_vision(present=[], unknown_count=0)
        with _with_config(enabled=True):
            self.assertFalse(presence.guest_present())

    def test_vision_disabled_never_restricts(self) -> None:
        faces.set_owner("aadit")
        context.update_vision(present=["sam"], unknown_count=2)
        with _with_config(enabled=False):
            self.assertFalse(presence.guest_present())
            self.assertFalse(presence.guest_mode_active())

    def test_guest_mode_can_be_switched_off_independently(self) -> None:
        faces.set_owner("aadit")
        context.update_vision(present=["sam"], unknown_count=0)
        with _with_config(enabled=True, guest_mode={"enabled": False}):
            self.assertTrue(presence.guest_present(), "presence itself should still resolve")
            self.assertFalse(presence.guest_mode_active())


class GuestGuardTest(_StoreCase):
    """The action-path guard: restricts only, and only while a guest is there."""

    def setUp(self) -> None:
        super().setUp()
        faces.enroll("Aadit", [[1.0, 0.0, 0.0]])
        faces.enroll("Sam", [[0.0, 1.0, 0.0]])
        faces.set_owner("aadit")
        context.vision.present = []
        context.vision.unknown_count = 0

    def tearDown(self) -> None:
        context.vision.present = []
        context.vision.unknown_count = 0
        super().tearDown()

    def _guest(self):
        context.update_vision(present=["sam"], unknown_count=0)
        return _with_config(enabled=True)

    def _owner_only(self):
        context.update_vision(present=["aadit"], unknown_count=0)
        return _with_config(enabled=True)

    def test_private_action_is_withheld_while_a_guest_is_present(self) -> None:
        from nora import security
        with self._guest():
            self.assertTrue(security.guest_blocks("recall"))
            self.assertTrue(security.guest_blocks("check_email"))

    def test_nothing_is_withheld_from_the_owner_alone(self) -> None:
        from nora import security
        with self._owner_only():
            self.assertFalse(security.guest_blocks("recall"))

    def test_harmless_actions_still_run_with_a_guest_present(self) -> None:
        from nora import security
        with self._guest():
            self.assertFalse(security.guest_blocks("get_time"))
            self.assertFalse(security.guest_blocks("play_music"))

    def test_guest_mode_never_grants(self) -> None:
        """The asymmetry: a visible owner must not relax any existing guard."""
        from nora import security
        with self._owner_only():
            self.assertTrue(security.needs_confirmation("forget_all_faces"))
            self.assertEqual(security.is_blocked("forget_face"),
                             "forget_face" in security._blocked())

    def test_restricted_categories_extend_the_list(self) -> None:
        from nora import security
        import nora.commands.spotify  # noqa: F401  — registers play_music's category

        with self._guest():
            self.assertFalse(security.guest_blocks("play_music"))
        context.update_vision(present=["sam"], unknown_count=0)
        with _with_config(enabled=True,
                          guest_mode={"restricted_actions": [], "restricted_categories": ["music"]}):
            self.assertTrue(security.guest_blocks("play_music"))
            self.assertFalse(security.guest_blocks("recall"))

    def test_decline_message_can_name_the_guest(self) -> None:
        from nora import security
        with _with_config(enabled=True, guest_mode={"decline": "Not while {who} is here."}):
            context.update_vision(present=["sam"], unknown_count=1)
            message = security.guest_decline_message("recall")
            self.assertIn("Sam", message)
            self.assertNotIn("{who}", message)

    def test_execute_declines_without_calling_the_handler(self) -> None:
        import asyncio
        from nora import command_engine
        from nora.schemas import ActionStep, IntentResponse

        called = []
        with mock.patch.dict(command_engine._registry,
                             {"recall": lambda **kw: called.append(kw) or "secret"},
                             clear=False):
            with self._guest():
                intent = IntentResponse(intent="recall", steps=[ActionStep(action="recall")])
                results = asyncio.run(command_engine.execute(intent))

        self.assertEqual(called, [], "the handler ran despite guest mode")
        self.assertFalse(results[0].success)
        self.assertTrue(results[0].withheld, "a refusal was reported as a failure")
        self.assertNotIn("secret", results[0].message)

    def test_a_withheld_step_is_spoken_as_a_refusal_not_a_failure(self) -> None:
        from nora.pipeline import summarize_results
        from nora.schemas import StepResult

        summary = summarize_results([
            StepResult(action="recall", success=False, withheld=True,
                       message="I'll keep that private.")
        ])
        self.assertEqual(summary, "I'll keep that private.")
        self.assertNotIn("Failed", summary)

    def test_execute_runs_the_handler_for_the_owner(self) -> None:
        import asyncio
        from nora import command_engine
        from nora.schemas import ActionStep, IntentResponse

        called = []
        with mock.patch.dict(command_engine._registry,
                             {"recall": lambda **kw: called.append(kw) or "the answer"},
                             clear=False):
            with self._owner_only():
                intent = IntentResponse(intent="recall", steps=[ActionStep(action="recall")])
                results = asyncio.run(command_engine.execute(intent))

        self.assertEqual(len(called), 1)
        self.assertTrue(results[0].success)


class PresencePromptTest(_StoreCase):
    """The fusion point: what the LLM is told about who is in the room."""

    def setUp(self) -> None:
        super().setUp()
        faces.enroll("Aadit", [[1.0, 0.0, 0.0]])
        faces.enroll("Sam", [[0.0, 1.0, 0.0]])
        faces.set_owner("aadit")
        context.vision.present = []
        context.vision.unknown_count = 0

    def tearDown(self) -> None:
        context.vision.present = []
        context.vision.unknown_count = 0
        super().tearDown()

    def test_empty_when_nobody_is_visible(self) -> None:
        context.update_vision(present=[], unknown_count=0)
        with _with_config(enabled=True):
            self.assertEqual(presence.format_for_prompt(), "")

    def test_names_the_owner(self) -> None:
        context.update_vision(present=["aadit"], unknown_count=0)
        with _with_config(enabled=True):
            block = presence.format_for_prompt()
        self.assertIn("Aadit (owner)", block)
        self.assertNotIn("GUEST MODE", block)

    def test_guest_mode_instruction_is_included(self) -> None:
        context.update_vision(present=["aadit"], unknown_count=1)
        with _with_config(enabled=True):
            block = presence.format_for_prompt()
        self.assertIn("GUEST MODE", block)
        self.assertIn("not read private content aloud", block)

    def test_persona_hint_only_when_alone_in_frame(self) -> None:
        faces.set_profile("aadit", persona={"tone": "casual"})

        context.update_vision(present=["aadit"], unknown_count=0)
        with _with_config(enabled=True):
            self.assertIn("tone: casual", presence.format_for_prompt())

        context.update_vision(present=["aadit"], unknown_count=1)
        with _with_config(enabled=True):
            self.assertNotIn("tone: casual", presence.format_for_prompt())


if __name__ == "__main__":
    unittest.main()
