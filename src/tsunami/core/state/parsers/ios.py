"""State table parsers for Cisco IOS and IOS-XE.

The formats and their variations are in docs/research/state-tables.md; the fixtures under
tests/fixtures/state/ios are real output (IOL 17.18, IOSv 15.9, a C1101 on 16.9).
"""

from __future__ import annotations

import re

from tsunami.core.state.parsers.helpers import data_lines, long_interface
from tsunami.core.state.tables import Row

COMMANDS: dict[str, str] = {
    "interfaces": "show interfaces description",
    "port_channels": "show etherchannel summary",
    "port_channel_members": "show etherchannel summary",
    "ospf_neighbors": "show ip ospf neighbor",
    "vlans": "show vlan brief",
    "routes": "show ip route summary",
    "spanning_tree": "show spanning-tree summary",
}

# Interface                      Status         Protocol Description
# Gi0/1/1                        admin down     down     SPARE
_INTERFACE = re.compile(
    r"^(?P<name>\S+)\s+(?P<status>admin down|up|down|deleted|reset|testing)\s+"
    r"(?P<protocol>up|down)\b",
    re.I,
)


def interfaces(output: str) -> list[Row]:
    rows: list[Row] = []
    for line in data_lines(output):
        match = _INTERFACE.match(line)
        if match is None:  # the header, or a wrapped description
            continue
        status = match["status"].lower()
        rows.append(
            {
                "name": long_interface(match["name"]),
                "status": "admin-down" if status == "admin down" else status,
                "protocol": match["protocol"].lower(),
            }
        )
    return rows


# Neighbor ID     Pri   State           Dead Time   Address         Interface
# 10.255.0.4        0   FULL/  -        00:00:33    10.10.34.2      Ethernet0/2
_OSPF = re.compile(
    r"^(?P<id>\d+\.\d+\.\d+\.\d+)\s+\d+\s+(?P<state>[A-Z0-9-]+)\s*/\s*(?P<role>[A-Z-]+)\s+"
    r"\S+\s+(?P<address>\d+\.\d+\.\d+\.\d+)\s+(?P<interface>\S+)\s*$",
    re.I,
)
_OSPF_STATES = {"2-way": "2way", "two-way": "2way"}


def ospf_neighbors(output: str) -> list[Row]:
    rows: list[Row] = []
    for line in data_lines(output):
        match = _OSPF.match(line)
        if match is None:
            continue
        state = match["state"].lower()
        role = match["role"].lower()
        rows.append(
            {
                "neighbor_id": match["id"],
                "address": match["address"],
                "interface": long_interface(match["interface"]),
                "state": _OSPF_STATES.get(state, state),
                "role": "" if role == "-" else role,
            }
        )
    return rows


# Group  Port-channel  Protocol    Ports
# ------+-------------+-----------+-----------------------------------------------
# 3      Po3(SU)        LACP        Te2/0/1(P)      Te2/0/2(D)
#                                   Te2/0/3(s)      Te3/0/1(s)
# Routers print tabs between the columns and words for the member flags:
# 10	Po10(RU)		LACP	 Te0/0/2(bndl) Te0/0/3(bndl)
_CHANNEL = re.compile(
    r"^\s*\d+\s+(?P<name>Po\d+)\((?P<flags>[A-Za-z]+)\)\s+(?P<protocol>LACP|PAgP|-)(?P<rest>.*)$",
    re.I,
)
_MEMBER = re.compile(r"(?P<port>[A-Za-z][\w/.:-]*\d)\((?P<flag>[A-Za-z-]+)\)")
# Member flags are case-sensitive: `s` is suspended, `S` (on the port channel) is Layer2.
_MEMBER_STATUS = {
    "P": "bundled",
    "bndl": "bundled",
    "D": "down",
    "s": "suspended",
    "susp": "suspended",
    "I": "standalone",
    "H": "hot-standby",
}
_CHANNEL_PROTOCOL = {"lacp": "lacp", "pagp": "pagp", "-": "static"}


