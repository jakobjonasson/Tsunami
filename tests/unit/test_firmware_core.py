"""Upgrade plays, apart from the per-platform drivers (design.md, Firmware upgrades): the
play model and its rules, the firmware folder, the render line an approval is pinned to,
and the pipeline's firmware steps with a stand-in driver against the mock transport."""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

import pytest
from pydantic import ValidationError
from tests.unit.test_runner import FIXTURE, LOGIN, make_runner, statuses

from tsunami.core import pipeline
from tsunami.core.firmware import (
    DEVICE_HASH,
    TRANSFERS,
    DeviceGone,
    FirmwareError,
    ImageState,
    same_version,
    version_key,
)
from tsunami.core.firmware.library import file_hashes, list_images, resolve_image
from tsunami.core.history import History
from tsunami.core.models.common import PlatformFamily
from tsunami.core.models.jobs import DeviceStatus, JobStatus
from tsunami.core.models.play import (
    Firmware,
    FirmwareImage,
    PlayType,
    RollbackMode,
    RunMode,
    TransferMethod,
)
from tsunami.core.render.renderer import Renderer
from tsunami.core.runner import RunRequest
from tsunami.core.secret_store import KeyringSecretStore, LoginCredential
from tsunami.core.services import play_entry
from tsunami.core.transports.mock import MockTransport
from tsunami.core.workspace import load_workspace, validate_play_text

SHOW_VERSION = (Path(__file__).parents[1] / "fixtures/show/ios/c1101_show_version.txt").read_text(
    encoding="utf-8"
)
IMAGE = b"not really an image, but it hashes"
SHA256 = hashlib.sha256(IMAGE).hexdigest()
MD5 = hashlib.md5(IMAGE).hexdigest()  # noqa: S324 - the device's own check, not security
FILE = "c1100-universalk9.17.09.05.SPA.bin"

PLAY = f"""\
name: Upgrade access switches
type: upgrade
customer: acme
targets:
  roles: [access]
firmware:
  images:
    - file: {FILE}
      version: 17.09.05
      sha256: {SHA256}
      platforms: [ios-xe]
execution:
  canary: false
steps:
  - firmware: stage
    tags: [stage]
  - firmware: activate
    allow_destructive: true
    tags: [activate]
"""


@pytest.fixture
def ws_root(tmp_path) -> Path:
    root = tmp_path / "ws"
    shutil.copytree(FIXTURE, root)
    (root / "plays/upgrade").mkdir()
    return root


@pytest.fixture
def secrets(tmp_path) -> KeyringSecretStore:
    store = KeyringSecretStore(index_path=tmp_path / "credential-index.json")
    store.set_login("acme-netadmin", LoginCredential("netadmin", LOGIN))
    return store


@pytest.fixture
def history(tmp_path):
    db = History(tmp_path / "history.db")
    yield db
    db.close()


@pytest.fixture
def folder(app_paths) -> Path:
    """The firmware folder, holding the play's image."""
    app_paths.firmware_dir.mkdir(parents=True)
    (app_paths.firmware_dir / FILE).write_bytes(IMAGE)
    return app_paths.firmware_dir


def write_upgrade(root: Path, body: str = PLAY, name: str = "upgrade") -> str:
    relative = f"plays/upgrade/{name}.yml"
    (root / relative).write_text(body, encoding="utf-8")
    return relative


def issue_codes(root: Path, path: str) -> set[str]:
    return {i.code for i in load_workspace(root).issues_for(Path(path))}


class StandInDriver:
    """A firmware driver that keeps its device's state in a dict, to test the pipeline."""

    family = PlatformFamily.IOS
    transfers = TRANSFERS[PlatformFamily.IOS]
    device_hash = "md5"

    def __init__(self) -> None:
        self.running = "16.09.04"
        self.staged = False
        self.free: int | None = 10_000_000
        self.calls: list[str] = []
        self.stage_error: str | None = None
        self.gone = False

    def inspect(self, job) -> ImageState:
        self.calls.append("inspect")
        return ImageState(self.running, self.staged, f"runs {self.running}", self.free)

    def stage(self, job) -> None:
        self.calls.append("stage")
        if self.stage_error:
            raise FirmwareError(self.stage_error)
        assert job.image.local is not None and job.image.hash("md5") == MD5
        job.upload(job.image.local, f"flash:/{job.image.file}", None)
        self.staged = True

    def activate(self, job) -> None:
        self.calls.append("activate")
        if not self.staged:
            raise FirmwareError(f"{job.image.file} is not on the device: run the stage step first")
        job.session.send_interactive("reload", [(r"\[confirm\]", "")], may_disconnect=True)
        if self.gone:
            raise DeviceGone("the device did not come back within 30 minutes")
        job.reconnect()
        self.running = job.image.version


