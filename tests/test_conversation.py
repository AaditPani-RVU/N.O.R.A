"""Tests for the conversation layer (phrasing, dialogue, conversation, ack).

Stdlib unittest only — run with:  python -m unittest tests.test_conversation -v
No network: the one test that exercises the generation path stubs the model
call, so the suite is deterministic and offline.
"""
from __future__ import annotations

import unittest
from unittest import mock

from nora import ack, conversation, dialogue, phrasing
from nora.dialogue import Act


class PhrasingTest(unittest.TestCase):
    def setUp(self) -> None:
        phrasing.reset_history()

    def test_returns_a_line_from_the_pool(self) -> None:
        line = phrasing.get("cancelled")
        self.assertIn(line, phrasing._POOLS["cancelled"])

    def test_never_repeats_back_to_back(self) -> None:
        # The whole point of the module: the same waveform twice running is
        # the loudest "this is a lookup table" signal there is.
        for category in ("not_understood", "greeting", "error", "backchannel"):
            with self.subTest(category=category):
                previous = ""
                for _ in range(40):
                    line = phrasing.get(category)
                    self.assertNotEqual(line, previous, f"{category} repeated {line!r}")
                    previous = line

    def test_unknown_category_returns_default(self) -> None:
        self.assertEqual(phrasing.get("no_such_pool", "fallback"), "fallback")
        self.assertEqual(phrasing.get("no_such_pool"), "")

    def test_every_pool_is_non_empty_and_varied(self) -> None:
        for name, pool in phrasing._POOLS.items():
            with self.subTest(pool=name):
                self.assertGreaterEqual(len(pool), 3, f"{name} too small to vary")
                self.assertEqual(len(pool), len(set(pool)), f"{name} has duplicates")

    def test_casual_tone_selects_casual_variant(self) -> None:
        with mock.patch.object(phrasing, "_tone", return_value="casual"):
            picks = {phrasing.get("greeting") for _ in range(20)}
        self.assertTrue(picks.issubset(set(phrasing._POOLS["greeting_casual"])))


class DialogueHistoryTest(unittest.TestCase):
    def setUp(self) -> None:
        dialogue.clear()

    def test_records_both_sides(self) -> None:
        dialogue.record_user("what's the capital of France")
        dialogue.record_nora("Paris.")
        self.assertEqual(dialogue.turn_count(), 2)
        self.assertEqual(dialogue.last_nora_reply(), "Paris.")
        self.assertEqual(dialogue.last_user_utterance(), "what's the capital of France")

    def test_has_context_requires_a_nora_turn(self) -> None:
        self.assertFalse(dialogue.has_context())
        dialogue.record_user("hello")
        self.assertFalse(dialogue.has_context())
        dialogue.record_nora("Hello, sir.")
        self.assertTrue(dialogue.has_context())

    def test_blank_utterances_are_ignored(self) -> None:
        dialogue.record_user("   ")
        dialogue.record_nora("")
        self.assertEqual(dialogue.turn_count(), 0)

    def test_as_messages_merges_consecutive_same_role(self) -> None:
        # An action turn speaks a summary right after a chat reply; most chat
        # endpoints reject back-to-back assistant messages.
        dialogue.record_user("open chrome")
        dialogue.record_nora("Opening Chrome.")
        dialogue.record_nora("Chrome is open.")
        dialogue.record_user("thanks")
        messages = dialogue.as_messages()
        roles = [m["role"] for m in messages]
        self.assertEqual(roles, ["user", "assistant", "user"])
        self.assertIn("Opening Chrome.", messages[1]["content"])
        self.assertIn("Chrome is open.", messages[1]["content"])

    def test_consecutive_user_turns_are_kept_separate(self) -> None:
        # Merging them makes two unrelated utterances read as one thought.
        dialogue.record_user("what's the weather")
        dialogue.record_user("i'm rewriting the audio layer")
        messages = dialogue.as_messages()
        self.assertEqual([m["role"] for m in messages], ["user", "user"])

    def test_history_is_bounded(self) -> None:
        for i in range(200):
            dialogue.record_user(f"turn {i}")
        self.assertLessEqual(dialogue.turn_count(), dialogue._MAX_TURNS)

    def test_transcript_labels_speakers(self) -> None:
        dialogue.record_user("hi")
        dialogue.record_nora("Hello.")
        transcript = dialogue.as_transcript()
        self.assertIn("User: hi", transcript)
        self.assertIn("NORA: Hello.", transcript)

    def test_topic_skips_stopwords(self) -> None:
        dialogue.set_topic("what do you think about the rust compiler")
        self.assertIn("rust", dialogue.get_topic())
        self.assertNotIn("think", dialogue.get_topic().split())


