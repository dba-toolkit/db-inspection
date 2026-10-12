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
from .package_adapter import (
    OraclePackageContext as PackageContext,
    filesystem_type,
    fetch_state,
    prefer_state,
    state_is_collected,
)
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
    # 这条判据的**数据来源**在采集侧的声明状态（`ok`/`empty`/`partial`/`unavailable`…）。
    # ★ 规则项在报告里也印「采集状态」一行，此前那一行读的是 `quality.non_ok_items`
    #   （只记失败态、且按**采集项** id 索引）⇒ 规则 id 永远查不到 ⇒ 无论底层表是
    #   「查了没有」还是「根本没采到」，一律印成「正常」（批 19 的 C 项）。
    #   状态从判据自己身上带出来，是唯一不会与真实采集情况脱钩的做法。
    source_status: str = ""

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
            "source_status": self.source_status,
        }


def _collected(metrics: dict[str, Any], key: str) -> tuple[Any, bool, str]:
    """取一个「来自采集包表格的指标」→ `(值, 采集过吗, 采集侧状态)`。

    ★ 状态键**缺失一律按「没采到」处理**（fail-safe）：宁可写「证据不足」，不可写「检查通过」。
      这是批 19 判据 A 的落点 —— 从前 `metrics.get(key, 0)` 在「取数键写错」「这项没采」
      「查了 0 行」三种情况下都得到 `0`，报告一律写「检查通过」，其中前两种是**漏报**。
    """
    state = str((metrics.get("source_status") or {}).get(key) or "")
    return metrics.get(key, 0), state_is_collected(state), state


