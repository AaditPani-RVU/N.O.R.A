# JARVIS / NORA: project summary

- **Written:** 2026-10-07 12:48:27
- **Source:** ask_claude:sonnet
- **Request:** summary on the JARVIS project

---
## What it is
JARVIS is a personal voice assistant named **NORA**. It is a Python system, in a repo of about 214 tracked files, with roughly 206 registered voice and text commands and about 858 tests. The name JARVIS is the project folder. The assistant itself is called NORA.

NORA listens for a wake word or push-to-talk and parses the request into structured intents. It runs the matching tools and speaks the answer back. It started as a single-laptop assistant. It is now being turned into a persistent "core" with devices attached to it.

## Architecture today
- **Turn pipeline:** a fast path (no model call) → dialogue and conversation → an LLM intent parser or a ReAct planner (`nora/planner.py`) for multi-step jobs.
- **LLM access:** `nora/model_router.py` picks a model per role from `config.yaml`. Groq `gpt-oss-120b` is the primary, with fallbacks such as NVIDIA Nemotron.
- **Tool registry:** `command_engine` with `@register(...)` metadata (risk level, confirmation policy, timeout). The LLM prompt's action list is generated from this registry.
- **Policy and safety:**
  - risk scoring, five autonomy tiers, a tool-trust ledger, consent memory and the NeuroSym guard;
  - an audit log of every executed step.
- **Memory and cognition:**
  - ChromaDB semantic memory, a task ledger, a user model and a nightly consolidation job;
  - a session-replay briefing on first wake and a persona calibration system.
- **Background work:** a durable job queue and a natural-language scheduler.
- **Integrations:** Google Calendar, Gmail, GitHub, Spotify, weather, web search, an MCP client bridge and a Telegram gateway.
- **Linux desktop control:** AT-SPI, D-Bus, PipeWire, ydotool, eBPF observability, snapshots, a compositor IPC and vision (camera, faces, presence).
- **Dev tools:**
  - terminal co-pilot and repo-aware context;
  - git narration, voice code surgery and an ML experiment co-pilot.
- **Remote surface:** a WebSocket and HTTP UI server, an audio relay and a browser dashboard.

## Roadmap history (`ROADMAP.md`)
Sprints 1–5 are done (2026-05-03 to 2026-05-17):
- **Sprint 1:** command timeouts, prompt caching and sentence-streaming TTS.
- **Sprint 2:** session context and the tool manifest registry.
- **Sprint 2b:** durable cognition.
- **Sprint 3:** dev mode.
- **Sprint 4:** the ReAct planner, workflows and verified screen automation.
- **Sprint 5:** MCP, calendar, email and GitHub plugins, the authenticated WebSocket API and personas.

The remaining backlog is an intent reranker, local LoRA fine-tuning, emotional TTS, an offline mode and plugin auto-install.

## Current direction

**NORA Distributed** (`NORA_DISTRIBUTED_PLAN.md`):
- The brain becomes a permanent core on this laptop, which stays at home and on.
- Phones, other laptops and browsers become devices that advertise capabilities. A Device Hub proxies those capabilities as ordinary tools, so existing policy and audit apply to them unchanged.
- Other planned pieces are channel-aware turns, a delivery router, an event and trigger engine and a shared SQLite store.
- Phase 0 fixed six security issues in the remote surface, including an unauthenticated `/audio` endpoint and a token check that failed open.
- Phases 4–6 are done and verified on the Pixel, including the Android app and talking to NORA from the phone. Phases 7–9 are still pending.

**NORA Sharp** (`NORA_SHARP_PLAN.md`), a proposal that runs before Phases 7–9. It is aimed at reliability, speed and measurement.
- **Current numbers:**
  - 47% of turns hit the fast path at about 0.1–0.2 s.
  - Model turns take a 1.5–1.8 s median and 7.6–9.3 s at p90.
  - The intent prompt averages about 5.4k tokens.
- **Failures it targets:** an expired Calendar token that nobody noticed, a dead Groq model still first in line for search, and common questions that miss the fast path. It also targets misheard names, wrong routes and test traffic that trains the routine learner.
- **Planned fixes:** widen the fast path, shrink the prompt and fail loudly. It also adds a replay-based understanding regression suite. Models become replaceable parts that NORA evaluates against her own eval set before switching.

## Constraints
- Free or keyless services only, with no paid APIs.
- The Groq budget (8k tokens/min, 200k/day) is shared with the live NORA.
- Kokoro TTS runs at about 0.4x real time on this CPU, so phone voice defaults to Android TTS.
- Systemd runs NORA from the main tree. New phases are built in separate worktrees.

## Working tree state
The branch is `main`. The latest commit is "Talk to NORA from the phone (Phase 6)".

Uncommitted changes:
- `NORA_DISTRIBUTED_PLAN.md`
- `nora/hub/server.py`
- `nora/pipeline.py`
- the new `NORA_SHARP_PLAN.md`
- the new `nora_core.db*` files and the `.migrated` JSON files, which are leftovers from the SQLite migration

I did not read the code beyond the plan documents, so the counts and status come from those documents.
