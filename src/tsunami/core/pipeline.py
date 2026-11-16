"""The device-level pipeline: connect, facts, final render, pre-checks, backup, revert
timer, steps, post-checks, then confirm and save, or roll back.

One call handles one device in apply, check or compliance mode. It never raises for a
device problem: every outcome becomes a device status, written to history.db and
emitted as an event, with all text redacted first.

See design.md, Execution engine: Device level (apply mode), Run modes, Diff and
compliance; Architecture: Process model (device leases).
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from tsunami.core.backups import write_backup
from tsunami.core.checks import run_check
from tsunami.core.diff import diff_config
from tsunami.core.errordetect import find_error
from tsunami.core.events import DeviceLog, DeviceStatusChanged, Event
from tsunami.core.facts import Facts, facts_commands, parse_facts
from tsunami.core.firmware import (
    DeviceGone,
    FirmwareError,
    FirmwareJob,
    ImageState,
    driver_for,
    same_version,
)
from tsunami.core.firmware.library import firmware_dir, resolve_image
from tsunami.core.models.common import PLATFORM_FAMILY, Platform
from tsunami.core.models.inventory import Device
from tsunami.core.models.jobs import DeviceStatus
from tsunami.core.models.play import (
    ConnectionMethod,
    EffectiveSettings,
    FirmwareAction,
    Play,
    PromptType,
    RollbackMode,
    RunMode,
    TransferMethod,
)
from tsunami.core.models.settings import AppSettings
from tsunami.core.paths import AppPaths
from tsunami.core.redact import Redactor, redact_exception
from tsunami.core.render.renderer import Renderer, RenderError, Rendering
from tsunami.core.render.secret_refs import MASK
from tsunami.core.rollback import Prepared, RollbackError, strategy_for
from tsunami.core.secret_store import LoginCredential
from tsunami.core.state import Row, StateError, read_table
from tsunami.core.transports.base import (
    ConnectCancelled,
    ConnectError,
    ConnectTarget,
    HostKeyMismatchError,
    JumpTarget,
    Session,
    Transport,
    UnreachableError,
    UploadError,
)

log = logging.getLogger(__name__)

# After a reload is sent: how long before the first attempt to reconnect (the device is
# still shutting down and would answer), and how long between attempts.
RELOAD_SETTLE_SECONDS = 60.0
RECONNECT_EVERY_SECONDS = 20.0


@dataclass
class JobContext:
    """What every device in one run shares. Built by the runner."""

    job_id: str
    workspace: Any  # tsunami.core.workspace.Workspace
    entry: Any  # tsunami.core.workspace.PlayEntry
    play: Play
    settings: EffectiveSettings
    mode: RunMode
    tags: tuple[str, ...] | None
    transport: Transport
    history: Any  # tsunami.core.history.History
    paths: AppPaths
    app_settings: AppSettings
    redactor: Redactor
    renderer: Renderer
    cancel: threading.Event
    emit: Callable[[Event], None]
    # Login per target label: the device's own, its site's default, or the customer's.
    credentials: Mapping[str, LoginCredential] = field(default_factory=dict, repr=False)
    # Jump host per target label, for targets whose effective jump host is set (device, then
    # site, then customer). A target not listed connects directly.
    jumps: Mapping[str, JumpTarget] = field(default_factory=dict, repr=False)
    secret_values: Mapping[str, str] = field(default_factory=dict, repr=False)
    approved_hashes: Mapping[str, str] | None = None
    allow_new_host_key: bool = True
    no_new_host_key: frozenset[str] = frozenset()  # labels refused a first-contact key
    serial_port: str | None = None  # chosen at run time when the play says port: ask
    # Per target label: why it has no login (a serial run goes on without one, since a
    # factory-default console needs none; the reason is logged before connecting).
    credential_notes: Mapping[str, str] = field(default_factory=dict)
    # The check behind an MCP change request: a checked device is ready or no_changes,
    # never success, since nothing was applied (runner.RunRequest.approval_check).
    approval_check: bool = False


class _Stop(Exception):
    """Ends the pipeline for one device with a status (and optional detail)."""

    def __init__(self, status: DeviceStatus, detail: str = "") -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


class DevicePipeline:
    def __init__(
        self, ctx: JobContext, device: Device | None, prompts: Mapping[str, Any], *, label: str
    ) -> None:
        self.ctx = ctx
        self.device = device
        self.prompts = dict(prompts)
        self.label = label  # device name, or the hostname being staged
        self.session: Session | None = None
        self.prepared: Prepared | None = None
        self.changed = False  # any config or exec step was sent
        self.running_size: int | None = None  # bytes of running config, for the snapshot
        self.output: list[str] = []  # this device's job log, redacted; kept in history
        self.target: ConnectTarget | None = None  # how this device was reached
        self.backup_path: str | None = None
        # State tables as read before any change, for post-checks that compare.
        self.state_before: dict[str, list[Row]] = {}
        self.firmware_job: FirmwareJob | None = None
        self._rendering: Rendering | None = None  # the final render, while steps run

    # -- events and history ------------------------------------------------------

    def status(self, status: DeviceStatus, detail: str = "", **fields: Any) -> None:
        detail = self.ctx.redactor.text(detail)
        if self.output:
            fields.setdefault("output_redacted", "\n".join(self.output)[-_OUTPUT_KEEP:])
        self.ctx.history.update_device(self.ctx.job_id, self.label, status=status, **fields)
        self.ctx.emit(
            DeviceStatusChanged(
                job_id=self.ctx.job_id, device=self.label, status=status, detail=detail
            )
        )

    def log(self, text: str) -> None:
        redacted = self.ctx.redactor.text(text)
        self.output.append(redacted)
        self.ctx.emit(DeviceLog(job_id=self.ctx.job_id, device=self.label, text=redacted))

    # -- the pipeline ----------------------------------------------------------------

    def run(self) -> DeviceStatus:
        ctx = self.ctx
        customer = ctx.play.customer
        leased = False
        try:
            if ctx.cancel.is_set():
                raise _Stop(DeviceStatus.CANCELLED, "cancelled before start")
            if ctx.mode == RunMode.APPLY and self.device is not None:
                leased = ctx.history.acquire_lease(customer, self.device.name, ctx.job_id)
                if not leased:
                    holder = ctx.history.lease_holder(customer, self.device.name)
                    raise _Stop(
                        DeviceStatus.DEVICE_BUSY, f"device is being changed by job {holder}"
                    )
            self.status(DeviceStatus.CONNECTING, started_at=_now())
            self.connect()
            self.status(DeviceStatus.RUNNING)
            facts = self.gather_facts()
            rendering = self.final_render(facts)
            if ctx.mode in (RunMode.CHECK, RunMode.COMPLIANCE):
                return self.check(rendering, facts)
            return self.apply(rendering, facts)
        except _Stop as stop:
            extra = {"backup_path": self.backup_path} if self.backup_path else {}
            self.status(
                stop.status,
                stop.detail,
                finished_at=_now(),
                error_redacted=ctx.redactor.text(stop.detail) or None,
                **extra,
            )
            return stop.status
        except Exception as exc:  # anything unexpected fails this device, never the run
            log.exception("device %s failed unexpectedly", self.label)
            detail = redact_exception(exc, ctx.redactor)
            self.status(DeviceStatus.FAILED, detail, finished_at=_now(), error_redacted=detail)
            return DeviceStatus.FAILED
        finally:
            if self.session is not None:
                try:
                    self.session.close()
                except Exception:  # noqa: BLE001 - closing must never mask the outcome
                    log.warning("closing session to %s failed", self.label)
            if leased and self.device is not None:
                ctx.history.release_lease(customer, self.device.name, ctx.job_id)

    def connect(self) -> None:
        ctx, device = self.ctx, self.device
        method = ctx.settings.method
        platform = self.platform()
        target = ConnectTarget(
            customer=ctx.play.customer,
            device=self.label,
            host=device.host if device else None,
            port=device.port if device else 22,
            platform=platform,
            method=method,
            credential=ctx.credentials.get(self.label),
            jump=ctx.jumps.get(self.label) if method == ConnectionMethod.SSH else None,
            connect_timeout=ctx.settings.connect_timeout,
            command_timeout=ctx.settings.command_timeout
            * (2 if method == ConnectionMethod.SERIAL else 1),
            serial_port=ctx.serial_port or _fixed_port(ctx.play.connection.port),
            baud=ctx.play.connection.baud or self.model_baud(),
            allow_new_host_key=ctx.allow_new_host_key and self.label not in ctx.no_new_host_key,
            cancel=ctx.cancel,
            log=self.log,
        )
        note = ctx.credential_notes.get(self.label)
        if note:
            self.log(f"login: {note}; connecting without one")
        self.target = target
        try:
            self.session = ctx.transport.open(target)
        except ConnectCancelled as exc:
            raise _Stop(DeviceStatus.CANCELLED, str(exc)) from None
        except HostKeyMismatchError as exc:
            raise _Stop(DeviceStatus.HOSTKEY_MISMATCH, str(exc)) from None
        except UnreachableError as exc:
            raise _Stop(DeviceStatus.UNREACHABLE, str(exc)) from None
        except ConnectError as exc:
            raise _Stop(DeviceStatus.UNREACHABLE, str(exc)) from None

    def model_baud(self) -> int | None:
        """Console speed of the chosen hardware model, for day-0 plays."""
        for name, prompt in self.ctx.play.prompts.items():
            if prompt.type == PromptType.MODEL and name in self.prompts:
                return self.ctx.workspace.catalog.get(self.prompts[name]).console_baud
        return None

    def platform(self) -> Platform:
        if self.device is not None:
            return self.device.platform
        resolved = self.ctx.workspace.resolve_vars(
            self.ctx.play.customer, play=self.ctx.entry, prompt_values=self.prompts
        )
        return Platform(resolved.values["platform"])

    def gather_facts(self) -> Facts | None:
        if not self.ctx.settings.gather_facts:
            return None
        assert self.session is not None
        family = PLATFORM_FAMILY[self.platform()]
        outputs = {cmd: self.session.send_exec(cmd).output for cmd in facts_commands(family)}
        facts = parse_facts(family, outputs)
        expected = self.platform()
        if facts.platform is not None and facts.platform != expected:
            raise _Stop(
                DeviceStatus.FAILED,
                f"device reports {facts.platform}, but inventory says {expected}",
            )
        known = [f"{k}={v}" for k, v in (("model", facts.model), ("version", facts.version)) if v]
        if known:
            self.log("facts: " + " ".join(known))
        if self.device is not None:  # a device staged over console has no inventory entry
            redact = self.ctx.redactor.text
            try:
                self.ctx.history.record_device_facts(
                    self.ctx.play.customer,
                    self.device.name,
                    platform=facts.platform.value if facts.platform else None,
                    model=redact(facts.model) if facts.model else None,
                    version=redact(facts.version) if facts.version else None,
                    serial=redact(facts.serial) if facts.serial else None,
                )
            except Exception:  # noqa: BLE001 - a display cache must not fail a run
                log.warning("could not record facts for %s", self.label, exc_info=True)
        return facts

    def final_render(self, facts: Facts | None) -> Rendering:
        ctx = self.ctx
        try:
            rendering = ctx.renderer.render(
                ctx.entry,
                device=self.device.name if self.device else None,
                prompt_values=self.prompts,
                facts=facts.as_dict() if facts else None,
                tags=ctx.tags,
            )
        except RenderError as exc:
            raise _Stop(DeviceStatus.FAILED, f"render failed: {exc}") from None
        render_hash = rendering.render_hash()
        approved = (ctx.approved_hashes or {}).get(self.label)
        ctx.history.update_device(
            ctx.job_id, self.label, render_hash=render_hash, render_masked=rendering.masked_text()
        )
        if ctx.approved_hashes is not None and approved != render_hash:
            raise _Stop(DeviceStatus.RENDER_CHANGED, "the render differs from what was approved")
        return rendering

    def check(self, rendering: Rendering, facts: Facts | None = None) -> DeviceStatus:
        ctx = self.ctx
        assert self.session is not None
        running = self.session.running_config()
        if ctx.mode == RunMode.CHECK:
            # What prepare() would fail on during an apply, found before anyone approves it.
            try:
                strategy_for(PLATFORM_FAMILY[rendering.platform], ctx.job_id).preflight(
                    self.session,
                    ctx.settings.rollback,
                    facts.version if facts else None,
                    len(running.encode("utf-8")),
                )
            except RollbackError as exc:
                raise _Stop(
                    DeviceStatus.FAILED,
                    f"rollback copy not possible: {redact_exception(exc, ctx.redactor)}",
                ) from None
        owns = [o for s in ctx.play.steps if s.owns for o in s.owns]
        family = PLATFORM_FAMILY[rendering.platform]
        result = diff_config(
            rendering.masked_lines(), running, family, owns=owns, redactor=ctx.redactor
        )
        added = [f"+ {m}" for m in result.missing]
        if ctx.mode == RunMode.CHECK:
            # What an apply would stop on, found before anyone approves it: a state table
            # this device cannot give, an image that does not fit.
            self.read_state(rendering, family, ("pre", "post"), DeviceStatus.FAILED)
            added += [f"+ {line}" for line in self.firmware_plan(rendering, facts)]
        diff_text = "\n".join([*added, *(f"- {e}" for e in result.extra)])
        if ctx.mode == RunMode.COMPLIANCE:
            ctx.history.add_compliance_result(
                job_id=ctx.job_id,
                device=self.label,
                missing_count=len(result.missing),
                extra_count=len(result.extra),
                details={"missing": list(result.missing), "extra": list(result.extra)},
            )
        status = DeviceStatus.SUCCESS
        if ctx.mode == RunMode.CHECK:
            changes = changes_device(diff_text, rendering.masked_text())
            if changes:
                detail = "changes to apply"
            else:
                detail = "no changes" + (" (device has extra lines)" if result.extra else "")
            if ctx.approval_check:
                status = DeviceStatus.READY if changes else DeviceStatus.NO_CHANGES
                if changes:
                    detail += "; nothing is applied until a person approves the request"
        else:
            detail = "in compliance" if result.clean else "drift found"
        self.status(status, detail, finished_at=_now(), diff_redacted=diff_text)
        return status

    def apply(self, rendering: Rendering, facts: Facts | None) -> DeviceStatus:
        ctx, session = self.ctx, self.session
        assert session is not None
        settings = ctx.settings
        family = PLATFORM_FAMILY[rendering.platform]

        for check in (c for c in rendering.checks if c.phase == "pre"):
            result = run_check(
                check,
                session,
                host=self.device.host if self.device else "",
                port=self.device.port if self.device else 22,
                connect_timeout=settings.connect_timeout,
                family=family,
            )
            if not result.ok:
                raise _Stop(DeviceStatus.SKIPPED_PRECHECK, f"{result.name}: {result.detail}")
            if check.state:
                self.log(f"pre-check {result.name}: ok ({result.output})")
        # Every table a post-check reads is read now, before anything changes: for the
        # comparison, and so a table this device cannot give stops it here, unchanged.
        self.state_before = self.read_state(
            rendering, family, ("post",), DeviceStatus.SKIPPED_PRECHECK
        )

        backup_path = self.backup_path = self.backup() if settings.backup else None
        if self.running_size is None:
            self.running_size = len(session.running_config().encode("utf-8"))
        strategy = strategy_for(family, ctx.job_id)
        try:
            self.prepared = strategy.prepare(
                session,
                settings.rollback,
                settings.revert_minutes,
                facts.version if facts else None,
                self.running_size,
            )
        except RollbackError as exc:
            raise _Stop(
                DeviceStatus.FAILED,
                f"rollback preparation failed: {redact_exception(exc, ctx.redactor)}",
            ) from None

        self._rendering = rendering
        failure = self.send_steps(rendering, family)
        session = self.session  # a firmware step's reload replaces it
        assert session is not None
        if failure is None:
            failure = self.post_checks(rendering)
        if failure is None:
            try:
                strategy.confirm(session, self.prepared)
                if settings.save:
                    session.send_exec("write memory", read_timeout=settings.command_timeout * 2)
            except Exception as exc:  # noqa: BLE001 - reported, then rolled back below
                failure = f"confirm/save failed: {redact_exception(exc, ctx.redactor)}"
            else:
                warning = self.clean_up(strategy)
                extra = {"error_redacted": ctx.redactor.text(warning)} if warning else {}
                self.status(
                    DeviceStatus.SUCCESS,
                    warning or "",
                    finished_at=_now(),
                    backup_path=backup_path,
                    **extra,
                )
                return DeviceStatus.SUCCESS

        return self.roll_back(strategy, failure, backup_path)

    def send_steps(self, rendering: Rendering, family) -> str | None:
        """Send each step; return why it failed, or None."""
        ctx, session = self.ctx, self.session
        assert session is not None
        first_config = True
        steps = (
            rendering.materialize(ctx.secret_values) if rendering.secret_refs else rendering.steps
        )
        for step in steps:
            if ctx.cancel.is_set():
                return "cancelled"
            if step.skipped:
                self.log(f"step {step.index + 1} skipped ({step.skipped})")
                continue
            if step.kind == "firmware":
                error = self.firmware_step(step)
                session = self.session  # a new one after a reload
                assert session is not None
                if error:
                    return f"step {step.index + 1}: {error}"
                continue
            if step.kind == "exec":
                result = session.send_exec(
                    step.lines[0],
                    expect_prompt=step.expect_prompt,
                    answer=step.answer,
                    read_timeout=step.read_timeout,
                )
                self.changed = True
                error = find_error(result.output, family, self.benign(family))
                self.log(f"exec: {step.lines[0]}\n{result.output}")
                if error:
                    self.ctx.history.update_device(
                        ctx.job_id, self.label, failed_line=step.lines[0]
                    )
                    return f"step {step.index + 1}: {error}"
                continue
            command = self.prepared.config_mode_command if first_config and self.prepared else None
            first_config = False
            result = session.send_config(list(step.lines), config_mode_command=command)
            self.changed = True
            self.log(result.output)
            if not result.ok:
                failed = ctx.redactor.text(result.failed_line or "")
                ctx.history.update_device(ctx.job_id, self.label, failed_line=failed or None)
                return f"step {step.index + 1} rejected: {failed or result.error}"
        return None

    def post_checks(self, rendering: Rendering) -> str | None:
        ctx = self.ctx
        for check in (c for c in rendering.checks if c.phase == "post"):
            result = run_check(
                check,
                self.session,
                host=self.device.host if self.device else "",
                port=self.device.port if self.device else 22,
                connect_timeout=ctx.settings.connect_timeout,
                family=PLATFORM_FAMILY[rendering.platform],
                before=self.state_before,
            )
            passed = f"ok ({result.output})" if check.state else "ok"
            self.log(f"post-check {result.name}: {passed if result.ok else result.detail}")
            if not result.ok:
                return f"post-check failed: {result.name}: {result.detail}"
        return None

    # -- state checks ------------------------------------------------------------------

    def read_state(
        self, rendering: Rendering, family, phases: tuple[str, ...], on_error: DeviceStatus
    ) -> dict[str, list[Row]]:
        """Read every state table the checks of these phases use, once each. A table the
        device cannot give ends the device with on_error."""
        assert self.session is not None
        names = [c.state for c in rendering.checks if c.state and c.phase in phases]
        tables: dict[str, list[Row]] = {}
        for name in dict.fromkeys(names):
            try:
                tables[name] = read_table(self.session, family, name)
            except StateError as exc:
                raise _Stop(on_error, f"state check cannot run: {exc}") from None
            count = len(tables[name])
            self.log(f"state {name}: {count} row{'' if count == 1 else 's'} before the change")
        return tables

    # -- firmware ----------------------------------------------------------------------

    def firmware(self, rendering: Rendering) -> FirmwareJob:
        """This device's firmware job: its image, and how to upload and to reconnect."""
        if self.firmware_job is not None:
            return self.firmware_job
        ctx = self.ctx
        firmware = ctx.play.firmware
        assert firmware is not None and self.session is not None
        model = self.device.model if self.device else None
        chosen = firmware.image_for(rendering.platform, model)
        if chosen is None:
            raise FirmwareError(f"no firmware image is listed for {rendering.platform}")
        folder = firmware_dir(ctx.paths, ctx.app_settings)
        image = resolve_image(folder, chosen, need_file=firmware.transfer == TransferMethod.SCP)
        upload = None
        if hasattr(ctx.transport, "upload") and self.target is not None:
            target = self.target

            def upload(local, remote, progress=None) -> None:
                try:
                    ctx.transport.upload(
                        target, local, remote, progress=progress, cancel=ctx.cancel
                    )
                except (UploadError, ConnectError) as exc:
                    raise FirmwareError(f"upload failed: {exc}") from None

        self.firmware_job = FirmwareJob(
            platform=rendering.platform,
            session=self.session,
            image=image,
            transfer=firmware.transfer,
            server=firmware.server,
            vrf=firmware.vrf,
            log=self.log,
            upload=upload,
            reconnect=self.reconnect,
            cancel=ctx.cancel,
            command_timeout=ctx.settings.command_timeout,
            reload_timeout=firmware.reload_timeout,
            sleep=self.wait,
        )
        return self.firmware_job

    def wait(self, seconds: float) -> None:
        """Sleep, but wake at once when the job is cancelled."""
        self.ctx.cancel.wait(seconds)

    def reconnect(self) -> Session:
        """After a reload: close the dead session, wait for the device, open a new one.
        The new session replaces the pipeline's and the firmware job's."""
        ctx = self.ctx
        firmware = ctx.play.firmware
        limit = float(firmware.reload_timeout if firmware else 1800)
        assert self.target is not None
        if self.session is not None:
            try:
                self.session.close()
            except Exception:  # noqa: BLE001 - the device hung up on it
                log.debug("closing the session to %s after its reload failed", self.label)
            self.session = None
        self.log(f"waiting for {self.label} to come back (up to {limit / 60:.0f} minutes)")
        started = _monotonic()
        self.wait(min(RELOAD_SETTLE_SECONDS, limit))
        last = ""
        while _monotonic() - started < limit:
            if ctx.cancel.is_set():
                raise DeviceGone(
                    "cancelled while waiting for the device to come back: the reload was "
                    "already sent, so check the device by hand"
                )
            try:
                session = ctx.transport.open(self.target)
            except HostKeyMismatchError as exc:
                raise DeviceGone(f"the device came back with a different host key: {exc}") from None
            except ConnectError as exc:
                last = str(exc)
                self.wait(RECONNECT_EVERY_SECONDS)
                continue
            self.session = session
            if self.firmware_job is not None:
                self.firmware_job.session = session
            self.log(f"{self.label} is back after {(_monotonic() - started) / 60:.1f} minutes")
            return session
        raise DeviceGone(
            f"the device did not come back within {limit / 60:.0f} minutes"
            + (f" (last attempt: {last})" if last else "")
        )

    def firmware_step(self, step) -> str | None:
        """Run one firmware step; return why it failed, or None. A device that does not
        come back from its reload ends as interrupted, which raises the alarm a
        half-configured device does: it needs a person."""
        action = step.lines[0].split(" ", 1)[0]
        try:
            assert self._rendering is not None
            job = self.firmware(self._rendering)
            driver = driver_for(PLATFORM_FAMILY[job.platform])
            state = driver.inspect(job)
            self.log(f"firmware: {state.detail}")
            if same_version(state.version, job.image.version):
                self.log(f"firmware {action}: already runs {job.image.version}; nothing to do")
                return None
            if action == FirmwareAction.STAGE:
                self._enough_space(job, state)
                driver.stage(job)
                self.changed = True
                self.log(f"firmware stage: {job.image.file} is on the device and verified")
            else:
                self.changed = True
                driver.activate(job)
                self.log(f"firmware activate: the device runs {job.image.version}")
        except DeviceGone as exc:
            raise _Stop(DeviceStatus.INTERRUPTED, f"firmware {action}: {exc}") from None
        except FirmwareError as exc:
            return f"firmware {action}: {exc}"
        return None

    @staticmethod
    def _enough_space(job: FirmwareJob, state: ImageState) -> None:
        size, free = job.image.size, state.free_bytes
        if state.staged or size is None or free is None or free > size:
            return
        raise FirmwareError(
            f"not enough free space for {job.image.file}: it is {size:,} bytes and the "
            f"device has {free:,} free; remove old images first"
        )

    def firmware_plan(self, rendering: Rendering, facts: Facts | None) -> list[str]:
        """Check mode: what the firmware steps would do to this device, as lines for the
        diff. Fails the device now for what an apply would fail on."""
        steps = [s for s in rendering.steps if s.kind == "firmware" and not s.skipped]
        if not steps:
            return []
        try:
            job = self.firmware(rendering)
            state = driver_for(PLATFORM_FAMILY[job.platform]).inspect(job)
            self.log(f"firmware: {state.detail}")
            if same_version(state.version, job.image.version):
                return []
            actions = [s.lines[0].split(" ", 1)[0] for s in steps]
            if FirmwareAction.STAGE in actions:
                self._enough_space(job, state)
            elif not state.staged:
                raise FirmwareError(
                    f"{job.image.file} is not on the device: run the stage step first"
                )
        except FirmwareError as exc:
            raise _Stop(DeviceStatus.FAILED, f"firmware: {exc}") from None
        where = "already on the device" if state.staged else "not on the device yet"
        running = state.version or (facts.version if facts else None) or "unknown"
        return [
            f"firmware {action}: {running} -> {job.image.version} with {job.image.file} ({where})"
            for action in actions
        ]

    def roll_back(self, strategy, failure: str, backup_path: str | None) -> DeviceStatus:
        ctx, session = self.ctx, self.session
        cancelled = failure == "cancelled"
        final = DeviceStatus.CANCELLED if cancelled else DeviceStatus.FAILED
        if not self.changed or self.prepared is None or self.prepared.mode == RollbackMode.NONE:
            self.status(
                final,
                failure,
                finished_at=_now(),
                backup_path=backup_path,
                error_redacted=ctx.redactor.text(failure),
            )
            return final
        try:
            strategy.revert(session, self.prepared)
        except Exception as exc:  # noqa: BLE001
            detail = f"{failure}; rollback failed: {redact_exception(exc, ctx.redactor)}"
            self.status(
                DeviceStatus.ROLLBACK_FAILED,
                detail,
                finished_at=_now(),
                backup_path=backup_path,
                rollback_status="failed",
                error_redacted=detail,
            )
            return DeviceStatus.ROLLBACK_FAILED
        status = DeviceStatus.CANCELLED if cancelled else DeviceStatus.ROLLED_BACK
        warning = self.clean_up(strategy)
        failure = f"{failure}; {warning}" if warning else failure
        self.status(
            status,
            f"{failure}; rolled back",
            finished_at=_now(),
            backup_path=backup_path,
            rollback_status="ok",
            error_redacted=ctx.redactor.text(failure),
        )
        return status

    def clean_up(self, strategy) -> str | None:
        """Delete the rollback snapshot from the device. A failure is a warning: the
        change (or the revert) already happened, and a leftover file is harmless."""
        if self.prepared is None or self.prepared.mode == RollbackMode.NONE:
            return None
        try:
            strategy.cleanup(self.session, self.prepared)
        except Exception as exc:  # noqa: BLE001 - reported, never rolled back for
            warning = (
                f"could not delete the rollback copy {self.prepared.name}: "
                f"{redact_exception(exc, self.ctx.redactor)}"
            )
            self.log(warning)
            return warning
        return None

    def backup(self) -> str | None:
        """Save running config, unredacted, to machine data (design.md, Backups)."""
        assert self.session is not None and self.device is not None
        ctx = self.ctx
        running = self.session.running_config()
        self.running_size = len(running.encode("utf-8"))
        path = write_backup(
            ctx.paths,
            ctx.play.customer,
            self.device.name,
            running,
            ctx.job_id,
            keep=ctx.app_settings.backup_retention,
        )
        return str(path)

    def benign(self, family) -> list[str]:
        return list(self.ctx.app_settings.benign_messages.get(family, []))


def changes_device(diff_text: str | None, render_masked: str | None) -> bool:
    """Whether applying would do anything to the device, from its check-mode result.

    New config lines would. So would exec steps (write memory and the like), which a
    config diff never shows, and lines carrying a secret, which the diff compares only
    up to the secret and so cannot tell current from rotated.
    """
    diff = (diff_text or "").split("\n")
    render = (render_masked or "").split("\n")
    return (
        any(line.startswith("+ ") for line in diff)
        or any(line.startswith("exec: ") for line in render)
        or any(MASK in line for line in render)
    )


def _fixed_port(port: str | None) -> str | None:
    return None if port in (None, "ask") else port


_OUTPUT_KEEP = 200_000  # characters of a device's log kept in history


def _now() -> datetime:
    return datetime.now(UTC)


def _monotonic() -> float:
    return time.monotonic()
