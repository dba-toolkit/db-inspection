"""Adapt the Oracle report envelope to the shared report contract.

The Oracle collector already emits a report model close to the shared shape.
This adapter is deterministic and additive: it fills stable ``item_id`` values
and presentation-safe analysis states, but never recalculates rule outcomes,
health scores, severities, or evidence.
"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any


REPORT_CONTRACT = "oracle_inspection_report_model"
REPORT_SCHEMA = "2.0"

FACT_CONCLUSION = "已采集并展示本项巡检事实，不单独形成风险判断。"
NO_RECORD_CONCLUSION = "未采集到结构化记录，本项不作通过结论。"

_CHART_CAPTIONS = {
    # 系统四张来自公共 OS 层（inspection_core.charts.specs.OS_CHART_IDS 同一集合）
    "SYSTEM_CPU": "CPU 使用率趋势",
    "SYSTEM_MEMORY": "内存使用率趋势",
    "SYSTEM_DISK": "磁盘 I/O 趋势",
    "SYSTEM_NETWORK_REALTIME": "网络吞吐趋势",
    "oracle_physical_io": "物理 I/O 趋势",
    "oracle_logical_vs_physical": "逻辑读与物理读",
    "oracle_redo_rate": "Redo 速率",
    "oracle_parse_ratio": "解析率",
}

# 公共渲染器用 sar_history / realtime_snapshot 表达来源，报告模型的历史词汇是
# historical；两者在这里对齐一次，避免下游再按图名猜。
_HISTORICAL_SCOPES = {"historical", "sar_history"}
REPORT_SCOPES = ("historical", "realtime_snapshot")


def _source_scope(chart: dict[str, Any], chart_id: str) -> str:
    scope = str(chart.get("source_scope") or "").strip()
    if scope in _HISTORICAL_SCOPES:
        return "historical"
    if scope in REPORT_SCOPES:
        return "realtime_snapshot"
    # 旧报告模型没有 scope 字段：按图名兜底（SYSTEM_* / sar_* 来自 SAR 历史）。
    return "historical" if chart_id.startswith(("SYSTEM_", "system_", "sar_")) else "realtime_snapshot"


def _extract_item_id(source: Any, section_id: str, index: int) -> str:
    raw = str(source or "").strip()
    for separator in (" — ", " – ", " - ", "—", "–"):
        if separator in raw:
            tail = raw.rsplit(separator, 1)[-1].strip()
            if tail:
                return tail
            break
    return f"{section_id}.{index}"


def _normalize_analysis(item: dict[str, Any]) -> dict[str, Any]:
    analysis = dict(item.get("analysis") or {})
    status = str(analysis.get("status") or "").strip()
    rows = (item.get("display") or {}).get("rows") or []
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
    value.setdefault("source_severity", value.get("severity"))
    value.setdefault("status", "triggered")
    return value


def _normalize_history(window: dict[str, Any]) -> dict[str, Any]:
    source = window.get("history") or {}
    history = deepcopy(source)
    history["status"] = "usable" if source.get("usable_for_trend_rules") else "unusable"
    history.setdefault("coverage_hours", None)
    history.setdefault("requested_hours", None)
    return history


def _normalize_charts(raw: list[dict[str, Any]]) -> list[dict[str, Any]]:
    charts: list[dict[str, Any]] = []
    for chart in raw:
        chart_id = str(chart.get("chart_id") or "")
        file = chart.get("file") or ""
        if not chart_id or chart_id == "ALL" or not file:
            continue
        charts.append({
            "chart_id": chart_id,
            "caption": _CHART_CAPTIONS.get(chart_id, chart_id),
            "status": chart.get("status") or "generated",
            "file": f"charts/{Path(file).name}",
            "source_scope": _source_scope(chart, chart_id),
            "source_points": chart.get("source_points"),
        })
    return charts


def adapt_report_model(source: dict[str, Any]) -> dict[str, Any]:
    """Return a shared-renderer Oracle model while preserving source facts."""
    result = deepcopy(source)
    result.setdefault("schema_version", REPORT_SCHEMA)
    result.setdefault("generator_contract", REPORT_CONTRACT)

    for section in result.get("inspection_sections") or []:
        for index, item in enumerate(section.get("items") or [], 1):
            item["item_id"] = _extract_item_id(
                item.get("source"), str(section.get("section_id")), index
            )
            display = item.setdefault("display", {})
            rows = display.get("rows") or []
            display.setdefault("shown_rows", len(rows))
            display.setdefault("total_rows", len(rows))
            item["analysis"] = _normalize_analysis(item)

    result["risk_register"] = [
        _normalize_finding(item) for item in result.get("risk_register") or []
    ]

    appendix = result.get("appendix") or {}
    window = appendix.get("collection_window") or {}
    window["history"] = _normalize_history(window)
    appendix["collection_window"] = window
    result["appendix"] = appendix

    result["oracle_analysis"] = {"charts": _normalize_charts(source.get("charts") or [])}

    return result
