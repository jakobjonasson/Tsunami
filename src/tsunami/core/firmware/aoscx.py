"""Firmware driver for HPE Aruba AOS-CX (6000 to 6300 series, VSF stacks).

The switch keeps two images, primary and secondary. The new image is written to the bank
the switch is NOT running from, so the running image stays in place as the way back, and
then that bank is booted:

    stage      copy <tftp url> <bank>; read the bank's version back; ask the switch to
               verify the image's signature
    activate   write memory; boot system <bank>; wait for the switch; confirm that it runs
               the target bank and the new version; on a VSF stack, that every member is back

The switch cannot compute a hash of an image, so there is no device hash: it checks the
image's signature itself when it takes the file and at every boot, and Tsunami confirms by
reading the version in the bank. The 6000 to 6300 have no documented way to take an
upload, so the image is fetched over TFTP. Commands and output come from
docs/research/firmware.md, section C; what that file marks UNVERIFIED is marked here too,
and an output this code does not understand fails the step with the device's words rather
than being guessed at.

See design.md, Firmware upgrades.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from tsunami.core.firmware.base import (
    FirmwareError,
    FirmwareJob,
    ImageState,
    same_version,
    server_url,
)
from tsunami.core.models.common import PlatformFamily
from tsunami.core.models.play import TransferMethod

COPY_SECONDS = 3600.0
VERIFY_SECONDS = 300.0
RELOAD_PROMPT_SECONDS = 120.0
MEMBER_POLLS, MEMBER_POLL_SECONDS = 20, 30.0  # ten minutes for a stack to form again
BANKS = ("primary", "secondary")

# AOS-CX Primary Image
# Version : FL.10.10.1010
# SHA-256 : 4c84e49c...
_BANK = re.compile(r"^[ \t]*AOS-CX (?P<bank>Primary|Secondary) Image[ \t]*$", re.M | re.I)
_VERSION = re.compile(r"^[ \t]*Version[ \t]*:[ \t]*(?P<version>\S+)[ \t]*$", re.M)
_SHA256 = re.compile(r"^[ \t]*SHA-256[ \t]*:[ \t]*(?P<sha>[0-9a-fA-F]{64})\b", re.M)
_ACTIVE = re.compile(r"^[ \t]*Active Image[ \t]*:[ \t]*(?P<bank>\S+)", re.M | re.I)
_DEFAULT = re.compile(r"^[ \t]*Default Image[ \t]*:[ \t]*(?P<bank>\S+)", re.M | re.I)
# Mbr ID  Mac Address        Type    Status
# 1       aa:bb:cc:00:11:22  JL668A  Conductor
_MEMBER = re.compile(
    r"^[ \t]*(?P<id>\d+)[ \t]+(?P<mac>[0-9a-f]{2}(?::[0-9a-f]{2}){5})[ \t]+\S+[ \t]+"
    r"(?P<status>\S.*?)[ \t]*$",
    re.M | re.I,
)
_REJECTED = re.compile(
    r"^\s*(?:% ?)?(?:Unknown command|Invalid input|Incomplete command|Ambiguous command)"
    r"|^\s*%",
    re.M | re.I,
)
_FAILED = re.compile(r"\b(?:fail(?:ed|ure)?|error)\b", re.I)
_VALID = re.compile(r"\bis valid\b", re.I)
_NOT_VALID = re.compile(r"\b(?:not valid|invalid)\b|does not contain", re.I)
_COPY_ANSWERS = ((r"Continue \(y/n\)\?", "y"),)
# The save question comes first so that it wins: the configuration was just saved.
_BOOT_ANSWERS = (
    (r"save the current configuration \(y/n\)\?", "n"),
    (r"Continue \(y/n\)\?", "y"),
)


def _tail(output: str, count: int = 4) -> str:
    """The last lines of device output, for a message that quotes the device."""
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    text = " / ".join(lines[-count:])
    return text[:400] if text else "no output from the device"


@dataclass(frozen=True)
class _Images:
    versions: dict[str, str | None]  # bank -> version, for both banks
    hashes: dict[str, str | None]  # bank -> SHA-256 as `show images` prints it
    active: str  # the bank the switch is running from
    default: str | None  # the bank it boots next

    @property
    def target(self) -> str:
        """The bank that is not running: where the new image goes."""
        return BANKS[1] if self.active == BANKS[0] else BANKS[0]

    @property
    def running(self) -> str | None:
        return self.versions[self.active]


class ArubaCxFirmware:
    family = PlatformFamily.AOS_CX
    # The 6000 to 6300 have no documented way to take an upload (research C.3).
    transfers = frozenset({TransferMethod.TFTP})
    device_hash = None  # the switch checks the image's signature itself

    # -- reading the device ------------------------------------------------------------

    def _images(self, job: FirmwareJob) -> _Images:
        output = job.session.send_exec("show images").output
        headers = list(_BANK.finditer(output))
        versions: dict[str, str | None] = {}
        hashes: dict[str, str | None] = {}
        for index, header in enumerate(headers):
            end = headers[index + 1].start() if index + 1 < len(headers) else len(output)
            text = output[header.end() : end]
            bank = header["bank"].lower()
            found = _VERSION.search(text)
            version = found["version"] if found else None
            sha = _SHA256.search(text)
            if bank in versions and versions[bank] != version:
                raise FirmwareError(
                    f"`show images` lists the {bank} image twice with different versions, so "
                    f"Tsunami cannot tell which is meant. The device says: {_tail(output)}"
                )
            versions[bank] = version
            hashes[bank] = sha["sha"].lower() if sha else None
        actives = {m["bank"].lower() for m in _ACTIVE.finditer(output)}
        if set(versions) != set(BANKS) or len(actives) != 1 or not actives <= set(BANKS):
            raise FirmwareError(
                "`show images` does not show a primary and a secondary image and one active "
                "image, so Tsunami cannot tell which bank the switch runs from and will not "
                f"guess. The device says: {_tail(output)}"
            )
        default = _DEFAULT.search(output)
        return _Images(
            versions, hashes, actives.pop(), default["bank"].lower() if default else None
        )

    def _members(self, job: FirmwareJob) -> list[tuple[str, str]]:
        """VSF members as (id, status). Empty on a switch that rejects `show vsf` or prints
        no member table: that switch is one device."""
        output = job.session.send_exec("show vsf").output
        if _REJECTED.search(output):
            return []
        return [(m["id"], m["status"]) for m in _MEMBER.finditer(output)]

    @staticmethod
    def _present(members: list[tuple[str, str]]) -> list[tuple[str, str]]:
        return [m for m in members if m[1].lower() != "not present"]

    def inspect(self, job: FirmwareJob) -> ImageState:
        images = self._images(job)
        members = max(1, len(self._present(self._members(job))))
        target = images.target
        staged = same_version(images.versions[target], job.image.version)
        stack = f"; a VSF stack of {members}" if members > 1 else ""
        detail = (
            f"runs {images.running or 'an unknown version'} from {images.active}; "
            f"{target} holds {images.versions[target] or 'no readable version'}{stack}"
        )
        return ImageState(images.running, staged, detail, None, members)

    # -- stage -------------------------------------------------------------------------

    def stage(self, job: FirmwareJob) -> None:
        images = self._images(job)
        bank = images.target
        if same_version(images.versions[bank], job.image.version):
            job.log(f"the {bank} image already holds {job.image.version}; not copying it again")
        else:
            job.check_cancel()
            self._copy_in(job, bank, images.versions[bank])
        job.check_cancel()
        self._verify(job, bank)

    def _copy_in(self, job: FirmwareJob, bank: str, old: str | None) -> None:
        command = f"copy {server_url(job)} {bank}"
        if job.vrf:
            command += f" vrf {job.vrf}"
        job.log(f"the switch is fetching the image into {bank} (replacing {old or 'its content'})")
        result = job.session.send_interactive(command, _COPY_ANSWERS, read_timeout=COPY_SECONDS)
        after = self._images(job)
        if not same_version(after.versions[bank], job.image.version):
            raise FirmwareError(
                f"after the copy the {bank} image holds "
                f"{after.versions[bank] or 'no readable version'}, not {job.image.version}. "
                f"The running {after.active} image was not touched. The switch said: "
                f"{_tail(result.output)}"
            )
        job.log(f"the {bank} image now holds {job.image.version}")
        wanted, found = job.image.hash("sha256"), after.hashes[bank]
        if wanted and found:
            # UNVERIFIED (research C.4): that `show images` computes its SHA-256 over the
            # same bytes as sha256sum of the .swi. A difference is logged, never fatal.
            if wanted.lower() == found:
                job.log(f"the {bank} image's SHA-256 matches the play's")
            else:
                job.log(
                    f"the {bank} image's SHA-256 differs from the play's ({found}); the switch "
                    "may compute it over other bytes, so this does not fail the step"
                )

    def _verify(self, job: FirmwareJob, bank: str) -> None:
        output = job.session.send_exec(
            f"show images verify {bank}", read_timeout=max(VERIFY_SECONDS, job.command_timeout)
        ).output
        # UNVERIFIED (research C.4): the command and its words (`The primary image is
        # valid`). A switch that does not know the command is not failed for that: it
        # checked the signature itself while taking the file.
        if _REJECTED.search(output):
            job.log(f"`show images verify {bank}` is not available; relying on the version read")
            return
        if _NOT_VALID.search(output) or not _VALID.search(output):
            raise FirmwareError(
                f"the switch does not say the {bank} image is valid. It said: {_tail(output)}. "
                f"The {bank} image was not made the boot image; run the stage step again"
            )
        job.log(f"the switch says the {bank} image is valid")

    # -- activate ----------------------------------------------------------------------

    def activate(self, job: FirmwareJob) -> None:
        images = self._images(job)
        bank = images.target
        if not same_version(images.versions[bank], job.image.version):
            raise FirmwareError(
                f"the {bank} image holds {images.versions[bank] or 'no readable version'}, not "
                f"{job.image.version}: run the stage step first. Nothing was changed"
            )
        self._verify(job, bank)
        before = len(self._present(self._members(job)))
        job.check_cancel()
        saved = job.session.send_exec("write memory").output
        if _REJECTED.search(saved) or _FAILED.search(saved):
            raise FirmwareError(
                f"`write memory` failed, so the switch was not rebooted: {_tail(saved)}"
            )
        job.log(f"configuration saved; booting the {bank} image (the switch reloads)")
        result = job.session.send_interactive(
            f"boot system {bank}",
            _BOOT_ANSWERS,
            read_timeout=RELOAD_PROMPT_SECONDS,
            may_disconnect=True,
        )
        if "reboot aborted" in result.output.lower():
            raise FirmwareError(
                f"the switch aborted the reboot, so it still runs {images.active}: "
                f"{_tail(result.output)}"
            )
        if not result.disconnected and _REJECTED.search(result.output):
            raise FirmwareError(
                f"the switch rejected `boot system {bank}`, so it was not rebooted: "
                f"{_tail(result.output)}"
            )
        job.reconnect()
        after = self._images(job)
        if after.active != bank or not same_version(after.running, job.image.version):
            raise FirmwareError(
                f"the switch came back running {after.running or 'an unknown version'} from "
                f"{after.active}, not {job.image.version} from {bank}. It is reachable; check "
                "`show images` before trying again. The old image is still in the other bank"
            )
        job.log(f"the switch runs {after.running} from {bank}")
        if before > 1:
            self._wait_for_members(job, before)

    def _wait_for_members(self, job: FirmwareJob, before: int) -> None:
        members: list[tuple[str, str]] = []
        for attempt in range(MEMBER_POLLS):
            members = self._members(job)
            ready = [m for m in members if m[1].lower() not in ("booting", "not present")]
            if len(ready) >= before:
                job.log(f"all {before} VSF members are back")
                return
            if attempt < MEMBER_POLLS - 1:
                job.check_cancel()
                job.sleep(MEMBER_POLL_SECONDS)
        waiting = [
            f"member {number} is {status}"
            for number, status in members
            if status.lower() in ("booting", "not present")
        ]
        if len(members) < before:
            waiting.append(f"{before - len(members)} of {before} members are not listed")
        raise FirmwareError(
            "the VSF stack did not form again after the reload (" + "; ".join(waiting) + "). "
            "The switch runs the new image; check `show vsf` before going on"
        )


DRIVER = ArubaCxFirmware()
