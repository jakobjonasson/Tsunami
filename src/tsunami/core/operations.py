"""Operations that are not play runs, recorded as jobs so History shows them: MCP show
commands, discovery, Check for unlearned neighbors, Trust host keys and connection tests.
Drift checks (core/drift.py) are recorded the same way, with their own code.

Each operation is a job with a fixed play path (its label), a mode from OPERATION_MODES and
one row per device, so the job view, Last job and Last contact work unchanged and no play
can be run again from it. History hides operations unless Show operations is ticked.

Every text stored here must already be redacted. A history write that fails is logged and
never stops the operation.

See design.md, Local data and audit: Operations in history.
"""

from __future__ import annotations

import logging
import threading
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime
from typing import Any

from tsunami.core.drift import DRIFT_MODE
from tsunami.core.models.jobs import Actor, DeviceStatus, JobStatus

log = logging.getLogger(__name__)

SHOW_MODE = "show"
DISCOVERY_MODE = "discovery"
NEIGHBORS_MODE = "neighbors"
HOSTKEYS_MODE = "hostkeys"
TEST_MODE = "test"
RESTORE_MODE = "restore"
# The play path each operation's job records, shown in History's Play column.
LABELS = {
    SHOW_MODE: "Show command",
    DISCOVERY_MODE: "Discovery",
    NEIGHBORS_MODE: "Check for unlearned neighbors",
    HOSTKEYS_MODE: "Trust host keys",
    TEST_MODE: "Test connection",
    RESTORE_MODE: "Restore from backup",
}
OPERATION_MODES = frozenset({DRIFT_MODE, *LABELS})
# Operations that change a device. History lists them with play runs instead of hiding
# them behind Show operations.
CHANGE_MODES = frozenset({RESTORE_MODE})
# A device row in one of these says the operation reached it and did what it was for.
_DONE = frozenset({DeviceStatus.SUCCESS, DeviceStatus.NO_CHANGES})


def is_operation(mode: str) -> bool:
    return mode in OPERATION_MODES


def listed_with_plays(mode: str) -> bool:
    """Whether History shows the job without Show operations: plays and restores."""
    return not is_operation(mode) or mode in CHANGE_MODES


def connect_status(exc: BaseException) -> DeviceStatus:
    """The device status for a connection that failed with exc."""
    from tsunami.core.transports.base import (
        AuthenticationError,
        ConnectCancelled,
        HostKeyMismatchError,
        UnreachableError,
    )

    if isinstance(exc, HostKeyMismatchError):
        return DeviceStatus.HOSTKEY_MISMATCH
    if isinstance(exc, ConnectCancelled):
        return DeviceStatus.CANCELLED
    if isinstance(exc, AuthenticationError):
        return DeviceStatus.FAILED
    if isinstance(exc, UnreachableError):
        return DeviceStatus.UNREACHABLE
    return DeviceStatus.FAILED


