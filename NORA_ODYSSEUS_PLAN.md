# NORA × Odysseus — Integration Plan & Free Model Routing

**Written:** 2026-08-05
**Scope:** every free-tier LLM provider worth wiring in, which model goes to which
job, and the full architecture for connecting NORA to Odysseus.

---

## 0. TL;DR

- NORA and Odysseus are **different categories** — an OS agent and a web
  workspace. Don't merge them. **Federate over MCP**, both directions.
- Both sides already speak MCP. `nora/mcp_bridge.py` is a working MCP *client*
  (stdio + http). Odysseus ships MCP *servers* in `mcp_servers/`. Direction B
  (NORA consuming Odysseus) is **a config edit with zero code**.
- The live pane is mostly already built: `nora/ui_server.py` exposes an
  authenticated WebSocket pushing `state` / `stage` / `notification` and
  accepting `command` / `ptt_start` / `ptt_end`. Odysseus needs a *view*, not a
  protocol.
- **TPM is the real ceiling on free tiers, not requests/day.** Plan around it.
- You can't have "no data leaves my machine" *and* "no local models." §3 is the
  split that gets most of the privacy back for one 8B model.

---

## PART 1 — Free provider catalog

> **Confidence markers.** ✅ = pulled from the provider's own docs on
> 2026-08-05. ⚠️ = aggregator/blog sourced, directionally right, verify before
> depending on it. Free tiers move constantly — re-check quarterly.

### 1.1 Groq ✅ *(official docs, 2026-08-05)*

Base URL: `https://api.groq.com/openai/v1`

| Model ID | RPM | RPD | TPM | TPD |
|---|---|---|---|---|
| `llama-3.1-8b-instant` | 30 | 14,400 | 6K | 500K |
| `llama-3.3-70b-versatile` | 30 | 1,000 | 12K | 100K |
| `openai/gpt-oss-120b` | 30 | 1,000 | 8K | 200K |
| `openai/gpt-oss-20b` | 30 | 1,000 | 8K | 200K |
| `qwen/qwen3.6-27b` | 30 | 1,000 | 8K | 200K |
| `groq/compound` | 30 | 250 | 70K | — |
| `groq/compound-mini` | 30 | 250 | 70K | — |
| `meta-llama/llama-prompt-guard-2-86m` | 30 | 14,400 | 15K | 500K |
| `meta-llama/llama-prompt-guard-2-22m` | 30 | 14,400 | 15K | 500K |
| `whisper-large-v3-turbo` | 20 | 2,000 | — | — |

**Notes**
- The famous "14.4k requests/day" only applies to `llama-3.1-8b-instant`.
  Everything good is **1,000 RPD**.
- **6K TPM on the 8B model is the binding constraint.** Two or three
  2,000-token document calls and you're throttled, regardless of the daily
  headroom. Same trap on `gpt-oss-120b` at 8K TPM.
- `groq/compound` has an unusually high 70K TPM but only 250 RPD — good for a
  few large-context calls per day, bad for chat.
- Supports enforced `response_format: json_object`. Already used by NORA
  (`llm.json_mode`).

**🔥 `llama-prompt-guard-2-86m` is directly relevant to us.** A dedicated
prompt-injection classifier, free, at 14,400 RPD / 15K TPM. That is a
near-unlimited budget for a *second opinion* on top of `neurosym_guard`. See
§4.4 — this is the cheapest real hardening available to NORA right now.

### 1.2 Cerebras ✅ *(official docs, 2026-08-05)*

Base URL: `https://api.cerebras.ai/v1`

| Model ID | Params | Free context | Speed |
|---|---|---|---|
| `gpt-oss-120b` | 120B | 65K | ~3,000 tok/s |
| `gemma-4-31b` | 31B | 65K | ~1,850 tok/s |
| `zai-glm-4.7` | 355B | 64K | ~1,000 tok/s |

Free tier: ~30 RPM, ~1M tokens/day ⚠️, context capped at 64–65K (paid gets 131K).

**⚠️ Two warnings.**
1. **`zai-glm-4.7` is scheduled for deprecation on 2026-08-17** — twelve days
   from this writing. Do not build anything on it.
