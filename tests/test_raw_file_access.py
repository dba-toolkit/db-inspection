"""批 14：取数口径统一 —— 非表格式证据文件一律走 ``ctx.raw_lines``。

守两件事：

① **行为**：``PackageContext.raw_lines`` 按**契约文件名**在 ``tables/`` 与 ``evidence/``
   两处查找并返回原始行；缺文件返回空（既不抛异常，也不静默造值）。
② **结构**：Oracle / MySQL 的 ``presentation.py`` 里**不许再出现 ``ctx.root / …`` 的路径拼接**。
   那正是 N-8「4 个列对齐文本从 ``tables/`` 挪到 ``evidence/``」会**静默少一节内容**的原因
   —— 读不到只返回空，规则层不报、测试不红。

★ 本文件里 ``test_ruler_catches_a_regression`` 是**尺子自证**：把一段拼路径的源码喂给
  结构断言用的**同一个函数**，必须抓到。没有它，结构性断言可能是"永远绿"的空断言
   （批 13 的教训：断言形式与被测对象不匹配时，绿色不代表正确）。
"""

from __future__ import annotations

import ast
import tempfile
import unittest
from pathlib import Path

from inspection_core import PackageContext
from inspection_core.models import _RAW_FILE_DIRS
from plugins.oracle.package_adapter import OraclePackageContext

ROOT = Path(__file__).resolve().parents[1]

#: 批 14 已收口的两个库。**PostgreSQL 仍是同一模式（9 处，见 fix-plan 批 14）**，
#: 未纳入本守门 —— 纳入了会当场红，那是"待办"不是"缺陷"。
GUARDED_PRESENTATIONS = (
    ROOT / "plugins" / "oracle" / "presentation.py",
    ROOT / "plugins" / "mysql" / "presentation.py",
)


def _make_ctx(root: Path) -> PackageContext:
    return PackageContext(
        source=root, root=root, snapshot={}, status={}, manifest={}, integrity={}
    )


