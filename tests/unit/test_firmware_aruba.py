"""The AOS-CX firmware driver against a scripted session (design.md, Firmware upgrades).
The order of commands is asserted where order keeps a switch bootable: the bank is read
back and verified before the boot command."""

from __future__ import annotations

import pytest
from tests.unit.firmware_fakes import ScriptedSession, image, make_job

from tsunami.core.firmware import DEVICE_HASH, TRANSFERS, DeviceGone, FirmwareError
from tsunami.core.firmware.aoscx import DRIVER
from tsunami.core.models.common import PlatformFamily
from tsunami.core.models.play import TransferMethod

OLD, NEW = "FL.10.10.1010", "FL.10.13.1000"
SHA = "a" * 64
URL = "tftp://192.0.2.10/images"
VSF_TWO = """\
Mbr ID  Mac Address        Type    Status
1       aa:bb:cc:00:11:22  JL668A  Conductor
2       aa:bb:cc:00:11:33  JL668A  Standby
"""


def show_images(versions: dict[str, str], active: str, default: str | None = None) -> str:
    out = ""
    for bank in ("primary", "secondary"):
        out += (
            f"AOS-CX {bank.capitalize()} Image\nVersion : {versions[bank]}\n"
            f"Size    : 722 MB\nSHA-256 : {SHA}\n"
        )
    return (
        out
        + f"Default Image : {default or active}\nManagement Module 1/1 (Active)\n"
        + f"Active Image       : {active}\nService OS Version : FL.01.05.0001\n"
    )


class Device:
    """A scripted AOS-CX switch whose banks change as the driver works."""

    def __init__(self, active: str = "primary", vsf: str = "", valid: str | None = None):
        self.versions = {"primary": OLD, "secondary": OLD}
        self.versions["secondary" if active == "primary" else "primary"] = "FL.10.09.0001"
        self.active = active
        self.copies = True
        self.boots = True
        self.boot_output = "The system is going down for reboot.\n"
        self.vsf = vsf
        self.valid = valid
        self.session = self.make()
        self.after = self.make()

    def make(self) -> ScriptedSession:
        s = ScriptedSession(default="")
        s.on(r"^show images$", lambda c: show_images(self.versions, self.active))
        s.on(r"^show vsf$", lambda c: self.vsf or "% Unknown command.\n")
        s.on(r"^show images verify (\w+)$", self._verify)
        s.on(r"^write memory$", "Copying configuration: [Success]\n")
        s.on(r"^copy ", self._copy)
        s.on(r"^boot system ", self._boot)
        return s

    def _verify(self, command: str) -> str:
        bank = command.split()[-1]
        return self.valid if self.valid is not None else f"The {bank} image is valid\n"

    def _copy(self, command: str) -> str:
        bank = command.split()[2]
        if self.copies:
            self.versions[bank] = NEW
        return "Verifying and writing system firmware...\n"

    def _boot(self, command: str) -> str:
        bank = command.split()[2]
        if self.boots:
            self.active = bank
        return self.boot_output


def job_for(device: Device, **kw):
    kw.setdefault("transfer", TransferMethod.TFTP)
    kw.setdefault("server", URL)
    return make_job(
        device.session,
        image=image("fl.swi", NEW, sha256=SHA),
        after_reload=device.after,
        **kw,
    )


def test_tables_agree_with_the_package() -> None:
    assert DRIVER.family is PlatformFamily.AOS_CX
    assert DRIVER.transfers == TRANSFERS[PlatformFamily.AOS_CX]
    assert DRIVER.device_hash == DEVICE_HASH[PlatformFamily.AOS_CX]


@pytest.mark.parametrize(("active", "target"), [("primary", "secondary"), ("secondary", "primary")])
def test_inspect_picks_the_other_bank(active: str, target: str) -> None:
    device = Device(active)
    job, _ = job_for(device)
    state = DRIVER.inspect(job)
    assert state.version == OLD and not state.staged and state.members == 1
    assert f"runs {OLD} from {active}; {target} holds FL.10.09.0001" == state.detail
    device.versions[target] = NEW
    assert DRIVER.inspect(job).staged


def test_inspect_counts_vsf_members() -> None:
    job, _ = job_for(Device(vsf=VSF_TWO))
    assert DRIVER.inspect(job).members == 2


@pytest.mark.parametrize(
    "output",
    ["", "Version : FL.1\nActive Image : primary\n", "AOS-CX Primary Image\nVersion : X\n"],
)
def test_unparseable_show_images_raises_and_quotes(output: str) -> None:
    device = Device()
    device.session.on(r"^show images$", output or "garbage line\n")
    job, _ = job_for(device)
    with pytest.raises(FirmwareError, match="show images"):
        DRIVER.inspect(job)
    device.session.on(r"^show images$", show_images(device.versions, "primary").split("Active")[0])
    with pytest.raises(FirmwareError, match="Default Image"):
        DRIVER.stage(job)
    assert not [c for c in device.session.commands if c.startswith("copy")]


def test_stage_already_staged_copies_nothing() -> None:
    device = Device()
    device.versions["secondary"] = NEW
    job, record = job_for(device)
    DRIVER.stage(job)
    assert not [c for c in device.session.commands if c.startswith("copy")]
    assert "already holds" in record.text()


