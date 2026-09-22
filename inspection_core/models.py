"""Stable analysis-domain models shared by analyzers and rule engines."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class Finding:
    """One triggered inspection finding suitable for reporting."""

    rule_id: str
    severity: str
    title: str
    category: str
    summary: str
    facts: list[str]
    recommendation: str
    evidence_refs: list[str]
    requires_restart: bool | None = None
    status: str = "evaluated"
    confidence: float = 1.0
    finding_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "finding_id": self.finding_id,
            "rule_id": self.rule_id,
            "status": self.status,
            "evaluation_status": self.status,
            "triggered": True,
            "severity": self.severity,
            "category": self.category,
            "title": self.title,
            "summary": self.summary,
            "facts": self.facts,
            "recommendation": self.recommendation,
            "requires_restart": self.requires_restart,
            "confidence": self.confidence,
            "evidence_refs": self.evidence_refs,
        }


@dataclass
class RuleEvaluation:
    """The outcome of evaluating one rule, including non-triggered states."""

    rule_id: str
    category: str
    status: str
    reason: str
    severity_if_triggered: str | None = None
    finding_id: str | None = None
    evidence_refs: list[str] = field(default_factory=list)
    confidence: float = 1.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "category": self.category,
            "status": self.status,
            "reason": self.reason,
            "severity_if_triggered": self.severity_if_triggered,
            "finding_id": self.finding_id,
            "evidence_refs": self.evidence_refs,
            "confidence": self.confidence,
        }


@dataclass
class PackageContext:
    """Normalized inputs for analyzing one collected database package."""

    source: Path
    root: Path
    snapshot: dict[str, Any]
    status: dict[str, Any]
    manifest: dict[str, Any]
    integrity: dict[str, Any]
    tables: dict[str, list[dict[str, str]]] = field(default_factory=dict)
    variables: dict[str, str] = field(default_factory=dict)
    # F-29：单点快照，来源是 performance_schema.global_status —— 该表按设计
    #       不含 Com_xxx（手册 10.14；Bug #87645 官方判 by design，引 WL#6629）。
    #       所以这里永远取不到 Com_commit / Com_select 等；派生 TPS / 读写比
    #       一律用 timeseries["mysql_status"] 的首末行差分，不要消费本字段。
    global_status: dict[str, str] = field(default_factory=dict)
    timeseries: dict[str, list[dict[str, str]]] = field(default_factory=dict)
    history: dict[str, list[dict[str, str]]] = field(default_factory=dict)
    settings: dict[str, str] = field(default_factory=dict)

    @property
    def instance_id(self) -> str:
        identity = self.snapshot.get("instance_identity", {})
        return str(identity.get("server_uuid") or identity.get("instance_tag") or self.source.stem)
