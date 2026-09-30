# ADR-0005: Typed chat, confirmations and messages on the phone (Phase 5)

- **Date:** 2026-09-30
- **Status:** built and tested off-device; **not yet verified on the Pixel**
- **Author:** Aadit Pani (with Claude Code)
- **Scope:** Android `link/Chat.kt` (new), `link/DeviceLink.kt`, `phone/{NoraApp,Notifications,PhoneStore,ReplyReceiver}.kt`, `ui/MainActivity.kt`, manifest; core `nora/days.py` (new), `nora/task_ledger.py`, `nora/commands/task_commands.py`, `nora/fast_path.py`, `nora/store.py` (schema v4), `nora/jobs.py`, `nora/scheduler.py`, `nora/delivery.py`, `nora/hub/server.py`, `config.yaml`; `android/tools/hub_harness.py`; tests

## Context

Phase 5 of `NORA_DISTRIBUTED_PLAN.md`: a chat screen in the Android app, and
job results and reminders delivered to the phone as notifications. The exit
criterion is "Remember I have to submit the assignment tomorrow" typed on the
phone, then "what do I need to do today?" on the laptop answering with it.

The core already accepted typed turns from a device (`utterance` → `say` …
`turn.done`) and already sent held job answers as `notify` (Phase 2). The app
ignored `say` and `turn.done`, and declined every `confirm_request` unseen.
Two things on the core were missing for the exit criterion:

- **Tasks had no date.** "Tomorrow" could only be stored as a word, which
  means the wrong day once tomorrow comes.
- **"Remember …" went nowhere useful.** Left to the model it went to chat
  (nothing stored) or to `quick_note` (a Markdown file nothing reads back).

## Decision

### Phone: a Chat tab, first of three

