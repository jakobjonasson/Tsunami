"""Shared types: slugs, variable names, platforms, built-in variables.

See design.md, Workspace layout: Rules; Inventory and variables.
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Annotated

from pydantic import StringConstraints

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
    AOS_CX = "aos-cx"  # hardware catalog only; no driver yet
    AOS_S = "aos-s"  # hardware catalog only; no driver yet


# Platforms with a driver. Devices and plays may use only these. Catalog entries for
# other platforms are shown disabled (design.md, Hardware catalog).
SUPPORTED_PLATFORMS = frozenset({Platform.IOS, Platform.IOS_XE})

NETMIKO_SSH_DEVICE_TYPE = {
    Platform.IOS: "cisco_ios",
    Platform.IOS_XE: "cisco_xe",
}
NETMIKO_SERIAL_DEVICE_TYPE = {
    Platform.IOS: "cisco_ios_serial",
    Platform.IOS_XE: "cisco_ios_serial",
}

# Set by the engine. Defining one in a vars file is a validation error.
BUILTIN_VARS = frozenset(
    {"inventory_name", "customer", "site", "role", "platform", "host", "facts"}
)