2. The catalog is *volatile*. It has previously collapsed from a dozen models to
   two without notice. Treat Cerebras as raw speed, never as a dependency.

Still the best tokens/day of anything free, and by far the fastest.

### 1.3 Google AI Studio (Gemini) ⚠️

Base URL: `https://generativelanguage.googleapis.com/v1beta/openai/`

| Model | RPM | TPM | RPD |
|---|---|---|---|
| `gemini-3-flash` | 10 | 250K | 1,500 |
| `gemini-2.5-flash` | 10–15 | 250K–1M | 1,500 |
| `gemini-2.5-flash-lite` | 15 | — | 1,000 |

Official docs now punt to the AI Studio dashboard rather than publishing a
table, so these are aggregator numbers — **check your actual quota at
<https://aistudio.google.com/rate-limit>**.

- **250K TPM is 40× Groq's 8B budget.** This is the one free tier that survives
  long-context work.
- Free tier lost Pro access somewhere around May 2026; Flash and Flash-Lite
  only. Limits were cut 50–80% in Dec 2025 — the "generous Gemini" reputation is
  a year out of date, though it's still the best of the bunch on TPM.
- 🔒 **The free tier trains on your data.** Never route personal documents,
  email, or NORA voice transcripts here. See §3.

### 1.4 NVIDIA NIM ⚠️

Base URL: `https://integrate.api.nvidia.com/v1`

- 100+ models: DeepSeek R1, Llama Nemotron Ultra 253B, Qwen3 235B, Kimi.
- 40 RPM (upgradeable to 200 on request).
- **Credit-based: ~1,000 credits on signup, ~5,000 with the free NVIDIA
  Developer Program. 1 credit ≈ 1 request.**

Critically different from everything else here: credits **deplete and do not
refill**. Never make this a primary. It's a reserve for heavy one-off reasoning
(Deep Research, Compare) where model class matters and volume is low.

Odysseus already knows Nemotron's context window — `src/model_context.py:210`.

### 1.5 OpenRouter ⚠️

Base URL: `https://openrouter.ai/api/v1`

- 20 RPM. **50 requests/day free**; ~1,000/day after a one-time $10 credit
  purchase. The 20 RPM cap does not lift.
- 25–29 `:free` models at any given time — DeepSeek R1, Llama 4 Scout, Qwen3
  Coder 480B. Roster churns constantly; filter live at
  <https://openrouter.ai/models?q=:free>.

Value is **breadth from one credential**, not volume. Correct role: last link in
the fallback chain, and a way to reach a model nobody else hosts free.

### 1.6 Mistral ⚠️

Base URL: `https://api.mistral.ai/v1`

- Tier name: **"Experiment."** All models incl. Large, Codestral, Pixtral.
- ~1B tokens/month — enormous. But **2 RPM** ⚠️ (one source claims 60; assume
  the pessimistic number until you measure it).

Huge token budget behind a tiny request rate. That shape suits **batch and
background work**: overnight summarization, memory consolidation, digest
generation. Useless for interactive chat.

### 1.7 Everything else

| Provider | Base URL | Free limits | Verdict |
|---|---|---|---|
| SambaNova | `https://api.sambanova.ai/v1` | 10–30 RPM, persistent + $5 credit | Worth a key. Llama 405B free. |
| GitHub Models | `https://models.inference.ai.azure.com` | 10–15 RPM, 50–150 RPD, 8K in/4K out | GPT-5/o3/Grok access for free; tiny per-request cap |
| Cohere | native SDK | 20 RPM, ~1,000/month | ⛔ **non-commercial trial only** |
| Cloudflare Workers AI | Workers binding | 10,000 neurons/day | Only if already on CF |
| Zhipu (GLM) | `https://open.bigmodel.cn/api/paas/v4/` | undocumented | GLM-4.7-Flash free; opaque |
| HuggingFace | `https://api-inference.huggingface.co/v1` | ~$0.10/mo credits | Not viable |
| xAI | `https://api.x.ai/v1` | $25 signup credit | Trial, not free tier |
| DeepSeek | `https://api.deepseek.com` | 5M tokens / 30 days | Trial, not free tier |
| **Chutes** | — | — | ⛔ **free tier ended March 2026** |

