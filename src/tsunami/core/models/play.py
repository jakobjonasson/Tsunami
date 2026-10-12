"""Play schema (design.md, Play schema).

The models check structure and the rules inside one object: a step has exactly one
action, a choice prompt has choices. Rules across fields are in play_findings(), which
reports every problem with its location instead of stopping at the first. Rules that
need the inventory (targets resolve, snippets exist, one platform family for inline
config) are checked by the workspace loader.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated, Any

from pydantic import AfterValidator, BeforeValidator, Field, field_validator, model_validator

from tsunami.core.issues import Finding, Severity
from tsunami.core.models.common import BUILTIN_VARS, SingleLine, Slug, StrictModel, VarName


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


# Built-in variables an initial play may set, because its device is not in inventory.
INITIAL_PLAY_BUILTINS = frozenset({"site", "role", "platform"})
SERIAL_BAUD_RATES = (9600, 19200, 38400, 57600, 115200)

_SECRET_CALL_RE = re.compile(r"\bsecret\s*\(")
_TEMPLATE_DELIMITER_RE = re.compile(r"\{\{|\{%|\}\}|%\}")


def _regex(value: str) -> str:
    try:
        re.compile(value)
    except re.error as exc:
        raise ValueError(f"invalid regular expression: {exc.msg}") from None
    return value


def _choices_as_text(value: Any) -> Any:
    # YAML reads `choices: [10, 20]` as numbers; choices are compared as text.
    if isinstance(value, list):
        return [
            str(v) if isinstance(v, int | float) and not isinstance(v, bool) else v for v in value
        ]
    return value


Regex = Annotated[str, AfterValidator(_regex)]


class Connection(StrictModel):
    method: ConnectionMethod | None = Field(None, description="Default comes from the play type.")
    port: str | None = Field(
        None, pattern=r"^(COM[1-9][0-9]*|ask)$", description="Serial only: COMn or ask."
    )
    baud: int | None = Field(None, description="Serial only. Default 9600.")

    @field_validator("baud")
    @classmethod
    def _standard_baud(cls, value: int | None) -> int | None:
        if value is not None and value not in SERIAL_BAUD_RATES:
            raise ValueError(f"baud must be one of {', '.join(map(str, SERIAL_BAUD_RATES))}")
        return value


class Targets(StrictModel):
    sites: list[Slug] = []
    roles: list[Slug] = []
    devices: list[Slug] = []
    exclude: list[Slug] = []

    @property
    def empty(self) -> bool:
        return not (self.sites or self.roles or self.devices)


class Prompt(StrictModel):
    type: PromptType
    label: str | None = None
    help: str | None = None
    default: Any = Field(None, description="A value, or a Jinja expression over variables.")
    required: bool = True
    per_device: bool = False
    choices: Annotated[list[str] | None, BeforeValidator(_choices_as_text)] = None
    pattern: Regex | None = None
    min: int | None = None
    max: int | None = None

    @model_validator(mode="after")
    def _fields_match_type(self) -> Prompt:
        if self.type == PromptType.CHOICE:
            if not self.choices:
                raise ValueError("a choice prompt needs a non-empty choices list")
            if len(set(self.choices)) != len(self.choices):
                raise ValueError("choices must be unique")
        elif self.choices is not None:
            raise ValueError("choices is only valid for type choice")
        if self.pattern is not None and self.type != PromptType.STRING:
            raise ValueError("pattern is only valid for type string")
        if (self.min is not None or self.max is not None) and self.type != PromptType.INT:
            raise ValueError("min and max are only valid for type int")
        if self.min is not None and self.max is not None and self.min > self.max:
            raise ValueError("min is greater than max")
        return self


class Step(StrictModel):
    snippet: Slug | None = Field(None, description="Snippet name without .j2.")
    config: list[SingleLine] | None = Field(None, description="Config-mode lines; Jinja allowed.")
    exec: SingleLine | None = Field(None, description="One exec-mode command.")
    expect_prompt: Regex | None = None
    answer: SingleLine | None = None
    read_timeout: float | None = Field(None, gt=0)
    name: str | None = None
    tags: list[str] = []
    when: SingleLine | None = Field(None, description="Jinja expression without braces.")
    owns: list[SingleLine] = Field([], description="Top-level command prefixes this step owns.")
    allow_destructive: bool = False

    @model_validator(mode="after")
    def _one_action(self) -> Step:
        actions = [a for a in ("snippet", "config", "exec") if getattr(self, a) is not None]
        if len(actions) != 1:
            raise ValueError("a step needs exactly one of snippet, config or exec")
        if self.config is not None and not self.config:
            raise ValueError("config needs at least one line")
        exec_only = (self.expect_prompt, self.answer, self.read_timeout)
        if self.exec is None and any(v is not None for v in exec_only):
            raise ValueError("expect_prompt, answer and read_timeout are only valid on exec steps")
        if (self.expect_prompt is None) != (self.answer is None):
            raise ValueError("expect_prompt and answer go together")
        if self.owns and self.exec is not None:
            raise ValueError("owns is only valid on snippet and config steps")
        if self.when is not None and _TEMPLATE_DELIMITER_RE.search(self.when):
            raise ValueError("when is an expression without {{ }} or {% %}")
        return self

    @property
    def kind(self) -> str:
        return "snippet" if self.snippet is not None else "config" if self.config else "exec"


class Check(StrictModel):
    name: str | None = None
    exec: SingleLine | None = None
    expect: Regex | None = None
    expect_not: Regex | None = None
    reachable: bool | None = None
    retries: int = Field(0, ge=0)
    delay: float = Field(5, ge=0)

    @model_validator(mode="after")
    def _one_form(self) -> Check:
        if self.reachable is not None:
            if self.reachable is not True:
                raise ValueError("reachable can only be true")
            if any(v is not None for v in (self.exec, self.expect, self.expect_not)):
                raise ValueError("a reachable check has no exec, expect or expect_not")
            return self
        if self.exec is None:
            raise ValueError("a check needs exec (with expect or expect_not) or reachable: true")
        if (self.expect is None) == (self.expect_not is None):
            raise ValueError("an exec check needs exactly one of expect or expect_not")
        return self


class Checks(StrictModel):
    pre: list[Check] = []
    post: list[Check] = []


class Execution(StrictModel):
    """Every field defaults by play type (see EffectiveSettings); unset means the default."""

    gather_facts: bool | None = None
    backup: bool | None = None
    rollback: RollbackMode | None = None
    revert_minutes: int | None = Field(None, ge=1, le=120)
    batch_size: int | None = Field(None, ge=1, le=50)
    max_fail_percent: int | None = Field(None, ge=0, le=100)
    canary: bool | None = None
    save: bool | None = None
    connect_timeout: float | None = Field(None, gt=0)
    command_timeout: float | None = Field(None, gt=0)


class OnSuccess(StrictModel):
    add_to_inventory: bool = Field(False, description="Initial plays only.")


# A play for no customer in particular: it is bound to the customer it runs for
# (services.play_entry), and writes secrets as secret('name') for that customer's.
GLOBAL_CUSTOMER = "global"


class Play(StrictModel):
    name: str = Field(min_length=1)
    description: str | None = None
    type: PlayType
    customer: Slug
    connection: Connection = Connection()
    targets: Targets | None = None
    prompts: dict[VarName, Prompt] = {}
    vars: dict[VarName, Any] = {}
    steps: list[Step] = Field(min_length=1)
    execution: Execution = Execution()
    checks: Checks = Checks()
    on_success: OnSuccess = OnSuccess()

    def settings(self, workspace_batch_size: int | None = None) -> EffectiveSettings:
        """Execution settings after applying per-type defaults (design.md, Defaults by type)."""
        defaults = _TYPE_DEFAULTS[self.type]
        ex = self.execution

        def pick(name: str) -> Any:
            value = getattr(ex, name)
            return defaults[name] if value is None else value

        method = self.connection.method or defaults["method"]
        no_device = method == ConnectionMethod.RENDER
        serial = method == ConnectionMethod.SERIAL
        if ex.batch_size is not None:
            batch_size = ex.batch_size
        elif self.type == PlayType.INITIAL or serial:
            batch_size = 1
        else:
            batch_size = workspace_batch_size or defaults["batch_size"]
        rollback = pick("rollback")
        if no_device or (serial and ex.rollback is None):
            rollback = RollbackMode.NONE
        return EffectiveSettings(
            method=method,
            gather_facts=False if no_device else pick("gather_facts"),
            backup=False if no_device else pick("backup"),
            rollback=rollback,
            revert_minutes=ex.revert_minutes or 5,
            batch_size=batch_size,
            max_fail_percent=pick("max_fail_percent"),
            canary=False if no_device or serial else pick("canary"),
            save=False if no_device else pick("save"),
            connect_timeout=ex.connect_timeout or 15,
            command_timeout=ex.command_timeout or 60,
            compliance=self.type == PlayType.BASELINE,
        )


@dataclass(frozen=True)
class EffectiveSettings:
    method: ConnectionMethod
    gather_facts: bool
    backup: bool
    rollback: RollbackMode
    revert_minutes: int
    batch_size: int
    max_fail_percent: int
    canary: bool
    save: bool
    connect_timeout: float
    command_timeout: float
    compliance: bool


_PUSH_DEFAULTS: dict[str, Any] = {
    "method": ConnectionMethod.SSH,
    "gather_facts": True,
    "backup": True,
    "rollback": RollbackMode.ON_FAILURE,
    "batch_size": 5,
    "max_fail_percent": 10,
    "canary": True,
    "save": True,
}
_TYPE_DEFAULTS: dict[PlayType, dict[str, Any]] = {
    PlayType.INITIAL: {
        "method": ConnectionMethod.SERIAL,
        "gather_facts": False,
        "backup": False,
        "rollback": RollbackMode.NONE,
        "batch_size": 1,
        "max_fail_percent": 0,
        "canary": False,
        "save": True,
    },
    PlayType.PUSH: _PUSH_DEFAULTS,
    PlayType.BASELINE: _PUSH_DEFAULTS,
}


def play_findings(play: Play) -> list[Finding]:
    """Cross-field rules that need only the play itself (design.md, Cross-field validation)."""
    findings: list[Finding] = []

    def error(code: str, message: str, *loc: str | int) -> None:
        findings.append(Finding(Severity.ERROR, f"play.{code}", message, tuple(loc)))

    def warning(code: str, message: str, *loc: str | int) -> None:
        findings.append(Finding(Severity.WARNING, f"play.{code}", message, tuple(loc)))

    settings = play.settings()
    ex = play.execution
    initial = play.type == PlayType.INITIAL
    method = settings.method

    if initial:
        if play.targets is not None:
            error(
                "targets_forbidden",
                "initial plays have no targets: the device is not in inventory yet",
                "targets",
            )
    elif play.targets is None or play.targets.empty:
        error(
            "targets_empty",
            f"{play.type} plays need targets (sites, roles or devices); "
            "an empty target list never means all devices",
            *(("targets",) if play.targets is not None else ("type",)),
        )

    if method != ConnectionMethod.SERIAL:
        for field in ("port", "baud"):
            if getattr(play.connection, field) is not None:
                error("serial_only", f"{field} is only valid for serial connections",
                      "connection", field)  # fmt: skip
    if method == ConnectionMethod.SERIAL:
        if not initial:
            error(
                "serial_targets",
                "serial connections are for initial plays, which have no targets",
                "connection",
                "method",
            )
        if ex.batch_size not in (None, 1):
            error("serial_batch", "serial connections need batch_size 1",
                  "execution", "batch_size")  # fmt: skip
        if ex.rollback not in (None, RollbackMode.NONE):
            error("serial_rollback", "serial connections need rollback: none",
                  "execution", "rollback")  # fmt: skip
    if method == ConnectionMethod.RENDER:
        if play.checks.pre or play.checks.post:
            error("render_checks", "render plays have no checks", "checks")
        for field in ("backup", "save", "gather_facts"):
            if getattr(ex, field):
                error("render_device", f"render plays never connect, so {field} must be off",
                      "execution", field)  # fmt: skip
        if ex.rollback not in (None, RollbackMode.NONE):
            error("render_device", "render plays never connect, so rollback must be none",
                  "execution", "rollback")  # fmt: skip
    elif settings.rollback == RollbackMode.REVERT_TIMER and method != ConnectionMethod.SSH:
        error("revert_timer_ssh", "rollback: revert_timer needs method: ssh",
              "execution", "rollback")  # fmt: skip
    if ex.revert_minutes is not None and settings.rollback != RollbackMode.REVERT_TIMER:
        warning(
            "revert_minutes_unused",
            "revert_minutes is ignored unless rollback is revert_timer",
            "execution",
            "revert_minutes",
        )

    allowed = INITIAL_PLAY_BUILTINS if initial else frozenset()
    for section, names in (("vars", play.vars), ("prompts", play.prompts)):
        for name in names:
            if name in BUILTIN_VARS - allowed:
                error("builtin_var", f"'{name}' is a built-in variable set by the engine",
                      section, name)  # fmt: skip

    model_prompts = [n for n, p in play.prompts.items() if p.type == PromptType.MODEL]
    if initial:
        supplied = set(play.vars) | set(play.prompts) | ({"platform"} if model_prompts else set())
        missing = [name for name in ("site", "role", "platform") if name not in supplied]
        if missing:
            error(
                "initial_builtins",
                f"initial plays must supply {', '.join(missing)} through play vars or prompts "
                "(a model prompt supplies platform)",
                "type",
            )
        if len(model_prompts) > 1:
            error("model_prompts", "an initial play can have only one model prompt",
                  "prompts", model_prompts[1])  # fmt: skip
        if model_prompts and ("platform" in play.vars or "platform" in play.prompts):
            error("model_and_platform", "set platform with a model prompt or directly, not both",
                  "prompts", model_prompts[0])  # fmt: skip

    if play.on_success.add_to_inventory and not initial:
        error("add_to_inventory", "add_to_inventory is only valid in initial plays",
              "on_success", "add_to_inventory")  # fmt: skip

    for i, step in enumerate(play.steps):
        if step.exec is not None and _SECRET_CALL_RE.search(f"{step.exec} {step.answer or ''}"):
            error("secret_in_exec", "secret() is not allowed in exec steps", "steps", i, "exec")
    for phase in ("pre", "post"):
        for i, check in enumerate(getattr(play.checks, phase)):
            texts = (check.exec, check.expect, check.expect_not)
            if any(t and _SECRET_CALL_RE.search(t) for t in texts):
                error("secret_in_check", "secret() is not allowed in checks", "checks", phase, i)

    return findings
