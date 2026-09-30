# ADR-0006: Turn latency on Groq's free tier

- **Date:** 2026-09-30
- **Status:** implemented; checked against the live model
- **Author:** Aadit Pani (with Claude Code)
- **Scope:** `nora/intent_parser.py` (client, router, prompt), `nora/model_router.py`, `nora/commands/notifications.py`, `nora/scheduler.py`, `nora/fast_path.py`, tests

## Context

Typed chat from the phone (ADR-0005) made slow turns visible, with the
latency shown on each reply. On the first evening, single model calls took
**36–44 s**. Examples: 20:32:39 → 20:33:23, 20:33:41 → 20:34:17,
20:34:30 → 20:35:14 and 20:39:27 → 20:40:06. Others took 1–6 s.

The cause was two things together:

- **The prompt against the limit.** The intent prompt was about 6,400 tokens,
  and about 6,800 with the phone connected. Groq's free tier allows 8,000
  tokens a minute, so any two model turns inside a minute got a 429.
- **Silent retries.** The OpenAI SDK retries a 429 twice by itself, sleeping
  out Groq's `retry-after` each time. The caller saw one very slow call. The
  fallback models behind Groq were never tried, and the model router's
  cooldown never fired, because it never saw the 429.

The same evening's log showed a bug of the same kind. "Set a reminder for one
minute to …" went to the model, which sent `{"duration": "1 minute"}`.
`remind_me` takes `delay_minutes`, so the engine dropped the parameter and
the reminder was set for the 5-minute default.

## Decision

1. **No SDK retries** (`max_retries=0`) on the intent clients and in
   `model_router`. A 429 now reaches our code at once.
2. **The intent router honours rate limits.** A 429 marks that candidate as
   cooling for Groq's `retry-after` (90 s if none is given), and the next
   candidate (NVIDIA's `nemotron-3-super-120b`) answers straight away. Later
   turns skip the cooling candidate without a round trip, and a success
   clears the cooldown. The last candidate is always tried rather than
   failing flat.
3. **Each model call is logged** with its time and token counts
   (`intent: … answered in 1.2s (prompt 4051 tok, reply 137 tok)`), so this
   stays measurable.
4. **The prompt went from ~6,400 to ~4,050 tokens.**
   - It carried 60 examples, 20 of which the fast path answers before the
     model is ever asked ("open chrome", "play music", "what time is it" …).
     Several more were near-duplicates (five for "click", three for "stop").
     There are now 26 examples, one per behaviour, one line each.
   - `"requires_confirmation": false` is no longer repeated in every example;
     the schema defaults it, and the prompt says when to set it.
   - Two sections that only restated what the runtime tracks were folded
     into one short paragraph.
   - Two garbled arrows (`â†'`) from an old encoding slip are gone.
   - A test keeps it under budget and fails if an example is one the fast
     path answers.
5. **Reminders by duration are fast-pathed**, and durations are read wherever
   they come from:
   - "remind me in N minutes/hours/seconds to X", "set a reminder for N … to
     X" and "remind me to X in N …" go straight to `remind_me`, with no model
     call;
   - `remind_me` reads `duration` or `delay_minutes`, in numbers or words
     ("one minute", "90 seconds", "2 hours"), and asks instead of guessing
     when it can't ("soon");
   - replies say the time naturally ("in 90 seconds", "in 2 hours").
6. **Scheduler fix.** "In 1.5 minutes" used to miss the relative-time
   pattern and reach the clock parser, which read "1.5" as 1:05, 15 hours
   away. Decimals are now relative times.

## Effect

- **Routine turns:** 0.9–2.1 s on Groq. Measured on 8 live utterances chosen
  to cover the areas where examples or rule text were cut: places, weather,
  clicking, the CPU tracer, a clock-time reminder, a destructive rollback (it
  still asks), the screen, and a research question. All 8 routed correctly.
- **Turns that hit the per-minute limit:** these go to NVIDIA at once
  instead of waiting 40 s.
- **Daily budget:** 200k tokens a day now covers about 45 model turns, up
  from about 28. Everything the fast path answers still costs nothing.

## Blast radius & reversibility

- Prompt text only; the command set and the routing rules are unchanged.
  Revert the commit to restore the old prompt.
- A rate-limited turn is now answered by NVIDIA's model rather than waiting
  for Groq's. That model scored 6/6 on the intent prompt (ADR-0004).

## Verification

- **Python: 837 passed** (was 826). `tests/test_prompt_budget.py` covers:
  - failover on a 429, the remembered cooldown, clearing on success, the
    last candidate tried while cooling, and no SDK retries;
  - the prompt size, no fast-pathed examples, and no mojibake;
  - reminder phrasings, durations in either parameter, an unreadable
    duration, and fractional minutes.
- **Live:** 8/8 routing checks, as above.
