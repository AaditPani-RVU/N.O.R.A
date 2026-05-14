# NORA Development Roadmap

Generated from a joint Claude + Codex analysis (2026-05-03).
Each sprint builds on the last — do them in order.

---

## Sprint 1 — Stability & Quick Wins ✅ DONE (2026-05-03)
These unblock everything else.

| # | Task | Files | Notes |
|---|------|-------|-------|
| 1 | **Command timeout enforcement** — wrap each step in `asyncio.wait_for`, check `context.is_cancelled()` between steps | `nora/command_engine.py` | `config.yaml` already has `timeouts.command_sec: 15` but it was never enforced |
| 2 | **Prompt caching (Claude provider)** — add `cache_control` to the system prompt so the ~500-token static prefix is cached across turns | `nora/intent_parser.py` | Saves ~80% token cost on the Claude path; one-line change |
| 3 | **Sentence-streaming TTS** — pipeline: generate sentence N+1 while sentence N is playing; user hears first words in ~500 ms instead of waiting for full synthesis | `nora/speaker.py` | Biggest perceived latency win, especially for `ask_claude` / `tell_me_about` |

---

## Sprint 2 — Context & Plugin Foundation ✅ DONE (2026-05-04)
Architectural groundwork that Phase 4 requires.

| # | Task | Files | Notes |
|---|------|-------|-------|
| 4 | **Session context manager** ✅ — rolling window of last N turns (text + intent + result) fed into `_build_system_prompt()`. Enables "do that again", "fix the previous error", "continue where we left off" | `nora/context.py`, `nora/intent_parser.py`, `nora/pipeline.py` | Separate from ChromaDB long-term memory; short-term conversational buffer only |
| 5 | **Tool manifest registry** ✅ — extend `@register()` with metadata (parameter schema, risk level, timeout, confirmation policy). Auto-generate the LLM action signatures block from the registry instead of the hardcoded block in `intent_parser.py` | `nora/command_engine.py`, `nora/intent_parser.py`, `nora/schemas.py`, all plugins | Makes plugins fully self-describing; removes the drift between registered actions and the prompt |

---

## Sprint 2b — Know (Durable Cognition) ✅ DONE (2026-05-14)
Layered cognition: Task Ledger, Session Replay Briefing, Nightly Consolidation, User Model.

| # | Task | Files | Notes |
|---|------|-------|-------|
| 6 | **Task Ledger** ✅ — persistent JSON store of open/closed tasks with voice CRUD | `nora/task_ledger.py`, `nora/commands/task_commands.py` | add_task, list_tasks, close_task, start_task, log_task_note, task_status |
| 7 | **Session Replay Briefing** ✅ — spoken summary on first wake (>4h gap or new day) | `nora/session_briefing.py`, `nora/pipeline.py` | Sources: Task Ledger + episode log + daily_brief.json |
| 8 | **Nightly Consolidation Job** ✅ — scheduled at 23:00, writes nora_daily_brief.json | `nora/consolidation.py`, `nora/pipeline.py` | Scans episodes + tasks + git; stores ChromaDB entry |
| 9 | **User Model Layer** ✅ — typed accessors + 200-token user card in system prompt | `nora/user_model.py`, `nora/intent_parser.py` | peak_hours, top_commands, current_projects, stale_threads, recent_failures |

---

## Sprint 3 — Dev Mode (Voice-Native Developer Tools) ✅ DONE (2026-05-14)

| # | Task | Files | Notes |
|---|------|-------|-------|
| 13 | **Terminal Co-Pilot** ✅ — clipboard watcher alerts on stack traces; voice triage via Claude Haiku | `nora/terminal_monitor.py`, `plugins/dev_tools.py` (check_terminal, explain_error) | Background thread polls clipboard every 2s |
| 14 | **Repo-Aware Context Pack** ✅ — branch, dirty files, last 3 commits, hot files, open PRs auto-injected | `nora/repo_context.py`, `nora/intent_parser.py` | Cached 30s; detects repo from active window title |
| 15 | **Git Narration** ✅ — smart commit (AI message from diff), git_log, git_diff_summary, git_pull, git_switch_branch | `plugins/dev_tools.py` | git_smart_commit drafts message via Claude Haiku then commits immediately |
| 16 | **Voice-Driven Code Surgery** ✅ — explain/refactor/test/bisect via clipboard | `nora/commands/code_surgery.py` | explain_selection, refactor_selection, add_tests_for_selection, bisect_failure, dependency_audit |
| 17 | **ML Experiment Co-Pilot** ✅ — MLflow + TensorBoard + W&B voice surface | `plugins/ml_copilot.py` | ml_list_runs, ml_run_status, ml_last_metrics, ml_compare_runs, ml_kill_run |
| — | **Wakeword primary + Windows autostart** ✅ — wakeword is now default; PTT remains secondary | `config.yaml`, `nora/listener.py`, `start_nora.pyw`, `install_autostart.ps1` | Run install_autostart.ps1 once as Administrator |

