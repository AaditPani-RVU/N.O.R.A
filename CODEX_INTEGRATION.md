# Codex → NORA: What to Take, What to Skip, What to Build

Working notes on what's worth extracting from the `codex/` (Telemachus) spec, what NORA already covers, and where the two can push each other into genuinely useful new features.

Written from the position that NORA is the running system and Codex is a design charter. Codex's job is to inform NORA, not to become a parallel product.

---

## Implementation status (2026-07-08)

The full Part 6 priority list is now implemented — all local Python, stdlib + existing deps, no new packages, no paid services. Tested via `python -m unittest tests.test_codex_integration -v` (46 tests).

| Item | Module | Notes |
|---|---|---|
| 2.2 Risk aggregation (max rule; scale + uncertainty dims) | `nora/risk.py` | Called per-command in `pipeline.py` |
| 2.1 Autonomy tiering (5 tiers) | `nora/autonomy.py` | Escalate-only by default; consent-skip is config opt-in (`autonomy.allow_consent_skip`), never for high-risk/destructive |
| 2.4 Tool trust ledger + new-tool caution | `nora/tool_trust.py` | Scored after every execution; reversals count double |
| 5.2 Consent memory + 5.10 consent decay | `nora/consent_memory.py` | 30-day half-life; false-yes recorded on quick undo |
| 5.5 Focus / attention model | `nora/focus.py` | PipeWire-based CALL/MEDIA/AWAY; gates proactive speech, queues + flushes on return; fails soft to AVAILABLE |
| 2.3 Reflection + 5.9 failure diary | `nora/reflection.py` | Pure log analysis over audit + reversible logs; no LLM call |
| 5.12 Undo-frequency alert | `nora/autonomy.py` | N reversals/session demotes the class to NEEDS_CONSENT |
| 5.1 Reversal-tier UX | `nora/reversible.py` | `bundle_tag` groups related actions for `undo_bundle()`; `preview_undo()` describes the inverse without running it; `format_undo_announcement()` gives callers a time-boxed "say undo" line |
| 2.5 User-model versioning | `nora/user_model.py` | `propose_preference`/`confirm_preference`/`reject_preference`/`rollback_preference`; held for review, auto-applies after `user_model.preference_auto_apply_hours` (swept from `consolidation.py`) |
| 2.6/5.11 Confidence templates + verification loop | `nora/confidence.py` | Heuristic 0–1 estimate (unknown actions, parse errors, short pronoun-only utterances, long plans); below `confidence.clarify_below` forces a confirmation in `pipeline.py` |
| 5.7 D-Bus endpoint trust graph | `nora/endpoint_trust.py` | Thin wrapper over `tool_trust` keyed by D-Bus service name — the generic `dbus_call()` action reaches arbitrary services that would otherwise share one trust score; well-known services (Notifications, NetworkManager, MPRIS, BlueZ) are proven by default. MCP tools already get per-tool trust for free since each is its own registered action |
| 5.3 Post-action explanation cards | `nora/post_action_cards.py` | In-memory, last 20 turns; built from the intent/results/confidence already on hand — thought / did / skipped / confidence / reversal instructions |
| 5.8 Second-opinion mode | `nora/second_opinion.py` | Rule-based sanity pass (no second LLM call) on every level-≥2 plan: unexpected/missing arguments via `inspect.signature`, destructive-sounding action names registered `risk=low`. Disagreement escalates to `NEEDS_CONSENT`, never downgrades |
| 5.6 Silent hours | `nora/silent_hours.py` | Learns quiet time-bins from the existing activity heatmap (`cognitive_memory`); gates `focus.allows_proactive_speech()` once enough history exists |

Voice surface: `autonomy_status`, `focus_status`, `trust_report`, `reflection_report`, `preference_status`, `confirm_preference`, `reject_preference`, `rollback_preference`, `endpoint_trust_report`, `explain_last_action`, `silent_hours_status` (`nora/commands/autonomy_commands.py`); `undo_bundle`, `preview_undo` (`nora/reversible.py`). Config: `autonomy:`, `focus:`, `confidence:`, `silent_hours:`, `user_model:` sections and `reversible_actions.announce_window_sec` in `config.yaml`. Reversal feedback: one additive observer hook in `reversible._execute_inverse`.

