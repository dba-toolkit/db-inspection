"""拓扑合并（主从 / 多实例）回归测试。

守两处实测踩到的缺陷：

① **自环边**：节点的复制状态行指向自己时（`source_uuid` 是本机、或 `source_host` 就是本机 IP），
   原先会被画成 `A -> A` 的拓扑边 —— 客户报告里直接出现"源节点 = 目标节点"。

② **源端被误标成 replica**：采集端的角色判据是「replica_status 表有数据行 → replica」，
   不看复制线程是否在跑。源端那台常残留一行指向自己的 SHOW REPLICA STATUS（IO/SQL 都是 No），
   于是被判 replica；多包合并时明明有别的节点声明以它为上游，报告却仍写 replica。

对照的真实样本（3 套 MySQL 采集包）曾同时产出这两个错：
三行节点全标 `replica`，关系表多出一行 `e21a39ca -> e21a39ca`。
"""

from __future__ import annotations

import unittest
from pathlib import Path

from inspection_core.models import PackageContext
from plugins.mysql.analyzer import Analyzer


def node_context(
    *,
    uuid: str,
    tag: str,
    ip: str,
    hostname: str,
    role: str,
    source_uuid: str = "",
    source_host: str = "",
    version: str = "8.0.30",
) -> PackageContext:
    """构造只带身份与角色证据的最小上下文（拓扑构建只读这两块）。"""
    return PackageContext(
        source=Path(f"{tag}.tar.gz"),
        root=Path("_syn_") / tag,
        snapshot={
            "instance_identity": {
                "instance_tag": tag,
                "server_uuid": uuid,
                "instance_ip": ip,
                "mysql_hostname": hostname,
                "port": 3306,
                "server_id": tag,
                "version": version,
            },
            "role_evidence": {
                "role_observed": role,
                "source_uuid": source_uuid,
                "source_host": source_host,
            },
        },
        status={"items": []},
        manifest={},
        integrity={"status": "ok"},
    )


class TopologySelfReferenceTests(unittest.TestCase):
    """自环：上游声明指向自身时必须丢弃，且不能混进 unresolved。"""

    def test_source_uuid_pointing_to_self_is_dropped(self) -> None:
        ctx = node_context(
            uuid="aaaa", tag="db1", ip="192.168.1.10", hostname="db1",
            role="replica", source_uuid="aaaa", source_host="192.168.1.10",
        )
        topology = Analyzer.topology([ctx])

        self.assertEqual([], topology["edges"])
        self.assertEqual(1, len(topology["self_reference_edges"]))
        self.assertEqual([], topology["unresolved_edges"], "自指不等于查不到上游")

    def test_source_host_pointing_to_self_ip_is_dropped(self) -> None:
        ctx = node_context(
            uuid="aaaa", tag="db1", ip="192.168.1.10", hostname="db1",
            role="replica", source_uuid="", source_host="192.168.1.10",
        )
        topology = Analyzer.topology([ctx])

        self.assertEqual([], topology["edges"])
        self.assertEqual(1, len(topology["self_reference_edges"]))
        self.assertEqual([], topology["unresolved_edges"])

    def test_source_host_matching_a_peer_is_still_a_real_edge(self) -> None:
        source = node_context(
            uuid="aaaa", tag="db1", ip="192.168.1.10", hostname="db1",
            role="standalone_or_source",
        )
        replica = node_context(
            uuid="bbbb", tag="db2", ip="192.168.1.11", hostname="db2",
            role="replica", source_uuid="", source_host="192.168.1.10",
        )
        topology = Analyzer.topology([source, replica])

        self.assertEqual(1, len(topology["edges"]))
        self.assertEqual("aaaa", topology["edges"][0]["source_node_id"])
        self.assertEqual("bbbb", topology["edges"][0]["target_node_id"])
        self.assertEqual([], topology["self_reference_edges"])


class TopologyRoleCorrectionTests(unittest.TestCase):
    """角色校正：上游声明自指、但被下游声明为上游的节点，按复制源端处理。"""

    def _master_with_two_replicas(self) -> list[PackageContext]:
        # 源端：自身残留一行指向自己的复制状态行，采集端因此判成 replica
        master = node_context(
            uuid="mmmm", tag="db2", ip="192.168.1.33", hostname="db2",
            role="replica", source_uuid="", source_host="192.168.1.33",
        )
        replica_a = node_context(
            uuid="aaaa", tag="db1", ip="192.168.1.34", hostname="db1",
            role="replica", source_uuid="mmmm", source_host="192.168.1.33",
        )
        replica_b = node_context(
            uuid="bbbb", tag="db3", ip="192.168.1.125", hostname="db3",
            role="replica", source_uuid="mmmm", source_host="192.168.1.33",
        )
        return [master, replica_a, replica_b]

    def test_upstream_node_is_reported_as_source(self) -> None:
        topology = Analyzer.topology(self._master_with_two_replicas())
        nodes = {node["node_id"]: node for node in topology["nodes"]}

        self.assertEqual("source", nodes["mmmm"]["role_effective"])
        self.assertEqual(2, nodes["mmmm"]["upstream_node_count"])
        self.assertIn("role_note", nodes["mmmm"])
        self.assertIn("replica", nodes["mmmm"]["role_note"])
        self.assertEqual("replica", nodes["mmmm"]["role_observed"], "原始观测值必须留档，不得改写")

    def test_downstream_replicas_keep_observed_role(self) -> None:
        topology = Analyzer.topology(self._master_with_two_replicas())
        nodes = {node["node_id"]: node for node in topology["nodes"]}

        for node_id in ("aaaa", "bbbb"):
            self.assertEqual("replica", nodes[node_id]["role_effective"])
            self.assertEqual(0, nodes[node_id]["upstream_node_count"])
            self.assertNotIn("role_note", nodes[node_id])

    def test_two_real_edges_and_one_dropped_self_reference(self) -> None:
        topology = Analyzer.topology(self._master_with_two_replicas())

        self.assertEqual("multi_instance", topology["mode"])
        self.assertEqual("complete", topology["completeness"])
        self.assertEqual(2, len(topology["edges"]))
        self.assertEqual({"mmmm"}, {edge["source_node_id"] for edge in topology["edges"]})
        self.assertEqual(1, len(topology["self_reference_edges"]))

    def test_cascading_replica_is_not_misreported_as_source(self) -> None:
        """级联复制里，中间节点被下游指向、但它自己有真实上游 —— 不能被改写成 source。"""
        source = node_context(
            uuid="ssss", tag="db1", ip="192.168.1.50", hostname="db1",
            role="standalone_or_source",
        )
        middle = node_context(
            uuid="mmmm", tag="db2", ip="192.168.1.51", hostname="db2",
            role="replica", source_uuid="ssss", source_host="192.168.1.50",
        )
        leaf = node_context(
            uuid="llll", tag="db3", ip="192.168.1.52", hostname="db3",
            role="replica", source_uuid="mmmm", source_host="192.168.1.51",
        )
        topology = Analyzer.topology([source, middle, leaf])
        nodes = {node["node_id"]: node for node in topology["nodes"]}

        self.assertEqual(1, nodes["mmmm"]["upstream_node_count"])
        self.assertEqual("replica", nodes["mmmm"]["role_effective"], "有真实上游的节点不得改判")
        self.assertNotIn("role_note", nodes["mmmm"])
        self.assertEqual(2, len(topology["edges"]))


