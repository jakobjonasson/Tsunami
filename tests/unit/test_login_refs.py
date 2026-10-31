"""login(): pushing a saved login's username and password from a play (design.md, Snippets
and rendering: login()). Who may push which login, the sink rule, validation while
locked, redaction, and the approval hash."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from tsunami.core.history import History
from tsunami.core.issues import TsunamiError
from tsunami.core.logins import allowed_logins, customer_logins, referenced_logins, refusal
from tsunami.core.models.jobs import DeviceStatus, JobStatus
from tsunami.core.models.play import RunMode
from tsunami.core.render.renderer import PLACEHOLDER_USERNAME, Renderer, RenderError
from tsunami.core.render.secret_refs import MASK
from tsunami.core.runner import Runner, RunRequest
from tsunami.core.secret_store import KeyringSecretStore, LoginCredential
from tsunami.core.services import collect_prompts, play_entry, render_play
from tsunami.core.transports.mock import MockTransport
from tsunami.core.vault import VaultLocked
from tsunami.core.workspace import load_workspace

FIXTURE = Path(__file__).parents[1] / "fixtures" / "workspace"
LOGIN_PW = "Acme-Login-Pw-1"
ADMIN2_PW = "Admin2-Push-Pw-7"
ADMIN2_ENABLE = "Admin2-Enable-Pw-8"
OTHER_PW = "Other-Push-Pw-9"
JUMP_PW = "Jump-Host-Pw-5"

PLAY = """name: add admin2
type: push
customer: {customer}
targets:
  devices: [sa-as01]
prompts:
  admin2:
    type: login
    default: netadmin2
