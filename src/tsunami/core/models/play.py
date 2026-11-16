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
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

from pydantic import AfterValidator, BeforeValidator, Field, field_validator, model_validator

from tsunami.core.issues import Finding, Severity
from tsunami.core.models.common import (
    BUILTIN_VARS,
    Platform,
    SingleLine,
    Slug,
    StrictModel,
    VarName,
)


class PlayType(StrEnum):
    INITIAL = "initial"
    PUSH = "push"
    BASELINE = "baseline"
    UPGRADE = "upgrade"  # firmware: stage an image, activate it, verify (see Firmware)


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
    LOGIN = "login"  # a saved login's name; templates read it through login()
    SECRET = "secret"  # a saved secret's name; templates read its fields, masked
    PASSWORD = "password"  # typed for one run, masked everywhere, never stored


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
        if self.type == PromptType.PASSWORD:
            if self.default is not None:
                raise ValueError(
                    "a prompt of type 'password' has no default: a saved value belongs in "
                    "Credentials, Secrets (use a prompt of type 'secret')"
                )
            if self.per_device:
                raise ValueError(
                    "a prompt of type 'password' is asked once per run, not per device"
                )
        return self


class FirmwareAction(StrEnum):
    STAGE = "stage"  # copy the image to the device and verify it; nothing reboots
    ACTIVATE = "activate"  # boot the staged image: reloads the device


class TransferMethod(StrEnum):
    SCP = "scp"  # Tsunami uploads the file from its firmware folder
    TFTP = "tftp"  # the device fetches it from `server`
    HTTP = "http"  # the device fetches it from `server` (http or https)


_IMAGE_FILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*$")


def _hex(length: int) -> Any:
    def check(value: str) -> str:
        text = value.strip().lower()
        if len(text) != length or any(c not in "0123456789abcdef" for c in text):
            raise ValueError(f"must be {length} hexadecimal characters")
        return text

    return check


Md5 = Annotated[str, AfterValidator(_hex(32))]
Sha256 = Annotated[str, AfterValidator(_hex(64))]
Sha512 = Annotated[str, AfterValidator(_hex(128))]


class FirmwareImage(StrictModel):
    file: str = Field(description="The image's file name in the firmware folder; no path.")
    version: SingleLine = Field(
        min_length=1, description="The version the device reports once it runs this image."
    )
    md5: Md5 | None = None
    sha256: Sha256 | None = None
    sha512: Sha512 | None = None
    platforms: list[Platform] = Field(min_length=1)
    models: list[Slug] = Field([], description="Hardware model ids; empty means any model.")

    @model_validator(mode="after")
    def _checks(self) -> FirmwareImage:
        if not _IMAGE_FILE_RE.match(self.file) or ".." in self.file:
            raise ValueError("file is a plain file name: letters, digits, dot, dash, underscore")
        if not (self.md5 or self.sha256 or self.sha512):
            raise ValueError(
                "an image needs a hash (sha256, sha512 or md5), so the file cannot be "
                "swapped for another after the play is reviewed"
            )
        return self

    @property
    def hashes(self) -> dict[str, str]:
        found = (("md5", self.md5), ("sha256", self.sha256), ("sha512", self.sha512))
        return {name: value for name, value in found if value}