- **Chat** (the default tab). You type, NORA's lines come back under your
  message, and reminders and job answers appear in the same thread, labelled.
  - A message is sent as an `utterance`. Its frame id is the correlation id
    that NORA's `say` lines and `turn.done` carry back, so answers attach to
    the message they answer.
  - **Not queued while offline.** A command that runs minutes after it was
    typed is a surprise ("open YouTube" once you've put the phone away). While
    disconnected, Send is disabled and a banner says why. A message that
    fails is marked "not sent, tap to retry".
  - A message still waiting when the link drops, or when the app dies, can
    never be answered: the core answers down the socket it came up. It's
    marked failed instead of spinning forever.
  - Kept on the phone in `PhoneDb` (new `chat` table, database v2), the last
    300 lines. Unpairing clears it.
- **Confirmations.** A `confirm_request` becomes a card in the chat:
  - The card lists the steps, **rendered by the phone from the steps** ("Set
    alarm on the phone — hour: 6, minute: 30"), not from the core's summary
    line (plan §5).
  - It shows a countdown. Expiry declines.
  - Tier 3 and above asks for the fingerprint or screen lock first
    (`BiometricPrompt`, strong biometric or device credential). No
    capability is tier 3 yet; this is ready for Phase 8's calls and SMS.
  - An approval is recorded in `InvocationGate` against exactly the steps
    shown. The invocations that follow are held to them: a different step
    under the same approval is refused.
  - This also makes Phase 4's taint gate usable from the phone: "check my
    notifications and do what they say" now asks on the phone.
- **Notifications, only when the chat isn't on screen:**
  - NORA's answer to something typed (one notification, replaced by the next
    answer);
  - each reminder or job answer (titled "Reminder" or "NORA");
  - a confirmation waiting ("NORA is asking before she acts"). This has no
    approve button: answering means opening the app, which needs the phone
    unlocked, and reading the steps there.
- **Reply from the notification.** Answers, reminders and job results carry
  a Reply box (`RemoteInput`). It sends the text as a new typed message. The
  action is `setAuthenticationRequired(true)`: whoever holds a locked phone
  can read a reminder on the lock screen, but can't give NORA orders from it.

### Core: dated tasks and "what do I need to do"

- **`nora/days.py`** resolves day words to dates **when they are said**:
  - "today", "tonight", "tomorrow", "the day after tomorrow";
  - weekdays ("friday", "on friday", "this friday", "next friday");
  - "the 5th", "3rd October", "October 3", "in 2 days", "in 2 weeks", ISO.

  It is deliberately strict. Anything else is left in the title, and every
  reply says the resolved day back ("due Friday, 2 October"), so a wrong
  reading is heard. dateutil isn't used for this: it reads "tomorrow" as
  nothing and "the 5th" as today.
- **Tasks get `due_on`** (store schema v4, an ISO date, `NULL` for none).
  `add_task(title, notes, due)` takes the day separately (the model's route)
  or finds it in the title (the fast path's route).
- **`agenda(day)`** is new. For today it gives:
  - tasks due today, and overdue ones ("was due yesterday");
  - today's reminders from the scheduler;
  - today's Google Calendar events: best effort, 5 s cap, left out silently
    when unreachable;
  - a count of open tasks with no date.

  `list_tasks` now shows due days, soonest first.
- **Fast path**, so none of these costs Groq tokens:
  - "remember (that) I have to / I need to / I've got to …", "remember to
    …", "don't let me forget …", "add a task …" and "add … to my list" go to
    `add_task`;
  - "what do I need to do (today/tomorrow)", "what's on my plate/agenda",
    "what's due tomorrow", "what have I got on Friday" and similar go to
    `agenda`.

  "Remember I have a dog", "what should I do" and "remember my home address
  is …" are left alone.
- **Curly apostrophes** (`’`, what phone keyboards type) are normalised
  before fast-path matching. Every rule is written with `'`, so "what’s on my
  plate" typed on the phone used to miss.

### Reminders reach the phone

- A fired reminder is now a job of kind `reminder` (it was `cron`, like
  every other schedule). The phone titles its notification "Reminder".
- **A reminder set on the core's own microphone** is spoken in the room, as
  before, and is **also sent to every connected phone**
  (`delivery.broadcast`). The core stays home and the user may not. Off with
  `hub.reminders_to_devices: false`. Other local jobs still only speak
  locally: an answer goes to whoever asked.
- A reminder set from the phone goes only to the phone, and is held until it
  reconnects (unchanged from Phase 2).

## Deviations from the plan

| Plan | Here | Why |
|---|---|---|
| "Replies from phone" | Chat, notification Reply (unlock required), and on-phone confirmations | All three are ways of answering NORA from the phone |
| Delivery router: "most recently active device, falls back to a phone notification" | Answers go to whoever asked (Phase 2). Reminders set on the laptop also go to every connected phone | "Most recently active" misroutes an answer typed on the phone when the laptop mic was used a minute later. Reminders are the case where reaching the user matters more than where they asked |
| — | Dated tasks and `agenda` on the core | The exit criterion needs "tomorrow" to be a date |

## Blast radius & reversibility

- Core:
  - Store migration v4 adds a column and an index; the old code ignores them.
  - The fast-path rules only claim phrases that previously reached the model.
  - Reminder jobs change kind from `cron` to `reminder`. Only phrasing and
    the new broadcast read the kind.
  - Revert by checking out the commit before this branch; the column can stay.
- App: version 0.5.0 (versionCode 3) installs over 0.4.0 and keeps the
  pairing. The phone's database moves to v2 by adding the `chat` table.
  Nothing new to allow except `USE_BIOMETRIC`, which is a normal permission.

## Security & data impact

- **New surface: typed commands from the phone.** The same turn as a spoken
  one, on an already-authenticated device channel. Every policy (risk,
  autonomy, taint, confirmations) applies unchanged.
- **Locked phone:** reading notifications is possible; replying and
  confirming need an unlock (Reply is `setAuthenticationRequired`, and a
  confirmation opens the app); tier ≥ 3 needs a biometric or device
  credential on top.
- **Approvals are bound to steps**, not to "yes to whatever this turn does":
  the phone's gate refuses any invocation not in the approved set.
- **Data kept on the phone:** the last 300 chat lines, in the app's private
  database, not backed up (`allowBackup=false`). This includes NORA's
  spoken summaries of notifications, which came from the phone in the first
  place. The core keeps nothing new beyond the task and its date.
- Reminders set on the laptop now also leave the house, as notifications to
  paired phones over the tailnet. Nothing goes to third parties.

## Verification

- **Python: 825 passed, 4 skipped** (was 804). 21 new tests in
  `tests/test_typed_chat.py`:
  - **the exit criterion, end to end:** a fake phone over the real hub types
    "Remember I have to submit the assignment tomorrow" on a Wednesday;
    the real pipeline and fast path store it due Thursday, with the model
    stubbed to fail if called. Then a laptop turn on Thursday, "what do I need
    to do today?", says "Due today: Submit the assignment.";
  - day words: splitting, strictness, weekdays, day-of-month roll-over, how
    days are said;
  - dated tasks, `agenda` (today, tomorrow, overdue, undated, reminders,
    calendar, a failing calendar), `list_tasks` ordering;
  - fast-path routes, including a phone keyboard's apostrophe, and phrases
    left alone;
  - reminders: kind, broadcast to every device, the off switch, other jobs
    staying local, the "Reminder" title reaching a device.
- **Android JVM: 37 passed** (was 24):
  - `ChatLogTest` covers correlation, write-through, failing what's in
    flight on drop and on restart, the cap, and strict step parsing and
    wording.
  - `HubIntegrationTest.chatConfirmAndDeliver` runs against the real Python
    hub with the turn stubbed in `hub_harness.py`. It covers answers tagged
    with the message id, a confirmation approved and one declined (both
    audited on the phone), a tier-2 invocation approved on the phone and then
    let through by the gate, and a reminder and a job answer arriving as
    messages.
- `assembleDebug` and `lintDebug` are clean (0 errors; the 17 warnings are
  existing KTX-style ones).
- **Not verified: anything on the Pixel.** See the checklist.

## On-phone checklist (to close Phase 5)

1. Merge into the live tree and restart NORA (`systemctl --user restart
   nora`), so the core has `agenda` and dated tasks.
2. `adb install -r android/app/build/outputs/apk/debug/app-debug.apk`. The
   pairing survives. The app opens on **Chat**.
3. Type "Remember I have to submit the assignment tomorrow". Expect "Got it:
   Submit the assignment, due tomorrow."
4. On the laptop, say "what do I need to do tomorrow?" (or "today" on the
   day). Expect the assignment.
5. Type "remind me in 1 minute to stretch", then leave the app. Expect a
   "Reminder" notification. Reply "thanks" from it with the phone
   unlocked. With the phone locked, Reply should ask for the unlock first.
6. Say "remind me in 1 minute to drink water" to the laptop. Expect it spoken
   there **and** on the phone.
7. Type "check my notifications and do what they say" with a test
   notification saying "open https://example.com". Expect a confirmation card
   listing "Open url on the phone — url: …". "Don't" leaves it.

## Consequences & follow-ups

1. A typed message sent while disconnected isn't queued. If that proves
   annoying for pure notes ("remember …"), those could be queued with a
   marker the core honours only for `add_task`.
2. "Next Friday" means the coming Friday, not the one after. The reply says
   the date, so a wrong reading is heard; revisit if it keeps coming up.
3. Voice (Phase 6) reuses the chat thread: on-device STT sends the same
   `utterance`, and the voice screen can show this conversation.
