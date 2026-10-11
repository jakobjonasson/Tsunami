"""Shipped data files. The hardware catalog is tested in test_catalog.py."""

from __future__ import annotations

from importlib.resources import files

from ruamel.yaml import YAML

from tsunami.core.models.common import PlatformFamily

DATA = files("tsunami.core.data")


def load(name: str):
    return YAML(typ="safe").load(DATA.joinpath(name).read_text(encoding="utf-8"))


def test_default_policy_ships_disabled():
    policy = load("default-mcp-policy.yml")
    assert policy["enabled"] is False
    assert policy["apply"] in {"never", "lab_only"}


def test_default_policy_covers_every_family_without_write_out_pipes():
    policy = load("default-mcp-policy.yml")
    families = set(PlatformFamily)
    assert set(policy["show_allowlist"]) == families
    assert set(policy["pipes"]) == families
    for family, pipes in policy["pipes"].items():
        assert not {"redirect", "tee", "append"} & set(pipes), family
    for family, commands in policy["show_allowlist"].items():
        assert all(c.startswith("show ") for c in commands), family
