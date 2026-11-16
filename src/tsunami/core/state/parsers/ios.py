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
    "ospf_neighbors": "show ip ospf neighbor",
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


PARSERS = {
    "interfaces": interfaces,
    "ospf_neighbors": ospf_neighbors,
}
