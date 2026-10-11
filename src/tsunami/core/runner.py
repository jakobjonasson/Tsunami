"""Run level: validation, canary, batches, max-fail, cancellation, heartbeats.

Nothing connects to any device until every device has rendered cleanly. Apply runs with
canary on do one device, then wait for confirmation; the rest run in parallel batches,
capped at 50 sessions. After each batch the failure rate over all attempted devices is
checked against max_fail_percent, and once exceeded no further batch starts.

The runner knows nothing about the UI: it reports through on_event and asks for the
canary decision through confirm_canary. The GUI wraps it in a QThread; the CLI and the
MCP server call run() directly.

See design.md, Execution engine: Run level; Architecture: Process model.
"""

from __future__ import annotations

import hashlib
import logging
import threading
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tsunami.core.events import (
    BatchFinished,
    CanaryWaiting,
    DeviceStatusChanged,
    Event,
    JobFinished,
    JobStarted,
    MaxFailReached,
)
from tsunami.core.issues import Severity, TsunamiError
from tsunami.core.models.inventory import Device
from tsunami.core.models.jobs import Actor, DeviceStatus, JobStatus
from tsunami.core.models.play import ConnectionMethod, PlayType, RunMode
from tsunami.core.models.settings import AppSettings
from tsunami.core.paths import AppPaths
from tsunami.core.pipeline import DevicePipeline, JobContext
from tsunami.core.redact import Redactor
from tsunami.core.render.renderer import Renderer, RenderError
from tsunami.core.services import collect_prompts, play_entry
from tsunami.core.targets import resolve_targets
from tsunami.core.transports.base import JumpTarget, Transport

log = logging.getLogger(__name__)

MAX_SESSIONS = 50
HEARTBEAT_SECONDS = 10.0
# Statuses that count as failed for max_fail_percent. Pre-check skips and busy devices
# made no change, so they do not count.
FAILURE_STATUSES = frozenset(
    {
        DeviceStatus.FAILED,
        DeviceStatus.ROLLED_BACK,
        DeviceStatus.ROLLBACK_FAILED,
        DeviceStatus.UNREACHABLE,
        DeviceStatus.HOSTKEY_MISMATCH,
        DeviceStatus.RENDER_CHANGED,
        DeviceStatus.INTERRUPTED,
    }
)


@dataclass
class RunRequest:
    play_path: Path | str
    mode: RunMode
    prompts: Mapping[str, Any] = field(default_factory=dict)  # raw answers, shared
    device_prompts: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)  # per_device
    tags: Sequence[str] | None = None
    limit: Sequence[str] | None = None
    actor: Actor = Actor.GUI
    job_id: str | None = None  # an approved job created earlier by the approval flow
    approved_hashes: Mapping[str, str] | None = None
    allow_new_host_key: bool = True
    serial_port: str | None = None


@dataclass(frozen=True)
class _Target:
    label: str
    device: Device | None
    prompts: dict[str, Any]


