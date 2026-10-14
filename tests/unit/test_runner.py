"""Runner and device pipeline against the mock transport (design.md, Execution engine)."""

from __future__ import annotations

import shutil
import threading
from pathlib import Path

import pytest

from tsunami.core.events import (
    CanaryWaiting,
    DeviceStatusChanged,
    Event,
    JobFinished,
    MaxFailReached,
)
from tsunami.core.history import History
from tsunami.core.models.jobs import DeviceStatus, JobStatus
from tsunami.core.models.play import RunMode
from tsunami.core.runner import Runner, RunRequest
from tsunami.core.secret_store import KeyringSecretStore, LoginCredential
from tsunami.core.transports.base import HostKeyMismatchError
from tsunami.core.transports.mock import MockTransport
from tsunami.core.workspace import load_workspace

FIXTURE = Path(__file__).parents[1] / "fixtures" / "workspace"
ADD_VLAN = "plays/push/add-vlan.yml"
BASELINE = "plays/baseline/acme-baseline.yml"
VLAN = {"vlan_id": "30", "vlan_name": "Cameras"}
SECRET = "Real-Tacacs-Key-77"
LOGIN = "Login-Pass-1234"


@pytest.fixture
def ws_root(tmp_path) -> Path:
    root = tmp_path / "ws"
    shutil.copytree(FIXTURE, root)
    return root


@pytest.fixture
def secrets(tmp_path) -> KeyringSecretStore:
    store = KeyringSecretStore(index_path=tmp_path / "credential-index.json")
    store.set_login("acme-netadmin", LoginCredential("netadmin", LOGIN))
    store.set_secret("acme", "tacacs", SECRET)
    return store


@pytest.fixture
def history(tmp_path):
    db = History(tmp_path / "history.db")
    yield db
    db.close()


def mock_for(names, **kwargs) -> MockTransport:
    mock = MockTransport(**kwargs)
    for name in names:
        state = mock.add_device(name, running=["hostname " + name, "ntp server 192.0.2.99"])
        state.exec_responses["re:^show vlan id"] = "30   Cameras   active"
    return mock


def write_play(root: Path, name: str, body: str) -> str:
    relative = f"plays/push/{name}.yml"
    (root / relative).write_text(body, encoding="utf-8")
    return relative


def add_devices(root: Path, count: int) -> list[str]:
    names = [f"sa-as{i:03d}" for i in range(1, count + 1)]
    lines = [
        f"- name: {n}\n  host: 203.0.113.{(i % 250) + 1}\n  site: site-a\n  role: access\n"
        "  platform: ios-xe\n"
        for i, n in enumerate(names)
    ]
    (root / "inventory/customers/acme/devices.yml").write_text("".join(lines), encoding="utf-8")
    return names


def make_runner(root, mock, history, secrets, app_paths, **kwargs):
    events: list[Event] = []
    runner = Runner(
        load_workspace(root),
        transport=mock,
        history=history,
        secrets=secrets,
        paths=app_paths,
        on_event=events.append,
        heartbeat_seconds=0.05,
        **kwargs,
    )
    return runner, events


def statuses(history, job_id):
    return {d.device: d.status for d in history.devices(job_id)}


# -- apply ----------------------------------------------------------------------------


def test_apply_with_canary_backup_and_save(ws_root, history, secrets, app_paths):
    mock = mock_for(["sa-as01", "sa-as02"])
    asked = []
    runner, events = make_runner(
        ws_root, mock, history, secrets, app_paths, confirm_canary=lambda d: asked.append(d) or True
    )
    job_id = runner.run(RunRequest(ADD_VLAN, RunMode.APPLY, prompts=VLAN))

    assert history.get_job(job_id).status == JobStatus.COMPLETED
    assert statuses(history, job_id) == {
        "sa-as01": DeviceStatus.SUCCESS,
        "sa-as02": DeviceStatus.SUCCESS,
    }
    assert asked == ["sa-as01"]
    assert any(isinstance(e, CanaryWaiting) for e in events)
    for name in ("sa-as01", "sa-as02"):
        state = mock.device(name)
        assert "vlan 30" in state.running and " name Cameras" in state.running
        assert state.startup == state.running  # write memory
        assert state.flash == {}  # rollback snapshot removed after success
    backups = list((app_paths.backups_dir / "acme").rglob("*.cfg"))
    assert len(backups) == 2
    assert all(d.render_hash and d.backup_path for d in history.devices(job_id))


def test_declined_canary_cancels_the_rest(ws_root, history, secrets, app_paths):
    mock = mock_for(["sa-as01", "sa-as02"])
    runner, _ = make_runner(
        ws_root, mock, history, secrets, app_paths, confirm_canary=lambda d: False
    )
    job_id = runner.run(RunRequest(ADD_VLAN, RunMode.APPLY, prompts=VLAN))
    assert history.get_job(job_id).status == JobStatus.CANCELLED
    assert statuses(history, job_id)["sa-as02"] == DeviceStatus.CANCELLED
    assert "vlan 30" not in mock.device("sa-as02").running


