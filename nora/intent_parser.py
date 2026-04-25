from __future__ import annotations

import json
import logging
import re

import requests

from nora.command_engine import get_available_actions
from nora.config import get_config
from nora.schemas import IntentResponse

logger = logging.getLogger("nora.intent_parser")

SYSTEM_PROMPT_TEMPLATE = """You are NORA â€” a high-performance, local, voice-controlled AI operating system.

You are NOT a chatbot. You are an execution engine. Your purpose:
1. Understand user intent with precision
2. Convert it into structured, executable actions
3. Maximize efficiency, speed, and reliability
4. Actively improve user productivity

CORE EXECUTION RULES
- ALWAYS return strictly valid JSON. No prose, no markdown fences, no explanation.
- NEVER hallucinate actions â€” only use the registered commands below.
- Prefer the smallest number of steps. Combine actions intelligently.
- If the command is ambiguous, return a clarification instead of guessing.

EFFICIENCY
- Prefer local execution over web-based.
- Avoid redundant app launches (context.active_apps tracks what's already open).
- Skip unnecessary confirmations unless the action is destructive.
- For music: if the user says "play music" and something's already playing, don't restart it.

CONTEXT AWARENESS
- Active apps, current music state (track/artist/source/status), PTT mode, and recent
  commands are maintained by the runtime. You don't need to ask â€” the runtime fills gaps.
- For "play something" with no details, emit play_music with empty parameters;
  the runtime substitutes the user's preferred track.

INPUT / PTT
- PTT is toggled in real time by voice. "Enable push to talk" â†’ set_ptt_mode(true).
  "Disable push to talk" / "turn off push to talk" â†’ set_ptt_mode(false).

INTERRUPTION
- "stop", "cancel", "pause everything", "shut up" â†’ stop_all().
  This halts TTS, stops music, and clears pending steps.

MUSIC PRIORITY
- play_music handles the chain automatically: local â†’ Apple Music COM â†’ Apple Music URI â†’ YouTube.
- "resume music" â†’ resume_music().
- For a specific song/artist on Apple Music, use apple_music_play_song / apple_music_play_artist.

RESPONSE FORMAT (exactly one of these three shapes):
  Execution plan: {{"intent": "...", "steps": [{{"action": "name", "parameters": {{}}}}], "requires_confirmation": false}}
  Clarification:  {{"intent": "clarify", "steps": [], "error": "..."}}
  System message: {{"intent": "...", "steps": [], "error": null}}

Available actions: {actions}

Action parameter signatures:
- open_app(name: str)                  examples: "chrome", "notepad", "vscode", "terminal", "explorer", "spotify"
- close_app(name: str)
- type_text(text: str)
- press_keys(keys: str)                e.g. "ctrl+s", "alt+tab", "enter"
- create_file(path: str, content: str)
- delete_file(path: str)               requires_confirmation: true
- move_file(source: str, destination: str)
- list_files(path: str)
- web_search(query: str)               ONLY when user says "search for" / "google". Opens browser silently.
- open_url(url: str)
- get_system_info()
- get_time()
- set_volume(level: int)               0-100
- take_screenshot()
- lock_screen()
- tell_me_about(query: str)            Research + SPEAK aloud. Use for "tell me about", "what is", "who is".
- ask_claude(question: str)            Ask Claude + SPEAK. Use for coding/opinion/advice.
- daddys_home()                        "daddy's home" welcome
- play_music(track: str, artist: str)  Priority-chain playback (local â†’ Apple Music â†’ YouTube)
- resume_music()                       Resume last-played / paused track
- stop_music()
- pause_music()
- stop_all()                           Kill TTS + music + pending steps â€” use for "stop", "cancel", "pause everything"
- speaker_stop()                       Stop only the TTS utterance
- set_ptt_mode(enabled: bool)          Toggle push-to-talk at runtime
- get_ptt_mode()
- open_apple_music()
- apple_music_play_song(song: str)
- apple_music_play_artist(artist: str)
- apple_music_pause()
- apple_music_next_track()
- apple_music_previous_track()
- recall(query: str)                Search the personal knowledge base for past commands or things said.
- semantic_recall(query: str)       Deep semantic similarity search over all memory (smarter than recall)
- show_patterns()                   Show user's behavioral patterns (time-of-day habits, workflows)
- inject_knowledge(text: str)       Store a fact or note in memory permanently
- memory_status()                   Show cognitive memory statistics

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

User: "what did I say about the auth bug"
{{"intent": "recall past notes", "steps": [{{"action": "recall", "parameters": {{"query": "auth bug"}}}}], "requires_confirmation": false}}

User: "recall my notes on deployment"
{{"intent": "search knowledge base", "steps": [{{"action": "recall", "parameters": {{"query": "deployment"}}}}], "requires_confirmation": false}}

CRITICAL: Return ONLY the JSON object. No explanation, no markdown fences, no extra text."""


