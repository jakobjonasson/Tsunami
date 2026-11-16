"""What firmware steps ask of the transports: a command that asks several questions and
may end the session (a reload), and an SCP upload that only goes to a pinned host key.
See design.md, Firmware upgrades: Transfer; Local data and audit: known_hosts."""

from __future__ import annotations

import paramiko
import pytest
import scp
from tests.unit.test_ssh_transport import CRED, KEY_A, KEY_B, target

from tsunami.core.models.common import PlatformFamily
from tsunami.core.secret_store import LoginCredential
from tsunami.core.transports import netmiko_session
from tsunami.core.transports.base import (
    AuthenticationError,
    HostKeyMismatchError,
    JumpTarget,
    UnreachableError,
    UploadError,
)
from tsunami.core.transports.hostkeys import HostKeyStore
from tsunami.core.transports.netmiko_session import NetmikoSession
from tsunami.core.transports.ssh import SshTransport

# -- send_interactive -------------------------------------------------------------------


class Channel:
    """A Netmiko connection double: read_channel hands out scripted chunks. A chunk can be
    an exception (the session dropped), and each written line is recorded."""

    def __init__(self, chunks, *, prompt="sa-as01", alive=True):
        self.base_prompt = prompt
        self.chunks = list(chunks)
        self.written: list[str] = []
        self.alive = alive
        self.remote_conn = None

    def write_channel(self, data: str) -> None:
        self.written.append(data)

    def read_channel(self) -> str:
        if not self.chunks:
            return ""
        chunk = self.chunks.pop(0)
        if isinstance(chunk, BaseException):
            raise chunk
        return chunk

    def is_alive(self) -> bool:
        return self.alive


@pytest.fixture(autouse=True)
def no_pauses(monkeypatch):
    monkeypatch.setattr(netmiko_session, "_READ_PAUSE", 0)
    monkeypatch.setattr(netmiko_session, "_ALIVE_AFTER", 0)


def session(conn) -> NetmikoSession:
    return NetmikoSession(conn, PlatformFamily.IOS, command_timeout=5)


RELOAD = [(r"Save\? \[yes/no\]", "no"), (r"\[confirm\]", "")]


def test_each_question_is_answered_once_and_the_prompt_ends_it():
    conn = Channel(
        [
            "copy tftp://192.0.2.50/x.bin flash:\n",
            "Destination filename [x.bin]? ",
            "\nAccessing tftp://192.0.2.50/x.bin...\n!!!!\n",
            "[OK - 1000 bytes]\n\nsa-as01#",
        ]
    )
    result = session(conn).send_interactive(
        "copy tftp://192.0.2.50/x.bin flash:", [(r"Destination filename \[", "")]
    )
    assert conn.written == ["copy tftp://192.0.2.50/x.bin flash:\n", "\n"]
    assert "[OK - 1000 bytes]" in result.output and not result.disconnected


def test_a_reload_answers_both_questions_and_the_drop_is_a_result():
    conn = Channel(
        [
            "reload\n\nSystem configuration has been modified. Save? [yes/no]: ",
            "no\nProceed with reload? [confirm]",
            OSError("Socket is closed"),
        ]
    )
    result = session(conn).send_interactive("reload", RELOAD, may_disconnect=True)
    assert conn.written == ["reload\n", "no\n", "\n"]
    assert result.disconnected and "Proceed with reload" in result.output


def test_a_question_seen_twice_in_new_output_is_answered_twice():
    conn = Channel(["Continue (y/n)? ", "y\nContinue (y/n)? ", "y\nsa-as01#"])
    session(conn).send_interactive("boot system secondary", [(r"\(y/n\)\?", "y")])
    assert conn.written == ["boot system secondary\n", "y\n", "y\n"]


