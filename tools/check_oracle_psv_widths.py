# -*- coding: utf-8 -*-
"""守门：采集脚本里会**破坏 `.psv` 一行一记录契约**的四类写法。

`.psv` 的形态是 sqlplus 的 `SET COLSEP '|'` 输出，下游 `parse_delimited` 按行 + 竖线切。
只要出现下面任一种写法，某一行就不再等于一条记录 —— 而且**静默**（不报错、不抛异常）：

  ① **宽度失配**：`SUBSTR(<表达式>, 1, N) <别名>`，但脚本里对 `<别名>` 的宽度声明
     `COLUMN <别名> FORMAT a<M>` 不存在或 `M < N` ⇒ sqlplus 按 M 折行。
     实测：`control_files` 的值是 `SUBSTR(value,1,400)` 而 `COLUMN value FORMAT a80`
     ⇒ 107 字符的值被断成两行，残段 `|02.ctl` 被解析成"第二条参数"。
  ② **SQL 字面量含竖线**：`'…|…'`。COLSEP 是 `|`，值里的 `|` 会被下游当列边界；
     2.0 的 `psv_normalize` 还会把两侧空白一起压掉（`a | b` → `a|b`），
     于是第二个值被静默丢进 csv 的 `None` 列（1.4 不压空白，所以一直没暴露）。
  ③ **`WORD_WRAPPED`**：sqlplus 按**词**折行，折点落在词间，残段**无法**用
     「无分隔符续接」还原（会粘字）。去掉后只剩「断词折行」一种模式，
     解析层 `package_adapter._rejoin_wrapped_rows` 才能安全兜底。
  ④ **同名别名宽度冲突**：`COLUMN` 是**会话级**设置，同一名字声明两次时**后一条生效**
     ⇒ 前一条形同虚设。判 ① 若按「取最大值」比较，`COLUMN value FORMAT a500` 后面
     跟一条 `COLUMN VALUE FORMAT a80`（大小写是两个名字，但 sqlplus 列名大小写不敏感，
     采集脚本两类都写）就会**静默通过** —— 「某处被改窄」看不出来。
     本条是**负控制实测发现的漏洞**：把 `a500` 改回 `a80`，判 ① 仍然全绿。
     ★ 全仓库实测：45 条声明 / 26 个去重别名，**没有**一个别名有第二种宽度
     （大小写两份宽度始终相同）⇒ 这条不变量零误报。

用法：
    python tools/check_oracle_psv_widths.py            # 检仓库里的采集脚本
    python tools/check_oracle_psv_widths.py <脚本路径>
退出码：0 = 通过；1 = 有违规（逐条打印）。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SCRIPT = ROOT / "inspection" / "oracle_inspection.sh"

# 一次 SQL 采集项：ora_sql_to_md "<item_id>" "<标题>" "<SQL>"
_CALL = re.compile(r'ora_sql_to_md\s+"([^"]*)"\s+"([^"]*)"\s+"(.*?)"\s*\n', re.S)
# sqlplus 的列宽声明：COLUMN <名字> FORMAT a<宽度>
_COLUMN_WIDTH = re.compile(r"^\s*COLUMN\s+([A-Za-z_][A-Za-z0-9_]*)\s+FORMAT\s+a(\d+)\b", re.M)
# SQL 侧截断 + 别名：SUBSTR(<表达式>, 1, N) <别名>
_SUBSTR_ALIAS = re.compile(
    r"SUBSTR\s*\(\s*[^()]*?\s*,\s*1\s*,\s*(\d+)\s*\)\s+([A-Za-z_][A-Za-z0-9_]*)"
)
# 按词折行（只看 COLUMN 声明行；注释里提到这个词不算）
_WORD_WRAPPED = re.compile(r"^\s*COLUMN\b.*\bWORD_WRAPPED\b", re.M)


def sql_literals(sql: str) -> list[str]:
    """按引号状态机取出 SQL 里的**字符串字面量**内容。

    ★ 不能用 `'[^']*\\|[^']*'` 这种朴素正则：`'GB' ELSE x || 'MB'` 会被从
    **前一个字面量的收尾引号**到**后一个字面量的起始引号**整段命中 ——
    匹配到的是 `' ELSE x || '`，而它根本不是字面量（`||` 是拼接运算符）。
    判据必须认结构：先过一遍引号状态机拿到真正的字面量（含 `''` 转义）。
    """
    out: list[str] = []
    index = 0
    length = len(sql)
    while index < length:
        if sql[index] != "'":
            index += 1
            continue
        cursor = index + 1
        buffer: list[str] = []
        while cursor < length:
            if sql[cursor] == "'":
                if cursor + 1 < length and sql[cursor + 1] == "'":  # '' 转义成一个 '
                    buffer.append("'")
                    cursor += 2
                    continue
                break
            buffer.append(sql[cursor])
            cursor += 1
        out.append("".join(buffer))
        index = cursor + 1
    return out


def iter_sql_bodies(script: str) -> list[tuple[str, int, str]]:
    """抽出所有 `ora_sql_to_md` 调用：`(item_id, 行号, SQL 正文)`。"""
    out: list[tuple[str, int, str]] = []
    for match in _CALL.finditer(script):
        item_id, _title, body = match.group(1), match.group(2), match.group(3)
        line_no = script.count("\n", 0, match.start()) + 1
        out.append((item_id, line_no, body))
    return out


def column_width_groups(script: str) -> dict[str, set[int]]:
    """每个别名声明过的**全部**宽度：`{列名小写: {宽度…}}`。

    单独留这个函数是为了让「同名宽度必须唯一」这条不变量可被判据直接查
    （`column_widths` 只取最大值，会把冲突抹平 —— 那正是负控制抓到的漏洞）。
    """
    groups: dict[str, set[int]] = {}
    for name, width in _COLUMN_WIDTH.findall(script):
        groups.setdefault(name.lower(), set()).add(int(width))
    return groups


def column_widths(script: str) -> dict[str, int]:
    """采集脚本里声明的字符列宽度：`{列名小写: 宽度}`（同名取最大值）。

    ★ 这里取 `max` 只在「同名宽度唯一」成立时才有意义，而 uniqueness 由判 ④
    单独守；两条判据**必须成对存在**，只留 `max` 就等于给"某处被改窄"背书。
    """
    return {name: max(widths) for name, widths in column_width_groups(script).items()}


def check_script(script: str) -> list[str]:
    """返回违规清单（空 = 通过）。**任何一条源码都喂给这个函数**，尺子自证靠它。"""
    problems: list[str] = []
    groups = column_width_groups(script)
    widths = {name: max(group) for name, group in groups.items()}

    # 判 ④：同名别名宽度必须唯一 —— 否则 `max` 会把"某处被改窄"抹平（负控制实测漏洞）。
    for alias in sorted(groups):
        if len(groups[alias]) > 1:
            widths_seen = "、".join("a%d" % width for width in sorted(groups[alias]))
            problems.append(
                f"[宽度冲突] 列 `{alias}` 被声明为多种宽度（{widths_seen}）—— "
                f"sqlplus 按最后一条生效，前面的形同虚设，"
                f"而「取最大值」的判据会让改窄的那一处静默通过"
            )

    for item_id, line_no, body in iter_sql_bodies(script):
        for width_text, alias in _SUBSTR_ALIAS.findall(body):
            need = int(width_text)
            declared = widths.get(alias.lower())
            if declared is None:
                problems.append(
                    f"[宽度失配] {item_id}（脚本第 {line_no} 行）："
                    f"SUBSTR(…,1,{need}) {alias} —— 脚本里没有 "
                    f"`COLUMN {alias} FORMAT aN`，宽度交由 sqlplus 从表达式推导、不可控"
                )
            elif declared < need:
                problems.append(
                    f"[宽度失配] {item_id}（脚本第 {line_no} 行）："
                    f"SUBSTR(…,1,{need}) {alias} 但 `COLUMN {alias} FORMAT a{declared}` "
                    f"⇒ 超出 {declared} 的部分会被折到下一行"
                )
        for literal in sql_literals(body):
            if "|" in literal:
                problems.append(
                    f"[竖线字面量] {item_id}（脚本第 {line_no} 行）：SQL 字面量 "
                    f"{literal!r} 含 `|`，会被下游当列边界（COLSEP 就是 |）"
                )

    for match in _WORD_WRAPPED.finditer(script):
        line_no = script.count("\n", 0, match.start()) + 1
        line = script.splitlines()[line_no - 1].strip()
        problems.append(
            f"[WORD_WRAPPED] 脚本第 {line_no} 行：{line} —— 按词折行的残段无法无分隔符续接"
        )

    return problems


def main(argv: list[str]) -> int:
    script_path = Path(argv[1]) if len(argv) > 1 else DEFAULT_SCRIPT
    script = script_path.read_text(encoding="utf-8")
    bodies = iter_sql_bodies(script)
    problems = check_script(script)

    print(f"脚本：{script_path}")
    print(f"SQL 采集项：{len(bodies)} 个；列宽声明：{len(column_widths(script))} 条")
    if not problems:
        print("✓ 四类判据全部通过（宽度失配 / 竖线字面量 / WORD_WRAPPED / 宽度冲突）")
        return 0
    print(f"✗ 发现 {len(problems)} 处违规：")
    for problem in problems:
        print("  - " + problem)
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
