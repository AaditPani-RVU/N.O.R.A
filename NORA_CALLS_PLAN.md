# NORA Calls — What She Can Do on the Phone, Exactly

*Status: plan. Written 2026-10-17 against `main` @ 7044af2, after the first day
of calls (58acb37 → 7044af2). It lists what's built, what still needs checking
on the Pixel, what comes next, and what can't be done, with the reason for each.*

Calls make NORA something that **reaches you**, not only something you ask.
Everything here runs on what NORA already has: the link between the core and
the phone app, the phone's own SIM, and free services.

```
ring only when it matters · never call someone else without a yes on the phone
nothing she called about is lost · free, on the phone you already have
```

Constraints carried over: no paid APIs (so no Twilio or other telephony
service); the Groq budget (8k tokens/min, 200k/day) is shared with live NORA;
Android decides what a normal app may do during a call (see §4).

---

## 1. Built (on `main`, live since 2026-10-17)

| What | How you use it | Where |
|---|---|---|
| NORA rings you | Asked directly, or "call me in 10 minutes about the laundry" | `phone.call`, `remind_me(ring=true)` |
| Wake-up calls | "Call me every day at 7 with my briefing", "wake me up every weekday at 6:30 with my briefing" | `schedule_task(ring=true)`, `morning_briefing` |
| Calendar heads-up | Rings 10 minutes before a timed event, once per event | `nora/calls.py` watcher |
| Long answers | A background answer that took over 3 minutes rings when it lands | `jobs._worth_a_call` |
| Screen watches | "Watch for the training to finish" rings when it does | `watch_for` |
| Missed-call retry | Missed: one more try 5 minutes later. Missed again: laptop notification, and a long message (briefing, answer) arrives on the phone as text. Declined isn't retried | `calls.ended` |
| Quiet hours | Calls nobody asked for wait between 23:00 and 07:00 and arrive as notifications. Calls you asked for ring at any hour | `calls.quiet_hours` |
| Hang up by voice | "Hang up", "you can cut the call" during a call | `HangUp` in `Voice.kt` |
| Calling people | "Call Mom", "call 98450 12345". Approved on the phone first ("Phone call to Mom from your SIM"). Two matching people means she asks which | `phone.call_contact` (tier 2, live user only) |
| Calling NORA | "Call NORA" shortcut: long-press the app icon, or System → Phone access → *Put Call NORA on the home screen* | `CallNora` in `Triggers.kt` |
| Retrying after a fix | Asking again after a failure re-runs the action instead of repeating the old error | intent prompt rule (7044af2) |

Settings live under `calls:` in `config.yaml`.

## 2. Still to check on the Pixel

Checked on 2026-10-17 evening, except where marked open.

1. **Wake-up call, end to end.** Done. Rang at 22:02, answered; the
   briefing was read (~40 s) and NORA listened after it.
2. **Missed-call path.** Done. Missed at 22:06, retried at 22:11, missed
   again: the whole briefing arrived on the phone as a "Missed call from
   NORA" notification, and the laptop got one too (Clavis shows it for 7 s,
   so it's easy to miss; it stays in the sidebar). The ringing screen used
   to show the briefing's first sentence ("Good evening."); it now says what
   was scheduled ("Morning briefing"), 4995d55.
3. **Calendar heads-up.** Half done. The watcher found the 21:30 event at
   21:20, but the phone had been off the tailnet since 16:49 (Tailscale had
   stopped), so it fell back to a notification. Open: the ring itself. The
   laptop now says when the phone stays offline 10 minutes (ff2a192).
4. **Calling a contact.** Done. "Call Mama" reached `phone.call_contact` and
   its approval prompt (16:43).
5. **Call NORA shortcut.** Open. Needs you: locked and unlocked.
6. **The briefing itself.** ~40 s when spoken on a call. Open: whether three
   headlines is too many.

## 3. Next: what can be built

### 3.1 Call screening — built 2026-10-17 (5feda4e, c50a5ca)

**Status.** On `main` and on the Pixel. NORA holds the call-screening role;
Google Phone keeps its own spam filter (it screens as the default dialer,
a separate step, and telecom lists both). Default mode is *ring*: unknown
callers ring as before and are only logged. *Silence unknown numbers* (System
tab) stops the ring and leaves a notification. "Who called", "block the last
number that called" and "call that number back" are fast-path rules, and the
briefing counts unknown callers from the last 12 hours. Built differently
from the sketch below: *announce* is just *ring* (Google Phone already shows
the number, and there's no free caller-ID lookup), and blocking is by voice
only, with "unblock all" on the phone. Open: one real call from a number not
in the contacts, to see it logged end to end.


Android lets one app hold the *call screening* role (`CallScreeningService`).
The phone asks that app about each incoming call **from a number not in your
contacts**, before it rings. The app can let the call through, silence it, or
reject it. It cannot talk to the caller or hear them.

What NORA would do with that:

- **Unknown numbers:** you choose per mode. *Announce* rings normally, and
  NORA says or shows who it might be. *Silence* doesn't ring, and you get
  "Unknown number +91… called at 4:12, silenced". *Reject* is for numbers
  you've told her to block.
- **Contacts always ring.** Android never sends calls from contacts to a
  screening app, so a call from someone you know can't be silenced by mistake.
- **"Who called?"** Screened calls are logged on the core, so "who called while
  I was out" and "call that number back" work (the callback goes through
  `phone.call_contact`, approved as usual).
- **Blocking by voice:** "block the last number that called" adds it to a list
  kept on the phone.
- **In the briefing:** "Two unknown numbers called overnight."

Work: a `CallScreeningService` in the app, a request for the screening role
(`RoleManager.ROLE_CALL_SCREENING`, one system prompt), a mode switch in the
System tab, a `call.screened` event to the core, and three commands
(`who_called`, `block_number`, call-back by "that number").

Risk to check first: holding the role may displace Google Phone's own spam
filter on the Pixel. Test with the role on and off before relying on it.

### 3.2 Missed calls and texts in context

When the phone shows a missed call from a contact, NORA can mention it
("Mom called twice while you were in the meeting") and offer to call back.
This uses the notification access NORA already has, so it adds no new permission.

### 3.3 Answering with headphones or in the car (needs a decision)

Registering NORA with Android as a self-managed calling app
(`ConnectionService`) makes her calls look like real calls to the phone.
Headphone buttons and car head units can answer them, and they use call
audio. It's the riskiest change: call audio could clash with how NORA
listens, and it touches the voice pipeline that works today. Build it only
if answering from headphones or the car turns out to matter in daily use.

## 4. Can't be done (and why)

| Idea | Why not |
|---|---|
| NORA talking to a caller on your behalf (like Google's Call Screen, Hold for Me) | Android only lets apps built into the phone's system hear the other person or speak into a phone call. An installed app can't, on any phone. Google's features rely on that system access. |
| NORA joining a call on speakerphone | Technically possible (she hears through the mic and talks through the speaker), but there'd be echo, the other person hears your room, and it would break easily. Not worth building. |
| An AI phone agent that places and handles calls itself | Needs a paid telephony service, which breaks the free-only rule. In many places an AI talking to someone, or recording them, also has to say so up front. |

## 5. Order

1. §2: check everything already built, on the phone.
2. §3.1: call screening (built; one real call to check).
3. §3.2: missed calls in context.
4. §3.3: only if needed, after a decision.