@pytest.mark.parametrize(("vrf", "tail"), [(None, ""), ("mgmt", " vrf mgmt")])
def test_stage_copies_to_the_other_bank_and_answers(vrf: str | None, tail: str) -> None:
    device = Device()
    job, record = job_for(device, vrf=vrf)
    DRIVER.stage(job)
    command = f"copy {URL}/fl.swi secondary{tail}"
    assert device.session.commands == [
        "show images",
        command,
        "show images",
        "show images verify secondary",
    ]
    (call,) = device.session.interactive
    assert call[0] == command and (r"Continue \(y/n\)\?", "y") in call[1] and not call[2]
    assert device.session.timeouts[command] == 3600.0
    assert device.versions["primary"] == OLD
    assert "SHA-256 matches" in record.text()


def test_stage_logs_but_survives_a_hash_difference() -> None:
    device = Device()
    job, record = make_job(
        device.session,
        image=image("fl.swi", NEW, sha256="b" * 64),
        transfer=TransferMethod.TFTP,
        server=URL,
    )
    DRIVER.stage(job)
    assert "differs" in record.text()


def test_stage_fails_when_the_bank_lacks_the_version() -> None:
    device = Device()
    device.copies = False
    job, _ = job_for(device)
    with pytest.raises(FirmwareError, match="after the copy.*going down|after the copy"):
        DRIVER.stage(job)
    assert not any("verify" in c for c in device.session.commands)


def test_stage_verify_rejected_goes_on_and_invalid_raises() -> None:
    device = Device()
    device.valid = "% Unknown command.\n"
    job, _ = job_for(device)
    DRIVER.stage(job)
    device = Device()
    device.valid = "The image does not contain a signature\n"
    job, _ = job_for(device)
    with pytest.raises(FirmwareError, match="does not contain a signature"):
        DRIVER.stage(job)


def staged_device(**kw) -> Device:
    device = Device(**kw)
    device.versions["secondary"] = NEW
    return device


def test_activate_refuses_when_not_staged() -> None:
    device = Device()
    job, record = job_for(device)
    with pytest.raises(FirmwareError, match="stage step first"):
        DRIVER.activate(job)
    assert not [c for c in device.session.commands if c.startswith(("boot", "write"))]
    assert record.reconnects == 0


def test_activate_saves_boots_the_target_and_confirms() -> None:
    device = staged_device()
    job, record = job_for(device)
    DRIVER.activate(job)
    cmds = device.session.commands
    assert cmds.index("show images verify secondary") < cmds.index("write memory")
    assert cmds.index("write memory") < cmds.index("boot system secondary")
    command, answers, may_disconnect = device.session.interactive[-1]
    assert command == "boot system secondary" and may_disconnect
    assert answers[0][1] == "n" and "save" in answers[0][0]
    assert answers[1] == (r"Continue \(y/n\)\?", "y")
    assert record.reconnects == 1
    assert device.active == "secondary"


def test_activate_handles_a_session_that_ends() -> None:
    device = staged_device()
    device.session.disconnect_on = [r"^boot system"]
    job, record = job_for(device)
    device.after.on(r"^show images$", show_images({"primary": OLD, "secondary": NEW}, "secondary"))
    DRIVER.activate(job)
    assert record.reconnects == 1


def test_activate_old_bank_or_old_version_raises() -> None:
    device = staged_device()
    device.boots = False
    job, _ = job_for(device)
    with pytest.raises(FirmwareError, match="came back running"):
        DRIVER.activate(job)
    device = staged_device()
    job, _ = job_for(device)
    device.after.on(r"^show images$", show_images({"primary": OLD, "secondary": OLD}, "secondary"))
    with pytest.raises(FirmwareError, match="came back running"):
        DRIVER.activate(job)


def test_activate_reboot_aborted_does_not_reconnect() -> None:
    device = staged_device()
    device.boot_output = "Reboot aborted.\n"
    job, record = job_for(device)
    with pytest.raises(FirmwareError, match="aborted"):
        DRIVER.activate(job)
    assert record.reconnects == 0


def test_activate_device_gone_propagates() -> None:
    device = staged_device()
    job, _ = make_job(
        device.session, image=image("fl.swi", NEW), transfer=TransferMethod.TFTP, server=URL
    )
    with pytest.raises(DeviceGone):
        DRIVER.activate(job)


def test_activate_waits_for_vsf_members_and_names_the_missing_one() -> None:
    device = staged_device(vsf=VSF_TWO)
    job, record = job_for(device)
    booting = VSF_TWO.replace("Standby", "Booting")
    answers = iter([booting, booting, VSF_TWO])
    device.after.on(r"^show vsf$", lambda c: next(answers))
    DRIVER.activate(job)
    assert record.slept == [30.0, 30.0]
    device = staged_device(vsf=VSF_TWO)
    job, record = job_for(device)
    device.after.on(r"^show vsf$", booting)
    with pytest.raises(FirmwareError, match="member 2 is Booting"):
        DRIVER.activate(job)
    assert sum(record.slept) == 19 * 30.0