---

## PART 2 — Routing plan

### 2.1 The rule that actually matters

> **Tokens-per-minute is the ceiling. Requests-per-day is marketing.**

A 2,500-token prompt against Groq's 6K TPM throttles on the third call — while
13,000 of your 14,400 daily requests sit unused. Route by **TPM headroom against
expected prompt size**, not by the RPD number on the landing page.

Corollary: for fallback, **dual-home the same open weights across two
providers** (`gpt-oss-120b` runs on both Groq and Cerebras). Failing over to an
*identical* model preserves behaviour mid-incident; failing over to a different
model changes your output quality silently.

### 2.2 High-grade — agentic / task work

Needs: reliable structured output, tool calling, reasoning. Ordered per role.

| Job | Primary | Fallback 1 | Fallback 2 |
|---|---|---|---|
| **Agent loop / tool calling** | `gpt-oss-120b` @ Cerebras | `openai/gpt-oss-120b` @ Groq | `gemini-3-flash` |
| **Deep Research** (long ctx) | `gemini-3-flash` (250K TPM) | `zai-glm-4.7` @ Cerebras ⚠️deprecating | Nemotron 253B @ NVIDIA |
| **Code / repo work** | `qwen3-coder-480b:free` @ OpenRouter | `codestral` @ Mistral | `gpt-oss-120b` @ Groq |
| **Hard reasoning (rare)** | DeepSeek R1 @ NVIDIA | `deepseek-r1:free` @ OpenRouter | — |

Cerebras first for the agent loop because ~3,000 tok/s makes multi-step ReAct
loops feel instant, and the agent loop is the most latency-sensitive thing you
run. Groq holds the identical model as a same-behaviour fallback.

### 2.3 General — conversational

Deliberately *different* models, so a bad day at one provider doesn't flatten
every surface at once.

| Job | Primary | Fallback 1 | Fallback 2 |
|---|---|---|---|
| **Main chat** | `gemini-3-flash` | `llama-3.3-70b-versatile` @ Groq | `gpt-oss-120b` @ Cerebras |
| **Second opinion** (`second_opinion.py`) | `mistral-large` | `llama-3.3-70b` @ SambaNova | GPT-5 @ GitHub Models |
| **Vision** | `gemini-3-flash` | `pixtral` @ Mistral | — |

A second-opinion layer is worthless if it queries the same family as the primary
— keep Mistral/SambaNova here specifically because they're *architecturally
unrelated* to the Gemini/Llama primaries.

### 2.4 Local — never leaves the box

| Job | Model | Why local |
|---|---|---|
| **NORA intent parsing** | `qwen3:8b` @ Ollama | Your literal spoken life |
| **Odysseus `utility_model`** | `qwen3:8b` @ Ollama | Titles/tags/summaries of your email + notes |
| **STT** | `faster-whisper distil-small.en` | Already local. Keep it. |
| **TTS** | Kokoro-82M (`speaker.backend: kokoro`) | Replaces edge-tts → removes Microsoft |
| **Embeddings** | `all-MiniLM-L6-v2` + ChromaDB | Already local |
| **Guard** | `neurosym_guard` + prompt-guard-2-86m | Local rules, cloud classifier as 2nd opinion |

`qwen3:8b` with Ollama's `format=`-schema constrained decoding makes invalid
intent JSON structurally impossible — better reliability *and* better privacy
than the cloud path it replaces. This is the single highest-value change in this
document.

---

## PART 3 — The privacy boundary

**Current state:** the storage layer is fully local (SQLite, `data/`, ChromaDB,
`nora_knowledge.json`, audit logs). The *inference* layer is not. Under the
routing above, five vendors see prompt content.

Two leaks worth naming explicitly:

1. **`STACK.md` lists Groq as NORA's intent LLM.** The README's "no data leaves
   your machine unless you configure it to" is technically true and practically
   false — it *is* configured to. Every voice command is transcribed locally,
   then shipped to Groq as text.
