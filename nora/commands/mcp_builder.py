"""Build MCP servers on demand — hands Claude Code (via the `claude` CLI, no
API key needed) a natural-language spec and lets it write a new mcpforge-based
server file. Runs in the background since code generation can take well past
NORA's per-command timeout; the result is spoken when it's ready.
"""
from __future__ import annotations

import logging
import re
import shutil
import subprocess
import sys
import threading
from pathlib import Path

from nora.command_engine import register
from nora.commands.ask_claude import CLAUDE_MODELS

logger = logging.getLogger("nora.commands.mcp_builder")

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_MCP_SERVERS_DIR = _REPO_ROOT / "mcp_servers"
_BUILD_TIMEOUT_SEC = 600
_BUILD_MODEL = CLAUDE_MODELS["sonnet"]

# Only filesystem tools — no Bash, no network — so a bad spec can produce a
# bad file but never run arbitrary commands.
_ALLOWED_TOOLS = "Read,Write,Edit,Glob"


def _slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", text.strip().lower()).strip("_")
    return slug or "custom"


def _derive_name(name: str, description: str) -> str:
    if name.strip():
        return _slugify(name)
    words = re.findall(r"[a-zA-Z0-9]+", description)[:4]
    return _slugify("_".join(words))


def _build_prompt(server_name: str, description: str) -> str:
    return f"""Write a new MCP (Model Context Protocol) server for the NORA voice
assistant project, using its `mcpforge` framework (stdlib only — don't add new
pip dependencies unless the task genuinely requires one; if so, name it clearly
in your final summary).

Task: {description}

Requirements:
1. Create exactly one file named "{server_name}_server.py" in the current
   directory. Do not create or edit anything else.
2. Follow this pattern exactly:

     import sys
     from pathlib import Path
     sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
     from mcpforge import MCPServer

     server = MCPServer("{server_name}", version="1.0")

     @server.tool()
     def some_tool(arg: str) -> str:
         \"\"\"One-line description the LLM will see.

         arg: what this parameter is for
         \"\"\"
         ...

     if __name__ == "__main__":
         server.run()

3. Give each tool a clear docstring: first line is the tool description, then
   one "param_name: description" line per parameter — mcpforge parses both
   into the JSON Schema it sends to clients.
4. If the task needs credentials (API key, OAuth token, session cookie, etc.),
   read them from an environment variable via os.environ — never hardcode a
   secret — and document the variable name in the module's top docstring.
5. If the target has no public API and no MCP of its own, prefer plain HTTP
   requests against documented/public endpoints where possible. Only resort to
   browser automation if the task truly requires it, and say so explicitly in
   your summary — that's a fragile approach worth flagging, not defaulting to.
6. Keep it minimal and correct for the stated task. No speculative extra
   tools, no framework changes.

When you're done, state in your final message: the file name, the list of
tool names you added, and any environment variables the user needs to set.
"""


def _run_claude_build(server_name: str, description: str) -> None:
    from nora import speaker as _spk

    target = _MCP_SERVERS_DIR / f"{server_name}_server.py"
    prompt = _build_prompt(server_name, description)
    try:
        result = subprocess.run(
            [
                "claude", "-p", prompt,
                "--model", _BUILD_MODEL,
                "--permission-mode", "acceptEdits",
                "--allowedTools", _ALLOWED_TOOLS,
                "--output-format", "text",
            ],
            cwd=str(_MCP_SERVERS_DIR),
            capture_output=True, text=True, timeout=_BUILD_TIMEOUT_SEC,
        )
    except subprocess.TimeoutExpired:
        logger.error("mcp build timed out for %s", server_name)
        _spk.speak(f"Building the {server_name} MCP server timed out — Claude didn't finish in time.")
        return
    except Exception as exc:
        logger.error("mcp build subprocess failed for %s: %s", server_name, exc)
        _spk.speak(f"Couldn't reach Claude to build the {server_name} MCP server.")
        return

    if not target.exists():
        logger.error("claude build: %s not created. stdout=%s stderr=%s",
                     target, result.stdout[:300], result.stderr[:300])
        _spk.speak(f"Claude didn't create the {server_name} server file — something went wrong.")
        return

    compiled = subprocess.run(
        [sys.executable, "-m", "py_compile", str(target)],
        capture_output=True, text=True,
    )
    if compiled.returncode != 0:
        logger.error("py_compile failed for %s: %s", target, compiled.stderr[:300])
        _spk.speak(f"I created {server_name}_server.py but it has a syntax error — you'll need to fix it by hand.")
        return

    logger.info("MCP build succeeded: %s", target)
    _spk.speak(
        f"Done — {server_name}_server.py is ready in mcp_servers. "
        f"Add it under mcp_servers in config.yaml to make it voice-callable."
    )


@register(
    "build_mcp_server",
    sig="build_mcp_server(description: str, name: str = '')",
    description="Ask Claude Code to write a new mcpforge MCP server from a natural-language spec (builds in the background, speaks when ready)",
    category="mcp",
    risk="medium",
    requires_confirmation=True,
)
def build_mcp_server(description: str, name: str = "") -> str:
    if not shutil.which("claude"):
        return "Claude CLI not found. Make sure Claude Code is installed and `claude` is in PATH."

    server_name = _derive_name(name, description)
    target = _MCP_SERVERS_DIR / f"{server_name}_server.py"
    if target.exists():
        return f"mcp_servers/{server_name}_server.py already exists — pick a different name."

    _MCP_SERVERS_DIR.mkdir(parents=True, exist_ok=True)
    thread = threading.Thread(
        target=_run_claude_build,
        args=(server_name, description),
        daemon=True,
        name=f"nora-mcp-build-{server_name[:20]}",
    )
    thread.start()
    logger.info("Started MCP build for %s: %s", server_name, description)
    return f"On it — asking Claude to build the {server_name} MCP server. I'll let you know when it's ready."
