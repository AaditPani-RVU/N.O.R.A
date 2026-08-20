from __future__ import annotations

import json
import logging
import re

import requests

from nora.command_engine import get_available_actions, get_action_signatures
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

You are NOT a chatbot. You are an execution engine. Your purpose:
1. Understand user intent with precision
2. Convert it into structured, executable actions
3. Maximize efficiency, speed, and reliability
4. Actively improve user productivity

CORE EXECUTION RULES
- ALWAYS return strictly valid JSON. No prose, no markdown fences, no explanation.
- NEVER hallucinate actions — only use the registered commands below.
- Prefer the smallest number of steps. Combine actions intelligently.

EXECUTION BIAS — CRITICAL (follow these exactly):
- ALWAYS act on the most obvious interpretation. NEVER ask for additional context on clear commands.
- "play music" / "play something" → ALWAYS emit play_music(track="", artist=""). NEVER ask what to play.
- "open [app]" → ALWAYS emit open_app(name="[app]"). NEVER ask which version or instance.
- Single-word commands → execute the obvious default. "screenshot" → take_screenshot(). "time" → get_time().
- ANY question about what is currently on screen, in this window, or what the
  user is looking at → read_screen(question="..."). NEVER ask_claude() for
  these: ask_claude cannot see the screen and will invent an answer. And never
  take_screenshot() either — that saves a file to disk without describing it.
  read_screen is the only action that actually looks.
- Partial or colloquial phrases → find the closest registered action and execute it.
- Questions or research requests → use ask_claude() or tell_me_about(). NEVER say "I need more info."
- Hard multi-step reasoning, math, or logic problems (NOT everyday factual questions) → deep_reasoning().
- ONLY return the Clarification shape for DESTRUCTIVE actions where two distinct targets are equally plausible
  and choosing the wrong one cannot be undone (e.g. "delete that" with two open files of the same name).
- For everything else: execute first, let the user correct if needed.

