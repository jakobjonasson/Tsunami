"""Trust host keys in bulk: record the SSH host key of many devices without logging in.

For onboarding a customer with hundreds of devices: each device's key is read with the
same path a run uses (direct, or through the device's jump host, whose login is the only
one made) and checked against the customer's known_hosts with trust on first use.

- A new key is recorded.
- A key that matches the one on file is left alone.
- A key that differs from the one on file is NEVER replaced here. It is reported with
  both fingerprints; replacing it is a separate, explicit accept_changed() that the GUI
  asks the person to confirm.
- The jump host's key is kept in the same file. Each jump host is reported first, as a
  JUMP_HOST result: recorded, known, unreachable, or changed. A changed key can be
  accepted the same way as a device's; until then every device behind it is unreachable.

No device login is ever sent: probe_host_key stops after the key exchange.

See design.md, Local data: known_hosts.
"""

from __future__ import annotations

import socket
import threading
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

import paramiko

from tsunami.core.paths import AppPaths
from tsunami.core.transports.base import (
    ConnectError,
    ConnectTarget,
    HostKeyMismatchError,
    JumpTarget,
)
from tsunami.core.transports.hostkeys import HostKeyStore, fingerprint, probe_host_key

MAX_WORKERS = 10  # matches the jump host channel cap (OpenSSH MaxSessions defaults to 10)
JUMP_HOST = "(jump host)"  # the device name of the jump host's result; never a device slug


class Outcome(StrEnum):
    RECORDED = "recorded"  # new key, now on file
    KNOWN = "known"  # same key as on file
    CHANGED = "changed"  # differs from the key on file; not replaced
    UNREACHABLE = "unreachable"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class DeviceAddress:
    name: str
    host: str
    port: int = 22


@dataclass(frozen=True)
class KeyProbe:
    device: str
    host: str
    port: int
    outcome: Outcome
    fingerprint: str | None = None  # what the device presented
    known_fingerprint: str | None = None  # what is on file (changed keys)
    detail: str = ""
    key: paramiko.PKey | None = field(default=None, repr=False, compare=False)


def trust_host_keys(
    paths: AppPaths,
    customer: str,
    devices: Sequence[DeviceAddress],
    *,
    jump: JumpTarget | None = None,
    jumps: Mapping[str, JumpTarget] | None = None,
    connect_timeout: float = 10.0,
    legacy: bool = True,
    workers: int = MAX_WORKERS,
    cancel: threading.Event | None = None,
    on_result: Callable[[KeyProbe], None] | None = None,
    probe: Callable[..., paramiko.PKey] = probe_host_key,
    jump_factory: Callable[[ConnectTarget, HostKeyStore], Any] | None = None,
) -> list[KeyProbe]:
    """Probe every device's host key and record new ones. Results come back in device
    order, after a JUMP_HOST result for each jump host; on_result is called as each
    finishes (from worker threads).

    jumps maps a device name to its own jump host (device, then site, then customer); a
    device not in it connects directly. jump is a jump host for every device without an
    entry in jumps. One connection is opened per distinct jump host (host and port)."""
    cancel = cancel or threading.Event()
    store = HostKeyStore(paths.known_hosts_file(customer))
    jumps = jumps or {}
    by_jump: dict[tuple[str, int] | None, list[DeviceAddress]] = {}
    targets: dict[tuple[str, int], JumpTarget] = {}
    for device in devices:
        chosen = jumps.get(device.name, jump)
        key = (str(chosen.host), chosen.port) if chosen is not None else None
        if chosen is not None:
            targets.setdefault(key, chosen)  # type: ignore[arg-type]
        by_jump.setdefault(key, []).append(device)

    connections: dict[tuple[str, int] | None, Any] = {None: None}
    jump_rows: list[KeyProbe] = []
    failed: dict[str, KeyProbe] = {}
    multiple = len(targets) > 1
    if targets:
        from tsunami.core.transports.ssh import JumpConnection
    for key, target_jump in targets.items():
        target = ConnectTarget(
            customer=customer,
            device=f"jump host {target_jump.host}",
            host=target_jump.host,
            port=target_jump.port,
            platform=_ANY_PLATFORM,
            method=_SSH,
            jump=target_jump,
            connect_timeout=connect_timeout,
            allow_new_host_key=True,  # the person started this from the GUI
        )
        make = jump_factory or (
            lambda t, s: JumpConnection(t, s, max_channels=workers, probe=probe, legacy=legacy)
        )
        name = f"{JUMP_HOST} {key[0]}:{key[1]}" if multiple else JUMP_HOST
        had_key = bool(store.known_keys(target_jump.host, target_jump.port))
        try:
            connections[key] = make(target, store)
        except ConnectError as exc:
            detail = str(exc)
            row = KeyProbe(
                name, target_jump.host, target_jump.port, Outcome.UNREACHABLE, detail=detail
            )
            if isinstance(exc, HostKeyMismatchError):
                # Probe the jump host itself so its changed key can be accepted like a
                # device's; until then nothing behind it can be reached.
                address = DeviceAddress(name, target_jump.host, target_jump.port)
                probed = _probe_one(store, address, None, connect_timeout, legacy, probe)
                if probed.outcome == Outcome.CHANGED:
                    row = probed
                    detail = "the jump host's key changed; accept it, then start again"
            jump_rows.append(row)
            for d in by_jump[key]:
                failed[d.name] = KeyProbe(
                    d.name, d.host, d.port, Outcome.UNREACHABLE, detail=detail
                )
        else:
            # The connection checked the jump host's key against the file (trust on first
            # use), so it is on file now: new unless it was there before.
            on_file = store.known_keys(target_jump.host, target_jump.port)
            jump_rows.append(
                KeyProbe(
                    name,
                    target_jump.host,
                    target_jump.port,
                    Outcome.KNOWN if had_key else Outcome.RECORDED,
                    fingerprint=fingerprint(next(iter(on_file.values()))) if on_file else None,
                    detail=f"jump host for {len(by_jump[key])} device(s)",
                )
            )
    jump_of = {d.name: key for key, group in by_jump.items() for d in group}

    def one(device: DeviceAddress) -> KeyProbe:
        if device.name in failed:
            return failed[device.name]
        if cancel.is_set():
            result = KeyProbe(device.name, device.host, device.port, Outcome.CANCELLED)
        else:
            result = _probe_one(
                store, device, connections[jump_of[device.name]], connect_timeout, legacy, probe
            )
        return result

    def reported(device: DeviceAddress) -> KeyProbe:
        result = one(device)
        if on_result is not None:
            on_result(result)
        return result

    for row in jump_rows:
        if on_result is not None:
            on_result(row)
    try:
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            return [*jump_rows, *pool.map(reported, devices)]
    finally:
        for connection in connections.values():
            if connection is not None:
                connection.close()


