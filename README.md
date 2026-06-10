<div align="center">

# NORA
### *Never Off, Rarely Asked*

[![Python](https://img.shields.io/badge/python-3.10%2B-blue?style=flat-square)](https://python.org)
[![License: MIT](https://img.shields.io/badge/license-MIT-green?style=flat-square)](LICENSE)
[![Powered by NeuroSym](https://img.shields.io/badge/guardrails-neurosym--ai-blueviolet?style=flat-square)](https://github.com/AaditPani-RVU/NeuroSym-AI)
[![Platform](https://img.shields.io/badge/platform-Linux-orange?style=flat-square&logo=linux&logoColor=white)]()
[![Status](https://img.shields.io/badge/status-active-brightgreen?style=flat-square)]()

**A local, voice-controlled AI assistant with neuro-symbolic guardrails —**  
**built for Linux with capabilities that are literally impossible on Windows or macOS.**

> This is the **Linux flagship branch**. It adds five kernel-native features on top of the core assistant:  
> AT-SPI2 screen control · D-Bus universal remote · eBPF causal observer · time-travel filesystem · adaptive PipeWire audio.

</div>

---

## What is NORA?

NORA is a fully local voice AI that listens for your commands, parses intent through a language model, validates every action through a symbolic safety layer, and executes against your OS — all in under a second, with no data leaving your machine unless you configure it to.

She learns your habits, anticipates your next move, remembers everything you've done, and will block you from doing something destructive without asking first.

The name is literal: **Never Off, Rarely Asked** — the proactive intelligence engine surfaces suggestions before you open your mouth.

On Linux she goes further. By exploiting the accessibility bus, D-Bus IPC, eBPF kernel tracing, COW filesystems, and PipeWire graph mutation, NORA can do things no proprietary assistant — Siri, Cortana, Alexa — can touch.

---

## Architecture

```
  User input  (voice · text · WhatsApp · UI)
         │
         ▼
  ┌──────────────────────────────────────────────────────┐
  │  NEUROSYM GUARD  ─── input stage                     │
  │  PromptInjectionRule · 9 attack categories · <1ms    │
  │  Blocks adversarial commands before LLM sees them    │
  └────────┬─────────────────────────────────────────────┘
           │
           ▼
  ┌──────────────────────────────────────────────────────┐
  │  LLM INTENT PARSER  +  ReAct PLANNER                 │
  │  Natural language  →  structured JSON action plan    │
  │  Groq · Claude · Ollama  (provider-agnostic)         │
  └────────┬─────────────────────────────────────────────┘
           │
           ▼
  ┌──────────────────────────────────────────────────────┐
  │  SEMANTIC MEMORY  (ChromaDB + RAG)                   │
  │  all-MiniLM-L6-v2 embeddings · episodic + knowledge  │
  │  Behavioral model · proactive suggestions            │
  └────────┬─────────────────────────────────────────────┘
           │
           ▼
  ┌──────────────────────────────────────────────────────┐
  │  NEUROSYM GUARD  ─── action stage                    │
  │  destructive_needs_confirmation · max_steps(15)      │
  │  no_path_outside_sandbox · full audit trace          │
  └────────┬─────────────────────────────────────────────┘
           │
           ▼
  ┌──────────────────────────────────────────────────────────────────┐
  │  ACTION ENGINE  +  TTS RESPONSE                                  │
  │  Core commands · plugins · Linux flagship modules (F1–F5)        │
  │  AT-SPI2 · D-Bus · eBPF · btrfs/CRIU · PipeWire · Wayland IPC   │
  └──────────────────────────────────────────────────────────────────┘
```

---

## Linux Flagship Features

These five features exploit Linux kernel and userspace primitives that simply don't exist on other operating systems. Each is demo-worthy in 30 seconds.

---

### F1 — Semantic Screen Control via AT-SPI2

Replace fragile OCR loops with Linux's universal accessibility tree. Every GTK, Qt, and Electron app publishes its full widget hierarchy over the AT-SPI D-Bus — NORA reads it directly to find buttons, fields, and tabs **by description**, deterministically. A small local VLM (Qwen2-VL-2B) fires only when AT-SPI data is sparse (un-flagged Electron apps). Mouse and keyboard synthesis runs via `ydotool`, which works identically under **Wayland and X11**.

```
"NORA, in Firefox click the address bar, type github.com/anthropic,
press enter, then click the Issues tab."
```
→ Four AT-SPI calls. Zero pixels read. Faster and more accurate than any OCR loop.

| Action | What it does |
|---|---|
| `click_element(description)` | Find widget by natural description and click it |
| `fill_field(label, text)` | Locate a form field by label and type into it |
| `read_focused_field()` | Read whatever field has focus |
| `list_buttons_in_window()` | List all actionable elements in the active window |
| `wait_for_element(description)` | Event-driven wait — no polling |

---

### F2 — D-Bus Universal Voice Remote

Auto-discover every service on the session and system D-Bus, introspect their XML, and synthesize voice commands — **no per-app plugins, ever**. Spotify, VLC, Firefox, NetworkManager, BlueZ, Hyprland, KDE Connect, libvirt, podman: all become voice-controllable the moment they appear on the bus. The top methods you use after 7 days auto-promote to named shortcuts in `~/.nora/dbus_blessed.json`.

```
"Pause Spotify, connect to wifi CoffeeShop, turn on night light,
and tell Firefox to open my GitHub notifications."
```
→ Four different D-Bus services. One sentence. No per-app code.

| Action | What it does |
|---|---|
| `media_play_pause()` / `media_next()` | MPRIS2 control for any media player |
| `wifi_connect(ssid)` | NetworkManager via D-Bus |
| `bluetooth_pair(mac)` | BlueZ pairing |
| `night_light_toggle()` | GNOME/KDE color temperature |
| `discover_dbus(query)` | Semantic search across the full service catalog |
| `dbus_call(service, object, method, args)` | Umbrella for the long tail |

---

### F3 — eBPF Causal Observer ("Why Engine")

NORA gains kernel-grade observability. A library of bpftrace one-shots runs on demand and pipes structured JSON output to the LLM, which speaks a plain-English causal explanation. The anomaly watchdog upgrades from "CPU > 90%" to "CPU > 90% — and here's the call stack."

```
"Why is my fan loud?"
```
→ 5-second `cpu_hotspot.bt` trace → *"Chrome's GPU process is at 340% CPU running a WebGL benchmark in tab 7. Want me to kill that tab?"*

| Action | Trace |
|---|---|
| `why_busy()` | CPU hot-path per process and function |
| `what_writes_disk()` | I/O by process and file |
| `who_opened(path)` | Which process touched a file/device |
| `top_talkers()` | Network traffic by process and remote IP |
| `watch_path(path)` | Persistent filesystem watcher |

The privileged bpftrace runner is isolated as `/usr/local/libexec/nora-bpf-runner` with `cap_bpf,cap_perfmon` capabilities — the main NORA process stays unprivileged.

---

### F4 — Time-Travel Filesystem + CRIU Session Continuity

NORA auto-snapshots before every risky action (file edit, package install, config change) using whatever your filesystem supports — **btrfs subvolumes** (instant, zero-copy), **ZFS snapshots**, or a targeted **rsync** fallback for ext4. Roll back by label or time with a single voice command.

Going further: **CRIU** checkpoints your entire process tree to disk. Pause a coding session — editor, terminals, dev server — and resume it across a full reboot.

```
"Snapshot now, label refactor-start."
... 20 minutes of work ...
"This is broken, roll back to refactor-start."
```
→ btrfs subvolume swap. 20 minutes erased. Editor reloads.

```
"Pause my coding session."
```
→ Close laptop. Next morning: **"Resume yesterday's coding session."** → CRIU restores nvim + terminals + dev server.

| Action | Risk |
|---|---|
| `snapshot_now(label)` | Low — instant COW snapshot |
| `list_snapshots()` | Low |
| `rollback_to(label_or_time)` | **High** — requires voice confirmation |
| `pause_session(name)` | **High** — requires voice confirmation |
| `resume_session(name)` | High |

---

### F5 — Adaptive Ambient (PipeWire + Wayland)

PipeWire graph mutation lets NORA do something no proprietary OS exposes as a consumer feature: **live re-route any application's audio**. Sidechain music on wakeword. Load RNNoise as a LADSPA filter on your mic. Tap Discord audio into NORA's transcription pipeline. Layered on top: Wayland compositor IPC (Sway / Hyprland / KWin) tiles windows reactively for inferred focus contexts.

```
"Enter focus mode for writing."
```
→ Firefox + Obsidian tile 60/40 on Sway. Slack notifications muted via D-Bus. Spotify switches to a lo-fi playlist via F2's MPRIS wrapper.

| Action | What it does |
|---|---|
| `duck_app_when_speaking(app)` | Sidechain audio on wakeword, restore after |
| `denoise_mic()` | Load RNNoise LADSPA filter on default mic source |
| `tap_app_audio(app)` | Route app output into NORA's transcription |
| `focus_mode(intent)` | Compositor retile + notifications + audio profile |

---

## Core Features

### Voice Control
- Push-to-talk (`Ctrl+\``) or always-on ambient mode
- Local Whisper transcription — nothing sent to a speech API
- Text input fallback for silent environments
- Remote mic support (`nora_remote.py`) — use your phone as a microphone

### Built-in Commands
| Category | Actions |
|---|---|
| Applications | Open/close apps, switch windows |
| Web | Search, open URLs |
| Music | Local playback, Apple Music, YouTube |
| Files | Open, move, delete (with confirmation) |
| System | Volume, brightness, shutdown, lock |
| Memory | Recall past commands, inject knowledge |
| Notifications | Voice reminders |
| Code | Screen intelligence, clipboard extraction |

### Cognitive Memory
- **Semantic store** — ChromaDB + `all-MiniLM-L6-v2` embeddings, everything persisted
- **Episodic memory** — full intent + outcome log per command
- **Behavioral model** — learns time-of-day patterns and command sequences
- **Workflow prediction** — `open_vscode` → NORA asks *"should I also open terminal?"*

### Proactive Intelligence
- Pattern engine detects recurring workflows (bigram frequency + time-of-day)
- Surfaces suggestions during idle — before you ask
- Frustration detection: repeated failures trigger a *"want me to ask Claude?"* offer
- Linux flagship actions record to cognitive memory — NORA learns which apps are AT-SPI-blind, which D-Bus services you use most, and when you tend to need snapshots

### Security — Powered by NeuroSym-AI

```python
# Voice is an untrusted attack surface — block injection before the LLM sees it
input_guard = Guard(rules=[PromptInjectionRule()], deny_above="high")

# Validate the action plan before execution
action_guard = Guard(rules=[
    destructive_needs_confirmation(),
    max_steps(15),
    no_path_outside_sandbox(["/home/user"]),
], deny_above="critical")
```

- **Input guard** — catches prompt injection, role-switch attacks, path traversal, exfiltration attempts
- **Action guard** — blocks runaway plans, enforces confirmation on destructive ops, prevents sandbox escapes
- **Full audit trace** — every blocked command logged with rule ID, severity, and offending text
- **0.48ms average overhead** — invisible in a voice pipeline

---

## Installation

### System dependencies

```bash
# AT-SPI2 screen control (F1)
sudo apt install python3-gi gir1.2-atspi-2.0 at-spi2-core ydotool

# eBPF observability (F3)
sudo apt install bpftrace linux-headers-$(uname -r)

# Time-travel snapshots (F4)
sudo apt install btrfs-progs criu

# Adaptive audio (F5)
sudo apt install pipewire pipewire-pulse wireplumber
```

### Python setup

```bash
# 1. Clone the Linux branch
git clone -b linux https://github.com/AaditPani-RVU/N.O.R.A.git
cd N.O.R.A

# 2. Install Python dependencies
pip install -r requirements.txt

# 3. Add your API key
echo "GROQ_API_KEY=your_key_here" > .env

# 4. Bootstrap privileged Linux helpers (one-time, polkit-mediated)
python nora_linux_setup.py

# 5. Run
python main.py
```

> **Ollama alternative:** set `llm.provider: ollama` in `config.yaml` and run `ollama serve` — no API key needed.

### Linux-specific notes

- `ydotool` needs `/dev/uinput`; the setup script installs a udev rule so your user's input group covers it — no sudo required at runtime
- The bpftrace helper runs as a capability-pinned binary (`cap_bpf,cap_perfmon`) — NORA's main process stays unprivileged
- CRIU + GPU processes: checkpointing PIDs holding `/dev/nvidia*` requires `experimental_gpu_checkpoint: true` in `config.yaml`
- btrfs snapshots of `/home` require `/home` to be its own subvolume (Fedora default: yes; Ubuntu: check with `findmnt /home`)

---

## Configuration

```yaml
llm:
  provider: "groq"              # groq | claude | ollama
  model: "llama-3.1-8b-instant"

transcriber:
  device: "cuda"                # cuda | cpu
  model_size: "distil-small.en"

speaker:
  voice: "en-GB-SoniaNeural"

neurosym:
  enabled: true
  input_deny_above: "high"
  max_plan_steps: 15

security:
  blocked_actions: []
  destructive_actions:
    - delete_file
    - shutdown
    - rollback_to         # snapshot rollback always needs confirmation

linux:
  experimental_gpu_checkpoint: false   # CRIU + NVIDIA (unstable)
  dbus_catalog_ttl_hours: 24
  snapshot_max_count: 100
  bpf_trace_max_seconds: 5
  bpf_trace_max_events: 10000
```

---

## Usage

```
Hold [Ctrl+`] → speak → release
```

**Core commands:**
```
"Open VS Code and terminal"
"Search for neuro-symbolic AI papers"
"Play something"
"Delete the file on my desktop called notes.txt"   ← triggers confirmation
"What did I work on yesterday?"
"Set a reminder in 10 minutes to check the build"
```

**Linux flagship commands:**
```
"Click the Submit button in Firefox"
"Fill in the username field with aadit"
"Pause Spotify"
"Connect to wifi HomeNetwork"
"Why is my fan loud?"
"What process is writing to disk?"
"Snapshot now, label before-upgrade"
"Roll back to before-upgrade"
"Pause my coding session"
"Resume my coding session"
"Denoise my microphone"
"Enter focus mode for coding"
```

**Voice shortcuts:**
| Phrase | Action |
|---|---|
| `stop` / `cancel` | Halt TTS and cancel pending steps |
| `exit` / `shut down nora` | Clean shutdown |
| `what did I do today` | Episodic memory recall |
| `show my patterns` | Display learned workflow habits |
| `discover dbus for <app>` | Introspect a D-Bus service on demand |

---

## Extending NORA

Drop a `.py` file into `plugins/`. Any function decorated with `@register` is live on the next run.

```python
from nora.command_engine import register

@register("brew_coffee")
def brew_coffee(parameters: dict) -> str:
    # your automation here
    return "Coffee brewing."
```

**Bundled plugins:**

| File | Actions |
|---|---|
| `plugins/github.py` | `gh_list_prs`, `gh_create_issue`, `gh_merge_pr` |
| `plugins/gmail.py` | `gmail_search`, `gmail_draft`, `gmail_send` |
| `plugins/google_calendar.py` | `cal_list`, `cal_create_event`, `cal_respond` |

---

## Roadmap

| Phase | Status | Description |
|---|---|---|
| 1 — Core pipeline | ✅ Done | Voice → Intent → Action → TTS |
| 2 — Cognitive Memory | ✅ Done | Semantic store, episodic memory, proactive engine |
| 3 — Screen Intelligence | ✅ Done | OCR, click-by-description, code explanation |
| 4 — Security | ✅ Done | NeuroSym-AI input + action guardrails |
| 5 — Linux F1: AT-SPI2 screen control | ✅ Done | Accessibility tree, ydotool, VLM fallback |
| 5 — Linux F2: D-Bus universal remote | ✅ Done | Full session + system bus introspection |
| 5 — Linux F3: eBPF Why Engine | ✅ Done | bpftrace scripts, anomaly drill-down |
| 5 — Linux F4: Time-travel + CRIU | ✅ Done | btrfs/zfs/rsync snapshots, process checkpoint |
| 5 — Linux F5: PipeWire + Wayland | ✅ Done | Audio graph mutation, compositor tiling |
| 6 — Autonomous Planning | 🔜 Planned | Multi-step goal decomposition, long-horizon tasks |

---

## Tech Stack

| Layer | Technology |
|---|---|
| Speech-to-Text | [faster-whisper](https://github.com/guillaumekynast/faster-whisper) `distil-small.en` |
| LLM | Groq `llama-3.1-8b-instant` / Claude / Ollama |
| Guardrails | [NeuroSym-AI](https://github.com/AaditPani-RVU/NeuroSym-AI) `v0.2.0` |
| Memory | [ChromaDB](https://www.trychroma.com/) + `sentence-transformers` |
| TTS | [edge-tts](https://github.com/rany2/edge-tts) `en-GB-SoniaNeural` |
| Screen control | `gi.repository.Atspi` (AT-SPI2) + `ydotool` + Qwen2-VL-2B (VLM fallback) |
| App automation | `dbus-next` (asyncio D-Bus) |
| Kernel observability | `bpftrace` (eBPF, BTF-aware) |
| Snapshots | `btrfs-progs` / `zfsutils` / `rsync` + `pycriu` (CRIU) |
| Audio | `pw-cli` / `pw-dump` (PipeWire) |
| Compositor | `i3ipc` (Sway) · `hyprctl` (Hyprland) · `org.kde.KWin` D-Bus (KDE) |
| Runtime | Python `asyncio`, `pydantic` |

---

## License

MIT © [Aadit Pani](https://github.com/AaditPani-RVU)
