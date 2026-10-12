"""MCP tools through the enforcement wrapper (design.md, MCP interface).

The tools run against a copy of the fixture workspace with a MockTransport. The
background jobs that check, compliance and request_run start are joined before the
assertions.
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import pytest

from tsunami.core.approvals import approve
from tsunami.core.history import History
from tsunami.core.models.jobs import Actor, JobStatus
from tsunami.core.models.policy import McpPolicy
from tsunami.core.models.settings import AppSettings, save_settings
from tsunami.core.policy import save_policy
from tsunami.core.secret_store import KeyringSecretStore, LoginCredential
from tsunami.core.transports.mock import MockTransport
from tsunami.core.workspace import load_workspace
from tsunami.mcp_server.enforcement import McpApp, ToolRefused
from tsunami.mcp_server.tools import Tools

FIXTURE = Path(__file__).parents[1] / "fixtures" / "workspace"
PLAY = "plays/push/add-vlan.yml"
BASELINE = "plays/baseline/acme-baseline.yml"
PROMPTS = {"vlan_id": "30", "vlan_name": "Cameras"}
LOGIN_PASSWORD = "Login-Pass-1234"
TACACS = "Tacacs-Key-98765"

LAB_CUSTOMER = """\
name: Lab
credential: lab-admin
lab: true
"""
LAB_DEVICES = """\
- name: lab-sw1
  host: 192.0.2.21
  site: bench
  role: access
  platform: ios-xe