2. **`edge-tts` is a Microsoft cloud endpoint.** Every sentence NORA speaks is a
   network call. `nora/tts_local.py` (Kokoro) already exists — flip it.

**The split:** §2.4 moves the two highest-frequency, most personal, *least*
demanding workloads on-device for the cost of one 8B model. Cloud keeps the
heavy reasoning you were always going to send out. That's not "no data to
anyone," but it's the majority of sensitive traffic back in-house at minimal
hardware cost.

Non-negotiables:
- 🔒 Never route personal docs / email / voice transcripts to **Gemini free**
  (trains on data) or **GitHub Models**.
- SearXNG being self-hosted removes the *profile*, not the *query* — searches
  still reach upstream engines.
- If the quota router (§4.5) ships, **log provider-per-request**. Spreading load
  across five vendors means losing track of what went where. That log is the
  only way to answer "who saw this?" after the fact.

---

## PART 4 — Integration architecture

### 4.1 Principle: federate, don't merge

Odysseus is at PR #5885 and moving fast. Its value to you *is* that it keeps
improving without your effort. Any change that makes rebasing painful destroys
that value. So: **two repos, connected over MCP** — a stable protocol boundary
instead of a coupling to someone else's internals.

```
   ┌──────────────────┐  MCP (stdio) ─ Odysseus servers  ┌──────────────────┐
   │      NORA        │ ───────────────────────────────► │    Odysseus      │
   │  (native host)   │                                   │  (Docker)        │
   │                  │ ◄─────────────────────────────── │                  │
   │  mic·AT-SPI·D-Bus│  MCP (stdio) ─ NORA server        │ chat·docs·email  │
   │  eBPF·CRIU·guard │                                   │ calendar·research│
   └────────┬─────────┘                                   └────────┬─────────┘
            │              WebSocket (proxied)                     │
            └──────────────────────────────────────────────────────┘
                          live NORA pane in the UI
```

### 4.2 Direction B — NORA consumes Odysseus *(zero code, do this first)*

Odysseus's `mcp_servers/` are stdio MCP servers importing from its codebase.
NORA's `mcp_bridge.py` already speaks stdio. Add to `config.yaml`:

```yaml
mcp_servers:
  - name: odysseus_memory
    transport: stdio
    command: ["/home/aadit/Projects/odysseus/venv/bin/python",
              "-m", "mcp_servers.memory_server"]
    cwd: "/home/aadit/Projects/odysseus"
  - name: odysseus_rag
    transport: stdio
    command: ["/home/aadit/Projects/odysseus/venv/bin/python",
              "-m", "mcp_servers.rag_server"]
    cwd: "/home/aadit/Projects/odysseus"
  - name: odysseus_email
    transport: stdio
    command: ["/home/aadit/Projects/odysseus/venv/bin/python",
              "-m", "mcp_servers.email_server"]
    cwd: "/home/aadit/Projects/odysseus"
```

Tools register as `mcp_odysseus_email_<tool>` etc. and become LLM-callable
immediately. "What's in my inbox" spoken to NORA now hits Odysseus.

**Gotchas**
- Needs Odysseus's venv *and* its `ODYSSEUS_DATA_DIR` — hence `cwd`. Wrong cwd
  = wrong database or import failure.
- `memory_server.py` is owner-scoped (`_configured_owner()`). Set the user NORA
  acts as, or it returns nothing.
- If `mcp_bridge.py` has no `cwd` key yet, that's a ~5-line addition to
  `_MCPServer` subprocess spawn.

### 4.3 Direction A — Odysseus consumes NORA

Wrap NORA's command engine as an MCP server. **`mcpforge/` is the tool you built
for exactly this.** Then register it in Odysseus like any other MCP server.

Odysseus's agent gains hands: it can drive real applications, snapshot the
filesystem before writing, and query the eBPF why-engine.

Expose a **curated, semantic** surface:

| Tool | Backed by |
|---|---|
| `screen_read` | AT-SPI tree (`platform/linux/atspi_tree`) |
| `screen_act(target, action)` | `atspi_actions` |
| `open_app(name)` | command engine |
| `dbus_invoke(service, method)` | `dbus_invoker` (allowlisted services only) |
| `snapshot_create` / `snapshot_restore` | `time_travel`, `criu_session` |
| `why(pid)` | `why_engine` |

