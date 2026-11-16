"""The firmware driver interface: what an upgrade play's firmware steps ask of a platform.

A driver does three things for one device, through an open session:

    inspect(job)   what the device runs now, and whether the image is already on it
    stage(job)     get the image onto the device and verify it; nothing reboots
    activate(job)  make the device boot the staged image, reload it, wait for it to
                   return, and confirm it runs the new version

stage and activate raise FirmwareError with words a person can act on. Everything a
driver logs or raises may quote the device, so the pipeline redacts it.

Rules every driver follows:
- stage is safe to repeat: an image already on the device and verified is not copied
  again, and a copy that fails verification is removed where the platform allows.
- stage never changes what the device boots next, and activate never transfers.
- activate refuses unless the image is on the device and verifies, so a boot variable is
  never pointed at a file that is missing or damaged.
- activate saves the configuration before the reload, so the questions a reload asks
  about unsaved changes never decide anything.
- After the reload activate calls job.reconnect(), which waits for the device and puts
  the new session in job.session, and then checks the running version (and on a stack,
  that every member that was there before is back).

See design.md, Firmware upgrades, and docs/research/firmware.md for the vendor commands.
"""

from __future__ import annotations

import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from tsunami.core.issues import TsunamiError
from tsunami.core.models.common import Platform, PlatformFamily
from tsunami.core.models.play import TransferMethod
from tsunami.core.transports.base import Session


class FirmwareError(TsunamiError):
    """A firmware step failed. The text may quote the device: redact it."""


class DeviceGone(FirmwareError):
    """The device did not come back after its reload in the time allowed."""


@dataclass(frozen=True)
class ImageFile:
    """The image a device gets: the play's entry, plus what the firmware folder knows."""

    file: str
    version: str
    # Hashes of the file: the play's, and when the file is in the firmware folder every
    # one Tsunami can compute (md5, sha256, sha512), checked against the play's.
    hashes: dict[str, str] = field(default_factory=dict)
    size: int | None = None  # bytes; None when the file is not in the firmware folder
    local: Path | None = None  # the file in the firmware folder, if it is there

    def hash(self, name: str) -> str | None:
        return self.hashes.get(name)


@dataclass(frozen=True)
class ImageState:
    """What inspect found."""

    version: str | None  # the running version, as the device prints it
    staged: bool  # the image is on the device (not yet verified: stage and activate do)
    detail: str  # one line for the job log and for check mode
    free_bytes: int | None = None  # where the image goes; None when the platform has slots
    members: int = 1  # stack members


@dataclass
class FirmwareJob:
    """One device's firmware work. Built by the pipeline; tests build their own."""

    platform: Platform
    session: Session  # replaced by reconnect() after a reload
    image: ImageFile
    transfer: TransferMethod
    server: str | None  # tftp and http: the play's server URL, without a trailing slash
    vrf: str | None  # the VRF the device reaches the server through, where a command asks
    log: Callable[[str], None]
    # scp: copy a local file to the device as a remote path. None when the transport
    # cannot upload. progress gets (bytes sent, total).
    upload: Callable[[Path, str, Callable[[int, int], None] | None], None] | None
    # Close the session, wait for the device to return, and open a new one (which becomes
    # job.session). Raises DeviceGone at the play's reload_timeout.
    reconnect: Callable[[], Session]
    cancel: threading.Event = field(default_factory=threading.Event)
    command_timeout: float = 60
    reload_timeout: float = 1800
    # Waits between polls go through here, so tests do not sleep.
    sleep: Callable[[float], None] = time.sleep

    def progress_logger(self, what: str) -> Callable[[int, int], None]:
        """A progress callback that logs every tenth of the way."""
        last = [-1]

        def report(sent: int, total: int) -> None:
            tenth = (sent * 10 // total) if total else 10
            if tenth > last[0]:
                last[0] = tenth
                self.log(f"{what}: {tenth * 10}% ({sent:,} of {total:,} bytes)")

        return report

    def check_cancel(self) -> None:
        if self.cancel.is_set():
            raise FirmwareError("cancelled")


class FirmwareDriver(Protocol):
    family: PlatformFamily
    # How an image can reach this platform. A play whose transfer is not listed fails
    # validation for these targets.
    transfers: frozenset[TransferMethod]
    # The hash the device itself can compute and compare ("md5"), or None when the
    # platform only checks the image's signature. A pull transfer (tftp, http) of a file
    # that is not in the firmware folder needs this hash in the play.
    device_hash: str | None

    def inspect(self, job: FirmwareJob) -> ImageState: ...

    def stage(self, job: FirmwareJob) -> None: ...

    def activate(self, job: FirmwareJob) -> None: ...


_NUMBER = re.compile(r"\d+|[a-z]+", re.I)


def version_key(version: str | None) -> tuple[Any, ...]:
    """A version in a form two spellings of it share: 17.09.05 and 17.9.5, 16.9.4 and
    16.09.04a differ only where they really differ."""
    parts: list[Any] = []
    for token in _NUMBER.findall(version or ""):
        parts.append(int(token) if token.isdigit() else token.lower())
    return tuple(parts)


def same_version(a: str | None, b: str | None) -> bool:
    key = version_key(a)
    return bool(key) and key == version_key(b)


def server_url(job: FirmwareJob) -> str:
    """The image's URL on the play's server."""
    if not job.server:
        raise FirmwareError(f"transfer {job.transfer} needs a server in the play")
    return f"{job.server}/{job.image.file}"