class Runner:
    def __init__(
        self,
        workspace: Any,  # tsunami.core.workspace.Workspace
        *,
        transport: Transport,
        history: Any,  # tsunami.core.history.History
        secrets: Any,  # tsunami.core.secret_store.SecretStore
        paths: AppPaths,
        app_settings: AppSettings | None = None,
        on_event: Callable[[Event], None] | None = None,
        confirm_canary: Callable[[str], bool] | None = None,
        cancel: threading.Event | None = None,
        max_sessions: int = MAX_SESSIONS,
        heartbeat_seconds: float = HEARTBEAT_SECONDS,
    ) -> None:
        self.workspace = workspace
        self.transport = transport
        self.history = history
        self.secrets = secrets
        self.paths = paths
        self.app_settings = app_settings or AppSettings()
        self.on_event = on_event or (lambda event: None)
        self.confirm_canary = confirm_canary or (lambda device: True)
        self.cancel = cancel or threading.Event()
        self.max_sessions = max_sessions
        self.heartbeat_seconds = heartbeat_seconds

    # -- public -----------------------------------------------------------------

    def run(self, request: RunRequest) -> str:
        """Run one play. Returns the job id; problems are recorded on the job, not raised."""
        entry = play_entry(self.workspace, request.play_path)
        play = entry.play
        job_id = request.job_id or self.history.create_job(
            actor=request.actor,
            play_path=entry.path.as_posix(),
            play_hash=play_hash(self.workspace.root / entry.path),
            customer=play.customer,
            mode=str(request.mode),
            status=JobStatus.PENDING,
            prompts=dict(request.prompts),
            tags=list(request.tags) if request.tags is not None else None,
            limit=list(request.limit) if request.limit is not None else None,
        )
        try:
            targets = self._prepare(entry, request)
        except TsunamiError as exc:
            return self._fail_job(job_id, str(exc))
        self.history.set_job_status(job_id, JobStatus.RUNNING, started_at=_now())
        existing = {d.device for d in self.history.devices(job_id)}  # approved jobs have rows
        self.history.add_devices(job_id, [t.label for t in targets if t.label not in existing])
        self.on_event(
            JobStarted(
                job_id=job_id, devices=tuple(t.label for t in targets), mode=str(request.mode)
            )
        )

        if request.mode == RunMode.RENDER:
            return self._render_only(job_id, entry, targets, request)

        stop_heartbeat = threading.Event()
        heartbeat = threading.Thread(
            target=self._heartbeat, args=(job_id, stop_heartbeat), daemon=True
        )
        heartbeat.start()
        try:
            ctx = self._context(job_id, entry, targets, request)
            results = self._execute(ctx, targets, request)
        except TsunamiError as exc:
            stop_heartbeat.set()
            return self._fail_job(job_id, str(exc))
        finally:
            stop_heartbeat.set()
            heartbeat.join(timeout=5)
            close = getattr(self.transport, "close", None)
            if callable(close):  # jump-host connections live for one run
                close()
        return self._finish(job_id, results)

    # -- preparation (no device contact) ----------------------------------------------

    def _prepare(self, entry: Any, request: RunRequest) -> list[_Target]:
        play = entry.play
        errors = [i for i in self.workspace.issues_for(entry.path) if i.severity == Severity.ERROR]
        if errors:
            raise TsunamiError(
                "the play has validation errors:\n  " + "\n  ".join(map(str, errors))
            )
        if request.mode == RunMode.COMPLIANCE and play.type != PlayType.BASELINE:
            raise TsunamiError("compliance mode is for baseline plays only")
        cust = self.workspace.customer(play.customer)

        if play.type == PlayType.INITIAL:
            values = collect_prompts(self.workspace, entry, request.prompts)
            label = str(values.get("hostname") or "new-device").lower()
            targets = [_Target(label, None, values)]
        else:
            result = resolve_targets(
                play.targets,
                cust.devices,
                sites=cust.sites.keys(),
                roles=self.workspace.roles.keys(),
                limit=request.limit,
            )
            if not result.ok:
                raise TsunamiError("targets: " + "; ".join(f.message for f in result.findings))
            targets = []
            for device in result.devices:
                raw = {**request.prompts, **request.device_prompts.get(device.name, {})}
                values = collect_prompts(self.workspace, entry, raw, device=device.name)
                targets.append(_Target(device.name, device, values))

        # Validation render for every device before anything connects (run level step 4).
        renderer = Renderer(self.workspace, self.app_settings)
        problems = []
        for target in targets:
            try:
                renderer.render(
                    entry,
                    device=target.device.name if target.device else None,
                    prompt_values=target.prompts,
                    tags=request.tags,
                    validation=True,
                )
            except RenderError as exc:
                problems.append(f"{target.label}: {exc}")
        if problems:
            raise TsunamiError("render failed:\n  " + "\n  ".join(problems[:20]))
        return targets

    def _context(self, job_id: str, entry: Any, targets: list[_Target], request: RunRequest):
        play = entry.play
        cust = self.workspace.customer(play.customer)
        settings = play.settings(
            self.workspace.config.defaults.batch_size if self.workspace.config else None
        )
        credential = None
        if settings.method != ConnectionMethod.RENDER:
            try:
                credential = self.secrets.get_login(cust.config.credential)
            except TsunamiError:
                if settings.method != ConnectionMethod.SERIAL:  # factory-default consoles need none
                    raise
        jump = None
        if cust.config.jump_host is not None:
            jh = cust.config.jump_host
            jump = JumpTarget(jh.host, jh.port, self.secrets.get_login(jh.credential))

        renderer = Renderer(self.workspace, self.app_settings)
        secret_values: dict[str, str] = {}
        if request.mode == RunMode.APPLY:
            refs = set()
            for target in targets:
                rendering = renderer.render(
                    entry,
                    device=target.device.name if target.device else None,
                    prompt_values=target.prompts,
                    tags=request.tags,
                    validation=True,
                )
                refs.update(rendering.secret_refs)
            for ref in sorted(refs):
                customer, name = ref.split("/", 1)
                secret_values[ref] = self.secrets.get_secret(customer, name)

        exact = list(self.secrets.secret_values(play.customer)) if _can_list(self.secrets) else []
        exact += list(secret_values.values())
        for cred in (credential, jump.credential if jump else None):
            if cred is not None:
                exact += [v for v in (cred.password, cred.enable) if v]
        return JobContext(
            job_id=job_id,
            workspace=self.workspace,
            entry=entry,
            play=play,
            settings=settings,
            mode=request.mode,
            tags=tuple(request.tags) if request.tags is not None else None,
            transport=self.transport,
            history=self.history,
            paths=self.paths,
            app_settings=self.app_settings,
            redactor=Redactor(exact, extra_rules=self.app_settings.extra_rules()),
            renderer=renderer,
            cancel=self.cancel,
            emit=self.on_event,
            credential=credential,
            jump=jump,
            secret_values=secret_values,
            approved_hashes=request.approved_hashes,
            allow_new_host_key=request.allow_new_host_key,
            serial_port=request.serial_port,
        )

    # -- execution --------------------------------------------------------------------

    def _execute(
        self, ctx: JobContext, targets: list[_Target], request: RunRequest
    ) -> dict[str, DeviceStatus]:
        settings = ctx.settings
        results: dict[str, DeviceStatus] = {}
        pending = list(targets)
        batch_size = max(1, min(settings.batch_size, self.max_sessions))
        batches: list[list[_Target]] = []
        if request.mode == RunMode.APPLY and settings.canary and len(pending) > 1:
            batches.append([pending.pop(0)])
        batches += [pending[i : i + batch_size] for i in range(0, len(pending), batch_size)]
        canary = request.mode == RunMode.APPLY and settings.canary and len(targets) > 1

        with ThreadPoolExecutor(max_workers=batch_size, thread_name_prefix="tsunami") as pool:
            for number, batch in enumerate(batches, start=1):
                if self.cancel.is_set():
                    self._mark(ctx, batch_rest(batches, number), DeviceStatus.CANCELLED, results)
                    break
                statuses = list(pool.map(lambda t: self._device(ctx, t), batch))
                results.update({t.label: s for t, s in zip(batch, statuses, strict=True)})
                attempted = len(results)
                failed = sum(1 for s in results.values() if s in FAILURE_STATUSES)
                self.on_event(
                    BatchFinished(
                        job_id=ctx.job_id, batch=number, attempted=attempted, failed=failed
                    )
                )
                remaining = batch_rest(batches, number + 1)
                if not remaining:
                    break
                if attempted and failed * 100 / attempted > settings.max_fail_percent:
                    self.on_event(
                        MaxFailReached(
                            job_id=ctx.job_id,
                            failed=failed,
                            attempted=attempted,
                            max_fail_percent=settings.max_fail_percent,
                        )
                    )
                    self._mark(ctx, remaining, DeviceStatus.SKIPPED_MAXFAIL, results)
                    break
                if canary and number == 1:
                    if not self._canary(ctx, batch[0].label):
                        self.cancel.set()
                        self._mark(ctx, remaining, DeviceStatus.CANCELLED, results)
                        break
        return results

    def _device(self, ctx: JobContext, target: _Target) -> DeviceStatus:
        return DevicePipeline(ctx, target.device, target.prompts, label=target.label).run()

    def _canary(self, ctx: JobContext, device: str) -> bool:
        self.history.set_job_status(ctx.job_id, JobStatus.PAUSED)
        self.on_event(CanaryWaiting(job_id=ctx.job_id, device=device))
        approved = bool(self.confirm_canary(device))
        if approved:
            self.history.set_job_status(ctx.job_id, JobStatus.RUNNING)
        return approved

    def _mark(
        self,
        ctx: JobContext,
        targets: list[_Target],
        status: DeviceStatus,
        results: dict[str, DeviceStatus],
    ) -> None:
        for target in targets:
            self.history.update_device(ctx.job_id, target.label, status=status)
            results[target.label] = status

    def _render_only(self, job_id: str, entry: Any, targets: list[_Target], request) -> str:
        renderer = Renderer(self.workspace, self.app_settings)
        results = {}
        for target in targets:
            rendering = renderer.render(
                entry,
                device=target.device.name if target.device else None,
                prompt_values=target.prompts,
                tags=request.tags,
            )
            self.history.update_device(
                job_id,
                target.label,
                status=DeviceStatus.SUCCESS,
                render_masked=rendering.masked_text(),
                render_hash=rendering.render_hash(),
            )
            results[target.label] = DeviceStatus.SUCCESS
            self.on_event(
                DeviceStatusChanged(
                    job_id=job_id,
                    device=target.label,
                    status=DeviceStatus.SUCCESS,
                    detail="rendered",
                )
            )
        return self._finish(job_id, results)

    # -- bookkeeping --------------------------------------------------------------------

    def _heartbeat(self, job_id: str, stop: threading.Event) -> None:
        while not stop.wait(self.heartbeat_seconds):
            try:
                self.history.heartbeat(job_id)
            except Exception:  # noqa: BLE001 - a missed heartbeat must not kill the run
                log.warning("heartbeat for job %s failed", job_id)

    def _finish(self, job_id: str, results: dict[str, DeviceStatus]) -> str:
        counts: dict[str, int] = {}
        for status in results.values():
            counts[str(status)] = counts.get(str(status), 0) + 1
        if self.cancel.is_set() or DeviceStatus.CANCELLED in results.values():
            status = JobStatus.CANCELLED
        elif any(s in FAILURE_STATUSES for s in results.values()):
            status = JobStatus.FAILED
        else:
            status = JobStatus.COMPLETED
        self.history.set_job_status(job_id, status, finished_at=_now(), summary={"counts": counts})
        self.on_event(JobFinished(job_id=job_id, status=status, counts=counts))
        return job_id

    def _fail_job(self, job_id: str, message: str) -> str:
        self.history.set_job_status(
            job_id, JobStatus.FAILED, finished_at=_now(), summary={"error": message}
        )
        self.on_event(JobFinished(job_id=job_id, status=JobStatus.FAILED, counts={}))
        return job_id


def batch_rest(batches: list[list[_Target]], start: int) -> list[_Target]:
    """Targets in batches numbered `start` (1-based) and later."""
    return [t for batch in batches[start - 1 :] for t in batch]


def play_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _can_list(secrets: Any) -> bool:
    try:
        secrets.list_secrets  # noqa: B018
    except AttributeError:
        return False
    return True


def _now() -> datetime:
    return datetime.now(UTC)
