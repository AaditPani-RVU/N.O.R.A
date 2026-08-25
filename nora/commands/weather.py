"""Weather — read it off the same API the dashboard already shows.

Before this, "what's the weather" fell through to web_search, which meant NORA
scraped a search page to describe conditions the dashboard was already
displaying live in the corner of the screen. Same provider (Open-Meteo, free
and keyless), same units the widget uses (degrees C, m/s), so the spoken answer
and the widget can't disagree.

Location resolution mirrors the dashboard too: an explicit place name is
geocoded, otherwise config `weather.home`, otherwise IP geolocation — starting
with ipapi.co, the exact call fetchWeather() makes in index.html, and falling
through to ipwho.is when ipapi rate-limits the shared IP (it does, often, and
the dashboard widget just shows ERROR when it happens).
"""
from __future__ import annotations

import logging
import time

import requests

from nora.command_engine import register
from nora.config import get_config

logger = logging.getLogger("nora.commands.weather")

# WMO weather interpretation codes, phrased for speech rather than for the
# dashboard's uppercase HUD labels ("light drizzle", not "LT DRIZZLE").
WMO_SPOKEN = {
    0: "clear", 1: "mainly clear", 2: "partly cloudy", 3: "overcast",
    45: "foggy", 48: "freezing fog",
    51: "light drizzle", 53: "drizzle", 55: "heavy drizzle",
    56: "freezing drizzle", 57: "heavy freezing drizzle",
    61: "light rain", 63: "raining", 65: "heavy rain",
    66: "freezing rain", 67: "heavy freezing rain",
    71: "light snow", 73: "snowing", 75: "heavy snow", 77: "snow grains",
    80: "rain showers", 81: "heavy rain showers", 82: "violent rain showers",
    85: "snow showers", 86: "heavy snow showers",
    95: "thunderstorms", 96: "thunderstorms with hail",
    99: "thunderstorms with heavy hail",
}

_HERE_WORDS = {"here", "my location", "current location", "outside", "today", "now",
               "local", "locally", "where i am", "my area"}

_IP_LOCATION: tuple[float, dict] | None = None
_IP_TTL = 1800.0  # the laptop does not change city mid-conversation

# Both are keyless and return lat/lon + city; ipapi.co is first only because
# that is what the dashboard widget uses, and matching it keeps the spoken
# answer and the on-screen widget pinned to the same city.
_IP_PROVIDERS = (
    ("https://ipapi.co/json/", "city", "country_name"),
    ("https://ipwho.is/", "city", "country"),
)


def _ip_location() -> dict | None:
    """Where we are, by IP. Cached — this is the slowest leg of the call."""
    global _IP_LOCATION
    if _IP_LOCATION and time.time() - _IP_LOCATION[0] < _IP_TTL:
        return _IP_LOCATION[1]
    for url, city_key, country_key in _IP_PROVIDERS:
        try:
            data = requests.get(url, timeout=6).json()
            # ipapi.co answers 200 with {"error": true, "reason": "RateLimited"}
            if data.get("error") or data.get("latitude") is None:
                logger.info("ip geolocation via %s unusable: %s", url,
                            data.get("reason") or data.get("message") or "no coords")
                continue
            place = {
                "name": data.get(city_key) or "here",
                "country": data.get(country_key) or "",
                "lat": float(data["latitude"]),
                "lon": float(data["longitude"]),
            }
            _IP_LOCATION = (time.time(), place)
            return place
        except Exception as e:
            logger.warning("ip geolocation via %s failed: %s", url, e)
    return None


def _home() -> str:
    return str((get_config().get("weather", {}) or {}).get("home", "") or "").strip()


def _resolve(location: str) -> dict | None:
    place = location.strip()
    if not place or place.lower() in _HERE_WORDS:
        # A configured home city beats IP geolocation: on a VPN or mobile
        # tether the IP answer is somewhere the user has never been. Resolved
        # here rather than by recursing, so a `home:` of "here" can't loop.
        place = _home()
        if not place or place.lower() in _HERE_WORDS:
            return _ip_location()
    # Same geocoder the globe takeover uses, so "weather in Tokyo" and
    # "tell me about Tokyo" can never land on two different Tokyos.
    from nora.commands.location import _geocode

    geo = _geocode(place)
    if not geo:
        return None
    return {
        "name": geo.get("name", place),
        "country": geo.get("country", ""),
        "lat": geo["latitude"],
        "lon": geo["longitude"],
    }


def _describe(place: dict, cur: dict, day: dict) -> str:
    name = place["name"]
    temp = round(cur["temperature_2m"])
    feels = round(cur["apparent_temperature"])
    cond = WMO_SPOKEN.get(cur.get("weather_code"), "unsettled")
    wind = cur.get("wind_speed_10m")

    where = "It's" if name == "here" else f"In {name} it's"
    parts = [f"{where} {temp} degrees and {cond}"]

    # Only mention "feels like" when it actually diverges — a spoken answer
    # that recites both numbers every time is tedious to listen to.
    if abs(feels - temp) >= 3:
        parts.append(f"feels like {feels}")
    if isinstance(wind, (int, float)) and wind >= 8:
        parts.append(f"wind {round(wind)} metres per second")
    first = ", ".join(parts) + "."

    second = ""
    if day:
        hi, lo = day.get("temperature_2m_max"), day.get("temperature_2m_min")
        rain = day.get("precipitation_probability_max")
        if hi is not None and lo is not None:
            second = f" High of {round(hi)}, low of {round(lo)}"
            # "rain N percent likely" rather than "a N percent chance of rain":
            # the article would need an a/an rule for 8, 11, 18 and the 80s,
            # and TTS reads a wrong article as a stumble.
            if isinstance(rain, (int, float)) and rain >= 25:
                second += f", rain {round(rain)} percent likely"
            second += "."
    return first + second


@register(
    "get_weather",
    sig="get_weather(location: str = '')",
    description=(
        "Current weather and today's forecast from the live weather API. "
        'Use for ANY weather question — "what\'s the weather", "is it going to '
        'rain", "how cold is it", "weather in Tokyo". Never web_search for '
        "weather. Omit location for here."
    ),
    category="web",
)
def get_weather(location: str = "") -> str:
    place = _resolve(location)
    if not place:
        return (
            f"I couldn't find a place called {location}."
            if location else
            "I couldn't work out where you are, so I can't check the weather."
        )

    try:
        resp = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": place["lat"],
                "longitude": place["lon"],
                "current": ("temperature_2m,apparent_temperature,relative_humidity_2m,"
                            "weather_code,wind_speed_10m"),
                "daily": ("temperature_2m_max,temperature_2m_min,"
                          "precipitation_probability_max"),
                "wind_speed_unit": "ms",
                "timezone": "auto",
                "forecast_days": 1,
            },
            timeout=8,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        logger.warning("weather fetch failed for %s: %s", place["name"], e)
        return "I couldn't reach the weather service just now."

    cur = data.get("current") or {}
    if "temperature_2m" not in cur:
        return "The weather service returned nothing usable."

    daily = data.get("daily") or {}
    day = {k: v[0] for k, v in daily.items() if isinstance(v, list) and v}

    answer = _describe(place, cur, day)
    logger.info("weather for %s: %s", place["name"], answer)
    return answer
