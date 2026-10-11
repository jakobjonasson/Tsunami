"""MCP path checks (design.md, Testing: path validation)."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from tsunami.core.pathguard import PathError, resolve_draft_write, resolve_read


@pytest.fixture
def root(tmp_path) -> Path:
    for folder in ("plays/push", "snippets/common/ios", "drafts/plays/push", "inventory"):
        (tmp_path / folder).mkdir(parents=True)
    (tmp_path / "plays/push/add-vlan.yml").write_text("name: x\n", encoding="utf-8")
    (tmp_path / "inventory/global.yml").write_text("domain: x\n", encoding="utf-8")
    (tmp_path.parent / "outside.yml").write_text("secret\n", encoding="utf-8")
    return tmp_path


def test_reads_inside_plays_and_snippets(root):
    assert (
        resolve_read(root, "plays/push/add-vlan.yml")
        == (root / "plays/push/add-vlan.yml").resolve()
    )
    assert resolve_read(root, "plays\\push\\add-vlan.yml").name == "add-vlan.yml"


@pytest.mark.parametrize(
    "path",
    [
        "inventory/global.yml",  # not a readable tree
        "../outside.yml",
        "plays/../../outside.yml",
        "plays/./push/add-vlan.yml",
        "plays\\.\\push\\add-vlan.yml",
        "plays\\push\\..\\push\\add-vlan.yml",
        "C:/Windows/win.ini",
        "C:plays/push/add-vlan.yml",
        "\\\\server\\share\\x.yml",
        "/plays/push/add-vlan.yml",
        "plays/push/add-vlan.yml:hidden",  # NTFS alternate data stream
        "plays/push/add-vlan.yml\x00.txt",
        "plays/push/add\nvlan.yml",
        "",
        "plays",
    ],
)
def test_reads_refused(root, path):
    with pytest.raises(PathError):
        resolve_read(root, path)


@pytest.mark.parametrize(
    "path",
    [
        "drafts/plays/push/new-vlan.yml",
        "drafts/plays/initial/day0.yml",
        "drafts/snippets/common/ios/syslog.j2",
        "drafts/snippets/platform/ios-xe/syslog.j2",
        "drafts/snippets/customers/acme/aos-cx/radius.j2",
    ],
)
def test_draft_writes_allowed(root, path):
    assert resolve_draft_write(root, path).is_relative_to((root / "drafts").resolve())


@pytest.mark.parametrize(
    "path",
    [
        "plays/push/new-vlan.yml",  # live tree
        "drafts/plays/push/new-vlan.yaml",
        "drafts/plays/other/x.yml",
        "drafts/plays/x.yml",
        "drafts/plays/push/Bad_Name.yml",
        "drafts/snippets/common/nxos/x.j2",
        "drafts/snippets/common/ios/x.yml",
        "drafts/snippets/customers/Acme/ios/x.j2",
        "drafts/snippets/x.j2",
        "drafts/../plays/push/x.yml",
        "drafts/plays/push/x.yml:ads",
        "inventory/global.yml",
        "drafts/policy.yml",
    ],
)
def test_draft_writes_refused(root, path):
    with pytest.raises(PathError):
        resolve_draft_write(root, path)


def _link(link: Path, target: Path) -> bool:
    """A directory junction (Windows) or symlink; False if the OS refuses."""
    try:
        if os.name == "nt":
            result = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True
            )
            return result.returncode == 0
        link.symlink_to(target, target_is_directory=True)
        return True
    except OSError:
        return False


def test_junction_out_of_the_workspace_is_refused(root, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside")
    (outside / "stolen.yml").write_text("secret\n", encoding="utf-8")
    if not _link(root / "plays" / "escape", outside):
        pytest.skip("cannot create a junction or symlink here")
    with pytest.raises(PathError):
        resolve_read(root, "plays/escape/stolen.yml")


def test_junction_out_of_drafts_is_refused_for_writes(root):
    live = root / "plays" / "push"
    target = root / "drafts" / "plays" / "baseline"
    if not _link(target, live):
        pytest.skip("cannot create a junction or symlink here")
    with pytest.raises(PathError, match="outside drafts"):
        resolve_draft_write(root, "drafts/plays/baseline/x.yml")
