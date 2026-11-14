"""Save, browse and compare a device's config backups, always through the redactor.

A device's running config is saved, unredacted, to
<backups_dir>/<customer>/<device>/<UTC stamp>_<tag>.cfg. The pipeline saves one before
every change, tagged with the job id (DevicePipeline.backup). Discovery and the drift
check save through save_if_changed, tagged `discovery` or `drift`: a device's first
backup, then a new one only when its config differs from the newest backup.

Those files hold every password, SNMP community and key in clear text, so nothing here
returns their raw text except read_for_restore, whose one caller sends it back to a device
(core/restore.py). A single backup and both sides of a diff go through the caller's
Redactor first, and the diff is computed on the redacted text so a secret cannot appear in
it. Customer and device must be slugs and every path must stay under backups_dir.

To open a backup in VS Code, write_redacted_copy writes its redacted text to
<backup_views_dir>/<customer>/<device>/<stem>.redacted.cfg; the raw file is never opened.
Copies older than a day are deleted the next time one is written.

See design.md, Backups; Secrets and credentials: Redactor.
"""

from __future__ import annotations

import difflib
import re
from collections.abc import Iterable, Sequence
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
VIEW_COPY_MAX_AGE = timedelta(days=1)
VIEW_COPY_HEADER = (
    "! Redacted copy of Tsunami backup {name}, made for viewing.\n"
    "! Secret values are masked: do not restore a device from this file.\n"
)
MAX_BACKUP_BYTES = 8 * 1024 * 1024  # a running config is far smaller; refuse the absurd
_TAG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
DISCOVERY_TAG = "discovery"
DRIFT_TAG = "drift"
PRE_RESTORE_TAG = "pre-restore"  # what the device had before a restore
RESTORE_TAG = "restore"  # what the device had after a restore
# Backups that no job took, and how the backups view names them.
RUN_TAGS = {
    DISCOVERY_TAG: "Discovery",
    DRIFT_TAG: "Drift check",
    PRE_RESTORE_TAG: "Before restore",
    RESTORE_TAG: "Restore",
}
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
    root = _resolved(paths.backups_dir)
    folder = _resolved(root / customer / device)
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


@dataclass(frozen=True)
class BackupSummary:
    """How many backups a device has, and its newest (None without any)."""

    count: int
    newest: BackupEntry | None


def backup_summaries(
    paths: AppPaths, customer: str, devices: Iterable[str]
) -> dict[str, BackupSummary]:
    """Each device's backup count and newest backup. A name that cannot have backups (not
    a slug) counts as having none."""
    out: dict[str, BackupSummary] = {}
    for name in devices:
        try:
            entries = list_backups(paths, customer, name)
        except BackupError:
            entries = []
        out[name] = BackupSummary(len(entries), entries[0] if entries else None)
    return out


def taken_by(entry: BackupEntry) -> str:
    """Who took a backup, as the backups views name it: Discovery, Drift check, or a job."""
    return RUN_TAGS.get(entry.job_id) or f"job {entry.job_id[:8]}"


def _resolved(path: Path | str) -> Path:
    """path.resolve() without Windows' extended-length prefix. While another thread creates
    folders on the way, resolve() can return \\\\?\\C:\\... for a path it otherwise returns
    as C:\\..., and is_relative_to then treats the two as unrelated."""
    text = str(Path(path).resolve())
    if text.startswith("\\\\?\\UNC\\"):
        text = "\\\\" + text[8:]
    elif text.startswith("\\\\?\\"):
        text = text[4:]
    return Path(text)


def _checked(path: Path | str, root: Path | None) -> Path:
    path = Path(path)
    if parse_name(path.name) is None:
        raise BackupError("not a Tsunami backup file")
    resolved = _resolved(path)
    if root is not None and not resolved.is_relative_to(_resolved(root)):
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


def read_for_restore(paths: AppPaths, customer: str, device: str, path: Path | str) -> str:
    """The raw text of one of this device's backups, to send back to a device.

    The only function that returns a backup unredacted. Its one caller is core/restore.py,
    which sends the lines to the device and redacts anything it logs or stores."""
    source = _checked(path, backup_folder(paths, customer, device))
    try:
        if source.stat().st_size > MAX_BACKUP_BYTES:
            raise BackupError("the backup file is too large to restore")
        return source.read_text(encoding="utf-8", errors="replace").replace("\r\n", "\n")
    except OSError as exc:
        raise BackupError(f"cannot read the backup: {exc.strerror or 'I/O error'}") from None


def redacted_diff(
    old_text: str,
    new_text: str,
    family: PlatformFamily,
    redactor: Redactor,
    *,
    fromfile: str = "",
    tofile: str = "",
) -> tuple[str, int, int]:
    """The redacted diff of two configs, ignoring what comparable_lines ignores, as
    (diff, added, removed). Body lines are written "+ line" as in diff_redacted."""
    old_lines = old_text.replace("\r\n", "\n").split("\n")
    new_lines = new_text.replace("\r\n", "\n").split("\n")
    old_red = _kept(redactor.lines(old_lines), comparable_lines(old_lines, family))
    new_red = _kept(redactor.lines(new_lines), comparable_lines(new_lines, family))
    out: list[str] = []
    added = removed = 0
    for line in difflib.unified_diff(
        old_red, new_red, fromfile=fromfile, tofile=tofile, lineterm=""
    ):
        if line.startswith(("--- ", "+++ ", "@@")):
            out.append(line)
            continue
        added += line[0] == "+"
        removed += line[0] == "-"
        out.append(f"{line[0]} {line[1:]}")
    return "\n".join(out), added, removed


def write_redacted_copy(
    paths: AppPaths,
    customer: str,
    device: str,
    path: Path | str,
    redactor: Redactor,
    *,
    now: datetime | None = None,
) -> Path:
    """Write the backup's redacted text, under a two-line header, to the backup views
    folder and return the copy's path. The path must be one of this device's backups.
    Copies older than VIEW_COPY_MAX_AGE are deleted first."""
    source = _checked(path, backup_folder(paths, customer, device))
    text = read_redacted(source, redactor, root=paths.backups_dir)
    views = paths.backup_views_dir
    prune_view_copies(views, now=now)
    folder = views / customer / device
    try:
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / f"{source.stem}.redacted.cfg"
        target.write_text(
            VIEW_COPY_HEADER.format(name=source.name) + text, encoding="utf-8", newline="\n"
        )
    except OSError as exc:
        raise BackupError(f"cannot write the copy: {exc.strerror or 'I/O error'}") from None
    return target


def prune_view_copies(views: Path, *, now: datetime | None = None) -> None:
    """Delete redacted copies older than VIEW_COPY_MAX_AGE. Best effort: a copy VS Code
    still holds open is left for next time."""
    cutoff = (now or datetime.now(UTC)) - VIEW_COPY_MAX_AGE
    if not views.is_dir():
        return
    for copy in views.rglob("*.redacted.cfg"):
        try:
            if datetime.fromtimestamp(copy.stat().st_mtime, UTC) < cutoff:
                copy.unlink()
        except OSError:
            continue


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
