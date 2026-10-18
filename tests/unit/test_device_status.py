"""Device facts, per-device activity and last contact in history.db, and the SSH launcher
(design.md, GUI: Views, device list and device page)."""

from __future__ import annotations

import inspect
import shutil
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tsunami.core import ssh_launch
from tsunami.core.history import CONNECTED_DEVICE_STATUSES, History
from tsunami.core.models.jobs import Actor, DeviceStatus
from tsunami.core.models.play import RunMode
from tsunami.core.runner import Runner, RunRequest
from tsunami.core.secret_store import KeyringSecretStore, LoginCredential
from tsunami.core.transports.mock import MockTransport
from tsunami.core.workspace import load_workspace


class FakeClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 10, 12, 0, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def hist(tmp_path: Path, clock: FakeClock):
    h = History(tmp_path / "history.db", clock=clock)
    yield h
    h.close()


def job(h: History, devices: dict[str, DeviceStatus], *, customer="acme", play="plays/a.yml"):
    """A job with these device rows; each row gets started_at and finished_at of 'now'."""
    job_id = h.create_job(
        actor=Actor.GUI,
        play_path=play,
        play_hash="h",
        customer=customer,
        mode="apply",
    )
    h.add_devices(job_id, devices)
    for name, status in devices.items():
        h.update_device(job_id, name, status=status, finished_at=h._clock())
    return job_id


# -- facts ----------------------------------------------------------------------------


def test_facts_are_remembered_and_none_keeps_the_old_value(hist, clock):
    hist.record_device_facts(
        "acme", "sw1", platform="ios-xe", model="C9300-24P", version="17.9.4a", serial="FCW1"
    )
    clock.advance(60)
    hist.record_device_facts("acme", "sw1", version="17.12.1", model=None, serial="")
    facts = hist.device_facts("acme")["sw1"]
    assert (facts.platform, facts.model, facts.version, facts.serial) == (
        "ios-xe",
        "C9300-24P",
        "17.12.1",
        "FCW1",
    )
    assert facts.updated_at == clock()


def test_facts_with_nothing_new_write_nothing(hist, clock):
    hist.record_device_facts("acme", "sw1", model="C9300")
    first = hist.device_facts("acme")["sw1"].updated_at
    clock.advance(60)
    hist.record_device_facts("acme", "sw1")
    hist.record_device_facts("acme", "sw2", model=None, version="")
    assert hist.device_facts("acme")["sw1"].updated_at == first
    assert "sw2" not in hist.device_facts("acme")


def test_facts_are_per_customer(hist):
    hist.record_device_facts("acme", "sw1", model="A")
    hist.record_device_facts("other", "sw1", model="B")
    assert hist.device_facts("acme")["sw1"].model == "A"
    assert hist.device_facts("other")["sw1"].model == "B"
    assert hist.device_facts("nobody") == {}


# -- activity and last contact --------------------------------------------------------


def test_activity_is_the_newest_job_row_per_device(hist, clock):
    first = job(hist, {"sw1": DeviceStatus.SUCCESS, "sw2": DeviceStatus.FAILED}, play="plays/a.yml")
    clock.advance(60)
    second = job(hist, {"sw1": DeviceStatus.UNREACHABLE}, play="plays/b.yml")
    clock.advance(60)
    job(hist, {"sw1": DeviceStatus.SUCCESS}, customer="other")

    activity = hist.device_activity("acme")
    assert set(activity) == {"sw1", "sw2"}
    assert activity["sw1"].job_id == second
    assert activity["sw1"].play_path == "plays/b.yml"
    assert activity["sw1"].status == DeviceStatus.UNREACHABLE
    assert activity["sw2"].job_id == first
    assert activity["sw2"].status == DeviceStatus.FAILED
    assert activity["sw2"].at == clock() - timedelta(seconds=120)
    assert hist.device_activity("nobody") == {}


def test_activity_time_falls_back_to_started_then_queued(hist, clock):
    job_id = hist.create_job(
        actor=Actor.GUI, play_path="p.yml", play_hash="h", customer="acme", mode="apply"
    )
    hist.add_devices(job_id, ["sw1"])
    queued = clock()
    assert hist.device_activity("acme")["sw1"].at == queued
    clock.advance(5)
    hist.update_device(job_id, "sw1", status=DeviceStatus.RUNNING, started_at=clock())
    assert hist.device_activity("acme")["sw1"].at == clock()


