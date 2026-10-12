#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Oracle 取数键静态审计：把「取表键」逐**语句**拉出来，核对它到底能不能命中。

为什么需要它：`ctx.tables.get(名字, [])` 在**键写错**与**查了 0 行**两种情况下都返回
`[]`，`ctx.find_table(...)` 还多一层"找不到就挑一个名字像的"子串兜底 —— 后者实测把
FRA 使用率的取数点兜到了 `快速恢复区（FRA）参数`（只有 NAME/VALUE、没有使用率列），
于是"命中"了却又算不出数，规则**永不触发**。这两类错都**不报错、不抛异常、测试不红**，
只有在报告里表现为"这项检查通过"（读者无法反驳）。本工具就是给它们配的尺子。

三档判据：

* 命中（`✓`）   —— 键在包里存在。**同时打印命中的那个键**（子串兜底的错命中看得见）。
* 未命中（`—`） —— 所有别名都不在包里。要么键写错，要么这一项本来就没采（两者报告
  里必须长得不一样，由规则层按 `source_status` 分开）。**需要人工过一遍。**
* 单边（`!`）   —— 只在一种包形态（1.4 / 2.0）里命中。这是批 19 抓出 3 条恒通过漏报的形态。

用法::

    python tools/check_oracle_lookup_keys.py <包1> [包2 …] [--json]
    # 不传包时自动找仓库内 1.4 / 2.0 两个样例包

⚠ 覆盖范围：只扫**判定层**（`plugins/oracle/metrics.py` + `plugins/oracle/oracle_rules.py`）——
`presentation.py` 的取数错不会改变任何结论，只会让某张表少几列/整项消失，那类错由呈现层
自己的测试（`tests/test_oracle_report_quality.py`）守。