def test_a_silent_dead_session_counts_as_disconnected_only_when_allowed():
    conn = Channel(["reload\nProceed with reload? [confirm]"], alive=False)
    result = session(conn).send_interactive("reload", RELOAD, may_disconnect=True)
    assert result.disconnected

    dropped = Channel(["show version\n", OSError("Socket is closed")])
    with pytest.raises(OSError, match="Socket is closed"):
        session(dropped).send_interactive("show version")


def test_a_command_that_never_finishes_times_out(monkeypatch):
    ticks = iter(range(0, 1000, 2))
    monkeypatch.setattr(netmiko_session.time, "monotonic", lambda: next(ticks))
    conn = Channel(["verify /md5 flash:x.bin\n....."])
    with pytest.raises(TimeoutError, match="still running after 5 s"):
        session(conn).send_interactive("verify /md5 flash:x.bin")


def test_a_prompt_inside_the_output_does_not_end_the_command():
    conn = Channel(["show x\nsa-as01# is how the prompt looks\nmore\n", "sa-as01#"])
    result = session(conn).send_interactive("show x")
    assert result.output.endswith("sa-as01#") and "more" in result.output


# -- upload ---------------------------------------------------------------------------------


class Ssh:
    """A paramiko.SSHClient double that keeps what connect() was given."""

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.keys = paramiko.HostKeys()
        self.policy = None
        self.kwargs: dict = {}
        self.closed = False

    def get_host_keys(self):
        return self.keys

    def set_missing_host_key_policy(self, policy) -> None:
        self.policy = policy

    def connect(self, host, **kwargs) -> None:
        self.kwargs = {"host": host, **kwargs}
        if self.error is not None:
            raise self.error

    def get_transport(self):
        return "transport"

    def close(self) -> None:
        self.closed = True


class Scp:
    def __init__(self, transport, *, socket_timeout, progress, error=None, size=4000):
        self.args = (transport, socket_timeout)
        self.progress = progress
        self.error = error
        self.size = size
        self.put_args = None
        self.closed = False

    def put(self, local, remote) -> None:
        self.put_args = (local, remote)
        for sent in (1000, self.size):
            self.progress(b"x.bin", self.size, sent)
        if self.error is not None:
            raise self.error

    def close(self) -> None:
        self.closed = True


def uploader(app_paths, *, key=KEY_A, known=KEY_A, ssh=None, scp_error=None, probes=None):
    ssh = ssh or Ssh()
    made: list[Scp] = []

    def scp_factory(transport, **kwargs):
        made.append(Scp(transport, error=scp_error, **kwargs))
        return made[-1]

    def probe(opener, **kwargs):
        if probes is not None:
            probes.append(kwargs)
        return key

    transport = SshTransport(app_paths, probe=probe, ssh_client=lambda: ssh, scp_client=scp_factory)
    if known is not None:
        HostKeyStore(app_paths.known_hosts_file("acme")).check("203.0.113.11", 22, known)
    return transport, ssh, made


@pytest.fixture
def image(tmp_path):
    path = tmp_path / "x.bin"
    path.write_bytes(b"0" * 4000)
    return path


def test_upload_logs_in_with_the_pinned_key_and_copies_the_file(app_paths, image):
    transport, ssh, made = uploader(app_paths)
    seen: list[tuple[int, int]] = []
    transport.upload(target(), image, "flash:/x.bin", progress=lambda s, t: seen.append((s, t)))

    assert ssh.kwargs["host"] == "203.0.113.11"
    assert (ssh.kwargs["username"], ssh.kwargs["password"]) == (CRED.username, CRED.password)
    assert ssh.kwargs["allow_agent"] is False and ssh.kwargs["look_for_keys"] is False
    assert isinstance(ssh.policy, paramiko.RejectPolicy)
    assert ssh.keys.lookup("203.0.113.11") is not None  # the client checks the same key
    assert made[0].put_args == (str(image), "flash:/x.bin")
    assert seen == [(1000, 4000), (4000, 4000)]
    assert ssh.closed and made[0].closed


