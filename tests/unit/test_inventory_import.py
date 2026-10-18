"""CSV device import (core/inventory_import.py): parse, map, check every row, write once."""

from __future__ import annotations

import shutil
import time
from pathlib import Path

import pytest

from tsunami.core.inventory_edit import InventoryEditError, InventoryEditor
from tsunami.core.inventory_import import (
    ColumnMapping,
    ImportRefused,
    apply_import,
    auto_map,
    preview,
    read_csv,
    secret_headers,
)
from tsunami.core.workspace import load_workspace

FIXTURE = Path(__file__).parents[1] / "fixtures" / "workspace"
ACME = Path("inventory/customers/acme")
HEADER = "name,host,site,type,platform"
LOGINS = ["acme-netadmin"]


@pytest.fixture
def root(tmp_path) -> Path:
    root = tmp_path / "ws"
    shutil.copytree(FIXTURE, root)
    return root


def write_csv(tmp_path, text: str, *, name="devices.csv", encoding="utf-8") -> Path:
    path = tmp_path / name
    path.write_bytes(text.encode(encoding))
    return path


def run(root, tmp_path, text: str, mapping=None, **kwargs):
    table = read_csv(write_csv(tmp_path, text))
    mapping = mapping or auto_map(table.headers)
    kwargs.setdefault("known_logins", LOGINS)
    return preview(root, "acme", table, mapping, **kwargs)


def reasons(plan) -> dict[str, str]:
    return {str(r.fields["name"]): r.reason for r in plan.rows}


def statuses(plan) -> list[str]:
    return [r.status for r in plan.rows]


def clean(root: Path):
    ws = load_workspace(root)
    assert ws.errors == [], ws.errors
    return ws


# -- reading and mapping -----------------------------------------------------------------


def test_auto_map_aliases_ignore_case_and_spaces():
    mapping = auto_map(
        ["Device Name", "IP Address", "SITE", "device_type", "OS", "Model", "SSH Port", "Login"]
    )
    assert mapping.columns == {
        "name": "Device Name",
        "host": "IP Address",
        "site": "SITE",
        "role": "device_type",
        "platform": "OS",
        "model": "Model",
        "port": "SSH Port",
        "credential": "Login",
    }
    other = auto_map(["Hostname", "Mgmt IP", "Type", "Platform"]).columns
    assert other == {"name": "Hostname", "host": "Mgmt IP", "role": "Type", "platform": "Platform"}
    assert auto_map(["Device", "Management IP"]).columns == {
        "name": "Device",
        "host": "Management IP",
    }
    assert auto_map(["Address"]).columns == {"host": "Address"}
    assert auto_map(["colour", "notes"]).columns == {}


@pytest.mark.parametrize("delimiter", [";", "\t", ","])
def test_delimiters_are_sniffed(tmp_path, delimiter):
    text = delimiter.join(["name", "host", "site", "type", "platform"]) + "\n"
    text += delimiter.join(["sa-as03", "203.0.113.13", "site-a", "access", "ios-xe"]) + "\n"
    table = read_csv(write_csv(tmp_path, text))
    assert table.delimiter == delimiter
    assert table.headers == ["name", "host", "site", "type", "platform"]
    assert table.rows == [(2, ["sa-as03", "203.0.113.13", "site-a", "access", "ios-xe"])]


def test_bom_and_blank_lines_and_row_numbers(tmp_path):
    text = "﻿" + HEADER + "\n\nsa-as03,203.0.113.13,site-a,access,ios-xe\n,,,,\n"
    text += "sa-as04,203.0.113.14,site-a,access,ios-xe\n"
    table = read_csv(write_csv(tmp_path, text))
    assert table.headers[0] == "name"  # the BOM is not part of the first header
    assert [n for n, _ in table.rows] == [3, 5]


def test_unreadable_files_are_refused(tmp_path):
    with pytest.raises(ImportRefused, match="empty"):
        read_csv(write_csv(tmp_path, "\n\n"))
    with pytest.raises(ImportRefused, match="UTF-8"):
        read_csv(write_csv(tmp_path, "name\n\xe9\n", encoding="latin-1"))
    with pytest.raises(ImportRefused, match="Cannot read"):
        read_csv(tmp_path / "missing.csv")


