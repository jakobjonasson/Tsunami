"""tsunami-mcp.exe entry point: the local stdio MCP server (design.md, MCP interface).

Stdout belongs to the MCP protocol. Phase 5 configures logging to logs\\mcp.log before
importing anything that logs, then starts FastMCP. Nothing reachable from here prints.
"""

from __future__ import annotations

import sys

from tsunami import bootstrap


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    code = bootstrap.handle_common_flags(argv, "tsunami-mcp")
    if code is not None:
        return code
    sys.stderr.write("tsunami-mcp: the MCP server is not implemented yet (Phase 5)\n")
    return 1