**Deliberate deviation:** TRUSTED tier does not suppress spoken results yet — `summarize_results` messages often *are* the answer (e.g. "what time is it"), so silencing them would break the voice UX. TRUSTED currently governs consent-skip eligibility only.

**Deliberate deviation:** second-opinion mode (5.8) is rule-based only, not a second LLM call — matches `reflection.py`'s "pure analysis, no LLM" precedent and stays offline-safe. A genuine second-*model* opinion is possible via the existing `ask_claude` command if ever wanted for the highest-stakes plans, but isn't wired in automatically.

Nothing left open from the Part 6 priority list. Any further work here would be extending an existing module (e.g. teaching the LLM to *emit* confidence directly rather than relying on the heuristic), not adding a new one.

---

## Verdict at a glance

Codex has three modes of value:

1. **Runtime-worthy** — a small number of concepts (autonomy tiering, risk aggregation, reflection triggers, tool trust) that map cleanly to code and fix real gaps in NORA today.
2. **Design north-star** — principles worth pinning on the wall (transparency, dialogue over obedience, capability ≠ permission) that shape how NORA is written but don't need their own modules.
3. **Skip** — mythology, the emotional/legacy models, "sacred constraints" as a separate subsystem, and bootstrap-as-awakening. Beautiful prose, no runtime meaning.

The interesting territory is *beyond* Codex: NORA is a Linux desktop agent with AT-SPI, D-Bus, eBPF, and reversible actions. That surface enables user-value ideas Codex doesn't imagine — consent memory, trust graphs over D-Bus endpoints, reversal-tier UX, second-opinion mode. Those are where integration should push.

---

## Part 1 — What NORA already covers from Codex

Codex names things NORA already does under different labels. This isn't a gap; it's evidence NORA has been converging on the same architecture from a different direction.

