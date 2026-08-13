"""Database-neutral statistical helpers for inspection metrics."""

from __future__ import annotations

import math
import statistics
from collections.abc import Iterable, Sequence
from typing import Any


def percentile(values: Sequence[float], p: float) -> float | None:
    data = sorted(value for value in values if math.isfinite(value))
    if not data:
        return None
    if len(data) == 1:
        return data[0]
    rank = (len(data) - 1) * p
    low = math.floor(rank)
    high = math.ceil(rank)
    if low == high:
        return data[low]
    return data[low] + (data[high] - data[low]) * (rank - low)


def summarize(values: Iterable[float]) -> dict[str, Any]:
    data = [float(value) for value in values if value is not None and math.isfinite(float(value))]
    if not data:
        return {"count": 0, "min": None, "average": None, "p95": None, "max": None}
    return {
        "count": len(data),
        "min": round(min(data), 4),
        "average": round(statistics.fmean(data), 4),
        "p95": round(percentile(data, 0.95) or 0.0, 4),
        "max": round(max(data), 4),
    }
