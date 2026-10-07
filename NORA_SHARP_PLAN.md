# NORA Sharp — Reliability & Speed Proposal

*Status: proposal, next in line: it runs before distributed Phases 7–9. Written
2026-10-05 against `main` @ c9c9826, from the core's journal since 2026-09-28
and a day of on-phone voice testing (ADR-0007).*

The distributed plan gave NORA a body on every device. This plan makes the
brain behind it **fast, right, and measured**: answer in about a second,
understand what was meant, and say plainly when something is broken
instead of failing quietly.

It also makes NORA **keep up**. A new model ships every couple of weeks, and
an assistant pinned to the models it launched with is stale within months.
Models become replaceable parts: NORA notices new ones, tries them against
her own eval set, and proposes a switch backed by numbers, not hype.

```
measure first · answer locally when possible · ask the model less, and with less
never fail silently · models are replaceable parts · adopt on evidence
```

Constraints carried over: free or keyless services only (no paid APIs);
the Groq budget (8k tokens/min, 200k/day) is shared with live NORA; the
core is this laptop's CPU (Kokoro runs at ~0.4x real time, so local models
must be measured before being relied on).

---

## 1. Audit: where NORA is today

From 115 real turns in the core's journal (2026-09-28 → 2026-10-05):

| Measure | Value | Note |
|---|---|---|
| Turns answered by the fast path | 54 (47%) | No model call; ~0.1–0.2 s on the core |
| Turns needing the intent model | 44 | p50 1.5 s, p90 7.6 s |
| Turns going to the chat model | 14 | p50 1.8 s, p90 9.3 s |
| Intent prompt size | 5,363 tokens on average | ~2/3 of a minute's Groq budget per turn |
| Groq "over capacity" (503) | 8 | Fallback to NVIDIA Nemotron: 3–8 s, one JSON parse failure |
| Groq rate limits | 5 | |
| Requests to a model that no longer exists | 14 | `groq/compound-mini` (live web search), every time |
| Voice: first audio, fast-path turn | ~1.0 s | Recognition alone varies 0.65–1.6 s |
| Voice: first audio, model turn | 3–9 s (20 s during a 503) | Phase 6 criterion fails on a realistic mix |

What people actually ask (top of the log): the time, phone battery, music on
the phone, reminders, "remember…", what's on today/tomorrow, calendar,
weather, notifications, directions, short chat.

### Failures found

1. **Silent breakage.** The Google Calendar token expired (`invalid_grant`);
   NORA said "I can't check your calendar" and nothing else noticed. A
   removed Groq model is still first in line for live search.
2. **Common questions miss the fast path.** "What's my battery level", "what's
   the date", "what day is it", "is my phone charging", "what's on my
   calendar tomorrow", "what's the weather" all cost a model call.
3. **Mishearing proper names.** "Pink fluid", "risk by deaf tools", "Deptones",
   "lay some pink floyd" went to Spotify as heard.
4. **Wrong routes.** "I want to go to Ladakh on a trip" opened directions.
   "Stop, what day is it" dropped the question (fixed 2026-10-05).
5. **Stale facts.** "Who is Donald Trump" was answered from model memory, out
   of date, with no lookup.
6. **Device turns were second-class.** Phone replies never reached the
   conversation history, so the chat model answered every remark with the
   time (fixed 2026-10-05). Nothing tests that every path behaves alike.
7. **Test traffic trains NORA.** After a burst of "what time is it" tests the
   routine learner offered "Based on your routine, should I get time?"
8. **No regression net for understanding.** 858 tests guard code paths; none
   replay real utterances to check where they land.
9. **Models are picked by hand and kept until they break.** The router
   already chooses per role from `config.yaml` (`llm_router.roles`), so a
   switch is one line, but nothing notices new models, tries them, or
   retires dead ones: `groq/compound-mini` was withdrawn by Groq and is
   still first in line.

---

## 2. Target

