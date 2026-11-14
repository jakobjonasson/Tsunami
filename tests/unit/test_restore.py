"""Restore from backup: what is sent (plan_restore), the job against the mock transport,
and NetmikoSession.paste_config against a fake Netmiko connection."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from tsunami.core.backups import PRE_RESTORE_TAG, RESTORE_TAG, list_backups
from tsunami.core.issues import TsunamiError
from tsunami.core.models.common import Platform, PlatformFamily
from tsunami.core.models.jobs import DeviceStatus, JobStatus
from tsunami.core.models.play import ConnectionMethod
from tsunami.core.operations import RESTORE_MODE, listed_with_plays
from tsunami.core.redact import Redactor
from tsunami.core.restore import (
    KEYGEN,
    RestoreJob,
    RestoreRequest,
    address_in_config,
    plan_restore,
)
from tsunami.core.secret_store import LoginCredential
from tsunami.core.transports.base import ConfigChunk, ConnectTarget, UnreachableError
from tsunami.core.transports.mock import MockTransport
from tsunami.core.transports.netmiko_session import NetmikoSession

ENABLE = "Enable-Secret-4321"
TACACS = "Real-Tacacs-Key-77"
TYPED = LoginCredential("netadmin", "Typed-Pass-5150", enable="Typed-Enable-8080")

IOS_BACKUP = f"""Building configuration...

Current configuration : 1234 bytes
!
! Last configuration change at 10:00:00 UTC Fri Nov 13 2026
!
version 15.2
hostname sa-as01
!
enable secret 9 {ENABLE}
!
interface Vlan10
 ip address 192.0.2.10 255.255.255.0
!
tacacs server T1
 address ipv4 192.0.2.20
 key 7 {TACACS}
!
banner motd ^C
Authorized access only
^C
crypto pki certificate chain TP-self-signed-1
 certificate self-signed 01
  3082 0222 3082
  018B A0030201
  \tquit
ip ssh version 2
line vty 0 4
 login local
