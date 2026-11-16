"""The IOS and IOS-XE firmware driver against a scripted session (design.md, Firmware
upgrades). The order of commands is asserted wherever order is what keeps a device
bootable: the image verified before the boot variable changes, and the boot variable read
back before the reload."""

from __future__ import annotations

from pathlib import Path

import pytest
from tests.unit.firmware_fakes import ScriptedSession, image, make_job

from tsunami.core.firmware import DEVICE_HASH, TRANSFERS, DeviceGone, FirmwareError
from tsunami.core.firmware.ios import DRIVER
from tsunami.core.models.common import PlatformFamily
from tsunami.core.models.play import TransferMethod

SHOW = Path(__file__).parents[1] / "fixtures" / "show" / "ios"
C1101 = (SHOW / "c1101_show_version.txt").read_text(encoding="utf-8")  # bundle mode, real
OLD = "c1100-universalk9_ias.16.09.04.SPA.bin"
NEW = "c1100-universalk9.17.09.05.SPA.bin"
MD5 = "0123456789abcdef0123456789abcdef"
WRONG = "ffffffffffffffffffffffffffffffff"
SIZE = 706_000_000
C1101_NEW = C1101.replace("Version 16.09.04", "Version 17.09.05").replace(OLD, NEW)

INSTALL_VERSION = """\
Cisco IOS XE Software, Version 17.06.04
Cisco IOS Software [Bengaluru], Catalyst L3 Switch Software (CAT9K_IOSXE), Version 17.6.4
System image file is "flash:packages.conf"

Switch Ports Model              SW Version        SW Image              Mode
------ ----- -----              ----------        ----------            ----
*    1 56    C9300-48P          17.06.04          CAT9K_IOSXE           INSTALL
     2 56    C9300-48P          17.06.04          CAT9K_IOSXE           INSTALL
"""
STACK_BUNDLE_VERSION = """\
Cisco IOS Software, C2960X Software (C2960X-UNIVERSALK9-M), Version 15.2(7)E3, RELEASE
System image file is "flash:c2960x-universalk9-mz.152-7.E3.bin"
"""
INVALID = "show switch\n       ^\n% Invalid input detected at '^' marker.\n"
SWITCH_TWO = """\
Switch/Stack Mac Address : 0011.2233.4455 - Local Mac Address
                                             H/W   Current
Switch#   Role    Mac Address     Priority Version  State
-------------------------------------------------------------------------------------
*1       Active   0011.2233.4455     15     V01     Ready
 2       Standby  0011.2233.6677     14     V01     Ready
"""


def listing(fs: str, files: dict[str, int], free: int = 1_900_000_000) -> str:
    rows = "".join(
        f"   {i + 10}  -rw-   {size}  Nov 1 2026 10:00:00 +00:00  {name}\n"
        for i, (name, size) in enumerate(files.items())
    )
    return f"Directory of {fs}/\n\n{rows}\n2863103488 bytes total ({free} bytes free)\n"


def verify(path: str, md5: str) -> str:
    return f"....................Done!\nverify /md5 ({path}) = {md5}\n"


class Device:
    """A scripted IOS device whose flash a test can change as the driver works."""

    def __init__(self, version: str = C1101, fs: str = "bootflash:", switch: str = INVALID):
        self.fs = fs
        self.files: dict[str, dict[str, int]] = {fs: {OLD: 500_000_000}}
        self.md5: dict[str, str] = {}
        self.scp = True
        self.boot = f"BOOT variable = {fs}{OLD};"
        self.session = ScriptedSession(default="")
        s = self.session
        s.on(r"^show version$", version)
        s.on(r"^show switch$", switch)
        s.on(r"^dir (\S+)$", self._dir)
        s.on(r"^verify /md5 (\S+)$", self._verify)
        s.on(r"^delete /force (\S+)$", self._delete)
        s.on(r"^show running-config \| include \^ip scp", lambda c: self._scp())
        s.on(r"^write memory$", "Building configuration...\n[OK]\n")
        s.on(r"^show boot$", lambda c: self.boot + "\n")
        s.on(r"^copy ", self._copy)

    def _scp(self) -> str:
        return "ip scp server enable\n" if self.scp else ""

    def _split(self, path: str) -> tuple[str, str]:
        fs, _, name = path.partition(":")
        return fs + ":", name.lstrip("/")

    def _dir(self, command: str) -> str:
        fs = command.split()[1]
        if fs not in self.files:
            return f"%Error opening {fs}/ (No such device)\n"
        return listing(fs.rstrip(":") + ":", self.files[fs])

    def _verify(self, command: str) -> str:
        path = command.split()[2]
        return verify(path, self.md5.get(path, MD5))

    def _delete(self, command: str) -> str:
        fs, name = self._split(command.split()[2])
        self.files[fs].pop(name, None)
        return ""

    def _copy(self, command: str) -> str:
        _, _source, dest = command.split()
        fs, name = self._split(dest)
        self.files.setdefault(fs, {})[name] = SIZE
        return f"Destination filename [{name}]? \n!!!!\n{SIZE} bytes copied in 50.6 secs\n"

    def put(self, fs: str | None = None, name: str = NEW, size: int = SIZE) -> None:
        self.files.setdefault(fs or self.fs, {})[name] = size

    def uploaded(self, local, remote: str) -> None:
        fs, name = self._split(remote)
        self.files[fs][name] = SIZE


