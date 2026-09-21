"""Adapt the PostgreSQL 2.0 report envelope to the shared report contract.

The adapter is deliberately deterministic: it reorganizes existing facts for
the shared renderer and never recalculates rule outcomes or the health score.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from typing import Any


REPORT_CONTRACT = "postgresql_inspection_report_model"
REPORT_SCHEMA = "2.0"

SEVERITY_MAP = {
    "critical": "high",
    "warning": "medium",
    "info": "low",
    "high": "high",
    "medium": "medium",
    "low": "low",
}
PRIORITY_MAP = {"high": "P1", "medium": "P2", "low": "P3"}


def _first(value: Any) -> dict[str, Any]:
    return value[0] if isinstance(value, list) and value and isinstance(value[0], dict) else {}


def _inspection_date(value: Any) -> str:
    raw = str(value or "")
    try:
        return datetime.fromisoformat(raw).date().isoformat()
    except ValueError:
        return raw[:10]


def _normalize_finding(value: dict[str, Any]) -> dict[str, Any]:
    finding = deepcopy(value)
    original = str(finding.get("severity") or "info")
    finding["source_severity"] = original
    finding["severity"] = SEVERITY_MAP.get(original, original)
    finding.setdefault("status", "triggered")
    return finding


def _build_plan(findings: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    plan: dict[str, list[dict[str, Any]]] = {"P1": [], "P2": [], "P3": []}
    for finding in findings:
        priority = PRIORITY_MAP.get(str(finding.get("severity")), "P3")
        plan[priority].append({
            "finding_id": finding.get("finding_id"),
            "title": finding.get("title"),
            "recommendation": finding.get("recommendation"),
            "owner": None,
            "planned_window": None,
            "verification": "按原检查项重新采集并复核规则状态",
            "status": None,
        })
    return plan


# Chart id → report section.  This is the single source of truth: the analyzer
# tags the raw charts with it so the audit trail records where each picture was
# meant to land, and the adapter re-derives it when adapting older models.
HISTORY_CHART_IDS = ("SYSTEM_CPU", "SYSTEM_MEMORY", "SYSTEM_DISK")
REALTIME_CHART_IDS = ("SYSTEM_NETWORK_REALTIME",)


def chart_section(chart_id: str) -> str:
    """Return the report section a chart belongs to, or '' when unknown."""
    if chart_id in HISTORY_CHART_IDS or chart_id in REALTIME_CHART_IDS:
        return "system_info"
    if chart_id == "pg_sessions":
        return "connections"
    if chart_id == "pg_stats":
        return "performance"
    return ""


def _normalized_conclusions(source: dict[str, Any], evaluations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    conclusions = deepcopy(source.get("comprehensive_conclusions") or [])
    backup_status = next(
        (item.get("status") for item in evaluations if item.get("rule_id") == "PG.BACKUP.CONFIGURED"),
        None,
    )
    if backup_status != "not_evaluated":
        return conclusions
    old = "未检测到备份策略"
    replacement = "备份策略证据不足，未作通过结论"
    for item in conclusions:
        if item.get("topic") != "安全与可恢复性":
            continue
        item["conclusion"] = str(item.get("conclusion") or "").replace(old, replacement)
        item["evidence"] = [replacement if value == old else value for value in item.get("evidence") or []]
    return conclusions


def adapt_report_model(source: dict[str, Any]) -> dict[str, Any]:
    """Return a shared-renderer model while preserving the PG source model."""
    instance = _first(source.get("instances"))
    identity = instance.get("identity") or {}
    quality = instance.get("collection_quality") or {}
    sampling = instance.get("sampling") or {}
    collector = instance.get("collector") or {}
    window = source.get("collection_window") or {}
    analyzed_at = source.get("analyzed_at")
    collection_time = collector.get("finished_at") or collector.get("started_at") or analyzed_at

    findings = [_normalize_finding(item) for item in source.get("risk_register") or []]
    severity_counts = {
        severity: sum(1 for item in findings if item.get("severity") == severity)
        for severity in ("high", "medium", "low")
    }

    nodes = deepcopy((source.get("topology") or {}).get("nodes") or [])
    if not nodes and instance:
        nodes = [{"instance_id": instance.get("instance_id")}]
    for node in nodes:
        if node.get("instance_id") == instance.get("instance_id"):
            node.setdefault("instance_tag", instance.get("instance_id"))
            node.setdefault("hostname", identity.get("hostname"))
            node.setdefault("ip", identity.get("ip"))
            node.setdefault("port", identity.get("port"))
            node.setdefault("role_observed", identity.get("role_observed") or node.get("role"))
            node.setdefault("version", identity.get("version"))
    topology = deepcopy(source.get("topology") or {})
    topology["nodes"] = nodes
    topology.setdefault("edges", [])
    topology.setdefault("mode", "single_instance" if len(nodes) == 1 else "multi_node")

    raw_charts = source.get("charts") or []
    charts = []
    for item in raw_charts:
        if item.get("status") == "skipped":
            charts.append(deepcopy(item))
            continue
        chart = deepcopy(item)
        chart["status"] = "generated"
        chart["source_section_id"] = chart.get("section_id")
        chart["section_id"] = chart_section(str(chart.get("chart_id")))
        chart.setdefault(
            "source_scope",
            "historical" if chart.get("chart_id") in HISTORY_CHART_IDS else "realtime_snapshot",
        )
        charts.append(chart)

    sar_coverage = window.get("sar_coverage_hours")
    sar_requested = window.get("sar_requested_hours")
    try:
        sar_usable = bool(window.get("sar_available")) and float(sar_coverage or 0) >= 0.8 * float(sar_requested or 24)
    except (TypeError, ValueError):
        sar_usable = False
    limitations: list[str] = []
    if window.get("sar_available") and not sar_usable:
        limitations.append(f"SAR 历史覆盖仅 {sar_coverage}/{sar_requested} 小时，不作为完整周期趋势证据")
    if quality.get("unsupported"):
        limitations.append(f"{quality.get('unsupported')} 个采集项不受当前环境支持")
    if quality.get("not_enabled"):
        limitations.append(f"{quality.get('not_enabled')} 个可选能力未启用")

    evaluations = deepcopy(source.get("rule_evaluations") or [])
    gaps = [{
        "item_id": item.get("rule_id"),
        "status": "external_evidence_required",
        "reason": item.get("reason"),
        "recommended_action": "补充相应证据后重新评价，不得按正常处理",
    } for item in evaluations if item.get("status") == "not_evaluated"]

    sections = deepcopy((source.get("inspection_model") or {}).get("sections") or [])
    for section in sections:
        for item in section.get("items") or []:
            display = item.get("display") or {}
            rows = display.get("rows") or []
            display.setdefault("shown_rows", len(rows))
            display.setdefault("total_rows", len(rows))
            if item.get("item_id") == "pg.backup":
                item.setdefault("extensions", {})["source_analysis"] = deepcopy(item.get("analysis") or {})
                item["analysis"] = {
                    "status": "not_evaluated",
                    "conclusion": "未检测到主机侧备份工具或定时任务；该线索不能证明数据库没有备份，当前证据不足。",
                    "evidence": ["主机侧备份工具/cron/timer 未检出", "备份平台记录与恢复演练证据未纳入采集包"],
                    "recommendation": "补充备份平台任务、最近成功记录、保留周期及恢复演练证据后重新评价。",
                }

    return {
        "schema_version": REPORT_SCHEMA,
        "generator_contract": REPORT_CONTRACT,
        "cover": {
            "title": "PostgreSQL数据库巡检分析报告",
            "inspection_target": identity.get("hostname") or instance.get("instance_id"),
            "database_version": identity.get("version"),
            "report_version": "V1.0",
            "inspection_date": _inspection_date(collection_time),
        },
        "document_control": {
            "customer": "",
            "database": "PostgreSQL",
            "report_version": "V1.0",
            "generated_at": analyzed_at,
        },
        "overview": {
            "host": identity.get("hostname"),
            "ip": identity.get("ip"),
            "database_version": identity.get("version"),
            "collection_time": collection_time,
            "data_quality": quality,
        },
        "environment": {"instances": deepcopy(source.get("instances") or [])},
        "topology": topology,
        "health_assessment": {
            "score": (source.get("health_summary") or {}).get("score"),
            "grade": (source.get("health_summary") or {}).get("grade"),
            "counts": severity_counts,
            "source_scale": {"critical": "high", "warning": "medium", "info": "low"},
        },
        "postgresql_analysis": {"charts": charts},
        "risk_register": findings,
        "optimization_plan": _build_plan(findings),
        "comprehensive_conclusions": _normalized_conclusions(source, evaluations),
        "inspection_sections": sections,
        "collection_gaps": gaps,
        "appendix": {
            "collection_window": {
                "realtime_window_seconds": window.get("realtime_window_seconds") or sampling.get("duration_seconds"),
                "postgresql_sample_points": sampling.get("sample_points"),
                "short_window": window.get("short_window"),
                "history": {
                    "status": "usable" if sar_usable else "partial" if window.get("sar_available") else "unusable",
                    "coverage_hours": sar_coverage,
                    "requested_hours": sar_requested,
                },
            },
            "data_quality": {
                "score": quality.get("score") or window.get("data_score"),
                "grade": window.get("data_grade"),
                "integrity": {"status": "not_evaluated", "reason": "旧 PG 报告模型未携带 manifest 校验明细"},
                "limitations": limitations,
            },
            "rule_evaluations": evaluations,
            "disclaimer": "本报告基于只读采集包和确定性规则生成；未采集、未评价与不适用项目不得解释为正常。",
        },
        "extensions": {
            "postgresql": {
                "source_analyzer_version": source.get("analyzer_version"),
                "source_analysis_schema": source.get("analysis_schema"),
                "source_health_summary": deepcopy(source.get("health_summary") or {}),
                "source_collection_window": deepcopy(window),
            }
        },
    }
