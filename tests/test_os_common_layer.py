"""公共 OS 层（inspection_core/charts + inspection_core/system_checks）边界测试。

这一层是四库共用的地基，测试必须钉死三件事：
1. 公共图规格与 MySQL 现有实现等价（否则"先抽公共层再接线"会引入图表回归）；
2. 阈值与判据口径唯一（iowait 峰值 20、内存取窗口峰值，不再用 average）；
3. 采集形态不同（嵌套 system_history/system_realtime vs PG 的扁平 *_max）都要能归一化。
"""
from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path

from inspection_core.charts import (
    COLOR_MAP,
    COLORS,
    CPU_SUMMARY_VALUES,
    FLAT_PALETTE,
    apply_style,
    busy_percent,
    chart_colors,
    finite,
    gap_indices,
    generate_os_charts,
    has_matplotlib,
    os_chart_specs,
    parse_chart_time,
    parse_time,
    render_spec,
    sar_cpu_summary_rows,
    series_values,
    spec_has_data,
)
from inspection_core.charts.history import display_timezone
from inspection_core.models import PackageContext
from inspection_core.system_checks import (
    CANONICAL_RULE_IDS,
    DEFAULT_THRESHOLDS,
    MEMORY_CRITERION,
    OS_CHECK_LABELS,
    OS_RULE_KEYS,
    RETIRED_RULE_IDS,
    canonical_rule_id,
    format_bytes,
    normalize_os_metrics,
    os_disk_rows,
    os_network_rows,
    os_pressure_report,
    os_pressure_verdicts,
    os_resource_rows,
    os_source,
)

TAG = "db02_192.168.1.10_3306"


def sar_rows(count: int, first_minute: int = 0) -> list[dict[str, str]]:
    return [
        {
            "CPU": "-1",
            "timestamp": f"2026-08-11T10:{(first_minute + i):02d}:00",
            "%user": str(10 + i),
            "%system": str(3 + i),
            "%iowait": str(1 + i),
            "%steal": "0.5",
            "%idle": str(80 - i),
        }
        for i in range(count)
    ]


def memory_rows(count: int) -> list[dict[str, str]]:
    return [
        {"timestamp": f"2026-08-11T10:{i:02d}:00", "%memused": str(50 + i),
         "kbcached": str(8_000_000 + i * 1000)}
        for i in range(count)
    ]


def disk_rows(count: int) -> list[dict[str, str]]:
    return [
        {"DEV": "sda", "timestamp": f"2026-08-11T10:{i:02d}:00", "%util": str(5 + i),
         "await": str(1 + i), "rkB/s": str(10 + i), "wkB/s": str(20 + i)}
        for i in range(count)
    ]


def context(history: dict | None = None, timeseries: dict | None = None,
            timezone_name: str = "Asia/Shanghai") -> PackageContext:
    return PackageContext(
        source=Path("synthetic.tar.gz"),
        root=Path("."),
        snapshot={
            "instance_identity": {"instance_tag": TAG},
            "host_identity": {"memory_total_bytes": 16 * 1024 ** 3, "database_target_is_local": True},
            "time_evidence": {"timezone": timezone_name},
        },
        status={"items": []},
        manifest={},
        integrity={"status": "ok"},
        history=history or {},
        timeseries=timeseries or {},
    )


class ChartStyleTests(unittest.TestCase):
    def test_style_application_is_idempotent_and_reports_availability(self) -> None:
        self.assertIsInstance(has_matplotlib(), bool)
        self.assertIsInstance(apply_style(), bool)
        self.assertIsInstance(apply_style(), bool)

    def test_palette_and_semantic_aliases_are_consistent(self) -> None:
        self.assertEqual(len(COLORS), 8)
        self.assertEqual(len(FLAT_PALETTE), 6)
        for name in ("critical", "high", "medium", "low", "healthy", "muted"):
            self.assertIn(name, COLOR_MAP)
        self.assertEqual(set(chart_colors()), {
            "primary", "alert", "highlight", "normal", "purple", "teal", "muted", "dark",
        })

    def test_fourteen_semantic_series_aliases_exist(self) -> None:
        from inspection_core.charts import style

        aliases = [
            "CPU_USER", "CPU_SYSTEM", "CPU_IOWAIT", "CPU_STEAL",
            "MEM_USED", "MEM_AVAILABLE", "MEM_CACHED", "MEM_SWAP",
            "DISK_READ", "DISK_WRITE", "DISK_UTIL", "DISK_AWAIT",
            "NET_RX", "NET_TX",
        ]
        for name in aliases:
            self.assertIn(getattr(style, name), COLORS)


