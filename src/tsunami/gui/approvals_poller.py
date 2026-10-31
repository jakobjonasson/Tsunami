"""Polls history.db every 2 seconds for MCP change requests awaiting approval.

Also expires stale requests and marks jobs whose process died as interrupted.

See design.md, MCP interface: Approval flow; Architecture: Process model.
"""

from __future__ import annotations

import logging
from typing import Any

from PySide6.QtCore import QObject, QTimer, Signal

from tsunami.core.models.jobs import JobStatus
from tsunami.core.models.proposals import ProposalStatus

log = logging.getLogger(__name__)
POLL_MS = 2000


class ApprovalsPoller(QObject):
    pending_changed = Signal(int)
    new_request = Signal(str)  # job id

    def __init__(self, ctx: Any, *, expiry_minutes: int = 60, interval_ms: int = POLL_MS) -> None:
        super().__init__()
        self.ctx = ctx
        self.expiry_minutes = expiry_minutes
        self._seen: set[str] = set()
        self._count = -1
        self._timer = QTimer(self)
        self._timer.setInterval(interval_ms)
        self._timer.timeout.connect(self.poll)

    def start(self) -> None:
        self.poll()
        self._timer.start()

    def stop(self) -> None:
        self._timer.stop()

    def poll(self) -> None:
        history = self.ctx.history
        try:
            changed = bool(history.expire_approvals(older_than_minutes=self.expiry_minutes))
            changed |= bool(
                history.expire_inventory_proposals(older_than_minutes=self.expiry_minutes)
            )
            changed |= bool(history.mark_interrupted())
            # Run requests and inventory proposals wait in the same list.
            pending = [
                j.id for j in history.list_jobs(status=JobStatus.AWAITING_APPROVAL, limit=500)
            ] + [
                p.id
                for p in history.list_inventory_proposals(
                    status=ProposalStatus.AWAITING_APPROVAL, limit=500
                )
            ]
        except Exception:  # noqa: BLE001 - a busy database must not break the UI
            log.warning("approvals poll failed", exc_info=True)
            return
        for request_id in pending:
            if request_id not in self._seen:
                self._seen.add(request_id)
                self.new_request.emit(request_id)
                self.ctx.approvals_changed.emit(len(pending))
        if changed:
            self.ctx.jobs_changed.emit()
            self.ctx.alarms_changed.emit()
        if len(pending) != self._count:
            self._count = len(pending)
            self.pending_changed.emit(self._count)
            self.ctx.approvals_changed.emit(self._count)
