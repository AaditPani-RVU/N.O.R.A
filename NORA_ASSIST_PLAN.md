# NORA Assist — Seven Features for the Main Assistant

*Started 2026-11-24. Built on `next/assistant` in `~/Projects/JARVIS-next`.*

Each feature is grounded in something the usage logs showed (about 1,000 audited actions,
`nora_user_model.json`, `nora_turn_trace.jsonl`). Everything is free and keyless, and nothing new
leaves the machine except what already did: Groq/NVIDIA model calls, Google News RSS, and
Claude Code via the `claude` CLI that `ask_claude` already uses.

| # | Feature | Why (from the logs) | Module |
|---|---|---|---|
| A | Personal vocabulary | "Deptones", "deaf tools", "pink fluid", "state of vermouth" (Hormuz), "Belaruss" | `nora/vocabulary.py` |
| B | Follow-through | "submit the assignment" sat overdue 51 days; it came up only when asked | `nora/follow_through.py` |
| C | Routines (Milo) | "call me in 3 minutes about feeding my dog", "remember my dog's name is Milo" | `nora/routines.py` |
| D | Story watch | Hormuz, Iran–US, the new iPhone, Indian cricket asked again and again | `nora/story_watch.py` |
| E | Spending | the phone already sees UPI and bank notifications | `nora/spending.py` + Android listener |
| F | Study mode | PE exam, CV project review, assignments | `nora/study.py` |
| G | Self-repair overnight | `add_calendar_event` failed 6/~14; "couldn't analyze the screen"; Groq JSON 400s | `nora/self_repair.py` |

New tables go in as store migrations 9+ (append only, as `nora/store.py` requires).
Background LLM work (story summaries, flashcards) uses a new `background` router role that
leads with NVIDIA, so it never spends the Groq budget live NORA runs on.

---

## A — Personal vocabulary

**Problem.** Every recogniser hears the user's own words wrong. The phone already biases Android's
recogniser with the Spotify artists (`music_names.biasing`, Sharp D). The laptop's Whisper gets no
hints at all.

**Build.**
- `vocabulary.words()` — one list built from the user's artists, saved places, contacts NORA has
  called, watched stories, study subjects, and words the user taught her. Most specific first, capped.
- The laptop transcriber passes these as Whisper's `prompt` (remote Groq endpoint) or
  `initial_prompt` (local faster-whisper). Whisper treats this as preceding context, so spellings
  in it win.
- The phone's `speech_hints` come from `vocabulary.biasing()` instead of the artists alone.
- **Taught corrections.** "You misheard, I said Hormuz, not vermouth" → `learn_word`
  stores heard→meant, and `vocabulary.repair()` rewrites that phrase (whole words, case-insensitive)
  in every later transcript before routing. "Learn the word Deftones" adds a word without a mishearing.
- Table `vocabulary(word, heard, added_at)`.

## B — Follow-through

**Problem.** Dated tasks are reported only when the user asks. Nothing ever escalates.

**Build.** A ladder per open dated task, checked every 10 minutes:

| Rung | When | How |
|---|---|---|
| 0 | due today | in the agenda and briefing (exists) |
| 1 | 1+ day overdue | a phone notification, at most once a day, after `nudge_from` (10:00) |
| 2 | 3+ days overdue | raised in the nightly check-in |
| 3 | 7+ days overdue, nudged 3+ times, still open | NORA rings the phone (`calls.ring(asked=False)`, so quiet hours hold) |

- **Nightly check-in** at `checkin_at` (21:00): what's due tomorrow, what's late, and the alarm if
  one is set. Said on the laptop when the user is home, otherwise sent to the phone. Replies are
  ordinary turns: "done with the assignment" → `close_task`; "push it to Friday" → `push_task`;
  "drop it" → `drop_task`.
- Table `nudges(task_id, rung, at)` so a restart doesn't repeat a rung.

## C — Routines (Milo)

- `log_routine("fed Milo")` from "I fed Milo" / "I walked Milo" / "gave Milo his meds".
- `last_routine("feed Milo")` → "You fed Milo at 8:10 this morning", or "Not today."
- `add_routine("feed Milo", "8am and 8pm")` → if nothing's logged by `time + grace` (45 min),
  a phone nudge; once per slot.
- Routine names are normalised by verb stem ("fed"/"feed"/"feeding" match), so the log and the
  question meet without an LLM.
- Tables `routines(name, times, grace_min, created_at)`, `routine_log(name, at, note)`.

## D — Story watch

- "Keep an eye on the Strait of Hormuz" → `watch_story`. Polls Google News RSS (the same
  `briefing._google_news` the briefing uses) every 3 hours.
