"""Hardware catalog model and the shipped data/hardware.yml (design.md, Hardware catalog)."""

from __future__ import annotations

import copy

import pytest

from tsunami.core.catalog import Catalog, CatalogEntry, load_catalog, parse_catalog
from tsunami.core.issues import TsunamiError
from tsunami.core.models.common import (
    NETMIKO_SSH_DEVICE_TYPE,
    SUPPORTED_PLATFORMS,
    Platform,
    PlatformFamily,
)

GOOD = {
    "id": "cisco-c9300",
    "vendor": "cisco",
    "label": "Catalyst 9300",
    "platform": "ios-xe",
    "netmiko_device_type": "cisco_xe",
    "console_baud": 9600,
    "lifecycle": "current",
    "eos_year": None,
    "model_pattern": "^C9300",
    "examples": ["C9300-24P"],
}


def entry_dict(**overrides) -> dict:
    return {**copy.deepcopy(GOOD), **overrides}


@pytest.fixture(scope="module")
def catalog() -> Catalog:
    return load_catalog()


def test_shipped_catalog_loads_and_is_cached(catalog):
    assert len(catalog.entries) > 0
    assert load_catalog() is catalog


def test_ids_are_unique(catalog):
    ids = [e.id for e in catalog.entries]
    assert len(ids) == len(set(ids))
    assert catalog.ids() == frozenset(ids)


def test_every_entry_is_selectable_today(catalog):
    assert {e.platform for e in catalog.entries} <= SUPPORTED_PLATFORMS
    assert catalog.selectable() == catalog.entries


def test_device_type_matches_platform(catalog):
    for entry in catalog.entries:
        assert entry.netmiko_device_type == NETMIKO_SSH_DEVICE_TYPE[entry.platform], entry.id


def test_eos_year_only_for_end_of_sale(catalog):
    for entry in catalog.entries:
        assert (entry.eos_year is not None) == (entry.lifecycle == "end-of-sale"), entry.id


def test_examples_match_their_own_entry(catalog):
    # Cisco examples are model numbers as `show version` reports them.
    for entry in catalog.entries:
        if entry.vendor == "cisco":
            for example in entry.examples:
                assert entry.matches_model(example), (entry.id, example)


def test_get_returns_entry_and_raises_on_unknown(catalog):
    assert catalog.get("cisco-c9300").label.startswith("Catalyst 9300")
    with pytest.raises(TsunamiError, match="unknown hardware model: 'nope'"):
        catalog.get("nope")


def test_by_vendor_keeps_file_order(catalog):
    groups = catalog.by_vendor()
    assert list(groups) == ["cisco", "aruba"]
    for vendor, entries in groups.items():
        assert all(e.vendor == vendor for e in entries)
        assert [e.id for e in entries] == [e.id for e in catalog.entries if e.vendor == vendor]


def test_platform_family(catalog):
    assert catalog.get("cisco-c9300").platform_family is PlatformFamily.IOS
    assert catalog.get("cisco-c1000").platform_family is PlatformFamily.IOS
    assert catalog.get("aruba-cx6300").platform_family is PlatformFamily.AOS_CX
    assert catalog.get("aruba-2930f").platform_family is PlatformFamily.AOS_S


@pytest.mark.parametrize(
    ("reported", "expected"),
    [
        ("C9300-48P", {"cisco-c9300"}),
        ("C9300L-48P-4G", {"cisco-c9300"}),
        ("WS-C2960X-48FPD-L", {"cisco-c2960x"}),
        ("WS-C2960XR-48LPD-I", {"cisco-c2960x"}),
        ("WS-C3850-24P", {"cisco-c3850"}),
        ("WS-C2960SF-24PS-L", {"cisco-c2960s"}),
        ("WS-C4500X-32", {"cisco-c4500e"}),
        ("JL665A 6300F 48G CL4 PoE 4SFP56 Sw", {"aruba-cx6300"}),
        ("JL725A 6200F 24G CL4 4SFP+370W Sw", {"aruba-cx6200"}),
        ("2930F-24G-PoE+-4SFP+ JL261A", {"aruba-2930f"}),
        ("5406Rzl2 J9850A", {"aruba-5400r"}),
        ("2530-48G-PoE+ J9772A", {"aruba-2530"}),
        ("not a switch", set()),
    ],
)
def test_match_reported_model(catalog, reported, expected):
    assert {e.id for e in catalog.match_reported_model(reported)} == expected


def test_matches_model_is_a_search_not_a_full_match(catalog):
    entry = catalog.get("aruba-cx6300")
    assert entry.matches_model("JL665A 6300F 48G CL4 PoE 4SFP56 Sw")
    assert not entry.matches_model("JL725A 6200F 24G CL4 4SFP+370W Sw")


def test_entries_are_immutable(catalog):
    entry = catalog.get("cisco-c9300")
    with pytest.raises(ValueError):
        entry.label = "x"  # type: ignore[misc]
    with pytest.raises(AttributeError):
        catalog.entries = ()  # type: ignore[misc]


def test_good_entry_validates():
    entry = CatalogEntry.model_validate(GOOD)
    assert entry.selectable
    assert entry.platform is Platform.IOS_XE


def test_duplicate_id_is_rejected_with_the_id():
    with pytest.raises(TsunamiError, match="duplicate id 'cisco-c9300'"):
        parse_catalog([entry_dict(), entry_dict()])


def test_bad_regex_is_rejected_with_the_id():
    with pytest.raises(TsunamiError, match=r"cisco-c9300.*model_pattern"):
        parse_catalog([entry_dict(model_pattern="C93(")])


def test_unknown_key_is_rejected():
    with pytest.raises(TsunamiError, match="cisco-c9300"):
        parse_catalog([entry_dict(family="Catalyst 9300")])


def test_wrong_device_type_for_supported_platform_is_rejected():
    with pytest.raises(TsunamiError, match=r"cisco-c9300.*cisco_xe"):
        parse_catalog([entry_dict(netmiko_device_type="cisco_ios")])


def test_empty_examples_are_rejected():
    with pytest.raises(TsunamiError, match="examples"):
        parse_catalog([entry_dict(examples=[])])


def test_bad_baud_and_lifecycle_are_rejected():
    with pytest.raises(TsunamiError, match="console_baud"):
        parse_catalog([entry_dict(console_baud=1234)])
    with pytest.raises(TsunamiError, match="lifecycle"):
        parse_catalog([entry_dict(lifecycle="retired")])


def test_failure_names_the_index_when_there_is_no_id():
    bad = entry_dict()
    del bad["id"]
    with pytest.raises(TsunamiError, match=r"entry #1"):
        parse_catalog([entry_dict(id="ok"), bad])


def test_top_level_must_be_a_list():
    with pytest.raises(TsunamiError, match="list"):
        parse_catalog({"id": "x"})
