# NORA — Consolidated Deliverables
_Joint analysis: Claude (Sonnet 4.6) + Opus 4 + Codex. Debate-synthesized on 2026-05-14._

Each deliverable is marked with the source that originated it: **(O)** = Opus, **(C)** = Codex, **(S)** = synthesis of both.

---

## The Single Biggest Gap

**Persistent, structured cognition** — NORA has memory (ChromaDB) and patterns (bigrams) but no concept of *ongoing work* or a *user model that sharpens over time*. Every session partially starts cold. The graduation moment is when NORA briefs you on what crashed overnight, finds the relevant fix from two weeks ago, and applies it. That requires layered cognition: a Task Ledger (immediate), a Daily Brain (strategic), and an Evaluation Harness (operational safety net). Every other deliverable lands harder once this foundation exists.

---

## Sprint 1 — Feel (Perception Layer)
_Ship these first. They change what NORA feels like before any new capability lands._

| # | Name | What it is | Why it matters | Complexity | Priority |
|---|------|-----------|----------------|------------|----------|
| 1 | **Sub-300ms First Phoneme** (O) | Begin synthesizing a short ack token ("mm", "sure", "one sec") the moment VAD detects end-of-speech, before Whisper or the LLM finishes. Barge-in monitor runs in parallel so the user can cut NORA off without artifacts. | Current pipeline waits for STT+LLM+TTS. A 250ms verbal ack is the single biggest perceived-intelligence win — humans only sound alive when they respond before they've finished thinking. | Medium | P1 |
| 2 | **Prosody-Aware TTS Director** (O) | Small classifier in front of `speaker.py` that picks SSML rate/pitch/pauses based on action class: status report vs. error vs. casual chat vs. urgent warning vs. confirmation request. Errors get slower pacing; acks get clipped; confirmations get rising inflection. | NORA currently has one voice mood. Real assistants modulate. This is 80% of the "JARVIS feel" and is a frontend-only change. | Low | P1 |
| 3 | **Wakeword Detection** (C) | Replace PTT as primary activation with always-on wakeword detection via openWakeWord. User-trainable phrase — "Hey NORA" by default, enrollable to any phrase. PTT remains as fallback. | PTT breaks flow when both hands are on a keyboard. Wakeword activation makes NORA feel ambient rather than peripheral. This is the interface shift. | Medium | P1 |
| 4 | **Embodied Status Surface** (O) | Replace the static web dashboard text log with an ambient waveform/orb that breathes when idle, pulses on listen, lights up per pipeline stage (STT→guard→LLM→guard→action), shows a thinking-trace bubble. Mirrors to a system-tray icon. | A glanceable embodiment is what makes users trust an always-on agent. NORA is currently invisible until it speaks. | Medium | P2 |
| 5 | **Continuous Barge-In** (O) | Lightweight always-on VAD that instantly suppresses TTS when the user starts speaking mid-response. Also emits soft "mhm / go on" during long user utterances in free-form mode. | Removes the walkie-talkie feel entirely. Required before PTT can ever be retired as default. | Medium | P2 |

---

## Sprint 2 — Know (Durable Cognition)
_The memory layer that turns logs into a living model of you._

| # | Name | What it is | Why it matters | Complexity | Priority |
|---|------|-----------|----------------|------------|----------|
| 6 | **Task Ledger** (C) | Persistent, structured store of open and closed tasks — title, status, associated files/commands, freeform log. NORA creates, updates, and closes entries on voice command and auto-populates from session context. "NORA, what was I working on last night?" becomes a real query. | Highest-leverage immediate capability. Users stop losing context across sessions. Foundation for Goal Decomposition and Daily Brain. | Medium | P1 |
| 7 | **Session Replay Briefing** (C) | On first wake of a new session, NORA delivers a 3–5 sentence spoken briefing: what was done, what is open, any anomalies (errors hit, commands that failed). Sourced from the Task Ledger + episode log. | Removes the "what was I doing?" tax at the start of every work block. For ML researchers running overnight jobs, immediately indispensable. | Low | P1 |
| 8 | **Nightly Consolidation Job** (O) | Scheduled task at idle/EOD that scans the day's episodes + screen OCR snippets + git activity + window-focus log and produces a structured `daily_brief.json`: active projects, dominant apps, frustration spikes, completed vs. abandoned tasks. Stored in ChromaDB as high-priority retrieval. | Converts raw logs into structured self-knowledge. Becomes the always-on context every future plan conditions on. Foundation for User Model. | Medium | P1 |
| 9 | **User Model Layer** (O) | `nora/user_model.py` with typed accessors: `current_projects()`, `peak_hours()`, `recent_frustrations()`, `stale_threads()`, `preferred_terminology()`. Auto-injected into system prompt as a compact 200-token user card. Fed by Nightly Consolidation. | Lets NORA say "you haven't touched that repo in 4 days — open it?" instead of generic suggestions. Differentiates from every cloud assistant that forgets you between sessions. | Medium | P1 |
| 10 | **Memory Hygiene & Forgetting Curve** (O) | Background scorer that decays low-utility episodes, deduplicates near-identical ones, promotes high-reuse episodes to "core memory", exposes a `/memory` review UI to redact or pin entries. | ChromaDB retrieval quality collapses past ~10k episodes without curation. Also a trust requirement — users need to see and control what NORA remembers. | Medium | P2 |
| 11 | **Goal Decomposition Engine** (C) | User states a high-level goal ("get the transformer fine-tune pipeline working"). NORA breaks it into trackable subtasks, stores them in the Task Ledger, surfaces the next unfinished step proactively. | Moves NORA from reactive command executor to active work partner. No voice assistant does this well today. | High | P2 |
| 12 | **Weekly Self-Review Letter** (O+C) | Every Sunday, NORA generates a 200-word spoken letter: what shipped, what stalled, the manual workflow it noticed, one experiment to try next week. Delivered on first wake Monday. Daily version at 6 PM is a standup with yourself. | Ritual creates habit. A weekly artifact turns a tool into a companion. Retention driver. | Low | P2 |