@pytest.fixture
def driver(monkeypatch) -> StandInDriver:
    stand_in = StandInDriver()
    monkeypatch.setattr(pipeline, "driver_for", lambda family: stand_in)
    monkeypatch.setattr(pipeline, "RELOAD_SETTLE_SECONDS", 0)
    monkeypatch.setattr(pipeline, "RECONNECT_EVERY_SECONDS", 0)
    return stand_in


def mock_devices(*names: str) -> MockTransport:
    mock = MockTransport()
    for name in names:
        state = mock.add_device(name, running=["hostname " + name])
        state.exec_responses["show version"] = SHOW_VERSION
        state.disconnect_on = ["^reload$"]
    return mock


# -- versions ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("a", "b", "same"),
    [
        ("17.09.05", "17.9.5", True),
        ("16.09.04", "16.9.4", True),
        ("17.09.05", "17.09.05a", False),
        ("WC.16.10.0009", "wc.16.10.9", True),
        ("FL.10.13.1000", "FL.10.13.1010", False),
        ("15.2(7)E10", "15.2(7)E10", True),
        ("15.2(7)E10", "15.2(7)E1", False),
        ("", "", False),
        (None, "17.9.5", False),
    ],
)
def test_same_version(a, b, same):
    assert same_version(a, b) is same


def test_version_key_keeps_letters_and_numbers_apart():
    assert version_key("15.2(7)E10") == (15, 2, 7, "e", 10)


# -- the play model -----------------------------------------------------------------------


def image_fields(**changes):
    base = {"file": FILE, "version": "17.09.05", "sha256": SHA256, "platforms": ["ios-xe"]}
    return {**base, **changes}


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"file": "images/x.bin"}, "plain file name"),
        ({"file": "..\\x.bin"}, "plain file name"),
        ({"file": "x..bin"}, "plain file name"),
        ({"sha256": None}, "needs a hash"),
        ({"sha256": "abc"}, "64 hexadecimal"),
        ({"md5": "z" * 32}, "32 hexadecimal"),
        ({"platforms": []}, "at least 1"),
        ({"platforms": ["junos"]}, "platforms"),
    ],
)
def test_image_model_refuses(changes, message):
    with pytest.raises(ValidationError, match=message):
        FirmwareImage(**image_fields(**changes))


def test_an_unquoted_all_digit_hash_is_explained():
    with pytest.raises(ValidationError, match="put the hash in quotes"):
        FirmwareImage(**image_fields(md5=12345678901234567890123456789012))


def test_image_hashes_are_normalized():
    image = FirmwareImage(**image_fields(sha256=SHA256.upper(), md5=f" {MD5} "))
    assert image.hashes == {"md5": MD5, "sha256": SHA256}


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({"transfer": "tftp"}, "needs server"),
        ({"transfer": "scp", "server": "tftp://192.0.2.50"}, "server is for tftp and http"),
        ({"transfer": "tftp", "server": "http://192.0.2.50"}, "needs a tftp server"),
        ({"transfer": "http", "server": "tftp://192.0.2.50"}, "needs a http or https server"),
        ({"transfer": "http", "server": "http://user:pw@192.0.2.50"}, "username or password"),
        ({"transfer": "http", "server": "http://192.0.2.50/x?a=1"}, "plain URL"),
        ({"transfer": "tftp", "server": "tftp://192.0.2.50:69/x"}, "takes no port"),
        ({"transfer": "tftp", "server": "ftp://192.0.2.50"}, "server is tftp://"),
        ({"reload_timeout": 10}, "greater than or equal to 300"),
        ({"vrf": "mgmt; reload"}, "vrf"),
    ],
)
def test_firmware_model_refuses(fields, message):
    with pytest.raises(ValidationError, match=message):
        Firmware(images=[image_fields()], **fields)


