"""IOS and IOS-XE state table parsers against captured output (design.md, State checks).

The fixtures under tests/fixtures/state/ios named *_iol_*, *_ioll2_*, *_iosv_* and *_c1101_*
were captured from lab nodes and a C1101; *_ntc and *_genie_* are real device captures
taken from the test data of the ntc-templates and genieparser projects."""

from __future__ import annotations

from pathlib import Path

import pytest

from tsunami.core.state import StateAssertion, evaluate, parse_table, table
from tsunami.core.state.parsers import ios
from tsunami.core.state.parsers.helpers import long_interface

FIXTURES = Path(__file__).parents[1] / "fixtures" / "state" / "ios"


def rows(name: str, fixture: str) -> list[dict]:
    text = (FIXTURES / f"{fixture}.txt").read_text(encoding="utf-8")
    return parse_table("ios", name, text)


def test_commands_and_parsers_name_the_same_tables():
    assert set(ios.COMMANDS) == set(ios.PARSERS)
    assert all(command.startswith("show ") for command in ios.COMMANDS.values())


@pytest.mark.parametrize(
    ("short", "full"),
    [
        ("Gi1/0/1", "GigabitEthernet1/0/1"),
        ("Te2/0/3", "TenGigabitEthernet2/0/3"),
        ("Twe1/0/1", "TwentyFiveGigE1/0/1"),
        ("Tw1/0/2", "TwoGigabitEthernet1/0/2"),
        ("Fi1/0/5", "FiveGigabitEthernet1/0/5"),
        ("Fo1/1/1", "FortyGigabitEthernet1/1/1"),
        ("Hu1/0/49", "HundredGigE1/0/49"),
        ("Fa0/1", "FastEthernet0/1"),
        ("Et0/2", "Ethernet0/2"),
        ("Po12", "Port-channel12"),
        ("Vl10", "Vlan10"),
        ("Lo0", "Loopback0"),
        ("Tu100", "Tunnel100"),
        ("GigabitEthernet0/0/0", "GigabitEthernet0/0/0"),
        ("Port-channel1", "Port-channel1"),
        ("mgmt0", "mgmt0"),
    ],
)
def test_long_interface(short, full):
    assert long_interface(short) == full


# -- port channels ------------------------------------------------------------------------


def test_port_channels_with_wrapped_member_lists():
    assert rows("port_channels", "port_channels_lacp_ntc") == [
        {
            "name": "Port-channel1",
            "status": "up",
            "protocol": "lacp",
            "members": 4,
            "members_up": 4,
        },
        {
            "name": "Port-channel3",
            "status": "up",
            "protocol": "lacp",
            "members": 2,
            "members_up": 2,
        },
        {
            "name": "Port-channel13",
            "status": "up",
            "protocol": "lacp",
            "members": 1,
            "members_up": 1,
        },
    ]
    members = rows("port_channel_members", "port_channels_lacp_ntc")
    assert [m["member"] for m in members if m["port_channel"] == "Port-channel1"] == [
        "TenGigabitEthernet6/4",
        "TenGigabitEthernet3/5",
        "TenGigabitEthernet9/4",
        "TenGigabitEthernet5/5",  # from the continuation line
    ]
    assert {m["status"] for m in members} == {"bundled"}


def test_port_channels_down_static_and_partly_bundled():
    found = {r["name"]: r for r in rows("port_channels", "port_channels_mixed_genie_iosxe")}
    assert found["Port-channel1"] == {
        "name": "Port-channel1",
        "status": "down",
        "protocol": "static",
        "members": 0,
        "members_up": 0,
    }
    # Six members over three lines; two bundled, two suspended, two down.
    assert found["Port-channel3"]["status"] == "up"
    assert (found["Port-channel3"]["members"], found["Port-channel3"]["members_up"]) == (6, 2)
    # A Layer3 port channel that is down: R is the layer, not a state.
    assert found["Port-channel4"]["status"] == "down"

    members = rows("port_channel_members", "port_channels_mixed_genie_iosxe")
    by_port = {m["member"]: m["status"] for m in members}
    assert by_port == {
        "TenGigabitEthernet2/0/1": "bundled",
        "TenGigabitEthernet2/0/2": "down",
        "TenGigabitEthernet2/0/3": "suspended",
        "TenGigabitEthernet3/0/1": "suspended",
        "TenGigabitEthernet3/0/2": "down",
        "TenGigabitEthernet3/0/3": "bundled",
        "FiveGigabitEthernet1/0/5": "down",
        "GigabitEthernet4/0/5": "down",
    }


def test_port_channels_on_a_router_with_tabs_words_and_a_footer():
    assert rows("port_channels", "port_channels_router_ntc") == [
        {
            "name": "Port-channel10",
            "status": "up",
            "protocol": "lacp",
            "members": 2,
            "members_up": 2,
        }
    ]
    assert rows("port_channel_members", "port_channels_router_ntc") == [
        {
            "port_channel": "Port-channel10",
            "member": "TenGigabitEthernet0/0/2",
            "status": "bundled",
        },
        {
            "port_channel": "Port-channel10",
            "member": "TenGigabitEthernet0/0/3",
            "status": "bundled",
        },
    ]


@pytest.mark.parametrize(
    "fixture", ["port_channels_empty_c1101_16_9", "port_channels_empty_ioll2_17_18"]
)
def test_no_port_channels_is_an_empty_table_under_either_legend(fixture):
    assert rows("port_channels", fixture) == []
    assert rows("port_channel_members", fixture) == []


