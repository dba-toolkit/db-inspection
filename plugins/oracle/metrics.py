"""Oracle 指标派生：集中计算规则与报告共用的指标，不运行规则、不生成报告。"""

from __future__ import annotations

import math
import statistics
from collections.abc import Sequence
from typing import Any

from .package_adapter import fetch_state


def safe_float(value: Any) -> float | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.upper() in {"NULL", "N/A", "NA", "NONE", "-"}:
        return None
    try:
        return float(text.replace(",", ""))
    except (TypeError, ValueError):
        return None


def safe_int(value: Any) -> int | None:
    number = safe_float(value)
    return None if number is None else int(number)


def summarize(values: list[float]) -> dict[str, Any]:
    data = [float(v) for v in values if v is not None and math.isfinite(float(v))]
    if not data:
        return {"count": 0, "min": None, "average": None, "max": None}
    return {
        "count": len(data),
        "min": round(min(data), 4),
        "average": round(statistics.fmean(data), 4),
        "max": round(max(data), 4),
    }


def counter_rates(rows: list[dict[str, str]], counters: Sequence[str]) -> list[dict[str, Any]]:
    """Oracle v$sysstat 计数器按采样间隔求每秒速率。"""
    if len(rows) < 2:
        return []
    result: list[dict[str, Any]] = []
    for prev, cur in zip(rows, rows[1:]):
        p_ms = safe_float(prev.get("elapsed_ms"))
        c_ms = safe_float(cur.get("elapsed_ms"))
        if p_ms is None or c_ms is None or c_ms <= p_ms:
            continue
        dt = (c_ms - p_ms) / 1000.0
        point: dict[str, Any] = {
            "timestamp": cur.get("timestamp"),
            "elapsed_ms": c_ms,
            "interval_seconds": dt,
        }
        for counter in counters:
            a = safe_float(prev.get(counter))
            b = safe_float(cur.get(counter))
            if a is None or b is None:
                point[counter] = b
                point[counter + "_per_sec"] = None
            elif b >= a:
                point[counter] = b
                point[counter + "_per_sec"] = round((b - a) / dt, 4)
            else:
                point[counter] = b
                point[counter + "_per_sec"] = None
        result.append(point)
    return result