def _server_url(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError:
        raise ValueError("server is not a valid URL") from None
    if parts.scheme not in ("tftp", "http", "https") or not parts.hostname:
        raise ValueError("server is tftp://host[/folder] or http(s)://host[:port][/folder]")
    if parts.username or parts.password:
        raise ValueError("server must not hold a username or password: plays are not secret")
    if parts.query or parts.fragment or any(c.isspace() for c in value):
        raise ValueError("server is a plain URL: no spaces, query or fragment")
    if parts.scheme == "tftp" and port is not None:
        raise ValueError("a tftp server takes no port")
    return value.rstrip("/")


class Firmware(StrictModel):
    """What an upgrade play installs and how the image reaches the device."""

    transfer: TransferMethod = TransferMethod.SCP
    server: Annotated[str | None, AfterValidator(_server_url)] = Field(
        None, description="tftp and http: where devices fetch the image from."
    )
    images: list[FirmwareImage] = Field(min_length=1)
    reload_timeout: int = Field(
        1800, ge=300, le=7200, description="Seconds to wait for a device to return."
    )

    @model_validator(mode="after")
    def _transfer_and_server(self) -> Firmware:
        if self.transfer == TransferMethod.SCP:
            if self.server is not None:
                raise ValueError("server is for tftp and http; scp uploads from this PC")
            return self
        if self.server is None:
            raise ValueError(f"transfer: {self.transfer} needs server")
        scheme = urlsplit(self.server).scheme
        allowed = ("tftp",) if self.transfer == TransferMethod.TFTP else ("http", "https")
        if scheme not in allowed:
            raise ValueError(f"transfer: {self.transfer} needs a {' or '.join(allowed)} server")
        return self

    def image_for(self, platform: Platform | str, model: str | None) -> FirmwareImage | None:
        """The first image listed for this platform and model."""
        for image in self.images:
            if str(platform) not in {str(p) for p in image.platforms}:
                continue
            if not image.models or (model is not None and model in image.models):
                return image
        return None


class Step(StrictModel):
    snippet: Slug | None = Field(None, description="Snippet name without .j2.")
    config: list[SingleLine] | None = Field(None, description="Config-mode lines; Jinja allowed.")
    exec: SingleLine | None = Field(None, description="One exec-mode command.")
    firmware: FirmwareAction | None = Field(
        None,
        description="Upgrade plays: stage (copy and verify the image) or activate "
        "(set it to boot, reload, verify the version).",
    )
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
        names = ("snippet", "config", "exec", "firmware")
        actions = [a for a in names if getattr(self, a) is not None]
        if len(actions) != 1:
            raise ValueError("a step needs exactly one of snippet, config, exec or firmware")
        if self.config is not None and not self.config:
            raise ValueError("config needs at least one line")
        exec_only = (self.expect_prompt, self.answer, self.read_timeout)
        if self.exec is None and any(v is not None for v in exec_only):
            raise ValueError("expect_prompt, answer and read_timeout are only valid on exec steps")
        if (self.expect_prompt is None) != (self.answer is None):
            raise ValueError("expect_prompt and answer go together")
        if self.owns and (self.exec is not None or self.firmware is not None):
            raise ValueError("owns is only valid on snippet and config steps")
        if self.when is not None and _TEMPLATE_DELIMITER_RE.search(self.when):
            raise ValueError("when is an expression without {{ }} or {% %}")
        return self

    @property
    def kind(self) -> str:
        if self.firmware is not None:
            return "firmware"
        return "snippet" if self.snippet is not None else "config" if self.config else "exec"


# A value a state check compares a cell with: a word or number, /a regex/, a list of
# those (any of), or {min, max} for a number (core/state/evaluate.py).
StateScalar = str | int
StateMatcher = StateScalar | list[StateScalar] | dict[Literal["min", "max"], int]


def _as_list(matcher: Any) -> list[Any]:
    return list(matcher) if isinstance(matcher, list) else [matcher]


def _state_matchers(value: dict[str, Any] | None) -> dict[str, Any] | None:
    for name, matcher in (value or {}).items():
        if isinstance(matcher, dict) and not matcher:
            raise ValueError(f"{name}: a range needs min, max or both")
        for item in _as_list(matcher):
            if isinstance(item, bool):
                raise ValueError(f"{name}: a state value is text; put true or false in quotes")
            regex = isinstance(item, str) and len(item) >= 2 and item[0] == item[-1] == "/"
            if regex and not _TEMPLATE_DELIMITER_RE.search(item):
                _regex(item[1:-1])
    return value


StateMatchers = Annotated[dict[VarName, StateMatcher], AfterValidator(_state_matchers)]


class CountRange(StrictModel):
    min: int | None = Field(None, ge=0)
    max: int | None = Field(None, ge=0)

    @model_validator(mode="after")
    def _bounds(self) -> CountRange:
        if self.min is None and self.max is None:
            raise ValueError("count needs a number, or min, max or both")
        if self.min is not None and self.max is not None and self.min > self.max:
            raise ValueError("min is greater than max")
        return self


class StateCompare(StrEnum):
    COUNT = "count"  # as many rows as before the change
    NO_LOSS = "no_loss"  # every row from before is still there, unchanged; new rows are fine
    SAME = "same"  # no_loss, and no new rows


class Check(StrictModel):
    name: str | None = None
    exec: SingleLine | None = None
    expect: Regex | StateMatchers | None = Field(
        None,
        description="exec checks: a regex the output must match. state checks: column "
        "values every selected row must have.",
    )
    expect_not: Regex | None = None
    reachable: bool | None = None
    state: Slug | None = Field(
        None, description="A state table, e.g. ospf_neighbors (design.md, State checks)."
    )
    where: StateMatchers | None = Field(None, description="state: which rows; all by default.")
    count: Annotated[int, Field(ge=0)] | CountRange | None = Field(
        None, description="state: how many rows are selected: a number, or min and max."
    )
    compare: StateCompare | None = Field(
        None, description="state, post-checks only: against the rows before the change."
    )
    retries: int = Field(0, ge=0)
    delay: float = Field(5, ge=0)

    @model_validator(mode="after")
    def _one_form(self) -> Check:
        forms = [n for n in ("exec", "reachable", "state") if getattr(self, n) is not None]
        if len(forms) > 1:
            raise ValueError(
                f"a check is one of exec, reachable or state, not {' and '.join(forms)}"
            )
        state_only = (self.where, self.count, self.compare)
        if self.state is None and any(v is not None for v in state_only):
            raise ValueError("where, count and compare are only valid on state checks")
        if self.state is not None:
            return self._state_form()
        if isinstance(self.expect, dict):
            raise ValueError("expect is a regex here; a mapping is for state checks")
        if self.reachable is not None:
            if self.reachable is not True:
                raise ValueError("reachable can only be true")
            if any(v is not None for v in (self.exec, self.expect, self.expect_not)):
                raise ValueError("a reachable check has no exec, expect or expect_not")
            return self
        if self.exec is None:
            raise ValueError(
                "a check needs exec (with expect or expect_not), reachable: true, or state"
            )
        if (self.expect is None) == (self.expect_not is None):
            raise ValueError("an exec check needs exactly one of expect or expect_not")
        return self

    def _state_form(self) -> Check:
        from tsunami.core.state.tables import TABLES

        spec = TABLES.get(self.state or "")
        if spec is None:
            known = ", ".join(sorted(TABLES))
            raise ValueError(f"unknown state table {self.state!r}; the tables are {known}")
        if self.expect_not is not None:
            raise ValueError("expect_not is for exec checks; a state check uses expect")
        if self.expect is not None and not isinstance(self.expect, dict):
            raise ValueError(
                "in a state check, expect is a mapping of column to value, e.g. {state: full}"
            )
        if not self.expect and self.count is None and self.compare is None:
            raise ValueError("a state check needs expect, count or compare")
        for section in ("where", "expect"):
            for column in getattr(self, section) or {}:
                if spec.column(column) is None:
                    raise ValueError(
                        f"{section}: {self.state} has no column {column!r}; "
                        f"its columns are {', '.join(spec.column_names)}"
                    )
        return self

    @property
    def count_range(self) -> tuple[int | None, int | None] | None:
        if self.count is None:
            return None
        if isinstance(self.count, int):
            return (self.count, self.count)
        return (self.count.min, self.count.max)


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
    firmware: Firmware | None = Field(None, description="Upgrade plays only.")
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
        elif self.type in (PlayType.INITIAL, PlayType.UPGRADE) or serial:
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
    # One device at a time and stop at the first failure: a reload takes the device out
    # of service, and nothing can roll a firmware upgrade back.
    PlayType.UPGRADE: {
        **_PUSH_DEFAULTS,
        "rollback": RollbackMode.NONE,
        "batch_size": 1,
        "max_fail_percent": 0,
    },
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
            if any(_SECRET_CALL_RE.search(t) for t in _check_texts(check)):
                error("secret_in_check", "secret() is not allowed in checks", "checks", phase, i)
            if phase == "pre" and check.compare is not None:
                error(
                    "compare_pre",
                    "compare is for post-checks: it compares with the state before the change",
                    "checks", phase, i, "compare",
                )  # fmt: skip

    _firmware_findings(play, settings, error)

    return findings


def _check_texts(check: Check) -> list[str]:
    """Every string a check holds that Jinja renders."""
    texts = [t for t in (check.exec, check.expect_not) if t]
    if isinstance(check.expect, str):
        texts.append(check.expect)
    for section in (check.where, check.expect if isinstance(check.expect, dict) else None):
        for matcher in (section or {}).values():
            texts.extend(m for m in _as_list(matcher) if isinstance(m, str))
    return texts


def _firmware_findings(play: Play, settings: EffectiveSettings, error: Any) -> None:
    """Rules for upgrade plays (design.md, Firmware upgrades)."""
    steps = [(i, s) for i, s in enumerate(play.steps) if s.firmware is not None]
    if play.type != PlayType.UPGRADE:
        if play.firmware is not None:
            error("firmware_type", "firmware is only valid in upgrade plays", "firmware")
        for i, _ in steps:
            error("firmware_type", "firmware steps are only valid in upgrade plays",
                  "steps", i, "firmware")  # fmt: skip
        return
    if play.firmware is None:
        error("firmware_missing", "an upgrade play needs a firmware section with its images",
              "type")  # fmt: skip
    if not steps:
        error("firmware_steps", "an upgrade play needs a firmware step (stage, activate)",
              "steps")  # fmt: skip
    if settings.method != ConnectionMethod.SSH:
        error("firmware_ssh", "upgrade plays connect over ssh", "connection", "method")
    if settings.rollback != RollbackMode.NONE:
        error(
            "firmware_rollback",
            "upgrade plays need rollback: none: a config rollback cannot undo a firmware "
            "upgrade, and the reload ends the session it would run in",
            "execution", "rollback",
        )  # fmt: skip
    if not settings.gather_facts:
        error("firmware_facts", "upgrade plays need gather_facts: the running version "
              "decides what to do", "execution", "gather_facts")  # fmt: skip
    seen: list[FirmwareAction] = []
    for i, step in steps:
        action = step.firmware
        if action in seen:
            error("firmware_repeat", f"firmware: {action} appears twice", "steps", i, "firmware")
        if action == FirmwareAction.STAGE and FirmwareAction.ACTIVATE in seen:
            error("firmware_order", "stage must come before activate", "steps", i, "firmware")
        if action == FirmwareAction.ACTIVATE and not step.allow_destructive:
            error(
                "firmware_reload",
                "firmware: activate reloads the device; set allow_destructive: true on "
                "this step",
                "steps", i,
            )  # fmt: skip
        seen.append(action)
