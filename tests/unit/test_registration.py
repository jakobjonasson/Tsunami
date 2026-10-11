"""Claude Desktop registration. Always uses explicit temp paths."""

from __future__ import annotations

import json

import pytest

from tsunami.core.issues import TsunamiError
from tsunami.core.registration import (
    claude_code_command,
    claude_desktop_config_paths,
    register_claude_desktop,
    register_claude_desktop_all,
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


def _env(tmp_path):
    return {"APPDATA": str(tmp_path / "roaming"), "LOCALAPPDATA": str(tmp_path / "local")}


def _store_config_dir(tmp_path):
    folder = tmp_path / "local/Packages/Claude_pzs8sxrjxfjjc/LocalCache/Roaming/Claude"
    folder.mkdir(parents=True)
    return folder


def test_standard_install_uses_appdata(tmp_path):
    standard = tmp_path / "roaming/Claude/claude_desktop_config.json"
    assert claude_desktop_config_paths(_env(tmp_path)) == [standard]


def test_store_install_uses_its_package_folder(tmp_path):
    store = _store_config_dir(tmp_path) / "claude_desktop_config.json"
    assert claude_desktop_config_paths(_env(tmp_path)) == [store]


def test_both_installs_get_registered(tmp_path):
    store = _store_config_dir(tmp_path) / "claude_desktop_config.json"
    standard = tmp_path / "roaming/Claude/claude_desktop_config.json"
    standard.parent.mkdir(parents=True)
    standard.write_text("{}")
    assert claude_desktop_config_paths(_env(tmp_path)) == [store, standard]


def test_store_package_that_never_ran_falls_back_to_appdata(tmp_path):
    (tmp_path / "local/Packages/Claude_pzs8sxrjxfjjc").mkdir(parents=True)
    standard = tmp_path / "roaming/Claude/claude_desktop_config.json"
    assert claude_desktop_config_paths(_env(tmp_path)) == [standard]


def test_register_all_keeps_the_store_config_settings(tmp_path, monkeypatch):
    for name, value in _env(tmp_path).items():
        monkeypatch.setenv(name, value)
    store = _store_config_dir(tmp_path) / "claude_desktop_config.json"
    original = {"mcpServers": {"cml": {"command": "npx"}}, "preferences": {"sidebarMode": "chat"}}
    store.write_text(json.dumps(original))
    assert register_claude_desktop_all(EXE) == [store]
    data = json.loads(store.read_text())
    assert data["preferences"] == original["preferences"]
    assert set(data["mcpServers"]) == {"cml", "tsunami"}
    assert json.loads(store.with_name(store.name + ".bak").read_text()) == original


def test_claude_code_command():
    cmd = claude_code_command(EXE)
    assert cmd.startswith("claude mcp add --scope user tsunami -- ")
    assert "tsunami-mcp.exe" in cmd
