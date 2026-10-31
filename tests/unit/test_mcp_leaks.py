"""Blind adversarial tests: no secret value may come out of any MCP tool.

The server is driven through the real MCPServer object, the way a client would. Every
result (content blocks, structured content, error text) is serialized and searched for
the secret values stored in the (in-memory) keyring, in a few common transformations.
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
import yaml

from tsunami.core.history import History
from tsunami.core.models.policy import McpPolicy
from tsunami.core.models.settings import AppSettings, save_settings
from tsunami.core.policy import save_policy
from tsunami.core.secret_store import KeyringSecretStore, LoginCredential
from tsunami.core.transports.mock import MockTransport
from tsunami.mcp_server.enforcement import McpApp
from tsunami.mcp_server.server import build_server

FIXTURES = Path(__file__).parents[1] / "fixtures"
CONFIGS = FIXTURES / "configs"

LOGIN_PW = "Zq9-Login-7731"
ENABLE_PW = "En4-Enable-8842"
TACACS = "Tk4-Tacacs-5520"
RADIUS = "Rd6-Radius-3319"
SNMP = "Sn8-Snmp-4417"
ODD = "Qx\"7\\-p'é€-9921"  # quotes, backslash, non-ASCII
BETA_PW = "Bt3-BetaLogin-9905"
BETA_KEY = "Bt5-BetaKey-2206"
LAB_PW = "Lb7-LabLogin-6602"
LAB_KEY = "Lt2-LabTacacs-1180"

ACME_SECRETS = {
    "tacacs": TACACS,
    "radius": RADIUS,
    "snmp": SNMP,
    "odd": ODD,
}
ALL_VALUES = [
    LOGIN_PW,
    ENABLE_PW,
    *ACME_SECRETS.values(),
    BETA_PW,
    BETA_KEY,
    LAB_PW,
    LAB_KEY,
]
ACME_VALUES = [LOGIN_PW, ENABLE_PW, *ACME_SECRETS.values()]

FAMILY_DEVICE = {"ios": "sa-as01", "aos-cx": "sa-cx01", "aos-s": "sa-sw01"}
FAMILY_PLATFORM = {"ios": "ios-xe", "aos-cx": "aos-cx", "aos-s": "aos-s"}


def _own_prefixes() -> list[str]:
    """First one and two words of every top-level line in the fixture configs."""
    found: dict[str, None] = {}
    for cfg in sorted(CONFIGS.glob("*/*.txt")):
        for line in cfg.read_text(encoding="utf-8").splitlines():
            if not line.strip() or line[:1].isspace():
                continue
            words = line.split()
            found[words[0]] = None
            if len(words) > 1:
                found[" ".join(words[:2])] = None
    return list(found)


# Baseline steps that own (and so report as extra) the top-level running-config lines
# starting with these prefixes: every command word the fixture configs use.
OWN_EVERYTHING = _own_prefixes()


# ---------------------------------------------------------------------------------------
# harness
# ---------------------------------------------------------------------------------------


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for k, v in value.items() for s in (*_strings(k), *_strings(v))]
    if isinstance(value, list | tuple):
        return [s for v in value for s in _strings(v)]
    return []


def variants(value: str) -> dict[str, str]:
    raw = value.encode()
    return {
        "exact": value,
        "reversed": value[::-1],
        "base64": base64.b64encode(raw).decode(),
        "hex": raw.hex(),
        "hex-upper": raw.hex().upper(),
    }


@dataclass
class Outcome:
    tool: str
    result: Any = None
    exc: BaseException | None = None

    @property
    def refused(self) -> bool:
        return self.exc is not None or bool(getattr(self.result, "is_error", False))

    @property
    def data(self) -> Any:
        assert self.result is not None, f"{self.tool} raised {self.exc!r}"
        return self.result.structured_content

    @property
    def error_text(self) -> str:
        return " | ".join(self._exception_chain()) if self.exc else ""

    def _exception_chain(self) -> list[str]:
        out, seen, cur = [], set(), self.exc
        while cur is not None and id(cur) not in seen:
            seen.add(id(cur))
            out += [str(cur), repr(cur)]
            cur = cur.__cause__
        return out

    def haystacks(self) -> list[str]:
        """Every textual form a client could see: raw dump, decoded strings, squashed."""
        raw: list[str] = []
        decoded: list[str] = []
        if self.result is not None:
            raw.append(self.result.model_dump_json())
            raw.append(json.dumps(self.result.structured_content, ensure_ascii=False))
            decoded += _strings(self.result.structured_content)
            for block in self.result.content:
                decoded.append(getattr(block, "text", "") or "")
                try:
                    decoded += _strings(json.loads(getattr(block, "text", "") or "null"))
                except ValueError:
                    pass
        raw += self._exception_chain()
        decoded += self._exception_chain()
        joined = "\n".join(decoded)
        squashed = re.sub(r"\s+", "", joined)
        return [*raw, joined, squashed, re.sub(r"\s+", "", "".join(raw))]

    def text(self) -> str:
        return "\n".join(self.haystacks())


def leaked(
    outcome: Outcome,
    values: list[str] | None = None,
    *,
    exclude: tuple[str, ...] = (),
    sentinel: bool = True,
) -> list[str]:
    """Secrets (exact and transformed) and the sentinel marker found in an outcome.

    `exclude` lists values the caller itself supplied in the call: echoing those is policy.
    """
    found = []
    haystacks = outcome.haystacks()
    for value in ALL_VALUES if values is None else values:
        if value in exclude:
            continue
        for kind, needle in variants(value).items():
            if any(needle in h for h in haystacks):
                found.append(f"{value!r} ({kind})")
    text = outcome.text()
    if sentinel and ("\x00" in text or "SECRET:" in text or "\\u0000" in text):
        found.append("internal secret sentinel")
    return found


def assert_clean(
    outcome: Outcome,
    values: list[str] | None = None,
    *,
    exclude: tuple[str, ...] = (),
    sentinel: bool = True,
) -> None:
    found = leaked(outcome, values, exclude=exclude, sentinel=sentinel)
    assert not found, f"{outcome.tool} leaked {found}:\n{outcome.text()[:3000]}"


VOLATILE = {
    "job_id", "created_at", "started_at", "finished_at", "heartbeat_at", "render_hash", "id", "ts",
    "duration_ms", "backup", "approved_at",
}  # fmt: skip


def twin(value: str) -> str:
    """A same-length value with the same character classes that is not a stored secret."""
    out = []
    for ch in value:
        if ch.isdigit():
            out.append("7")
        elif ch.isalpha() and ch.isupper():
            out.append("Q")
        elif ch.isalpha():
            out.append("q")
        else:
            out.append(ch)
    result = "".join(out)
    assert result != value and result not in ALL_VALUES
    return result


def _scrub(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _scrub(v) for k, v in value.items() if k not in VOLATILE}
    if isinstance(value, list):
        return [_scrub(v) for v in value]
    return value


def shape(outcome: Outcome, guess: str) -> str:
    """The response with the guess substituted out: equal shapes mean no oracle."""
    if outcome.result is not None:
        body: Any = ["ok", bool(outcome.result.is_error), _scrub(outcome.result.structured_content)]
        if body[1]:
            body.append(" ".join(getattr(c, "text", "") for c in outcome.result.content))
    else:
        body = ["raised", str(outcome.exc), str(outcome.exc.__cause__) if outcome.exc else ""]
    text = json.dumps(body, sort_keys=True, ensure_ascii=False)
    forms = [guess, repr(guess)[1:-1]]  # as typed, and as a Python repr in a message
    forms += [json.dumps(f, ensure_ascii=False)[1:-1] for f in forms] + [json.dumps(guess)[1:-1]]
    for form in sorted(set(forms), key=len, reverse=True):
        text = text.replace(form, "<GUESS>")
    return text


@dataclass
class Env:
    root: Path
    tmp: Path
    paths: Any
    history: History
    secrets: KeyringSecretStore
    mock: MockTransport
    app: McpApp
    server: Any
    outcomes: list[Outcome] = field(default_factory=list)

    def call(self, tool: str, **args: Any) -> Outcome:
        try:
            result = asyncio.run(self.server.call_tool(tool, args))
            out = Outcome(tool, result=result)
        except BaseException as exc:  # noqa: BLE001 - a client sees any failure as an error
            out = Outcome(tool, exc=exc)
        self.outcomes.append(out)
        return out

    def join(self) -> None:
        for thread in list(self.app.threads):
            thread.join(timeout=30)
            assert not thread.is_alive()

    def job(self, outcome: Outcome) -> Outcome:
        self.join()
        return self.call("get_job", job_id=outcome.data["job_id"])

    def write(self, rel: str, text: str) -> Path:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
        return path

    def set_policy(self, **fields: Any) -> None:
        fields.setdefault("rate_limit_per_minute", 600)
        save_policy(McpPolicy(enabled=True, **fields), self.paths.policy_file)

    def audit_rows(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.history._query("SELECT * FROM mcp_calls ORDER BY id")]

    def device_state(self, name: str):
        return self.mock.device(name)


def add_customer_vars(env, text: str) -> None:
    """Add 'key: value' lines (two-space indented) to the acme customer's vars block."""
    path = env.root / "inventory/customers/acme/customer.yml"
    path.write_text(
        path.read_text(encoding="utf-8").replace("vars:\n", "vars:\n" + text, 1),
        encoding="utf-8",
    )


def ios_aaa_play(name: str, customer: str, lines: list[str], *, type_: str = "push") -> str:
    return yaml.safe_dump(
        {
            "name": name,
            "type": type_,
            "customer": customer,
            "targets": {"roles": ["access"]},
            "steps": [{"config": lines}],
        },
        sort_keys=False,
    )


