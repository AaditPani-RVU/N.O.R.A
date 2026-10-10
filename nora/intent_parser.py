from __future__ import annotations

import json
import logging
import re

import requests

from nora.command_engine import get_available_actions, get_action_signatures
from nora import trace
from nora.config import get_config
from nora.schemas import IntentResponse

logger = logging.getLogger("nora.intent_parser")

# Module-level singletons — avoid per-call client construction overhead
_groq_clients: dict[tuple, "object"] = {}
_claude_client: "object | None" = None
# Endpoints (base_url, model) known to reject response_format. Tracked per
# endpoint rather than as one process-wide flag: intent parsing now fails over
# between Groq and NVIDIA, and a single flag meant one fallback model refusing
# JSON mode would permanently disable it for the primary provider too —
# a healthy path degraded by a broken one it never even used.
_json_mode_unsupported: set[tuple[str, str]] = set()


def _get_groq_client(
    api_key: str, timeout_sec: float,
    base_url: str | None = None, key_env: str | None = None,
) -> "object":
    """Return a cached OpenAI-compatible client for one endpoint.

    Keyed by endpoint rather than held as a single global: intent parsing now
    fails over across providers, so Groq and NVIDIA clients coexist. A single
    global would have handed the Groq client to an NVIDIA candidate and sent
    the wrong key to the wrong host.
    """
    import os
    from openai import OpenAI

    # llm.api_base lets any OpenAI-compatible endpoint stand in for Groq
    # (OpenRouter, Cerebras, Together free tiers) with zero code changes.
    cfg = get_config().get("llm", {})
    base_url = base_url or cfg.get("api_base", "https://api.groq.com/openai/v1")
    key_env = key_env or cfg.get("api_key_env", "GROQ_API_KEY")

    cache_key = (base_url, key_env, timeout_sec)
    client = _groq_clients.get(cache_key)
    if client is None:
        client = OpenAI(
            api_key=api_key or os.environ.get(key_env, ""),
            base_url=base_url,
            timeout=timeout_sec,
            # The SDK otherwise retries a 429 twice by itself, sleeping out
            # the provider's retry-after: on Groq's free tier that turned one
            # rate-limited turn into a silent 40 s wait, while the fallback
            # models behind it sat unused. Retries are this module's job.
            max_retries=0,
        )
        _groq_clients[cache_key] = client
    return client


def _get_claude_client(timeout_sec: float) -> "object":
    global _claude_client
    if _claude_client is None:
        import anthropic
        _claude_client = anthropic.Anthropic(timeout=timeout_sec)
    return _claude_client

SYSTEM_PROMPT_TEMPLATE = """You are NORA — a high-performance, local, voice-controlled AI operating system.

You are NOT a chatbot. You are an execution engine: understand the user's intent precisely and turn it
into structured, executable actions, in the fewest steps.

CORE EXECUTION RULES
- ALWAYS return strictly valid JSON. No prose, no markdown fences, no explanation.
- NEVER hallucinate actions — only use the actions listed below.
- ALWAYS act on the most obvious interpretation. NEVER ask for more context on a clear command.
- Partial or colloquial phrases → the closest listed action. Single words → its obvious default.
- Questions or research (not places, not weather) → tell_me_about() or ask_claude(). NEVER say "I need more info."
- Hard multi-step reasoning, math or logic (NOT everyday facts) → deep_reasoning().
- Anything about what is on screen or in this window → read_screen(question="..."). ask_claude cannot see
  the screen and will invent an answer.
- "tell me about <X>" / "what's <X> like" / "show me <X>" for a city, country or landmark → show_location(location="<X>").
  A capitalised proper noun with no other qualifier counts as a place.
- ANY weather or forecast question → get_weather(). NEVER web_search or tell_me_about for weather.
- "stop", "cancel", "pause everything", "shut up" → stop_all().
- Clarify ONLY for a DESTRUCTIVE action where two targets are equally plausible and a wrong guess can't be undone.
  For everything else: execute first, let the user correct if needed.
- Active apps, music state and recent commands are tracked by the runtime; don't ask for them, and don't
  reopen what's open or restart what's playing.
{rules}
RESPONSE FORMAT (exactly one of these four shapes):
  Execution plan: {{"intent": "...", "steps": [{{"action": "name", "parameters": {{}}}}]}}
  Clarification:  {{"intent": "clarify", "steps": [], "error": "..."}}
  System message: {{"intent": "...", "steps": [], "error": null}}
  Conversation:   {{"intent": "chat", "steps": [], "response": "your spoken reply", "error": null}}
Add "requires_confirmation": true to a plan only when a step is destructive or can't be undone.

Use the Conversation shape for greetings, small talk, or questions that need a spoken answer but no action.
Conversation responses are spoken aloud, so: no markdown, no lists, no emoji — plain sentences only.
Length should fit the question. A greeting takes a few words; a real question takes two to four
sentences. Do not pad, and do not truncate a genuine answer into a fragment. Sound like a person
talking, not like a status line.

Actions (the ones that fit this request; use these exact parameter names):
{action_signatures}

Examples:
{examples}

CRITICAL: Return ONLY the JSON object. No explanation, no markdown fences, no extra text."""