@pytest.mark.parametrize("status", list(DeviceStatus))
def test_only_connected_statuses_count_as_contact(hist, status):
    job(hist, {"sw1": status})
    assert ("sw1" in hist.last_contact("acme")) == (status in CONNECTED_DEVICE_STATUSES)


def test_statuses_that_never_reached_the_device_are_not_contact():
    never = {
        DeviceStatus.UNREACHABLE,
        DeviceStatus.HOSTKEY_MISMATCH,
        DeviceStatus.PENDING,
        DeviceStatus.DEVICE_BUSY,
        DeviceStatus.CANCELLED,
        DeviceStatus.SKIPPED_MAXFAIL,
        DeviceStatus.CONNECTING,
        DeviceStatus.INTERRUPTED,
    }
    assert never.isdisjoint(CONNECTED_DEVICE_STATUSES)
    assert {DeviceStatus.SUCCESS, DeviceStatus.FAILED, DeviceStatus.NO_CHANGES} <= (
        CONNECTED_DEVICE_STATUSES
    )


def test_last_contact_skips_newer_unreachable_rows(hist, clock):
    job(hist, {"sw1": DeviceStatus.SUCCESS, "sw2": DeviceStatus.UNREACHABLE})
    reached = clock()
    clock.advance(3600)
    job(hist, {"sw1": DeviceStatus.UNREACHABLE, "sw2": DeviceStatus.UNREACHABLE})
    contact = hist.last_contact("acme")
    assert contact == {"sw1": reached}  # sw2 was never reached
    assert hist.device_activity("acme")["sw1"].status == DeviceStatus.UNREACHABLE


def test_last_contact_takes_the_newest_connected_row(hist, clock):
    job(hist, {"sw1": DeviceStatus.SUCCESS})
    clock.advance(100)
    job(hist, {"sw1": DeviceStatus.FAILED})
    clock.advance(100)
    job(hist, {"sw1": DeviceStatus.HOSTKEY_MISMATCH})
    assert hist.last_contact("acme")["sw1"] == clock() - timedelta(seconds=100)


def test_recent_jobs_for_device(hist, clock):
    ids = []
    for _i in range(12):
        ids.append(job(hist, {"sw1": DeviceStatus.SUCCESS, "sw2": DeviceStatus.FAILED}))
        clock.advance(10)
    job(hist, {"sw1": DeviceStatus.SUCCESS}, customer="other")
    recent = hist.recent_jobs_for_device("acme", "sw1")
    assert [r.job_id for r in recent] == ids[::-1][:10]
    assert recent[0].mode == "apply" and recent[0].status == DeviceStatus.SUCCESS
    assert len(hist.recent_jobs_for_device("acme", "sw1", limit=3)) == 3
    assert [r.status for r in hist.recent_jobs_for_device("acme", "sw2", limit=1)] == [
        DeviceStatus.FAILED
    ]
    assert hist.recent_jobs_for_device("acme", "nope") == []


def test_each_query_is_one_statement_for_250_devices(hist, clock):
    names = [f"sw{i:03d}" for i in range(250)]
    for _ in range(3):
        job(hist, dict.fromkeys(names, DeviceStatus.SUCCESS))
        clock.advance(1)
    for name in names:
        hist.record_device_facts("acme", name, version="1.0")

    seen: list[str] = []
    hist._reader.set_trace_callback(seen.append)
    started = time.perf_counter()
    facts = hist.device_facts("acme")
    activity = hist.device_activity("acme")
    contact = hist.last_contact("acme")
    elapsed = time.perf_counter() - started
    hist._reader.set_trace_callback(None)

    assert len(facts) == len(activity) == len(contact) == 250
    assert len([s for s in seen if s.lstrip().upper().startswith("SELECT")]) == 3
    assert elapsed < 2.0


# -- the pipeline records facts -------------------------------------------------------

