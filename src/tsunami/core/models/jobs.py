"""Job and device statuses (design.md, Execution engine: Device level; Local data and audit).

JobRecord and DeviceRecord are the rows stored in history.db (core/history.py).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any


class Actor(StrEnum):
    GUI = "gui"
    MCP = "mcp"


class JobStatus(StrEnum):
    PENDING = "pending"
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    DENIED = "denied"
    EXPIRED = "expired"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    NO_CHANGES = "no_changes"  # an approval request whose diff would change nothing
    FAILED = "failed"
    INTERRUPTED = "interrupted"
    CANCELLED = "cancelled"


class DeviceStatus(StrEnum):
    PENDING = "pending"
    READY = "ready"  # diffed for an approval request; nothing applied yet
    NO_CHANGES = "no_changes"  # diffed for an approval request; already as rendered
    CONNECTING = "connecting"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    ROLLED_BACK = "rolled_back"
    ROLLBACK_FAILED = "rollback_failed"
    SKIPPED_PRECHECK = "skipped_precheck"
    SKIPPED_MAXFAIL = "skipped_maxfail"
    UNREACHABLE = "unreachable"
    HOSTKEY_MISMATCH = "hostkey_mismatch"
    RENDER_CHANGED = "render_changed"
    DEVICE_BUSY = "device_busy"
    INTERRUPTED = "interrupted"
    CANCELLED = "cancelled"


# A device in one of these states may be half-configured. The job view shows a red
# banner that is dismissed only by explicit acknowledgment.
ALARM_DEVICE_STATUSES = frozenset({DeviceStatus.ROLLBACK_FAILED, DeviceStatus.INTERRUPTED})


# Final device states: nothing more will happen to the device in this job.
TERMINAL_DEVICE_STATUSES = frozenset(
    {
        DeviceStatus.SUCCESS,
        DeviceStatus.NO_CHANGES,
        DeviceStatus.FAILED,
        DeviceStatus.ROLLED_BACK,
        DeviceStatus.ROLLBACK_FAILED,
        DeviceStatus.SKIPPED_PRECHECK,
        DeviceStatus.SKIPPED_MAXFAIL,
        DeviceStatus.UNREACHABLE,
        DeviceStatus.HOSTKEY_MISMATCH,
        DeviceStatus.RENDER_CHANGED,
        DeviceStatus.DEVICE_BUSY,
        DeviceStatus.INTERRUPTED,
        DeviceStatus.CANCELLED,
    }
)


@dataclass(frozen=True)
class JobRecord:
    """One row of history.db `jobs`. Timestamps are timezone-aware UTC."""

    id: str
    created_at: datetime
    actor: Actor
    play_path: str
    play_hash: str
    customer: str
    mode: str
    status: JobStatus
    prompts: dict[str, Any] = field(default_factory=dict)  # masked
    tags: list[str] = field(default_factory=list)
    limit: list[str] = field(default_factory=list)
    approved_by: str | None = None
    approved_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    heartbeat_at: datetime | None = None
    summary: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DeviceRecord:
    """One row of history.db `job_devices`. Text columns are already redacted or masked."""

    job_id: str
    device: str
    status: DeviceStatus
    started_at: datetime | None = None
    finished_at: datetime | None = None
    render_hash: str | None = None
    render_masked: str | None = None
    output_redacted: str | None = None
    diff_redacted: str | None = None
    failed_line: str | None = None
    error_redacted: str | None = None
    rollback_status: str | None = None
    backup_path: str | None = None
