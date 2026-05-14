# NORA — Complete Command Reference
_All commands that should work in the target version of NORA (current + planned deliverables)._
_Annotated with status: ✅ exists | 🔨 in roadmap | 🆕 new from DELIVERABLES.md_

---

## Activation

| Voice Command | Action | Status |
|---------------|--------|--------|
| `Ctrl + \`` (hold) | Push-to-talk activation | ✅ |
| Double clap | Clap-to-talk activation | ✅ |
| `Hey NORA [command]` | Always-on wakeword activation | 🆕 Sprint 1 |
| `[custom wakeword] [command]` | User-enrolled wakeword | 🆕 Sprint 1 |

---

## Control & Interruption

| Voice Command | Action | Status |
|---------------|--------|--------|
| `stop` / `cancel` / `never mind` | Cancel current action | ✅ |
| `stop listening` | Pause NORA entirely | ✅ |
| `resume` / `start listening` | Resume from pause | ✅ |
| `undo that` | Undo the last NORA action | 🆕 Sprint 4 |
| `undo everything you did this morning` | Time-machine undo by time window | 🆕 Sprint 4 |
| `undo the last [N] things` | Undo N most recent actions | 🆕 Sprint 4 |
| `dry run [command]` | Preview what NORA would do before executing | 🆕 Sprint 4 |
| `preview [command]` | Alias for dry run | 🆕 Sprint 4 |

---

## Greetings & Session

| Voice Command | Action | Status |
|---------------|--------|--------|
| `hello` / `hey` / `hi NORA` | Greeting response | ✅ |
| `good morning` | Morning greeting + session replay briefing | ✅ + 🆕 Sprint 2 |
| `brief me on last session` | Spoken summary of last session's work and open items | 🆕 Sprint 2 |
| `what were we working on` / `what was I doing last night` | Task Ledger + session replay query | 🆕 Sprint 2 |
| `how are you` | Status response | ✅ |
| `what can you do` | Lists available commands | ✅ |
| `goodnight` / `goodbye` | Closing + idle-time synthesis trigger | ✅ + 🆕 Sprint 5 |

---

## App & Window Control

| Voice Command | Action | Status |
|---------------|--------|--------|
| `open [app name]` | Launch application | ✅ |
| `close [app name]` | Close application | ✅ |
| `close all apps` | Close all open apps (requires confirmation) | ✅ |
| `switch to [app name]` | Focus application | ✅ |
| `minimize [app name]` | Minimize window | ✅ |
| `maximize [app name]` | Maximize window | ✅ |
| `snap left` / `snap right` | Snap active window | ✅ |

---

## System Control

| Voice Command | Action | Status |
|---------------|--------|--------|
| `shutdown` / `shut down my computer` | Shutdown (requires confirmation) | ✅ |
| `restart` / `reboot` | Restart (requires confirmation) | ✅ |
| `sleep` / `put computer to sleep` | Sleep mode | ✅ |
| `lock screen` | Lock workstation | ✅ |
| `volume up` / `volume down` | Adjust system volume | ✅ |
| `set volume to [N] percent` | Set exact volume | ✅ |
| `mute` / `unmute` | Toggle mute | ✅ |
| `brightness up` / `brightness down` | Adjust display brightness | ✅ |
| `set brightness to [N] percent` | Set exact brightness | ✅ |
| `take a screenshot` | Capture screen to clipboard/file | ✅ |
| `empty recycle bin` | Clear recycle bin (requires confirmation) | ✅ |
| `go fully local` / `offline mode` | Switch all providers to local (Whisper + Ollama + edge-tts) | 🆕 Sprint 6 |

---

## System Info

| Voice Command | Action | Status |
|---------------|--------|--------|
| `what's the time` / `what time is it` | Speak current time | ✅ |
| `what's the date` / `what day is it` | Speak current date | ✅ |
| `what's the weather` | Speak current weather | ✅ |
| `battery status` / `how much battery do I have` | Speak battery percentage | ✅ |
| `CPU usage` / `how's the CPU` | Speak current CPU load | ✅ |
| `memory usage` / `RAM status` | Speak RAM usage | ✅ |
| `GPU usage` / `how's the GPU` | Speak GPU utilization | ✅ |
| `disk space` / `storage status` | Speak disk usage | ✅ |
| `system info` | Full system health summary | ✅ |
| `show health report` | Weekly system health card (latency, success rates, WER trend) | 🆕 Sprint 5 |

---

## Monitoring & Anomaly Watchdog

