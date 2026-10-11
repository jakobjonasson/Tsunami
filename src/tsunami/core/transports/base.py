"""Transport interface (design.md, Execution engine: Transports).

The pipeline reaches devices only through a Transport that opens Sessions, so batching,
canary, max-fail and rollback are tested against the mock transport before any real
device. Connection failures are typed, so the pipeline can map them to device statuses.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from tsunami.core.issues import TsunamiError
from tsunami.core.models.common import Platform
from tsunami.core.models.play import ConnectionMethod
from tsunami.core.secret_store import LoginCredential


class ConnectError(TsunamiError):
    """The device could not be reached or logged in to. Messages are redacted."""


class UnreachableError(ConnectError):
    pass


class HostKeyMismatchError(ConnectError):
    pass


class AuthenticationError(ConnectError):
    pass


@dataclass(frozen=True)
class JumpTarget:
    host: str
    port: int
    credential: LoginCredential = field(repr=False)


@dataclass(frozen=True)
class ConnectTarget:
    """Everything a transport needs to open one session."""

    customer: str
    device: str  # inventory name, or the hostname being staged over console
    host: str | None  # None over console
    port: int
    platform: Platform
    method: ConnectionMethod
    credential: LoginCredential | None = field(default=None, repr=False)
    jump: JumpTarget | None = None
    connect_timeout: float = 15
    command_timeout: float = 60
    serial_port: str | None = None
    baud: int | None = None
    allow_new_host_key: bool = True  # trust on first use; MCP jobs never replace a key


class Transport(Protocol):
    def open(self, target: ConnectTarget) -> Session:
        """Open a session in enable mode, or raise a ConnectError subclass."""
        ...


@dataclass(frozen=True)
class ConfigResult:
    output: str  # raw device output; redact before storing or showing
    failed_line: str | None = None  # first line the device rejected
    error: str | None = None  # transport-level error, already redacted

    @property
    def ok(self) -> bool:
        return self.failed_line is None and self.error is None


@dataclass(frozen=True)
class ExecResult:
    command: str
    output: str  # raw device output; redact before storing or showing


class Session(Protocol):
    """One open CLI session to one device, already in enable mode."""

    def send_config(
        self, lines: list[str], *, config_mode_command: str | None = None
    ) -> ConfigResult:
        """Send config-mode lines, stopping at the first error.

        config_mode_command overrides `configure terminal`, e.g. for the revert timer.
        """
        ...

    def send_exec(
        self,
        command: str,
        *,
        expect_prompt: str | None = None,
        answer: str | None = None,
        read_timeout: float | None = None,
    ) -> ExecResult:
        """Run one exec-mode command, answering a confirmation prompt if given."""
        ...

    def running_config(self) -> str: ...

    def close(self) -> None: ...
