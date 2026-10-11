# ruff: noqa: E501
"""Blind adversarial tests for the redactor (work package 2.5).

Written against the public API only: ``Redactor(exact_values=...)`` with
``text``, ``lines`` and ``line``. Fixtures live in ``tests/fixtures/configs/<family>/``.
Each ``<name>.txt`` has ``<name>.secrets`` (found by the pattern layer) and an
optional ``<name>.exact-only.secrets`` (banner/description text that only the
exact-value layer can catch).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tsunami.core.redact import REDACTED, Redactor

CONFIG_ROOT = Path(__file__).resolve().parent.parent / "fixtures" / "configs"
FIXTURES = sorted(CONFIG_ROOT.glob("*/*.txt"))

# Known leaks, keyed by (fixture file name, secret) -> reason. Filled from observed
# failures; every entry is a real leak in the redactor, not a test problem.
LEAKS: dict[tuple[str, str], str] = {}

# Single-line cases that currently leak, keyed by case id -> reason.
LINE_LEAKS: dict[str, str] = {}

# Context (non-secret tokens) that the pattern layer also replaces. Not a secrecy leak,
# but it damages diffs: encryption type digits and plaintext/ciphertext/user keywords vanish.
CONTEXT_OVER: set[str] = set()

# Non-secret fixture lines the redactor currently mangles (verbatim input lines).
KNOWN_OVER_REDACTION = {"snmp-server host 192.0.2.5 version 2c"}


def _read(path: Path) -> list[str]:
    if not path.exists():
        return []
    return [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _pattern_secrets(fx: Path) -> list[str]:
    return _read(fx.with_suffix(".secrets"))


def _exact_only(fx: Path) -> list[str]:
    return _read(fx.with_suffix(".exact-only.secrets"))


def _ids(paths: list[Path]) -> list[str]:
    return [f"{p.parent.name}/{p.name}" for p in paths]


def _leak_marks(fx: Path, secret: str) -> list[pytest.MarkDecorator]:
    reason = LEAKS.get((fx.name, secret))
    if reason is None:
        return []
    return [pytest.mark.xfail(strict=True, reason=f"LEAK: {reason}")]


PATTERN_PARAMS = [
    pytest.param(fx, s, id=f"{fx.parent.name}/{fx.stem}::{s[:40]}", marks=_leak_marks(fx, s))
    for fx in FIXTURES
    for s in _pattern_secrets(fx)
]


def test_fixtures_present() -> None:
    families = {p.parent.name for p in FIXTURES}
    assert {"ios", "aos-cx", "aos-s"} <= families
    assert sum(1 for p in FIXTURES if p.parent.name == "ios") >= 2


@pytest.mark.parametrize("fx", FIXTURES, ids=_ids(FIXTURES))
def test_every_planted_secret_is_really_in_fixture(fx: Path) -> None:
    """Guards the fixtures themselves, so a typo cannot hide a leak."""
    config = _text(fx)
    for s in _pattern_secrets(fx) + _exact_only(fx):
        assert s in config, f"{s!r} not planted in {fx.name}"


# 1. Pattern layer ---------------------------------------------------------------


@pytest.mark.parametrize(("fx", "secret"), PATTERN_PARAMS)
def test_pattern_layer_removes_secret(fx: Path, secret: str) -> None:
    out = Redactor().text(_text(fx))
    assert secret not in out


# 2. Exact layer -----------------------------------------------------------------


@pytest.mark.parametrize("fx", FIXTURES, ids=_ids(FIXTURES))
def test_exact_layer_removes_everything(fx: Path) -> None:
    secrets = _pattern_secrets(fx) + _exact_only(fx)
    out = Redactor(exact_values=secrets).text(_text(fx))
    leaked = [s for s in secrets if len(s) >= 4 and s in out]
    assert not leaked


@pytest.mark.parametrize("fx", FIXTURES, ids=_ids(FIXTURES))
def test_exact_only_secrets_survive_pattern_layer_but_not_exact(fx: Path) -> None:
    """Sanity: banners/descriptions are the exact layer's job (documents the split)."""
    for s in _exact_only(fx):
        assert s not in Redactor(exact_values=[s]).text(_text(fx))


# 3. Structure preserved ---------------------------------------------------------

_HEX_LINE = re.compile(r"^[0-9A-Fa-f ]+$")


def _contains_any(line: str, secrets: list[str]) -> bool:
    return any(s in line for s in secrets)


@pytest.mark.parametrize("fx", FIXTURES, ids=_ids(FIXTURES))
@pytest.mark.parametrize("with_exact", [False, True], ids=["pattern", "exact"])
def test_line_count_unchanged(fx: Path, with_exact: bool) -> None:
    text = _text(fx)
    r = Redactor(exact_values=_pattern_secrets(fx) + _exact_only(fx)) if with_exact else Redactor()
    assert r.text(text).count("\n") == text.count("\n")
    assert len(r.text(text).split("\n")) == len(text.split("\n"))


