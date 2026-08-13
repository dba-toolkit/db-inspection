#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Oracle inspection rule engine — mirrors MySQL rules.py architecture.

Config-driven, threshold-externalized. Evaluates all defined rules
against PackageContext + derived metrics using the same Finding /
RuleEvaluation dataclasses as the analyzer.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dataclasses import dataclass, field

from .metrics import safe_float, safe_int
from .package_adapter import OraclePackageContext as PackageContext
from .presentation import Finding

RULES_CONFIG = Path(__file__).resolve().parent / "inspection_rules_oracle.json"


def load_rules_config(path: Path | None = None) -> dict[str, Any]:
    target = path or RULES_CONFIG
    with target.open("r", encoding="utf-8") as f:
        config = json.load(f)
    if not isinstance(config, dict):
        raise ValueError(f"Rules config must be a JSON object: {target}")
    if config.get("version") != "2.0":
        raise ValueError(f"Unsupported rules config version: {config.get('version')}")
    return config


@dataclass
class RuleEvaluation:
    rule_id: str = ""
    category: str = ""
    title: str = ""
    severity_if_triggered: str = "medium"
    status: str = "not_evaluated"  # passed | triggered | not_evaluated | not_applicable
    reason: str = ""
    finding_id: str | None = None
    evidence_refs: list[str] = field(default_factory=list)
    confidence: float = 1.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "category": self.category,
            "title": self.title,
            "severity": self.severity_if_triggered,
            "status": self.status,
            "reason": self.reason,
            "finding_id": self.finding_id,
            "evidence_refs": self.evidence_refs,
            "confidence": self.confidence,
        }


