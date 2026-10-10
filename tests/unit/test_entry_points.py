"""--version and --selftest on every entry point (design.md, Packaging and installation: Build)."""

from __future__ import annotations

import subprocess
import sys

from tsunami import __version__
from tsunami.gui import app
from tsunami.mcp_server import server


def run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "tsunami", *args],
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_cli_version():
    result = run_cli("--version")
    assert result.returncode == 0
    assert result.stdout.strip() == f"tsunami {__version__}"


def test_cli_selftest():
    result = run_cli("--selftest")
    assert result.returncode == 0, result.stderr
    assert "selftest ok" in result.stdout


def test_cli_unimplemented_command_exits_2():
    result = run_cli("render")
    assert result.returncode == 2
    assert "Phase 2" in result.stderr


def test_gui_version(capsys):
    assert app.main(["--version"]) == 0
    assert capsys.readouterr().out.strip() == f"tsunami {__version__}"


def test_mcp_version(capsys):
    assert server.main(["--version"]) == 0
    assert capsys.readouterr().out.strip() == f"tsunami-mcp {__version__}"
