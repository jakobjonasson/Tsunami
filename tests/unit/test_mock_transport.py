"""Mock transport (design.md, Testing: Scale)."""

from __future__ import annotations

import threading

import pytest

from tsunami.core.transports.mock import (
    MockError,
    MockTransport,
    MockUnreachable,
)


def make(running: list[str] | None = None):
    t = MockTransport()
    st = t.add_device("sw1", running or [])
    return t, st, t.connect("sw1")


def test_config_nesting_and_append() -> None:
    t, st, s = make(["hostname sw1", "interface Gi1", " description old", "line vty 0 4"])
    r = s.send_config(["interface Gi1", " shutdown", " description old", "vlan 10", " name x"])
    assert r.ok
    assert st.running == [
        "hostname sw1",
        "interface Gi1",
        " description old",
        " shutdown",
        "line vty 0 4",
        "vlan 10",
        " name x",
    ]
    s.send_config(["vlan 10"])
    assert st.running.count("vlan 10") == 1


def test_config_no_removal() -> None:
    t, st, s = make(["a", " b", " c", "d"])
    s.send_config(["a", " no b"])
    assert st.running == ["a", " c", "d"]
    s.send_config(["no a", "no missing"])
    assert st.running == ["d"]


def test_config_rejected_line_partial_apply() -> None:
    t, st, s = make(["a"])
    st.reject_lines.append(r"^bogus")
    r = s.send_config(["b", "bogus cmd", "c"])
    assert not r.ok
    assert r.failed_line == "bogus cmd"
    assert "% Invalid input detected at '^' marker." in r.output
    assert st.running == ["a", "b"]


def test_flash_backup_and_replace() -> None:
    t, st, s = make(["a", "b"])
    s.send_exec("copy running-config flash:bk", expect_prompt="]?", answer="")
    s.send_config(["c"])
    assert st.flash["bk"] == ["a", "b"]
    s.send_exec("configure replace flash:bk force")
    assert st.running == ["a", "b"]
    assert "Error" in s.send_exec("configure replace flash:nope force").output


def test_write_memory() -> None:
    t, st, s = make(["a"])
    s.send_exec("write memory")
    assert st.startup == ["a"]


def test_checkpoints() -> None:
    t, st, s = make(["a"])
    s.send_exec("copy running-config checkpoint cp1")
    s.send_config(["b"])
    s.send_exec("checkpoint rollback cp1")
    assert st.running == ["a"]
    s.send_exec("erase checkpoint cp1")
    assert st.checkpoints == {}


def test_cfg_backup_restore() -> None:
    t, st, s = make(["a"])
    s.send_exec("cfg-backup running-config config bk")
    assert st.flash["bk"] == ["a"]
    s.send_config(["b"])
    out = s.send_exec("cfg-restore flash bk").output
    assert out == "Successfully applied configuration 'bk' to running configuration."
    assert st.running == ["a"]
    assert s.send_exec("show cfg-restore status").output == "Status : Success"


def test_delete_files() -> None:
    t, st, s = make(["a"])
    st.flash = {"x": ["a"], "y": ["a"]}
    s.send_exec("delete flash:x")
    s.send_exec("erase config y")
    assert st.flash == {}


def test_exec_responses() -> None:
    t, st, s = make()
    st.exec_responses = {"show version": "v1", "re:^show int .*": "re-out"}
    assert s.send_exec("show version").output == "v1"
    assert s.send_exec("show int Gi1").output == "re-out"
    assert s.send_exec("show nothing").output == ""
    assert ("exec", "show version") in st.sent


def test_running_config_format() -> None:
    t, st, s = make(["a", " b"])
    assert s.running_config() == "a\n b\nend"


def test_latency_injected_sleep_deterministic() -> None:
    def run() -> list[float]:
        calls: list[float] = []
        t = MockTransport(latency=(0.1, 0.5), seed=7, sleep=calls.append)
        t.add_device("sw1")
        s = t.connect("sw1")
        s.send_config(["a"])
        s.send_exec("x")
        s.running_config()
        return calls

    a = run()
    assert len(a) == 4
    assert all(0.1 <= d <= 0.5 for d in a)
    assert a == run()


def test_unreachable() -> None:
    t = MockTransport(unreachable=["sw1"])
    t.add_device("sw1")
    with pytest.raises(MockUnreachable):
        t.connect("sw1")
    assert t.active_sessions == 0


def test_failure_rate_seeded() -> None:
    t = MockTransport(failure_rate=0.2, seed=1)
    t.add_device("sw1")
    fails = 0
    for _ in range(1000):
        try:
            t.connect("sw1").close()
        except MockError:
            fails += 1
    assert 150 <= fails <= 250
    assert t.connections == 1000
    assert t.active_sessions == 0


def test_fail_connect_times() -> None:
    t = MockTransport()
    t.add_device("sw1", fail_connect_times=2)
    for _ in range(2):
        with pytest.raises(MockError):
            t.connect("sw1")
    t.connect("sw1")
    assert t.connections == 3


def test_drop_after_sends() -> None:
    t = MockTransport()
    st = t.add_device("sw1", drop_after_sends=2)
    s = t.connect("sw1")
    s.send_config(["a"])
    s.send_exec("x")
    with pytest.raises(MockError, match="connection lost"):
        s.send_config(["b"])
    assert st.running == ["a"]
    assert t.active_sessions == 0
    t.connect("sw1")  # reconnect works


def test_close_idempotent() -> None:
    t, _, s = make()
    assert t.active_sessions == 1
    s.close()
    s.close()
    assert t.active_sessions == 0
    with pytest.raises(MockError):
        s.send_exec("x")


def test_concurrency_200_devices_cap_50() -> None:
    t = MockTransport(latency=(0.0, 0.001), seed=3)
    for i in range(200):
        t.add_device(f"sw{i}", ["hostname x"])
    sem = threading.Semaphore(50)
    names = iter(range(200))
    name_lock = threading.Lock()
    errors: list[BaseException] = []

    def worker() -> None:
        while True:
            with name_lock:
                i = next(names, None)
            if i is None:
                return
            try:
                with sem:
                    s = t.connect(f"sw{i}")
                    try:
                        assert s.send_config(["vlan 10", " name v"]).ok
                    finally:
                        s.close()
            except BaseException as e:  # noqa: BLE001
                errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(50)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert not errors
    assert t.max_concurrent_sessions <= 50
    assert t.active_sessions == 0
    assert t.connections == 200
    assert all(t.device(f"sw{i}").running[-2:] == ["vlan 10", " name v"] for i in range(200))
