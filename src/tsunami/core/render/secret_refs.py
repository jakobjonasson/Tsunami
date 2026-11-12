"""secret() calls: sentinel tokens, the sink rule, masking and in-memory materialization.

Rendering never sees a secret value. secret("<customer>/<name>") returns a sentinel that
carries a random per-render nonce, so neither a template nor a variable can forge one.
After rendering, every sentinel must sit inside a sink span of the target platform's
driver, or rendering fails. Displayed and stored copies swap sentinels for ********;
only apply mode materializes real values, in memory, just before sending.

login(name).password and .enable are sentinels from the same collector, recorded as
`login:<name>/password` and `login:<name>/enable`, under the same rules.

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
_NAME_RE = re.compile(r"[a-z0-9][a-z0-9-]*")
LOGIN_PREFIX = "login:"  # login references; secret() cannot make one, it takes slugs only
PROMPT_PREFIX = "prompt:"  # a password prompt's value, typed for one run
FIELD_SEP = "."  # customer/name.field: one field of a secret with fields


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

    def login_ref(self, name: str, field: str) -> str:
        """The sentinel for a login's password or enable (login() in core/render)."""
        return self._sentinel_for(f"{LOGIN_PREFIX}{name}/{field}")

    def prompt_ref(self, name: str) -> str:
        """The sentinel for a password prompt's value."""
        return self._sentinel_for(f"{PROMPT_PREFIX}{name}")

    def field_ref(self, customer: str, name: str, field: str | None = None) -> str:
        """The sentinel for a secret, or for one field of a secret with fields."""
        ref = f"{customer}/{name}" if field is None else f"{customer}/{name}{FIELD_SEP}{field}"
        return self._sentinel_for(ref)

    def __call__(self, ref: object) -> str:
        return self.field_ref(*self.parse(ref))

    def parse(self, ref: object) -> tuple[str, str]:
        """(customer, name) of a secret('name') or secret('customer/name') argument."""
        if isinstance(ref, str) and _NAME_RE.fullmatch(ref):
            ref = f"{self.customer}/{ref}"  # secret('name'): this play's customer (global plays)
        match = _REF_RE.fullmatch(ref) if isinstance(ref, str) else None
        if match is None:
            raise SecretRefError(f"secret() takes 'name' or 'customer/name' slugs, got {ref!r}")
        if match["customer"] != self.customer:
            raise SecretRefError(
                f"secret({ref!r}) belongs to another customer; this play is for {self.customer!r}"
            )
        return match["customer"], match["name"]

    def _sentinel_for(self, ref: str) -> str:
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
                "secret() output was changed by a filter or expression; use "
                "{{ secret('customer/name') }} or {{ login(name).password }} as a whole value"
            )
        sinks = driver.sink_spans(line, parent)
        for match in self._sentinel.finditer(line):
            start, end = match.span()
            if not any(s <= start and end <= e for s, e in sinks):
                ref = self.refs[int(match.group(1))]
                raise SecretRefError(
                    f"{describe_ref(ref)} is not allowed here for {driver.family} (secret "
                    "values go only on lines the redactor masks): "
                    f"{self.mask(line).strip()!r}"
                )

    def mask(self, text: str) -> str:
        return self._sentinel.sub(MASK, text)

    def mask_spans(self, text: str) -> tuple[str, list[tuple[int, int, str]]]:
        """text masked, and where each mask is: (start, end, reference) in the masked text.
        For the GUI's press-to-reveal; the reference names the secret, never its value."""
        out: list[str] = []
        spans: list[tuple[int, int, str]] = []
        length, last = 0, 0
        for match in self._sentinel.finditer(text):
            out.append(text[last : match.start()])
            length += match.start() - last
            spans.append((length, length + len(MASK), self.refs[int(match.group(1))]))
            out.append(MASK)
            length += len(MASK)
            last = match.end()
        out.append(text[last:])
        return "".join(out), spans

    def materialize(self, text: str, values: Mapping[str, str]) -> str:
        """Swap sentinels for real values. Apply mode only; never store the result."""
        return self._sentinel.sub(lambda m: values[self.refs[int(m.group(1))]], text)


def describe_ref(ref: str) -> str:
    """How an error names a reference, never with its value."""
    if ref.startswith(LOGIN_PREFIX):
        return f"login() {ref.rsplit('/', 1)[1]}"
    if ref.startswith(PROMPT_PREFIX):
        return f"prompt {ref[len(PROMPT_PREFIX) :]!r} (type 'password')"
    _, _, field = split_ref(ref)
    return "secret()" if field is None else f"secret() field {field!r}"


def split_ref(ref: str) -> tuple[str, str, str | None]:
    """Customer, name and field (None for a single value) of a secret reference."""
    customer, name = ref.split("/", 1)
    if FIELD_SEP in name:
        name, field = name.split(FIELD_SEP, 1)
        return customer, name, field
    return customer, name, None