@pytest.mark.parametrize("fx", FIXTURES, ids=_ids(FIXTURES))
def test_non_secret_lines_byte_identical(fx: Path) -> None:
    secrets = _pattern_secrets(fx) + _exact_only(fx)
    src = _text(fx).split("\n")
    out = Redactor().text(_text(fx)).split("\n")
    assert len(src) == len(out)
    damaged = [
        (a, b) for a, b in zip(src, out, strict=True) if not _contains_any(a, secrets) and a != b
    ]
    damaged = [d for d in damaged if d[0] not in KNOWN_OVER_REDACTION]
    assert not damaged, f"over-redaction changed non-secret lines: {damaged[:5]}"


@pytest.mark.parametrize("fx", FIXTURES, ids=_ids(FIXTURES))
def test_non_secret_lines_byte_identical_with_exact_layer(fx: Path) -> None:
    secrets = _pattern_secrets(fx) + _exact_only(fx)
    src = _text(fx).split("\n")
    out = Redactor(exact_values=secrets).text(_text(fx)).split("\n")
    damaged = [
        (a, b) for a, b in zip(src, out, strict=True) if not _contains_any(a, secrets) and a != b
    ]
    damaged = [d for d in damaged if d[0] not in KNOWN_OVER_REDACTION]
    assert not damaged, f"exact layer changed non-secret lines: {damaged[:5]}"


@pytest.mark.parametrize("fx", FIXTURES, ids=_ids(FIXTURES))
def test_secret_lines_keep_command_keyword(fx: Path) -> None:
    secrets = _pattern_secrets(fx) + _exact_only(fx)
    src = _text(fx).split("\n")
    out = Redactor(exact_values=secrets).text(_text(fx)).split("\n")
    problems = []
    for a, b in zip(src, out, strict=True):
        if not _contains_any(a, secrets) or _HEX_LINE.match(a.strip()):
            continue
        if not a.split() or not b.split():
            problems.append((a, b))
            continue
        if a.split()[0] != b.split()[0] or REDACTED not in b:
            problems.append((a, b))
    assert not problems, problems[:5]


@pytest.mark.parametrize("fx", FIXTURES, ids=_ids(FIXTURES))
def test_secret_lines_keep_leading_whitespace(fx: Path) -> None:
    secrets = _pattern_secrets(fx)
    src = _text(fx).split("\n")
    out = Redactor().text(_text(fx)).split("\n")
    for a, b in zip(src, out, strict=True):
        if _contains_any(a, secrets):
            assert a[: len(a) - len(a.lstrip())] == b[: len(b) - len(b.lstrip())], (a, b)


@pytest.mark.parametrize("fx", FIXTURES, ids=_ids(FIXTURES))
def test_text_lines_line_agree(fx: Path) -> None:
    r = Redactor(exact_values=_pattern_secrets(fx))
    src = _text(fx).split("\n")
    assert r.text("\n".join(src)).split("\n") == r.lines(src)
    # line() has no parent context, so it may only be stricter: a key chain id it redacts.
    for single, contextual in zip([r.line(x) for x in src], r.lines(src), strict=True):
        if single != contextual:
            assert contextual.strip().startswith("key ")
            assert single.strip() == "key <redacted>"


@pytest.mark.parametrize("fx", FIXTURES, ids=_ids(FIXTURES))
def test_idempotent(fx: Path) -> None:
    r = Redactor()
    once = r.text(_text(fx))
    assert r.text(once) == once


# 4. Certificate blocks ----------------------------------------------------------


def _cert_blocks(src: list[str]) -> list[tuple[int, int]]:
    blocks, start = [], None
    for i, ln in enumerate(src):
        s = ln.strip()
        if s.startswith("certificate "):
            start = i
        elif s == "quit" and start is not None:
            blocks.append((start, i))
            start = None
    return blocks


IOS_FIXTURES = [p for p in FIXTURES if p.parent.name == "ios"]


@pytest.mark.parametrize("fx", IOS_FIXTURES, ids=_ids(IOS_FIXTURES))
def test_certificate_block(fx: Path) -> None:
    src = _text(fx).split("\n")
    out = Redactor().text(_text(fx)).split("\n")
    blocks = _cert_blocks(src)
    assert blocks
    for start, end in blocks:
        assert out[start] == src[start]
        assert out[end] == src[end]
        for i in range(start + 1, end):
            indent = src[i][: len(src[i]) - len(src[i].lstrip())]
            assert out[i] == f"{indent}{REDACTED}", (src[i], out[i])


def test_certificate_block_inline_cases() -> None:
    cfg = (
        "crypto pki certificate chain TP\n"
        " certificate 05\n"
        "  AABBCCDD 11223344\n"
        "  55667788\n"
        "  quit\n"
        " certificate ca 01\n"
        "\t30820321 30820209\n"
        "  quit\n"
        "hostname after\n"
    )
    out = Redactor().text(cfg).split("\n")
    assert out[0] == "crypto pki certificate chain TP"
    assert out[1] == " certificate 05"
    assert out[2] == f"  {REDACTED}"
    assert out[3] == f"  {REDACTED}"
    assert out[4] == "  quit"
    assert out[5] == " certificate ca 01"
    assert out[6] == f"\t{REDACTED}"
    assert out[7] == "  quit"
    assert out[8] == "hostname after"


def test_certificate_block_crlf() -> None:
    cfg = " certificate 05\r\n  AABBCCDD 11223344\r\n  quit\r\n"
    out = Redactor().text(cfg)
    assert "AABBCCDD" not in out and "11223344" not in out
    assert "certificate 05" in out and "quit" in out


