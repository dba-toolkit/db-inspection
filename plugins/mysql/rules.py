#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MySQL inspection rule engine — config-driven, threshold-externalized.

Loads inspection_rules.json and provides RuleEngine which evaluates all
defined rules against PackageContext + derived metrics.  The engine uses
the shared Finding / RuleEvaluation models so the contract is unchanged.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from inspection_core import (
    Finding,
    PackageContext,
    RuleEvaluation,
    safe_float,
)
from inspection_core.system_checks import (
    CANONICAL_RULE_IDS,
    DEFAULT_THRESHOLDS,
    os_pressure_report,
)

from .metrics import (
    local_host_names,
    replica_threads_running,
    split_self_referencing_replica_rows,
)

RULES_CONFIG = Path(__file__).resolve().parent / "inspection_rules.json"


def load_rules_config(path: Path | None = None) -> dict[str, Any]:
    target = path or RULES_CONFIG
    with target.open("r", encoding="utf-8") as f:
        config = json.load(f)
    if not isinstance(config, dict):
        raise ValueError(f"Rules config must be a JSON object: {target}")
    if config.get("version") != "2.0":
        raise ValueError(f"Unsupported rules config version: {config.get('version')}")
    return config


class RuleEngine:
    """Evaluates all rules from config against one PackageContext + metrics.

    Usage::

        engine = RuleEngine(config_path)
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
        self._check_security_root_remote(ctx)
        self._check_buffer_pool_ratio(ctx, metrics)
        self._check_performance_metrics(ctx, metrics)
        self._check_connection_usage(ctx, metrics)
        self._check_filesystem_usage(ctx, metrics)
        self._check_schema_items(ctx, metrics)
        self._check_sql_no_index(ctx, metrics)
        self._check_long_transactions(ctx, metrics)
        self._check_lock_waiting(ctx, metrics)
        self._check_replication_health(ctx)
        self._check_error_log(ctx, metrics)
        self._check_backup(ctx)

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
        """Core evaluation dispatcher — mirrors the original evaluate() closure."""
        cfg = self._cfg(rule_id)
        category: str = cfg.get("category", rule_id.split(".")[0])
        severity: str = cfg.get("severity", "medium")
        title: str = cfg.get("title", rule_id)
        summary: str = cfg.get("summary", "")
        recommendation: str = cfg.get("recommendation", "")
        evidence: list[str] = cfg.get("evidence_refs", [])
        confidence: float = float(cfg.get("confidence", 1.0))
        if confidence_override is not None:
            # Data-source driven (SAR history vs. a short live sample), so it
            # cannot live in the rule pack.
            confidence = confidence_override
        if severity_override is not None:
            # Two-tier rule: the pack declares the base level, the caller
            # promotes it when the harder threshold is crossed.
            severity = severity_override
        requires_restart: bool | None = cfg.get("requires_restart", False)

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
                evidence_refs=evidence, requires_restart=requires_restart,
                status="triggered", confidence=confidence, finding_id=finding_id,
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
        rule = "COMMON.COLLECTION.INTEGRITY"
        ok = ctx.integrity.get("status") == "ok"
        self._evaluate(
            rule, True, True, not ok,
            "清单哈希与文件完整性校验",
            [f"失败文件数：{ctx.integrity.get('failure_count', 0)}"],
        )

    def _check_collection_quality(self, quality: dict[str, Any]) -> None:
        rule = "COMMON.COLLECTION.QUALITY"
        score = quality.get("score", 100)
        min_score = self._threshold(rule, "quality_score_min", 80)
        self._evaluate(
            rule, True, True, score < min_score,
            f"采集完整度 {score}%",
            [f"数据完整度：{score}%"],
        )

    def _check_time_sync(self, ctx: PackageContext, metrics: dict[str, Any]) -> None:
        rule = CANONICAL_RULE_IDS["time_sync"]
        local = metrics["scope"]["database_target_is_local"]
        ntp = str(ctx.snapshot.get("time_evidence", {}).get("ntp_synchronized", "")).lower()
        self._evaluate(
            rule, local, bool(ntp),
            ntp in {"no", "false", "0", "inactive"},
            f"NTP synchronized={ntp or 'unknown'}",
            [f"NTP synchronized：{ntp or 'unknown'}"],
        )

    def _check_system_resources(self, ctx: PackageContext, metrics: dict[str, Any]) -> None:
        """CPU / IO wait / memory pressure, judged by the shared OS layer.

        MySQL ships three of the four OS checks (no standalone disk-util rule),
        which is the only thing it still decides here — the window, the peak
        criterion, the thresholds and the verdict semantics all come from
        ``inspection_core.system_checks``.
        """
        local = metrics["scope"]["database_target_is_local"]
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

    def _check_security_root_remote(self, ctx: PackageContext) -> None:
        rule = "MYSQL.SECURITY.ROOT_REMOTE"
        accounts = ctx.tables.get("accounts", [])
        remote_roots = [
            r for r in accounts
            if r.get("user") == "root" and r.get("host") not in {"localhost", "127.0.0.1", "::1"}
        ]
        self._evaluate(
            rule, True, bool(accounts), bool(remote_roots),
            "检查 root 的登录来源",
            ["账户来源：" + ", ".join(sorted({r.get('host', '') for r in remote_roots}))]
            if remote_roots else [],
        )

    def _check_buffer_pool_ratio(self, ctx: PackageContext, metrics: dict[str, Any]) -> None:
        rule = "MYSQL.INNODB.BUFFER_POOL_RATIO"
        local = metrics["scope"]["database_target_is_local"]
        bp_ratio = metrics["mysql_realtime"].get("buffer_pool_to_memory_ratio")
        lo = self._threshold(rule, "buffer_pool_ratio_min", 0.4)
        hi = self._threshold(rule, "buffer_pool_ratio_max", 0.85)
        self._evaluate(
            rule, local, bp_ratio is not None,
            bp_ratio is not None and (bp_ratio < lo or bp_ratio > hi),
            "仅当数据库与采集主机相同且内存信息可用时判断",
            [f"Buffer Pool/内存：{bp_ratio * 100:.1f}%"] if bp_ratio is not None else [],
        )

    def _check_performance_metrics(self, ctx: PackageContext, metrics: dict[str, Any]) -> None:
        mr = metrics["mysql_realtime"]

        tmp_ratio = mr.get("tmp_disk_ratio")
        t = self._threshold("MYSQL.PERFORMANCE.TMP_DISK_RATIO", "tmp_disk_ratio_warning", 0.25)
        self._evaluate(
            "MYSQL.PERFORMANCE.TMP_DISK_RATIO", True, tmp_ratio is not None,
            tmp_ratio is not None and tmp_ratio > t,
            "基于采样窗口内计数器增量",
            [f"临时表落盘比例：{tmp_ratio * 100:.1f}%"] if tmp_ratio is not None else [],
        )

        bp_miss = mr.get("buffer_pool_read_miss_ratio")
        t = self._threshold("MYSQL.INNODB.BUFFER_POOL_MISS", "buffer_pool_miss_warning", 0.01)
        self._evaluate(
            "MYSQL.INNODB.BUFFER_POOL_MISS", True, bp_miss is not None,
            bp_miss is not None and bp_miss > t,
            "基于采样窗口内读请求增量",
            [f"读未命中比例：{bp_miss * 100:.2f}%"] if bp_miss is not None else [],
        )

        cache_miss = mr.get("table_open_cache_miss_ratio")
        t = self._threshold("MYSQL.PERFORMANCE.TABLE_CACHE_MISS", "table_cache_miss_warning", 0.10)
        self._evaluate(
            "MYSQL.PERFORMANCE.TABLE_CACHE_MISS", True, cache_miss is not None,
            cache_miss is not None and cache_miss > t,
            "基于采样窗口内缓存命中增量",
            [f"表缓存未命中比例：{cache_miss * 100:.1f}%"] if cache_miss is not None else [],
        )

    def _check_connection_usage(self, ctx: PackageContext, metrics: dict[str, Any]) -> None:
        rule = "MYSQL.CONNECTION.USAGE"
        connected_max = metrics["mysql_realtime"].get("threads_connected", {}).get("max")
        max_conn = safe_float(ctx.variables.get("max_connections"))
        ratio = connected_max / max_conn if connected_max is not None and max_conn else None
        t = self._threshold(rule, "connection_usage_warning", 0.85)
        self._evaluate(
            rule, True, ratio is not None,
            ratio is not None and ratio >= t,
            "Threads_connected/Max_connections",
            [f"连接峰值：{connected_max:.0f}", f"上限：{max_conn:.0f}", f"使用率：{ratio * 100:.1f}%"]
            if ratio is not None else [],
        )

    def _check_filesystem_usage(self, ctx: PackageContext, metrics: dict[str, Any]) -> None:
        rule = CANONICAL_RULE_IDS["filesystem_usage"]
        local = metrics["scope"]["database_target_is_local"]
        # Persistent mounts only: metrics dropped pseudo filesystems, read-only
        # media and container overlays before taking the maximum, so the number
        # and the table in the report describe the same set of mounts.
        max_fs = metrics["capacity"].get("max_filesystem_usage_percent")
        t = DEFAULT_THRESHOLDS["filesystem_usage_critical"]
        self._evaluate(
            rule, local, max_fs is not None,
            max_fs is not None and max_fs >= t,
            "检查所有已成功读取的持久化文件系统",
            [f"最高文件系统使用率：{max_fs:.1f}%（已排除虚拟/只读挂载点）"] if max_fs is not None else [],
        )

    def _check_schema_items(self, ctx: PackageContext, metrics: dict[str, Any]) -> None:
        schema = metrics["schema"]

        no_pk = schema.get("tables_without_primary_key")
        self._evaluate(
            "MYSQL.SCHEMA.NO_PRIMARY_KEY", True, no_pk is not None,
            bool(no_pk and no_pk > 0),
            "统计业务表主键情况",
            [f"无主键表：{no_pk}"] if no_pk is not None else [],
        )

        redundant = schema.get("redundant_index_count")
        self._evaluate(
            "MYSQL.SCHEMA.REDUNDANT_INDEX", True, redundant is not None,
            bool(redundant and redundant > 0),
            "依据 sys schema 候选结果",
            [f"候选数量：{redundant}"] if redundant is not None else [],
        )

        auto_count = schema.get("auto_increment_warning_count")
        self._evaluate(
            "MYSQL.SCHEMA.AUTO_INCREMENT_CAPACITY", True, auto_count is not None,
            bool(auto_count and auto_count > 0),
            "采集器仅输出达到风险阈值的自增列",
            [f"风险对象数：{auto_count}"] if auto_count is not None else [],
        )

        frag_count = schema.get("fragmentation_candidate_count")
        self._evaluate(
            "MYSQL.SCHEMA.FRAGMENTATION", True, frag_count is not None,
            bool(frag_count and frag_count > 0),
            "按采集器碎片候选阈值判断",
            [f"候选对象数：{frag_count}"] if frag_count is not None else [],
        )

        non_innodb = schema.get("non_innodb_table_count")
        self._evaluate(
            "MYSQL.SCHEMA.NON_INNODB", True, non_innodb is not None,
            bool(non_innodb and non_innodb > 0),
            "检查业务 schema 中的非 InnoDB 表",
            [f"表数量：{non_innodb}"] if non_innodb is not None else [],
        )

    def _check_sql_no_index(self, ctx: PackageContext, metrics: dict[str, Any]) -> None:
        rule = "MYSQL.SQL.NO_INDEX_DIGEST"
        digest_rows = ctx.tables.get("sql_digests_top", [])
        no_index_exec = sum(
            int((r.get("SUM_NO_INDEX_USED") or "").strip() or 0) for r in digest_rows
        )
        no_index_seconds = sum(
            float((r.get("total_seconds") or "").strip() or 0)
            for r in digest_rows if int((r.get("SUM_NO_INDEX_USED") or "").strip() or 0) > 0
        )
        exec_min = self._threshold(rule, "no_index_exec_min", 100)
        sec_min = self._threshold(rule, "no_index_seconds_min", 60)
        self._evaluate(
            rule, True, bool(digest_rows),
            no_index_exec > exec_min and no_index_seconds >= sec_min,
            "Performance Schema 摘要中 SUM_NO_INDEX_USED 累计值；需结合启动时长",
            [f"摘要累计执行次数：{no_index_exec}", f"累计耗时：{no_index_seconds:.1f} 秒"]
            if digest_rows else [],
        )

    def _check_long_transactions(self, ctx: PackageContext, metrics: dict[str, Any]) -> None:
        rule = "MYSQL.TRANSACTION.LONG_RUNNING"
        activity = metrics["activity"]
        long_count = activity.get("long_transaction_count")
        long_max = activity.get("max_long_transaction_seconds")
        t = self._threshold(rule, "long_transaction_seconds", 300)
        self._evaluate(
            rule, True, long_count is not None,
            bool(long_count and long_max is not None and long_max >= t),
            f"长事务阈值 {t} 秒",
            [f"数量：{long_count}", f"最长：{long_max:.0f} 秒"] if long_count else [],
        )

    def _check_lock_waiting(self, ctx: PackageContext, metrics: dict[str, Any]) -> None:
        rule = "MYSQL.LOCK.WAITING"
        activity = metrics["activity"]
        lock_count = (
            (activity.get("data_lock_wait_count") or 0)
            + (activity.get("pending_metadata_lock_count") or 0)
        )
        self._evaluate(
            rule, True, True, lock_count > 0,
            "检查数据锁等待和待授予元数据锁",
            [f"等待记录：{lock_count}"] if lock_count else [],
        )

    def _check_replication_health(self, ctx: PackageContext) -> None:
        rule = "MYSQL.REPLICATION.HEALTH"
        role = ctx.snapshot.get("role_evidence", {})

        # 先按「上游是否指向本机」拆行：指向自身的行是残留通道，不是主从关系。
        # 它既不能算作"副本在跑"，也不能算作"复制异常"——把它当异常会让复制源端
        # 背上一条 high（还进 P1 整改），而拓扑层同一份数据已经把它记进
        # self_reference_edges 了。这条判据和拓扑层共用 plugins.mysql.metrics。
        real_rows, residual_rows = split_self_referencing_replica_rows(
            ctx.tables.get("replica_status", []), local_host_names(ctx)
        )
        if residual_rows:
            self._evaluate(
                "MYSQL.REPLICATION.RESIDUAL_CHANNEL", True, True, True,
                "复制状态行的上游声明指向本机，不构成真实主从关系",
                [f"指向自身的复制状态行：{len(residual_rows)} 条"],
            )

        lag = safe_float(role.get("replica_lag_seconds"))
        t = self._threshold(rule, "replication_lag_seconds", 60)
        # 没有真实上游行就没有可评价的复制关系（源端/单实例是正常状态），
        # 不再拿 role_observed 里的 "replica" 当判据 —— 那个观测值正是残留行造成的。
        applicable = bool(real_rows)
        repl_bad = False
        facts: list[str] = []
        if applicable:
            states = [replica_threads_running(row) for row in real_rows]
            # False 才是"明确在停"；None 是这一列没采到，不能当异常报。
            repl_bad = any(
                io_ok is False or sql_ok is False for io_ok, sql_ok in states
            ) or (lag is not None and lag > t)
            facts = [
                f"复制通道：{len(real_rows)} 条",
                f"IO/SQL 线程正常：{sum(1 for io_ok, sql_ok in states if io_ok and sql_ok)} 条",
                f"lag={lag}",
            ]
        self._evaluate(
            rule, applicable, applicable, repl_bad,
            "检查复制线程状态及延迟",
            facts,
        )

    def _check_error_log(self, ctx: PackageContext, metrics: dict[str, Any]) -> None:
        rule = "MYSQL.LOG.ERROR_EVENTS"
        error_count = metrics["activity"].get("error_log_error_occurrences")
        self._evaluate(
            rule, True, error_count is not None,
            bool(error_count and error_count > 0),
            "仅按错误级别汇总，不读取日志正文",
            [f"错误级事件次数：{error_count}"] if error_count is not None else [],
        )

    def _check_backup(self, ctx: PackageContext) -> None:
        """备份任务配置可见性。

        采集端只产出备份任务的**配置证据**（cron / systemd timer / 备份进程），
        不包含"最近一次备份是否成功"的结果数据；因此本条只评价配置可见性，
        备份有效性仍需接入备份平台任务结果与恢复演练记录后才能判定。
        """
        rule = "MYSQL.BACKUP.TASK_VISIBILITY"
        evidence_dir = ctx.root / "evidence"
        sources = [
            evidence_dir / "backup_cron.txt",
            evidence_dir / "backup_timers.txt",
            evidence_dir / "backup_processes.txt",
        ]
        present = [path for path in sources if path.exists()]
        if not present:
            self._evaluate(rule, True, False, False, "采集包未包含备份任务证据文件", [])
            return
        found = 0
        for path in present:
            found += sum(
                1 for line in path.read_text(encoding="utf-8", errors="replace").splitlines() if line.strip()
            )
        if found:
            self._evaluate(
                rule, True, True, False,
                f"发现 {found} 条备份相关任务配置", [f"备份任务配置 {found} 条"],
            )
        else:
            self._evaluate(
                rule, True, True, True,
                "未发现任何备份任务配置（cron / systemd timer / 备份进程）",
                ["备份任务配置 0 条"],
            )

class MySQLRuleProvider:
    """Database plugin facade for evaluating the MySQL rule pack."""

    def __init__(self, config_path: Path | None = None) -> None:
        self.config_path = config_path

    def evaluate(
        self,
        ctx: PackageContext,
        metrics: dict[str, Any],
        quality: dict[str, Any],
    ) -> tuple[list[Finding], list[RuleEvaluation]]:
        return RuleEngine(config_path=self.config_path).run(ctx, metrics, quality)


