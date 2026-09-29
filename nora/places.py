"""Named places ("home", "college") and how far away they are.

Spec example 4 — "I'm going home" — needs three things the phone alone does
not have: where *home* is, how long the trip will take, and a map already
navigating. The first two live here, on the core:

  * **Saved places** are rows in the core store, so every device resolves
    "home" the same way. Set by address ("my home address is …", geocoded)
    or by standing there ("save this place as college", from the phone's fix).
  * **Geocoding** uses OpenStreetMap's Nominatim, **routing** the FOSSGIS
    OSRM servers (routing.openstreetmap.de). Both are free and keyless; the
    repo does not take on paid APIs. OSRM has no live traffic, so a car
    estimate is said as "before traffic".

Coordinates never leave the core except in those two lookups, and the
phone's position is asked for only when the user asks to go somewhere.
"""
from __future__ import annotations

import logging
import math
import re
import time
from dataclasses import dataclass

from nora import store

logger = logging.getLogger("nora.places")

_HTTP_TIMEOUT = 8.0
# Nominatim's usage policy asks for an identifying agent, not a browser's.
_USER_AGENT = "NORA/0.4 (self-hosted personal assistant)"

_DEFAULTS = {
    "geocoder_url": "https://nominatim.openstreetmap.org/search",
    "router_url": "https://routing.openstreetmap.de",
}

# travel mode → (OSRM server path, OSRM profile, spoken)
_PROFILES = {
    "driving": ("routed-car", "driving", "by car"),
    "walking": ("routed-foot", "foot", "on foot"),
    "bicycling": ("routed-bike", "bike", "by bike"),
}
MODES = ("driving", "walking", "bicycling", "transit")

_ALIASES = {
    "my home": "home", "my house": "home", "my place": "home", "house": "home",
    "the house": "home", "my flat": "home", "my apartment": "home",
    "the office": "work", "my office": "work", "office": "work", "my work": "work",
    "uni": "college", "university": "college", "my college": "college",
    "my university": "college", "campus": "college",
}


@dataclass
class Place:
    name: str
    address: str
    lat: float | None
    lon: float | None

    @property
    def located(self) -> bool:
        return self.lat is not None and self.lon is not None

    @property
    def target(self) -> str:
        """What to hand a maps app: coordinates when known, else the address."""
        return f"{self.lat:.6f},{self.lon:.6f}" if self.located else self.address


def _cfg() -> dict:
    try:
        from nora.config import get_config
        return {**_DEFAULTS, **(get_config().get("places", {}) or {})}
    except Exception:
        return dict(_DEFAULTS)


def canonical(name: str) -> str:
    n = re.sub(r"\s+", " ", name.strip().lower()).strip(" .,!?")
    n = _ALIASES.get(n, n)
    return re.sub(r"^(?:my|the)\s+", "", n)


def normalise_mode(mode: str) -> str:
    m = (mode or "").strip().lower()
    if m in ("", "car", "drive", "drive_to", "driving"):
        return "driving"
    if m in ("walk", "foot", "walking", "on foot"):
        return "walking"
    if m in ("bike", "cycle", "cycling", "bicycle", "bicycling"):
        return "bicycling"
    if m in ("bus", "metro", "train", "transit", "public transport"):
        return "transit"
    return "driving"


# ── saved places ─────────────────────────────────────────────────────────────

def save(name: str, *, address: str = "", lat: float | None = None,
         lon: float | None = None) -> Place:
    key = canonical(name)
    if not key:
        raise ValueError("a place needs a name")
    with store.transaction() as conn:
        conn.execute(
            "INSERT INTO places (name, address, lat, lon, updated_at) VALUES (?,?,?,?,?)"
            " ON CONFLICT(name) DO UPDATE SET address=excluded.address, lat=excluded.lat,"
            " lon=excluded.lon, updated_at=excluded.updated_at",
            (key, address.strip()[:300], lat, lon, time.time()))
    return Place(key, address.strip()[:300], lat, lon)