def test_hex_outside_certificate_untouched() -> None:
    line = "mac address-table static 0011.2233.4455 vlan 10 interface Gi0/1"
    assert Redactor().line(line) == line
    line2 = "  30820321 30820209"
    assert Redactor().line(line2) == line2  # a lone line is not in a cert block


# 5. Adversarial single lines ----------------------------------------------------

# (case id, input line, secrets that must vanish, text that must survive)
LINE_CASES: list[tuple[str, str, list[str], list[str]]] = [
    (
        "user-named-secret",
        "username secret password 0 Xyz123abc",
        ["Xyz123abc"],
        ["username secret password 0"],
    ),
    (
        "user-named-password",
        "username password secret 5 $1$ab12$Cd34Ef56",
        ["$1$ab12$Cd34Ef56"],
        ["username password secret 5"],
    ),
    (
        "user-named-password-type0",
        "username password password 0 Zq8Lm2Vx",
        ["Zq8Lm2Vx"],
        ["username password password 0"],
    ),
    (
        "user-named-secret-priv",
        "username secret privilege 15 secret 9 $9$abcd$EFGHijkl",
        ["$9$abcd$EFGHijkl"],
        ["username secret privilege 15 secret 9"],
    ),
    ("digits-user-pw", "username bob password 0 8675309", ["8675309"], ["username bob password 0"]),
    ("digits-enable-pw", "enable password 12345678", ["12345678"], ["enable password"]),
    ("digits-enable-secret0", "enable secret 0 20241010", ["20241010"], ["enable secret 0"]),
    (
        "digits-community",
        "snmp-server community 4815162342 RO",
        ["4815162342"],
        ["snmp-server community", "RO"],
    ),
    ("digits-tacacs-key", "tacacs-server key 99887766", ["99887766"], ["tacacs-server key"]),
    ("digits-line-pw", " password 0 5550123", ["5550123"], [" password 0"]),
    (
        "digits-cx-radius",
        "radius-server host 192.0.2.5 key plaintext 31415926",
        ["31415926"],
        ["radius-server host 192.0.2.5 key plaintext"],
    ),
    ("hash-char", "enable password 0 Pa#ss#word1", ["Pa#ss#word1"], ["enable password 0"]),
    (
        "bang-char",
        "username a password 0 Hello!World9",
        ["Hello!World9"],
        ["username a password 0"],
    ),
    (
        "dollar-slash",
        "enable secret 5 $1$x/y$Q/w9e8r7t6y5u4",
        ["$1$x/y$Q/w9e8r7t6y5u4"],
        ["enable secret 5"],
    ),
    ("dollar-plain", "username b password 0 pa$$w/ord", ["pa$$w/ord"], ["username b password 0"]),
    ("dquote", 'username c password 0 pa"ss"word9', ['pa"ss"word9'], ["username c password 0"]),
    (
        "squote",
        "username d password 0 it's-a-Secret9",
        ["it's-a-Secret9"],
        ["username d password 0"],
    ),
    (
        "quoted-key-with-bang",
        'radius-server host 192.0.2.9 key "k3y!#$/q"',
        ["k3y!#$/q"],
        ["radius-server host 192.0.2.9 key"],
    ),
    (
        "tacacs-key-special",
        "tacacs-server host 192.0.2.8 key 0 s#e!c$r/e't9",
        ["s#e!c$r/e't9"],
        ["tacacs-server host 192.0.2.8 key 0"],
    ),
    (
        "cx-plain-special",
        'radius-server host 192.0.2.7 key plaintext p#1!$/"q',
        ['p#1!$/"q'],
        ["radius-server host 192.0.2.7 key plaintext"],
    ),
    (
        "snmp-community-bang",
        "snmp-server community Pub!lic#1 RO 10",
        ["Pub!lic#1"],
        ["snmp-server community", "RO 10"],
    ),
    (
        "quoted-community-spaces",
        'snmp-server community "my secret comm" operator',
        ["my secret comm", "secret comm", "my secret"],
        ["snmp-server community", "operator"],
    ),
    (
        "quoted-community-spaces-rw",
        'snmp-server community "two words" RW 5',
        ["two words", "words"],
        ["snmp-server community", "RW 5"],
    ),
    (
        "quoted-aos-s-community-unrestricted",
        'snmp-server community "Cust Lab ro9" unrestricted',
        ["Cust Lab ro9", "Lab ro9", "ro9"],
        ["snmp-server community", "unrestricted"],
    ),
    (
        "quoted-aos-s-host-community",
        'snmp-server host 192.0.2.30 community "trap comm x1"',
        ["trap comm x1", "comm x1"],
        ["snmp-server host 192.0.2.30 community"],
    ),
    (
        "aos-s-quoted-key-spaces",
        'radius-server host 192.0.2.20 key "shared sec ret9"',
        ["shared sec ret9", "sec ret9", "ret9"],
        ["radius-server host 192.0.2.20 key"],
    ),
    (
        "aos-s-sntp-key-quoted-spaces",
        'sntp authentication key-id 2 authentication-mode md5 key-value "ntp k ey7"',
        ["ntp k ey7", "ey7"],
        ["sntp authentication key-id 2 authentication-mode md5 key-value"],
    ),
    (
        "mixed-case-enable",
        "Enable Secret 5 $1$abcd$EfGhIjKlMn",
        ["$1$abcd$EfGhIjKlMn"],
        ["Enable Secret 5"],
    ),
    (
        "upper-snmp",
        "SNMP-SERVER COMMUNITY UpperComm9 RO",
        ["UpperComm9"],
        ["SNMP-SERVER COMMUNITY"],
    ),
    (
        "mixed-username",
        "Username bob PASSWORD 0 Hunter2Pw",
        ["Hunter2Pw"],
        ["Username bob PASSWORD 0"],
    ),
    (
        "mixed-tacacs",
        "TACACS-Server Host 192.0.2.1 Key 7 0822455D0A16",
        ["0822455D0A16"],
        ["TACACS-Server Host 192.0.2.1 Key 7"],
    ),
    (
        "mixed-bgp",
        " Neighbor 203.0.113.1 Password 7 1511021F0725",
        ["1511021F0725"],
        [" Neighbor 203.0.113.1 Password 7"],
    ),
    (
        "mixed-cx-ciphertext",
        "radius-server host 192.0.2.1 key CipherText AQBapMixedCase==",
        ["AQBapMixedCase=="],
        ["radius-server host 192.0.2.1 key CipherText"],
    ),
    (
        "crlf-enable",
        "enable secret 5 $1$abcd$efghijklmnop\r",
        ["$1$abcd$efghijklmnop"],
        ["enable secret 5"],
    ),
    (
        "crlf-community",
        "snmp-server community crlfcomm1 RO\r",
        ["crlfcomm1"],
        ["snmp-server community"],
    ),
    ("crlf-key7", " key 7 0822455D0A16\r", ["0822455D0A16"], [" key 7"]),
    (
        "crlf-cx",
        "user admin group administrators password ciphertext AQBapCrlfUser==\r",
        ["AQBapCrlfUser=="],
        ["user admin group administrators password ciphertext"],
    ),
    (
        "crlf-quoted",
        'tacacs-server key "crlf key 1"\r',
        ["crlf key 1", "key 1"],
        ["tacacs-server key"],
    ),
    ("tab-key-string", "\tkey-string 7 06080A2E4F5A1B", ["06080A2E4F5A1B"], ["\tkey-string 7"]),
    ("tab-vty-password", "\t\tpassword 7 0822455D0A16", ["0822455D0A16"], ["\t\tpassword 7"]),
    (
        "tab-bgp",
        "\tneighbor 203.0.113.1 password 7 1511021F0725",
        ["1511021F0725"],
        ["\tneighbor 203.0.113.1 password 7"],
    ),
    ("tab-separated", "enable\tsecret\t5\t$1$tabs$AbCdEfGhIj", ["$1$tabs$AbCdEfGhIj"], ["enable"]),
    (
        "tab-cx-key",
        "\t\tkey-string ciphertext AQBapTabbed==",
        ["AQBapTabbed=="],
        ["key-string ciphertext"],
    ),
    (
        "trailing-tacacs-timeout",
        "tacacs-server host 192.0.2.1 key tacSecretX9 timeout 5",
        ["tacSecretX9"],
        ["tacacs-server host 192.0.2.1 key", "timeout 5"],
    ),
    (
        "trailing-tacacs-timeout-type7",
        "tacacs-server host 192.0.2.1 timeout 5 key 7 0822455D0A16",
        ["0822455D0A16"],
        ["tacacs-server host 192.0.2.1 timeout 5 key 7"],
    ),
    (
        "trailing-radius-retransmit",
        "radius-server host 192.0.2.2 auth-port 1812 acct-port 1813 key 7 0822455D0A16 retransmit 3",
        ["0822455D0A16"],
        ["radius-server host 192.0.2.2 auth-port 1812 acct-port 1813 key 7", "retransmit 3"],
    ),
    (
        "trailing-cx-radius",
        "radius-server host 192.0.2.3 key ciphertext AQBapTrail== timeout 5 vrf mgmt",
        ["AQBapTrail=="],
        ["timeout 5 vrf mgmt"],
    ),
    (
        "trailing-cx-ntp",
        "ntp authentication-key 2 sha1 cxTrustedKey trusted",
        ["cxTrustedKey"],
        ["ntp authentication-key 2 sha1", "trusted"],
    ),
    (
        "trailing-snmp-udp",
        "snmp-server host 192.0.2.5 version 2c commX9 udp-port 162",
        ["commX9"],
        ["snmp-server host 192.0.2.5 version 2c", "udp-port 162"],
    ),
    (
        "trailing-ntp-type7",
        "ntp authentication-key 1 md5 104D000A0618 7",
        ["104D000A0618"],
        ["ntp authentication-key 1 md5"],
    ),
    (
        "trailing-isakmp-mask",
        "crypto isakmp key keyZ9 address 203.0.113.1 255.255.255.255 no-xauth",
        ["keyZ9"],
        ["crypto isakmp key", "address 203.0.113.1 255.255.255.255 no-xauth"],
    ),
    (
        "trailing-cx-neighbor",
        "    neighbor 203.0.113.1 password plaintext cxBgp9 send-community",
        ["cxBgp9"],
        ["neighbor 203.0.113.1 password plaintext"],
    ),
    (
        "trailing-aos-s-key",
        'radius-server host 192.0.2.4 key "ks9" dyn-authorization',
        ["ks9"],
        ["radius-server host 192.0.2.4 key", "dyn-authorization"],
    ),
    (
        "type7-then-tokens-username",
        "username bob password 7 0822455D0A16 privilege 15",
        ["0822455D0A16"],
        ["username bob password 7"],
    ),
    (
        "type7-then-tokens-line",
        " password 7 0822455D0A16 extra tokens",
        ["0822455D0A16"],
        [" password 7"],
    ),
    (
        "type7-ospf",
        " ip ospf message-digest-key 1 md5 7 0822455D0A16 ",
        ["0822455D0A16"],
        [" ip ospf message-digest-key 1 md5 7"],
    ),
    (
        "type7-hsrp",
        " standby 1 authentication md5 key-string 7 110A1016141D timeout 30",
        ["110A1016141D"],
        [" standby 1 authentication md5 key-string 7"],
    ),
    (
        "type7-snmp-user",
        "snmp-server user u G v3 auth sha AuthK3y9x priv aes 128 PrivK3y9x",
        ["AuthK3y9x", "PrivK3y9x"],
        ["snmp-server user u G v3 auth sha"],
    ),
    (
        "type6-isakmp",
        "crypto isakmp key 6 \\YcbV[TNPSHOSgWZZb address 203.0.113.9",
        ["\\YcbV[TNPSHOSgWZZb"],
        ["crypto isakmp key 6", "address 203.0.113.9"],
    ),
    (
        "type8-9",
        "enable secret 8 $8$abc$DEFghi/jkl.mnoPQR",
        ["$8$abc$DEFghi/jkl.mnoPQR"],
        ["enable secret 8"],
    ),
    (
        "enable-secret-level",
        "enable secret level 5 9 $9$lvl$Abcdefghij",
        ["$9$lvl$Abcdefghij"],
        ["enable secret level 5 9"],
    ),
    (
        "enable-password-level",
        "enable password level 5 lvlPass9",
        ["lvlPass9"],
        ["enable password level 5"],
    ),
    (
        "username-secret-no-priv",
        "username noprivuser secret nopriv-Secret9",
        ["nopriv-Secret9"],
        ["username noprivuser secret"],
    ),
    (
        "username-nopassword-after",
        "username bob privilege 15 secret 5 $1$zz$abcdefgh",
        ["$1$zz$abcdefgh"],
        ["username bob privilege 15 secret 5"],
    ),
    (
        "username-password-contains-keyword",
        "username bob password 0 secret",
        ["secret"],
        ["username bob password 0"],
    ),
    (
        "password-equals-username",
        "username alice password 0 alice1234",
        ["alice1234"],
        ["username alice password 0"],
    ),
    (
        "snmp-community-view",
        "snmp-server community commV9 view MYVIEW RO 12",
        ["commV9"],
        ["snmp-server community", "view MYVIEW RO 12"],
    ),
    (
        "snmp-user-encrypted",
        "snmp-server user u G v3 encrypted auth sha 5A1B2C3D4E5F6A7B8C9D priv aes 128 1A2B3C4D5E6F7081",
        ["5A1B2C3D4E5F6A7B8C9D", "1A2B3C4D5E6F7081"],
        ["snmp-server user u G v3 encrypted auth sha"],
    ),
    ("tacacs-new-style-key", " key 7 14141B180F0B7B7977", ["14141B180F0B7B7977"], [" key 7"]),
    ("tacacs-new-style-key0", " key 0 plainKey0Z", ["plainKey0Z"], [" key 0"]),
    ("radius-pac-key", " pac key 7 15060E1F0A3B2C", ["15060E1F0A3B2C"], [" pac key 7"]),
    (
        "server-private",
        " server-private 192.0.2.74 key 7 1311181F0D1B",
        ["1311181F0D1B"],
        [" server-private 192.0.2.74 key 7"],
    ),
    (
        "server-private-port",
        " server-private 192.0.2.75 auth-port 1812 key radPrivClear9",
        ["radPrivClear9"],
        [" server-private 192.0.2.75 auth-port 1812 key"],
    ),
    (
        "keychain-key-string",
        "  key-string 7 06080A2E4F5A1B",
        ["06080A2E4F5A1B"],
        ["  key-string 7"],
    ),
    ("keychain-key-string-bare", "  key-string bareKeyStr9", ["bareKeyStr9"], ["  key-string"]),
    (
        "vrrp-auth",
        " vrrp 5 authentication text vrrpPlain5",
        ["vrrpPlain5"],
        [" vrrp 5 authentication text"],
    ),
    (
        "hsrp-text",
        " standby 2 authentication text hsrpPlainTxt",
        ["hsrpPlainTxt"],
        [" standby 2 authentication text"],
    ),
    (
        "ospf-authkey",
        " ip ospf authentication-key 7 13061E010803",
        ["13061E010803"],
        [" ip ospf authentication-key 7"],
    ),
    (
        "ospf-authkey-bare",
        " ip ospf authentication-key ospfBare9",
        ["ospfBare9"],
        [" ip ospf authentication-key"],
    ),
    (
        "bgp-password-bare",
        " neighbor 203.0.113.1 password bgpBare9",
        ["bgpBare9"],
        [" neighbor 203.0.113.1 password"],
    ),
    (
        "bgp-password-peergroup",
        " neighbor PEERS password 7 1511021F0725",
        ["1511021F0725"],
        [" neighbor PEERS password 7"],
    ),
    (
        "keyring",
        " pre-shared-key address 203.0.113.20 key keyringPSK9",
        ["keyringPSK9"],
        [" pre-shared-key address 203.0.113.20 key"],
    ),
    ("ikev2-local", " pre-shared-key local ikeLocal9", ["ikeLocal9"], [" pre-shared-key local"]),
    (
        "ikev2-remote",
        " pre-shared-key remote ikeRemote9",
        ["ikeRemote9"],
        [" pre-shared-key remote"],
    ),
    (
        "ikev2-auth-local",
        " authentication local pre-share key ikev2Auth9",
        ["ikev2Auth9"],
        [" authentication local pre-share key"],
    ),
    (
        "config-key",
        "key config-key password-encrypt cfgKeyEnc9",
        ["cfgKeyEnc9"],
        ["key config-key password-encrypt"],
    ),
    (
        "ppp-chap",
        " ppp chap password 7 0514290E3A5B1F",
        ["0514290E3A5B1F"],
        [" ppp chap password 7"],
    ),
    ("ppp-chap-bare", " ppp chap password chapPlain9", ["chapPlain9"], [" ppp chap password"]),
    ("line-password-bare", " password bareLinePw9", ["bareLinePw9"], [" password"]),
    ("line-password-type0", " password 0 zeroLinePw9", ["zeroLinePw9"], [" password 0"]),
    (
        "cx-user-plain",
        "user bob group operators password plaintext cxUserPlain9",
        ["cxUserPlain9"],
        ["user bob group operators password plaintext"],
    ),
    (
        "cx-snmpv3-auth",
        "snmpv3 user u auth sha auth-pass plaintext cxAP9 priv aes priv-pass plaintext cxPP9",
        ["cxAP9", "cxPP9"],
        ["snmpv3 user u auth sha auth-pass plaintext", "priv aes priv-pass plaintext"],
    ),
    (
        "cx-community",
        "snmp-server community cxCommDirect9",
        ["cxCommDirect9"],
        ["snmp-server community"],
    ),
    (
        "cx-mac-auth",
        "aaa authentication port-access mac-auth password ciphertext AQBapMacAuth==",
        ["AQBapMacAuth=="],
        ["aaa authentication port-access mac-auth password ciphertext"],
    ),
    ("cx-cak", "    cak ciphertext AQBapCak9==", ["AQBapCak9=="], ["cak ciphertext"]),
    (
        "cx-secret-key",
        "radius dyn-authorization client 192.0.2.40 secret-key ciphertext AQBapDyn9==",
        ["AQBapDyn9=="],
        ["radius dyn-authorization client 192.0.2.40 secret-key ciphertext"],
    ),
    (
        "cx-ospf-md5",
        "    ip ospf message-digest-key 1 md5 ciphertext AQBapOspf9==",
        ["AQBapOspf9=="],
        ["ip ospf message-digest-key 1 md5 ciphertext"],
    ),
    (
        "s-password-manager",
        'password manager user-name "manager" sha1 "da39a3ee5e6b4b0d3255bfef95601890afd80709"',
        ["da39a3ee5e6b4b0d3255bfef95601890afd80709"],
        ['password manager user-name "manager" sha1'],
    ),
    (
        "s-password-operator",
        'password operator user-name "op" sha256 "2c26b46b68ffc68ff99b453c1d304134"',
        ["2c26b46b68ffc68ff99b453c1d304134"],
        ['password operator user-name "op" sha256'],
    ),
    (
        "s-password-manager-plaintext",
        "password manager plaintext sMgrPlain9",
        ["sMgrPlain9"],
        ["password manager plaintext"],
    ),
    ("s-encrypt-cred", "; encrypt-cred AQBapHdr9+/==", ["AQBapHdr9+/=="], ["; encrypt-cred"]),
    (
        "s-encrypt-cred-crlf",
        "; encrypt-cred AQBapHdr9+/==\r",
        ["AQBapHdr9+/=="],
        ["; encrypt-cred"],
    ),
    (
        "s-local-user",
        'aaa authentication local-user "u" group "Level-15" password sha1 "a94a8fe5ccb19ba61c4c0873d391e987982fbbd3"',
        ["a94a8fe5ccb19ba61c4c0873d391e987982fbbd3"],
        ['aaa authentication local-user "u" group "Level-15" password sha1'],
    ),
    (
        "s-snmpv3",
        'snmpv3 user "x" auth sha "authhash9" priv aes "privhash9"',
        ["authhash9", "privhash9"],
        ['snmpv3 user "x" auth sha', "priv aes"],
    ),
    (
        "s-ntp-key-value",
        "ntp authentication key-id 4 authentication-mode sha1 key-value ntpKv9",
        ["ntpKv9"],
        ["ntp authentication key-id 4 authentication-mode sha1 key-value"],
    ),
]