"""


class RecordingTransport:
    """MockTransport that remembers each ConnectTarget, to check the host key policy."""

    def __init__(self, mock: MockTransport) -> None:
        self.mock = mock
        self.targets = []

    def open(self, target):
        self.targets.append(target)
        return self.mock.open(target)


class Env:
    def __init__(self, root, app, tools, history, mock, recorder, paths):
        self.root, self.app, self.tools = root, app, tools
        self.history, self.mock, self.recorder, self.paths = history, mock, recorder, paths

    def policy(self, **fields) -> None:
        save_policy(McpPolicy(enabled=True, **fields), self.paths.policy_file)

    def join(self) -> None:
        for thread in list(self.app.threads):
            thread.join(timeout=20)
            assert not thread.is_alive()

    def audit(self) -> list[dict]:
        rows = self.history._query("SELECT tool, status, args_redacted_json FROM mcp_calls")
        return [dict(r) for r in rows]


@pytest.fixture
def env(tmp_path, app_paths):
    root = tmp_path / "ws"
    shutil.copytree(FIXTURE, root)
    lab = root / "inventory" / "customers" / "lab"
    (lab / "sites").mkdir(parents=True)
    (lab / "customer.yml").write_text(LAB_CUSTOMER, encoding="utf-8")
    (lab / "devices.yml").write_text(LAB_DEVICES, encoding="utf-8")
    (lab / "sites" / "bench.yml").write_text("{}\n", encoding="utf-8")
    play = (root / PLAY).read_text(encoding="utf-8")
    (root / "plays/push/lab-vlan.yml").write_text(
        play.replace("customer: acme", "customer: lab"), encoding="utf-8"
    )
    save_settings(AppSettings(workspace=str(root)), app_paths.settings_file)
    save_policy(McpPolicy(enabled=True), app_paths.policy_file)

    history = History(tmp_path / "history.db")
    secrets = KeyringSecretStore(index_path=tmp_path / "idx.json")
    secrets.set_login("acme-netadmin", LoginCredential("netadmin", LOGIN_PASSWORD))
    secrets.set_login("lab-admin", LoginCredential("admin", "Lab-Pass-5555"))
    secrets.set_secret("acme", "tacacs", TACACS)
    mock = MockTransport()
    for name in ("sa-as01", "sa-as02", "lab-sw1"):
        state = mock.add_device(name, running=[f"hostname {name}"])
        state.exec_responses["re:^show vlan id"] = "30 Cameras active"
        state.exec_responses["show version"] = "Cisco IOS XE Software, Version 17.9.4"
        state.exec_responses["show running-config"] = (
            f"hostname {name}\ntacacs server t1\n key 7 {TACACS}\n"
            f"username netadmin secret 0 {LOGIN_PASSWORD}\n"
        )
    recorder = RecordingTransport(mock)
    app = McpApp(app_paths, history=history, secrets=secrets, transport_factory=lambda: recorder)
    yield Env(root, app, Tools(app), history, mock, recorder, app_paths)
    for thread in app.threads:
        thread.join(timeout=20)
    history.close()


# -- enforcement --------------------------------------------------------------------------


def test_disabled_policy_refuses_and_audits(env):
    save_policy(McpPolicy(enabled=False), env.paths.policy_file)
    with pytest.raises(ToolRefused, match="turned off"):
        env.tools.list_customers()
    assert env.audit() == [
        {"tool": "list_customers", "status": "refused", "args_redacted_json": "{}"}
    ]


def test_blocked_customer_looks_unknown(env):
    env.policy(customers_blocked=["acme"])
    assert [c["slug"] for c in env.tools.list_customers()["customers"]] == ["lab"]
    with pytest.raises(ToolRefused, match="unknown customer 'acme'"):
        env.tools.list_devices("acme")
    with pytest.raises(ToolRefused, match="unknown customer 'nobody'"):
        env.tools.list_devices("nobody")
    with pytest.raises(ToolRefused, match="unknown customer"):
        env.tools.read_play(PLAY)
    plays = [p["path"] for p in env.tools.list_plays()["plays"]]
    assert PLAY not in plays and "plays/push/lab-vlan.yml" in plays


def test_rate_limit(env):
    env.policy(rate_limit_per_minute=2)
    env.tools.list_customers()
    env.tools.list_customers()
    with pytest.raises(ToolRefused, match="rate limit"):
        env.tools.list_customers()


def test_unexpected_errors_do_not_leak(env, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError(f"password is {LOGIN_PASSWORD}")

    monkeypatch.setattr("tsunami.mcp_server.tools.render_play", boom)
    with pytest.raises(ToolRefused) as info:
        env.tools.render(PLAY, device="sa-as01", prompts=PROMPTS)
    assert str(info.value) == "internal error (RuntimeError); see mcp.log"
    assert env.audit()[-1]["status"] == "error"


def test_audit_args_are_redacted(env):
    content = f"name: x\ntype: push\ncustomer: acme\n# {LOGIN_PASSWORD}\n"
    env.tools.validate(content=content)
    row = env.audit()[-1]
    assert row["tool"] == "validate" and LOGIN_PASSWORD not in row["args_redacted_json"]


# -- tier 0 -----------------------------------------------------------------------------


def test_list_devices_filters(env):
    devices = env.tools.list_devices("acme", role="access")["devices"]
    assert [d["name"] for d in devices] == ["sa-as01", "sa-as02"]
    assert env.tools.list_devices("acme", site="nowhere")["devices"] == []


def test_list_plays_hides_drafts_unless_asked(env):
    (env.root / "drafts/plays/push").mkdir(parents=True)
    (env.root / "drafts/plays/push/try.yml").write_text(
        (env.root / PLAY).read_text(encoding="utf-8"), encoding="utf-8"
    )
    paths = {p["path"] for p in env.tools.list_plays()["plays"]}
    assert "drafts/plays/push/try.yml" not in paths and PLAY in paths
    drafts = {p["path"]: p for p in env.tools.list_plays(include_drafts=True)["plays"]}
    assert drafts["drafts/plays/push/try.yml"]["draft"] is True
    assert {p["path"] for p in env.tools.list_plays(type="baseline")["plays"]} == {BASELINE}


@pytest.mark.parametrize(
    "path", ["inventory/global.yml", "../ws/plays/push/add-vlan.yml", "C:/Windows/win.ini"]
)
def test_read_play_refuses_paths_outside_plays(env, path):
    with pytest.raises(ToolRefused):
        env.tools.read_play(path)


def test_read_play(env):
    result = env.tools.read_play("plays\\push\\add-vlan.yml")
    assert result["path"] == PLAY and "Add VLAN" in result["text"]


def test_snippets(env):
    listing = {(s["name"], s["level"]) for s in env.tools.list_snippets()["snippets"]}
    assert ("aaa", "customer") in listing and ("ntp", "common") in listing
    resolved = env.tools.list_snippets(customer="acme", platform="ios-xe")["snippets"]
    assert {s["name"]: s["level"] for s in resolved}["aaa"] == "customer"
    snippet = env.tools.read_snippet("aaa", customer="acme", platform="ios-xe")
    assert snippet["level"] == "customer" and "secret('acme/tacacs')" in snippet["content"]
    with pytest.raises(ToolRefused, match="not found"):
        env.tools.read_snippet("nope", customer="acme", platform="ios-xe")


def test_resolved_vars_with_provenance(env):
    result = env.tools.resolved_vars("acme", "sa-as01")
    assert result["values"]["domain"] == "acme.example.local"
    assert result["provenance"]["domain"]["file"].endswith("acme/customer.yml")
    assert result["provenance"]["mgmt_vlan"]["level"]


def test_validate_content_and_path(env):
    assert env.tools.validate(path=PLAY)["valid"] is True
    bad = "name: Broken\ntype: push\ncustomer: acme\ntargets: {roles: [access]}\nsteps: []\n"
    result = env.tools.validate(content=bad)
    assert result["valid"] is False and result["issues"]
    with pytest.raises(ToolRefused, match="exactly one"):
        env.tools.validate()


def test_render_masks_secrets(env):
    result = env.tools.render(BASELINE, device="sa-as01")
    assert TACACS not in str(result)
    assert "acme/tacacs" in result["secret_names"]
    assert any(line.startswith(" key ") for line in result["lines"])
    assert len(result["render_hash"]) == 64


def test_write_and_delete_drafts(env):
    play = (env.root / PLAY).read_text(encoding="utf-8")
    result = env.tools.write_draft("drafts/plays/push/new-vlan.yml", play)
    assert result["written"] and result["valid"]
    assert (env.root / "drafts/plays/push/new-vlan.yml").read_text(encoding="utf-8") == play

    snippet = env.tools.write_draft("drafts/snippets/common/ios/broken.j2", "{% if x %}\n")
    assert snippet["valid"] is False and snippet["issues"][0]["code"] == "template.syntax"

    with pytest.raises(ToolRefused):
        env.tools.write_draft("plays/push/sneaky.yml", play)
    with pytest.raises(ToolRefused, match="limited"):
        env.tools.write_draft("drafts/plays/push/huge.yml", "x" * (300 * 1024))

    assert env.tools.delete_draft("drafts/plays/push/new-vlan.yml")["deleted"]
    with pytest.raises(ToolRefused, match="no draft"):
        env.tools.delete_draft("drafts/plays/push/new-vlan.yml")


def test_write_draft_for_blocked_customer_is_refused(env):
    env.policy(customers_blocked=["acme"])
    play = (env.root / PLAY).read_text(encoding="utf-8")
    with pytest.raises(ToolRefused, match="unknown customer"):
        env.tools.write_draft("drafts/plays/push/x.yml", play)
    with pytest.raises(ToolRefused, match="unknown customer"):
        env.tools.write_draft("drafts/snippets/customers/acme/ios/x.j2", "logging host 1\n")


# -- tier 1 -----------------------------------------------------------------------------


def test_tier_one_refused_when_disabled(env):
    env.policy(customers_blocked=[])
    save_policy(McpPolicy(enabled=False), env.paths.policy_file)
    with pytest.raises(ToolRefused):
        env.tools.show("acme", "sa-as01", "show version")


def test_show_redacts_and_refuses_new_host_keys(env):
    result = env.tools.show("acme", "sa-as01", "show running-config")
    output = result["untrusted_device_output"]
    assert "hostname sa-as01" in output
    assert TACACS not in output and LOGIN_PASSWORD not in output
    assert env.recorder.targets[-1].allow_new_host_key is False


@pytest.mark.parametrize("command", ["sh run", "conf t", "show run | include x | include y"])
def test_show_refuses_commands_outside_the_allowlist(env, command):
    with pytest.raises(ToolRefused):
        env.tools.show("acme", "sa-as01", command)
    assert env.recorder.targets == []


def test_check_job(env):
    started = env.tools.check(PLAY, prompts=PROMPTS)
    assert started["devices"] == 2
    env.join()
    job = env.tools.get_job(started["job_id"])
    assert job["actor"] == "mcp" and job["status"] == "completed"
    assert {d["device"] for d in job["devices"]} == {"sa-as01", "sa-as02"}
    assert "vlan 30" in job["devices"][0]["untrusted_device_output"]["diff"]
    assert "backup_path" not in str(job)
    assert all(t.allow_new_host_key is False for t in env.recorder.targets)
    assert not any(kind == "config" for kind, _ in env.mock.device("sa-as01").sent)


def test_check_refuses_too_many_targets(env):
    env.policy(max_check_targets=1)
    with pytest.raises(ToolRefused, match="at most 1"):
        env.tools.check(PLAY, prompts=PROMPTS)


def test_compliance_only_for_baselines(env):
    with pytest.raises(ToolRefused, match="baseline"):
        env.tools.compliance(PLAY)
    env.tools.compliance(BASELINE)
    env.join()


def test_list_jobs_hides_blocked_customers(env):
    env.tools.check(PLAY, prompts=PROMPTS)
    env.join()
    assert len(env.tools.list_jobs()["jobs"]) == 1
    env.policy(customers_blocked=["acme"])
    assert env.tools.list_jobs()["jobs"] == []


# -- tier 2 -----------------------------------------------------------------------------


def test_request_run_waits_for_approval(env):
    started = env.tools.request_run(PLAY, ["sa-as01", "sa-as02"], prompts=PROMPTS)
    env.join()
    job = env.history.get_job(started["job_id"])
    assert job.status == JobStatus.AWAITING_APPROVAL and job.actor == Actor.MCP
    assert all(d.render_hash for d in env.history.devices(job.id))
    assert "vlan 30" not in env.mock.device("sa-as01").running

    request = approve(env.history, load_workspace(env.root), job.id, approved_by="tech1")
    assert request.approved_hashes and set(request.limit) == {"sa-as01", "sa-as02"}


def test_request_run_on_lab_customer_applies_directly(env):
    started = env.tools.request_run("plays/push/lab-vlan.yml", ["lab-sw1"], prompts=PROMPTS)
    assert started["status"] == "running"
    env.join()
    assert env.history.get_job(started["job_id"]).status == JobStatus.COMPLETED
    assert "vlan 30" in env.mock.device("lab-sw1").running


def test_lab_apply_needs_policy(env):
    env.policy(apply="never")
    started = env.tools.request_run("plays/push/lab-vlan.yml", ["lab-sw1"], prompts=PROMPTS)
    env.join()
    assert env.history.get_job(started["job_id"]).status == JobStatus.AWAITING_APPROVAL
    assert "vlan 30" not in env.mock.device("lab-sw1").running


def test_request_run_refusals(env):
    with pytest.raises(ToolRefused, match="console"):
        env.tools.request_run("plays/initial/acme-access-day0.yml", ["sa-as01"])
    with pytest.raises(ToolRefused, match="at least one"):
        env.tools.request_run(PLAY, [], prompts=PROMPTS)
    with pytest.raises(ToolRefused):
        env.tools.request_run(PLAY, ["not-a-device"], prompts=PROMPTS)
    (env.root / "drafts/plays/push").mkdir(parents=True)
    shutil.copy(env.root / PLAY, env.root / "drafts/plays/push/try.yml")
    with pytest.raises(ToolRefused, match="promote"):
        env.tools.request_run("drafts/plays/push/try.yml", ["sa-as01"], prompts=PROMPTS)
    assert env.history.list_jobs() == []


def test_cancel_job(env):
    started = env.tools.request_run(PLAY, ["sa-as01"], prompts=PROMPTS)
    env.join()
    assert env.tools.cancel_job(started["job_id"])["status"] == "cancelled"
    assert env.history.get_job(started["job_id"]).status == JobStatus.CANCELLED
    with pytest.raises(ToolRefused, match="not yet started"):
        env.tools.cancel_job(started["job_id"])

    gui_job = env.history.create_job(
        actor=Actor.GUI, play_path=PLAY, play_hash="x", customer="acme", mode="apply"
    )
    with pytest.raises(ToolRefused, match="over MCP"):
        env.tools.cancel_job(gui_job)
    with pytest.raises(ToolRefused, match="unknown job"):
        env.tools.cancel_job("nope")


# -- the MCP server -----------------------------------------------------------------------


def test_server_registers_tools_and_calls_them(env):
    from tsunami.mcp_server.server import build_server

    server = build_server(env.app)
    tools = {t.name: t for t in asyncio.run(server.list_tools())}
    assert len(tools) == 18
    assert tools["show"].annotations.read_only_hint is True
    assert tools["request_run"].annotations.read_only_hint is False

    result = asyncio.run(server.call_tool("list_customers", {}))
    assert "acme" in str(result)


def test_job_tools_respect_the_policy_before_looking_up_jobs(env):
    job_id = env.history.create_job(
        actor=Actor.MCP, play_path=PLAY, play_hash="x", customer="acme", mode="check"
    )
    save_policy(McpPolicy(enabled=False), env.paths.policy_file)
    with pytest.raises(ToolRefused, match="turned off"):
        env.tools.get_job("no-such-job")
    with pytest.raises(ToolRefused, match="turned off"):
        env.tools.cancel_job(job_id)
    assert [r["tool"] for r in env.audit()] == ["get_job", "cancel_job"]

    env.policy(customers_blocked=["acme"])
    for call in (env.tools.get_job, env.tools.cancel_job):
        with pytest.raises(ToolRefused) as info:
            call(job_id)
        assert str(info.value) == f"unknown job {job_id!r}"  # never names the customer


def test_list_snippets_for_one_customer_hides_other_customers(env):
    other = env.root / "snippets/customers/lab/ios"
    other.mkdir(parents=True)
    (other / "aaa.j2").write_text("aaa new-model\n", encoding="utf-8")
    paths = {s["path"] for s in env.tools.list_snippets(customer="acme")["snippets"]}
    assert "snippets/customers/acme/ios/aaa.j2" in paths
    assert "snippets/customers/lab/ios/aaa.j2" not in paths


# -- leak fixes (5.4 findings) ----------------------------------------------------------


def test_refusal_reason_reaches_the_client(env):
    from tsunami.mcp_server.server import build_server

    server = build_server(env.app)
    with pytest.raises(Exception, match="unknown customer 'nobody'"):
        asyncio.run(server.call_tool("list_devices", {"customer": "nobody"}))


def test_show_pipe_runs_locally_on_redacted_output(env):
    secret_line = f"snmp-server community {TACACS} RO"
    env.mock.device("sa-as01").exec_responses["show running-config"] = (
        f"hostname sa-as01\n{secret_line}\n"
    )
    hit = env.tools.show("acme", "sa-as01", "show running-config | include ^snmp-server")
    assert hit["untrusted_device_output"] == "snmp-server community <redacted> RO\n"
    assert "command" not in hit
    # A guess at the secret's first characters finds nothing: the filter never sees it.
    guess = env.tools.show("acme", "sa-as01", f"show running-config | include {TACACS[:3]}")
    assert guess["untrusted_device_output"] == ""
    assert ("exec", "show running-config") in env.mock.device("sa-as01").sent
    assert not any("|" in text for _, text in env.mock.device("sa-as01").sent)


def test_show_refuses_unsafe_pipe_patterns(env):
    with pytest.raises(ToolRefused, match="groups"):
        env.tools.show("acme", "sa-as01", "show running-config | include (a+)+$")
    assert env.recorder.targets == []


def test_guess_at_a_secret_is_answered_like_any_other_value(env):
    """Redacting the echo of a correct guess would confirm it."""

    def rendered(value):
        result = env.tools.render(PLAY, device="sa-as01", prompts={**PROMPTS, "vlan_name": value})
        return result["text"].replace(value, "VALUE")

    assert rendered(TACACS) == rendered("Xyz-Plain-12345")


def test_other_customers_secrets_are_redacted_too(env):
    env.app.secrets.set_secret("lab", "radius", "Lab-Radius-4433")
    env.mock.device("sa-as01").exec_responses["show version"] = "uptime Lab-Radius-4433\n"
    output = env.tools.show("acme", "sa-as01", "show version")["untrusted_device_output"]
    assert "Lab-Radius-4433" not in output


def test_audit_rows_redact_secrets_the_caller_supplied(env):
    env.tools.render(PLAY, device="sa-as01", prompts={**PROMPTS, "vlan_name": TACACS})
    row = env.audit()[-1]
    assert row["tool"] == "render" and TACACS not in row["args_redacted_json"]


def test_policy_can_let_claude_make_first_contact(env):
    env.tools.show("acme", "sa-as01", "show version")
    assert env.recorder.targets[-1].allow_new_host_key is False  # the default
    env.policy(trust_new_host_keys=True)
    env.tools.show("acme", "sa-as01", "show version")
    env.tools.check(PLAY, prompts=PROMPTS)
    env.join()
    assert all(t.allow_new_host_key for t in env.recorder.targets[1:])


def test_devices_read_ready_until_approved_and_applied(env):
    from tsunami.core.models.jobs import DeviceStatus
    from tsunami.core.runner import Runner

    started = env.tools.request_run(PLAY, ["sa-as01", "sa-as02"], prompts=PROMPTS)
    env.join()
    job = env.history.get_job(started["job_id"])
    assert {d.status for d in env.history.devices(job.id)} == {DeviceStatus.READY}
    assert job.summary == {"counts": {"ready": 2}}
    assert env.tools.get_job(job.id)["devices"][0]["status"] == "ready"

    request = approve(env.history, load_workspace(env.root), job.id, approved_by="tech1")
    runner = Runner(
        load_workspace(env.root),
        transport=env.mock,
        history=env.history,
        secrets=env.app.secrets,
        paths=env.paths,
        heartbeat_seconds=0.05,
    )
    runner.run(request)
    assert {d.status for d in env.history.devices(job.id)} == {DeviceStatus.SUCCESS}
    assert env.history.get_job(job.id).summary["counts"] == {"success": 2}


def test_denied_or_cancelled_requests_leave_no_ready_devices(env):
    from tsunami.core.approvals import deny
    from tsunami.core.models.jobs import DeviceStatus

    denied = env.tools.request_run(PLAY, ["sa-as01"], prompts=PROMPTS)["job_id"]
    cancelled = env.tools.request_run(PLAY, ["sa-as02"], prompts=PROMPTS)["job_id"]
    env.join()
    deny(env.history, denied, denied_by="tech1")
    env.tools.cancel_job(cancelled)
    for job_id in (denied, cancelled):
        assert {d.status for d in env.history.devices(job_id)} == {DeviceStatus.CANCELLED}


def test_request_that_would_change_nothing_ends_no_changes(env):
    from tsunami.core.models.jobs import DeviceStatus

    for name in ("sa-as01", "sa-as02"):  # VLAN 30 is already configured as rendered
        env.mock.device(name).running += ["vlan 30", " name Cameras"]
    started = env.tools.request_run(PLAY, ["sa-as01", "sa-as02"], prompts=PROMPTS)
    env.join()
    job = env.history.get_job(started["job_id"])
    assert job.status == JobStatus.NO_CHANGES and job.finished_at is not None
    assert {d.status for d in env.history.devices(job.id)} == {DeviceStatus.NO_CHANGES}
    assert job.summary == {"counts": {"no_changes": 2}}
    assert env.history.list_jobs(status=JobStatus.AWAITING_APPROVAL) == []


def test_mixed_request_approves_only_the_devices_that_change(env):
    from tsunami.core.models.jobs import DeviceStatus

    env.mock.device("sa-as01").running += ["vlan 30", " name Cameras"]
    started = env.tools.request_run(PLAY, ["sa-as01", "sa-as02"], prompts=PROMPTS)
    env.join()
    statuses = {d.device: d.status for d in env.history.devices(started["job_id"])}
    assert statuses == {"sa-as01": DeviceStatus.NO_CHANGES, "sa-as02": DeviceStatus.READY}
    request = approve(env.history, load_workspace(env.root), started["job_id"], approved_by="t")
    assert list(request.limit) == ["sa-as02"] and set(request.approved_hashes) == {"sa-as02"}


@pytest.mark.parametrize(
    ("diff", "render", "changes"),
    [
        ("", "! step 1: vlan\nvlan 30\n", False),
        ("+ vlan 30", "! step 1: vlan\nvlan 30\n", True),
        ("- ntp server 192.0.2.9", "! step 1: ntp\nntp server 192.0.2.1\n", False),  # extras stay
        ("", "! step 1: save\nexec: write memory\n", True),  # exec steps never show in a diff
        ("", "! step 1: aaa\n key ********\n", True),  # a rotated secret cannot be seen
    ],
)
def test_would_change(diff, render, changes):
    from types import SimpleNamespace

    from tsunami.core.approvals import would_change

    assert would_change(SimpleNamespace(diff_redacted=diff, render_masked=render)) is changes


def test_global_play_needs_a_customer_and_respects_blocking(env):
    text = (
        (env.root / PLAY).read_text(encoding="utf-8").replace("customer: acme", "customer: global")
    )
    (env.root / "plays/push/any-vlan.yml").write_text(text, encoding="utf-8")
    path = "plays/push/any-vlan.yml"
    with pytest.raises(ToolRefused, match="choose the customer"):
        env.tools.render(path, device="sa-as01", prompts=PROMPTS)
    rendered = env.tools.render(path, device="sa-as01", prompts=PROMPTS, customer="acme")
    assert "vlan 30" in rendered["lines"]
    started = env.tools.check(path, prompts=PROMPTS, customer="acme")
    env.join()
    job = env.history.get_job(started["job_id"])
    assert job.customer == "acme" and job.status == JobStatus.COMPLETED
    env.policy(customers_blocked=["acme"])
    with pytest.raises(ToolRefused, match="unknown customer"):
        env.tools.render(path, device="sa-as01", prompts=PROMPTS, customer="acme")
