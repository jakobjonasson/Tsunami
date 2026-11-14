"""Restore a device from one of its backups, over SSH or a serial console.

The backup's lines are sent in config mode, in order, one at a time, carrying on past any
line the device rejects (Session.paste_config). That is a merge: lines on the device that
the backup lacks stay. It is what a restore onto a factory-new or freshly reset device
needs, and over a console it is the only way in: there is no file transfer.

Before sending, plan_restore drops what is not config (headers, comments, IOS `end`) and
skips the platform's guarded config lines with their context (AOS-S module and stacking
provisioning, console speed, include-credentials; design.md, Guarded commands), which
need a reboot, could cut the console session, or change how credentials are stored. They
are listed for the person to apply by hand. Banners and IOS certificates are sent as
blocks, and IOS's `^C` banner delimiter, which `show running-config` prints as two
characters, is replaced with a character the banner does not contain.

Over a console, the transport's day-0 handling (transports/console.py) answers a
factory-new device's dialogs: IOS's initial configuration dialog, the AOS-CX forced admin
password and the AOS-S first-login manager password, which become the chosen login's
password. A device that has only a login set logs in with it. Over SSH, the device must
already have an address, SSH and a login.

The job then, in order: saves the device's current config as a backup (`pre-restore`,
only if it differs from the newest), sends the plan, creates IOS SSH host keys if the
device has none (and re-sends `ip ssh` lines IOS refused without them), saves to startup
(`write memory`), reads the config back, saves it as a backup (`restore`) and reports
the redacted difference from the backup restored.

A restore is recorded as an operation job (operations.RESTORE_MODE) that History lists
with play runs. It holds the device's lease and a heartbeat, as a run does. A session
that ends part way leaves the device half restored: its row is `interrupted`, which
raises the half-configured alarm.

Everything logged or stored passes the redactor, which also holds the login used.

See design.md, Backups: Restore from backup.
"""

from __future__ import annotations

import logging
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tsunami.core.backups import (
    PRE_RESTORE_TAG,
    RESTORE_TAG,
    BackupError,
    read_for_restore,
    redacted_diff,
    save_if_changed,
    write_backup,
)
from tsunami.core.configtree import is_header_line
from tsunami.core.diff import diff_config
from tsunami.core.errordetect import find_error
from tsunami.core.issues import TsunamiError
from tsunami.core.models.common import PLATFORM_FAMILY, Platform, PlatformFamily, require_slug
from tsunami.core.models.jobs import Actor, DeviceStatus
from tsunami.core.models.play import ConnectionMethod
from tsunami.core.operations import RESTORE_MODE, OperationLog, connect_status
from tsunami.core.paths import AppPaths
from tsunami.core.platforms import get_driver
from tsunami.core.redact import Redactor, redact_exception
from tsunami.core.secret_store import LoginCredential
from tsunami.core.transports.base import (
    ConfigChunk,
    ConnectError,
    ConnectTarget,
    HostKeyMismatchError,
    JumpTarget,
)

log = logging.getLogger(__name__)

# Console speed a platform starts at; a hardware catalog entry overrides it.
FAMILY_BAUD = {
    PlatformFamily.IOS: 9600,
    PlatformFamily.AOS_CX: 115200,
    PlatformFamily.AOS_S: 9600,
}
HEARTBEAT_SECONDS = 10.0
KEYGEN = "crypto key generate rsa modulus 2048"
SAVE_COMMAND = "write memory"

_BANNER_RE = re.compile(r"^banner\s+\S+\s+(?P<delim>\^C|\S)(?P<rest>.*)$")
_IOS_DELIMITERS = "#$%&@~*+=|"
_CERT_RE = re.compile(r"^\s+certificate\s+(?:self-signed\s+|ca\s+)?[0-9A-Fa-f]+\s*$")


@dataclass(frozen=True)
class SkippedLine:
    line: str  # raw; redact before showing
    reason: str


@dataclass(frozen=True)
class RestorePlan:
    chunks: tuple[ConfigChunk, ...]
    skipped: tuple[SkippedLine, ...] = ()

    @property
    def line_count(self) -> int:
        return sum(len(c.lines) for c in self.chunks)


