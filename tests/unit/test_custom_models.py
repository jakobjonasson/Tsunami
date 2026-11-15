"""Custom hardware models in inventory/models.yml (design.md, Hardware catalog)."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from tsunami.core.catalog import (
    CUSTOM_MODELS,
    builtin_catalog,
    load_catalog,
    load_custom_models,
    parse_custom_models,
)
from tsunami.core.inventory_edit import InventoryEditError, InventoryEditor
from tsunami.core.inventory_import import ColumnMapping, CsvTable, preview
from tsunami.core.workspace import load_workspace, secret_literal_keys

FIXTURE = Path(__file__).parents[1] / "fixtures" / "workspace"
IE3300 = {
    "id": "cisco-ie3300",
    "label": "Catalyst IE3300",
    "platform": "ios-xe",
    "model_pattern": "^IE-3300",
    "examples": ["IE-3300-8T2S"],
}


@pytest.fixture
def root(tmp_path):
    root = tmp_path / "ws"
    shutil.copytree(FIXTURE, root)
    return root


def test_a_custom_model_takes_vendor_type_and_speed_from_its_platform():
    found = parse_custom_models(
        [IE3300, {"id": "aruba-cx4100", "label": "CX 4100i", "platform": "aos-cx"}]
    )
    assert found.problems == ()
    ie, cx = found.entries
    assert (ie.vendor, ie.netmiko_device_type, ie.console_baud, ie.custom) == (
        "cisco",
        "cisco_xe",
        9600,
        True,
    )
    assert (cx.vendor, cx.console_baud, cx.model_pattern, cx.examples) == (
        "aruba",
        115200,
        None,
        [],
    )
    assert ie.matches_model("IE-3300-8T2S") and not cx.matches_model("anything")


@pytest.mark.parametrize(
    ("entry", "message"),
    [
        ({**IE3300, "id": "cisco-c9300"}, "built-in"),
        ({**IE3300, "platform": "junos"}, "platform"),
        ({**IE3300, "model_pattern": "(x"}, "not a valid regex"),
        ({**IE3300, "label": ""}, "label"),
        ({**IE3300, "console_baud": 1234}, "console_baud"),
        ({**IE3300, "colour": "blue"}, "colour"),
        ({**IE3300, "id": "Bad Id"}, "id"),
    ],
)
def test_bad_custom_models_are_left_out_with_a_reason(entry, message):
    found = parse_custom_models([entry, {**IE3300, "id": "ok-model"}])
    assert [e.id for e in found.entries] == ["ok-model"]
    [problem] = found.problems
    assert problem.index == 0 and message in problem.message


def test_a_duplicate_custom_id_is_left_out():
    found = parse_custom_models([IE3300, IE3300])
    assert len(found.entries) == 1 and "twice" in found.problems[0].message


def test_load_catalog_merges_the_workspace_models(root):
    assert load_catalog(root) is builtin_catalog()  # no models.yml
    InventoryEditor(root).add_model(IE3300)
    catalog = load_catalog(root)
    assert "cisco-ie3300" in catalog.ids() and "cisco-c9300" in catalog.ids()
    assert [e.id for e in catalog.custom()] == ["cisco-ie3300"]
    assert "cisco-ie3300" not in load_catalog().ids()
    # Edited by hand with the same size and no time passing: still read again.
    path = root / CUSTOM_MODELS
    path.write_text(path.read_text(encoding="utf-8").replace("IE3300", "IE3301"), "utf-8")
    assert load_catalog(root).get("cisco-ie3300").label == "Catalyst IE3301"


def test_the_workspace_reports_a_bad_models_file_and_keeps_the_good_entries(root):
    (root / CUSTOM_MODELS).write_text(
        "- id: cisco-ie3300\n  label: IE\n  platform: ios-xe\n"
        "- id: cisco-c9300\n  label: Shadow\n  platform: ios\n",
        encoding="utf-8",
    )
    ws = load_workspace(root, render=False)
    [issue] = [i for i in ws.issues if i.code == "models.invalid"]
    assert issue.file.as_posix() == "inventory/models.yml" and issue.line == 4
    assert "built-in" in issue.message
    assert "cisco-ie3300" in ws.catalog.ids()
    assert ws.catalog.get("cisco-c9300").label != "Shadow"
    (root / CUSTOM_MODELS).write_text("{not: a list}\n", encoding="utf-8")
    assert load_custom_models(root).problems[0].index is None


def test_devices_and_imports_can_name_a_custom_model(root):
    editor = InventoryEditor(root)
    with pytest.raises(InventoryEditError, match="not in the catalog"):
        editor.update_device("acme", "sa-as01", {"model": "cisco-ie3300"})
    editor.add_model(IE3300)
    editor.update_device("acme", "sa-as01", {"model": "cisco-ie3300"})
    ws = load_workspace(root, render=False)
    assert not [i for i in ws.errors if "model" in i.code]
    table = CsvTable(
        Path("x.csv"),
        ",",
        ["name", "host", "site", "role", "platform", "model"],
        [(1, ["sa-as07", "192.0.2.7", "site-a", "access", "ios-xe", "Catalyst IE3300"])],
    )
    mapping = ColumnMapping(columns={c: c for c in table.headers})
    [row] = preview(root, "acme", table, mapping).rows
    assert row.fields["model"] == "cisco-ie3300" and row.status != "error"


def test_editing_and_deleting_custom_models(root):
    editor = InventoryEditor(root)
    editor.add_model(IE3300)
    with pytest.raises(InventoryEditError, match="already exists"):
        editor.add_model(IE3300)
    with pytest.raises(InventoryEditError, match="built-in"):
        editor.add_model({**IE3300, "id": "cisco-c9300"})
    with pytest.raises(InventoryEditError, match="built in and cannot be changed"):
        editor.update_model("cisco-c9300", {"label": "x", "platform": "ios"})
    editor.update_device("acme", "sa-as01", {"model": "cisco-ie3300"})
    with pytest.raises(InventoryEditError, match="sa-as01"):
        editor.update_model("cisco-ie3300", {"label": "IE", "platform": "aos-cx"})
    with pytest.raises(InventoryEditError, match="used by acme/sa-as01"):
        editor.delete_model("cisco-ie3300")
    entry = editor.update_model(
        "cisco-ie3300", {**IE3300, "label": "IE 3300 rugged", "console_baud": 115200}
    )
    assert (entry.label, entry.console_baud) == ("IE 3300 rugged", 115200)
    editor.update_device("acme", "sa-as01", {"model": None})
    editor.delete_model("cisco-ie3300")
    assert editor.read_models() == []
    with pytest.raises(InventoryEditError, match="unknown custom model"):
        editor.delete_model("cisco-ie3300")


def test_the_models_file_keeps_comments(root):
    path = root / CUSTOM_MODELS
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# our rugged switches\n- id: a1\n  label: A1\n  platform: ios\n", "utf-8")
    InventoryEditor(root).add_model(IE3300)
    text = path.read_text(encoding="utf-8")
    assert text.startswith("# our rugged switches") and "cisco-ie3300" in text


@pytest.mark.parametrize(
    ("values", "found"),
    [
        ({"poe_enabled": True, "ntp_key_id": 5, "enable": False}, []),
        ({"snmp_community": "public"}, ["snmp_community"]),
        ({"snmp": {"auth_key": "x"}, "ok": "x"}, ["snmp.auth_key"]),
        ({"users": [{"password": "x"}]}, ["users.0.password"]),
        ({"api_token": "abc"}, ["api_token"]),
    ],
)
def test_secret_literal_keys(values, found):
    assert secret_literal_keys(values) == found
