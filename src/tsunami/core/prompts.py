"""Prompt type validation and coercion, and placeholder values for validation renders.

See design.md, Play schema: prompts (type table and the control-character rule) and
Testing: Unit tests (prompt validators).
"""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from tsunami.core.models.common import PlatformFamily
from tsunami.core.models.play import PromptType
from tsunami.core.platforms.interfaces import InterfaceNameError, validate_interface_name

__all__ = ["PromptContext", "PromptValueError", "coerce", "placeholder"]


class PromptValueError(ValueError):
    """Shown to people and MCP clients: keep it short and never echo secrets."""


@dataclass(frozen=True)
class PromptContext:
    sites: frozenset[str] = frozenset()
    roles: frozenset[str] = frozenset()
    model_ids: frozenset[str] = frozenset()  # hardware catalog ids
    family: PlatformFamily | None = None  # required for `interface` prompts


# C0 and C1 controls, DEL, and the Unicode line and paragraph separators: str.splitlines()
# treats \x85,   and   as line breaks too.
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f  ]")
_INT_RE = re.compile(r"[+-]?[0-9]+")
_HOSTNAME_RE = re.compile(r"[A-Za-z](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?")
_VLAN_TOKEN_RE = re.compile(r"[0-9]+(?:\s*-\s*[0-9]+)?")
_MAX_LISTED = 10


def _reject_control(value: object) -> None:
    # Runs on the raw value, before any strip, so a trailing newline cannot be trimmed away.
    if isinstance(value, str):
        if _CONTROL_RE.search(value):
            raise PromptValueError("value contains a line break or control character")
    elif isinstance(value, (list, tuple)):
        for item in value:
            _reject_control(item)


def _text(raw: object, *, strip: bool = True) -> str:
    if not isinstance(raw, str):
        raise PromptValueError("expected text")
    return raw.strip() if strip else raw


def _int(raw: object, what: str = "integer") -> int:
    # bool is an int subclass, so it needs an explicit check.
    if isinstance(raw, bool):
        raise PromptValueError(f"expected an {what}, not a boolean")
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str) and _INT_RE.fullmatch(raw.strip()):
        return int(raw.strip())
    raise PromptValueError(f"expected an {what}")


def _vlan_id(value: int) -> int:
    if not 1 <= value <= 4094:
        raise PromptValueError("VLAN id must be 1 to 4094")
    return value


def _string(raw: object, pattern: str | None) -> str:
    text = _text(raw, strip=False)
    if pattern is not None:
        try:
            matched = re.fullmatch(pattern, text) is not None
        except re.error as exc:
            raise PromptValueError("invalid pattern for this prompt") from exc
        if not matched:
            raise PromptValueError("value does not match the required pattern")
    return text


def _int_prompt(raw: object, minimum: int | None, maximum: int | None) -> int:
    value = _int(raw)
    if minimum is not None and value < minimum:
        raise PromptValueError(f"value must be at least {minimum}")
    if maximum is not None and value > maximum:
        raise PromptValueError(f"value must be at most {maximum}")
    return value


def _bool(raw: object) -> bool:
    if isinstance(raw, bool):
        return raw
    text = _text(raw).lower()
    if text == "true":
        return True
    if text == "false":
        return False
    raise PromptValueError("expected true or false")


def _options(options: Sequence[str] | frozenset[str]) -> str:
    items = sorted(options) if isinstance(options, frozenset) else [str(o) for o in options]
    if not items:
        return "(none defined)"
    return ", ".join(items[:_MAX_LISTED]) + (", ..." if len(items) > _MAX_LISTED else "")


def _choice(raw: object, choices: Sequence[str] | None) -> str:
    if choices is None:
        raise PromptValueError("choice prompt has no choices")
    if isinstance(raw, bool) or not isinstance(raw, (str, int)):
        raise PromptValueError("expected one of the listed choices")
    text = raw.strip() if isinstance(raw, str) else str(raw)
    for choice in choices:
        if str(choice) == text:
            return str(choice)
    raise PromptValueError(f"must be one of: {_options(choices)}")


def _hostname(raw: object) -> str:
    text = _text(raw)
    if not _HOSTNAME_RE.fullmatch(text):
        raise PromptValueError(
            "hostname must be 1 to 63 letters, digits or hyphens, start with a letter "
            "and end with a letter or digit"
        )
    return text


def _vlan(raw: object) -> int:
    return _vlan_id(_int(raw, "VLAN id"))


def _vlan_list(raw: object) -> list[int]:
    ids: set[int] = set()
    if isinstance(raw, (list, tuple)):
        for item in raw:
            if isinstance(item, str):
                raise PromptValueError("VLAN list items must be integers")
            ids.add(_vlan_id(_int(item, "VLAN id")))
    else:
        text = _text(raw)
        for token in text.split(","):
            token = token.strip()
            if not _VLAN_TOKEN_RE.fullmatch(token):
                raise PromptValueError("VLAN list must look like 10,20,30-35")
            if "-" in token:
                low_s, high_s = (part.strip() for part in token.split("-"))
                low, high = _vlan_id(int(low_s)), _vlan_id(int(high_s))
                if low > high:
                    raise PromptValueError("VLAN range must be ascending")
                ids.update(range(low, high + 1))
            else:
                ids.add(_vlan_id(int(token)))
    if not ids:
        raise PromptValueError("VLAN list is empty")
    return sorted(ids)


def _interface(raw: object, context: PromptContext) -> str:
    if context.family is None:
        raise PromptValueError("interface prompt needs a platform family")
    try:
        return validate_interface_name(context.family, _text(raw))
    except InterfaceNameError as exc:
        raise PromptValueError(str(exc)) from exc


