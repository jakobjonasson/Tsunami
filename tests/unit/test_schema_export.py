"""Schema export and VS Code settings merge (design.md, Workspace layout: Init workspace)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tsunami import __version__
from tsunami.core.issues import TsunamiError
from tsunami.core.schema_export import (
    SCHEMA_FILES,
    VSCODE_YAML_SCHEMAS,
    ensure_schemas,
    write_schemas,
    write_vscode_settings,
)


def test_write_schemas_creates_parseable_files(tmp_path: Path) -> None:
    write_schemas(tmp_path)
    for name in SCHEMA_FILES:
        text = (tmp_path / ".schema" / name).read_text(encoding="utf-8")
        assert text.endswith("\n")
        assert "\r" not in text
        data = json.loads(text)
        assert data["$schema"].startswith("https://json-schema.org/")
        assert data["title"]
    play = (tmp_path / ".schema" / "play.schema.json").read_text(encoding="utf-8")
    assert "steps" in play
    assert "prompts" in play
    assert (tmp_path / ".schema" / ".tsunami-version").read_text(encoding="utf-8") == __version__


def test_ensure_schemas_follows_marker(tmp_path: Path) -> None:
    assert ensure_schemas(tmp_path) is True  # no marker yet
    assert ensure_schemas(tmp_path) is False
    marker = tmp_path / ".schema" / ".tsunami-version"
    marker.write_text("0.0.0-old", encoding="utf-8")
    assert ensure_schemas(tmp_path) is True
    assert marker.read_text(encoding="utf-8") == __version__


def test_vscode_settings_created(tmp_path: Path) -> None:
    path = write_vscode_settings(tmp_path)
    assert json.loads(path.read_text(encoding="utf-8"))["yaml.schemas"] == VSCODE_YAML_SCHEMAS


def test_vscode_settings_merge_keeps_other_entries(tmp_path: Path) -> None:
    path = tmp_path / ".vscode" / "settings.json"
    path.parent.mkdir()
    path.write_text(
        json.dumps(
            {
                "editor.tabSize": 2,
                "yaml.schemas": {
                    "https://example.test/x.json": "x/*.yml",
                    "./.schema/play.schema.json": "stale/*.yml",
                },
            }
        ),
        encoding="utf-8",
    )
    write_vscode_settings(tmp_path)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["editor.tabSize"] == 2
    assert data["yaml.schemas"]["https://example.test/x.json"] == "x/*.yml"
    assert (
        data["yaml.schemas"]["./.schema/play.schema.json"]
        == VSCODE_YAML_SCHEMAS["./.schema/play.schema.json"]
    )


def test_vscode_settings_jsonc_raises_and_is_untouched(tmp_path: Path) -> None:
    path = tmp_path / ".vscode" / "settings.json"
    path.parent.mkdir()
    original = '{\n  // my comment\n  "editor.tabSize": 2\n}\n'
    path.write_text(original, encoding="utf-8")
    with pytest.raises(TsunamiError):
        write_vscode_settings(tmp_path)
    assert path.read_text(encoding="utf-8") == original
