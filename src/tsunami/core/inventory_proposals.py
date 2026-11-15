"""Inventory changes Claude proposes over MCP, applied only when a person approves them.

A proposal edits existing devices (any field: name, host, port, site, role, platform,
model, login, jump host, canary priority, variables) or removes them. It is validated
when proposed (by running it on a scratch copy of the customer's inventory, so a proposal
that could never apply is refused up front) and again when approved, against the
inventory as it is then: a device that changed in between fails the whole proposal and
nothing is written. Denied, expired and failed proposals change nothing. Approval is
atomic: all of a proposal's changes land in one write, or none do.

Each change is stored as {"device", "remove", "before", "set"}: before holds the device's
fields as proposed against, set the new values, so the approval card can show a
before/after per field and the stale check can compare.

See design.md, MCP interface: Inventory proposals.
"""

from __future__ import annotations

import json
import shutil
import tempfile
from collections.abc import Collection, Mapping
from pathlib import Path
from typing import Any

from tsunami.core.catalog import CUSTOM_MODELS
from tsunami.core.inventory_edit import (
    CUSTOMERS,
    DIRECT,
    ROLES,
    InventoryEditError,
    InventoryEditor,
)
from tsunami.core.issues import TsunamiError
from tsunami.core.models.common import require_slug
from tsunami.core.models.inventory import DEFAULT_CANARY_PRIORITY
from tsunami.core.models.policy import has_control_chars
from tsunami.core.models.proposals import InventoryProposal, ProposalStatus
from tsunami.core.workspace import secret_literal_keys

SOURCE_MCP = "mcp"
# Every device field, in devices.yml order. A login (the device's or its jump host's) may
# only be one the customer already logs in with, so a proposal never hands a device
# another customer's login, and a host or jump host it sets is never first-contacted
# over MCP (approve_proposal records it).
FIELDS = (
    "name",
    "host",
    "port",
    "site",
    "role",
    "platform",
    "model",
    "credential",
    "jump_host",
    "canary_priority",
    "vars",
)
TEXT_FIELDS = frozenset({"name", "host", "site", "role", "platform", "model", "credential"})
OPTIONAL = frozenset({"model", "credential", "jump_host", "vars"})  # null clears it
MAX_CHANGES = 50
MAX_PENDING = 20
MAX_VALUE = 100
MAX_VARS_BYTES = 8 * 1024


def snapshot(device: Mapping[str, Any]) -> dict[str, Any]:
    """A device's fields as a proposal compares and shows them. Defaults are filled in;
    jump_host is None when inherited, "none" for a direct connection, else a mapping."""
    out: dict[str, Any] = {}
    for f in TEXT_FIELDS:
        out[f] = None if device.get(f) is None else str(device[f])
    out["port"] = _int(device.get("port"), 22)
    out["canary_priority"] = _int(device.get("canary_priority"), DEFAULT_CANARY_PRIORITY)
    out["vars"] = dict(device.get("vars") or {})
    out["jump_host"] = _jump_value(device["jump_host"]) if "jump_host" in device else None
    return {f: out[f] for f in FIELDS}


def build_changes(
    current: Mapping[str, Mapping[str, Any]],
    raw: Any,
    *,
    logins: Collection[str] | None = None,
) -> list[dict[str, Any]]:
    """Check what Claude sent against the customer's devices and return the stored form.

    current: the customer's devices by name (InventoryEditor.read_devices). logins: the
    logins a device or jump host may be given (the customer's own); None refuses any
    login change. Raises InventoryEditError with a message safe to hand back to Claude.
    """
    if not isinstance(raw, list) or not raw:
        raise InventoryEditError("changes must be a non-empty list")
    if len(raw) > MAX_CHANGES:
        raise InventoryEditError(f"at most {MAX_CHANGES} devices per proposal")
    allowed = frozenset(logins or ())
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for number, item in enumerate(raw, start=1):
        where = f"change {number}"
        if not isinstance(item, Mapping):
            raise InventoryEditError(f"{where} must be a mapping")
        unknown = sorted(set(map(str, item)) - {"device", "remove", *FIELDS})
        if unknown:
            raise InventoryEditError(f"{where}: unknown field(s) {', '.join(unknown)}")
        name = item.get("device")
        if not isinstance(name, str) or name not in current:
            raise InventoryEditError(f"{where}: unknown device {_shown(name)}")
        if name in seen:
            raise InventoryEditError(f"{where}: device {name!r} is already in this proposal")
        seen.add(name)
        before = snapshot(current[name])
        wanted = {f: item[f] for f in FIELDS if f in item}
        if "remove" in item:
            if item["remove"] is not True or wanted:
                raise InventoryEditError(
                    f"{where}: remove must be true and cannot be combined with other fields"
                )
            out.append({"device": name, "remove": True, "before": before, "set": {}})
            continue
        if not wanted:
            raise InventoryEditError(f"{where}: nothing to change for {name!r}")
        changed: dict[str, Any] = {}
        for field, value in wanted.items():
            value = _checked(where, field, value, allowed)
            if value == before[field]:
                raise InventoryEditError(
                    f"{where}: {name!r} already has {field} {_shown(display(field, value))}"
                )
            changed[field] = value
        out.append({"device": name, "remove": False, "before": before, "set": changed})
    return out


