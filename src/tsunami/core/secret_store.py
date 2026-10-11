"""Credential Manager access (design.md, Secrets and credentials: Storage).

Work package 2.3: the keyring-backed store. The protocol and naming are fixed so
other modules can depend on them.

How WinVaultKeyring stores a ``(service, username)`` pair (read from the
installed keyring source): the first username stored under a service gets the
Credential Manager target name ``<service>`` with ``UserName`` = the keyring
username. When a second, different username is stored, the existing entry is
re-saved under the compound target ``<username>@<service>`` and the new one takes
the bare ``<service>`` target. So the targets are ``<service>`` plus
``<username>@<service>`` for the others, and the ``UserName`` field of every
entry is always the exact keyring username. The blob is UTF-16-LE.

Listing names. keyring has no list API, but ``win32ctypes.pywin32.win32cred``
(pywin32-ctypes) provides ``CredEnumerate``. Credential Manager's filter only
supports a trailing ``*`` (prefix match), so ``*@tsunami`` cannot be used; we
enumerate all generic credentials (targets come back as
``LegacyGeneric:target=<target>``; the prefix is stripped), keep those whose
target is ``tsunami`` or ``<UserName>@tsunami``, and read the keyring username
from ``UserName``. This is
the source of truth and cannot drift from the vault, so it is the primary
mechanism. Parsing is separated from the Win32 call so it is unit-tested with
fake results.

Fallback: when the active keyring backend is not WinVaultKeyring (tests, other
platforms) enumeration is impossible, so a names-only JSON index at ``index_path``
(normally ``%APPDATA%\\Tsunami\\credential-index.json``, injected by the caller)
is kept in sync on set and delete. It never holds values.

Size limit: Credential Manager caps a blob at 2,560 bytes and keyring writes the
value as UTF-16-LE, so the limit is checked on the UTF-16-LE byte length (2 bytes
per BMP character, i.e. 1,280 characters), not UTF-8.

Errors raised here never include a secret value, and keyring backend exception
text is discarded (only the exception class name is reported).
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import keyring
import keyring.errors

from tsunami.core.issues import TsunamiError
from tsunami.core.models.common import require_slug

SERVICE = "tsunami"
TEST_SERVICE = "tsunami-test"
MAX_VALUE_BYTES = 2560  # Credential Manager's per-value limit

# keyring usernames under SERVICE:
#   cred/<name>               JSON: username, password, optional enable
#   secret/<customer>/<name>  the secret string

_CRED_PREFIX = "cred/"
_SECRET_PREFIX = "secret/"
_ERROR_NOT_FOUND = 1168
# CredEnumerate reports generic targets with this prefix (CredRead does not).
_ENUM_TARGET_PREFIX = "LegacyGeneric:target="


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


def parse_enumerated_usernames(service: str, credentials: list[dict[str, Any]]) -> list[str]:
    """Keyring usernames for ``service`` from CredEnumerate results (sorted, unique).

    A credential belongs to the service when its target is ``service`` or
    ``<UserName>@service``; its ``UserName`` field is the keyring username.
    """
    names: set[str] = set()
    for cred in credentials:
        target = cred.get("TargetName")
        user = cred.get("UserName")
        if not isinstance(target, str) or not isinstance(user, str):
            continue
        target = target.removeprefix(_ENUM_TARGET_PREFIX)
        if target in (service, f"{user}@{service}"):
            names.add(user)
    return sorted(names)


def _win32_enumerate() -> list[dict[str, Any]]:
    from win32ctypes.pywin32 import pywintypes, win32cred

    try:
        return list(win32cred.CredEnumerate(None, win32cred.CRED_ENUMERATE_ALL_CREDENTIALS))
    except pywintypes.error as exc:
        if exc.winerror == _ERROR_NOT_FOUND:  # no credentials at all
            return []
        raise


def _on_win_vault() -> bool:
    try:
        from keyring.backends.Windows import WinVaultKeyring
    except Exception:  # noqa: BLE001
        return False
    return isinstance(keyring.get_keyring(), WinVaultKeyring)


def _stored_size(value: str) -> int:
    return len(value.encode("utf-16-le", errors="surrogatepass"))


class KeyringSecretStore:
    """SecretStore backed by keyring (Windows Credential Manager)."""

    def __init__(
        self,
        service: str = SERVICE,
        *,
        index_path: Path | None = None,
        enumerator: Callable[[], list[dict[str, Any]]] | None = None,
    ) -> None:
        self._service = service
        self._index_path = index_path
        self._enumerator = enumerator  # test seam: replaces CredEnumerate

    def __repr__(self) -> str:
        return f"KeyringSecretStore(service={self._service!r})"

    # ---- logins ----

    def get_login(self, name: str) -> LoginCredential:
        raw = self._get(f"{_CRED_PREFIX}{require_slug(name)}", f"credential {name}")
        try:
            data = json.loads(raw)
            enable = data.get("enable")
            return LoginCredential(
                username=str(data["username"]),
                password=str(data["password"]),
                enable=None if enable is None else str(enable),
            )
        except (ValueError, KeyError, AttributeError, TypeError):
            raise TsunamiError(f"stored credential {name!r} is malformed") from None

    def set_login(self, name: str, credential: LoginCredential) -> None:
        payload: dict[str, str] = {
            "username": credential.username,
            "password": credential.password,
        }
        if credential.enable is not None:
            payload["enable"] = credential.enable
        self._set(f"{_CRED_PREFIX}{require_slug(name)}", json.dumps(payload), f"credential {name}")

    def delete_login(self, name: str) -> None:
        self._delete(f"{_CRED_PREFIX}{require_slug(name)}", f"credential {name}")

    def list_logins(self) -> list[str]:
        names = (u for u in self._usernames() if u.startswith(_CRED_PREFIX))
        return sorted(u[len(_CRED_PREFIX) :] for u in names)

    # ---- secrets ----

    def get_secret(self, customer: str, name: str) -> str:
        return self._get(self._secret_key(customer, name), f"secret {customer}/{name}")

    def set_secret(self, customer: str, name: str, value: str) -> None:
        self._set(self._secret_key(customer, name), value, f"secret {customer}/{name}")

    def delete_secret(self, customer: str, name: str) -> None:
        self._delete(self._secret_key(customer, name), f"secret {customer}/{name}")

    def list_secrets(self, customer: str) -> list[str]:
        prefix = f"{_SECRET_PREFIX}{require_slug(customer)}/"
        return sorted(u[len(prefix) :] for u in self._usernames() if u.startswith(prefix))

    def secret_values(self, customer: str) -> list[str]:
        """Every secret value in the customer's namespace, for the exact-value redactor."""
        values = []
        for name in self.list_secrets(customer):
            try:
                values.append(self.get_secret(customer, name))
            except TsunamiError:
                continue  # removed between listing and reading
        return values

    def all_secret_values(self) -> list[str]:
        """Every stored secret and login password or enable, across all customers.

        For redaction that cannot be tied to one customer: MCP results and audit rows.
        Login usernames are left out, as everywhere (design.md, Redaction).
        """
        values: list[str] = []
        for key in self._usernames():
            try:
                if key.startswith(_CRED_PREFIX):
                    login = self.get_login(key[len(_CRED_PREFIX) :])
                    values += [v for v in (login.password, login.enable) if v]
                elif key.startswith(_SECRET_PREFIX):
                    values.append(self._get(key, "secret"))
            except TsunamiError:
                continue  # removed between listing and reading, or malformed
        return values

    # ---- internals ----

    @staticmethod
    def _secret_key(customer: str, name: str) -> str:
        return f"{_SECRET_PREFIX}{require_slug(customer)}/{require_slug(name)}"

    def _get(self, key: str, label: str) -> str:
        try:
            value = keyring.get_password(self._service, key)
        except Exception as exc:  # noqa: BLE001
            raise self._backend_error("reading", label, exc) from None
        if value is None:
            raise TsunamiError(
                f"{label} is not in Credential Manager; add it in Tsunami's Credentials view"
            )
        return value

    def _set(self, key: str, value: str, label: str) -> None:
        size = _stored_size(value)
        if size > MAX_VALUE_BYTES:
            raise TsunamiError(
                f"value for {label} is {size} bytes as stored (UTF-16), "
                f"over the Credential Manager limit of {MAX_VALUE_BYTES}"
            )
        try:
            keyring.set_password(self._service, key, value)
        except Exception as exc:  # noqa: BLE001
            raise self._backend_error("writing", label, exc) from None
        self._index_add(key)

    def _delete(self, key: str, label: str) -> None:
        try:
            keyring.delete_password(self._service, key)
        except keyring.errors.PasswordDeleteError:
            self._index_remove(key)
            raise TsunamiError(f"{label} is not in Credential Manager") from None
        except Exception as exc:  # noqa: BLE001
            raise self._backend_error("deleting", label, exc) from None
        self._index_remove(key)

    @staticmethod
    def _backend_error(action: str, label: str, exc: Exception) -> TsunamiError:
        # Only the class name: backend messages may carry the value.
        return TsunamiError(f"credential store failed {action} {label} ({type(exc).__name__})")

    def _usernames(self) -> list[str]:
        if self._enumerator is not None or _on_win_vault():
            enumerate_fn = self._enumerator or _win32_enumerate
            try:
                return parse_enumerated_usernames(self._service, enumerate_fn())
            except Exception as exc:  # noqa: BLE001
                raise self._backend_error("listing", "credentials", exc) from None
        return self._index_read()

    # ---- fallback names index (names only, never values) ----

    def _use_index(self) -> bool:
        return self._enumerator is None and self._index_path is not None and not _on_win_vault()

    def _index_read(self) -> list[str]:
        if self._index_path is None:
            raise TsunamiError(
                "cannot list credentials: backend has no enumeration and no index path"
            )
        try:
            data = json.loads(self._index_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        except (OSError, ValueError):
            raise TsunamiError(f"credential index {self._index_path} is unreadable") from None
        if not isinstance(data, list) or not all(isinstance(x, str) for x in data):
            raise TsunamiError(f"credential index {self._index_path} is malformed")
        return sorted(set(data))

    def _index_write(self, names: list[str]) -> None:
        assert self._index_path is not None
        self._index_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._index_path.with_name(self._index_path.name + ".tmp")
        tmp.write_text(json.dumps(sorted(set(names)), indent=2), encoding="utf-8")
        os.replace(tmp, self._index_path)

    def _index_add(self, key: str) -> None:
        if self._use_index():
            self._index_write([*self._index_read(), key])

    def _index_remove(self, key: str) -> None:
        if self._use_index():
            self._index_write([n for n in self._index_read() if n != key])
