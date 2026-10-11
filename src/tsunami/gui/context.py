"""Application state shared by every view: paths, settings, history, secrets, workspace.

Views read from here and listen to its signals; they never construct these objects
themselves. Workspace loading (which includes validation renders) runs off the UI
thread.

See design.md, GUI; Architecture: Process model.
"""

from __future__ import annotations

import getpass
from collections.abc import Callable
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, QRunnable, QSettings, QThreadPool, Signal

from tsunami.core.models.jobs import ALARM_DEVICE_STATUSES
from tsunami.core.models.settings import AppSettings, save_settings
from tsunami.core.paths import AppPaths
from tsunami.core.runner import Runner
from tsunami.core.workspace import Workspace, load_workspace


class _LoadSignals(QObject):
    done = Signal(object, object)  # (Path, Workspace | Exception)


class _LoadTask(QRunnable):
    def __init__(self, path: Path, signals: _LoadSignals) -> None:
        super().__init__()
        self.path = path
        self.signals = signals

    def run(self) -> None:
        try:
            result: Any = load_workspace(self.path)
        except Exception as exc:  # noqa: BLE001 - reported to the UI
            result = exc
        self.signals.done.emit(self.path, result)


class AppContext(QObject):
    workspace_changed = Signal()
    workspace_loading = Signal(bool)
    settings_changed = Signal()
    jobs_changed = Signal()
    alarms_changed = Signal()

    def __init__(
        self,
        paths: AppPaths,
        settings: AppSettings,
        history: Any,  # tsunami.core.history.History
        secrets: Any,  # tsunami.core.secret_store.SecretStore
        *,
        transport_factory: Callable[[], Any] | None = None,
        qsettings: QSettings | None = None,
    ) -> None:
        super().__init__()
        self.paths = paths
        self.settings = settings
        self.history = history
        self.secrets = secrets
        self.workspace: Workspace | None = None
        self.workspace_path: Path | None = None
        self.last_error: str | None = None
        self._transport_factory = transport_factory
        self.qsettings = qsettings or QSettings("Tsunami", "Tsunami")
        self._load_signals = _LoadSignals()
        self._load_signals.done.connect(self._loaded)

    # -- workspace ---------------------------------------------------------------

    def open_workspace(self, path: Path | str, *, block: bool = False) -> None:
        path = Path(path)
        self.workspace_loading.emit(True)
        if block:
            try:
                result: Any = load_workspace(path)
            except Exception as exc:  # noqa: BLE001
                result = exc
            self._loaded(path, result)
            return
        QThreadPool.globalInstance().start(_LoadTask(path, self._load_signals))

    def reload_workspace(self, *, block: bool = False) -> None:
        if self.workspace_path is not None:
            self.open_workspace(self.workspace_path, block=block)

    def _loaded(self, path: Path, result: Any) -> None:
        self.workspace_loading.emit(False)
        if isinstance(result, Exception):
            self.last_error = f"{type(result).__name__}: {result}"
            self.workspace_changed.emit()
            return
        self.last_error = None
        self.workspace, self.workspace_path = result, path
        if self.settings.workspace != str(path):
            self.save_settings(self.settings.model_copy(update={"workspace": str(path)}))
        self.workspace_changed.emit()

    # -- settings --------------------------------------------------------------

    def save_settings(self, settings: AppSettings) -> None:
        save_settings(settings, self.paths.settings_file)
        self.settings = settings
        self.settings_changed.emit()

    # -- runs ------------------------------------------------------------------

    def make_runner(self, **kwargs: Any) -> Runner:
        """A Runner for this workspace; kwargs are on_event, confirm_canary, cancel."""
        if self.workspace is None:
            raise RuntimeError("no workspace is open")
        transport = self._transport_factory() if self._transport_factory else self._transport()
        return Runner(
            self.workspace,
            transport=transport,
            history=self.history,
            secrets=self.secrets,
            paths=self.paths,
            app_settings=self.settings,
            **kwargs,
        )

    def _transport(self) -> Any:
        from tsunami.core.transports import DeviceTransport

        return DeviceTransport(self.paths, self.settings)

    @staticmethod
    def windows_user() -> str:
        return getpass.getuser()

    # -- alarms (rollback_failed / interrupted) -----------------------------------

    def unacknowledged_alarms(self, limit: int = 200) -> list[str]:
        """Recent job ids with a device that may be half-configured, not yet acknowledged."""
        acknowledged = set(self.qsettings.value("alarms/acknowledged", [], list) or [])
        alarms = []
        for job in self.history.list_jobs(limit=limit):
            if job.id in acknowledged:
                continue
            if any(d.status in ALARM_DEVICE_STATUSES for d in self.history.devices(job.id)):
                alarms.append(job.id)
        return alarms

    def acknowledge_alarm(self, job_id: str) -> None:
        acknowledged = list(self.qsettings.value("alarms/acknowledged", [], list) or [])
        if job_id not in acknowledged:
            acknowledged.append(job_id)
            self.qsettings.setValue("alarms/acknowledged", acknowledged[-1000:])
        self.alarms_changed.emit()
