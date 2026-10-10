"""SQL Server ???????"""

from __future__ import annotations

from collections import Counter
from datetime import datetime
from typing import Any

from .metrics import num, score, SEVERITY_WEIGHTS
from .analysis_text import decorate_sections, pending_confirmations


ANALYZER_VERSION = "1.1.0"


# 采集状态文案：与 inspection_core/word_engine.py 的 display_status 用同一套译名。
# 同一个状态会在报告的「条目采集标记」与「采集执行明细」两处出现，译名不一致会被读成两回事。
# 这里不 import 那个函数：word_engine 顶层 import python-docx，分析层不该拖这个依赖。
_COLLECTION_STATUS_CN = {
    "ok": "正常", "success": "成功", "success_no_data": "成功（无数据）", "failed": "失败",
    "partial": "部分可用", "empty": "无记录", "not_applicable": "不适用", "not_evaluated": "未判断",
    "skipped": "已跳过", "unsupported": "不支持", "permission_denied": "权限不足",
    "timeout": "超时", "error": "失败", "not_enabled": "未启用",
}


def build_model(snapshot: dict[str, Any], findings: list[dict[str, Any]], charts: list[dict[str, str]],
                 evaluations: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    status = snapshot.get("collection", {}).get("module_status", [])
    # 采集完整度只统计「应当有结论」的模块。skipped / not_applicable 是明确的不采集或本实例
    # 不具备该特性，算进分母会把「OS 计数器在远程目标下有意跳过」误报成采集不完整；
    # success_no_data（SQL 跑通但结果为空）算成功 —— 空结果不等于失败，更不等于无风险。
    evaluable = [x for x in status if str(x.get("status", "")).lower() not in ("skipped", "not_applicable")]
    ok = sum(1 for x in evaluable if str(x.get("status", "")).lower() in ("success", "success_no_data"))
    quality = round(ok * 100 / len(evaluable), 1) if evaluable else 100.0
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
    # CPU 调度压力（runnable / work_queue / pending_disk_io）只有采集器 v1.3.2 起才在采样点里带。
    # 老包的这几个键不存在，峰值必须落 None 表示"未采集" —— 落 0 会被读成"CPU 一点都不忙"，
    # 那正是 AGENTS.md 禁止的"把未知写成 0"。
    def _sched_peak(key):
        vals = [num(x.get(key)) or 0 for x in perf_samples if x.get(key) is not None]
        return max(vals) if vals else None

    perf_summary = {
        "sample_count": len(perf_samples),
        "batch_requests_peak": max([num(x.get("batch_requests_per_sec")) or 0 for x in perf_samples] or [0]),
        "transactions_peak": max([num(x.get("transactions_per_sec")) or 0 for x in perf_samples] or [0]),
        "ple_min": min([num(x.get("page_life_expectancy")) for x in perf_samples if num(x.get("page_life_expectancy")) is not None] or [0]),
        "memory_grants_pending_peak": max([num(x.get("memory_grants_pending")) or 0 for x in perf_samples] or [0]),
        "connections_peak": max([num(x.get("user_connections")) or 0 for x in perf_samples] or [0]),
        "blocked_sessions_peak": max([num(x.get("blocked_session_count")) or 0 for x in perf_samples] or [0]),
        "runnable_tasks_peak": _sched_peak("runnable_tasks_count"),
        "runnable_tasks_max_peak": _sched_peak("runnable_tasks_max"),
        "work_queue_peak": _sched_peak("work_queue_count"),
        "pending_io_peak": _sched_peak("pending_disk_io_count")
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
            # 共享引擎（word_engine.build_summary_v31）读的键名是 "counts" 而不是
            # "severity_counts"；缺了它，首页「结论速览」里的项数会全部印成 0。
            "counts": dict(counts),
            # 缺了 scoring_policy，引擎会整段跳过「结论速览」—— 首页只剩两张小表，
            # 读者看到「0 / 100」无从分辨那是扣分触底还是实例真的不可用。
            # 权重取自 metrics.SEVERITY_WEIGHTS，与评分函数同源，避免两处漂移。
            "scoring_policy": {
                "base": 100,
                "penalty_per_finding": dict(SEVERITY_WEIGHTS),
                "minimum": 0,
            },
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
                            "no_backup_databases": snapshot.get("no_backup_databases", []), "suspect_pages": snapshot.get("suspect_pages", []),
                            "restore_history": snapshot.get("restore_history", [])},
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
            "job_steps": snapshot.get("job_steps", []),
            "error_log_summary": snapshot.get("error_log_summary", [])
        },
        "risk_register": risks,
        # 规则求值（四态）是附录「规则执行状态」的数据源，必须在这一步就写进
        # report_model：generate_report 只拿得到 report_model、拿不到 snapshot，
        # 到那一步再算就没有依据了（旧实现正是只能在 adapter 里由 findings 反推，
        # 于是「通过/未判断/不适用」三行在结构上恒为 0）。
        "rule_evaluations": list(evaluations or []),
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
            "风险登记册", "整改计划", "综合结论", "采集缺口与附录", "采集执行明细"
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
            "orphaned_users": snapshot.get("orphaned_users", [])[:50],
            "restore_history": snapshot.get("restore_history", []), "job_steps": snapshot.get("job_steps", [])[:100]
        },
        "charts": charts,
        "methodology": [
            "结论由本地确定性规则生成；未采集数据不会判定为正常。",
            "等待统计、文件 I/O 和计划缓存指标为实例启动/统计清零后的累计视图，需结合业务时段解释。",
            "备份合规不仅看时间，还应通过定期还原验证证明可恢复性。",
            "所有整改建议均需完成影响评估、测试验证、备份与变更审批。"
        ],
        "sections": _inject_analysis(_build_sections(snapshot, perf_summary), findings),
        # 待客户确认事项由已触发规则派生（模板见 analysis_text.CONFIRMATION_TEMPLATES）。
        # 引擎对「模型没给这个键」与「给了但是空清单」的渲染不同：前者只能声明"未产出清单"，
        # 后者才敢写"本次没有待确认事项" —— 延续「未采集 ≠ 0」，所以这个键必须出现。
        "pending_confirmations": pending_confirmations(findings, instance),
    }

def _blank_note(collection, confirmed: str) -> str:
    """空数据检查项的说明文案。

    「采集成功、确认没有」与「没采到、无法判定」是完全相反的两个结论，不能让它们
    在报告里长得一样 —— 写「已检查全部用户库、未发现候选」而实际是深度检查根本
    没启用，那就是一句客户会当真的假话。这正是「未采集 ≠ 0」要防的事。
    """
    if str((collection or {}).get("status")) == "success_no_data":
        return confirmed
    return "本项未完成评价（未采集或采集不完整），不能据此判定为没有问题。"