@pytest.mark.parametrize(
    "header",
    [
        "Password",
        "password",
        "enable",
        "Enable Secret",
        "enable_password",
        "SSH Key",
        "api-key",
        "Token",
        "snmp community",
        "PSK",
        "NetworkPassword",
        "pass",
    ],
)
def test_secret_looking_column_refuses_the_whole_file(tmp_path, header):
    text = f"{HEADER},{header}\nsa-as03,203.0.113.13,site-a,access,ios-xe,Hunter2-value\n"
    with pytest.raises(ImportRefused, match="Add logins in Credentials") as caught:
        read_csv(write_csv(tmp_path, text))
    assert "Hunter2-value" not in str(caught.value)


def test_ordinary_headers_are_not_secrets():
    headers = ["name", "host", "site", "type", "platform", "model", "port", "login", "keyboard "]
    assert secret_headers(headers[:-1]) == []
    assert secret_headers(["Passport"]) == []


# -- checking rows -----------------------------------------------------------------------


def test_good_rows_are_ok(root, tmp_path):
    plan = run(
        root,
        tmp_path,
        HEADER + ",model,port,login\n"
        "sa-as03,203.0.113.13,site-a,Access switch,IOS-XE,cisco-c9300,2222,acme-netadmin\n"
        "sa-wl01,198.51.100.5,site-a,WLC,iosxe,,,\n"
        "sa-ap01,sa-ap01.example.net,site-a,AP,ArubaOS-CX,,,\n"
        "sa-rt01,2001:db8::1,site-a,router,ProCurve,,,\n"
        "sa-ds01,192.0.2.7,site-a,core,AOS-S,,,\n",
    )
    assert statuses(plan) == ["ok"] * 5
    devices = plan.devices()
    assert devices[0] == {
        "name": "sa-as03",
        "host": "203.0.113.13",
        "site": "site-a",
        "role": "access",
        "platform": "ios-xe",
        "model": "cisco-c9300",
        "port": 2222,
        "credential": "acme-netadmin",
    }
    assert [d["role"] for d in devices] == ["access", "wlc", "ap", "router", "core"]
    assert [d["platform"] for d in devices] == ["ios-xe", "ios-xe", "aos-cx", "aos-s", "aos-s"]
    assert plan.ready == 5 and plan.errors == 0 and plan.new_sites == []


def test_every_error_type(root, tmp_path):
    plan = run(
        root,
        tmp_path,
        HEADER + ",model,port\n"
        ",203.0.113.13,site-a,access,ios,,\n"  # 2 missing name
        "e-host,,site-a,access,ios,,\n"  # 3 missing host
        "Bad_Name,203.0.113.13,site-a,access,ios,,\n"  # 4 bad slug
        "e-ip,203.0.113.999,site-a,access,ios,,\n"  # 5 bad IP
        "e-dns,bad host!,site-a,access,ios,,\n"  # 6 bad hostname
        "e-plat,203.0.113.13,site-a,access,junos,,\n"  # 7 unknown platform
        "e-type,203.0.113.13,site-a,toaster,ios,,\n"  # 8 unknown type
        "e-site,203.0.113.13,site-z,access,ios,,\n"  # 9 unknown site
        "e-nosite,203.0.113.13,,access,ios,,\n"  # 10 missing site
        "e-model,203.0.113.13,site-a,access,ios,nope-9000,\n"  # 11 unknown model
        "e-mplat,203.0.113.13,site-a,access,ios,cisco-c9300,\n"  # 12 model on another platform
        "e-port,203.0.113.13,site-a,access,ios,,70000\n"  # 13 bad port
        "e-port2,203.0.113.13,site-a,access,ios,,ssh\n"  # 14 bad port
        "e-notype,203.0.113.13,site-a,,ios,,\n"  # 15 missing type
        "e-noplat,203.0.113.13,site-a,access,,,\n",  # 16 missing platform
    )
    assert statuses(plan) == ["error"] * 15
    by_row = {r.number: r.reason for r in plan.rows}
    assert by_row[2] == "missing name"
    assert by_row[3] == "missing host"
    assert "lowercase" in by_row[4] and "bad_name" not in by_row[4]
    assert "not a valid IP address" in by_row[5]
    assert "not a valid IP address or DNS name" in by_row[6]
    assert "unknown platform 'junos'" in by_row[7]
    assert "unknown type 'toaster'" in by_row[8]
    assert "unknown site 'site-z'" in by_row[9] and "Create missing sites" in by_row[9]
    assert by_row[10] == "missing site"
    assert "unknown model 'nope-9000'" in by_row[11]
    assert "runs ios-xe, not ios" in by_row[12]
    assert "bad port" in by_row[13] and "bad port" in by_row[14]
    assert by_row[15] == "missing type"
    assert by_row[16] == "missing platform"
    assert plan.ready == 0 and plan.errors == 15