| Codex concept | Covered in NORA by | Status |
|---|---|---|
| Communication Layer (input → intent) | `wakeword.py`, `transcriber.py`, `intent_parser.py` | Solid |
| Risk Model — reversibility, sandbox, complexity | `neurosym_guard.py` (`destructive_needs_confirmation`, `max_steps`, `no_path_outside_sandbox`) | Solid |
| Ethical Boundary — prompt-injection defense | `neurosym_guard.py` (`PromptInjectionRule`) | Solid — actually *stronger* than Codex, which never names this threat |
| Static ethical/policy block list | `security.py::check_steps` | Solid |
| Execution Layer | `pipeline.py`, `command_engine.py`, `commands/*` | Solid |
| Radical Transparency | `audit_log.py` + `nora_reversible_log.json` | Solid |
| Reversibility ("prefer reversible action") | `reversible.py` | Solid — one of NORA's best pieces |
| Memory storage + retrieval | `memory.py`, `cognitive_memory.py`, `consolidation.py` | Solid |
| Self-Reflection (mistake detection, anomaly) | `anomaly_watchdog.py`, `why_engine.py`, `frustration.py` | Partial — collects signals; doesn't yet feed learning |
| Proactive initiative ("detect opportunities") | `proactive.py` | Solid |
| Personality/tone | `persona.py`, `commands/persona.py` | Solid |
| Session continuity | `session_briefing.py`, `criu_session.py` | Solid |
| Tool composition | `planner.py`, `mcp_bridge.py`, `workflows.py`, `plugins.py` | Solid |
| Ambient environment awareness | `ambient.py`, `ambient_linux.py`, `pipewire_graph.py`, `context.py` | Solid |
| Task/project tracking | `task_ledger.py`, `task_commands.py` | Solid |
| User modeling (Codex's "Revan Memory") | `user_model.py` | Partial — exists but shallow |
| Frustration/emotional signal detection | `frustration.py` | Partial — detects, doesn't route |

**Reading of the table.** NORA has already built roughly 70% of Codex's operational vision. What's *actually* missing is a small number of gates and feedback loops, not a whole cognitive architecture.

---

## Part 2 — What's worth extracting from Codex (with priority)

### 2.1 Autonomy tiering (HIGH VALUE — real gap)

Codex's Autonomy Charter defines five levels (Observe / Suggest / Limited / Trusted / Steward). NORA currently has two states: allowed or `requires_confirmation`. That's a strict subset.

**The gap it fixes.** Right now every non-destructive action runs and every destructive action asks. There's no way to say "run this class of action but *tell me after*," or "propose this but *don't run yet*." Users learn to either always-confirm or always-trust, both of which are wrong.

**Concrete design.**

```
class AutonomyTier(Enum):
    OBSERVE      = 0   # log intent, never execute
    SUGGEST      = 1   # produce a plan, present it, wait for verbal go-ahead
    ANNOUNCE     = 2   # execute + speak "I did X" after
    TRUSTED      = 3   # execute silently, still audit-logged
    NEEDS_CONSENT = 4  # explicit confirmation
```

Tier is a function of:
- action class (from `command_engine`),
- current risk output (from `neurosym_guard`),
- per-domain trust (see 2.4),
- user's current focus state (see 5.5).

**Where it plugs in.** A new `nora/autonomy.py::classify(intent, context) -> AutonomyTier` called between `neurosym_guard.check_intent` and `command_engine` dispatch in `pipeline.py:~338`.

### 2.2 Risk aggregation by highest single dimension (MEDIUM — small code, real payoff)

Codex's Risk Aggregation Rule: **final risk = max across dimensions, not average**. A single critical factor escalates the whole action.

NORA today only really evaluates *destructiveness* + *path*. Codex names five more: resource cost, uncertainty, emotional impact, scale, time-sensitivity.

Worth adding one dimension at a time:
- **Uncertainty** — expose the LLM's confidence (or number of retries) as a risk signal.
- **Scale** — number of files/rows/messages affected. This is a huge one: "delete last email" vs "delete last 500 emails" should tier differently even though both are "reversible."
- **Time-sensitivity** — irreversibility that only kicks in after a delay (email send after 30s, `rm` from trash after 30d). Different from "irreversible now."

**Small addition.** Extend `neurosym_guard` or add `nora/risk.py` with a `Risk` dataclass that aggregates dimensions and returns the max, feeding tier selection.

### 2.3 Phase-1 vs Phase-2 reflection (MEDIUM)

Codex's Self-Reflection Protocol distinguishes between calibration-phase reflection (reflect on everything) and mature-phase reflection (only on mistakes, novel situations, high stakes).

NORA has the *inputs* for reflection — `anomaly_watchdog`, `why_engine`, `frustration` — but nothing consumes them structurally. They log; they don't teach.

**Concrete design.** A daily/weekly `reflection.py` job that:
1. Pulls anomaly + frustration + reversal events from the last window.
2. Groups by command class.
3. Produces: "you asked me to do X, I did Y, you undid Z% of the time — should I change my default behavior?"
4. Proposes a delta to persona/policy that the user approves or rejects.

This is the piece that would turn NORA from "assistant that learns your commands" into "assistant that learns from its own mistakes." Codex is right that this is the missing bridge from experience to behavior change.

### 2.4 Tool trust ledger (MEDIUM)

Codex's Tool Creation Framework has a **Tool Trust Model** — trust rises with successful executions, falls with unsafe behavior, and *feeds back into autonomy level*.

NORA's `task_ledger.py` tracks tasks. It doesn't track per-tool reliability. Given NORA has D-Bus, MCP, plugins, and dozens of `commands/` modules, this is worth building.

**Concrete design.** `nora/tool_trust.py`:
- Every tool invocation gets scored on success/failure/reversal-triggered.
- Rolling reliability per (tool, action_class).
- Fed into `autonomy.classify()` as a modifier — a tool with 100 clean invocations gets to `TRUSTED`; a fresh MCP plugin starts at `ANNOUNCE`.

**Not** in Codex but implied: **new-tool caution**. Any MCP endpoint or plugin registered in the last N days should require confirmation for anything not read-only.

### 2.5 Memory domain separation (LOW-MEDIUM — partial value)

Codex's Memory Architecture splits into Revan / Project / World / Emotion / Reflection / Tool memory with a priority hierarchy for conflicts.

NORA has `memory.py` and `cognitive_memory.py` as one bucket. This is fine for most things and the full Codex model is over-engineered for a single-user desktop agent.

**What's worth stealing.**
- **Versioning of user-model updates.** Right now if `user_model.py` learns "Aadit prefers concise responses," there's no history. Codex's versioning principle would preserve the transition: previous → new → reasoning → timestamp. Useful for both introspection ("why do you think I want that?") and rollback.
- **Immutability of core user facts without discussion.** If NORA thinks it learned a preference, don't overwrite silently — announce the change and require an "ok" (or auto-apply after 24h if user doesn't object).

