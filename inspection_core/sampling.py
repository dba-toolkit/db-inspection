"""Shared sampling-quality evaluation for Linux SAR history."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any

from .models import PackageContext
from .values import safe_float

# A SAR history is written at a fixed interval; anything wider than this is a
# collection gap, not a sampling interval, and must not be counted as coverage.
_MAX_SAMPLE_GAP_SECONDS = 3600.0


def parse_time(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    normalized = re.sub(r"\s+UTC$", "+00:00", text, flags=re.IGNORECASE)
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


def effective_coverage_hours(rows: list[dict[str, Any]], window_hours: float = 24.0) -> float:
    """Measure the span actually covered inside the latest window.

    The collectors report a ``coverage_hours`` number of their own, but the
    analyzer must be able to verify it from the timestamps: a file that spans
    three hours with a two-hour hole does not describe those three hours.
    Gaps wider than one hour are treated as missing time.
    """
    timestamps = sorted({
        moment for row in rows
        if (moment := parse_time(row.get("timestamp"))) is not None
    })
    if len(timestamps) < 2:
        return 0.0
    cutoff = timestamps[-1] - timedelta(hours=window_hours)
    timestamps = [moment for moment in timestamps if moment >= cutoff]
    covered_seconds = sum(
        gap for earlier, later in zip(timestamps, timestamps[1:])
        if 0 < (gap := (later - earlier).total_seconds()) <= _MAX_SAMPLE_GAP_SECONDS
    )
    return round(covered_seconds / 3600, 2)


def sar_history_quality(ctx: PackageContext) -> dict[str, Any]:
    sampling = ctx.snapshot.get("sampling", {})
    declared = sampling.get("sar_history", {})
    requested = safe_float(sampling.get("sar_history_requested_hours")) or 24.0
    coverage = safe_float(declared.get("coverage_hours")) or 0.0
    cpu_rows = [row for row in ctx.history.get("sar_cpu", []) if row.get("CPU") in {"-1", "all", "ALL"}]
    last = parse_time(declared.get("last_timestamp"))
    finished = parse_time(ctx.snapshot.get("collector", {}).get("finished_at"))
    age_minutes = None
    if last and finished:
        age_minutes = round(
            (finished.astimezone(timezone.utc) - last.astimezone(timezone.utc)).total_seconds() / 60.0,
            1,
        )
    coverage_ratio = min(1.0, coverage / requested) if requested > 0 else 0.0
    fresh = age_minutes is not None and -5 <= age_minutes <= 30
    enough_points = len(cpu_rows) >= 20
    usable = declared.get("status") == "ok" and coverage_ratio >= 0.8 and fresh and enough_points
    reasons: list[str] = []
    if coverage_ratio < 0.8:
        reasons.append(f"历史覆盖仅 {coverage:.2f}/{requested:.0f} 小时")
    if not fresh:
        if age_minutes is None:
            reasons.append("无法确认历史数据的新鲜度")
        else:
            reasons.append(f"最后历史点距采集结束约 {age_minutes:.1f} 分钟")
    if not enough_points:
        reasons.append(f"历史 CPU 有效点仅 {len(cpu_rows)} 个")
    return {
        "status": "usable" if usable else "unusable",
        "usable_for_trend_rules": usable,
        "requested_hours": requested,
        "coverage_hours": coverage,
        "coverage_ratio": round(coverage_ratio, 4),
        "cpu_point_count": len(cpu_rows),
        "first_timestamp": declared.get("first_timestamp"),
        "last_timestamp": declared.get("last_timestamp"),
        "age_at_collection_minutes": age_minutes,
        "reasons": reasons,
    }
