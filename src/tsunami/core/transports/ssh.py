"""SSH transport: Netmiko over SSH, through the customer's jump host when one is set.

Order of operations for every device: probe the host key without authenticating, check
it against the customer's known_hosts (recording it on first use), and only then log in,
with Netmiko set to strict checking against that same file. Jump hosts use one paramiko
connection per run with a direct-tcpip channel per device, at most 10 at a time because
OpenSSH's MaxSessions defaults to 10.

See design.md, Execution engine: Transports; Local data and audit: known_hosts;
Platform drivers: Families and sessions.
"""

from __future__ import annotations

import logging
import os
import socket
import tempfile
import threading
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

import paramiko

from tsunami.core.models.common import NETMIKO_SSH_DEVICE_TYPE, PLATFORM_FAMILY, PlatformFamily
from tsunami.core.models.play import ConnectionMethod
from tsunami.core.paths import AppPaths
from tsunami.core.transports.base import (
    AuthenticationError,
    ConnectError,
    ConnectTarget,
    HostKeyMismatchError,
    UnreachableError,
)
from tsunami.core.transports.hostkeys import HostKeyStore, probe_host_key
from tsunami.core.transports.legacy_ssh import transport_class, uses_sha1
from tsunami.core.transports.netmiko_session import NetmikoSession, connection_class

MAX_JUMP_CHANNELS = 10
log = logging.getLogger(__name__)


def _note_legacy(conn: Any, device: str) -> None:
    """Log when a device could only be reached with SHA-1 algorithms."""
    try:
        transport = conn.remote_conn.get_transport()
    except AttributeError:
        return
    if transport is not None and uses_sha1(transport):
        log.info("%s: SSH uses SHA-1 algorithms (the device offers nothing newer)", device)


class JumpConnection:
    """One authenticated connection to a jump host, shared by every device in a run."""

    def __init__(
        self,
        target: ConnectTarget,
        store: HostKeyStore,
        *,
        max_channels: int = MAX_JUMP_CHANNELS,
        probe: Callable[..., paramiko.PKey] = probe_host_key,
        legacy: bool = True,
    ) -> None:
        jump = target.jump
        assert jump is not None
        key = probe(
            lambda: socket.create_connection((jump.host, jump.port), target.connect_timeout),
            timeout=target.connect_timeout,
            key_types=store.known_types(jump.host, jump.port) or None,
            legacy=legacy,
        )
        store.check(jump.host, jump.port, key, allow_new=target.allow_new_host_key)
        client = paramiko.SSHClient()
        store.load_into(client.get_host_keys())  # read-only; never written back
        client.set_missing_host_key_policy(paramiko.RejectPolicy())
        try:
            client.connect(
                jump.host,
                port=jump.port,
                username=jump.credential.username,
                password=jump.credential.password,
                timeout=target.connect_timeout,
                allow_agent=False,
                look_for_keys=False,
                transport_factory=transport_class(legacy),
            )
        except paramiko.BadHostKeyException:
            raise HostKeyMismatchError(f"jump host {jump.host}: host key changed") from None
        except paramiko.AuthenticationException:
            raise AuthenticationError(f"jump host {jump.host}: login failed") from None
        except (paramiko.SSHException, OSError) as exc:
            raise UnreachableError(f"jump host {jump.host}: {type(exc).__name__}") from None
        self.client = client
        self.timeout = target.connect_timeout
        self._slots = threading.BoundedSemaphore(max_channels)

    def acquire(self) -> None:
        self._slots.acquire()

    def release(self) -> None:
        self._slots.release()

    def channel(self, host: str, port: int) -> paramiko.Channel:
        transport = self.client.get_transport()
        if transport is None or not transport.is_active():
            raise UnreachableError("jump host connection closed")
        try:
            return transport.open_channel(
                "direct-tcpip", (host, port), ("127.0.0.1", 0), timeout=self.timeout
            )
        except (paramiko.SSHException, OSError) as exc:
            raise UnreachableError(f"via jump host: {type(exc).__name__}") from None

    def close(self) -> None:
        self.client.close()


