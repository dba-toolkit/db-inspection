"""采集脚本在 `Set-StrictMode -Version Latest` 下的安全性 —— 静态守门。

为什么需要这条测试：脚本第 123 行开了 `Set-StrictMode -Version Latest`。此时

    $obj.missingKey     -> PropertyNotFoundException（FullyQualifiedErrorId = PropertyNotFoundStrict）
    $null.anything      -> 同上

**实测（PS 5.1）**：对空 `[ordered]@{}` 取键、对 PSCustomObject 取不存在的属性、对 `$null`
取任何属性，三者都抛 `PropertyNotFoundStrict`。

采集包里"某个模块没采到 / 该版本没有这一项 / 某项计数器库不可用"是常态，所以
**任何可能缺键的取属性都必须走 `Get-ObjProp`**。否则一个"模块没采到"会被放大成
"整脚本崩溃、一个包都产不出来" —— 2026-10-08 真机首跑就是这样崩的：

  * 根因：实例查询里 `windows_version` / `language` / `os_language` **三个列名都不存在**
    （`sys.dm_os_windows_info` 自 2008 R2 SP1 起只有 windows_release / windows_service_pack_level /
    windows_sku / os_language_version 四列）→ 整条 SQL 报 "Invalid column name"；
  * 已被 catch 吞掉（**而且那个 catch 当时不写日志**，控制台完全无痕）；
  * 于是 `$snapshot.instance` 落成空 `[ordered]@{}`，下一行 `$snapshot.instance.connection_ip`
    直接抛 `PropertyNotFoundStrict` → 脚本终止、无包。

两条一起守：① 列名不许再凭记忆写；② 可能缺键的取属性不许用裸点取。

**第三条纪律（同日、同一批修复中由等价冒烟抓出）**：`Get-ObjProp` **不能用来取数组型字段**。

    ① PowerShell 的 `return $value` 走管道，**单元素数组会被拆包成标量**；
    ② 而 StrictMode 下对 PSCustomObject 取 `.Count` 会抛 `PropertyNotFoundStrict`。

实测（PS 5.1）：`[pscustomobject]@{name='C:'}.Count` → `PropertyNotFoundStrict`；
`function f { return $o.PSObject.Properties['k'].Value }` 传 1 元素数组进去 → 出来是标量。

两者相乘的后果是**机器上恰好只有 1 块盘 / 1 张网卡就崩**（2 块盘反而正常）——
这是典型的"只有在特定环境才复现"的雷。所以数组型字段一律走 `Get-ObjArray`（恒返回数组）。
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
COLLECTOR = ROOT / "inspection" / "sqlserver_inspection.ps1"

# $snapshot 里这两个子对象在脚本开头就以字面量构造，键必定存在，允许裸点取。
ALWAYS_PRESENT_SNAPSHOT_KEYS = {"collection", "target"}

# 严格模式安全访问器
HELPERS = ("Get-ObjProp", "Set-ObjProp", "Get-ObjArray")

# 数组型字段：必须用 Get-ObjArray 取，之后才可以放心取 .Count
ARRAY_FIELD_CALL_SITES = (
    ("$lastDisks", "$last", "disks"),
    ("$lastNet", "$last", "network"),
    ("$osDisks", "$os", "physical_disks"),
)

_ASSIGN_OBJPROP = re.compile(r"^\s*\$(?P<var>[A-Za-z_]\w*)\s*=\s*Get-ObjProp\b")
_ASSIGN_OBJARRAY = re.compile(r"^\s*\$(?P<var>[A-Za-z_]\w*)\s*=\s*Get-ObjArray\b")

# ---------------------------------------------------------------------------
# DMV 陷阱列名：这些列在对应 DMV 上**不存在**，写了会让整条 SQL 报
# "Invalid column name"，然后被模块级 catch 吞成 failed（真机 2026-09-10 实测）。
# 每项：(DMV 名, 禁止的列名, 官方依据 / 正确写法)
# 官方文档：
#   sys.dm_exec_sessions      -> https://learn.microsoft.com/sql/relational-databases/system-dynamic-management-views/sys-dm-exec-sessions-transact-sql
#   sys.dm_server_services    -> https://learn.microsoft.com/sql/relational-databases/system-dynamic-management-objects/sys-dm-server-services-transact-sql
#   sys.dm_os_windows_info    -> 同族 DMV 页
DMV_FORBIDDEN_COLUMNS = (
    (
        "sys.dm_exec_sessions",
        "physical_reads",
        "sys.dm_exec_sessions 没有 physical_reads 列。物理读就是它自己的 reads 列"
        "（官方描述：Number of physical reads performed, by requests in this session）。",
    ),
    (
        "sys.dm_server_services",
        "servicetype",
        "sys.dm_server_services 没有 servicetype 列（官方列：servicename / startup_type[_desc] / "
        "status[_desc] / process_id / last_startup_time / service_account / filename / "
        "is_clustered / cluster_nodename / instant_file_initialization_enabled）。",
    ),
    (
        "sys.dm_server_services",
        "version",
        "sys.dm_server_services 没有 version 列；实例版本在 sys.dm_os_sys_info.product_version。",
    ),
    (
        "sys.dm_os_windows_info",
        "windows_version",
        "sys.dm_os_windows_info 没有 windows_version；只有 windows_release / "
        "windows_service_pack_level / windows_sku / os_language_version。",
    ),
)

# here-string 形式的 SQL 块（@" ... "@）——模块 SQL 都写在这种块里。
_SQL_BLOCK = re.compile(r'@"(.*?)"@', re.S)

# DECLARE 语句（以分号结尾）
_DECLARE_STMT = re.compile(r"\bDECLARE\b[^;]*;", re.I)


def strip_sql_line_comments(text: str) -> str:
    """去掉 SQL 的 `--` 行注释，**保留换行**（行号不漂）。

    必要：脚本里刻意用注释记录了"曾经写错的列名"（例如实例查询 L361 的
    `-- v1.3.2 之前这里写的是 windows_version / language / os_language`）——
    那是**说明为什么不能这么写**，不该被当成违规代码。同理，本文件守门
    报错信息里也会引用反例。
    """
    return "\n".join(line.split("--", 1)[0] for line in text.split("\n"))


def sql_blocks(text: str):
    """产出脚本里每个 here-string SQL 块的正文（已剥掉 `--` 行注释）。"""
    for match in _SQL_BLOCK.finditer(text):
        yield strip_sql_line_comments(match.group(1))


def find_forbidden_column_in_blocks(text: str, dmv: str, column: str) -> list[str]:
    """在含 `FROM/JOIN <dmv>` 的 SQL 块里找出对 <column> 的引用。

    **别名感知**，避免误伤别的表上的同名列（例如 `qs.total_physical_reads` 来自
    `sys.dm_exec_query_stats`，是合法列）。只认两种形态：

      * ``<alias>.<column>``，alias 限该 DMV 在本块里的别名；
      * 裸 ``<column>``（前面既不是词字符也不是点）—— 裸列名不可能来自别的表。
    """
    hits: list[str] = []
    for block in sql_blocks(text):
        if not re.search(r"\b" + re.escape(dmv) + r"\b", block, re.I):
            continue
        aliases = re.findall(
            r"(?:FROM|JOIN)\s+" + re.escape(dmv) + r"\s+(?:AS\s+)?([A-Za-z_]\w*)",
            block,
            re.I,
        )
        patterns = [
            re.compile(r"\b" + re.escape(a) + r"\." + re.escape(column) + r"\b", re.I)
            for a in aliases
        ]
        patterns.append(re.compile(r"(?<![\w.])" + re.escape(column) + r"\b", re.I))
        for pattern in patterns:
            match = pattern.search(block)
            if match:
                hits.append(match.group(0))
                break
    return hits


def collector_text() -> str:
    # 脚本是 UTF-8 带 BOM + 纯 LF，必须用 utf-8-sig 读
    return COLLECTOR.read_text(encoding="utf-8-sig")


def code_lines(text: str):
    """产出 (行号, 行内容)，**跳过纯注释行**。

    注释里会引用反例代码（比如解释这次事故时写到的裸取写法），不该被当成真代码。
    只跳过「整行以 # 开头」的行：真正的违规代码不会整行以 # 开头，而 here-string 里的
    SQL 虽然可能有 `#temp` 表名，但 SQL 里不会出现 `$snapshot.` 这种 PowerShell 语法。
    """
    for lineno, line in enumerate(text.split("\n"), 1):
        if line.lstrip().startswith("#"):
            continue
        yield lineno, line


class StrictModeSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.text = collector_text()

    def test_strict_mode_is_enabled(self) -> None:
        """守门本身以 StrictMode 为前提；被关掉时这条测试要提醒。"""
        self.assertIn(
            "Set-StrictMode -Version Latest",
            self.text,
            "脚本应保持 Set-StrictMode -Version Latest（它是发现口误的护栏）；"
            "若确实要降级，请同时把本文件的安全规则一起改掉，而不是单方面关掉守卫。",
        )

    def test_no_bare_two_level_snapshot_access(self) -> None:
        """`$snapshot.<key>.<prop>` 只允许 collection / target（键在构造时就存在）。"""
        pattern = re.compile(r"\$snapshot\.([A-Za-z_]\w*)\.([A-Za-z_]\w*)")
        offenders = []
        for lineno, line in code_lines(self.text):
            for match in pattern.finditer(line):
                if match.group(1) not in ALWAYS_PRESENT_SNAPSHOT_KEYS:
                    offenders.append(f"L{lineno}: {match.group(0)}")
        self.assertEqual(
            [],
            offenders,
            "这些位置对 $snapshot 的子键做了裸点取，键可能不存在 → StrictMode 下抛异常；"
            "请改用 Get-ObjProp：\n  " + "\n  ".join(offenders),
        )

    def test_no_bare_access_on_script_built_objects(self) -> None:
        """脚本自己造的对象（空实例 / OS 身份 / OS 采样点）不许裸点取。"""
        patterns = [
            re.compile(r"\$inst\.[A-Za-z_]"),
            re.compile(r"\$osi\.[A-Za-z_]"),
            re.compile(r"\$last\.(?:cpu_pct|available_mb|processor_queue_length|disks|network)\b"),
        ]
        offenders = []
        for lineno, line in code_lines(self.text):
            for pattern in patterns:
                for match in pattern.finditer(line):
                    offenders.append(f"L{lineno}: {match.group(0)}")
        self.assertEqual(
            [],
            offenders,
            "$inst / $osi / OS 采样点都是脚本自造的、键可能缺失（采样点是按指标各自 try/catch "
            "后条件写键的）→ 必须走 Get-ObjProp：\n  " + "\n  ".join(offenders),
        )

    def test_strict_safe_accessors_are_defined_before_first_use(self) -> None:
        """两个访问器必须「先定义、后调用」，且真的被调用过。

        PowerShell 按执行顺序解析脚本：函数调用写在定义之前会报 CommandNotFound。
        """
        for helper in HELPERS:
            defn = self.text.find(f"function {helper} {{")
            self.assertNotEqual(
                -1,
                defn,
                f"{helper} 未定义；请保留 Convert-Table 之后的那两个访问器。",
            )
            # 该函数块的结束位置 = 下一个顶层 function 的起点
            block_end = self.text.find("\nfunction ", defn + 1)
            if block_end == -1:
                block_end = len(self.text)
            call = self.text.find(helper + " ", block_end)
            self.assertNotEqual(
                -1,
                call,
                f"{helper} 定义了却从未被调用 —— 安全访问器形同虚设。",
            )
            self.assertLess(
                defn,
                call,
                f"{helper} 的调用出现在定义之前（PowerShell 按执行顺序解析，会报找不到命令）。",
            )

    def test_obj_array_reader_preserves_arrays(self) -> None:
        """`Get-ObjArray` 必须真的保住数组（`return , @(...)`）。

        少了 `,` 就会退化成 `return`，PowerShell 走管道把单元素数组拆成标量，
        调用方的 `.Count` 随即在 StrictMode 下抛异常。
        """
        self.assertIn(
            "function Get-ObjArray {",
            self.text,
            "缺少数组型字段读取器 Get-ObjArray。",
        )
        self.assertIn(
            "return , @($v | Where-Object { $null -ne $_ })",
            self.text,
            "Get-ObjArray 必须以 `return , @(...)` 返回：少了逗号，单元素数组会被拆包成标量，"
            "调用方取 .Count 时抛 PropertyNotFoundStrict。",
        )

    def test_array_typed_fields_use_obj_array_reader(self) -> None:
        """三个数组型字段必须走 Get-ObjArray。"""
        missing = []
        for var, obj, key in ARRAY_FIELD_CALL_SITES:
            # 允许对齐用的空白：`$lastNet   = Get-ObjArray $last 'network'`
            pattern = re.compile(
                re.escape(var) + r"\s*=\s*Get-ObjArray\s+" + re.escape(obj) + r"\s+'" + re.escape(key) + r"'"
            )
            if not pattern.search(self.text):
                missing.append(f"{var} = Get-ObjArray {obj} '{key}'")
        self.assertEqual(
            [],
            missing,
            "这些数组型字段没有走 Get-ObjArray（改用 Get-ObjProp 会在单元素时被拆包）：\n  "
            + "\n  ".join(missing),
        )

    def test_no_bare_count_on_objprop_result(self) -> None:
        r"""不许对 Get-ObjProp 的结果直接取 .Count。

        两种违规形态都拦：
          * 同行：`Get-ObjProp $o 'k' @()).Count`（没有 `@()` 包裹）；
          * 跨行：`$x = Get-ObjProp ...` 之后又写 `$x.Count`。
        数组型字段请改用 Get-ObjArray（恒返回数组）。
        """
        offenders = []
        from_objprop: set[str] = set()
        for lineno, line in code_lines(self.text):
            m = _ASSIGN_OBJPROP.match(line)
            if m:
                from_objprop.add(m.group("var"))
                continue
            m = _ASSIGN_OBJARRAY.match(line)
            if m:
                from_objprop.discard(m.group("var"))
                continue
            if "Get-ObjProp" in line and ".Count" in line and "@(Get-ObjProp" not in line:
                offenders.append(f"L{lineno}: {line.strip()}")
                continue
            for var in sorted(from_objprop):
                if re.search(r"\$" + re.escape(var) + r"\.Count\b", line):
                    offenders.append(f"L{lineno}: ${var}.Count（${var} 来自 Get-ObjProp）")
        self.assertEqual(
            [],
            offenders,
            "对 Get-ObjProp 的结果取 .Count：单元素数组会被拆包成标量，"
            "StrictMode 下 .Count 抛 PropertyNotFoundStrict（实测：机器只有 1 块盘/1 张网卡就崩）。"
            "数组型字段请改用 Get-ObjArray：\n  " + "\n  ".join(offenders),
        )

    def test_instance_query_failure_is_logged(self) -> None:
        """实例查询失败必须留痕。

        原先那个 catch 只写 limitations / module_status —— 两者都要等包产出才看得见，
        而脚本会在下游崩掉、包根本产不出来，于是"哪条 SQL 错了"永远查不出来。
        """
        marker = "实例基础信息 failed:"
        self.assertIn(
            marker,
            self.text,
            "实例基础信息的 catch 必须以 Write-CollectorLog 报错（可搜索 '实例基础信息 failed:'）；"
            "否则实例查询失败在控制台完全无痕。",
        )

    def test_dm_os_windows_info_uses_documented_column_names(self) -> None:
        """`sys.dm_os_windows_info` 的列名以官方文档为准。

        官方列（2008 R2 SP1 起至今，含 SQL 2022+）：
        windows_release / windows_service_pack_level / windows_sku / os_language_version。
        历史上这里写过 windows_version / language / os_language —— 三个都不存在。
        """
        self.assertIn(
            "w.windows_release",
            self.text,
            "实例查询应使用官方列名 w.windows_release。",
        )
        for bad in ("w.windows_version", "w.language", "w.os_language"):
            self.assertNotIn(
                bad,
                self.text,
                f"{bad} 不是 sys.dm_os_windows_info 的列（官方文档只有 windows_release / "
                "windows_service_pack_level / windows_sku / os_language_version）；"
                "写成它会让整条实例查询报 Invalid column name。",
            )

    def test_dmvs_do_not_reference_columns_that_do_not_exist(self) -> None:
        """DMV 列名以官方文档为准（别名感知，不误伤别的表的同名列）。

        2026-09-10 真机首跑：`dm_exec_sessions.physical_reads` 与
        `dm_server_services.servicetype` / `.version` 三处列名不存在 → 两个模块
        status=failed（SQL 报 Invalid column name，被模块级 catch 吞成一行 limitations）。
        注意 SQL Server 只报**第一个**无效列名，所以 `servicetype` 之外还藏着一个
        `version` —— 这类"修一个冒一个"的坑必须整表守住。
        """
        offenders: list[str] = []
        for dmv, column, why in DMV_FORBIDDEN_COLUMNS:
            for hit in find_forbidden_column_in_blocks(self.text, dmv, column):
                offenders.append(f"{dmv}.{column}（命中 {hit!r}）—— {why}")
        self.assertEqual(
            [],
            offenders,
            "这些 SQL 引用了不存在的列，会让整个模块 failed：\n  " + "\n  ".join(offenders),
        )

    def test_no_table_variable_mixed_with_scalar_in_declare(self) -> None:
        r"""表变量必须单独一条 `DECLARE`。

        T-SQL 不允许把普通变量与表变量合并声明：

            DECLARE @dist sysname, @blocks table(...);   -- 关键字 'table' 附近有语法错误

        判据只看 `table(` **之前**的那段里有没有逗号 —— 表变量定义体内部的逗号
        （`table(a int, b int)`）是合法的，不能算。
        """
        offenders: list[str] = []
        for match in _DECLARE_STMT.finditer(strip_sql_line_comments(self.text)):
            stmt = match.group(0)
            table_pos = re.search(r"\btable\s*\(", stmt, re.I)
            if table_pos is None:
                continue
            if "," in stmt[: table_pos.start()]:
                offenders.append(stmt.strip()[:100])
        self.assertEqual(
            [],
            offenders,
            "表变量与普通变量写在同一条 DECLARE 里 —— T-SQL 会报「关键字 'table' 附近有语法错误」，"
            "整个模块 failed。请拆成两条 DECLARE：\n  " + "\n  ".join(offenders),
        )


if __name__ == "__main__":
    unittest.main()
