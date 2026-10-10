"""Job and device statuses (design.md, Execution engine: Device level; Local data and audit).

Phase 3 (work package 3.3) adds the job records stored in history.db.
"""

from __future__ import annotations

from enum import StrEnum


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
    FAILED = "failed"
    INTERRUPTED = "interrupted"
    CANCELLED = "cancelled"


class DeviceStatus(StrEnum):
    PENDING = "pending"
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
