"""Hardware catalog: the built-in models in data/hardware.yml, plus the workspace's own.

Custom models live in the workspace, in inventory/models.yml, so they travel with the
devices that name them. They are added in the GUI (Inventory, Hardware models) or by
Claude over MCP (add_model). A custom id never shadows a built-in one.

See design.md, Inventory and variables: Hardware catalog.
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from functools import cache
from importlib.resources import files
from pathlib import Path
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)
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

CUSTOM_MODELS = Path("inventory", "models.yml")
VENDOR_OF_FAMILY = {
    PlatformFamily.IOS: "cisco",
    PlatformFamily.AOS_CX: "aruba",
    PlatformFamily.AOS_S: "aruba",
}
# Console speed a platform's switches start at; an entry's console_baud overrides it.
DEFAULT_BAUD = {
    PlatformFamily.IOS: 9600,
    PlatformFamily.AOS_CX: 115200,
    PlatformFamily.AOS_S: 9600,
}
BAUD_RATES = (9600, 19200, 38400, 57600, 115200)
MAX_PATTERN = 200
MAX_EXAMPLES = 20
Baud = Literal[9600, 19200, 38400, 57600, 115200]


class CatalogEntry(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: Slug
    vendor: Literal["cisco", "aruba"]
    label: str
    platform: Platform
    netmiko_device_type: str
    console_baud: Baud
    lifecycle: Literal["current", "end-of-sale"]
    eos_year: int | None
    # None only for a custom model without a pattern: it never matches a reported model,
    # so discovery never picks it by itself.
    model_pattern: str | None
    examples: list[str]
    custom: bool = False  # from the workspace's inventory/models.yml

    @field_validator("model_pattern")
    @classmethod
    def _compiles(cls, value: str | None) -> str | None:
        return _check_pattern(value)

    @model_validator(mode="after")
    def _documented(self) -> CatalogEntry:
        # Built-in entries document their SKUs and how to recognise them; a custom model
        # may have neither yet.
        if not self.custom:
            if not self.examples:
                raise ValueError("examples must have at least one entry")
            if self.model_pattern is None:
                raise ValueError("model_pattern is required")
        return self

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
        if self.model_pattern is None:
            return False
        return re.search(self.model_pattern, reported_model) is not None


def _check_pattern(value: str | None) -> str | None:
    if value is None:
        return None
    if len(value) > MAX_PATTERN:
        raise ValueError(f"model_pattern is limited to {MAX_PATTERN} characters")
    try:
        re.compile(value)
    except re.error as exc:
        raise ValueError(f"model_pattern is not a valid regex: {exc}") from exc
    return value


def _one_line(value: str, what: str) -> str:
    if any(ord(c) < 32 or 127 <= ord(c) <= 159 or c in "  " for c in value):
        raise ValueError(f"{what} must be one line without control characters")
    return value


class CustomModel(BaseModel):
    """One entry of inventory/models.yml. Vendor and Netmiko type follow from the platform,
    and the console speed defaults to the platform's."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: Slug = Field(max_length=64)
    label: str = Field(min_length=1, max_length=100)
    platform: Platform
    model_pattern: str | None = Field(None, max_length=MAX_PATTERN)
    examples: list[str] = Field(default_factory=list, max_length=MAX_EXAMPLES)
    console_baud: Baud | None = None
    lifecycle: Literal["current", "end-of-sale"] = "current"
    eos_year: int | None = Field(None, ge=1980, le=2100)

    @field_validator("label")
    @classmethod
    def _label(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("label must not be blank")
        return _one_line(value, "label")

    @field_validator("model_pattern", mode="before")
    @classmethod
    def _empty_pattern(cls, value: Any) -> Any:
        return None if value == "" else value

    @field_validator("model_pattern")
    @classmethod
    def _compiles(cls, value: str | None) -> str | None:
        return _check_pattern(value if value is None else _one_line(value, "model_pattern"))

    @field_validator("examples")
    @classmethod
    def _examples(cls, value: list[str]) -> list[str]:
        for item in value:
            if not item.strip() or len(item) > 64:
                raise ValueError("each example is text of 1 to 64 characters")
            _one_line(item, "an example")
        return value

    def entry(self) -> CatalogEntry:
        family = PLATFORM_FAMILY[self.platform]
        return CatalogEntry(
            id=self.id,
            vendor=VENDOR_OF_FAMILY[family],  # type: ignore[arg-type]
            label=self.label,
            platform=self.platform,
            netmiko_device_type=NETMIKO_SSH_DEVICE_TYPE[self.platform],
            console_baud=self.console_baud or DEFAULT_BAUD[family],  # type: ignore[arg-type]
            lifecycle=self.lifecycle,
            eos_year=self.eos_year,
            model_pattern=self.model_pattern,
            examples=list(self.examples),
            custom=True,
        )


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

    def custom(self) -> tuple[CatalogEntry, ...]:
        return tuple(e for e in self.entries if e.custom)


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
def builtin_catalog() -> Catalog:
    """Load and validate data/hardware.yml from the package (works in a frozen build)."""
    text = files("tsunami.core.data").joinpath("hardware.yml").read_text(encoding="utf-8")
    try:
        raw = YAML(typ="safe").load(text)
    except YAMLError as exc:
        raise TsunamiError(f"hardware catalog: invalid YAML: {exc}") from exc
    return parse_catalog(raw)


@dataclass(frozen=True)
class CustomProblem:
    """An entry of inventory/models.yml that was left out, and why. index is None when the
    whole file is unusable."""

    index: int | None
    message: str


@dataclass(frozen=True)
class CustomModels:
    entries: tuple[CatalogEntry, ...]
    problems: tuple[CustomProblem, ...]


def model_error(exc: ValidationError) -> str:
    """A CustomModel validation error as one line."""
    return "; ".join(
        f"{'.'.join(str(p) for p in err['loc']) or '(entry)'}: "
        f"{err['msg'].removeprefix('Value error, ')}"
        for err in exc.errors(include_url=False, include_input=False, include_context=False)
    )


def parse_custom_models(raw: Any, builtin: Catalog | None = None) -> CustomModels:
    """The valid entries of a parsed models.yml, and a problem for each one left out."""
    builtin = builtin or builtin_catalog()
    if raw is None:
        return CustomModels((), ())
    if not isinstance(raw, list):
        return CustomModels((), (CustomProblem(None, "must be a list of models"),))
    entries: list[CatalogEntry] = []
    problems: list[CustomProblem] = []
    seen: set[str] = set()
    for index, item in enumerate(raw):
        try:
            model = CustomModel.model_validate(item)
        except ValidationError as exc:
            problems.append(CustomProblem(index, model_error(exc)))
            continue
        if model.id in builtin.ids():
            problems.append(CustomProblem(index, f"id {model.id!r} is a built-in model"))
        elif model.id in seen:
            problems.append(CustomProblem(index, f"id {model.id!r} is listed twice"))
        else:
            seen.add(model.id)
            entries.append(model.entry())
    return CustomModels(tuple(entries), tuple(problems))


_custom_cache: dict[str, tuple[bytes, CustomModels]] = {}
_custom_lock = threading.Lock()


def load_custom_models(root: Path | str) -> CustomModels:
    """The workspace's custom models. Parsed again only when the file's bytes change (a
    timestamp is not enough: Windows' clock ticks about every 15 ms)."""
    path = Path(root) / CUSTOM_MODELS
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return CustomModels((), ())
    except OSError as exc:
        return CustomModels((), (CustomProblem(None, f"cannot be read ({type(exc).__name__})"),))
    key = str(path)
    with _custom_lock:
        hit = _custom_cache.get(key)
        if hit is not None and hit[0] == data:
            return hit[1]
    try:
        raw = YAML(typ="safe").load(data.decode("utf-8-sig"))
    except (YAMLError, UnicodeDecodeError) as exc:
        problem = CustomProblem(None, f"is not valid YAML ({type(exc).__name__})")
        found = CustomModels((), (problem,))
    else:
        found = parse_custom_models(raw)
    with _custom_lock:
        _custom_cache[key] = (data, found)
    return found


def load_catalog(root: Path | str | None = None) -> Catalog:
    """The built-in catalog, plus the custom models of the workspace at root. Invalid
    custom entries are left out here; the workspace loader reports them."""
    builtin = builtin_catalog()
    if root is None:
        return builtin
    custom = load_custom_models(root).entries
    if not custom:
        return builtin
    return Catalog((*builtin.entries, *custom))