def test_upload_never_sends_a_login_to_an_unknown_or_changed_key(app_paths, image):
    transport, ssh, made = uploader(app_paths, known=None)
    with pytest.raises(HostKeyMismatchError):
        transport.upload(target(), image, "flash:/x.bin")
    assert ssh.kwargs == {} and not made  # no login, no copy
    # An upload never records a key either: the session's own check does that.
    assert HostKeyStore(app_paths.known_hosts_file("acme")).known_types("203.0.113.11", 22) == []

    transport, ssh, made = uploader(app_paths, key=KEY_B, known=KEY_A)
    with pytest.raises(HostKeyMismatchError):
        transport.upload(target(), image, "flash:/x.bin")
    assert ssh.kwargs == {} and not made


def test_upload_maps_connection_failures(app_paths, image):
    for error, expected in (
        (paramiko.AuthenticationException(), AuthenticationError),
        (paramiko.SSHException("no"), UnreachableError),
        (paramiko.BadHostKeyException("h", KEY_B, KEY_A), HostKeyMismatchError),
    ):
        transport, ssh, _ = uploader(app_paths, ssh=Ssh(error))
        with pytest.raises(expected):
            transport.upload(target(), image, "flash:/x.bin")
        assert ssh.closed


def test_upload_needs_a_login_with_a_password(app_paths, image):
    transport, ssh, _ = uploader(app_paths)
    with pytest.raises(AuthenticationError, match="needs a login with a password"):
        transport.upload(target(credential=LoginCredential("manager", "")), image, "/os/primary")
    assert ssh.kwargs == {}


def test_the_devices_refusal_is_reported_without_the_login(app_paths, image):
    refusal = scp.SCPException("Administratively disabled.\n")
    transport, ssh, _ = uploader(app_paths, scp_error=refusal)
    with pytest.raises(UploadError) as raised:
        transport.upload(target(), image, "flash:/x.bin")
    text = str(raised.value)
    assert "the device refused the copy: Administratively disabled." in text
    assert CRED.password not in text and ssh.closed

    transport, _, _ = uploader(app_paths, scp_error=EOFError())
    with pytest.raises(UploadError, match="the upload stopped \\(EOFError\\)"):
        transport.upload(target(), image, "flash:/x.bin")


def test_cancel_stops_an_upload_part_way(app_paths, image):
    import threading

    cancel = threading.Event()
    transport, ssh, made = uploader(app_paths)

    def progress(sent: int, total: int) -> None:
        cancel.set()

    with pytest.raises(UploadError, match="cancelled part way"):
        transport.upload(target(), image, "flash:/x.bin", progress=progress, cancel=cancel)
    assert made[0].closed and ssh.closed


def test_upload_goes_through_the_jump_host_and_frees_its_slot(app_paths, image):
    class Jump:
        def __init__(self):
            self.opened: list[tuple[str, int]] = []
            self.held = 0

        def acquire(self):
            self.held += 1

        def release(self):
            self.held -= 1

        def channel(self, host, port):
            self.opened.append((host, port))
            return f"channel-{len(self.opened)}"

        def close(self):
            pass

    jump = Jump()
    ssh = Ssh()
    transport = SshTransport(
        app_paths,
        probe=lambda opener, **kw: (opener(), KEY_A)[1],
        jump_factory=lambda t, store: jump,
        ssh_client=lambda: ssh,
        scp_client=lambda transport, **kw: Scp(transport, **kw),
    )
    HostKeyStore(app_paths.known_hosts_file("acme")).check("203.0.113.11", 22, KEY_A)
    via = target(jump=JumpTarget("198.51.100.5", 22, CRED))
    transport.upload(via, image, "flash:/x.bin")

    # One channel for the key probe, one for the login; never a direct socket.
    assert jump.opened == [("203.0.113.11", 22), ("203.0.113.11", 22)]
    assert ssh.kwargs["sock"] == "channel-2"
    assert jump.held == 0
