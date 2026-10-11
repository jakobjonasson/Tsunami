"""Device transports. Everything goes through the Session protocol in base.py.

See design.md, Execution engine: Transports.
"""

from __future__ import annotations

from typing import Any

from tsunami.core.models.play import ConnectionMethod
from tsunami.core.models.settings import AppSettings
from tsunami.core.paths import AppPaths
from tsunami.core.transports.base import ConnectError, ConnectTarget


class DeviceTransport:
    """The production Transport: SSH or serial console, by the target's method."""

    def __init__(self, paths: AppPaths, settings: AppSettings | None = None) -> None:
        self.paths = paths
        self.settings = settings or AppSettings()
        self._ssh: Any = None
        self._console: Any = None

    def open(self, target: ConnectTarget) -> Any:
        if target.method == ConnectionMethod.SSH:
            if self._ssh is None:
                from tsunami.core.transports.ssh import SshTransport

                self._ssh = SshTransport(self.paths, benign=self.settings.benign_messages)
            return self._ssh.open(target)
        if target.method == ConnectionMethod.SERIAL:
            if self._console is None:
                from tsunami.core.transports.console import ConsoleTransport

                self._console = ConsoleTransport()
            return self._console.open(target)
        raise ConnectError(f"no transport for method {target.method}")

    def close(self) -> None:
        """End of run: close jump-host connections."""
        if self._ssh is not None:
            self._ssh.close()
