"""Config error detection (work package 3.5)."""

from __future__ import annotations

import re

import pytest

from tsunami.core.errordetect import error_pattern, find_error

ERRORS = {
    "ios": [
        "% Invalid input detected at '^' marker.",
        "% Incomplete command.",
        '% Ambiguous command: "sh"',
        "% Unknown command or computer name, or unable to find computer address",
        "% Access VLAN does not exist. Creating vlan 30",
        "  % Bad mask",
    ],
    "aos-cx": [
        "% Command incomplete.",
        "Invalid input: systemctl",
        "Error: Checkpoint auto timer is already ON",
        "Cannot execute command. Internal error.",
        "VLAN 99 doesn't exist",
        "  Invalid input: foo",
    ],
    "aos-s": [
        "Invalid input: 255.255.255.255",
        "Incomplete input: custom",
        "Module not present for port or invalid port: A25",
        "  Invalid input: x",
    ],
}


@pytest.mark.parametrize(("family", "line"), [(f, ln) for f, ls in ERRORS.items() for ln in ls])
def test_errors_caught(family, line):
    assert find_error(f"sw(config)#something\n{line}\nsw(config)#", family) == line.strip()
    assert re.search(error_pattern(family), line, re.M)


@pytest.mark.parametrize("family", ["ios", "aos-cx", "aos-s"])
def test_clean_output(family):
    assert find_error("sw(config)#vlan 30\nsw(config-vlan)#name x\n", family) is None
    assert find_error("", family) is None


def test_echoed_percent_not_error():
    out = "sw(config-if)#description 100% uplink\n description 100% uplink\n"
    for family in ("ios", "aos-cx", "aos-s"):
        assert find_error(out, family) is None
    assert find_error(" description 100%uplink", "ios") is None


def test_ios_percent_must_be_followed_by_space_or_word():
    assert find_error("%%%", "ios") is None
    assert find_error("%SYS-5-CONFIG_I: Configured", "ios") == "%SYS-5-CONFIG_I: Configured"


def test_ios_benign_suppression():
    benign = [r"% Access VLAN does not exist.*", r"%SYS-5-.*"]
    out = "% Access VLAN does not exist. Creating vlan 30\n%SYS-5-CONFIG_I: x\n"
    assert find_error(out, "ios", benign) is None
    assert find_error(out, "ios") is not None
    # Benign entries match the whole line only.
    assert find_error("% Bad mask 1", "ios", [r"% Bad mask"]) == "% Bad mask 1"
    assert find_error("% Bad mask", "ios", [r"% Bad mask"]) is None


@pytest.mark.parametrize(
    "line",
    [
        "% Invalid input detected at '^' marker.",
        "% Incomplete command.",
        '% Ambiguous command: "x"',
        "% Unknown command or computer name",
    ],
)
def test_ios_always_errors_beat_benign(line):
    assert find_error(line, "ios", [r"%.*"]) == line


def test_first_error_reported():
    out = "ok\n% Invalid input detected at '^' marker.\n% Incomplete command.\n"
    assert find_error(out, "ios") == "% Invalid input detected at '^' marker."


def test_pattern_works_like_netmiko():
    out = "sw(config)#vlan 30\r\n% Incomplete command.\r\nsw(config)#"
    for family in ("ios", "aos-cx"):
        assert re.search(error_pattern(family), out, re.M)
    assert not re.search(error_pattern("ios"), "sw(config)#vlan 30\nsw(config)#", re.M)
    assert re.search(error_pattern("aos-s"), "a\nInvalid input: x\nb", re.M)


def test_benign_with_crlf():
    assert find_error("%SYS-5-X: y\r\n", "ios", [r"%SYS-5-.*"]) is None


def test_platform_names_accepted():
    assert find_error("% Incomplete command.", "ios-xe") is not None
