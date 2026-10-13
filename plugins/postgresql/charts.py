"""PostgreSQL chart provider.

Chart rendering consumes facts and metrics but does not run rules or build
report conclusions.

Layering (stage 14 step ④ + batch 20):

- the four whole-host charts (CPU / memory / disk / network) come from the shared
  ``inspection_core.charts`` layer;
- ``{tag}_pg_sessions`` and ``{tag}_pg_stats`` read PostgreSQL counters, but they
  only describe those counters as a spec — rendering, palette, time handling,
  sampling-gap breaks and the Pillow fallback all stay in the common layer.

Batch 20 folded the last hand-written matplotlib here into ``render_spec()``.
Before that these two charts disagreed with everything else in the report: data
point markers on, no source note, sampling gaps drawn as one continuous line,
and — when matplotlib was missing — no chart at all.  The plugin must not import
matplotlib again; if a chart needs something the common layer cannot express,
add a field there instead of drawing it here.

Every file carries the instance tag: a single report run may hold several
instances sharing one ``charts/`` directory, and untagged names overwrite each
other.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from inspection_core.charts import (
    REALTIME_SCOPE,
    os_chart_specs,
    render_spec,
)
from inspection_core.models import PackageContext
from inspection_core.values import safe_float


class PostgreSQLChartProvider:
    def __init__(self, output: Path, charts_dir: Path) -> None:
        self.output = output
        self.charts_dir = charts_dir

    def generate_charts(self, ctx: PackageContext,
                        metrics: dict[str, Any]) -> list[dict[str, Any]]:
        self.charts_dir.mkdir(parents=True, exist_ok=True)
        tag = _instance_tag(ctx)
        records = [
            render_spec(ctx, spec, self.charts_dir, self.output)
            for spec in os_chart_specs(ctx, tag)
        ]
        records.extend(self._pg_charts(ctx, tag))
        return records

    # ── PostgreSQL-specific charts ───────────────────────────────────────────
    def _pg_charts(self, ctx: PackageContext, tag: str) -> list[dict[str, Any]]:
        """会话数与事务累计两张：产出 spec、交给公共 ``render_spec()``。"""
        sessions = ctx.timeseries.get("pg_activity", [])
        stats = ctx.timeseries.get("pg_stats", [])
        if not sessions and not stats:
            return []

        records: list[dict[str, Any]] = []
        if sessions and any(row.get("active_sessions") for row in sessions):
            records += self._emit(
                ctx, tag, "pg_sessions", "PostgreSQL 会话数（现场短时采样）", "连接数",
                sessions,
                [("总数", "total_sessions"), ("活跃", "active_sessions"),
                 ("空闲事务", "idle_in_xact")],
            )
        if stats:
            records += self._emit(
                ctx, tag, "pg_stats", "PostgreSQL 事务累计（现场短时采样）", "累计次数",
                stats, [("提交", "xact_commit"), ("回滚", "xact_rollback")],
            )
        return records

    def _emit(self, ctx: PackageContext, tag: str, chart_id: str, title: str,
              ylabel: str, data: list[dict[str, Any]],
              series: list[tuple[str, str]]) -> list[dict[str, Any]]:
        """把一组 `(图例, 列名)` 翻译成 spec，渲染与降级都归公共层。"""
        spec: dict[str, Any] = {
            "chart_id": chart_id,
            "title": title,
            "x": [str(row.get("timestamp", "")) for row in data],
            "panels": [("", ylabel, [
                (label, [safe_float(row.get(column)) for row in data])
                for label, column in series
            ])],
            "source_scope": REALTIME_SCOPE,
            "filename": f"{tag}_{chart_id}.png",
        }
        result = render_spec(ctx, spec, self.charts_dir, self.output)
        if result.get("status") != "generated":
            return []
        path = self.charts_dir / spec["filename"]
        return [{
            "chart_id": result["chart_id"],
            "title": title,
            "file": path.relative_to(self.output).as_posix(),
            "status": "generated",
            "source_scope": result["source_scope"],
            "source_points": result["source_points"],
            "renderer": result["renderer"],
            "duration_ms": result["duration_ms"],
        }]


def _instance_tag(ctx: PackageContext) -> str:
    """Instance tag used as the chart filename prefix."""
    for block in (ctx.snapshot.get("instance_identity"), ctx.snapshot.get("host_identity")):
        if isinstance(block, dict):
            tag = str(block.get("instance_tag") or "").strip()
            if tag:
                return tag
    return "postgresql"
