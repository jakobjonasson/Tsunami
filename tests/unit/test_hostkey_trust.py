"""Trusting host keys in bulk (core/hostkey_trust.py): record new, keep known, never
replace a changed key without accept_changed(), never log in to a device."""

from __future__ import annotations

import socket
import threading

import paramiko
import pytest

from tsunami.core.hostkey_trust import (
    DeviceAddress,
    Outcome,
    accept_changed,
    trust_host_keys,
)
from tsunami.core.secret_store import LoginCredential
from tsunami.core.transports.base import JumpTarget, UnreachableError
from tsunami.core.transports.hostkeys import HostKeyStore, fingerprint

KEY_A = paramiko.ECDSAKey.generate()
KEY_B = paramiko.ECDSAKey.generate()
DEVICES = [DeviceAddress(f"sw{i}", f"192.0.2.{i}") for i in range(1, 6)]


def fake_probe(keys: dict[str, paramiko.PKey], unreachable: set[str] = frozenset()):
    """A probe that returns a key per host; the opener is called like the real one."""

    def probe(opener, *, timeout, key_types=None, legacy=True):
        host = opener()
        if host in unreachable:
            raise UnreachableError(f"{host}: cannot connect: TimeoutError")
        return keys[host]

    return probe


def opener_hosts(monkeypatch):
    """Direct connections return the host name instead of opening a socket."""
    monkeypatch.setattr(socket, "create_connection", lambda addr, timeout: addr[0])


def test_records_new_keys_keeps_known_and_reports_unreachable(app_paths, monkeypatch):
    opener_hosts(monkeypatch)
    keys = {d.host: KEY_A for d in DEVICES}
    probe = fake_probe(keys, unreachable={"192.0.2.5"})
    first = trust_host_keys(app_paths, "acme", DEVICES, probe=probe)
    assert [r.outcome for r in first] == [Outcome.RECORDED] * 4 + [Outcome.UNREACHABLE]
    assert first[0].fingerprint == fingerprint(KEY_A)
    assert "cannot connect" in first[4].detail
    store = HostKeyStore(app_paths.known_hosts_file("acme"))
    assert store.known_types("192.0.2.1", 22) == [KEY_A.get_name()]
    again = trust_host_keys(app_paths, "acme", DEVICES[:4], probe=probe)
    assert {r.outcome for r in again} == {Outcome.KNOWN}


def test_a_changed_key_is_reported_with_both_fingerprints_and_never_replaced(
    app_paths, monkeypatch
):
    opener_hosts(monkeypatch)
    trust_host_keys(app_paths, "acme", DEVICES[:1], probe=fake_probe({"192.0.2.1": KEY_A}))
    [result] = trust_host_keys(
        app_paths, "acme", DEVICES[:1], probe=fake_probe({"192.0.2.1": KEY_B})
    )
    assert result.outcome == Outcome.CHANGED
    assert result.fingerprint == fingerprint(KEY_B)
    assert result.known_fingerprint == fingerprint(KEY_A)
    store = HostKeyStore(app_paths.known_hosts_file("acme"))
    assert store.known_keys("192.0.2.1", 22)[KEY_A.get_name()] == KEY_A  # untouched

    accept_changed(app_paths, "acme", result)  # the explicit, confirmed step
    assert store.known_keys("192.0.2.1", 22)[KEY_B.get_name()] == KEY_B
    [after] = trust_host_keys(
        app_paths, "acme", DEVICES[:1], probe=fake_probe({"192.0.2.1": KEY_B})
    )
    assert after.outcome == Outcome.KNOWN


def test_only_a_changed_key_can_be_accepted(app_paths, monkeypatch):
    opener_hosts(monkeypatch)
    [recorded] = trust_host_keys(
        app_paths, "acme", DEVICES[:1], probe=fake_probe({"192.0.2.1": KEY_A})
    )
    with pytest.raises(ValueError, match="only a changed key"):
        accept_changed(app_paths, "acme", recorded)


def test_keys_are_kept_per_customer(app_paths, monkeypatch):
    opener_hosts(monkeypatch)
    trust_host_keys(app_paths, "acme", DEVICES[:1], probe=fake_probe({"192.0.2.1": KEY_A}))
    [other] = trust_host_keys(
        app_paths, "beta", DEVICES[:1], probe=fake_probe({"192.0.2.1": KEY_B})
    )
    assert other.outcome == Outcome.RECORDED  # another customer's file


def test_results_stream_and_cancel_stops_the_rest(app_paths, monkeypatch):
    opener_hosts(monkeypatch)
    cancel = threading.Event()
    seen = []

    def on_result(result):
        seen.append(result.device)
        cancel.set()  # stop after the first

    results = trust_host_keys(
        app_paths,
        "acme",
        DEVICES,
        probe=fake_probe({d.host: KEY_A for d in DEVICES}),
        workers=1,
        cancel=cancel,
        on_result=on_result,
    )
    assert results[0].outcome == Outcome.RECORDED
    assert {r.outcome for r in results[1:]} == {Outcome.CANCELLED}
    assert len(seen) == len(DEVICES)  # every device reported, in order


