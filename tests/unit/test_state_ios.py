"""IOS and IOS-XE state table parsers against captured output (design.md, State checks).

The fixtures under tests/fixtures/state/ios named *_iol_*, *_ioll2_*, *_iosv_* and *_c1101_*
were captured from lab nodes and a C1101; *_ntc and *_genie_* are real device captures
taken from the test data of the ntc-templates and genieparser projects."""

from __future__ import annotations

from pathlib import Path

import pytest

from tsunami.core.state import (
    TABLES,
    StateAssertion,
    evaluate,
    parse_table,
    supported_tables,
    table,
)
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


# -- bgp, eigrp, neighbors, stack and first-hop redundancy ---------------------------------

NEIGHBORS = Path(__file__).parents[1] / "fixtures" / "neighbors" / "ios"


def neighbor_rows(name: str, fixture: str) -> list[dict]:
    return parse_table("ios", name, (NEIGHBORS / f"{fixture}.txt").read_text(encoding="utf-8"))


def test_ios_reads_every_table():
    assert supported_tables("ios") == tuple(TABLES)


def test_bgp_established_counts_and_state_words():
    assert rows("bgp_neighbors", "bgp_summary_ntc") == [
        {"neighbor": "10.0.0.1", "remote_as": 65000, "state": "established", "prefixes": 558720},
        {"neighbor": "10.0.0.3", "remote_as": 65002, "state": "established", "prefixes": 0},
        {"neighbor": "10.0.0.5", "remote_as": 65004, "state": "established", "prefixes": 558720},
        {"neighbor": "10.0.0.10", "remote_as": 65009, "state": "admin-down", "prefixes": 0},
    ]
    assert rows("bgp_neighbors", "bgp_summary_vendor")[0] == {
        "neighbor": "10.100.1.1",
        "remote_as": 200,
        "state": "established",
        "prefixes": 23,
    }


def test_bgp_address_family_blocks_never_and_ipv6_neighbors():
    found = rows("bgp_neighbors", "bgp_summary_genie_iosxe")
    assert [(r["neighbor"], r["state"], r["prefixes"]) for r in found] == [
        ("192.168.111.1", "idle", 0),
        ("192.168.4.1", "idle", 0),
        ("2001:db8:400::1:1", "idle", 0),
        ("2001::14:4", "idle", 0),
    ]


def test_bgp_state_words_asdot_dynamic_peers_and_a_wrapped_neighbor():
    text = (
        "Neighbor        V           AS MsgRcvd MsgSent   TblVer  InQ OutQ Up/Down  State/PfxRcd\n"
        "192.0.2.1       4        65001       0       0        1    0    0 never    Active\n"
        "192.0.2.2       4        65002       0       0        1    0    0 00:00:05 Connect\n"
        "192.0.2.3       4        65003       3       3        1    0    0 00:00:07 OpenSent\n"
        "192.0.2.4       4        65004       3       3        1    0    0 00:00:09 OpenConfirm\n"
        "192.0.2.5       4        65005       9       9        1    0    0 1d02h    Idle (PfxCt)\n"
        "*192.0.2.6      4        65000.1000  9       9        1    0    0 1d02h    4\n"
        "2001:db8:400:0:0:0:0:1\n"
        "                4        65007     120     120       55    0    0 2d03h    12\n"
    )
    found = parse_table("ios", "bgp_neighbors", text)
    assert [(r["neighbor"], r["state"], r["prefixes"]) for r in found] == [
        ("192.0.2.1", "active", 0),
        ("192.0.2.2", "connect", 0),
        ("192.0.2.3", "opensent", 0),
        ("192.0.2.4", "openconfirm", 0),
        ("192.0.2.5", "idle", 0),
        ("192.0.2.6", "established", 4),
        ("2001:db8:400:0:0:0:0:1", "established", 12),
    ]
    assert found[5]["remote_as"] == 65000 * 65536 + 1000
    assert found[6]["remote_as"] == 65007


def test_bgp_not_active_is_an_empty_table():
    assert rows("bgp_neighbors", "bgp_not_active") == []