# Rules that only matter when one of their actions is in the prompt. Each is
# (actions, text); the text goes in when any of the actions was picked.
RULES: list[tuple[tuple[str, ...], str]] = [
    (("play_music", "spotify_play_song", "spotify_play_artist", "spotify_play_album",
      "spotify_play_playlist", "now_playing", "spotify_set_volume"),
     """MUSIC (Spotify)
- No title → play_music(track="", artist=""); the runtime plays the user's preference. NEVER ask what to play.
- A song → spotify_play_song(song); song and artist → play_music(track, artist); an artist alone →
  spotify_play_artist(artist); an album → spotify_play_album(album, artist); a playlist → spotify_play_playlist(name).
- "what's playing" → now_playing(). NEVER guess the track from memory.
- spotify_set_volume(level) is Spotify's volume only; set_volume(level) is the system's."""),
    (("remind_me", "schedule_task"),
     """TIME
- "remind me in N minutes to X" → remind_me(message="X", delay_minutes=N). A clock time or a
  repeat ("at 6pm", "every morning") → schedule_task(when, what)."""),
    (("open_app",),
     """- "open [app]" → open_app(name="[app]"). NEVER ask which version or instance."""),
    (("take_screenshot",),
     """- take_screenshot() only saves a file; it never describes the screen (read_screen does)."""),
]

# One example per behaviour, each shown when its action is in the prompt.
# Common commands never reach the model (a rule table answers them first), so
# none of these is one the fast path answers.
EXAMPLES: list[tuple[str, str]] = [
    ("stop_all", 'User: "stop" → {"intent": "stop everything", "steps": [{"action": "stop_all", "parameters": {}}]}'),
    ("", 'User: "you look really cool today" → {"intent": "chat", "steps": [], "response": "Thank you, sir. I try.", "error": null}'),
    ("tell_me_about", 'User: "tell me about quantum computing" → {"intent": "research", "steps": [{"action": "tell_me_about", "parameters": {"query": "quantum computing"}}]}'),
    ("show_location", 'User: "tell me about Tokyo" → {"intent": "show location", "steps": [{"action": "show_location", "parameters": {"location": "Tokyo"}}]}'),
    ("ask_claude", 'User: "how do black holes form" → {"intent": "research question", "steps": [{"action": "ask_claude", "parameters": {"question": "how do black holes form"}}]}'),
    ("deep_reasoning", 'User: "if a train leaves at 60mph and another at 80mph, when do they meet" → {"intent": "math reasoning", "steps": [{"action": "deep_reasoning", "parameters": {"question": "if a train leaves at 60mph and another at 80mph, when do they meet"}}]}'),
    ("read_screen", 'User: "what does this error say" → {"intent": "read the screen", "steps": [{"action": "read_screen", "parameters": {"question": "What does the error message say?"}}]}'),
    ("recall", 'User: "what did I say about the auth bug" → {"intent": "recall past notes", "steps": [{"action": "recall", "parameters": {"query": "auth bug"}}]}'),
    ("remind_me", 'User: "remind me in half an hour to check the oven" → {"intent": "set reminder", "steps": [{"action": "remind_me", "parameters": {"message": "check the oven", "delay_minutes": 30}}]}'),
    ("delete_file", 'User: "delete test.txt" → {"intent": "delete file", "steps": [{"action": "delete_file", "parameters": {"path": "test.txt"}}], "requires_confirmation": true}'),
    ("daddys_home", 'User: "daddy\'s home" → {"intent": "greeting", "steps": [{"action": "daddys_home", "parameters": {}}]}'),
    ("click_element", 'User: "click the submit button in chrome" → {"intent": "click UI element", "steps": [{"action": "click_element", "parameters": {"description": "submit button in chrome"}}]}'),
    ("fill_field", 'User: "fill the username field with john" → {"intent": "fill form field", "steps": [{"action": "fill_field", "parameters": {"label": "username", "text": "john"}}]}'),
    ("what_writes_disk", 'User: "what\'s writing to disk" → {"intent": "disk IO trace", "steps": [{"action": "what_writes_disk", "parameters": {}}]}'),
    ("who_opened", 'User: "who opened my ssh key" → {"intent": "file access trace", "steps": [{"action": "who_opened", "parameters": {"path": "~/.ssh/id_rsa"}}]}'),
    ("media_play_pause", 'User: "pause Spotify" → {"intent": "media control", "steps": [{"action": "media_play_pause", "parameters": {}}]}'),
    ("wifi_connect", 'User: "connect to wifi CoffeeShop" → {"intent": "wifi connect", "steps": [{"action": "wifi_connect", "parameters": {"ssid": "CoffeeShop"}}]}'),
    ("snapshot_now", 'User: "snapshot before refactor" → {"intent": "create snapshot", "steps": [{"action": "snapshot_now", "parameters": {"label": "before-refactor"}}]}'),
    ("rollback_to", 'User: "roll back to before-refactor" → {"intent": "rollback snapshot", "steps": [{"action": "rollback_to", "parameters": {"label_or_time": "before-refactor"}}], "requires_confirmation": true}'),
    ("duck_app_when_speaking", 'User: "duck Spotify when I speak" → {"intent": "audio duck", "steps": [{"action": "duck_app_when_speaking", "parameters": {"app": "Spotify"}}]}'),
    ("focus_mode", 'User: "enter focus mode for writing" → {"intent": "focus mode", "steps": [{"action": "focus_mode", "parameters": {"intent": "writing"}}]}'),
]

