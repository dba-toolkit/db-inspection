"""SQL Server ?????"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from .metrics import num


def build_charts(snapshot: dict[str, Any], findings: list[dict[str, Any]], output: Path) -> list[dict[str, str]]:
    from .chart_style import bar_chart, line_chart

    output.mkdir(parents=True, exist_ok=True)
    charts: list[dict[str, str]] = []

    def add_line(filename: str, title: str, samples: list[dict[str, Any]], specs: list[tuple[str, str]]) -> None:
        if not samples or not any(any(num(x.get(key)) is not None for x in samples) for key, _ in specs):
            return
        path = output / filename
        labels = [str(x.get("timestamp", ""))[11:19] for x in samples]
        line_chart(path, title, labels, [(label, [num(x.get(key)) for x in samples]) for key, label in specs])
        charts.append({"id": path.stem, "title": title, "path": str(path.resolve()), "kind": "line"})

    def add_bar(filename: str, title: str, labels: list[str], values: list[float], suffix: str = "", colors: list[str] | None = None) -> None:
        if not labels or not values:
            return
        path = output / filename
        bar_chart(path, title, labels, values, colors=colors, suffix=suffix)
        charts.append({"id": path.stem, "title": title, "path": str(path.resolve()), "kind": "bar"})

    samples = snapshot.get("performance_samples", [])
    add_line("01_activity.png", "SQL Server 业务活动采样", samples,
             [("batch_requests_per_sec", "Batch Requests/s"), ("transactions_per_sec", "Transactions/s")])
    add_line("02_memory.png", "SQL Server 内存压力指标", samples,
             [("page_life_expectancy", "PLE (s)"), ("memory_grants_pending", "Memory Grants Pending")])
    add_line("03_connections.png", "连接与阻塞采样", samples,
             [("user_connections", "User Connections"), ("blocked_session_count", "Blocked Sessions")])

    waits = sorted(snapshot.get("wait_stats", []), key=lambda x: num(x.get("wait_time_ms")) or 0, reverse=True)[:8]
    add_bar("05_waits.png", "主要等待类型（累计秒）",
            [str(x.get("wait_type", "-")) for x in waits], [(num(x.get("wait_time_ms")) or 0) / 1000 for x in waits], "s")

    dbs = [x for x in snapshot.get("databases", []) if str(x.get("name", "")).lower() not in {"master", "model", "msdb", "tempdb"}]
    dbs = sorted(dbs, key=lambda x: (num(x.get("data_size_mb")) or 0) + (num(x.get("log_size_mb")) or 0), reverse=True)[:8]
    add_bar("06_database_size.png", "用户数据库容量（GB）",
            [str(x.get("name", "-")) for x in dbs], [((num(x.get("data_size_mb")) or 0) + (num(x.get("log_size_mb")) or 0)) / 1024 for x in dbs], "GB")

    files = sorted(snapshot.get("database_files", []), key=lambda x: max(num(x.get("avg_read_latency_ms")) or 0, num(x.get("avg_write_latency_ms")) or 0), reverse=True)[:8]
    add_bar("07_file_latency.png", "数据库文件最大平均 I/O 延迟",
            [f"{x.get('database_name')}/{x.get('logical_name')}" for x in files],
            [max(num(x.get("avg_read_latency_ms")) or 0, num(x.get("avg_write_latency_ms")) or 0) for x in files], "ms")

    volumes = sorted(snapshot.get("volumes", []), key=lambda x: num(x.get("free_pct")) or 100)[:8]
    add_bar("08_volume_free.png", "数据库所在卷可用空间比例",
            [str(x.get("volume_mount_point") or x.get("logical_volume_name") or "卷") for x in volumes],
            [num(x.get("free_pct")) or 0 for x in volumes], "%",
            ["#9B1C1C" if (num(x.get("free_pct")) or 0) < 10 else "#B26A00" if (num(x.get("free_pct")) or 0) < 20 else "#276749" for x in volumes])

    collected = snapshot.get("collection", {}).get("finished_at")
    try:
        now = datetime.fromisoformat(str(collected).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        now = datetime.now().astimezone()
    backup_labels: list[str] = []
    backup_hours: list[float] = []
    for row in snapshot.get("backups", []):
        if str(row.get("database_name", "")).lower() == "tempdb":
            continue
        try:
            then = datetime.fromisoformat(str(row.get("last_full_backup")).replace("Z", "+00:00"))
            if then.tzinfo is None and now.tzinfo is not None:
                then = then.replace(tzinfo=now.tzinfo)
            hours = max(0.0, (now - then).total_seconds() / 3600)
        except (TypeError, ValueError):
            hours = 999.0
        backup_labels.append(str(row.get("database_name", "-")))
        backup_hours.append(hours)
    pairs = sorted(zip(backup_labels, backup_hours), key=lambda x: x[1], reverse=True)[:8]
    add_bar("09_backup_age.png", "最近完整备份距今时间",
            [x[0] for x in pairs], [x[1] for x in pairs], "h",
            ["#9B1C1C" if x[1] > 168 else "#B26A00" if x[1] > 72 else "#276749" for x in pairs])

    return charts

