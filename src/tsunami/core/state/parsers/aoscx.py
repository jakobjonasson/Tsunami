"""State table parsers for AOS-CX. See docs/research/state-tables.md for the formats.

Most layouts here come from HPE documentation whose column spacing was rebuilt by hand, so
every parser works on tokens and anchors (a row's first token, known state words, the last
tokens) and never on column offsets. Each parser says above it whether its format is from a
real capture or from documentation only (UNVERIFIED on a device).

Left out: eigrp_neighbors and hsrp (not on Aruba), lldp_neighbors and cdp_neighbors (the
research has no sample of either command's output on AOS-CX).
"""

from __future__ import annotations

import re
from collections.abc import Callable

from tsunami.core.state.parsers.helpers import data_lines
from tsunami.core.state.tables import Row

COMMANDS: dict[str, str] = {
    "interfaces": "show interface brief",
    "port_channels": "show lacp interfaces",
    "port_channel_members": "show lacp interfaces",
    "ospf_neighbors": "show ip ospf neighbors all-vrfs",
    "bgp_neighbors": "show bgp all-vrfs all summary",
    "stack": "show vsf",
    "vlans": "show vlan",
    "routes": "show ip route summary",
    "vrrp": "show vrrp brief",
    "spanning_tree": "show spanning-tree",
}

_IPV4 = r"\d+\.\d+\.\d+\.\d+"
# 1/1/1, 1/1/52:1 (split port), lag1, lag1(mc), vlan10, loopback0, mgmt
_NAME = re.compile(
    r"^(?:\d+/\d+/\d+(?::\d+)?|lag\d+(?:\(mc\))?|vlan\d+|loopback\d+|tunnel\d+|mgmt)$", re.I
)
_PORT = re.compile(r"^\d+/\d+/\d+(?::\d+)?$")
_LAG = re.compile(r"^(?P<name>lag\d+)(?:\(mc\))?$", re.I)


def _name(token: str) -> str:
    """An interface name as the `interface` command takes it, without the `(mc)` marker."""
    return re.sub(r"\(mc\)$", "", token.strip(), flags=re.I).lower()


# Port       Native  Mode     Type     Enabled Status  Reason                  Speed  Description
#            VLAN                                                            (Mb/s)
# 1/1/1      1       access   1GbT     yes     up                              1000   --
# 1/1/3      --      routed   --       no      down    Administratively down   --     --
# Documentation only (header tokens are HPE's, rows written from background knowledge):
# UNVERIFIED on a device. The row is read from the first token and the `yes|no` `up|down`
# pair that Enabled and Status make; Reason holds spaces, so nothing after Status is used.
def interfaces(output: str) -> list[Row]:
    rows: list[Row] = []
    for line in data_lines(output):
        tokens = line.split()
        if not _NAME.match(tokens[0]):
            continue
        for i in range(1, len(tokens) - 1):
            enabled, status = tokens[i].lower(), tokens[i + 1].lower()
            if enabled in ("yes", "no") and status in ("up", "down"):
                break
        else:
            continue
        if enabled == "no":
            state = ("admin-down", "down")
        else:
            state = (status, status)
        rows.append({"name": _name(tokens[0]), "status": state[0], "protocol": state[1]})
    return rows


# Actor details of all interfaces:
# Intf    Aggr     Port  Port  State   System-id         System Aggr Forwarding
# 1/1/1   lag10    17    1     ALFOE   70:72:cf:37:a3:5c 20     10   lacp-block
# 1/1/2   lag128   69    1     ALFNCD  70:72:cf:37:a3:5c 20     128  up
# 1/1/4   lag128                                                     down
# 1/4/14  lag1(mc) 206   1     ALFNCD  f8:60:f0:06:49:00 65534  1    up
# Partner details of all interfaces:   (ignored)
# Documentation only (HPE, layout reconstructed): UNVERIFIED on a device. A row is the
# member port, its LAG, optional LACP columns and, last, the forwarding state; static and
# down members leave the middle empty. Only the Actor section is read.
def _lacp_members(output: str) -> list[tuple[str, str, str, str]]:
    """(lag, port, flags, forwarding state) per member, in the order printed."""
    members: list[tuple[str, str, str, str]] = []
    in_actor = True
    for line in data_lines(output):
        lowered = line.strip().lower()
        if lowered.startswith("actor details"):
            in_actor = True
            continue
        if lowered.startswith("partner details"):
            in_actor = False
            continue
        if not in_actor:
            continue
        tokens = line.split()
        if len(tokens) < 3 or not _PORT.match(tokens[0]):
            continue
        lag = _LAG.match(tokens[1])
        forwarding = tokens[-1].lower()
        if lag is None or not re.match(r"^[a-z][a-z-]*$", forwarding):
            continue
        flags = tokens[4] if len(tokens) >= 5 and re.match(r"^[A-Za-z]{2,}$", tokens[4]) else ""
        members.append((lag["name"].lower(), tokens[0], flags, forwarding))
    return members


