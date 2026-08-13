"""MySQL chart provider.

Charts are presentation artifacts only.  They consume normalized evidence,
prefer usable SAR history for host trends, and never alter analyzer facts.
"""

from __future__ import annotations

import math
import re
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from inspection_core import PackageContext, safe_float


def duration_ms(start_ns: int) -> int:
    return int((time.monotonic_ns() - start_ns) / 1_000_000)


def _display_timezone(ctx: PackageContext) -> tuple[str, Any]:
    timezone_name = str(ctx.snapshot.get("time_evidence", {}).get("timezone") or "UTC")
    try:
        return timezone_name, ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        return "UTC", timezone.utc


def _parse_chart_time(value: Any, display_tz: Any) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    normalized = re.sub(r"\s+UTC$", "+00:00", raw, flags=re.IGNORECASE)
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=display_tz)
    return parsed.astimezone(display_tz)


def _finite(value: Any) -> float | None:
    parsed = safe_float(value)
    return parsed if parsed is not None and math.isfinite(parsed) else None


def _gap_indices(parsed: list[datetime | None]) -> set[int]:
    """Return points that begin a new segment after a material time gap."""
    if not parsed or any(value is None for value in parsed):
        return set()
    intervals = [
        (parsed[index] - parsed[index - 1]).total_seconds()  # type: ignore[operator]
        for index in range(1, len(parsed))
        if (parsed[index] - parsed[index - 1]).total_seconds() > 0  # type: ignore[operator]
    ]
    if not intervals:
        return set()
    threshold = max(3600.0, statistics.median(intervals) * 4)
    return {
        index for index in range(1, len(parsed))
        if (parsed[index] - parsed[index - 1]).total_seconds() > threshold  # type: ignore[operator]
    }