# Examples always shown: the chat shape and the two research routes. The
# other core actions' rules are in the prompt's head; their examples go in
# only when retrieval ranks them for the utterance itself, which keeps a
# mid-conversation prompt near 2k tokens (Sharp Phase C).
_ALWAYS_EXAMPLES = ("", "tell_me_about", "ask_claude")


def _picked_actions(text: str, memory_ctx: dict | None) -> list[str]:
    """The actions this turn's prompt lists (nora.tool_retrieval)."""
    from nora import tool_retrieval
    previous: list[str] = []
    previous_text = ""
    turns = (memory_ctx or {}).get("session_turns") or []
    if turns:
        previous = list(turns[0].get("actions") or [])
        previous_text = turns[0].get("text") or ""
    return tool_retrieval.select(text, previous=previous, previous_text=previous_text)


_MAX_DESCRIPTION = 200


def _signature_line(name: str, meta) -> str:
    """One action for the prompt: its signature, and its description cut at a
    sentence (or word) near _MAX_DESCRIPTION characters."""
    sig = meta.sig or f"{name}()"
    desc = " ".join((meta.description or "").split())
    if len(desc) > _MAX_DESCRIPTION:
        cut = desc[:_MAX_DESCRIPTION]
        end = max(cut.rfind(". "), cut.rfind("; "))
        desc = cut[:end + 1] if end > _MAX_DESCRIPTION // 2 else cut.rsplit(" ", 1)[0] + "…"
    return f"- {sig} — {desc}" if desc else f"- {sig}"


# Per utterance in the verbatim dialogue. A long answer (a notifications
# summary, a researched reply) was carried whole into every later prompt;
# its opening is what a follow-up refers back to.
_DIALOGUE_CHARS = {"User": 200, "NORA": 160}


def _recent_dialogue(n: int = 6) -> str:
    try:
        from nora import dialogue as _dlg
        lines = []
        for turn in _dlg.history(n):
            who = "User" if turn.speaker == "user" else "NORA"
            said = " ".join(turn.text.split())
            cap = _DIALOGUE_CHARS[who]
            if len(said) > cap:
                said = said[:cap].rsplit(" ", 1)[0] + "…"
            lines.append(f"{who}: {said}")
        return "\n".join(lines)
    except Exception:
        return ""


