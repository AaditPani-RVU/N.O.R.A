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

## Sprint 4 — Phase 4: Autonomous Planning ✅ DONE (2026-05-14)
The capability jump. Requires Sprints 1 + 2.

| # | Task | Files | Notes |
|---|------|-------|-------|
| 6 | **ReAct planner loop** ✅ — replace one-shot JSON plan + flat execute with an `observe → decide → act → verify` loop. LLM can see step results before choosing the next step. Max-steps guard already in NeuroSym (`max_plan_steps: 15`) | new `nora/planner.py`, `nora/pipeline.py`, `nora/intent_parser.py`, `nora/schemas.py` | Enables: "set up my ML experiment", "organise my downloads folder", "clone and run this repo" |

---

## Sprint 4 — Workflows & Screen ✅ DONE (2026-05-14)
Productivity and reliability upgrades.

| # | Task | Files | Notes |
|---|------|-------|-------|
| 7 | **Workflow recording & replay** ✅ — promote repeated multi-step episodes into named, editable macros. Voice: "save this as morning-routine" → `run_workflow("morning-routine")` | `nora/commands/workflows.py` | `save_workflow`, `run_workflow`, `list_workflows`, `delete_workflow` — stored in `nora_workflows.json` |
| 8 | **Verified screen automation** ✅ — upgrade `click_on()` from one-shot coordinate guess to `locate → click → screenshot → verify`. Logs to reversible store | `nora/commands/screen_intelligence.py` | Before/after screenshot verification; graceful "may not have changed" fallback |
| 9 | **Multimodal context fusion** ✅ — pre-attach active window title + OCR snippet to ambiguous commands (deictic words: "this", "that", "here") | `nora/pipeline.py`, `nora/intent_parser.py`, `nora/commands/screen_intelligence.py` | 10s cache; only fires when needs_screen_context() heuristic matches |

---

## Sprint 5 — Ecosystem Expansion ✅ DONE (2026-05-17)
External integrations and reach.

| # | Task | Files | Notes |
|---|------|-------|-------|
| 10 | **MCP client integration** ✅ — stdio + HTTP transports; auto-discovers and registers any MCP server's tools into NORA's registry as `mcp_<name>_<tool>` | new `nora/mcp_bridge.py`, `config.yaml` (`mcp_servers:`) | Zero-config: add servers under `mcp_servers:` in config.yaml; `load_all()` called at startup |
| 11 | **Calendar / Email / GitHub plugins** ✅ — calendar_week, calendar_next_event, calendar_free_time, calendar_tomorrow; gmail_latest, gmail_draft_reply, gmail_search, gmail_important; github_my_prs, github_pr_review, github_my_issues, github_notifications | new `plugins/google_calendar.py`, `plugins/gmail.py`, `plugins/github.py` | Calendar + Gmail via Claude Code MCP (already authenticated); GitHub via GITHUB_TOKEN in .env |
| 12 | **Authenticated WebSocket API** ✅ — ws://host:8765; NORA_API_TOKEN Bearer auth; mobile PTT (ptt_start/ptt_end); typed commands; live state + stage push; proactive notification push | `nora/ui_server.py` (start_ws, ws_push, ws_notify), `nora/security.py` (check_api_token), `config.yaml` (`websocket_api:`) | Install: `pip install websockets`. No token = localhost-only; set NORA_API_TOKEN to lock it down |
| 26 | **Persona Calibration System** ✅ — verbosity/tone/style dimensions, voice-adjustable on the fly, persisted in nora_persona.json, injected into every system prompt | new `nora/persona.py`, new `nora/commands/persona.py`, `nora/intent_parser.py` | Voice: "be more concise", "use a casual tone", "be more technical", "reset your persona" |

---

## Backlog (lower priority, good ideas)
- **Personalized intent learner** — lightweight reranker trained on your episode history to suggest likely actions before the LLM finalises the plan
- **Local fine-tuning** — PEFT/LoRA fine-tune phi3:mini on your logged command history for faster, more personal intent parsing
- **Voice persona & emotional intelligence** — adapt TTS tone (professional / casual / urgent) based on context; different behavior per detected user
- **Offline / air-gap mode** — first-class toggle: Whisper + Ollama + edge-tts, zero network, with a clear indicator in the dashboard
- **Plugin discovery & auto-install** — "install the obsidian plugin" → auto-downloads and hot-loads from a plugin registry
