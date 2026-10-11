"""Config tree parsing per family (design.md, Diff and compliance)."""

from __future__ import annotations

import pytest

from tsunami.core.configtree import ConfigNode, iter_paths, parse_config, secret_key

IOS = """\
Building configuration...

Current configuration : 1234 bytes
!
version 17.3
hostname sw-acme-01
!
interface Vlan10
 ip address 10.1.1.5 255.255.255.0
 no shutdown
!
router ospf 1
 network 10.1.1.0 0.0.0.255 area 0
!
key chain KC1
 key 1
  key-string abc
  send-lifetime 00:00:00 Jan 1 2025 infinite
end
"""

CX = """\
Current configuration:
!
!Version AOS-CX FL.10.13.1000
!export-password: default
hostname sw-acme-01
vlan 10
    name Users
interface 1/1/1
    no shutdown
    !comment
    vlan access 10
"""

AOSS_RUNNING = """\
; J9999A Configuration Editor; Created on release #YA.16.11
hostname "sw-acme-01"
vlan 10
   name "Users"
   untagged 1-4
   exit
interface 1
   name "Uplink to core"
   exit
"""

AOSS_SNIPPET = """\
vlan 10
   name "Users"
   untagged 1-4
interface 1
   name "Uplink to core"
"""


def shape(nodes: tuple[ConfigNode, ...]) -> list:
    return [(n.text, shape(n.children)) for n in nodes]


def test_ios_nesting_and_noise() -> None:
    tree = parse_config(IOS, "ios")
    assert shape(tree) == [
        ("hostname sw-acme-01", []),
        ("interface Vlan10", [("ip address 10.1.1.5 255.255.255.0", []), ("no shutdown", [])]),
        ("router ospf 1", [("network 10.1.1.0 0.0.0.255 area 0", [])]),
        (
            "key chain KC1",
            [
                (
                    "key 1",
                    [("key-string abc", []), ("send-lifetime 00:00:00 Jan 1 2025 infinite", [])],
                )
            ],
        ),
    ]


def test_ios_line_numbers() -> None:
    tree = parse_config(IOS, "ios")
    assert tree[0].line == 6
    assert tree[1].line == 8 and tree[1].children[0].line == 9
    assert tree[3].children[0].children[1].line == 18


def test_ios_paths() -> None:
    paths = list(iter_paths(parse_config(IOS, "ios")))
    assert ("interface Vlan10", "ip address 10.1.1.5 255.255.255.0") in paths
    assert ("key chain KC1", "key 1", "key-string abc") in paths
    assert len(paths) == 10


def test_ios_end_only_dropped_at_top_level() -> None:
    tree = parse_config("line vty 0 4\n end\n", "ios")
    assert shape(tree) == [("line vty 0 4", [("end", [])])]


def test_ios_accepts_sequence_and_crlf() -> None:
    a = parse_config("interface Vlan10\r\n ip  address 1.1.1.1   255.0.0.0  \r\n", "ios")
    b = parse_config(["interface Vlan10", " ip address 1.1.1.1 255.0.0.0"], "ios")
    assert shape(a) == shape(b)


def test_aoscx_four_space_tree() -> None:
    tree = parse_config(CX, "aos-cx")
    assert shape(tree) == [
        ("hostname sw-acme-01", []),
        ("vlan 10", [("name Users", [])]),
        ("interface 1/1/1", [("no shutdown", []), ("vlan access 10", [])]),
    ]
    assert tree[2].children[1].line == 11


def test_aoscx_trailing_space_on_context_line() -> None:
    assert shape(parse_config("interface 1/1/31 \n    no shutdown\n", "aos-cx")) == [
        ("interface 1/1/31", [("no shutdown", [])])
    ]


def test_aoss_exit_closes_blocks_and_is_dropped() -> None:
    tree = parse_config(AOSS_RUNNING, "aos-s")
    assert shape(tree) == [
        ('hostname "sw-acme-01"', []),
        ("vlan 10", [('name "Users"', []), ("untagged 1-4", [])]),
        ("interface 1", [('name "Uplink to core"', [])]),
    ]


def test_aoss_snippet_without_exit_matches_running() -> None:
    running = parse_config(AOSS_RUNNING, "aos-s")
    snippet = parse_config(AOSS_SNIPPET, "aos-s")
    assert shape(snippet) == shape(running)[1:]


def test_aoss_running_configuration_header_dropped() -> None:
    tree = parse_config('Running configuration:\n\nhostname "a"\n', "aos-s")
    assert [n.text for n in tree] == ['hostname "a"']


def test_aoss_top_level_exit_ignored() -> None:
    assert shape(parse_config('hostname "a"\nexit\nvlan 1\n', "aos-s")) == [
        ('hostname "a"', []),
        ("vlan 1", []),
    ]


def test_aoss_quoted_names_compared_as_printed() -> None:
    node = parse_config('vlan 10\n   name  "Two  Words"\n', "aos-s")[0].children[0]
    assert node.text == 'name "Two Words"'


@pytest.mark.parametrize(
    ("family", "line", "parent", "expected"),
    [
        ("ios", "tacacs server ISE1", None, "tacacs server ISE1"),
        ("ios", "key 7 0822455D0A16", "tacacs server ISE1", "key"),
        ("ios", "key ********", "tacacs server ISE1", "key"),
        ("ios", "key 1", "key chain KC1", "key 1"),
        (
            "aos-cx",
            "radius-server host 192.0.2.20 key plaintext ********",
            None,
            "radius-server host 192.0.2.20 key",
        ),
        (
            "aos-cx",
            "radius-server host 192.0.2.20 key ciphertext AQBapEXAMPLE==",
            None,
            "radius-server host 192.0.2.20 key",
        ),
        (
            "aos-s",
            'radius-server host 192.0.2.20 key "********"',
            None,
            "radius-server host 192.0.2.20 key",
        ),
        ("aos-s", "radius-server host 192.0.2.20", None, "radius-server host 192.0.2.20"),
    ],
)
def test_secret_key(family: str, line: str, parent: str | None, expected: str) -> None:
    assert secret_key(line, family, parent) == expected