class ChartHistoryTests(unittest.TestCase):
    def test_parse_time_normalises_utc_suffix_and_z(self) -> None:
        self.assertIsNotNone(parse_time("2026-08-11 10:00:00 UTC"))
        self.assertIsNotNone(parse_time("2026-08-11T10:00:00Z"))
        self.assertIsNone(parse_time(""))
        self.assertIsNone(parse_time("not-a-time"))

    def test_display_timezone_falls_back_to_utc(self) -> None:
        name, tz = display_timezone(context(timezone_name=""))
        self.assertEqual(name, "UTC")
        self.assertEqual(str(tz), "UTC")
        name, tz = display_timezone(context(timezone_name="Mars/Olympus"))
        self.assertEqual(name, "UTC")

    def test_parse_chart_time_is_naive_tolerant(self) -> None:
        _, tz = display_timezone(context())
        self.assertIsNone(parse_chart_time("", tz))
        parsed = parse_chart_time("2026-08-11 10:00:00", tz)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.strftime("%Y-%m-%d %H:%M:%S"), "2026-08-11 10:00:00")

    def test_gap_indices_breaks_on_material_hole_only(self) -> None:
        _, tz = display_timezone(context())
        steady = [parse_chart_time(f"2026-08-11T10:{i:02d}:00", tz) for i in range(10)]
        self.assertEqual(gap_indices(steady), set())
        holed = steady[:5] + [parse_chart_time("2026-08-11T18:00:00", tz)]
        self.assertEqual(gap_indices(holed), {5})
        self.assertEqual(gap_indices([None, None]), set())

    def test_sar_cpu_summary_rows_accepts_every_collector_marker(self) -> None:
        rows = [{"CPU": value} for value in ("-1", "all", "ALL", "0", "1", "")]
        kept = [row["CPU"] for row in sar_cpu_summary_rows(rows)]
        self.assertEqual(kept, list(CPU_SUMMARY_VALUES))

    def test_series_values_preserve_none_and_support_transform(self) -> None:
        rows = [{"%idle": "80"}, {"%idle": "70"}, {"%idle": ""}]
        self.assertEqual(series_values(rows, "%idle"), [80.0, 70.0, None])
        self.assertEqual(busy_percent(rows), [20.0, 30.0, None])
        self.assertIsNone(finite("nan"))

    def test_summarize_reports_none_for_empty_stream(self) -> None:
        from inspection_core.charts import summarize

        summary = summarize([])
        self.assertEqual(summary["count"], 0)
        self.assertIsNone(summary["max"])


class OsSpecTests(unittest.TestCase):
    def test_four_os_specs_are_produced_with_expected_ids(self) -> None:
        specs = os_chart_specs(context({"sar_cpu": sar_rows(10)}), TAG)
        self.assertEqual([spec["chart_id"] for spec in specs], [
            "SYSTEM_CPU", "SYSTEM_MEMORY", "SYSTEM_DISK", "SYSTEM_NETWORK_REALTIME",
        ])

    def test_sar_history_wins_over_realtime_sample(self) -> None:
        ctx = context(
            {"sar_cpu": sar_rows(10), "sar_memory": [
                {"timestamp": f"2026-08-11T10:{i:02d}:00", "%memused": "50", "kbcached": "8000000"}
                for i in range(10)
            ]},
            {"system_cpu": [{"timestamp": "2026-08-11T10:00:00", "user_pct": "9"}]},
        )
        cpu = os_chart_specs(ctx, TAG)[0]
        self.assertEqual(cpu["source_scope"], "sar_history")
        self.assertIn("SAR 历史", cpu["title"])

    def test_realtime_is_used_when_history_is_too_short(self) -> None:
        ctx = context({}, {"system_cpu": [
            {"timestamp": f"2026-08-11T10:00:{i:02d}", "user_pct": "9", "system_pct": "1",
             "iowait_pct": "0.5", "steal_pct": "0"} for i in range(4)
        ]})
        cpu = os_chart_specs(ctx, TAG)[0]
        self.assertEqual(cpu["source_scope"], "realtime_snapshot")
        self.assertIn("现场短时采样", cpu["title"])

    def test_network_drops_warmup_point_and_loopback(self) -> None:
        rows = [
            {"interface": "eth0", "timestamp": f"2026-08-11T10:00:{i:02d}",
             "rx_bytes_per_sec": "1048576", "tx_bytes_per_sec": "0"} for i in range(5)
        ] + [{"interface": "lo", "timestamp": "2026-08-11T10:00:00",
              "rx_bytes_per_sec": "999", "tx_bytes_per_sec": "999"}]
        spec = os_chart_specs(context({}, {"system_network": rows}), TAG)[3]
        self.assertIn("eth0", spec["title"])
        self.assertEqual(spec["warmup_points_excluded"], 1)
        self.assertEqual(len(spec["x"]), 4)

    def test_disk_spec_keeps_only_the_busiest_device(self) -> None:
        rows = [
            {"DEV": "sda", "timestamp": f"2026-08-11T10:{i:02d}:00", "%util": "5",
             "await": "1", "rkB/s": "10", "wkB/s": "20"} for i in range(4)
        ] + [
            {"DEV": "sdb", "timestamp": f"2026-08-11T10:{i:02d}:00", "%util": "60",
             "await": "9", "rkB/s": "100", "wkB/s": "200"} for i in range(4)
        ]
        spec = os_chart_specs(context({"sar_disk": rows}), TAG)[2]
        self.assertIn("sdb", spec["title"])
        self.assertEqual(len(spec["panels"]), 3)
        self.assertEqual(len(spec["x"]), 4)

    def test_spec_without_points_has_no_usable_data(self) -> None:
        spec = os_chart_specs(context(), TAG)[0]
        self.assertFalse(spec_has_data(spec))


