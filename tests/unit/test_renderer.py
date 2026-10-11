"""Renderer, secret() sink rule and validation renders (design.md, Snippets and rendering)."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from tsunami.core.models.settings import AppSettings
from tsunami.core.render.renderer import Renderer, RenderError
from tsunami.core.render.secret_refs import MASK, SecretCollector, SecretRefError
from tsunami.core.workspace import load_workspace

FIXTURE = Path(__file__).parents[1] / "fixtures" / "workspace"
BASELINE = Path("plays/baseline/acme-baseline.yml")
DAY0 = Path("plays/initial/acme-access-day0.yml")

ARUBA_DEVICES = """
- name: sa-cx01
  host: 203.0.113.21
  site: site-a
  role: distribution
  platform: aos-cx
- name: sa-s01
  host: 203.0.113.31
  site: site-a
  role: distribution
  platform: aos-s
"""


@pytest.fixture
def ws_root(tmp_path) -> Path:
    root = tmp_path / "ws"
    shutil.copytree(FIXTURE, root)
    with (root / "inventory/customers/acme/devices.yml").open("a", encoding="utf-8") as f:
        f.write(ARUBA_DEVICES)
    return root


def write(root: Path, relative: str, text: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def push_play(root: Path, name: str, body: str, *, targets: str = "devices: [sa-as01]") -> Path:
    relative = f"plays/push/{name}.yml"
    write(
        root, relative, f"name: {name}\ntype: push\ncustomer: acme\ntargets:\n  {targets}\n{body}"
    )
    return Path(relative)


def render(root: Path, play: Path, device: str = "sa-as01", **kwargs):
    ws = load_workspace(root, render=False)
    return Renderer(ws, kwargs.pop("settings", None)).render(
        ws.plays[play], device=device, **kwargs
    )


# -- masking, hashing, materializing --------------------------------------------


def test_baseline_render_is_masked_and_hashable(ws_root):
    first = render(ws_root, BASELINE)
    second = render(ws_root, BASELINE)
    assert " key ********" in first.masked_lines()
    assert first.secret_refs == ("acme/tacacs",)
    assert "\x00" not in first.masked_text()
    assert first.render_hash() == second.render_hash()  # nonces differ, hashes do not

    write(ws_root, "snippets/common/ios/ntp.j2", "ntp server 192.0.2.99\n")
    assert render(ws_root, BASELINE).render_hash() != first.render_hash()


def test_materialize_inserts_values_and_requires_all(ws_root):
    rendering = render(ws_root, BASELINE)
    steps = rendering.materialize({"acme/tacacs": "Real-Tacacs-Key"})
    assert " key Real-Tacacs-Key" in steps[1].lines
    assert "Real-Tacacs-Key" not in rendering.masked_text()
    with pytest.raises(Exception, match="missing secrets: acme/tacacs"):
        rendering.materialize({})


def test_comments_and_blank_lines_are_dropped(ws_root):
    write(ws_root, "snippets/common/ios/svc.j2", "! a comment\n\nservice timestamps log\n  \n")
    play = push_play(ws_root, "svc", "steps:\n  - snippet: svc\n")
    assert render(ws_root, play).masked_lines() == ["service timestamps log"]


# -- the sink rule ------------------------------------------------------------------


@pytest.mark.parametrize(
    "line",
    [
        "enable secret {{ secret('acme/enable') }}",
        "username netadmin privilege 15 secret {{ secret('acme/admin') }}",
        "snmp-server community {{ secret('acme/ro') }} RO",
        "tacacs server ISE1\n key {{ secret('acme/tacacs') }}",
        "router bgp 65000\n neighbor 192.0.2.1 password {{ secret('acme/bgp') }}",
    ],
)
def test_secret_allowed_in_sinks(ws_root, line):
    write(ws_root, "snippets/common/ios/s.j2", line + "\n")
    play = push_play(ws_root, "s", "steps:\n  - snippet: s\n")
    assert MASK in render(ws_root, play).masked_text()


@pytest.mark.parametrize(
    "line",
    [
        "interface GigabitEthernet1/0/1\n description {{ secret('acme/x') }}",
        "interface GigabitEthernet1/0/1\n description secret {{ secret('acme/x') }}",
        "banner motd ^ {{ secret('acme/x') }} ^",
        "hostname {{ secret('acme/x') }}",
        "snmp-server location {{ secret('acme/x') }}",
        "username {{ secret('acme/x') }} privilege 15 secret 0 x",
    ],
)
def test_secret_refused_outside_sinks(ws_root, line):
    write(ws_root, "snippets/common/ios/s.j2", line + "\n")
    play = push_play(ws_root, "s", "steps:\n  - snippet: s\n")
    with pytest.raises(RenderError, match="not allowed here"):
        render(ws_root, play)
    issues = load_workspace(ws_root).issues_for(Path("snippets/common/ios/s.j2"))
    assert [i.code for i in issues] == ["render.failed"]


def test_secret_from_another_customer_is_refused(ws_root):
    write(ws_root, "snippets/common/ios/s.j2", "enable secret {{ secret('globex/enable') }}\n")
    play = push_play(ws_root, "s", "steps:\n  - snippet: s\n")
    with pytest.raises(RenderError, match="another customer"):
        render(ws_root, play)


def test_sentinels_cannot_be_forged():
    first, second = SecretCollector("acme"), SecretCollector("acme")
    token = first("acme/tacacs")
    assert first.contains(token) and not first.altered(token)
    assert second.mask(token) == token  # not recognized, so never materialized either
    assert second.altered(token)  # and a stray NUL in output is an error at the sink check
    with pytest.raises(SecretRefError):
        first("acme/../x")


@pytest.mark.parametrize(
    "expression",
    [
        "secret('acme/tacacs') | reverse",
        "secret('acme/tacacs') | list",
        "secret('acme/tacacs') | tojson",
        "secret('acme/tacacs') | truncate(9)",
        "secret('acme/tacacs')[1:12]",
        "secret('acme/tacacs') | replace(secret('acme/tacacs')[0], '')",
    ],
)
def test_altered_sentinel_is_an_error(ws_root, expression):
    write(ws_root, "snippets/common/ios/s.j2", f" key {{{{ {expression} }}}}\n")
    play = push_play(ws_root, "s", "steps:\n  - snippet: s\n")
    with pytest.raises(RenderError, match="changed by a filter"):
        render(ws_root, play)


def test_settings_can_add_a_sink(ws_root):
    write(ws_root, "snippets/common/ios/s.j2", "custom-auth {{ secret('acme/x') }}\n")
    play = push_play(ws_root, "s", "steps:\n  - snippet: s\n")
    with pytest.raises(RenderError):
        render(ws_root, play)
    settings = AppSettings(extra_sink_patterns={"ios": [r"^custom-auth\s+(?P<secret>\S+)"]})
    assert render(ws_root, play, settings=settings).masked_lines() == ["custom-auth ********"]


# -- errors point at the right place ------------------------------------------------


def test_undefined_variable_points_at_snippet_line(ws_root):
    write(
        ws_root, "snippets/common/ios/u.j2", "service timestamps log\n!\nlogging host {{ nope }}\n"
    )
    push_play(ws_root, "u", "steps:\n  - snippet: u\n")
    issues = load_workspace(ws_root).issues_for(Path("snippets/common/ios/u.j2"))
    assert [(i.code, i.line) for i in issues] == [("render.failed", 3)]
    assert "nope" in issues[0].message


def test_include_is_refused(ws_root):
    write(ws_root, "snippets/common/ios/i.j2", "{% include 'ntp.j2' %}\n")
    push_play(ws_root, "i", "steps:\n  - snippet: i\n")
    issues = load_workspace(ws_root).issues_for(Path("snippets/common/ios/i.j2"))
    assert [(i.code, i.line) for i in issues] == [("render.failed", 1)]


def test_config_line_cannot_expand_to_several_lines(ws_root):
    injected = 'vars:\n  multi: "vlan 10\\nusername x privilege 15"\n'
    play = push_play(ws_root, "m", injected + 'steps:\n  - config:\n      - "{{ multi }}"\n')
    with pytest.raises(RenderError, match="more than one line"):
        render(ws_root, play)
    issues = load_workspace(ws_root).issues_for(play)
    assert [(i.code, i.line) for i in issues] == [("render.failed", 10)]


# -- platform rules -------------------------------------------------------------------


def test_guarded_exec_needs_allow_destructive(ws_root):
    play = push_play(ws_root, "r", "steps:\n  - exec: reload in 5\n")
    with pytest.raises(RenderError, match="guarded command"):
        render(ws_root, play)
    push_play(ws_root, "r", "steps:\n  - exec: reload in 5\n    allow_destructive: true\n")
    assert render(ws_root, play).steps[0].lines == ("reload in 5",)


def test_aos_s_reboot_required_lines_need_rollback_none(ws_root):
    write(ws_root, "snippets/common/aos-s/mv.j2", "max-vlans 512\n")
    body = "steps:\n  - snippet: mv\n    allow_destructive: true\n"
    play = push_play(ws_root, "mv", body, targets="devices: [sa-s01]")
    with pytest.raises(RenderError, match="needs a reboot"):
        render(ws_root, play, device="sa-s01")
    push_play(ws_root, "mv", body + "execution:\n  rollback: none\n", targets="devices: [sa-s01]")
    assert render(ws_root, play, device="sa-s01").masked_lines() == ["max-vlans 512"]


def test_aos_cx_revert_timer_is_capped_at_60_minutes(ws_root):
    write(ws_root, "snippets/common/aos-cx/n.j2", "ntp server 192.0.2.10\n")
    body = "steps:\n  - snippet: n\nexecution:\n  rollback: revert_timer\n  revert_minutes: 90\n"
    play = push_play(ws_root, "n", body, targets="devices: [sa-cx01]")
    with pytest.raises(RenderError, match="up to 60"):
        render(ws_root, play, device="sa-cx01")


def test_aruba_secrets_use_their_own_sinks(ws_root):
    write(
        ws_root,
        "snippets/common/aos-cx/r.j2",
        "radius-server host 192.0.2.20 key plaintext {{ secret('acme/radius') }}\n",
    )
    write(
        ws_root,
        "snippets/common/aos-s/r.j2",
        "radius-server host 192.0.2.20 key \"{{ secret('acme/radius') }}\"\n",
    )
    cx = push_play(ws_root, "cx", "steps:\n  - snippet: r\n", targets="devices: [sa-cx01]")
    assert render(ws_root, cx, device="sa-cx01").masked_lines() == [
        "radius-server host 192.0.2.20 key plaintext ********"
    ]
    s = push_play(ws_root, "s", "steps:\n  - snippet: r\n", targets="devices: [sa-s01]")
    assert render(ws_root, s, device="sa-s01").masked_lines() == [
        'radius-server host 192.0.2.20 key "********"'
    ]


# -- tags and when ------------------------------------------------------------------------


def test_tag_filter_keeps_always_steps(ws_root):
    steps = render(ws_root, BASELINE, tags=["ntp"]).steps
    assert [(s.label, s.skipped) for s in steps] == [
        ("archive", None),
        ("aaa", "tags"),
        ("ntp", None),
    ]


def test_when_on_facts_is_true_in_validation_only(ws_root):
    write(ws_root, "snippets/common/ios/f.j2", "service timestamps log\n")
    play = push_play(
        ws_root, "f", "steps:\n  - snippet: f\n    when: facts.version.startswith('17')\n"
    )
    assert render(ws_root, play, validation=True).steps[0].skipped is None
    real = render(ws_root, play, facts={"version": "16.12.4"})
    assert real.steps[0].skipped == "when"


def test_day0_renders_for_the_chosen_model(ws_root):
    answers = {
        "model": "cisco-c9300",
        "hostname": "SA-AS03",
        "site": "site-a",
        "mgmt_vlan": 10,
        "mgmt_ip": "203.0.113.13/24",
        "gateway": "203.0.113.1",
        "uplink": "TenGigabitEthernet1/1/1",
    }
    rendering = render(ws_root, DAY0, device=None, prompt_values=answers)
    lines = rendering.masked_lines()
    assert "hostname SA-AS03" in lines
    assert " ip address 203.0.113.13 255.255.255.0" in lines
    assert "interface TenGigabitEthernet1/1/1" in lines  # ios-xe-only step ran
    assert rendering.checks[0].exec == "show interfaces TenGigabitEthernet1/1/1 status"


def test_sample_workspace_still_validates_clean_with_renders():
    assert load_workspace(FIXTURE).issues == []
