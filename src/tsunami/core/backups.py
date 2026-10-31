"""Save, browse and compare a device's config backups, always through the redactor.

A device's running config is saved, unredacted, to
<backups_dir>/<customer>/<device>/<UTC stamp>_<tag>.cfg. The pipeline saves one before
every change, tagged with the job id (DevicePipeline.backup). Discovery and the drift
check save through save_if_changed, tagged `discovery` or `drift`: a device's first
backup, then a new one only when its config differs from the newest backup.

Those files hold every password, SNMP community and key in clear text, so nothing here
returns their raw text: a single backup and both sides of a diff go through the caller's
Redactor first, and the diff is computed on the redacted text so a secret cannot appear in
it. Customer and device must be slugs and every path must stay under backups_dir.

See design.md, Backups; Secrets and credentials: Redactor.
"""

from __future__ import annotations

import difflib
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path

from tsunami.core.configtree import is_header_line
from tsunami.core.issues import TsunamiError
from tsunami.core.models.common import PlatformFamily, require_slug
from tsunami.core.paths import AppPaths
from tsunami.core.platforms import get_driver
from tsunami.core.redact import Redactor

_NAME_RE = re.compile(r"^(\d{8}T\d{6}Z)_([A-Za-z0-9][A-Za-z0-9._-]*)\.cfg$")
_STAMP_FORMAT = "%Y%m%dT%H%M%SZ"
MAX_BACKUP_BYTES = 8 * 1024 * 1024  # a running config is far smaller; refuse the absurd
_TAG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
DISCOVERY_TAG = "discovery"
DRIFT_TAG = "drift"
# Backups that no job took, and how the backups view names them.
RUN_TAGS = {DISCOVERY_TAG: "Discovery", DRIFT_TAG: "Drift check"}
# Lines the device rewrites on its own, ignored when comparing. Comments, blank lines and
# the headers above the config are ignored too.
_VOLATILE = {
    PlatformFamily.IOS: re.compile(r"^ntp clock-period\b"),
}


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


def write_backup(
    paths: AppPaths, customer: str, device: str, running: str, tag: str, *, keep: int
) -> Path:
    """Save running config, unredacted, as the device's newest backup, then keep only the
    newest `keep` backups."""
    if not _TAG_RE.match(tag):
        raise BackupError(f"not a backup tag: {tag!r}")
    folder = backup_folder(paths, customer, device)
    folder.mkdir(parents=True, exist_ok=True)
    taken = datetime.now(UTC)
    path = folder / f"{taken.strftime(_STAMP_FORMAT)}_{tag}.cfg"
    while path.exists():  # two backups in one second: never overwrite, take the next
        taken += timedelta(seconds=1)
        path = folder / f"{taken.strftime(_STAMP_FORMAT)}_{tag}.cfg"
    path.write_text(running, encoding="utf-8", newline="\n")
    prune(folder, keep)
    return path


def prune(folder: Path, keep: int) -> None:
    """Delete all but the newest `keep` backups in a device's folder."""
    backups = sorted(p for p in folder.glob("*.cfg") if parse_name(p.name) is not None)
    for old in backups[: max(0, len(backups) - keep)]:
        old.unlink(missing_ok=True)


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


class Change(StrEnum):
    FIRST = "first backup"
    CHANGED = "changed"
    SECRETS = "secrets changed"  # only secret values differ; the redacted diff is empty
    UNCHANGED = "no change"


@dataclass(frozen=True)
class Saved:
    """What save_if_changed did."""

    change: Change
    path: Path | None  # the new backup; None when nothing changed
    previous: BackupEntry | None  # the newest backup before this one
    diff: str = ""  # redacted, previous to now; "" unless CHANGED
    added: int = 0
    removed: int = 0

    @property
    def summary(self) -> str:
        if self.change is Change.CHANGED:
            return f"changed: +{self.added} -{self.removed}"
        return str(self.change)


def comparable_lines(lines: Sequence[str], family: PlatformFamily) -> list[bool]:
    """For each line, whether it counts when comparing two configs: not blank, not a
    comment, not a header the device prints, not a line the device rewrites on its own."""
    driver = get_driver(family)
    volatile = _VOLATILE.get(family)
    keep = []
    for raw in lines:
        stripped = raw.strip()
        keep.append(
            bool(stripped)
            and not driver.is_comment(raw)
            and not is_header_line(stripped, family)
            and not (volatile is not None and volatile.search(stripped))
        )
    return keep


def _kept(lines: Sequence[str], keep: Sequence[bool]) -> list[str]:
    return [line.rstrip() for line, k in zip(lines, keep, strict=True) if k]


def save_if_changed(
    paths: AppPaths,
    customer: str,
    device: str,
    running: str,
    family: PlatformFamily,
    redactor: Redactor,
    *,
    tag: str,
    keep: int,
) -> Saved:
    """Save the device's running config if it has no backup yet or differs from its newest
    backup, and say what changed.

    Comments, headers and lines the device rewrites on its own are ignored. The diff is
    computed on redacted text; a change to secret values alone is SECRETS, with no diff.
    The raw text of either config never leaves this function.
    """
    if not isinstance(redactor, Redactor):
        raise BackupError("a redactor is required to compare backups")
    entries = list_backups(paths, customer, device)
    if not entries:
        path = write_backup(paths, customer, device, running, tag, keep=keep)
        return Saved(Change.FIRST, path, None)
    previous = entries[0]
    resolved = _checked(previous.path, Path(paths.backups_dir))
    try:
        if resolved.stat().st_size > MAX_BACKUP_BYTES:
            raise BackupError("the newest backup is too large to compare")
        old_text = resolved.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise BackupError(f"cannot read the backup: {exc.strerror or 'I/O error'}") from None
    old_lines = old_text.replace("\r\n", "\n").split("\n")
    new_lines = running.replace("\r\n", "\n").split("\n")
    old_keep = comparable_lines(old_lines, family)
    new_keep = comparable_lines(new_lines, family)
    if _kept(old_lines, old_keep) == _kept(new_lines, new_keep):
        return Saved(Change.UNCHANGED, None, previous)
    old_red = _kept(redactor.lines(old_lines), old_keep)
    new_red = _kept(redactor.lines(new_lines), new_keep)
    path = write_backup(paths, customer, device, running, tag, keep=keep)
    out: list[str] = []
    added = removed = 0
    for line in difflib.unified_diff(
        old_red, new_red, fromfile=previous.path.name, tofile=path.name, lineterm=""
    ):
        if line.startswith(("--- ", "+++ ", "@@")):
            out.append(line)
            continue
        added += line[0] == "+"
        removed += line[0] == "-"
        out.append(f"{line[0]} {line[1:]}")
    if not added and not removed:
        return Saved(Change.SECRETS, path, previous)
    return Saved(Change.CHANGED, path, previous, "\n".join(out), added, removed)
