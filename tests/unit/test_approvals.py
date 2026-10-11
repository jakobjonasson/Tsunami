"""Approval flow: approve, deny, and render_changed after approval (design.md, Approval flow)."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from tsunami.core.approvals import approve, deny
from tsunami.core.history import History
from tsunami.core.issues import TsunamiError
from tsunami.core.models.jobs import Actor, DeviceStatus, JobStatus
from tsunami.core.render.renderer import Renderer
from tsunami.core.runner import Runner, play_hash
from tsunami.core.secret_store import KeyringSecretStore, LoginCredential
from tsunami.core.services import collect_prompts, play_entry
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
    yield root, history, secrets, mock, app_paths
    history.close()


def pending_request(root: Path, history: History) -> str:
    """What the MCP server's request_run stores: play hash, renders and their hashes."""
    workspace = load_workspace(root)
    entry = play_entry(workspace, PLAY)
    job_id = history.create_job(
        actor=Actor.MCP,
        play_path=PLAY,
        play_hash=play_hash(root / PLAY),
        customer="acme",
        mode="apply",
        status=JobStatus.AWAITING_APPROVAL,
        prompts=PROMPTS,
    )
    history.add_devices(job_id, ["sa-as01", "sa-as02"])
    renderer = Renderer(workspace)
    for device in ("sa-as01", "sa-as02"):
        values = collect_prompts(workspace, entry, PROMPTS, device=device)
        rendering = renderer.render(entry, device=device, prompt_values=values)
        history.update_device(
            job_id,
            device,
            render_hash=rendering.render_hash(),
            render_masked=rendering.masked_text(),
        )
    return job_id


def execute(root, history, secrets, mock, paths, request):
    runner = Runner(
        load_workspace(root),
        transport=mock,
        history=history,
        secrets=secrets,
        paths=paths,
        heartbeat_seconds=0.05,
    )
    return runner.run(request)


def test_approved_request_runs_with_the_approved_renders(setup):
    root, history, secrets, mock, paths = setup
    job_id = pending_request(root, history)
    request = approve(history, load_workspace(root), job_id, approved_by="tech1")
    job = history.get_job(job_id)
    assert (job.status, job.approved_by) == (JobStatus.APPROVED, "tech1")
    assert execute(root, history, secrets, mock, paths, request) == job_id
    assert history.get_job(job_id).status == JobStatus.COMPLETED
    assert {d.status for d in history.devices(job_id)} == {DeviceStatus.SUCCESS}
    assert "vlan 30" in mock.device("sa-as02").running


def test_play_edited_after_request_fails_every_device(setup):
    root, history, secrets, mock, paths = setup
    job_id = pending_request(root, history)
    play = root / PLAY
    play.write_text(play.read_text(encoding="utf-8") + "# edited\n", encoding="utf-8")
    request = approve(history, load_workspace(root), job_id, approved_by="tech1")
    execute(root, history, secrets, mock, paths, request)
    statuses = {d.device: d.status for d in history.devices(job_id)}
    # The canary fails render_changed; max-fail then stops the run before sa-as02.
    assert statuses == {
        "sa-as01": DeviceStatus.RENDER_CHANGED,
        "sa-as02": DeviceStatus.SKIPPED_MAXFAIL,
    }
    assert all("vlan 30" not in mock.device(d).running for d in statuses)


def test_tampered_render_hash_fails_that_device(setup):
    root, history, secrets, mock, paths = setup
    job_id = pending_request(root, history)
    history.update_device(job_id, "sa-as02", render_hash="0" * 64)
    request = approve(history, load_workspace(root), job_id, approved_by="tech1")
    execute(root, history, secrets, mock, paths, request)
    statuses = {d.device: d.status for d in history.devices(job_id)}
    assert statuses == {"sa-as01": DeviceStatus.SUCCESS, "sa-as02": DeviceStatus.RENDER_CHANGED}


def test_deny_and_double_decisions(setup):
    root, history, *_ = setup
    job_id = pending_request(root, history)
    deny(history, job_id, denied_by="tech2")
    job = history.get_job(job_id)
    assert (job.status, job.approved_by) == (JobStatus.DENIED, "tech2")
    with pytest.raises(TsunamiError, match="not awaiting approval"):
        approve(history, load_workspace(root), job_id, approved_by="tech1")
    with pytest.raises(TsunamiError):
        deny(history, job_id, denied_by="tech2")
