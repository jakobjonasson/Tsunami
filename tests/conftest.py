"""Shared fixtures. Tests never touch the real Credential Manager or real app-data folders."""

from __future__ import annotations

import os

import keyring
import keyring.errors
import pytest
from keyring.backend import KeyringBackend

from tsunami.core.paths import AppPaths


class MemoryKeyring(KeyringBackend):
    """In-memory keyring backend. keyring does not ship one."""

    priority = 1  # type: ignore[assignment]

    def __init__(self) -> None:
        super().__init__()
        self.store: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self.store.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.store[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        try:
            del self.store[(service, username)]
        except KeyError:
            raise keyring.errors.PasswordDeleteError(username) from None


@pytest.fixture(autouse=True)
def memory_keyring() -> MemoryKeyring:
    previous = keyring.get_keyring()
    backend = MemoryKeyring()
    keyring.set_keyring(backend)
    yield backend
    keyring.set_keyring(previous)


@pytest.fixture(autouse=True)
def app_paths(tmp_path, monkeypatch) -> AppPaths:
    """Point %LOCALAPPDATA% and %APPDATA% at temp dirs for every test, subprocesses included."""
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localappdata"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    return AppPaths.from_env()


def pytest_collection_modifyitems(config, items) -> None:
    gates = {"cml": "TSUNAMI_CML", "serial": "TSUNAMI_SERIAL"}
    for item in items:
        for marker, env_var in gates.items():
            if marker in item.keywords and not os.environ.get(env_var):
                item.add_marker(pytest.mark.skip(reason=f"set {env_var}=1 to run"))
