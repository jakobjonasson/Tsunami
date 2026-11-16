"""AOS-CX and AOS-S state table parsers (design.md, State checks).

The fixtures under tests/fixtures/state/aos-cx and aos-s are all text the research notes
(docs/research/state-tables.md) quote, and none was captured by this project on a device:

- real captures from the ntc-templates test data, spacing real, names and addresses edited:
  aos-cx bgp_ntc, vlans_ntc; aos-s interfaces_ntc_*, vlans_ntc_*.
- *_vendor: HPE documentation or background knowledge, "layout reconstructed": the words
  and their order are HPE's but the column spacing was rebuilt by hand (stack_*, lacp,
  ospf, ... on aos-cx and aos-s; aos-s lacp and stp are UNVERIFIED layouts).

Every fixture has a *_tight variant (two spaces between columns) and some a *_tabs variant
(a tab), generated from it, to show the parsers do not read column positions."""

from __future__ import annotations

from pathlib import Path

import pytest

from tsunami.core.state import StateError, parse_table, supported_tables
from tsunami.core.state.parsers import aoscx, aoss
from tsunami.core.state.tables import TABLES

FIXTURES = Path(__file__).parents[1] / "fixtures" / "state"
FAMILY_DIR = {"aos-cx": "aos-cx", "aos-s": "aos-s"}


def fixture_text(family: str, fixture: str) -> str:
    return (FIXTURES / FAMILY_DIR[family] / f"{fixture}.txt").read_text(encoding="utf-8")


def variants(family: str, fixture: str) -> list[str]:
    names = [fixture, f"{fixture}_tight"]
    if (FIXTURES / FAMILY_DIR[family] / f"{fixture}_tabs.txt").exists():
        names.append(f"{fixture}_tabs")
    return names


# -- row builders -------------------------------------------------------------------------


def iface(name, status, protocol):
    return {"name": name, "status": status, "protocol": protocol}


def channel(name, status, protocol, members, members_up):
    return {
        "name": name,
        "status": status,
        "protocol": protocol,
        "members": members,
        "members_up": members_up,
    }


def member(port_channel, port, status):
    return {"port_channel": port_channel, "member": port, "status": status}


def ospf(neighbor_id, address, interface, state, role):
    return {
        "neighbor_id": neighbor_id,
        "address": address,
        "interface": interface,
        "state": state,
        "role": role,
    }


def bgp(neighbor, remote_as, state):
    return {"neighbor": neighbor, "remote_as": remote_as, "state": state, "prefixes": 0}


def stack_row(member_id, role, state):
    return {"member": member_id, "role": role, "state": state}


def vlan(vlan_id, name, status):
    return {"id": vlan_id, "name": name, "status": status}


def route(source, count):
    return {"source": source, "routes": count}


def fhrp(interface, group, state, priority):
    return {"interface": interface, "group": group, "state": state, "priority": priority}


def stp(instance, blocking, forwarding):
    return {"instance": instance, "blocking": blocking, "forwarding": forwarding}


# -- the fixtures and the rows each one parses to -----------------------------------------