class SshTransport:
    """Satisfies Transport for method ssh. Call close() at the end of a run."""

    def __init__(
        self,
        paths: AppPaths,
        *,
        benign: Mapping[PlatformFamily, Sequence[str]] | None = None,
        max_jump_channels: int = MAX_JUMP_CHANNELS,
        connect_handler: Callable[..., Any] | None = None,
        probe: Callable[..., paramiko.PKey] = probe_host_key,
        jump_factory: Callable[[ConnectTarget, HostKeyStore], Any] | None = None,
        legacy: bool = True,
    ) -> None:
        self.paths = paths
        self.benign = benign or {}
        self.max_jump_channels = max_jump_channels
        self.legacy = legacy  # offer SHA-1 algorithms last (legacy_ssh.py)
        self._connect_handler = connect_handler
        self._probe = probe
        self._jump_factory = jump_factory or (
            lambda target, store: JumpConnection(
                target, store, max_channels=max_jump_channels, probe=probe, legacy=legacy
            )
        )
        self._jumps: dict[tuple[str, str, int], Any] = {}
        self._lock = threading.Lock()

    def open(self, target: ConnectTarget) -> NetmikoSession:
        if target.method != ConnectionMethod.SSH or not target.host:
            raise ConnectError("the SSH transport needs method ssh and a host")
        family = PLATFORM_FAMILY[target.platform]
        store = HostKeyStore(self.paths.known_hosts_file(target.customer))
        jump = self._jump(target, store) if target.jump else None
        if jump is not None:
            jump.acquire()
        try:
            if jump is not None:
                opener = lambda: jump.channel(target.host, target.port)  # noqa: E731
            else:
                opener = lambda: socket.create_connection(  # noqa: E731
                    (target.host, target.port), target.connect_timeout
                )
            key = self._probe(
                opener,
                timeout=target.connect_timeout,
                key_types=store.known_types(target.host, target.port) or None,
                legacy=self.legacy,
            )
            store.check(target.host, target.port, key, allow_new=target.allow_new_host_key)
            conn = self._login(target, family, store, opener() if jump is not None else None)
        except BaseException:
            if jump is not None:
                jump.release()
            raise
        session = NetmikoSession(
            conn,
            family,
            command_timeout=target.command_timeout,
            benign=self.benign.get(family, ()),
            on_close=jump.release if jump is not None else None,
        )
        try:
            self._check_privilege(session, family, target)
        except BaseException:
            session.close()
            raise
        return session

    def close(self) -> None:
        with self._lock:
            jumps, self._jumps = list(self._jumps.values()), {}
        for jump in jumps:
            jump.close()

    # -- internals --------------------------------------------------------------------

    def _jump(self, target: ConnectTarget, store: HostKeyStore) -> Any:
        assert target.jump is not None
        key = (target.customer, target.jump.host, target.jump.port)
        with self._lock:
            if key not in self._jumps:
                self._jumps[key] = self._jump_factory(target, store)
            return self._jumps[key]

    def _login(
        self, target: ConnectTarget, family: PlatformFamily, store: HostKeyStore, sock: Any
    ) -> Any:
        from netmiko.exceptions import NetmikoAuthenticationException, NetmikoTimeoutException

        credential = target.credential
        if credential is None:
            raise AuthenticationError("no login credential for this customer")
        # A private copy of this host's key for Netmiko (see HostKeyStore.export_host).
        fd, key_file = tempfile.mkstemp(prefix="known_hosts.", dir=store.path.parent)
        os.close(fd)
        store.export_host(target.host, target.port, Path(key_file))
        kwargs: dict[str, Any] = {
            "device_type": NETMIKO_SSH_DEVICE_TYPE[target.platform],
            "host": target.host,
            "port": target.port,
            "username": credential.username,
            "password": credential.password,
            "secret": credential.enable or "",
            "conn_timeout": target.connect_timeout,
            "ssh_strict": True,
            "system_host_keys": False,
            "alt_host_keys": True,
            "alt_key_file": key_file,
            "fast_cli": False,
        }
        if sock is not None:
            kwargs["sock"] = sock
        handler = self._connect_handler
        if handler is None:
            handler = connection_class(
                kwargs["device_type"],
                aoscx=family == PlatformFamily.AOS_CX,
                transport_factory=transport_class(self.legacy),
            )
        try:
            conn = handler(**kwargs)
            _note_legacy(conn, target.device)
            return conn
        except NetmikoAuthenticationException:
            raise AuthenticationError(f"{target.device}: login failed") from None
        except NetmikoTimeoutException:
            raise UnreachableError(f"{target.device}: connection timed out") from None
        except paramiko.BadHostKeyException:
            raise HostKeyMismatchError(f"{target.device}: host key changed") from None
        except (paramiko.SSHException, OSError) as exc:
            raise ConnectError(f"{target.device}: {type(exc).__name__}") from None
        finally:
            Path(key_file).unlink(missing_ok=True)  # paramiko has read it by now

    @staticmethod
    def _check_privilege(session: NetmikoSession, family: PlatformFamily, target) -> None:
        conn = session.conn
        if family == PlatformFamily.AOS_CX:
            # Administrators land at #; anything else cannot configure the switch.
            if not conn.find_prompt().strip().endswith("#"):
                raise AuthenticationError(
                    f"{target.device}: the login must be in the AOS-CX administrators group"
                )
            return
        if family == PlatformFamily.IOS and not conn.check_enable_mode():
            if not (target.credential and target.credential.enable):
                raise AuthenticationError(
                    f"{target.device}: login is not privileged and no enable secret is stored"
                )
            conn.enable()
