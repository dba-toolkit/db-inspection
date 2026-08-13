"""SQL Server ????????"""

from __future__ import annotations

import math
from typing import Any


def num(v: Any) -> float | None:
    try:
        x = float(v)
        return x if math.isfinite(x) else None
    except (TypeError, ValueError):
        return None

def score(findings: list[dict[str, Any]], quality_pct: float) -> tuple[int, str]:
    weights = {"critical": 18, "high": 10, "medium": 4, "low": 1}
    raw = max(0, 100 - sum(weights.get(str(x.get("severity")), 0) for x in findings))
    result = round(raw * (0.85 + 0.15 * quality_pct / 100))
    grade = "健康" if result >= 90 else "良好" if result >= 75 else "关注" if result >= 60 else "高风险"
    return result, grade