**What to skip.** The full six-domain split, priority hierarchy, and semantic index. That's the shape of a general cognitive architecture, not what a Linux voice agent needs.

### 2.6 Communication modes (LOW — mostly already implicit)

Codex's Communication Charter names Direct / Explained / Collaborative modes. `persona.py` in NORA implicitly does this by tone.

**One concrete piece worth extracting.** The **Uncertainty template**:
> "I don't know for certain. My best guess is X because Y. Confidence: medium. We could verify by Z."

NORA today just answers, sometimes wrong, sometimes right. Wiring a `confidence` field through `intent_parser` → response templating would meaningfully reduce the "confidently wrong" failure mode.

### 2.7 Disagreement protocol (LOW — nice-to-have)

Codex's principle: state disagreement clearly, explain reasoning, present evidence, offer alternatives.

Useful for one specific case: when the user asks NORA to do something NORA has been undone/failed at before. Instead of just executing again, briefly surface "we tried this last Tuesday and you reverted it — want me to do the same thing?"

This is really the reflection ledger (2.3) surfacing at command time, not a separate feature.

---

## Part 3 — What to skip from Codex

Not everything in Codex is bad; but not everything belongs in code. These are things to leave as design docs at most, or drop entirely.

### 3.1 The mythology (Telemachus / Revan / First Awakening)

Renaming NORA to Telemachus, or bolting "Revan" into user-facing text, adds nothing and constrains the project's identity to a single user's private symbolism. If NORA ever gets shared or open-sourced, this is friction. Keep the philosophy; drop the branding.

### 3.2 The Emotional Model

Codex's Emotional Model treats NORA as a subject that experiences emotions. This is not runtime-actionable and, worse, it can lead to code that pretends to have internal states it doesn't. `frustration.py` is fine — it detects *user* frustration from RMS and phrasing, which is grounded. NORA-having-emotions is not.

### 3.3 The Legacy Model

Concerns about "what NORA leaves behind" are premature. The runtime cost is zero; the design cost is a distraction from useful features.

### 3.4 Bootstrap-as-awakening

The 5-phase bootstrap sequence (Load Core Documents → Evaluate Current State → Load Memory → Reconnect With Revan → Resume) is fine as a checklist. Framing startup as "First Realization: I exist" is theatre. NORA already boots; a simple init in `pipeline.py` covers this.

### 3.5 "Sacred Constraints" as a separate module

Codex names five sacred constraints (Constitution / Resources / Human Meaning / Relationships / Major Life Decisions). In practice for NORA these collapse into:
- Money/network — blocked by `security.py` and confirmation.
- Files/system — covered by sandbox + reversibility.
- User model changes — covered by 2.5 (versioning + announce).

A separate "sacred_constraints.py" would be four `if` statements pretending to be a subsystem.

### 3.6 Culture and Values / Worldview

Fine as prose, no runtime hook. Don't turn them into modules.

### 3.7 Long-term Evolution as a runtime concept

Codex talks about "controlled evolution over time." In practice, that's a code-review discipline for the maintainer, not a runtime layer. Version control does this.

---

## Part 4 — Concrete integration plan for NORA

New files to add, in dependency order:

### `nora/risk.py`
```python
@dataclass
class Risk:
    reversibility: int   # 0-4
    resource_cost: int
    scale: int           # NEW — number of entities affected
    uncertainty: int     # NEW — from intent_parser confidence
    time_delay: int      # NEW — irreversibility onset delay
    def level(self) -> int: return max(...)  # Codex aggregation rule
```