class OperationLog:
    """One operation's job. Rows can be added as devices turn up (discovery)."""

    def __init__(
        self,
        history: Any,
        *,
        mode: str,
        customer: str,
        actor: Actor = Actor.GUI,
        devices: Iterable[str] = (),
        summary: Mapping[str, Any] | None = None,
    ) -> None:
        self.history = history
        self.job_id: str | None = None
        self._rows: set[str] = set()
        self._lock = threading.Lock()  # devices may report from worker threads
        self._statuses: dict[str, DeviceStatus] = {}
        self._summary = dict(summary or {})
        if history is None:
            return
        try:
            self.job_id = history.create_job(
                actor=actor,
                play_path=LABELS.get(mode, mode),
                play_hash="",
                customer=customer,
                mode=mode,
                status=JobStatus.RUNNING,
            )
        except Exception:  # noqa: BLE001 - a history failure never stops the operation
            log.warning("cannot record a %s job", mode, exc_info=True)
            return
        self._write(lambda: history.update_job(self.job_id, started_at=_now()))
        self.add(devices)

    def add(self, devices: Iterable[str]) -> None:
        with self._lock:
            names = [d for d in devices if d not in self._rows]
            self._rows.update(names)
        if not names:
            return
        self._write(lambda: self.history.add_devices(self.job_id, names))

    def device(
        self,
        name: str,
        status: DeviceStatus,
        *,
        output: str | None = None,
        error: str | None = None,
        diff: str | None = None,
        backup_path: str | None = None,
    ) -> None:
        """Record one device's result. Every text must already be redacted."""
        self.add([name])
        with self._lock:
            self._statuses[name] = status
        fields: dict[str, Any] = {"status": status, "finished_at": _now()}
        for key, value in (
            ("output_redacted", output),
            ("error_redacted", error),
            ("diff_redacted", diff),
            ("backup_path", backup_path),
        ):
            if value:
                fields[key] = value
        self._write(lambda: self.history.update_device(self.job_id, name, **fields))

    def finish(self, *, cancelled: bool = False, **summary: Any) -> None:
        statuses = list(self._statuses.values())
        done = sum(1 for s in statuses if s in _DONE)
        if cancelled:
            status = JobStatus.CANCELLED
        elif statuses and done == len(statuses):
            status = JobStatus.COMPLETED
        elif not statuses:
            status = JobStatus.COMPLETED if not self._rows else JobStatus.FAILED
        elif done == 0:
            status = JobStatus.FAILED
        else:
            status = JobStatus.PARTIAL
        counts = dict(Counter(str(s) for s in statuses))
        body = {**self._summary, **summary, "counts": counts}
        self._write(
            lambda: self.history.set_job_status(
                self.job_id, status, finished_at=_now(), summary=body
            )
        )

    def _write(self, action: Callable[[], Any]) -> None:
        if self.history is None or self.job_id is None:
            return
        try:
            action()
        except Exception:  # noqa: BLE001
            log.warning("cannot record operation job %s", self.job_id, exc_info=True)


def _now() -> datetime:
    return datetime.now(UTC)


def record_host_keys(history: Any, customer: str, results: Iterable[Any], cancel: Any) -> None:
    """A finished Trust host keys run (core/hostkey_trust.py) as an operation in History."""
    from tsunami.core.hostkey_trust import Outcome

    statuses = {
        Outcome.RECORDED: DeviceStatus.SUCCESS,
        Outcome.KNOWN: DeviceStatus.SUCCESS,
        Outcome.CHANGED: DeviceStatus.HOSTKEY_MISMATCH,
        Outcome.UNREACHABLE: DeviceStatus.UNREACHABLE,
        Outcome.CANCELLED: DeviceStatus.CANCELLED,
    }
    record = OperationLog(history, mode=HOSTKEYS_MODE, customer=customer)
    for result in results:
        text = f"{result.outcome}: {result.host}:{result.port}"
        if result.fingerprint:
            text += f" {result.fingerprint}"
        if result.known_fingerprint:
            text += f" (on file: {result.known_fingerprint})"
        ok = result.outcome in (Outcome.RECORDED, Outcome.KNOWN)
        record.device(
            result.device,
            statuses.get(result.outcome, DeviceStatus.FAILED),
            output=text,
            error=None if ok else (result.detail or None),
        )
    record.finish(cancelled=bool(cancel is not None and cancel.is_set()))


def record_test(
    history: Any, customer: str, device: str, login: str, ok: bool, message: str, exc: Any = None
) -> None:
    """A Test connection (Credentials, or the device page) as an operation in History.
    message must already be redacted."""
    record = OperationLog(
        history, mode=TEST_MODE, customer=customer, devices=[device], summary={"login": login}
    )
    if ok:
        record.device(device, DeviceStatus.SUCCESS, output=message)
    else:
        status = connect_status(exc) if exc is not None else DeviceStatus.FAILED
        record.device(device, status, error=message)
    record.finish()