def _member_status(flags: str, forwarding: str) -> str:
    if forwarding == "up":
        return "standalone" if "I" in flags and "F" not in flags else "bundled"
    if forwarding == "down":
        return "down"
    if forwarding == "lacp-block":
        return "suspended"
    return "other"


def port_channels(output: str) -> list[Row]:
    groups: dict[str, list[tuple[str, str, str]]] = {}
    for lag, port, flags, forwarding in _lacp_members(output):
        groups.setdefault(lag, []).append((port, flags, forwarding))
    rows: list[Row] = []
    for lag, members in groups.items():
        up = sum(1 for _, _, forwarding in members if forwarding == "up")
        if any(flags for _, flags, _ in members):
            protocol = "lacp"
        else:
            # a static member that is up has no LACP columns; a down one cannot be told apart
            protocol = "static" if up else ""
        rows.append(
            {
                "name": lag,
                "status": "up" if up else "down",
                "protocol": protocol,
                "members": len(members),
                "members_up": up,
            }
        )
    return rows


def port_channel_members(output: str) -> list[Row]:
    return [
        {"port_channel": lag, "member": port, "status": _member_status(flags, forwarding)}
        for lag, port, flags, forwarding in _lacp_members(output)
    ]


# OSPF Process ID 1 VRF default
# ==============================
# Neighbor ID      Priority  State             Nbr Address       Interface
# 2.2.2.2          1         FULL/DR           10.1.1.2          1/1/1
# Documentation only (HPE, layout reconstructed): UNVERIFIED on a device. The block headers
# and the `Total Number of Neighbors` line are skipped; a neighbor in two VRFs is two rows
# with the same key, which the table cannot tell apart.
_OSPF = re.compile(
    rf"^\s*(?P<id>{_IPV4})\s+\d+\s+(?P<state>[A-Za-z0-9-]+)(?:\s*/\s*(?P<role>[A-Za-z-]+))?\s+"
    rf"(?P<address>{_IPV4})\s+(?P<interface>\S+)\s*$"
)


def _ospf_state(word: str) -> str:
    state = word.lower().replace("-", "")
    return "2way" if state == "twoway" else state


def ospf_neighbors(output: str) -> list[Row]:
    rows: list[Row] = []
    for line in data_lines(output):
        match = _OSPF.match(line)
        if match is None:
            continue
        role = (match["role"] or "").lower()
        rows.append(
            {
                "neighbor_id": match["id"],
                "address": match["address"],
                "interface": _name(match["interface"]),
                "state": _ospf_state(match["state"]),
                "role": "" if role == "-" else role,
            }
        )
    return rows


# VRF : default
# BGP Summary
# Address-family : IPv4 Unicast
#  Neighbor        Remote-AS MsgRcvd MsgSent   Up/Down Time State        AdminStatus
#  10.252.13.2     4290020685  0       0       00h:00m:00s  Idle          Up
#  *10.1.1.5       11        26      24        00h:20m:26s  Established  Up
# Real capture (ntc-templates `show bgp all-vrfs all summary`, spacing real); the
# `show bgp all summary` form that HPE documents prints the same blocks. No prefix count
# exists in this summary, so `prefixes` stays 0. The same peer in several address families
# (IPv4 and EVPN) is one row, the first one; one peer address in two VRFs is also one row.
_BGP_STATES = {"established", "idle", "active", "connect", "opensent", "openconfirm"}


def _asn(word: str) -> int:
    if "." in word:  # asdot
        high, low = word.split(".", 1)
        return int(high) * 65536 + int(low)
    return int(word)


