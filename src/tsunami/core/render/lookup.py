"""Snippet lookup: drafts (draft plays only), then customer, platform, common.

Customer and common snippets live in per-family folders, so the same snippet name
resolves to each device's own CLI. Paths are workspace-relative.

See design.md, Snippets and rendering: Lookup order.
"""

from __future__ import annotations

from pathlib import Path

from tsunami.core.models.common import PLATFORM_FAMILY, Platform, require_slug


def snippet_paths(
    name: str, *, customer: str, platform: Platform, draft: bool = False
) -> list[Path]:
    """Candidate paths for a snippet, first match wins."""
    require_slug(name)
    require_slug(customer)
    family = PLATFORM_FAMILY[platform]
    file = f"{name}.j2"
    live = [
        Path("snippets", "customers", customer, family, file),
        Path("snippets", "platform", platform, file),
        Path("snippets", "common", family, file),
    ]
    if not draft:
        return live
    return [Path("drafts", *p.parts) for p in live] + live


def find_snippet(
    root: Path, name: str, *, customer: str, platform: Platform, draft: bool = False
) -> Path | None:
    """The workspace-relative path of the snippet that wins, or None."""
    for path in snippet_paths(name, customer=customer, platform=platform, draft=draft):
        if (root / path).is_file():
            return path
    return None