def test_rejected_line_rolls_back_with_configure_replace(ws_root, history, secrets, app_paths):
    mock = mock_for(["sa-as01", "sa-as02"])
    mock.device("sa-as01").reject_lines.append(r"^name Cameras$")
    before = list(mock.device("sa-as01").running)
    runner, _ = make_runner(ws_root, mock, history, secrets, app_paths)
    job_id = runner.run(RunRequest(ADD_VLAN, RunMode.APPLY, prompts=VLAN, limit=["sa-as01"]))
    device = history.devices(job_id)[0]
    assert device.status == DeviceStatus.ROLLED_BACK
    assert device.failed_line == " name Cameras"
    assert mock.device("sa-as01").running == before
    assert history.get_job(job_id).status == JobStatus.FAILED


def test_failed_post_check_rolls_back(ws_root, history, secrets, app_paths):
    mock = mock_for(["sa-as01"])
    mock.device("sa-as01").exec_responses["re:^show vlan id"] = "VLAN id 30 not found"
    runner, _ = make_runner(ws_root, mock, history, secrets, app_paths)
    job_id = runner.run(RunRequest(ADD_VLAN, RunMode.APPLY, prompts=VLAN, limit=["sa-as01"]))
    device = history.devices(job_id)[0]
    assert device.status == DeviceStatus.ROLLED_BACK
    assert "post-check" in device.error_redacted
    assert "vlan 30" not in mock.device("sa-as01").running


def test_secrets_reach_the_device_but_never_history_or_events(ws_root, history, secrets, app_paths):
    mock = mock_for(["sa-as01", "sa-as02"])
    runner, events = make_runner(ws_root, mock, history, secrets, app_paths)
    job_id = runner.run(RunRequest(BASELINE, RunMode.APPLY))
    assert history.get_job(job_id).status == JobStatus.COMPLETED
    assert " key " + SECRET in mock.device("sa-as01").running
    stored = [str(history.get_job(job_id))] + [str(d) for d in history.devices(job_id)]
    stored += [str(e) for e in events]
    for text in stored:
        assert SECRET not in text and LOGIN not in text
    assert any("key ********" in (d.render_masked or "") for d in history.devices(job_id))


def test_max_fail_stops_scheduling(ws_root, history, secrets, app_paths):
    names = add_devices(ws_root, 20)
    play = write_play(
        ws_root,
        "mf",
        "name: mf\ntype: push\ncustomer: acme\ntargets:\n  roles: [access]\n"
        "steps:\n  - config: [vlan 40]\nexecution:\n  batch_size: 5\n  canary: false\n"
        "  max_fail_percent: 10\n",
    )
    mock = mock_for(names, unreachable=names[:2])
    runner, events = make_runner(ws_root, mock, history, secrets, app_paths)
    job_id = runner.run(RunRequest(play, RunMode.APPLY))
    result = statuses(history, job_id)
    assert result["sa-as001"] == DeviceStatus.UNREACHABLE
    assert sum(s == DeviceStatus.SKIPPED_MAXFAIL for s in result.values()) == 15
    assert any(isinstance(e, MaxFailReached) for e in events)


def test_two_hundred_devices_stay_within_fifty_sessions(ws_root, history, secrets, app_paths):
    names = add_devices(ws_root, 200)
    play = write_play(
        ws_root,
        "scale",
        "name: scale\ntype: push\ncustomer: acme\ntargets:\n"
        "  roles: [access]\nsteps:\n  - config: [vlan 50]\nexecution:\n"
        "  batch_size: 50\n  canary: false\n  backup: false\n  gather_facts: false\n",
    )
    mock = mock_for(names, latency=(0.001, 0.004), seed=7)
    runner, _ = make_runner(ws_root, mock, history, secrets, app_paths)
    job_id = runner.run(RunRequest(play, RunMode.APPLY))
    assert history.get_job(job_id).status == JobStatus.COMPLETED
    assert all(s == DeviceStatus.SUCCESS for s in statuses(history, job_id).values())
    assert 1 < mock.max_concurrent_sessions <= 50
    assert mock.active_sessions == 0


def test_leased_device_is_busy(ws_root, history, secrets, app_paths):
    other = history.create_job(
        actor="mcp",
        play_path="x",
        play_hash="h",
        customer="acme",
        mode="apply",
        status=JobStatus.RUNNING,
    )
    history.heartbeat(other)
    assert history.acquire_lease("acme", "sa-as01", other)
    mock = mock_for(["sa-as01"])
    runner, _ = make_runner(ws_root, mock, history, secrets, app_paths)
    job_id = runner.run(RunRequest(ADD_VLAN, RunMode.APPLY, prompts=VLAN, limit=["sa-as01"]))
    assert statuses(history, job_id) == {"sa-as01": DeviceStatus.DEVICE_BUSY}
    assert mock.connections == 0