def new_image(tmp_path=None, **hashes) -> object:
    local = None
    if tmp_path is not None:
        local = tmp_path / NEW
        local.write_bytes(b"x")
    return image(NEW, "17.09.05", size=SIZE, local=local, **({"md5": MD5} | hashes))


def test_driver_declares_what_validation_assumes():
    assert DRIVER.family is PlatformFamily.IOS
    assert DRIVER.transfers == TRANSFERS[PlatformFamily.IOS]
    assert DRIVER.device_hash == DEVICE_HASH[PlatformFamily.IOS]


# -- inspect ------------------------------------------------------------------------------


def test_inspect_a_router_in_bundle_mode():
    device = Device()
    job, _ = make_job(device.session, image=new_image())
    state = DRIVER.inspect(job)
    assert state.version == "16.09.04"
    assert not state.staged and state.members == 1
    assert state.free_bytes == 1_900_000_000
    assert state.detail == (
        f"runs 16.09.04 from bootflash:{OLD} (bundle mode); 1,900,000,000 bytes free on "
        f"bootflash:; {NEW} is not on the device"
    )


def test_inspect_sees_a_staged_image_and_a_partial_one():
    device = Device()
    device.put()
    job, _ = make_job(device.session, image=new_image())
    assert DRIVER.inspect(job).staged

    device.put(size=1234)  # a copy that stopped part way
    state = DRIVER.inspect(job)
    assert not state.staged and "wrong size (1,234 bytes)" in state.detail


def test_inspect_a_stack_in_install_mode():
    device = Device(INSTALL_VERSION, "flash:", SWITCH_TWO)
    job, _ = make_job(device.session, image=new_image())
    state = DRIVER.inspect(job)
    assert state.members == 2
    assert "(install mode); a stack of 2" in state.detail and state.version == "17.06.04"


def test_inspect_fails_when_show_version_names_no_image():
    session = ScriptedSession().on(r"^show version$", "Cisco IOS Software, Version 15.2\n")
    job, _ = make_job(session, image=new_image())
    with pytest.raises(FirmwareError, match="does not say which image"):
        DRIVER.inspect(job)


# -- stage --------------------------------------------------------------------------------


def test_stage_uploads_then_verifies(tmp_path):
    device = Device()
    job, record = make_job(device.session, image=new_image(tmp_path), on_upload=device.uploaded)
    DRIVER.stage(job)
    assert record.uploads == [(tmp_path / NEW, f"bootflash:/{NEW}")]
    commands = device.session.commands
    scp_check = commands.index("show running-config | include ^ip scp server enable")
    assert scp_check < commands.index(f"verify /md5 bootflash:{NEW}")
    assert device.session.timeouts[f"verify /md5 bootflash:{NEW}"] >= 900
    assert "copied and verified" in record.text()
    assert not device.session.config  # staging never touches what the device boots


def test_stage_copies_nothing_when_the_image_is_there_and_verifies(tmp_path):
    device = Device()
    device.put()
    job, record = make_job(device.session, image=new_image(tmp_path))
    DRIVER.stage(job)
    assert record.uploads == []
    assert "already on the device and its md5 matches" in record.text()


def test_stage_replaces_a_file_with_the_wrong_md5(tmp_path):
    device = Device()
    device.put()
    path = f"bootflash:{NEW}"
    device.md5[path] = WRONG

    def upload(local, remote):
        device.uploaded(local, remote)
        device.md5[path] = MD5

    job, record = make_job(device.session, image=new_image(tmp_path), on_upload=upload)
    DRIVER.stage(job)
    commands = device.session.commands
    assert commands.index(f"delete /force {path}") < commands.index(
        "show running-config | include ^ip scp server enable"
    )
    assert len(record.uploads) == 1


