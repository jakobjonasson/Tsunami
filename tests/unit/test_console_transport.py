"""Console transport tests: everything runs against fakes, no serial port is opened."""

from __future__ import annotations

import dataclasses
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tsunami.core.models.common import Platform, PlatformFamily
from tsunami.core.models.play import ConnectionMethod
from tsunami.core.secret_store import LoginCredential
from tsunami.core.transports import console as console_mod
from tsunami.core.transports.base import (
    AuthenticationError,
    ConnectCancelled,
    ConnectError,
    ConnectTarget,
    UnreachableError,
)
from tsunami.core.transports.console import (
    ConsoleTransport,
    DialogRule,
    available_ports,
    clean_console,
)
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
    with pytest.raises(AuthenticationError, match="asks for a username"):
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


# -- escape codes (ProCurve wraps its prompt in them) ---------------------------------

FIXTURES = Path(__file__).parents[1] / "fixtures" / "console"


class ChunkedSerial(FakeSerial):
    """Replays raw byte captures a few bytes per read, so escape sequences split across
    reads exactly as they do on a 9600-baud console."""

    def __init__(self, steps: list[tuple[bytes, str | None]], chunk: int = 5, **kwargs: Any):
        super().__init__([], **kwargs)
        self.blobs = steps
        self.chunk = chunk
        self.offset = 0
        self.answered = False

    def read(self, n: int = 1) -> bytes:
        if self.idx >= len(self.blobs):
            return b""
        blob, expected = self.blobs[self.idx]
        if self.offset < len(blob):
            part = blob[self.offset : self.offset + self.chunk]
            self.offset += len(part)
            if self.offset >= len(blob) and (expected is None or self.answered):
                self.idx, self.offset, self.answered = self.idx + 1, 0, False
            return part
        if self.answered:  # the answer came before the last bytes; move on
            self.idx, self.offset, self.answered = self.idx + 1, 0, False
            return self.read(n)
        return b""

    def write(self, data: bytes) -> None:
        text = data.decode()
        self.writes.append(text)
        if self.idx < len(self.blobs) and text == self.blobs[self.idx][1]:
            self.answered = True  # a device takes a key press even mid-output


def test_clean_console_turns_cursor_moves_into_line_breaks():
    raw = "Banner\x1b[24;1H\x1b[2K\x1b[?25hHP-2530-24G# \x1b[24;14H"
    assert clean_console(raw) == "Banner\nHP-2530-24G# \n"
    assert clean_console("a\x1b7b\x1b8c\x1b[1;24rd") == "abcd"  # ESC 7/8, scroll region


def test_clean_console_keeps_a_split_sequence_until_its_end_arrives():
    first = clean_console("prompt line\x1b[24;")
    assert first.endswith("\x1b[24;")  # not cleaned yet, not mangled
    assert clean_console(first + "1HHP-2530-24G# ") == "prompt line\nHP-2530-24G# "


def _replay_2530(cred):
    """A real 2530-48G (YA.16.11) boot captured with PuTTY: speed sense, then a login
    prompt wrapped in escape codes (ESC[24;1HUsername: ESC[?25h ESC[24;11H...)."""
    boot = (FIXTURES / "aos-s-2530-48g-boot.txt").read_bytes()
    login = (FIXTURES / "aos-s-2530-48g-login.txt").read_bytes()
    assert b"Press <Enter> twice" in boot and b"\x1b[24;1HUsername: " in login
    # After the username the switch asks for the password twice, then shows its prompt.
    # The wording is from a bench run's job log; the escape codes follow the capture's
    # style, and the prompt arrives in pieces ("Password" before ":"), as it did there.
    password = b"\r\n\x1b[24;1H\x1b[2KPassword", b": \x1b[?25h\x1b[24;11H"
    again = b"\r\n\x1b[24;1H\x1b[2KRe-enter the new password: \x1b[?25h\x1b[24;28H"
    prompt = b"\x1b[1;24r\x1b[24;1H\x1b[2K\x1b[24;1H\x1b[?25h\x1b[24;1HHP-2530-48G# \x1b[24;14H"
    ports: list[ChunkedSerial] = []

    def serial_factory(**k):
        steps = [(boot, "\r\r"), (login, "ops\r")]
        if cred is not None:
            # "Password" and ":" as separate reads, as on the bench: the rule, which waits
            # for the colon, must still answer once it arrives.
            steps += [(password[0], None), (password[1], PW + "\r")]
            steps += [(again, PW + "\r"), (prompt, None)]
        s = ChunkedSerial(steps, **k)
        ports.append(s)
        return s

    clock = Clock()
    conns: list[dict[str, Any]] = []
    transport = ConsoleTransport(
        serial_factory=serial_factory,
        netmiko_factory=lambda **k: conns.append(k) or SimpleNamespace(base_prompt="sw"),
        list_ports=lambda: ["COM3"],
        sleep=clock.sleep,
        clock=clock,
        wait_timeout=60,
    )
    target = ConnectTarget(
        customer="acme",
        device="sw1",
        host=None,
        port=0,
        platform=Platform.AOS_S,
        method=ConnectionMethod.SERIAL,
        credential=cred,
        serial_port="COM3",
    )
    return transport, target, ports, conns, clock