def get(name: str) -> Place | None:
    rows = store.query("SELECT * FROM places WHERE name = ?", (canonical(name),))
    if not rows:
        return None
    r = rows[0]
    return Place(r["name"], r["address"], r["lat"], r["lon"])


def listing() -> list[Place]:
    return [Place(r["name"], r["address"], r["lat"], r["lon"])
            for r in store.query("SELECT * FROM places ORDER BY name")]


def forget(name: str) -> bool:
    with store.transaction() as conn:
        return conn.execute("DELETE FROM places WHERE name = ?",
                            (canonical(name),)).rowcount == 1


# ── lookups ──────────────────────────────────────────────────────────────────

def geocode(query: str, *, near: tuple[float, float] | None = None) -> tuple[float, float, str] | None:
    """An address or place name → (lat, lon, display name), or None.

    With `near`, only matches within about 30 km of it count: "Phoenix mall"
    means the one in this city, and no answer beats one on another continent.
    """
    import requests

    query = query.strip()
    if not query:
        return None
    params: dict = {"q": query, "format": "jsonv2", "limit": 1}
    if near is not None:
        lat, lon = near
        params.update(viewbox=f"{lon - 0.3:.4f},{lat + 0.3:.4f},{lon + 0.3:.4f},{lat - 0.3:.4f}",
                      bounded=1)
    try:
        resp = requests.get(_cfg()["geocoder_url"], params=params,
                            headers={"User-Agent": _USER_AGENT}, timeout=_HTTP_TIMEOUT)
        resp.raise_for_status()
        hits = resp.json()
    except Exception as e:
        logger.warning("Geocoding %r failed: %s", query, e)
        return None
    if not hits:
        return None
    hit = hits[0]
    try:
        return float(hit["lat"]), float(hit["lon"]), str(hit.get("display_name", ""))
    except (KeyError, TypeError, ValueError):
        return None


def geocode_near(query: str, here: tuple[float, float]) -> tuple[float, float, str] | None:
    return geocode(query, near=here)


@dataclass
class Route:
    seconds: float
    metres: float
    mode: str

    def spoken(self) -> str:
        minutes = max(1, round(self.seconds / 60))
        if minutes >= 90:
            h, m = divmod(minutes, 60)
            took = f"{h} hour{'s' if h != 1 else ''}" + (f" {m} minutes" if m else "")
        else:
            took = f"{minutes} minute{'s' if minutes != 1 else ''}"
        km = self.metres / 1000
        dist = f"{km:.1f} km" if km < 10 else f"{km:.0f} km"
        how = _PROFILES[self.mode][2]
        tail = ", before traffic" if self.mode == "driving" else ""
        return f"about {took} {how} ({dist}){tail}"


def route(origin: tuple[float, float], dest: tuple[float, float],
          mode: str = "driving") -> Route | None:
    """Travel time and distance between two (lat, lon) points, or None.
    Transit has no free router, so it has no estimate."""
    import requests

    mode = normalise_mode(mode)
    if mode not in _PROFILES:
        return None
    server, profile, _ = _PROFILES[mode]
    coords = f"{origin[1]:.6f},{origin[0]:.6f};{dest[1]:.6f},{dest[0]:.6f}"
    url = f"{_cfg()['router_url'].rstrip('/')}/{server}/route/v1/{profile}/{coords}"
    try:
        resp = requests.get(url, params={"overview": "false"},
                            headers={"User-Agent": _USER_AGENT}, timeout=_HTTP_TIMEOUT)
        resp.raise_for_status()
        body = resp.json()
        best = body["routes"][0]
        return Route(float(best["duration"]), float(best["distance"]), mode)
    except Exception as e:
        logger.warning("Routing failed: %s", e)
        return None


def straight_line_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Haversine distance, for "you're already there"."""
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    h = (math.sin((lat2 - lat1) / 2) ** 2
         + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2)
    return 6371.0 * 2 * math.asin(math.sqrt(h))
