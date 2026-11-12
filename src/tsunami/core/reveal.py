"""Press-to-reveal in the GUI: one masked value at a time, in the run preview and the
backups viewer, after the Tsunami password is asked for again.

The value is read when it is revealed: in a run preview from the secret store (or, for a
password prompt, from what the person typed in that wizard), in the backups viewer from
the backup file on disk. It is never taken from a stored render. It goes only to the
widget that shows it, and nothing here logs, stores or returns it anywhere else: MCP,
the job record and the logs never see it, so the redactor's guarantee for everything
Claude sees is unchanged.

Every reveal is audited in history.db (`reveals`) before the value is read: who, when,
which secret or backup token, and the preview or backup it was viewed in, with the line
as shown (masked). A reveal whose audit row cannot be written shows nothing.

See design.md, GUI: Revealing a masked value.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tsunami.core.backups import BackupError, _checked
from tsunami.core.issues import TsunamiError
from tsunami.core.render.secret_refs import (
    LOGIN_PREFIX,
    PROMPT_PREFIX,
    describe_ref,
    split_ref,
)

PREVIEW, BACKUP = "preview", "backup"
REDACTED = "<redacted>"


@dataclass(frozen=True)
class MaskSpan:
    """One masked value in a view's text: where it is, and what it stands for."""

    start: int  # offsets in the view's text
    end: int
    ref: str  # a render reference (preview), or "line:token" (backup), 0-based
    line: int  # 0-based line of the text
    item: str  # how the audit and the tooltip name it


def _line_of(text: str, offset: int) -> int:
    return text.count("\n", 0, offset)


def line_text(text: str, line: int) -> str:
    lines = text.split("\n")
    return lines[line] if 0 <= line < len(lines) else ""


# -- run preview ---------------------------------------------------------------------


def preview_spans(text: str, spans: list[tuple[int, int, str]]) -> list[MaskSpan]:
    """MaskSpans for Rendering.masked_text_spans()."""
    return [MaskSpan(a, b, ref, _line_of(text, a), describe_ref(ref)) for a, b, ref in spans]


def preview_value(secrets: Any, ref: str, typed: Mapping[str, Any]) -> str:
    """A preview value, read now: from the store, or a password prompt's typed answer."""
    if ref.startswith(PROMPT_PREFIX):
        value = typed.get(ref[len(PROMPT_PREFIX) :])
        if not value:
            raise TsunamiError("nothing was typed for this prompt")
        return str(value)
    if ref.startswith(LOGIN_PREFIX):
        name, field = ref[len(LOGIN_PREFIX) :].split("/", 1)
        value = getattr(secrets.get_login(name), field)
        if not value:
            raise TsunamiError(f"login {name!r} has no {field} value")
        return str(value)
    customer, name, field = split_ref(ref)
    if field is None:
        return secrets.get_secret(customer, name)
    values = secrets.get_secret_fields(customer, name).values
    if field not in values:
        raise TsunamiError(f"{name!r} has no field {field!r}")
    return values[field]


# -- backups --------------------------------------------------------------------------


def backup_spans(text: str) -> list[MaskSpan]:
    """A MaskSpan for each <redacted> token in a redacted backup's text."""
    spans: list[MaskSpan] = []
    lines = text.split("\n")
    offset = 0
    for number, line in enumerate(lines):
        for token, match in enumerate(re.finditer(re.escape(REDACTED), line)):
            spans.append(
                MaskSpan(
                    offset + match.start(),
                    offset + match.end(),
                    f"{number}:{token}",
                    number,
                    f"line {number + 1}, token {token + 1}",
                )
            )
        offset += len(line) + 1
    return spans


def backup_value(path: Path | str, root: Path | None, shown_line: str, ref: str) -> str:
    """The raw text behind one <redacted> token of a backup line, read from the file now.

    The redacted line's text around the tokens must match the raw line exactly, so a
    token is only ever matched to the value at its own place on its own line."""
    number, token = (int(part) for part in ref.split(":", 1))
    resolved = _checked(path, root)
    try:
        raw_lines = resolved.read_text(encoding="utf-8", errors="replace").split("\n")
    except OSError as exc:
        raise BackupError(f"cannot read the backup: {exc.strerror or 'I/O error'}") from None
    if number >= len(raw_lines):
        raise TsunamiError("this line is not in the backup file")
    raw = raw_lines[number].rstrip("\r")
    pieces = shown_line.split(REDACTED)
    if token >= len(pieces) - 1:
        raise TsunamiError("this token is not on the line")
    pattern = "(.*?)".join(re.escape(p) for p in pieces)
    match = re.fullmatch(pattern, raw, flags=re.DOTALL)
    if match is None:
        raise TsunamiError("this line of the backup cannot be matched to the file")
    return match.group(token + 1)


# -- audit ----------------------------------------------------------------------------


def audit(
    history: Any,
    *,
    revealed_by: str,
    kind: str,
    customer: str,
    device: str | None,
    span: MaskSpan,
    context: str,
    shown_text: str,
) -> None:
    """Record a reveal before its value is read. The masked line is all that is kept."""
    if history is None:
        raise TsunamiError("revealing needs history.db, to record who revealed what")
    try:
        history.add_reveal(
            revealed_by=revealed_by,
            kind=kind,
            customer=customer,
            device=device,
            item=span.item,
            context=context,
            masked_line=line_text(shown_text, span.line),
        )
    except Exception as exc:  # noqa: BLE001 - no audit row, no reveal
        raise TsunamiError(
            f"the reveal could not be recorded, so nothing is shown ({exc})"
        ) from None