class FakeJump:
    def __init__(self) -> None:
        self.channels: list[tuple[str, int]] = []
        self.active = 0
        self.peak = 0
        self.closed = False
        self._lock = threading.Lock()

    def acquire(self) -> None:
        with self._lock:
            self.active += 1
            self.peak = max(self.peak, self.active)

    def release(self) -> None:
        with self._lock:
            self.active -= 1

    def channel(self, host: str, port: int):
        self.channels.append((host, port))
        return host

    def close(self) -> None:
        self.closed = True


def test_devices_behind_a_jump_host_are_probed_through_it(app_paths):
    jump = FakeJump()
    made = []

    def factory(target, store):
        made.append(target)
        return jump

    login = LoginCredential("jumpuser", "Jump-Pass-1")
    results = trust_host_keys(
        app_paths,
        "acme",
        DEVICES,
        jump=JumpTarget("198.51.100.10", 22, login),
        probe=fake_probe({d.host: KEY_A for d in DEVICES}),
        jump_factory=factory,
    )
    assert {r.outcome for r in results} == {Outcome.RECORDED}
    assert sorted(h for h, _ in jump.channels) == sorted(d.host for d in DEVICES)
    assert jump.active == 0 and jump.closed
    assert made[0].jump.host == "198.51.100.10" and made[0].allow_new_host_key


def test_an_unreachable_jump_host_marks_every_device(app_paths):
    def factory(target, store):
        raise UnreachableError("jump host 198.51.100.10: TimeoutError")

    results = trust_host_keys(
        app_paths,
        "acme",
        DEVICES,
        jump=JumpTarget("198.51.100.10", 22, LoginCredential("u", "p")),
        probe=fake_probe({}),
        jump_factory=factory,
    )
    assert {r.outcome for r in results} == {Outcome.UNREACHABLE}
    assert "jump host" in results[0].detail


def test_the_real_probe_reads_the_key_and_never_logs_in(app_paths, monkeypatch):
    """Against a real SSH server on a socket pair: the key is recorded and the server
    never sees an authentication attempt."""
    server_key = paramiko.ECDSAKey.generate()
    attempts: list[str] = []

    class Server(paramiko.ServerInterface):
        def get_allowed_auths(self, username):
            attempts.append(f"asked:{username}")
            return "password"

        def check_auth_password(self, username, password):
            attempts.append(f"password:{username}")
            return paramiko.AUTH_FAILED

    server_sock, client_sock = socket.socketpair()

    def serve() -> None:
        transport = paramiko.Transport(server_sock)
        transport.add_server_key(server_key)
        try:
            transport.start_server(server=Server())
            transport.accept(timeout=3)
        except Exception:  # noqa: BLE001, S110 - the client hangs up after the key
            pass
        finally:
            transport.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    monkeypatch.setattr(socket, "create_connection", lambda addr, timeout: client_sock)
    [result] = trust_host_keys(app_paths, "acme", DEVICES[:1], connect_timeout=5)
    thread.join(timeout=10)
    assert result.outcome == Outcome.RECORDED
    assert result.fingerprint == fingerprint(server_key)
    assert attempts == []  # no username, no password ever reached the server


def test_the_known_hosts_lock_never_asks_the_file_system(monkeypatch, tmp_path):
    """Keyed by string normalization: path.resolve() could spell the same path
    differently while another thread created the folder, giving two locks, and about one
    run in a hundred with a fresh data folder lost a new key."""
    from pathlib import Path

    from tsunami.core.transports import hostkeys

    def no_resolve(self, strict=False):
        raise AssertionError("_lock_for must not call Path.resolve")

    monkeypatch.setattr(Path, "resolve", no_resolve)
    path = tmp_path / "not-yet" / "known_hosts" / "acme"
    lock = hostkeys._lock_for(path)
    path.parent.mkdir(parents=True)
    assert hostkeys._lock_for(path) is lock
    assert hostkeys._lock_for(Path(str(path).upper())) is lock  # Windows: case-insensitive
    assert hostkeys._lock_for(path.parent / "." / "acme") is lock


def test_parallel_recording_into_a_fresh_folder_keeps_every_key(app_paths, monkeypatch):
    opener_hosts(monkeypatch)
    devices = [DeviceAddress(f"sw{i}", f"192.0.2.{i}") for i in range(1, 21)]
    for n in range(10):
        customer = f"fresh{n}"
        probe = fake_probe({d.host: KEY_A for d in devices})
        trust_host_keys(app_paths, customer, devices, probe=probe)
        lines = app_paths.known_hosts_file(customer).read_text(encoding="utf-8").splitlines()
        assert len(lines) == len(devices)
