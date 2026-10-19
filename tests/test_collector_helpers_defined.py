"""守门：采集脚本调用的"登记类"辅助函数必须在本文件里定义。

来历：B1 给 Oracle 补 `else` 登记时，一行写成 `record_skipped …` ——
而 `record_skipped` 只在 MySQL/PG 里有定义，Oracle 侧是 10 参数的 `record_status`。
后果：`record_skipped: command not found`（rc=127），`system.timedatectl` 这一项
**在缺 timedatectl 的机器上根本不进台账** —— 正是 B1 要修的那个"分母变小"的坑，
却伪装成"已经修好了"（文本上看着有 else 分支）。

为什么原 B1 尺子兜不住：它只判"每个 item 在 else 段里出现过"，不判"用的函数存不存在"。
这是同一种坏味道的又一次现形 —— **没有能力说出自己没生效，看起来就跟生效了一样**。

判据（规则来源 = 命名约定，不取自被测文件自身）：
  凡出现形如 `record_xxx` / `capture_xxx` / `mark_xxx` 的 token（**全行注释除外**），
  该名字必须在**同一个文件**里有 `name() {` 形态的定义。
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COLLECTORS = [
    ROOT / "inspection" / "pg_inspection_standard.sh",
    ROOT / "inspection" / "mysql_inspection_standard.sh",
    ROOT / "inspection" / "oracle_inspection.sh",
]

# 命名约定（独立来源，不从文件里反推）：登记/采集类辅助函数
_HELPER_NAME = re.compile(r"\b((?:record|capture|mark)_[a-z0-9_]+)\b")
_DEFINITION = re.compile(r"^[ \t]*(?:function[ \t]+)?([a-zA-Z_][a-zA-Z0-9_]*)[ \t]*\(\)[ \t]*\{", re.M)

# "命令位"取词：行首 / `;` `&` `|` 之后 / `$(` 之后 / then|do|else 之后。
# 必须限定命令位，否则 `--alert-log` 这类**开关名**会被当成调用 `log`（实测假阳性）。
_AT_COMMAND_POS = re.compile(
    r"(?:^|[;&|]|\$\(|\bthen\b|\bdo\b|\belse\b)[ \t]*([a-z_][a-z0-9_]*)", re.M
)


def strip_full_line_comments(text: str) -> str:
    return "\n".join(l for l in text.split("\n") if not l.lstrip().startswith("#"))


def undefined_helpers(text: str) -> dict[str, int]:
    """返回 {被调用但本文件未定义的名字: 首次出现的**真实文件行号**}。"""
    lines = text.split("\n")
    # 定义只在非注释行里找（注释掉的定义不算定义）
    defined = set(_DEFINITION.findall(strip_full_line_comments(text)))
    out: dict[str, int] = {}
    for lineno, line in enumerate(lines, 1):
        if line.lstrip().startswith("#"):
            continue
        for name in _HELPER_NAME.findall(line):
            if name not in defined and name not in out:
                out[name] = lineno
    return out


def cross_script_undefined_helpers(sources: dict[str, str]) -> dict[str, list[str]]:
    """"三库同族"判据：某个辅助函数在**至少一个**采集脚本里定义了，
    另一个脚本在**命令位**用到它却没有自己的定义 ⇒ 报出。

    独立来源是**另外两个脚本**（不是被测文件自身），所以不会出现
    "被测对象漏了、候选集跟着漏"的自我引用坏味道。
    """
    defs = {name: set(_DEFINITION.findall(strip_full_line_comments(text)))
            for name, text in sources.items()}
    family: set[str] = set().union(*defs.values()) if defs else set()

    out: dict[str, list[str]] = {}
    for name, text in sources.items():
        used = set(_AT_COMMAND_POS.findall(strip_full_line_comments(text)))
        missing = sorted(used & (family - defs[name]))
        if missing:
            out[name] = missing
    return out


class CollectorHelperDefinitionTests(unittest.TestCase):
    def test_every_used_helper_is_defined_in_same_file(self) -> None:
        problems = []
        for path in COLLECTORS:
            text = path.read_text(encoding="utf-8")
            for name, lineno in sorted(undefined_helpers(text).items()):
                problems.append(f"{path.name}:{lineno} 调用了未定义的 {name}")
        self.assertEqual(
            [], problems,
            "采集脚本调用了本文件没有定义的辅助函数（运行时 command not found，"
            "该采集项**不进台账**却看起来像已登记）：\n" + "\n".join(problems),
        )

    # ---------- 尺子自证 ----------
    def test_ruler_catches_undefined_helper(self) -> None:
        fake = (
            "record_status() {\n  :\n}\n"
            "if has_cmd timedatectl; then capture_command x; else record_skipped a b c d; fi\n"
        )
        got = undefined_helpers(fake)
        self.assertIn("record_skipped", got, "未定义的 helper 必须被抓到")

    def test_ruler_does_not_false_positive_on_defined_helper(self) -> None:
        fake = (
            "record_status() {\n  :\n}\n"
            "record_skipped() {\n  record_status a b c\n}\n"
            "record_skipped p q r s\n"
        )
        self.assertEqual({}, undefined_helpers(fake), "定义了就不许误伤")

    def test_ruler_ignores_full_line_comments(self) -> None:
        """注释里提到某个不存在的 helper，不算缺陷（否则注释没法解释历史）。"""
        fake = (
            "record_status() {\n  :\n}\n"
            "# 早期版本这里写过 record_skipped，后来删了\n"
            "record_status a b c\n"
        )
        self.assertEqual({}, undefined_helpers(fake))


class CrossScriptHelperTests(unittest.TestCase):
    """三库是同一族采集器的三个 fork：一个库里定义了的辅助函数，
    另一个库用到就必须也有定义。B1 那次 Oracle 误写 `record_skipped` 正是这么暴露的。
    """

    def test_no_helper_used_but_undefined_across_collectors(self) -> None:
        sources = {p.name: p.read_text(encoding="utf-8") for p in COLLECTORS}
        report = [f"{name}: {missing}" for name, missing in cross_script_undefined_helpers(sources).items()]
        self.assertEqual([], report, "跨库调用了他库定义、本库没有的辅助函数：\n" + "\n".join(report))

    def test_flag_names_are_not_mistaken_for_calls(self) -> None:
        """`--alert-log` 里含 `log`（MySQL/PG 有 log() 函数）—— 不许当成调用。"""
        sources = {
            "a.sh": "log() {\n  :\n}\n",
            "b.sh": "case \"$1\" in\n  --alert-log) A=1 ;;\n  --no-alert-log) A=0 ;;\nesac\n",
        }
        self.assertEqual({}, cross_script_undefined_helpers(sources))

    def test_cross_script_ruler_catches_missing_helper(self) -> None:
        """尺子自证：a.sh 定义了 foo()，b.sh 在命令位调 foo() 却没定义 ⇒ 必须抓到。"""
        sources = {
            "a.sh": "foo() {\n  :\n}\n",
            "b.sh": "if has_cmd x; then\n  foo\nfi\n",
        }
        got = cross_script_undefined_helpers(sources)
        self.assertEqual(["foo"], got.get("b.sh"))
        self.assertNotIn("a.sh", got, "定义方不许被误伤")


if __name__ == "__main__":
    for p in COLLECTORS:
        t = p.read_text(encoding="utf-8")
        u = undefined_helpers(t)
        print(f"{p.name}: 未定义 helper {len(u)} {u if u else ''}")
