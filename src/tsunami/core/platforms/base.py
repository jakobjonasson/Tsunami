"""PlatformDriver: the data and behavior that differ between platform families.

Phase 2 holds the rendering side: secret patterns, guarded commands, comment syntax and
interface names. Phase 3 adds the run side (sessions, facts, errors, rollback, day-0).

Secret patterns do two jobs. The redactor masks every `secret` span they find, and the
sink rule allows secret() in a template only inside a span from a rule marked `sink`.
So every secret a template places is, by construction, one the redactor will mask.
Catch-all rules (any token after `password`) redact but never authorize a sink.

See design.md, Platform drivers.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from tsunami.core.models.common import PlatformFamily
from tsunami.core.platforms.interfaces import validate_interface_name

# A secret token: a double-quoted string (AOS-S) or one bare word.
VALUE = r'(?P<secret>"[^"\n]*"|\S+)'
# An optional IOS encryption-type digit before the token (0, 5, 6, 7, 8, 9).
TYPE = r"(?:\d{1,2}\s+)?"
# The word `key` on its own, not the end of `pre-shared-key` or `key-string`.
KEY = r"(?<![\w-])key"
# Not an AOS-CX encoding keyword: IOS rules must not take `plaintext` as the value.
NOT_FORM = r"(?!(?:plaintext|ciphertext)\s)"


@dataclass(frozen=True)
class LineRule:
    """Secret tokens on one kind of line.

    `line` selects the line; every match of `token` on it has its `secret` group masked.
    Without `token`, `line` carries the `secret` group itself. `skip_under` names a
    top-level parent line under which the rule does not apply (key chain key ids).
    """

    line: re.Pattern[str]
    token: re.Pattern[str] | None = None
    sink: bool = True
    skip_under: re.Pattern[str] | None = None

    def secret_spans(self, text: str, parent: str | None = None) -> list[tuple[int, int]]:
        if self.skip_under is not None and parent and self.skip_under.search(parent):
            return []
        if not self.line.search(text):
            return []
        pattern = self.token or self.line
        return [m.span("secret") for m in pattern.finditer(text) if m.group("secret")]


@dataclass(frozen=True)
class BlockRule:
    """Every line strictly between a start line and an end line is secret (certificates)."""

    start: re.Pattern[str]
    end: re.Pattern[str]


def rule(
    line: str,
    token: str | None = None,
    *,
    sink: bool = True,
    skip_under: str | None = None,
    ignore_case: bool = True,
) -> LineRule:
    # Devices print lowercase keywords, but matching ignores case as defense in depth.
    # Catch-alls keep case, so free text ("description Secret room") is left alone.
    flags = re.IGNORECASE if ignore_case else 0
    return LineRule(
        re.compile(line, flags),
        re.compile(token, flags) if token else None,
        sink,
        re.compile(skip_under, flags) if skip_under else None,
    )


def top_level_parent(line: str, parent: str | None) -> str | None:
    """The parent to use for the next line: the latest line with no indentation."""
    return line if line and not line[0].isspace() else parent


@dataclass(frozen=True)
class PlatformDriver:
    family: PlatformFamily
    secret_rules: tuple[LineRule, ...]
    secret_blocks: tuple[BlockRule, ...] = ()
    guarded_exec: tuple[str, ...] = ()
    guarded_config: tuple[str, ...] = ()
    comment_prefixes: tuple[str, ...] = ("!",)

    def validate_interface(self, name: str) -> str:
        return validate_interface_name(self.family, name)

    def guarded_exec_command(self, command: str) -> str | None:
        """The guarded command this exec line starts with, if any."""
        return _starts_with_any(command, self.guarded_exec)

    def guarded_config_line(self, line: str) -> str | None:
        """The guarded command this config line starts with, if any."""
        return _starts_with_any(line, self.guarded_config)

    def is_comment(self, line: str) -> bool:
        return line.lstrip().startswith(self.comment_prefixes)

    def sink_spans(self, line: str, parent: str | None = None) -> list[tuple[int, int]]:
        return [span for r in self.secret_rules if r.sink for span in r.secret_spans(line, parent)]


def _starts_with_any(text: str, commands: tuple[str, ...]) -> str | None:
    words = " ".join(text.split()).lower()
    for command in commands:
        if words == command or words.startswith(command + " "):
            return command
    return None
