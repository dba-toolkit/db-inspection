"""SQL Server 判据语义守门：三条「报告会说反话」的缺陷不得回归。

为什么单独一个文件：这三条缺陷**都不抛异常、不打红测试** ——
采集包照常分析、Word 照常生成、章节照常渲染，只是结论与事实相反。
它们是在真机包上逐条回溯 findings 才发现的，所以必须用构造快照把它们钉死在代码层，
而不能再依赖「跑一遍真包看看」这种人工核对。

覆盖：
1. `INSTANCE.SERVICE_STARTUP` 必须区分引擎服务与依赖服务 ——
   SQL Server 代理停止（微软出厂默认就是 Manual）不等于实例不可用。
2. `BACKUP.FULL` 与 `BACKUP.NO_BACKUP` 不得对同一个库各报一次 critical。
3. `system_database_guard`：系统库的「建议项」降级，但**只降 applies_to 列出的规则**，
   而且业务库一律不受影响。
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

ENGINE_SVC = {
    "servicename": "SQL Server (MSSQLSERVER)",
    "service_account": "NT Service\\MSSQLSERVER",
    "startup_type_desc": "Automatic",
    "status_desc": "Running",
    "filename": r'"C:\Program Files\Microsoft SQL Server\MSSQL15.MSSQLSERVER\MSSQL\Binn\sqlservr.exe" -sMSSQLSERVER',
}
AGENT_SVC = {
    "servicename": "SQL Server 代理 (MSSQLSERVER)",
    "service_account": "NT Service\\SQLSERVERAGENT",
    "startup_type_desc": "Manual",
    "status_desc": "Stopped",
    "filename": r'"C:\Program Files\Microsoft SQL Server\MSSQL15.MSSQLSERVER\MSSQL\Binn\SQLAGENT.EXE" -i MSSQLSERVER',
}
FULLTEXT_SVC = {
    "servicename": "SQL Full-text Filter Daemon Launcher (MSSQLSERVER)",
    "service_account": "NT Service\\MSSQLFDLauncher",
    "startup_type_desc": "Manual",
    "status_desc": "Running",
    "filename": r'"C:\...\fdlauncher.exe" -s MSSQL15.MSSQLSERVER',
}


def _config() -> dict:
    return json.loads(RULES_CONFIG.read_text(encoding="utf-8"))


def _engine(config: dict | None = None) -> RuleEngine:
    return RuleEngine(config if config is not None else _config(),
                      datetime.now().astimezone())


def _snapshot(**overrides):
    """足够跑完整个规则引擎的最小快照；其余键留空，不触发任何规则。"""
    snap = {
        "collection": {"module_status": [], "limitations": []},
        "instance": {},
        "databases": [],
        "backups": [],
        "no_backup_databases": [],
        "database_files": [],
        "configurations": [],
        "server_services": [],
        "agent_jobs": [],
        "wait_stats": [],
        "security": {},
        "tempdb": {"files": []},
        "buffer_pool": [],
    }
    snap.update(overrides)
    return snap


def _by_rule(findings, rule_id):
    return [f for f in findings if f["rule_id"] == rule_id]


class DependentServiceNotInstanceDownTests(unittest.TestCase):
    """引擎服务与依赖服务必须分流。"""

    def test_agent_stopped_is_not_reported_as_instance_down(self):
        findings = _engine().evaluate(_snapshot(
            server_services=[ENGINE_SVC, AGENT_SVC],
            agent_jobs=[{"job_name": "nightly_backup"}],
        ))
        downs = [f for f in _by_rule(findings, "INSTANCE.SERVICE_STARTUP")
                 if "未处于运行状态" in f["title"]]
        self.assertEqual(downs, [], "引擎在运行，代理停止不得报成「实例不可用」")

    def test_engine_stopped_is_still_critical(self):
        stopped = dict(ENGINE_SVC, status_desc="Stopped")
        findings = _engine().evaluate(_snapshot(server_services=[stopped]))
        downs = [f for f in _by_rule(findings, "INSTANCE.SERVICE_STARTUP")
                 if "未处于运行状态" in f["title"]]
        self.assertEqual(len(downs), 1, "引擎停止必须报")
        self.assertEqual(downs[0]["severity"], "critical")

    def test_agent_stopped_without_jobs_is_silent(self):
        findings = _engine().evaluate(_snapshot(
            server_services=[ENGINE_SVC, AGENT_SVC], agent_jobs=[]))
        self.assertEqual(_by_rule(findings, "INSTANCE.SERVICE_STARTUP"), [],
                         "实例没有作业时代理停止属于未启用该功能，不是故障")

    def test_agent_stopped_with_jobs_is_medium_not_critical(self):
        findings = _engine().evaluate(_snapshot(
            server_services=[ENGINE_SVC, AGENT_SVC],
            agent_jobs=[{"job_name": "nightly_backup"}],
        ))
        hits = _by_rule(findings, "INSTANCE.SERVICE_STARTUP")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["severity"], "medium")
        self.assertIn("代理", hits[0]["title"])

    def test_fulltext_manual_startup_is_not_flagged(self):
        """Manual 是全文服务的标准启动类型，不能当问题报。"""
        findings = _engine().evaluate(_snapshot(server_services=[ENGINE_SVC, FULLTEXT_SVC]))
        self.assertEqual(_by_rule(findings, "INSTANCE.SERVICE_STARTUP"), [])

    def test_engine_startup_type_not_automatic_is_medium(self):
        manual = dict(ENGINE_SVC, startup_type_desc="Manual")
        findings = _engine().evaluate(_snapshot(server_services=[manual]))
        hits = _by_rule(findings, "INSTANCE.SERVICE_STARTUP")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["severity"], "medium")
        self.assertIn("启动类型", hits[0]["title"])

    def test_engine_detected_by_binary_when_filename_present(self):
        """中文实例名 + 英文实例名 + 依赖服务混排时，仍只认 sqlservr.exe。"""
        eng = _engine()
        self.assertTrue(eng._is_engine_service(ENGINE_SVC))
        self.assertFalse(eng._is_engine_service(AGENT_SVC))
        self.assertFalse(eng._is_engine_service(FULLTEXT_SVC))
        self.assertFalse(eng._is_engine_service(
            dict(AGENT_SVC, servicename="SQL Server Agent (INST1)")))

    def test_engine_detected_by_name_when_filename_missing(self):
        """filename 未采到时按服务名回退判断。"""
        eng = _engine()
        self.assertTrue(eng._is_engine_service(
            {"servicename": "SQL Server (MSSQLSERVER)", "filename": None}))
        self.assertFalse(eng._is_engine_service(
            {"servicename": "SQL Server 代理 (MSSQLSERVER)", "filename": None}))
        self.assertFalse(eng._is_engine_service(
            {"servicename": "SQL Server Browser", "filename": None}))


class BackupNoDoubleCountTests(unittest.TestCase):
    """「从未备份」只能由 BACKUP.NO_BACKUP 出口。"""

    def test_never_backed_up_reports_once(self):
        findings = _engine().evaluate(_snapshot(
            backups=[{"database_name": "app", "recovery_model_desc": "SIMPLE",
                      "last_full_backup": None}],
            no_backup_databases=[{"database_name": "app", "recovery_model_desc": "SIMPLE",
                                  "create_date": "2026-03-30T16:23:40.8470000"}],
        ))
        crit = [f for f in findings if f["object_name"] == "app" and f["severity"] == "critical"]
        backup_crit = [f for f in crit if f["rule_id"].startswith("BACKUP.")]
        self.assertEqual(len(backup_crit), 1, "同一库的「从未备份」只能报一次")
        self.assertEqual(backup_crit[0]["rule_id"], "BACKUP.NO_BACKUP")

    def test_overdue_backup_still_reported_by_backup_full(self):
        """有记录但超期，仍归 BACKUP.FULL。"""
        findings = _engine().evaluate(_snapshot(
            backups=[{"database_name": "app", "recovery_model_desc": "SIMPLE",
                      "last_full_backup": "2000-01-01T00:00:00"}],
            no_backup_databases=[],
        ))
        hits = [f for f in _by_rule(findings, "BACKUP.FULL") if f["object_name"] == "app"]
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["severity"], "critical")

    def test_backup_full_still_falls_back_when_no_backup_module_missing(self):
        """no_backup_databases 清单没覆盖该库时，BACKUP.FULL 必须兜底，不能漏报。"""
        findings = _engine().evaluate(_snapshot(
            backups=[{"database_name": "app", "recovery_model_desc": "SIMPLE",
                      "last_full_backup": None}],
            no_backup_databases=[],  # 清单里没有 app
        ))
        hits = [f for f in _by_rule(findings, "BACKUP.FULL") if f["object_name"] == "app"]
        self.assertEqual(len(hits), 1, "清单未覆盖时必须兜底报，避免用去重换来漏报")

    def test_no_backup_registers_gap_when_module_not_collected(self):
        """模块没采到要登记未判断，而不是静默当作「没有库从未备份」。"""
        eng = _engine()
        eng.evaluate(_snapshot(no_backup_databases=[]))
        evals = {e["rule_id"]: e for e in eng.rule_evaluations()}
        self.assertEqual(evals["BACKUP.NO_BACKUP"]["status"], "not_evaluated")


class SystemDatabaseGuardTests(unittest.TestCase):
    """系统库降级：范围可控、只降指定规则、业务库不受影响。"""

    def test_system_database_severity_is_capped(self):
        findings = _engine().evaluate(_snapshot(
            no_backup_databases=[{"database_name": "msdb", "recovery_model_desc": "SIMPLE",
                                  "create_date": "2019-09-24T14:21:42.2700000"}],
        ))
        hits = [f for f in _by_rule(findings, "BACKUP.NO_BACKUP") if f["object_name"] == "msdb"]
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["severity"], "low")

    def test_user_database_keeps_critical(self):
        findings = _engine().evaluate(_snapshot(
            no_backup_databases=[{"database_name": "huangjinjing", "recovery_model_desc": "FULL",
                                  "create_date": "2026-03-30T16:23:40.8470000"}],
        ))
        hits = [f for f in _by_rule(findings, "BACKUP.NO_BACKUP")
                if f["object_name"] == "huangjinjing"]
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["severity"], "critical")

    def test_guard_scope_is_limited_to_listed_rules(self):
        """未列入 applies_to 的规则不得降级 —— MEM.CACHE_IMBALANCE 的 msdb 是真实问题。"""
        eng = _engine()
        self.assertEqual(
            eng._apply_system_db_guard("MEM.CACHE_IMBALANCE", "msdb", "medium"), "medium")
        self.assertEqual(
            eng._apply_system_db_guard("BACKUP.NO_BACKUP", "msdb", "critical"), "low")
        self.assertEqual(
            eng._apply_system_db_guard("BACKUP.NO_BACKUP", "huangjinjing", "critical"), "critical")

    def test_object_name_with_slash_is_matched(self):
        """文件增长规则的 object_name 形如 msdb/MSDBData。"""
        eng = _engine()
        self.assertEqual(
            eng._apply_system_db_guard("FILE.PERCENT_GROWTH", "msdb/MSDBData", "medium"), "low")
        self.assertEqual(
            eng._apply_system_db_guard("FILE.PERCENT_GROWTH", "huangjinjing/app_data", "medium"),
            "medium")

    def test_never_upgrades_severity(self):
        """cap 只降不升：本来就比 cap 低的一律原样。"""
        eng = _engine()
        self.assertEqual(
            eng._apply_system_db_guard("BACKUP.NO_BACKUP", "msdb", "low"), "low")

    def test_guard_disabled_when_config_absent(self):
        """规则包没有 system_database_guard 段时完全不生效（向后兼容历史规则包）。"""
        eng = _engine({"thresholds": {}, "rules": {}, "excluded_databases": ["tempdb"]})
        self.assertEqual(
            eng._apply_system_db_guard("BACKUP.NO_BACKUP", "msdb", "critical"), "critical")

    def test_rule_pack_declares_expected_guard(self):
        """规则包配置本身也要守住：清单被误删会让上面所有断言失去意义。"""
        guard = _config().get("system_database_guard") or {}
        self.assertEqual(guard.get("severity_cap"), "low")
        self.assertEqual(sorted(guard.get("databases") or []), ["master", "model", "msdb"])
        self.assertIn("BACKUP.NO_BACKUP", guard.get("applies_to") or [])
        self.assertNotIn("MEM.CACHE_IMBALANCE", guard.get("applies_to") or [])


if __name__ == "__main__":
    unittest.main()