def test_stage_removes_a_copy_that_does_not_verify(tmp_path):
    device = Device()
    device.md5[f"bootflash:{NEW}"] = WRONG
    job, _ = make_job(device.session, image=new_image(tmp_path), on_upload=device.uploaded)
    with pytest.raises(FirmwareError, match="did not verify after the copy.*copy was removed"):
        DRIVER.stage(job)
    assert NEW not in device.files["bootflash:"]


def test_stage_needs_scp_enabled_and_says_how(tmp_path):
    device = Device()
    device.scp = False
    job, record = make_job(device.session, image=new_image(tmp_path))
    with pytest.raises(FirmwareError, match="add `ip scp server enable`"):
        DRIVER.stage(job)
    assert record.uploads == []


def test_stage_needs_an_md5_before_it_copies_anything():
    device = Device()
    job, record = make_job(device.session, image=image(NEW, "17.09.05", sha256="a" * 64))
    with pytest.raises(FirmwareError, match="no md5 for"):
        DRIVER.stage(job)
    assert device.session.commands == [] and record.uploads == []


@pytest.mark.parametrize(
    ("transfer", "server"),
    [
        (TransferMethod.TFTP, "tftp://192.0.2.50/images"),
        (TransferMethod.HTTP, "https://192.0.2.50:8443/images"),
    ],
)
def test_stage_has_the_device_fetch_the_image(transfer, server):
    device = Device()
    job, record = make_job(
        device.session, image=new_image(), transfer=transfer, server=server, upload=False
    )
    DRIVER.stage(job)
    command = f"copy {server}/{NEW} bootflash:{NEW}"
    sent, answers, may_disconnect = device.session.interactive[0]
    assert sent == command and not may_disconnect
    assert (r"Destination filename \[", "") in answers
    assert device.session.timeouts[command] == 3600
    assert f"{SIZE} bytes copied" in record.text()
    commands = device.session.commands
    assert commands.index(command) < commands.index(f"verify /md5 bootflash:{NEW}")


def test_a_failed_fetch_quotes_the_device():
    device = Device()
    device.session.on(r"^copy ", "%Error opening tftp://192.0.2.50/x.bin (Timed out)\n")
    job, _ = make_job(
        device.session,
        image=new_image(),
        transfer=TransferMethod.TFTP,
        server="tftp://192.0.2.50",
        upload=False,
    )
    with pytest.raises(FirmwareError, match=r"failed: %Error opening tftp.*Timed out"):
        DRIVER.stage(job)


def test_stage_on_a_bundle_mode_stack_puts_the_image_on_every_member(tmp_path):
    device = Device(STACK_BUNDLE_VERSION, "flash:", SWITCH_TWO)
    device.files = {"flash:": {"c2960x-universalk9-mz.152-7.E3.bin": 20_000_000}, "flash2:": {}}
    job, record = make_job(device.session, image=new_image(tmp_path), on_upload=device.uploaded)
    DRIVER.stage(job)
    commands = device.session.commands
    assert "dir flash-2:" in commands and "dir flash2:" in commands  # neither form assumed
    copy = f"copy flash:{NEW} flash2:{NEW}"
    assert commands.index(copy) < commands.index(f"verify /md5 flash2:{NEW}")
    assert NEW in device.files["flash2:"]
    assert "member 2: copying the image to flash2:" in record.text()


def test_a_member_whose_flash_cannot_be_found_stops_the_stage(tmp_path):
    device = Device(STACK_BUNDLE_VERSION, "flash:", SWITCH_TWO)
    device.files = {"flash:": {}}
    job, _ = make_job(device.session, image=new_image(tmp_path), on_upload=device.uploaded)
    with pytest.raises(FirmwareError, match="cannot find stack member 2's flash.*by hand"):
        DRIVER.stage(job)


def test_install_mode_leaves_the_members_to_install(tmp_path):
    device = Device(INSTALL_VERSION, "flash:", SWITCH_TWO)
    device.files = {"flash:": {"packages.conf": 5000}}
    job, _ = make_job(device.session, image=new_image(tmp_path), on_upload=device.uploaded)
    DRIVER.stage(job)
    assert not any(c.startswith("copy ") or "flash-2" in c for c in device.session.commands)


# -- activate: bundle mode ------------------------------------------------------------------


def bundle_device() -> tuple[Device, ScriptedSession]:
    device = Device()
    device.put()
    device.session.disconnect_on = ["^reload$"]
    back = ScriptedSession(default="")
    back.on(r"^show version$", C1101_NEW)
    back.on(r"^show switch$", INVALID)
    back.on(r"^dir (\S+)$", listing("bootflash:", {NEW: SIZE, OLD: 500_000_000}))
    return device, back