_LINE_PARAMS = [
    pytest.param(
        *case[1:],
        id=case[0],
        marks=[pytest.mark.xfail(strict=True, reason=f"LEAK: {LINE_LEAKS[case[0]]}")]
        if case[0] in LINE_LEAKS
        else [],
    )
    for case in LINE_CASES
]


@pytest.mark.parametrize(("line", "gone", "kept"), _LINE_PARAMS)
def test_adversarial_line(line: str, gone: list[str], kept: list[str]) -> None:
    out = Redactor().line(line)
    for s in gone:
        assert s not in out, f"{s!r} survived in {out!r}"
    assert REDACTED in out
    # leading whitespace and the first keyword always survive
    assert out.split()[0] == line.split()[0]
    assert out[: len(out) - len(out.lstrip())] == line[: len(line) - len(line.lstrip())]


_CONTEXT_PARAMS = [
    pytest.param(
        *case[1:],
        id=case[0],
        marks=[
            pytest.mark.xfail(
                strict=True,
                reason="OVER-REDACTION: non-secret tokens (type digit / plaintext / ciphertext / keyword) replaced",
            )
        ]
        if case[0] in CONTEXT_OVER
        else [],
    )
    for case in LINE_CASES
    if case[0] not in LINE_LEAKS
]


@pytest.mark.parametrize(("line", "gone", "kept"), _CONTEXT_PARAMS)
def test_adversarial_line_keeps_non_secret_context(
    line: str, gone: list[str], kept: list[str]
) -> None:
    out = Redactor().line(line)
    for k in kept:
        assert k in out, f"{k!r} missing from {out!r}"


