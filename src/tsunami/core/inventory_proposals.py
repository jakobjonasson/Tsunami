"""Inventory changes Claude proposes over MCP, applied only when a person approves them.

A proposal edits existing devices (role, host, site, platform, model) or removes them.
It is validated when proposed (by running it on a scratch copy of the customer's
inventory, so a proposal that could never apply is refused up front) and again when
approved, against the inventory as it is then: a device that changed in between fails
the whole proposal and nothing is written. Denied, expired and failed proposals change
nothing. Approval is atomic: all of a proposal's changes land in one write, or none do.

Each change is stored as {"device", "remove", "before", "set"}: before holds the device's
fields as proposed against, set the new values, so the approval card can show a
before/after per field and the stale check can compare.

See design.md, MCP interface: Inventory proposals.
"""

from __future__ import annotations

import shutil
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from tsunami.core.inventory_edit import CUSTOMERS, ROLES, InventoryEditError, InventoryEditor
from tsunami.core.issues import TsunamiError
from tsunami.core.models.common import require_slug
from tsunami.core.models.policy import has_control_chars
from tsunami.core.models.proposals import InventoryProposal, ProposalStatus

SOURCE_MCP = "mcp"
# Fields a proposal may change. Never port, credential or vars: a login must not be
# redirected by a proposal, and variables can carry anything.
FIELDS = ("role", "host", "site", "platform", "model")
OPTIONAL = frozenset({"model"})  # null clears it
MAX_CHANGES = 50
MAX_PENDING = 20
MAX_VALUE = 100


def snapshot(device: Mapping[str, Any]) -> dict[str, str | None]:
    """A device's proposable fields as strings; None where it has none (model)."""
    return {f: (None if device.get(f) is None else str(device[f])) for f in FIELDS}


def build_changes(current: Mapping[str, Mapping[str, Any]], raw: Any) -> list[dict[str, Any]]:
    """Check what Claude sent against the customer's devices and return the stored form.

    current: the customer's devices by name (InventoryEditor.read_devices). Raises
    InventoryEditError with a message safe to hand back to Claude.
    """
    if not isinstance(raw, list) or not raw:
        raise InventoryEditError("changes must be a non-empty list")
    if len(raw) > MAX_CHANGES:
        raise InventoryEditError(f"at most {MAX_CHANGES} devices per proposal")
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
        changed: dict[str, str | None] = {}
        for field, value in wanted.items():
            if value is None and field in OPTIONAL:
                pass
            elif (
                not isinstance(value, str)
                or not value.strip()
                or len(value) > MAX_VALUE
                or has_control_chars(value)
            ):
                raise InventoryEditError(
                    f"{where}: {field} must be text of 1 to {MAX_VALUE} characters "
                    "without control characters"
                )
            if value == before[field]:
                raise InventoryEditError(f"{where}: {name!r} already has {field} {_shown(value)}")
            changed[field] = value
        out.append({"device": name, "remove": False, "before": before, "set": changed})
    return out


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
        _apply(InventoryEditor(copy), slug, changes)


def stale_problems(
    current: Mapping[str, Mapping[str, Any]], changes: list[dict[str, Any]]
) -> list[str]:
    """Devices that are no longer as they were when the change was proposed."""
    problems = []
    for change in changes:
        name = change["device"]
        if name not in current:
            problems.append(f"{name} no longer exists")
        elif snapshot(current[name]) != change["before"]:
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
    for change in proposal.changes:
        # A host a proposal set came from Claude, like one it added: MCP connections never
        # make first contact with it, so a person still trusts its key first.
        if "host" in change["set"]:
            history.record_mcp_device(proposal.customer, change["device"], change["set"]["host"])
    return history.get_inventory_proposal(proposal_id)


def deny_proposal(history: Any, proposal_id: str, *, denied_by: str) -> None:
    history.decide_inventory_proposal(proposal_id, ProposalStatus.DENIED, decided_by=denied_by)


def describe(changes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Each device's change as the approval card and get_inventory_proposal show it."""
    out = []
    for change in changes:  # fields in FIELDS order: history.db stores the JSON key-sorted
        if change["remove"]:
            fields = [{"field": f, "before": change["before"][f], "after": None} for f in FIELDS]
        else:
            fields = [
                {"field": f, "before": change["before"][f], "after": change["set"][f]}
                for f in FIELDS
                if f in change["set"]
            ]
        action = "remove" if change["remove"] else "update"
        out.append({"device": change["device"], "action": action, "fields": fields})
    return out


def _apply(editor: InventoryEditor, customer: str, changes: list[dict[str, Any]]) -> None:
    editor.apply_device_changes(
        customer,
        {c["device"]: c["set"] for c in changes if not c["remove"]},
        [c["device"] for c in changes if c["remove"]],
    )


def _shown(value: Any) -> str:
    return repr(value) if isinstance(value, str) and len(value) <= MAX_VALUE else "(not text)"
