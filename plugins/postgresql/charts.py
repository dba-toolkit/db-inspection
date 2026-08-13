"""PostgreSQL chart provider.

Chart rendering consumes facts and metrics but does not run rules or build
report conclusions.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from inspection_core.models import PackageContext
from inspection_core.values import safe_float
from .parsers import (
    _parse_sar_cpu,
    _parse_sar_disk,
    _parse_sar_metric_by_metric,
    _parse_sar_timestamp,
    _read_sar_csv,
)


class PostgreSQLChartProvider:
    def __init__(self, charts_dir: Path) -> None:
        self.charts_dir = charts_dir

    def generate_charts(self, ctx: PackageContext, metrics: dict[str, Any]) -> list[dict[str, Any]]:
        self.charts_dir.mkdir(parents=True, exist_ok=True)
        try:
            import matplotlib
            matplotlib.use("Agg")
            import matplotlib.pyplot as plt
            import matplotlib.dates as mdates
            from .chart_style import apply_style
            apply_style(plt)
        except ImportError:
            return [{"chart_id": "ALL", "title": "图表", "status": "skipped", "reason": "matplotlib_not_installed"}]

        # Detect display timezone (like MySQL does)
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
        timezone_name = str(ctx.snapshot.get("time_evidence", {}).get("timezone", "Asia/Shanghai"))
        try:
            display_tz = ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError:
            use_zone = str(ctx.snapshot.get("time_evidence", {}).get("timezone", "UTC"))
            try:
                display_tz = ZoneInfo(use_zone)
            except ZoneInfoNotFoundError:
                display_tz = timezone.utc

        charts: list[dict[str, Any]] = []

        def _tz_label() -> str:
            return f"时间 ({display_tz.key if hasattr(display_tz, 'key') else str(display_tz)})"

        # ---- System: SAR history (24h) ----
        sar_dir = ctx.root / "history"

        sar_cpu_file = sar_dir / "sar_cpu.csv"
        if sar_cpu_file.exists():
            rows = _read_sar_csv(sar_cpu_file)
            series = _parse_sar_cpu(rows or [])
            if series:
                self._sar_line_chart("system_cpu", "CPU 使用率（SAR 历史）", series, "%", charts, tz=display_tz, xlabel=_tz_label())

        sar_mem_file = sar_dir / "sar_memory.csv"
        if sar_mem_file.exists():
            series = _parse_sar_metric_by_metric(sar_mem_file, {"%memused"})
            if series:
                self._sar_line_chart("system_memory", "内存使用率（SAR 历史）", series, "%", charts, tz=display_tz, xlabel=_tz_label())

        sar_disk_file = sar_dir / "sar_disk.csv"
        if sar_disk_file.exists():
            series = _parse_sar_disk(sar_disk_file)
            if series:
                util_series = [s for s in series if s[0].endswith("util%")]
                if util_series:
                    self._sar_line_chart("system_disk", "磁盘利用率（SAR 历史）", util_series, "%", charts, tz=display_tz, xlabel=_tz_label())

        # ---- PG: realtime sampling (30s) ----
        pg_csv = ctx.timeseries.get("pg_activity", [])
        if pg_csv and any(r.get("active_sessions") for r in pg_csv):
            self._line_chart(
                "pg_sessions", "PG 会话数 (30s)",
                pg_csv,
                [("total", "total_sessions"), ("active", "active_sessions"), ("idle in tx", "idle_in_xact")],
                "sessions", charts,
            )

        pg_stats = ctx.timeseries.get("pg_stats", [])
        if pg_stats:
            self._line_chart(
                "pg_stats", "PG 事务累计 (30s)",
                pg_stats,
                [("commit", "xact_commit"), ("rollback", "xact_rollback")],
                "count", charts,
            )

        return charts

    def _sar_line_chart(self, cid: str, title: str,
                        series: list[tuple[str, list[datetime], list[float]]],
                        y_label: str, charts: list[dict],
                        *, tz: Any = None, xlabel: str = "") -> None:
        try:
            import matplotlib
            matplotlib.use("Agg")
            import matplotlib.pyplot as plt
            import matplotlib.dates as mdates
            from .chart_style import apply_style

            apply_style(plt)
            fig, ax = plt.subplots(figsize=(14, 5))

            latest = max((x for _, xs, _ in series for x in xs), default=None)
            if latest is not None:
                cutoff = latest - timedelta(hours=24)
                filtered = []
                for label, xs, ys in series:
                    pairs = [(x, y) for x, y in zip(xs, ys) if x >= cutoff]
                    if pairs:
                        filtered.append((label, [p[0] for p in pairs], [p[1] for p in pairs]))
                series = filtered

            for label, x_vals, y_vals in series:
                ax.plot(x_vals, y_vals, label=label, linewidth=1.0, marker="", markersize=0)

            ax.set_title(title)
            ax.set_ylabel(y_label)
            all_x = [x for _, xs, _ in series for x in xs]
            span = (max(all_x) - min(all_x)).total_seconds() if len(all_x) > 1 else 0
            fmt = "%m-%d %H:%M" if span >= 86400 else "%H:%M"
            chart_tz = tz or (all_x[0].tzinfo if all_x else timezone.utc)
            ax.set_xlabel(xlabel if xlabel else f"time ({chart_tz})")
            ax.xaxis.set_major_formatter(mdates.DateFormatter(fmt, tz=chart_tz))
            fig.autofmt_xdate(rotation=0, ha="center")
            ax.legend(loc="best", fontsize=8)
            ax.grid(True, alpha=0.3)

            path = self.charts_dir / f"{cid}.png"
            fig.savefig(path, dpi=150, bbox_inches="tight")
            plt.close(fig)
            charts.append({"chart_id": cid, "title": title, "file": f"charts/{cid}.png"})
        except Exception:
            pass

    def _line_chart(self, cid: str, title: str, data: list[dict],
                    series: list[tuple[str, str]], y_label: str,
                    charts: list[dict]) -> None:
        try:
            import matplotlib
            matplotlib.use("Agg")
            import matplotlib.pyplot as plt
            import matplotlib.dates as mdates
            from .chart_style import apply_style

            apply_style(plt)
            fig, ax = plt.subplots(figsize=(14, 5))

            # Use timestamp if available, else formatted elapsed
            if data and data[0].get("timestamp"):
                x_vals = [_parse_sar_timestamp(r.get("timestamp", "")) for r in data]
                if all(v is None for v in x_vals):
                    x_vals = [safe_float(r.get("elapsed_ms")) or 0 for r in data]
                    ax.set_xlabel("elapsed (ms)")
                    ax.xaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"{x/1000:.0f}s"))
                else:
                    valid_x = [v for v in x_vals if v is not None]
                    tz = valid_x[0].tzinfo if valid_x else timezone.utc
                    ax.set_xlabel(f"time ({valid_x[0].tzname() or 'UTC'})" if valid_x else "time")
                    ax.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d\n%H:%M:%S", tz=tz))
                    fig.autofmt_xdate(rotation=0, ha="center")
            else:
                x_vals = [safe_float(r.get("elapsed_ms")) or 0 for r in data]
                ax.set_xlabel("elapsed")
                ax.xaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"{x/1000:.0f}s"))

            for label, col in series:
                y_vals = [safe_float(r.get(col)) for r in data]
                ax.plot(x_vals, y_vals, label=label, linewidth=1.2, marker="o", markersize=3)

            ax.set_title(title)
            ax.set_ylabel(y_label)
            ax.legend(loc="best", fontsize=9)
            ax.grid(True, alpha=0.3)

            path = self.charts_dir / f"{cid}.png"
            fig.savefig(path, dpi=150, bbox_inches="tight")
            plt.close(fig)
            charts.append({"chart_id": cid, "title": title, "file": f"charts/{cid}.png"})
        except Exception:
            pass