def bgp_neighbors(output: str) -> list[Row]:
    rows: list[Row] = []
    seen: set[str] = set()
    for line in data_lines(output):
        tokens = line.split()
        if len(tokens) < 4:
            continue
        address = tokens[0].lstrip("*")
        if not re.match(rf"^(?:{_IPV4}|[0-9A-Fa-f:]*:[0-9A-Fa-f:.]*)$", address):
            continue
        if not re.match(r"^\d+(?:\.\d+)?$", tokens[1]):
            continue
        state, admin = tokens[-2].lower(), tokens[-1].lower()
        if state not in _BGP_STATES or admin not in ("up", "down"):
            continue
        if address.lower() in seen:
            continue
        seen.add(address.lower())
        rows.append(
            {
                "neighbor": address,
                "remote_as": _asn(tokens[1]),
                "state": "admin-down" if admin == "down" else state,
                "prefixes": 0,
            }
        )
    return rows


# Mbr  Mac Address         type            Status
# ID
# ---  ------------------- --------------  ---------------
# 1    38:21:c7:5c:62:40   JL668A          Conductor
# 4    18:7a:3b:1b:66:40   R8S89A          Member Booting
# 5                        JL662A          Not Present
# Documentation only (HPE, layout reconstructed; `Master` is the pre-10.07 word for
# Conductor): UNVERIFIED on a device. A row starts with the member number and ends with its
# status, which can be two words.
_VSF = re.compile(
    r"^\s*\*?(?P<id>\d+)\s.*?\b(?P<status>Conductor|Master|Standby|Member Booting|Member|"
    r"Not Present|In Other Fragment)\s*$",
    re.I,
)
_VSF_STATUS = {
    "conductor": ("active", "ready"),
    "master": ("active", "ready"),
    "standby": ("standby", "ready"),
    "member": ("member", "ready"),
    "member booting": ("member", "booting"),
    "not present": ("member", "not-present"),
    "in other fragment": ("member", "other-fragment"),
}


def stack(output: str) -> list[Row]:
    rows: list[Row] = []
    for line in data_lines(output):
        match = _VSF.match(line)
        if match is None:
            continue
        status = " ".join(match["status"].lower().split())
        role, state = _VSF_STATUS[status]
        rows.append({"member": int(match["id"]), "role": role, "state": state})
    return rows


# VLAN  Name                              Status  Reason                  Type        Interfaces
# 1     DEFAULT_VLAN_1                    up      ok                      default     1/1/34-1/1/52
# 3072  mgmt                              up      ok                      static      1/1/2-1/1/4,
#                                                                                     1/1/17-1/1/19
# Real capture (ntc-templates `aruba_aoscx/show_vlan`). The Interfaces list wraps onto
# indented lines, which start with no VLAN number and are skipped.
_VLAN = re.compile(r"^(?P<id>\d+)\s+(?P<name>\S+)\s+(?P<status>up|down)\b", re.I)


def vlans(output: str) -> list[Row]:
    rows: list[Row] = []
    for line in data_lines(output):
        match = _VLAN.match(line)
        if match is not None:
            rows.append(
                {
                    "id": int(match["id"]),
                    "name": match["name"],
                    "status": match["status"].lower(),
                }
            )
    return rows


# VRF name : red
# Protocol        Active Routes   Protected Routes
# connected       3               -
# bgp             12              5
# Documentation only (HPE, layout reconstructed): UNVERIFIED on a device. Only the default
# VRF is counted (a block under another `VRF name :` header is skipped, and so is output
# with no header at all counted as default), because the table has one row per source. The
# total is the sum of the rows, `local` routes included.
_ROUTE_VRF = re.compile(r"^\s*VRF(?:\s+name)?\s*:\s*(?P<vrf>\S+)", re.I)
_ROUTE = re.compile(r"^\s*(?P<source>[A-Za-z][\w-]*)\s+(?P<count>\d+)(?:\s+[\d-]+)*\s*$")


def routes(output: str) -> list[Row]:
    counts: dict[str, int] = {}
    vrf = "default"
    for line in data_lines(output):
        header = _ROUTE_VRF.match(line)
        if header is not None:
            vrf = header["vrf"].lower()
            continue
        match = _ROUTE.match(line)
        if match is None or vrf != "default":
            continue
        source = match["source"].lower()
        counts[source] = counts.get(source, 0) + int(match["count"])
    if not counts:
        return []
    rows: list[Row] = [{"source": s, "routes": n} for s, n in counts.items()]
    rows.append({"source": "total", "routes": sum(counts.values())})
    return rows


