"""YAML loading with a line map for file:line errors, and round-trip writes.

Loading converts ruamel's round-trip nodes to plain Python for Pydantic, and records
the line of every mapping key and sequence item so a validation error can point at
the exact spot. Round-trip writes keep comments, key order and indentation for the
few places the app writes YAML (add to inventory, draft promotion).

See design.md, Technology stack; Play schema: Cross-field validation.
"""

from __future__ import annotations

import os
import re
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq
from ruamel.yaml.constructor import DuplicateKeyError
from ruamel.yaml.error import MarkedYAMLError, YAMLError
from ruamel.yaml.util import load_yaml_guess_indent

from tsunami.core.issues import Issue, Severity, TsunamiError

Loc = tuple[str | int, ...]

_DUPLICATE_KEY_RE = re.compile(r'found duplicate key "([^"]*)"')


@dataclass(frozen=True)
class YamlDoc:
    path: Path
    data: Any
    lines: dict[Loc, int]  # 1-based line of each mapping key and sequence item

    def line_for(self, loc: Iterable[str | int]) -> int:
        """Line of the deepest part of loc present in the document.

        Parts not in the document are skipped: Pydantic adds union tags to error
        locations, and a missing field has no line of its own, so it falls back to
        its parent's line.
        """
        line = 1
        current: Loc = ()
        for part in loc:
            candidate = (*current, part)
            if candidate in self.lines:
                current = candidate
                line = self.lines[candidate]
        return line

    def display_loc(self, loc: Iterable[str | int]) -> str:
        """loc as people read it (`steps[0].snippet`), without Pydantic's union tags."""
        parts = list(loc)
        shown: list[str | int] = []
        current: Loc = ()
        for i, part in enumerate(parts):
            candidate = (*current, part)
            if candidate in self.lines or i == len(parts) - 1:
                shown.append(part)
                current = candidate
        text = ""
        for part in shown:
            if isinstance(part, int):
                text += f"[{part}]"
            else:
                text += f".{part}" if text else str(part)
        return text


class YamlLoadError(TsunamiError):
    """The file is not valid YAML. Carries an Issue with file and line."""

    def __init__(self, issue: Issue) -> None:
        super().__init__(str(issue))
        self.issue = issue


def load_yaml(path: Path) -> YamlDoc:
    # utf-8-sig: PowerShell and Notepad may write a byte-order mark.
    return load_yaml_text(Path(path).read_text(encoding="utf-8-sig"), path)


def load_yaml_text(text: str, path: Path) -> YamlDoc:
    """Parse YAML text. path is used only in messages (the MCP validate tool has no file)."""
    path = Path(path)
    try:
        node = YAML(typ="rt").load(text)
    except DuplicateKeyError as exc:
        # The parser's message quotes both values; report only the key name.
        match = _DUPLICATE_KEY_RE.search(exc.problem or "")
        key = f" {match.group(1)!r}" if match else ""
        issue = _issue("yaml.duplicate_key", f"duplicate key{key}", path, exc)
        raise YamlLoadError(issue) from None
    except MarkedYAMLError as exc:
        message = exc.problem or exc.context or "invalid YAML"
        raise YamlLoadError(_issue("yaml.syntax", message, path, exc)) from None
    except YAMLError as exc:
        raise YamlLoadError(Issue(Severity.ERROR, "yaml.syntax", str(exc), path, None)) from None
    lines: dict[Loc, int] = {}
    data = _plain(node, (), lines)
    return YamlDoc(path=path, data=data, lines=lines)


def _issue(code: str, message: str, path: Path, exc: MarkedYAMLError) -> Issue:
    mark = exc.problem_mark or exc.context_mark
    line = mark.line + 1 if mark is not None else None
    return Issue(Severity.ERROR, code, message, path, line)


