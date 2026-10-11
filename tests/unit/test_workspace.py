"""Workspace loader: the sample validates clean, broken copies report file and line.

See design.md, Build plan: Phase 1 acceptance.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from tsunami.core.issues import Severity
from tsunami.core.models.inventory import DEVICE_TYPES
from tsunami.core.resolver import Level
from tsunami.core.workspace import load_workspace

FIXTURE = Path(__file__).parents[1] / "fixtures" / "workspace"

DEVICES = "inventory/customers/acme/devices.yml"
CUSTOMER = "inventory/customers/acme/customer.yml"
ADD_VLAN = "plays/push/add-vlan.yml"
BASELINE = "plays/baseline/acme-baseline.yml"
DAY0 = "plays/initial/acme-access-day0.yml"

ARUBA_DEVICE = (
    "- name: sa-cx01\n  host: 203.0.113.21\n  site: site-a\n  role: access\n  platform: aos-cx\n"
)


@pytest.fixture
def workspace(tmp_path) -> Path:
    root = tmp_path / "ws"
    shutil.copytree(FIXTURE, root)
    return root


def edit(root: Path, relative: str, old: str, new: str) -> None:
    path = root / relative
    text = path.read_text(encoding="utf-8")
    assert text.count(old) == 1, f"{old!r} must occur exactly once in {relative}"
    path.write_text(text.replace(old, new), encoding="utf-8")


def test_sample_workspace_is_clean():
    ws = load_workspace(FIXTURE)
    assert ws.issues == []
    assert set(ws.customers) == {"acme"}
    assert set(ws.roles) == set(DEVICE_TYPES)  # built-in types; access and distribution have files
    assert all(entry.play is not None for entry in ws.plays.values())


def test_device_variables_with_provenance():
    resolved = load_workspace(FIXTURE).resolve_vars("acme", device="sa-as01")
    v = resolved.values
    assert v["domain"] == "acme.example.local"  # customer beats global
    assert v["ntp_servers"] == ["198.51.100.1", "198.51.100.2"]  # list replaced, not appended
    assert v["mgmt_vlan"] == 10
    assert v["hostname"] == "sa-as01"
    assert v["platform"] == "ios-xe"
    assert resolved.source_of("mgmt_vlan").level == Level.CUSTOMER_ROLE
    assert resolved.source_of("gateway").file == Path("inventory/customers/acme/sites/site-a.yml")
    assert resolved.source_of("mgmt_ip").level == Level.DEVICE
    assert resolved.source_of("timezone").level == Level.GLOBAL
    assert resolved.source_of("hostname").level == Level.DEFAULT
    assert resolved.source_of("customer").level == Level.BUILTIN


def test_new_device_variables_from_day0_answers():
    ws = load_workspace(FIXTURE)
    play = ws.plays[Path(DAY0)]
    resolved = ws.resolve_vars("acme", play=play, prompt_values={"site": "site-a"})
    assert resolved.values["gateway"] == "203.0.113.1"  # site layer from the prompt answer
    assert resolved.values["mgmt_vlan"] == 10  # role comes from play vars
    assert resolved.source_of("role").level == Level.PLAY
    assert resolved.source_of("site").level == Level.PROMPT
    assert "hostname" not in resolved.values  # no device, so no default


GOLDEN = [
    # (file, old, new, code, line)
    ("workspace.yml", "schema_version: 1", "schema_version: 99", "workspace.schema_too_new", 2),
    ("workspace.yml", "batch_size: 5", "batch_size: 5\n  batch_size: 6", "yaml.duplicate_key", 6),
    (DEVICES, "name: sa-as02", "name: sa-as01", "inventory.duplicate_device", 8),
    (
        DEVICES,
        "  platform: ios-xe\n  vars:\n    mgmt_ip: 203.0.113.12",
        "  platform: nxos\n  vars:\n    mgmt_ip: 203.0.113.12",
        "devices.enum",
        12,
    ),
    (
        DEVICES,
        "  platform: ios-xe\n  vars:\n    mgmt_ip: 203.0.113.11",
        "  platform: ios-xe\n  model: no-such-model\n  vars:\n    mgmt_ip: 203.0.113.11",
        "inventory.unknown_model",
        6,
    ),
    (
        DEVICES,
        "host: 203.0.113.11\n  site: site-a",
        "host: 203.0.113.11\n  site: site-b",
        "inventory.unknown_site",
        3,
    ),
    (
        CUSTOMER,
        "credential: acme-netadmin",
        "credential: Acme NetAdmin",
        "customer.string_pattern_mismatch",
        2,
    ),
    (CUSTOMER, "  tacacs_host: 198.51.100.30", "  site: hq", "customer.value_error", 8),
    (
        "inventory/global.yml",
        "timezone: CST -6 0",
        "timezone: CST -6 0\nsnmp_community: public",
        "vars.secret_literal",
        5,
    ),
    (ADD_VLAN, "roles: [access]", "roles: [acess]", "targets.unknown_role", 5),
    (ADD_VLAN, "customer: acme", "customer: globex", "play.unknown_customer", 3),
    (ADD_VLAN, "    type: vlan\n", "    type: vlan\n    choices: [a]\n", "play.value_error", 7),
    (
        ADD_VLAN,
        "show vlan id {{ vlan_id }}",
        "show vlan id {{ secret('acme/x') }}",
        "play.secret_in_check",
        18,
    ),
    (BASELINE, "snippet: ntp", "snippet: ntpp", "play.snippet_missing", 11),
    (
        DAY0,
        "customer: acme\n",
        "customer: acme\ntargets:\n  roles: [access]\n",
        "play.targets_forbidden",
        5,
    ),
    (DAY0, "default: TenGigabitEthernet1/1/1", "default: Te1/1/1", "play.prompt_default", 25),
    (
        DAY0,
        "  role: access\nsteps:",
        "  platform: ios-xe\n  role: access\nsteps:",
        "play.model_and_platform",
        9,
    ),
    (
        DAY0,
        "  mgmt_ip:\n    type: ipv4_interface\n    label: Mgmt SVI (IP/prefix)\n",
        "",
        "play.add_to_inventory",
        41,
    ),
    (DAY0, "snippet: base", "snippet: nowhere", "play.no_model_fits", 9),
    (
        DEVICES,
        "mgmt_ip: 203.0.113.12/24\n",
        "mgmt_ip: 203.0.113.12/24\n" + ARUBA_DEVICE,
        "play.mixed_family_inline",
        13,
    ),
]


@pytest.mark.parametrize(("relative", "old", "new", "code", "line"), GOLDEN)
def test_broken_copy_reports_file_and_line(workspace, relative, old, new, code, line):
    edit(workspace, relative, old, new)
    issues = load_workspace(workspace).issues
    found = {(i.code, i.file.as_posix() if i.file else None, i.line) for i in issues}
    expected_file = ADD_VLAN if code == "play.mixed_family_inline" else relative
    assert (code, expected_file, line) in found, "\n".join(map(str, issues))


def test_newer_schema_stops_loading(workspace):
    edit(workspace, "workspace.yml", "schema_version: 1", "schema_version: 99")
    ws = load_workspace(workspace)
    assert [i.code for i in ws.issues] == ["workspace.schema_too_new"]
    assert ws.plays == {}


def test_missing_workspace_file(tmp_path):
    ws = load_workspace(tmp_path)
    assert [i.code for i in ws.errors] == ["workspace.missing"]


def test_one_bad_device_does_not_hide_the_others(workspace):
    edit(workspace, DEVICES, "name: sa-as01", "name: SA-AS01")
    ws = load_workspace(workspace)
    assert [d.name for d in ws.customers["acme"].devices] == ["sa-as02"]
    assert any(i.code == "devices.string_pattern_mismatch" and i.line == 1 for i in ws.issues)


def test_draft_snippets_serve_draft_plays_only(workspace):
    play = (
        "name: Draft\ntype: push\ncustomer: acme\ntargets:\n  roles: [access]\n"
        "steps:\n  - snippet: syslog\n"
    )
    draft_snippet = workspace / "drafts/snippets/common/ios/syslog.j2"
    draft_snippet.parent.mkdir(parents=True)
    draft_snippet.write_text("logging host 192.0.2.20\n", encoding="utf-8")
    for folder in ("drafts/plays/push", "plays/push"):
        (workspace / folder).mkdir(parents=True, exist_ok=True)
        (workspace / folder / "syslog.yml").write_text(play, encoding="utf-8")

    ws = load_workspace(workspace)
    draft_issues = ws.issues_for(Path("drafts/plays/push/syslog.yml"))
    live_issues = ws.issues_for(Path("plays/push/syslog.yml"))
    assert draft_issues == []
    assert [i.code for i in live_issues] == ["play.snippet_missing"]
    assert ws.plays[Path("drafts/plays/push/syslog.yml")].draft


def test_snippet_outside_lookup_paths_warns(workspace):
    (workspace / "snippets/common/stray.j2").write_text("x\n", encoding="utf-8")
    ws = load_workspace(workspace)
    assert [(i.code, i.severity) for i in ws.issues] == [("snippet.location", Severity.WARNING)]


def test_snippet_only_plays_may_span_families(workspace):
    edit(
        workspace,
        DEVICES,
        "mgmt_ip: 203.0.113.12/24\n",
        "mgmt_ip: 203.0.113.12/24\n" + ARUBA_DEVICE,
    )
    for name in ("archive", "ntp"):
        path = workspace / f"snippets/common/aos-cx/{name}.j2"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("ntp server 192.0.2.10\n", encoding="utf-8")
    (workspace / "snippets/customers/acme/aos-cx").mkdir(parents=True)
    (workspace / "snippets/customers/acme/aos-cx/aaa.j2").write_text("aaa\n", encoding="utf-8")
    ws = load_workspace(workspace)
    assert ws.issues_for(Path(BASELINE)) == []
    assert {i.code for i in ws.issues_for(Path(ADD_VLAN))} == {"play.mixed_family_inline"}


def test_cli_validate(workspace, capsys):
    from tsunami import cli

    assert cli.main(["validate", str(workspace)]) == 0
    assert capsys.readouterr().out.strip().endswith("0 error(s), 0 warning(s)")

    edit(workspace, BASELINE, "snippet: ntp", "snippet: ntpp")
    assert cli.main(["validate", str(workspace)]) == 1
    out = capsys.readouterr().out
    assert f"{Path(BASELINE)}:11: error:" in out
    assert "[play.snippet_missing]" in out


def test_built_in_device_types_need_no_role_file(workspace):
    router = (
        "- name: sa-rt01\n  host: 203.0.113.30\n  site: site-a\n  role: router\n  platform: ios\n"
    )
    (workspace / DEVICES).write_text(
        (workspace / DEVICES).read_text(encoding="utf-8") + router, encoding="utf-8"
    )
    ws = load_workspace(workspace)
    assert ws.errors == []
    assert ws.customers["acme"].device("sa-rt01").role == "router"


def test_login_precedence_device_then_site_then_customer(workspace):
    edit(
        workspace,
        CUSTOMER,
        "lab: false\n",
        "lab: false\nsite_credentials:\n  site-a: acme-site-a\n",
    )
    edit(
        workspace,
        DEVICES,
        "  platform: ios-xe\n  vars:\n    mgmt_ip: 203.0.113.12/24\n",
        "  platform: ios-xe\n  credential: acme-sa-as02\n  vars:\n    mgmt_ip: 203.0.113.12/24\n",
    )
    ws = load_workspace(workspace)
    assert ws.errors == []
    acme = ws.customers["acme"]
    assert acme.credential_for(acme.device("sa-as02")) == "acme-sa-as02"
    assert acme.credential_for(acme.device("sa-as01")) == "acme-site-a"
    assert acme.credential_for(None) == "acme-netadmin"


def test_site_credentials_must_name_a_site(workspace):
    edit(workspace, CUSTOMER, "lab: false\n", "lab: false\nsite_credentials:\n  site-z: x-login\n")
    issues = [i for i in load_workspace(workspace).errors if i.code == "inventory.unknown_site"]
    assert issues and "site-z" in issues[0].message


def test_customer_with_no_sites_still_loads(workspace):
    """The day-0 play asks for a site; with none left, loading must not crash."""
    (workspace / DEVICES).unlink()
    (workspace / "inventory/customers/acme/sites/site-a.yml").unlink()
    ws = load_workspace(workspace)
    assert "acme" in ws.customers and ws.customers["acme"].sites == {}
