"""Events the runner emits to its on_event callback.

The GUI turns these into Qt signals (throttled), the dev CLI prints them, and the MCP
server ignores them and polls history.db. Text in events is already redacted.

See design.md, Architecture: Process model.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from tsunami.core.models.jobs import DeviceStatus, JobStatus


@dataclass(frozen=True, kw_only=True)
class Event:
    job_id: str


@dataclass(frozen=True, kw_only=True)
class JobStarted(Event):
    devices: tuple[str, ...]
    mode: str


@dataclass(frozen=True, kw_only=True)
class DeviceStatusChanged(Event):
    device: str
    status: DeviceStatus
    detail: str = ""


@dataclass(frozen=True, kw_only=True)
class DeviceLog(Event):
    device: str
    text: str


@dataclass(frozen=True, kw_only=True)
class CanaryWaiting(Event):
    device: str


@dataclass(frozen=True, kw_only=True)
class BatchFinished(Event):
    batch: int
    attempted: int
    failed: int


@dataclass(frozen=True, kw_only=True)
class MaxFailReached(Event):
    failed: int
    attempted: int
    max_fail_percent: int


@dataclass(frozen=True, kw_only=True)
class JobFinished(Event):
    status: JobStatus
    counts: dict[str, int] = field(default_factory=dict)
