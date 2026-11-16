"""State table parsers for Cisco IOS and IOS-XE.

The formats and their variations are in docs/research/state-tables.md; the fixtures under
tests/fixtures/state/ios are real output (IOL 17.18, IOSv 15.9, a C1101 on 16.9).
"""

from __future__ import annotations

import re

from tsunami.core.discovery import parse_cdp_ios
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
    "bgp_neighbors": "show ip bgp summary",
    "eigrp_neighbors": "show ip eigrp neighbors",
    "cdp_neighbors": "show cdp neighbors detail",
    "lldp_neighbors": "show lldp neighbors detail",
    "stack": "show switch",
    "hsrp": "show standby brief",
    "vrrp": "show vrrp brief",
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


# Neighbor        V           AS MsgRcvd MsgSent   TblVer  InQ OutQ Up/Down  State/PfxRcd
# 10.0.0.1        4        65000 2746767 2396274 512185206    0    0 3w0d       558720
# 10.0.0.10       4        65009 26930508  942614 512185203    0    0 1y10w     Idle (Admin)
# A neighbor name longer than the column (IPv6) may sit alone on the line above the row.
# `For address family:` headers and the lines under them are not rows.
_BGP = re.compile(
    r"^(?:\*?(?P<neighbor>\S+)\s+|\s+)(?P<version>\d)\s+(?P<asn>\d+(?:\.\d+)?)"
    r"(?:\s+\d+){5}\s+(?P<updown>\S+)\s+(?P<state>\S.*?)\s*$"
)
_BGP_STATES = ("idle", "active", "connect", "opensent", "openconfirm")


def _asn(text: str) -> int:
    """An AS number in asplain or asdot (65000.1000) form, as the asplain number."""
    high, _, low = text.partition(".")
    return int(high) * 65536 + int(low) if low else int(high)


def bgp_neighbors(output: str) -> list[Row]:
    rows: list[Row] = []
    pending = ""
    for line in data_lines(output):
        match = _BGP.match(line)
        neighbor = match["neighbor"] if match and match["neighbor"] else pending
        if match is None or not neighbor:
            single = not line[0].isspace() and len(line.split()) == 1
            pending = line.strip().lstrip("*") if single and re.search(r"[.:]", line) else ""
            continue
        pending = ""
        state = match["state"]
        if state.isdigit():
            word, prefixes = "established", int(state)
        else:
            lowered = state.lower()
            first = lowered.split()[0]
            if first == "idle" and "(admin)" in lowered:
                word = "admin-down"
            else:
                word = first if first in _BGP_STATES else lowered
            prefixes = 0
        rows.append(
            {
                "neighbor": neighbor,
                "remote_as": _asn(match["asn"]),
                "state": word,
                "prefixes": prefixes,
            }
        )
    return rows


# EIGRP-IPv4 Neighbors for AS(10)         (also: IP-EIGRP neighbors for process 1,
# EIGRP-IPv4:(535) neighbors for process 535, and in named mode
# EIGRP-IPv4 VR(name) Address-Family Neighbors for AS(10))
# H   Address                 Interface       Hold Uptime   SRTT   RTO  Q  Seq
#                                             (sec)         (ms)       Cnt Num
# 0   192.168.1.2            Gi0/0             10 00:12:45   19   100  0  11
_EIGRP_HEADER = re.compile(r"neighbors for (?:AS\((?P<asn>\d+)\)|process (?P<process>\d+))", re.I)
_EIGRP = re.compile(
    r"^\s*\d+\s+(?P<address>\d+\.\d+\.\d+\.\d+)\s+(?P<interface>\S+)\s+\d+\s+\S+"
    r"\s+\d+\s+\d+\s+\d+\s+\d+\s*$"
)


def eigrp_neighbors(output: str) -> list[Row]:
    rows: list[Row] = []
    as_number = 0
    for line in data_lines(output):
        header = _EIGRP_HEADER.search(line)
        if header is not None:
            as_number = int(header["asn"] or header["process"])
            continue
        match = _EIGRP.match(line)
        if match is not None:
            rows.append(
                {
                    "address": match["address"],
                    "interface": long_interface(match["interface"]),
                    "as_number": as_number,
                }
            )
    return rows


def cdp_neighbors(output: str) -> list[Row]:
    return [
        {
            "local_interface": long_interface(n.local_port),
            "neighbor": n.name,
            "remote_port": long_interface(n.remote_port),
        }
        for n in parse_cdp_ios(output)
        if n.local_port and n.name
    ]


