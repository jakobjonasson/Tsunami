"""Shared types: slugs, variable names, platforms, built-in variables.

See design.md, Workspace layout: Rules; Inventory and variables.
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, StringConstraints

from tsunami.core.issues import TsunamiError

SLUG_PATTERN = r"^[a-z0-9][a-z0-9-]*$"
VAR_NAME_PATTERN = r"^[a-z][a-z0-9_]*$"

Slug = Annotated[str, StringConstraints(pattern=SLUG_PATTERN)]
VarName = Annotated[str, StringConstraints(pattern=VAR_NAME_PATTERN)]

_SLUG_RE = re.compile(SLUG_PATTERN)


def require_slug(value: str) -> str:
    """Return value if it is a slug, else raise. Use before building a path from a slug."""
    if not _SLUG_RE.fullmatch(value):
        raise TsunamiError(f"not a valid slug: {value!r}")
    return value


class Platform(StrEnum):
    IOS = "ios"
    IOS_XE = "ios-xe"
    AOS_CX = "aos-cx"
    AOS_S = "aos-s"


class PlatformFamily(StrEnum):
    """Platforms that share one CLI. Customer and common snippets are written per family."""

    IOS = "ios"
    AOS_CX = "aos-cx"
    AOS_S = "aos-s"


PLATFORM_FAMILY = {
    Platform.IOS: PlatformFamily.IOS,
    Platform.IOS_XE: PlatformFamily.IOS,
    Platform.AOS_CX: PlatformFamily.AOS_CX,
    Platform.AOS_S: PlatformFamily.AOS_S,
}

# Platforms with a driver. Devices and plays may use only these. Catalog entries for
# other platforms are shown disabled (design.md, Hardware catalog).
SUPPORTED_PLATFORMS = frozenset(Platform)

NETMIKO_SSH_DEVICE_TYPE = {
    Platform.IOS: "cisco_ios",
    Platform.IOS_XE: "cisco_xe",
    Platform.AOS_CX: "aruba_aoscx",
    Platform.AOS_S: "hp_procurve",
}
# Netmiko 4.8 registers no serial device type for the Aruba platforms; their drivers
# run the Aruba classes over pyserial (design.md, Platform drivers).
NETMIKO_SERIAL_DEVICE_TYPE = {
    Platform.IOS: "cisco_ios_serial",
    Platform.IOS_XE: "cisco_ios_serial",
}

# Set by the engine. Defining one in a vars file is a validation error.
BUILTIN_VARS = frozenset(
    {"inventory_name", "customer", "site", "role", "platform", "host", "facts"}
)


class StrictModel(BaseModel):
    """Base for every file-format model: unknown keys are errors, instances are immutable."""

    model_config = ConfigDict(extra="forbid", frozen=True)


def _single_line(value: str) -> str:
    # A line break inside one config line would send extra lines to the device.
    if "\n" in value or "\r" in value:
        raise ValueError("must be a single line")
    return value


SingleLine = Annotated[str, AfterValidator(_single_line)]


def _not_builtin(name: str) -> str:
    if name in BUILTIN_VARS:
        raise ValueError(f"'{name}' is a built-in variable set by the engine")
    return name


# Variable names in vars files: the pattern, and never a built-in.
UserVarName = Annotated[VarName, AfterValidator(_not_builtin)]
