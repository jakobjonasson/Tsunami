"""Workspace path checks for MCP tools: real-path containment, drafts-only writes.

Paths from Claude are untrusted. They must be relative, contain no drive, UNC prefix,
alternate data stream (`:`), `..` escape or control character, and must still be inside
the permitted folder after resolving symlinks and NTFS junctions. Writes go only to the
draft tree, in the same layout as the live tree, with the right extension.

See design.md, MCP interface: Enforcement order (step 4), Leak paths.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path, PureWindowsPath

from tsunami.core.issues import TsunamiError
from tsunami.core.models.common import SLUG_PATTERN, Platform, PlatformFamily

READABLE = ("plays", "snippets", "drafts/plays", "drafts/snippets")
MAX_DRAFT_BYTES = 256 * 1024

_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f  ]")
_SLUG_RE = re.compile(SLUG_PATTERN)
_PLAY_TYPES = ("initial", "push", "baseline")


class PathError(TsunamiError):
    """A path from an MCP client was refused."""


def _clean_relative(relative: str) -> PureWindowsPath:
    if not isinstance(relative, str) or not relative.strip():
        raise PathError("a workspace-relative path is required")
    if _CONTROL_RE.search(relative) or ":" in relative:
        raise PathError("the path contains a forbidden character")
    pure = PureWindowsPath(relative.replace("/", "\\"))
    if pure.is_absolute() or pure.drive or pure.root or relative.startswith(("\\", "/")):
        raise PathError("the path must be relative to the workspace")
    # Check the raw text: PureWindowsPath silently drops "." segments.
    if any(part in ("..", ".") for part in re.split(r"[\\/]+", relative)):
        raise PathError("the path must not contain '..' or '.' segments")
    return pure


def resolve_read(root: Path, relative: str, *, allowed: Sequence[str] = READABLE) -> Path:
    """The real path of a file Claude may read, or PathError."""
    pure = _clean_relative(relative)
    root_real = Path(root).resolve()
    candidate = (root_real / Path(*pure.parts)).resolve()
    for base in allowed:
        base_real = (root_real / base).resolve()
        if candidate.is_relative_to(base_real) and candidate != base_real:
            return candidate
    raise PathError(f"reads are limited to {', '.join(f'{b}/' for b in allowed)}")


def resolve_draft_write(root: Path, relative: str) -> Path:
    """The real path a draft may be written to, or PathError.

    drafts/plays/<initial|push|baseline>/<slug>.yml
    drafts/snippets/common/<family>/<slug>.j2
    drafts/snippets/platform/<platform>/<slug>.j2
    drafts/snippets/customers/<customer>/<family>/<slug>.j2
    """
    pure = _clean_relative(relative)
    parts = list(pure.parts)  # case-sensitive: slugs and folder names are lowercase
    families = {str(f) for f in PlatformFamily}
    platforms = {str(p) for p in Platform}
    stem, suffix = Path(parts[-1]).stem, Path(parts[-1]).suffix
    valid = parts[:1] == ["drafts"] and _SLUG_RE.fullmatch(stem) is not None and (
        (parts[1:2] == ["plays"] and len(parts) == 4 and parts[2] in _PLAY_TYPES
         and suffix == ".yml")
        or (parts[1:2] == ["snippets"] and suffix == ".j2" and (
            (len(parts) == 5 and parts[2] == "common" and parts[3] in families)
            or (len(parts) == 5 and parts[2] == "platform" and parts[3] in platforms)
            or (len(parts) == 6 and parts[2] == "customers"
                and _SLUG_RE.fullmatch(parts[3]) is not None and parts[4] in families)
        ))
    )  # fmt: skip
    if not valid:
        raise PathError(
            "drafts go to drafts/plays/<type>/<name>.yml or drafts/snippets/... mirroring the "
            "live layout, with slug names"
        )
    root_real = Path(root).resolve()
    candidate = (root_real / Path(*pure.parts)).resolve()
    drafts_real = (root_real / "drafts").resolve()
    if not candidate.is_relative_to(drafts_real):
        raise PathError("the draft path resolves outside drafts/")
    return candidate
