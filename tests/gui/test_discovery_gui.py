"""Discovery tab, the example CSV, and the Settings allowlist button."""

from __future__ import annotations

import pytest

from tsunami.core.backups import list_backups
from tsunami.core.policy import load_policy
from tsunami.core.transports.base import AuthenticationError, ExecResult, UnreachableError
from tsunami.gui.screens import discovery, import_devices, settings
from tsunami.gui.screens.discovery import DiscoveryTab, open_discovery_tab

from .conftest import LOGIN_PASSWORD, all_widget_text

BANNER = "Cisco IOS Software [IOSXE], Linux Software, Version 17.18.2"


def cdp(name: str, address: str, caps: str = "Router Switch IGMP") -> str:
    return (
        f"-------------------------\nDevice ID: {name}\nEntry address(es): \n"
        f"  IP address: {address}\nPlatform: Linux Unix,  Capabilities: {caps} \n"
        "Interface: Ethernet0/1,  Port ID (outgoing port): Ethernet0/0\n\n"
        f"Version :\n{BANNER}\n\nManagement address(es): \n  IP address: {address}\n"
    )


class Session:
    def __init__(self, hostname: str, neighbors: list[str]) -> None:
        self.hostname, self.neighbors = hostname, neighbors

    def send_exec(self, command: str, **_: object) -> ExecResult:
        if command == "show version":
            return ExecResult(command, f"{BANNER}\n{self.hostname} uptime is 2 days\n")
        if command == "show cdp neighbors detail":
            return ExecResult(command, "\n".join(self.neighbors))
        return ExecResult(command, "% LLDP is not enabled")

    def running_config(self) -> str:
        return f"hostname {self.hostname}"

    def close(self) -> None:
        pass


class Transport:
    """Devices by address; the targets it was asked for are kept."""

    def __init__(self, devices: dict) -> None:
        self.devices = devices
        self.targets: list = []

    def open(self, target):
        self.targets.append(target)
        device = self.devices.get(target.host)
        if device is None:
            raise UnreachableError(f"{target.device}: connection timed out")
        if isinstance(device, Exception):
            raise device
        return Session(*device)


@pytest.fixture
def notes(main_window, monkeypatch):
    seen: list[tuple[str, str]] = []
    monkeypatch.setattr(main_window, "notify", lambda m, level="info": seen.append((level, m)))
    return seen


def use(gui_ctx, transport) -> Transport:
    gui_ctx._transport_factory = lambda: transport
    return transport


def wait_done(qtbot, tab: DiscoveryTab) -> None:
    qtbot.waitUntil(lambda: not tab.running, timeout=15000)


def column(table, name: str, columns: list[str]) -> list[str]:
    col = columns.index(name)
    return [table.item(r, col).text() for r in range(table.rowCount())]


def acme_device(gui_ctx, name: str):
    return gui_ctx.workspace.customers["acme"].device(name)


# -- discovery ---------------------------------------------------------------------------