class OracleMetricProvider:
    """从标准 Oracle PackageContext 派生 51 项指标。"""

    def derive(self, ctx: Any) -> dict[str, Any]:
        metrics: dict[str, Any] = {}
        # 「来自 `tables/` 的指标」→ **这项采集侧声明的状态**。
        # ★ 必须单独记一份：`ctx.tables.get(名字, [])` 在「取数键写错」与「查了 0 行」
        #   两种情况下都返回 `[]`，指标值双双落成 0/None；规则层据此写「检查通过」，
        #   而真相可能是**这一项根本没采到**（批 19 的注入实验实测出 3 条恒通过的漏报）。
        #   规则层拿 `source_status[key]` 才能把两者分开（`empty` 算采集过、`unavailable` 不算）。
        source_status: dict[str, str] = metrics.setdefault("source_status", {})

        cpu_rows = ctx.timeseries.get("system_cpu", [])
        mem_rows = ctx.timeseries.get("system_memory", [])
        metrics["system_realtime"] = {
            "cpu_busy_pct": summarize([v for row in cpu_rows if (v := safe_float(row.get("busy_pct"))) is not None]),
            "cpu_iowait_pct": summarize([v for row in cpu_rows if (v := safe_float(row.get("iowait_pct"))) is not None]),
            "memory_used_pct": summarize([v for row in mem_rows if (v := safe_float(row.get("mem_used_pct"))) is not None]),
        }

        ora_rows = ctx.timeseries.get("oracle_sysstat", [])
        ora_rates = counter_rates(ora_rows, [
            "user_commits", "user_rollbacks", "execute_count", "parse_count_total",
            "parse_count_hard",
            "physical_reads", "physical_writes", "redo_size", "sorts_memory", "sorts_disk",
            "consistent_gets", "db_block_gets", "session_logical_reads",
        ])
        workload_rates = ora_rates[1:] if len(ora_rates) > 2 else ora_rates

        commits_per_sec = [safe_float(r.get("user_commits_per_sec")) or 0.0 for r in workload_rates]
        rollbacks_per_sec = [safe_float(r.get("user_rollbacks_per_sec")) or 0.0 for r in workload_rates]
        reads_per_sec = [safe_float(r.get("physical_reads_per_sec")) or 0.0 for r in workload_rates]
        writes_per_sec = [safe_float(r.get("physical_writes_per_sec")) or 0.0 for r in workload_rates]
        redo_per_sec = [safe_float(r.get("redo_size_per_sec")) or 0.0 for r in workload_rates]
        logical_per_sec = [safe_float(r.get("session_logical_reads_per_sec")) or 0.0 for r in workload_rates]

        buffer_hit = None
        if len(ora_rows) >= 2:
            cg0 = safe_float(ora_rows[0].get("consistent_gets")) or 0
            db0 = safe_float(ora_rows[0].get("db_block_gets")) or 0
            pr0 = safe_float(ora_rows[0].get("physical_reads")) or 0
            cg1 = safe_float(ora_rows[-1].get("consistent_gets")) or 0
            db1 = safe_float(ora_rows[-1].get("db_block_gets")) or 0
            pr1 = safe_float(ora_rows[-1].get("physical_reads")) or 0
            total_logical = (cg1 - cg0) + (db1 - db0)
            total_physical = pr1 - pr0
            if total_logical > 0:
                buffer_hit = round((1 - total_physical / total_logical) * 100, 2)

        sort_disk_ratio = None
        if len(ora_rows) >= 2:
            sm0 = safe_float(ora_rows[0].get("sorts_memory")) or 0
            sd0 = safe_float(ora_rows[0].get("sorts_disk")) or 0
            sm1 = safe_float(ora_rows[-1].get("sorts_memory")) or 0
            sd1 = safe_float(ora_rows[-1].get("sorts_disk")) or 0
            if (sm1 - sm0) + (sd1 - sd0) > 0:
                sort_disk_ratio = round((sd1 - sd0) / ((sm1 - sm0) + (sd1 - sd0)) * 100, 2)

        metrics["oracle_realtime"] = {
            "sample_points": len(ora_rows),
            "rate_points": len(ora_rates),
            "commits_per_sec": summarize(commits_per_sec),
            "rollbacks_per_sec": summarize(rollbacks_per_sec),
            "physical_reads_per_sec": summarize(reads_per_sec),
            "physical_writes_per_sec": summarize(writes_per_sec),
            "redo_bytes_per_sec": summarize(redo_per_sec),
            "logical_reads_per_sec": summarize(logical_per_sec),
            "buffer_cache_hit_pct": buffer_hit,
            "sort_disk_ratio_pct": sort_disk_ratio,
            "derived_rate_series": ora_rates,
            "workload_statistics_excluded_initial_intervals": 1 if len(ora_rates) > 2 else 0,
        }

        sar_cpu = ctx.history.get("sar_cpu", [])
        sar_cpu_busy: list[float] = []
        sar_iowait: list[float] = []
        for row in sar_cpu:
            idle = safe_float(row.get("%idle"))
            iowait = safe_float(row.get("%iowait"))
            if idle is not None:
                sar_cpu_busy.append(100.0 - idle)
            if iowait is not None:
                sar_iowait.append(iowait)

        sar_mem = ctx.history.get("sar_memory", [])
        sar_mem_used: list[float] = []
        for row in sar_mem:
            mem_pct = safe_float(row.get("%memused"))
            if mem_pct is not None:
                sar_mem_used.append(mem_pct)

        metrics["system_history"] = {
            "cpu_busy_pct": summarize(sar_cpu_busy),
            "cpu_iowait_pct": summarize(sar_iowait),
            "memory_used_pct": summarize(sar_mem_used),
        }

        tablespace_fetch = ctx.table_fetch(
            "所有表空间容量使用情况", "oracle.tablespace.usage",
        )
        tablespace_rows = tablespace_fetch.rows
        tablespace_usage: list[float] = []
        tablespace_capacity_pct: list[float] = []
        for row in tablespace_rows:
            v = safe_float(row.get("PCT_USED"))
            if v is not None and 0 <= v <= 100:
                tablespace_usage.append(v)
            else:
                for k in list(row.keys()):
                    ku = k.upper().strip()
                    if ku in ("PCT_USED", "USED_PCT", "PERCENT_USED") or (ku.startswith("PCT_") and "USED" in ku):
                        v = safe_float(row.get(k))
                        if v is not None and 0 <= v <= 100:
                            tablespace_usage.append(v)
            used = safe_float(row.get("USED_MB") or row.get("USED"))
            max_mb = safe_float(row.get("MAX_MB"))
            if used is not None and max_mb and max_mb > 0:
                tablespace_capacity_pct.append(used / max_mb * 100)
        metrics["tablespace_max_used_pct"] = max(tablespace_usage) if tablespace_usage else None
        metrics["tablespace_max_capacity_pct"] = max(tablespace_capacity_pct) if tablespace_capacity_pct else None
        # 两个指标同源于一张表，状态也同一个（键按指标名登记，省得规则层再记一套名字）。
        source_status["tablespace_max_used_pct"] = fetch_state(tablespace_fetch)
        source_status["tablespace_max_capacity_pct"] = fetch_state(tablespace_fetch)

        # 取数一律走 `table_fetch`（精确键，**不走子串兜底**）：别名按「2.0 item_id → 1.4 中文标题」
        # 列全，命中哪个键就有哪个键的真实状态；一个都没命中 ⇒ `unavailable`，规则层写「证据不足」。
        asm_fetch = ctx.table_fetch("asm_diskgroup_summary", "oracle.asm.diskgroup_summary", "ASM 磁盘组汇总")
        asm_usage: list[float] = []
        for row in asm_fetch.rows:
            v = safe_float(row.get("USED_PCT")) or safe_float(row.get("PCT")) or safe_float(row.get("%USED"))
            if v is not None:
                asm_usage.append(v)
        metrics["asm_max_used_pct"] = max(asm_usage) if asm_usage else None
        source_status["asm_max_used_pct"] = fetch_state(asm_fetch)

        inv_fetch = ctx.table_fetch("无效对象检查", "oracle.basic.invalid_objects", "无效对象统计")
        inv_count = inv_fetch.counted
        for row in inv_fetch.rows:
            v = safe_int(row.get("COUNT")) or safe_int(row.get("INVALID_COUNT"))
            if v is not None:
                inv_count = v
                break
        metrics["invalid_object_count"] = inv_count
        source_status["invalid_object_count"] = fetch_state(inv_fetch)

        txn_fetch = ctx.table_fetch("长事务检查", "oracle.session.long_transactions", "长事务（运行超过1小时）")
        metrics["long_transaction_count"] = txn_fetch.counted
        source_status["long_transaction_count"] = fetch_state(txn_fetch)

        lock_fetch = ctx.table_fetch("锁等待链", "oracle.session.lock_chain", "锁等待链（阻塞者 → 被阻塞者）")
        metrics["lock_wait_count"] = lock_fetch.counted
        source_status["lock_wait_count"] = fetch_state(lock_fetch)

        backup_fetch = ctx.table_fetch("最近RMAN备份任务状态", "oracle.rman.backup_summary", "最近备份集汇总")
        metrics["recent_backup_count"] = backup_fetch.counted
        has_recent = False
        for row in backup_fetch.rows:
            status_val = str(row.get("STATUS", "")).upper()
            if status_val in {"COMPLETED", "COMPLETED WITH WARNINGS"}:
                has_recent = True
        metrics["has_recent_backup"] = has_recent
        source_status["has_recent_backup"] = fetch_state(backup_fetch)

        collector_host = str(ctx.snapshot.get("host", "")).strip()
        is_local = (collector_host == "") or (collector_host in ("127.0.0.1", "localhost", "::1")) or collector_host.startswith("127.")
        sar_cpu_history = ctx.history.get("sar_cpu", [])
        history_usable = len(sar_cpu_history) > 10
        metrics["scope"] = {
            "database_target_is_local": is_local,
            "collector_hostname": collector_host,
        }
        metrics["sampling_context"] = {
            "history": {
                "usable_for_trend_rules": history_usable,
                "sar_cpu_points": len(sar_cpu_history),
            },
            "realtime": {
                "sample_count": len(ora_rows),
                "oracle_stat_points": len(ora_rates),
            },
        }

        ts_first = ora_rows[0].get("timestamp", "") if ora_rows else ""
        ts_last = ora_rows[-1].get("timestamp", "") if ora_rows else ""
        metrics["time_evidence"] = {
            "first_sample_ts": ts_first,
            "last_sample_ts": ts_last,
        }
        return metrics
