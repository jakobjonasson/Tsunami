"""MCP stdio smoke test against a built tsunami-mcp.exe.

Starts the exe with APPDATA and LOCALAPPDATA pointed at temp dirs, initializes, lists the
tools and exits. Run: uv run python packaging/smoke_mcp.py [path\to\tsunami-mcp.exe]
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from tsunami.mcp_server.tools import TOOL_NAMES

# The frozen server must list exactly the tools the source defines; never a hard-coded count.
EXPECTED_TOOLS = len(TOOL_NAMES)
DEFAULT_EXE = Path(__file__).parents[1] / "dist" / "Tsunami" / "tsunami-mcp.exe"


async def smoke(exe: Path) -> int:
    with tempfile.TemporaryDirectory(prefix="tsunami-smoke-") as tmp:
        env = {
            **os.environ,
            "APPDATA": str(Path(tmp) / "roaming"),
            "LOCALAPPDATA": str(Path(tmp) / "local"),
        }
        params = StdioServerParameters(command=str(exe), args=[], env=env)
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.list_tools()
                names = sorted(tool.name for tool in result.tools)
    print(f"{len(names)} tools: {', '.join(names)}")
    if set(names) != TOOL_NAMES:
        missing, extra = sorted(TOOL_NAMES - set(names)), sorted(set(names) - TOOL_NAMES)
        print(f"expected {EXPECTED_TOOLS} tools; missing {missing}, extra {extra}", file=sys.stderr)
        return 1
    print("smoke ok")
    return 0


def main() -> int:
    exe = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_EXE
    if not exe.is_file():
        print(f"not found: {exe}", file=sys.stderr)
        return 1
    return asyncio.run(asyncio.wait_for(smoke(exe), timeout=60))


if __name__ == "__main__":
    raise SystemExit(main())
