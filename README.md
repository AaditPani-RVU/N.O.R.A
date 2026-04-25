<div align="center">

# NORA
### *Never Off, Rarely Asked*

[![Python](https://img.shields.io/badge/python-3.10%2B-blue?style=flat-square)](https://python.org)
[![License: MIT](https://img.shields.io/badge/license-MIT-green?style=flat-square)](LICENSE)
[![Powered by NeuroSym](https://img.shields.io/badge/guardrails-neurosym--ai-blueviolet?style=flat-square)](https://github.com/AaditPani-RVU/NeuroSym-AI)
[![Platform](https://img.shields.io/badge/platform-Windows-lightgrey?style=flat-square)]()
[![Status](https://img.shields.io/badge/status-active-brightgreen?style=flat-square)]()

**A local, voice-controlled AI assistant with neuro-symbolic guardrails.**  
Built for developers who want to automate everything — without giving a cloud service access to their life.

</div>

---

## What is NORA?

NORA is a fully local voice AI that listens for your commands, parses intent through a language model, validates every action through a symbolic safety layer, and executes against your OS — all in under a second, with no data leaving your machine unless you configure it to.

She learns your habits, anticipates your next move, remembers everything you've done, and will block you from doing something destructive without asking first.

The name is literal: **Never Off, Rarely Asked** — the proactive intelligence engine surfaces suggestions before you open your mouth.

---

## Architecture

```
  Voice / Text Input
         │
         ▼
  ┌─────────────────┐
  │  Whisper STT    │  faster-whisper · local GPU · distil-small.en
  └────────┬────────┘
           │
           ▼
  ┌─────────────────────────────────────────────────────┐
  │  NeuroSym Input Guard                               │  ← your voice is untrusted input
  │  PromptInjectionRule · 9 attack categories · <1ms   │
  └────────┬──────────────────────────────────────────--┘
           │
           ▼
  ┌─────────────────┐
  │  Intent Parser  │  Groq · Claude · Ollama (provider-agnostic)
  └────────┬────────┘
           │
           ▼
  ┌─────────────────────────────────────────────────────┐
  │  NeuroSym Action Guard                              │
  │  destructive_needs_confirmation · max_steps(15)     │
  │  no_path_outside_sandbox · full audit trace         │
  └────────┬──────────────────────────────────────────--┘
           │
           ▼
  ┌─────────────────┐
  │  Command Engine │  20+ built-in actions · plugin support
  └────────┬────────┘
           │
           ▼
  ┌─────────────────┐
  │  Windows API /  │  pyautogui · pywin32 · subprocess
  │  System Calls   │
  └────────┬────────┘
           │
           ▼
  ┌─────────────────┐
  │  Edge TTS       │  en-GB-SoniaNeural · streaming · no latency
  └─────────────────┘
```

---

## Features

### Voice Control
- Push-to-talk (`Ctrl+\``) or always-on ambient mode
- Local Whisper transcription — nothing sent to a speech API
- Text input fallback for silent environments

### Commands (20+ built-in)
| Category | Actions |
|---|---|
| Applications | Open/close apps, switch windows |
| Web | Search Google, open URLs |
| Music | Local playback, Apple Music, YouTube |
| Files | Open, move, delete (with confirmation) |
| System | Volume, brightness, shutdown, lock |
| Memory | Recall past commands, inject knowledge |
| Notifications | Set voice reminders |

### Cognitive Memory System
- **Semantic store** — ChromaDB + `all-MiniLM-L6-v2` embeddings persist everything you've done
- **Episodic memory** — full intent + outcome logging
- **Behavioral model** — learns time-of-day patterns and command sequences
- **Workflow prediction** — `open_vscode` → NORA asks "should I also open Terminal?"

### Proactive Intelligence
- Pattern engine detects recurring workflows (bigram frequency + time-of-day)
- Surfaces suggestions after idle periods — before you ask
- Frustration detection: repeated failures trigger a "want me to ask Claude for help?" offer

### Security — Powered by NeuroSym-AI
NORA uses [NeuroSym-AI](https://github.com/AaditPani-RVU/NeuroSym-AI) — a neuro-symbolic guardrail library — as its primary safety layer:

```python
# Voice is an untrusted attack surface — block injection before the LLM sees it
input_guard = Guard(rules=[PromptInjectionRule()], deny_above="high")

# Validate the action plan before execution
action_guard = Guard(rules=[
    destructive_needs_confirmation(),
    max_steps(15),
    no_path_outside_sandbox(["C:/Users/..."]),
], deny_above="critical")
```

- **Input guard**: catches prompt injection, role-switch attacks, path traversal, exfiltration attempts, and more — directly on the raw voice transcription
- **Action guard**: blocks runaway plans, enforces confirmation on destructive operations, and prevents sandbox escapes
- **Full audit trace**: every blocked command is logged with rule ID, severity, and the offending text
- **0.48ms average overhead** — invisible in a voice pipeline

---

## Prerequisites

| Requirement | Notes |
|---|---|
| Python 3.10+ | |
| CUDA GPU (optional) | Speeds up Whisper and embeddings; CPU fallback available |
| [Groq API key](https://console.groq.com) | Free tier. Or configure Claude / Ollama instead |
| Microphone | Any standard input device |

---

## Installation

```bash
# 1. Clone
git clone https://github.com/AaditPani-RVU/NORA.git
cd NORA

# 2. Install dependencies
pip install -r requirements.txt

# 3. Add your API key
echo "GROQ_API_KEY=your_key_here" > .env

# 4. Run
python main.py
```

> **Ollama alternative:** set `llm.provider: ollama` in `config.yaml` and run `ollama serve` — no API key needed.

---

## Configuration

All settings live in `config.yaml`. Key options:

```yaml
llm:
  provider: "groq"              # groq | claude | ollama
  model: "llama-3.1-8b-instant"

transcriber:
  device: "cuda"                # cuda | cpu
  model_size: "distil-small.en"

speaker:
  voice: "en-GB-SoniaNeural"   # any edge-tts voice

neurosym:
  enabled: true
  input_deny_above: "high"      # info | low | medium | high | critical
  max_plan_steps: 15

security:
  blocked_actions: []           # actions NORA will never execute
  destructive_actions:          # always require voice confirmation
    - delete_file
    - shutdown
```

---

## Usage

```
Hold [Ctrl+`] → speak → release
```

**Example commands:**
```
"Open VS Code and terminal"
"Search for neuro-symbolic AI papers"
"Play something"
"Delete the file on my desktop called notes.txt"   ← triggers confirmation
"What did I work on yesterday?"
"Set a reminder in 10 minutes to check the build"
"Shut down NORA"
```

**Voice shortcuts:**
| Phrase | Action |
|---|---|
| `stop` / `cancel` | Halt TTS + cancel pending steps |
| `exit` / `shut down nora` | Clean shutdown |
| `what did I do today` | Episodic memory recall |
| `show my patterns` | Display learned workflow habits |

---

## Extending NORA

Drop a `.py` file into `~/.nora/plugins/`. Any function decorated with `@register` is live on the next run — no core changes needed.

```python
from nora.command_engine import register

@register("brew_coffee")
def brew_coffee(parameters: dict) -> str:
    # your automation here
    return "Coffee brewing."
```

---

## Roadmap

| Phase | Status | Description |
|---|---|---|
| 1 — Core pipeline | ✅ Done | Voice → Intent → Action → TTS |
| 2 — Cognitive Memory | ✅ Done | Semantic store, episodic memory, proactive engine |
| 5 — Security | ✅ Done | NeuroSym-AI input + action guardrails |
| 3 — Screen Intelligence | 🔄 In progress | OCR, UI element detection, context from screen |
| 4 — Autonomous Planning | 🔜 Planned | Multi-step goal decomposition, long-horizon tasks |

---

## Tech Stack

| Layer | Technology |
|---|---|
| Speech-to-Text | [faster-whisper](https://github.com/guillaumekynast/faster-whisper) (`distil-small.en`) |
| LLM | Groq `llama-3.1-8b-instant` / Claude / Ollama |
| Guardrails | [NeuroSym-AI](https://github.com/AaditPani-RVU/NeuroSym-AI) `v0.2.0` |
| Memory | [ChromaDB](https://www.trychroma.com/) + `sentence-transformers` |
| TTS | [edge-tts](https://github.com/rany2/edge-tts) `en-GB-SoniaNeural` |
| Automation | `pyautogui`, `pywin32`, `subprocess` |
| Runtime | Python `asyncio`, `pydantic` |

---

## License

MIT © [Aadit Pani](https://github.com/AaditPani-RVU)
