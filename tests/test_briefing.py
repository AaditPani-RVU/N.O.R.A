"""Briefing feed layer — parsing, routing and merge.

All offline. The feeds themselves are somebody else's uptime; what has to
keep working is what we do with their bytes.
"""
from __future__ import annotations

import time
import unittest
from unittest import mock

from nora import briefing


RSS = """<?xml version="1.0"?>
<rss version="2.0" xmlns:media="http://search.yahoo.com/mrss/">
  <channel>
    <item>
      <title>Chevy&apos;s New V8 Ditches The Dipstick</title>
      <link>https://example.com/v8</link>
      <pubDate>Wed, 17 Sep 2026 10:00:00 GMT</pubDate>
      <description>&lt;p&gt;A digital oil gauge.&lt;/p&gt;</description>
      <media:thumbnail url="https://cdn.example.com/v8.jpg"/>
    </item>
    <item>
      <title>No Picture Here</title>
      <link>https://example.com/plain</link>
      <pubDate>Wed, 17 Sep 2026 09:00:00 GMT</pubDate>
    </item>
  </channel>
</rss>"""

ATOM = """<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <title>An Atom Story</title>
    <link href="https://atom.example.com/one"/>
    <updated>2026-09-17T10:00:00Z</updated>
    <summary>Body text.</summary>
  </entry>
</feed>"""

# media:content also carries audio and video; only the picture may be taken.
MIXED_MEDIA = """<?xml version="1.0"?>
<rss version="2.0" xmlns:media="http://search.yahoo.com/mrss/">
  <channel><item>
    <title>Podcast Episode</title>
    <link>https://example.com/pod</link>
    <media:content url="https://cdn.example.com/ep.mp3" type="audio/mpeg"/>
    <media:content url="https://cdn.example.com/art.jpg" type="image/jpeg"/>
  </item></channel>
</rss>"""


class TestParsing(unittest.TestCase):
    def test_rss_item_becomes_a_card(self):
        items = briefing._parse_feed(RSS.encode(), "https://www.motor1.com/rss/")
        self.assertEqual(len(items), 2)
        first = items[0]
        self.assertEqual(first["title"], "Chevy's New V8 Ditches The Dipstick")
        self.assertEqual(first["url"], "https://example.com/v8")
        self.assertEqual(first["image"], "https://cdn.example.com/v8.jpg")
        self.assertEqual(first["source"], "Motor1")

    def test_description_html_is_stripped_for_speech(self):
        items = briefing._parse_feed(RSS.encode(), "https://x.com/f")
        self.assertEqual(items[0]["summary"], "A digital oil gauge.")

    def test_missing_image_is_empty_not_absent(self):
        items = briefing._parse_feed(RSS.encode(), "https://x.com/f")
        self.assertEqual(items[1]["image"], "")

    def test_atom_entries_parse_and_link_comes_from_href(self):
        items = briefing._parse_feed(ATOM.encode(), "https://atom.example.com/f")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["url"], "https://atom.example.com/one")

    def test_audio_enclosure_is_not_used_as_the_picture(self):
        items = briefing._parse_feed(MIXED_MEDIA.encode(), "https://x.com/f")
        self.assertEqual(items[0]["image"], "https://cdn.example.com/art.jpg")

    def test_unparseable_feed_yields_nothing_rather_than_raising(self):
        self.assertEqual(briefing._parse_feed(b"<html>nope", "https://x.com/f"), [])


class TestSourceNames(unittest.TestCase):
    def test_public_suffix_only_is_stripped(self):
        # Regression: stripping every dotted part turned hnrss.org into "Org".
        self.assertEqual(briefing._source_name("https://hnrss.org/frontpage"),
                         "Hacker News")

    def test_known_hosts_get_their_real_masthead(self):
        self.assertEqual(
            briefing._source_name("https://www.theguardian.com/world/rss"),
            "The Guardian")
        self.assertEqual(
            briefing._source_name("https://www.caranddriver.com/rss/all.xml/"),
            "Car and Driver")

    def test_unknown_host_falls_back_to_its_stem(self):
        self.assertEqual(briefing._source_name("https://www.motor1.com/rss/"),
                         "Motor1")