def test_several_problems_in_one_row_are_all_listed(root, tmp_path):
    plan = run(root, tmp_path, HEADER + "\n,,site-z,toaster,junos\n")
    reason = plan.rows[0].reason
    for part in (
        "missing name",
        "missing host",
        "unknown site",
        "unknown type",
        "unknown platform",
    ):
        assert part in reason


def test_uppercase_name_suggests_the_slug(root, tmp_path):
    plan = run(root, tmp_path, HEADER + "\nSA-AS03,203.0.113.13,site-a,access,ios\n")
    assert plan.rows[0].status == "error"
    assert "try 'sa-as03'" in plan.rows[0].reason  # never guessed silently


def test_platform_spellings_are_conservative(root, tmp_path):
    ok = [
        "ios",
        "IOS",
        "IOS-XE",
        "iosxe",
        "IOS XE",
        "Cisco IOS-XE",
        "aos-cx",
        "ArubaOS-CX",
        "AOS-S",
        "ProCurve",
        "HP ProCurve",
    ]
    rows = "".join(f"d{i},192.0.2.{i + 1},site-a,access,{p}\n" for i, p in enumerate(ok))
    rows += "x1,192.0.2.50,site-a,access,xe\nx2,192.0.2.51,site-a,access,nxos\n"
    plan = run(root, tmp_path, HEADER + "\n" + rows)
    assert statuses(plan) == ["ok"] * len(ok) + ["error", "error"]


def test_type_labels_and_slugs(root, tmp_path):
    names = [
        "Core switch",
        "Distribution switch",
        "Access switch",
        "Wireless LAN controller",
        "Access point",
        "Router",
        "core",
        "DISTRIBUTION",
        "wlc",
        "ap",
        "Router ",
    ]
    rows = "".join(f"d{i},192.0.2.{i + 1},site-a,{t},ios\n" for i, t in enumerate(names))
    plan = run(root, tmp_path, HEADER + "\n" + rows)
    assert statuses(plan) == ["ok"] * len(names)
    assert [d["role"] for d in plan.devices()][:6] == [
        "core",
        "distribution",
        "access",
        "wlc",
        "ap",
        "router",
    ]


def test_custom_role_is_accepted(root, tmp_path):
    (root / "inventory" / "roles" / "firewall.yml").write_text("{}\n")
    plan = run(root, tmp_path, HEADER + "\nsa-fw01,192.0.2.9,site-a,firewall,ios\n")
    assert plan.rows[0].status == "ok" and plan.rows[0].fields["role"] == "firewall"


def test_unknown_login_is_only_a_warning(root, tmp_path):
    plan = run(
        root,
        tmp_path,
        HEADER + ",login\n"
        "sa-as03,203.0.113.13,site-a,access,ios,acme-netadmin\n"
        "sa-as04,203.0.113.14,site-a,access,ios,later-login\n"
        "sa-as05,203.0.113.15,site-a,access,ios,Bad Login\n",
    )
    assert statuses(plan) == ["ok", "warning", "error"]
    assert "not in Credential Manager" in plan.rows[1].reason
    assert plan.ready == 2 and plan.warnings == 1
    assert plan.devices()[1]["credential"] == "later-login"
    # without a login list the check is skipped
    plan = run(
        root,
        tmp_path,
        HEADER + ",login\nsa-as04,203.0.113.14,site-a,access,ios,x-y\n",
        known_logins=None,
    )
    assert plan.rows[0].status == "ok"


def test_duplicate_names_in_the_file_flag_every_copy(root, tmp_path):
    plan = run(
        root,
        tmp_path,
        HEADER + "\n"
        "sa-as03,203.0.113.13,site-a,access,ios\n"
        "sa-as04,203.0.113.14,site-a,access,ios\n"
        "sa-as03,203.0.113.99,site-a,access,ios\n",
    )
    assert statuses(plan) == ["error", "ok", "error"]
    assert "duplicate name in the file (also row 4)" in plan.rows[0].reason
    assert "also row 2" in plan.rows[2].reason


