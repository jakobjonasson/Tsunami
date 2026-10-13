"""IOS rollback copy: the device's default file system, free space, cleanup, error text."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from tsunami.core import rollback
from tsunami.core.history import History
from tsunami.core.models.jobs import DeviceStatus
from tsunami.core.models.play import RunMode
from tsunami.core.rollback import RollbackError, default_filesystem, device_error
from tsunami.core.runner import Runner, RunRequest
from tsunami.core.secret_store import KeyringSecretStore, LoginCredential
from tsunami.core.transports.mock import MockTransport
from tsunami.core.workspace import load_workspace

FIXTURE = Path(__file__).parents[1] / "fixtures" / "workspace"
ADD_VLAN = "plays/push/add-vlan.yml"
VLAN = {"vlan_id": "30", "vlan_name": "Cameras"}

IOS_XE = """File Systems:

       Size(b)       Free(b)      Type  Flags  Prefixes
             -             -    opaque     rw   system:
*   7194652672    6355730432      disk     rw   bootflash: flash:
     1652318208    1530556416      disk     ro   webui:
"""
IOL = """File Systems:

     Size(b)     Free(b)      Type  Flags  Prefixes
*          0           0    unix     rw   unix:
           -           -  opaque     rw   system:
"""
STACK = """File Systems:

       Size(b)       Free(b)      Type  Flags  Prefixes
*   1621966848    1426690048      disk     rw   flash-1: flash:
     1621966848    1426690048      disk     rw   flash-2:
"""
ISR = """File Systems:

     Size(b)     Free(b)      Type  Flags  Prefixes
*  255744000   190550016      disk     rw   flash0: flash:#
           -           -    opaque     rw   archive:
"""


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        (IOS_XE, ("bootflash:", 6355730432)),
        (IOL, ("unix:", None)),
        (STACK, ("flash-1:", 1426690048)),
        (ISR, ("flash0:", 190550016)),
        ("*  -  -  disk  rw  flash:\n", ("flash:", None)),
    ],
)
def test_default_filesystem_is_the_starred_line(output, expected):
    assert default_filesystem(output) == expected


def test_no_starred_file_system_says_so_with_the_device_text():
    with pytest.raises(RollbackError, match=r"no default \(\*\).*Invalid input"):
        default_filesystem("show file systems\n% Invalid input detected at '^' marker.\nR1#")


def test_device_error_quotes_the_error_never_just_the_prompt():
    out = "copy running-config flash:x.cfg\n%Error opening flash:x.cfg (Invalid argument)\ntb-r1#"
    assert device_error(out) == "%Error opening flash:x.cfg (Invalid argument)"
    assert device_error("something odd happened\ntb-r1#") == "something odd happened"
    assert device_error("tb-r1#") == "no error text from the device"


# -- through the runner, against the mock device ---------------------------------------


@pytest.fixture
def run(tmp_path, app_paths):
    root = tmp_path / "ws"
    shutil.copytree(FIXTURE, root)
    history = History(tmp_path / "history.db")
    secrets = KeyringSecretStore(index_path=tmp_path / "idx.json")
    secrets.set_login("acme-netadmin", LoginCredential("netadmin", "Login-Pass-1234"))
    mock = MockTransport()
    for name in ("sa-as01", "sa-as02"):
        state = mock.add_device(name, running=[f"hostname {name}"])
        state.exec_responses["re:^show vlan id"] = "30 Cameras active"

    def go(mode: RunMode) -> list:
        runner = Runner(
            load_workspace(root),
            transport=mock,
            history=history,
            secrets=secrets,
            paths=app_paths,
            heartbeat_seconds=0.05,
        )
        job = runner.run(RunRequest(ADD_VLAN, mode, prompts=VLAN, limit=["sa-as01"]))
        return history.devices(job)

    go.mock = mock
    yield go
    history.close()


def test_apply_on_an_iol_style_device_uses_its_file_system_and_cleans_up(run):
    state = run.mock.device("sa-as01")
    state.filesystem = "unix:"
    [device] = run(RunMode.APPLY)
    assert device.status == DeviceStatus.SUCCESS
    sent = [text for kind, text in state.sent if kind == "exec"]
    assert any(c.startswith("copy running-config unix:tsunami-") for c in sent)
    assert any(c.startswith("delete /force unix:tsunami-") for c in sent)
    assert state.flash == {}  # snapshot removed
    assert "vlan 30" in state.running


def test_check_fails_a_device_without_room_for_the_rollback_copy(run):
    state = run.mock.device("sa-as01")
    state.fs_free = 1000
    [device] = run(RunMode.CHECK)
    assert device.status == DeviceStatus.FAILED
    assert "rollback copy not possible" in device.error_redacted
    assert "flash: has 1000 bytes free" in device.error_redacted
    assert not any(kind == "config" for kind, _ in state.sent)


def test_check_passes_with_room_and_never_writes_to_the_device(run):
    state = run.mock.device("sa-as01")
    [device] = run(RunMode.CHECK)
    assert device.status == DeviceStatus.SUCCESS
    sent = [text for kind, text in state.sent if kind == "exec"]
    assert "show file systems" in sent
    assert not any(c.startswith(("copy ", "delete ")) for c in sent)
    assert state.flash == {}


def test_apply_without_room_fails_before_any_change(run):
    state = run.mock.device("sa-as01")
    state.fs_free = 1000
    before = list(state.running)
    [device] = run(RunMode.APPLY)
    assert device.status == DeviceStatus.FAILED
    assert "rollback preparation failed" in device.error_redacted
    assert state.running == before


def test_rollback_deletes_its_snapshot_after_reverting(run):
    state = run.mock.device("sa-as01")
    state.reject_lines.append(r"^name Cameras$")
    before = list(state.running)
    [device] = run(RunMode.APPLY)
    assert device.status == DeviceStatus.ROLLED_BACK
    assert state.running == before
    assert state.flash == {}


def test_a_failed_cleanup_after_success_is_a_warning_not_a_rollback(run, monkeypatch):
    def refuse(self, session, prepared):
        raise RollbackError("'delete /force flash:x' failed: %Error deleting flash:x")

    monkeypatch.setattr(rollback.IosRollback, "cleanup", refuse)
    state = run.mock.device("sa-as01")
    [device] = run(RunMode.APPLY)
    assert device.status == DeviceStatus.SUCCESS
    assert "vlan 30" in state.running
    assert "could not delete the rollback copy" in device.error_redacted
    assert "%Error deleting" in device.error_redacted


def test_a_failed_copy_reports_the_device_error_not_its_prompt(run, monkeypatch):
    def wrong_guess(self, session, mode, version=None, config_size=None):
        self.filesystem = "flash:"  # what Tsunami used to assume

    monkeypatch.setattr(rollback.IosRollback, "preflight", wrong_guess)
    run.mock.device("sa-as01").filesystem = "unix:"
    [device] = run(RunMode.APPLY)
    assert device.status == DeviceStatus.FAILED
    assert "%Error opening flash:tsunami-" in device.error_redacted
    assert "(Invalid argument)" in device.error_redacted
    assert not device.error_redacted.rstrip().endswith("#")
