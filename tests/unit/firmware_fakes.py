"""Doubles for firmware driver tests: a session that answers commands from a script, and a
FirmwareJob built around it.

A driver test scripts what the device prints for each command the driver sends, runs
inspect, stage or activate, and then asserts on what was sent, in which order:

    session = ScriptedSession()
    session.on(r"^show version$", SHOW_VERSION)
    session.on(r"^dir flash:$", DIR_OUTPUT)
    job = make_job(session, image=image("x.bin", "17.9.5", md5="..."))
    DRIVER.stage(job)
    assert session.commands == [...]

Rules added later win over earlier ones, so a test can start from a base script and
override one command. A reply can be a function of the command, which lets a device
change its answer after something happened (the file is there after the copy).
"""

from __future__ import annotations

import re
import threading
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from tsunami.core.firmware.base import DeviceGone, FirmwareJob, ImageFile
from tsunami.core.models.common import Platform
from tsunami.core.models.play import TransferMethod
from tsunami.core.transports.base import ConfigResult, ExecResult

Reply = str | Callable[[str], str]


class ScriptedSession:
    """Satisfies Session for the calls a firmware driver makes."""

    def __init__(self, rules: Sequence[tuple[str, Reply]] = (), *, default: str = "") -> None:
        self._rules: list[tuple[re.Pattern[str], Reply]] = []
        for pattern, reply in rules:
            self.on(pattern, reply)
        self.default = default
        self.commands: list[str] = []  # every exec and interactive command, in order
        self.config: list[list[str]] = []  # every send_config call's lines
        # Every send_interactive call: (command, answers, may_disconnect).
        self.interactive: list[tuple[str, tuple[tuple[str, str], ...], bool]] = []
        self.timeouts: dict[str, float | None] = {}  # command -> read_timeout it was sent with
        self.disconnect_on: list[str] = []  # regexes: an interactive command that ends the session
        self.closed = False
        self.config_error: str | None = None  # send_config fails with this line rejected

    def on(self, pattern: str, reply: Reply) -> ScriptedSession:
        self._rules.insert(0, (re.compile(pattern), reply))
        return self

    def _reply(self, command: str) -> str:
        if self.closed:
            raise OSError("session closed")
        self.commands.append(command)
        for pattern, reply in self._rules:
            if pattern.search(command):
                return reply(command) if callable(reply) else reply
        return self.default

    def send_exec(
        self,
        command: str,
        *,
        expect_prompt: str | None = None,
        answer: str | None = None,
        read_timeout: float | None = None,
    ) -> ExecResult:
        self.timeouts[command] = read_timeout
        return ExecResult(command, self._reply(command))

    def send_interactive(
        self,
        command: str,
        answers: Sequence[tuple[str, str]] = (),
        *,
        read_timeout: float | None = None,
        may_disconnect: bool = False,
    ) -> ExecResult:
        self.timeouts[command] = read_timeout
        self.interactive.append((command, tuple(answers), may_disconnect))
        if any(re.search(pattern, command) for pattern in self.disconnect_on):
            self.commands.append(command)
            self.closed = True
            if not may_disconnect:
                raise OSError("connection lost")
            return ExecResult(command, "", disconnected=True)
        return ExecResult(command, self._reply(command))

    def send_config(
        self, lines: list[str], *, config_mode_command: str | None = None
    ) -> ConfigResult:
        if self.closed:
            raise OSError("session closed")
        self.config.append(list(lines))
        if self.config_error is not None and self.config_error in lines:
            return ConfigResult(output="% Invalid input", failed_line=self.config_error)
        return ConfigResult(output="")

    def paste_config(self, chunks: Any, *, cancel: Any = None, progress: Any = None) -> Any:
        raise NotImplementedError

    def running_config(self) -> str:
        return self._reply("show running-config")

    def close(self) -> None:
        self.closed = True


def image(
    file: str = "image.bin",
    version: str = "17.09.05",
    *,
    size: int | None = 1000,
    local: Path | None = None,
    **hashes: str,
) -> ImageFile:
    return ImageFile(file, version, dict(hashes), size, local)


class JobLog:
    """What a job logged and uploaded, and the sessions it was given after reloads."""

    def __init__(self) -> None:
        self.lines: list[str] = []
        self.uploads: list[tuple[Path, str]] = []
        self.reconnects = 0
        self.slept: list[float] = []

    def text(self) -> str:
        return "\n".join(self.lines)


def make_job(
    session: ScriptedSession,
    *,
    image: ImageFile,
    platform: Platform = Platform.IOS_XE,
    transfer: TransferMethod = TransferMethod.SCP,
    server: str | None = None,
    vrf: str | None = None,
    after_reload: ScriptedSession | None = None,
    upload: bool = True,
    on_upload: Callable[[Path, str], None] | None = None,
    reload_timeout: float = 1800,
) -> tuple[FirmwareJob, JobLog]:
    """A FirmwareJob over a scripted session. after_reload is the session reconnect()
    hands out (the device after its reload); without it reconnect() raises DeviceGone.
    on_upload runs when the driver uploads, so a test can make the file appear."""
    record = JobLog()

    def do_upload(local: Path, remote: str, progress: Any = None) -> None:
        record.uploads.append((local, remote))
        if on_upload is not None:
            on_upload(local, remote)
        if progress is not None:
            progress(1000, 1000)

    def reconnect() -> ScriptedSession:
        record.reconnects += 1
        job.session.close()
        if after_reload is None:
            raise DeviceGone("the device did not come back within 30 minutes")
        job.session = after_reload
        return after_reload

    job = FirmwareJob(
        platform=platform,
        session=session,
        image=image,
        transfer=transfer,
        server=server,
        vrf=vrf,
        log=record.lines.append,
        upload=do_upload if upload else None,
        reconnect=reconnect,
        cancel=threading.Event(),
        command_timeout=60,
        reload_timeout=reload_timeout,
        sleep=record.slept.append,
    )
    return job, record