def _checked(where: str, field: str, value: Any, logins: frozenset[str]) -> Any:
    """One proposed value in its stored form, or InventoryEditError."""
    if value is None and field in OPTIONAL:
        return {} if field == "vars" else None
    if field in TEXT_FIELDS:
        value = _text(where, field, value)
        if field == "credential" and value not in logins:
            raise InventoryEditError(_login_refusal(where, field, value))
        return value
    if field == "port":
        return _number(where, field, value, 1, 65535)
    if field == "canary_priority":
        return _number(where, field, value, 0, 254)
    if field == "jump_host":
        if isinstance(value, str) and value.strip().lower() in ("none", "direct"):
            return DIRECT
        if not isinstance(value, Mapping):
            raise InventoryEditError(
                f"{where}: jump_host is a mapping (host, port, credential), 'none' to connect "
                "directly, or null to inherit the site's or customer's"
            )
        extra = sorted(set(map(str, value)) - {"host", "port", "credential"})
        if extra:
            raise InventoryEditError(f"{where}: jump_host has unknown field(s) {', '.join(extra)}")
        host = _text(where, "jump_host.host", value.get("host"))
        login = _text(where, "jump_host.credential", value.get("credential"))
        if login not in logins:
            raise InventoryEditError(_login_refusal(where, "jump_host.credential", login))
        port = _number(where, "jump_host.port", value.get("port", 22), 1, 65535)
        return {"host": host, "port": port, "credential": login}
    # vars: the whole mapping, replacing the device's
    if not isinstance(value, Mapping):
        raise InventoryEditError(f"{where}: vars must be a mapping of variable names to values")
    try:
        size = len(json.dumps(value, ensure_ascii=False))
    except (TypeError, ValueError):
        raise InventoryEditError(f"{where}: vars must be plain values") from None
    if size > MAX_VARS_BYTES:
        raise InventoryEditError(f"{where}: vars are limited to {MAX_VARS_BYTES // 1024} KB")
    secretish = secret_literal_keys(value)
    if secretish:
        raise InventoryEditError(
            f"{where}: vars never hold secrets; leave out " + ", ".join(repr(k) for k in secretish)
        )
    return dict(value)


def _text(where: str, field: str, value: Any) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > MAX_VALUE
        or has_control_chars(value)
    ):
        raise InventoryEditError(
            f"{where}: {field} must be text of 1 to {MAX_VALUE} characters "
            "without control characters"
        )
    return value