def _build_sections(s: dict[str, Any], perf: dict[str, Any]) -> list[dict[str, Any]]:
    """Build data-driven inspection sections for the docx generator."""
    import math as _math
    from datetime import datetime as _datetime
    inst = s.get("instance", {})
    tdb = s.get("tempdb", {})
    sec = s.get("security", {})
    mods = s.get("collection", {}).get("module_status", [])
    mod_ok = {str(m.get("name", "")): m for m in mods}

    def _coll(*names):
        # 允许同时给新旧模块名：采集端去掉过两个模块名的「（深度）」后缀，旧采集包仍用旧名。
        # 改名不应该改变历史包的解读结果，所以按顺序回退，而不是硬匹配一个名字。
        for name in names:
            m = mod_ok.get(name)
            if m:
                return {"status": m.get("status", "ok"), "row_count": m.get("row_count", 0)}
        # 找不到模块名不能算「达标」：这通常意味着采集端改了模块名而这里没跟。
        # 回退成 ok 会把「模块缺失」渲染成「达标 0 行」，落 not_evaluated 才暴露得出来。
        return {"status": "not_evaluated", "row_count": 0}

    def _cst(*names):
        """只取「采集是否成功」的标记，行数由调用方按自己那份数据算。

        安全块是一个 try 包住 sql_logins / sysadmin_members 等多类查询，module_status 里
        只有一条「安全与权限」—— 状态可以共用，行数不能共用，否则 sysadmin 条目会显示登录名的行数。
        """
        return _coll(*names).get("status", "ok")

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

    def _hhmmss(v):
        """msdb.dbo.sysjobhistory.run_duration 是 HHMMSS 整数，**不是秒数**。

        直接把它当秒渲染会夸大两个数量级（738 秒会被写成 7m38s 之外的 738s 之外的 7 小时）。
        实测报告里 Auto Fee 是 1253 秒（约 20.9 分钟），对应 run_duration = 205300。
        """
        if v is None:
            return "-"
        try:
            n = int(str(v).strip() or 0)
        except (TypeError, ValueError):
            return "-"
        if n <= 0:
            return "-"
        h, m, s = n // 10000, (n // 100) % 100, n % 100
        if h:
            return f"{h}h{m:02d}m{s:02d}s"
        if m:
            return f"{m}m{s:02d}s"
        return f"{s}s"

    def _num(v):
        try: v = float(v); return v if _math.isfinite(v) else 0
        except: return 0

    def _tempdb_io_rows(scope):
        """tempdb 的累计读写次数 / 延迟。

        报告 1.5 用 tempdb 的读写次数来解释业务库的 tempdb 依赖强度，而旧版 TempDB 章节
        只有空间和使用率 —— 数据其实一直在 database_files 里（含 tempdb 行），只是没人读。
        """
        files = [f for f in scope.get("database_files", [])
                 if str(f.get("database_name", "")).lower() == "tempdb"]
        if not files:
            return []
        reads = sum((_num(f.get("num_of_reads")) or 0) for f in files)
        writes = sum((_num(f.get("num_of_writes")) or 0) for f in files)
        lat_r = [(_num(f.get("avg_read_latency_ms")) or 0) for f in files]
        lat_w = [(_num(f.get("avg_write_latency_ms")) or 0) for f in files]
        return [["累计读次数", _nm(reads, 0)], ["累计写次数", _nm(writes, 0)],
                ["读/写平均延迟", f"{max(lat_r):.1f} / {max(lat_w):.1f} ms（取最慢文件）"]]

    sections = []

    # 分节标记用 section_id 而不是序号：渲染顺序由 word_report 的
    # SQLSERVER_WORD_PROFILE.section_order 决定，本函数的 append 顺序只关系到
    # 「空章节是否跳过」，与章号无关。（此处原写「# 13. Configuration」——
    # config 上移到第 4 章后那种序号注释就当场变成错的了。）
    # instance — 实例与数据库概览
    s1 = []
    if inst:
        s1.append({"title": "实例基础信息", "source": "SERVERPROPERTY + sys.dm_os_sys_info", "collection": _coll("实例基础信息"),
            "display": {"headers": ["属性", "值"], "rows": [
                ["服务器名称", inst.get("server_name")], ["机器名", inst.get("machine_name")],
                ["实例名", inst.get("instance_name")], ["产品版本", f"{inst.get('product_version')} {inst.get('product_level')}"],
                ["Edition", inst.get("edition")], ["排序规则", inst.get("collation")],
                ["CPU 核数", inst.get("cpu_count")], ["物理内存", f"{_num(inst.get('physical_memory_mb'))/1024:.1f} GB"],
                ["启动时间", _dt(inst.get("sqlserver_start_time"))],
                # 物理插槽 x 每插槽核数 x NUMA 节点必须并排给出：现场报告 1.1 写「CPU数量 64」
                # 而说明里又写「服务器CPU个数为16」，两个口径混在一栏，读者无法判断 MAXDOP 该取几。
                ["物理CPU/每插槽核数", f"{_nm(inst.get('socket_count'),0)} × {_nm(inst.get('cores_per_socket'),0)}"],
                ["NUMA 节点数", _nm(inst.get("numa_node_count"),0)],
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
                ["总日志", f"{_num(ds.get('total_log_mb')):,.0f} MB"], ["总计", f"{_num(ds.get('total_size_mb')):,.0f} MB"],
                ["实例总数", ds.get("database_count_all")], ["日志文件数", ds.get("log_file_count")]]}})
    svcs = s.get("server_services", [])
    if svcs:
        # 标题原为「服务与启动参数」，但 dm_server_services 只给服务清单（服务名 / 账号 /
        # 启动类型 / 状态 / 程序路径 / 上次启动），一个启动参数都没有 —— 读者会在这里找
        # max server memory，找到的却是 Automatic。真正的启动参数在「实例关键配置」章
        # （sys.configurations）。collection 仍用采集端模块名，那是 collector 契约。
        s1.append({"title": "服务运行状态", "source": "sys.dm_server_services", "collection": _coll("SQL Server 服务与启动参数"),
            "display": {"headers": ["服务", "账号", "启动类型", "状态", "程序路径", "上次启动"],
                "rows": [[v.get("servicename"), v.get("service_account"),
                    v.get("startup_type_desc"), v.get("status_desc"), v.get("filename"),
                    _dt(v.get("last_startup_time"))] for v in svcs]}})
    osm = s.get("os_metrics", {}) or {}
    osid = osm.get("identity") or {}
    if osid:
        s1.append({"title": "操作系统与主机", "source": "os_metrics（仅目标为本机时采集）", "collection": _coll("OS 指标"),
            "display": {"headers": ["项目", "值"], "rows": [
                ["操作系统", f"{osid.get('os_caption') or '-'} {osid.get('os_version') or ''} (Build {osid.get('os_build') or '-'})"],
                ["架构", osid.get("os_architecture")],
                ["厂商/型号", f"{osid.get('manufacturer') or '-'} {osid.get('model') or ''}"],
                ["逻辑处理器", osid.get("logical_processors")],
                ["可见内存", f"{_num(osid.get('total_visible_memory_mb'))/1024:.1f} GB"],
                ["内存使用率", f"{_num(osid.get('memory_used_pct')):.1f}%"],
                ["物理处理器", osid.get("physical_processors")],
                ["页面文件", f"{_num(osid.get('page_file_total_mb')):,.0f} MB（可用 {_num(osid.get('page_file_free_mb')):,.0f} MB）"],
                ["OS 安装日期", _dt(osid.get("os_install_date"))],
                ["最近启动", _dt(osid.get("last_boot_up_time"))]]}})
    sections.append({"section_id": "instance", "title": "实例与数据库概览", "items": s1, "chart_ids": ["06_database_size"]})

    # backup — 备份与恢复
    s2 = []
    backs = s.get("backups", [])
    if backs:
        s2.append({"title": "备份状态", "source": "msdb.dbo.backupset", "collection": _coll("备份状态"),
            "display": {"headers": ["数据库", "恢复模式", "最后完整备份", "最后差异", "最后日志", "大小MB"],
                "rows": _rows(backs, ["database_name","recovery_model_desc","last_full_backup","last_diff_backup","last_log_backup","full_backup_size_mb"], {"last_full_backup":_dt,"last_diff_backup":_dt,"last_log_backup":_dt})}})
    bhist = s.get("backup_history", [])
    if bhist:
        def _btype(row):
            # copy-only 与快照备份不参与备份链，类型列必须能看出区别，
            # 否则"最近 7 天有备份"会把一条不接链的备份当成链完整。
            label = str(row.get("type") or "-")
            copy_only = row.get("is_copy_only")
            if copy_only is True or str(copy_only or "").upper() in ("YES", "1", "TRUE"):
                label += "/copy-only"
            snapshot = row.get("is_snapshot")
            if snapshot is True or str(snapshot or "") in ("1", "True", "true"):
                label += "/snapshot"
            return label
        s2.append({"title": "近7天备份历史", "source": "msdb.dbo.backupset", "collection": _coll("备份历史（近7天）"),
            "display": {"headers": ["数据库", "类型", "完成时间", "耗时秒", "原始MB", "压缩后MB", "校验和", "加密"],
                "rows": [[r.get("database_name"), _btype(r), _dt(r.get("backup_finish_date")),
                    _nm(r.get("duration_seconds"),0), _nm(r.get("backup_size_mb"),0),
                    _nm(r.get("compressed_size_mb"),0), _tv(r.get("has_backup_checksums")),
                    str(r.get("encryptor_type") or "-")] for r in bhist[:30]]}})
    nobk = s.get("no_backup_databases", [])
    if nobk:
        s2.append({"title": "无备份数据库", "source": "sys.databases - backupset 对比", "collection": _coll("无备份数据库"),
            "display": {"headers": ["数据库", "恢复模式", "创建时间"], "rows": _rows(nobk, ["database_name","recovery_model_desc","create_date"], {"create_date":_dt})}})
    # 可疑页面 = 页级损坏记录，直接影响可恢复性，所以放「备份与恢复」而不是数据库对象章节。
    suspect = s.get("suspect_pages", [])
    s2.append({"title": "可疑页面", "source": "msdb.dbo.suspect_pages", "collection": _coll("可疑页面"),
        "display": {"headers": ["数据库", "文件ID", "页ID", "事件类型", "错误次数", "最后更新"],
            "empty_text": _blank_note(_coll("可疑页面"),
                "已检查 msdb.dbo.suspect_pages，本次未发现可疑或损坏页面。"),
            "rows": [[x.get("database_name"), _nm(x.get("file_id"),0), _nm(x.get("page_id"),0),
                x.get("event_type"), _nm(x.get("error_count"),0), _dt(x.get("last_update_date"))]
                for x in suspect[:30]]}})
    # 还原验证：backupset 只能证明"备份文件存在"，restorehistory 才证明"这台实例还原过"。
    # 「备份存在 ≠ 备份可还原」是备份章节唯一缺的一环；无任何还原记录时本表不出现
    # （不要用 0 表示，报告侧另有 collection_gap 说明）。
    rest = s.get("restore_history", [])
    if rest:
        s2.append({"title": "还原验证历史", "source": "msdb.dbo.restorehistory（RESTORE / VERIFYONLY）",
            "collection": _coll("还原验证历史"),
            "display": {"headers": ["数据库", "还原次数", "最近还原", "最近实际还原", "最近仅验证"],
                "rows": [[x.get("database_name"), _nm(x.get("restore_count"),0), _dt(x.get("last_restore_date")),
                    _dt(x.get("last_applied_restore_date")), _dt(x.get("last_verify_only_date"))]
                    for x in rest[:50]]}})
    sections.append({"section_id": "backup", "title": "备份与恢复", "items": s2, "chart_ids": ["09_backup_age"]})

    # memory — 内存、缓冲池与CPU
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
    clerks = s.get("memory_clerks", [])
    if clerks:
        s3.append({"title": "内存组件分布", "source": "sys.dm_os_memory_clerks", "collection": _coll("内存组件分布"),
            "display": {"headers": ["内存组件", "占用MB", "保留虚拟MB", "提交虚拟MB"],
                "rows": [[r.get("type"), _nm(r.get("pages_mb"),1), _nm(r.get("reserved_mb"),1), _nm(r.get("committed_mb"),1)] for r in clerks[:10]]}})
    smem = s.get("system_memory", [])
    if smem:
        sm0 = smem[0]
        s3.append({"title": "系统内存状态", "source": "sys.dm_os_sys_memory", "collection": _coll("系统内存状态"),
            "display": {"headers": ["项目", "值"], "rows": [
                ["物理内存总量", f"{_num(sm0.get('total_physical_memory_mb')):,.0f} MB"],
                ["可用物理内存", f"{_num(sm0.get('available_physical_memory_mb')):,.0f} MB"],
                ["系统缓存", f"{_num(sm0.get('system_cache_mb')):,.0f} MB"],
                ["内存状态", str(sm0.get("system_memory_state_desc") or "-")]]}})
    sections.append({"section_id": "memory", "title": "内存、缓冲池与CPU", "items": s3})

    # capacity — 容量、存储与文件
    s4 = []
    vols = s.get("volumes", [])
    if vols:
        s4.append({"title": "存储卷容量", "source": "sys.dm_os_volume_stats", "collection": _coll("存储卷容量"),
            # 「来源」列不能省：同一挂载点会分别以 DATABASE_FILE / XP_FIXEDDRIVES / BACKUP_MEDIA
            # 三个来源各出一行，没有这一列会被读成"重复行"。
            "display": {"headers": ["挂载点", "来源", "卷名", "文件系统", "总量MB", "可用MB", "可用率"],
                "rows": [[v.get("volume_mount_point"), v.get("source"), v.get("logical_volume_name"),
                    v.get("file_system_type"),
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
    osdisks = (s.get("os_metrics", {}) or {}).get("physical_disks") or []
    if osdisks:
        s4.append({"title": "物理磁盘介质", "source": "os_metrics（仅目标为本机时采集）", "collection": _coll("OS 指标"),
            "display": {"headers": ["物理盘", "介质类型", "总线", "转速RPM", "容量GB", "健康"],
                "rows": [[d.get("friendly_name"), d.get("media_type"), d.get("bus_type"),
                    _nm(d.get("spindle_speed"),0), _nm(d.get("size_gb"),2),
                    d.get("health_status")] for d in osdisks]}})
    sections.append({"section_id": "capacity", "title": "容量、存储与文件", "items": s4, "chart_ids": ["08_volume_free", "07_file_latency"]})

    # perf_baseline — 性能基线
    s5 = []
    if perf.get("sample_count", 0) > 0:
        # 状态读采集端登记的「性能采样」，不能写死 ok：采样块整体失败时报告仍会显示达标。
        rows5 = [
            ["Batch Req/s 峰值", _nm(perf.get("batch_requests_peak"),1), "吞吐"],
            ["Trans/s 峰值", _nm(perf.get("transactions_peak"),1), "事务"],
            ["PLE 最小", f"{_num(perf.get('ple_min')):.0f} s", "页生命周期"],
            ["Memory Grants Pending 峰值", _nm(perf.get("memory_grants_pending_peak"),0), "内存授权"],
            ["连接峰值", _nm(perf.get("connections_peak"),0), "用户连接"],
            ["阻塞峰值", _nm(perf.get("blocked_sessions_peak"),0), "阻塞会话"]]
        # 只有采集器 v1.3.2 起的包才带 CPU 调度字段；老包这几个键为 None 时**不加行**，
        # 不是补一行 0 —— 「可运行任务 0」和「没采到」是两件事，报告不能混。
        if perf.get("runnable_tasks_peak") is not None:
            rows5.append(["可运行任务峰值", _nm(perf.get("runnable_tasks_peak"),0), "CPU 压力（调度器合计）"])
        if perf.get("work_queue_peak") is not None:
            rows5.append(["工作队列峰值", _nm(perf.get("work_queue_peak"),0), "CPU 压力（调度器合计）"])
        if perf.get("pending_io_peak") is not None:
            rows5.append(["待处理磁盘IO峰值", _nm(perf.get("pending_io_peak"),0), "IO 压力（调度器合计）"])
        s5.append({"title": "性能采样",
            "source": "sys.dm_os_performance_counters + sys.dm_os_schedulers 多点采样",
            "collection": _coll("性能采样"),
            "display": {"headers": ["指标", "结果", "说明"], "rows": rows5}})
        # 峰值说明"最忙到过什么程度"，序列说明"忙了多久、是否持续"—— CPU 压力的直接证据，
        # 此前整份报告没有任何 CPU 时间序列（OS 层序列只在目标为本机时才有）。
        sched_rows = [[_dt(p.get("timestamp")), _nm(p.get("runnable_tasks_count"),0), _nm(p.get("runnable_tasks_max"),0),
                       _nm(p.get("work_queue_count"),0), _nm(p.get("pending_disk_io_count"),0),
                       _nm(p.get("current_tasks_count"),0)]
                      for p in (s.get("performance_samples") or [])
                      if p.get("runnable_tasks_count") is not None]
        if sched_rows:
            s5.append({"title": "CPU 调度压力采样", "source": "sys.dm_os_schedulers（scheduler_id<255，瞬时值）",
                "collection": _coll("性能采样"),
                "display": {"headers": ["采样时刻", "可运行任务", "单调度器最多", "工作队列", "待处理IO", "当前任务"],
                    "rows": sched_rows}})
    osamples = (s.get("os_metrics", {}) or {}).get("samples") or []
    if osamples:
        s5.append({"title": "操作系统采样（CPU 与内存）", "source": "os_metrics（仅目标为本机时采集）", "collection": _coll("OS 指标"),
            "display": {"headers": ["采样时刻", "CPU%", "处理器队列", "可用内存MB", "硬缺页/秒"],
                "rows": [[_dt(p.get("collected_at")), _nm(p.get("cpu_pct"),0), _nm(p.get("processor_queue_length"),0),
                    _nm(p.get("available_mb"),0), _nm(p.get("page_faults_per_sec"),0)] for p in osamples]}})
        # 磁盘与网络在一个采样点里是一对多，展平成逐卷/逐网卡行，时间戳保留以便定位尖峰时段。
        disk_rows = []
        for p in osamples:
            for d in (p.get("disks") or []):
                disk_rows.append([_dt(p.get("collected_at")), d.get("name"), _nm(d.get("pct_disk_time"),0),
                                  _nm(d.get("avg_disk_queue_length"),2), _nm(d.get("reads_per_sec"),0),
                                  _nm(d.get("writes_per_sec"),0)])
        if disk_rows:
            s5.append({"title": "磁盘繁忙率采样", "source": "os_metrics（仅目标为本机时采集）", "collection": _coll("OS 指标"),
                "display": {"headers": ["采样时刻", "卷", "磁盘时间%", "平均队列", "读/秒", "写/秒"],
                    "rows": disk_rows}})
        net_rows = []
        for p in osamples:
            for n in (p.get("network") or []):
                net_rows.append([_dt(p.get("collected_at")), n.get("name"), _nm(n.get("bytes_total_per_sec"),0),
                                 _nm(n.get("bytes_received_per_sec"),0), _nm(n.get("bytes_sent_per_sec"),0),
                                 _nm(n.get("packets_received_errors"),0), _nm(n.get("packets_outbound_errors"),0)])
        if net_rows:
            s5.append({"title": "网络吞吐采样", "source": "os_metrics（仅目标为本机时采集）", "collection": _coll("OS 指标"),
                "display": {"headers": ["采样时刻", "接口", "总字节/秒", "收字节/秒", "发字节/秒", "入错包", "出错包"],
                    "rows": net_rows}})
    sections.append({"section_id": "perf_baseline", "title": "性能基线", "items": s5, "chart_ids": ["01_activity","02_memory","03_connections"]})

    # waits — 等待、阻塞与会话
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
    sess = s.get("session_activity", [])
    if sess:
        s6.append({"title": "会话资源占用", "source": "sys.dm_exec_sessions 按登录名与状态聚合",
            "collection": _coll("会话资源占用"),
            "display": {"headers": ["登录名", "状态", "会话数", "累计CPUms", "累计物理读", "累计逻辑读", "最早登录"],
                "rows": [[r.get("login_name"), r.get("status"), _nm(r.get("session_count"),0),
                    _nm(r.get("total_cpu_ms"),0), _nm(r.get("total_physical_reads"),0),
                    _nm(r.get("total_logical_reads"),0), _dt(r.get("earliest_login_time"))]
                    for r in sess[:20]]}})
    sections.append({"section_id": "waits", "title": "等待、阻塞与会话", "items": s6, "chart_ids": ["05_waits"]})

    # tempdb — TempDB
    s7 = []
    if tdb and tdb.get("total_mb"):
        s7.append({"title": "TempDB 空间", "source": "tempdb.sys.dm_db_file_space_usage", "collection": _coll("TempDB"),
            "display": {"headers": ["指标", "值"], "rows": [
                ["总空间", f"{_num(tdb.get('total_mb')):,.0f} MB"], ["已用", f"{_num(tdb.get('used_mb')):,.0f} MB"],
                ["使用率", f"{_num(tdb.get('used_pct')):.1f}%"], ["文件数", len(tdb.get("files",[]))]]
                + _tempdb_io_rows(s)}})
    sections.append({"section_id": "tempdb", "title": "TempDB", "items": s7})

    # sql_index — SQL 与索引
    s8 = []
    tq = s.get("top_queries", [])
    if tq:
        s8.append({"title": "高消耗SQL(CPU)", "source": "sys.dm_exec_query_stats", "collection": _coll("高消耗SQL"),
            "display": {"headers": ["数据库", "执行次数", "总CPUms", "平均逻辑读", "溢出次数", "语句指纹"],
                "rows": [[q.get("database_name"), q.get("execution_count"), _nm(q.get("total_cpu_ms"),0),
                    _nm(q.get("avg_logical_reads"),0), _nm(q.get("total_spills"),0),
                    q.get("query_hash") or "-"] for q in tq[:15]]}})
    tqlr = s.get("top_queries_logical_reads", [])
    if tqlr:
        s8.append({"title": "高逻辑读SQL", "source": "sys.dm_exec_query_stats", "collection": _coll("高逻辑读SQL"),
            "display": {"headers": ["数据库", "执行次数", "总逻辑读", "平均逻辑读", "溢出次数", "语句指纹"],
                "rows": [[q.get("database_name"), q.get("execution_count"), _nm(q.get("total_logical_reads"),0),
                    _nm(q.get("avg_logical_reads"),0), _nm(q.get("total_spills"),0),
                    q.get("query_hash") or "-"] for q in tqlr[:15]]}})
    # 采集端按四个维度各取 TOP30（CPU / 逻辑读 / 执行次数 / 物理读），报告此前只渲染前两个。
    # 物理读那一条是判断「缓冲池不足还是索引缺失」的直接证据 —— 逻辑读高说明索引选择还可以，
    # 物理读高才说明数据没在内存里。
    tqe = s.get("top_queries_executions", [])
    if tqe:
        s8.append({"title": "高执行次数SQL", "source": "sys.dm_exec_query_stats", "collection": _coll("高执行次数SQL"),
            "display": {"headers": ["数据库", "执行次数", "总CPUms", "总逻辑读", "总物理读", "语句指纹"],
                "rows": [[q.get("database_name"), q.get("execution_count"), _nm(q.get("total_cpu_ms"),0),
                    _nm(q.get("total_logical_reads"),0), _nm(q.get("total_physical_reads"),0),
                    q.get("query_hash") or "-"] for q in tqe[:15]]}})
    tqp = s.get("top_queries_physical_reads", [])
    if tqp:
        s8.append({"title": "高物理读SQL", "source": "sys.dm_exec_query_stats", "collection": _coll("高物理读SQL"),
            "display": {"headers": ["数据库", "执行次数", "总物理读", "总CPUms", "总耗时ms", "语句指纹"],
                "rows": [[q.get("database_name"), q.get("execution_count"), _nm(q.get("total_physical_reads"),0),
                    _nm(q.get("total_cpu_ms"),0), _nm(q.get("total_elapsed_ms"),0),
                    q.get("query_hash") or "-"] for q in tqp[:15]]}})
    # 采集成功但无数据（success_no_data）也必须成项：「本实例未产生缺失索引建议」
    # 是一个结论，不等于"没有这一项"。原先整段用 if 包裹，空数据时整项从报告里
    # 消失，读者无法区分"没查过"和"查了没有"—— 与「未采集 ≠ 0」是同一条规矩。
    mi = s.get("missing_indexes", [])
    s8.append({"title": "缺失索引候选", "source": "sys.dm_db_missing_index_*", "collection": _coll("缺失索引建议"),
        "display": {"headers": ["数据库", "对象", "改善分", "键列", "包含列"],
            "empty_text": _blank_note(_coll("缺失索引建议"), "已检查实例内全部用户库的缺失索引建议，本次未产生候选。"),
            "rows": [[x.get("database_name"), f"{x.get('schema_name')}.{x.get('table_name')}", _nm(x.get("improvement_score"),0),
                str(x.get("equality_columns") or "-")[:80], str(x.get("included_columns") or "-")[:80]] for x in mi[:20]]}})
    ui = s.get("unused_indexes", [])
    s8.append({"title": "未使用索引", "source": "sys.dm_db_index_usage_stats", "collection": _coll("未使用索引"),
        "display": {"headers": ["数据库", "对象", "索引", "更新次数"],
            "empty_text": _blank_note(_coll("未使用索引"), "已检查实例内全部用户库的索引使用统计，本次未发现长期未被读取的索引。"),
            "rows": [[x.get("database_name"), f"{x.get('schema_name')}.{x.get('table_name')}", x.get("index_name"), _nm(x.get("user_updates"),0)] for x in ui[:20]]}})
    sections.append({"section_id": "sql_index", "title": "SQL 与索引", "items": s8})

    # db_objects — 数据库对象
    s9 = []
    nopk = s.get("tables_without_pk", [])
    if nopk:
        s9.append({"title": "无主键表", "source": "INFORMATION_SCHEMA 遍历", "collection": _coll("无主键表", "无主键表（深度）"),
            "display": {"headers": ["数据库", "无主键表数"], "rows": _rows(nopk, ["database_name","table_count"])}})
    lgt = s.get("large_tables", [])
    if lgt:
        s9.append({"title": "大表统计", "source": "sys.tables + partitions 遍历", "collection": _coll("大表统计", "大表统计（深度）"),
            "display": {"headers": ["数据库", "表", "行数", "空间MB"],
                "rows": [[t.get("database_name"), f"{t.get('schema_name')}.{t.get('table_name')}", _nm(t.get("row_count"),0), _nm(t.get("total_mb"),0)] for t in lgt[:30]]}})
    rcsi = s.get("database_rcsi", [])
    if rcsi:
        s9.append({"title": "快照隔离", "source": "sys.databases RCSI/SI", "collection": _coll("数据库RCSI状态"),
            "display": {"headers": ["数据库", "RCSI", "Snapshot Isolation"],
                "rows": [[r.get("database_name"), _tv(r.get("RCSI_enabled")), str(r.get("SI_state","-"))] for r in rcsi]}})
    # 这三项只有启用 -IncludeDeepChecks 才采集，采集端模块名带「（深度）」后缀。
    # 此前它们只有逐条 finding、没有汇总表 —— 几十上百个对象散在风险册里等于没有。
    # 同上前两项：深度检查未启用或本实例确实无候选，都必须成项 ——「本次未发现」
    # 和「这一项不存在」在报告里不能长得一样。
    stale = s.get("stale_statistics", [])
    s9.append({"title": "陈旧统计信息", "source": "sys.dm_db_stats_properties（深度检查）",
        "collection": _coll("陈旧统计信息（深度）"),
        "display": {"headers": ["数据库", "表", "统计名", "最后更新", "行数", "变更次数"],
            "empty_text": _blank_note(_coll("陈旧统计信息（深度）"), "已检查实例内全部用户库的统计信息，本次未发现陈旧统计。"),
            "rows": [[x.get("database_name"), f"{x.get('schema_name')}.{x.get('table_name')}", x.get("stats_name"),
                _dt(x.get("last_updated")), _nm(x.get("row_count"),0), _nm(x.get("modification_counter"),0)]
                for x in stale[:30]]}})
    frag = s.get("fragmented_indexes", [])
    s9.append({"title": "索引碎片", "source": "sys.dm_db_index_physical_stats（深度检查）",
        "collection": _coll("索引碎片（深度）"),
        "display": {"headers": ["数据库", "表", "索引", "类型", "碎片%", "页数"],
            "empty_text": _blank_note(_coll("索引碎片（深度）"), "已检查实例内全部用户库的索引碎片，本次未发现超过阈值的候选。"),
            "rows": [[x.get("database_name"), f"{x.get('schema_name')}.{x.get('table_name')}", x.get("index_name"),
                x.get("index_type_desc"), _nm(x.get("avg_fragmentation_in_percent"),1), _nm(x.get("page_count"),0)]
                for x in frag[:30]]}})
    sections.append({"section_id": "db_objects", "title": "数据库对象", "items": s9})

    # security — 安全与身份
    s10 = []
    sc = sec.get("security_checks", [])
    if sc:
        s10.append({"title": "安全检查", "source": "综合安全检测", "collection": _coll("安全与权限"),
            "display": {"headers": ["检查项", "结果", "详情"],
                "rows": [[c.get("check_name"), "通过" if c.get("result")=="PASS" else "未通过", c.get("detail","-")] for c in sc]}})
    logins = sec.get("sql_logins", [])
    if logins:
        s10.append({"title": "SQL登录", "source": "sys.sql_logins", "collection": {"status": _cst("安全与权限"), "row_count": len(logins)},
            "display": {"headers": ["登录名", "类型", "状态", "默认库"],
                "rows": _rows(logins[:50], ["name","type_desc","is_disabled","default_database_name"])}})
    sysadm = sec.get("sysadmin_members", [])
    if sysadm:
        s10.append({"title": "sysadmin成员", "source": "sys.server_role_members", "collection": {"status": _cst("安全与权限"), "row_count": len(sysadm)},
            "display": {"headers": ["成员", "类型", "状态"],
                "rows": [[m.get("name"), m.get("type_desc"), "禁用" if m.get("is_disabled") else "启用"] for m in sysadm]}})
    orphan = s.get("orphaned_users", [])
    s10.append({"title": "孤立用户", "source": "sys.database_principals（深度检查）",
        "collection": _coll("孤立用户（深度）"),
        "display": {"headers": ["数据库", "用户", "类型", "认证类型"],
            "empty_text": _blank_note(_coll("孤立用户（深度）"),
                "已检查实例内全部用户库的数据库用户，本次未发现孤立用户。"),
            "rows": [[x.get("database_name"), x.get("user_name"), x.get("user_type_desc"),
                x.get("authentication_type_desc")] for x in orphan[:30]]}})
    sections.append({"section_id": "security", "title": "安全与身份", "items": s10})

    # ha — 高可用与复制
    s11 = []
    ag = s.get("availability_replicas", [])
    if ag:
        s11.append({"title": "AlwaysOn可用性组", "source": "sys.dm_hadr_*", "collection": _coll("Always On"),
            "display": {"headers": ["AG", "副本", "数据库", "角色", "同步", "健康"],
                "rows": [[a.get("availability_group"), a.get("replica_server_name"), a.get("database_name"),
                    a.get("role_desc"), a.get("synchronization_state_desc"), a.get("synchronization_health_desc")] for a in ag]}})
        # 提交模式必须与同步状态并排：SYNCHRONIZING 在异步提交下是常态、同步提交下才是真延迟，
        # 只看同步状态列会把正常态读成故障、也会把真延迟读成正常（现场报告 4.1 就是后者）。
        s11.append({"title": "副本同步与延迟", "source": "sys.dm_hadr_database_replica_states", "collection": _coll("Always On"),
            "display": {"headers": ["AG", "数据库", "提交模式", "同步状态", "延迟秒", "最后重做", "发送队列MB", "重做队列MB"],
                "rows": [[a.get("availability_group"), a.get("database_name"), a.get("availability_mode_desc"),
                    a.get("synchronization_state_desc"),
                    _nm(a.get("replica_lag_seconds"),0) if a.get("replica_lag_seconds") is not None else _nm(a.get("secondary_lag_seconds"),0),
                    _dt(a.get("last_redone_time")), _nm(a.get("log_send_queue_mb"),1), _nm(a.get("redo_queue_mb"),1)] for a in ag]}})
        s11.append({"title": "副本连接与状态", "source": "sys.dm_hadr_availability_replica_states", "collection": _coll("Always On"),
            "display": {"headers": ["副本", "角色", "运行状态", "连接状态", "重做健康", "连接错误"],
                "rows": [[a.get("replica_server_name"), a.get("role_desc"), a.get("operational_state_desc"),
                    a.get("connected_state_desc"), a.get("recovery_health_desc"),
                    str(a.get("last_connect_error_description") or "-")[:60]] for a in ag]}})
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
    hac = s.get("ha_status", [])
    if hac:
        h0 = hac[0]
        hadr_on = str(h0.get("hadr_enabled")).strip() in ("1", "True", "true")
        s11.append({"title": "高可用状态", "source": "SERVERPROPERTY + sys.availability_*", "collection": _coll("高可用状态"),
            "display": {"headers": ["项目", "值"], "rows": [
                ["AG 已启用", _tv(hadr_on)],
                ["可用性组数", _nm(h0.get("availability_group_count"),0)],
                ["副本数", _nm(h0.get("availability_replica_count"),0)],
                ["AG 内数据库数", _nm(h0.get("ag_database_count"),0)]]}})
    repl = s.get("replication", [])
    if repl:
        # 两个形状：v1.2 采集端压成单结果集（record_type 区分种类），更早的包是 publication_name 那种。
        if any("record_type" in row for row in repl):
            relay = [[str(r.get("record_type") or "-"), r.get("name"), r.get("source_db"), r.get("status_desc"),
                      _nm(r.get("error_count"),0), str(r.get("error_message") or r.get("detail") or "-")[:80]]
                     for r in repl]
            s11.append({"title": "复制拓扑", "source": "分发库 dbo.MS* / sys.databases 复制标志", "collection": _coll("复制拓扑", "复制配置"),
                "display": {"headers": ["记录类型", "名称", "库", "状态", "次数", "详情"], "rows": relay[:40]}})
        else:
            s11.append({"title": "复制配置", "source": "msdb.dbo.MSpublications/MSsubscriptions", "collection": _coll("复制拓扑", "复制配置"),
                "display": {"headers": ["发布名", "类型", "状态", "订阅服务器", "订阅库"],
                    "rows": [[r.get("publication_name",""), r.get("type_desc","-"), r.get("status_desc","-"),
                        r.get("subscriber_server","-"), r.get("subscriber_db","-")] for r in repl]}})
    sections.append({"section_id": "ha", "title": "高可用与复制", "items": s11})

    # ops — 作业与错误日志
    s12 = []
    fj = s.get("failed_jobs", [])
    if fj:
        s12.append({"title": "失败作业(近24h)", "source": "msdb.dbo.sysjobhistory", "collection": _coll("SQL Agent失败作业"),
            "display": {"headers": ["作业", "失败时间", "消息"],
                "rows": [[j.get("job_name"), _dt(j.get("run_datetime")), str(j.get("message") or "-")[:200]] for j in fj]}})
    all_j = s.get("agent_jobs", [])
    if all_j:
        s12.append({"title": "作业清单", "source": "msdb.dbo.sysjobs", "collection": _coll("SQL Agent作业清单"),
            "display": {"headers": ["作业", "启用", "最近结果", "最近运行", "最近耗时"],
                "rows": [[j.get("job_name"), _tv(j.get("enabled")), j.get("last_run_status_desc","-"),
                    _dt(j.get("last_run_datetime")), _hhmmss(j.get("last_run_duration"))] for j in all_j[:50]]}})
    errs = s.get("error_log_summary", [])
    if errs:
        s12.append({"title": "错误日志", "source": "xp_readerrorlog", "collection": _coll("错误日志摘要"),
            "display": {"headers": ["日期", "错误号", "Severity", "次数", "示例"],
                "rows": [[e.get("event_date"), e.get("error_number") or "-", e.get("severity"),
                    _nm(e.get("event_count"),0), str(e.get("sample_message") or "-")[:160]] for e in errs[:20]]}})
    # 作业步骤明细：作业清单只有作业名 + 整体历史结果，"失败在哪一步"答不了。
    # 采集端按"最近失败优先"排序，故表内靠前的行就是最该看的行。
    steps = s.get("job_steps", [])
    if steps:
        s12.append({"title": "步骤明细", "source": "msdb.dbo.sysjobsteps（最近失败优先）",
            "collection": _coll("SQL Agent作业步骤"),
            "display": {"headers": ["作业", "步骤号", "步骤名", "子系统", "失败动作", "重试", "上次结果", "上次耗时"],
                "rows": [[x.get("job_name"), _nm(x.get("step_id"),0), x.get("step_name"), x.get("subsystem"),
                    x.get("on_fail_action"), _nm(x.get("retry_attempts"),0),
                    x.get("last_run_outcome_desc") or "-", _hhmmss(x.get("last_run_duration"))]
                    for x in steps[:80]]}})
    sections.append({"section_id": "ops", "title": "作业与错误日志", "items": s12})

    # config — 实例关键配置
    cfg = s.get("configurations", [])
    if cfg:
        sections.append({"section_id": "config", "title": "实例关键配置", "items": [
            {"title": "关键配置项", "source": "sys.configurations", "collection": _coll("实例配置"),
                "display": {"headers": ["配置项", "值", "最小", "最大", "动态"],
                    "rows": [[c.get("name"), c.get("value_in_use"), c.get("minimum"), c.get("maximum"), _tv(c.get("is_dynamic"))] for c in cfg]}}]})

    # collection_detail — 采集执行明细
    # 采集完整度此前只体现为第 1 章的一个百分比（"数据完整度 100%"），47 个模块里哪个失败、
    # 哪个最慢，报告里看不到 —— 交包被问「这块采到没有」只能回去翻 collector.log。
    s14 = []
    if mods:
        timed = []
        for m in mods:
            elapsed = m.get("elapsed_ms")
            try:
                secs = float(elapsed) if elapsed is not None else None
            except (TypeError, ValueError):
                secs = None
            timed.append((m, secs))
        # 耗时降序：最慢的模块置顶，这正是这张表要回答的问题；未计时的排最后（不是 0 秒）。
        timed.sort(key=lambda pair: (pair[1] is None, -(pair[1] or 0)))
        rows14 = [[m.get("name"),
                   _COLLECTION_STATUS_CN.get(str(m.get("status") or "").lower(), str(m.get("status") or "-")),
                   _nm(m.get("row_count"), 0),
                   "-" if secs is None else _nm(secs, 0),
                   str(m.get("error") or "-")[:80]] for m, secs in timed]
        s14.append({"title": "采集执行明细", "source": "collection.module_status（采集端逐模块登记）",
            "collection": {"status": "ok", "row_count": len(mods)},
            "display": {"headers": ["模块", "状态", "行数", "耗时ms", "错误"], "rows": rows14}})
    # 无条件登记：适配器会按「有无条目」跳过空章节，这里要保证章节集合稳定可校验。
    sections.append({"section_id": "collection_detail", "title": "采集执行明细", "items": s14})

    # 每个检查项的口径说明（display.note）与默认分析结论由 analysis_text 统一补。
    # 集中在一处而不是逐项手写：42 个检查项的文案才能被守门测试整体校验，
    # 也避免 presentation.py 再长出一个"文案与结构混在一起"的章节。
    return decorate_sections(sections, s, perf)

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
        ('backup', '可疑页面'): ['DB.SUSPECT_PAGES'],
        ('memory', '缓冲池'): ['MEM.CACHE_IMBALANCE'],
        # 只匹配「进程内存状态」这一项。原先写的是 '内存' 子串 —— 它会同时命中同章的
        # 「内存组件分布」「系统内存状态」，让三项结论逐字相同（都在说"进程内存利用率极高"），
        # 而这两项在 analysis_text 里本来各有定制文案（讲 clerk 占用 / 整机内存）。
        ('memory', '进程内存'): ['MEM.SIGNALING','MEM.PRESSURE'],
        ('capacity', '存储卷'): ['STORAGE.FREE_SPACE'],
        ('capacity', '日志'): ['LOG.SPACE'],
        ('capacity', 'VLF'): ['LOG.VLF'],
        ('capacity', '文件'): ['FILE.IO_LATENCY','FILE.PERCENT_GROWTH'],
        ('perf', '性能'): ['PERF.PLE','PERF.MEMORY_GRANTS'],
        ('waits', '主要等待'): ['PERF.WAIT_TOP'],
        ('waits', '阻塞'): ['PERF.BLOCKING'],
        ('waits', '活动'): ['PERF.LONG_REQUEST'],
        ('waits', '连接'): ['PERF.CONNECTION_FLOOD'],
        ('waits', '会话'): ['SESSION.RESOURCE_HOG'],
        ('tempdb', 'TempDB'): ['TEMPDB.USAGE','TEMPDB.FILE_BALANCE'],
        ('db_objects', '无主键'): ['OBJ.NO_PK'],
        ('db_objects', '大表'): ['OBJ.LARGE_TABLE'],
        ('db_objects', '快照'): ['DB.RCSI'],
        ('db_objects', '陈旧统计'): ['PERF.STALE_STATS'],
        ('db_objects', '索引碎片'): ['PERF.INDEX_FRAGMENTATION'],
        ('security', '安全'): ['SEC.SA_ACCOUNT_DISABLED','SEC.EMPTY_PASSWORD_LOGINS','SEC.XP_CMDSHELL_ENABLED','SEC.OLE_AUTOMATION_ENABLED'],
        ('security', 'SQL登录'): ['SEC.SA_ENABLED'],
        ('security', 'sysadmin'): ['SEC.SYSADMIN_COUNT'],
        ('security', '孤立用户'): ['SEC.ORPHANED_USER'],
        ('ha', 'Always'): ['HA.AG_HEALTH','HA.AG_QUEUE'],
        ('ha', '镜像'): ['HA.MIRRORING'],
        ('ha', '日志传送'): ['HA.LOG_SHIPPING'],
        ('ha', '复制'): ['HA.REPLICATION'],
        ('ops', '失败'): ['AGENT.JOB_FAILED'],
        ('ops', '作业清单'): ['AGENT.JOB_DISABLED','AGENT.JOB_OVERDUE'],
        ('ops', '错误'): ['ERRORLOG.SEVERE'],
        ('config', '配置'): ['INSTANCE.MAX_MEMORY','INSTANCE.MAXDOP','INSTANCE.COST_THRESHOLD',
            'INSTANCE.ADHOC_WORKLOADS','INSTANCE.BLOCKED_PROC_THRESHOLD','INSTANCE.BACKUP_COMPRESSION'],
        ('instance', '服务'): ['INSTANCE.SERVICE_STARTUP'],
        ('instance', '实例'): ['INSTANCE.VERSION_SUPPORT'],
        ('ha', '同步'): ['HA.AG_SYNC_MODE','HA.AG_LAG'],
        ('ha', '连接'): ['HA.AG_CONNECT_ERROR'],
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
            # 三档：high/critical -> risk；medium **或 low** -> attention。
            # 曾经把「只剩 low」判成 normal，于是「数据库清单」出现状态=正常、结论=三条风险
            # 并排的自相矛盾 —— 命中的全是 low 档也是问题，只是轻，不能写"正常"。
            status = 'risk' if severities & {'critical','high'} else 'attention' if severities & {'medium','low'} else 'normal'
            # 一个检查项命中多条规则时，各条的依据与建议会拼进同一格。不加主语就会
            # 串行 —— 实测「max server memory=…，物理内存=4095 MB；当前值=5；」里那个
            # "当前值=5" 是 cost threshold 的，读者会读成最大内存配了 5 MB。
            multi = len(unique) > 1

            def _label(ff: dict, value) -> str:
                v = str(value or '')
                if not multi or not v:
                    return v
                name = str(ff.get('object_name') or '').strip()
                if not name or v.startswith(name):
                    return v
                return f"{name}：{v}"

            item['analysis'] = {
                'status': status,
                'conclusion': '；'.join(f"{ff['title']}（{ff['object_name']}）" for ff in unique[:3]),
                'evidence': [_label(ff, ff.get('evidence', '')) for ff in unique[:3] if ff.get('evidence')],
                'recommendation': '；'.join(_label(ff, ff.get('recommendation', '')) for ff in unique[:3] if ff.get('recommendation')),
            }
    return sections

