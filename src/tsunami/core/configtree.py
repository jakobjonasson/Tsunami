"""Parse running or rendered config into a tree built from indentation structure.

A line's parent is the nearest less-indented line above it, whatever the indent width
(IOS one space, AOS-CX four, AOS-S three). On AOS-S an `exit` line closes the block at its
indentation and is dropped, so snippets without `exit` parse to the same tree.

See design.md, Execution engine: Diff and compliance; Platform drivers: Running config
and authoring.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass

from tsunami.core.models.common import PlatformFamily
from tsunami.core.platforms import get_driver

_PREFIX_DROP: dict[PlatformFamily, tuple[str, ...]] = {
    PlatformFamily.IOS: ("building configuration", "current configuration"),
    PlatformFamily.AOS_CX: ("current configuration",),
    PlatformFamily.AOS_S: ("running configuration",),
}
_IOS_VERSION = re.compile(r"version\s+\S+$", re.IGNORECASE)
# A trailing encoding word that the device inserts but the template may omit.
_ENCODING_TAIL = re.compile(r"\s+(?:plaintext|ciphertext|\d{1,2})$")


@dataclass(frozen=True)
class ConfigNode:
    text: str
    children: tuple[ConfigNode, ...]
    line: int


class _Builder:
    def __init__(self, text: str, line: int) -> None:
        self.text = text
        self.line = line
        self.children: list[_Builder] = []

    def freeze(self) -> ConfigNode:
        return ConfigNode(self.text, tuple(c.freeze() for c in self.children), self.line)


def is_header_line(text: str, family: PlatformFamily) -> bool:
    """A line the device prints above its config (`Building configuration...`, the byte
    count), which is not config."""
    return text.lower().startswith(_PREFIX_DROP[family])


def _is_noise(text: str, family: PlatformFamily, indent: int) -> bool:
    low = text.lower()
    if is_header_line(low, family):
        return True
    if family is PlatformFamily.IOS and indent == 0:
        return low == "end" or bool(_IOS_VERSION.match(low))
    return False


def parse_config(text: str | Sequence[str], family: PlatformFamily | str) -> tuple[ConfigNode, ...]:
    family = PlatformFamily(str(family)) if not isinstance(family, PlatformFamily) else family
    driver = get_driver(family)
    lines = re.split(r"\r?\n", text) if isinstance(text, str) else list(text)
    root = _Builder("", 0)
    stack: list[tuple[int, _Builder]] = [(-1, root)]
    for number, raw in enumerate(lines, start=1):
        raw = raw.rstrip()
        stripped = raw.strip()
        if not stripped or driver.is_comment(raw):
            continue
        indent = len(raw.expandtabs()) - len(raw.expandtabs().lstrip())
        collapsed = " ".join(stripped.split())
        if _is_noise(collapsed, family, indent):
            continue
        if family is PlatformFamily.AOS_S and collapsed == "exit":
            while len(stack) > 1 and stack[-1][0] >= indent:
                stack.pop()
            continue
        while stack[-1][0] >= indent:
            stack.pop()
        node = _Builder(collapsed, number)
        stack[-1][1].children.append(node)
        stack.append((indent, node))
    return tuple(c.freeze() for c in root.children)


def iter_paths(nodes: Sequence[ConfigNode]) -> Iterator[tuple[str, ...]]:
    def walk(items: Sequence[ConfigNode], prefix: tuple[str, ...]) -> Iterator[tuple[str, ...]]:
        for node in items:
            path = (*prefix, node.text)
            yield path
            yield from walk(node.children, path)

    return walk(nodes, ())


def secret_key(text: str, family: PlatformFamily | str, parent: str | None = None) -> str:
    """The part of a secret-bearing line before its first secret, else the text unchanged.

    A trailing encoding word (`plaintext`, `ciphertext`, an IOS type digit) is dropped
    from the key: the template and the device disagree on it.
    """
    # Sub-mode rules anchor on leading whitespace, which tree text no longer has.
    probe = (" " if parent else "") + text
    spans = [s for r in get_driver(family).secret_rules for s in r.secret_spans(probe, parent)]
    if not spans:
        return text
    key = probe[: min(start for start, _ in spans)].strip()
    return _ENCODING_TAIL.sub("", key)
