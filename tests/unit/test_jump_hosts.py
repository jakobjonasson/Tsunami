"""Jump hosts per site and per device (design.md, Jump hosts): precedence, null/none,
the editor round trip, and Trust host keys with several jump hosts."""

from __future__ import annotations

import shutil
from pathlib import Path

import paramiko
import pytest

from tsunami.core.hostkey_trust import JUMP_HOST, DeviceAddress, Outcome, trust_host_keys
from tsunami.core.inventory_edit import DIRECT, InventoryEditor
from tsunami.core.logins import allowed_logins, jump_logins
from tsunami.core.secret_store import LoginCredential
from tsunami.core.transports.base import HostKeyMismatchError, JumpTarget
from tsunami.core.workspace import load_workspace

FIXTURE = Path(__file__).parents[1] / "fixtures" / "workspace"
ACME = "inventory/customers/acme"
KEY = paramiko.ECDSAKey.generate()
JUMP = {"host": "198.51.100.10", "port": 2222, "credential": "bastion-a"}
OTHER = {"host": "198.51.100.20", "credential": "bastion-b"}


@pytest.fixture
def root(tmp_path) -> Path:
    target = tmp_path / "ws"
    shutil.copytree(FIXTURE, target)
    (target / ACME / "sites" / "site-b.yml").write_text("{}\n", encoding="utf-8")
    return target


def editor(root: Path) -> InventoryEditor:
    return InventoryEditor(root, recycle=lambda path: None)


def acme(root: Path):
    ws = load_workspace(root)
    assert ws.errors == [], ws.errors
    return ws.customers["acme"]


def test_a_device_with_no_setting_uses_the_customers_jump_host(root):
    editor(root).update_customer("acme", jump_host={"host": "198.51.100.1", "credential": "c"})
    inv = acme(root)
    jh, source = inv.jump_with_source(inv.device("sa-as01"))
    assert (str(jh.host), source) == ("198.51.100.1", "customer")


def test_precedence_is_device_then_site_then_customer(root):
    ed = editor(root)
    ed.update_customer("acme", jump_host={"host": "198.51.100.1", "credential": "c"})
    ed.update_site("acme", "site-a", jump_host=JUMP)
    inv = acme(root)
    assert inv.jump_with_source(inv.device("sa-as01"))[1] == "site"
    assert inv.jump_for(inv.device("sa-as01")).port == 2222
    ed.update_device("acme", "sa-as01", {"jump_host": OTHER})
    inv = acme(root)
    jh, source = inv.jump_with_source(inv.device("sa-as01"))
    assert (str(jh.host), source) == ("198.51.100.20", "device")
    assert inv.jump_with_source(inv.device("sa-as02"))[1] == "site"


def test_a_site_without_a_device_uses_its_own_jump_host_else_the_customers(root):
    # Discovery connects to a site's addresses before they are devices.
    ed = editor(root)
    ed.update_customer("acme", jump_host={"host": "198.51.100.1", "credential": "c"})
    ed.update_site("acme", "site-a", jump_host=JUMP)
    ed.update_site("acme", "site-b", jump_host=DIRECT)
    inv = acme(root)
    assert inv.jump_for_site("site-a").port == 2222
    assert inv.jump_for_site("site-b") is None  # direct
    ed.update_site("acme", "site-b", jump_host=None)  # inherit again
    assert str(acme(root).jump_for_site("site-b").host) == "198.51.100.1"


@pytest.mark.parametrize("spelling", ["null", "none", "None"])
def test_null_or_none_at_site_or_device_means_direct(root, spelling):
    ed = editor(root)
    ed.update_customer("acme", jump_host={"host": "198.51.100.1", "credential": "c"})
    site = root / ACME / "sites" / "site-a.yml"
    site.write_text(site.read_text(encoding="utf-8") + f"jump_host: {spelling}\n", encoding="utf-8")
    inv = acme(root)
    assert inv.jump_for(inv.device("sa-as01")) is None
    assert inv.jump_with_source(inv.device("sa-as01"))[1] == "site"
    assert "jump_host" not in inv.sites["site-a"]  # a setting, not a variable
    ed.update_device("acme", "sa-as01", {"jump_host": JUMP})
    ed.update_device("acme", "sa-as02", {"jump_host": DIRECT})
    inv = acme(root)
    assert inv.jump_for(inv.device("sa-as01")).host == "198.51.100.10"
    assert inv.jump_for(inv.device("sa-as02")) is None


def test_a_site_jump_host_with_a_bad_host_is_reported_with_its_file(root):
    site = root / ACME / "sites" / "site-b.yml"
    site.write_text("jump_host:\n  host: 'bad host'\n  credential: x\n", encoding="utf-8")
    ws = load_workspace(root)
    assert any(i.file == Path(ACME) / "sites" / "site-b.yml" for i in ws.errors)