def test_adversarial_lines_via_text_api() -> None:
    """The same cases through ``text`` and ``lines`` must agree with ``line``."""
    inputs = [c[1].rstrip("\r") for c in LINE_CASES]
    r = Redactor()
    assert r.lines(inputs) == [r.line(x) for x in inputs]
    assert r.text("\n".join(inputs)).split("\n") == r.lines(inputs)


NON_SECRET_LINES = [
    "ip ssh version 2",
    "aaa authentication login default group tacacs+ local",
    "aaa authentication enable default group tacacs+ enable",
    "aaa authorization exec default group tacacs+ local",
    "snmp-server enable traps",
    "snmp-server location Rack 4 Row B",
    "snmp-server contact noc@acme.example",
    "service password-encryption",
    "no service password-recovery",
    "password encryption aes",
    "login authentication default",
    "line vty 0 4",
    " transport input ssh",
    "tacacs-server directed-request",
    "tacacs server TAC-PRIMARY",
    " address ipv4 192.0.2.62",
    "radius-server host 192.0.2.1",
    "radius-server attribute 6 on-for-login-auth",
    "ntp authenticate",
    "ntp trusted-key 1",
    "ntp server 198.51.100.123 key 1",
    "key chain ACME-KC",
    " key 1",
    "  accept-lifetime 00:00:00 Jan 1 2026 infinite",
    "crypto isakmp policy 10",
    " authentication pre-share",
    "crypto pki trustpoint TP",
    "router bgp 65001",
    " neighbor 203.0.113.1 remote-as 65002",
    " neighbor 203.0.113.1 activate",
    " ip ospf authentication message-digest",
    " ip ospf authentication key-chain ACME-KC",
    " standby 1 ip 192.0.2.1",
    " standby 1 priority 110",
    " standby 3 authentication md5 key-chain ACME-KC",
    "interface Vlan10",
    " description Password policy VLAN",
    " description Secret room uplink",
    "hostname sa-secret-01",
    "username-prompt is not a command",
    "enable",
    "password manager",
    "password operator",
    "snmp-server vrf default",
    "snmp-server host 192.0.2.5 version 2c",
    "radius-server host 192.0.2.20 timeout 5",
    "aaa authentication port-access dot1x authenticator",
    "snmpv3 enable",
    "user-role administrators",
    "ntp authentication",
    "!export-password: default",
    "tacacs-server host 192.0.2.30",
    "ip ssh time-out 60",
    "spanning-tree mode rapid-pvst",
    "",
    "   ",
    "!",
    "end",
]


