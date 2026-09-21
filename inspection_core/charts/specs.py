"""OS chart specifications shared by every database plugin.

A "spec" is pure data: chart id, title, x axis, panels and the source scope the
numbers came from.  Rendering lives in ``render.py``, which keeps the MySQL,
Oracle and PostgreSQL plugins from drifting apart on titles, panel layout and
warm-up handling.

Only whole-host Linux metrics are described here.  Database-specific charts
(QPS/TPS, sessions, tablespaces, ...) stay in ``plugins/<db>/charts.py``.
"""
from __future__ import annotations

from typing import Any

from ..models import PackageContext
from ..system_checks import host_identity, memory_total_kb
from .history import disk_throughput_values, finite, sar_cpu_summary_rows

__all__ = [
    "busiest_history_device",
    "host_identity",
    "os_chart_specs",
    "os_cpu_spec",
    "os_disk_spec",
    "os_memory_spec",
    "os_network_spec",
]

HISTORY_SCOPE = "sar_history"
REALTIME_SCOPE = "realtime_snapshot"


def _cpu_panels_from_history(ctx: PackageContext) -> list[tuple[str, str, list]]:
    rows = sar_cpu_summary_rows(ctx.history.get("sar_cpu", []))
    return [
        ("", "%", [
            ("用户", [finite(row.get("%user")) for row in rows]),
            ("系统", [finite(row.get("%system")) for row in rows]),
            ("IO 等待", [finite(row.get("%iowait")) for row in rows]),
            ("虚拟化抢占", [finite(row.get("%steal")) for row in rows]),
        ]),
    ]


def _cpu_panels_from_realtime(rows: list[dict[str, Any]]) -> list[tuple[str, str, list]]:
    return [
        ("", "%", [
            ("用户", [finite(row.get("user_pct")) for row in rows]),
            ("系统", [finite(row.get("system_pct")) for row in rows]),
            ("IO 等待", [finite(row.get("iowait_pct")) for row in rows]),
            ("虚拟化抢占", [finite(row.get("steal_pct")) for row in rows]),
        ]),
    ]


def os_cpu_spec(ctx: PackageContext, instance_tag: str) -> dict[str, Any]:
    """CPU utilisation chart — SAR history when usable, else the live sample.

    Preferring SAR keeps the panel honest: a three-minute snapshot of a
    twenty-four-hour day must not be titled as if it described the day.
    """
    sar_rows = sar_cpu_summary_rows(ctx.history.get("sar_cpu", []))
    if len(sar_rows) >= 2:
        rows, scope = sar_rows, HISTORY_SCOPE
        title = "CPU 使用率（SAR 历史）"
        panels = _cpu_panels_from_history(ctx)
    else:
        rows, scope = ctx.timeseries.get("system_cpu", []), REALTIME_SCOPE
        title = "CPU 使用率（现场短时采样）"
        panels = _cpu_panels_from_realtime(rows)
    return {
        "chart_id": "SYSTEM_CPU",
        "title": title,
        "x": [row.get("timestamp", "") for row in rows],
        "panels": panels,
        "source_scope": scope,
        "filename": f"{instance_tag}_cpu.png",
    }


def os_memory_spec(ctx: PackageContext, instance_tag: str) -> dict[str, Any]:
    """Memory utilisation chart (used / cached / swap) with a total-memory base."""
    sar_rows = ctx.history.get("sar_memory", [])
    if len(sar_rows) >= 2:
        rows, scope = sar_rows, HISTORY_SCOPE
        total_kb = memory_total_kb(ctx, sar_rows)
        swap_by_ts = {
            str(row.get("timestamp", "")): finite(row.get("%swpused"))
            for row in ctx.history.get("sar_swap", [])
        }
        title = "内存使用率（SAR 历史）"
        # 缓存曲线只在总量已知时才有意义：没有总量就画不了百分比，
        # 用 1.0 兜底会画出几十万 % 的假线（Oracle 的扁平 snapshot 曾如此）。
        cached_series = (
            [("缓存", [(finite(row.get("kbcached")) or 0.0) / total_kb * 100 for row in rows])]
            if total_kb else []
        )
        panels = [
            ("", "%", [
                ("已用", [finite(row.get("%memused")) for row in rows]),
                *cached_series,
                ("Swap 已用", [swap_by_ts.get(str(row.get("timestamp", ""))) for row in rows]),
            ]),
        ]
    else:
        rows, scope = ctx.timeseries.get("system_memory", []), REALTIME_SCOPE
        title = "内存使用率（现场短时采样）"
        available_pct: list[float | None] = []
        cached_pct: list[float | None] = []
        swap_pct: list[float | None] = []
        for row in rows:
            total = finite(row.get("mem_total_bytes")) or 0.0
            swap_total = finite(row.get("swap_total_bytes")) or 0.0
            available = finite(row.get("mem_available_bytes")) or 0.0
            cached = finite(row.get("cached_bytes")) or 0.0
            swap_used = finite(row.get("swap_used_bytes")) or 0.0
            available_pct.append(available / total * 100 if total else None)
            cached_pct.append(cached / total * 100 if total else None)
            swap_pct.append(swap_used / swap_total * 100 if swap_total else None)
        panels = [
            ("", "%", [
                ("已用", [finite(row.get("mem_used_pct")) for row in rows]),
                ("可用", available_pct),
                ("缓存", cached_pct),
                ("Swap 已用", swap_pct),
            ]),
        ]
    return {
        "chart_id": "SYSTEM_MEMORY",
        "title": title,
        "x": [row.get("timestamp", "") for row in rows],
        "panels": panels,
        "source_scope": scope,
        "filename": f"{instance_tag}_memory.png",
    }