class TestTopicRouting(unittest.TestCase):
    """The carrier-phrase bug: "what's new in the AI world" is not politics."""

    FAKE = {
        "interests": {
            "cars": {"say": ["cars", "auto"], "feeds": ["https://c.example/f"]},
            "politics": {"say": ["politics", "india"], "feeds": ["https://p.example/f"]},
        }
    }

    def setUp(self):
        patcher = mock.patch.object(briefing, "_cfg", return_value=self.FAKE)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_bare_question_means_everything(self):
        for phrase in ("", "what's going on in the world", "what's the news",
                       "catch me up", "anything happening"):
            label, feeds = briefing.match_interest(phrase)
            self.assertEqual(label, "your world", phrase)
            self.assertEqual(len(feeds), 2, phrase)

    def test_ai_world_does_not_route_to_politics(self):
        label, feeds = briefing.match_interest("what's new in the AI world")
        self.assertEqual(label, "ai")
        self.assertEqual(feeds, [])          # falls through to Google News

    def test_named_interest_uses_only_its_feeds(self):
        label, feeds = briefing.match_interest("anything happening with cars")
        self.assertEqual(label, "cars")
        self.assertEqual(feeds, ["https://c.example/f"])

    def test_interest_still_matches_inside_a_longer_phrase(self):
        label, _ = briefing.match_interest("tell me about indian politics")
        self.assertEqual(label, "politics")

    def test_unknown_subject_survives_for_google_news(self):
        label, feeds = briefing.match_interest("what's new in quantum computing")
        self.assertEqual(label, "quantum computing")
        self.assertEqual(feeds, [])


class TestMerge(unittest.TestCase):
    def _card(self, title, published=None):
        return {"title": title, "url": "u", "image": "", "source": "s",
                "published": published or time.time(), "summary": ""}

    def test_round_robin_stops_one_feed_sweeping_the_grid(self):
        loud = [self._card(f"loud {i}") for i in range(6)]
        quiet = [self._card("quiet 1")]
        out = briefing._interleave([loud, quiet], 4)
        self.assertEqual(out[0]["title"], "loud 0")
        self.assertEqual(out[1]["title"], "quiet 1")

    def test_same_story_from_two_outlets_appears_once(self):
        a = [self._card("Budget passes after long debate")]
        b = [self._card("Budget passes after long debate!")]   # different punctuation
        out = briefing._interleave([a, b], 6)
        self.assertEqual(len(out), 1)

    def test_limit_is_respected(self):
        batch = [self._card(f"story {i}") for i in range(20)]
        self.assertEqual(len(briefing._interleave([batch], 6)), 6)


class TestGoogleNewsAttribution(unittest.TestCase):
    FEED = """<?xml version="1.0"?><rss version="2.0"><channel>
      <item><title>Energy Department Launches Quantum Push - GovTech</title>
            <link>https://news.google.com/x</link></item>
      <item><title>A headline with no publisher suffix</title>
            <link>https://news.google.com/y</link></item>
    </channel></rss>"""

    def test_publisher_is_lifted_out_of_the_headline(self):
        with mock.patch.object(briefing, "_fetch_one",
                               return_value=briefing._parse_feed(
                                   self.FEED.encode(), "https://news.google.com/rss")):
            items = briefing._google_news("quantum", 4)
        self.assertEqual(items[0]["source"], "GovTech")
        self.assertEqual(items[0]["title"],
                         "Energy Department Launches Quantum Push")

    def test_headline_without_a_suffix_is_left_alone(self):
        with mock.patch.object(briefing, "_fetch_one",
                               return_value=briefing._parse_feed(
                                   self.FEED.encode(), "https://news.google.com/rss")):
            items = briefing._google_news("quantum", 4)
        self.assertEqual(items[1]["source"], "Google News")
        self.assertEqual(items[1]["title"], "A headline with no publisher suffix")


if __name__ == "__main__":
    unittest.main()


