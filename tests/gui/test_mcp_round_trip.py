"""An MCP change request approved or denied in the GUI (plan 5.5, design.md Approval flow).

The MCP side uses its own History connection to the same history.db, as the two
processes do in production.
"""

from __future__ import annotations

import pytest
from PySide6.QtWidgets import QMessageBox
from tests.gui.conftest import LOGIN_PASSWORD, TACACS_SECRET

from tsunami.core.history import History
from tsunami.core.models.jobs import DeviceStatus, JobStatus
from tsunami.core.models.policy import McpPolicy
from tsunami.core.models.settings import AppSettings, save_settings
from tsunami.core.policy import save_policy
from tsunami.gui.screens.approvals import ApprovalsView
from tsunami.mcp_server.enforcement import McpApp
from tsunami.mcp_server.tools import Tools

PLAY = "plays/push/add-vlan.yml"
PROMPTS = {"vlan_id": "30", "vlan_name": "Cameras"}


@pytest.fixture
def mcp(gui_ctx, app_paths, workspace_root, mock_transport, tmp_path, monkeypatch):
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    save_settings(AppSettings(workspace=str(workspace_root)), app_paths.settings_file)
    save_policy(McpPolicy(enabled=True), app_paths.policy_file)
    history = History(tmp_path / "history.db")
    app = McpApp(
        app_paths,
        history=history,
        secrets=gui_ctx.secrets,
        transport_factory=lambda: mock_transport,
    )
    yield app, Tools(app)
    for thread in app.threads:
        thread.join(timeout=20)
    history.close()


def request(app, tools) -> str:
    job_id = tools.request_run(PLAY, ["sa-as01", "sa-as02"], prompts=PROMPTS)["job_id"]
    for thread in app.threads:
        thread.join(timeout=20)
    assert tools.get_job(job_id)["status"] == "awaiting_approval"
    return job_id


def approvals_view(window) -> ApprovalsView:
    window.poller.start()  # app.main starts it once the window is shown
    return next(
        found
        for page in window._views.values()
        if (found := page.findChild(ApprovalsView)) is not None
    )


def test_request_approved_in_the_gui_runs_and_reports_back(
    mcp, main_window, gui_ctx, mock_transport, qtbot
):
    app, tools = mcp
    job_id = request(app, tools)
    view = approvals_view(main_window)
    qtbot.waitUntil(lambda: view.list.count() == 1, timeout=10_000)  # the 2 s poller

    card = view.open_request(job_id)
    card.approve_button.click()
    qtbot.waitUntil(
        lambda: gui_ctx.history.get_job(job_id).status == JobStatus.COMPLETED, timeout=20_000
    )

    result = tools.get_job(job_id)
    assert result["status"] == "completed" and result["approved_by"]
    assert {d["status"] for d in result["devices"]} == {str(DeviceStatus.SUCCESS)}
    assert LOGIN_PASSWORD not in str(result) and TACACS_SECRET not in str(result)
    assert "vlan 30" in mock_transport.device("sa-as01").running


def test_request_denied_in_the_gui(mcp, main_window, mock_transport, qtbot):
    app, tools = mcp
    job_id = request(app, tools)
    view = approvals_view(main_window)
    qtbot.waitUntil(lambda: view.list.count() == 1, timeout=10_000)

    view.open_request(job_id).deny_button.click()
    assert tools.get_job(job_id)["status"] == "denied"
    assert "vlan 30" not in mock_transport.device("sa-as01").running


def test_play_edited_after_the_request_fails_every_device(
    mcp, main_window, gui_ctx, workspace_root, mock_transport, qtbot
):
    app, tools = mcp
    job_id = request(app, tools)
    play = workspace_root / PLAY
    play.write_text(play.read_text(encoding="utf-8") + "# edited\n", encoding="utf-8")

    view = approvals_view(main_window)
    qtbot.waitUntil(lambda: view.list.count() == 1, timeout=10_000)
    view.open_request(job_id).approve_button.click()
    qtbot.waitUntil(
        lambda: gui_ctx.history.get_job(job_id).status in (JobStatus.FAILED, JobStatus.COMPLETED),
        timeout=20_000,
    )
    statuses = {d["status"] for d in tools.get_job(job_id)["devices"]}
    assert str(DeviceStatus.SUCCESS) not in statuses
    assert "vlan 30" not in mock_transport.device("sa-as01").running


# -- inventory proposals ------------------------------------------------------------------


def propose(app, tools, app_paths) -> str:
    save_policy(McpPolicy(enabled=True, inventory_edits=True), app_paths.policy_file)
    result = tools.propose_inventory_change(
        "acme",
        [{"device": "sa-as01", "role": "distribution", "host": "203.0.113.77"}],
        reason="the site survey says so",
    )
    return result["proposal_id"]


def devices_text(workspace_root) -> str:
    return (workspace_root / "inventory/customers/acme/devices.yml").read_text(encoding="utf-8")


def test_proposal_card_shows_source_and_before_after_and_approval_applies(
    mcp, main_window, gui_ctx, app_paths, workspace_root, qtbot
):
    app, tools = mcp
    before = devices_text(workspace_root)
    proposal_id = propose(app, tools, app_paths)
    view = approvals_view(main_window)
    qtbot.waitUntil(lambda: view.list.count() == 1, timeout=10_000)  # the 2 s poller
    assert devices_text(workspace_root) == before

    card = view.open_request(proposal_id)
    table = card.changes
    rows = [[table.item(r, c).text() for c in range(4)] for r in range(table.rowCount())]
    assert rows == [
        ["sa-as01", "role", "access", "distribution"],
        ["sa-as01", "host", "203.0.113.11", "203.0.113.77"],
    ]
    shown = " ".join(label.text() for label in card.findChildren(type(card.status_label)))
    assert "Claude, over MCP" in shown and "the site survey says so" in shown

    card.approve_button.click()
    assert tools.get_inventory_proposal(proposal_id)["status"] == "applied"
    assert "203.0.113.77" in devices_text(workspace_root)


def test_proposal_denied_in_the_gui_changes_nothing(
    mcp, main_window, app_paths, workspace_root, qtbot
):
    app, tools = mcp
    before = devices_text(workspace_root)
    proposal_id = propose(app, tools, app_paths)
    view = approvals_view(main_window)
    qtbot.waitUntil(lambda: view.list.count() == 1, timeout=10_000)

    view.open_request(proposal_id).deny_button.click()
    assert tools.get_inventory_proposal(proposal_id)["status"] == "denied"
    assert devices_text(workspace_root) == before
    qtbot.waitUntil(lambda: view.list.count() == 0, timeout=10_000)
