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
_groq_client: "object | None" = None
_claude_client: "object | None" = None


def _get_groq_client(api_key: str, timeout_sec: float) -> "object":
    global _groq_client
    if _groq_client is None:
        import os
        from openai import OpenAI
        _groq_client = OpenAI(
            api_key=api_key or os.environ.get("GROQ_API_KEY", ""),
            base_url="https://api.groq.com/openai/v1",
            timeout=timeout_sec,
        )
    return _groq_client


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
- Partial or colloquial phrases → find the closest registered action and execute it.
- Questions or research requests → use ask_claude() or tell_me_about(). NEVER say "I need more info."
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

MUSIC PRIORITY
- play_music handles the chain automatically: local â†' Apple Music COM â†' Apple Music URI â†' YouTube.
- "resume music" â†' resume_music().
- For a specific song/artist on Apple Music, use apple_music_play_song / apple_music_play_artist.

RESPONSE FORMAT (exactly one of these four shapes):
  Execution plan: {{"intent": "...", "steps": [{{"action": "name", "parameters": {{}}}}], "requires_confirmation": false}}
  Clarification:  {{"intent": "clarify", "steps": [], "error": "..."}}
  System message: {{"intent": "...", "steps": [], "error": null}}
  Conversation:   {{"intent": "chat", "steps": [], "response": "your spoken reply", "error": null}}

Use the Conversation shape for greetings, small talk, or questions that need a spoken answer but no action.
Conversation responses MUST be 1-2 sentences maximum — this is spoken aloud, not written text.

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
{{"intent": "play song on Apple Music", "steps": [{{"action": "apple_music_play_song", "parameters": {{"song": "Blinding Lights"}}}}], "requires_confirmation": false}}

User: "play something by The Weeknd"
{{"intent": "play artist", "steps": [{{"action": "apple_music_play_artist", "parameters": {{"artist": "The Weeknd"}}}}], "requires_confirmation": false}}

User: "next song"
{{"intent": "skip track", "steps": [{{"action": "apple_music_next_track", "parameters": {{}}}}], "requires_confirmation": false}}

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

User: "time"
{{"intent": "get current time", "steps": [{{"action": "get_time", "parameters": {{}}}}], "requires_confirmation": false}}

User: "chrome"
{{"intent": "open Chrome", "steps": [{{"action": "open_app", "parameters": {{"name": "chrome"}}}}], "requires_confirmation": false}}

User: "how do black holes form"
{{"intent": "research question", "steps": [{{"action": "ask_claude", "parameters": {{"question": "how do black holes form"}}}}], "requires_confirmation": false}}

User: "what's the weather"
{{"intent": "check weather", "steps": [{{"action": "web_search", "parameters": {{"query": "weather today"}}}}], "requires_confirmation": false}}

User: "what did I say about the auth bug"
{{"intent": "recall past notes", "steps": [{{"action": "recall", "parameters": {{"query": "auth bug"}}}}], "requires_confirmation": false}}

User: "recall my notes on deployment"
{{"intent": "search knowledge base", "steps": [{{"action": "recall", "parameters": {{"query": "deployment"}}}}], "requires_confirmation": false}}

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
    # Hard cap: keep under ~2000 chars so total prompt stays well under 5k tokens
    native_sigs = "\n".join(native_sigs_lines)
    if len(native_sigs) > 2000:
        native_sigs = native_sigs[:2000] + "\n... (more actions available)"
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
            turn_lines.append(line)
        prompt += "\n\n" + "\n".join(turn_lines)

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
    text: str, cfg: dict, memory_ctx: dict | None = None, screen_ctx: dict | None = None
) -> IntentResponse:
    """Call the Groq API (OpenAI-compatible) to parse intent."""
    import os
    import time as _time
    from openai import APIConnectionError, APITimeoutError

    api_key = os.environ.get("GROQ_API_KEY", "")
    if not api_key:
        raise EnvironmentError("GROQ_API_KEY environment variable not set.")

    model = cfg.get("model", "llama-3.1-8b-instant")
    temperature = float(cfg.get("temperature", 0.1))
    max_tokens = int(cfg.get("max_tokens", 512))
    timeout_sec = float(get_config().get("timeouts", {}).get("llm_sec", 20))
    system_prompt = _build_system_prompt(memory_ctx, screen_ctx)

    client = _get_groq_client(api_key, timeout_sec)

    last_exc: Exception = RuntimeError("no attempts made")
    for net_attempt in range(3):
        if net_attempt > 0:
            _time.sleep(net_attempt)  # 1s, 2s backoff
        for json_attempt in range(2):
            prompt = text if json_attempt == 0 else f"Return ONLY a valid JSON object for this command: {text}"
            logger.info(f"Sending to Groq (net {net_attempt+1}/3, json {json_attempt+1}/2): '{text}'")
            try:
                resp = client.chat.completions.create(
                    model=model,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user",   "content": prompt},
                    ],
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
        "options": {"temperature": temperature, "num_predict": max_tokens},
    }

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


def needs_screen_context(text: str) -> bool:
    """Return True if the command likely requires knowing what's on screen."""
    lower = " " + text.lower() + " "
    return any(w in lower for w in _DEICTIC_WORDS)


def _call_llm(system: str, messages: list[dict]) -> str:
    """Generic LLM call that returns raw text. Used by the ReAct planner."""
    cfg = get_config().get("llm", {})
    provider = cfg.get("provider", "ollama").lower()
    model = cfg.get("model", "llama-3.1-8b-instant")
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