def test_activate_in_bundle_mode():
    device, back = bundle_device()
    device.boot = f"BOOT variable = bootflash:{NEW},12;bootflash:{OLD},12;"
    job, record = make_job(device.session, image=new_image(), after_reload=back)
    DRIVER.activate(job)

    assert device.session.config == [
        ["no boot system", f"boot system bootflash:{NEW}", f"boot system bootflash:{OLD}"]
    ]
    commands = device.session.commands
    order = [f"verify /md5 bootflash:{NEW}", "write memory", "show boot", "reload"]
    assert [commands.index(c) for c in order] == sorted(commands.index(c) for c in order)
    sent, answers, may_disconnect = device.session.interactive[-1]
    assert sent == "reload" and may_disconnect
    assert answers == ((r"Save\? \[yes/no\]", "no"), (r"\[confirm\]", ""))
    assert record.reconnects == 1 and job.session is back
    assert "boot variable set to" in record.text()


def test_activate_refuses_an_image_that_is_missing():
    device = Device()
    job, record = make_job(device.session, image=new_image())
    with pytest.raises(FirmwareError, match="is not on the device: run the stage step first"):
        DRIVER.activate(job)
    assert device.session.config == [] and "reload" not in device.session.commands
    assert record.reconnects == 0


def test_activate_refuses_an_image_that_does_not_verify():
    device, back = bundle_device()
    device.md5[f"bootflash:{NEW}"] = WRONG
    job, record = make_job(device.session, image=new_image(), after_reload=back)
    with pytest.raises(FirmwareError, match="is not the image the play names.*Nothing was changed"):
        DRIVER.activate(job)
    assert device.session.config == [] and "reload" not in device.session.commands


def test_activate_does_not_reload_on_a_boot_variable_it_cannot_confirm():
    device, back = bundle_device()  # show boot still names only the old image
    job, record = make_job(device.session, image=new_image(), after_reload=back)
    with pytest.raises(FirmwareError, match="boot variable does not name.*was not reloaded"):
        DRIVER.activate(job)
    assert "reload" not in device.session.commands and record.reconnects == 0


def test_activate_reads_bootvar_where_show_boot_is_not_a_command():
    device, back = bundle_device()
    device.session.on(r"^show boot$", "show boot\n       ^\n% Invalid input detected\n")
    device.session.on(r"^show bootvar$", f"BOOT variable = bootflash:{NEW},12;\n")
    job, _ = make_job(device.session, image=new_image(), after_reload=back)
    DRIVER.activate(job)
    commands = device.session.commands
    assert commands.index("show bootvar") < commands.index("reload")


def test_activate_stops_when_the_boot_command_is_rejected():
    device, back = bundle_device()
    device.session.config_error = f"boot system bootflash:{NEW}"
    job, _ = make_job(device.session, image=new_image(), after_reload=back)
    with pytest.raises(FirmwareError, match="rejected `boot system"):
        DRIVER.activate(job)
    assert "reload" not in device.session.commands


def test_a_device_back_on_the_old_version_fails():
    device, back = bundle_device()
    device.boot = f"BOOT variable = bootflash:{NEW};"
    back.on(r"^show version$", C1101)
    job, _ = make_job(device.session, image=new_image(), after_reload=back)
    with pytest.raises(FirmwareError, match="came back on 16.09.04, not 17.09.05"):
        DRIVER.activate(job)


def test_a_device_that_never_returns_is_device_gone():
    device, _ = bundle_device()
    device.boot = f"BOOT variable = bootflash:{NEW};"
    job, record = make_job(device.session, image=new_image(), after_reload=None)
    with pytest.raises(DeviceGone):
        DRIVER.activate(job)
    assert record.reconnects == 1


def test_a_bundle_mode_stack_boots_every_member_from_a_verified_copy():
    device = Device(STACK_BUNDLE_VERSION, "flash:", SWITCH_TWO)
    old = "c2960x-universalk9-mz.152-7.E3.bin"
    device.files = {"flash:": {old: 20_000_000, NEW: SIZE}, "flash2:": {NEW: SIZE}}
    device.boot = f"BOOT path-list      : flash:{NEW};flash:{old}"
    device.session.disconnect_on = ["^reload$"]
    back = ScriptedSession(default="")
    back.on(r"^show version$", STACK_BUNDLE_VERSION.replace("15.2(7)E3", "17.09.05"))
    back.on(r"^show switch$", SWITCH_TWO)
    back.on(r"^dir (\S+)$", listing("flash:", {NEW: SIZE}))
    job, record = make_job(device.session, image=new_image(), after_reload=back)
    DRIVER.activate(job)
    assert device.session.config == [
        [
            "no boot system",
            f"boot system switch all flash:{NEW}",
            f"boot system switch all flash:{old}",
        ]
    ]
    commands = device.session.commands
    assert commands.index(f"verify /md5 flash2:{NEW}") < commands.index("reload")
    assert "all 2 stack members are ready" in record.text()


