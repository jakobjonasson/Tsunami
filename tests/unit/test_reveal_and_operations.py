"""0.6.1: canary priority, operations in History, and the core of press-to-reveal."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from tsunami.core import reveal
from tsunami.core.backups import write_backup
from tsunami.core.events import CanaryWaiting
from tsunami.core.history import History
from tsunami.core.inventory_edit import InventoryEditor
from tsunami.core.issues import TsunamiError
from tsunami.core.models.jobs import Actor, DeviceStatus, JobStatus
from tsunami.core.models.play import RunMode
from tsunami.core.operations import (
    HOSTKEYS_MODE,
    LABELS,
    SHOW_MODE,
    TEST_MODE,
    OperationLog,
    is_operation,
    record_host_keys,
    record_test,
)
from tsunami.core.redact import Redactor
from tsunami.core.render.renderer import Renderer
from tsunami.core.render.secret_refs import MASK
from tsunami.core.runner import Runner, RunRequest
from tsunami.core.secret_store import KeyringSecretStore, LoginCredential, SecretFields
from tsunami.core.services import collect_prompts
from tsunami.core.transports.mock import MockTransport
from tsunami.core.workspace import load_workspace

FIXTURE = Path(__file__).parents[1] / "fixtures" / "workspace"
COMMUNITY = "Community-Value-71"
AUTH = "Auth-Key-Value-72"


@pytest.fixture
def ws_root(tmp_path) -> Path:
    root = tmp_path / "ws"
    shutil.copytree(FIXTURE, root)
    return root


@pytest.fixture
def secrets(tmp_path) -> KeyringSecretStore:
    store = KeyringSecretStore(index_path=tmp_path / "idx.json")
    store.set_login("acme-netadmin", LoginCredential("netadmin", "Login-Pw-73"))
    store.set_secret("acme", "ro", COMMUNITY)
    store.set_secret_fields(
        "acme",
        "snmpv3",
        SecretFields({"username": "mon", "auth_key": AUTH}, frozenset({"username"})),
    )
    return store


@pytest.fixture
def history(tmp_path):
    db = History(tmp_path / "history.db")
    yield db
    db.close()


def set_priorities(root: Path, **priorities: int) -> None:
    editor = InventoryEditor(root)
    for name, value in priorities.items():
        editor.update_device("acme", name.replace("_", "-"), {"canary_priority": value})


PLAY = "plays/push/p.yml"


def write_play(root: Path, body: str = "steps:\n  - config:\n      - vlan 40\n") -> str:
    (root / PLAY).write_text(
        "name: p\ntype: push\ncustomer: acme\ntargets:\n  devices: [sa-as01, sa-as02]\n" + body,
        encoding="utf-8",
    )
    return PLAY


def run_apply(root, secrets, history, app_paths):
    mock = MockTransport()
    for name in ("sa-as01", "sa-as02"):
        mock.add_device(name, running=[f"hostname {name}"])
    events: list = []
    runner = Runner(
        load_workspace(root),
        transport=mock,
        history=history,
        secrets=secrets,
        paths=app_paths,
        on_event=events.append,
        heartbeat_seconds=0.05,
    )
    job_id = runner.run(RunRequest(PLAY, RunMode.APPLY))
    return job_id, events


# -- canary priority -------------------------------------------------------------------


def test_the_highest_canary_priority_goes_first(ws_root, secrets, history, app_paths):
    write_play(ws_root)
    set_priorities(ws_root, sa_as02=200)
    job_id, events = run_apply(ws_root, secrets, history, app_paths)
    assert [e.device for e in events if isinstance(e, CanaryWaiting)] == ["sa-as02"]
    assert history.get_job(job_id).status == JobStatus.COMPLETED


def test_priority_zero_is_never_the_canary(ws_root, secrets, history, app_paths):
    write_play(ws_root)
    set_priorities(ws_root, sa_as01=0)  # sa-as02 keeps the default, 100
    _, events = run_apply(ws_root, secrets, history, app_paths)
    assert [e.device for e in events if isinstance(e, CanaryWaiting)] == ["sa-as02"]


def test_every_target_at_zero_refuses_a_canary_run(ws_root, secrets, history, app_paths):
    write_play(ws_root)
    set_priorities(ws_root, sa_as01=0, sa_as02=0)
    job_id, _ = run_apply(ws_root, secrets, history, app_paths)
    job = history.get_job(job_id)
    assert job.status == JobStatus.FAILED and "canary_priority 0" in job.summary["error"]


def test_the_only_target_runs_even_at_zero(ws_root, secrets, history, app_paths):
    (ws_root / PLAY).write_text(
        "name: p\ntype: push\ncustomer: acme\ntargets:\n  devices: [sa-as01]\n"
        "steps:\n  - config:\n      - vlan 40\n",
        encoding="utf-8",
    )
    set_priorities(ws_root, sa_as01=0)
    job_id, _ = run_apply(ws_root, secrets, history, app_paths)
    assert history.get_job(job_id).status == JobStatus.COMPLETED


def test_the_default_priority_stays_out_of_devices_yml(ws_root):
    set_priorities(ws_root, sa_as01=100, sa_as02=7)
    text = (ws_root / "inventory/customers/acme/devices.yml").read_text(encoding="utf-8")
    assert text.count("canary_priority") == 1 and "canary_priority: 7" in text


# -- operations in history -------------------------------------------------------------


def test_an_operation_is_a_job_history_can_hide(history):
    record = OperationLog(
        history, mode=SHOW_MODE, customer="acme", actor=Actor.MCP, devices=["sa-as01"]
    )
    record.device("sa-as01", DeviceStatus.SUCCESS, output="show version\nok")
    record.finish(command="show version")
    job = history.get_job(record.job_id)
    assert (job.play_path, job.mode, job.status) == (
        LABELS[SHOW_MODE],
        SHOW_MODE,
        JobStatus.COMPLETED,
    )
    assert job.summary["command"] == "show version" and is_operation(job.mode)
    assert not is_operation("apply") and is_operation("drift")


def test_host_keys_and_tests_are_recorded(history):
    from tsunami.core.hostkey_trust import KeyProbe, Outcome

    record_host_keys(
        history,
        "acme",
        [
            KeyProbe("sa-as01", "203.0.113.11", 22, Outcome.RECORDED, "SHA256:x"),
            KeyProbe("sa-as02", "203.0.113.12", 22, Outcome.UNREACHABLE, detail="timed out"),
        ],
        None,
    )
    record_test(history, "acme", "sa-as01", "acme-netadmin", True, "Logged in")
    jobs = {j.mode: j for j in history.list_jobs()}
    assert jobs[HOSTKEYS_MODE].status == JobStatus.PARTIAL
    assert jobs[TEST_MODE].status == JobStatus.COMPLETED
    rows = {d.device: d.status for d in history.devices(jobs[HOSTKEYS_MODE].id)}
    assert rows == {"sa-as01": DeviceStatus.SUCCESS, "sa-as02": DeviceStatus.UNREACHABLE}


def test_a_history_failure_never_stops_the_operation():
    class Broken:
        def create_job(self, **_):
            raise RuntimeError("disk full")

    record = OperationLog(Broken(), mode=SHOW_MODE, customer="acme", devices=["x"])
    record.device("x", DeviceStatus.SUCCESS)
    record.finish()  # no exception


# -- reveal: preview ---------------------------------------------------------------------


def test_masked_text_spans_match_masked_text(ws_root, secrets):
    write_play(
        ws_root,
        "steps:\n  - config:\n"
        "      - \"snmp-server community {{ secret('ro') }} RO\"\n"
        "      - \"snmp-server user {{ secret('snmpv3', 'username') }} g v3 auth sha "
        "{{ secret('snmpv3', 'auth_key') }}\"\n",
    )
    ws = load_workspace(ws_root, render=False)
    entry = ws.plays[Path(PLAY)]
    values = collect_prompts(ws, entry, {}, device="sa-as01", secrets=secrets)
    rendering = Renderer(ws, secrets=secrets).render(entry, device="sa-as01", prompt_values=values)
    text, raw_spans = rendering.masked_text_spans()
    assert text == rendering.masked_text()
    spans = reveal.preview_spans(text, raw_spans)
    assert [text[s.start : s.end] for s in spans] == [MASK, MASK]
    assert [s.ref for s in spans] == ["acme/ro", "acme/snmpv3.auth_key"]
    assert [s.item for s in spans] == ["secret()", "secret() field 'auth_key'"]
    assert reveal.preview_value(secrets, spans[0].ref, {}) == COMMUNITY
    assert reveal.preview_value(secrets, spans[1].ref, {}) == AUTH
    assert reveal.preview_value(secrets, "login:acme-netadmin/password", {}) == "Login-Pw-73"
    assert reveal.preview_value(secrets, "prompt:key", {"key": "Typed-74"}) == "Typed-74"


# -- reveal: backups ---------------------------------------------------------------------


def test_backup_tokens_map_to_their_own_values(app_paths, secrets):
    raw = f"hostname sw1\nsnmp-server community {COMMUNITY} RO\nusername a secret 0 Pw-Value-75\n"
    path = write_backup(app_paths, "acme", "sa-as01", raw, "drift", keep=30)
    shown = Redactor([COMMUNITY]).text(raw)
    spans = reveal.backup_spans(shown)
    assert [s.item for s in spans] == ["line 2, token 1", "line 3, token 1"]
    root = app_paths.backups_dir
    assert reveal.backup_value(path, root, reveal.line_text(shown, 1), spans[0].ref) == COMMUNITY
    assert reveal.backup_value(path, root, reveal.line_text(shown, 2), spans[1].ref) == (
        "Pw-Value-75"
    )
    with pytest.raises(TsunamiError, match="cannot be matched"):
        reveal.backup_value(path, root, "something else <redacted>", spans[0].ref)


def test_a_reveal_is_audited_with_the_masked_line_only(history):
    span = reveal.MaskSpan(23, 31, "acme/ro", 0, "secret()")
    shown = f"snmp-server community {MASK} RO"
    reveal.audit(
        history,
        revealed_by="tech1",
        kind=reveal.PREVIEW,
        customer="acme",
        device="sa-as01",
        span=span,
        context="preview of plays/push/p.yml",
        shown_text=shown,
    )
    [row] = history.list_reveals()
    assert row["revealed_by"] == "tech1" and row["item"] == "secret()"
    assert row["masked_line"] == shown and COMMUNITY not in str(row)
    with pytest.raises(TsunamiError, match="record"):
        reveal.audit(
            None,
            revealed_by="tech1",
            kind=reveal.PREVIEW,
            customer="acme",
            device=None,
            span=span,
            context="x",
            shown_text=shown,
        )
