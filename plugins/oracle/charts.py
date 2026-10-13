"""Oracle 图表生成：全部交给公共层渲染，插件只把 Oracle 语义翻译成 spec。

分层边界（阶段 14 第③步 + 批 20）：
- CPU / 内存 / 磁盘 / 网络四张是纯 OS 图，直接取 ``os_chart_specs()``；
- 事务速率 / 物理 I/O / 逻辑读对比 / Redo / 解析率五张依赖 Oracle 语义的统计项
  速率，但**渲染同样交给公共 ``render_spec()``**（解析率那张用公共层的
  ``secondary`` 字段表达双轴）；
- 表空间 / Top SQL / 归档量三张走 ``render_bar_spec()`` / ``render_spec()``。

★ 批 20 之前那五张是**手写 matplotlib**，于是同一份报告里并存两套版式。公共层
  出的图是中文题、右上带框图例、无数据点标记、底部带口径注、采样中断处断开折线、
  缺 matplotlib 时降级 Pillow；手绘的那五张全是反的 —— 英文题、左上无框图例、
  带 marker、没有口径注、**把采样中断连成假趋势**、缺 matplotlib 时整批消失。
  现在本插件里不再允许出现 ``plt.subplots`` / ``savefig``：样式与降级只有公共层
  一份。发现插件又自己画图，先问「公共层是不是缺个字段」。

★ **``ORACLE_CHART_IDS`` 是「产出」与「声明」的单一来源**：本模块按它出图，
  ``plugins/oracle/word_report.py`` 的 ``section_charts`` 按它声明。两份集合一旦
  分叉就会出现两种静默缺陷 —— 产出了没人声明（**生成即丢弃**，图算了白算），
  或声明了产不出（报告里"这个位置本该有图"只能靠读代码才知道）。
  ``tests/test_oracle_chart_coverage.py`` 钉住两者相等，别再手抄第二份清单。

输出记录保持 Oracle 报告契约的字段（``chart_id`` + ``path`` + ``source_points``），
并补上 ``status`` / ``source_scope``，让下游不必再按图名猜数据来源。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from inspection_core.charts import (
    REALTIME_SCOPE,
    os_chart_specs,
    render_bar_spec,
    render_spec,
)

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
    "oracle_tablespace_usage": "表空间容量使用率排行",
    "oracle_archive_daily": "每日归档量趋势",
}


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

    # ── Oracle 专属五张（实时速率）—— 只翻译语义，渲染归公共层 ────────────────
    def _oracle_charts(self, ctx: Any, tag: str,
                       metrics: dict[str, Any]) -> list[dict[str, Any]]:
        """五张速率图：产出 spec、交给公共 ``render_spec()``。

        ``tag`` 保留在签名里只为与 ``_os_charts`` / ``_table_charts`` 同形；实例信息
        由公共层的图题与文件名负责，插件不再往标题里拼。
        """
        rows = ctx.timeseries.get("oracle_sysstat", [])
        rates = metrics.get("oracle_realtime", {}).get("derived_rate_series", [])
        if len(rows) < 2 or not rates:
            return []

        # 派生速率由相邻两个采样点算出，因此比时间戳少一个，两者按顺序对齐。
        # ★ 用 zip 截断而不是逐点跳过：跳过会把后面的点整体前移，等于把采样中断
        #   画成连续 —— 而公共渲染器本来就会在断点处断开折线，别在取数层提前抹平。
        paired = list(zip([str(row.get("timestamp", "")) for row in rows[1:]], rates))
        x_values = [item[0] for item in paired]
        rate_rows = [item[1] for item in paired]
        if len(x_values) < 2:
            return []

        records: list[dict[str, Any]] = []

        def _emit(chart_id: str, title: str, ylabel: str,
                  series: list[tuple[str, list[float | None]]], *,
                  secondary: dict[str, Any] | None = None) -> None:
            # 产出侧自守门：id 必须在单一来源里注册过，否则会画出一张没人声明、
            # 永远不会出现在报告里的图（生成即丢弃）。宁可当场报错，也别静默产垃圾。
            if chart_id not in ORACLE_CHART_IDS:
                raise ValueError(f"未在 ORACLE_CHART_IDS 注册的 Oracle 图：{chart_id}")
            spec: dict[str, Any] = {
                "chart_id": chart_id,
                "title": title,
                "x": x_values,
                "panels": [("", ylabel, series)],
                "source_scope": REALTIME_SCOPE,
                "filename": f"{chart_id}.png",
            }
            if secondary is not None:
                spec["secondary"] = secondary
            result = render_spec(ctx, spec, self.charts_dir, self.charts_dir)
            if result.get("status") != "generated":
                return
            records.append({
                "chart_id": result["chart_id"],
                "path": str((self.charts_dir / spec["filename"]).resolve()),
                "status": "generated",
                "source_scope": result["source_scope"],
                "source_points": result["source_points"],
                "renderer": result["renderer"],
                "duration_ms": result["duration_ms"],
            })

        def _series(key: str) -> list[float | None]:
            return [safe_float(row.get(key)) for row in rate_rows]

        def _positive(values: list[float | None]) -> bool:
            """保留原有门槛：全零的速率不出一张平线图。"""
            return any(value is not None and value > 0 for value in values)

        reads = _series("physical_reads_per_sec")
        writes = _series("physical_writes_per_sec")
        commits = _series("user_commits_per_sec")
        rollbacks = _series("user_rollbacks_per_sec")
        logical = _series("session_logical_reads_per_sec")
        redo = _series("redo_size_per_sec")
        execs = _series("execute_count_per_sec")
        hard = _series("parse_count_hard_per_sec")

        if _positive(commits) or _positive(rollbacks):
            _emit("oracle_txn_rate", "Oracle 事务速率（现场短时采样）", "次/秒",
                  [("提交", commits), ("回滚", rollbacks)])
        if _positive(reads) or _positive(writes):
            _emit("oracle_physical_io", "Oracle 物理 I/O 速率（现场短时采样）", "次/秒",
                  [("物理读", reads), ("物理写", writes)])
        if _positive(logical) and len(reads) >= 2:
            _emit("oracle_logical_vs_physical", "Oracle 逻辑读与物理读（现场短时采样）",
                  "次/秒", [("逻辑读", logical), ("物理读", reads)])
        if _positive(redo):
            _emit("oracle_redo_rate", "Oracle Redo 速率（现场短时采样）", "MB/s",
                  [("Redo 速率",
                    [None if value is None else value / 1024 / 1024 for value in redo])])
        if any(value is not None and value >= 1 for value in execs) and len(hard) >= 2:
            ratio = [None if (exec_value is None or hard_value is None)
                     else hard_value / (exec_value or 1) * 100
                     for exec_value, hard_value in zip(execs, hard)]
            _emit("oracle_parse_ratio", "Oracle 执行速率与硬解析率（现场短时采样）",
                  "次/秒", [("执行速率", execs)],
                  secondary={"ylabel": "硬解析率（%）", "series": [("硬解析率", ratio)]})
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
            # ★ 口径必须与**同一份报告的表 6.1** 和**规则层**一致：都按
            #   「已使用 ÷ 最大可扩(MAX_MB)」。此前图取的是 `PCT_USED`
            #   （已使用 ÷ 已分配 ALLOC_MB），于是 SYSTEM 表空间在图里 99.98% 被标红、
            #   在表里却是 3.39% —— 同一次巡检、同一个表空间、两个数，客户只会
            #   先信那个更吓人的。MAX_MB 缺失时才退回 ALLOC 口径（老包）。
            used = _number(row, "USED_MB")
            max_mb = _number(row, "MAX_MB")
            if used is not None and max_mb and max_mb > 0:
                pct = used / max_mb * 100
            else:
                pct = _number(row, "PCT_USED")
            if not name or pct is None:
                continue
            items.append((name, pct))
        items.sort(key=lambda pair: pair[1], reverse=True)
        items = items[:_TOP_N]
        if not items:
            return []
        spec = {
            "chart_id": "oracle_tablespace_usage",
            "title": f"表空间容量使用率 TOP{len(items)}",
            "categories": [name for name, _ in items],
            "values": [value for _, value in items],
            "value_suffix": "%",
            "xmax": 100.0,
            "alert_threshold": 90.0,
            "xlabel": "已用率（已使用 ÷ 最大可扩，%）",
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