def test_firmware_server_and_image_choice():
    firmware = Firmware(
        transfer="http",
        server="https://192.0.2.50:8443/images/",
        vrf="Mgmt-vrf",
        images=[
            image_fields(file="cat9300.bin", models=["catalyst-9300"]),
            image_fields(file="any-xe.bin"),
            image_fields(file="cx.swi", platforms=["aos-cx"]),
        ],
    )
    assert firmware.server == "https://192.0.2.50:8443/images"
    assert firmware.image_for("ios-xe", "catalyst-9300").file == "cat9300.bin"
    assert firmware.image_for("ios-xe", "catalyst-9200").file == "any-xe.bin"
    assert firmware.image_for("ios-xe", None).file == "any-xe.bin"
    assert firmware.image_for("aos-cx", None).file == "cx.swi"
    assert firmware.image_for("aos-s", None) is None


def test_upgrade_defaults_are_one_at_a_time_and_stop_at_the_first_failure(ws_root):
    path = write_upgrade(ws_root)
    play = play_entry(load_workspace(ws_root), path).play
    settings = play.settings(workspace_batch_size=5)
    assert play.type == PlayType.UPGRADE
    assert (settings.batch_size, settings.max_fail_percent) == (1, 0)
    assert settings.rollback == RollbackMode.NONE
    assert settings.gather_facts and settings.backup


@pytest.mark.parametrize(
    ("old", "new", "code"),
    [
        ("    allow_destructive: true\n", "", "play.firmware_reload"),
        ("execution:\n", "execution:\n  rollback: on_failure\n", "play.firmware_rollback"),
        ("execution:\n", "execution:\n  gather_facts: false\n", "play.firmware_facts"),
        ("platforms: [ios-xe]", "platforms: [aos-cx]", "play.firmware_no_image"),
        ("firmware:\n", "firmware:\n  transfer: tftp\n  server: tftp://192.0.2.50\n", None),
        (
            "  - firmware: stage\n    tags: [stage]\n",
            "",
            None,
        ),
    ],
)
def test_upgrade_play_rules(ws_root, old, new, code):
    assert old in PLAY
    path = write_upgrade(ws_root, PLAY.replace(old, new))
    codes = issue_codes(ws_root, path)
    if code is None:
        assert not {c for c in codes if c.startswith("play.firmware")}
    else:
        assert code in codes


def test_firmware_steps_must_be_in_order_and_only_in_upgrade_plays(ws_root):
    stage = "  - firmware: stage\n    tags: [stage]\n"
    activate = "  - firmware: activate\n    allow_destructive: true\n    tags: [activate]\n"
    swapped = PLAY.replace(stage + activate, activate + stage)
    assert "play.firmware_order" in issue_codes(ws_root, write_upgrade(ws_root, swapped))
    twice = PLAY.replace(stage, stage + stage)
    assert "play.firmware_repeat" in issue_codes(ws_root, write_upgrade(ws_root, twice))

    push = PLAY.replace("type: upgrade", "type: push")
    (ws_root / "plays/push/not-upgrade.yml").write_text(push, encoding="utf-8")
    assert "play.firmware_type" in issue_codes(ws_root, "plays/push/not-upgrade.yml")

    none = PLAY.replace(stage + activate, "  - exec: show version\n")
    assert "play.firmware_steps" in issue_codes(ws_root, write_upgrade(ws_root, none))


def test_a_transfer_the_platform_cannot_do_fails_validation(ws_root):
    devices = ws_root / "inventory/customers/acme/devices.yml"
    devices.write_text(devices.read_text().replace("platform: ios-xe", "platform: aos-cx"))
    play = PLAY.replace("platforms: [ios-xe]", "platforms: [aos-cx]")
    result = validate_play_text(load_workspace(ws_root), play, Path("plays/upgrade/x.yml"))
    messages = [i.message for i in result if i.code == "play.firmware_transfer"]
    assert messages and "transfer: scp is not available on aos-cx (use tftp)" in messages[0]


def test_every_family_declares_its_transfers_and_hash():
    assert set(TRANSFERS) == set(DEVICE_HASH) == set(PlatformFamily)
    assert TransferMethod.SCP not in TRANSFERS[PlatformFamily.AOS_CX]


# -- the firmware folder ----------------------------------------------------------------------


