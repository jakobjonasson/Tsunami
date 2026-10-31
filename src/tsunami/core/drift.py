"""Back up a site's devices and report config drift since their last backup.

Each device is logged in to with its own login (device, else site default, else customer)
and its running config read. A device without a backup gets its first one. Otherwise the
config is compared with the newest backup, whoever took it (a job, discovery or an earlier
drift check); when it differs a new backup is saved and the redacted diff reported
(backups.save_if_changed). Nothing is sent to the device but `show running-config`, and
the inventory is not touched.

Device output is untrusted; the diff reaches the GUI already redacted, as text.

See design.md, Backups: Drift check.
"""

from __future__ import annotations

import threading
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from tsunami.core.backups import DRIFT_TAG, BackupError, Change, save_if_changed
from tsunami.core.issues import TsunamiError
from tsunami.core.models.common import PLATFORM_FAMILY, Platform, require_slug
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

MAX_WORKERS = 10  # matches the jump host channel cap, as in a run


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
        keep: int = 30,
        on_result: Callable[[DriftResult], None] | None = None,
        cancel: threading.Event | None = None,
        workers: int = MAX_WORKERS,
        connect_timeout: float = 15,
        command_timeout: float = 60,
        allow_new_host_key: bool = True,
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
        self.jump = jump
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

    def cancel(self) -> None:
        self.cancel_event.set()

    def run(self) -> DriftReport:
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            list(pool.map(self._check, self.devices))
        return DriftReport(list(self.results), self.cancel_event.is_set())

    def _emit(self, result: DriftResult) -> None:
        with self._lock:
            self.results.append(result)
        if self.on_result is not None:
            self.on_result(result)

    def _check(self, device: DriftDevice) -> None:
        self._emit(self._read(device))

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
            jump=self.jump,
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
        )

    def _text(self, exc: BaseException) -> str:
        return self.redactor.text(str(exc))
