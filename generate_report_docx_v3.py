#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Compatibility CLI for generating a MySQL inspection Word report."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from inspection_core.word_engine import (
    GENERATOR_VERSION,
    default_output_path as _default_output_path,
    load_json as _load_json,
)
from plugins.mysql.word_report import MYSQL_WORD_PROFILE, MySQLWordReportGenerator


ReportGenerator = MySQLWordReportGenerator


def load_json(path: Path) -> dict[str, Any]:
    """Keep the legacy MySQL-only helper signature."""
    return _load_json(path, MYSQL_WORD_PROFILE)


def default_output_path(model_path: Path, model: dict[str, Any], target: str = "") -> Path:
    """Keep the legacy MySQL-only filename helper signature."""
    return _default_output_path(model_path, model, MYSQL_WORD_PROFILE, target)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate a professional MySQL inspection DOCX from report_model.json."
    )
    parser.add_argument("input", help="report_model.json or its containing directory")
    parser.add_argument("--output", help="output .docx path")
    parser.add_argument("--customer", default="", help="customer name")
    parser.add_argument(
        "--company",
        default=MYSQL_WORD_PROFILE.default_company,
        help="company name shown in the first-page header",
    )
    parser.add_argument("--target", default="", help="inspection target name override")
    parser.add_argument("--author", default="王劲松", help="author/preparer")
    parser.add_argument("--reviewer", default="邓秋爽", help="reviewer")
    parser.add_argument("--report-version", default="", help="report version override")
    parser.add_argument("--logo", default="logo.png", help="path to logo image")
    parser.add_argument(
        "--layout",
        choices=("professional", "legacy"),
        default="professional",
        help="professional: facts first and chapter conclusions; legacy: 4.0 layout",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    source = Path(args.input).expanduser().resolve()
    model_path = source / "report_model.json" if source.is_dir() else source
    if not model_path.exists():
        print(f"ERROR: report_model.json not found: {model_path}", file=sys.stderr)
        return 1
    output = (
        Path(args.output).expanduser().resolve()
        if args.output
        else default_output_path(model_path, load_json(model_path), args.target)
    )
    try:
        logo_path = Path(args.logo).expanduser().resolve() if args.logo else None
        if logo_path is not None and not logo_path.exists():
            logo_path = None
        generated = ReportGenerator(
            model_path,
            output,
            customer=args.customer,
            author=args.author,
            reviewer=args.reviewer,
            report_version=args.report_version,
            logo_path=logo_path,
            target=args.target,
            company=args.company,
            layout=args.layout,
        ).build()
        print(
            json.dumps(
                {
                    "status": "success",
                    "output": str(generated),
                    "generator_version": GENERATOR_VERSION,
                },
                ensure_ascii=False,
            )
        )
        return 0
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
