"""The Tsunami password: stored logins and secrets are encrypted with a key derived from it.

Values in Credential Manager are AES-256-GCM ciphertext under a key derived from the
password with scrypt. Windows still protects each entry (DPAPI, this machine only), and
the password adds a lock that other programs running as you cannot open without it.
The key lives only in the GUI process, and only while Tsunami is unlocked. A forgotten
password cannot be recovered: resetting it deletes every stored login and secret.

vault.json (in %LOCALAPPDATA%\\Tsunami) holds the salt, the scrypt cost and a check
value. None of it is secret.

See design.md, Secrets and credentials: Tsunami password.
"""

from __future__ import annotations

import base64
import json
import os
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from tsunami.core.issues import TsunamiError

TOKEN_PREFIX = "tv1:"  # an encrypted value in Credential Manager
MIN_PASSWORD_LENGTH = 8
_CHECK = b"tsunami vault check"
_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2**17, 8, 1  # about 128 MiB and a fraction of a second


class VaultError(TsunamiError):
    """Wrong password, a damaged vault file, or a value that does not decrypt."""


class VaultLocked(TsunamiError):
    """Tsunami is locked: stored logins and secrets cannot be read or written."""

    def __init__(self) -> None:
        super().__init__("Tsunami is locked; unlock it with the Tsunami password")


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.b64decode(text.encode("ascii"), validate=True)


@dataclass(frozen=True)
class Cipher:
    """Encrypts store values under the vault key. AAD binds each value to its entry name,
    so a value copied to another entry does not decrypt."""

    key: bytes

    def encrypt(self, name: str, value: str) -> str:
        nonce = os.urandom(12)
        sealed = AESGCM(self.key).encrypt(nonce, value.encode("utf-8"), name.encode("utf-8"))
        return TOKEN_PREFIX + _b64(nonce + sealed)

    def decrypt(self, name: str, token: str) -> str:
        if not token.startswith(TOKEN_PREFIX):
            raise VaultError(f"{name} is not encrypted")
        try:
            raw = _unb64(token[len(TOKEN_PREFIX) :])
            plain = AESGCM(self.key).decrypt(raw[:12], raw[12:], name.encode("utf-8"))
        except (InvalidTag, ValueError):
            raise VaultError(f"{name} does not decrypt with this password") from None
        return plain.decode("utf-8")


class LockedCipher:
    """The cipher while Tsunami is locked: every use is refused."""

    def encrypt(self, name: str, value: str) -> str:
        raise VaultLocked

    def decrypt(self, name: str, token: str) -> str:
        raise VaultLocked


class Vault:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def exists(self) -> bool:
        return self.path.is_file()

    def create(self, password: str) -> Cipher:
        """Set the Tsunami password (first launch, or after a reset)."""
        cipher, save = self.prepare(password)
        save()
        return cipher

    def prepare(self, password: str) -> tuple[Cipher, Callable[[], None]]:
        """A cipher for a new password, and the function that saves it as the Tsunami
        password. Re-encrypt the store with the cipher first, then save: if that fails,
        the old password still opens everything."""
        check_password(password)
        salt = os.urandom(16)
        n, r, p = _SCRYPT_N, _SCRYPT_R, _SCRYPT_P
        cipher = Cipher(_derive(password, salt, n, r, p))
        check = cipher.encrypt("vault", _CHECK.hex())
        return cipher, lambda: self._write(salt, n, r, p, check)

    def unlock(self, password: str) -> Cipher:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            salt = _unb64(data["salt"])
            n, r, p = int(data["n"]), int(data["r"]), int(data["p"])
            check = data["check"]
        except FileNotFoundError:
            raise VaultError("no Tsunami password is set yet") from None
        except (OSError, ValueError, KeyError, TypeError):
            raise VaultError(f"{self.path.name} is damaged; reset the Tsunami password") from None
        cipher = Cipher(_derive(password, salt, n, r, p))
        try:
            ok = cipher.decrypt("vault", check) == _CHECK.hex()
        except VaultError:
            ok = False
        if not ok:
            raise VaultError("wrong Tsunami password")
        return cipher

    def remove(self) -> None:
        self.path.unlink(missing_ok=True)

    def _write(self, salt: bytes, n: int, r: int, p: int, check: str) -> None:
        payload = {"version": 1, "kdf": "scrypt", "n": n, "r": r, "p": p}
        payload |= {"salt": _b64(salt), "check": check}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2)
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise


def check_password(password: str) -> None:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise VaultError(f"the Tsunami password needs at least {MIN_PASSWORD_LENGTH} characters")


def _derive(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return Scrypt(salt=salt, length=32, n=n, r=r, p=p).derive(password.encode("utf-8"))
