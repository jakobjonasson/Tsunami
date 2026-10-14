"""Serial console transport with day-0 dialog handling.

See design.md, Execution engine: Transports (serial) and Platform drivers: Day-0 console,
and research/aruba-cli.md section 8. Netmiko's own serial login cannot answer the day-0
prompts (the AOS-CX forced admin password, the AOS-S first-login manager password, the
IOS initial-configuration dialog), so a raw pyserial phase clears them first. The port is
then closed and handed to Netmiko, which logs in (or finds the CLI already open).

Netmiko 4.8 registers no Aruba serial device types, but BaseConnection picks the serial
protocol from a `_serial` substring of device_type, so the classes are used directly.

Console output carries terminal escape codes (ProCurve wraps its prompt in them, and
moves the cursor with ESC[24;1H where other devices send a newline). The buffer is
cleaned after each read is joined onto it, so a sequence split across two reads is
cleaned once its end arrives; cursor positioning becomes a line break so a prompt is not
glued onto the previous line.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from tsunami.core.models.common import (
    NETMIKO_SERIAL_DEVICE_TYPE,
    PLATFORM_FAMILY,
    PlatformFamily,
)
from tsunami.core.models.play import ConnectionMethod
from tsunami.core.transports.base import (
    AuthenticationError,
    ConnectCancelled,
    ConnectError,
    ConnectTarget,
    UnreachableError,
)
from tsunami.core.transports.netmiko_session import NetmikoSession, aoscx_class

DEFAULT_BAUD = 9600
_WAKE_AFTER = 5.0  # seconds of silence before sending a bare Enter
_POLL = 0.1
_MAX_FIRINGS = 3  # per rule, so a rejected factory login falls through to Netmiko
_BUFFER_KEEP = 4000

# Answer markers; never sent literally.
CREDENTIAL_PASSWORD = "\x00credential-password"
REQUIRE_CREDENTIAL = "\x00require-credential"  # send nothing, but fail early without one


@dataclass(frozen=True)
class DialogRule:
    pattern: str  # regex searched in console output
    answer: str  # text to send ("" means just Enter)
    note: str = ""


DEFAULT_DIALOGS: dict[PlatformFamily, tuple[DialogRule, ...]] = {
    PlatformFamily.IOS: (
        DialogRule(
            r"Would you like to enter the initial configuration dialog\? \[yes/no\]:",
            "no",
            "initial configuration dialog",
        ),
        DialogRule(r"Would you like to terminate autoinstall\? \[yes\]:", "yes", "autoinstall"),
        DialogRule(r"Press RETURN to get started", "", "press RETURN"),
    ),
    PlatformFamily.AOS_CX: (
        DialogRule(r"login:\s*$", "admin", "factory user"),
        DialogRule(r"(?<!new )Password:\s*$", "", "empty factory password"),
        DialogRule(
            r"Please configure the 'admin' user account password",
            REQUIRE_CREDENTIAL,
            "forced password change",
        ),
        DialogRule(r"(?i)enter new password:\s*$", CREDENTIAL_PASSWORD, "new admin password"),
        DialogRule(r"(?i)confirm new password:\s*$", CREDENTIAL_PASSWORD, "confirm admin password"),
    ),
    PlatformFamily.AOS_S: (
        DialogRule(r"Press any key to continue", "", "press any key"),
        # Prompt text is unverified. The retype rule precedes the manager rule because
        # "Retype Manager password:" would also match the latter (ties go to rule order).
        DialogRule(
            r"(?i)(?:re-?type|confirm)\s+(?:new\s+)?(?:manager\s+)?password",
            CREDENTIAL_PASSWORD,
            "retype manager password",
        ),
        DialogRule(
            r"(?i)(new )?manager password", CREDENTIAL_PASSWORD, "first-login manager password"
        ),
    ),
}

_PROMPT_RE = re.compile(r"^(?=[^\n]*\w)[\w.\-/()]+[>#]$")
# Cursor positioning (ESC[row;colH or f) stands for a line break; other escape codes
# (CSI sequences such as ESC[2K, ESC[?25h, and two-byte codes such as ESC 7) are dropped.
_CURSOR_POS = re.compile(r"\x1b\[\d*(?:;\d*)?[Hf]")
_ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|[0-Z\\-~])")


def clean_console(text: str) -> str:
    """Console text without escape codes. An incomplete sequence at the end is kept, so
    it is cleaned once the rest arrives with the next read."""
    return _ANSI.sub("", _CURSOR_POS.sub("\n", text))


_LOGIN_RE = re.compile(r"(?i)(?:login|username):$")


def available_ports() -> list[str]:
    """COM port names, sorted (the GUI's port picker)."""
    from serial.tools import list_ports

    return sorted(p.device for p in list_ports.comports())


def _default_serial_factory(**kwargs: Any) -> Any:
    import serial

    return serial.Serial(**kwargs)


def _default_netmiko_factory(**kwargs: Any) -> Any:
    from netmiko import ConnectHandler

    device_type = kwargs["device_type"]
    if device_type == "aruba_aoscx_serial":
        return aoscx_class(serial=True)(**kwargs)
    if device_type == "hp_procurve_serial":
        from netmiko.hp.hp_procurve import HPProcurveSSH

        return HPProcurveSSH(**kwargs)
    return ConnectHandler(**kwargs)


class ConsoleTransport:
    """Satisfies Transport for method serial."""

    def __init__(
        self,
        *,
        dialogs: Mapping[PlatformFamily, Sequence[DialogRule]] | None = None,
        serial_factory: Callable[..., Any] | None = None,
        netmiko_factory: Callable[..., Any] | None = None,
        list_ports: Callable[[], list[str]] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        wait_timeout: float = 300.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._extra = {PlatformFamily(k): tuple(v) for k, v in (dialogs or {}).items()}
        self._serial_factory = serial_factory or _default_serial_factory
        self._netmiko_factory = netmiko_factory or _default_netmiko_factory
        self._list_ports = list_ports or available_ports
        self._sleep = sleep
        self._clock = clock
        self.wait_timeout = wait_timeout
        # Masked record of what was sent; never contains received text or any secret.
        self.dialog_log: list[str] = []

    def open(self, target: ConnectTarget) -> NetmikoSession:
        if target.method != ConnectionMethod.SERIAL:
            raise ConnectError(f"console transport cannot open method {target.method.value}")
        port = target.serial_port
        if not port:
            raise ConnectError("serial connection needs a serial port")
        if port not in self._list_ports():
            raise UnreachableError(f"{port}: serial port not found")
        family = PLATFORM_FAMILY[target.platform]
        baud = target.baud or DEFAULT_BAUD
        xonxoff = family == PlatformFamily.AOS_S

        self.dialog_log = []
        self._clear_dialogs(target, family, port, baud, xonxoff)

        cred = target.credential
        kwargs: dict[str, Any] = {
            "device_type": self._device_type(target),
            "serial_settings": {"port": port, "baudrate": baud, "xonxoff": xonxoff},
            "username": cred.username if cred else "",
            "password": cred.password if cred else "",
            "secret": (cred.enable if cred else None) or "",
            "fast_cli": False,
            "conn_timeout": target.connect_timeout,
        }
        try:
            conn = self._netmiko_factory(**kwargs)
        except Exception as exc:  # noqa: BLE001 - mapped below; messages carry the type only
            raise self._map_netmiko_error(exc, port) from None
        return NetmikoSession(conn, family, command_timeout=target.command_timeout)

    @staticmethod
    def _device_type(target: ConnectTarget) -> str:
        family = PLATFORM_FAMILY[target.platform]
        if family == PlatformFamily.AOS_CX:
            return "aruba_aoscx_serial"
        if family == PlatformFamily.AOS_S:
            return "hp_procurve_serial"
        return NETMIKO_SERIAL_DEVICE_TYPE[target.platform]

    @staticmethod
    def _map_netmiko_error(exc: Exception, port: str) -> ConnectError:
        from netmiko.exceptions import NetmikoAuthenticationException, NetmikoTimeoutException

        name = type(exc).__name__
        if isinstance(exc, NetmikoAuthenticationException):
            return AuthenticationError(f"{port}: console login failed ({name})")
        if isinstance(exc, NetmikoTimeoutException):
            return UnreachableError(f"{port}: console did not respond ({name})")
        if isinstance(exc, (OSError, ValueError)):
            return UnreachableError(f"{port}: serial port error ({name})")
        return ConnectError(f"{port}: console connection failed ({name})")

    def _rules(self, family: PlatformFamily) -> list[DialogRule]:
        return [*self._extra.get(family, ()), *DEFAULT_DIALOGS.get(family, ())]

    def _clear_dialogs(
        self, target: ConnectTarget, family: PlatformFamily, port: str, baud: int, xonxoff: bool
    ) -> None:
        cred = target.credential
        rules = self._rules(family)
        fired = [0] * len(rules)
        try:
            ser = self._serial_factory(port=port, baudrate=baud, timeout=1, xonxoff=xonxoff)
        except Exception as exc:  # noqa: BLE001
            raise UnreachableError(
                f"{port}: cannot open serial port ({type(exc).__name__})"
            ) from None
        try:
            buf = ""
            now = self._clock()
            deadline = now + self.wait_timeout
            last_activity = now
            while self._clock() < deadline:
                if target.cancel is not None and target.cancel.is_set():
                    raise ConnectCancelled(f"{port}: cancelled while waiting for the console")
                data = self._read(ser)
                if data:
                    buf = clean_console(buf + data)[-_BUFFER_KEEP:]
                    last_activity = self._clock()
                best: tuple[int, int] | None = None  # (match end, rule index)
                for i, rule in enumerate(rules):
                    if fired[i] >= _MAX_FIRINGS:
                        continue
                    m = re.search(rule.pattern, buf)
                    # Latest match wins; ties go to the earlier rule (extra rules first).
                    if m and (best is None or m.end() > best[0]):
                        best = (m.end(), i)
                if best is not None:
                    end, i = best
                    fired[i] += 1
                    self._answer(ser, rules[i], cred.password if cred else None, port)
                    buf = buf[end:]
                    last_activity = self._clock()
                    continue
                if self._ready(buf, cred is not None):
                    return
                if not data:
                    if self._clock() - last_activity >= _WAKE_AFTER:
                        ser.write(b"\r")
                        last_activity = self._clock()
                    self._sleep(_POLL)
            raise UnreachableError(f"{port}: no console prompt after {self.wait_timeout:.0f}s")
        finally:
            try:
                ser.close()
            except Exception:  # noqa: BLE001 - best effort; Netmiko reopens the port
                pass

    @staticmethod
    def _read(ser: Any) -> str:
        waiting = getattr(ser, "in_waiting", 0) or 0
        raw = ser.read(max(1, waiting))
        return raw.decode("utf-8", errors="replace") if raw else ""

    def _answer(self, ser: Any, rule: DialogRule, password: str | None, port: str) -> None:
        label = rule.note or rule.pattern
        if rule.answer in (CREDENTIAL_PASSWORD, REQUIRE_CREDENTIAL):
            if password is None:
                raise AuthenticationError(
                    f"{port}: the switch asks to set a password ({label}) "
                    "but no login credential was provided"
                )
            if rule.answer == REQUIRE_CREDENTIAL:
                self.dialog_log.append(f"{label}: credential present")
                return
            ser.write(password.encode("utf-8") + b"\r")
            self.dialog_log.append(f"{label}: sent <password>")
            return
        ser.write(rule.answer.encode("utf-8") + b"\r")
        self.dialog_log.append(f"{label}: sent {rule.answer!r}")

    @staticmethod
    def _ready(buf: str, has_credential: bool) -> bool:
        lines = [ln.strip() for ln in buf.splitlines() if ln.strip()]
        if not lines:
            return False
        last = lines[-1]
        if _PROMPT_RE.match(last):
            return True
        return has_credential and bool(_LOGIN_RE.search(last))
