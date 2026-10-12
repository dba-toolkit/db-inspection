# -*- coding: utf-8 -*-
"""批 19 守门：Oracle 取数键静态审计工具（`tools/check_oracle_lookup_keys.py`）的自测。

为什么要给尺子写测试：这份工具的全部价值在于"**取数键写错了、报告里却看不出**"这类
静默缺陷 —— 而它自己同样是静默的：尺子坏了（聚合口径错、API 漏收）会**永远绿**，
比没有还危险。所以这里逐条钉住：

1. **尺子自证** —— 多行 `or` 链归成一个取数点（按行聚合会造出假缺陷）。
2. **配一条"故意违规的源码喂进同一个函数、必须被抓到"** —— 只断言"合规的通过"等于没测。
3. **受监控 API 清单不许漂** —— 新加取数 API 却忘了登记，新调用点对尺子不可见（静默漏审），
   这里改成断言"源码里所有取表动作都在清单里"，加 API 会真的变红。
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
    path = ROOT / "tools" / "check_oracle_lookup_keys.py"
    spec = importlib.util.spec_from_file_location("check_oracle_lookup_keys", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


tool = _load_tool()


class RulerSelfProofTests(unittest.TestCase):
    """① 尺子自证：不合规的输入必须**真的**被抓到。"""

    def test_builtin_self_proof_passes(self) -> None:
        ok, detail = tool._self_proof()
        self.assertTrue(ok, detail)
        self.assertIn("or 链归并正确", detail)

    def test_a_multiline_or_chain_is_one_site_not_three(self) -> None:
        source = (
            'rows = (ctx.tables.get("甲", [])\n'
            '        or ctx.tables.get("乙", [])\n'
            '        or ctx.tables.get("丙", []))\n'
        )
        site = tool.sites_from_source(source)
        self.assertEqual(list(site), [1], f"三行 or 链应归成一个取数点，实际 {site}")
        self.assertEqual(set(site[1]), {"甲", "乙", "丙"})

    def test_per_line_aggregation_would_manufacture_fake_defects(self) -> None:
        """反证：真正要防的是"按行拆成三个取数点"——那把同一个表达式拆成了三个假缺陷。"""
        source = (
            'rows = (ctx.tables.get("甲", [])\n'
            '        or ctx.tables.get("乙", [])\n'
            '        or ctx.tables.get("丙", []))\n'
        )
        lines_with_keys = [n for n in source.splitlines() if 'ctx.tables.get' in n]
        self.assertEqual(len(lines_with_keys), 3)
        self.assertEqual(len(tool.sites_from_source(source)), 1)


class RulerCatchesDeliberateViolationsTests(unittest.TestCase):
    """② 负控制：把故意违规的源码喂进**同一个**函数，必须抓到（否则尺子永远绿）。"""

    def test_table_fetch_multi_argument_is_fully_harvested(self) -> None:
        site = tool.sites_from_source('f = ctx.table_fetch("甲", "乙", "丙")\n')
        self.assertEqual(set(site[1]), {"甲", "乙", "丙"})

    def test_subscript_form_is_visible(self) -> None:
        site = tool.sites_from_source('rows = ctx.tables["甲"]\n')
        self.assertEqual(set(site[1]), {"甲"})

    def test_unrelated_dict_get_is_not_harvested(self) -> None:
        """防误伤：`cfg.get("category")` 不是取表键，不该被算进取数点。"""
        site = tool.sites_from_source('kind = cfg.get("category")\n')
        self.assertEqual(site, {})

    def test_non_literal_key_is_visible_as_a_site_without_names(self) -> None:
        """键是变量时也要留下取数点（只是没有字面量可对账）——不能整点消失。"""
        site = tool.sites_from_source('rows = ctx.tables.get(variable_name, [])\n')
        self.assertEqual(len(site), 1)
        self.assertEqual(site[1], [])


class WatchedApiIsInSyncTests(unittest.TestCase):
    """③ 受监控 API 清单与源码同步 —— 加 API 忘登记 = 新调用点静默漏审。

    ★ 判据要写成「**在 ctx 上**调用的、名字里带 table 的方法」而不是「名字里带 table
      的方法」：后者会被 `self._check_tablespace_usage()` 命中（`tablespace` 里含
      `table`），是典型子串陷阱（与 `"SYNCHRONOUS" in "ASYNCHRONOUS_COMMIT"` 同源）。
      下面 `test_tablespace_helper_is_not_mistaken_for_a_table_lookup` 就是给这条判据
      配的"故意违规/故意误伤"用例。
    """

    CTX_RECEIVERS = {"ctx", "context"}

    def _unlisted(self, source: str) -> list[str]:
        import ast

        watched = set(tool._TABLE_GETTERS) | set(tool._TABLE_ATTRS)
        unlisted: list[str] = []
        for node in ast.walk(ast.parse(source)):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not isinstance(func, ast.Attribute) or not isinstance(func.value, ast.Name):
                continue
            if func.value.id not in self.CTX_RECEIVERS:
                continue
            if "table" not in func.attr.lower():
                continue
            if func.attr not in watched:
                unlisted.append(f"ctx.{func.attr}()")
        return unlisted

    def test_tablespace_helper_is_not_mistaken_for_a_table_lookup(self) -> None:
        """防误伤：规则引擎自己的 `self._check_tablespace_usage()` 不是取表动作。"""
        self.assertEqual(self._unlisted("self._check_tablespace_usage(ctx, metrics)\n"), [])
        self.assertEqual(self._unlisted("engine.build_tablespace_plan()\n"), [])

    def test_an_unlisted_ctx_table_api_is_caught(self) -> None:
        """防漏判：真在 ctx 上新加一个取表 API 且没登记，必须被抓到。"""
        self.assertEqual(
            self._unlisted('rows = ctx.lookup_table("甲")\n'), ["ctx.lookup_table()"]
        )

    def test_every_table_like_attribute_call_in_sources_is_watched(self) -> None:
        unlisted: list[str] = []
        for rel in tool.SOURCES:
            for name in self._unlisted((ROOT / rel).read_text(encoding="utf-8")):
                unlisted.append(f"{rel}  {name}")
        self.assertEqual(
            unlisted, [],
            "这些取表动作没进 `_TABLE_GETTERS`，对尺子不可见（加完 API 记得同步）：\n  "
            + "\n  ".join(unlisted),
        )

    def test_sources_are_the_judgement_layer_only(self) -> None:
        self.assertEqual(
            set(tool.SOURCES),
            {"plugins/oracle/metrics.py", "plugins/oracle/oracle_rules.py"},
        )
        for rel in tool.SOURCES:
            self.assertTrue((ROOT / rel).exists(), rel)


class VerdictTaxonomyTests(unittest.TestCase):
    """④ 四档判定：命中 / 单边 / 未命中 / 含未登记名。"""

    def _audit(self, source: str, packages: dict) -> dict:
        return tool.audit(packages, sources={"plugins/oracle/x.py": source})

    def test_hit_when_every_package_resolves_the_key(self) -> None:
        result = self._audit(
            'rows = ctx.table_fetch("oracle.awr.hwm_segments")\n',
            {"1.4": {"oracle.awr.hwm_segments": [{"A": "1"}]},
             "2.0": {"oracle.awr.hwm_segments": [{"A": "1"}]}},
        )
        self.assertEqual(result["sites"][0]["verdict"], "hit")

    def test_single_when_only_one_package_resolves(self) -> None:
        result = self._audit(
            'rows = ctx.table_fetch("oracle.awr.hwm_segments")\n',
            {"1.4": {}, "2.0": {"oracle.awr.hwm_segments": [{"A": "1"}]}},
        )
        self.assertEqual(result["sites"][0]["verdict"], "single")

    def test_missing_when_no_package_resolves(self) -> None:
        result = self._audit(
            'rows = ctx.table_fetch("oracle.awr.hwm_segments")\n',
            {"1.4": {}, "2.0": {}},
        )
        self.assertEqual(result["sites"][0]["verdict"], "missing")

    def test_unregistered_name_is_flagged_even_when_it_happens_to_resolve(self) -> None:
        """★ 顺序陷阱：判定先按"命中与否"算，`unknown_keys` 再覆盖显示。

        所以要单独断言 `unknown_keys` 本身 —— 只看 `verdict` 会漏掉"名字拼错、
        恰好被某个包的键命中"这一种（真包上不会，合成包上会）。
        """
        result = self._audit(
            'rows = ctx.table_fetch("THIS_NAME_IS_NEVER_REGISTERED")\n',
            {"2.0": {"THIS_NAME_IS_NEVER_REGISTERED": [{"A": "1"}]}},
        )
        site = result["sites"][0]
        self.assertEqual(site["unknown_keys"], ["THIS_NAME_IS_NEVER_REGISTERED"])
        self.assertTrue(tool.has_unregistered_names(result))

    def test_registered_key_has_no_unknown_marker(self) -> None:
        result = self._audit(
            'rows = ctx.table_fetch("oracle.awr.hwm_segments")\n', {"2.0": {}}
        )
        self.assertEqual(result["sites"][0]["unknown_keys"], [])
        self.assertFalse(tool.has_unregistered_names(result))

    def test_os_layer_keys_are_allowed_without_being_in_item_titles(self) -> None:
        """`system.filesystems` / `hugepages` 来自 OS 层采集（不在 `item_titles.json`）。"""
        result = self._audit(
            'rows = ctx.table_fetch("filesystems", "system.filesystems")\n', {"2.0": {}}
        )
        self.assertEqual(result["sites"][0]["unknown_keys"], [])


class GateExitCodeTests(unittest.TestCase):
    """⑤ 门禁接线：「含未登记名」必须真的让进程非 0 退出。

    ★ 不写 `main` 的替身 —— 直接把**故意违规的源码**放进临时文件、临时改 `SOURCES`
      指向它，再跑真正的 `main()`。断言的是"进程真的非 0 退出"，而不是"我以为会非 0"。
    """

    def _run_main(self, source: str) -> int:
        with tempfile.TemporaryDirectory(dir=ROOT / "_work") as temporary:
            probe = Path(temporary) / "probe.py"
            probe.write_text(source, encoding="utf-8")
            rel = probe.relative_to(ROOT).as_posix()
            original_sources, original_samples = tool.SOURCES, tool.SAMPLE_PACKAGES
            tool.SOURCES = (rel,)
            tool.SAMPLE_PACKAGES = ()  # 不加载真包：本用例只测门禁接线，静态对账足够
            try:
                with contextlib.redirect_stdout(io.StringIO()) as buffer:
                    code = tool.main([])
            finally:
                tool.SOURCES, tool.SAMPLE_PACKAGES = original_sources, original_samples
        return code

    def test_main_exits_nonzero_on_an_unregistered_key(self) -> None:
        self.assertEqual(
            self._run_main('rows = ctx.table_fetch("ZZZ_NEVER_REGISTERED_ANYWHERE")\n'), 1
        )

    def test_main_exits_zero_on_a_registered_key(self) -> None:
        self.assertEqual(
            self._run_main('rows = ctx.table_fetch("oracle.awr.hwm_segments")\n'), 0
        )

    def test_gate_helper_reads_every_site(self) -> None:
        clean = {"sites": [{"unknown_keys": []}, {"unknown_keys": []}]}
        dirty = {"sites": [{"unknown_keys": []}, {"unknown_keys": ["拼错的名字"]}]}
        self.assertFalse(tool.has_unregistered_names(clean))
        self.assertTrue(tool.has_unregistered_names(dirty))

    def test_missing_and_single_do_not_fail_the_gate(self) -> None:
        """未命中 / 单边可能是"这一项本来没采"，不许当错字拦下来。"""
        result = {
            "sites": [
                {"unknown_keys": [], "verdict": "missing"},
                {"unknown_keys": [], "verdict": "single"},
            ]
        }
        self.assertFalse(tool.has_unregistered_names(result))


class RenderTests(unittest.TestCase):
    """⑥ 呈现：「三档不一致」在文本上必须长得不一样。"""

    @staticmethod
    def _result() -> dict:
        return {
            "packages": ["1.4", "2.0"],
            "sites": [
                {"file": "plugins/oracle/a.py", "line": 1, "keys": ["甲"],
                 "unknown_keys": [], "resolved": {"1.4": "甲", "2.0": "甲"}, "verdict": "hit"},
                {"file": "plugins/oracle/a.py", "line": 2, "keys": ["乙"],
                 "unknown_keys": [], "resolved": {"1.4": "", "2.0": "乙"}, "verdict": "single"},
                {"file": "plugins/oracle/a.py", "line": 3, "keys": ["丙"],
                 "unknown_keys": [], "resolved": {"1.4": "", "2.0": ""}, "verdict": "missing"},
                {"file": "plugins/oracle/a.py", "line": 4, "keys": ["丁"],
                 "unknown_keys": ["丁"], "resolved": {"1.4": "", "2.0": ""}, "verdict": "missing"},
            ],
        }

    def test_verdicts_render_as_distinct_marks(self) -> None:
        text = tool.render(self._result())
        self.assertIn("✓", text)
        self.assertIn("! 单边", text)
        self.assertIn("— 未命中", text)
        self.assertIn("✗ 含未登记名", text)

    def test_key_missing_and_key_unregistered_do_not_read_the_same(self) -> None:
        """★ 「未命中」（可能本来没采）与「含未登记名」（纯错字）是两种不同的错，
        不许渲染成一个样 —— 否则读者分不清该去改代码还是该去补采集。"""
        text = tool.render(self._result())
        missing_line = [x for x in text.splitlines() if "a.py:3" in x]
        unregistered_line = [x for x in text.splitlines() if "a.py:4" in x]
        self.assertEqual(len(missing_line), 1)
        self.assertEqual(len(unregistered_line), 1)
        self.assertNotEqual(missing_line[0], unregistered_line[0])

    def test_summary_counts_every_verdict(self) -> None:
        text = tool.render(self._result())
        self.assertIn("共 4 个取数点", text)
        self.assertIn("命中 1", text)
        self.assertIn("单边 1", text)
        self.assertIn("未命中 2", text)
        self.assertIn("含未登记名 1", text)


class LiveRepositoryTests(unittest.TestCase):
    """⑦ 真仓库：判定层取数键必须 0 个"未登记名"，自证通过、门禁为 0。"""

    def test_repository_has_no_unregistered_lookup_keys(self) -> None:
        result = tool.audit({})
        offenders = [
            f"{row['file']}:{row['line']}  {' / '.join(row['unknown_keys'])}"
            for row in result["sites"]
            if row["unknown_keys"]
        ]
        self.assertEqual(offenders, [], "取数键里出现了从未登记的名字：\n  " + "\n  ".join(offenders))

    def test_repository_main_returns_zero(self) -> None:
        """不带包时只做静态词表对账（不加载真包，避免单测拖成分钟级），仍必须过门禁。"""
        original = tool.SAMPLE_PACKAGES
        tool.SAMPLE_PACKAGES = ()
        try:
            with contextlib.redirect_stdout(io.StringIO()) as buffer:
                code = tool.main([])
        finally:
            tool.SAMPLE_PACKAGES = original
        self.assertEqual(code, 0)
        self.assertIn("尺子自证通过", buffer.getvalue())

    def test_audit_finds_the_expected_number_of_sites(self) -> None:
        """取数点数量是尺子的"敏感度"基线：聚合成语句后不应再有 30+ 的分裂假点。"""
        result = tool.audit({})
        self.assertGreaterEqual(len(result["sites"]), 20)
        self.assertLessEqual(len(result["sites"]), 60)


if __name__ == "__main__":
    unittest.main()
