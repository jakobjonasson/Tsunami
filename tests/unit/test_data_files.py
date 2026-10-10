"""Shipped data files. Structural checks until the Phase 1 catalog model (work package 1.9)."""

from __future__ import annotations

import re
from importlib.resources import files

import pytest
from ruamel.yaml import YAML

from tsunami.core.models.common import (
    NETMIKO_SSH_DEVICE_TYPE,
    SLUG_PATTERN,
    SUPPORTED_PLATFORMS,
    Platform,
)

DATA = files("tsunami.core.data")
CATALOG_KEYS = {
    "id", "vendor", "family", "platform", "netmiko_device_type", "console_baud",
    "lifecycle", "eos_year", "show_version_model", "examples",
}  # fmt: skip
BAUD_RATES = {9600, 19200, 38400, 57600, 115200}


def load(name: str):
    return YAML(typ="safe").load(DATA.joinpath(name).read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def catalog() -> list[dict]:
    return load("hardware.yml")


def test_catalog_ids_are_unique_slugs(catalog):
    ids = [entry["id"] for entry in catalog]
    assert len(ids) == len(set(ids))
    assert all(re.fullmatch(SLUG_PATTERN, i) for i in ids)


def test_catalog_entries_are_well_formed(catalog):
    for entry in catalog:
        where = entry.get("id")
        assert set(entry) == CATALOG_KEYS, where
        assert entry["vendor"] in {"cisco", "aruba"}, where
        assert entry["platform"] in set(Platform), where
        assert entry["console_baud"] in BAUD_RATES, where
        assert entry["lifecycle"] in {"current", "end-of-sale"}, where
        assert entry["eos_year"] is None or isinstance(entry["eos_year"], int), where
        assert entry["examples"], where
        re.compile(entry["show_version_model"])


def test_supported_entries_use_the_platform_device_type(catalog):
    for entry in catalog:
        platform = Platform(entry["platform"])
        if platform in SUPPORTED_PLATFORMS:
            assert entry["netmiko_device_type"] == NETMIKO_SSH_DEVICE_TYPE[platform], entry["id"]


def test_default_policy_ships_disabled():
    policy = load("default-mcp-policy.yml")
    assert policy["enabled"] is False
    assert policy["apply"] in {"never", "lab_only"}
