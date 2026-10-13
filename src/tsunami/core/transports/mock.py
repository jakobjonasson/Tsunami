"""Fake transport with configurable latency and failure rate, for scale tests.

See design.md, Testing: Scale, and Execution engine: Device level (apply mode) and
Transports. Implements the `Session` protocol from `base.py` against in-memory
simulated devices, so batching, canary, max-fail and rollback are exercised without
real nodes, including a 200-device run under the 50-session cap.

Config-apply semantics (deterministic, deliberately simple):
- A line with no leading whitespace is top-level. It is appended to `running` if not
  already present, and becomes the parent for the indented lines that follow it in the
  same call. `exit` clears the parent; `end` is ignored.
- An indented line goes under the current parent, after the parent's existing
  children (the contiguous indented lines following it), unless an identical child
  (compared after stripping) already exists. With no parent it is appended at the end.
- `no <cmd>` removes the exact matching line (top-level, or a child of the current
  parent when indented). Removing a top-level line also removes its children. A
  missing target is a silent no-op.
- A line matching a `reject_lines` regex (re.search on the stripped line) stops the
  call there. Earlier lines stay applied; the rejected line is not.
"""

from __future__ import annotations

import random
import re
import threading
import time
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass, field

from tsunami.core.transports.base import (
    ConfigResult,
    ConnectError,
    ConnectTarget,
    ExecResult,
    UnreachableError,
)

_INVALID = "% Invalid input detected at '^' marker."


class MockError(Exception):
    """Simulated connection failure."""


class MockUnreachable(MockError):
    """connect() to an unreachable device."""


@dataclass
class MockDeviceState:
    """One simulated device. Mutable; shared across sessions to the same device."""

    name: str
    running: list[str] = field(default_factory=list)  # IOS-style indentation
    startup: list[str] = field(default_factory=list)
    reject_lines: list[str] = field(default_factory=list)
    exec_responses: dict[str, str] = field(default_factory=dict)  # "re:" prefix = regex
    flash: dict[str, list[str]] = field(default_factory=dict)
    # IOS default file system and its free bytes, for `show file systems` and the
    # rollback copy; a copy to any other prefix fails as on a real device.
    filesystem: str = "flash:"
    fs_free: int | None = 500_000_000
    checkpoints: dict[str, list[str]] = field(default_factory=dict)
    sent: list[tuple[str, str]] = field(default_factory=list)  # ("config"|"exec", text)
    # A session raises MockError on its (N+1)th send (config or exec); counted per session.
    drop_after_sends: int | None = None
    fail_connect_times: int = 0  # the next N connects fail
    lock: threading.RLock = field(default_factory=threading.RLock, repr=False, compare=False)


def _children_end(lines: list[str], idx: int) -> int:
    """Index just past the contiguous indented children of lines[idx]."""
    end = idx + 1
    while end < len(lines) and lines[end][:1].isspace():
        end += 1
    return end


def _apply_line(running: list[str], line: str, parent: str | None) -> None:
    indented = line[:1].isspace()
    text = line.strip()
    negate = text.startswith("no ")
    target = text[3:].strip() if negate else text
    if not indented:
        if negate:
            if target in running:
                i = running.index(target)
                del running[i : _children_end(running, i)]
        elif text not in running:
            running.append(text)
        return
    if parent is None or parent not in running:
        if not negate:
            running.append(line)
        return
    pi = running.index(parent)
    end = _children_end(running, pi)
    for i in range(pi + 1, end):
        if running[i].strip() == target:
            if negate:
                del running[i]
            return
    if not negate:
        running.insert(end, line)