def _plain(node: Any, loc: Loc, lines: dict[Loc, int]) -> Any:
    if isinstance(node, CommentedMap):
        out: dict[Any, Any] = {}
        for key, value in node.items():
            plain_key = _plain_scalar(key)
            child = (*loc, plain_key)
            try:
                lines[child] = node.lc.key(key)[0] + 1
            except (KeyError, TypeError):  # keys pulled in by a YAML merge (<<) have no line
                pass
            out[plain_key] = _plain(value, child, lines)
        return out
    if isinstance(node, CommentedSeq):
        items = []
        for index, value in enumerate(node):
            child = (*loc, index)
            try:
                lines[child] = node.lc.item(index)[0] + 1
            except (KeyError, TypeError):
                pass
            items.append(_plain(value, child, lines))
        return items
    return _plain_scalar(node)


def _plain_scalar(value: Any) -> Any:
    # ruamel returns subclasses (ScalarFloat, ScalarString, ScalarBoolean, ...) that
    # carry formatting. bool is checked before int because ScalarBoolean is an int.
    if value is None or type(value) in (bool, int, float, str):
        return value
    if type(value).__name__ == "ScalarBoolean":
        return bool(value)
    for plain in (int, float, str):
        if isinstance(value, plain):
            return plain(value)
    return value  # dates and timestamps stay as they are


def validation_issues(
    exc: ValidationError, doc: YamlDoc, *, code: str, prefix: Loc = ()
) -> list[Issue]:
    """Turn a Pydantic ValidationError into Issues with file and line.

    prefix locates the validated object inside the document (e.g. one item of a list
    validated on its own). Input values are never included in messages, so a secret
    pasted into the wrong field is not echoed back.
    """
    issues = []
    for error in exc.errors(include_url=False, include_input=False, include_context=False):
        loc = (*prefix, *error["loc"])
        message = error["msg"].removeprefix("Value error, ")
        where = doc.display_loc(loc)
        text = f"{where}: {message}" if where else message
        issues.append(
            Issue(Severity.ERROR, f"{code}.{error['type']}", text, doc.path, doc.line_for(loc))
        )
    return issues


@dataclass
class RoundTripDoc:
    """A YAML file loaded for editing, with its indentation so writes match the original."""

    path: Path
    data: Any
    mapping_indent: int
    sequence_indent: int
    sequence_offset: int


def load_roundtrip(path: Path) -> RoundTripDoc:
    path = Path(path)
    text = path.read_text(encoding="utf-8-sig")
    try:
        data, indent, offset = load_yaml_guess_indent(text)
    except MarkedYAMLError as exc:
        message = exc.problem or "invalid YAML"
        raise YamlLoadError(_issue("yaml.syntax", message, path, exc)) from None
    # ruamel's guess is the sequence indent (dash to content). In the usual
    # `key:\n  - item` style the mapping indent equals the dash offset; a top-level
    # list (`- name: x`) has offset 0 and maps at the sequence indent.
    indent = indent or 2
    offset = offset or 0
    return RoundTripDoc(
        path=path,
        data=data,
        mapping_indent=offset or indent,
        sequence_indent=indent,
        sequence_offset=offset,
    )


def dump_roundtrip(doc: RoundTripDoc) -> None:
    """Write doc back atomically, keeping comments and the original indentation."""
    yaml = YAML(typ="rt")
    yaml.preserve_quotes = True
    yaml.width = 4096  # never re-wrap long lines
    yaml.indent(
        mapping=doc.mapping_indent, sequence=doc.sequence_indent, offset=doc.sequence_offset
    )
    # ruamel writes None as an empty value; the workspace convention is explicit null.
    yaml.representer.add_representer(
        type(None), lambda rep, _: rep.represent_scalar("tag:yaml.org,2002:null", "null")
    )
    fd, tmp = tempfile.mkstemp(prefix=f".{doc.path.name}.", dir=doc.path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            yaml.dump(doc.data, handle)
        os.replace(tmp, doc.path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