steps:
{steps}
"""
ADD_USER = (
    "  - config:\n"
    "      - username {{ login(admin2).username }} privilege 15 algorithm-type scrypt "
    "secret {{ login(admin2).password }}\n"
)


@pytest.fixture
def ws_root(tmp_path) -> Path:
    root = tmp_path / "ws"
    shutil.copytree(FIXTURE, root)
    customer = root / "inventory/customers/acme/customer.yml"
    text = customer.read_text(encoding="utf-8").replace(
        "jump_host: null\n",
        "jump_host:\n  host: 198.51.100.10\n  credential: acme-jump\n",
    )
    customer.write_text(text + "push_logins: [netadmin2, spare-admin]\n", encoding="utf-8")
    other = root / "inventory/customers/globex"
    (other / "sites").mkdir(parents=True)
    (other / "customer.yml").write_text("name: Globex\ncredential: globex-admin\n", "utf-8")
    (other / "sites" / "hq.yml").write_text("{}\n", encoding="utf-8")
    (other / "devices.yml").write_text(
        "- name: gx-sw1\n  host: 192.0.2.50\n  site: hq\n  role: access\n  platform: ios-xe\n",
        encoding="utf-8",
    )
    return root


@pytest.fixture
def secrets(tmp_path) -> KeyringSecretStore:
    store = KeyringSecretStore(index_path=tmp_path / "credential-index.json")
    store.set_login("acme-netadmin", LoginCredential("netadmin", LOGIN_PW))
    store.set_login("netadmin2", LoginCredential("netadmin2", ADMIN2_PW, ADMIN2_ENABLE))
    store.set_login("spare-admin", LoginCredential("spare", OTHER_PW))
    store.set_login("acme-jump", LoginCredential("jumper", JUMP_PW))
    store.set_login("globex-admin", LoginCredential("gx", "Globex-Pw-3"))
    store.set_secret("acme", "tacacs", "Tacacs-Key-Value-4")
    return store


def write_play(root: Path, steps: str, *, name: str = "admin2", customer: str = "acme") -> Path:
    relative = Path(f"plays/push/{name}.yml")
    (root / relative).write_text(PLAY.format(customer=customer, steps=steps), encoding="utf-8")
    return relative


def render(root, secrets, play, prompts=None, **kwargs):
    ws = load_workspace(root, render=False)
    entry = ws.plays[play]
    values = collect_prompts(ws, entry, prompts or {}, device="sa-as01")
    return Renderer(ws, secrets=secrets).render(
        entry, device="sa-as01", prompt_values=values, **kwargs
    )


# -- which logins a customer may push --------------------------------------------------


def test_allowed_logins_are_the_customers_own_plus_push_logins(ws_root):
    ws = load_workspace(ws_root, render=False)
    acme = ws.customers["acme"]
    assert customer_logins(acme) == {"acme-netadmin"}  # not its jump host's
    assert allowed_logins(acme) == {"acme-netadmin", "netadmin2", "spare-admin"}
    assert "acme-jump" in referenced_logins(ws.customers.values())
    assert "globex-admin" in referenced_logins(ws.customers.values())


def test_username_is_plain_and_password_is_a_secret(ws_root, secrets):
    rendering = render(ws_root, secrets, write_play(ws_root, ADD_USER))
    assert rendering.masked_lines() == [
        f"username netadmin2 privilege 15 algorithm-type scrypt secret {MASK}"
    ]
    assert rendering.secret_refs == ("login:netadmin2/password",)
    steps = rendering.materialize({"login:netadmin2/password": ADMIN2_PW})
    assert steps[0].lines[0].endswith(f"secret {ADMIN2_PW}")
    assert ADMIN2_PW not in rendering.masked_text()


def test_a_literal_name_works_too(ws_root, secrets):
    steps = (
        "  - config:\n"
        "      - username {{ login('spare-admin').username }} privilege 15 "
        "secret {{ login('spare-admin').password }}\n"
    )
    rendering = render(ws_root, secrets, write_play(ws_root, steps))
    assert rendering.masked_lines() == [f"username spare privilege 15 secret {MASK}"]


@pytest.mark.parametrize(
    "line",
    [
        "interface GigabitEthernet1/0/1\n description {{ login(admin2).password }}",
        "banner motd ^ {{ login(admin2).password }} ^",
        "hostname {{ login(admin2).password }}",
        "snmp-server location {{ login(admin2).password }}",
    ],
)
def test_the_password_only_goes_on_password_lines(ws_root, secrets, line):
    (ws_root / "snippets/common/ios/s.j2").write_text(line + "\n", encoding="utf-8")
    play = write_play(ws_root, "  - snippet: s\n")
    with pytest.raises(RenderError, match="login\\(\\) password is not allowed here"):
        render(ws_root, secrets, play)


def test_the_password_is_refused_in_exec_steps_and_checks(ws_root, secrets):
    play = write_play(ws_root, "  - exec: show users | include {{ login(admin2).password }}\n")
    with pytest.raises(RenderError, match="not allowed in exec steps"):
        render(ws_root, secrets, play)
    check = "checks:\n  post:\n    - exec: show {{ login(admin2).password }}\n      expect: x\n"
    (ws_root / play).write_text(PLAY.format(customer="acme", steps=ADD_USER) + check, "utf-8")
    with pytest.raises(RenderError, match="not allowed in checks"):
        render(ws_root, secrets, play)


@pytest.mark.parametrize(
    "expression",
    [
        "login(admin2).password | reverse",
        "login(admin2).password[1:8]",
        "login(admin2).password | tojson",
        "login(admin2).password | upper",
    ],
)
def test_a_changed_password_placeholder_is_an_error(ws_root, secrets, expression):
    steps = f"  - config:\n      - username x privilege 15 secret {{{{ {expression} }}}}\n"
    with pytest.raises(RenderError, match="changed by a filter"):
        render(ws_root, secrets, write_play(ws_root, steps))


def test_enable_is_a_secret_and_missing_enable_names_the_login(ws_root, secrets):
    steps = "  - config:\n      - enable secret {{ login(admin2).enable }}\n"
    rendering = render(ws_root, secrets, write_play(ws_root, steps))
    assert rendering.masked_lines() == [f"enable secret {MASK}"]
    assert rendering.secret_refs == ("login:netadmin2/enable",)
    with pytest.raises(RenderError, match="login 'spare-admin' has no enable value"):
        render(ws_root, secrets, write_play(ws_root, steps), {"admin2": "spare-admin"})


# -- refusals read the same ------------------------------------------------------------


@pytest.mark.parametrize("name", ["globex-admin", "no-such-login", "acme-jump"])
def test_a_literal_login_outside_the_customer_fails_validation_alike(ws_root, secrets, name):
    steps = (
        f"  - config:\n      - username x privilege 15 secret {{{{ login('{name}').password }}}}\n"
    )
    play = write_play(ws_root, steps)
    with pytest.raises(RenderError) as caught:
        render(ws_root, secrets, play)
    assert refusal(name, "acme") in str(caught.value)
    issues = load_workspace(ws_root).issues_for(play)
    assert [i.code for i in issues] == ["render.failed"]
    assert refusal(name, "acme") in issues[0].message


@pytest.mark.parametrize("name", ["globex-admin", "no-such-login", "acme-jump"])
def test_a_prompt_answer_outside_the_customer_is_refused_alike(ws_root, name):
    play = write_play(ws_root, ADD_USER)
    ws = load_workspace(ws_root, render=False)
    entry = ws.plays[play]
    with pytest.raises(TsunamiError) as caught:
        collect_prompts(ws, entry, {"admin2": name}, device="sa-as01")
    assert f"login {name!r} is not available to this customer" in str(caught.value)


def test_a_prompt_default_outside_the_customer_fails_validation(ws_root):
    play = write_play(ws_root, ADD_USER)
    text = (
        (ws_root / play)
        .read_text(encoding="utf-8")
        .replace("default: netadmin2", "default: globex-admin")
    )
    (ws_root / play).write_text(text, encoding="utf-8")
    issues = load_workspace(ws_root).issues_for(play)
    assert "play.prompt_default" in [i.code for i in issues]


def test_a_global_play_is_checked_against_the_customer_it_runs_for(ws_root, secrets):
    write_play(ws_root, ADD_USER, customer="global")
    ws = load_workspace(ws_root, render=False)
    rendering = render_play(
        ws, "plays/push/admin2.yml", device="sa-as01", customer="acme", secrets=secrets
    )
    assert rendering.secret_refs == ("login:netadmin2/password",)
    globex_play = write_play(
        ws_root,
        "  - config:\n      - username x privilege 15 secret {{ login('netadmin2').password }}\n",
        name="literal",
        customer="global",
    )
    ws = load_workspace(ws_root, render=False)
    entry = play_entry(ws, globex_play, "globex")
    with pytest.raises(RenderError, match="not available to customer 'globex'"):
        Renderer(ws, secrets=secrets).render(entry, device="gx-sw1", prompt_values={})


# -- locked, validation, approvals -----------------------------------------------------


class Locked:
    def get_login(self, name):
        raise VaultLocked()


def test_validation_renders_never_read_the_saved_logins(ws_root):
    play = write_play(ws_root, ADD_USER)
    ws = load_workspace(ws_root)  # validation renders run here, with no store at all
    assert ws.issues_for(play) == []
    rendering = render(ws_root, Locked(), play, validation=True)
    assert rendering.masked_lines()[0].startswith(f"username {PLACEHOLDER_USERNAME} ")


def test_a_real_render_while_locked_fails_with_the_unlock_message(ws_root):
    with pytest.raises(RenderError, match="locked"):
        render(ws_root, Locked(), write_play(ws_root, ADD_USER))


def test_a_new_username_changes_the_render_hash_a_new_password_does_not(ws_root, secrets):
    play = write_play(ws_root, ADD_USER)
    first = render(ws_root, secrets, play).render_hash()
    secrets.set_login("netadmin2", LoginCredential("netadmin2", "Another-Pw-11"))
    assert render(ws_root, secrets, play).render_hash() == first
    secrets.set_login("netadmin2", LoginCredential("admin-two", ADMIN2_PW))
    assert render(ws_root, secrets, play).render_hash() != first


# -- runs ------------------------------------------------------------------------------


@pytest.fixture
def history(tmp_path):
    db = History(tmp_path / "history.db")
    yield db
    db.close()


def make_runner(root, secrets, history, app_paths, mock):
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
    return runner, events


def mock_device() -> MockTransport:
    mock = MockTransport()
    state = mock.add_device("sa-as01", running=["hostname sa-as01"])
    # A device echoing passwords somewhere no line rule looks: only the exact layer helps.
    state.exec_responses["show users"] = (
        f"netadmin2 note {ADMIN2_PW}\nspare note {OTHER_PW}\nlogin {LOGIN_PW}"
    )
    return mock


def test_apply_pushes_the_login_and_history_never_holds_a_password(
    ws_root, secrets, history, app_paths
):
    play = write_play(ws_root, ADD_USER + "  - exec: show users\n")
    mock = mock_device()
    runner, events = make_runner(ws_root, secrets, history, app_paths, mock)
    job_id = runner.run(RunRequest(play, RunMode.APPLY, prompts={"admin2": "netadmin2"}))
    assert history.get_job(job_id).status == JobStatus.COMPLETED, history.devices(job_id)
    pushed = f"username netadmin2 privilege 15 algorithm-type scrypt secret {ADMIN2_PW}"
    assert pushed in mock.device("sa-as01").running
    stored = [str(history.get_job(job_id))] + [str(d) for d in history.devices(job_id)]
    stored += [str(e) for e in events]
    for text in stored:
        for value in (ADMIN2_PW, OTHER_PW, LOGIN_PW, ADMIN2_ENABLE):
            assert value not in text  # pushed, merely allowed, and the connection login
    [device] = history.devices(job_id)
    assert "netadmin2 note" in device.output_redacted  # the username is not redacted
    assert history.get_job(job_id).prompts == {"admin2": "netadmin2"}  # only the name


def test_check_mode_shows_the_username_and_a_masked_password(ws_root, secrets, history, app_paths):
    play = write_play(ws_root, ADD_USER)
    mock = mock_device()
    runner, _ = make_runner(ws_root, secrets, history, app_paths, mock)
    job_id = runner.run(RunRequest(play, RunMode.CHECK))
    assert history.get_job(job_id).status == JobStatus.COMPLETED
    assert not any(line.startswith("username netadmin2") for line in mock.device("sa-as01").running)
    [device] = history.devices(job_id)
    assert f"secret {MASK}" in device.render_masked
    assert "username netadmin2" in device.render_masked


def test_a_changed_username_between_check_and_apply_needs_a_new_approval(
    ws_root, secrets, history, app_paths
):
    play = write_play(ws_root, ADD_USER)
    runner, _ = make_runner(ws_root, secrets, history, app_paths, mock_device())
    check = runner.run(RunRequest(play, RunMode.CHECK))
    approved = {d.device: d.render_hash for d in history.devices(check)}
    secrets.set_login("netadmin2", LoginCredential("admin-two", ADMIN2_PW))
    runner, _ = make_runner(ws_root, secrets, history, app_paths, mock_device())
    apply = runner.run(RunRequest(play, RunMode.APPLY, approved_hashes=approved))
    assert {d.status for d in history.devices(apply)} == {DeviceStatus.RENDER_CHANGED}


def test_a_refused_prompt_answer_stops_the_run_before_connecting(
    ws_root, secrets, history, app_paths
):
    play = write_play(ws_root, ADD_USER)
    mock = mock_device()
    runner, _ = make_runner(ws_root, secrets, history, app_paths, mock)
    job_id = runner.run(RunRequest(play, RunMode.APPLY, prompts={"admin2": "globex-admin"}))
    assert history.get_job(job_id).status == JobStatus.FAILED
    assert mock.connections == 0


def test_draft_reviews_highlight_login_calls():
    from tsunami.core.drafts import highlights

    found = highlights("  - username {{ login(admin2).username }} secret {{ secret('x') }}\n")
    assert [(n, kind, what) for n, kind, what in found] == [
        (1, "secret", "login()"),
        (1, "secret", "secret()"),
    ]
