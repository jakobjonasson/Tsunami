"""A customer's or a site's devices as a CSV report: inventory, login and jump host names,
what the devices last reported, their last job and contact, and their backups.

Nothing secret goes in: logins and jump hosts appear by name and address only, and job
statuses come from history.db. Model and version are what the devices reported, so they
are untrusted; a cell that a spreadsheet would read as a formula is written as text.

See design.md, Backups: Backup status and CSV export.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from tsunami.core.backups import backup_summaries, taken_by
from tsunami.core.issues import TsunamiError
from tsunami.core.paths import AppPaths

COLUMNS = (
    "customer",
    "site",
    "device",
    "type",
    "platform",
    "model",
    "version",
    "serial",
    "host",
    "port",
    "login",
    "jump_host",
    "last_job",
    "last_job_status",
    "last_job_at",
    "last_contact",
    "backups",
    "newest_backup_at",
    "newest_backup_by",
)
_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


@dataclass(frozen=True)
class ReportScope:
    customer: str
    site: str | None = None  # None: every site of the customer


def _when(value: datetime | None) -> str:
    """UTC, to the second, in a form spreadsheets sort and parse."""
    return value.strftime("%Y-%m-%d %H:%M:%SZ") if value else ""


def _safe(value: Any) -> str:
    """The value as CSV text that a spreadsheet will not run as a formula."""
    text = "" if value is None else str(value)
    return "'" + text if text.startswith(_FORMULA_START) else text


def _job_name(play_path: str) -> str:
    return Path(play_path.replace("\\", "/")).stem or play_path


def report_rows(workspace: Any, history: Any, paths: AppPaths, scope: ReportScope) -> list[dict]:
    """One row per device of the scope, sorted by site then name. Values are plain text."""
    inv = workspace.customers.get(scope.customer) if workspace is not None else None
    if inv is None:
        raise TsunamiError(f"unknown customer {scope.customer!r}")
    if scope.site is not None and scope.site not in inv.sites:
        raise TsunamiError(f"{scope.customer} has no site {scope.site!r}")
    devices = sorted(
        (d for d in inv.devices if scope.site is None or d.site == scope.site),
        key=lambda d: (d.site, d.name),
    )
    facts = history.device_facts(scope.customer)
    activity = history.device_activity(scope.customer)
    contact = history.last_contact(scope.customer)
    backups = backup_summaries(paths, scope.customer, [d.name for d in devices])
    rows = []
    for dev in devices:
        fact = facts.get(dev.name)
        job = activity.get(dev.name)
        summary = backups[dev.name]
        jump = inv.jump_for(dev)
        rows.append(
            {
                "customer": scope.customer,
                "site": dev.site,
                "device": dev.name,
                "type": dev.role,
                "platform": str(dev.platform),
                "model": dev.model or (fact.model if fact and fact.model else ""),
                "version": fact.version if fact and fact.version else "",
                "serial": fact.serial if fact and fact.serial else "",
                "host": str(dev.host),
                "port": dev.port,
                "login": inv.credential_for(dev),
                "jump_host": f"{jump.host}:{jump.port}" if jump is not None else "",
                "last_job": _job_name(job.play_path) if job else "",
                "last_job_status": str(job.status) if job else "",
                "last_job_at": _when(job.at) if job else "",
                "last_contact": _when(contact.get(dev.name)),
                "backups": summary.count,
                "newest_backup_at": _when(summary.newest.taken_at) if summary.newest else "",
                "newest_backup_by": taken_by(summary.newest) if summary.newest else "",
            }
        )
    return rows


def to_csv(rows: list[dict]) -> str:
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=COLUMNS, lineterminator="\r\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({k: _safe(row.get(k)) for k in COLUMNS})
    return out.getvalue()


def write_report(
    path: Path, workspace: Any, history: Any, paths: AppPaths, scope: ReportScope
) -> int:
    """Write the scope's report to path, UTF-8 with a BOM so Excel reads it as UTF-8.
    Returns the number of devices written."""
    rows = report_rows(workspace, history, paths, scope)
    Path(path).write_text(to_csv(rows), encoding="utf-8-sig", newline="")
    return len(rows)
