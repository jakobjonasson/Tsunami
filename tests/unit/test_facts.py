"""Facts parsing and platform detection (work package 3.5)."""

from __future__ import annotations

from pathlib import Path

import pytest

from tsunami.core.facts import Facts, detect_platform, facts_commands, parse_facts
from tsunami.core.models.common import Platform, PlatformFamily

SHOW = Path(__file__).parent.parent / "fixtures" / "show"


def _read(family: str, name: str) -> str:
    return (SHOW / family / name).read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("name", "platform", "model", "version", "serial", "uptime"),
    [
        (
            "2960x_show_version.txt",
            Platform.IOS,
            "WS-C2960X-48FPD-L",
            "15.2(7)E3",
            "FCW0000A0AA",
            "3 weeks, 2 days, 4 hours, 12 minutes",
        ),
        (
            "c1000_show_version.txt",
            Platform.IOS,
            "C1000-24P-4G-L",
            "15.2(7)E4",
            "FOC0000B1BB",
            "5 days, 1 hour, 30 minutes",
        ),
        (
            "c9300_show_version.txt",
            Platform.IOS_XE,
            "C9300-48P",
            "17.06.04",
            "FOC0000C2CC",
            "2 weeks, 6 days, 22 hours, 5 minutes",
        ),
        (
            "3850_show_version.txt",
            Platform.IOS_XE,
            "WS-C3850-48P",
            "16.09.04",
            "FOC0000D3DD",
            "41 weeks, 3 days, 1 hour, 9 minutes",
        ),
    ],
)
def test_ios_fixtures(name, platform, model, version, serial, uptime):
    out = _read("ios", name)
    facts = parse_facts("ios", {"show version": out})
    assert facts == Facts(platform, model, version, serial, uptime)
    assert detect_platform(out) is platform


def test_aos_cx_fixture():
    outputs = {
        "show version": _read("aos-cx", "6300_show_version.txt"),
        "show system": _read("aos-cx", "6300_show_system.txt"),
    }
    facts = parse_facts(PlatformFamily.AOS_CX, outputs)
    assert facts.platform is Platform.AOS_CX
    assert facts.model == "JL665A 6300F 48G CL4 PoE 4SFP56 Sw"
    assert facts.version == "FL.10.11.1021"
    assert facts.serial == "SG0000E4EE"
    assert facts.uptime == "1 week, 5 hours, 28 minutes"


def test_aos_cx_version_falls_back_to_show_version():
    outputs = {
        "show version": _read("aos-cx", "6300_show_version.txt"),
        "show system": "Up Time : 12 minutes\n",
    }
    facts = parse_facts("aos-cx", outputs)
    assert facts.version == "FL.10.11.1021"
    assert facts.uptime == "12 minutes"
    assert facts.model is None


def test_aos_s_single_switch_fixture():
    outputs = {
        "show version": _read("aos-s", "2930f_show_version.txt"),
        "show system information": _read("aos-s", "2930f_show_system_information.txt"),
        "show modules": _read("aos-s", "2930f_show_modules.txt"),
    }
    facts = parse_facts("aos-s", outputs)
    assert facts.platform is Platform.AOS_S
    assert facts.model == "2930F-24G-PoE+-4SFP JL261A"
    assert facts.version == "WC.16.11.0001"
    assert facts.serial == "SG0000F5FF"
    assert facts.uptime == "15 days"


def test_aos_s_chassis_modules():
    facts = parse_facts("aos-s", {"show modules": _read("aos-s", "5406r_show_modules.txt")})
    assert facts.model == "5406Rzl2 J9850A"
    assert facts.serial == "SG4BG491BL"  # falls back to the chassis serial


def test_aos_s_version_from_show_version_only():
    facts = parse_facts("aos-s", {"show version": _read("aos-s", "2930f_show_version.txt")})
    assert facts.version == "WC.16.11.0001"
    assert facts.platform is Platform.AOS_S


@pytest.mark.parametrize("family", ["ios", "aos-cx", "aos-s"])
def test_garbage_and_empty_give_none(family):
    for outputs in ({}, {c: "%%% garbage \x00 ???" for c in facts_commands(family)}):
        assert parse_facts(family, outputs) == Facts(None, None, None, None, None)


def test_missing_fields_do_not_crash():
    facts = parse_facts("ios", {"show version": "Cisco IOS Software, Version 15.2(7)E3, X\n"})
    assert facts.platform is Platform.IOS
    assert facts.version == "15.2(7)E3"
    assert facts.model is None and facts.serial is None and facts.uptime is None


def test_ios_model_falls_back_to_cisco_line():
    out = (
        "Cisco IOS Software, Version 15.0(2)SE\ncisco WS-C2960-24TT-L (PowerPC) processor with 1K\n"
    )
    assert parse_facts("ios", {"show version": out}).model == "WS-C2960-24TT-L"


def test_detect_platform_families():
    assert detect_platform(_read("aos-cx", "6300_show_version.txt")) is Platform.AOS_CX
    assert detect_platform("AOS-CX\nVersion : ML.10.08.0001") is Platform.AOS_CX
    assert detect_platform(_read("aos-s", "2930f_show_version.txt")) is Platform.AOS_S
    assert detect_platform("   KB.16.10.0010\n") is Platform.AOS_S
    assert detect_platform("") is None
    assert detect_platform("hello world") is None


def test_facts_commands():
    assert facts_commands("ios") == ("show version",)
    assert facts_commands(PlatformFamily.AOS_CX) == ("show version", "show system")
    assert facts_commands("aos-s") == (
        "show version",
        "show system information",
        "show modules",
    )
    assert facts_commands("ios-xe") == ("show version",)


def test_as_dict():
    d = Facts(Platform.IOS_XE, "C9300-48P", "17.06.04", None, None).as_dict()
    assert d == {
        "model": "C9300-48P",
        "version": "17.06.04",
        "serial": "",
        "uptime": "",
        "platform": "ios-xe",
    }
    assert Facts(None, None, None, None, None).as_dict()["platform"] == ""