def test_2530_first_boot_creates_the_login_then_reaches_the_prompt() -> None:
    transport, target, ports, conns, clock = _replay_2530(CRED)
    transport.open(target)
    # Both Enters for speed sense, the username, the password twice; nothing else.
    assert ports[0].writes == ["\r\r", "ops\r", PW + "\r", PW + "\r"]
    assert transport.dialog_log == [
        "speed sense: sent '\\r'",
        "username: sent <username>",
        "password: sent <password>",
        "confirm password: sent <password>",
    ]
    assert PW not in repr(transport.dialog_log)
    assert conns and conns[0]["device_type"] == "hp_procurve_serial"
    assert clock.t < 30  # found the prompt, did not time out


def test_2530_login_prompt_without_a_credential_fails_at_once() -> None:
    transport, target, _ports, conns, clock = _replay_2530(None)
    with pytest.raises(AuthenticationError, match="asks for a username, but no login"):
        transport.open(target)
    assert not conns and clock.t < 30


def test_cancel_stops_the_console_wait() -> None:
    cancel = threading.Event()
    cancel.set()
    t, target, ports, _ = make(Platform.IOS, [("garbage", None)], wait_timeout=300)
    with pytest.raises(ConnectCancelled, match="cancelled while waiting for the console"):
        t.open(dataclasses.replace(target, cancel=cancel))
    assert ports[0].closed


def test_the_console_conversation_goes_to_the_job_log_without_the_password() -> None:
    transport, target, _ports, _conns, _clock = _replay_2530(CRED)
    lines: list[str] = []
    transport.open(dataclasses.replace(target, log=lines.append))
    text = "\n".join(lines)
    assert "console: COM3 at 9600 baud" in text
    assert "Press <Enter> twice to continue" in text  # what the switch showed
    assert "console: username: sent <username>" in text
    assert "console: confirm password: sent <password>" in text
    assert "console: ready at 'HP-2530-48G#'" in text
    assert "handing over to Netmiko" in text and "console: logged in" in text
    assert PW not in text and "\x1b" not in text


def test_a_failed_netmiko_login_logs_what_the_console_last_showed() -> None:
    from netmiko.exceptions import NetmikoAuthenticationException

    lines: list[str] = []

    def netmiko_factory(**k):
        k["session_log"].write(b"\x1b[24;1HUsername: ops\r\nPassword: \r\nInvalid password\r\n")
        raise NetmikoAuthenticationException("auth failed")

    t, target, _, _ = make(Platform.AOS_S, [("\nsw1# ", None)])
    t._netmiko_factory = netmiko_factory
    with pytest.raises(AuthenticationError, match="console login failed"):
        t.open(dataclasses.replace(target, log=lines.append))
    text = "\n".join(lines)
    assert "Netmiko login failed (NetmikoAuthenticationException); it saw:" in text
    assert "Invalid password" in text and "\x1b" not in text


def test_netmiko_recognises_the_2530_prompt_after_tsunami_logged_in() -> None:
    """Netmiko's own serial login, against a console showing the prompt the way a 2530
    does (cursor codes after the #). Stock HPProcurveSSH only strips those codes after
    login, so it gave up at the prompt; Tsunami's class strips them during login."""
    from netmiko.exceptions import NetmikoAuthenticationException
    from netmiko.hp.hp_procurve import HPProcurveSSH

    from tsunami.core.transports.netmiko_session import procurve_serial_class

    prompt = "\x1b[24;1H\x1b[2K\x1b[24;1H\x1b[?25h\x1b[24;1HHP-2530-48G# \x1b[24;14H"

    class Channel:
        def __init__(self) -> None:
            self.writes: list[str] = []

        def read_channel(self) -> str:
            return prompt if self.writes else ""

        def write_channel(self, data: str) -> None:
            self.writes.append(data)

    def login(cls):
        conn = cls(
            device_type="hp_procurve_serial",
            username="ops",
            password=PW,
            auto_connect=False,
            serial_settings={"port": "COM3"},
        )
        conn.channel = Channel()
        conn.remote_conn = SimpleNamespace(close=lambda: None)
        return conn.serial_login(max_loops=2)

    with pytest.raises(NetmikoAuthenticationException):
        login(HPProcurveSSH)  # what failed on the bench
    assert "HP-2530-48G#" in login(procurve_serial_class())
