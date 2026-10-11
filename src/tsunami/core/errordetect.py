"""Config-mode error detection from output text, per platform family.

See design.md, Execution engine: Error detection, and Platform drivers: Errors.
The AOS-CX and AOS-S patterns follow docs/research/aruba-cli.md section 2; `% Unknown
command` and `% Ambiguous command` on AOS-CX are UNVERIFIED there (covered by `% `).
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from tsunami.core.models.common import PLATFORM_FAMILY, Platform, PlatformFamily

# Always errors on IOS, even when a benign pattern also matches.
_IOS_ALWAYS = (
    "% Invalid input detected",
    "% Incomplete command",
    "% Ambiguous command",
    "% Unknown command",
)

_CX_MARKERS = r"(?:% |Invalid input:|Error:|Cannot execute command|.*doesn't exist)"
_S_MARKERS = r"(?:Invalid input:|Incomplete input:|Module not present for port or invalid port:)"


def _family(family: PlatformFamily | str) -> PlatformFamily:
    try:
        return PlatformFamily(family)
    except ValueError:
        return PLATFORM_FAMILY[Platform(family)]


def _benign_guard(benign: Sequence[str]) -> str:
    """Lookahead (placed after leading whitespace) that rejects whole benign lines."""
    if not benign:
        return ""
    alt = "|".join(f"(?:{b})" for b in benign)
    return rf"(?!(?:{alt})[ \t]*\r?$)"


def error_pattern(family: PlatformFamily | str, benign: Sequence[str] = ()) -> str:
    """One regex for Netmiko's error_pattern (searched with re.M over the output).

    Benign entries are regexes matched against the whole line, starting at its first
    non-space character.
    """
    fam = _family(family)
    guard = _benign_guard(benign)
    if fam is PlatformFamily.IOS:
        always = "|".join(re.escape(m) for m in _IOS_ALWAYS)
        return rf"^[ \t]*(?:(?:{always})|{guard}%(?=[ \t\w]))"
    markers = _CX_MARKERS if fam is PlatformFamily.AOS_CX else _S_MARKERS
    return rf"^[ \t]*{guard}{markers}"


def find_error(output: str, family: PlatformFamily | str, benign: Sequence[str] = ()) -> str | None:
    """The first output line that is a config error, or None."""
    rx = re.compile(error_pattern(family, benign), re.M)
    for line in output.splitlines():
        if rx.search(line):
            return line.strip()
    return None
