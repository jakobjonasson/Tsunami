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


def test_app_icon_has_every_size(qapp):
    from tsunami.gui import icons

    sizes = sorted(s.width() for s in icons.app_icon().availableSizes())
    assert sizes == [16, 24, 32, 48, 64, 256]


def test_refresh_button_picks_up_new_devices(main_window, gui_ctx, workspace_root, qtbot):
    from PySide6.QtWidgets import QToolButton

    devices = workspace_root / "inventory/customers/acme/devices.yml"
    devices.write_text(
        devices.read_text(encoding="utf-8")
        + "- name: sa-as03\n  host: 203.0.113.13\n  site: site-a\n  role: access\n"
        "  platform: ios-xe\n",
        encoding="utf-8",
    )
    assert gui_ctx.workspace.customers["acme"].device("sa-as03") is None
    buttons = main_window.findChildren(QToolButton, "SideBarAction")
    assert buttons and "F5" in buttons[0].toolTip()
    buttons[0].click()
    qtbot.waitUntil(
        lambda: (
            gui_ctx.workspace is not None
            and gui_ctx.workspace.customers["acme"].device("sa-as03") is not None
        ),
        timeout=10_000,
    )
