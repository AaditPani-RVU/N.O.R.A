"""Tests for document persistence (nora/claude_logs.py + ask_claude wiring).

Stdlib unittest only — run with:  python -m unittest tests.test_claude_logs -v

Regression cover for the 2026-08-20 phantom-log incident: `ask_claude` relayed
Claude's "I created a log at logs/foo.md" as fact for a file that was never
written, and a relative `create_file` had no predictable destination. Nothing
here touches the network or the real claude CLI.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from nora import claude_logs
from nora.commands import ask_claude as ac
from nora.commands import file_operations as fo


class _TempLogDir(unittest.TestCase):
    """Redirects LOG_DIR and the repo root at a scratch dir for each test."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        patches = [
            mock.patch.object(claude_logs, "_ROOT", root),
            mock.patch.object(claude_logs, "LOG_DIR", root / "claude_logs"),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.root = root
        self.addCleanup(self._tmp.cleanup)


class TestResolve(_TempLogDir):
    def test_document_dirs_collapse_into_claude_logs(self):
        # The exact path from the incident.
        p = claude_logs.resolve("logs/2026-08-20_vision-guest-mode.md")
        self.assertEqual(p.parent, claude_logs.LOG_DIR)
        self.assertEqual(p.name, "2026-08-20_vision-guest-mode.md")

    def test_bare_document_name_goes_to_claude_logs(self):
        self.assertEqual(claude_logs.resolve("notes.md").parent, claude_logs.LOG_DIR)

    def test_source_paths_anchor_to_repo_root_not_cwd(self):
        self.assertEqual(claude_logs.resolve("nora/commands/x.py"), self.root / "nora/commands/x.py")

    def test_absolute_paths_pass_through(self):
        self.assertEqual(claude_logs.resolve("/etc/hosts"), Path("/etc/hosts"))


class TestWrite(_TempLogDir):
    def test_write_creates_dated_file_with_header(self):
        path = claude_logs.write("vision guest mode", "body text", source="test", question="q")
        self.assertTrue(path.exists())
        self.assertRegex(path.name, r"^\d{4}-\d{2}-\d{2}_[a-z0-9-]+\.md$")
        text = path.read_text(encoding="utf-8")
        self.assertIn("body text", text)
        self.assertIn("**Source:** test", text)

    def test_repeat_titles_do_not_overwrite(self):
        a = claude_logs.write("same title", "first")
        b = claude_logs.write("same title", "second")
        self.assertNotEqual(a, b)
        self.assertEqual(a.read_text(encoding="utf-8").count("first"), 1)

    def test_conversational_preamble_is_trimmed_and_h1_becomes_the_title(self):
        body = "Now I have a complete picture. Here is the log.\n\n# Vision Guest-Mode\n\nDetail."
        path = claude_logs.write("create a log for this specific change", body)
        text = path.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("# Vision Guest-Mode\n"))
        self.assertNotIn("complete picture", text)
        self.assertEqual(text.count("# Vision Guest-Mode"), 1)

    def test_headingless_note_keeps_its_first_line(self):
        path = claude_logs.write("quick note", "Remember to re-run the vision calibration.")
        self.assertIn("Remember to re-run the vision calibration.",
                      path.read_text(encoding="utf-8"))

    def test_gist_skips_structure_and_metadata(self):
        body = "# Title\n\n**Date:** 2026-08-20\n\nThe guard blanks frames when a guest is present."
        self.assertEqual(claude_logs.gist(body),
                         "The guard blanks frames when a guest is present.")

    def test_recent_lists_newest_first(self):
        claude_logs.write("older", "a")
        newest = claude_logs.write("newer", "b")
        self.assertEqual(claude_logs.recent(5)[0], newest)


class TestClaimDetection(unittest.TestCase):
    def test_finds_path_claimed_in_prose(self):
        said = "I put together a log file at `logs/2026-08-20_vision-guest-mode.md` summarizing this."
        self.assertEqual(claude_logs.find_claimed_paths(said),
                         ["logs/2026-08-20_vision-guest-mode.md"])

    def test_plain_prose_claims_nothing(self):
        self.assertEqual(claude_logs.find_claimed_paths("It is about 4pm. Nothing to save."), [])


class TestWantsDocument(unittest.TestCase):
    def test_document_requests(self):
        for q in ["create a log for this change", "write up what we did",
                  "summarise the vision changes", "make a report on the guard"]:
            self.assertTrue(claude_logs.wants_document(q), q)

    def test_ordinary_questions(self):
        for q in ["what time is it", "who won the match", "explain TCP slow start"]:
            self.assertFalse(claude_logs.wants_document(q), q)


