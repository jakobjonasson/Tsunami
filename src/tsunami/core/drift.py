"""Back up a site's devices and report config drift since their last backup.

Each device is logged in to with its own login (device, else site default, else customer)
and its running config read. A device without a backup gets its first one. Otherwise the
config is compared with the newest backup, whoever took it (a job, discovery or an earlier
drift check); when it differs a new backup is saved and the redacted diff reported
(backups.save_if_changed). Nothing is sent to the device but `show running-config`, and
the inventory is not touched.

Device output is untrusted; the diff reaches the GUI already redacted, as text.

Given a History, the check is recorded as a job (mode `drift`, play path DRIFT_JOB) with
a row per device, so it shows in History and in a device's Last job and Last contact. A
device that was read is `success`; the drift result goes in the row's output, the
redacted diff in its diff, and the new backup's path in backup_path.

See design.md, Backups: Drift check.
"""

from __future__ import annotations

import logging
import threading
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from tsunami.core.backups import DRIFT_TAG, BackupError, Change, save_if_changed
from tsunami.core.issues import TsunamiError
from tsunami.core.models.common import PLATFORM_FAMILY, Platform, require_slug
from tsunami.core.models.jobs import Actor, DeviceStatus, JobStatus
from tsunami.core.models.play import ConnectionMethod
from tsunami.core.paths import AppPaths
from tsunami.core.redact import Redactor, redact_exception
from tsunami.core.secret_store import LoginCredential
from tsunami.core.transports.base import (
    AuthenticationError,
    ConnectCancelled,
    ConnectError,
    ConnectTarget,
    HostKeyMismatchError,
    JumpTarget,
    UnreachableError,
)

log = logging.getLogger(__name__)

MAX_WORKERS = 10  # matches the jump host channel cap, as in a run
DRIFT_JOB = "Drift check"  # the play path a drift check's job records
DRIFT_MODE = "drift"
HEARTBEAT_SECONDS = 10.0


class DriftStatus(StrEnum):
    FIRST = "first backup"
    CHANGED = "changed"
    SECRETS = "secrets changed"
    UNCHANGED = "no change"
    UNREACHABLE = "unreachable"
    LOGIN_FAILED = "login failed"
    HOSTKEY_CHANGED = "host key changed"
    NO_LOGIN = "no login"
    FAILED = "failed"
    CANCELLED = "cancelled"


_FROM_CHANGE = {
    Change.FIRST: DriftStatus.FIRST,
    Change.CHANGED: DriftStatus.CHANGED,
    Change.SECRETS: DriftStatus.SECRETS,
    Change.UNCHANGED: DriftStatus.UNCHANGED,
}
READ = frozenset(_FROM_CHANGE.values())  # the device was read and compared

# How each result is recorded in history.db. A failed login means the device answered (it
# counts as contact, as a failed run does); no login and a cancel mean it was never tried.
_DEVICE_STATUS = {
    **{status: DeviceStatus.SUCCESS for status in READ},
    DriftStatus.UNREACHABLE: DeviceStatus.UNREACHABLE,
    DriftStatus.HOSTKEY_CHANGED: DeviceStatus.HOSTKEY_MISMATCH,
    DriftStatus.LOGIN_FAILED: DeviceStatus.FAILED,
    DriftStatus.NO_LOGIN: DeviceStatus.SKIPPED_PRECHECK,
    DriftStatus.FAILED: DeviceStatus.FAILED,
    DriftStatus.CANCELLED: DeviceStatus.CANCELLED,
}


def device_status(status: DriftStatus) -> DeviceStatus:
    """The history.db status a drift result is recorded with."""
    return _DEVICE_STATUS[status]


@dataclass(frozen=True)
class DriftDevice:
    name: str
    host: str
    platform: Platform
    login: str  # the login's name in the credential store
    port: int = 22


@dataclass(frozen=True)
class DriftResult:
    device: str
    host: str
    status: DriftStatus
    detail: str = ""
    previous_at: datetime | None = None  # UTC; the backup it was compared with
    added: int = 0
    removed: int = 0
    diff: str = ""  # redacted, previous backup to now
    backup_path: Path | None = None  # the backup this check saved, if any

    @property
    def text(self) -> str:
        """One line for history and tables: the status, its changes and the detail."""
        head = str(self.status)
        if self.diff:
            head += f" (+{self.added} -{self.removed})"
        return f"{head}: {self.detail}" if self.detail else head


