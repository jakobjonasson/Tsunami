"""Browse and compare a device's config backups, always through the redactor.

Before every change the pipeline saves the device's running config, unredacted, to
<backups_dir>/<customer>/<device>/<UTC stamp>_<job id>.cfg (DevicePipeline.backup).
Those files hold every password, SNMP community and key in clear text, so nothing here
returns their raw text: a single backup and both sides of a diff go through the caller's
Redactor first, and the diff is computed on the redacted text so a secret cannot appear in
it. Customer and device must be slugs and every path must stay under backups_dir.

See design.md, Backups; Secrets and credentials: Redactor.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from tsunami.core.issues import TsunamiError
from tsunami.core.models.common import require_slug
from tsunami.core.paths import AppPaths
from tsunami.core.redact import Redactor

_NAME_RE = re.compile(r"^(\d{8}T\d{6}Z)_([A-Za-z0-9][A-Za-z0-9._-]*)\.cfg$")
_STAMP_FORMAT = "%Y%m%dT%H%M%SZ"
MAX_BACKUP_BYTES = 8 * 1024 * 1024  # a running config is far smaller; refuse the absurd


class BackupError(TsunamiError):
    """A backup path was refused, or a backup cannot be read."""


@dataclass(frozen=True)
class BackupEntry:
    path: Path
    taken_at: datetime  # UTC
    job_id: str
    size: int  # bytes


def backup_folder(paths: AppPaths, customer: str, device: str) -> Path:
    """The folder holding a device's backups, or BackupError for a bad name or escape."""
    try:
        require_slug(customer)
        require_slug(device)
    except TsunamiError as exc:
        raise BackupError(str(exc)) from None
    root = Path(paths.backups_dir).resolve()
    folder = (root / customer / device).resolve()
    if not folder.is_relative_to(root) or folder == root:
        raise BackupError("the backup folder resolves outside the backups folder")
    return folder


def parse_name(name: str) -> tuple[datetime, str] | None:
    """(taken_at, job id) from a backup file name, or None if it is not a backup."""
    match = _NAME_RE.fullmatch(name)
    if match is None:
        return None
    try:
        taken = datetime.strptime(match.group(1), _STAMP_FORMAT).replace(tzinfo=UTC)
    except ValueError:
        return None
    return taken, match.group(2)


def list_backups(paths: AppPaths, customer: str, device: str) -> list[BackupEntry]:
    """The device's backups, newest first. Files with other names are ignored."""
    folder = backup_folder(paths, customer, device)
    if not folder.is_dir():
        return []
    entries = []
    for child in folder.iterdir():
        parsed = parse_name(child.name)
        if parsed is None or child.is_symlink() or not child.is_file():
            continue
        try:
            size = child.stat().st_size
        except OSError:
            continue
        entries.append(BackupEntry(child, parsed[0], parsed[1], size))
    entries.sort(key=lambda e: (e.taken_at, e.path.name), reverse=True)
    return entries


def _checked(path: Path | str, root: Path | None) -> Path:
    path = Path(path)
    if parse_name(path.name) is None:
        raise BackupError("not a Tsunami backup file")
    resolved = path.resolve()
    if root is not None and not resolved.is_relative_to(Path(root).resolve()):
        raise BackupError("the backup is outside the backups folder")
    if resolved.is_symlink() or not resolved.is_file():
        raise BackupError("the backup file does not exist")
    return resolved


def read_redacted(path: Path | str, redactor: Redactor, *, root: Path | None = None) -> str:
    """The backup's text with every secret masked. Pass root (the backups folder) to refuse
    a path outside it. The raw text never leaves this function."""
    if not isinstance(redactor, Redactor):
        raise BackupError("a redactor is required to read a backup")
    resolved = _checked(path, root)
    try:
        if resolved.stat().st_size > MAX_BACKUP_BYTES:
            raise BackupError("the backup file is too large to show")
        raw = resolved.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise BackupError(f"cannot read the backup: {exc.strerror or 'I/O error'}") from None
    return redactor.text(raw.replace("\r\n", "\n"))


def diff_redacted(
    old_path: Path | str,
    new_path: Path | str,
    redactor: Redactor,
    *,
    root: Path | None = None,
) -> str:
    """A unified diff of the two backups' redacted text, old to new ("" if identical).

    Both sides are redacted before diffing. Body lines are written "+ line", "- line" and
    "  line" (marker and a space) so the GUI diff view colors them, as in job diffs. Secrets
    are masked on both sides, so a rotated password does not show as a difference.
    """
    old = read_redacted(old_path, redactor, root=root).split("\n")
    new = read_redacted(new_path, redactor, root=root).split("\n")
    out: list[str] = []
    for line in difflib.unified_diff(
        old, new, fromfile=Path(old_path).name, tofile=Path(new_path).name, lineterm=""
    ):
        if line.startswith(("--- ", "+++ ", "@@")):
            out.append(line)
        else:
            out.append(f"{line[0]} {line[1:]}")
    return "\n".join(out)
