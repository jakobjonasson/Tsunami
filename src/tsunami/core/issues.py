"""Validation findings and the base exception (design.md, Play schema: Cross-field validation).

Every problem shown to a person carries the file and line it came from, so the GUI,
the dev CLI and the MCP validate tool can all point at the exact spot.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class Severity(StrEnum):
    ERROR = "error"
    WARNING = "warning"


@dataclass(frozen=True)
class Issue:
    severity: Severity
    code: str  # stable machine-readable id, e.g. "play.targets.empty"
    message: str
    file: Path | None = None
    line: int | None = None  # 1-based

    def __str__(self) -> str:
        where = ""
        if self.file is not None:
            where = f"{self.file}:{self.line}: " if self.line else f"{self.file}: "
        return f"{where}{self.severity}: {self.message} [{self.code}]"


class TsunamiError(Exception):
    """An error reported to a person. The message must already be redacted."""