def _build_workspace(root: Path) -> None:
    # extra devices on three platform families
    devices = root / "inventory/customers/acme/devices.yml"
    extra = [
        {"name": "sa-cx01", "host": "203.0.113.21", "site": "site-a", "role": "access",
         "platform": "aos-cx", "vars": {"mgmt_ip": "203.0.113.21/24"}},
        {"name": "sa-sw01", "host": "203.0.113.22", "site": "site-a", "role": "access",
         "platform": "aos-s", "vars": {"mgmt_ip": "203.0.113.22/24"}},
    ]  # fmt: skip
    devices.write_text(
        devices.read_text(encoding="utf-8") + yaml.safe_dump(extra, sort_keys=False),
        encoding="utf-8",
    )
    # family snippets with secret() in the sink positions of each family
    snip = root / "snippets/customers/acme"
    (snip / "aos-cx").mkdir(parents=True)
    (snip / "aos-s").mkdir(parents=True)
    (snip / "aos-cx/aaa.j2").write_text(
        "radius-server host 192.0.2.20 key plaintext {{ secret('acme/radius') }}\n",
        encoding="utf-8",
    )
    (snip / "aos-s/aaa.j2").write_text(
        "radius-server host 192.0.2.20 key \"{{ secret('acme/radius') }}\"\n", encoding="utf-8"
    )
    # baseline whose steps own every top-level running line: extras show device lines
    play = {
        "name": "Own everything",
        "type": "baseline",
        "customer": "acme",
        "targets": {"roles": ["access"]},
        "steps": [{"snippet": "aaa", "owns": OWN_EVERYTHING}],
    }
    (root / "plays/baseline/acme-own-all.yml").write_text(
        yaml.safe_dump(play, sort_keys=False), encoding="utf-8"
    )

    # a second, non-lab customer
    beta = root / "inventory/customers/beta"
    beta.mkdir(parents=True)
    (beta / "customer.yml").write_text(
        "name: Beta Industries\ncredential: beta-netadmin\nlab: false\n"
        "vars:\n  tacacs_host: 198.51.100.40\n",
        encoding="utf-8",
    )
    (beta / "devices.yml").write_text(
        "- name: bt-as01\n  host: 203.0.113.51\n  site: hq\n  role: access\n  platform: ios-xe\n"
        "  vars:\n    mgmt_ip: 203.0.113.51/24\n",
        encoding="utf-8",
    )
    (beta / "sites").mkdir()
    (beta / "sites/hq.yml").write_text("site_code: HQ\ngateway: 203.0.113.49\n", encoding="utf-8")
    (root / "snippets/customers/beta/ios").mkdir(parents=True)
    (root / "snippets/customers/beta/ios/aaa.j2").write_text(
        "tacacs server ISE1\n key {{ secret('beta/tacacs') }}\n", encoding="utf-8"
    )
    (root / "plays/push/beta-aaa.yml").write_text(
        ios_aaa_play(
            "Beta aaa", "beta", ["tacacs server ISE1", " key {{ secret('beta/tacacs') }}"]
        ),
        encoding="utf-8",
    )
    # a lab customer that may run directly
    lab = root / "inventory/customers/labco"
    lab.mkdir(parents=True)
    (lab / "customer.yml").write_text(
        "name: Lab Company\ncredential: labco-netadmin\nlab: true\n"
        "vars:\n  tacacs_host: 198.51.100.60\n",
        encoding="utf-8",
    )
    (lab / "devices.yml").write_text(
        "- name: lab-sw01\n  host: 203.0.113.61\n  site: lab\n  role: access\n  platform: ios-xe\n"
        "  vars:\n    mgmt_ip: 203.0.113.61/24\n",
        encoding="utf-8",
    )
    (lab / "sites").mkdir()
    (lab / "sites/lab.yml").write_text("site_code: LB\ngateway: 203.0.113.49\n", encoding="utf-8")
    (root / "plays/push/labco-aaa.yml").write_text(
        ios_aaa_play(
            "Lab aaa", "labco", ["tacacs server ISE1", " key {{ secret('labco/tacacs') }}"]
        ),
        encoding="utf-8",
    )


@pytest.fixture
def env(tmp_path, app_paths):
    root = tmp_path / "ws"
    shutil.copytree(FIXTURES / "workspace", root)
    _build_workspace(root)
    save_settings(AppSettings(workspace=str(root)), app_paths.settings_file)
    save_policy(McpPolicy(enabled=True, rate_limit_per_minute=600), app_paths.policy_file)
    history = History(tmp_path / "history.db")
    secrets = KeyringSecretStore(index_path=tmp_path / "idx.json")
    secrets.set_login("acme-netadmin", LoginCredential("netadmin", LOGIN_PW, ENABLE_PW))
    secrets.set_login("beta-netadmin", LoginCredential("betaadmin", BETA_PW))
    secrets.set_login("labco-netadmin", LoginCredential("labadmin", LAB_PW))
    for name, value in ACME_SECRETS.items():
        secrets.set_secret("acme", name, value)
    secrets.set_secret("beta", "tacacs", BETA_KEY)
    secrets.set_secret("labco", "tacacs", LAB_KEY)
    mock = MockTransport()
    for name in ("sa-as01", "sa-as02", "sa-cx01", "sa-sw01", "bt-as01", "lab-sw01"):
        mock.add_device(name, running=[f"hostname {name}"])
    app = McpApp(app_paths, history=history, secrets=secrets, transport_factory=lambda: mock)
    yield Env(root, tmp_path, app_paths, history, secrets, mock, app, build_server(app))
    for thread in app.threads:
        thread.join(timeout=30)
    history.close()


@pytest.fixture(autouse=True)
def cached_workspace(monkeypatch):
    """Parsing the workspace YAML dominates a tool call; reuse it while no file changes."""
    import sys

    from tsunami.core import workspace as workspace_module

    original = workspace_module.load_workspace
    cache: dict[Any, Any] = {}

    def signature(folder: Any) -> tuple:
        files = (p for p in Path(folder).rglob("*") if p.is_file())
        return tuple(sorted((str(p), p.stat().st_mtime_ns, p.stat().st_size) for p in files))

    def cached(folder, *args, **kwargs):
        key = (str(folder), args, tuple(sorted(kwargs.items())), signature(folder))
        if key not in cache:
            cache[key] = original(folder, *args, **kwargs)
        return cache[key]

    for name, module in list(sys.modules.items()):
        if name.startswith("tsunami") and getattr(module, "load_workspace", None) is original:
            monkeypatch.setattr(module, "load_workspace", cached)


def fixture_configs() -> list[tuple[str, Path]]:
    return [(p.parent.name, p) for p in sorted(CONFIGS.glob("*/*.txt"))]


def read_tokens(path: Path) -> list[str]:
    if not path.exists():
        return []
    return [t for t in path.read_text(encoding="utf-8").splitlines() if t.strip()]


def config_tokens(txt: Path) -> tuple[list[str], list[str]]:
    stem = txt.with_suffix("")
    return read_tokens(stem.with_suffix(".secrets")), read_tokens(
        stem.with_suffix(".exact-only.secrets")
    )


# ---------------------------------------------------------------------------------------
# render
# ---------------------------------------------------------------------------------------


def test_render_masks_sink_and_lists_names_only(env):
    out = env.call("render", path="plays/baseline/acme-baseline.yml", device="sa-as01")
    assert_clean(out)
    assert out.data["secret_names"] == ["acme/tacacs"]
    assert any("key" in ln for ln in out.data["lines"])
    assert_clean(env.call("render", path="plays/baseline/acme-baseline.yml"))


@pytest.mark.parametrize("value", [LOGIN_PW, TACACS, ODD])
def test_prompt_equal_to_a_secret_has_no_oracle_and_leaks_nothing_else(env, value):
    """render/check/request_run + get_job: a right guess and a wrong guess look identical."""
    wrong = twin(value)
    for tool, extra in (
        ("render", {"device": "sa-as01"}),
        ("check", {}),
        ("request_run", {"targets": ["sa-as01"]}),
    ):
        shapes = []
        for guess in (value, wrong):
            args = {
                "path": "plays/push/add-vlan.yml",
                "prompts": {"vlan_id": "30", "vlan_name": guess},
                **extra,
            }
            out = env.call(tool, **args)
            assert_clean(out, exclude=(value,))
            results = [out]
            if tool != "render" and not out.refused:
                results.append(env.job(out))
                assert_clean(results[-1], exclude=(value,))
            shapes.append([shape(r, guess) for r in results])
        assert shapes[0] == shapes[1], f"{tool}: response differs between a right and wrong guess"
    assert_clean(env.call("list_jobs"), exclude=(value,))
    assert_clean(env.call("list_jobs", customer="acme"), exclude=(value,))


def test_prompt_values_with_secret_in_every_slot(env):
    prompts = {"vlan_id": TACACS, "vlan_name": LOGIN_PW, "extra": {"nested": [SNMP]}}
    supplied = (TACACS, LOGIN_PW, SNMP)
    out = env.call("render", path="plays/push/add-vlan.yml", device="sa-as01", prompts=prompts)
    assert_clean(out, exclude=supplied)
    out = env.call("check", path="plays/push/add-vlan.yml", prompts=prompts)
    assert_clean(out, exclude=supplied)
    if not out.refused:
        assert_clean(env.job(out), exclude=supplied)


def test_day0_play_prompts_with_secrets(env):
    prompts = {
        "model": LOGIN_PW, "hostname": TACACS, "site": SNMP, "mgmt_vlan": RADIUS,
        "mgmt_ip": ENABLE_PW, "gateway": ODD, "uplink": LOGIN_PW,
    }  # fmt: skip
    supplied = (LOGIN_PW, TACACS, SNMP, RADIUS, ENABLE_PW, ODD)
    for device in (None, "sa-as01"):
        args = {"path": "plays/initial/acme-access-day0.yml", "prompts": prompts}
        if device:
            args["device"] = device
        assert_clean(env.call("render", **args), exclude=supplied)
    # only some supplied: the others must stay hidden
    out = env.call(
        "render",
        path="plays/initial/acme-access-day0.yml",
        prompts={"hostname": TACACS, "uplink": LOGIN_PW},
    )
    assert_clean(out, exclude=(TACACS, LOGIN_PW))


@pytest.mark.xfail(
    strict=True,
    reason=(
        "ACCEPTED RESIDUAL (design.md, Redaction): a secret also stored in plain text in "
        "workspace variables and printed where only the exact layer catches it can be "
        "confirmed by a whole-value guess; needs that misconfiguration, rate-limited, audited"
    ),
)
def test_guess_next_to_a_stored_secret_in_render_has_no_oracle(env):
    """The play also prints a variable that holds the real secret (copied by a person)."""
    add_customer_vars(env, f"  copied_key: {TACACS}\n")
    env.write(
        "plays/push/copied.yml",
        ios_aaa_play(
            "Copied", "acme", ["description {{ copied_key }}", " name {{ vlan_name }}"]
        ).replace("steps:", "prompts:\n  vlan_name:\n    type: string\nsteps:", 1),
    )
    shapes = []
    for guess in (TACACS, twin(TACACS)):
        out = env.call(
            "render", path="plays/push/copied.yml", device="sa-as01", prompts={"vlan_name": guess}
        )
        assert not out.refused, out.error_text
        assert_clean(out, exclude=(TACACS,) if guess == TACACS else ())
        shapes.append(shape(out, guess))
        assert "description" in out.text()
    assert shapes[0] == shapes[1], (
        "the copied variable is shown in the clear when the guess is right"
    )


# ---------------------------------------------------------------------------------------
# template exfiltration: Claude-written drafts
# ---------------------------------------------------------------------------------------