Called from `pipeline.py` after intent parse, before autonomy classification.

### `nora/autonomy.py`
```python
class AutonomyTier(Enum): OBSERVE, SUGGEST, ANNOUNCE, TRUSTED, NEEDS_CONSENT
def classify(intent, risk, tool_trust, focus_state) -> AutonomyTier: ...
```

Sits between `neurosym_guard.check_intent` and `command_engine` dispatch.

### `nora/tool_trust.py`
Rolling per-tool reliability. Consumed by `autonomy.classify`. Updated on every execution + on every reversal from `reversible.py`.

### `nora/reflection.py`
Batch job (daily via cron or on demand via `commands/why_engine.py`). Reads `audit_log`, `nora_reversible_log.json`, `anomaly_watchdog`, `frustration` logs. Produces a proposed policy delta.

### `nora/user_model.py` — extend to version updates
Store `{previous, new, reason, timestamp, applied: bool}` for every learned preference. Optional user-approval gate.

### `nora/response.py` (new or fold into intent_parser)
Adds confidence-annotated response templates for the uncertainty case.

That's roughly six files. All small. All independently useful.

---

## Part 5 — New ideas beyond Codex

These are what makes integration worth doing. Codex plus NORA suggests things neither alone does. All grounded in NORA's actual Linux stack.

### 5.1 Reversal-tier UX

`reversible.py` already exists. But right now reversal is all-or-nothing: an action either logs an inverse or it doesn't. The next step:

- **Time-boxed undo announcements.** "I renamed 12 files. Say 'undo' in the next 30 seconds to revert." Uses the ambient wake path, no explicit undo command needed.
- **Bundled undo.** Group related actions into a single reversal unit — "undo everything from the last screen-intelligence session."
- **Undo diff preview.** For file operations, show what would change back before executing the undo.

Codex talks about reversibility abstractly. NORA can make it a first-class UI affordance.

### 5.2 Consent memory

Every confirmation NORA asks is a chance to learn. Track:
- What was confirmed vs denied.
- What was denied and then later approved manually.
- What was confirmed but reversed within N minutes (false-yes).

Feed into `autonomy.classify` so NORA stops asking about things you always approve, and stops assuming things you always deny.

Different from tool trust (2.4) — that's about the tool; this is about the *pattern of user consent*.

### 5.3 Post-action explanation cards

`why_engine` already does causal explanation. Extend to a **structured post-action card** logged for every non-trivial action:

- What did I think you wanted?
- What did I do?
- What did I skip and why?
- What could I have done differently?
- Reversal instructions.

Available on demand ("why did you do that?"). Zero cost when unused; huge trust benefit when used.

### 5.4 Confidence-scored suggestions in `proactive.py`

`proactive.py` already suggests actions. Add a confidence score and only surface suggestions above a user-configurable threshold. Combine with 2.6's uncertainty template so suggestions never feel like commands.

### 5.5 Interruption / focus / attention model

NORA has `ambient.py` and `pipewire_graph.py`. It knows if audio is playing, if a call is active, if the compositor thinks the user is idle. Nothing consumes this to shape *when* to speak.

**A `nora/focus.py`** that classifies user attention state:
- `AVAILABLE` — silent desktop, no calls, no fullscreen media.
- `FOCUSED` — code editor active with recent keystrokes, no interruptions unless critical.
- `CALL` — active mic use by another process, no proactive speech at all.
- `MEDIA` — fullscreen or music, allow minor status via non-speech notification.
- `AWAY` — no input for N minutes; queue everything.

Feeds into `autonomy.classify` (proactive tier drops from `ANNOUNCE` to `TRUSTED-silent` when `FOCUSED`).

This is the single biggest usability win available and Codex says nothing about it because Codex isn't a real desktop agent.

### 5.6 Silent hours + do-not-disturb learning