CX_CASES = [
    (
        "interfaces",
        "interfaces_vendor",
        [
            iface("1/1/1", "up", "up"),
            iface("1/1/2", "down", "down"),
            iface("1/1/3", "admin-down", "down"),
            iface("1/1/52:1", "down", "down"),
            iface("lag1", "up", "up"),
            iface("vlan10", "up", "up"),
        ],
    ),
    (
        "port_channels",
        "lacp_vendor",
        [
            channel("lag10", "down", "lacp", 1, 0),
            channel("lag128", "up", "lacp", 3, 2),
            channel("lag20", "up", "static", 1, 1),
            channel("lag30", "up", "lacp", 1, 1),
            channel("lag1", "up", "lacp", 1, 1),
            channel("lag2", "down", "", 1, 0),
        ],
    ),
    (
        "port_channel_members",
        "lacp_vendor",
        [
            member("lag10", "1/1/1", "suspended"),
            member("lag128", "1/1/2", "bundled"),
            member("lag128", "1/1/3", "bundled"),
            member("lag128", "1/1/4", "down"),
            member("lag20", "1/1/5", "bundled"),
            member("lag30", "1/1/6", "standalone"),
            member("lag1", "1/4/14", "bundled"),
            member("lag2", "1/5/15", "down"),
        ],
    ),
    (
        "ospf_neighbors",
        "ospf_vendor",
        [
            ospf("10.255.0.2", "10.1.1.2", "1/1/1", "full", "dr"),
            ospf("10.255.0.3", "10.1.2.2", "vlan20", "2way", "drother"),
            ospf("10.255.0.4", "10.1.3.2", "lag1", "full", ""),
            ospf("10.255.1.5", "192.168.9.2", "1/1/9", "exstart", "bdr"),
        ],
    ),
    (
        "bgp_neighbors",
        "bgp_ntc",
        [
            bgp("10.252.13.2", 4290020685, "idle"),
            bgp("172.25.0.1", 4290000000, "established"),
            bgp("10.1.1.5", 11, "established"),
            bgp("10.252.13.9", 65001, "admin-down"),
            bgp("2001:db8::2002", 11, "established"),
            bgp("172.18.174.17", 4290000000, "established"),
        ],
    ),
    (
        "stack",
        "stack_vsf_vendor",
        [
            stack_row(1, "active", "ready"),
            stack_row(2, "standby", "ready"),
            stack_row(3, "member", "ready"),
            stack_row(4, "member", "booting"),
            stack_row(5, "member", "not-present"),
        ],
    ),
    (
        "vlans",
        "vlans_ntc",
        [
            vlan(1, "DEFAULT_VLAN_1", "up"),
            vlan(10, "acme-users", "down"),
            vlan(23, "VLAN23", "up"),
            vlan(3072, "mgmt", "up"),
        ],
    ),
    (
        "routes",
        "routes_vendor",
        [
            route("connected", 3),
            route("local", 3),
            route("static", 2),
            route("bgp", 12),
            route("total", 20),
        ],
    ),
    (
        "vrrp",
        "vrrp_vendor",
        [
            fhrp("1/1/1", 1, "master", 100),
            fhrp("1/1/2", 1, "init", 100),
            fhrp("vlan20", 5, "backup", 90),
        ],
    ),
    (
        "spanning_tree",
        "stp_vendor",
        [stp("MST0", 3, 3), stp("MST1", 1, 2), stp("total", 4, 5)],
    ),
]

AOSS_CASES = [
    (
        "interfaces",
        "interfaces_ntc_chassis",
        [
            iface("A1", "up", "up"),
            iface("A8", "up", "up"),
            iface("D1", "up", "up"),
            iface("D5", "admin-down", "down"),
            iface("E3", "down", "down"),
            iface("F1", "up", "up"),
        ],
    ),
    (
        "interfaces",
        "interfaces_ntc_stack",
        [
            iface("1/1", "up", "up"),
            iface("1/24", "down", "down"),
            iface("1/25", "up", "up"),
            iface("1/27", "down", "down"),
            iface("1/28", "up", "up"),
            iface("2/1", "up", "up"),
        ],
    ),
    (
        "port_channels",
        "lacp_vendor",
        [
            channel("Trk1", "up", "lacp", 3, 2),
            channel("Trk2", "down", "lacp", 2, 0),
            channel("Dyn1", "up", "lacp", 3, 1),
        ],
    ),
    (
        "port_channel_members",
        "lacp_vendor",
        [
            member("Trk1", "A1", "bundled"),
            member("Trk1", "A2", "bundled"),
            member("Trk1", "A3", "down"),
            member("Trk2", "B1", "standalone"),
            member("Trk2", "B2", "suspended"),
            member("Dyn1", "D1", "bundled"),
            member("Dyn1", "D2", "hot-standby"),
            member("Dyn1", "D3", "down"),
        ],
    ),
    (
        "ospf_neighbors",
        "ospf_vendor",
        [
            ospf("10.255.0.34", "10.3.18.34", "10.3.18.34", "full", "dr"),
            ospf("10.255.0.38", "10.3.53.38", "10.3.53.38", "full", "bdr"),
            ospf("10.255.0.40", "10.3.54.40", "10.3.54.40", "2way", ""),
            ospf("10.255.0.41", "10.3.55.41", "10.3.55.41", "init", ""),
        ],
    ),
    (
        "bgp_neighbors",
        "bgp_vendor",
        [
            bgp("10.0.8.2", 65009, "established"),
            bgp("10.0.9.2", 65008, "active"),
            bgp("10.0.10.2", 65010, "admin-down"),
        ],
    ),
    (
        "stack",
        "stack_vsf_vendor",
        [
            stack_row(1, "member", "ready"),
            stack_row(2, "member", "ready"),
            stack_row(3, "standby", "ready"),
            stack_row(7, "active", "ready"),
        ],
    ),
    (
        "stack",
        "stack_stacking_vendor",
        [
            stack_row(1, "member", "missing"),
            stack_row(2, "active", "ready"),
            stack_row(3, "member", "missing"),
        ],
    ),
    (
        "vlans",
        "vlans_ntc_pipe",
        [
            vlan(1, "DEFAULT_VLAN", "active"),
            vlan(10, "Router Vlan", "active"),
            vlan(50, "Voice", "active"),
        ],
    ),
    (
        "vlans",
        "vlans_ntc_jumbo",
        [vlan(1, "DEFAULT_VLAN", "active"), vlan(100, "DEVOPS", "active")],
    ),
    (
        "routes",
        "routes_vendor",
        [route("connected", 1), route("static", 1), route("ospf", 4), route("total", 6)],
    ),
    (
        "vrrp",
        "vrrp_vendor",
        [
            fhrp("vlan10", 11, "master", 11),
            fhrp("vlan10", 123, "master", 202),
            fhrp("vlan110", 124, "backup", 202),
        ],
    ),
    (
        "spanning_tree",
        "stp_vendor",
        [stp("MST0", 1, 2), stp("total", 1, 2)],
    ),
]


