"""settings.json: machine settings edited in the GUI Settings view.

Lives in %APPDATA%\\Tsunami, never in the workspace, because extra sink patterns widen
where secret() may appear and Claude can write to the workspace.

See design.md, GUI: Settings; Snippets and rendering: secret(); Execution engine:
Error detection.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Annotated, Literal

from pydantic import AfterValidator, Field, ValidationError

from tsunami.core.issues import TsunamiError
from tsunami.core.models.common import PlatformFamily, StrictModel
from tsunami.core.platforms.base import LineRule


def _sink_pattern(value: str) -> str:
    try:
        compiled = re.compile(value)
    except re.error as exc:
        raise ValueError(f"invalid regular expression: {exc.msg}") from None
    if "secret" not in compiled.groupindex:
        raise ValueError("a sink pattern needs a (?P<secret>...) group marking the secret token")
    return value


def _regex(value: str) -> str:
    try:
        re.compile(value)
    except re.error as exc:
        raise ValueError(f"invalid regular expression: {exc.msg}") from None
    return value


SinkPattern = Annotated[str, AfterValidator(_sink_pattern)]
Regex = Annotated[str, AfterValidator(_regex)]


class AppSettings(StrictModel):
    workspace: str | None = None
    # Extra lines where secret() may appear; the redactor masks them too.
    extra_sink_patterns: dict[PlatformFamily, list[SinkPattern]] = {}
    # Device messages that look like errors but are informational.
    benign_messages: dict[PlatformFamily, list[Regex]] = {}
    backup_retention: int = Field(30, ge=1, le=1000)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    theme: Literal["system", "light", "dark"] = "system"
    # Offer SHA-1 SSH algorithms after every modern one, for devices with nothing newer.
    ssh_legacy_algorithms: bool = True

    def extra_rules(self, family: PlatformFamily | None = None) -> tuple[LineRule, ...]:
        """Extra sink patterns as LineRules, for one family or all of them."""
        families = [family] if family is not None else list(self.extra_sink_patterns)
        return tuple(
            LineRule(re.compile(pattern))
            for f in families
            for pattern in self.extra_sink_patterns.get(f, [])
        )


def load_settings(path: Path) -> AppSettings:
    """Settings from path; defaults if the file does not exist."""
    if not path.is_file():
        return AppSettings()
    try:
        return AppSettings.model_validate(json.loads(path.read_text(encoding="utf-8-sig")))
    except (ValueError, ValidationError) as exc:
        raise TsunamiError(f"{path.name} is invalid: {exc}") from None


def save_settings(settings: AppSettings, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(settings.model_dump_json(indent=2) + "\n")
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
