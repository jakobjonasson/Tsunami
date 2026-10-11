"""Sandboxed Jinja environment (design.md, Snippets and rendering: Jinja environment)."""

from __future__ import annotations

from typing import Any

import pytest

from tsunami.core.issues import TsunamiError
from tsunami.core.render.environment import (
    TemplateError,
    compile_expression,
    compile_template,
    make_environment,
    referenced_names,
    render_template,
)

NAME = "common/ios/test.j2"


def render(source: str, **variables: Any) -> str:
    env = make_environment()
    return render_template(compile_template(env, source, name=NAME), variables, name=NAME)


def test_template_error_is_tsunami_error() -> None:
    err = TemplateError("boom", template="a.j2", line=3)
    assert isinstance(err, TsunamiError)
    assert (err.template, err.line) == ("a.j2", 3)


def test_undefined_variable_names_it_and_line() -> None:
    src = "hostname {{ hostname }}\nip domain name x\nntp server {{ missing_var }}\n"
    with pytest.raises(TemplateError) as info:
        render(src, hostname="sw1")
    assert "missing_var" in str(info.value)
    assert info.value.template == NAME
    assert info.value.line == 3


def test_line_number_inside_loop() -> None:
    with pytest.raises(TemplateError) as info:
        render("a\n{% for s in items %}\n{{ s | ip }}\n{% endfor %}\n", items=["10.0.0.1"])
    assert info.value.line == 3
    assert "prefix" in str(info.value)


def test_syntax_error_has_line() -> None:
    with pytest.raises(TemplateError) as info:
        compile_template(make_environment(), "ok\nok\n{% if %}\n", name=NAME)
    assert info.value.line == 3
    assert info.value.template == NAME


@pytest.mark.parametrize(
    "tag",
    [
        "{% include 'x.j2' %}",
        "{% import 'x.j2' as x %}",
        "{% from 'x.j2' import y %}",
        "{% extends 'x.j2' %}",
    ],
)
def test_composition_rejected_at_parse_time(tag: str) -> None:
    with pytest.raises(TemplateError) as info:
        compile_template(make_environment(), f"line1\nline2\n{tag}\n", name=NAME)
    assert info.value.line == 3


def test_filters_registered() -> None:
    out = render(
        "{{ a | ip }} {{ a | netmask }} {{ v | vlan_range }}", a="10.1.1.5/24", v=[1, 2, 3]
    )
    assert out == "10.1.1.5 255.255.255.0 1-3"


def test_globals_available() -> None:
    env = make_environment(globals={"answer": lambda: 42})
    tpl = compile_template(env, "{{ answer() }}", name=NAME)
    assert render_template(tpl, {}, name=NAME) == "42"


ESCAPES = [
    "{{ ''.__class__.__mro__[1].__subclasses__() }}",
    "{{ cycler.__init__.__globals__ }}",
    "{{ self.__init__.__globals__ }}",
    "{{ lipsum.__globals__ }}",
    "{{ range.__class__ }}",
    "{{ range.__class__.__mro__ }}",
    "{{ ().__class__.__bases__[0].__subclasses__() }}",
    "{{ request }}",
    "{% set x = namespace() %}{{ x.__class__ }}",
    "{{ joiner.__init__.__globals__ }}",
    "{{ ''.format.__globals__ }}",
    "{{ ''|attr('__class__') }}",
]


@pytest.mark.parametrize("source", ESCAPES)
def test_sandbox_escapes_fail(source: str) -> None:
    with pytest.raises(TemplateError) as info:
        render(source)
    msg = str(info.value)
    assert "<class" not in msg
    assert "<module" not in msg
    assert "os." not in msg


def test_namespace_and_range_are_harmless() -> None:
    src = (
        "{% set ns = namespace(n=0) %}{% for i in range(3) %}"
        "{% set ns.n = ns.n + i %}{% endfor %}{{ ns.n }}"
    )
    assert render(src) == "3"


def test_trim_and_lstrip_blocks() -> None:
    src = (
        "hostname sw1\n{% for s in ntp_servers %}\nntp server {{ s }}\n{% endfor %}\n"
        "  {% if x %}\nend\n  {% endif %}\n"
    )
    out = render(src, ntp_servers=["192.0.2.1", "192.0.2.2"], x=True)
    assert out == "hostname sw1\nntp server 192.0.2.1\nntp server 192.0.2.2\nend\n"


def test_trailing_newline_kept() -> None:
    assert render("hostname sw1\n") == "hostname sw1\n"
    assert render("hostname sw1") == "hostname sw1"


def test_booleans_render_lowercase() -> None:
    assert render("{{ a }} {{ b }}", a=True, b=False) == "true false"


def test_bool_in_conditions_unchanged() -> None:
    assert render("{% if a %}yes{% endif %}", a=True) == "yes"


def test_none_renders_as_none() -> None:
    assert render("{{ a }}", a=None) == "None"


def test_other_values_untouched() -> None:
    assert render("{{ a }} {{ b }}", a=0, b="x") == "0 x"


def test_autoescape_off() -> None:
    assert render("{{ a }}", a="<&>") == "<&>"


def test_expression() -> None:
    expr = compile_expression(make_environment(), "platform == 'ios-xe'", name="when")
    assert expr(platform="ios-xe") is True
    assert expr(platform="eos") is False


def test_expression_syntax_error() -> None:
    with pytest.raises(TemplateError):
        compile_expression(make_environment(), "platform ==", name="when")


def test_expression_undefined_raises_template_error() -> None:
    expr = compile_expression(make_environment(), "nope == 1", name="when")
    with pytest.raises(TemplateError, match="nope"):
        expr()


def test_referenced_names() -> None:
    src = "hostname {{ hostname }}\n{% for s in ntp_servers %}\nntp server {{ s }}\n{% endfor %}\n"
    assert referenced_names(make_environment(), src) == {"hostname", "ntp_servers"}


def test_referenced_names_expression_wrapped() -> None:
    assert referenced_names(make_environment(), "{{ platform == 'x' and flag }}") == {
        "platform",
        "flag",
    }


def test_templates_cannot_mutate_their_variables():
    from tsunami.core.render.environment import compile_template, make_environment, render_template

    env = make_environment()
    servers = ["192.0.2.10"]
    template = compile_template(env, "{{ servers.append('192.0.2.99') }}", name="t")
    with pytest.raises(TemplateError):
        render_template(template, {"servers": servers}, name="t")
    assert servers == ["192.0.2.10"]
