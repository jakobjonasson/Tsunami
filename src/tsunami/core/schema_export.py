"""JSON Schema export from the models, plus .vscode/settings.json.

See design.md, Workspace layout: Init workspace.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from tsunami import __version__
from tsunami.core.issues import TsunamiError
from tsunami.core.models.inventory import CustomerFile, DevicesFile, VarsFile
from tsunami.core.models.play import Play
from tsunami.core.models.workspace import WorkspaceFile

SCHEMA_DIR = ".schema"
VERSION_MARKER = ".tsunami-version"
_DRAFT = "https://json-schema.org/draft/2020-12/schema"

SCHEMA_FILES: dict[str, type[BaseModel]] = {
    "play.schema.json": Play,
    "customer.schema.json": CustomerFile,
    "devices.schema.json": DevicesFile,
    "vars.schema.json": VarsFile,
    "workspace.schema.json": WorkspaceFile,
}

VSCODE_YAML_SCHEMAS: dict[str, str | list[str]] = {
    "./.schema/play.schema.json": ["plays/**/*.yml", "drafts/plays/**/*.yml"],
    "./.schema/customer.schema.json": "inventory/customers/*/customer.yml",
    "./.schema/devices.schema.json": "inventory/customers/*/devices.yml",
    "./.schema/vars.schema.json": [
        "inventory/global.yml",
        "inventory/roles/*.yml",
        "inventory/customers/*/sites/*.yml",
    ],
    "./.schema/workspace.schema.json": "workspace.yml",
}


def _write_text(path: Path, text: str) -> None:
    # newline="" keeps LF on Windows.
    with path.open("w", encoding="utf-8", newline="") as f:
        f.write(text)


def _schema_for(model: type[BaseModel]) -> dict[str, Any]:
    schema = model.model_json_schema()
    title = schema.get("title", model.__name__)
    rest = {k: v for k, v in schema.items() if k not in ("$schema", "title")}
    return {"$schema": _DRAFT, "title": title, **rest}


def write_schemas(workspace: Path) -> list[Path]:
    """Write every schema to <workspace>/.schema/ and a version marker."""
    out = workspace / SCHEMA_DIR
    out.mkdir(parents=True, exist_ok=True)
    written = []
    for file_name, model in SCHEMA_FILES.items():
        path = out / file_name
        _write_text(path, json.dumps(_schema_for(model), indent=2) + "\n")
        written.append(path)
    marker = out / VERSION_MARKER
    _write_text(marker, __version__)
    written.append(marker)
    return written


def ensure_schemas(workspace: Path) -> bool:
    """Regenerate schemas if the version marker is missing or stale. True if written."""
    marker = workspace / SCHEMA_DIR / VERSION_MARKER
    try:
        current = marker.read_text(encoding="utf-8").strip()
    except OSError:
        current = None
    if current == __version__:
        return False
    write_schemas(workspace)
    return True


def write_vscode_settings(workspace: Path) -> Path:
    """Create or merge .vscode/settings.json so "yaml.schemas" holds Tsunami's mappings."""
    path = workspace / ".vscode" / "settings.json"
    settings: dict[str, Any] = {}
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8-sig"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise TsunamiError(
                f"{path} is not plain JSON (comments?): {exc}. Add the yaml.schemas "
                "entries from design.md, Init workspace, by hand."
            ) from exc
        if not isinstance(loaded, dict):
            raise TsunamiError(f"{path} must contain a JSON object")
        settings = loaded
    existing = settings.get("yaml.schemas")
    if existing is None:
        existing = {}
    if not isinstance(existing, dict):
        raise TsunamiError(f"{path}: 'yaml.schemas' must be an object")
    # Replace only Tsunami's own keys; other mappings stay as they were.
    merged = {k: v for k, v in existing.items() if k not in VSCODE_YAML_SCHEMAS}
    merged.update(VSCODE_YAML_SCHEMAS)
    settings["yaml.schemas"] = merged
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_text(path, json.dumps(settings, indent=2) + "\n")
    return path
