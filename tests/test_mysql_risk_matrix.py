from __future__ import annotations

import unittest

from plugins.mysql import MySQLPresentationBuilder


NODES = [
    {"ip": "10.0.0.1", "role_effective": "source"},
    {"ip": "10.0.0.2", "role_effective": "replica"},
    {"ip": "10.0.0.3", "role_effective": "replica"},
]


class RiskMatrixTests(unittest.TestCase):
    """风险分级 × 节点矩阵（《可沉淀清单》F2）。

    「风险压在哪台」原先只能靠通读几十条台账自己数，矩阵是它的汇总视图。
    这里锁住三条容易被后续改动破坏的约定：行序由拓扑层决定、零风险节点照样
    占行、单实例不产出该结构（否则会打穿冻结的单实例报告契约）。
    """

    def test_rows_follow_topology_order_and_mark_source(self) -> None:
        matrix = MySQLPresentationBuilder._risk_matrix(
            findings=[
                {"node": "10.0.0.2", "severity": "high"},
                {"node": "10.0.0.1", "severity": "low"},
                {"node": "10.0.0.1", "severity": "high"},
            ],
            topology={"nodes": NODES},
        )
        self.assertEqual(matrix["severity_columns"], ["high", "low"])
        self.assertEqual(
            [(row["label"], row["total"]) for row in matrix["rows"]],
            [("10.0.0.1（源）", 2), ("10.0.0.2", 1), ("10.0.0.3", 0)],
        )

    def test_row_order_is_independent_of_findings_order(self) -> None:
        # 行序只能由拓扑层定，不得依赖调用者传包的先后（AGENTS.md 的硬规则）。
        forward = MySQLPresentationBuilder._risk_matrix(
            findings=[
                {"node": "10.0.0.1", "severity": "high"},
                {"node": "10.0.0.2", "severity": "medium"},
            ],
            topology={"nodes": NODES},
        )
        backward = MySQLPresentationBuilder._risk_matrix(
            findings=[
                {"node": "10.0.0.2", "severity": "medium"},
                {"node": "10.0.0.1", "severity": "high"},
            ],
            topology={"nodes": NODES},
        )
        self.assertEqual(forward, backward)

    def test_clean_node_still_occupies_a_row(self) -> None:
        # 按 findings 反推行集合会把「一条风险都没有」的节点整行丢掉，
        # 而「哪台干净」本身就是读者要的信息。
        matrix = MySQLPresentationBuilder._risk_matrix(
            findings=[{"node": "10.0.0.1", "severity": "high"}],
            topology={"nodes": NODES},
        )
        self.assertEqual(
            [row["label"] for row in matrix["rows"]],
            ["10.0.0.1（源）", "10.0.0.2", "10.0.0.3"],
        )
        self.assertEqual([row["total"] for row in matrix["rows"]], [1, 0, 0])

    def test_single_instance_has_no_matrix(self) -> None:
        # 单实例的 findings 不带 node（见 _merged_findings）→ 没有第二个节点
        # 可对比，不注入该键。
        self.assertIsNone(
            MySQLPresentationBuilder._risk_matrix(
                findings=[{"severity": "high"}], topology={"nodes": NODES}
            )
        )
        self.assertIsNone(MySQLPresentationBuilder._risk_matrix([], {"nodes": NODES}))

    def test_unregistered_severity_becomes_its_own_column(self) -> None:
        # 规则包将来新增档位时必须成列，否则各列之和小于合计，读者会以为算错了。
        matrix = MySQLPresentationBuilder._risk_matrix(
            findings=[
                {"node": "10.0.0.1", "severity": "critical"},
                {"node": "10.0.0.1", "severity": "high"},
            ],
            topology={"nodes": NODES},
        )
        self.assertEqual(matrix["severity_columns"], ["critical", "high"])
        for row in matrix["rows"]:
            self.assertEqual(row["total"], sum(row["counts"].values()))

    def test_missing_topology_falls_back_to_sorted_order(self) -> None:
        matrix = MySQLPresentationBuilder._risk_matrix(
            findings=[
                {"node": "10.0.0.2", "severity": "high"},
                {"node": "10.0.0.1", "severity": "low"},
            ],
            topology=None,
        )
        # 拓扑不可用时不知道还有哪些节点，只能呈现 findings 里出现过的；排序用
        # IP 字符串序兜底而不是出现顺序 —— 兜底路径同样不得依赖传包先后。
        # 拿不到角色，一律不加（源）后缀。
        self.assertEqual(
            [row["label"] for row in matrix["rows"]], ["10.0.0.1", "10.0.0.2"]
        )


if __name__ == "__main__":
    unittest.main()
