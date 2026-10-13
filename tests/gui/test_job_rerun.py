"""Job view: Apply changes after a check, Retry failed, Run again; check-mode wording."""

from __future__ import annotations

from PySide6.QtWidgets import QMessageBox

from tsunami.core.models.jobs import DeviceStatus
from tsunami.core.models.play import RunMode
from tsunami.core.runner import RunRequest
from tsunami.gui.screens.job_view import JobView

PLAY = "plays/push/add-vlan.yml"
PROMPTS = {"vlan_id": 30, "vlan_name": "Cameras"}


def _answer(monkeypatch, yes: bool, asked: list[str] | None = None) -> None:
    button = QMessageBox.StandardButton.Yes if yes else QMessageBox.StandardButton.No

    def question(_parent, title, text, *args, **kwargs):
        if asked is not None:
            asked.append(f"{title}: {text}")
        return button

    monkeypatch.setattr(QMessageBox, "question", question)


def _run(main_window, qtbot, request: RunRequest) -> JobView:
    """Start a run like the wizard does and wait for its job view to finish."""
    before = set(main_window.findChildren(JobView))
    main_window.start_run(request, "test")
    view = next(v for v in main_window.findChildren(JobView) if v not in before)
    qtbot.waitUntil(lambda: view._finished and view.job_id is not None, timeout=20000)
    qtbot.waitUntil(lambda: not main_window._threads, timeout=20000)
    return view


def test_check_shows_what_an_apply_would_do_and_applies_it(
    main_window, gui_ctx, mock_transport, qtbot, monkeypatch
):
    mock_transport.device("sa-as01").running += ["vlan 30", " name Cameras"]
    check = _run(main_window, qtbot, RunRequest(PLAY, RunMode.CHECK, prompts=PROMPTS))
    labels = {check.table.item(r, 0).text(): check.table.item(r, 1).text() for r in range(2)}
    assert labels == {"sa-as01": "no changes", "sa-as02": "changes to apply"}
    assert "1 changes to apply" in check.meta_label.text()
    assert "| changes to apply | 1 |" in check.summary_markdown()
    assert check.apply_button.isVisible() or not check.isVisible()
    assert not check.apply_button.isHidden()
    assert check.apply_button.text() == "Apply changes (1)…"
    assert check.retry_button.isHidden()  # nothing failed
    assert not check.again_button.isHidden()

    asked: list[str] = []
    _answer(monkeypatch, False, asked)
    check.apply_checked()
    assert "sa-as02" in asked[0] and "sa-as01" not in asked[0]
    assert len(main_window.findChildren(JobView)) == 1  # No: nothing started

    _answer(monkeypatch, True)
    before = set(main_window.findChildren(JobView))
    check.apply_checked()
    applied = next(v for v in main_window.findChildren(JobView) if v not in before)
    qtbot.waitUntil(lambda: applied._finished and applied.job_id is not None, timeout=20000)
    qtbot.waitUntil(lambda: not main_window._threads, timeout=20000)
    assert applied._mode() == "apply"
    assert {d.device: d.status for d in gui_ctx.history.devices(applied.job_id)} == {
        "sa-as02": DeviceStatus.SUCCESS
    }
    assert "vlan 30" in mock_transport.device("sa-as02").running
    assert applied.apply_button.isHidden()  # only check jobs offer it


def test_apply_after_check_stops_a_device_whose_render_changed(
    main_window, gui_ctx, mock_transport, qtbot, monkeypatch
):
    check = _run(main_window, qtbot, RunRequest(PLAY, RunMode.CHECK, prompts=PROMPTS))
    play = gui_ctx.workspace_path / PLAY
    play.write_text(play.read_text(encoding="utf-8") + "# edited\n", encoding="utf-8")
    _answer(monkeypatch, True)
    before = set(main_window.findChildren(JobView))
    check.apply_checked()
    applied = next(v for v in main_window.findChildren(JobView) if v not in before)
    qtbot.waitUntil(lambda: applied._finished and applied.job_id is not None, timeout=20000)
    qtbot.waitUntil(lambda: not main_window._threads, timeout=20000)
    statuses = {d.status for d in gui_ctx.history.devices(applied.job_id)}
    assert DeviceStatus.RENDER_CHANGED in statuses
    assert all("vlan 30" not in mock_transport.device(d).running for d in ("sa-as01", "sa-as02"))


def test_retry_failed_runs_only_the_failed_device(
    main_window, gui_ctx, mock_transport, qtbot, monkeypatch
):
    mock_transport.device("sa-as02").reject_lines = ["vlan 30"]
    asked: list[str] = []
    _answer(monkeypatch, True, asked)  # the canary question, then the retry confirmation
    first = _run(main_window, qtbot, RunRequest(PLAY, RunMode.APPLY, prompts=PROMPTS))
    assert not first.retry_button.isHidden()
    assert first.retry_button.text() == "Retry failed (1)"

    mock_transport.device("sa-as02").reject_lines = []
    asked.clear()
    before = set(main_window.findChildren(JobView))
    first.retry_failed()
    assert asked and "1 device?" in asked[0]  # apply asks first
    retry = next(v for v in main_window.findChildren(JobView) if v not in before)
    qtbot.waitUntil(lambda: retry._finished and retry.job_id is not None, timeout=20000)
    qtbot.waitUntil(lambda: not main_window._threads, timeout=20000)
    assert {d.device: d.status for d in gui_ctx.history.devices(retry.job_id)} == {
        "sa-as02": DeviceStatus.SUCCESS
    }
    assert retry.retry_button.isHidden()


def test_run_again_on_a_check_starts_without_asking(main_window, qtbot, monkeypatch):
    first = _run(main_window, qtbot, RunRequest(PLAY, RunMode.CHECK, prompts=PROMPTS))
    asked: list[str] = []
    _answer(monkeypatch, False, asked)
    before = set(main_window.findChildren(JobView))
    first.run_again()
    assert asked == []  # a check changes nothing
    again = next(v for v in main_window.findChildren(JobView) if v not in before)
    qtbot.waitUntil(lambda: again._finished and again.job_id is not None, timeout=20000)
    qtbot.waitUntil(lambda: not main_window._threads, timeout=20000)
    assert again._mode() == "check" and again.table.rowCount() == 2


def test_past_job_offers_the_same_actions(main_window, gui_ctx, qtbot):
    first = _run(main_window, qtbot, RunRequest(PLAY, RunMode.CHECK, prompts=PROMPTS))
    past = JobView(gui_ctx, main_window, job_id=first.job_id)
    qtbot.addWidget(past)
    assert not past.apply_button.isHidden() and not past.again_button.isHidden()
    assert past.apply_button.text() == "Apply changes (2)…"


def test_a_job_that_cannot_be_rebuilt_opens_the_wizard_filled_in(
    main_window, gui_ctx, qtbot, monkeypatch
):
    from tsunami.core import rerun
    from tsunami.gui.screens.run_wizard import RunWizard

    first = _run(main_window, qtbot, RunRequest(PLAY, RunMode.CHECK, prompts=PROMPTS))
    monkeypatch.setattr(rerun, "rebuild_problem", lambda ws, job: "per-device answers")
    started: list = []
    monkeypatch.setattr(main_window, "start_run", lambda *a: started.append(a))
    _answer(monkeypatch, True)
    first.apply_checked()
    assert started == []
    wizard = next(iter(main_window.findChildren(RunWizard)))
    assert wizard.mode() == RunMode.APPLY
    assert wizard.fields["vlan_id"].raw_value() == 30
    assert wizard.fields["vlan_name"].raw_value() == "Cameras"
