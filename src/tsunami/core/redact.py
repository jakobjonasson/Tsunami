"""The redactor: pattern layer and exact-value layer, applied to every outbound path.

Pattern layer: on every known secret-bearing line, the secret token is replaced with
<redacted> and the rest of the line kept, so diffs still line up; certificate blocks
are masked line by line. Every platform family's rules apply to all text, so output
from a mislabelled device is still covered and both sides of a diff are treated alike.

Exact-value layer: each known secret value of 4 or more characters is replaced wherever
it appears. Patterns miss things; exact matching catches plaintext copies anywhere.

Every result shown to people, stored in history.db, written to logs or returned over
MCP passes through here. See design.md, Secrets and credentials: Redactor.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

from tsunami.core.platforms import all_drivers
from tsunami.core.platforms.base import LineRule, top_level_parent

REDACTED = "<redacted>"
MIN_EXACT_LENGTH = 4
_WORD_SPLIT = re.compile(r"[\s'\"`,\[\]{}()]+")


def supplied_values(value: Any) -> set[str]:
    """Every string in value, whole and split into words: text an MCP caller supplied.

    MCP redaction leaves these out of the exact-value layer where they are echoed back,
    because redacting the echo of a correct guess would confirm it (design.md, Redaction).
    """
    out: set[str] = set()
    if isinstance(value, str):
        out |= {value, value.strip(), *(w for w in _WORD_SPLIT.split(value) if w)}
    elif isinstance(value, dict):
        for key, item in value.items():
            out |= supplied_values(key) | supplied_values(item)
    elif isinstance(value, list | tuple | set | frozenset):
        for item in value:
            out |= supplied_values(item)
    elif value is not None and not isinstance(value, bool):
        out.add(str(value))
    return out


class Redactor:
    def __init__(
        self, exact_values: Iterable[str] = (), *, extra_rules: Iterable[LineRule] = ()
    ) -> None:
        drivers = all_drivers()
        extra_rules = tuple(extra_rules)
        self._extra_count = len(extra_rules)
        self._rules = (*(r for d in drivers for r in d.secret_rules), *extra_rules)
        self._blocks = tuple(b for d in drivers for b in d.secret_blocks)
        values = {v for v in exact_values if isinstance(v, str) and len(v) >= MIN_EXACT_LENGTH}
        values |= {v.replace("\n", "\r\n") for v in values if "\n" in v}  # CRLF device output
        # Longest first, so a value containing a shorter one is replaced whole.
        self._exact = sorted(
            values,
            key=len,
            reverse=True,
        )

    def __repr__(self) -> str:
        return f"Redactor({len(self._exact)} exact values)"

    def with_values(self, values: Iterable[str]) -> Redactor:
        """This redactor plus more exact values (the logins a run uses)."""
        extra = self._rules[len(self._rules) - self._extra_count :] if self._extra_count else ()
        return Redactor([*self._exact, *values], extra_rules=extra)

    def line(self, line: str) -> str:
        """Pattern layer for one line (no block state), then the exact-value layer."""
        return self._exact_layer(self._pattern_line(line))

    def lines(self, lines: Iterable[str]) -> list[str]:
        out = []
        in_block = None
        parent = None  # the latest unindented line: some rules depend on it
        for line in lines:
            if in_block is not None:
                if in_block.end.search(line):
                    in_block = None
                    out.append(self._pattern_line(line, parent))
                else:
                    indent = line[: len(line) - len(line.lstrip())]
                    out.append(indent + REDACTED)
                continue
            in_block = next((b for b in self._blocks if b.start.search(line)), None)
            out.append(self._pattern_line(line, parent))
            parent = top_level_parent(line, parent)
        return [self._exact_layer(line) for line in out]

    def text(self, text: str) -> str:
        if not text:
            return text
        return self._exact_layer("\n".join(self.lines(text.split("\n"))))

    def _pattern_line(self, line: str, parent: str | None = None) -> str:
        spans = sorted(span for rule in self._rules for span in rule.secret_spans(line, parent))
        if not spans:
            return line
        merged: list[list[int]] = []
        for start, end in spans:
            if merged and start <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], end)
            else:
                merged.append([start, end])
        for start, end in reversed(merged):
            line = line[:start] + REDACTED + line[end:]
        return line

    def _exact_layer(self, text: str) -> str:
        for value in self._exact:
            if value in text:
                text = text.replace(value, REDACTED)
        return text


def redact_exception(exc: BaseException, redactor: Redactor | None = None) -> str:
    """Exception text safe to log or show: Netmiko exceptions can include sent commands."""
    return (redactor or Redactor()).text(f"{type(exc).__name__}: {exc}")
