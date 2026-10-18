"""The CSV device import dialog (gui/screens/import_devices.py), driven through its seams."""

from __future__ import annotations

from pathlib import Path

import pytest

from tsunami.core.inventory_edit import InventoryEditError
from tsunami.gui import theme
from tsunami.gui.screens import import_devices
from tsunami.gui.screens.import_devices import ImportDevicesDialog, open_import_dialog

SAMPLE = (
    "Hostname,IP Address,Site,Type,OS,Login\n"
    "sa-as03,203.0.113.13,site-a,Access switch,IOS-XE,acme-netadmin\n"  # ok
    "sa-as04,203.0.113.14,site-a,access,ios-xe,later-login\n"  # warning
    "sa-as01,203.0.113.11,site-a,access,ios-xe,\n"  # skipped
    "sa-as05,203.0.113.999,site-a,access,ios-xe,\n"  # error: bad IP
    "sb-as01,203.0.113.21,site-b,access,ios-xe,\n"  # error until sites are created
)


class Seams:
    """Replaces pick_csv_file and run_dialog: hand out a path, remember the dialog."""

    def __init__(self, monkeypatch, path: Path | None) -> None:
        self.path = path
        self.dialogs: list[ImportDevicesDialog] = []
        self.act = lambda dialog: None
        self.accepted = False
        monkeypatch.setattr(import_devices, "pick_csv_file", lambda parent=None: self.path)
        monkeypatch.setattr(import_devices, "run_dialog", self._run)

    def _run(self, dialog) -> bool:
        self.dialogs.append(dialog)
        self.act(dialog)
        return self.accepted


@pytest.fixture
def csv_path(tmp_path) -> Path:
    path = tmp_path / "devices.csv"
    path.write_text(SAMPLE, encoding="utf-8")
    return path


@pytest.fixture
def notes(main_window, monkeypatch) -> list[tuple[str, str]]:
    seen: list[tuple[str, str]] = []
    monkeypatch.setattr(main_window, "notify", lambda m, level="info": seen.append((level, m)))
    return seen


def cell(dialog, row: int, column: str) -> str:
    return dialog.preview_table.item(row, import_devices.COLUMNS.index(column)).text()


def statuses(dialog) -> list[str]:
    return [cell(dialog, r, "Status") for r in range(dialog.preview_table.rowCount())]


def open_dialog(gui_ctx, main_window, csv_path, **kwargs) -> ImportDevicesDialog:
    dialog = ImportDevicesDialog(gui_ctx, main_window, "acme", csv_path, **kwargs)
    return dialog


def test_dialog_maps_columns_and_previews_every_row(qtbot, gui_ctx, main_window, csv_path):
    dialog = open_dialog(gui_ctx, main_window, csv_path)
    qtbot.addWidget(dialog)
    assert dialog.mapping().columns == {
        "name": "Hostname",
        "host": "IP Address",
        "site": "Site",
        "role": "Type",
        "platform": "OS",
        "credential": "Login",
    }
    assert dialog.preview_table.rowCount() == 5
    assert statuses(dialog) == ["ok", "warning", "skipped", "error", "error"]
    assert [cell(dialog, r, "Row") for r in range(5)] == ["2", "3", "4", "5", "6"]
    assert cell(dialog, 0, "Name") == "sa-as03"
    assert cell(dialog, 0, "Type") == "Access switch"
    assert cell(dialog, 0, "Platform") == "ios-xe"  # normalised from IOS-XE
    assert "not in Credential Manager" in cell(dialog, 1, "Reason")
    assert "not a valid IP address" in cell(dialog, 3, "Reason")
    assert "unknown site 'site-b'" in cell(dialog, 4, "Reason")
    assert "comma separated" in dialog.file_label.text()


def test_status_colours_come_from_the_theme(qtbot, gui_ctx, main_window, csv_path):
    dialog = open_dialog(gui_ctx, main_window, csv_path)
    qtbot.addWidget(dialog)
    palette = theme.current()
    expected = [palette.success, palette.warning, palette.muted, palette.error, palette.error]
    status_column = import_devices.COLUMNS.index("Status")
    got = [
        dialog.preview_table.item(r, status_column).foreground().color().name() for r in range(5)
    ]
    assert got == expected


def test_import_button_counts_ready_rows(qtbot, gui_ctx, main_window, csv_path):
    dialog = open_dialog(gui_ctx, main_window, csv_path)
    qtbot.addWidget(dialog)
    assert dialog.import_button.text() == "Import 2 devices"
    assert dialog.import_button.isEnabled()
    dialog.create_sites_check.setChecked(True)
    assert statuses(dialog)[4] == "ok"
    assert dialog.import_button.text() == "Import 3 devices"
    assert "new site: site-b" in dialog.summary_label.text()
    # nothing ready: disabled, and no count
    dialog.set_fixed("platform", "junos")
    assert dialog.import_button.text() == "Import"
    assert not dialog.import_button.isEnabled()
    assert set(statuses(dialog)) == {"error"}
    dialog.set_fixed("platform", "ios")
    dialog.set_column("name", None)
    assert not dialog.import_button.isEnabled()
    assert "missing name" in cell(dialog, 0, "Reason")


def test_fixed_values_and_mapping_changes_refresh_the_preview(
    qtbot, gui_ctx, main_window, tmp_path
):
    path = tmp_path / "bare.csv"
    path.write_text("name;ip\nsa-as03;203.0.113.13\nsa-as04;203.0.113.14\n", encoding="utf-8")
    dialog = open_dialog(gui_ctx, main_window, path)
    qtbot.addWidget(dialog)
    assert "semicolon" in dialog.file_label.text()
    assert not dialog.import_button.isEnabled()  # site, type and platform are missing
    dialog.set_fixed("site", "site-a")
    dialog.set_fixed("role", "router")
    dialog.set_fixed("platform", "ArubaOS-CX")
    assert dialog.mapping().fixed == {"site": "site-a", "role": "router", "platform": "ArubaOS-CX"}
    assert dialog.fixed_edits["site"].isVisibleTo(dialog)
    assert not dialog.fixed_edits["name"].isVisibleTo(dialog)
    assert statuses(dialog) == ["ok", "ok"]
    assert dialog.import_button.text() == "Import 2 devices"


