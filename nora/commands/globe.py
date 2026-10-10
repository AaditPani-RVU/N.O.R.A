"""The globe: live flights, satellite passes, earthquakes and a place's
situation, from God's Eye View (Sharp Phase H; the client is `nora.gev`).

Each command asks GEV one question with flat arguments, says its one-line
answer in NORA's words, and sends the view that shows it to the dashboard,
where the globe panel flies there. GEV's raw rows stay here: the model never
sees them, and the prompt only lists these commands when retrieval picks them.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

from nora import gev
from nora.command_engine import register

logger = logging.getLogger("nora.commands.globe")

_HERE = {"", "here", "me", "my location", "where i am", "my place", "home", "overhead", "above me",
         "over me", "near me", "around me", "around here", "nearby"}
# Flights over a city by its own boundary can miss the airport outside it;
# when a name finds none, look this far around its centre instead.
_FLIGHT_RADIUS_KM = 60
# GEV tries OpenSky's worldwide snapshot twice, 10 s each, before its
# regional fallback answers; a shorter wait gave up just before it did.
_FLIGHT_TIMEOUT_SEC = 35
# Friendly layer words -> GEV layer ids.
_LAYERS = {
    "flights": "flights", "planes": "flights", "aircraft": "flights", "air traffic": "flights",
    "satellites": "satellites", "iss": "satellites",
    "earthquakes": "earthquakes", "quakes": "earthquakes",
    "fires": "local-firms", "wildfires": "local-firms",
    "ships": "ais-live-vessels", "vessels": "ais-live-vessels", "boats": "ais-live-vessels",
    "cyclones": "weather-cyclones", "storms": "weather-cyclones", "radar": "weather-radar",
    "rain": "weather-radar", "lightning": "weather-lightning", "launches": "rocket-launches",
    "military": "military", "traffic": "traffic",
}
_POINTS = {"N": "north", "NNE": "north-northeast", "NE": "northeast", "ENE": "east-northeast",
           "E": "east", "ESE": "east-southeast", "SE": "southeast", "SSE": "south-southeast",
           "S": "south", "SSW": "south-southwest", "SW": "southwest", "WSW": "west-southwest",
           "W": "west", "WNW": "west-northwest", "NW": "northwest", "NNW": "north-northwest"}


def _speech(text: str) -> str:
    """GEV writes for a screen ("M2.5+", "133 km WNW of", "(regional feed:
    ...)"); say it the way a person would."""
    t = re.sub(r"\s*\([^)]*\)", "", text or "")
    t = re.sub(r"\bM(\d+(?:\.\d+)?)\+", r"magnitude \1 or more", t)
    t = re.sub(r"\bM(\d+(?:\.\d+)?)\b", r"magnitude \1", t)
    t = re.sub(r"(magnitude \d+(?:\.\d+)?) (\d)", r"\1, \2", t)
    t = re.sub(r"\b(" + "|".join(sorted(_POINTS, key=len, reverse=True)) + r")\b(?= of\b)",
               lambda m: _POINTS[m.group(1)], t)
    t = re.sub(r"(^|[.;:]\s+)0 (\w)", lambda m: m.group(1) + "No " + m.group(2), t)
    t = re.sub(r"\bin the last 24 hours\b", "in the last day", t)
    t = re.sub(r"; strongest\b", "; the strongest was", t)
    return re.sub(r"\s{2,}", " ", t).strip()


def _refused(e: Exception) -> str:
    m = re.match(r'No place matched "([^"]+)"', str(e))
    return f"I couldn't find a place called {m.group(1)}." if m else _speech(str(e))


def _unavailable() -> str:
    # Down, or up and waiting on its upstream (OpenSky, CelesTrak, USGS):
    # say which, since only the first is something to fix here.
    if gev.ping(timeout=1.5):
        return "The globe's live data didn't come back in time. Try again in a minute."
    return "The globe isn't running right now, so I can't look that up."


def _here() -> dict | None:
    from nora.commands.weather import _resolve
    try:
        return _resolve("")
    except Exception as e:
        logger.debug("no location for 'here': %s", e)
        return None


def _is_here(place: str) -> bool:
    return (place or "").strip().lower().rstrip("?.!") in _HERE


def _short(label: str) -> str:
    """'Bengaluru, Bangalore North, Bengaluru Urban, Karnataka, India' -> 'Bengaluru'."""
    return (label or "").split(",")[0].strip()


