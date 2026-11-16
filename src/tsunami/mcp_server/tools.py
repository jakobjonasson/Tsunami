"""The MCP tools, as thin wrappers over core.

Tier 0 touches only the workspace. Tier 1 contacts devices read-only: the engine uses
credentials, and Claude sees only redacted output. Tier 2 requests changes, which wait
in history.db for a person to approve them in the GUI, unless the customer is a lab and
the policy allows direct apply. Every device connection made here refuses unknown host
keys: trust on first use is a GUI action.

The tools run inside the Tsunami GUI process, which alone holds the unlocked vault:
tsunami-mcp.exe forwards each call over the bridge (bridge.py) and only relays results.

See design.md, MCP interface: Tools, Approval flow, Leak paths, Process model.
"""

from __future__ import annotations

import hashlib
import inspect
import io
import json
import os
import queue
import re
import tempfile
import threading
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path, PureWindowsPath
from typing import Any

from ruamel.yaml import YAML

from tsunami.core.catalog import CatalogEntry
from tsunami.core.firmware.library import firmware_dir, list_images
from tsunami.core.inventory_edit import InventoryEditor
from tsunami.core.inventory_import import ERROR as IMPORT_ERROR
from tsunami.core.inventory_import import FIELDS as IMPORT_FIELDS
from tsunami.core.inventory_import import (
    ColumnMapping,
    CsvTable,
    ImportPlan,
    RowResult,
    apply_import,
    preview,
)
from tsunami.core.inventory_proposals import (
    MAX_PENDING,
    SOURCE_MCP,
    build_changes,
    check_applies,
    describe,
)
from tsunami.core.issues import Issue, Severity, TsunamiError
from tsunami.core.logins import customer_logins, jump_logins, referenced_logins
from tsunami.core.models.common import PLATFORM_FAMILY, SLUG_PATTERN, Platform, PlatformFamily
from tsunami.core.models.inventory import DEVICE_TYPES
from tsunami.core.models.jobs import Actor, DeviceStatus, JobStatus
from tsunami.core.models.play import (
    GLOBAL_CUSTOMER,
    ConnectionMethod,
    PlayType,
    PromptType,
    RunMode,
)
from tsunami.core.models.policy import TIERS, has_control_chars
from tsunami.core.models.proposals import ProposalStatus
from tsunami.core.operations import SHOW_MODE, OperationLog, connect_status
from tsunami.core.pathguard import MAX_DRAFT_BYTES, resolve_draft_write, resolve_read
from tsunami.core.policy import PolicyError, check_show_command, validate_allowlist_request
from tsunami.core.redact import redact_exception, supplied_values
from tsunami.core.render.environment import TemplateError, compile_template, make_environment
from tsunami.core.render.lookup import find_snippet
from tsunami.core.render.renderer import UnknownPlatformError
from tsunami.core.render.secret_refs import LOGIN_PREFIX, PROMPT_PREFIX
from tsunami.core.runner import Runner, RunRequest, masked_prompts, play_hash
from tsunami.core.services import play_entry, render_play
from tsunami.core.showfilter import FilterError, apply_filter, compile_pattern, split_command
from tsunami.core.state import (
    TABLES,
    StateError,
    StateTable,
    command_for,
    is_regex,
    parse_table,
    select,
)
from tsunami.core.state import table as state_table
from tsunami.core.targets import resolve_targets
from tsunami.core.transports.base import (
    ConnectTarget,
    JumpTarget,
)
from tsunami.core.workspace import secret_literal_keys, validate_play_text
from tsunami.core.yamlio import YamlLoadError, load_yaml_text
from tsunami.mcp_server.enforcement import McpApp, ToolRefused

PLAY_DIRS = ("plays", "drafts/plays")
SLUG_RE = re.compile(SLUG_PATTERN)
MAX_DEVICE_ROWS = 200
_DEVICE_KEYS = frozenset({*IMPORT_FIELDS, "type"})  # `type` is the CSV name for role
TOOL_NAMES = frozenset(
    {
        "list_customers",
        "list_devices",
        "list_plays",
        "read_play",
        "list_snippets",
        "read_snippet",
        "resolved_vars",
        "validate",
        "render",
        "write_draft",
        "delete_draft",
        "check",
        "compliance",
        "show",
        "request_allowlist_addition",
        "get_job",
        "list_jobs",
        "request_run",
        "cancel_job",
        "add_customer",
        "add_site",
        "add_role",
        "add_devices",
        "propose_inventory_change",
        "get_inventory_proposal",
        "get_policy",
        "show_many",
        "list_models",
        "add_model",
        "list_state_tables",
        "state",
        "list_firmware",
    }
)
MAX_REASON = 500
MAX_PENDING_PROPOSALS = MAX_PENDING
# What a password prompt gets in an MCP render or check: the value is never sent there, and
# Claude never supplies one (it would sit in the audit row and the job record).
PASSWORD_PLACEHOLDER = "placeholder-password"
PASSWORD_REFUSAL = (
    "prompts of type 'password' ({names}) are typed by a person in the Tsunami GUI; never "
    "pass a value for one. render and check mask it without a value; request_run cannot "
    "run a play that has one, so use a prompt of type 'secret' (saved in Credentials) instead"
)
LAB_APPROVAL = "lab policy (no approval)"  # approved_by of a lab run Claude started
# get_job: how much of each device to return, and how long it may wait for a change.
JOB_DETAILS = ("full", "diff", "summary")
MAX_WAIT_SECONDS = 50
WAIT_POLL_SECONDS = 0.5
# Job states nothing more will happen to: get_job never waits on them.
FINISHED_JOBS = frozenset(
    {
        JobStatus.DENIED,
        JobStatus.EXPIRED,
        JobStatus.COMPLETED,
        JobStatus.PARTIAL,
        JobStatus.NO_CHANGES,
        JobStatus.FAILED,
        JobStatus.INTERRUPTED,
        JobStatus.CANCELLED,
    }
)
SHOW_WORKERS = 4  # show_many: devices asked at once
# state: what a `where` may hold, and how many rows come back at most.
MAX_WHERE_COLUMNS = 8
MAX_MATCHER_ITEMS = 20
MAX_MATCHER_TEXT = 200
MAX_STATE_ROWS = 1000