class DialogueActTest(unittest.TestCase):
    def setUp(self) -> None:
        dialogue.clear()

    def test_commands_are_never_stolen_by_the_chat_path(self) -> None:
        # The critical safety property. A misrouted command is a much worse
        # failure than a misrouted pleasantry, so this list is the guard.
        commands = [
            "open chrome", "play some music", "take a screenshot",
            "delete test.txt", "close firefox", "turn up the volume",
            "type hello world", "click the submit button", "next track",
            "connect to wifi CoffeeShop", "shut down", "press enter",
            "search for python tutorials", "roll back to before-refactor",
            "hey open chrome", "nora, play music",
        ]
        for text in commands:
            with self.subTest(text=text):
                act = dialogue.classify(text, has_prior_turn=True)
                self.assertEqual(act, Act.COMMAND, f"{text!r} escaped the action path")
                self.assertFalse(dialogue.is_conversational(act))

    def test_small_talk(self) -> None:
        for text in ["hey", "hi there", "hey nora", "how are you", "what's up", "thanks"]:
            with self.subTest(text=text):
                self.assertEqual(dialogue.classify(text, has_prior_turn=False), Act.SMALL_TALK)

    def test_backchannels(self) -> None:
        for text in ["mhm", "ok", "cool", "right", "got it", "makes sense", "yeah", "uh huh"]:
            with self.subTest(text=text):
                self.assertEqual(dialogue.classify(text, has_prior_turn=True), Act.BACKCHANNEL)

    def test_follow_ups_need_prior_context(self) -> None:
        # "why?" with nothing said yet is not a follow-up.
        for text in ["why", "go on", "say that again", "are you sure", "what about the other one"]:
            with self.subTest(text=text):
                self.assertEqual(dialogue.classify(text, has_prior_turn=True), Act.FOLLOW_UP)
                self.assertNotEqual(dialogue.classify(text, has_prior_turn=False), Act.FOLLOW_UP)

    def test_meta_questions(self) -> None:
        for text in ["who are you", "what can you do", "are you an ai"]:
            with self.subTest(text=text):
                self.assertEqual(dialogue.classify(text, has_prior_turn=False), Act.META)

    def test_discussion_beats_meta(self) -> None:
        # "what do you think about X" is about X, not about NORA.
        self.assertEqual(
            dialogue.classify("what do you think about rust", has_prior_turn=False),
            Act.QUESTION,
        )

    def test_real_world_lookups_stay_on_the_action_path(self) -> None:
        # These need a tool call. Routed to chat, the model invents a forecast.
        # Matching the topic word ("weather", "news") was not enough — people
        # ask about the weather without ever saying it, so these cover the
        # phrasings that actually come out of someone's mouth.
        for text in [
            "what's the weather", "what's the weather like today",
            "is it going to rain", "is it raining", "will it rain tomorrow",
            "how hot is it outside", "how cold is it", "do i need an umbrella",
            "what's it like outside", "what's the forecast",
            "what time is it", "what's the time", "what's the date",
            "what's today's date", "what day is it",
            "what's in the news", "any news", "what's happening in the world",
            "who won the game last night", "what's the score",
            "what's apple trading at",
            "how much battery do i have", "how's my battery",
            "how much disk space do i have",
        ]:
            with self.subTest(text=text):
                act = dialogue.classify(text, has_prior_turn=True)
                self.assertEqual(act, Act.COMMAND, f"{text!r} would be answered from imagination")

    def test_screen_questions_never_reach_the_chat_path(self) -> None:
        # Observed in the wild: "what's on my screen" classified as a question,
        # went to the chat model, and got answered "Your screen is just the
        # desktop" — invented, because the chat path cannot see anything.
        # These have to reach read_screen.
        for text in [
            "what is on my screen", "whats on my screen right now",
            "what do you see on my screen", "what am I looking at",
            "read my screen", "what is happening on my screen right now",
            "what is this window", "what is showing",
            # Whisper garbles this one regularly; it must still route right.
            "What did on my screen right now?",
        ]:
            with self.subTest(text=text):
                act = dialogue.classify(text, has_prior_turn=True)
                self.assertFalse(
                    dialogue.is_conversational(act),
                    f"{text!r} would be answered without looking at the screen",
                )

    def test_lookup_topics_in_chat_do_not_hijack_the_action_path(self) -> None:
        # The other half of the same problem: a topic word in a conversational
        # turn used to drag it onto the action path, where there is no
        # transcript and nothing sensible to search for.
        for text, expected in [
            ("that's good news", Act.FOLLOW_UP),
            ("any news on that", Act.FOLLOW_UP),
            ("do you like cold weather", Act.META),
            ("what do you think about the news coverage", Act.QUESTION),
        ]:
            with self.subTest(text=text):
                act = dialogue.classify(text, has_prior_turn=True)
                self.assertEqual(act, expected)
                self.assertTrue(dialogue.is_conversational(act))

    def test_conversational_path_knows_the_real_date(self) -> None:
        # The safety net for anything the lookup patterns miss: if a time or
        # date question does reach the chat path, the answer has to come from
        # the clock, not the model.
        from datetime import datetime
        from nora import conversation

        block = conversation._situation_block(None)
        now = datetime.now()
        self.assertIn(now.strftime("%A"), block)
        self.assertIn(now.strftime("%B"), block)
        self.assertIn(str(now.year), block)

    def test_research_requests_stay_on_the_action_path(self) -> None:
        # Long how-to questions route to ask_claude via the intent parser.
        act = dialogue.classify("how do I reverse a linked list in python", has_prior_turn=True)
        self.assertFalse(dialogue.is_conversational(act))

    def test_empty_input(self) -> None:
        self.assertEqual(dialogue.classify(""), Act.UNKNOWN)
        self.assertEqual(dialogue.classify("uh, um"), Act.BACKCHANNEL)

    def test_classify_uses_live_transcript_by_default(self) -> None:
        self.assertNotEqual(dialogue.classify("why"), Act.FOLLOW_UP)
        dialogue.record_user("what's the capital of France")
        dialogue.record_nora("Paris.")
        self.assertEqual(dialogue.classify("why"), Act.FOLLOW_UP)


