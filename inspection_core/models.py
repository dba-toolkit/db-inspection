"""Stable analysis-domain models shared by analyzers and rule engines."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


#: 非表格式证据文件（`.txt` / key-value `.tsv`）在包内可能出现的目录，按此顺序查找。
#: ★ 批 6 / N-8 曾把 4 个「列对齐文本」（`free` / `df` / `df -i` / `lsblk`）从 `tables/`
#:   挪到 `evidence/`；呈现层当时把目录写死，移动后**静默少一节内容**、没有任何测试会红。
#:   所以目录名只在本模块出现一次，调用方一律按**契约文件名**取（`PackageContext.raw_lines`）。
_RAW_FILE_DIRS: tuple[str, ...] = ("tables", "evidence")


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
    # 整改三要素，由规则包逐条声明（owner 写**角色**不写人名，跨客户才不会错）。
    # 三者均为可选：基线 analysis.json 的 findings 里没有这三个键，to_dict() 走
    # 条目级条件注入 —— 无条件写入会打穿 test_report_builder_matches_frozen_report_contract
    # （它逐字典等值比对，且基线包已不在仓库、不可刷新）。
    owner: str | None = None
    apply_mode: str | None = None
    verification: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
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
        if self.owner:
            payload["owner"] = self.owner
        if self.apply_mode:
            payload["apply_mode"] = self.apply_mode
        if self.verification:
            payload["verification"] = self.verification
        return payload


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

    def raw_lines(self, name: str) -> list[str]:
        """按**契约文件名**在包内查找并返回其**原始行**；找不到返回 `[]`。

        只用于 `.txt` / key-value `.tsv` 这类**非表格式**证据文件（`lscpu.txt`、
        `mounts.txt`、`dmesg_errors.txt`、`os_release.txt`、`time_status.txt`、
        `kernel_parameters.tsv` …）。**表格式数据**（`tables/*.psv`）请走 `tables` 字典，
        两者不要混用。

        ★ 为什么不让调用方拼 `root / "evidence" / name`：这些文件的**所在目录**在历次采集器
        改造中移动过（N-8 把 4 个对齐文本从 `tables/` 挪到 `evidence/`）。路径写死的后果是
        **静默**的 —— 文件读不到就返回空，报告少一节内容，规则层不报、测试不红。
        所以查找目录集中在 `_RAW_FILE_DIRS`，调用方只认文件名。

        顺序即 `_RAW_FILE_DIRS`，命中第一个存在的文件即返回；同名文件不会在两个目录并存
        （`tables/` 只放表格式数据，`evidence/` 只放原文证据）。
        """
        for sub in _RAW_FILE_DIRS:
            path = self.root / sub / name
            if path.is_file():
                try:
                    return path.read_text(encoding="utf-8", errors="replace").splitlines()
                except OSError:
                    return []
        return []
