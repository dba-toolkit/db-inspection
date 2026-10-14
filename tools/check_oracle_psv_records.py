# -*- coding: utf-8 -*-
"""守门：Oracle 采集产物 ``.psv`` 的「**一行一记录**」契约（**数据侧**）。

与 ``tools/check_oracle_psv_widths.py`` 是**一对**，缺一不可：

| | ``check_oracle_psv_widths.py`` | 本工具 |
|---|---|---|
| 查什么 | **脚本写法**（静态） | **真实产物**（运行期） |
| 判据 | ``SUBSTR`` 长度 vs ``COLUMN`` 宽度、SQL 字面量含 ``|``、``WORD_WRAPPED``、同名宽度冲突 | 字段数、首列是否为空、最长值是否触顶 |

**为什么必须另有一个数据侧工具**：静态分析**结构性地**看不到「运行期数据本身含分隔符」。
实测（10-10 包）：``LISTAGG(name, '|')`` 的返回值里带竖线，而 ``COLSEP`` 就是 ``|``
⇒ 一个值被切成两列，``controlfile_paths`` 的**第二个路径被静默丢弃**，
静态守门当时是**全绿**的。

## 三条判据

- **A 字段数**：某行字段数 ``!=`` 表头字段数 ⇒ 违规。
  抓的是「值含分隔符」（字段变多）和「折行残段带多余列」。
- **B 首列非空**：``.psv`` 的第一列是**行标识**（参数名 / 名称 / 状态），语义上不该为空。
  折行残段的首列恰好为空 ⇒ 抓得住「字段数恰好相等」的残段。
  ⚠ **只对 2.0 的 ``.psv`` 成立** —— 采集会话设了 ``SET NULL '-'``，真 NULL 打成 ``-``，
  空单元格根本不存在。**1.4 的 ``.tsv`` 上这条不成立**：那里首列空白是「NULL 落在定宽列里」，
  是**真实数据行**，照搬会把数据行并进表头（08-10 包实测 ``控制文件.tsv`` 3 行塌成 1 行）。
  ⇒ 本工具默认只认 ``.psv``。``--include-tsv`` 会**同时**做两件事：**关掉判据 B**，
  并把切分符从 ``|`` 换成 **TAB** —— 1.4 的 ``.tsv`` 是 TAB 分隔，拿 ``|`` 去切会把
  「值里含 ``|``」误报成字段数不符。
- **C 触顶**：某列最长值 ``>=`` 采集脚本里该列的 ``SUBSTR(...,1,N)`` 上限 ⇒ **警告**
  （值可能就是被 SQL 砍到这个长度的）。C **不影响退出码** —— 它只是"疑似"，不是"确证"。
- **占位行不是记录**：``# oracle-inspection psv-empty …`` / ``… psv-unavailable …``
  是「查了没有 / 没取到」的占位形态，**无表头、无数据行**。守门把它当注释跳过，
  并把这类文件单独计数报出（``psv-empty`` / ``psv-unavailable`` 是**相反结论**，
  不能与「查过没问题」混在同一个计数里）。
  ★ 它与下游三处（``tabular.py`` / ``package_adapter.py`` / ``presentation.py``）**同一个判据**：
  守门必须按消费者看到的形态判，否则会出现「守门通过、而那一项从未被检查」的假绿。

## 两条判据必须同时用

A 单独用会漏「字段数恰好相等」的残段：实测 ``'|02.ctl'.split('|')`` = ``['', '02.ctl']``
**正好 2 个字段 = 表头 2 列**，A 判定通过。
B 单独用会漏「首列非空但值里含分隔符」的情况。
**断言形式与被测对象不匹配时，绿色不代表正确。**

## 用法

    python tools/check_oracle_psv_records.py <采集包目录 | tables 目录>
    python tools/check_oracle_psv_records.py --self-test      # 只跑尺子自证
    python tools/check_oracle_psv_records.py <目录> --script <采集脚本路径>

退出码：0 = 无 A/B 违规；1 = 有违规，或尺子自证失败。
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SCRIPT = ROOT / "inspection" / "oracle_inspection.sh"

_SUBSTR = re.compile(
    r"SUBSTR\s*\(\s*(?:[A-Za-z_][\w$.]*\.)?(\w+)\s*,\s*\d+\s*,\s*(\d+)\s*\)", re.I)


def _is_comment(line: str) -> bool:
    """`.psv` 的**占位行**（`# oracle-inspection psv-empty …` / `… psv-unavailable …`）。

    它与下游三处**同一个判据**（`inspection_core/tabular.py:15`、
    `plugins/oracle/package_adapter.py:403`、`plugins/oracle/presentation.py:200`）：
    守门必须**按消费者看到的形态**判 —— 消费者跳过它，守门就不能把它当表头/数据行，
    否则会出现「守门说通过了，而那一项其实**从来没被检查过**」这种假绿。
    占位行**不是记录**：`.psv` 的三种形态里，只有「数据」形态才有表头。
    """
    return line.strip().startswith("#")


def classify(header: list[str], lines: list[str], *, delimiter: str = "|",
             first_column_must_be_filled: bool = True) -> list[str]:
    """判据 A + B。返回违规描述列表；**被自证用例直接复用**，别在这里加外部依赖。

    ``delimiter`` **必须与产物格式一致**：2.0 的 ``.psv`` 是 ``|``（``SET COLSEP '|'``），
    1.4 的 ``.tsv`` 是 **TAB**。拿 ``|`` 去切 ``.tsv`` 会把「值里含 ``|``」误报成字段数不符 ——
    这正是 `--include-tsv` 必须连分隔符一起换掉的原因。
    """
    problems: list[str] = []
    width = len(header)
    for index, line in enumerate(lines, start=2):
        if not line.strip() or _is_comment(line):
            continue                      # 空行 / 占位行都不是记录（否则会误报）
        fields = line.split(delimiter)
        if len(fields) != width:
            problems.append(f"行{index} 字段数 {len(fields)} != 表头 {width}  [{line[:70]!r}]")
        elif first_column_must_be_filled and fields[0].strip() == "":
            problems.append(f"行{index} 首列为空（疑折行残段）  [{line[:70]!r}]")
    return problems


def _records(lines: list[str]) -> list[str]:
    """去掉空行与占位行后的**真记录**（含表头作为第 0 条）。"""
    return [line for line in lines if line.strip() and not _is_comment(line)]



def _legacy_field_count_only(header: list[str], lines: list[str]) -> list[str]:
    """批 16 之前的旧判据：只比字段数。**只用于自证**，不要拿它当守门。"""
    width = len(header)
    return [f"行{i}" for i, line in enumerate(lines, start=2)
            if line.strip() and len(line.split("|")) != width]


def self_test(verbose: bool = True) -> bool:
    """尺子自证：把已知样本喂给**同一个** ``classify()``，逐条确认判据有区分力。

    不证尺子就不知道它到底能兜住什么 —— 尤其是刚补上的那条判据。
    """
    header = ["NAME", "VALUE"]
    checks: list[tuple[str, bool]] = []

    # ① 折行残段：字段数恰好等于表头 ⇒ 旧判据必须漏、新判据必须抓
    wrapped = ["control_files|/u01/a/control01.ctl, /u01/a/control", "|02.ctl"]
    checks.append(("旧判据对折行残段漏报", _legacy_field_count_only(header, wrapped) == []))
    checks.append(("新判据抓到折行残段", len(classify(header, wrapped)) == 1))

    # ② 值含分隔符：字段数变多 ⇒ 判据 A 必须抓
    delim = ["2|/u01/a/control01.ctl|/u01/a/control02.ctl"]
    checks.append(("判据 A 抓到值内含分隔符", len(classify(header, delim)) == 1))

    # ③ 防误伤：干净文件必须过（含 SET NULL '-' 打出来的合法 '-')
    clean = ["db_name|orcl", "control_files|/u01/a/control01.ctl", "audit_trail|-"]
    checks.append(("干净文件不误报", classify(header, clean) == []))
    checks.append(("末尾空行不误报", classify(header, clean + ["", ""]) == []))

    # ④ 版本前提：关掉判据 B 后，首列空白不再报（1.4 的 .tsv 语义）
    blank_first = ["|/u01/a/control01.ctl"]
    checks.append(("关掉判据 B 后不再报首列空白",
                   classify(header, blank_first, first_column_must_be_filled=False) == []))

    # ⑤ 切分符也要跟着版本换：1.4 的 .tsv 是 TAB 分隔
    tsv_header = ["STATUS", "NAME"]
    tsv_line = "a|b|c\t/u01/x.ctl"          # 首字段含两个竖线
    checks.append(("tsv 用 TAB 切分不误报（值里含 |）",
                   classify(tsv_header, [tsv_line], delimiter="\t",
                            first_column_must_be_filled=False) == []))
    checks.append(("同一行误用 | 切就会被报（对照）",
                   classify(tsv_header, [tsv_line], delimiter="|",
                            first_column_must_be_filled=False) != []))

    # ⑥ 占位行（# oracle-inspection psv-empty …）**不是记录**：
    #    它既不能当表头，也不能当数据行。3 列表头下这行只有 2 个字段 ⇒ 若当成数据会被误报。
    marker = "# oracle-inspection psv-empty item_id=oracle.x sep=| rows=0 status=empty"
    three = ["ITEM", "STATUS", "COUNT"]
    checks.append(("占位行带 # 时被跳过", classify(three, [marker]) == []))
    checks.append(("_records 只留真记录", _records([marker, ""]) == []))
    # 负控制：把 `_is_comment` 换成"永不成立"，同一行必须被报出来 ——
    # 证明"跳过"确实由这条判据生效，而不是恰好字段数对上了。
    original_is_comment = globals()["_is_comment"]
    try:
        globals()["_is_comment"] = lambda line: False
        checks.append(("负控制：关掉 # 跳过后占位行必被报出", classify(three, [marker]) != []))
    finally:
        globals()["_is_comment"] = original_is_comment

    if verbose:
        print("=== 尺子自证 ===")
        for name, ok in checks:
            print(f"  {'✓' if ok else '✗'} {name}")
    return all(ok for _, ok in checks)


def substr_limits(script: Path) -> dict[str, set[int]]:
    """从采集脚本抽「列名 -> SUBSTR 上限」（大小写归一）。"""
    if not script.exists():
        return {}
    limits: dict[str, set[int]] = {}
    for match in _SUBSTR.finditer(script.read_text(encoding="utf-8", errors="replace")):
        limits.setdefault(match.group(1).upper(), set()).add(int(match.group(2)))
    return limits


def collect_files(target: Path, include_tsv: bool) -> list[Path]:
    if target.is_file():
        return [target]
    patterns = ["*.psv"] + (["*.tsv"] if include_tsv else [])
    return sorted(path for pattern in patterns for path in target.rglob(pattern))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Oracle .psv 一行一记录契约守门（数据侧）")
    parser.add_argument("target", nargs="?", help="采集包目录 / tables 目录 / 单个 .psv")
    parser.add_argument("--script", default=str(DEFAULT_SCRIPT), help="采集脚本（用于判据 C）")
    parser.add_argument("--self-test", action="store_true", help="只跑尺子自证")
    parser.add_argument("--include-tsv", action="store_true",
                        help="一并扫 .tsv（1.4 格式）—— **自动关闭判据 B**，见模块 docstring")
    args = parser.parse_args(argv)

    ok = self_test()
    if args.self_test:
        return 0 if ok else 1
    if not args.target:
        parser.error("需要给出采集包目录（或加 --self-test）")
    if not ok:
        print("\n✗ 尺子自证失败，拒绝出结论")
        return 1

    target = Path(args.target)
    files = collect_files(target, args.include_tsv)
    if not files:
        print(f"\n未找到可检查的文件：{target}")
        return 1

    limits = substr_limits(Path(args.script))
    delimiter = "\t" if args.include_tsv else "|"
    label = "TAB（.tsv/1.4）" if args.include_tsv else "|（.psv/2.0）"
    print(f"\n检查 {len(files)} 个文件（切分符 {label}；"
          f"判据 B {'开' if not args.include_tsv else '关'}）")

    violations: dict[str, list[str]] = {}
    warnings: list[str] = []
    placeholders: list[str] = []
    checked = 0
    for path in files:
        lines = path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
        if not lines:
            continue
        records = _records(lines)
        if not records:
            # 纯占位文件（`psv-empty` / `psv-unavailable`）：**按契约没有表头、没有记录**，
            # 无 A/B 可判。单独计数并在结论里报出来 —— 否则「检查了 N 个文件」会把
            # 「没记录可查」混进「查过没问题」，这正是「未采集 ≠ 0」要防的那种假绿。
            placeholders.append(path.name)
            continue
        checked += 1
        header = records[0].split(delimiter)
        found = classify(header, records[1:], delimiter=delimiter,
                         first_column_must_be_filled=not args.include_tsv)
        if found:
            violations[str(path)] = found
        for column_index, column in enumerate(header):
            longest = max((len(line.split(delimiter)[column_index])
                           for line in records[1:]
                           if column_index < len(line.split(delimiter))),
                          default=0)
            for limit in limits.get(column.strip().upper(), ()):
                if longest >= limit:
                    warnings.append(
                        f"{path.name} 列 {column.strip().upper()} 最长 {longest} 触到 SUBSTR 上限 {limit}")

    if placeholders:
        print(f"  ⓘ 其中 {len(placeholders)} 个为占位文件（psv-empty / psv-unavailable）："
              f"按契约无表头、无记录，不参与 A/B 判定；实际判定 {checked} 个")

    print("\n=== 判据 A/B（违规，影响退出码）===")
    if violations:
        for name, items in sorted(violations.items()):
            print(f"✗ {name}")
            for item in items[:6]:
                print(f"     {item}")
            if len(items) > 6:
                print(f"     … 还有 {len(items) - 6} 处")
    else:
        print("✓ 无违规")

    print("\n=== 判据 C（疑似截断，仅警告）===")
    print("✓ 无触顶" if not warnings else "\n".join(f"⚠ {w}" for w in warnings))

    total = sum(len(v) for v in violations.values())
    print(f"\n结论：违规 {total} 处（{len(violations)} 个文件）；警告 {len(warnings)} 处；"
          f"占位文件 {len(placeholders)} 个（无记录可查，未参与判定）")
    return 0 if total == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
