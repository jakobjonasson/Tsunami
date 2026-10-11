"""Platform drivers: everything that differs between platform families.

See design.md, Platform drivers.
"""

from __future__ import annotations

from tsunami.core.models.common import PLATFORM_FAMILY, Platform, PlatformFamily
from tsunami.core.platforms.base import PlatformDriver


def get_driver(which: Platform | PlatformFamily | str) -> PlatformDriver:
    """The driver for a platform or a family."""
    from tsunami.core.platforms import aoscx, aoss, ios

    drivers = {
        PlatformFamily.IOS: ios.DRIVER,
        PlatformFamily.AOS_CX: aoscx.DRIVER,
        PlatformFamily.AOS_S: aoss.DRIVER,
    }
    value = str(which)
    if value in {str(p) for p in Platform}:
        return drivers[PLATFORM_FAMILY[Platform(value)]]
    return drivers[PlatformFamily(value)]


def all_drivers() -> tuple[PlatformDriver, ...]:
    return tuple(get_driver(family) for family in PlatformFamily)
