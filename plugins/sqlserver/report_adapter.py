"""Adapt the SQL Server report envelope to the shared report contract.

The SQL Server report model already uses ``sqlserver_inspection_report_model``
but stores sections separately (``sections`` vs ``inspection_sections``),
displays tables as headers + two-dimensional rows, and keeps management fields
in its risk register.  This adapter is deterministic and reorganizes those
facts without recalculating rule outcomes, health scores, or severities.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any


REPORT_CONTRACT = "sqlserver_inspection_report_model"
REPORT_SCHEMA = "2.0"

FACT_CONCLUSION = "已采集并展示本项巡检事实，不单独形成风险判断。"
NO_RECORD_CONCLUSION = "未采集到结构化记录，本项不作通过结论。"

_COLLECTION_STATUS_MAP = {
    "success": "ok",
    "ok": "ok",
    "empty": "empty",
    "error": "error",
    "failed": "error",
    "skipped": "skipped",
    "partial": "partial",
    "permission_denied": "permission_denied",
    "timeout": "timeout",
    "unsupported": "unsupported",
    "not_applicable": "not_applicable",
}


def _date(value: Any) -> str:
    raw = str(value or "")
    try:
        return datetime.fromisoformat(raw).date().isoformat()
    except ValueError:
        return raw[:10]


def _seconds_between(start: Any, finish: Any) -> float | None:
    try:
        left = datetime.fromisoformat(str(start or ""))
        right = datetime.fromisoformat(str(finish or ""))
        return round((right - left).total_seconds(), 1)
    except (TypeError, ValueError):
        return None


def _grade(score: Any) -> str:
    try:
        value = float(score)
    except (TypeError, ValueError):
        return "unknown"
    if value >= 90:
        return "good"
    if value >= 70:
        return "fair"
    return "poor"


def _item_id(section_id: str, index: int) -> str:
    return f"{section_id}.{index:02d}"


def _normalize_collection(collection: Any) -> dict[str, Any]:
    value = dict(collection or {})
    status = str(value.get("status") or "").strip().lower()
    value["status"] = _COLLECTION_STATUS_MAP.get(status, status or "not_applicable")
    value.setdefault("row_count", None)
    value.setdefault("reason", "")
    return value


def _normalize_display(item: dict[str, Any]) -> dict[str, Any]:
    display = dict(item.get("display") or {})
    headers = display.get("headers") or []
    raw_rows = display.get("rows") or []
    if headers and raw_rows and all(isinstance(row, list) for row in raw_rows):
        rows = [
            {headers[index]: row[index] for index in range(min(len(headers), len(row)))}
            for row in raw_rows
        ]
    else:
        rows = list(raw_rows)
    display["rows"] = rows
    display["shown_rows"] = len(rows)
    display["total_rows"] = len(rows)
    return display


def _normalize_analysis(item: dict[str, Any], rows: list[Any]) -> dict[str, Any]:
    analysis = dict(item.get("analysis") or {})
    status = str(analysis.get("status") or "").strip()
    if status == "":
        if rows:
            analysis["status"] = "normal"
            if not str(analysis.get("conclusion") or "").strip():
                analysis["conclusion"] = FACT_CONCLUSION
        else:
            analysis["status"] = "not_evaluated"
            if not str(analysis.get("conclusion") or "").strip():
                analysis["conclusion"] = NO_RECORD_CONCLUSION
    if not isinstance(analysis.get("evidence"), list):
        analysis["evidence"] = []
    analysis.setdefault("recommendation", "")
    return analysis


def _normalize_finding(finding: dict[str, Any]) -> dict[str, Any]:
    value = deepcopy(finding)
    value.setdefault("finding_id", value.get("risk_id"))
    value.setdefault("source_severity", value.get("severity"))
    value.setdefault("status", "triggered")
    value.setdefault("title", value.get("risk") or value.get("title"))
    value.setdefault("summary", value.get("current_state") or value.get("impact"))
    facts: list[str] = []
    if value.get("current_state"):
        facts.append(str(value["current_state"]))
    if value.get("impact"):
        facts.append(str(value["impact"]))
    value.setdefault("facts", facts)
    references = value.get("evidence_refs")
    if references is None:
        references = [value["object_name"]] if value.get("object_name") else []
        value["evidence_refs"] = references
    return value


def _build_plan(findings: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    plan: dict[str, list[dict[str, Any]]] = {"P1": [], "P2": [], "P3": []}
    for finding in findings:
        priority = str(finding.get("priority") or "P3")
        plan.setdefault(priority, []).append({
            "finding_id": finding.get("risk_id"),
            "title": finding.get("risk") or finding.get("title"),
            "recommendation": finding.get("recommendation"),
            "owner": finding.get("owner"),
            "planned_window": finding.get("target_window"),
            "verification": finding.get("verification"),
            "status": finding.get("status"),
        })
    return plan


def _build_conclusions(source: dict[str, Any], health: dict[str, Any]) -> list[dict[str, Any]]:
    grade = str(health.get("grade") or "")
    status = {"高风险": "risk", "中风险": "attention", "低风险": "normal"}.get(grade, "attention")
    conclusions: list[dict[str, Any]] = []
    if source.get("overall"):
        conclusions.append({
            "topic": "总体结论",
            "status": status,
            "conclusion": source["overall"],
            "evidence": source.get("priority_risks") or [],
        })
    if source.get("strengths"):
        conclusions.append({
            "topic": "优势",
            "status": "normal",
            "conclusion": "；".join(str(item) for item in source["strengths"]),
            "evidence": [],
        })
    if source.get("decision_points"):
        conclusions.append({
            "topic": "决策要点",
            "status": "attention",
            "conclusion": "；".join(str(item) for item in source["decision_points"]),
            "evidence": [],
        })
    if source.get("next_inspection"):
        conclusions.append({
            "topic": "复检建议",
            "status": "attention",
            "conclusion": source["next_inspection"],
            "evidence": [],
        })
    return conclusions


def _normalize_charts(raw: list[dict[str, Any]]) -> list[dict[str, Any]]:
    charts: list[dict[str, Any]] = []
    for chart in raw:
        chart_id = str(chart.get("id") or "")
        path = chart.get("path") or ""
        charts.append({
            "chart_id": chart_id,
            "caption": chart.get("title") or chart_id,
            "status": "generated" if path else "skipped",
            "file": f"charts/{Path(path).name}" if path else "",
            "source_scope": "derived" if chart_id == "04_risk_distribution" else "realtime_snapshot",
            "kind": chart.get("kind"),
        })
    return charts


def _build_appendix(source: dict[str, Any], findings: list[dict[str, Any]]) -> dict[str, Any]:
    overview = source.get("overview") or {}
    quality = source.get("collection_quality") or {}
    window_seconds = _seconds_between(
        overview.get("collection_started_at"), overview.get("collection_finished_at")
    )

    evaluations: list[dict[str, Any]] = []
    seen: set[str] = set()
    for finding in findings:
        rule_id = str(finding.get("rule_id") or "")
        if not rule_id or rule_id in seen:
            continue
        seen.add(rule_id)
        evaluations.append({
            "rule_id": rule_id,
            "category": finding.get("category"),
            "status": "triggered",
            "severity_if_triggered": finding.get("severity"),
            "finding_id": finding.get("risk_id"),
            "evidence_refs": [finding["object_name"]] if finding.get("object_name") else [],
            "confidence": 1.0,
        })

    limitations = [str(item) for item in quality.get("limitations") or []]
    return {
        "collection_window": {
            "realtime_window_seconds": window_seconds,
            "sqlserver_sample_points": 1,
            "short_window": True,
            "history": {
                "status": "not_applicable",
                "coverage_hours": None,
                "requested_hours": None,
            },
        },
        "data_quality": {
            "score": quality.get("score_pct"),
            "grade": _grade(quality.get("score_pct")),
            "integrity": {
                "status": "not_evaluated",
                "reason": "SQL Server zip 采集包未提供 manifest 哈希校验明细",
            },
            "limitations": limitations,
        },
        "rule_evaluations": evaluations,
        "disclaimer": "本报告基于只读采集包和确定性规则生成；未采集、未评价与不适用项目不得解释为正常。",
    }


def adapt_report_model(source: dict[str, Any]) -> dict[str, Any]:
    """Return a shared-renderer SQL Server model while preserving source facts."""
    environment = source.get("environment") or {}
    instance = environment.get("instance") or {}
    target = environment.get("target") or {}
    overview = source.get("overview") or {}
    control = source.get("document_control") or {}
    cover = source.get("cover") or {}
    health = source.get("health_assessment") or source.get("health") or {}
    quality = source.get("collection_quality") or {}

    server_name = instance.get("server_name") or overview.get("server_name")
    raw_ip = instance.get("connection_ip") or overview.get("ip")
    ip_address = raw_ip if raw_ip not in (None, "", ".") else (instance.get("machine_name") or server_name)
    raw_port = target.get("port") or instance.get("connection_port") or 0
    port_value = raw_port or 1433
    version = instance.get("product_version") or overview.get("product_version")
    collection_time = (
        overview.get("collection_finished_at")
        or overview.get("collection_started_at")
        or source.get("generated_at")
    )

    findings = [_normalize_finding(item) for item in source.get("risk_register") or []]
    counts = health.get("severity_counts") or {}

    sections: list[dict[str, Any]] = []
    for section in source.get("sections") or []:
        items = section.get("items") or []
        if not items:
            continue
        normalized_items: list[dict[str, Any]] = []
        for index, item in enumerate(items, 1):
            display = _normalize_display(item)
            rows = display.get("rows") or []
            normalized_items.append({
                "item_id": _item_id(str(section.get("section_id")), index),
                "title": item.get("title"),
                "source": item.get("source"),
                "collection": _normalize_collection(item.get("collection")),
                "display": display,
                "analysis": _normalize_analysis(item, rows),
            })
        sections.append({
            "section_id": section.get("section_id"),
            "title": section.get("title"),
            "items": normalized_items,
        })

    gaps: list[dict[str, Any]] = []
    for index, gap in enumerate(source.get("collection_gaps") or [], 1):
        gaps.append({
            "item_id": f"collection_gap.{index}",
            "status": "external_evidence_required",
            "reason": str(gap),
            "recommended_action": "启用深度检查后重新采集并复核相应规则状态",
        })

    return {
        "schema_version": REPORT_SCHEMA,
        "generator_contract": REPORT_CONTRACT,
        "cover": {
            "title": "SQL Server数据库巡检分析报告",
            "inspection_target": server_name,
            "database_version": version,
            "report_version": control.get("report_version") or cover.get("report_version") or "V1.0",
            "inspection_date": _date(collection_time) or cover.get("inspection_date"),
        },
        "document_control": {
            "customer": control.get("customer", ""),
            "database": "SQL Server",
            "report_version": control.get("report_version") or "V1.0",
            "generated_at": control.get("generated_at") or source.get("generated_at"),
        },
        "overview": {
            "host": server_name,
            "ip": ip_address,
            "database_version": version,
            "collection_time": collection_time,
            "data_quality": quality,
        },
        "topology": {
            "mode": "single_instance",
            "nodes": [{
                "instance_tag": server_name,
                "hostname": instance.get("machine_name") or server_name,
                "ip": ip_address,
                "port": port_value,
                "role_observed": "PRIMARY",
                "version": version,
            }],
            "edges": [],
        },
        "health_assessment": {
            "score": health.get("score"),
            "grade": health.get("grade"),
            "counts": {
                "critical": counts.get("critical", 0),
                "high": counts.get("high", 0),
                "medium": counts.get("medium", 0),
                "low": counts.get("low", 0),
            },
            "risk_count": health.get("risk_count"),
        },
        "sqlserver_analysis": {"charts": _normalize_charts(source.get("charts") or [])},
        "risk_register": findings,
        "optimization_plan": _build_plan(findings),
        "comprehensive_conclusions": _build_conclusions(
            source.get("comprehensive_conclusions") or {}, health
        ),
        "inspection_sections": sections,
        "collection_gaps": gaps,
        "appendix": _build_appendix(source, findings),
        "extensions": {
            "sqlserver": {
                "source_analyzer_version": source.get("analyzer_version"),
                "source_schema_version": source.get("schema_version"),
                "source_environment": deepcopy(environment),
            },
        },
    }
