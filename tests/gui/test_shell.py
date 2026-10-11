"""Shell smoke tests: the window builds with every view and runs a play end to end."""

from __future__ import annotations

from tsunami.core.models.jobs import DeviceStatus, JobStatus
from tsunami.core.models.play import RunMode
from tsunami.core.runner import RunRequest


def test_window_builds_and_loads_workspace(main_window, gui_ctx):
    assert "Example network workspace" in main_window.windowTitle()
    assert main_window.status_issues.text().startswith("0 errors")


def test_start_run_completes_in_the_background(main_window, gui_ctx, qtbot, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    request = RunRequest(
        "plays/push/add-vlan.yml", RunMode.APPLY, prompts={"vlan_id": "30", "vlan_name": "Cameras"}
    )
    thread = main_window.start_run(request, "Add VLAN")
    finished = []
    thread.job_finished.connect(finished.append)
    qtbot.waitUntil(lambda: bool(finished), timeout=20000)
    job = gui_ctx.history.get_job(finished[0])
    assert job.status == JobStatus.COMPLETED
    assert {d.status for d in gui_ctx.history.devices(job.id)} == {DeviceStatus.SUCCESS}


def test_command_palette_lists_commands(main_window):
    assert "workspace.reload" in main_window._commands
    assert "palette.show" in main_window._commands