S = "secret('acme/tacacs')"
NUL = f"{S}[0]"
TRICKS = {
    "not-a-sink": f"description {{{{ {S} }}}}",
    "hostname": f"hostname {{{{ {S} }}}}",
    "upper": f" key {{{{ {S}|upper }}}}",
    "lower": f" key {{{{ {S}|lower }}}}",
    "reverse": f"description {{{{ {S}|reverse }}}}",
    "list": f"description {{{{ {S}|list }}}}",
    "join-list": f"description {{{{ {S}|list|join('-') }}}}",
    "concat": f"description {{{{ 'pre-' ~ {S} ~ '-post' }}}}",
    "slice": f"description {{{{ {S}[0:40] }}}}",
    "index": f"description {{{{ {S}[1] }}}}{{{{ {S}[2] }}}}{{{{ {S}[3] }}}}",
    "strip-nul": f"description {{{{ {S}|replace({NUL}, '') }}}}",
    "strip-nul-sink": f" key {{{{ {S}|replace({NUL}, '') }}}}",
    "split-nul": f"description {{{{ {S}|replace({NUL}, '')|replace(':', ' ') }}}}",
    "trim": f"description {{{{ {S}|trim }}}}",
    "string": f"description {{{{ {S}|string }}}}",
    "format": f"description {{{{ '%s' | format({S}) }}}}",
    "tojson": f"description {{{{ {S}|tojson }}}}",
    "length": f"description {{{{ {S}|length }}}}",
    "ord-loop": f"description {{% for c in {S} %}}{{{{ c }}}}.{{% endfor %}}",
    "center": f"description {{{{ {S}|center(80) }}}}",
    "truncate": f"description {{{{ {S}|truncate(5) }}}}",
    "wordwrap": f"description {{{{ {S}|wordwrap(3) }}}}",
    "urlencode": f"description {{{{ {S}|urlencode }}}}",
    "b64": f"description {{{{ {S}|b64encode }}}}",
    "hash": f"description {{{{ {S}|sha256 }}}}",
    "int": f"description {{{{ {S}|int }}}}",
    "attr-mask": f"description {{{{ {S}.mask('x') }}}}",
    "dunder-class": f"description {{{{ {S}.__class__ }}}}",
    "dunder-init-globals": "description {{ secret.__init__.__globals__ }}",
    "dunder-self": "description {{ secret.__self__ }}",
    "attr-filter": "description {{ secret|attr('__dict__') }}",
    "public-attrs": "description {{ secret.refs }} {{ secret.customer }} {{ secret.contains }}",
    "materialize": "description {{ secret.materialize('x', {'acme/tacacs': 'y'}) }}",
    "private-prefix": "description {{ secret._prefix }}",
    "lipsum-globals": "description {{ lipsum.__globals__ }}",
    "cycler-globals": "description {{ cycler.__init__.__globals__ }}",
    "mro-subclasses": "description {{ ''.__class__.__mro__[1].__subclasses__() }}",
    "self": "description {{ self }}",
    "config": "description {{ config }} {{ environment }}",
    "range-os": "description {{ range.__globals__ }}",
    "import-os": "{% import 'os' as os %}",
    "include": "{% include '/etc/passwd' %}",
    "set-and-reuse": f"{{% set k = {S} %}}description {{{{ k }}}}",
    "namespace": f"{{% set ns = namespace(v={S}) %}}description {{{{ ns.v }}}}",
    "macro": f"{{% macro m(x) %}}{{{{ x }}}}{{% endmacro %}}description {{{{ m({S}) }}}}",
    "cross-customer": "description {{ secret('beta/tacacs') }}",
    "bad-ref": "description {{ secret('acme/../../beta/tacacs') }}",
    "missing-ref": "description {{ secret('acme/does-not-exist') }}",
    "login-ref": "description {{ secret('acme/netadmin') }}",
    "vars-lookup": "description {{ vars }} {{ facts }} {{ prompts }}",
}


def _tricks_play(line: str) -> str:
    return ios_aaa_play("Trick", "acme", [line])


def test_draft_plays_cannot_exfiltrate(env):
    """Every trick: validate/render/read back; mangling the sentinel is now a render error."""
    names = sorted(TRICKS)
    for name in names:
        env.write(f"drafts/plays/push/trick-{name}.yml", _tricks_play(TRICKS[name]))
    failures = []
    refused = 0
    for name in names:
        path = f"drafts/plays/push/trick-{name}.yml"
        for out in (
            env.call("validate", path=path),
            env.call("render", path=path, device="sa-as01"),
        ):
            refused += out.tool == "render" and out.refused
            found = leaked(out)
            if found:
                failures.append(f"{name}/{out.tool}: {found}")
    assert not failures, failures
    assert refused >= len(names) // 2, "most tricks should be refused outright"
    assert_clean(env.call("list_plays", include_drafts=True))


def test_mangled_sentinel_is_a_render_error(env):
    for name in ("reverse", "join-list", "tojson", "truncate", "strip-nul", "ord-loop"):
        path = f"drafts/plays/push/m-{name}.yml"
        env.write(path, _tricks_play(TRICKS[name]))
        out = env.call("render", path=path, device="sa-as01")
        assert out.refused, f"{name}: rendered a mangled secret() without error"
        assert_clean(out)


def test_draft_play_tricks_through_check_and_request_run(env):
    for name in ("upper", "not-a-sink", "concat", "slice", "strip-nul-sink", "format"):
        path = f"drafts/plays/push/job-{name}.yml"
        env.write(path, _tricks_play(TRICKS[name]))
    outs = []
    for name in ("upper", "not-a-sink", "concat", "slice", "strip-nul-sink", "format"):
        path = f"drafts/plays/push/job-{name}.yml"
        chk = env.call("check", path=path)
        run = env.call("request_run", path=path, targets=["sa-as01"])
        outs += [chk, run]
    env.join()
    for out in list(outs):
        if not out.refused:
            outs.append(env.call("get_job", job_id=out.data["job_id"]))
    for out in outs:
        assert_clean(out)


@pytest.mark.parametrize("name", ["upper", "reverse", "strip-nul-sink", "not-a-sink", "concat"])
def test_draft_snippet_cannot_exfiltrate(env, name):
    env.write("drafts/snippets/common/ios/trick.j2", TRICKS[name] + "\n")
    env.write(
        "drafts/plays/push/uses-trick.yml",
        yaml.safe_dump(
            {
                "name": "Uses trick",
                "type": "push",
                "customer": "acme",
                "targets": {"roles": ["access"]},
                "steps": [{"snippet": "trick"}],
            }
        ),
    )
    for out in (
        env.call("validate", path="drafts/snippets/common/ios/trick.j2"),
        env.call("list_snippets", customer="acme", platform="ios"),
        env.call("read_snippet", name="trick", customer="acme", platform="ios"),
        env.call("render", path="drafts/plays/push/uses-trick.yml", device="sa-as01"),
    ):
        assert_clean(out)


def test_write_draft_tool_with_tricks(env):
    for name in ("upper", "reverse", "not-a-sink"):
        path = f"drafts/plays/push/w-{name}.yml"
        out = env.call("write_draft", path=path, content=_tricks_play(TRICKS[name]))
        assert_clean(out)
        assert_clean(env.call("render", path=path, device="sa-as01"))


def test_secret_in_check_exec_and_when_expressions(env):
    play = yaml.safe_dump(
        {
            "name": "Exec secret",
            "type": "push",
            "customer": "acme",
            "targets": {"roles": ["access"]},
            "steps": [
                {"config": ["vlan 99"], "when": f"{S} == 'x'"},
                {"exec": f"show version {{{{ {S} }}}}"},
            ],
            "checks": {"post": [{"exec": f"show vlan {{{{ {S} }}}}", "expect": f"{{{{ {S} }}}}"}]},
        }
    )
    env.mock.device("sa-as01").exec_responses["re:^show"] = f"echo {TACACS} / {LOGIN_PW}"
    outs = [env.call("write_draft", path="drafts/plays/push/exec.yml", content=play)]
    outs.append(env.call("validate", path="drafts/plays/push/exec.yml"))
    outs.append(env.call("render", path="drafts/plays/push/exec.yml", device="sa-as01"))
    chk = env.call("check", path="drafts/plays/push/exec.yml", targets=["sa-as01"])
    outs.append(chk)
    if not chk.refused:
        outs.append(env.job(chk))
    for out in outs:
        assert_clean(out)
    for _kind, sent in env.mock.device("sa-as01").sent:
        assert TACACS not in sent, "check mode sent a real secret to the device"


def test_secret_inside_play_vars_and_inventory_vars(env):
    # Placing secret() or the plain value in variable files must not surface the value.
    add_customer_vars(
        env,
        f"  tacacs_key_ref: \"{{{{ secret('acme/tacacs') }}}}\"\n  copied_key: {TACACS}\n"
        f"  login_note: 'pw is {LOGIN_PW}'\n",
    )
    dev = env.root / "inventory/customers/acme/devices.yml"
    dev.write_text(
        dev.read_text(encoding="utf-8").replace(
            "mgmt_ip: 203.0.113.11/24", f"mgmt_ip: 203.0.113.11/24\n    snmp_copy: {SNMP}"
        ),
        encoding="utf-8",
    )
    outs = (
        env.call("resolved_vars", customer="acme", device="sa-as01"),
        env.call("list_devices", customer="acme"),
        env.call("list_customers"),
        env.call("render", path="plays/baseline/acme-baseline.yml", device="sa-as01"),
    )
    for out in outs:
        assert not out.refused, out.error_text
        assert_clean(out)


def test_plaintext_secret_planted_in_workspace_files(env):
    """A person pasted a real secret into a play, snippet or var file: exact layer must catch."""
    play = ios_aaa_play("Planted", "acme", ["tacacs server ISE1", f" key {TACACS}"])
    env.write("plays/push/planted.yml", play)
    env.write("snippets/customers/acme/ios/planted.j2", f"enable secret 0 {ENABLE_PW}\n")
    env.write("drafts/plays/push/planted.yml", play)
    outs = [
        env.call("read_play", path="plays/push/planted.yml"),
        env.call("read_play", path="drafts/plays/push/planted.yml"),
        env.call("read_snippet", name="planted", customer="acme", platform="ios"),
        env.call("list_plays", include_drafts=True),
        env.call("validate", path="plays/push/planted.yml"),
        env.call("render", path="plays/push/planted.yml", device="sa-as01"),
    ]
    for out in outs:
        assert_clean(out, ACME_VALUES)


def test_plaintext_secret_in_common_snippet_read_without_customer(env):
    env.write(
        "snippets/common/ios/planted.j2", f"username ops password 0 {LOGIN_PW}\nkeyx {SNMP}\n"
    )
    out = env.call("read_snippet", name="planted", platform="ios")
    assert_clean(out, [LOGIN_PW, SNMP])