def plan_restore(text: str, platform: Platform) -> RestorePlan:
    """The lines of a backup to send, in order, and those left out on purpose."""
    family = PLATFORM_FAMILY[platform]
    driver = get_driver(family)
    lines = text.replace("\r\n", "\n").replace("\r", "").split("\n")
    chunks: list[ConfigChunk] = []
    skipped: list[SkippedLine] = []
    i = 0
    while i < len(lines):
        raw = lines[i].rstrip()
        stripped = raw.strip()
        indented = raw[:1].isspace()
        i += 1
        if not stripped:
            continue
        banner = None if indented else _BANNER_RE.match(raw)
        if banner is not None:
            block, i = _banner(lines, i - 1, banner)
            chunks.append(ConfigChunk(tuple(block), block=True))
            continue
        if is_header_line(stripped, family) or driver.is_comment(raw):
            continue
        if family is PlatformFamily.IOS and not indented and stripped.lower() == "end":
            continue
        guarded = driver.guarded_config_line(stripped)
        if guarded is not None:
            skipped.append(SkippedLine(raw, f"guarded command ({guarded}): apply it by hand"))
            if not indented:  # and the context it opens, with its exit
                while i < len(lines) and lines[i][:1].isspace():
                    if lines[i].strip():
                        skipped.append(SkippedLine(lines[i].rstrip(), f"inside {guarded}"))
                    i += 1
            continue
        if family is PlatformFamily.IOS and _CERT_RE.match(raw):
            block = [raw]
            while i < len(lines):
                line = lines[i].rstrip()
                i += 1
                if line.strip():
                    block.append(line)
                if line.strip() == "quit":
                    break
            chunks.append(ConfigChunk(tuple(block), block=True))
            continue
        chunks.append(ConfigChunk((raw,)))
    return RestorePlan(tuple(chunks), tuple(skipped))


def _banner(lines: list[str], start: int, match: re.Match[str]) -> tuple[list[str], int]:
    """A banner's lines from its opening line, and the index after it."""
    delim = match["delim"]
    block = [lines[start].rstrip()]
    end = start + 1
    if delim not in match["rest"]:
        while end < len(lines):
            block.append(lines[end].rstrip())
            end += 1
            if delim in block[-1]:
                break
    if delim == "^C":  # two characters in show output; IOS reads the first as the delimiter
        body = "\n".join(block)
        free = next((c for c in _IOS_DELIMITERS if c not in body), None)
        if free is not None:
            block = [line.replace("^C", free) for line in block]
    return block, end


def address_in_config(text: str, host: str) -> bool:
    """Whether the backup gives some interface this address, so an SSH session to it
    survives the restore. A rough check, for a warning only."""
    pattern = re.compile(rf"^\s*ip\s+address\s+{re.escape(host)}(?:[/\s]|$)", re.M)
    return bool(pattern.search(text))


@dataclass(frozen=True)
class RestoreRequest:
    customer: str
    device: str  # the inventory device whose backup this is; the restore is recorded there
    platform: Platform
    backup: Path
    method: ConnectionMethod
    host: str | None = None  # SSH
    port: int = 22
    serial_port: str | None = None  # console
    baud: int | None = None
    credential: LoginCredential | None = field(default=None, repr=False)
    login_label: str = ""  # how the login was chosen, for history (never a password)
    jump: JumpTarget | None = None
    save: bool = True  # write memory afterwards
    ssh_keys: bool = True  # IOS: create SSH host keys if the device has none
    connect_timeout: float = 15
    command_timeout: float = 120

    @property
    def where(self) -> str:
        if self.method == ConnectionMethod.SERIAL:
            speed = f" at {self.baud} baud" if self.baud else ""
            return f"console {self.serial_port}{speed}"
        via = f" through jump host {self.jump.host}" if self.jump is not None else ""
        return f"SSH {self.host}:{self.port}{via}"


@dataclass(frozen=True)
class RestoreResult:
    status: DeviceStatus
    summary: str  # one line, redacted
    total: int = 0  # lines in the plan
    sent: int = 0
    rejected: tuple[tuple[str, str], ...] = ()  # (line, device message), redacted
    skipped: tuple[SkippedLine, ...] = ()  # redacted
    notes: tuple[str, ...] = ()  # redacted
    saved: bool = False
    pre_backup: Path | None = None
    post_backup: Path | None = None
    diff: str = ""  # redacted: the backup restored, to the device afterwards
    # Lines of the backup (as parent > line paths, redacted) not on the device afterwards.
    missing_lines: tuple[str, ...] = ()
    job_id: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == DeviceStatus.SUCCESS

    @property
    def missing(self) -> int:
        return len(self.missing_lines)

    def report(self) -> str:
        """The result as text, for history and the restore tab."""
        parts = [self.summary]
        if self.rejected:
            parts.append("Rejected by the device:")
            parts += [
                f"  {line.strip()}  ->  {msg}" if msg else f"  {line.strip()}"
                for line, msg in self.rejected
            ]
        if self.skipped:
            parts.append("Not sent (apply by hand if needed):")
            parts += [f"  {s.line.strip()}  ({s.reason})" for s in self.skipped]
        if self.missing_lines:
            parts.append("In the backup but not on the device afterwards:")
            parts += [f"  {line}" for line in self.missing_lines]
        if self.notes:
            parts.append("Notes:")
            parts += [f"  {note}" for note in self.notes]
        return "\n".join(parts)


