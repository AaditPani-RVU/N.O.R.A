"""Dev mode: test turns that NORA doesn't learn from.

    python -m nora.dev on [--minutes 60]   every turn is a test until then
    python -m nora.dev off
    python -m nora.dev                     say whether it's on

Testing NORA means saying the same things to her many times. Kept, those
turns fill the session index ("turn 12" x51), teach the routine learner
patterns nobody has ("Saturday afternoon: click_on x5"), and crowd real
memories out of recall.

A turn is a test when its channel says so (a device sends `"test": true`
with the utterance, as `python -m nora.hub.fake_device say --test` does) or
while dev mode is on, which is how a test on the phone is marked without
changing the app. A test turn still runs, acts, is audited and keeps its
place in the short-term transcript, so follow-ups make sense; it just
writes nothing to the session index, long-term memory or the knowledge log.
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

DEV_PATH = Path(__file__).resolve().parent.parent / "nora_dev_mode.json"


def _until() -> float:
    # Read every time: normally the file is absent and this is one failed
    # stat. A cache keyed on mtime missed a rewrite within the same tick.
    try:
        return float(json.loads(DEV_PATH.read_text()).get("until", 0))
    except (OSError, ValueError, TypeError, AttributeError):
        return 0.0


def mode_on() -> bool:
    return time.time() < _until()


def is_test_turn() -> bool:
    """True while handling a test turn, or anything at all in dev mode."""
    from nora import channel
    ch = channel.current()
    return bool(ch is not None and ch.test) or mode_on()


def turn_on(minutes: float = 60.0) -> float:
    until = time.time() + minutes * 60
    DEV_PATH.write_text(json.dumps({"until": until}))
    return until


def turn_off() -> None:
    DEV_PATH.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m nora.dev", description="Turn dev mode on or off.")
    ap.add_argument("state", nargs="?", choices=["on", "off"])
    ap.add_argument("--minutes", type=float, default=60.0)
    args = ap.parse_args(argv)
    if args.state == "on":
        until = turn_on(args.minutes)
        print(f"Dev mode on until {time.strftime('%H:%M', time.localtime(until))}: "
              "turns run as usual but aren't remembered.")
    elif args.state == "off":
        turn_off()
        print("Dev mode off.")
    elif mode_on():
        print(f"Dev mode is on until {time.strftime('%H:%M', time.localtime(_until()))}.")
    else:
        print("Dev mode is off.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
