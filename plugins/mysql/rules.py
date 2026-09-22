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
    format_bytes,
    memory_total_kb,
    ntp_verdict,
    os_memory_available_min_bytes,
    os_pressure_report,
)

from .metrics import (
    backup_task_visibility,
    binlog_totals,
    config_runtime_drift,
    global_status_value,
    large_table_items,
    leftover_table_items,
    local_host_names,
    log_file_entries,
    mysql_uptime_seconds,
    replica_threads_running,
    runtime_variables,
    split_self_referencing_replica_rows,
    time_evidence,
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
        # 批 B：把技能阈值表里「已写但从不执行」的判据接进规则引擎。
        self._check_isolation_level(ctx)
        self._check_redo_capacity(ctx, metrics)
        self._check_replica_skip_errors(ctx)
        self._check_replica_writable(ctx)
        self._check_deadlock(ctx)
        self._check_transparent_hugepage(ctx)
        self._check_swap_pressure(ctx, metrics)
        self._check_log_rotation(ctx)
        self._check_binlog_encryption(ctx)
        self._check_binlog_capacity(ctx)
        self._check_runtime_drift(ctx)
        # 批 D：清单 A9/A10/A12/A13 —— 表缓存顶格、连接失败率、超大单表、残留表。
        self._check_table_cache(ctx)
        self._check_connection_errors(ctx)
        self._check_large_table(ctx)
        self._check_leftover_tables(ctx)

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
        """时间同步按节点分档：只有「未同步 + 有 RTC 偏移实锤」才落风险。

        判据取自公共层 ``ntp_verdict``，证据由 ``metrics.time_evidence`` 合并
        ``snapshot`` 与 ``evidence/timedatectl.txt``（采集侧该字段常为空串）。
        "已对齐但未启用持续同步源"落 ``passed``、在呈现层标提示 —— 折叠成故障会
        把三台都判 risk，而这台机器的时间其实是对的。
        """
        rule = CANONICAL_RULE_IDS["time_sync"]
        local = metrics["scope"]["database_target_is_local"]
        verdict = ntp_verdict(time_evidence(ctx))
        self._evaluate(
            rule, local, verdict["tier"] != "unknown", verdict["triggered"],
            verdict["reason"], verdict["facts"],
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
            if verdict["rule_key"] == "memory_pressure":
                verdict = self._memory_verdict(verdict, ctx, metrics)
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

        # 自增容量：采集端输出的是「按 used_pct 倒序的前 N 条明细」，所以
        # **明细条数不是风险对象数** —— 实测 100 条里最高仅 22.79%，一个都不到阈值。
        # 判定必须按 used_pct 阈值重算，并把"最高使用率"这个可核对的量写进事实依据。
        auto_items = schema.get("auto_increment_items") or []
        auto_available = "auto_increment_usage" in ctx.tables
        auto_pct = self._threshold(
            "MYSQL.SCHEMA.AUTO_INCREMENT_CAPACITY", "used_pct_warning", 60.0
        )
        auto_hits = [
            item for item in auto_items if (item.get("used_pct") or 0.0) >= float(auto_pct)
        ]
        auto_facts: list[str] = []
        if auto_items and auto_items[0].get("used_pct") is not None:
            top = auto_items[0]
            auto_facts.append(
                f"最高使用率 {top['used_pct']:.2f}%（{top['object']}，{top.get('column_type') or '未知类型'}）"
            )
        if auto_available:
            auto_facts.append(f"已采集明细 {len(auto_items)} 条（采集端按使用率倒序，非全集）")
        auto_facts.append(f"判定阈值：使用率 ≥ {float(auto_pct):g}%")
        self._evaluate(
            "MYSQL.SCHEMA.AUTO_INCREMENT_CAPACITY", True, auto_available, bool(auto_hits),
            "按自增列使用率（AUTO_INCREMENT / max_value）判定，不使用明细条数",
            auto_facts,
        )

        # 碎片：采集端按 fragmentation_pct 倒序，TOP 全是"分配 0.02 MB / 空闲 18 MB"
        # 的极小表；改按 data_free_mb（绝对可回收空间）判定，百分比只作辅助列。
        frag_items = schema.get("fragmentation_items") or []
        frag_available = "fragmentation_top" in ctx.tables
        frag_mb = self._threshold(
            "MYSQL.SCHEMA.FRAGMENTATION", "data_free_mb_min", 100.0
        )
        frag_hits = [
            item for item in frag_items if (item.get("data_free_mb") or 0.0) >= float(frag_mb)
        ]
        frag_facts: list[str] = []
        if frag_items and frag_items[0].get("data_free_mb") is not None:
            top = frag_items[0]
            frag_facts.append(
                f"最大可回收空间 {top['data_free_mb']:,.2f} MB（{top['object']}，"
                f"碎片率 {top.get('fragmentation_pct')}%）"
            )
        if frag_available:
            frag_facts.append(f"已采集明细 {len(frag_items)} 条（采集端按碎片率倒序，本判据在明细内按空闲空间重排）")
        frag_facts.append(f"判定阈值：可回收空间 ≥ {float(frag_mb):g} MB")
        self._evaluate(
            "MYSQL.SCHEMA.FRAGMENTATION", True, frag_available, bool(frag_hits),
            "按可回收空间（data_free_mb）判定；碎片率仅作辅助，避免极小表占满候选",
            frag_facts,
        )

        # 非 InnoDB：按引擎分档 —— MEMORY 重启丢数据且不支持事务，MyISAM 只是崩溃恢复弱。
        non_innodb = schema.get("non_innodb_table_count")
        non_innodb_engines = schema.get("non_innodb_engines") or {}
        engine_facts = [f"{engine} {count} 张" for engine, count in sorted(non_innodb_engines.items())]
        for engine, consequence in (
            ("MEMORY", "MEMORY 引擎：重启即丢数据、不支持事务，仅适合临时表"),
            ("MYISAM", "MyISAM 引擎：无事务、崩溃恢复弱，可磁盘持久化"),
        ):
            if non_innodb_engines.get(engine):
                engine_facts.append(consequence)
        self._evaluate(
            "MYSQL.SCHEMA.NON_INNODB", True, non_innodb is not None,
            bool(non_innodb and non_innodb > 0),
            "检查业务 schema 中的非 InnoDB 表，并按引擎分档给后果",
            engine_facts or ([f"表数量：{non_innodb}"] if non_innodb is not None else []),
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
        """备份任务配置可见性 —— 四态，核心是别把"证据为空"写成"0 条任务"。

        采集端只产出备份任务的**配置证据**（cron / systemd timer / 备份进程），
        不包含"最近一次备份是否成功"的结果数据。原实现把"文件存在但 0 字节"
        当成"没有备份任务"（`未发现任何备份任务配置 / 0 条`），既违反"未采集 ≠ 0"，
        又和同报告 `mysql.backup` 章节的 `not_evaluated` 结论打架。四态判据见
        ``metrics.backup_task_visibility``。
        """
        rule = "MYSQL.BACKUP.TASK_VISIBILITY"
        visibility = backup_task_visibility(ctx)
        state = visibility["state"]
        if state == "active":
            self._evaluate(
                rule, True, True, False,
                f"发现 {visibility['active_lines']} 条生效的备份任务配置",
                [f"备份任务配置 {visibility['active_lines']} 条"],
            )
        elif state == "disabled":
            self._evaluate(
                rule, True, True, True,
                "备份任务配置存在但全部处于注释状态，本机无生效的备份任务",
                [f"备份任务配置 {visibility['commented_lines']} 条，全部被注释"],
            )
        elif state == "empty":
            self._evaluate(
                rule, True, False, False,
                "备份证据文件为空（0 字节），无法判定是否存在备份任务",
                ["证据文件存在但内容为空；空文件不能推出\"没有备份任务\""],
            )
        else:
            self._evaluate(
                rule, True, False, False,
                "采集包未包含备份任务证据文件", [],
            )

    def _memory_verdict(
        self, verdict: dict[str, Any], ctx: PackageContext, metrics: dict[str, Any]
    ) -> dict[str, Any]:
        """内存压力判定叠加 MemAvailable 复核。

        SAR 的 ``%memused`` 把页缓存计入使用率（实测三节点 24h 峰值 99.7~99.8%），
        只看它会把"缓存占满、可用内存充足"的正常主机判成内存压力——这正是既有
        R001 长期误报的原因。Linux 上 ``MemAvailable`` 才是可用于新分配的估计值，
        因此当窗口内**最低可用内存**仍高于总内存的
        ``memory_available_floor_percent`` 时，不判定为内存压力，并把依据写进事实。
        """
        if not verdict.get("triggered"):
            return verdict
        # 「最低可用内存」的取值收口在公共层（该指标只在实时块里有）；插件不得
        # 自己挑数据窗口，否则判定窗口与口径会再次各写一份。
        available_min = os_memory_available_min_bytes(metrics)
        total_kb = memory_total_kb(ctx)
        if available_min is None or not total_kb:
            return verdict
        total_bytes = total_kb * 1024
        rule = CANONICAL_RULE_IDS["memory_pressure"]
        floor = float(self._threshold(rule, "memory_available_floor_percent", 10.0))
        available_percent = available_min / total_bytes * 100.0
        if available_percent < floor:
            return verdict
        adjusted = dict(verdict)
        adjusted["triggered"] = False
        adjusted["fact"] = (
            f"{verdict['label']}：{verdict['value']:.1f}%（SAR %memused 含页缓存），"
            f"窗口内最低可用内存 {format_bytes(available_min)}，"
            f"占总量 {available_percent:.1f}%，高于 {floor:.0f}% 底线，不构成内存压力"
        )
        return adjusted

    def _check_isolation_level(self, ctx: PackageContext) -> None:
        rule = "MYSQL.TRANSACTION.ISOLATION_LEVEL"
        value = str(ctx.variables.get("transaction_isolation") or "").strip().upper()
        self._evaluate(
            rule, True, bool(value), value in {"READ-UNCOMMITTED", "READ UNCOMMITTED"},
            "transaction_isolation 取值",
            [f"transaction_isolation={value}"] if value else [],
        )

    def _check_redo_capacity(self, ctx: PackageContext, metrics: dict[str, Any]) -> None:
        """Redo 实际容量判定。

        ``innodb_redo_log_capacity`` 的**变量值不代表实际容量**：容量由旧参数
        ``innodb_log_file_size × innodb_log_files_in_group`` 算出时不回写该变量
        （MySQL 8.0 手册 §17.6.5）。唯一可靠依据是状态变量
        ``Innodb_redo_log_capacity_resized``；它缺失时才退回旧参数推算，
        再退化到变量值，并在事实里写明用的是哪一层证据。
        """
        rule = "MYSQL.INNODB.REDO_CAPACITY"
        resized = safe_float(ctx.global_status.get("Innodb_redo_log_capacity_resized"))
        variable = safe_float(ctx.variables.get("innodb_redo_log_capacity"))
        file_size = safe_float(ctx.variables.get("innodb_log_file_size"))
        file_count = safe_float(ctx.variables.get("innodb_log_files_in_group"))
        legacy = file_size * file_count if file_size and file_count else None
        actual = resized if resized is not None else (legacy if legacy is not None else variable)
        if actual is None:
            self._evaluate(rule, True, False, False, "redo 容量证据缺失", [])
            return
        facts = [f"实际容量：{format_bytes(actual)}"]
        if resized is None:
            facts.append("状态变量 Innodb_redo_log_capacity_resized 未采集，按旧参数推算")
        if variable is not None and resized is not None and abs(variable - resized) > 1:
            facts.append(
                f"innodb_redo_log_capacity 变量值 {format_bytes(variable)} 不代表实际容量，"
                "以状态变量为准"
            )
        waits: list[str] = []
        for row in ctx.tables.get("error_log_summary", []) or []:
            if any("013865" in str(value) for value in row.values()):
                waits = [str(value) for value in row.values() if str(value).strip()][:3]
                break
        if waits:
            facts.append("错误日志出现 MY-013865（redo 写等待）：" + " / ".join(waits))
        threshold = float(self._threshold(rule, "redo_capacity_min_bytes", 1024 ** 3))
        self._evaluate(
            rule, True, True, actual < threshold,
            f"实际容量阈值 {format_bytes(threshold)}",
            facts,
            severity_override="high" if waits else None,
        )

    def _check_replica_skip_errors(self, ctx: PackageContext) -> None:
        rule = "MYSQL.REPLICATION.SKIP_ERRORS"
        raw = str(
            ctx.variables.get("replica_skip_errors")
            or ctx.variables.get("slave_skip_errors")
            or ""
        ).strip()
        real_rows, _residual = split_self_referencing_replica_rows(
            ctx.tables.get("replica_status", []), local_host_names(ctx)
        )
        skipping = raw.upper() not in {"", "OFF", "NONE", "0"}
        facts = [f"replica_skip_errors={raw or '未采集'}"]
        if real_rows:
            facts.append(f"当前真实复制通道：{len(real_rows)} 条，错误会被静默跳过")
        else:
            facts.append("当前无生效的复制链路；角色转换为副本后该配置会立即生效")
        self._evaluate(
            rule, True, bool(raw), skipping,
            "replica_skip_errors 取值",
            facts if raw else [],
            # 无真实上游时降级：此刻不会吞错误，但对端一切换过来就会。
            severity_override="high" if real_rows else "medium",
        )

    def _check_replica_writable(self, ctx: PackageContext) -> None:
        """副本可写性：read_only/super_read_only 与写入型事件三点闭合。"""
        rule = "MYSQL.REPLICATION.REPLICA_WRITABLE"
        real_rows, _residual = split_self_referencing_replica_rows(
            ctx.tables.get("replica_status", []), local_host_names(ctx)
        )
        is_replica = bool(real_rows)
        read_only = str(ctx.variables.get("read_only") or "").strip().upper()
        super_read_only = str(ctx.variables.get("super_read_only") or "").strip().upper()
        scheduler = str(ctx.variables.get("event_scheduler") or "").strip().upper()
        enabled_events = [
            row for row in ctx.tables.get("events", []) or []
            if str(row.get("STATUS") or "").strip().upper() == "ENABLED"
        ]
        normal_writes = read_only in {"OFF", "0"}
        scheduler_writes = scheduler in {"ON", "1"} and bool(enabled_events)
        writable = (
            is_replica
            and super_read_only in {"OFF", "0"}
            and (normal_writes or scheduler_writes)
        )
        facts = [
            f"read_only={read_only or '未采集'}",
            f"super_read_only={super_read_only or '未采集'}",
            f"event_scheduler={scheduler or '未采集'}",
            f"STATUS=ENABLED 的事件：{len(enabled_events)} 个",
        ]
        self._evaluate(
            rule, is_replica, True, writable,
            "只对真实副本判定；源端/单实例可写是正常状态",
            facts if is_replica else [],
        )

    def _check_deadlock(self, ctx: PackageContext) -> None:
        rule = "MYSQL.INNODB.DEADLOCK"
        path = ctx.root / "evidence" / "innodb_status.txt"
        if not path.exists():
            self._evaluate(rule, True, False, False, "采集包未包含 innodb_status 证据文件", [])
            return
        text = _read_evidence_text(path)
        marker = "LATEST DETECTED DEADLOCK"
        found = marker in text
        facts: list[str] = []
        if found:
            tail = text[text.index(marker) + len(marker):][:600]
            stamp = ""
            for line in tail.splitlines():
                stripped = line.strip()
                if not stripped or set(stripped) <= {"-"}:
                    continue
                stamp = stripped
                break
            facts = [f"最近一次死锁：{stamp or '时间未采集'}"]
            if "event_scheduler" in tail:
                facts.append("死锁事务的发起线程是 event_scheduler（事件调度器写入）")
        self._evaluate(rule, True, True, found, "innodb_status 中的 LATEST DETECTED DEADLOCK 段", facts)

    def _check_transparent_hugepage(self, ctx: PackageContext) -> None:
        rule = "MYSQL.SYSTEM.TRANSPARENT_HUGEPAGE"
        states = _hugepage_states(ctx)
        enabled = states.get("enabled", "")
        facts: list[str] = []
        if enabled:
            facts.append(f"transparent_hugepage/enabled={enabled}")
        if states.get("defrag"):
            facts.append(f"transparent_hugepage/defrag={states['defrag']}")
        if states.get("nr_hugepages") is not None:
            facts.append(f"vm.nr_hugepages={states['nr_hugepages']}")
        self._evaluate(
            rule, True, bool(enabled), enabled == "always",
            "读取 /sys/kernel/mm/transparent_hugepage",
            facts,
        )

    def _check_swap_pressure(self, ctx: PackageContext, metrics: dict[str, Any]) -> None:
        rule = "COMMON.SYSTEM.SWAP_PRESSURE"
        total, used = _swap_totals(ctx)
        if not total or used is None:
            self._evaluate(rule, True, False, False, "未采集 swap 总量，无法计算使用率", [])
            return
        ratio = used / total
        limit = float(self._threshold(rule, "swap_usage_warning", 0.3))
        self._evaluate(
            rule, True, True, ratio >= limit,
            f"阈值 {limit * 100:.0f}%",
            [f"Swap 使用 {format_bytes(used)} / {format_bytes(total)}（{ratio * 100:.1f}%）"],
        )

    def _check_log_rotation(self, ctx: PackageContext) -> None:
        """日志文件轮转（A6）。

        采集端提供 ``tables/log_files.tsv#size_bytes``，之前只被用来数"日志文件 3 个"，
        没有任何阈值判断 —— 实测某节点 error log 已到 132.9 GiB、另一节点 slow log
        51.3 GiB，报告里一个字都没提。判据用两档：单文件 > 1 GiB 提醒、> 10 GiB 告警。
        绝对值只说明"大"，除以 ``Uptime`` 折成日增速率才说明"该不该马上处理"。
        """
        rule = "COMMON.SYSTEM.LOG_ROTATION"
        entries = log_file_entries(ctx)
        if not entries:
            self._evaluate(rule, True, False, False, "未采集到日志文件大小（tables/log_files.tsv）", [])
            return
        warn = float(self._threshold(rule, "log_file_warning_bytes", 1024 ** 3))
        crit = float(self._threshold(rule, "log_file_critical_bytes", 10 * 1024 ** 3))
        biggest = max(entries, key=lambda item: item["size_bytes"])
        over = [item for item in entries if item["size_bytes"] > warn]
        facts = [
            f"最大日志文件：{biggest['log_type']} {format_bytes(biggest['size_bytes'])}（{biggest['path']}）",
        ]
        uptime = mysql_uptime_seconds(ctx)
        if uptime and uptime > 0:
            per_day = biggest["size_bytes"] / (uptime / 86400.0)
            facts.append(
                f"折合日增约 {format_bytes(per_day)}/天（实例已运行约 {uptime / 86400:.0f} 天）"
            )
        if over:
            facts.append(
                "超过提醒档（%s）的文件：%s"
                % (format_bytes(warn), "、".join(f"{i['log_type']} {format_bytes(i['size_bytes'])}" for i in over))
            )
        self._evaluate(
            rule, True, True, bool(over),
            f"单文件阈值 {format_bytes(warn)}（提醒）/ {format_bytes(crit)}（告警）",
            facts,
            severity_override="high" if biggest["size_bytes"] > crit else None,
        )

    def _check_binlog_encryption(self, ctx: PackageContext) -> None:
        """Binlog 明文落盘（A7）。"""
        rule = "MYSQL.SECURITY.BINLOG_UNENCRYPTED"
        totals = binlog_totals(ctx)
        if not totals["count"]:
            self._evaluate(rule, True, False, False, "未采集到 Binlog 文件列表", [])
            return
        limit = int(self._threshold(rule, "binlog_unencrypted_warning", 1))
        unencrypted = totals["unencrypted"]
        self._evaluate(
            rule, True, True, unencrypted >= limit,
            f"未加密文件阈值 {limit} 个",
            [
                f"未加密 Binlog {unencrypted} / {totals['count']} 个",
                f"合计容量 {format_bytes(totals['bytes'])}",
            ],
        )

    def _check_binlog_capacity(self, ctx: PackageContext) -> None:
        """Binlog 总容量（A7）。

        只判本实例的落盘总量。跨节点的**保留期倒挂**（从库保留期长于主库）需要
        同时看到对端，由 ``evaluate_replication_retention`` 在拿到全部实例后判。
        """
        rule = "MYSQL.CAPACITY.BINLOG_SIZE"
        totals = binlog_totals(ctx)
        if not totals["count"]:
            self._evaluate(rule, True, False, False, "未采集到 Binlog 文件列表", [])
            return
        limit = float(self._threshold(rule, "binlog_total_warning_bytes", 50 * 1024 ** 3))
        retention = runtime_variables(ctx).get("binlog_expire_logs_seconds")
        facts = [f"Binlog {totals['count']} 个，合计 {format_bytes(totals['bytes'])}"]
        if retention is not None:
            try:
                days = float(retention) / 86400.0
                facts.append(f"保留期 binlog_expire_logs_seconds={retention}（约 {days:.1f} 天）")
            except ValueError:
                facts.append(f"保留期 binlog_expire_logs_seconds={retention}")
        self._evaluate(
            rule, True, True, totals["bytes"] > limit,
            f"总容量阈值 {format_bytes(limit)}",
            facts,
        )

    def _check_runtime_drift(self, ctx: PackageContext) -> None:
        """配置文件值 vs 运行值不一致（B11）。

        ``long_query_time`` 这类参数改了 my.cnf 但没重启/没加载时，配置文件与运行值
        会长期不一致 —— 平时看不出来，一次重启就把线上行为换成配置文件的版本。
        实测三节点各有一处（``.33`` long_query_time 1→5、``.34`` 5→1、``.125``
        max_connections 16000→4190）。判据见 ``metrics.config_runtime_drift``。
        """
        rule = "MYSQL.CONFIG.RUNTIME_DRIFT"
        drifts = config_runtime_drift(ctx)
        if not drifts:
            self._evaluate(rule, True, True, False, "配置文件与运行值一致", [])
            return
        limit = int(self._threshold(rule, "runtime_drift_ignore", 1))
        facts: list[str] = []
        for item in drifts:
            configured = "、".join(item["configured_raw"])
            suffix = (
                f"（该别名实际映射到 {item['alias_of']}）" if item.get("alias_of") else ""
            )
            if item["conflict_in_file"]:
                facts.append(
                    f"{item['parameter']}：配置文件内写了 {configured} 等多个不同值，"
                    f"运行值 {item['runtime']}{suffix}"
                )
            else:
                facts.append(
                    f"{item['parameter']}：配置文件 {configured}，运行值 {item['runtime']}{suffix}"
                )
        self._evaluate(
            rule, True, True, len(drifts) >= limit,
            f"发现 {len(drifts)} 处配置与运行值不一致",
            facts,
        )

    def _check_table_cache(self, ctx: PackageContext) -> None:
        """表缓存顶格 / 打开速率（A9）。

        已有的 ``MYSQL.PERFORMANCE.TABLE_CACHE_MISS`` 用采样窗口差分算"未命中比例"，
        看不到"缓存是否已顶格"这个更直接的信号。实测 ``.33`` Open_tables=4000 恰等于
        table_open_cache、``.125`` cache 只有 400 而 Open_tables=683 已超 —— 顶格意味着
        每开一张新表都要挤掉旧表，是"该调大 table_open_cache"最硬的证据。另一个信号
        是累计打开速率 ``Opened_tables / Uptime``，它反映历史打开压力、不受采样窗口影响。
        """
        rule = "MYSQL.RUNTIME.TABLE_CACHE"
        open_tables = safe_float(global_status_value(ctx, "Open_tables"))
        opened_tables = safe_float(global_status_value(ctx, "Opened_tables"))
        cache = safe_float(runtime_variables(ctx).get("table_open_cache"))
        if open_tables is None or opened_tables is None or cache is None:
            self._evaluate(rule, True, False, False, "未采集到表缓存相关状态变量", [])
            return
        capped = open_tables >= cache
        uptime = mysql_uptime_seconds(ctx)
        rate = opened_tables / uptime if uptime and uptime > 0 else None
        ops_limit = float(self._threshold(rule, "table_cache_open_ops_per_sec", 20))
        triggered = capped or (rate is not None and rate > ops_limit)
        facts = [
            f"Open_tables {open_tables:.0f} / table_open_cache {cache:.0f}"
            + ("（已顶格）" if capped else ""),
        ]
        if rate is not None:
            facts.append(
                f"累计打开速率 {rate:.1f} 次/秒"
                f"（Opened_tables {opened_tables:.0f} / Uptime {uptime:.0f}s）"
            )
        self._evaluate(
            rule, True, True, triggered,
            f"顶格或打开速率阈值 {ops_limit:.0f} 次/秒",
            facts,
        )

    def _check_connection_errors(self, ctx: PackageContext) -> None:
        """连接失败率与 max_connect_errors 封禁风险（A10）。

        ``Aborted_connects / Connections`` 是累计比值、不是当前失败率；但实测 ``.33``
        高达 20%、``.34`` 10.8%，远超正常（<1%），且两者 ``max_connect_errors=1000``
        偏低，来源 IP 连续失败 1000 次即被拒（需 FLUSH HOSTS）。``.125`` 0.47% 属正常。
        """
        rule = "MYSQL.RUNTIME.CONNECTION_ERRORS"
        aborted = safe_float(global_status_value(ctx, "Aborted_connects"))
        connections = safe_float(global_status_value(ctx, "Connections"))
        if aborted is None or connections is None:
            self._evaluate(rule, True, False, False, "未采集到连接计数", [])
            return
        ratio = aborted / connections if connections > 0 else None
        limit = float(self._threshold(rule, "aborted_connects_ratio_warning", 0.05))
        mce_low = int(self._threshold(rule, "max_connect_errors_low", 10000))
        mce = runtime_variables(ctx).get("max_connect_errors")
        facts = [f"累计连接失败 {aborted:.0f} / 累计连接 {connections:.0f}（{ratio * 100:.1f}%）"]
        if mce is not None:
            mce_value = safe_float(mce)
            if mce_value is not None:
                low_note = "（偏低，失败达阈值即封禁来源 IP，需 FLUSH HOSTS）" if mce_value < mce_low else ""
                facts.append(f"max_connect_errors={mce}{low_note}")
            else:
                facts.append(f"max_connect_errors={mce}")
        self._evaluate(
            rule, True, ratio is not None,
            ratio is not None and ratio > limit,
            f"失败率阈值 {limit * 100:.0f}%",
            facts,
        )

    def _check_large_table(self, ctx: PackageContext) -> None:
        """单表规模阈值（A12）。

        只报数据量本身带来的运维压力：备份窗口、DDL（无 instant 的 ALTER 会重建表）、
        归档与容量规划。``index_mb=0`` 的表（实测 tb_doc_html 111GB 而索引 0）单独提示
        全表扫描风险。
        """
        rule = "MYSQL.CAPACITY.LARGE_TABLE"
        if "large_tables_top" not in ctx.tables:
            self._evaluate(rule, True, False, False, "未采集到 large_tables_top", [])
            return
        limit = float(self._threshold(rule, "large_table_warning_mb", 102400))
        items = large_table_items(ctx, limit)
        facts: list[str] = []
        for item in items:
            note = "，索引体积 0（存在全表扫描风险）" if (item["index_mb"] is not None and item["index_mb"] <= 0) else ""
            facts.append(
                f"{item['schema']}.{item['table']}（{item['total_mb']:,.0f} MB，{item['rows']} 行{note}）"
            )
        self._evaluate(
            rule, True, True, bool(items),
            f"单表规模阈值 {limit:,.0f} MB（100 GiB）",
            facts,
        )

    def _check_leftover_tables(self, ctx: PackageContext) -> None:
        """测试 / 备份 / 复制残留表（A13）。

        纯命名启发式，只做"疑似"提示、不判定业务是否在用 —— 所以 severity 是 low，
        结论把清单交给读者确认。实测三节点各命中 10~17 个（test_patlist、
        tb_template_bak*、tb_template_*_0728、admission_copy1 等）。
        """
        rule = "MYSQL.SCHEMA.TEST_TABLE_LEFTOVER"
        items, scanned = leftover_table_items(ctx)
        if not scanned:
            self._evaluate(rule, True, False, False, "未采集到任何含表名的对象表", [])
            return
        facts = [
            f"按命名模式命中 {len(items)} 个疑似残留对象（仅提示，需人工确认是否在用）",
        ]
        by_pattern: dict[str, list[str]] = {}
        for item in items:
            label = f"{item['schema']}.{item['table']}" if item["schema"] else item["table"]
            by_pattern.setdefault(item["pattern"], []).append(label)
        for label, names in by_pattern.items():
            facts.append(f"{label}：{'、'.join(names)}")
        self._evaluate(
            rule, True, True, bool(items),
            "命名模式启发式（test_ / _bak / 日期后缀 / _copyN）",
            facts,
        )


def _read_evidence_text(path: Path, limit: int = 400_000) -> str:
    """读取证据文件正文；读不到返回空串（= 不可用，不是"正常"）。"""
    try:
        return path.read_text(encoding="utf-8", errors="replace")[:limit]
    except OSError:
        return ""


def _hugepage_states(ctx: PackageContext) -> dict[str, str]:
    """还原 THP / HugePages 状态。

    ``tables/hugepages.tsv`` 的 value 形如 ``[always] madvise never``，
    方括号包住的是当前生效值；``nr_hugepages`` 是已配置的静态大页数量。
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


def _swap_totals(ctx: PackageContext) -> tuple[float | None, float | None]:
    """从 ``tables/memory_snapshot.tsv`` 还原 Swap 总量与已用量（字节）。

    该表落盘的是 ``free`` 的空格对齐输出，通用分隔符解析会把整行塞进一个键，
    所以这里按行取字符串再切 token；取不到就返回 (None, None)，
    让调用方落 not_evaluated 而不是拿 0 当数据。
    """
    for row in ctx.tables.get("memory_snapshot", []) or []:
        line = next(
            (str(value) for value in row.values() if isinstance(value, str) and value.strip()),
            "",
        )
        if "swap" not in line.lower():
            continue
        numbers = [safe_float(token) for token in line.replace(":", " ").split()]
        numbers = [number for number in numbers if number is not None]
        if len(numbers) >= 2:
            return numbers[0], numbers[1]
    return None, None


def _ip_sort_key(ctx: PackageContext) -> tuple[int, ...]:
    """按 IPv4 数值序排节点，保证跨节点结论的锚点选择是确定的。"""
    identity = ctx.snapshot.get("instance_identity") or {}
    raw = str(identity.get("instance_ip") or "")
    octets: list[int] = []
    for part in raw.split("."):
        try:
            octets.append(int(part))
        except ValueError:
            octets.append(999)
    while len(octets) < 4:
        octets.append(999)
    return tuple(octets[:4])


def _node_brief(host: dict[str, Any]) -> str:
    ip = str(host.get("primary_ip") or "").strip() or "IP 未采集"
    name = str(host.get("hostname") or "").strip() or "主机名未采集"
    machine_id = str(host.get("machine_id") or "").strip()
    return f"{name}（{ip}，machine-id {machine_id[:12] or '未采集'}）"


def evaluate_identity_conflicts(
    contexts: list[PackageContext],
) -> dict[str, tuple[list[Finding], list[RuleEvaluation]]]:
    """跨实例的主机名 / machine-id 冲突检查。

    规则引擎是逐实例执行的，看不到对端，因此这类"两台机器撞了"的问题必须在
    拿到全部实例之后判断。冲突组只产出 **一条** finding，挂在组内 IP 最小的
    节点上（锚点确定，不随传包顺序漂移），事实里列全所有成员，避免同一个
    问题在台账里按节点数重复计数。

    finding_id 由调用方（analyzer）在合并进各实例时统一编号。
    """
    groups: list[dict[str, Any]] = []
    for key, label in (("hostname", "主机名"), ("machine_id", "machine-id")):
        buckets: dict[str, list[PackageContext]] = {}
        for ctx in contexts:
            value = str((ctx.snapshot.get("host_identity") or {}).get(key) or "").strip()
            if value:
                buckets.setdefault(value, []).append(ctx)
        for value, members in buckets.items():
            if len(members) >= 2:
                groups.append({"key": key, "label": label, "value": value, "members": members})
    if not groups:
        return {}

    rule = "MYSQL.SYSTEM.IDENTITY_CONFLICT"
    cfg = (load_rules_config().get("rules") or {}).get(rule) or {}
    result: dict[str, tuple[list[Finding], list[RuleEvaluation]]] = {}
    for group in groups:
        members = sorted(group["members"], key=_ip_sort_key)
        anchor = members[0]
        names = "、".join(
            _node_brief(ctx.snapshot.get("host_identity") or {}) for ctx in members
        )
        facts = [
            f"{group['label']} 重复：{group['value']}",
            f"涉及节点：{names}",
            "本条目挂在 IP 最小的节点上，事实列出全部冲突节点，不按节点重复计数",
        ]
        finding = Finding(
            rule_id=rule,
            severity=str(cfg.get("severity", "medium")),
            title=f"{group['label']}重复（{len(members)} 个节点）",
            category=str(cfg.get("category", "system")),
            summary=str(cfg.get("summary", "")),
            facts=facts,
            recommendation=str(cfg.get("recommendation", "")),
            evidence_refs=list(cfg.get("evidence_refs") or []),
            requires_restart=cfg.get("requires_restart", False),
            status="triggered",
            confidence=float(cfg.get("confidence", 1.0)),
            finding_id="",
        )
        evaluation = RuleEvaluation(
            rule_id=rule,
            category=str(cfg.get("category", "system")),
            status="triggered",
            reason=f"{group['label']} 在 {len(members)} 个节点上重复",
            severity_if_triggered=str(cfg.get("severity", "medium")),
            evidence_refs=list(cfg.get("evidence_refs") or []),
            confidence=float(cfg.get("confidence", 1.0)),
        )
        bucket_findings, bucket_evaluations = result.setdefault(anchor.instance_id, ([], []))
        bucket_findings.append(finding)
        bucket_evaluations.append(evaluation)
    return result


def evaluate_replication_retention(
    contexts: list[PackageContext],
) -> dict[str, tuple[list[Finding], list[RuleEvaluation]]]:
    """跨实例的 Binlog 保留期倒挂检查。

    "从库保留期长于主库"意味着主库已轮转掉的日志在从库还长期占盘：既浪费空间，
    也让"从库还能补做多久"这个问题变得不可控。规则引擎逐实例执行看不到对端，
    所以和标识冲突一样放到拿到全部实例之后再判。

    只在能明确识别出**唯一源端**（自身没有任何真实上游行、且存在有上游的节点）时
    才判定；识别不出（多主 / 环形 / 单机）整条不适用 —— 不适用不是"通过"。
    finding 挂在保留期最长的那个副本上，事实里同时给出源端取值，便于直接对数。
    """
    if len(contexts) < 2:
        return {}
    retention: dict[str, float] = {}
    upstreams: dict[str, int] = {}
    for ctx in contexts:
        value = safe_float(runtime_variables(ctx).get("binlog_expire_logs_seconds"))
        if value is not None:
            retention[ctx.instance_id] = value
        real_rows, _ = split_self_referencing_replica_rows(
            ctx.tables.get("replica_status", []), local_host_names(ctx)
        )
        upstreams[ctx.instance_id] = len(real_rows)

    sources = [ctx for ctx in contexts if upstreams.get(ctx.instance_id, 0) == 0]
    replicas = [ctx for ctx in contexts if upstreams.get(ctx.instance_id, 0) > 0]
    if len(sources) != 1 or not replicas:
        return {}
    source = sources[0]
    source_retention = retention.get(source.instance_id)
    if source_retention is None:
        return {}

    offenders = [
        ctx for ctx in replicas
        if (retention.get(ctx.instance_id) or 0) > source_retention
    ]
    if not offenders:
        return {}

    rule = "MYSQL.REPLICATION.RETENTION_DRIFT"
    cfg = (load_rules_config().get("rules") or {}).get(rule) or {}
    anchor = min(offenders, key=lambda ctx: _ip_sort_key(ctx))
    facts = [
        f"源端 {_node_brief(source.snapshot.get('host_identity') or {})} 保留 "
        f"{source_retention:.0f} 秒",
    ]
    for ctx in offenders:
        facts.append(
            f"副本 {_node_brief(ctx.snapshot.get('host_identity') or {})} 保留 "
            f"{retention[ctx.instance_id]:.0f} 秒（长于源端）"
        )
    finding = Finding(
        rule_id=rule,
        severity=str(cfg.get("severity", "low")),
        title=f"副本 Binlog 保留期长于源端（{len(offenders)} 个节点）",
        category=str(cfg.get("category", "replication")),
        summary=str(cfg.get("summary", "")),
        facts=facts,
        recommendation=str(cfg.get("recommendation", "")),
        evidence_refs=list(cfg.get("evidence_refs") or []),
        requires_restart=cfg.get("requires_restart", False),
        status="triggered",
        confidence=float(cfg.get("confidence", 0.9)),
        finding_id="",
    )
    evaluation = RuleEvaluation(
        rule_id=rule,
        category=str(cfg.get("category", "replication")),
        status="triggered",
        reason=f"{len(offenders)} 个副本的 binlog 保留期长于源端",
        severity_if_triggered=str(cfg.get("severity", "low")),
        evidence_refs=list(cfg.get("evidence_refs") or []),
        confidence=float(cfg.get("confidence", 0.9)),
    )
    return {anchor.instance_id: ([finding], [evaluation])}


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


