"""history.db: migrations, jobs, devices, heartbeats, leases, audit (design.md, Local data)."""

from __future__ import annotations

import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tsunami.core.history import History
from tsunami.core.issues import TsunamiError
from tsunami.core.models.jobs import Actor, DeviceStatus, JobStatus


class FakeClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 10, 12, 0, 0, 123456, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "sub" / "history.db"


@pytest.fixture
def hist(db_path: Path, clock: FakeClock):
    h = History(db_path, clock=clock)
    yield h
    h.close()


def make_job(h: History, **kw) -> str:
    args = dict(
        actor=Actor.GUI, play_path="plays/a.yml", play_hash="h1", customer="acme", mode="apply"
    )
    args.update(kw)
    return h.create_job(**args)


def raw(path: Path, sql: str, params=()):
    conn = sqlite3.connect(path)
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


# --- migrations ---


def test_migrations_apply(hist: History, db_path: Path):
    tables = {r[0] for r in raw(db_path, "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {
        "schema_version",
        "jobs",
        "job_devices",
        "mcp_calls",
        "compliance_results",
        "mcp_drafts",
        "device_facts",
        "mcp_devices",
    } <= tables
    assert "device_leases" in tables
    assert raw(db_path, "SELECT version FROM schema_version") == [(4,)]


def test_wal_and_busy_timeout(hist: History):
    assert hist._conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert hist._conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    assert hist._conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_reopen_is_idempotent(db_path: Path, clock: FakeClock):
    h = History(db_path, clock=clock)
    job_id = make_job(h)
    h.close()
    h2 = History(db_path, clock=clock)
    assert h2.get_job(job_id).id == job_id
    h2.close()
    assert raw(db_path, "SELECT version FROM schema_version") == [(4,)]


def test_newer_database_is_refused(db_path: Path, clock: FakeClock):
    History(db_path, clock=clock).close()
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE schema_version SET version = 99")
    conn.commit()
    conn.close()
    with pytest.raises(TsunamiError, match="newer"):
        History(db_path, clock=clock)


# --- jobs ---


def test_job_round_trip(hist: History, db_path: Path, clock: FakeClock):
    job_id = make_job(
        hist,
        actor=Actor.MCP,
        status=JobStatus.AWAITING_APPROVAL,
        prompts={"vlan": "10", "pw": "<masked>"},
        tags=["a", "b"],
        limit=["sw1"],
    )
    job = hist.get_job(job_id)
    assert job.actor is Actor.MCP
    assert job.status is JobStatus.AWAITING_APPROVAL
    assert job.prompts == {"vlan": "10", "pw": "<masked>"}
    assert job.tags == ["a", "b"] and job.limit == ["sw1"]
    assert job.created_at == clock.now and job.created_at.tzinfo is not None
    assert job.approved_at is None and job.summary == {}
    stored = raw(db_path, "SELECT created_at FROM jobs")[0][0]
    assert stored == "2026-10-10T12:00:00.123456+00:00"


def test_get_missing_job(hist: History):
    with pytest.raises(TsunamiError):
        hist.get_job("nope")


def test_update_job_and_set_status(hist: History, clock: FakeClock):
    job_id = make_job(hist)
    hist.update_job(job_id, summary={"ok": 1}, started_at=clock.now, limit=["x"])
    hist.set_job_status(job_id, JobStatus.RUNNING, tags=["t"])
    job = hist.get_job(job_id)
    assert job.summary == {"ok": 1} and job.started_at == clock.now
    assert job.status is JobStatus.RUNNING and job.tags == ["t"] and job.limit == ["x"]
    with pytest.raises(TsunamiError, match="unknown"):
        hist.update_job(job_id, id="other")
    with pytest.raises(TsunamiError):
        hist.update_job("missing", mode="x")


def test_list_jobs_filters_and_order(hist: History, clock: FakeClock):
    ids = []
    for customer, actor, status in [
        ("acme", Actor.GUI, JobStatus.COMPLETED),
        ("acme", Actor.MCP, JobStatus.FAILED),
        ("globex", Actor.GUI, JobStatus.COMPLETED),
        ("acme", Actor.GUI, JobStatus.COMPLETED),
    ]:
        ids.append(make_job(hist, customer=customer, actor=actor, status=status))
        clock.advance(1)
    assert [j.id for j in hist.list_jobs()] == ids[::-1]
    assert [j.id for j in hist.list_jobs(customer="acme")] == [ids[3], ids[1], ids[0]]
    assert [j.id for j in hist.list_jobs(status=JobStatus.FAILED)] == [ids[1]]
    assert [j.id for j in hist.list_jobs(actor=Actor.MCP)] == [ids[1]]
    assert [j.id for j in hist.list_jobs(customer="acme", actor=Actor.GUI)] == [ids[3], ids[0]]
    assert len(hist.list_jobs(limit=2)) == 2


def test_approve(hist: History, clock: FakeClock):
    job_id = make_job(hist, status=JobStatus.AWAITING_APPROVAL)
    clock.advance(5)
    hist.approve(job_id, approved_by="alice")
    job = hist.get_job(job_id)
    assert job.status is JobStatus.APPROVED
    assert job.approved_by == "alice" and job.approved_at == clock.now


def test_approve_wrong_state(hist: History):
    job_id = make_job(hist, status=JobStatus.PENDING)
    with pytest.raises(TsunamiError):
        hist.approve(job_id, approved_by="alice")
    assert hist.get_job(job_id).status is JobStatus.PENDING
    with pytest.raises(TsunamiError):
        hist.approve("missing", approved_by="alice")


def test_expire_approvals(hist: History, clock: FakeClock):
    old = make_job(hist, status=JobStatus.AWAITING_APPROVAL)
    other_state = make_job(hist, status=JobStatus.PENDING)
    clock.advance(30 * 60)
    fresh = make_job(hist, status=JobStatus.AWAITING_APPROVAL)
    assert hist.expire_approvals(older_than_minutes=15) == [old]
    assert hist.get_job(old).status is JobStatus.EXPIRED
    assert hist.get_job(old).finished_at == clock.now
    assert hist.get_job(fresh).status is JobStatus.AWAITING_APPROVAL
    assert hist.get_job(other_state).status is JobStatus.PENDING


def test_ready_devices_of_an_expired_request_are_cancelled(hist: History, clock: FakeClock):
    from tsunami.core.models.jobs import DeviceStatus

    job_id = make_job(hist, status=JobStatus.COMPLETED)
    hist.add_devices(job_id, ["sw1", "sw2"])
    hist.update_device(job_id, "sw1", status=DeviceStatus.SUCCESS)
    hist.update_device(job_id, "sw2", status=DeviceStatus.SUCCESS)
    assert hist.mark_ready_for_approval(job_id, ready=["sw1", "sw2"], unchanged=[])
    assert hist.get_job(job_id).status is JobStatus.AWAITING_APPROVAL
    assert {d.status for d in hist.devices(job_id)} == {DeviceStatus.READY}
    assert hist.mark_ready_for_approval(job_id, ready=["sw1"], unchanged=[]) is None  # only once
    clock.advance(30 * 60)
    hist.expire_approvals(older_than_minutes=15)
    assert {d.status for d in hist.devices(job_id)} == {DeviceStatus.CANCELLED}


# --- devices ---


def test_device_round_trip(hist: History, clock: FakeClock):
    job_id = make_job(hist)
    hist.add_devices(job_id, ["sw2", "sw1", "sw3"])
    assert [d.device for d in hist.devices(job_id)] == ["sw2", "sw1", "sw3"]
    assert all(d.status is DeviceStatus.PENDING for d in hist.devices(job_id))
    hist.update_device(
        job_id,
        "sw1",
        status=DeviceStatus.FAILED,
        started_at=clock.now,
        diff_redacted="+ vlan 10",
        failed_line="vlan 10",
        error_redacted="boom",
        rollback_status="rolled_back",
        backup_path="C:/b.cfg",
    )
    d = hist.devices(job_id)[1]
    assert d.status is DeviceStatus.FAILED and d.started_at == clock.now
    assert d.diff_redacted == "+ vlan 10" and d.failed_line == "vlan 10"
    assert d.rollback_status == "rolled_back" and d.backup_path == "C:/b.cfg"
    with pytest.raises(TsunamiError):
        hist.update_device(job_id, "nope", status=DeviceStatus.FAILED)
    with pytest.raises(TsunamiError):
        hist.update_device(job_id, "sw1", render_bogus="x")


# --- heartbeats ---


def test_mark_interrupted(hist: History, clock: FakeClock):
    stale = make_job(hist, status=JobStatus.RUNNING)
    fresh = make_job(hist, status=JobStatus.RUNNING)
    done = make_job(hist, status=JobStatus.COMPLETED)
    for job_id in (stale, fresh):
        hist.add_devices(job_id, ["a", "b", "c"])
        hist.heartbeat(job_id)
        hist.update_device(job_id, "a", status=DeviceStatus.SUCCESS)
        hist.update_device(job_id, "b", status=DeviceStatus.RUNNING)
        assert hist.acquire_lease("acme", f"{job_id}-b", job_id)
    clock.advance(120)
    hist.heartbeat(fresh)
    clock.advance(10)

    assert hist.mark_interrupted(stale_after=60) == [stale]

    job = hist.get_job(stale)
    assert job.status is JobStatus.INTERRUPTED and job.finished_at == clock.now
    statuses = {d.device: d.status for d in hist.devices(stale)}
    assert statuses == {
        "a": DeviceStatus.SUCCESS,
        "b": DeviceStatus.INTERRUPTED,
        "c": DeviceStatus.INTERRUPTED,
    }
    assert hist.lease_holder("acme", f"{stale}-b") is None
    assert hist.get_job(fresh).status is JobStatus.RUNNING
    assert hist.devices(fresh)[1].status is DeviceStatus.RUNNING
    assert hist.lease_holder("acme", f"{fresh}-b") == fresh
    assert hist.get_job(done).status is JobStatus.COMPLETED
    assert hist.mark_interrupted(stale_after=60) == []


def test_mark_interrupted_without_heartbeat_uses_started_at(hist: History, clock: FakeClock):
    job_id = make_job(hist, status=JobStatus.RUNNING)
    hist.update_job(job_id, started_at=clock.now)
    clock.advance(30)
    assert hist.mark_interrupted(stale_after=60) == []
    clock.advance(60)
    assert hist.mark_interrupted(stale_after=60) == [job_id]


# --- leases ---


def test_leases(hist: History, clock: FakeClock):
    a = make_job(hist, status=JobStatus.RUNNING)
    b = make_job(hist, status=JobStatus.RUNNING)
    hist.heartbeat(a)
    hist.heartbeat(b)
    assert hist.lease_holder("acme", "sw1") is None
    assert hist.acquire_lease("acme", "sw1", a)
    assert hist.lease_holder("acme", "sw1") == a
    assert hist.acquire_lease("acme", "sw1", a)  # already ours
    assert not hist.acquire_lease("acme", "sw1", b)  # held by a live job
    assert hist.acquire_lease("globex", "sw1", b)  # leases are per customer
    clock.advance(30)
    hist.heartbeat(b)
    clock.advance(40)  # a is now 70s stale, b is 40s
    assert hist.acquire_lease("acme", "sw1", b)  # taken over
    assert hist.lease_holder("acme", "sw1") == b
    hist.release_lease("acme", "sw1", a)  # not the holder: no effect
    assert hist.lease_holder("acme", "sw1") == b
    hist.release_lease("acme", "sw1", b)
    assert hist.lease_holder("acme", "sw1") is None


# --- concurrency ---


def test_threads_update_different_devices(hist: History):
    job_id = make_job(hist, status=JobStatus.RUNNING)
    names = [f"sw{i}" for i in range(20)]
    hist.add_devices(job_id, names)
    errors: list[BaseException] = []
    barrier = threading.Barrier(len(names))

    def work(name: str) -> None:
        try:
            barrier.wait()
            hist.update_device(job_id, name, status=DeviceStatus.RUNNING)
            hist.heartbeat(job_id)
            hist.update_device(job_id, name, status=DeviceStatus.SUCCESS, output_redacted=name)
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(n,)) for n in names]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    devices = hist.devices(job_id)
    assert all(d.status is DeviceStatus.SUCCESS for d in devices)
    assert [d.output_redacted for d in devices] == names


