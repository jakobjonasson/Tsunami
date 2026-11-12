"""Secrets with fields, secret and password prompts, missing-secret checks, and the
approval check behind an MCP change request (design.md, secret(); Play schema: prompts;
MCP interface: Approval flow)."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from tsunami.core.history import History
from tsunami.core.issues import Severity, TsunamiError
from tsunami.core.models.jobs import DeviceStatus, JobStatus
from tsunami.core.models.play import Prompt, PromptType, RunMode
from tsunami.core.redact import Redactor
from tsunami.core.render.renderer import Renderer, RenderError
from tsunami.core.render.secret_refs import MASK
from tsunami.core.rerun import rebuild_problem
from tsunami.core.runner import Runner, RunRequest
from tsunami.core.secret_store import (
    FIELDS_PREFIX,
    KeyringSecretStore,
    LoginCredential,
    SecretFields,
)
from tsunami.core.services import collect_prompts
from tsunami.core.transports.mock import MockTransport
from tsunami.core.workspace import load_workspace, validate_play_text

FIXTURE = Path(__file__).parents[1] / "fixtures" / "workspace"
AUTH_KEY = "Snmp-Auth-Key-31"
PRIV_KEY = "Snmp-Priv-Key-32"
TYPED = "Typed-Once-Pw-77"
SNMP_LINE = (
    "snmp-server user {{ snmp.username }} grp v3 auth {{ snmp.auth_protocol }} "
    "{{ snmp.auth_key }} priv {{ snmp.priv_protocol }} {{ snmp.priv_key }}"
)


@pytest.fixture
def ws_root(tmp_path) -> Path:
    root = tmp_path / "ws"
    shutil.copytree(FIXTURE, root)
    return root


@pytest.fixture
def secrets(tmp_path) -> KeyringSecretStore:
    store = KeyringSecretStore(index_path=tmp_path / "credential-index.json")
    store.set_login("acme-netadmin", LoginCredential("netadmin", "Acme-Login-Pw-1"))
    store.set_secret("acme", "tacacs", "Tacacs-Key-Value-4")
    store.set_secret_fields(
        "acme",
        "snmpv3",
        SecretFields(
            {
                "username": "monitor",
                "auth_protocol": "sha",
                "auth_key": AUTH_KEY,
                "priv_protocol": "aes 128",
                "priv_key": PRIV_KEY,
            },
            frozenset({"username", "auth_protocol", "priv_protocol"}),
        ),
    )
    return store


@pytest.fixture
def history(tmp_path):
    db = History(tmp_path / "history.db")
    yield db
    db.close()


def write_play(root: Path, body: str, name: str = "p") -> Path:
    relative = Path(f"plays/push/{name}.yml")
    (root / relative).write_text(
        "name: p\ntype: push\ncustomer: acme\ntargets:\n  devices: [sa-as01, sa-as02]\n" + body,
        encoding="utf-8",
    )
    return relative


def render(root, store, play, prompts=None, *, validation=False):
    ws = load_workspace(root, render=False)
    entry = ws.plays[play]
    values = collect_prompts(ws, entry, prompts or {}, device="sa-as01", secrets=store)
    return Renderer(ws, secrets=store).render(
        entry, device="sa-as01", prompt_values=values, validation=validation
    )


SNMP_PLAY = (
    "prompts:\n  snmp:\n    type: secret\n    default: snmpv3\n"
    f"steps:\n  - config:\n      - '{SNMP_LINE}'\n"
)


# -- the store ---------------------------------------------------------------------------


def test_fields_round_trip_and_never_show_in_repr(secrets):
    saved = secrets.get_secret_fields("acme", "snmpv3")
    assert saved.values["auth_key"] == AUTH_KEY
    assert saved.plain == {"username", "auth_protocol", "priv_protocol"}
    assert AUTH_KEY not in repr(saved) and "monitor" not in repr(saved)
    # A single value is still stored as the bare string, as before 0.6.0.
    assert secrets.get_secret_fields("acme", "tacacs").single
    assert not secrets.get_secret_fields("acme", "tacacs").encode().startswith(FIELDS_PREFIX)


def test_only_secret_fields_join_the_exact_value_layer(secrets):
    values = secrets.secret_values("acme")
    assert AUTH_KEY in values and PRIV_KEY in values
    assert "monitor" not in values and "sha" not in values  # plain fields would corrupt output
    assert "monitor" not in secrets.all_secret_values()


def test_get_secret_on_a_secret_with_fields_says_how_to_read_it(secrets):
    with pytest.raises(TsunamiError, match="prompt of type 'secret'"):
        secrets.get_secret("acme", "snmpv3")


@pytest.mark.parametrize(
    "fields",
    [
        SecretFields({}),
        SecretFields({"Bad-Name": "x"}),
        SecretFields({"key": ""}),
        SecretFields({"key": "a\nb"}),
    ],
)
def test_bad_fields_are_refused(secrets, fields):
    with pytest.raises(TsunamiError):
        secrets.set_secret_fields("acme", "bad", fields)


def test_a_single_value_cannot_pose_as_fields(secrets):
    with pytest.raises(TsunamiError):
        secrets.set_secret("acme", "x", FIELDS_PREFIX + '{"fields": {}}')


# -- rendering --------------------------------------------------------------------------


def test_a_secret_prompt_renders_plain_fields_and_masks_secret_ones(ws_root, secrets):
    rendering = render(ws_root, secrets, write_play(ws_root, SNMP_PLAY))
    assert rendering.masked_lines() == [
        f"snmp-server user monitor grp v3 auth sha {MASK} priv aes 128 {MASK}"
    ]
    assert set(rendering.secret_refs) == {"acme/snmpv3.auth_key", "acme/snmpv3.priv_key"}
    assert AUTH_KEY not in rendering.masked_text()


def test_the_two_argument_form_reads_one_field(ws_root, secrets):
    body = (
        "steps:\n  - config:\n      - \"snmp-server user {{ secret('snmpv3', 'username') }} "
        "grp v3 auth sha {{ secret('snmpv3', 'auth_key') }}\"\n"
    )
    rendering = render(ws_root, secrets, write_play(ws_root, body))
    assert rendering.masked_lines() == [f"snmp-server user monitor grp v3 auth sha {MASK}"]


@pytest.mark.parametrize(
    ("line", "match"),
    [
        ("snmp-server location {{ snmp.auth_key }}", "not allowed here"),
        ("snmp-server location {{ snmp.nope }}", "has no field 'nope'"),
        ("snmp-server community {{ snmp }} RO", "has fields"),
    ],
)
def test_secret_fields_obey_the_sink_rule(ws_root, secrets, line, match):
    body = (
        "prompts:\n  snmp:\n    type: secret\n    default: snmpv3\n"
        f"steps:\n  - config:\n      - '{line}'\n"
    )
    with pytest.raises(RenderError, match=match):
        render(ws_root, secrets, write_play(ws_root, body))


def test_a_missing_secret_fails_the_render_and_says_where_to_add_it(ws_root, secrets):
    body = "steps:\n  - config:\n      - \"snmp-server community {{ secret('nope') }} RO\"\n"
    play = write_play(ws_root, body)
    with pytest.raises(RenderError, match="not among the saved secrets of customer 'acme'"):
        render(ws_root, secrets, play)
    with pytest.raises(RenderError, match="Credentials, Secrets"):
        render(ws_root, secrets, play, validation=True)  # the runner's preflight render
    # Without the store (workspace validation) it is not an error yet.
    ws = load_workspace(ws_root)
    assert not [i for i in ws.issues_for(play) if i.severity == Severity.ERROR]


def test_validate_with_the_store_reports_a_missing_secret(ws_root, secrets):
    ws = load_workspace(ws_root)
    text = (
        "name: p\ntype: push\ncustomer: acme\ntargets:\n  devices: [sa-as01]\n"
        "steps:\n  - config:\n      - \"snmp-server community {{ secret('nope') }} RO\"\n"
    )
    issues = validate_play_text(ws, text, "drafts/plays/push/p.yml", secrets)
    assert any("not among the saved secrets" in i.message for i in issues)
    assert not validate_play_text(ws, text, "drafts/plays/push/p.yml")  # no store: no check


def test_a_secret_prompt_answer_must_be_saved(ws_root, secrets):
    play = write_play(ws_root, SNMP_PLAY)
    with pytest.raises(TsunamiError, match="not saved"):
        render(ws_root, secrets, play, {"snmp": "radius"})


PASSWORD_PLAY = (
    "prompts:\n  key:\n    type: password\n"
    "steps:\n  - config:\n      - 'snmp-server community {{ key }} RO'\n"
)


def test_a_password_prompt_is_a_masked_secret(ws_root, secrets):
    rendering = render(ws_root, secrets, write_play(ws_root, PASSWORD_PLAY), {"key": TYPED})
    assert rendering.masked_lines() == [f"snmp-server community {MASK} RO"]
    assert rendering.secret_refs == ("prompt:key",)
    assert TYPED not in rendering.masked_text()


def test_a_password_prompt_obeys_the_sink_rule(ws_root, secrets):
    body = (
        "prompts:\n  key:\n    type: password\n"
        "steps:\n  - config:\n      - 'snmp-server location {{ key }}'\n"
    )
    with pytest.raises(RenderError, match="prompt 'key' \\(type 'password'\\) is not allowed"):
        render(ws_root, secrets, write_play(ws_root, body), {"key": TYPED})


def test_a_password_never_feeds_another_prompts_default(ws_root, secrets):
    body = (
        "prompts:\n  key:\n    type: password\n  copy:\n    type: string\n"
        "    default: '{{ key }}'\n"
        "steps:\n  - config:\n      - 'snmp-server community {{ key }} RO'\n"
        "      - 'snmp-server location {{ copy }}'\n"
    )
    rendering = render(ws_root, secrets, write_play(ws_root, body), {"key": TYPED})
    assert TYPED not in "\n".join(rendering.masked_lines())


@pytest.mark.parametrize(
    "fields",
    [{"type": "password", "default": "x"}, {"type": "password", "per_device": True}],
)
def test_a_password_prompt_has_no_default_and_is_not_per_device(fields):
    with pytest.raises(ValueError):
        Prompt(**fields)


# -- runs ----------------------------------------------------------------------------------


def make_runner(root, store, history, app_paths, mock):
    events: list = []
    runner = Runner(
        load_workspace(root),
        transport=mock,
        history=history,
        secrets=store,
        paths=app_paths,
        on_event=events.append,
        heartbeat_seconds=0.05,
    )
    return runner, events


def mock_for(*names, echo: str = "") -> MockTransport:
    mock = MockTransport()
    for name in names:
        state = mock.add_device(name, running=[f"hostname {name}"])
        state.exec_responses["show users"] = echo
    return mock


def test_apply_sends_fields_and_the_typed_password_and_stores_neither(
    ws_root, secrets, history, app_paths
):
    body = (
        "prompts:\n  snmp:\n    type: secret\n    default: snmpv3\n  key:\n    type: password\n"
        f"steps:\n  - config:\n      - '{SNMP_LINE}'\n"
        "      - 'snmp-server community {{ key }} RO'\n  - exec: show users\n"
        "execution:\n  canary: false\n"
    )
    play = write_play(ws_root, body)
    mock = mock_for("sa-as01", "sa-as02", echo=f"echo {TYPED} {AUTH_KEY}")
    runner, events = make_runner(ws_root, secrets, history, app_paths, mock)
    job_id = runner.run(RunRequest(play, RunMode.APPLY, prompts={"key": TYPED}))
    assert history.get_job(job_id).status == JobStatus.COMPLETED, history.devices(job_id)
    running = mock.device("sa-as01").running
    assert f"snmp-server user monitor grp v3 auth sha {AUTH_KEY} priv aes 128 {PRIV_KEY}" in (
        running
    )
    assert f"snmp-server community {TYPED} RO" in running
    stored = [str(history.get_job(job_id))] + [str(d) for d in history.devices(job_id)]
    stored += [str(e) for e in events]
    for text in stored:
        assert TYPED not in text and AUTH_KEY not in text and PRIV_KEY not in text
    assert history.get_job(job_id).prompts == {"key": MASK}  # never the typed value


def test_a_run_with_a_missing_secret_fails_before_any_device(ws_root, secrets, history, app_paths):
    body = "steps:\n  - config:\n      - \"snmp-server community {{ secret('nope') }} RO\"\n"
    play = write_play(ws_root, body)
    mock = mock_for("sa-as01", "sa-as02")
    runner, _ = make_runner(ws_root, secrets, history, app_paths, mock)
    job_id = runner.run(RunRequest(play, RunMode.CHECK))
    job = history.get_job(job_id)
    assert job.status == JobStatus.FAILED
    assert "not among the saved secrets" in job.summary["error"]
    assert mock.max_concurrent_sessions == 0  # nothing connected


def test_rerun_never_replays_a_password(ws_root, secrets, history, app_paths):
    play = write_play(ws_root, PASSWORD_PLAY)
    runner, _ = make_runner(ws_root, secrets, history, app_paths, mock_for("sa-as01", "sa-as02"))
    job_id = runner.run(RunRequest(play, RunMode.CHECK, prompts={"key": TYPED}))
    problem = rebuild_problem(load_workspace(ws_root), history.get_job(job_id))
    assert problem is not None and "password" in problem


def test_the_exact_layer_masks_a_typed_password_echoed_by_a_device(
    ws_root, secrets, history, app_paths
):
    body = PASSWORD_PLAY + "  - exec: show users\nexecution:\n  canary: false\n"
    play = write_play(ws_root, body)
    mock = mock_for("sa-as01", "sa-as02", echo=f"line {TYPED}")
    runner, _ = make_runner(ws_root, secrets, history, app_paths, mock)
    job_id = runner.run(RunRequest(play, RunMode.APPLY, prompts={"key": TYPED}))
    for device in history.devices(job_id):
        assert TYPED not in (device.output_redacted or "")
    assert Redactor([TYPED]).text(f"x {TYPED}") != f"x {TYPED}"


# -- the approval check behind an MCP change request ----------------------------------------

CHANGE = "steps:\n  - config:\n      - vlan 40\nexecution:\n  max_fail_percent: 0\n"


def approval_check(ws_root, secrets, history, app_paths, mock):
    from tsunami.core.models.jobs import Actor

    play = write_play(ws_root, CHANGE)
    job_id = history.create_job(
        actor=Actor.MCP,
        play_path=play.as_posix(),
        play_hash="x",
        customer="acme",
        mode="apply",
        status=JobStatus.PENDING,
    )
    runner, _ = make_runner(ws_root, secrets, history, app_paths, mock)
    runner.run(RunRequest(play, RunMode.CHECK, job_id=job_id, approval_check=True))
    return job_id


def test_an_approval_check_marks_devices_ready_and_waits(ws_root, secrets, history, app_paths):
    mock = mock_for("sa-as01", "sa-as02")
    mock.device("sa-as02").running.append("vlan 40")
    job_id = approval_check(ws_root, secrets, history, app_paths, mock)
    assert history.get_job(job_id).status == JobStatus.AWAITING_APPROVAL
    states = {d.device: d.status for d in history.devices(job_id)}
    assert states == {"sa-as01": DeviceStatus.READY, "sa-as02": DeviceStatus.NO_CHANGES}
    assert mock.device("sa-as01").running == ["hostname sa-as01"]  # nothing applied


def test_a_failed_approval_check_ends_failed_and_is_never_offered(
    ws_root, secrets, history, app_paths
):
    mock = MockTransport(unreachable=["sa-as01"])
    mock.add_device("sa-as01", running=["hostname sa-as01"])
    mock.add_device("sa-as02", running=["hostname sa-as02"])
    job_id = approval_check(ws_root, secrets, history, app_paths, mock)
    job = history.get_job(job_id)
    assert job.status == JobStatus.FAILED  # not partial: nothing was applied
    assert "not offered for approval" in job.summary["error"]
    states = {d.device: d.status for d in history.devices(job_id)}
    # max_fail_percent 0 did not stop the check: sa-as02 was checked too, then cancelled.
    assert states == {"sa-as01": DeviceStatus.UNREACHABLE, "sa-as02": DeviceStatus.CANCELLED}
    assert DeviceStatus.SUCCESS not in states.values()


def test_an_approval_check_with_nothing_to_change_ends_no_changes(
    ws_root, secrets, history, app_paths
):
    mock = mock_for("sa-as01", "sa-as02")
    for name in ("sa-as01", "sa-as02"):
        mock.device(name).running.append("vlan 40")
    job_id = approval_check(ws_root, secrets, history, app_paths, mock)
    assert history.get_job(job_id).status == JobStatus.NO_CHANGES
    assert {d.status for d in history.devices(job_id)} == {DeviceStatus.NO_CHANGES}


# -- PromptType coverage -------------------------------------------------------------------


def test_every_prompt_type_has_a_placeholder():
    from tsunami.core.prompts import PromptContext, placeholder

    context = PromptContext(
        sites=frozenset({"hq"}),
        roles=frozenset({"core"}),
        model_ids=frozenset({"m"}),
        logins=frozenset({"l"}),
    )
    for ptype in PromptType:
        if ptype in (PromptType.INTERFACE, PromptType.CHOICE):
            continue
        assert placeholder(ptype, context=context) is not None
