#!/usr/bin/env python3
"""统一分析入口：按数据库类型分发到对应分析器，输出标准 report_model.json 与图表。"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from inspection_core.preflight import preflight_report_dependencies


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


def resolve_db_type(sources: list[Path]) -> str | None:
    """按输入逐个识别数据库类型；识别出的类型必须唯一。

    多包输入时逐个尝试（首个能识别的为准），并拒绝把不同库的包混在一次分析里——
    各库分析器只认自己那套采集包结构，混着传只会在子进程里报出更难懂的错。
    """
    detected = [detect_db_type(source) for source in sources]
    found = {item for item in detected if item}
    if len(found) > 1:
        detail = "、".join(
            f"{Path(src).name} -> {db}" for src, db in zip(sources, detected)
        )
        raise ValueError(f"输入包含多种数据库类型的采集包，请分开分析：{detail}")
    return next(iter(found)) if found else None


def build_command(db_type: str, sources: list[Path], output: Path, rules_config: Path | None) -> list[str]:
    """把统一入口的参数翻译成各库分析器的子进程命令行。

    各库分析器已收进 plugins/<db>/：mysql 是 plugins/mysql/analyzer.py（编排器本体），
    postgresql / oracle / sqlserver 是 plugins/<db>/cli.py（薄命令行适配层）。

    多包输入原样透传。mysql / postgresql / oracle 的本体本就是 ``nargs="+"``
    （多实例/主从合并分析），这里必须一起传，否则统一入口会把它们降级成单包。
    sqlserver 的本体只接受单包，多传时在这里就报清楚，而不是丢给子进程去报 argparse 错。
    """
    if db_type == "mysql":
        cmd = [sys.executable, str(ROOT / "plugins" / "mysql" / "analyzer.py"), *map(str, sources), "--output", str(output)]
        if rules_config:
            cmd += ["--rules-config", str(rules_config)]
        return cmd
    if db_type == "postgresql":
        cmd = [sys.executable, str(ROOT / "plugins" / "postgresql" / "cli.py"), *map(str, sources), "-o", str(output)]
        if rules_config:
            cmd += ["--rules", str(rules_config)]
        return cmd
    if db_type == "oracle":
        default_rules = ROOT / "plugins" / "oracle" / "inspection_rules_oracle.json"
        cmd = [sys.executable, str(ROOT / "plugins" / "oracle" / "cli.py"), *map(str, sources), "--output", str(output)]
        cmd += ["--rules-config", str(rules_config or default_rules)]
        return cmd
    if db_type == "sqlserver":
        if len(sources) > 1:
            raise ValueError(
                f"SQL Server 分析器一次只接受一个采集包，收到 {len(sources)} 个；请分开分析。"
            )
        default_rules = ROOT / "plugins" / "sqlserver" / "inspection_rules.json"
        cmd = [sys.executable, str(ROOT / "plugins" / "sqlserver" / "cli.py"), *map(str, sources), "--output", str(output)]
        cmd += ["--rules-config", str(rules_config or default_rules)]
        return cmd
    raise ValueError(f"unknown db_type: {db_type}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Analyze one or more database inspection packages (mysql/postgresql/oracle/sqlserver).",
        epilog="传多个采集包即做多实例合并分析（主从/多节点拓扑由分析器自动识别）。",
    )
    parser.add_argument("sources", nargs="+", help="采集包 zip/tar.gz 或已解压目录，可多个（多实例/主从）")
    parser.add_argument("--db-type", choices=["mysql", "postgresql", "oracle", "sqlserver"], help="数据库类型，缺省按输入文件名自动识别")
    parser.add_argument("-o", "--output", default="analysis_output", help="输出目录")
    parser.add_argument("--rules-config", help="规则 JSON 路径（缺省使用各库自带）")
    args = parser.parse_args()

    # 图表渲染器缺 matplotlib 时会静默降级成 Pillow 手绘版，只在交付物里看得出来，
    # 因此在入口把"当前解释器是谁、缺什么"先讲清楚（致命缺失才阻断）。
    if preflight_report_dependencies(ROOT / "requirements.txt"):
        return 1

    sources = [Path(item).expanduser().resolve() for item in args.sources]
    missing = [path for path in sources if not path.exists()]
    if missing:
        for path in missing:
            print(f"ERROR: 输入不存在: {path}", file=sys.stderr)
        return 1

    if args.db_type:
        db_type = args.db_type
    else:
        try:
            db_type = resolve_db_type(sources)
        except ValueError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
    if not db_type:
        print("ERROR: 无法从文件名识别数据库类型，请用 --db-type 指定 mysql/postgresql/oracle/sqlserver", file=sys.stderr)
        return 1

    output = Path(args.output).expanduser().resolve()
    rules_config = Path(args.rules_config).expanduser().resolve() if args.rules_config else None
    try:
        cmd = build_command(db_type, sources, output, rules_config)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(f"[analyze] 数据库类型: {db_type}")
    print(f"[analyze] 采集包: {len(sources)} 个")
    print(f"[analyze] 执行: {' '.join(str(x) for x in cmd)}")
    return subprocess.call(cmd)


if __name__ == "__main__":
    raise SystemExit(main())
