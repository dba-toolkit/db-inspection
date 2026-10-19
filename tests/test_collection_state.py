"""`inspection_core.collection_state` 的判据契约。

这一层管的是「这项**采到没有**」和「这一**章**的证据够不够」——报告层与规则层
都靠它把「没采到」和「没问题」分开。判据写错的表现是**报告一切正常**，
所以每条期望都必须能说出"为什么是这个值"，且与真输出对齐（本文件的期望值
全部来自实跑输出，不是推演）。
"""

from __future__ import annotations

import unittest

from inspection_core.collection_state import (
    DATA_STATES,
    EMPTY_STATE,
    OUT_OF_SCOPE_STATES,
    SCORE_OK_STATES,
    expand_source_names,
    is_no_data,
    is_out_of_scope,
    merge_section_state,
    scores_as_ok,
    state_is_collected,
    worst_state,
)


class CollectedOrNotTests(unittest.TestCase):
    """「这一项采集过吗」——`empty` 与 `skipped` 是**相反**结论。"""

    def test_empty_counts_as_collected_but_skipped_does_not(self) -> None:
        self.assertTrue(state_is_collected("ok"))
        self.assertTrue(state_is_collected(EMPTY_STATE))     # 查了、确认 0 行
        self.assertTrue(state_is_collected("partial"))
        for missing in ("skipped", "unsupported", "not_enabled", "timeout",
                        "permission_denied", "error", "missing", "unavailable",
                        "schema_mismatch", "unmapped", ""):
            with self.subTest(state=missing):
                self.assertFalse(state_is_collected(missing))

    def test_missing_state_is_treated_as_not_collected(self) -> None:
        """缺状态（`""`）**一律按没采到**处理 —— 宁可写「证据不足」，不可写「检查通过」。"""
        self.assertTrue(is_no_data(""))
        self.assertFalse(state_is_collected(""))

    def test_unknown_and_schema_mismatch_are_not_data(self) -> None:
        # `schema_mismatch` = 表在、列不在（采集器缺陷）⇒ 不是"没有数据"，是"没采到"
        self.assertTrue(is_no_data("schema_mismatch"))
        self.assertTrue(is_no_data("unknown"))
        # `not_applicable` 两边都不算：既不是采到，也不是没采到
        self.assertFalse(is_no_data("not_applicable"))
        self.assertFalse(state_is_collected("not_applicable"))


class OutOfScopeTests(unittest.TestCase):
    """批 3A：完整度的**分母口径**与「这一项采到没有」是两把尺子。

    `skipped` / `unsupported` 在 `is_no_data` 上都为真（⇒ 不能说"没问题"），
    但它们同时也是**本环境不适用**（⇒ 不该算进完整度分母）。两条判据服务两个问题，
    所以必须分别有名字 —— 混用会得出"目标机装的东西越少、完整度越低"这种反结论。
    """

    def test_out_of_scope_membership_is_explicit(self) -> None:
        """词表本身钉住：多一个成员必须是有意的（它会**从分母里消失**）。"""
        self.assertEqual(OUT_OF_SCOPE_STATES,
                         frozenset({"not_applicable", "skipped", "unsupported"}))
        for state in ("not_applicable", "skipped", "unsupported"):
            with self.subTest(state=state):
                self.assertTrue(is_out_of_scope(state))
        # ★ 缺状态 = 不知道，**不许**被当成"不适用"剔出分母（否则分母会被"没登记"悄悄掏空）。
        self.assertFalse(is_out_of_scope(""), "缺状态被当成不适用了 —— 分母会被掏空")
        self.assertTrue(is_no_data(""), "缺状态必须仍算没采到")
        # ★ `not_applicable` 只在"不适用"这一档 —— 它**不是**「没采到」
        #   （`is_no_data` 为假），否则规则层会去写"证据不足"。
        self.assertFalse(is_no_data("not_applicable"))

    def test_real_gaps_are_never_out_of_scope(self) -> None:
        """**负控制**：真缺口一个都不许被剔出分母。"""
        for state in ("error", "timeout", "permission_denied", "schema_mismatch",
                      "partial", "not_enabled", "missing", "unavailable", "unknown", ""):
            with self.subTest(state=state):
                self.assertFalse(is_out_of_scope(state), f"{state!r} 被误判成本环境不适用")

    def test_score_ok_states_is_data_states_minus_partial(self) -> None:
        """打分口径与规则口径**刻意不同源**：`partial` 规则够用、完整度上算缺口。"""
        self.assertEqual(SCORE_OK_STATES, frozenset({"ok", EMPTY_STATE}))
        self.assertEqual(SCORE_OK_STATES, frozenset(set(DATA_STATES) - {"partial"}))
        self.assertIn("partial", DATA_STATES, "partial 不该被移出 DATA_STATES（规则层要用）")
        self.assertNotIn("partial", SCORE_OK_STATES)

    def test_scoring_is_positive_not_negative(self) -> None:
        """★ 打分必须**正着数** —— 判否式在新状态词忘了登记时会**让分数变高**。

        实测过的具体漏洞：缺状态（`""`）不在任何失败集合里，判否式把它算成「到位」。
        这条用"一个谁都没见过的新状态"当探针：正着数 ⇒ 算缺口（安全方向）；
        判否式 ⇒ 算到位（危险方向）。
        """
        self.assertFalse(scores_as_ok(""), "缺状态被算成到位了")
        self.assertFalse(scores_as_ok("brand_new_state_nobody_registered"))
        self.assertTrue(scores_as_ok("ok"))
        self.assertTrue(scores_as_ok(EMPTY_STATE))
        self.assertFalse(scores_as_ok("partial"))


