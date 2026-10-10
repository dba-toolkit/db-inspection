"""PERF.WAIT_TOP 判据语义守门：等待统计的「线索型」判据不得退化成噪声源。

`wait_stats` 长期是 SQL Server 侧唯一「采了、有定制文案、却没有任何阈值判据」的模块 ——
采集端专门用 `ignored_wait_types` 过滤掉 27 个良性类型，说明本来就打算判，只是下游没接。
补上之后要守住四件事：

1. **良性等待不得触发。** CXPACKET / SOS_SCHEDULER_YIELD / SOS_WORK_DISPATCHER /
   MEMORY_ALLOCATION_EXT 这类在高并发或正常运维下天然很高，对它们报线索等于制造噪声 ——
   实测健康实例里 SOS_WORK_DISPATCHER 累计占比超过 60%。
2. **零头等待不得触发。** 实测健康实例的 LCK_M_X / PAGEIOLATCH_SH 只累计到 3.6s / 2.4s，
   低于默认门槛（60s）时必须安静。
3. **真的命中异常等待时要给出方向**，而不是只说"等待偏高"；且一次只出一条，不刷屏。
4. **门槛是活旋钮**：改规则包里的数字必须真的改变触发结果（与 test_rule_pack_config 同源）。
"""
from __future__ import annotations

import json
import sys
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from plugins.sqlserver.rules import RuleEngine  # noqa: E402

RULES_CONFIG = ROOT / "plugins" / "sqlserver" / "inspection_rules.json"
WAIT_MODULE = "等待统计"
RULE_ID = "PERF.WAIT_TOP"


def _config() -> dict:
    return json.loads(RULES_CONFIG.read_text(encoding="utf-8"))


def _snapshot(waits, module_status: str = "success") -> dict:
    return {
        "collection": {
            "module_status": [{"name": WAIT_MODULE, "status": module_status}],
            "limitations": [],
        },
        "instance": {},
        "databases": [],
        "backups": [],
        "no_backup_databases": [],
        "database_files": [],
        "configurations": [],
        "server_services": [],
        "agent_jobs": [],
        "wait_stats": waits,
        "security": {},
        "tempdb": {"files": []},
        "buffer_pool": [],
    }


def _waits_findings(waits, module_status: str = "success", config: dict | None = None):
    engine = RuleEngine(config if config is not None else _config(),
                        datetime.now().astimezone())
    findings = engine.evaluate(_snapshot(waits, module_status))
    return [f for f in findings if f["rule_id"] == RULE_ID]


def _row(wait_type, wait_time_ms, waiting_tasks_count=1, avg_wait_ms=1.0):
    return {"wait_type": wait_type, "wait_time_ms": wait_time_ms,
            "waiting_tasks_count": waiting_tasks_count, "avg_wait_ms": avg_wait_ms}


class BenignWaitIsSilentTests(unittest.TestCase):
    """白名单外的良性等待累计再高也不得报。"""

    def test_benign_waits_do_not_trigger(self):
        waits = [
            _row("SOS_WORK_DISPATCHER", 402061185, 50246, 8001.0),
            _row("SOS_SCHEDULER_YIELD", 2034, 4113, 0.0),
            _row("MEMORY_ALLOCATION_EXT", 2172, 190207, 0.0),
            _row("CXPACKET", 900000000, 1000, 900.0),
            _row("PREEMPTIVE_OS_QUERYREGISTRY", 2560, 3481, 0.0),
        ]
        self.assertEqual(_waits_findings(waits), [],
                         "良性等待（CXPACKET / SOS_* / PREEMPTIVE_* 等）高是常态，不得报线索")

    def test_health_instance_zero_wait_does_not_trigger(self):
        """真机健康实例的真实形态：候选类型存在，但只有秒级零头。"""
        waits = [
            _row("LCK_M_X", 3585, 2, 1792.0),
            _row("PAGEIOLATCH_SH", 2411, 993, 2.0),
            _row("SOS_WORK_DISPATCHER", 402061185, 50246, 8001.0),
        ]
        self.assertEqual(_waits_findings(waits), [],
                         "健康实例的秒级零头等待不得被判成问题（默认门槛 60s）")


