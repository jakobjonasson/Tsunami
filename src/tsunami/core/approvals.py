"""Approving or denying a pending MCP change request, and the run that executes it.

A request stores the play hash and each device's render hash when it is created.
Approval records the Windows user and time, then builds a RunRequest carrying those
hashes; at execution every device is re-rendered and fails with render_changed if its
render differs. If the play file itself changed since the request, every device fails
that way, so content cannot be swapped after approval.

See design.md, MCP interface: Approval flow.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tsunami.core.issues import TsunamiError
from tsunami.core.models.jobs import Actor, JobStatus
from tsunami.core.models.play import RunMode
from tsunami.core.redact import supplied_values
from tsunami.core.runner import RunRequest, play_hash

PLAY_CHANGED = "play-changed"  # never equals a real render hash


def approve(history: Any, workspace: Any, job_id: str, *, approved_by: str) -> RunRequest:
    """Approve a pending request and return the run that executes it."""
    job = history.get_job(job_id)
    if job.status != JobStatus.AWAITING_APPROVAL:
        raise TsunamiError(f"job {job_id[:8]} is {job.status}, not awaiting approval")
    path = workspace.root / Path(job.play_path)
    if not path.is_file():
        raise TsunamiError(f"the play {job.play_path} no longer exists")
    devices = history.devices(job_id)
    hashes = {d.device: d.render_hash or "" for d in devices}
    if play_hash(path) != job.play_hash:
        hashes = dict.fromkeys(hashes, PLAY_CHANGED)
    history.approve(job_id, approved_by=approved_by)
    return RunRequest(
        play_path=job.play_path,
        mode=RunMode.APPLY,
        prompts=dict(job.prompts),
        tags=list(job.tags) or None,
        limit=[d.device for d in devices],
        actor=Actor.MCP,
        job_id=job_id,
        approved_hashes=hashes,
        exact_exempt=frozenset(supplied_values(dict(job.prompts))),  # Claude's own input
    )


def deny(history: Any, job_id: str, *, denied_by: str) -> None:
    job = history.get_job(job_id)
    if job.status != JobStatus.AWAITING_APPROVAL:
        raise TsunamiError(f"job {job_id[:8]} is {job.status}, not awaiting approval")
    history.set_job_status(
        job_id,
        JobStatus.DENIED,
        approved_by=denied_by,
        approved_at=datetime.now(UTC),
        finished_at=datetime.now(UTC),
    )
    history.cancel_ready_devices(job_id)
