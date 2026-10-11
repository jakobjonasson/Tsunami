"""Jinja filters: ip, netmask, prefixlen, network, wildcard, vlan_range.

See design.md, Snippets and rendering: Filters. All IP filters use `ipaddress` and raise
ValueError on invalid input; a bare host address with no prefix is invalid, because a
template that guesses a mask pushes the wrong config.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Callable, Iterable
from typing import Any


def _interface(value: object) -> ipaddress.IPv4Interface:
    if not isinstance(value, str):
        raise ValueError(f"expected an address like 10.1.1.5/24, got {type(value).__name__}")
    if "/" not in value:
        raise ValueError(
            f"missing prefix length in {value!r}; expected an address like 10.1.1.5/24"
        )
    try:
        return ipaddress.IPv4Interface(value)
    except ValueError as exc:
        raise ValueError(f"invalid IPv4 interface {value!r}: {exc}") from None


def ip(value: str) -> str:
    return str(_interface(value).ip)


def netmask(value: str) -> str:
    return str(_interface(value).netmask)


def prefixlen(value: str) -> int:
    return _interface(value).network.prefixlen


def network(value: str) -> str:
    return str(_interface(value).network.network_address)


def wildcard(value: str) -> str:
    return str(_interface(value).hostmask)


def vlan_range(value: Iterable[int]) -> str:
    try:
        items = list(value)
    except TypeError:
        raise ValueError("vlan_range expects a list of VLAN ids") from None
    if not items:
        raise ValueError("vlan_range got an empty list")
    for item in items:
        if isinstance(item, bool) or not isinstance(item, int):
            raise ValueError(f"VLAN id must be an integer, got {item!r}")
        if not 1 <= item <= 4094:
            raise ValueError(f"VLAN id {item} is outside 1-4094")
    ids = sorted(set(items))
    parts: list[str] = []
    start = prev = ids[0]
    for vid in [*ids[1:], None]:
        if vid is not None and vid == prev + 1:
            prev = vid
            continue
        parts.append(str(start) if start == prev else f"{start}-{prev}")
        if vid is not None:
            start = prev = vid
    return ",".join(parts)


FILTERS: dict[str, Callable[..., Any]] = {
    "ip": ip,
    "netmask": netmask,
    "prefixlen": prefixlen,
    "network": network,
    "wildcard": wildcard,
    "vlan_range": vlan_range,
}