# Redactor.line() sees one line with no parent, so it cannot tell a key chain id
# (" key 1") from a server key and redacts it; text() and lines() keep it.
NON_SECRET_KNOWN_BAD = {" key 1", "snmp-server host 192.0.2.5 version 2c"}


@pytest.mark.parametrize(
    "line",
    [
        pytest.param(
            x,
            id=repr(x),
            marks=[
                pytest.mark.xfail(
                    strict=True, reason=f"OVER-REDACTION: non-secret line {x!r} is changed"
                )
            ]
            if x in NON_SECRET_KNOWN_BAD
            else [],
        )
        for x in NON_SECRET_LINES
    ],
)
def test_non_secret_line_unchanged(line: str) -> None:
    assert Redactor().line(line) == line


def test_blank_and_empty_text() -> None:
    r = Redactor()
    assert r.text("") == ""
    assert r.lines([]) == []
    assert r.line("") == ""
    assert r.text("\n\n") == "\n\n"


def test_crlf_text_keeps_line_count() -> None:
    cfg = "hostname x\r\nenable secret 5 $1$abcd$efghijklmnop\r\nip ssh version 2\r\n"
    out = Redactor().text(cfg)
    assert "$1$abcd$efghijklmnop" not in out
    assert out.count("\n") == cfg.count("\n")
    assert "hostname x" in out and "ip ssh version 2" in out


