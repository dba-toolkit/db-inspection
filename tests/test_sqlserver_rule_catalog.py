"""SQL Server 规则目录与四态求值的契约测试。

守两件事：

1. **规则包的 rules 目录必须覆盖 rules.py 里所有会产出的 rule_id**。
   漏一条，那条就永远进不了「应当求值」的分母 —— 而分母正是这张表存在的意义
   （没有分母，「通过」永远算不出来，附录四行表里三行结构上恒 0）。

2. **「没采到」不得被算成「通过」**。判定依据只能是 `collection.module_status`，
   不能看快照键是否存在 —— `package_adapter.normalize_snapshot` 会对 30+ 个键
   做 `setdefault(key, [])`，把「这一版没采」和「采到了但结果为空」抹成同一形状。
   这一条是 AGENTS.md「未采集 ≠ 0/正常」的直接守门。
"""
from __future__ import annotations

import json
import re
import unittest
from datetime import datetime
from pathlib import Path

from plugins.sqlserver import rules as rule_module
from plugins.sqlserver.module_catalog import known_module_names

ROOT = Path(__file__).resolve().parents[1]
RULES_PY = ROOT / "plugins" / "sqlserver" / "rules.py"
RULES_JSON = ROOT / "plugins" / "sqlserver" / "inspection_rules.json"

VALID_STATUSES = {"triggered", "passed", "not_evaluated", "not_applicable"}

# rules.py 里用 f-string 拼出来的动态 rule_id（`f"SEC.{name.upper()}"`），
# 取值来自 security_checks 的 check_name 枚举。静态正则抓不到，单独登记。
DYNAMIC_RULE_IDS = {
    "SEC.EMPTY_PASSWORD_LOGINS",
    "SEC.SA_ACCOUNT_DISABLED",
    "SEC.XP_CMDSHELL_ENABLED",
    "SEC.OLE_AUTOMATION_ENABLED",
    "SEC.AUTHENTICATION_MODE",
}


def load_pack() -> dict:
    return json.loads(RULES_JSON.read_text(encoding="utf-8"))


def emitted_rule_ids() -> set[str]:
    """rules.py 里所有 self.add("<rule_id>") 的字面量 + 动态拼名。"""
    text = RULES_PY.read_text(encoding="utf-8")
    return set(re.findall(r'self\.add\(\s*"([A-Z][A-Z0-9_.]+)"', text)) | DYNAMIC_RULE_IDS


class RuleCatalogContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.pack = load_pack()
        self.catalog: dict = self.pack.get("rules") or {}

    def test_catalog_is_present(self) -> None:
        self.assertTrue(self.catalog, "规则包缺少 rules 目录，附录「规则执行状态」将无法算出通过数")

    def test_catalog_covers_every_emitted_rule_id(self) -> None:
        missing = sorted(emitted_rule_ids() - set(self.catalog))
        self.assertEqual(
            missing, [],
            "以下 rule_id 会产出 finding、却不在规则目录里（永远进不了通过的分母）：\n  "
            + "\n  ".join(missing),
        )

    def test_catalog_has_no_orphan_entries(self) -> None:
        orphans = sorted(rid for rid in self.catalog if rid not in emitted_rule_ids())
        self.assertEqual(orphans, [], f"规则目录登记了引擎根本不会产出的 rule_id：{orphans}")

    def test_every_entry_declares_metadata_and_modules(self) -> None:
        for rid, meta in self.catalog.items():
            with self.subTest(rule=rid):
                for key in ("category", "severity", "title", "required_modules"):
                    self.assertIn(key, meta, f"{rid} 缺 {key}")
                self.assertTrue(meta["required_modules"], f"{rid} 的 required_modules 为空")

    def test_required_modules_are_known_to_module_catalog(self) -> None:
        known = known_module_names()
        unknown = sorted({
            module for meta in self.catalog.values() for module in meta["required_modules"]
        } - known)
        self.assertEqual(
            unknown, [],
            "required_modules 里出现 module_catalog 不认识的模块名（改名后要同步目录）："
            f"{unknown}",
        )


