"""Tests for the tiered web research path (nora/commands/web_search.py).

Stdlib unittest only — run with:  python -m unittest tests.test_web_search -v

No network: every tier is stubbed. What's under test is the fall-through
order and the parsing, not the models.
"""
from __future__ import annotations

import unittest
from unittest import mock

from nora.commands import web_search as ws


class TestFreshness(unittest.TestCase):
    def test_time_sensitive_queries_are_detected(self):
        for q in ["what's going on in Iran right now", "current fuel prices",
                  "latest iPhone", "news today", "recent layoffs"]:
            self.assertTrue(ws._is_fresh_query(q), q)

    def test_timeless_queries_are_not(self):
        for q in ["who wrote Dune", "how does TCP slow start work"]:
            self.assertFalse(ws._is_fresh_query(q), q)

    def test_brave_only_sends_freshness_for_fresh_queries(self):
        captured = []

        class _Resp:
            status_code = 200
            def raise_for_status(self): pass
            def json(self): return {"web": {"results": []}}

        def fake_get(url, **kw):
            captured.append(kw["params"])
            return _Resp()

        with mock.patch.dict("os.environ", {"BRAVE_API_KEY": "k"}), \
             mock.patch.object(ws.requests, "get", fake_get):
            ws._brave_search("petrol price today")
            ws._brave_search("who wrote Dune")

        self.assertIn("freshness", captured[0])
        self.assertNotIn("freshness", captured[1])


class TestSnippetParsing(unittest.TestCase):
    def test_brave_survives_empty_extra_snippets(self):
        """`extra_snippets: []` used to raise IndexError and lose every result."""
        payload = {"web": {"results": [
            {"title": "T", "description": "", "extra_snippets": [], "url": "u"},
            {"title": "T2", "description": "<b>real</b> answer", "url": "u2", "age": "1 day ago"},
        ]}}

        class _Resp:
            def raise_for_status(self): pass
            def json(self): return payload

        with mock.patch.dict("os.environ", {"BRAVE_API_KEY": "k"}), \
             mock.patch.object(ws.requests, "get", lambda *a, **k: _Resp()):
            out = ws._brave_search("q")

        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["text"], "real answer")

    def test_format_snippets_includes_title_and_age(self):
        text = ws._format_snippets([{"title": "Rates", "age": "1 day ago", "text": "Rs 111"}])
        self.assertIn("Rates", text)
        self.assertIn("1 day ago", text)
        self.assertIn("Rs 111", text)


class TestSourceExtraction(unittest.TestCase):
    def test_urls_are_pulled_from_executed_tools(self):
        extras = {"executed_tools": [
            {"type": "search", "output": "Title: A\nURL: https://a.example\nContent: x\n"
                                         "Title: B\nURL: https://b.example\nContent: y"},
            {"type": "search", "output": "Title: A\nURL: https://a.example\nContent: dupe"},
        ]}
        self.assertEqual(ws._sources_from_executed_tools(extras),
                         ["https://a.example", "https://b.example"])

    def test_no_tools_means_no_sources(self):
        self.assertEqual(ws._sources_from_executed_tools({}), [])


class TestPrompts(unittest.TestCase):
    """Every tier is read aloud, so no tier may be told it can emit a URL."""

    def _prompt_from(self, call) -> str:
        captured = {}

        def fake_complete(role, messages, **kw):
            captured["p"] = messages[0]["content"]
            if "extras_out" in kw:
                kw["extras_out"]["executed_tools"] = [{"output": "URL: https://a.example"}]
            return "answer", "model"

        with mock.patch("nora.model_router.complete", fake_complete):
            call()
        return captured["p"]

    def test_every_tier_forbids_spoken_urls(self):
        prompts = [
            self._prompt_from(lambda: ws._live_search("q")),
            self._prompt_from(lambda: ws._summarise("q", [{"title": "", "age": "", "text": "s"}])),
            self._prompt_from(lambda: ws._summarise_no_context("q")),
        ]
        for p in prompts:
            self.assertIn("url", p.lower(), p)
            self.assertIn("domain", p.lower(), p)

    def test_searching_tiers_ask_for_a_spoken_source_name(self):
        for call in (lambda: ws._live_search("q"),
                     lambda: ws._summarise("q", [{"title": "", "age": "", "text": "s"}])):
            self.assertIn("according to the guardian", self._prompt_from(call).lower())


class TestTiering(unittest.TestCase):
    def test_live_answer_short_circuits_the_snippet_path(self):
        with mock.patch.object(ws, "_live_search", return_value="Live answer."), \
             mock.patch.object(ws, "_fetch_snippets") as fetch:
            self.assertEqual(ws.tell_me_about("what's happening now"), "Live answer.")
        fetch.assert_not_called()

    def test_falls_through_to_snippets_when_live_is_silent(self):
        with mock.patch.object(ws, "_live_search", return_value=""), \
             mock.patch.object(ws, "_fetch_snippets", return_value=[{"title": "", "age": "", "text": "s"}]), \
             mock.patch.object(ws, "_summarise", return_value="Snippet answer.") as summarise:
            self.assertEqual(ws.tell_me_about("q"), "Snippet answer.")
        summarise.assert_called_once()

    def test_falls_through_to_model_only_when_search_is_dead(self):
        with mock.patch.object(ws, "_live_search", return_value=""), \
             mock.patch.object(ws, "_fetch_snippets", return_value=[]), \
             mock.patch.object(ws, "_summarise_no_context", return_value="No web."):
            self.assertEqual(ws.tell_me_about("q"), "No web.")

    def test_live_answer_without_a_search_is_discarded(self):
        """compound answering from its own weights is tier 3, not tier 1."""
        with mock.patch("nora.model_router.complete", return_value=("stale guess", "c")):
            self.assertEqual(ws._live_search("q"), "")

    def test_live_answer_with_sources_is_kept_and_speech_shaped(self):
        def fake_complete(role, messages, **kw):
            kw["extras_out"]["executed_tools"] = [{"output": "URL: https://a.example"}]
            return "**Bold** answer. See https://a.example", "compound"

        with mock.patch("nora.model_router.complete", fake_complete):
            answer = ws._live_search("q")
        self.assertIn("Bold answer", answer)
        self.assertNotIn("*", answer)
        self.assertNotIn("http", answer)

    def test_summarise_reads_the_top_snippet_when_every_model_fails(self):
        from nora.model_router import AllCandidatesFailed
        with mock.patch("nora.model_router.complete", side_effect=AllCandidatesFailed("down")):
            out = ws._summarise("q", [{"title": "", "age": "", "text": "Petrol is Rs 111."}])
        self.assertIn("Petrol is Rs 111.", out)


if __name__ == "__main__":
    unittest.main()
