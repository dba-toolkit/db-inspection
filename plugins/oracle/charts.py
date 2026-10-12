"""Oracle 图表生成：系统四张走公共层，Oracle 专属五张留插件，不参与风险判断。

分层边界（阶段 14 第③步）：
- CPU / 内存 / 磁盘 / 网络四张是纯 OS 图，直接取 ``os_chart_specs()`` 并用公共
  渲染器出图 —— 与 MySQL 同一套规格、同一套配色、同一套时间处理，matplotlib
  缺失时自动降级到 Pillow。
- ``ORACLE_CHART_IDS`` 里的五张依赖 Oracle 语义（统计项速率、双轴解析率），仍由
  本插件用 matplotlib 绘制，但配色与时间处理一律取自公共层。

★ **``ORACLE_CHART_IDS`` 是「产出」与「声明」的单一来源**：本模块按它出图，
  ``plugins/oracle/word_report.py`` 的 ``section_charts`` 按它声明。两份集合一旦
  分叉就会出现两种静默缺陷 —— 产出了没人声明（**生成即丢弃**，图算了白算），
  或声明了产不出（报告里"这个位置本该有图"只能靠读代码才知道）。
  ``tests/test_oracle_chart_coverage.py`` 钉住两者相等，别再手抄第二份清单。

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
    render_bar_spec,
    render_spec,
)
from inspection_core.charts.style import COLOR_MAP

from .metrics import safe_float

OS_CHART_IDS = ("SYSTEM_CPU", "SYSTEM_MEMORY", "SYSTEM_DISK", "SYSTEM_NETWORK_REALTIME")

# Oracle 专属图的 chart_id —— **产出（本模块）与声明（word_report）的唯一清单**。
# 顺序即报告里的上图顺序；``_figure()`` 用 chart_id 直接命名 PNG，不再手写文件名，
# 因此这里改一个 id 就等于同时改了产出与声明（前提是 word_report 从本常量派生）。
ORACLE_CHART_IDS = (
    "oracle_txn_rate",
    "oracle_physical_io",
    "oracle_logical_vs_physical",
    "oracle_redo_rate",
    "oracle_parse_ratio",
    # 批 12b：下面三张的数据源是**包内的表**（非实时采样时序），形态与上面五张
    # 不同，但同样受「产出 == 声明」约束，故一并登记在这份唯一清单里。
    "oracle_tablespace_usage",
    "oracle_topsql_elapsed",
    "oracle_archive_daily",
)

# matplotlib 缺失时只影响上面这五张 Oracle 专属图。
_MATPLOTLIB_REASON = "matplotlib_not_installed"

# 单张排行图最多画多少个类别（表空间 / Top SQL 共用）。
_TOP_N = 10

# 图题（呈现层文案）与图 id **同源**，放一起 —— 谁抄第二份，新图上线时就会静默退化：
# `report_adapter` 是「重建」模型而非透传，没有登记的 id 会把 caption 落回 chart_id，
# 报告里图题直接变成英文 id。word_report 与 report_adapter 都从这里取。
OS_CHART_CAPTIONS = {
    "SYSTEM_CPU": "CPU 使用率趋势",
    "SYSTEM_MEMORY": "内存使用率趋势",
    "SYSTEM_DISK": "磁盘 I/O 趋势",
    "SYSTEM_NETWORK_REALTIME": "网络吞吐趋势",
}
ORACLE_CHART_CAPTIONS = {
    "oracle_txn_rate": "事务速率",
    "oracle_physical_io": "物理 I/O 趋势",
    "oracle_logical_vs_physical": "逻辑读与物理读",
    "oracle_redo_rate": "Redo 速率",
    "oracle_parse_ratio": "解析率",
    "oracle_topsql_elapsed": "Top SQL 执行时间排行",
    "oracle_tablespace_usage": "表空间使用率排行",
    "oracle_archive_daily": "每日归档量趋势",
}


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
        records.extend(self._table_charts(ctx, tag))
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

        def _figure(title: str, ylabel: str, chart_id: str,
                    series: list[tuple[str, list[tuple[datetime, float]]]],
                    *, secondary: tuple[str, list[tuple[datetime, float]]] | None = None,
                    points: int) -> None:
            # 产出侧自守门：id 必须在单一来源里注册过，否则会画出一张没人声明、
            # 永远不会出现在报告里的图（生成即丢弃）。宁可当场报错，也别静默产垃圾。
            if chart_id not in ORACLE_CHART_IDS:
                raise ValueError(f"未在 ORACLE_CHART_IDS 注册的 Oracle 图：{chart_id}")
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
            path = self.charts_dir / f"{chart_id}.png"
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
            _figure("Oracle Transaction Rate", "txn/s", "oracle_txn_rate",
                    [("commits/s", commits), ("rollbacks/s", rollbacks)], points=len(commits))
        if _positive(reads) or _positive(writes):
            _figure("Oracle Physical IO Rate", "IO/s", "oracle_physical_io",
                    [("reads/s", reads), ("writes/s", writes)], points=len(reads))
        if _positive(logical) and len(reads) >= 2:
            _figure("Oracle Reads: Logical vs Physical", "reads/s",
                    "oracle_logical_vs_physical",
                    [("logical reads/s", logical), ("physical reads/s", reads)],
                    points=len(logical))
        if _positive(redo):
            _figure("Oracle Redo Rate", "MB/s", "oracle_redo_rate",
                    [("redo MB/s", [(moment, value / 1024 / 1024) for moment, value in redo])],
                    points=len(redo))
        if any(value >= 1 for _, value in execs) and len(hard) >= 2:
            ratio = [(moment, hard_value / (exec_value or 1) * 100)
                     for (moment, exec_value), (_, hard_value) in zip(execs, hard)]
            _figure("Oracle Exec Rate & Hard Parse %", "exec/s", "oracle_parse_ratio",
                    [("exec/s", execs)], secondary=("hard parse %", ratio), points=len(execs))
        return records


    # ── 表数据三张（批 12b）─────────────────────────────────────────────────
    def _table_charts(self, ctx: Any, tag: str) -> list[dict[str, Any]]:
        """三张「有数据没画」的图：表空间 Top-N / Top SQL Top-N / 归档量趋势。

        与 ``_oracle_charts`` 的区别在**数据形态**：那五张画的是实时采样派生速率
        （``ctx.timeseries``），这三张读的是包里的表（``ctx.tables``）—— 表空间用量、
        Top SQL、归档量都是**一次快照的排行/汇总**，不是时间序列。
        """
        records: list[dict[str, Any]] = []
        records.extend(self._tablespace_chart(ctx))
        records.extend(self._topsql_chart(ctx))
        records.extend(self._archive_chart(ctx))
        return records

    def _tablespace_chart(self, ctx: Any) -> list[dict[str, Any]]:
        rows = _rows(ctx, "oracle.tablespace.usage", "所有表空间容量使用情况",
                     "tablespace.usage")
        items: list[tuple[str, float]] = []
        for row in rows:
            name = _text(row, "TABLESPACE_NAME")
            used = _number(row, "PCT_USED")
            if not name or used is None:
                continue
            items.append((name, used))
        items.sort(key=lambda pair: pair[1], reverse=True)
        items = items[:_TOP_N]
        if not items:
            return []
        spec = {
            "chart_id": "oracle_tablespace_usage",
            "title": f"表空间使用率 TOP{len(items)}",
            "categories": [name for name, _ in items],
            "values": [value for _, value in items],
            "value_suffix": "%",
            "xmax": 100.0,
            "alert_threshold": 90.0,
            "xlabel": "已用率（%）",
            "filename": "oracle_tablespace_usage.png",
            "source_scope": "table_snapshot",
        }
        return self._bar_record(spec)

    def _topsql_chart(self, ctx: Any) -> list[dict[str, Any]]:
        rows = _rows(ctx, "oracle.topsql.elapsed_time", "Top 20 SQL (按执行时间)",
                     "topsql.elapsed_time")
        items: list[tuple[str, float]] = []
        for row in rows:
            sql_id = _text(row, "SQL_ID")
            elapsed = _number(row, "ELAPSED_MIN")
            if not sql_id or elapsed is None:
                continue
            items.append((sql_id, elapsed))
        items.sort(key=lambda pair: pair[1], reverse=True)
        items = items[:_TOP_N]
        if not items:
            return []
        spec = {
            "chart_id": "oracle_topsql_elapsed",
            "title": f"Top SQL 执行时间 TOP{len(items)}",
            "categories": [sql_id for sql_id, _ in items],
            "values": [value for _, value in items],
            "value_suffix": "min",
            "xlabel": "执行时间（分钟）",
            "filename": "oracle_topsql_elapsed.png",
            "source_scope": "table_snapshot",
        }
        return self._bar_record(spec)

    def _archive_chart(self, ctx: Any) -> list[dict[str, Any]]:
        rows = _rows(ctx, "oracle.archive.daily_30d", "近30天每日归档量")
        pairs: list[tuple[str, float]] = []
        for row in rows:
            day = _text(row, "DAY")
            gb = _number(row, "DAY_GB")
            if not day or gb is None:
                continue
            pairs.append((day, gb))
        pairs.sort(key=lambda pair: pair[0])  # 按日期升序，折线才连得对
        if len(pairs) < 2:
            return []
        spec = {
            "chart_id": "oracle_archive_daily",
            "title": "每日归档量（近 30 天）",
            "x": [day for day, _ in pairs],
            "panels": [("", "GB", [("归档量", [gb for _, gb in pairs])])],
            "source_scope": "historical",
            "filename": "oracle_archive_daily.png",
        }
        result = render_spec(ctx, spec, self.charts_dir, self.charts_dir)
        if result.get("status") != "generated":
            return []
        path = self.charts_dir / spec["filename"]
        return [{
            "chart_id": result["chart_id"], "path": str(path.resolve()),
            "status": "generated", "source_scope": result["source_scope"],
            "source_points": result["source_points"], "renderer": result["renderer"],
            "duration_ms": result["duration_ms"],
        }]

    def _bar_record(self, spec: dict[str, Any]) -> list[dict[str, Any]]:
        result = render_bar_spec(spec, self.charts_dir, self.charts_dir)
        if result.get("status") != "generated":
            return []
        path = self.charts_dir / spec["filename"]
        return [{
            "chart_id": result["chart_id"], "path": str(path.resolve()),
            "status": "generated", "source_scope": result["source_scope"],
            "source_points": result["source_points"], "renderer": result["renderer"],
            "duration_ms": result["duration_ms"],
        }]


def _rows(ctx: Any, *names: str) -> list[dict[str, str]]:
    """按候选键取包内表；先走 adapter 的 ``find_table``（带子串兜底），再退化为直查。

    退化分支是给**测试替身**留的：合成 ctx 未必是完整 adapter 子类，但 ``tables``
    这个字典是基类就有的。
    """
    finder = getattr(ctx, "find_table", None)
    if callable(finder):
        rows = finder(*names)
        if rows:
            return rows
    tables = getattr(ctx, "tables", None) or {}
    for name in names:
        rows = tables.get(name)
        if rows:
            return rows
    return []


def _text(row: dict[str, Any], *names: str) -> str:
    """取字符串列，容忍列名大小写差异与 sqlplus 表头截断（前缀互含）。"""
    upper = {str(key).upper(): key for key in row}
    for name in names:
        wanted = name.upper()
        if wanted in upper and str(row.get(upper[wanted]) or "").strip():
            return str(row[upper[wanted]]).strip()
    for name in names:
        wanted = name.upper()
        for key, original in upper.items():
            if len(key) >= 4 and (wanted.startswith(key) or key.startswith(wanted)):
                value = str(row.get(original) or "").strip()
                if value:
                    return value
    return ""


def _number(row: dict[str, Any], *names: str) -> float | None:
    return safe_float(_text(row, *names))

