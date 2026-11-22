# ADR-0009: Calendar rules, replies that need a yes, and the travel laptop (Phase 8c–8e)

- **Date:** 2026-11-23
- **Status:** implemented; **not yet verified on the Pixel or the travel laptop** (checklists below)
- **Author:** Aadit Pani (with Claude Code)
- **Scope:** core `nora/calendar_rules.py`, `nora/commands/wake_alarms.py`, `nora/agent/` (new), `nora/hub/{server,fake_device}.py`, `nora/store.py` (migration 7), `nora/fast_tables.py`, `nora/evals/`, `config.yaml`, `deploy/`; Android `phone/{WakeAlarms,Messaging}.kt` (new), `phone/{Calls,Notifications,NotificationCapabilities,Capabilities,KillTileService}.kt`, `link/{Chat,PhoneLogic}.kt`, `ui/MainActivity.kt`, manifest; tests

## Context

ADR-0008 built the first two slices of Phase 8 (the trigger engine, and
geofences for home). This finishes the rest except the push doorbell, which
stays deferred until a week of drop measurements says it would help.

| Slice | What | Status |
|---|---|---|
| 8a, 8b | Trigger engine; arriving and leaving home | ADR-0008 |
| **8c** | Calendar rules: leave-by alerts, the night-before alarm | this ADR |
| **8d** | Replies that need a yes: `reply_notification`, `send_sms`, `send_whatsapp`, `dnd` | this ADR |
| **8e** | Device agent for the travel laptop | this ADR |
| doorbell | FCM wake-up | deferred |

## Decision

### 8c — calendar rules (`nora/calendar_rules.py`)

A one-minute tick reads the Google calendar (through the same cached
`_events_between` the fast path uses).

**Leave by.** A timed event with a place in its location gets a travel time:

- *From* the saved place the phone's geofences last put the user inside. Never
  seen a crossing: home. Last seen leaving everywhere known: **no alert**.
  A guess from home is wrong exactly when the user is out, which is when a
  leave-by alert matters.
- *To* a saved place the location names ("College – Room 204"), else the
  address geocoded near home (Nominatim), cached two weeks, misses too.
- OSRM's time, padded × 1.3 for a drive (no traffic data), plus 10 minutes to
  spare. Five minutes before the time to leave, the phone gets "Leave by 8:24 AM
  for Lecture (9 AM at college): about 26 minutes by car with traffic", and
  the laptop says it when the user is home (not in quiet hours or on a call).
  Seen late, it says "Time to leave".
- Online meetings (a link, Zoom, Meet, Teams…) and events more than four hours
  off are skipped before any lookup. Each event alerts once
  (`calendar_alerts`), across restarts.

**The night before.** From 21:30 to 23:30, once a night: tomorrow's first timed
event, if it starts before 11:00, sets an alarm at start − travel from home −
buffer − 60 minutes to get ready, rounded down to five minutes. Nothing before
11:00 means the user's own alarm stands, and the note says so.

- The alarm lives **on the phone**, as an AlarmManager alarm clock
  (`phone.wake_alarm`, kept out of the model's tools), so it goes off with the
  link down. It rings like a call from NORA but on the **alarm stream**, so
  silent mode doesn't mute it; answering says the morning's plan; missed, it
  snoozes five minutes, twice. It reports `alarm.ended`, not `call.ended`, so
  it can't be mistaken for a call the core placed.
- Like the geofences, the core keeps the list (`wake_alarms`) and sends all of
  it on every connect; the phone also keeps a copy so a reboot re-arms it
  before the link is back.
- "Undo" (it's recorded in `nora.reversible`), "cancel tomorrow's alarm" and
  "when's my alarm" work on it.
- Phone not connected at 21:30: retried each minute until 23:30.

### 8d — replies that need a yes

| Capability | How | Tier |
|---|---|---|
| `phone.reply_notification` | The notification's own reply box (`RemoteInput`), as a watch does. Only notifications still on screen, apps hidden from NORA excluded, two matching conversations is a question | 2 |
| `phone.send_sms` | `SmsManager` from the SIM; without the permission, Messages opens with the text | 3, live user only |
| `phone.send_whatsapp` | `wa.me` with the text; the user presses send. A national number gets the SIM country's code | 2, live user only |
| `phone.dnd` | `setInterruptionFilter`; `minutes` turns it back off (an exact alarm). Notification access counts as DND access | 2 |

- The phone's confirmation prompt says each one as a sentence with the
  message quoted in full, from the parameters it will run.
- The core floors these tiers in `hub.capability_tiers`, so an app build can't
  lower them. From a job the SMS is tier 4: not run at all.
- "Turn on do not disturb for an hour" takes the fast path; "is my phone on
  do not disturb" doesn't.

### 8e — the travel laptop (`nora/agent/`)

The laptop the user carries pairs like the phone: its own P-256 key (a file,
mode 600, in `~/.config/nora-agent/`), a one-use code, approval on the core.

- It is the fake device the hub tests already drive, with real capabilities
  behind it, its own schema check (plan §7.6), and reconnects with backoff and
  jitter. Only `nora.hub.protocol` is imported: websockets, cryptography and
  jsonschema are all it needs.
- Capabilities, named `laptop.*` so nothing collides with the phone's:
  `status`, `notify`, `open_url` (web links only), `clipboard`, and one shared
  folder (`~/NORA Shared`): `shared_files`, `read_file` (untrusted text),
  `save_file` (no overwrite unless told). Absolute paths, `..`, hidden files and
  symlinks out of the folder are refused.
- Reminders, finished jobs and leave-by alerts sent to it show as desktop
  notifications. `chat` talks to NORA at the terminal and answers her
  confirmations there; `run` declines them.

## Consequences

- Leave-by alerts depend on the geofences of ADR-0008; until "home" is saved
  there is no origin, and nothing fires.
- The night-before alarm needs this APK; with the old one installed the rule
  waits (it looks for `phone.wake_alarm`) and sends nothing.
- WhatsApp's `wa.me` and Maps' buttons are other apps' behaviour, which can
  change under us.

## On-phone checklist

1. Install the new APK. Settings → Apps → NORA: allow "Alarms & reminders" if
   it isn't already (USE_EXACT_ALARM normally grants it).
2. Add a calendar event tomorrow at 9:00 with location "college" (a saved
   place). At 21:30 the phone gets "Tomorrow: …" and an alarm shows in the
   status bar; "when's my alarm" answers. "Undo" cancels it.
3. Let the alarm ring on silent: it sounds, answering says the plan, missing it
   snoozes.
4. An event an hour away with an address: the leave-by note comes five minutes
   before the time to leave.
5. With a WhatsApp message on screen: "reply to <name> on WhatsApp: on my way" →
   the prompt quotes it; approve; the reply is sent.
6. "Text <name> testing" → fingerprint → sent. "Turn on do not disturb for 5
   minutes" → approve → DND on, and off again after five minutes.

## Travel-laptop checklist

1. `pip install -r deploy/requirements-agent.txt` in a checkout.
2. `python -m nora.hub pair` on the core; `python -m nora.agent pair wss://<core>.ts.net/v1/device <code>`;
   `python -m nora.hub approve <id>`.
3. `python -m nora.agent chat`: "what's on my laptop's shared folder", "open
   example.com on my laptop", a reminder set from the phone shows on the laptop.
4. Optionally `deploy/systemd/nora-agent.service` to keep it running.