SHOW_VERSION = """\
Cisco IOS XE Software, Version 17.09.04a
Cisco IOS Software [Cupertino], Catalyst L3 Switch Software (CAT9K_IOSXE), Version 17.9.4a
sa-as01 uptime is 3 weeks, 2 days
Model Number                       : C9300-24P
System Serial Number               : FCW2233L0AB
"""
FIXTURE = Path(__file__).parents[1] / "fixtures" / "workspace"


def _run_check(tmp_path, app_paths, show_version: str, history: History) -> None:
    root = tmp_path / "ws"
    shutil.copytree(FIXTURE, root)
    secrets = KeyringSecretStore(index_path=tmp_path / "credential-index.json")
    secrets.set_login("acme-netadmin", LoginCredential("netadmin", "Login-Pass-1234"))
    secrets.set_secret("acme", "tacacs", "Real-Tacacs-Key-77")
    mock = MockTransport()
    for name in ("sa-as01", "sa-as02"):
        state = mock.add_device(name, running=[f"hostname {name}"])
        state.exec_responses["show version"] = show_version
        state.exec_responses["re:^show vlan id"] = "30   Cameras   active"
    runner = Runner(
        load_workspace(root),
        transport=mock,
        history=history,
        secrets=secrets,
        paths=app_paths,
        on_event=lambda e: None,
        heartbeat_seconds=0.05,
    )
    runner.run(
        RunRequest(
            "plays/push/add-vlan.yml",
            RunMode.CHECK,
            prompts={"vlan_id": "30", "vlan_name": "Cameras"},
        )
    )


def test_a_run_remembers_what_the_device_reported(tmp_path, app_paths, hist):
    _run_check(tmp_path, app_paths, SHOW_VERSION, hist)
    facts = hist.device_facts("acme")
    assert set(facts) == {"sa-as01", "sa-as02"}
    assert facts["sa-as01"].model == "C9300-24P"
    assert facts["sa-as01"].version == "17.09.04a"
    assert facts["sa-as01"].serial == "FCW2233L0AB"
    assert facts["sa-as01"].platform == "ios-xe"
    assert set(hist.last_contact("acme")) == {"sa-as01", "sa-as02"}


def test_a_later_read_without_a_serial_keeps_the_known_serial(tmp_path, app_paths, hist):
    _run_check(tmp_path, app_paths, SHOW_VERSION, hist)
    no_serial = "\n".join(
        line for line in SHOW_VERSION.splitlines() if "Serial" not in line
    ).replace("17.09.04a", "17.12.1")
    _run_check(tmp_path / "again", app_paths, no_serial, hist)
    facts = hist.device_facts("acme")["sa-as01"]
    assert (facts.version, facts.serial) == ("17.12.1", "FCW2233L0AB")


# -- ssh_launch -----------------------------------------------------------------------

MOBA = Path("C:/Program Files (x86)/Mobatek/MobaXterm/MobaXterm.exe")
OPENSSH = Path("C:/Windows/System32/OpenSSH/ssh.exe")


def test_command_with_mobaxterm_and_user():
    argv = ssh_launch.build_command("203.0.113.11", 22, "netadmin", mobaxterm=MOBA)
    assert argv == [str(MOBA), "-newtab", "ssh -p 22 netadmin@203.0.113.11"]


def test_command_with_mobaxterm_without_user_and_custom_port():
    argv = ssh_launch.build_command("sa-as01.example.net", 2222, None, mobaxterm=MOBA)
    assert argv == [str(MOBA), "-newtab", "ssh -p 2222 sa-as01.example.net"]


def test_command_falls_back_to_openssh():
    argv = ssh_launch.build_command("203.0.113.11", 22, "netadmin", openssh=OPENSSH)
    assert argv == [str(OPENSSH), "-p", "22", "netadmin@203.0.113.11"]
    assert ssh_launch.build_command("203.0.113.11", 22, openssh=OPENSSH)[-1] == "203.0.113.11"


def test_command_prefers_mobaxterm_when_both_exist():
    argv = ssh_launch.build_command("203.0.113.11", 22, "u", mobaxterm=MOBA, openssh=OPENSSH)
    assert argv[0] == str(MOBA)


def test_no_client_is_an_error():
    with pytest.raises(ssh_launch.SshLaunchError, match="No SSH client"):
        ssh_launch.build_command("203.0.113.11", 22, "u")


