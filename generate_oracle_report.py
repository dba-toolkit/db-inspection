#!/usr/bin/env python3
"""Generate an Oracle report with the shared professional Word engine."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from inspection_core.package_io import read_json, write_json
from inspection_core.word_engine import GENERATOR_VERSION, default_output_path
from plugins.oracle.report_adapter import adapt_report_model
from plugins.oracle.word_report import ORACLE_WORD_PROFILE, OracleWordReportGenerator


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate a professional Oracle inspection DOCX.")
    parser.add_argument("input", help="report_model.json or its containing directory")
    parser.add_argument("--output", help="output .docx path")
    parser.add_argument("--customer", default="", help="customer name")
    parser.add_argument("--company", default=ORACLE_WORD_PROFILE.default_company)
    parser.add_argument("--target", default="", help="inspection target override")
    parser.add_argument("--author", default="王劲松")
    parser.add_argument("--reviewer", default="邓秋爽")
    parser.add_argument("--report-version", default="")
    parser.add_argument("--logo", default="logo.png")
    parser.add_argument("--layout", choices=("professional", "legacy"), default="professional")
    parser.add_argument("--standard-model", help="write the adapted standard report model to this path")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    source = Path(args.input).expanduser().resolve()
    model_path = source / "report_model.json" if source.is_dir() else source
    if not model_path.exists():
        print(f"ERROR: report_model.json not found: {model_path}", file=sys.stderr)
        return 1
    try:
        model = adapt_report_model(read_json(model_path))
        standard_path = (
            Path(args.standard_model).expanduser().resolve()
            if args.standard_model
            else model_path.parent / "report_model_standard.json"
        )
        write_json(standard_path, model)
        output = Path(args.output).expanduser().resolve() if args.output else default_output_path(
            model_path, model, ORACLE_WORD_PROFILE, args.target
        )
        logo = Path(args.logo).expanduser().resolve() if args.logo else None
        if logo is not None and not logo.exists():
            logo = None
        generated = OracleWordReportGenerator(
            standard_path, output, customer=args.customer, company=args.company,
            target=args.target, author=args.author, reviewer=args.reviewer,
            report_version=args.report_version, logo_path=logo, layout=args.layout,
        ).build()
        print(json.dumps({
            "status": "success",
            "database": "oracle",
            "output": str(generated),
            "generator_version": GENERATOR_VERSION,
            "report_contract": "oracle_inspection_report_model",
        }, ensure_ascii=False))
        return 0
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