def test_a_stack_member_without_the_image_blocks_the_activate():
    device = Device(STACK_BUNDLE_VERSION, "flash:", SWITCH_TWO)
    device.files = {"flash:": {NEW: SIZE}, "flash2:": {}}
    job, _ = make_job(device.session, image=new_image())
    with pytest.raises(FirmwareError, match="stack member 2 does not have a verified"):
        DRIVER.activate(job)
    assert device.session.config == [] and "reload" not in device.session.commands


def test_a_member_missing_after_the_reload_fails_after_waiting():
    device = Device(STACK_BUNDLE_VERSION, "flash:", SWITCH_TWO)
    device.files = {"flash:": {NEW: SIZE}, "flash2:": {NEW: SIZE}}
    device.boot = f"BOOT path-list      : flash:{NEW}"
    device.session.disconnect_on = ["^reload$"]
    back = ScriptedSession(default="")
    back.on(r"^show version$", STACK_BUNDLE_VERSION.replace("15.2(7)E3", "17.09.05"))
    back.on(
        r"^show switch$",
        SWITCH_TWO.replace("V01     Ready\n 2", "V01     Ready\n 9").replace(
            " 9       Standby  0011.2233.6677     14     V01     Ready",
            " 2       Member   0011.2233.6677     14     V01     V-Mismatch",
        ),
    )
    back.on(r"^dir (\S+)$", listing("flash:", {NEW: SIZE}))
    job, record = make_job(device.session, image=new_image(), after_reload=back)
    with pytest.raises(FirmwareError, match="did not form again.*member 2 is v-mismatch"):
        DRIVER.activate(job)
    assert len(record.slept) == 19 and set(record.slept) == {30.0}


# -- activate: install mode -------------------------------------------------------------------


def install_device() -> tuple[Device, ScriptedSession]:
    device = Device(INSTALL_VERSION, "flash:", SWITCH_TWO)
    device.files = {"flash:": {"packages.conf": 5000, NEW: SIZE}}
    back = ScriptedSession(default="")
    back.on(r"^show version$", INSTALL_VERSION.replace("17.06.04", "17.09.05"))
    back.on(r"^show switch$", SWITCH_TWO)
    back.on(r"^dir (\S+)$", listing("flash:", {"packages.conf": 5000}))
    return device, back


def test_activate_in_install_mode():
    device, back = install_device()
    command = f"install add file flash:{NEW} activate commit prompt-level none"
    device.session.disconnect_on = ["^install add file"]
    job, record = make_job(device.session, image=new_image(), after_reload=back)
    DRIVER.activate(job)

    sent, answers, may_disconnect = device.session.interactive[-1]
    assert sent == command and may_disconnect and answers == ((r"\[y/n\]", "y"),)
    assert device.session.timeouts[command] == job.reload_timeout
    commands = device.session.commands
    assert commands.index(f"verify /md5 flash:{NEW}") < commands.index("write memory")
    assert commands.index("write memory") < commands.index(command)
    assert device.session.config == []  # install mode keeps booting packages.conf
    assert record.reconnects == 1
    assert "all 2 stack members are ready" in record.text()


def test_a_failed_install_is_reported_and_nothing_reloads():
    device, back = install_device()
    device.session.on(
        r"^install add file",
        "install_add_activate_commit: START\n"
        "FAILED: install_add_activate_commit : Super package already added. "
        "Add operation not allowed.\n",
    )
    job, record = make_job(device.session, image=new_image(), after_reload=back)
    with pytest.raises(FirmwareError) as raised:
        DRIVER.activate(job)
    text = str(raised.value)
    assert "Super package already added" in text and "install remove inactive" in text
    assert record.reconnects == 0


def test_an_install_that_reports_success_then_reloads():
    device, back = install_device()
    device.session.on(
        r"^install add file", "SUCCESS: install_add_activate_commit Mon Nov 16 10:22:59\n"
    )
    job, record = make_job(device.session, image=new_image(), after_reload=back)
    DRIVER.activate(job)
    assert record.reconnects == 1
    assert "SUCCESS: install_add_activate_commit" in record.text()