| Voice Command | Action | Status |
|---------------|--------|--------|
| `watch GPU usage` / `monitor training` | Start GPU/process anomaly watchdog | 🆕 Sprint 4 |
| `alert me if CPU goes over [N] percent` | Set custom threshold alert | 🆕 Sprint 4 |
| `alert me if memory goes over [N] percent` | Set memory threshold | 🆕 Sprint 4 |
| `stop monitoring` / `stop watching` | Disable anomaly watchdog | 🆕 Sprint 4 |
| `what are my active monitors` | List active watchdog rules | 🆕 Sprint 4 |

---

## Web & Search

| Voice Command | Action | Status |
|---------------|--------|--------|
| `search for [query]` | Google search | ✅ |
| `search [query] on [platform]` | Platform-specific search (YouTube, GitHub, etc.) | ✅ |
| `open [URL]` | Open URL in browser | ✅ |
| `look up [query]` | Quick search + speak summary | ✅ |
| `what is [topic]` / `tell me about [topic]` | Ask Claude + speak answer | ✅ |

---

## Music

| Voice Command | Action | Status |
|---------------|--------|--------|
| `play [song / artist / album]` | Play local music | ✅ |
| `play [song] on Apple Music` | Play via Apple Music | ✅ |
| `pause` / `pause music` | Pause playback | ✅ |
| `resume` / `resume music` | Resume playback | ✅ |
| `stop music` | Stop playback | ✅ |
| `next track` / `skip` | Next song | ✅ |
| `previous track` / `go back` | Previous song | ✅ |
| `volume up` / `volume down` | Adjust music volume | ✅ |
| `what's playing` | Speak current track | ✅ |

---

## File Operations

| Voice Command | Action | Status |
|---------------|--------|--------|
| `create file [name]` / `new file [name]` | Create empty file | ✅ |
| `delete file [name]` | Delete file (requires confirmation) | ✅ |
| `move file [name] to [destination]` | Move file | ✅ |
| `copy file [name] to [destination]` | Copy file | ✅ |
| `rename file [name] to [new name]` | Rename file | ✅ |
| `open folder [path]` | Open folder in Explorer | ✅ |
| `search files for [query]` | Search filesystem for files matching query | ✅ |
| `create folder [name]` | Create directory | ✅ |
| `organize downloads folder` | Autonomous file organization (ReAct planner) | 🔨 Sprint 3 |

---

## Screen Intelligence

| Voice Command | Action | Status |
|---------------|--------|--------|
| `read the screen` / `what's on screen` | OCR and speak screen content | ✅ |
| `click on [element description]` | Find and click UI element | ✅ |
| `click the [color/position] button` | Multimodal click with context | 🔨 Sprint 4 |
| `what does [element] say` | OCR targeted area | ✅ |
| `scroll down` / `scroll up` | Scroll active window | ✅ |
| `go to [position on screen]` | Navigate to screen coordinate/element | ✅ |

---

## Typing & Clipboard

| Voice Command | Action | Status |
|---------------|--------|--------|
| `type [text]` | Type text at cursor | ✅ |
| `press enter` / `press escape` / `press [key]` | Send keystroke | ✅ |
| `copy that` / `copy to clipboard` | Copy selection to clipboard | ✅ |
| `paste that` / `paste clipboard` | Paste clipboard content | ✅ |
| `summarize this into clipboard` | Transform clipboard content + write back | 🆕 Sprint 6 |
| `rewrite that more concisely` | Transform selection, write to clipboard | 🆕 Sprint 6 |
| `rewrite that more formally` | Formal tone transform | 🆕 Sprint 6 |
| `translate that to [language]` | Translate clipboard/selection | 🆕 Sprint 6 |
| `expand that bullet point` | Expand short note into paragraph | 🆕 Sprint 6 |
| `format that as a list` | Restructure clipboard content | 🆕 Sprint 6 |

---

## Ask Claude / LLM

| Voice Command | Action | Status |
|---------------|--------|--------|
| `ask Claude [question]` | Query Claude API, speak response | ✅ |
| `tell me about [topic]` | Research query via Claude | ✅ |
| `explain [concept]` | Explanation via Claude | ✅ |
| `summarize [content]` | Summarization via Claude | ✅ |
| `write [document type] about [topic]` | Drafting via Claude | ✅ |

---

## Cognitive Memory