| Measure | Today | Target |
|---|---|---|
| Real turns answered without a model call | 47% | ≥ 70% |
| Voice first audio, median over 20 real-mix turns | ~3 s | ≤ 1.5 s (the Phase 6 criterion, honestly met) |
| Intent prompt | 5.4k tokens | ≤ 2k tokens |
| Model turn, p90 to first words | 7.6–9.3 s | ≤ 3 s |
| Routing accuracy on the eval set | unmeasured | ≥ 95% |
| A broken integration noticed | when the user hits it | within a day, said once, with the fix |
| A new free model evaluated | never | within a week of release, without manual work |

---

## 3. Phased plan

Each phase ships something usable, keeps every test green and adds its own.
S/M/L = relative size.

| Phase | Deliverable | Exit criteria | Size |
|---|---|---|---|
| **A — Measure** | **Eval set**: ~200 real utterances from the journal and session index (anonymised, private notification text excluded), each with its expected route (fast-path action / intent action + key params / chat). Offline runner for the fast path and router; model-dependent cases run nightly, spaced to stay inside the Groq budget. **Per-stage latency trace** on every turn (STT, hub, route, model, first `say`, first audio), pulled forward from distributed Phase 10. **`nora doctor`**: checks every model candidate, Google token, Spotify, hub and phone link; NORA mentions a failure once, with the fix. **Dev mode**: turns marked as tests skip the routine learner, memory and session index | Eval runs in CI for the offline part and nightly for the rest; a report shows accuracy and the latency of each stage; an expired Google token is reported within a day | M |
| **B — Answer locally** | Grow the fast path from the eval set's misses: phone battery and charging, date and day, calendar by day, weather, notifications, "where's my phone", volume. One table per family, so new phrasings are data, not code. Agenda and weather pre-fetched and cached on a short timer | ≥ 70% of eval-set turns are answered without a model call; no regression in routing accuracy | M |
| **C — Ask the model less, and faster** | **Tool retrieval**: send the intent model only the ~15 tools relevant to the utterance (embedding or keyword match over the registry), not all 205. **Streaming**: chat and research replies stream sentence by sentence to the device; the first sentence is spoken while the rest is generated. **Acknowledgement**: the phone says a short local "One sec" after ~600 ms of silence from the core, timed and reported separately from first words. **Health-aware routing**: skip a candidate that 404s or is over capacity until it recovers; drop dead models from config | Intent prompt ≤ 2k tokens; model-turn p90 to first words ≤ 3 s; the Phase 6 criterion passes on a realistic 20-turn mix (with and without the acknowledgement, reported separately) | L |
| **D — Hear it right** | **Name repair**: match music queries against the user's Spotify library, followed artists and recent plays (phonetic + fuzzy), so "deaf tools" → Deftones; ask when unsure. **Recogniser biasing**: pass the same names to on-device STT (`EXTRA_BIASING_STRINGS`, Android 13+). **Route repair** from eval misses: trip planning vs navigation, "stop, X" style compounds, current-facts questions ("who is", "latest", "score") go to a web lookup rather than model memory | Eval-set music queries resolve to the intended track/artist ≥ 90%; the Ladakh and Donald Trump cases route correctly; no new false positives on the rest of the set | M |
| **E — Survive the free tier** | **Budget governor**: one token bucket shared by live turns, jobs and background work; background waits, live never does. **Degraded mode**: when every model is down or rate-limited, the fast path still works and NORA says what she can't do right now instead of hanging. **Offline phone spike**: measure Gemini Nano (ML Kit GenAI) on the Pixel for basic commands when the core is unreachable; build only if it's fast and reliable | A simulated Groq outage leaves fast-path turns working and every other turn answered within 2 s with an honest "can't right now"; background jobs never cause a live-turn rate limit | M |
| **G — Keep up (continuous)** | **Scout**: a weekly job lists models from every free provider NORA can use (Groq, NVIDIA, and any other free tier added later) through their `/models` endpoints and flags new or withdrawn ones. **Trial**: new candidates run the eval set for each role (intent, chat, research, vision) in quiet hours, inside the budget governor; the report records accuracy, valid-JSON rate, latency, tokens per turn and free-tier limits. **Shadow**: a promising candidate answers a sample of live turns in parallel, without acting or speaking, and is compared with the live choice. **Promote**: when a candidate beats the current one on its role with no regression, NORA proposes the config change with the report; you approve, and rollback is one line. Withdrawn models are dropped automatically. **Model-neutral prompts**: one prompt per role, tested on every candidate; a model's quirks (hidden reasoning tokens, JSON habits) live in a small per-model adapter, not in the prompt. **Quarterly review**: beyond models, check new free capabilities (on-device STT/TTS, Android features, new free APIs) and append what's worth adopting to this plan | A model released on a used provider is evaluated within a week with no manual work; every model change in `config.yaml` points to an eval report; a deliberately bad candidate is rejected by the trial; a withdrawn model leaves the chain without a user-visible failure | M, then ongoing |
| **F — One NORA everywhere** | Every channel (laptop mic, phone voice, phone chat, Telegram, dashboard) goes through the same record-and-reply path, with a test per channel that the transcript, memory and policy see the turn alike. The transcript carries which device spoke. The routine learner needs a pattern across several days before suggesting. A phone screen to see and delete what NORA remembers | A parity test fails if any channel skips the transcript; no suggestion fires from a single session's burst; a memory can be deleted from the phone and is gone from recall | M |
| **H — The globe (plug-in, after C)** | Connect [God's Eye View](https://github.com/bilawalsidhu/gods-eye-view) (MIT, a CesiumJS globe of public live data: flights, satellites, quakes, fires, weather, cyclones, launches) as a separate service on the core; NORA's voice replaces its paid OpenAI voice. **Adapter**: about five NORA commands with flat arguments (`globe_show(place, layers)`, `flights_near(place)`, `satellite_pass(place, satellite)`, `quakes_near(place)`, `situation_brief(place)`) that call GEV over MCP, speak the one-line `summary` and never pass the 4–8 KB raw result to the model. Not a raw `mcp_servers` entry: the bridge flattens object arguments to strings, which GEV rejects, and its 30 tools would undo Phase C. **Dashboard globe**: a panel embedding `?embed=1` (allowed through `GEV_EMBED_FRAME_ANCESTORS`); the hub forwards each answer's `view` and the panel posts it as `gev:view`, so the globe flies to what NORA just said. `open_url` shows the same view on the phone, with GEV served over the tailnet (it binds to localhost by default). **Map budget**: photorealistic 3D (free Cesium ion token, personal use) only for city close-ups; world views stay on keyless Esri imagery. Optional free keys: NASA FIRMS (fires), AISStream (ships). **Upkeep**: pinned to a known-good commit, run as a systemd user service; `nora doctor` checks it answers; G's quarterly review checks for updates | "When does the ISS pass over me?", "what's flying over X?" and "earthquakes near X" are answered correctly in the eval set, and the dashboard globe shows each within 5 s; adding the adapter grows the intent prompt by ≤ 300 tokens; with GEV stopped, NORA says the globe is unavailable instead of hanging; ion use stays inside the free allowance over a week | S–M |

**Phase A, built 2026-10-07** (branch `sharp/a-measure`). `python -m nora.evals`
(254 labelled cases, private in `evals/`; offline gate in the test suite;
`nightly` via nora-evals.timer), `python -m nora.evals.latency` over the
per-turn trace, `python -m nora.doctor` (daily inside NORA, tells a new
failure once) and `python -m nora.dev on` / `"test": true` for turns that
aren't remembered. First baseline: 33% of distinct utterances answered
without a model; 86% of offline decisions right, 18 known misses tagged for
B and D; NVIDIA fallback 8/10, p50 2.3 s, p90 10.2 s, 5.5k prompt tokens.
Found on the way: `groq/compound-mini` withdrawn, Google token
`invalid_grant`, and two command modules (volume, mute, lock, screenshot)
not loading under systemd since it has no X display.

**Phase G, built 2026-10-07** (branch `sharp/g-keep-up`): `python -m nora.scout`.
Scout (weekly, free `/models` listings; first scan is a baseline), trials of
new chat models on the eval set's model cases for the intent role, a slice
a night and accumulated per case, judged against the role's first choice on
the cases both answered, proposed once on the phone, `promote` / `rollback`
editing one block of config.yaml. A model missing from two scans running
leaves the router's chains with no edit. Not yet: shadowing live turns,
per-model prompt adapters, trials for chat/research/vision (they need their
own eval cases), the quarterly review. Seen on the first night: GLM-5.3,
GLM-5.3-flash and Kimi-K3 are listed free on NVIDIA but were queued for
minutes at 22:40 IST (Kimi's first token at 180 s); a timeout or 404 counts
as unavailable, never as a wrong answer, and three such nights drop a trial.

**Phase B, built 2026-10-07** (branch `sharp/b-local`): `nora/fast_tables.py`,
one phrase table per family (date, battery, calendar and adding events,
weather, screen, email, system, globe, alarms and timers, Claude, memory
facts, what NORA can do, small talk), checked against held-out paraphrases
and near-misses as well as the eval set. Eval set: 72% answered without a
model call (was 33%), offline routing 97% right (was 86%), no regressions.
On all 527 harvested utterances, weighted by how often each was said: 58%
(was 35%); the rest is mostly open conversation. `get_weather` gained a
`day` (it only ever fetched today, so "rain tomorrow" got today's weather);
weather and today's/tomorrow's calendar are prefetched (`nora/prefetch.py`),
so their answers come from memory: weather 3.6 s → ~0, calendar 0.5 s → ~0.

**Order.** A comes first: without it, every other phase is a guess. G starts
right after A, because the eval set is what makes trying a new model safe,
and it then keeps running for as long as NORA does. B and F are independent
and small. C is the biggest win for voice. D and E can run in either order.
H comes after C, because Phase C's tool retrieval keeps the new commands
from adding to every prompt.

---

## 4. What stays unchanged

- The device protocol, pairing, policy tiers, taint and confirmation rules
  from the distributed plan.
- The model router's shape. Phases C and E add health and budget awareness
  to it; they don't replace it.
- The no-paid-APIs rule. Nothing here needs a paid service.

## 5. Open decisions

1. **Calendar auth.** Google OAuth apps left in "Testing" issue refresh
   tokens that expire after 7 days, which matches the `invalid_grant`.
   Options: publish the OAuth app (personal use, unverified warning
   accepted), or move to a calendar source that doesn't expire (an ICS
   feed, read-only). To be checked against Google's current rules before
   choosing.
2. **A local model for routing.** A small model on this CPU might classify
   intent faster than a Groq round trip, or might not (Kokoro's numbers
   suggest caution). Phase C measures before deciding; the fallback is
   tool retrieval alone.
3. **The acknowledgement.** "One sec" makes NORA feel faster, but it moves
   the goalposts. Proposed: build it, report it separately, and judge the
   Phase 6 criterion on first words of the answer.
4. **How far promotion goes on its own.** Proposed: NORA scouts, trials and
   shadows by herself, but a switch always waits for your yes. Fully
   automatic promotion could come later, once the eval set has proven it
   catches regressions.
5. **Eval privacy.** The eval set is built from real utterances. Proposed:
   keep it local and gitignored, strip names and numbers, and never include
   notification text (plan §7.9).
6. **The globe's footprint on the core.** God's Eye View is a Node server
   plus a WebGL page. A spike on 2026-10-05 showed it working: NORA's MCP
   client connected, calls answered (cold 2–10 s, warm ~0.1 s), and an
   embedded globe followed the views. Proposed: run the server always, but
   render the globe only while the dashboard is open, and measure CPU, GPU
   and ion use for a week before showing it all day.

## 6. Relation to the distributed plan

This comes before distributed Phases 7–9. Phases 0–6 already put NORA on
every device. What's left there adds new abilities, while this plan fixes
the ones she has, including the Phase 6 latency criterion, which only
Phase C can honestly meet. More features on a slow, unmeasured core would
only add more of both.

- Distributed Phase 10's per-stage latency trace moves into Phase A.
  The rest of Phase 10 (chaos tests, key rotation, fuzzing, battery)
  stays in the distributed plan.
- Phase 6 is marked verified once the Phase C exit criterion passes.
- Distributed Phases 7, 8 and 9 resume after A, G, B and C. D, E and F
  can interleave with them.
