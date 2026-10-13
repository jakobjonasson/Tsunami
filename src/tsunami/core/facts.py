"""Facts from show output: model, software version, serial, uptime, platform.

See design.md, Execution engine: Device level (step 2) and Platform drivers: Facts.
Aruba layouts come from docs/research/aruba-cli.md section 7; the AOS-S `Chassis:` line on
non-chassis models and the single-switch `show modules` layout are UNVERIFIED there.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

from tsunami.core.models.common import PLATFORM_FAMILY, Platform, PlatformFamily


@dataclass(frozen=True)
class Facts:
    platform: Platform | None = None
    model: str | None = None
    version: str | None = None
    serial: str | None = None
    uptime: str | None = None

    def as_dict(self) -> dict[str, str]:
        """Template view: unknown values are empty strings."""
        return {
            "model": self.model or "",
            "version": self.version or "",
            "serial": self.serial or "",
            "uptime": self.uptime or "",
            "platform": self.platform.value if self.platform else "",
        }


def _family(family: PlatformFamily | str) -> PlatformFamily:
    try:
        return PlatformFamily(family)
    except ValueError:
        return PLATFORM_FAMILY[Platform(family)]


def facts_commands(family: PlatformFamily | str) -> tuple[str, ...]:
    fam = _family(family)
    if fam is PlatformFamily.AOS_CX:
        return ("show version", "show system")
    if fam is PlatformFamily.AOS_S:
        return ("show version", "show system information", "show modules")
    return ("show version",)


_AOS_S_VERSION = r"[A-Z]{1,2}\.\d{2}\.\d{2}\.\d{4}\w*"


def detect_platform(show_version: str) -> Platform | None:
    text = show_version or ""
    if re.search(r"AOS-CX|ArubaOS-CX", text, re.I):
        return Platform.AOS_CX
    # XE is checked before IOS: a 3850/3650 on 16.x prints both banners, IOS-XE 3.x prints
    # "IOS-XE Software" next to a 15.x-style version, and IOL prints "[IOSXE]". The banner
    # is the marker; the version number never decides (3.x reports 15.5(3)S and the like).
    if re.search(r"IOS[ -]?XE", text, re.I):
        return Platform.IOS_XE
    if re.search(r"Cisco IOS Software|Cisco Internetwork Operating System", text):
        return Platform.IOS
    if re.search(r"^Image stamp:", text, re.M) or re.search(
        rf"^\s*{_AOS_S_VERSION}\s*$", text, re.M
    ):
        return Platform.AOS_S
    return None


def _first(pattern: str, text: str, flags: int = re.M) -> str | None:
    m = re.search(pattern, text, flags)
    if not m:
        return None
    value = m.group(1).strip()
    return value or None


def _parse_ios(out: str) -> Facts:
    version = _first(r"Cisco IOS XE Software.*?Version\s+([^\s,]+)", out) or _first(
        r"Cisco IOS Software.*?Version\s+([^\s,]+)", out
    )
    model = _first(r"^Model [Nn]umber\s*:\s*(\S+)", out) or _first(
        r"^cisco\s+(\S+)\s+\(.*processor", out, re.M | re.I
    )
    serial = _first(r"^System [Ss]erial [Nn]umber\s*:\s*(\S+)", out)
    uptime = _first(r"^\S+ uptime is (.+?)\s*$", out)
    return Facts(detect_platform(out), model, version, serial, uptime)


def _parse_cx(outputs: Mapping[str, str]) -> Facts:
    ver = outputs.get("show version", "")
    sysout = outputs.get("show system", "")
    version = _first(r"^(?:ArubaOS|AOS)-CX Version\s*:\s*(\S+)", sysout) or _first(
        r"^Version\s*:\s*(\S+)", ver
    )
    return Facts(
        detect_platform(ver) or detect_platform(sysout),
        _first(r"^Product Name\s*:\s*(.+?)\s*$", sysout),
        version,
        _first(r"^Chassis Serial Nbr\s*:\s*(\S+)", sysout),
        _first(r"^Up Time\s*:\s*(.+?)\s*$", sysout),
    )


def _parse_aos_s(outputs: Mapping[str, str]) -> Facts:
    ver = outputs.get("show version", "")
    info = outputs.get("show system information", "")
    mods = outputs.get("show modules", "")
    version = _first(r"Software revision\s*:\s*(\S+)", info) or _first(
        rf"^\s*({_AOS_S_VERSION})\s*$", ver
    )
    chassis = re.search(r"Chassis:\s*(.+?)\s+(J[A-Z0-9]{5})\s+Serial Number:\s*(\S+)", mods)
    model = f"{chassis.group(1)} {chassis.group(2)}" if chassis else None
    serial = _first(r"Serial Number\s*:\s*(\S+)", info) or (chassis.group(3) if chassis else None)
    # Two-column layout: the uptime is followed by a second column after 2+ spaces.
    uptime = _first(r"Up Time\s*:\s*(.+?)(?:\s{2,}\S.*)?$", info)
    return Facts(detect_platform(ver), model, version, serial, uptime)


def parse_facts(family: PlatformFamily | str, outputs: Mapping[str, str]) -> Facts:
    fam = _family(family)
    if fam is PlatformFamily.AOS_CX:
        return _parse_cx(outputs)
    if fam is PlatformFamily.AOS_S:
        return _parse_aos_s(outputs)
    return _parse_ios(outputs.get("show version", ""))