def test_eigrp_classic_header_with_a_two_line_column_title():
    assert rows("eigrp_neighbors", "eigrp_neighbors_classic_ntc") == [
        {"address": "192.168.1.2", "interface": "GigabitEthernet0/0", "as_number": 1},
        {"address": "192.168.2.2", "interface": "GigabitEthernet0/1", "as_number": 1},
    ]


def test_eigrp_as_header_with_tunnel_and_long_uptimes():
    assert rows("eigrp_neighbors", "eigrp_neighbors_as_ntc") == [
        {"address": "10.1.1.122", "interface": "Tunnel15", "as_number": 10},
        {"address": "10.1.1.34", "interface": "Tunnel117", "as_number": 10},
        {"address": "192.168.1.239", "interface": "GigabitEthernet0/0/2", "as_number": 10},
    ]


def test_eigrp_process_header_behind_a_timestamp_preamble():
    assert rows("eigrp_neighbors", "eigrp_neighbors_process_ntc") == [
        {"address": "10.120.250.1", "interface": "Vlan250", "as_number": 535}
    ]


def test_eigrp_named_mode_and_repeated_headers_set_the_as_per_block():
    text = (
        "EIGRP-IPv4 VR(corp) Address-Family Neighbors for AS(100)\n"
        "H   Address                 Interface              Hold Uptime   SRTT   RTO  Q  Seq\n"
        "                                                   (sec)         (ms)       Cnt Num\n"
        "0   10.1.1.2                Gi0/1                    12 00:01:02    2   100  0  5\n"
        "EIGRP-IPv4 VR(corp) Address-Family Neighbors for AS(200) VRF(red)\n"
        "H   Address                 Interface              Hold Uptime   SRTT   RTO  Q  Seq\n"
        "                                                   (sec)         (ms)       Cnt Num\n"
        "0   10.1.1.2                Gi0/1                    12 00:01:02    2   100  0  5\n"
    )
    found = parse_table("ios", "eigrp_neighbors", text)
    assert [(r["address"], r["as_number"]) for r in found] == [("10.1.1.2", 100), ("10.1.1.2", 200)]


def test_eigrp_header_only_is_an_empty_table():
    text = "EIGRP-IPv4 Neighbors for AS(10)\nH   Address     Interface   Hold Uptime\n"
    assert parse_table("ios", "eigrp_neighbors", text) == []


def test_cdp_detail_uses_full_names():
    assert neighbor_rows("cdp_neighbors", "cdp-detail-switch") == [
        {
            "local_interface": "Ethernet0/1",
            "neighbor": "DSC-SW3.lab.local",
            "remote_port": "Ethernet0/0",
        },
        {
            "local_interface": "Ethernet0/2",
            "neighbor": "dsc-sw4.lab.local",
            "remote_port": "Ethernet0/1",
        },
        {
            "local_interface": "Ethernet0/0",
            "neighbor": "dsc-sw1.lab.local",
            "remote_port": "Ethernet0/1",
        },
    ]


def test_cdp_with_no_neighbors_is_empty():
    assert parse_table("ios", "cdp_neighbors", "Total cdp entries displayed : 0\n") == []


def test_lldp_detail_expands_abbreviated_ports():
    assert neighbor_rows("lldp_neighbors", "lldp-detail-switch") == [
        {
            "local_interface": "Ethernet0/0",
            "neighbor": "dsc-sw1.lab.local",
            "remote_port": "Ethernet0/1",
        },
        {
            "local_interface": "Ethernet0/1",
            "neighbor": "DSC-SW3.lab.local",
            "remote_port": "Ethernet0/0",
        },
        {
            "local_interface": "Ethernet0/2",
            "neighbor": "dsc-sw4.lab.local",
            "remote_port": "Ethernet0/1",
        },
    ]


def test_lldp_falls_back_to_the_chassis_id_and_is_empty_when_off():
    text = (
        "------------------------------------------------\n"
        "Local Intf: Gi1/0/3\n"
        "Chassis id: aabb.cc00.b900\n"
        "Port id: 0050.5600.0001\n"
        "System Name: \n"
        "\n"
        "Total entries displayed: 1\n"
    )
    assert parse_table("ios", "lldp_neighbors", text) == [
        {
            "local_interface": "GigabitEthernet1/0/3",
            "neighbor": "aabb.cc00.b900",
            "remote_port": "0050.5600.0001",
        }
    ]
    assert rows("lldp_neighbors", "lldp_not_enabled") == []


