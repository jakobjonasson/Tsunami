"""Play schema (design.md, Play schema).

Phase 1 (work package 1.2) adds the Play model, per-type defaults and cross-field
rules. The enums are defined now so Phase 1 work packages can start in parallel.
"""

from __future__ import annotations

from enum import StrEnum


class PlayType(StrEnum):
    INITIAL = "initial"
    PUSH = "push"
    BASELINE = "baseline"


class ConnectionMethod(StrEnum):
    SSH = "ssh"
    SERIAL = "serial"
    RENDER = "render"


class RollbackMode(StrEnum):
    NONE = "none"
    ON_FAILURE = "on_failure"
    REVERT_TIMER = "revert_timer"


class RunMode(StrEnum):
    RENDER = "render"
    CHECK = "check"
    COMPLIANCE = "compliance"  # baseline plays only
    APPLY = "apply"


class PromptType(StrEnum):
    STRING = "string"
    INT = "int"
    BOOL = "bool"
    CHOICE = "choice"
    HOSTNAME = "hostname"
    VLAN = "vlan"
    VLAN_LIST = "vlan_list"
    INTERFACE = "interface"
    IPV4 = "ipv4"
    IPV4_INTERFACE = "ipv4_interface"
    IPV4_NETWORK = "ipv4_network"
    SITE = "site"
    ROLE = "role"
    MODEL = "model"  # hardware catalog entry


# Exec commands that need allow_destructive: true on their step.
DESTRUCTIVE_EXEC_COMMANDS = ("reload", "erase", "format", "delete", "write erase")
