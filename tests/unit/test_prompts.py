"""Prompt validators (design.md, Play schema: prompts; Testing: Unit tests)."""

from __future__ import annotations

import pytest

from tsunami.core.models.common import PlatformFamily
from tsunami.core.models.play import PromptType
from tsunami.core.prompts import PromptContext, PromptValueError, coerce, placeholder

P = PromptType
CTX = PromptContext(
    sites=frozenset({"hq", "branch"}),
    roles=frozenset({"access", "core"}),
    model_ids=frozenset({"c9300-48p", "cx6200-24g"}),
    family=PlatformFamily.IOS,
    logins=frozenset({"netadmin2", "acme-netadmin"}),
)
CHOICES = ["alpha", "beta"]


def run(ptype: PromptType, raw: object, **kw: object) -> object:
    kw.setdefault("context", CTX)
    return coerce(ptype, raw, **kw)  # type: ignore[arg-type]


# ---- vlan -----------------------------------------------------------------


@pytest.mark.parametrize("value", [1, 4094, "1", "4094", " 100 "])
def test_vlan_accepts(value: object) -> None:
    assert run(P.VLAN, value) == int(str(value).strip())


@pytest.mark.parametrize("value", [0, 4095, -1, "0", "4095", "abc", "", "1.5", 1.0, None, [1]])
def test_vlan_rejects(value: object) -> None:
    with pytest.raises(PromptValueError):
        run(P.VLAN, value)


@pytest.mark.parametrize("ptype", [P.VLAN, P.INT])
@pytest.mark.parametrize("value", [True, False])
def test_bool_rejected_as_integer(ptype: PromptType, value: bool) -> None:
    with pytest.raises(PromptValueError):
        run(ptype, value)


def test_bool_rejected_in_vlan_list() -> None:
    with pytest.raises(PromptValueError):
        run(P.VLAN_LIST, [True])


# ---- int ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "kw", "expected"),
    [
        (5, {}, 5),
        ("5", {}, 5),
        (" -7 ", {}, -7),
        (0, {"minimum": 0, "maximum": 0}, 0),
        (10, {"minimum": 10, "maximum": 20}, 10),
        (20, {"minimum": 10, "maximum": 20}, 20),
    ],
)
def test_int_accepts(raw: object, kw: dict[str, int], expected: int) -> None:
    assert run(P.INT, raw, **kw) == expected


@pytest.mark.parametrize(
    ("raw", "kw"),
    [
        (9, {"minimum": 10}),
        (21, {"maximum": 20}),
        ("abc", {}),
        ("1.5", {}),
        (1.5, {}),
        ("", {}),
        ("0x10", {}),
        ("1_000", {}),
        (None, {}),
        ("١", {}),
    ],
)
def test_int_rejects(raw: object, kw: dict[str, int]) -> None:
    with pytest.raises(PromptValueError):
        run(P.INT, raw, **kw)


# ---- bool -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(True, True), (False, False), ("true", True), ("FALSE", False), (" True ", True)],
)
def test_bool(raw: object, expected: bool) -> None:
    result = run(P.BOOL, raw)
    assert result is expected


@pytest.mark.parametrize("raw", ["yes", "1", 1, 0, "", None])
def test_bool_rejects(raw: object) -> None:
    with pytest.raises(PromptValueError):
        run(P.BOOL, raw)


# ---- string ---------------------------------------------------------------


def test_string_kept_exactly() -> None:
    assert run(P.STRING, "  spaced out  ") == "  spaced out  "


def test_string_pattern_uses_fullmatch() -> None:
    assert run(P.STRING, "ab12", pattern=r"[a-z]+[0-9]+") == "ab12"
    with pytest.raises(PromptValueError):
        run(P.STRING, "ab12!", pattern=r"[a-z]+[0-9]+")
    with pytest.raises(PromptValueError):
        run(P.STRING, "!ab12", pattern=r"[a-z]+[0-9]+")


def test_string_bad_pattern_is_prompt_error() -> None:
    with pytest.raises(PromptValueError):
        run(P.STRING, "x", pattern="(")


def test_string_non_text_rejected() -> None:
    with pytest.raises(PromptValueError):
        run(P.STRING, 5)


# ---- choice ---------------------------------------------------------------


def test_choice() -> None:
    assert run(P.CHOICE, "beta", choices=CHOICES) == "beta"
    assert run(P.CHOICE, 10, choices=["10", "20"]) == "10"
    with pytest.raises(PromptValueError):
        run(P.CHOICE, "gamma", choices=CHOICES)
    with pytest.raises(PromptValueError):
        run(P.CHOICE, "Alpha", choices=CHOICES)
    with pytest.raises(PromptValueError):
        run(P.CHOICE, True, choices=["True"])


def test_choice_without_choices_raises() -> None:
    with pytest.raises(PromptValueError):
        run(P.CHOICE, "alpha")


