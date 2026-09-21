"""PostgreSQL chart provider.

Chart rendering consumes facts and metrics but does not run rules or build
report conclusions.

Layering (stage 14 step ④):

- the four whole-host charts (CPU / memory / disk / network) come from the shared
  ``inspection_core.charts`` layer — identical specs, palette and time handling
  as MySQL and Oracle, with the Pillow fallback included;
- ``{tag}_pg_sessions`` and ``{tag}_pg_stats`` stay here because they read
  PostgreSQL counters, but they take their style and time handling from the
  common layer.

Every file carries the instance tag: a single report run may hold several
instances sharing one ``charts/`` directory, and untagged names overwrite each
other.
"""

from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path
from typing import Any

from inspection_core.charts import (
    REALTIME_SCOPE,
    apply_style,
    display_timezone,
    duration_ms,
    has_matplotlib,
    os_chart_specs,
    parse_chart_time,
    render_spec,
)
from inspection_core.models import PackageContext
from inspection_core.values import safe_float

# matplotlib is only required for the two PostgreSQL-specific charts; the four
# OS charts degrade to the Pillow renderer on their own.
_MATPLOTLIB_REASON = "matplotlib_not_installed"


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
        sessions = ctx.timeseries.get("pg_activity", [])
        stats = ctx.timeseries.get("pg_stats", [])
        wanted = bool(sessions) or bool(stats)
        if not wanted:
            return []
        if not has_matplotlib():
            if sessions or stats:
                return [{"chart_id": "PG_CHARTS", "status": "skipped",
                         "reason": _MATPLOTLIB_REASON}]
            return []

        import matplotlib.dates as mdates
        import matplotlib.pyplot as plt

        apply_style()
        timezone_name, display_tz = display_timezone(ctx)
        records: list[dict[str, Any]] = []

        def _line(cid: str, title: str, data: list[dict[str, Any]],
                  series: list[tuple[str, str]], ylabel: str) -> None:
            fig, axis = plt.subplots(figsize=(10, 3.9))
            x_values = _x_axis(axis, data, display_tz)
            for label, column in series:
                axis.plot(x_values, [safe_float(row.get(column)) for row in data],
                          linewidth=1.6, marker="o", markersize=4, label=label)
            axis.set_title(title, loc="center", fontsize=13)
            axis.set_ylabel(ylabel)
            axis.set_xlabel(f"时间（{timezone_name}）")
            axis.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M:%S", tz=display_tz))
            axis.xaxis.set_major_locator(mdates.AutoDateLocator(tz=display_tz))
            axis.legend(loc="upper right", ncol=min(4, max(1, len(series))))
            fig.autofmt_xdate(rotation=25)
            fig.tight_layout()
            path = self.charts_dir / f"{tag}_{cid}.png"
            start_ns = time.monotonic_ns()
            fig.savefig(path)
            plt.close(fig)
            records.append({
                "chart_id": cid,
                "title": title,
                "file": path.relative_to(self.output).as_posix(),
                "status": "generated",
                "source_scope": REALTIME_SCOPE,
                "source_points": len(data),
                "renderer": "matplotlib",
                "duration_ms": duration_ms(start_ns),
            })

        if sessions and any(row.get("active_sessions") for row in sessions):
            _line("pg_sessions", "PostgreSQL 会话数（现场短时采样）", sessions,
                  [("总数", "total_sessions"), ("活跃", "active_sessions"),
                   ("空闲事务", "idle_in_xact")], "连接数")
        if stats:
            _line("pg_stats", "PostgreSQL 事务累计（现场短时采样）", stats,
                  [("提交", "xact_commit"), ("回滚", "xact_rollback")], "累计次数")
        return records


def _instance_tag(ctx: PackageContext) -> str:
    """Instance tag used as the chart filename prefix."""
    for block in (ctx.snapshot.get("instance_identity"), ctx.snapshot.get("host_identity")):
        if isinstance(block, dict):
            tag = str(block.get("instance_tag") or "").strip()
            if tag:
                return tag
    return "postgresql"


def _x_axis(axis: Any, data: list[dict[str, Any]], display_tz: Any) -> list[Any]:
    """Clock axis when the rows carry timestamps, sample index otherwise."""
    parsed: list[datetime | None] = [
        parse_chart_time(row.get("timestamp"), display_tz) for row in data
    ]
    if parsed and all(moment is not None for moment in parsed):
        return parsed  # type: ignore[return-value]
    axis.set_xlabel("采样点")
    return list(range(len(data)))