def _number(where: str, field: str, value: Any, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise InventoryEditError(f"{where}: {field} must be a whole number from {low} to {high}")
    return value


def _login_refusal(where: str, field: str, login: str) -> str:
    # Worded alike for every name, so it never says whether a login exists elsewhere.
    return (
        f"{where}: {field} {_shown(login)} is not a login this customer uses; a person "
        "can set any login in the Tsunami GUI"
    )


def check_applies(root: Path, customer: str, changes: list[dict[str, Any]]) -> None:
    """Run the changes on a scratch copy of the customer's inventory, so everything the
    editor enforces (site exists, role known, model in the catalog and for the platform,
    names unique, host valid) is checked now. Nothing under root is touched."""
    slug = require_slug(customer)
    with tempfile.TemporaryDirectory(prefix="tsunami-proposal-") as scratch:
        copy = Path(scratch)
        shutil.copytree(Path(root) / CUSTOMERS / slug, copy / CUSTOMERS / slug)
        roles = Path(root) / ROLES
        if roles.is_dir():
            shutil.copytree(roles, copy / ROLES)
        models = Path(root) / CUSTOM_MODELS
        if models.is_file():
            shutil.copy2(models, copy / CUSTOM_MODELS)
        _apply(InventoryEditor(copy), slug, changes)


def stale_problems(
    current: Mapping[str, Mapping[str, Any]], changes: list[dict[str, Any]]
) -> list[str]:
    """Devices that are no longer as they were when the change was proposed. Only the
    fields the proposal recorded are compared (proposals from before 0.7.1 hold five)."""
    problems = []
    for change in changes:
        name = change["device"]
        if name not in current:
            problems.append(f"{name} no longer exists")
            continue
        now = snapshot(current[name])
        if any(now.get(f) != v for f, v in change["before"].items()):
            problems.append(f"{name} was changed after this was proposed")
    return problems


def approve_proposal(
    history: Any,
    root: Path,
    proposal_id: str,
    *,
    approved_by: str,
    expiry_minutes: int | None = None,
) -> InventoryProposal:
    """Apply a proposal a person approved. Raises TsunamiError, with nothing changed, if it
    is no longer awaiting approval, the inventory moved on, or the result is invalid."""
    if expiry_minutes is not None:
        history.expire_inventory_proposals(older_than_minutes=expiry_minutes)
    # Claims the proposal in one statement, so a second window cannot approve it as well.
    history.decide_inventory_proposal(proposal_id, ProposalStatus.APPROVED, decided_by=approved_by)
    proposal = history.get_inventory_proposal(proposal_id)
    try:
        editor = InventoryEditor(root)
        problems = stale_problems(editor.read_devices(proposal.customer), proposal.changes)
        if problems:
            raise InventoryEditError(
                "nothing was changed: " + "; ".join(problems) + ". Ask Claude to propose it again."
            )
        _apply(editor, proposal.customer, proposal.changes)
    except TsunamiError as exc:
        history.finish_inventory_proposal(proposal_id, ProposalStatus.FAILED, error=str(exc))
        raise
    except Exception:
        history.finish_inventory_proposal(
            proposal_id, ProposalStatus.FAILED, error="the inventory could not be written"
        )
        raise
    history.finish_inventory_proposal(proposal_id, ProposalStatus.APPLIED)
    _record_claude_hosts(history, proposal)
    return history.get_inventory_proposal(proposal_id)


def _record_claude_hosts(history: Any, proposal: InventoryProposal) -> None:
    """A host or jump host a proposal set came from Claude, like a device it added: MCP
    connections never make first contact through it, so a person trusts its key first. A
    rename carries the device's existing record over to its new name."""
    added = history.mcp_devices(proposal.customer)
    for change in proposal.changes:
        if change["remove"]:
            continue
        new = change["set"]
        old_name = change["device"]
        name = new.get("name", old_name)
        host = new.get("host", change["before"].get("host"))
        jump = new.get("jump_host")
        if "host" in new or isinstance(jump, Mapping):
            history.record_mcp_device(proposal.customer, name, host)
        elif name != old_name and old_name in added:
            history.record_mcp_device(proposal.customer, name, added[old_name])


def deny_proposal(history: Any, proposal_id: str, *, denied_by: str) -> None:
    history.decide_inventory_proposal(proposal_id, ProposalStatus.DENIED, decided_by=denied_by)


def describe(changes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Each device's change as the approval card and get_inventory_proposal show it."""
    out = []
    for change in changes:  # fields in FIELDS order: history.db stores the JSON key-sorted
        before = change["before"]
        if change["remove"]:
            fields = [
                {"field": f, "before": before[f], "after": None} for f in FIELDS if f in before
            ]
        else:
            fields = [
                {"field": f, "before": before.get(f), "after": change["set"][f]}
                for f in FIELDS
                if f in change["set"]
            ]
        action = "remove" if change["remove"] else "update"
        out.append({"device": change["device"], "action": action, "fields": fields})
    return out


def display(field: str, value: Any) -> str:
    """A field's value as one line of text for the approval card."""
    if field == "jump_host":
        if value is None:
            return "(inherit)"
        if value == DIRECT:
            return "(direct)"
        if isinstance(value, Mapping):
            return f"{value.get('host')}:{value.get('port', 22)} as {value.get('credential')}"
    if field == "credential" and value is None:
        return "(site or customer default)"
    if field == "vars":
        if not value:
            return "(none)"
        return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
    if value is None:
        return "-"
    return str(value)


def _apply(editor: InventoryEditor, customer: str, changes: list[dict[str, Any]]) -> None:
    editor.apply_device_changes(
        customer,
        {c["device"]: _editor_fields(c["set"]) for c in changes if not c["remove"]},
        [c["device"] for c in changes if c["remove"]],
    )


def _editor_fields(fields: Mapping[str, Any]) -> dict[str, Any]:
    """Stored values as InventoryEditor takes them: empty vars are removed."""
    out = dict(fields)
    if "vars" in out and not out["vars"]:
        out["vars"] = None
    return out


def _jump_value(value: Any) -> Any:
    if value is None or (isinstance(value, str) and value.strip().lower() == "none"):
        return DIRECT
    if isinstance(value, Mapping):
        return {
            "host": str(value.get("host")),
            "port": _int(value.get("port"), 22),
            "credential": value.get("credential"),
        }
    return value


def _int(value: Any, default: int) -> Any:
    return default if value is None else value


def _shown(value: Any) -> str:
    return repr(value) if isinstance(value, str) and len(value) <= MAX_VALUE else "(not text)"
