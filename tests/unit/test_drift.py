"""The drift check: first backups, drift since the newest backup, failures per device."""

from __future__ import annotations

import threading

import pytest

from tsunami.core.backups import DRIFT_TAG, list_backups
from tsunami.core.drift import DriftCheck, DriftDevice, DriftStatus
from tsunami.core.issues import TsunamiError
from tsunami.core.models.common import Platform
from tsunami.core.redact import Redactor
from tsunami.core.secret_store import LoginCredential
from tsunami.core.transports.base import (
    AuthenticationError,
    HostKeyMismatchError,
    UnreachableError,
)

LOGIN = LoginCredential("netops", "Drift-Login-Pw-1")
BRANCH = LoginCredential("branch", "Branch-Login-Pw-2")
ENABLE = "Enable-Secret-Value-9"


def config(ntp: str = "192.0.2.123", enable: str = ENABLE) -> str:
    return (
        "Building configuration...\n\nCurrent configuration : 900 bytes\n"
        "! Last configuration change at 10:00:00 UTC Fri Oct 30 2026\n"
        f"hostname sw\nenable secret {enable}\nntp server {ntp}\nend\n"
    )


class Session:
    def __init__(self, text: str | Exception) -> None:
        self.text = text

    def running_config(self) -> str:
        if isinstance(self.text, Exception):
            raise self.text
        return self.text

    def close(self) -> None:
        pass


class Transport:
    """Configs (or connect errors) by address; the targets opened are kept."""

    def __init__(self, devices: dict) -> None:
        self.devices = devices
        self.targets: list = []
        self.lock = threading.Lock()

    def open(self, target):
        with self.lock:
            self.targets.append(target)
        value = self.devices[target.host]
        if isinstance(value, ConnectFailure):
            raise value.error
        return Session(value)


class ConnectFailure:
    def __init__(self, error: Exception) -> None:
        self.error = error


DEVICES = [
    DriftDevice("sw1", "192.0.2.1", Platform.IOS_XE, "netops"),
    DriftDevice("sw2", "192.0.2.2", Platform.IOS_XE, "netops"),
    DriftDevice("sw3", "192.0.2.3", Platform.IOS_XE, "branch", port=2222),
]


def check(app_paths, configs, devices=DEVICES, *, logins=None, results=None, **kwargs):
    transport = Transport(configs)
    drift = DriftCheck(
        app_paths,
        "acme",
        devices,
        logins={"netops": LOGIN, "branch": BRANCH} if logins is None else logins,
        transport=transport,
        redactor=Redactor(),
        on_result=results.append if results is not None else None,
        **kwargs,
    )
    return drift.run(), transport


def by_device(report):
    return {r.device: r for r in report.results}


def test_first_check_saves_a_first_backup_of_each_device(app_paths):
    configs = {f"192.0.2.{n}": config() for n in (1, 2, 3)}
    results: list = []
    report, transport = check(app_paths, configs, results=results)
    assert {r.device: r.status for r in report.results} == dict.fromkeys(
        ("sw1", "sw2", "sw3"), DriftStatus.FIRST
    )
    assert len(results) == 3  # each reported as it finished
    [entry] = list_backups(app_paths, "acme", "sw1")
    assert entry.job_id == DRIFT_TAG
    targets = {t.device: t for t in transport.targets}
    assert targets["sw3"].credential == BRANCH and targets["sw3"].port == 2222
    assert targets["sw1"].credential == LOGIN
    assert report.summary() == "0 changed, 0 unchanged, 3 first backups."


def test_drift_since_the_newest_backup(app_paths):
    configs = {f"192.0.2.{n}": config() for n in (1, 2, 3)}
    check(app_paths, configs)
    configs["192.0.2.2"] = config(ntp="192.0.2.124")
    configs["192.0.2.3"] = config(enable="Rotated-Enable-77")
    report, _ = check(app_paths, configs)
    results = by_device(report)
    assert results["sw1"].status == DriftStatus.UNCHANGED and results["sw1"].diff == ""
    changed = results["sw2"]
    assert changed.status == DriftStatus.CHANGED
    assert (changed.added, changed.removed) == (1, 1)
    assert "+ ntp server 192.0.2.124" in changed.diff.split("\n")
    assert changed.previous_at is not None
    assert results["sw3"].status == DriftStatus.SECRETS
    for r in report.results:
        assert ENABLE not in r.diff and "Rotated-Enable-77" not in r.diff
    assert len(list_backups(app_paths, "acme", "sw1")) == 1
    assert len(list_backups(app_paths, "acme", "sw2")) == 2
    assert report.summary() == "2 changed, 1 unchanged, 0 first backups."


