"""--version and --selftest on every entry point (design.md, Packaging and installation: Build)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

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


def test_cli_unknown_command_exits_2():
    result = run_cli("frobnicate")
    assert result.returncode == 2


def test_cli_run_render_mode_needs_no_devices(monkeypatch, capsys):
    from tsunami import bootstrap, cli

    monkeypatch.setattr(bootstrap, "configure_keyring", lambda: None)  # keep the test keyring
    fixture = str(Path(__file__).parents[1] / "fixtures" / "workspace")
    code = cli.main(
        [
            "run",
            "plays/push/add-vlan.yml",
            "-w",
            fixture,
            "--mode",
            "render",
            "--set",
            "vlan_id=30",
            "--set",
            "vlan_name=Cameras",
        ]
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "sa-as01: success" in out and "completed" in out


def test_gui_version(capsys):
    assert app.main(["--version"]) == 0
    assert capsys.readouterr().out.strip() == f"tsunami {__version__}"


def test_mcp_version(capsys):
    assert server.main(["--version"]) == 0
    assert capsys.readouterr().out.strip() == f"tsunami-mcp {__version__}"
