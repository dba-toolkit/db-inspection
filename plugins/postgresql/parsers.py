"""Low-level parsers for PostgreSQL OS and SAR evidence."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from inspection_core.values import safe_float

def _read_sar_csv(path: Path) -> list[dict[str, str]] | None:
    """Read sadf semicolon-delimited CSV with auto-detected headers, skipping # comments."""
    rows: list[dict[str, str]] = []
    with path.open("r", encoding="utf-8-sig", errors="replace") as f:
        columns: list[str] | None = None
        for line in f:
            line = line.strip()
            if not line:
                continue
            if line.startswith("#"):
                candidate = line.lstrip("# ").strip()
                if ";" in candidate and "timestamp" in candidate.split(";"):
                    columns = candidate.split(";")
                continue
            if columns is None:
                candidate = line.split(";")
                if "timestamp" in candidate:
                    columns = candidate
                continue
            parts = line.split(";")
            if len(parts) >= 2:
                row = {col: (parts[i] if i < len(parts) else "") for i, col in enumerate(columns)}
                rows.append(row)
    return rows if rows else None


def _parse_sar_timestamp(raw: str) -> datetime | None:
    raw = raw.strip()
    if not raw:
        return None
    normalized = raw.replace(" UTC", "+00:00").replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(normalized)
        return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    normalized = raw.replace(" CST", "")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(normalized[:19], fmt).replace(tzinfo=timezone.utc)
        except (ValueError, IndexError):
            continue
    return None


def _parse_sar_cpu(rows: list[dict[str, str]]) -> list[tuple[str, list[datetime], list[float]]] | None:
    """Parse sadf CPU rows (columns: timestamp,%%user,%%system,%%iowait,%%idle)."""
    ts_list: list[datetime] = []
    user_v, sys_v, io_v, idle_v = [], [], [], []
    for r in rows:
        dt = _parse_sar_timestamp(r.get("timestamp", ""))
        if dt is None:
            continue
        ts_list.append(dt)
        user_v.append((safe_float(r.get("%user", r.get("%usr", "0"))) or 0) + (safe_float(r.get("%nice", "0")) or 0))
        sys_v.append(safe_float(r.get("%system", r.get("%sys", "0"))) or 0)
        io_v.append(safe_float(r.get("%iowait", "0")) or 0)
        idle_v.append(safe_float(r.get("%idle", "0")) or 0)
    if not ts_list:
        return None
    busy_v = [max(0.0, min(100.0, 100.0 - value)) for value in idle_v]
    return [
        ("busy", ts_list, busy_v),
        ("user+nice", ts_list, user_v),
        ("system", ts_list, sys_v),
        ("iowait", ts_list, io_v),
    ]


def _effective_sar_coverage_hours(series: list[tuple[str, list[datetime], list[float]]] | None) -> float:
    """Estimate real coverage inside the latest 24h, excluding long data gaps."""
    if not series:
        return 0.0
    timestamps = sorted(set(series[0][1]))
    if len(timestamps) < 2:
        return 0.0
    cutoff = timestamps[-1] - timedelta(hours=24)
    timestamps = [ts for ts in timestamps if ts >= cutoff]
    covered_seconds = sum(
        gap for a, b in zip(timestamps, timestamps[1:])
        if 0 < (gap := (b - a).total_seconds()) <= 3600
    )
    return round(covered_seconds / 3600, 2)


def _parse_sar_memory(rows: list[dict[str, str]]) -> list[tuple[str, list[datetime], list[float]]] | None:
    """Parse sadf memory rows (columns: timestamp,%%memused)."""
    ts_list, vals = [], []
    for r in rows:
        dt = _parse_sar_timestamp(r.get("timestamp", ""))
        if dt is None:
            continue
        v = safe_float(r.get("%memused"))
        if v is not None:
            ts_list.append(dt)
            vals.append(v)
    if not ts_list:
        return None
    return [("%memused", ts_list, vals)]


def _parse_sar_metric_by_metric(path: Path, col_names: set[str]) -> list[tuple[str, list[datetime], list[float]]] | None:
    """Generic sar parser using column names directly."""
    rows = _read_sar_csv(path)
    if not rows:
        return None
    result: list[tuple[str, list[datetime], list[float]]] = []
    for col in sorted(col_names):
        if col not in rows[0]:
            continue
        ts_list, vals = [], []
        for r in rows:
            dt = _parse_sar_timestamp(r.get("timestamp", ""))
            if dt is None:
                continue
            v = safe_float(r.get(col))
            if v is not None:
                ts_list.append(dt)
                vals.append(v)
        if ts_list:
            result.append((col, ts_list, vals))
    return result if result else None