def test_name_already_in_inventory(root, tmp_path):
    plan = run(
        root,
        tmp_path,
        HEADER + "\n"
        "sa-as01,203.0.113.11,site-a,Access switch,IOS-XE\n"  # identical: skipped
        "sa-as02,203.0.113.77,site-a,access,ios-xe\n"  # other host
        "sa-as01,203.0.113.11,site-a,access,ios-xe\n",  # duplicate in the file wins
    )
    assert statuses(plan) == ["error", "error", "error"]
    plan = run(
        root,
        tmp_path,
        HEADER + "\n"
        "sa-as01,203.0.113.11,site-a,Access switch,IOS-XE\n"
        "sa-as02,203.0.113.77,site-a,core,ios\n"
        "sa-as09,203.0.113.19,site-a,access,ios\n",
    )
    assert statuses(plan) == ["skipped", "error", "ok"]
    assert plan.rows[0].reason == "already in the inventory with the same fields"
    assert "already in the inventory with a different host, type, platform" in plan.rows[1].reason
    assert (plan.ready, plan.skipped, plan.errors) == (1, 1, 1)


def test_optional_fields_only_count_when_the_row_has_them(root, tmp_path):
    same = run(root, tmp_path, HEADER + ",port\nsa-as01,203.0.113.11,site-a,access,ios-xe,\n")
    assert same.rows[0].status == "skipped"
    same = run(root, tmp_path, HEADER + ",port\nsa-as01,203.0.113.11,site-a,access,ios-xe,22\n")
    assert same.rows[0].status == "skipped"
    other = run(root, tmp_path, HEADER + ",port\nsa-as01,203.0.113.11,site-a,access,ios-xe,2222\n")
    assert other.rows[0].status == "error" and "port" in other.rows[0].reason


def test_fixed_values_fill_or_override_columns(root, tmp_path):
    text = "name,ip\nsa-as03,203.0.113.13\nsa-as04,203.0.113.14\n"
    table = read_csv(write_csv(tmp_path, text))
    mapping = auto_map(table.headers)
    mapping.fixed.update(site="site-a", platform="ios-xe", role="Access switch", port="2222")
    plan = preview(root, "acme", table, mapping)
    assert statuses(plan) == ["ok", "ok"]
    assert plan.devices()[1] == {
        "name": "sa-as04",
        "host": "203.0.113.14",
        "site": "site-a",
        "role": "access",
        "platform": "ios-xe",
        "model": None,
        "port": 2222,
        "credential": None,
    }
    # a fixed value wins over a column, and a missing column is an error, not a guess
    table = read_csv(write_csv(tmp_path, HEADER + "\nsa-as03,203.0.113.13,site-z,access,ios\n"))
    mapping = auto_map(table.headers)
    mapping.fixed["site"] = "site-a"
    assert preview(root, "acme", table, mapping).rows[0].status == "ok"
    with pytest.raises(ImportRefused, match="no column 'nope'"):
        preview(root, "acme", table, ColumnMapping(columns={"name": "nope"}))


def test_unmapped_required_fields_are_errors(root, tmp_path):
    plan = run(root, tmp_path, "name,host\nsa-as03,203.0.113.13\n")
    assert plan.rows[0].status == "error"
    for part in ("missing site", "missing type", "missing platform"):
        assert part in plan.rows[0].reason


def test_create_missing_sites(root, tmp_path):
    text = (
        HEADER + "\nsb-as01,203.0.113.21,site-b,access,ios\n"
        "sb-as02,203.0.113.22,site-b,access,ios\n"
        "sc-as01,203.0.113.23,Site C,access,ios\n"
        "sa-as09,203.0.113.29,site-a,access,ios\n"
    )
    plan = run(root, tmp_path, text)
    assert statuses(plan) == ["error", "error", "error", "ok"]
    assert plan.new_sites == []
    plan = run(root, tmp_path, text, create_sites=True)
    assert statuses(plan) == ["ok", "ok", "error", "ok"]  # a bad slug is never created
    assert plan.new_sites == ["site-b"]
    assert "will be created" in plan.rows[0].reason
    summary = apply_import(root, "acme", plan)
    assert summary.sites_created == ["site-b"] and summary.imported == 3
    assert (root / ACME / "sites" / "site-b.yml").is_file()
    ws = clean(root)
    assert {d.name for d in ws.customers["acme"].devices} >= {"sb-as01", "sb-as02", "sa-as09"}


def test_site_not_created_when_its_rows_have_errors(root, tmp_path):
    text = (
        HEADER
        + "\nsb-as01,203.0.113.21,site-b,toaster,ios\nsa-as09,203.0.113.29,site-a,access,ios\n"
    )
    plan = run(root, tmp_path, text, create_sites=True)
    assert plan.new_sites == []
    apply_import(root, "acme", plan)
    assert not (root / ACME / "sites" / "site-b.yml").exists()


