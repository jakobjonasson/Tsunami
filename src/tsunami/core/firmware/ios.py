"""Firmware driver for Cisco IOS and IOS-XE.

Two ways a device boots decide how it is upgraded:

- Bundle mode (IOS classic, routers, IOS-XE booted from a .bin): the boot variable names
  an image file. Activate points it at the new file, keeps the running image as the
  second entry, reads the variable back, saves, and reloads. On a stack every member
  boots from its own flash, so stage copies the file to each member and verifies it there.
- Install mode (IOS-XE booted from packages.conf): `install add file ... activate commit`
  unpacks the image, copies it to every stack member and reloads by itself.

The image is verified with `verify /md5` after it is copied and again before anything
changes what the device boots. The commands and their output come from
docs/research/firmware.md; what that file marks UNVERIFIED is marked here too, and in
each such place an answer this code does not understand fails the step with the device's
words rather than being guessed at.

See design.md, Firmware upgrades.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from tsunami.core.facts import parse_facts
from tsunami.core.firmware.base import (
    FirmwareError,
    FirmwareJob,
    ImageState,
    same_version,
    server_url,
)
from tsunami.core.models.common import PlatformFamily
from tsunami.core.models.play import TransferMethod
from tsunami.core.rollback import device_error
from tsunami.core.state.parsers.ios import stack as parse_stack

INSTALL, BUNDLE = "install", "bundle"
VERIFY_SECONDS = 900.0  # verify /md5 of a large image on a slow switch takes minutes
COPY_SECONDS = 3600.0
RELOAD_PROMPT_SECONDS = 120.0
MEMBER_POLLS, MEMBER_POLL_SECONDS = 20, 30.0  # ten minutes for a stack to form again

_IMAGE_FILE = re.compile(r'^System image file is "(?P<fs>[\w-]+:)/?(?P<path>[^"]*)"', re.M)
# Switch Ports Model              SW Version        SW Image              Mode
# *    1 56    C9300-48P          17.06.04          CAT9K_IOSXE           INSTALL
_MODE = re.compile(r"^\*?\s*\d+\s+\d+\s+\S+\s+\S+\s+\S+\s+(?P<mode>INSTALL|BUNDLE)\s*$", re.M)
#    10  -rw-   601216545  Nov 1 2026 10:00:00 +00:00  cat9k_iosxe.17.09.05.SPA.bin
_DIR_ENTRY = re.compile(r"^\s*\d+\s+[-drwx]{4}\s+(?P<size>\d+)\s+.*\s(?P<name>\S+)\s*$")
_DIR_FREE = re.compile(r"\((?P<free>\d+) bytes free\)")
_MD5 = re.compile(r"=\s*(?P<md5>[0-9a-fA-F]{32})\b")
_REJECTED = re.compile(r"^\s*% ?(?:Invalid input|Incomplete command|Ambiguous command)", re.M)
_ERROR = re.compile(r"^\s*%", re.M)
_COPY_ANSWERS = (
    (r"Destination filename \[", ""),
    (r"over ?write\??.*\[confirm\]", ""),
)


@dataclass(frozen=True)
class _Boot:
    version: str | None
    fs: str  # where the running image is, and where a new one goes: flash:, bootflash:
    image: str  # the running image's path on fs: packages.conf, or a .bin
    mode: str  # INSTALL or BUNDLE


class IosFirmware:
    family = PlatformFamily.IOS
    transfers = frozenset({TransferMethod.SCP, TransferMethod.TFTP, TransferMethod.HTTP})
    device_hash = "md5"

    # -- reading the device ------------------------------------------------------------

    def _boot(self, job: FirmwareJob) -> _Boot:
        output = job.session.send_exec("show version").output
        found = _IMAGE_FILE.search(output)
        if found is None:
            raise FirmwareError(
                "show version does not say which image the device booted from "
                "(no `System image file is` line), so Tsunami cannot tell where images go"
            )
        image = found["path"]
        mode_line = _MODE.search(output)
        if mode_line is not None:
            mode = mode_line["mode"].lower()
        else:
            mode = INSTALL if image.lower().endswith("packages.conf") else BUNDLE
        version = parse_facts(PlatformFamily.IOS, {"show version": output}).version
        return _Boot(version, found["fs"], image, mode)

    def _dir(self, job: FirmwareJob, fs: str) -> tuple[int | None, dict[str, int]]:
        """Free bytes on a file system and the files in its top folder, name to size."""
        output = job.session.send_exec(f"dir {fs}").output
        if _ERROR.search(output):
            raise FirmwareError(f"`dir {fs}` failed: {device_error(output)}")
        files = {}
        for line in output.split("\n"):
            entry = _DIR_ENTRY.match(line)
            if entry is not None:
                files[entry["name"]] = int(entry["size"])
        free = _DIR_FREE.search(output)
        return (int(free["free"]) if free else None), files

    def _members(self, job: FirmwareJob) -> list[tuple[int, str, str]]:
        """Stack members as (number, role, state). Empty on a device that is not a
        stackable switch."""
        output = job.session.send_exec("show switch").output
        if _REJECTED.search(output):
            return []
        rows = parse_stack(output.replace("\r\n", "\n"))
        return [(int(row["member"]), str(row["role"]), str(row["state"])) for row in rows]

    def _md5(self, job: FirmwareJob, path: str) -> str:
        job.log(f"verifying {path} (this can take several minutes)")
        timeout = max(VERIFY_SECONDS, job.command_timeout)
        output = job.session.send_exec(f"verify /md5 {path}", read_timeout=timeout).output
        found = _MD5.search(output)
        if found is None:
            raise FirmwareError(f"`verify /md5 {path}` gave no hash: {device_error(output)}")
        return found["md5"].lower()

    @staticmethod
    def _wanted_md5(job: FirmwareJob) -> str:
        md5 = job.image.hash("md5")
        if not md5:
            raise FirmwareError(
                f"there is no md5 for {job.image.file} to compare with: add md5 to the "
                "image in the play, or put the file in the firmware folder"
            )
        return md5.lower()

    def _present(self, job: FirmwareJob, files: dict[str, int]) -> bool:
        name, size = job.image.file, job.image.size
        return name in files and (size is None or files[name] == size)

    def inspect(self, job: FirmwareJob) -> ImageState:
        boot = self._boot(job)
        free, files = self._dir(job, boot.fs)
        members = max(1, len(self._members(job)))
        staged = self._present(job, files)
        if staged:
            where = f"{job.image.file} is on the device"
        elif job.image.file in files:
            where = (
                f"{job.image.file} is on the device with the wrong size "
                f"({files[job.image.file]:,} bytes)"
            )
        else:
            where = f"{job.image.file} is not on the device"
        room = f"{free:,} bytes free on {boot.fs}" if free is not None else f"images on {boot.fs}"
        stack = f"; a stack of {members}" if members > 1 else ""
        detail = (
            f"runs {boot.version or 'an unknown version'} from {boot.fs}{boot.image} "
            f"({boot.mode} mode){stack}; {room}; {where}"
        )
        return ImageState(boot.version, staged, detail, free, members)

    # -- stage -------------------------------------------------------------------------

    def stage(self, job: FirmwareJob) -> None:
        md5 = self._wanted_md5(job)
        boot = self._boot(job)
        path = f"{boot.fs}{job.image.file}"
        _free, files = self._dir(job, boot.fs)
        copied = False
        if job.image.file in files:
            if self._md5(job, path) == md5:
                job.log(f"{path} is already on the device and its md5 matches")
            else:
                job.log(f"{path} is on the device but is not the right file; replacing it")
                self._delete(job, path)
                copied = True
        else:
            copied = True
        if copied:
            job.check_cancel()
            self._copy_in(job, boot.fs)
            job.check_cancel()
            found = self._md5(job, path)
            if found != md5:
                self._delete(job, path)
                raise FirmwareError(
                    f"{path} did not verify after the copy: its md5 is {found}, expected "
                    f"{md5}. The copy was removed; run the stage step again"
                )
            job.log(f"{path} copied and verified")
        if boot.mode == BUNDLE:
            self._stage_members(job, boot, md5)

    def _copy_in(self, job: FirmwareJob, fs: str) -> None:
        file = job.image.file
        if job.transfer == TransferMethod.SCP:
            if job.upload is None or job.image.local is None:
                raise FirmwareError(
                    "an scp transfer needs the image in the firmware folder and an SSH "
                    "connection to the device"
                )
            enabled = job.session.send_exec(
                "show running-config | include ^ip scp server enable"
            ).output
            if "ip scp server enable" not in enabled:
                raise FirmwareError(
                    "the device does not accept SCP uploads: add `ip scp server enable` "
                    "(a config step before the stage step will do). IOS also needs "
                    "`aaa authorization exec` so the login lands at privilege 15"
                )
            job.log(f"uploading {file} to {fs}")
            job.upload(job.image.local, f"{fs}/{file}", job.progress_logger("upload"))
            return
        url = server_url(job)
        job.log(f"the device is fetching {url}")
        self._copy(job, f"copy {url} {fs}{file}")

    def _copy(self, job: FirmwareJob, command: str) -> None:
        result = job.session.send_interactive(command, _COPY_ANSWERS, read_timeout=COPY_SECONDS)
        if _ERROR.search(result.output) or "bytes copied" not in result.output:
            raise FirmwareError(f"`{command}` failed: {device_error(result.output)}")
        done = [line.strip() for line in result.output.split("\n") if "bytes copied" in line]
        job.log(f"{command}: {done[-1]}")

    def _delete(self, job: FirmwareJob, path: str) -> None:
        output = job.session.send_exec(f"delete /force {path}").output
        if _ERROR.search(output):
            raise FirmwareError(f"could not remove {path}: {device_error(output)}")

    # Members' file systems. UNVERIFIED which form a platform uses (flash-2: on IOS-XE,
    # flash2: on IOS classic), so both are tried and neither is assumed.
    def _member_fs(self, job: FirmwareJob, fs: str, number: int) -> tuple[str, dict[str, int]]:
        base = fs.rstrip(":")
        for candidate in (f"{base}-{number}:", f"{base}{number}:"):
            output = job.session.send_exec(f"dir {candidate}").output
            if _ERROR.search(output):
                continue
            files = {}
            for line in output.split("\n"):
                entry = _DIR_ENTRY.match(line)
                if entry is not None:
                    files[entry["name"]] = int(entry["size"])
            return candidate, files
        raise FirmwareError(
            f"cannot find stack member {number}'s flash ({base}-{number}: and "
            f"{base}{number}: both failed). In bundle mode every member boots from its own "
            f"flash: copy {job.image.file} to member {number} by hand, then run this again"
        )

    def _other_members(self, job: FirmwareJob) -> list[int]:
        """Every stack member but the active one, whose flash is the plain file system."""
        members = self._members(job)
        if len(members) < 2:
            return []
        return [number for number, role, _state in members if role not in ("active", "master")]

    def _stage_members(self, job: FirmwareJob, boot: _Boot, md5: str) -> None:
        for number in self._other_members(job):
            job.check_cancel()
            fs, files = self._member_fs(job, boot.fs, number)
            path = f"{fs}{job.image.file}"
            if job.image.file in files and self._md5(job, path) == md5:
                job.log(f"member {number}: {path} is there and its md5 matches")
                continue
            if job.image.file in files:
                self._delete(job, path)
            job.log(f"member {number}: copying the image to {fs}")
            self._copy(job, f"copy {boot.fs}{job.image.file} {path}")
            found = self._md5(job, path)
            if found != md5:
                self._delete(job, path)
                raise FirmwareError(
                    f"member {number}: {path} did not verify after the copy (md5 {found})"
                )

    def _check_members(self, job: FirmwareJob, boot: _Boot, md5: str) -> None:
        for number in self._other_members(job):
            fs, files = self._member_fs(job, boot.fs, number)
            path = f"{fs}{job.image.file}"
            if job.image.file not in files or self._md5(job, path) != md5:
                raise FirmwareError(
                    f"stack member {number} does not have a verified {job.image.file} on "
                    f"{fs}: run the stage step first"
                )

    # -- activate ----------------------------------------------------------------------

    def activate(self, job: FirmwareJob) -> None:
        md5 = self._wanted_md5(job)
        boot = self._boot(job)
        path = f"{boot.fs}{job.image.file}"
        _free, files = self._dir(job, boot.fs)
        if job.image.file not in files:
            raise FirmwareError(f"{path} is not on the device: run the stage step first")
        found = self._md5(job, path)
        if found != md5:
            raise FirmwareError(
                f"{path} is not the image the play names (md5 {found}, expected {md5}): "
                "run the stage step again. Nothing was changed"
            )
        before = max(1, len(self._members(job)))
        job.check_cancel()
        if boot.mode == INSTALL:
            self._install(job, path)
        else:
            self._boot_from(job, boot, path, md5, stack=before > 1)
        job.reconnect()
        state = self.inspect(job)
        if not same_version(state.version, job.image.version):
            raise FirmwareError(
                f"the device came back on {state.version or 'an unknown version'}, not "
                f"{job.image.version}. It is reachable; check its boot variable and "
                f"`dir {boot.fs}` before trying again"
            )
        if before > 1:
            self._wait_for_members(job, before)

    def _save(self, job: FirmwareJob) -> None:
        output = job.session.send_exec(
            "write memory",
            expect_prompt=r"\[confirm\]",
            answer="",
            read_timeout=max(RELOAD_PROMPT_SECONDS, job.command_timeout),
        ).output
        if _ERROR.search(output):
            raise FirmwareError(f"`write memory` failed: {device_error(output)}")

    def _install(self, job: FirmwareJob, path: str) -> None:
        self._save(job)
        command = f"install add file {path} activate commit prompt-level none"
        job.log(f"{command} (the device unpacks the image, then reloads by itself)")
        result = job.session.send_interactive(
            command, [(r"\[y/n\]", "y")], read_timeout=job.reload_timeout, may_disconnect=True
        )
        failed = [ln.strip() for ln in result.output.split("\n") if "FAILED:" in ln]
        if failed and not result.disconnected:
            hint = ""
            if "already added" in failed[-1]:
                hint = ". `install remove inactive` discards a package added earlier"
            raise FirmwareError(f"the install failed: {failed[-1]}{hint}. Nothing was reloaded")
        done = [ln.strip() for ln in result.output.split("\n") if "SUCCESS:" in ln]
        if done:
            job.log(done[-1])

    def _boot_from(
        self, job: FirmwareJob, boot: _Boot, path: str, md5: str, *, stack: bool
    ) -> None:
        if stack:
            self._check_members(job, boot, md5)
        word = "boot system switch all " if stack else "boot system "
        lines = ["no boot system", word + path]
        # The image running now stays as the second entry: if the new one cannot be
        # loaded, the boot loader goes on to it.
        if boot.image.lower().endswith(".bin") and boot.image != job.image.file:
            lines.append(word + boot.fs + boot.image)
        result = job.session.send_config(lines)
        if not result.ok:
            raise FirmwareError(
                f"the device rejected `{result.failed_line or result.error}` while setting "
                "the boot variable. Check `show boot` before anything reloads this device"
            )
        self._save(job)
        # Never reload on a boot variable that does not name the verified image.
        shown = job.session.send_exec("show boot").output
        if _REJECTED.search(shown):
            shown = job.session.send_exec("show bootvar").output
        if job.image.file not in shown:
            raise FirmwareError(
                f"the boot variable does not name {job.image.file} after it was set, so "
                f"the device was not reloaded. It says: {device_error(shown, 'BOOT')}"
            )
        job.log(f"boot variable set to {path}; reloading")
        job.session.send_interactive(
            "reload",
            [(r"Save\? \[yes/no\]", "no"), (r"\[confirm\]", "")],
            read_timeout=RELOAD_PROMPT_SECONDS,
            may_disconnect=True,
        )

    def _wait_for_members(self, job: FirmwareJob, before: int) -> None:
        members: list[tuple[int, str, str]] = []
        for attempt in range(MEMBER_POLLS):
            members = self._members(job)
            ready = [m for m in members if m[2] == "ready"]
            if len(ready) >= before:
                job.log(f"all {before} stack members are ready")
                return
            if attempt < MEMBER_POLLS - 1:
                job.check_cancel()
                job.sleep(MEMBER_POLL_SECONDS)
        waiting = [f"member {n} is {state}" for n, _role, state in members if state != "ready"]
        if len(members) < before:
            waiting.append(f"{before - len(members)} of {before} members are missing")
        raise FirmwareError("the stack did not form again after the reload: " + "; ".join(waiting))


DRIVER = IosFirmware()