def _build_system_prompt(memory_ctx: dict | None = None, screen_ctx: dict | None = None,
                         text: str = "") -> str:
    # Only the actions retrieval picks for this utterance are listed, each in
    # full (Sharp Phase C). Listing all ~220 cost ~5k tokens a turn, and the
    # signature block was cut at 4,000 characters, so most tools were listed
    # by name with no parameters. MCP tools were never listed: the command
    # engine routes to them after the intent is parsed.
    from nora import command_engine

    picked = _picked_actions(text, memory_ctx)
    chosen = set(picked)
    from nora import tool_retrieval
    ranked = chosen - set(tool_retrieval.CORE) | set(tool_retrieval.ranked_core(text))
    sig_lines = []
    for name in picked:
        meta = command_engine.get_action_meta(name)
        if meta is not None:
            sig_lines.append(_signature_line(name, meta))
    rules = "".join("\n" + body + "\n" for actions, body in RULES if chosen.intersection(actions))
    examples = "\n".join(line for action, line in EXAMPLES
                         if action in _ALWAYS_EXAMPLES or action in ranked)
    prompt = SYSTEM_PROMPT_TEMPLATE.format(
        rules=rules,
        action_signatures="\n".join(sig_lines),
        examples=examples,
    )

    # Skills contribute one line each — name and description only. The body
    # stays on disk until `run_skill` picks it up, which is what keeps fifty
    # saved procedures from costing fifty procedures' worth of prompt.
    try:
        from nora import skills
        catalogue = skills.catalogue()
        if catalogue:
            prompt += "\n\n" + catalogue
    except Exception as e:
        logger.debug("skill catalogue unavailable: %s", e)

    if not memory_ctx:
        return prompt

    lines: list[str] = []

    music = memory_ctx.get("preferred_music", {})
    if music.get("track") or music.get("artist"):
        lines.append(f"- Preferred music: {music.get('artist', '').strip()} — {music.get('track', '').strip()}")

    top_apps = memory_ctx.get("top_apps", [])
    if top_apps:
        lines.append(f"- Most used apps: {', '.join(top_apps)}")

    top_actions = memory_ctx.get("top_actions", [])
    if top_actions:
        lines.append(f"- Most frequent actions: {', '.join(top_actions)}")

    recent = memory_ctx.get("recent_commands", [])
    if recent:
        intents = [c.get("intent", "") for c in recent[:3] if c.get("intent")]
        if intents:
            lines.append(f"- Recent intents: {'; '.join(intents)}")

    typical = memory_ctx.get("typical_actions_now", [])
    if typical:
        lines.append(f"- Typical actions at this time of day: {', '.join(typical)}")

    relevant = memory_ctx.get("relevant_context", [])
    if relevant:
        lines.append(f"- Relevant past context: {' | '.join(relevant[:2])}")

    # Inject user card from User Model Layer
    try:
        from nora.user_model import format_user_card_for_prompt
        user_card = format_user_card_for_prompt()
        if user_card:
            lines.extend(user_card.splitlines())
    except Exception:
        pass

    if lines:
        prompt += "\n\nUSER PROFILE (use to personalize — do not echo back):\n" + "\n".join(lines)

    # Inject persona calibration settings (Sprint 5)
    try:
        from nora import persona as _persona
        persona_block = _persona.format_for_prompt()
        if persona_block:
            prompt += "\n\n" + persona_block
    except Exception:
        pass

    # Inject who the camera can see (vision Phase 2). Empty unless vision is
    # on and someone is in frame; carries the guest-mode instruction with it.
    try:
        from nora.vision.presence import format_for_prompt as _presence_fmt
        presence_block = _presence_fmt()
        if presence_block:
            prompt += "\n\n" + presence_block
    except Exception:
        pass

    # Repo context pack (branch, dirty files, recent commits), only when the
    # words are about the user's code ("my code", "the repo", "this error",
    # "nora"). It is ~150 tokens no other request uses (Sharp Phase C). Not
    # "a coding tool was picked": "is the strait open or closed" ranks
    # github_my_prs first.
    try:
        from nora import conversation as _conv
        from nora.repo_context import format_for_prompt as _repo_fmt
        repo_block = _repo_fmt() if _conv.mentions_repo(text) else ""
        if repo_block:
            prompt += "\n\n" + repo_block
    except Exception:
        pass

    transcript = _recent_dialogue()
    session_turns = memory_ctx.get("session_turns", [])
    if session_turns:
        turn_lines = [
            "RECENT SESSION — use it to resolve follow-ups. 'Are you sure', 'really?', 'that's wrong', "
            "or 'they/it/that' with no clear noun is a follow-up to the last turn, not a new request: "
            "answer it in the Conversation shape from this context.",
        ]
        if transcript:
            # The verbatim dialogue below has both sides' words; all this adds
            # is what each turn did and whether it worked.
            done = []
            for turn in reversed(session_turns):
                status = "ok" if turn.get("success") else "failed"
                acts = ", ".join(turn.get("actions") or []) or "spoken reply"
                done.append(f"{turn['intent']} [{acts}, {status}]")
            turn_lines.append("  Turns so far, oldest first: " + "; ".join(done))
        else:
            for turn in reversed(session_turns):
                status = "[ok]" if turn.get("success") else "[fail]"
                line = f'  {status} User: "{turn["text"]}" -> {turn["intent"]}'
                if turn.get("result_summary"):
                    line += f' | Result: {turn["result_summary"][:80]}'
                # What NORA said back. Without it the model saw only half of
                # each exchange and could not resolve "why did you say that".
                if turn.get("reply"):
                    line += f' | You said: "{turn["reply"][:120]}"'
                turn_lines.append(line)
        prompt += "\n\n" + "\n".join(turn_lines)

    # Verbatim recent dialogue — the structured turn list above summarises
    # intents, but pronoun resolution needs the actual words.
    if transcript:
        prompt += ("\n\nVERBATIM RECENT DIALOGUE (resolve pronouns and follow-ups against this):\n"
                   + transcript)

    # Multimodal context fusion — inject screen snippet for deictic commands
    if screen_ctx:
        snippet = screen_ctx.get("snippet", "")
        window_title = screen_ctx.get("window_title", "")
        if snippet or window_title:
            screen_lines = ["CURRENT SCREEN CONTEXT (to resolve 'this', 'that', 'here', etc.):"]
            if window_title:
                screen_lines.append(f"  Active window: {window_title}")
            if snippet:
                screen_lines.append(f"  Screen content: {snippet}")
            prompt += "\n\n" + "\n".join(screen_lines)

    return prompt


def _extract_json(text: str) -> dict:
    """Extract JSON from LLM response, handling markdown fences and extra text."""
    # Try direct parse first
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Try to find JSON in markdown code blocks
    match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if match:
        return json.loads(match.group(1))

    # Try to find first { ... } block
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        return json.loads(match.group(0))

    raise ValueError(f"No valid JSON found in response: {text[:200]}")