def _say_place(summary: str) -> str:
    """GEV names places in full ("in Greater London, England, United
    Kingdom"); say only the first part."""
    return re.sub(r"\b(in|over|around)\s+([^,.;]+)(?:,\s*[^,.;]+)+", r"\1 \2", summary)


def _show(view: dict | None) -> None:
    """Send a view to the dashboard's globe panel. Photoreal 3D (the Cesium
    ion allowance) only for a close-up; wider views use keyless imagery."""
    if not view or not isinstance(view, dict):
        return
    view = dict(view)
    view.pop("url", None)
    if not view.get("map"):
        alt = float((view.get("camera") or {}).get("altitude_m") or 0)
        view["map"] = "photoreal" if 0 < alt <= 60000 else "esri-imagery"
    try:
        from nora import ui_server
        ui_server.notify_takeover("globe", view=view, app=gev.url())
    except Exception as e:
        logger.debug("globe view not shown: %s", e)


def _area(place: str, radius_km: float | None = None) -> tuple[dict, str] | None:
    """GEV's `area` for a place, and what to call it. None when "here" can't
    be found."""
    if _is_here(place):
        here = _here()
        if not here:
            return None
        return ({"lat": here["lat"], "lon": here["lon"], "radius_km": radius_km or _FLIGHT_RADIUS_KM},
                "around you")
    return {"place": place.strip()}, ""


def _local_time(iso: str) -> str:
    """'2026-10-10T23:35:32Z' -> '5:05 AM tomorrow' in local time."""
    try:
        when = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone()
    except ValueError:
        return iso
    now = datetime.now(timezone.utc).astimezone()
    clock = when.strftime("%I:%M %p").lstrip("0")
    days = (when.date() - now.date()).days
    if days == 0:
        return f"{clock} today" if when.hour < 18 else f"{clock} tonight"
    if days == 1:
        return f"{clock} tomorrow"
    return f"{clock} on {when.strftime('%A')}"


def _direction(code: str) -> str:
    return _POINTS.get((code or "").upper(), "the horizon")


@register(
    "flights_near",
    sig="flights_near(place: str)",
    description='Live aircraft over a place, on the globe. "what\'s flying over Delhi", '
                '"planes over me", "how many flights are over London". place="" for here.',
    category="globe",
)
def flights_near(place: str = "") -> str:
    area = _area(place)
    if area is None:
        return "I don't know where you are right now."
    try:
        summary, data = gev.call("aircraft_in_area", {"area": area[0], "airborne_only": True, "limit": 50},
                                 timeout=_FLIGHT_TIMEOUT_SEC)
        if not data.get("total") and "place" in area[0]:
            cam = (data.get("view") or {}).get("camera") or {}
            if cam.get("lat") is not None:
                summary, data = gev.call("aircraft_in_area", {
                    "area": {"lat": cam["lat"], "lon": cam["lon"], "radius_km": _FLIGHT_RADIUS_KM},
                    "airborne_only": True, "limit": 50}, timeout=_FLIGHT_TIMEOUT_SEC)
                summary = f"{data.get('total', 0)} aircraft within {_FLIGHT_RADIUS_KM} km of {place.strip()}."
    except gev.Unavailable:
        return _unavailable()
    except gev.Refused as e:
        return _refused(e)
    _show(data.get("view"))
    rows = data.get("rows") or []
    said = _speech(_say_place(summary))
    if area[1] and data.get("total") is not None:
        said = f"{data['total']} aircraft {area[1]} right now."
    near = next((r for r in rows if r.get("callsign")), None)
    if near and near.get("altitude_m"):
        said += f" The nearest is {near['callsign'].strip()} at {int(round(near['altitude_m'], -2)):,} metres."
    return said


@register(
    "satellite_pass",
    sig="satellite_pass(place: str, satellite: str)",
    description='When a satellite (default the ISS) next passes over a place, and whether it\'s '
                'visible. "when does the ISS pass over me", "when can I see the space station". '
                'place="" for here.',
    category="globe",
)
def satellite_pass(place: str = "", satellite: str = "ISS") -> str:
    if _is_here(place):
        here = _here()
        if not here:
            return "I don't know where you are right now."
        location, name = {"lat": here["lat"], "lon": here["lon"]}, "you"
    else:
        location, name = {"place": place.strip()}, ""
    sat = (satellite or "ISS").strip()
    if re.fullmatch(r"(?i)(the\s+)?(iss|international space station|space station)", sat):
        sat = "ISS"
    try:
        summary, data = gev.call("next_satellite_pass", {"location": location, "satellite": sat})
    except gev.Unavailable:
        return _unavailable()
    except gev.Refused as e:
        return _refused(e)
    _show(data.get("view"))
    p = data.get("pass") or {}
    if not p.get("rise"):
        return _speech(_say_place(summary))
    who = "The ISS" if sat == "ISS" else (data.get("satellite") or sat).title()
    where = name or _short((data.get("location") or {}).get("label", "")) or place
    over = "over you" if where == "you" else f"over {where}"
    said = (f"{who} next rises {over} at {_local_time(p['rise'])}, in the {_direction(p.get('rise_direction', ''))}, "
            f"peaking at {int((p.get('max_elevation_deg') or 0) + 0.5)} degrees.")
    said += " You'll be able to see it." if p.get("visible") else " It won't be visible to the naked eye."
    return said


