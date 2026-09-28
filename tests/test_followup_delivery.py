"""Why a promised answer never arrived.

"I'll get back to you" is made true by `conversation._honour_promise` queueing
a real `nora.jobs` job, and the job did run — 5.9 seconds, answer in hand. It
was never spoken, for reasons in two different modules:

  * `focus` classified NORA's own echo-cancel capture stream as somebody
    else's microphone, so the state was CALL forever and the gated speak
    channel queued everything without ever flushing;
  * `jobs._save` raced itself on a shared temp filename, so the queue stopped
    persisting and nothing survived a restart to be recalled later.

Plus the routing failure that started it: "can you check my mail" is an
instruction, and sending it to the tool-less conversation engine is what
produced a promise in the first place.
"""
from __future__ import annotations

import threading
import unittest
from unittest import mock

from nora import dialogue, focus, jobs
from nora.dialogue import Act


def _node(media_class: str, *, state: str = "running", **props) -> dict:
    return {
        "type": "PipeWire:Interface:Node",
        "info": {"state": state, "props": {"media.class": media_class, **props}},
    }


class FocusSelfStreamTest(unittest.TestCase):
    """A running input stream is only a call if it belongs to someone else."""

    def setUp(self):
        focus._cache = None
        focus._last_activity_ts = __import__("time").time()

    def tearDown(self):
        focus._cache = None

    def _state_for(self, nodes):
        with mock.patch("nora.platform.linux.pipewire_graph._pw_dump", return_value=nodes):
            focus._cache = None
            return focus._pipewire_state()

    def test_noras_own_echo_capture_is_not_a_call(self):
        """The regression. Empty application.name, identity in node.name."""
        nodes = [_node("Stream/Input/Audio", **{
            "application.name": "", "node.name": "nora_echo_capture"})]
        self.assertIs(self._state_for(nodes), focus.FocusState.AVAILABLE)

    def test_noras_denoised_source_is_not_a_call(self):
        nodes = [_node("Audio/Source", **{"node.name": "nora_denoised_mic"})]
        self.assertIs(self._state_for(nodes), focus.FocusState.AVAILABLE)

    def test_noras_own_playback_is_not_media(self):
        nodes = [_node("Stream/Output/Audio", **{
            "application.name": "", "node.name": "nora_echo_playback"})]
        self.assertIs(self._state_for(nodes), focus.FocusState.AVAILABLE)

    def test_a_real_call_is_still_a_call(self):
        nodes = [_node("Stream/Input/Audio", **{
            "application.name": "Zoom", "node.name": "zoom_capture"})]
        self.assertIs(self._state_for(nodes), focus.FocusState.CALL)

    def test_someone_elses_playback_is_still_media(self):
        nodes = [_node("Stream/Output/Audio", **{
            "application.name": "Spotify", "node.name": "spotify"})]
        self.assertIs(self._state_for(nodes), focus.FocusState.MEDIA)

    def test_suspended_streams_are_ignored(self):
        nodes = [_node("Stream/Input/Audio", state="suspended",
                       **{"application.name": "Zoom"})]
        self.assertIs(self._state_for(nodes), focus.FocusState.AVAILABLE)

    def test_the_observed_graph_reads_as_available(self):
        """Every node from the real `pw-dump` at the time of the bug, minus the
        browser that was genuinely playing audio."""
        nodes = [
            _node("Stream/Input/Audio", **{"application.name": "", "node.name": "nora_echo_capture"}),
            _node("Audio/Source", **{"application.name": "", "node.name": "nora_denoised_mic"}),
            _node("Stream/Output/Audio", **{"application.name": "", "node.name": "nora_echo_playback"}),
            _node("Audio/Source", state="suspended", **{"node.name": "alsa_input...Mic2__source"}),
            _node("Audio/Source", **{"node.name": "alsa_input...Mic1__source"}),
            _node("Stream/Output/Audio", state="idle", **{"application.name": "Spotify"}),
            _node("Stream/Input/Audio", **{
                "application.name": "PipeWire ALSA [python3.13]",
                "node.name": "alsa_capture.python3.13"}),
            _node("Stream/Output/Audio", **{"application.name": "python3.13"}),
        ]
        self.assertIs(self._state_for(nodes), focus.FocusState.AVAILABLE)


class GatedSpeechFlushTest(unittest.TestCase):
    """Deferred speech is re-timed, not dropped — provided the state recovers."""

    def setUp(self):
        focus._deferred.clear()
        focus._cache = None

    def tearDown(self):
        focus._deferred.clear()
        focus._cache = None
        focus._speak_fn = None

    def test_held_while_busy_then_spoken_on_next_activity(self):
        spoken: list[str] = []
        with mock.patch.object(focus, "allows_proactive_speech", return_value=False), \
                mock.patch.object(focus, "current", return_value=focus.FocusState.CALL):
            focus.gated(spoken.append)("Back to your question — 5 unread.")
        self.assertEqual(spoken, [])
        self.assertEqual(len(focus._deferred), 1)

        with mock.patch.object(focus, "allows_proactive_speech", return_value=True):
            focus.note_activity()
        self.assertEqual(spoken, ["Back to your question — 5 unread."])
        self.assertEqual(focus._deferred, [])


class JobsSaveConcurrencyTest(unittest.TestCase):
    """Two workers finishing together must not lose the store."""

    def test_concurrent_saves_all_succeed(self):
        warnings: list[str] = []
        with mock.patch.object(jobs.logger, "warning",
                               lambda msg, *a: warnings.append(str(msg) % a if a else msg)):
            barrier = threading.Barrier(8)

            def hammer():
                barrier.wait()
                jobs._save()

            threads = [threading.Thread(target=hammer) for _ in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        self.assertEqual(warnings, [], f"saves failed: {warnings}")
        self.assertTrue(jobs._JOBS_PATH.exists())

    def test_no_temp_files_are_left_behind(self):
        jobs._save()
        leftovers = list(jobs._JOBS_PATH.parent.glob(f"{jobs._JOBS_PATH.name}.*.tmp"))
        self.assertEqual(leftovers, [])


class MailRoutingTest(unittest.TestCase):
    """The turn that started it: a command phrased as a polite question."""

    def test_polite_mail_requests_take_the_action_path(self):
        for text in (
            "Can you check my mail?",
            "check my mail",
            "could you check the calendar",
            "check my inbox",
            "check my messages",
        ):
            with self.subTest(text=text):
                act = dialogue.classify(text)
                self.assertIs(act, Act.COMMAND)
                self.assertFalse(dialogue.is_conversational(act))

    def test_check_without_an_object_stays_conversational(self):
        """`check` is only a command verb when it has something to check."""
        act = dialogue.classify("should I check out that restaurant")
        self.assertTrue(dialogue.is_conversational(act))


if __name__ == "__main__":
    unittest.main()