@pytest.mark.parametrize(
    ("error", "status"),
    [
        (UnreachableError("timed out"), DriftStatus.UNREACHABLE),
        (AuthenticationError(f"denied for {LOGIN.password}"), DriftStatus.LOGIN_FAILED),
        (HostKeyMismatchError("key changed"), DriftStatus.HOSTKEY_CHANGED),
    ],
)
def test_connect_failures_are_reported_per_device(app_paths, error, status):
    configs = {"192.0.2.1": ConnectFailure(error), "192.0.2.2": config(), "192.0.2.3": config()}
    report, _ = check(app_paths, configs)
    results = by_device(report)
    assert results["sw1"].status == status
    assert LOGIN.password not in results["sw1"].detail
    assert results["sw2"].status == DriftStatus.FIRST
    assert report.summary().endswith("1 not read.")


def test_a_read_failure_is_redacted(app_paths):
    configs = {"192.0.2.1": OSError(f"lost channel {LOGIN.password}")}
    report, _ = check(app_paths, configs, DEVICES[:1])
    [result] = report.results
    assert result.status == DriftStatus.FAILED
    assert LOGIN.password not in result.detail
    assert list_backups(app_paths, "acme", "sw1") == []


def test_a_device_whose_login_is_not_stored(app_paths):
    configs = {f"192.0.2.{n}": config() for n in (1, 2, 3)}
    report, transport = check(app_paths, configs, logins={"netops": LOGIN})
    assert by_device(report)["sw3"].status == DriftStatus.NO_LOGIN
    assert "192.0.2.3" not in {t.host for t in transport.targets}


def test_cancelled_before_start_reads_nothing(app_paths):
    cancel = threading.Event()
    cancel.set()
    report, transport = check(app_paths, {}, cancel=cancel)
    assert {r.status for r in report.results} == {DriftStatus.CANCELLED}
    assert report.cancelled and transport.targets == []


def test_needs_devices_and_a_redactor(app_paths):
    with pytest.raises(TsunamiError, match="no devices"):
        DriftCheck(app_paths, "acme", [], logins={}, transport=None, redactor=Redactor())
    with pytest.raises(TsunamiError, match="redactor"):
        DriftCheck(app_paths, "acme", DEVICES, logins={}, transport=None, redactor=None)


# -- the check as a job in history.db ---------------------------------------------------


@pytest.fixture
def hist(tmp_path):
    from tsunami.core.history import History

    h = History(tmp_path / "history.db")
    yield h
    h.close()


def test_a_check_is_recorded_as_a_job(app_paths, hist):
    from tsunami.core.drift import DRIFT_JOB, DRIFT_MODE
    from tsunami.core.models.jobs import DeviceStatus, JobStatus

    configs = {f"192.0.2.{n}": config() for n in (1, 2, 3)}
    check(app_paths, configs, history=hist)
    configs["192.0.2.2"] = config(ntp="192.0.2.124")
    configs["192.0.2.3"] = ConnectFailure(UnreachableError("timed out"))
    report, _ = check(app_paths, configs, history=hist)

    newest, first = hist.list_jobs(customer="acme")
    assert (newest.play_path, newest.mode, newest.customer) == (DRIFT_JOB, DRIFT_MODE, "acme")
    assert first.status == JobStatus.COMPLETED
    assert newest.status == JobStatus.PARTIAL  # sw3 was not read
    assert newest.started_at and newest.finished_at
    assert newest.summary["counts"] == {"no change": 1, "changed": 1, "unreachable": 1}
    rows = {d.device: d for d in hist.devices(newest.id)}
    assert {n: r.status for n, r in rows.items()} == {
        "sw1": DeviceStatus.SUCCESS,
        "sw2": DeviceStatus.SUCCESS,
        "sw3": DeviceStatus.UNREACHABLE,
    }
    assert rows["sw1"].output_redacted == "no change"
    assert rows["sw2"].output_redacted == "changed (+1 -1): saved a new backup"
    assert "+ ntp server 192.0.2.124" in rows["sw2"].diff_redacted.split("\n")
    newest_backup = list_backups(app_paths, "acme", "sw2")[0]
    assert rows["sw2"].backup_path == str(newest_backup.path)
    assert rows["sw1"].backup_path is None and rows["sw1"].diff_redacted is None
    assert rows["sw3"].error_redacted == "timed out"
    assert all(r.started_at and r.finished_at for r in rows.values())
    for r in rows.values():
        for text in (r.output_redacted, r.diff_redacted, r.error_redacted):
            assert ENABLE not in (text or "") and LOGIN.password not in (text or "")


