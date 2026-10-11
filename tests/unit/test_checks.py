"""Pre/post checks (work package 3.5)."""

from __future__ import annotations

from tsunami.core.checks import run_check
from tsunami.core.render.renderer import RenderedCheck
from tsunami.core.transports.base import ExecResult


class FakeSession:
    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.commands: list[str] = []

    def send_exec(self, command, **kwargs):
        self.commands.append(command)
        item = self.outputs.pop(0) if len(self.outputs) > 1 else self.outputs[0]
        if isinstance(item, Exception):
            raise item
        return ExecResult(command, item)


def _check(**kw) -> RenderedCheck:
    base = {
        "phase": "post",
        "index": 0,
        "name": None,
        "exec": "show vlan id 30",
        "expect": None,
        "expect_not": None,
        "reachable": False,
        "retries": 0,
        "delay": 5.0,
    }
    base.update(kw)
    return RenderedCheck(**base)


def _run(check, session, **kw):
    sleeps: list[float] = []
    res = run_check(
        check,
        session,
        host="192.0.2.1",
        port=22,
        connect_timeout=3.0,
        sleep=sleeps.append,
        **kw,
    )
    return res, sleeps


def test_expect_pass_and_fail():
    ok, _ = _run(_check(expect=r"^30\s+active"), FakeSession(["30  active  Gi1/0/1"]))
    assert ok.ok and ok.attempts == 1 and ok.detail == ""
    bad, _ = _run(_check(expect=r"^30\s+active"), FakeSession(["no such vlan"]))
    assert not bad.ok
    assert "not found" in bad.detail
    assert bad.output == "no such vlan"


def test_expect_not_pass_and_fail():
    ok, _ = _run(_check(expect_not="down"), FakeSession(["all up"]))
    assert ok.ok
    bad, _ = _run(_check(expect_not="down"), FakeSession(["Gi1 is down"]))
    assert not bad.ok and "unexpected" in bad.detail


def test_multiline_regex():
    res, _ = _run(_check(expect=r"^line two$"), FakeSession(["line one\nline two\nthree"]))
    assert res.ok


def test_retries_sleep_delays():
    s = FakeSession(["bad", "bad", "bad"])
    res, sleeps = _run(_check(expect="good", retries=2, delay=7.5), s)
    assert not res.ok and res.attempts == 3
    assert sleeps == [7.5, 7.5]
    assert len(s.commands) == 3


def test_stops_at_first_success():
    s = FakeSession(["bad", "good", "never"])
    res, sleeps = _run(_check(expect="good", retries=5, delay=2), s)
    assert res.ok and res.attempts == 2
    assert sleeps == [2]
    assert len(s.commands) == 2


def test_session_exception_is_failed_attempt():
    s = FakeSession([TimeoutError("secret-ish text"), "good"])
    res, sleeps = _run(_check(expect="good", retries=1), s)
    assert res.ok and res.attempts == 2 and sleeps == [5.0]
    s2 = FakeSession([TimeoutError("boom")])
    res2, _ = _run(_check(expect="good"), s2)
    assert not res2.ok
    assert "TimeoutError" in res2.detail
    assert "boom" not in res2.detail


def test_reachable():
    calls = []

    def up(host, port, timeout):
        calls.append((host, port, timeout))
        return True

    res, _ = _run(_check(exec=None, reachable=True), None, tcp_connect=up)
    assert res.ok and calls == [("192.0.2.1", 22, 3.0)]
    assert res.name == "post 1: reachable"

    res, sleeps = _run(
        _check(exec=None, reachable=True, retries=1, delay=1),
        None,
        tcp_connect=lambda h, p, t: False,
    )
    assert not res.ok and res.attempts == 2 and sleeps == [1]
    assert "192.0.2.1:22" in res.detail


def test_reachable_exception():
    def boom(h, p, t):
        raise RuntimeError("x")

    res, _ = _run(_check(exec=None, reachable=True), None, tcp_connect=boom)
    assert not res.ok and "RuntimeError" in res.detail


def test_none_session_exec_check():
    res, sleeps = _run(_check(expect="x"), None)
    assert not res.ok and "no session" in res.detail and sleeps == []


def test_names():
    assert _run(_check(name="vlan up", expect="a"), FakeSession(["a"]))[0].name == "vlan up"
    res, _ = _run(_check(expect="a", index=1), FakeSession(["a"]))
    assert res.name == "post 2: show vlan id 30"