def check_ollama_connection() -> bool:
    """Verify the configured LLM backend is reachable/configured before starting."""
    import os
    cfg = get_config().get("llm", {})
    provider = cfg.get("provider", "ollama").lower()

    if provider == "claude":
        return True  # checked at call time via ANTHROPIC_API_KEY

    if provider == "groq":
        if not os.environ.get("GROQ_API_KEY", ""):
            logger.error("GROQ_API_KEY is not set. Add it to your .env file.")
            return False
        return True  # actual connectivity verified on first call

    # Ollama — check the local server is up
    base_url = cfg.get("base_url", "http://localhost:11434")
    try:
        resp = requests.get(f"{base_url}/api/tags", timeout=5)
        return resp.status_code == 200
    except Exception:
        return False


def _parse_via_groq(
    text: str, cfg: dict, memory_ctx: dict | None = None,
    screen_ctx: dict | None = None, net_attempts: int = 3, timeout_sec: float | None = None,
) -> IntentResponse:
    """Call an OpenAI-compatible endpoint to parse intent.

    ``net_attempts`` is 3 when this endpoint is all there is, and 1 when the
    router is going to try another provider straight after — retrying a dead
    host three times with backoff before failing over turns a 2s recovery into
    a 9s one, which on stage reads as a hang.
    """
    import os
    import time as _time
    from openai import APIConnectionError, APITimeoutError, RateLimitError

    api_key = os.environ.get(cfg.get("api_key_env", "GROQ_API_KEY"), "")
    if not api_key:
        raise EnvironmentError(f"{cfg.get('api_key_env', 'GROQ_API_KEY')} environment variable not set.")

    model = cfg.get("model", "openai/gpt-oss-120b")
    temperature = float(cfg.get("temperature", 0.1))
    max_tokens = int(cfg.get("max_tokens", 512))
    timeout_sec = timeout_sec or float(get_config().get("timeouts", {}).get("llm_sec", 20))
    system_prompt = _build_system_prompt(memory_ctx, screen_ctx, text)
    # When the prompt was ready: the time from `routed` to here is NORA's own,
    # before any model is asked. It hid ~2.8 s of git and GitHub calls once.
    trace.mark("prompt")

    client = _get_groq_client(
        api_key, timeout_sec,
        base_url=cfg.get("api_base"), key_env=cfg.get("api_key_env"),
    )

    endpoint = (
        cfg.get("api_base") or "https://api.groq.com/openai/v1",
        model,
    )
    last_exc: Exception = RuntimeError("no attempts made")
    for net_attempt in range(net_attempts):
        if net_attempt > 0:
            _time.sleep(net_attempt)  # 1s, 2s backoff
        for json_attempt in range(2):
            prompt = text if json_attempt == 0 else f"Return ONLY a valid JSON object for this command: {text}"
            logger.info(
                f"Sending to {model} (net {net_attempt+1}/{net_attempts}, "
                f"json {json_attempt+1}/2): '{text}'")
            # Enforced JSON mode: the endpoint constrains decoding to valid JSON,
            # eliminating the fence-stripping/regex failure class entirely.
            # Disabled once per process if the endpoint rejects response_format.
            extra: dict = {}
            if bool(cfg.get("json_mode", True)) and endpoint not in _json_mode_unsupported:
                extra["response_format"] = {"type": "json_object"}
            if cfg.get("extra_body"):
                extra["extra_body"] = cfg["extra_body"]
            try:
                started = _time.monotonic()
                resp = client.chat.completions.create(
                    model=model,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user",   "content": prompt},
                    ],
                    **extra,
                )
                response_text = resp.choices[0].message.content or ""
                usage = getattr(resp, "usage", None)
                logger.info("intent: %s answered in %.1fs (prompt %s tok, reply %s tok)", model,
                            _time.monotonic() - started, getattr(usage, "prompt_tokens", "?"),
                            getattr(usage, "completion_tokens", "?"))
                logger.debug(f"Groq raw response: {response_text}")
                trace.note_model("intent", cfg.get("name") or model, _time.monotonic() - started,
                                 prompt_tokens=getattr(usage, "prompt_tokens", None),
                                 completion_tokens=getattr(usage, "completion_tokens", None))
                data = _extract_json(response_text)
                intent = IntentResponse.model_validate(data)
                logger.info(f"Parsed intent: {intent.intent} with {len(intent.steps)} step(s)")
                return intent
            except (json.JSONDecodeError, ValueError) as e:
                logger.warning(f"JSON parse failed: {e}")
                last_exc = e
                if json_attempt == 1:
                    break  # try next network attempt
            except RateLimitError:
                # Not a network blip: waiting here is exactly the stall the
                # caller has fallbacks for. The router records the cooldown.
                trace.note_model("intent", cfg.get("name") or model, _time.monotonic() - started,
                                 ok=False, error="rate-limited")
                raise
            except (APIConnectionError, APITimeoutError) as e:
                trace.note_model("intent", cfg.get("name") or model, _time.monotonic() - started,
                                 ok=False, error=type(e).__name__)
                logger.warning(f"Groq network error (attempt {net_attempt+1}): {e}")
                last_exc = e
                break  # skip json retry, go straight to next network attempt
            except Exception as e:
                if extra and "response_format" in str(e):
                    # This endpoint doesn't support JSON mode — drop it for
                    # this endpoint only, and retry immediately.
                    _json_mode_unsupported.add(endpoint)
                    logger.warning(
                        "JSON mode rejected by %s — falling back to plain text for it", endpoint[1])
                    continue
                trace.note_model("intent", cfg.get("name") or model, _time.monotonic() - started,
                                 ok=False, error=str(e))
                logger.warning(f"Groq unexpected error: {e}")
                last_exc = e
                break
    raise last_exc