class ForSpeechTest(unittest.TestCase):
    def test_strips_reasoning_traces(self) -> None:
        self.assertEqual(
            conversation.for_speech("<think>let me consider</think>The answer is 42."),
            "The answer is 42.",
        )

    def test_strips_orphaned_closing_think_tag(self) -> None:
        # A truncated response can lose the opening tag but keep the closer.
        self.assertEqual(
            conversation.for_speech("reasoning about it</think> The answer is 42."),
            "The answer is 42.",
        )

    def test_strips_markdown(self) -> None:
        out = conversation.for_speech("**Bold** and `code` and [a link](https://x.com)")
        for artifact in ("**", "`", "[", "]", "(", "https"):
            self.assertNotIn(artifact, out)

    def test_list_items_get_sentence_boundaries(self) -> None:
        out = conversation.for_speech("- install ripgrep\n- run it")
        self.assertNotIn("-", out)
        self.assertIn("install ripgrep.", out.lower())
        self.assertIn("run it.", out.lower())

    def test_urls_become_the_site_name(self) -> None:
        # Nobody can click a link they are hearing, and reading the path aloud
        # ("slash petrol dash price dash in dash bangalore dot html") is worse
        # than useless — say who said it instead.
        cases = {
            "Per www.goodreturns.in/petrol-price-in-bangalore.html today.":
                "Per goodreturns today.",
            "Reported by https://www.theguardian.com/world/iran":
                "Reported by theguardian",
            "See en.wikipedia.org/wiki/Iran": "See wikipedia",
            "Sources: bbc.co.uk and reuters.com.": "Sources: bbc and reuters.",
            "Rates from goodreturns.in today.": "Rates from goodreturns today.",
        }
        for raw, spoken in cases.items():
            self.assertEqual(conversation.for_speech(raw), spoken, raw)

    def test_dotted_non_urls_are_left_alone(self) -> None:
        for raw in ("Pi is 3.14 and the module is nora.commands.web_search",
                    "Petrol is Rs.106.86 in Bangalore.",
                    "The U.S. Department said so."):
            self.assertEqual(conversation.for_speech(raw), raw, raw)

    def test_a_missing_space_after_a_full_stop_is_not_a_domain(self) -> None:
        # Models drop the space after a full stop, and ".in"/".me"/".info" are
        # both TLDs and ordinary words — matching them ate the following word.
        self.assertEqual(
            conversation.for_speech("I finished.In the meantime, ask me."),
            "I finished. In the meantime, ask me.",
        )
        self.assertEqual(conversation.for_speech("It ended.Me too."), "It ended. Me too.")
        self.assertEqual(conversation.for_speech("Done.Info arrives later."),
                         "Done. Info arrives later.")

    def test_strips_emoji(self) -> None:
        self.assertNotIn("🚀", conversation.for_speech("Done 🚀"))

    def test_strips_stacked_stock_openers(self) -> None:
        out = conversation.for_speech("Certainly! I would be happy to help with that.")
        self.assertFalse(out.lower().startswith("certainly"))
        self.assertFalse(out.lower().startswith("i would be happy"))

    def test_normalises_typographic_punctuation(self) -> None:
        # gpt-oss emits U+2011; some voices read it as a spoken word.
        out = conversation.for_speech("zero‑cost and it’s fine…")
        self.assertNotIn("‑", out)
        self.assertNotIn("’", out)
        self.assertNotIn("…", out)

    def test_unwraps_fully_quoted_reply(self) -> None:
        self.assertEqual(conversation.for_speech('"Just a quoted reply."'), "Just a quoted reply.")

    def test_sentence_cap(self) -> None:
        self.assertEqual(
            conversation.for_speech("One. Two. Three. Four.", max_sentences=2), "One. Two."
        )

    def test_collapses_glued_duplicate_sentences(self) -> None:
        # Two model channels concatenated with no separator. Invisible when
        # skimmed, unmissable when spoken.
        out = conversation.for_speech("It is raining.It is raining.")
        self.assertEqual(out, "It is raining.")

    def test_collapses_spaced_duplicate_sentences(self) -> None:
        out = conversation.for_speech("The answer is 42. The answer is 42. But it varies.")
        self.assertEqual(out, "The answer is 42. But it varies.")

    def test_initialisms_survive_the_boundary_fix(self) -> None:
        out = conversation.for_speech("He works at the U.S.A. Department.")
        self.assertIn("U.S.A.", out)

    def test_distinct_sentences_are_not_collapsed(self) -> None:
        self.assertEqual(conversation.for_speech("One. Two. Three."), "One. Two. Three.")

    def test_truncates_a_looped_answer(self) -> None:
        # Observed with gpt-oss at temperature 0.7: the model restarts its own
        # answer and the token budget cuts the second pass off mid-sentence.
        looped = (
            "A rewrite pays off when the code blocks you. "
            "On the flip side it costs real time and risk. "
            "A rewrite pays off when the code blocks you. "
            "On the flip side it costs real"
        )
        out = conversation.for_speech(looped)
        self.assertEqual(out.count("A rewrite pays off"), 1)
        self.assertTrue(out.endswith("."))

    def test_short_repeated_sentences_are_allowed(self) -> None:
        # "Yes." twice in one answer is legitimate; only long repeats mark a loop.
        self.assertEqual(conversation.for_speech("Yes. It works well. Yes."),
                         "Yes. It works well. Yes.")

    def test_drops_a_truncated_trailing_fragment(self) -> None:
        self.assertEqual(
            conversation.for_speech("This is complete. This one got cut off mid"),
            "This is complete.",
        )

    def test_strips_leaked_chain_of_thought(self) -> None:
        # gpt-oss's analysis channel leaking into content — NORA reading its
        # own scratchpad aloud. Suppressed at the provider, filtered here too.
        out = conversation.for_speech(
            "Yes, a rewrite makes sense. We need to consider the conversation: "
            "user asked \"are you sure\". The guidelines say answer in 1-3 sentences."
        )
        self.assertEqual(out, "Yes, a rewrite makes sense.")

    def test_analysis_filter_does_not_empty_a_reply(self) -> None:
        # If every sentence looks like analysis the match was spurious.
        out = conversation.for_speech("We need to talk about this. We need to decide soon.")
        self.assertTrue(out)

    def test_empty_input(self) -> None:
        self.assertEqual(conversation.for_speech(""), "")


