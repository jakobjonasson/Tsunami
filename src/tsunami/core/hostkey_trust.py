"""Trust host keys in bulk: record the SSH host key of many devices without logging in.

For onboarding a customer with hundreds of devices: each device's key is read with the
same path a run uses (direct, or through the customer's jump host, which is the only
login made) and checked against the customer's known_hosts with trust on first use.

- A new key is recorded.
- A key that matches the one on file is left alone.
- A key that differs from the one on file is NEVER replaced here. It is reported with
  both fingerprints; replacing it is a separate, explicit accept_changed() that the GUI
  asks the person to confirm.
- The jump host's key is kept in the same file. If it changed, the jump host is reported
  first, as a JUMP_HOST result that can be accepted the same way, and every device
  behind it as unreachable.

No device login is ever sent: probe_host_key stops after the key exchange.

See design.md, Local data: known_hosts.
"""

from __future__ import annotations

import socket
import threading
from collections.abc import Callable, Sequence
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
    connect_timeout: float = 10.0,
    legacy: bool = True,
    workers: int = MAX_WORKERS,
    cancel: threading.Event | None = None,
    on_result: Callable[[KeyProbe], None] | None = None,
    probe: Callable[..., paramiko.PKey] = probe_host_key,
    jump_factory: Callable[[ConnectTarget, HostKeyStore], Any] | None = None,
) -> list[KeyProbe]:
    """Probe every device's host key and record new ones. Results come back in device
    order, after a JUMP_HOST result if the jump host's key changed; on_result is called
    as each finishes (from worker threads)."""
    cancel = cancel or threading.Event()
    store = HostKeyStore(paths.known_hosts_file(customer))
    connection = None
    if jump is not None:
        from tsunami.core.transports.ssh import JumpConnection

        target = ConnectTarget(
            customer=customer,
            device=f"jump host {jump.host}",
            host=jump.host,
            port=jump.port,
            platform=_ANY_PLATFORM,
            method=_SSH,
            jump=jump,
            connect_timeout=connect_timeout,
            allow_new_host_key=True,  # the person started this from the GUI
        )
        make = jump_factory or (
            lambda t, s: JumpConnection(t, s, max_channels=workers, probe=probe, legacy=legacy)
        )
        try:
            connection = make(target, store)
        except ConnectError as exc:
            results = []
            detail = str(exc)
            if isinstance(exc, HostKeyMismatchError):
                # Probe the jump host itself so its changed key can be accepted like a
                # device's; until then nothing behind it can be reached.
                address = DeviceAddress(JUMP_HOST, jump.host, jump.port)
                probed = _probe_one(store, address, None, connect_timeout, legacy, probe)
                if probed.outcome == Outcome.CHANGED:
                    results.append(probed)
                    detail = "the jump host's key changed; accept it, then start again"
            results += [
                KeyProbe(d.name, d.host, d.port, Outcome.UNREACHABLE, detail=detail)
                for d in devices
            ]
            for result in results:
                if on_result is not None:
                    on_result(result)
            return results

    def one(device: DeviceAddress) -> KeyProbe:
        if cancel.is_set():
            result = KeyProbe(device.name, device.host, device.port, Outcome.CANCELLED)
        else:
            result = _probe_one(store, device, connection, connect_timeout, legacy, probe)
        if on_result is not None:
            on_result(result)
        return result

    try:
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            return list(pool.map(one, devices))
    finally:
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
