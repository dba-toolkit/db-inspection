"""Time and SAR-history normalisation shared by every database plugin.

Collectors write Linux SAR history into the package as CSV.  The column names
and the "CPU summary row" marker differ slightly between collectors, and every
plugin used to re-implement timestamp parsing and gap detection.  This module
owns those rules once.
"""
from __future__ import annotations

import math
import re
import statistics
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..models import PackageContext
from ..sampling import parse_time
from ..statistics import summarize
from ..values import safe_float

__all__ = [
    "CPU_SUMMARY_VALUES",
    "SAR_CPU_ALIASES",
    "busy_percent",
    "display_timezone",
    "finite",
    "gap_indices",
    "parse_chart_time",
    "parse_time",
    "sar_cpu_summary_rows",
    "series_values",
    "summarize",
]

# `sar -u` marks the aggregate line as CPU "-1" on some builds and "all"/"ALL"
# on others.  All three mean the same thing: the whole-host summary row.
# Every consumer must use this tuple — three different filters existed before.
CPU_SUMMARY_VALUES = ("-1", "all", "ALL")

# The two collectors spell the same SAR columns differently: the MySQL script
# feeds `sadf` output verbatim (`%user`/`%system`), the Oracle script writes
# sysstat's short form (`%usr`/`%sys`).  PostgreSQL grew its own workaround for
# this before the common layer existed.  Normalise here, once.
SAR_CPU_ALIASES: dict[str, tuple[str, ...]] = {
    "%user": ("%usr",),
    "%system": ("%sys",),
}

_TIME_KEYS = ("timestamp", "ts", "time")

_UTC_SUFFIX = re.compile(r"\s+UTC$", flags=re.IGNORECASE)


def finite(value: Any) -> float | None:
    """Parse a scalar and drop NaN/Inf so charts never plot undefined points."""
    parsed = safe_float(value)
    if parsed is None or not math.isfinite(parsed):
        return None
    return parsed


def _ts_offset(value: Any) -> Any:
    """Return the UTC offset carried by a timestamp, or None when it carries none."""
    raw = str(value or "").strip()
    if not raw:
        return None
    normalized = _UTC_SUFFIX.sub("+00:00", raw)
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    return parsed.tzinfo


def _data_timezone(ctx: PackageContext) -> Any:
    """Derive the display timezone from the collected timestamps themselves.

    Oracle packages carry no ``time_evidence`` block, and their realtime rows
    are written with an explicit offset.  Without this fallback the whole
    report would silently be drawn on the UTC clock.
    """
    for block in (ctx.timeseries, ctx.history):
        for name in sorted(block or {}):
            for row in block[name] or []:
                for key in _TIME_KEYS:
                    offset = _ts_offset(row.get(key))
                    if offset is not None:
                        return offset
    return None


def _offset_label(tzinfo: Any) -> str:
    name = str(getattr(tzinfo, "key", "") or "").strip()
    if name:
        return name
    offset = tzinfo.utcoffset(None) if hasattr(tzinfo, "utcoffset") else None
    if offset is None:
        return "UTC"
    total_minutes = int(offset.total_seconds() // 60)
    if total_minutes == 0:
        return "UTC"
    sign = "+" if total_minutes > 0 else "-"
    hours, minutes = divmod(abs(total_minutes), 60)
    return f"UTC{sign}{hours:02d}:{minutes:02d}"


def display_timezone(ctx: PackageContext) -> tuple[str, Any]:
    """Resolve the timezone used for chart axes and time labels.

    Preference order: the collector's recorded timezone, then the offset carried
    by the timestamps themselves, then UTC — so the axis label never claims a
    zone the data is not actually in.
    """
    raw = ctx.snapshot.get("time_evidence")
    if isinstance(raw, dict):
        name = str(raw.get("timezone") or "").strip()
        if name:
            try:
                return name, ZoneInfo(name)
            except (ZoneInfoNotFoundError, ValueError, KeyError):
                pass
    offset_tz = _data_timezone(ctx)
    if offset_tz is not None:
        return _offset_label(offset_tz), offset_tz
    return "UTC", timezone.utc


def parse_chart_time(value: Any, display_tz: Any) -> datetime | None:
    """Parse a collected timestamp and convert it to the display timezone.

    Naive timestamps are assumed to already be in the display timezone, which
    matches how the collectors write local host time.
    """
    raw = str(value or "").strip()
    if not raw:
        return None
    normalized = _UTC_SUFFIX.sub("+00:00", raw)
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=display_tz)
    return parsed.astimezone(display_tz)


def gap_indices(parsed: list[datetime | None]) -> set[int]:
    """Return the points that begin a new segment after a material time gap.

    Plotting across a multi-hour hole invents a trend that never happened, so
    the renderer inserts a None break before each returned index.
    """
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


def _with_aliases(row: dict[str, Any]) -> dict[str, Any]:
    """Add canonical SAR CPU column names when the collector used a variant spelling."""
    filled: dict[str, Any] = {}
    for canonical, aliases in SAR_CPU_ALIASES.items():
        if canonical in row:
            continue
        for alias in aliases:
            if alias in row:
                filled[canonical] = row[alias]
                break
    if not filled:
        return row
    return {**row, **filled}


def sar_cpu_summary_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep only whole-host CPU rows, with ``%user``/``%system`` guaranteed present.

    The returned rows are the caller's own dicts unless an alias had to be filled
    in, so callers may keep using either spelling of the source columns.
    """
    return [
        _with_aliases(row) for row in rows
        if str(row.get("CPU", "")) in CPU_SUMMARY_VALUES
    ]


def series_values(rows: list[dict[str, Any]], key: str, scale: float = 1.0,
                  offset: float = 0.0) -> list[float | None]:
    """Extract one numeric column, preserving None so gaps stay visible."""
    result: list[float | None] = []
    for row in rows:
        value = finite(row.get(key))
        result.append(None if value is None else value * scale + offset)
    return result


def busy_percent(rows: list[dict[str, Any]], idle_key: str = "%idle") -> list[float | None]:
    """Convert a SAR ``%idle`` column into a busy percentage."""
    return series_values(rows, idle_key, scale=-1.0, offset=100.0)
