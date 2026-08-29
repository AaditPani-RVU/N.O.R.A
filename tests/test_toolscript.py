"""Tests for nora.toolscript — many actions, one inference.

Two things are under test and they pull in opposite directions. The script has
to be *powerful* enough to be worth the round-trip it saves: loops, branching,
feeding one command's output into the next. And it has to be *contained*: no
imports, no filesystem, no route around the security policy that a plain step
would have hit.

The containment tests matter more than the capability ones. A tool script is
`exec` on model output, and the only thing that makes that acceptable is that
the namespace genuinely has nothing dangerous in it — so the escape attempts
are written as tests rather than trusted to the docstring.

Stdlib unittest only — run with:  python -m unittest tests.test_toolscript -v
"""
from __future__ import annotations

import asyncio
import unittest
from unittest import mock

from nora import command_engine, toolscript


def _run(source: str, **kw) -> toolscript.ScriptResult:
    return asyncio.run(toolscript.run(source, **kw))


class _Registry:
    """Swaps the command table for a small fake one for the duration of a test."""

    def __init__(self, **handlers):
        self.handlers = handlers

    def __enter__(self):
        self._reg = dict(command_engine._registry)
        self._meta = dict(command_engine._meta)
        command_engine._registry.clear()
        command_engine._meta.clear()
        for name, fn in self.handlers.items():
            command_engine._registry[name] = fn
            command_engine._meta[name] = command_engine.CommandMeta(sig=f"{name}()")
        return self

    def __exit__(self, *a):
        command_engine._registry.clear()
        command_engine._registry.update(self._reg)
        command_engine._meta.clear()
        command_engine._meta.update(self._meta)


class CapabilityTest(unittest.TestCase):
    """What the script can do that a flat ActionStep list could not."""

    def setUp(self) -> None:
        self.calls: list[str] = []
        self.reg = _Registry(
            pause_music=lambda: self.calls.append("pause") or "Paused.",
            set_brightness=lambda level: self.calls.append(f"bright:{level}") or f"Set to {level}.",
            get_calendar=lambda: "Two meetings.",
        )
        self.reg.__enter__()
        self.addCleanup(self.reg.__exit__)

    def test_three_actions_one_script(self) -> None:
        res = _run(
            "pause_music()\n"
            "set_brightness(40)\n"
            "say('Paused and dimmed.', get_calendar())\n"
        )
        self.assertTrue(res.ok, res.error)
        self.assertEqual(self.calls, ["pause", "bright:40"])
        self.assertIn("Paused and dimmed.", res.summary())
        self.assertIn("Two meetings.", res.summary())
        self.assertEqual(res.actions, ["pause_music", "set_brightness", "get_calendar"])

    def test_output_feeds_the_next_call(self) -> None:
        res = _run("agenda = get_calendar()\nsay('Today:', agenda)")
        self.assertTrue(res.ok, res.error)
        self.assertEqual(res.summary(), "Today: Two meetings.")

    def test_loops_and_conditionals(self) -> None:
        res = _run(
            "for level in [10, 20, 30]:\n"
            "    if level > 15:\n"
            "        set_brightness(level)\n"
            "say('Done.')\n"
        )
        self.assertTrue(res.ok, res.error)
        self.assertEqual(self.calls, ["bright:20", "bright:30"])

    def test_async_handlers_are_awaited(self) -> None:
        async def slow_action() -> str:
            await asyncio.sleep(0.01)
            return "async done"

        with _Registry(slow_action=slow_action):
            res = _run("say(slow_action())")
        self.assertTrue(res.ok, res.error)
        self.assertEqual(res.summary(), "async done")

    def test_no_say_still_succeeds(self) -> None:
        res = _run("pause_music()")
        self.assertTrue(res.ok, res.error)
        self.assertEqual(res.summary(), "Done.")