def _missing_reason(what: str, state: str) -> str:
    """取数没落地时报告里那句结论 —— **「没采到」与「查了没有」不许长得一样**。

    `empty`（查了、确认 0 行）是**查过了**，只是这次没有记录；`unavailable` 才是没采到。
    两者被写成同一句话，读者就无法区分「没问题」与「不知道」（批 14 立的规矩，批 19 补上取数层）。
    """
    if state_is_collected(state):
        return f"{what}已检查，本次未发现相关记录"
    return f"未采集到{what}（采集包内无对应表）"


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
        source_status: str = "",
        confidence_override: float | None = None,
        severity_override: str | None = None,
    ) -> None:
        """Core evaluation dispatcher — mirrors MySQL _evaluate.

        `source_status` = 这条判据的**数据来源在采集侧的声明状态**，随 `RuleEvaluation`
        一路带到报告里（规则项那行「采集状态」）。空串表示「来源不是采集包里的表」。
        """
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
            source_status=source_status,
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
            "ok" if ntp else "unavailable",
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
                "ok" if verdict["available"] else "unavailable",
                confidence_override=source["confidence"],
            )

    def _check_filesystem_usage(self, ctx: PackageContext, metrics: dict[str, Any]) -> None:
        rule = CANONICAL_RULE_IDS["filesystem_usage"]
        local = metrics.get("scope", {}).get("database_target_is_local", False)
        # `filesystems` 是 1.4 时代的键；2.0 从 `evidence/filesystems.txt` 回填时同时登记
        # `system.filesystems` / `filesystems`。取数走 `table_fetch`（精确键），
        # 命中不到就是「这一项没采」（1.4 包实测如此）—— 不拿空表冒充「查了没问题」。
        fetch = ctx.table_fetch("filesystems", "system.filesystems")
        all_rows = fetch.rows
        state = fetch_state(fetch)
        collected = state_is_collected(state)
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
            rule, local, collected and bool(fs_rows),
            max_pct >= t,
            "检查所有已成功读取的持久化文件系统" if collected
            else _missing_reason("文件系统清单（df -PT）", state),
            [fact] if fs_rows else [],
            state,
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
            str((metrics.get("source_status") or {}).get("tablespace_max_capacity_pct") or ""),
            severity_override=promote,
        )

    def _check_asm_usage(self, metrics: dict[str, Any]) -> None:
        rule = "ORA.STORAGE.ASM"
        pct, collected, state = _collected(metrics, "asm_max_used_pct")
        t = self._threshold(rule, "asm_warning_pct", 80)
        # ★ `if pct` 会把**真的 0%**（磁盘组刚建、还没放数据）吞成"无 ASM" ⇒ 一律 `is not None`。
        usable = collected and pct is not None
        self._evaluate(
            rule, True, usable,
            usable and pct >= t,
            f"ASM 最大使用率 {pct:.1f}%" if usable else _missing_reason("ASM 磁盘组使用情况", state),
            [f"ASM 最大使用率 {pct:.1f}%"] if usable else [],
            state,
        )

    def _check_fra_usage(self, ctx: PackageContext, metrics: dict[str, Any]) -> None:
        rule = "ORA.STORAGE.FRA"
        # ★ 从前这里是 `ctx.find_table("FRA", "闪回区", "快速恢复区")`：**子串兜底**先把
        #   "FRA" 匹配到 `快速恢复区（FRA）参数`（只有 NAME/VALUE，没有 PERCENT_SPACE_USED）
        #   ⇒ 命中了**错的表**，使用率恒 None ⇒ 这条规则在两种包形态上都**永不触发**
        #   （批 19 注入实测：给真正的使用率表灌 95% 也不触发，即静默漏报）。
        #   真实落点有两处（普通实例 `V$FLASH_RECOVERY_AREA_USAGE` / ASM 实例
        #   `V$RECOVERY_AREA_USAGE`），按 2.0 item_id 与 1.4 标题列全。
        usage_fetch = ctx.table_fetch("oracle.flashback.area_usage", "闪回区使用情况")
        asm_fetch = ctx.table_fetch("oracle.archive.fra_asm", "ASM 快速恢复区使用情况")
        state = prefer_state(fetch_state(usage_fetch), fetch_state(asm_fetch))
        pcts = [value for value in
                (_fra_percent(row) for row in [*usage_fetch.rows, *asm_fetch.rows])
                if value is not None]
        # 多个文件类型各占一行 ⇒ 取**最大**。原实现取"最后一行"，报不报取决于视图返回顺序。
        pct = max(pcts) if pcts else None
        t = self._threshold(rule, "fra_warning_pct", 80)
        usable = pct is not None
        self._evaluate(
            rule, True, usable,
            usable and pct >= t,
            f"FRA 使用率 {pct:.1f}%" if usable else _missing_reason("快速恢复区（FRA）使用情况", state),
            [f"FRA 使用率 {pct:.1f}%"] if usable else [],
            state,
        )

    def _check_invalid_objects(self, metrics: dict[str, Any]) -> None:
        rule = "ORA.OBJECT.INVALID"
        count, collected, state = _collected(metrics, "invalid_object_count")
        self._evaluate(
            rule, True, collected, count > 0,
            (f"发现 {count} 个无效对象" if count else "对象状态正常") if collected
            else _missing_reason("无效对象统计", state),
            [f"invalid_count={count}"] if collected else [],
            state,
        )

    def _check_long_transactions(self, metrics: dict[str, Any]) -> None:
        rule = "ORA.TXN.LONG"
        count, collected, state = _collected(metrics, "long_transaction_count")
        self._evaluate(
            rule, True, collected, count > 0,
            (f"发现 {count} 个长时间事务" if count else "无长事务") if collected
            else _missing_reason("长事务检查", state),
            [f"count={count}"] if collected else [],
            state,
        )

    def _check_lock_waiting(self, metrics: dict[str, Any]) -> None:
        rule = "ORA.LOCK.BLOCKING"
        count, collected, state = _collected(metrics, "lock_wait_count")
        self._evaluate(
            rule, True, collected, count > 0,
            (f"发现 {count} 组锁等待" if count else "无锁等待") if collected
            else _missing_reason("锁等待链", state),
            [f"count={count}"] if collected else [],
            state,
        )

    def _check_archive_mode(self, ctx: PackageContext) -> None:
        rule = "ORA.ARCHIVE.MODE"
        fetch = ctx.table_fetch("归档模式检查", "oracle.archive.mode", "archive_mode")
        archive_rows = fetch.rows
        state = fetch_state(fetch)
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
            # 空表不许直接落成「没开归档」：`available` 已经把它收口到 not_evaluated，
            # 这里只负责别把 reason 写成一句客户会当真结论的话。
            is_archivelog = True
        self._evaluate(
            rule, True, state_is_collected(state) and bool(archive_rows),
            not is_archivelog,
            "ARCHIVELOG" if is_archivelog else "NOARCHIVELOG",
            ["LOG_MODE=NOARCHIVELOG"] if not is_archivelog else [],
            state,
        )

    def _check_backup_recent(self, metrics: dict[str, Any]) -> None:
        rule = "ORA.BACKUP.RECENT"
        has_backup, collected, state = _collected(metrics, "has_recent_backup")
        self._evaluate(
            rule, True, collected, not has_backup,
            ("备份正常" if has_backup else "近 48h 无成功备份") if collected
            else _missing_reason("最近备份集汇总", state),
            [],
            state,
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
        fetch = ctx.table_fetch("数据库坏块记录", "oracle.validation.corruption", "block_corruption")
        rows = fetch.rows
        self._evaluate(
            rule, True, fetch.available, len(rows) > 0,
            (f"发现 {len(rows)} 条坏块记录" if rows else "未发现坏块") if fetch.available
            else _missing_reason("坏块记录（V$DATABASE_BLOCK_CORRUPTION）", fetch_state(fetch)),
            [f"count={len(rows)}"] if fetch.available else [],
            fetch_state(fetch),
        )

    def _check_datafile_anomalies(self, ctx: PackageContext) -> None:
        rule = "ORA.DATA.FILE_HEADER"
        fetch = ctx.table_fetch("数据文件头异常", "oracle.validation.datafile_header", "datafile_header")
        rows = fetch.rows
        # Only count rows where STATUS indicates a real problem
        bad_rows = [r for r in rows
                    if str(r.get("STATUS","")).upper().strip() not in ("ONLINE", "", "NORMAL")]
        triggered = len(bad_rows) > 0
        self._evaluate(
            rule, True, bool(rows),
            triggered,
            f"发现 {len(bad_rows)} 个数据文件头异常" if triggered else (
                "所有文件正常" if rows else _missing_reason("数据文件头状态", fetch_state(fetch))),
            [f"bad_count={len(bad_rows)}, total={len(rows)}"] if triggered else [],
            fetch_state(fetch),
        )

    def _check_control_redundancy(self, ctx: PackageContext) -> None:
        rule = "ORA.CONTROL.REDUNDANCY"
        fetch = ctx.table_fetch("控制文件多路复用", "oracle.validation.controlfile_multiplex",
                                "controlfile_redundancy")
        rows = fetch.rows
        count = 0
        for row in rows:
            v = safe_int(row.get("CONTROLFILE_COUNT"))
            if v is not None:
                count = v
                break
        self._evaluate(
            rule, True, count > 0, count < 2,
            f"控制文件 {count} 份" if count else _missing_reason("控制文件多路复用", fetch_state(fetch)),
            [f"count={count}"],
            fetch_state(fetch),
        )

    def _check_scheduler_failures(self, ctx: PackageContext) -> None:
        rule = "ORA.JOB.FAILURE"
        fetch = ctx.table_fetch("Scheduler 失败作业（近7天）", "oracle.validation.scheduler_failed",
                                "Scheduler失败", "scheduler_failures")
        rows = fetch.rows
        self._evaluate(
            rule, True, fetch.available, len(rows) > 0,
            (f"近 7 天 {len(rows)} 个失败作业" if rows else "无失败作业") if fetch.available
            else _missing_reason("Scheduler 失败作业", fetch_state(fetch)),
            [f"count={len(rows)}"] if fetch.available else [],
            fetch_state(fetch),
        )

    def _check_checksum_params(self, ctx: PackageContext) -> None:
        rule = "ORA.DATA.CHECKSUM"
        fetch = ctx.table_fetch("块校验与丢失写保护参数", "oracle.validation.block_checksum",
                                "checksum_params")
        rows = fetch.rows
        bad_params = [r for r in rows if str(r.get("VALUE", "")).upper() in {"OFF", "FALSE", "NONE"}]
        self._evaluate(
            rule, True, fetch.available, len(bad_params) > 0,
            (f"{len(bad_params)} 个参数未开启" if bad_params else "正常") if fetch.available
            else _missing_reason("块校验与丢失写保护参数", fetch_state(fetch)),
            [f"params={[r.get('NAME') for r in bad_params]}"] if bad_params else [],
            fetch_state(fetch),
        )

    def _check_password_expiry(self, ctx: PackageContext) -> None:
        rule = "ORA.SECURITY.PASSWORD_EXPIRY"
        # ★ 这三个键从前是 `or` 链手写的，而**每一条都可能命中不同版本**：
        #   2.0 是 `oracle.basic.password_expiry`、1.4 是中文标题文件；键写错时 `or` 链
        #   静默落到下一条、最后返回 `[]`，与"查了没有"不可分。改走 `table_fetch` 后
        #   命中哪个键、键的真实状态是什么都是可查的（批 19 的 B 项）。
        fetch = ctx.table_fetch(
            "用户密码过期预警（账户非OPEN或30天内到期）", "oracle.basic.password_expiry",
            "password_expiry",
        )
        rows = fetch.rows
        self._evaluate(
            rule, True, fetch.available, len(rows) > 0,
            (f"{len(rows)} 个账户需关注" if rows else "正常") if fetch.available
            else _missing_reason("账户密码过期预警", fetch_state(fetch)),
            [f"count={len(rows)}"] if fetch.available else [],
            fetch_state(fetch),
        )

    # ── 扩展规则（利用已采集但未使用的 TSV 表） ──

    def _check_redo_member(self, ctx: PackageContext) -> None:
        rule = "ORA.CONFIG.REDO_MEMBER"
        fetch = ctx.table_fetch("Redo 日志多路复用检查", "oracle.validation.redo_multiplex")
        state = fetch_state(fetch)
        rows = fetch.rows
        if not rows:
            self._evaluate(rule, True, False, False,
                           _missing_reason("Redo 日志多路复用检查", state), [], state)
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
            state,
        )

    def _check_redo_size(self, ctx: PackageContext) -> None:
        rule = "ORA.CONFIG.REDO_SIZE"
        fetch = ctx.table_fetch("Redo 日志组信息", "oracle.basic.redo_groups")
        state = fetch_state(fetch)
        rows = fetch.rows
        if not rows:
            self._evaluate(rule, True, False, False,
                           _missing_reason("Redo 日志组信息", state), [], state)
            return
        sizes = [safe_float(r.get("SIZE_MB")) for r in rows if safe_float(r.get("SIZE_MB")) is not None]
        if not sizes:
            self._evaluate(rule, True, False, False, "无法解析 redo 尺寸", [], state)
            return
        min_size = min(sizes)
        t = self._threshold(rule, "redo_size_mb_min", 500)
        triggered = min_size < t
        facts = [f"min_redo_size={min_size:.0f}MB, groups={len(sizes)}"]
        switch_fetch = ctx.table_fetch("重做日志切换频率（近24小时）", "oracle.archive.log_switch_rate")
        for row in switch_fetch.rows:
            switches = safe_float(row.get("SWITCHES_PER_HOUR"))
            if switches is not None and switches > self._threshold(rule, "redo_switch_per_hour_warning", 4):
                facts.append(f"switches_per_hour={switches:.1f}")
                triggered = True
        self._evaluate(
            rule, True, True, triggered,
            f"最小 redo {min_size:.0f}MB" + ("，切换频率偏高" if triggered else ""),
            facts,
            # 数据来源是**两张表**：主判据看日志组尺寸，切换频率是加分项 ⇒ 状态取二者中
            # 「有内容」的那个（有尺寸就说有数据；只有切换频率有内容也不说没采到）。
            prefer_state(state, fetch_state(switch_fetch)),
        )

    def _check_library_cache(self, ctx: PackageContext, metrics: dict[str, Any]) -> None:
        rule = "ORA.PERFORMANCE.LIBRARY_CACHE"
        fetch = ctx.table_fetch("Library Cache 命中率", "oracle.performance.library_cache_hit")
        state = fetch_state(fetch)
        rows = fetch.rows
        if not rows:
            self._evaluate(rule, True, False, False,
                           _missing_reason("Library Cache 命中率", state), [], state)
            return
        sql_hit = None
        for row in rows:
            if str(row.get("NAMESPACE", "")).strip().upper() == "SQL AREA":
                sql_hit = safe_float(row.get("GETHIT_PCT"))
                break
        if sql_hit is None:
            self._evaluate(rule, True, False, False, "无法定位 SQL AREA 命中率", [], state)
            return
        t = self._threshold(rule, "library_cache_hit_warning", 95)
        t_crit = self._threshold(rule, "library_cache_hit_critical", 90)
        triggered = sql_hit < t
        promote = "critical" if sql_hit < t_crit else None
        self._evaluate(
            rule, True, True, triggered,
            f"SQL AREA GETHIT_PCT={sql_hit:.1f}%，低于{'严重' if promote else '告警'}阈值" if triggered else f"SQL AREA GETHIT_PCT={sql_hit:.1f}%",
            [f"sql_area_gethit_pct={sql_hit:.1f}"],
            state,
            severity_override=promote,
        )

    def _check_wait_events(self, ctx: PackageContext) -> None:
        rule = "ORA.PERFORMANCE.WAIT_EVENTS"
        fetch = ctx.table_fetch("等待事件 TOP10（数据库级）", "oracle.performance.wait_top10")
        state = fetch_state(fetch)
        rows = fetch.rows
        if not rows:
            self._evaluate(rule, True, False, False,
                           _missing_reason("等待事件 TOP10（数据库级）", state), [], state)
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
            state,
        )

    def _check_autoextend(self, ctx: PackageContext) -> None:
        rule = "ORA.CONFIG.AUTOEXTEND"
        fetch = ctx.table_fetch("数据文件自动扩展余量", "oracle.validation.autoextend")
        state = fetch_state(fetch)
        rows = fetch.rows
        if not rows:
            self._evaluate(rule, True, False, False,
                           _missing_reason("数据文件自动扩展余量", state), [], state)
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
            state,
        )

    def _check_dba_count(self, ctx: PackageContext) -> None:
        rule = "ORA.SECURITY.DBA_COUNT"
        fetch = ctx.table_fetch("特权用户 (DBA角色)", "oracle.audit.privileged_users")
        rows = fetch.rows
        t = self._threshold(rule, "dba_user_count_warning", 5)
        count = len(rows)
        triggered = count > t
        # 「DBA 用户 0 个」是**查过了的确定结论**（`empty` 算采集过）⇒ 照旧给 passed；
        # 只有这一项**没采到**才落 not_evaluated。
        self._evaluate(
            rule, True, fetch.available, triggered,
            (f"{count} 个 DBA 角色用户" + ("，超出建议范围" if triggered else "")) if fetch.available
            else _missing_reason("特权用户（DBA 角色）", fetch_state(fetch)),
            [f"dba_user_count={count}"] if fetch.available else [],
            fetch_state(fetch),
        )

    def _check_account_locked(self, ctx: PackageContext) -> None:
        rule = "ORA.SECURITY.ACCOUNT_LOCKED"
        fetch = ctx.table_fetch("用户列表", "oracle.basic.users")
        state = fetch_state(fetch)
        rows = fetch.rows
        if not rows:
            self._evaluate(rule, True, False, False,
                           _missing_reason("用户列表", state), [], state)
            return
        locked = [r for r in rows if "LOCKED" in str(r.get("ACCOUNT_STATUS", "")).upper()]
        count = len(locked)
        t = self._threshold(rule, "locked_account_count_warning", 20)
        triggered = count > t
        self._evaluate(
            rule, True, True, triggered,
            f"{count} 个锁定账户" + ("，建议清理" if triggered else ""),
            [f"locked_accounts={count}"],
            state,
        )

    def _check_sql_execution(self, ctx: PackageContext) -> None:
        rule = "ORA.PERFORMANCE.SQL_EXECUTION"
        fetch = ctx.table_fetch("Top 20 SQL (按执行时间)", "oracle.topsql.elapsed_time")
        state = fetch_state(fetch)
        rows = fetch.rows
        if not rows:
            self._evaluate(rule, True, False, False,
                           _missing_reason("Top SQL（按执行时间）", state), [], state)
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
            state,
        )

    def _check_index_unusable(self, ctx: PackageContext) -> None:
        rule = "ORA.DATA.INDEX_UNUSABLE"
        fetch = ctx.table_fetch("无效_不可用索引", "oracle.index.invalid")
        rows = fetch.rows
        usable = fetch.available
        self._evaluate(
            rule, True, usable, len(rows) > 0,
            (f"发现 {len(rows)} 个不可用索引" if rows else "索引状态正常") if usable
            else _missing_reason("无效/不可用索引", fetch_state(fetch)),
            [f"unusable_count={len(rows)}"] if usable else [],
            fetch_state(fetch),
        )

    def _check_missing_stats(self, ctx: PackageContext) -> None:
        rule = "ORA.DATA.MISSING_STATS"
        fetch = ctx.table_fetch("缺失统计信息表 (TOP20)", "oracle.awr.missing_stats")
        rows = fetch.rows
        count = len(rows)
        t = self._threshold(rule, "missing_stats_table_count_warning", 10)
        triggered = count > t
        usable = fetch.available
        self._evaluate(
            rule, True, usable, triggered,
            (f"{count} 个表缺失统计信息" if triggered
             else ("统计信息正常" if not rows else f"{count} 个表缺失统计信息，未超阈值")) if usable
            else _missing_reason("缺失统计信息表（TOP20）", fetch_state(fetch)),
            [f"missing_stats_count={count}"] if usable else [],
            fetch_state(fetch),
        )

    def _check_io_hotfile(self, ctx: PackageContext) -> None:
        rule = "ORA.PERFORMANCE.IO_HOTFILE"
        fetch = ctx.table_fetch("IO 消耗最高数据文件 TOP10", "oracle.performance.io_files")
        state = fetch_state(fetch)
        rows = fetch.rows
        if not rows:
            self._evaluate(rule, True, False, False,
                           _missing_reason("IO 消耗最高数据文件 TOP10", state), [], state)
            return
        total_io = 0.0
        max_io = 0.0
        for row in rows:
            io_val = safe_float(row.get("TOTAL_IO")) or 0
            total_io += io_val
            max_io = max(max_io, io_val)
        if total_io == 0:
            self._evaluate(rule, True, True, False, "IO 活动极低", [], state)
            return
        pct = (max_io / total_io * 100) if total_io > 0 else 0
        t = self._threshold(rule, "single_file_io_pct_warning", 30)
        triggered = pct > t
        self._evaluate(
            rule, True, True, triggered,
            f"TOP1 数据文件 IO 占比 {pct:.1f}%" + ("，存在热点" if triggered else "，分布正常"),
            [f"max_file_io_pct={pct:.1f}"],
            state,
        )

    # ── 批 11：DataGuard / 对象治理 / 口令 Profile / 大页 ──
    #
    # 背景：这些域的采集项在包内一直有数据，但规则侧**一条判据都没有**（见
    # `_work/oracle-analysis-gap-review.md` §3）。补充时守两条既有口径：
    #   * 「未采集 ≠ 0」—— 表为空一律落 not_evaluated，不当成"没问题"；
    #   * 表键同时给 2.0 的 `item_id` 与 1.4 的中文标题；批 19 起取数走 `table_fetch`
    #     （**精确键**，不再走 `find_table` 的子串兜底 —— 那套兜底在 FRA 上命中了错的表）。

    def _check_dg_transport(self, ctx: PackageContext) -> None:
        """归档 / DG 传输目的地报错（v$archive_dest，主库也适用）。"""
        rule = "ORA.DG.TRANSPORT_ERROR"
        fetch = ctx.table_fetch("oracle.dg.archive_dest", "日志传输服务状态 (v$archive_dest)")
        rows = fetch.rows
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
            else (f"{len(rows)} 个归档目的地状态正常" if rows
                  else _missing_reason("归档目的地（V$ARCHIVE_DEST）", fetch_state(fetch))),
            facts,
            fetch_state(fetch),
        )

    def _check_dg_lag(self, ctx: PackageContext, metrics: dict[str, Any]) -> None:
        """备库传输 / 应用延迟（v$dataguard_stats 的 transport lag / apply lag）。

        单实例（无备库）时该视图为空 —— 落 not_evaluated，不当作"延迟正常"。
        """
        rule = "ORA.DG.APPLY_LAG"
        fetch = ctx.table_fetch("oracle.dg.protection_mode", "保护模式与主库统计")
        state = fetch_state(fetch)
        rows = fetch.rows
        if not rows:
            self._evaluate(rule, True, False, False,
                           "无备库统计（未配置 DataGuard）" if state_is_collected(state)
                           else _missing_reason("保护模式与主库统计", state),
                           [], state)
            return
        lags: dict[str, float] = {}
        for row in rows:
            name = str(row.get("NAME") or row.get("name") or "").strip().lower()
            seconds = _parse_interval_seconds(row.get("VALUE") or row.get("value"))
            if name and seconds is not None:
                lags[name] = seconds
        if not lags:
            self._evaluate(rule, True, False, False, "统计中无可用延迟值", [], state)
            return
        worst_key = max(lags, key=lambda key: lags[key])
        worst = lags[worst_key]
        threshold = self._threshold(rule, "dg_lag_seconds_warning", 900)
        triggered = worst > threshold
        self._evaluate(
            rule, True, True, triggered,
            f"最大延迟 {worst_key}={worst:.0f}s" + ("，超过阈值" if triggered else "，正常"),
            [f"{key}={value:.0f}s" for key, value in sorted(lags.items())],
            state,
        )

    def _check_dg_log_gap(self, ctx: PackageContext) -> None:
        """主备库归档日志序差距（备库落后主库多少个归档）。"""
        rule = "ORA.DG.LOG_GAP"
        fetch = ctx.table_fetch("oracle.dg.log_sequence_gap", "主备库日志序差距")
        state = fetch_state(fetch)
        rows = fetch.rows
        if not rows:
            self._evaluate(rule, True, False, False,
                           "无备库（未配置 DataGuard）" if state_is_collected(state)
                           else _missing_reason("主备库日志序差距", state),
                           [], state)
            return
        gaps = [safe_int(row.get("GAP") or row.get("gap")) for row in rows]
        gaps = [gap for gap in gaps if gap is not None]
        if not gaps:
            self._evaluate(rule, True, False, False, "无法解析日志序差距", [], state)
            return
        worst = max(gaps)
        threshold = self._threshold(rule, "dg_log_gap_warning", 5)
        triggered = worst > threshold
        self._evaluate(
            rule, True, True, triggered,
            f"最大日志序差距 {worst}" + ("，备库落后" if triggered else "，正常"),
            [f"max_gap={worst}", f"threads={len(gaps)}"],
            state,
        )

    def _check_dg_events(self, ctx: PackageContext) -> None:
        """近 24 小时 DataGuard 事件里是否有非零错误码。"""
        rule = "ORA.DG.EVENT_ERROR"
        fetch = ctx.table_fetch("oracle.dg.recent_events", "最近 DG 事件 (v$dataguard_status)")
        rows = fetch.rows
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
            else (f"近 24h {len(rows)} 条 DG 事件均无错误" if rows
                  else _missing_reason("最近 DataGuard 事件", fetch_state(fetch))),
            facts,
            fetch_state(fetch),
        )

    def _check_segment_bloat(self, ctx: PackageContext) -> None:
        """段空间膨胀：实际占用 - 按统计信息估算的应有大小。"""
        rule = "ORA.SCHEMA.SEGMENT_BLOAT"
        fetch = ctx.table_fetch("oracle.awr.segment_bloat", "数据膨胀预估 (DBA_SEGMENTS vs DBA_TABLES)")
        state = fetch_state(fetch)
        rows = fetch.rows
        if not rows:
            self._evaluate(rule, True, False, False,
                           _missing_reason("段膨胀预估（需统计信息）", state), [], state)
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
            state,
        )

    def _check_large_segment(self, ctx: PackageContext) -> None:
        """超大表 / 索引段（表的 SIZE_GB 与索引的 SIZE_MB 统一折成 GB 比较）。"""
        rule = "ORA.CAPACITY.LARGE_SEGMENT"
        table_fetch_ = ctx.table_fetch("oracle.basic.large_segments", "大表段 TOP20（按大小排序）")
        index_fetch = ctx.table_fetch("oracle.index.large_segments", "大索引段 TOP20")
        table_rows = table_fetch_.rows
        index_rows = index_fetch.rows
        if not table_rows and not index_rows:
            self._evaluate(
                rule, True, False, False,
                _missing_reason("大段数据（大表段 / 大索引段）",
                                prefer_state(fetch_state(table_fetch_), fetch_state(index_fetch))),
                [], prefer_state(fetch_state(table_fetch_), fetch_state(index_fetch)),
            )
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
            prefer_state(fetch_state(table_fetch_), fetch_state(index_fetch)),
        )

    def _check_password_profile(self, ctx: PackageContext) -> None:
        """口令 Profile 弱策略：无复杂度校验 / 口令永不过期 / 失败次数不受限。"""
        rule = "ORA.SECURITY.PASSWORD_PROFILE"
        fetch = ctx.table_fetch("oracle.audit.profile_params", "密码配置文件参数")
        state = fetch_state(fetch)
        rows = fetch.rows
        if not rows:
            self._evaluate(rule, True, False, False,
                           _missing_reason("Profile 参数", state), [], state)
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
            state,
        )

    def _check_hugepages(self, ctx: PackageContext) -> None:
        """透明大页 / HugePages —— **Oracle 口径**，与 MySQL 只判 THP 不同。

        Oracle 既要 THP 关闭（`never`），又要 HugePages 承载 SGA；而"配了却闲置"
        （Total>0 但 Free 占比高）说明实例没有真正用上静态大页，同样要报。
        """
        rule = "ORA.SYSTEM.HUGEPAGES"
        states, state = _hugepage_states(ctx)
        enabled = states.get("enabled", "")
        if not enabled:
            self._evaluate(rule, True, False, False,
                           _missing_reason("透明大页 / HugePages 状态", state), [], state)
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
            state,
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


