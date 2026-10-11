"""secret() calls: sentinel tokens, the sink rule, masking and in-memory materialization.

Rendering never sees a secret value. secret("<customer>/<name>") returns a sentinel that
carries a random per-render nonce, so neither a template nor a variable can forge one.
After rendering, every sentinel must sit inside a sink span of the target platform's
driver, or rendering fails. Displayed and stored copies swap sentinels for ********;
only apply mode materializes real values, in memory, just before sending.

See design.md, Snippets and rendering: secret().
"""

from __future__ import annotations

import re
import secrets
from collections.abc import Mapping

from tsunami.core.issues import TsunamiError
from tsunami.core.platforms.base import PlatformDriver

MASK = "********"
_REF_RE = re.compile(r"(?P<customer>[a-z0-9][a-z0-9-]*)/(?P<name>[a-z0-9][a-z0-9-]*)")


class SecretRefError(TsunamiError):
    """A secret() call is malformed, crosses customers, or lands outside a sink."""


class SecretCollector:
    """The secret() function for one render: records references, returns sentinels."""

    def __init__(self, customer: str) -> None:
        self.customer = customer
        self.refs: list[str] = []
        nonce = secrets.token_hex(8)
        self._nonce = nonce
        self._prefix = f"\x00SECRET:{nonce}:"
        self._sentinel = re.compile(rf"\x00SECRET:{nonce}:(\d+)\x00")

    def __repr__(self) -> str:
        return f"SecretCollector({self.customer!r}, {len(self.refs)} refs)"

    def __call__(self, ref: object) -> str:
        match = _REF_RE.fullmatch(ref) if isinstance(ref, str) else None
        if match is None:
            raise SecretRefError(f"secret() takes 'customer/name' slugs, got {ref!r}")
        if match["customer"] != self.customer:
            raise SecretRefError(
                f"secret({ref!r}) belongs to another customer; this play is for {self.customer!r}"
            )
        if ref not in self.refs:
            self.refs.append(ref)
        return f"{self._prefix}{self.refs.index(ref)}\x00"

    def contains(self, text: str) -> bool:
        """True if text holds a sentinel, whole or altered (any NUL counts)."""
        return self._prefix in text or self.altered(text)

    def altered(self, text: str) -> bool:
        """A sentinel was changed by a filter or expression (reverse, slicing, tojson...).

        It could never be materialized, and its fragments would be sent to the device.
        """
        rest = self._sentinel.sub("", text)
        # A NUL, or its repr from | list or string(): no config line contains either.
        return any(mark in rest for mark in ("\x00", "\\x00", self._nonce, "SECRET:"))

    def check_sink(self, line: str, driver: PlatformDriver, parent: str | None = None) -> None:
        """Every sentinel on the line must be whole and lie inside a driver sink span."""
        if self.altered(line):
            raise SecretRefError(
                "secret() output was changed by a filter or expression; "
                "use {{ secret('customer/name') }} as a whole value"
            )
        sinks = driver.sink_spans(line, parent)
        for match in self._sentinel.finditer(line):
            start, end = match.span()
            if not any(s <= start and end <= e for s, e in sinks):
                raise SecretRefError(
                    f"secret() is not allowed here for {driver.family}: {self.mask(line).strip()!r}"
                )

    def mask(self, text: str) -> str:
        return self._sentinel.sub(MASK, text)

    def materialize(self, text: str, values: Mapping[str, str]) -> str:
        """Swap sentinels for real values. Apply mode only; never store the result."""
        return self._sentinel.sub(lambda m: values[self.refs[int(m.group(1))]], text)
