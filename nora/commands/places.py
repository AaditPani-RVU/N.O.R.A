"""Going places: "I'm going home", "save this place as college".

The phone does the parts only the phone can — where it is (`phone.get_location`)
and turn-by-turn directions (`phone.navigate`). The core does the rest: knows
where "home" is, and works out how long the trip takes (`nora.places`).
"""
from __future__ import annotations

import asyncio
import logging

from nora import places
from nora.command_engine import register
from nora.schemas import StepResult

logger = logging.getLogger("nora.commands.places")

_NAMED = ("home", "work", "college")
# Closer than this, "navigate home" means you are already there.
_ARRIVED_KM = 0.15


async def _phone(name: str, params: dict | None = None) -> StepResult:
    from nora.hub import server
    return await server.call_capability(name, params)


def _here(result: StepResult) -> tuple[float, float] | None:
    try:
        return float(result.data["lat"]), float(result.data["lon"])
    except (KeyError, TypeError, ValueError):
        return None


class _Trip:
    """Where to, where from, and how long: the shared first half of
    navigate_to and travel_time."""

    def __init__(self, label: str, spoken: str, place, here, dest, eta):
        self.label, self.spoken, self.place = label, spoken, place
        self.here, self.dest, self.eta = here, dest, eta

    @property
    def target(self) -> str:
        if self.place is not None:
            return self.place.target
        if self.dest is not None:
            return f"{self.dest[0]:.6f},{self.dest[1]:.6f}"
        return self.label

    @property
    def arrived(self) -> bool:
        return (self.here is not None and self.dest is not None
                and places.straight_line_km(self.here, self.dest) < _ARRIVED_KM)

    def eta_sentence(self) -> str:
        if self.eta is None:
            return ""
        return f"{self.spoken[:1].upper()}{self.spoken[1:]} is {self.eta.spoken()}."


async def _plan_trip(action: str, destination: str, mode: str) -> _Trip | StepResult:
    mode = places.normalise_mode(mode)
    place = places.get(destination)
    key = places.canonical(destination)
    if place is None and key in _NAMED:
        return StepResult(
            action=action, success=False,
            message=f"I don't know where {key} is yet. Tell me \"my {key} address is …\", "
                    f"or say \"save this place as {key}\" when you're there.")
    label = place.name if place else destination.strip()

    # Where the phone is, for the estimate. Best effort: directions work without it.
    here = None
    loc = await _phone("phone.get_location")
    if loc.success:
        here = _here(loc)

    loop = asyncio.get_running_loop()
    dest: tuple[float, float] | None = None
    if place is not None and place.located:
        dest = (place.lat, place.lon)
    elif place is None and here is not None:
        hit = await loop.run_in_executor(None, places.geocode_near, destination, here)
        if hit is not None:
            dest = hit[:2]

    eta = None
    if here is not None and dest is not None and places.straight_line_km(here, dest) >= _ARRIVED_KM:
        eta = await loop.run_in_executor(None, places.route, here, dest, mode)
    trip = _Trip(label, label, place, here, dest, eta)
    trip.mode = mode
    trip.location = loc
    return trip


@register(
    "navigate_to",
    sig="navigate_to(destination: str, mode: str = 'driving')",
    category="device",
    description="directions on the user's phone to a saved place (home, college) or any "
                "address, with travel time. mode: driving|walking|bicycling|transit. "
                "Use for 'I'm going home', 'take me to X'",
)
async def navigate_to(destination: str, mode: str = "driving") -> StepResult:
    trip = await _plan_trip("navigate_to", destination, mode)
    if isinstance(trip, StepResult):
        return trip
    if trip.arrived:
        return StepResult(action="navigate_to", success=True,
                          message=f"You're already at {trip.spoken}.")
    nav = await _phone("phone.navigate", {"destination": trip.target[:200], "mode": trip.mode,
                                          "label": trip.label[:60]})
    parts = [trip.eta_sentence()] if trip.eta is not None else []
    parts.append("Directions are up on your phone." if nav.success else nav.message)
    return StepResult(action="navigate_to", success=nav.success,
                      withheld=nav.withheld, error_code=nav.error_code,
                      message=" ".join(parts))


@register(
    "travel_time",
    sig="travel_time(destination: str, mode: str = 'driving')",
    category="device",
    description="how long it takes from where the user's phone is to a saved place or "
                "address, without starting directions",
)
async def travel_time(destination: str, mode: str = "driving") -> StepResult:
    trip = await _plan_trip("travel_time", destination, mode)
    if isinstance(trip, StepResult):
        return trip
    if trip.arrived:
        return StepResult(action="travel_time", success=True,
                          message=f"You're already at {trip.spoken}.")
    if trip.eta is not None:
        return StepResult(action="travel_time", success=True, message=trip.eta_sentence())
    if trip.here is None:
        return StepResult(action="travel_time", success=False, withheld=trip.location.withheld,
                          error_code=trip.location.error_code,
                          message=f"I need your phone's location for that. {trip.location.message}".strip())
    if places.normalise_mode(trip.mode) == "transit":
        return StepResult(action="travel_time", success=False,
                          message="I can't estimate public transport times; Maps can.")
    return StepResult(action="travel_time", success=False,
                      message=f"I couldn't work out a route to {trip.spoken}.")


@register(
    "save_place",
    sig="save_place(name: str, address: str = '')",
    category="device",
    description="remember a named place (home, college, gym). Without an address, "
                "saves where the user's phone is right now",
)
async def save_place(name: str, address: str = "") -> StepResult:
    key = places.canonical(name)
    if not key:
        return StepResult(action="save_place", success=False, message="What should I call it?")
    loop = asyncio.get_running_loop()
    if address.strip():
        hit = await loop.run_in_executor(None, places.geocode, address)
        if hit is None:
            places.save(key, address=address)
            return StepResult(action="save_place", success=True,
                              message=f"Saved {key} as {address.strip()}. I couldn't pin it on "
                                      f"a map, so I can give directions but not travel times.")
        places.save(key, address=address, lat=hit[0], lon=hit[1])
        return StepResult(action="save_place", success=True,
                          message=f"Saved {key}: {hit[2] or address.strip()}.")

    loc = await _phone("phone.get_location")
    here = _here(loc) if loc.success else None
    if here is None:
        return StepResult(action="save_place", success=False, withheld=loc.withheld,
                          error_code=loc.error_code,
                          message=f"I couldn't get your phone's location, so {key} isn't saved. "
                                  f"{loc.message}".strip())
    places.save(key, address=str(loc.data.get("address") or ""), lat=here[0], lon=here[1])
    where = loc.data.get("address")
    return StepResult(action="save_place", success=True,
                      message=f"Saved this spot as {key}" + (f", {where}." if where else "."))


@register("forget_place", sig="forget_place(name: str)", category="device",
          description="forget a saved place")
def forget_place(name: str) -> str:
    key = places.canonical(name)
    return f"Forgot {key}." if places.forget(key) else f"I didn't have a place called {key}."


@register("list_places", sig="list_places()", category="device",
          description="the places the user has saved (home, college …)")
def list_places() -> str:
    saved = places.listing()
    if not saved:
        return "No saved places yet."
    return "Saved places: " + ", ".join(
        f"{p.name} ({p.address})" if p.address else p.name for p in saved) + "."
