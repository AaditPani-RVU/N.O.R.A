"""Tests for nora.skills — procedural memory as Markdown.

The load path is the thing worth covering. A malformed skill must be skipped
rather than crash discovery, because these files are hand-written and, once
`save_skill` exists, model-written too — a bad one has to cost that skill and
nothing else.

The other property under test is progressive disclosure: `catalogue()` is what
reaches the intent-parser prompt on every single utterance, so it has to carry
descriptions and never bodies. Getting that wrong doesn't fail loudly, it just
quietly makes every turn slower and more expensive, which is exactly the kind
of regression a test should catch.

Stdlib unittest only — run with:  python -m unittest tests.test_skills -v
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from nora import skills


class SkillsTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        skills.SKILL_DIRS = (self.dir,)
        skills._cache = None
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        skills.SKILL_DIRS = None
        skills._cache = None

    def make(self, slug: str, text: str) -> Path:
        (self.dir / slug).mkdir(parents=True, exist_ok=True)
        path = self.dir / slug / "SKILL.md"
        path.write_text(text, encoding="utf-8")
        return path


class DiscoveryTest(SkillsTestCase):
    def test_loads_frontmatter_and_body(self) -> None:
        self.make("wind-down", (
            "---\nname: wind-down\ndescription: Evening shutdown routine.\n---\n\n"
            "1. Pause music.\n2. Dim the screen.\n"
        ))
        found = skills.discover(force=True)
        self.assertIn("wind-down", found)
        skill = found["wind-down"]
        self.assertEqual(skill.description, "Evening shutdown routine.")
        self.assertIn("Pause music", skill.body)
        self.assertNotIn("---", skill.body)

    def test_quoted_description_is_unwrapped(self) -> None:
        """The bundled Claude Code skills quote their descriptions."""
        self.make("q", '---\nname: q\ndescription: "Quoted, with a comma."\n---\n\nDo it.\n')
        self.assertEqual(skills.discover(force=True)["q"].description, "Quoted, with a comma.")

    def test_extra_frontmatter_keys_are_ignored(self) -> None:
        self.make("v", "---\nname: v\nversion: 1.0.0\ndescription: Has a version.\n---\n\nBody.\n")
        self.assertIn("v", skills.discover(force=True))

    def test_name_defaults_to_directory(self) -> None:
        self.make("from-dir", "---\ndescription: No name key.\n---\n\nBody.\n")
        self.assertIn("from-dir", skills.discover(force=True))

    def test_bad_skills_are_skipped_not_fatal(self) -> None:
        self.make("good", "---\nname: good\ndescription: Fine.\n---\n\nBody.\n")
        self.make("no-desc", "---\nname: no-desc\n---\n\nBody.\n")
        self.make("no-body", "---\nname: no-body\ndescription: Empty.\n---\n\n")
        self.make("no-frontmatter", "Just some prose with no header at all.\n")

        found = skills.discover(force=True)
        self.assertEqual(set(found), {"good"})

    def test_first_directory_wins(self) -> None:
        second = Path(self.tmp.name) / "_second"
        (second / "dup").mkdir(parents=True)
        (second / "dup" / "SKILL.md").write_text(
            "---\nname: dup\ndescription: Second.\n---\n\nSecond body.\n", encoding="utf-8")
        self.make("dup", "---\nname: dup\ndescription: First.\n---\n\nFirst body.\n")

        skills.SKILL_DIRS = (self.dir, second)
        self.assertEqual(skills.discover(force=True)["dup"].description, "First.")


class LookupTest(SkillsTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.make("wind-down", "---\nname: wind-down\ndescription: Evening routine.\n---\n\nBody.\n")
        skills.discover(force=True)

    def test_exact_name(self) -> None:
        self.assertIsNotNone(skills.get("wind-down"))

    def test_spoken_form_matches(self) -> None:
        """Whisper transcribes 'wind-down' as 'wind down', every time."""
        for spoken in ("wind down", "Wind Down", "wind  down"):
            with self.subTest(spoken=spoken):
                skill = skills.get(spoken)
                self.assertIsNotNone(skill, f"{spoken!r} did not match")
                self.assertEqual(skill.name, "wind-down")

    def test_partial_match(self) -> None:
        self.assertIsNotNone(skills.get("the wind down thing"))

    def test_no_match_returns_none(self) -> None:
        self.assertIsNone(skills.get("order a pizza"))


class CatalogueTest(SkillsTestCase):
    def test_carries_descriptions_never_bodies(self) -> None:
        self.make("wind-down", (
            "---\nname: wind-down\ndescription: Evening shutdown routine.\n---\n\n"
            "SECRET_BODY_MARKER: pause music then dim the screen.\n"
        ))
        skills.discover(force=True)
        cat = skills.catalogue()

        self.assertIn("wind-down", cat)
        self.assertIn("Evening shutdown routine.", cat)
        # The whole point of progressive disclosure: bodies stay on disk.
        self.assertNotIn("SECRET_BODY_MARKER", cat)

    def test_empty_when_no_skills(self) -> None:
        self.assertEqual(skills.catalogue(), "")

    def test_stays_small_with_many_skills(self) -> None:
        """Fifty skills must not cost fifty procedures of prompt."""
        for i in range(50):
            self.make(f"skill-{i}", (
                f"---\nname: skill-{i}\ndescription: Does thing {i}.\n---\n\n"
                + ("A long procedure body. " * 200)
            ))
        skills.discover(force=True)
        self.assertLess(len(skills.catalogue()), 3000)


class AuthoringTest(SkillsTestCase):
    def test_write_then_read_back(self) -> None:
        skill = skills.write("Morning Brief", "Reads the day out.", "1. Check calendar.")
        self.assertIsNotNone(skill)
        self.assertEqual(skill.name, "morning-brief")
        self.assertTrue(skill.path.exists())

        # Available immediately, without a restart.
        again = skills.get("morning brief")
        self.assertIsNotNone(again)
        self.assertIn("Check calendar", again.body)

    def test_written_skill_round_trips_through_the_parser(self) -> None:
        """What save_skill writes, discover must be able to read."""
        skills.write("test-skill", "A description: with a colon.", "Body here.")
        found = skills.discover(force=True)["test-skill"]
        self.assertEqual(found.description, "A description: with a colon.")
        self.assertEqual(found.body, "Body here.")

    def test_unusable_name_is_refused(self) -> None:
        self.assertIsNone(skills.write("!!!", "Nope.", "Body."))


if __name__ == "__main__":
    unittest.main()