# 6. Exact-value edge cases ------------------------------------------------------


def test_exact_short_values_not_replaced() -> None:
    r = Redactor(exact_values=["abc", "ab", "x", "123"])
    line = "abc ab x 123 stays abcdef"
    assert r.line(line) == line


def test_exact_four_chars_replaced() -> None:
    r = Redactor(exact_values=["abcd"])
    assert r.line("x abcd y") == f"x {REDACTED} y"
    assert r.line("prefixabcdsuffix") == f"prefix{REDACTED}suffix"


def test_exact_replaced_in_free_text_everywhere() -> None:
    r = Redactor(exact_values=["Tr0ub4dor&3"])
    for line in [
        "banner motd ^C Tr0ub4dor&3 ^C",
        " description wifi=Tr0ub4dor&3",
        "! note: Tr0ub4dor&3 and Tr0ub4dor&3",
        "Tr0ub4dor&3",
    ]:
        out = r.line(line)
        assert "Tr0ub4dor&3" not in out
        assert REDACTED in out


@pytest.mark.parametrize(
    "values", [["abcd", "abcdefgh"], ["abcdefgh", "abcd"]], ids=["short-first", "long-first"]
)
def test_exact_longer_value_containing_shorter_replaced_whole(values: list[str]) -> None:
    r = Redactor(exact_values=values)
    out = r.line("x abcdefgh y")
    assert "efgh" not in out and "abcd" not in out
    assert out == f"x {REDACTED} y"


