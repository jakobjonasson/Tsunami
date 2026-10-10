"""Transport interface (design.md, Execution engine: Transports).

The pipeline reaches devices only through Session, so batching, canary, max-fail and
rollback are tested against the mock transport before any real device. Phase 3
(work package 3.1) adds the connect factory.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


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
