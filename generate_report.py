#!/usr/bin/env python3
"""统一 Word 生成入口：按 generator_contract 分发到对应 Word Profile，纯渲染。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from inspection_core.package_io import read_json, write_json
from inspection_core.word_engine import GENERATOR_VERSION, default_output_path
from plugins.mysql.word_report import MYSQL_WORD_PROFILE, MySQLWordReportGenerator
from plugins.postgresql.word_report import POSTGRESQL_WORD_PROFILE, PostgreSQLWordReportGenerator
from plugins.oracle.word_report import ORACLE_WORD_PROFILE, OracleWordReportGenerator
from plugins.sqlserver.word_report import SQLSERVER_WORD_PROFILE, SQLServerWordReportGenerator


GENERATORS = {
    "mysql_inspection_report_model": (MYSQL_WORD_PROFILE, MySQLWordReportGenerator),
    "postgresql_inspection_report_model": (POSTGRESQL_WORD_PROFILE, PostgreSQLWordReportGenerator),
    "oracle_inspection_report_model": (ORACLE_WORD_PROFILE, OracleWordReportGenerator),
    "sqlserver_inspection_report_model": (SQLSERVER_WORD_PROFILE, SQLServerWordReportGenerator),
}


def adapt_if_needed(contract: str, model: dict) -> dict:
    if contract == "oracle_inspection_report_model":
        from plugins.oracle.report_adapter import adapt_report_model
        return adapt_report_model(model)
    if contract == "sqlserver_inspection_report_model":
        from plugins.sqlserver.report_adapter import adapt_report_model
        return adapt_report_model(model)
    return model


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate a professional inspection DOCX from report_model.json.")
    parser.add_argument("input", help="report_model.json 或其所在目录")
    parser.add_argument("--output", help="输出 .docx 路径")
    parser.add_argument("--customer", default="")
    parser.add_argument("--company", default="")
    parser.add_argument("--target", default="")
    parser.add_argument("--author", default="王劲松")
    parser.add_argument("--reviewer", default="邓秋爽")
    parser.add_argument("--report-version", default="")
    parser.add_argument("--logo", default="logo.png")
    parser.add_argument("--layout", choices=("professional", "legacy"), default="professional")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    source = Path(args.input).expanduser().resolve()
    model_path = source / "report_model.json" if source.is_dir() else source
    if not model_path.exists():
        print(f"ERROR: report_model.json 不存在: {model_path}", file=sys.stderr)
        return 1

    model = read_json(model_path)
    contract = model.get("generator_contract")
    if contract not in GENERATORS:
        print(f"ERROR: 不支持的报告契约: {contract}", file=sys.stderr)
        return 1

    profile, generator_cls = GENERATORS[contract]
    model = adapt_if_needed(contract, model)

    render_path = model_path
    if contract in {"oracle_inspection_report_model", "sqlserver_inspection_report_model"}:
        render_path = model_path.parent / "report_model_standard.json"
        write_json(render_path, model)

    output = Path(args.output).expanduser().resolve() if args.output else default_output_path(
        model_path, model, profile, args.target
    )
    logo = Path(args.logo).expanduser().resolve() if args.logo else None
    if logo is not None and not logo.exists():
        logo = None

    generated = generator_cls(
        render_path, output, customer=args.customer, company=args.company or profile.default_company,
        target=args.target, author=args.author, reviewer=args.reviewer,
        report_version=args.report_version, logo_path=logo, layout=args.layout,
    ).build()

    print(json.dumps({
        "status": "success",
        "contract": contract,
        "output": str(generated),
        "generator_version": GENERATOR_VERSION,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