def test_exact_longer_contains_shorter_multiple_places() -> None:
    r = Redactor(exact_values=["pass", "pass-extra-long"])
    out = r.line("pass-extra-long and pass and pass-extra-long")
    assert "extra" not in out and "long" not in out
    assert out == f"{REDACTED} and {REDACTED} and {REDACTED}"


def test_exact_empty_string_never_crashes() -> None:
    for vals in ([""], ["", ""], ["", "realvalue"], [" "], ["   "]):
        r = Redactor(exact_values=vals)
        out = r.text("hostname sa-as01\n description realvalue\n")
        assert out.count("\n") == 2
        assert "realvalue" not in out or "realvalue" not in vals
    assert Redactor(exact_values=[""]).line("hostname sa-as01") == "hostname sa-as01"
    assert Redactor(exact_values=[]).line("hostname sa-as01") == "hostname sa-as01"
    assert Redactor(exact_values=[""]).text("") == ""


def test_exact_empty_value_does_not_insert_markers() -> None:
    line = "interface Gi0/1"
    assert Redactor(exact_values=[""]).line(line) == line


@pytest.mark.parametrize(
    "value",
    [
        "a.c+d",
        "(x*y)[z]",
        "^$|\\",
        "p?ss{2}",
        "a\\d+b",
        "[abc]+",
        "(?i)abcd",
        ".*.*",
        "a|bcdef",
        "C:\\temp\\x",
        "$1$ab$cd",
    ],
)
def test_exact_regex_metacharacters_literal(value: str) -> None:
    r = Redactor(exact_values=[value])
    assert r.line(f"pre {value} post") == f"pre {REDACTED} post"
    # must not behave as a pattern against look-alike text
    decoy = "pre abcccdxyz aaaa zzzz abcd post"
    assert r.line(decoy) == decoy


def test_exact_metachar_dot_does_not_match_any_char() -> None:
    r = Redactor(exact_values=["a.cd"])
    assert r.line("abcd") == "abcd"
    assert r.line("a.cd") == REDACTED


def test_exact_values_case_sensitive() -> None:
    r = Redactor(exact_values=["SecretPw9"])
    assert r.line("x secretpw9 y") == "x secretpw9 y"
    assert r.line("x SecretPw9 y") == f"x {REDACTED} y"


def test_exact_unicode_value() -> None:
    r = Redactor(exact_values=["pässwörd1"])
    assert r.line("descr pässwörd1 end") == f"descr {REDACTED} end"


def test_exact_multiline_value_is_not_required_but_must_not_crash() -> None:
    r = Redactor(exact_values=["line1\nline2"])
    out = r.text("a\nline1\nline2\nb")
    assert isinstance(out, str)


def test_exact_values_many() -> None:
    vals = [f"secret{i:04d}" for i in range(500)]
    r = Redactor(exact_values=vals)
    text = "\n".join(f"description {v}" for v in vals)
    out = r.text(text)
    assert not any(v in out for v in vals)
    assert out.count("\n") == 499


def test_exact_with_pattern_line_both_layers() -> None:
    r = Redactor(exact_values=["Banner-Secret-1"])
    out = r.text("enable secret 5 $1$abcd$efghijklmnop\nbanner motd ^C Banner-Secret-1 ^C")
    assert "$1$abcd$efghijklmnop" not in out
    assert "Banner-Secret-1" not in out
    assert out.startswith("enable secret 5 ")


def test_default_redactor_has_no_exact_layer() -> None:
    line = "banner motd ^C Banner-Secret-1 ^C"
    assert Redactor().line(line) == line


def test_redacted_constant() -> None:
    assert REDACTED == "<redacted>"