| Voice Command | Action | Status |
|---------------|--------|--------|
| `remember [fact]` / `note that [fact]` | Store fact in semantic memory | ✅ |
| `what do I remember about [topic]` | Semantic recall query | ✅ |
| `recall [query]` | Retrieve from ChromaDB | ✅ |
| `inject knowledge [info]` | Directly write to knowledge base | ✅ |
| `memory status` | Speak memory store statistics | ✅ |
| `show patterns` / `what are my patterns` | Speak detected behavioral patterns | ✅ |
| `what have you learned about me` | Speak User Model summary | 🆕 Sprint 2 |
| `show my user profile` | Display User Model in dashboard | 🆕 Sprint 2 |
| `forget [topic]` | Redact memory entries matching topic | 🆕 Sprint 2 |
| `pin this memory` / `pin that fact` | Promote to core memory | 🆕 Sprint 2 |
| `review my memories` | Open memory review UI | 🆕 Sprint 2 |

---

## Task Ledger

| Voice Command | Action | Status |
|---------------|--------|--------|
| `add task [description]` | Create new task in Task Ledger | 🆕 Sprint 2 |
| `what am I working on` / `show my tasks` | Speak open task list | 🆕 Sprint 2 |
| `task status` | Full Task Ledger summary | 🆕 Sprint 2 |
| `complete task [name or number]` | Mark task done | 🆕 Sprint 2 |
| `close task [name]` | Close and archive task | 🆕 Sprint 2 |
| `log [note] to task [name]` | Append note to task log | 🆕 Sprint 2 |
| `new goal [description]` | Create high-level goal, decompose to subtasks | 🆕 Sprint 2 |
| `what's the next step on [goal]` | Surface next unfinished subtask | 🆕 Sprint 2 |
| `show task history` | Browse completed tasks | 🆕 Sprint 2 |

---

## Proactive Intelligence & Reports

| Voice Command | Action | Status |
|---------------|--------|--------|
| `what should I work on next` | Proactive suggestion from patterns + User Model | ✅ + 🆕 |
| `suggest next step` | Next action suggestion for current context | ✅ |
| `morning briefing` | Session replay + open tasks + weather + calendar | 🆕 Sprint 2 |
| `give me my daily summary` | End-of-day spoken report | 🆕 Sprint 5 |
| `weekly review` | Trigger weekly self-review letter | 🆕 Sprint 5 |
| `what have I been doing this week` | Activity summary from Nightly Consolidation | 🆕 Sprint 2 |

---

## Audit & Privacy

| Voice Command | Action | Status |
|---------------|--------|--------|
| `what did you do today` / `action log` | Speak audit log summary | 🆕 Sprint 4 |
| `what did you do in the last hour` | Time-windowed audit log query | 🆕 Sprint 4 |
| `what did you change` | List file/system changes made | 🆕 Sprint 4 |
| `what did you send out today` | Privacy report: data egress summary | 🆕 Sprint 6 |
| `show privacy report` | Full data egress log in dashboard | 🆕 Sprint 6 |
| `don't send screen data to cloud` | Set per-action provider routing rule | 🆕 Sprint 6 |
| `never send [data type] to [provider]` | Add provider routing exclusion | 🆕 Sprint 6 |

---

## Persona & Preferences

| Voice Command | Action | Status |
|---------------|--------|--------|
| `be more concise` | Reduce response verbosity | 🆕 Sprint 4 |
| `be more detailed` / `more verbose` | Increase response detail | 🆕 Sprint 4 |
| `stop asking for confirmation on [action]` | Remove confirmation requirement for action | 🆕 Sprint 4 |
| `always ask before [action]` | Add confirmation requirement for action | 🆕 Sprint 4 |
| `formal mode` | Switch to professional tone | 🆕 Sprint 4 |
| `casual mode` | Switch to relaxed tone | 🆕 Sprint 4 |
| `you're being too slow` / `speed up` | Shorten acknowledgements and preamble | 🆕 Sprint 4 |

---

## Git & Version Control