def test_stack_iosxe_roles_and_the_local_marker():
    assert rows("stack", "stack_ntc_iosxe") == [
        {"member": 1, "role": "active", "state": "ready"},
        {"member": 2, "role": "member", "state": "ready"},
        {"member": 3, "role": "member", "state": "ready"},
        {"member": 4, "role": "standby", "state": "ready"},
    ]


def test_stack_provisioned_members_have_no_hardware_version():
    assert rows("stack", "stack_provisioned_genie_iosxe") == [
        {"member": 1, "role": "active", "state": "ready"},
        {"member": 2, "role": "member", "state": "provisioned"},
        {"member": 3, "role": "member", "state": "provisioned"},
    ]


def test_stack_ios15_master_is_active_and_the_port_table_is_not_rows():
    assert rows("stack", "stack_detail_ios15_ntc") == [
        {"member": 1, "role": "active", "state": "ready"},
        {"member": 2, "role": "member", "state": "ready"},
    ]


def test_stack_states_with_several_words():
    text = (
        "Switch#   Role    Mac Address     Priority Version  State\n"
        "-------------------------------------------------------------\n"
        "*1       Active   cc98.9114.3580     15     V01     Ready   \n"
        " 2       Standby  706b.b9c8.2380     14     V01     HA Sync in Progress\n"
        " 3       Member   a0f8.4911.c080     1      V01     V-Mismatch\n"
        " 4       Member   a0f8.4911.c081     1      V01     Sync not started\n"
    )
    assert [(r["member"], r["state"]) for r in parse_table("ios", "stack", text)] == [
        (1, "ready"),
        (2, "ha sync in progress"),
        (3, "v-mismatch"),
        (4, "sync not started"),
    ]


def test_hsrp_wrapped_interface_and_blank_preempt_column():
    assert rows("hsrp", "hsrp_brief_ntc") == [
        {"interface": "Vlan1", "group": 1, "state": "active", "priority": 120},
        {"interface": "Vlan1", "group": 6, "state": "active", "priority": 120},
        {"interface": "Vlan51", "group": 1, "state": "init", "priority": 120},
        {"interface": "Vlan4010", "group": 1, "state": "active", "priority": 120},
    ]
    assert rows("hsrp", "hsrp_brief_nopreempt_genie_iosxe") == [
        {"interface": "Vlan1309", "group": 1, "state": "init", "priority": 110}
    ]


def test_hsrp_other_states_in_any_case():
    text = (
        "Interface   Grp  Pri P State   Active          Standby         Virtual IP\n"
        "Gi0/0       1    100   Standby local           10.0.0.2        10.0.0.1\n"
        "Gi0/0       2    100   Speak   unknown         unknown         10.0.0.3\n"
        "Gi0/0       3    100   LISTEN  10.0.0.9        10.0.0.8        10.0.0.4\n"
        "Gi0/0       4    100 P Disabled unknown        unknown         10.0.0.5\n"
    )
    assert [(r["group"], r["state"]) for r in parse_table("ios", "hsrp", text)] == [
        (1, "standby"),
        (2, "speak"),
        (3, "listen"),
        (4, "disabled"),
    ]


def test_vrrp_v2_with_a_blank_own_column():
    assert rows("vrrp", "vrrp_brief_ntc") == [
        {"interface": "FastEthernet0/1", "group": 1, "state": "master", "priority": 110},
        {"interface": "FastEthernet0/1.3", "group": 3, "state": "backup", "priority": 110},
        {"interface": "FastEthernet0/1.4", "group": 4, "state": "init", "priority": 100},
    ]


def test_vrrp_v3_has_an_address_family_and_upper_case_states():
    assert rows("vrrp", "vrrp_brief_v3_ntc") == [
        {"interface": "GigabitEthernet0/0", "group": 1, "state": "master", "priority": 110},
        {"interface": "GigabitEthernet0/0", "group": 6, "state": "master", "priority": 110},
        {"interface": "GigabitEthernet0/0.4010", "group": 1, "state": "backup", "priority": 110},
    ]


def test_vrrp_header_only_is_an_empty_table():
    assert rows("vrrp", "vrrp_empty_iol_17_18") == []