class OracleRuleEngine:
    """Evaluates all Oracle rules from config against one PackageContext + metrics.

    Usage::
        engine = OracleRuleEngine(config_path)
        findings, evaluations = engine.run(ctx, metrics, quality)
    """

    def __init__(self, config_path: Path | None = None) -> None:
        self.config = load_rules_config(config_path)
        self._globals = self.config.get("globals", {})
        self._rule_defs = self.config.get("rules", {})
        self._findings: list[Finding] = []
        self._evaluations: list[RuleEvaluation] = []

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------

    def run(
        self, ctx: PackageContext, metrics: dict[str, Any], quality: dict[str, Any]
    ) -> tuple[list[Finding], list[RuleEvaluation]]:
        self._findings = []
        self._evaluations = []

        # Determine scope from metrics (addded by analyzer's derive_metrics)
        is_local = metrics.get("scope", {}).get("database_target_is_local", False)
        has_history = metrics.get("sampling_context", {}).get("history", {}).get("usable_for_trend_rules", False)

        self._check_collection_integrity(ctx)
        self._check_collection_quality(quality)
        self._check_time_sync(ctx, metrics)
        self._check_system_resources(ctx, metrics)
        self._check_filesystem_usage(ctx, metrics)
        self._check_tablespace_usage(metrics)
        self._check_asm_usage(metrics)
        self._check_fra_usage(ctx, metrics)
        self._check_invalid_objects(metrics)
        self._check_long_transactions(metrics)
        self._check_lock_waiting(metrics)
        self._check_archive_mode(ctx)
        self._check_backup_recent(metrics)
        self._check_buffer_cache(metrics)
        self._check_sort_disk(metrics)
        self._check_block_corruption(ctx)
        self._check_datafile_anomalies(ctx)
        self._check_control_redundancy(ctx)
        self._check_scheduler_failures(ctx)
        self._check_checksum_params(ctx)
        self._check_password_expiry(ctx)

        # ── 扩展规则 v2.1 ──
        self._check_redo_member(ctx)
        self._check_redo_size(ctx)
        self._check_library_cache(ctx, metrics)
        self._check_wait_events(ctx)
        self._check_autoextend(ctx)
        self._check_dba_count(ctx)
        self._check_account_locked(ctx)
        self._check_sql_execution(ctx)
        self._check_index_unusable(ctx)
        self._check_missing_stats(ctx)
        self._check_io_hotfile(ctx)

        # Assign finding IDs
        order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
        self._findings.sort(key=lambda x: (order.get(x.severity, 99), x.category))
        for i, f in enumerate(self._findings, 1):
            f.finding_id = f"F-{i:03d}"

        # Link evaluation finding_ids
        for ev in self._evaluations:
            for f in self._findings:
                if f.rule_id == ev.rule_id:
                    ev.finding_id = f.finding_id
                    break

        return self._findings, self._evaluations

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _cfg(self, rule_id: str) -> dict[str, Any]:
        return self._rule_defs.get(rule_id, {})

    def _threshold(self, rule_id: str, key: str, default: Any = None) -> Any:
        return self._cfg(rule_id).get("threshold", {}).get(key, self._globals.get(key, default))

    def _evaluate(
        self,
        rule_id: str,
        applicable: bool,
        available: bool,
        triggered: bool,
        reason: str,
        facts: list[str],
    ) -> None:
        """Core evaluation dispatcher — mirrors MySQL _evaluate."""
        cfg = self._cfg(rule_id)
        category: str = cfg.get("category", rule_id.split(".")[0])
        severity: str = cfg.get("severity", "medium")
        title: str = cfg.get("title", rule_id)
        summary: str = cfg.get("summary", "")
        recommendation: str = cfg.get("recommendation", "")
        evidence: list[str] = cfg.get("evidence_refs", [])
        confidence: float = float(cfg.get("confidence", 1.0))

        if not applicable:
            status = "not_applicable"
        elif not available:
            status = "not_evaluated"
        elif triggered:
            status = "triggered"
        else:
            status = "passed"

        finding_id = None
        if status == "triggered":
            finding_id = f"R{len(self._findings) + 1:03d}"
            self._findings.append(Finding(
                rule_id=rule_id, severity=severity, title=title, category=category,
                summary=summary, facts=facts, recommendation=recommendation,
                evidence_refs=evidence, status="triggered", confidence=confidence,
                finding_id=finding_id, triggered=True,
            ))
        self._evaluations.append(RuleEvaluation(
            rule_id=rule_id, category=category, status=status, reason=reason,
            severity_if_triggered=severity, finding_id=finding_id,
            evidence_refs=evidence, confidence=confidence,
        ))

    # ------------------------------------------------------------------
    # rule implementations
    # ------------------------------------------------------------------

    def _check_collection_integrity(self, ctx: PackageContext) -> None:
        rule = "ORA.COLLECTION.INTEGRITY"
        ok = ctx.integrity.get("status") == "ok"
        self._evaluate(
            rule, True, True, not ok,
            "清单哈希与文件完整性校验",
            [f"失败文件数：{ctx.integrity.get('failure_count', 0)}"],
        )

    def _check_collection_quality(self, quality: dict[str, Any]) -> None:
        rule = "ORA.COLLECTION.QUALITY"
        score = quality.get("score", 100)
        min_score = self._threshold(rule, "quality_score_min", 80)
        self._evaluate(
            rule, True, True, score < min_score,
            f"采集完整度 {score:.1f}%",
            [f"数据完整度：{score:.1f}%"],
        )

    def _check_time_sync(self, ctx: PackageContext, metrics: dict[str, Any]) -> None:
        rule = "ORA.SYSTEM.TIME_SYNC"
        local = metrics.get("scope", {}).get("database_target_is_local", False)
        te = ctx.snapshot.get("time_evidence", {})
        ntp = str(te.get("ntp_synchronized", "")).lower()
        self._evaluate(
            rule, local, bool(ntp),
            ntp in {"no", "false", "0", "inactive"},
            f"NTP synchronized={ntp or 'unknown'}",
            [f"NTP synchronized：{ntp or 'unknown'}"],
        )

    def _check_system_resources(self, ctx: PackageContext, metrics: dict[str, Any]) -> None:
        local = metrics.get("scope", {}).get("database_target_is_local", False)
        history_usable = metrics.get("sampling_context", {}).get("history", {}).get("usable_for_trend_rules", False)
        source = metrics["system_history"] if history_usable else metrics.get("system_realtime", {})
        conf = 0.9 if history_usable else 0.65
        reason_note = "使用有效 SAR 历史" if history_usable else "仅使用现场短时样本，结论置信度较低"

        # CPU
        cpu_max = source.get("cpu_busy_pct", {}).get("max")
        threshold = self._threshold("ORA.SYSTEM.CPU_PRESSURE", "cpu_peak_warning", 90)
        self._evaluate(
            "ORA.SYSTEM.CPU_PRESSURE", local, cpu_max is not None,
            cpu_max is not None and cpu_max >= threshold,
            reason_note,
            [f"CPU 峰值：{cpu_max:.1f}%"] if cpu_max is not None else [],
        )
        if self._evaluations:
            self._evaluations[-1].confidence = conf

        # IO wait
        iowait_max = source.get("cpu_iowait_pct", {}).get("max")
        threshold = self._threshold("ORA.SYSTEM.IOWAIT_PRESSURE", "iowait_peak_warning", 20)
        self._evaluate(
            "ORA.SYSTEM.IOWAIT_PRESSURE", local, iowait_max is not None,
            iowait_max is not None and iowait_max >= threshold,
            reason_note,
            [f"IO wait 峰值：{iowait_max:.1f}%"] if iowait_max is not None else [],
        )
        if self._evaluations:
            self._evaluations[-1].confidence = conf

        # Memory
        mem_avg = source.get("memory_used_pct", {}).get("average")
        threshold = self._threshold("ORA.SYSTEM.MEMORY_PRESSURE", "memory_usage_warning", 90)
        self._evaluate(
            "ORA.SYSTEM.MEMORY_PRESSURE", local, mem_avg is not None,
            mem_avg is not None and mem_avg >= threshold,
            reason_note,
            [f"内存平均使用率：{mem_avg:.1f}%"] if mem_avg is not None else [],
        )
        if self._evaluations:
            self._evaluations[-1].confidence = conf

    def _check_filesystem_usage(self, ctx: PackageContext, metrics: dict[str, Any]) -> None:
        rule = "ORA.CAPACITY.FILESYSTEM_USAGE"
        local = metrics.get("scope", {}).get("database_target_is_local", False)
        fs_rows = ctx.tables.get("filesystems", [])
        max_pct = 0.0
        for row in fs_rows:
            pct = safe_float(row.get("USE%")) or safe_float(row.get("used_pct")) or 0
            max_pct = max(max_pct, pct)
        t = self._threshold(rule, "filesystem_usage_critical", 90)
        self._evaluate(
            rule, local, bool(fs_rows),
            max_pct >= t,
            "检查所有已成功读取的文件系统",
            [f"最高文件系统使用率：{max_pct:.1f}%"] if fs_rows else [],
        )

    def _check_tablespace_usage(self, metrics: dict[str, Any]) -> None:
        rule = "ORA.STORAGE.TABLESPACE"
        # Prefer capacity-based % (USED/MAX) over allocation-based (USED/ALLOC)
        pct = metrics.get("tablespace_max_capacity_pct")
        source_label = "容量使用率"
        if pct is None:
            pct = metrics.get("tablespace_max_used_pct")
            source_label = "分配使用率"
        t_warn = self._threshold(rule, "tablespace_warning_pct", 80)
        t_crit = self._threshold(rule, "tablespace_critical_pct", 95)
        triggered = pct is not None and pct >= t_warn
        severity_override = "critical" if (pct is not None and pct >= t_crit) else None
        self._evaluate(
            rule, True, pct is not None,
            triggered,
            f"{source_label} {pct:.1f}%，超过{'严重' if severity_override else '告警'}阈值" if triggered else f"{source_label} {pct:.1f}%，正常" if pct is not None else "无数据",
            [f"max_{'capacity' if source_label=='容量使用率' else 'used'}_pct={pct}"] if pct is not None else [],
        )

    def _check_asm_usage(self, metrics: dict[str, Any]) -> None:
        rule = "ORA.STORAGE.ASM"
        pct = metrics.get("asm_max_used_pct")
        t = self._threshold(rule, "asm_warning_pct", 80)
        self._evaluate(
            rule, True, pct is not None,
            pct is not None and pct >= t,
            f"ASM 最大使用率 {pct:.1f}%" if pct else "无 ASM",
            [f"max_used_pct={pct}"] if pct is not None else [],
        )

    def _check_fra_usage(self, ctx: PackageContext, metrics: dict[str, Any]) -> None:
        rule = "ORA.STORAGE.FRA"
        fra_rows = ctx.find_table("FRA", "闪回区", "快速恢复区")
        pct = None
        for row in fra_rows:
            pct = safe_float(row.get("PERCENT_SPACE_USED")) or safe_float(row.get("PCT"))
        t = self._threshold(rule, "fra_warning_pct", 80)
        self._evaluate(
            rule, True, pct is not None,
            pct is not None and pct >= t,
            f"FRA 使用率 {pct:.1f}%" if pct else "无 FRA 记录",
            [f"used_pct={pct}"] if pct is not None else [],
        )

    def _check_invalid_objects(self, metrics: dict[str, Any]) -> None:
        rule = "ORA.OBJECT.INVALID"
        count = metrics.get("invalid_object_count", 0)
        self._evaluate(
            rule, True, True, count > 0,
            f"发现 {count} 个无效对象" if count else "对象状态正常",
            [f"invalid_count={count}"],
        )

    def _check_long_transactions(self, metrics: dict[str, Any]) -> None:
        rule = "ORA.TXN.LONG"
        count = metrics.get("long_transaction_count", 0)
        self._evaluate(
            rule, True, True, count > 0,
            f"发现 {count} 个长时间事务" if count else "无长事务",
            [f"count={count}"],
        )

    def _check_lock_waiting(self, metrics: dict[str, Any]) -> None:
        rule = "ORA.LOCK.BLOCKING"
        count = metrics.get("lock_wait_count", 0)
        self._evaluate(
            rule, True, True, count > 0,
            f"发现 {count} 组锁等待" if count else "无锁等待",
            [f"count={count}"],
        )

    def _check_archive_mode(self, ctx: PackageContext) -> None:
        rule = "ORA.ARCHIVE.MODE"
        archive_rows = ctx.find_table("归档模式检查", "归档模式", "archive_mode")
        # Handle both tab-separated and pipe-separated TSV formats.
        # Critical: "NOARCHIVELOG" contains "ARCHIVELOG" as substring — must exclude!
        def _has_archivelog(row) -> bool:
            for v in row.values():
                s = str(v).upper()
                if "ARCHIVELOG" in s and "NOARCHIVELOG" not in s:
                    return True
            return False
        is_archivelog = any(_has_archivelog(r) for r in archive_rows)
        if not archive_rows:
            is_archivelog = True  # assume OK if not checked
        self._evaluate(
            rule, True, bool(archive_rows),
            not is_archivelog,
            "ARCHIVELOG" if is_archivelog else "NOARCHIVELOG",
            ["LOG_MODE=NOARCHIVELOG"] if not is_archivelog else [],
        )

    def _check_backup_recent(self, metrics: dict[str, Any]) -> None:
        rule = "ORA.BACKUP.RECENT"
        has_backup = metrics.get("has_recent_backup", False)
        self._evaluate(
            rule, True, True, not has_backup,
            "近 48h 无成功备份" if not has_backup else "备份正常",
            [],
        )

    def _check_buffer_cache(self, metrics: dict[str, Any]) -> None:
        rule = "ORA.PERFORMANCE.BUFFER_CACHE"
        hit_pct = metrics.get("oracle_realtime", {}).get("buffer_cache_hit_pct")
        t = self._threshold(rule, "buffer_cache_hit_warning", 90)
        self._evaluate(
            rule, True, hit_pct is not None,
            hit_pct is not None and hit_pct < t,
            f"命中率 {hit_pct:.1f}%" if hit_pct else "无采样数据",
            [f"hit_pct={hit_pct}"] if hit_pct is not None else [],
        )

    def _check_sort_disk(self, metrics: dict[str, Any]) -> None:
        rule = "ORA.PERFORMANCE.SORT_DISK"
        ratio = metrics.get("oracle_realtime", {}).get("sort_disk_ratio_pct")
        t = self._threshold(rule, "sort_disk_warning_pct", 5)
        self._evaluate(
            rule, True, ratio is not None,
            ratio is not None and ratio > t,
            f"磁盘排序 {ratio:.1f}%" if ratio else "排序数据不足",
            [f"sort_disk_pct={ratio}"] if ratio is not None else [],
        )

    def _check_block_corruption(self, ctx: PackageContext) -> None:
        rule = "ORA.DATA.BLOCK_CORRUPTION"
        rows = ctx.tables.get("数据库坏块记录", []) or ctx.tables.get("block_corruption", [])
        self._evaluate(
            rule, True, True, len(rows) > 0,
            f"发现 {len(rows)} 条坏块记录" if rows else "未发现坏块",
            [f"count={len(rows)}"],
        )

    def _check_datafile_anomalies(self, ctx: PackageContext) -> None:
        rule = "ORA.DATA.FILE_HEADER"
        rows = ctx.find_table("数据文件头异常", "datafile_header")
        # Only count rows where STATUS indicates a real problem
        bad_rows = [r for r in rows
                    if str(r.get("STATUS","")).upper().strip() not in ("ONLINE", "", "NORMAL")]
        triggered = len(bad_rows) > 0
        self._evaluate(
            rule, True, bool(rows),
            triggered,
            f"发现 {len(bad_rows)} 个数据文件头异常" if triggered else ("所有文件正常" if rows else "无数据"),
            [f"bad_count={len(bad_rows)}, total={len(rows)}"] if triggered else [],
        )

    def _check_control_redundancy(self, ctx: PackageContext) -> None:
        rule = "ORA.CONTROL.REDUNDANCY"
        rows = ctx.tables.get("控制文件多路复用", []) or ctx.tables.get("controlfile_redundancy", [])
        count = 0
        for row in rows:
            v = safe_int(row.get("CONTROLFILE_COUNT"))
            if v is not None:
                count = v
                break
        self._evaluate(
            rule, True, count > 0, count < 2,
            f"控制文件 {count} 份" if count else "未采集",
            [f"count={count}"],
        )

    def _check_scheduler_failures(self, ctx: PackageContext) -> None:
        rule = "ORA.JOB.FAILURE"
        rows = ctx.find_table("Scheduler 失败", "Scheduler失败", "scheduler_failures")
        self._evaluate(
            rule, True, True, len(rows) > 0,
            f"近 7 天 {len(rows)} 个失败作业" if rows else "无失败作业",
            [f"count={len(rows)}"],
        )

    def _check_checksum_params(self, ctx: PackageContext) -> None:
        rule = "ORA.DATA.CHECKSUM"
        rows = ctx.tables.get("块校验与丢失写保护参数", []) or ctx.tables.get("checksum_params", [])
        bad_params = [r for r in rows if str(r.get("VALUE", "")).upper() in {"OFF", "FALSE", "NONE"}]
        self._evaluate(
            rule, True, bool(rows), len(bad_params) > 0,
            f"{len(bad_params)} 个参数未开启" if bad_params else "正常",
            [f"params={[r.get('NAME') for r in bad_params]}"] if bad_params else [],
        )

    def _check_password_expiry(self, ctx: PackageContext) -> None:
        rule = "ORA.SECURITY.PASSWORD_EXPIRY"
        rows = (ctx.tables.get("用户密码过期预警（账户非OPEN或30天内到期）", [])
                or ctx.tables.get("用户密码过期预警", [])
                or ctx.tables.get("password_expiry", []))
        self._evaluate(
            rule, True, True, len(rows) > 0,
            f"{len(rows)} 个账户需关注" if rows else "正常",
            [f"count={len(rows)}"],
        )

    # ── 扩展规则（利用已采集但未使用的 TSV 表） ──

    def _check_redo_member(self, ctx: PackageContext) -> None:
        rule = "ORA.CONFIG.REDO_MEMBER"
        rows = ctx.tables.get("Redo 日志多路复用检查", [])
        if not rows:
            self._evaluate(rule, True, False, False, "无 redo log 多路复用数据", [])
            return
        single_members = 0
        for row in rows:
            raw = str(row.get("MEMBER_COUNT", "") or row.get("MEMBERS", "1")).strip()
            try:
                if int(raw) < 2:
                    single_members += 1
            except ValueError:
                pass
        triggered = single_members > 0
        self._evaluate(
            rule, True, True, triggered,
            f"{single_members} 个日志组为单成员" if triggered else "所有日志组已多路复用",
            [f"single_member_groups={single_members}"],
        )

    def _check_redo_size(self, ctx: PackageContext) -> None:
        rule = "ORA.CONFIG.REDO_SIZE"
        rows = ctx.tables.get("Redo 日志组信息", [])
        if not rows:
            self._evaluate(rule, True, False, False, "无 redo log 数据", [])
            return
        sizes = [safe_float(r.get("SIZE_MB")) for r in rows if safe_float(r.get("SIZE_MB")) is not None]
        if not sizes:
            self._evaluate(rule, True, False, False, "无法解析 redo 尺寸", [])
            return
        min_size = min(sizes)
        t = self._threshold(rule, "redo_size_mb_min", 500)
        triggered = min_size < t
        facts = [f"min_redo_size={min_size:.0f}MB, groups={len(sizes)}"]
        switch_rows = ctx.tables.get("重做日志切换频率（近24小时）", [])
        if switch_rows:
            for row in switch_rows:
                switches = safe_float(row.get("SWITCHES_PER_HOUR"))
                if switches is not None and switches > self._threshold(rule, "redo_switch_per_hour_warning", 4):
                    facts.append(f"switches_per_hour={switches:.1f}")
                    triggered = True
        self._evaluate(
            rule, True, True, triggered,
            f"最小 redo {min_size:.0f}MB" + ("，切换频率偏高" if triggered else ""),
            facts,
        )

    def _check_library_cache(self, ctx: PackageContext, metrics: dict[str, Any]) -> None:
        rule = "ORA.PERFORMANCE.LIBRARY_CACHE"
        rows = ctx.tables.get("Library Cache 命中率", [])
        if not rows:
            self._evaluate(rule, True, False, False, "无 Library Cache 数据", [])
            return
        sql_hit = None
        for row in rows:
            if str(row.get("NAMESPACE", "")).strip().upper() == "SQL AREA":
                sql_hit = safe_float(row.get("GETHIT_PCT"))
                break
        if sql_hit is None:
            self._evaluate(rule, True, False, False, "无法定位 SQL AREA 命中率", [])
            return
        t = self._threshold(rule, "library_cache_hit_warning", 95)
        triggered = sql_hit < t
        self._evaluate(
            rule, True, True, triggered,
            f"SQL AREA GETHIT_PCT={sql_hit:.1f}%",
            [f"sql_area_gethit_pct={sql_hit:.1f}"],
        )

    def _check_wait_events(self, ctx: PackageContext) -> None:
        rule = "ORA.PERFORMANCE.WAIT_EVENTS"
        rows = ctx.tables.get("等待事件 TOP10（数据库级）", [])
        if not rows:
            self._evaluate(rule, True, False, False, "无等待事件数据", [])
            return
        total_wait = 0.0
        top_event = ""
        for row in rows:
            wt = safe_float(row.get("TIME_WAITED"))
            if wt is not None:
                total_wait += wt
                if not top_event:
                    top_event = str(row.get("EVENT", "")).strip()
        t = self._threshold(rule, "wait_time_seconds_warning", 60)
        triggered = total_wait > t
        self._evaluate(
            rule, True, True, triggered,
            f"累计等待 {total_wait:.0f}s, TOP1: {top_event}" if triggered else f"累计等待 {total_wait:.0f}s，正常",
            [f"total_wait_seconds={total_wait:.1f}", f"top_event={top_event}"],
        )

    def _check_autoextend(self, ctx: PackageContext) -> None:
        rule = "ORA.CONFIG.AUTOEXTEND"
        rows = ctx.tables.get("数据文件自动扩展余量", [])
        if not rows:
            self._evaluate(rule, True, False, False, "无数据文件自动扩展数据", [])
            return
        no_auto = 0
        for row in rows:
            af = safe_int(row.get("AUTOEXTEND_FILES"))
            df = safe_int(row.get("DATAFILE_COUNT"))
            if af is not None and df is not None and af < df:
                no_auto += (df - af)
        triggered = no_auto > 0
        self._evaluate(
            rule, True, True, triggered,
            f"{no_auto} 个数据文件未开启自动扩展" if triggered else "所有数据文件已开启自动扩展",
            [f"non_autoextend_files={no_auto}"],
        )

    def _check_dba_count(self, ctx: PackageContext) -> None:
        rule = "ORA.SECURITY.DBA_COUNT"
        rows = ctx.tables.get("特权用户 (DBA角色)", [])
        t = self._threshold(rule, "dba_user_count_warning", 5)
        count = len(rows)
        triggered = count > t
        self._evaluate(
            rule, True, True, triggered,
            f"{count} 个 DBA 角色用户" + ("，超出建议范围" if triggered else ""),
            [f"dba_user_count={count}"],
        )

    def _check_account_locked(self, ctx: PackageContext) -> None:
        rule = "ORA.SECURITY.ACCOUNT_LOCKED"
        rows = ctx.tables.get("用户列表", [])
        if not rows:
            self._evaluate(rule, True, False, False, "无用户列表数据", [])
            return
        locked = [r for r in rows if "LOCKED" in str(r.get("ACCOUNT_STATUS", "")).upper()]
        count = len(locked)
        t = self._threshold(rule, "locked_account_count_warning", 20)
        triggered = count > t
        self._evaluate(
            rule, True, True, triggered,
            f"{count} 个锁定账户" + ("，建议清理" if triggered else ""),
            [f"locked_accounts={count}"],
        )

    def _check_sql_execution(self, ctx: PackageContext) -> None:
        rule = "ORA.PERFORMANCE.SQL_EXECUTION"
        rows = ctx.tables.get("Top 20 SQL (按执行时间)", [])
        if not rows:
            self._evaluate(rule, True, False, False, "无 Top SQL 数据", [])
            return
        max_elapsed = 0.0
        max_sql = ""
        for row in rows:
            elapsed = safe_float(row.get("ELAPSED_MIN"))
            if elapsed is not None:
                max_elapsed = max(max_elapsed, elapsed)
                if not max_sql:
                    max_sql = str(row.get("SQL_ID", "")).strip()
        t = self._threshold(rule, "single_sql_elapsed_min_warning", 10)
        triggered = max_elapsed > t
        self._evaluate(
            rule, True, True, triggered,
            f"最大单 SQL 执行 {max_elapsed:.1f}min, SQL_ID={max_sql}" if triggered else f"SQL 执行时间正常（最大 {max_elapsed:.1f}min）",
            [f"max_elapsed_min={max_elapsed:.1f}", f"sql_id={max_sql}"],
        )

    def _check_index_unusable(self, ctx: PackageContext) -> None:
        rule = "ORA.DATA.INDEX_UNUSABLE"
        rows = ctx.tables.get("无效_不可用索引", [])
        self._evaluate(
            rule, True, True, len(rows) > 0,
            f"发现 {len(rows)} 个不可用索引" if rows else "索引状态正常",
            [f"unusable_count={len(rows)}"],
        )

    def _check_missing_stats(self, ctx: PackageContext) -> None:
        rule = "ORA.DATA.MISSING_STATS"
        rows = ctx.tables.get("缺失统计信息表 (TOP20)", [])
        count = len(rows)
        t = self._threshold(rule, "missing_stats_table_count_warning", 10)
        triggered = count > t
        self._evaluate(
            rule, True, True, triggered,
            f"{count} 个表缺失统计信息" if triggered else ("统计信息正常" if not rows else f"{count} 个表缺失统计信息，未超阈值"),
            [f"missing_stats_count={count}"],
        )

    def _check_io_hotfile(self, ctx: PackageContext) -> None:
        rule = "ORA.PERFORMANCE.IO_HOTFILE"
        rows = ctx.tables.get("IO 消耗最高数据文件 TOP10", [])
        if not rows:
            self._evaluate(rule, True, False, False, "无 IO 数据文件数据", [])
            return
        total_io = 0.0
        max_io = 0.0
        for row in rows:
            io_val = safe_float(row.get("TOTAL_IO")) or 0
            total_io += io_val
            max_io = max(max_io, io_val)
        if total_io == 0:
            self._evaluate(rule, True, True, False, "IO 活动极低", [])
            return
        pct = (max_io / total_io * 100) if total_io > 0 else 0
        t = self._threshold(rule, "single_file_io_pct_warning", 30)
        triggered = pct > t
        self._evaluate(
            rule, True, True, triggered,
            f"TOP1 数据文件 IO 占比 {pct:.1f}%" + ("，存在热点" if triggered else "，分布正常"),
            [f"max_file_io_pct={pct:.1f}"],
        )
