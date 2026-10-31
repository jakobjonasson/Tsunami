"""mcp-policy.yml model.

See design.md, MCP interface: Policy file.
"""

from __future__ import annotations

from functools import cache
from importlib.resources import files
from typing import Annotated, Literal

from pydantic import AfterValidator, Field, field_validator
from ruamel.yaml import YAML

from tsunami.core.models.common import PlatformFamily, Slug, StrictModel

# Pipe modifiers that can write output to files or remote URLs. Never allowed.
FORBIDDEN_PIPES = frozenset({"redirect", "tee", "append"})


def has_control_chars(value: str) -> bool:
    # C0/C1 controls, DEL, and the Unicode line separators str.splitlines() honours.
    return any(ord(c) < 32 or 127 <= ord(c) <= 159 or c in "  " for c in value)


def _allowlist_entry(value: str) -> str:
    if not value.startswith("show "):
        raise ValueError("allowlist entries must start with 'show '")
    if "|" in value or ";" in value or has_control_chars(value):
        raise ValueError("allowlist entries may not contain '|', ';' or control characters")
    if value != " ".join(value.split()):
        raise ValueError("allowlist entries must use single spaces")
    return value


def _pipe_name(value: str) -> str:
    if not value or has_control_chars(value) or any(c.isspace() or c in "|;" for c in value):
        raise ValueError("a pipe modifier is a single word")
    if value.lower() in FORBIDDEN_PIPES:
        raise ValueError(f"pipe modifier '{value}' can write output elsewhere and is not allowed")
    return value


AllowEntry = Annotated[str, AfterValidator(_allowlist_entry)]
PipeName = Annotated[str, AfterValidator(_pipe_name)]


@cache
def _template() -> dict:
    text = files("tsunami.core.data").joinpath("default-mcp-policy.yml").read_text("utf-8")
    return YAML(typ="safe").load(text)


def default_show_allowlist() -> dict[PlatformFamily, list[str]]:
    return {PlatformFamily(k): list(v) for k, v in _template()["show_allowlist"].items()}


def default_pipes() -> dict[PlatformFamily, list[str]]:
    return {PlatformFamily(k): list(v) for k, v in _template()["pipes"].items()}


class McpPolicy(StrictModel):
    enabled: bool = False
    apply: Literal["never", "lab_only"] = "lab_only"
    lab_allow_drafts: bool = True
    # Claude's connections record a device's host key on first contact, as the GUI's runs
    # do. Off: a person makes first contact. A changed key is refused either way.
    trust_new_host_keys: bool = False
    # Claude may add customers, sites and devices to the inventory (never edit or delete).
    inventory_edits: bool = False
    customers_blocked: list[Slug] = []
    show_allowlist: dict[PlatformFamily, list[AllowEntry]] = Field(
        default_factory=default_show_allowlist
    )
    pipes: dict[PlatformFamily, list[PipeName]] = Field(default_factory=default_pipes)
    max_check_targets: int = Field(50, ge=1, le=200)
    rate_limit_per_minute: int = Field(30, ge=1, le=600)
    approval_expiry_minutes: int = Field(60, ge=1, le=1440)

    @field_validator("customers_blocked")
    @classmethod
    def _unique(cls, value: list[str]) -> list[str]:
        return list(dict.fromkeys(value))
