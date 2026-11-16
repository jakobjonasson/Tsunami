"""Firmware upgrades: the drivers behind an upgrade play's `firmware: stage` and
`firmware: activate` steps.

See design.md, Firmware upgrades; base.py for the driver interface.
"""

from __future__ import annotations

from tsunami.core.firmware.base import (
    DeviceGone,
    FirmwareDriver,
    FirmwareError,
    FirmwareJob,
    ImageFile,
    ImageState,
    same_version,
    version_key,
)
from tsunami.core.models.common import PLATFORM_FAMILY, Platform, PlatformFamily
from tsunami.core.models.play import TransferMethod

__all__ = [
    "TRANSFERS",
    "TAKES_VRF",
    "DEVICE_HASH",
    "DeviceGone",
    "FirmwareDriver",
    "FirmwareError",
    "FirmwareJob",
    "ImageFile",
    "ImageState",
    "available",
    "driver_for",
    "same_version",
    "version_key",
]

# What validation needs to know without importing a driver (docs/research/firmware.md,
# summary table). Each driver's own `transfers` and `device_hash` must say the same; a
# test compares them.
TRANSFERS: dict[PlatformFamily, frozenset[TransferMethod]] = {
    # IOS and IOS-XE take an SCP upload (`ip scp server enable`) and fetch over TFTP/HTTP.
    PlatformFamily.IOS: frozenset({TransferMethod.SCP, TransferMethod.TFTP, TransferMethod.HTTP}),
    # AOS-CX 6000 to 6300 have no documented way to take an upload, and no HTTP in `copy`.
    PlatformFamily.AOS_CX: frozenset({TransferMethod.TFTP}),
    # AOS-S takes an SCP upload (`ip ssh filetransfer`, which turns its TFTP client off).
    PlatformFamily.AOS_S: frozenset({TransferMethod.SCP, TransferMethod.TFTP}),
}
# Families whose copy command takes the play's `vrf`. IOS fetches through the interface
# its configuration names, so a vrf there would be ignored; validation refuses it.
TAKES_VRF: frozenset[PlatformFamily] = frozenset({PlatformFamily.AOS_CX})
DEVICE_HASH: dict[PlatformFamily, str | None] = {
    PlatformFamily.IOS: "md5",  # verify /md5
    PlatformFamily.AOS_CX: None,  # the switch checks the image's signature itself
    PlatformFamily.AOS_S: None,
}


def _family(family: PlatformFamily | Platform | str) -> PlatformFamily:
    try:
        return PlatformFamily(family)
    except ValueError:
        return PLATFORM_FAMILY[Platform(family)]


def driver_for(family: PlatformFamily | Platform | str) -> FirmwareDriver:
    """The firmware driver for a platform family. Imported on use. A family whose driver
    is not built yet raises FirmwareError; validation refuses such a play first."""
    fam = _family(family)
    try:
        if fam is PlatformFamily.IOS:
            from tsunami.core.firmware.ios import DRIVER
        elif fam is PlatformFamily.AOS_CX:
            from tsunami.core.firmware.aoscx import DRIVER
        else:
            from tsunami.core.firmware.aoss import DRIVER
    except ImportError:
        raise FirmwareError(f"firmware upgrades are not available for {fam} yet") from None
    return DRIVER


def available(family: PlatformFamily | Platform | str) -> bool:
    """Whether this family has a firmware driver."""
    try:
        driver_for(family)
    except FirmwareError:
        return False
    return True