class RuleEvaluationStatusTests(unittest.TestCase):
    def setUp(self) -> None:
        self.pack = load_pack()
        self.now = datetime.now().astimezone()

    def _evaluate(self, snapshot: dict) -> list[dict]:
        engine = rule_module.RuleEngine(self.pack, self.now)
        engine.evaluate(snapshot)
        return engine.rule_evaluations()

    def test_statuses_partition_the_catalog(self) -> None:
        rows = self._evaluate({})
        self.assertEqual(len(rows), len(self.pack["rules"]), "求值条数与规则目录条数不一致")
        self.assertTrue(
            {row["status"] for row in rows} <= VALID_STATUSES,
            f"出现未知状态：{ {row['status'] for row in rows} - VALID_STATUSES }",
        )

    def test_triggered_evaluations_cover_all_findings(self) -> None:
        snapshot = {
            "collection": {"module_status": [{"name": "实例配置", "status": "success"}]},
            "configurations": [{"name": "max server memory (mb)", "value_in_use": 65536}],
            "instance": {"physical_memory_mb": 4096, "product_version": "15.0.2000.5"},
        }
        engine = rule_module.RuleEngine(self.pack, self.now)
        findings = engine.evaluate(snapshot)
        triggered = {row["rule_id"] for row in engine.rule_evaluations() if row["status"] == "triggered"}
        finding_rules = {str(item.get("rule_id")) for item in findings}
        self.assertTrue(finding_rules, "合成快照应当触发 INSTANCE.MAX_MEMORY")
        self.assertTrue(
            finding_rules <= triggered,
            f"产出了 finding 却没被标成 triggered：{sorted(finding_rules - triggered)}",
        )

    def test_no_module_status_means_no_passed(self) -> None:
        """一个模块都没登记 → 不允许有任何一条被判「通过」。

        这条测试就是「未采集 ≠ 正常」的守门：如果哪天判定退回成"看快照键存在与否"，
        空快照里 normalize 补出来的空列表会让大批规则变成 passed，这里立刻红。
        """
        rows = self._evaluate({})
        passed = [row["rule_id"] for row in rows if row["status"] == "passed"]
        self.assertEqual(passed, [], f"模块状态为空却判出了「通过」：{passed[:8]}")

    def test_failed_module_is_not_evaluated(self) -> None:
        snapshot = {
            "collection": {"module_status": [{"name": "备份状态", "status": "failed"}]},
            "backups": [],
        }
        by_rule = {row["rule_id"]: row["status"] for row in self._evaluate(snapshot)}
        self.assertEqual(by_rule["BACKUP.FULL"], "not_evaluated")
        self.assertIn("备份状态", next(
            row["reason"] for row in self._evaluate(snapshot) if row["rule_id"] == "BACKUP.FULL"
        ))

    def test_successful_module_with_empty_rows_is_passed(self) -> None:
        """采集成功、结果为空 = 真的检查过、没有可报告的对象 → 通过。"""
        snapshot = {
            "collection": {"module_status": [{"name": "备份状态", "status": "success"}]},
            "backups": [],
        }
        by_rule = {row["rule_id"]: row["status"] for row in self._evaluate(snapshot)}
        self.assertEqual(by_rule["BACKUP.FULL"], "passed")

    def test_legacy_module_name_resolves_to_not_applicable(self) -> None:
        """旧采集包注册的是「复制配置」（现名「复制拓扑」）—— 别名必须查得到。

        查到模块成功 + 没有任何复制记录 → 本实例未配置复制，属「不适用」，
        **不是**「未判断」（模块确实采到了），更不是「通过」。
        """
        snapshot = {
            "collection": {"module_status": [{"name": "复制配置", "status": "success"}]},
            "replication": [],
        }
        by_rule = {row["rule_id"]: row["status"] for row in self._evaluate(snapshot)}
        self.assertEqual(by_rule["HA.REPLICATION"], "not_applicable")

    def test_missing_legacy_module_is_not_evaluated(self) -> None:
        """模块名在 module_status 里完全不存在 = 那一版采集器没有这个模块 → 未判断。"""
        snapshot = {"collection": {"module_status": [{"name": "备份状态", "status": "success"}]}}
        by_rule = {row["rule_id"]: row["status"] for row in self._evaluate(snapshot)}
        self.assertEqual(by_rule["PERF.STALE_STATS"], "not_evaluated")


if __name__ == "__main__":
    unittest.main()