### 4.4 ⚠️ The trust boundary — read before shipping 4.3

Odysseus browses the web. If its research agent ingests a page containing a
prompt injection, and that agent can call `screen_act` → `ydotool`, you have
built a path from **an attacker-controlled webpage to keystrokes on your
desktop**.

Rules:

1. **The guard stays on NORA's side of the MCP boundary.** Every exposed tool
   dispatches through `neurosym_guard` + `risk.py` *inside NORA*, before
   execution. Then every Odysseus call is validated for free.
2. **Semantic tools only. Never a raw `exec` passthrough.** `open_app(name)`
   against an allowlist — not `run_command(str)`. Exposing primitives routes
   around your own safety layer.
3. **Destructive actions require `consent_memory` confirmation** even when the
   caller is Odysseus. An automated caller must not be able to auto-confirm.
4. **Add the Groq `llama-prompt-guard-2-86m` second opinion** (§1.1) on any
   input that originated outside NORA. 14,400 RPD is effectively unlimited for
   this, and `nora/second_opinion.py` is the natural home.
5. **Everything crossing the boundary lands in `nora_audit_log.jsonl`**, tagged
   with the calling origin.

This belongs in a threat-model doc on NORA's side. Odysseus has
`THREAT_MODEL.md`; NORA should too, and this is its first section.

### 4.5 The live pane

**NORA cannot run inside Odysseus.** It needs mic, PipeWire, AT-SPI, D-Bus,
ydotool — Docker can't reach any of it. NORA runs natively; the pane is a *view
onto* a daemon living outside the browser.

**The protocol already exists.** `nora/ui_server.py`:

```
NORA → client:  {"type":"state", "data":{...}}      every state change
                {"type":"stage", "stage":"listening"}
                {"type":"notification", "message":"..."}
client → NORA:  {"type":"command", "text":"open chrome"}
                {"type":"ptt_start"} / {"type":"ptt_end"}
```

Push-based state, pipeline stage for the listening animation, proactive
notifications, push-to-talk. Exactly a live pane's needs.

**Proxy the WebSocket through the Odysseus backend.** Do not connect the browser
straight to `ws://localhost:8765`:
- an HTTPS-served page blocks `ws://` as mixed content;
- a direct connection bypasses Odysseus auth entirely;
- it breaks the moment you open the UI from anywhere but the host.

One new Odysseus route proxying to NORA, authenticating with `NORA_API_TOKEN` on
the backend hop. Then Odysseus's session auth gates the pane, and it works over
Tailscale. From a phone, the pane becomes a *remote control* for the desktop
NORA — which `nora_remote.py` and `remote_mic.py` already anticipate.

**UI shape.** Build it as a **new "Dashboard" view alongside** the existing
sections — *not* a rewrite of Odysseus's shell.

- `static/style.css` has ~780 grid/panel/pane rules and every feature module
  renders into the main view. A 2×2 shell rewrite is the most
  merge-conflicting change possible; it means abandoning rebases forever.
- v1: **iframes** for the existing panes. Views are hash-addressable
  (`static/js/init.js:19` reads `location.hash`; chat/email/document/sessions
  all route through it), so each pane is an iframe at the right hash. Inelegant,
  but `chat.js` / `calendar.js` / `document.js` assume singleton DOM ownership,
  and making them multi-instance is a large refactor in code you don't own.
- Only the **NORA pane** is a native component — you're writing that anyway.
- **Don't hard-code 2×2.** Chat and the document editor need width. What
  actually works is a persistent NORA rail plus 2–3 resizable panes.

### 4.6 Events (later)

Odysseus has `routes/webhook/webhook_routes.py` and an `ntfy` container in
compose. NORA's proactive engine POSTs there, so ambient suggestions surface in
the Odysseus UI without either side importing the other.

### 4.7 Quota-aware router — build it in NORA

