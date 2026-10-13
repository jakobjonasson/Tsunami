"""Running a past job again (core/rerun.py): apply what a check found, retry, repeat."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from tsunami.core.events import DeviceLog, DeviceStatusChanged
from tsunami.core.history import History
from tsunami.core.issues import TsunamiError
from tsunami.core.models.jobs import Actor, DeviceStatus, JobStatus
from tsunami.core.models.play import RunMode
from tsunami.core.rerun import (
    again_request,
    apply_checked_request,
    changed_devices,
    rebuild_problem,
    retry_devices,
    retry_request,
)
from tsunami.core.runner import Runner, RunRequest
from tsunami.core.secret_store import KeyringSecretStore, LoginCredential
from tsunami.core.transports.mock import MockTransport
from tsunami.core.workspace import load_workspace

FIXTURE = Path(__file__).parents[1] / "fixtures" / "workspace"
PLAY = "plays/push/add-vlan.yml"
PROMPTS = {"vlan_id": "30", "vlan_name": "Cameras"}


@pytest.fixture
def setup(tmp_path, app_paths):
    root = tmp_path / "ws"
    shutil.copytree(FIXTURE, root)
    history = History(tmp_path / "history.db")
    secrets = KeyringSecretStore(index_path=tmp_path / "idx.json")
    secrets.set_login("acme-netadmin", LoginCredential("netadmin", "Login-Pass-1234"))
    mock = MockTransport()
    for name in ("sa-as01", "sa-as02"):
        mock.add_device(name, running=[f"hostname {name}"]).exec_responses["re:^show vlan id"] = (
            "30 Cameras active"
        )

    events: list = []

    def run(request: RunRequest) -> str:
        runner = Runner(
            load_workspace(root),
            transport=mock,
            history=history,
            secrets=secrets,
            paths=app_paths,
            on_event=events.append,
            heartbeat_seconds=0.05,
        )
        return runner.run(request)

    run.events = events
    yield root, history, mock, run
    history.close()


def statuses(history: History, job_id: str) -> dict[str, DeviceStatus]:
    return {d.device: d.status for d in history.devices(job_id)}


def test_apply_after_check_pushes_only_the_devices_that_would_change(setup):
    root, history, mock, run = setup
    mock.device("sa-as01").running += ["vlan 30", " name Cameras"]
    check = run(RunRequest(PLAY, RunMode.CHECK, prompts=PROMPTS))
    details = {d.device: d for d in history.devices(check)}
    assert changed_devices(history, check) == ["sa-as02"]

    request = apply_checked_request(history, load_workspace(root), check)
    assert (request.mode, request.actor, list(request.limit)) == (
        RunMode.APPLY,
        Actor.GUI,
        ["sa-as02"],
    )
    assert request.approved_hashes == {"sa-as02": details["sa-as02"].render_hash}
    applied = run(request)
    assert applied != check
    assert statuses(history, applied) == {"sa-as02": DeviceStatus.SUCCESS}
    assert "vlan 30" in mock.device("sa-as02").running


def test_check_detail_says_what_an_apply_would_do(setup):
    _root, history, mock, run = setup
    mock.device("sa-as01").running += ["vlan 30", " name Cameras"]
    check = run(RunRequest(PLAY, RunMode.CHECK, prompts=PROMPTS))
    assert set(statuses(history, check).values()) == {DeviceStatus.SUCCESS}
    final = {
        e.device: e.detail
        for e in run.events
        if isinstance(e, DeviceStatusChanged) and e.status == DeviceStatus.SUCCESS
    }
    assert final == {"sa-as01": "no changes", "sa-as02": "changes to apply"}
    logs = [e.text for e in run.events if isinstance(e, DeviceLog)]
    assert not any("None" in text for text in logs if text.startswith("facts:"))


def test_apply_after_check_refuses_a_play_edited_since(setup):
    root, history, mock, run = setup
    check = run(RunRequest(PLAY, RunMode.CHECK, prompts=PROMPTS))
    play = root / PLAY
    play.write_text(play.read_text(encoding="utf-8") + "# edited\n", encoding="utf-8")
    applied = run(apply_checked_request(history, load_workspace(root), check))
    assert DeviceStatus.RENDER_CHANGED in statuses(history, applied).values()
    assert all("vlan 30" not in mock.device(d).running for d in ("sa-as01", "sa-as02"))


def test_apply_after_check_refuses_a_changed_answer(setup):
    root, history, mock, run = setup
    check = run(RunRequest(PLAY, RunMode.CHECK, prompts=PROMPTS))
    history.update_job(check, prompts={"vlan_id": "31", "vlan_name": "Cameras"})
    applied = run(apply_checked_request(history, load_workspace(root), check))
    assert DeviceStatus.RENDER_CHANGED in statuses(history, applied).values()
    assert all("vlan 31" not in mock.device(d).running for d in ("sa-as01", "sa-as02"))


def test_nothing_to_apply(setup):
    root, history, mock, run = setup
    for name in ("sa-as01", "sa-as02"):
        mock.device(name).running += ["vlan 30", " name Cameras"]
    check = run(RunRequest(PLAY, RunMode.CHECK, prompts=PROMPTS))
    assert changed_devices(history, check) == []
    with pytest.raises(TsunamiError, match="nothing to apply"):
        apply_checked_request(history, load_workspace(root), check)
    applied = run(RunRequest(PLAY, RunMode.APPLY, prompts=PROMPTS))
    assert changed_devices(history, applied) == []  # only check jobs offer it


def test_retry_runs_only_the_devices_that_failed(setup):
    root, history, mock, run = setup
    mock.device("sa-as02").reject_lines = ["vlan 30"]
    first = run(RunRequest(PLAY, RunMode.APPLY, prompts=PROMPTS))
    assert statuses(history, first)["sa-as01"] == DeviceStatus.SUCCESS
    assert statuses(history, first)["sa-as02"] != DeviceStatus.SUCCESS
    assert retry_devices(history, first) == ["sa-as02"]

    mock.device("sa-as02").reject_lines = []
    request = retry_request(history, load_workspace(root), first)
    assert (request.mode, list(request.limit), request.approved_hashes) == (
        RunMode.APPLY,
        ["sa-as02"],
        None,
    )
    second = run(request)
    assert statuses(history, second) == {"sa-as02": DeviceStatus.SUCCESS}
    assert retry_devices(history, second) == []
    with pytest.raises(TsunamiError, match="no devices to retry"):
        retry_request(history, load_workspace(root), second)


def test_run_again_repeats_every_device_with_the_same_answers(setup):
    root, history, _mock, run = setup
    first = run(RunRequest(PLAY, RunMode.CHECK, prompts=PROMPTS, limit=["sa-as01"]))
    request = again_request(history, load_workspace(root), first)
    assert (request.mode, list(request.limit), dict(request.prompts)) == (
        RunMode.CHECK,
        ["sa-as01"],
        PROMPTS,
    )


def test_running_jobs_offer_nothing(setup):
    _root, history, _mock, _run = setup
    job_id = history.create_job(
        actor=Actor.GUI,
        play_path=PLAY,
        play_hash="x",
        customer="acme",
        mode="check",
        status=JobStatus.RUNNING,
        prompts=PROMPTS,
    )
    history.add_devices(job_id, ["sa-as01"])
    history.update_device(job_id, "sa-as01", status=DeviceStatus.FAILED)
    assert retry_devices(history, job_id) == []
    assert changed_devices(history, job_id) == []


def test_jobs_that_cannot_be_rebuilt_from_history(setup):
    root, history, _mock, run = setup
    workspace = load_workspace(root)
    first = run(RunRequest(PLAY, RunMode.CHECK, prompts=PROMPTS))
    assert rebuild_problem(workspace, history.get_job(first)) is None
    initial = history.create_job(
        actor=Actor.GUI,
        play_path="plays/initial/acme-access-day0.yml",
        play_hash="x",
        customer="acme",
        mode="apply",
        status=JobStatus.FAILED,
    )
    assert "initial play" in rebuild_problem(workspace, history.get_job(initial))
    (root / PLAY).unlink()
    assert "no longer exists" in rebuild_problem(load_workspace(root), history.get_job(first))
    with pytest.raises(TsunamiError, match="no longer exists"):
        again_request(history, load_workspace(root), first)
