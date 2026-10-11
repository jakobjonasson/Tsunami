"""MCP policy model, loader and show-allowlist matcher."""

from __future__ import annotations

import pytest

from tsunami.core.models.policy import McpPolicy
from tsunami.core.policy import PolicyError, check_show_command, load_policy, save_policy


def test_defaults_match_template():
    p = McpPolicy()
    assert p.enabled is False and p.apply == "lab_only" and p.lab_allow_drafts is True
    assert (p.max_check_targets, p.rate_limit_per_minute, p.approval_expiry_minutes) == (50, 30, 60)
    assert "show version" in p.show_allowlist["ios"]
    assert "count" in p.pipes["aos-cx"]


def test_missing_file_is_disabled(tmp_path):
    assert load_policy(tmp_path / "nope.yml").enabled is False


def test_round_trip_keeps_comments(tmp_path):
    path = tmp_path / "mcp-policy.yml"
    path.write_text(
        "# top comment\nenabled: false  # keep me\napply: lab_only\ncustomers_blocked: []\n",
        encoding="utf-8",
    )
    policy = load_policy(path)
    save_policy(policy.model_copy(update={"enabled": True, "customers_blocked": ["acme"]}), path)
    text = path.read_text(encoding="utf-8")
    assert "# top comment" in text and "# keep me" in text
    back = load_policy(path)
    assert back.enabled is True and back.customers_blocked == ["acme"]
    assert back.show_allowlist == policy.show_allowlist


def test_save_new_file_loads_back(tmp_path):
    path = tmp_path / "sub" / "p.yml"
    save_policy(McpPolicy(enabled=True, max_check_targets=7), path)
    back = load_policy(path)
    assert back.enabled and back.max_check_targets == 7


@pytest.mark.parametrize("text", ["enabled: [", "enabled: maybe\n", "bogus: 1\n", "- a\n"])
def test_invalid_files_raise(tmp_path, text):
    path = tmp_path / "p.yml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(PolicyError):
        load_policy(path)


def test_model_rejects_bad_entries():
    with pytest.raises(ValueError):
        McpPolicy(pipes={"ios": ["include", "redirect"]})
    with pytest.raises(ValueError):
        McpPolicy(show_allowlist={"ios": ["configure terminal"]})
    with pytest.raises(ValueError):
        McpPolicy(show_allowlist={"ios": ["show version | include x"]})
    with pytest.raises(ValueError):
        McpPolicy(max_check_targets=0)


@pytest.mark.parametrize(
    "command",
    [
        "sh run",
        "show running-config | redirect flash:x",
        "show running-config | tee x",
        "show running-config | append x",
        "show running-config | include a | include b",
        "show version; reload",
        "show version\nreload",
        "show version\rreload",
        "show version\x07",
        "show version\tx",
        "configure terminal",
        "show",
        "show running-config |",
        "",
    ],
)
def test_rejected(command):
    with pytest.raises(PolicyError):
        check_show_command(McpPolicy(), "ios", command)


def test_allowed():
    p = McpPolicy()
    assert (
        check_show_command(p, "ios", "show  interfaces   GigabitEthernet1/0/1")
        == "show interfaces GigabitEthernet1/0/1"
    )
    assert (
        check_show_command(p, "ios", "show running-config |  include ntp")
        == "show running-config | include ntp"
    )
    assert check_show_command(p, "aos-cx", "show interface brief | count") == (
        "show interface brief | count"
    )
    assert check_show_command(p, "aos-s", "show vlans") == "show vlans"


def test_families_are_independent():
    p = McpPolicy()
    with pytest.raises(PolicyError):
        check_show_command(p, "ios", "show vlans")
    with pytest.raises(PolicyError):
        check_show_command(p, "ios", "show interface brief | count")
    with pytest.raises(PolicyError):
        check_show_command(p, "aos-s", "show vlan")  # token must match exactly
    with pytest.raises(PolicyError):
        check_show_command(p, "nxos", "show version")


def test_forbidden_pipe_blocked_even_if_policy_object_forged():
    p = McpPolicy().model_copy(update={"pipes": {"ios": ["redirect"]}})
    with pytest.raises(PolicyError):
        check_show_command(p, "ios", "show version | redirect x")