class OsRenderTests(unittest.TestCase):
    def test_render_writes_png_and_names_the_renderer(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            ctx = context({"sar_cpu": sar_rows(10)})
            charts = generate_os_charts(ctx, TAG, output / "charts", output)
            generated = [chart for chart in charts if chart["status"] == "generated"]
            self.assertTrue(generated, charts)
            for chart in generated:
                self.assertIn(chart["renderer"], {"matplotlib", "pillow_fallback"})
                self.assertTrue((output / chart["file"]).exists(), chart["file"])

    def test_render_skips_spec_without_enough_points(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            spec = os_chart_specs(context(), TAG)[0]
            result = render_spec(context(), spec, output / "charts", output)
            self.assertEqual(result["status"], "skipped")
            self.assertEqual(result["reason"], "insufficient_data_points")


class OsSystemCheckTests(unittest.TestCase):
    def test_normalizes_nested_and_flat_collector_shapes(self) -> None:
        nested = normalize_os_metrics({
            "system_history": {"cpu_busy_pct": {"average": 10.0, "max": 40.0}},
            "system_realtime": {"cpu_busy_percent": {"average": 5.0, "max": 20.0}},
        })
        self.assertEqual(nested["shape"], "nested")
        self.assertEqual(nested["history"]["cpu_busy"]["max"], 40.0)
        self.assertEqual(nested["realtime"]["cpu_busy"]["max"], 20.0)

        flat = normalize_os_metrics({"cpu_busy_max": 95.0, "iowait_max": 3.0})
        self.assertEqual(flat["shape"], "flat")
        self.assertEqual(flat["flat"]["cpu_busy"]["max"], 95.0)
        self.assertEqual(flat["flat"]["memory_used"]["max"], None)

    def test_normalization_is_idempotent(self) -> None:
        once = normalize_os_metrics({"cpu_busy_max": 1.0})
        self.assertIs(normalize_os_metrics(once), once)

    def test_thresholds_are_single_sourced(self) -> None:
        self.assertEqual(DEFAULT_THRESHOLDS["iowait_peak_warning"], 20.0)
        self.assertEqual(DEFAULT_THRESHOLDS["memory_usage_warning"], 90.0)
        self.assertEqual(MEMORY_CRITERION, "max")
        self.assertEqual(CANONICAL_RULE_IDS["iowait_pressure"], "COMMON.SYSTEM.IOWAIT_PRESSURE")

    def test_iowait_peak_of_25_triggers(self) -> None:
        block = normalize_os_metrics({"iowait_max": 25.0})["flat"]
        verdict = next(
            item for item in os_pressure_verdicts(block)
            if item["metric"] == "cpu_iowait"
        )
        self.assertTrue(verdict["triggered"])
        self.assertEqual(verdict["threshold"], 20.0)

    def test_memory_judged_on_peak_not_average(self) -> None:
        # 平均 50%（远低于阈值）但峰值 95%——按 average 判会漏报，按 max 判必须触发。
        block = normalize_os_metrics({
            "system_history": {"memory_used_pct": {"average": 50.0, "max": 95.0}},
        })["history"]
        verdict = next(
            item for item in os_pressure_verdicts(block)
            if item["metric"] == "memory_used"
        )
        self.assertEqual(verdict["criterion"], "max")
        self.assertEqual(verdict["value"], 95.0)
        self.assertTrue(verdict["triggered"])

    def test_missing_metric_is_unavailable_not_passed(self) -> None:
        verdicts = os_pressure_verdicts(normalize_os_metrics({})["flat"])
        for verdict in verdicts:
            self.assertFalse(verdict["available"])
            self.assertFalse(verdict["triggered"])
            self.assertIsNone(verdict["value"])

    def test_retired_rule_ids_resolve_to_canonical(self) -> None:
        self.assertEqual(
            canonical_rule_id("oracle", "ORA.SYSTEM.CPU_PRESSURE"),
            "COMMON.SYSTEM.CPU_PRESSURE",
        )
        self.assertEqual(
            canonical_rule_id("oracle", "ORA.SYSTEM.IOWAIT_PRESSURE"),
            "COMMON.SYSTEM.IOWAIT_PRESSURE",
        )
        self.assertEqual(
            canonical_rule_id("postgresql", "COMMON.SYSTEM.MEMORY"),
            "COMMON.SYSTEM.MEMORY_PRESSURE",
        )
        # MySQL never had a different spelling — the lookup must be a no-op.
        self.assertEqual(
            canonical_rule_id("mysql", "COMMON.SYSTEM.MEMORY_PRESSURE"),
            "COMMON.SYSTEM.MEMORY_PRESSURE",
        )
        # 已收口：映射只有一个方向，不存在把 canonical 名翻译回插件的写法。
        self.assertNotIn("legacy_rule_id", dir(__import__(
            "inspection_core.system_checks", fromlist=["x"])))

    def test_every_retired_spelling_maps_to_a_live_rule_id(self) -> None:
        live = set(CANONICAL_RULE_IDS.values())
        for database, mapping in RETIRED_RULE_IDS.items():
            for retired, canonical in mapping.items():
                self.assertIn(canonical, live, f"{database}: {retired} → {canonical} 不是现行规则")

    def test_pressure_verdicts_carry_label_and_fact(self) -> None:
        block = normalize_os_metrics({"iowait_max": 25.0})["flat"]
        verdict = next(v for v in os_pressure_verdicts(block) if v["metric"] == "cpu_iowait")
        self.assertEqual(verdict["rule_key"], "iowait_pressure")
        self.assertEqual(verdict["label"], OS_CHECK_LABELS["cpu_iowait"])
        self.assertEqual(verdict["fact"], "IO wait 峰值：25.0%")
        missing = next(
            v for v in os_pressure_verdicts(normalize_os_metrics({})["flat"])
            if v["metric"] == "cpu_iowait"
        )
        self.assertIsNone(missing["fact"])

    def test_include_filter_keeps_report_order(self) -> None:
        block = normalize_os_metrics({"cpu_busy_max": 1.0})["flat"]
        keys = [v["rule_key"] for v in os_pressure_verdicts(block)]
        self.assertEqual(keys, list(OS_RULE_KEYS))
        # MySQL / Oracle 不比磁盘 util，PG 比四条。
        subset = [v["rule_key"] for v in os_pressure_verdicts(
            block, include=("memory_pressure", "cpu_pressure"))]
        self.assertEqual(subset, ["cpu_pressure", "memory_pressure"])

    def test_pressure_report_picks_the_window_it_judges(self) -> None:
        metrics = {
            "system_history": {"cpu_busy_pct": {"average": 10.0, "max": 30.0}},
            "system_realtime": {"cpu_busy_pct": {"average": 80.0, "max": 99.0}},
            "sampling_context": {"history": {"usable_for_trend_rules": True}},
        }
        source, verdicts = os_pressure_report(metrics)
        cpu = next(v for v in verdicts if v["metric"] == "cpu_busy")
        self.assertEqual(source["scope"], "history")
        # 判据必须来自被选中的那个窗口，不能是实时窗口的 99%。
        self.assertEqual(cpu["value"], 30.0)
        self.assertFalse(cpu["triggered"])
        self.assertEqual(source["reason"], "使用有效 SAR 历史")

    def test_source_reason_distinguishes_short_sar_coverage(self) -> None:
        # 2 小时覆盖是 24 小时窗口的 8.3%，不足以用"日"的口径下结论。
        short = os_source({
            "cpu_busy_max": 10.0, "sar_effective_coverage_hours": 2.0,
            "system_metric_source": "sar+realtime",
        })
        self.assertEqual(short["label"], "SAR 历史 + 实时采样")
        self.assertEqual(short["confidence"], 0.65)
        self.assertLess(short["coverage_ratio"], 0.8)
        self.assertIn("覆盖不足", short["reason"])
        live = os_source({
            "cpu_busy_max": 10.0, "sar_effective_coverage_hours": 20.0,
            "system_metric_source": "sar+realtime",
        })
        self.assertEqual(live["confidence"], 0.9)
        self.assertGreaterEqual(live["coverage_ratio"], 0.8)
        # 没有 SAR 文件时是纯实时窗口，不涉及覆盖比例。
        realtime_only = os_source({
            "cpu_busy_max": 10.0, "sar_effective_coverage_hours": 0.0,
            "system_metric_source": "realtime",
        })
        self.assertEqual(realtime_only["label"], "现场短时采样")
        self.assertEqual(realtime_only["confidence"], 0.65)

    def test_history_source_wins_when_sar_is_usable(self) -> None:
        metrics = {
            "system_history": {"cpu_busy_pct": {"average": 10.0, "max": 30.0}},
            "system_realtime": {"cpu_busy_pct": {"average": 80.0, "max": 99.0}},
            "sampling_context": {"history": {"usable_for_trend_rules": True}},
        }
        source = os_source(metrics)
        self.assertEqual(source["scope"], "history")
        self.assertEqual(source["confidence"], 0.9)
        self.assertEqual(source["block"]["cpu_busy"]["max"], 30.0)

    def test_realtime_source_is_low_confidence(self) -> None:
        metrics = {
            "system_realtime": {"cpu_busy_pct": {"average": 80.0, "max": 99.0}},
            "sampling_context": {"history": {"usable_for_trend_rules": False}},
        }
        source = os_source(metrics)
        self.assertEqual(source["scope"], "realtime")
        self.assertLess(source["confidence"], 0.9)

    def test_resource_rows_are_built_for_nested_shape(self) -> None:
        metrics = {
            "system_realtime": {
                "cpu_busy_pct": {"average": 12.0, "max": 20.0},
                "cpu_iowait_pct": {"average": 1.0, "max": 3.0},
                "memory_used_pct": {"average": 55.0, "max": 60.0},
                "memory_available_bytes": {"average": 8 * 1024 ** 3, "min": 6 * 1024 ** 3},
                "swap_used_bytes": {"average": 1024 ** 3, "max": 2 * 1024 ** 3},
            },
        }
        rows = os_resource_rows(metrics)
        self.assertEqual(list(rows[0]), ["指标", "平均值", "峰值", "数据源"])
        self.assertEqual(rows[0]["指标"], "CPU 使用率")
        self.assertEqual(rows[0]["峰值"], "20.0%")
        labels = [row["指标"] for row in rows]
        self.assertIn("可用内存", labels)
        self.assertIn("已用 Swap", labels)

    def test_byte_rows_are_absent_when_collector_has_no_byte_metrics(self) -> None:
        metrics = {
            "system_history": {
                "cpu_busy_pct": {"average": 12.0, "max": 20.0},
                "memory_used_pct": {"average": 55.0, "max": 60.0},
            },
        }
        labels = [row["指标"] for row in os_resource_rows(metrics)]
        self.assertNotIn("可用内存", labels)
        self.assertNotIn("已用 Swap", labels)

    def test_flat_shape_disk_and_network_tables_are_empty(self) -> None:
        flat = {"cpu_busy_max": 95.0}
        self.assertEqual(os_disk_rows(flat), [])
        self.assertEqual(os_network_rows(flat), [])

    def test_format_bytes(self) -> None:
        self.assertIsNone(format_bytes(None))
        self.assertEqual(format_bytes(512), "512.00 B")
        self.assertEqual(format_bytes(16 * 1024 ** 3), "16.00 GB")


class MySQLCutoverTests(unittest.TestCase):
    """MySQL 已切公共层，这组测试防止 OS 图规格重新在插件里长出来。"""

    def test_mysql_owns_only_its_two_database_charts(self) -> None:
        from plugins.mysql.charts import MySQLChartProvider

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            provider = MySQLChartProvider(root / "output", root / "output" / "charts")
            ctx = context({"sar_cpu": sar_rows(10), "sar_memory": memory_rows(10),
                           "sar_disk": disk_rows(4)})
            specs = provider._chart_specs(ctx, {"mysql_realtime": {"derived_rate_series": []}})
            self.assertEqual([spec["chart_id"] for spec in specs], [
                "SYSTEM_CPU", "SYSTEM_MEMORY", "SYSTEM_DISK", "SYSTEM_NETWORK_REALTIME",
                "MYSQL_QPS_TPS", "MYSQL_THREADS",
            ])
            # 前四张必须是公共层原样输出，插件不得改写标题、面板或数据源。
            self.assertEqual(specs[:4], os_chart_specs(ctx, TAG))

    def test_mysql_no_longer_declares_os_chart_helpers(self) -> None:
        from plugins.mysql import charts

        for name in ("_render_matplotlib", "_render_pillow", "_pillow_fonts", "_has_data",
                     "_display_timezone", "_parse_chart_time", "_finite", "_gap_indices",
                     "duration_ms"):
            self.assertFalse(hasattr(charts, name), f"{name} 应已下沉到 inspection_core.charts")

    def test_mysql_private_chart_style_is_removed(self) -> None:
        self.assertFalse(
            (Path(__file__).resolve().parents[1] / "plugins/mysql/chart_style.py").exists(),
            "MySQL 配色必须从 inspection_core.charts.style 读取，不得保留私有副本",
        )


class CollectorDivergenceTests(unittest.TestCase):
    """两库采集列名/元数据不一致时，公共层必须归一，而不是让某个库画出空白线。"""

    def test_sar_cpu_rows_accept_sysstat_short_column_names(self) -> None:
        # Oracle 采集脚本写 %usr/%sys，MySQL 走 sadf 原样列 %user/%system。
        oracle_rows = [{"CPU": "-1", "timestamp": "t", "%usr": "4.02", "%sys": "0.79",
                        "%iowait": "0.45", "%steal": "0.00", "%idle": "93.88"}]
        normalized = sar_cpu_summary_rows(oracle_rows)
        self.assertEqual(normalized[0]["%user"], "4.02")
        self.assertEqual(normalized[0]["%system"], "0.79")

        mysql_rows = [{"CPU": "-1", "timestamp": "t", "%user": "10", "%system": "3"}]
        unchanged = sar_cpu_summary_rows(mysql_rows)
        self.assertIs(unchanged[0], mysql_rows[0], "MySQL 行不得被复制或改写")

    def test_cpu_spec_plots_user_and_system_for_short_column_collector(self) -> None:
        rows = [
            {"CPU": "-1", "timestamp": f"2026-08-09 00:3{i}:09 UTC",
             "%usr": str(10 + i), "%sys": str(3 + i), "%iowait": "1", "%steal": "0"}
            for i in range(4)
        ]
        panels = os_chart_specs(context({"sar_cpu": rows}), TAG)[0]["panels"]
        series = dict((label, values) for label, values in panels[0][2])
        self.assertEqual(series["用户"], [10.0, 11.0, 12.0, 13.0])
        self.assertEqual(series["系统"], [3.0, 4.0, 5.0, 6.0])

    def test_display_timezone_falls_back_to_the_data_offset(self) -> None:
        # Oracle 的扁平 snapshot 没有 time_evidence，实时行自带 +08:00。
        ctx = PackageContext(
            source=Path("synthetic.tar.gz"), root=Path("."),
            snapshot={"instance_tag": TAG},
            status={"items": []}, manifest={}, integrity={"status": "ok"},
            history={},
            timeseries={"system_cpu": [{"timestamp": "2026-08-10T20:45:55+08:00"}]},
        )
        name, tz = display_timezone(ctx)
        self.assertEqual(name, "UTC+08:00")
        self.assertEqual(tz.utcoffset(None).total_seconds(), 8 * 3600)

    def test_display_timezone_stays_utc_when_data_carries_no_offset(self) -> None:
        ctx = PackageContext(
            source=Path("synthetic.tar.gz"), root=Path("."),
            snapshot={"instance_tag": TAG},
            status={"items": []}, manifest={}, integrity={"status": "ok"},
            history={"sar_cpu": sar_rows(3)}, timeseries={},
        )
        name, _ = display_timezone(ctx)
        self.assertEqual(name, "UTC")

    def test_recorded_timezone_still_wins_over_data_offset(self) -> None:
        ctx = context({"sar_cpu": sar_rows(3)}, {"system_cpu": [
            {"timestamp": "2026-08-10T20:45:55+08:00"}]})
        self.assertEqual(display_timezone(ctx)[0], "Asia/Shanghai")

    def test_memory_total_prefers_snapshot_then_realtime_then_sar(self) -> None:
        from inspection_core.system_checks import memory_total_kb

        self.assertEqual(memory_total_kb(context()), 16 * 1024 ** 2)
        realtime_only = PackageContext(
            source=Path("synthetic.tar.gz"), root=Path("."), snapshot={},
            status={"items": []}, manifest={}, integrity={"status": "ok"}, history={},
            timeseries={"system_memory": [{"mem_total_bytes": str(4 * 1024 ** 3)}]},
        )
        self.assertEqual(memory_total_kb(realtime_only), 4 * 1024 ** 2)
        sar_only = PackageContext(
            source=Path("synthetic.tar.gz"), root=Path("."), snapshot={},
            status={"items": []}, manifest={}, integrity={"status": "ok"}, timeseries={},
            history={"sar_memory": [{"kbmemused": "3152660", "%memused": "79.01"}]},
        )
        self.assertAlmostEqual(memory_total_kb(sar_only), 3152660 / 0.7901, places=3)
        self.assertIsNone(memory_total_kb(PackageContext(
            source=Path("synthetic.tar.gz"), root=Path("."), snapshot={},
            status={"items": []}, manifest={}, integrity={"status": "ok"},
            history={}, timeseries={},
        )))

    def test_memory_spec_omits_cached_series_when_total_is_unknown(self) -> None:
        ctx = PackageContext(
            source=Path("synthetic.tar.gz"), root=Path("."), snapshot={"instance_tag": TAG},
            status={"items": []}, manifest={}, integrity={"status": "ok"}, timeseries={},
            history={"sar_memory": [
                {"timestamp": f"2026-08-11T10:0{i}:00", "%memused": "50", "kbmemused": ""}
                for i in range(3)
            ]},
        )
        panels = os_chart_specs(ctx, TAG)[1]["panels"]
        labels = [label for label, _ in panels[0][2]]
        self.assertNotIn("缓存", labels, "总量未知时不能画缓存百分比")
        self.assertIn("已用", labels)


class OracleCutoverTests(unittest.TestCase):
    """Oracle 的 OS 四张已切公共层，这组测试防止它在插件里重写一遍。"""

    def test_oracle_os_charts_come_from_the_shared_layer(self) -> None:
        from plugins.oracle.charts import OS_CHART_IDS, OracleChartProvider

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            provider = OracleChartProvider(root / "charts")
            ctx = context(
                {"sar_cpu": sar_rows(10), "sar_memory": memory_rows(10),
                 "sar_disk": disk_rows(4)},
                {"system_network": [
                    {"interface": "eth0", "timestamp": f"2026-08-11T10:00:{i:02d}",
                     "rx_bytes_per_sec": "1048576", "tx_bytes_per_sec": "524288"}
                    for i in range(5)
                ]},
            )
            records = provider._os_charts(ctx, TAG)
            self.assertEqual([record["chart_id"] for record in records], list(OS_CHART_IDS))
            self.assertEqual(
                [record["chart_id"] for record in records],
                [spec["chart_id"] for spec in os_chart_specs(ctx, TAG)],
            )

    def test_oracle_no_longer_declares_os_chart_helpers(self) -> None:
        from inspection_core.charts import style as common_style
        from plugins.oracle import charts

        for name in ("_time_line", "_duration_ms", "_parse_ts"):
            self.assertFalse(hasattr(charts, name), f"{name} 应已下沉到 inspection_core.charts")
        # 配色必须是公共层那一份（同一对象），不能是插件自建的同名副本。
        self.assertIs(charts.COLOR_MAP, common_style.COLOR_MAP)

    def test_oracle_private_chart_style_is_removed(self) -> None:
        self.assertFalse(
            (Path(__file__).resolve().parents[1] / "plugins/oracle/chart_style.py").exists(),
            "Oracle 配色必须从 inspection_core.charts.style 读取，不得保留私有副本",
        )


class PostgreSQLCutoverTests(unittest.TestCase):
    """PG 的 OS 四张已切公共层，这组测试防止私有 SAR 解析与画图实现复活。"""

    def test_postgresql_os_charts_come_from_the_shared_layer(self) -> None:
        from plugins.postgresql.charts import PostgreSQLChartProvider

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            provider = PostgreSQLChartProvider(root, root / "charts")
            ctx = context(
                {"sar_cpu": sar_rows(10), "sar_memory": memory_rows(10),
                 "sar_disk": disk_rows(4)},
                {"system_network": [
                    {"interface": "ens160", "timestamp": f"2026-08-11T10:00:{i:02d}",
                     "rx_bytes_per_sec": "1048576", "tx_bytes_per_sec": "524288"}
                    for i in range(5)
                ]},
            )
            records = provider.generate_charts(ctx, {})
        self.assertEqual(
            [record["chart_id"] for record in records],
            [spec["chart_id"] for spec in os_chart_specs(ctx, TAG)],
            "PG 的系统图必须逐张等于公共规格，且不得多画一张",
        )
        self.assertEqual([record["chart_id"] for record in records], [
            "SYSTEM_CPU", "SYSTEM_MEMORY", "SYSTEM_DISK", "SYSTEM_NETWORK_REALTIME",
        ])

    def test_postgresql_keeps_its_two_database_charts_tagged(self) -> None:
        from plugins.postgresql.charts import PostgreSQLChartProvider

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            provider = PostgreSQLChartProvider(root, root / "charts")
            ctx = context({}, {
                "pg_activity": [
                    {"timestamp": f"2026-08-11T10:00:{i:02d}", "total_sessions": "10",
                     "active_sessions": "2", "idle_in_xact": "0"} for i in range(3)],
                "pg_stats": [
                    {"timestamp": f"2026-08-11T10:00:{i:02d}", "xact_commit": str(100 + i),
                     "xact_rollback": "1"} for i in range(3)],
            })
            records = provider.generate_charts(ctx, {})
        pg_records = [r for r in records if r["chart_id"].startswith("pg_")]
        if has_matplotlib():
            self.assertEqual([r["chart_id"] for r in pg_records], ["pg_sessions", "pg_stats"])
            for record in pg_records:
                # 多实例共用 charts/ 目录，文件名不带实例前缀会互相覆盖。
                self.assertTrue(record["file"].endswith(f"_{record['chart_id']}.png"),
                                record["file"])
        else:
            self.assertEqual([r["chart_id"] for r in records], ["PG_CHARTS"])

    def test_postgresql_no_longer_declares_sar_parsers(self) -> None:
        from plugins.postgresql import parsers

        for name in ("_read_sar_csv", "_parse_sar_timestamp", "_parse_sar_cpu",
                     "_parse_sar_memory", "_parse_sar_metric_by_metric",
                     "_parse_sar_disk", "_parse_sar_network",
                     "_effective_sar_coverage_hours"):
            self.assertFalse(hasattr(parsers, name), f"{name} 应已下沉到 inspection_core")

    def test_postgresql_analyzer_has_no_dead_sar_aliases(self) -> None:
        from plugins.postgresql import analyzer

        for name in ("_parse_sar_cpu", "_read_sar_csv", "_parse_df_pt", "_parse_free_b"):
            self.assertFalse(hasattr(analyzer, name), f"{name} 是无效的转发别名，不得保留")

    def test_postgresql_private_chart_style_is_removed(self) -> None:
        self.assertFalse(
            (Path(__file__).resolve().parents[1] / "plugins/postgresql/chart_style.py").exists(),
            "PG 配色必须从 inspection_core.charts.style 读取，不得保留私有副本",
        )

    def test_postgresql_reads_history_with_the_sadf_parser(self) -> None:
        source = (Path(__file__).resolve().parents[1] /
                  "plugins/postgresql/package_adapter.py").read_text(encoding="utf-8")
        self.assertIn("parse_sadf(path)", source)
        self.assertNotIn("parse_csv(path)", source.split("history_dir")[-1][:200],
                         "history/sar_*.csv 是 sadf 分号格式，用逗号 CSV 解析会得到空表")

    def test_sar_history_peak_outranks_the_short_realtime_sample(self) -> None:
        """SAR 历史峰值必须参与合并。

        旧实现按 label == "idle" 取历史 busy，而解析结果里根本没有 idle 这一条，
        于是 CPU 历史峰值从未进入 cpu_busy_max —— 只剩 30 秒实时窗口的读数。
        """
        from plugins.postgresql.metrics import PostgreSQLMetricProvider

        ctx = context(
            {"sar_cpu": [
                {"CPU": "-1", "timestamp": f"2026-08-09 0{i}:30:09 UTC",
                 "%user": "40", "%nice": "0", "%system": "10", "%iowait": "1",
                 "%steal": "0", "%idle": str(40 - i)}          # busy = 60 + i
                for i in range(3)
            ]},
            {"system_cpu": [
                {"timestamp": "2026-08-10T14:00:48+08:00", "busy_pct": "8.0",
                 "iowait_pct": "0.1", "idle_pct": "92", "user_pct": "5",
                 "system_pct": "3", "steal_pct": "0"},
            ]},
        )
        provider = PostgreSQLMetricProvider(lambda _ctx: {"score": 100.0})
        metrics = provider.derive_metrics(ctx)
        self.assertAlmostEqual(metrics["cpu_busy_max"], 62.0)
        # 三点、间隔恰好一小时：全部计入覆盖，两段共 2 小时。
        self.assertEqual(metrics["sar_effective_coverage_hours"], 2.0)


class OsThresholdOwnershipTests(unittest.TestCase):
    """OS 阈值与规则名只能有一份定义（inspection_core.system_checks）。

    这组测试是第⑤步收口的守门人：任何插件再把 OS 阈值写回规则包，或者再冒出一个
    第四种 rule-id 拼法，都必须在这里失败。
    """

    ROOT = Path(__file__).resolve().parents[1]
    PACKS = {
        "mysql": "plugins/mysql/inspection_rules.json",
        "oracle": "plugins/oracle/inspection_rules_oracle.json",
        "postgresql": "plugins/postgresql/inspection_rules.json",
    }
    SOURCES = {
        "mysql": "plugins/mysql/rules.py",
        "oracle": "plugins/oracle/oracle_rules.py",
        "postgresql": "plugins/postgresql/rules.py",
    }
    OS_THRESHOLD_KEYS = {
        "cpu_peak_warning", "iowait_peak_warning", "memory_usage_warning",
        "disk_util_warning", "filesystem_usage_critical",
    }
    RETIRED_IDS = {
        "ORA.SYSTEM.CPU_PRESSURE", "ORA.SYSTEM.IOWAIT_PRESSURE",
        "ORA.SYSTEM.MEMORY_PRESSURE", "ORA.SYSTEM.TIME_SYNC",
        "ORA.CAPACITY.FILESYSTEM_USAGE",
        "ORA.SYSTEM.SAR_CPU_PEAK", "ORA.SYSTEM.SAR_IOWAIT_PEAK",
        "COMMON.SYSTEM.CPU", "COMMON.SYSTEM.IOWAIT", "COMMON.SYSTEM.MEMORY",
    }
    # 只匹配完整的规则 id，避免 "COMMON.SYSTEM.CPU" 命中 "COMMON.SYSTEM.CPU_PRESSURE"
    ID_TOKEN = re.compile(r"(?:COMMON|MYSQL|ORA|PG)\.[A-Z][A-Z0-9_.]*")

    def _text(self, relative: str) -> str:
        return (self.ROOT / relative).read_text(encoding="utf-8")

    def _pack(self, database: str) -> dict:
        import json

        return json.loads(self._text(self.PACKS[database]))

    def test_no_plugin_redeclares_an_os_threshold(self) -> None:
        for database in self.PACKS:
            config = self._pack(database)
            declared = set(config.get("globals", {}))
            for rule in config.get("rules", {}).values():
                declared |= set(rule.get("threshold", {}) or {})
            self.assertEqual(
                declared & self.OS_THRESHOLD_KEYS, set(),
                f"{database} 规则包又声明了 OS 阈值；它们归 DEFAULT_THRESHOLDS 管",
            )

    def test_no_plugin_keeps_a_retired_rule_id(self) -> None:
        for database in self.PACKS:
            found = set(self.ID_TOKEN.findall(self._text(self.PACKS[database])))
            self.assertEqual(found & self.RETIRED_IDS, set(),
                             f"{database} 规则包仍含旧拼写")
            found = set(self.ID_TOKEN.findall(self._text(self.SOURCES[database])))
            self.assertEqual(found & self.RETIRED_IDS, set(),
                             f"{database} 规则引擎仍含旧拼写")

    def test_plugin_sources_import_the_shared_judging_entry_point(self) -> None:
        for database, relative in self.SOURCES.items():
            self.assertIn("os_pressure_report", self._text(relative),
                          f"{database} 未走公共判定")

    def test_plugin_sources_do_not_recompute_the_window(self) -> None:
        """数据窗口与置信度不得再出现在插件里。"""
        for database, relative in self.SOURCES.items():
            text = self._text(relative)
            for marker in ("usable_for_trend_rules", "system_history", "system_realtime"):
                self.assertNotIn(marker, text, f"{database} 仍在自选数据窗口（{marker}）")
            self.assertNotIn("0.65", text, f"{database} 仍在自定置信度")

    def test_os_rule_ids_ship_the_canonical_spelling(self) -> None:
        for database in self.PACKS:
            rules = self._pack(database)["rules"]
            self.assertIn("COMMON.SYSTEM.CPU_PRESSURE", rules, database)
            self.assertIn("COMMON.SYSTEM.IOWAIT_PRESSURE", rules, database)
            self.assertIn("COMMON.SYSTEM.MEMORY_PRESSURE", rules, database)

    def test_oracle_dead_sar_rules_are_gone(self) -> None:
        """ORA.SYSTEM.SAR_* 从未被任何代码路径上报，留着只会让人调到无效阈值。"""
        rules = self._pack("oracle")["rules"]
        self.assertNotIn("ORA.SYSTEM.SAR_CPU_PEAK", rules)
        self.assertNotIn("ORA.SYSTEM.SAR_IOWAIT_PEAK", rules)


class MysqlSystemResourceWiringTests(unittest.TestCase):
    """MySQL 的 OS 三条已切公共判定。

    自备采集包缺失，`test_mysql_providers` 那几条跑不起来，所以这里用合成 metrics
    直接把 `_check_system_resources` 跑通：只要判定还留在插件里，这组就会失败。
    """

    def _engine(self):
        from plugins.mysql.rules import RuleEngine

        engine = RuleEngine()
        engine._evaluations = []
        return engine

    def _metrics(self, *, usable_history: bool = True, **overrides) -> dict:
        metrics = {
            "scope": {"database_target_is_local": True},
            "system_history": {
                "cpu_busy_percent": {"average": 10.0, "max": 30.0},
                "cpu_iowait_percent": {"average": 1.0, "max": 3.0},
                "memory_used_percent": {"average": 50.0, "max": 95.0},
            },
            "system_realtime": {
                "cpu_busy_percent": {"average": 5.0, "max": 20.0},
                "cpu_iowait_percent": {"average": 0.5, "max": 1.0},
                "memory_used_percent": {"average": 40.0, "max": 45.0},
            },
            "sampling_context": {"history": {"usable_for_trend_rules": usable_history}},
        }
        metrics.update(overrides)
        return metrics

    def _run(self, metrics: dict) -> dict:
        engine = self._engine()
        engine._check_system_resources(context(), metrics)
        return {item.rule_id: item for item in engine._evaluations}

    def test_only_the_three_mysql_os_checks_are_evaluated(self) -> None:
        evaluations = self._run(self._metrics())
        self.assertEqual(list(evaluations), [
            "COMMON.SYSTEM.CPU_PRESSURE",
            "COMMON.SYSTEM.IOWAIT_PRESSURE",
            "COMMON.SYSTEM.MEMORY_PRESSURE",
        ])
        self.assertNotIn("COMMON.SYSTEM.DISK_UTIL", evaluations)

    def test_memory_is_judged_on_the_history_peak_not_the_average(self) -> None:
        # 历史窗口均值 50%（远低于 90）但峰值 95%——按 average 判会漏报。
        evaluations = self._run(self._metrics())
        memory = evaluations["COMMON.SYSTEM.MEMORY_PRESSURE"]
        self.assertEqual(memory.status, "triggered")
        self.assertAlmostEqual(memory.confidence, 0.9)
        # CPU / IO wait 在同一个窗口里都没过线。
        self.assertEqual(evaluations["COMMON.SYSTEM.CPU_PRESSURE"].status, "passed")
        self.assertEqual(evaluations["COMMON.SYSTEM.IOWAIT_PRESSURE"].status, "passed")

    def test_realtime_window_is_used_when_history_is_unusable(self) -> None:
        evaluations = self._run(self._metrics(usable_history=False))
        memory = evaluations["COMMON.SYSTEM.MEMORY_PRESSURE"]
        self.assertEqual(memory.status, "passed")          # 实时峰值 45%
        self.assertAlmostEqual(memory.confidence, 0.65)
        self.assertIn("短时样本", memory.reason)

    def test_iowait_threshold_comes_from_the_shared_layer(self) -> None:
        metrics = self._metrics()
        metrics["system_history"]["cpu_iowait_percent"] = {"average": 1.0, "max": 25.0}
        evaluations = self._run(metrics)
        iowait = evaluations["COMMON.SYSTEM.IOWAIT_PRESSURE"]
        self.assertEqual(iowait.status, "triggered")
        self.assertEqual(DEFAULT_THRESHOLDS["iowait_peak_warning"], 20.0)

    def test_missing_os_block_is_not_evaluated_not_passed(self) -> None:
        metrics = self._metrics()
        metrics["system_history"] = {}
        metrics["system_realtime"] = {}
        evaluations = self._run(metrics)
        for evaluation in evaluations.values():
            self.assertEqual(evaluation.status, "not_evaluated")


if __name__ == "__main__":
    unittest.main()