# ---- hostname -------------------------------------------------------------


@pytest.mark.parametrize(
    "value", ["a", "sw1", "core-sw-01", "A", "a" * 63, "a" + "-" * 61 + "b", " sw1 "]
)
def test_hostname_accepts(value: str) -> None:
    assert run(P.HOSTNAME, value) == value.strip()


@pytest.mark.parametrize(
    "value",
    ["", "a" * 64, "1sw", "-sw", "sw-", "sw_1", "sw 1", "sw.example", "sw!", "swé", "١sw"],
)
def test_hostname_rejects(value: str) -> None:
    with pytest.raises(PromptValueError):
        run(P.HOSTNAME, value)


# ---- vlan_list ------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("10", [10]),
        ("10,20,30-35", [10, 20, 30, 31, 32, 33, 34, 35]),
        ("30-32, 10 , 20", [10, 20, 30, 31, 32]),
        (" 10 - 12 ", [10, 11, 12]),
        ("20,10,20,10-11", [10, 11, 20]),
        ("5-5", [5]),
        ("1,4094", [1, 4094]),
        ("1-4094", list(range(1, 4095))),
        ([30, 10, 20, 10], [10, 20, 30]),
        ((3, 2), [2, 3]),
    ],
)
def test_vlan_list_accepts(raw: object, expected: list[int]) -> None:
    assert run(P.VLAN_LIST, raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        [],
        "35-30",
        "0",
        "4095",
        "10,4095",
        "0-5",
        "4090-4095",
        "10,,20",
        "10,",
        ",10",
        "abc",
        "10-",
        "-10",
        "10-20-30",
        "1 0",
        [0],
        [4095],
        ["10"],
        [1.5],
        None,
        10,
    ],
)
def test_vlan_list_rejects(raw: object) -> None:
    with pytest.raises(PromptValueError):
        run(P.VLAN_LIST, raw)


# ---- interface ------------------------------------------------------------


@pytest.mark.parametrize(
    ("family", "good", "bad"),
    [
        (PlatformFamily.IOS, "GigabitEthernet1/0/1", "Gi1/0/1"),
        (PlatformFamily.AOS_CX, "1/1/1", "GigabitEthernet1/0/1"),
        (PlatformFamily.AOS_S, "A1", "1/1/1"),
    ],
)
def test_interface_per_family(family: PlatformFamily, good: str, bad: str) -> None:
    ctx = PromptContext(family=family)
    assert run(P.INTERFACE, f" {good} ", context=ctx) == good
    with pytest.raises(PromptValueError):
        run(P.INTERFACE, bad, context=ctx)


def test_interface_range_and_vlan_bounds() -> None:
    with pytest.raises(PromptValueError):
        run(P.INTERFACE, "GigabitEthernet1/0/1-4")
    cx = PromptContext(family=PlatformFamily.AOS_CX)
    assert run(P.INTERFACE, "vlan 4094", context=cx) == "vlan 4094"
    with pytest.raises(PromptValueError):
        run(P.INTERFACE, "vlan 4095", context=cx)


def test_interface_requires_family() -> None:
    with pytest.raises(PromptValueError):
        run(P.INTERFACE, "GigabitEthernet1/0/1", context=PromptContext())


def test_interface_non_text_rejected() -> None:
    with pytest.raises(PromptValueError):
        run(P.INTERFACE, 1)


# ---- ipv4 -----------------------------------------------------------------


@pytest.mark.parametrize("value", ["192.0.2.1", "198.51.100.255", "10.0.0.0", " 203.0.113.9 "])
def test_ipv4_accepts(value: str) -> None:
    assert run(P.IPV4, value) == value.strip()


@pytest.mark.parametrize(
    "value",
    [
        "192.0.2.1/24",
        "192.0.2.1/32",
        "192.0.2",
        "192.0.2.256",
        "192.0.2.01",
        "::1",
        "a.b.c.d",
        "",
        "192.0.2.1 1",
    ],
)
def test_ipv4_rejects(value: str) -> None:
    with pytest.raises(PromptValueError):
        run(P.IPV4, value)


