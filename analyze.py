#!/usr/bin/env python3
"""统一分析入口：按数据库类型分发到对应分析器，输出标准 report_model.json 与图表。"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent


def detect_db_type(source: Path) -> str | None:
    name = source.name.lower()
    if "mysql" in name:
        return "mysql"
    if "postgresql" in name or name.startswith("pg_inspection"):
        return "postgresql"
    if "oracle" in name:
        return "oracle"
    if "sqlserver" in name or "mssql" in name or "sql_server" in name:
        return "sqlserver"
    return None


def build_command(db_type: str, source: Path, output: Path, rules_config: Path | None) -> list[str]:
    if db_type == "mysql":
        cmd = [sys.executable, str(ROOT / "analyze_inspection_v2.py"), str(source), "--output", str(output)]
        if rules_config:
            cmd += ["--rules-config", str(rules_config)]
        return cmd
    if db_type == "postgresql":
        cmd = [sys.executable, str(ROOT / "analyze_postgresql.py"), str(source), "-o", str(output)]
        if rules_config:
            cmd += ["--rules", str(rules_config)]
        return cmd
    if db_type == "oracle":
        default_rules = ROOT / "plugins" / "oracle" / "inspection_rules_oracle.json"
        cmd = [sys.executable, str(ROOT / "analyze_oracle.py"), str(source), "--output", str(output)]
        cmd += ["--rules-config", str(rules_config or default_rules)]
        return cmd
    if db_type == "sqlserver":
        default_rules = ROOT / "plugins" / "sqlserver" / "inspection_rules.json"
        cmd = [sys.executable, str(ROOT / "analyze_sqlserver.py"), str(source), "--output", str(output)]
        cmd += ["--rules-config", str(rules_config or default_rules)]
        return cmd
    raise ValueError(f"unknown db_type: {db_type}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Analyze a database inspection package (mysql/postgresql/oracle/sqlserver).")
    parser.add_argument("source", help="采集包 zip/tar.gz 或已解压目录")
    parser.add_argument("--db-type", choices=["mysql", "postgresql", "oracle", "sqlserver"], help="数据库类型，缺省按文件名自动识别")
    parser.add_argument("-o", "--output", default="analysis_output", help="输出目录")
    parser.add_argument("--rules-config", help="规则 JSON 路径（缺省使用各库自带）")
    args = parser.parse_args()

    source = Path(args.source).expanduser().resolve()
    if not source.exists():
        print(f"ERROR: 输入不存在: {source}", file=sys.stderr)
        return 1

    db_type = args.db_type or detect_db_type(source)
    if not db_type:
        print("ERROR: 无法从文件名识别数据库类型，请用 --db-type 指定 mysql/postgresql/oracle/sqlserver", file=sys.stderr)
        return 1

    output = Path(args.output).expanduser().resolve()
    rules_config = Path(args.rules_config).expanduser().resolve() if args.rules_config else None
    cmd = build_command(db_type, source, output, rules_config)
    print(f"[analyze] 数据库类型: {db_type}")
    print(f"[analyze] 执行: {' '.join(str(x) for x in cmd)}")
    return subprocess.call(cmd)


if __name__ == "__main__":
    raise SystemExit(main())