def test_discover_adds_the_chain_and_lists_unlearned_neighbors(qtbot, gui_ctx, main_window, notes):
    transport = use(
        gui_ctx,
        Transport(
            {
                "192.0.2.1": (
                    "new-sw1",
                    [cdp("new-sw2.lab", "192.0.2.2"), cdp("far", "198.51.100.5")],
                ),
                "192.0.2.2": (
                    "NEW-SW2",
                    [cdp("new-sw1.lab", "192.0.2.1"), cdp("lost", "192.0.2.3")],
                ),
                "192.0.2.3": AuthenticationError("lost: login failed"),
            }
        ),
    )
    tab = open_discovery_tab(gui_ctx, main_window, "acme", "site-b")
    assert isinstance(tab, DiscoveryTab)
    assert not tab.site_devices_check.isEnabled()  # site-b has no devices yet
    tab.seeds_edit.setText("192.0.2.1")
    assert tab.scope_edit.text() == "192.0.2.0/24"  # follows the seeds until edited
    assert tab.login_combo.currentData() == "acme-netadmin"
    assert tab.start()
    wait_done(qtbot, tab)

    columns = discovery.DEVICE_COLUMNS
    assert column(tab.devices_table, "Device", columns) == ["new-sw1", "new-sw2", ""]
    assert column(tab.devices_table, "Status", columns) == ["added", "added", "login failed"]
    assert column(tab.devices_table, "Backup", columns) == ["first backup", "first backup", ""]
    assert [e.job_id for e in list_backups(gui_ctx.paths, "acme", "new-sw2")] == ["discovery"]
    unlearned = discovery.UNLEARNED_COLUMNS
    reasons = dict(
        zip(
            column(tab.unlearned_table, "Neighbor", unlearned),
            column(tab.unlearned_table, "Reason", unlearned),
            strict=True,
        )
    )
    assert reasons == {"far": "outside the scope", "lost": "could not be learned"}
    qtbot.waitUntil(lambda: acme_device(gui_ctx, "new-sw2") is not None, timeout=15000)
    assert acme_device(gui_ctx, "new-sw1").site == "site-b"
    assert any("2 added" in m for _, m in notes)
    assert LOGIN_PASSWORD not in all_widget_text(tab)
    assert all(t.allow_new_host_key for t in transport.targets)  # a person started it


def test_discover_needs_seeds_and_a_scope(gui_ctx, main_window):
    tab = open_discovery_tab(gui_ctx, main_window, "acme", "site-b")
    assert not tab.start()
    assert "seed" in tab.error_label.text()
    tab.seeds_edit.setText("192.0.2.1")
    tab.scope_edit.setText("")
    assert not tab.start()
    assert "scope" in tab.error_label.text()
    tab.scope_edit.setText("not-a-net")
    assert not tab.start()
    assert "not a network" in tab.error_label.text()


def test_another_login_is_written_on_the_devices(qtbot, gui_ctx, main_window):
    from tsunami.core.secret_store import LoginCredential

    gui_ctx.secrets.set_login("branch-admin", LoginCredential("branch", "Branch-Pass-1"))
    use(gui_ctx, Transport({"192.0.2.1": ("new-sw1", [])}))
    tab = open_discovery_tab(gui_ctx, main_window, "acme", "site-b")
    tab.seeds_edit.setText("192.0.2.1")
    tab.login_combo.setCurrentIndex(tab.login_combo.findData("branch-admin"))
    assert tab.start()
    wait_done(qtbot, tab)
    qtbot.waitUntil(lambda: acme_device(gui_ctx, "new-sw1") is not None, timeout=15000)
    assert acme_device(gui_ctx, "new-sw1").credential == "branch-admin"


def test_check_for_unlearned_neighbors_adds_nothing(qtbot, gui_ctx, main_window):
    use(
        gui_ctx,
        Transport(
            {
                "203.0.113.11": (
                    "sa-as01",
                    [cdp("sa-as02", "203.0.113.12"), cdp("rogue", "203.0.113.50")],
                ),
                "203.0.113.12": ("sa-as02", [cdp("phone", "203.0.113.60", caps="Host Phone")]),
            }
        ),
    )
    devices_file = gui_ctx.workspace_path / "inventory/customers/acme/devices.yml"
    before = devices_file.read_text(encoding="utf-8")
    tab = open_discovery_tab(gui_ctx, main_window, "acme", "site-a", check_only=True)
    assert tab.start_button.text() == "Check"
    assert tab.start()
    wait_done(qtbot, tab)
    assert column(tab.devices_table, "Status", discovery.DEVICE_COLUMNS) == [
        "in inventory",
        "in inventory",
    ]
    unlearned = discovery.UNLEARNED_COLUMNS
    neighbors = column(tab.unlearned_table, "Neighbor", unlearned)
    reasons = column(tab.unlearned_table, "Reason", unlearned)
    assert dict(zip(neighbors, reasons, strict=True)) == {
        "rogue": "not in the inventory",
        "phone": "not a switch or router",
    }
    assert devices_file.read_text(encoding="utf-8") == before


