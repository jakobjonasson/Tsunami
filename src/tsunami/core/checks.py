"""Pre-checks and post-checks: exec with expect/expect_not, and reachable.

See design.md, Play schema: checks, and Device level steps 4 and 8.
"""

from __future__ import annotations

import re
import socket
import time
from collections.abc import Callable
from dataclasses import dataclass

from tsunami.core.render.renderer import RenderedCheck
from tsunami.core.transports.base import Session


@dataclass(frozen=True)
class CheckResult:
    ok: bool
    name: str
    detail: str  # why it failed; device text, so the caller redacts before storing
    attempts: int
    output: str  # last raw output; the caller redacts


def _tcp_connect(host: str, port: int, timeout: float) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _label(check: RenderedCheck) -> str:
    if check.name:
        return check.name
    what = check.exec if check.exec else "reachable"
    return f"{check.phase} {check.index + 1}: {what}"


def _attempt(
    check: RenderedCheck,
    session: Session | None,
    host: str,
    port: int,
    connect_timeout: float,
    tcp_connect: Callable[[str, int, float], bool],
) -> tuple[bool, str, str]:
    """One try: (ok, detail, output)."""
    if check.reachable:
        try:
            ok = tcp_connect(host, port, connect_timeout)
        except Exception as exc:
            return False, f"{type(exc).__name__} during connect", ""
        return ok, ("" if ok else f"{host}:{port} not reachable"), ""
    if not check.exec:
        return False, "check has neither exec nor reachable", ""
    if session is None:
        return False, "no session available for exec check", ""
    try:
        output = session.send_exec(check.exec).output
    except Exception as exc:
        return False, f"{type(exc).__name__} running {check.exec!r}", ""
    try:
        if check.expect is not None and not re.search(check.expect, output, re.M):
            return False, f"expected pattern not found: {check.expect}", output
        if check.expect_not is not None and re.search(check.expect_not, output, re.M):
            return False, f"unexpected pattern found: {check.expect_not}", output
    except re.error as exc:
        return False, f"invalid regex: {exc}", output
    return True, "", output


def run_check(
    check: RenderedCheck,
    session: Session | None,
    *,
    host: str,
    port: int,
    connect_timeout: float,
    sleep: Callable[[float], None] = time.sleep,
    tcp_connect: Callable[[str, int, float], bool] = _tcp_connect,
) -> CheckResult:
    """Run a check, retrying up to `retries` extra times with `delay` between tries."""
    total = max(0, check.retries) + 1
    detail, output = "", ""
    for attempt in range(1, total + 1):
        ok, detail, output = _attempt(check, session, host, port, connect_timeout, tcp_connect)
        if ok:
            return CheckResult(True, _label(check), "", attempt, output)
        if attempt < total:
            sleep(check.delay)
    return CheckResult(False, _label(check), detail, total, output)