def test_two_instances_see_each_other(db_path: Path, clock: FakeClock):
    h1 = History(db_path, clock=clock)
    h2 = History(db_path, clock=clock)
    job_id = make_job(h1, status=JobStatus.RUNNING)
    assert h2.get_job(job_id).status is JobStatus.RUNNING
    h2.heartbeat(job_id)
    assert h1.get_job(job_id).heartbeat_at == clock.now
    assert h1.acquire_lease("acme", "sw1", job_id)
    other = make_job(h2, status=JobStatus.RUNNING)
    h2.heartbeat(other)
    assert not h2.acquire_lease("acme", "sw1", other)
    h1.close()
    h2.close()


# --- audit ---


def test_mcp_calls(hist: History, db_path: Path):
    job_id = make_job(hist)
    hist.record_mcp_call(
        tool="run_play",
        args_redacted={"b": 1, "a": "<redacted>"},
        status="ok",
        duration_ms=12,
        result_bytes=345,
        job_id=job_id,
    )
    hist.record_mcp_call(
        tool="list_plays", args_redacted={}, status="error", duration_ms=1, result_bytes=0
    )
    rows = raw(
        db_path,
        "SELECT ts, tool, args_redacted_json, status, duration_ms, result_bytes, job_id"
        " FROM mcp_calls ORDER BY id",
    )
    assert rows[0] == (
        "2026-10-10T12:00:00.123456+00:00",
        "run_play",
        '{"a": "<redacted>", "b": 1}',
        "ok",
        12,
        345,
        job_id,
    )
    assert rows[1][6] is None