class MySQLChartProvider:
    """Generate six report charts with Matplotlib and a CJK-safe Pillow fallback."""

    def __init__(self, output: Path, charts_dir: Path) -> None:
        self.output = output
        self.charts_dir = charts_dir

    @staticmethod
    def _series(label: str, values: list[Any]) -> tuple[str, list[float | None]]:
        return label, [_finite(value) for value in values]

    def _chart_specs(self, ctx: PackageContext, metrics: dict[str, Any]) -> list[dict[str, Any]]:
        instance_tag = ctx.snapshot["instance_identity"]["instance_tag"]
        specs: list[dict[str, Any]] = []

        sar_cpu = [row for row in ctx.history.get("sar_cpu", []) if row.get("CPU") == "-1"]
        if len(sar_cpu) >= 2:
            cpu_rows, cpu_scope = sar_cpu, "sar_history"
            cpu_title = "CPU 使用率（SAR 历史）"
            cpu_series = [
                self._series("用户", [row.get("%user") for row in cpu_rows]),
                self._series("系统", [row.get("%system") for row in cpu_rows]),
                self._series("IO 等待", [row.get("%iowait") for row in cpu_rows]),
                self._series("虚拟化抢占", [row.get("%steal") for row in cpu_rows]),
            ]
        else:
            cpu_rows, cpu_scope = ctx.timeseries.get("system_cpu", []), "realtime_snapshot"
            cpu_title = "CPU 使用率（现场短时采样）"
            cpu_series = [
                self._series("用户", [row.get("user_pct") for row in cpu_rows]),
                self._series("系统", [row.get("system_pct") for row in cpu_rows]),
                self._series("IO 等待", [row.get("iowait_pct") for row in cpu_rows]),
                self._series("虚拟化抢占", [row.get("steal_pct") for row in cpu_rows]),
            ]
        specs.append({
            "chart_id": "SYSTEM_CPU", "title": cpu_title,
            "x": [row.get("timestamp", "") for row in cpu_rows],
            "panels": [("", "%", cpu_series)], "source_scope": cpu_scope,
            "filename": f"{instance_tag}_cpu.png",
        })

        sar_mem = ctx.history.get("sar_memory", [])
        if len(sar_mem) >= 2:
            mem_rows, mem_scope = sar_mem, "sar_history"
            total_kb = (_finite(ctx.snapshot.get("host_identity", {}).get("memory_total_bytes")) or 1.0) / 1024
            swap_by_ts = {
                str(row.get("timestamp", "")): _finite(row.get("%swpused"))
                for row in ctx.history.get("sar_swap", [])
            }
            mem_title = "内存使用率（SAR 历史）"
            mem_series = [
                self._series("已用", [row.get("%memused") for row in mem_rows]),
                self._series("缓存", [
                    (_finite(row.get("kbcached")) or 0.0) / total_kb * 100 for row in mem_rows
                ]),
                self._series("Swap 已用", [swap_by_ts.get(str(row.get("timestamp", ""))) for row in mem_rows]),
            ]
        else:
            mem_rows, mem_scope = ctx.timeseries.get("system_memory", []), "realtime_snapshot"
            mem_title = "内存使用率（现场短时采样）"
            available_pct, cached_pct, swap_pct = [], [], []
            for row in mem_rows:
                total = _finite(row.get("mem_total_bytes")) or 0.0
                swap_total = _finite(row.get("swap_total_bytes")) or 0.0
                available_pct.append((_finite(row.get("mem_available_bytes")) or 0.0) / total * 100 if total else None)
                cached_pct.append((_finite(row.get("cached_bytes")) or 0.0) / total * 100 if total else None)
                swap_pct.append((_finite(row.get("swap_used_bytes")) or 0.0) / swap_total * 100 if swap_total else None)
            mem_series = [
                self._series("已用", [row.get("mem_used_pct") for row in mem_rows]),
                self._series("可用", available_pct),
                self._series("缓存", cached_pct),
                self._series("Swap 已用", swap_pct),
            ]
        specs.append({
            "chart_id": "SYSTEM_MEMORY", "title": mem_title,
            "x": [row.get("timestamp", "") for row in mem_rows],
            "panels": [("", "%", mem_series)], "source_scope": mem_scope,
            "filename": f"{instance_tag}_memory.png",
        })

        sar_disk = ctx.history.get("sar_disk", [])
        disk_rows: list[dict[str, Any]] = []
        disk_scope = "sar_history" if len(sar_disk) >= 2 else "realtime_snapshot"
        if disk_scope == "sar_history":
            devices = sorted({str(row.get("DEV")) for row in sar_disk if row.get("DEV")})
            if devices:
                device = max(devices, key=lambda name: sum(_finite(row.get("%util")) or 0.0 for row in sar_disk if row.get("DEV") == name))
                disk_rows = [row for row in sar_disk if row.get("DEV") == device]
                read_values = [row.get("rkB/s") for row in disk_rows]
                write_values = [row.get("wkB/s") for row in disk_rows]
                util_values = [row.get("%util") for row in disk_rows]
                await_values = [row.get("await") for row in disk_rows]
                disk_title = f"磁盘 I/O（{device}，SAR 历史）"
        else:
            realtime_disk = ctx.timeseries.get("system_disk", [])
            devices = sorted({str(row.get("device")) for row in realtime_disk if row.get("device")})
            if devices:
                device = max(devices, key=lambda name: sum(
                    (_finite(row.get("read_bytes_per_sec")) or 0.0) + (_finite(row.get("write_bytes_per_sec")) or 0.0)
                    for row in realtime_disk if row.get("device") == name
                ))
                disk_rows = [row for row in realtime_disk if row.get("device") == device]
                read_values = [(_finite(row.get("read_bytes_per_sec")) or 0.0) / 1024 for row in disk_rows]
                write_values = [(_finite(row.get("write_bytes_per_sec")) or 0.0) / 1024 for row in disk_rows]
                util_values = [row.get("util_pct") for row in disk_rows]
                await_values = [row.get("read_await_ms") for row in disk_rows]
                disk_title = f"磁盘 I/O（{device}，现场短时采样）"
        if disk_rows:
            specs.append({
                "chart_id": "SYSTEM_DISK", "title": disk_title,
                "x": [row.get("timestamp", "") for row in disk_rows],
                "panels": [
                    ("吞吐", "KiB/s", [self._series("读取", read_values), self._series("写入", write_values)]),
                    ("繁忙度", "%", [self._series("繁忙度", util_values)]),
                    ("响应时间", "ms", [self._series("响应时间", await_values)]),
                ],
                "source_scope": disk_scope, "filename": f"{instance_tag}_disk.png",
            })
        else:
            specs.append({
                "chart_id": "SYSTEM_DISK", "title": "磁盘 I/O", "x": [], "panels": [],
                "source_scope": disk_scope, "filename": f"{instance_tag}_disk.png",
            })

        network = ctx.timeseries.get("system_network", [])
        interfaces = sorted({str(row.get("interface")) for row in network if row.get("interface") and row.get("interface") != "lo"})
        network_rows: list[dict[str, Any]] = []
        interface = "unknown"
        if interfaces:
            interface = max(interfaces, key=lambda name: sum(
                (_finite(row.get("rx_bytes_per_sec")) or 0.0) + (_finite(row.get("tx_bytes_per_sec")) or 0.0)
                for row in network if row.get("interface") == name
            ))
            network_rows = [row for row in network if row.get("interface") == interface]
        network_rows = network_rows[1:] if len(network_rows) > 2 else network_rows
        specs.append({
            "chart_id": "SYSTEM_NETWORK_REALTIME", "title": f"网络吞吐（{interface}，现场短时采样）",
            "x": [row.get("timestamp", "") for row in network_rows],
            "panels": [("", "KiB/s", [
                self._series("接收", [(_finite(row.get("rx_bytes_per_sec")) or 0.0) / 1024 for row in network_rows]),
                self._series("发送", [(_finite(row.get("tx_bytes_per_sec")) or 0.0) / 1024 for row in network_rows]),
            ])],
            "source_scope": "realtime_snapshot", "warmup_points_excluded": 1,
            "filename": f"{instance_tag}_network_realtime.png",
        })

        rates = metrics.get("mysql_realtime", {}).get("derived_rate_series", [])
        rates = rates[1:] if len(rates) > 2 else rates
        specs.append({
            "chart_id": "MYSQL_QPS_TPS", "title": "MySQL QPS/TPS（现场短时采样）",
            "x": [row.get("timestamp", "") for row in rates],
            "panels": [("", "次/秒", [
                self._series("QPS", [row.get("Questions_per_sec") for row in rates]),
                self._series("TPS", [(_finite(row.get("Com_commit_per_sec")) or 0.0) + (_finite(row.get("Com_rollback_per_sec")) or 0.0) for row in rates]),
            ])],
            "source_scope": "realtime_snapshot", "warmup_points_excluded": 1,
            "filename": f"{instance_tag}_mysql_qps_tps.png",
        })

        mysql_rows = ctx.timeseries.get("mysql_status", [])
        specs.append({
            "chart_id": "MYSQL_THREADS", "title": "MySQL 连接与运行线程（现场短时采样）",
            "x": [row.get("timestamp", "") for row in mysql_rows],
            "panels": [("", "线程数", [
                self._series("已连接", [row.get("Threads_connected") for row in mysql_rows]),
                self._series("运行中", [row.get("Threads_running") for row in mysql_rows]),
            ])],
            "source_scope": "realtime_snapshot", "filename": f"{instance_tag}_mysql_threads.png",
        })
        return specs

    @staticmethod
    def _has_data(spec: dict[str, Any]) -> bool:
        if len(spec.get("x") or []) < 2:
            return False
        return any(
            any(value is not None for value in values)
            for _, _, series in spec.get("panels") or []
            for _, values in series
        )

    def _render_matplotlib(self, ctx: PackageContext, spec: dict[str, Any]) -> dict[str, Any]:
        start_ns = time.monotonic_ns()
        try:
            import matplotlib
            matplotlib.use("Agg")
            import matplotlib.dates as mdates
            import matplotlib.pyplot as plt
            from chart_style import apply_style
            apply_style()
        except ImportError:
            return {"chart_id": spec["chart_id"], "status": "skipped", "reason": "matplotlib_not_installed"}

        timezone_name, display_tz = _display_timezone(ctx)
        x = spec["x"]
        parsed = [_parse_chart_time(value, display_tz) for value in x]
        parsed_ok = all(value is not None for value in parsed)
        gap_indices = _gap_indices(parsed)
        span_seconds = 0.0
        if parsed_ok and len(parsed) > 1:
            span_seconds = (max(parsed) - min(parsed)).total_seconds()  # type: ignore[arg-type]
        short_clock = parsed_ok and span_seconds < 300
        plot_x: list[Any] = list(range(len(x))) if short_clock or not parsed_ok else parsed
        panels = spec["panels"]
        fig, axes = plt.subplots(len(panels), 1, sharex=True, figsize=(10, 3.9 if len(panels) == 1 else 7.2))
        if len(panels) == 1:
            axes = [axes]
        for axis, (panel_title, ylabel, series) in zip(axes, panels):
            for label, values in series:
                if gap_indices:
                    expanded_x: list[Any] = []
                    expanded_values: list[float | None] = []
                    for index, (x_value, y_value) in enumerate(zip(plot_x, values)):
                        if index in gap_indices:
                            expanded_x.append(x_value)
                            expanded_values.append(None)
                        expanded_x.append(x_value)
                        expanded_values.append(y_value)
                    axis.plot(expanded_x, expanded_values, label=label, linewidth=1.6)
                else:
                    axis.plot(plot_x, values, label=label, linewidth=1.6)
            if panel_title:
                axis.set_title(panel_title, loc="left", fontsize=10)
            axis.set_ylabel(ylabel)
            axis.legend(loc="upper right", ncol=min(4, max(1, len(series))))
        axes[0].set_title(spec["title"], loc="center", fontsize=13)
        last_axis = axes[-1]
        axis_mode = "clock_time" if short_clock else "datetime" if parsed_ok else "sample_index"
        if short_clock:
            step = max(1, len(x) // 6)
            ticks = list(range(0, len(x), step))
            if ticks[-1] != len(x) - 1:
                ticks.append(len(x) - 1)
            last_axis.set_xticks(ticks)
            last_axis.set_xticklabels([parsed[index].strftime("%H:%M:%S") for index in ticks])  # type: ignore[union-attr]
            last_axis.set_xlabel(f"时间（{timezone_name}）")
        elif parsed_ok:
            locator = mdates.AutoDateLocator(minticks=4, maxticks=8, tz=display_tz)
            formatter = mdates.DateFormatter("%m-%d %H:%M" if span_seconds <= 172800 else "%m-%d", tz=display_tz)
            last_axis.xaxis.set_major_locator(locator)
            last_axis.xaxis.set_major_formatter(formatter)
            last_axis.set_xlabel(f"时间（{timezone_name}）")
            fig.autofmt_xdate(rotation=25)
        else:
            step = max(1, len(x) // 6)
            last_axis.set_xticks(list(range(0, len(x), step)))
            last_axis.set_xlabel("采样点")
        if spec.get("source_scope") == "realtime_snapshot":
            note = "注：现场短时采样仅反映采集窗口。"
            if spec.get("warmup_points_excluded"):
                note += "首个启动点未纳入趋势线，原始值保留在分析数据中。"
            else:
                note += "首个点可能受采集启动影响。"
            fig.text(0.01, 0.005, note, fontsize=8, color="#666666")
        fig.tight_layout(rect=(0, 0.035, 1, 1))
        path = self.charts_dir / spec["filename"]
        fig.savefig(path)
        plt.close(fig)
        return {
            "chart_id": spec["chart_id"], "title": spec["title"], "status": "generated",
            "file": path.relative_to(self.output).as_posix(), "source_points": len(x),
            "source_scope": spec["source_scope"], "axis_mode": axis_mode,
            "time_zone": timezone_name, "renderer": "matplotlib", "duration_ms": duration_ms(start_ns),
            "warmup_points_excluded": spec.get("warmup_points_excluded", 0),
        }

    @staticmethod
    def _pillow_fonts() -> tuple[Any, Any, Any]:
        from PIL import ImageFont
        candidates = [
            Path("C:/Windows/Fonts/msyh.ttc"), Path("C:/Windows/Fonts/NotoSansSC-VF.ttf"),
            Path("C:/Windows/Fonts/simhei.ttf"), Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
            Path("/usr/share/fonts/truetype/wqy/wqy-microhei.ttc"),
        ]
        font_path = next((path for path in candidates if path.exists()), None)
        if font_path is None:
            return ImageFont.load_default(), ImageFont.load_default(), ImageFont.load_default()
        bold_path = Path("C:/Windows/Fonts/msyhbd.ttc")
        if not bold_path.exists():
            bold_path = font_path
        return (
            ImageFont.truetype(str(font_path), 24),
            ImageFont.truetype(str(font_path), 19),
            ImageFont.truetype(str(bold_path), 30),
        )

    def _render_pillow(self, ctx: PackageContext, spec: dict[str, Any]) -> dict[str, Any]:
        start_ns = time.monotonic_ns()
        try:
            from PIL import Image, ImageDraw
        except ImportError:
            return {"chart_id": spec["chart_id"], "status": "skipped", "reason": "pillow_not_installed"}
        timezone_name, display_tz = _display_timezone(ctx)
        x = spec["x"]
        parsed = [_parse_chart_time(value, display_tz) for value in x]
        parsed_ok = all(value is not None for value in parsed)
        gap_indices = _gap_indices(parsed)
        span_seconds = (max(parsed) - min(parsed)).total_seconds() if parsed_ok and len(parsed) > 1 else 0.0  # type: ignore[arg-type]
        axis_mode = "clock_time" if parsed_ok and span_seconds < 300 else "datetime" if parsed_ok else "sample_index"
        panels = spec["panels"]
        width = 1400
        panel_height = 300 if len(panels) > 1 else 480
        height = 115 + panel_height * len(panels) + 100
        image = Image.new("RGB", (width, height), "white")
        draw = ImageDraw.Draw(image)
        font, small, title_font = self._pillow_fonts()
        draw.text((95, 22), spec["title"], fill="#171717", font=title_font)
        colors = ("#1D4ED8", "#C2410C", "#9B1C1C", "#0F766E", "#6D28D9", "#4B5563")
        left, right = 105, 45
        plot_width = width - left - right
        top = 88
        for panel_index, (panel_title, ylabel, series) in enumerate(panels):
            panel_top = top + panel_index * panel_height
            panel_bottom = panel_top + panel_height - 55
            plot_height = panel_bottom - panel_top
            finite_values = [value for _, values in series for value in values if value is not None and math.isfinite(value)]
            if not finite_values:
                continue
            ymin = min(0.0, min(finite_values))
            ymax = max(finite_values)
            if ymax <= ymin:
                ymax = ymin + 1.0
            ymax *= 1.08
            if panel_title:
                draw.text((left, panel_top - 28), panel_title, fill="#333333", font=small)
            draw.text((10, panel_top), ylabel, fill="#555555", font=small)
            for grid_index in range(5):
                y = panel_top + int(plot_height * grid_index / 4)
                value = ymax - (ymax - ymin) * grid_index / 4
                draw.line((left, y, width - right, y), fill="#E5E7EB", width=1)
                draw.text((35, y - 10), f"{value:.1f}", fill="#777777", font=small)
            legend_x = left
            for series_index, (label, values) in enumerate(series):
                color = colors[series_index % len(colors)]
                segment: list[tuple[int, int]] = []
                for point_index, value in enumerate(values):
                    if point_index in gap_indices and len(segment) >= 2:
                        draw.line(segment, fill=color, width=3)
                        segment = []
                    if value is None:
                        if len(segment) >= 2:
                            draw.line(segment, fill=color, width=3)
                        segment = []
                        continue
                    px = left + int(plot_width * point_index / max(1, len(values) - 1))
                    py = panel_top + int((ymax - value) / (ymax - ymin) * plot_height)
                    segment.append((px, py))
                if len(segment) >= 2:
                    draw.line(segment, fill=color, width=3)
                draw.line((legend_x, panel_bottom + 14, legend_x + 42, panel_bottom + 14), fill=color, width=4)
                draw.text((legend_x + 50, panel_bottom + 1), label, fill="#333333", font=small)
                legend_x += 205
        label_y = height - 64
        step = max(1, len(x) // 5)
        ticks = list(range(0, len(x), step))
        if ticks and ticks[-1] != len(x) - 1:
            ticks.append(len(x) - 1)
        for index in ticks:
            px = left + int(plot_width * index / max(1, len(x) - 1))
            if axis_mode == "clock_time":
                label = parsed[index].strftime("%H:%M:%S")  # type: ignore[union-attr]
            elif axis_mode == "datetime":
                label = parsed[index].strftime("%m-%d %H:%M")  # type: ignore[union-attr]
            else:
                label = str(index + 1)
            draw.text((px - 42, label_y), label, fill="#666666", font=small)
        axis_label = f"时间（{timezone_name}）" if parsed_ok else "采样点"
        draw.text((width // 2 - 90, height - 30), axis_label, fill="#555555", font=small)
        if spec.get("source_scope") == "realtime_snapshot":
            note = "注：现场短时采样仅反映采集窗口。"
            if spec.get("warmup_points_excluded"):
                note += "首个启动点未纳入趋势线，原始值保留在分析数据中。"
            else:
                note += "首个点可能受采集启动影响。"
            draw.text((left, height - 92), note, fill="#666666", font=small)
        path = self.charts_dir / spec["filename"]
        image.save(path, "PNG")
        return {
            "chart_id": spec["chart_id"], "title": spec["title"], "status": "generated",
            "file": path.relative_to(self.output).as_posix(), "source_points": len(x),
            "source_scope": spec["source_scope"], "axis_mode": axis_mode,
            "time_zone": timezone_name, "renderer": "pillow_fallback", "duration_ms": duration_ms(start_ns),
            "warmup_points_excluded": spec.get("warmup_points_excluded", 0),
        }

    def generate_base(self, ctx: PackageContext, metrics: dict[str, Any]) -> list[dict[str, Any]]:
        self.charts_dir.mkdir(parents=True, exist_ok=True)
        charts: list[dict[str, Any]] = []
        for spec in self._chart_specs(ctx, metrics):
            if not self._has_data(spec):
                charts.append({
                    "chart_id": spec["chart_id"], "title": spec["title"], "status": "skipped",
                    "reason": "insufficient_data_points", "source_scope": spec["source_scope"],
                })
                continue
            chart = self._render_matplotlib(ctx, spec)
            if chart.get("status") != "generated":
                chart = self._render_pillow(ctx, spec)
            charts.append(chart)
        return charts

    def generate(self, ctx: PackageContext, metrics: dict[str, Any]) -> list[dict[str, Any]]:
        return self.generate_base(ctx, metrics)