class LeakedReasoningTest(unittest.TestCase):
    """A hybrid-reasoning model dumping its scratchpad into `content`.

    Both samples below are real replies NORA spoke aloud (nora.log, 2026-08-20
    16:25 and 16:32) after the Groq candidates were unavailable and the chain
    fell through to Nemotron, which has no reasoning_format switch. The config
    fix (chat_template_kwargs.thinking) stops it at the provider; these guard
    the net underneath it.
    """

    LEAKS = [
        'The prior context: "How many units does Nora have?" The assistant '
        'said they need to look it up. Now user says "Yeah, can you do it?" '
        "So we should proceed to retrieve the unit count. Likely we have a "
        "function to query some system? Not fully defined.",
        "Must not repeat previous opening. Must not restate question. In "
        'prior conversation, we told them "Yes, you asked me to tell you '
        'about Kanye West." That\'s it.',
        "We need to recall what we previously told the user about Kanye "
        "West. So now user asks what we said. We should respond with that "
        "info again?",
    ]

    # Real replies from the same log. These must survive — the validator gates
    # genuine answers, so a false positive costs the user a real response.
    GENUINE = [
        "I told you that Kanye West, born June 8, 1977, is an American "
        "producer, rapper, and designer.",
        "You asked me to recall your recent notes. I'm ready to retrieve "
        "whatever you need.",
        "Yes, you asked me to tell you about Kanye West.",
        "I'm not sure of the exact unit count offhand; I'll need to look "
        "that up for you. Give me a moment and I'll get the current number.",
        # "We need to" addressed to the user, not about them.
        "We need to get you set up with an API key first, then I can "
        "search live.",
        "The user manual for that printer is probably on the maker's site.",
    ]

    def test_leaked_scratchpad_is_detected(self):
        for raw in self.LEAKS:
            self.assertTrue(conversation.looks_like_analysis(raw), raw[:60])

    def test_genuine_replies_are_not_flagged(self):
        for raw in self.GENUINE:
            self.assertFalse(conversation.looks_like_analysis(raw), raw[:60])

    def test_router_falls_through_to_the_next_model_on_a_leak(self):
        """A leak must be treated as a failed call, not spoken."""
        from nora import model_router

        calls = []

        def fake_call(candidate, messages, max_tokens, temperature, timeout, extras):
            calls.append(candidate["name"])
            if candidate["name"] == "leaky":
                return self.LEAKS[0]
            return "He's an American producer and designer."

        candidates = [{"name": "leaky", "model": "m"}, {"name": "clean", "model": "m"}]
        with mock.patch.object(model_router, "_candidates_for", return_value=candidates), \
             mock.patch.object(model_router, "_call_openai_compatible", fake_call):
            text, used = model_router.complete(
                "chat", [{"role": "user", "content": "hi"}],
                validate=lambda t: not conversation.looks_like_analysis(t),
            )

        self.assertEqual(calls, ["leaky", "clean"])
        self.assertEqual(used, "clean")
        self.assertNotIn("prior context", text.lower())

    def test_for_speech_removes_analysis_sentences(self):
        spoken = conversation.for_speech(
            "The user asked about Kanye West. He's an American producer."
        )
        self.assertNotIn("user asked", spoken.lower())
        self.assertIn("American producer", spoken)