class RestoreJob:
    """One restore. Call run() once (it blocks); cancel() from another thread stops it
    between lines, which leaves the device partly restored."""

    def __init__(
        self,
        paths: AppPaths,
        request: RestoreRequest,
        *,
        transport: Any,
        redactor: Redactor,
        history: Any = None,
        keep: int = 30,
        on_log: Callable[[str], None] | None = None,
        on_progress: Callable[[int, int], None] | None = None,
        cancel: threading.Event | None = None,
        actor: Actor = Actor.GUI,
    ) -> None:
        require_slug(request.customer)
        require_slug(request.device)
        if not isinstance(redactor, Redactor):
            raise TsunamiError("a restore needs a redactor")
        if request.method == ConnectionMethod.SSH and not request.host:
            raise TsunamiError("an SSH restore needs an address")
        if request.method == ConnectionMethod.SSH and request.credential is None:
            raise TsunamiError("an SSH restore needs a login")
        if request.method == ConnectionMethod.SERIAL and not request.serial_port:
            raise TsunamiError("a console restore needs a serial port")
        self.paths = paths
        self.request = request
        self.transport = transport
        self.history = history
        self.keep = keep
        self.on_log = on_log
        self.on_progress = on_progress
        self.cancel_event = cancel or threading.Event()
        self.actor = actor
        values: list[str] = []
        for cred in (request.credential, request.jump.credential if request.jump else None):
            if cred is not None:
                values += [cred.password, cred.enable or ""]
        self.redactor = redactor.with_values([v for v in values if v])
        self.family = PLATFORM_FAMILY[request.platform]
        self._notes: list[str] = []

    def cancel(self) -> None:
        self.cancel_event.set()

    # -- reporting -----------------------------------------------------------------------

    def _log(self, text: str) -> None:
        clean = self.redactor.text(str(text))
        log.info("restore %s/%s: %s", self.request.customer, self.request.device, clean)
        if self.on_log is not None:
            self.on_log(clean)

    def _note(self, text: str) -> None:
        self._notes.append(self.redactor.text(text))
        self._log(text)

    def _text(self, exc: BaseException) -> str:
        return redact_exception(exc, self.redactor)

    # -- run -----------------------------------------------------------------------------

    def run(self) -> RestoreResult:
        req = self.request
        record = OperationLog(
            self.history,
            mode=RESTORE_MODE,
            customer=req.customer,
            actor=self.actor,
            devices=[req.device],
            summary={"backup": req.backup.name, "via": req.where, "login": req.login_label},
        )
        stop = threading.Event()
        beat = threading.Thread(target=self._heartbeat, args=(record, stop), daemon=True)
        leased = False
        try:
            if record.job_id is not None:
                beat.start()
                leased = self.history.acquire_lease(req.customer, req.device, record.job_id)
                if not leased:
                    holder = self.history.lease_holder(req.customer, req.device) or "?"
                    result = RestoreResult(
                        DeviceStatus.DEVICE_BUSY,
                        f"{req.device} is in use by job {holder[:8]}; try again when it ends",
                    )
                    return self._finish(record, result)
            return self._finish(record, self._restore())
        except Exception as exc:  # noqa: BLE001 - reported, and recorded as failed
            log.warning("restore failed", exc_info=True)
            result = RestoreResult(DeviceStatus.FAILED, f"restore failed: {self._text(exc)}")
            return self._finish(record, result)
        finally:
            stop.set()
            if beat.is_alive():
                beat.join(timeout=5)
            if leased:
                try:
                    self.history.release_lease(req.customer, req.device, record.job_id)
                except Exception:  # noqa: BLE001
                    log.warning("cannot release the lease on %s", req.device, exc_info=True)

    def _heartbeat(self, record: OperationLog, stop: threading.Event) -> None:
        while not stop.wait(HEARTBEAT_SECONDS):
            try:
                self.history.heartbeat(record.job_id)
            except Exception:  # noqa: BLE001
                log.warning("restore heartbeat failed", exc_info=True)

    def _finish(self, record: OperationLog, result: RestoreResult) -> RestoreResult:
        result = _with(result, job_id=record.job_id)
        error = None if result.ok else result.summary
        record.device(
            self.request.device,
            result.status,
            output=result.report(),
            error=error,
            diff=result.diff or None,
            backup_path=str(self.request.backup),
        )
        record.finish(cancelled=result.status == DeviceStatus.CANCELLED)
        self._log(result.summary)
        return result

    def _restore(self) -> RestoreResult:
        req = self.request
        try:
            raw = read_for_restore(self.paths, req.customer, req.device, req.backup)
        except BackupError as exc:
            return RestoreResult(DeviceStatus.FAILED, f"cannot read the backup: {exc}")
        plan = plan_restore(raw, req.platform)
        skipped = tuple(SkippedLine(self.redactor.text(s.line), s.reason) for s in plan.skipped)
        total = plan.line_count
        if not total:
            return RestoreResult(DeviceStatus.FAILED, "the backup has no configuration lines")
        if self.cancel_event.is_set():
            return RestoreResult(DeviceStatus.CANCELLED, "cancelled before connecting")

        self._log(f"Connecting over {req.where}")
        target = ConnectTarget(
            customer=req.customer,
            device=req.device,
            host=req.host if req.method == ConnectionMethod.SSH else None,
            port=req.port,
            platform=req.platform,
            method=req.method,
            credential=req.credential,
            jump=req.jump if req.method == ConnectionMethod.SSH else None,
            connect_timeout=req.connect_timeout,
            command_timeout=req.command_timeout,
            serial_port=req.serial_port,
            baud=req.baud,
            allow_new_host_key=True,
            cancel=self.cancel_event,
            log=self._log,
        )
        try:
            session = self.transport.open(target)
        except ConnectError as exc:
            text = self._text(exc)
            if isinstance(exc, HostKeyMismatchError):
                text += (
                    ". If this is a replacement device, accept its new key with Trust host "
                    "keys on its site first."
                )
            return RestoreResult(connect_status(exc), text, total, skipped=skipped)
        try:
            return self._send(session, raw, plan, skipped)
        finally:
            try:
                session.close()
            except Exception:  # noqa: BLE001, S110 - closing is best effort
                pass

    def _send(self, session: Any, raw: str, plan: RestorePlan, skipped: tuple) -> RestoreResult:
        req = self.request
        total = plan.line_count
        pre = self._save_before(session)

        self._log(f"Sending {total} lines from {req.backup.name}")
        paste = session.paste_config(
            list(plan.chunks), cancel=self.cancel_event, progress=self._progress(total)
        )
        rejected = list(paste.rejected)
        for line, message in rejected:
            self._log(f"rejected: {line.strip()}  ->  {message}")
        if paste.error is not None:
            status = DeviceStatus.INTERRUPTED if paste.sent else DeviceStatus.FAILED
            summary = (
                f"{paste.error}: {paste.sent} of {total} lines were sent, so the device is "
                "partly restored"
            )
            return self._result(status, summary, total, paste.sent, rejected, skipped, pre)
        if paste.cancelled:
            summary = (
                f"cancelled after {paste.sent} of {total} lines; the device is partly restored"
            )
            return self._result(
                DeviceStatus.CANCELLED, summary, total, paste.sent, rejected, skipped, pre
            )

        if req.ssh_keys and self.family is PlatformFamily.IOS:
            rejected = self._ssh_keys(session, rejected)
        saved = self._save(session) if req.save else False
        post, diff, missing = self._read_back(session, raw)

        parts = [f"sent {total} lines"]
        if rejected:
            parts.append(f"{len(rejected)} rejected")
        if skipped:
            parts.append(f"{len(skipped)} not sent")
        if req.save:
            parts.append("saved to startup" if saved else "NOT saved to startup")
        if post is not None:
            count = len(missing)
            parts.append(
                f"{count} line{'s' if count != 1 else ''} of the backup missing afterwards"
                if count
                else "every line of the backup is on the device"
            )
        ok = not rejected and (saved or not req.save)
        status = DeviceStatus.SUCCESS if ok else DeviceStatus.FAILED
        return self._result(
            status,
            "Restored: " + ", ".join(parts),
            total,
            paste.sent,
            rejected,
            skipped,
            pre,
            saved=saved,
            post_backup=post,
            diff=diff,
            missing_lines=missing,
        )

    def _progress(self, total: int) -> Callable[[int], None]:
        def report(sent: int) -> None:
            if self.on_progress is not None:
                self.on_progress(sent, total)

        return report

    def _result(
        self,
        status: DeviceStatus,
        summary: str,
        total: int,
        sent: int,
        rejected: list[tuple[str, str]],
        skipped: tuple,
        pre: Path | None,
        **more: Any,
    ) -> RestoreResult:
        return RestoreResult(
            status,
            self.redactor.text(summary),
            total,
            sent,
            tuple((self.redactor.text(line), self.redactor.text(msg)) for line, msg in rejected),
            skipped,
            tuple(self._notes),
            pre_backup=pre,
            **more,
        )

    # -- steps ---------------------------------------------------------------------------

    def _save_before(self, session: Any) -> Path | None:
        req = self.request
        try:
            before = session.running_config()
            saved = save_if_changed(
                self.paths,
                req.customer,
                req.device,
                before,
                self.family,
                self.redactor,
                tag=PRE_RESTORE_TAG,
                keep=self.keep,
            )
        except Exception as exc:  # noqa: BLE001 - a restore onto a blank device goes on
            self._note(f"the device's config before the restore was not saved: {self._text(exc)}")
            return None
        if saved.path is not None:
            self._log(f"Saved the device's config before the restore as {saved.path.name}")
        else:
            self._log("The device's config matches its newest backup; nothing to save first")
        return saved.path

    def _ssh_keys(self, session: Any, rejected: list[tuple[str, str]]) -> list[tuple[str, str]]:
        try:
            if _has_keys(session):
                return rejected
            self._log(f"The device has no SSH host keys; creating them ({KEYGEN})")
            session.paste_config([ConfigChunk((KEYGEN,), block=True, check=False)])
            if not _has_keys(session):
                self._note(
                    "SSH host keys were not created (IOS needs a hostname and an ip "
                    f"domain-name); create them by hand: {KEYGEN}"
                )
                return rejected
            retry = [line for line, _ in rejected if line.strip().startswith("ip ssh")]
            if not retry:
                return rejected
            self._log(f"Sending {len(retry)} ip ssh line(s) again, now that keys exist")
            again = session.paste_config([ConfigChunk((line,)) for line in retry])
            still = {line for line, _ in again.rejected}
            return [(ln, msg) for ln, msg in rejected if ln not in retry or ln in still]
        except Exception as exc:  # noqa: BLE001
            self._note(f"SSH host keys were not checked: {self._text(exc)}")
            return rejected

    def _save(self, session: Any) -> bool:
        self._log(f"Saving to startup ({SAVE_COMMAND})")
        try:
            output = session.send_exec(
                SAVE_COMMAND,
                expect_prompt=r"\[confirm\]",
                answer="",
                read_timeout=max(self.request.command_timeout, 120),
            ).output
        except Exception as exc:  # noqa: BLE001
            self._note(f"{SAVE_COMMAND} failed: {self._text(exc)}")
            return False
        error = find_error(output, self.family)
        if error is not None:
            self._note(f"{SAVE_COMMAND} failed: {self.redactor.text(error)}")
            return False
        return True

    def _read_back(self, session: Any, raw: str) -> tuple[Path | None, str, tuple[str, ...]]:
        """Read the config back, save it as a backup, and compare it with the backup
        restored: the redacted line diff, and the backup's lines (by parent path, as a
        check's Missing) that the device does not have."""
        req = self.request
        try:
            after = session.running_config()
            path = write_backup(
                self.paths, req.customer, req.device, after, RESTORE_TAG, keep=self.keep
            )
        except Exception as exc:  # noqa: BLE001
            self._note(f"the config after the restore was not read back: {self._text(exc)}")
            return None, "", ()
        diff, _added, _removed = redacted_diff(
            raw, after, self.family, self.redactor, fromfile=req.backup.name, tofile=path.name
        )
        missing = diff_config(raw, after, self.family, redactor=self.redactor).missing
        self._log(f"Read the config back and saved it as {path.name}")
        return path, diff, missing


def _has_keys(session: Any) -> bool:
    output = session.send_exec("show crypto key mypubkey rsa").output
    return "key name" in output.lower()


def _with(result: RestoreResult, **changes: Any) -> RestoreResult:
    from dataclasses import replace

    return replace(result, **changes)