def test_resolve_image_checks_every_hash_the_play_gives(folder):
    spec = FirmwareImage(**image_fields())
    found = resolve_image(folder, spec, need_file=True)
    assert found.local == folder / FILE and found.size == len(IMAGE)
    assert found.hashes == file_hashes(folder / FILE)
    assert found.hash("md5") == MD5

    wrong = FirmwareImage(**image_fields(md5="0" * 32))
    with pytest.raises(FirmwareError, match="not the file the play names: its md5 is"):
        resolve_image(folder, wrong, need_file=True)


def test_a_missing_file_matters_only_for_an_upload(folder):
    spec = FirmwareImage(**image_fields(file="other.bin", md5=MD5))
    with pytest.raises(FirmwareError, match="other.bin is not in the firmware folder"):
        resolve_image(folder, spec, need_file=True)
    found = resolve_image(folder, spec, need_file=False)
    assert found.local is None and found.size is None
    assert found.hashes == {"md5": MD5, "sha256": SHA256}


def test_a_changed_file_is_hashed_again(folder):
    before = file_hashes(folder / FILE)
    (folder / FILE).write_bytes(IMAGE + b" changed")
    assert file_hashes(folder / FILE) != before


def test_list_images(folder, tmp_path):
    (folder / "sub").mkdir()
    listed = list_images(folder)
    assert [(f.name, f.size, f.hashes["sha256"]) for f in listed] == [(FILE, len(IMAGE), SHA256)]
    assert list_images(tmp_path / "nowhere") == []


# -- the render line ----------------------------------------------------------------------------


def test_the_render_names_the_image_and_pins_its_hash(ws_root):
    path = write_upgrade(ws_root)
    ws = load_workspace(ws_root)
    rendering = Renderer(ws).render(play_entry(ws, path), device="sa-as01", validation=True)
    text = rendering.masked_text()
    assert f"firmware: stage {FILE} version 17.09.05 sha256:{SHA256} via scp" in text
    assert f"firmware: activate {FILE}" in text
    assert rendering.masked_lines() == []  # nothing here is configuration to diff

    other = PLAY.replace(SHA256, "f" * 64)
    ws2_path = write_upgrade(ws_root, other, "other")
    ws2 = load_workspace(ws_root)
    changed = Renderer(ws2).render(play_entry(ws2, ws2_path), device="sa-as01", validation=True)
    assert changed.render_hash() != rendering.render_hash()


def test_a_tag_filter_leaves_the_other_step_out(ws_root):
    path = write_upgrade(ws_root)
    ws = load_workspace(ws_root)
    rendering = Renderer(ws).render(
        play_entry(ws, path), device="sa-as01", tags=["stage"], validation=True
    )
    assert [s.skipped for s in rendering.steps] == [None, "tags"]


# -- through the pipeline -----------------------------------------------------------------------


def test_stage_and_activate(ws_root, history, secrets, app_paths, folder, driver):
    path = write_upgrade(ws_root)
    mock = mock_devices("sa-as01", "sa-as02")
    mock.device("sa-as01").fail_connect_times = 0
    runner, _ = make_runner(ws_root, mock, history, secrets, app_paths)

    def after_first_connect(name: str) -> None:
        mock.device(name).fail_connect_times = 2  # still booting on the first two tries

    driver_activate = driver.activate

    def activate(job):
        after_first_connect(job.session._state.name)
        driver.staged = True
        driver_activate(job)

    driver.activate = activate
    job_id = runner.run(RunRequest(path, RunMode.APPLY, limit=["sa-as01"]))

    row = history.devices(job_id)[0]
    assert row.status == DeviceStatus.SUCCESS, row.error_redacted
    assert driver.calls == ["inspect", "stage", "inspect", "activate"]
    state = mock.device("sa-as01")
    assert state.uploads == {f"flash:/{FILE}": len(IMAGE)}
    assert ("exec", "reload") in state.sent
    # The config is saved on the session opened after the reload, not the dead one.
    assert state.sent[-1] == ("exec", "write memory")
    log = row.output_redacted
    assert "firmware: runs 16.09.04" in log
    assert "sa-as01 is back after" in log
    assert "firmware activate: the device runs 17.09.05" in log
    assert row.backup_path


