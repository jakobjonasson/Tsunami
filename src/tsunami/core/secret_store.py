"""Credential Manager access (design.md, Secrets and credentials: Storage).

Phase 2 (work package 2.3) implements the keyring-backed store. The protocol and
naming are fixed now so other modules can depend on them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

SERVICE = "tsunami"
TEST_SERVICE = "tsunami-test"
MAX_VALUE_BYTES = 2560  # Credential Manager's per-value limit

# keyring usernames under SERVICE:
#   cred/<name>               JSON: username, password, optional enable
#   secret/<customer>/<name>  the secret string


@dataclass(frozen=True)
class LoginCredential:
    username: str
    password: str = field(repr=False)
    enable: str | None = field(default=None, repr=False)


class SecretStore(Protocol):
    def get_login(self, name: str) -> LoginCredential: ...

    def set_login(self, name: str, credential: LoginCredential) -> None: ...

    def delete_login(self, name: str) -> None: ...

    def list_logins(self) -> list[str]:
        """Names only, for the Credentials screen."""
        ...

    def get_secret(self, customer: str, name: str) -> str: ...

    def set_secret(self, customer: str, name: str, value: str) -> None: ...

    def delete_secret(self, customer: str, name: str) -> None: ...

    def list_secrets(self, customer: str) -> list[str]:
        """Names only, for the Credentials screen and the exact-value redactor."""
        ...
