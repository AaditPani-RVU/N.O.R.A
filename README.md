<div align="center">

# NORA
### *Never Off, Rarely Asked*

[![Python](https://img.shields.io/badge/python-3.10%2B-blue?style=flat-square&logo=python&logoColor=white)](https://python.org)
[![License: MIT](https://img.shields.io/badge/license-MIT-green?style=flat-square)](LICENSE)
[![Powered by NeuroSym](https://img.shields.io/badge/guardrails-neurosym--ai-blueviolet?style=flat-square)](https://github.com/AaditPani-RVU/NeuroSym-AI)
[![Platform](https://img.shields.io/badge/platform-Linux-orange?style=flat-square&logo=linux&logoColor=white)]()
[![Tests](https://img.shields.io/badge/tests-232%20passing-brightgreen?style=flat-square)]()
[![Status](https://img.shields.io/badge/status-active-brightgreen?style=flat-square)]()

**A local, voice-controlled AI assistant with neuro-symbolic guardrails —**  
**built for Linux with capabilities that are literally impossible on Windows or macOS.**

> This is the **Linux flagship branch**. It adds five kernel-native features on top of the core assistant:  
> AT-SPI2 screen control · D-Bus universal remote · eBPF causal observer · time-travel filesystem · adaptive PipeWire audio.

<sub>
  <a href="#what-is-nora">What is NORA</a> ·
  <a href="#architecture">Architecture</a> ·
  <a href="#linux-flagship-features">Flagship Features</a> ·
  <a href="#core-features">Core</a> ·
  <a href="#installation">Install</a> ·
  <a href="#configuration">Config</a> ·
  <a href="#usage">Usage</a> ·
  <a href="#remote--mobile">Remote &amp; Mobile</a> ·
  <a href="#extending-nora">Extend</a> ·
  <a href="#tech-stack">Stack</a>
</sub>

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
  └────────┬─────────────────────────────────┬───────────────────────┘
           │                                 │
           ▼                                 ▼
   local speakers (pygame)          WebSocket audio relay
                                    phone on the tailnet
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
- **Remote audio out** — replies mirror to any browser on your tailnet, so NORA can talk on your phone while executing on the laptop ([details](#remote--mobile))

### Dashboard

A single-file HUD at **`http://localhost:8766`** (`nora/static/index.html`) — no build step,
no framework, no CDN. It opens on the laptop and on any phone on the tailnet.

- **Orb + stage bar** — the pipeline as it runs: `LISTEN › STT › GUARD › LLM › ACT › SPEAK`
- **Transcript** — both sides of the conversation, each reply tagged with the model that
  produced it and its round-trip latency
- **Live panels** — system stats, camera, response-time histogram, weather, uptime,
  Spotify transport, command log
- **Appearance** — accent colour, and toggles for grid, scanlines, aurora, particles,
  spotlight, typewriter and the boot sequence

Idle is a designed state, not a blank box: the transcript sits behind a corner reticle and
a slow equaliser so a waiting assistant never reads as a crashed one. Everything is
theme-driven off one `--rgb` accent token, so the swatch picker retints the whole HUD —
orb, bars, borders and glow — in one step.

### Written Output — `claude_logs/`

Every document NORA produces — a change log, a summary, a note — lands in **`claude_logs/`**
inside the project root, named `YYYY-MM-DD_slug.md`. One directory, always the same place.

```
"Write a log for the vision guest-mode change"
  → claude_logs/2026-08-20_vision-guest-mode.md
"What logs do you have?"
  → recent_logs — reads the directory, no guessing
```

NORA owns persistence, not the model. When a request needs a document, Claude runs with
**no file-writing tools at all** and returns the content; `nora/claude_logs.py` writes it.
That inversion is deliberate — a one-shot `claude -p` has nobody to approve a write, so
left to itself it would narrate a save it could never perform, and NORA would speak the
phantom path as fact. Relative destinations (`logs/x.md`, a bare `notes.md`) are resolved
to the same folder rather than to whatever directory NORA was launched from.

### Built-in Commands
| Category | Actions |
|---|---|
| Applications | Open/close apps, switch windows |
| Web | Search, open URLs |
| Music | Spotify over MPRIS D-Bus — play by song/artist/album/playlist, transport, shuffle, repeat |
| Files | Open, move, delete (with confirmation) |
| System | Volume, brightness, shutdown, lock |
| Memory | Recall past commands, inject knowledge |
| Notifications | Voice reminders |
| Code | Screen intelligence, clipboard extraction |

### Model Routing

Every LLM call goes through `nora/model_router.py` under a **role** — `chat`, `intent`,
`research`, `live_search`, `reasoning` — and each role is an *ordered* chain of candidates
in `config.yaml`. The first candidate not in rate-limit cooldown wins. A 429 sends that
candidate to cooldown (honouring `Retry-After` when the provider sends one) and the chain
falls through; any other error — auth, connection, timeout, empty response — falls through
immediately. Roles are what let a fast model answer small talk while a slower, stronger one
handles `deep_reasoning`.

**Hybrid-reasoning models must have their thinking channel suppressed.** This is the one
rule that is not optional. A model like `gpt-oss` or Nemotron emits a scratchpad before its
answer; providers normally return it in a separate field, but when the thinking runs long it
overflows into `content` — and since NORA speaks `content`, she reads her own scratchpad out
loud:

> *"We need to recall what we previously told the user about Kanye West. So now user asks…"*

Each provider spells the switch differently, so it lives per-candidate in `extra_body`:

```yaml
- name: groq_gpt_oss_120b          # Groq
  extra_body:
    reasoning_format: "hidden"

- name: nvidia_nemotron_nano_30b   # NVIDIA
  extra_body:
    chat_template_kwargs:
      thinking: false
```

Two nets sit under that, because the config only protects models someone remembered to
configure:

1. `model_router.complete(validate=...)` — a rejected reply is treated exactly like a failed
   call, so a leak falls through to the next model instead of reaching the speaker.
2. `conversation.for_speech()` — strips `<think>` blocks and any surviving analysis
   sentences on the way to TTS.

The detector (`conversation.looks_like_analysis`) distinguishes a scratchpad from a real
answer by **who the user is to the sentence**: a leak talks *about* them in the third person
("we need to recall what we told the user"), a genuine reply talks *to* them ("we need to
get you set up with an API key"). Guarding that distinction matters — the validator gates
real answers, so a false positive costs the user a response.

If you add a reasoning-capable model to any spoken role, set its suppression flag and add a
sample to `tests/test_conversation.py::LeakedReasoningTest`.

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

**Confirm ≠ block.** The two outcomes are distinct and gated separately. A plan that merely
*needs consent* — `create_file`, `move_file`, anything in `DESTRUCTIVE_ACTIONS` — is routed to
a spoken *"create file. Confirm?"*, while only a hard-deny (`deny_above: critical`, e.g. a path
outside the sandbox) refuses outright. The distinction lives in `hard_denied`, not in
neurosym's `ok`, which is simply "zero violations" — conflating them turns every ordinary file
write into a refusal.

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
  model: "openai/gpt-oss-120b"

llm_router:                     # ordered fallback chain per role
  roles:
    chat:
      - name: groq_gpt_oss_120b
        model: "openai/gpt-oss-120b"
        extra_body:
          reasoning_format: "hidden"      # or NORA speaks her own scratchpad
      - name: nvidia_nemotron_nano_30b    # survives a full Groq outage
        model: "nvidia/nemotron-3-nano-30b-a3b"
        extra_body:
          chat_template_kwargs:
            thinking: false

transcriber:
  device: "cuda"                # cuda | cpu
  model_size: "distil-small.en"

speaker:
  voice: "en-GB-SoniaNeural"

websocket_api:
  enabled: true
  port: 8765
  relay_audio: true             # mirror spoken replies to phones on the tailnet

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
| `what logs do you have` | List recent documents in `claude_logs/` |

---

## Remote & Mobile

Drive NORA from your phone from anywhere: **commands execute on the laptop, the voice comes
out of your phone.** The laptop keeps speaking through its own speakers too — the relay is a
mirror, not a handoff, so nothing changes about local behaviour.

```
   ┌─── phone (anywhere) ─────────┐          ┌─── laptop (tailnet) ───────────┐
   │  dashboard in the browser    │          │  listener → intent → action    │
   │    ├─ typed / PTT command  ──┼── wss ──▶│                                │
   │    └─ <audio> playback     ◀─┼── wss ───┼── edge-tts MP3 chunks          │
   │       "TAP FOR AUDIO" once   │          │  …and pygame, locally, as ever │
   └──────────────────────────────┘          └────────────────────────────────┘
                        WireGuard via Tailscale — no port forwarding
```

### 1. Put both devices on a tailnet

```bash
# laptop (Debian/Ubuntu)
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up

# phone: install Tailscale from the Play Store, sign in with the same account
```

That's the whole transport. No `tailscale serve`, no certificates, no port forwarding —
the tailnet is a private network and both ports are reachable directly on it.

### 2. Open the dashboard on your phone

```
http://<laptop-tailscale-ip>:8766/?token=<NORA_API_TOKEN>
```

Find the IP with `tailscale ip -4`, and the token with `grep NORA_API_TOKEN .env`.
The token is remembered in `localStorage`, so later visits need only the bare URL.
Omit it entirely if `NORA_API_TOKEN` is unset.

### 3. Tap **TAP FOR AUDIO** once

Android blocks programmatic playback until the page has seen a real user gesture. One tap
per session unlocks it; the button then hides itself. If playback is ever refused later it
reappears rather than failing silently.

Replies arrive as MP3 chunks, one sentence at a time, queued in order — so remote playback
tracks local playback instead of waiting for the whole reply to synthesise. Saying *"stop"*
clears the queue on both ends.

| Setting | Where | Meaning |
|---|---|---|
| `websocket_api.enabled` | `config.yaml` | Master switch for the socket API |
| `websocket_api.relay_audio` | `config.yaml` | Mirror spoken audio to clients (default `true`) |
| `NORA_API_TOKEN` | `.env` | Require token auth on the socket |
| `NORA_HOST` | `.env` | Target for `nora_remote.py` — set to the tailnet name |

> **Note** — the relay is strictly a side channel. If no client is connected NORA skips the
> encoding entirely, and every failure path inside `nora/audio_relay.py` is swallowed: a
> browser that is absent, slow, or throwing can never stall the machine actually speaking.

### Issuing commands from the phone

Tap **⌨ TEXT MODE** and type. The command posts to `/type_command`, runs through the full
pipeline on the laptop, and the spoken reply comes back to your phone.

The **CLICK TO SPEAK** button is *not* the phone's microphone — it triggers the laptop's mic
over `POST /ptt`, which is useless when you're not in the room. Speaking into the phone
needs one of:

| Route | Status |
|---|---|
| Text mode over the tailnet | ✅ Works today |
| `nora_remote.py` with `NORA_HOST` set to the tailnet IP | ✅ Works — needs Python (Termux on Android) |
| Browser mic capture in the dashboard | ❌ Not implemented — would need `getUserMedia` + HTTPS via `tailscale serve` |

### Optional — HTTPS

Only required if you add browser mic capture later, since `getUserMedia` refuses to run
outside a secure context:

```bash
tailscale serve --bg 8766                    # needs MagicDNS + HTTPS certs enabled
tailscale serve --bg --set-path=/ws 8765     # in the Tailscale admin console
```

The dashboard already handles this: the socket switches to `wss://` automatically when the
page is served over TLS.

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

**Or build an MCP server** (works in NORA, Claude Code, and Claude Desktop at once):
`python -m mcpforge new myserver` scaffolds a ready-to-run server — decorate plain
Python functions with `@server.tool()` and the JSON Schema, protocol handshake, and
transports are handled for you (zero dependencies, `mcpforge/`). Add it under
`mcp_servers:` in `config.yaml` and every tool becomes voice-callable. See STACK.md.

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
| 6 — Remote & Mobile | ✅ Done | Tailscale reach, WebSocket audio relay, phone playback |
| 7 — Autonomous Planning | 🔜 Planned | Multi-step goal decomposition, long-horizon tasks |

---

## Tech Stack

| Layer | Technology |
|---|---|
| Speech-to-Text | [faster-whisper](https://github.com/guillaumekynast/faster-whisper) `distil-small.en` |
| LLM | Groq `openai/gpt-oss-120b` / Claude / Ollama |
| Web research | Groq `groq/compound-mini` (live search) → Brave / DuckDuckGo snippets |
| Guardrails | [NeuroSym-AI](https://github.com/AaditPani-RVU/NeuroSym-AI) `v0.2.0` |
| Memory | [ChromaDB](https://www.trychroma.com/) + `sentence-transformers` |
| TTS | [edge-tts](https://github.com/rany2/edge-tts) `en-GB-SoniaNeural` + `pygame.mixer` |
| Remote transport | [Tailscale](https://tailscale.com/) (WireGuard) + `websockets` · base64 MP3 relay |
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
