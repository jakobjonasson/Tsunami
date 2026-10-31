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
from tsunami.mcp_server.tools import TOOL_NAMES, Tools

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


def test_new_ios_defaults_are_allowed(env):
    for command in (
        "show switch",
        "show power inline",
        "show license summary",
        "show vrrp brief",
        "show mac address-table",
    ):
        result = env.tools.request_allowlist_addition("ios", command, "needed")
        assert result["status"] == "already_allowed"


def test_allowlist_request_is_queued_not_applied(env):
    before = env.paths.policy_file.read_text(encoding="utf-8")
    result = env.tools.request_allowlist_addition("ios", "show  Ip  route", "routing check")
    assert result["status"] == "pending_approval" and result["command"] == "show ip route"
    again = env.tools.request_allowlist_addition("ios", "show ip route", "again")
    assert again["request_id"] == result["request_id"]
    assert env.paths.policy_file.read_text(encoding="utf-8") == before
    assert len(env.history.pending_allowlist_requests()) == 1
    with pytest.raises(ToolRefused):
        env.tools.show("acme", "sa-as01", "show ip route")


@pytest.mark.parametrize(
    "command",
    [
        "configure terminal",
        "write memory",
        "show ip route | tee x",
        "show a; reload",
        "show",
        "sh run",
    ],
)
def test_allowlist_request_refuses_non_show(env, command):
    with pytest.raises(ToolRefused):
        env.tools.request_allowlist_addition("ios", command, "please")
    assert env.history.pending_allowlist_requests() == []


def test_allowlist_request_refuses_bad_family_and_reason(env):
    with pytest.raises(ToolRefused):
        env.tools.request_allowlist_addition("junos", "show route", "x")
    with pytest.raises(ToolRefused):
        env.tools.request_allowlist_addition("ios", "show ip route", "")


def test_approving_a_request_adds_the_command(env):
    from tsunami.core.policy import add_allowlist_entry, load_policy

    assert add_allowlist_entry(env.paths.policy_file, "ios", "show ip route") is True
    assert add_allowlist_entry(env.paths.policy_file, "ios", "show ip route") is False
    assert "show ip route" in load_policy(env.paths.policy_file).show_allowlist["ios"]
    with pytest.raises(Exception):  # noqa: B017 - PolicyError
        add_allowlist_entry(env.paths.policy_file, "ios", "configure terminal")


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
    assert set(tools) == TOOL_NAMES  # every tool is registered, and nothing else
    assert tools["add_devices"].annotations.read_only_hint is False
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


# -- inventory additions --------------------------------------------------------------------

NEW_DEVICE = {
    "name": "sa-as09",
    "host": "192.0.2.99",
    "site": "site-a",
    "role": "access",
    "platform": "ios-xe",
}


def test_inventory_additions_are_off_by_default(env):
    for call in (
        lambda: env.tools.add_customer("beta", "Beta Inc", "beta-login"),
        lambda: env.tools.add_site("acme", "site-z"),
        lambda: env.tools.add_devices("acme", [NEW_DEVICE]),
    ):
        with pytest.raises(ToolRefused, match="turned off"):
            call()
    assert not (env.root / "inventory/customers/beta").exists()


def test_add_customer_is_never_a_lab_and_never_takes_another_customers_login(env):
    reloads = []
    env.app.on_inventory_changed = lambda: reloads.append(1)
    env.policy(inventory_edits=True)
    result = env.tools.add_customer("beta", "Beta Inc", "beta-login")
    assert result["created"] and result["lab"] is False
    cust = load_workspace(env.root).customers["beta"]
    assert (cust.config.lab, cust.config.credential, cust.config.name) == (
        False,
        "beta-login",
        "Beta Inc",
    )
    assert reloads == [1]
    with pytest.raises(ToolRefused, match="another customer"):
        env.tools.add_customer("gamma", "Gamma", "acme-netadmin")
    with pytest.raises(ToolRefused, match="control characters"):
        env.tools.add_customer("delta", "Delta\nlab: true", "delta-login")


def test_add_customer_answers_alike_for_taken_blocked_and_reserved_slugs(env):
    env.policy(inventory_edits=True, customers_blocked=["lab"])
    messages = set()
    for slug in ("acme", "lab", "global", "Bad Slug"):
        with pytest.raises(ToolRefused) as refused:
            env.tools.add_customer(slug, "X", f"x-{len(messages)}-login")
        messages.add(str(refused.value).replace(repr(slug), "SLUG"))
    assert len(messages) == 1 and "taken or not valid" in messages.pop()


