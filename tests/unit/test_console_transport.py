"""Console transport tests: everything runs against fakes, no serial port is opened."""

from __future__ import annotations

import dataclasses
from types import SimpleNamespace
from typing import Any

import pytest

from tsunami.core.models.common import Platform, PlatformFamily
from tsunami.core.models.play import ConnectionMethod
from tsunami.core.secret_store import LoginCredential
from tsunami.core.transports import console as console_mod
from tsunami.core.transports.base import (
    AuthenticationError,
    ConnectError,
    ConnectTarget,
    UnreachableError,
)
from tsunami.core.transports.console import ConsoleTransport, DialogRule, available_ports
from tsunami.core.transports.netmiko_session import NetmikoSession

PW = "Sup3r-Secret-pw"
CRED = LoginCredential("ops", PW, enable="en-secret")


class FakeSerial:
    """Replays steps of (emit, expected_write); the next step starts once the write arrives."""

    def __init__(self, steps: list[tuple[str, str | None]], **kwargs: Any) -> None:
        self.steps = steps
        self.kwargs = kwargs
        self.idx = 0
        self.emitted = False
        self.writes: list[str] = []
        self.closed = False
        self.in_waiting = 0

    def read(self, n: int = 1) -> bytes:
        if self.idx < len(self.steps) and not self.emitted:
            emit, expected = self.steps[self.idx]
            if expected is None:
                self.idx += 1
            else:
                self.emitted = True
            return emit.encode()
        return b""

    def write(self, data: bytes) -> None:
        text = data.decode()
        self.writes.append(text)
        if self.idx < len(self.steps) and self.emitted and text == self.steps[self.idx][1]:
            self.idx += 1
            self.emitted = False

    def close(self) -> None:
        self.closed = True


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.t += s


def make(platform: Platform, steps, cred=CRED, baud=None, wait_timeout=60, **kw):
    clock = Clock()
    ports: list[FakeSerial] = []
    conns: list[dict[str, Any]] = []

    def serial_factory(**k):
        s = FakeSerial(steps, **k)
        ports.append(s)
        return s

    def netmiko_factory(**k):
        conns.append(k)
        return SimpleNamespace(base_prompt="sw")

    transport = ConsoleTransport(
        serial_factory=serial_factory,
        netmiko_factory=netmiko_factory,
        list_ports=lambda: ["COM3", "COM4"],
        sleep=clock.sleep,
        clock=clock,
        wait_timeout=wait_timeout,
        **kw,
    )
    target = ConnectTarget(
        customer="acme",
        device="sw1",
        host=None,
        port=0,
        platform=platform,
        method=ConnectionMethod.SERIAL,
        credential=cred,
        serial_port="COM3",
        baud=baud,
    )
    return transport, target, ports, conns


def test_ios_factory_boot() -> None:
    steps = [
        (
            "--- System Configuration Dialog ---\nWould you like to enter the initial "
            "configuration dialog? [yes/no]: ",
            "no\r",
        ),
        ("Would you like to terminate autoinstall? [yes]: ", "yes\r"),
        ("Press RETURN to get started!\n", "\r"),
        ("\nSwitch>", None),
    ]
    t, target, ports, conns = make(Platform.IOS, steps, cred=None)
    session = t.open(target)
    assert ports[0].writes == ["no\r", "yes\r", "\r"]
    assert ports[0].closed
    assert ports[0].kwargs == {"port": "COM3", "baudrate": 9600, "timeout": 1, "xonxoff": False}
    assert conns[0]["device_type"] == "cisco_ios_serial"
    assert conns[0]["username"] == "" and conns[0]["secret"] == ""
    assert isinstance(session, NetmikoSession)
    assert session.family == PlatformFamily.IOS


def cx_steps():
    return [
        ("boot log...\nswitch login: ", "admin\r"),
        ("Password: ", "\r"),
        ("Please configure the 'admin' user account password.\nEnter new password: ", PW + "\r"),
        ("\nConfirm new password: ", PW + "\r"),
        ("\nswitch# ", None),
    ]


def test_aoscx_forced_password() -> None:
    t, target, ports, conns = make(Platform.AOS_CX, cx_steps(), baud=115200)
    session = t.open(target)
    assert ports[0].writes == ["admin\r", "\r", PW + "\r", PW + "\r"]
    assert ports[0].kwargs["baudrate"] == 115200
    assert t.dialog_log
    assert PW not in repr(t.dialog_log)
    assert PW not in repr(session) and PW not in repr(target)
    assert conns[0]["device_type"] == "aruba_aoscx_serial"
    assert conns[0]["serial_settings"] == {"port": "COM3", "baudrate": 115200, "xonxoff": False}
    assert session.family == PlatformFamily.AOS_CX


