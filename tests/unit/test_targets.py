"""Target resolution (design.md, Play schema: targets; Run-time options: Limit)."""

from __future__ import annotations

import pytest

from tsunami.core.models.inventory import Device
from tsunami.core.models.play import Targets
from tsunami.core.targets import resolve_targets


def _dev(name: str, host: str, site: str, role: str) -> Device:
    return Device(name=name, host=host, site=site, role=role, platform="aos-cx")


DEVICES = [
    _dev("sb-as01", "198.51.100.1", "sb", "access"),
    _dev("sa-as01", "192.0.2.1", "sa", "access"),
    _dev("sa-ds01", "192.0.2.2", "sa", "distribution"),
    _dev("sb-ds01", "198.51.100.2", "sb", "distribution"),
    _dev("sc-as01", "203.0.113.1", "sc", "access"),
]
SITES = {"sa", "sb", "sc"}
ROLES = {"access", "distribution", "core"}


def run(targets, **kw):
    return resolve_targets(targets, DEVICES, sites=SITES, roles=ROLES, **kw)


def names(result):
    return [d.name for d in result.devices]


def codes(result):
    return [(f.code, f.loc) for f in result.findings]


@pytest.mark.parametrize(
    ("targets", "expected"),
    [
        (Targets(sites=["sa"]), ["sa-as01", "sa-ds01"]),
        (Targets(sites=["sa", "sc"]), ["sa-as01", "sa-ds01", "sc-as01"]),
        (Targets(roles=["distribution"]), ["sa-ds01", "sb-ds01"]),
        (Targets(devices=["sc-as01", "sa-as01"]), ["sa-as01", "sc-as01"]),
        (Targets(sites=["sa", "sb"], roles=["access"]), ["sb-as01", "sa-as01"]),
        (Targets(sites=["sa"], roles=["access"], devices=["sa-as01", "sb-as01"]), ["sa-as01"]),
        (Targets(sites=["sa", "sa"], devices=["sa-as01", "sa-as01"]), ["sa-as01"]),
        (Targets(sites=["sa"], devices=["sb-as01"]), None),
    ],
)
def test_and_across_keys_or_within(targets, expected):
    result = run(targets)
    if expected is None:
        assert codes(result) == [("targets.no_devices", ("targets",))]
        assert not result.ok
    else:
        assert names(result) == expected
        assert result.ok
        assert result.findings == ()


def test_exclude_applied_last():
    result = run(Targets(sites=["sa"], exclude=["sa-ds01"]))
    assert names(result) == ["sa-as01"]
    assert result.ok


def test_exclude_everything_is_no_devices():
    result = run(Targets(sites=["sa"], exclude=["sa-as01", "sa-ds01"]))
    assert result.devices == ()
    assert codes(result) == [("targets.no_devices", ("targets",))]


@pytest.mark.parametrize("targets", [None, Targets(), Targets(exclude=["sa-as01"])])
def test_empty_targets(targets):
    result = run(targets)
    assert result.devices == ()
    assert codes(result) == [("targets.empty", ("targets",))]
    assert "all devices" in result.findings[0].message
    assert not result.ok


@pytest.mark.parametrize(
    ("targets", "code", "loc", "value"),
    [
        (Targets(sites=["sa", "sb", "zz"]), "targets.unknown_site", ("targets", "sites", 2), "zz"),
        (Targets(roles=["wan"]), "targets.unknown_role", ("targets", "roles", 0), "wan"),
        (
            Targets(devices=["sa-as01", "nope"]),
            "targets.unknown_device",
            ("targets", "devices", 1),
            "nope",
        ),
        (
            Targets(sites=["sa"], exclude=["sa-as01", "ghost"]),
            "targets.unknown_device",
            ("targets", "exclude", 1),
            "ghost",
        ),
    ],
)
def test_unknown_slug(targets, code, loc, value):
    result = run(targets)
    assert not result.ok
    assert codes(result) == [(code, loc)]
    assert value in result.findings[0].message


def test_multiple_findings_all_reported():
    result = run(
        Targets(sites=["zz"], roles=["access", "wan"], devices=["nope"], exclude=["ghost"])
    )
    assert codes(result) == [
        ("targets.unknown_site", ("targets", "sites", 0)),
        ("targets.unknown_role", ("targets", "roles", 1)),
        ("targets.unknown_device", ("targets", "devices", 0)),
        ("targets.unknown_device", ("targets", "exclude", 0)),
    ]


def test_order_follows_input_not_target_lists():
    result = run(Targets(devices=["sc-as01", "sa-ds01", "sb-as01"]))
    assert names(result) == ["sb-as01", "sa-ds01", "sc-as01"]


def test_limit_subset():
    result = run(Targets(sites=["sa", "sb"]), limit=["sa-ds01", "sb-as01"])
    assert names(result) == ["sb-as01", "sa-ds01"]
    assert result.ok


def test_limit_outside_targets():
    result = run(Targets(sites=["sa"]), limit=["sa-as01", "sb-as01"])
    assert names(result) == ["sa-as01"]
    assert codes(result) == [("targets.limit_not_in_targets", ())]
    assert "sb-as01" in result.findings[0].message
    assert not result.ok


def test_limit_to_nothing():
    result = run(Targets(sites=["sa"]), limit=[])
    assert result.devices == ()
    assert codes(result) == [("targets.no_devices", ())]


def test_limit_all_outside_targets():
    result = run(Targets(sites=["sa"]), limit=["sc-as01"])
    assert codes(result) == [
        ("targets.limit_not_in_targets", ()),
        ("targets.no_devices", ()),
    ]


def test_limit_excluded_device_is_outside_targets():
    result = run(Targets(sites=["sa"], exclude=["sa-ds01"]), limit=["sa-ds01"])
    assert ("targets.limit_not_in_targets", ()) in codes(result)
