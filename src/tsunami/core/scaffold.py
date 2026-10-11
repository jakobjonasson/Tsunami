"""Init workspace: create the skeleton, schemas and VS Code settings.

See design.md, Workspace layout: Init workspace.
"""

from __future__ import annotations

from pathlib import Path

from tsunami.core.issues import TsunamiError
from tsunami.core.models.common import Platform, PlatformFamily
from tsunami.core.models.workspace import SCHEMA_VERSION
from tsunami.core.schema_export import write_schemas, write_vscode_settings

_PLAY_KINDS = ("initial", "push", "baseline")


def _dirs() -> list[str]:
    dirs = ["inventory/roles", "inventory/customers", "snippets/customers"]
    dirs += [f"plays/{k}" for k in _PLAY_KINDS]
    dirs += [f"drafts/plays/{k}" for k in _PLAY_KINDS]
    for root in ("snippets", "drafts/snippets"):
        dirs += [f"{root}/common/{f.value}" for f in PlatformFamily]
        dirs += [f"{root}/platform/{p.value}" for p in Platform]
    dirs.append("drafts/snippets/customers")
    return dirs


def _yaml_quote(name: str) -> str:
    return '"' + name.replace("\\", "\\\\").replace('"', '\\"') + '"'


def init_workspace(path: Path, *, name: str) -> list[Path]:
    """Create the workspace skeleton at path. Returns the files and dirs created."""
    if not name.strip() or any(ord(c) < 32 or ord(c) == 127 for c in name):
        raise TsunamiError("workspace name must be non-empty and contain no control characters")
    if (path / "workspace.yml").exists():
        raise TsunamiError(f"{path} is already a Tsunami workspace (workspace.yml exists)")

    created: list[Path] = []

    def write(rel: str, text: str) -> None:
        f = path / rel
        if f.exists():
            return  # never overwrite
        f.parent.mkdir(parents=True, exist_ok=True)
        with f.open("w", encoding="utf-8", newline="") as fh:
            fh.write(text)
        created.append(f)

    path.mkdir(parents=True, exist_ok=True)
    for rel in _dirs():
        d = path / rel
        if not d.is_dir():
            d.mkdir(parents=True)
            created.append(d)
        # Leaf directories get a .gitkeep so git keeps the structure.
        write(f"{rel}/.gitkeep", "")

    write(
        "workspace.yml",
        "# Tsunami workspace settings.\n"
        f"name: {_yaml_quote(name)}\n"
        f"schema_version: {SCHEMA_VERSION}\n"
        "defaults:\n"
        f"  platform: {Platform.IOS_XE.value}\n"
        "  batch_size: 5\n",
    )
    write("inventory/global.yml", "# Variables that apply to every device.\n{}\n")
    write(
        ".gitignore",
        "# Guards: Tsunami never writes these into a workspace.\n*.cfg\n*.log\n*.db\n",
    )
    created.extend(write_schemas(path))
    created.append(write_vscode_settings(path))
    return created
