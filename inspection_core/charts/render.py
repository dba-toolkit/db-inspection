"""Spec renderer with a matplotlib backend and a dependency-light fallback.

Report generation must never silently lose a chart.  When matplotlib is absent
the Pillow backend draws the same spec, marks the renderer as
``pillow_fallback`` and the caller keeps the fact in the report model instead
of pretending the chart was produced by matplotlib.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from ..models import PackageContext
from .history import display_timezone, gap_indices, parse_chart_time
from .style import (
    BACKGROUND,
    COLOR_MAP,
    FALLBACK_LABEL_COLOUR,
    FALLBACK_NOTE_COLOUR,
    FALLBACK_PANEL_TITLE_COLOUR,
    FALLBACK_TICK_COLOUR,
    FALLBACK_TITLE_COLOUR,
    FLAT_PALETTE,
    LIGHT_GRID,
    apply_style,
    has_matplotlib,
)

__all__ = ["bar_spec_has_data", "duration_ms", "generate_os_charts", "render_bar_spec", "render_spec", "spec_has_data"]


def duration_ms(start_ns: int) -> int:
    return int((time.monotonic_ns() - start_ns) / 1_000_000)


def spec_has_data(spec: dict[str, Any]) -> bool:
    """A chart needs at least two x points and one defined value."""
    if len(spec.get("x") or []) < 2:
        return False
    return any(
        any(value is not None for value in values)
        for _, _, series in spec.get("panels") or []
        for _, values in series
    )


def _source_note(spec: dict[str, Any]) -> str:
    note = "注：现场短时采样仅反映采集窗口。"
    if spec.get("warmup_points_excluded"):
        note += "首个启动点未纳入趋势线，原始值保留在分析数据中。"
    else:
        note += "首个点可能受采集启动影响。"
    return note


def _axis_mode(parsed: list[Any], parsed_ok: bool) -> tuple[str, float, bool]:
    """Classify the x axis: clock ticks, calendar datetimes, or bare indices."""
    span_seconds = 0.0
    if parsed_ok and len(parsed) > 1:
        span_seconds = (max(parsed) - min(parsed)).total_seconds()
    short_clock = parsed_ok and span_seconds < 300
    if short_clock:
        return "clock_time", span_seconds, True
    if parsed_ok:
        return "datetime", span_seconds, False
    return "sample_index", span_seconds, False


def _render_matplotlib(spec: dict[str, Any], charts_dir: Path, output: Path,
                       timezone_name: str, display_tz: Any) -> dict[str, Any] | None:
    if not has_matplotlib():
        return None
    start_ns = time.monotonic_ns()
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt

    apply_style()
    x = spec["x"]
    parsed = [parse_chart_time(value, display_tz) for value in x]
    parsed_ok = all(value is not None for value in parsed)
    gaps = gap_indices(parsed)
    axis_mode, span_seconds, short_clock = _axis_mode(parsed, parsed_ok)
    plot_x: list[Any] = list(range(len(x))) if short_clock or not parsed_ok else parsed
    panels = spec["panels"]
    fig, axes = plt.subplots(
        len(panels), 1, sharex=True,
        figsize=(10, 3.9 if len(panels) == 1 else 7.2),
    )
    if len(panels) == 1:
        axes = [axes]
    for axis, (panel_title, ylabel, series) in zip(axes, panels):
        for label, values in series:
            if gaps:
                expanded_x: list[Any] = []
                expanded_values: list[float | None] = []
                for index, (x_value, y_value) in enumerate(zip(plot_x, values)):
                    if index in gaps:
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
    if short_clock:
        step = max(1, len(x) // 6)
        ticks = list(range(0, len(x), step))
        if ticks[-1] != len(x) - 1:
            ticks.append(len(x) - 1)
        last_axis.set_xticks(ticks)
        last_axis.set_xticklabels([parsed[index].strftime("%H:%M:%S") for index in ticks])
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
        fig.text(0.01, 0.005, _source_note(spec), fontsize=8, color=FALLBACK_NOTE_COLOUR)
    fig.tight_layout(rect=(0, 0.035, 1, 1))
    path = charts_dir / spec["filename"]
    fig.savefig(path)
    plt.close(fig)
    return {
        "chart_id": spec["chart_id"], "title": spec["title"], "status": "generated",
        "file": path.relative_to(output).as_posix(), "source_points": len(x),
        "source_scope": spec["source_scope"], "axis_mode": axis_mode,
        "time_zone": timezone_name, "renderer": "matplotlib", "duration_ms": duration_ms(start_ns),
        "warmup_points_excluded": spec.get("warmup_points_excluded", 0),
    }


def _pillow_fonts() -> tuple[Any, Any, Any]:
    from PIL import ImageFont

    candidates = [
        Path("C:/Windows/Fonts/msyh.ttc"),
        Path("C:/Windows/Fonts/NotoSansSC-VF.ttf"),
        Path("C:/Windows/Fonts/simhei.ttf"),
        Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
        Path("/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc"),
        Path("/usr/share/fonts/truetype/wqy/wqy-microhei.ttc"),
        Path("/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc"),
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


def _render_pillow(spec: dict[str, Any], charts_dir: Path, output: Path,
                   timezone_name: str, display_tz: Any) -> dict[str, Any] | None:
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return None
    import math

    start_ns = time.monotonic_ns()
    x = spec["x"]
    parsed = [parse_chart_time(value, display_tz) for value in x]
    parsed_ok = all(value is not None for value in parsed)
    gaps = gap_indices(parsed)
    axis_mode, _, _ = _axis_mode(parsed, parsed_ok)
    panels = spec["panels"]
    width = 1400
    panel_height = 300 if len(panels) > 1 else 480
    height = 115 + panel_height * len(panels) + 100
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    font, small, title_font = _pillow_fonts()
    draw.text((95, 22), spec["title"], fill=FALLBACK_TITLE_COLOUR, font=title_font)
    colors = FLAT_PALETTE
    left, right = 105, 45
    plot_width = width - left - right
    top = 88
    for panel_index, (panel_title, ylabel, series) in enumerate(panels):
        panel_top = top + panel_index * panel_height
        panel_bottom = panel_top + panel_height - 55
        plot_height = panel_bottom - panel_top
        finite_values = [
            value for _, values in series
            for value in values if value is not None and math.isfinite(value)
        ]
        if not finite_values:
            continue
        ymin = min(0.0, min(finite_values))
        ymax = max(finite_values)
        if ymax <= ymin:
            ymax = ymin + 1.0
        ymax *= 1.08
        if panel_title:
            draw.text((left, panel_top - 28), panel_title, fill=FALLBACK_PANEL_TITLE_COLOUR, font=small)
        draw.text((10, panel_top), ylabel, fill=FALLBACK_LABEL_COLOUR, font=small)
        for grid_index in range(5):
            y = panel_top + int(plot_height * grid_index / 4)
            value = ymax - (ymax - ymin) * grid_index / 4
            draw.line((left, y, width - right, y), fill="#E5E7EB", width=1)
            draw.text((35, y - 10), f"{value:.1f}", fill=FALLBACK_TICK_COLOUR, font=small)
        legend_x = left
        for series_index, (label, values) in enumerate(series):
            color = colors[series_index % len(colors)]
            segment: list[tuple[int, int]] = []
            for point_index, value in enumerate(values):
                if point_index in gaps and len(segment) >= 2:
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
            draw.text((legend_x + 50, panel_bottom + 1), label, fill=FALLBACK_PANEL_TITLE_COLOUR, font=small)
            legend_x += 205
    label_y = height - 64
    step = max(1, len(x) // 5)
    ticks = list(range(0, len(x), step))
    if ticks and ticks[-1] != len(x) - 1:
        ticks.append(len(x) - 1)
    for index in ticks:
        px = left + int(plot_width * index / max(1, len(x) - 1))
        if axis_mode == "clock_time":
            label = parsed[index].strftime("%H:%M:%S")
        elif axis_mode == "datetime":
            label = parsed[index].strftime("%m-%d %H:%M")
        else:
            label = str(index + 1)
        draw.text((px - 42, label_y), label, fill=FALLBACK_TICK_COLOUR, font=small)
    axis_label = f"时间（{timezone_name}）" if parsed_ok else "采样点"
    draw.text((width // 2 - 90, height - 30), axis_label, fill=FALLBACK_LABEL_COLOUR, font=small)
    if spec.get("source_scope") == "realtime_snapshot":
        draw.text((left, height - 92), _source_note(spec), fill=FALLBACK_NOTE_COLOUR, font=small)
    path = charts_dir / spec["filename"]
    image.save(path, "PNG")
    return {
        "chart_id": spec["chart_id"], "title": spec["title"], "status": "generated",
        "file": path.relative_to(output).as_posix(), "source_points": len(x),
        "source_scope": spec["source_scope"], "axis_mode": axis_mode,
        "time_zone": timezone_name, "renderer": "pillow_fallback", "duration_ms": duration_ms(start_ns),
        "warmup_points_excluded": spec.get("warmup_points_excluded", 0),
    }


def render_spec(ctx: PackageContext, spec: dict[str, Any], charts_dir: Path, output: Path,
                *, timezone_name: str | None = None, display_tz: Any = None) -> dict[str, Any]:
    """Render one spec, preferring matplotlib and falling back to Pillow.

    Returns a result record that always names the renderer actually used, or a
    ``skipped`` record with a reason — never a silent success.
    """
    if not spec_has_data(spec):
        return {
            "chart_id": spec["chart_id"], "title": spec["title"], "status": "skipped",
            "reason": "insufficient_data_points", "source_scope": spec["source_scope"],
        }
    if timezone_name is None or display_tz is None:
        timezone_name, display_tz = display_timezone(ctx)
    charts_dir.mkdir(parents=True, exist_ok=True)
    result = _render_matplotlib(spec, charts_dir, output, timezone_name, display_tz)
    if result is not None:
        return result
    result = _render_pillow(spec, charts_dir, output, timezone_name, display_tz)
    if result is not None:
        return result
    return {
        "chart_id": spec["chart_id"], "title": spec["title"], "status": "skipped",
        "reason": "no_renderer_available", "source_scope": spec["source_scope"],
    }


def generate_os_charts(ctx: PackageContext, instance_tag: str, charts_dir: Path,
                       output: Path) -> list[dict[str, Any]]:
    """Render the four shared OS charts into ``charts_dir``."""
    from .specs import os_chart_specs

    timezone_name, display_tz = display_timezone(ctx)
    return [
        render_spec(
            ctx, spec, charts_dir, output,
            timezone_name=timezone_name, display_tz=display_tz,
        )
        for spec in os_chart_specs(ctx, instance_tag)
    ]


# ---------------------------------------------------------------------------
# category-axis bar charts（Top-N 排行）
#
# 与上面的时间轴折线是**两种规格**：折线的 x 是时间戳、要求 ≥2 点；条形的 x
# 是类别（表空间名 / SQL_ID），一个类别就该能画。把两者塞进同一 spec 结构会
# 让 ``spec_has_data`` 的「≥2 点」判据对条形失效，故独立成入口，互不牵连。
# ---------------------------------------------------------------------------
def bar_spec_has_data(spec: dict[str, Any]) -> bool:
    """A bar chart needs at least one category carrying a defined value."""
    if not (spec.get("categories") or []):
        return False
    return any(value is not None for value in (spec.get("values") or []))


def _bar_pairs(spec: dict[str, Any]) -> list[tuple[str, float]]:
    """Zip categories with values, dropping pairs whose value is missing."""
    pairs: list[tuple[str, float]] = []
    for category, value in zip(spec.get("categories") or [], spec.get("values") or []):
        if value is None:
            continue
        pairs.append((str(category), float(value)))
    return pairs


def _bar_color(value: float, spec: dict[str, Any]) -> str:
    """Single-hue bars, turning red once a threshold is crossed (e.g. 表空间 ≥90%)."""
    threshold = spec.get("alert_threshold")
    if threshold is not None and value >= float(threshold):
        return COLOR_MAP["red"]
    return COLOR_MAP["blue"]


def _short_number(value: float) -> str:
    """Compact label: 1234 → 1.2k, 3_400_000 → 3.4M."""
    magnitude = abs(float(value))
    for limit, suffix in ((1e9, "G"), (1e6, "M"), (1e3, "k")):
        if magnitude >= limit:
            return f"{value / limit:.1f}{suffix}"
    return f"{value:g}"


def _render_bar_matplotlib(spec: dict[str, Any], charts_dir: Path,
                           output: Path) -> dict[str, Any] | None:
    if not has_matplotlib():
        return None
    start_ns = time.monotonic_ns()
    import matplotlib.pyplot as plt

    apply_style()
    pairs = _bar_pairs(spec)
    labels = [label for label, _ in pairs]
    values = [value for _, value in pairs]
    suffix = spec.get("value_suffix", "")
    height = max(3.0, 0.42 * len(labels) + 1.8)
    fig, axis = plt.subplots(figsize=(10, height))
    positions = list(range(len(labels)))
    axis.barh(positions, values, height=0.66,
              color=[_bar_color(value, spec) for value in values])
    axis.set_yticks(positions)
    axis.set_yticklabels(labels)
    axis.invert_yaxis()  # 最大值在顶部
    axis.set_title(spec["title"], loc="center", fontsize=13)
    if spec.get("xmax"):
        axis.set_xlim(0, float(spec["xmax"]))
    if spec.get("xlabel"):
        axis.set_xlabel(spec["xlabel"])
    for position, value in zip(positions, values):
        axis.text(value, position, f" {_short_number(value)}{suffix}",
                  va="center", fontsize=8.5)
    fig.tight_layout()
    path = charts_dir / spec["filename"]
    fig.savefig(path)
    plt.close(fig)
    return {
        "chart_id": spec["chart_id"], "title": spec["title"], "status": "generated",
        "file": path.relative_to(output).as_posix(), "source_points": len(labels),
        "source_scope": spec.get("source_scope", ""), "axis_mode": "category",
        "renderer": "matplotlib", "duration_ms": duration_ms(start_ns),
    }


def _render_bar_pillow(spec: dict[str, Any], charts_dir: Path,
                       output: Path) -> dict[str, Any] | None:
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return None

    start_ns = time.monotonic_ns()
    pairs = _bar_pairs(spec)
    labels = [label for label, _ in pairs]
    values = [value for _, value in pairs]
    suffix = spec.get("value_suffix", "")
    width = 1400
    left, right, top, bottom = 170, 70, 108, 74
    row_height = 42
    plot_height = max(1, len(labels)) * row_height
    height = top + plot_height + bottom
    image = Image.new("RGB", (width, height), BACKGROUND)
    draw = ImageDraw.Draw(image)
    font, small, title_font = _pillow_fonts()
    draw.text((60, 24), spec["title"], fill=FALLBACK_TITLE_COLOUR, font=title_font)
    data_max = max(values) if values else 1.0
    xmax = float(spec.get("xmax") or max(data_max * 1.18, 1.0)) or 1.0
    plot_width = width - left - right
    for index in range(6):
        x = left + plot_width * index / 5
        draw.line((x, top, x, top + plot_height), fill=LIGHT_GRID, width=1)
        draw.text((x - 22, top + plot_height + 10), _short_number(xmax * index / 5),
                  fill=FALLBACK_TICK_COLOUR, font=small)
    for index, (label, value) in enumerate(pairs):
        y1 = top + index * row_height + 6
        y2 = y1 + row_height - 12
        bar_width = plot_width * (value / xmax)
        draw.rounded_rectangle((left, y1, max(left + 2, left + bar_width), y2),
                               radius=5, fill=_bar_color(value, spec))
        rendered = label if len(label) <= 20 else label[:18] + "…"
        draw.text((10, y1 + 4), rendered, fill=FALLBACK_PANEL_TITLE_COLOUR, font=small)
        draw.text((min(left + bar_width + 8, width - right - 80), y1 + 4),
                  f"{_short_number(value)}{suffix}",
                  fill=FALLBACK_PANEL_TITLE_COLOUR, font=small)
    path = charts_dir / spec["filename"]
    image.save(path, "PNG")
    return {
        "chart_id": spec["chart_id"], "title": spec["title"], "status": "generated",
        "file": path.relative_to(output).as_posix(), "source_points": len(labels),
        "source_scope": spec.get("source_scope", ""), "axis_mode": "category",
        "renderer": "pillow_fallback", "duration_ms": duration_ms(start_ns),
    }


def render_bar_spec(spec: dict[str, Any], charts_dir: Path, output: Path) -> dict[str, Any]:
    """Render one category-axis bar spec, matplotlib first, Pillow as fallback.

    Same contract as ``render_spec``: never a silent success — a data-less spec
    yields a ``skipped`` record, and the record always names the renderer used.
    """
    if not bar_spec_has_data(spec):
        return {
            "chart_id": spec["chart_id"], "title": spec["title"], "status": "skipped",
            "reason": "insufficient_data_points",
            "source_scope": spec.get("source_scope", ""),
        }
    charts_dir.mkdir(parents=True, exist_ok=True)
    result = _render_bar_matplotlib(spec, charts_dir, output)
    if result is not None:
        return result
    result = _render_bar_pillow(spec, charts_dir, output)
    if result is not None:
        return result
    return {
        "chart_id": spec["chart_id"], "title": spec["title"], "status": "skipped",
        "reason": "no_renderer_available", "source_scope": spec.get("source_scope", ""),
    }

