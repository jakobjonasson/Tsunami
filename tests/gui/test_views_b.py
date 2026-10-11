"""Run wizard, job view and their widgets (work package 4.2B)."""

from __future__ import annotations

import pytest
from PySide6.QtWidgets import QApplication, QMessageBox
from tests.gui.conftest import LOGIN_PASSWORD, TACACS_SECRET, all_widget_text

from tsunami.core.models.common import Platform
from tsunami.core.models.jobs import Actor, DeviceStatus, JobStatus
from tsunami.core.models.play import Prompt, PromptType, RunMode
from tsunami.core.prompts import PromptContext
from tsunami.core.runner import RunRequest
from tsunami.gui.screens import job_view
from tsunami.gui.screens.job_view import JobView, make_job_view
from tsunami.gui.screens.run_wizard import RunWizard, open_run_wizard
from tsunami.gui.widgets.diff_view import DiffView
from tsunami.gui.widgets.port_picker import PortPicker
from tsunami.gui.widgets.prompt_widgets import make_prompt_widget

CONTEXT = PromptContext(frozenset({"site-a"}), frozenset({"access"}), frozenset(), None)


@pytest.fixture(autouse=True)
def no_dialogs(monkeypatch):
    yes = QMessageBox.StandardButton.Yes
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: yes)
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: yes)
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: yes)


def _wait_done(qtbot, view: JobView) -> None:
    qtbot.waitUntil(lambda: view._finished and view.job_id is not None, timeout=20000)


def _job_view(main_window) -> JobView:
    return main_window.findChildren(JobView)[0]


# -- widgets ------------------------------------------------------------------------


def test_vlan_spin_box_range(gui_ctx):
    field = make_prompt_widget("v", Prompt(type=PromptType.VLAN), CONTEXT)
    assert (field.editor.minimum(), field.editor.maximum()) == (1, 4094)
    assert field.raw_value() is None  # untouched: never silently submits VLAN 1
    field.set_value(30)
    assert field.raw_value() == 30


def test_int_defaults_help_and_error(gui_ctx):
    prompt = Prompt(type=PromptType.INT, min=2, max=9, default=5, help="How many")
    field = make_prompt_widget("n", prompt, CONTEXT)
    assert (field.editor.minimum(), field.editor.maximum(), field.raw_value()) == (2, 9, 5)
    assert field.toolTip() == "How many"
    field.set_error("too big")
    assert field.has_error() and field.error_text == "too big"
    field.set_error(None)
    assert not field.has_error()


def test_line_edit_error_marks_invalid(gui_ctx):
    field = make_prompt_widget("ip", Prompt(type=PromptType.IPV4_INTERFACE), CONTEXT)
    field.set_value("10.1.1.0/24")  # the network address: invalid
    field.set_error("address is the network address")
    assert field.editor.property("invalid") is True
    assert field.has_error()
    field.set_error(None)
    assert field.editor.property("invalid") is False


def test_model_combo_groups_by_vendor(gui_ctx):
    field = make_prompt_widget("model", Prompt(type=PromptType.MODEL), CONTEXT)
    combo = field.editor
    model = combo.model()
    headers = [
        combo.itemText(i)
        for i in range(combo.count())
        if not model.item(i).isEnabled() and combo.itemData(i) is None
    ]
    assert headers == ["Cisco", "Aruba"]
    cisco = [i for i in range(combo.count()) if str(combo.itemData(i)).startswith("cisco-")]
    assert cisco and all(model.item(i).isEnabled() for i in cisco)
    field.set_value("cisco-c9300")
    assert field.raw_value() == "cisco-c9300"

    only_aruba = make_prompt_widget(
        "model", Prompt(type=PromptType.MODEL), CONTEXT, platforms={Platform.AOS_CX}
    )
    texts = [only_aruba.editor.itemText(i) for i in range(only_aruba.editor.count())]
    assert "Cisco" not in texts and "Aruba" in texts


def test_choice_bool_and_site_widgets(gui_ctx):
    choice = make_prompt_widget("c", Prompt(type=PromptType.CHOICE, choices=["a", "b"]), CONTEXT)
    choice.set_value("b")
    assert choice.raw_value() == "b"
    flag = make_prompt_widget("f", Prompt(type=PromptType.BOOL, default=True), CONTEXT)
    assert flag.raw_value() is True
    site = make_prompt_widget("s", Prompt(type=PromptType.SITE), CONTEXT)
    site.set_value("site-a")
    assert site.raw_value() == "site-a"