class RawLinesAccessorTests(unittest.TestCase):
    """访问点行为：两处都能命中、缺文件不炸、目录顺序是契约。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "tables").mkdir()
        (self.root / "evidence").mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_finds_file_under_tables(self) -> None:
        # kernel_parameters.tsv 实际就落在 tables/（key-value TSV，不是表格式）
        (self.root / "tables" / "kernel_parameters.tsv").write_text(
            "vm.swappiness\t60\n", encoding="utf-8"
        )
        self.assertEqual(
            _make_ctx(self.root).raw_lines("kernel_parameters.tsv"),
            ["vm.swappiness\t60"],
        )

    def test_finds_file_under_evidence(self) -> None:
        (self.root / "evidence" / "lscpu.txt").write_text(
            "Model name: Xeon\n", encoding="utf-8"
        )
        self.assertEqual(
            _make_ctx(self.root).raw_lines("lscpu.txt"), ["Model name: Xeon"]
        )

    def test_missing_file_returns_empty_list(self) -> None:
        self.assertEqual(_make_ctx(self.root).raw_lines("nope.txt"), [])

    def test_missing_root_returns_empty_list(self) -> None:
        # 真实包目录不在时不许抛 —— 报告应"少一节并如实说明"，不是崩掉
        ghost = self.root / "no_such_dir"
        self.assertEqual(_make_ctx(ghost).raw_lines("lscpu.txt"), [])

    def test_tables_dir_is_searched_first(self) -> None:
        # 目录顺序是契约的一部分（tables/ 只放表格式数据、evidence/ 只放原文证据，
        # 同名不会在两处并存）；把顺序钉住，改动顺序会立刻被发现。
        (self.root / "tables" / "x.txt").write_text("from-tables\n", encoding="utf-8")
        (self.root / "evidence" / "x.txt").write_text("from-evidence\n", encoding="utf-8")
        self.assertEqual(_make_ctx(self.root).raw_lines("x.txt"), ["from-tables"])

    def test_dir_order_is_single_sourced(self) -> None:
        self.assertEqual(tuple(_RAW_FILE_DIRS), ("tables", "evidence"))


class AccessorIsInheritedNotReimplementedTests(unittest.TestCase):
    """单一来源：访问点在共享基类上，子类只继承、不另写一份。"""

    def test_accessor_lives_on_the_shared_base(self) -> None:
        self.assertTrue(callable(getattr(PackageContext, "raw_lines", None)))

    def test_oracle_context_inherits_the_accessor(self) -> None:
        self.assertTrue(issubclass(OraclePackageContext, PackageContext))
        # 继承来的就是基类那一个函数对象（若子类覆写会不等 ⇒ 立刻发现"两份口径"）
        self.assertIs(OraclePackageContext.raw_lines, PackageContext.raw_lines)


class NoHardcodedPackagePathTests(unittest.TestCase):
    """结构：呈现层不许再拼包内目录（改走 ``ctx.raw_lines``）。"""
    @staticmethod
    def _offenders_in_source(source: str) -> list[tuple[int, str]]:
        """返回源码里所有「用 `…/ root / 后面的目录` 拼包内路径」的 (行号, 表达式)。

        判据：`BinOp(op=Div)` 且**左操作数**是 `ctx.root` 这类属性访问，或一个名叫 `root`
        的局部变量。只看左操作数，避免把 `a / b` 这种数值除法误判进来。
        """
        tree = ast.parse(source)
        found: list[tuple[int, str]] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.BinOp) or not isinstance(node.op, ast.Div):
                continue
            left = node.left
            if isinstance(left, ast.Attribute) and left.attr == "root":
                found.append((node.lineno, ast.unparse(node)))
            elif isinstance(left, ast.Name) and left.id == "root":
                found.append((node.lineno, ast.unparse(node)))
        return found

    def _offenders(self, path: Path) -> list[tuple[int, str]]:
        return self._offenders_in_source(path.read_text(encoding="utf-8"))

    def test_ruler_catches_a_regression(self) -> None:
        # ★ 尺子自证：同一函数喂进一段"故意拼路径"的源码必须抓到，否则下面的断言是假绿。
        caught = self._offenders_in_source(
            "x = ctx.root / 'evidence' / 'lscpu.txt'\n"
        )
        self.assertEqual([line for line, _ in caught], [1])
        local = self._offenders_in_source("root = ctx.root\ny = root / 'tables' / 'a.tsv'\n")
        self.assertEqual([line for line, _ in local], [2])

    def test_oracle_presentation_has_no_package_path_building(self) -> None:
        path = GUARDED_PRESENTATIONS[0]
        offenders = self._offenders(path)
        self.assertEqual(
            offenders,
            [],
            f"{path.name} 里仍有拼包内路径的表达式（请改走 ctx.raw_lines）：{offenders}",
        )

    def test_mysql_presentation_has_no_package_path_building(self) -> None:
        path = GUARDED_PRESENTATIONS[1]
        offenders = self._offenders(path)
        self.assertEqual(
            offenders,
            [],
            f"{path.name} 里仍有拼包内路径的表达式（请改走 ctx.raw_lines）：{offenders}",
        )


#: 呈现层**实际消费**的契约文件名（按 AST 从源码里取 `.raw_lines("…")` 的实参）。
#: 改名是**静默**失效模式 —— `raw_lines` 找不到就返回空，报告少一节而没人知道。
ORACLE_RAW_FILES = {
    "lscpu.txt",
    "time_status.txt",
    "kernel_parameters.tsv",
    "mounts.txt",
    "dmesg_errors.txt",
    "os_release.txt",
}
MYSQL_RAW_FILES = {
    "lscpu.txt",
    "kernel_parameters.tsv",
    "mounts.txt",
    "dmesg_errors.txt",
}


class PresentationParsersTakeLinesTests(unittest.TestCase):
    """解析器只吃「行」、**不再吃 Path** —— 想绕过 `ctx.raw_lines` 直接喂路径会当场 TypeError。"""

    def test_oracle_parsers_accept_lines(self) -> None:
        from plugins.oracle.presentation import (
            _colon_map,
            _dmesg_rows,
            _key_value_rows,
            _kv_map,
            _mount_rows,
        )

        self.assertEqual(_colon_map(["Model name: Xeon", "no-colon"]), {"Model name": "Xeon"})
        self.assertEqual(_kv_map(["a=b", "junk"]), {"a": "b"})
        self.assertEqual(
            _key_value_rows(["vm.swappiness\t60", "# c", "x=y"]),
            [{"参数": "vm.swappiness", "值": "60"}, {"参数": "x", "值": "y"}],
        )
        self.assertEqual(_dmesg_rows(["e1", "", "e2"], limit=1), [{"内核错误日志": "e1"}])
        self.assertEqual(len(_mount_rows(["/dev/sda1 on / type ext4 (rw,relatime)"])), 1)

    def test_mysql_parsers_accept_lines(self) -> None:
        from plugins.mysql.presentation import MySQLPresentationBuilder as B

        self.assertEqual(B._raw_key_value_rows(["a\tb", "# c"]), [{"参数": "a", "值": "b"}])
        self.assertEqual(B._colon_key_values(["Model name: Xeon"]), {"Model name": "Xeon"})
        self.assertEqual(B._dmesg_error_rows(["x", "", "y"], limit=1), [{"内核错误日志": "x"}])
        self.assertEqual(len(B._mount_rows(["/dev/sda1 on / type ext4 (rw,relatime)"])), 1)

    def test_passing_a_path_is_rejected(self) -> None:
        # ★ 传 Path 必 TypeError ⇒ "必须走 ctx.raw_lines" 这个口径在运行期也成立，
        #   不是只写在注释里。
        from plugins.oracle.presentation import _colon_map

        with self.assertRaises(TypeError):
            _colon_map(Path("lscpu.txt"))  # type: ignore[arg-type]


class ContractFilenamesTests(unittest.TestCase):
    """呈现层消费的**契约文件名集合**必须与采集器契约一致（改名会静默返回空）。"""

    @staticmethod
    def _raw_lines_args(path: Path) -> set[str]:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        names: set[str] = set()
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "raw_lines"
            ):
                for arg in node.args:
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                        names.add(arg.value)
        return names

    def test_ruler_sees_the_call_sites(self) -> None:
        # 尺子自证：一段 `ctx.raw_lines("x.txt")` 必须被认出来，否则下面的断言是空断言。
        probe = ast.parse('y = ctx.raw_lines("x.txt")\n').body[0]
        found = {
            a.value
            for a in ast.walk(probe)
            if isinstance(a, ast.Constant) and isinstance(a.value, str)
        }
        self.assertEqual(found, {"x.txt"})

    def test_oracle_contract_filenames(self) -> None:
        self.assertEqual(
            self._raw_lines_args(GUARDED_PRESENTATIONS[0]), ORACLE_RAW_FILES
        )

    def test_mysql_contract_filenames(self) -> None:
        self.assertEqual(
            self._raw_lines_args(GUARDED_PRESENTATIONS[1]), MYSQL_RAW_FILES
        )


if __name__ == "__main__":
    unittest.main()