def test_secret_in_other_customers_files_not_shown_to_this_customer_calls(env):
    # beta's secret planted in an acme-visible file is not known to acme's redactor; the call
    # concerns acme so only acme values are guaranteed. Document that boundary: no beta value
    # may come out of any beta-scoped call.
    env.write("snippets/customers/beta/ios/planted.j2", f"key-string {BETA_KEY}\n")
    out = env.call("read_snippet", name="planted", customer="beta", platform="ios")
    assert_clean(out, [BETA_KEY])


# ---------------------------------------------------------------------------------------
# device output: show
# ---------------------------------------------------------------------------------------


def _load_device(env, family: str, config: Path) -> str:
    name = FAMILY_DEVICE[family]
    state = env.mock.device(name)
    text = config.read_text(encoding="utf-8")
    state.running[:] = text.splitlines()
    state.exec_responses["re:^show running-config"] = text
    return name


def _store_exact_only(env, tokens: list[str]) -> None:
    for i, token in enumerate(tokens):
        env.secrets.set_secret("acme", f"exo-{i}", token)


SHOW_COMMANDS = [
    "show running-config",
    "show running-config | include username",
    "show running-config | section line",
    "show running-config | begin snmp",
    "show running-config | exclude ip",
    "show running-config | count a",
]


@pytest.mark.parametrize(
    ("family", "config"), fixture_configs(), ids=lambda v: getattr(v, "name", v)
)
def test_show_running_config_fixture_configs(env, family, config):
    pattern_tokens, exact_tokens = config_tokens(config)
    _store_exact_only(env, exact_tokens)
    name = _load_device(env, family, config)
    plain = env.call("show", customer="acme", device=name, command="show running-config")
    assert not plain.refused, plain.error_text
    assert set(plain.data) == {"device", "untrusted_device_output"}
    # redaction must not eat the whole output
    assert len(plain.data["untrusted_device_output"]) > 100
    ran = 0
    for command in SHOW_COMMANDS:
        out = env.call("show", customer="acme", device=name, command=command)
        if out.refused:
            continue  # modifier not allowed for this family
        ran += 1
        blob = "\n".join(_strings(out.data))
        missed = [t for t in pattern_tokens + exact_tokens if t in blob]
        assert not missed, f"{family}/{config.name} {command!r} leaked {missed}"
        assert_clean(out)
    assert ran >= 3


SHOW_POSITIONS = [
    ("ios", "show version", "Banner: login pw {pw}\nSystem image ok"),
    ("ios", "show interfaces", "Gi1/0/1 is up\n  Description: tmp {pw} do not share"),
    ("ios", "show cdp neighbors", "Device ID: sw-{pw}-x\nPlatform: cisco"),
    ("aos-cx", "show system", "Hostname : cx-{pw}\nProduct : 6300"),
    ("aos-cx", "show interface brief", "1/1/1 desc={pw} up"),
    ("aos-s", "show system information", "System Name : {pw}\nContact: it@example.com"),
    ("aos-s", "show interfaces brief", "1 100/1000T | {pw} Yes"),
]


def test_show_echoes_secret_in_arbitrary_positions(env):
    for family, command, template in SHOW_POSITIONS:
        name = FAMILY_DEVICE[family]
        for value in ACME_VALUES:
            env.mock.device(name).exec_responses[command] = template.format(pw=value)
            out = env.call("show", customer="acme", device=name, command=command)
            assert not out.refused
            assert_clean(out, ACME_VALUES)


SECRET_LINES = [
    "username admin secret 0 {pw}",
    "username admin privilege 15 password 7 104D000A0618",
    "enable secret 5 $1$abcd$AbCdEfGhIjKlMnOpQrStUv",
    "snmp-server community {pw} RW",
    "snmp-server user bob grp v3 auth sha {pw} priv aes 128 {pw}",
    "tacacs-server host 192.0.2.5 key {pw}",
    "radius server R1\n address ipv4 192.0.2.6\n key 7 {pw}",
    "key chain K\n key 1\n  key-string {pw}",
    "crypto isakmp key {pw} address 192.0.2.7",
    "ntp authentication-key 1 md5 {pw}",
    "interface Vlan10\n ip ospf message-digest-key 1 md5 {pw}",
    "router bgp 65000\n neighbor 192.0.2.8 password {pw}",
    "interface Vlan10\n standby 1 authentication {pw}",
    "banner motd ^C\nuse password {pw}\n^C",
    "interface Gi1/0/1\n description backup pw={pw}",
    "ip http client password {pw}",
    "snmpv3 user bob auth-pass plaintext {pw} priv-pass plaintext {pw}",
    'radius-server host 192.0.2.5 key "{pw}"',
    "crypto pki certificate chain TP\n certificate self-signed 01\n  3082035A 30820242\n  quit",
]


def test_show_running_config_secret_lines(env):
    for line in SECRET_LINES:
        for value in (LOGIN_PW, TACACS, ODD):
            text = "hostname x\n" + line.format(pw=value) + "\nend"
            env.mock.device("sa-as01").exec_responses["show running-config"] = text
            out = env.call("show", customer="acme", device="sa-as01", command="show running-config")
            assert_clean(out, ACME_VALUES)
            assert "3082035A" not in out.text()


def test_show_unstored_secret_forms_are_caught_by_patterns(env):
    # None of these values are in the keyring: only the pattern layer can catch them.
    unstored = ["Un1stored-Pw-1001", "Un2stored-Pw-1002", "Un3stored-Pw-1003", "Un4stored-Pw-1004"]
    text = "\n".join(
        [
            f"username a secret 0 {unstored[0]}",
            f"snmp-server community {unstored[1]} RO",
            f"tacacs server T\n key {unstored[2]}",
            f"enable password {unstored[3]}",
        ]
    )
    env.mock.device("sa-as01").exec_responses["show running-config"] = text
    out = env.call("show", customer="acme", device="sa-as01", command="show running-config")
    assert_clean(out)
    assert not [u for u in unstored if u in out.text()]


@pytest.mark.xfail(
    strict=True,
    reason="LIMITATION: exact-value redaction misses a value the device wrapped over two lines",
)
def test_show_secret_wrapped_across_lines(env):
    half = len(TACACS) // 2
    text = f"description wrapped {TACACS[:half]}\n{TACACS[half:]} end"
    env.mock.device("sa-as01").exec_responses["show version"] = text
    out = env.call("show", customer="acme", device="sa-as01", command="show version")
    assert_clean(out, ACME_VALUES)


def test_show_secret_with_crlf_line_endings(env):
    text = f"line1\r\nkey-material {TACACS}\r\nusername x secret 0 {LOGIN_PW}\r\n"
    env.mock.device("sa-as01").exec_responses["show running-config"] = text
    out = env.call("show", customer="acme", device="sa-as01", command="show running-config")
    assert_clean(out, ACME_VALUES)


def test_multiline_secret_value_with_crlf_device_output(env):
    key = "-----BEGIN-KEY-----\nAbCdEf1234567890\nZyXwVu0987654321\n-----END-KEY-----"
    env.secrets.set_secret("acme", "sshkey", key)
    env.mock.device("sa-as01").exec_responses["show version"] = key.replace("\n", "\r\n")
    out = env.call("show", customer="acme", device="sa-as01", command="show version")
    assert_clean(out, [key, "AbCdEf1234567890", "ZyXwVu0987654321"])


def test_show_never_sends_a_pipe_to_the_device(env):
    state = env.mock.device("sa-as01")
    state.exec_responses["re:^show running-config"] = f"username a secret 0 {LOGIN_PW}\nend"
    for modifier in ("include", "exclude", "begin", "section", "count"):
        env.call(
            "show",
            customer="acme",
            device="sa-as01",
            command=f"show running-config | {modifier} username",
        )
    sent = [cmd for kind, cmd in state.sent if kind == "exec"]
    assert sent and not [c for c in sent if "|" in c], sent


def test_show_pipe_with_a_secret_pattern_cannot_reveal_the_device_line(env):
    """A guess in the pipe pattern must not make the redacted device line come back."""
    line = f"description backup-{TACACS}-x"
    env.mock.device("sa-as01").exec_responses["re:^show running-config"] = (
        f"hostname a\n{line}\nend"
    )
    shapes = []
    for guess in (TACACS, twin(TACACS)):
        row = []
        for command in (
            f"show running-config | include {guess}",
            f"show running-config | begin {guess}",
            f"show running-config | section {guess}",
            f"show running-config | exclude {guess}",
            f"show running-config | count {guess}",
            f"show running-config {guess}",
            f"show running-config | include backup-{guess}",
        ):
            out = env.call("show", customer="acme", device="sa-as01", command=command)
            assert_clean(out, exclude=(TACACS,) if guess == TACACS else ())
            row.append(shape(out, guess))
        shapes.append(row)
    assert shapes[0] == shapes[1], "a right guess in a pipe pattern changes the response"


def test_show_supplied_prefix_of_a_longer_secret_still_hides_the_rest(env):
    long_value = "Pre-Fix-1111-Extended-2222"
    short = "Pre-Fix-1111"
    env.secrets.set_secret("acme", "long", long_value)
    env.mock.device("sa-as01").exec_responses["show version"] = f"x {long_value} y"
    out = env.call("show", customer="acme", device="sa-as01", command=f"show version {short}")
    assert "Extended-2222" not in out.text()
    assert_clean(out, [long_value])


def test_show_cross_customer_device(env):
    env.mock.device("bt-as01").exec_responses["show version"] = f"b {BETA_PW} {BETA_KEY}"
    out = env.call("show", customer="acme", device="bt-as01", command="show version")
    assert out.refused
    assert_clean(out)
    assert_clean(env.call("show", customer="beta", device="bt-as01", command="show version"))


def test_show_secrets_of_other_customers_are_redacted_too(env):
    env.mock.device("sa-as01").exec_responses["show version"] = (
        f"neighbor note {BETA_KEY} {BETA_PW} {LAB_KEY}"
    )
    assert_clean(env.call("show", customer="acme", device="sa-as01", command="show version"))


def test_show_error_text_from_device_and_transport(env):
    env.mock.device("sa-as01").fail_connect_times = 3
    assert_clean(env.call("show", customer="acme", device="sa-as01", command="show version"))
    env.mock.device("sa-as02").exec_responses["show version"] = (
        f"% Invalid input detected at '^' marker. password {LOGIN_PW}"
    )
    assert_clean(env.call("show", customer="acme", device="sa-as02", command="show version"))