def test_compliance(hist: History, clock: FakeClock):
    old = make_job(hist)
    clock.advance(10)
    new = make_job(hist)
    other = make_job(hist, customer="globex")
    hist.add_compliance_result(
        job_id=old, device="sw1", missing_count=1, extra_count=2, details={"m": ["x"]}
    )
    hist.add_compliance_result(job_id=new, device="sw1", missing_count=0, extra_count=0, details={})
    hist.add_compliance_result(
        job_id=other, device="sw9", missing_count=5, extra_count=5, details={}
    )
    rows = hist.compliance_history("acme")
    assert [r["job_id"] for r in rows] == [new, old]
    assert rows[1]["missing_count"] == 1 and rows[1]["extra_count"] == 2
    assert rows[1]["details"] == {"m": ["x"]} and rows[1]["device"] == "sw1"
    assert len(hist.compliance_history("acme", limit=1)) == 1
    assert hist.compliance_history("nobody") == []


def test_reads_do_not_wait_for_a_writer(hist: History):
    """The GUI thread reads while run workers write: it must never queue behind them."""
    import threading
    import time

    make_job(hist)
    holding, release = threading.Event(), threading.Event()

    def writer() -> None:
        with hist._tx():
            holding.set()
            release.wait(5)

    thread = threading.Thread(target=writer)
    thread.start()
    holding.wait(5)
    start = time.monotonic()
    jobs = hist.list_jobs()
    elapsed = time.monotonic() - start
    release.set()
    thread.join()
    assert len(jobs) == 1 and elapsed < 0.5


def test_mcp_devices_are_remembered_per_customer(hist: History):
    assert hist.mcp_devices("acme") == {}
    hist.record_mcp_device("acme", "sw-new", "192.0.2.9")
    hist.record_mcp_device("acme", "sw-new", "192.0.2.10")  # added again: the newer host
    hist.record_mcp_device("other", "sw-x", "192.0.2.11")
    assert hist.mcp_devices("acme") == {"sw-new": "192.0.2.10"}