def test_port_picker_refreshes_on_popup(qtbot):
    ports = [["COM3"]]
    picker = PortPicker(provider=lambda: ports[0])
    qtbot.addWidget(picker)
    assert picker.port() == "COM3"
    ports[0] = ["COM3", "COM7"]
    picker.showPopup()
    picker.hidePopup()
    assert [picker.itemText(i) for i in range(picker.count())] == ["COM3", "COM7"]


def test_diff_view_colors_added_and_removed(gui_ctx, qtbot):
    view = DiffView("+ vlan 30\n- ntp server 1.1.1.1\n  context")
    qtbot.addWidget(view)
    assert view.isReadOnly()
    formats = view.document().firstBlock().layout().formats()
    assert formats and formats[0].format.foreground().color().name() != ""
    assert view.document().firstBlock().next().layout().formats()
    assert not view.document().lastBlock().layout().formats()


# -- run wizard ---------------------------------------------------------------------


def _open(main_window, gui_ctx, path) -> RunWizard:
    wizard = open_run_wizard(gui_ctx, main_window, path)
    assert isinstance(wizard, RunWizard)
    return wizard


def test_push_wizard_preview_and_check_run(main_window, gui_ctx, qtbot):
    wizard = _open(main_window, gui_ctx, "plays/push/add-vlan.yml")
    assert [d.name for d in wizard.selected_devices()] == ["sa-as01", "sa-as02"]
    assert not wizard.start_button.isEnabled()
    wizard.fields["vlan_id"].set_value(30)
    wizard.fields["vlan_name"].set_value("Cameras")
    assert wizard.preview()
    assert wizard.preview_device_names() == ["sa-as01", "sa-as02"]
    for i in range(2):
        assert "vlan 30" in wizard.preview_tabs.widget(i).toPlainText()
    assert wizard.start_button.isEnabled()
    wizard.fields["vlan_name"].set_value("Cameras2")  # any change disables Start
    assert not wizard.start_button.isEnabled()
    wizard.fields["vlan_name"].set_value("Cameras")
    wizard.preview()
    assert wizard.mode() == RunMode.CHECK

    wizard.start_button.click()
    view = _job_view(main_window)
    _wait_done(qtbot, view)
    qtbot.waitUntil(lambda: view.counts().get("success") == 2, timeout=20000)
    assert view.table.rowCount() == 2
    assert [view.table.item(r, 1).text() for r in range(2)] == ["success", "success"]
    assert view.log.toPlainText().count("-> success") == 2
    view.select_device("sa-as01")
    qtbot.waitUntil(lambda: "+ vlan 30" in view.diff_view.toPlainText(), timeout=20000)
    assert "vlan 30" in view.render_view.toPlainText()


def test_unchecking_a_device_sets_limit(main_window, gui_ctx):
    wizard = _open(main_window, gui_ctx, "plays/push/add-vlan.yml")
    wizard.set_device_checked("sa-as02", False)
    wizard.fields["vlan_id"].set_value(30)
    wizard.fields["vlan_name"].set_value("Cameras")
    assert wizard.preview()
    assert wizard.preview_device_names() == ["sa-as01"]
    request = wizard.build_request()
    assert request.limit == ["sa-as01"]
    assert request.prompts == {"vlan_id": 30, "vlan_name": "Cameras"}


def test_apply_asks_for_confirmation(main_window, gui_ctx, monkeypatch):
    wizard = _open(main_window, gui_ctx, "plays/push/add-vlan.yml")
    wizard.fields["vlan_id"].set_value(30)
    wizard.fields["vlan_name"].set_value("Cameras")
    wizard.set_mode(RunMode.APPLY)
    wizard.preview()
    asked, started = [], []
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *a, **k: asked.append(a[2]) or QMessageBox.StandardButton.No,
    )
    monkeypatch.setattr(main_window, "start_run", lambda *a: started.append(a))
    wizard.start_button.click()
    assert asked and "2 devices" in asked[0]
    assert not started


def test_baseline_preview_never_shows_secret(main_window, gui_ctx):
    wizard = _open(main_window, gui_ctx, "plays/baseline/acme-baseline.yml")
    assert wizard.mode_combo.findData(RunMode.COMPLIANCE) >= 0
    assert list(wizard.tag_boxes) == ["aaa", "always", "archive", "ntp"]
    assert wizard.preview()
    text = "\n".join(
        wizard.preview_tabs.widget(i).toPlainText() for i in range(wizard.preview_tabs.count())
    )
    assert "key ********" in text
    assert TACACS_SECRET not in all_widget_text(wizard)


