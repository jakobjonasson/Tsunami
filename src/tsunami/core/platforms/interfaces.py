"""Interface-name validation per platform family.

See design.md, Play schema: prompts (interface values must match `show running-config`).
"""

from __future__ import annotations

import re

from tsunami.core.models.common import PlatformFamily

__all__ = ["InterfaceNameError", "validate_interface_name"]


class InterfaceNameError(ValueError):
    """The name is not a valid single interface name for the platform family."""


# [0-9] rather than \d: \d also matches non-ASCII digits.
_IOS_PHYSICAL = (
    "Ethernet|FastEthernet|GigabitEthernet|TwoGigabitEthernet|FiveGigabitEthernet|"
    "TenGigabitEthernet|TwentyFiveGigE|FortyGigabitEthernet|FiftyGigE|HundredGigE|"
    "TwoHundredGigE|FourHundredGigE|AppGigabitEthernet"
)
_IOS_RE = re.compile(
    rf"(?:{_IOS_PHYSICAL})[0-9]+(?:/[0-9]+){{0,3}}(?:\.[0-9]+)?"
    r"|Port-channel[0-9]+(?:\.[0-9]+)?"
    r"|(?:Vlan|Loopback|Tunnel)[0-9]+"
)
_CX_RE = re.compile(r"[0-9]+/[0-9]+/[0-9]+|lag [0-9]+|vlan [0-9]+|loopback [0-9]+|mgmt")
_CX_VLAN_RE = re.compile(r"vlan ([0-9]+)")
_S_RE = re.compile(r"[0-9]+|[A-Z][0-9]+|[0-9]+/[A-Z]?[0-9]+|Trk[0-9]+")


def validate_interface_name(family: PlatformFamily, name: str) -> str:
    """Return name if valid for the family (running-config form, single interface), else raise."""
    if not isinstance(name, str):
        raise InterfaceNameError("interface name must be text")
    if family == PlatformFamily.IOS:
        ok = _IOS_RE.fullmatch(name) is not None
    elif family == PlatformFamily.AOS_CX:
        ok = _CX_RE.fullmatch(name) is not None
        vlan = _CX_VLAN_RE.fullmatch(name)
        if ok and vlan and not 1 <= int(vlan.group(1)) <= 4094:
            raise InterfaceNameError("vlan interface must be 1 to 4094")
    elif family == PlatformFamily.AOS_S:
        ok = _S_RE.fullmatch(name) is not None
    else:
        raise InterfaceNameError(f"unsupported platform family: {family}")
    if not ok:
        raise InterfaceNameError(
            f"not a valid {family.value} interface name in running-config form: {name[:40]!r}"
        )
    return name