class Tools:
    def __init__(self, app: McpApp) -> None:
        self.app = app
        self._cancels: dict[str, threading.Event] = {}

    def dispatch(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        """Run one tool by name with named arguments (calls arriving over the bridge)."""
        if tool not in TOOL_NAMES:
            raise ToolRefused(f"unknown tool {tool!r}")
        method = getattr(self, tool)
        try:
            bound = inspect.signature(method).bind(**args)
        except TypeError:
            raise ToolRefused(f"bad arguments for {tool}") from None
        return method(*bound.args, **bound.kwargs)

    # == tier 0: workspace only ======================================================

    def list_customers(self) -> dict[str, Any]:
        def run(policy):
            ws = self.app.workspace()
            return {
                "customers": [
                    {
                        "slug": slug,
                        "name": cust.config.name,
                        "lab": cust.config.lab,
                        "device_count": len(cust.devices),
                    }
                    for slug, cust in ws.customers.items()
                    if slug not in policy.customers_blocked
                ]
            }

        return self.app.call("list_customers", 0, {}, run)

    def list_devices(self, customer: str, site: str | None = None, role: str | None = None):
        def run(policy):
            cust = self.app.visible_customer(policy, self.app.workspace(), customer)
            return {
                "devices": [
                    {
                        "name": d.name,
                        "host": d.host,
                        "site": d.site,
                        "role": d.role,
                        "platform": str(d.platform),
                        "model": d.model,
                        "jump_host": _jump_view(cust, d),
                        "canary_priority": d.canary_priority,
                    }
                    for d in cust.devices
                    if (site is None or d.site == site) and (role is None or d.role == role)
                ]
            }

        args = {"customer": customer, "site": site, "role": role}
        return self.app.call("list_devices", 0, args, run, customer=customer)

    def list_plays(
        self,
        type: str | None = None,
        customer: str | None = None,  # noqa: A002
        include_drafts: bool = False,
    ):
        def run(policy):
            ws = self.app.workspace()
            plays = []
            for path, entry in ws.plays.items():
                play = entry.play
                if entry.draft and not include_drafts:
                    continue
                if play is not None and play.customer in policy.customers_blocked:
                    continue
                if customer and (play is None or play.customer != customer):
                    continue
                if type and (play is None or play.type != type):
                    continue
                issues = ws.issues_for(path)
                plays.append(
                    {
                        "path": path.as_posix(),
                        "name": play.name if play else None,
                        "type": str(play.type) if play else None,
                        "customer": play.customer if play else None,
                        "draft": entry.draft,
                        "errors": sum(i.severity == Severity.ERROR for i in issues),
                        "warnings": sum(i.severity == Severity.WARNING for i in issues),
                    }
                )
            return {"plays": plays}

        args = {"type": type, "customer": customer, "include_drafts": include_drafts}
        known = self._drafts() if include_drafts else []
        return self.app.call("list_plays", 0, args, run, customer=customer, known=known)

    def read_play(self, path: str):
        def run(policy):
            ws = self.app.workspace()
            real = resolve_read(ws.root, path, allowed=PLAY_DIRS)
            text = _read_text(real)
            self._check_play_customer(policy, ws, text)
            return {"path": _rel(ws.root, real), "text": text}

        customer, known = self._customer_of(path), self._drafts_for(path)
        return self.app.call("read_play", 0, {"path": path}, run, customer=customer, known=known)

    def list_snippets(self, customer: str | None = None, platform: str | None = None):
        def run(policy):
            ws = self.app.workspace()
            if customer:
                self.app.visible_customer(policy, ws, customer)
            files = sorted((ws.root / "snippets").rglob("*.j2"))
            names = sorted({f.stem for f in files})
            if customer and platform:
                plat = _platform(platform)
                found = []
                for name in names:
                    hit = find_snippet(ws.root, name, customer=customer, platform=plat)
                    if hit is not None:
                        found.append({"name": name, "path": hit.as_posix(), "level": _level(hit)})
                return {"snippets": found}
            listing = []
            for f in files:
                rel = f.relative_to(ws.root)
                if rel.parts[1] == "customers" and (
                    rel.parts[2] in policy.customers_blocked
                    or (customer and rel.parts[2] != customer)
                ):
                    continue
                listing.append({"name": f.stem, "path": rel.as_posix(), "level": _level(rel)})
            return {"snippets": listing}

        args = {"customer": customer, "platform": platform}
        return self.app.call("list_snippets", 0, args, run, customer=customer)

    def read_snippet(self, name: str, customer: str | None = None, platform: str | None = None):
        def run(policy):
            ws = self.app.workspace()
            plat = _platform(platform or (ws.config.defaults.platform if ws.config else "ios-xe"))
            if customer:
                self.app.visible_customer(policy, ws, customer)
                hit = find_snippet(ws.root, name, customer=customer, platform=plat)
            else:
                family = PLATFORM_FAMILY[plat]
                candidates = [
                    Path("snippets", "platform", plat, f"{name}.j2"),
                    Path("snippets", "common", family, f"{name}.j2"),
                ]
                hit = next((c for c in candidates if (ws.root / c).is_file()), None)
            if hit is None:
                raise ToolRefused(f"snippet {name!r} not found for {plat}")
            real = resolve_read(ws.root, hit.as_posix(), allowed=("snippets",))
            return {"path": hit.as_posix(), "level": _level(hit), "content": _read_text(real)}

        args = {"name": name, "customer": customer, "platform": platform}
        return self.app.call("read_snippet", 0, args, run, customer=customer)

    def resolved_vars(self, customer: str, target: str | None = None, device: str | None = None):
        alias = device

        def run(policy):
            device = _target_name(target, alias, required=True)
            ws = self.app.workspace()
            cust = self.app.visible_customer(policy, ws, customer)
            if cust.device(device) is None:
                raise ToolRefused(f"unknown device {device!r}")
            resolved = ws.resolve_vars(customer, device=device)
            provenance = {
                ".".join(path): {
                    "level": source.level.label,
                    "file": source.file.as_posix() if source.file else None,
                }
                for path, source in resolved.provenance.items()
            }
            return {"values": resolved.values, "provenance": provenance}

        args = {"customer": customer, "target": target or device}
        return self.app.call("resolved_vars", 0, args, run, customer=customer)

    def validate(self, path: str | None = None, content: str | None = None):
        def run(policy):
            if (path is None) == (content is None):
                raise ToolRefused("give exactly one of path or content")
            ws = self.app.workspace()
            if path is not None:
                real = resolve_read(ws.root, path, allowed=PLAY_DIRS)
                text, rel = _read_text(real), Path(_rel(ws.root, real))
            else:
                text, rel = content, Path("drafts/plays/push/unsaved.yml")
            self._check_play_customer(policy, ws, text)
            issues = validate_play_text(ws, text, rel, self.app.secrets)
            return _issues_result(issues)

        args = {"path": path, "content": _short(content)}
        customer, known = self._customer_of(path, content), self._drafts_for(path)
        known += [content] if content is not None else []  # args hold only a prefix
        return self.app.call("validate", 0, args, run, customer=customer, known=known)

    def render(
        self,
        path: str,
        target: str | None = None,
        prompts: dict | None = None,
        customer: str | None = None,
        device: str | None = None,
    ):
        alias = device

        def run(policy):
            device = _target_name(target, alias, required=False)
            ws = self.app.workspace()
            real = resolve_read(ws.root, path, allowed=PLAY_DIRS)
            rel = _rel(ws.root, real)
            self._check_play_customer(policy, ws, _read_text(real), customer)
            entry = self._entry(ws, path, customer)
            answers = self._without_passwords(entry, prompts)
            try:
                rendering = render_play(
                    ws,
                    rel,
                    device=device,
                    prompts=answers,
                    customer=customer,
                    secrets=self.app.secrets,
                )
            except UnknownPlatformError:
                if device is None:
                    self._refuse_untargeted(policy, ws, entry)
                raise
            return {
                "path": rel,
                "device": device,
                "platform": str(rendering.platform),
                "lines": rendering.masked_lines(),
                "text": rendering.masked_text(),
                "secret_names": secret_names(rendering.secret_refs),
                "render_hash": rendering.render_hash(),
            }

        args = {
            "path": path,
            "target": target or device,
            "prompts": self._audit_prompts(path, customer, prompts),
            "customer": customer,
        }
        bound, known = self._customer_of(path, customer=customer), self._drafts_for(path)
        return self.app.call("render", 0, args, run, customer=bound, known=known)

    def _refuse_untargeted(self, policy, ws, entry) -> None:
        """A render with no target whose platform comes only from a device: say how many
        devices the play targets, rather than the renderer's 'platform is unknown'."""
        play = entry.play
        if play is None or play.type == PlayType.INITIAL or not play.targets:
            return
        cust = self.app.visible_customer(policy, ws, play.customer)
        found = resolve_targets(
            play.targets, cust.devices, sites=cust.sites.keys(), roles=ws.roles.keys()
        ).devices
        if len(found) > 1:
            raise ToolRefused(f"pass target: this play targets {len(found)} devices")
        if found:
            raise ToolRefused(f"pass target: this play targets one device, {found[0].name}")

    def write_draft(self, path: str, content: str):
        def run(policy):
            if not isinstance(content, str) or "\x00" in content:
                raise ToolRefused("content must be text")
            if len(content.encode("utf-8")) > MAX_DRAFT_BYTES:
                raise ToolRefused(f"a draft is limited to {MAX_DRAFT_BYTES // 1024} KB")
            ws = self.app.workspace()
            target = resolve_draft_write(ws.root, path)
            rel = Path(_rel(ws.root, target))
            if target.suffix == ".yml":
                self._check_play_customer(policy, ws, content)
            elif rel.parts[2] == "customers":
                self.app.visible_customer(policy, ws, rel.parts[3])
            _atomic_write(target, content)
            digest = hashlib.sha256(target.read_bytes()).hexdigest()
            self.app.history.record_mcp_draft(rel.as_posix(), digest)
            if target.suffix == ".yml":
                issues = validate_play_text(self.app.workspace(), content, rel, self.app.secrets)
                result = _issues_result(issues)
            else:
                result = _template_check(content, rel)
            return {"path": rel.as_posix(), "written": True, **result}

        args = {"path": path, "content": _short(content)}
        customer = self._customer_of(None, content)
        return self.app.call("write_draft", 0, args, run, customer=customer, known=[content])

    def delete_draft(self, path: str):
        def run(policy):
            ws = self.app.workspace()
            target = resolve_draft_write(ws.root, path)
            if not target.is_file():
                raise ToolRefused(f"no draft at {path}")
            target.unlink()
            self.app.history.forget_mcp_draft(_rel(ws.root, target))
            return {"path": _rel(ws.root, target), "deleted": True}

        return self.app.call("delete_draft", 0, {"path": path}, run)

    # == tier 0: inventory additions (policy: inventory_edits) ===========================
    # Add only: nothing here edits or deletes. Customers are never lab customers (a lab
    # customer can be changed without approval), and a device may only name a login its
    # customer already uses. Devices added here are recorded, and MCP connections never
    # make first contact with them (see _mcp_added).

    def add_customer(self, slug: str, name: str, credential: str):
        def run(policy):
            ws = self.app.workspace()
            editor = self._inventory_editor(policy, ws)
            _plain_text(name, "name", 100)
            taken = slug in ws.customers or slug in policy.customers_blocked
            if taken or slug == GLOBAL_CUSTOMER or not SLUG_RE.fullmatch(slug or ""):
                # One message for taken, blocked and reserved: never confirm a blocked one.
                raise ToolRefused(
                    f"customer {slug!r} cannot be added: the slug is taken or not valid"
                )
            if not SLUG_RE.fullmatch(credential or ""):
                raise ToolRefused(
                    "credential must be a login name: lowercase letters, digits and dashes"
                )
            # Any login another customer refers to, its jump host and push_logins included.
            if credential in referenced_logins(ws.customers.values()):
                raise ToolRefused(
                    f"login {credential!r} already belongs to another customer; a person can "
                    "share a login between customers in the GUI"
                )
            try:
                editor.create_customer(slug, name=name, credential=credential, lab=False)
            except TsunamiError as exc:
                raise ToolRefused(str(exc)) from None
            self.app.inventory_changed()
            return {
                "customer": slug,
                "created": True,
                "lab": False,
                "note": f"A person must add the login {credential!r} in Tsunami Credentials "
                "before anything can connect to this customer's devices.",
            }

        args = {"slug": slug, "name": name, "credential": credential}
        # No customer= here: the block-list check would answer "unknown customer" for a
        # blocked slug, which would tell it apart from a free one.
        return self.app.call("add_customer", 0, args, run)

    def add_site(self, customer: str, site: str, vars: dict[str, Any] | None = None):  # noqa: A002
        def run(policy):
            ws = self.app.workspace()
            editor = self._inventory_editor(policy, ws)
            cust = self.app.visible_customer(policy, ws, customer)
            if not SLUG_RE.fullmatch(site or ""):
                raise ToolRefused("site must be lowercase letters, digits and dashes")
            if site in cust.sites:
                raise ToolRefused(f"site {site!r} already exists for {customer!r}")
            values = {} if vars is None else vars
            if not isinstance(values, dict):
                raise ToolRefused("vars must be a mapping of variable names to values")
            _refuse_secret_vars(values)
            try:
                editor.create_site(customer, site, vars_text=_yaml_text(values))
            except TsunamiError as exc:
                raise ToolRefused(str(exc)) from None
            self.app.inventory_changed()
            return {"customer": customer, "site": site, "created": True}

        args = {"customer": customer, "site": site, "vars": vars}
        return self.app.call("add_site", 0, args, run, customer=customer)

    def add_role(self, role: str, vars: dict[str, Any] | None = None):  # noqa: A002
        def run(policy):
            ws = self.app.workspace()
            editor = self._inventory_editor(policy, ws)
            if not SLUG_RE.fullmatch(role or ""):
                raise ToolRefused("role must be lowercase letters, digits and dashes")
            if role in DEVICE_TYPES:
                raise ToolRefused(
                    f"role {role!r} is a built-in device type and cannot be redefined"
                )
            if role in ws.roles:
                raise ToolRefused(f"role {role!r} already exists")
            values = {} if vars is None else vars
            if not isinstance(values, dict):
                raise ToolRefused("vars must be a mapping of variable names to values")
            _refuse_secret_vars(values)
            try:
                editor.create_role(role, vars_text=_yaml_text(values))
            except TsunamiError as exc:
                raise ToolRefused(str(exc)) from None
            self.app.inventory_changed()
            return {"role": role, "created": True}

        return self.app.call("add_role", 0, {"role": role, "vars": vars}, run)

    def add_devices(
        self,
        customer: str,
        devices: list[dict[str, Any]],
        create_sites: bool = False,
        create_roles: bool = False,
    ):
        def run(policy):
            ws = self.app.workspace()
            editor = self._inventory_editor(policy, ws)
            cust = self.app.visible_customer(policy, ws, customer)
            if not isinstance(devices, list) or not devices:
                raise ToolRefused("devices must be a non-empty list")
            if len(devices) > MAX_DEVICE_ROWS:
                raise ToolRefused(f"at most {MAX_DEVICE_ROWS} devices per call")
            # create_roles: custom roles the rows name are created first, so the rows
            # validate, and removed again if no row that uses them is imported.
            new_roles = _new_roles(devices, ws) if create_roles else []
            for role in new_roles:
                try:
                    editor.create_role(role)
                except TsunamiError as exc:
                    raise ToolRefused(str(exc)) from None
            try:
                plan = _device_plan(ws.root, cust, devices, bool(create_sites))
                summary = apply_import(ws.root, customer, plan, editor=editor)
            except (TsunamiError, ToolRefused) as exc:
                _remove_roles(ws.root, new_roles)
                if isinstance(exc, ToolRefused):
                    raise
                raise ToolRefused(str(exc)) from None
            used = {r.fields.get("role") for r in plan.rows if r.importable}
            _remove_roles(ws.root, [r for r in new_roles if r not in used])
            new_roles = [r for r in new_roles if r in used]
            for row in plan.rows:
                if row.importable:
                    self.app.history.record_mcp_device(
                        customer, row.fields["name"], str(row.fields["host"])
                    )
            if summary.imported or new_roles:
                self.app.inventory_changed()
            return {
                "customer": customer,
                "imported": summary.imported,
                "sites_created": summary.sites_created,
                "roles_created": new_roles,
                "rows": [
                    {
                        "row": r.number,
                        "name": r.fields.get("name"),
                        "status": r.status,
                        "reason": r.reason,
                    }
                    for r in plan.rows
                ],
                "note": "You cannot make first contact with devices you added: a person must "
                "trust their host keys in the Tsunami GUI (Inventory, Trust host keys) before "
                "show, check or request_run can reach them.",
            }

        args = {
            "customer": customer,
            "devices": devices,
            "create_sites": create_sites,
            "create_roles": create_roles,
        }
        return self.app.call("add_devices", 0, args, run, customer=customer)

    def list_state_tables(self, family: str | None = None):
        """Every state table with its columns and the show command per platform family
        that fills it (null where the family cannot read it)."""

        def run(policy):
            families = [str(f) for f in PlatformFamily]
            if family is not None and family not in families:
                raise ToolRefused(
                    f"unknown family {family!r}; the families are {', '.join(families)}"
                )
            tables = []
            for spec in TABLES.values():
                commands = {f: _state_command(f, spec.name) for f in families}
                if family is not None and commands[family] is None:
                    continue
                tables.append(
                    {
                        "name": spec.name,
                        "title": spec.title,
                        "key": list(spec.key),
                        "columns": [
                            {"name": c.name, "kind": c.kind, "help": c.help} for c in spec.columns
                        ],
                        "compared": list(spec.compared),
                        "commands": commands,
                    }
                )
            return {"tables": tables}

        return self.app.call("list_state_tables", 0, {"family": family}, run)

    def list_firmware(self):
        """The files in the firmware folder with their hashes. That folder only."""

        def run(policy):
            folder = firmware_dir(self.app.paths, self.app.settings())
            try:
                found = list_images(folder)
            except OSError as exc:
                raise ToolRefused(
                    f"cannot read the firmware folder ({type(exc).__name__})"
                ) from None
            return {
                "folder": str(folder),
                "folder_exists": folder.is_dir(),
                "files": [
                    {
                        "name": f.name,
                        "size": f.size,
                        "md5": f.hashes["md5"],
                        "sha256": f.hashes["sha256"],
                        "sha512": f.hashes["sha512"],
                    }
                    for f in found
                ],
            }

        return self.app.call("list_firmware", 0, {}, run)

    def list_models(self, platform: str | None = None):
        """Hardware models a device's model field can name: built-in and the workspace's
        custom ones."""

        def run(policy):
            ws = self.app.workspace()
            plat = _platform(platform) if platform else None
            return {
                "models": [
                    _model_view(e)
                    for e in ws.catalog.entries
                    if (plat is None or e.platform == plat) and e.selectable
                ]
            }

        return self.app.call("list_models", 0, {"platform": platform}, run)

    def add_model(
        self,
        id: str,  # noqa: A002
        label: str,
        platform: str,
        model_pattern: str | None = None,
        examples: list[str] | None = None,
        console_baud: int | None = None,
    ):
        """Add a custom hardware model to the workspace (inventory/models.yml). Add only."""

        def run(policy):
            ws = self.app.workspace()
            editor = self._inventory_editor(policy, ws)
            if examples is not None and not isinstance(examples, list):
                raise ToolRefused("examples must be a list of model numbers")
            fields = {
                "id": id,
                "label": label,
                "platform": platform,
                "model_pattern": model_pattern,
                "examples": examples,
                "console_baud": console_baud,
            }
            redactor = self.app.full_redactor()  # the workspace never holds a secret value
            if any(redactor.text(v) != v for v in _strings(fields)):
                raise ToolRefused("a model field looks like a secret; refused")
            try:
                entry = editor.add_model(fields)
            except TsunamiError as exc:
                raise ToolRefused(str(exc)) from None
            self.app.inventory_changed()
            return {"model": _model_view(entry), "created": True}

        args = {
            "id": id,
            "label": label,
            "platform": platform,
            "model_pattern": model_pattern,
            "examples": examples,
            "console_baud": console_baud,
        }
        return self.app.call("add_model", 0, args, run)

    def _inventory_editor(self, policy, ws) -> InventoryEditor:
        if not policy.inventory_edits:
            raise ToolRefused(
                "changing the inventory over MCP is turned off in Tsunami Settings (MCP, Inventory)"
            )
        return InventoryEditor(ws.root)

    # == tier 2: inventory proposals (policy: inventory_edits) ===========================
    # Edits and removals of existing devices. Nothing is written here: the proposal waits
    # in history.db until a person approves it in the GUI, for lab customers too.

    def propose_inventory_change(
        self, customer: str, changes: list[dict[str, Any]], reason: str | None = None
    ):
        def run(policy):
            ws = self.app.workspace()
            editor = self._inventory_editor(policy, ws)
            cust = self.app.visible_customer(policy, ws, customer)
            note = "" if reason is None else _plain_text(reason, "reason", MAX_REASON)
            waiting = self.app.history.list_inventory_proposals(
                status=ProposalStatus.AWAITING_APPROVAL, limit=MAX_PENDING_PROPOSALS
            )
            if len(waiting) >= MAX_PENDING_PROPOSALS:
                raise ToolRefused(
                    "too many inventory proposals are waiting for approval; ask the user to "
                    "review them in Tsunami (Approvals)"
                )
            # A login a proposal sets (the device's or its jump host's) must be one this
            # customer logs in with: never another customer's, never a push-only login.
            logins = customer_logins(cust) | jump_logins(cust)
            try:
                stored = build_changes(editor.read_devices(customer), changes, logins=logins)
                check_applies(ws.root, customer, stored)
            except TsunamiError as exc:
                raise ToolRefused(str(exc)) from None
            redactor = self.app.full_redactor()
            for change in stored:
                for value in _strings(change["set"]):
                    if redactor.text(value) != value:
                        raise ToolRefused("a proposed value looks like a secret; refused")
            proposal_id = self.app.history.create_inventory_proposal(
                customer=customer,
                source=SOURCE_MCP,
                reason=redactor.text(note),
                changes=stored,
            )
            return {
                "proposal_id": proposal_id,
                "status": str(ProposalStatus.AWAITING_APPROVAL),
                "changes": describe(stored),
                "note": "Nothing has changed. A person must approve this in the Tsunami GUI "
                "(Approvals); it expires if nobody does. Show the user the changes, and poll "
                "get_inventory_proposal for the outcome.",
            }

        args = {"customer": customer, "changes": changes, "reason": reason}
        return self.app.call("propose_inventory_change", 2, args, run, customer=customer)

    def get_inventory_proposal(self, proposal_id: str):
        def run(policy):
            try:
                proposal = self.app.history.get_inventory_proposal(proposal_id)
            except TsunamiError:
                proposal = None
            if proposal is None or proposal.customer in policy.customers_blocked:
                raise ToolRefused(f"unknown proposal {proposal_id!r}")
            return {
                "proposal_id": proposal.id,
                "customer": proposal.customer,
                "status": str(proposal.status),
                "created_at": proposal.created_at.isoformat(),
                "decided_by": proposal.decided_by,
                "error": proposal.error,
                "changes": describe(proposal.changes),
            }

        args = {"proposal_id": proposal_id}
        return self.app.call("get_inventory_proposal", 0, args, run)

    # == tier 1: read-only device access =================================================

    def check(
        self,
        path: str,
        targets: list[str] | None = None,
        prompts: dict | None = None,
        customer: str | None = None,
    ):
        return self._read_only_job("check", RunMode.CHECK, path, targets, prompts, customer)

    def compliance(
        self,
        path: str,
        targets: list[str] | None = None,
        prompts: dict | None = None,
        customer: str | None = None,
    ):
        return self._read_only_job(
            "compliance", RunMode.COMPLIANCE, path, targets, prompts, customer
        )

    def show(
        self,
        customer: str,
        command: str,
        target: str | None = None,
        device: str | None = None,
    ):
        alias = device

        def run(policy):
            device = _target_name(target, alias, required=True)
            ws = self.app.workspace()
            cust = self.app.visible_customer(policy, ws, customer)
            dev = cust.device(device)
            if dev is None:
                raise ToolRefused(f"unknown device {device!r}")
            normalized, parts = _show_parts(policy, dev, command)
            connect = self._show_target(policy, cust, dev, self._mcp_added(cust))
            # Recorded in History as an operation: the command and the fully redacted
            # output Claude was given (every stored secret masked, none exempt).
            record = OperationLog(
                self.app.history,
                mode=SHOW_MODE,
                customer=customer,
                actor=Actor.MCP,
                devices=[dev.name],
                summary={"command": normalized},
            )
            full = self.app.full_redactor()
            transport = self.app.transport()
            try:
                output = _run_show(transport, connect, parts, full)
            except Exception as exc:
                record.device(dev.name, connect_status(exc), error=redact_exception(exc, full))
                record.finish()
                raise
            finally:
                _close(transport)
            record.device(dev.name, DeviceStatus.SUCCESS, output=f"{normalized}\n{output}")
            record.finish()
            # The command is not echoed back: Claude knows what it sent.
            return {"device": dev.name, "untrusted_device_output": output}

        args = {"customer": customer, "target": target or device, "command": command}
        return self.app.call("show", 1, args, run, customer=customer)

    def state(self, customer: str, target: str, table: str, where: dict[str, Any] | None = None):
        """One state table's rows from one device. The table's show command goes through
        the show allowlist and the connection is show's; every text cell is redacted
        before `where` filters the rows, so a filter cannot confirm a secret."""

        def run(policy):
            ws = self.app.workspace()
            cust = self.app.visible_customer(policy, ws, customer)
            dev = cust.device(target) if isinstance(target, str) else None
            if dev is None:
                raise ToolRefused(f"unknown device {target!r}")
            family = PLATFORM_FAMILY[dev.platform]
            try:
                if not isinstance(table, str):
                    raise StateError(
                        "unknown state table: pass a table name from list_state_tables"
                    )
                spec = state_table(table)
                command = command_for(family, table)
            except StateError as exc:
                raise ToolRefused(str(exc)) from None
            try:
                normalized = check_show_command(policy, family, command)
            except PolicyError:
                raise ToolRefused(
                    f"{dev.name}: the {spec.name} table reads `{command}` on {family}, which "
                    f"is not on the show allowlist; call request_allowlist_addition for "
                    f"`{command}` ({family}) and ask the user to approve it"
                ) from None
            base = split_command(normalized).base
            matchers = _state_where(spec, where)
            connect = self._show_target(policy, cust, dev, self._mcp_added(cust))
            record = OperationLog(
                self.app.history,
                mode=SHOW_MODE,
                customer=customer,
                actor=Actor.MCP,
                devices=[dev.name],
                summary={"command": normalized},
            )
            full = self.app.full_redactor()
            transport = self.app.transport()
            try:
                rows = _read_state(transport, connect, family, spec, base, full)
            except Exception as exc:
                error = redact_exception(exc, full)
                record.device(dev.name, connect_status(exc), error=error)
                record.finish()
                if isinstance(exc, StateError):
                    raise ToolRefused(full.text(str(exc))) from None
                raise
            finally:
                _close(transport)
            # `where` runs on the redacted cells, as a show pipe runs on redacted output.
            chosen = select(spec, rows, matchers)
            shown = chosen[:MAX_STATE_ROWS]
            text = json.dumps(shown, ensure_ascii=False)
            record.device(dev.name, DeviceStatus.SUCCESS, output=f"{normalized}\n{text}")
            record.finish()
            result: dict[str, Any] = {
                "device": dev.name,
                "table": spec.name,
                "row_count": len(shown),
                "untrusted_device_output": shown,
            }
            if len(chosen) > len(shown):
                result["truncated"] = f"{len(chosen)} rows matched; the first {len(shown)} shown"
            return result

        args = {"customer": customer, "target": target, "table": table, "where": where}
        return self.app.call("state", 1, args, run, customer=customer)

    def show_many(
        self,
        customer: str,
        command: str,
        devices: list[str] | None = None,
        site: str | None = None,
        role: str | None = None,
    ):
        """One allowlisted show command on many devices, as a job (at most
        max_check_targets devices). Each device's output is in get_job."""

        def run(policy):
            ws = self.app.workspace()
            cust = self.app.visible_customer(policy, ws, customer)
            if devices is not None:
                if not isinstance(devices, list) or not devices:
                    raise ToolRefused("devices must list at least one device, or be left out")
                unknown = sorted({str(d) for d in devices} - {d.name for d in cust.devices})
                if unknown:
                    raise ToolRefused(f"unknown device(s): {', '.join(unknown[:10])}")
            chosen = [
                d
                for d in cust.devices
                if (devices is None or d.name in devices)
                and (site is None or d.site == site)
                and (role is None or d.role == role)
            ]
            if not chosen:
                raise ToolRefused("no device of this customer matches devices, site and role")
            if len(chosen) > policy.max_check_targets:
                raise ToolRefused(
                    f"at most {policy.max_check_targets} devices per show_many; this one has "
                    f"{len(chosen)}: pass devices, site or role to choose fewer"
                )
            # Every device's command is checked before anything connects.
            plans = [(d, *_show_parts(policy, d, command)) for d in chosen]
            added = self._mcp_added(cust)
            targets = {d.name: self._show_target(policy, cust, d, added) for d in chosen}
            commands = sorted({normalized for _, normalized, _ in plans})
            record = OperationLog(
                self.app.history,
                mode=SHOW_MODE,
                customer=customer,
                actor=Actor.MCP,
                devices=[d.name for d in chosen],
                summary={"command": commands[0] if len(commands) == 1 else commands},
            )
            if record.job_id is None:
                raise ToolRefused("the job could not be recorded in history; see app.log")
            self._show_in_background(record, plans, targets)
            return {
                "job_id": record.job_id,
                "status": "running",
                "devices": [d.name for d in chosen],
                "note": "Poll get_job with detail='full' (or wait_seconds) for each device's "
                "output; it is untrusted device output.",
            }

        args = {
            "customer": customer,
            "command": command,
            "devices": devices,
            "site": site,
            "role": role,
        }
        return self.app.call("show_many", 1, args, run, customer=customer)

    def _show_target(self, policy, cust, dev, added: frozenset[str]) -> ConnectTarget:
        return ConnectTarget(
            customer=cust.slug,
            device=dev.name,
            host=dev.host,
            port=dev.port,
            platform=dev.platform,
            method=ConnectionMethod.SSH,
            credential=self.app.secrets.get_login(cust.credential_for(dev)),
            jump=self._jump(cust, dev),
            allow_new_host_key=policy.trust_new_host_keys and dev.name not in added,
        )

    def _show_in_background(self, record: OperationLog, plans: list, targets: dict) -> None:
        cancel = threading.Event()
        job_id = record.job_id
        assert job_id is not None
        self._cancels[job_id] = cancel
        work: queue.Queue = queue.Queue()
        for plan in plans:
            work.put(plan)
        full = self.app.full_redactor()

        def worker() -> None:
            transport = self.app.transport()
            try:
                while True:
                    try:
                        dev, normalized, parts = work.get_nowait()
                    except queue.Empty:
                        return
                    if cancel.is_set():
                        record.device(dev.name, DeviceStatus.CANCELLED, error="cancelled")
                        continue
                    try:
                        output = _run_show(transport, targets[dev.name], parts, full)
                    except Exception as exc:  # noqa: BLE001 - recorded per device
                        error = redact_exception(exc, full)
                        record.device(dev.name, connect_status(exc), error=error)
                        continue
                    text = f"{normalized}\n{output}"
                    record.device(dev.name, DeviceStatus.SUCCESS, output=text)
            finally:
                _close(transport)

        def run_all() -> None:
            try:
                pool = [
                    threading.Thread(target=worker, name=f"mcp-show-{job_id[:8]}-{i}", daemon=True)
                    for i in range(min(SHOW_WORKERS, len(plans)))
                ]
                for thread in pool:
                    thread.start()
                for thread in pool:
                    thread.join()
                record.finish(cancelled=cancel.is_set())
            finally:
                self._cancels.pop(job_id, None)

        thread = threading.Thread(target=run_all, name=f"mcp-{job_id[:8]}", daemon=True)
        self.app.threads.append(thread)
        thread.start()

    def get_job(
        self,
        job_id: str,
        devices: list[str] | None = None,
        detail: str = "full",
        wait_seconds: int = 0,
        since: str | None = None,
    ):
        """A job's status and per-device results.

        devices: only these devices' rows. detail: "full" (render, output and diff),
        "diff" (the diff, no render or output) or "summary" (status and error only).
        wait_seconds: wait up to this long (at most MAX_WAIT_SECONDS) for the job's state
        to differ from since (a state token from an earlier get_job; default: its state
        when the call started), returning at once if the job has finished. One waiting call
        replaces many polls under the rate limit.
        """

        def run(policy):
            if detail not in JOB_DETAILS:
                raise ToolRefused(f"detail must be one of {', '.join(JOB_DETAILS)}")
            if devices is not None and (
                not isinstance(devices, list) or not all(isinstance(d, str) for d in devices)
            ):
                raise ToolRefused("devices must be a list of device names")
            if isinstance(wait_seconds, bool) or not isinstance(wait_seconds, int | float):
                raise ToolRefused("wait_seconds must be a number of seconds")
            wait = max(0.0, min(float(wait_seconds), MAX_WAIT_SECONDS))
            job = self._visible_job(policy, job_id)
            rows = self.app.history.devices(job_id)
            state = job_state(job, rows)
            if wait:
                target = since if since is not None else state
                deadline = time.monotonic() + wait
                while (
                    state == target
                    and job.status not in FINISHED_JOBS
                    and time.monotonic() < deadline
                ):
                    time.sleep(min(WAIT_POLL_SECONDS, max(0.0, deadline - time.monotonic())))
                    job = self._visible_job(policy, job_id)
                    rows = self.app.history.devices(job_id)
                    state = job_state(job, rows)
            phase, note = job_phase(job)
            wanted = None if devices is None else set(devices)
            shown = [d for d in rows if wanted is None or d.device in wanted]
            result = {
                "job_id": job.id,
                "play": job.play_path,
                "customer": job.customer,
                "mode": job.mode,
                "phase": phase,
                **({"note": note} if note else {}),
                "status": str(job.status),
                "finished": job.status in FINISHED_JOBS,
                "state": state,
                "actor": str(job.actor),
                "created_at": job.created_at.isoformat(),
                "approved_by": job.approved_by,
                "summary": job.summary,
                "counts": dict(Counter(str(d.status) for d in rows)),
                "devices": [_device_view(d, detail) for d in shown],
            }
            if wanted is not None:
                missing = sorted(wanted - {d.device for d in rows})
                if missing:
                    result["devices_not_in_job"] = missing
            return result

        args = {
            "job_id": job_id,
            "devices": devices,
            "detail": detail,
            "wait_seconds": wait_seconds,
            "since": since,
        }
        return self.app.call("get_job", 1, args, run, known=self._job_supplied(job_id))

    def list_jobs(self, customer: str | None = None, limit: int = 20):
        def run(policy):
            jobs = self.app.history.list_jobs(customer=customer, limit=max(1, min(limit, 200)))
            return {
                "jobs": [
                    {
                        "job_id": j.id,
                        "created_at": j.created_at.isoformat(),
                        "actor": str(j.actor),
                        "play": j.play_path,
                        "customer": j.customer,
                        "mode": j.mode,
                        "status": str(j.status),
                    }
                    for j in jobs
                    if j.customer not in policy.customers_blocked
                ]
            }

        args = {"customer": customer, "limit": limit}
        return self.app.call("list_jobs", 1, args, run, customer=customer)

    # == tier 2: change requests =========================================================

    def request_run(
        self,
        path: str,
        targets: list[str] | None = None,
        prompts: dict | None = None,
        tags: list[str] | None = None,
        customer: str | None = None,
    ):
        def run(policy):
            ws = self.app.workspace()
            entry = self._entry(ws, path, customer)
            play = entry.play
            cust = self.app.visible_customer(policy, ws, play.customer)
            if play.type == PlayType.INITIAL:
                raise ToolRefused("initial (day-0) plays run over a console cable from the GUI")
            passwords = _password_prompts(entry)
            if passwords:
                raise ToolRefused(PASSWORD_REFUSAL.format(names=", ".join(sorted(passwords))))
            if targets is not None and not targets:
                raise ToolRefused("targets must list at least one device, or be left out")
            direct = cust.config.lab and policy.apply == "lab_only"
            if entry.draft and not (direct and policy.lab_allow_drafts):
                raise ToolRefused(
                    "drafts cannot run against devices; a person must promote it in the GUI"
                )
            # No targets: every device the play targets.
            devices = [d.name for d in self._check_targets(ws, cust, play, targets)]
            if len(devices) > policy.max_check_targets:
                raise ToolRefused(
                    f"at most {policy.max_check_targets} devices per request; this one has "
                    f"{len(devices)}: pass targets to choose some"
                )
            mode = RunMode.APPLY if direct else RunMode.CHECK
            # Everything that would stop the run before it reached a device, now: prompt
            # answers, renders, and every secret the play uses being saved.
            self._preflight(entry, mode, prompts, tags, devices)
            job_id = self._create_job(entry.path, play.customer, "apply", prompts, tags, devices)
            if direct:
                self.app.history.update_job(
                    job_id, approved_by=LAB_APPROVAL, approved_at=datetime.now(UTC)
                )
                self._background(
                    RunRequest(
                        entry.path,
                        RunMode.APPLY,
                        prompts=prompts or {},
                        tags=tags,
                        limit=devices,
                        actor=Actor.MCP,
                        job_id=job_id,
                        allow_new_host_key=policy.trust_new_host_keys,
                        no_new_host_key=self._mcp_added(cust),
                        exact_exempt=self._exempt(entry, prompts),
                        customer=entry.play.customer,
                    )
                )
                return {
                    "job_id": job_id,
                    "status": "running",
                    "devices": devices,
                    "note": "lab customer: runs without approval; poll get_job",
                }
            self._background(
                RunRequest(
                    entry.path,
                    RunMode.CHECK,
                    prompts=prompts or {},
                    tags=tags,
                    limit=devices,
                    actor=Actor.MCP,
                    job_id=job_id,
                    allow_new_host_key=policy.trust_new_host_keys,
                    no_new_host_key=self._mcp_added(cust),
                    exact_exempt=self._exempt(entry, prompts),
                    customer=entry.play.customer,
                    approval_check=True,
                )
            )
            return {
                "job_id": job_id,
                "status": "preparing",
                "devices": devices,
                "note": "Tsunami renders and diffs each device, then the request waits for a "
                "person to approve it in the Tsunami GUI. Show the user the diff from get_job.",
            }

        args = {
            "path": path,
            "targets": targets,
            "prompts": self._audit_prompts(path, customer, prompts),
            "tags": tags,
            "customer": customer,
        }
        bound = self._customer_of(path, customer=customer)
        return self.app.call("request_run", 2, args, run, customer=bound)

    def request_allowlist_addition(self, family: str, command: str, reason: str):
        """Ask a person to allow a show command. Queues a request; never edits the policy."""

        def run(policy):
            try:
                fam, normalized = validate_allowlist_request(family, command)
            except PolicyError as exc:
                raise ToolRefused(str(exc)) from None
            why = _plain_text(reason, "reason", 300)
            if any(
                normalized == e.lower() or normalized.startswith(e.lower() + " ")
                for e in policy.show_allowlist.get(fam, [])
            ):
                return {"status": "already_allowed", "family": fam.value, "command": normalized}
            request_id = self.app.history.add_allowlist_request(fam.value, normalized, why)
            return {
                "status": "pending_approval",
                "request_id": request_id,
                "family": fam.value,
                "command": normalized,
                "note": "A person must approve this in Tsunami Settings. Until then the command "
                "is refused; try show again after they have.",
            }

        args = {"family": family, "command": command, "reason": reason}
        return self.app.call("request_allowlist_addition", 0, args, run)

    def get_policy(self):
        """The policy as it applies to Claude now: what is allowed and the limits, and the
        show-allowlist requests with their status. Never the blocked customers (they are
        hidden from MCP entirely) and never who decided a request."""

        def run(policy):
            requests = [
                {
                    "request_id": r["id"],
                    "family": r["family"],
                    "command": r["command"],
                    "status": r["status"],
                    "requested_at": r["requested_at"],
                    "decided_at": r["decided_at"],
                }
                for r in self.app.history.allowlist_requests(limit=50)
            ]
            return {
                "max_tier": policy.max_tier,
                "tiers": {str(t): text for t, text in TIERS.items()},
                "apply": policy.apply,
                "lab_allow_drafts": policy.lab_allow_drafts,
                "trust_new_host_keys": policy.trust_new_host_keys,
                "inventory_edits": policy.inventory_edits,
                "show_allowlist": {str(f): list(v) for f, v in policy.show_allowlist.items()},
                "pipes": {str(f): list(v) for f, v in policy.pipes.items()},
                "max_check_targets": policy.max_check_targets,
                "rate_limit_per_minute": policy.rate_limit_per_minute,
                "approval_expiry_minutes": policy.approval_expiry_minutes,
                "allowlist_requests": requests,
            }

        return self.app.call("get_policy", 0, {}, run)

    def cancel_job(self, job_id: str):
        """Cancel a job Claude started: one not yet started (a change request before or
        while it waits for approval), or one running in this process without a person's
        approval (a lab run, a check, compliance, show_many). A run a person approved is
        stopped in the GUI."""

        def run(policy):
            current = self._visible_job(policy, job_id)
            if current.actor != Actor.MCP:
                raise ToolRefused("only jobs requested over MCP can be cancelled here")
            event = self._cancels.get(job_id)
            if current.status in (JobStatus.PENDING, JobStatus.AWAITING_APPROVAL):
                if event is not None:
                    event.set()
                self.app.history.set_job_status(job_id, JobStatus.CANCELLED)
                self.app.history.cancel_ready_devices(job_id)
                return {"job_id": job_id, "status": "cancelled"}
            if current.status == JobStatus.RUNNING and event is not None:
                event.set()
                return {
                    "job_id": job_id,
                    "status": "cancelling",
                    "note": "No new device or step starts. A device in the middle of a change "
                    "is rolled back as on any failure. Poll get_job for the outcome.",
                }
            if current.status == JobStatus.RUNNING:
                raise ToolRefused(
                    "a person approved and started this run; it can be stopped only in the "
                    "Tsunami GUI"
                )
            raise ToolRefused(f"job is {current.status}; there is nothing to cancel")

        args = {"job_id": job_id}
        # Tier 1: cancelling only stops work Claude started, so a read-only policy keeps it.
        return self.app.call("cancel_job", 1, args, run, known=self._job_supplied(job_id))

    # == helpers ===========================================================================

    def _read_only_job(self, tool, mode, path, targets, prompts, customer=None):
        def run(policy):
            ws = self.app.workspace()
            entry = self._entry(ws, path, customer)
            play = entry.play
            cust = self.app.visible_customer(policy, ws, play.customer)
            if play.type == PlayType.INITIAL:
                raise ToolRefused("initial (day-0) plays have no inventory targets to check")
            if mode == RunMode.COMPLIANCE and play.type != PlayType.BASELINE:
                raise ToolRefused("compliance is for baseline plays")
            if entry.draft and not (cust.config.lab and policy.lab_allow_drafts):
                raise ToolRefused("drafts can only be checked against lab customers")
            if targets is not None and not targets:
                raise ToolRefused("targets must list at least one device, or be left out")
            devices = self._check_targets(ws, cust, play, targets)
            if len(devices) > policy.max_check_targets:
                raise ToolRefused(
                    f"at most {policy.max_check_targets} devices per {tool}; this one has "
                    f"{len(devices)}: pass targets to choose some, and run it again for the rest"
                )
            answers = self._without_passwords(entry, prompts)
            job_id = self._create_job(entry.path, play.customer, str(mode), answers, None, targets)
            self._background(
                RunRequest(
                    entry.path,
                    mode,
                    prompts=answers,
                    limit=targets,
                    actor=Actor.MCP,
                    job_id=job_id,
                    allow_new_host_key=policy.trust_new_host_keys,
                    no_new_host_key=self._mcp_added(cust),
                    exact_exempt=self._exempt(entry, prompts),
                    customer=entry.play.customer,
                )
            )
            return {"job_id": job_id, "status": "running", "devices": [d.name for d in devices]}

        args = {
            "path": path,
            "targets": targets,
            "prompts": self._audit_prompts(path, customer, prompts),
            "customer": customer,
        }
        return self.app.call(
            tool, 1, args, run, customer=self._customer_of(path, customer=customer)
        )

    def _entry(self, ws, path: str, customer: str | None = None):
        """The play, bound to customer if it is a global play."""
        real = resolve_read(ws.root, path, allowed=PLAY_DIRS)
        return play_entry(ws, _rel(ws.root, real), customer)

    def _check_targets(self, ws, cust, play, targets):
        result = resolve_targets(
            play.targets,
            cust.devices,
            sites=cust.sites.keys(),
            roles=ws.roles.keys(),
            limit=targets,
        )
        if not result.ok:
            raise ToolRefused("; ".join(f.message for f in result.findings))
        return result.devices

    def _create_job(self, path: Path, customer, mode, prompts, tags, targets) -> str:
        ws_root = Path(self.app.settings().workspace)
        return self.app.history.create_job(
            actor=Actor.MCP,
            play_path=path.as_posix(),
            play_hash=play_hash(ws_root / path),
            customer=customer,
            mode=mode,
            status=JobStatus.PENDING,
            prompts=dict(prompts or {}),
            tags=list(tags) if tags else None,
            limit=list(targets) if targets else None,
        )

    def _preflight(self, entry, mode: RunMode, prompts, tags, devices: list[str]) -> None:
        runner = Runner(
            self.app.workspace(),
            transport=None,  # preflight never connects
            history=self.app.history,
            secrets=self.app.secrets,
            paths=self.app.paths,
            app_settings=self.app.settings(),
        )
        request = RunRequest(
            entry.path,
            mode,
            prompts=prompts or {},
            tags=tags,
            limit=devices,
            customer=entry.play.customer,
            approval_check=mode == RunMode.CHECK,  # the run it asks for is an apply
        )
        try:
            runner.preflight(request)
        except TsunamiError as exc:
            raise ToolRefused(f"the request cannot run: {exc}") from None

    def _without_passwords(self, entry, prompts) -> dict:
        """Prompt answers for an MCP render or check: refused if Claude passed a password
        value, with a placeholder for each password prompt (never sent in those modes)."""
        answers = dict(prompts or {})
        passwords = _password_prompts(entry)
        supplied = sorted(passwords & set(answers))
        if supplied:
            raise ToolRefused(PASSWORD_REFUSAL.format(names=", ".join(supplied)))
        return {**answers, **dict.fromkeys(passwords, PASSWORD_PLACEHOLDER)}

    def _audit_prompts(self, path, customer, prompts):
        """Prompt answers as an audit row may hold them: a password prompt's value masked.
        The call is refused, but its arguments are recorded first."""
        if not prompts or not isinstance(prompts, dict):
            return prompts
        try:
            entry = self._entry(self.app.workspace(), path, customer)
        except Exception:  # noqa: BLE001 - the call itself reports the problem
            return prompts
        return masked_prompts(entry.play, prompts)

    def _background(self, request: RunRequest, after=None) -> None:
        cancel = threading.Event()
        self._cancels[request.job_id] = cancel

        def work() -> None:
            try:
                runner = Runner(
                    self.app.workspace(),
                    transport=self.app.transport(),
                    history=self.app.history,
                    secrets=self.app.secrets,
                    paths=self.app.paths,
                    app_settings=self.app.settings(),
                    confirm_canary=lambda device: True,
                    cancel=cancel,
                )
                runner.run(request)
                if after is not None and not cancel.is_set():
                    after()
            finally:
                self._cancels.pop(request.job_id, None)

        thread = threading.Thread(target=work, name=f"mcp-{request.job_id[:8]}", daemon=True)
        self.app.threads.append(thread)
        thread.start()

    def _mcp_added(self, cust) -> frozenset[str]:
        """Devices Claude added to this customer whose host is still the one it gave. MCP
        connections never record a first-contact host key for them."""
        added = self.app.history.mcp_devices(cust.slug)
        return frozenset(d.name for d in cust.devices if added.get(d.name) == str(d.host))

    def _jump(self, cust, device=None) -> JumpTarget | None:
        jh = cust.jump_for(device)
        if jh is None:
            return None
        return JumpTarget(jh.host, jh.port, self.app.secrets.get_login(jh.credential))

    def _visible_job(self, policy, job_id: str):
        """The job, or ToolRefused if unknown or its customer is blocked (indistinguishable)."""
        try:
            job = self.app.history.get_job(job_id)
        except TsunamiError:
            job = None
        if job is None or job.customer in policy.customers_blocked:
            raise ToolRefused(f"unknown job {job_id!r}")
        return job

    def _job_supplied(self, job_id: str) -> list[str]:
        """What Claude supplied for an MCP job: its prompt values, and the draft it ran."""
        try:
            job = self.app.history.get_job(job_id)
        except TsunamiError:
            return []
        if job.actor != Actor.MCP:
            return []  # a person typed these; Claude never saw them
        return [str(v) for v in job.prompts.values()] + self._drafts_for(job.play_path)

    def _exempt(self, entry, prompts) -> frozenset[str]:
        """Text Claude supplied for a run, kept out of the runner's exact-value layer."""
        known = list((prompts or {}).values()) + (self._drafts() if entry.draft else [])
        return frozenset(supplied_values(known))

    def _drafts_for(self, path: str | None) -> list[str]:
        if path and PureWindowsPath(path).as_posix().startswith("drafts/"):
            return self._drafts()
        return []

    def _drafts(self) -> list[str]:
        """Path, name and text of each draft Claude wrote over MCP and nobody changed since.

        When Claude reads its own text back, an exact-value redaction would answer whether
        the text it wrote is a secret. A draft a person saved or edited is not Claude's
        input, so it is redacted like any other file.
        """
        try:
            root = Path(self.app.settings().workspace or "")
            written = self.app.history.mcp_draft_hashes()
        except Exception:  # noqa: BLE001
            return []
        out: list[str] = []
        for rel, digest in written.items():
            file = root / rel
            try:
                data = file.read_bytes()
            except OSError:
                continue
            if hashlib.sha256(data).hexdigest() != digest:
                continue
            out += [rel, file.stem, data.decode("utf-8-sig", errors="replace")]
        return out

    def _check_play_customer(self, policy, ws, text: str, customer: str | None = None) -> None:
        """Refuse a blocked customer: the play's, or for a global play the one it runs for."""
        named = _yaml_customer(text)
        target = customer if named == GLOBAL_CUSTOMER else named
        if target is not None:
            self.app.visible_customer(policy, ws, target)

    def _customer_of(
        self, path: str | None, content: str | None = None, customer: str | None = None
    ) -> str | None:
        """The customer a call concerns (a global play's is the one it runs for); None if
        it cannot be read."""
        try:
            if content is not None:
                named = _yaml_customer(content)
            elif path is None:
                return None
            else:
                ws_root = Path(self.app.settings().workspace or "")
                named = _yaml_customer(_read_text(resolve_read(ws_root, path, allowed=PLAY_DIRS)))
        except Exception:  # noqa: BLE001
            return None
        return customer if named == GLOBAL_CUSTOMER else named


class LocalBackend:
    """Runs tool calls in this process: the GUI behind the bridge, and tests."""

    def __init__(self, tools: Tools) -> None:
        self.tools = tools

    def call(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        return self.tools.dispatch(tool, args)


class ToolProxy:
    """Tools' method signatures over a backend (LocalBackend or bridge.BridgeClient)."""

    def __init__(self, backend: Any) -> None:
        self._backend = backend

    def __getattr__(self, name: str) -> Any:
        if name not in TOOL_NAMES:
            raise AttributeError(name)
        params = list(inspect.signature(getattr(Tools, name)).parameters)[1:]

        def call(*args: Any, **kwargs: Any) -> dict[str, Any]:
            return self._backend.call(name, {**dict(zip(params, args, strict=False)), **kwargs})

        return call


def _target_name(target: str | None, device: str | None, *, required: bool) -> str | None:
    """The device a show, resolved_vars or render call names. target is the parameter and
    device its old name, still accepted: a client reaching Tsunami through a relay that
    claims a top-level `device` argument for itself never receives it."""
    if target is not None and device is not None and target != device:
        raise ToolRefused(
            f"target {target!r} and device {device!r} name different devices; pass only target"
        )
    name = target if target is not None else device
    if required and not name:
        raise ToolRefused("pass target: the device name")
    return name


def _jump_view(cust: Any, device: Any) -> dict[str, Any] | None:
    """A device's effective jump host for list_devices: host, port and the level that sets
    it. Never its login. None means a direct connection."""
    jh, source = cust.jump_with_source(device)
    if jh is None:
        return None
    return {"host": str(jh.host), "port": jh.port, "set_at": source}


def _refuse_secret_vars(values: Any) -> None:
    """Vars never hold secrets: refuse a secret-looking key with a literal string value, the
    rule the workspace linter warns on. poe_enabled: true or ntp_key_id: 5 is fine."""
    secretish = secret_literal_keys(values)
    if secretish:
        raise ToolRefused(
            "vars never hold secrets; leave out " + ", ".join(repr(k) for k in secretish)
        )


def _strings(value: Any) -> list[str]:
    """Every string in a value, mapping keys included."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for k, v in value.items() for s in (*_strings(k), *_strings(v))]
    if isinstance(value, list | tuple):
        return [s for v in value for s in _strings(v)]
    return []


def _show_parts(policy: Any, dev: Any, command: str) -> tuple[str, Any]:
    """The normalized command and its parts for this device's family, or ToolRefused
    (a bad pattern is refused here, before anything connects)."""
    try:
        normalized = check_show_command(policy, PLATFORM_FAMILY[dev.platform], command)
        parts = split_command(normalized)
        if parts.pattern is not None:
            compile_pattern(parts.pattern)
    except (PolicyError, FilterError) as exc:
        raise ToolRefused(f"{dev.name}: {exc}") from None
    return normalized, parts


def _state_command(family: str, name: str) -> str | None:
    try:
        return command_for(family, name)
    except StateError:
        return None


def _read_state(
    transport: Any, target: ConnectTarget, family: Any, spec: StateTable, base: str, full: Any
) -> list[dict[str, Any]]:
    """The table's rows, redacted. The output is redacted whole before it is parsed (a
    secret that spans cells, or a line the pattern layer masks), and every text cell once
    more after, so no cell holds a stored secret whichever way the parser split it."""
    session = transport.open(target)
    try:
        output = session.send_exec(base).output
    finally:
        session.close()
    rows = parse_table(family, spec.name, full.text(output))
    return [{k: full.text(v) if isinstance(v, str) else v for k, v in row.items()} for row in rows]


def _state_where(spec: StateTable, where: Any) -> dict[str, Any]:
    """A `where` Claude supplied, checked before anything connects: known columns, and
    matchers of the shapes a play's check takes, with regular expressions held to the
    show pipe's bounds so matching time stays small."""
    if where is None:
        return {}
    if not isinstance(where, dict):
        raise ToolRefused("where is a mapping of column to matcher, e.g. {state: full}")
    if len(where) > MAX_WHERE_COLUMNS:
        raise ToolRefused(f"where may name at most {MAX_WHERE_COLUMNS} columns")
    checked: dict[str, Any] = {}
    for column, matcher in where.items():
        if not isinstance(column, str) or spec.column(column) is None:
            raise ToolRefused(
                f"where: {spec.name} has no column {str(column)!r}; "
                f"its columns are {', '.join(spec.column_names)}"
            )
        if isinstance(matcher, list):
            if not matcher or len(matcher) > MAX_MATCHER_ITEMS:
                raise ToolRefused(f"where {column}: a list holds 1 to {MAX_MATCHER_ITEMS} matchers")
            checked[column] = [_state_matcher(m, column) for m in matcher]
        else:
            checked[column] = _state_matcher(matcher, column)
    return checked


def _state_matcher(matcher: Any, column: str) -> Any:
    shapes = f"where {column}: a matcher is text, a number, a list or {{min, max}}"
    if isinstance(matcher, bool) or matcher is None:
        raise ToolRefused(shapes)
    if isinstance(matcher, int):
        return matcher
    if isinstance(matcher, str):
        if len(matcher) > MAX_MATCHER_TEXT:
            raise ToolRefused(f"where {column}: a matcher is at most {MAX_MATCHER_TEXT} characters")
        if is_regex(matcher):
            try:
                compile_pattern(matcher[1:-1])
            except FilterError as exc:
                raise ToolRefused(f"where {column}: {exc}") from None
        return matcher
    if isinstance(matcher, dict):
        whole = all(isinstance(v, int) and not isinstance(v, bool) for v in matcher.values())
        if not matcher or not set(matcher) <= {"min", "max"} or not whole:
            raise ToolRefused(f"where {column}: a range is {{min: n, max: n}} with whole numbers")
        return matcher
    raise ToolRefused(shapes)


def _run_show(transport: Any, target: ConnectTarget, parts: Any, full: Any) -> str:
    """Run the base command, then the pipe here, on fully redacted text. On the device the
    pipe would see the secrets, and which lines came back would reveal them
    (showfilter.py)."""
    session = transport.open(target)
    try:
        output = session.send_exec(parts.base).output
    finally:
        session.close()
    return apply_filter(full.text(output), parts)


def _close(transport: Any) -> None:
    close = getattr(transport, "close", None)
    if callable(close):
        close()


def job_state(job: Any, rows: list[Any]) -> str:
    """A short token that changes when the job's or a device's status does (get_job since)."""
    data = [str(job.status), str(job.approved_at)] + [
        f"{d.device}={d.status}/{d.rollback_status}" for d in rows
    ]
    return hashlib.sha256(json.dumps(data).encode("utf-8")).hexdigest()[:16]


def _device_view(d: Any, detail: str) -> dict[str, Any]:
    view: dict[str, Any] = {
        "device": d.device,
        "status": str(d.status),
        "failed_line": d.failed_line,
        "error": d.error_redacted,
        "rollback_status": d.rollback_status,
    }
    if detail == "full":
        view["render"] = d.render_masked
        view["untrusted_device_output"] = {"output": d.output_redacted, "diff": d.diff_redacted}
    elif detail == "diff":
        view["untrusted_device_output"] = {"diff": d.diff_redacted}
    return view


def _model_view(entry: CatalogEntry) -> dict[str, Any]:
    return {
        "id": entry.id,
        "label": entry.label,
        "vendor": entry.vendor,
        "platform": str(entry.platform),
        "custom": entry.custom,
        "console_baud": entry.console_baud,
        "model_pattern": entry.model_pattern,
        "examples": list(entry.examples),
        "lifecycle": entry.lifecycle,
        "eos_year": entry.eos_year,
    }


def _plain_text(value: Any, what: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ToolRefused(f"{what} must be text of 1 to {limit} characters")
    if has_control_chars(value):
        raise ToolRefused(f"{what} must not contain line breaks or control characters")
    return value


def job_phase(job: Any) -> tuple[str, str | None]:
    """Which part of a job this is, and a note for an MCP change request not yet applied.

    A change request's job has mode apply from the start, but until a person approves it
    (or the lab policy runs it) it is only a check: nothing has been sent to any device.
    """
    if job.mode != str(RunMode.APPLY):
        return job.mode, None
    if job.actor == Actor.MCP and job.approved_at is None:
        if job.status in (JobStatus.DENIED, JobStatus.EXPIRED):
            return "approval check", f"Nothing was applied: the request was {job.status}."
        return "approval check", (
            "Nothing has been applied. This is the check behind a change request: devices "
            "marked ready would change, as their diff shows, once a person approves the "
            "request in the Tsunami GUI."
        )
    return "apply", None


def _password_prompts(entry: Any) -> set[str]:
    return {n for n, p in entry.play.prompts.items() if p.type == PromptType.PASSWORD}


def secret_names(refs: Any) -> list[str]:
    """The secrets a render uses: customer/name (customer/name.field for one field of a
    secret with fields), login:<name> once per login, password:<prompt> per password prompt."""
    names: list[str] = []
    for ref in refs:
        if ref.startswith(LOGIN_PREFIX):
            name = ref.split("/", 1)[0]
        elif ref.startswith(PROMPT_PREFIX):
            name = "password:" + ref[len(PROMPT_PREFIX) :]
        else:
            name = ref
        if name not in names:
            names.append(name)
    return names


def _yaml_text(values: dict[str, Any]) -> str:
    if not values:
        return ""
    out = io.StringIO()
    yaml = YAML(typ="safe")
    yaml.default_flow_style = False
    yaml.dump(values, out)
    return out.getvalue()


def _new_roles(devices: list[Any], ws: Any) -> list[str]:
    """Custom roles the rows name that do not exist yet: valid slugs, never a built-in."""
    named = {str(d.get("role", d.get("type")) or "") for d in devices if isinstance(d, dict)}
    return sorted(r for r in named - set(ws.roles) - set(DEVICE_TYPES) if SLUG_RE.fullmatch(r))


def _remove_roles(root: Path, roles: list[str]) -> None:
    """Undo roles this call created (they are new files, so nothing else uses them)."""
    for role in roles:
        (root / "inventory" / "roles" / f"{role}.yml").unlink(missing_ok=True)


def _device_plan(root: Path, cust: Any, devices: list[Any], create_sites: bool) -> ImportPlan:
    """Check the rows as a CSV import does, plus one rule for MCP: a device may only name a
    login its customer already uses, never another customer's."""
    rows: list[tuple[int, list[str]]] = []
    for number, device in enumerate(devices, start=1):
        if not isinstance(device, dict):
            raise ToolRefused(f"device {number} must be a mapping of fields")
        unknown = sorted(set(map(str, device)) - _DEVICE_KEYS)
        if unknown:
            raise ToolRefused(f"device {number}: unknown field(s) {', '.join(unknown)}")
        values = {**device, "role": device.get("role", device.get("type"))}
        cells = ["" if values.get(f) is None else str(values[f]) for f in IMPORT_FIELDS]
        if any(has_control_chars(cell) for cell in cells):
            raise ToolRefused(f"device {number}: fields must not contain control characters")
        rows.append((number, cells))
    table = CsvTable(Path("mcp"), ",", list(IMPORT_FIELDS), rows)
    mapping = ColumnMapping(columns={f: f for f in IMPORT_FIELDS})
    # known_logins=None: MCP never probes Credential Manager, not even for names.
    plan = preview(root, cust.slug, table, mapping, known_logins=None, create_sites=create_sites)
    # The logins this customer logs in with (its jump host's too); never push_logins,
    # which Tsunami does not log in with.
    allowed = customer_logins(cust) | jump_logins(cust)
    checked: list[RowResult] = []
    for row in plan.rows:
        login = row.fields.get("credential")
        if row.importable and login and login not in allowed:
            row = RowResult(
                row.number,
                row.fields,
                IMPORT_ERROR,
                f"login {login!r} is not used by {cust.slug}; leave credential out to use "
                "the site's or the customer's login",
            )
        checked.append(row)
    needed = {r.fields["site"] for r in checked if r.importable}
    return ImportPlan(cust.slug, checked, [s for s in plan.new_sites if s in needed])


def _yaml_customer(text: str) -> str | None:
    try:
        data = load_yaml_text(text, Path("x.yml")).data
    except YamlLoadError:
        return None
    customer = data.get("customer") if isinstance(data, dict) else None
    return customer if isinstance(customer, str) else None


def _platform(value: str) -> Platform:
    try:
        return Platform(value)
    except ValueError:
        raise ToolRefused(f"unknown platform {value!r}") from None


def _level(path: Path) -> str:
    parts = path.parts
    return {"customers": "customer", "platform": "platform", "common": "common"}.get(
        parts[1] if parts[0] == "snippets" else parts[2], "unknown"
    )


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        raise ToolRefused("no such file") from None


def _rel(root: Path, real: Path) -> str:
    return real.relative_to(Path(root).resolve()).as_posix()


def _short(text: str | None) -> str | None:
    """Audit rows record a prefix of long text arguments, not all of it."""
    if text is None:
        return None
    return text if len(text) <= 200 else text[:200] + f"... ({len(text)} chars)"


def _atomic_write(target: Path, content: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
        os.replace(tmp, target)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _issue_dict(issue: Issue) -> dict[str, Any]:
    return {
        "severity": str(issue.severity),
        "code": issue.code,
        "message": issue.message,
        "file": issue.file.as_posix() if issue.file else None,
        "line": issue.line,
    }


def _issues_result(issues: list[Issue]) -> dict[str, Any]:
    return {
        "valid": not any(i.severity == Severity.ERROR for i in issues),
        "issues": [_issue_dict(i) for i in issues],
    }


def _template_check(content: str, rel: Path) -> dict[str, Any]:
    try:
        compile_template(make_environment(), content, name=rel.as_posix())
    except TemplateError as exc:
        issue = Issue(Severity.ERROR, "template.syntax", str(exc), rel, exc.line)
        return _issues_result([issue])
    return _issues_result([])


__all__ = ["PlatformFamily", "Tools"]