def busiest_history_device(rows: list[dict[str, Any]]) -> str | None:
    """The device with the highest accumulated ``%util`` in a SAR disk table.

    Shared with the metric layer so "which disk is the busy one" is decided once.
    """
    devices = sorted({str(row.get("DEV")) for row in rows if row.get("DEV")})
    if not devices:
        return None
    return max(
        devices,
        key=lambda name: sum(
            finite(row.get("%util")) or 0.0 for row in rows if row.get("DEV") == name
        ),
    )


def _busiest_realtime_device(rows: list[dict[str, Any]]) -> str | None:
    devices = sorted({str(row.get("device")) for row in rows if row.get("device")})
    if not devices:
        return None
    return max(
        devices,
        key=lambda name: sum(
            (finite(row.get("read_bytes_per_sec")) or 0.0)
            + (finite(row.get("write_bytes_per_sec")) or 0.0)
            for row in rows if row.get("device") == name
        ),
    )


def os_disk_spec(ctx: PackageContext, instance_tag: str) -> dict[str, Any]:
    """Three-panel disk chart (throughput / busy / latency) for the busiest device.

    Only the busiest device is plotted: overlaying every device turns the panel
    into an unreadable thicket, and the report text carries the full table.
    """
    sar_rows = ctx.history.get("sar_disk", [])
    if len(sar_rows) >= 2:
        scope = HISTORY_SCOPE
        device = busiest_history_device(sar_rows)
        rows = [row for row in sar_rows if row.get("DEV") == device] if device else []
        # 读写吞吐走公共归一：sysstat 只认 `-p` 时给 rkB/s、否则给扇区/秒，
        # 两种拼法都要能画出来（见 history.DISK_THROUGHPUT_ALIASES）。
        read_values = disk_throughput_values(rows, "rkB/s")
        write_values = disk_throughput_values(rows, "wkB/s")
        util_values = [row.get("%util") for row in rows]
        await_values = [row.get("await") for row in rows]
        title = f"磁盘 I/O（{device}，SAR 历史）" if device else "磁盘 I/O（SAR 历史）"
    else:
        scope = REALTIME_SCOPE
        realtime_rows = ctx.timeseries.get("system_disk", [])
        device = _busiest_realtime_device(realtime_rows)
        rows = [row for row in realtime_rows if row.get("device") == device] if device else []
        read_values = [(finite(row.get("read_bytes_per_sec")) or 0.0) / 1024 for row in rows]
        write_values = [(finite(row.get("write_bytes_per_sec")) or 0.0) / 1024 for row in rows]
        util_values = [row.get("util_pct") for row in rows]
        await_values = [row.get("read_await_ms") for row in rows]
        title = f"磁盘 I/O（{device}，现场短时采样）" if device else "磁盘 I/O（现场短时采样）"
    if not rows:
        return {
            "chart_id": "SYSTEM_DISK", "title": "磁盘 I/O", "x": [], "panels": [],
            "source_scope": scope, "filename": f"{instance_tag}_disk.png",
        }
    return {
        "chart_id": "SYSTEM_DISK",
        "title": title,
        "x": [row.get("timestamp", "") for row in rows],
        "panels": [
            ("吞吐", "KiB/s", [
                ("读取", [finite(value) for value in read_values]),
                ("写入", [finite(value) for value in write_values]),
            ]),
            ("繁忙度", "%", [("繁忙度", [finite(value) for value in util_values])]),
            ("响应时间", "ms", [("响应时间", [finite(value) for value in await_values])]),
        ],
        "source_scope": scope,
        "filename": f"{instance_tag}_disk.png",
    }


def os_network_spec(ctx: PackageContext, instance_tag: str) -> dict[str, Any]:
    """Network throughput for the busiest non-loopback interface.

    The first sample is dropped when more than two points exist: it is measured
    across the collector's own startup, so it reads low and drags the scale.
    """
    rows = ctx.timeseries.get("system_network", [])
    interfaces = sorted({
        str(row.get("interface")) for row in rows
        if row.get("interface") and row.get("interface") != "lo"
    })
    interface = "unknown"
    selected: list[dict[str, Any]] = []
    if interfaces:
        interface = max(
            interfaces,
            key=lambda name: sum(
                (finite(row.get("rx_bytes_per_sec")) or 0.0)
                + (finite(row.get("tx_bytes_per_sec")) or 0.0)
                for row in rows if row.get("interface") == name
            ),
        )
        selected = [row for row in rows if row.get("interface") == interface]
    excluded = 1 if len(selected) > 2 else 0
    if excluded:
        selected = selected[1:]
    return {
        "chart_id": "SYSTEM_NETWORK_REALTIME",
        "title": f"网络吞吐（{interface}，现场短时采样）",
        "x": [row.get("timestamp", "") for row in selected],
        "panels": [
            ("", "KiB/s", [
                ("接收", [
                    (finite(row.get("rx_bytes_per_sec")) or 0.0) / 1024 for row in selected
                ]),
                ("发送", [
                    (finite(row.get("tx_bytes_per_sec")) or 0.0) / 1024 for row in selected
                ]),
            ]),
        ],
        "source_scope": REALTIME_SCOPE,
        "warmup_points_excluded": excluded,
        "filename": f"{instance_tag}_network_realtime.png",
    }


def os_chart_specs(ctx: PackageContext, instance_tag: str) -> list[dict[str, Any]]:
    """The four OS charts every Linux-hosted database inspection shares."""
    return [
        os_cpu_spec(ctx, instance_tag),
        os_memory_spec(ctx, instance_tag),
        os_disk_spec(ctx, instance_tag),
        os_network_spec(ctx, instance_tag),
    ]
