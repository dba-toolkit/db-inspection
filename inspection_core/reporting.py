"""Database-neutral management models used while building reports.

The current MySQL report keeps its legacy JSON shape.  These models also carry
the owner/window/verification fields used by SQL Server reports and the
license/node-scope disclosures needed by Oracle and clustered databases.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class RemediationAction:
    finding_id: str
    title: str
    recommendation: str
    priority: str
    owner: str | None = None
    target_window: str | None = None
    verification: str | None = None
    status: str = "planned"

    def to_legacy_dict(self) -> dict[str, Any]:
        """Preserve the existing MySQL optimization-plan contract."""

        return {
            "finding_id": self.finding_id,
            "title": self.title,
            "recommendation": self.recommendation,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "finding_id": self.finding_id,
            "title": self.title,
            "recommendation": self.recommendation,
            "priority": self.priority,
            "owner": self.owner,
            "target_window": self.target_window,
            "verification": self.verification,
            "status": self.status,
        }


@dataclass(frozen=True)
class EvidenceDisclosure:
    check_id: str
    status: str
    reason: str
    recommended_action: str
    collector_change_required: bool = False
    evidence_refs: list[str] = field(default_factory=list)
    license_boundary: str | None = None
    node_scope: str | None = None

    def to_legacy_gap(self) -> dict[str, Any]:
        """Preserve the existing MySQL collection-gaps contract."""

        return {
            "item_id": self.check_id,
            "status": self.status,
            "reason": self.reason,
            "recommended_action": self.recommended_action,
            "collector_change_required": self.collector_change_required,
        }

    def to_dict(self) -> dict[str, Any]:
        value = self.to_legacy_gap()
        value.update({
            "evidence_refs": list(self.evidence_refs),
            "license_boundary": self.license_boundary,
            "node_scope": self.node_scope,
        })
        return value
