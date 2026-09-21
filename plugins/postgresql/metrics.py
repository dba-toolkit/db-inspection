"""PostgreSQL metric derivation provider.

Consumes collected facts and produces deterministic metrics.  It does not run
rules, build report prose, render charts or write output files.
"""

from __future__ import annotations

from typing import Any, Callable

from inspection_core.charts import busiest_history_device, busy_percent, series_values
from inspection_core.models import PackageContext
from inspection_core.sampling import effective_coverage_hours
from inspection_core.values import safe_float, safe_int
from .parsers import _parse_df_pt


class PostgreSQLMetricProvider:
    def __init__(self, quality_provider: Callable[[PackageContext], dict[str, Any]]) -> None:
        self._quality_provider = quality_provider

    def collection_quality(self, context: PackageContext) -> dict[str, Any]:
        return self._quality_provider(context)

    def derive_metrics(self, ctx: PackageContext) -> dict[str, Any]:
        """Derive structured metrics from PG collector data."""
        tables = ctx.tables
        settings = ctx.settings
        ts = ctx.timeseries

        # --- connection metrics ---
        conns = tables.get("connections", [])
        max_conn = safe_int(settings.get("max_connections"))
        cur_conn = safe_int(conns[0].get("current_connections")) if conns else None
        conn_usage = round(cur_conn / max_conn * 100, 1) if cur_conn and max_conn and max_conn > 0 else None

        # --- cache hit ratio ---
        dbstats = tables.get("database_stats", [])
        hits = sum(safe_float(r.get("blks_hit")) or 0 for r in dbstats)
        reads = sum(safe_float(r.get("blks_read")) or 0 for r in dbstats)
        cache_hit = round(hits / (hits + reads) * 100, 2) if hits + reads > 0 else None

        # --- dead tuple metrics ---
        thealth = tables.get("table_health", [])
        dead_tup_total = sum(safe_int(r.get("n_dead_tup")) or 0 for r in thealth)
        max_dead = max((safe_float(r.get("dead_tuple_ratio")) or 0 for r in thealth), default=0)
        stale_tables = sum(
            1 for r in thealth
            if not r.get("last_autovacuum") and not r.get("last_vacuum")
            and (
                (safe_int(r.get("n_live_tup")) or 0) + (safe_int(r.get("n_dead_tup")) or 0) >= 10000
                or (safe_int(r.get("total_bytes")) or 0) >= 100 * 1024 * 1024
                or (safe_int(r.get("n_mod_since_analyze")) or 0) >= 10000
            )
        )

        # --- replication ---
        slots = tables.get("replication_slots", [])
        active_slots = sum(1 for r in slots if r.get("active") == "t")
        rep_status = tables.get("stat_replication", [])
        max_lag_bytes = max((safe_float(r.get("replay_lag_bytes")) or 0 for r in rep_status), default=0)

        # --- WAL ---
        archiver = tables.get("archiver", [])
        arch_failed = safe_int(archiver[0].get("failed_count")) if archiver else None

        # --- database age ---
        db_ages = tables.get("database_ages", [])
        max_xid_age = max((safe_int(r.get("xid_age")) or 0 for r in db_ages), default=0)

        # --- bloat ---
        bloat = tables.get("bloat_estimation", [])
        # n_dead_tup is an estimate, not proof of physical bloat. Only count
        # material candidates; keep all rows available as supporting evidence.
        bloat_candidates = [
            r for r in bloat
            if (safe_float(r.get("dead_pct")) or 0) >= 10
            and (safe_float(r.get("estimated_dead_bytes")) or 0) >= 100 * 1024 * 1024
        ]
        bloat_tables = len(bloat_candidates)
        max_dead_bytes = max((safe_float(r.get("estimated_dead_bytes")) or 0 for r in bloat), default=0)

        # --- indexes ---
        unused_idx = tables.get("unused_indexes", [])
        unused_idx_count = len(unused_idx)
        dup_idx = tables.get("duplicate_indexes", [])
        dup_idx_count = len(dup_idx)

        # --- long transactions ---
        long_xacts = tables.get("long_transactions", [])
        long_xact_count = len(long_xacts)
        max_xact_sec = max((safe_int(r.get("duration_seconds")) or 0 for r in long_xacts), default=0)

        # --- lock waits ---
        lock_waits = tables.get("lock_waits", [])

        # --- partitions ---
        partitions = tables.get("partition_summary", [])
        max_partitions = max((safe_int(r.get("partition_count")) or 0 for r in partitions), default=0)

        # --- extensions / pg_stat_statements ---
        exts = tables.get("extensions", [])
        ext_names = {r.get("extension_name", "") for r in exts}
        has_pgss = "pg_stat_statements" in ext_names

        # --- OS metrics from timeseries ---
        cpu_busy_max: float | None = None
        iowait_max: float | None = None
        mem_used_max: float | None = None
        disk_util_max: float | None = None
        system_cpu = ts.get("system_cpu", [])
        if system_cpu:
            busy = [safe_float(r.get("busy_pct")) for r in system_cpu if r.get("busy_pct")]
            cpu_busy_max = max((v for v in busy if v is not None), default=None)
            iowait = [safe_float(r.get("iowait_pct")) for r in system_cpu if r.get("iowait_pct")]
            iowait_max = max((v for v in iowait if v is not None), default=None)

        system_mem = ts.get("system_memory", [])
        if system_mem:
            mem_pct = [safe_float(r.get("mem_used_pct")) for r in system_mem if r.get("mem_used_pct")]
            mem_used_max = max((v for v in mem_pct if v is not None), default=None)

        system_disk = ts.get("system_disk", [])
        if system_disk:
            disk_vals = [safe_float(r.get("util_pct")) for r in system_disk if r.get("util_pct")]
            disk_util_max = max((v for v in disk_vals if v is not None), default=None)

        # Prefer the widest available observation window: merge SAR history peaks
        # with the short realtime sample instead of evaluating only ~30 seconds.
        # All four host series come from the shared SAR normaliser, so the column
        # aliases (%usr/%sys vs %user/%system) and the "whole host" row marker are
        # decided once rather than re-derived here.
        sar_cpu_rows = ctx.history.get("sar_cpu", [])
        if sar_cpu_rows:
            hist_busy = max(
                (value for value in busy_percent(sar_cpu_rows) if value is not None),
                default=None,
            )
            if hist_busy is not None:
                cpu_busy_max = max(v for v in (cpu_busy_max, hist_busy) if v is not None)
            hist_iowait = max(
                (value for value in series_values(sar_cpu_rows, "%iowait") if value is not None),
                default=None,
            )
            if hist_iowait is not None:
                iowait_max = max(v for v in (iowait_max, hist_iowait) if v is not None)

        sar_mem_rows = ctx.history.get("sar_memory", [])
        if sar_mem_rows:
            hist_mem = max(
                (value for value in series_values(sar_mem_rows, "%memused") if value is not None),
                default=None,
            )
            if hist_mem is not None:
                mem_used_max = max(v for v in (mem_used_max, hist_mem) if v is not None)

        sar_disk_rows = ctx.history.get("sar_disk", [])
        if sar_disk_rows:
            busiest_device = busiest_history_device(sar_disk_rows)
            busiest_rows = [row for row in sar_disk_rows if row.get("DEV") == busiest_device]
            hist_disk = max(
                (value for value in series_values(busiest_rows, "%util") if value is not None),
                default=None,
            )
            if hist_disk is not None:
                disk_util_max = max(v for v in (disk_util_max, hist_disk) if v is not None)

        sar_coverage_hours = effective_coverage_hours(sar_cpu_rows)

        # --- PG timeseries ---
        pg_act = ts.get("pg_activity", [])
        max_active = max((safe_float(r.get("active_sessions")) or 0 for r in pg_act), default=0) if pg_act else None

        # --- backup ---
        has_backup_tool = any(
            (ctx.root / "evidence" / f).exists()
            and (ctx.root / "evidence" / f).stat().st_size > 0
            for f in ["pgbackrest_info.json", "barman_check.txt"]
        )
        backup_cron = ctx.root / "evidence" / "backup_cron.txt"
        has_backup_cron = backup_cron.exists() and backup_cron.stat().st_size > 0
        # Local process/cron inspection cannot prove that an external backup
        # platform is absent. Only mark the check conclusive when evidence exists.
        backup_assessment_complete = has_backup_tool or has_backup_cron

        # --- security ---
        auth_methods = set()
        for r in tables.get("hba_rules", []):
            auth_methods.add(r.get("auth_method", ""))
        has_trust = "trust" in auth_methods or "password" in auth_methods
        password_enc = settings.get("password_encryption", "")
        weak_pw = password_enc in ("md5",)

        # --- key settings ---
        shared_buffers = settings.get("shared_buffers", "")
        effective_cache = settings.get("effective_cache_size", "")
        work_mem = settings.get("work_mem", "")
        maintenance_work_mem = settings.get("maintenance_work_mem", "")
        wal_level = settings.get("wal_level", "")
        archive_mode = settings.get("archive_mode", "")
        autovacuum = settings.get("autovacuum", "on")

        # --- additional metrics for expanded rules ---
        no_pk = tables.get("no_primary_key", [])
        no_pk_count = len(no_pk)

        idx_table = tables.get("indexes", [])
        invalid_idx = sum(1 for r in idx_table if r.get("indisvalid") == "f")

        ssl_on = settings.get("ssl", "off") == "on"
        idle_timeout = settings.get("idle_in_transaction_session_timeout", "0")
        has_idle_timeout = idle_timeout != "0"
        stmt_timeout = settings.get("statement_timeout", "0")
        has_stmt_timeout = stmt_timeout != "0"
        log_min_duration = settings.get("log_min_duration_statement", "-1")
        has_slow_query_log = log_min_duration != "-1"
        log_collector = settings.get("logging_collector", "off") == "on"
        log_dest = settings.get("log_destination", "")

        inactive_slots = sum(
            1 for r in tables.get("replication_slots", [])
            if r.get("active") == "f"
            and (bool(r.get("restart_lsn")) or (safe_float(r.get("retained_bytes")) or 0) > 0)
        )
        superuser_count = sum(1 for r in tables.get("roles", []) if r.get("is_superuser") == "t")

        # --- filesystem usage for PGDATA ---
        data_dir = settings.get("data_directory", "")
        fs_usage: float | None = None
        fs_rows = _parse_df_pt(ctx.root / "tables" / "filesystems.tsv")
        for row in fs_rows:
            mp = row.get("mountpoint", "")
            if data_dir and mp and data_dir.startswith(mp):
                fs_usage = row.get("usage_percent")
                break

        return {
            "collection_quality": self.collection_quality(ctx)["score"],
            "cache_hit_ratio": cache_hit,
            "connection_usage": conn_usage,
            "max_connections": max_conn,
            "current_connections": cur_conn,
            "dead_tuples_total": dead_tup_total,
            "max_dead_ratio": round(max_dead * 100, 2) if thealth else None,
            "stale_tables": stale_tables,
            "max_replication_lag_bytes": max_lag_bytes,
            "active_replication_slots": active_slots,
            "archive_failed_count": arch_failed,
            "max_xid_age": max_xid_age,
            "bloat_table_count": bloat_tables,
            "max_dead_bytes": max_dead_bytes,
            "unused_index_count": unused_idx_count,
            "duplicate_index_count": dup_idx_count,
            "long_transaction_count": long_xact_count,
            "max_transaction_seconds": max_xact_sec,
            "lock_wait_count": len(lock_waits),
            "max_partitions": max_partitions,
            # ``if x`` would turn a genuine 0.0 reading (idle CPU, zero iowait)
            # into "not collected", and the rule engine would report
            # not_evaluated instead of passed.
            "cpu_busy_max": round(cpu_busy_max, 2) if cpu_busy_max is not None else None,
            "iowait_max": round(iowait_max, 2) if iowait_max is not None else None,
            "memory_used_max": round(mem_used_max, 2) if mem_used_max is not None else None,
            "disk_util_max": round(disk_util_max, 2) if disk_util_max is not None else None,
            "sar_effective_coverage_hours": sar_coverage_hours,
            "system_metric_source": "sar+realtime" if sar_cpu_rows else "realtime",
            "max_active_sessions": int(max_active) if max_active else None,
            "has_pg_stat_statements": has_pgss,
            "has_backup_tool": has_backup_tool,
            "has_backup_cron": has_backup_cron,
            "backup_assessment_complete": backup_assessment_complete,
            "has_trust_auth": has_trust,
            "weak_password_encryption": weak_pw,
            "shared_buffers": shared_buffers,
            "effective_cache_size": effective_cache,
            "work_mem": work_mem,
            "maintenance_work_mem": maintenance_work_mem,
            "wal_level": wal_level,
            "archive_mode": archive_mode,
            "autovacuum": autovacuum,
            "no_pk_count": no_pk_count,
            "invalid_index_count": invalid_idx,
            "ssl_enabled": ssl_on,
            "has_idle_timeout": has_idle_timeout,
            "has_stmt_timeout": has_stmt_timeout,
            "has_slow_query_log": has_slow_query_log,
            "log_collector_on": log_collector,
            "log_destination": log_dest,
            "inactive_slots": inactive_slots,
            "superuser_count": superuser_count,
            "data_dir_filesystem_usage": fs_usage,
        }

