"""HPE Aruba AOS-CX driver (family aos-cx): rendering side.

AOS-CX prints secrets as ciphertext bound to the export password and accepts `plaintext`
on input, so templates always use the plaintext form. Phase 3 (work package 3.10) adds
the run side. Facts behind these rules: docs/research/aruba-cli.md, section 4.1.

See design.md, Platform drivers.
"""

from __future__ import annotations

from tsunami.core.models.common import PlatformFamily
from tsunami.core.platforms.base import KEY, VALUE, PlatformDriver, rule

_PT = r"(?:plaintext|ciphertext)\s+"
_OSPF = (
    r"^\s*ip\s+ospf\s+(?:authentication-key|message-digest-key\s+\d+\s+md5|sha-key\s+\d+\s+sha)\s+"
)

SECRET_RULES = (
    rule(rf"^\s*user\s+\S+\s+group\s+\S+\s+password\s+{_PT}{VALUE}"),
    rule(r"^\s*(?:radius|tacacs)-server\b", rf"{KEY}\s+{_PT}{VALUE}"),
    rule(rf"^\s*snmp-server\s+community\s+{VALUE}"),
    rule(r"^\s*snmp-server\s+host\b", rf"\bcommunity\s+{VALUE}"),
    rule(r"^\s*snmpv3\s+user\b", rf"\b(?:auth-pass|priv-pass)\s+{_PT}{VALUE}"),
    rule(rf"^\s*ntp\s+authentication-key\s+\d+\s+\S+\s+(?:ciphertext\s+)?{VALUE}"),
    rule(_OSPF + _PT + VALUE),
    rule(rf"^\s*key-string\s+{_PT}{VALUE}"),
    rule(rf"^\s*neighbor\s+\S+\s+password\s+{_PT}{VALUE}"),
    rule(rf"^\s*aaa\s+authentication\s+port-access\s+mac-auth\s+password\s+{_PT}{VALUE}"),
    # Catch-all: any token after plaintext/ciphertext (MACsec cak, UBT keys, VRRP, ...).
    rule(
        r"\b(?:plaintext|ciphertext)\s",
        rf"\b(?:plaintext|ciphertext)\s+{VALUE}",
        sink=False,
        ignore_case=False,
    ),
)

DRIVER = PlatformDriver(
    family=PlatformFamily.AOS_CX,
    secret_rules=SECRET_RULES,
    guarded_exec=("boot", "erase", "copy", "checkpoint rollback"),
    guarded_config=(
        "console baud-rate",
        "service export-password",
        "no ssh server",
        "no https-server",
    ),
    comment_prefixes=("!",),
)
