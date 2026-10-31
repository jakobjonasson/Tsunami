"""history.db repository: jobs, devices, leases, audit, compliance (SQLite, WAL).

See design.md, Local data and audit: history.db tables, and Architecture: Process model.

Callers redact. History stores exactly what it is given and never decides what is
secret: prompts must be masked, and device text, errors and MCP arguments must already
have passed through core/redact.py.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from importlib import resources
from pathlib import Path
from typing import Any, NamedTuple

from tsunami.core.issues import TsunamiError
from tsunami.core.models.jobs import (
    TERMINAL_DEVICE_STATUSES,
    Actor,
    DeviceRecord,
    DeviceStatus,
    JobRecord,
    JobStatus,
)

BUSY_TIMEOUT_MS = 5000

# Python-facing JSON field name -> column.
_JOB_JSON = {
    "prompts": "prompts_json",
    "tags": "tags_json",
    "limit": "limit_json",
    "summary": "summary_json",
}
_JOB_COLUMNS = frozenset(
    {
        "actor",
        "play_path",
        "play_hash",
        "customer",
        "mode",
        "status",
        "approved_by",
        "approved_at",
        "started_at",
        "finished_at",
        "heartbeat_at",
        *_JOB_JSON,
    }
)
_DEVICE_COLUMNS = frozenset(
    {
        "status",
        "started_at",
        "finished_at",
        "render_hash",
        "render_masked",
        "output_redacted",
        "diff_redacted",
        "failed_line",
        "error_redacted",
        "rollback_status",
        "backup_path",
    }
)
_TERMINAL_VALUES = tuple(sorted(s.value for s in TERMINAL_DEVICE_STATUSES))

# A device in one of these states answered Tsunami: the SSH session opened, and what went
# wrong, if anything, went wrong after that. Excluded on purpose: unreachable and
# hostkey_mismatch (the connect failed), pending and connecting (not reached yet),
# device_busy, cancelled and skipped_maxfail (never connected), interrupted (a crash:
# unknown whether it got that far).
CONNECTED_DEVICE_STATUSES = frozenset(
    {
        DeviceStatus.READY,
        DeviceStatus.NO_CHANGES,
        DeviceStatus.RUNNING,
        DeviceStatus.SUCCESS,
        DeviceStatus.FAILED,
        DeviceStatus.ROLLED_BACK,
        DeviceStatus.ROLLBACK_FAILED,
        DeviceStatus.SKIPPED_PRECHECK,
        DeviceStatus.RENDER_CHANGED,
    }
)
_CONNECTED_VALUES = tuple(sorted(s.value for s in CONNECTED_DEVICE_STATUSES))


class StoredFacts(NamedTuple):
    """What a device last reported (table device_facts); None where it never said."""

    platform: str | None
    model: str | None
    version: str | None
    serial: str | None
    updated_at: datetime


class DeviceActivity(NamedTuple):
    """A device's most recent job row. `at` is when it finished, else started, else was queued."""

    job_id: str
    play_path: str
    status: DeviceStatus
    at: datetime


class DeviceJob(NamedTuple):
    """One job a device took part in, newest first in recent_jobs_for_device."""

    job_id: str
    play_path: str
    mode: str
    status: DeviceStatus
    at: datetime


# When a job_devices row happened: finished, else started, else its job was queued.
_ROW_TIME = "COALESCE(d.finished_at, d.started_at, j.created_at)"


def _dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True)