class TopologySinglePackageTests(unittest.TestCase):
    """单包分析不得被改动影响：角色保持观测值，不产生任何边。"""

    def test_single_replica_with_unresolvable_source(self) -> None:
        ctx = node_context(
            uuid="aaaa", tag="db1", ip="192.168.1.10", hostname="db1",
            role="replica", source_uuid="zzzz", source_host="192.168.1.99",
        )
        topology = Analyzer.topology([ctx])
        node = topology["nodes"][0]

        self.assertEqual("single_instance", topology["mode"])
        self.assertEqual([], topology["edges"])
        self.assertEqual("replica", node["role_observed"])
        self.assertEqual("replica", node["role_effective"], "单包无下游证据，不得改判")
        self.assertEqual(0, node["upstream_node_count"])
        self.assertEqual(1, len(topology["unresolved_edges"]), "上游不在本次输入里，属未解析")

    def test_standalone_source_without_any_upstream(self) -> None:
        ctx = node_context(
            uuid="aaaa", tag="db1", ip="192.168.1.10", hostname="db1",
            role="standalone_or_source",
        )
        topology = Analyzer.topology([ctx])
        node = topology["nodes"][0]

        self.assertEqual("standalone_or_source", node["role_effective"])
        self.assertEqual([], topology["unresolved_edges"])
        self.assertEqual("complete", topology["completeness"])


class TopologyRenderingFieldTests(unittest.TestCase):
    """节点必须自带渲染需要的字段：数据库版本，以及"源端排第一"的稳定顺序。

    报告 2.2 的节点表固定四列 `节点 | 地址 | 角色 | 版本`，直接按 ``nodes`` 顺序出。
    因此版本必须挂在节点上（缺了渲染成"未采集"），顺序也必须由拓扑层定下来
    （否则同样的三台机器换个传包顺序，报告就换了个样子）。
    """

    def test_node_carries_database_version(self) -> None:
        ctx = node_context(
            uuid="aaaa", tag="db1", ip="192.168.1.10", hostname="db1",
            role="replica", version="8.0.30",
        )
        node = Analyzer.topology([ctx])["nodes"][0]

        self.assertEqual(
            "8.0.30", node["version"],
            "2.2 节点表的版本列读 node['version']，节点不带这个键就会显示'未采集'",
        )

    def test_source_node_renders_first_regardless_of_input_order(self) -> None:
        master = node_context(
            uuid="mmmm", tag="db2", ip="192.168.1.33", hostname="db2",
            role="replica", source_uuid="", source_host="192.168.1.33",
        )
        replica_high = node_context(
            uuid="bbbb", tag="db3", ip="192.168.1.125", hostname="db3",
            role="replica", source_uuid="mmmm", source_host="192.168.1.33",
        )
        replica_low = node_context(
            uuid="aaaa", tag="db1", ip="192.168.1.34", hostname="db1",
            role="replica", source_uuid="mmmm", source_host="192.168.1.33",
        )
        # 故意把源端放在输入的最后一位：报告顺序不得依赖调用者传包的先后
        order = [node["ip"] for node in Analyzer.topology([replica_high, replica_low, master])["nodes"]]

        self.assertEqual("192.168.1.33", order[0], "复制源端必须出现在节点表第一行")
        self.assertEqual(
            ["192.168.1.34", "192.168.1.125"], order[1:],
            "其余节点按 IP 数值序排列（字符串序会把 .125 排到 .34 前面）",
        )

    def test_single_package_node_order_is_unchanged(self) -> None:
        ctx = node_context(
            uuid="aaaa", tag="db1", ip="192.168.1.10", hostname="db1",
            role="standalone_or_source",
        )
        topology = Analyzer.topology([ctx])

        self.assertEqual(1, len(topology["nodes"]))
        self.assertEqual("192.168.1.10", topology["nodes"][0]["ip"])


if __name__ == "__main__":
    unittest.main()
