"""State tables and state checks (design.md, State checks): the table registry, the
evaluation of where/expect/count/compare, the play model, and the checks running through
the device pipeline against the mock transport."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from pydantic import ValidationError
from tests.unit.test_runner import FIXTURE, LOGIN, VLAN, make_runner, mock_for, statuses, write_play

from tsunami.core.history import History
from tsunami.core.models.jobs import DeviceStatus, JobStatus
from tsunami.core.models.play import Check, RunMode
from tsunami.core.runner import RunRequest
from tsunami.core.secret_store import KeyringSecretStore, LoginCredential
from tsunami.core.services import play_entry
from tsunami.core.state import (
    TABLES,
    StateAssertion,
    StateError,
    command_for,
    evaluate,
    matches,
    parse_table,
    supported_tables,
    table,
    unknown_command,
)
from tsunami.core.workspace import load_workspace

FIXTURES = Path(__file__).parents[1] / "fixtures" / "state"
OSPF = table("ospf_neighbors")


@pytest.fixture
def ws_root(tmp_path) -> Path:
    root = tmp_path / "ws"
    shutil.copytree(FIXTURE, root)
    return root


@pytest.fixture
def secrets(tmp_path) -> KeyringSecretStore:
    store = KeyringSecretStore(index_path=tmp_path / "credential-index.json")
    store.set_login("acme-netadmin", LoginCredential("netadmin", LOGIN))
    return store


@pytest.fixture
def history(tmp_path):
    db = History(tmp_path / "history.db")
    yield db
    db.close()


def fixture(family: str, name: str) -> str:
    return (FIXTURES / family / f"{name}.txt").read_text(encoding="utf-8")


def neighbor(rid: str, interface: str, state: str = "full") -> dict:
    return {
        "neighbor_id": rid,
        "address": "10.0.0.1",
        "interface": interface,
        "state": state,
        "role": "",
    }


THREE = [
    neighbor("10.255.0.1", "Ethernet0/0"),
    neighbor("10.255.0.2", "Ethernet0/1"),
    neighbor("10.255.0.4", "Ethernet0/2"),
]


def check(**fields) -> StateAssertion:
    base = {"table": "ospf_neighbors", "where": {}, "expect": {}}
    return StateAssertion(**{**base, **fields})


# -- tables -----------------------------------------------------------------------------


def test_every_table_is_consistent():
    for spec in TABLES.values():
        names = spec.column_names
        assert len(set(names)) == len(names)
        assert spec.key and set(spec.key) <= set(names)
        assert set(spec.compared) <= set(names)
        assert all(c.kind in ("text", "int") for c in spec.columns)


@pytest.mark.parametrize("family", ["ios", "aos-cx", "aos-s"])
def test_a_family_only_offers_tables_it_can_parse(family):
    for name in supported_tables(family):
        assert name in TABLES
        assert command_for(family, name).startswith("show ")


def test_unknown_and_unsupported_tables_say_so():
    with pytest.raises(StateError, match="unknown state table"):
        command_for("ios", "nonsense")
    missing = [n for n in TABLES if n not in supported_tables("aos-s")]
    if missing:
        with pytest.raises(StateError, match="not available on aos-s"):
            command_for("aos-s", missing[0])


# -- reading ----------------------------------------------------------------------------


def test_ios_interfaces_from_real_output_use_running_config_names():
    rows = parse_table("ios", "interfaces", fixture("ios", "interfaces_c1101_16_9"))
    by_name = {r["name"]: r for r in rows}
    assert by_name["GigabitEthernet0/0/0"] == {
        "name": "GigabitEthernet0/0/0",
        "status": "up",
        "protocol": "up",
    }
    assert by_name["GigabitEthernet0/1/1"]["status"] == "admin-down"
    assert by_name["Vlan1"] == {"name": "Vlan1", "status": "up", "protocol": "down"}
    assert "Loopback0" in by_name and len(rows) == 10


def test_ios_ospf_neighbors_from_real_output():
    rows = parse_table("ios", "ospf_neighbors", fixture("ios", "ospf_neighbors_iol_17_18"))
    assert [(r["neighbor_id"], r["interface"], r["state"], r["role"]) for r in rows] == [
        ("10.255.0.4", "Ethernet0/2", "full", ""),
        ("10.255.0.2", "Ethernet0/1", "full", ""),
        ("10.255.0.1", "Ethernet0/0", "full", ""),
    ]
    with_role = (
        "Neighbor ID     Pri   State           Dead Time   Address         Interface\n"
        "192.0.2.7         1   FULL/DR         00:00:31    198.51.100.7    Gi1/0/1\n"
        "192.0.2.8         1   2WAY/DROTHER    00:00:35    198.51.100.8    Vlan10\n"
    )
    rows = parse_table("ios", "ospf_neighbors", with_role)
    assert [(r["interface"], r["state"], r["role"]) for r in rows] == [
        ("GigabitEthernet1/0/1", "full", "dr"),
        ("Vlan10", "2way", "drother"),
    ]


def test_a_feature_that_is_off_is_an_empty_table_but_an_unknown_command_is_an_error():
    assert parse_table("ios", "ospf_neighbors", "") == []
    assert parse_table("ios", "ospf_neighbors", fixture("ios", "bgp_not_active")) == []
    assert unknown_command("ios", fixture("ios", "bgp_not_active")) is None
    with pytest.raises(StateError, match="rejected `show ip ospf neighbor`.*Invalid input"):
        parse_table("ios", "ospf_neighbors", fixture("ios", "invalid_command"))


# -- matching and evaluation --------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "matcher", "kind", "expected"),
    [
        ("full", "FULL", "text", True),
        ("full", "2way", "text", False),
        ("full", ["2way", "full"], "text", True),
        ("TenGigabitEthernet1/1/4", r"/^TenGigabitEthernet1\/1\//", "text", True),
        ("GigabitEthernet1/0/4", r"/^TenGigabitEthernet1\/1\//", "text", False),
        (10, "10", "int", True),
        (10, 11, "int", False),
        (3, {"min": 2}, "int", True),
        (1, {"min": 2}, "int", False),
        (5, {"min": 2, "max": 4}, "int", False),
        ("up", {"min": 1}, "text", False),
        ("/", "/", "text", True),  # one slash is a value, not a regex
    ],
)
def test_matchers(value, matcher, kind, expected):
    assert matches(value, matcher, kind) is expected


def test_expect_holds_for_every_selected_row_and_needs_a_row():
    assert evaluate(OSPF, check(expect={"state": "full"}), THREE).ok
    down = [*THREE[:2], neighbor("10.255.0.4", "Ethernet0/2", "init")]
    result = evaluate(OSPF, check(expect={"state": "full"}), down)
    assert not result.ok
    assert "1 of 3 rows not state full" in result.detail
    assert "neighbor_id=10.255.0.4 interface=Ethernet0/2 (state=init)" in result.detail
    scoped = check(where={"interface": "Ethernet0/0"}, expect={"state": "full"})
    assert evaluate(OSPF, scoped, down).ok
    none = evaluate(OSPF, check(where={"interface": "Vlan99"}, expect={"state": "full"}), THREE)
    assert not none.ok and "no rows" in none.detail


def test_count_is_exact_or_a_range():
    assert evaluate(OSPF, check(count=(3, 3)), THREE).ok
    exact = evaluate(OSPF, check(count=(2, 2)), THREE)
    assert not exact.ok and "3 rows, expected 2" in exact.detail
    assert evaluate(OSPF, check(count=(2, None)), THREE).ok
    low = evaluate(OSPF, check(count=(4, None)), THREE)
    assert not low.ok and "expected at least 4" in low.detail
    assert evaluate(OSPF, check(count=(0, 0), where={"state": "init"}), THREE).ok


def test_compare_count_no_loss_and_same():
    fewer = THREE[:2]
    more = [*THREE, neighbor("10.255.0.9", "Ethernet0/3")]
    flapped = [*THREE[:2], neighbor("10.255.0.4", "Ethernet0/2", "exstart")]

    assert evaluate(OSPF, check(compare="count"), THREE, THREE).ok
    assert not evaluate(OSPF, check(compare="count"), fewer, THREE).ok

    lost = evaluate(OSPF, check(compare="no_loss"), fewer, THREE)
    assert not lost.ok and "1 row gone since before the change" in lost.detail
    assert "neighbor_id=10.255.0.4" in lost.detail
    assert evaluate(OSPF, check(compare="no_loss"), more, THREE).ok
    changed = evaluate(OSPF, check(compare="no_loss"), flapped, THREE)
    assert not changed.ok and "state full -> exstart" in changed.detail

    assert evaluate(OSPF, check(compare="same"), THREE, THREE).ok
    added = evaluate(OSPF, check(compare="same"), more, THREE)
    assert not added.ok and "1 row new since before the change" in added.detail


def test_compare_ignores_columns_the_table_does_not_compare():
    moved = [dict(row, address="10.9.9.9") for row in THREE]
    assert evaluate(OSPF, check(compare="same"), moved, THREE).ok


def test_compare_without_a_reading_from_before_fails():
    result = evaluate(OSPF, check(compare="same"), THREE, None)
    assert not result.ok and "no reading from before" in result.detail


# -- the play model -----------------------------------------------------------------------


def test_check_model_accepts_state_forms():
    one = Check(state="ospf_neighbors", expect={"state": "full"}, where={"interface": "Vlan10"})
    assert one.count_range is None
    assert Check(state="ospf_neighbors", count=2).count_range == (2, 2)
    assert Check(state="ospf_neighbors", count={"min": 1}).count_range == (1, None)
    assert Check(state="bgp_neighbors", compare="no_loss").compare == "no_loss"
    assert Check(state="interfaces", expect={"status": ["up", "admin-down"]}).state


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({"state": "nonsense", "count": 1}, "unknown state table"),
        ({"state": "ospf_neighbors"}, "needs expect, count or compare"),
        ({"state": "ospf_neighbors", "expect": "full"}, "mapping of column to value"),
        ({"state": "ospf_neighbors", "expect": {"speed": "1g"}}, "has no column 'speed'"),
        ({"state": "ospf_neighbors", "where": {"vrf": "x"}, "count": 1}, "has no column 'vrf'"),
        ({"state": "ospf_neighbors", "exec": "show x", "count": 1}, "one of exec, reachable"),
        ({"state": "ospf_neighbors", "expect_not": "x", "count": 1}, "expect_not is for exec"),
        ({"exec": "show x", "expect": "y", "count": 1}, "only valid on state checks"),
        ({"exec": "show x", "expect": {"state": "full"}}, "expect is a regex here"),
        ({"state": "ospf_neighbors", "expect": {"state": "/(/"}}, "invalid regular expression"),
        ({"state": "ospf_neighbors", "count": {}}, "count needs"),
        ({"state": "ospf_neighbors", "compare": "exactly"}, "compare"),
    ],
)
def test_check_model_refuses(fields, message):
    with pytest.raises(ValidationError, match=message):
        Check(**fields)


# -- through the pipeline -------------------------------------------------------------------

OSPF_OUT = fixture("ios", "ospf_neighbors_iol_17_18")
OSPF_TWO = "\n".join(OSPF_OUT.split("\n")[:4]) + "\n"  # one neighbor fewer
PLAY = """\
name: Add VLAN with state checks
type: push
customer: acme
targets:
  roles: [access]
