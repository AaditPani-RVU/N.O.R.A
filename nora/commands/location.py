"""Location visualization — the globe takeover for "tell me about X".

Geocodes a place name (free, no-key Open-Meteo endpoint — same provider the
dashboard's weather widget already uses), pushes it to the dashboard so the
orb can morph into a globe and drop a pin, then speaks a short blurb about
the place. The location push happens before the blurb is generated so the
globe animation starts immediately instead of waiting on the LLM call.
"""
from __future__ import annotations

import logging

import requests

from nora import ui_server
from nora.command_engine import register
from nora.conversation import for_speech

logger = logging.getLogger("nora.commands.location")


def _geocode(place: str) -> dict | None:
    try:
        resp = requests.get(
            "https://geocoding-api.open-meteo.com/v1/search",
            params={"name": place, "count": 1},
            timeout=6,
        )
        resp.raise_for_status()
        results = resp.json().get("results") or []
        if not results:
            return None
        return results[0]
    except Exception as e:
        logger.warning("geocode error for %r: %s", place, e)
        return None


def _blurb(name: str, country: str) -> str:
    from nora.model_router import complete, AllCandidatesFailed

    prompt = (
        f"Give a vivid, 2-3 sentence spoken blurb about {name}"
        + (f", {country}" if country else "")
        + ". No markdown, no URLs, no lists — this is read aloud."
    )
    try:
        text, _ = complete(
            "research",
            [{"role": "user", "content": prompt}],
            max_tokens=220,
            temperature=0.5,
        )
        return for_speech(text, max_sentences=3)
    except AllCandidatesFailed as e:
        logger.warning("location blurb failed on every candidate: %s", e)
    except Exception as e:
        logger.warning("location blurb error: %s", e)
    return f"That's {name}" + (f", {country}." if country else ".")


@register(
    "show_location",
    sig="show_location(location: str)",
    description=(
        'User wants to see/learn about a place — visualizes it on the globe '
        'AND speaks a short blurb. Use for "tell me about <place>", '
        '"show me <place>", "what\'s <place> like".'
    ),
    category="web",
)
def show_location(location: str) -> str:
    geo = _geocode(location)
    if not geo:
        return f"I couldn't find a place called {location}."

    name = geo.get("name", location)
    country = geo.get("country", "")
    ui_server.notify_location(name, country, geo["latitude"], geo["longitude"])

    return _blurb(name, country)
