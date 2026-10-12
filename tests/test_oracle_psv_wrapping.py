# -*- coding: utf-8 -*-
"""批 16 守门：`.psv` 的「一行一记录」契约。

病（2026-10-12 实测，源包 2026-10-10）：`5.1 关键初始化参数` 里 `control_files` 的值
被 sqlplus 按 `COLUMN value FORMAT a80` 折成两行，残段 `|02.ctl` 在报告里变成
**第二条参数**（名字空）。根因链三层：

  ① 采集层：SQL 里是 `SUBSTR(value,1,400)`，宽度声明却是 `a80` ⇒ 差值 320 字符被折行；
  ② 采集层：`WORD_WRAPPED` 是**按词**折行，残段无法用「无分隔符续接」还原；
  ③ 解析层：`psv_normalize` 把值里的 ` | ` 压成 `|` ⇒ `LISTAGG(name,' | ')` 的第二个
     路径被当成列边界，静默丢进 csv 的 `None` 列（1.4 不压空白，所以一直没暴露）；
  ④ 展示层：值列又写死 `[:80]`，正好也切在第 80 个字符 —— **让 ①②③ 的修复在报告里看不见**。
     该层的守门在 `tests/test_oracle_report_quality.py::InitParameterValueDisplayTests`
     （那里有现成的 `build()` 合成 ctx 夹具），本文件不重复搭。

①②③ 都用**结构判据**守住；另有一条「尺子自证」——把故意违规的源码喂给**同一个** `check_script`。
"""
from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path

from plugins.oracle.package_adapter import (
    LEGACY_COLLECTOR_MAJOR,
    _iter_table_specs,
    _rejoin_wrapped_rows,
    parse_delimited,
)

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "inspection" / "oracle_inspection.sh"