def _ipv4(raw: object) -> str:
    try:
        return str(ipaddress.IPv4Address(_text(raw)))
    except ValueError as exc:
        raise PromptValueError("expected an IPv4 host address without a prefix") from exc


def _split_prefix(text: str) -> tuple[str, str]:
    addr, sep, mask = text.partition("/")
    if not sep:
        raise PromptValueError("expected an address with a prefix, like 192.0.2.1/24")
    if "." in mask:
        # ipaddress also accepts a wildcard mask here; only a real netmask is valid.
        try:
            netmask = ipaddress.IPv4Network(f"0.0.0.0/{mask}").netmask
        except ValueError as exc:
            raise PromptValueError("invalid netmask") from exc
        if str(netmask) != mask:
            raise PromptValueError("invalid netmask")
    return addr, mask


def _ipv4_interface(raw: object) -> str:
    addr, mask = _split_prefix(_text(raw))
    try:
        iface = ipaddress.IPv4Interface(f"{addr}/{mask}")
    except ValueError as exc:
        raise PromptValueError("expected an IPv4 address with a prefix, like 192.0.2.1/24") from exc
    net = iface.network
    if net.prefixlen <= 30 and iface.ip in (net.network_address, net.broadcast_address):
        raise PromptValueError("address is the network or broadcast address of its subnet")
    return f"{iface.ip}/{net.prefixlen}"


def _ipv4_network(raw: object) -> str:
    addr, mask = _split_prefix(_text(raw))
    try:
        net = ipaddress.IPv4Network(f"{addr}/{mask}", strict=True)
    except ValueError as exc:
        raise PromptValueError(
            "expected a network with host bits clear, like 192.0.2.0/24"
        ) from exc
    return f"{net.network_address}/{net.prefixlen}"


def _member(raw: object, valid: frozenset[str], what: str) -> str:
    text = _text(raw)
    if text not in valid:
        raise PromptValueError(f"unknown {what}; valid: {_options(valid)}")
    return text


def coerce(
    ptype: PromptType,
    raw: object,
    *,
    context: PromptContext,
    choices: Sequence[str] | None = None,
    pattern: str | None = None,
    minimum: int | None = None,
    maximum: int | None = None,
) -> object:
    """Validate a raw value and return it in its rendered form (spec: 'Rendered as')."""
    _reject_control(raw)
    simple: dict[PromptType, Callable[[object], object]] = {
        PromptType.BOOL: _bool,
        PromptType.HOSTNAME: _hostname,
        PromptType.VLAN: _vlan,
        PromptType.VLAN_LIST: _vlan_list,
        PromptType.IPV4: _ipv4,
        PromptType.IPV4_INTERFACE: _ipv4_interface,
        PromptType.IPV4_NETWORK: _ipv4_network,
    }
    if ptype in simple:
        return simple[ptype](raw)
    if ptype == PromptType.STRING:
        return _string(raw, pattern)
    if ptype == PromptType.INT:
        return _int_prompt(raw, minimum, maximum)
    if ptype == PromptType.CHOICE:
        return _choice(raw, choices)
    if ptype == PromptType.INTERFACE:
        return _interface(raw, context)
    if ptype == PromptType.SITE:
        return _member(raw, context.sites, "site")
    if ptype == PromptType.ROLE:
        return _member(raw, context.roles, "role")
    if ptype == PromptType.MODEL:
        return _member(raw, context.model_ids, "model")
    raise PromptValueError(f"unsupported prompt type: {ptype}")


_PLACEHOLDER_INTERFACE = {
    PlatformFamily.IOS: "GigabitEthernet1/0/1",
    PlatformFamily.AOS_CX: "1/1/1",
    PlatformFamily.AOS_S: "1",
}


def _first(options: frozenset[str], what: str) -> str:
    if not options:
        raise PromptValueError(f"no {what} defined")
    return min(options)


def placeholder(
    ptype: PromptType,
    *,
    context: PromptContext,
    choices: Sequence[str] | None = None,
    minimum: int | None = None,
    maximum: int | None = None,
) -> object:
    """A type-appropriate value used for validation renders when no real value exists yet."""
    if ptype == PromptType.STRING:
        return "placeholder"
    if ptype == PromptType.INT:
        if minimum is not None:
            return minimum
        return maximum if maximum is not None and maximum < 0 else 0
    if ptype == PromptType.BOOL:
        return False
    if ptype == PromptType.CHOICE:
        if not choices:
            raise PromptValueError("choice prompt has no choices")
        return choices[0]
    if ptype == PromptType.HOSTNAME:
        return "placeholder-host"
    if ptype == PromptType.VLAN:
        return 1
    if ptype == PromptType.VLAN_LIST:
        return [1]
    if ptype == PromptType.INTERFACE:
        if context.family is None:
            raise PromptValueError("interface prompt needs a platform family")
        return _PLACEHOLDER_INTERFACE[context.family]
    if ptype == PromptType.IPV4:
        return "192.0.2.1"
    if ptype == PromptType.IPV4_INTERFACE:
        return "192.0.2.1/24"
    if ptype == PromptType.IPV4_NETWORK:
        return "192.0.2.0/24"
    if ptype == PromptType.SITE:
        return _first(context.sites, "sites")
    if ptype == PromptType.ROLE:
        return _first(context.roles, "roles")
    if ptype == PromptType.MODEL:
        return _first(context.model_ids, "models")
    raise PromptValueError(f"unsupported prompt type: {ptype}")
