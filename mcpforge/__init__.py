"""mcpforge — build MCP servers from plain Python functions. Stdlib only.

    from mcpforge import MCPServer
    server = MCPServer("myserver")

    @server.tool()
    def do_thing(arg: str) -> str:
        \"\"\"One-line description the LLM will see.\"\"\"
        ...

    server.run()

Scaffold a new server:  python -m mcpforge new <name>
"""
from mcpforge.core import LATEST_VERSION, SUPPORTED_VERSIONS, MCPServer, build_input_schema

__all__ = ["MCPServer", "build_input_schema", "SUPPORTED_VERSIONS", "LATEST_VERSION"]
__version__ = "0.1.0"
