from __future__ import annotations

import unittest

from plugins.mysql import MySQLPresentationBuilder

DETAIL_TITLES = ["存在远程 root 高权限账户", "存在无主键业务表", "日志文件体积超阈值"]

INSTANCES = [
    {
        "instance_id": "i1",
        "identity": {"ip": "10.0.0.1", "hostname": "db01", "role_observed": "source"},
        "health_summary": {"score": 42, "counts": {"high": 1, "medium": 1, "low": 1}},
        "findings": [
            {"severity": "high", "title": DETAIL_TITLES[0], "facts": ["账户来源：%"]},
            {"severity": "medium", "title": DETAIL_TITLES[1], "facts": ["无主键表：94"]},
            {"severity": "low", "title": DETAIL_TITLES[2], "facts": ["最大日志文件：error 16 GB"]},
        ],
        "comprehensive_conclusions": [
            {"topic": "参数配置合规", "status": "attention", "conclusion": "已检查 4 组核心参数。"}
        ],
    },
    {
        "instance_id": "i2",
        "identity": {"ip": "10.0.0.2", "hostname": "db02", "role_observed": "replica"},
        "health_summary": {"score": 60, "counts": {"high": 0, "medium": 1, "low": 0}},
        "findings": [
            {"severity": "medium", "title": DETAIL_TITLES[1], "facts": ["无主键表：12"]},
        ],
    },
]

ANALYSIS = {
    "instances": INSTANCES,
    "topology": {
        "nodes": [
            {"node_id": "i1", "role_effective": "source"},
            {"node_id": "i2", "role_effective": "replica"},
        ]
    },
}


class SummaryDigestTests(unittest.TestCase):
    """综合结论摘要位只放「结论」，不放「明细」。

    背景：节点风险清单曾把每个节点的全部风险标题与事实拼进 conclusion，
    三节点实测最长一条 1988 字，整坨挤在摘要表格的一个单元格里，而且与第 12
    章风险登记册逐条重复。这里锁住四条约定：明细条目要打 scope、明细条目必须
    压成一句话、明细标题不得出现在任何结论条目里、单实例不受影响（冻结契约）。
    """

    def setUp(self) -> None:
        self.builder = MySQLPresentationBuilder()

    def _rows(self) -> list[dict]:
        return self.builder._node_attributed_conclusions(ANALYSIS, INSTANCES[0])

    def test_every_entry_carries_a_scope(self) -> None:
        rows = self._rows()
        self.assertTrue(rows)
        for row in rows:
            self.assertIn(row.get("scope"), {"cluster", "node_detail"})

    def test_node_detail_is_a_one_liner(self) -> None:
        rows = [r for r in self._rows() if r.get("scope") == "node_detail"]
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertLess(len(row["conclusion"]), 200)
            # 计数不能因为压缩而丢失，且要指明明细去哪看。
            self.assertIn("健康分", row["conclusion"])
            self.assertIn("风险登记册", row["conclusion"])

    def test_detail_titles_never_leak_into_conclusions(self) -> None:
        # 这条是防"重新堆起来"的闸门：只要有改动又把逐条风险抄回结论，这里就红。
        for row in self._rows():
            for title in DETAIL_TITLES:
                self.assertNotIn(title, row["conclusion"])

    def test_cluster_summary_keeps_per_node_counts(self) -> None:
        rows = [r for r in self._rows() if r.get("topic") == "节点风险分布"]
        self.assertEqual(len(rows), 1)
        conclusion = rows[0]["conclusion"]
        # 两个节点都要出现，且各自的高/中/低计数保留 —— 压缩不能压掉信息。
        self.assertIn("10.0.0.1", conclusion)
        self.assertIn("10.0.0.2", conclusion)
        self.assertIn("高 1 / 中 1 / 低 1", conclusion)

    def test_single_instance_is_untouched(self) -> None:
        # 单实例直接返回分析层的原始条目，不注入 scope/node —— 否则会打穿
        # 冻结的单实例报告契约（report_model 逐字典等值比对）。
        single = [INSTANCES[0]]
        rows = self.builder._node_attributed_conclusions(
            {"instances": single, "topology": {"nodes": [{"node_id": "i1"}]}}, INSTANCES[0]
        )
        self.assertEqual(rows, [dict(item) for item in INSTANCES[0]["comprehensive_conclusions"]])
        for row in rows:
            self.assertNotIn("scope", row)


if __name__ == "__main__":
    unittest.main()