def test_site_given_by_the_caller_fixes_the_site_field(qtbot, gui_ctx, main_window, csv_path):
    dialog = open_dialog(gui_ctx, main_window, csv_path, site="site-a")
    qtbot.addWidget(dialog)
    assert dialog.mapping().fixed == {"site": "site-a"}
    assert "site" not in dialog.mapping().columns
    assert not dialog.combos["site"].isEnabled()
    assert not dialog.fixed_edits["site"].isEnabled()
    assert dialog.fixed_edits["site"].text() == "site-a"
    # the sb-as01 row now lands in site-a too
    assert statuses(dialog) == ["ok", "warning", "skipped", "error", "ok"]
    assert "site-a" in dialog.windowTitle()


def test_problem_filter_hides_good_rows(qtbot, gui_ctx, main_window, csv_path):
    dialog = open_dialog(gui_ctx, main_window, csv_path)
    qtbot.addWidget(dialog)
    dialog.problems_check.setChecked(True)
    hidden = [dialog.preview_table.isRowHidden(r) for r in range(5)]
    assert hidden == [True, False, True, False, False]


def test_import_writes_inventory_and_refreshes_workspace(
    qtbot, gui_ctx, main_window, csv_path, monkeypatch, notes
):
    seams = Seams(monkeypatch, csv_path)
    seams.accepted = True

    def act(dialog):
        dialog.create_sites_check.setChecked(True)
        assert dialog.save()

    seams.act = act
    assert open_import_dialog(gui_ctx, main_window, "acme") is True
    dialog = seams.dialogs[0]
    assert dialog.summary.imported == 3 and dialog.summary.sites_created == ["site-b"]
    qtbot.waitUntil(
        lambda: (
            gui_ctx.workspace is not None
            and "sb-as01" in {d.name for d in gui_ctx.workspace.customers["acme"].devices}
        ),
        timeout=15000,
    )
    devices = {d.name: d for d in gui_ctx.workspace.customers["acme"].devices}
    assert set(devices) == {"sa-as01", "sa-as02", "sa-as03", "sa-as04", "sb-as01"}
    assert devices["sa-as03"].credential == "acme-netadmin"
    assert devices["sa-as04"].credential == "later-login"  # added in Credentials later
    assert "site-b" in gui_ctx.workspace.customers["acme"].sites
    assert notes and notes[-1][0] == "info"
    assert "Imported 3 devices" in notes[-1][1]
    assert "created site site-b" in notes[-1][1]


def test_cancelled_picker_does_nothing(gui_ctx, main_window, monkeypatch, notes):
    seams = Seams(monkeypatch, None)
    assert open_import_dialog(gui_ctx, main_window, "acme") is False
    assert seams.dialogs == [] and notes == []


def test_password_column_is_refused_with_a_message(
    gui_ctx, main_window, tmp_path, monkeypatch, notes
):
    path = tmp_path / "secrets.csv"
    path.write_text(
        "name,host,site,type,platform,Password\n"
        "sa-as03,203.0.113.13,site-a,access,ios-xe,Hunter2-value\n",
        encoding="utf-8",
    )
    seams = Seams(monkeypatch, path)
    assert open_import_dialog(gui_ctx, main_window, "acme") is False
    assert seams.dialogs == []  # no dialog, so the values are never shown
    assert notes[0][0] == "error"
    assert "Add logins in Credentials" in notes[0][1]
    assert "Hunter2-value" not in notes[0][1]
    devices = (gui_ctx.workspace_path / "inventory/customers/acme/devices.yml").read_text()
    assert "sa-as03" not in devices


def test_save_with_nothing_ready_writes_nothing(qtbot, gui_ctx, main_window, tmp_path):
    path = tmp_path / "bad.csv"
    path.write_text("name,host,site,type,platform\nsa-x,203.0.113.999,site-a,access,ios\n")
    dialog = open_dialog(gui_ctx, main_window, path)
    qtbot.addWidget(dialog)
    assert dialog.save() is False
    assert not dialog.error_label.isHidden()
    assert dialog.summary is None


def test_editor_refusal_stays_inline(qtbot, gui_ctx, main_window, csv_path, monkeypatch):
    dialog = open_dialog(gui_ctx, main_window, csv_path)
    qtbot.addWidget(dialog)

    def refuse(*args, **kwargs):
        raise InventoryEditError("device name 'sa-as03' is already used")

    monkeypatch.setattr(import_devices, "apply_import", refuse)
    assert dialog.save() is False
    assert "already used" in dialog.error_label.text()
    assert not dialog.error_label.isHidden()


def test_choose_another_file_reloads_the_table(
    qtbot, gui_ctx, main_window, csv_path, tmp_path, monkeypatch
):
    other = tmp_path / "other.csv"
    other.write_text("name,host,site,type,platform\nsa-as09,192.0.2.9,site-a,core,ios\n")
    dialog = open_dialog(gui_ctx, main_window, csv_path)
    qtbot.addWidget(dialog)
    monkeypatch.setattr(import_devices, "pick_csv_file", lambda parent=None: other)
    assert dialog.change_file()
    assert dialog.preview_table.rowCount() == 1
    assert dialog.import_button.text() == "Import 1 device"