---

## Sprint 4 — Phase 4: Autonomous Planning
The capability jump. Requires Sprints 1 + 2.

| # | Task | Files | Notes |
|---|------|-------|-------|
| 6 | **ReAct planner loop** — replace one-shot JSON plan + flat execute with an `observe → decide → act → verify` loop. LLM can see step results before choosing the next step. Max-steps guard already in NeuroSym (`max_plan_steps: 15`) | new `nora/planner.py`, `nora/pipeline.py`, `nora/intent_parser.py`, `nora/schemas.py` | Enables: "set up my ML experiment", "organise my downloads folder", "clone and run this repo" |

---

## Sprint 4 — Workflows & Screen
Productivity and reliability upgrades.

| # | Task | Files | Notes |
|---|------|-------|-------|
| 7 | **Workflow recording & replay** — promote repeated multi-step episodes into named, editable macros. Voice: "save this as morning-routine" → `run_workflow("morning-routine")`. NORA already records bigrams; this makes them directly actionable | `nora/cognitive_memory.py`, `nora/command_engine.py`, `nora/intent_parser.py`, `nora/ui_server.py`, `nora/static/index.html` | The behavioral model already has the data; this just surfaces it |
| 8 | **Verified screen automation** — upgrade `click_on()` from one-shot coordinate guess to `locate → click → screenshot → verify`. Store bounding boxes, before/after state, retry rules | `nora/commands/screen_intelligence.py`, `nora/schemas.py`, `config.yaml` | Required for autonomous tasks that interact with GUI apps |
| 9 | **Multimodal context fusion** — pre-attach active window title + OCR snippet to ambiguous commands so NORA understands "click the blue one" or "copy that command" without explicit screen command names | `nora/pipeline.py`, `nora/intent_parser.py`, `nora/commands/screen_intelligence.py`, `nora/context.py` | Screen intelligence currently runs only after LLM emits a screen action |

---

## Sprint 5 — Ecosystem Expansion
External integrations and reach.

| # | Task | Files | Notes |
|---|------|-------|-------|
| 10 | **MCP client integration** — use Model Context Protocol to consume ready-made tool servers (Playwright browser automation, Google Drive, Obsidian) without writing custom plugins for each | `nora/command_engine.py`, new `nora/mcp_bridge.py`, `config.yaml` | Claude Code already has MCP servers available; NORA can speak the same protocol |
| 11 | **Calendar / Email / GitHub plugins** — "what's on my calendar today", "draft a reply to my last email", "review my latest PR". Via Google Calendar MCP, Gmail MCP, or direct API plugins | new `plugins/google_calendar.py`, `plugins/gmail.py`, `plugins/github.py` | Highest daily-utility value of the external integrations |
| 12 | **Authenticated WebSocket API** — replace the localhost-only dashboard with an auth-gated WebSocket endpoint for mobile PTT, typed commands, proactive push notifications, and live state streaming | `nora/ui_server.py`, `nora/security.py`, `config.yaml` | Lets you use NORA away from the keyboard via a phone browser or companion app |

---

## Backlog (lower priority, good ideas)
- **Personalized intent learner** — lightweight reranker trained on your episode history to suggest likely actions before the LLM finalises the plan
- **Local fine-tuning** — PEFT/LoRA fine-tune phi3:mini on your logged command history for faster, more personal intent parsing
- **Voice persona & emotional intelligence** — adapt TTS tone (professional / casual / urgent) based on context; different behavior per detected user
- **Offline / air-gap mode** — first-class toggle: Whisper + Ollama + edge-tts, zero network, with a clear indicator in the dashboard
- **Plugin discovery & auto-install** — "install the obsidian plugin" → auto-downloads and hot-loads from a plugin registry