- Headlines seen are remembered; only new ones count. Once 2+ are new, the `background` role writes a
  one- or two-sentence "what changed since: <last summary>" and stores it as pending.
- Pending updates are read in the next session briefing. A burst (6+ new headlines in one poll)
  goes to the phone at once, outside quiet hours.
- "What's new on Hormuz?" → `story_update` (fetches now). "Stop watching Hormuz" → `unwatch_story`.
- Table `story_watch(topic, created_at, last_check, seen JSON, summary, pending, pending_at)`.

## E — Spending

- The Android notification listener forwards a new notification as `money.notification` when it
  looks like a payment: from a payments or banking app, or with ₹/Rs/INR next to
  paid/debited/sent/spent. The phone sends the title and text only for those.
- The core parses amount, direction (debit/credit) and counterparty ("paid ₹250 to Swiggy",
  "Rs 1,200.00 debited ... to VPA swiggy@icici", "INR 500 credited") and stores **only the parsed fields**,
  deduplicated by a hash of the text plus a 10-minute bucket.
- "How much did I spend this week?" / "on Swiggy this month?" → `spending`.
- Table `spend(id, at, amount, direction, party, app, digest)`. On by default, off with
  `spending.enabled: false`.

## F — Study mode

- **Subjects and exams.** "My PE exam is on December 3rd" → `add_exam`. The session briefing gets
  "PE exam in 4 days; 12 cards to review."
- **Material in.**
  - PDFs, text and Markdown dropped in `~/NORA/study/<subject>/` (`pdftotext` for PDFs).
  - "Save this to my PE notes" after "what is this" with the phone camera: the photo's
    description/OCR from the vision answer is kept as a note.
  - "Add a PE note: …" for anything said.
- **Cards.** New or changed notes are turned into question–answer cards by the `background` role
  in one batch, at most `cards_per_run` per night, at `generate_at` (02:30). A changed file's old
  cards are retired.
- **Quiz.** "Quiz me on PE for 10 minutes" starts a session: due cards first (spaced repetition,
  SM-2-style intervals), then new ones. While a quiz is active, the next reply is taken as the answer.
  It's graded by fuzzy match first and, only when that's unsure, a ~200-token model check.
  "Skip", "I don't know", and "stop the quiz" work. It ends after the minutes asked or when
  nothing's left.
- **Study blocks.** "Plan study time for PE" finds free hours in the next 3 days of the calendar
  (09:00–22:00, around events) and, after a yes, adds "Study: PE" events.
- Tables `study_subjects`, `study_notes`, `study_cards`.

## G — Self-repair overnight

- At `run_at` (03:30), read the last 24 h of failed actions (`nora_audit_log.jsonl`) and failed
  turns (`nora_turn_trace.jsonl`). Group them by action, and pick the worst one with 2+ failures
  that hasn't been attempted in 7 days.
- Create a worktree `~/Projects/JARVIS-repair` on a fresh branch `repair/<action>-<date>` from
  `main`. Run `claude -p` there, with the failing requests and errors and the repo's rules
  (no Claude trailers, small change, add a test), restricted to read/edit tools and pytest.
  There is a 25-minute cap.
- Then run the test suite in the worktree, and record the branch, diff stat, test result and
  Claude's summary.
- The morning briefing says it: "Overnight I looked at adding calendar events, which failed 6 times.
  There's a fix on repair/… and the tests pass. Say 'show me the fix' or 'merge the fix'."
- `merge_repair` is high-risk and needs confirmation. It refuses unless the tests passed and the
  live tree is clean. It fast-forwards or merges into `main`, then restarts the NORA service
  20 seconds later. It never pushes; pushing stays a human step.
- Never touches the live tree while working. Off with `self_repair.enabled: false`.

---

## Order and status

| Phase | Status |
|---|---|
| A vocabulary | built |
| B follow-through | built |
| C routines | built |
| D story watch | built |
| E spending | built (core); APK change needs a rebuild + install |
| F study mode | built |
| G self-repair | built |

## Hands-on checks (need the user)

- A: say "play Deftones" on the laptop mic; check the log line `Transcribed (remote)` spells it right.
- B: leave a dated task overdue; check for the 10:00 notification and the 21:00 check-in.
- E: install the new APK, pay something by UPI, ask "what did I spend today".
- F: drop a PDF in `~/NORA/study/PE/`, wait for the night (or run `python -m nora.study generate`), then say "quiz me on PE".
- G: `python -m nora.self_repair --dry-run` lists what it would pick; a real run spends Claude usage.