class TestBriefingReachesTheActionPath(unittest.TestCase):
    """The gate, not the command.

    Every one of these was classified as conversation and answered from the
    model's weights — "what's new in quantum computing" came back with three
    paragraphs of invented research. They have to reach the planner, which is
    the only place show_briefing can be chosen.
    """

    def _act(self, text):
        from nora import dialogue
        return dialogue.classify(text, has_prior_turn=False)

    def test_browse_requests_take_the_action_path(self):
        from nora.dialogue import Act
        for phrase in (
            "what's going on in the world",
            "what's the news",
            "what's new in the car world",
            "what's new in quantum computing",
            "what's new in music",
            "what's the latest in AI",
            "anything happening with cars",
            "catch me up",
            "bring me up to speed",
            "what's happening in india",
        ):
            with self.subTest(phrase):
                self.assertEqual(self._act(phrase), Act.COMMAND)

    def test_asking_after_nora_stays_a_pleasantry(self):
        # "what's new with you" is small talk. The news patterns are wide
        # enough to swallow it without the trailing lookaheads.
        from nora.dialogue import Act
        for phrase in ("what's new with you", "what's new with you nora",
                       "anything new with you", "what's going on with you"):
            with self.subTest(phrase):
                self.assertNotEqual(self._act(phrase), Act.COMMAND)

    def test_bare_and_incidental_uses_are_left_alone(self):
        from nora.dialogue import Act
        for phrase in ("what's new?", "that's good news", "how are you",
                       "what do you think about Rust"):
            with self.subTest(phrase):
                self.assertNotEqual(self._act(phrase), Act.COMMAND)


class TestCommandIsRegistered(unittest.TestCase):
    def test_show_briefing_is_discoverable_by_the_planner(self):
        from nora import command_engine
        command_engine.discover_commands()
        self.assertIn("show_briefing", command_engine._registry)
        meta = command_engine._meta["show_briefing"]
        # The planner picks by description; tell_me_about used to claim "any
        # factual/current-events question" and won every briefing request.
        self.assertIn("tell_me_about", meta.description)


class TestFastPathClaimsBriefing(unittest.TestCase):
    """The planner would not route these consistently.

    Asked to choose, it sent "what's new in the car world" to ask_claude and
    "what's new in quantum computing" to tell_me_about, while routing the
    near-identical "what's new in music" to show_briefing. The phrasings are a
    closed set with one meaning, so they are resolved before the LLM is asked.
    """

    def _resolved(self, text):
        from nora import fast_path
        r = fast_path.resolve(text)
        if not r or not r.steps:
            return None, None
        return r.steps[0].action, r.steps[0].parameters.get("topic")

    def test_topical_phrasings_resolve_with_their_subject(self):
        for phrase, topic in (
            ("what's new in the car world", "the car world"),
            ("what's new in quantum computing", "quantum computing"),
            ("what's new in music", "music"),
            ("what's the latest in AI", "AI"),
            ("anything happening with cars", "cars"),
            ("whats happening in india", "india"),     # Whisper drops apostrophes
        ):
            with self.subTest(phrase):
                action, got = self._resolved(phrase)
                self.assertEqual(action, "show_briefing")
                self.assertEqual(got, topic)

    def test_bare_phrasings_resolve_with_no_topic(self):
        for phrase in ("what's going on in the world", "what's the news",
                       "catch me up", "bring me up to speed", "any news",
                       "whats the news", "headlines"):
            with self.subTest(phrase):
                action, topic = self._resolved(phrase)
                self.assertEqual(action, "show_briefing")
                self.assertEqual(topic, "")

    def test_the_captured_topic_survives_interest_matching(self):
        # The rule passes the subject through raw; _core_topic does the work.
        for phrase, label in (("what's new in the car world", "cars"),
                              ("what's new in music", "music"),
                              ("what's going on in the world", "your world")):
            with self.subTest(phrase):
                _, topic = self._resolved(phrase)
                self.assertEqual(briefing.match_interest(topic or "")[0], label)

    def test_small_talk_and_specific_questions_are_left_to_the_planner(self):
        for phrase in ("what's new with you", "whats new with you",
                       "who won the F1 race yesterday", "what's the weather",
                       "what's on my screen"):
            with self.subTest(phrase):
                action, _ = self._resolved(phrase)
                self.assertNotEqual(action, "show_briefing")
