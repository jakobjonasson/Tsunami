"""Variable merge and provenance (design.md, Inventory and variables)."""

from __future__ import annotations

from pathlib import Path

import pytest

from tsunami.core.resolver import Layer, Level, Source, resolve

G = Path("inventory/global.yml")
R = Path("inventory/roles/access.yml")
C = Path("inventory/customers/acme/customer.yml")
S = Path("inventory/customers/acme/sites/site-a.yml")
D = Path("inventory/customers/acme/devices.yml")
P = Path("plays/push/add-vlan.yml")


def all_levels(name: str) -> list[Layer]:
    return [
        Layer(Level.GLOBAL, {name: "global"}, G),
        Layer(Level.ROLE, {name: "role"}, R),
        Layer(Level.CUSTOMER, {name: "customer"}, C),
        Layer(Level.CUSTOMER_ROLE, {name: "customer-role"}, C),
        Layer(Level.SITE, {name: "site"}, S),
        Layer(Level.DEVICE, {name: "device"}, D),
        Layer(Level.PLAY, {name: "play"}, P),
        Layer(Level.PROMPT, {name: "prompt"}),
    ]


@pytest.mark.parametrize("top", range(1, 9))
def test_each_level_beats_every_lower_level(top):
    layers = all_levels("x")[:top]
    resolved = resolve(layers)
    assert resolved.values["x"] == layers[-1].values["x"]
    assert resolved.source_of("x") == Source(layers[-1].level, layers[-1].file)


def test_lists_replace_and_do_not_append():
    resolved = resolve(
        [
            Layer(Level.GLOBAL, {"ntp_servers": ["192.0.2.10", "192.0.2.11"]}, G),
            Layer(Level.CUSTOMER, {"ntp_servers": ["198.51.100.1"]}, C),
        ]
    )
    assert resolved.values["ntp_servers"] == ["198.51.100.1"]
    assert resolved.source_of("ntp_servers").level == Level.CUSTOMER


def test_dicts_deep_merge_with_per_key_provenance():
    resolved = resolve(
        [
            Layer(
                Level.GLOBAL, {"snmp": {"location": "dc", "contact": "noc", "opts": {"a": 1}}}, G
            ),
            Layer(Level.SITE, {"snmp": {"location": "site-a", "opts": {"b": 2}}}, S),
        ]
    )
    assert resolved.values["snmp"] == {
        "location": "site-a",
        "contact": "noc",
        "opts": {"a": 1, "b": 2},
    }
    assert resolved.source_of("snmp", "location") == Source(Level.SITE, S)
    assert resolved.source_of("snmp", "contact") == Source(Level.GLOBAL, G)
    assert resolved.source_of("snmp", "opts", "a") == Source(Level.GLOBAL, G)
    assert resolved.source_of("snmp", "opts", "b") == Source(Level.SITE, S)


def test_scalar_replacing_a_dict_drops_the_old_nested_provenance():
    resolved = resolve(
        [
            Layer(Level.GLOBAL, {"banner": {"motd": "x"}}, G),
            Layer(Level.DEVICE, {"banner": "plain"}, D),
        ]
    )
    assert resolved.values["banner"] == "plain"
    assert resolved.source_of("banner", "motd") is None


def test_dict_replacing_a_scalar_records_nested_provenance():
    resolved = resolve(
        [Layer(Level.GLOBAL, {"aaa": None}, G), Layer(Level.CUSTOMER, {"aaa": {"x": 1}}, C)]
    )
    assert resolved.values["aaa"] == {"x": 1}
    assert resolved.source_of("aaa", "x") == Source(Level.CUSTOMER, C)


def test_null_is_an_ordinary_value():
    resolved = resolve([Layer(Level.GLOBAL, {"x": "a"}, G), Layer(Level.SITE, {"x": None}, S)])
    assert resolved.values["x"] is None
    assert resolved.source_of("x").level == Level.SITE


def test_defaults_lose_to_any_layer_and_builtins_win():
    resolved = resolve(
        [Layer(Level.GLOBAL, {"domain": "example.local"}, G)],
        defaults={"hostname": "sa-as01"},
        builtins={"customer": "acme", "site": "site-a"},
    )
    assert resolved.values == {
        "hostname": "sa-as01",
        "domain": "example.local",
        "customer": "acme",
        "site": "site-a",
    }
    assert resolved.source_of("hostname") == Source(Level.DEFAULT)
    assert resolved.source_of("customer") == Source(Level.BUILTIN)

    overridden = resolve(
        [Layer(Level.DEVICE, {"hostname": "SA-AS01"}, D)], defaults={"hostname": "sa-as01"}
    )
    assert overridden.values["hostname"] == "SA-AS01"


def test_inputs_are_not_mutated_and_result_is_independent():
    global_vars = {"snmp": {"location": "dc"}, "ntp": ["192.0.2.10"]}
    resolved = resolve(
        [Layer(Level.GLOBAL, global_vars, G), Layer(Level.SITE, {"snmp": {"x": 1}}, S)]
    )
    resolved.values["snmp"]["location"] = "changed"
    resolved.values["ntp"].append("192.0.2.99")
    assert global_vars == {"snmp": {"location": "dc"}, "ntp": ["192.0.2.10"]}


def test_layers_must_be_in_precedence_order():
    with pytest.raises(ValueError, match="precedence order"):
        resolve([Layer(Level.SITE, {}, S), Layer(Level.GLOBAL, {}, G)])


def test_level_labels_for_display():
    assert Level.CUSTOMER_ROLE.label == "customer role"
    assert Level.BUILTIN.label == "built-in"
    assert [lvl.value for lvl in Level][1:9] == list(range(1, 9))
