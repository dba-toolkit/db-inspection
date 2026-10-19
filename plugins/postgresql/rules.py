#!/usr/bin/env python3
"""PG inspection rule engine — config-driven, threshold-externalized."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from inspection_core.collection_state import state_is_collected
from inspection_core.system_checks import os_pressure_report

RULES_CONFIG = Path(__file__).resolve().parent / "inspection_rules.json"


#: 规则 id → 它判的那条指标（`metrics` 里的键）。
#:
#: 规则层据此从 `metrics["source_status"]`（= 采集台账里那张表的状态）判断
#: "这条判据的数据来源**采到没有**"。此前 `available` 被硬编码成 `True`，
#: 于是空表算出 0 就判「通过」——**假通过**。
#:
#: ★ 新增规则必须同步登记；漏登记 ⇒ 恒 `not_evaluated`（响亮地"证据不足"，
#:   而不是静默地假通过）。不依赖采集表（如备份可见性）的规则**刻意不登记**。
RULE_METRIC_KEY: dict[str, str] = {
    "PG.CACHE.HIT_RATIO": "cache_hit_ratio",
    "PG.CONNECTION.USAGE": "connection_usage",
    "PG.VACUUM.DEAD_TUPLE": "dead_tuples_total",
    "PG.VACUUM.DEAD_RATIO": "max_dead_ratio",
    "PG.VACUUM.STALE": "stale_tables",
    "PG.BLOAT.TABLE": "bloat_table_count",
    "PG.INDEX.UNUSED": "unused_index_count",
    "PG.INDEX.DUPLICATE": "duplicate_index_count",
    "PG.INDEX.INVALID": "invalid_index_count",
    "PG.INDEX.FK_NO_INDEX": "missing_fk_index_count",
    "PG.REPLICATION.LAG": "max_replication_lag_bytes",
    "PG.REPLICATION.SLOT_INACTIVE": "inactive_slots",
    "PG.WAL.ARCHIVE": "archive_failed_count",
    "PG.AGE.XID": "max_xid_age",
    "PG.LONG.TRANSACTION": "long_transaction_count",
    "PG.LOCK.WAIT": "lock_wait_count",
    "PG.PARTITION.COUNT": "max_partitions",
    "PG.STORAGE.NO_PRIMARY_KEY": "no_pk_count",
    "PG.ROLE.SUPERUSER_COUNT": "superuser_count",
    "PG.SECURITY.AUTH": "has_trust_auth",
    "PG.SECURITY.PASSWORD": "weak_password_encryption",
    "PG.SECURITY.SSL": "ssl_enabled",
    "PG.SETTINGS.AUTOVACUUM": "autovacuum",
    "PG.SETTINGS.WAL_LEVEL": "wal_level",
    "PG.SETTINGS.ARCHIVE": "archive_mode",
    "PG.SETTINGS.SLOW_QUERY_LOG": "has_slow_query_log",
    "PG.SETTINGS.IDLE_TIMEOUT": "has_idle_timeout",
    "PG.SETTINGS.STATEMENT_TIMEOUT": "has_stmt_timeout",
    "PG.FILESYSTEM.DATA_DIR": "data_dir_filesystem_usage",
    "PG.LOG.CONFIG": "log_collector_on",
}


@dataclass
class Finding:
    rule_id: str
    severity: str
    title: str
    category: str
    summary: str
    facts: list[str]
    recommendation: str
    evidence_refs: list[str] = field(default_factory=list)
    finding_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "finding_id": self.finding_id,
            "rule_id": self.rule_id,
            "status": "triggered",
            "severity": self.severity,
            "category": self.category,
            "title": self.title,
            "summary": self.summary,
            "facts": self.facts,
            "recommendation": self.recommendation,
            "evidence_refs": self.evidence_refs,
        }


@dataclass
class RuleEvaluation:
    rule_id: str
    category: str
    status: str
    reason: str
    severity_if_triggered: str | None = None
    finding_id: str | None = None
    #: 这条判据的数据来源在**采集侧**声明的状态（`metrics["source_status"]`）。
    #: 报告层的规则项靠它印真实采集状态 —— `quality.non_ok_items` 只记**失败态**
    #: （按采集项 id 索引），规则 id 在那本账里永远查不到，会恒落回默认「正常」。
    source_status: str = ""

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "rule_id": self.rule_id,
            "category": self.category,
            "status": self.status,
            "reason": self.reason,
            "severity_if_triggered": self.severity_if_triggered,
            "finding_id": self.finding_id,
        }
        # 条件注入：旧基线 `analysis.json` 的 `rule_evaluations` 里没有这个键，
        # 无条件写入会打穿逐字典等值比对的冻结契约测试。
        if self.source_status:
            payload["source_status"] = self.source_status
        return payload


def load_rules_config(path: Path | None = None) -> dict[str, Any]:
    target = path or RULES_CONFIG
    with target.open("r", encoding="utf-8") as f:
        cfg = json.load(f)
    if not isinstance(cfg, dict):
        raise ValueError(f"Rules config must be a JSON object: {target}")
    return cfg


class RuleEngine:
    def __init__(self, config_path: Path | None = None) -> None:
        self.config = load_rules_config(config_path)
        self._globals = self.config.get("globals", {})
        self._rules = self.config.get("rules", {})

    def run(self, ctx: Any, metrics: dict[str, Any], quality: dict[str, Any]) -> tuple[list[Finding], list[RuleEvaluation]]:
        findings: list[Finding] = []
        evaluations: list[RuleEvaluation] = []
        fid_counter = 0

        def evaluate(rule_id: str, applicable: bool, available: bool, triggered: bool,
                     reason: str, facts: list[str], evidence_refs: list[str] | None = None,
                     severity_override: str | None = None, source_status: str = "") -> None:
            nonlocal fid_counter
            rule = self._rules.get(rule_id, {})
            category = rule.get("category", "")
            severity = rule.get("severity", "info")
            title = rule.get("title", rule_id)
            summary = rule.get("summary", "")
            recommendation = rule.get("recommendation", "")
            if severity_override is not None:
                # Two-tier rule: the pack declares the base level, the caller
                # promotes it when the harder threshold is crossed.
                severity = severity_override

            if not applicable:
                ev = RuleEvaluation(rule_id=rule_id, category=category, status="not_evaluated",
                                    reason=reason, severity_if_triggered=severity,
                                    source_status=source_status)
                evaluations.append(ev)
                return

            if not available:
                ev = RuleEvaluation(rule_id=rule_id, category=category, status="not_evaluated",
                                    reason=reason, severity_if_triggered=severity,
                                    source_status=source_status)
                evaluations.append(ev)
                return

            if triggered:
                fid_counter += 1
                fid = f"PG-{fid_counter:03d}"
                f = Finding(rule_id=rule_id, severity=severity, title=title, category=category,
                            summary=summary, facts=facts, recommendation=recommendation,
                            evidence_refs=evidence_refs or [], finding_id=fid)
                findings.append(f)
                evaluations.append(RuleEvaluation(rule_id=rule_id, category=category, status="triggered",
                                                   reason=reason, severity_if_triggered=severity,
                                                   finding_id=fid, source_status=source_status))
            else:
                evaluations.append(RuleEvaluation(rule_id=rule_id, category=category, status="passed",
                                                   reason=reason, severity_if_triggered=severity,
                                                   source_status=source_status))

        #: `metrics["source_status"]`：指标键 → 采集侧那张表的状态（空串 = 没采到）。
        source_status = metrics.get("source_status") or {}
        metric_keys = RULE_METRIC_KEY

        def evaluate_metric(rule_id: str, triggered: bool, reason: str, facts: list[str],
                            severity_override: str | None = None) -> None:
            """按**采集侧来源状态**决定这条规则评不评（F-22）。

            `key` 由 `RULE_METRIC_KEY` 给出。那张表**没采到**时落 `not_evaluated`
            并写「证据不足」——不得因为空表算出 0 就判「通过」。
            ★ 未登记的规则（如备份可见性，不依赖采集表）走 `evaluate` 原路径。
            """
            key = metric_keys.get(rule_id)
            if not key:
                # ★ **漏登记必须响亮失败**，不能回落到"available=True"：那等于悄悄退回
                #   "空表算出 0 就判通过"（本批要修的病）。不登记 = 无法判断采到没有。
                raise KeyError(
                    f"规则 {rule_id} 未在 RULE_METRIC_KEY 登记数据来源；"
                    "不登记就无法判断'采集到没有'，会静默退回假通过"
                )
            if key not in metrics:
                # 映射写错（指向一个 `derive_metrics` 不会返回的指标）时同样响亮报错：
                # 否则 `metrics.get(key)` 恒 None ⇒ 规则恒 `not_evaluated`（静默）。
                raise KeyError(f"规则 {rule_id} 映射到不存在指标 {key!r}；请核对 RULE_METRIC_KEY")
            state = str(source_status.get(key) or "")
            collected = state_is_collected(state)
            evaluate(
                rule_id,
                metrics.get(key) is not None,
                collected,
                triggered if collected else False,
                reason if collected else "证据不足：本判据所需采集项未采集",
                facts if collected else [],
                severity_override=severity_override,
                source_status=state,
            )

        def threshold(rule_id: str, key: str, default: Any = None) -> Any:
            return self._rules.get(rule_id, {}).get("threshold", {}).get(key, self._globals.get(key, default))

        # === Collection integrity ===
        quality_score = quality.get("score", 100)
        evaluate("COMMON.COLLECTION.QUALITY", True, True,
                 quality_score < self._globals.get("quality_score_min", 80),
                 f"采集完整度 {quality_score:.1f}%", [f"采集完整度：{quality_score:.1f}%"])

        err_count = quality.get("errors", 0)
        evaluate("COMMON.COLLECTION.ERRORS", True, True,
                 err_count > 0, f"采集错误项: {err_count}", [f"错误项数量：{err_count}"])

        # === System resources ===
        # 判据、阈值、数据窗口全部来自 inspection_core.system_checks；
        # PG 只决定自己上报四条（含 DISK_UTIL）。
        source, verdicts = os_pressure_report(metrics)
        for verdict in verdicts:
            evaluate(
                verdict["rule_id"], verdict["available"], True, verdict["triggered"],
                f"阈值 {verdict['threshold']:g}%；{source['reason']}",
                [verdict["fact"]] if verdict["fact"] else [],
            )

        # === Cache hit ratio ===
        cache = metrics.get("cache_hit_ratio")
        cache_limit = threshold("PG.CACHE.HIT_RATIO", "cache_hit_min", 95)
        evaluate_metric("PG.CACHE.HIT_RATIO",
                 cache is not None and cache < cache_limit,
                 f"阈值 {cache_limit}%", [f"Buffer 命中率：{cache:.1f}%"] if cache else [])

        # === Connection usage ===
        conn = metrics.get("connection_usage")
        conn_max = metrics.get("max_connections")
        conn_cur = metrics.get("current_connections")
        conn_limit = threshold("PG.CONNECTION.USAGE", "connection_usage_warning", 80)
        evaluate_metric("PG.CONNECTION.USAGE",
                 conn is not None and conn >= conn_limit,
                 f"阈值 {conn_limit}%",
                 [f"连接使用率：{conn:.1f}% ({conn_cur}/{conn_max})"] if conn else [])

        # === Dead tuples ===
        dead_tup = metrics.get("dead_tuples_total")
        dead_limit = threshold("PG.VACUUM.DEAD_TUPLE", "dead_tuple_total_warning", 100000)
        evaluate_metric("PG.VACUUM.DEAD_TUPLE",
                 dead_tup is not None and dead_tup >= dead_limit,
                 f"阈值 {dead_limit}", [f"死元组总数：{dead_tup}"] if dead_tup else [])

        max_dead = metrics.get("max_dead_ratio")
        evaluate_metric("PG.VACUUM.DEAD_RATIO",
                 max_dead is not None and max_dead > 10,
                 "阈值 10%", [f"最大死元组比例：{max_dead}%"] if max_dead else [])

        # === Stale tables (never vacuumed) ===
        stale = metrics.get("stale_tables")
        evaluate_metric("PG.VACUUM.STALE",
                 stale is not None and stale > 0,
                 "存在从未 VACUUM 的表", [f"从未 VACUUM 的表：{stale} 张"] if stale else [])

        # === Table bloat ===
        bloat_count = metrics.get("bloat_table_count")
        bloat_limit = threshold("PG.BLOAT.TABLE", "bloat_count_warning", 5)
        evaluate_metric("PG.BLOAT.TABLE",
                 bloat_count is not None and bloat_count >= bloat_limit,
                 f"阈值 {bloat_limit} 张", [f"膨胀表数量：{bloat_count} 张"] if bloat_count else [])

        # === Unused indexes ===
        unused = metrics.get("unused_index_count")
        evaluate_metric("PG.INDEX.UNUSED",
                 unused is not None and unused > 0,
                 "存在未使用的索引", [f"未使用索引数：{unused}"] if unused else [])

        # === Duplicate indexes ===
        dup = metrics.get("duplicate_index_count")
        evaluate_metric("PG.INDEX.DUPLICATE",
                 dup is not None and dup > 0,
                 "存在重复索引", [f"重复索引数：{dup}"] if dup else [])

        # === Foreign keys without a supporting index ===
        # 外键的引用侧缺索引时，父表删/改行要全表扫描子表并对其加锁（PG 不会替外键
        # 自动建索引），高并发下直接表现为锁等待与写放大。此前 `db.*.missing_fk_indexes`
        # **采到了却没有任何规则与章节消费**（真机 mydb 有 1 条：orders.user_id）。
        fk_no_idx = metrics.get("missing_fk_index_count")
        evaluate_metric("PG.INDEX.FK_NO_INDEX",
                 fk_no_idx is not None and fk_no_idx > 0,
                 "外键列缺少支撑索引",
                 [f"外键缺索引数：{fk_no_idx}"] if fk_no_idx else [])

        # === Replication lag ===
        lag = metrics.get("max_replication_lag_bytes")
        lag_limit = threshold("PG.REPLICATION.LAG", "lag_bytes_warning", 100 * 1024 * 1024)
        evaluate_metric("PG.REPLICATION.LAG",
                 lag > lag_limit if (lag is not None and lag > 0) else False,
                 f"阈值 {lag_limit} bytes", [f"最大复制延迟：{lag} bytes"] if lag else [])

        # === Archive failures ===
        arch = metrics.get("archive_failed_count")
        evaluate_metric("PG.WAL.ARCHIVE",
                 arch is not None and arch > 0,
                 "归档失败", [f"归档失败次数：{arch}"] if arch else [])

        # === XID age ===
        xid = metrics.get("max_xid_age")
        xid_limit = threshold("PG.AGE.XID", "xid_age_warning", 200000000)
        evaluate_metric("PG.AGE.XID",
                 xid is not None and xid >= xid_limit,
                 f"阈值 {xid_limit}", [f"最大事务年龄：{xid}"] if xid else [])

        # === Long transactions ===
        long_count = metrics.get("long_transaction_count")
        long_sec = metrics.get("max_transaction_seconds")
        evaluate_metric("PG.LONG.TRANSACTION",
                 long_count is not None and long_count > 0,
                 "存在长时间运行的事务", [f"长事务数：{long_count}，最长 {long_sec} 秒"] if long_count else [])

        # === Lock waits ===
        locks = metrics.get("lock_wait_count")
        evaluate_metric("PG.LOCK.WAIT",
                 locks is not None and locks > 0,
                 "存在锁等待", [f"锁等待数：{locks}"] if locks else [])

        # === Partition count ===
        max_part = metrics.get("max_partitions")
        part_limit = threshold("PG.PARTITION.COUNT", "partition_count_warning", 500)
        evaluate_metric("PG.PARTITION.COUNT",
                 max_part is not None and max_part >= part_limit,
                 f"阈值 {part_limit}", [f"最大分区数：{max_part}"] if max_part else [])

        # === Backup ===
        has_tool = metrics.get("has_backup_tool")
        has_cron = metrics.get("has_backup_cron")
        backup_assessed = metrics.get("backup_assessment_complete")
        evaluate("PG.BACKUP.CONFIGURED", backup_assessed is True, True,
                 not has_tool and not has_cron,
                 "未检测到备份工具或定时任务", ["未发现 pgbackrest/barman 或备份 crontab"])

        # === Security ===
        trust = metrics.get("has_trust_auth")
        evaluate_metric("PG.SECURITY.AUTH",
                 trust is not None and trust,
                 "pg_hba.conf 包含 trust/password 认证", ["存在不够安全的认证方式"])

        weak_pw = metrics.get("weak_password_encryption")
        evaluate_metric("PG.SECURITY.PASSWORD",
                 weak_pw is not None and weak_pw,
                 "密码加密算法不安全", ["password_encryption=md5，建议改为 scram-sha-256"])

        # === Key settings ===
        autovac = metrics.get("autovacuum")
        evaluate_metric("PG.SETTINGS.AUTOVACUUM",
                 autovac is not None and autovac == "off",
                 "autovacuum 已关闭", ["生产环境强烈建议开启 autovacuum"])

        wal_lvl = metrics.get("wal_level")
        evaluate_metric("PG.SETTINGS.WAL_LEVEL",
                 wal_lvl is not None and wal_lvl == "minimal",
                 "wal_level=minimal", ["WAL 级别过低，不支持复制和 PITR"])

        archive = metrics.get("archive_mode")
        evaluate_metric("PG.SETTINGS.ARCHIVE",
                 archive is not None and archive == "off",
                 "archive_mode 未开启", ["建议开启归档以支持 PITR"])

        # === No primary key tables ===
        no_pk = metrics.get("no_pk_count")
        evaluate_metric("PG.STORAGE.NO_PRIMARY_KEY",
                 no_pk is not None and no_pk > 0,
                 "存在无主键的表", [f"无主键表数量：{no_pk}"] if no_pk else [])

        # === SSL ===
        ssl = metrics.get("ssl_enabled")
        evaluate_metric("PG.SECURITY.SSL",
                 ssl is not None and not ssl,
                 "SSL 未开启", ["生产环境强烈建议启用 SSL 加密"])

        # === Inactive replication slots ===
        inactive = metrics.get("inactive_slots")
        evaluate_metric("PG.REPLICATION.SLOT_INACTIVE",
                 inactive is not None and inactive > 0,
                 "存在非活跃复制槽", [f"非活跃槽数：{inactive}，持续积累 WAL 有磁盘写满风险"] if inactive else [])

        # === Slow query log ===
        has_slow = metrics.get("has_slow_query_log")
        evaluate_metric("PG.SETTINGS.SLOW_QUERY_LOG",
                 has_slow is not None and not has_slow,
                 "慢查询日志未配置", ["建议设置 log_min_duration_statement=1000（记录 >1s 的查询）"])

        # === idle_in_transaction timeout ===
        has_idle = metrics.get("has_idle_timeout")
        evaluate_metric("PG.SETTINGS.IDLE_TIMEOUT",
                 has_idle is not None and not has_idle,
                 "未设置 idle_in_transaction_session_timeout", ["建议设置此参数防止事务长时间空闲占用连接"])

        # === statement_timeout ===
        has_stmt = metrics.get("has_stmt_timeout")
        evaluate_metric("PG.SETTINGS.STATEMENT_TIMEOUT",
                 has_stmt is not None and not has_stmt,
                 "未设置全局 statement_timeout", ["建议设置 statement_timeout 防止单条 SQL 长时间运行"])

        # === Data directory filesystem usage ===
        fs_use = metrics.get("data_dir_filesystem_usage")
        evaluate_metric("PG.FILESYSTEM.DATA_DIR",
                 fs_use is not None and fs_use >= 80,
                 f"数据目录磁盘 {fs_use:.1f}%" if fs_use is not None else "数据目录磁盘使用率未知",
                 [f"数据目录磁盘使用率：{fs_use:.1f}%"] if fs_use else [])

        # === Invalid indexes ===
        inv_idx = metrics.get("invalid_index_count")
        evaluate_metric("PG.INDEX.INVALID",
                 inv_idx is not None and inv_idx > 0,
                 "存在无效索引", [f"无效索引数：{inv_idx}（CONCURRENTLY 创建失败遗留）"] if inv_idx else [])

        # === Superuser count ===
        su_count = metrics.get("superuser_count")
        evaluate_metric("PG.ROLE.SUPERUSER_COUNT",
                 su_count is not None and su_count > 3,
                 "超级用户过多", [f"超级用户数：{su_count}，安全风险"] if su_count else [])

        # === Logging collector ===
        log_col = metrics.get("log_collector_on")
        log_dest = metrics.get("log_destination", "")
        evaluate_metric("PG.LOG.CONFIG",
                 log_col is not None and not log_col and "stderr" not in str(log_dest),
                 "日志收集未正确配置", ["建议设置 logging_collector=on 并配置 log_directory"])

        return findings, evaluations
