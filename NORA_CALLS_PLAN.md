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

These are covered by unit tests but haven't happened for real yet.

1. **Wake-up call, end to end.** Schedule one for two minutes out, answer it,
   and check that the whole briefing is read and that NORA keeps listening.
2. **Missed-call path.** Let one ring out twice: the retry after 5 minutes,
   the laptop notification, and the briefing arriving as text on the phone.
3. **Calendar heads-up.** Make an event 12 minutes out and wait.
4. **Calling a contact.** Needs you: a real call to someone you choose.
   "Call Mama" should now reach the approval prompt.
5. **Call NORA shortcut.** From the home screen while locked, and while unlocked.
6. **The briefing itself.** How long it runs when spoken, and whether three
   headlines is too many on a call.

## 3. Next: what can be built

### 3.1 Call screening (recommended next)

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
2. §3.1: call screening.
3. §3.2: missed calls in context.
4. §3.3: only if needed, after a decision.
