# STACK.md — Best Free Tool per Pipeline Stage

Every stage of NORA's pipeline, the free tool chosen for it, and why.
"Free" here means free to run indefinitely — permissive open source, or a
cloud free tier generous enough for single-user daily use. Verified July 2026.

| Stage | Tool in use | Why it's the best free option | Runner-up |
|---|---|---|---|
| Wakeword | **openWakeWord** (optional) | Apache-2.0, pre-trained models, CPU-only, no per-key licensing (Porcupine's free tier is keyed and limited) | Porcupine free tier |
| VAD | **faster-whisper built-in** (Silero) | Silero VAD ships inside faster-whisper's `vad_filter=True` — already on | standalone silero-vad |
| STT | **faster-whisper** (`distil-small.en`) | CTranslate2 runtime is ~4× faster than openai-whisper at the same accuracy; distil models halve latency again. MIT | whisper.cpp |
| Intent LLM (cloud) | **Groq free tier** (`openai/gpt-oss-120b`) | Most permissive free tier of any provider at ~10× GPU-API speed; supports enforced JSON mode. Groq retired the llama-3.x models — `nora.preflight.check_models` now verifies every configured model still exists | Google AI Studio free tier |
| Web research | **Groq `groq/compound-mini`** | Agentic model with a server-side search tool: it runs its own queries and answers from what it read, so "what's happening right now" is answerable at all. Falls back to Brave/DDG snippets summarised by the router's `research` role | Tavily / Perplexity API |
| Intent LLM (local) | **Ollama + qwen3:8b** | Best small-model tool-calling/JSON accuracy in 2026 tests; `format=`-schema constrained decoding makes invalid JSON impossible | llama3-groq-8b-tool-use |
| Any other free endpoint | `llm.api_base` + `llm.api_key_env` | OpenRouter/Cerebras/Together free tiers are OpenAI-compatible — swap via two config keys, zero code | — |
| TTS (default) | **edge-tts** | Microsoft neural voices, unmetered, no key. Cloud-dependent is the only caveat | — |
| TTS (offline) | **Kokoro-82M** via `speaker.backend: kokoro` | Best local TTS of 2026: Apache-2.0, 82M params, faster than realtime on CPU, near-cloud quality | Piper (for Pi-class hardware) |
| Embeddings | **all-MiniLM-L6-v2** (config: `embedder_model`) | Still the best size/speed default. Upgrading to `BAAI/bge-small-en-v1.5` scores higher but requires re-embedding the existing ChromaDB store — do it deliberately, not silently | bge-small-en-v1.5 |
| Vector memory | **ChromaDB** | Local, persistent, zero-config, Apache-2.0 | sqlite-vec |
| Guardrails | **neurosym-ai** | Already integrated; input + action stages | — |
| Tool protocol | **MCP** via `mcpforge/` (ours) + `nora/mcp_bridge.py` | Open standard; one server works in NORA, Claude Code, and Claude Desktop simultaneously | — |

## Reliability upgrades wired in (July 2026)

- **Enforced JSON at the decoder** — Groq/OpenAI-compatible calls send
  `response_format: json_object` (`llm.json_mode`, default on, falls back
  automatically if an endpoint rejects it). Ollama calls send the full intent
  JSON Schema as `format` (`llm.structured_output`, default on, needs
  Ollama ≥ 0.5). This removes the "LLM wrapped the JSON in prose/fences"
  failure class instead of retrying around it.
- **Offline TTS fallback** — `nora/tts_local.py` (Kokoro). Enable via
  `speaker.backend: kokoro`; degrades back to edge-tts per chunk on any error.
- **`health_check` voice command** — `nora/health.py` sweeps all 11 subsystems
  (mic, STT, TTS, LLM, memory, wakeword, PipeWire, AT-SPI, D-Bus, input
  injection, MCP) in under a second and names exactly what is degraded and why.

## One-line installs for the optional pieces

```bash
pip install openwakeword          # wakeword
pip install dbus-next             # D-Bus voice remote (F2)
pip install kokoro-onnx           # offline TTS; model files: github.com/thewh1teagle/kokoro-onnx/releases
sudo apt install ydotool          # input injection on Wayland
```

## Building MCP servers (mcpforge)

Any Python function becomes a voice-callable tool:

```python
from mcpforge import MCPServer
server = MCPServer("myserver")

@server.tool()
def do_thing(arg: str, count: int = 1) -> str:
    """One-line description the LLM sees.

    arg: what to operate on
    count: how many times
    """
    ...

server.run()
```

- Scaffold: `python -m mcpforge new <name>` (writes the file, prints the
  NORA `config.yaml` snippet and the `claude mcp add` line).
- Schema is generated from type hints (`str/int/float/bool/list[str]/Literal`,
  optionals via `| None`, defaults) and docstring `name: description` lines.
- Transports: stdio (NORA, Claude Code, Claude Desktop) and HTTP
  (`server.serve_http(port, token=...)` — matches NORA's http transport).
- Zero dependencies; example server in `mcp_servers/notes_server.py`.

Sources: [Groq free tier limits](https://tokenmix.ai/blog/groq-free-tier-limits-2026) ·
[Groq structured outputs](https://console.groq.com/docs/structured-outputs) ·
[Ollama structured outputs](https://ollama.com/blog/structured-outputs) ·
[Local TTS comparison 2026](https://localaimaster.com/blog/best-local-tts-models) ·
[Kokoro setup](https://localaimaster.com/blog/kokoro-tts-local-setup) ·
[Tool-calling local models 2026](https://localaimaster.com/blog/best-ollama-models-tool-calling) ·
[MCP spec versions](https://blog.modelcontextprotocol.io/posts/2026-07-28-release-candidate/)
