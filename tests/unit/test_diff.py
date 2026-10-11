"""Missing and extra lines, secret-line comparison, compliance report."""

from __future__ import annotations

import json

from tsunami.core.diff import DiffResult, compliance_report, diff_config
from tsunami.core.redact import Redactor

RUNNING_IOS = """\
Building configuration...
hostname sw-acme-01
ntp server 192.0.2.10
ntp server 192.0.2.11
ntp server 192.0.2.99
ntp server-key 5
interface Vlan10
 ip address 10.1.1.5 255.255.255.0
 no shutdown
end
"""


def test_unchanged_is_clean() -> None:
    rendered = "interface Vlan10\n ip address 10.1.1.5 255.255.255.0\n"
    result = diff_config(rendered, RUNNING_IOS, "ios")
    assert result.clean and result == DiffResult((), ())


def test_changed_ip_reports_full_path() -> None:
    rendered = "interface Vlan10\n ip address 10.1.1.6 255.255.255.0\n no shutdown\n"
    result = diff_config(rendered, RUNNING_IOS, "ios")
    assert result.missing == ("interface Vlan10 > ip address 10.1.1.6 255.255.255.0",)
    assert result.extra == ()
    assert not result.clean


def test_whitespace_and_crlf_are_not_drift() -> None:
    rendered = "interface   Vlan10\r\n  ip address  10.1.1.5 255.255.255.0  \r\n"
    assert diff_config(rendered, RUNNING_IOS.replace("\n", "\r\n"), "ios").clean


def test_rendered_as_line_sequence() -> None:
    assert diff_config(["hostname sw-acme-01"], RUNNING_IOS, "ios").clean


def test_parent_missing_reports_parent_and_child() -> None:
    result = diff_config("interface Vlan20\n no shutdown\n", RUNNING_IOS, "ios")
    assert result.missing == ("interface Vlan20", "interface Vlan20 > no shutdown")


def test_stale_ntp_server_is_extra() -> None:
    rendered = "ntp server 192.0.2.10\nntp server 192.0.2.11\n"
    result = diff_config(rendered, RUNNING_IOS, "ios", owns=["ntp server"])
    assert result.missing == ()
    assert result.extra == ("ntp server 192.0.2.99",)


def test_owns_word_boundary_only() -> None:
    result = diff_config("hostname x\n", RUNNING_IOS, "ios", owns=["ntp server"])
    assert "ntp server-key 5" not in result.extra
    assert len(result.extra) == 3
    assert diff_config("", "ntp serverx 1\n", "ios", owns=["ntp server"]).extra == ()


def test_owns_top_level_only() -> None:
    running = "interface Vlan10\n ntp server 192.0.2.50\n"
    assert diff_config("", running, "ios", owns=["ntp server"]).clean


def test_owns_several_prefixes_in_source_order() -> None:
    running = "snmp-server host 192.0.2.5\nntp server 192.0.2.9\nhostname a\n"
    result = diff_config("hostname a\n", running, "ios", owns=["ntp server", "snmp-server host"])
    assert result.extra == ("snmp-server host 192.0.2.5", "ntp server 192.0.2.9")


def test_no_owns_means_no_extra() -> None:
    assert diff_config("", RUNNING_IOS, "ios").extra == ()


def test_ios_tacacs_secret_compared_by_key() -> None:
    rendered = "tacacs server ISE1\n key ********\n"
    running = "tacacs server ISE1\n address ipv4 192.0.2.30\n key 7 0822455D0A16\n"
    assert diff_config(rendered, running, "ios").clean


def test_ios_key_chain_key_id_is_not_a_secret() -> None:
    result = diff_config("key chain KC1\n key 2\n", "key chain KC1\n key 1\n", "ios")
    assert result.missing == ("key chain KC1 > key 2",)


def test_aoscx_plaintext_vs_ciphertext() -> None:
    rendered = "radius-server host 192.0.2.20 key plaintext ********\n"
    running = "radius-server host 192.0.2.20 key ciphertext AQBapEXAMPLE3EXAMPLE3==\n"
    assert diff_config(rendered, running, "aos-cx").clean


def test_aoscx_secret_line_for_other_host_is_missing() -> None:
    rendered = "radius-server host 192.0.2.21 key plaintext ********\n"
    running = "radius-server host 192.0.2.20 key ciphertext AQBapEXAMPLE3==\n"
    result = diff_config(rendered, running, "aos-cx")
    assert result.missing == ("radius-server host 192.0.2.21 key",)


def test_aoss_hidden_credentials() -> None:
    rendered = 'radius-server host 192.0.2.20 key "********"\n'
    running = "radius-server host 192.0.2.20\n"
    assert diff_config(rendered, running, "aos-s").clean


def test_aoss_hidden_credentials_other_host_still_missing() -> None:
    rendered = 'radius-server host 192.0.2.21 key "********"\n'
    result = diff_config(rendered, "radius-server host 192.0.2.20\n", "aos-s")
    assert result.missing == ("radius-server host 192.0.2.21 key",)


def test_aoss_snippet_vs_running_with_exit() -> None:
    rendered = 'vlan 10\n   name "Users"\n'
    running = '; header\nvlan 10\n   name "Users"\n   untagged 1-4\n   exit\n'
    assert diff_config(rendered, running, "aos-s").clean


def test_aoscx_nested_diff() -> None:
    running = "interface 1/1/1\n    no shutdown\n    vlan access 10\n"
    result = diff_config("interface 1/1/1\n    vlan access 20\n", running, "aos-cx")
    assert result.missing == ("interface 1/1/1 > vlan access 20",)


def test_no_secret_leaks() -> None:
    secret = "0822455D0A16"
    cipher = "AQBapSECRETSECRETSECRET=="
    running = (
        f"tacacs server ISE1\n key 7 {secret}\n"
        f"username bob secret 9 {cipher}\n"
        f"ntp authentication-key 1 md5 {secret}\n"
        f"snmp-server community {secret}\n"
    )
    rendered = "tacacs server ISE2\n key ********\nsnmp-server community ********\n"
    redactor = Redactor([secret, cipher])
    result = diff_config(
        rendered,
        running,
        "ios",
        owns=["username", "ntp authentication-key", "snmp-server"],
        redactor=redactor,
    )
    assert result.missing and result.extra
    blob = json.dumps([list(result.missing), list(result.extra), compliance_report({"sw": result})])
    assert secret not in blob and cipher not in blob


def test_compliance_report_totals() -> None:
    results = {
        "sw-a": DiffResult(("a > b", "c"), ("ntp server 192.0.2.9",)),
        "sw-b": DiffResult((), ()),
    }
    report = compliance_report(results)
    assert report["devices"]["sw-a"] == {
        "missing_count": 2,
        "extra_count": 1,
        "missing": ["a > b", "c"],
        "extra": ["ntp server 192.0.2.9"],
    }
    assert report["devices"]["sw-b"]["missing_count"] == 0
    assert report["total_missing"] == 2
    assert report["total_extra"] == 1
    assert report["total_devices"] == 2
    assert report["compliant_devices"] == 1
    assert json.loads(json.dumps(report)) == report
