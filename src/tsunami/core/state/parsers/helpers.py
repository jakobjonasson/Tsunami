"""Shared by the state table parsers."""

from __future__ import annotations

import re

# IOS show commands abbreviate interface names (Gi1/0/1); running-config, prompts and
# checks use the full name. Longest prefixes first, so Twe is not read as Tw.
_IOS_PREFIXES = (
    ("TwentyFiveGigE", ("TwentyFiveGigE", "Twe")),
    ("TwoGigabitEthernet", ("TwoGigabitEthernet", "Tw")),
    ("TenGigabitEthernet", ("TenGigabitEthernet", "TenGi", "Te")),
    ("FortyGigabitEthernet", ("FortyGigabitEthernet", "Fo")),
    ("FiveGigabitEthernet", ("FiveGigabitEthernet", "Fi")),
    ("HundredGigE", ("HundredGigE", "Hu")),
    ("AppGigabitEthernet", ("AppGigabitEthernet", "Ap")),
    ("GigabitEthernet", ("GigabitEthernet", "Gig", "Gi")),
    ("FastEthernet", ("FastEthernet", "Fas", "Fa")),
    ("Ethernet", ("Ethernet", "Eth", "Et")),
    ("Port-channel", ("Port-channel", "Po")),
    ("Loopback", ("Loopback", "Lo")),
    ("Tunnel", ("Tunnel", "Tu")),
    ("Vlan", ("Vlan", "Vl")),
    ("Serial", ("Serial", "Se")),
    ("Cellular", ("Cellular", "Ce")),
    ("Dialer", ("Dialer", "Di")),
    ("BDI", ("BDI", "BD")),
    ("Null", ("Null", "Nu")),
)
_IOS_NAME = re.compile(r"^([A-Za-z-]+?)(\d.*)$")


def long_interface(name: str) -> str:
    """An IOS interface name in its running-config form: Gi1/0/1 -> GigabitEthernet1/0/1.
    A name that is already long, or not recognized, comes back unchanged."""
    text = name.strip()
    match = _IOS_NAME.match(text)
    if match is None:
        return text
    prefix, rest = match.group(1), match.group(2)
    for full, short_forms in _IOS_PREFIXES:
        if prefix.lower() in (s.lower() for s in short_forms):
            return full + rest
    return text


def data_lines(output: str) -> list[str]:
    """Output lines without the blank ones and without trailing space."""
    return [line.rstrip() for line in output.split("\n") if line.strip()]
