"""Render a play for one device into masked lines, sentinel lines and a render hash.

The renderer resolves the device's variables, renders every step through the sandboxed
environment, drops comments and blank lines, and enforces the secret() sink rule and the
platform rules (guarded commands, AOS-S reboot-required lines, revert-timer limits). A
Rendering holds lines in sentinel form, which never leave the process: callers get
masked lines for display and storage, a hash of them for approvals, and real values only
through materialize() in apply mode.

Validation renders use placeholder prompt values and facts, and treat any `when` that
mentions facts as true, so every branch is checked before anything connects.

See design.md, Snippets and rendering; Play schema: Cross-field validation; Platform
drivers; Execution engine: Run level.
"""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Collection, Iterator, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import jinja2

from tsunami.core.catalog import load_catalog
from tsunami.core.issues import Issue, Severity, TsunamiError
from tsunami.core.models.common import PLATFORM_FAMILY, Platform
from tsunami.core.models.play import Play, PlayType, Prompt, PromptType, RollbackMode
from tsunami.core.models.settings import AppSettings
from tsunami.core.platforms import get_driver
from tsunami.core.platforms.aoss import REBOOT_REQUIRED
from tsunami.core.platforms.base import PlatformDriver, top_level_parent
from tsunami.core.prompts import PromptContext, PromptValueError, coerce, placeholder
from tsunami.core.render.environment import (
    TemplateError,
    compile_expression,
    compile_template,
    make_environment,
    referenced_names,
    render_template,
)
from tsunami.core.render.lookup import find_snippet
from tsunami.core.render.secret_refs import SecretCollector, SecretRefError
from tsunami.core.targets import resolve_targets

PLACEHOLDER_FACTS = {
    "model": "placeholder",
    "version": "placeholder",
    "serial": "placeholder",
    "uptime": "placeholder",
}
# AOS-CX `checkpoint auto` accepts 1 to 60 minutes (design.md, Backup and rollback).
MAX_REVERT_MINUTES = {"aos-cx": 60}


class RenderError(TsunamiError):
    """Rendering failed. Points at a snippet line, or at a location in the play file."""

    def __init__(
        self, message: str, *, file: Path | None, line: int | None = None, loc: tuple = ()
    ) -> None:
        super().__init__(message)
        self.file = file
        self.line = line
        self.loc = loc


@dataclass(frozen=True)
class RenderedStep:
    index: int
    kind: str  # snippet | config | exec
    label: str
    source: Path | None  # the snippet file, or None for inline steps
    lines: tuple[str, ...]  # sentinel form for config steps; one command for exec
    skipped: str | None = None  # "when" or "tags"
    expect_prompt: str | None = None
    answer: str | None = None
    read_timeout: float | None = None
    owns: tuple[str, ...] = ()


@dataclass(frozen=True)
class RenderedCheck:
    phase: str
    index: int
    name: str | None
    exec: str | None
    expect: str | None
    expect_not: str | None
    reachable: bool
    retries: int
    delay: float