class MergeSectionStateTests(unittest.TestCase):
    """章节级归并：问的是"证据够不够"，**不是**"最坏有多坏"。"""

    CASES = (
        ([], "unmapped"),                       # 台账里一个都查不到 ⇒ 不许默认 ok
        (["ok"], "ok"),
        (["ok", "ok"], "ok"),
        (["partial"], "partial"),
        (["ok", "partial"], "ok"),              # 都算"有证据"
        (["empty"], "empty"),
        (["ok", "empty"], "ok"),                # ★ 真输出校准：empty 算采到 ⇒ 证据齐
        (["skipped"], "skipped"),
        (["unsupported"], "unsupported"),
        (["not_enabled"], "not_enabled"),
        (["not_applicable"], "not_applicable"),
        (["ok", "skipped"], "partial"),         # 有证据也有缺口 ⇒ 必须写出来
        (["empty", "skipped"], "partial"),
        (["ok", "empty", "skipped"], "partial"),
        (["error", "ok"], "error"),             # 严重失败优先报
        (["timeout", "skipped"], "timeout"),
        (["schema_mismatch", "error"], "error"),
        (["", ""], "unmapped"),                 # 缺状态不参与判据
    )

    def test_merge_table(self) -> None:
        for states, expect in self.CASES:
            with self.subTest(states=states):
                self.assertEqual(merge_section_state(states), expect)

    def test_mixed_never_reports_ok(self) -> None:
        """**防误伤配对**：只要有一个子项没采到，就**不许**报 `ok`（否则缺口被藏起来）。"""
        for missing in ("skipped", "unsupported", "not_enabled", "error", "timeout",
                        "permission_denied", "unavailable", "schema_mismatch"):
            with self.subTest(missing=missing):
                self.assertNotEqual(merge_section_state(["ok", missing]), "ok")

    def test_pure_missing_never_reports_partial(self) -> None:
        """**另一个方向**：一项证据都没有时不许报 `partial`（那会让人以为"有一部分能用"）。"""
        for missing in ("skipped", "unsupported", "unavailable", "error"):
            with self.subTest(missing=missing):
                self.assertEqual(merge_section_state([missing, missing]), missing)

    def test_worst_state_picks_severe_first(self) -> None:
        self.assertEqual(worst_state([]), "")
        self.assertEqual(worst_state(["ok", "skipped"]), "skipped")
        self.assertEqual(worst_state(["skipped", "error"]), "error")
        self.assertEqual(worst_state(["ok", "ok"]), "ok")


class ExpandSourceNamesTests(unittest.TestCase):
    """按别名（含 `前缀.*.后缀` 通配）展开成台账里**实际存在**的键。"""

    INDEX = {
        "db.mydb.table_health": {"status": "ok"},
        "db.postgres.table_health": {"status": "empty"},
        "pg.database_ages": {"status": "ok"},
        "db.mydb.unused_indexes": {"status": "empty"},
    }

    def test_literal_and_wildcard(self) -> None:
        self.assertEqual(
            expand_source_names(self.INDEX, ["db.*.table_health", "pg.database_ages"]),
            ["db.mydb.table_health", "db.postgres.table_health", "pg.database_ages"],
        )

    def test_missing_names_are_not_padded(self) -> None:
        """查不到的项**不补空状态** ⇒ 调用方据此落 `unmapped`（不等于"查了 0 行"）。"""
        self.assertEqual(expand_source_names(self.INDEX, ["no.such.item"]), [])
        self.assertEqual(merge_section_state(
            [self.INDEX[k]["status"] for k in expand_source_names(self.INDEX, ["no.such.item"])]
        ), "unmapped")

    def test_deduplicates_across_overlapping_patterns(self) -> None:
        self.assertEqual(
            expand_source_names(self.INDEX, ["db.*.table_health", "db.mydb.table_health"]),
            ["db.mydb.table_health", "db.postgres.table_health"],
        )

    def test_empty_names_yield_nothing(self) -> None:
        self.assertEqual(expand_source_names(self.INDEX, []), [])
        self.assertEqual(expand_source_names(self.INDEX, [""]), [])


if __name__ == "__main__":
    unittest.main()