def test_inventory_menu_opens_the_discovery_tabs(main_window, monkeypatch):
    from tsunami.gui.screens.inventory import InventoryView

    view = next(
        p.findChild(InventoryView)
        for p in main_window._views.values()
        if p.findChild(InventoryView) is not None
    )
    opened = []
    monkeypatch.setattr(discovery, "open_discovery_tab", lambda *a, **k: opened.append((a[2:], k)))
    view.discover("acme", "site-a")
    view.check_neighbors("acme", "site-a")
    assert opened == [(("acme", "site-a"), {}), (("acme", "site-a"), {"check_only": True})]


# -- example CSV -------------------------------------------------------------------------


def test_save_example_writes_a_csv_where_the_person_chooses(
    tmp_path, main_window, notes, monkeypatch
):
    target = tmp_path / "example"
    monkeypatch.setattr(import_devices, "pick_save_path", lambda parent, suggested: target)
    path = import_devices.save_example(main_window, "site-a")
    assert path == target.with_suffix(".csv") and path.is_file()
    assert path.read_text(encoding="utf-8-sig").splitlines()[0] == (
        "name,host,site,type,platform,model,port,login"
    )
    assert any("Saved an example CSV" in m for _, m in notes)
    monkeypatch.setattr(import_devices, "pick_save_path", lambda parent, suggested: None)
    assert import_devices.save_example(main_window) is None


def test_import_dialog_offers_the_example(qtbot, tmp_path, gui_ctx, main_window, monkeypatch):
    csv_path = tmp_path / "d.csv"
    csv_path.write_text("name,host\n", encoding="utf-8")
    dialog = import_devices.ImportDevicesDialog(
        gui_ctx, main_window, "acme", csv_path, site="site-a"
    )
    qtbot.addWidget(dialog)
    saved = []
    monkeypatch.setattr(
        import_devices, "save_example", lambda shell, site, parent: saved.append(site)
    )
    dialog.example_button.click()
    assert saved == ["site-a"]


# -- settings ----------------------------------------------------------------------------


def test_allowlist_button_opens_the_policy_at_the_show_allowlist(main_window, gui_ctx, monkeypatch):
    calls = []
    monkeypatch.setattr(
        main_window, "open_in_vscode", lambda path, line=None: calls.append((path, line))
    )
    tab = settings.open_settings(gui_ctx, main_window)
    tab.open_allowlists_button.click()
    path, line = calls[-1]
    assert path == gui_ctx.paths.policy_file
    assert path.read_text(encoding="utf-8").splitlines()[line - 1].startswith("show_allowlist:")


def test_allowlist_button_warns_about_unsaved_edits(main_window, gui_ctx, monkeypatch, notes):
    monkeypatch.setattr(main_window, "open_in_vscode", lambda path, line=None: None)
    tab = settings.open_settings(gui_ctx, main_window)
    tab.rate_limit.setValue(tab.rate_limit.value() + 1)
    tab.open_allowlists()
    assert any(level == "warning" and "unsaved" in m for level, m in notes)


def test_inventory_edits_switch_is_saved_to_the_policy(main_window, gui_ctx):
    tab = settings.open_settings(gui_ctx, main_window)
    assert not tab.inventory_edits.isChecked()
    tab.inventory_edits.setChecked(True)
    assert tab.save_all()
    assert load_policy(gui_ctx.paths.policy_file).inventory_edits is True


def test_discovery_without_backups(qtbot, gui_ctx, main_window):
    use(gui_ctx, Transport({"192.0.2.1": ("new-sw1", [])}))
    tab = open_discovery_tab(gui_ctx, main_window, "acme", "site-b")
    assert tab.backup_check.isChecked()  # on by default
    tab.backup_check.setChecked(False)
    tab.seeds_edit.setText("192.0.2.1")
    assert tab.start()
    wait_done(qtbot, tab)
    assert column(tab.devices_table, "Backup", discovery.DEVICE_COLUMNS) == [""]
    assert list_backups(gui_ctx.paths, "acme", "new-sw1") == []


def test_check_only_offers_no_backup(gui_ctx, main_window):
    tab = open_discovery_tab(gui_ctx, main_window, "acme", "site-a", check_only=True)
    assert not tab.build_request().backup
