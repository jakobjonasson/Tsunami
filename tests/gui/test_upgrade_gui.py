"""Upgrade plays in the GUI: the run wizard and the approval card say that an activate
reloads the devices, and show the image line of the render as plain text."""

from __future__ import annotations

import pytest
from PySide6.QtWidgets import QMessageBox
from tests.gui.test_views_c1 import pending, side_view

from tsunami.core.models.play import RunMode
from tsunami.gui.screens.approvals import ApprovalCard, ApprovalsView
from tsunami.gui.screens.run_wizard import RELOAD_WARNING, RunWizard, open_run_wizard

UPGRADE = "plays/upgrade/ios-17.yml"
FILE = "c1100-universalk9.17.09.05.SPA.bin"
SHA256 = "ab" * 32

PLAY = f"""\
name: Upgrade <b>access</b> switches
type: upgrade
customer: acme
targets:
  roles: [access]
firmware:
  images:
    - file: {FILE}
      version: 17.09.05
      sha256: "{SHA256}"
      platforms: [ios-xe]
steps:
  - firmware: stage
    tags: [stage]
  - firmware: activate
    allow_destructive: true
    tags: [activate]
"""


@pytest.fixture
def upgrade(gui_ctx, workspace_root):
    path = workspace_root / UPGRADE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(PLAY, encoding="utf-8")
    gui_ctx.reload_workspace(block=True)
    return UPGRADE


@pytest.fixture
def asked(monkeypatch):
    seen: list[str] = []
    monkeypatch.setattr(
        QMessageBox, "question", lambda *a, **k: seen.append(a[2]) or QMessageBox.StandardButton.No
    )
    return seen


def _wizard(main_window, gui_ctx, path, tags=()) -> RunWizard:
    wizard = open_run_wizard(gui_ctx, main_window, path)
    assert isinstance(wizard, RunWizard)
    for tag in tags:
        wizard.tag_boxes[tag].setChecked(True)
    wizard.set_mode(RunMode.APPLY)
    assert wizard.preview()
    return wizard


def test_the_preview_shows_the_image_pinned_by_hash(main_window, gui_ctx, upgrade):
    wizard = _wizard(main_window, gui_ctx, upgrade)
    text = wizard.preview_tabs.widget(0).toPlainText()
    assert f"stage {FILE} version 17.09.05 sha256:{SHA256}" in text


@pytest.mark.parametrize("tags", [(), ("activate",), ("stage", "activate")])
def test_apply_that_reaches_activate_warns_of_the_reload(
    main_window, gui_ctx, upgrade, asked, monkeypatch, tags
):
    started: list[object] = []
    monkeypatch.setattr(main_window, "start_run", lambda *a: started.append(a))
    wizard = _wizard(main_window, gui_ctx, upgrade, tags)
    wizard.start_button.click()
    assert asked and RELOAD_WARNING in asked[0]
    assert not started


def test_a_stage_only_run_does_not_warn(main_window, gui_ctx, upgrade, asked):
    wizard = _wizard(main_window, gui_ctx, upgrade, ("stage",))
    wizard.start_button.click()
    assert asked and RELOAD_WARNING not in asked[0]


def test_a_push_play_does_not_warn(main_window, gui_ctx, asked):
    wizard = open_run_wizard(gui_ctx, main_window, "plays/push/add-vlan.yml")
    wizard.fields["vlan_id"].set_value(30)
    wizard.fields["vlan_name"].set_value("Cameras")
    wizard.set_mode(RunMode.APPLY)
    assert wizard.preview()
    wizard.start_button.click()
    assert asked and RELOAD_WARNING not in asked[0]


def test_approval_card_warns_when_the_request_reaches_activate(
    main_window, gui_ctx, upgrade, asked
):
    job_id = pending(gui_ctx, upgrade, prompts={})
    card = side_view(main_window, ApprovalsView).open_request(job_id)
    assert isinstance(card, ApprovalCard)
    assert card.reloads and card.reload_label.isVisibleTo(card)
    assert card.reload_label.text() == RELOAD_WARNING
    card.approve_button.click()
    assert asked and RELOAD_WARNING in asked[0]


def test_approval_card_for_a_stage_request_does_not_warn(main_window, gui_ctx, upgrade):
    job_id = pending(gui_ctx, upgrade, prompts={}, tags=["stage"])
    card = ApprovalCard(gui_ctx, main_window, job_id)
    assert not card.reloads and not card.reload_label.isVisibleTo(card)


def test_approval_card_for_a_push_does_not_warn(main_window, gui_ctx):
    card = ApprovalCard(gui_ctx, main_window, pending(gui_ctx))
    assert not card.reloads and not card.reload_label.isVisibleTo(card)