def test_redaction_fails_closed_when_the_secret_store_cannot_be_read(
    env, memory_keyring, monkeypatch
):
    env.mock.device("sa-as01").exec_responses["show version"] = f"pw {LOGIN_PW} key {TACACS}"

    def boom(service, username):
        raise RuntimeError("credential store locked")

    monkeypatch.setattr(memory_keyring, "get_password", boom)
    for tool, args in (
        ("show", {"customer": "acme", "device": "sa-as01", "command": "show version"}),
        ("render", {"path": "plays/baseline/acme-baseline.yml", "device": "sa-as01"}),
        ("read_snippet", {"name": "aaa", "customer": "acme", "platform": "ios"}),
    ):
        out = env.call(tool, **args)
        assert_clean(out)


# ---------------------------------------------------------------------------------------
# jobs: check, compliance, request_run, get_job, list_jobs
# ---------------------------------------------------------------------------------------


def _all_fixture_tokens() -> tuple[list[str], list[str]]:
    pat, exact = [], []
    for _family, cfg in fixture_configs():
        p, e = config_tokens(cfg)
        pat += p
        exact += e
    return list(dict.fromkeys(pat)), list(dict.fromkeys(exact))


def test_compliance_extra_lines_never_carry_device_secrets(env):
    pattern_tokens, exact_tokens = _all_fixture_tokens()
    _store_exact_only(env, exact_tokens)
    by_family = {fam: [c for f, c in fixture_configs() if f == fam] for fam in FAMILY_DEVICE}
    for family, device in FAMILY_DEVICE.items():
        text = by_family[family][0].read_text(encoding="utf-8")
        env.mock.device(device).running[:] = text.splitlines()
    env.mock.device("sa-as02").running[:] = (
        by_family["ios"][-1].read_text(encoding="utf-8").splitlines()
    )
    job = env.call("compliance", path="plays/baseline/acme-own-all.yml")
    assert_clean(job)
    result = env.job(job)
    assert_clean(result)
    blob = "\n".join(_strings(result.data))
    # Sanity: the owned-everything baseline really did surface lots of extra lines.
    assert blob.count("\n- ") > 20, blob[:500]
    missed = [t for t in pattern_tokens + exact_tokens if t in blob]
    assert not missed, f"compliance extras leaked {missed}"


def test_check_diff_with_secret_bearing_running_config(env):
    pattern_tokens, exact_tokens = _all_fixture_tokens()
    _store_exact_only(env, exact_tokens)
    text = (CONFIGS / "ios/c2960-sa-as01.txt").read_text(encoding="utf-8")
    for dev in ("sa-as01", "sa-as02"):
        env.mock.device(dev).running[:] = text.splitlines()
    job = env.call("check", path="plays/baseline/acme-own-all.yml")
    result = env.job(job)
    assert_clean(result)
    blob = "\n".join(_strings(result.data))
    assert not [t for t in pattern_tokens + exact_tokens if t in blob]


def test_check_post_exec_output_and_errors_with_secrets(env):
    for dev in ("sa-as01", "sa-as02"):
        env.mock.device(dev).exec_responses["re:^show vlan id"] = (
            f"% Error pw={LOGIN_PW} key {TACACS}\nusername z secret 0 {SNMP}"
        )
        env.mock.device(dev).running[:] = [f"username q secret 0 {RADIUS}"]
    prompts = {"vlan_id": "30", "vlan_name": "Cams"}
    chk = env.call("check", path="plays/push/add-vlan.yml", prompts=prompts)
    assert_clean(chk)
    assert_clean(env.job(chk))
    assert_clean(env.call("list_jobs"))
    assert_clean(env.call("list_jobs", customer="acme", limit=500))


def test_job_failed_by_connect_error(env):
    for dev in ("sa-as01", "sa-as02"):
        env.mock.device(dev).fail_connect_times = 99
    chk = env.call("check", path="plays/baseline/acme-baseline.yml")
    assert_clean(chk)
    assert_clean(env.job(chk))


def test_request_run_non_lab_diff_and_cancel(env):
    prompts = {"vlan_id": "30", "vlan_name": "Cams"}
    req = env.call("request_run", path="plays/baseline/acme-own-all.yml", targets=["sa-as01"])
    assert_clean(req)
    assert_clean(env.job(req))
    if not req.refused:
        assert_clean(env.call("cancel_job", job_id=req.data["job_id"]))
    req = env.call(
        "request_run", path="plays/push/add-vlan.yml", targets=["sa-as01"], prompts=prompts
    )
    assert_clean(env.job(req))


def test_jobs_never_show_a_secret_the_caller_did_not_supply(env):
    """get_job/list_jobs after jobs whose device output and prompts hold secrets."""
    for dev in ("sa-as01", "sa-as02"):
        state = env.mock.device(dev)
        state.running[:] = [f"username u secret 0 {LOGIN_PW}", f"snmp-server community {SNMP} RO"]
        state.exec_responses["re:^show vlan id"] = f"x {TACACS} {RADIUS} {ENABLE_PW}"
    ids = []
    for prompts in (
        {"vlan_id": "30", "vlan_name": "Cams"},
        {"vlan_id": "31", "vlan_name": TACACS},
        {"vlan_id": "32", "vlan_name": LOGIN_PW},
    ):
        supplied = tuple(v for v in prompts.values() if v in ALL_VALUES)
        for tool, extra in (("check", {}), ("request_run", {"targets": ["sa-as01", "sa-as02"]})):
            out = env.call("check" if tool == "check" else tool, path="plays/push/add-vlan.yml",
                           prompts=prompts, **extra)  # fmt: skip
            assert_clean(out, exclude=supplied)
            if not out.refused:
                ids.append((out.data["job_id"], supplied))
    env.join()
    for job_id, supplied in ids:
        assert_clean(env.call("get_job", job_id=job_id), exclude=supplied)
    # a listing carries no per-job prompts, so nothing is exempt
    assert_clean(env.call("list_jobs", limit=200))
    assert_clean(env.call("list_jobs", customer="acme"))


def _lab_run(env, *, reject: str | None = None, play: str = "plays/push/labco-aaa.yml", **args):
    state = env.mock.device("lab-sw01")
    state.running[:] = ["hostname lab-sw01"]
    state.reject_lines = [reject] if reject else []
    return env.call("request_run", path=play, targets=["lab-sw01"], **args)


def test_lab_apply_failures_do_not_leak_materialized_secret(env):
    """Apply mode materializes the real value; failed_line/error/output must be redacted."""
    ran = 0
    for reject in (None, "key", "ISE1"):
        out = _lab_run(env, reject=reject)
        assert_clean(out)
        if out.refused:
            continue
        ran += 1
        assert_clean(env.job(out))
        assert_clean(env.call("list_jobs", customer="labco"))
        assert_clean(
            env.call("show", customer="labco", device="lab-sw01", command="show running-config")
        )
    assert ran, "lab direct run was never available"


def test_lab_run_then_show_running_config_has_masked_secret(env):
    out = _lab_run(env)
    assert not out.refused, out.error_text
    env.job(out)
    state = env.mock.device("lab-sw01")
    state.exec_responses["show running-config"] = "\n".join(state.running)
    assert any(LAB_KEY in line for line in state.running), "lab run did not apply the secret"
    for cmd in (
        "show running-config",
        "show running-config | include key",
        "show running-config | section tacacs",
        "show running-config | begin tacacs",
        "show running-config | exclude hostname",
        "show running-config | count key",
    ):
        assert_clean(env.call("show", customer="labco", device="lab-sw01", command=cmd))


def test_lab_exfil_through_sink_variants_then_read_back(env):
    """Place the real secret in several sink shapes, apply on the lab, read it back."""
    lines = [
        ["username probe secret 0 {S}"],
        ["snmp-server community {S} RO"],
        ["tacacs server T2", " key {S}"],
        ["radius-server host 192.0.2.9 key {S}"],
        ["ntp authentication-key 5 md5 {S}"],
        ["key chain KC", " key 1", "  key-string {S}"],
    ]
    expr = "{{ secret('labco/tacacs') }}"
    for i, group in enumerate(lines):
        env.write(
            f"drafts/plays/push/lab-sink-{i}.yml",
            ios_aaa_play(f"Sink {i}", "labco", [ln.replace("{S}", expr) for ln in group]),
        )
    for i in range(len(lines)):
        path = f"drafts/plays/push/lab-sink-{i}.yml"
        state = env.mock.device("lab-sw01")
        state.running[:] = ["hostname lab-sw01"]
        outs = [env.call("render", path=path, device="lab-sw01")]
        run = env.call("request_run", path=path, targets=["lab-sw01"])
        outs.append(run)
        if not run.refused:
            outs.append(env.job(run))
        state.exec_responses["show running-config"] = "\n".join(state.running)
        outs.append(
            env.call("show", customer="labco", device="lab-sw01", command="show running-config")
        )
        for out in outs:
            assert_clean(out, [LAB_KEY, LAB_PW])


LAB_TAG_PLAY = yaml.safe_dump(
    {
        "name": "Lab tag",
        "type": "push",
        "customer": "labco",
        "targets": {"roles": ["access"]},
        "prompts": {"tag": {"type": "string"}},
        "steps": [
            {
                "config": [
                    "tacacs server ISE1",
                    " key {{ secret('labco/tacacs') }}",
                    "interface Vlan1",
                    " description {{ tag }}",
                    "interface Vlan2",
                    " description {{ copied }}",
                ]
            },
            {"exec": "show vlan id 5"},
        ],
        "checks": {"post": [{"exec": "show vlan id 5", "expect": "active"}]},
    },
    sort_keys=False,
)


def _lab_tag_setup(env) -> None:
    lab = env.root / "inventory/customers/labco/customer.yml"
    lab.write_text(
        lab.read_text(encoding="utf-8").replace("vars:\n", f"vars:\n  copied: {LAB_KEY}\n", 1),
        encoding="utf-8",
    )
    env.mock.device("lab-sw01").exec_responses["re:^show vlan id"] = f"nothing {LAB_KEY}"
    env.write("plays/push/labco-tag.yml", LAB_TAG_PLAY)


def test_lab_post_check_failure_output_holds_no_secret(env):
    _lab_tag_setup(env)
    run = _lab_run(env, play="plays/push/labco-tag.yml", prompts={"tag": "plain"})
    assert not run.refused, run.error_text
    job = env.job(run)
    assert_clean(run, [LAB_KEY, LAB_PW])
    assert_clean(job, [LAB_KEY, LAB_PW])
    assert "exec" in json.dumps(job.data) or job.data["devices"], job.data


