"""Hardware catalog loader and model for data/hardware.yml.

See design.md, Inventory and variables: Hardware catalog.
"""

from __future__ import annotations

import re
from functools import cache
from importlib.resources import files
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator, model_validator
from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

from tsunami.core.issues import TsunamiError
from tsunami.core.models.common import (
    NETMIKO_SSH_DEVICE_TYPE,
    PLATFORM_FAMILY,
    SUPPORTED_PLATFORMS,
    Platform,
    PlatformFamily,
    Slug,
)


class CatalogEntry(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: Slug
    vendor: Literal["cisco", "aruba"]
    label: str
    platform: Platform
    netmiko_device_type: str
    console_baud: Literal[9600, 19200, 38400, 57600, 115200]
    lifecycle: Literal["current", "end-of-sale"]
    eos_year: int | None
    model_pattern: str
    examples: list[str]

    @field_validator("model_pattern")
    @classmethod
    def _compiles(cls, value: str) -> str:
        try:
            re.compile(value)
        except re.error as exc:
            raise ValueError(f"model_pattern is not a valid regex: {exc}") from exc
        return value

    @field_validator("examples")
    @classmethod
    def _has_example(cls, value: list[str]) -> list[str]:
        if not value:
            raise ValueError("examples must have at least one entry")
        return value

    @model_validator(mode="after")
    def _device_type_matches_platform(self) -> CatalogEntry:
        if self.platform in SUPPORTED_PLATFORMS:
            expected = NETMIKO_SSH_DEVICE_TYPE[self.platform]
            if self.netmiko_device_type != expected:
                raise ValueError(
                    f"netmiko_device_type must be {expected!r} for platform "
                    f"{self.platform.value!r}, got {self.netmiko_device_type!r}"
                )
        return self

    @property
    def selectable(self) -> bool:
        return self.platform in SUPPORTED_PLATFORMS

    @property
    def platform_family(self) -> PlatformFamily:
        return PLATFORM_FAMILY[self.platform]

    def matches_model(self, reported_model: str) -> bool:
        return re.search(self.model_pattern, reported_model) is not None


class Catalog:
    """Immutable, in file order."""

    __slots__ = ("_by_id", "entries")

    entries: tuple[CatalogEntry, ...]

    def __init__(self, entries: tuple[CatalogEntry, ...]) -> None:
        seen: set[str] = set()
        for entry in entries:
            if entry.id in seen:
                raise TsunamiError(f"hardware catalog: duplicate id {entry.id!r}")
            seen.add(entry.id)
        object.__setattr__(self, "entries", tuple(entries))
        object.__setattr__(self, "_by_id", {e.id: e for e in entries})

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("Catalog is immutable")

    def get(self, entry_id: str) -> CatalogEntry:
        try:
            return self._by_id[entry_id]
        except KeyError:
            raise TsunamiError(f"unknown hardware model: {entry_id!r}") from None

    def ids(self) -> frozenset[str]:
        return frozenset(self._by_id)

    def selectable(self) -> tuple[CatalogEntry, ...]:
        return tuple(e for e in self.entries if e.selectable)

    def by_vendor(self) -> dict[str, tuple[CatalogEntry, ...]]:
        groups: dict[str, list[CatalogEntry]] = {}
        for entry in self.entries:
            groups.setdefault(entry.vendor, []).append(entry)
        return {vendor: tuple(group) for vendor, group in groups.items()}

    def match_reported_model(self, reported_model: str) -> tuple[CatalogEntry, ...]:
        return tuple(e for e in self.entries if e.matches_model(reported_model))


def parse_catalog(raw: Any) -> Catalog:
    """Validate parsed YAML. Raises TsunamiError naming the offending entry."""
    if not isinstance(raw, list):
        raise TsunamiError("hardware catalog: top level must be a list of entries")
    entries: list[CatalogEntry] = []
    for index, item in enumerate(raw):
        name = item.get("id") if isinstance(item, dict) else None
        where = f"entry {name!r}" if isinstance(name, str) else f"entry #{index}"
        try:
            entries.append(CatalogEntry.model_validate(item))
        except ValidationError as exc:
            problems = "; ".join(
                f"{'.'.join(str(p) for p in err['loc']) or '(entry)'}: {err['msg']}"
                for err in exc.errors()
            )
            raise TsunamiError(f"hardware catalog: {where}: {problems}") from exc
    return Catalog(tuple(entries))


@cache
def load_catalog() -> Catalog:
    """Load and validate data/hardware.yml from the package (works in a frozen build)."""
    text = files("tsunami.core.data").joinpath("hardware.yml").read_text(encoding="utf-8")
    try:
        raw = YAML(typ="safe").load(text)
    except YAMLError as exc:
        raise TsunamiError(f"hardware catalog: invalid YAML: {exc}") from exc
    return parse_catalog(raw)
