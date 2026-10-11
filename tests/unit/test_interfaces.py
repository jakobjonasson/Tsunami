"""Interface-name validation (design.md, Play schema: prompts)."""

from __future__ import annotations

import pytest

from tsunami.core.models.common import PlatformFamily
from tsunami.core.platforms.interfaces import InterfaceNameError, validate_interface_name

IOS = PlatformFamily.IOS
CX = PlatformFamily.AOS_CX
S = PlatformFamily.AOS_S

VALID = [
    (IOS, "GigabitEthernet1/0/1"),
    (IOS, "GigabitEthernet0/1"),
    (IOS, "GigabitEthernet1"),
    (IOS, "GigabitEthernet1/0/1.100"),
    (IOS, "GigabitEthernet1/0/0/1"),
    (IOS, "Ethernet0/0"),
    (IOS, "FastEthernet0/1"),
    (IOS, "TwoGigabitEthernet1/0/1"),
    (IOS, "FiveGigabitEthernet1/0/1"),
    (IOS, "TenGigabitEthernet1/1/1"),
    (IOS, "TwentyFiveGigE1/0/1"),
    (IOS, "FortyGigabitEthernet1/1/1"),
    (IOS, "FiftyGigE1/0/1"),
    (IOS, "HundredGigE1/0/1"),
    (IOS, "TwoHundredGigE1/0/1"),
    (IOS, "FourHundredGigE1/0/1"),
    (IOS, "AppGigabitEthernet1/0/1"),
    (IOS, "Port-channel1"),
    (IOS, "Port-channel1.20"),
    (IOS, "Vlan10"),
    (IOS, "Loopback0"),
    (IOS, "Tunnel5"),
    (CX, "1/1/1"),
    (CX, "1/1/52"),
    (CX, "lag 1"),
    (CX, "vlan 1"),
    (CX, "vlan 4094"),
    (CX, "loopback 0"),
    (CX, "mgmt"),
    (S, "1"),
    (S, "24"),
    (S, "A1"),
    (S, "1/1"),
    (S, "2/A3"),
    (S, "Trk1"),
]

INVALID = [
    (IOS, "Gi1/0/1"),
    (IOS, "Te1/1/1"),
    (IOS, "Po1"),
    (IOS, "gigabitethernet1/0/1"),
    (IOS, "GigabitEthernet 1/0/1"),
    (IOS, "GigabitEthernet1/0/1-2"),
    (IOS, "GigabitEthernet1/0/1,GigabitEthernet1/0/2"),
    (IOS, "GigabitEthernet1/0/0/0/1"),
    (IOS, "GigabitEthernet"),
    (IOS, "GigabitEthernet1/0/1."),
    (IOS, "Vlan 10"),
    (IOS, "Vlan10.5"),
    (IOS, "Loopback1/0"),
    (IOS, "Port-channel1/2"),
    (IOS, "Port-channel 1"),
    (IOS, ""),
    (IOS, " GigabitEthernet1/0/1"),
    (IOS, "GigabitEthernet1/0/1\n"),
    (IOS, "GigabitEthernet1/0/1\nhostname x"),
    (IOS, "GigabitEthernet١/0/1"),
    (IOS, "1/1/1"),
    (IOS, "Trk1"),
    (CX, "1/1"),
    (CX, "1/1/1/1"),
    (CX, "1/1/1-3"),
    (CX, "1/1/1,1/1/2"),
    (CX, "vlan 0"),
    (CX, "vlan 4095"),
    (CX, "vlan  10"),
    (CX, "vlan10"),
    (CX, "lag1"),
    (CX, "Lag 1"),
    (CX, "lag 1 "),
    (CX, "mgmt0"),
    (CX, "GigabitEthernet1/0/1"),
    (CX, "Trk1"),
    (CX, ""),
    (CX, "1/1/1\n"),
    (S, "a1"),
    (S, "1-5"),
    (S, "1,2"),
    (S, "1/1/1"),
    (S, "A1-A3"),
    (S, "Trk"),
    (S, "trk1"),
    (S, "Trk 1"),
    (S, "AA1"),
    (S, "1/AA1"),
    (S, "lag 1"),
    (S, "GigabitEthernet1/0/1"),
    (S, ""),
    (S, "1\n"),
]


@pytest.mark.parametrize(("family", "name"), VALID)
def test_valid_names_returned_unchanged(family: PlatformFamily, name: str) -> None:
    assert validate_interface_name(family, name) == name


@pytest.mark.parametrize(("family", "name"), INVALID)
def test_invalid_names_rejected(family: PlatformFamily, name: str) -> None:
    with pytest.raises(InterfaceNameError):
        validate_interface_name(family, name)


def test_error_is_value_error() -> None:
    assert issubclass(InterfaceNameError, ValueError)


def test_non_string_rejected() -> None:
    with pytest.raises(InterfaceNameError):
        validate_interface_name(IOS, 1)  # type: ignore[arg-type]