# ---- ipv4_interface -------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("192.0.2.5/24", "192.0.2.5/24"),
        ("192.0.2.1/24", "192.0.2.1/24"),
        ("192.0.2.254/24", "192.0.2.254/24"),
        ("192.0.2.5/255.255.255.0", "192.0.2.5/24"),
        ("198.51.100.1/30", "198.51.100.1/30"),
        ("198.51.100.2/30", "198.51.100.2/30"),
        # /31 and /32 accept any address.
        ("198.51.100.0/31", "198.51.100.0/31"),
        ("198.51.100.1/31", "198.51.100.1/31"),
        ("198.51.100.4/32", "198.51.100.4/32"),
        ("198.51.100.0/32", "198.51.100.0/32"),
        ("10.1.1.5/8", "10.1.1.5/8"),
        (" 203.0.113.7/24 ", "203.0.113.7/24"),
    ],
)
def test_ipv4_interface_accepts(raw: str, expected: str) -> None:
    assert run(P.IPV4_INTERFACE, raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "192.0.2.0/24",  # network
        "192.0.2.255/24",  # broadcast
        "198.51.100.0/30",  # network
        "198.51.100.3/30",  # broadcast
        "192.0.2.0/255.255.255.0",
        "192.0.2.1",  # no prefix
        "192.0.2.1/",
        "192.0.2.1/33",
        "192.0.2.1/0.0.0.255",  # wildcard mask
        "192.0.2.1/255.0.255.0",
        "192.0.2.1/-1",
        "192.0.2.1/24/24",
        "192.0.2.256/24",
        "192.0.2.1 /24",
        "::1/64",
        "",
    ],
)
def test_ipv4_interface_rejects(raw: str) -> None:
    with pytest.raises(PromptValueError):
        run(P.IPV4_INTERFACE, raw)


# ---- ipv4_network ---------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("192.0.2.0/24", "192.0.2.0/24"),
        ("192.0.2.0/255.255.255.0", "192.0.2.0/24"),
        ("198.51.100.4/30", "198.51.100.4/30"),
        ("198.51.100.4/32", "198.51.100.4/32"),
        ("0.0.0.0/0", "0.0.0.0/0"),
    ],
)
def test_ipv4_network_accepts(raw: str, expected: str) -> None:
    assert run(P.IPV4_NETWORK, raw) == expected


@pytest.mark.parametrize(
    "raw", ["192.0.2.1/24", "192.0.2.0", "192.0.2.0/33", "192.0.2.0/0.0.0.255", "x/24", ""]
)
def test_ipv4_network_rejects(raw: str) -> None:
    with pytest.raises(PromptValueError):
        run(P.IPV4_NETWORK, raw)


# ---- site / role / model --------------------------------------------------


@pytest.mark.parametrize(
    ("ptype", "good", "bad"),
    [
        (P.SITE, "hq", "nowhere"),
        (P.ROLE, "core", "edge"),
        (P.MODEL, "c9300-48p", "c9999"),
    ],
)
def test_membership(ptype: PromptType, good: str, bad: str) -> None:
    assert run(ptype, f" {good} ") == good
    with pytest.raises(PromptValueError) as err:
        run(ptype, bad)
    assert good in str(err.value)


def test_membership_empty_set_and_non_text() -> None:
    with pytest.raises(PromptValueError):
        run(P.SITE, "hq", context=PromptContext())
    with pytest.raises(PromptValueError):
        run(P.SITE, 1)


def test_membership_error_lists_at_most_ten() -> None:
    ctx = PromptContext(sites=frozenset(f"site{i:02d}" for i in range(15)))
    with pytest.raises(PromptValueError) as err:
        run(P.SITE, "nope", context=ctx)
    message = str(err.value)
    assert "site00" in message
    assert "site09" in message
    assert "site10" not in message


# ---- control characters ---------------------------------------------------

VALID_FOR_CONTROL: dict[PromptType, tuple[object, dict[str, object]]] = {
    P.STRING: ("hello", {}),
    P.INT: ("5", {}),
    P.BOOL: ("true", {}),
    P.CHOICE: ("alpha", {"choices": CHOICES}),
    P.HOSTNAME: ("sw1", {}),
    P.VLAN: ("10", {}),
    P.VLAN_LIST: ("10,20", {}),
    P.INTERFACE: ("GigabitEthernet1/0/1", {}),
    P.IPV4: ("192.0.2.1", {}),
    P.IPV4_INTERFACE: ("192.0.2.5/24", {}),
    P.IPV4_NETWORK: ("192.0.2.0/24", {}),
    P.SITE: ("hq", {}),
    P.ROLE: ("core", {}),
    P.MODEL: ("c9300-48p", {}),
    P.LOGIN: ("netadmin2", {}),
}


def test_control_table_covers_every_prompt_type() -> None:
    assert set(VALID_FOR_CONTROL) == set(PromptType)


@pytest.mark.parametrize("ptype", list(PromptType))
def test_valid_baseline_is_accepted(ptype: PromptType) -> None:
    raw, kw = VALID_FOR_CONTROL[ptype]
    run(ptype, raw, **kw)