def _parse_via_claude(
    text: str, cfg: dict, memory_ctx: dict | None = None, screen_ctx: dict | None = None
) -> IntentResponse:
    """Call the Anthropic Claude API to parse intent."""
    import time as _time
    from anthropic import APIConnectionError, APITimeoutError

    model = cfg.get("model", "claude-haiku-4-5-20251001")
    temperature = float(cfg.get("temperature", 0.1))
    max_tokens = int(cfg.get("max_tokens", 512))
    timeout_sec = float(get_config().get("timeouts", {}).get("llm_sec", 20))
    system_prompt = _build_system_prompt(memory_ctx, screen_ctx, text)

    client = _get_claude_client(timeout_sec)

    last_exc: Exception = RuntimeError("no attempts made")
    for net_attempt in range(3):
        if net_attempt > 0:
            _time.sleep(net_attempt)
        for json_attempt in range(2):
            prompt = text if json_attempt == 0 else f"Return ONLY a valid JSON object for this command: {text}"
            logger.info(f"Sending to Claude (net {net_attempt+1}/3, json {json_attempt+1}/2): '{text}'")
            try:
                msg = client.messages.create(
                    model=model,
                    max_tokens=max_tokens,
                    system=[{
                        "type": "text",
                        "text": system_prompt,
                        "cache_control": {"type": "ephemeral"},
                    }],
                    messages=[{"role": "user", "content": prompt}],
                    temperature=temperature,
                )
                response_text = msg.content[0].text
                logger.debug(f"Claude raw response: {response_text}")
                data = _extract_json(response_text)
                intent = IntentResponse.model_validate(data)
                logger.info(f"Parsed intent: {intent.intent} with {len(intent.steps)} step(s)")
                return intent
            except (json.JSONDecodeError, ValueError) as e:
                logger.warning(f"JSON parse failed: {e}")
                last_exc = e
                if json_attempt == 1:
                    break
            except (APIConnectionError, APITimeoutError) as e:
                logger.warning(f"Claude network error (attempt {net_attempt+1}): {e}")
                last_exc = e
                break
            except Exception as e:
                logger.warning(f"Claude unexpected error: {e}")
                last_exc = e
                break
    raise last_exc


def _intent_json_schema() -> dict:
    """Compact JSON Schema for IntentResponse, for constrained decoding.

    Hand-written rather than IntentResponse.model_json_schema(): pydantic's
    anyOf-nullable output over-constrains small local models; this keeps
    only the fields the pipeline actually reads.
    """
    return {
        "type": "object",
        "properties": {
            "intent": {"type": "string"},
            "steps": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string"},
                        "parameters": {"type": "object"},
                    },
                    "required": ["action"],
                },
            },
            "requires_confirmation": {"type": "boolean"},
            "response": {"type": "string"},
        },
        "required": ["intent", "steps"],
    }


