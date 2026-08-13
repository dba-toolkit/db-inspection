"""SQL Server 分析器流程编排器。"""

from __future__ import annotations

import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

from inspection_core.package_io import write_json

from .charts import build_charts
from .metrics import num  # noqa: F401  (kept for parity with monolith scope)
from .package_adapter import discover_snapshot, normalize_snapshot, read_json
from .presentation import build_model
from .rule_provider import SQLServerRuleProvider


ANALYZER_VERSION = "1.1.0"


def analyze_sqlserver(source: Path, output: Path, rules_config: Path) -> dict[str, Any]:
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="sqlserver_inspection_") as tmp:
        path = discover_snapshot(source, Path(tmp))
        snapshot = normalize_snapshot(read_json(path))

    config = read_json(rules_config)
    ignored_waits = {str(x).upper() for x in config.get("ignored_wait_types", [])}
    snapshot["wait_stats"] = [
        x for x in snapshot.get("wait_stats", [])
        if str(x.get("wait_type", "")).upper() not in ignored_waits
    ]

    collected = snapshot.get("collection", {}).get("finished_at") or datetime.now().astimezone().isoformat()
    try:
        now = datetime.fromisoformat(str(collected).replace("Z", "+00:00"))
    except ValueError:
        now = datetime.now().astimezone()

    findings = SQLServerRuleProvider(config, now).evaluate(snapshot)
    charts = build_charts(snapshot, findings, output / "charts")
    model = build_model(snapshot, findings, charts)

    write_json(output / "analysis.json", {"snapshot": snapshot, "findings": findings})
    write_json(output / "report_model.json", model)
    lines = [
        "SQL Server 巡检分析完成",
        f"健康评分: {model['health']['score']} ({model['health']['grade']})",
        f"发现项: {len(findings)}",
        f"采集完整度: {model['collection_quality']['score_pct']}%",
    ]
    (output / "analysis_summary.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return model