def accept_changed(paths: AppPaths, customer: str, result: KeyProbe) -> None:
    """Replace the key on file with the one a probe found. Only after the person has
    seen both fingerprints and confirmed."""
    if result.outcome != Outcome.CHANGED or result.key is None:
        raise ValueError("only a changed key can be accepted")
    HostKeyStore(paths.known_hosts_file(customer)).replace(result.host, result.port, result.key)


def _probe_one(
    store: HostKeyStore,
    device: DeviceAddress,
    jump: Any,
    timeout: float,
    legacy: bool,
    probe: Callable[..., paramiko.PKey],
) -> KeyProbe:
    if jump is not None:
        jump.acquire()
    try:
        if jump is not None:
            opener = lambda: jump.channel(device.host, device.port)  # noqa: E731
        else:
            opener = lambda: socket.create_connection((device.host, device.port), timeout)  # noqa: E731
        try:
            key = probe(
                opener,
                timeout=timeout,
                key_types=store.known_types(device.host, device.port) or None,
                legacy=legacy,
            )
        except ConnectError as exc:
            return KeyProbe(
                device.name, device.host, device.port, Outcome.UNREACHABLE, detail=str(exc)
            )
        presented = fingerprint(key)
        try:
            recorded = store.check(device.host, device.port, key, allow_new=True)
        except HostKeyMismatchError:
            known = store.known_keys(device.host, device.port)
            old = known.get(key.get_name()) or next(iter(known.values()), None)
            return KeyProbe(
                device.name,
                device.host,
                device.port,
                Outcome.CHANGED,
                fingerprint=presented,
                known_fingerprint=fingerprint(old) if old is not None else None,
                detail="host key differs from the one on file; not replaced",
                key=key,
            )
        outcome = Outcome.RECORDED if recorded else Outcome.KNOWN
        return KeyProbe(device.name, device.host, device.port, outcome, fingerprint=presented)
    finally:
        if jump is not None:
            jump.release()


def _placeholders() -> tuple[Any, Any]:
    from tsunami.core.models.common import Platform
    from tsunami.core.models.play import ConnectionMethod

    return next(iter(Platform)), ConnectionMethod.SSH


_ANY_PLATFORM, _SSH = _placeholders()  # a jump host target needs some platform; unused