def test_unknown_customer_and_bad_devices_file(root, tmp_path):
    table = read_csv(write_csv(tmp_path, HEADER + "\n"))
    with pytest.raises(ImportRefused, match="Unknown customer"):
        preview(root, "nobody", table, auto_map(table.headers))
    (root / ACME / "devices.yml").write_text("name: not-a-list\n")
    with pytest.raises(ImportRefused, match="unexpected layout"):
        preview(root, "acme", table, auto_map(table.headers))


# -- writing -----------------------------------------------------------------------------


def test_apply_writes_only_ok_rows_and_keeps_comments(root, tmp_path):
    devices_file = root / ACME / "devices.yml"
    devices_file.write_text("# acme devices\n" + devices_file.read_text() + "# end of file\n")
    plan = run(
        root,
        tmp_path,
        HEADER + ",login\n"
        "sa-as01,203.0.113.11,site-a,access,ios-xe,\n"  # skipped
        "sa-as03,203.0.113.13,site-a,access,ios-xe,acme-netadmin\n"  # ok
        "sa-as04,203.0.113.14,site-a,access,ios-xe,later-login\n"  # warning, imported
        "sa-bad,203.0.113.999,site-a,access,ios-xe,\n",  # error
    )
    summary = apply_import(root, "acme", plan)
    assert (summary.imported, summary.skipped, summary.errors) == (2, 1, 1)
    assert summary.sites_created == []
    assert "Imported 2 devices" in summary.message() and "skipped 1" in summary.message()
    text = devices_file.read_text()
    assert text.startswith("# acme devices\n") and "# end of file" in text
    names = [d.name for d in clean(root).customers["acme"].devices]
    assert names == ["sa-as01", "sa-as02", "sa-as03", "sa-as04"]


def test_nothing_is_written_when_every_row_has_an_error(root, tmp_path):
    before = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
    plan = run(
        root,
        tmp_path,
        HEADER + "\nsa-x1,203.0.113.999,site-a,access,ios\nsa-x2,203.0.113.2,site-z,access,ios\n",
        create_sites=False,
    )
    assert plan.ready == 0

    class Tripwire(InventoryEditor):
        def add_devices(self, *args, **kwargs):  # pragma: no cover
            raise AssertionError("the editor must not be called")

    summary = apply_import(root, "acme", plan, editor=Tripwire(root))
    assert (summary.imported, summary.errors, summary.sites_created) == (0, 2, [])
    assert {p: p.read_bytes() for p in root.rglob("*") if p.is_file()} == before


def test_batch_is_all_or_nothing(root, tmp_path):
    plan = run(
        root, tmp_path, HEADER + "\nsb-as01,203.0.113.21,site-b,access,ios\n", create_sites=True
    )
    # the inventory changed after the preview: the name is taken now
    InventoryEditor(root).add_device(
        "acme",
        {
            "name": "sb-as01",
            "host": "192.0.2.1",
            "site": "site-a",
            "role": "access",
            "platform": "ios",
        },
    )
    before = (root / ACME / "devices.yml").read_bytes()
    with pytest.raises(InventoryEditError, match="already used"):
        apply_import(root, "acme", plan)
    assert (root / ACME / "devices.yml").read_bytes() == before
    assert not (root / ACME / "sites" / "site-b.yml").exists()  # created for this call only


def test_250_rows_import_fast_and_round_trip(root, tmp_path):
    devices_file = root / ACME / "devices.yml"
    devices_file.write_text("# acme devices\n" + devices_file.read_text())
    lines = [HEADER + ",login"]
    for i in range(250):
        site = "site-a" if i % 2 else "site-b"
        lines.append(
            f"sw{i:03d},10.{i // 250}.{i % 250}.1,{site},Access switch,IOS-XE,acme-netadmin"
        )
    plan = run(root, tmp_path, "\n".join(lines) + "\n", create_sites=True)
    assert plan.ready == 250 and plan.new_sites == ["site-b"]
    started = time.perf_counter()
    summary = apply_import(root, "acme", plan)
    elapsed = time.perf_counter() - started
    assert summary.imported == 250
    assert elapsed < 10  # seconds, not minutes (add_device per row validates the file 250 times)
    assert devices_file.read_text().startswith("# acme devices\n")
    ws = clean(root)
    devices = ws.customers["acme"].devices
    assert len(devices) == 252
    assert sum(1 for d in devices if d.site == "site-b") == 125
    # importing the same file again changes nothing
    again = run(root, tmp_path, "\n".join(lines) + "\n", create_sites=True)
    assert again.skipped == 250 and again.ready == 0