def _fra_percent(row: dict[str, Any]) -> float | None:
    """一行 FRA 使用率 → 百分数。

    ★ 两个落点的**列不一样**（同一份事实、两条 SQL，都出自 `V$RECOVERY_FILE_DEST`）：
        `oracle.flashback.area_usage` → `PCT_USED`（SQL 里已算好）
        `oracle.archive.fra_asm`      → 只有 `USED_GB` / `LIMIT_GB`（要自己算）
      从前按 `PERCENT_SPACE_USED`（那是 `V$FLASH_RECOVERY_AREA_USAGE` 的列名，
      **这两条 SQL 都不产出**）取值 ⇒ 哪怕表里有数据也算不出使用率。
      另：判空一律 `is not None`，`or` 链会把**真的 0.0%**（FRA 还空着）吞成「无数据」。
    """
    for key in ("PCT_USED", "PERCENT_SPACE_USED", "PCT"):
        value = safe_float(row.get(key))
        if value is not None:
            return value
    used = safe_float(row.get("USED_GB"))
    limit = safe_float(row.get("LIMIT_GB"))
    if used is not None and limit:
        return used / limit * 100
    return None


def _hugepage_states(ctx: PackageContext) -> tuple[dict[str, str], str]:
    """还原 THP / HugePages 状态 → `(状态字典, 采集侧状态)`。

    `value` 形如 ``[never]`` / ``always madvise [never]`` —— 方括号里的是当前生效值。
    `/proc/meminfo:` 前缀的几行是批 2 新增的**实测值**，只有 Total 与 Free 对照才
    算得出"配了但没被用掉"。

    ★ 返回采集状态的理由：这张表**只有 2.0 采**（1.4 采集器不采 OS 层）⇒ 1.4 包上
      "读不到"与"查了没有"必须能分开，否则报告会把"这台机器没采操作系统层"写成
      "透明大页状态正常"（批 19）。
    """
    fetch = ctx.table_fetch("hugepages", "system.hugepages")
    states: dict[str, str] = {}
    for row in fetch.rows:
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
    return states, fetch_state(fetch)
