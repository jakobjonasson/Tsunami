"""The real server over stdio: the handshake works and nothing but protocol reaches stdout."""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
from pathlib import Path

from tsunami.core.models.policy import McpPolicy
from tsunami.core.models.settings import AppSettings, save_settings
from tsunami.core.policy import save_policy

FIXTURE = Path(__file__).parents[1] / "fixtures" / "workspace"


async def _session(env: dict[str, str]) -> tuple[list[str], str]:
    from mcp.client.session import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    params = StdioServerParameters(
        command=sys.executable,
        args=["-c", "import sys; from tsunami.mcp_server.server import main; sys.exit(main([]))"],
        env=env,
    )
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        tools = await session.list_tools()
        result = await session.call_tool("list_customers", {})
        return [t.name for t in tools.tools], str(result.content)


def test_stdio_handshake(tmp_path, app_paths):
    root = tmp_path / "ws"
    shutil.copytree(FIXTURE, root)
    save_settings(AppSettings(workspace=str(root)), app_paths.settings_file)
    save_policy(McpPolicy(enabled=True), app_paths.policy_file)

    names, customers = asyncio.run(asyncio.wait_for(_session(dict(os.environ)), timeout=60))
    assert len(names) == 18 and "request_run" in names
    assert "acme" in customers
    assert (app_paths.logs_dir / "mcp.log").is_file()
