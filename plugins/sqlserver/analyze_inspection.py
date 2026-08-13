#!/usr/bin/env python3
"""Analyze SQL Server inspection snapshots and build a stable report model."""
from __future__ import annotations

import argparse
import json
import math
import shutil
import tempfile
import zipfile
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

from rules import RuleEngine

ANALYZER_VERSION = "1.1.0"


def read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8-sig") as f:
        value = json.load(f)
    if not isinstance(value, dict):
        raise ValueError(f"JSON root must be an object: {path}")
    return value


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(value, f, ensure_ascii=False, indent=2)
        f.write("\n")


def discover_snapshot(source: Path, temp_root: Path) -> Path:
    if source.is_file() and source.suffix.lower() == ".json":
        return source
    if source.is_dir():
        direct = source / "snapshot.json"
        if direct.exists():
            return direct
        matches = list(source.rglob("snapshot.json"))
        if len(matches) == 1:
            return matches[0]
        raise ValueError(f"Cannot uniquely locate snapshot.json in {source}")
    if source.is_file() and source.suffix.lower() == ".zip":
        out = temp_root / source.stem
        with zipfile.ZipFile(source) as zf:
            for item in zf.infolist():
                target = (out / item.filename).resolve()
                if out.resolve() not in target.parents and target != out.resolve():
                    raise ValueError(f"Unsafe archive entry: {item.filename}")
            zf.extractall(out)
        matches = list(out.rglob("snapshot.json"))
        if len(matches) != 1:
            raise ValueError(f"Archive must contain one snapshot.json: {source}")
        return matches[0]
    raise ValueError(f"Unsupported input: {source}")


def num(v: Any) -> float | None:
    try:
        x = float(v)
        return x if math.isfinite(x) else None
    except (TypeError, ValueError):
        return None