def _cases(family: str, cases: list) -> list:
    return [
        pytest.param(family, table, name, expected, id=f"{family}-{table}-{name}")
        for table, base, expected in cases
        for name in variants(family, base)
    ]


@pytest.mark.parametrize(
    ("family", "table", "fixture", "expected"),
    _cases("aos-cx", CX_CASES) + _cases("aos-s", AOSS_CASES),
)
def test_fixture_parses_to_exact_rows(family, table, fixture, expected):
    assert parse_table(family, table, fixture_text(family, fixture)) == [
        {
            name: row.get(name, 0 if TABLES[table].column(name).kind == "int" else "")
            for name in TABLES[table].column_names
        }
        for row in expected
    ]


# -- contract -----------------------------------------------------------------------------


@pytest.mark.parametrize(("family", "module"), [("aos-cx", aoscx), ("aos-s", aoss)])
def test_commands_and_parsers_agree(family, module):
    assert set(module.COMMANDS) == set(module.PARSERS)
    assert all(command.startswith("show ") for command in module.COMMANDS.values())
    supported = supported_tables(family)
    assert set(supported) <= set(TABLES)
    assert set(supported) == set(module.COMMANDS)
    assert "eigrp_neighbors" not in supported
    assert "hsrp" not in supported


def test_both_families_offer_the_tables_the_research_supports():
    expected = {
        "interfaces",
        "port_channels",
        "port_channel_members",
        "ospf_neighbors",
        "bgp_neighbors",
        "stack",
        "vlans",
        "routes",
        "vrrp",
        "spanning_tree",
    }
    assert set(supported_tables("aos-cx")) == expected
    assert set(supported_tables("aos-s")) == expected


# -- empty output, banners and features that are off --------------------------------------

BANNER = (
    "Welcome to the switch\n"
    "Last login: Mon Nov 16 09:00:00 2026 from 192.0.2.10\n"
    "Use of this system is for authorized users only.\n"
)
FEATURE_OFF = {
    "aos-cx": ["VRRP is not enabled", "BGP is not running", "Spanning tree status : Disabled"],
    "aos-s": ["VRRP is not enabled", "BGP is not running", " STP Enabled : No"],
}


@pytest.mark.parametrize(
    ("family", "table"),
    [(f, t) for f in ("aos-cx", "aos-s") for t in supported_tables(f)],
)
@pytest.mark.parametrize("text", ["", "\n\n", BANNER], ids=["empty", "blank", "banner"])
def test_empty_banner_and_feature_off_give_no_rows(family, table, text):
    assert parse_table(family, table, text) == []


@pytest.mark.parametrize(
    ("family", "table"),
    [(f, t) for f in ("aos-cx", "aos-s") for t in supported_tables(f)],
)
def test_feature_off_lines_give_no_rows(family, table):
    for line in FEATURE_OFF[family]:
        assert parse_table(family, table, line + "\n") == []


@pytest.mark.parametrize(
    ("family", "text"),
    [
        ("aos-cx", "Invalid input: ospf\n"),
        ("aos-cx", "% Unknown command.\n"),
        ("aos-s", "Invalid input: vsf\n"),
    ],
)
def test_a_rejected_command_is_an_error_not_an_empty_table(family, text):
    with pytest.raises(StateError):
        parse_table(family, "stack", text)


@pytest.mark.parametrize(
    ("family", "table"),
    [(f, t) for f in ("aos-cx", "aos-s") for t in supported_tables(f)],
)
def test_crlf_output_parses_like_lf(family, table):
    cases = CX_CASES if family == "aos-cx" else AOSS_CASES
    base = next(b for t, b, _ in cases if t == table)
    text = fixture_text(family, base)
    assert parse_table(family, table, text.replace("\n", "\r\n")) == parse_table(
        family, table, text
    )