def test_add_site_with_vars_and_no_secrets(env):
    env.policy(inventory_edits=True)
    env.tools.add_site("acme", "site-b", {"site_code": "SB", "gateway": "192.0.2.1"})
    ws = load_workspace(env.root)
    assert ws.customers["acme"].sites["site-b"] == {"site_code": "SB", "gateway": "192.0.2.1"}
    with pytest.raises(ToolRefused, match="already exists"):
        env.tools.add_site("acme", "site-b")
    with pytest.raises(ToolRefused, match="never hold secrets"):
        env.tools.add_site("acme", "site-c", {"snmp_community": "public"})
    with pytest.raises(ToolRefused):
        env.tools.add_site("acme", "site-d", {"customer": "other"})  # a built-in variable
    with pytest.raises(ToolRefused, match="unknown customer"):
        env.tools.add_site("nobody", "site-e")


def test_add_role_then_devices_use_it(env):
    env.policy(inventory_edits=True)
    device = {**NEW_DEVICE, "role": "plant-core"}
    env.tools.add_role("plant-core", {"tier": "plant"})
    assert load_workspace(env.root).roles["plant-core"] == {"tier": "plant"}
    result = env.tools.add_devices("acme", [device])
    assert result["imported"] == 1
    with pytest.raises(ToolRefused, match="already exists"):
        env.tools.add_role("plant-core")
    with pytest.raises(ToolRefused, match="built-in"):
        env.tools.add_role("core")
    with pytest.raises(ToolRefused, match="lowercase"):
        env.tools.add_role("Bad_Role")
    with pytest.raises(ToolRefused, match="never hold secrets"):
        env.tools.add_role("other-role", {"snmp_community": "public"})
    assert not (env.root / "inventory/roles/other-role.yml").exists()


def test_add_role_is_off_by_default(env):
    with pytest.raises(ToolRefused, match="turned off"):
        env.tools.add_role("plant-core")
    assert not (env.root / "inventory/roles/plant-core.yml").exists()


def test_add_devices_checks_rows_like_an_import(env):
    env.policy(inventory_edits=True)
    rows = [
        NEW_DEVICE,
        {
            "name": "sa-as10",
            "host": "192.0.2.100",
            "site": "site-a",
            "type": "Distribution",
            "platform": "ios-xe",
        },
        {**NEW_DEVICE, "name": "Bad Name"},
        {
            "name": "sa-as01",
            "host": "203.0.113.11",
            "site": "site-a",
            "role": "access",
            "platform": "ios-xe",
        },  # already present
        {**NEW_DEVICE, "name": "sa-as11", "host": "192.0.2.101", "credential": "lab-admin"},
        {**NEW_DEVICE, "name": "sb-as01", "host": "192.0.2.102", "site": "site-b"},
    ]
    result = env.tools.add_devices("acme", rows)
    statuses = {r["name"]: r["status"] for r in result["rows"]}
    assert statuses == {
        "sa-as09": "ok",
        "sa-as10": "ok",
        "Bad Name": "error",
        "sa-as01": "skipped",
        "sa-as11": "error",  # another customer's login
        "sb-as01": "error",  # unknown site without create_sites
    }
    assert result["imported"] == 2
    devices = {d.name: d for d in load_workspace(env.root).customers["acme"].devices}
    assert devices["sa-as10"].role == "distribution"
    assert env.history.mcp_devices("acme") == {"sa-as09": "192.0.2.99", "sa-as10": "192.0.2.100"}

    result = env.tools.add_devices(
        "acme",
        [{**NEW_DEVICE, "name": "sb-as01", "host": "192.0.2.102", "site": "site-b"}],
        create_sites=True,
    )
    assert result["sites_created"] == ["site-b"]
    with pytest.raises(ToolRefused, match="unknown field"):
        env.tools.add_devices("acme", [{**NEW_DEVICE, "password": "x"}])
    with pytest.raises(ToolRefused, match="non-empty"):
        env.tools.add_devices("acme", [])


def test_add_devices_for_a_blocked_customer_looks_unknown(env):
    env.policy(inventory_edits=True, customers_blocked=["acme"])
    with pytest.raises(ToolRefused, match="unknown customer"):
        env.tools.add_devices("acme", [NEW_DEVICE])