class DominantWaitIsReportedTests(unittest.TestCase):
    """真的命中异常等待时：一条、低风险、带方向。"""

    def test_dominant_attention_wait_triggers_low(self):
        waits = [
            _row("SOS_WORK_DISPATCHER", 400000000, 1, 1.0),   # 良性、量大，不参与
            _row("PAGEIOLATCH_SH", 5000000, 50000, 100.0),    # 候选、最高
            _row("LCK_M_X", 4, 1, 4.0),                       # 候选、低于门槛
        ]
        hits = _waits_findings(waits)
        self.assertEqual(len(hits), 1, "一次只出一条主导线索，不刷屏")
        hit = hits[0]
        self.assertEqual(hit["severity"], "low", "线索型判据不硬定风险级")
        self.assertEqual(hit["object_name"], "PAGEIOLATCH_SH")
        self.assertIn("PAGEIOLATCH_SH", hit["evidence"])
        self.assertIn("存储", hit["recommendation"])

    def test_secondary_candidates_merge_into_evidence(self):
        waits = [
            _row("PAGEIOLATCH_SH", 5000000, 1, 1.0),
            _row("THREADPOOL", 3000000, 1, 1.0),
        ]
        hits = _waits_findings(waits)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["object_name"], "PAGEIOLATCH_SH")
        self.assertIn("THREADPOOL", hits[0]["evidence"], "次要候选进 evidence，不另开一条")

    def test_hint_is_type_specific(self):
        page = _waits_findings([_row("PAGEIOLATCH_EX", 9999999)])[0]
        self.assertIn("索引", page["recommendation"])
        thread = _waits_findings([_row("THREADPOOL", 9999999)])[0]
        self.assertIn("线程", thread["impact"])

    def test_title_does_not_duplicate_object_name(self):
        """标题不带类型名，交给呈现层拼 «标题（对象）» —— 否则会拼成两遍类型名。"""
        hit = _waits_findings([_row("RESOURCE_SEMAPHORE", 9999999)])[0]
        self.assertNotIn("RESOURCE_SEMAPHORE", hit["title"])
        self.assertEqual(hit["title"], "存在主导异常等待类型")


class ThresholdIsALiveKnobTests(unittest.TestCase):
    def test_threshold_floor_can_be_tuned(self):
        cfg = _config()
        cfg["thresholds"]["wait_attention_min_ms"] = 10000000
        hits = _waits_findings([_row("PAGEIOLATCH_SH", 5000000)], config=cfg)
        self.assertEqual(hits, [], "门槛是可调旋钮：调高后同一输入必须安静")

    def test_not_collected_is_not_passed(self):
        engine = RuleEngine(_config(), datetime.now().astimezone())
        engine.evaluate(_snapshot([], module_status="failed"))
        evals = {e["rule_id"]: e for e in engine.rule_evaluations()}
        self.assertEqual(evals[RULE_ID]["status"], "not_evaluated",
                         "模块没采到要登记未判断，不能算通过")


class RulePackEntryTests(unittest.TestCase):
    def test_rule_is_registered_in_catalog(self):
        pack = _config()
        meta = (pack.get("rules") or {}).get(RULE_ID)
        self.assertIsNotNone(meta, "新规则必须进规则包目录（否则进不了「应当求值」的分母）")
        self.assertEqual(meta["required_modules"], [WAIT_MODULE])
        self.assertEqual(meta["severity"], "low")

    def test_threshold_key_is_declared(self):
        self.assertIn("wait_attention_min_ms", _config().get("thresholds") or {})


if __name__ == "__main__":
    unittest.main()