def test_a_device_already_on_the_version_is_left_alone(
    ws_root, history, secrets, app_paths, folder, driver
):
    driver.running = "17.9.5"
    path = write_upgrade(ws_root)
    mock = mock_devices("sa-as01")
    runner, _ = make_runner(ws_root, mock, history, secrets, app_paths)
    job_id = runner.run(RunRequest(path, RunMode.APPLY, limit=["sa-as01"]))

    assert statuses(history, job_id) == {"sa-as01": DeviceStatus.SUCCESS}
    assert driver.calls == ["inspect", "inspect"]
    assert ("exec", "reload") not in mock.device("sa-as01").sent
    assert "already runs 17.09.05; nothing to do" in history.devices(job_id)[0].output_redacted


def test_only_the_tagged_step_runs(ws_root, history, secrets, app_paths, folder, driver):
    path = write_upgrade(ws_root)
    mock = mock_devices("sa-as01")
    runner, _ = make_runner(ws_root, mock, history, secrets, app_paths)
    job_id = runner.run(RunRequest(path, RunMode.APPLY, limit=["sa-as01"], tags=["stage"]))

    assert statuses(history, job_id) == {"sa-as01": DeviceStatus.SUCCESS}
    assert driver.calls == ["inspect", "stage"]
    assert driver.staged and driver.running == "16.09.04"


def test_a_failed_stage_stops_the_run_at_that_device(
    ws_root, history, secrets, app_paths, folder, driver
):
    driver.stage_error = "the device refused the copy: No space left on device"
    path = write_upgrade(ws_root)
    mock = mock_devices("sa-as01", "sa-as02")
    runner, _ = make_runner(ws_root, mock, history, secrets, app_paths)
    job_id = runner.run(RunRequest(path, RunMode.APPLY))

    assert statuses(history, job_id) == {
        "sa-as01": DeviceStatus.FAILED,
        "sa-as02": DeviceStatus.SKIPPED_MAXFAIL,
    }
    assert "firmware stage: the device refused the copy" in (
        history.devices(job_id)[0].error_redacted
    )
    assert history.get_job(job_id).status == JobStatus.FAILED


def test_a_device_that_does_not_come_back_raises_the_alarm(
    ws_root, history, secrets, app_paths, folder, driver
):
    driver.staged = True
    driver.gone = True
    path = write_upgrade(ws_root)
    mock = mock_devices("sa-as01", "sa-as02")
    runner, _ = make_runner(ws_root, mock, history, secrets, app_paths)
    job_id = runner.run(RunRequest(path, RunMode.APPLY, tags=["activate"]))

    assert statuses(history, job_id) == {
        "sa-as01": DeviceStatus.INTERRUPTED,
        "sa-as02": DeviceStatus.SKIPPED_MAXFAIL,
    }
    row = history.devices(job_id)[0]
    assert "did not come back within 30 minutes" in row.error_redacted
    assert row.backup_path  # the backup taken before the reload is still on record


def test_reconnect_gives_up_at_the_reload_timeout(
    ws_root, history, secrets, app_paths, folder, driver, monkeypatch
):
    driver.staged = True
    path = write_upgrade(ws_root)
    mock = mock_devices("sa-as01")
    clock = iter(range(0, 100_000, 400))
    monkeypatch.setattr(pipeline, "_monotonic", lambda: next(clock))
    activate = driver.activate

    def never_back(job):
        mock.device("sa-as01").fail_connect_times = 10_000
        activate(job)

    driver.activate = never_back
    runner, _ = make_runner(ws_root, mock, history, secrets, app_paths)
    job_id = runner.run(RunRequest(path, RunMode.APPLY, limit=["sa-as01"], tags=["activate"]))

    row = history.devices(job_id)[0]
    assert row.status == DeviceStatus.INTERRUPTED
    assert "did not come back within 30 minutes" in row.error_redacted
    assert "last attempt" in row.error_redacted


