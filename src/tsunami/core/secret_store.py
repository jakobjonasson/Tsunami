"""Credential Manager access (design.md, Secrets and credentials: Storage).

Work package 2.3: the keyring-backed store. The protocol and naming are fixed so
other modules can depend on them.

One Credential Manager entry per value. Each entry is stored with keyring service
``<service>/<key>`` and username ``<key>`` (key: ``cred/<name>`` or
``secret/<customer>/<name>``), so its target name is ``tsunami/cred/<name>`` and no
other entry ever shares it.

Persistence is always local machine. Windows keeps enterprise (roaming) and local-machine
entries in separate stores; after an enterprise write anywhere in the session, a stale
enterprise entry with the same target is shown again in place of a newer local one,
which is then lost. Early Tsunami builds used keyring's enterprise default, and a test
writing enterprise entries kept bringing one back over a real login.

Why not one service for everything: WinVaultKeyring keeps the most recently written
username of a service on the bare target ``<service>`` and moves the previous one to
``<username>@<service>`` on every write, so every write and delete touches an entry
belonging to a different login. In that layout the bare ``tsunami`` target had been
written with enterprise persistence by early builds and local machine by later ones,
which is how a stale login kept coming back over a real one (see Persistence above).
migrate_legacy() moves entries from that layout (``tsunami`` and ``<key>@tsunami``) at
startup and deletes them, stale enterprise copies included.

Listing names. keyring has no list API, but ``win32ctypes.pywin32.win32cred``
(pywin32-ctypes) provides ``CredEnumerate``. We enumerate all generic credentials
(targets come back as ``LegacyGeneric:target=<target>``; the prefix is stripped) and
keep those whose target is ``<service>/<UserName>``. This is the source of truth and
cannot drift from the vault, so it is the primary mechanism. Parsing is separated from
the Win32 call so it is unit-tested with fake results.

keyring's WinVaultKeyring also moves an entry aside when the same username is written
again under its service (``<key>@<service>/<key>``), which would leave a stale copy on
every overwrite. Writes therefore go straight to the entry's own target through the
backend's ``_set_password`` (a CredWrite with the backend's persistence) when it has one.

check_missing() reports names that disappeared since the last check without this
process deleting them; the GUI warns with the time it noticed.

Fallback: when the active keyring backend is not WinVaultKeyring (tests, other
platforms) enumeration is impossible, so a names-only JSON index at ``index_path``
(normally ``%APPDATA%\\Tsunami\\credential-index.json``, injected by the caller)
is kept in sync on set and delete. It never holds values.

Size limit: Credential Manager caps a blob at 2,560 bytes and keyring writes the
value as UTF-16-LE, so the limit is checked on the UTF-16-LE byte length (2 bytes
per BMP character, i.e. 1,280 characters), not UTF-8.

Errors raised here never include a secret value, and keyring backend exception
text is discarded (only the exception class name is reported).

Every write and delete is logged (app.log) with the entry name and the code that asked
for it, never the value, so an entry that goes missing can be traced.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import keyring
import keyring.errors

from tsunami.core.issues import TsunamiError
from tsunami.core.models.common import require_slug
from tsunami.core.vault import TOKEN_PREFIX, LockedCipher, VaultLocked

log = logging.getLogger(__name__)

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
    """Keys stored by this store for ``service`` from CredEnumerate results (sorted,
    unique): entries whose target is ``<service>/<UserName>``."""
    return _parse(credentials, lambda target, user: target == f"{service}/{user}")


def parse_legacy_usernames(service: str, credentials: list[dict[str, Any]]) -> list[str]:
    """Keys in the old shared layout: target ``service`` or ``<UserName>@service``."""
    return _parse(credentials, lambda target, user: target in (service, f"{user}@{service}"))


def _parse(credentials: list[dict[str, Any]], belongs: Callable[[str, str], bool]) -> list[str]:
    names: set[str] = set()
    for cred in credentials:
        target = cred.get("TargetName")
        user = cred.get("UserName")
        if not isinstance(target, str) or not isinstance(user, str):
            continue
        if belongs(target.removeprefix(_ENUM_TARGET_PREFIX), user):
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
        cipher: Any = None,
    ) -> None:
        self._service = service
        self._index_path = index_path
        self._enumerator = enumerator  # test seam: replaces CredEnumerate
        # vault.Cipher while unlocked, vault.LockedCipher while locked; None stores plain
        # values (no Tsunami password set: tests, and stores from before the vault).
        self.cipher = cipher
        self._known: set[str] | None = None  # names at the last check_missing()

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
            except VaultLocked:
                raise  # never redact with a silently empty list
            except TsunamiError:
                continue  # removed between listing and reading, or malformed
        return values

    def rekey(self, cipher: Any) -> int:
        """Re-store every entry under a new cipher: first-time encryption of plain values,
        or a password change. Everything is read before anything is written."""
        keys = self._usernames()
        plain = {key: self._get(key, key) for key in keys}
        stored = {key: keyring.get_password(self._entry(key), key) for key in keys}
        previous, self.cipher = self.cipher, cipher
        try:
            for key, value in plain.items():
                self._set(key, value, key)
        except BaseException:
            # Put every entry back as it was, so the old password still opens all of them.
            for key, raw in stored.items():
                if raw is not None:
                    self._write(key, raw)
            self.cipher = previous
            raise
        return len(plain)

    def delete_everything(self) -> int:
        """Remove every Tsunami entry: the reset after a forgotten Tsunami password."""
        keys = self._usernames()
        for key in keys:
            self._delete(key, key)
        return len(keys)

    def encrypt_plain(self) -> int:
        """Encrypt values still stored in plain text (saved before the Tsunami password,
        or copied from an old entry). Needs the unlocked cipher; returns how many."""
        if self.cipher is None or isinstance(self.cipher, LockedCipher):
            return 0
        done = 0
        for key in self._usernames():
            raw = keyring.get_password(self._entry(key), key)
            if raw is None or raw.startswith(TOKEN_PREFIX):
                continue
            self._set(key, raw, key)  # _set encrypts with the cipher
            done += 1
        return done

    def migrate_legacy(self) -> int:
        """Move entries from the old shared layout (one ``<service>`` target, the rest
        at ``<key>@<service>``) to one target per entry. The raw stored value is copied,
        so nothing is decrypted and no password is needed; the old entry is deleted only
        after the copy reads back identical. Returns how many entries moved."""
        moved = 0
        for key in self._legacy_keys():
            try:
                raw = keyring.get_password(self._service, key)
                if raw is None:
                    continue
                current = keyring.get_password(self._entry(key), key)
                if current is None:
                    self._write(key, raw)
                    if keyring.get_password(self._entry(key), key) != raw:
                        log.error("credential store: copy of %s did not read back; kept", key)
                        continue
                    moved += 1
                    log.info("credential store: moved %s to its own entry", key)
                else:
                    log.info("credential store: %s already has its own entry", key)
                keyring.delete_password(self._service, key)
                self._index_add(key)
            except Exception as exc:  # noqa: BLE001 - one bad entry must not stop the rest
                log.error("credential store: could not move %s (%s)", key, type(exc).__name__)
        return moved

    def _legacy_keys(self) -> list[str]:
        if self._enumerator is not None or _on_win_vault():
            enumerate_fn = self._enumerator or _win32_enumerate
            return parse_legacy_usernames(self._service, enumerate_fn())
        if self._index_path is None:
            return []
        return [k for k in self._index_read() if keyring.get_password(self._service, k)]

    def check_missing(self) -> list[str]:
        """Names that were there at the last check and are gone now, without this
        process deleting them. The first call records the names and returns []."""
        current = set(self._usernames())
        known, self._known = self._known, current
        return sorted(known - current) if known is not None else []

    # ---- internals ----

    def _entry(self, key: str) -> str:
        """The keyring service for one entry: its own Credential Manager target."""
        return f"{self._service}/{key}"

    def _write(self, key: str, value: str) -> None:
        """Store a raw value on the entry's own target, never moving another entry."""
        backend = keyring.get_keyring()
        direct = getattr(backend, "_set_password", None)
        if direct is not None:  # WinVaultKeyring: CredWrite to exactly this target
            # Never enterprise (keyring's default): Windows keeps enterprise and local-machine
            # entries apart, and an enterprise write elsewhere brings a stale enterprise copy
            # back over a newer local one with the same target. That replaced a real login.
            backend.persist = "local machine"
            direct(self._entry(key), key, value)
        else:
            keyring.set_password(self._entry(key), key, value)

    @staticmethod
    def _secret_key(customer: str, name: str) -> str:
        return f"{_SECRET_PREFIX}{require_slug(customer)}/{require_slug(name)}"

    def _get(self, key: str, label: str) -> str:
        try:
            value = keyring.get_password(self._entry(key), key)
        except Exception as exc:  # noqa: BLE001
            raise self._backend_error("reading", label, exc) from None
        if value is None:
            raise TsunamiError(
                f"{label} is not in Credential Manager; add it in Tsunami's Credentials view"
            )
        if self.cipher is not None:
            if value.startswith(TOKEN_PREFIX):
                return self.cipher.decrypt(key, value)
            if isinstance(self.cipher, LockedCipher):
                raise VaultLocked
        elif value.startswith(TOKEN_PREFIX):
            raise VaultLocked  # encrypted, and no key in this process
        return value  # stored before the Tsunami password was set; rekey encrypts it

    def _set(self, key: str, value: str, label: str) -> None:
        if self.cipher is not None:
            value = self.cipher.encrypt(key, value)
        size = _stored_size(value)
        if size > MAX_VALUE_BYTES:
            raise TsunamiError(
                f"value for {label} is {size} bytes as stored (UTF-16), "
                f"over the Credential Manager limit of {MAX_VALUE_BYTES}"
            )
        try:
            self._write(key, value)
        except Exception as exc:  # noqa: BLE001
            raise self._backend_error("writing", label, exc) from None
        self._index_add(key)
        if self._known is not None:
            self._known.add(key)
        log.info("credential store: wrote %s (%s, from %s)", key, self._service, _caller())

    def _delete(self, key: str, label: str) -> None:
        log.info("credential store: deleting %s (%s, from %s)", key, self._service, _caller())
        if self._known is not None:
            self._known.discard(key)
        try:
            keyring.delete_password(self._entry(key), key)
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


def _caller() -> str:
    """The first function outside this module on the stack: who asked for the change."""
    frame = sys._getframe(1)
    while frame is not None and frame.f_globals.get("__name__") == __name__:
        frame = frame.f_back
    if frame is None:
        return "unknown"
    return f"{frame.f_globals.get('__name__', '?')}.{frame.f_code.co_name}"
