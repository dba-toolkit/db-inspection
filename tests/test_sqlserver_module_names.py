"""采集端注册的模块名 与 呈现端 _coll 查找的名字 必须对得上。

为什么需要这条测试：`presentation.py` 的 `_coll(name)` 在找不到模块时回退成
`{"status": "ok", "row_count": 0}` —— 也就是说**名字写错/改名没同步，报告会静默显示"正常"**，
而不是报错。这一类缺陷在报告里看不出来（页面照常生成、状态列照常打勾），
只有换个采集包或换个模块名才会暴露，所以必须在代码层拦。

验的是**第一个参数**：多参数形式 `_coll("新名", "旧名")` 是给历史采集包留的兼容回退
（改名不应该改变旧包的解读结果），第一个参数永远应当是当前采集器的名字。
旧名单独登记在 LEGACY_ALIASES 里，新增别名必须显式登记，避免"随手多写一个名字"蒙混过关。
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
COLLECTOR = ROOT / "inspection" / "sqlserver_inspection.ps1"
PRESENTATION = ROOT / "plugins" / "sqlserver" / "presentation.py"

# 采集端改名后保留的旧名（只作为 _coll 的第二个及以后参数出现）。
# 已核对：这三个名字在旧采集器（git HEAD 版本）里确实注册过，不是凭空的别名。
LEGACY_ALIASES = {
    "复制配置",
    "无主键表（深度）",
    "大表统计（深度）",
}


def registered_module_names() -> set[str]:
    """采集端注册模块名的四种写法。

    三种来自 `Invoke-CollectionModule` / `Add-ModuleStatus`，第四种是内联的
    `$moduleStatus.Add([ordered]@{name='X';...})`（实例基础信息 / TempDB / 安全与权限 /
    OS 指标就是这样登记的）—— 只认前三种会误报"呈现端名字找不到"。
    """
    text = COLLECTOR.read_text(encoding="utf-8-sig")
    names = set(re.findall(r"Invoke-CollectionModule\s+'([^']+)'", text))
    names |= set(re.findall(r"Invoke-CollectionModule\s+-Name\s+'([^']+)'", text))
    names |= set(re.findall(r"Add-ModuleStatus\s+-Name\s+'([^']+)'", text))
    names |= set(re.findall(r"moduleStatus\.Add\(\[ordered\]@\{name='([^']+)'", text))
    if "name=$osModuleName" in text:
        names.add("OS 指标")
    return names


def coll_calls() -> list[tuple[int, list[str]]]:
    text = PRESENTATION.read_text(encoding="utf-8")
    calls: list[tuple[int, list[str]]] = []
    for match in re.finditer(r"_coll\(([^)]*)\)", text):
        args = re.findall(r'"([^"]+)"', match.group(1))
        if args:
            calls.append((text.count("\n", 0, match.start()) + 1, args))
    return calls


class SQLServerModuleNameContractTests(unittest.TestCase):
    @unittest.skipUnless(COLLECTOR.exists(), "缺少 SQL Server 采集脚本")
    def test_every_coll_call_resolves_to_a_registered_module(self) -> None:
        registered = registered_module_names()
        self.assertTrue(registered, "未能从采集脚本解析出任何模块名，解析式需要更新")
        missing = [
            f"presentation.py:{line} _coll({args[0]!r})"
            for line, args in coll_calls()
            if args[0] not in registered
        ]
        self.assertEqual(
            missing,
            [],
            "以下 _coll 的主名在采集端没有任何注册，报告会静默显示为正常：\n  "
            + "\n  ".join(missing),
        )

    @unittest.skipUnless(COLLECTOR.exists(), "缺少 SQL Server 采集脚本")
    def test_alias_arguments_are_known_legacy_names(self) -> None:
        """多参数形式只允许登记过的旧名，防止把错名字塞进第二个参数当"兜底"。"""
        registered = registered_module_names()
        unknown = set()
        for _line, args in coll_calls():
            for alias in args[1:]:
                if alias not in registered and alias not in LEGACY_ALIASES:
                    unknown.add(alias)
        self.assertEqual(
            sorted(unknown),
            [],
            f"出现了未登记的 _coll 别名（旧名清单需要更新或名字写错了）：{sorted(unknown)}",
        )

    @unittest.skipUnless(COLLECTOR.exists(), "缺少 SQL Server 采集脚本")
    def test_collector_has_no_dead_media_enum_column(self) -> None:
        """device_type 是裸数字枚举、下游无消费者，按 AGENTS.md 已从采集端移除。"""
        text = COLLECTOR.read_text(encoding="utf-8-sig")
        self.assertNotIn("device_type", text)

    @unittest.skipUnless(COLLECTOR.exists(), "缺少 SQL Server 采集脚本")
    def test_collector_does_not_compare_dbnull_by_value(self) -> None:
        """DBNull 只能判类型，不能判值。

        `$Value -eq [DBNull]::Value` 在 PowerShell 里会先把右侧转成左侧类型，
        于是 `$true -eq [DBNull]::Value` 结果是 **True** —— 后果是所有"值为真"的 bit 列
        都被写成 null（实测 bit=1 -> null、bit=0 -> false）。
        危害不是"少一列"：`is_auto_create_stats_on` 这类 bit NOT NULL 列整包变 null，
        `DB.CHAINING` / `DB.TRUSTWORTHY` 这类判据在真实包上永不触发，属于静默假阴性。
        注释里允许出现这个字符串（用来说明为什么不能这么写），所以只看非注释行。
        """
        code_lines = [
            line for line in COLLECTOR.read_text(encoding="utf-8-sig").splitlines()
            if not line.lstrip().startswith("#")
        ]
        offenders = [line.strip() for line in code_lines if "-eq [DBNull]::Value" in line]
        self.assertEqual(
            offenders, [],
            "采集端又出现了按值比较 DBNull 的写法，会让所有真值 bit 列变成 null：\n  "
            + "\n  ".join(offenders),
        )
        self.assertTrue(
            any("-is [System.DBNull]" in line for line in code_lines),
            "Convert-DbValue 的 DBNull 判型不存在，空值处理可能被改坏",
        )


class SQLServerRuleHelperTests(unittest.TestCase):
    """`_dt` / `_elapsed_seconds` 是全部"距今多久"判据的地基。

    `_dt` 曾经**恒返回 None**（函数体只剩 `if not value: return None`，解析代码被
    `_elapsed_seconds` 插到后面落在 `return` 之后，成了不可达代码）。后果是所有时间比较
    退化为"时间未知"：有新鲜备份的库被写成"无完整备份记录"，有 CHECKDB 记录的库被写成
    "未取得成功 CHECKDB 时间"。这类缺陷不抛异常、不让任何数量断言变红 —— 只有行为断言能拦。
    """

    def test_dt_parses_iso_timestamps(self) -> None:
        from plugins.sqlserver.rules import _dt

        self.assertIsNone(_dt(None))
        self.assertIsNone(_dt(""))
        self.assertIsNone(_dt("不是时间"))
        self.assertIsNotNone(_dt("2026-07-28T00:02:24.9921153+08:00"), "_dt 又变成恒返回 None 了")
        self.assertIsNotNone(_dt("2026-07-28T00:02:24"))
        self.assertEqual(_dt("2026-07-28T00:02:24+08:00").year, 2026)

    def test_elapsed_seconds_normalizes_timezone(self) -> None:
        from datetime import datetime, timedelta, timezone

        from plugins.sqlserver.rules import _dt, _elapsed_seconds

        aware_now = datetime(2026, 7, 28, 0, 2, 24, tzinfo=timezone(timedelta(hours=8)))
        # 采集包里的时间戳可能不带时区（agent_datetime 直出），直接相减会 TypeError
        self.assertAlmostEqual(
            _elapsed_seconds(aware_now, _dt("2026-07-28T00:00:00")), 144.0, places=3
        )
        self.assertAlmostEqual(
            _elapsed_seconds(aware_now, _dt("2026-07-28T00:02:24.9921153+08:00")), -0.992, places=3
        )
        self.assertIsNone(_elapsed_seconds(aware_now, None))

    def test_job_duration_is_rendered_as_hhmmss_not_seconds(self) -> None:
        """sysjobhistory.run_duration 是 HHMMSS 整数，不是秒。

        现场报告里 Auto Fee 是 1253 秒（约 20.9 分钟），对应 run_duration=2053；
        直接当秒渲染会写成 2053 秒，夸大近一倍。
        """
        import json as _json
        import tempfile

        from plugins.sqlserver.analyzer import analyze_sqlserver

        snapshot = {
            "collector_version": "1.3.0",
            "target": {"server": "T"},
            "collection": {"module_status": [{"name": "SQL Agent作业清单", "status": "success", "row_count": 1}]},
            "agent_jobs": [{"job_name": "Auto Fee", "enabled": 1, "last_run_status_desc": "SUCCEEDED",
                            "last_run_duration": 2053}],
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "snapshot.json").write_text(_json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
            model = analyze_sqlserver(root, root / "out", ROOT / "plugins" / "sqlserver" / "inspection_rules.json")
        titles = [i["title"] for s in model["sections"] for i in s["items"]]
        self.assertIn("作业清单", titles)
        for section in model["sections"]:
            for item in section["items"]:
                if item["title"] != "作业清单":
                    continue
                self.assertIn("最近耗时", item["display"]["headers"])
                rendered = item["display"]["rows"][0][-1]
                self.assertEqual(rendered, "20m53s", f"run_duration=2053 应渲染成 20m53s，实际 {rendered}")


if __name__ == "__main__":
    unittest.main()
