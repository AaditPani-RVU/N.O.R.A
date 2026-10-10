"""Sharp B: the fast path's phrase tables, on phrasings the eval set never saw.

The eval set is what the tables were grown from, so passing it proves little
about the next way someone says it. These are held-out paraphrases, and the
near-misses that must *not* be taken (a wrong fast answer is worse than a
slow right one).
"""
from __future__ import annotations

import pytest

from nora import command_engine, fast_path
from nora.evals.harness import on_phone, phone_connected


@pytest.fixture(scope="module", autouse=True)
def phone():
    command_engine.discover_commands()
    with phone_connected():
        yield


def _resolve(text: str, via: str = "phone"):
    with on_phone(via):
        r = fast_path.resolve(text)
    if r is None:
        return None
    return (r.steps[0].action, dict(r.steps[0].parameters)) if r.steps else ("chat", {})


@pytest.mark.parametrize("text, action, params", [
    # date
    ("what's today's date", "get_time", {}),
    ("Nora, what day is it today?", "get_time", {}),
    # battery
    ("how's my phone battery", "device.status", {}),
    ("how much battery is left on my phone", "device.status", {}),
    # calendar
    ("anything on my calendar tomorrow?", "check_calendar", {"when": "tomorrow"}),
    ("what's on my schedule for friday", "check_calendar", {"when": "friday"}),
    ("show me my calendar for this week", "calendar_week", {}),
    ("what meetings do I have tomorrow", "check_calendar", {"when": "tomorrow"}),
    # adding events
    ("schedule a meeting called standup for tomorrow at 9:30 am", "add_calendar_event",
     {"summary": "standup", "date": "tomorrow", "time": "9:30am"}),
    ("put an event on my calendar for friday at 6 pm called dinner with sam", "add_calendar_event",
     {"summary": "dinner with sam", "date": "friday", "time": "6pm"}),
    # weather
    ("how's the weather in Mumbai", "get_weather", {"location": "Mumbai"}),
    ("is it going to rain later", "get_weather", {}),
    ("will it rain tomorrow", "get_weather", {"day": "tomorrow"}),
    ("what's the weather going to be like on saturday in Pune", "get_weather", {"day": "saturday"}),
    ("how cold is it outside", "get_weather", {}),
    # screen, email, system
    ("describe my screen", "read_screen", {}),
    ("do I have any new emails", "check_email", {}),
    ("any important emails", "gmail_important", {}),
    ("why is my laptop so slow", "why_busy", {}),
    ("what's using all my bandwidth", "top_talkers", {}),
    # globe
    ("show me Kyoto", "show_location", {"location": "Kyoto"}),
    ("show me the Grand Canyon on the globe", "show_location", {"location": "the Grand Canyon"}),
    # phone alarms and timers
    ("wake me up at 7:15", "phone.set_alarm", {"hour": 7, "minute": 15}),
    ("set an alarm for 6 pm", "phone.set_alarm", {"hour": 18, "minute": 0}),
    ("start a timer for five minutes", "phone.set_timer", {"seconds": 300}),
    # memory and Claude
    ("remember that my sister's birthday is on the 12th", "inject_knowledge", {}),
    ("ask claude to review my last commit", "ask_claude", {"question": "review my last commit"}),
    # "play" heard as "lay"
    ("lay some pink floyd", "play_on_phone", {"query": "pink floyd"}),
    ("lay my workout playlist", "play_on_phone", {"kind": "playlist"}),
    # who someone is, looked up
    ("who's the prime minister of the UK", "tell_me_about", {}),
    ("who is Sundar Pichai", "tell_me_about", {}),
    ("who runs OpenAI these days", "tell_me_about", {}),
    # small talk, answered locally
    ("hi there, how's it going?", "chat", {}),
    ("sounds good", "chat", {}),
    ("what can you help me with", "chat", {}),
])
def test_held_out_phrasings(text, action, params):
    got = _resolve(text)
    assert got is not None, f"{text!r} fell through to the model"
    assert got[0] == action, f"{text!r} → {got}"
    for k, v in params.items():
        assert got[1].get(k) == v, f"{text!r} → {got}"


@pytest.mark.parametrize("text, wrong", [
    # "show me" that is not a place
    ("show me how to reverse a list", "show_location"),
    ("show me the files in downloads", "show_location"),
    ("show me directions to the airport", "show_location"),
    ("show me a picture of a cat", "show_location"),
    ("show me my notifications", "show_location"),
    # "type" that is not a typing request
    ("type of dog is milo", "type_into_focused"),
    # questions about abilities are not requests
    ("can you play playlists or only songs", "spotify_play_song"),
    ("are you able to set alarms", "phone.set_alarm"),
    # an event with no title, or an hour with no am/pm, is the model's to ask about
    ("add an event for tomorrow", "add_calendar_event"),
    ("add an event called gym tomorrow at 5", "add_calendar_event"),
    # remembering a to-do is a task, not a fact
    ("remember I have to call mom tomorrow", "inject_knowledge"),
    # acks only on their own
    ("okay play some music", "chat"),
    # "who" about NORA, the phone, or what's playing
    ("who are you", "tell_me_about"),
    ("who is calling", "tell_me_about"),
    ("who is that", "tell_me_about"),
    ("who's playing right now", "tell_me_about"),
    ("who's using the most network", "tell_me_about"),
    # "lay" that means lay
    ("lay down for a bit", "play_on_phone"),
    ("lay out my day", "play_on_phone"),
    # weather words in other senses
    ("what's the weather app called", "get_weather"),
])
def test_near_misses_are_not_taken(text, wrong):
    got = _resolve(text)
    assert got is None or got[0] != wrong, f"{text!r} wrongly → {got}"


def test_battery_said_to_the_laptop_is_the_laptops():
    assert _resolve("what's my battery level", via="laptop")[0] == "get_system_info"
    assert _resolve("what's my battery level", via="phone")[0] == "device.status"


def test_phone_only_families_step_aside_without_a_phone():
    command_engine.unregister_device("d_eval_phone")
    try:
        assert _resolve("set a timer for 10 minutes", via="laptop") is None or \
            _resolve("set a timer for 10 minutes", via="laptop")[0] != "phone.set_timer"
        assert _resolve("wake me up at 7", via="laptop") is None
    finally:
        pass