def test_aoscx_without_credential() -> None:
    t, target, ports, _ = make(Platform.AOS_CX, cx_steps(), cred=None)
    with pytest.raises(AuthenticationError) as ei:
        t.open(target)
    assert "credential" in str(ei.value)
    assert ports[0].closed
    assert PW + "\r" not in ports[0].writes


def test_aoss_press_any_key_and_manager_password() -> None:
    steps = [
        ("Banner\nPress any key to continue", "\r"),
        ("\nNew Manager password: ", PW + "\r"),
        ("Retype Manager password: ", PW + "\r"),
        ("\nsw1# ", None),
    ]
    t, target, ports, conns = make(Platform.AOS_S, steps)
    session = t.open(target)
    assert ports[0].writes == ["\r", PW + "\r", PW + "\r"]
    assert ports[0].kwargs["xonxoff"] is True
    assert conns[0]["device_type"] == "hp_procurve_serial"
    assert conns[0]["serial_settings"]["xonxoff"] is True
    assert conns[0]["secret"] == "en-secret"
    assert session.family == PlatformFamily.AOS_S
    assert PW not in repr(t.dialog_log)


def test_login_prompt_ready_only_with_credential() -> None:
    steps = [("Username: ", None)]
    t, target, _, conns = make(Platform.IOS, steps)
    t.open(target)
    assert conns[0]["username"] == "ops"
    t2, target2, _, _ = make(Platform.IOS, steps, cred=None, wait_timeout=10)
    with pytest.raises(UnreachableError):
        t2.open(target2)


def test_extra_dialogs_take_precedence() -> None:
    steps = [("Press RETURN to get started", "custom\r"), ("Switch>", None)]
    extra = {PlatformFamily.IOS: [DialogRule(r"Press RETURN to get started", "custom")]}
    t, target, ports, _ = make(Platform.IOS, steps, dialogs=extra)
    t.open(target)
    assert ports[0].writes == ["custom\r"]


def test_timeout_names_port_without_real_waiting() -> None:
    t, target, ports, _ = make(Platform.IOS, [("garbage", None)], wait_timeout=30)
    with pytest.raises(UnreachableError, match=r"COM3: no console prompt after 30s"):
        t.open(target)
    assert ports[0].closed
    assert "\r" in ports[0].writes  # woke the console while waiting


@pytest.mark.parametrize(
    "mutate, exc",
    [
        ({"serial_port": None}, ConnectError),
        ({"serial_port": "COM9"}, UnreachableError),
        ({"method": ConnectionMethod.SSH}, ConnectError),
    ],
)
def test_validation(mutate, exc) -> None:
    t, target, ports, _ = make(Platform.IOS, [])
    with pytest.raises(exc):
        t.open(dataclasses.replace(target, **mutate))
    assert not ports


def test_netmiko_exceptions_mapped() -> None:
    from netmiko.exceptions import NetmikoAuthenticationException, NetmikoTimeoutException

    for raised, expected in [
        (NetmikoAuthenticationException(f"bad {PW}"), AuthenticationError),
        (NetmikoTimeoutException(f"slow {PW}"), UnreachableError),
        (ValueError(PW), UnreachableError),
    ]:
        t, target, _, _ = make(Platform.IOS, [("Switch>", None)])

        def boom(_raised=raised, **_k: Any) -> Any:
            raise _raised

        t._netmiko_factory = boom
        with pytest.raises(expected) as ei:
            t.open(target)
        assert PW not in str(ei.value)
        assert "COM3" in str(ei.value)


def test_serial_open_failure() -> None:
    t, target, _, _ = make(Platform.IOS, [])

    def boom(**_k: Any) -> Any:
        raise OSError(f"access denied {PW}")

    t._serial_factory = boom
    with pytest.raises(UnreachableError) as ei:
        t.open(target)
    assert PW not in str(ei.value)


def test_default_netmiko_factory_picks_class(monkeypatch: pytest.MonkeyPatch) -> None:
    import netmiko.hp.hp_procurve as hp

    seen: dict[str, Any] = {}

    class Fake:
        def __init__(self, **k: Any) -> None:
            seen.update(k)

    monkeypatch.setattr(console_mod, "aoscx_class", lambda serial=False: Fake)
    console_mod._default_netmiko_factory(device_type="aruba_aoscx_serial", username="x")
    assert seen["device_type"] == "aruba_aoscx_serial"
    monkeypatch.setattr(hp, "HPProcurveSSH", Fake)
    seen.clear()
    console_mod._default_netmiko_factory(device_type="hp_procurve_serial", username="y")
    assert seen["device_type"] == "hp_procurve_serial"


def test_available_ports(monkeypatch: pytest.MonkeyPatch) -> None:
    import serial.tools.list_ports as lp

    monkeypatch.setattr(
        lp, "comports", lambda: [SimpleNamespace(device="COM10"), SimpleNamespace(device="COM3")]
    )
    assert available_ports() == ["COM10", "COM3"]
