"""State table parsers for AOS-S (ArubaOS-Switch).

See docs/research/state-tables.md for the formats.

Most layouts here come from HPE documentation whose column spacing was rebuilt by hand, so
every parser works on tokens and anchors and never on column offsets. Each parser says above
it whether its format is from a real capture or from documentation only (UNVERIFIED on a
device).

Left out: eigrp_neighbors and hsrp (not on Aruba), lldp_neighbors and cdp_neighbors (the
research has no sample of either command's output on AOS-S).
"""

from __future__ import annotations

import re
from collections.abc import Callable

from tsunami.core.state.parsers.helpers import data_lines
from tsunami.core.state.tables import Row

COMMANDS: dict[str, str] = {
    "interfaces": "show interfaces brief",
    "port_channels": "show lacp",
    "port_channel_members": "show lacp",
    "ospf_neighbors": "show ip ospf neighbor",
    "bgp_neighbors": "show ip bgp summary",
    "stack": "show vsf",
    "vlans": "show vlans",
    "routes": "show ip route summary",
    "vrrp": "show vrrp summary",
    "spanning_tree": "show spanning-tree",
}

_IPV4 = r"\d+\.\d+\.\d+\.\d+"
# 1, A1, 1/1, A8*, and in status tables 1/28-Trk1
_PORT = re.compile(r"^[A-Za-z]?\d+(?:/\d+)*\*?(?:-(?:Trk|Dyn)\d+)?$", re.I)
_TRUNK_SUFFIX = re.compile(r"-(?:Trk|Dyn)\d+$", re.I)


def _port(token: str) -> str:
    """A port as the config prints it: `A8*` and `1/28-Trk1` are `A8` and `1/28`."""
    return _TRUNK_SUFFIX.sub("", token.rstrip("*"))


# Port  Type       | Alert     Enabled Status Mode       Mode Ctrl Limit
# ----- ---------- + --------- ------- ------ ---------- ---- ---- -----
# 1/28-Trk1    SFP+SR     | No        Yes     Up     10GigFD    NA   off  0
# A8*          SFP+SR     | No        Yes     Up     10GigFD    NA   off  0
# E3                      | No        Yes     Down   .               off  0
# Real capture (ntc-templates `hp_procurve/show_interfaces_brief`, four layouts). Type and
# Mode can be empty and Mode can be `.`, so the row is read from the port and the
# `Yes|No` `Up|Down` pair that Enabled and Status make. The platform prints one link state:
# enabled and up is up/up, enabled and down is down/down, disabled is admin-down/down.
def interfaces(output: str) -> list[Row]:
    rows: list[Row] = []
    for line in data_lines(output):
        tokens = line.replace("|", " ").split()
        if not tokens or not _PORT.match(tokens[0]):
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
        rows.append({"name": _port(tokens[0]), "status": state[0], "protocol": state[1]})
    return rows


#   Port  LACP     Trunk   Port    LACP     LACP
#   Numb  Enabled  Group   Status  Partner  Status
#   A1    Active   Trk1    Up      Yes      Success
#   A3    Active   A3      Down    No       Failure
# Documentation only (HPE field meanings; the layout is written from background
# knowledge): UNVERIFIED on a device. Only LACP ports are listed, so a static trunk
# (`trunk ... trk3 trunk`) is not a row here; a port whose group is the port itself is not in
# a trunk and is skipped. Port Status is Up, Down, Disabled, Blocked or Standby.
def _lacp_rows(output: str) -> list[tuple[str, str, str, str]]:
    """(trunk, port, port status, LACP status) per trunked port, in the order printed."""
    found: list[tuple[str, str, str, str]] = []
    for line in data_lines(output):
        tokens = line.split()
        if len(tokens) < 4 or not _PORT.match(tokens[0]):
            continue
        if tokens[1].lower() not in ("active", "passive"):
            continue
        group = re.match(r"^(trk|dyn)(\d+)$", tokens[2], re.I)
        if group is None:
            continue
        lacp_status = tokens[5].lower() if len(tokens) > 5 else ""
        found.append(
            (group[1].capitalize() + group[2], _port(tokens[0]), tokens[3].lower(), lacp_status)
        )
    return found


def _member_status(port_status: str, lacp_status: str) -> str:
    if port_status == "up":
        return "standalone" if lacp_status == "failure" else "bundled"
    if port_status in ("down", "disabled"):
        return "down"
    if port_status == "blocked":
        return "suspended"
    if port_status == "standby":
        return "hot-standby"
    return "other"


def port_channels(output: str) -> list[Row]:
    groups: dict[str, list[str]] = {}
    for trunk, _port_name, port_status, lacp_status in _lacp_rows(output):
        groups.setdefault(trunk, []).append(_member_status(port_status, lacp_status))
    return [
        {
            "name": trunk,
            "status": "up" if statuses.count("bundled") else "down",
            "protocol": "lacp",
            "members": len(statuses),
            "members_up": statuses.count("bundled"),
        }
        for trunk, statuses in groups.items()
    ]