class ConversationRespondTest(unittest.TestCase):
    def setUp(self) -> None:
        dialogue.clear()
        phrasing.reset_history()

    def test_backchannel_skips_the_model_entirely(self) -> None:
        with mock.patch.object(conversation, "_generate") as generate:
            reply = conversation.respond("mhm", None, Act.BACKCHANNEL)
        generate.assert_not_called()
        self.assertIn(reply, phrasing._POOLS["backchannel"])

    def test_thanks_inside_a_backchannel_gets_a_thanks_reply(self) -> None:
        reply = conversation.respond("cool thanks", None, Act.BACKCHANNEL)
        self.assertIn(reply, phrasing._POOLS["thanks_reply"])

    def test_records_the_user_turn_but_leaves_noras_to_the_speaker(self) -> None:
        # respond() used to record NORA's side as well, and speaker.speak()
        # records everything NORA says centrally — so every chat reply landed
        # in the transcript twice. as_messages() merges consecutive same-role
        # turns, so the model was shown itself repeating verbatim while being
        # told not to repeat itself.
        with mock.patch.object(conversation, "_generate", return_value="Paris."):
            reply = conversation.respond("what's the capital of France", None, Act.QUESTION)
        self.assertEqual(reply, "Paris.")
        self.assertEqual(dialogue.last_user_utterance(), "what's the capital of France")
        nora_turns = [t for t in dialogue.history() if t.speaker == "nora"]
        self.assertEqual(nora_turns, [], "respond() must not record NORA's side")

    def test_a_spoken_reply_lands_in_the_transcript_exactly_once(self) -> None:
        # The other half of the contract: speaker.speak owns the recording, so
        # the reply must still reach the transcript — once.
        from nora import speaker
        with mock.patch.object(speaker, "_speak_streaming") as streaming:
            streaming.side_effect = lambda text, mood=None, spoken=None: (
                spoken.append(text) if spoken is not None else None
            )
            speaker.speak("Paris.", mood="chat")
        nora_turns = [t.text for t in dialogue.history() if t.speaker == "nora"]
        self.assertEqual(nora_turns, ["Paris."])

    def test_an_interrupted_reply_records_only_what_was_spoken(self) -> None:
        # Barge-in cuts playback part-way. Recording the full reply left NORA
        # referring back to sentences the user never heard.
        from nora import speaker
        with mock.patch.object(speaker, "_speak_streaming") as streaming:
            streaming.side_effect = lambda text, mood=None, spoken=None: (
                spoken.append("First part.") if spoken is not None else None
            )
            speaker.speak("First part. Second part. Third part.", mood="chat")
        nora_turns = [t.text for t in dialogue.history() if t.speaker == "nora"]
        self.assertEqual(nora_turns, ["First part."])

    def test_does_not_double_record_a_turn_the_pipeline_already_logged(self) -> None:
        dialogue.record_user("what's the capital of France")
        with mock.patch.object(conversation, "_generate", return_value="Paris."):
            conversation.respond("what's the capital of France", None, Act.QUESTION)
        users = [t for t in dialogue.history() if t.speaker == "user"]
        self.assertEqual(len(users), 1)

    def test_history_is_passed_to_the_model(self) -> None:
        dialogue.record_user("what's the capital of France")
        dialogue.record_nora("Paris.")
        captured: dict = {}

        def fake(system, messages, act):
            captured["messages"] = messages
            captured["system"] = system
            return "Because it's the seat of government."

        with mock.patch.object(conversation, "_generate", side_effect=fake):
            conversation.respond("why", None, Act.FOLLOW_UP)

        roles = [m["role"] for m in captured["messages"]]
        self.assertIn("assistant", roles)
        self.assertTrue(any("Paris." in m["content"] for m in captured["messages"]))
        # The previous reply is surfaced so the model doesn't repeat itself.
        self.assertIn("Paris.", captured["system"])

    def test_model_outage_degrades_to_speech_not_silence(self) -> None:
        with mock.patch.object(conversation, "_generate", return_value=""):
            reply = conversation.respond("what do you think about rust", None, Act.QUESTION)
        self.assertTrue(reply)
        self.assertIn(reply, phrasing._POOLS["chat_unavailable"])

    def test_output_is_shaped_for_speech(self) -> None:
        with mock.patch.object(conversation, "_generate", return_value="**Sure!** Use `rg` 🚀"):
            reply = conversation.respond("what do you think", None, Act.QUESTION)
        for artifact in ("**", "`", "🚀"):
            self.assertNotIn(artifact, reply)

    def test_should_handle_respects_config(self) -> None:
        self.assertTrue(conversation.should_handle(Act.QUESTION))
        self.assertFalse(conversation.should_handle(Act.COMMAND))
        with mock.patch.object(conversation, "_conv_cfg", return_value={"enabled": False}):
            self.assertFalse(conversation.should_handle(Act.QUESTION))

    def test_system_prompt_forbids_markdown(self) -> None:
        prompt = conversation.build_system_prompt(Act.QUESTION)
        self.assertIn("markdown", prompt.lower())
        self.assertIn("speaking out loud", prompt.lower())

    def test_length_guidance_varies_by_act(self) -> None:
        # The old prompt applied one flat "1-2 sentences" cap to everything.
        short = conversation.build_system_prompt(Act.BACKCHANNEL)
        long = conversation.build_system_prompt(Act.QUESTION)
        self.assertNotEqual(short, long)
        self.assertIn("five words", short)