★ 聚合口径：按**最近的闭合语句**归并，不按 `lineno`。同一表达式里的多行 `or` 链是
**一个**取数点 —— 按行聚合会造出三个假缺陷（`ORA.SECURITY.PASSWORD_EXPIRY` 曾被误报成
"三个别名全死"，而它实测在两个包上都正常触发）。这个口径由 `_self_proof()` 自证。
"""
from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

# 判定层：取数错了会**改变结论**的两个文件。
SOURCES = ("plugins/oracle/metrics.py", "plugins/oracle/oracle_rules.py")

# 自动探测样例包（仓库内的本地产物，缺失时跳过，不算失败）。
SAMPLE_PACKAGES = (
    ("1.4", "_work/_pkg14/oracle_inspection_task_*"),
    ("2.0", "_work/_b16_pkg/oracle_inspection_task_*"),
)

# 取数 API：加新 API 时**必须同步这里**，否则新调用点对尺子不可见（静默漏审）。
_TABLE_ATTRS = {"tables"}
_TABLE_GETTERS = {"find_table", "table_fetch", "find_table_fetch"}

# OS 层采集项：它们的键来自 `system.*`（由 `system_static` 模块产出，
# `evidence/filesystems.txt` → `_EVIDENCE_TABLES` 回填），**不在** `item_titles.json` 里
# （那份 JSON 只覆盖 Oracle SQL 标题）。列在这里就是"已知不在词表里但确实是合法键"。
ALLOWED_OS_KEYS = frozenset({
    "filesystems", "system.filesystems",
    "hugepages", "system.hugepages",
})


def _is_table_key_access(node: ast.AST) -> tuple[bool, list[str]]:
    """该节点是不是「取表键」；是则返回其中的字面量键。"""
    if isinstance(node, ast.Subscript):
        target = node.value
        if isinstance(target, ast.Attribute) and target.attr in _TABLE_ATTRS:
            index = node.slice
            if isinstance(index, ast.Constant) and isinstance(index.value, str):
                return True, [index.value]
        return False, []
    if not isinstance(node, ast.Call):
        return False, []
    func = node.func
    attr = getattr(func, "attr", "") or ""
    is_tables_get = (
        attr == "get"
        and isinstance(func.value, ast.Attribute)
        and func.value.attr in _TABLE_ATTRS
    )
    if not (is_tables_get or attr in _TABLE_GETTERS):
        return False, []
    return True, [a.value for a in node.args
                  if isinstance(a, ast.Constant) and isinstance(a.value, str)]


def _statement_of(tree: ast.AST) -> dict[int, int]:
    """每个节点 → 所属**语句**的行号（最近的 `stmt` 祖先）。"""
    owner: dict[int, int] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            owner[id(child)] = parent.lineno if isinstance(parent, ast.stmt) else owner.get(id(parent), 0)
    return owner


def sites_from_source(source: str) -> dict[int, list[str]]:
    """`{语句起始行: [取表键…]}`；同一表达式里的多条别名归到同一个取数点下。"""
    tree = ast.parse(source)
    owner = _statement_of(tree)
    found: dict[int, list[str]] = {}
    for node in ast.walk(tree):
        hit, names = _is_table_key_access(node)
        if not hit:
            continue
        key = owner.get(id(node), node.lineno)
        bucket = found.setdefault(key, [])
        for name in names:
            if name not in bucket:
                bucket.append(name)
    return found


def lookup_sites(path: Path) -> dict[int, list[str]]:
    return sites_from_source(path.read_text(encoding="utf-8"))


def _self_proof() -> tuple[bool, str]:
    """尺子自证：多行 `or` 链必须归成**一个**取数点，且字典取值不许被误抓。"""
    probe = (
        'rows = (ctx.tables.get("长名", [])\n'
        '        or ctx.tables.get("短名", [])\n'
        '        or ctx.tables.get("英文", []))\n'
        'other = ctx.table_fetch("甲", "乙")\n'
        'legacy = ctx.find_table("丙")\n'
        'noise = cfg.get("category")\n'
    )
    got = sites_from_source(probe)
    if len(got) != 3:
        return False, f"取数点个数应为 3，实际 {len(got)}：{got}"
    if set(got.get(1, [])) != {"长名", "短名", "英文"}:
        return False, f"多行 or 链没归成一个点：{got.get(1)}"
    if set(got.get(4, [])) != {"甲", "乙"}:
        return False, f"table_fetch 多参数没抓全：{got.get(4)}"
    if set(got.get(5, [])) != {"丙"}:
        return False, f"find_table 没抓到：{got.get(5)}"
    return True, "多行 or 链归并正确、table_fetch/find_table 均可见、字典取值未被误抓"


def _load_package(pattern: str):
    """按通配符找第一个包并加载；找不到返回 None。"""
    import glob

    from plugins.oracle.package_adapter import OraclePackageAdapter

    roots = [Path(p) for p in glob.glob(str(PROJECT_ROOT / pattern))]
    if not roots:
        return None
    work = PROJECT_ROOT / "_work" / "_keyaudit"
    return OraclePackageAdapter(work).load(roots[0])


def _resolve(tables: dict, names: list[str]) -> str:
    for name in names:
        if name in tables:
            return name
    return ""


def audit(packages: dict[str, dict],
          sources: dict[str, str] | None = None) -> dict:
    """逐取数点核对：命中哪个键 / 未命中 / 只有单边命中 / 写了从未登记过的名字。

    `sources` 可注入 `{相对路径: 源码文本}`，不传则从 `SOURCES` 读盘。
    ★ 留这个口是为了让**守门自己**被测：把故意违规的源码喂进**同一个** `audit()`，
      断言它真的变红 —— 否则尺子坏了会永远绿（`tests/test_oracle_lookup_keys.py`）。
    """
    from plugins.oracle.package_adapter import known_table_keys

    vocabulary = known_table_keys() | ALLOWED_OS_KEYS
    all_sites: dict[str, dict[int, list[str]]] = {}
    if sources is None:
        for rel in SOURCES:
            all_sites[rel] = lookup_sites(PROJECT_ROOT / rel)
    else:
        for rel, source in sources.items():
            all_sites[rel] = sites_from_source(source)

    rows: list[dict] = []
    for rel, found in all_sites.items():
        for line, names in sorted(found.items()):
            hits = {tag: _resolve(tables, names) for tag, tables in packages.items()}
            matched = [tag for tag, key in hits.items() if key]
            rows.append({
                "file": rel,
                "line": line,
                "keys": names,
                "unknown_keys": [name for name in names if name not in vocabulary],
                "resolved": hits,
                "verdict": "hit" if len(matched) == len(packages)
                else ("single" if matched else "missing"),
            })
    return {"packages": list(packages), "sites": rows}


def render(result: dict) -> str:
    tags = result["packages"]
    header = f"{'取数点':<44}" + "".join(f"{tag + ' 命中键':<30}" for tag in tags) + "判定"
    lines = [f"样例包：{' / '.join(tags) or '（无 —— 只做静态词表对账）'}", header]
    counts = {"hit": 0, "single": 0, "missing": 0}
    unknown_total = 0
    for row in result["sites"]:
        counts[row["verdict"]] += 1
        unknown_total += len(row["unknown_keys"])
        where = f"{Path(row['file']).name}:{row['line']}"
        cells = "".join(f"{row['resolved'].get(tag) or '—':<30}" for tag in tags)
        mark = {"hit": "✓", "single": "! 单边", "missing": "— 未命中"}[row["verdict"]]
        if row["unknown_keys"]:
            mark = "✗ 含未登记名"
        lines.append(f"{where:<44}{cells}{mark}  {' / '.join(row['keys'])}")
        if row["unknown_keys"]:
            lines.append(f"      ↑ 从未被登记的名字（拼错 / 已废弃）：{' / '.join(row['unknown_keys'])}")
    lines.append("")
    lines.append(f"共 {len(result['sites'])} 个取数点：命中 {counts['hit']} / 单边 {counts['single']}"
                 f" / 未命中 {counts['missing']} / 含未登记名 {unknown_total}")
    lines.append("提示：未命中与单边都要人工过一遍（键写错 ⇄ 这一项本来没采，报告里必须长得不一样）。")
    return "\n".join(lines)


def has_unregistered_names(result: dict) -> bool:
    """门禁判据：「写了从未登记过的名字」= 纯粹的错字，可以直接失败。

    「未命中 / 单边」**不**进门禁 —— 那可能是这一项本来就没采（要由规则层按
    `source_status` 写成「证据不足」），静态尺子分不清，只提示、交人工过。
    """
    return any(row["unknown_keys"] for row in result["sites"])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Oracle 取数键静态审计")
    parser.add_argument("packages", nargs="*", help="采集包（tar.gz 或解压目录）；省略则用仓库样例包")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args(argv)

    ok, proof = _self_proof()
    if not ok:
        print(f"✗ 尺子自证失败：{proof}")
        return 1
    if not args.json:
        print(f"✓ 尺子自证通过（{proof}）\n")

    if args.packages:
        from plugins.oracle.package_adapter import OraclePackageAdapter

        packages = {}
        for index, raw in enumerate(args.packages):
            path = Path(raw)
            packages[path.stem] = OraclePackageAdapter(
                PROJECT_ROOT / "_work" / f"_keyaudit_{index}"
            ).load(path).tables
    else:
        packages = {}
        for tag, pattern in SAMPLE_PACKAGES:
            ctx = _load_package(pattern)
            if ctx is not None:
                packages[tag] = ctx.tables

    result = audit(packages)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(render(result))
    # 「写了从未登记过的名字」= 纯粹的错字，可以直接当门禁（退出码非 0）。
    # 「未命中 / 单边」不一定是错（可能是这一项本来没采），只提示、不失败。
    return 1 if has_unregistered_names(result) else 0


if __name__ == "__main__":
    raise SystemExit(main())
