"""SSH transport, host keys and the Netmiko session (design.md, Transports; known_hosts)."""

from __future__ import annotations

import threading
import time

import paramiko
import pytest
from netmiko.exceptions import (
    ConfigInvalidException,
    NetmikoAuthenticationException,
    NetmikoTimeoutException,
)

from tsunami.core.models.common import Platform, PlatformFamily
from tsunami.core.models.play import ConnectionMethod
from tsunami.core.secret_store import LoginCredential
from tsunami.core.transports.base import (
    AuthenticationError,
    ConnectTarget,
    HostKeyMismatchError,
    JumpTarget,
    UnreachableError,
)
from tsunami.core.transports.hostkeys import HostKeyStore, entry_name, fingerprint
from tsunami.core.transports.netmiko_session import NetmikoSession, close_contexts
from tsunami.core.transports.ssh import SshTransport

KEY_A = paramiko.ECDSAKey.generate()
KEY_B = paramiko.ECDSAKey.generate()
CRED = LoginCredential("netadmin", "Login-Pass-1234", enable="Enable-5678")


class FakeConn:
    def __init__(self, *, enabled=True, prompt="sa-as01#", fail_config=None):
        self.enabled = enabled
        self.prompt = prompt
        self.base_prompt = prompt.rstrip("#>")
        self.fail_config = fail_config
        self.sent: list = []
        self.enable_calls = 0
        self.exited = False
        self.disconnected = False

    def check_enable_mode(self):
        return self.enabled

    def enable(self):
        self.enable_calls += 1
        self.enabled = True

    def find_prompt(self):
        return self.prompt

    def send_config_set(self, commands, **kwargs):
        self.sent.append(("config", list(commands), kwargs))
        for cmd in commands:
            if self.fail_config and cmd == self.fail_config:
                raise ConfigInvalidException(
                    f"Invalid input detected at command: {cmd}, matched error: % Invalid input"
                )
        return "\n".join(commands)

    def send_command(self, command, **kwargs):
        self.sent.append(("exec", command, kwargs))
        if command.startswith("copy"):
            return "Destination filename [x]? "
        return "done"

    def exit_config_mode(self):
        self.exited = True

    def disconnect(self):
        self.disconnected = True


def target(customer="acme", host="203.0.113.11", port=22, platform=Platform.IOS_XE, **kw):
    return ConnectTarget(
        customer=customer,
        device="sa-as01",
        host=host,
        port=port,
        platform=platform,
        method=ConnectionMethod.SSH,
        credential=CRED,
        **kw,
    )


def transport(app_paths, *, key=KEY_A, conn=None, calls=None, **kw):
    calls = calls if calls is not None else []

    def handler(**kwargs):
        calls.append(kwargs)
        return conn or FakeConn()

    return SshTransport(app_paths, connect_handler=handler, probe=lambda *a, **k: key, **kw), calls


# -- host keys ---------------------------------------------------------------------------


def test_first_use_records_key_then_logs_in_strictly(app_paths):
    ssh, calls = transport(app_paths)
    session = ssh.open(target())
    store = HostKeyStore(app_paths.known_hosts_file("acme"))
    assert store.known_types("203.0.113.11", 22) == [KEY_A.get_name()]
    kwargs = calls[0]
    assert kwargs["ssh_strict"] is True and kwargs["alt_host_keys"] is True
    assert kwargs["system_host_keys"] is False
    assert kwargs["alt_key_file"] != str(app_paths.known_hosts_file("acme"))  # private copy
    assert (kwargs["username"], kwargs["password"], kwargs["secret"]) == (
        "netadmin",
        "Login-Pass-1234",
        "Enable-5678",
    )
    session.close()


def test_changed_key_fails_before_credentials_are_sent(app_paths):
    first, _ = transport(app_paths, key=KEY_A)
    first.open(target()).close()
    second, calls = transport(app_paths, key=KEY_B)
    with pytest.raises(HostKeyMismatchError, match="host key changed") as caught:
        second.open(target())
    assert calls == []  # never logged in
    assert fingerprint(KEY_A) in str(caught.value) and fingerprint(KEY_B) in str(caught.value)


def test_customers_have_separate_key_files(app_paths):
    one, _ = transport(app_paths, key=KEY_A)
    one.open(target(customer="acme")).close()
    two, calls = transport(app_paths, key=KEY_B)
    two.open(target(customer="globex")).close()  # same IP, different customer: no clash
    assert len(calls) == 1


def test_new_keys_can_be_refused(app_paths):
    ssh, calls = transport(app_paths)
    with pytest.raises(HostKeyMismatchError, match="not allowed"):
        ssh.open(target(allow_new_host_key=False))
    assert calls == []


def test_store_replace_and_port_naming(app_paths):
    store = HostKeyStore(app_paths.known_hosts_file("acme"))
    assert entry_name("203.0.113.11", 22) == "203.0.113.11"
    assert entry_name("203.0.113.11", 2222) == "[203.0.113.11]:2222"
    assert store.check("203.0.113.11", 2222, KEY_A) is True
    assert store.check("203.0.113.11", 2222, KEY_A) is False
    with pytest.raises(HostKeyMismatchError):
        store.check("203.0.113.11", 2222, KEY_B)
    store.replace("203.0.113.11", 2222, KEY_B)
    assert store.check("203.0.113.11", 2222, KEY_B) is False
    assert "[203.0.113.11]:2222" in app_paths.known_hosts_file("acme").read_text()


