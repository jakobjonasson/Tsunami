"""Target resolution: sites, roles, devices and exclude, combined with AND/OR.

See design.md, Play schema: targets, and Run-time options (Limit).

Different keys combine with AND, items within one list with OR, and `exclude` is
applied last. An empty `targets` never means "all devices". Unknown slugs are errors
so typos do not silently shrink the target set. The run-time limit is applied after
`exclude`. Findings are reported together, not just the first.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from dataclasses import dataclass

from tsunami.core.issues import Finding, Severity
from tsunami.core.models.inventory import Device
from tsunami.core.models.play import Targets


@dataclass(frozen=True)
class TargetResult:
    devices: tuple[Device, ...]  # in the order of the input devices (devices.yml order)
    findings: tuple[Finding, ...]

    @property
    def ok(self) -> bool:
        return not any(f.severity is Severity.ERROR for f in self.findings)


def _error(code: str, message: str, loc: tuple[str | int, ...]) -> Finding:
    return Finding(Severity.ERROR, code, message, loc)


def resolve_targets(
    targets: Targets | None,
    devices: Sequence[Device],
    *,
    sites: Collection[str],
    roles: Collection[str],
    limit: Collection[str] | None = None,
) -> TargetResult:
    if targets is None or targets.empty:
        return TargetResult(
            (),
            (
                _error(
                    "targets.empty",
                    "targets is empty; an empty target list never means all devices",
                    ("targets",),
                ),
            ),
        )

    findings: list[Finding] = []
    device_names = {d.name for d in devices}
    checks = (
        ("sites", targets.sites, sites, "site"),
        ("roles", targets.roles, roles, "role"),
        ("devices", targets.devices, device_names, "device"),
        ("exclude", targets.exclude, device_names, "device"),
    )
    for key, values, known, kind in checks:
        for i, value in enumerate(values):
            if value not in known:
                findings.append(
                    _error(
                        f"targets.unknown_{kind}",
                        f"unknown {kind} '{value}' in targets.{key}",
                        ("targets", key, i),
                    )
                )

    want_sites = set(targets.sites)
    want_roles = set(targets.roles)
    want_devices = set(targets.devices)
    excluded = set(targets.exclude)
    resolved = [
        d
        for d in devices
        if (not want_sites or d.site in want_sites)
        and (not want_roles or d.role in want_roles)
        and (not want_devices or d.name in want_devices)
        and d.name not in excluded
    ]

    # An unknown slug already explains an empty match; do not pile a second error on it.
    if not resolved and not findings:
        findings.append(_error("targets.no_devices", "targets match no devices", ("targets",)))

    if limit is not None and resolved:
        wanted = set(limit)
        for name in sorted(wanted - {d.name for d in resolved}):
            findings.append(
                _error(
                    "targets.limit_not_in_targets",
                    f"limit names '{name}', which is not among the targeted devices",
                    (),
                )
            )
        resolved = [d for d in resolved if d.name in wanted]
        if not resolved:
            findings.append(_error("targets.no_devices", "limit leaves no devices", ()))

    return TargetResult(tuple(resolved), tuple(findings))