@pytest.mark.xfail(
    strict=True,
    reason=(
        "ACCEPTED RESIDUAL (design.md, Redaction): a secret also stored in plain text in "
        "workspace variables and printed where only the exact layer catches it can be "
        "confirmed by a whole-value guess; needs that misconfiguration, rate-limited, audited"
    ),
)
def test_lab_job_with_a_secret_prompt_has_no_oracle(env):
    """A right guess as prompt must not unmask the device's or the inventory's own copy."""
    _lab_tag_setup(env)
    shapes = []
    for guess in (LAB_KEY, twin(LAB_KEY)):
        run = _lab_run(env, play="plays/push/labco-tag.yml", prompts={"tag": guess})
        assert not run.refused, run.error_text
        job = env.job(run)
        assert_clean(job, exclude=(LAB_KEY,) if guess == LAB_KEY else ())
        shapes.append(shape(job, guess))
    assert shapes[0] == shapes[1], "get_job differs between a right and a wrong prompt guess"


# ---------------------------------------------------------------------------------------
# error paths with secrets in tool arguments
# ---------------------------------------------------------------------------------------

ARG_CASES = [
    ("list_devices", {"customer": "{S}"}),
    ("list_devices", {"customer": "acme", "site": "{S}", "role": "{S}"}),
    ("list_plays", {"type": "{S}", "customer": "{S}"}),
    ("read_play", {"path": "plays/{S}.yml"}),
    ("read_play", {"path": "{S}"}),
    ("list_snippets", {"customer": "{S}", "platform": "{S}"}),
    ("read_snippet", {"name": "{S}", "customer": "acme", "platform": "ios"}),
    ("read_snippet", {"name": "aaa", "customer": "{S}", "platform": "ios"}),
    ("read_snippet", {"name": "aaa", "customer": "acme", "platform": "{S}"}),
    ("resolved_vars", {"customer": "{S}", "device": "{S}"}),
    ("resolved_vars", {"customer": "acme", "device": "{S}"}),
    ("validate", {"content": "name: {S}\ntype: {S}\ncustomer: {S}\nsteps: {S}"}),
    ("validate", {"content": "{S}: [unclosed"}),
    ("validate", {"content": "name: x\ntype: push\ncustomer: acme\nsteps:\n  - config: ['{S}']"}),
    (
        "validate",
        {"content": "name: x\ntype: push\ncustomer: acme\nsteps:\n  - config: ['{{ {S} }}']"},
    ),
    ("validate", {"path": "{S}"}),
    ("render", {"path": "{S}"}),
    ("render", {"path": "plays/push/add-vlan.yml", "device": "{S}"}),
    ("render", {"path": "plays/push/add-vlan.yml", "device": "sa-as01", "prompts": {"{S}": "{S}"}}),
    ("write_draft", {"path": "drafts/plays/push/{S}.yml", "content": "name: {S}\n"}),
    ("write_draft", {"path": "{S}", "content": "{S}"}),
    ("write_draft", {"path": "drafts/snippets/common/ios/err.j2", "content": "{{ {S} }}"}),
    ("write_draft", {"path": "drafts/snippets/common/ios/err2.j2", "content": "{% if {S} %}"}),
    ("delete_draft", {"path": "drafts/{S}"}),
    ("delete_draft", {"path": "{S}"}),
    ("check", {"path": "{S}"}),
    ("check", {"path": "plays/baseline/acme-baseline.yml", "targets": ["{S}"]}),
    ("compliance", {"path": "{S}"}),
    ("show", {"customer": "acme", "device": "sa-as01", "command": "{S}"}),
    ("get_job", {"job_id": "{S}"}),
    ("list_jobs", {"customer": "{S}"}),
    ("request_run", {"path": "{S}", "targets": ["{S}"]}),
    ("request_run", {"path": "plays/push/add-vlan.yml", "targets": ["sa-as01"], "tags": ["{S}"]}),
    ("cancel_job", {"job_id": "{S}"}),
]


def _sub(value: Any, secret: str) -> Any:
    if isinstance(value, str):
        return value.replace("{S}", secret)
    if isinstance(value, dict):
        return {_sub(k, secret): _sub(v, secret) for k, v in value.items()}
    if isinstance(value, list):
        return [_sub(v, secret) for v in value]
    return value


ARG_CASES += [
    ("show", {"customer": "{S}", "device": "sa-as01", "command": "show version"}),
    ("show", {"customer": "acme", "device": "{S}", "command": "show version"}),
    ("show", {"customer": "acme", "device": "sa-as01", "command": "show version {S}"}),
    ("show", {"customer": "acme", "device": "sa-as01", "command": "show version | include {S}"}),
    ("show", {"customer": "acme", "device": "sa-as01", "command": "show version | begin a{S}"}),
    ("request_run", {"path": "plays/push/add-vlan.yml", "targets": ["sa-as01"], "tags": ["{S}"]}),
    ("check", {"path": "plays/push/add-vlan.yml", "prompts": {"vlan_name": "{S}"}}),
    ("render", {"path": "plays/push/add-vlan.yml", "device": "sa-as01", "prompts": {"a": "{S}"}}),
]
CASE_IDS = [f"{tool}-{i}" for i, (tool, _) in enumerate(ARG_CASES)]
ARG_SECRETS = (LOGIN_PW, ODD)


# Cases that put the secret in a dictionary key: see test_audit_redacts_dictionary_keys.
AUDIT_KEY_LEAKS = {"render-18"}


def _args_run(env):
    """Each case with a real secret and a same-shape non-secret; returns findings by property."""
    findings: dict[str, list[str]] = {"leak": [], "oracle": [], "audit": []}
    seen = 0
    for case_id, (tool, args) in zip(CASE_IDS, ARG_CASES, strict=True):
        for secret in ARG_SECRETS:
            wrong = twin(secret)
            right = env.call(tool, **_sub(args, secret))
            other = env.call(tool, **_sub(args, wrong))
            # (b) nothing the caller did not supply, and nothing at all for the wrong guess
            if found := leaked(right, exclude=(secret,)):
                findings["leak"].append(f"{case_id}/{secret!r}: {found}")
            if found := leaked(other):
                findings["leak"].append(f"{case_id}/{wrong!r}: {found}")
            # (a) a right guess looks like a wrong guess
            if shape(right, secret) != shape(other, wrong):
                findings["oracle"].append(f"{case_id}/{secret!r}")
        # (c) the audit rows written by this case
        rows = env.audit_rows()
        blob = json.dumps(rows[seen:], ensure_ascii=False)
        seen = len(rows)
        if case_id not in AUDIT_KEY_LEAKS:
            findings["audit"] += [f"{case_id}: {v!r}" for v in ALL_VALUES if v in blob]
    env.join()
    return findings


# Argument cases where a right and a wrong guess still get different responses (filled from
# observation; see test_arguments_have_no_oracle).
ORACLE_CASES: set[str] = set()


def test_arguments_leak_nothing_have_no_oracle_and_are_audited_clean(env):
    findings = _args_run(env)
    problems = [f"{kind}: {item}" for kind, items in findings.items() for item in items]
    assert not problems, chr(10).join(problems)


def test_audit_redacts_dictionary_keys(env):
    out = env.call(
        "render",
        path="plays/push/add-vlan.yml",
        device="sa-as01",
        prompts={LOGIN_PW: "x", TACACS: {RADIUS: "y"}},
    )
    assert_clean(out, exclude=(LOGIN_PW, TACACS, RADIUS))
    blob = json.dumps(env.audit_rows(), ensure_ascii=False)
    assert not [v for v in (LOGIN_PW, TACACS, RADIUS) if v in blob], blob


AUDIT_CALLS = {
    "validate-content": (
        "validate",
        {"content": f"name: {LOGIN_PW}\ntype: push\ncustomer: acme\nsteps: []"},
    ),
    "write_draft-content": (
        "write_draft",
        {"path": "drafts/snippets/common/ios/aud.j2", "content": f"key {TACACS}\n"},
    ),
    "show-command": (
        "show",
        {"customer": "acme", "device": "sa-as01", "command": f"show version | include {RADIUS}"},
    ),
    "render-prompt": (
        "render",
        {
            "path": "plays/push/add-vlan.yml",
            "device": "sa-as01",
            "prompts": {"vlan_id": "1", "vlan_name": SNMP},
        },
    ),
    "check-prompt": (
        "check",
        {"path": "plays/push/add-vlan.yml", "prompts": {"vlan_name": ENABLE_PW}},
    ),
    "request_run-prompt": (
        "request_run",
        {
            "path": "plays/push/add-vlan.yml",
            "targets": ["sa-as01"],
            "prompts": {"vlan_id": "1", "vlan_name": TACACS},
        },
    ),
    "request_run-tags": (
        "request_run",
        {"path": "plays/push/add-vlan.yml", "targets": ["sa-as01"], "tags": [LOGIN_PW]},
    ),
    "read_play-path": ("read_play", {"path": f"plays/{RADIUS}.yml"}),
    "get_job-id": ("get_job", {"job_id": SNMP}),
    "list_devices-customer": ("list_devices", {"customer": ENABLE_PW}),
}


def test_audit_table_never_holds_secrets(env):
    for tool, args in AUDIT_CALLS.values():
        env.call(tool, **args)
    env.join()
    rows = env.audit_rows()
    assert len(rows) == len(AUDIT_CALLS)
    blob = json.dumps(rows, ensure_ascii=False)
    assert not [v for v in ALL_VALUES if v in blob], blob


def test_history_database_job_rows_never_come_back_with_unsupplied_secrets(env):
    env.mock.device("sa-as01").running[:] = [f"username u secret 0 {LOGIN_PW}"]
    env.mock.device("sa-as01").exec_responses["re:^show"] = f"{TACACS} {SNMP}"
    chk = env.call(
        "check", path="plays/push/add-vlan.yml", prompts={"vlan_id": "3", "vlan_name": "Cams"}
    )
    assert_clean(env.job(chk))
    assert_clean(env.call("list_jobs"))


def test_a_supplied_secret_is_not_an_exemption_for_other_secrets(env):
    """Supplying one secret must not unmask any other, in any tool that echoes arguments."""
    env.mock.device("sa-as01").exec_responses["show version"] = f"{TACACS} {RADIUS} {SNMP}"
    out = env.call(
        "show", customer="acme", device="sa-as01", command=f"show version {LOGIN_PW} {ENABLE_PW}"
    )
    assert_clean(out, exclude=(LOGIN_PW, ENABLE_PW))
    out = env.call(
        "render",
        path="plays/push/add-vlan.yml",
        device="sa-as01",
        prompts={"vlan_id": "1", "vlan_name": f"{TACACS},{RADIUS}"},
    )
    assert_clean(out, exclude=(TACACS, RADIUS))


def test_draft_content_is_not_a_cross_call_oracle(env):
    shapes = []
    for guess in (TACACS, twin(TACACS)):
        path = "drafts/plays/push/oracle.yml"
        env.call(
            "write_draft", path=path, content=ios_aaa_play("o", "acme", [f"description {guess}"])
        )
        out = env.call("read_play", path=path)
        shapes.append(shape(out, guess))
    assert shapes[0] == shapes[1]


