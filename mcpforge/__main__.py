"""Scaffolder:  python -m mcpforge new <name> [directory]

Generates <name>_server.py from a working template and prints the config
snippets for wiring it into NORA (config.yaml) and Claude Code.
"""
from __future__ import annotations

import sys
from pathlib import Path

_TEMPLATE = '''"""{name} — MCP server built with mcpforge.

Run standalone:   python {filename}
Wire into NORA:   add the mcp_servers entry printed by the scaffolder.
Wire into Claude: claude mcp add {name} -- python {filename}
"""
from __future__ import annotations

import sys
from pathlib import Path

# Make mcpforge importable when this file lives outside the repo root
sys.path.insert(0, str(Path(__file__).resolve().parent{parents}))

from mcpforge import MCPServer

server = MCPServer("{name}", version="1.0")


@server.tool()
def example_tool(text: str, times: int = 1) -> str:
    """Repeat the given text. Replace me with something real.

    text: what to repeat
    times: how many times to repeat it
    """
    return " ".join([text] * times)


if __name__ == "__main__":
    server.run()
'''


def main(argv: list[str]) -> int:
    if len(argv) < 2 or argv[0] != "new":
        print(__doc__)
        return 1
    name = argv[1].replace("-", "_")
    target_dir = Path(argv[2]) if len(argv) > 2 else Path("mcp_servers")
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / f"{name}_server.py"
    if path.exists():
        print(f"refusing to overwrite existing {path}")
        return 1

    # How many .parent hops from the server file back to the repo root
    # (where mcpforge/ lives). Falls back to one hop for out-of-tree targets,
    # where mcpforge must be on PYTHONPATH or copied alongside anyway.
    try:
        depth = len(path.resolve().parent.relative_to(Path.cwd().resolve()).parts)
    except ValueError:
        depth = 1
    parents = ".parent" * depth
    path.write_text(_TEMPLATE.format(name=name, filename=path.name, parents=parents),
                    encoding="utf-8")

    print(f"created {path}\n")
    print("── NORA: add under mcp_servers: in config.yaml ─────────────")
    print(f'  - name: {name}')
    print(f'    transport: stdio')
    print(f'    command: ["python", "{path}"]\n')
    print("── Claude Code ─────────────────────────────────────────────")
    print(f"  claude mcp add {name} -- python {path.resolve()}\n")
    print(f"── Test it ─────────────────────────────────────────────────")
    print(f"  echo '{{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"tools/list\"}}' | python {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