---

## Sprint 3 — Dev Mode (Voice-Native Developer Tools)
_The actual job the user does. These make NORA reach for instead of typing._

| # | Name | What it is | Why it matters | Complexity | Priority |
|---|------|-----------|----------------|------------|----------|
| 13 | **Terminal Co-Pilot** (C) | NORA monitors an active terminal (PowerShell/WSL) for errors and command output. When it detects a failure, it speaks a triage hypothesis and optionally runs a suggested fix after confirmation. | The moment a developer hears "that's a CUDA device mismatch, want me to set CUDA_VISIBLE_DEVICES=0?" without reading a stack trace, NORA crosses from assistant to partner. Fastest path to the graduation moment. | Medium | P1 |
| 14 | **Repo-Aware Context Pack** (O) | When NORA detects an active VS Code / terminal in a git repo, auto-loads a compact context pack (current branch, dirty files, last 3 commits, open PRs, failing tests, top 5 most-edited files this week). Pinned to system prompt, refreshed on focus change. | Turns "review my latest PR" from generic Q&A into project-specific assistance without re-explaining context every session. | Medium | P1 |
| 15 | **Git Narration** (C) | Voice commands for the full git workflow: stage, commit (AI-drafted message from diff), push, switch branch, show log. NORA reads the diff, generates a commit message; user approves by voice. | Developers context-switch constantly. Voice-native git removes one of the most repetitive keyboard surfaces in a developer's day. | Medium | P1 |
| 16 | **Voice-Driven Code Surgery** (O) | Dedicated dev actions: `explain_selection`, `refactor_selection`, `add_tests_for`, `bisect_failure`, `regression_hunt`, `dependency_audit`. Reads editor selection via screen-intelligence, runs a focused LLM call with the repo context pack, applies diff via `patch_file` through NeuroSym diff-size guards. | This is the actual differentiator for a developer doing ML + security + SWE. No other voice assistant operates inside the editor with repo awareness. | High | P1 |
| 17 | **ML Experiment Co-Pilot** (O+C) | Plugin wrapping W&B / MLflow / local TensorBoard. Voice surface: "start training run alpha with lr 3e-4", "what's the val loss on the last run", "kill the run that's diverging", "compare alpha and beta". Pulls metrics, narrates trends, suggests next sweep config. | ML iteration is exactly the workflow where eyes-on-screen + hands-on-keyboard fails. You want to ask while watching a curve. First-mover advantage in this niche is wide open. | High | P1 |
| 18 | **Security Research Workbench** (O+C) | Cybersec-flavored plugin: `recon_target` (gated to scoped domains via NeuroSym sandbox), `parse_burp_request`, `cve_lookup`, `extract_iocs_from_clipboard`, `triage_finding`. Integrates with nmap, gobuster, ffuf output streaming to dashboard. | User profile includes cybersecurity and ML. Demonstrates NORA can host specialized professional workflows on the generic core. Strict NeuroSym scoping is the differentiator vs. raw terminal access. | Medium | P2 |
| 19 | **Code Review Briefing** (C) | "NORA, brief me on this PR" — fetches GitHub PR diff, summarizes what changed, flags high-risk areas, reads top reviewer comments aloud. Uses the Repo-Aware Context Pack for project framing. | Code review is reading-heavy and context-switching-heavy. Voice summarization lets the user stay in flow while getting oriented. | Medium | P2 |
| 20 | **Spoken Code Reader** (O) | Code-aware TTS path that reads code naturally: skips boilerplate, expands symbols (`snake_case` → "snake case"), pronounces operators sensibly, summarizes long functions, offers "structure only" vs. "everything" modes. | Code review by voice is currently brutal because TTS reads `__init__` as "underscore underscore init". Fixing this unlocks eyes-free PR review and accessibility. | Medium | P3 |

