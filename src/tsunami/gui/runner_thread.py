"""QThread adapter: runner events to Qt signals, UI updates throttled to 100 ms.

The runner and its device workers never touch widgets. They put events on a queue;
a timer in the GUI thread drains it every 100 ms and emits one batch. The canary
question goes the same way, and the worker waits for the answer.

See design.md, Architecture: Process model.
"""

from __future__ import annotations

import logging
import queue
import threading
from typing import Any

from PySide6.QtCore import QThread, QTimer, Signal

from tsunami.core.redact import redact_exception
from tsunami.core.runner import RunRequest

log = logging.getLogger(__name__)
THROTTLE_MS = 100


class RunnerThread(QThread):
    events = Signal(list)  # list[Event], already redacted
    canary_requested = Signal(str)  # device; answer with answer_canary()
    job_started = Signal(str)  # job id, once known
    job_finished = Signal(str)  # job id
    run_failed = Signal(str)  # unexpected error, redacted

    def __init__(self, ctx: Any, request: RunRequest, parent: Any = None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self.request = request
        self.job_id: str | None = None
        self._queue: queue.SimpleQueue[tuple[str, Any]] = queue.SimpleQueue()
        self._answers: queue.SimpleQueue[bool] = queue.SimpleQueue()
        self._cancel = threading.Event()
        self._timer = QTimer(self)
        self._timer.setInterval(THROTTLE_MS)
        self._timer.timeout.connect(self._drain)
        self.finished.connect(self._drain)  # deliver whatever is left

    def start(self, *args: Any) -> None:
        self._timer.start()
        super().start(*args)

    def run(self) -> None:  # worker thread
        try:
            runner = self.ctx.make_runner(
                on_event=self._on_event, confirm_canary=self._confirm, cancel=self._cancel
            )
            job_id = runner.run(self.request)
            self._queue.put(("finished", job_id))
        except Exception as exc:  # noqa: BLE001 - shown to the person, never raised into Qt
            log.exception("run failed")
            self._queue.put(("failed", redact_exception(exc)))

    def cancel(self) -> None:
        """Cooperative: in-flight devices finish their step, then stop and roll back."""
        self._cancel.set()
        self._answers.put(False)  # unblock a pending canary question

    def answer_canary(self, proceed: bool) -> None:
        self._answers.put(proceed)

    # -- worker side --------------------------------------------------------------

    def _on_event(self, event: Any) -> None:
        self._queue.put(("event", event))

    def _confirm(self, device: str) -> bool:
        self._queue.put(("canary", device))
        return self._answers.get()

    # -- GUI side --------------------------------------------------------------------

    def _drain(self) -> None:
        batch = []
        while True:
            try:
                kind, payload = self._queue.get_nowait()
            except queue.Empty:
                break
            if kind == "event":
                if self.job_id is None and getattr(payload, "job_id", None):
                    self.job_id = payload.job_id
                    self.job_started.emit(self.job_id)
                batch.append(payload)
            else:
                if batch:
                    self.events.emit(batch)
                    batch = []
                if kind == "canary":
                    self.canary_requested.emit(payload)
                elif kind == "finished":
                    self.job_id = payload
                    self.job_finished.emit(payload)
                    self.ctx.jobs_changed.emit()
                elif kind == "failed":
                    self.run_failed.emit(payload)
        if batch:
            self.events.emit(batch)
        if self.isFinished() and self._queue.empty():
            self._timer.stop()
