#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Oracle inspection rule engine — mirrors MySQL rules.py architecture.

Config-driven, threshold-externalized. Evaluates all defined rules
against PackageContext + derived metrics using the same Finding /
RuleEvaluation dataclasses as the analyzer.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dataclasses import dataclass, field

from .metrics import safe_float, safe_int
from .package_adapter import OraclePackageContext as PackageContext, filesystem_type
from .presentation import Finding
from inspection_core.system_checks import (
    CANONICAL_RULE_IDS,
    DEFAULT_THRESHOLDS,
    is_persistent_fstype,
    os_pressure_report,
)

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
    # 规则**未触发**时，实测值原本被丢弃：触发才有 Finding、Finding 里才带 facts，
    # 于是"通过"的规则在报告里只剩一句"检查通过，未发现异常"，依据列全落成
    # 「未提供补充依据」。这里把调用方传入的 facts 一并留下来 —— 展示层据此给
    # 通过项写出实测值（如「最高文件系统使用率：33.0%」），而不是让读者猜。
    facts: list[str] = field(default_factory=list)
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
            "facts": self.facts,
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

        # ── 扩展规则 v2.2（批 11：补"采了没判"的域） ──
        # DataGuard 6 项采集此前 0 条判据；对象治理 / 口令 Profile / 大页同病。
        self._check_dg_transport(ctx)
        self._check_dg_lag(ctx, metrics)
        self._check_dg_log_gap(ctx)
        self._check_dg_events(ctx)
        self._check_segment_bloat(ctx)
        self._check_large_segment(ctx)
        self._check_password_profile(ctx)
        self._check_hugepages(ctx)

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
        confidence_override: float | None = None,
        severity_override: str | None = None,
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
        if confidence_override is not None:
            # Data-source driven (SAR history vs. a short live sample).
            confidence = confidence_override
        if severity_override is not None:
            # Two-tier rule: the pack declares the base level, the caller
            # promotes it when the harder threshold is crossed.  This is the
            # only way a per-rule second threshold can reach the finding — the
            # status itself stays a single triggered/passed bit.
            severity = severity_override

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
            # 未触发也留证据：展示层用它给"通过"项写出实测值。
            facts=list(facts or []),
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
        rule = CANONICAL_RULE_IDS["time_sync"]
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
        """CPU / IO wait / memory pressure, judged by the shared OS layer.

        Same three checks MySQL ships; the window, peak criterion, thresholds
        and verdict semantics all come from ``inspection_core.system_checks``.
        """
        local = metrics.get("scope", {}).get("database_target_is_local", False)
        source, verdicts = os_pressure_report(
            metrics, include=("cpu_pressure", "iowait_pressure", "memory_pressure"),
        )
        reason_note = source["reason"]
        for verdict in verdicts:
            self._evaluate(
                verdict["rule_id"], local, verdict["available"], verdict["triggered"],
                reason_note,
                [verdict["fact"]] if verdict["fact"] else [],
                confidence_override=source["confidence"],
            )

    def _check_filesystem_usage(self, ctx: PackageContext, metrics: dict[str, Any]) -> None:
        rule = CANONICAL_RULE_IDS["filesystem_usage"]
        local = metrics.get("scope", {}).get("database_target_is_local", False)
        all_rows = ctx.tables.get("filesystems", [])
        # 容量判据只看**持久化**文件系统。光驱（iso9660/squashfs/udf）是只读介质、
        # 恒定 100% 满且 0 可用，tmpfs/devtmpfs/proc/sysfs 根本没有"容量"概念 ——
        # 把它们算进最大值只会制造客户既无法确认也无法处置的"风险"（真踩过：一份
        # 单机库报告因为光驱被判「文件系统使用率过高 100%」）。
        # 判据与展示层、MySQL 侧**同源**（inspection_core.system_checks.is_persistent_fstype），
        # 不在这里再维护一份黑名单 —— 此处写一份、展示层写一份，两边迟早不一致。
        fs_rows = [row for row in all_rows if is_persistent_fstype(filesystem_type(row))]
        excluded = len(all_rows) - len(fs_rows)
        max_pct = 0.0
        for row in fs_rows:
            pct = safe_float(row.get("USE%")) or safe_float(row.get("used_pct")) or 0
            max_pct = max(max_pct, pct)
        t = DEFAULT_THRESHOLDS["filesystem_usage_critical"]
        fact = f"最高文件系统使用率：{max_pct:.1f}%"
        if excluded:
            # 口径要能自证：客户看到"最高 33%"会问"我 fs 表里有 9 行，凭什么只算这个"。
            fact += f"（已排除 {excluded} 个 tmpfs/devtmpfs/光驱等非持久化挂载点）"
        self._evaluate(
            rule, local, bool(fs_rows),
            max_pct >= t,
            "检查所有已成功读取的持久化文件系统",
            [fact] if fs_rows else [],
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
        promote = "critical" if (pct is not None and pct >= t_crit) else None
        self._evaluate(
            rule, True, pct is not None,
            triggered,
            f"{source_label} {pct:.1f}%，超过{'严重' if promote else '告警'}阈值" if triggered else f"{source_label} {pct:.1f}%，正常" if pct is not None else "无数据",
            # 实测值进报告「依据」列，写成人能读的：`max_capacity_pct=3.387451171875`
            # 那种原始浮点既占地方又要读者自己换算。
            [f"{source_label} {pct:.1f}%"] if pct is not None else [],
            severity_override=promote,
        )

    def _check_asm_usage(self, metrics: dict[str, Any]) -> None:
        rule = "ORA.STORAGE.ASM"
        pct = metrics.get("asm_max_used_pct")
        t = self._threshold(rule, "asm_warning_pct", 80)
        self._evaluate(
            rule, True, pct is not None,
            pct is not None and pct >= t,
            f"ASM 最大使用率 {pct:.1f}%" if pct else "无 ASM",
            [f"ASM 最大使用率 {pct:.1f}%"] if pct is not None else [],
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
        min_members = self._threshold(rule, "redo_member_min", 2)
        below_min = 0
        for row in rows:
            raw = str(row.get("MEMBER_COUNT", "") or row.get("MEMBERS", "1")).strip()
            try:
                if int(raw) < min_members:
                    below_min += 1
            except ValueError:
                pass
        triggered = below_min > 0
        self._evaluate(
            rule, True, True, triggered,
            f"{below_min} 个日志组成员数少于 {min_members}" if triggered else f"所有日志组成员数均不少于 {min_members}",
            [f"groups_below_min_members={below_min}"],
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
        t_crit = self._threshold(rule, "library_cache_hit_critical", 90)
        triggered = sql_hit < t
        promote = "critical" if sql_hit < t_crit else None
        self._evaluate(
            rule, True, True, triggered,
            f"SQL AREA GETHIT_PCT={sql_hit:.1f}%，低于{'严重' if promote else '告警'}阈值" if triggered else f"SQL AREA GETHIT_PCT={sql_hit:.1f}%",
            [f"sql_area_gethit_pct={sql_hit:.1f}"],
            severity_override=promote,
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

    # ── 批 11：DataGuard / 对象治理 / 口令 Profile / 大页 ──
    #
    # 背景：这些域的采集项在包内一直有数据，但规则侧**一条判据都没有**（见
    # `_work/oracle-analysis-gap-review.md` §3）。补充时守两条既有口径：
    #   * 「未采集 ≠ 0」—— 表为空一律落 not_evaluated，不当成"没问题"；
    #   * 表键同时给 2.0 的 `item_id` 与 1.4 的中文标题，`find_table` 两套包都命中。

    def _check_dg_transport(self, ctx: PackageContext) -> None:
        """归档 / DG 传输目的地报错（v$archive_dest，主库也适用）。"""
        rule = "ORA.DG.TRANSPORT_ERROR"
        rows = ctx.find_table("oracle.dg.archive_dest", "日志传输服务状态")
        broken = [
            row for row in rows
            if str(row.get("ERROR", "")).strip().upper() not in {"", "-", "0", "NULL", "NONE"}
        ]
        facts = [
            f"{row.get('DEST_NAME') or row.get('dest_name')}: {row.get('ERROR')}"
            for row in broken[:5]
        ]
        self._evaluate(
            rule, True, bool(rows), bool(broken),
            f"{len(broken)} 个归档目的地报错" if broken
            else (f"{len(rows)} 个归档目的地状态正常" if rows else "未采集到归档目的地"),
            facts,
        )

    def _check_dg_lag(self, ctx: PackageContext, metrics: dict[str, Any]) -> None:
        """备库传输 / 应用延迟（v$dataguard_stats 的 transport lag / apply lag）。

        单实例（无备库）时该视图为空 —— 落 not_evaluated，不当作"延迟正常"。
        """
        rule = "ORA.DG.APPLY_LAG"
        rows = ctx.find_table("oracle.dg.protection_mode", "保护模式与主库统计")
        if not rows:
            self._evaluate(rule, True, False, False, "无备库统计（未配置 DataGuard）", [])
            return
        lags: dict[str, float] = {}
        for row in rows:
            name = str(row.get("NAME") or row.get("name") or "").strip().lower()
            seconds = _parse_interval_seconds(row.get("VALUE") or row.get("value"))
            if name and seconds is not None:
                lags[name] = seconds
        if not lags:
            self._evaluate(rule, True, False, False, "统计中无可用延迟值", [])
            return
        worst_key = max(lags, key=lambda key: lags[key])
        worst = lags[worst_key]
        threshold = self._threshold(rule, "dg_lag_seconds_warning", 900)
        triggered = worst > threshold
        self._evaluate(
            rule, True, True, triggered,
            f"最大延迟 {worst_key}={worst:.0f}s" + ("，超过阈值" if triggered else "，正常"),
            [f"{key}={value:.0f}s" for key, value in sorted(lags.items())],
        )

    def _check_dg_log_gap(self, ctx: PackageContext) -> None:
        """主备库归档日志序差距（备库落后主库多少个归档）。"""
        rule = "ORA.DG.LOG_GAP"
        rows = ctx.find_table("oracle.dg.log_sequence_gap", "主备库日志序差距")
        if not rows:
            self._evaluate(rule, True, False, False, "无备库（未配置 DataGuard）", [])
            return
        gaps = [safe_int(row.get("GAP") or row.get("gap")) for row in rows]
        gaps = [gap for gap in gaps if gap is not None]
        if not gaps:
            self._evaluate(rule, True, False, False, "无法解析日志序差距", [])
            return
        worst = max(gaps)
        threshold = self._threshold(rule, "dg_log_gap_warning", 5)
        triggered = worst > threshold
        self._evaluate(
            rule, True, True, triggered,
            f"最大日志序差距 {worst}" + ("，备库落后" if triggered else "，正常"),
            [f"max_gap={worst}", f"threads={len(gaps)}"],
        )

    def _check_dg_events(self, ctx: PackageContext) -> None:
        """近 24 小时 DataGuard 事件里是否有非零错误码。"""
        rule = "ORA.DG.EVENT_ERROR"
        rows = ctx.find_table("oracle.dg.recent_events", "最近 DG 事件")
        broken = [
            row for row in rows
            if safe_int(row.get("ERROR_CODE") or row.get("error_code")) not in (None, 0)
        ]
        facts = [
            f"{row.get('TIMESTAMP')} {row.get('MESSAGE')}: {row.get('ERROR_CODE')}"
            for row in broken[:5]
        ]
        self._evaluate(
            rule, True, bool(rows), bool(broken),
            f"近 24h {len(broken)} 条 DG 错误事件" if broken
            else (f"近 24h {len(rows)} 条 DG 事件均无错误" if rows else "近 24h 无 DG 事件记录"),
            facts,
        )

    def _check_segment_bloat(self, ctx: PackageContext) -> None:
        """段空间膨胀：实际占用 - 按统计信息估算的应有大小。"""
        rule = "ORA.SCHEMA.SEGMENT_BLOAT"
        rows = ctx.find_table("oracle.awr.segment_bloat", "数据膨胀预估")
        if not rows:
            self._evaluate(rule, True, False, False, "未采集到段膨胀数据（需统计信息）", [])
            return
        threshold = self._threshold(rule, "segment_bloat_mb_warning", 1024)
        worst, worst_name = 0.0, ""
        for row in rows:
            bloat = safe_float(row.get("BLOAT_MB")) or 0.0
            if bloat > worst:
                worst = bloat
                worst_name = f"{row.get('OWNER')}.{row.get('TABLE_NAME')}"
        triggered = worst >= threshold
        self._evaluate(
            rule, True, True, triggered,
            f"最大可回收空间 {worst:.1f}MB（{worst_name}）" + ("，超过阈值" if triggered else ""),
            [f"max_bloat_mb={worst:.2f}", f"candidates={len(rows)}"],
        )

    def _check_large_segment(self, ctx: PackageContext) -> None:
        """超大表 / 索引段（表的 SIZE_GB 与索引的 SIZE_MB 统一折成 GB 比较）。"""
        rule = "ORA.CAPACITY.LARGE_SEGMENT"
        table_rows = ctx.find_table("oracle.basic.large_segments", "大表段")
        index_rows = ctx.find_table("oracle.index.large_segments", "大索引段")
        if not table_rows and not index_rows:
            self._evaluate(rule, True, False, False, "未采集到大段数据", [])
            return
        worst_gb, worst = 0.0, ""
        for row in table_rows:
            gb = safe_float(row.get("SIZE_GB")) or 0.0
            if gb > worst_gb:
                worst_gb = gb
                worst = f"{row.get('OWNER')}.{row.get('TABLE_NAME')}"
        for row in index_rows:
            gb = (safe_float(row.get("SIZE_MB")) or 0.0) / 1024.0
            if gb > worst_gb:
                worst_gb = gb
                worst = f"{row.get('OWNER')}.{row.get('INDEX_NAME')}（索引）"
        threshold = self._threshold(rule, "large_segment_gb_warning", 100)
        triggered = worst_gb >= threshold
        self._evaluate(
            rule, True, True, triggered,
            f"最大段 {worst_gb:.2f}GB（{worst}）" + ("，超过阈值" if triggered else ""),
            [f"max_segment_gb={worst_gb:.3f}"],
        )

    def _check_password_profile(self, ctx: PackageContext) -> None:
        """口令 Profile 弱策略：无复杂度校验 / 口令永不过期 / 失败次数不受限。"""
        rule = "ORA.SECURITY.PASSWORD_PROFILE"
        rows = ctx.find_table("oracle.audit.profile_params", "密码配置文件参数")
        if not rows:
            self._evaluate(rule, True, False, False, "未采集到 Profile 参数", [])
            return
        weak: list[str] = []
        for row in rows:
            name = str(row.get("RESOURCE_NAME") or "").strip().upper()
            limit = str(row.get("LIMIT") or "").strip().upper()
            profile = str(row.get("PROFILE") or "").strip()
            if name == "PASSWORD_VERIFY_FUNCTION" and limit in {"", "NULL", "NONE"}:
                weak.append(f"{profile}.PASSWORD_VERIFY_FUNCTION 未启用")
            elif name == "FAILED_LOGIN_ATTEMPTS" and limit in {"", "UNLIMITED"}:
                weak.append(f"{profile}.FAILED_LOGIN_ATTEMPTS 不受限")
            elif name == "PASSWORD_LIFE_TIME" and limit in {"", "UNLIMITED"}:
                weak.append(f"{profile}.PASSWORD_LIFE_TIME 永不过期")
        self._evaluate(
            rule, True, True, bool(weak),
            f"{len(weak)} 项口令策略偏弱" if weak else "口令 Profile 策略正常",
            weak[:6],
        )

    def _check_hugepages(self, ctx: PackageContext) -> None:
        """透明大页 / HugePages —— **Oracle 口径**，与 MySQL 只判 THP 不同。

        Oracle 既要 THP 关闭（`never`），又要 HugePages 承载 SGA；而"配了却闲置"
        （Total>0 但 Free 占比高）说明实例没有真正用上静态大页，同样要报。
        """
        rule = "ORA.SYSTEM.HUGEPAGES"
        states = _hugepage_states(ctx)
        enabled = states.get("enabled", "")
        if not enabled:
            self._evaluate(rule, True, False, False, "未采集到透明大页状态", [])
            return
        total = safe_int(states.get("total")) or 0
        free = safe_int(states.get("free")) or 0
        facts = [f"transparent_hugepage/enabled={enabled}"]
        if states.get("defrag"):
            facts.append(f"transparent_hugepage/defrag={states['defrag']}")
        facts.append(f"HugePages_Total={total}")
        if total:
            facts.append(f"HugePages_Free={free}")
        threshold = self._threshold(rule, "hugepage_free_ratio_warning", 0.5)
        thp_bad = enabled != "never"
        unused = total > 0 and (free / total) > threshold
        reasons: list[str] = []
        if thp_bad:
            reasons.append(f"透明大页未关闭（{enabled}）")
        if unused:
            reasons.append(f"已配 HugePages 但闲置 {free / total * 100:.0f}%")
        self._evaluate(
            rule, True, True, bool(reasons),
            "；".join(reasons) if reasons else "透明大页已关闭",
            facts,
        )


def _parse_interval_seconds(value: Any) -> float | None:
    """Oracle 间隔型统计值 → 秒。

    `v$dataguard_stats` 的 `value` 形如 ``+00 00:00:05``（天 时:分:秒），个别版本带
    小数秒。解析不出来（``NULL`` / 空 / 纯数字异常）就返回 None —— 让调用方落
    not_evaluated，不拿 0 冒充"无延迟"。
    """
    text = str(value or "").strip()
    if not text or text.upper() in {"NULL", "NONE", "-"}:
        return None
    match = re.match(r"^([+-]?)(\d+)\s+(\d+):(\d+):(\d+(?:\.\d+)?)$", text)
    if match:
        sign = -1.0 if match.group(1) == "-" else 1.0
        days, hours, minutes, seconds = (
            float(match.group(2)), float(match.group(3)),
            float(match.group(4)), float(match.group(5)),
        )
        return sign * (days * 86400 + hours * 3600 + minutes * 60 + seconds)
    # 少数环境直接给秒数
    try:
        return float(text)
    except ValueError:
        return None


def _hugepage_states(ctx: PackageContext) -> dict[str, str]:
    """还原 THP / HugePages 状态（`tables/hugepages.tsv`，真 TAB 表，列 `path`/`value`）。

    `value` 形如 ``[never]`` / ``always madvise [never]`` —— 方括号里的是当前生效值。
    `/proc/meminfo:` 前缀的几行是批 2 新增的**实测值**，只有 Total 与 Free 对照才
    算得出"配了但没被用掉"。
    """
    states: dict[str, str] = {}
    for row in ctx.tables.get("hugepages", []) or []:
        path = str(row.get("path") or "").strip()
        raw = str(row.get("value") or "").strip()
        if not path:
            continue
        if path.endswith("nr_hugepages"):
            states["nr_hugepages"] = raw
            continue
        if path.endswith("meminfo:HugePages_Total"):
            states["total"] = raw
            continue
        if path.endswith("meminfo:HugePages_Free"):
            states["free"] = raw
            continue
        if "transparent_hugepage" not in path:
            continue
        active = ""
        for token in raw.split():
            if token.startswith("[") and token.endswith("]"):
                active = token.strip("[]")
                break
        if not active:
            active = raw.split()[0] if raw.split() else ""
        states["enabled" if path.endswith("/enabled") else "defrag"] = active
    return states