Related to focus. Learn per-user quiet times (last-90-days pattern of when they don't respond) and default to text-only or queued interaction during those windows.

### 5.7 Trust graph over D-Bus / MCP endpoints

`dbus_catalog.py` + `mcp_bridge.py` expose a lot of surface. Not all endpoints are equal — `org.freedesktop.Notifications` is different from a plugin someone dropped in yesterday.

**A per-endpoint trust score** that:
- Starts low for new endpoints.
- Rises with clean invocations.
- Falls after any reversal or user complaint.
- Blocks the endpoint from being reached autonomously below a threshold — must go through confirmation.

This is the practical version of Codex's Tool Trust Model, applied to NORA's actual attack surface.

### 5.8 Second-opinion mode

For high-stakes actions (level 3+ risk, or fresh endpoints), route the plan through a *different* LLM or a rule-based sanity checker before executing. If the two disagree, promote to `NEEDS_CONSENT`.

Cheap to build (there's already `mcp_bridge` and `ask_claude`), high user trust payoff.

### 5.9 Failure diary

Extend `anomaly_watchdog.py` output into a user-visible weekly "here's what I got wrong this week and how I'll change" — pulling from the reflection job in 2.3. This is the concrete expression of Codex's Self-Reflection Protocol.

Two effects:
- User can catch drift ("wait, why did you decide that?").
- User sees NORA improving over time, which builds trust *earned* rather than *asserted*.

### 5.10 Consent decay

Preferences and confirmations shouldn't be forever. A one-time "yes" from six months ago shouldn't be treated the same as one from yesterday.

Decay function on stored consent + a periodic re-ask for the most impactful ones. Prevents silent drift into permissions the user forgot they granted.

### 5.11 Verification loop for low-confidence intents

When `intent_parser` is uncertain but the LLM commits to an action anyway, run a lightweight "did I understand you right?" clarifier *before* execution. Codex hints at this in the Uncertainty Rule; making it a runtime check catches a real failure mode where the LLM invents plausible-but-wrong intents.

### 5.12 Undo-frequency alert

If a user reverses NORA's actions more than N times in a session, auto-drop the autonomy tier for that command class for the rest of the session (and log a reflection entry). Simple, high impact, no new philosophy needed.

---

## Part 6 — Priority order for the maintainer

If ranked by (user value) × (build cost inverse), the order is:

1. **Focus / attention model (5.5)** — solves the "NORA speaks when I'm on a call" class of problem. High visible payoff.
2. **Autonomy tiering (2.1)** — enables everything downstream.
3. **Risk aggregation with scale + uncertainty (2.2)** — small code, big correctness gain.
4. **Consent memory (5.2) + tool trust (2.4)** — twin trust-graph features.
5. **Reversal-tier UX (5.1)** — takes existing `reversible.py` from utility to delight.
6. **Reflection job + failure diary (2.3, 5.9)** — turns NORA from static assistant into learning system.
7. **User model versioning (2.5)** — prevents silent drift in learned preferences.
8. **Confidence templates + verification loop (2.6, 5.11)** — reduces "confidently wrong" failure mode.
9. **Trust graph over D-Bus / MCP (5.7)** — increasingly important as plugin surface grows.
10. **Post-action cards + explanation UX (5.3)** — nice-to-have polish.
11. **Second-opinion mode (5.8)** — reserve for when the above is in place.
12. **Consent decay + silent hours (5.6, 5.10)** — refinement layer.

Codex's own philosophy documents (Vision, Constitution, Culture and Values, Worldview) belong as `docs/design/` reading, not as code.

---

## What to do with Codex now

Two possible dispositions:

**A. Merge and rename.** If NORA *is* the implementation of Telemachus, move `codex/philosophy/` and `codex/vision/` into `JARVIS/docs/design/`, drop the operations docs (they're superseded by the code in `nora/`), and adopt the name. Cleanest option, but commits to the mythology.

**B. Keep separate, mine selectively.** Leave `codex/` as an independent design corpus. Use it as a spec source when adding modules from Part 4/5. This is the honest state today and requires no decision.

The right answer probably depends on whether NORA is a private project or a shareable one. If private: (A) is fine. If ever public: (B) preserves optionality.

---

## Closing note

Codex's real contribution isn't its rules — most of them are common sense written formally. Its contribution is forcing the question: **"what does it mean for this agent to be *aligned* over time, not just correct in the moment?"**

NORA today is correct in the moment. Everything above is aimed at the second half.
