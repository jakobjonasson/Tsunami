"""Backup snapshots and rollback, per platform family.

Each strategy prepares before any change (a snapshot, and for the revert timer the
config-mode command that starts it), confirms after post-checks pass, and reverts on
failure. Commands marked *verify* in design.md, Platform drivers, are confirmed on a
device in work package 3.11.

preflight() runs the read-only part of prepare: on IOS it finds the default file system
(`show file systems`, the line marked *) and checks it has room for the snapshot. Check
mode runs it too, so a device that cannot take a rollback copy fails before anyone
approves a change. IOS file systems differ: flash:, bootflash:, flash-1: on a stack,
flash0: on some ISRs, unix: on IOL.

Errors quote the device's own error lines, never just its prompt.

See design.md, Execution engine: Device level; Platform drivers: Backup and rollback.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from tsunami.core.issues import TsunamiError
from tsunami.core.models.common import PlatformFamily
from tsunami.core.models.play import RollbackMode
from tsunami.core.transports.base import Session

CONFIRM_PROMPT = r"\[confirm\]|\[yes/no\]|\(y/n\)|\[y/n\]"
# Room to leave beyond the running config's own size on the file system.
_SNAPSHOT_MARGIN = 64 * 1024
_AOSS_MIN_VERSION = (16, 5)
_AOSS_MAX_CONFIG_FILES = 5


class RollbackError(TsunamiError):
    """Preparing, confirming or reverting a change failed. Messages are redacted."""


@dataclass(frozen=True)
class Prepared:
    mode: RollbackMode
    name: str  # snapshot / checkpoint / config file name
    config_mode_command: str | None = None  # starts the revert timer (IOS)


class RollbackStrategy:
    family: PlatformFamily

    def __init__(self, job_id: str) -> None:
        # AOS-CX checkpoint names are limited to 32 characters and cannot start with CPC.
        self.name = f"tsunami-{job_id.replace('-', '')[:12]}"

    def preflight(
        self,
        session: Session,
        mode: RollbackMode,
        version: str | None = None,
        config_size: int | None = None,
    ) -> None:
        """Read-only checks that prepare() would fail; raises RollbackError. No changes."""

    def prepare(
        self,
        session: Session,
        mode: RollbackMode,
        minutes: int,
        version: str | None = None,
        config_size: int | None = None,
    ) -> Prepared:
        raise NotImplementedError

    def cleanup(self, session: Session, prepared: Prepared) -> None:
        """Remove the snapshot once it is no longer needed: after the change is kept and
        saved, or after a successful revert. Raises RollbackError; callers treat a failure
        as a warning, never as a reason to roll back."""

    def confirm(self, session: Session, prepared: Prepared) -> None:
        """Keep the change: stop a revert timer. The snapshot stays until cleanup()."""
        raise NotImplementedError

    def revert(self, session: Session, prepared: Prepared) -> None:
        raise NotImplementedError


def _run(
    session: Session,
    command: str,
    *,
    prompt: str | None = None,
    answer: str | None = None,
    timeout: float | None = None,
    fail_on: str = r"^\s*%|Error|Invalid input",
) -> str:
    output = session.send_exec(command, expect_prompt=prompt, answer=answer, read_timeout=timeout)
    if re.search(fail_on, output.output, re.M):
        raise RollbackError(f"{command!r} failed: {device_error(output.output, fail_on)}")
    return output.output


def device_error(output: str, fail_on: str = r"^\s*%|Error|Invalid input") -> str:
    """The device's own error text from command output: the lines that match fail_on,
    else the last line that is not a prompt. Never just the prompt."""
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    errors = [line for line in lines if re.search(fail_on, line)]
    if not errors:
        errors = [line for line in lines if not _PROMPT.fullmatch(line)][-1:]
    text = " / ".join(errors[:3])
    return text[:300] if text else "no error text from the device"


_PROMPT = re.compile(r"\S+[#>]\s*")


def default_filesystem(output: str) -> tuple[str, int | None]:
    """The default file system from `show file systems`: the first prefix on the line
    marked *, and its free bytes if the device reports them."""
    for line in output.splitlines():
        match = re.match(r"\s*\*\s+(.*)", line)
        if not match:
            continue
        fields = match[1].split()
        prefixes = [f for f in fields if f.endswith(":")]
        if not prefixes:
            continue
        free = int(fields[1]) if len(fields) > 1 and fields[1].isdigit() else None
        if fields[0] == "0":
            free = None  # no reported size (IOL's unix:) says nothing about free space
        return prefixes[0], free
    raise RollbackError(
        "show file systems marks no default (*) file system: " + device_error(output)
    )


class IosRollback(RollbackStrategy):
    family = PlatformFamily.IOS

    def __init__(self, job_id: str) -> None:
        super().__init__(job_id)
        self.filesystem: str | None = None

    def preflight(self, session, mode, version=None, config_size=None):
        if mode != RollbackMode.ON_FAILURE:
            return  # the revert timer keeps its copy in the archive, not a file of ours
        output = session.send_exec("show file systems").output
        filesystem, free = default_filesystem(output)
        if free is not None and config_size is not None:
            need = config_size + _SNAPSHOT_MARGIN
            if free < need:
                raise RollbackError(
                    f"{filesystem} has {free} bytes free; the rollback copy needs about {need}"
                )
        self.filesystem = filesystem

    def prepare(self, session, mode, minutes, version=None, config_size=None):
        if mode == RollbackMode.ON_FAILURE:
            if self.filesystem is None:
                self.preflight(session, mode, version, config_size)
            file = f"{self.filesystem}{self.name}.cfg"
            _run(session, f"copy running-config {file}", prompt=r"Destination filename", answer="")
            return Prepared(mode, file)
        if mode == RollbackMode.REVERT_TIMER:
            # A configured archive reports the next file it will write.
            archive = session.send_exec("show archive").output
            if "next archive file will be named" not in archive.lower():
                raise RollbackError("revert_timer needs an archive path; show archive reports none")
            return Prepared(
                mode, self.name, config_mode_command=f"configure terminal revert timer {minutes}"
            )
        return Prepared(mode, "")

    def confirm(self, session, prepared):
        if prepared.mode == RollbackMode.REVERT_TIMER:
            _run(session, "configure confirm")

    def revert(self, session, prepared):
        if prepared.mode == RollbackMode.ON_FAILURE:
            _run(
                session,
                f"configure replace {prepared.name} force",
                timeout=300,
                fail_on=r"^\s*%|[Rr]ollback [Ff]ailed|[Ee]rror",
            )
        elif prepared.mode == RollbackMode.REVERT_TIMER:
            _run(session, "configure revert now", timeout=300)

    def cleanup(self, session, prepared):
        if prepared.mode == RollbackMode.ON_FAILURE:
            _run(session, f"delete /force {prepared.name}")


class AosCxRollback(RollbackStrategy):
    family = PlatformFamily.AOS_CX

    def prepare(self, session, mode, minutes, version=None, config_size=None):
        if mode == RollbackMode.NONE:
            return Prepared(mode, "")
        _run(session, f"copy running-config checkpoint {self.name}")
        if mode == RollbackMode.REVERT_TIMER:
            if minutes > 60:
                raise RollbackError("AOS-CX checkpoint auto accepts 1 to 60 minutes")
            _run(session, f"checkpoint auto {minutes}")
        return Prepared(mode, self.name)

    def confirm(self, session, prepared):
        if prepared.mode == RollbackMode.REVERT_TIMER:
            _run(session, "checkpoint auto confirm")

    def revert(self, session, prepared):
        if prepared.mode != RollbackMode.NONE:
            # *verify*: interaction with a still-running auto-checkpoint timer.
            _run(
                session,
                f"checkpoint rollback {prepared.name}",
                prompt=CONFIRM_PROMPT,
                answer="y",
                timeout=300,
            )

    def cleanup(self, session, prepared):
        if prepared.mode != RollbackMode.NONE:
            _run(session, f"erase checkpoint {prepared.name}", prompt=CONFIRM_PROMPT, answer="y")


class AosSRollback(RollbackStrategy):
    family = PlatformFamily.AOS_S

    def __init__(self, job_id: str) -> None:
        super().__init__(job_id)
        short = job_id.replace("-", "")[:12]
        self.job = f"tsunami_{short}"
        self.alias = f"tsunami_rb_{short}"

    def preflight(self, session, mode, version=None, config_size=None):
        if mode == RollbackMode.NONE:
            return
        if not _aoss_version_ok(version):
            raise RollbackError("rollback on AOS-S needs software 16.05 or later (cfg-restore)")
        files = session.send_exec("show config files").output
        if _count_config_files(files) >= _AOSS_MAX_CONFIG_FILES:
            raise RollbackError("no free config file slot for the rollback snapshot")

    def prepare(self, session, mode, minutes, version=None, config_size=None):
        if mode == RollbackMode.NONE:
            return Prepared(mode, "")
        self.preflight(session, mode, version, config_size)
        _run(session, f"cfg-backup running-config config {self.name}")
        if mode == RollbackMode.REVERT_TIMER:
            delay = f"{minutes // 60:02d}:{minutes % 60:02d}"
            _run(session, f'alias "{self.alias}" "cfg-restore flash {self.name}"')
            _run(session, f'job "{self.job}" delay {delay} "{self.alias}" count 1')
        return Prepared(mode, self.name)

    def confirm(self, session, prepared):
        if prepared.mode == RollbackMode.REVERT_TIMER:
            _run(session, f'no job "{self.job}"')
            _run(session, f'no alias "{self.alias}"')

    def revert(self, session, prepared):
        if prepared.mode == RollbackMode.NONE:
            return
        if prepared.mode == RollbackMode.REVERT_TIMER:
            _run(session, f'no job "{self.job}"')
        _run(
            session,
            f"cfg-restore flash {prepared.name}",
            prompt=CONFIRM_PROMPT,
            answer="y",
            timeout=300,
        )
        status = session.send_exec("show cfg-restore status").output
        if not re.search(r"Status\s*:\s*Success", status):
            raise RollbackError("cfg-restore did not report success")

    def cleanup(self, session, prepared):
        if prepared.mode != RollbackMode.NONE:
            _run(session, f"erase config {prepared.name}", prompt=CONFIRM_PROMPT, answer="y")


def strategy_for(family: PlatformFamily, job_id: str) -> RollbackStrategy:
    return {
        PlatformFamily.IOS: IosRollback,
        PlatformFamily.AOS_CX: AosCxRollback,
        PlatformFamily.AOS_S: AosSRollback,
    }[family](job_id)


def _aoss_version_ok(version: str | None) -> bool:
    match = re.search(r"[A-Z]{1,2}\.(\d{2})\.(\d{2})", version or "")
    return bool(match) and (int(match[1]), int(match[2])) >= _AOSS_MIN_VERSION


def _count_config_files(output: str) -> int:
    # `show config files` lists one numbered row per file (e.g. " 1 | *  * | config").
    return len(re.findall(r"^\s*\d+\s*\|", output, re.M))
