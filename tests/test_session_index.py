"""Tests for nora.session_index — exact-match recall over the transcript.

The case that justifies the module is the proper noun: "what did I say about
FABSeg". An embedding index has nothing useful to say about a made-up token,
because it has no neighbours; a keyword index finds the one utterance that
contains it. So that is the first test, and the hybrid ordering test exists to
make sure the exact hit stays in front of the plausible-looking paraphrase.

The second thing under test is that none of this can take a turn down. It is
hooked into `dialogue.record_user`, which runs on literally every utterance —
a bad query string or a missing FTS5 build has to degrade to "no results", not
raise.

Stdlib unittest only — run with:  python -m unittest tests.test_session_index -v
"""
from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from nora import session_index


class SessionIndexTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        session_index._use_path_for_tests(Path(self.tmp.name) / "test.db")
        self.addCleanup(session_index.close)


class RecordAndSearchTest(SessionIndexTest):
    def test_finds_a_proper_noun(self) -> None:
        """The case vector search is worst at."""
        session_index.record("the FABSeg paper needs a new ablation table")
        session_index.record("remind me to buy milk")
        session_index.record("what's the weather like")

        hits = session_index.search("FABSeg")
        self.assertEqual(len(hits), 1)
        self.assertIn("FABSeg", hits[0].text)

    def test_partial_phrase_matches(self) -> None:
        session_index.record("the training loss is diverging on the second epoch")
        hits = session_index.search("what did I say about the training loss")
        self.assertTrue(hits)
        self.assertIn("diverging", hits[0].text)

    def test_roles_are_kept(self) -> None:
        session_index.record("play some music", role="user", source="command")
        session_index.record("Playing your focus playlist.", role="nora", source="command")

        hits = session_index.search("playlist")
        self.assertEqual(hits[0].role, "nora")

    def test_empty_text_is_ignored(self) -> None:
        session_index.record("")
        session_index.record("   ")
        self.assertEqual(session_index.count(), 0)

    def test_since_filter(self) -> None:
        session_index.record("an old thought about gradients")
        cutoff = time.time() + 1
        self.assertEqual(session_index.search("gradients", since=cutoff), [])
        self.assertTrue(session_index.search("gradients"))

    def test_count(self) -> None:
        for i in range(5):
            session_index.record(f"utterance number {i}")
        self.assertEqual(session_index.count(), 5)


class QuerySafetyTest(SessionIndexTest):
    """FTS5 syntax in a spoken question must not produce an error."""

    def setUp(self) -> None:
        super().setUp()
        session_index.record("I was reading about C++ templates and the NEAR operator")

    def test_fts_operators_in_speech_are_neutralised(self) -> None:
        for query in ('C++', 'NEAR', 'templates AND operator', '"unclosed quote',
                      'what about (this)', 'a - b', 'x: y', '*'):
            with self.subTest(query=query):
                # The contract is "never raises", not "always finds something".
                session_index.search(query)

    def test_empty_query_returns_nothing(self) -> None:
        self.assertEqual(session_index.search(""), [])
        self.assertEqual(session_index.search("   "), [])
        self.assertEqual(session_index.search("*()"), [])

    def test_any_word_matching(self) -> None:
        """A spoken question rarely repeats the indexed phrasing exactly."""
        hits = session_index.search("tell me about those templates I mentioned")
        self.assertTrue(hits, "requiring every term found nothing")


class HybridTest(SessionIndexTest):
    def test_exact_hits_lead(self) -> None:
        session_index.record("the FABSeg ablation is missing a baseline")

        fake_semantic = [{"text": "a vaguely similar thought about segmentation",
                          "metadata": {"source": "ambient", "timestamp": time.time()}}]
        with mock.patch("nora.cognitive_memory.semantic_search", return_value=fake_semantic):
            hits = session_index.search_hybrid("FABSeg", limit=5)

        self.assertGreaterEqual(len(hits), 2)
        self.assertIn("FABSeg", hits[0].text)
        self.assertEqual(hits[0].source, "command")

    def test_semantic_fills_the_gap(self) -> None:
        fake_semantic = [{"text": "something only the embedding index knows",
                          "metadata": {"source": "ambient", "timestamp": time.time()}}]
        with mock.patch("nora.cognitive_memory.semantic_search", return_value=fake_semantic):
            hits = session_index.search_hybrid("nothing matches this keyword", limit=5)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].source, "ambient")

    def test_duplicates_are_collapsed(self) -> None:
        session_index.record("the exact same sentence")
        fake_semantic = [{"text": "the exact same sentence", "metadata": {}}]
        with mock.patch("nora.cognitive_memory.semantic_search", return_value=fake_semantic):
            hits = session_index.search_hybrid("sentence", limit=5)
        self.assertEqual(len(hits), 1)

    def test_semantic_failure_is_survivable(self) -> None:
        """Chroma being cold or absent must not break keyword recall."""
        session_index.record("a keyword hit")
        with mock.patch("nora.cognitive_memory.semantic_search",
                        side_effect=RuntimeError("chroma is down")):
            hits = session_index.search_hybrid("keyword", limit=5)
        self.assertEqual(len(hits), 1)


class DegradationTest(SessionIndexTest):
    def test_no_fts5_degrades_quietly(self) -> None:
        """A sqlite build without FTS5 loses recall, not the turn."""
        import sqlite3

        session_index.close()
        session_index._available = None
        with mock.patch.object(session_index.sqlite3, "connect",
                               side_effect=sqlite3.OperationalError("no such module: fts5")):
            session_index.record("this goes nowhere")
            self.assertEqual(session_index.search("anything"), [])
            self.assertEqual(session_index.count(), 0)
        session_index._available = None

    def test_dialogue_hook_never_raises(self) -> None:
        from nora import dialogue

        with mock.patch.object(session_index, "record",
                               side_effect=RuntimeError("disk full")):
            dialogue._index("some text", "user", "chat")  # must not raise


class AgeLabelTest(unittest.TestCase):
    def test_spoken_ages(self) -> None:
        now = time.time()
        cases = [
            (now - 60, "earlier today"),
            (now - 90000, "yesterday"),
            (now - 1209600, "2 weeks ago"),
        ]
        for ts, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(session_index.Entry("t", "user", "s", ts).age(), expected)


if __name__ == "__main__":
    unittest.main()
