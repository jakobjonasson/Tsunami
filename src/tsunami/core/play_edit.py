"""Copy a play to another customer (or from a global play to a customer).

The copy is a new file next to the original with its customer set and its secret()
references pointed at the new customer: secret('old/name') becomes secret('name'),
which resolves to the play's own customer. Comments and layout are kept. Snippets are
not copied: customer-level snippets the play relies on must exist for the new customer,
and the workspace reload reports any that do not.

See design.md, Plays: Global plays.
"""

from __future__ import annotations

import re
from pathlib import Path

from tsunami.core.issues import TsunamiError
from tsunami.core.models.common import require_slug
from tsunami.core.yamlio import RoundTripDoc, YamlLoadError, dump_roundtrip, load_roundtrip

_SECRET_REF = re.compile(r"""secret\(\s*(['"])(?P<customer>[a-z0-9][a-z0-9-]*)/""")


def copy_play(root: Path, play_path: Path | str, customer: str, name: str | None = None) -> Path:
    """Copy a live play for customer. Returns the new workspace-relative path."""
    root = Path(root)
    source = root / Path(play_path)
    if not source.is_file() or Path(play_path).parts[:1] != ("plays",):
        raise TsunamiError(f"no live play at {Path(play_path).as_posix()}")
    try:
        require_slug(customer)
    except TsunamiError:
        raise TsunamiError(f"not a customer name: {customer!r}") from None
    stem = name or f"{source.stem}-{customer}"
    try:
        require_slug(stem)
    except TsunamiError:
        raise TsunamiError(
            f"file name {stem!r} must be lowercase letters, digits, dashes"
        ) from None
    target = source.with_name(f"{stem}.yml")
    if target.exists():
        raise TsunamiError(f"{target.relative_to(root).as_posix()} already exists")
    try:
        doc = load_roundtrip(source)
    except YamlLoadError as exc:
        raise TsunamiError(str(exc)) from None
    if not isinstance(doc.data, dict):
        raise TsunamiError(f"{source.name} is not a play")
    doc.data["customer"] = customer
    dump_roundtrip(
        RoundTripDoc(target, doc.data, doc.mapping_indent, doc.sequence_indent, doc.sequence_offset)
    )
    text = target.read_text(encoding="utf-8")
    rewritten = _SECRET_REF.sub(lambda m: f"secret({m.group(1)}", text)
    if rewritten != text:
        target.write_text(rewritten, encoding="utf-8", newline="\n")
    return target.relative_to(root)
