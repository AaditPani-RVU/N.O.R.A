# ADR-0008: Triggers and geofences (Phase 8, first slice)

- **Date:** 2026-11-21
- **Status:** implemented; **not yet verified on the Pixel** (needs "home" saved and a real trip out)
- **Author:** Aadit Pani (with Claude Code)
- **Scope:** core `nora/triggers.py` (new), `nora/hub/server.py`, `nora/places.py`, `nora/store.py` (migration 6), `nora/wiring.py`, `nora/commands/system_control.py`, `config.yaml`; Android `phone/Geofences.kt` (new), `phone/Capabilities.kt`, manifest, `build.gradle.kts`; tests

## Context

Phase 8 of `NORA_DISTRIBUTED_PLAN.md` is events, triggers and always-on. It is
large, so it is built in slices:

| Slice | What | Status |
|---|---|---|
| **8a** | Event bus and trigger engine on the core | this ADR |
| **8b** | Geofences on the phone; arriving and leaving home | this ADR |
| 8c | Calendar rules: leave-by alerts, the night-before alarm | ADR-0009 |
| 8d | Replies that need a yes: `reply_notification`, `send_sms`, `send_whatsapp`, `dnd` | ADR-0009 |
| 8e | Device agent for the travel laptop | ADR-0009 |

The push "doorbell" (FCM / UnifiedPush) is **deferred**. `LinkService` is a
foreground service that keeps the socket open; on 2026-11-21 the phone
dropped three times in 24 h, and the one long outage was Tailscale stopping,
which a doorbell would not have fixed. Measure for a week first.

NORA's existing proactive engine (`nora/proactive.py`) asked "Based on your
routine, should I show location?" built from action names. The logs show it
asked that 19 times on Friday evenings. The user chose to replace it.

## Decision

### Rules act, and say what they did

The user chose **act, offer undo** over asking first or notifying only. A rule
does low-risk things itself, tells the user what it did, and records anything
that can be put back in `nora.reversible`, so "undo" works on it like on any
command. `proactive.enabled` is now false.

### Events

The hub hands every device event to `triggers.emit` with the phone's own
`occurred_at`. One worker thread runs the rules, so a rule that locks the
screen or asks the phone something never holds up the socket.

### Geofences: the phone watches, the core keeps the list

- The saved places (`nora.places`) become Play services geofences on the
  phone (`phone.set_geofences`, 150 m, enter and exit, two-minute
  responsiveness). The phone reports `geofence.transition`; its position
  never streams to the core.
- The core sends the whole list on every connect and whenever a place is
  saved or forgotten, so a reboot or reinstall (which clears Android's
  fences) heals itself and the phone keeps no state of its own.
- `phone.set_geofences` is kept **out of the model's tool list**
  (`triggers.INTERNAL_CAPABILITIES`): which places are watched follows the
  saved places, never a turn's say-so.
- No initial trigger, so setting the fences at home doesn't read as arriving.
  The phone therefore never says which side of a new fence it is on, so the
  core assumes it: a place saved from the phone's location is "inside", and a
  place moved by address or forgotten goes back to unknown. Otherwise a stale
  "outside" turns the first real exit into a repeat, and leaving does nothing.
- Needs location "Allow all the time". Without it the phone says where the
  setting is, once per run.

### Being sure a crossing is real

`place_presence` (store migration 6) keeps inside/outside per place.

- A repeat, or a crossing older than the one already known, is ignored.
- A crossing more than 10 min old (it waited in the outbox while the phone
  was offline) updates the state but **acts on nothing**: an hour-old
  "left home" must not lock the laptop now.
- A crossing within 3 min of the last one is GPS drift at the fence's edge;
  it updates the state only.

### Home

| | Leaving | Arriving |
|---|---|---|
| Laptop | Lock the screen (Clavis IPC, then `loginctl`); pause Spotify (undo resumes) | Say welcome, how long you were out, missed calls, what's due; then resume Spotify (undo pauses) |
| Phone | Notification: what was done, battery under 30 % and not charging, anything due or overdue | The same welcome as a notification |
| Quiet hours (23:00–07:00) | as above | No speech, no music; the notification still comes |

The welcome is said even though focus reads "away" (arriving is exactly when
it would): only quiet hours and a call in progress keep the laptop silent.
Every part is a switch under `triggers.home`.

`lock_screen` called Windows' `LockWorkStation` and failed on the Linux core;
it now uses Clavis's lock, then `loginctl lock-session`.

## Consequences

- Saying "save this place as home" on the phone, while at home, is the one
  setup step. Until a place has coordinates there is nothing to watch.
- A rule can't be tested at a desk: the phone has to cross the fence. Tests
  cover the rules with fake events and the hub path with the fake device.

## On-phone checklist

1. Location "Allow all the time" for NORA.
2. "Save this place as home" on the phone; the log shows `watching 1 place(s)`.
3. Leave home with Spotify playing on the laptop: within a few minutes the
   laptop locks, Spotify pauses, and the phone gets the leaving note.
4. Come back: the laptop says welcome, then Spotify resumes; "undo" pauses it.
