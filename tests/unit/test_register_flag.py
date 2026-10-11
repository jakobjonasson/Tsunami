"""--register-claude-desktop: the installer's last page (design.md, Packaging and installation)."""

from __future__ import annotations

import json
import sys

from tsunami import bootstrap
from tsunami.gui import app


def test_flag_registers_sibling_mcp_exe(tmp_path, monkeypatch, capsys):
    install = tmp_path / "install"
    install.mkdir()
    (install / "tsunami-mcp.exe").write_bytes(b"")
    monkeypatch.setattr(sys, "executable", str(install / "tsunami.exe"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "roaming"))

    assert app.main(["--register-claude-desktop"]) == 0

    config = tmp_path / "roaming" / "Claude" / "claude_desktop_config.json"
    entry = json.loads(config.read_text(encoding="utf-8"))["mcpServers"]["tsunami"]
    assert entry["command"] == str(install / "tsunami-mcp.exe")
    assert "Registered with Claude Desktop" in capsys.readouterr().out


def test_flag_fails_when_mcp_exe_is_missing(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sys, "executable", str(tmp_path / "tsunami.exe"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "roaming"))

    assert bootstrap.handle_common_flags(["--register-claude-desktop"], "tsunami") == 1
    assert "tsunami-mcp.exe not found" in capsys.readouterr().err
    assert not (tmp_path / "roaming").exists()
