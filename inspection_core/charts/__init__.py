"""Database-neutral chart building blocks.

Layering rule: this package may import ``inspection_core`` primitives but must
never import a database plugin.  Plugins consume these helpers to render their
own report charts.

What lives here:
- ``style``   — palette, semantic series aliases, severity colours, rcParams
- ``history`` — SAR/时间归一化、断点检测、序列抽取
- ``specs``   — 四类 OS 图的纯数据规格（CPU/内存/磁盘/网络）
- ``render``  — matplotlib 后端 + Pillow 降级后端

What does not live here: database-specific charts (QPS/TPS、会话、表空间……)
still belong to ``plugins/<db>/charts.py``.
"""

from .history import (
    CPU_SUMMARY_VALUES,
    busy_percent,
    display_timezone,
    finite,
    gap_indices,
    parse_chart_time,
    parse_time,
    sar_cpu_summary_rows,
    series_values,
    summarize,
)
from .render import duration_ms, generate_os_charts, render_spec, spec_has_data
from .specs import (
    HISTORY_SCOPE,
    REALTIME_SCOPE,
    busiest_history_device,
    host_identity,
    os_chart_specs,
    os_cpu_spec,
    os_disk_spec,
    os_memory_spec,
    os_network_spec,
)
from .style import (
    CJK_FONTS,
    COLOR_MAP,
    COLORS,
    FLAT_PALETTE,
    apply_style,
    chart_colors,
    has_matplotlib,
)

__all__ = [
    "CJK_FONTS",
    "COLOR_MAP",
    "COLORS",
    "CPU_SUMMARY_VALUES",
    "FLAT_PALETTE",
    "HISTORY_SCOPE",
    "REALTIME_SCOPE",
    "apply_style",
    "busiest_history_device",
    "busy_percent",
    "chart_colors",
    "display_timezone",
    "duration_ms",
    "finite",
    "gap_indices",
    "generate_os_charts",
    "has_matplotlib",
    "host_identity",
    "os_chart_specs",
    "os_cpu_spec",
    "os_disk_spec",
    "os_memory_spec",
    "os_network_spec",
    "parse_chart_time",
    "parse_time",
    "render_spec",
    "sar_cpu_summary_rows",
    "series_values",
    "spec_has_data",
    "summarize",
]
