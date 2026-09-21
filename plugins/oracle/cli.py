#!/usr/bin/env python3
"""Oracle 分析独立入口（插件化分析器）。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence

# 直接以脚本方式运行本文件时把项目根补回 sys.path，保证 plugins.* 可导入。
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from plugins.oracle.analyzer import OracleAnalyzer


class OracleAnalyzerError(RuntimeError):
    pass


def discover_sources(inputs: Sequence[str]) -> list[Path]:
    result: list[Path] = []
    for raw in inputs:
        path = Path(raw).expanduser().resolve()
        if not path.exists():
            raise OracleAnalyzerError(f"Input does not exist: {path}")
        if path.is_dir() and (path / "snapshot.json").exists():
            result.append(path)
        elif path.is_dir():
            archives = sorted([*path.glob("*.tar.gz"), *path.glob("*.tgz"), *path.glob("*.tar")])
            if archives:
                result.extend(archives)
            else:
                md_files = sorted(path.glob("oracle_inspection_*.md"))
                if md_files:
                    result.extend(md_files)
                else:
                    raise OracleAnalyzerError(f"No packages or .md files in: {path}")
        else:
            result.append(path)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Oracle inspection package analyzer v2.0")
    parser.add_argument("inputs", nargs="+", help="Tar.gz packages, extracted dirs, .md files, or dirs containing them")
    parser.add_argument("--output", default="analysis_output", help="Output directory")
    parser.add_argument("--keep-extracted", action="store_true", help="Keep extracted packages")
    parser.add_argument("--rules-config", default=None, help="Path to inspection_rules_oracle.json")
    parser.add_argument("--verbose", action="store_true", help="Show detailed stage log")
    args = parser.parse_args()

    try:
        sources = discover_sources(args.inputs)
        output = Path(args.output).expanduser().resolve()
        rules_config = Path(args.rules_config).expanduser().resolve() if args.rules_config else None
        analyzer = OracleAnalyzer(output, args.keep_extracted, rules_config)
        analysis = analyzer.analyze(sources)
        total = analysis["overall_health_summary"]
        print(f"OK: {output}/report_model.json")
        print(f"  Instances: {len(analysis.get('instances', []))}")
        total_findings = sum(len(inst.get("findings", [])) for inst in analysis.get("instances", []))
        print(f"  Findings: {total_findings} (High:{total['high_count']} Med:{total['medium_count']} Low:{total['low_count']})")
        print(f"  Score: {total['score']}/100")
        return 0
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
