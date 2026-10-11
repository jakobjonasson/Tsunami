"""Play and inventory models (design.md, Play schema; Inventory and variables)."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from tsunami.core.models.inventory import CustomerFile, Device, VarsFile
from tsunami.core.models.play import (
    Check,
    ConnectionMethod,
    Play,
    Prompt,
    RollbackMode,
    Step,
    play_findings,
)


def play(**overrides: Any) -> Play:
    data: dict[str, Any] = {
        "name": "Test",
        "type": "push",
        "customer": "acme",
        "targets": {"roles": ["access"]},
        "steps": [{"snippet": "base"}],
    }
    data.update(overrides)
    return Play.model_validate(data)


def initial(**overrides: Any) -> Play:
    base: dict[str, Any] = {
        "type": "initial",
        "targets": None,
        "vars": {"site": "site-a", "role": "access", "platform": "ios-xe"},
    }
    base.update(overrides)
    return play(**base)


def codes(p: Play) -> set[str]:
    return {f.code for f in play_findings(p)}


# -- object-level rules ---------------------------------------------------------


@pytest.mark.parametrize(
    "step",
    [
        {},
        {"snippet": "a", "exec": "show version"},
        {"config": []},
        {"snippet": "a", "expect_prompt": "x", "answer": "y"},
        {"exec": "copy a b", "expect_prompt": "Destination"},
        {"exec": "show version", "owns": ["ntp server"]},
        {"snippet": "a", "when": "{{ x }}"},
        {"config": ["vlan 10\nusername evil privilege 15"]},
        {"exec": "show version\r\nreload"},
        {"snippet": "Base"},
    ],
)
def test_invalid_steps(step):
    with pytest.raises(ValidationError):
        Step.model_validate(step)


def test_step_kind():
    assert Step(snippet="a").kind == "snippet"
    assert Step(config=["vlan 10"]).kind == "config"
    assert Step(exec="show clock").kind == "exec"


@pytest.mark.parametrize(
    "prompt",
    [
        {"type": "choice"},
        {"type": "choice", "choices": ["a", "a"]},
        {"type": "string", "choices": ["a"]},
        {"type": "vlan", "pattern": "x"},
        {"type": "string", "pattern": "("},
        {"type": "string", "min": 1},
        {"type": "int", "min": 5, "max": 1},
        {"type": "nope"},
    ],
)
def test_invalid_prompts(prompt):
    with pytest.raises(ValidationError):
        Prompt.model_validate(prompt)


def test_numeric_choices_become_text():
    assert Prompt.model_validate({"type": "choice", "choices": [10, 20]}).choices == ["10", "20"]


@pytest.mark.parametrize(
    "check",
    [
        {},
        {"exec": "show clock"},
        {"exec": "show clock", "expect": "a", "expect_not": "b"},
        {"reachable": False},
        {"reachable": True, "exec": "show clock"},
        {"exec": "show clock", "expect": "["},
    ],
)
def test_invalid_checks(check):
    with pytest.raises(ValidationError):
        Check.model_validate(check)


def test_unknown_keys_are_errors():
    with pytest.raises(ValidationError, match="Extra inputs"):
        play(stpes=[])


# -- defaults by type -------------------------------------------------------------


def test_push_defaults_and_workspace_batch_size():
    s = play().settings(workspace_batch_size=8)
    assert (s.method, s.rollback, s.batch_size, s.canary, s.backup) == (
        ConnectionMethod.SSH,
        RollbackMode.ON_FAILURE,
        8,
        True,
        True,
    )
    assert play(execution={"batch_size": 3}).settings(workspace_batch_size=8).batch_size == 3


def test_initial_defaults():
    s = initial().settings(workspace_batch_size=8)
    assert (s.method, s.rollback, s.batch_size, s.gather_facts, s.save) == (
        ConnectionMethod.SERIAL,
        RollbackMode.NONE,
        1,
        False,
        True,
    )


def test_render_method_turns_off_device_work():
    s = play(connection={"method": "render"}).settings()
    assert not (s.backup or s.save or s.gather_facts or s.canary)
    assert s.rollback == RollbackMode.NONE


def test_baseline_runs_compliance():
    assert play(type="baseline").settings().compliance


# -- cross-field findings --------------------------------------------------------------


def test_clean_plays_have_no_findings():
    assert play_findings(play()) == []
    assert play_findings(initial()) == []


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"targets": None}, "play.targets_empty"),
        ({"targets": {"exclude": ["sa-as01"]}}, "play.targets_empty"),
        ({"connection": {"method": "serial"}}, "play.serial_targets"),
        ({"connection": {"port": "COM3"}}, "play.serial_only"),
        (
            {"execution": {"rollback": "revert_timer"}, "connection": {"method": "render"}},
            "play.render_device",
        ),
        (
            {"connection": {"method": "render"}, "checks": {"post": [{"reachable": True}]}},
            "play.render_checks",
        ),
        ({"connection": {"method": "render"}, "execution": {"backup": True}}, "play.render_device"),
        ({"vars": {"site": "site-a"}}, "play.builtin_var"),
        ({"prompts": {"host": {"type": "string"}}}, "play.builtin_var"),
        ({"on_success": {"add_to_inventory": True}}, "play.add_to_inventory"),
        ({"steps": [{"exec": "show run | include {{ secret('acme/x') }}"}]}, "play.secret_in_exec"),
        (
            {"checks": {"pre": [{"exec": "show clock", "expect": "{{ secret('acme/x') }}"}]}},
            "play.secret_in_check",
        ),
        ({"execution": {"revert_minutes": 10}}, "play.revert_minutes_unused"),
    ],
)
def test_push_play_findings(overrides, code):
    assert code in codes(play(**overrides))


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"targets": {"roles": ["access"]}}, "play.targets_forbidden"),
        ({"vars": {"site": "site-a", "role": "access"}}, "play.initial_builtins"),
        ({"vars": {"host": "x", "site": "s", "role": "r", "platform": "ios"}}, "play.builtin_var"),
        ({"execution": {"batch_size": 2}}, "play.serial_batch"),
        ({"execution": {"rollback": "on_failure"}}, "play.serial_rollback"),
        ({"execution": {"rollback": "revert_timer"}}, "play.serial_rollback"),
        ({"prompts": {"a": {"type": "model"}, "b": {"type": "model"}}}, "play.model_prompts"),
        ({"prompts": {"m": {"type": "model"}}}, "play.model_and_platform"),
    ],
)
def test_initial_play_findings(overrides, code):
    assert code in codes(initial(**overrides))


def test_initial_play_platform_from_model_prompt():
    p = initial(vars={"site": "site-a", "role": "access"}, prompts={"m": {"type": "model"}})
    assert play_findings(p) == []


def test_revert_timer_needs_ssh_when_not_serial_or_render():
    findings = codes(play(execution={"rollback": "revert_timer"}))
    assert "play.revert_timer_ssh" not in findings  # push defaults to ssh


# -- inventory ---------------------------------------------------------------------


def test_vars_files_reject_builtins_and_bad_names():
    with pytest.raises(ValidationError, match="built-in"):
        VarsFile.model_validate({"platform": "ios"})
    with pytest.raises(ValidationError):
        VarsFile.model_validate({"NTP": []})
    assert VarsFile.model_validate({"ntp_servers": [], "snmp": {"Any-Key": 1}}).root


def test_customer_and_device_models():
    CustomerFile.model_validate({"name": "Acme", "credential": "acme-netadmin"})
    with pytest.raises(ValidationError):
        CustomerFile.model_validate(
            {"name": "Acme", "credential": "acme", "role_vars": {"x": {"role": 1}}}
        )
    device = {"name": "sa-as01", "host": "203.0.113.11", "site": "site-a", "role": "access"}
    Device.model_validate({**device, "platform": "aos-s"})
    for bad in (
        {"platform": "nxos"},
        {"platform": "ios", "host": "bad host"},
        {"platform": "ios", "port": 0},
    ):
        with pytest.raises(ValidationError):
            Device.model_validate({**device, **bad})