---

## Sprint 4 — Autonomous & Trustworthy
_The permission system that lets users say yes to autonomy._

| # | Name | What it is | Why it matters | Complexity | Priority |
|---|------|-----------|----------------|------------|----------|
| 21 | **Counterfactual Pre-Flight** (O) | Before any multi-step plan with destructive or external-side-effect steps, NORA renders a structured preview: "I will: open Notion → search 'sprint review' → append 3 bullets → close. Affects: 1 doc. Reversible: yes. Confirm?" User can voice-edit any step before execution. | This is the feature that makes autonomy trusted. Without it, ReAct loops are scary; with it, they feel like piloting. Ships as part of the Sprint 3 ReAct planner. | Medium | P1 |
| 22 | **Reversible Actions & Time-Machine Undo** (O) | Every state-changing action records a compensating inverse: `move_file` → reverse path, `delete_file` → recycle-bin path, `git_commit` → SHA snapshot, `patch_file` → original content. Voice: "undo the last thing you did" / "undo everything you did this morning". | Lowering the cost of mistakes is what makes users say yes to autonomy. Pairs with NeuroSym's destructive-action layer. This is table stakes for trusting NORA with multi-step plans. | High | P1 |
| 23 | **Audit Log with Voice Playback** (C+O) | Every NORA action is timestamped and logged in a structured, searchable format. "What did you do in the last hour?" gets a spoken summary. Full log visible in the dashboard. Pairs with Action Confirmation for the full trust loop. | Transparency is trust. Users who can audit NORA's actions are users who grant it more permissions over time. Without this, security-conscious users keep NORA's access conservative. | Low | P1 |
| 24 | **Anomaly Watchdog** (C) | NORA runs as a background observer of system state — GPU utilization, memory, disk I/O, running processes. Speaks an alert when something crosses a threshold: "GPU memory spiked to 94%, training job may be about to OOM." User-configurable thresholds per metric. | ML researchers lose hours to silent failures. Proactive alerting before a crash, not after, is the difference between a tool and a guardian. | Low | P1 |
| 25 | **Plan Repair & Self-Critique** (O) | When a step fails or verification disagrees with intent, the planner enters a `reflect → repair` micro-loop instead of immediately escalating to the user. Capped at N retries, failure signature logged so the same trap is avoided next time. | Real autonomy fails gracefully. Currently a single click_on miss aborts the whole task. | Medium | P2 |
| 26 | **Persona Calibration System** (C) | NORA learns the user's preferred verbosity, formality, and interruption tolerance from explicit feedback ("too verbose", "stop asking for confirmation on volume changes") and implicit signals (commands that get cancelled immediately). Preferences persist and evolve via User Model. | Default assistant behavior is calibrated for the average user. The power user here is not average. This is what separates a tool that gets disabled from one that gets promoted to startup. | Medium | P2 |

---

## Sprint 5 — Self-Improving
_The loop that makes NORA more capable monthly without writing code._

