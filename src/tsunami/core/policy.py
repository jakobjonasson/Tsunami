"""MCP policy loading (re-read on every call) and the show-allowlist matcher.

See design.md, MCP interface: Policy file, show allowlist matching.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq

from tsunami.core.issues import TsunamiError
from tsunami.core.models.common import PlatformFamily
from tsunami.core.models.policy import FORBIDDEN_PIPES, McpPolicy, has_control_chars
from tsunami.core.yamlio import YamlLoadError, load_yaml


class PolicyError(TsunamiError):
    """The policy file is invalid, or a command is not allowed by it."""


def load_policy(path: Path) -> McpPolicy:
    """The policy at path. A missing file means MCP is disabled (the defaults)."""
    path = Path(path)
    if not path.is_file():
        return McpPolicy()
    try:
        doc = load_yaml(path)
    except YamlLoadError as exc:
        raise PolicyError(f"{path.name} is not valid YAML: {exc}") from None
    except OSError as exc:
        raise PolicyError(f"{path.name} cannot be read ({type(exc).__name__})") from None
    data = {} if doc.data is None else doc.data
    if not isinstance(data, dict):
        raise PolicyError(f"{path.name} must be a mapping")
    try:
        return McpPolicy.model_validate(data)
    except ValidationError as exc:
        problems = []
        for err in exc.errors(include_url=False, include_input=False, include_context=False):
            where = ".".join(str(p) for p in err["loc"])
            problems.append(f"{where}: {err['msg'].removeprefix('Value error, ')}")
        raise PolicyError(f"{path.name} is invalid: " + "; ".join(problems)) from None


def _merge(existing: Any, new: Any) -> Any:
    """Update a round-trip node in place from plain data, keeping its comments."""
    if isinstance(new, dict) and isinstance(existing, CommentedMap):
        for key in [k for k in existing if k not in new]:
            del existing[key]
        for key, value in new.items():
            existing[key] = _merge(existing[key], value) if key in existing else value
        return existing
    if isinstance(new, list) and isinstance(existing, CommentedSeq):
        existing[:] = new
        return existing
    return new


def save_policy(policy: McpPolicy, path: Path) -> None:
    """Write the policy atomically. An existing file keeps its comments and layout."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    plain = policy.model_dump(mode="json")
    yaml = YAML(typ="rt")
    yaml.width = 4096
    yaml.indent(mapping=2, sequence=4, offset=2)
    data: Any = plain
    if path.is_file():
        try:
            loaded = yaml.load(path.read_text(encoding="utf-8-sig"))
        except Exception:  # noqa: BLE001 - unreadable old file: write a fresh one
            loaded = None
        if isinstance(loaded, CommentedMap):
            data = _merge(loaded, plain)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            yaml.dump(data, handle)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _tokens_start_with(tokens: list[str], entry: list[str]) -> bool:
    return len(tokens) >= len(entry) and [t.lower() for t in tokens[: len(entry)]] == entry


def check_show_command(policy: McpPolicy, family: PlatformFamily | str, command: str) -> str:
    """The normalized command if the policy allows it, else PolicyError."""
    try:
        fam = PlatformFamily(family)
    except ValueError:
        raise PolicyError(f"unknown platform family: {family!s}") from None
    if ";" in command or has_control_chars(command):
        raise PolicyError("command contains a forbidden character (';', line break or control)")
    parts = [p.split() for p in command.split("|")]
    if len(parts) > 2:
        raise PolicyError("at most one pipe is allowed")
    base = parts[0]
    if not base:
        raise PolicyError("empty command")
    allowed = [e.lower().split() for e in policy.show_allowlist.get(fam, [])]
    if not any(_tokens_start_with(base, entry) for entry in allowed):
        raise PolicyError(f"command is not on the {fam.value} show allowlist")
    normalized = " ".join(base)
    if len(parts) == 2:
        modifier = parts[1]
        if not modifier:
            raise PolicyError("a pipe needs a modifier")
        word = modifier[0].lower()
        permitted = {p.lower() for p in policy.pipes.get(fam, [])} - FORBIDDEN_PIPES
        if word not in permitted:
            raise PolicyError(f"pipe modifier '{modifier[0]}' is not allowed for {fam.value}")
        normalized += " | " + " ".join(modifier)
    return normalized


def validate_allowlist_request(
    family: PlatformFamily | str, command: str
) -> tuple[PlatformFamily, str]:
    """The family and normalized command of an allowlist request, else PolicyError.

    Only plain `show` commands can be requested: no pipe, ';' or control character, no
    abbreviation. Everything else is refused outright, whatever the reason given.
    """
    try:
        fam = PlatformFamily(family)
    except ValueError:
        raise PolicyError(f"unknown platform family: {family!s}") from None
    if not isinstance(command, str) or not command.strip():
        raise PolicyError("empty command")
    if "|" in command or ";" in command or has_control_chars(command):
        raise PolicyError("a request may not contain '|', ';', a line break or a control character")
    normalized = " ".join(command.split())
    tokens = normalized.split(" ")
    if tokens[0].lower() != "show" or len(tokens) < 2:
        raise PolicyError("only 'show' commands can be added to the allowlist")
    return fam, normalized.lower()


def add_allowlist_entry(path: Path, family: PlatformFamily | str, command: str) -> bool:
    """Add a show command to the policy file (a person approved it). False if present.

    Only the GUI calls this; the MCP server has no path to it.
    """
    fam, normalized = validate_allowlist_request(family, command)
    policy = load_policy(path)
    entries = list(policy.show_allowlist.get(fam, []))
    if normalized in (e.lower() for e in entries):
        return False
    entries.append(normalized)
    policy.show_allowlist[fam] = entries
    save_policy(McpPolicy.model_validate(policy.model_dump()), path)
    return True