class ContainmentTest(unittest.TestCase):
    """The namespace has to be genuinely empty of anything dangerous."""

    def setUp(self) -> None:
        self.reg = _Registry(pause_music=lambda: "Paused.")
        self.reg.__enter__()
        self.addCleanup(self.reg.__exit__)

    def test_import_is_rejected(self) -> None:
        for src in ("import os", "from os import system", "import os.path as p"):
            with self.subTest(src=src):
                ok, reason = toolscript.validate(src)
                self.assertFalse(ok)
                self.assertIn("can't use", reason)

    def test_dunder_escape_is_rejected(self) -> None:
        ok, reason = toolscript.validate("().__class__.__bases__[0].__subclasses__()")
        self.assertFalse(ok)
        self.assertIn("dunder", reason)

    def test_builtins_are_not_reachable(self) -> None:
        for src in ("open('/etc/passwd')", "eval('1+1')", "exec('x=1')",
                    "__import__('os')", "getattr(str, 'upper')"):
            with self.subTest(src=src):
                res = _run(src)
                self.assertFalse(res.ok, f"{src} should not have run")

    def test_unknown_action_is_refused_before_running(self) -> None:
        res = _run("pause_music()\nnuke_everything()")
        self.assertFalse(res.ok)
        self.assertIn("no action called nuke_everything", res.error)
        # Nothing ran — the whole script is rejected up front.
        self.assertEqual(res.actions, [])

    def test_blocked_action_is_refused(self) -> None:
        with mock.patch("nora.security.is_blocked", lambda a: a == "pause_music"):
            res = _run("pause_music()")
        self.assertFalse(res.ok)
        self.assertIn("blocked", res.error)

    def test_guest_mode_stops_a_call_mid_script(self) -> None:
        """Guest mode can flip after validation, so it is re-checked per call."""
        with mock.patch("nora.security.guest_blocks", lambda a: True), \
             mock.patch("nora.security.guest_decline_message", lambda a: "Not with company here."):
            res = _run("say('before')\npause_music()\nsay('after')")
        self.assertFalse(res.ok)
        self.assertIn("Not with company here.", res.error)
        # What was said before the refusal is kept; what came after never ran.
        self.assertEqual(res.spoken, ["before"])

    def test_syntax_error_is_spoken_not_raised(self) -> None:
        res = _run("say('unclosed")
        self.assertFalse(res.ok)
        self.assertIn("doesn't parse", res.error)

    def test_failing_command_halts_the_script(self) -> None:
        def boom() -> str:
            raise RuntimeError("device busy")

        with _Registry(boom=boom, pause_music=lambda: "Paused."):
            res = _run("boom()\npause_music()")
        self.assertFalse(res.ok)
        self.assertIn("device busy", res.error)

    def test_runaway_script_is_abandoned(self) -> None:
        res = _run("while True:\n    pass\n", timeout=0.5)
        self.assertFalse(res.ok)
        self.assertIn("too long", res.error)


class ConfirmationGateTest(unittest.TestCase):
    """A script must not be a way around the confirmation prompt."""

    def test_inner_actions_reach_check_steps(self) -> None:
        from nora.schemas import ActionStep
        from nora import security

        step = ActionStep(action="run_script",
                          parameters={"code": "delete_file('/tmp/x')\nsay('gone')"})
        with mock.patch.object(security, "needs_confirmation",
                               lambda a: a == "delete_file"):
            blocked, confirm = security.check_steps([step])
        self.assertTrue(confirm, "delete_file inside a script escaped the gate")
        self.assertFalse(blocked)

    def test_inner_blocked_action_is_caught(self) -> None:
        from nora.schemas import ActionStep
        from nora import security

        step = ActionStep(action="run_script", parameters={"code": "shutdown()"})
        with mock.patch.object(security, "is_blocked", lambda a: a == "shutdown"):
            blocked, _ = security.check_steps([step])
        self.assertTrue(blocked)

    def test_unparseable_script_is_treated_as_needing_confirmation(self) -> None:
        from nora.schemas import ActionStep
        from nora import security

        step = ActionStep(action="run_script", parameters={"code": "say('unclosed"})
        names = security._script_actions(step)
        self.assertEqual(names, ["run_script"])

    def test_requires_confirmation_lists_risky_actions(self) -> None:
        with _Registry(delete_file=lambda p: "gone", play=lambda: "ok"):
            command_engine._meta["delete_file"] = command_engine.CommandMeta(
                sig="delete_file(path)", risk="high", requires_confirmation=True)
            flagged = toolscript.requires_confirmation("play()\ndelete_file('/tmp/x')")
        self.assertEqual(flagged, ["delete_file"])


if __name__ == "__main__":
    unittest.main()
