"""SHA-1 fallback for old devices (core/transports/legacy_ssh.py), against real handshakes.

A local Paramiko server stands in for an old switch that offers only SHA-1 algorithms,
and a default one for a modern device.
"""

from __future__ import annotations

import socket
import threading

import paramiko
import pytest

from tsunami.core.transports.hostkeys import probe_host_key
from tsunami.core.transports.legacy_ssh import (
    KexGroup1SHA1,
    LegacyRSAKey,
    LegacyTransport,
    offered_key_types,
    uses_sha1,
)


@pytest.fixture(scope="module")
def host_key() -> LegacyRSAKey:
    return LegacyRSAKey(key=paramiko.RSAKey.generate(2048).key)


def serve(transport_cls: type[paramiko.Transport], host_key, *, kex=None, keys=None):
    """Start an SSH server on one end of a socket pair; return the client's end."""
    server_sock, client_sock = socket.socketpair()

    def run() -> None:
        transport = transport_cls(server_sock)
        if kex:
            transport.get_security_options().kex = kex
        transport.add_server_key(host_key)
        if keys:
            transport.get_security_options().key_types = keys
        try:
            transport.start_server(server=paramiko.ServerInterface())
            transport.accept(timeout=5)
        except Exception:  # noqa: BLE001 - a failed client handshake ends the server too
            pass
        finally:
            transport.close()

    threading.Thread(target=run, daemon=True).start()
    return client_sock


OLD_SWITCH = {"kex": ["diffie-hellman-group14-sha1"], "keys": ["ssh-rsa"]}


def test_modern_client_cannot_reach_an_sha1_only_device(host_key):
    sock = serve(LegacyTransport, host_key, **OLD_SWITCH)
    client = paramiko.Transport(sock)
    with pytest.raises(paramiko.SSHException):
        client.start_client(timeout=5)
    client.close()


@pytest.mark.parametrize("kex", ["diffie-hellman-group14-sha1", "diffie-hellman-group1-sha1"])
def test_legacy_client_falls_back_to_sha1(host_key, kex):
    sock = serve(LegacyTransport, host_key, kex=[kex], keys=["ssh-rsa"])
    client = LegacyTransport(sock)
    client.start_client(timeout=5)
    try:
        assert client.agreed_kex == kex
        assert client.host_key_type == "ssh-rsa"
        assert client.get_remote_server_key().asbytes() == host_key.asbytes()
        assert uses_sha1(client)
    finally:
        client.close()


def test_legacy_client_still_prefers_modern_algorithms(host_key):
    sock = serve(paramiko.Transport, host_key)  # a current device
    client = LegacyTransport(sock)
    client.start_client(timeout=5)
    try:
        assert client.agreed_kex and "sha1" not in client.agreed_kex
        assert client.host_key_type in ("rsa-sha2-512", "rsa-sha2-256")
        assert not uses_sha1(client)
    finally:
        client.close()


def test_probe_reads_the_host_key_of_an_sha1_device(host_key):
    key = probe_host_key(lambda: serve(LegacyTransport, host_key, **OLD_SWITCH), timeout=5)
    assert key.asbytes() == host_key.asbytes()


def test_probe_without_legacy_refuses_an_sha1_device(host_key):
    from tsunami.core.transports.base import UnreachableError

    with pytest.raises(UnreachableError):
        probe_host_key(
            lambda: serve(LegacyTransport, host_key, **OLD_SWITCH), timeout=5, legacy=False
        )


def test_pinned_rsa_key_asks_for_sha2_before_sha1():
    transport = LegacyTransport(socket.socketpair()[0])
    try:
        assert offered_key_types(transport, ["ssh-rsa"]) == [
            "rsa-sha2-512",
            "rsa-sha2-256",
            "ssh-rsa",
        ]
        assert offered_key_types(transport, ["ssh-ed25519"]) == ["ssh-ed25519"]
    finally:
        transport.close()


def test_group1_prime_is_the_rfc_2409_safe_prime():
    p = KexGroup1SHA1.P
    assert p.bit_length() == 1024
    for n in (p, (p - 1) // 2):  # a typo would almost surely make one composite
        assert all(pow(a, n - 1, n) == 1 for a in (2, 3, 5, 7, 11, 13))


class _PasswordServer(paramiko.ServerInterface):
    def get_allowed_auths(self, username):
        return "password"

    def check_auth_password(self, username, password):
        ok = (username, password) == ("netadmin", "pw")
        return paramiko.AUTH_SUCCESSFUL if ok else paramiko.AUTH_FAILED


def test_ssh_client_logs_in_to_an_sha1_device_with_the_legacy_transport(host_key):
    """The path Netmiko takes: SSHClient.connect with transport_factory."""
    server_sock, client_sock = socket.socketpair()

    def run() -> None:
        transport = LegacyTransport(server_sock)
        transport.get_security_options().kex = ["diffie-hellman-group14-sha1"]
        transport.add_server_key(host_key)
        transport.get_security_options().key_types = ["ssh-rsa"]
        try:
            transport.start_server(server=_PasswordServer())
            transport.accept(timeout=5)
        except Exception:  # noqa: BLE001
            pass

    threading.Thread(target=run, daemon=True).start()
    client = paramiko.SSHClient()
    client.get_host_keys().add("old-switch", "ssh-rsa", host_key)
    client.set_missing_host_key_policy(paramiko.RejectPolicy())
    client.connect(
        "old-switch",
        username="netadmin",
        password="pw",
        sock=client_sock,
        allow_agent=False,
        look_for_keys=False,
        transport_factory=LegacyTransport,
    )
    try:
        transport = client.get_transport()
        assert transport.is_authenticated() and uses_sha1(transport)
    finally:
        client.close()


def test_netmiko_connection_class_passes_the_transport():
    from tsunami.core.transports.netmiko_session import connection_class

    cls = connection_class("cisco_ios", aoscx=False, transport_factory=LegacyTransport)
    conn = cls.__new__(cls)  # without connecting
    for name in ("host", "username", "password", "key_file", "pkey", "passphrase", "sock"):
        setattr(conn, name, None)
    conn.port, conn.use_keys, conn.allow_agent, conn.ssh_config_file = 22, False, False, None
    conn.disabled_algorithms, conn.conn_timeout = {}, 10
    conn.auth_timeout = conn.banner_timeout = 10
    assert conn._connect_params_dict()["transport_factory"] is LegacyTransport
