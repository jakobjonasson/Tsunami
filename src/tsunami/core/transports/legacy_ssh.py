"""SHA-1 SSH algorithms for older devices, offered after every modern one.

Paramiko 5 removed SHA-1 key exchange and ssh-rsa (SHA-1) host-key signatures. Many
switches still in service, older Cisco IOS and ProCurve among them, offer nothing else.
LegacyTransport adds them back on its own class, after all modern algorithms in
preference order. SSH settles on the first algorithm in the client's list that the
server also offers, so a modern device still negotiates modern crypto, and only a device
with nothing better falls back. Host keys are pinned per customer either way, and the
key exchange hash covers both sides' algorithm lists, so a downgrade needs an SHA-1
collision found during the handshake.

AppSettings.ssh_legacy_algorithms turns this off. See design.md, Execution engine:
Transports.
"""

from __future__ import annotations

from hashlib import sha1

import paramiko
from cryptography.hazmat.primitives import hashes
from paramiko.kex_gex import KexGexSHA256
from paramiko.kex_group14 import KexGroup14SHA256
from paramiko.rsakey import RSAKey


class KexGroup14SHA1(KexGroup14SHA256):
    name = "diffie-hellman-group14-sha1"
    hash_algo = sha1


class KexGroup1SHA1(KexGroup14SHA256):
    # RFC 2409 section 6.2, Oakley group 2: 1024 bits, so the parent's 128-byte x fits.
    P = int(
        "FFFFFFFFFFFFFFFFC90FDAA22168C234C4C6628B80DC1CD129024E088A67CC74"
        "020BBEA63B139B22514A08798E3404DDEF9519B3CD3A431B302B0A6DF25F1437"
        "4FE1356D6D51C245E485B576625E7EC6F44C42E9A637ED6B0BFF5CB6F406B7ED"
        "EE386BFB5A899FA5AE9F24117C4B1FE649286651ECE65381FFFFFFFFFFFFFFFF",
        16,
    )
    G = 2
    name = "diffie-hellman-group1-sha1"
    hash_algo = sha1


class KexGexSHA1(KexGexSHA256):
    name = "diffie-hellman-group-exchange-sha1"
    hash_algo = sha1


class LegacyRSAKey(RSAKey):
    """An RSA host key that also verifies ssh-rsa (SHA-1) signatures."""

    HASHES = {
        **RSAKey.HASHES,
        "ssh-rsa": hashes.SHA1,
        "ssh-rsa-cert-v01@openssh.com": hashes.SHA1,
    }


# Strongest first; all of them after every algorithm Paramiko offers itself.
LEGACY_KEX = {cls.name: cls for cls in (KexGroup14SHA1, KexGexSHA1, KexGroup1SHA1)}
LEGACY_HOST_KEY_TYPES = ("ssh-rsa",)


class LegacyTransport(paramiko.Transport):
    """A Paramiko transport that falls back to SHA-1 for devices that need it."""

    _preferred_kex = paramiko.Transport._preferred_kex + tuple(LEGACY_KEX)
    _kex_info = {**paramiko.Transport._kex_info, **LEGACY_KEX}
    _preferred_keys = paramiko.Transport._preferred_keys + LEGACY_HOST_KEY_TYPES
    _key_info = {**paramiko.Transport._key_info, "ssh-rsa": LegacyRSAKey}
    agreed_kex = ""  # Paramiko drops kex_engine after the exchange; keep its name

    def _parse_kex_init(self, m):
        super()._parse_kex_init(m)
        # Not every kex class has a name attribute (KexCurve25519 does not): look it up.
        engine = type(self.kex_engine)
        self.agreed_kex = next((n for n, cls in self._kex_info.items() if cls is engine), "")


def transport_class(legacy: bool) -> type[paramiko.Transport]:
    return LegacyTransport if legacy else paramiko.Transport


def offered_key_types(transport: paramiko.Transport, pinned: list[str]) -> list[str]:
    """The host-key algorithms to ask for, given the key types already pinned for a host.

    An RSA key is pinned as "ssh-rsa" whatever signature it was read with, so it allows
    the SHA-2 signatures first and SHA-1 last; asking only for "ssh-rsa" would force
    SHA-1 on devices that support better.
    """
    wanted = set(pinned)
    if "ssh-rsa" in wanted:
        wanted |= {"rsa-sha2-512", "rsa-sha2-256"}
    available = transport.get_security_options().key_types
    return [t for t in available if t in wanted]


def uses_sha1(transport: paramiko.Transport) -> bool:
    """True once a connection has settled on a SHA-1 key exchange or host-key signature."""
    kex = getattr(transport, "agreed_kex", "")
    return kex in LEGACY_KEX or transport.host_key_type in LEGACY_HOST_KEY_TYPES