def port_channel_members(output: str) -> list[Row]:
    return [
        {"port_channel": trunk, "member": port, "status": _member_status(status, lacp)}
        for trunk, port, status, lacp in _lacp_rows(output)
    ]


#                                                          Rxmt          Helper
#   Router ID       Pri IP Address     NbIfState State     QLen  Events  Status
#   10.0.8.34       1   10.3.18.34     DR        FULL      0     6       none
#   10.3.53.38      1   10.3.53.38               2WAY      0     6       none
# Documentation only (HPE, layout reconstructed): UNVERIFIED on a device. NbIfState is
# blank when the neighbor is neither DR nor BDR, so the state is the first state word after
# the address. The table has no interface column: `interface` is set to the neighbor's
# address, which keeps the row key unique.
_OSPF_STATES = {
    "down": "down",
    "attempt": "attempt",
    "init": "init",
    "2way": "2way",
    "2-way": "2way",
    "exstart": "exstart",
    "exchange": "exchange",
    "loading": "loading",
    "full": "full",
}


def ospf_neighbors(output: str) -> list[Row]:
    rows: list[Row] = []
    for line in data_lines(output):
        tokens = line.split()
        if len(tokens) < 4:
            continue
        if not re.match(rf"^{_IPV4}$", tokens[0]) or not tokens[1].isdigit():
            continue
        if not re.match(rf"^{_IPV4}$", tokens[2]):
            continue
        rest = [t.lower() for t in tokens[3:]]
        state = next((_OSPF_STATES[t] for t in rest if t in _OSPF_STATES), "")
        if not state:
            continue
        role = rest[0] if rest[0] in ("dr", "bdr", "drother") else ""
        rows.append(
            {
                "neighbor_id": tokens[0],
                "address": tokens[2],
                "interface": tokens[2],
                "state": state,
                "role": role,
            }
        )
    return rows


#  Peer Information
#   Remote Address  Remote-AS Local-AS State        Admin Status
#   2.2.2.2         65009     65009    Established  Start
# Documentation only (HPE, layout reconstructed): UNVERIFIED on a device. No prefix count
# exists, so `prefixes` is 0. Admin Status `Stop` is read as admin-down (UNVERIFIED).
_BGP_STATES = {"established", "idle", "active", "connect", "opensent", "openconfirm"}


def bgp_neighbors(output: str) -> list[Row]:
    rows: list[Row] = []
    for line in data_lines(output):
        tokens = line.split()
        if len(tokens) < 4:
            continue
        if not re.match(rf"^(?:{_IPV4}|[0-9A-Fa-f:]*:[0-9A-Fa-f:.]*)$", tokens[0]):
            continue
        if not tokens[1].isdigit() or not tokens[2].isdigit():
            continue
        state = tokens[3].lower()
        if state not in _BGP_STATES:
            continue
        admin = tokens[4].lower() if len(tokens) > 4 else ""
        rows.append(
            {
                "neighbor": tokens[0],
                "remote_as": int(tokens[1]),
                "state": "admin-down" if admin == "stop" else state,
                "prefixes": 0,
            }
        )
    return rows


#  Mbr
#  ID  MAC Address       Model                                 Pri Status
#  --- ----------------- ------------------------------------- --- ---------------
#  1   f40343-0796b0     Aruba JL258A 2930F-8G-PoE+-2SFP+ S... 128 Member
#  *2  941882-435580     Aruba JL254A 2930F-48G-4SFP+ Switch   128 Member
#  7   b05ada-9631e0     Aruba JL255A 2930F-24G-PoE+-4SFP+ ... 128 Commander
# Documentation only (HPE, layout reconstructed): UNVERIFIED on a device. The same member
# table is printed by `show vsf` (2930F) and by `show stacking` (2930M, 3810M, backplane
# stacking), so this parser reads both, but each table has one command and it is
# `show vsf`: the 2930F is the AOS-S model in this repo's fixtures. A row starts with the
# member number and ends with its status; the model holds spaces and `...`.
_STACK = re.compile(
    r"^\s*\*?(?P<id>\d+)\s.*?\b(?P<status>Commander|Standby|Member|Missing|Provisioned|"
    r"Not Joined|In Other Fragment)\s*$",
    re.I,
)
_STACK_STATUS = {
    "commander": ("active", "ready"),
    "standby": ("standby", "ready"),
    "member": ("member", "ready"),
    "missing": ("member", "missing"),
    "provisioned": ("member", "provisioned"),
    "not joined": ("member", "not-joined"),
    "in other fragment": ("member", "other-fragment"),
}


def stack(output: str) -> list[Row]:
    rows: list[Row] = []
    for line in data_lines(output):
        match = _STACK.match(line)
        if match is None:
            continue
        role, state = _STACK_STATUS[" ".join(match["status"].lower().split())]
        rows.append({"member": int(match["id"]), "role": role, "state": state})
    return rows


