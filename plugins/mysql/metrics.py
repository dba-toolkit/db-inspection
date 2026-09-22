"""MySQL metric provider.

This module converts a normalized ``PackageContext`` into facts consumed by
rules and presentation.  It knows MySQL counters and table names, but it does
not execute rules or build report sections.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from inspection_core import PackageContext, safe_float, safe_int
from inspection_core.charts import disk_throughput_value
from inspection_core.sampling import sar_history_quality
from inspection_core.statistics import summarize
from inspection_core.system_checks import is_persistent_fstype, parse_timedatectl


MYSQL_COUNTERS = (
    "Questions", "Queries", "Com_select", "Com_insert", "Com_update", "Com_delete",
    "Com_commit", "Com_rollback", "Bytes_received", "Bytes_sent", "Connections",
    "Aborted_connects", "Slow_queries", "Created_tmp_tables", "Created_tmp_disk_tables",
    "Opened_tables", "Innodb_buffer_pool_read_requests", "Innodb_buffer_pool_reads",
    "Innodb_rows_read", "Innodb_rows_inserted", "Innodb_rows_updated", "Innodb_rows_deleted",
    "Innodb_data_reads", "Innodb_data_writes", "Innodb_os_log_written", "Innodb_row_lock_waits",
    "Innodb_row_lock_time", "Handler_read_rnd_next", "Select_full_join", "Sort_merge_passes",
)


def row_number(row: dict[str, str], *keys: str) -> float | None:
    lowered = {str(key).lower(): value for key, value in row.items()}
    for key in keys:
        value = safe_float(row.get(key))
        if value is None:
            value = safe_float(lowered.get(key.lower()))
        if value is not None:
            return value
    return None


def time_evidence(ctx: PackageContext) -> dict[str, Any]:
    """时间同步证据：``snapshot.time_evidence`` 打底，缺失字段用 ``timedatectl`` 原文回填。

    采集侧在部分版本把 ``ntp_synchronized`` 落成了空串（解析缺失），而
    ``evidence/timedatectl.txt`` 一直在包里。只补空值、**不覆盖采集已有值**，
    解析算法取自公共层 —— 规则引擎和呈现层因此共用同一份证据，不会再出现
    "规则拿空值所以不判、呈现层拿空值所以判 risk" 这种两处打架。
    """
    raw = ctx.snapshot.get("time_evidence")
    evidence: dict[str, Any] = dict(raw) if isinstance(raw, dict) else {}
    path = ctx.root / "evidence" / "timedatectl.txt"
    if path.is_file():
        parsed = parse_timedatectl(path.read_text(encoding="utf-8", errors="replace"))
        for key, value in parsed.items():
            if not evidence.get(key):
                evidence[key] = value
    return evidence


# 采集端提供的备份**任务配置**证据（不含"最近一次是否成功"）。
BACKUP_EVIDENCE_FILES = ("backup_cron.txt", "backup_processes.txt", "backup_timers.txt")


def backup_task_visibility(ctx: PackageContext) -> dict[str, Any]:
    """备份任务配置的可见性 —— 关键是「证据为空」不能写成「没有备份任务」。

    实测：``.34`` / ``.125`` 的 ``evidence/backup_*.txt`` 是 **0 字节**（不是
    "读到空列表"），无法区分"确实没有备份任务"与"无权限读 crontab"；而 ``.33``
    的 crontab 有内容但**整段被注释** —— 那才是唯一可以判"本机无生效任务"的情况。
    四态因此是：

    - ``absent``   文件不存在          → 未采集，不判定
    - ``empty``    文件存在且全为 0 字节 → 证据为空，**不能判定**（≠ 不存在）
    - ``disabled`` 有内容但全部被注释   → 本机任务已停用（可判"无生效任务"）
    - ``active``   存在未注释的任务行   → 发现生效的备份任务配置

    规则层与呈现层共用这一份分类，避免出现"章节说无法判定、风险台账说 0 条"。
    """
    evidence_dir = ctx.root / "evidence"
    present: list[str] = []
    missing: list[str] = []
    empty: list[str] = []
    active_lines = 0
    commented_lines = 0
    for name in BACKUP_EVIDENCE_FILES:
        path = evidence_dir / name
        if not path.is_file():
            missing.append(name)
            continue
        present.append(name)
        text = path.read_text(encoding="utf-8", errors="replace")
        if not text.strip():
            empty.append(name)
            continue
        for line in text.splitlines():
            if not line.strip():
                continue
            if line.lstrip().startswith("#"):
                commented_lines += 1
            else:
                active_lines += 1
    if active_lines:
        state = "active"
    elif commented_lines:
        state = "disabled"
    elif empty:
        state = "empty"
    else:
        state = "absent"
    return {
        "state": state,
        "active_lines": active_lines,
        "commented_lines": commented_lines,
        "present": present,
        "missing": missing,
        "empty": empty,
    }


# ----------------------------------------------------------------------
# 复制状态口径
#
# 「上游指向自己」的复制状态行不是真实的主从关系。它在源端也可能存在：有人执行过
# CHANGE REPLICATION SOURCE TO，或通道被停用后残留配置，`SHOW REPLICA STATUS` 就会
# 留一行 `Source_UUID` 为空、`Source_Host` 指向本机的记录。采集端以「有行」当作
# replica 判据，于是这行会把复制源端误标成副本。拓扑层必须把它丢进
# `self_reference_edges`（不能画成自环边），规则层和报告层也必须用同一判据排除它，
# 否则同一份采集数据会同时产出「源节点 = 目标节点」的关系表和「复制状态异常」的
# 误报。判据只能在这里写一份 —— 三个消费方各自实现过一次就已经出现过口径漂移。
# ----------------------------------------------------------------------

_REPLICA_THREAD_UP = {"yes", "on", "1", "true"}


def local_host_names(ctx: PackageContext) -> set[str]:
    """本机可能出现的名字（IP / 主机名 / 连接主机），用于识别"上游指向自己"。"""
    identity = ctx.snapshot.get("instance_identity") or {}
    host = ctx.snapshot.get("host_identity") or {}
    names = [
        identity.get("instance_ip"),
        identity.get("mysql_hostname"),
        identity.get("connect_host"),
        identity.get("connect_ip"),
        host.get("hostname"),
        host.get("short_hostname"),
        host.get("primary_ip"),
    ]
    return {str(name).strip() for name in names if str(name or "").strip()}


def is_self_referencing_replica_row(row: dict[str, Any], local_names: set[str]) -> bool:
    """该复制状态行的上游声明是否指向本机（残留/未启动的通道）。

    兼容 MySQL 8.0.22 改名前后的两套列名（Source_* / Master_*）。
    """
    source_uuid = str(row.get("Source_UUID") or row.get("Master_UUID") or "").strip()
    if source_uuid:
        return False
    source_host = str(row.get("Source_Host") or row.get("Master_Host") or "").strip()
    return bool(source_host) and source_host in local_names


def split_self_referencing_replica_rows(
    rows: list[dict[str, Any]], local_names: set[str]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """拆成 ``(真实上游行, 指向自身的残留行)``，保持原有先后顺序。"""
    real: list[dict[str, Any]] = []
    residual: list[dict[str, Any]] = []
    for row in rows:
        target = residual if is_self_referencing_replica_row(row, local_names) else real
        target.append(row)
    return real, residual


def replica_threads_running(row: dict[str, Any]) -> tuple[bool | None, bool | None]:
    """``(IO 线程在跑, SQL 线程在跑)``，兼容 8.0.22 改名前后的两种列名。

    该列没采到（键不存在或值为空）时返回 None，与"明确是 No"区分开：前者是
    证据不足，后者才是复制线程停了。把两者混在一起会把"没采到"报成"复制异常"。
    """

    def flag(*keys: str) -> bool | None:
        for key in keys:
            if key not in row:
                continue
            value = str(row.get(key) or "").strip().lower()
            if value:
                return value in _REPLICA_THREAD_UP
        return None

    return (
        flag("Replica_IO_Running", "Slave_IO_Running"),
        flag("Replica_SQL_Running", "Slave_SQL_Running"),
    )


def filesystem_rows(path: Path) -> list[dict[str, Any]]:
    """Parse POSIX ``df -PT`` output stored in the filesystems artifact."""

    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    for line in lines[1:]:
        parts = re.split(r"\s+", line.strip(), maxsplit=6)
        if len(parts) != 7:
            continue
        filesystem, fs_type, blocks, used, available, capacity, mountpoint = parts
        rows.append({
            "filesystem": filesystem,
            "type": fs_type,
            "blocks_kb": safe_float(blocks),
            "used_kb": safe_float(used),
            "available_kb": safe_float(available),
            "usage_percent": safe_float(capacity.replace("%", "")),
            "mountpoint": mountpoint,
        })
    return rows


def counter_rates(rows: list[dict[str, str]], counters: Sequence[str]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for previous, current in zip(rows, rows[1:]):
        previous_ms = safe_float(previous.get("elapsed_ms"))
        current_ms = safe_float(current.get("elapsed_ms"))
        if previous_ms is None or current_ms is None or current_ms <= previous_ms:
            continue
        interval_seconds = (current_ms - previous_ms) / 1000.0
        point: dict[str, Any] = {
            "timestamp": current.get("timestamp"),
            "elapsed_ms": current_ms,
            "interval_seconds": interval_seconds,
        }
        for counter in counters:
            before = safe_float(previous.get(counter))
            after = safe_float(current.get(counter))
            if before is None or after is None or after < before:
                point[counter + "_per_sec"] = None
            else:
                point[counter + "_per_sec"] = (after - before) / interval_seconds
        result.append(point)
    return result


def delta_ratio(rows: list[dict[str, str]], numerator: str, denominator: str) -> float | None:
    if len(rows) < 2:
        return None
    first_numerator = safe_float(rows[0].get(numerator))
    last_numerator = safe_float(rows[-1].get(numerator))
    first_denominator = safe_float(rows[0].get(denominator))
    last_denominator = safe_float(rows[-1].get(denominator))
    if None in {first_numerator, last_numerator, first_denominator, last_denominator}:
        return None
    numerator_delta = last_numerator - first_numerator
    denominator_delta = last_denominator - first_denominator
    if denominator_delta <= 0 or numerator_delta < 0:
        return None
    return numerator_delta / denominator_delta


def collected_count(ctx: PackageContext, table_name: str) -> int | None:
    """Return zero only when the artifact was present and collected as empty."""

    if table_name not in ctx.tables:
        return None
    return len(ctx.tables[table_name])


def _object_label(row: dict[str, Any], *names: str) -> str:
    parts = [str(row.get(name) or "").strip() for name in names]
    return ".".join(part for part in parts if part)


def auto_increment_items(ctx: PackageContext) -> list[dict[str, Any]]:
    """自增容量候选清单（按 ``used_pct`` 倒序）。

    采集端给的是**按使用率倒序的前 N 条明细**，所以条数**不是**风险对象数：
    实测那 100 条里最高只有 22.79%，一个都没到阈值。判定留给规则层（阈值外置在
    规则包的 ``MYSQL.SCHEMA.AUTO_INCREMENT_CAPACITY.threshold``），这里只把可算的
    量算出来：使用率、剩余可用量、对象标识。
    """
    items: list[dict[str, Any]] = []
    for row in ctx.tables.get("auto_increment_usage") or []:
        auto = row_number(row, "AUTO_INCREMENT", "auto_increment")
        max_value = row_number(row, "max_value")
        remaining = None
        if auto is not None and max_value:
            remaining = max(max_value - auto, 0.0)
        items.append({
            "object": _object_label(row, "TABLE_SCHEMA", "TABLE_NAME", "COLUMN_NAME"),
            "column_type": row.get("COLUMN_TYPE") or row.get("column_type"),
            "used_pct": row_number(row, "used_pct"),
            "remaining": remaining,
        })
    items.sort(key=lambda item: (item["used_pct"] is None, -(item["used_pct"] or 0.0)))
    return items


def fragmentation_items(ctx: PackageContext) -> list[dict[str, Any]]:
    """碎片候选清单，**按空闲空间（data_free_mb）重排**。

    采集端按 ``fragmentation_pct`` 倒序，TOP 会被"分配 0.02 MB / 空闲 18 MB"的极小表
    占满（99.91%），真正值得回收的表（空闲 2.8 GB）反而落在后面。重排只发生在已采集
    的明细内，因此可能漏掉"空闲很大但碎片率低"的表 —— 这一点必须写进报告，不能让
    读者以为这就是全集。
    """
    items: list[dict[str, Any]] = []
    for row in ctx.tables.get("fragmentation_top") or []:
        items.append({
            "object": _object_label(row, "TABLE_SCHEMA", "TABLE_NAME"),
            "engine": row.get("ENGINE") or row.get("engine"),
            "allocated_mb": row_number(row, "allocated_mb"),
            "data_free_mb": row_number(row, "data_free_mb"),
            "fragmentation_pct": row_number(row, "fragmentation_pct"),
        })
    items.sort(key=lambda item: (item["data_free_mb"] is None, -(item["data_free_mb"] or 0.0)))
    return items


def non_innodb_items(ctx: PackageContext) -> list[dict[str, Any]]:
    """非 InnoDB 表清单（含引擎），供按引擎分档给结论。

    MEMORY 与 MyISAM 的后果完全不同：MEMORY 重启即丢数据且不支持事务，
    MyISAM 有索引与磁盘持久化、只是崩溃恢复弱。同一档文案会误导处置优先级。
    """
    items: list[dict[str, Any]] = []
    for row in ctx.tables.get("non_innodb_tables") or []:
        items.append({
            "object": _object_label(row, "TABLE_SCHEMA", "TABLE_NAME"),
            "engine": str(row.get("ENGINE") or row.get("engine") or "").upper(),
            "table_rows": row_number(row, "TABLE_ROWS", "table_rows"),
            "total_mb": row_number(row, "total_mb"),
        })
    return items


def non_innodb_engine_counts(items: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        engine = str(item.get("engine") or "UNKNOWN").upper()
        counts[engine] = counts.get(engine, 0) + 1
    return counts


class MySQLMetricProvider:
    """Build all rule-facing metrics for one MySQL package context."""

    def build_base(self, ctx: PackageContext) -> dict[str, Any]:
        cpu_rows = ctx.timeseries.get("system_cpu", [])
        memory_rows = ctx.timeseries.get("system_memory", [])
        disk_rows = ctx.timeseries.get("system_disk", [])
        network_rows = ctx.timeseries.get("system_network", [])
        mysql_rows = ctx.timeseries.get("mysql_status", [])

        rates = counter_rates(mysql_rows, MYSQL_COUNTERS)
        workload_rates = rates[1:] if len(rates) > 2 else rates
        qps = [safe_float(row.get("Questions_per_sec")) for row in workload_rates]
        tps = [
            (safe_float(row.get("Com_commit_per_sec")) or 0.0)
            + (safe_float(row.get("Com_rollback_per_sec")) or 0.0)
            for row in workload_rates
        ]

        per_device: dict[str, dict[str, Any]] = {}
        for row in disk_rows:
            device = row.get("device", "unknown")
            bucket = per_device.setdefault(
                device,
                {"util": [], "read_await": [], "write_await": [], "read_bps": [], "write_bps": [], "queue": []},
            )
            for source_key, target_key in (
                ("util_pct", "util"), ("read_await_ms", "read_await"),
                ("write_await_ms", "write_await"), ("read_bytes_per_sec", "read_bps"),
                ("write_bytes_per_sec", "write_bps"), ("avg_queue_size", "queue"),
            ):
                value = safe_float(row.get(source_key))
                if value is not None:
                    bucket[target_key].append(value)
        disk_summary = {
            device: {key: summarize(values) for key, values in data.items()}
            for device, data in per_device.items()
        }

        interfaces: dict[str, dict[str, list[float]]] = {}
        for row in network_rows:
            interface = row.get("interface", "unknown")
            bucket = interfaces.setdefault(interface, {"rx_bps": [], "tx_bps": []})
            received = safe_float(row.get("rx_bytes_per_sec"))
            transmitted = safe_float(row.get("tx_bytes_per_sec"))
            if received is not None:
                bucket["rx_bps"].append(received)
            if transmitted is not None:
                bucket["tx_bps"].append(transmitted)

        temporary_disk_ratio = delta_ratio(mysql_rows, "Created_tmp_disk_tables", "Created_tmp_tables")
        buffer_pool_miss = delta_ratio(mysql_rows, "Innodb_buffer_pool_reads", "Innodb_buffer_pool_read_requests")
        table_cache_misses = None
        if len(mysql_rows) >= 2:
            miss_start = safe_float(mysql_rows[0].get("Table_open_cache_misses"))
            miss_end = safe_float(mysql_rows[-1].get("Table_open_cache_misses"))
            hit_start = safe_float(mysql_rows[0].get("Table_open_cache_hits"))
            hit_end = safe_float(mysql_rows[-1].get("Table_open_cache_hits"))
            if None not in {miss_start, miss_end, hit_start, hit_end}:
                miss_delta = miss_end - miss_start
                hit_delta = hit_end - hit_start
                if miss_delta >= 0 and hit_delta >= 0 and miss_delta + hit_delta > 0:
                    table_cache_misses = miss_delta / (miss_delta + hit_delta)

        total_memory = safe_float(ctx.snapshot.get("host_identity", {}).get("memory_total_bytes"))
        buffer_pool = safe_float(ctx.variables.get("innodb_buffer_pool_size"))
        buffer_pool_ratio = buffer_pool / total_memory if buffer_pool and total_memory else None

        sar_cpu_busy: list[float] = []
        sar_iowait: list[float] = []
        for row in ctx.history.get("sar_cpu", []):
            idle = safe_float(row.get("%idle"))
            iowait = safe_float(row.get("%iowait"))
            if idle is not None:
                sar_cpu_busy.append(100.0 - idle)
            if iowait is not None:
                sar_iowait.append(iowait)
        sar_memory_used = [
            value for row in ctx.history.get("sar_memory", [])
            if (value := safe_float(row.get("%memused"))) is not None
        ]
        sar_swap_used = [
            value for row in ctx.history.get("sar_swap", [])
            if (value := safe_float(row.get("%swpused"))) is not None
        ]
        sar_disk_by_device: dict[str, dict[str, list[float]]] = {}
        for row in ctx.history.get("sar_disk", []):
            device = row.get("DEV", "unknown")
            bucket = sar_disk_by_device.setdefault(
                device, {"util": [], "await": [], "read_kbps": [], "write_kbps": []}
            )
            for key, target in (("%util", "util"), ("await", "await")):
                value = safe_float(row.get(key))
                if value is not None:
                    bucket[target].append(value)
            # 吞吐列有两种拼法（rkB/s 与扇区/秒），归一在公共层做，
            # 这里只负责取 KiB/s 的数（见 history.DISK_THROUGHPUT_ALIASES）。
            for key, target in (("rkB/s", "read_kbps"), ("wkB/s", "write_kbps")):
                value = disk_throughput_value(row, key)
                if value is not None:
                    bucket[target].append(value)

        return {
            "system_realtime": {
                "cpu_busy_percent": summarize(value for row in cpu_rows if (value := safe_float(row.get("busy_pct"))) is not None),
                "cpu_iowait_percent": summarize(value for row in cpu_rows if (value := safe_float(row.get("iowait_pct"))) is not None),
                "memory_used_percent": summarize(value for row in memory_rows if (value := safe_float(row.get("mem_used_pct"))) is not None),
                "memory_available_bytes": summarize(value for row in memory_rows if (value := safe_float(row.get("mem_available_bytes"))) is not None),
                "swap_used_bytes": summarize(value for row in memory_rows if (value := safe_float(row.get("swap_used_bytes"))) is not None),
                "disk_devices": disk_summary,
                "network_interfaces": {
                    interface: {key: summarize(values) for key, values in data.items()}
                    for interface, data in interfaces.items()
                },
            },
            "mysql_realtime": {
                "sample_points": len(mysql_rows),
                "rate_points": len(rates),
                "qps": summarize(value for value in qps if value is not None),
                "tps": summarize(tps),
                "threads_connected": summarize(value for row in mysql_rows if (value := safe_float(row.get("Threads_connected"))) is not None),
                "threads_running": summarize(value for row in mysql_rows if (value := safe_float(row.get("Threads_running"))) is not None),
                "tmp_disk_ratio": None if temporary_disk_ratio is None else round(temporary_disk_ratio, 6),
                "buffer_pool_read_miss_ratio": None if buffer_pool_miss is None else round(buffer_pool_miss, 8),
                "table_open_cache_miss_ratio": None if table_cache_misses is None else round(table_cache_misses, 8),
                "workload_statistics_excluded_initial_intervals": 1 if len(rates) > 2 else 0,
                "buffer_pool_to_memory_ratio": None if buffer_pool_ratio is None else round(buffer_pool_ratio, 6),
                "derived_rate_series": rates,
            },
            "system_history": {
                "coverage": ctx.snapshot.get("sampling", {}).get("sar_history", {}),
                "cpu_busy_percent": summarize(sar_cpu_busy),
                "cpu_iowait_percent": summarize(sar_iowait),
                "memory_used_percent": summarize(sar_memory_used),
                "swap_used_percent": summarize(sar_swap_used),
                "disk_devices": {
                    device: {key: summarize(values) for key, values in data.items()}
                    for device, data in sar_disk_by_device.items()
                },
            },
        }

    def build(self, ctx: PackageContext) -> dict[str, Any]:
        metrics = self.build_base(ctx)
        sampling = ctx.snapshot.get("sampling", {})
        local = bool(ctx.snapshot.get("host_identity", {}).get("database_target_is_local", False))
        elapsed_ms = safe_float(sampling.get("actual_elapsed_ms"))
        rate_points = safe_int(metrics["mysql_realtime"].get("rate_points")) or 0
        metrics["scope"] = {
            "database_target_is_local": local,
            "system_metrics_apply_to_database_host": local,
            "note": "数据库目标与采集主机一致" if local else "数据库为远程目标，主机指标不得用于数据库健康判断",
        }
        metrics["sampling_context"] = {
            "realtime_window_seconds": round(elapsed_ms / 1000.0, 2) if elapsed_ms is not None else None,
            "mysql_sample_points": safe_int(sampling.get("actual_mysql_points")) or 0,
            "rate_points": rate_points,
            "short_window": (elapsed_ms or 0) < 300_000,
            "percentile_reliable": rate_points >= 20,
            "preferred_realtime_statistics": ["average", "max"] if rate_points < 20 else ["average", "p95", "max"],
            "history": sar_history_quality(ctx),
        }

        all_filesystems = filesystem_rows(ctx.root / "tables/filesystems.tsv")
        # Capacity is judged on the same set of mounts the report shows.  Kernel
        # pseudo filesystems (proc/tmpfs/hugetlbfs), read-only media (iso9660) and
        # container overlays are dropped first: an optical drive is permanently
        # 100% full with 0 bytes free, so counting it would raise a capacity risk
        # the customer can neither confirm nor fix.  The predicate lives in the
        # shared layer so all three plugins filter on the same rule.
        filesystems = [row for row in all_filesystems if is_persistent_fstype(row.get("type"))]
        excluded_filesystems = [row for row in all_filesystems if not is_persistent_fstype(row.get("type"))]
        database_rows = ctx.tables.get("database_sizes", [])
        database_bytes = 0.0
        for row in database_rows:
            value = row_number(row, "total_bytes", "size_bytes", "database_size_bytes", "total_mb", "size_mb")
            if value is not None:
                if any(str(key).lower() in {"total_mb", "size_mb"} for key in row):
                    value *= 1024 ** 2
                database_bytes += value
        object_rows = ctx.tables.get("object_counts", [])
        table_count = sum(int(row_number(row, "table_count", "tables", "base_table_count") or 0) for row in object_rows)
        no_primary_key_rows = ctx.tables.get("no_primary_key_summary", [])
        no_primary_key_count = int(row_number(no_primary_key_rows[0], "table_count") or 0) if no_primary_key_rows else None
        long_transactions = ctx.tables.get("long_transactions", [])
        max_long_transaction = max((row_number(row, "duration_seconds") or 0 for row in long_transactions), default=0)
        error_rows = ctx.tables.get("error_log_summary", [])
        error_count = sum(
            int(row_number(row, "occurrence_count") or 0)
            for row in error_rows
            if str(row.get("PRIO", "")).lower() in {"error", "critical", "system"}
        )
        metrics["capacity"] = {
            "database_size_bytes": round(database_bytes) if database_rows else None,
            "database_count": len(database_rows) if database_rows else None,
            "table_count": table_count if object_rows else None,
            "filesystems": filesystems,
            "excluded_filesystems": excluded_filesystems,
            "max_filesystem_usage_percent": max(
                (row["usage_percent"] for row in filesystems if row["usage_percent"] is not None),
                default=None,
            ),
        }
        auto_increment = auto_increment_items(ctx)
        fragmentation = fragmentation_items(ctx)
        non_innodb = non_innodb_items(ctx)
        # 自增 / 碎片是**明细清单**（采集端按某列倒序取前 N 条），不是"候选项集合"。
        # 旧口径把清单条数直接当风险对象数（实测 100 条里最高使用率仅 22.79%），
        # 现在只暴露明细与可算的极值，判定交给规则层按外部化阈值做。
        metrics["schema"] = {
            "tables_without_primary_key": no_primary_key_count,
            "auto_increment_items": auto_increment,
            "auto_increment_collected_count": collected_count(ctx, "auto_increment_usage"),
            "auto_increment_max_used_pct": max(
                (item["used_pct"] for item in auto_increment if item["used_pct"] is not None),
                default=None,
            ),
            "fragmentation_items": fragmentation,
            "fragmentation_collected_count": collected_count(ctx, "fragmentation_top"),
            "fragmentation_max_data_free_mb": max(
                (item["data_free_mb"] for item in fragmentation if item["data_free_mb"] is not None),
                default=None,
            ),
            "non_innodb_items": non_innodb,
            "non_innodb_engines": non_innodb_engine_counts(non_innodb),
            "non_innodb_table_count": collected_count(ctx, "non_innodb_tables"),
            "redundant_index_count": collected_count(ctx, "redundant_indexes"),
            "unused_index_candidate_count": collected_count(ctx, "unused_indexes"),
        }
        metrics["activity"] = {
            "long_transaction_count": collected_count(ctx, "long_transactions"),
            "max_long_transaction_seconds": max_long_transaction if long_transactions else None,
            "data_lock_wait_count": collected_count(ctx, "data_lock_waits"),
            "pending_metadata_lock_count": collected_count(ctx, "metadata_locks_pending"),
            "error_log_error_occurrences": error_count if "error_log_summary" in ctx.tables else None,
        }
        if not local:
            metrics["mysql_realtime"]["buffer_pool_to_memory_ratio"] = None
            metrics["mysql_realtime"]["buffer_pool_to_memory_ratio_reason"] = "remote_database_target"
        return metrics


# ---------------------------------------------------------------------------
# 共享取值入口：日志轮转 / binlog 容量 / uptime
#
# 这三份数据同时被规则层（判据）和呈现层（表格结论）消费。原先呈现层自己
# 在 build_inspection_model 里就地算一遍文件数与总容量，规则层另写一份的话
# 就会出现"表格说 74 个文件、规则说 20 个"这种两处口径 —— 所以取值只留这里。
# ---------------------------------------------------------------------------


def mysql_uptime_seconds(ctx: PackageContext) -> float | None:
    """``global_status.tsv`` 里的 ``Uptime``（秒）。

    用于把日志体积折算成"日增速率"：绝对值 132 GiB 说明大，除以 uptime 才能说明
    "每天涨多少"，后者才是运维判断该不该立刻加轮转的依据。采集不到返回 None。
    """
    for row in ctx.tables.get("global_status", []) or []:
        name = str(row.get("VARIABLE_NAME") or row.get("Variable_name") or "").strip()
        if name.lower() == "uptime":
            return safe_float(row.get("VARIABLE_VALUE") or row.get("Value"))
    return None


def log_file_entries(ctx: PackageContext) -> list[dict[str, Any]]:
    """已存在且大小可读的日志文件：``log_type`` / ``path`` / ``size_bytes``。

    只保留 ``exists=1`` 且 ``size_bytes`` 能解析成数字的行 —— 未采集 ≠ 0，
    缺大小的行不能当"0 字节"参与阈值判断。
    """
    entries: list[dict[str, Any]] = []
    for row in ctx.tables.get("log_files", []) or []:
        if str(row.get("exists") or "").strip().lower() not in {"1", "true", "yes"}:
            continue
        size = safe_float(row.get("size_bytes"))
        if size is None:
            continue
        entries.append(
            {
                "log_type": str(row.get("log_type") or "").strip() or "unknown",
                "path": str(row.get("path") or "").strip(),
                "size_bytes": size,
            }
        )
    return entries


def binlog_totals(ctx: PackageContext) -> dict[str, Any]:
    """Binlog 文件数 / 合计字节 / 未加密文件数（口径唯一来源）。"""
    rows = ctx.tables.get("binary_logs", []) or []
    total = 0.0
    unencrypted = 0
    for row in rows:
        total += row_number(row, "File_size") or 0.0
        if str(row.get("Encrypted") or "").strip().lower() in {"no", "false", "0", "off"}:
            unencrypted += 1
    return {"count": len(rows), "bytes": total, "unencrypted": unencrypted}


def runtime_variables(ctx: PackageContext) -> dict[str, str]:
    """``global_variables.tsv`` 的运行值（键统一小写）。"""
    result: dict[str, str] = {}
    for row in ctx.tables.get("global_variables", []) or []:
        name = str(row.get("VARIABLE_NAME") or row.get("Variable_name") or "").strip()
        if name:
            result[name.lower()] = str(row.get("VARIABLE_VALUE") or row.get("Value") or "").strip()
    return result


def mycnf_entries(ctx: PackageContext) -> list[dict[str, str]]:
    """``mycnf_allowlist.tsv`` 的配置项（parameter / configured_value / section / source_file）。"""
    entries: list[dict[str, str]] = []
    for row in ctx.tables.get("mycnf_allowlist", []) or []:
        parameter = str(row.get("parameter") or "").strip()
        if not parameter:
            continue
        entries.append(
            {
                "parameter": parameter,
                "configured_value": str(row.get("configured_value") or "").strip(),
                "section": str(row.get("section") or "").strip(),
                "source_file": str(row.get("source_file") or "").strip(),
            }
        )
    return entries


_UNIT_FACTORS = {"k": 1024, "m": 1024 ** 2, "g": 1024 ** 3, "t": 1024 ** 4, "p": 1024 ** 5}
_SIZE_SETTING = re.compile(r"^([0-9]+(?:\.[0-9]+)?)\s*([kmgtp])i?b?$", re.IGNORECASE)

# MySQL 会按 OS 限制自动下调这两个值：``open_files_limit`` 受 systemd / ulimit 约束，
# ``table_open_cache`` 再受 ``open_files_limit`` 约束。运行值低于配置文件属正常调整
# （不是"配置没生效"），报出来只会制造噪音，因此不参与"配置 vs 运行"比对。
RUNTIME_DRIFT_AUTO_ADJUSTED = frozenset({"open_files_limit", "table_open_cache"})

# MySQL 8.0 的已弃用别名：配置文件的 ``expire_logs_days`` 实际映射到运行值
# ``binlog_expire_logs_seconds``（天 × 86400），而旧的 ``expire_logs_days`` 变量只会读到 0。
# 直接按名字比会把"配置 7 天、实际 5 天"错报成"配置 7、运行 0"。
RUNTIME_DRIFT_ALIASES: dict[str, tuple[str, float]] = {
    "expire_logs_days": ("binlog_expire_logs_seconds", 86400.0),
}


def _setting_value(text: Any) -> tuple[str, Any] | None:
    """把一侧取值归成 ``(域, 归一值)``。

    域决定"这两个值能不能直接比"：``bool``（ON/OFF/TRUE/FALSE）、``size``（200M/1G）、
    ``number``、``text``（路径等）。``log_bin`` 在配置文件里是路径、在运行值里是 ON，
    跨域比较没有意义，返回 None 让调用方跳过，而不是报一条假漂移。
    """
    raw = str(text or "").strip().strip("'\"").strip()
    if not raw:
        return None
    lowered = raw.lower()
    if lowered in {"on", "true"}:
        return ("bool", True)
    if lowered in {"off", "false"}:
        return ("bool", False)
    matched = _SIZE_SETTING.match(lowered)
    if matched:
        return ("size", float(matched.group(1)) * _UNIT_FACTORS[matched.group(2).lower()])
    try:
        return ("number", float(raw))
    except ValueError:
        # 路径统一去掉尾斜杠：``/usr/local/mysql`` 与 ``/usr/local/mysql/`` 是同一个目录。
        return ("text", re.sub(r"/+$", "", lowered))


def _settings_match(left: Any, right: Any) -> bool | None:
    """两侧取值是否等价；None = 不可比（域不同，不能判成不一致）。"""
    a = _setting_value(left)
    b = _setting_value(right)
    if a is None or b is None:
        return None

    def as_bool(pair: tuple[str, Any]) -> bool | None:
        kind, value = pair
        if kind == "bool":
            return value
        if kind == "number":
            return value != 0
        return None

    # ON/OFF 与 1/0 是同一件事的两种写法。
    if a[0] == "bool" or b[0] == "bool":
        left_bool, right_bool = as_bool(a), as_bool(b)
        if left_bool is None or right_bool is None:
            return None
        return left_bool == right_bool

    def as_size(pair: tuple[str, Any]) -> float | None:
        kind, value = pair
        if kind == "size":
            return value
        if kind == "number":
            return value  # 无单位的一侧按字节理解（200M vs 209715200）
        return None

    # 只要有一侧带单位（200M / 1G），两边都按字节比。
    if a[0] == "size" or b[0] == "size":
        left_size, right_size = as_size(a), as_size(b)
        if left_size is None or right_size is None:
            return None
        return abs(left_size - right_size) < 0.5

    if a[0] == "number" and b[0] == "number":
        return abs(a[1] - b[1]) < 1e-9
    if a[0] == "text" and b[0] == "text":
        return a[1] == b[1]
    return None


def config_runtime_drift(ctx: PackageContext) -> list[dict[str, Any]]:
    """配置文件值 vs 运行值的不一致项（已过滤单位/写法差异带来的假阳性）。

    两类都算不一致，且要分开说：
    * **单一配置值 ≠ 运行值** —— 改了配置没重启 / 没加载，下次重启行为会翻转；
    * **同一参数在配置文件里被写了多个不同的值** —— 生效值取决于加载顺序，
      本身就是隐患，即使其中一个恰好等于运行值。

    归一到同一"域"后仍然不同的才算；域不同（如 ``log_bin`` 路径 vs ``ON``）或
    运行值取不到时直接跳过，不制造假漂移。
    """
    runtime = runtime_variables(ctx)
    grouped: dict[str, list[dict[str, str]]] = {}
    for entry in mycnf_entries(ctx):
        grouped.setdefault(entry["parameter"].lower(), []).append(entry)

    result: list[dict[str, Any]] = []
    for parameter, entries in grouped.items():
        if parameter in RUNTIME_DRIFT_AUTO_ADJUSTED:
            continue
        running = runtime.get(parameter)
        divisor = 1.0
        alias = RUNTIME_DRIFT_ALIASES.get(parameter)
        if alias is not None:
            target, divisor = alias
            raw_alias = safe_float(runtime.get(target))
            if raw_alias is None:
                continue
            running = f"{raw_alias / divisor:g}"
        if running is None:
            continue

        configured_raw = [item["configured_value"] for item in entries]
        mismatch = _settings_match(configured_raw[0], running) is False
        for value in configured_raw[1:]:
            matched = _settings_match(value, running)
            if matched is False:
                mismatch = True
                break
        distinct: list[str] = []
        for value in configured_raw:
            if not any(_settings_match(value, seen) for seen in distinct):
                distinct.append(value)
        conflict = len(distinct) > 1
        if not (conflict or mismatch):
            continue
        result.append(
            {
                "parameter": parameter,
                "runtime": running,
                "configured_raw": configured_raw,
                "configured_distinct": distinct,
                "sources": sorted({item["source_file"] for item in entries if item["source_file"]}),
                "sections": sorted({item["section"] for item in entries if item["section"]}),
                "conflict_in_file": conflict,
                "mismatch_runtime": mismatch,
                "alias_of": alias[0] if alias else None,
            }
        )
    return result


def global_status_value(ctx: PackageContext, name: str) -> str | None:
    """``global_status.tsv`` 单个状态变量的原始值；取不到返回 None。

    与 ``mysql_uptime_seconds`` 的专用取值不同，这是通用入口 —— 表缓存（A9）与连接
    失败率（A10）各要读好几个 status 键，重复遍历不如一个 helper 统一口径。
    """
    for row in ctx.tables.get("global_status", []) or []:
        key = str(row.get("VARIABLE_NAME") or row.get("Variable_name") or "").strip()
        if key.lower() == name.lower():
            return str(row.get("VARIABLE_VALUE") or row.get("Value") or "").strip()
    return None


def large_table_items(ctx: PackageContext, min_total_mb: float) -> list[dict[str, Any]]:
    """``large_tables_top.tsv`` 里 ``total_mb`` 超阈值的表（A12）。

    只做阈值筛选，顺序沿用采集端（已按 total_mb 倒序）；``index_mb`` 为 0 的表单独
    带出，供规则层追加"全表扫描风险"提示。
    """
    items: list[dict[str, Any]] = []
    for row in ctx.tables.get("large_tables_top", []) or []:
        total = safe_float(row.get("total_mb"))
        if total is None or total < min_total_mb:
            continue
        items.append(
            {
                "schema": str(row.get("TABLE_SCHEMA") or "").strip(),
                "table": str(row.get("TABLE_NAME") or "").strip(),
                "engine": str(row.get("ENGINE") or "").strip(),
                "rows": str(row.get("TABLE_ROWS") or "").strip(),
                "total_mb": total,
                "index_mb": safe_float(row.get("index_mb")),
            }
        )
    return items


# 疑似测试/备份/复制残留的命名模式（A13）。刻意不收 ``_duplication``：实测该子串
# 只会命中 ``*_duplicationcheck`` 这类"查重"业务表（如 warddrugapply_duplicationcheck、
# feechargerecord_duplicationcheck），不是复制残留，收进来全是误报。
_LEFTOVER_PATTERNS: tuple[tuple[Any, str], ...] = (
    (re.compile(r"^test_"), "test_ 前缀"),
    (re.compile(r"_bak\d+"), "_bak 备份表"),
    (re.compile(r"_\d{4}$"), "_mmdd 日期后缀"),
    (re.compile(r"_copy\d+$"), "_copyN 复制表"),
)


def leftover_table_items(ctx: PackageContext) -> tuple[list[dict[str, Any]], int]:
    """按命名模式识别疑似残留表（A13）。

    扫 ``ctx.tables`` 里所有带 ``TABLE_NAME`` / ``table_name`` 列的采集表，跨表去重后
    返回 ``(items, scanned)``。``items`` 每项带 ``schema`` / ``table`` / ``source``（来源
    采集表）/ ``pattern``（命中的模式）；``scanned`` 是扫到的含表名列的采集表数量，
    用于区分"没有残留"与"没有采集到任何表信息"（未采集 ≠ 0）。
    """
    found: dict[tuple[str, str], dict[str, Any]] = {}
    scanned = 0
    for source, rows in (ctx.tables or {}).items():
        if not rows or not isinstance(rows, list):
            continue
        keys = set(rows[0].keys())
        scol = "TABLE_SCHEMA" if "TABLE_SCHEMA" in keys else ("table_schema" if "table_schema" in keys else None)
        tcol = "TABLE_NAME" if "TABLE_NAME" in keys else ("table_name" if "table_name" in keys else None)
        if not tcol:
            continue
        scanned += 1
        for row in rows:
            name = str(row.get(tcol) or "").strip()
            if not name:
                continue
            for pattern, label in _LEFTOVER_PATTERNS:
                if pattern.search(name):
                    schema = str(row.get(scol) or "").strip() if scol else ""
                    key = (schema, name)
                    if key not in found:
                        found[key] = {
                            "schema": schema,
                            "table": name,
                            "source": source,
                            "pattern": label,
                        }
                    break
    items = sorted(found.values(), key=lambda d: (d["pattern"], d["schema"], d["table"]))
    return items, scanned

