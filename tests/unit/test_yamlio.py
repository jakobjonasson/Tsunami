from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError

from tsunami.core.yamlio import (
    YamlLoadError,
    dump_roundtrip,
    load_roundtrip,
    load_yaml,
    load_yaml_text,
    validation_issues,
)

FIXTURES = Path(__file__).parents[1] / "fixtures" / "workspace"

PLAY = """\
# comment line
name: Add VLAN
steps:
  - snippet: base
  - config:
      - vlan 10
      - " name Users"
vars:
  flag: yes
  ratio: 2.5
  enabled: true
  missing: null
"""


def test_plain_types_and_lines():
    doc = load_yaml_text(PLAY, Path("play.yml"))
    assert doc.data["vars"] == {"flag": "yes", "ratio": 2.5, "enabled": True, "missing": None}
    assert type(doc.data["vars"]["ratio"]) is float  # not ruamel's ScalarFloat
    assert doc.lines[("name",)] == 2
    assert doc.lines[("steps", 1)] == 5
    assert doc.lines[("steps", 1, "config", 1)] == 7


@pytest.mark.parametrize(
    ("loc", "line"),
    [
        (("steps", 1, "config", 0), 6),
        (("steps", 1, "some-union-tag", "config"), 5),  # union tags are skipped
        (("steps", 0, "tags"), 4),  # missing field falls back to its parent
        (("nowhere",), 1),
        ((), 1),
    ],
)
def test_line_for(loc, line):
    assert load_yaml_text(PLAY, Path("play.yml")).line_for(loc) == line


class Step(BaseModel):
    model_config = ConfigDict(extra="forbid")
    snippet: str


class Play(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    steps: list[Step]


def test_validation_issues_point_at_lines():
    text = "name: x\nsteps:\n  - snippet: base\n  - snipet: typo\n"
    doc = load_yaml_text(text, Path("plays/push/x.yml"))
    with pytest.raises(ValidationError) as caught:
        Play.model_validate(doc.data)
    issues = validation_issues(caught.value, doc, code="play")
    found = {(i.code, i.line, i.message) for i in issues}
    assert ("play.extra_forbidden", 4, "steps[1].snipet: Extra inputs are not permitted") in found
    assert ("play.missing", 4, "steps[1].snippet: Field required") in found
    assert all(i.file == Path("plays/push/x.yml") for i in issues)


def test_validation_issues_never_echo_input():
    doc = load_yaml_text("name: x\nsteps: hunter2-secret\n", Path("x.yml"))
    with pytest.raises(ValidationError) as caught:
        Play.model_validate(doc.data)
    assert "hunter2" not in " ".join(str(i) for i in validation_issues(caught.value, doc, code="p"))


def test_duplicate_key_reports_key_but_not_values():
    with pytest.raises(YamlLoadError) as caught:
        load_yaml_text("a: 1\nkey: first-secret\nkey: second-secret\n", Path("x.yml"))
    issue = caught.value.issue
    assert issue.code == "yaml.duplicate_key"
    assert issue.line == 3
    assert "'key'" in issue.message
    assert "secret" not in issue.message


def test_syntax_error_has_line():
    with pytest.raises(YamlLoadError) as caught:
        load_yaml_text("a: 1\nb: [1, 2\nc: 3\n", Path("x.yml"))
    assert caught.value.issue.code == "yaml.syntax"
    assert caught.value.issue.line is not None


def test_byte_order_mark_is_accepted(tmp_path):
    path = tmp_path / "global.yml"
    path.write_bytes("﻿domain: example.local\n".encode())
    assert load_yaml(path).data == {"domain": "example.local"}


def test_empty_file_loads_as_none():
    assert load_yaml_text("", Path("x.yml")).data is None


@pytest.mark.parametrize(
    "relative",
    [
        "inventory/customers/acme/devices.yml",
        "inventory/customers/acme/customer.yml",
        "plays/initial/acme-access-day0.yml",
    ],
)
def test_roundtrip_without_changes_keeps_file_identical(tmp_path, relative):
    path = tmp_path / Path(relative).name
    shutil.copy(FIXTURES / relative, path)
    original = path.read_text(encoding="utf-8")
    dump_roundtrip(load_roundtrip(path))
    assert path.read_text(encoding="utf-8") == original


def test_roundtrip_append_keeps_comments_and_style(tmp_path):
    path = tmp_path / "devices.yml"
    path.write_text(
        "# acme devices\n- name: sa-as01  # first switch\n  host: 203.0.113.11\n  vars:\n"
        "    mgmt_ip: 203.0.113.11/24\n",
        encoding="utf-8",
    )
    doc = load_roundtrip(path)
    doc.data.append({"name": "sa-as03", "host": "203.0.113.13"})
    dump_roundtrip(doc)
    text = path.read_text(encoding="utf-8")
    assert text.startswith("# acme devices\n- name: sa-as01  # first switch\n  host: 203.0.113.11")
    assert text.endswith("- name: sa-as03\n  host: 203.0.113.13\n")
