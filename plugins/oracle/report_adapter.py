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
# 「查了、确认没有」(success_no_data) 与「没采到」(not_evaluated) 是**相反结论**，
# 不能让它们在章末小结里长得一样：正文按采集状态写了"已检查，本次未发现相关记录。"，
# 小结却写"未采集到结构化记录"，同一件事两种说法，读者只会认为报告自相矛盾。
EMPTY_CONCLUSION = "已检查，本次未发现相关记录。"
EMPTY_EVIDENCE = "已按本项口径查询，本次无匹配记录。"

from .charts import ORACLE_CHART_CAPTIONS, OS_CHART_CAPTIONS

# 图题与图 id 同源（``plugins/oracle/charts.py``），**不在这里抄第二份**：
# 适配器是「重建」模型而非透传，抄一份就会在新图上线时把 caption 静默落回
# chart_id（报告里图题变成英文 id），且没有任何测试会红。
_CHART_CAPTIONS = {**OS_CHART_CAPTIONS, **ORACLE_CHART_CAPTIONS}

# 公共渲染器用 sar_history / realtime_snapshot 表达来源，报告模型的历史词汇是
# historical；两者在这里对齐一次，避免下游再按图名猜。
_HISTORICAL_SCOPES = {"historical", "sar_history"}
# 报告模型的来源词汇：历史 / 实时短采样 / **表数据快照**（排行、汇总 —— 一次查询的
# 结果，既不是 SAR 历史，也不是采集窗口内的实时采样）。词汇不全时新图会被兜底成
# realtime_snapshot，让只有排行图的章节（存储容量 / 备份）被误加「短时采样」说明。
REPORT_SCOPES = ("historical", "realtime_snapshot", "table_snapshot")


def _source_scope(chart: dict[str, Any], chart_id: str) -> str:
    scope = str(chart.get("source_scope") or "").strip()
    if scope in _HISTORICAL_SCOPES:
        return "historical"
    if scope in REPORT_SCOPES:
        return scope
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
    display = item.get("display") or {}
    rows = display.get("rows") or []
    if status == "":
        collection_status = str((item.get("collection") or {}).get("status") or "").strip()
        if rows:
            analysis["status"] = "normal"
            if not str(analysis.get("conclusion") or "").strip():
                analysis["conclusion"] = FACT_CONCLUSION
        elif collection_status == "empty":
            # 查了、确认没有：结论必须是"未发现"，不能压成"未采集"。
            analysis["status"] = "normal"
            if not str(analysis.get("conclusion") or "").strip():
                analysis["conclusion"] = EMPTY_CONCLUSION
            if not analysis.get("evidence"):
                analysis["evidence"] = [EMPTY_EVIDENCE]
        else:
            analysis["status"] = "not_evaluated"
            if not str(analysis.get("conclusion") or "").strip():
                analysis["conclusion"] = NO_RECORD_CONCLUSION
    if not isinstance(analysis.get("evidence"), list):
        analysis["evidence"] = []
    if not analysis["evidence"]:
        # 事实项（无规则判定）原来在章末小结里只剩「未提供补充依据」一句：既没说采了
        # 多少、也没说该看什么，读者无从复核。小结的"依据与建议"列本就是放**可核对的
        # 量**的（MySQL 同列写的是「CPU 64 Core；内存 251.74 GB」），这里补上：
        # 展示层备注优先（如「已排除 7 个非持久化挂载点」），否则用结构化记录条数。
        note = str(display.get("note") or "").strip()
        if note:
            analysis["evidence"] = [note]
        elif rows:
            analysis["evidence"] = [f"本项为事实展示，共 {len(rows)} 条结构化记录"]
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