def build_charts(snapshot: dict[str, Any], findings: list[dict[str, Any]], output: Path) -> list[dict[str, str]]:
    from chart_style import bar_chart, line_chart

    output.mkdir(parents=True, exist_ok=True)
    charts: list[dict[str, str]] = []

    def add_line(filename: str, title: str, samples: list[dict[str, Any]], specs: list[tuple[str, str]]) -> None:
        if not samples or not any(any(num(x.get(key)) is not None for x in samples) for key, _ in specs):
            return
        path = output / filename
        labels = [str(x.get("timestamp", ""))[11:19] for x in samples]
        line_chart(path, title, labels, [(label, [num(x.get(key)) for x in samples]) for key, label in specs])
        charts.append({"id": path.stem, "title": title, "path": str(path.resolve()), "kind": "line"})

    def add_bar(filename: str, title: str, labels: list[str], values: list[float], suffix: str = "", colors: list[str] | None = None) -> None:
        if not labels or not values:
            return
        path = output / filename
        bar_chart(path, title, labels, values, colors=colors, suffix=suffix)
        charts.append({"id": path.stem, "title": title, "path": str(path.resolve()), "kind": "bar"})

    samples = snapshot.get("performance_samples", [])
    add_line("01_activity.png", "SQL Server 业务活动采样", samples,
             [("batch_requests_per_sec", "Batch Requests/s"), ("transactions_per_sec", "Transactions/s")])
    add_line("02_memory.png", "SQL Server 内存压力指标", samples,
             [("page_life_expectancy", "PLE (s)"), ("memory_grants_pending", "Memory Grants Pending")])
    add_line("03_connections.png", "连接与阻塞采样", samples,
             [("user_connections", "User Connections"), ("blocked_session_count", "Blocked Sessions")])

    severity_order = ["critical", "high", "medium", "low"]
    severity_cn = {"critical": "严重", "high": "高", "medium": "中", "low": "低"}
    severity_values = [sum(1 for x in findings if x.get("severity") == s) for s in severity_order]
    add_bar("04_risk_distribution.png", "巡检风险等级分布",
            [severity_cn[s] for s in severity_order], severity_values, "项",
            ["#9B1C1C", "#C2410C", "#B26A00", "#66717E"])

    waits = sorted(snapshot.get("wait_stats", []), key=lambda x: num(x.get("wait_time_ms")) or 0, reverse=True)[:8]
    add_bar("05_waits.png", "主要等待类型（累计秒）",
            [str(x.get("wait_type", "-")) for x in waits], [(num(x.get("wait_time_ms")) or 0) / 1000 for x in waits], "s")

    dbs = [x for x in snapshot.get("databases", []) if str(x.get("name", "")).lower() not in {"master", "model", "msdb", "tempdb"}]
    dbs = sorted(dbs, key=lambda x: (num(x.get("data_size_mb")) or 0) + (num(x.get("log_size_mb")) or 0), reverse=True)[:8]
    add_bar("06_database_size.png", "用户数据库容量（GB）",
            [str(x.get("name", "-")) for x in dbs], [((num(x.get("data_size_mb")) or 0) + (num(x.get("log_size_mb")) or 0)) / 1024 for x in dbs], "GB")

    files = sorted(snapshot.get("database_files", []), key=lambda x: max(num(x.get("avg_read_latency_ms")) or 0, num(x.get("avg_write_latency_ms")) or 0), reverse=True)[:8]
    add_bar("07_file_latency.png", "数据库文件最大平均 I/O 延迟",
            [f"{x.get('database_name')}/{x.get('logical_name')}" for x in files],
            [max(num(x.get("avg_read_latency_ms")) or 0, num(x.get("avg_write_latency_ms")) or 0) for x in files], "ms")

    volumes = sorted(snapshot.get("volumes", []), key=lambda x: num(x.get("free_pct")) or 100)[:8]
    add_bar("08_volume_free.png", "数据库所在卷可用空间比例",
            [str(x.get("volume_mount_point") or x.get("logical_volume_name") or "卷") for x in volumes],
            [num(x.get("free_pct")) or 0 for x in volumes], "%",
            ["#9B1C1C" if (num(x.get("free_pct")) or 0) < 10 else "#B26A00" if (num(x.get("free_pct")) or 0) < 20 else "#276749" for x in volumes])

    collected = snapshot.get("collection", {}).get("finished_at")
    try:
        now = datetime.fromisoformat(str(collected).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        now = datetime.now().astimezone()
    backup_labels: list[str] = []
    backup_hours: list[float] = []
    for row in snapshot.get("backups", []):
        if str(row.get("database_name", "")).lower() == "tempdb":
            continue
        try:
            then = datetime.fromisoformat(str(row.get("last_full_backup")).replace("Z", "+00:00"))
            if then.tzinfo is None and now.tzinfo is not None:
                then = then.replace(tzinfo=now.tzinfo)
            hours = max(0.0, (now - then).total_seconds() / 3600)
        except (TypeError, ValueError):
            hours = 999.0
        backup_labels.append(str(row.get("database_name", "-")))
        backup_hours.append(hours)
    pairs = sorted(zip(backup_labels, backup_hours), key=lambda x: x[1], reverse=True)[:8]
    add_bar("09_backup_age.png", "最近完整备份距今时间",
            [x[0] for x in pairs], [x[1] for x in pairs], "h",
            ["#9B1C1C" if x[1] > 168 else "#B26A00" if x[1] > 72 else "#276749" for x in pairs])

    return charts


def normalize_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    snapshot.setdefault("schema_version", "1.0")
    for key in ("databases", "backups", "backup_history", "no_backup_databases", "database_files",
                "wait_stats", "blocking", "active_requests", "connection_stats",
                "top_queries", "top_queries_logical_reads", "top_queries_executions", "top_queries_physical_reads",
                "missing_indexes", "unused_indexes", "configurations", "failed_jobs",
                "availability_replicas", "error_log_summary", "performance_samples"):
        snapshot.setdefault(key, [])
    for key in ("volumes", "log_space", "vlf_summary", "suspect_pages", "agent_jobs",
                "database_mirroring", "log_shipping", "replication",
                "stale_statistics", "fragmented_indexes", "orphaned_users",
                "tables_without_pk", "large_tables", "database_rcsi", "database_summary",
                "buffer_pool", "memory_status", "cpu_by_database"):
        snapshot.setdefault(key, [])
    snapshot.setdefault("instance", {})
    snapshot.setdefault("tempdb", {"files": []})
    snapshot.setdefault("security", {"sql_logins": [], "sysadmin_members": [], "linked_servers": [], "security_checks": []})
    snapshot.setdefault("collection", {})
    return snapshot


def score(findings: list[dict[str, Any]], quality_pct: float) -> tuple[int, str]:
    weights = {"critical": 18, "high": 10, "medium": 4, "low": 1}
    raw = max(0, 100 - sum(weights.get(str(x.get("severity")), 0) for x in findings))
    result = round(raw * (0.85 + 0.15 * quality_pct / 100))
    grade = "健康" if result >= 90 else "良好" if result >= 75 else "关注" if result >= 60 else "高风险"
    return result, grade


def build_model(snapshot: dict[str, Any], findings: list[dict[str, Any]], charts: list[dict[str, str]]) -> dict[str, Any]:
    status = snapshot.get("collection", {}).get("module_status", [])
    ok = sum(1 for x in status if str(x.get("status", "")).lower() == "success")
    quality = round(ok * 100 / len(status), 1) if status else 100.0
    health_score, health_grade = score(findings, quality)
    counts = Counter(str(x.get("severity", "info")) for x in findings)
    priority = sorted(findings, key=lambda x: ({"critical": 0, "high": 1, "medium": 2, "low": 3}.get(str(x.get("severity")), 9), str(x.get("category"))))
    instance = snapshot.get("instance", {})
    owner_map = {
        "备份恢复": "备份管理员", "高可用": "数据库架构师", "安全": "安全管理员",
        "数据库安全": "安全管理员", "存储与容量": "存储/数据库管理员", "存储与文件": "存储/数据库管理员",
        "事务日志": "数据库管理员", "SQL Agent": "数据库管理员", "性能与会话": "性能优化负责人",
        "性能与内存": "性能优化负责人", "性能治理": "数据库开发/管理员", "TempDB": "数据库管理员",
        "完整性": "数据库管理员", "错误日志": "数据库管理员", "实例配置": "数据库管理员",
        "数据库": "数据库管理员", "数据库对象": "数据库开发/管理员"
    }
    target_map = {"P1": "0-7 天", "P2": "8-30 天", "P3": "31-90 天"}
    verify_map = {
        "BACKUP": "完成备份链检查和测试还原，保存还原日志与结果截图。",
        "HA": "复采同步状态并完成一次受控故障切换或灾备可用性验证。",
        "PERF": "在同等业务窗口复采，确认等待、耗时或资源指标明显改善且无回归。",
        "SEC": "重新执行权限与配置查询，确认危险配置关闭或例外审批有效。",
        "STORAGE": "复采容量与增长趋势，确认告警阈值恢复并建立监控。",
        "LOG": "复采日志使用率、VLF 和 log_reuse_wait，确认日志链与增长恢复正常。",
        "DB": "执行针对性检查并复采数据库状态、选项或完整性证据。",
        "AGENT": "补跑作业并确认连续成功，保存作业历史。",
        "TEMPDB": "在业务高峰复采 tempdb 使用率、文件配置和等待指标。"
    }
    risks: list[dict[str, Any]] = []
    for idx, f in enumerate(priority, 1):
        prefix = str(f.get("rule_id", "")).split(".")[0]
        risks.append({
            "risk_id": f"R-{idx:03d}", "rule_id": f.get("rule_id"), "category": f.get("category"),
            "risk": f.get("title"), "severity": f.get("severity"), "priority": f.get("priority"),
            "object_name": f.get("object_name"), "current_state": f.get("evidence"),
            "impact": f.get("impact"), "recommendation": f.get("recommendation"),
            "owner": owner_map.get(str(f.get("category")), "数据库管理员"),
            "target_window": target_map.get(str(f.get("priority")), "持续跟踪"),
            "verification": verify_map.get(prefix, "完成整改后重新采集对应证据，并由复核人确认关闭。"),
            "status": "待处理"
        })
    plans = []
    for pcode in ("P1", "P2", "P3"):
        items = [x for x in risks if x["priority"] == pcode]
        plans.append({
            "phase": pcode, "label": {"P1": "立即处置", "P2": "计划整改", "P3": "持续优化"}[pcode],
            "target_window": target_map[pcode], "items": items,
            "exit_criteria": "全部项目形成执行记录和复采证据；未关闭项具备风险接受审批。"
        })
    healthy_signals: list[str] = []
    if not snapshot.get("suspect_pages"):
        healthy_signals.append("未采集到严重可疑页面记录。")
    if not snapshot.get("blocking"):
        healthy_signals.append("采集时点未发现活动阻塞链。")
    if not snapshot.get("failed_jobs"):
        healthy_signals.append("检查窗口内未发现 SQL Agent 失败作业。")
    if all(str(x.get("state_desc", "")).upper() == "ONLINE" for x in snapshot.get("databases", [])):
        healthy_signals.append("采集范围内数据库均处于 ONLINE 状态。")
    if not snapshot.get("no_backup_databases"):
        healthy_signals.append("所有用户数据库均存在完整备份记录。")
    if not snapshot.get("tables_without_pk"):
        healthy_signals.append("所有用户数据库均无缺失主键的表。")
    if not healthy_signals:
        healthy_signals.append("当前样本以风险验证为主，未形成可确认的健康亮点。")
    top_risks = [f"{x['risk']}（{x['object_name']}）" for x in risks[:5]]
    total_data_mb = sum((num(x.get("data_size_mb")) or 0) for x in snapshot.get("databases", []))
    total_log_mb = sum((num(x.get("log_size_mb")) or 0) for x in snapshot.get("databases", []))
    user_dbs = [x for x in snapshot.get("databases", []) if str(x.get("name", "")).lower() not in {"master", "model", "msdb", "tempdb"}]
    perf_samples = snapshot.get("performance_samples", [])
    perf_summary = {
        "sample_count": len(perf_samples),
        "batch_requests_peak": max([num(x.get("batch_requests_per_sec")) or 0 for x in perf_samples] or [0]),
        "transactions_peak": max([num(x.get("transactions_per_sec")) or 0 for x in perf_samples] or [0]),
        "ple_min": min([num(x.get("page_life_expectancy")) for x in perf_samples if num(x.get("page_life_expectancy")) is not None] or [0]),
        "memory_grants_pending_peak": max([num(x.get("memory_grants_pending")) or 0 for x in perf_samples] or [0]),
        "connections_peak": max([num(x.get("user_connections")) or 0 for x in perf_samples] or [0]),
        "blocked_sessions_peak": max([num(x.get("blocked_session_count")) or 0 for x in perf_samples] or [0])
    }
    return {
        "generator_contract": "sqlserver_inspection_report_model",
        "schema_version": "1.0",
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "analyzer_version": ANALYZER_VERSION,
        "cover": {
            "title": "SQL Server 数据库巡检分析报告",
            "inspection_target": instance.get("server_name") or snapshot.get("target", {}).get("server", "SQL Server"),
            "database_version": str(instance.get("product_version") or "未知"),
            "report_version": "V1.0",
            "inspection_date": str(snapshot.get("collection", {}).get("started_at") or "")[:10],
        },
        "document_control": {
            "report_type": "SQL Server 数据库巡检分析报告", "report_version": "V1.0",
            "classification": "内部使用", "analysis_method": "只读采集 + 确定性规则 + 人工复核",
            "customer": "待填写", "database": "SQL Server",
            "generated_at": datetime.now().astimezone().isoformat(timespec="seconds")
        },
        "overview": {
            "server_name": instance.get("server_name") or snapshot.get("target", {}).get("server"),
            "instance_name": instance.get("instance_name"),
            "ip": snapshot.get("target", {}).get("server"),
            "product_version": instance.get("product_version"),
            "edition": instance.get("edition"),
            "sqlserver_start_time": instance.get("sqlserver_start_time"),
            "os_version": instance.get("os_version"),
            "cpu_count": instance.get("cpu_count"),
            "physical_memory_mb": instance.get("physical_memory_mb"),
            "database_count": len(snapshot.get("databases", [])),
            "collection_started_at": snapshot.get("collection", {}).get("started_at"),
            "collection_finished_at": snapshot.get("collection", {}).get("finished_at")
        },
        "health": {"score": health_score, "grade": health_grade, "severity_counts": dict(counts), "finding_count": len(findings)},
        "collection_quality": {"score_pct": quality, "modules": status, "limitations": snapshot.get("collection", {}).get("limitations", [])},
        "health_assessment": {
            "score": health_score, "grade": health_grade, "severity_counts": dict(counts),
            "risk_count": len(risks), "top_risks": top_risks, "healthy_signals": healthy_signals,
            "management_summary": (
                f"本次巡检共识别 {len(risks)} 项风险，其中严重 {counts.get('critical',0)} 项、高风险 {counts.get('high',0)} 项。"
                f"综合健康评分为 {health_score} 分，采集完整度为 {quality}%。应优先处理影响可恢复性、数据完整性、"
                "存储容量和高可用状态的 P1 项，并通过复采与演练形成闭环证据。"
            )
        },
        "environment": {
            "instance": instance, "target": snapshot.get("target", {}), "database_count": len(snapshot.get("databases", [])),
            "user_database_count": len(user_dbs), "total_data_mb": round(total_data_mb, 2), "total_log_mb": round(total_log_mb, 2)
        },
        "system_analysis": {
            "databases": snapshot.get("databases", []), "configurations": snapshot.get("configurations", []),
            "files": snapshot.get("database_files", []), "volumes": snapshot.get("volumes", []),
            "log_space": snapshot.get("log_space", []), "vlf_summary": snapshot.get("vlf_summary", []),
            "suspect_pages": snapshot.get("suspect_pages", []), "database_rcsi": snapshot.get("database_rcsi", []),
            "database_summary": snapshot.get("database_summary", []),
            "tables_without_pk": snapshot.get("tables_without_pk", []), "large_tables": snapshot.get("large_tables", [])
        },
        "sqlserver_performance": {
            "summary": perf_summary, "samples": perf_samples, "wait_stats": snapshot.get("wait_stats", []),
            "blocking": snapshot.get("blocking", []), "active_requests": snapshot.get("active_requests", []),
            "connection_stats": snapshot.get("connection_stats", []),
            "top_queries": snapshot.get("top_queries", []), "top_queries_logical_reads": snapshot.get("top_queries_logical_reads", []),
            "top_queries_executions": snapshot.get("top_queries_executions", []), "top_queries_physical_reads": snapshot.get("top_queries_physical_reads", []),
            "missing_indexes": snapshot.get("missing_indexes", []), "unused_indexes": snapshot.get("unused_indexes", []),
            "stale_statistics": snapshot.get("stale_statistics", []), "fragmented_indexes": snapshot.get("fragmented_indexes", []),
            "tempdb": snapshot.get("tempdb", {}),
            "buffer_pool": snapshot.get("buffer_pool", []), "memory_status": snapshot.get("memory_status", []),
            "cpu_by_database": snapshot.get("cpu_by_database", [])
        },
        "backup_recovery": {"backups": snapshot.get("backups", []), "backup_history": snapshot.get("backup_history", []),
                            "no_backup_databases": snapshot.get("no_backup_databases", []), "suspect_pages": snapshot.get("suspect_pages", [])},
        "security_analysis": {**snapshot.get("security", {}), "orphaned_users": snapshot.get("orphaned_users", []),
                              "security_checks": snapshot.get("security", {}).get("security_checks", [])},
        "capacity_analysis": {
            "total_data_mb": round(total_data_mb, 2), "total_log_mb": round(total_log_mb, 2),
            "database_sizes": snapshot.get("databases", []), "volumes": snapshot.get("volumes", []),
            "files": snapshot.get("database_files", []), "log_space": snapshot.get("log_space", []),
            "database_summary": snapshot.get("database_summary", []), "large_tables": snapshot.get("large_tables", []),
            "capacity_conclusion": "容量结论基于当前时点；正式预测需至少 30 天同口径历史数据。"
        },
        "high_availability": {
            "availability_replicas": snapshot.get("availability_replicas", []),
            "database_mirroring": snapshot.get("database_mirroring", []), "log_shipping": snapshot.get("log_shipping", []),
            "replication": snapshot.get("replication", [])
        },
        "operations": {
            "failed_jobs": snapshot.get("failed_jobs", []), "agent_jobs": snapshot.get("agent_jobs", []),
            "error_log_summary": snapshot.get("error_log_summary", [])
        },
        "risk_register": risks,
        "optimization_plan": plans,
        "comprehensive_conclusions": {
            "overall": f"实例健康等级为“{health_grade}”，建议按 P1/P2/P3 三阶段实施整改并在同等业务窗口复采。",
            "strengths": healthy_signals, "priority_risks": top_risks,
            "decision_points": ["确认 P1 风险责任人和维护窗口", "安排备份还原与高可用验证", "建立容量与性能历史基线"],
            "next_inspection": "重大整改完成后立即复检；常规生产实例建议每月巡检，核心实例建议每周自动采集并按月出具正式报告。"
        },
        "inspection_sections": [
            "管理摘要", "范围与方法", "健康评估", "实例环境", "数据库配置",
            "备份恢复", "备份历史", "容量与存储",
            "性能基线", "等待与阻塞", "TempDB", "SQL与索引",
            "内存与缓冲池", "连接与会话", "数据库对象",
            "安全权限", "高可用", "复制", "作业与日志",
            "风险登记册", "整改计划", "综合结论", "采集缺口与附录"
        ],
        "collection_gaps": snapshot.get("collection", {}).get("limitations", []),
        "appendix": {"rule_count": len({x.get("rule_id") for x in findings}), "chart_count": len(charts), "collector_version": snapshot.get("collector_version")},
        "executive_findings": priority[:10],
        "findings": priority,
        "inventory": {
            "databases": snapshot.get("databases", []), "backups": snapshot.get("backups", []),
            "backup_history": snapshot.get("backup_history", [])[:30], "no_backup_databases": snapshot.get("no_backup_databases", []),
            "database_files": snapshot.get("database_files", []), "wait_stats": snapshot.get("wait_stats", [])[:15],
            "blocking": snapshot.get("blocking", []), "active_requests": snapshot.get("active_requests", [])[:20],
            "connection_stats": snapshot.get("connection_stats", [])[:30],
            "top_queries": snapshot.get("top_queries", [])[:20], "top_queries_logical_reads": snapshot.get("top_queries_logical_reads", [])[:20],
            "top_queries_executions": snapshot.get("top_queries_executions", [])[:20], "top_queries_physical_reads": snapshot.get("top_queries_physical_reads", [])[:20],
            "missing_indexes": snapshot.get("missing_indexes", [])[:20], "unused_indexes": snapshot.get("unused_indexes", [])[:20],
            "tempdb": snapshot.get("tempdb", {}), "configurations": snapshot.get("configurations", []),
            "buffer_pool": snapshot.get("buffer_pool", []), "memory_status": snapshot.get("memory_status", []),
            "cpu_by_database": snapshot.get("cpu_by_database", []),
            "tables_without_pk": snapshot.get("tables_without_pk", []), "large_tables": snapshot.get("large_tables", [])[:30],
            "database_rcsi": snapshot.get("database_rcsi", []), "database_summary": snapshot.get("database_summary", []),
            "security": snapshot.get("security", {}), "failed_jobs": snapshot.get("failed_jobs", []),
            "availability_replicas": snapshot.get("availability_replicas", []),
            "error_log_summary": snapshot.get("error_log_summary", [])[:30],
            "volumes": snapshot.get("volumes", []), "log_space": snapshot.get("log_space", []),
            "vlf_summary": snapshot.get("vlf_summary", []), "suspect_pages": snapshot.get("suspect_pages", []),
            "agent_jobs": snapshot.get("agent_jobs", []), "database_mirroring": snapshot.get("database_mirroring", []),
            "log_shipping": snapshot.get("log_shipping", []), "replication": snapshot.get("replication", []),
            "stale_statistics": snapshot.get("stale_statistics", [])[:50], "fragmented_indexes": snapshot.get("fragmented_indexes", [])[:50],
            "orphaned_users": snapshot.get("orphaned_users", [])[:50]
        },
        "charts": charts,
        "methodology": [
            "结论由本地确定性规则生成；未采集数据不会判定为正常。",
            "等待统计、文件 I/O 和计划缓存指标为实例启动/统计清零后的累计视图，需结合业务时段解释。",
            "备份合规不仅看时间，还应通过定期还原验证证明可恢复性。",
            "所有整改建议均需完成影响评估、测试验证、备份与变更审批。"
        ],
        "sections": _inject_analysis(_build_sections(snapshot, perf_summary), findings),
    }


def _build_sections(s: dict[str, Any], perf: dict[str, Any]) -> list[dict[str, Any]]:
    """Build data-driven inspection sections for the docx generator."""
    import math as _math
    from datetime import datetime as _datetime
    inst = s.get("instance", {})
    tdb = s.get("tempdb", {})
    sec = s.get("security", {})
    mods = s.get("collection", {}).get("module_status", [])
    mod_ok = {str(m.get("name", "")): m for m in mods}

    def _coll(name): m = mod_ok.get(name, {}); return {"status": m.get("status", "ok"), "row_count": m.get("row_count", 0)}

    def _rows(data, keys, fmts=None):
        result = []
        for row in data:
            r = []
            for k in keys:
                v = row.get(k)
                if fmts and k in fmts:
                    v = fmts[k](v)
                else:
                    v = _tv(v)
                r.append(v)
            result.append(r)
        return result

    def _tv(v):
        if v is None: return "-"
        if isinstance(v, bool): return "是" if v else "否"
        return str(v)

    def _nm(v, d=1, sf=""):
        try: v = float(v)
        except: return "-"
        if not _math.isfinite(v): return "-"
        out = f"{v:,.{d}f}".rstrip("0").rstrip(".") if d else f"{v:,.0f}"
        return out + sf

    def _dt(v):
        if not v: return "-"
        try: return _datetime.fromisoformat(str(v).replace("Z","+00:00")).strftime("%Y-%m-%d %H:%M")
        except: return str(v)[:19]

    def _num(v):
        try: v = float(v); return v if _math.isfinite(v) else 0
        except: return 0

    sections = []

    # 1. Instance & Database Overview
    s1 = []
    if inst:
        s1.append({"title": "实例基础信息", "source": "SERVERPROPERTY + sys.dm_os_sys_info", "collection": _coll("实例基础信息"),
            "display": {"headers": ["属性", "值"], "rows": [
                ["服务器名称", inst.get("server_name")], ["机器名", inst.get("machine_name")],
                ["实例名", inst.get("instance_name")], ["产品版本", f"{inst.get('product_version')} {inst.get('product_level')}"],
                ["Edition", inst.get("edition")], ["排序规则", inst.get("collation")],
                ["CPU 核数", inst.get("cpu_count")], ["物理内存", f"{_num(inst.get('physical_memory_mb'))/1024:.1f} GB"],
                ["启动时间", _dt(inst.get("sqlserver_start_time"))],
                ["是否群集", _tv(inst.get("is_clustered"))], ["HADR", _tv(inst.get("is_hadr_enabled"))],
            ]}})
    dbs = s.get("databases", [])
    if dbs:
        s1.append({"title": "数据库清单", "source": "sys.databases + sys.master_files", "collection": _coll("数据库清单与完整性"),
            "display": {"headers": ["数据库", "兼容级别", "恢复模式", "状态", "数据MB", "日志MB", "最后DBCC"],
                "rows": _rows(dbs, ["name","compatibility_level","recovery_model_desc","state_desc","data_size_mb","log_size_mb","last_good_checkdb_time"], {"last_good_checkdb_time": _dt})}})
    dsum = s.get("database_summary", [])
    if dsum:
        ds = dsum[0]
        s1.append({"title": "数据库大小汇总", "source": "sys.master_files 汇总", "collection": _coll("数据库大小汇总"),
            "display": {"headers": ["指标", "值"], "rows": [
                ["用户数据库数", ds.get("user_db_count")], ["总数据", f"{_num(ds.get('total_data_mb')):,.0f} MB"],
                ["总日志", f"{_num(ds.get('total_log_mb')):,.0f} MB"], ["总计", f"{_num(ds.get('total_size_mb')):,.0f} MB"]]}})
    sections.append({"section_id": "instance", "title": "实例与数据库概览", "items": s1, "chart_ids": ["06_database_size"]})

    # 2. Backup & Recovery
    s2 = []
    backs = s.get("backups", [])
    if backs:
        s2.append({"title": "备份状态", "source": "msdb.dbo.backupset", "collection": _coll("备份状态"),
            "display": {"headers": ["数据库", "恢复模式", "最后完整备份", "最后差异", "最后日志", "大小MB"],
                "rows": _rows(backs, ["database_name","recovery_model_desc","last_full_backup","last_diff_backup","last_log_backup","full_backup_size_mb"], {"last_full_backup":_dt,"last_diff_backup":_dt,"last_log_backup":_dt})}})
    bhist = s.get("backup_history", [])
    if bhist:
        s2.append({"title": "近7天备份历史", "source": "msdb.dbo.backupset", "collection": _coll("备份历史（近7天）"),
            "display": {"headers": ["数据库", "类型", "完成时间", "大小MB", "CHECKSUM"],
                "rows": _rows(bhist[:30], ["database_name","type","backup_finish_date","backup_size_mb","has_backup_checksums"], {"backup_finish_date":_dt})}})
    nobk = s.get("no_backup_databases", [])
    if nobk:
        s2.append({"title": "无备份数据库", "source": "sys.databases - backupset 对比", "collection": _coll("无备份数据库"),
            "display": {"headers": ["数据库", "恢复模式", "创建时间"], "rows": _rows(nobk, ["database_name","recovery_model_desc","create_date"], {"create_date":_dt})}})
    sections.append({"section_id": "backup", "title": "备份与恢复", "items": s2, "chart_ids": ["09_backup_age"]})

    # 3. Memory & Buffer Pool
    s3 = []
    bp = s.get("buffer_pool", [])
    if bp:
        s3.append({"title": "缓冲池分布", "source": "sys.dm_os_buffer_descriptors", "collection": _coll("缓冲池使用分布"),
            "display": {"headers": ["数据库", "缓存MB", "占比"], "rows": [[r.get("database_name"), _nm(r.get("cache_size_mb"),0), f"{_num(r.get('cache_pct')):.1f}%"] for r in bp[:15]]}})
    mem = s.get("memory_status", [])
    if mem:
        m = mem[0]
        s3.append({"title": "进程内存状态", "source": "sys.dm_os_process_memory", "collection": _coll("进程内存状态"),
            "display": {"headers": ["指标", "值", "说明"], "rows": [
                ["物理内存", f"{_num(m.get('physical_memory_used_mb')):,.0f} MB", "进程占用"],
                ["锁定页", f"{_num(m.get('locked_page_mb')):,.0f} MB", "LPIM"],
                ["内存利用率", f"{_num(m.get('memory_utilization_percentage')):.1f}%", "分配/目标"],
                ["物理内存不足", _tv(m.get("process_physical_memory_low")), "OS信号"],
                ["虚拟内存不足", _tv(m.get("process_virtual_memory_low")), "OS信号"]]}})
    cpu = s.get("cpu_by_database", [])
    if cpu:
        s3.append({"title": "CPU 按数据库分布", "source": "sys.dm_exec_query_stats 聚合", "collection": _coll("CPU按数据库分布"),
            "display": {"headers": ["数据库", "总CPUms", "执行次数", "逻辑读"],
                "rows": [[r.get("database_name"), _nm(r.get("total_cpu_ms"),0), r.get("execution_count"), _nm(r.get("total_logical_reads"),0)] for r in cpu[:15]]}})
    sections.append({"section_id": "memory", "title": "内存、缓冲池与CPU", "items": s3})

    # 4. Capacity & Storage
    s4 = []
    vols = s.get("volumes", [])
    if vols:
        s4.append({"title": "存储卷容量", "source": "sys.dm_os_volume_stats", "collection": _coll("存储卷容量"),
            "display": {"headers": ["挂载点", "卷名", "文件系统", "总量MB", "可用MB", "可用率"],
                "rows": [[v.get("volume_mount_point"), v.get("logical_volume_name"), v.get("file_system_type"),
                    _nm(v.get("total_mb"),0), _nm(v.get("available_mb"),0), f"{_num(v.get('free_pct')):.1f}%"] for v in vols]}})
    lsp = s.get("log_space", [])
    if lsp:
        s4.append({"title": "事务日志空间", "source": "DBCC SQLPERF(LOGSPACE)", "collection": _coll("事务日志空间"),
            "display": {"headers": ["数据库", "日志MB", "使用率"],
                "rows": [[r.get("database_name"), _nm(r.get("log_size_mb"),0), f"{_num(r.get('log_used_pct')):.1f}%"] for r in lsp]}})
    vlf = s.get("vlf_summary", [])
    if vlf:
        s4.append({"title": "VLF 摘要", "source": "sys.dm_db_log_info", "collection": _coll("VLF摘要"),
            "display": {"headers": ["数据库", "VLF总数", "活动VLF"], "rows": _rows(vlf, ["database_name","vlf_count","active_vlf_count"])}})
    files = s.get("database_files", [])
    if files:
        s4.append({"title": "数据库文件与IO", "source": "sys.master_files + dm_io_virtual_file_stats", "collection": _coll("数据库文件与IO"),
            "display": {"headers": ["数据库/文件", "类型", "大小MB", "增长", "读延迟ms", "写延迟ms"],
                "rows": [[f"{f.get('database_name')}/{f.get('logical_name')}", f.get("type_desc"),
                    _nm(f.get("size_mb"),0), f.get("growth_value"), _nm(f.get("avg_read_latency_ms"),1), _nm(f.get("avg_write_latency_ms"),1)] for f in files[:40]]}})
    sections.append({"section_id": "capacity", "title": "容量、存储与文件", "items": s4, "chart_ids": ["08_volume_free", "07_file_latency"]})

    # 5. Performance Baseline
    s5 = []
    if perf.get("sample_count", 0) > 0:
        s5.append({"title": "性能采样", "source": "sys.dm_os_performance_counters 多点采样", "collection": {"status":"ok","row_count":perf.get("sample_count")},
            "display": {"headers": ["指标", "结果", "说明"], "rows": [
                ["Batch Req/s 峰值", _nm(perf.get("batch_requests_peak"),1), "吞吐"],
                ["Trans/s 峰值", _nm(perf.get("transactions_peak"),1), "事务"],
                ["PLE 最小", f"{_num(perf.get('ple_min')):.0f} s", "页生命周期"],
                ["Memory Grants Pending 峰值", _nm(perf.get("memory_grants_pending_peak"),0), "内存授权"],
                ["连接峰值", _nm(perf.get("connections_peak"),0), "用户连接"],
                ["阻塞峰值", _nm(perf.get("blocked_sessions_peak"),0), "阻塞会话"]]}})
    sections.append({"section_id": "perf_baseline", "title": "性能基线", "items": s5, "chart_ids": ["01_activity","02_memory","03_connections"]})

    # 6. Waits, Blocking, Sessions
    s6 = []
    waits = s.get("wait_stats", [])
    if waits:
        s6.append({"title": "主要等待", "source": "sys.dm_os_wait_stats", "collection": _coll("等待统计"),
            "display": {"headers": ["等待类型", "等待ms", "平均ms", "次数"],
                "rows": [[w.get("wait_type"), _nm(w.get("wait_time_ms"),0), _nm(w.get("avg_wait_ms"),1), _nm(w.get("waiting_tasks_count"),0)] for w in waits[:20]]}})
    blk = s.get("blocking", [])
    if blk:
        s6.append({"title": "活动阻塞", "source": "sys.dm_exec_requests", "collection": _coll("阻塞链"),
            "display": {"headers": ["SPID", "阻塞源", "数据库", "等待s", "类型"],
                "rows": [[b.get("session_id"), b.get("blocking_session_id"), b.get("database_name"), _nm(b.get("wait_seconds"),0), b.get("wait_type")] for b in blk]}})
    active = s.get("active_requests", [])
    if active:
        s6.append({"title": "活动请求", "source": "sys.dm_exec_requests TOP50", "collection": _coll("活动请求"),
            "display": {"headers": ["SPID", "数据库", "状态", "耗时s", "CPUs"],
                "rows": [[a.get("session_id"), a.get("database_name"), a.get("status"), _nm(a.get("elapsed_seconds"),0), _nm(a.get("cpu_time"),0)] for a in active[:20]]}})
    conn = s.get("connection_stats", [])
    if conn:
        s6.append({"title": "连接数统计", "source": "sys.dm_exec_sessions 分组", "collection": _coll("连接数统计"),
            "display": {"headers": ["IP", "主机", "程序", "登录名", "连接数"],
                "rows": _rows(conn[:20], ["client_ip","host_name","program_name","login_name","connection_count"])}})
    sections.append({"section_id": "waits", "title": "等待、阻塞与会话", "items": s6, "chart_ids": ["05_waits"]})

    # 7. TempDB
    s7 = []
    if tdb and tdb.get("total_mb"):
        s7.append({"title": "TempDB 空间", "source": "tempdb.sys.dm_db_file_space_usage", "collection": _coll("TempDB"),
            "display": {"headers": ["指标", "值"], "rows": [
                ["总空间", f"{_num(tdb.get('total_mb')):,.0f} MB"], ["已用", f"{_num(tdb.get('used_mb')):,.0f} MB"],
                ["使用率", f"{_num(tdb.get('used_pct')):.1f}%"], ["文件数", len(tdb.get("files",[]))]]}})
    sections.append({"section_id": "tempdb", "title": "TempDB", "items": s7})

    # 8. SQL & Indexes
    s8 = []
    tq = s.get("top_queries", [])
    if tq:
        s8.append({"title": "高消耗SQL(CPU)", "source": "sys.dm_exec_query_stats", "collection": _coll("高消耗SQL"),
            "display": {"headers": ["数据库", "执行次数", "总CPUms", "平均逻辑读"],
                "rows": [[q.get("database_name"), q.get("execution_count"), _nm(q.get("total_cpu_ms"),0), _nm(q.get("avg_logical_reads"),0)] for q in tq[:15]]}})
    tqlr = s.get("top_queries_logical_reads", [])
    if tqlr:
        s8.append({"title": "高逻辑读SQL", "source": "sys.dm_exec_query_stats", "collection": _coll("高逻辑读SQL"),
            "display": {"headers": ["数据库", "执行次数", "总逻辑读", "平均逻辑读"],
                "rows": [[q.get("database_name"), q.get("execution_count"), _nm(q.get("total_logical_reads"),0), _nm(q.get("avg_logical_reads"),0)] for q in tqlr[:15]]}})
    mi = s.get("missing_indexes", [])
    if mi:
        s8.append({"title": "缺失索引候选", "source": "sys.dm_db_missing_index_*", "collection": _coll("缺失索引建议"),
            "display": {"headers": ["数据库", "对象", "改善分", "键列", "包含列"],
                "rows": [[x.get("database_name"), f"{x.get('schema_name')}.{x.get('table_name')}", _nm(x.get("improvement_score"),0),
                    str(x.get("equality_columns") or "-")[:80], str(x.get("included_columns") or "-")[:80]] for x in mi[:20]]}})
    ui = s.get("unused_indexes", [])
    if ui:
        s8.append({"title": "未使用索引", "source": "sys.dm_db_index_usage_stats", "collection": _coll("未使用索引"),
            "display": {"headers": ["数据库", "对象", "索引", "更新次数"],
                "rows": [[x.get("database_name"), f"{x.get('schema_name')}.{x.get('table_name')}", x.get("index_name"), _nm(x.get("user_updates"),0)] for x in ui[:20]]}})
    sections.append({"section_id": "sql_index", "title": "SQL 与索引", "items": s8})

    # 9. Database Objects
    s9 = []
    nopk = s.get("tables_without_pk", [])
    if nopk:
        s9.append({"title": "无主键表", "source": "INFORMATION_SCHEMA 遍历", "collection": _coll("无主键表（深度）"),
            "display": {"headers": ["数据库", "无主键表数"], "rows": _rows(nopk, ["database_name","table_count"])}})
    lgt = s.get("large_tables", [])
    if lgt:
        s9.append({"title": "大表统计", "source": "sys.tables + partitions 遍历", "collection": _coll("大表统计（深度）"),
            "display": {"headers": ["数据库", "表", "行数", "空间MB"],
                "rows": [[t.get("database_name"), f"{t.get('schema_name')}.{t.get('table_name')}", _nm(t.get("row_count"),0), _nm(t.get("total_mb"),0)] for t in lgt[:30]]}})
    rcsi = s.get("database_rcsi", [])
    if rcsi:
        s9.append({"title": "快照隔离", "source": "sys.databases RCSI/SI", "collection": _coll("数据库RCSI状态"),
            "display": {"headers": ["数据库", "RCSI", "Snapshot Isolation"],
                "rows": [[r.get("database_name"), _tv(r.get("RCSI_enabled")), str(r.get("SI_state","-"))] for r in rcsi]}})
    sections.append({"section_id": "db_objects", "title": "数据库对象", "items": s9})

    # 10. Security
    s10 = []
    sc = sec.get("security_checks", [])
    if sc:
        s10.append({"title": "安全检查", "source": "综合安全检测", "collection": _coll("安全与权限"),
            "display": {"headers": ["检查项", "结果", "详情"],
                "rows": [[c.get("check_name"), "通过" if c.get("result")=="PASS" else "未通过", c.get("detail","-")] for c in sc]}})
    logins = sec.get("sql_logins", [])
    if logins:
        s10.append({"title": "SQL登录", "source": "sys.sql_logins", "collection": {"status":"ok","row_count":len(logins)},
            "display": {"headers": ["登录名", "类型", "状态", "默认库"],
                "rows": _rows(logins[:50], ["name","type_desc","is_disabled","default_database_name"])}})
    sysadm = sec.get("sysadmin_members", [])
    if sysadm:
        s10.append({"title": "sysadmin成员", "source": "sys.server_role_members", "collection": {"status":"ok","row_count":len(sysadm)},
            "display": {"headers": ["成员", "类型", "状态"],
                "rows": [[m.get("name"), m.get("type_desc"), "禁用" if m.get("is_disabled") else "启用"] for m in sysadm]}})
    sections.append({"section_id": "security", "title": "安全与身份", "items": s10})

    # 11. High Availability
    s11 = []
    ag = s.get("availability_replicas", [])
    if ag:
        s11.append({"title": "AlwaysOn可用性组", "source": "sys.dm_hadr_*", "collection": _coll("Always On"),
            "display": {"headers": ["AG", "副本", "数据库", "角色", "同步", "健康"],
                "rows": [[a.get("availability_group"), a.get("replica_server_name"), a.get("database_name"),
                    a.get("role_desc"), a.get("synchronization_state_desc"), a.get("synchronization_health_desc")] for a in ag]}})
    mirrors = s.get("database_mirroring", [])
    if mirrors:
        s11.append({"title": "数据库镜像", "source": "sys.database_mirroring", "collection": _coll("数据库镜像"),
            "display": {"headers": ["数据库", "角色", "状态", "安全级别"],
                "rows": _rows(mirrors, ["database_name","mirroring_role_desc","mirroring_state_desc","mirroring_safety_level_desc"])}})
    lship = s.get("log_shipping", [])
    if lship:
        s11.append({"title": "日志传送", "source": "msdb.dbo.log_shipping_monitor_*", "collection": _coll("日志传送"),
            "display": {"headers": ["角色", "数据库", "延迟分钟", "状态"],
                "rows": _rows(lship, ["monitor_role","database_name","minutes_since_last_action","status"])}})
    repl = s.get("replication", [])
    if repl:
        s11.append({"title": "复制配置", "source": "msdb.dbo.MSpublications/MSsubscriptions", "collection": _coll("复制配置"),
            "display": {"headers": ["发布名", "类型", "状态", "订阅服务器", "订阅库"],
                "rows": [[r.get("publication_name",""), r.get("type_desc","-"), r.get("status_desc","-"),
                    r.get("subscriber_server","-"), r.get("subscriber_db","-")] for r in repl]}})
    sections.append({"section_id": "ha", "title": "高可用与复制", "items": s11})

    # 12. Jobs & Logs
    s12 = []
    fj = s.get("failed_jobs", [])
    if fj:
        s12.append({"title": "失败作业(近24h)", "source": "msdb.dbo.sysjobhistory", "collection": _coll("SQL Agent失败作业"),
            "display": {"headers": ["作业", "失败时间", "消息"],
                "rows": [[j.get("job_name"), _dt(j.get("run_datetime")), str(j.get("message") or "-")[:200]] for j in fj]}})
    all_j = s.get("agent_jobs", [])
    if all_j:
        s12.append({"title": "作业清单", "source": "msdb.dbo.sysjobs", "collection": _coll("SQL Agent作业清单"),
            "display": {"headers": ["作业", "启用", "最近结果", "最近运行"],
                "rows": [[j.get("job_name"), _tv(j.get("enabled")), j.get("last_run_status_desc","-"), _dt(j.get("last_run_datetime"))] for j in all_j[:50]]}})
    errs = s.get("error_log_summary", [])
    if errs:
        s12.append({"title": "错误日志", "source": "xp_readerrorlog", "collection": _coll("错误日志摘要"),
            "display": {"headers": ["日期", "Severity", "次数", "示例"],
                "rows": [[e.get("event_date"), e.get("severity"), _nm(e.get("event_count"),0), str(e.get("sample_message") or "-")[:160]] for e in errs[:20]]}})
    sections.append({"section_id": "ops", "title": "作业与错误日志", "items": s12})

    # 13. Configuration
    cfg = s.get("configurations", [])
    if cfg:
        sections.append({"section_id": "config", "title": "实例关键配置", "items": [
            {"title": "关键配置项", "source": "sys.configurations", "collection": _coll("实例配置"),
                "display": {"headers": ["配置项", "值", "最小", "最大", "动态"],
                    "rows": [[c.get("name"), c.get("value_in_use"), c.get("minimum"), c.get("maximum"), _tv(c.get("is_dynamic"))] for c in cfg]}}]})

    return sections

def _inject_analysis(sections, findings):
    if not findings:
        return sections
    by_prefix = {}
    for f_ in findings:
        rid = str(f_.get('rule_id', ''))
        by_prefix.setdefault(rid.upper(), []).append(f_)
    _map = {
        ('instance', '数据库清单'): ['DB.CHECKDB','DB.STATE','DB.AUTO_OPTIONS','DB.TRUSTWORTHY','DB.QUERY_STORE','DB.PAGE_VERIFY'],
        ('backup', '备份状态'): ['BACKUP.FULL','BACKUP.LOG','BACKUP.CHECKSUM'],
        ('backup', '无备份'): ['BACKUP.NO_BACKUP'],
        ('memory', '缓冲池'): ['MEM.CACHE_IMBALANCE'],
        ('memory', '内存'): ['MEM.SIGNALING','MEM.PRESSURE'],
        ('capacity', '存储卷'): ['STORAGE.FREE_SPACE'],
        ('capacity', '日志'): ['LOG.SPACE'],
        ('capacity', 'VLF'): ['LOG.VLF'],
        ('capacity', '文件'): ['FILE.IO_LATENCY','FILE.PERCENT_GROWTH'],
        ('perf', '性能'): ['PERF.PLE','PERF.MEMORY_GRANTS'],
        ('waits', '阻塞'): ['PERF.BLOCKING'],
        ('waits', '活动'): ['PERF.LONG_REQUEST'],
        ('waits', '连接'): ['PERF.CONNECTION_FLOOD'],
        ('tempdb', 'TempDB'): ['TEMPDB.USAGE','TEMPDB.FILE_BALANCE'],
        ('db_objects', '无主键'): ['OBJ.NO_PK'],
        ('db_objects', '大表'): ['OBJ.LARGE_TABLE'],
        ('db_objects', '快照'): ['DB.RCSI'],
        ('security', '安全'): ['SEC.SA_ACCOUNT_DISABLED','SEC.EMPTY_PASSWORD_LOGINS','SEC.XP_CMDSHELL_ENABLED','SEC.OLE_AUTOMATION_ENABLED'],
        ('security', 'SQL登录'): ['SEC.SA_ENABLED'],
        ('security', 'sysadmin'): ['SEC.SYSADMIN_COUNT'],
        ('ha', 'Always'): ['HA.AG_HEALTH','HA.AG_QUEUE'],
        ('ha', '镜像'): ['HA.MIRRORING'],
        ('ha', '日志传送'): ['HA.LOG_SHIPPING'],
        ('ha', '复制'): ['HA.REPLICATION'],
        ('ops', '失败'): ['AGENT.JOB_FAILED'],
        ('ops', '作业'): ['AGENT.JOB_DISABLED'],
        ('ops', '错误'): ['ERRORLOG.SEVERE'],
        ('config', '配置'): ['INSTANCE.MAX_MEMORY','INSTANCE.MAXDOP','INSTANCE.COST_THRESHOLD'],
    }
    for section in sections:
        sid = str(section.get('section_id', ''))
        for item in section.get('items', []):
            title = str(item.get('title', ''))
            matched = []
            for (sk, tk), prefixes in _map.items():
                if sk in sid and tk in title:
                    for p in prefixes:
                        matched.extend(by_prefix.get(p, []))
            seen = set(); unique = []
            for ff in matched:
                if ff['rule_id'] not in seen:
                    seen.add(ff['rule_id']); unique.append(ff)
            if not unique:
                continue
            severities = {ff.get('severity','medium') for ff in unique}
            status = 'risk' if severities & {'critical','high'} else 'attention' if 'medium' in severities else 'normal'
            item['analysis'] = {
                'status': status,
                'conclusion': '；'.join(f"{ff['title']}（{ff['object_name']}）" for ff in unique[:3]),
                'evidence': [ff.get('evidence','') for ff in unique[:3] if ff.get('evidence')],
                'recommendation': '；'.join(ff.get('recommendation','') for ff in unique[:3] if ff.get('recommendation')),
            }
    return sections


def main() -> int:
    p = argparse.ArgumentParser(description="Analyze SQL Server inspection package")
    p.add_argument("input", help="snapshot.json, extracted directory, or zip package")
    p.add_argument("--output", default="analysis_output")
    p.add_argument("--rules-config", default=str(Path(__file__).with_name("inspection_rules.json")))
    args = p.parse_args()
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="sqlserver_inspection_") as tmp:
        path = discover_snapshot(Path(args.input).resolve(), Path(tmp))
        snapshot = normalize_snapshot(read_json(path))
    config = read_json(Path(args.rules_config).resolve())
    ignored_waits = {str(x).upper() for x in config.get("ignored_wait_types", [])}
    snapshot["wait_stats"] = [x for x in snapshot.get("wait_stats", []) if str(x.get("wait_type", "")).upper() not in ignored_waits]
    collected = snapshot.get("collection", {}).get("finished_at") or datetime.now().astimezone().isoformat()
    try:
        now = datetime.fromisoformat(str(collected).replace("Z", "+00:00"))
    except ValueError:
        now = datetime.now().astimezone()
    findings = RuleEngine(config, now).evaluate(snapshot)
    charts = build_charts(snapshot, findings, output / "charts")
    model = build_model(snapshot, findings, charts)
    write_json(output / "analysis.json", {"snapshot": snapshot, "findings": findings})
    write_json(output / "report_model.json", model)
    lines = [f"SQL Server 巡检分析完成", f"健康评分: {model['health']['score']} ({model['health']['grade']})", f"发现项: {len(findings)}", f"采集完整度: {model['collection_quality']['score_pct']}%"]
    (output / "analysis_summary.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    print(f"报告模型: {output / 'report_model.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
