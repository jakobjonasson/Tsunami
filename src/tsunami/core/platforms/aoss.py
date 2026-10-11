"""HPE Aruba AOS-S driver, formerly ProCurve (family aos-s): rendering side.

AOS-S quotes names and values and, by default, keeps credentials out of the running
config. Phase 3 (work package 3.10) adds the run side. Facts behind these rules:
docs/research/aruba-cli.md, sections 4.2 and 6.2.

See design.md, Platform drivers.
"""

from __future__ import annotations

from tsunami.core.models.common import PlatformFamily
from tsunami.core.platforms.base import KEY, TYPE, VALUE, PlatformDriver, rule

_HASH = r"(?:sha1|sha256|plaintext|bcrypt)\s+"

SECRET_RULES = (
    rule(r"^\s*password\s+\S+", rf"\b{_HASH}{VALUE}"),
    rule(rf"^\s*snmp-server\s+community\s+{VALUE}"),
    rule(r"^\s*snmp-server\s+host\b", rf"\bcommunity\s+{VALUE}"),
    rule(
        r"^\s*snmpv3\s+user\b",
        rf"\b(?:auth\s+(?:sha|md5)|priv\s+(?:des|aes))\s+(?!(?:auth|priv)-pass\b){VALUE}",
    ),
    rule(
        r"^\s*(?:radius|tacacs)-server\b",
        rf"(?:{KEY}|encrypted-key)\s+{TYPE}(?!(?:plaintext|ciphertext)\s){VALUE}",
    ),
    rule(r"^\s*s?ntp\s+authentication\b", rf"\b(?:key-value|encrypted-key)\s+{VALUE}"),
    rule(r"^\s*aaa\s+authentication\s+local-user\b", rf"\b{_HASH}{VALUE}"),
    rule(rf"^;\s*encrypt-cred\s+{VALUE}", sink=False, ignore_case=False),
    rule(r"\bplaintext\s", rf"\bplaintext\s+{VALUE}", sink=False, ignore_case=False),
)

# Commands that make cfg-restore refuse a rollback (design.md, Backup and rollback).
REBOOT_REQUIRED = (
    "secure-mode",
    "max-vlans",
    "qinq",
    "allow-v2-modules",
    "no allow-v2-modules",
    "vsf member",
    "vsf remove",
    "console",
    "qos queue-config",
    "access-list grouping",
    "module",
    "stacking",
)

DRIVER = PlatformDriver(
    family=PlatformFamily.AOS_S,
    secret_rules=SECRET_RULES,
    guarded_exec=("reload", "boot", "erase", "copy", "cfg-restore"),
    guarded_config=(
        "include-credentials",
        "encrypt-credentials",
        "no include-credentials",
        "no encrypt-credentials",
        *REBOOT_REQUIRED,
    ),
    comment_prefixes=(";", "!"),
)