def _parse_via_ollama(
    text: str, cfg: dict, memory_ctx: dict | None = None, screen_ctx: dict | None = None
) -> IntentResponse:
    """Call local Ollama to parse intent."""
    import time as _time

    base_url = cfg.get("base_url", "http://localhost:11434")
    model = cfg.get("model", "phi3:mini")
    temperature = cfg.get("temperature", 0.1)
    max_tokens = cfg.get("max_tokens", 512)
    timeout_sec = float(get_config().get("timeouts", {}).get("llm_sec", 20))
    system_prompt = _build_system_prompt(memory_ctx, screen_ctx, text)

    payload = {
        "model": model,
        "prompt": text,
        "system": system_prompt,
        "stream": False,
        # Hybrid-reasoning models (qwen3, ...) spend the token budget on
        # chain-of-thought before the schema-constrained JSON — with a voice
        # command's max_tokens budget that leaves an empty/truncated
        # response. Intent parsing needs speed, not deliberation.
        "think": False,
        "options": {"temperature": temperature, "num_predict": max_tokens},
    }
    # Constrained decoding (Ollama "format"): the model can only emit tokens
    # matching the intent schema — invalid JSON becomes impossible, which is
    # the single biggest reliability win for small local models.
    if bool(cfg.get("structured_output", True)):
        payload["format"] = _intent_json_schema()
    else:
        payload["format"] = "json"

    last_exc: Exception = RuntimeError("no attempts made")
    for net_attempt in range(3):
        if net_attempt > 0:
            _time.sleep(net_attempt)
        for json_attempt in range(2):
            if json_attempt == 1:
                payload["prompt"] = f"Return ONLY a valid JSON object for this command: {text}"
            try:
                logger.info(f"Sending to Ollama (net {net_attempt+1}/3, json {json_attempt+1}/2): '{text}'")
                resp = requests.post(f"{base_url}/api/generate", json=payload, timeout=timeout_sec)
                resp.raise_for_status()
                response_text = resp.json().get("response", "")
                logger.debug(f"LLM raw response: {response_text}")
                data = _extract_json(response_text)
                intent = IntentResponse.model_validate(data)
                logger.info(f"Parsed intent: {intent.intent} with {len(intent.steps)} step(s)")
                return intent
            except (json.JSONDecodeError, ValueError) as e:
                logger.warning(f"JSON parse failed: {e}")
                last_exc = e
                if json_attempt == 1:
                    break
            except requests.RequestException as e:
                logger.warning(f"Ollama network error (attempt {net_attempt+1}): {e}")
                last_exc = e
                break
    raise last_exc


def _intent_candidates() -> list[dict]:
    """Candidates for the router's "intent" role, in preference order, minus
    any the scout has seen withdrawn."""
    from nora import scout
    return scout.live(get_config().get("llm_router", {}).get("roles", {}).get("intent", []) or [])


def candidate_cfg(candidate: dict) -> dict:
    """The `_parse_via_groq` config for one router candidate."""
    base = get_config().get("llm", {})
    return {
        **base,
        "name": candidate.get("name", candidate.get("model", "?")),
        "model": candidate["model"],
        "api_base": candidate.get("base_url") or base.get("api_base"),
        "api_key_env": candidate.get("api_key_env") or base.get("api_key_env", "GROQ_API_KEY"),
        "extra_body": candidate.get("extra_body"),
    }


def _parse_via_router(
    text: str, memory_ctx: dict | None = None, screen_ctx: dict | None = None
) -> IntentResponse:
    """Try each configured intent candidate in order; first one to answer wins.

    Deliberately not routed through ``model_router.complete``: intent parsing
    needs JSON-mode decoding, the two-shot "return ONLY JSON" retry and schema
    validation, all of which live in ``_parse_via_groq``. This reuses the
    router's *configuration* rather than its call path.
    """
    import time as _time
    from nora import model_router

    errors: list[str] = []
    total = float(get_config().get("timeouts", {}).get("llm_sec", 20))
    plan = model_router.order(_intent_candidates())
    last_tried = max((i for i, (_, skip) in enumerate(plan) if not skip), default=-1)

    for i, (candidate, skip) in enumerate(plan):
        name = candidate.get("name", candidate.get("model", "?"))
        # Rate-limited, gone or hanging a moment ago: don't spend a round trip
        # learning it again (model_router.order keeps one to try if all are).
        if skip:
            logger.info("intent: skipping %s (%s for %.0fs more)", name,
                        model_router.cooldown_reason(name) or "cooling down",
                        model_router._cooldown_until(name) - _time.time())
            errors.append(f"{name}: cooling down")
            continue
        try:
            result = _parse_via_groq(text, candidate_cfg(candidate), memory_ctx, screen_ctx,
                                     net_attempts=1,
                                     timeout_sec=model_router.attempt_timeout(total, i == last_tried))
            model_router._clear_cooldown(name)
            return result
        except Exception as e:
            kind = model_router.note_failure(name, e)
            logger.warning("intent: %s failed (%s) — %s", name, kind, e)
            errors.append(f"{name}: {e}")

    raise RuntimeError("all intent candidates failed: " + " | ".join(errors))


def parse_intent(
    text: str,
    memory_ctx: dict | None = None,
    screen_ctx: dict | None = None,
) -> IntentResponse:
    """Route intent parsing to the configured provider (groq, claude, or ollama).

    Layer 1: deterministic fast-path (zero latency, zero fallback risk).
    Layer 2: LLM provider with execution-biased system prompt.
    Layer 3: fast-path rescue if the LLM still returned a clarification.
    """
    from nora import fast_path

    # Layer 1 — skip LLM entirely for obvious commands
    fp = fast_path.resolve(text)
    if fp is not None:
        logger.info(f"Fast-path resolved '{text}' → {fp.intent}")
        return fp

    cfg = get_config().get("llm", {})

    # Layer 2 — the router first, if an "intent" role is configured. The action
    # path used to hang off a single endpoint: when it was down, intent parsing
    # failed outright and NORA could not do anything at all, while the chat
    # path sailed on through its own fallback chain. Same treatment for both.
    if _intent_candidates():
        result = _parse_via_router(text, memory_ctx, screen_ctx)
    else:
        provider = cfg.get("provider", "ollama").lower()
        if provider == "groq":
            result = _parse_via_groq(text, cfg, memory_ctx, screen_ctx)
        elif provider == "claude":
            result = _parse_via_claude(text, cfg, memory_ctx, screen_ctx)
        else:
            result = _parse_via_ollama(text, cfg, memory_ctx, screen_ctx)

    # Layer 3 — rescue if LLM returned a clarification or produced no steps
    if _is_clarification(result):
        rescue = fast_path.resolve(text)
        if rescue is not None and rescue.steps:
            logger.info(f"LLM clarified on '{text}'; fast-path rescued → {rescue.intent}")
            return rescue

    return result


