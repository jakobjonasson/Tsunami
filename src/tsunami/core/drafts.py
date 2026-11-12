"""Draft listing, diff against the live file, and promotion.

Drafts live under drafts/plays and drafts/snippets and mirror the live tree. A person
promotes a draft by moving it to its live path. Every move or delete resolves real
paths first (following symlinks and junctions) and refuses anything that would read
or write outside the permitted directories.

See design.md, GUI: Drafts; Workspace layout.
"""

from __future__ import annotations

import difflib
import hashlib
import os
import re
import time
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


def draft_digest(root: Path, draft_path: Path) -> str:
    """sha256 of the draft as it is now, so a promotion can refuse content nobody saw."""
    source, _, _ = _resolve_plan(root, draft_path)
    try:
        return hashlib.sha256(source.read_bytes()).hexdigest()
    except OSError as exc:
        raise TsunamiError(f"cannot read the draft {source.name}: {exc.strerror}") from None


def live_exists(root: Path, draft_path: Path) -> bool:
    """Whether the draft's live file exists now (not when the draft was listed)."""
    _, target, _ = _resolve_plan(root, draft_path)
    return target.exists()


def promote(
    root: Path,
    draft_path: Path,
    *,
    overwrite: bool = False,
    expected_digest: str | None = None,
    attempts: int = 10,
) -> Path:
    """Move the draft to its live path. Returns the live path.

    expected_digest: the draft as the person reviewed it; a draft rewritten since (Claude
    can write drafts at any time) is refused. On Windows a live file another program has
    open (an editor, the search indexer, antivirus) refuses the move for a moment, so the
    move is retried briefly before it fails with the reason.
    """
    source, target, _ = _resolve_plan(root, draft_path)
    if not source.is_file():
        raise TsunamiError(f"draft not found: {Path(draft_path).name}")
    if expected_digest is not None and draft_digest(root, draft_path) != expected_digest:
        raise TsunamiError(
            f"the draft {source.name} changed since it was opened; review it again before "
            "promoting it"
        )
    if target.exists() and not overwrite:
        raise TsunamiError(f"the live file already exists: {target.name}")
    target.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(max(1, attempts)):
        try:
            os.replace(source, target)
            return target
        except PermissionError as exc:
            if attempt + 1 >= attempts:
                raise TsunamiError(
                    f"could not replace {target.name}: {exc.strerror or exc}. Another program "
                    "may have it open, or it is read-only; close it and promote again"
                ) from None
            time.sleep(0.1)
        except OSError as exc:
            raise TsunamiError(
                f"could not move the draft to {target.name}: {exc.strerror or exc}"
            ) from None
    return target  # pragma: no cover - the loop returns or raises


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
