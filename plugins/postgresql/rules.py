#!/usr/bin/env python3
"""PG inspection rule engine — config-driven, threshold-externalized."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from inspection_core.system_checks import os_pressure_report

RULES_CONFIG = Path(__file__).resolve().parent / "inspection_rules.json"


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

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "category": self.category,
            "status": self.status,
            "reason": self.reason,
            "severity_if_triggered": self.severity_if_triggered,
            "finding_id": self.finding_id,
        }


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
                     severity_override: str | None = None) -> None:
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
                                    reason=reason, severity_if_triggered=severity)
                evaluations.append(ev)
                return

            if not available:
                ev = RuleEvaluation(rule_id=rule_id, category=category, status="not_evaluated",
                                    reason=reason, severity_if_triggered=severity)
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
                                                   finding_id=fid))
            else:
                evaluations.append(RuleEvaluation(rule_id=rule_id, category=category, status="passed",
                                                   reason=reason, severity_if_triggered=severity))

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
        evaluate("PG.CACHE.HIT_RATIO", cache is not None, True,
                 cache is not None and cache < cache_limit,
                 f"阈值 {cache_limit}%", [f"Buffer 命中率：{cache:.1f}%"] if cache else [])

        # === Connection usage ===
        conn = metrics.get("connection_usage")
        conn_max = metrics.get("max_connections")
        conn_cur = metrics.get("current_connections")
        conn_limit = threshold("PG.CONNECTION.USAGE", "connection_usage_warning", 80)
        evaluate("PG.CONNECTION.USAGE", conn is not None, True,
                 conn is not None and conn >= conn_limit,
                 f"阈值 {conn_limit}%",
                 [f"连接使用率：{conn:.1f}% ({conn_cur}/{conn_max})"] if conn else [])

        # === Dead tuples ===
        dead_tup = metrics.get("dead_tuples_total")
        dead_limit = threshold("PG.VACUUM.DEAD_TUPLE", "dead_tuple_total_warning", 100000)
        evaluate("PG.VACUUM.DEAD_TUPLE", dead_tup is not None, True,
                 dead_tup is not None and dead_tup >= dead_limit,
                 f"阈值 {dead_limit}", [f"死元组总数：{dead_tup}"] if dead_tup else [])

        max_dead = metrics.get("max_dead_ratio")
        evaluate("PG.VACUUM.DEAD_RATIO", max_dead is not None, True,
                 max_dead is not None and max_dead > 10,
                 "阈值 10%", [f"最大死元组比例：{max_dead}%"] if max_dead else [])

        # === Stale tables (never vacuumed) ===
        stale = metrics.get("stale_tables")
        evaluate("PG.VACUUM.STALE", stale is not None, True,
                 stale is not None and stale > 0,
                 "存在从未 VACUUM 的表", [f"从未 VACUUM 的表：{stale} 张"] if stale else [])

        # === Table bloat ===
        bloat_count = metrics.get("bloat_table_count")
        bloat_limit = threshold("PG.BLOAT.TABLE", "bloat_count_warning", 5)
        evaluate("PG.BLOAT.TABLE", bloat_count is not None, True,
                 bloat_count is not None and bloat_count >= bloat_limit,
                 f"阈值 {bloat_limit} 张", [f"膨胀表数量：{bloat_count} 张"] if bloat_count else [])

        # === Unused indexes ===
        unused = metrics.get("unused_index_count")
        evaluate("PG.INDEX.UNUSED", unused is not None, True,
                 unused is not None and unused > 0,
                 "存在未使用的索引", [f"未使用索引数：{unused}"] if unused else [])

        # === Duplicate indexes ===
        dup = metrics.get("duplicate_index_count")
        evaluate("PG.INDEX.DUPLICATE", dup is not None, True,
                 dup is not None and dup > 0,
                 "存在重复索引", [f"重复索引数：{dup}"] if dup else [])

        # === Replication lag ===
        lag = metrics.get("max_replication_lag_bytes")
        lag_limit = threshold("PG.REPLICATION.LAG", "lag_bytes_warning", 100 * 1024 * 1024)
        evaluate("PG.REPLICATION.LAG", lag is not None and lag > 0, True,
                 lag > lag_limit if (lag is not None and lag > 0) else False,
                 f"阈值 {lag_limit} bytes", [f"最大复制延迟：{lag} bytes"] if lag else [])

        # === Archive failures ===
        arch = metrics.get("archive_failed_count")
        evaluate("PG.WAL.ARCHIVE", arch is not None, True,
                 arch is not None and arch > 0,
                 "归档失败", [f"归档失败次数：{arch}"] if arch else [])

        # === XID age ===
        xid = metrics.get("max_xid_age")
        xid_limit = threshold("PG.AGE.XID", "xid_age_warning", 200000000)
        evaluate("PG.AGE.XID", xid is not None, True,
                 xid is not None and xid >= xid_limit,
                 f"阈值 {xid_limit}", [f"最大事务年龄：{xid}"] if xid else [])

        # === Long transactions ===
        long_count = metrics.get("long_transaction_count")
        long_sec = metrics.get("max_transaction_seconds")
        evaluate("PG.LONG.TRANSACTION", long_count is not None, True,
                 long_count is not None and long_count > 0,
                 "存在长时间运行的事务", [f"长事务数：{long_count}，最长 {long_sec} 秒"] if long_count else [])

        # === Lock waits ===
        locks = metrics.get("lock_wait_count")
        evaluate("PG.LOCK.WAIT", locks is not None, True,
                 locks is not None and locks > 0,
                 "存在锁等待", [f"锁等待数：{locks}"] if locks else [])

        # === Partition count ===
        max_part = metrics.get("max_partitions")
        part_limit = threshold("PG.PARTITION.COUNT", "partition_count_warning", 500)
        evaluate("PG.PARTITION.COUNT", max_part is not None, True,
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
        evaluate("PG.SECURITY.AUTH", trust is not None, True,
                 trust is not None and trust,
                 "pg_hba.conf 包含 trust/password 认证", ["存在不够安全的认证方式"])

        weak_pw = metrics.get("weak_password_encryption")
        evaluate("PG.SECURITY.PASSWORD", weak_pw is not None, True,
                 weak_pw is not None and weak_pw,
                 "密码加密算法不安全", ["password_encryption=md5，建议改为 scram-sha-256"])

        # === Key settings ===
        autovac = metrics.get("autovacuum")
        evaluate("PG.SETTINGS.AUTOVACUUM", autovac is not None, True,
                 autovac is not None and autovac == "off",
                 "autovacuum 已关闭", ["生产环境强烈建议开启 autovacuum"])

        wal_lvl = metrics.get("wal_level")
        evaluate("PG.SETTINGS.WAL_LEVEL", wal_lvl is not None, True,
                 wal_lvl is not None and wal_lvl == "minimal",
                 "wal_level=minimal", ["WAL 级别过低，不支持复制和 PITR"])

        archive = metrics.get("archive_mode")
        evaluate("PG.SETTINGS.ARCHIVE", archive is not None, True,
                 archive is not None and archive == "off",
                 "archive_mode 未开启", ["建议开启归档以支持 PITR"])

        # === No primary key tables ===
        no_pk = metrics.get("no_pk_count")
        evaluate("PG.STORAGE.NO_PRIMARY_KEY", no_pk is not None, True,
                 no_pk is not None and no_pk > 0,
                 "存在无主键的表", [f"无主键表数量：{no_pk}"] if no_pk else [])

        # === SSL ===
        ssl = metrics.get("ssl_enabled")
        evaluate("PG.SECURITY.SSL", ssl is not None, True,
                 ssl is not None and not ssl,
                 "SSL 未开启", ["生产环境强烈建议启用 SSL 加密"])

        # === Inactive replication slots ===
        inactive = metrics.get("inactive_slots")
        evaluate("PG.REPLICATION.SLOT_INACTIVE", inactive is not None, True,
                 inactive is not None and inactive > 0,
                 "存在非活跃复制槽", [f"非活跃槽数：{inactive}，持续积累 WAL 有磁盘写满风险"] if inactive else [])

        # === Slow query log ===
        has_slow = metrics.get("has_slow_query_log")
        evaluate("PG.SETTINGS.SLOW_QUERY_LOG", has_slow is not None, True,
                 has_slow is not None and not has_slow,
                 "慢查询日志未配置", ["建议设置 log_min_duration_statement=1000（记录 >1s 的查询）"])

        # === idle_in_transaction timeout ===
        has_idle = metrics.get("has_idle_timeout")
        evaluate("PG.SETTINGS.IDLE_TIMEOUT", has_idle is not None, True,
                 has_idle is not None and not has_idle,
                 "未设置 idle_in_transaction_session_timeout", ["建议设置此参数防止事务长时间空闲占用连接"])

        # === statement_timeout ===
        has_stmt = metrics.get("has_stmt_timeout")
        evaluate("PG.SETTINGS.STATEMENT_TIMEOUT", has_stmt is not None, True,
                 has_stmt is not None and not has_stmt,
                 "未设置全局 statement_timeout", ["建议设置 statement_timeout 防止单条 SQL 长时间运行"])

        # === Data directory filesystem usage ===
        fs_use = metrics.get("data_dir_filesystem_usage")
        evaluate("PG.FILESYSTEM.DATA_DIR", fs_use is not None, True,
                 fs_use is not None and fs_use >= 80,
                 f"数据目录磁盘 {fs_use:.1f}%" if fs_use is not None else "数据目录磁盘使用率未知",
                 [f"数据目录磁盘使用率：{fs_use:.1f}%"] if fs_use else [])

        # === Invalid indexes ===
        inv_idx = metrics.get("invalid_index_count")
        evaluate("PG.INDEX.INVALID", inv_idx is not None, True,
                 inv_idx is not None and inv_idx > 0,
                 "存在无效索引", [f"无效索引数：{inv_idx}（CONCURRENTLY 创建失败遗留）"] if inv_idx else [])

        # === Superuser count ===
        su_count = metrics.get("superuser_count")
        evaluate("PG.ROLE.SUPERUSER_COUNT", su_count is not None, True,
                 su_count is not None and su_count > 3,
                 "超级用户过多", [f"超级用户数：{su_count}，安全风险"] if su_count else [])

        # === Logging collector ===
        log_col = metrics.get("log_collector_on")
        log_dest = metrics.get("log_destination", "")
        evaluate("PG.LOG.CONFIG", log_col is not None, True,
                 log_col is not None and not log_col and "stderr" not in str(log_dest),
                 "日志收集未正确配置", ["建议设置 logging_collector=on 并配置 log_directory"])

        return findings, evaluations