# ---------------------------------------------------------------------------------------
# blocked customers
# ---------------------------------------------------------------------------------------

ACME_MARKERS = ["sa-as01", "sa-as02", "Acme", "203.0.113.1", "198.51.100.30", "acme-netadmin"]


def _hides_acme(out: Outcome) -> list[str]:
    text = out.text()
    return [m for m in ACME_MARKERS if m in text]


def test_blocked_customer_looks_unknown_everywhere(env):
    # create acme jobs while acme is visible, then block it
    chk = env.call("check", path="plays/baseline/acme-baseline.yml")
    env.join()
    acme_job = chk.data["job_id"]
    req = env.call("request_run", path="plays/push/add-vlan.yml", targets=["sa-as01"],
                   prompts={"vlan_id": "30", "vlan_name": "x"})  # fmt: skip
    env.join()
    env.set_policy(customers_blocked=["acme"])

    results = {
        "list_customers": env.call("list_customers"),
        "list_devices": env.call("list_devices", customer="acme"),
        "list_devices-site": env.call("list_devices", customer="acme", site="site-a"),
        "list_plays": env.call("list_plays", include_drafts=True),
        "list_plays-customer": env.call("list_plays", customer="acme"),
        "list_snippets": env.call("list_snippets", customer="acme", platform="ios"),
        "read_snippet": env.call("read_snippet", name="aaa", customer="acme", platform="ios"),
        "resolved_vars": env.call("resolved_vars", customer="acme", device="sa-as01"),
        "show": env.call("show", customer="acme", device="sa-as01", command="show version"),
        "list_jobs": env.call("list_jobs"),
        "list_jobs-customer": env.call("list_jobs", customer="acme"),
        "get_job": env.call("get_job", job_id=acme_job),
        "cancel_job": env.call("cancel_job", job_id=req.data["job_id"] if not req.refused else "x"),
        "check": env.call("check", path="plays/baseline/acme-baseline.yml"),
        "compliance": env.call("compliance", path="plays/baseline/acme-baseline.yml"),
        "request_run": env.call("request_run", path="plays/push/add-vlan.yml", targets=["sa-as01"]),
        "render": env.call("render", path="plays/baseline/acme-baseline.yml", device="sa-as01"),
        "validate-path": env.call("validate", path="plays/baseline/acme-baseline.yml"),
        "read_play": env.call("read_play", path="plays/baseline/acme-baseline.yml"),
        "write_draft": env.call(
            "write_draft", path="drafts/plays/push/b.yml", content=ios_aaa_play("b", "acme", ["x"])
        ),
    }
    env.join()
    for out in results.values():
        assert_clean(out)
    # Customer-keyed tools: refused like an unknown customer, naming nothing else.
    for name in ("list_devices", "list_devices-site", "list_snippets", "read_snippet",
                 "resolved_vars", "show", "check", "request_run"):  # fmt: skip
        assert results[name].refused, name
    # Lists omit the customer entirely.
    for name in ("list_customers", "list_plays", "list_plays-customer", "list_jobs",
                 "list_jobs-customer"):  # fmt: skip
        assert not _hides_acme(results[name]), (name, _hides_acme(results[name]))
    # Job ids of blocked customers look nonexistent.
    ghost = env.call("get_job", job_id="0" * 32)
    assert results["get_job"].refused and ghost.refused
    assert not _hides_acme(results["get_job"])
    for name in ("read_play", "render", "validate-path", "compliance"):
        assert results[name].refused or not _hides_acme(results[name]), (
            f"{name} on a blocked customer's play still shows acme data:\n"
            + results[name].text()[:600]
        )
    assert not (env.root / "drafts/plays/push/b.yml").exists() or results["write_draft"].refused


def test_blocked_customer_error_matches_unknown_customer(env):
    env.set_policy(customers_blocked=["acme"])
    blocked = env.call("list_devices", customer="acme")
    unknown = env.call("list_devices", customer="zzz")
    assert blocked.refused and unknown.refused
    assert blocked.error_text.replace("acme", "zzz") == unknown.error_text


def test_policy_disabled_and_rate_limit_messages(env):
    save_policy(McpPolicy(enabled=False), env.paths.policy_file)
    out = env.call("show", customer="acme", device="sa-as01", command=f"show {LOGIN_PW}")
    assert out.refused
    assert_clean(out)
    save_policy(McpPolicy(enabled=True, rate_limit_per_minute=1), env.paths.policy_file)
    env.call("list_customers")
    assert_clean(env.call("show", customer="acme", device="sa-as01", command=f"show {LOGIN_PW}"))


# ---------------------------------------------------------------------------------------
# paths
# ---------------------------------------------------------------------------------------

CONTENT_MARKERS = [
    "Acme Corporation",
    "acme-netadmin",
    "rate_limit_per_minute",
    "customers_blocked",
    "example.local",
    "root:",
    "[fonts]",
    "for 16-bit app support",
    "schema_version",
    "show_allowlist",
]


def _bad_paths(env) -> list[str]:
    policy = str(env.paths.policy_file)
    return [
        "../inventory/global.yml",
        "plays/../inventory/global.yml",
        "plays/push/../../workspace.yml",
        "plays\\..\\inventory\\global.yml",
        "inventory/global.yml",
        "inventory/customers/acme/customer.yml",
        "workspace.yml",
        "snippets/customers/acme/ios/aaa.j2",
        "C:/Windows/win.ini",
        "C:\\Windows\\win.ini",
        "\\\\localhost\\c$\\Windows\\win.ini",
        "//127.0.0.1/c$/Windows/win.ini",
        "\\\\?\\C:\\Windows\\win.ini",
        "/etc/passwd",
        "~/.ssh/id_rsa",
        "%APPDATA%\\Tsunami\\mcp-policy.yml",
        policy,
        policy.replace("\\", "/"),
        str(env.root / "inventory/global.yml"),
        "plays/push/add-vlan.yml:stream",
        "plays/push/add-vlan.yml::$DATA",
        "plays/push/add-vlan.yml\x00.txt",
        "plays/push/add-vlan.yml\n../../workspace.yml",
        "plays/push/CON",
        "plays/push/NUL.yml",
        "drafts/../inventory/global.yml",
        "drafts/../workspace.yml",
        "drafts/plays/../../inventory/global.yml",
        "drafts/./../workspace.yml",
        "drafts\\..\\workspace.yml",
        "plays/push/add-vlan.yml/..",
        "plays/push/add-vlan.yml/../../../workspace.yml",
        "..%2finventory%2fglobal.yml",
        "plays/..%5cinventory%5cglobal.yml",
        "plays/push/add-vlan.yml ",
        "plays/push/add-vlan.yml.",
        "",
        ".",
        "plays",
    ]


@pytest.mark.parametrize("tool", ["read_play", "validate", "delete_draft", "render"])
def test_bad_paths_are_refused_without_contents(env, tool):
    for path in _bad_paths(env):
        if tool == "delete_draft" and path in (
            "plays/push/add-vlan.yml ",
            "plays/push/add-vlan.yml.",
        ):
            continue
        args: dict[str, Any] = {"path": path}
        out = env.call(tool, **args)
        env.join()
        assert_clean(out)
        text = out.text()
        found = [m for m in CONTENT_MARKERS if m in text]
        # Legitimate plays/ files are fine for read_play on exact match only.
        allowed = path.strip() in ("plays/push/add-vlan.yml",) and tool != "delete_draft"
        if not allowed:
            assert not found, f"{tool}({path!r}) exposed {found}:\n{text[:400]}"
    # no file was removed
    assert (env.root / "plays/push/add-vlan.yml").exists()
    assert (env.root / "inventory/global.yml").exists()
    assert (env.root / "workspace.yml").exists()


def test_read_snippet_name_path_tricks(env):
    for name in [
        "../../../../inventory/global",
        "..\\..\\inventory\\global",
        "../../../customers/acme/ios/aaa",
        "C:/Windows/win",
        "aaa.j2",
        "aaa\x00",
        "aaa/../../../../../workspace",
        "\\\\localhost\\c$\\x",
    ]:
        for customer, platform in (
            ("acme", "ios"),
            (None, "ios"),
            ("../..", "ios"),
            ("acme", "../.."),
        ):
            out = env.call("read_snippet", name=name, customer=customer, platform=platform)
            assert_clean(out)
            found = [m for m in CONTENT_MARKERS if m in out.text()]
            assert not found, f"read_snippet({name!r},{customer!r},{platform!r}) exposed {found}"


def test_write_draft_path_tricks_cannot_write_outside_drafts(env):
    before = {p: p.read_bytes() for p in env.root.rglob("*") if p.is_file()}
    policy_before = env.paths.policy_file.read_bytes()
    evil = "name: evil\n"
    paths = [p for p in _bad_paths(env) if p] + [
        "drafts/../plays/push/add-vlan.yml",
        "drafts/plays/push/../../../plays/push/add-vlan.yml",
        "drafts/plays/push/x.yml:ads",
        "drafts/plays/push/x.yml::$DATA",
        "drafts/inventory/customers/acme/customer.yml",
        "drafts/workspace.yml",
        "drafts/plays/push/con.yml",
        "plays/push/new.yml",
        "snippets/common/ios/new.j2",
        "drafts/policy/mcp-policy.yml",
        str(env.paths.policy_file.parent / "evil.yml"),
        "drafts",
        "drafts/",
    ]
    for path in paths:
        out = env.call("write_draft", path=path, content=evil)
        assert_clean(out)
        text = out.text()
        assert not [m for m in CONTENT_MARKERS if m in text], (path, text[:300])
    after = {p: p.read_bytes() for p in env.root.rglob("*") if p.is_file()}
    changed = [p for p in before if before[p] != after.get(p)]
    assert not changed, changed
    new_outside = [p for p in after if p not in before and (env.root / "drafts") not in p.parents]
    assert not new_outside, new_outside
    assert env.paths.policy_file.read_bytes() == policy_before
    assert not list(env.tmp.rglob("evil.yml"))


def test_drafts_symlink_or_junction_escape(env, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.yml").write_text(f"name: stolen {LOGIN_PW}\n", encoding="utf-8")
    link = env.root / "drafts" / "plays"
    link.parent.mkdir(exist_ok=True)
    import subprocess

    made = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(outside)], capture_output=True, text=True
    )
    if made.returncode != 0:
        pytest.skip("cannot create a junction here")
    out = env.call("read_play", path="drafts/plays/secret.yml")
    assert_clean(out)
    assert "stolen" not in out.text()
    out = env.call("write_draft", path="drafts/plays/push/escape.yml", content="name: e\n")
    assert not (outside / "push/escape.yml").exists()
    assert "stolen" not in env.call("list_plays", include_drafts=True).text()
    assert not (outside / "push").exists()
    # the junction target must survive intact
    assert (outside / "secret.yml").exists()
    # remove the junction without touching the target
    subprocess.run(["cmd", "/c", "rmdir", str(link)], capture_output=True)


