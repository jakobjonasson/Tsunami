"""customer.yml, devices.yml and vars-file models (design.md, Inventory and variables).

Rules that need more than one file (a device's site and role exist, device names are
unique) are checked by the workspace loader, which knows every file's lines.
"""

from __future__ import annotations

import re
from typing import Annotated, Any

from pydantic import AfterValidator, Field, RootModel, field_validator

from tsunami.core.models.common import (
    SUPPORTED_PLATFORMS,
    Platform,
    Slug,
    StrictModel,
    UserVarName,
)

_HOST_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9.:_-]*")


def _host(value: str) -> str:
    if not _HOST_RE.fullmatch(value):
        raise ValueError("must be an IP address or DNS name")
    return value


Host = Annotated[str, AfterValidator(_host)]
Port = Annotated[int, Field(ge=1, le=65535)]
Vars = dict[UserVarName, Any]

# Built-in roles: the device types every workspace has, whether or not it has a
# roles/<role>.yml file (one adds variables to the role). Plays target them by slug.
DEVICE_TYPES: dict[str, str] = {
    "core": "Core switch",
    "distribution": "Distribution switch",
    "access": "Access switch",
    "wlc": "Wireless LAN controller",
    "ap": "Access point",
    "router": "Router",
}


class VarsFile(RootModel[Vars]):
    """inventory/global.yml, inventory/roles/<role>.yml, sites/<site>.yml."""


class JumpHost(StrictModel):
    host: Host
    port: Port = 22
    credential: Slug


class CustomerFile(StrictModel):
    name: str = Field(min_length=1)
    credential: Slug = Field(description="Login credential name in Credential Manager.")
    jump_host: JumpHost | None = None
    lab: bool = Field(
        False, description="Lab customer, e.g. CML. MCP policy decides what it may change."
    )
    vars: Vars = {}
    role_vars: dict[Slug, Vars] = {}
    site_credentials: dict[Slug, Slug] = Field(
        {}, description="Default login credential per site; a device's own credential wins."
    )


class Device(StrictModel):
    name: Slug
    host: Host
    port: Port = 22
    site: Slug
    role: Slug = Field(description="Device type: a built-in role or roles/<role>.yml.")
    platform: Platform
    model: Slug | None = Field(None, description="Hardware catalog id.")
    credential: Slug | None = Field(
        None, description="Login credential; default: the site's, then the customer's."
    )
    vars: Vars = {}

    @field_validator("platform")
    @classmethod
    def _has_driver(cls, value: Platform) -> Platform:
        if value not in SUPPORTED_PLATFORMS:
            raise ValueError(f"platform '{value}' has no driver")
        return value


class DevicesFile(RootModel[list[Device]]):
    """inventory/customers/<customer>/devices.yml."""