| # | Name | What it is | Why it matters | Complexity | Priority |
|---|------|-----------|----------------|------------|----------|
| 27 | **Episodic Skill Compiler** (O) | Once a workflow has been recorded + replayed successfully N times (Sprint 4's workflow recording already captures this data), promote it to a first-class skill with a learned parameter schema, a canonical voice trigger, and a NeuroSym risk profile. NORA writes new plugins for itself from observed behavior, gated by user approval. | This is the ceiling-raising feature. NORA gets more capable monthly without code changes. The self-improvement loop no one else has built. | High | P2 |
| 28 | **Evaluation Harness & Regression Suite** (O) | YAML-driven test bench of 200+ scripted voice prompts with expected action plans, run nightly against each LLM provider. Reports drift and regressions on provider updates, lets you A/B prompt changes empirically. | Once NORA has 50+ actions and 3 providers, vibes-based testing fails silently. This is the operational safety net for a production-grade assistant. | Medium | P2 |
| 29 | **Idle-Time Synthesis** (C) | When the user has been idle for N minutes, NORA synthesizes a brief from the session — commands run, files changed, errors occurred — and stores it in the Task Ledger and Nightly Consolidation pipeline. No user prompt needed. | Removes the discipline requirement from journaling. The record exists whether or not the user remembered to ask for it. | Low | P2 |
| 30 | **Drift & Health Telemetry** (O) | Local-only metrics: STT WER trend, LLM latency p50/p95 per provider, guard block rate, planner retry rate, action success rate per skill, memory retrieval precision. Surfaced as a weekly health card in the dashboard. | You cannot improve what you cannot see. Early-warning system for model regressions when providers update. Pairs with Evaluation Harness. | Medium | P3 |

---

## Sprint 6 — Trust, Safety & Privacy (Cross-Cutting)

| # | Name | What it is | Why it matters | Complexity | Priority |
|---|------|-----------|----------------|------------|----------|
| 31 | **Privacy Console** (O) | Dashboard tab showing exactly what left the machine in the last 24h (provider, redacted payload, byte count), per-action provider routing rules ("never send screen OCR to Claude"), and a one-click "go fully local now" toggle that routes everything to Ollama + Whisper + edge-tts. | "Local-first" is the brand promise. Make it auditable and instantly enforceable, or it's marketing. Trust is earned through transparency. | Medium | P1 |
| 32 | **Secrets-Aware Output Filter** (O) | NeuroSym output-stage rule that scans every LLM prompt and every TTS utterance for API keys, tokens, env values, file contents matching `.env`/credential patterns. Either redacts or refuses before the string leaves the machine or gets spoken aloud. | A screen-reading voice assistant will eventually read a token aloud. Closing this hole now is cheaper than a post-incident retrofit. Complements the existing input guard with an output guard. | Low | P1 |
| 33 | **Voice-Native Clipboard Pipeline** (C) | NORA receives spoken input, transforms it (summarize, reformat, translate, expand), and writes the result directly to the clipboard or into the focused application — without the user touching the keyboard. "Rewrite that paragraph more concisely." | Writing is the highest-frequency task for the target user. Hands-on-mouse + voice transformation is a qualitative workflow change. | Low | P1 |

---

## Sprint 7 — Platform & Reach

| # | Name | What it is | Why it matters | Complexity | Priority |
|---|------|-----------|----------------|------------|----------|
| 34 | **MCP Server Mode** (O) | In addition to consuming MCP servers (planned Sprint 5), expose NORA's own skill catalog *as* an MCP server so Claude Code, Cursor, and other MCP-aware agents can drive NORA actions — Claude Code asks NORA to read your screen, run a local workflow, or control a GUI app. | Inverts the relationship. NORA becomes the local-machine substrate that other agents rely on. Massive ecosystem leverage with zero user acquisition cost. | Medium | P2 |
| 35 | **NORA Companion PWA** (O) | Pairs with the planned authenticated WebSocket API. PWA on phone: PTT, typed commands, push notifications for proactive nudges, microphone offload, live "what is my desktop doing right now" view. | Voice AI dies the moment you walk away from the desk. This extends NORA's reach without rewriting the core. | Medium | P2 |
| 36 | **Skill Marketplace & Sandboxed Plugins** (O) | Signed plugin format with a manifest declaring required actions, permissions, network egress, and NeuroSym risk class. Local registry + `nora install <plugin>` voice command. Plugins run in a restricted import context with audited tool access. | Turns NORA from "the user's project" into "a platform other people build for". Without sandboxing, third-party code can never be safely accepted. | High | P3 |

---

## Priority Stack Rank (Top 15 — ship in this order)

| Rank | Deliverable | Sprint |
|------|-------------|--------|
| 1 | Sub-300ms First Phoneme | 1 |
| 2 | Prosody-Aware TTS Director | 1 |
| 3 | Wakeword Detection | 1 |
| 4 | Task Ledger | 2 |
| 5 | Session Replay Briefing | 2 |
| 6 | Nightly Consolidation Job | 2 |
| 7 | Terminal Co-Pilot | 3 |
| 8 | Counterfactual Pre-Flight | 4 |
| 9 | Reversible Actions & Time-Machine Undo | 4 |
| 10 | Audit Log with Voice Playback | 4 |
| 11 | Secrets-Aware Output Filter | 6 |
| 12 | Privacy Console | 6 |
| 13 | Git Narration | 3 |
| 14 | Anomaly Watchdog | 4 |
| 15 | User Model Layer | 2 |

---

## The Graduation Moment

The user is three hours into a debugging session. GPU OOM. They step away. When they come back, NORA briefs them: "While you were away, the training job crashed with CUDA OOM at epoch 4. I found a similar error in your session from two weeks ago — you fixed it by reducing batch size in config.yaml. Want me to apply that change and restart the run?"

The user says yes. NORA previews the action (Counterfactual Pre-Flight), applies the edit, logs it to the Task Ledger, and confirms. The user did not touch a keyboard.

That is the moment. Remembered + reasoned + acted + auditable.
