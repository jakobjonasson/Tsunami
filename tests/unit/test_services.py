"""Prompt collection and play rendering through the services API."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from tsunami import cli
from tsunami.core.issues import TsunamiError
from tsunami.core.services import collect_prompts, play_entry, render_play
from tsunami.core.workspace import load_workspace

FIXTURE = Path(__file__).parents[1] / "fixtures" / "workspace"
DAY0 = "plays/initial/acme-access-day0.yml"
ADD_VLAN = "plays/push/add-vlan.yml"

DAY0_ANSWERS = {
    "model": "cisco-c9300",
    "hostname": "SA-AS03",
    "site": "site-a",
    "mgmt_vlan": "10",
    "mgmt_ip": "203.0.113.13/24",
    "gateway": "203.0.113.1",
}


@pytest.fixture(scope="module")
def ws():
    return load_workspace(FIXTURE)


def test_collect_coerces_and_fills_literal_defaults(ws):
    values = collect_prompts(ws, play_entry(ws, DAY0), DAY0_ANSWERS)
    assert values["mgmt_vlan"] == 10  # coerced from text
    assert values["uplink"] == "TenGigabitEthernet1/1/1"  # literal default, checked for ios


def test_collect_reports_every_problem(ws):
    bad = {**DAY0_ANSWERS, "mgmt_vlan": "4095", "site": "nowhere", "bogus": "x"}
    del bad["hostname"]
    with pytest.raises(TsunamiError) as caught:
        collect_prompts(ws, play_entry(ws, DAY0), bad)
    message = str(caught.value)
    for expected in (
        "hostname: required",
        "mgmt_vlan:",
        "site: unknown site",
        "bogus: not a prompt",
    ):
        assert expected in message


def test_jinja_expression_defaults_use_resolved_variables(tmp_path):
    root = tmp_path / "ws"
    shutil.copytree(FIXTURE, root)
    play = root / ADD_VLAN
    text = play.read_text(encoding="utf-8").replace(
        "  vlan_name:\n    type: string\n",
        '  vlan_name:\n    type: string\n    default: "{{ site_code }}-users"\n',
    )
    play.write_text(text, encoding="utf-8")
    workspace = load_workspace(root)
    values = collect_prompts(
        workspace, play_entry(workspace, ADD_VLAN), {"vlan_id": 20}, device="sa-as01"
    )
    assert values == {"vlan_id": 20, "vlan_name": "SA-users"}


def test_render_play_end_to_end(ws):
    rendering = render_play(
        ws, ADD_VLAN, device="sa-as01", prompts={"vlan_id": "30", "vlan_name": "Cameras"}
    )
    assert rendering.masked_lines() == ["vlan 30", " name Cameras"]


def test_cli_render(capsys):
    code = cli.main(["render", ADD_VLAN, "-w", str(FIXTURE), "-d", "sa-as01",
                     "--set", "vlan_id=30", "--set", "vlan_name=Cameras"])  # fmt: skip
    assert code == 0
    out = capsys.readouterr().out
    assert "vlan 30\n name Cameras\n" in out

    assert cli.main(["render", ADD_VLAN, "-w", str(FIXTURE), "-d", "sa-as01"]) == 1
    assert "vlan_id: required" in capsys.readouterr().err
