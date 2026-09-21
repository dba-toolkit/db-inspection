"""采集包消费面自检：一份包「采了什么 / 分析真读了多少 / 什么没人读」。

每接一个新客户的采集包，跑一次这个工具，就能看到分析层的真实覆盖范围，
避免"采了但没人读"的字段长期静默堆积——缺口是被列出来的，不会悄悄变大。

用法::

    python tools/coverage_audit.py <package.tar.gz|解压目录>
    python tools/coverage_audit.py <package> --json
    python tools/coverage_audit.py <package> --source plugins/mysql --source inspection_core
    python tools/coverage_audit.py <package> --exempt tables/block_devices

每个类别分四档：

* ``consumed``      —— 源码**真读取**了它（``ctx.tables.get("x")`` /
  ``ctx.tables["x"]`` / ``ctx.root / "tables/x.tsv"`` / ``ctx.history.get("x")`` …）。
* ``declared_only`` —— 只在 source / evidence_refs 这类**声明字符串**里出现，
  代码并没有读它的数据。**这是"声明了来源却没读"的隐蔽缺口**
  （如某些巡检项的 ``source`` 写着两张表，实际只读了一张）。
* ``unused``        —— 包里有、但任何分析代码都没提过。**这是要补的分析缺口。**
* ``missing``       —— 源码读的键、包里没有。**能抓出「规则读了不存在的表」这类缺陷**
  （历史上 ``rules.py`` 读 ``backup_evidence``，而采集端从不产出该表）。

局限：消费面用静态正则提取源码里的**字面量**引用。通过变量间接访问
（如 ``key = "x"; ctx.tables.get(key)``）无法识别，所以结论是"引用下界"，
``unused`` 可能含假阳性——确认后请加入 ``--exempt``。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tarfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

DEFAULT_SOURCES = ("plugins/mysql", "inspection_core")

# 采集端自身的中间产物，不是分析的输入，默认不计入未消费。
DEFAULT_EXEMPT = frozenset({"history/coverage"})

CATEGORIES = ("tables", "evidence", "timeseries", "history")
_EXT = {"tables": ".tsv", "evidence": ".txt", "timeseries": ".csv", "history": ".csv"}

# 真读取：从 ctx 取数，或按项目内相对路径打开文件（root 可能是 ctx.root，也可能是局部 root）。
READ_PATTERNS: dict[str, tuple[str, ...]] = {
    "tables": (
        r"""ctx\.tables\.get\(\s*["']([A-Za-z0-9_.]+)["']""",
        r"""ctx\.tables\[\s*["']([A-Za-z0-9_.]+)["']\s*\]""",
        r"""\broot\s*/\s*["']tables/([A-Za-z0-9_\-]+)\.tsv["']""",
    ),
    "history": (
        r"""ctx\.history\.get\(\s*["']([A-Za-z0-9_]+)["']""",
        r"""ctx\.history\[\s*["']([A-Za-z0-9_]+)["']\s*\]""",
        r"""\broot\s*/\s*["']history/([A-Za-z0-9_\-]+)\.csv["']""",
    ),
    "timeseries": (
        r"""ctx\.timeseries\.get\(\s*["']([A-Za-z0-9_]+)["']""",
        r"""ctx\.timeseries\[\s*["']([A-Za-z0-9_]+)["']\s*\]""",
        r"""\broot\s*/\s*["']timeseries/([A-Za-z0-9_\-]+)\.csv["']""",
    ),
    "evidence": (
        r"""\broot\s*/\s*["']evidence/([A-Za-z0-9_.\-\*]+)["']""",
    ),
}

# 仅声明：出现在 source / evidence_refs 字符串里，不代表读了数据。
# 不要求左右引号，以便覆盖 "tables/a.tsv; tables/b.tsv" 这类拼接串里的每一项。
DECLARE_PATTERNS: dict[str, tuple[str, ...]] = {
    "tables": (r"""tables/([A-Za-z0-9_\-]+)\.tsv""",),
    "history": (r"""history/([A-Za-z0-9_\-]+)\.csv""",),
    "timeseries": (r"""timeseries/([A-Za-z0-9_\-]+)\.csv""",),
    "evidence": (r"""evidence/([A-Za-z0-9_.\-\*]+)""",),
}


def _iter_package_members(package: Path) -> list[str]:
    """返回包内相对路径（去掉采集包顶层目录）。"""
    if package.is_dir():
        return [
            path.relative_to(package).as_posix()
            for path in sorted(package.rglob("*"))
            if path.is_file()
        ]
    members: list[str] = []
    with tarfile.open(package) as tar:
        for member in tar.getmembers():
            if not member.isfile():
                continue
            parts = member.name.split("/")
            members.append("/".join(parts[1:]) if len(parts) > 1 else parts[0])
    return sorted(members)