# -- other modes ------------------------------------------------------------------------


def test_render_mode_never_connects(ws_root, history, secrets, app_paths):
    mock = mock_for(["sa-as01", "sa-as02"])
    runner, _ = make_runner(ws_root, mock, history, secrets, app_paths)
    job_id = runner.run(RunRequest(BASELINE, RunMode.RENDER))
    assert mock.connections == 0
    assert all("key ********" in d.render_masked for d in history.devices(job_id))


def test_check_mode_diffs_without_changes(ws_root, history, secrets, app_paths):
    mock = mock_for(["sa-as01"])
    runner, _ = make_runner(ws_root, mock, history, secrets, app_paths)
    job_id = runner.run(RunRequest(ADD_VLAN, RunMode.CHECK, prompts=VLAN, limit=["sa-as01"]))
    device = history.devices(job_id)[0]
    assert device.status == DeviceStatus.SUCCESS
    assert "+ vlan 30" in device.diff_redacted
    assert not any(kind == "config" for kind, _ in mock.device("sa-as01").sent)


def test_compliance_flags_stale_ntp_server(ws_root, history, secrets, app_paths):
    mock = mock_for(["sa-as01", "sa-as02"])
    runner, _ = make_runner(ws_root, mock, history, secrets, app_paths)
    job_id = runner.run(RunRequest(BASELINE, RunMode.COMPLIANCE))
    rows = history.compliance_history("acme")
    assert {r["device"] for r in rows} == {"sa-as01", "sa-as02"}
    assert all(r["extra_count"] == 1 for r in rows)
    assert any("ntp server 192.0.2.99" in e for r in rows for e in r["details"]["extra"])
    assert history.get_job(job_id).status == JobStatus.COMPLETED


# -- refusals -----------------------------------------------------------------------------


def test_cancel_before_start(ws_root, history, secrets, app_paths):
    mock = mock_for(["sa-as01", "sa-as02"])
    cancel = threading.Event()
    cancel.set()
    runner, _ = make_runner(ws_root, mock, history, secrets, app_paths, cancel=cancel)
    job_id = runner.run(RunRequest(ADD_VLAN, RunMode.APPLY, prompts=VLAN))
    assert history.get_job(job_id).status == JobStatus.CANCELLED
    assert mock.connections == 0


def test_bad_prompts_fail_the_job_before_any_connection(ws_root, history, secrets, app_paths):
    mock = mock_for(["sa-as01"])
    runner, events = make_runner(ws_root, mock, history, secrets, app_paths)
    job_id = runner.run(RunRequest(ADD_VLAN, RunMode.APPLY, prompts={"vlan_id": "5000"}))
    job = history.get_job(job_id)
    assert job.status == JobStatus.FAILED
    assert "vlan_id" in job.summary["error"] and "vlan_name: required" in job.summary["error"]
    assert mock.connections == 0
    assert isinstance(events[-1], JobFinished)


def test_render_changed_after_approval(ws_root, history, secrets, app_paths):
    mock = mock_for(["sa-as01"])
    runner, _ = make_runner(ws_root, mock, history, secrets, app_paths)
    job_id = runner.run(
        RunRequest(
            ADD_VLAN,
            RunMode.APPLY,
            prompts=VLAN,
            limit=["sa-as01"],
            approved_hashes={"sa-as01": "not-the-hash"},
        )
    )
    assert statuses(history, job_id) == {"sa-as01": DeviceStatus.RENDER_CHANGED}
    assert not any(kind == "config" for kind, _ in mock.device("sa-as01").sent)


def test_host_key_mismatch_status(ws_root, history, secrets, app_paths):
    class Refusing:
        def open(self, target):
            raise HostKeyMismatchError(f"{target.device}: host key changed")

    runner, _ = make_runner(ws_root, Refusing(), history, secrets, app_paths)
    job_id = runner.run(RunRequest(ADD_VLAN, RunMode.APPLY, prompts=VLAN, limit=["sa-as01"]))
    assert statuses(history, job_id) == {"sa-as01": DeviceStatus.HOSTKEY_MISMATCH}