| Voice Command | Action | Status |
|---------------|--------|--------|
| `git status` / `what's changed` | Speak current git status | 🆕 Sprint 3 |
| `stage all changes` | Git add all modified files | 🆕 Sprint 3 |
| `stage [file name]` | Git add specific file | 🆕 Sprint 3 |
| `commit these changes` | AI-draft commit message from diff, confirm by voice | 🆕 Sprint 3 |
| `commit with message [text]` | Commit with specified message | 🆕 Sprint 3 |
| `push` / `push my changes` | Git push (requires confirmation) | 🆕 Sprint 3 |
| `pull` / `pull latest changes` | Git pull | 🆕 Sprint 3 |
| `switch to branch [name]` | Git checkout | 🆕 Sprint 3 |
| `create branch [name]` | Git checkout -b | 🆕 Sprint 3 |
| `show git log` / `what are my recent commits` | Speak last N commits | 🆕 Sprint 3 |
| `show my open PRs` | List GitHub PRs | 🆕 Sprint 3 |
| `brief me on PR [number]` / `review PR [number]` | Code Review Briefing | 🆕 Sprint 3 |
| `show my open issues` | List GitHub issues assigned to me | 🆕 Sprint 3 |
| `what branch am I on` | Speak current branch name | 🆕 Sprint 3 |

---

## Code Surgery (requires active editor / repo context)

| Voice Command | Action | Status |
|---------------|--------|--------|
| `explain this` / `explain the selection` | Explain selected code using repo context | 🆕 Sprint 3 |
| `refactor this` / `refactor the selection` | Generate refactored version via LLM + apply via patch | 🆕 Sprint 3 |
| `add tests for this` | Generate test cases for selected code | 🆕 Sprint 3 |
| `why is this test failing` | Triage test failure using error output + code context | 🆕 Sprint 3 |
| `find what's causing this bug` / `bisect this failure` | Guided failure bisection via ReAct planner | 🆕 Sprint 3 |
| `audit dependencies` | Check for outdated/vulnerable dependencies | 🆕 Sprint 3 |
| `read this code to me` | Spoken Code Reader — natural TTS of code structure | 🆕 Sprint 3 |
| `read structure only` | Spoken Code Reader — summary mode | 🆕 Sprint 3 |
| `what does [function/class] do` | Explain named code entity | 🆕 Sprint 3 |

---

## Terminal Co-Pilot

| Voice Command | Action | Status |
|---------------|--------|--------|
| `what went wrong` / `analyze that error` | Triage most recent terminal error | 🆕 Sprint 3 |
| `suggest a fix` / `fix that error` | Propose fix for detected error, apply after confirmation | 🆕 Sprint 3 |
| `explain that stack trace` | Natural-language explanation of traceback | 🆕 Sprint 3 |
| `run [command]` | Execute shell command via NORA | 🔨 Sprint 3 |
| `run that again` | Repeat last terminal command | 🆕 Sprint 3 |

---

## ML Experiment Co-Pilot

| Voice Command | Action | Status |
|---------------|--------|--------|
| `start training run [name] with [config key] [value]` | Launch training job via W&B / MLflow | 🆕 Sprint 3 |
| `what's my training status` | Speak status of active runs | 🆕 Sprint 3 |
| `what's the val loss on the last run` | Fetch and speak metric from experiment tracker | 🆕 Sprint 3 |
| `kill the run that's diverging` | Stop diverging training run | 🆕 Sprint 3 |
| `compare run [A] and run [B]` | Fetch and narrate metric comparison | 🆕 Sprint 3 |
| `log this run as [name]` | Tag current run with label | 🆕 Sprint 3 |
| `suggest next sweep config` | LLM-proposed hyperparameter sweep based on run history | 🆕 Sprint 3 |
| `show TensorBoard` | Open TensorBoard in browser | 🆕 Sprint 3 |
| `what's my best [metric] this week` | Query experiment history | 🆕 Sprint 3 |

---

## Security Research Workbench

| Voice Command | Action | Status |
|---------------|--------|--------|
| `recon [target]` | Run scoped recon (NeuroSym sandbox-gated) | 🆕 Sprint 3 |
| `enumerate open ports on [host/range]` | nmap port scan (NeuroSym-gated, scoped to authorized targets) | 🆕 Sprint 3 |
| `parse this Burp request` | Parse and summarize HTTP request from clipboard | 🆕 Sprint 3 |
| `look up CVE [ID]` | Fetch CVE details and speak summary | 🆕 Sprint 3 |
| `extract IOCs from clipboard` | Parse indicators of compromise from copied text | 🆕 Sprint 3 |
| `log finding [description]` | Add security finding to Task Ledger | 🆕 Sprint 3 |
| `triage this finding` | LLM triage of current security finding | 🆕 Sprint 3 |
| `security mode on` / `security mode off` | Toggle Security Research Workbench context | 🆕 Sprint 3 |

---

## Notifications