class MockSession:
    """Satisfies `Session`. One thread at a time per session."""

    def __init__(self, transport: MockTransport, state: MockDeviceState) -> None:
        self._transport = transport
        self._state = state
        self._closed = False
        self._sends = 0

    def _enter(self, *, send: bool) -> MockDeviceState:
        if self._closed:
            raise MockError("session closed")
        self._transport._delay()
        st = self._state
        if send:
            limit = st.drop_after_sends
            if limit is not None and self._sends >= limit:
                self.close()
                raise MockError("connection lost")
            self._sends += 1
        return st

    def send_config(
        self, lines: list[str], *, config_mode_command: str | None = None
    ) -> ConfigResult:
        st = self._enter(send=True)
        with st.lock:
            st.sent.append(("config", "\n".join(lines)))
            patterns = [re.compile(p) for p in st.reject_lines]
            parent: str | None = None
            for line in lines:
                text = line.strip()
                if any(p.search(text) for p in patterns):
                    return ConfigResult(output=f"{text}\n{_INVALID}", failed_line=line)
                if text == "end":
                    continue
                if text == "exit":
                    parent = None
                    continue
                _apply_line(st.running, line, parent)
                if not line[:1].isspace() and not text.startswith("no "):
                    parent = text
        return ConfigResult(output="")

    def send_exec(
        self,
        command: str,
        *,
        expect_prompt: str | None = None,
        answer: str | None = None,
        read_timeout: float | None = None,
    ) -> ExecResult:
        st = self._enter(send=True)
        with st.lock:
            st.sent.append(("exec", command))
            return ExecResult(command=command, output=self._exec(st, command.strip()))

    def _exec(self, st: MockDeviceState, cmd: str) -> str:
        if cmd == "show file systems":
            free = "-" if st.fs_free is None else str(st.fs_free)
            return (
                "File Systems:\n\n     Size(b)     Free(b)      Type  Flags  Prefixes\n"
                f"*  1000000000  {free}  disk  rw   {st.filesystem}\n"
                "           -           -    opaque     rw   system:\n"
                f"{st.name}#"
            )
        if m := re.fullmatch(r"copy running-config ([\w-]+:)(\S+)", cmd):
            if m[1] != st.filesystem:
                return f"%Error opening {m[1]}{m[2]} (Invalid argument)\n{st.name}#"
            st.flash[m[2]] = list(st.running)
            return f"Destination filename [{m[2]}]\n{len(st.running)} lines copied"
        if m := re.fullmatch(r"configure replace ([\w-]+:)(\S+) force", cmd):
            if m[1] != st.filesystem or m[2] not in st.flash:
                return f"% Error: {m[1]}{m[2]}: no such file"
            st.running[:] = st.flash[m[2]]
            return "The rollback configlet from the last pass is listed below:"
        if cmd == "write memory":
            st.startup[:] = st.running
            return "Building configuration...\n[OK]"
        if m := re.fullmatch(r"copy running-config checkpoint (\S+)", cmd):
            st.checkpoints[m[1]] = list(st.running)
            return ""
        if m := re.fullmatch(r"checkpoint rollback (\S+)", cmd):
            if m[1] not in st.checkpoints:
                return f"Checkpoint {m[1]} does not exist"
            st.running[:] = st.checkpoints[m[1]]
            return ""
        if m := re.fullmatch(r"erase checkpoint (\S+)", cmd):
            if st.checkpoints.pop(m[1], None) is None:
                return f"Checkpoint {m[1]} does not exist"
            return ""
        if m := re.fullmatch(r"cfg-backup running-config config (\S+)", cmd):
            st.flash[m[1]] = list(st.running)
            return ""
        if m := re.fullmatch(r"cfg-restore flash (\S+)", cmd):
            if m[1] not in st.flash:
                return f"Configuration '{m[1]}' does not exist"
            st.running[:] = st.flash[m[1]]
            return f"Successfully applied configuration '{m[1]}' to running configuration."
        if cmd == "show cfg-restore status":
            return "Status : Success"
        if m := re.fullmatch(r"delete (?:/force )?([\w-]+:)(\S+)", cmd):
            if m[1] != st.filesystem or st.flash.pop(m[2], None) is None:
                return f"%Error deleting {m[1]}{m[2]} (No such file or directory)"
            return ""
        if m := re.fullmatch(r"erase config (\S+)", cmd):
            if st.flash.pop(m[1], None) is None:
                return f"% Error deleting {m[1]} (No such file or directory)"
            return ""
        if cmd in st.exec_responses:
            return st.exec_responses[cmd]
        for key, out in st.exec_responses.items():
            if key.startswith("re:") and re.search(key[3:], cmd):
                return out
        return ""

    def running_config(self) -> str:
        st = self._enter(send=False)
        with st.lock:
            return "\n".join([*st.running, "end"])

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._transport._release()


class MockTransport:
    """In-memory device farm. Thread-safe; `connect` returns one `MockSession`."""

    def __init__(
        self,
        *,
        latency: tuple[float, float] = (0.0, 0.0),
        failure_rate: float = 0.0,
        unreachable: Collection[str] = (),
        seed: int | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._latency = latency
        self._failure_rate = failure_rate
        self._unreachable = set(unreachable)
        self._sleep = sleep
        self._rng = random.Random(seed)
        self._rng_lock = threading.Lock()
        self._lock = threading.Lock()
        self._devices: dict[str, MockDeviceState] = {}
        self._active = 0
        self._max_active = 0
        self._connections = 0

    def add_device(
        self, name: str, running: Sequence[str] = (), **state_fields: object
    ) -> MockDeviceState:
        state = MockDeviceState(name=name, running=list(running), **state_fields)  # type: ignore[arg-type]
        with self._lock:
            self._devices[name] = state
        return state

    def device(self, name: str) -> MockDeviceState:
        with self._lock:
            return self._devices[name]

    def _delay(self) -> None:
        with self._rng_lock:
            d = self._rng.uniform(*self._latency)
        self._sleep(d)

    def connect(self, name: str) -> MockSession:
        with self._lock:
            self._connections += 1
        self._delay()
        state = self.device(name)
        if name in self._unreachable:
            raise MockUnreachable(f"{name}: unreachable")
        with state.lock:
            if state.fail_connect_times > 0:
                state.fail_connect_times -= 1
                raise MockError(f"{name}: connection refused")
        if self._failure_rate > 0:
            with self._rng_lock:
                fail = self._rng.random() < self._failure_rate
            if fail:
                raise MockError(f"{name}: connection timed out")
        with self._lock:
            self._active += 1
            self._max_active = max(self._max_active, self._active)
        return MockSession(self, state)

    def open(self, target: ConnectTarget) -> MockSession:
        """The Transport interface: map simulated failures to the engine's error types."""
        try:
            return self.connect(target.device)
        except MockUnreachable as exc:
            raise UnreachableError(str(exc)) from None
        except MockError as exc:
            raise ConnectError(str(exc)) from None

    def _release(self) -> None:
        with self._lock:
            self._active -= 1

    @property
    def active_sessions(self) -> int:
        with self._lock:
            return self._active

    @property
    def max_concurrent_sessions(self) -> int:
        with self._lock:
            return self._max_active

    @property
    def connections(self) -> int:
        """Total connect attempts, including failed ones."""
        with self._lock:
            return self._connections
