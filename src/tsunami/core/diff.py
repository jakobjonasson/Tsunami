"""Missing and extra lines, and the compliance report.

Missing: rendered paths absent from the running config. Extra: owned top-level running
lines absent from the render. Lines carrying a secret are compared by the part before
the secret on both sides, since no platform stores a secret as the template wrote it.
Everything returned is redacted.

See design.md, Execution engine: Diff and compliance; Platform drivers: Secrets.
"""

from __future__ import annotations

import itertools
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from tsunami.core.configtree import ConfigNode, iter_paths, parse_config, secret_key
from tsunami.core.models.common import PlatformFamily
from tsunami.core.redact import Redactor

SEPARATOR = " > "


@dataclass(frozen=True)
class DiffResult:
    missing: tuple[str, ...]
    extra: tuple[str, ...]

    @property
    def clean(self) -> bool:
        return not self.missing and not self.extra


def _keys(path: tuple[str, ...], family: PlatformFamily) -> tuple[str, ...]:
    # Only the top-level ancestor matters to the secret rules (key chain key ids).
    return tuple(
        secret_key(text, family, path[0] if depth else None) for depth, text in enumerate(path)
    )


def _variants(key: str, original: str, family: PlatformFamily) -> tuple[str, ...]:
    """Keys a rendered element may appear as on the device.

    AOS-S hides credentials, so `radius-server host H key "x"` runs as `radius-server host H`.
    """
    if family is PlatformFamily.AOS_S and key != original and " " in key:
        return (key, key.rsplit(" ", 1)[0])
    return (key,)


def diff_config(
    rendered: str | Sequence[str],
    running: str,
    family: PlatformFamily | str,
    *,
    owns: Sequence[str] = (),
    redactor: Redactor | None = None,
) -> DiffResult:
    fam = family if isinstance(family, PlatformFamily) else PlatformFamily(str(family))
    redactor = redactor or Redactor()
    rendered_tree = parse_config(rendered, fam)
    running_tree = parse_config(running, fam)
    running_keys = {_keys(p, fam) for p in iter_paths(running_tree)}

    missing: list[str] = []
    for path in iter_paths(rendered_tree):
        keys = _keys(path, fam)
        options = [_variants(k, t, fam) for k, t in zip(keys, path, strict=True)]
        if any(candidate in running_keys for candidate in itertools.product(*options)):
            continue
        missing.append(redactor.line(SEPARATOR.join(keys)))

    rendered_top = {
        variant
        for node in rendered_tree
        for variant in _variants(secret_key(node.text, fam), node.text, fam)
    }
    prefixes = [p.strip() for p in owns if p.strip()]
    extra: list[str] = []
    for node in running_tree:
        if not any(_owned(node, p) for p in prefixes):
            continue
        key = secret_key(node.text, fam)
        if key not in rendered_top:
            extra.append(redactor.line(key))
    return DiffResult(tuple(dict.fromkeys(missing)), tuple(dict.fromkeys(extra)))


def _owned(node: ConfigNode, prefix: str) -> bool:
    prefix = " ".join(prefix.split())
    return node.text == prefix or node.text.startswith(prefix + " ")


def compliance_report(results: Mapping[str, DiffResult]) -> dict:
    devices = {
        name: {
            "missing_count": len(r.missing),
            "extra_count": len(r.extra),
            "missing": list(r.missing),
            "extra": list(r.extra),
        }
        for name, r in results.items()
    }
    return {
        "devices": devices,
        "total_devices": len(devices),
        "compliant_devices": sum(1 for r in results.values() if r.clean),
        "total_missing": sum(d["missing_count"] for d in devices.values()),
        "total_extra": sum(d["extra_count"] for d in devices.values()),
    }