# -- login and privilege ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (NetmikoAuthenticationException("bad"), AuthenticationError),
        (NetmikoTimeoutException("slow"), UnreachableError),
        (paramiko.BadHostKeyException("h", KEY_B, KEY_A), HostKeyMismatchError),
    ],
)
def test_netmiko_errors_are_mapped(app_paths, exc, expected):
    def handler(**kwargs):
        raise exc

    ssh = SshTransport(app_paths, connect_handler=handler, probe=lambda *a, **k: KEY_A)
    with pytest.raises(expected) as caught:
        ssh.open(target())
    assert "Login-Pass-1234" not in str(caught.value)


def test_unprivileged_ios_login_uses_enable(app_paths):
    conn = FakeConn(enabled=False)
    ssh, _ = transport(app_paths, conn=conn)
    ssh.open(target())
    assert conn.enable_calls == 1


def test_unprivileged_ios_login_without_enable_secret_fails(app_paths):
    conn = FakeConn(enabled=False)
    ssh, _ = transport(app_paths, conn=conn)
    no_enable = ConnectTarget(
        customer="acme",
        device="sa-as01",
        host="203.0.113.11",
        port=22,
        platform=Platform.IOS,
        method=ConnectionMethod.SSH,
        credential=LoginCredential("u", "p"),
    )
    with pytest.raises(AuthenticationError, match="not privileged"):
        ssh.open(no_enable)
    assert conn.disconnected


def test_aos_cx_login_must_be_administrator(app_paths):
    ssh, _ = transport(app_paths, conn=FakeConn(prompt="sw-cx01>"))
    with pytest.raises(AuthenticationError, match="administrators"):
        ssh.open(target(platform=Platform.AOS_CX))


# -- jump hosts --------------------------------------------------------------------------------


class FakeJump:
    def __init__(self, limit):
        self._slots = threading.BoundedSemaphore(limit)
        self._lock = threading.Lock()
        self.active = 0
        self.peak = 0
        self.closed = False

    def acquire(self):
        self._slots.acquire()
        with self._lock:
            self.active += 1
            self.peak = max(self.peak, self.active)

    def release(self):
        with self._lock:
            self.active -= 1
        self._slots.release()

    def channel(self, host, port):
        return object()

    def close(self):
        self.closed = True


def test_jump_host_channels_are_capped_at_ten(app_paths):
    jump = FakeJump(10)
    ssh, calls = transport(app_paths, jump_factory=lambda t, s: jump)
    via = JumpTarget("198.51.100.5", 22, LoginCredential("jump", "Jump-Pass"))

    def one(i):
        session = ssh.open(target(host=f"203.0.113.{i + 1}", jump=via))
        time.sleep(0.02)
        session.close()

    threads = [threading.Thread(target=one, args=(i,)) for i in range(25)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert jump.peak == 10 and jump.active == 0
    assert all("sock" in c for c in calls)
    ssh.close()
    assert jump.closed


# -- the Netmiko session ------------------------------------------------------------------------


def test_rejected_line_is_reported_and_config_mode_left():
    conn = FakeConn(fail_config=" name Cam eras")
    session = NetmikoSession(conn, PlatformFamily.IOS, command_timeout=30)
    result = session.send_config(["vlan 30", " name Cam eras"])
    assert result.failed_line == " name Cam eras"
    assert conn.exited
    assert conn.sent[0][2]["error_pattern"]


def test_revert_timer_command_is_passed_through():
    conn = FakeConn()
    session = NetmikoSession(conn, PlatformFamily.IOS, command_timeout=30)
    session.send_config(["vlan 30"], config_mode_command="configure terminal revert timer 5")
    assert conn.sent[0][2]["config_mode_command"] == "configure terminal revert timer 5"


def test_exec_answers_confirmation_prompts():
    conn = FakeConn()
    session = NetmikoSession(conn, PlatformFamily.IOS, command_timeout=30)
    result = session.send_exec(
        "copy running-config flash:x.cfg", expect_prompt=r"Destination", answer=""
    )
    assert [s[1] for s in conn.sent] == ["copy running-config flash:x.cfg", ""]
    assert result.output.startswith("Destination")


def test_aos_s_contexts_are_closed_with_exit():
    assert close_contexts(["vlan 10", '   name "Users"', "   untagged 1-12", "ntp enable"]) == [
        "vlan 10",
        '   name "Users"',
        "   untagged 1-12",
        "   exit",
        "ntp enable",
    ]
    assert close_contexts(["interface 1", '   name "x"']) == [
        "interface 1",
        '   name "x"',
        "   exit",
    ]
    assert close_contexts(["vlan 10", '   name "a"', "   exit"]) == [
        "vlan 10",
        '   name "a"',
        "   exit",
    ]