def test_a_recorded_check_updates_last_job_and_last_contact(app_paths, hist):
    configs = {"192.0.2.1": config(), "192.0.2.2": ConnectFailure(UnreachableError("down"))}
    check(app_paths, configs, DEVICES[:2], history=hist)
    activity = hist.device_activity("acme")
    assert set(activity) == {"sw1", "sw2"}
    assert str(activity["sw1"].status) == "success"
    contact = hist.last_contact("acme")
    assert set(contact) == {"sw1"}  # sw2 was never reached


@pytest.mark.parametrize(
    ("status", "recorded"),
    [
        (DriftStatus.FIRST, "success"),
        (DriftStatus.SECRETS, "success"),
        (DriftStatus.LOGIN_FAILED, "failed"),
        (DriftStatus.NO_LOGIN, "skipped_precheck"),
        (DriftStatus.HOSTKEY_CHANGED, "hostkey_mismatch"),
        (DriftStatus.CANCELLED, "cancelled"),
    ],
)
def test_how_results_are_recorded(status, recorded):
    from tsunami.core.drift import device_status

    assert str(device_status(status)) == recorded


def test_every_result_has_a_recorded_status():
    from tsunami.core.drift import device_status

    for status in DriftStatus:
        device_status(status)


def test_a_cancelled_check_is_a_cancelled_job(app_paths, hist):
    from tsunami.core.models.jobs import DeviceStatus, JobStatus

    cancel = threading.Event()
    cancel.set()
    check(app_paths, {}, history=hist, cancel=cancel)
    [job] = hist.list_jobs()
    assert job.status == JobStatus.CANCELLED
    assert {d.status for d in hist.devices(job.id)} == {DeviceStatus.CANCELLED}


def test_no_device_read_is_a_failed_job(app_paths, hist):
    from tsunami.core.models.jobs import JobStatus

    configs = {"192.0.2.1": ConnectFailure(UnreachableError("down"))}
    check(app_paths, configs, DEVICES[:1], history=hist)
    assert hist.list_jobs()[0].status == JobStatus.FAILED


def test_a_history_that_fails_does_not_stop_the_check(app_paths):
    class Broken:
        def __getattr__(self, name):
            def fail(*args, **kwargs):
                raise OSError("disk full")

            return fail

    configs = {f"192.0.2.{n}": config() for n in (1, 2, 3)}
    report, _ = check(app_paths, configs, history=Broken())
    assert {r.status for r in report.results} == {DriftStatus.FIRST}


def test_a_long_check_keeps_its_job_alive(app_paths, hist, monkeypatch):
    """The approvals poller marks a running job without a heartbeat as interrupted."""
    from tsunami.core import drift

    monkeypatch.setattr(drift, "HEARTBEAT_SECONDS", 0.02)
    beats: list = []
    real = hist.heartbeat
    monkeypatch.setattr(hist, "heartbeat", lambda job_id: (beats.append(job_id), real(job_id)))

    class Slow(Transport):
        def open(self, target):
            import time

            time.sleep(0.2)
            return super().open(target)

    drift_check = DriftCheck(
        app_paths,
        "acme",
        DEVICES[:1],
        logins={"netops": LOGIN},
        transport=Slow({"192.0.2.1": config()}),
        redactor=Redactor(),
        history=hist,
    )
    drift_check.run()
    assert beats and set(beats) == {drift_check.job_id}
