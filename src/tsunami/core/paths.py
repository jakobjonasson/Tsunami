"""Machine-local locations (design.md, Architecture: Data locations; Local data and audit).

Paths come from %LOCALAPPDATA% and %APPDATA%, never the working directory, because the
MCP server can start with an undefined cwd. Tests point both variables at temp dirs.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from tsunami.core.issues import TsunamiError
from tsunami.core.models.common import require_slug

APP_DIR_NAME = "Tsunami"


@dataclass(frozen=True)
class AppPaths:
    machine: Path  # %LOCALAPPDATA%\Tsunami: history.db, backups, logs, known_hosts
    settings: Path  # %APPDATA%\Tsunami: settings.json, mcp-policy.yml

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> AppPaths:
        env = os.environ if environ is None else environ
        missing = [name for name in ("LOCALAPPDATA", "APPDATA") if not env.get(name)]
        if missing:
            raise TsunamiError(f"environment variable not set: {', '.join(missing)}")
        return cls(
            machine=Path(env["LOCALAPPDATA"]) / APP_DIR_NAME,
            settings=Path(env["APPDATA"]) / APP_DIR_NAME,
        )

    @property
    def history_db(self) -> Path:
        return self.machine / "history.db"

    @property
    def known_hosts_dir(self) -> Path:
        return self.machine / "known_hosts"

    def known_hosts_file(self, customer: str) -> Path:
        return self.known_hosts_dir / require_slug(customer)

    @property
    def backups_dir(self) -> Path:
        return self.machine / "backups"

    @property
    def logs_dir(self) -> Path:
        return self.machine / "logs"

    def session_log_dir(self, job_id: str) -> Path:
        return self.logs_dir / "sessions" / job_id

    @property
    def settings_file(self) -> Path:
        return self.settings / "settings.json"

    @property
    def policy_file(self) -> Path:
        return self.settings / "mcp-policy.yml"
