"""Oracle ??????????????????? report model?"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .metrics import safe_float
from .package_adapter import parse_instance_tag


VERSION = "2.0.0"
CONTRACT = "oracle_inspection_report_model"
ANALYSIS_SCHEMA_VERSION = "2.0"


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


@dataclass
class Finding:
    finding_id: str
    rule_id: str
    severity: str
    category: str
    title: str
    summary: str
    facts: list[str]
    recommendation: str
    evidence_refs: list[str]
    confidence: float = 0.9
    status: str = "triggered"  # triggered | passed
    triggered: bool = True
    requires_restart: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "finding_id": self.finding_id,
            "rule_id": self.rule_id,
            "severity": self.severity,
            "category": self.category,
            "title": self.title,
            "summary": self.summary,
            "facts": self.facts,
            "recommendation": self.recommendation,
            "evidence_refs": self.evidence_refs,
            "confidence": self.confidence,
            "status": self.status,
            "triggered": self.triggered,
            "requires_restart": self.requires_restart,
        }

def build_inspection_sections(ctx: Any, metrics: dict[str, Any],
                               findings: list[Finding],
                               evaluations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build data-driven inspection sections mirroring MySQL's build_inspection_model.

    Each section maps to a report chapter; each item carries display rows + analysis block.
    Findings and rule evaluations are looked up by rule_id to build analysis blocks.
    """
    env = ctx.snapshot
    tag = env.get("instance_tag", "")
    s_hostname, s_ip = parse_instance_tag(tag)
    quality = metrics.get("_quality", {})
    sampling = metrics.get("sampling_context", {})
    ora = metrics.get("oracle_realtime", {})
    sys_rt = metrics.get("system_realtime", {})
    sys_hist = metrics.get("system_history", {})

    # ── build lookup indexes ──
    _finding_by_rule: dict[str, dict[str, Any]] = {}
    for f in findings:
        _finding_by_rule[f.rule_id] = f.to_dict()

    _eval_by_rule: dict[str, dict[str, Any]] = {}
    for e in evaluations:
        _eval_by_rule[e.get("rule_id", "")] = e

    # ── helpers ──
    def _status(item_id: str) -> dict[str, Any]:
        for item in quality.get("non_ok_items", []):
            if item.get("item_id") == item_id:
                return item
        return {"status": "ok"}

    def _val_row(name: str, value: Any, desc: str = "") -> dict[str, Any]:
        return {"检查项": name, "采集值": str(value) if value is not None else "未采集", "说明": desc}

    def _analysis(rule_id: str) -> dict[str, Any]:
        f = _finding_by_rule.get(rule_id)
        ev = _eval_by_rule.get(rule_id)
        ev_status = str(ev.get("status", "")) if ev else ""

        if f:
            sev = f.get("severity", "medium")
            status = "risk" if sev in ("critical", "high") else "attention"
            return {
                "status": status,
                "conclusion": f.get("summary", ""),
                "evidence": f.get("facts", []),
                "recommendation": f.get("recommendation", ""),
            }
        elif ev_status == "passed":
            return {"status": "normal", "conclusion": "检查通过，未发现异常。", "evidence": [], "recommendation": ""}
        elif ev_status == "not_applicable":
            return {"status": "not_applicable", "conclusion": "当前架构不适用此项检查。", "evidence": [], "recommendation": ""}
        elif ev_status:  # has evaluation but not triggered/passed/applicable
            return {"status": "not_evaluated", "conclusion": "证据不足，未执行评价。", "evidence": [], "recommendation": ""}
        else:  # no evaluation at all — data-only display item
            return {"status": "", "conclusion": ""}

    def _item(title: str, item_id: str, rows: list[dict[str, Any]] | None = None,
               analysis: dict[str, Any] | None = None) -> dict[str, Any]:
        col = _status(item_id)
        return {
            "title": title,
            "source": f"Oracle 采集包 — {item_id}",
            "collection": {
                "status": col.get("status", "ok"),
                "row_count": len(rows) if rows else None,
                "reason": col.get("reason", ""),
            },
            "display": {"rows": rows or [], "note": ""},
            "analysis": analysis if analysis is not None else _analysis(item_id),
        }

    def _data_item(title: str, item_id: str, rows: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        """Data-only display item — no analysis block."""
        return _item(title, item_id, rows=rows, analysis={"status": "", "conclusion": ""})

    sections: list[dict[str, Any]] = []

    # ── 3. 系统环境检查 (system_environment) ──
    is_rac = int(env.get("is_rac", 0)) == 1
    is_cdb = int(env.get("is_cdb", 0)) == 1
    has_asm = int(env.get("has_asm", 0)) == 1
    se_items: list[dict[str, Any]] = [
        _item("主机信息", "system.host", [
            _val_row("主机名", s_hostname or env.get("host", ""), "采集目标主机"),
            _val_row("IP 地址", s_ip or env.get("host", ""), "数据库服务地址"),
            _val_row("操作系统", env.get("os") or env.get("distribution", ""), ""),
        ]),
        _item("数据库身份", "oracle.identity", [
            _val_row("数据库名", env.get("db_name")),
            _val_row("实例名", env.get("instance_name")),
            _val_row("版本", env.get("ora_version")),
            _val_row("角色", env.get("db_role")),
            _val_row("RAC", "是 (集群)" if is_rac else "否 (单机)"),
            _val_row("CDB/PDB", "是" if is_cdb else "否"),
            _val_row("ASM", "是" if has_asm else "否"),
        ]),
        _item("时间同步检查", "ORA.SYSTEM.TIME_SYNC",
               analysis=_analysis("ORA.SYSTEM.TIME_SYNC")),
        _item("采集完整性", "ORA.COLLECTION.INTEGRITY",
               analysis=_analysis("ORA.COLLECTION.INTEGRITY")),
        _item("采集数据质量", "ORA.COLLECTION.QUALITY",
               analysis=_analysis("ORA.COLLECTION.QUALITY")),
    ]
    # ── DB config info (informational display) ──
    db_info_rows = ctx.tables.get("数据库基本信息", [])
    charset_rows = ctx.tables.get("数据库字符集", [])
    sga_rows = ctx.tables.get("SGA 汇总信息", [])
    pga_rows = ctx.tables.get("PGA 统计", [])
    display_dbconf: list[dict[str, Any]] = [
        _val_row("DB_NAME", env.get("db_name", "")),
        _val_row("DB_UNIQUE_NAME", db_info_rows[0].get("DB_UNIQUE_NAME", "").strip() if db_info_rows else ""),
        _val_row("数据库角色", env.get("db_role", "")),
        _val_row("打开模式", env.get("open_mode", "") or (db_info_rows[0].get("OPEN_MODE", "").strip() if db_info_rows else "")),
        _val_row("归档模式", "ARCHIVELOG" if db_info_rows and str(db_info_rows[0].get("LOG_MODE","")).strip() == "ARCHIVELOG" else "NOARCHIVELOG" if db_info_rows else ""),
        _val_row("创建时间", (db_info_rows[0].get("CREATED","").strip() if db_info_rows else "")),
    ]
    for row in charset_rows:
        pname = str(row.get("PARAMETER","")).strip()
        if pname in ("NLS_CHARACTERSET","NLS_NCHAR_CHARACTERSET","NLS_LANGUAGE","NLS_DATE_FORMAT"):
            display_dbconf.append(_val_row(pname, str(row.get("VALUE","")).strip()))
    for row in sga_rows:
        display_dbconf.append(_val_row("SGA / " + str(row.get("NAME","")).strip(), f"{safe_float(row.get('MB',row.get('     MB','0'))):.0f} MB" if row.get("MB") or row.get("     MB") else ""))
    pga_val = pga_rows[0].get("VALUE","") if pga_rows else ""
    if pga_val:
        try:
            pga_val = f"{float(pga_val)/1024/1024:.0f} MB"
        except: pass
    display_dbconf.append(_val_row("PGA 总分配", pga_val))
    se_items.append(_item("数据库配置概要", "oracle.db_config", display_dbconf))
    sections.append({"section_id": "system_environment", "title": "系统环境检查", "items": se_items})

    # ── 4. 系统性能检查 (system_performance) ──
    local = metrics.get("scope", {}).get("database_target_is_local", False)
    hist_usable = sampling.get("history", {}).get("usable_for_trend_rules", False)

    # Choose best data source
    cpu_src = sys_hist if hist_usable else sys_rt
    cpu_label = "SAR 24h" if hist_usable else "实时采样"
    resource_rows = [
        {
            "指标": "CPU 使用率(%)",
            "平均值": f"{cpu_src.get('cpu_busy_pct', {}).get('average', '-')}",
            "最大值": f"{cpu_src.get('cpu_busy_pct', {}).get('max', '-')}",
            "数据源": cpu_label,
        },
        {
            "指标": "IO Wait(%)",
            "平均值": f"{cpu_src.get('cpu_iowait_pct', {}).get('average', '-')}",
            "最大值": f"{cpu_src.get('cpu_iowait_pct', {}).get('max', '-')}",
            "数据源": cpu_label,
        },
        {
            "指标": "内存使用率(%)",
            "平均值": f"{sys_rt.get('memory_used_pct', {}).get('average', '-')}",
            "最大值": f"{sys_rt.get('memory_used_pct', {}).get('max', '-')}",
            "数据源": "实时采样",
        },
    ]
    sp_items: list[dict[str, Any]] = [
        _item("系统资源概要", "system.resources", resource_rows),
    ]
    if local:
        sp_items.append(_item("CPU 压力评估", "ORA.SYSTEM.CPU_PRESSURE",
                               analysis=_analysis("ORA.SYSTEM.CPU_PRESSURE")))
        sp_items.append(_item("IO Wait 评估", "ORA.SYSTEM.IOWAIT_PRESSURE",
                               analysis=_analysis("ORA.SYSTEM.IOWAIT_PRESSURE")))
        sp_items.append(_item("内存压力评估", "ORA.SYSTEM.MEMORY_PRESSURE",
                               analysis=_analysis("ORA.SYSTEM.MEMORY_PRESSURE")))

    # ── Filesystem data layer ──
    fs_rows = ctx.tables.get("filesystems", [])
    display_fs: list[dict[str, Any]] = []
    if fs_rows:
        for row in fs_rows[:20]:
            display_fs.append({
                "挂载点": row.get("MOUNTPOINT") or row.get("mountpoint", ""),
                "文件系统": row.get("FILESYSTEM") or row.get("filesystem", ""),
                "使用率": row.get("USE%") or row.get("used_pct", ""),
                "可用空间": row.get("AVAILABLE") or row.get("available", ""),
            })
    if display_fs:
        sp_items.append(_item("文件系统使用情况", "oracle.filesystems", rows=display_fs))
    sp_items.append(_item("文件系统分析", "ORA.CAPACITY.FILESYSTEM_USAGE",
                           analysis=_analysis("ORA.CAPACITY.FILESYSTEM_USAGE")))

    sections.append({"section_id": "system_performance", "title": "系统性能检查", "items": sp_items})

    # ── 5. 存储与容量检查 (storage_capacity) ──
    ts_rows = ctx.find_table("所有表空间容量使用情况", "表空间", "tablespace")
    display_ts: list[dict[str, Any]] = []
    for row in ts_rows[:30]:
        used_val = safe_float(row.get("USED_MB") or row.get("USED"))
        max_val = safe_float(row.get("MAX_MB"))
        alloc_val = safe_float(row.get("ALLOC_MB") or row.get("TOTAL_MB"))
        # Use MAX_MB as denominator when available; fall back to ALLOC_MB
        pct_base = max_val if max_val and max_val > 0 else alloc_val
        pct_val = f"{used_val / pct_base * 100:.2f}" if used_val is not None and pct_base and pct_base > 0 else ""
        display_ts.append({
            "表空间": row.get("TABLESPACE_NAME") or row.get("TABLESPACE", ""),
            "已分配(MB)": f"{alloc_val:.0f}" if alloc_val else "",
            "最大可扩(MB)": f"{max_val:.0f}" if max_val else "",
            "已使用(MB)": f"{used_val:.0f}" if used_val else "",
            "使用率(%)": pct_val,
        })
    sc_items: list[dict[str, Any]] = [
        # ── Data layer ──
        _item("表空间容量一览", "oracle.tablespace_list", rows=display_ts),
    ]

    # Datafile list (data layer)
    df_rows = ctx.tables.get("表空间数据文件列表", [])
    display_df: list[dict[str, Any]] = []
    for row in df_rows[:40]:
        display_df.append({
            "表空间": str(row.get("TABLESPACE_NAME","")).strip(),
            "文件路径": str(row.get("FILE_NAME","")).strip()[-60:],
            "大小(MB)": row.get("BYTES","") if row.get("BYTES") else row.get("SIZE_MB",""),
        })
    if display_df:
        sc_items.append(_item("数据文件列表", "oracle.datafile_list", rows=display_df))

    # ── Analysis layer ──
    sc_items.append(_item("表空间使用风险", "ORA.STORAGE.TABLESPACE",
                           analysis=_analysis("ORA.STORAGE.TABLESPACE")))

    if has_asm:
        asm_rows = ctx.tables.get("asm_diskgroup_summary", [])
        display_asm: list[dict[str, Any]] = []
        for row in asm_rows[:20]:
            display_asm.append({
                "磁盘组": row.get("NAME") or row.get("GROUP_NAME", ""),
                "总大小(MB)": row.get("TOTAL_MB", ""),
                "已用(MB)": row.get("USED_MB", ""),
                "使用率(%)": row.get("USED_PCT") or row.get("PCT", ""),
            })
        sc_items.append(_item("ASM 磁盘组", "ORA.STORAGE.ASM",
                               rows=display_asm,
                               analysis=_analysis("ORA.STORAGE.ASM")))

    fra_rows = ctx.find_table("FRA", "闪回区", "快速恢复区")
    if fra_rows:
        display_fra: list[dict[str, Any]] = []
        for row in fra_rows[:10]:
            display_fra.append({
                "名称": row.get("NAME", ""),
                "大小(MB)": row.get("SPACE_LIMIT") or row.get("SIZE_MB", ""),
                "使用率(%)": row.get("PERCENT_SPACE_USED") or row.get("PCT", ""),
            })
        sc_items.append(_item("快速恢复区(FRA)", "ORA.STORAGE.FRA",
                               rows=display_fra,
                               analysis=_analysis("ORA.STORAGE.FRA")))

    # ── Autoextend display ──
    ae_rows = ctx.tables.get("数据文件自动扩展余量", [])
    display_ae: list[dict[str, Any]] = []
    for row in ae_rows[:20]:
        display_ae.append({
            "表空间": str(row.get("TABLESPACE_NAME","")).strip(),
            "数据文件数": row.get("DATAFILE_COUNT",""),
            "当前(MB)": row.get("CURRENT_MB",""),
            "最大(MB)": row.get("MAX_MB",""),
            "自动扩展文件数": row.get("AUTOEXTEND_FILES",""),
        })
    sc_items.append(_item("数据文件自动扩展", "ORA.CONFIG.AUTOEXTEND",
                           rows=display_ae,
                           analysis=_analysis("ORA.CONFIG.AUTOEXTEND")))

    sections.append({"section_id": "storage_capacity", "title": "存储与容量检查", "items": sc_items})

    # ── 6. Oracle 实例检查 (oracle_instance) ──
    archive_rows = ctx.find_table("归档模式检查", "归档模式", "archive_mode")
    arch_display: list[dict[str, Any]] = []
    for row in archive_rows[:5]:
        # Parse columns regardless of tab/pipe separator
        if "LOG_MODE" in row and "ARCHIVE" in row:
            arch_display.append({"参数": "LOG_MODE", "值": row.get("LOG_MODE", row.get("ARCHIVE", ""))})
            arch_display.append({"参数": "ARCHIVE", "值": row.get("ARCHIVE", "")})
        else:
            # Pipe-delimited: split combined cell
            for rk, rv in row.items():
                if "|" in rk:
                    parts = rk.split("|")
                    vals = rv.split("|") if rv else []
                    for i, p in enumerate(parts):
                        arch_display.append({"参数": p.strip(), "值": vals[i].strip() if i < len(vals) else ""})
                    break
            else:
                arch_display.append({"参数": "归档模式", "值": next(iter(row.values()), "")})
    # ── Redo log display ──
    redo_rows = ctx.tables.get("Redo 日志组信息", [])
    display_redo: list[dict[str, Any]] = []
    for row in redo_rows[:10]:
        display_redo.append({
            "组号": row.get("GROUP#",""),
            "线程": row.get("THREAD#",""),
            "序列号": row.get("SEQUENCE#",""),
            "大小(MB)": row.get("SIZE_MB",""),
            "成员数": row.get("MEMBERS",""),
            "状态": str(row.get("STATUS","")).strip(),
        })
    oi_items: list[dict[str, Any]] = [
        # ── Data layer ──
        _item("关键初始化参数", "oracle.init_params", rows=[
            {"参数": str(r.get("NAME","")).strip(), "值": str(r.get("VALUE","")).strip()[:80]}
            for r in ctx.tables.get("关键初始化参数", [])[:40]
        ]),
        _item("Redo 日志组信息", "oracle.redo_logs", rows=display_redo),
        # 控制文件路径在这里构建,避免未定义错误
        _item("控制文件路径", "oracle.control_files", rows=[
            {"路径": str(row.get("NAME","") or row.get("STATUS |NAME","")).strip()}
            for row in ctx.tables.get("控制文件", [])
        ]),
        # ── Analysis layer ──
        _item("归档模式检查", "ORA.ARCHIVE.MODE",
               rows=arch_display,
               analysis=_analysis("ORA.ARCHIVE.MODE")),
        _item("Redo 日志评估", "ORA.CONFIG.REDO_MEMBER",
               analysis=_analysis("ORA.CONFIG.REDO_MEMBER")),
    ]

    # Checksum params
    chk_rows = ctx.tables.get("块校验与丢失写保护参数", [])
    if chk_rows:
        display_chk: list[dict[str, Any]] = []
        for row in chk_rows[:10]:
            display_chk.append({
                "参数": row.get("NAME", ""),
                "值": row.get("VALUE", ""),
            })
        oi_items.append(_item("块校验参数", "ORA.DATA.CHECKSUM",
                               rows=display_chk,
                               analysis=_analysis("ORA.DATA.CHECKSUM")))

    # Scheduler failures
    sched_rows = ctx.find_table("Scheduler 失败", "Scheduler失败", "scheduler_failures")
    display_sched: list[dict[str, Any]] = []
    for row in sched_rows[:20]:
        display_sched.append({
            "作业名": row.get("JOB_NAME") or row.get("NAME", ""),
            "状态": row.get("STATE") or row.get("STATUS", ""),
            "失败时间": row.get("LAST_START_DATE", ""),
        })
    oi_items.append(_item("Scheduler 作业状态", "ORA.JOB.FAILURE",
                           rows=display_sched,
                           analysis=_analysis("ORA.JOB.FAILURE")))

    sections.append({"section_id": "oracle_instance", "title": "Oracle 实例检查", "items": oi_items})

    # ── 7. 性能检查 (oracle_performance) ──
    perf_rows = [
        {
            "指标": "Commits/s",
            "平均值": f"{ora.get('commits_per_sec', {}).get('average', '-')}",
            "最大值": f"{ora.get('commits_per_sec', {}).get('max', '-')}",
        },
        {
            "指标": "物理读/s",
            "平均值": f"{ora.get('physical_reads_per_sec', {}).get('average', '-')}",
            "最大值": f"{ora.get('physical_reads_per_sec', {}).get('max', '-')}",
        },
        {
            "指标": "物理写/s",
            "平均值": f"{ora.get('physical_writes_per_sec', {}).get('average', '-')}",
            "最大值": f"{ora.get('physical_writes_per_sec', {}).get('max', '-')}",
        },
        {
            "指标": "逻辑读/s",
            "平均值": f"{ora.get('logical_reads_per_sec', {}).get('average', '-')}",
            "最大值": f"{ora.get('logical_reads_per_sec', {}).get('max', '-')}",
        },
        {
            "指标": "Redo(MB/s)",
            "平均值": "%.2f" % (ora.get('redo_bytes_per_sec', {}).get('average', 0) / 1024 / 1024) if ora.get('redo_bytes_per_sec', {}).get('average') else "-",
            "最大值": "%.2f" % (ora.get('redo_bytes_per_sec', {}).get('max', 0) / 1024 / 1024) if ora.get('redo_bytes_per_sec', {}).get('max') else "-",
        },
        {
            "指标": "Buffer Cache 命中率",
            "值": f"{ora.get('buffer_cache_hit_pct', '-')}%" if ora.get('buffer_cache_hit_pct') is not None else "未采集",
        },
        {
            "指标": "磁盘排序比例",
            "值": f"{ora.get('sort_disk_ratio_pct', '-')}%" if ora.get('sort_disk_ratio_pct') is not None else "未采集",
        },
    ]
    # ── Library Cache + Wait Events + Top SQL + IO hotfile displays ──
    lc_rows = ctx.tables.get("Library Cache 命中率", [])
    display_lc: list[dict[str, Any]] = []
    for row in lc_rows[:10]:
        display_lc.append({
            "Namespace": str(row.get("NAMESPACE","")).strip(),
            "GETS": row.get("GETS",""),
            "GETHIT%": row.get("GETHIT_PCT",""),
            "PINS": row.get("PINS",""),
            "PINHIT%": row.get("PINHIT_PCT",""),
            "RELOADS": row.get("RELOADS",""),
        })

    wait_rows = ctx.tables.get("等待事件 TOP10（数据库级）", [])
    display_wait: list[dict[str, Any]] = []
    for row in wait_rows[:10]:
        display_wait.append({
            "等待事件": str(row.get("EVENT","")).strip(),
            "等待次数": row.get("TOTAL_WAITS",""),
            "等待时间(s)": row.get("TIME_WAITED","") if safe_float(row.get("TIME_WAITED")) else row.get("TIME_WAITED",""),
            "平均等待(cs)": row.get("AVG_WAIT_CS",""),
        })

    sql_rows = ctx.tables.get("Top 20 SQL (按逻辑读)", [])
    display_sql: list[dict[str, Any]] = []
    for row in sql_rows[:10]:
        display_sql.append({
            "SQL_ID": row.get("SQL_ID",""),
            "模块": str(row.get("MODULE","")).strip(),
            "逻辑读": row.get("BUFFER_GETS",""),
            "磁盘读": row.get("DISK_READS",""),
            "耗时(min)": row.get("ELAPSED_MIN",""),
        })

    io_rows = ctx.tables.get("IO 消耗最高数据文件 TOP10", [])
    display_io: list[dict[str, Any]] = []
    for row in io_rows[:10]:
        fname = str(row.get("FILE_NAME","")).strip()
        display_io.append({
            "数据文件": fname[-50:] if len(fname) > 50 else fname,
            "物理读": row.get("PHYRDS",""),
            "物理写": row.get("PHYWRTS",""),
            "总IO": row.get("TOTAL_IO",""),
        })

    cpu_rows = ctx.tables.get("CPU 高消耗会话 TOP10", [])
    display_cpu: list[dict[str, Any]] = []
    for row in cpu_rows[:10]:
        display_cpu.append({
            "SID": row.get("SID",""),
            "Serial#": row.get("SERIAL#",""),
            "用户名": str(row.get("USERNAME","")).strip(),
            "CPU(s)": row.get("CPU_SECS",""),
            "机器": str(row.get("MACHINE","")).strip(),
        })

    op_items: list[dict[str, Any]] = [
        _item("Oracle 性能指标", "oracle.performance", perf_rows),
        _item("Buffer Cache 命中率", "ORA.PERFORMANCE.BUFFER_CACHE",
               analysis=_analysis("ORA.PERFORMANCE.BUFFER_CACHE")),
        _item("Library Cache 命中率", "ORA.PERFORMANCE.LIBRARY_CACHE",
               rows=display_lc,
               analysis=_analysis("ORA.PERFORMANCE.LIBRARY_CACHE")),
        _item("磁盘排序比例", "ORA.PERFORMANCE.SORT_DISK",
               analysis=_analysis("ORA.PERFORMANCE.SORT_DISK")),
        _item("数据库等待事件", "ORA.PERFORMANCE.WAIT_EVENTS",
               rows=display_wait,
               analysis=_analysis("ORA.PERFORMANCE.WAIT_EVENTS")),
        _item("Top SQL (按逻辑读)", "ORA.PERFORMANCE.SQL_EXECUTION",
               rows=display_sql,
               analysis=_analysis("ORA.PERFORMANCE.SQL_EXECUTION")),
        _item("Top SQL (按执行时间)", "oracle.top_sql_elapsed", rows=[
            {"SQL_ID": r.get("SQL_ID",""),"模块": str(r.get("MODULE","")).strip(),"耗时(min)": r.get("ELAPSED_MIN",""),
             "CPU(s)": r.get("CPU_SEC",""),"执行次数": r.get("EXECUTIONS",""),"逻辑读": r.get("BUFFER_GETS","")}
            for r in ctx.tables.get("Top 20 SQL (按执行时间)", [])[:10]
        ]),
        _item("IO 热点数据文件", "ORA.PERFORMANCE.IO_HOTFILE",
               rows=display_io,
               analysis=_analysis("ORA.PERFORMANCE.IO_HOTFILE")),
        _item("CPU 高消耗会话", "oracle.cpu_top_sessions", rows=display_cpu),
    ]
    sections.append({"section_id": "oracle_performance", "title": "Oracle 性能检查", "items": op_items})

    # ── 8. 会话与事务检查 (sessions_transactions) ──
    long_txn_rows = ctx.tables.get("长事务检查", [])
    display_txn: list[dict[str, Any]] = []
    for row in long_txn_rows[:20]:
        display_txn.append({
            "SID": row.get("SID", ""),
            "Serial#": row.get("SERIAL#", ""),
            "用户名": row.get("USERNAME", ""),
            "状态": row.get("STATUS", ""),
            "开始时间": row.get("START_TIME", ""),
        })
    lock_rows = ctx.tables.get("锁等待链", [])
    display_lock: list[dict[str, Any]] = []
    for row in lock_rows[:20]:
        display_lock.append({
            "阻塞SID": row.get("BLOCKING_SID") or row.get("BLOCKING_SESSION", ""),
            "等待SID": row.get("WAITING_SID") or row.get("WAITING_SESSION", ""),
            "锁类型": row.get("LOCK_TYPE", ""),
            "等待时间(s)": row.get("WAIT_SECONDS", ""),
        })
    st_items: list[dict[str, Any]] = [
        _item("长时间事务", "ORA.TXN.LONG",
               rows=display_txn,
               analysis=_analysis("ORA.TXN.LONG")),
        _item("锁等待/阻塞", "ORA.LOCK.BLOCKING",
               rows=display_lock,
               analysis=_analysis("ORA.LOCK.BLOCKING")),
    ]
    sections.append({"section_id": "sessions_transactions", "title": "会话与事务检查", "items": st_items})

    # ── 9. 对象检查 (objects) ──
    inv_rows = ctx.tables.get("无效对象检查", [])
    display_inv: list[dict[str, Any]] = []
    for row in inv_rows[:30]:
        display_inv.append({
            "对象名": row.get("OBJECT_NAME", ""),
            "类型": row.get("OBJECT_TYPE", ""),
            "Owner": row.get("OWNER", ""),
            "状态": row.get("STATUS", ""),
        })
    obj_items: list[dict[str, Any]] = [
        _item("无效对象", "ORA.OBJECT.INVALID",
               rows=display_inv,
               analysis=_analysis("ORA.OBJECT.INVALID")),
        _item("不可用索引", "ORA.DATA.INDEX_UNUSABLE",
               analysis=_analysis("ORA.DATA.INDEX_UNUSABLE")),
        _item("统计信息缺失", "ORA.DATA.MISSING_STATS",
               analysis=_analysis("ORA.DATA.MISSING_STATS")),
    ]
    sections.append({"section_id": "objects", "title": "对象检查", "items": obj_items})

    # ── 10. 安全与审计检查 (security) ──
    pwd_rows = ctx.tables.get("用户密码过期预警（账户非OPEN或30天内到期）", []) or ctx.tables.get("用户密码过期预警", [])
    display_pwd: list[dict[str, Any]] = []
    for row in pwd_rows[:20]:
        display_pwd.append({
            "用户名": row.get("USERNAME", ""),
            "账户状态": row.get("ACCOUNT_STATUS", ""),
            "过期日期": row.get("EXPIRY_DATE", ""),
        })
    # ── DBA users + locked accounts ──
    dba_rows = ctx.tables.get("特权用户 (DBA角色)", [])
    display_dba: list[dict[str, Any]] = []
    for row in dba_rows[:20]:
        display_dba.append({
            "用户名": row.get("USERNAME",""),
            "账户状态": row.get("ACCOUNT_STATUS",""),
        })
    locked_rows = ctx.tables.get("锁定账户检查", [])
    display_locked: list[dict[str, Any]] = []
    for row in locked_rows[:20]:
        display_locked.append({
            "用户名": row.get("USERNAME",""),
            "账户状态": row.get("ACCOUNT_STATUS",""),
            "锁定日期": row.get("LOCK_DATE",""),
        })
    sec_items: list[dict[str, Any]] = [
        _item("账户密码状态", "ORA.SECURITY.PASSWORD_EXPIRY",
               rows=display_pwd,
               analysis=_analysis("ORA.SECURITY.PASSWORD_EXPIRY")),
        _item("DBA 权限用户", "ORA.SECURITY.DBA_COUNT",
               rows=display_dba,
               analysis=_analysis("ORA.SECURITY.DBA_COUNT")),
        _item("锁定账户", "ORA.SECURITY.ACCOUNT_LOCKED",
               rows=display_locked,
               analysis=_analysis("ORA.SECURITY.ACCOUNT_LOCKED")),
    ]
    sections.append({"section_id": "security", "title": "安全与审计检查", "items": sec_items})

    # ── 11. 备份与恢复检查 (backup_recovery) ──
    backup_rows = ctx.tables.get("最近RMAN备份任务状态", [])
    display_bk: list[dict[str, Any]] = []
    for row in backup_rows[:20]:
        display_bk.append({
            "备份类型": row.get("BACKUP_TYPE", ""),
            "状态": row.get("STATUS", ""),
            "开始时间": row.get("START_TIME", ""),
            "结束时间": row.get("COMPLETION_TIME", ""),
        })
    # ── ADR/ALERT display ──
    adr_rows = ctx.tables.get("ADR 预警日志路径", [])
    display_adr: list[dict[str, Any]] = []
    for row in adr_rows:
        display_adr.append({"参数": str(row.get("NAME","")).strip(), "值": str(row.get("VALUE","") or "").strip()})
    sql_err_rows = ctx.tables.get("最近 SQL 错误 (近24h)", [])
    display_err: list[dict[str, Any]] = []
    for row in sql_err_rows[:20]:
        display_err.append({
            "时间": str(row.get("ORIGINATING_TIMESTAMP",""))[:19],
            "错误码": str(row.get("ERROR_NUMBER","") or row.get("MESSAGE_GROUP","")).strip(),
            "消息": str(row.get("MESSAGE_TEXT","") or row.get("PROBLEM_KEY",""))[:60].strip(),
        })
    adr_diag_rows = ctx.tables.get("ADR Diag 路径", [])
    display_diag: list[dict[str, Any]] = []
    for row in adr_diag_rows:
        display_diag.append({"参数": str(row.get("NAME","")).strip(), "值": str(row.get("VALUE","") or "").strip()})

    br_items: list[dict[str, Any]] = [
        # ── Data layer ──
        _item("归档趋势（近30天）", "oracle.archive_trend", rows=[
            {"归档日期": str(r.get("ARCHIVE_DATE","") or r.get("DAY","")).strip(),
             "归档数量": r.get("ARCHIVE_COUNT","") or r.get("LOGS",""),
             "大小(GB)": r.get("ARCHIVE_SIZE_GB","") or r.get("SIZE_GB","")}
            for r in ctx.tables.get("近30天每日归档量", [])[:30]
        ]),
        _item("RMAN 备份配置", "oracle.rman_config", rows=[
            {"配置项": str(r.get("NAME","")).strip(), "值": str(r.get("VALUE","")).strip()}
            for r in ctx.tables.get("备份策略参数（RMAN CONFIGURE）", [])[:20]
        ]),
        _item("ADR 诊断路径", "oracle.adr_diag", rows=display_diag),
        _item("近期 SQL 错误（24h）", "oracle.recent_sql_errors", rows=display_err),
        # ── Analysis layer ──
        _item("RMAN 备份检查", "ORA.BACKUP.RECENT",
               rows=display_bk,
               analysis=_analysis("ORA.BACKUP.RECENT")),
    ]

    # Block corruption
    bad_rows = ctx.tables.get("数据库坏块记录", [])
    display_bad: list[dict[str, Any]] = []
    for row in bad_rows[:10]:
        display_bad.append({
            "文件号": row.get("FILE#", ""),
            "块号": row.get("BLOCK#", ""),
            "坏块数": row.get("BLOCKS", ""),
            "变更号": row.get("CORRUPTION_CHANGE#", ""),
        })
    br_items.append(_item("物理坏块", "ORA.DATA.BLOCK_CORRUPTION",
                           rows=display_bad,
                           analysis=_analysis("ORA.DATA.BLOCK_CORRUPTION")))

    # Datafile header
    df_rows = ctx.find_table("数据文件头异常", "datafile_header")
    display_df: list[dict[str, Any]] = []
    for row in df_rows[:20]:
        display_df.append({
            "文件号": row.get("FILE#", ""),
            "状态": row.get("STATUS", ""),
            "错误": row.get("ERROR", ""),
        })
    br_items.append(_item("数据文件头状态", "ORA.DATA.FILE_HEADER",
                           rows=display_df,
                           analysis=_analysis("ORA.DATA.FILE_HEADER")))

    # Control file redundancy
    ctrl_rows = ctx.tables.get("控制文件多路复用", [])
    display_ctrl: list[dict[str, Any]] = []
    for row in ctrl_rows[:10]:
        display_ctrl.append({
            "参数": row.get("NAME", ""),
            "值": row.get("VALUE", ""),
        })
    br_items.append(_item("控制文件冗余", "ORA.CONTROL.REDUNDANCY",
                           rows=display_ctrl,
                           analysis=_analysis("ORA.CONTROL.REDUNDANCY")))

    sections.append({"section_id": "backup_recovery", "title": "备份与恢复检查", "items": br_items})

    return sections

def build_report_model(analysis: dict[str, Any], inspection_sections_list: list[list[dict[str, Any]]], output: Path) -> dict[str, Any]:
    """Build the final report_model.json matching MySQL v3 contract."""
    instances = analysis.get("instances", [])
    primary = instances[0] if instances else {}
    identity = primary.get("identity", {})
    generated = analysis.get("analyzer", {}).get("generated_at", now_iso())
    quality = primary.get("collection_quality", {})
    findings_all = [f for inst in instances for f in inst.get("findings", [])]

    # ── cover ──
    db_name = identity.get("db_name") or identity.get("instance_tag", "Oracle Database")
    ora_ver = identity.get("ora_version", "")
    cover = {
        "title": "Oracle 数据库巡检分析报告",
        "inspection_target": db_name,
        "database_version": ora_ver,
        "report_version": "V2.0",
        "inspection_date": (generated or "")[:10],
    }

    # ── document_control ──
    document_control = {
        "customer": "待填写",
        "database": "Oracle",
        "report_version": "V2.0",
        "generated_at": generated,
    }

    # ── overview ──
    tag = identity.get("instance_tag", "")
    hostname, ip_addr = parse_instance_tag(tag)
    overview = {
        "host": hostname or identity.get("host", ""),
        "ip": ip_addr,
        "database_version": ora_ver,
        "collection_time": identity.get("collection_started_at", generated),
        "data_quality": quality,
    }

    # ── topology ──
    nodes = [{
        "hostname": hostname or identity.get("host", ""),
        "ip": ip_addr,
        "port": int(identity.get("port", 1521)),
        "role_observed": identity.get("db_role", "PRIMARY"),
        "version": ora_ver,
        "instance_tag": identity.get("instance_tag", db_name),
    }]
    topology = {
        "mode": "single_instance" if len(instances) <= 1 else "multi_instance",
        "nodes": nodes,
        "edges": [],
    }

    # ── health_assessment ──
    health = primary.get("health_summary", {})
    counts = health.get("counts", {})
    health_assessment = {
        "score": health.get("score", 100),
        "grade": health.get("grade", "good"),
        "counts": {
            "critical": counts.get("critical", 0),
            "high": counts.get("high", 0),
            "medium": counts.get("medium", 0),
            "low": counts.get("low", 0),
        },
        "scoring_policy": health.get("scoring_policy", ""),
    }

    # ── comprehensive_conclusions ──
    comprehensive_conclusions = primary.get("comprehensive_conclusions", [])

    # ── risk_register ──
    risk_register = findings_all

    # ── optimization_plan ──
    priorities: dict[str, list[dict[str, Any]]] = {"P1": [], "P2": [], "P3": []}
    for f in findings_all:
        sev = f.get("severity", "low")
        pri = "P1" if sev in ("critical", "high") else "P2" if sev == "medium" else "P3"
        priorities[pri].append({
            "finding_id": f.get("finding_id", ""),
            "title": f.get("title", ""),
            "recommendation": f.get("recommendation", ""),
        })

    # ── collection_gaps ──
    collection_gaps: list[dict[str, Any]] = []
    gap_status_cn: dict[str, str] = {
        "partial": "部分可用", "permission_denied": "权限不足",
        "timeout": "超时", "error": "失败",
        "insufficient_history": "历史不足",
        "external_evidence_required": "需要外部证据",
    }
    for item in quality.get("non_ok_items", []):
        status = str(item.get("status", ""))
        if status in {"ok", "empty", "not_applicable"}:
            continue
        collection_gaps.append({
            "item_id": item.get("item_id", ""),
            "status": status,
            "reason": item.get("reason", ""),
            "recommended_action": "修复采集条件后重采；已取得的部分数据仍保留为证据。",
            "collector_change_required": False,
        })

    # ── appendix ──
    sampling = primary.get("metrics", {}).get("sampling_context", {})
    appendix = {
        "collection_window": {
            "realtime_window_seconds": sampling.get("realtime", {}).get("sample_count"),
            "oracle_sample_points": sampling.get("realtime", {}).get("oracle_stat_points"),
            "short_window": False,
            "history": sampling.get("history", {}),
        },
        "data_quality": quality,
        "rule_evaluations": primary.get("rule_evaluations", []),
        "disclaimer": "本报告基于采集窗口内可获得的证据自动生成。短时采样不代表全天负载；未采集或证据不足的项目不作通过结论，变更前应完成业务确认、备份与回滚评估。",
    }

    # ── charts ──
    charts_raw = primary.get("charts", [])
    charts_normalized: list[dict[str, Any]] = []
    for c in charts_raw:
        path_str = str(c.get("path", ""))
        # Convert absolute path to relative (for report portability)
        rel_path = path_str
        if path_str:
            try:
                rel_path = str(Path(path_str).relative_to(output))
            except ValueError:
                rel_path = Path(path_str).name
        charts_normalized.append({
            "chart_id": c.get("chart_id", ""),
            "status": "generated",
            "file": c.get("path", ""),
            "source_points": c.get("source_points", 0),
        })

    return {
        "schema_version": "2.0",
        "generator_contract": "oracle_inspection_report_model",
        "cover": cover,
        "document_control": document_control,
        "overview": overview,
        "topology": topology,
        "health_assessment": health_assessment,
        "comprehensive_conclusions": comprehensive_conclusions,
        "inspection_sections": inspection_sections_list[0] if inspection_sections_list else [],
        "risk_register": risk_register,
        "optimization_plan": priorities,
        "collection_gaps": collection_gaps,
        "charts": charts_normalized,
        "appendix": appendix,
    }

# ---- Main Pipeline ----

