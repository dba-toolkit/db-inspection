#!/usr/bin/env python3
"""从采集脚本生成 `plugins/oracle/item_titles.json`（item_id → Markdown 标题）。

**单一来源 = `inspection/oracle_inspection.sh`**（每个 `ora_sql_to_md "<id>" "<标题>"` 调用点）。
本脚本只做"抽取 + 落盘"，不引入第二份映射表；`--check` 模式供测试守门用（不一致即退出码 1）。

为什么需要这份表：v2.0 的 `tables/<item_id>.psv` 只带**列名行**，标题只存在于采集脚本与
人读 md 里；而 Python 侧（`metrics.py` / `oracle_rules.py` / `presentation.py`）是按 **1.4 的中文标题**写的
查找键。adapter 要把同一张表同时注册到 `item_id`、`中文标题`、以及"尾段"别名下，
下游才不需要改。

用法：
    python tools/gen_oracle_item_titles.py            # 写入 plugins/oracle/item_titles.json
    python tools/gen_oracle_item_titles.py --check     # 只校验，不写
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COLLECTOR = ROOT / "inspection" / "oracle_inspection.sh"
TARGET = ROOT / "plugins" / "oracle" / "item_titles.json"

# 注意：`ora_sql_to_md <item_id> <标题> <SQL>`，标题是第 2 个引号串；SQL 可跨行，故不做行锚定。
_CALL_RE = re.compile(r'ora_sql_to_md\s+"([^"]+)"\s+"([^"]*)"')


def extract(collector_path: Path = COLLECTOR) -> dict[str, str]:
    text = collector_path.read_text(encoding="utf-8", errors="replace")
    seen: dict[str, str] = {}
    duplicates: list[str] = []
    for item_id, title in _CALL_RE.findall(text):
        if item_id in seen:
            duplicates.append(item_id)
        seen[item_id] = title
    if duplicates:
        raise SystemExit(f"采集脚本里出现重复 item_id（契约：同一 id 只能一次）: {sorted(set(duplicates))}")
    if not seen:
        raise SystemExit(f"未从 {collector_path} 抽到任何 ora_sql_to_md 调用点")
    return {k: seen[k] for k in sorted(seen)}


def dumps(mapping: dict[str, str]) -> str:
    return json.dumps(mapping, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="生成 Oracle item_id → 标题 对照表")
    parser.add_argument("--check", action="store_true", help="只校验现有文件是否与采集脚本一致")
    parser.add_argument("--out", default=str(TARGET), help="输出路径")
    args = parser.parse_args()

    mapping = extract()
    payload = dumps(mapping)
    out = Path(args.out)

    if args.check:
        if not out.exists():
            print(f"FAIL: {out} 不存在，请先运行本脚本生成", file=sys.stderr)
            return 1
        current = out.read_text(encoding="utf-8")
        if json.loads(current) != mapping:
            print(f"FAIL: {out} 与 {COLLECTOR} 不一致，请重新生成", file=sys.stderr)
            return 1
        print(f"OK: {out} 与采集脚本一致（{len(mapping)} 项）")
        return 0

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(payload, encoding="utf-8", newline="\n")
    print(f"OK: 写入 {out}（{len(mapping)} 项）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