Odysseus's fallback chain (`resolve_chat_fallback_candidates()`,
`src/endpoint_resolver.py:445`) is **availability-based and stateless**. It fires
when an endpoint is disabled or a call hard-fails, and it does not remember that
Groq 429'd ninety seconds ago.

Given §2.1, that's the gap that matters. Build a router that:
- tracks per-endpoint `(requests, tokens)` per rolling window;
- parses `retry-after` / rate-limit headers into a reset time;
- **pre-emptively skips** exhausted endpoints instead of spending a round trip;
- decays penalties so providers return automatically;
- logs provider-per-request (§3).

Skip LLM-based "is this prompt hard?" classification — it costs latency and
burns a request from the quota you're conserving, and role-based assignment
already separates cheap from expensive work.

**Build it in NORA**, not Odysseus. `nora/endpoint_trust.py` (67 lines) is
already the seed, and it's your codebase.

---

## PART 5 — Build order

Sequenced so nothing unsafe ships before the thing that makes it safe.

- [ ] **1. Direction B** (§4.2) — config only, works today. Immediate payoff.
- [ ] **2. Local models** (§2.4) — `ollama pull qwen3:8b`; point NORA intent
      parsing + Odysseus `utility_model` at it. Flip `speaker.backend: kokoro`.
- [ ] **3. Guard tests** — ⚠️ **blocks step 5.** `neurosym_guard.py`, `risk.py`,
      `reversible.py`, `consent_memory.py` are 15,273 lines of unverified code
      whose failure mode is destroying data. Adversarial cases for all 9
      injection categories + every destructive path. *(NORA currently has 3 test
      files; Odysseus has 743 — this is the real gap between the projects.)*
- [ ] **4. `intent_parser.py` tests** (777 lines) — golden-file tests over
      recorded transcripts. NL→JSON is where models misbehave.
- [ ] **5. Direction A** (§4.3) — only after 3. Curated semantic tools, guard
      inside NORA, prompt-guard second opinion.
- [ ] **6. WebSocket proxy route** in Odysseus (§4.5).
- [ ] **7. NORA pane** as a single Dashboard view.
- [ ] **8. Quota router** (§4.7).
- [ ] **9. Grid layout** — last, once you know what belongs beside it.

**Housekeeping alongside:** split commits by subsystem (the F1–F5 commit
contains five subsystems — nothing in it is bisectable); move `nora_*.json`,
`nora.log`, `nora_cognitive_db/` under `data/`; break the flat ~50-module `nora/`
namespace into subpackages, mirroring Odysseus's recent
`routes/{document,webhook,vault}/` refactor.

*(`.env` and runtime state are correctly gitignored — verified. No leak.)*

---

## Sources

Verified 2026-08-05. Official docs marked ✅ above; the rest are aggregators and
should be re-checked before you depend on a number.

- [Groq — rate limits (official)](https://console.groq.com/docs/rate-limits)
- [Cerebras — model catalog (official)](https://inference-docs.cerebras.ai/models/overview)
- [Gemini — rate limits (official, defers to dashboard)](https://ai.google.dev/gemini-api/docs/rate-limits)
- [Gemini — your live quota](https://aistudio.google.com/rate-limit)
- [OpenRouter — free models compared](https://openrouter.ai/blog/tutorials/free-llm-apis-compared/)
- [awesome-free-llm-apis (curated list)](https://github.com/amardeeplakshkar/awesome-free-llm-apis)
- [Every Free AI API in 2026 — Awesome Agents](https://awesomeagents.ai/tools/free-ai-inference-providers-2026/)
- [Free LLM API Tiers 2026 — Ian Paterson](https://ianlpaterson.com/blog/free-llm-api-2026/) *(source of the TPM-is-the-real-ceiling analysis)*
- [Cerebras free tier](https://www.getaiperks.com/en/ai/cerebras-free-tier-guide)
- [NVIDIA NIM free tier](https://yangmao.ai/en/providers/nvidia-build/free-tier/)
- [Mistral free tier](https://pricepertoken.com/endpoints/mistral/free)
- [GitHub Models free tier](https://free-llm-apis.pages.dev/providers/github-models/)
- [Chutes free tier (ended)](https://free-llm.com/provider/chutes-ai)