EFFICIENCY
- Prefer local execution over web-based.
- Avoid redundant app launches (context.active_apps tracks what's already open).
- Skip unnecessary confirmations unless the action is destructive.
- For music: if the user says "play music" and something's already playing, don't restart it.

CONTEXT AWARENESS
- Active apps, current music state (track/artist/source/status), PTT mode, and recent
  commands are maintained by the runtime. You don't need to ask — the runtime fills gaps.
- For "play something" with no details, emit play_music with empty parameters;
  the runtime substitutes the user's preferred track.

INPUT / PTT
- PTT is toggled in real time by voice. "Enable push to talk" â†' set_ptt_mode(true).
  "Disable push to talk" / "turn off push to talk" â†' set_ptt_mode(false).

INTERRUPTION
- "stop", "cancel", "pause everything", "shut up" â†' stop_all().
  This halts TTS, stops music, and clears pending steps.

MUSIC (Spotify only)
- All music runs through Spotify on the local desktop client. There is no other music backend.
- "play music" / "play something" with no title → play_music(track="", artist="") — it replays the user's preference.
- A specific song → spotify_play_song(song). A song plus artist → play_music(track, artist).
- An artist with no song ("play some Slowdive") → spotify_play_artist(artist).
- An album → spotify_play_album(album, artist). A playlist → spotify_play_playlist(name).
- Transport: resume_music, pause_music, toggle_music, stop_music, next_track, previous_track.
- "what's playing" / "what song is this" → now_playing(). NEVER guess the track from memory.
- spotify_set_volume(level) changes Spotify's volume only; set_volume(level) changes system volume.

RESPONSE FORMAT (exactly one of these four shapes):
  Execution plan: {{"intent": "...", "steps": [{{"action": "name", "parameters": {{}}}}], "requires_confirmation": false}}
  Clarification:  {{"intent": "clarify", "steps": [], "error": "..."}}
  System message: {{"intent": "...", "steps": [], "error": null}}
  Conversation:   {{"intent": "chat", "steps": [], "response": "your spoken reply", "error": null}}

Use the Conversation shape for greetings, small talk, or questions that need a spoken answer but no action.
Conversation responses are spoken aloud, so: no markdown, no lists, no emoji — plain sentences only.
Length should fit the question. A greeting takes a few words; a real question takes two to four
sentences. Do not pad, and do not truncate a genuine answer into a fragment. Sound like a person
talking, not like a status line.

Available actions: {actions}

Action parameter signatures:
{action_signatures}

Examples:
User: "open chrome"
{{"intent": "open Chrome", "steps": [{{"action": "open_app", "parameters": {{"name": "chrome"}}}}], "requires_confirmation": false}}

User: "enable push to talk"
{{"intent": "enable PTT mode", "steps": [{{"action": "set_ptt_mode", "parameters": {{"enabled": true}}}}], "requires_confirmation": false}}

User: "disable push to talk"
{{"intent": "disable PTT mode", "steps": [{{"action": "set_ptt_mode", "parameters": {{"enabled": false}}}}], "requires_confirmation": false}}

User: "stop"
{{"intent": "stop everything", "steps": [{{"action": "stop_all", "parameters": {{}}}}], "requires_confirmation": false}}

User: "cancel that"
{{"intent": "cancel pending actions", "steps": [{{"action": "stop_all", "parameters": {{}}}}], "requires_confirmation": false}}

User: "pause everything"
{{"intent": "halt all execution", "steps": [{{"action": "stop_all", "parameters": {{}}}}], "requires_confirmation": false}}

User: "play music"
{{"intent": "play preferred music", "steps": [{{"action": "play_music", "parameters": {{"track": "", "artist": ""}}}}], "requires_confirmation": false}}

User: "play something"
{{"intent": "play preferred music", "steps": [{{"action": "play_music", "parameters": {{"track": "", "artist": ""}}}}], "requires_confirmation": false}}

User: "resume music"
{{"intent": "resume last track", "steps": [{{"action": "resume_music", "parameters": {{}}}}], "requires_confirmation": false}}

User: "play Blinding Lights"
{{"intent": "play song on Spotify", "steps": [{{"action": "spotify_play_song", "parameters": {{"song": "Blinding Lights"}}}}], "requires_confirmation": false}}

User: "play something by The Weeknd"
{{"intent": "play artist on Spotify", "steps": [{{"action": "spotify_play_artist", "parameters": {{"artist": "The Weeknd"}}}}], "requires_confirmation": false}}

User: "play the album Souvlaki"
{{"intent": "play album on Spotify", "steps": [{{"action": "spotify_play_album", "parameters": {{"album": "Souvlaki", "artist": ""}}}}], "requires_confirmation": false}}

User: "what song is this"
{{"intent": "now playing", "steps": [{{"action": "now_playing", "parameters": {{}}}}], "requires_confirmation": false}}

User: "next song"
{{"intent": "skip track", "steps": [{{"action": "next_track", "parameters": {{}}}}], "requires_confirmation": false}}

User: "start coding"
{{"intent": "coding workflow", "steps": [{{"action": "open_app", "parameters": {{"name": "vscode"}}}}, {{"action": "open_app", "parameters": {{"name": "chrome"}}}}, {{"action": "play_music", "parameters": {{"track": "", "artist": ""}}}}], "requires_confirmation": false}}

User: "what time is it"
{{"intent": "get time", "steps": [{{"action": "get_time", "parameters": {{}}}}], "requires_confirmation": false}}

User: "tell me about quantum computing"
{{"intent": "research", "steps": [{{"action": "tell_me_about", "parameters": {{"query": "quantum computing"}}}}], "requires_confirmation": false}}

User: "how do I reverse a linked list in Python"
{{"intent": "coding help", "steps": [{{"action": "ask_claude", "parameters": {{"question": "how do I reverse a linked list in Python"}}}}], "requires_confirmation": false}}

User: "delete test.txt"
{{"intent": "delete file", "steps": [{{"action": "delete_file", "parameters": {{"path": "test.txt"}}}}], "requires_confirmation": true}}

User: "daddy's home"
{{"intent": "greeting", "steps": [{{"action": "daddys_home", "parameters": {{}}}}], "requires_confirmation": false}}

User: "hello how are you"
{{"intent": "chat", "steps": [], "response": "Doing well, sir. Ready for your commands.", "error": null}}

User: "are you there"
{{"intent": "chat", "steps": [], "response": "Always here, sir. What do you need?", "error": null}}

User: "play"
{{"intent": "play preferred music", "steps": [{{"action": "play_music", "parameters": {{"track": "", "artist": ""}}}}], "requires_confirmation": false}}

User: "screenshot"
{{"intent": "take screenshot", "steps": [{{"action": "take_screenshot", "parameters": {{}}}}], "requires_confirmation": false}}

User: "what's on my screen"
{{"intent": "read the screen", "steps": [{{"action": "read_screen", "parameters": {{"question": "What is on the screen right now?"}}}}], "requires_confirmation": false}}

User: "what am I looking at"
{{"intent": "read the screen", "steps": [{{"action": "read_screen", "parameters": {{"question": "What is on the screen right now?"}}}}], "requires_confirmation": false}}

User: "what does this error say"
{{"intent": "read the screen", "steps": [{{"action": "read_screen", "parameters": {{"question": "What does the error message say?"}}}}], "requires_confirmation": false}}

User: "time"
{{"intent": "get current time", "steps": [{{"action": "get_time", "parameters": {{}}}}], "requires_confirmation": false}}

User: "chrome"
{{"intent": "open Chrome", "steps": [{{"action": "open_app", "parameters": {{"name": "chrome"}}}}], "requires_confirmation": false}}

User: "how do black holes form"
{{"intent": "research question", "steps": [{{"action": "ask_claude", "parameters": {{"question": "how do black holes form"}}}}], "requires_confirmation": false}}

User: "if a train leaves chicago at 60mph and another leaves new york at 80mph, when do they meet"
{{"intent": "math reasoning", "steps": [{{"action": "deep_reasoning", "parameters": {{"question": "if a train leaves chicago at 60mph and another leaves new york at 80mph, when do they meet"}}}}], "requires_confirmation": false}}

User: "what's the weather"
{{"intent": "check weather", "steps": [{{"action": "web_search", "parameters": {{"query": "weather today"}}}}], "requires_confirmation": false}}

User: "what did I say about the auth bug"
{{"intent": "recall past notes", "steps": [{{"action": "recall", "parameters": {{"query": "auth bug"}}}}], "requires_confirmation": false}}

User: "recall my notes on deployment"
{{"intent": "search knowledge base", "steps": [{{"action": "recall", "parameters": {{"query": "deployment"}}}}], "requires_confirmation": false}}

User: "click the address bar"
{{"intent": "click UI element", "steps": [{{"action": "click_element", "parameters": {{"description": "address bar"}}}}], "requires_confirmation": false}}

User: "click the address bar in firefox"
{{"intent": "click UI element", "steps": [{{"action": "click_element", "parameters": {{"description": "address bar in firefox"}}}}], "requires_confirmation": false}}

User: "click the submit button in chrome"
{{"intent": "click UI element", "steps": [{{"action": "click_element", "parameters": {{"description": "submit button in chrome"}}}}], "requires_confirmation": false}}

User: "click the save button"
{{"intent": "click UI element", "steps": [{{"action": "click_on", "parameters": {{"target": "save button"}}}}], "requires_confirmation": false}}

User: "click the submit button"
{{"intent": "click UI element", "steps": [{{"action": "click_element", "parameters": {{"description": "submit button"}}}}], "requires_confirmation": false}}

User: "fill the username field with john"
{{"intent": "fill form field", "steps": [{{"action": "fill_field", "parameters": {{"label": "username", "text": "john"}}}}], "requires_confirmation": false}}

User: "type hello world"
{{"intent": "type text", "steps": [{{"action": "type_into_focused", "parameters": {{"text": "hello world"}}}}], "requires_confirmation": false}}

User: "press enter"
{{"intent": "press key", "steps": [{{"action": "press_key", "parameters": {{"keys": "Return"}}}}], "requires_confirmation": false}}

User: "why is my fan loud"
{{"intent": "cpu trace", "steps": [{{"action": "why_busy", "parameters": {{}}}}], "requires_confirmation": false}}

User: "why is my computer slow"
{{"intent": "cpu trace", "steps": [{{"action": "why_busy", "parameters": {{}}}}], "requires_confirmation": false}}

User: "what's writing to disk"
{{"intent": "disk IO trace", "steps": [{{"action": "what_writes_disk", "parameters": {{}}}}], "requires_confirmation": false}}

User: "who's using the most network"
{{"intent": "network trace", "steps": [{{"action": "top_talkers", "parameters": {{}}}}], "requires_confirmation": false}}

User: "what process is using the network"
{{"intent": "network trace", "steps": [{{"action": "top_talkers", "parameters": {{}}}}], "requires_confirmation": false}}

User: "who opened my ssh key"
{{"intent": "file access trace", "steps": [{{"action": "who_opened", "parameters": {{"path": "~/.ssh/id_rsa"}}}}], "requires_confirmation": false}}

User: "pause Spotify"
{{"intent": "media control", "steps": [{{"action": "media_play_pause", "parameters": {{}}}}], "requires_confirmation": false}}

User: "next track"
{{"intent": "media next", "steps": [{{"action": "media_next", "parameters": {{}}}}], "requires_confirmation": false}}

User: "connect to wifi CoffeeShop"
{{"intent": "wifi connect", "steps": [{{"action": "wifi_connect", "parameters": {{"ssid": "CoffeeShop"}}}}], "requires_confirmation": false}}

User: "snapshot now"
{{"intent": "create snapshot", "steps": [{{"action": "snapshot_now", "parameters": {{"label": "manual"}}}}], "requires_confirmation": false}}

User: "snapshot before refactor"
{{"intent": "create snapshot", "steps": [{{"action": "snapshot_now", "parameters": {{"label": "before-refactor"}}}}], "requires_confirmation": false}}

User: "roll back to before-refactor"
{{"intent": "rollback snapshot", "steps": [{{"action": "rollback_to", "parameters": {{"label_or_time": "before-refactor"}}}}], "requires_confirmation": true}}

User: "duck Spotify when I speak"
{{"intent": "audio duck", "steps": [{{"action": "duck_app_when_speaking", "parameters": {{"app": "Spotify"}}}}], "requires_confirmation": false}}

User: "enter focus mode for writing"
{{"intent": "focus mode", "steps": [{{"action": "focus_mode", "parameters": {{"intent": "writing"}}}}], "requires_confirmation": false}}

User: "enable mic denoising"
{{"intent": "denoise mic", "steps": [{{"action": "denoise_mic", "parameters": {{}}}}], "requires_confirmation": false}}

CRITICAL: Return ONLY the JSON object. No explanation, no markdown fences, no extra text."""


def _build_system_prompt(memory_ctx: dict | None = None, screen_ctx: dict | None = None) -> str:
    # Exclude MCP tool names from the intent parser — they're not voice commands and
    # their signatures are hundreds of tokens each. Command engine routes to them after intent is parsed.
    action_set = {a for a in get_available_actions() if not a.startswith("mcp_")}
    all_sigs = get_action_signatures()
    # Strip MCP tools entirely — not voice-addressable and cost ~3k tokens each session
    native_sigs_lines = [
        line for line in all_sigs.splitlines()
        if "mcp_" not in line and "MCP Tools" not in line
    ]
    # Hard cap: keep under ~4000 chars. Linux optional categories come last in
    # get_action_signatures(), so the old 2000-char limit silently dropped all of them.
    native_sigs = "\n".join(native_sigs_lines)
    if len(native_sigs) > 4000:
        native_sigs = native_sigs[:4000] + "\n... (more actions available)"
    prompt = SYSTEM_PROMPT_TEMPLATE.format(
        actions=", ".join(sorted(action_set)),
        action_signatures=native_sigs,
    )

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

    # Inject repo context pack (branch, dirty files, recent commits)
    try:
        from nora.repo_context import format_for_prompt as _repo_fmt
        repo_block = _repo_fmt()
        if repo_block:
            prompt += "\n\n" + repo_block
    except Exception:
        pass

    session_turns = memory_ctx.get("session_turns", [])
    if session_turns:
        turn_lines = [
            "RECENT SESSION — CRITICAL: use this to resolve follow-ups.",
            "If the user says 'are you sure', 'really?', 'is that right', 'that's wrong', "
            "'correct that', or references 'they/it/that' without a clear noun — treat it as "
            "a conversational follow-up to the last turn, NOT a new research request. "
            "Return the Conversation shape with a direct spoken reply using this context.",
        ]
        for turn in reversed(session_turns):
            status = "[ok]" if turn.get("success") else "[fail]"
            line = f'  {status} User: "{turn["text"]}" -> {turn["intent"]}'
            if turn.get("result_summary"):
                line += f' | Result: {turn["result_summary"][:80]}'
            # What NORA said back. Without it the model saw only half of each
            # exchange and could not resolve "why did you say that".
            if turn.get("reply"):
                line += f' | You said: "{turn["reply"][:120]}"'
            turn_lines.append(line)
        prompt += "\n\n" + "\n".join(turn_lines)

    # Verbatim recent dialogue — the structured turn list above summarises
    # intents, but pronoun resolution needs the actual words.
    try:
        from nora import dialogue as _dlg
        transcript = _dlg.as_transcript(6)
        if transcript:
            prompt += (
                "\n\nVERBATIM RECENT DIALOGUE (resolve pronouns and follow-ups against this):\n"
                + transcript
            )
    except Exception:
        pass

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
    screen_ctx: dict | None = None, net_attempts: int = 3,
) -> IntentResponse:
    """Call an OpenAI-compatible endpoint to parse intent.

    ``net_attempts`` is 3 when this endpoint is all there is, and 1 when the
    router is going to try another provider straight after — retrying a dead
    host three times with backoff before failing over turns a 2s recovery into
    a 9s one, which on stage reads as a hang.
    """
    import os
    import time as _time
    from openai import APIConnectionError, APITimeoutError

    api_key = os.environ.get(cfg.get("api_key_env", "GROQ_API_KEY"), "")
    if not api_key:
        raise EnvironmentError(f"{cfg.get('api_key_env', 'GROQ_API_KEY')} environment variable not set.")

    model = cfg.get("model", "openai/gpt-oss-120b")
    temperature = float(cfg.get("temperature", 0.1))
    max_tokens = int(cfg.get("max_tokens", 512))
    timeout_sec = float(get_config().get("timeouts", {}).get("llm_sec", 20))
    system_prompt = _build_system_prompt(memory_ctx, screen_ctx)

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
                logger.debug(f"Groq raw response: {response_text}")
                data = _extract_json(response_text)
                intent = IntentResponse.model_validate(data)
                logger.info(f"Parsed intent: {intent.intent} with {len(intent.steps)} step(s)")
                return intent
            except (json.JSONDecodeError, ValueError) as e:
                logger.warning(f"JSON parse failed: {e}")
                last_exc = e
                if json_attempt == 1:
                    break  # try next network attempt
            except (APIConnectionError, APITimeoutError) as e:
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
    system_prompt = _build_system_prompt(memory_ctx, screen_ctx)

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
    system_prompt = _build_system_prompt(memory_ctx, screen_ctx)

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
    """Candidates for the router's "intent" role, in preference order."""
    return get_config().get("llm_router", {}).get("roles", {}).get("intent", []) or []


def _parse_via_router(
    text: str, memory_ctx: dict | None = None, screen_ctx: dict | None = None
) -> IntentResponse:
    """Try each configured intent candidate in order; first one to answer wins.

    Deliberately not routed through ``model_router.complete``: intent parsing
    needs JSON-mode decoding, the two-shot "return ONLY JSON" retry and schema
    validation, all of which live in ``_parse_via_groq``. This reuses the
    router's *configuration* rather than its call path.
    """
    base = get_config().get("llm", {})
    errors: list[str] = []

    for candidate in _intent_candidates():
        name = candidate.get("name", candidate.get("model", "?"))
        cfg = {
            **base,
            "model": candidate["model"],
            "api_base": candidate.get("base_url") or base.get("api_base"),
            "api_key_env": candidate.get("api_key_env") or base.get("api_key_env", "GROQ_API_KEY"),
            "extra_body": candidate.get("extra_body"),
        }
        try:
            return _parse_via_groq(text, cfg, memory_ctx, screen_ctx, net_attempts=1)
        except Exception as e:
            logger.warning("intent: %s failed — %s", name, e)
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
