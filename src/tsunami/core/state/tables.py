"""State tables: what a device's operational state looks like as rows.

A table is a fixed show command per platform family and a parser that turns its output
into rows with the same columns and the same value words on every family, so a check
written once (`state: ospf_neighbors`, `expect: {state: full}`) means the same on Cisco
and Aruba. The table says which columns identify a row (`key`) and which ones a
before/after comparison looks at (`compared`): counters and timers are left out, since
they differ between any two reads.

The definitions here are family-neutral. Each family's module under state/parsers says
which tables it can read, with which command, and holds the parsers.

See design.md, State checks.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from tsunami.core.issues import TsunamiError

Row = dict[str, Any]

TEXT = "text"
INT = "int"


class StateError(TsunamiError):
    """A state table could not be read. The text may quote the device: redact it."""


@dataclass(frozen=True)
class Column:
    name: str
    kind: str = TEXT  # TEXT or INT
    help: str = ""


@dataclass(frozen=True)
class StateTable:
    name: str
    title: str
    key: tuple[str, ...]
    columns: tuple[Column, ...]
    compared: tuple[str, ...] = ()

    def column(self, name: str) -> Column | None:
        return next((c for c in self.columns if c.name == name), None)

    @property
    def column_names(self) -> tuple[str, ...]:
        return tuple(c.name for c in self.columns)

    def key_of(self, row: Row) -> tuple[Any, ...]:
        return tuple(_fold(row.get(name)) for name in self.key)

    def label(self, row: Row) -> str:
        """A row named by its key, for messages: `neighbor_id=10.0.0.1 interface=Vlan10`."""
        return " ".join(f"{name}={row.get(name)}" for name in self.key)


def _fold(value: Any) -> Any:
    return value.lower() if isinstance(value, str) else value


def _table(
    name: str, title: str, key: tuple[str, ...], compared: tuple[str, ...], *columns: Column
) -> StateTable:
    return StateTable(name, title, key, tuple(columns), compared)


_UP_DOWN = "up or down"

TABLES: dict[str, StateTable] = {
    t.name: t
    for t in (
        _table(
            "interfaces",
            "Interfaces",
            ("name",),
            ("status", "protocol"),
            Column("name", help="as show running-config prints it, e.g. GigabitEthernet1/0/1"),
            Column("status", help="up, down or admin-down"),
            Column("protocol", help=f"line protocol: {_UP_DOWN}"),
        ),
        _table(
            "port_channels",
            "Link aggregation groups",
            ("name",),
            ("status", "members_up"),
            Column("name", help="Port-channel1 (IOS), lag1 (AOS-CX), Trk1 (AOS-S)"),
            Column("status", help=_UP_DOWN),
            Column("protocol", help="lacp, pagp or static"),
            Column("members", INT, "member ports configured"),
            Column("members_up", INT, "member ports bundled and forwarding"),
        ),
        _table(
            "port_channel_members",
            "Link aggregation member ports",
            ("port_channel", "member"),
            ("status",),
            Column("port_channel", help="the group's name, as in port_channels"),
            Column("member", help="the member port's name"),
            Column("status", help="bundled, down, suspended, standalone, hot-standby or other"),
        ),
        _table(
            "ospf_neighbors",
            "OSPF neighbors",
            ("neighbor_id", "interface"),
            ("state",),
            Column("neighbor_id", help="the neighbor's router ID"),
            Column("address", help="the neighbor's interface address"),
            Column("interface", help="the local interface"),
            Column("state", help="full, 2way, exstart, exchange, loading, init, attempt or down"),
            Column("role", help="dr, bdr, drother, or empty on a point-to-point link"),
        ),
        _table(
            "bgp_neighbors",
            "BGP neighbors",
            ("neighbor",),
            ("state",),
            Column("neighbor", help="the peer's address"),
            Column("remote_as", INT),
            Column(
                "state",
                help="established, idle, active, connect, opensent, openconfirm or admin-down",
            ),
            Column("prefixes", INT, "prefixes received; 0 unless established"),
        ),
        _table(
            "eigrp_neighbors",
            "EIGRP neighbors",
            ("address", "interface"),
            (),
            Column("address"),
            Column("interface", help="the local interface"),
            Column("as_number", INT, "the EIGRP autonomous system"),
        ),
        _table(
            "cdp_neighbors",
            "CDP neighbors",
            ("local_interface", "neighbor"),
            ("remote_port",),
            Column("local_interface"),
            Column("neighbor", help="the neighbor's device ID"),
            Column("remote_port"),
        ),
        _table(
            "lldp_neighbors",
            "LLDP neighbors",
            ("local_interface", "neighbor"),
            ("remote_port",),
            Column("local_interface"),
            Column("neighbor", help="the neighbor's system name, or its chassis ID"),
            Column("remote_port"),
        ),
        _table(
            "stack",
            "Stack members",
            ("member",),
            ("state",),
            Column("member", INT, "the member number"),
            Column("role", help="active, standby or member"),
            Column("state", help="ready when the member is up and in the stack"),
        ),
        _table(
            "vlans",
            "VLANs",
            ("id",),
            ("status",),
            Column("id", INT),
            Column("name"),
            Column("status", help="active, suspended or another word the platform prints"),
        ),
        _table(
            "routes",
            "IPv4 routes per source",
            ("source",),
            ("routes",),
            Column("source", help="connected, static, ospf 1, bgp 65000, ..., and total"),
            Column("routes", INT, "routes from this source in the routing table"),
        ),
        _table(
            "hsrp",
            "HSRP groups",
            ("interface", "group"),
            ("state",),
            Column("interface"),
            Column("group", INT),
            Column("state", help="active, standby, speak, listen, init or disabled"),
            Column("priority", INT),
        ),
        _table(
            "vrrp",
            "VRRP groups",
            ("interface", "group"),
            ("state",),
            Column("interface"),
            Column("group", INT),
            Column("state", help="master, backup or init"),
            Column("priority", INT),
        ),
        _table(
            "spanning_tree",
            "Spanning tree ports per VLAN or instance",
            ("instance",),
            ("blocking", "forwarding"),
            Column("instance", help="VLAN0010 (PVST), MST0 or the like, and total"),
            Column("blocking", INT, "ports blocking"),
            Column("forwarding", INT, "ports forwarding"),
        ),
    )
}


def table(name: str) -> StateTable:
    try:
        return TABLES[name]
    except KeyError:
        known = ", ".join(sorted(TABLES))
        raise StateError(f"unknown state table {name!r}; the tables are {known}") from None


def clean_rows(spec: StateTable, rows: list[Row]) -> list[Row]:
    """Parser output made uniform: every column present, ints as ints, text stripped.
    A row without its key is a parser fault, reported rather than compared."""
    out: list[Row] = []
    for raw in rows:
        row: Row = {}
        for column in spec.columns:
            value = raw.get(column.name)
            if column.kind == INT:
                try:
                    row[column.name] = int(value) if value not in (None, "") else 0
                except (TypeError, ValueError):
                    raise StateError(
                        f"{spec.name}: {column.name} is not a number: {value!r}"
                    ) from None
            else:
                row[column.name] = "" if value is None else str(value).strip()
        if any(row[name] == "" for name in spec.key):
            raise StateError(f"{spec.name}: a row has no {'/'.join(spec.key)}")
        out.append(row)
    return out
