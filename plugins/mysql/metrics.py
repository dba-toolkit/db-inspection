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
from inspection_core.system_checks import is_persistent_fstype


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
        metrics["schema"] = {
            "tables_without_primary_key": no_primary_key_count,
            "auto_increment_warning_count": collected_count(ctx, "auto_increment_usage"),
            "fragmentation_candidate_count": collected_count(ctx, "fragmentation_top"),
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
