"""Inventory change proposals (design.md, MCP interface: Inventory proposals).

InventoryProposal is a row of history.db `inventory_proposals` (core/history.py).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any


class ProposalStatus(StrEnum):
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"  # claimed by a person; being applied
    APPLIED = "applied"
    DENIED = "denied"
    EXPIRED = "expired"
    FAILED = "failed"  # approved, but the inventory no longer matched: nothing was changed


@dataclass(frozen=True)
class InventoryProposal:
    """One proposal. Timestamps are timezone-aware UTC."""

    id: str
    created_at: datetime
    customer: str
    status: ProposalStatus
    source: str  # who asked, e.g. "mcp"
    reason: str  # Claude's own words: unverified, shown as such
    changes: list[dict[str, Any]] = field(default_factory=list)
    decided_by: str | None = None
    decided_at: datetime | None = None
    error: str | None = None
