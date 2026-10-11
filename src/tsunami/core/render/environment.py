"""SandboxedEnvironment with StrictUndefined and no loader.

include, import and extends are rejected at parse time.

See design.md, Snippets and rendering: Jinja environment. Claude writes templates, so this
module is a security boundary: every template and `when:` expression is built through here.
"""

from __future__ import annotations

import traceback
from collections.abc import Callable, Mapping
from typing import Any

import jinja2
from jinja2 import meta, nodes
from jinja2.sandbox import ImmutableSandboxedEnvironment, SandboxedEnvironment

from tsunami.core.issues import TsunamiError
from tsunami.core.render.filters import FILTERS

_FORBIDDEN: dict[type[nodes.Node], str] = {
    nodes.Include: "include",
    nodes.Import: "import",
    nodes.FromImport: "from ... import",
    nodes.Extends: "extends",
}


class TemplateError(TsunamiError):
    """A template failed to parse or render. Carries the template name and 1-based line."""

    def __init__(
        self, message: str, *, template: str | None = None, line: int | None = None
    ) -> None:
        super().__init__(message)
        self.template = template
        self.line = line

    def __str__(self) -> str:
        where = ""
        if self.template:
            where = f"{self.template}:{self.line}: " if self.line else f"{self.template}: "
        return where + super().__str__()


def _finalize(value: Any) -> Any:
    # Prompt type bool renders as true/false. None is deliberately left alone: it renders
    # as "None" rather than silently vanishing, so a missing value is visible in the output.
    if isinstance(value, bool):
        return "true" if value else "false"
    return value


def make_environment(*, globals: Mapping[str, Any] | None = None) -> SandboxedEnvironment:  # noqa: A002
    """The one Jinja environment configuration Tsunami uses."""
    # Immutable: templates (Claude writes drafts) cannot mutate the variables they are given.
    env = ImmutableSandboxedEnvironment(
        undefined=jinja2.StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
        autoescape=False,
        loader=None,
        finalize=_finalize,
    )
    env.filters.update(FILTERS)
    if globals:
        env.globals.update(globals)
    return env


def _syntax_error(exc: jinja2.TemplateSyntaxError, name: str) -> TemplateError:
    return TemplateError(f"syntax error: {exc.message}", template=name, line=exc.lineno)


def compile_template(env: SandboxedEnvironment, source: str, *, name: str) -> jinja2.Template:
    """Parse source, rejecting include/import/extends. Raises TemplateError with the line."""
    try:
        tree = env.parse(source, name, name)
        for node in tree.find_all(tuple(_FORBIDDEN)):
            raise TemplateError(
                f"{{% {_FORBIDDEN[type(node)]} %}} is not allowed; compose with steps instead",
                template=name,
                line=node.lineno,
            )
        code = env.compile(tree, name, name)
    except jinja2.TemplateSyntaxError as exc:
        raise _syntax_error(exc, name) from None
    return jinja2.Template.from_code(env, code, env.make_globals(None))


def _template_line(exc: BaseException, name: str) -> int | None:
    for frame in reversed(traceback.extract_tb(exc.__traceback__)):
        if frame.filename == name:
            return frame.lineno
    return None


def render_template(template: jinja2.Template, variables: Mapping[str, Any], *, name: str) -> str:
    """Render; convert any error to TemplateError with the template name and line if known."""
    try:
        return template.render(dict(variables))
    except TemplateError:
        raise
    except Exception as exc:
        raise TemplateError(
            f"{type(exc).__name__}: {exc}", template=name, line=_template_line(exc, name)
        ) from None


def compile_expression(
    env: SandboxedEnvironment, expression: str, *, name: str
) -> Callable[..., Any]:
    """For step `when:` expressions (no braces). Raises TemplateError on syntax errors.

    The returned callable takes the variables as keyword arguments and raises TemplateError
    if evaluation fails, for example on an undefined variable.
    """
    try:
        compiled = env.compile_expression(expression, undefined_to_none=False)
    except jinja2.TemplateSyntaxError as exc:
        raise _syntax_error(exc, name) from None

    def evaluate(*args: Any, **kwargs: Any) -> Any:
        try:
            return compiled(*args, **kwargs)
        except Exception as exc:
            raise TemplateError(f"{type(exc).__name__}: {exc}", template=name, line=1) from None

    return evaluate


def referenced_names(env: SandboxedEnvironment, source: str) -> set[str]:
    """Top-level variable names a template uses. For an expression, pass it wrapped in `{{ }}`."""
    try:
        return set(meta.find_undeclared_variables(env.parse(source)))
    except jinja2.TemplateSyntaxError as exc:
        raise _syntax_error(exc, "<template>") from None
