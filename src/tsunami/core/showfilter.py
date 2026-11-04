"""Output modifiers (| include, exclude, begin, section, count) applied locally.

MCP show commands never send a pipe to the device. The device runs the base command,
the whole output is redacted, and the modifier runs here on the redacted text. A
device-side filter would see secrets before redaction, and whether a line came back
would reveal them one guess at a time (design.md, MCP interface: Leak paths).

Patterns are regular expressions without repeated groups, (?...) constructs,
backreferences or counted repetition, so matching time stays bounded on the MCP server.
A '|' in the pattern is alternation: only the first '|' of a command is the pipe.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from tsunami.core.issues import TsunamiError

MAX_PATTERN = 200
MAX_QUANTIFIERS = 4


class FilterError(TsunamiError):
    """The pipe modifier or its pattern is not supported."""


@dataclass(frozen=True)
class ShowCommand:
    base: str  # what the device runs
    modifier: str | None = None  # include, exclude, begin, section, count
    pattern: str | None = None


def split_command(normalized: str) -> ShowCommand:
    """Split a policy-normalized command ('show x | include y') into its parts."""
    base, sep, rest = normalized.partition("|")
    if not sep:
        return ShowCommand(base.strip())
    words = rest.split(None, 1)
    if not words:
        raise FilterError("a pipe needs a modifier")
    pattern = words[1].strip() if len(words) > 1 else None
    return ShowCommand(base.strip(), words[0].lower(), pattern or None)


def compile_pattern(pattern: str) -> re.Pattern[str]:
    if len(pattern) > MAX_PATTERN:
        raise FilterError(f"a pipe pattern is limited to {MAX_PATTERN} characters")
    quantifiers = _check_shape(pattern)
    if quantifiers > MAX_QUANTIFIERS:
        raise FilterError(f"a pipe pattern may use at most {MAX_QUANTIFIERS} * + ? operators")
    try:
        return re.compile(pattern)
    except re.error as exc:
        raise FilterError(f"invalid pipe pattern: {exc.msg}") from None


def _check_shape(pattern: str) -> int:
    """The number of * + ? operators, after refusing what can make matching slow.

    Plain groups such as (1|2|10) are fine. A group with a quantifier after it, {n,m},
    backreferences and (?...) constructs are refused: with them a short pattern can
    backtrack for a very long time.
    """
    quantifiers = 0
    i, n = 0, len(pattern)
    while i < n:
        c = pattern[i]
        if c == "\\":
            if i + 1 < n and pattern[i + 1].isdigit():
                raise FilterError("pipe patterns may not use backreferences")
            i += 2
            continue
        if c == "[":  # a character class: everything up to its ']' is literal
            j = i + 1
            if j < n and pattern[j] == "^":
                j += 1
            if j < n and pattern[j] == "]":
                j += 1
            while j < n and pattern[j] != "]":
                j += 2 if pattern[j] == "\\" else 1
            i = j + 1
            if i < n and pattern[i] in "*+?":
                quantifiers += 1
                i += 1
            continue
        if c in "{}":
            raise FilterError("pipe patterns may not use {n,m} repetition")
        if c == "(" and i + 1 < n and pattern[i + 1] == "?":
            raise FilterError("pipe patterns may not use (?...) groups")
        if c == ")" and i + 1 < n and pattern[i + 1] in "*+?":
            raise FilterError("pipe patterns may not repeat a group: (a+)+ can take forever")
        if c in "*+?":
            quantifiers += 1
        i += 1
    return quantifiers


def apply_filter(output: str, command: ShowCommand) -> str:
    """The output with the command's modifier applied, as the device would."""
    if command.modifier is None:
        return output
    lines = output.split("\n")
    if command.modifier == "count":
        regex = compile_pattern(command.pattern) if command.pattern else None
        hits = [line for line in lines if line.strip() and (regex is None or regex.search(line))]
        return f"Count: {len(hits)}\n"
    if command.pattern is None:
        raise FilterError(f"'| {command.modifier}' needs a pattern")
    regex = compile_pattern(command.pattern)
    if command.modifier == "include":
        kept = [line for line in lines if regex.search(line)]
    elif command.modifier == "exclude":
        kept = [line for line in lines if not regex.search(line)]
    elif command.modifier == "begin":
        start = next((i for i, line in enumerate(lines) if regex.search(line)), len(lines))
        kept = lines[start:]
    elif command.modifier == "section":
        kept = _sections(lines, regex)
    else:
        raise FilterError(f"pipe modifier '{command.modifier}' is not supported")
    return "\n".join(kept) + ("\n" if kept else "")


def _sections(lines: list[str], regex: re.Pattern[str]) -> list[str]:
    """IOS '| section': each matching line with the lines indented beneath it."""
    kept: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if not regex.search(line):
            i += 1
            continue
        depth = len(line) - len(line.lstrip())
        kept.append(line)
        i += 1
        while (
            i < len(lines) and lines[i].strip() and len(lines[i]) - len(lines[i].lstrip()) > depth
        ):
            kept.append(lines[i])
            i += 1
    return kept
