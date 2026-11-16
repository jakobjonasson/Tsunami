"""Evaluate a state check against a table's rows, and against the rows read before the
change when the check compares.

A check selects rows with `where`, then asserts on the selection: `expect` (every
selected row has these values, and at least one row is selected), `count` (how many rows
are selected), `compare` (the selection against the same selection before the change).

Matchers, for `where` and `expect` values:
  full            the value, compared as text without case (numbers as numbers)
  /^Te1\\/1\\//     a regular expression searched in the value, without case
  [full, 2way]    any of these
  {min: 1}        a number at least and/or at most (min, max)

Results name rows by their key columns. The text can hold device-written words (a
neighbor's name, a VLAN's name): the caller redacts it before logging or storing.

See design.md, State checks.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from tsunami.core.state.tables import INT, Row, StateTable

COMPARE_COUNT = "count"
COMPARE_NO_LOSS = "no_loss"
COMPARE_SAME = "same"
COMPARE_MODES = (COMPARE_COUNT, COMPARE_NO_LOSS, COMPARE_SAME)
_SHOWN = 5  # rows named in one failure message


@dataclass(frozen=True)
class StateAssertion:
    """The state part of a rendered check."""

    table: str
    where: Mapping[str, Any]
    expect: Mapping[str, Any]
    count: tuple[int | None, int | None] | None = None  # (min, max)
    compare: str | None = None


@dataclass(frozen=True)
class StateResult:
    ok: bool
    detail: str  # why it failed, or a short summary when it passed; redact before use


def is_regex(matcher: Any) -> bool:
    return isinstance(matcher, str) and len(matcher) >= 2 and matcher[0] == matcher[-1] == "/"


def matches(value: Any, matcher: Any, kind: str = "text") -> bool:
    """Whether one cell satisfies one matcher."""
    if isinstance(matcher, list | tuple):
        return any(matches(value, m, kind) for m in matcher)
    if isinstance(matcher, Mapping):
        try:
            number = int(value)
        except (TypeError, ValueError):
            return False
        low, high = matcher.get("min"), matcher.get("max")
        return (low is None or number >= low) and (high is None or number <= high)
    if is_regex(matcher):
        return re.search(matcher[1:-1], str(value), re.IGNORECASE) is not None
    if kind == INT:
        try:
            return int(value) == int(matcher)
        except (TypeError, ValueError):
            return False
    return str(value).strip().lower() == str(matcher).strip().lower()


def select(spec: StateTable, rows: Sequence[Row], where: Mapping[str, Any]) -> list[Row]:
    return [
        row
        for row in rows
        if all(matches(row.get(name), m, _kind(spec, name)) for name, m in where.items())
    ]


def _kind(spec: StateTable, name: str) -> str:
    column = spec.column(name)
    return column.kind if column else "text"


def _matcher_text(matcher: Any) -> str:
    if isinstance(matcher, list | tuple):
        return " or ".join(_matcher_text(m) for m in matcher)
    if isinstance(matcher, Mapping):
        parts = [f"{k} {matcher[k]}" for k in ("min", "max") if matcher.get(k) is not None]
        return ", ".join(parts)
    return str(matcher)


def _named(spec: StateTable, rows: Sequence[Row], extra: Sequence[str] = ()) -> str:
    shown = []
    for row in rows[:_SHOWN]:
        more = " ".join(f"{name}={row.get(name)}" for name in extra if name not in spec.key)
        shown.append(f"{spec.label(row)}" + (f" ({more})" if more else ""))
    if len(rows) > _SHOWN:
        shown.append(f"and {len(rows) - _SHOWN} more")
    return "; ".join(shown)


def _plural(n: int) -> str:
    return f"{n} row" + ("" if n == 1 else "s")


def evaluate(
    spec: StateTable,
    check: StateAssertion,
    rows: Sequence[Row],
    before: Sequence[Row] | None = None,
) -> StateResult:
    """Run one check. `before` is the table as read before the change (compare only)."""
    chosen = select(spec, rows, check.where)
    scope = f"{spec.name}" + (f" where {_where_text(check.where)}" if check.where else "")
    done: list[str] = []

    if check.expect:
        if not chosen and check.count is None:
            return StateResult(False, f"{scope}: no rows, so nothing to check")
        wrong = [
            row
            for row in chosen
            if not all(
                matches(row.get(name), m, _kind(spec, name)) for name, m in check.expect.items()
            )
        ]
        if wrong:
            want = ", ".join(f"{k} {_matcher_text(v)}" for k, v in check.expect.items())
            return StateResult(
                False,
                f"{scope}: {len(wrong)} of {_plural(len(chosen))} not {want}: "
                + _named(spec, wrong, tuple(check.expect)),
            )
        done.append(f"{_plural(len(chosen))} as expected")

    if check.count is not None:
        low, high = check.count
        n = len(chosen)
        if (low is not None and n < low) or (high is not None and n > high):
            if low == high:
                want = str(low)
            else:
                bounds = [("at least", low), ("at most", high)]
                want = " and ".join(f"{word} {v}" for word, v in bounds if v is not None)
            return StateResult(False, f"{scope}: {_plural(n)}, expected {want}")
        done.append(_plural(n))

    if check.compare is not None:
        if before is None:
            return StateResult(False, f"{scope}: no reading from before the change to compare")
        result = _compare(spec, check.compare, scope, select(spec, before, check.where), chosen)
        if not result.ok:
            return result
        done.append(result.detail)

    return StateResult(True, f"{scope}: " + ", ".join(done))


def _compare(
    spec: StateTable, mode: str, scope: str, before: list[Row], after: list[Row]
) -> StateResult:
    if mode == COMPARE_COUNT:
        if len(before) != len(after):
            return StateResult(
                False, f"{scope}: {_plural(len(after))} now, {len(before)} before the change"
            )
        return StateResult(True, "as many rows as before")

    now = {spec.key_of(row): row for row in after}
    was = {spec.key_of(row): row for row in before}
    lost = [row for key, row in was.items() if key not in now]
    if lost:
        return StateResult(
            False,
            f"{scope}: {_plural(len(lost))} gone since before the change: "
            + _named(spec, lost, spec.compared),
        )
    changed = []
    for key, old in was.items():
        new = now[key]
        diffs = [
            f"{name} {old.get(name)} -> {new.get(name)}"
            for name in spec.compared
            if _differs(old.get(name), new.get(name))
        ]
        if diffs:
            changed.append(f"{spec.label(new)} ({', '.join(diffs)})")
    if changed:
        text = "; ".join(changed[:_SHOWN])
        if len(changed) > _SHOWN:
            text += f"; and {len(changed) - _SHOWN} more"
        return StateResult(
            False, f"{scope}: {_plural(len(changed))} changed since before the change: {text}"
        )
    if mode == COMPARE_SAME:
        added = [row for key, row in now.items() if key not in was]
        if added:
            return StateResult(
                False,
                f"{scope}: {_plural(len(added))} new since before the change: "
                + _named(spec, added, spec.compared),
            )
        return StateResult(True, "the same rows as before")
    extra = len(now) - len(was)
    added = f", {extra} new" if extra else ""
    return StateResult(True, f"all {_plural(len(was))} from before still there{added}")


def _differs(old: Any, new: Any) -> bool:
    if isinstance(old, str) and isinstance(new, str):
        return old.lower() != new.lower()
    return old != new


def _where_text(where: Mapping[str, Any]) -> str:
    return ", ".join(f"{k} {_matcher_text(v)}" for k, v in where.items())