prompts:
  vlan_id:
    type: vlan
  vlan_name:
    type: string
execution:
  canary: false
steps:
  - config:
      - vlan {{ vlan_id }}
      - " name {{ vlan_name }}"
checks:
"""


def state_play(root: Path, checks: str) -> str:
    return write_play(root, "state-vlan", PLAY + checks)


def with_ospf(mock, output=OSPF_OUT):
    for state in list(mock._devices.values()):
        state.exec_responses["show ip ospf neighbor"] = output
    return mock


def test_post_check_compares_with_the_state_before_the_change(ws_root, history, secrets, app_paths):
    path = state_play(
        ws_root,
        "  pre:\n    - state: ospf_neighbors\n      count: {min: 1}\n"
        "  post:\n    - state: ospf_neighbors\n      compare: same\n",
    )
    mock = with_ospf(mock_for(["sa-as01", "sa-as02"]))
    runner, _ = make_runner(ws_root, mock, history, secrets, app_paths)
    job_id = runner.run(RunRequest(path, RunMode.APPLY, prompts=VLAN))

    assert history.get_job(job_id).status == JobStatus.COMPLETED
    row = history.devices(job_id)[0]
    assert "state ospf_neighbors: 3 rows before the change" in row.output_redacted
    assert "post-check post 1: state ospf_neighbors: ok" in row.output_redacted
    assert "the same rows as before" in row.output_redacted


def test_a_neighbor_lost_by_the_change_rolls_it_back(ws_root, history, secrets, app_paths):
    path = state_play(ws_root, "  post:\n    - state: ospf_neighbors\n      compare: no_loss\n")
    mock = with_ospf(mock_for(["sa-as01"]))
    state = mock.device("sa-as01")
    real_exec = type(mock.connect("sa-as01"))._exec

    def losing(self, st, cmd):
        # Once the VLAN is configured, one neighbor is gone.
        if cmd == "show ip ospf neighbor" and "vlan 30" in st.running:
            return OSPF_TWO
        return real_exec(self, st, cmd)

    type(mock.connect("sa-as01"))._exec = losing
    try:
        runner, _ = make_runner(ws_root, mock, history, secrets, app_paths)
        job_id = runner.run(RunRequest(path, RunMode.APPLY, prompts=VLAN, limit=["sa-as01"]))
    finally:
        type(mock.connect("sa-as01"))._exec = real_exec

    row = history.devices(job_id)[0]
    assert row.status == DeviceStatus.ROLLED_BACK
    assert "1 row gone since before the change" in row.error_redacted
    assert "neighbor_id=10.255.0.1" in row.error_redacted
    assert "vlan 30" not in state.running


def test_a_table_the_device_cannot_give_stops_it_before_any_change(
    ws_root, history, secrets, app_paths
):
    path = state_play(ws_root, "  post:\n    - state: ospf_neighbors\n      compare: same\n")
    mock = with_ospf(mock_for(["sa-as01"]), fixture("ios", "invalid_command"))
    runner, _ = make_runner(ws_root, mock, history, secrets, app_paths)
    job_id = runner.run(RunRequest(path, RunMode.APPLY, prompts=VLAN, limit=["sa-as01"]))

    row = history.devices(job_id)[0]
    assert row.status == DeviceStatus.SKIPPED_PRECHECK
    assert "state check cannot run" in row.error_redacted
    assert "vlan 30" not in mock.device("sa-as01").running


def test_check_mode_finds_a_table_the_device_cannot_give(ws_root, history, secrets, app_paths):
    path = state_play(ws_root, "  post:\n    - state: ospf_neighbors\n      compare: same\n")
    mock = with_ospf(mock_for(["sa-as01", "sa-as02"]))
    mock.device("sa-as02").exec_responses["show ip ospf neighbor"] = fixture(
        "ios", "invalid_command"
    )
    runner, _ = make_runner(ws_root, mock, history, secrets, app_paths)
    job_id = runner.run(RunRequest(path, RunMode.CHECK, prompts=VLAN))

    assert statuses(history, job_id) == {
        "sa-as01": DeviceStatus.SUCCESS,
        "sa-as02": DeviceStatus.FAILED,
    }


def test_a_failed_state_pre_check_skips_the_device(ws_root, history, secrets, app_paths):
    path = state_play(
        ws_root,
        "  pre:\n    - state: ospf_neighbors\n      expect: {state: full}\n      count: 4\n",
    )
    mock = with_ospf(mock_for(["sa-as01"]))
    runner, _ = make_runner(ws_root, mock, history, secrets, app_paths)
    job_id = runner.run(RunRequest(path, RunMode.APPLY, prompts=VLAN, limit=["sa-as01"]))

    row = history.devices(job_id)[0]
    assert row.status == DeviceStatus.SKIPPED_PRECHECK
    assert "3 rows, expected 4" in row.error_redacted


def test_state_values_are_rendered_and_join_the_render_hash(ws_root):
    from tsunami.core.render.renderer import Renderer

    path = state_play(
        ws_root,
        "  post:\n    - state: ospf_neighbors\n"
        "      where: {interface: 'Vlan{{ vlan_id }}'}\n      expect: {state: full}\n",
    )
    ws = load_workspace(ws_root)
    entry = play_entry(ws, path)
    renderer = Renderer(ws)
    one = renderer.render(entry, device="sa-as01", prompt_values={**VLAN, "vlan_id": 30})
    other = renderer.render(entry, device="sa-as01", prompt_values={**VLAN, "vlan_id": 31})
    assert dict(one.checks[0].where) == {"interface": "Vlan30"}
    assert one.render_hash() != other.render_hash()


def test_a_table_missing_on_a_target_family_fails_validation(ws_root):
    absent = next((n for n in TABLES if n not in supported_tables("ios")), None)
    if absent is None:
        pytest.skip("ios reads every table")
    path = state_play(ws_root, f"  post:\n    - state: {absent}\n      compare: count\n")
    ws = load_workspace(ws_root)
    issues = [i for i in ws.issues_for(Path(path)) if i.code == "play.state_unsupported"]
    assert issues and "not available on ios" in issues[0].message


def test_compare_in_a_pre_check_fails_validation(ws_root):
    path = state_play(ws_root, "  pre:\n    - state: ospf_neighbors\n      compare: same\n")
    ws = load_workspace(ws_root)
    assert any(i.code == "play.compare_pre" for i in ws.issues_for(Path(path)))
