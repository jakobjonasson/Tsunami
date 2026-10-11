"""Register tsunami-mcp.exe in claude_desktop_config.json, keeping a .bak copy.

Claude Desktop reads its config from one of two places. The standard installer reads
%APPDATA%/Claude. The Microsoft Store (MSIX) build reads a virtualized copy in its
package folder, %LOCALAPPDATA%/Packages/Claude_<id>/LocalCache/Roaming/Claude, and
ignores the %APPDATA% file once that copy exists. Registration writes every config the
installed app may read.

See design.md, Packaging and installation: Register with Claude Desktop.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from collections.abc import Mapping
from pathlib import Path

from tsunami.core.issues import TsunamiError

SERVER_NAME = "tsunami"
CONFIG_NAME = "claude_desktop_config.json"


def claude_desktop_config_paths(environ: Mapping[str, str] | None = None) -> list[Path]:
    """Every claude_desktop_config.json that Claude Desktop may read on this machine."""
    env = os.environ if environ is None else environ
    appdata = env.get("APPDATA")
    if not appdata:
        raise TsunamiError("environment variable not set: APPDATA")
    standard = Path(appdata) / "Claude" / CONFIG_NAME
    paths: list[Path] = []
    local = env.get("LOCALAPPDATA")
    if local:
        for package in sorted((Path(local) / "Packages").glob("Claude_*")):
            virtual = package / "LocalCache" / "Roaming" / "Claude"
            if virtual.is_dir():  # the Store build has run and keeps its own config
                paths.append(virtual / CONFIG_NAME)
    if standard.is_file() or not paths:
        paths.append(standard)
    return paths


def register_claude_desktop_all(exe_path: Path) -> list[Path]:
    """Register in every config Claude Desktop may read. Returns the files written."""
    paths = claude_desktop_config_paths()
    for path in paths:
        register_claude_desktop(exe_path, path)
    return paths


def register_claude_desktop(exe_path: Path, config_path: Path) -> Path:
    """Add or update the tsunami server entry. Returns the .bak path, or the config path
    if the file did not exist. Every other key and server is preserved."""
    config_path = Path(config_path)
    exe = Path(os.path.abspath(exe_path))
    data: dict = {}
    backup = config_path
    existed = config_path.is_file()
    if existed:
        try:
            text = config_path.read_text(encoding="utf-8-sig")
            data = json.loads(text) if text.strip() else {}
        except (ValueError, OSError):
            raise TsunamiError(
                f"{config_path.name} is not valid JSON; it was left unchanged"
            ) from None
        if not isinstance(data, dict):
            raise TsunamiError(f"{config_path.name} is not a JSON object; it was left unchanged")
    servers = data.get("mcpServers")
    if servers is None:
        servers = data["mcpServers"] = {}
    if not isinstance(servers, dict):
        raise TsunamiError(f"mcpServers in {config_path.name} is not an object; left unchanged")
    if existed:
        backup = config_path.with_name(config_path.name + ".bak")
        shutil.copy2(config_path, backup)
    servers[SERVER_NAME] = {"command": str(exe), "args": []}
    config_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{config_path.name}.", dir=config_path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(data, handle, indent=2)
            handle.write("\n")
        os.replace(tmp, config_path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return backup


def claude_code_command(exe_path: Path) -> str:
    """The `claude mcp add` command that registers the exe for the current user."""
    exe = os.path.abspath(exe_path)
    return f'claude mcp add --scope user {SERVER_NAME} -- "{exe}"'