!
end
"""
# The mock shows a banner with the delimiter it was sent, where IOS prints ^C again.
NO_BANNER = IOS_BACKUP.replace("banner motd ^C\nAuthorized access only\n^C\n", "")


def _backup(app_paths, text: str = IOS_BACKUP, device: str = "sa-as01") -> Path:
    folder = app_paths.backups_dir / "acme" / device
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "20261101T010101Z_job1.cfg"
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


def _sent_lines(plan) -> list[str]:
    return [line for chunk in plan.chunks for line in chunk.lines]


# -- plan_restore --------------------------------------------------------------------------


def test_ios_plan_drops_headers_comments_and_end_and_keeps_order():
    plan = plan_restore(IOS_BACKUP, Platform.IOS)
    lines = _sent_lines(plan)
    assert lines[:3] == ["version 15.2", "hostname sa-as01", f"enable secret 9 {ENABLE}"]
    assert not any(line.startswith(("Building", "Current", "!")) for line in lines)
    assert "end" not in lines
    assert lines[-3:] == ["ip ssh version 2", "line vty 0 4", " login local"]
    assert plan.skipped == ()


def test_ios_banner_is_one_block_with_a_delimiter_ios_reads_as_one_character():
    plan = plan_restore(IOS_BACKUP, Platform.IOS)
    (banner,) = [c for c in plan.chunks if c.lines[0].startswith("banner")]
    assert banner.block
    assert banner.lines == ("banner motd #", "Authorized access only", "#")


def test_a_one_line_banner_and_a_banner_holding_the_first_free_delimiter():
    text = "banner login ^CKeep out^C\nbanner exec ^C\nRoom #4\n^C\n"
    chunks = plan_restore(text, Platform.IOS).chunks
    assert chunks[0].lines == ("banner login #Keep out#",)
    assert chunks[1].lines == ("banner exec $", "Room #4", "$")


def test_ios_certificate_is_sent_as_one_block_through_quit():
    plan = plan_restore(IOS_BACKUP, Platform.IOS)
    (cert,) = [c for c in plan.chunks if c.lines[0].strip().startswith("certificate")]
    assert cert.block and cert.lines[-1].strip() == "quit"
    assert len(cert.lines) == 4


def test_aos_s_plan_keeps_exits_and_skips_guarded_contexts():
    text = (
        "; J9773A Configuration Editor; Created on release #YA.16.11.0002\n"
        "; Ver #14:01.4f.f8.1d.9b.3f.bf.bb.ef.7c.59.fc.6b.fb.9f.fc.ff.ff.37.ef:02\n"
        'hostname "sa-as01"\n'
        "module 1 type j9773a\n"
        "stacking\n"
        '   member 1 type "J9773A" mac-address 000000-000001\n'
        "   exit\n"
        "vlan 10\n"
        '   name "Users"\n'
        "   exit\n"
        "console inactivity-timer 10\n"
    )
    plan = plan_restore(text, Platform.AOS_S)
    assert _sent_lines(plan) == ['hostname "sa-as01"', "vlan 10", '   name "Users"', "   exit"]
    skipped = [s.line.strip() for s in plan.skipped]
    assert skipped[0] == "module 1 type j9773a"
    assert "stacking" in skipped and "exit" in skipped  # the context and its exit
    assert "console inactivity-timer 10" in skipped
    assert all("apply it by hand" in s.reason or "inside" in s.reason for s in plan.skipped)


def test_aos_cx_banner_keeps_its_bang_lines_and_console_speed_is_skipped():
    text = (
        "Current configuration:\n"
        "!\n"
        "!Version AOS-CX FL.10.10.1000\n"
        "!export-password: default\n"
        "hostname sa-cx01\n"
        "banner motd !\n"
        "Lab switch\n"
        "!\n"
        "console baud-rate 9600\n"
        "interface 1/1/1\n"
        "    no shutdown\n"
    )
    plan = plan_restore(text, Platform.AOS_CX)
    assert [c.lines for c in plan.chunks] == [
        ("hostname sa-cx01",),
        ("banner motd !", "Lab switch", "!"),
        ("interface 1/1/1",),
        ("    no shutdown",),
    ]
    assert [s.line for s in plan.skipped] == ["console baud-rate 9600"]


def test_address_in_config():
    assert address_in_config(IOS_BACKUP, "192.0.2.10")
    assert not address_in_config(IOS_BACKUP, "192.0.2.1")
    assert address_in_config("interface vlan 10\n    ip address 192.0.2.5/24\n", "192.0.2.5")


# -- the job -------------------------------------------------------------------------------


@pytest.fixture
def hist(tmp_path):
    from tsunami.core.history import History

    h = History(tmp_path / "history.db")
    yield h
    h.close()


def _request(backup: Path, **changes) -> RestoreRequest:
    fields = dict(
        customer="acme",
        device="sa-as01",
        platform=Platform.IOS,
        backup=backup,
        method=ConnectionMethod.SERIAL,
        serial_port="COM3",
        baud=9600,
        credential=TYPED,
        login_label="typed (netadmin)",
    )
    fields.update(changes)
    return RestoreRequest(**fields)


def _job(app_paths, request, transport, **kwargs) -> RestoreJob:
    return RestoreJob(
        app_paths, request, transport=transport, redactor=Redactor([TACACS]), **kwargs
    )


def _factory(transport: MockTransport, **state):
    return transport.add_device("sa-as01", ["hostname Switch", "interface Vlan1"], **state)


def test_a_restore_sends_every_line_saves_and_reads_back(app_paths, hist):
    backup = _backup(app_paths, NO_BANNER)
    transport = MockTransport()
    state = _factory(transport)
    logs: list[str] = []
    progress: list[tuple[int, int]] = []

    result = _job(
        app_paths,
        _request(backup),
        transport,
        history=hist,
        on_log=logs.append,
        on_progress=lambda sent, total: progress.append((sent, total)),
    ).run()

    assert result.status == DeviceStatus.SUCCESS, result.report()
    assert result.saved and state.startup == state.running  # write memory ran
    assert "hostname sa-as01" in state.running and " login local" in state.running
    assert result.sent == result.total == progress[-1][0] == progress[-1][1]
    assert result.missing == 0 and "every line of the backup" in result.summary
    assert state.ssh_keys  # IOS keys created; the keygen is not a config line
    assert not any(KEYGEN in line for line in state.running)

    tags = [e.job_id for e in list_backups(app_paths, "acme", "sa-as01")]
    assert tags[:2] == [RESTORE_TAG, PRE_RESTORE_TAG]  # newest first
    assert result.post_backup is not None and result.pre_backup is not None

    (job,) = hist.list_jobs(customer="acme")
    assert job.mode == RESTORE_MODE and job.play_path == "Restore from backup"
    assert job.status == JobStatus.COMPLETED
    assert job.summary["via"] == "console COM3 at 9600 baud" and job.summary["login"]
    (row,) = hist.devices(job.id)
    assert row.status == DeviceStatus.SUCCESS
    assert row.backup_path == str(backup)
    assert hist.lease_holder("acme", "sa-as01") is None  # released
    assert listed_with_plays(job.mode)  # History shows it without Show operations

    everything = "\n".join([*logs, result.report(), row.output_redacted or ""])
    for secret in (TYPED.password, TYPED.enable, TACACS):
        assert secret not in everything


def test_rejected_lines_are_reported_redacted_and_the_rest_still_applied(app_paths, hist):
    backup = _backup(app_paths)
    transport = MockTransport()
    state = _factory(transport, reject_lines=[r"^key 7 ", r"^address ipv4"])

    result = _job(app_paths, _request(backup), transport, history=hist).run()

    assert result.status == DeviceStatus.FAILED
    assert len(result.rejected) == 2 and "2 rejected" in result.summary
    assert all(TACACS not in line for line, _ in result.rejected)
    assert "<redacted>" in result.rejected[1][0]
    assert " login local" in state.running  # lines after the rejected ones were sent
    (job,) = hist.list_jobs(customer="acme")
    (row,) = hist.devices(job.id)
    assert "Rejected by the device:" in row.output_redacted
    assert TACACS not in row.output_redacted


def test_a_session_that_ends_part_way_is_interrupted(app_paths, hist):
    backup = _backup(app_paths)
    transport = MockTransport()
    _factory(transport, drop_after_sends=5)  # one read, then four chunks

    result = _job(app_paths, _request(backup), transport, history=hist).run()

    assert result.status == DeviceStatus.INTERRUPTED  # raises the half-configured alarm
    assert 0 < result.sent < result.total
    assert "partly restored" in result.summary
    assert not result.saved


def test_cancel_stops_between_lines(app_paths):
    backup = _backup(app_paths)
    transport = MockTransport()
    _factory(transport)
    cancel = threading.Event()

    def progress(sent: int, total: int) -> None:
        if sent >= 3:
            cancel.set()

    result = _job(app_paths, _request(backup), transport, cancel=cancel, on_progress=progress).run()
    assert result.status == DeviceStatus.CANCELLED
    assert 3 <= result.sent < result.total


def test_unreachable_and_busy_devices_send_nothing(app_paths, hist):
    backup = _backup(app_paths)

    class Down:
        def open(self, target: ConnectTarget):
            raise UnreachableError(f"{target.serial_port}: no console prompt after 300s")

    result = _job(app_paths, _request(backup), Down(), history=hist).run()
    assert result.status == DeviceStatus.UNREACHABLE and "no console prompt" in result.summary

    other = hist.create_job(
        actor="gui",
        play_path="x",
        play_hash="",
        customer="acme",
        mode="apply",
        status=JobStatus.RUNNING,
    )
    hist.heartbeat(other)
    assert hist.acquire_lease("acme", "sa-as01", other)
    transport = MockTransport()
    state = _factory(transport)
    result = _job(app_paths, _request(backup), transport, history=hist).run()
    assert result.status == DeviceStatus.DEVICE_BUSY
    assert state.sent == []


def test_save_can_be_left_off_and_ssh_keys_are_not_touched_on_aruba(app_paths):
    text = "hostname sa-cx01\ninterface 1/1/1\n    no shutdown\n"
    backup = _backup(app_paths, text)
    transport = MockTransport()
    state = _factory(transport)
    result = _job(
        app_paths, _request(backup, platform=Platform.AOS_CX, save=False), transport
    ).run()
    assert result.status == DeviceStatus.SUCCESS
    assert state.startup == [] and not result.saved
    assert not state.ssh_keys
    assert not any(cmd == "show crypto key mypubkey rsa" for _, cmd in state.sent)


def test_ssh_restore_needs_an_address_and_a_login(app_paths):
    backup = _backup(app_paths)
    with pytest.raises(TsunamiError, match="address"):
        _job(app_paths, _request(backup, method=ConnectionMethod.SSH), MockTransport())
    with pytest.raises(TsunamiError, match="login"):
        _job(
            app_paths,
            _request(backup, method=ConnectionMethod.SSH, host="192.0.2.10", credential=None),
            MockTransport(),
        )
    assert _request(backup, method=ConnectionMethod.SSH, host="192.0.2.10").where == (
        "SSH 192.0.2.10:22"
    )


def test_a_backup_of_another_device_is_refused(app_paths):
    other = _backup(app_paths, device="sa-as02")
    transport = MockTransport()
    state = _factory(transport)
    result = _job(app_paths, _request(other), transport).run()
    assert result.status == DeviceStatus.FAILED and "cannot read the backup" in result.summary
    assert state.sent == []


# -- NetmikoSession.paste_config -----------------------------------------------------------


class PasteConn:
    def __init__(self, reject=(), die_at=None, block_output=""):
        self.calls: list[tuple[list[str], dict]] = []
        self.reject, self.die_at, self.block_output = reject, die_at, block_output
        self.steps: list[str] = []

    def config_mode(self):
        self.steps.append("config")
        return ""

    def send_config_set(self, commands, **kwargs):
        from netmiko.exceptions import ConfigInvalidException

        self.calls.append((list(commands), kwargs))
        if self.die_at is not None and len(self.calls) > self.die_at:
            raise OSError("Socket is closed")
        if not kwargs.get("cmd_verify", True):
            return self.block_output
        if commands[0] in self.reject:
            raise ConfigInvalidException(
                f"Invalid input detected at command: {commands[0]}, matched error: "
                "% Invalid input detected at '^' marker."
            )
        return ""

    def exit_config_mode(self):
        self.steps.append("end")

    def set_base_prompt(self):
        self.steps.append("prompt")


def test_paste_goes_on_past_rejected_lines_and_relearns_the_prompt():
    conn = PasteConn(reject={"bad line"})
    session = NetmikoSession(conn, PlatformFamily.IOS, command_timeout=30)
    seen: list[int] = []
    result = session.paste_config(
        [ConfigChunk(("hostname new",)), ConfigChunk(("bad line",)), ConfigChunk(("ok",))],
        progress=seen.append,
    )
    assert result.sent == 3 and seen == [1, 2, 3]
    assert result.rejected == (("bad line", "% Invalid input detected at '^' marker."),)
    assert all(not kw["enter_config_mode"] and not kw["exit_config_mode"] for _, kw in conn.calls)
    assert conn.calls[0][1]["error_pattern"]  # one-line chunks are checked by Netmiko
    assert conn.steps == ["config", "end", "prompt"]


def test_paste_sends_a_block_without_echo_checks_and_scans_its_output():
    conn = PasteConn(block_output="banner motd #\n% Banner too long\nSW(config)#")
    session = NetmikoSession(conn, PlatformFamily.IOS, command_timeout=30)
    result = session.paste_config([ConfigChunk(("banner motd #", "x", "#"), block=True)])
    ((lines, kwargs),) = conn.calls
    assert lines == ["banner motd #", "x", "#"] and kwargs["cmd_verify"] is False
    assert result.rejected == (("banner motd #", "% Banner too long"),)
    unchecked = session.paste_config([ConfigChunk((KEYGEN,), block=True, check=False)])
    assert unchecked.rejected == ()


def test_paste_reports_a_session_that_dies_and_cancel():
    conn = PasteConn(die_at=1)
    session = NetmikoSession(conn, PlatformFamily.IOS, command_timeout=30)
    result = session.paste_config([ConfigChunk(("a",)), ConfigChunk(("b",)), ConfigChunk(("c",))])
    assert result.sent == 1 and result.error == "the session ended after 1 line (OSError)"
    assert conn.steps[-2:] == ["end", "prompt"]

    cancel = threading.Event()
    cancel.set()
    conn = PasteConn()
    result = NetmikoSession(conn, PlatformFamily.IOS, command_timeout=30).paste_config(
        [ConfigChunk(("a",))], cancel=cancel
    )
    assert result.cancelled and result.sent == 0 and conn.calls == []
