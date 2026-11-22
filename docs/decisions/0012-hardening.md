# ADR-0012: Hardening the link (Phase 10)

- **Date:** 2026-11-23
- **Status:** implemented; battery target **not yet measured** (needs a week of the new APK)
- **Author:** Aadit Pani (with Claude Code)
- **Scope:** core `nora/hub/{server,protocol,registry,fake_device,__main__}.py`, `nora/battery_stats.py`, `nora/commands/battery_drain.py` (new), `nora/store.py` (migration 8), `nora/triggers.py`, `nora/agent/`; Android `link/{DeviceLink,Protocol}.kt`, `phone/{KeystoreSigner,BatterySampler,LinkService,NoraApp}.kt`, `ui/MainActivity.kt`; tests

## Context

Phase 10's list: latency tracing per stage, reconnection chaos tests, key
rotation, protocol fuzzing, battery measurement, UX polish. Exit: battery drain
under 3 %/day idle, and no invocation without an audit row on both sides.
Latency tracing moved into Sharp Phase A and exists (`nora/trace.py`).

## Decision

### Key rotation

- On a session the old key already authenticated, the device sends
  `key.rotate {public_key, proof}`, the proof signed by the **new** key over
  `session_id ‖ 0 ‖ device_id ‖ "nora-v1-rotate"` — bound to the session, so it
  can't be replayed elsewhere. The core swaps the key in and answers
  `key.rotated {ok}`; from then on only the new key authenticates.
- The phone makes the next key under its own Keystore alias and keeps it
  beside the current one until the core answers (aliases can't be renamed, so
  which one is in use lives in prefs, written with `commit()`). If the answer
  never comes and the core did take it, the next connect is refused with the
  old key and the phone falls over to the next one.
- The phone rotates every 90 days on its own, or from NEW KEY on the System tab.
  The travel laptop: `python -m nora.agent rotate`.

### An audit row for every attempt

The hub recorded invocations it sent; refusals before sending (offline, kill
switch, unavailable, bad parameters, live-user and tier refusals) left no row.
They are now `status = 'refused'` with the code. The phone already audited its
own refusals; a replay returns the stored reply and its first run's row stands.

### Fuzzing and chaos (`tests/test_hub_hardening.py`)

Seeded `random` (no hypothesis in the venv), so the same thousands of frames
every run:

- `decode`, `check_manifest_entry` and `validate_params` only raise
  `ProtocolError`. This found a manifest `version` that wasn't a number raising
  `TypeError`; it's now refused.
- The live hub survives garbage before authentication and from an
  authenticated device (the socket may be closed; the hub isn't), and random
  `blob.chunk` frames never produce a photo.
- 25 overlapping reconnects leave one session with its capabilities registered
  once, dropped when it goes; a device revoked mid-storm stays out.

### Battery measurement

- The phone sends `battery.sample {level, charging, screen_on}` when the level
  moves a percent, the charger changes, or the screen goes on or off — riding
  broadcasts the phone sends anyway, no timers or wakeups of its own; the
  outbox holds them offline.
- The core keeps 30 days (`battery_samples`) and computes idle drain: intervals
  that start screen-off and unplugged and end unplugged, gaps over 36 h and
  rises left out; at least six idle hours before it gives a number.
  "How fast is my phone's battery draining?" or `python -m nora.hub battery`.

## Not done

- **UX polish** needs the phone in hand.
- **The battery target** needs a week on the new APK; then `python -m nora.hub battery`.