def _parse_sar_disk(path: Path) -> list[tuple[str, list[datetime], list[float]]] | None:
    """Parse sadf disk rows, picking busiest device by %%util."""
    rows = _read_sar_csv(path)
    if not rows:
        return None
    dev_col = next((k for k in rows[0] if k.strip() == "DEV"), None)
    if not dev_col:
        return None
    util_by_dev: dict[str, float] = {}
    for r in rows:
        dev = r.get(dev_col, "").strip()
        if not dev:
            continue
        util_by_dev[dev] = util_by_dev.get(dev, 0) + (safe_float(r.get("%util")) or 0)
    if not util_by_dev:
        return None
    busiest = max(util_by_dev, key=util_by_dev.get)
    ts_list: list[datetime] = []
    util_v, read_v, write_v = [], [], []
    for r in rows:
        if r.get(dev_col, "").strip() != busiest:
            continue
        dt = _parse_sar_timestamp(r.get("timestamp", ""))
        if dt is None:
            continue
        ts_list.append(dt)
        util_v.append(safe_float(r.get("%util", "0")) or 0)
        rk = safe_float(r.get("rkB/s"))
        rd = safe_float(r.get("rd_sec/s"))
        read_v.append(rk if rk is not None else (rd / 2 if rd is not None else 0))
        wk = safe_float(r.get("wkB/s"))
        wr = safe_float(r.get("wr_sec/s"))
        write_v.append(wk if wk is not None else (wr / 2 if wr is not None else 0))
    result: list[tuple[str, list[datetime], list[float]]] = []
    if ts_list:
        result.append((f"{busiest} util%", ts_list, util_v))
        result.append((f"{busiest} read", ts_list, read_v))
        result.append((f"{busiest} write", ts_list, write_v))
    return result if result else None


def _parse_sar_network(path: Path) -> list[tuple[str, list[datetime], list[float]]] | None:
    """Parse sadf network rows, picking busiest interface."""
    rows = _read_sar_csv(path)
    if not rows:
        return None
    iface_col = next((k for k in rows[0] if k.strip() == "IFACE"), None)
    if not iface_col:
        return None
    rx_by_if: dict[str, float] = {}
    for r in rows:
        iface = r.get(iface_col, "").strip()
        if not iface:
            continue
        if iface == "lo":
            continue
        rx_by_if[iface] = rx_by_if.get(iface, 0) + (safe_float(r.get("rxkB/s")) or 0) + (safe_float(r.get("txkB/s")) or 0)
    if not rx_by_if:
        return None
    busiest = max(rx_by_if, key=rx_by_if.get)
    ts_list, rx_v, tx_v = [], [], []
    for r in rows:
        if r.get(iface_col, "").strip() != busiest:
            continue
        dt = _parse_sar_timestamp(r.get("timestamp", ""))
        if dt is None:
            continue
        ts_list.append(dt)
        rx_v.append(safe_float(r.get("rxkB/s", "0")) or 0)
        tx_v.append(safe_float(r.get("txkB/s", "0")) or 0)
    result: list[tuple[str, list[datetime], list[float]]] = []
    if ts_list:
        result.append((f"{busiest} rx", ts_list, rx_v))
        result.append((f"{busiest} tx", ts_list, tx_v))
    return result if result else None

def _parse_df_pt(path: Path) -> list[dict[str, Any]]:
    """Parse df -PT output (space-separated with multi-word mountpoints)."""
    result: list[dict[str, Any]] = []
    if not path.exists():
        return result
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    if len(lines) < 2:
        return result
    # Skip header, parse each line from the end (mountpoint may contain spaces)
    for line in lines[1:]:
        parts = line.split()
        if len(parts) < 7:
            continue
        # Last field is mountpoint; second-to-last is capacity (NN%)
        cap_str = parts[-2].rstrip("%")
        mp = parts[-1]
        result.append({
            "filesystem": parts[0],
            "fstype": parts[1],
            "blocks": safe_float(parts[2]),
            "used": safe_float(parts[3]),
            "available": safe_float(parts[4]),
            "usage_percent": safe_float(cap_str),
            "mountpoint": mp,
        })
    return result


def _parse_free_b(path: Path) -> dict[str, Any] | None:
    """Parse free -b output, returning Mem: row with bytes values."""
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8", errors="replace")
    for line in text.splitlines():
        if line.startswith("Mem:"):
            parts = line.split()
            if len(parts) >= 7:
                return {
                    "total": safe_float(parts[1]),
                    "used": safe_float(parts[2]),
                    "free": safe_float(parts[3]),
                    "available": safe_float(parts[6]),
                }
    return None

