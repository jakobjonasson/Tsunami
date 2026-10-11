"""Per-customer known_hosts: trust on first use, key probe before login, mismatch handling.

One OpenSSH-format file per customer, because customers reuse private address ranges and
one shared file would match one customer's key against another customer's device.
Entries are keyed the way paramiko looks keys up: `host`, or `[host]:port` off port 22.

Before the first login to a host, the transport probes its key with an unauthenticated
paramiko transport and records it here, so credentials only ever go to a host whose key
is on file. A changed key fails the device; only the GUI can replace a key.

See design.md, Local data and audit: known_hosts.
"""

from __future__ import annotations

import base64
import hashlib
import os
import socket
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path

import paramiko

from tsunami.core.transports.base import HostKeyMismatchError, UnreachableError

_locks: dict[Path, threading.Lock] = {}
_locks_guard = threading.Lock()


def _lock_for(path: Path) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(path.resolve(), threading.Lock())


def entry_name(host: str, port: int) -> str:
    return host if port == 22 else f"[{host}]:{port}"


def fingerprint(key: paramiko.PKey) -> str:
    digest = hashlib.sha256(key.asbytes()).digest()
    return "SHA256:" + base64.b64encode(digest).decode().rstrip("=")


class HostKeyStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def known_types(self, host: str, port: int) -> list[str]:
        """Key types on file for the host, so the probe asks for one of those."""
        with _lock_for(self.path):
            entry = self._load().lookup(entry_name(host, port))
        return list(entry.keys()) if entry else []

    def export_host(self, host: str, port: int, path: Path) -> None:
        """Write just this host's keys to path, for Netmiko's alt_key_file.

        Netmiko reads its key file during login, outside our lock; giving it a private
        copy keeps parallel logins from reading the shared file while it is replaced.
        """
        name = entry_name(host, port)
        with _lock_for(self.path):
            single = paramiko.HostKeys()
            for key_type, key in (self._load().lookup(name) or {}).items():
                single.add(name, key_type, key)
        single.save(str(path))

    def load_into(self, keys: paramiko.HostKeys) -> None:
        """Load the customer's keys into a paramiko client's key set (jump hosts)."""
        with _lock_for(self.path):
            if self.path.is_file():
                keys.load(str(self.path))

    def check(self, host: str, port: int, key: paramiko.PKey, *, allow_new: bool = True) -> bool:
        """Verify a probed key. Records it on first use; returns True if it was recorded."""
        name = entry_name(host, port)
        with _lock_for(self.path):
            keys = self._load()
            known = keys.lookup(name)
            if known:
                old = known.get(key.get_name()) or next(iter(known.values()))
                if old.get_name() == key.get_name() and old == key:
                    return False
                raise HostKeyMismatchError(
                    f"{name}: host key changed (on file {fingerprint(old)}, "
                    f"device presented {fingerprint(key)}); accept it in the GUI if expected"
                )
            if not allow_new:
                raise HostKeyMismatchError(
                    f"{name}: no host key on file and new keys are not allowed"
                )
            keys.add(name, key.get_name(), key)
            self._save(keys)
            return True

    def replace(self, host: str, port: int, key: paramiko.PKey) -> None:
        """Accept a new key for a host (GUI only, after showing both fingerprints)."""
        name = entry_name(host, port)
        with _lock_for(self.path):
            keys = self._load()
            if name in keys:
                del keys[name]
            keys.add(name, key.get_name(), key)
            self._save(keys)

    def _load(self) -> paramiko.HostKeys:
        keys = paramiko.HostKeys()
        if self.path.is_file():
            keys.load(str(self.path))
        return keys

    def _save(self, keys: paramiko.HostKeys) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=self.path.parent)
        os.close(fd)
        try:
            keys.save(tmp)
            for attempt in range(20):
                try:
                    os.replace(tmp, self.path)
                    break
                except PermissionError:
                    # Windows refuses to replace a file another process has open
                    # (antivirus, an editor); it is brief, so retry.
                    if attempt == 19:
                        raise
                    time.sleep(0.05)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise


def probe_host_key(
    open_socket: Callable[[], socket.socket | paramiko.Channel],
    *,
    timeout: float,
    key_types: list[str] | None = None,
) -> paramiko.PKey:
    """The server's host key, read without authenticating."""
    try:
        sock = open_socket()
    except OSError as exc:
        raise UnreachableError(f"cannot connect: {type(exc).__name__}") from None
    transport = paramiko.Transport(sock)
    try:
        if key_types:
            options = transport.get_security_options()
            preferred = [t for t in options.key_types if t in key_types]
            if preferred:
                options.key_types = preferred
        transport.start_client(timeout=timeout)
        return transport.get_remote_server_key()
    except (paramiko.SSHException, OSError, EOFError) as exc:
        raise UnreachableError(f"SSH handshake failed: {type(exc).__name__}") from None
    finally:
        transport.close()