# VRRP is enabled
# Interface  Grp  A-F   Pri  Time  Owner  Pre  State   Active addr/Group addr
# 1/1/1      1    IPv4  100  0     N      Y    ACTIVE  10.0.0.2(local) 10.0.0.1
# 1/1/2      1    IPv6  100  0     N      Y    INIT    AF-UNDEFINED no address
# Documentation only (HPE, layout reconstructed): UNVERIFIED on a device. ACTIVE (and the
# older MASTER) is master, STANDBY (and BACKUP) is backup. A group that runs IPv4 and IPv6
# is one row, the IPv4 one, since the table keys on interface and group.
_VRRP_STATES = {
    "active": "master",
    "master": "master",
    "standby": "backup",
    "backup": "backup",
    "init": "init",
}


def vrrp(output: str) -> list[Row]:
    found: dict[tuple[str, int], tuple[bool, Row]] = {}
    for line in data_lines(output):
        tokens = line.split()
        if len(tokens) < 6 or not _NAME.match(tokens[0]):
            continue
        if not tokens[1].isdigit() or tokens[2].lower() not in ("ipv4", "ipv6"):
            continue
        if not tokens[3].isdigit():
            continue
        state = next((_VRRP_STATES[t.lower()] for t in tokens[4:] if t.lower() in _VRRP_STATES), "")
        if not state:
            continue
        key = (_name(tokens[0]), int(tokens[1]))
        is_v4 = tokens[2].lower() == "ipv4"
        row: Row = {
            "interface": key[0],
            "group": key[1],
            "state": state,
            "priority": int(tokens[3]),
        }
        if key not in found or (is_v4 and not found[key][0]):
            found[key] = (is_v4, row)
    return [row for _, row in found.values()]


# MST0
#   Root ID    Priority   : 0
# PORT     ROLE        STATE      COST       PRIORITY  TYPE      BPDU-Tx ...
# 1/1/1    Designated  Forwarding 20000      128       P2P Edge  100  60  20  10
# 1/1/5    Alternate   Loop-Inc   20000      128       Shr Edge  100  60  20  10
# Documentation only (HPE, layout reconstructed): UNVERIFIED on a device. One row per
# instance header (`MST0`, `VLAN10`) that has a port table, counting ports by the role and
# state words, plus a `total` row. Loop-Inc and Root-Inc ports count as blocking; learning,
# listening and down ports as neither. TYPE is never read, so `P2P Edge` costs nothing.
_STP_INSTANCE = re.compile(r"^(?P<name>MST\d+|VLAN\d+|CIST|CST)\s*$", re.I)
_STP_ROLES = {"designated", "root", "alternate", "backup", "disabled", "master"}


def spanning_tree(output: str) -> list[Row]:
    counts: dict[str, list[int]] = {}
    current: str | None = None
    for line in data_lines(output):
        header = _STP_INSTANCE.match(line)
        if header is not None:
            current = header["name"].upper()
            counts.setdefault(current, [0, 0])
            continue
        tokens = line.split()
        if current is None or len(tokens) < 3 or not _NAME.match(tokens[0]):
            continue
        if tokens[1].lower() not in _STP_ROLES:
            continue
        state = tokens[2].lower()
        if state == "forwarding":
            counts[current][1] += 1
        elif state == "blocking" or state.endswith("-inc"):
            counts[current][0] += 1
    if not counts:
        return []
    rows: list[Row] = [
        {"instance": name, "blocking": blocking, "forwarding": forwarding}
        for name, (blocking, forwarding) in counts.items()
    ]
    rows.append(
        {
            "instance": "total",
            "blocking": sum(c[0] for c in counts.values()),
            "forwarding": sum(c[1] for c in counts.values()),
        }
    )
    return rows


PARSERS: dict[str, Callable[[str], list[Row]]] = {
    "interfaces": interfaces,
    "port_channels": port_channels,
    "port_channel_members": port_channel_members,
    "ospf_neighbors": ospf_neighbors,
    "bgp_neighbors": bgp_neighbors,
    "stack": stack,
    "vlans": vlans,
    "routes": routes,
    "vrrp": vrrp,
    "spanning_tree": spanning_tree,
}