# -- hazards the research lists -----------------------------------------------------------


def test_cx_interface_with_the_enabled_flag_in_the_description():
    text = (
        "1/1/7      1       access   1GbT           yes     down    Waiting for link  --  yes up\n"
    )
    assert parse_table("aos-cx", "interfaces", text) == [iface("1/1/7", "down", "down")]


def test_cx_old_master_word_is_the_conductor():
    text = "Mbr  Mac Address         type   Status\nID\n1    38:21:c7:5c:62:40   JL668A  Master\n"
    assert parse_table("aos-cx", "stack", text) == [stack_row(1, "active", "ready")]


def test_cx_split_vsf_member_in_the_other_fragment():
    text = "3    38:21:c7:5c:57:c0   JL668A  In Other Fragment\n"
    assert parse_table("aos-cx", "stack", text) == [stack_row(3, "member", "other-fragment")]


def test_cx_routes_without_a_vrf_header_count_as_default():
    text = "Protocol        Active Routes\nconnected       2\nlocal           2\n"
    assert parse_table("aos-cx", "routes", text) == [
        route("connected", 2),
        route("local", 2),
        route("total", 4),
    ]


def test_cx_routes_of_other_vrfs_are_not_added_to_the_default_table():
    rows = parse_table("aos-cx", "routes", fixture_text("aos-cx", "routes_vendor"))
    assert route("connected", 3) in rows
    assert route("connected", 7) not in rows


def test_cx_bgp_peer_in_two_address_families_is_one_row():
    rows = parse_table("aos-cx", "bgp_neighbors", fixture_text("aos-cx", "bgp_ntc"))
    assert [r["neighbor"] for r in rows].count("172.25.0.1") == 1


def test_cx_vlan_interface_list_continuation_is_not_a_vlan():
    rows = parse_table("aos-cx", "vlans", fixture_text("aos-cx", "vlans_ntc"))
    assert [r["id"] for r in rows] == [1, 10, 23, 3072]


def test_cx_ospf_point_to_point_has_no_role():
    text = "10.255.0.4  0  FULL/ -  10.1.3.2  1/1/4\n"
    assert parse_table("aos-cx", "ospf_neighbors", text)[0]["role"] == ""


def test_cx_lacp_partner_section_is_not_read_as_members():
    rows = parse_table("aos-cx", "port_channel_members", fixture_text("aos-cx", "lacp_vendor"))
    assert len(rows) == 8


def test_s_marker_glued_to_the_port_is_dropped():
    rows = parse_table("aos-s", "interfaces", fixture_text("aos-s", "interfaces_ntc_chassis"))
    assert [r["name"] for r in rows] == ["A1", "A8", "D1", "D5", "E3", "F1"]


def test_s_interface_with_empty_type_and_mode_still_reads_its_status():
    text = "  E3                      | No        Yes     Down   .               off  0\n"
    assert parse_table("aos-s", "interfaces", text) == [iface("E3", "down", "down")]


def test_s_ospf_blank_nbifstate_does_not_shift_the_state():
    rows = parse_table("aos-s", "ospf_neighbors", fixture_text("aos-s", "ospf_vendor"))
    assert [(r["state"], r["role"]) for r in rows][2:] == [("2way", ""), ("init", "")]


def test_s_vrrp_uptime_with_a_space_and_owner_word_do_not_hide_the_state():
    text = " 110  124  202 2 mins   Backup True  Backup  110.1.1.1   110.1.1.110\n"
    assert parse_table("aos-s", "vrrp", text) == [fhrp("vlan110", 124, "backup", 202)]


def test_s_lacp_port_that_is_not_trunked_is_not_a_member():
    rows = parse_table("aos-s", "port_channel_members", fixture_text("aos-s", "lacp_vendor"))
    assert "C1" not in [r["member"] for r in rows]


def test_s_vlan_names_with_spaces_are_kept_whole():
    rows = parse_table("aos-s", "vlans", fixture_text("aos-s", "vlans_ntc_pipe_tight"))
    assert rows[1]["name"] == "Router Vlan"


def test_s_stack_model_with_dots_and_spaces_does_not_break_the_row():
    rows = parse_table("aos-s", "stack", fixture_text("aos-s", "stack_vsf_vendor"))
    assert [r["member"] for r in rows] == [1, 2, 3, 7]


def test_s_stp_short_row_of_a_disconnected_port_counts_as_neither():
    rows = parse_table("aos-s", "spanning_tree", fixture_text("aos-s", "stp_vendor"))
    assert rows[0] == stp("MST0", 1, 2)
