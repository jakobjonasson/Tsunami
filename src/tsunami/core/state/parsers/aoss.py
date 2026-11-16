"""State table parsers for AOS-S (ArubaOS-Switch).

See docs/research/state-tables.md for the formats.
"""

from __future__ import annotations

from collections.abc import Callable

from tsunami.core.state.tables import Row

COMMANDS: dict[str, str] = {}
PARSERS: dict[str, Callable[[str], list[Row]]] = {}
