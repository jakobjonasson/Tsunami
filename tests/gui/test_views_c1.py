"""Approvals and Drafts views (work package 4.2C1)."""

from __future__ import annotations

import pytest
from PySide6.QtWidgets import QMessageBox
from tests.gui.conftest import TACACS_SECRET, all_widget_text

from tsunami.core.models.jobs import Actor, DeviceStatus, JobStatus
from tsunami.core.render.renderer import Renderer
from tsunami.core.runner import play_hash
from tsunami.core.services import collect_prompts, play_entry
from tsunami.gui.screens.approvals import ApprovalCard, ApprovalsView
from tsunami.gui.screens.drafts import DraftsView, DraftView

PUSH = "plays/push/add-vlan.yml"
BASELINE = "plays/baseline/acme-baseline.yml"


@pytest.fixture(autouse=True)
def answer_yes(monkeypatch):
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)


def pending(ctx, play=PUSH, prompts=None):
    prompts = {"vlan_id": "30", "vlan_name": "Cameras"} if prompts is None else prompts
    entry = play_entry(ctx.workspace, play)
    job_id = ctx.history.create_job(
        actor=Actor.MCP,
        play_path=play,
        play_hash=play_hash(ctx.workspace_path / play),
        customer="acme",
        mode="apply",
        status=JobStatus.AWAITING_APPROVAL,
        prompts=prompts,
    )
    ctx.history.add_devices(job_id, ["sa-as01", "sa-as02"])
    renderer = Renderer(ctx.workspace)
    for device in ("sa-as01", "sa-as02"):
        values = collect_prompts(ctx.workspace, entry, prompts, device=device)
        rendering = renderer.render(entry, device=device, prompt_values=values)
        ctx.history.update_device(
            job_id,
            device,
            render_hash=rendering.render_hash(),
            render_masked=rendering.masked_text(),
        )
    return job_id


def side_view(window, cls):
    return next(
        found for page in window._views.values() if (found := page.findChild(cls)) is not None
    )


def test_approvals_sidebar_lists_pending(main_window, gui_ctx):
    view = side_view(main_window, ApprovalsView)
    assert view.list.count() == 0
    assert view.empty.isVisibleTo(view)
    pending(gui_ctx)
    gui_ctx.approvals_changed.emit(1)
    assert view.list.count() == 1
    text = view.list.item(0).text()
    assert "add-vlan" in text and "acme" in text and "2 device" in text


def test_card_shows_device_tabs_with_masked_renders(main_window, gui_ctx):
    job_id = pending(gui_ctx)
    view = side_view(main_window, ApprovalsView)
    card = view.open_request(job_id)
    assert isinstance(card, ApprovalCard)
    assert card.device_tabs.count() == 2
    assert "vlan 30" in all_widget_text(card)


def test_approve_runs_to_completion(main_window, gui_ctx, qtbot):
    job_id = pending(gui_ctx)
    card = side_view(main_window, ApprovalsView).open_request(job_id)
    card.approve_button.click()
    qtbot.waitUntil(
        lambda: gui_ctx.history.get_job(job_id).status == JobStatus.COMPLETED, timeout=20000
    )
    assert {d.status for d in gui_ctx.history.devices(job_id)} == {DeviceStatus.SUCCESS}
    assert f"approval:{job_id}" not in main_window._tabs


def test_deny_sets_denied(main_window, gui_ctx):
    job_id = pending(gui_ctx)
    card = side_view(main_window, ApprovalsView).open_request(job_id)
    card.deny_button.click()
    assert gui_ctx.history.get_job(job_id).status == JobStatus.DENIED
    assert f"approval:{job_id}" not in main_window._tabs
    assert side_view(main_window, ApprovalsView).list.count() == 0


def test_decided_request_disables_buttons(main_window, gui_ctx):
    job_id = pending(gui_ctx)
    gui_ctx.history.set_job_status(job_id, JobStatus.DENIED)
    card = ApprovalCard(gui_ctx, main_window, job_id)
    assert not card.approve_button.isEnabled()
    assert not card.deny_button.isEnabled()
    assert "denied" in card.status_label.text()


def test_baseline_card_never_shows_secret(main_window, gui_ctx):
    job_id = pending(gui_ctx, BASELINE, prompts={})
    card = side_view(main_window, ApprovalsView).open_request(job_id)
    text = all_widget_text(card)
    assert TACACS_SECRET not in text
    assert "key" in text


def write_draft(root, rel="drafts/plays/push/new-play.yml"):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "name: New play\ntype: push\ncustomer: acme\ntargets:\n  roles: [access]\n"
        "steps:\n  - config:\n      - tacacs-server key {{ secret('acme/tacacs') }}\n"
        "  - exec: reload\n",
        encoding="utf-8",
    )
    return path


def test_drafts_lists_and_highlights(main_window, gui_ctx, workspace_root):
    write_draft(workspace_root)
    gui_ctx.reload_workspace(block=True)
    view = side_view(main_window, DraftsView)
    labels = [view.list.item(i).text() for i in range(view.list.count())]
    assert any("push/new-play.yml" in label and "new" in label for label in labels)
    draft_item = next(
        view.list.item(i) for i in range(view.list.count()) if view.list.item(i).data(256)
    )
    tab = view.open_draft(draft_item.data(256))
    assert isinstance(tab, DraftView)
    assert any(kind == "secret" for _, kind, _ in tab.found)
    assert any(kind == "destructive" for _, kind, _ in tab.found)
    assert "secret() call" in tab.summary.text()
    assert "destructive command" in tab.summary.text()
    assert "+name: New play" in tab.diff_view.toPlainText()


def test_promote_moves_draft_and_refreshes(main_window, gui_ctx, workspace_root, qtbot):
    write_draft(workspace_root)
    gui_ctx.reload_workspace(block=True)
    view = side_view(main_window, DraftsView)
    draft = next(
        view.list.item(i).data(256) for i in range(view.list.count()) if view.list.item(i).data(256)
    )
    tab = view.open_draft(draft)
    tab.promote_button.click()
    assert (workspace_root / "plays/push/new-play.yml").is_file()
    assert not (workspace_root / "drafts/plays/push/new-play.yml").exists()
    qtbot.waitUntil(lambda: view.list.count() == 0, timeout=20000)
    assert "draft:drafts/plays/push/new-play.yml" not in main_window._tabs
