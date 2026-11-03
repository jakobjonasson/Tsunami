"""Shared fixtures. Tests never touch the real Credential Manager or real app-data folders."""

from __future__ import annotations

import os

import keyring
import keyring.errors
import pytest
from keyring.backend import KeyringBackend

from tsunami.core.paths import AppPaths
from tsunami.core.secret_store import SERVICE


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


def pytest_configure(config) -> None:
    """Refuse any write or delete of the real "tsunami" entries in Windows Credential
    Manager for the whole run, even from a thread that outlives its test's in-memory
    keyring. Real-vault tests use the separate "tsunami-test" service."""
    try:
        from keyring.backends.Windows import WinVaultKeyring
    except Exception:  # noqa: BLE001 - not Windows
        return

    def refuse(target: str) -> None:
        if target == SERVICE or target.endswith(f"@{SERVICE}") or target.startswith(f"{SERVICE}/"):
            raise RuntimeError(f"a test tried to change the real Credential Manager ({target})")

    from win32ctypes.pywin32 import win32cred

    ENTERPRISE = win32cred.CRED_PERSIST_ENTERPRISE  # noqa: N806
    write, delete = WinVaultKeyring._set_password, WinVaultKeyring._delete_password

    def guarded_write(self, target, username, password):
        refuse(target)
        if self.persist == ENTERPRISE:
            # An enterprise write makes Windows bring back stale roaming entries over newer
            # local-machine ones with the same target: this is how a test (the real-vault
            # round trip) kept replacing a real login on a developer's machine.
            raise RuntimeError("a test tried to write an enterprise-persistence credential")
        return write(self, target, username, password)

    def guarded_delete(self, target):
        refuse(target)
        return delete(self, target)

    WinVaultKeyring._set_password = guarded_write
    WinVaultKeyring._delete_password = guarded_delete


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