@pytest.mark.parametrize("ptype", list(PromptType))
# \x85,   and   are line breaks to str.splitlines(); \x9f ends the C1 range.
@pytest.mark.parametrize(
    "char",
    ["\n", "\r", "\t", "\x00", "\x7f", "\x1b", "\x0b", "\x1f", "\x85", "\x9f", " ", " "],
)
@pytest.mark.parametrize("where", ["middle", "end", "start", "only"])
def test_control_characters_rejected(ptype: PromptType, char: str, where: str) -> None:
    raw, kw = VALID_FOR_CONTROL[ptype]
    assert isinstance(raw, str)
    if where == "middle":
        bad = raw[:1] + char + raw[1:]
    elif where == "end":
        bad = raw + char
    elif where == "start":
        bad = char + raw
    else:
        bad = char
    with pytest.raises(PromptValueError):
        run(ptype, bad, **kw)


@pytest.mark.parametrize("char", ["\n", "\r", "\t", "\x00", "\x7f"])
def test_control_characters_rejected_in_lists(char: str) -> None:
    with pytest.raises(PromptValueError):
        run(P.VLAN_LIST, [10, f"2{char}0"])


def test_injection_payload_rejected_for_string_with_permissive_pattern() -> None:
    with pytest.raises(PromptValueError):
        run(P.STRING, "ok\nno shutdown", pattern=r".*")


# ---- whitespace -----------------------------------------------------------


@pytest.mark.parametrize("ptype", [t for t in PromptType if t not in (P.STRING,)])
def test_surrounding_spaces_stripped_for_text_types(ptype: PromptType) -> None:
    raw, kw = VALID_FOR_CONTROL[ptype]
    assert run(ptype, f"  {raw}  ", **kw) == run(ptype, raw, **kw)


# ---- placeholders ---------------------------------------------------------


@pytest.mark.parametrize("family", list(PlatformFamily))
@pytest.mark.parametrize("ptype", list(PromptType))
def test_placeholder_round_trips(ptype: PromptType, family: PlatformFamily) -> None:
    ctx = PromptContext(
        sites=CTX.sites,
        roles=CTX.roles,
        model_ids=CTX.model_ids,
        family=family,
        logins=CTX.logins,
    )
    value = placeholder(ptype, context=ctx, choices=CHOICES)
    result = coerce(ptype, value, context=ctx, choices=CHOICES)
    assert result == value


@pytest.mark.parametrize(
    ("ptype", "kw", "expected"),
    [
        (P.STRING, {}, "placeholder"),
        (P.INT, {}, 0),
        (P.INT, {"minimum": 5, "maximum": 9}, 5),
        (P.BOOL, {}, False),
        (P.CHOICE, {"choices": ["zeta", "alpha"]}, "zeta"),
        (P.HOSTNAME, {}, "placeholder-host"),
        (P.VLAN, {}, 1),
        (P.VLAN_LIST, {}, [1]),
        (P.IPV4, {}, "192.0.2.1"),
        (P.IPV4_INTERFACE, {}, "192.0.2.1/24"),
        (P.IPV4_NETWORK, {}, "192.0.2.0/24"),
        (P.SITE, {}, "branch"),
        (P.ROLE, {}, "access"),
        (P.MODEL, {}, "c9300-48p"),
        (P.LOGIN, {}, "acme-netadmin"),
    ],
)
def test_placeholder_values(ptype: PromptType, kw: dict[str, object], expected: object) -> None:
    assert placeholder(ptype, context=CTX, **kw) == expected  # type: ignore[arg-type]


def test_placeholder_int_respects_negative_maximum() -> None:
    ctx = PromptContext()
    assert coerce(P.INT, placeholder(P.INT, context=ctx, maximum=-5), context=ctx, maximum=-5) == -5


@pytest.mark.parametrize(
    ("family", "expected"),
    [
        (PlatformFamily.IOS, "GigabitEthernet1/0/1"),
        (PlatformFamily.AOS_CX, "1/1/1"),
        (PlatformFamily.AOS_S, "1"),
    ],
)
def test_placeholder_interface(family: PlatformFamily, expected: str) -> None:
    assert placeholder(P.INTERFACE, context=PromptContext(family=family)) == expected


@pytest.mark.parametrize(
    ("ptype", "kw"),
    [
        (P.INTERFACE, {}),
        (P.CHOICE, {}),
        (P.CHOICE, {"choices": []}),
        (P.SITE, {}),
        (P.ROLE, {}),
        (P.MODEL, {}),
        (P.LOGIN, {}),
    ],
)
def test_placeholder_raises_when_unavailable(ptype: PromptType, kw: dict[str, object]) -> None:
    with pytest.raises(PromptValueError):
        placeholder(ptype, context=PromptContext(), **kw)  # type: ignore[arg-type]


# ---- login prompts --------------------------------------------------------


@pytest.mark.parametrize("name", ["other-customers-login", "no-such-login", "NETADMIN2"])
def test_a_login_outside_the_customer_reads_the_same(name: str) -> None:
    with pytest.raises(PromptValueError) as caught:
        run(P.LOGIN, name)
    assert str(caught.value) == (
        f"login {name!r} is not available to this customer; valid: acme-netadmin, netadmin2"
    )
