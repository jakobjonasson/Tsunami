"""Inventory editing from the GUI (core/inventory_edit.py).

Deletions go to a fake Recycle Bin here; the real one is exercised only in CI.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from tsunami.core.inventory_edit import InventoryEditError, InventoryEditor, send_to_recycle_bin
from tsunami.core.workspace import load_workspace

FIXTURE = Path(__file__).parents[1] / "fixtures" / "workspace"
ACME = Path("inventory/customers/acme")


@pytest.fixture
def root(tmp_path) -> Path:
    root = tmp_path / "ws"
    shutil.copytree(FIXTURE, root)
    return root


@pytest.fixture
def recycled() -> list[Path]:
    return []


@pytest.fixture
def editor(root, recycled, tmp_path) -> InventoryEditor:
    def fake_recycle(path: Path) -> None:
        recycled.append(path)
        shutil.move(str(path), str(tmp_path / f"bin-{len(recycled)}"))

    return InventoryEditor(root, recycle=fake_recycle)


def clean(root: Path):
    ws = load_workspace(root)
    assert ws.errors == [], ws.errors
    return ws


ROUTER = {
    "name": "sa-rt01",
    "host": "203.0.113.30",
    "site": "site-a",
    "role": "router",
    "platform": "ios",
}


# -- devices --------------------------------------------------------------------------


def test_add_device_keeps_comments_and_validates(root, editor):
    devices = root / ACME / "devices.yml"
    devices.write_text("# managed by the network team\n" + devices.read_text(encoding="utf-8"))
    editor.add_device("acme", {**ROUTER, "port": 22, "credential": "acme-rt"})
    text = devices.read_text(encoding="utf-8")
    assert text.startswith("# managed by the network team\n")
    assert "- name: sa-rt01\n  host: 203.0.113.30\n  site: site-a\n  role: router\n" in text
    assert "port:" not in text.split("sa-rt01")[1]  # the default stays implicit
    device = clean(root).customers["acme"].device("sa-rt01")
    assert device.role == "router" and device.credential == "acme-rt"


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"name": "sa-as01"}, "already used"),
        ({"site": "site-z"}, "site 'site-z' does not exist"),
        ({"role": "firewall"}, "not a device type"),
        ({"host": "bad host!"}, "host"),
        ({"platform": "nxos"}, "platform"),
        ({"model": "no-such-model"}, "not in the catalog"),
    ],
)
def test_bad_devices_are_refused_without_writing(root, editor, change, message):
    before = (root / ACME / "devices.yml").read_bytes()
    with pytest.raises(InventoryEditError, match=message):
        editor.add_device("acme", {**ROUTER, **change})
    assert (root / ACME / "devices.yml").read_bytes() == before


def test_update_device_sets_and_clears_fields(root, editor):
    editor.update_device("acme", "sa-as02", {"credential": "acme-as02", "role": "distribution"})
    acme = clean(root).customers["acme"]
    assert acme.device("sa-as02").credential == "acme-as02"
    assert acme.device("sa-as02").role == "distribution"
    text = (root / ACME / "devices.yml").read_text(encoding="utf-8")
    part = text.split("- name: sa-as02")[1]
    assert part.index("role:") < part.index("platform:") < part.index("credential:")
    editor.update_device("acme", "sa-as02", {"credential": None, "name": "sa-as02b"})
    acme = clean(root).customers["acme"]
    assert acme.device("sa-as02") is None and acme.device("sa-as02b").credential is None
    with pytest.raises(InventoryEditError, match="required"):
        editor.update_device("acme", "sa-as01", {"host": None})


def test_delete_device(root, editor):
    editor.delete_device("acme", "sa-as02")
    assert clean(root).customers["acme"].device("sa-as02") is None
    with pytest.raises(InventoryEditError, match="unknown device"):
        editor.delete_device("acme", "sa-as02")


# -- sites ----------------------------------------------------------------------------


def test_create_site_with_default_login(root, editor):
    editor.create_site("acme", "site-b", credential="acme-site-b", vars_text="# site B\nfloor: 2\n")
    assert (root / ACME / "sites/site-b.yml").read_text(encoding="utf-8") == "# site B\nfloor: 2\n"
    ws = clean(root)
    assert ws.customers["acme"].config.site_credentials == {"site-b": "acme-site-b"}
    editor.add_device("acme", {**ROUTER, "site": "site-b"})
    acme = clean(root).customers["acme"]
    assert acme.credential_for(acme.device("sa-rt01")) == "acme-site-b"


def test_create_site_refuses_bad_input(root, editor):
    with pytest.raises(InventoryEditError, match="already exists"):
        editor.create_site("acme", "site-a")
    with pytest.raises(InventoryEditError, match="lowercase"):
        editor.create_site("acme", "Site B")
    with pytest.raises(InventoryEditError):
        editor.create_site("acme", "site-b", vars_text="Bad-Name: 1\n")
    assert not (root / ACME / "sites/site-b.yml").exists()


def test_rename_site_moves_devices_and_default_login(root, editor):
    editor.update_site("acme", "site-a", credential="acme-a", set_credential=True)
    editor.update_site("acme", "site-a", new_name="hq")
    acme = clean(root).customers["acme"]
    assert set(acme.sites) == {"hq"}
    assert {d.site for d in acme.devices} == {"hq"}
    assert acme.config.site_credentials == {"hq": "acme-a"}
    editor.update_site("acme", "hq", set_credential=True, credential=None)
    assert clean(root).customers["acme"].config.site_credentials == {}


def test_delete_site_needs_consent_for_its_devices(root, editor, recycled):
    with pytest.raises(InventoryEditError, match="still has devices: sa-as01, sa-as02"):
        editor.delete_site("acme", "site-a")
    editor.create_site("acme", "site-b", credential="b-login")
    editor.add_device("acme", {**ROUTER, "site": "site-b"})
    editor.delete_site("acme", "site-b", delete_devices=True)
    acme = clean(root).customers["acme"]
    assert "site-b" not in acme.sites and acme.device("sa-rt01") is None
    assert acme.config.site_credentials == {}
    assert recycled == [root / ACME / "sites/site-b.yml"]


# -- customers ------------------------------------------------------------------------


def test_create_and_edit_customer(root, editor):
    editor.create_customer("globex", name="Globex Corp", credential="globex-admin")
    editor.create_site("globex", "main")
    editor.add_device("globex", {**ROUTER, "site": "main"})
    ws = clean(root)
    assert ws.customers["globex"].config.name == "Globex Corp"
    editor.update_customer(
        "globex",
        name="Globex",
        lab=True,
        jump_host={"host": "198.51.100.5", "credential": "globex-jump"},
    )
    config = clean(root).customers["globex"].config
    assert config.lab and config.jump_host.host == "198.51.100.5"
    editor.update_customer("globex", jump_host=None)
    assert clean(root).customers["globex"].config.jump_host is None
    with pytest.raises(InventoryEditError, match="already exists"):
        editor.create_customer("globex", name="x", credential="y")
    with pytest.raises(InventoryEditError, match="cannot edit"):
        editor.update_customer("globex", vars={"a": 1})


def test_customer_edit_keeps_comments(root, editor):
    path = root / ACME / "customer.yml"
    path.write_text("# Acme: main contract\n" + path.read_text(encoding="utf-8"))
    editor.update_customer("acme", credential="acme-new")
    assert path.read_text(encoding="utf-8").startswith("# Acme: main contract\n")
    assert clean(root).customers["acme"].config.credential == "acme-new"


def test_delete_customer_goes_to_the_recycle_bin(root, editor, recycled):
    editor.delete_customer("acme")
    assert recycled == [root / ACME]
    assert "acme" not in load_workspace(root).customers


@pytest.mark.skipif(not os.environ.get("CI"), reason="uses the real Recycle Bin; CI only")
def test_real_recycle_bin(tmp_path):
    victim = tmp_path / "tsunami-recycle-test.txt"
    victim.write_text("x")
    send_to_recycle_bin(victim)
    assert not victim.exists()
