"""Drafts: listing, diff, highlights, and path-safe promote and delete."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from tsunami.core.drafts import delete, highlights, list_drafts, promote, unified_diff
from tsunami.core.issues import TsunamiError

PLAY = "name: Reload\nsteps:\n  - exec: reload\n  - config:\n      - vlan 30\n"
SNIPPET = "line 1\nkey {{ secret('acme/x') }}\nother {{ secret('acme/y') }}\n"


@pytest.fixture
def root(tmp_path) -> Path:
    for rel, text in (
        ("drafts/plays/push/reload.yml", PLAY),
        ("drafts/snippets/acme/aaa.j2", SNIPPET),
        ("snippets/acme/aaa.j2", "line 1\n"),
        ("plays/.keep", ""),
    ):
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return tmp_path


def test_list_diff_and_highlights(root):
    drafts = list_drafts(root)
    assert [(d.kind, d.path.as_posix(), d.live_exists) for d in drafts] == [
        ("play", "drafts/plays/push/reload.yml", False),
        ("snippet", "drafts/snippets/acme/aaa.j2", True),
    ]
    assert drafts[1].live_path.as_posix() == "snippets/acme/aaa.j2"
    new_play = unified_diff(root, drafts[0])
    assert any(line.startswith("+name: Reload") for line in new_play)
    diff = unified_diff(root, drafts[1])
    assert any(line.startswith("+key {{ secret") for line in diff)
    assert not any(line.startswith("-line 1") for line in diff)
    assert [h[:2] for h in highlights(SNIPPET)] == [(2, "secret"), (3, "secret")]
    assert (3, "destructive", "reload") in highlights(PLAY)
    assert highlights("vlan 30\n") == []


def test_promote_new_then_existing(root):
    live = promote(root, Path("drafts/plays/push/reload.yml"))
    assert live == (root / "plays/push/reload.yml").resolve()
    assert live.read_text(encoding="utf-8") == PLAY
    assert not (root / "drafts/plays/push/reload.yml").exists()
    with pytest.raises(TsunamiError, match="already exists"):
        promote(root, Path("drafts/snippets/acme/aaa.j2"))
    promote(root, Path("drafts/snippets/acme/aaa.j2"), overwrite=True)
    assert "secret" in (root / "snippets/acme/aaa.j2").read_text(encoding="utf-8")


def test_delete(root):
    delete(root, Path("drafts/plays/push/reload.yml"))
    assert not (root / "drafts/plays/push/reload.yml").exists()
    with pytest.raises(TsunamiError):
        delete(root, Path("plays/.keep"))


def test_path_escapes_refused(root, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside") / "x.yml"
    outside.write_text("name: x\n", encoding="utf-8")
    (root / "drafts/plays/bad.txt").write_text("x", encoding="utf-8")
    for bad in (
        Path("drafts/plays/../../plays/.keep"),
        Path("drafts/plays/../../../x.yml"),
        outside,
        Path("plays/.keep"),  # a live file, not a draft
        Path("drafts/plays/bad.txt"),  # wrong suffix
        Path("drafts/plays"),
    ):
        with pytest.raises(TsunamiError):
            promote(root, bad, overwrite=True)
        with pytest.raises(TsunamiError):
            delete(root, bad)
    assert outside.exists()
    assert (root / "plays/.keep").exists()


def _link_dir(link: Path, target: Path) -> bool:
    """A directory symlink, or an NTFS junction when symlinks need privileges."""
    try:
        os.symlink(target, link, target_is_directory=True)
        return True
    except OSError:
        pass
    if os.name != "nt":
        return False
    result = subprocess.run(  # noqa: S603
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],  # noqa: S607
        capture_output=True,
        check=False,
    )
    return result.returncode == 0


def test_symlink_escape_refused(root, tmp_path_factory):
    outside_dir = tmp_path_factory.mktemp("outside")
    secret_file = outside_dir / "stolen.yml"
    secret_file.write_text("name: stolen\n", encoding="utf-8")
    try:
        os.symlink(secret_file, root / "drafts/plays/link.yml")
    except OSError:
        link_file = False
    else:
        link_file = True
    if not _link_dir(root / "plays/escape", outside_dir):
        pytest.skip("cannot create symlinks or junctions")
    if link_file:
        with pytest.raises(TsunamiError):
            promote(root, Path("drafts/plays/link.yml"))
        with pytest.raises(TsunamiError):
            delete(root, Path("drafts/plays/link.yml"))
    # a draft whose live directory is a link out of the workspace
    (root / "drafts/plays/escape").mkdir()
    (root / "drafts/plays/escape/ok.yml").write_text("name: ok\n", encoding="utf-8")
    with pytest.raises(TsunamiError):
        promote(root, Path("drafts/plays/escape/ok.yml"))
    assert secret_file.exists()
    assert not (outside_dir / "ok.yml").exists()