@dataclass(frozen=True)
class Rendering:
    play_path: Path
    customer: str
    device: str | None
    platform: Platform
    steps: tuple[RenderedStep, ...]
    checks: tuple[RenderedCheck, ...]
    _secrets: SecretCollector = field(repr=False, compare=False)

    @property
    def secret_refs(self) -> tuple[str, ...]:
        return tuple(self._secrets.refs)

    def masked_steps(self) -> list[RenderedStep]:
        mask = self._secrets.mask
        return [replace(s, lines=tuple(mask(line) for line in s.lines)) for s in self.steps]

    def masked_lines(self) -> list[str]:
        """Config lines that would be sent, secrets as ********."""
        return [
            line
            for step in self.masked_steps()
            if step.kind != "exec" and step.skipped is None
            for line in step.lines
        ]

    def masked_text(self) -> str:
        out = []
        for step in self.masked_steps():
            header = f"! step {step.index + 1}: {step.label}"
            if step.skipped:
                out.append(f"{header} (skipped: {step.skipped})")
                continue
            out.append(header)
            out.extend(f"exec: {line}" if step.kind == "exec" else line for line in step.lines)
        return "\n".join(out) + "\n"

    def render_hash(self) -> str:
        """sha256 of the masked render: approvals compare it at execution time."""
        payload = {
            "steps": [[s.kind, s.skipped, list(s.lines)] for s in self.masked_steps()],
            "checks": [[c.phase, c.exec, c.expect, c.expect_not, c.reachable] for c in self.checks],
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()

    def materialize(self, values: Mapping[str, str]) -> list[RenderedStep]:
        """Steps with real secret values, for sending in apply mode. Never store these."""
        missing = [ref for ref in self._secrets.refs if ref not in values]
        if missing:
            raise TsunamiError(f"missing secrets: {', '.join(missing)}")
        real = self._secrets.materialize
        return [replace(s, lines=tuple(real(line, values) for line in s.lines)) for s in self.steps]


class Renderer:
    """Renders plays from one loaded workspace. Compiled templates are cached."""

    def __init__(self, workspace: Any, settings: AppSettings | None = None) -> None:
        self.workspace = workspace  # tsunami.core.workspace.Workspace
        self.settings = settings or AppSettings()
        self.env = make_environment()
        self._templates: dict[tuple[str, str], jinja2.Template] = {}

    def driver(self, platform: Platform) -> PlatformDriver:
        """The platform's driver, with extra sink patterns from settings."""
        driver = get_driver(platform)
        extra = self.settings.extra_rules(driver.family)
        return replace(driver, secret_rules=driver.secret_rules + extra) if extra else driver

    def render(
        self,
        entry: Any,  # tsunami.core.workspace.PlayEntry
        *,
        device: str | None = None,
        prompt_values: Mapping[str, Any] | None = None,
        facts: Mapping[str, Any] | None = None,
        tags: Collection[str] | None = None,
        validation: bool = False,
    ) -> Rendering:
        play: Play | None = entry.play
        if play is None:
            raise RenderError("the play failed validation", file=entry.path)
        answers = dict(prompt_values or {})
        for name, prompt in play.prompts.items():
            if prompt.type == PromptType.MODEL and name in answers:
                answers.setdefault("platform", str(load_catalog().get(answers[name]).platform))
        resolved = self.workspace.resolve_vars(
            play.customer, device=device, play=entry, prompt_values=answers
        )
        platform_value = resolved.values.get("platform")
        if platform_value not in {str(p) for p in Platform}:
            raise RenderError("the target platform is unknown", file=entry.path, loc=("type",))
        platform = Platform(platform_value)
        driver = self.driver(platform)

        variables = copy.deepcopy(resolved.values)
        if facts is not None or validation:
            variables["facts"] = dict(facts if facts is not None else PLACEHOLDER_FACTS)
        collector = SecretCollector(play.customer)
        context = {**variables, "secret": collector}

        steps = tuple(
            self._step(entry, play, i, context, collector, driver, platform, tags, validation)
            for i in range(len(play.steps))
        )
        checks = tuple(self._checks(entry, play, context, collector))
        self._platform_rules(entry, play, steps, driver)
        return Rendering(entry.path, play.customer, device, platform, steps, checks, collector)

    # -- steps -------------------------------------------------------------------

    def _step(
        self,
        entry: Any,
        play: Play,
        i: int,
        context: dict[str, Any],
        collector: SecretCollector,
        driver: PlatformDriver,
        platform: Platform,
        tags: Collection[str] | None,
        validation: bool,
    ) -> RenderedStep:
        step = play.steps[i]
        label = step.name or step.snippet or (step.exec if step.exec else f"config ({i + 1})")
        base = {"index": i, "kind": step.kind, "label": label, "owns": tuple(step.owns)}
        if tags is not None and "always" not in step.tags and not set(step.tags) & set(tags):
            return RenderedStep(**base, source=None, lines=(), skipped="tags")
        if step.when is not None and not self._when(entry, i, step.when, context, validation):
            return RenderedStep(**base, source=None, lines=(), skipped="when")

        if step.snippet is not None:
            source = find_snippet(
                self.workspace.root,
                step.snippet,
                customer=play.customer,
                platform=platform,
                draft=entry.draft,
            )
            if source is None:
                raise RenderError(
                    f"snippet {step.snippet!r} not found for {platform}",
                    file=entry.path,
                    loc=("steps", i, "snippet"),
                )
            text = (self.workspace.root / source).read_text(encoding="utf-8-sig")
            rendered = self._render(text, source.as_posix(), context, file=source)
            lines = self._lines(rendered, driver)
            self._sinks(lines, collector, driver, file=source)
            return RenderedStep(**base, source=source, lines=tuple(lines))

        if step.config is not None:
            lines = []
            for j, raw in enumerate(step.config):
                loc = ("steps", i, "config", j)
                rendered = self._inline(entry, raw, loc, context)
                lines.extend(self._lines(rendered, driver))
            self._sinks(lines, collector, driver, file=entry.path, loc=("steps", i))
            return RenderedStep(**base, source=None, lines=tuple(lines))

        assert step.exec is not None
        command = self._inline(entry, step.exec, ("steps", i, "exec"), context).strip()
        answer = (
            self._inline(entry, step.answer, ("steps", i, "answer"), context).strip()
            if step.answer is not None
            else None
        )
        if collector.contains(command) or (answer and collector.contains(answer)):
            raise RenderError(
                "secret() is not allowed in exec steps",
                file=entry.path,
                loc=("steps", i, "exec"),
            )
        return RenderedStep(
            **base,
            source=None,
            lines=(command,),
            expect_prompt=step.expect_prompt,
            answer=answer,
            read_timeout=step.read_timeout,
        )

    def _when(
        self, entry: Any, i: int, expression: str, context: dict[str, Any], validation: bool
    ) -> bool:
        name = f"{entry.path.as_posix()}#steps[{i}].when"
        try:
            if validation and "facts" in referenced_names(self.env, f"{{{{ {expression} }}}}"):
                return True
            return bool(compile_expression(self.env, expression, name=name)(**context))
        except TemplateError as exc:
            raise RenderError(
                f"when: {_message(exc)}", file=entry.path, loc=("steps", i, "when")
            ) from None

    def _checks(
        self, entry: Any, play: Play, context: dict[str, Any], collector: SecretCollector
    ) -> Iterator[RenderedCheck]:
        for phase in ("pre", "post"):
            for i, check in enumerate(getattr(play.checks, phase)):
                rendered = {}
                for field_name in ("exec", "expect", "expect_not"):
                    raw = getattr(check, field_name)
                    loc = ("checks", phase, i, field_name)
                    value = self._inline(entry, raw, loc, context).strip() if raw else None
                    if value and collector.contains(value):
                        raise RenderError(
                            "secret() is not allowed in checks", file=entry.path, loc=loc
                        )
                    rendered[field_name] = value
                yield RenderedCheck(
                    phase=phase,
                    index=i,
                    name=check.name,
                    reachable=bool(check.reachable),
                    retries=check.retries,
                    delay=check.delay,
                    **rendered,
                )

    # -- helpers -------------------------------------------------------------------

    def _template(self, source: str, name: str) -> jinja2.Template:
        key = (name, source)
        if key not in self._templates:
            self._templates[key] = compile_template(self.env, source, name=name)
        return self._templates[key]

    def _render(
        self, source: str, name: str, context: dict[str, Any], *, file: Path, loc: tuple = ()
    ) -> str:
        try:
            return render_template(self._template(source, name), context, name=name)
        except TemplateError as exc:
            line = exc.line if not loc else None
            raise RenderError(_message(exc), file=file, line=line, loc=loc) from None

    def _inline(self, entry: Any, raw: str, loc: tuple, context: dict[str, Any]) -> str:
        name = f"{entry.path.as_posix()}#{loc}"
        text = self._render(raw, name, context, file=entry.path, loc=loc)
        if "\n" in text.strip("\n"):
            raise RenderError("renders to more than one line", file=entry.path, loc=loc)
        return text

    @staticmethod
    def _lines(text: str, driver: PlatformDriver) -> list[str]:
        # Split on "\n" only: str.splitlines() also breaks on \x85,   and others.
        lines = (line.rstrip() for line in text.split("\n"))
        return [line for line in lines if line.strip() and not driver.is_comment(line)]

    @staticmethod
    def _sinks(
        lines: list[str],
        collector: SecretCollector,
        driver: PlatformDriver,
        *,
        file: Path,
        loc: tuple = (),
    ) -> None:
        parent = None
        for line in lines:
            if collector.contains(line):
                try:
                    collector.check_sink(line, driver, parent)
                except SecretRefError as exc:
                    raise RenderError(str(exc), file=file, loc=loc) from None
            parent = top_level_parent(line, parent)

    def _platform_rules(
        self, entry: Any, play: Play, steps: tuple[RenderedStep, ...], driver: PlatformDriver
    ) -> None:
        settings = play.settings()
        family = str(driver.family)
        limit = MAX_REVERT_MINUTES.get(family)
        if settings.rollback == RollbackMode.REVERT_TIMER and limit is not None:
            if settings.revert_minutes > limit:
                raise RenderError(
                    f"{family} supports revert_minutes up to {limit}",
                    file=entry.path,
                    loc=("execution", "revert_minutes"),
                )
        for step in steps:
            if step.skipped:
                continue
            loc = ("steps", step.index)
            allowed = play.steps[step.index].allow_destructive
            for line in step.lines:
                if step.kind == "exec":
                    guarded = driver.guarded_exec_command(line)
                else:
                    guarded = driver.guarded_config_line(line)
                    reboot = family == "aos-s" and _starts_with(line, REBOOT_REQUIRED)
                    if reboot and settings.rollback != RollbackMode.NONE:
                        raise RenderError(
                            f"{reboot!r} needs a reboot on AOS-S, so cfg-restore could not roll "
                            "it back; set rollback: none for this play",
                            file=entry.path,
                            loc=loc,
                        )
                if guarded and not allowed:
                    raise RenderError(
                        f"{guarded!r} is a guarded command on {family}; "
                        "set allow_destructive: true on this step",
                        file=entry.path,
                        loc=loc,
                    )


def _starts_with(line: str, commands: tuple[str, ...]) -> str | None:
    words = " ".join(line.split()).lower()
    return next((c for c in commands if words == c or words.startswith(c + " ")), None)


def _message(exc: TemplateError) -> str:
    return exc.args[0] if exc.args else str(exc)


# -- validation renders ---------------------------------------------------------------


def validation_render_issues(workspace: Any) -> list[Issue]:
    """Render every valid play for every target with placeholder prompts (design.md,
    Cross-field validation). Plays that already have errors are skipped."""
    renderer = Renderer(workspace)
    issues: dict[tuple, Issue] = {}
    for path, entry in workspace.plays.items():
        if entry.play is None or any(
            i.severity == Severity.ERROR for i in workspace.issues_for(path)
        ):
            continue
        for issue in entry_render_issues(workspace, entry, workspace.play_docs.get(path), renderer):
            issues.setdefault((issue.file, issue.line, issue.message), issue)
    return list(issues.values())


def entry_render_issues(
    workspace: Any, entry: Any, doc: Any = None, renderer: Renderer | None = None
) -> list[Issue]:
    """Validation-render one play for each of its targets; doc maps play locations to lines."""
    renderer = renderer or Renderer(workspace)
    path = entry.path
    issues: dict[tuple, Issue] = {}
    for device, answers, who in _validation_targets(workspace, entry):
        try:
            renderer.render(entry, device=device, prompt_values=answers, validation=True)
        except RenderError as exc:
            file = exc.file or path
            line = exc.line
            if exc.loc and file == path and doc is not None:
                line = doc.line_for(exc.loc)
            key = (file, line, str(exc))
            if key not in issues:
                message = f"{exc} (rendering {path.as_posix()} for {who})"
                issues[key] = Issue(Severity.ERROR, "render.failed", message, file, line)
    return list(issues.values())


def _validation_targets(workspace: Any, entry: Any) -> Iterator[tuple[str | None, dict, str]]:
    play: Play = entry.play
    cust = workspace.customers.get(play.customer)
    if cust is None:
        return
    catalog = load_catalog()
    sites, roles = frozenset(cust.sites), frozenset(workspace.roles)
    if play.type != PlayType.INITIAL:
        result = resolve_targets(play.targets, cust.devices, sites=sites, roles=roles)
        for device in result.devices:
            context = PromptContext(sites, roles, catalog.ids(), PLATFORM_FAMILY[device.platform])
            yield device.name, _placeholders(play.prompts, context), f"device {device.name}"
        return
    # A new device: one render per platform the play can stage.
    model_prompt = next((n for n, p in play.prompts.items() if p.type == PromptType.MODEL), None)
    platforms: list[Platform]
    if "platform" in play.vars:
        platforms = [Platform(play.vars["platform"])]
    else:
        platforms = sorted({e.platform for e in catalog.selectable()})
    for platform in platforms:
        if model_prompt and not all(
            find_snippet(
                workspace.root,
                s.snippet,
                customer=play.customer,
                platform=platform,
                draft=entry.draft,
            )
            for s in play.steps
            if s.snippet
        ):
            continue  # the model dropdown will not offer this platform
        context = PromptContext(sites, roles, catalog.ids(), PLATFORM_FAMILY[platform])
        answers = _placeholders(play.prompts, context)
        if model_prompt:
            answers[model_prompt] = next(
                e.id for e in catalog.selectable() if e.platform == platform
            )
            answers["platform"] = str(platform)
        yield None, answers, f"platform {platform}"


def _placeholders(prompts: Mapping[str, Prompt], context: PromptContext) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for name, prompt in prompts.items():
        kwargs = {"choices": prompt.choices, "minimum": prompt.min, "maximum": prompt.max}
        default = prompt.default
        try:
            if default is not None and not (isinstance(default, str) and "{" in default):
                values[name] = coerce(
                    prompt.type, default, context=context, pattern=prompt.pattern, **kwargs
                )
            else:
                values[name] = placeholder(prompt.type, context=context, **kwargs)
        except PromptValueError:
            values[name] = placeholder(prompt.type, context=context, **kwargs)
    return values
