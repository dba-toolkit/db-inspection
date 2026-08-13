#!/usr/bin/env python3
"""Independent PostgreSQL analysis entry backed by the PostgreSQL plug-in."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

from inspection_core.package_io import read_json, write_json
from plugins.postgresql.analyzer import ANALYZER_VERSION, Analyzer
from plugins.postgresql.report_adapter import adapt_report_model


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Analyze PostgreSQL inspection package(s).")
    parser.add_argument("sources", nargs="+", help="tar.gz packages or extracted package directories")
    parser.add_argument("-o", "--output", default="analysis_output_postgresql", help="output directory")
    parser.add_argument("--rules", help="PostgreSQL rules JSON path")
    parser.add_argument("--keep-extracted", action="store_true", help="keep temporary extracted package contents")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    output = Path(args.output).expanduser().resolve()
    sources = [Path(value).expanduser().resolve() for value in args.sources]
    missing = [str(path) for path in sources if not path.exists()]
    if missing:
        print(f"ERROR: input does not exist: {', '.join(missing)}", file=sys.stderr)
        return 1
    rules = Path(args.rules).expanduser().resolve() if args.rules else None
    try:
        analyzer = Analyzer(output, rules_config=rules)
        analyzer.analyze(sources)
        legacy_path = output / "report_model.json"
        legacy_copy = output / "report_model_legacy.json"
        shutil.copy2(legacy_path, legacy_copy)
        write_json(legacy_path, adapt_report_model(read_json(legacy_copy)))
        if not args.keep_extracted:
            shutil.rmtree(output / "_work", ignore_errors=True)
        print(json.dumps({
            "status": "success",
            "database": "postgresql",
            "analyzer_version": ANALYZER_VERSION,
            "output": str(output),
            "report_contract": "postgresql_inspection_report_model",
            "legacy_report_model": str(legacy_copy),
        }, ensure_ascii=False))
        return 0
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
