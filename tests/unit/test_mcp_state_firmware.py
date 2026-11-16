"""The state and firmware MCP tools (design.md, State checks, Firmware upgrades, MCP interface).

`list_state_tables` and `list_firmware` are tier 0; `state` is tier 1 and reads a table
through the same allowlist, connection and redaction as `show`.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest

from tsunami.core.history import History
from tsunami.core.models.jobs import Actor, JobStatus
from tsunami.core.models.policy import McpPolicy
from tsunami.core.models.settings import AppSettings, save_settings
from tsunami.core.policy import save_policy
from tsunami.core.secret_store import KeyringSecretStore, LoginCredential
from tsunami.core.state import TABLES, StateError, command_for, supported_tables
from tsunami.core.transports.mock import MockTransport
from tsunami.mcp_server.enforcement import McpApp, ToolRefused
from tsunami.mcp_server.tools import TOOL_NAMES, Tools

FIXTURE = Path(__file__).parents[1] / "fixtures" / "workspace"
LOGIN_PASSWORD = "Login-Pass-1234"
VLAN_SECRET = "Vl4n-Secret-7788"
FAMILIES = ("ios", "aos-cx", "aos-s")

VLANS = f"""\
VLAN Name                             Status    Ports
---- -------------------------------- --------- -------------------------------
1    default                          active    Gi1/0/3
10   Users                            active    Gi1/0/1, Gi1/0/2
20   Voice                            active
30   {VLAN_SECRET}             active
1002 fddi-default                     act/unsup
"""


class RecordingTransport:
    def __init__(self, mock: MockTransport) -> None:
        self.mock = mock
        self.targets = []

    def open(self, target):
        self.targets.append(target)
        return self.mock.open(target)


class Env:
    def __init__(self, app, tools, history, mock, recorder, paths, root):
        self.app, self.tools, self.history = app, tools, history
        self.mock, self.recorder, self.paths, self.root = mock, recorder, paths, root

    def policy(self, **fields) -> None:
        fields.setdefault("rate_limit_per_minute", 600)
        save_policy(McpPolicy(enabled=True, **fields), self.paths.policy_file)


@pytest.fixture
def env(tmp_path, app_paths):
    root = tmp_path / "ws"
    shutil.copytree(FIXTURE, root)
    devices = root / "inventory/customers/acme/devices.yml"
    devices.write_text(
        devices.read_text(encoding="utf-8")
        + "- {name: sa-cx01, host: 203.0.113.21, site: site-a, role: access, platform: aos-cx}\n"
        + "- {name: sa-sw01, host: 203.0.113.22, site: site-a, role: access, platform: aos-s}\n",
        encoding="utf-8",
    )
    save_settings(AppSettings(workspace=str(root)), app_paths.settings_file)
    save_policy(McpPolicy(enabled=True, rate_limit_per_minute=600), app_paths.policy_file)
    history = History(tmp_path / "history.db")
    secrets = KeyringSecretStore(index_path=tmp_path / "idx.json")
    secrets.set_login("acme-netadmin", LoginCredential("netadmin", LOGIN_PASSWORD))
    secrets.set_secret("acme", "vlan", VLAN_SECRET)
    mock = MockTransport()
    for name in ("sa-as01", "sa-as02", "sa-cx01", "sa-sw01"):
        state = mock.add_device(name, running=[f"hostname {name}"])
        state.exec_responses["show vlan brief"] = VLANS
    recorder = RecordingTransport(mock)
    app = McpApp(app_paths, history=history, secrets=secrets, transport_factory=lambda: recorder)
    yield Env(app, Tools(app), history, mock, recorder, app_paths, root)
    for thread in app.threads:
        thread.join(timeout=20)
    history.close()


def vlan_rows(result: dict) -> dict[str, dict]:
    return {str(row["id"]): row for row in result["untrusted_device_output"]}


# -- registration ------------------------------------------------------------------------


def test_the_new_tools_are_registered():
    assert {"list_state_tables", "state", "list_firmware"} <= TOOL_NAMES


# -- list_state_tables ---------------------------------------------------------------------


def test_list_state_tables_describes_every_table(env):
    tables = env.tools.list_state_tables()["tables"]
    assert [t["name"] for t in tables] == list(TABLES)
    by_name = {t["name"]: t for t in tables}
    vlans = by_name["vlans"]
    assert vlans["title"] == TABLES["vlans"].title
    assert vlans["key"] == ["id"]
    assert vlans["compared"] == list(TABLES["vlans"].compared)
    assert [c["name"] for c in vlans["columns"]] == list(TABLES["vlans"].column_names)
    assert all(set(c) == {"name", "kind", "help"} for c in vlans["columns"])
    assert {c["kind"] for t in tables for c in t["columns"]} == {"text", "int"}
    assert vlans["commands"]["ios"] == "show vlan brief"


def test_list_state_tables_gives_a_command_or_null_per_family(env):
    for entry in env.tools.list_state_tables()["tables"]:
        assert list(entry["commands"]) == list(FAMILIES)
        for family in FAMILIES:
            try:
                expected = command_for(family, entry["name"])
            except StateError:
                expected = None
            assert entry["commands"][family] == expected


def test_list_state_tables_has_a_null_where_a_family_cannot_read_a_table(env):
    tables = env.tools.list_state_tables()["tables"]
    assert any(c is None for t in tables for c in t["commands"].values())


@pytest.mark.parametrize("family", FAMILIES)
def test_list_state_tables_family_keeps_only_what_it_can_read(env, family):
    tables = env.tools.list_state_tables(family)["tables"]
    assert [t["name"] for t in tables] == list(supported_tables(family))
    assert all(t["commands"][family] for t in tables)


def test_list_state_tables_refuses_an_unknown_family(env):
    with pytest.raises(ToolRefused, match="unknown family 'nxos'.*ios, aos-cx, aos-s"):
        env.tools.list_state_tables("nxos")


def test_list_state_tables_is_tier_zero_and_touches_no_device(env):
    env.policy(max_tier=0)
    assert env.tools.list_state_tables()["tables"]
    assert env.recorder.targets == []


# -- state ---------------------------------------------------------------------------------


def test_state_returns_the_rows_redacted_under_untrusted_device_output(env):
    result = env.tools.state("acme", "sa-as01", "vlans")
    assert result["device"] == "sa-as01" and result["table"] == "vlans"
    rows = vlan_rows(result)
    assert rows["10"]["name"] == "Users" and rows["10"]["status"] == "active"
    assert rows["30"]["name"] == "<redacted>"
    assert VLAN_SECRET not in json.dumps(result)
    assert result["row_count"] == len(result["untrusted_device_output"]) == len(rows)
    assert LOGIN_PASSWORD not in json.dumps(result)


def test_state_does_not_echo_the_command_or_where(env):
    result = env.tools.state("acme", "sa-as01", "vlans", where={"name": "Users"})
    assert "show vlan brief" not in json.dumps(result)
    assert "Users" in json.dumps(result)  # the row's own value
    assert set(result) == {"device", "table", "row_count", "untrusted_device_output"}


def test_state_connects_as_show_does(env):
    env.tools.state("acme", "sa-as01", "vlans")
    [target] = env.recorder.targets
    assert target.device == "sa-as01" and target.allow_new_host_key is False
    assert target.credential.username == "netadmin"
    assert env.mock.device("sa-as01").sent == [("exec", "show vlan brief")]


def test_state_first_contact_follows_policy_and_never_a_device_claude_added(env):
    env.policy(trust_new_host_keys=True)
    env.tools.state("acme", "sa-as01", "vlans")
    assert env.recorder.targets[-1].allow_new_host_key is True
    env.history.record_mcp_device("acme", "sa-as02", "203.0.113.12")
    env.tools.state("acme", "sa-as02", "vlans")
    assert env.recorder.targets[-1].allow_new_host_key is False


def test_state_where_filters_the_rows(env):
    rows = vlan_rows(env.tools.state("acme", "sa-as01", "vlans", where={"name": "users"}))
    assert list(rows) == ["10"]
    rows = vlan_rows(
        env.tools.state("acme", "sa-as01", "vlans", where={"id": {"min": 10, "max": 20}})
    )
    assert list(rows) == ["10", "20"]
    rows = vlan_rows(
        env.tools.state("acme", "sa-as01", "vlans", where={"name": ["Users", "/^voi/"]})
    )
    assert list(rows) == ["10", "20"]
    rows = vlan_rows(env.tools.state("acme", "sa-as01", "vlans", where={"id": 20}))
    assert list(rows) == ["20"]
    none = env.tools.state("acme", "sa-as01", "vlans", where={"name": "nothing"})
    assert none["untrusted_device_output"] == [] and none["row_count"] == 0


def test_state_where_runs_on_redacted_cells(env):
    # The stored secret is a VLAN name. Asking for it finds nothing, as asking for any
    # other wrong name does; the redacted marker is all the filter can see.
    guess = env.tools.state("acme", "sa-as01", "vlans", where={"name": VLAN_SECRET})
    wrong = env.tools.state("acme", "sa-as01", "vlans", where={"name": "Qq4n-Secret-7788"})
    assert guess["untrusted_device_output"] == wrong["untrusted_device_output"] == []
    regex = env.tools.state("acme", "sa-as01", "vlans", where={"name": "/Secret/"})
    assert regex["untrusted_device_output"] == []
    assert VLAN_SECRET not in json.dumps([guess, wrong, regex])


def test_state_refuses_when_the_command_is_not_on_the_allowlist(env):
    env.policy(show_allowlist={"ios": ["show version"]})
    with pytest.raises(ToolRefused) as refused:
        env.tools.state("acme", "sa-as01", "vlans")
    message = str(refused.value)
    assert "show vlan brief" in message and "request_allowlist_addition" in message
    assert env.recorder.targets == []


def test_state_is_not_a_way_around_a_narrower_allowlist_entry(env):
    # `show vlan` alone does not cover `show vlan brief` for an entry with more words
    env.policy(show_allowlist={"ios": ["show vlan id"]})
    with pytest.raises(ToolRefused, match="request_allowlist_addition"):
        env.tools.state("acme", "sa-as01", "vlans")
    assert env.recorder.targets == []


def test_state_needs_tier_one(env):
    env.policy(max_tier=0)
    with pytest.raises(ToolRefused, match="tier 1"):
        env.tools.state("acme", "sa-as01", "vlans")
    assert env.recorder.targets == []


def test_state_for_a_blocked_customer_reads_as_unknown(env):
    env.policy(customers_blocked=["acme"])
    with pytest.raises(ToolRefused, match="unknown customer 'acme'"):
        env.tools.state("acme", "sa-as01", "vlans")
    with pytest.raises(ToolRefused, match="unknown customer 'nobody'"):
        env.tools.state("nobody", "sa-as01", "vlans")
    assert env.recorder.targets == []


def test_state_refuses_an_unknown_device_table_and_family_gap_before_connecting(env):
    with pytest.raises(ToolRefused, match="unknown device 'nope'"):
        env.tools.state("acme", "nope", "vlans")
    with pytest.raises(ToolRefused, match="unknown state table 'nope'"):
        env.tools.state("acme", "sa-as01", "nope")
    with pytest.raises(ToolRefused, match="unknown state table"):
        env.tools.state("acme", "sa-as01", ["vlans"])
    assert env.recorder.targets == []


@pytest.mark.parametrize(
    ("family", "device"), [("ios", "sa-as01"), ("aos-cx", "sa-cx01"), ("aos-s", "sa-sw01")]
)
def test_state_refuses_a_table_the_family_cannot_read_with_the_cores_message(env, family, device):
    gaps = [t for t in TABLES if t not in supported_tables(family)]
    if not gaps:
        pytest.skip(f"{family} reads every table")
    for gap in gaps:
        with pytest.raises(StateError) as core:
            command_for(family, gap)
        with pytest.raises(ToolRefused) as refused:
            env.tools.state("acme", device, gap)
        assert str(refused.value) == str(core.value)
    assert env.recorder.targets == []


@pytest.mark.parametrize(
    "where",
    [
        {"nope": "x"},
        {"name": {"floor": 1}},
        {"id": {"min": "1"}},
        {"id": {}},
        {"name": True},
        {"name": None},
        {"name": []},
        {"name": [["nested"]]},
        {"name": "/(a+)+/"},
        {"name": "/(/"},
        {"name": "x" * 300},
        ["name"],
        "name",
    ],
)
def test_state_refuses_a_bad_where_before_connecting(env, where):
    with pytest.raises(ToolRefused):
        env.tools.state("acme", "sa-as01", "vlans", where=where)
    assert env.recorder.targets == []


def test_state_bad_where_column_names_the_columns(env):
    with pytest.raises(ToolRefused, match="vlans has no column 'speed'.*id, name, status"):
        env.tools.state("acme", "sa-as01", "vlans", where={"speed": 1})


def test_state_reports_a_device_that_does_not_know_the_command(env):
    env.mock.device("sa-as01").exec_responses["show vlan brief"] = (
        "% Invalid input detected at '^' marker."
    )
    with pytest.raises(ToolRefused, match="rejected"):
        env.tools.state("acme", "sa-as01", "vlans")
    [job] = [j for j in env.history.list_jobs() if j.mode == "show"]
    [row] = env.history.devices(job.id)
    assert row.status != JobStatus.COMPLETED and "Invalid input" in row.error_redacted


def test_state_is_recorded_in_history_like_show(env):
    env.tools.state("acme", "sa-as01", "vlans", where={"id": {"min": 10, "max": 10}})
    [job] = [j for j in env.history.list_jobs() if j.mode == "show"]
    assert (job.actor, job.play_path, job.status) == (
        Actor.MCP,
        "Show command",
        JobStatus.COMPLETED,
    )
    assert job.summary["command"] == "show vlan brief"
    [row] = env.history.devices(job.id)
    assert row.device == "sa-as01"
    assert row.output_redacted.startswith("show vlan brief\n")
    rows = json.loads(row.output_redacted.split("\n", 1)[1])
    assert [r["id"] for r in rows] == [10]
    audit = env.history._query("SELECT tool, status, args_redacted_json FROM mcp_calls")
    assert [(a["tool"], a["status"]) for a in audit] == [("state", "ok")]


def test_state_history_holds_no_secret(env):
    env.tools.state("acme", "sa-as01", "vlans")
    [job] = [j for j in env.history.list_jobs() if j.mode == "show"]
    [row] = env.history.devices(job.id)
    assert VLAN_SECRET not in row.output_redacted and "<redacted>" in row.output_redacted


def test_state_counts_against_the_rate_limit(env):
    env.policy(rate_limit_per_minute=1)
    env.tools.state("acme", "sa-as01", "vlans")
    with pytest.raises(ToolRefused, match="rate limit"):
        env.tools.state("acme", "sa-as01", "vlans")


# -- list_firmware ---------------------------------------------------------------------------


def make_image(folder: Path, name: str, data: bytes) -> dict:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_bytes(data)
    return {
        "name": name,
        "size": len(data),
        "md5": hashlib.md5(data).hexdigest(),
        "sha256": hashlib.sha256(data).hexdigest(),
        "sha512": hashlib.sha512(data).hexdigest(),
    }


def test_list_firmware_lists_the_default_folder(env):
    folder = env.paths.firmware_dir
    a = make_image(folder, "cat9k_iosxe.17.09.04a.SPA.bin", b"image one" * 100)
    b = make_image(folder, "a-first.bin", b"another")
    result = env.tools.list_firmware()
    assert result["folder"] == str(folder)
    assert result["files"] == [b, a]  # by name


def test_list_firmware_empty_or_missing_folder(env):
    result = env.tools.list_firmware()
    assert result["files"] == [] and result["folder"] == str(env.paths.firmware_dir)
    env.paths.firmware_dir.mkdir(parents=True)
    assert env.tools.list_firmware()["files"] == []


def test_list_firmware_uses_the_folder_in_settings(env, tmp_path):
    chosen = tmp_path / "images"
    entry = make_image(chosen, "image.bin", b"chosen")
    make_image(env.paths.firmware_dir, "default.bin", b"default")
    save_settings(
        AppSettings(workspace=str(env.root), firmware_dir=str(chosen)), env.paths.settings_file
    )
    result = env.tools.list_firmware()
    assert result["folder"] == str(chosen)
    assert result["files"] == [entry]


def test_list_firmware_lists_one_folder_only(env):
    folder = env.paths.firmware_dir
    make_image(folder, "image.bin", b"x")
    (folder / "nested").mkdir()
    make_image(folder / "nested", "deep.bin", b"y")
    assert [f["name"] for f in env.tools.list_firmware()["files"]] == ["image.bin"]


def test_list_firmware_takes_no_path(env):
    with pytest.raises(ToolRefused, match="bad arguments"):
        env.app and Tools(env.app).dispatch("list_firmware", {"path": "C:/"})


def test_list_firmware_is_tier_zero_and_touches_no_device(env):
    env.policy(max_tier=0)
    make_image(env.paths.firmware_dir, "image.bin", b"x")
    assert len(env.tools.list_firmware()["files"]) == 1
    assert env.recorder.targets == []
