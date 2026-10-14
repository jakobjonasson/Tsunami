"""Session over a Netmiko connection, shared by the SSH and console transports.

Config is sent with Netmiko's error_pattern from errordetect, so sending stops at the
first rejected line and the line is reported. AOS-CX gets a subclass whose config-mode
check also recognizes sub-contexts such as (config-if)#; AOS-S contexts are closed with
`exit` when sending, because snippets omit it.

See design.md, Execution engine: Transports, Error detection; Platform drivers.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

from tsunami.core.errordetect import error_pattern
from tsunami.core.models.common import PlatformFamily
from tsunami.core.transports.base import ConfigResult, ExecResult

_FAILED_RE = re.compile(r"at command: (?P<cmd>.*), matched error: ", re.S)


class NetmikoSession:
    """Satisfies Session. `conn` is a connected Netmiko object (or a test double)."""

    def __init__(
        self,
        conn: Any,
        family: PlatformFamily,
        *,
        command_timeout: float,
        benign: Sequence[str] = (),
        on_close: Any = None,
    ) -> None:
        self.conn = conn
        self.family = PlatformFamily(family)
        self.timeout = command_timeout
        self.error_pattern = error_pattern(self.family, benign)
        self._on_close = on_close
        self._closed = False

    def send_config(
        self, lines: list[str], *, config_mode_command: str | None = None
    ) -> ConfigResult:
        from netmiko.exceptions import ConfigInvalidException

        commands = close_contexts(lines) if self.family == PlatformFamily.AOS_S else list(lines)
        kwargs: dict[str, Any] = {"error_pattern": self.error_pattern, "read_timeout": self.timeout}
        if config_mode_command:
            kwargs["config_mode_command"] = config_mode_command
        try:
            output = self.conn.send_config_set(commands, **kwargs)
        except ConfigInvalidException as exc:
            match = _FAILED_RE.search(str(exc))
            self._leave_config_mode()
            failed = match.group("cmd") if match else None
            return ConfigResult(output=str(exc), failed_line=failed or "(unknown line)")
        return ConfigResult(output=output)

    def send_exec(
        self,
        command: str,
        *,
        expect_prompt: str | None = None,
        answer: str | None = None,
        read_timeout: float | None = None,
    ) -> ExecResult:
        timeout = read_timeout or self.timeout
        if expect_prompt is None:
            return ExecResult(command, self.conn.send_command(command, read_timeout=timeout))
        prompt = self._prompt_pattern()
        output = self.conn.send_command(
            command,
            expect_string=f"(?:{expect_prompt})|(?:{prompt})",
            read_timeout=timeout,
            strip_prompt=False,
            strip_command=False,
        )
        if re.search(expect_prompt, output):
            output += self.conn.send_command(
                answer or "",
                expect_string=prompt,
                read_timeout=timeout,
                strip_prompt=False,
                strip_command=False,
            )
        return ExecResult(command, output)

    def running_config(self) -> str:
        return self.conn.send_command("show running-config", read_timeout=max(self.timeout, 120))

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self.conn.disconnect()
        finally:
            if self._on_close is not None:
                self._on_close()

    def _prompt_pattern(self) -> str:
        base = getattr(self.conn, "base_prompt", "") or ""
        return re.escape(base) + r"[^\n]*[#>]\s*$" if base else r"[#>]\s*$"

    def _leave_config_mode(self) -> None:
        try:
            self.conn.exit_config_mode()
        except Exception:  # noqa: BLE001 - best effort; the failure is already reported
            pass


def close_contexts(lines: Sequence[str]) -> list[str]:
    """Insert AOS-S `exit` lines where an indented context ends (snippets omit them)."""
    out: list[str] = []
    depths: list[int] = []  # indentation of each open context's children
    for line in lines:
        indent = len(line) - len(line.lstrip())
        stripped = line.strip()
        if stripped == "exit":
            if depths:
                depths.pop()
            out.append(line)
            continue
        while depths and indent < depths[-1]:
            depths.pop()
            out.append(" " * (depths[-1] if depths else 3) + "exit")
        if out and indent > _indent(out[-1]) and out[-1].strip() != "exit":
            depths.append(indent)
        out.append(line)
    while depths:
        depths.pop()
        out.append(" " * (depths[-1] if depths else 3) + "exit")
    return out


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip())


def connection_class(device_type: str, *, aoscx: bool, transport_factory: Any) -> type:
    """The Netmiko class for device_type, connecting through transport_factory (so the
    SHA-1 fallback in legacy_ssh.py reaches Netmiko's Paramiko client)."""
    from netmiko.ssh_dispatcher import CLASS_MAPPER

    base = aoscx_class() if aoscx else CLASS_MAPPER[device_type]

    class TsunamiConnection(base):
        def _connect_params_dict(self) -> dict[str, Any]:
            params = super()._connect_params_dict()
            params["transport_factory"] = transport_factory
            return params

    return TsunamiConnection


def aoscx_class(serial: bool = False) -> type:
    """ArubaCxSSH whose config-mode check also matches sub-contexts like (config-if)#.

    Netmiko 4.8 only matches the literal "(config)#", so after a command that leaves a
    sub-context it neither sends `end` nor recognizes config mode.
    """
    from netmiko.aruba.aruba_aoscx import ArubaCxSSH

    class TsunamiArubaCx(ArubaCxSSH):
        def check_config_mode(
            self, check_string: str = "(config", pattern: str = r"[>#]", force_regex: bool = False
        ) -> bool:
            return super().check_config_mode(check_string=check_string, pattern=pattern)

    return TsunamiArubaCx


def procurve_serial_class() -> type:
    """HPProcurveSSH for a console port, recognising the prompt during login.

    Netmiko 4.8 turns on VT100 stripping for ProCurve in session_preparation, which runs
    after login. Over a console the 2530 follows its prompt with cursor codes
    (HP-2530-48G# ESC[24;14H), so the login's prompt check (#, then end of line) never
    matched and Netmiko gave up at a prompt it was looking at. Strip from the start.
    """
    from netmiko.hp.hp_procurve import HPProcurveSSH

    class TsunamiProcurveSerial(HPProcurveSSH):
        def serial_login(self, *args: Any, **kwargs: Any) -> str:
            self.ansi_escape_codes = True
            return super().serial_login(*args, **kwargs)

    return TsunamiProcurveSerial