def test_job_that_fails_before_any_device_leaves_none_pending(ws_root, history, secrets, app_paths):
    secrets.delete_login("acme-netadmin")
    mock = mock_for(["sa-as01", "sa-as02"])
    runner, events = make_runner(ws_root, mock, history, secrets, app_paths)
    job_id = runner.run(RunRequest(ADD_VLAN, RunMode.CHECK, prompts=VLAN))
    job = history.get_job(job_id)
    assert job.status == JobStatus.FAILED
    assert job.summary["error"].startswith("credential acme-netadmin is not in Credential Manager")
    devices = history.devices(job_id)
    assert {d.status for d in devices} == {DeviceStatus.FAILED}
    assert all(d.error_redacted.startswith("not started: credential") for d in devices)
    changed = [e for e in events if isinstance(e, DeviceStatusChanged)]
    assert {e.device for e in changed} == {"sa-as01", "sa-as02"}
    assert mock.connections == 0


def test_each_device_logs_in_with_its_own_credential(ws_root, history, secrets, app_paths):
    customer = ws_root / "inventory/customers/acme/customer.yml"
    customer.write_text(
        customer.read_text(encoding="utf-8").replace(
            "lab: false\n", "lab: false\nsite_credentials:\n  site-a: acme-site-a\n"
        ),
        encoding="utf-8",
    )
    devices = ws_root / "inventory/customers/acme/devices.yml"
    devices.write_text(
        devices.read_text(encoding="utf-8").replace(
            "  host: 203.0.113.12\n", "  host: 203.0.113.12\n  credential: acme-sa-as02\n"
        ),
        encoding="utf-8",
    )
    secrets.set_login("acme-site-a", LoginCredential("siteadmin", "Site-Pass-5151"))
    secrets.set_login("acme-sa-as02", LoginCredential("as02admin", "Dev-Pass-6262"))
    mock = mock_for(["sa-as01", "sa-as02"])
    used = {}
    real_open = mock.open

    def recording_open(target):
        used[target.device] = target.credential.username
        state = mock.device(target.device)
        state.exec_responses["show running-config"] = f"hostname x\n{target.credential.password}\n"
        return real_open(target)

    mock.open = recording_open
    runner, events = make_runner(ws_root, mock, history, secrets, app_paths)
    job_id = runner.run(RunRequest(ADD_VLAN, RunMode.CHECK, prompts=VLAN))
    assert history.get_job(job_id).status == JobStatus.COMPLETED
    assert used == {"sa-as01": "siteadmin", "sa-as02": "as02admin"}
    stored = repr([vars(d) for d in history.devices(job_id)]) + repr(events)
    assert "Site-Pass-5151" not in stored and "Dev-Pass-6262" not in stored


def test_some_devices_failing_makes_the_job_partial(ws_root, history, secrets, app_paths):
    mock = mock_for(["sa-as01", "sa-as02"])
    mock.device("sa-as02").reject_lines.append(r"^name Cameras$")  # sa-as01 is the canary
    runner, _ = make_runner(ws_root, mock, history, secrets, app_paths)
    job_id = runner.run(RunRequest(ADD_VLAN, RunMode.APPLY, prompts=VLAN))
    assert statuses(history, job_id) == {
        "sa-as01": DeviceStatus.SUCCESS,
        "sa-as02": DeviceStatus.ROLLED_BACK,
    }
    assert history.get_job(job_id).status == JobStatus.PARTIAL


class _CancelledDuringConnect:
    """A transport that sees Cancel pressed while it connects, like the console wait."""

    def __init__(self, cancel: threading.Event, honour: bool) -> None:
        self.cancel, self.honour = cancel, honour

    def open(self, target):
        from tsunami.core.transports.base import ConnectCancelled, UnreachableError

        self.cancel.set()
        if self.honour and target.cancel is self.cancel:
            raise ConnectCancelled("COM3: cancelled while waiting for the console")
        raise UnreachableError("COM3: no console prompt after 300s")


def test_cancel_after_a_device_already_timed_out_leaves_the_job_failed(
    ws_root, history, secrets, app_paths
):
    cancel = threading.Event()
    transport = _CancelledDuringConnect(cancel, honour=False)
    runner, _ = make_runner(ws_root, transport, history, secrets, app_paths, cancel=cancel)
    job_id = runner.run(RunRequest(ADD_VLAN, RunMode.CHECK, prompts=VLAN, limit=["sa-as01"]))
    assert statuses(history, job_id) == {"sa-as01": DeviceStatus.UNREACHABLE}
    assert history.get_job(job_id).status == JobStatus.FAILED  # not "cancelled"


def test_cancel_during_the_connect_marks_device_and_job_cancelled(
    ws_root, history, secrets, app_paths
):
    cancel = threading.Event()
    transport = _CancelledDuringConnect(cancel, honour=True)
    runner, _ = make_runner(ws_root, transport, history, secrets, app_paths, cancel=cancel)
    job_id = runner.run(RunRequest(ADD_VLAN, RunMode.CHECK, prompts=VLAN, limit=["sa-as01"]))
    assert statuses(history, job_id) == {"sa-as01": DeviceStatus.CANCELLED}
    assert history.get_job(job_id).status == JobStatus.CANCELLED
