"""MySQL chart provider.

Six report charts:

- the four whole-host charts (CPU / memory / disk / network) come from the shared
  ``inspection_core.charts`` layer — they are the same pictures Oracle and
  PostgreSQL print, so they must not be re-implemented per database;
- ``MYSQL_QPS_TPS`` and ``MYSQL_THREADS`` are MySQL-specific and stay here.

Charts are presentation artifacts only.  They consume normalized evidence,
prefer usable SAR history for host trends, and never alter analyzer facts.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from inspection_core import PackageContext
from inspection_core.charts import (
    REALTIME_SCOPE,
    finite,
    os_chart_specs,
    render_spec,
)


def _series(label: str, values: list[Any]) -> tuple[str, list[float | None]]:
    return label, [finite(value) for value in values]


def _drop_warmup(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop the first point while more than two remain.

    The first sample spans the collector's own startup, so it reads low and
    drags the scale.  With two or fewer points there is nothing to spare.
    """
    return rows[1:] if len(rows) > 2 else rows


def mysql_rate_spec(ctx: PackageContext, instance_tag: str,
                    metrics: dict[str, Any]) -> dict[str, Any]:
    """QPS/TPS from the collector's derived per-second counters."""
    rates = _drop_warmup(metrics.get("mysql_realtime", {}).get("derived_rate_series", []))
    return {
        "chart_id": "MYSQL_QPS_TPS",
        "title": "MySQL QPS/TPS（现场短时采样）",
        "x": [row.get("timestamp", "") for row in rates],
        "panels": [("", "次/秒", [
            _series("QPS", [row.get("Questions_per_sec") for row in rates]),
            _series("TPS", [
                (finite(row.get("Com_commit_per_sec")) or 0.0)
                + (finite(row.get("Com_rollback_per_sec")) or 0.0)
                for row in rates
            ]),
        ])],
        "source_scope": REALTIME_SCOPE,
        "warmup_points_excluded": 1,
        "filename": f"{instance_tag}_mysql_qps_tps.png",
    }


def mysql_threads_spec(ctx: PackageContext, instance_tag: str) -> dict[str, Any]:
    """Connected vs running threads over the live sampling window."""
    rows = ctx.timeseries.get("mysql_status", [])
    return {
        "chart_id": "MYSQL_THREADS",
        "title": "MySQL 连接与运行线程（现场短时采样）",
        "x": [row.get("timestamp", "") for row in rows],
        "panels": [("", "线程数", [
            _series("已连接", [row.get("Threads_connected") for row in rows]),
            _series("运行中", [row.get("Threads_running") for row in rows]),
        ])],
        "source_scope": REALTIME_SCOPE,
        "filename": f"{instance_tag}_mysql_threads.png",
    }


class MySQLChartProvider:
    """Generate the MySQL report charts with the shared renderer."""

    def __init__(self, output: Path, charts_dir: Path) -> None:
        self.output = output
        self.charts_dir = charts_dir

    def _chart_specs(self, ctx: PackageContext,
                     metrics: dict[str, Any]) -> list[dict[str, Any]]:
        instance_tag = ctx.snapshot["instance_identity"]["instance_tag"]
        return [
            *os_chart_specs(ctx, instance_tag),
            mysql_rate_spec(ctx, instance_tag, metrics),
            mysql_threads_spec(ctx, instance_tag),
        ]

    def generate_base(self, ctx: PackageContext,
                      metrics: dict[str, Any]) -> list[dict[str, Any]]:
        self.charts_dir.mkdir(parents=True, exist_ok=True)
        return [
            render_spec(ctx, spec, self.charts_dir, self.output)
            for spec in self._chart_specs(ctx, metrics)
        ]

    def generate(self, ctx: PackageContext,
                 metrics: dict[str, Any]) -> list[dict[str, Any]]:
        return self.generate_base(ctx, metrics)