def test_invalid_prompt_shows_error_and_blocks_start(main_window, gui_ctx):
    wizard = _open(main_window, gui_ctx, "plays/push/add-vlan.yml")
    wizard.fields["vlan_id"].set_value(30)
    wizard.fields["vlan_name"].set_value("bad name!")
    assert not wizard.preview()
    assert wizard.fields["vlan_name"].has_error()
    assert "pattern" in wizard.fields["vlan_name"].error_text
    assert not wizard.error_label.isHidden() and wizard.error_label.text()
    assert not wizard.start_button.isEnabled()
    assert wizard.preview_tabs.count() == 0


def test_missing_required_prompt_blocks_preview(main_window, gui_ctx):
    wizard = _open(main_window, gui_ctx, "plays/push/add-vlan.yml")
    assert not wizard.preview()
    assert wizard.fields["vlan_id"].error_text == "required"


def test_day0_two_stage_form(main_window, gui_ctx):
    wizard = _open(main_window, gui_ctx, "plays/initial/acme-access-day0.yml")
    assert wizard.visible_prompt_names() == ["model", "site"]
    assert isinstance(wizard.port_picker, PortPicker)
    assert wizard.devices == ()
    wizard.fields["model"].set_value("cisco-c9300")
    assert wizard.visible_prompt_names() == ["model", "site"]  # site still missing
    wizard.fields["site"].set_value("site-a")
    assert wizard.visible_prompt_names() == [
        "model",
        "hostname",
        "site",
        "mgmt_vlan",
        "mgmt_ip",
        "gateway",
        "uplink",
    ]
    assert wizard.fields["gateway"].raw_value() == "203.0.113.1"
    assert wizard.fields["uplink"].raw_value() == "TenGigabitEthernet1/1/1"
    # a typed value survives a later change of the first stage
    wizard.fields["gateway"].set_value("203.0.113.9")
    wizard.fields["model"].set_value("cisco-c9200")
    assert wizard.fields["gateway"].raw_value() == "203.0.113.9"
    # inline validation of the second stage
    wizard.fields["mgmt_ip"].set_value("203.0.113.0/24")
    assert wizard.fields["mgmt_ip"].has_error()
    wizard.fields["mgmt_ip"].set_value("203.0.113.5/24")
    assert not wizard.fields["mgmt_ip"].has_error()


def test_day0_preview_renders_new_device(main_window, gui_ctx):
    wizard = _open(main_window, gui_ctx, "plays/initial/acme-access-day0.yml")
    wizard.fields["model"].set_value("cisco-c9300")
    wizard.fields["site"].set_value("site-a")
    wizard.fields["hostname"].set_value("SA-AS09")
    wizard.fields["mgmt_vlan"].set_value(10)
    wizard.fields["mgmt_ip"].set_value("203.0.113.9/24")
    assert wizard.preview(), wizard.error_label.text()
    assert wizard.preview_device_names() == ["sa-as09"]
    assert "hostname SA-AS09" in wizard.preview_tabs.widget(0).toPlainText()
    wizard.port_picker.clear()
    wizard.port_picker.addItem("COM9")
    request = wizard.build_request()
    assert request.serial_port == "COM9" and request.limit is None


# -- job view -----------------------------------------------------------------------


def test_apply_run_canary_stop_cancels_second_device(main_window, gui_ctx, qtbot, monkeypatch):
    asked = []
    monkeypatch.setattr(
        job_view,
        "ask_canary",
        lambda parent, device, status, n: asked.append((device, status, n)) or False,
    )
    request = RunRequest(
        "plays/push/add-vlan.yml", RunMode.APPLY, prompts={"vlan_id": "30", "vlan_name": "Cameras"}
    )
    main_window.start_run(request, "Add VLAN (apply)")
    view = _job_view(main_window)
    _wait_done(qtbot, view)
    qtbot.waitUntil(lambda: view.counts().get("cancelled") == 1, timeout=20000)
    assert asked == [("sa-as01", "success", 1)]
    statuses = {view.table.item(r, 0).text(): view.table.item(r, 1).text() for r in range(2)}
    assert statuses == {"sa-as01": "success", "sa-as02": "cancelled"}
    assert not view.cancel_button.isEnabled()
    assert gui_ctx.history.get_job(view.job_id).status in (JobStatus.CANCELLED, JobStatus.COMPLETED)