#   VLAN ID Name                             | Status     Voice Jumbo
#   ------- -------------------------------- + ---------- ----- -----
#   1       DEFAULT_VLAN                     | Port-based No    No
#   10      Router Vlan                      | Port-based No    No
# Real capture (ntc-templates `hp_procurve/show_vlans`, three layouts). Names can hold
# spaces, so the name is whatever lies between the ID and the status word; the `|` and the
# Jumbo column are optional. Status `Port-based` is the VLAN's type, not a link state: it
# is reported as `active`, and other kinds (Protocol, Dynamic, Static) as their own word.
_VLAN = re.compile(
    r"^\s*(?P<id>\d+)\s+(?P<name>.+?)\s+(?:\|\s*)?(?P<status>Port-based|Protocol|Dynamic|Static)\b",
    re.I,
)


def vlans(output: str) -> list[Row]:
    rows: list[Row] = []
    for line in data_lines(output):
        match = _VLAN.match(line)
        if match is None:
            continue
        status = match["status"].lower()
        rows.append(
            {
                "id": int(match["id"]),
                "name": match["name"].strip(),
                "status": "active" if status == "port-based" else status,
            }
        )
    return rows


#  IPv4 Route Table Summary
#   Protocol  Active Routes
#   --------- -------------
#   Connected 1
#   Static    1
# Documentation only (HPE, layout reconstructed): UNVERIFIED on a device. The total is the
# sum of the rows.
_ROUTE = re.compile(r"^\s*(?P<source>[A-Za-z][\w-]*)\s+(?P<count>\d+)(?:\s+[\d-]+)*\s*$")


def routes(output: str) -> list[Row]:
    counts: dict[str, int] = {}
    for line in data_lines(output):
        match = _ROUTE.match(line)
        if match is not None:
            source = match["source"].lower()
            counts[source] = counts.get(source, 0) + int(match["count"])
    if not counts:
        return []
    rows: list[Row] = [{"source": s, "routes": n} for s, n in counts.items()]
    rows.append({"source": "total", "routes": sum(counts.values())})
    return rows


#  Vlan VRID Pri Up Time  Owner  Pre   State   Master Addr Virtual Addr
#  ---- ---- --- -------- ------ ----- ------- ----------- ---------------
#  10   11   11  2 mins   Backup True  Master  11.1.1.2    11.1.1.9
# Documentation only (HPE, layout reconstructed): UNVERIFIED on a device. Up Time has a
# space and the Owner column can hold a state-like word, so the state is the last
# `Master|Backup|Init` word before the first address (or the end of the line).
_VRRP_STATES = {"master": "master", "backup": "backup", "init": "init"}


def vrrp(output: str) -> list[Row]:
    rows: list[Row] = []
    for line in data_lines(output):
        tokens = line.split()
        if len(tokens) < 5 or not all(t.isdigit() for t in tokens[:3]):
            continue
        state = ""
        for token in tokens[3:]:
            if re.match(rf"^{_IPV4}$", token):
                break
            if token.lower() in _VRRP_STATES:
                state = _VRRP_STATES[token.lower()]
        if not state:
            continue
        rows.append(
            {
                "interface": f"vlan{tokens[0]}",
                "group": int(tokens[1]),
                "state": state,
                "priority": int(tokens[2]),
            }
        )
    return rows


#  Multiple Spanning Tree (MST) Information
#  Port  Type      | Cost      Priority Role       State      Designated Bridge ...
#  ----- --------- + --------- -------- ---------- ---------- -----------------
#  1     100/1000T | 20000     128      Designated Forwarding 0024a8-cd3d00  2  Yes No
#  2     100/1000T | Auto      128      Disabled   Disabled
# Documentation only (the vendor example is an image; layout written from background
# knowledge): UNVERIFIED on a device. This is the regional (CIST) port table, so there is
# one instance, `MST0` when the output says Multiple Spanning Tree and `CST` otherwise,
# plus a `total` row that repeats it. A port is a first token that looks like a port
# followed, somewhere on the line, by a role word and a state word; a short row for a
# disconnected port still has both. Output with no port rows (STP disabled) is empty.
_STP_ROLES = {"root", "designated", "alternate", "backup", "disabled", "master"}
_STP_STATE_WORDS = {"forwarding", "blocking", "learning", "listening", "disabled", "discarding"}


def spanning_tree(output: str) -> list[Row]:
    blocking = forwarding = 0
    ports = 0
    for line in data_lines(output):
        tokens = line.replace("|", " ").split()
        if len(tokens) < 3 or not (
            _PORT.match(tokens[0]) or re.match(r"^trk\d+$", tokens[0], re.I)
        ):
            continue
        for i in range(1, len(tokens) - 1):
            role, state = tokens[i].lower(), tokens[i + 1].lower()
            if role in _STP_ROLES and state in _STP_STATE_WORDS:
                break
        else:
            continue
        ports += 1
        if state == "forwarding":
            forwarding += 1
        elif state in ("blocking", "discarding"):
            blocking += 1
    if not ports:
        return []
    instance = "MST0" if "multiple spanning tree" in output.lower() else "CST"
    return [
        {"instance": instance, "blocking": blocking, "forwarding": forwarding},
        {"instance": "total", "blocking": blocking, "forwarding": forwarding},
    ]


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