def _parse_ts(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _cell(value: Any) -> Any:
    """A Python value as a column value: enums, datetimes."""
    if isinstance(value, datetime):
        # Fixed-width microseconds keep string comparison equal to time order.
        return value.astimezone(UTC).isoformat(timespec="microseconds")
    if hasattr(value, "value"):  # enums
        return value.value
    return value


def _set_clause(
    fields: dict[str, Any], allowed: frozenset[str], json_map: dict[str, str]
) -> tuple[str, list[Any]]:
    """Validated SET clause and parameters for update_job / update_device."""
    if not fields:
        raise TsunamiError("nothing to update")
    unknown = sorted(set(fields) - allowed)
    if unknown:
        raise TsunamiError(f"unknown history field: {', '.join(unknown)}")
    cols, params = [], []
    for name, value in fields.items():
        if name in json_map:
            cols.append(f"{json_map[name]} = ?")
            params.append(_dumps(value))
        else:
            cols.append(f"{name} = ?")
            params.append(_cell(value))
    return ", ".join(cols), params


class History:
    """history.db: job queue and audit trail. Thread-safe within a process."""

    def __init__(self, db_path: Path, *, clock: Callable[[], datetime] | None = None) -> None:
        self._clock = clock or (lambda: datetime.now(UTC))
        self._lock = threading.Lock()
        db_path = Path(db_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        try:
            self._conn.execute("PRAGMA journal_mode=WAL")
            # WAL with NORMAL syncs at checkpoints, not on every commit: a run's hundreds of
            # small writes stop holding the lock through an fsync each. A crash keeps the
            # database intact; only a power loss can drop the last commits.
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._migrate()
        except BaseException:
            self._conn.close()
            raise
        # Reads get their own connection: in WAL mode they never wait for a writer, so the
        # GUI thread's queries do not queue behind a run's worker threads.
        self._read_lock = threading.Lock()
        self._reader = sqlite3.connect(db_path, check_same_thread=False, isolation_level=None)
        self._reader.row_factory = sqlite3.Row
        self._reader.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")

    def close(self) -> None:
        with self._lock, self._read_lock:
            self._conn.close()
            self._reader.close()

    # --- plumbing ---

    def _now(self) -> str:
        return _cell(self._clock())

    def _cutoff(self, seconds: float) -> str:
        return _cell(self._clock() - timedelta(seconds=seconds))

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        """Lock plus BEGIN IMMEDIATE: the write lock is taken up front, so a read-then-write
        sequence cannot be interleaved by another process."""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            self._conn.execute("COMMIT")

    def _exec(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, tuple(params))

    def _query(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        with self._read_lock:
            return self._reader.execute(sql, tuple(params)).fetchall()

    def _migrate(self) -> None:
        versions = sorted(
            (int(p.name.split("_", 1)[0]), p)
            for p in resources.files("tsunami.core.migrations").iterdir()
            if p.name.endswith(".sql")
        )
        newest = max((v for v, _ in versions), default=0)
        has_table = self._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_version'"
        ).fetchone()
        current = 0
        if has_table:
            row = self._conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
            current = row[0] or 0
        if current > newest:
            raise TsunamiError(
                f"history.db is schema version {current}, newer than supported ({newest})"
            )
        for version, path in versions:
            if version <= current:
                continue
            # executescript would commit an open transaction, so each migration carries
            # its own BEGIN/COMMIT with the version bump inside it.
            self._conn.executescript(
                "BEGIN IMMEDIATE;\n"
                + path.read_text(encoding="utf-8")
                + f"\nDELETE FROM schema_version; INSERT INTO schema_version VALUES ({version});"
                + "\nCOMMIT;"
            )

    # --- jobs ---

    def create_job(
        self,
        *,
        actor: Actor,
        play_path: str,
        play_hash: str,
        customer: str,
        mode: str,
        status: JobStatus = JobStatus.PENDING,
        prompts: dict | None = None,
        tags: list[str] | None = None,
        limit: list[str] | None = None,
    ) -> str:
        job_id = uuid.uuid4().hex
        self._exec(
            "INSERT INTO jobs (id, created_at, actor, play_path, play_hash, customer, mode, status,"
            " prompts_json, tags_json, limit_json, summary_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                job_id,
                self._now(),
                _cell(actor),
                play_path,
                play_hash,
                customer,
                mode,
                _cell(status),
                _dumps(prompts or {}),
                _dumps(tags or []),
                _dumps(limit or []),
                _dumps({}),
            ),
        )
        return job_id

    def get_job(self, job_id: str) -> JobRecord:
        rows = self._query("SELECT * FROM jobs WHERE id = ?", (job_id,))
        if not rows:
            raise TsunamiError(f"no such job: {job_id}")
        return self._job(rows[0])

    def list_jobs(
        self,
        *,
        customer: str | None = None,
        status: JobStatus | None = None,
        actor: Actor | None = None,
        limit: int = 100,
    ) -> list[JobRecord]:
        where, params = [], []
        for column, value in (("customer", customer), ("status", status), ("actor", actor)):
            if value is not None:
                where.append(f"{column} = ?")
                params.append(_cell(value))
        sql = "SELECT * FROM jobs"
        if where:
            sql += " WHERE " + " AND ".join(where)
        # rowid breaks ties between jobs created in the same microsecond.
        sql += " ORDER BY created_at DESC, rowid DESC LIMIT ?"
        return [self._job(r) for r in self._query(sql, (*params, limit))]

    def update_job(self, job_id: str, **fields: Any) -> None:
        clause, params = _set_clause(fields, _JOB_COLUMNS, _JOB_JSON)
        cur = self._exec(f"UPDATE jobs SET {clause} WHERE id = ?", (*params, job_id))
        if cur.rowcount == 0:
            raise TsunamiError(f"no such job: {job_id}")

    def set_job_status(self, job_id: str, status: JobStatus, **fields: Any) -> None:
        self.update_job(job_id, status=status, **fields)

    def approve(self, job_id: str, *, approved_by: str) -> None:
        with self._tx() as conn:
            row = conn.execute("SELECT status FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if row is None:
                raise TsunamiError(f"no such job: {job_id}")
            if row["status"] != JobStatus.AWAITING_APPROVAL:
                raise TsunamiError(
                    f"job {job_id} is {row['status']}, not {JobStatus.AWAITING_APPROVAL}"
                )
            conn.execute(
                "UPDATE jobs SET status = ?, approved_by = ?, approved_at = ? WHERE id = ?",
                (_cell(JobStatus.APPROVED), approved_by, self._now(), job_id),
            )

    def heartbeat(self, job_id: str) -> None:
        self._exec("UPDATE jobs SET heartbeat_at = ? WHERE id = ?", (self._now(), job_id))

    def mark_interrupted(self, *, stale_after: float = 60.0) -> list[str]:
        cutoff = self._cutoff(stale_after)
        now = self._now()
        with self._tx() as conn:
            ids = [
                r["id"]
                for r in conn.execute(
                    "SELECT id FROM jobs WHERE status = ?"
                    " AND COALESCE(heartbeat_at, started_at, created_at) < ?",
                    (_cell(JobStatus.RUNNING), cutoff),
                )
            ]
            marks = ",".join("?" * len(_TERMINAL_VALUES))
            for job_id in ids:
                conn.execute(
                    "UPDATE jobs SET status = ?, finished_at = ? WHERE id = ?",
                    (_cell(JobStatus.INTERRUPTED), now, job_id),
                )
                conn.execute(
                    "UPDATE job_devices SET status = ?, finished_at = ?"
                    f" WHERE job_id = ? AND status NOT IN ({marks})",
                    (_cell(DeviceStatus.INTERRUPTED), now, job_id, *_TERMINAL_VALUES),
                )
                conn.execute("DELETE FROM device_leases WHERE job_id = ?", (job_id,))
        return ids

    def expire_approvals(self, *, older_than_minutes: int) -> list[str]:
        cutoff = _cell(self._clock() - timedelta(minutes=older_than_minutes))
        with self._tx() as conn:
            ids = [
                r["id"]
                for r in conn.execute(
                    "SELECT id FROM jobs WHERE status = ? AND created_at < ?",
                    (_cell(JobStatus.AWAITING_APPROVAL), cutoff),
                )
            ]
            for job_id in ids:
                conn.execute(
                    "UPDATE jobs SET status = ?, finished_at = ? WHERE id = ?",
                    (_cell(JobStatus.EXPIRED), self._now(), job_id),
                )
                self._cancel_ready(conn, job_id)
        return ids

    def mark_ready_for_approval(
        self, job_id: str, *, ready: list[str], unchanged: list[str]
    ) -> JobStatus | None:
        """After an approval request's check-mode diff (see approvals.classify_request):
        devices that would change become ready (not success, which would read as applied),
        the rest no_changes. With nothing to change, the job ends no_changes and nobody is
        asked to approve. None if the check did not complete."""
        status = JobStatus.AWAITING_APPROVAL if ready else JobStatus.NO_CHANGES
        with self._tx() as conn:
            fields = {"status": _cell(status)}
            if status == JobStatus.NO_CHANGES:
                fields["finished_at"] = self._now()
            sets = ", ".join(f"{k} = ?" for k in fields)
            updated = conn.execute(
                f"UPDATE jobs SET {sets} WHERE id = ? AND status = ?",
                (*fields.values(), job_id, _cell(JobStatus.COMPLETED)),
            ).rowcount
            if not updated:
                return None
            for names, device_status in (
                (ready, DeviceStatus.READY),
                (unchanged, DeviceStatus.NO_CHANGES),
            ):
                for name in names:
                    conn.execute(
                        "UPDATE job_devices SET status = ? WHERE job_id = ? AND device = ?",
                        (_cell(device_status), job_id, name),
                    )
            counts = {
                str(s): n
                for s, n in (
                    (DeviceStatus.READY, len(ready)),
                    (DeviceStatus.NO_CHANGES, len(unchanged)),
                )
                if n
            }
            conn.execute(
                "UPDATE jobs SET summary_json = ? WHERE id = ?",
                (_dumps({"counts": counts}), job_id),
            )
        return status

    def cancel_ready_devices(self, job_id: str) -> None:
        """A denied, expired or cancelled request: its ready devices were never touched."""
        with self._tx() as conn:
            self._cancel_ready(conn, job_id)

    def _cancel_ready(self, conn: sqlite3.Connection, job_id: str) -> None:
        conn.execute(
            "UPDATE job_devices SET status = ?, finished_at = ? WHERE job_id = ? AND status = ?",
            (_cell(DeviceStatus.CANCELLED), self._now(), job_id, _cell(DeviceStatus.READY)),
        )

    # --- devices ---

    def add_devices(self, job_id: str, devices: Iterable[str]) -> None:
        with self._tx() as conn:
            conn.executemany(
                "INSERT INTO job_devices (job_id, device, status) VALUES (?,?,?)",
                [(job_id, d, _cell(DeviceStatus.PENDING)) for d in devices],
            )

    def update_device(self, job_id: str, device: str, **fields: Any) -> None:
        clause, params = _set_clause(fields, _DEVICE_COLUMNS, {})
        cur = self._exec(
            f"UPDATE job_devices SET {clause} WHERE job_id = ? AND device = ?",
            (*params, job_id, device),
        )
        if cur.rowcount == 0:
            raise TsunamiError(f"no such device {device!r} in job {job_id}")

    def devices(self, job_id: str) -> list[DeviceRecord]:
        rows = self._query("SELECT * FROM job_devices WHERE job_id = ? ORDER BY rowid", (job_id,))
        return [self._device(r) for r in rows]

    # --- leases ---

    def acquire_lease(
        self, customer: str, device: str, job_id: str, *, stale_after: float = 60.0
    ) -> bool:
        cutoff = self._cutoff(stale_after)
        with self._tx() as conn:
            row = conn.execute(
                "SELECT l.job_id, l.acquired_at, j.heartbeat_at, j.started_at FROM device_leases l"
                " LEFT JOIN jobs j ON j.id = l.job_id WHERE l.customer = ? AND l.device = ?",
                (customer, device),
            ).fetchone()
            if row is not None and row["job_id"] != job_id:
                # A lease lives as long as its job's heartbeat does.
                last_seen = row["heartbeat_at"] or row["started_at"] or row["acquired_at"]
                if last_seen >= cutoff:
                    return False
            conn.execute(
                "INSERT OR REPLACE INTO device_leases (customer, device, job_id, acquired_at)"
                " VALUES (?,?,?,?)",
                (customer, device, job_id, self._now()),
            )
            return True

    def release_lease(self, customer: str, device: str, job_id: str) -> None:
        self._exec(
            "DELETE FROM device_leases WHERE customer = ? AND device = ? AND job_id = ?",
            (customer, device, job_id),
        )

    def lease_holder(self, customer: str, device: str) -> str | None:
        rows = self._query(
            "SELECT job_id FROM device_leases WHERE customer = ? AND device = ?", (customer, device)
        )
        return rows[0]["job_id"] if rows else None

    # --- audit ---

    def record_mcp_call(
        self,
        *,
        tool: str,
        args_redacted: dict,
        status: str,
        duration_ms: int,
        result_bytes: int,
        job_id: str | None = None,
    ) -> None:
        self._exec(
            "INSERT INTO mcp_calls (ts, tool, args_redacted_json, status, duration_ms,"
            " result_bytes, job_id) VALUES (?,?,?,?,?,?,?)",
            (self._now(), tool, _dumps(args_redacted), status, duration_ms, result_bytes, job_id),
        )

    # --- drafts written over MCP ---

    def record_mcp_draft(self, path: str, sha256: str) -> None:
        self._exec(
            "INSERT INTO mcp_drafts (path, sha256, written_at) VALUES (?,?,?)"
            " ON CONFLICT(path) DO UPDATE SET sha256 = excluded.sha256,"
            " written_at = excluded.written_at",
            (path, sha256, self._now()),
        )

    def forget_mcp_draft(self, path: str) -> None:
        self._exec("DELETE FROM mcp_drafts WHERE path = ?", (path,))

    def record_mcp_device(self, customer: str, device: str, host: str) -> None:
        """Remember a device Claude added over MCP (see mcp_devices)."""
        self._exec(
            "INSERT INTO mcp_devices (customer, device, host, added_at) VALUES (?,?,?,?)"
            " ON CONFLICT(customer, device) DO UPDATE SET host = excluded.host,"
            " added_at = excluded.added_at",
            (customer, device, host, self._now()),
        )

    def mcp_devices(self, customer: str) -> dict[str, str]:
        """Devices Claude added to a customer over MCP: name -> the host it gave."""
        rows = self._query("SELECT device, host FROM mcp_devices WHERE customer = ?", (customer,))
        return {r["device"]: r["host"] for r in rows}

    # --- show-allowlist requests (request_allowlist_addition) ---

    def add_allowlist_request(self, family: str, command: str, reason: str) -> int:
        """Queue a request, or return the id of an identical pending one."""
        with self._tx() as conn:
            row = conn.execute(
                "SELECT id FROM allowlist_requests"
                " WHERE status='pending' AND family=? AND command=?",
                (family, command),
            ).fetchone()
            if row:
                return int(row["id"])
            cur = conn.execute(
                "INSERT INTO allowlist_requests (family, command, reason, requested_at)"
                " VALUES (?,?,?,?)",
                (family, command, reason, self._now()),
            )
            return int(cur.lastrowid or 0)

    def pending_allowlist_requests(self) -> list[dict[str, Any]]:
        rows = self._query(
            "SELECT id, family, command, reason, requested_at FROM allowlist_requests"
            " WHERE status='pending' ORDER BY id"
        )
        return [dict(r) for r in rows]

    def decide_allowlist_request(self, request_id: int, *, approved: bool, by: str) -> bool:
        """Record the decision on a pending request; False if it was not pending."""
        cur = self._exec(
            "UPDATE allowlist_requests SET status=?, decided_at=?, decided_by=?"
            " WHERE id=? AND status='pending'",
            ("approved" if approved else "denied", self._now(), by, request_id),
        )
        return cur.rowcount > 0

    def mcp_draft_hashes(self) -> dict[str, str]:
        return {r["path"]: r["sha256"] for r in self._query("SELECT path, sha256 FROM mcp_drafts")}

    def add_compliance_result(
        self, *, job_id: str, device: str, missing_count: int, extra_count: int, details: dict
    ) -> None:
        self._exec(
            "INSERT INTO compliance_results (job_id, device, missing_count, extra_count,"
            " details_json) VALUES (?,?,?,?,?)",
            (job_id, device, missing_count, extra_count, _dumps(details)),
        )

    def compliance_history(self, customer: str, *, limit: int = 50) -> list[dict]:
        rows = self._query(
            "SELECT c.job_id, c.device, c.missing_count, c.extra_count, c.details_json,"
            " j.created_at FROM compliance_results c JOIN jobs j ON j.id = c.job_id"
            " WHERE j.customer = ? ORDER BY j.created_at DESC, c.rowid DESC LIMIT ?",
            (customer, limit),
        )
        return [
            {
                "job_id": r["job_id"],
                "device": r["device"],
                "created_at": _parse_ts(r["created_at"]),
                "missing_count": r["missing_count"],
                "extra_count": r["extra_count"],
                "details": json.loads(r["details_json"]),
            }
            for r in rows
        ]

    # --- what each device reported, and what happened to it (one query per customer) ---

    def record_device_facts(
        self,
        customer: str,
        device: str,
        *,
        platform: str | None = None,
        model: str | None = None,
        version: str | None = None,
        serial: str | None = None,
    ) -> None:
        """Remember what a device reported. A value that is None or empty keeps the known one;
        with nothing new to say, nothing is written."""
        values = [v or None for v in (platform, model, version, serial)]
        if not any(values):
            return
        self._exec(
            "INSERT INTO device_facts (customer, device, platform, model, version, serial,"
            " updated_at) VALUES (?,?,?,?,?,?,?)"
            " ON CONFLICT(customer, device) DO UPDATE SET"
            " platform = COALESCE(excluded.platform, platform),"
            " model = COALESCE(excluded.model, model),"
            " version = COALESCE(excluded.version, version),"
            " serial = COALESCE(excluded.serial, serial),"
            " updated_at = excluded.updated_at",
            (customer, device, *values, self._now()),
        )

    def device_facts(self, customer: str) -> dict[str, StoredFacts]:
        rows = self._query(
            "SELECT device, platform, model, version, serial, updated_at FROM device_facts"
            " WHERE customer = ?",
            (customer,),
        )
        return {
            r["device"]: StoredFacts(
                r["platform"],
                r["model"],
                r["version"],
                r["serial"],
                _parse_ts(r["updated_at"]),  # type: ignore[arg-type]
            )
            for r in rows
        }

    def device_activity(self, customer: str) -> dict[str, DeviceActivity]:
        """Each device's most recent job row, whatever its status: the job (newest created
        first) it was last part of."""
        rows = self._query(
            "SELECT device, job_id, play_path, status, at FROM ("
            " SELECT d.device AS device, d.job_id AS job_id, j.play_path AS play_path,"
            f" d.status AS status, {_ROW_TIME} AS at,"
            " ROW_NUMBER() OVER (PARTITION BY d.device"
            "  ORDER BY j.created_at DESC, j.rowid DESC) AS n"
            " FROM job_devices d JOIN jobs j ON j.id = d.job_id WHERE j.customer = ?"
            ") WHERE n = 1",
            (customer,),
        )
        return {
            r["device"]: DeviceActivity(
                r["job_id"],
                r["play_path"],
                DeviceStatus(r["status"]),
                _parse_ts(r["at"]),  # type: ignore[arg-type]
            )
            for r in rows
        }

    def last_contact(self, customer: str) -> dict[str, datetime]:
        """When Tsunami last reached each device: its newest job row in a status of
        CONNECTED_DEVICE_STATUSES."""
        marks = ",".join("?" * len(_CONNECTED_VALUES))
        rows = self._query(
            f"SELECT d.device AS device, MAX({_ROW_TIME}) AS at FROM job_devices d"
            f" JOIN jobs j ON j.id = d.job_id WHERE j.customer = ? AND d.status IN ({marks})"
            " GROUP BY d.device",
            (customer, *_CONNECTED_VALUES),
        )
        return {r["device"]: _parse_ts(r["at"]) for r in rows}  # type: ignore[misc]

    def recent_jobs_for_device(
        self, customer: str, device: str, limit: int = 10
    ) -> list[DeviceJob]:
        rows = self._query(
            "SELECT d.job_id AS job_id, j.play_path AS play_path, j.mode AS mode,"
            f" d.status AS status, {_ROW_TIME} AS at FROM job_devices d"
            " JOIN jobs j ON j.id = d.job_id WHERE j.customer = ? AND d.device = ?"
            " ORDER BY j.created_at DESC, j.rowid DESC LIMIT ?",
            (customer, device, limit),
        )
        return [
            DeviceJob(
                r["job_id"],
                r["play_path"],
                r["mode"],
                DeviceStatus(r["status"]),
                _parse_ts(r["at"]),  # type: ignore[arg-type]
            )
            for r in rows
        ]

    # --- row mapping ---

    @staticmethod
    def _job(r: sqlite3.Row) -> JobRecord:
        return JobRecord(
            id=r["id"],
            created_at=_parse_ts(r["created_at"]),  # type: ignore[arg-type]
            actor=Actor(r["actor"]),
            play_path=r["play_path"],
            play_hash=r["play_hash"],
            customer=r["customer"],
            mode=r["mode"],
            status=JobStatus(r["status"]),
            prompts=json.loads(r["prompts_json"]),
            tags=json.loads(r["tags_json"]),
            limit=json.loads(r["limit_json"]),
            approved_by=r["approved_by"],
            approved_at=_parse_ts(r["approved_at"]),
            started_at=_parse_ts(r["started_at"]),
            finished_at=_parse_ts(r["finished_at"]),
            heartbeat_at=_parse_ts(r["heartbeat_at"]),
            summary=json.loads(r["summary_json"]),
        )

    @staticmethod
    def _device(r: sqlite3.Row) -> DeviceRecord:
        return DeviceRecord(
            job_id=r["job_id"],
            device=r["device"],
            status=DeviceStatus(r["status"]),
            started_at=_parse_ts(r["started_at"]),
            finished_at=_parse_ts(r["finished_at"]),
            render_hash=r["render_hash"],
            render_masked=r["render_masked"],
            output_redacted=r["output_redacted"],
            diff_redacted=r["diff_redacted"],
            failed_line=r["failed_line"],
            error_redacted=r["error_redacted"],
            rollback_status=r["rollback_status"],
            backup_path=r["backup_path"],
        )
