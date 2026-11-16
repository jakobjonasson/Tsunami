"""Operational state as tables, for pre-checks and post-checks that assert on what the
device is doing rather than on the text a command prints.

    read_table(session, family, "ospf_neighbors")  ->  rows
    evaluate(table("ospf_neighbors"), assertion, rows, before)  ->  StateResult

See design.md, State checks; tables.py for the tables and evaluate.py for the checks.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from tsunami.core.models.common import PLATFORM_FAMILY, Platform, PlatformFamily
from tsunami.core.state.evaluate import (
    COMPARE_MODES,
    StateAssertion,
    StateResult,
    evaluate,
    is_regex,
    matches,
    select,
)
from tsunami.core.state.tables import TABLES, Row, StateError, StateTable, clean_rows, table

__all__ = [
    "COMPARE_MODES",
    "TABLES",
    "Row",
    "StateAssertion",
    "StateError",
    "StateResult",
    "StateTable",
    "command_for",
    "evaluate",
    "is_regex",
    "matches",
    "parse_table",
    "read_table",
    "select",
    "supported_tables",
    "table",
    "unknown_command",
]

Parser = Callable[[str], list[Row]]

# The device does not have the command at all (an L2 switch asked for OSPF neighbors).
# Any other complaint (`% BGP not active`, `% LLDP is not enabled`) means the feature is
# off, which is an empty table, not a table that cannot be read.
_UNKNOWN_COMMAND = {
    PlatformFamily.IOS: re.compile(
        r"^\s*% ?(?:Invalid input detected|Incomplete command|Ambiguous command|Unknown command)",
        re.M,
    ),
    PlatformFamily.AOS_CX: re.compile(
        r"^\s*(?:Invalid input:|% ?(?:Unknown|Ambiguous|Incomplete) command|% Command incomplete)",
        re.M,
    ),
    PlatformFamily.AOS_S: re.compile(
        r"^\s*(?:Invalid input:|Incomplete input:|Ambiguous input:)", re.M
    ),
}


def _family(family: PlatformFamily | Platform | str) -> PlatformFamily:
    try:
        return PlatformFamily(family)
    except ValueError:
        return PLATFORM_FAMILY[Platform(family)]


def _module(family: PlatformFamily) -> Any:
    # Imported on use: the parsers are only needed when a play has state checks.
    if family is PlatformFamily.IOS:
        from tsunami.core.state.parsers import ios as module
    elif family is PlatformFamily.AOS_CX:
        from tsunami.core.state.parsers import aoscx as module
    else:
        from tsunami.core.state.parsers import aoss as module
    return module


def supported_tables(family: PlatformFamily | Platform | str) -> tuple[str, ...]:
    """The tables this family can read, in the order tables.py lists them."""
    module = _module(_family(family))
    return tuple(name for name in TABLES if name in module.COMMANDS and name in module.PARSERS)


def command_for(family: PlatformFamily | Platform | str, name: str) -> str:
    """The show command that fills a table on this family."""
    fam = _family(family)
    table(name)  # an unknown name reads as that, not as unsupported
    command = _module(fam).COMMANDS.get(name)
    if command is None or name not in _module(fam).PARSERS:
        raise StateError(f"state table {name!r} is not available on {fam}")
    return command


def unknown_command(family: PlatformFamily | Platform | str, output: str) -> str | None:
    """The device's complaint when it does not know the command, else None."""
    match = _UNKNOWN_COMMAND[_family(family)].search(output or "")
    if match is None:
        return None
    start = output.rfind("\n", 0, match.start()) + 1
    end = output.find("\n", match.start())
    return output[start : end if end != -1 else len(output)].strip()


def parse_table(family: PlatformFamily | Platform | str, name: str, output: str) -> list[Row]:
    """Rows from one command's output. Raises StateError when the device rejected the
    command or the parser's rows are not usable."""
    fam = _family(family)
    command = command_for(fam, name)
    text = (output or "").replace("\r\n", "\n").replace("\r", "")
    complaint = unknown_command(fam, text)
    if complaint is not None:
        raise StateError(f"{name}: the device rejected `{command}`: {complaint}")
    try:
        rows = _module(fam).PARSERS[name](text)
    except StateError:
        raise
    except Exception as exc:  # noqa: BLE001 - a parser fault fails the check, not the run
        raise StateError(f"{name}: cannot parse `{command}` ({type(exc).__name__})") from None
    return clean_rows(table(name), rows)


def read_table(session: Any, family: PlatformFamily | Platform | str, name: str) -> list[Row]:
    """Run the table's command on an open session and parse it."""
    command = command_for(family, name)
    try:
        output = session.send_exec(command).output
    except Exception as exc:  # noqa: BLE001 - the type only: the text may hold the command
        raise StateError(f"{name}: `{command}` failed ({type(exc).__name__})") from None
    return parse_table(family, name, output)