@dataclass(frozen=True)
class DriftReport:
    results: list[DriftResult]
    cancelled: bool = False

    def count(self, status: DriftStatus) -> int:
        return sum(1 for r in self.results if r.status == status)

    def summary(self) -> str:
        counts = Counter(r.status for r in self.results)
        parts = [
            f"{counts[DriftStatus.CHANGED] + counts[DriftStatus.SECRETS]} changed",
            f"{counts[DriftStatus.UNCHANGED]} unchanged",
            f"{counts[DriftStatus.FIRST]} first backups",
        ]
        failed = sum(n for s, n in counts.items() if s not in READ)
        if failed:
            parts.append(f"{failed} not read")
        return ", ".join(parts) + (" (cancelled)" if self.cancelled else "") + "."


class DriftCheck:
    """One drift check. Call run() once (it blocks); cancel() from another thread."""

    def __init__(
        self,
        paths: AppPaths,
        customer: str,
        devices: Sequence[DriftDevice],
        *,
        logins: Mapping[str, LoginCredential],
        transport: Any,
        redactor: Redactor,
        jump: JumpTarget | None = None,
        jumps: Mapping[str, JumpTarget] | None = None,
        keep: int = 30,
        on_result: Callable[[DriftResult], None] | None = None,
        cancel: threading.Event | None = None,
        workers: int = MAX_WORKERS,
        connect_timeout: float = 15,
        command_timeout: float = 60,
        allow_new_host_key: bool = True,
        history: Any = None,
        actor: Actor = Actor.GUI,
    ) -> None:
        require_slug(customer)
        if not devices:
            raise TsunamiError("there are no devices to check")
        if not isinstance(redactor, Redactor):
            raise TsunamiError("a drift check needs a redactor")
        self.paths = paths
        self.customer = customer
        self.devices = list(devices)
        self.logins = dict(logins)
        self.transport = transport
        self.jump = jump  # for every device that has no entry in jumps
        self.jumps = dict(jumps or {})  # device name -> its own jump host
        self.keep = keep
        self.on_result = on_result
        self.cancel_event = cancel or threading.Event()
        self.workers = max(1, min(workers, MAX_WORKERS))
        self.connect_timeout = connect_timeout
        self.command_timeout = command_timeout
        self.allow_new_host_key = allow_new_host_key
        values = [v for c in self.logins.values() for v in (c.password, c.enable or "")]
        self.redactor = redactor.with_values(values)
        self._lock = threading.Lock()
        self.results: list[DriftResult] = []
        self.history = history
        self.actor = actor
        self.job_id: str | None = None

    def cancel(self) -> None:
        self.cancel_event.set()

    def run(self) -> DriftReport:
        self._start_job()
        stop = threading.Event()
        beat = threading.Thread(target=self._heartbeat, args=(stop,), daemon=True)
        if self.job_id is not None:
            beat.start()
        try:
            with ThreadPoolExecutor(max_workers=self.workers) as pool:
                list(pool.map(self._check, self.devices))
        finally:
            stop.set()
            if beat.is_alive():
                beat.join(timeout=5)
        report = DriftReport(list(self.results), self.cancel_event.is_set())
        self._finish_job(report)
        return report

    # -- history ------------------------------------------------------------------------

    def _record(self, action: Callable[[], Any]) -> None:
        """A history write. A failure is logged and never stops the check."""
        if self.history is None or self.job_id is None:
            return
        try:
            action()
        except Exception:  # noqa: BLE001
            log.warning("cannot record drift check job %s", self.job_id, exc_info=True)

    def _start_job(self) -> None:
        if self.history is None:
            return
        try:
            self.job_id = self.history.create_job(
                actor=self.actor,
                play_path=DRIFT_JOB,
                play_hash="",
                customer=self.customer,
                mode=DRIFT_MODE,
                status=JobStatus.RUNNING,
                limit=[d.name for d in self.devices],
            )
        except Exception:  # noqa: BLE001
            log.warning("cannot record a drift check job", exc_info=True)
            return
        names = [d.name for d in self.devices]
        self._record(lambda: self.history.update_job(self.job_id, started_at=_now()))
        self._record(lambda: self.history.add_devices(self.job_id, names))

    def _heartbeat(self, stop: threading.Event) -> None:
        while not stop.wait(HEARTBEAT_SECONDS):
            self._record(lambda: self.history.heartbeat(self.job_id))

    def _record_result(self, result: DriftResult) -> None:
        fields: dict[str, Any] = {
            "status": device_status(result.status),
            "finished_at": _now(),
            "output_redacted": result.text,
        }
        if result.diff:
            fields["diff_redacted"] = result.diff
        if result.backup_path is not None:
            fields["backup_path"] = str(result.backup_path)
        if result.status not in READ and result.detail:
            fields["error_redacted"] = result.detail
        self._record(lambda: self.history.update_device(self.job_id, result.device, **fields))

    def _finish_job(self, report: DriftReport) -> None:
        read = sum(1 for r in report.results if r.status in READ)
        if report.cancelled:
            status = JobStatus.CANCELLED
        elif read == len(report.results):
            status = JobStatus.COMPLETED
        elif read == 0:
            status = JobStatus.FAILED
        else:
            status = JobStatus.PARTIAL
        counts = dict(Counter(str(r.status) for r in report.results))
        self._record(
            lambda: self.history.set_job_status(
                self.job_id, status, finished_at=_now(), summary={"counts": counts}
            )
        )

    def _emit(self, result: DriftResult) -> None:
        with self._lock:
            self.results.append(result)
        if self.on_result is not None:
            self.on_result(result)

    def _check(self, device: DriftDevice) -> None:
        if not self.cancel_event.is_set():
            self._record(
                lambda: self.history.update_device(
                    self.job_id, device.name, status=DeviceStatus.CONNECTING, started_at=_now()
                )
            )
        result = self._read(device)
        self._record_result(result)
        self._emit(result)

    def _read(self, device: DriftDevice) -> DriftResult:
        def result(status: DriftStatus, detail: str = "") -> DriftResult:
            return DriftResult(device.name, device.host, status, detail)

        if self.cancel_event.is_set():
            return result(DriftStatus.CANCELLED)
        login = self.logins.get(device.login)
        if login is None:
            return result(DriftStatus.NO_LOGIN, f"login {device.login} is not stored")
        target = ConnectTarget(
            customer=self.customer,
            device=device.name,
            host=device.host,
            port=device.port,
            platform=device.platform,
            method=ConnectionMethod.SSH,
            credential=login,
            jump=self.jumps.get(device.name, self.jump),
            connect_timeout=self.connect_timeout,
            command_timeout=self.command_timeout,
            allow_new_host_key=self.allow_new_host_key,
            cancel=self.cancel_event,
        )
        try:
            session = self.transport.open(target)
        except ConnectCancelled:
            return result(DriftStatus.CANCELLED)
        except HostKeyMismatchError as exc:
            return result(DriftStatus.HOSTKEY_CHANGED, self._text(exc))
        except AuthenticationError as exc:
            return result(DriftStatus.LOGIN_FAILED, self._text(exc))
        except UnreachableError as exc:
            return result(DriftStatus.UNREACHABLE, self._text(exc))
        except ConnectError as exc:
            return result(DriftStatus.FAILED, self._text(exc))
        try:
            running = session.running_config()
        except Exception as exc:  # noqa: BLE001 - one device failing never stops the run
            return result(DriftStatus.FAILED, redact_exception(exc, self.redactor))
        finally:
            try:
                session.close()
            except Exception:  # noqa: BLE001, S110 - closing is best effort
                pass
        try:
            saved = save_if_changed(
                self.paths,
                self.customer,
                device.name,
                running,
                PLATFORM_FAMILY[device.platform],
                self.redactor,
                tag=DRIFT_TAG,
                keep=self.keep,
            )
        except (BackupError, OSError) as exc:
            return result(DriftStatus.FAILED, f"backup not saved: {self._text(exc)}")
        previous = saved.previous.taken_at if saved.previous else None
        detail = {
            Change.FIRST: "saved as the first backup",
            Change.CHANGED: "saved a new backup",
            Change.SECRETS: "only secret values differ; saved a new backup",
            Change.UNCHANGED: "",
        }[saved.change]
        return DriftResult(
            device.name,
            device.host,
            _FROM_CHANGE[saved.change],
            detail,
            previous,
            saved.added,
            saved.removed,
            saved.diff,
            saved.path,
        )

    def _text(self, exc: BaseException) -> str:
        return self.redactor.text(str(exc))


def _now() -> datetime:
    return datetime.now(UTC)
