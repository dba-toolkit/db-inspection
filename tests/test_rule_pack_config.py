"""规则包配置键的消费面自检。

`coverage_audit.py` 管的是「采集包采了什么 / 分析读了多少」，这里管的是同一件事的
上一层：**规则包声明了什么 / 代码真读了多少**。两边都会出现同一类腐烂 —— 声明了
但没人读的键。区别是采集侧的后果是数据白采，规则侧的后果更隐蔽：运维照着规则包
调一个阈值，代码根本不看那个键，**改完没有任何效果，而且不会报错**。

三类断言：

* ``globals`` / per-rule ``threshold`` 里声明的键，必须真的有代码去请求它；
* 代码请求的键，必须在规则包某处有声明（否则实际生效的是代码里的默认值，
  规则包给出的那个数字是假的）；
* 收口时删掉的死键不得复活。

外加一条「转正」断言：二级阈值（告警档 + 严重档）的两个键必须同时满足
「声明了」和「被请求了」，否则严重档就是个装饰 —— 这正是上一轮把它删掉的原因。

键的请求形态各库不同（MySQL/Oracle 用 ``self._threshold(rule, "key", default)``，
PG 用内层函数 ``threshold("RULE.ID", "key", default)``），所以下面按「调用里出现的
小写字符串字面量」抽取，而不是按固定参数位置 —— 规则 id 是大写，不会混进来。
"""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

# 库 -> (规则包, 规则实现源码, per-rule 键也在这里声明)
TARGETS: dict[str, tuple[str, str]] = {
    "mysql": ("plugins/mysql/inspection_rules.json", "plugins/mysql/rules.py"),
    "oracle": (
        "plugins/oracle/inspection_rules_oracle.json",
        "plugins/oracle/oracle_rules.py",
    ),
    "postgresql": (
        "plugins/postgresql/inspection_rules.json",
        "plugins/postgresql/rules.py",
    ),
}

# 阈值请求调用：``_threshold(...)`` / ``threshold(...)``
# 前面只挡标识符字符，不能连点号一起挡 —— MySQL/Oracle 的写法是
# ``self._threshold(...)``，点号正是调用的前缀。
_CALL = re.compile(r"(?<![\w])_?threshold\(([^)]*)\)")
# 调用参数里的小写键名（规则 id 是大写，默认值不是字符串）
_KEY_IN_CALL = re.compile(r"""["']([a-z][a-z0-9_]*)["']""")
# 直接读 globals
_GLOBALS_GET = re.compile(r"""_globals\s*(?:\.get\(\s*|\[\s*)["']([a-z][a-z0-9_]*)["']""")

# 收口时删掉的死键：声明了但没有任何代码路径请求它。
# 复活 = 运维又拿到一个「调了没反应」的旋钮。
RETIRED_CONFIG_KEYS: dict[str, tuple[str, ...]] = {
    # 与 per-rule 的 long_transaction_seconds / replication_lag_seconds 重复，
    # 而 per-rule 值优先级更高，所以这两个 globals 永远读不到。
    "mysql": (
        "long_transaction_threshold_seconds",
        "replication_lag_threshold_seconds",
    ),
    # 无失败登录规则消费（采集端有「失败登录审计统计」表，规则端从未实现）。
    # archive_switch_warning_per_hour 与在用的 redo_switch_per_hour_warning 同名不同值。
    "oracle": (
        "failed_login_warning",
        "archive_switch_warning_per_hour",
    ),
    "postgresql": (),
}
# 目前没有仍处于「已删除」状态的 per-rule 死键 —— 上一轮那两个（Oracle 的
# redo_member_min / library_cache_hit_critical）这一步已经接线成真旋钮，见下方
# TWO_TIER_THRESHOLDS。留着空表是为了下次真有死键时有个明确落点。
RETIRED_PER_RULE_KEYS: dict[str, dict[str, tuple[str, ...]]] = {
    "oracle": {},
}

# 二级阈值：同一规则里「告警档 + 严重档」两个键。前者决定是否触发，
# 后者通过 severity_override 把 finding 的级别从基础值提到 critical。
# 两个键都必须真的被代码请求（否则又是死旋钮），且严重档必须比告警档更严格，
# 否则某个取值区间会出现「级别是严重但其实没触发」的错配。
TWO_TIER_THRESHOLDS: dict[str, dict[str, tuple[str, str]]] = {
    "oracle": {
        "ORA.STORAGE.TABLESPACE": ("tablespace_warning_pct", "tablespace_critical_pct"),
        "ORA.PERFORMANCE.LIBRARY_CACHE": (
            "library_cache_hit_warning",
            "library_cache_hit_critical",
        ),
    },
}


def load_pack(database: str) -> dict:
    path = ROOT / TARGETS[database][0]
    return json.loads(path.read_text(encoding="utf-8"))


def source_text(database: str) -> str:
    return (ROOT / TARGETS[database][1]).read_text(encoding="utf-8")


def requested_keys(database: str) -> set[str]:
    """代码真正会去规则包取的那些键。"""
    text = source_text(database)
    found: set[str] = set(_GLOBALS_GET.findall(text))
    for arguments in _CALL.findall(text):
        found.update(_KEY_IN_CALL.findall(arguments))
    return found


