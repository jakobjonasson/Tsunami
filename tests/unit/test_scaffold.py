"""Init workspace (design.md, Workspace layout: Init workspace)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from tsunami import cli
from tsunami.core.issues import TsunamiError
from tsunami.core.models.common import Platform, PlatformFamily
from tsunami.core.models.workspace import WorkspaceFile
from tsunami.core.scaffold import init_workspace


def test_creates_expected_dirs(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    init_workspace(ws, name="demo")
    expected = [
        "inventory/roles",
        "inventory/customers",
        "snippets/customers",
        ".schema",
        ".vscode",
    ]
    expected += [
        f"{root}/{k}" for root in ("plays", "drafts/plays") for k in ("initial", "push", "baseline")
    ]
    for root in ("snippets", "drafts/snippets"):
        expected += [f"{root}/common/{f.value}" for f in PlatformFamily]
        expected += [f"{root}/platform/{p.value}" for p in Platform]
    expected.append("drafts/snippets/customers")
    for rel in expected:
        assert (ws / rel).is_dir(), rel
    assert (ws / "plays/push/.gitkeep").is_file()
    assert (ws / "snippets/platform/ios-xe/.gitkeep").is_file()


def test_workspace_yml_and_files(tmp_path: Path) -> None:
    init_workspace(tmp_path, name="demo")
    data = yaml.safe_load((tmp_path / "workspace.yml").read_text(encoding="utf-8"))
    ws = WorkspaceFile.model_validate(data)
    assert ws.name == "demo"
    assert ws.defaults.platform == Platform.IOS_XE
    assert ws.defaults.batch_size == 5
    assert yaml.safe_load((tmp_path / "inventory/global.yml").read_text(encoding="utf-8")) == {}
    ignore = (tmp_path / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert {"*.cfg", "*.log", "*.db"} <= set(ignore)
    assert (tmp_path / ".schema" / "play.schema.json").is_file()
    assert (tmp_path / ".vscode" / "settings.json").is_file()


def test_odd_name_round_trips(tmp_path: Path) -> None:
    init_workspace(tmp_path, name='Acme "East": #1')
    data = yaml.safe_load((tmp_path / "workspace.yml").read_text(encoding="utf-8"))
    assert WorkspaceFile.model_validate(data).name == 'Acme "East": #1'


@pytest.mark.parametrize("name", ["", "   ", "a\nb", "a\x00b"])
def test_bad_name_rejected(tmp_path: Path, name: str) -> None:
    with pytest.raises(TsunamiError):
        init_workspace(tmp_path / "ws", name=name)
    assert not (tmp_path / "ws").exists()


def test_second_init_raises(tmp_path: Path) -> None:
    init_workspace(tmp_path, name="demo")
    with pytest.raises(TsunamiError):
        init_workspace(tmp_path, name="demo")


def test_existing_files_untouched(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("hello", encoding="utf-8")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".gitignore").write_text("mine\n", encoding="utf-8")
    (tmp_path / "plays" / "push").mkdir(parents=True)
    (tmp_path / "plays" / "push" / "keep.yml").write_text("x", encoding="utf-8")
    init_workspace(tmp_path, name="demo")
    assert (tmp_path / "README.md").read_text(encoding="utf-8") == "hello"
    assert (tmp_path / ".gitignore").read_text(encoding="utf-8") == "mine\n"
    assert (tmp_path / "plays/push/keep.yml").read_text(encoding="utf-8") == "x"
    assert (tmp_path / ".git").is_dir()


def test_cli_init_in_process(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ws = tmp_path / "my-ws"
    assert cli.main(["init", str(ws)]) == 0
    assert (ws / "workspace.yml").is_file()
    assert "my-ws" in yaml.safe_load((ws / "workspace.yml").read_text(encoding="utf-8"))["name"]
    assert capsys.readouterr().out.strip()
    assert cli.main(["init", str(ws)]) == 1
    assert "already" in capsys.readouterr().err


def test_cli_init_name_option(tmp_path: Path) -> None:
    assert cli.main(["init", str(tmp_path), "--name", "Lab"]) == 0
    data = yaml.safe_load((tmp_path / "workspace.yml").read_text(encoding="utf-8"))
    assert data["name"] == "Lab"


def test_cli_init_subprocess(tmp_path: Path) -> None:
    ws = tmp_path / "sub"
    cmd = [sys.executable, "-m", "tsunami", "init", str(ws)]
    done = subprocess.run(cmd, capture_output=True, text=True, check=False)
    assert done.returncode == 0, done.stderr
    assert (ws / "workspace.yml").is_file()
    again = subprocess.run(cmd, capture_output=True, text=True, check=False)
    assert again.returncode == 1
