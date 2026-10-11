"""Output modifiers applied locally to redacted show output (core/showfilter.py)."""

from __future__ import annotations

import pytest

from tsunami.core.showfilter import FilterError, ShowCommand, apply_filter, split_command

CONFIG = """\
hostname sw1
ntp server 192.0.2.1
interface GigabitEthernet1/0/1
 description uplink
 switchport mode trunk
interface GigabitEthernet1/0/2
 description printer
line vty 0 4
 transport input ssh
"""


def test_split_command():
    assert split_command("show running-config") == ShowCommand("show running-config")
    assert split_command("show running-config | include ntp server") == ShowCommand(
        "show running-config", "include", "ntp server"
    )
    assert split_command("show interfaces | count") == ShowCommand("show interfaces", "count")


INTERFACES = ["interface GigabitEthernet1/0/1", "interface GigabitEthernet1/0/2"]
GI1 = ["interface GigabitEthernet1/0/1", " description uplink", " switchport mode trunk"]


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("show x | include ntp", ["ntp server 192.0.2.1"]),
        ("show x | include ^interface", INTERFACES),
        (
            "show x | exclude ^interface|^ |^$",
            ["hostname sw1", "ntp server 192.0.2.1", "line vty 0 4"],
        ),
        ("show x | begin ^line", ["line vty 0 4", " transport input ssh", ""]),
        ("show x | section 1/0/1", GI1),
        ("show x | section description", [" description uplink", " description printer"]),
        ("show x | include nothing-matches", []),
    ],
)
def test_filters(command, expected):
    parts = split_command(command.replace("|^ |^$", ""))
    if "exclude" in command:  # the policy allows one pipe, so the alternation goes here
        parts = ShowCommand(parts.base, "exclude", "^interface|^ |^$")
    assert apply_filter(CONFIG, parts) == "".join(f"{line}\n" for line in expected)


def test_count():
    assert apply_filter(CONFIG, split_command("show x | count ^interface")) == "Count: 2\n"


@pytest.mark.parametrize(
    "command",
    [
        "show x | include (a+)+$",  # groups: catastrophic backtracking
        "show x | include a{1,99}",
        "show x | include a*b*c*d*e*",  # too many quantifiers
        "show x | include [",  # invalid
        "show x | include " + "a" * 201,
        "show x | include",  # needs a pattern
        "show x | redirect flash:x",
    ],
)
def test_bad_patterns_are_refused(command):
    with pytest.raises(FilterError):
        apply_filter(CONFIG, split_command(command))


def test_filter_never_sees_a_redacted_secret():
    redacted = "snmp-server community <redacted> RO\n"
    assert apply_filter(redacted, split_command("show x | include ^snmp-server community P")) == ""