@register(
    "quakes_near",
    sig="quakes_near(place: str)",
    description='Recent earthquakes (last 24 hours, M2.5+) near a place, on the globe. '
                '"any earthquakes in Japan", "earthquakes near me". place="" for here.',
    category="globe",
)
def quakes_near(place: str = "") -> str:
    area = _area(place, radius_km=500)
    if area is None:
        return "I don't know where you are right now."
    try:
        summary, data = gev.call("get_earthquakes", {"area": area[0], "min_magnitude": 2.5})
    except gev.Unavailable:
        return _unavailable()
    except gev.Refused as e:
        return _refused(e)
    _show(data.get("view"))
    if area[1]:
        n = int(data.get("total") or 0)
        if not n:
            return "No earthquakes of magnitude 2.5 or more within 500 km of you in the last day."
        said = f"{n} earthquake{'s' if n != 1 else ''} of magnitude 2.5 or more within 500 km of you in the last day."
        rows = [r for r in data.get("rows") or [] if r.get("magnitude") is not None]
        if rows:
            big = max(rows, key=lambda r: r["magnitude"])
            said += f" The strongest was magnitude {big['magnitude']:.1f}, {big.get('place', 'nearby')}."
        return _speech(said)
    return _speech(_say_place(summary))


@register(
    "situation_brief",
    sig="situation_brief(place: str)",
    description='A live picture of a place from the globe: weather, quakes, fires, flights, ships, '
                'storms. "what\'s the situation in Taiwan", "brief me on the Gulf". Not for news.',
    category="globe",
)
def situation_brief(place: str = "") -> str:
    area = _area(place, radius_km=100)
    if area is None:
        return "I don't know where you are right now."
    try:
        summary, data = gev.call("situation_brief", {"area": area[0]}, timeout=_FLIGHT_TIMEOUT_SEC)
    except gev.Unavailable:
        return _unavailable()
    except gev.Refused as e:
        return _refused(e)
    _show(data.get("view"))
    sentences = re.split(r"(?<=[.!?])\s+", _speech(_say_place(summary)))
    # "Situation in X: Weather at X: ..." reads badly aloud; keep the facts.
    sentences = [re.sub(r"^Situation (?:in|around) [^:]+:\s*", "", s) for s in sentences]
    sentences = [re.sub(r"^Weather at [^:]+:\s*", "Weather: ", s) for s in sentences]
    return " ".join(s for s in sentences[:5] if s)


@register(
    "globe_show",
    sig="globe_show(place: str, layers: str)",
    description='Open the live globe on the dashboard at a place, with data layers on '
                '(comma-separated: flights, satellites, earthquakes, fires, ships, storms, radar). '
                '"show flights over Tokyo on the globe", "put earthquakes on the globe".',
    category="globe",
)
def globe_show(place: str = "", layers: str = "") -> str:
    want = []
    for word in re.split(r"[,/]|\band\b", (layers or "").lower()):
        layer = _LAYERS.get(word.strip())
        if layer and layer not in want:
            want.append(layer)
    area = _area(place, radius_km=200)
    if area is None:
        return "I don't know where you are right now."
    args: dict = {"area": area[0]}
    if want:
        args["layers"] = want
    try:
        summary, data = gev.call("show_in_gods_eye_view", args)
    except gev.Unavailable:
        return _unavailable()
    except gev.Refused as e:
        return _refused(e)
    _show(data.get("view"))
    where = "where you are" if area[1] else (place.strip() or "the world")
    on = f" with {', '.join(w.replace('local-firms', 'fires').replace('ais-live-vessels', 'ships').replace('weather-', '') for w in want)} on" if want else ""
    return f"Showing {where} on the globe{on}."
