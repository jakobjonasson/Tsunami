"""Cisco IOS and IOS-XE driver (family ios): rendering side.

Secret rules cover every encryption type (0, 5, 6, 7, 8, 9); type 7 is reversible, so
it counts as plaintext. Phase 3 (work package 3.9) adds the run side.

See design.md, Platform drivers; Secrets and credentials: Redactor; secret().
"""

from __future__ import annotations

import re

from tsunami.core.models.common import PlatformFamily
from tsunami.core.platforms.base import (
    KEY,
    NOT_FORM,
    TYPE,
    BlockRule,
    PlatformDriver,
    rule,
)
from tsunami.core.platforms.base import VALUE as _ANY_VALUE

_ENABLE = r"^\s*enable\s+(?:algorithm-type\s+\S+\s+)?(?:secret|password)(?:\s+level\s+\d+)?\s+"
_OSPF = r"^\s+ip\s+ospf\s+(?:authentication-key|message-digest-key\s+\d+\s+md5)\s+"
# Community form only: no `community` keyword (AOS-CX/AOS-S style) and not SNMPv3,
# where the token after the security level is a user name.
_SNMP_HOST = (
    r"^(?!.*\b(?:community|version\s+3)\b)\s*snmp-server\s+host\s+\S+(?:\s+vrf\s+\S+)?"
    r"(?:\s+(?:informs|traps))?(?:\s+version\s+(?:1|2c))?\s+"
)
# Sub-mode `key N` lines under a key chain are key ids, not secrets.
_KEY_CHAIN = r"^(?:key\s+chain|keychain)\b"
# Words that follow `password`/`secret` without being the value. password, secret and
# privilege are the value only at the end of a line ("username bob password 0 secret").
_NOT_A_VALUE = r"(?!(?:manager|operator|encryption)\b|(?:password|secret|privilege)\s)"
# IOS values never start with an AOS-CX encoding keyword; every family's rules run on
# all output, so this keeps IOS rules from masking `plaintext` instead of the value.
VALUE = NOT_FORM + _ANY_VALUE

SECRET_RULES = (
    rule(_ENABLE + TYPE + VALUE),
    # The user name is matched as a word first, so a user named "secret" cannot shift it.
    rule(rf"^\s*username\s+\S+\s+(?:.*?\s)?(?:secret|password)\s+{TYPE}{VALUE}"),
    rule(rf"^\s*snmp-server\s+community\s+{VALUE}"),
    rule(r"^\s*snmp-server\s+user\b", rf"\bauth\s+(?:md5|sha\S*)\s+{VALUE}"),
    rule(r"^\s*snmp-server\s+user\b", rf"\bpriv\s+(?:3des|des\S*|aes(?:\s+\d+)?)\s+{VALUE}"),
    rule(_SNMP_HOST + VALUE),
    rule(
        r"^\s*(?:tacacs|radius)-server\b",
        rf"{KEY}\s+{TYPE}{VALUE}",
    ),
    # First word of a sub-mode line: line password, key chain key-string.
    rule(rf"^\s+(?:key-string|password|authentication-key)\s+{TYPE}{VALUE}"),
    # tacacs/radius server key; under a key chain, `key N` is a key id instead.
    rule(rf"^\s+key\s+{TYPE}{VALUE}", skip_under=_KEY_CHAIN),
    rule(r"^\s+(?:pac|server-private)\b.*\bkey\b", rf"{KEY}\s+{TYPE}{VALUE}"),
    rule(r"^\s+pre-shared-key\b", rf"{KEY}\s+{TYPE}{VALUE}"),
    rule(rf"^\s+pre-shared-key\s+{TYPE}(?!(?:address|hostname|local|remote)\b){VALUE}"),
    rule(rf"^\s+pre-shared-key\s+(?:local|remote)\s+{TYPE}{VALUE}"),
    rule(r"^\s+authentication\s+(?:local|remote)\s+pre-share\b", rf"{KEY}\s+{TYPE}{VALUE}"),
    rule(
        r"^\s+(?:standby|vrrp)\b.*\bauthentication\b",
        rf"\bauthentication\s+(?!md5\s+key-chain\b)(?:text\s+|md5\s+key-string\s+{TYPE})?{VALUE}",
    ),
    rule(_OSPF + TYPE + VALUE),
    rule(rf"^\s+neighbor\s+\S+\s+password\s+{TYPE}{VALUE}"),
    rule(rf"^\s*crypto\s+isakmp\s+key\s+{TYPE}{VALUE}"),
    rule(rf"^\s*ntp\s+authentication-key\s+\d+\s+\S+\s+{VALUE}"),
    rule(rf"^\s*key\s+config-key\s+password-encrypt\s+{VALUE}"),
    rule(r"^\s+ppp\s+(?:chap|pap)\b.*\bpassword\b", rf"\bpassword\s+{TYPE}{VALUE}"),
    # Catch-all: redacts, never authorizes secret() (a description saying "password x").
    rule(
        r"\b(?:password|secret)\s",
        rf"\b(?:password|secret)\s+(?:level\s+\d+\s+)?{TYPE}"
        rf"(?:(?:plaintext|ciphertext|sha1|sha256|bcrypt|md5)\s+)?{_NOT_A_VALUE}{_ANY_VALUE}",
        sink=False,
        ignore_case=False,
    ),
)

SECRET_BLOCKS = (
    # crypto pki certificate chain: hex lines between "certificate <serial>" and "quit".
    BlockRule(
        re.compile(r"^\s+certificate\s+(?:self-signed\s+|ca\s+)?[0-9A-Fa-f]+\s*$"),
        re.compile(r"^\s+quit\s*$"),
    ),
)

DRIVER = PlatformDriver(
    family=PlatformFamily.IOS,
    secret_rules=SECRET_RULES,
    secret_blocks=SECRET_BLOCKS,
    guarded_exec=("reload", "erase", "format", "delete", "write erase"),
    comment_prefixes=("!",),
)