class AckTest(unittest.TestCase):
    """How the tokens behave *when switched on*. They ship off.

    Acknowledgement tokens are disabled in config.yaml and no capture path
    calls them any more: played into an open microphone they were recorded as
    the user's own speech and ended the turn (nora/ack.py has the full note;
    tests/test_ptt_turns.py guards the removal). These cover the tuning that
    still governs them for anyone who turns `ack.enabled` back on, so they
    enable it explicitly rather than inheriting the shipped default.
    """

    ENABLED = {"enabled": True}

    def setUp(self) -> None:
        ack._loaded.clear()
        ack._ack_sounds.clear()
        ack._last_played_at = 0.0
        with ack._lock:
            if ack._pending_timer is not None:
                ack._pending_timer.cancel()
            ack._pending_timer = None

    def test_ack_is_deferred_not_immediate(self) -> None:
        # The fix for "it says uh huh too fast": the ack is scheduled, and a
        # real response arriving first cancels it unheard.
        ack._loaded.set()
        ack._ack_sounds["Mm-hm."] = object()
        with mock.patch.object(ack, "_cfg", return_value=self.ENABLED), \
             mock.patch.object(ack, "_play_now") as play:
            ack.speak_ack()
            self.assertIsNotNone(ack._pending_timer)
            play.assert_not_called()
            ack.cancel_ack()
        self.assertIsNone(ack._pending_timer)

    def test_force_plays_immediately(self) -> None:
        # `force` skips the defer-and-cancel dance. Nothing uses it now that the
        # wake path is silent, but it is the switch a safe cue would need.
        ack._loaded.set()
        ack._ack_sounds["Mm-hm."] = object()
        with mock.patch.object(ack, "_cfg", return_value=self.ENABLED), \
             mock.patch.object(ack, "_play_now") as play:
            ack.speak_ack(force=True)
            play.assert_called_once_with(force=True)

    def test_cooldown_suppresses_rapid_acks(self) -> None:
        ack._loaded.set()
        ack._ack_sounds["Mm-hm."] = mock.MagicMock()
        with mock.patch.object(ack, "_cfg", return_value={"cooldown_sec": 60.0}), \
             mock.patch.object(ack, "_ack_channel", None):
            ack._play_now()
            first = ack._last_played_at
            ack._play_now()
            self.assertEqual(ack._last_played_at, first, "cooldown was not enforced")

    def test_ack_rate_is_slower_than_baseline(self) -> None:
        # Tokens used to synthesize at +40% on top of the speaker's +20%.
        self.assertTrue(ack._ACK_RATE.startswith("-"))
        self.assertLess(ack._ACK_VOLUME, 1.0)

    def test_disabled_config_is_a_no_op(self) -> None:
        ack._loaded.set()
        ack._ack_sounds["Mm-hm."] = object()
        with mock.patch.object(ack, "_cfg", return_value={"enabled": False}), \
             mock.patch.object(ack, "_play_now") as play:
            ack.speak_ack(delay=0)
            play.assert_not_called()


if __name__ == "__main__":
    unittest.main()