def _build_system_prompt(memory_ctx: dict | None = None) -> str:
    actions = ", ".join(get_available_actions())
    prompt = SYSTEM_PROMPT_TEMPLATE.format(actions=actions)

    if not memory_ctx:
        return prompt

    lines: list[str] = []

    music = memory_ctx.get("preferred_music", {})
    if music.get("track") or music.get("artist"):
        lines.append(f"- Preferred music: {music.get('artist', '').strip()} â€” {music.get('track', '').strip()}")

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

    if lines:
        prompt += "\n\nUSER PROFILE (use to personalize â€” do not echo back):\n" + "\n".join(lines)

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
    """Check if Ollama is reachable (only relevant when provider=ollama)."""
    cfg = get_config().get("llm", {})
    if cfg.get("provider", "ollama") == "claude":
        return True  # Claude API availability checked at call time
    base_url = cfg.get("base_url", "http://localhost:11434")
    try:
        resp = requests.get(f"{base_url}/api/tags", timeout=5)
        return resp.status_code == 200
    except Exception:
        return False


def _parse_via_groq(text: str, cfg: dict, memory_ctx: dict | None = None) -> IntentResponse:
    """Call the Groq API (OpenAI-compatible) to parse intent."""
    import os
    import time as _time
    from openai import OpenAI, APIConnectionError, APITimeoutError

    api_key = os.environ.get("GROQ_API_KEY", "")
    if not api_key:
        raise EnvironmentError("GROQ_API_KEY environment variable not set.")

    model = cfg.get("model", "llama-3.1-8b-instant")
    temperature = float(cfg.get("temperature", 0.1))
    max_tokens = int(cfg.get("max_tokens", 512))
    timeout_sec = float(get_config().get("timeouts", {}).get("llm_sec", 20))
    system_prompt = _build_system_prompt(memory_ctx)

    client = OpenAI(api_key=api_key, base_url="https://api.groq.com/openai/v1", timeout=timeout_sec)

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


def _parse_via_claude(text: str, cfg: dict, memory_ctx: dict | None = None) -> IntentResponse:
    """Call the Anthropic Claude API to parse intent."""
    import time as _time
    import anthropic
    from anthropic import APIConnectionError, APITimeoutError

    model = cfg.get("model", "claude-haiku-4-5-20251001")
    temperature = float(cfg.get("temperature", 0.1))
    max_tokens = int(cfg.get("max_tokens", 512))
    timeout_sec = float(get_config().get("timeouts", {}).get("llm_sec", 20))
    system_prompt = _build_system_prompt(memory_ctx)

    client = anthropic.Anthropic(timeout=timeout_sec)

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
                    system=system_prompt,
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


def _parse_via_ollama(text: str, cfg: dict, memory_ctx: dict | None = None) -> IntentResponse:
    """Call local Ollama to parse intent."""
    import time as _time

    base_url = cfg.get("base_url", "http://localhost:11434")
    model = cfg.get("model", "phi3:mini")
    temperature = cfg.get("temperature", 0.1)
    max_tokens = cfg.get("max_tokens", 512)
    timeout_sec = float(get_config().get("timeouts", {}).get("llm_sec", 20))
    system_prompt = _build_system_prompt(memory_ctx)

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


def parse_intent(text: str, memory_ctx: dict | None = None) -> IntentResponse:
    """Route intent parsing to the configured provider (groq, claude, or ollama)."""
    cfg = get_config().get("llm", {})
    provider = cfg.get("provider", "ollama").lower()
    if provider == "groq":
        return _parse_via_groq(text, cfg, memory_ctx)
    if provider == "claude":
        return _parse_via_claude(text, cfg, memory_ctx)
    return _parse_via_ollama(text, cfg, memory_ctx)
