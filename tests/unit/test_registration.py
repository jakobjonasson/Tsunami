"""Claude Desktop registration. Always uses explicit temp paths."""

from __future__ import annotations

import json

import pytest

from tsunami.core.issues import TsunamiError
from tsunami.core.registration import (
    claude_code_command,
    claude_desktop_config_path,
    register_claude_desktop,
)

EXE = r"C:\Apps\Tsunami\tsunami-mcp.exe"


def test_creates_new_file(tmp_path):
    cfg = tmp_path / "Claude" / "claude_desktop_config.json"
    assert register_claude_desktop(EXE, cfg) == cfg
    data = json.loads(cfg.read_text())
    entry = data["mcpServers"]["tsunami"]
    assert entry["args"] == []
    assert entry["command"].lower().endswith("tsunami-mcp.exe")


def test_merges_and_backs_up(tmp_path):
    cfg = tmp_path / "c.json"
    original = {"theme": "x", "mcpServers": {"other": {"command": "o", "args": ["1"]}}}
    cfg.write_text(json.dumps(original))
    bak = register_claude_desktop(EXE, cfg)
    assert bak != cfg and json.loads(bak.read_text()) == original
    data = json.loads(cfg.read_text())
    assert data["theme"] == "x" and data["mcpServers"]["other"] == original["mcpServers"]["other"]
    assert "tsunami" in data["mcpServers"]


def test_invalid_json_untouched(tmp_path):
    cfg = tmp_path / "c.json"
    cfg.write_text("{not json")
    with pytest.raises(TsunamiError):
        register_claude_desktop(EXE, cfg)
    assert cfg.read_text() == "{not json"
    assert not (tmp_path / "c.json.bak").exists()


def test_command_is_absolute(tmp_path):
    cfg = tmp_path / "c.json"
    register_claude_desktop("tsunami-mcp.exe", cfg)
    from pathlib import Path

    assert Path(json.loads(cfg.read_text())["mcpServers"]["tsunami"]["command"]).is_absolute()


def test_reregister_updates_in_place(tmp_path):
    cfg = tmp_path / "c.json"
    register_claude_desktop(EXE, cfg)
    register_claude_desktop(r"D:\New\tsunami-mcp.exe", cfg)
    servers = json.loads(cfg.read_text())["mcpServers"]
    assert list(servers) == ["tsunami"]
    assert servers["tsunami"]["command"].startswith("D:")


def test_default_path_uses_appdata(monkeypatch, tmp_path):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    assert claude_desktop_config_path() == tmp_path / "Claude" / "claude_desktop_config.json"


def test_claude_code_command():
    cmd = claude_code_command(EXE)
    assert cmd.startswith("claude mcp add --scope user tsunami -- ")
    assert "tsunami-mcp.exe" in cmd