def _is_clarification(intent: IntentResponse) -> bool:
    """Return True when the LLM is asking for more information instead of acting."""
    if intent.intent == "clarify":
        return True
    if not intent.steps and intent.error:
        return True
    if not intent.steps and intent.response:
        # If the spoken response contains clarification language, treat it as a failure
        clarify_signals = (
            "more context", "more information", "more detail", "be more specific",
            "what do you mean", "could you clarify", "please specify", "which one",
            "i'm not sure what", "i don't understand", "can you tell me more",
        )
        return any(sig in intent.response.lower() for sig in clarify_signals)
    return False


# ── Sprint 4 additions ─────────────────────────────────────────────────────

# Keywords that indicate the user wants an autonomous multi-step goal executed
_AUTONOMOUS_KEYWORDS = (
    "autonomously", "automatically", "set up", "setup", "organize", "organise",
    "clone and run", "bootstrap", "scaffold and", "do everything to",
    "handle the whole", "take care of", "go ahead and", "run the full",
    "end to end", "end-to-end",
)


def is_autonomous_task(text: str) -> bool:
    """Return True if the utterance signals an autonomous multi-step goal."""
    lower = text.lower()
    return any(kw in lower for kw in _AUTONOMOUS_KEYWORDS)


# Pronouns/deictic words that benefit from screen context
_DEICTIC_WORDS = (
    " this ", " that ", " here ", " there ", " it ", " those ", " these ",
    "the one", "on screen", "on the screen", "what's on", "what is on",
    "visible", "currently showing",
    "are you sure", "is that right", "is that correct", "really?", "you sure",
    "that's wrong", "correct that", "are they",
)


# The substring probes above are space-padded, so they only match the exact
# phrasings listed — "on screen" and "on the screen" hit, "on my screen" does
# not, and trailing punctuation defeats all of them ("my screen?" is not
# " screen "). The result was that the most explicitly screen-directed
# utterance possible got no screen snippet attached. Word-boundary matching
# handles the possessives and the punctuation; \b after "screen" keeps it from
# firing on "screenshot", which needs no OCR of its own.
_SCREEN_RE = re.compile(
    r"\b(?:my|the|your|this)\s+screen\b|\bon\s+screen\b"
    r"|\bsee\s+on\b|\bshowing\b|\bdisplayed\b",
    re.I,
)


def needs_screen_context(text: str) -> bool:
    """Return True if the command likely requires knowing what's on screen."""
    if _SCREEN_RE.search(text):
        return True
    lower = " " + text.lower() + " "
    return any(w in lower for w in _DEICTIC_WORDS)


def _call_llm(system: str, messages: list[dict]) -> str:
    """Generic LLM call that returns raw text. Used by the ReAct planner."""
    cfg = get_config().get("llm", {})
    provider = cfg.get("provider", "ollama").lower()
    model = cfg.get("model", "openai/gpt-oss-120b")
    max_tokens = int(cfg.get("max_tokens", 512))
    temperature = float(cfg.get("temperature", 0.1))
    timeout_sec = float(get_config().get("timeouts", {}).get("llm_sec", 20))

    if provider == "groq":
        import os
        from openai import OpenAI
        client = OpenAI(
            api_key=os.environ.get("GROQ_API_KEY", ""),
            base_url="https://api.groq.com/openai/v1",
            timeout=timeout_sec,
        )
        resp = client.chat.completions.create(
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            messages=[{"role": "system", "content": system}] + messages,
        )
        return resp.choices[0].message.content or ""

    if provider == "claude":
        import anthropic
        client = anthropic.Anthropic(timeout=timeout_sec)
        msg = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=messages,
        )
        return msg.content[0].text

    # Ollama
    base_url = cfg.get("base_url", "http://localhost:11434")
    user_content = messages[-1]["content"] if messages else ""
    payload = {
        "model": model,
        "prompt": user_content,
        "system": system,
        "stream": False,
        "options": {"temperature": temperature, "num_predict": max_tokens},
    }
    resp = requests.post(f"{base_url}/api/generate", json=payload, timeout=timeout_sec)
    resp.raise_for_status()
    return resp.json().get("response", "")
