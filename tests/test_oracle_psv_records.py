# -*- coding: utf-8 -*-
"""守门：Oracle `.psv` 产物「一行一记录」的**数据侧**审计工具
（`tools/check_oracle_psv_records.py`）的自测。

## 为什么要有这个工具（静态那份兜不住的部分）

`tools/check_oracle_psv_widths.py` 查的是**脚本写法**，它在 10-10 包上**全绿**，
可那个包里 `controlfile_multiplex` 的第二个控制文件路径**真的丢了** ——
`LISTAGG(name,'|')` 的返回值里带竖线，而 `COLSEP` 就是 `|`，一个值被切成两列。
这类问题**只在运行期才存在**，静态分析结构性地看不见。

## 为什么要给尺子写测试

它同样是静默的：判据写错会**永远绿**，比没有还危险。逐条钉住：

1. **旧判据确实有盲区** —— `'|02.ctl'.split('|')` 正好 2 字段 = 表头 2 列，
   只比字段数的判据**漏报**。这是"为什么要加判据 B"的反证，不是风格偏好。
2. **判据 B（首列非空）必须真的抓得到** —— 配"故意违规的样本喂进同一个 `classify()`"。
3. **防误伤配对** —— 干净文件、`SET NULL '-'` 打出来的合法 `-`、末尾空行，都不许报。
4. **版本前提不许漂** —— 判据 B 只在 2.0 的 `.psv` 成立；1.4 的 `.tsv` 里首列空白是
   合法 NULL，照搬会把真实数据行报成残段（08-10 包实测 `控制文件.tsv` 3 行塌成 1 行）。
   所以 `--include-tsv` 必须**自动关掉**判据 B，并**把切分符从 `|` 换成 TAB**
   （拿 `|` 去切 `.tsv` 会把「值里含 `|`」误报成字段数不符 —— 那是假阳性）。
5. **判据 C 只是警告**，不许把门禁搞红（触顶 ≠ 确证被截断）。
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load_tool():
    """按路径加载 `tools/` 里的脚本（`tools/` 不是包，没有 `__init__.py`）。"""
    path = ROOT / "tools" / "check_oracle_psv_records.py"
    spec = importlib.util.spec_from_file_location("check_oracle_psv_records", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


TOOL = _load_tool()

# 折行残段样本：`COLUMN value FORMAT a80` 把 107 字符的 control_files 断成两行。
WRAPPED = ["control_files|/u01/a/control01.ctl, /u01/a/control", "|02.ctl"]
# 值含分隔符样本：`LISTAGG(name,'|')` 的返回值里带竖线。
DELIMITED = ["2|/u01/a/control01.ctl|/u01/a/control02.ctl"]


def _write_psv(directory: str, name: str, lines: list[str]) -> Path:
    path = Path(directory) / name
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _run(argv: list[str]) -> tuple[int, str]:
    """直接调 `main()`，把 stdout 收起来 —— 比 fork 子进程快，且不依赖 shell。"""
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = TOOL.main(argv)
    return code, buffer.getvalue()


class RulerSelfProofTests(unittest.TestCase):
    """① 尺子自证：内置样本逐条确认判据有区分力。"""

    def test_builtin_self_test_passes(self) -> None:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            ok = TOOL.self_test()
        self.assertTrue(ok, buffer.getvalue())
        self.assertIn("新判据抓到折行残段", buffer.getvalue())

    def test_old_field_count_only_ruler_really_misses_the_fragment(self) -> None:
        """反证：只比字段数的旧判据对 `|02.ctl` **漏报**（2 字段 == 表头 2 列）。

        这条不是凑数 —— 它是"为什么要加判据 B"的全部理由。哪天有人嫌 B 多余把它删了，
        这里会先红。
        """
        header = ["NAME", "VALUE"]
        self.assertEqual(TOOL._legacy_field_count_only(header, WRAPPED), [])
        self.assertEqual(len(TOOL.classify(header, WRAPPED)), 1)


class ViolationDetectionTests(unittest.TestCase):
    """② 两类真违规都必须被抓到，且报的是对的那个判据。"""

    def test_wrapped_fragment_is_caught_by_first_column_rule(self) -> None:
        problems = TOOL.classify(["NAME", "VALUE"], WRAPPED)
        self.assertEqual(len(problems), 1)
        self.assertIn("首列为空", problems[0])

    def test_value_with_delimiter_is_caught_by_field_count_rule(self) -> None:
        problems = TOOL.classify(["CONTROLFILE_COUNT", "CONTROLFILE_PATHS"], DELIMITED)
        self.assertEqual(len(problems), 1)
        self.assertIn("字段数", problems[0])

    def test_two_axes_are_distinguishable(self) -> None:
        """两类问题必须报成**不同**的话 —— 一个是"值里带分隔符"，一个是"折行残段"。"""
        wrapped = TOOL.classify(["NAME", "VALUE"], WRAPPED)[0]
        delimited = TOOL.classify(["A", "B"], DELIMITED)[0]
        self.assertNotEqual(wrapped, delimited)
        self.assertNotIn("字段数", wrapped)
        self.assertNotIn("首列为空", delimited)

    def test_cli_exits_nonzero_on_a_dirty_package(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _write_psv(temporary, "dirty.psv", ["NAME|VALUE"] + WRAPPED)
            code, output = _run([temporary, "--script", str(Path(temporary, "none.sh"))])
        self.assertEqual(code, 1)
        self.assertIn("首列为空", output)


class FalsePositiveGuardTests(unittest.TestCase):
    """③ 防误伤：合法产物一个都不许报（与上面的"必须抓到"配对）。"""

    def test_clean_psv_passes(self) -> None:
        clean = ["db_name|orcl", "control_files|/u01/a/control01.ctl", "audit_trail|-"]
        self.assertEqual(TOOL.classify(["NAME", "VALUE"], clean), [])

    def test_set_null_dash_is_not_a_blank_first_column(self) -> None:
        """采集会话设了 `SET NULL '-'`，第一列的真 NULL 是 `-` 而**不是**空字符串。"""
        self.assertEqual(TOOL.classify(["STATUS", "NAME"], ["-|/u01/a/control01.ctl"]), [])

    def test_trailing_blank_lines_are_not_records(self) -> None:
        clean = ["db_name|orcl", "", "   "]
        self.assertEqual(TOOL.classify(["NAME", "VALUE"], clean), [])

    def test_cli_exits_zero_on_a_clean_package(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _write_psv(temporary, "clean.psv", ["NAME|VALUE", "db_name|orcl"])
            code, output = _run([temporary, "--script", str(Path(temporary, "none.sh"))])
        self.assertEqual(code, 0)
        self.assertIn("无违规", output)


class VersionPremiseTests(unittest.TestCase):
    """④ 版本前提：判据 B 只在 2.0 的 `.psv` 成立，`--include-tsv` 必须关掉它。"""

    def test_first_column_blank_is_legal_when_rule_is_off(self) -> None:
        self.assertEqual(
            TOOL.classify(["STATUS", "NAME"], ["|/u01/a/control01.ctl"],
                          first_column_must_be_filled=False),
            [])

    def test_tsv_mode_does_not_report_blank_first_columns(self) -> None:
        """1.4 的 `.tsv` 里首列空白是「NULL 落在定宽列里」，是真实数据行。"""
        with tempfile.TemporaryDirectory() as temporary:
            # TAB 分隔（1.4 的形状）：表头 + 两行首列空白的真实数据
            _write_psv(temporary, "legacy.tsv",
                       ["STATUS\tNAME",
                        "\t/u01/a/control01.ctl",
                        "\t/u01/a/control02.ctl"])
            code, output = _run([temporary, "--include-tsv",
                                 "--script", str(Path(temporary, "none.sh"))])
        self.assertEqual(code, 0)
        self.assertIn("判据 B 关", output)

    def test_tsv_switches_the_delimiter_to_tab(self) -> None:
        """★ 切分符必须跟着版本换：1.4 的 `.tsv` 是 **TAB** 分隔。

        拿 `|` 去切 `.tsv`，值里含 `|` 的行会被误报成「字段数不符」—— 假阳性。
        这里做**配对**：同一份内容，TAB 语义下不报、`|` 语义下必须报。
        """
        row = "a|b|c\t/u01/x.ctl"           # 首字段含两个竖线
        with tempfile.TemporaryDirectory() as temporary:
            _write_psv(temporary, "legacy.tsv", ["STATUS\tNAME", row])
            code_tsv, out_tsv = _run([temporary, "--include-tsv",
                                      "--script", str(Path(temporary, "none.sh"))])
            # 同一份内容放进 `.psv` ⇒ 按 `|` 切，就该报出来
            _write_psv(temporary, "same_content.psv", ["STATUS\tNAME", row])
            code_psv, out_psv = _run([temporary, "--script", str(Path(temporary, "none.sh"))])
        self.assertEqual(code_tsv, 0, out_tsv)
        self.assertIn("无违规", out_tsv)
        self.assertEqual(code_psv, 1, out_psv)
        self.assertIn("字段数", out_psv)

    def test_psv_mode_reports_a_blank_first_column(self) -> None:
        """同一份 `.psv` 语义下就该被报出来 —— 证明上面的"不报"来自开关，不是判据失灵。"""
        with tempfile.TemporaryDirectory() as temporary:
            _write_psv(temporary, "current.psv",
                       ["STATUS|NAME", "|/u01/a/control01.ctl"])
            code, output = _run([temporary, "--script", str(Path(temporary, "none.sh"))])
        self.assertEqual(code, 1)
        self.assertIn("首列为空", output)


class WarningOnlyTests(unittest.TestCase):
    """⑤ 判据 C（触顶）只警告：值恰好等于上限**不等于**确证被截断。"""

    def test_touch_limit_warns_without_failing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            script = Path(temporary) / "fake.sh"
            script.write_text('SELECT SUBSTR(value,1,10) value FROM v$p;\n', encoding="utf-8")
            # VALUE 最长 10 字符 == SUBSTR 上限 10 ⇒ 触顶
            _write_psv(temporary, "touched.psv", ["NAME|VALUE", "db_name|0123456789"])
            code, output = _run([temporary, "--script", str(script)])
        self.assertEqual(code, 0, "触顶只该警告，不该把门禁搞红")
        self.assertIn("触到 SUBSTR 上限", output)

    def test_substr_limits_are_read_case_insensitively(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            script = Path(temporary) / "fake.sh"
            script.write_text('SELECT SUBSTR(t.Value,1,400) value FROM v$p;\n', encoding="utf-8")
            limits = TOOL.substr_limits(script)
        self.assertEqual(limits.get("VALUE"), {400})


if __name__ == "__main__":
    unittest.main()