def _load_checker():
    spec = importlib.util.spec_from_file_location(
        "check_oracle_psv_widths", ROOT / "tools" / "check_oracle_psv_widths.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


CHECKER = _load_checker()


class CheckerSelfProofTests(unittest.TestCase):
    """尺子自证：把故意违规的源码喂给**同一个** check_script，必须抓到。"""

    def test_undeclared_substr_alias_is_caught(self) -> None:
        src = 'ora_sql_to_md "x.y" "T" "\nSELECT SUBSTR(value,1,400) value\nFROM v$p;"\n'
        problems = CHECKER.check_script(src)
        self.assertTrue(any("宽度失配" in p for p in problems), problems)

    def test_width_narrower_than_truncation_is_caught(self) -> None:
        src = (
            "COLUMN value FORMAT a80\n"
            'ora_sql_to_md "x.y" "T" "\nSELECT SUBSTR(value,1,400) value\nFROM v$p;"\n'
        )
        problems = CHECKER.check_script(src)
        self.assertTrue(any("宽度失配" in p for p in problems), problems)

    def test_declared_width_equal_to_truncation_passes(self) -> None:
        src = (
            "COLUMN value FORMAT a400\n"
            'ora_sql_to_md "x.y" "T" "\nSELECT SUBSTR(value,1,400) value\nFROM v$p;"\n'
        )
        self.assertEqual(CHECKER.check_script(src), [])

    def test_pipe_inside_literal_is_caught(self) -> None:
        src = 'ora_sql_to_md "x.y" "T" "\nSELECT LISTAGG(n,\' | \') p\nFROM v$c;"\n'
        problems = CHECKER.check_script(src)
        self.assertTrue(any("竖线字面量" in p for p in problems), problems)

    def test_concatenation_operator_is_not_a_literal(self) -> None:
        """★ 误伤检查：`'GB' ELSE x || 'MB'` 不是含竖线的字面量。

        朴素正则 `'[^']*\\|[^']*'` 会从**前一个字面量的收尾引号**匹配到
        **后一个字面量的起始引号**（命中 `' ELSE x || '`）—— 这就是本用例要钉住的误报。
        """
        src = (
            'ora_sql_to_md "x.y" "T" "\n'
            "SELECT CASE WHEN a >= 1024 THEN ROUND(a/1024,2) || 'GB' ELSE a || 'MB' END AS sz\n"
            "FROM v$d;"
            '"\n'
        )
        self.assertEqual(CHECKER.check_script(src), [])

    def test_same_alias_declared_twice_at_one_width_is_fine(self) -> None:
        """★ 误伤检查：大小写两份声明**同宽**是 sqlplus 的要求（列名大小写敏感），不是冲突。"""
        src = (
            "COLUMN value FORMAT a500\n"
            "COLUMN VALUE FORMAT a500\n"
            'ora_sql_to_md "x.y" "T" "\nSELECT SUBSTR(value, 1, 400) value\nFROM v$p;"\n'
        )
        self.assertEqual(CHECKER.check_script(src), [])

    def test_narrowed_declaration_of_a_twice_declared_alias_is_caught(self) -> None:
        """★ 这条是**负控制实测抓出来的漏洞**，不是想出来的：

        判 ① 按「同名取最大值」比较，于是 `value a500` 后面跟一条 `VALUE a80`
        （sqlplus 列名大小写不敏感，两类都写是常规做法）时，`max` 取到 500 ⇒ 全绿。
        把采集层真实改窄（a500 → a80）就是这么溜过去的。
        """
        src = (
            "COLUMN value FORMAT a80\n"
            "COLUMN VALUE FORMAT a500\n"
            'ora_sql_to_md "x.y" "T" "\nSELECT SUBSTR(value, 1, 400) value\nFROM v$p;"\n'
        )
        problems = CHECKER.check_script(src)
        self.assertTrue(any("宽度冲突" in p for p in problems), problems)

    def test_width_conflict_is_reported_through_the_group_view(self) -> None:
        """判 ④ 的取值入口本身要能用（避免它只被 check_script 间接覆盖）。"""
        groups = CHECKER.column_width_groups("COLUMN value FORMAT a500\nCOLUMN value FORMAT a80\n")
        self.assertEqual(groups["value"], {80, 500})

    def test_word_wrapped_on_column_line_is_caught(self) -> None:
        src = "COLUMN message FORMAT a120 WORD_WRAPPED\n"
        problems = CHECKER.check_script(src)
        self.assertTrue(any("WORD_WRAPPED" in p for p in problems), problems)

    def test_word_wrapped_in_a_comment_is_not_caught(self) -> None:
        """★ 误伤检查：注释里提到这个词不算违规（否则改注释就会让守门变红）。"""
        src = "# 注意：本文件不再使用 WORD_WRAPPED\n"
        self.assertEqual(CHECKER.check_script(src), [])

    def test_real_collector_script_passes(self) -> None:
        script = SCRIPT.read_text(encoding="utf-8")
        self.assertEqual(CHECKER.check_script(script), [])
        self.assertGreaterEqual(len(CHECKER.iter_sql_bodies(script)), 100)

    def test_heredoc_body_has_no_backtick_or_bare_dollar(self) -> None:
        """未加引号的 heredoc：正文里的反引号会被当真命令执行、裸 `$x` 会被展开。"""
        script = SCRIPT.read_text(encoding="utf-8")
        start = script.index('ora_exec > "$tmp" 2> "$err_file" <<EOF')
        start = script.index("\n", start)
        end = script.index("\nEOF", start)
        body = script[start:end]
        self.assertNotIn("`", body)


class RejoinWrappedRowTests(unittest.TestCase):
    """解析层兜底：SQL*Plus 断词折行的续行回并。"""

    def test_two_column_value_wrap_is_rejoined(self) -> None:
        lines = [
            "NAME|VALUE",
            "control_files|/u01/app/oracle/oradata/ZYDB/control01.ctl, /u01/app/oracle/oradata/ZYDB/control",
            "|02.ctl",
            "cursor_sharing|EXACT",
        ]
        merged = _rejoin_wrapped_rows(lines, "|")
        self.assertEqual(len(merged), 3)
        self.assertEqual(
            merged[1],
            "control_files|/u01/app/oracle/oradata/ZYDB/control01.ctl, "
            "/u01/app/oracle/oradata/ZYDB/control02.ctl",
        )

    def test_three_physical_lines_chain(self) -> None:
        lines = ["K|V", "a|first-", "|second-", "|third"]
        merged = _rejoin_wrapped_rows(lines, "|")
        self.assertEqual(merged, ["K|V", "a|first-second-third"])

    def test_wrap_in_middle_column_keeps_trailing_columns(self) -> None:
        """折在第 2 列、后面还有列时：残段续接第 2 列，**还没打印的列**跟在残段后面补进来。

        形状来自 sqlplus 的真实行为：某一列折行时，它**后面的列**会打印在它结束的那一行，
        所以残段是「空位 + 值后半 + 后续列」，字段数比上一行多。
        """
        lines = [
            "NAME|VALUE|ISDEFAULT|ISMODIFIED",
            "undo_retention|/u01/app/oracle/oradata/ZYDB/undo01.d",
            "|bf|TRUE|FALSE",
        ]
        merged = _rejoin_wrapped_rows(lines, "|")
        self.assertEqual(len(merged), 2)
        self.assertEqual(
            merged[1],
            "undo_retention|/u01/app/oracle/oradata/ZYDB/undo01.dbf|TRUE|FALSE",
        )

    def test_wrap_in_last_column_over_three_physical_lines(self) -> None:
        lines = ["A|B|C", "x|y|p1", "||p2", "||p3"]
        merged = _rejoin_wrapped_rows(lines, "|")
        self.assertEqual(merged, ["A|B|C", "x|y|p1p2p3"])

    def test_tab_delimited_tables_are_untouched(self) -> None:
        """真 TAB 表格没有 `SET NULL '-'` 契约，不能套用这条判据。"""
        lines = ["a\tb", "\tc", "d\te"]
        self.assertEqual(_rejoin_wrapped_rows(lines, "\t"), lines)

    def test_ambiguous_row_is_left_alone(self) -> None:
        """上一行那一格是空的 → 形状对不上，不猜，原样保留（宁可露出，不要猜错）。"""
        lines = ["A|B|C", "x||", "|orphan"]
        self.assertEqual(_rejoin_wrapped_rows(lines, "|"), lines)

    def test_leading_empty_row_without_predecessor_is_left_alone(self) -> None:
        lines = ["|no-predecessor"]
        self.assertEqual(_rejoin_wrapped_rows(lines, "|"), lines)

    def test_continuation_beyond_the_previous_row_extends_it(self) -> None:
        """残段里出现上一行还没打印到的列 ⇒ 补进来（`K|V` + `|a|b` → `K|Va|b`）。"""
        lines = ["K|V", "|a|b"]
        self.assertEqual(_rejoin_wrapped_rows(lines, "|"), ["K|Va|b"])


class ParseDelimitedEndToEndTests(unittest.TestCase):
    """走真实入口 `parse_delimited`：值完整、行数与表头一致。

    ★ `repair_wrap` **默认关**，只有 2.0 的 `.psv` 由 `_iter_table_specs` 打开。
    默认关是**fail-safe 选择**：忘了传参的代价是"折行没修"（报告里看得见），
    而不是"真实数据行被并进表头"（静默吃掉数据）。
    """

    def _parse(self, text: str, delimiter: str = "|", repair_wrap: bool = True):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sample.psv"
            path.write_text(text, encoding="utf-8", newline="\n")
            return parse_delimited(path, delimiter, repair_wrap=repair_wrap)

    def test_wrapped_value_survives_the_real_entry_point(self) -> None:
        rows = self._parse(
            "NAME|VALUE\n"
            "control_files|/u01/app/oracle/oradata/ZYDB/control01.ctl, "
            "/u01/app/oracle/oradata/ZYDB/control\n"
            "|02.ctl\n"
            "db_name|zydb\n"
        )
        self.assertEqual([r["NAME"] for r in rows], ["control_files", "db_name"])
        self.assertEqual(
            rows[0]["VALUE"],
            "/u01/app/oracle/oradata/ZYDB/control01.ctl, "
            "/u01/app/oracle/oradata/ZYDB/control02.ctl",
        )

    def test_repair_is_off_by_default(self) -> None:
        """★ 默认关：1.4 那类"首列是空白、但其实是真实数据行"的表不会被误并。"""
        rows = self._parse(
            "STATUS |NAME\n"
            "       |/u01/app/oracle/oradata/ZYDB/control01.ctl\n"
            "       |/u01/app/oracle/oradata/ZYDB/control02.ctl\n",
            repair_wrap=False,
        )
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["NAME"], "/u01/app/oracle/oradata/ZYDB/control01.ctl")
        self.assertEqual(rows[1]["NAME"], "/u01/app/oracle/oradata/ZYDB/control02.ctl")
        # 空白的第一列解析成空串（键与值都会被 strip，所以键是 `STATUS`）——
        # 它**是数据**，不是残段。
        self.assertEqual(rows[0]["STATUS"], "")

    def test_turning_the_repair_on_does_corrupt_that_shape(self) -> None:
        """★ 把反例钉死：同一份 1.4 形状喂给**开着回并**的入口，整表塌成 **0 行**。

        比"并成一行"更糟：表头把两行数据都吸进去，`csv.DictReader` 于是没有任何数据行
        ⇒ 报告里这一项**直接变空**（08-10 基线实测：`控制文件路径` 2 行 → 0 行）。
        这条不是"期望行为"，是**错误示范** —— 正因为开了会这样，`_iter_table_specs`
        才必须对 1.4 关掉它。
        """
        rows = self._parse(
            "STATUS |NAME\n"
            "       |/u01/app/oracle/oradata/ZYDB/control01.ctl\n"
            "       |/u01/app/oracle/oradata/ZYDB/control02.ctl\n",
            repair_wrap=True,
        )
        self.assertEqual(rows, [])

    def test_placeholder_rows_still_return_empty_table(self) -> None:
        self.assertEqual(
            self._parse("# oracle-inspection psv-empty item_id=x sep=| rows=0 status=empty\n"),
            [],
        )


class TableSpecVersionTests(unittest.TestCase):
    """回并开关的**唯一来源**是采集器版本，不是分隔符。"""

    def test_legacy_tsv_is_not_repaired(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tables = Path(tmp)
            (tables / "控制文件.tsv").write_text("STATUS |NAME\n", encoding="utf-8")
            specs = _iter_table_specs(tables, LEGACY_COLLECTOR_MAJOR)
        self.assertEqual([(p.suffix, delim, repair) for p, delim, repair in specs],
                         [(".tsv", "|", False)])

    def test_20_psv_is_repaired_and_20_tsv_is_not(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tables = Path(tmp)
            (tables / "oracle.basic.parameters.psv").write_text("NAME|VALUE\n", encoding="utf-8")
            (tables / "kernel_parameters.tsv").write_text("path\tvalue\n", encoding="utf-8")
            specs = _iter_table_specs(tables, "2")
        got = {p.suffix: (delim, repair) for p, delim, repair in specs}
        self.assertEqual(got[".psv"], ("|", True))
        self.assertEqual(got[".tsv"], ("\t", False))


if __name__ == "__main__":
    unittest.main()