# ------------------------------------------------
# Local Intf: Et0/0
# Chassis id: aabb.cc00.b900
# Port id: Et0/1
# System Name: dsc-sw1.lab.local
_LLDP_BLOCKS = re.compile(r"^\s*-{5,}\s*$", re.M)


def _lldp_field(label: str, block: str) -> str:
    match = re.search(rf"^{label}:[ \t]*(\S.*?)\s*$", block, re.M | re.I)
    return match[1] if match else ""


def lldp_neighbors(output: str) -> list[Row]:
    rows: list[Row] = []
    for block in _LLDP_BLOCKS.split(output):
        local = _lldp_field("Local Intf", block)
        name = _lldp_field("System Name", block) or _lldp_field("Chassis id", block)
        if local and name:
            rows.append(
                {
                    "local_interface": long_interface(local.split()[0]),
                    "neighbor": name,
                    "remote_port": long_interface(_lldp_field("Port id", block)),
                }
            )
    return rows


# Switch#   Role    Mac Address     Priority Version  State
# *1       Active   cc98.9114.3580     15     V01     Ready
#  2       Member   0000.0000.0000     0              Provisioned   (H/W version empty)
#  3       Member   a0f8.4911.c080     3      V02     HA Sync in Progress
_STACK = re.compile(
    r"^\s*\*?\s*(?P<member>\d+)\s+(?P<role>[A-Za-z]+)\s+"
    r"[0-9a-f]{4}\.[0-9a-f]{4}\.[0-9a-f]{4}\s+\d+\s+(?:V?\d\S*\s+)?"
    r"(?P<state>[A-Za-z]\S*(?: +\S+)*)$",
    re.I,
)
_STACK_ROLES = {"master": "active"}


def stack(output: str) -> list[Row]:
    rows: list[Row] = []
    for line in data_lines(output):
        match = _STACK.match(line)
        if match is None:
            continue
        role = match["role"].lower()
        rows.append(
            {
                "member": match["member"],
                "role": _STACK_ROLES.get(role, role),
                "state": " ".join(match["state"].lower().split()),
            }
        )
    return rows


#                      P indicates configured to preempt.
# Interface   Grp  Pri P State   Active          Standby         Virtual IP
# Vl1         1    120 P Active  local           10.1.1.3      10.1.1.1
# Vl1309      1    110   Init    unknown         unknown         192.0.2.100   (P blank)
# Vl4010                                          (a long name puts the rest on the next line)
#      1    120 P Active  local           10.1.254.3    10.1.254.1
_HSRP = re.compile(
    r"^(?:(?P<interface>\S+)\s+|\s+)(?P<group>\d+)\s+(?P<priority>\d+)\s+(?:P\s+)?"
    r"(?P<state>Active|Standby|Speak|Listen|Learn|Init|Disabled)\b",
    re.I,
)


def hsrp(output: str) -> list[Row]:
    rows: list[Row] = []
    pending = ""
    for line in data_lines(output):
        match = _HSRP.match(line)
        interface = match["interface"] if match and match["interface"] else pending
        if match is None or not interface:
            single = not line[0].isspace() and len(line.split()) == 1
            pending = line.strip() if single else ""
            continue
        pending = ""
        rows.append(
            {
                "interface": long_interface(interface),
                "group": match["group"],
                "state": match["state"].lower(),
                "priority": match["priority"],
            }
        )
    return rows


# Interface          Grp Pri Time  Own Pre State   Master addr     Group addr
# Fa0/1              1   110 3570       Y  Master  192.168.88.2    192.168.88.1
# VRRPv3 adds an address family after Grp and prints the states in upper case:
#   Gi0/0                1 IPv4 110     0  N   Y  MASTER  10.0.1.2(local) 10.0.1.1
_VRRP = re.compile(
    r"^\s*(?P<interface>\S+)\s+(?P<group>\d+)\s+(?:IPv[46]\s+)?(?P<priority>\d+)\s+\d+\s+"
    r"(?:[YN]\s+){0,2}(?P<state>Master|Backup|Init)\b",
    re.I,
)


def vrrp(output: str) -> list[Row]:
    rows: list[Row] = []
    for line in data_lines(output):
        match = _VRRP.match(line)
        if match is not None:
            rows.append(
                {
                    "interface": long_interface(match["interface"]),
                    "group": match["group"],
                    "state": match["state"].lower(),
                    "priority": match["priority"],
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
    "bgp_neighbors": bgp_neighbors,
    "eigrp_neighbors": eigrp_neighbors,
    "cdp_neighbors": cdp_neighbors,
    "lldp_neighbors": lldp_neighbors,
    "stack": stack,
    "hsrp": hsrp,
    "vrrp": vrrp,
}