def test_other_member_flags():
    text = (
        "Group  Port-channel  Protocol    Ports\n"
        "------+-------------+-----------+----------------\n"
        "1      Po1(SU)         PAgP      Gi1/0/1(P)  Gi1/0/2(I)  Gi1/0/3(H)  Gi1/0/4(w)\n"
        "2      Po2(SM)         LACP      Gi1/0/5(D)\n"
    )
    assert [m["status"] for m in parse_table("ios", "port_channel_members", text)] == [
        "bundled",
        "standalone",
        "hot-standby",
        "other",
        "down",
    ]
    channels = parse_table("ios", "port_channels", text)
    assert channels[0]["protocol"] == "pagp"
    assert channels[1]["status"] == "down"  # M: not in use, minimum links not met


def test_the_check_an_uplink_port_channel_needs():
    """What a play asserts before and after a change: the channel is up with both
    members, and no member dropped out of the bundle."""
    before = rows("port_channels", "port_channels_lacp_ntc")
    spec = table("port_channels")
    up = StateAssertion(
        "port_channels",
        {"name": "Port-channel3"},
        {"status": "up", "members_up": {"min": 2}},
    )
    assert evaluate(spec, up, before).ok

    after = [dict(r, members_up=1) if r["name"] == "Port-channel3" else r for r in before]
    lost = evaluate(spec, up, after)
    assert not lost.ok and "members_up min 2" in lost.detail
    compared = StateAssertion("port_channels", {}, {}, compare="no_loss")
    result = evaluate(spec, compared, after, before)
    assert not result.ok and "name=Port-channel3 (members_up 2 -> 1)" in result.detail


# -- the other tables ---------------------------------------------------------------------


def test_vlans():
    found = rows("vlans", "vlans_c1101_16_9")
    assert found[:4] == [
        {"id": 1, "name": "default", "status": "active"},
        {"id": 10, "name": "Internal", "status": "active"},
        {"id": 20, "name": "Guest", "status": "active"},
        {"id": 30, "name": "DMZ", "status": "active"},
    ]
    assert found[-1] == {"id": 1005, "name": "trnet-default", "status": "act/unsup"}
    assert len(rows("vlans", "vlans_ioll2_17_18")) == 5


def test_vlans_ignores_wrapped_port_lists_and_reads_suspended():
    text = (
        "VLAN Name                             Status    Ports\n"
        "---- -------------------------------- --------- -------------------------------\n"
        "1    default                          active    Gi1/0/1, Gi1/0/2, Gi1/0/3, Gi1/0/4\n"
        "                                                Gi1/0/5, Gi1/0/6\n"
        "20   Printers                         suspended\n"
        "30   Lab                              act/lshut Gi1/0/9\n"
    )
    assert [(r["id"], r["status"]) for r in parse_table("ios", "vlans", text)] == [
        (1, "active"),
        (20, "suspended"),
        (30, "act/lshut"),
    ]


def test_routes_counts_networks_and_subnets_per_source():
    assert rows("routes", "routes_iol_17_18") == [
        {"source": "connected", "routes": 7},
        {"source": "static", "routes": 0},
        {"source": "application", "routes": 0},
        {"source": "ospf 1", "routes": 3},
        {"source": "bgp 65010", "routes": 0},
        {"source": "total", "routes": 11},
    ]
    assert {"source": "static", "routes": 1} in rows("routes", "routes_static_iol_17_18")
    assert rows("routes", "routes_c1101_16_9")[-1] == {"source": "total", "routes": 12}


def test_spanning_tree_per_instance_and_total():
    assert rows("spanning_tree", "spanning_tree_summary_ioll2_17_18") == [
        {"instance": "VLAN0001", "blocking": 0, "forwarding": 3},
        {"instance": "total", "blocking": 0, "forwarding": 3},
    ]
    several = (
        "Name                   Blocking Listening Learning Forwarding STP Active\n"
        "---------------------- -------- --------- -------- ---------- ----------\n"
        "VLAN0001                     1         0        0          7          8\n"
        "VLAN0010                     2         0        0          5          7\n"
        "---------------------- -------- --------- -------- ---------- ----------\n"
        "2 vlans                      3         0        0         12         15\n"
    )
    assert parse_table("ios", "spanning_tree", several) == [
        {"instance": "VLAN0001", "blocking": 1, "forwarding": 7},
        {"instance": "VLAN0010", "blocking": 2, "forwarding": 5},
        {"instance": "total", "blocking": 3, "forwarding": 12},
    ]


def test_interfaces_ignore_the_description_column():
    found = {r["name"]: r for r in rows("interfaces", "interfaces_iol_17_18")}
    assert found["Ethernet0/3"] == {
        "name": "Ethernet0/3",
        "status": "admin-down",
        "protocol": "down",
    }
    # A description that reads like a status must not move the columns.
    text = (
        "Interface                      Status         Protocol Description\n"
        "Gi1/0/1                        up             up       down for maintenance up\n"
        "Po1                            down           down     uplink admin down\n"
    )
    assert parse_table("ios", "interfaces", text) == [
        {"name": "GigabitEthernet1/0/1", "status": "up", "protocol": "up"},
        {"name": "Port-channel1", "status": "down", "protocol": "down"},
    ]


@pytest.mark.parametrize("name", sorted(ios.PARSERS))
def test_every_parser_takes_empty_and_unrelated_output(name):
    assert parse_table("ios", name, "") == []
    assert parse_table("ios", name, "% Feature not active\n") == []
    assert parse_table("ios", name, "some banner text\nmore words here 1 2 3\n") == []
