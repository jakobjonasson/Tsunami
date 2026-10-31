"""Draft listing, diff against the live file, and promotion.

Drafts live under drafts/plays and drafts/snippets and mirror the live tree. A person
promotes a draft by moving it to its live path. Every move or delete resolves real
paths first (following symlinks and junctions) and refuses anything that would read
or write outside the permitted directories.

See design.md, GUI: Drafts; Workspace layout.
"""

from __future__ import annotations

import difflib
import os
import re
from dataclasses import dataclass
from pathlib import Path

from tsunami.core.issues import TsunamiError
from tsunami.core.platforms import all_drivers

_KINDS = {"plays": ("play", ".yml"), "snippets": ("snippet", ".j2")}
_SECRET_RE = re.compile(r"\b(secret|login)\s*\(")
_PREFIX_RE = re.compile(r"""^(?:-\s+)*(?:(?:exec|config)\s*:\s*)?(?:-\s+)*["']?""")


@dataclass(frozen=True)
class Draft:
    path: Path  # workspace-relative, under drafts/
    live_path: Path  # the matching path without the leading drafts/ part
    kind: str  # "play" | "snippet"
    live_exists: bool


def list_drafts(root: Path) -> list[Draft]:
    root = Path(root)
    drafts: list[Draft] = []
    for folder, (kind, suffix) in _KINDS.items():
        base = root / "drafts" / folder
        if not base.is_dir():
            continue
        for file in sorted(base.rglob(f"*{suffix}")):
            if not file.is_file():
                continue
            rel = file.relative_to(root)
            live = Path(*rel.parts[1:])
            drafts.append(Draft(rel, live, kind, (root / live).exists()))
    return sorted(drafts, key=lambda d: (d.kind, d.path.as_posix()))


def _read(path: Path) -> list[str]:
    try:
        return path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    except (OSError, UnicodeDecodeError) as exc:
        raise TsunamiError(f"cannot read {path.name}: {exc}") from exc


def unified_diff(root: Path, draft: Draft) -> list[str]:
    root = Path(root)
    new = _read(root / draft.path)
    old = _read(root / draft.live_path)
    return list(
        difflib.unified_diff(
            old,
            new,
            fromfile=f"live/{draft.live_path.as_posix()}" if old else "(new file)",
            tofile=f"draft/{draft.path.as_posix()}",
            lineterm="",
        )
    )


def highlights(text: str) -> list[tuple[int, str, str]]:
    """(1-based line, kind, detail): secret() calls and guarded (destructive) commands."""
    drivers = all_drivers()
    found: list[tuple[int, str, str]] = []
    for number, line in enumerate(text.splitlines(), start=1):
        for match in _SECRET_RE.finditer(line):
            found.append((number, "secret", f"{match.group(1)}()"))
        stripped = _PREFIX_RE.sub("", line.strip()).rstrip("'\"")
        if not stripped:
            continue
        for driver in drivers:
            hit = driver.guarded_exec_command(stripped) or driver.guarded_config_line(stripped)
            if hit:
                found.append((number, "destructive", hit))
                break
    return found


def _draft_folder(root_real: Path, source: Path) -> tuple[str, str, str, Path]:
    """(folder, kind, suffix, base) for a real path inside drafts/plays or drafts/snippets."""
    for folder, (kind, suffix) in _KINDS.items():
        base = root_real / "drafts" / folder
        if source.is_relative_to(base) and source != base:
            return folder, kind, suffix, base
    raise TsunamiError("not a draft: the file is not inside drafts/plays or drafts/snippets")


def _resolve_plan(root: Path, draft_path: Path) -> tuple[Path, Path, str]:
    """(real source, real target, kind) after every safety check."""
    root_real = Path(root).resolve()
    given = Path(draft_path)
    source = (given if given.is_absolute() else root_real / given).resolve()
    folder, kind, suffix, base = _draft_folder(root_real, source)
    if source.suffix != suffix:
        raise TsunamiError(f"a draft {kind} must be a {suffix} file")
    target = (root_real / folder / source.relative_to(base)).resolve()
    live_base = root_real / folder
    if not target.is_relative_to(live_base) or target == live_base:
        raise TsunamiError(f"the live path must be inside {folder}/")
    if target.suffix != suffix:
        raise TsunamiError(f"the live path must be a {suffix} file")
    return source, target, kind


def promote(root: Path, draft_path: Path, *, overwrite: bool = False) -> Path:
    """Move the draft to its live path. Returns the live path."""
    source, target, _ = _resolve_plan(root, draft_path)
    if not source.is_file():
        raise TsunamiError(f"draft not found: {Path(draft_path).name}")
    if target.exists() and not overwrite:
        raise TsunamiError(f"the live file already exists: {target.name}")
    target.parent.mkdir(parents=True, exist_ok=True)
    os.replace(source, target)
    return target


def delete(root: Path, draft_path: Path) -> None:
    root_real = Path(root).resolve()
    given = Path(draft_path)
    source = (given if given.is_absolute() else root_real / given).resolve()
    _, _, suffix, _ = _draft_folder(root_real, source)
    if source.suffix != suffix:
        raise TsunamiError(f"a draft must be a {suffix} file")
    if not source.is_file():
        raise TsunamiError(f"draft not found: {given.name}")
    source.unlink()