@pytest.mark.parametrize(
    ("host", "user", "port"),
    [
        ("host; calc", None, 22),
        ("host name", None, 22),
        ("", None, 22),
        ("-oProxyCommand=x", None, 22),
        ("203.0.113.11", "bad user", 22),
        ("203.0.113.11", "u;rm", 22),
        ("203.0.113.11", 'u"', 22),
        ("203.0.113.11", None, 0),
        ("203.0.113.11", None, 70000),
    ],
)
def test_unsafe_parts_are_refused(host, user, port):
    with pytest.raises(ssh_launch.SshLaunchError):
        ssh_launch.build_command(host, port, user, mobaxterm=MOBA)


def test_ipv6_and_upn_logins_are_allowed():
    argv = ssh_launch.build_command("2001:db8::1", 22, "jo@corp.example", openssh=OPENSSH)
    assert argv[-1] == "jo@corp.example@2001:db8::1"


def test_a_password_cannot_reach_the_command():
    params = inspect.signature(ssh_launch.build_command).parameters
    assert not any("pass" in name.lower() or "secret" in name.lower() for name in params)
    open_params = inspect.signature(ssh_launch.open_ssh).parameters
    assert not any("pass" in name.lower() for name in open_params)
    argv = ssh_launch.build_command("203.0.113.11", 22, "netadmin", mobaxterm=MOBA)
    assert "Login-Pass-1234" not in " ".join(argv)


def test_find_mobaxterm_in_install_folders_then_path():
    env = {"ProgramFiles": "C:/Program Files", "ProgramFiles(x86)": "C:/Program Files (x86)"}
    x86 = Path("C:/Program Files (x86)") / "Mobatek" / "MobaXterm" / "MobaXterm.exe"
    found = ssh_launch.find_mobaxterm(env, exists=lambda p: p == x86, which=lambda _: None)
    assert found == x86
    assert ssh_launch.find_mobaxterm(env, exists=lambda p: False, which=lambda _: None) is None
    on_path = ssh_launch.find_mobaxterm(
        env, exists=lambda p: False, which=lambda _: "D:/tools/MobaXterm.exe"
    )
    assert on_path == Path("D:/tools/MobaXterm.exe")


def test_find_openssh_on_path_then_system32():
    env = {"SystemRoot": "C:/Windows"}
    assert ssh_launch.find_openssh(env, which=lambda _: "C:/x/ssh.exe") == Path("C:/x/ssh.exe")
    sys32 = Path("C:/Windows") / "System32" / "OpenSSH" / "ssh.exe"
    found = ssh_launch.find_openssh(env, exists=lambda p: p == sys32, which=lambda _: None)
    assert found == sys32
    assert ssh_launch.find_openssh({}, exists=lambda p: True, which=lambda _: None) is None


def test_open_ssh_launches_through_the_seam(monkeypatch):
    launched = []
    monkeypatch.setattr(ssh_launch, "spawn", lambda argv, **kw: launched.append((argv, kw)))
    monkeypatch.setattr(ssh_launch, "find_mobaxterm", lambda *a, **k: MOBA)
    argv = ssh_launch.open_ssh("203.0.113.11", 22, "netadmin")
    assert launched == [(argv, {})]
    assert argv[1] == "-newtab"

    launched.clear()
    monkeypatch.setattr(ssh_launch, "find_mobaxterm", lambda *a, **k: None)
    monkeypatch.setattr(ssh_launch, "find_openssh", lambda *a, **k: OPENSSH)
    argv = ssh_launch.open_ssh("203.0.113.11", 22)
    assert argv == [str(OPENSSH), "-p", "22", "203.0.113.11"]
    assert launched[0][1] == {"creationflags": 0x10}  # a console window of its own


def test_open_ssh_reports_a_missing_program(monkeypatch):
    def boom(argv, **kw):
        raise FileNotFoundError(2, "The system cannot find the file specified")

    monkeypatch.setattr(ssh_launch, "spawn", boom)
    monkeypatch.setattr(ssh_launch, "find_mobaxterm", lambda *a, **k: MOBA)
    with pytest.raises(ssh_launch.SshLaunchError, match="could not start"):
        ssh_launch.open_ssh("203.0.113.11", 22)
