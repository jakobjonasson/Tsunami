"""Eight-level variable merge with per-key provenance.

Layers merge lowest to highest: global, role, customer, customer role, site, device,
play, prompt. Dicts deep-merge; lists, scalars and null replace. Engine defaults (the
hostname defaults to the device name) sit below every layer, and built-ins (customer,
site, role, platform, host, inventory_name, facts) sit above them. The result records,
for every value down to nested dict keys, the level and file it came from.

See design.md, Inventory and variables: Precedence, Merge rules, Provenance.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path
from typing import Any

Path_ = tuple[str, ...]


class Level(IntEnum):
    DEFAULT = 0  # engine default, overridable (hostname)
    GLOBAL = 1
    ROLE = 2
    CUSTOMER = 3
    CUSTOMER_ROLE = 4
    SITE = 5
    DEVICE = 6
    PLAY = 7
    PROMPT = 8
    BUILTIN = 9  # set by the engine, never overridden

    @property
    def label(self) -> str:
        return _LABELS[self]


_LABELS = {
    Level.DEFAULT: "default",
    Level.GLOBAL: "global",
    Level.ROLE: "role",
    Level.CUSTOMER: "customer",
    Level.CUSTOMER_ROLE: "customer role",
    Level.SITE: "site",
    Level.DEVICE: "device",
    Level.PLAY: "play",
    Level.PROMPT: "prompt",
    Level.BUILTIN: "built-in",
}


@dataclass(frozen=True)
class Source:
    level: Level
    file: Path | None = None  # workspace-relative; None for prompts and engine values


@dataclass(frozen=True)
class Layer:
    level: Level
    values: Mapping[str, Any]
    file: Path | None = None


@dataclass(frozen=True)
class ResolvedVars:
    values: dict[str, Any]
    provenance: dict[Path_, Source] = field(default_factory=dict)

    def source_of(self, *path: str) -> Source | None:
        return self.provenance.get(tuple(path))


def resolve(
    layers: Sequence[Layer],
    *,
    defaults: Mapping[str, Any] | None = None,
    builtins: Mapping[str, Any] | None = None,
) -> ResolvedVars:
    """Merge layers (in precedence order) between engine defaults and built-ins."""
    levels = [layer.level for layer in layers]
    if levels != sorted(levels) or any(lvl in (Level.DEFAULT, Level.BUILTIN) for lvl in levels):
        raise ValueError("layers must be inventory/play/prompt levels in precedence order")

    values: dict[str, Any] = {}
    provenance: dict[Path_, Source] = {}
    ordered = [Layer(Level.DEFAULT, defaults or {}), *layers, Layer(Level.BUILTIN, builtins or {})]
    for layer in ordered:
        _merge(values, provenance, layer.values, Source(layer.level, layer.file), ())
    return ResolvedVars(values=values, provenance=provenance)


def _merge(
    target: dict[str, Any],
    provenance: dict[Path_, Source],
    incoming: Mapping[str, Any],
    source: Source,
    prefix: Path_,
) -> None:
    for key, value in incoming.items():
        path = (*prefix, key)
        if isinstance(value, Mapping) and isinstance(target.get(key), dict):
            provenance[path] = source  # most recent contributor to a merged dict
            _merge(target[key], provenance, value, source, path)
            continue
        for stale in [p for p in provenance if p[: len(path)] == path]:
            del provenance[stale]
        target[key] = copy.deepcopy(dict(value) if isinstance(value, Mapping) else value)
        _record(provenance, path, target[key], source)


def _record(provenance: dict[Path_, Source], path: Path_, value: Any, source: Source) -> None:
    provenance[path] = source
    if isinstance(value, dict):
        for key, child in value.items():
            _record(provenance, (*path, str(key)), child, source)
