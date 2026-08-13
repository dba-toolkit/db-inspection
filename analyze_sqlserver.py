#!/usr/bin/env python3
"""SQL Server 分析独立入口（插件化分析器）。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from plugins.sqlserver.analyzer import analyze_sqlserver


def main() -> int:
    parser = argparse.ArgumentParser(description="Analyze SQL Server inspection package")
    parser.add_argument("input", help="snapshot.json, extracted directory, or zip package")
    parser.add_argument("--output", default="analysis_output")
    parser.add_argument("--rules-config", default=str(Path(__file__).parent / "plugins" / "sqlserver" / "inspection_rules.json"))
    args = parser.parse_args()
    try:
        output = Path(args.output).resolve()
        rules_config = Path(args.rules_config).resolve()
        model = analyze_sqlserver(Path(args.input).resolve(), output, rules_config)
        print(f"SQL Server 巡检分析完成")
        print(f"  健康评分: {model['health']['score']} ({model['health']['grade']})")
        print(f"  发现项: {len(model['findings'])}")
        print(f"  采集完整度: {model['collection_quality']['score_pct']}%")
        print(f"  报告模型: {output / 'report_model.json'}")
        return 0
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
