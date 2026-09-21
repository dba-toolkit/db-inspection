"""二级严重度（告警档 / 严重档）的接线自检。

规则引擎的状态位只有 ``triggered`` / ``passed`` 两种，所以「同一指标，过 80% 是
告警、过 95% 是严重」这件事没法靠状态表达 —— 它只能落在 finding 的 ``severity``
上。`_evaluate` 为此增加了 ``severity_override``：规则包声明基础级别，调用方在
越过更硬的那条阈值时把级别提上去。

这组测试守两件事：

1. **机制本身**：不传 ``severity_override`` 时级别完全来自规则包（默认无副作用）；
   传了就必须同时落到 ``Finding.severity`` 和 ``RuleEvaluation.severity_if_triggered``。
2. **接线是真的**：不是「默认值和旧硬编码恰好相同」，而是**改动配置旋钮，
   结论跟着变**。一个没人读的旋钮和一个真旋钮在默认值下长得一模一样 —— 只有把
   旋钮拧到别的值才分得出来。
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]

ORACLE_PACK = ROOT / "plugins/oracle/inspection_rules_oracle.json"


def _ctx(tables: dict | None = None) -> SimpleNamespace:
    """最小 PackageContext 替身：被测的几条规则只用到 ``ctx.tables``。"""
    return SimpleNamespace(tables=tables or {})


class OracleSeverityOverrideTests(unittest.TestCase):
    """Oracle 的 ``_evaluate`` 机制 + 三处二级阈值/旋钮接线。"""

    def _engine(self, **threshold_overrides):
        from plugins.oracle.oracle_rules import OracleRuleEngine

        engine = OracleRuleEngine(ORACLE_PACK)
        # 只改规则包里某条规则的 threshold，用来证明「旋钮有效」。
        for rule_id, keys in threshold_overrides.items():
            engine._rule_defs[rule_id]["threshold"].update(keys)
        return engine

    def _run(self, method: str, *args) -> tuple[list, dict]:
        engine = self._engine()
        getattr(engine, method)(*args)
        return engine._findings, {e.rule_id: e for e in engine._evaluations}

    def _base_severity(self, rule_id: str) -> str:
        return self._engine()._cfg(rule_id).get("severity", "medium")

    # ── 机制 ───────────────────────────────────────────────────────────
    def test_override_is_noop_when_not_supplied(self) -> None:
        findings, evaluations = self._run(
            "_check_library_cache",
            _ctx({"Library Cache 命中率": [{"NAMESPACE": "SQL AREA", "GETHIT_PCT": "96.0"}]}),
            {},
        )
        self.assertEqual(findings, [])
        self.assertEqual(
            evaluations["ORA.PERFORMANCE.LIBRARY_CACHE"].severity_if_triggered,
            self._base_severity("ORA.PERFORMANCE.LIBRARY_CACHE"),
            "没传 severity_override 时级别必须原样来自规则包",
        )

    def test_override_reaches_both_finding_and_evaluation(self) -> None:
        findings, evaluations = self._run(
            "_check_library_cache",
            _ctx({"Library Cache 命中率": [{"NAMESPACE": "SQL AREA", "GETHIT_PCT": "79.2"}]}),
            {},
        )
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].severity, "critical")
        self.assertEqual(
            evaluations["ORA.PERFORMANCE.LIBRARY_CACHE"].severity_if_triggered,
            "critical",
        )

    # ── 接线 1：Library Cache 命中率（命中率越低越严重，所以严重档数值更小）──
    def test_library_cache_warn_tier_keeps_the_base_severity(self) -> None:
        # 92% 低于告警档 95 但高于严重档 90 —— 只该按基础级别报。
        findings, _ = self._run(
            "_check_library_cache",
            _ctx({"Library Cache 命中率": [{"NAMESPACE": "SQL AREA", "GETHIT_PCT": "92.0"}]}),
            {},
        )
        self.assertEqual(len(findings), 1)
        self.assertEqual(
            findings[0].severity, self._base_severity("ORA.PERFORMANCE.LIBRARY_CACHE")
        )

    def test_library_cache_critical_tier_requires_the_configured_value(self) -> None:
        # 79.2% 在默认档位下够严重。把严重档拧到 70% 之后，同一条数据就不该再升级 ——
        # 这证明代码真的在读 library_cache_hit_critical，而不是写着好看。
        rows = {"Library Cache 命中率": [{"NAMESPACE": "SQL AREA", "GETHIT_PCT": "79.2"}]}
        engine = self._engine(
            **{"ORA.PERFORMANCE.LIBRARY_CACHE": {"library_cache_hit_critical": 70}}
        )
        engine._check_library_cache(_ctx(rows), {})
        self.assertEqual(len(engine._findings), 1)
        self.assertEqual(
            engine._findings[0].severity,
            self._base_severity("ORA.PERFORMANCE.LIBRARY_CACHE"),
        )

    # ── 接线 2：表空间使用率（使用率越高越严重，所以严重档数值更大）──
    def test_tablespace_critical_tier_promotes_the_finding(self) -> None:
        findings, evaluations = self._run(
            "_check_tablespace_usage", {"tablespace_max_capacity_pct": 96.0}
        )
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].severity, "critical")
        self.assertEqual(
            evaluations["ORA.STORAGE.TABLESPACE"].severity_if_triggered, "critical"
        )
        self.assertIn("严重阈值", evaluations["ORA.STORAGE.TABLESPACE"].reason)

    def test_tablespace_warn_tier_does_not_promote(self) -> None:
        findings, evaluations = self._run(
            "_check_tablespace_usage", {"tablespace_max_capacity_pct": 85.0}
        )
        self.assertEqual(findings[0].severity, self._base_severity("ORA.STORAGE.TABLESPACE"))
        self.assertIn("告警阈值", evaluations["ORA.STORAGE.TABLESPACE"].reason)

    # ── 接线 3：redo 成员数下限（原名 redo_member_min，曾是硬编码 < 2）──
    def test_redo_member_threshold_drives_the_count(self) -> None:
        rows = [{"MEMBER_COUNT": "2"}, {"MEMBER_COUNT": "2"}]
        engine = self._engine()
        engine._check_redo_member(_ctx({"Redo 日志多路复用检查": rows}))
        self.assertEqual(engine._findings, [], "成员数 2 >= 下限 2，应通过")

        # 把下限拧到 3：同一批数据必须转为触发。硬编码 < 2 的写法在这条会失败。
        engine = self._engine(**{"ORA.CONFIG.REDO_MEMBER": {"redo_member_min": 3}})
        engine._check_redo_member(_ctx({"Redo 日志多路复用检查": rows}))
        self.assertEqual(len(engine._findings), 1)
        evaluation = engine._evaluations[0]
        self.assertIn("少于 3", evaluation.reason)
        self.assertEqual(engine._findings[0].facts, ["groups_below_min_members=2"])

    def test_redo_member_reason_names_the_threshold(self) -> None:
        findings, evaluations = self._run(
            "_check_redo_member",
            _ctx({"Redo 日志多路复用检查": [{"MEMBER_COUNT": "1"}, {"MEMBER_COUNT": "2"}]}),
        )
        self.assertEqual(len(findings), 1)
        self.assertIn("少于 2", evaluations["ORA.CONFIG.REDO_MEMBER"].reason)


class MysqlSeverityOverrideTests(unittest.TestCase):
    """MySQL 的钩子已就位，且默认不改变任何输出。"""

    def _engine(self):
        from plugins.mysql.rules import RuleEngine

        return RuleEngine()

    def test_default_severity_comes_from_the_pack(self) -> None:
        engine = self._engine()
        rule = "COMMON.SYSTEM.CPU_PRESSURE"
        engine._evaluate(rule, True, True, True, "合成", ["cpu=99"])
        self.assertEqual(engine._findings[0].severity, engine._cfg(rule).get("severity"))
        self.assertEqual(
            engine._evaluations[0].severity_if_triggered, engine._cfg(rule).get("severity")
        )

    def test_override_reaches_both_finding_and_evaluation(self) -> None:
        engine = self._engine()
        rule = "COMMON.SYSTEM.CPU_PRESSURE"
        engine._evaluate(rule, True, True, True, "合成", ["cpu=99"], severity_override="critical")
        self.assertEqual(engine._findings[0].severity, "critical")
        self.assertEqual(engine._evaluations[0].severity_if_triggered, "critical")


class PostgresSeverityHookTests(unittest.TestCase):
    """PG 目前没有启用二级阈值，但钩子必须已经就位。

    PG 的判定是 ``run()`` 里的闭包，没法单独调用；这里退一步只断言签名里有
    ``severity_override`` 且真的赋给了 severity —— 将来谁接线时不需要再改一次
    ``evaluate``。行为层面的「默认无副作用」由全量回归里 PG 输出逐字节不变来证。
    """

    def test_evaluate_closure_accepts_and_applies_the_override(self) -> None:
        source = (ROOT / "plugins/postgresql/rules.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        candidates = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "evaluate"
        ]
        self.assertTrue(candidates, "PG rules.py 里找不到 evaluate 闭包")
        func = max(candidates, key=lambda n: len(n.args.args))
        self.assertIn("severity_override", [a.arg for a in func.args.args])
        body = "\n".join(ast.unparse(stmt) for stmt in func.body)
        self.assertIn("severity = severity_override", body)


if __name__ == "__main__":
    unittest.main()
