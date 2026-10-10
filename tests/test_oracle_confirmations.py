"""「待客户确认事项」（清单 G1）的守门测试。

本键由**已触发的规则**派生（模板见 ``plugins/oracle/presentation.CONFIRMATION_TEMPLATES``），
是新增字段 —— 旧基线 ``report_model.json`` 里并不存在，因此报告层必须**条件注入**，
否则会打穿冻结契约。

这里钉住四件事，缺一条就会出现「报告里某一章静默空转」这类看不见的退步：
  1. 模板里的 rule_id **必须真实存在于规则集** —— 写了个不存在的 rule_id，模板永不触发，
     报告永远显示「本次没有待确认事项」，而没有任何测试会红；
  2. 派生只认已触发的 finding，且同一 rule_id 去重（一条规则只问一件事）；
  3. ``None``（未产出清单）与 ``[]``（本次没有）必须可区分 —— 延续「未采集 ≠ 0」；
  4. 章节顺序（读者动线：实例 → 配置 → 运行 → 容量）—— 集合断言看不出顺序，必须比下标。
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from plugins.oracle.presentation import (
    CONFIRMATION_TEMPLATES,
    _merged_confirmations,
    pending_confirmations,
)
from plugins.oracle.rule_provider import OracleRuleProvider
from plugins.oracle.word_report import ORACLE_WORD_PROFILE


ROOT = Path(__file__).resolve().parents[1]


class ConfirmationTemplateTests(unittest.TestCase):
    def test_every_rule_id_exists_in_the_rule_set(self) -> None:
        """★ 防漂移：模板引用的 rule_id 必须真实存在，否则该条永不触发。"""
        config = Path(OracleRuleProvider().config_path)
        rules = json.loads(config.read_text(encoding="utf-8"))["rules"]
        known = set(rules)
        unknown = sorted(set(CONFIRMATION_TEMPLATES) - known)
        self.assertEqual(unknown, [], f"模板引用了规则集里不存在的 rule_id：{unknown}")

    def test_templates_are_answerable_by_the_customer(self) -> None:
        """模板只收「须客户拍板 / 须客户提供外部证据」的事项，不是第二个风险台账。"""
        for rule_id, (topic, question) in CONFIRMATION_TEMPLATES.items():
            with self.subTest(rule_id=rule_id):
                self.assertTrue(topic.strip(), "主题不能为空")
                self.assertGreater(len(question.strip()), 40, "待确认内容写得太短，等于没问")
                # 技术性整改项不进表：这些词出现在模板里说明口径抄错了。
                for forbidden in ("重新收集统计信息", "重建索引", "调整为 512M"):
                    self.assertNotIn(forbidden, question)


class PendingConfirmationDerivationTests(unittest.TestCase):
    def test_only_triggered_templated_rules_are_collected(self) -> None:
        instance = {
            "findings": [
                {"rule_id": "ORA.ARCHIVE.MODE", "finding_id": "f1", "facts": ["LOG_MODE=NOARCHIVELOG"]},
                # 不在模板里的规则（纯技术整改项）不进表
                {"rule_id": "ORA.CONFIG.REDO_SIZE", "finding_id": "f2", "facts": ["members=1"]},
                # 同一规则重复触发只出一条
                {"rule_id": "ORA.ARCHIVE.MODE", "finding_id": "f3", "facts": ["dup"]},
            ]
        }
        items = pending_confirmations(instance)
        self.assertEqual([item["rule_id"] for item in items], ["ORA.ARCHIVE.MODE"])
        self.assertEqual(items[0]["topic"], CONFIRMATION_TEMPLATES["ORA.ARCHIVE.MODE"][0])
        self.assertEqual(items[0]["evidence"], ["LOG_MODE=NOARCHIVELOG"])
        self.assertEqual(items[0]["finding_id"], "f1")

    def test_no_findings_yields_empty_list_not_none(self) -> None:
        """实例有这个键、但本次没有要问的事 ⇒ ``[]``（报告写「本次没有」）。"""
        self.assertEqual(pending_confirmations({"findings": []}), [])

    def test_node_defaults_to_blank_for_single_instance(self) -> None:
        """单实例报告不渲染节点列；留空是刻意的（引擎按 has_node 决定列数）。"""
        items = pending_confirmations(
            {"findings": [{"rule_id": "ORA.ARCHIVE.MODE", "finding_id": "f1", "facts": []}]}
        )
        self.assertEqual(items[0]["node"], "")


class MergedConfirmationTests(unittest.TestCase):
    def test_missing_key_returns_none(self) -> None:
        """旧分析产物没有该字段 ⇒ ``None`` ⇒ 报告只能声明「未产出清单」。"""
        self.assertIsNone(_merged_confirmations([{"findings": []}]))

    def test_present_but_empty_returns_empty_list(self) -> None:
        self.assertEqual(_merged_confirmations([{"pending_confirmations": []}]), [])

    def test_merges_every_instance_and_sorts_stably(self) -> None:
        merged = _merged_confirmations([
            {"pending_confirmations": [
                {"topic": "备份归属与可恢复性", "node": "192.168.1.12"},
            ]},
            {"pending_confirmations": [
                {"topic": "归档模式的启用窗口与备份策略", "node": "192.168.1.11"},
                {"topic": "备份归属与可恢复性", "node": "192.168.1.11"},
            ]},
        ])
        # 逐行保留（不按主题归并：不同节点的证据不一样），按 (topic, node) 稳定排序。
        self.assertEqual(
            [(item["topic"], item["node"]) for item in merged],
            [
                ("备份归属与可恢复性", "192.168.1.11"),
                ("备份归属与可恢复性", "192.168.1.12"),
                ("归档模式的启用窗口与备份策略", "192.168.1.11"),
            ],
        )


class SectionOrderTests(unittest.TestCase):
    def test_instance_section_precedes_storage_section(self) -> None:
        """★ 读者动线：先知道「实例是什么」，再看「它装了什么」。

        集合断言（``set(section_order) == section_ids``）对顺序完全不敏感，
        所以这类改动必须单独比下标 —— 否则顺序被改回去也没有任何测试会红。
        """
        order = ORACLE_WORD_PROFILE.section_order
        self.assertLess(
            order.index("oracle_instance"),
            order.index("storage_capacity"),
            "存储与容量检查排在了 Oracle 实例检查之前",
        )
        # 环境/性能仍在前，备份恢复仍在尾 —— 只换了两者相对位置。
        self.assertEqual(order[0], "system_environment")
        self.assertEqual(order[-1], "backup_recovery")


if __name__ == "__main__":
    unittest.main()
