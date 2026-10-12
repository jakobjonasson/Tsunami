"""The single API the GUI, dev CLI and MCP server call. Front ends hold no logic.

Grows each phase. Phase 2: collecting prompt values and rendering a play.

See design.md, Architecture: Components; Execution engine: Run level (steps 3 and 4).
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from pathlib import Path
from typing import Any

from tsunami.core.catalog import load_catalog
from tsunami.core.issues import TsunamiError
from tsunami.core.models.common import PLATFORM_FAMILY, Platform
from tsunami.core.models.play import GLOBAL_CUSTOMER, PromptType
from tsunami.core.models.settings import AppSettings
from tsunami.core.prompts import PromptContext, PromptValueError, coerce
from tsunami.core.render.environment import (
    TemplateError,
    compile_template,
    make_environment,
    render_template,
)
from tsunami.core.render.renderer import Renderer, Rendering
from tsunami.core.workspace import PlayEntry, Workspace

# Asked first in initial plays; the rest are pre-filled from variables these select.
FIRST_STAGE = (PromptType.MODEL, PromptType.SITE, PromptType.ROLE)
_ENV = make_environment()  # for Jinja-expression prompt defaults


def play_entry(
    workspace: Workspace, play_path: Path | str, customer: str | None = None
) -> PlayEntry:
    """The play at play_path. A global play is bound to customer: everything downstream
    (rendering, targets, secrets, the job's customer) then sees that customer."""
    entry = workspace.plays.get(Path(play_path))
    if entry is None:
        raise TsunamiError(f"no play at {Path(play_path).as_posix()}")
    if entry.play is None:
        raise TsunamiError(f"{Path(play_path).as_posix()} failed validation")
    if entry.play.customer != GLOBAL_CUSTOMER:
        if customer is not None and customer != entry.play.customer:
            raise TsunamiError(f"{Path(play_path).as_posix()} is for {entry.play.customer!r}")
        return entry
    if customer is None:
        raise TsunamiError(
            f"{Path(play_path).as_posix()} is a global play: choose the customer to run it for"
        )
    if customer not in workspace.customers:
        raise TsunamiError(f"unknown customer {customer!r}")
    bound = entry.play.model_copy(update={"customer": customer})
    return PlayEntry(path=entry.path, draft=entry.draft, play=bound)


def collect_prompts(
    workspace: Workspace,
    entry: PlayEntry,
    raw: Mapping[str, Any],
    *,
    device: str | None = None,
) -> dict[str, Any]:
    """Validate raw prompt answers and fill defaults (design.md, prompts).

    Raises TsunamiError listing every problem. In initial plays, model, site and role
    are coerced first and the remaining defaults are resolved with them.
    """
    play = entry.play
    assert play is not None
    cust = workspace.customer(play.customer)
    catalog = load_catalog()
    unknown = sorted(set(raw) - set(play.prompts))
    problems = [f"{name}: not a prompt of this play" for name in unknown]
    values: dict[str, Any] = {}

    def context() -> PromptContext:
        platform = None
        if device is not None and (dev := cust.device(device)) is not None:
            platform = dev.platform
        elif "platform" in play.vars:
            platform = Platform(play.vars["platform"])
        else:
            model = next(
                (
                    values[n]
                    for n, p in play.prompts.items()
                    if p.type == PromptType.MODEL and n in values
                ),
                None,
            )
            platform = catalog.get(model).platform if model else None
        family = PLATFORM_FAMILY[platform] if platform else None
        return PromptContext(
            frozenset(cust.sites), frozenset(workspace.roles), catalog.ids(), family
        )

    stages = (
        [(n, p) for n, p in play.prompts.items() if p.type in FIRST_STAGE],
        [(n, p) for n, p in play.prompts.items() if p.type not in FIRST_STAGE],
    )
    for stage in stages:
        resolved = None
        for name, prompt in stage:
            value = raw.get(name)
            if value is None and prompt.default is not None:
                value = prompt.default
                if isinstance(value, str) and "{" in value:
                    if resolved is None:
                        resolved = workspace.resolve_vars(
                            play.customer, device=device, play=entry, prompt_values=values
                        ).values
                    try:
                        template = compile_template(_ENV, value, name=f"prompt {name} default")
                        value = render_template(template, resolved, name=name).strip()
                    except TemplateError as exc:
                        problems.append(f"{name}: default could not be computed ({exc})")
                        continue
            if value is None:
                if prompt.required:
                    problems.append(f"{name}: required")
                continue
            try:
                values[name] = coerce(
                    prompt.type,
                    value,
                    context=context(),
                    choices=prompt.choices,
                    pattern=prompt.pattern,
                    minimum=prompt.min,
                    maximum=prompt.max,
                )
            except PromptValueError as exc:
                problems.append(f"{name}: {exc}")
    if problems:
        raise TsunamiError("invalid prompt values:\n  " + "\n  ".join(problems))
    return values


def render_play(
    workspace: Workspace,
    play_path: Path | str,
    *,
    device: str | None = None,
    prompts: Mapping[str, Any] | None = None,
    tags: Collection[str] | None = None,
    settings: AppSettings | None = None,
    customer: str | None = None,
) -> Rendering:
    """Render one play for one device (or a new device, for initial plays), masked.
    customer binds a global play."""
    entry = play_entry(workspace, play_path, customer)
    values = collect_prompts(workspace, entry, prompts or {}, device=device)
    return Renderer(workspace, settings).render(
        entry, device=device, prompt_values=values, tags=tags
    )
