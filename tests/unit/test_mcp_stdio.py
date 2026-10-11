"""The real tsunami-mcp process over stdio, relaying to a bridge server in this process.

The bridge server stands in for the Tsunami window: it runs the tools only while
"unlocked". Nothing but protocol may reach the MCP process's stdout.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
from pathlib import Path

import pytest

from tsunami.core.history import History
from tsunami.core.models.policy import McpPolicy
from tsunami.core.models.settings import AppSettings, save_settings
from tsunami.core.policy import save_policy
from tsunami.core.secret_store import KeyringSecretStore
from tsunami.mcp_server.bridge import BridgeServer, token_path
from tsunami.mcp_server.enforcement import McpApp
from tsunami.mcp_server.tools import Tools

FIXTURE = Path(__file__).parents[1] / "fixtures" / "workspace"


async def _session() -> tuple[list[str], object]:
    from mcp.client.session import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    params = StdioServerParameters(
        command=sys.executable,
        args=["-c", "import sys; from tsunami.mcp_server.server import main; sys.exit(main([]))"],
        env=dict(os.environ),
    )
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        tools = await session.list_tools()
        result = await session.call_tool("list_customers", {})
        return [t.name for t in tools.tools], result


def run_session():
    return asyncio.run(asyncio.wait_for(_session(), timeout=60))


@pytest.fixture
def gui(tmp_path, app_paths):
    """A bridge server like the Tsunami window's, with a lock switch."""
    root = tmp_path / "ws"
    shutil.copytree(FIXTURE, root)
    save_settings(AppSettings(workspace=str(root)), app_paths.settings_file)
    save_policy(McpPolicy(enabled=True), app_paths.policy_file)
    history = History(tmp_path / "history.db")
    secrets = KeyringSecretStore(index_path=tmp_path / "idx.json")
    state = {"unlocked": True}
    tools = Tools(McpApp(app_paths, history=history, secrets=secrets))
    server = BridgeServer(app_paths, tools.dispatch, is_unlocked=lambda: state["unlocked"])
    server.start()
    yield state, history
    server.stop()
    history.close()


def test_calls_go_through_while_unlocked(gui, app_paths):
    _state, history = gui
    names, result = run_session()
    assert len(names) == 18 and "request_run" in names
    assert not result.is_error and "acme" in str(result.content)
    assert (app_paths.logs_dir / "mcp.log").is_file()
    rows = history._query("SELECT tool, status FROM mcp_calls")
    assert [tuple(r) for r in rows] == [("list_customers", "ok")]  # audited by the GUI side


def test_locked_tsunami_answers_403(gui):
    state, _history = gui
    state["unlocked"] = False
    _names, result = run_session()
    assert result.is_error and "403 Tsunami is locked" in str(result.content)


def test_closed_tsunami_answers_503(app_paths):
    assert not token_path(app_paths).exists()
    names, result = run_session()
    assert len(names) == 18  # the server still starts and lists its tools
    assert result.is_error and "503 Tsunami is not running" in str(result.content)
