"""SQL Server 指标与健康评分。"""

from __future__ import annotations

import math
from typing import Any


# 严重度权重：score() 用它算分，报告模型把它写进 health_assessment.scoring_policy
# 供共享引擎在首页复述扣分算式。两处必须同源 —— 首页那段话的全部价值就在于
# 「读者能拿它把分数核对一遍」，权重一旦漂移，它就从说明变成一段看起来精确的错话。
SEVERITY_WEIGHTS = {"critical": 18, "high": 10, "medium": 4, "low": 1}


def num(v: Any) -> float | None:
    try:
        x = float(v)
        return x if math.isfinite(x) else None
    except (TypeError, ValueError):
        return None

def score(findings: list[dict[str, Any]], quality_pct: float) -> tuple[int, str]:
    raw = max(0, 100 - sum(SEVERITY_WEIGHTS.get(str(x.get("severity")), 0) for x in findings))
    result = round(raw * (0.85 + 0.15 * quality_pct / 100))
    grade = "健康" if result >= 90 else "良好" if result >= 75 else "关注" if result >= 60 else "高风险"
    return result, grade

