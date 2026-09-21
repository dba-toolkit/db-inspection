"""Oracle 图表生成：系统四张走公共层，Oracle 专属四张留插件，不参与风险判断。

分层边界（阶段 14 第③步）：
- CPU / 内存 / 磁盘 / 网络四张是纯 OS 图，直接取 ``os_chart_specs()`` 并用公共
  渲染器出图 —— 与 MySQL 同一套规格、同一套配色、同一套时间处理，matplotlib
  缺失时自动降级到 Pillow。
- ``oracle_physical_io`` / ``oracle_logical_vs_physical`` / ``oracle_redo_rate`` /
  ``oracle_parse_ratio`` 依赖 Oracle 语义（统计项速率、双轴解析率），仍由本插件
  用 matplotlib 绘制，但配色与时间处理一律取自公共层。

输出记录保持 Oracle 报告契约的字段（``chart_id`` + ``path`` + ``source_points``），
并补上 ``status`` / ``source_scope``，让下游不必再按图名猜数据来源。
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
from inspection_core.charts.style import COLOR_MAP

from .metrics import safe_float

OS_CHART_IDS = ("SYSTEM_CPU", "SYSTEM_MEMORY", "SYSTEM_DISK", "SYSTEM_NETWORK_REALTIME")

# matplotlib 缺失时只影响这四张 Oracle 专属图。
_MATPLOTLIB_REASON = "matplotlib_not_installed"


def _pairs(rows: list[dict[str, Any]], values: list[dict[str, Any]], key: str,
           display_tz: Any) -> list[tuple[datetime, float]]:
    """把「时间戳」与「派生速率」配成对，跳过无法解析的时间点。

    派生速率由相邻两个采样点算出，因此比时间戳少一个，两者按顺序对齐。
    """
    result: list[tuple[datetime, float]] = []
    for row, rate in zip(rows[1:], values):
        moment = parse_chart_time(row.get("timestamp"), display_tz)
        value = safe_float(rate.get(key))
        if moment is None or value is None:
            continue
        result.append((moment, value))
    return result


class OracleChartProvider:
    def __init__(self, charts_dir: Path) -> None:
        self.charts_dir = charts_dir

    def generate(self, ctx: Any, metrics: dict[str, Any]) -> list[dict[str, Any]]:
        self.charts_dir.mkdir(parents=True, exist_ok=True)
        tag = str(ctx.snapshot.get("instance_tag") or "oracle")
        records = self._os_charts(ctx, tag)
        records.extend(self._oracle_charts(ctx, tag, metrics))
        return records

    # ── 公共 OS 四张 ─────────────────────────────────────────────────────────
    def _os_charts(self, ctx: Any, tag: str) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for spec in os_chart_specs(ctx, tag):
            result = render_spec(ctx, spec, self.charts_dir, self.charts_dir)
            if result.get("status") != "generated":
                continue
            path = self.charts_dir / spec["filename"]
            records.append({
                "chart_id": result["chart_id"],
                "path": str(path.resolve()),
                "status": "generated",
                "source_scope": result["source_scope"],
                "source_points": result["source_points"],
                "renderer": result["renderer"],
                "duration_ms": result["duration_ms"],
            })
        return records

    # ── Oracle 专属四张 ──────────────────────────────────────────────────────
    def _oracle_charts(self, ctx: Any, tag: str,
                       metrics: dict[str, Any]) -> list[dict[str, Any]]:
        rows = ctx.timeseries.get("oracle_sysstat", [])
        rates = metrics.get("oracle_realtime", {}).get("derived_rate_series", [])
        if len(rows) < 2 or not rates or not has_matplotlib():
            if len(rows) >= 2 and rates and not has_matplotlib():
                return [{"chart_id": "ORACLE_CHARTS", "status": "skipped",
                         "reason": _MATPLOTLIB_REASON}]
            return []

        import matplotlib.dates as mdates
        import matplotlib.pyplot as plt

        apply_style()
        timezone_name, display_tz = display_timezone(ctx)
        records: list[dict[str, Any]] = []

        def _figure(title: str, ylabel: str, filename: str,
                    series: list[tuple[str, list[tuple[datetime, float]]]],
                    *, secondary: tuple[str, list[tuple[datetime, float]]] | None = None,
                    points: int) -> None:
            fig, axis = plt.subplots()
            for label, pairs in series:
                axis.plot([p[0] for p in pairs], [p[1] for p in pairs],
                          linewidth=1.5, marker="o", markersize=4, label=label)
            handles, labels = axis.get_legend_handles_labels()
            if secondary is not None:
                twin = axis.twinx()
                twin.plot([p[0] for p in secondary[1]], [p[1] for p in secondary[1]],
                          color=COLOR_MAP["red"], linewidth=2, marker="D", markersize=4,
                          label=secondary[0])
                twin.set_ylabel(secondary[0])
                twin_handles, twin_labels = twin.get_legend_handles_labels()
                handles, labels = handles + twin_handles, labels + twin_labels
            axis.set_title(f"{title} — {tag}")
            axis.set_ylabel(ylabel)
            axis.legend(handles, labels, loc="upper left", frameon=False, fontsize=9)
            axis.set_xlabel(f"时间（{timezone_name}）")
            axis.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M:%S", tz=display_tz))
            axis.xaxis.set_major_locator(mdates.AutoDateLocator(tz=display_tz))
            fig.autofmt_xdate(rotation=25)
            fig.tight_layout()
            path = self.charts_dir / filename
            start_ns = time.monotonic_ns()
            fig.savefig(path)
            plt.close(fig)
            records.append({
                "chart_id": path.stem,
                "path": str(path.resolve()),
                "status": "generated",
                "source_scope": REALTIME_SCOPE,
                "source_points": points,
                "renderer": "matplotlib",
                "duration_ms": duration_ms(start_ns),
            })

        def _series(key: str) -> list[tuple[datetime, float]]:
            return _pairs(rows, rates, key, display_tz)

        def _positive(pairs: list[tuple[datetime, float]]) -> bool:
            """保留原有门槛：全零的速率不出一张平线图。"""
            return any(value > 0 for _, value in pairs)

        reads = _series("physical_reads_per_sec")
        writes = _series("physical_writes_per_sec")
        commits = _series("user_commits_per_sec")
        rollbacks = _series("user_rollbacks_per_sec")
        logical = _series("session_logical_reads_per_sec")
        redo = _series("redo_size_per_sec")
        execs = _series("execute_count_per_sec")
        hard = _series("parse_count_hard_per_sec")

        if _positive(commits) or _positive(rollbacks):
            _figure("Oracle Transaction Rate", "txn/s", "oracle_txn_rate.png",
                    [("commits/s", commits), ("rollbacks/s", rollbacks)], points=len(commits))
        if _positive(reads) or _positive(writes):
            _figure("Oracle Physical IO Rate", "IO/s", "oracle_physical_io.png",
                    [("reads/s", reads), ("writes/s", writes)], points=len(reads))
        if _positive(logical) and len(reads) >= 2:
            _figure("Oracle Reads: Logical vs Physical", "reads/s",
                    "oracle_logical_vs_physical.png",
                    [("logical reads/s", logical), ("physical reads/s", reads)],
                    points=len(logical))
        if _positive(redo):
            _figure("Oracle Redo Rate", "MB/s", "oracle_redo_rate.png",
                    [("redo MB/s", [(moment, value / 1024 / 1024) for moment, value in redo])],
                    points=len(redo))
        if any(value >= 1 for _, value in execs) and len(hard) >= 2:
            ratio = [(moment, hard_value / (exec_value or 1) * 100)
                     for (moment, exec_value), (_, hard_value) in zip(execs, hard)]
            _figure("Oracle Exec Rate & Hard Parse %", "exec/s", "oracle_parse_ratio.png",
                    [("exec/s", execs)], secondary=("hard parse %", ratio), points=len(execs))
        return records
