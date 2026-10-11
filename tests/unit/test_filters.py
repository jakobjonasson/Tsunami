"""Jinja filters (design.md, Snippets and rendering: Filters)."""

from __future__ import annotations

from typing import Any

import pytest

from tsunami.core.render.filters import (
    FILTERS,
    ip,
    netmask,
    network,
    prefixlen,
    vlan_range,
    wildcard,
)


def test_registry_has_exactly_the_six_filters() -> None:
    assert set(FILTERS) == {"ip", "netmask", "prefixlen", "network", "wildcard", "vlan_range"}


@pytest.mark.parametrize(
    ("func", "expected"),
    [
        (ip, "10.1.1.5"),
        (netmask, "255.255.255.0"),
        (prefixlen, 24),
        (network, "10.1.1.0"),
        (wildcard, "0.0.0.255"),
    ],
)
def test_spec_table(func: Any, expected: object) -> None:
    assert func("10.1.1.5/24") == expected


@pytest.mark.parametrize(
    ("value", "mask", "plen", "net", "wild"),
    [
        ("192.0.2.1/31", "255.255.255.254", 31, "192.0.2.0", "0.0.0.1"),
        ("192.0.2.1/32", "255.255.255.255", 32, "192.0.2.1", "0.0.0.0"),
        ("192.0.2.1/0", "0.0.0.0", 0, "0.0.0.0", "255.255.255.255"),
    ],
)
def test_edge_prefixes(value: str, mask: str, plen: int, net: str, wild: str) -> None:
    assert ip(value) == value.split("/")[0]
    assert netmask(value) == mask
    assert prefixlen(value) == plen
    assert network(value) == net
    assert wildcard(value) == wild


@pytest.mark.parametrize("func", [ip, netmask, prefixlen, network, wildcard])
@pytest.mark.parametrize(
    "bad",
    ["10.1.1.5", "10.1.1.300/24", "not-an-ip/24", "10.1.1.5/33", "", 5, None, ["10.1.1.5/24"]],
)
def test_ip_filters_reject_invalid(func: Any, bad: object) -> None:
    with pytest.raises(ValueError):
        func(bad)


def test_missing_prefix_message_is_helpful() -> None:
    with pytest.raises(ValueError, match="missing prefix"):
        ip("10.1.1.5")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ([10, 20, 21, 22], "10,20-22"),
        ([1, 2, 3], "1-3"),
        ([5], "5"),
        ([22, 10, 21, 20], "10,20-22"),
        ([10, 10, 11, 11], "10-11"),
        ([1, 4094], "1,4094"),
        ([1, 3, 5], "1,3,5"),
        ((x for x in [3, 2, 1]), "1-3"),
    ],
)
def test_vlan_range(value: Any, expected: str) -> None:
    assert vlan_range(value) == expected


@pytest.mark.parametrize(
    "bad",
    [[], [True], [1, False], [0], [4095], [-1], ["10"], [1.0], [None], 5],
)
def test_vlan_range_rejects(bad: Any) -> None:
    with pytest.raises(ValueError):
        vlan_range(bad)