def _channels(output: str) -> list[tuple[str, str, str, list[tuple[str, str]]]]:
    """Each group as (name, flags, protocol, [(member, status)]). A group's member list
    wraps onto lines that hold nothing but members; legend and footer lines hold none."""
    groups: list[tuple[str, str, str, list[tuple[str, str]]]] = []
    in_table = False
    for line in data_lines(output):
        if line.lstrip().startswith("------+"):
            in_table = True
            continue
        if not in_table:
            continue
        match = _CHANNEL.match(line)
        rest = match["rest"] if match else line
        members = [
            (long_interface(m["port"]), _MEMBER_STATUS.get(m["flag"], "other"))
            for m in _MEMBER.finditer(rest)
        ]
        if match:
            protocol = _CHANNEL_PROTOCOL[match["protocol"].lower()]
            groups.append((long_interface(match["name"]), match["flags"], protocol, members))
        elif groups and members and not _MEMBER.sub("", rest).strip():
            groups[-1][3].extend(members)
    return groups


def port_channels(output: str) -> list[Row]:
    return [
        {
            "name": name,
            "status": "up" if "U" in flags else "down",
            "protocol": protocol,
            "members": len(members),
            "members_up": sum(1 for _, status in members if status == "bundled"),
        }
        for name, flags, protocol, members in _channels(output)
    ]


def port_channel_members(output: str) -> list[Row]:
    return [
        {"port_channel": name, "member": member, "status": status}
        for name, _flags, _protocol, members in _channels(output)
        for member, status in members
    ]


# VLAN Name                             Status    Ports
# 1    default                          active    Et0/1, Et0/2, Et0/3
# A long Ports list wraps onto lines that start with spaces: those are not rows.
_VLAN = re.compile(r"^(?P<id>\d+)\s+(?P<name>\S+)\s+(?P<status>(?:act|sus)\S*)(?:\s|$)", re.I)


def vlans(output: str) -> list[Row]:
    rows: list[Row] = []
    for line in data_lines(output):
        match = _VLAN.match(line)
        if match is not None:
            rows.append(
                {"id": match["id"], "name": match["name"], "status": match["status"].lower()}
            )
    return rows


# Route Source    Networks    Subnets     Replicates  Overhead    Memory (bytes)
# ospf 1          0           3           0           336         948
#   Intra-area: 3 Inter-area: 0 External-1: 0 External-2: 0      (detail: not a row)
# internal        1                                               1192   (not routes)
# Total           1           10          0           1120        4324
_ROUTE = re.compile(
    r"^(?P<source>[A-Za-z][\w .-]*?)\s+(?P<networks>\d+)\s+(?P<subnets>\d+)(?:\s+\d+){3}\s*$"
)


def routes(output: str) -> list[Row]:
    rows: list[Row] = []
    for line in data_lines(output):
        match = _ROUTE.match(line)
        if match is None:
            continue
        rows.append(
            {
                "source": match["source"].strip().lower(),
                "routes": int(match["networks"]) + int(match["subnets"]),
            }
        )
    return rows


# Name                   Blocking Listening Learning Forwarding STP Active
# VLAN0001                     0         0        0          3          3
# 1 vlan                       0         0        0          3          3   (the total)
_STP = re.compile(
    r"^(?P<instance>\S+|\d+\s+\w+)\s+(?P<blocking>\d+)\s+\d+\s+\d+\s+(?P<forwarding>\d+)\s+\d+\s*$"
)


def spanning_tree(output: str) -> list[Row]:
    rows: list[Row] = []
    for line in data_lines(output):
        match = _STP.match(line)
        if match is None:
            continue
        total = " " in match["instance"].strip()
        rows.append(
            {
                "instance": "total" if total else match["instance"],
                "blocking": match["blocking"],
                "forwarding": match["forwarding"],
            }
        )
    return rows


PARSERS = {
    "interfaces": interfaces,
    "port_channels": port_channels,
    "port_channel_members": port_channel_members,
    "ospf_neighbors": ospf_neighbors,
    "vlans": vlans,
    "routes": routes,
    "spanning_tree": spanning_tree,
}