def test_legit_paths_still_work(env):
    # guard against over-redaction making the tools useless
    play = env.call("read_play", path="plays/push/add-vlan.yml")
    assert "vlan_id" in play.data["content"] if "content" in play.data else "vlan" in play.text()
    snippet = env.call("read_snippet", name="aaa", customer="acme", platform="ios")
    assert "tacacs server ISE1" in snippet.text()
    rendered = env.call("render", path="plays/baseline/acme-baseline.yml", device="sa-as01")
    assert "198.51.100.30" in rendered.text()
    assert "ntp server 198.51.100.1" in rendered.text()
    vars_ = env.call("resolved_vars", customer="acme", device="sa-as01")
    assert vars_.data["values"]["tacacs_host"] == "198.51.100.30"
    assert env.call("list_customers").data["customers"][0]["slug"] == "acme"


def test_render_output_keeps_secret_position_visible(env):
    rendered = env.call("render", path="plays/baseline/acme-baseline.yml", device="sa-as01")
    key_lines = [ln for ln in rendered.data["lines"] if ln.strip().startswith("key")]
    assert key_lines, rendered.data["lines"]
    assert all(("redacted" in ln or "********" in ln) for ln in key_lines)


def test_read_snippet_of_secret_sink_stays_readable(env):
    snippet = env.call("read_snippet", name="aaa", customer="acme", platform="ios")
    assert "secret('acme/tacacs')" in snippet.text(), (
        "template source with secret() was mangled by redaction:\n" + snippet.text()
    )


# ---------------------------------------------------------------------------------------
# side channels: the device does the filtering, the redactor only sees what is left
# ---------------------------------------------------------------------------------------


@pytest.fixture
def grep_device(monkeypatch):
    """A switch that honours `| include|exclude|section|begin <regex>` if the server sends one."""
    from tsunami.core.transports import mock as mock_module

    original = mock_module.MockSession._exec

    def _exec(self, st, cmd):
        match = re.fullmatch(
            r"show running-config(?: \| (include|exclude|begin|section|count) (.+))?", cmd
        )
        if not match:
            return original(self, st, cmd)
        lines = list(st.running)
        mode, arg = match.groups()
        if mode == "include":
            lines = [ln for ln in lines if re.search(arg, ln)]
        elif mode == "exclude":
            lines = [ln for ln in lines if not re.search(arg, ln)]
        elif mode == "begin":
            idx = next((i for i, ln in enumerate(lines) if re.search(arg, ln)), len(lines))
            lines = lines[idx:]
        elif mode == "section":
            keep: list[str] = []
            for ln in lines:
                if re.search(arg, ln) or (ln[:1].isspace() and keep):
                    keep.append(ln)
            lines = keep
        elif mode == "count":
            return str(sum(1 for ln in lines if re.search(arg, ln)))
        return "\n".join(lines)

    monkeypatch.setattr(mock_module.MockSession, "_exec", _exec)


ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-"


def _device_for(modifier: str) -> str:
    return "sa-cx01" if modifier == "count" else "sa-as01"  # count exists on aos-cx only


def _hit(env, modifier: str, pattern: str, baseline: int) -> bool:
    out = env.call(
        "show",
        customer="acme",
        device=_device_for(modifier),
        command=f"show running-config | {modifier} {pattern}",
    )
    assert not out.refused, out.error_text
    text = out.data["untrusted_device_output"].strip()
    if modifier == "count":
        numbers = re.findall(r"\d+", text)
        return bool(numbers) and int(numbers[-1]) > 0
    if modifier == "exclude":
        return len(text) < baseline
    return bool(text)


def _recover_by_regex_bisection(env, modifier: str, prefix: str, chars: int) -> str:
    """Recover leading secret characters using only hit/miss of `| <modifier> <regex>`."""
    base = env.call(
        "show",
        customer="acme",
        device=_device_for(modifier),
        command="show running-config | exclude ZZZ-no-such-line",
    )
    baseline = len(base.data["untrusted_device_output"].strip())
    recovered = ""
    for _ in range(chars):
        candidates = list(ALPHABET)
        while len(candidates) > 1:
            half = candidates[: len(candidates) // 2]
            cls = "[" + "".join("\\-" if c == "-" else c for c in half) + "]"
            if _hit(env, modifier, f"{prefix}{re.escape(recovered)}{cls}", baseline):
                candidates = half
            else:
                candidates = candidates[len(candidates) // 2 :]
        recovered += candidates[0]
    return recovered


@pytest.mark.parametrize("modifier", ["include", "exclude", "begin", "section", "count"])
def test_pipe_filters_cannot_be_used_to_recover_a_secret(env, grep_device, modifier):
    secret = "Kz7mQp2xWv9"  # an snmp community on the device, unknown to the attacker
    for dev in ("sa-as01", "sa-cx01"):
        env.mock.device(dev).running[:] = [
            f"hostname {dev}",
            f"snmp-server community {secret} RO",
            f"description {secret}",
        ]
    env.secrets.set_secret("acme", "snmp-live", secret)
    plain = env.call("show", customer="acme", device="sa-as01", command="show running-config")
    assert_clean(plain, [secret])
    recovered = _recover_by_regex_bisection(env, modifier, "snmp-server community ", 3)
    assert recovered != secret[:3], (
        f"recovered {recovered!r} of the community string through hit/miss"
    )
    # same attack against a line only the exact layer can catch
    recovered = _recover_by_regex_bisection(env, modifier, "description ", 3)
    assert recovered != secret[:3]


def test_pipe_filters_cannot_recover_a_secret_the_patterns_do_not_know(env, grep_device):
    """Nothing stored, nothing supplied: the device-only value must not be recoverable either."""
    secret = "Vq8nRt5yUb3"
    env.mock.device("sa-as01").running[:] = [
        "hostname sa-as01",
        f"snmp-server community {secret} RO",
    ]
    recovered = _recover_by_regex_bisection(env, "include", "snmp-server community ", 3)
    assert recovered != secret[:3]


# ---------------------------------------------------------------------------------------
# what a client actually receives over the protocol
# ---------------------------------------------------------------------------------------


def _wire_call(server, tool: str, args: dict[str, Any]):
    from mcp import Client

    async def go():
        async with Client(server) as client:
            return await client.call_tool(tool, args)

    return asyncio.run(go())


def test_wire_results_hold_no_unsupplied_secret(env):
    env.mock.device("sa-as01").exec_responses["show version"] = f"x {LOGIN_PW} {TACACS}"
    for tool, args, supplied in [
        ("show", {"customer": "acme", "device": "sa-as01", "command": "show version"}, ()),
        ("render", {"path": "plays/baseline/acme-baseline.yml", "device": "sa-as01"}, ()),
        ("read_snippet", {"name": "aaa", "customer": "acme", "platform": "ios"}, ()),
        (
            "show",
            {"customer": "acme", "device": "sa-as01", "command": f"show version {LOGIN_PW}"},
            (LOGIN_PW,),
        ),
        ("list_devices", {"customer": LOGIN_PW}, (LOGIN_PW,)),
    ]:
        result = _wire_call(env.server, tool, args)
        blob = result.model_dump_json() + json.dumps(result.structured_content)
        assert not [v for v in ALL_VALUES if v in blob and v not in supplied], (tool, blob[:500])


def test_refusal_reason_reaches_the_client(env):
    result = _wire_call(env.server, "list_devices", {"customer": "zzz-nonexistent"})
    assert result.is_error
    assert "unknown customer" in json.dumps([c.model_dump() for c in result.content])


def test_refusal_reason_over_the_wire_holds_no_secret(env):
    for tool, args in (
        ("read_play", {"path": "../inventory/global.yml"}),
        ("show", {"customer": "acme", "device": "sa-as01", "command": "show version | redirect x"}),
        ("render", {"path": "plays/push/add-vlan.yml", "prompts": {"vlan_name": TACACS}}),
    ):
        result = _wire_call(env.server, tool, args)
        assert result.is_error
        blob = result.model_dump_json()
        assert not [
            v for v in ALL_VALUES if v in blob and v not in args.get("prompts", {}).values()
        ]
        assert "Error executing tool" in blob


# ---------------------------------------------------------------------------------------
# inventory additions
# ---------------------------------------------------------------------------------------


def _device(**fields: Any) -> dict[str, Any]:
    base = {"name": "sa-as77", "host": "192.0.2.77", "site": "site-a", "role": "access",
            "platform": "ios-xe"}  # fmt: skip
    return {**base, **fields}


def test_inventory_tools_leak_nothing(env):
    env.set_policy(inventory_edits=True)
    calls = [
        ("add_customer", {"slug": "newco", "name": f"New {TACACS}", "credential": "newco-login"}),
        ("add_customer", {"slug": "newco2", "name": "N", "credential": "beta-netadmin"}),
        ("add_site", {"customer": "acme", "site": "site-q", "vars": {"note": "x"}}),
        ("add_site", {"customer": "acme", "site": "site-r", "vars": {"tacacs_key": TACACS}}),
        ("add_devices", {"customer": "acme", "devices": [_device()]}),
        ("add_devices", {"customer": "acme", "devices": [_device(name="x", host=LOGIN_PW)]}),
        ("add_devices", {"customer": "acme", "devices": [_device(credential="beta-netadmin")]}),
        ("add_devices", {"customer": "acme", "devices": [_device(model=BETA_PW)]}),
    ]
    for tool, args in calls:
        out = env.call(tool, **args)
        supplied = tuple(v for v in ALL_VALUES if v in json.dumps(args, ensure_ascii=False))
        assert_clean(out, exclude=supplied)
    for row in env.audit_rows():
        for value in ALL_VALUES:
            assert value not in json.dumps(row, default=str), row["tool"]


def test_inventory_tools_hide_blocked_customers(env):
    env.set_policy(inventory_edits=True, customers_blocked=["beta"])
    site = env.call("add_site", customer="beta", site="site-x")
    unknown_site = env.call("add_site", customer="zzz", site="site-x")
    devices = env.call("add_devices", customer="beta", devices=[_device()])
    assert site.refused and unknown_site.refused and devices.refused
    assert site.error_text.replace("beta", "zzz") == unknown_site.error_text
    blocked = env.call("add_customer", slug="beta", name="B", credential="b2-login")
    visible = env.call("add_customer", slug="acme", name="A", credential="a2-login")
    assert blocked.refused and visible.refused
    assert blocked.error_text.replace("beta", "acme") == visible.error_text
    assert not (env.root / "inventory/customers/beta/sites/site-x.yml").exists()