def test_claude_never_makes_first_contact_with_a_device_it_added(env):
    env.policy(inventory_edits=True, trust_new_host_keys=True)
    env.tools.add_devices("acme", [NEW_DEVICE])
    env.mock.add_device("sa-as09", running=["hostname sa-as09"])
    env.tools.show("acme", "sa-as09", "show version")
    assert env.recorder.targets[-1].allow_new_host_key is False
    env.tools.show("acme", "sa-as01", "show version")  # a device a person added
    assert env.recorder.targets[-1].allow_new_host_key is True
    env.tools.check(PLAY, targets=["sa-as01", "sa-as09"], prompts=PROMPTS)
    env.join()
    by_device = {t.device: t.allow_new_host_key for t in env.recorder.targets[2:]}
    assert by_device == {"sa-as01": True, "sa-as09": False}


def test_a_person_changing_the_host_lifts_the_first_contact_rule(env):
    from tsunami.core.inventory_edit import InventoryEditor

    env.policy(inventory_edits=True, trust_new_host_keys=True)
    env.tools.add_devices("acme", [NEW_DEVICE])
    InventoryEditor(env.root).update_device("acme", "sa-as09", {"host": "192.0.2.123"})
    env.mock.add_device("sa-as09", running=["hostname sa-as09"])
    env.tools.show("acme", "sa-as09", "show version")
    assert env.recorder.targets[-1].allow_new_host_key is True


# -- login() ----------------------------------------------------------------------------

ADMIN2_PASSWORD = "Admin2-Pass-4321"
LOGIN_PLAY = """name: add admin2
type: push
customer: acme
targets:
  devices: [sa-as01]
prompts:
  admin2:
    type: login
    default: netadmin2
steps:
  - config:
      - username {{ login(admin2).username }} privilege 15 secret {{ login(admin2).password }}
"""


@pytest.fixture
def login_env(env):
    customer = env.root / "inventory/customers/acme/customer.yml"
    customer.write_text(
        customer.read_text(encoding="utf-8") + "push_logins: [netadmin2]\n", encoding="utf-8"
    )
    (env.root / "plays/push/add-admin2.yml").write_text(LOGIN_PLAY, encoding="utf-8")
    env.app.secrets.set_login("netadmin2", LoginCredential("netadmin2", ADMIN2_PASSWORD))
    return env


def test_render_shows_the_username_and_masks_the_login_password(login_env):
    result = login_env.tools.render("plays/push/add-admin2.yml", device="sa-as01")
    [line] = result["lines"]  # the username in clear; MCP output redacts the mask again
    assert line.startswith("username netadmin2 privilege 15 secret ")
    assert result["secret_names"] == ["login:netadmin2"]
    assert ADMIN2_PASSWORD not in str(result) and LOGIN_PASSWORD not in str(result)


@pytest.mark.parametrize("name", ["lab-admin", "no-such-login", "acme-jump"])
def test_a_login_outside_the_customer_is_refused_alike(login_env, name):
    login_env.policy(customers_blocked=["lab"])  # lab-admin now belongs to a blocked customer
    with pytest.raises(ToolRefused) as caught:
        login_env.tools.render(
            "plays/push/add-admin2.yml", device="sa-as01", prompts={"admin2": name}
        )
    message = str(caught.value)
    assert f"login {name!r} is not available to this customer" in message
    assert "customer 'lab'" not in message and "blocked" not in message  # nor who has it


def test_add_devices_can_create_a_new_role_in_the_same_call(env):
    env.policy(inventory_edits=True)
    rows = [
        {**NEW_DEVICE, "role": "plant-core"},
        {**NEW_DEVICE, "name": "sa-as10", "platform": "junos", "role": "plant-edge"},
    ]
    result = env.tools.add_devices("acme", rows, create_roles=True)
    assert result["imported"] == 1
    assert result["roles_created"] == ["plant-core"]  # plant-edge's only row failed
    roles = env.root / "inventory/roles"
    assert (roles / "plant-core.yml").is_file()
    assert not (roles / "plant-edge.yml").exists()
    assert load_workspace(env.root).customers["acme"].device("sa-as09").role == "plant-core"


def test_add_devices_without_create_roles_refuses_an_unknown_role(env):
    env.policy(inventory_edits=True)
    result = env.tools.add_devices("acme", [{**NEW_DEVICE, "role": "plant-core"}])
    assert result["imported"] == 0 and result["roles_created"] == []
    assert "unknown type" in result["rows"][0]["reason"]
    assert not (env.root / "inventory/roles/plant-core.yml").exists()


def test_create_roles_never_redefines_a_built_in_type(env):
    env.policy(inventory_edits=True)
    result = env.tools.add_devices("acme", [{**NEW_DEVICE, "role": "core"}], create_roles=True)
    assert result["roles_created"] == []
    assert not (env.root / "inventory/roles/core.yml").exists()
