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

    def to_plan_dict(self) -> dict[str, Any]:
        """分级整改计划条目的出参：legacy 三键 + **有值才带**的整改三要素。

        为什么不直接把三要素塞进 ``to_legacy_dict``：它的契约被冻结为三键
        （``test_management_models_preserve_legacy_and_extended_fields`` 明确断言
        「传了 owner 也仍只输出三键」），是给只认旧格式的下游用的出口。
        整改计划要喂渲染层的六个列头，所以另开出口，而不是改 legacy 的默认行为。

        键名是 ``planned_window`` 而非数据类的 ``target_window``：渲染层
        （``build_remediation_plan_professional``）读的就是前者。

        空值不注入：旧基线 ``report_model.json`` 的 plan 条目只有三键，
        条件注入才能让单实例冻结契约（逐字典等值比对）保持不变；
        而缺项时补默认值等于替客户认定责任方，渲染层的「待指定 / 未登记」
        才是正确呈现。
        """

        payload = self.to_legacy_dict()
        if self.owner:
            payload["owner"] = self.owner
        if self.target_window:
            payload["planned_window"] = self.target_window
        if self.verification:
            payload["verification"] = self.verification
        return payload

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
