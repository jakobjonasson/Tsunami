"""Running a past job again from the job view: apply what a check found, retry the
devices that did not finish, or repeat the whole job.

Each builds a RunRequest from the job's stored prompts, tags and customer. Apply after
a check pins every device to the render hash the check recorded, the same guard the
approval flow uses: if the play, the inventory or a variable changed since, that device
stops with render_changed instead of pushing config nobody reviewed.

A job can only be rebuilt when everything it ran with is in history.db. Per-device
prompt answers are not stored, and an initial play needs a console port and a new
device; for those, rebuild_problem() says why and the GUI opens the run wizard instead.

See design.md, GUI: Views (Job view).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from tsunami.core.approvals import PLAY_CHANGED, would_change
from tsunami.core.issues import TsunamiError
from tsunami.core.models.jobs import Actor, DeviceStatus, JobRecord, JobStatus
from tsunami.core.models.play import PlayType, RunMode
from tsunami.core.runner import RunRequest, play_hash

# Devices a retry leaves alone: they finished, or a precheck deliberately skipped them.
NOT_RETRIED = frozenset(
    {
        DeviceStatus.SUCCESS,
        DeviceStatus.NO_CHANGES,
        DeviceStatus.READY,
        DeviceStatus.SKIPPED_PRECHECK,
    }
)
_ACTIVE = frozenset({JobStatus.PENDING, JobStatus.RUNNING, JobStatus.AWAITING_APPROVAL})


def changed_devices(history: Any, job_id: str) -> list[str]:
    """The devices a finished check job says an apply would change."""
    job = history.get_job(job_id)
    if job.mode != str(RunMode.CHECK) or job.status in _ACTIVE:
        return []
    return [
        d.device
        for d in history.devices(job_id)
        if d.status == DeviceStatus.SUCCESS and would_change(d)
    ]


def retry_devices(history: Any, job_id: str) -> list[str]:
    """The devices of a finished job that did not complete."""
    job = history.get_job(job_id)
    if job.status in _ACTIVE:
        return []
    return [d.device for d in history.devices(job_id) if d.status not in NOT_RETRIED]


def rebuild_problem(workspace: Any, job: JobRecord) -> str | None:
    """Why this job cannot be run again from history alone; None if it can."""
    entry = workspace.plays.get(Path(job.play_path))
    if entry is None:
        return f"the play {job.play_path} no longer exists"
    if entry.play is None:
        return f"{job.play_path} no longer passes validation"
    if entry.play.type == PlayType.INITIAL:
        return "an initial play needs its console port and device details again"
    if any(p.per_device for p in entry.play.prompts.values()):
        return "per-device answers are not kept in history"
    return None


def apply_checked_request(history: Any, workspace: Any, job_id: str) -> RunRequest:
    """Apply exactly what a finished check job found, on the devices it would change."""
    job = history.get_job(job_id)
    devices = changed_devices(history, job_id)
    if not devices:
        raise TsunamiError(f"job {job_id[:8]} has nothing to apply")
    hashes = {d.device: d.render_hash or "" for d in history.devices(job_id)}
    pinned = {name: hashes.get(name, "") for name in devices}
    if play_hash(workspace.root / Path(job.play_path)) != job.play_hash:
        pinned = dict.fromkeys(pinned, PLAY_CHANGED)
    return _request(workspace, job, RunMode.APPLY, devices, approved_hashes=pinned)


def retry_request(history: Any, workspace: Any, job_id: str) -> RunRequest:
    """The same job again, on the devices that did not complete."""
    job = history.get_job(job_id)
    devices = retry_devices(history, job_id)
    if not devices:
        raise TsunamiError(f"job {job_id[:8]} has no devices to retry")
    return _request(workspace, job, RunMode(job.mode), devices)


def again_request(history: Any, workspace: Any, job_id: str) -> RunRequest:
    """The same job again on all its devices, rendered fresh."""
    job = history.get_job(job_id)
    devices = [d.device for d in history.devices(job_id)]
    return _request(workspace, job, RunMode(job.mode), devices)


def _request(
    workspace: Any,
    job: JobRecord,
    mode: RunMode,
    devices: list[str],
    *,
    approved_hashes: dict[str, str] | None = None,
) -> RunRequest:
    problem = rebuild_problem(workspace, job)
    if problem is not None:
        raise TsunamiError(problem)
    return RunRequest(
        play_path=job.play_path,
        mode=mode,
        prompts=dict(job.prompts),
        tags=list(job.tags) or None,
        limit=devices,
        actor=Actor.GUI,  # a person asked for it in the Tsunami window
        approved_hashes=approved_hashes,
        customer=job.customer,  # binds a global play again
    )