class TestAskClaude(_TempLogDir):
    def test_document_request_is_saved_and_path_is_real(self):
        with mock.patch.object(ac, "_call_claude", return_value="## Change log\n\nThe guest-mode guard now blanks frames when an unknown face is present."), \
             mock.patch.object(ac, "_build_context_block", return_value=""):
            reply = ac.ask_claude("create a log for this specific change")

        saved = claude_logs.recent(5)
        self.assertEqual(len(saved), 1)
        # Whatever NORA says out loud must name a file that exists.
        self.assertIn(saved[0].name, reply)
        self.assertTrue((claude_logs.LOG_DIR / saved[0].name).exists())

    def test_no_write_tools_are_offered_to_the_cli(self):
        captured = {}

        class _Result:
            returncode = 0
            stdout = "ok"
            stderr = ""

        def _run(cmd, **kw):
            captured["cmd"] = cmd
            return _Result()

        with mock.patch.object(ac.shutil, "which", return_value="/usr/bin/claude"), \
             mock.patch.object(ac.subprocess, "run", _run):
            ac._call_claude("hi")

        cmd = captured["cmd"]
        self.assertIn("--disallowedTools", cmd)
        denied = cmd[cmd.index("--disallowedTools") + 1]
        for tool in ("Write", "Edit", "Bash"):
            self.assertIn(tool, denied)
        self.assertNotIn("--allowedTools", cmd)

    def test_phantom_path_in_a_chat_reply_is_backed_by_a_real_file(self):
        phantom = "I put together a log file at logs/2026-08-20_vision-guest-mode.md for you."
        with mock.patch.object(ac, "_call_claude", return_value=phantom), \
             mock.patch.object(ac, "_build_context_block", return_value=""):
            reply = ac.ask_claude("what did you change in the vision module")

        saved = claude_logs.recent(5)
        self.assertEqual(len(saved), 1)
        self.assertNotIn("logs/2026-08-20_vision-guest-mode.md", reply)
        self.assertIn(saved[0].name, reply)

    def test_cli_errors_are_not_saved_as_documents(self):
        with mock.patch.object(ac, "_call_claude", return_value="Claude took too long to respond."), \
             mock.patch.object(ac, "_build_context_block", return_value=""):
            reply = ac.ask_claude("write a log about the guard")
        self.assertEqual(reply, "Claude took too long to respond.")
        self.assertEqual(claude_logs.recent(5), [])


class TestCreateFile(_TempLogDir):
    def test_relative_log_path_lands_in_claude_logs(self):
        msg = fo.create_file("logs/2026-08-20_vision-guest-mode.md", "content")
        target = claude_logs.LOG_DIR / "2026-08-20_vision-guest-mode.md"
        self.assertTrue(target.exists())
        # The reported path is absolute, so it can be acted on verbatim.
        self.assertIn(str(target), msg)


class TestNeurosymGating(unittest.TestCase):
    def test_confirmable_violation_does_not_hard_block(self):
        """create_file is in DESTRUCTIVE_ACTIONS; it must confirm, not block."""
        from nora import neurosym_guard

        class _Result:
            ok = False           # neurosym: "ok" just means zero violations
            hard_denied = False  # ...and only this means "deny"
            violations = [{"rule_id": "policy.destructive_needs_confirmation",
                           "severity": "high"}]

        class _Guard:
            def apply_json(self, plan):
                return _Result()

        class _Intent:
            def model_dump(self):
                return {"steps": [{"action": "create_file"}], "requires_confirmation": False}

        with mock.patch.object(neurosym_guard, "_action", return_value=_Guard()):
            is_safe, needs_confirm, _ = neurosym_guard.check_intent(_Intent())

        self.assertTrue(is_safe)
        self.assertTrue(needs_confirm)

    def test_critical_violation_still_blocks(self):
        from nora import neurosym_guard

        class _Result:
            ok = False
            hard_denied = True
            violations = [{"rule_id": "policy.path_outside_sandbox", "severity": "critical"}]

        class _Guard:
            def apply_json(self, plan):
                return _Result()

        class _Intent:
            def model_dump(self):
                return {"steps": [{"action": "delete_file"}]}

        with mock.patch.object(neurosym_guard, "_action", return_value=_Guard()):
            is_safe, _, _ = neurosym_guard.check_intent(_Intent())

        self.assertFalse(is_safe)


if __name__ == "__main__":
    unittest.main()