| Voice Command | Action | Status |
|---------------|--------|--------|
| `notify me in [N] minutes` | Set a timed reminder | ✅ |
| `remind me to [task] at [time]` | Schedule reminder | ✅ |
| `show notifications` / `what are my notifications` | Read pending notifications | ✅ |
| `clear notifications` | Dismiss all notifications | ✅ |

---

## Google Services

| Voice Command | Action | Status |
|---------------|--------|--------|
| `what's on my calendar today` | Speak today's calendar events | ✅ |
| `add [event] to calendar on [date] at [time]` | Create calendar event | ✅ |
| `what's my next meeting` | Speak next upcoming event | ✅ |
| `create a Google Doc about [topic]` | Create and open new Google Doc | ✅ |
| `search my Drive for [query]` | Search Google Drive | ✅ |
| `draft a reply to my last email` | Compose reply to most recent Gmail | 🔨 Sprint 5 |

---

## Focus Mode

| Voice Command | Action | Status |
|---------------|--------|--------|
| `start focus mode` / `focus mode on` | Activate focus mode (suppress distractions) | ✅ |
| `stop focus mode` / `focus mode off` | Deactivate focus mode | ✅ |
| `start pomodoro` | Begin 25-minute focus timer | ✅ |
| `how much time is left in my pomodoro` | Speak remaining timer | ✅ |

---

## Workflows & Automation

| Voice Command | Action | Status |
|---------------|--------|--------|
| `run workflow [name]` | Execute saved workflow | ✅ |
| `list workflows` / `what workflows do I have` | Speak available workflows | ✅ |
| `save this as [name]` | Record current session actions as a named workflow | 🔨 Sprint 4 |
| `delete workflow [name]` | Remove saved workflow | 🔨 Sprint 4 |
| `edit workflow [name]` | Open workflow in dashboard editor | 🔨 Sprint 4 |
| `promote [workflow] to a skill` | Episodic Skill Compiler: approve auto-generated plugin | 🆕 Sprint 5 |
| `what workflows have I been doing manually` | Surface repetitive unrecorded patterns | 🆕 Sprint 5 |

---

## Plugins

| Voice Command | Action | Status |
|---------------|--------|--------|
| `list plugins` / `what plugins are loaded` | Speak active plugin list | ✅ |
| `enable plugin [name]` | Activate installed plugin | ✅ |
| `disable plugin [name]` | Deactivate plugin | ✅ |
| `install plugin [name]` / `nora install [plugin]` | Download and hot-load from plugin registry | 🆕 Sprint 7 |
| `show plugin registry` | Browse available plugins | 🆕 Sprint 7 |

---

## Dev Tools Plugin

| Voice Command | Action | Status |
|---------------|--------|--------|
| `start dev server` | Launch local development server | ✅ |
| `stop dev server` | Stop running dev server | ✅ |
| `run tests` | Execute project test suite | ✅ |
| `build project` | Run build command | ✅ |
| `check logs` | Read and summarize recent log output | ✅ |
| `open terminal` | Launch PowerShell / Windows Terminal | ✅ |

---

## Productivity Plugin

| Voice Command | Action | Status |
|---------------|--------|--------|
| `daily summary` / `give me my day` | Speak productivity summary | ✅ |
| `how productive was I today` | Speak app-usage and task completion report | ✅ |
| `block distracting sites` | Activate site blocker | ✅ |
| `unblock sites` | Deactivate site blocker | ✅ |

---

## PTT Control

| Voice Command | Action | Status |
|---------------|--------|--------|
| `push to talk mode` | Switch to PTT-only activation | ✅ |
| `always on mode` / `wakeword mode` | Switch to wakeword activation | 🆕 Sprint 1 |
| `change push to talk key to [key]` | Rebind PTT key | ✅ |

---

## Dashboard & UI

| Voice Command | Action | Status |
|---------------|--------|--------|
| `open dashboard` | Open web dashboard in browser | ✅ |
| `show memory` / `memory review` | Open memory review tab | 🆕 Sprint 2 |
| `show task board` | Open Task Ledger in dashboard | 🆕 Sprint 2 |
| `show audit log` | Open action history tab | 🆕 Sprint 4 |
| `show privacy console` | Open data egress tab | 🆕 Sprint 6 |
| `show health telemetry` | Open system health dashboard | 🆕 Sprint 5 |

---

## Legend

| Symbol | Meaning |
|--------|---------|
| ✅ | Already implemented in NORA v1 |
| 🔨 | Planned in existing ROADMAP.md sprints 3–5 |
| 🆕 | New — from DELIVERABLES.md (joint Claude + Codex analysis, 2026-05-14) |