def test_canary_dialog_text(qtbot, monkeypatch):
    seen = {}

    def fake_question(parent, title, text, *args):
        seen["text"] = text
        return QMessageBox.StandardButton.No

    monkeypatch.setattr(QMessageBox, "question", fake_question)
    assert job_view.ask_canary(None, "sa-as01", "success", 4) is False
    assert seen["text"].startswith(
        "Canary sa-as01 finished with status success. Continue with the remaining 4 devices?"
    )


def test_run_failed_shows_warning(main_window, gui_ctx, qtbot):
    request = RunRequest("plays/push/missing.yml", RunMode.CHECK)
    main_window.start_run(request, "Missing")
    view = _job_view(main_window)
    qtbot.waitUntil(lambda: not view.warning_banner.isHidden(), timeout=20000)
    assert "Run failed" in view.warning_label.text()
    assert not view.cancel_button.isEnabled()


def _past_job(ctx, device_status=DeviceStatus.ROLLBACK_FAILED) -> str:
    job_id = ctx.history.create_job(
        actor=Actor.GUI,
        play_path="plays/push/add-vlan.yml",
        play_hash="abc",
        customer="acme",
        mode="apply",
        status=JobStatus.FAILED,
    )
    ctx.history.add_devices(job_id, ["sa-as01", "sa-as02"])
    ctx.history.update_device(job_id, "sa-as01", status=DeviceStatus.SUCCESS)
    ctx.history.update_device(
        job_id,
        "sa-as02",
        status=device_status,
        error_redacted="rollback | failed\nbadly",
        failed_line="vlan 30",
        rollback_status="failed",
        backup_path="C:/backups/sa-as02.cfg",
        diff_redacted="+ vlan 30\n- ntp server 192.0.2.99",
    )
    return job_id


def test_alarm_banner_needs_acknowledgment(gui_ctx, qtbot):
    job_id = _past_job(gui_ctx)
    assert job_id in gui_ctx.unacknowledged_alarms()
    view = make_job_view(gui_ctx, None, job_id=job_id)
    qtbot.addWidget(view)
    assert not view.alarm_banner.isHidden()
    assert view.alarm_banner.objectName() == "Banner"
    assert view.alarm_banner.property("level") == "error"
    assert view.alarm_label.text() == "1 device(s) may be half-configured"
    view._refresh_all()
    assert not view.alarm_banner.isHidden()  # never auto-dismissed
    view.acknowledge_button.click()
    assert view.alarm_banner.isHidden()
    assert job_id not in gui_ctx.unacknowledged_alarms()
    # reopening an acknowledged job does not raise the banner again
    again = make_job_view(gui_ctx, None, job_id=job_id)
    qtbot.addWidget(again)
    assert again.alarm_banner.isHidden()


def test_past_job_detail_tabs(gui_ctx, qtbot):
    job_id = _past_job(gui_ctx)
    view = make_job_view(gui_ctx, None, job_id=job_id)
    qtbot.addWidget(view)
    assert view.title_label.text() == "Add VLAN"
    assert not view.cancel_button.isEnabled()
    view.select_device("sa-as02")
    assert "+ vlan 30" in view.diff_view.toPlainText()
    details = view.details_view.toPlainText()
    assert "Failed line: vlan 30" in details and "Backup: C:/backups/sa-as02.cfg" in details
    assert view.table.item(1, 1).foreground().color().name() == "#c4402f"


def test_copy_summary_has_devices_and_no_secrets(main_window, gui_ctx, qtbot):
    request = RunRequest("plays/baseline/acme-baseline.yml", RunMode.CHECK)
    main_window.start_run(request, "Baseline (check)")
    view = _job_view(main_window)
    _wait_done(qtbot, view)
    view.copy_button.click()
    text = QApplication.clipboard().text()
    for needle in (
        "sa-as01",
        "sa-as02",
        view.job_id,
        "Acme baseline",
        "| Device | Status | Error |",
    ):
        assert needle in text
    assert TACACS_SECRET not in text and LOGIN_PASSWORD not in text
    assert any(f"| {name} | " in text for name in ("success", "failed")) or "| sa-as01 |" in text


def test_summary_escapes_cells(gui_ctx, qtbot):
    view = make_job_view(gui_ctx, None, job_id=_past_job(gui_ctx))
    qtbot.addWidget(view)
    text = view.summary_markdown()
    assert "rollback \\| failed badly" in text
    assert "| sa-as02 | rollback_failed |" in text
