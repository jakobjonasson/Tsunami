"""Global plays (customer: global) and copying a play to another customer."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from tsunami.core.history import History
from tsunami.core.issues import TsunamiError
from tsunami.core.models.jobs import DeviceStatus, JobStatus
from tsunami.core.models.play import RunMode
from tsunami.core.play_edit import copy_play
from tsunami.core.render.secret_refs import SecretCollector, SecretRefError
from tsunami.core.runner import Runner, RunRequest
from tsunami.core.secret_store import KeyringSecretStore, LoginCredential
from tsunami.core.services import play_entry, render_play
from tsunami.core.transports.mock import MockTransport
from tsunami.core.workspace import load_workspace

FIXTURE = Path(__file__).parents[1] / "fixtures" / "workspace"
GLOBAL_PLAY = "plays/push/global-vlan.yml"
VLAN = {"vlan_id": "30", "vlan_name": "Cameras"}


@pytest.fixture
def root(tmp_path) -> Path:
    root = tmp_path / "ws"
    shutil.copytree(FIXTURE, root)
    text = (root / "plays/push/add-vlan.yml").read_text(encoding="utf-8")
    (root / GLOBAL_PLAY).write_text(
        text.replace("name: Add VLAN", "name: Add VLAN (any customer)").replace(
            "customer: acme", "customer: global"
        ),
        encoding="utf-8",
    )
    return root


def test_global_play_loads_without_a_customer(root):
    ws = load_workspace(root)
    assert ws.errors == []
    assert ws.plays[Path(GLOBAL_PLAY)].play.customer == "global"


def test_global_play_binds_to_the_customer_it_runs_for(root):
    ws = load_workspace(root)
    with pytest.raises(TsunamiError, match="global play: choose the customer"):
        play_entry(ws, GLOBAL_PLAY)
    entry = play_entry(ws, GLOBAL_PLAY, "acme")
    assert entry.play.customer == "acme"
    assert ws.plays[Path(GLOBAL_PLAY)].play.customer == "global"  # the workspace is unchanged
    with pytest.raises(TsunamiError, match="unknown customer"):
        play_entry(ws, GLOBAL_PLAY, "nobody")
    with pytest.raises(TsunamiError, match="is for 'acme'"):
        play_entry(ws, "plays/push/add-vlan.yml", "other")
    rendering = render_play(ws, GLOBAL_PLAY, device="sa-as01", prompts=VLAN, customer="acme")
    assert rendering.customer == "acme" and "vlan 30" in rendering.masked_lines()


def test_global_play_runs_and_the_job_belongs_to_the_customer(root, tmp_path, app_paths):
    history = History(tmp_path / "history.db")
    secrets = KeyringSecretStore(index_path=tmp_path / "idx.json")
    secrets.set_login("acme-netadmin", LoginCredential("netadmin", "Login-Pass-1"))
    mock = MockTransport()
    for name in ("sa-as01", "sa-as02"):
        mock.add_device(name, running=[f"hostname {name}"]).exec_responses["re:^show vlan id"] = (
            "30 Cameras active"
        )
    runner = Runner(
        load_workspace(root),
        transport=mock,
        history=history,
        secrets=secrets,
        paths=app_paths,
        heartbeat_seconds=0.05,
    )
    job_id = runner.run(RunRequest(GLOBAL_PLAY, RunMode.CHECK, prompts=VLAN, customer="acme"))
    job = history.get_job(job_id)
    assert job.status == JobStatus.COMPLETED and job.customer == "acme"
    assert job.play_path == GLOBAL_PLAY
    assert {d.status for d in history.devices(job_id)} == {DeviceStatus.SUCCESS}
    history.close()


def test_name_only_secret_resolves_to_the_plays_customer():
    collector = SecretCollector("acme")
    collector("tacacs")
    collector("acme/radius")
    assert collector.refs == ["acme/tacacs", "acme/radius"]
    with pytest.raises(SecretRefError, match="another customer"):
        collector("other/tacacs")


def test_customer_folder_named_global_is_refused(root):
    shutil.copytree(root / "inventory/customers/acme", root / "inventory/customers/global")
    codes = {i.code for i in load_workspace(root).errors}
    assert "inventory.reserved_customer" in codes


def test_copy_play_to_another_customer(root):
    (root / "inventory/customers/acme").rename(root / "inventory/customers/zeta")
    shutil.copytree(root / "inventory/customers/zeta", root / "inventory/customers/acme")
    baseline = root / "plays/baseline/acme-baseline.yml"
    baseline.write_text(
        "# keep me\n"
        + baseline.read_text(encoding="utf-8")
        + "  - config:\n      - \" key {{ secret('acme/radius') }}\"\n",
        encoding="utf-8",
    )
    new = copy_play(root, "plays/baseline/acme-baseline.yml", "zeta")
    assert new == Path("plays/baseline/acme-baseline-zeta.yml")
    text = (root / new).read_text(encoding="utf-8")
    assert text.startswith("# keep me\n") and "customer: zeta" in text
    assert "secret('radius')" in text and "acme/" not in text
    with pytest.raises(TsunamiError, match="already exists"):
        copy_play(root, "plays/baseline/acme-baseline.yml", "zeta")
    assert copy_play(root, GLOBAL_PLAY, "zeta", "zeta-vlan") == Path("plays/push/zeta-vlan.yml")
    with pytest.raises(TsunamiError, match="no live play"):
        copy_play(root, "drafts/plays/push/x.yml", "zeta")


def test_copy_a_customer_play_as_a_global_play(root):
    baseline = root / "plays/baseline/acme-baseline.yml"
    baseline.write_text(
        baseline.read_text(encoding="utf-8")
        + "  - config:\n      - \" key {{ secret('acme/radius') }}\"\n",
        encoding="utf-8",
    )
    new = copy_play(root, "plays/baseline/acme-baseline.yml", "global", "baseline")
    assert new == Path("plays/baseline/baseline.yml")
    text = (root / new).read_text(encoding="utf-8")
    assert "customer: global" in text
    assert "secret('radius')" in text and "acme/" not in text  # the customer's own secret
    ws = load_workspace(root)
    entry = ws.plays[new]
    assert entry.play is not None and entry.play.customer == "global"
    assert not [i for i in ws.errors if i.file and Path(i.file).name == "baseline.yml"]
    bound = play_entry(ws, new, "acme")  # runs for a customer like any global play
    assert bound.play.customer == "acme"


def test_a_play_that_names_devices_cannot_become_global(root):
    play = root / "plays/push/two-devices.yml"
    play.write_text(
        "name: Two devices\ntype: push\ncustomer: acme\n"
        "targets:\n  devices: [sa-as01, sa-as02]\n"
        "steps:\n  - config:\n      - ntp server 192.0.2.99\n",
        encoding="utf-8",
    )
    with pytest.raises(TsunamiError, match=r"names devices \(sa-as01, sa-as02\)"):
        copy_play(root, "plays/push/two-devices.yml", "global")
    assert not (root / "plays/push/two-devices-global.yml").exists()