def enumerate_collected(package: Path) -> dict[str, set[str]]:
    """包内实际存在的采集项（键 = 表/序列名，evidence = 文件名）。"""
    collected: dict[str, set[str]] = {category: set() for category in CATEGORIES}
    for rel in _iter_package_members(package):
        head, _, tail = rel.partition("/")
        if head not in CATEGORIES or not tail.endswith(_EXT[head]):
            continue
        collected[head].add(Path(tail).name if head == "evidence" else Path(tail).stem)
    return collected


def collect_references(
    sources: list[Path],
    patterns: dict[str, tuple[str, ...]],
) -> dict[str, set[str]]:
    """静态提取源码里的字面量引用。"""
    references: dict[str, set[str]] = {category: set() for category in CATEGORIES}
    for source in sources:
        files = [source] if source.is_file() else sorted(source.rglob("*.py"))
        for path in files:
            text = path.read_text(encoding="utf-8", errors="replace")
            for category, group in patterns.items():
                for pattern in group:
                    references[category].update(re.findall(pattern, text))
    return references


def _matches(name: str, keys: set[str]) -> bool:
    if name in keys:
        return True
    for key in keys:
        if "*" in key:
            prefix = key.split("*", 1)[0]
            if prefix and name.startswith(prefix):
                return True
    return False


def audit(
    package: Path,
    sources: list[Path],
    exempt: frozenset[str] = DEFAULT_EXEMPT,
) -> dict[str, object]:
    """对比「采集面」与「消费面」，返回结构化结果。"""
    collected = enumerate_collected(package)
    read = collect_references(sources, READ_PATTERNS)
    declared = collect_references(sources, DECLARE_PATTERNS)
    categories: dict[str, object] = {}
    totals = {"collected": 0, "consumed": 0, "declared_only": 0, "unused": 0}

    def _exempted(category: str, name: str) -> bool:
        return f"{category}/{name}" in exempt

    for category in CATEGORIES:
        names = collected[category]
        consumed = sorted(n for n in names if _matches(n, read[category]))
        declared_only = sorted(
            n for n in names if not _matches(n, read[category]) and _matches(n, declared[category])
        )
        unused = sorted(
            n
            for n in names
            if not _matches(n, read[category])
            and not _matches(n, declared[category])
            and not _exempted(category, n)
        )
        # 源码真读了、包里却没有的键（备份规则踩过的坑）。
        missing = sorted(
            key
            for key in read[category]
            if "*" not in key and key not in names and not _exempted(category, key)
        )
        totals["collected"] += len(names)
        totals["consumed"] += len(consumed)
        totals["declared_only"] += len(declared_only)
        totals["unused"] += len(unused)
        categories[category] = {
            "collected": len(names),
            "consumed": consumed,
            "declared_only": declared_only,
            "unused": unused,
            "missing": missing,
        }

    return {
        "package": str(package),
        "sources": [str(source) for source in sources],
        "categories": categories,
        "totals": totals,
    }


def render(result: dict[str, object]) -> str:
    totals = result["totals"]
    lines = [
        f"采集包: {result['package']}",
        f"分析源: {', '.join(result['sources'])}",
        "",
        "合计: 采集 {collected} 项 / 真读取 {consumed} / 仅声明 {declared_only} / 无人提 {unused}".format(**totals),
    ]
    for category, data in result["categories"].items():
        lines.append("")
        lines.append(
            f"[{category}] 采集 {data['collected']} / 真读取 {len(data['consumed'])}"
            f" / 仅声明 {len(data['declared_only'])} / 无人提 {len(data['unused'])}"
        )
        if data["consumed"]:
            lines.append("  读取: " + ", ".join(data["consumed"]))
        if data["declared_only"]:
            lines.append("  仅声明未读取: " + ", ".join(data["declared_only"]))
        if data["unused"]:
            lines.append("  无人提及: " + ", ".join(data["unused"]))
        if data["missing"]:
            lines.append("  ⚠ 源码读取但包内不存在: " + ", ".join(data["missing"]))
    return "\n".join(lines)


def default_sources() -> list[Path]:
    return [PROJECT_ROOT / relative for relative in DEFAULT_SOURCES]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="采集包消费面自检")
    parser.add_argument("package", help="采集包路径（.tar.gz 或已解压目录）")
    parser.add_argument(
        "--source",
        action="append",
        default=None,
        help=f"分析源码路径，可重复；默认 {', '.join(DEFAULT_SOURCES)}",
    )
    parser.add_argument("--exempt", default="", help="额外豁免，逗号分隔，形如 tables/block_devices")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args(argv)

    package = Path(args.package).expanduser().resolve()
    if not package.exists():
        parser.error(f"采集包不存在: {package}")
    sources = (
        [Path(item).expanduser().resolve() for item in args.source]
        if args.source
        else default_sources()
    )
    exempt = set(DEFAULT_EXEMPT)
    exempt.update(item.strip() for item in args.exempt.split(",") if item.strip())

    result = audit(package, sources, frozenset(exempt))
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(render(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