def test_existing_customer_level_config_is_unchanged(root):
    editor(root).update_customer("acme", jump_host={"host": "198.51.100.1", "credential": "c"})
    inv = acme(root)
    assert {str(inv.jump_for(d).host) for d in inv.devices} == {"198.51.100.1"}


def test_saving_site_variables_keeps_its_jump_host_and_clearing_removes_it(root):
    ed = editor(root)
    ed.update_site("acme", "site-a", jump_host=JUMP)
    ed.update_site("acme", "site-a", vars_text="gateway: 203.0.113.1\n")
    inv = acme(root)
    assert inv.site_jumps["site-a"].credential == "bastion-a"
    assert inv.sites["site-a"] == {"gateway": "203.0.113.1"}
    ed.update_site("acme", "site-a", jump_host=None)
    assert "site-a" not in acme(root).site_jumps


def test_jump_logins_are_never_pushable_but_are_listed(root):
    ed = editor(root)
    ed.update_site("acme", "site-a", jump_host=JUMP)
    ed.update_device("acme", "sa-as01", {"jump_host": OTHER})
    inv = acme(root)
    assert jump_logins(inv) == {"bastion-a", "bastion-b"}
    assert not jump_logins(inv) & allowed_logins(inv)


def test_two_jump_hosts_are_distinct_hosts_in_device_order(root):
    ed = editor(root)
    ed.update_site("acme", "site-a", jump_host=JUMP)
    ed.update_device("acme", "sa-as02", {"jump_host": OTHER})
    assert [str(j.host) for j in acme(root).jump_hosts()] == ["198.51.100.10", "198.51.100.20"]


class FakeJump:
    def __init__(self) -> None:
        self.channels: list[str] = []
        self.closed = False

    def acquire(self) -> None: ...

    def release(self) -> None: ...

    def channel(self, host: str, port: int):
        self.channels.append(host)
        return host

    def close(self) -> None:
        self.closed = True


DEVICES = [DeviceAddress(f"sw{i}", f"192.0.2.{i}") for i in range(1, 5)]


def jump_to(host: str) -> JumpTarget:
    return JumpTarget(host, 22, LoginCredential("u", "p"))


def test_each_device_is_reached_through_its_own_jump_host(app_paths, monkeypatch):
    import socket

    monkeypatch.setattr(socket, "create_connection", lambda addr, timeout: addr[0])
    made: dict[str, FakeJump] = {}

    def factory(target, store):
        made[target.host] = FakeJump()
        return made[target.host]

    jumps = {
        "sw1": jump_to("198.51.100.10"),
        "sw2": jump_to("198.51.100.10"),
        "sw3": jump_to("198.51.100.20"),
    }  # sw4 connects directly
    results = trust_host_keys(
        app_paths, "acme", DEVICES, jumps=jumps, probe=probe_any, jump_factory=factory
    )
    # One row per jump host (several, so each is named), then the devices in order.
    assert [r.device for r in results] == [
        f"{JUMP_HOST} 198.51.100.10:22",
        f"{JUMP_HOST} 198.51.100.20:22",
        *(d.name for d in DEVICES),
    ]
    assert {r.outcome for r in results} == {Outcome.RECORDED}
    assert sorted(made) == ["198.51.100.10", "198.51.100.20"]  # one connection per jump host
    assert sorted(made["198.51.100.10"].channels) == ["192.0.2.1", "192.0.2.2"]
    assert made["198.51.100.20"].channels == ["192.0.2.3"]
    assert all(j.closed for j in made.values())


def test_a_changed_jump_host_key_blocks_only_its_own_devices(app_paths, monkeypatch):
    import socket

    monkeypatch.setattr(socket, "create_connection", lambda addr, timeout: addr[0])

    def factory(target, store):
        if target.host == "198.51.100.10":
            raise HostKeyMismatchError("jump host 198.51.100.10: host key changed")
        return FakeJump()

    from tsunami.core.transports.hostkeys import HostKeyStore

    HostKeyStore(app_paths.known_hosts_file("acme")).check(
        "198.51.100.10", 22, paramiko.ECDSAKey.generate(), allow_new=True
    )
    jumps = {
        "sw1": jump_to("198.51.100.10"),
        "sw3": jump_to("198.51.100.20"),
    }
    results = trust_host_keys(
        app_paths, "acme", DEVICES, jumps=jumps, probe=probe_any, jump_factory=factory
    )
    rows = {r.device: r for r in results}
    assert rows[f"{JUMP_HOST} 198.51.100.10:22"].outcome == Outcome.CHANGED
    assert rows[f"{JUMP_HOST} 198.51.100.20:22"].outcome == Outcome.RECORDED
    assert rows["sw1"].outcome == Outcome.UNREACHABLE
    assert rows["sw3"].outcome == Outcome.RECORDED
    assert rows["sw2"].outcome == Outcome.RECORDED  # direct


def probe_any(opener, *, timeout, key_types=None, legacy=True):
    opener()
    return KEY