def declared_globals(pack: dict) -> set[str]:
    return set(pack.get("globals", {}))


def declared_per_rule(pack: dict) -> dict[str, set[str]]:
    result: dict[str, set[str]] = {}
    for rule_id, rule in (pack.get("rules") or {}).items():
        threshold = (rule or {}).get("threshold") or {}
        if threshold:
            result[rule_id] = set(threshold)
    return result


def declared_keys(pack: dict) -> set[str]:
    return declared_globals(pack) | {
        key for keys in declared_per_rule(pack).values() for key in keys
    }


class RulePackConfigAuditTests(unittest.TestCase):
    def test_declared_globals_keys_are_all_requested(self) -> None:
        for database in TARGETS:
            with self.subTest(database=database):
                dead = declared_globals(load_pack(database)) - requested_keys(database)
                self.assertEqual(
                    dead, set(),
                    f"{database} 规则包 globals 里这些键没有任何代码请求，"
                    f"改它们不会有任何效果：{sorted(dead)}",
                )

    def test_declared_per_rule_thresholds_are_all_requested(self) -> None:
        for database in TARGETS:
            pack = load_pack(database)
            live = requested_keys(database)
            dead: dict[str, list[str]] = {}
            for rule_id, keys in declared_per_rule(pack).items():
                missing = keys - live
                if missing:
                    dead[rule_id] = sorted(missing)
            with self.subTest(database=database):
                self.assertEqual(
                    dead, {},
                    f"{database} 规则包 per-rule threshold 里这些键没有代码请求：{dead}",
                )

    def test_every_requested_key_is_declared_somewhere(self) -> None:
        # 请求了但两处都没声明 → 实际生效的是代码里的默认值，
        # 规则包给出的数字（如果有）是假的。
        for database in TARGETS:
            with self.subTest(database=database):
                undeclared = requested_keys(database) - declared_keys(load_pack(database))
                self.assertEqual(
                    undeclared, set(),
                    f"{database} 代码请求了这些键，但规则包没声明，"
                    f"实际走的是代码默认值：{sorted(undeclared)}",
                )

    def test_retired_globals_keys_stay_removed(self) -> None:
        for database, keys in RETIRED_CONFIG_KEYS.items():
            pack = load_pack(database)
            with self.subTest(database=database):
                self.assertEqual(
                    declared_globals(pack) & set(keys), set(),
                    f"{database} 已收口的死键又回到了 globals：{sorted(keys)}",
                )

    def test_retired_per_rule_keys_stay_removed(self) -> None:
        for database, rules in RETIRED_PER_RULE_KEYS.items():
            declared = declared_per_rule(load_pack(database))
            for rule_id, keys in rules.items():
                with self.subTest(database=database, rule=rule_id):
                    self.assertEqual(
                        declared.get(rule_id, set()) & set(keys), set(),
                        f"{database} {rule_id} 已收口的死键又回到了 threshold：{sorted(keys)}",
                    )

    def test_two_tier_thresholds_are_declared_and_requested(self) -> None:
        # 二级阈值的两个键都必须「规则包声明了 + 代码真去取」。少了任一半，
        # 严重档就退化成一个装饰：改它不会有任何效果，这正是上一轮删掉它们的原因。
        for database, rules in TWO_TIER_THRESHOLDS.items():
            pack = load_pack(database)
            declared = declared_per_rule(pack)
            live = requested_keys(database)
            for rule_id, (warn_key, crit_key) in rules.items():
                with self.subTest(database=database, rule=rule_id):
                    self.assertIn(
                        warn_key, declared.get(rule_id, set()),
                        f"{database} {rule_id} 缺告警档键 {warn_key}",
                    )
                    self.assertIn(
                        crit_key, declared.get(rule_id, set()),
                        f"{database} {rule_id} 缺严重档键 {crit_key}",
                    )
                    self.assertIn(
                        warn_key, live, f"{database} {warn_key} 没有代码请求",
                    )
                    self.assertIn(
                        crit_key, live, f"{database} {crit_key} 没有代码请求",
                    )
                    thresholds = (pack["rules"][rule_id] or {}).get("threshold") or {}
                    self.assertNotEqual(
                        thresholds[warn_key], thresholds[crit_key],
                        f"{database} {rule_id} 的告警档与严重档取值相同，"
                        f"严重的那一层永远不会单独命中",
                    )

    def test_extractor_finds_the_live_keys_it_should(self) -> None:
        # 守住抽取逻辑本身：换掉调用写法会让上面几条断言变成「恒真」。
        mysql = requested_keys("mysql")
        self.assertIn("connection_usage_warning", mysql)
        self.assertIn("long_transaction_seconds", mysql)
        postgresql = requested_keys("postgresql")
        self.assertIn("cache_hit_min", postgresql)
        self.assertIn("quality_score_min", postgresql)
        oracle = requested_keys("oracle")
        self.assertIn("tablespace_critical_pct", oracle)
        # 二级阈值也要能被抽到 —— 这两条调用传的第一参是变量（rule），
        # 不是字面量，专门守住「不按参数位置、按调用里的小写字面量抽」这条规则。
        self.assertIn("library_cache_hit_critical", oracle)
        self.assertIn("redo_member_min", oracle)


if __name__ == "__main__":
    unittest.main()