def test_check_mode_says_what_an_upgrade_would_do(
    ws_root, history, secrets, app_paths, folder, driver
):
    path = write_upgrade(ws_root)
    mock = mock_devices("sa-as01", "sa-as02")
    runner, _ = make_runner(ws_root, mock, history, secrets, app_paths)
    job_id = runner.run(RunRequest(path, RunMode.CHECK))

    rows = {d.device: d for d in history.devices(job_id)}
    assert rows["sa-as01"].status == DeviceStatus.SUCCESS
    diff = rows["sa-as01"].diff_redacted
    assert f"+ firmware stage: 16.09.04 -> 17.09.05 with {FILE} (not on the device yet)" in diff
    assert "+ firmware activate: 16.09.04 -> 17.09.05" in diff
    assert pipeline.changes_device(diff, rows["sa-as01"].render_masked)
    assert driver.calls == ["inspect", "inspect"]  # nothing staged, nothing reloaded


def test_check_mode_reports_no_change_when_the_version_is_right(
    ws_root, history, secrets, app_paths, folder, driver
):
    driver.running = "17.09.05"
    path = write_upgrade(ws_root)
    mock = mock_devices("sa-as01")
    runner, _ = make_runner(ws_root, mock, history, secrets, app_paths)
    job_id = runner.run(RunRequest(path, RunMode.CHECK, limit=["sa-as01"]))

    row = history.devices(job_id)[0]
    assert row.status == DeviceStatus.SUCCESS
    assert not pipeline.changes_device(row.diff_redacted, row.render_masked)


def test_check_mode_fails_a_device_without_room_for_the_image(
    ws_root, history, secrets, app_paths, folder, driver
):
    driver.free = 5
    path = write_upgrade(ws_root)
    mock = mock_devices("sa-as01")
    runner, _ = make_runner(ws_root, mock, history, secrets, app_paths)
    job_id = runner.run(RunRequest(path, RunMode.CHECK, limit=["sa-as01"]))

    row = history.devices(job_id)[0]
    assert row.status == DeviceStatus.FAILED
    assert "not enough free space" in row.error_redacted


def test_activating_what_was_never_staged_fails_the_check(
    ws_root, history, secrets, app_paths, folder, driver
):
    path = write_upgrade(ws_root)
    mock = mock_devices("sa-as01")
    runner, _ = make_runner(ws_root, mock, history, secrets, app_paths)
    job_id = runner.run(RunRequest(path, RunMode.CHECK, limit=["sa-as01"], tags=["activate"]))

    row = history.devices(job_id)[0]
    assert row.status == DeviceStatus.FAILED
    assert "run the stage step first" in row.error_redacted


def test_a_missing_or_swapped_image_stops_the_run_before_any_connection(
    ws_root, history, secrets, app_paths, driver
):
    path = write_upgrade(ws_root)
    mock = mock_devices("sa-as01", "sa-as02")
    runner, _ = make_runner(ws_root, mock, history, secrets, app_paths)
    job_id = runner.run(RunRequest(path, RunMode.APPLY))
    job = history.get_job(job_id)
    assert job.status == JobStatus.FAILED
    assert "is not in the firmware folder" in str(job.summary)
    assert mock.connections == 0

    app_paths.firmware_dir.mkdir(parents=True)
    (app_paths.firmware_dir / FILE).write_bytes(b"another file under the same name")
    job_id = runner.run(RunRequest(path, RunMode.CHECK))
    assert "not the file the play names" in str(history.get_job(job_id).summary)
    assert mock.connections == 0


def test_a_pull_needs_the_hash_the_device_can_check(ws_root, history, secrets, app_paths, driver):
    pull = PLAY.replace("firmware:\n", "firmware:\n  transfer: tftp\n  server: tftp://192.0.2.50\n")
    path = write_upgrade(ws_root, pull)
    mock = mock_devices("sa-as01")
    runner, _ = make_runner(ws_root, mock, history, secrets, app_paths)
    job_id = runner.run(RunRequest(path, RunMode.APPLY, limit=["sa-as01"]))
    assert "verifies an image by its md5" in str(history.get_job(job_id).summary)
    assert mock.connections == 0

    with_md5 = pull.replace(f"sha256: {SHA256}", f"sha256: {SHA256}\n      md5: {MD5}")
    path = write_upgrade(ws_root, with_md5, "with-md5")
    runner, _ = make_runner(ws_root, mock, history, secrets, app_paths)
    driver.stage = lambda job: driver.calls.append("stage")
    job_id = runner.run(RunRequest(path, RunMode.APPLY, limit=["sa-as01"], tags=["stage"]))
    assert statuses(history, job_id) == {"sa-as01": DeviceStatus.SUCCESS}
