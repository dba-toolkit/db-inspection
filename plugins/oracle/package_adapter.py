"""Oracle 采集包适配器：识别、加载、校验和采集质量统计。

复用 inspection_core 的安全解包、表格解析和数值转换，只保留 Oracle 专属的
状态目录解析、manifest 校验和采集质量算法。
"""

from __future__ import annotations

import csv
import json
import re
import shutil
from collections import Counter
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

from inspection_core.models import PackageContext as BasePackageContext
from inspection_core.package_io import is_safe_member, read_json, safe_extract_tar, sha256_file
from inspection_core.tabular import parse_sadf
from inspection_core.values import safe_int


# 支持的采集器主版本：`1` = 1.4 legacy（`tables/<中文标题>.tsv`，`|` 分隔）；
# `2` = 2.0（`tables/<item_id>.psv` = `|` + `tables/<name>.tsv` = 真 TAB）。
SUPPORTED_COLLECTOR_MAJORS = ("1", "2")
LEGACY_COLLECTOR_MAJOR = "1"

_ITEM_TITLES_PATH = Path(__file__).with_name("item_titles.json")

# 1.4 时代的查找键（中文标题 / 英文别名）→ 2.0 的 item_id。
# `metrics.py` / `oracle_rules.py` / `presentation.py` 是按 1.4 的键写的；2.0 改了名字，
# 这里做一次桥接，免得改 40+ 处调用点。**1.4 包不受影响** —— 这些键在 1.4 里本来就是真键。
# 单一来源说明：只登记"2.0 改过名"的少数几个；能靠 `item_id` / `尾段` / `标题` 命中的不在此列。
LEGACY_ALIASES: dict[str, str] = {
    "asm_diskgroup_summary": "oracle.asm.diskgroup_summary",
    "ASM磁盘组使用情况": "oracle.asm.diskgroup_summary",
    "rman_backup_status": "oracle.rman.backup_summary",
    "最近RMAN备份任务状态": "oracle.rman.backup_summary",
    "无效_不可用索引": "oracle.index.invalid",
    "无效对象检查": "oracle.basic.invalid_objects",
    "长事务检查": "oracle.session.long_transactions",
    "lock_wait_chain": "oracle.session.lock_chain",
    "锁等待链": "oracle.session.lock_chain",
    "block_corruption": "oracle.validation.corruption",
    "controlfile_redundancy": "oracle.validation.controlfile_multiplex",
    "checksum_params": "oracle.validation.block_checksum",
    "scheduler_failures": "oracle.validation.scheduler_failed",
    "Scheduler失败": "oracle.validation.scheduler_failed",
    "archive_mode": "oracle.archive.mode",
}

# 批 6 / N-8 把 4 个"扩展名说谎"的对齐文本从 `tables/` 挪到了 `evidence/*.txt`，
# 但下游 `_check_filesystem_usage` 仍按表格读 `filesystems`。这里只做**已声明的**一项回填，
# 不做通用的 evidence 解析框架（其余 evidence 项按契约只作原文展示）。
_EVIDENCE_TABLES: dict[str, tuple[str, str]] = {
    "system.filesystems": ("filesystems.txt", "df_pt"),
}

# `tables/` 里"采集侧声明的状态"，两种形态（interface §6.3）：
#   2.0 `.psv` 占位行 —— `# oracle-inspection psv-empty … status=empty`
#                       `# oracle-inspection psv-unavailable … status=timeout`
#   1.4 `.tsv` 占位行 —— `# no rows returned`
_PSV_PLACEHOLDER = re.compile(
    r"^#\s*oracle-inspection\s+psv-(?:empty|unavailable)\b.*?\bstatus=(\S+)"
)
_LEGACY_EMPTY_MARK = "# no rows returned"

# 「这项**采集过**吗」的判据用的状态集合。
# ★ 与「有没有数据」是两件事：`empty`（查了、确认没有）**算采集过**，
#   在 `_NO_DATA_STATES` 里的才是「没采到」——两者的报告结论相反，
#   绝不能落成同一句话（「未采集 ≢ 0」，批 14 立的规矩）。
_EMPTY_STATE = "empty"
_DATA_STATES = frozenset({"ok", _EMPTY_STATE, "partial"})
_NO_DATA_STATES = frozenset({
    "unavailable", "permission_denied", "timeout", "error",
    "not_enabled", "skipped", "unsupported", "missing",
})

# `item_titles.json` 里的标题 vs 1.4 文件名，实测有三种漂移（都是同一个 JSON 里的既有事实）：
#   · `UNDO 使用统计 (v\$undostat)`（标题多一个反斜杠）↔ `…(v$undostat)`（文件名）
#   · `无效/不可用索引`（标题）↔ `无效_不可用索引`（文件名）
#   · `禁用/失效约束（非SYS用户）` ↔ `禁用_失效约束（非SYS用户）`
# 不做归一时 6/98 张 1.4 表拿不到 item_id。**归一化的是分隔符与多余转义，不是语义。**
_NAME_BACKSLASH = chr(92)


def name_variants(name: str) -> set[str]:
    """标题/文件名的等价写法集合（含原名）。"""
    base = {name, name.replace(_NAME_BACKSLASH, "")}
    return {variant for variant in base | {item.replace("_", "/") for item in base} if variant}


@lru_cache(maxsize=1)
def load_item_ids_by_title() -> dict[str, str]:
    """`1.4 标题（含等价写法） → 2.0 item_id`。

    ★ 为什么需要它：1.4 的 `tables/<中文标题>.tsv` 只登记了文件名本身，而
    `metrics.py` / `oracle_rules.py` 里的取数点写的是 **2.0 的 item_id**
    （`oracle.session.long_transactions` 等）。`ctx.tables.get(名字, [])` 在键不存在时
    也返回 `[]`，于是"取不到"和"查了 0 行"不可区分 —— **故障完全静默**。
    批 19 的注入实验实测：无效对象 / 长事务 / 锁等待 / 最近备份 四条规则在 1.4 包上
    与数据无关（3 条恒"通过"漏报、1 条恒"触发"误报）。

    单一来源 = 同一个 `item_titles.json`（它本身就是 `item_id → 1.4 标题` 的声明）。
    **标题重复的条目一律不登记**（宁可少一个别名，不可指向错的表）。
    """
    by_title: dict[str, str] = {}
    ambiguous: set[str] = set()
    for item_id, title in load_item_titles().items():
        for variant in name_variants(title):
            if variant in by_title and by_title[variant] != item_id:
                ambiguous.add(variant)
                continue
            by_title[variant] = item_id
    for variant in ambiguous:
        by_title.pop(variant, None)
    return by_title


@lru_cache(maxsize=1)
def known_table_keys() -> frozenset[str]:
    """`tables/` 里**可能被登记**的键（不含运行期才知道的 OS 层文件名）。

    ★ 为什么要有它：取数点写错名字在运行期**完全静默** —— `ctx.tables.get(名字, [])`
    返回 `[]`，`ctx.table_fetch(...)` 返回未命中的 `TableFetch`，两者都不会报错。
    唯一能提前发现的手段是静态对账，而对账需要一份"合法键"的词表。把词表的计算放在
    **注册逻辑所在的这个文件**里：别名策略改一次，审计工具（`tools/check_oracle_lookup_keys.py`）
    与它的单测就跟着变，不会各算各的一套。

    收录范围 = 2.0 `item_id` ∪ 1.4 标题（含等价写法）∪ `LEGACY_ALIASES` ∪ 各 `item_id` 的尾段别名。
    ⚠ 这是**下界**：尾段别名是否真的登记还要看它在具体包里是否唯一（见 `_load_tables`
    的 `short_counts`），包相关的部分由审计工具在真实包上跑一遍来看。
    """
    titles = load_item_titles()
    keys: set[str] = set(titles)
    keys.update(load_item_ids_by_title())
    keys.update(LEGACY_ALIASES)
    for item_id in titles:
        keys.update(_short_aliases(item_id))
    return frozenset(key for key in keys if key)


def _declared_status(path: Path) -> str:
    """读文件首个非空行拿「采集侧声明的状态」；不是占位形态返回空串。

    ★ 这两个占位形态此前被 `parse_delimited` 当注释丢掉 ⇒ 「查了没有」与
    「没采到」在适配器里就已经混成一样了，下游再怎么判都救不回来。
    """
    try:
        with path.open("r", encoding="utf-8", errors="replace") as stream:
            for line in stream:
                stripped = line.strip()
                if not stripped:
                    continue
                if not stripped.startswith("#"):
                    return ""
                match = _PSV_PLACEHOLDER.match(stripped)
                if match:
                    return match.group(1)
                if stripped.startswith(_LEGACY_EMPTY_MARK):
                    return _EMPTY_STATE
                continue
    except OSError:
        return ""
    return ""


class OraclePackageError(RuntimeError):
    """Oracle 采集包无法安全读取。"""


@dataclass(frozen=True)
class TableFetch:
    """一次取数的结果：**行** + **这项到底采没采**。

    `rows` 单独看是不安全的 —— `ctx.tables.get(名字, [])` 在"名字不对"和"查了 0 行"
    两种情况下都返回 `[]`。`matched` / `state` 就是用来把这两件事分开的。
    """

    rows: list[dict[str, str]] = field(default_factory=list)
    matched: str = ""
    state: str = ""

    @property
    def resolved(self) -> bool:
        """取数键命中了吗（包里存在这一项）。"""
        return bool(self.matched)

    @property
    def available(self) -> bool:
        """这项**采集过**吗。

        判据两条缺一不可：① 键命中（别名对得上）；② 采集侧声明的状态不是"没采到"
        （`empty` = 查了、确认 0 行，**算采集过**）。任一不满足 ⇒ `not_evaluated`，
        报告写「证据不足」，**不许写「检查通过」**。
        """
        return self.resolved and self.state not in _NO_DATA_STATES

    @property
    def counted(self) -> int:
        return len(self.rows)


def fetch_state(fetch: TableFetch) -> str:
    """一次取数 → **采集侧状态**（写进分析产物的那个字符串）。

    `resolved=False` 说明这个键在包里不存在（别名对不上，或这一项本来就没采）⇒ `unavailable`。
    ★ 取数**没命中**与「查了、确认 0 行」(`empty`) 是**相反结论**，这里必须先分开，
      下游才可能把它们写成两句话（批 14 立的「未采集 ≠ 0」在这里才第一次真正可判）。
    """
    if not fetch.resolved:
        return "unavailable"
    return fetch.state or "ok"


def state_is_collected(state: str) -> bool:
    """「采集侧状态」→ **这项采集过吗**。

    `empty`（查了、确认 0 行）**算采集过**；`unavailable` / `timeout` / `permission_denied` 不算。
    ★ 缺状态（`""`）一律按**没采到**处理 —— 宁可写「证据不足」，不可写「检查通过」。
      这与 `TableFetch.available` 是同一个判据，规则层与适配器层因此不会各判一套。
    """
    return state in _DATA_STATES


def prefer_state(*states: str) -> str:
    """多个来源 → 一个采集状态。优先级：**有内容 > 查了没有 > 没采到**。

    用于「同一份事实有两个落点」的取数点（FRA 的 `V$FLASH_RECOVERY_AREA_USAGE` 与
    ASM 版 `V$RECOVERY_AREA_USAGE` 各是一张表）。只要有一张真的查到了内容，就不说没采到。
    """
    known = [state for state in states if state]
    if not known:
        return ""
    for state in known:
        if state in _DATA_STATES and state != _EMPTY_STATE:
            return state
    for state in known:
        if state in _DATA_STATES:
            return state
    return known[0]


@dataclass
class OraclePackageContext(BasePackageContext):
    # 本包按哪套 `tables/` 语义解析（"1" = 1.4，"2" = 2.0）；空串 = 未知，按 2.0 处理
    collector_major: str = ""
    # 每个注册名（item_id / 标题 / 尾段 / legacy 别名）→ 采集侧声明的真实状态
    table_state: dict[str, str] = field(default_factory=dict)

    @property
    def instance_id(self) -> str:
        return str(self.snapshot.get("instance_tag") or self.source.stem)

    def find_table(self, *names: str) -> list[dict[str, str]]:
        for name in names:
            if name in self.tables and self.tables[name]:
                return self.tables[name]
        for name in names:
            lowered = name.lower()
            for key in self.tables:
                if (name in key or lowered in key.lower()) and self.tables[key]:
                    return self.tables[key]
        return []

    def table_fetch(self, *names: str) -> TableFetch:
        """按别名顺序取数，返回 `TableFetch`（行 + 状态 + 命中名）。

        ★ **不走 `find_table` 的子串兜底** —— 那是"取不到就找一个名字像的"，
        实测会把 FRA 使用率的取数点兜到 `快速恢复区（FRA）参数` 上（那张表只有
        NAME/VALUE，没有 `PERCENT_SPACE_USED`）⇒ 命中了**错的表**，指标恒为 None。
        要兜底就在调用点把名字列全，让"名字"这件事保持可审计。
        """
        for name in names:
            if name in self.tables:
                return TableFetch(self.tables[name], name, self.table_state.get(name, ""))
        return TableFetch()


def parse_instance_tag(tag: str) -> tuple[str, str]:
    """'ZYDB_db01_192.168.100.80_1521_zydb1' -> (hostname, ip)."""
    parts = str(tag).split("_")
    hostname, ip_addr = "", ""
    for index, part in enumerate(parts):
        if re.match(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$", part):
            ip_addr = part
            if index >= 1:
                hostname = parts[index - 1]
            break
    return hostname, ip_addr


def _load_collection_status(status_dir: Path) -> dict[str, Any]:
    fields = ["item_id", "category", "status", "started_at", "finished_at",
              "duration_ms", "row_count", "exit_code", "output_file", "reason"]
    items: list[dict[str, Any]] = []
    for file in sorted(status_dir.glob("*.tsv")):
        with file.open("r", encoding="utf-8", errors="replace") as stream:
            reader = csv.reader(stream, delimiter="\t")
            first_row = next(reader, None)
            if first_row is None:
                continue
            first_cell = (first_row[0] or "").strip() if first_row else ""
            has_header = first_cell in {"item_id", "ID"}
            if not has_header:
                items.append(dict(zip(fields, first_row + [""] * len(fields))))
            for row in reader:
                if row:
                    items.append(dict(zip(fields, row[: len(fields)] + [""] * len(fields))))
    return {"items": items}


def _rejoin_wrapped_rows(lines: list[str], delimiter: str) -> list[str]:
    """回并 sqlplus 的**折行续行**（**只对 2.0 的 `.psv` 成立**，调用方用 `repair_wrap=True` opt-in）。

    ★★ 判据为什么在 2.0 上成立（结构化，不是启发式）：2.0 采集会话里设了 `SET NULL '-'`
    —— 真 NULL 会打成 `-`，**空单元格根本不存在**。实测 10-10 包 103 张表里只有 1 行
    首字段为空（`oracle.basic.parameters.psv` 的 `|02.ctl`），正是折行残段：
    `control_files` 的值被 `COLUMN value FORMAT a80` 断成两行，残段被当成了第二条参数。

    ⚠⚠ **1.4 的 `.tsv` 上这条判据不成立，必须关掉**（08-10 包实测，代价很实在）：1.4 的
    `控制文件.tsv` 是
        `STATUS |NAME` / `       |…/control01.ctl` / `       |…/control02.ctl`
    —— 第一列空白是 **NULL 落在定宽列里**，那两行是**真实数据行**。开回并会把两行数据
    并进**表头**（表从 2 行塌成 0 行，报告里 `控制文件路径` 整项变空）。1.4 另有
    `控制文件序列号.tsv`（3→1 行）、`最近 SQL 错误 (近24h).tsv`（15→14 行）同样被误并。
    ⇒ **判据的前提是「采集器版本保证空单元格不存在」，不是「分隔符是 `|`」** ——
    所以它由 `_iter_table_specs` 按版本给出，不按分隔符猜。

    拼回规则：**不加分隔符**。采集侧已把 `WORD_WRAPPED` 全部撤掉（见
    `inspection/oracle_inspection.sh::ora_sql_to_md` 的注释），折行只剩**断词**一种，
    残段就是被打断 token 的后半截，直接续接即可。

    保守边界（对不上就**原样保留**，宁可让异常露出来，也不猜）：
      · 首字段非空 → 正常行；
      · 没有上一行 / `lead` 越出上一行的列数 / 上一行那一格是空的 → 不并。

    形状要点（真实产物的形状，别按"残段只有一列"想）：折行那一列之后**还没打印的列**
    会跟在残段后面 —— 例如
        `undo_retention|<被折断的值前半` / `|值后半|FALSE|MODIFIED`
    所以残段的字段数**可以多于**上一行；续接那一列、其余列按"空位才填"补进去。

    ⚠ 已知边界：折点若恰好落在**空格**上，行尾空格已被 `psv_normalize` 剥掉，
    续接会少一个空格。走向上由采集侧的「宽度 ≥ SQL 截断长度」消除；
    本函数只作**存量包**的兜底。
    """
    if delimiter != "|":
        return lines
    merged_lines: list[str] = []
    for line in lines:
        cells = line.split(delimiter)
        lead = 0
        while lead < len(cells) and not cells[lead].strip():
            lead += 1
        if lead == 0 or not merged_lines:
            merged_lines.append(line)
            continue
        previous = merged_lines[-1].split(delimiter)
        if lead >= len(previous) or not previous[lead].strip():
            merged_lines.append(line)
            continue
        merged = list(previous)
        for index in range(lead, len(cells)):
            cell = cells[index].strip()
            if not cell:
                continue
            if index == lead:
                # 折行那一列：sqlplus 是**断词**折行（采集侧已撤掉 WORD_WRAPPED），直接续接。
                merged[index] = previous[index] + cell
            elif index >= len(merged):
                merged.extend([""] * (index + 1 - len(merged)))
                merged[index] = cell
            elif not merged[index].strip():
                merged[index] = cell
            # else：上一行该列已有值 —— 保守起见保留，不覆盖。
        merged_lines[-1] = delimiter.join(merged)
    return merged_lines


def parse_delimited(path: Path, delimiter: str = "\t", *,
                    repair_wrap: bool = False) -> list[dict[str, str]]:
    """Oracle 表格解析：跳过 sqlplus 分隔线 / 占位行，并去除键/值两侧空白。

    `#` 开头的行一律丢弃 —— 这同时覆盖了批 5 起 `.psv` 的两种占位形态
    （`# oracle-inspection psv-empty …` / `… psv-unavailable …`）：**占位返回空表**，
    "查了没有" 与 "没采到" 的区别去 `status/*.tsv` 的 `status` 列看，不在表格里。

    `repair_wrap=True` 时追加一步**折行续行回并**（见 `_rejoin_wrapped_rows`）。
    ★ **默认关**，不是懒省事：这条回并的判据是「采集器保证空单元格不存在」，
    **只有 2.0 的 `.psv` 成立**；1.4 的 `.tsv` 上开它会**把真实数据行并进表头**
    （08-10 包实测：`控制文件.tsv` 3 行塌成 1 行）。默认关 ⇒ 忘了传参的代价是
    「折行没修」（报告里看得见），不是「数据被静默吃掉」。
    版本 → 是否回并的对应关系由 `_iter_table_specs` 单点给出。
    """
    if not path.exists() or path.stat().st_size == 0:
        return []
    lines: list[str] = []
    with path.open("r", encoding="utf-8", errors="replace") as stream:
        for line in stream:
            stripped = line.rstrip("\n\r")
            if not stripped or stripped.startswith("#"):
                continue
            if all(c in "-| \t" for c in stripped):
                continue
            lines.append(stripped)
    if not lines:
        return []
    if repair_wrap:
        lines = _rejoin_wrapped_rows(lines, delimiter)
    reader = csv.DictReader(lines, delimiter=delimiter)
    result: list[dict[str, str]] = []
    for row in reader:
        cleaned: dict[str, str] = {}
        for key, value in row.items():
            if key is not None:
                clean_key = key.strip()
                clean_value = value.strip() if value else ""
                if clean_key:
                    cleaned[clean_key] = clean_value
        if cleaned:
            result.append(cleaned)
    return result


def parse_csv(path: Path) -> list[dict[str, str]]:
    return parse_delimited(path, ",")


@lru_cache(maxsize=1)
def load_item_titles() -> dict[str, str]:
    """`item_id` → Markdown 标题。由 `tools/gen_oracle_item_titles.py` 从采集脚本生成。"""
    try:
        data = json.loads(_ITEM_TITLES_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(key): str(value) for key, value in data.items()}


def collector_major(snapshot: dict[str, Any]) -> str:
    """取 `collector_version` 的主版本号；优先读 `collector.version` 段（v2.0 起）。"""
    if not isinstance(snapshot, dict):
        return ""
    raw = ""
    collector = snapshot.get("collector")
    if isinstance(collector, dict):
        raw = str(collector.get("version") or "")
    raw = raw or str(snapshot.get("collector_version") or "")
    return raw.split(".", 1)[0].strip()


def _iter_table_specs(tables_dir: Path, major: str) -> list[tuple[Path, str, bool]]:
    """按采集器主版本返回 `(文件, 分隔符, 是否回并折行)` 清单。

    ★ 第三项是**版本属性**，不是分隔符属性：只有 2.0 会话设了 `SET NULL '-'`
    （空单元格不存在），回并折行才安全；1.4 的第一列空白是 NULL 落在定宽列里、
    是**真实数据行**（详见 `_rejoin_wrapped_rows` 的 docstring）。这是全仓库唯一
    把「版本」翻译成「能不能回并」的地方。
    """
    specs: list[tuple[Path, str, bool]] = []
    if major == LEGACY_COLLECTOR_MAJOR:
        # 1.4：`tables/<中文标题>.tsv`，sqlplus 定宽表（`SET COLSEP '|'`）。
        #      无 `SET NULL '-'` 保证 ⇒ 不回并。
        specs = [(path, "|", False) for path in sorted(tables_dir.glob("*.tsv"))]
    else:
        # 2.0：`.psv`（SQL 结果，`|`，**可回并**）+ `.tsv`（真 TAB 表，分隔符不同、天然不触发）
        specs = [(path, "|", True) for path in sorted(tables_dir.glob("*.psv"))]
        specs += [(path, "\t", False) for path in sorted(tables_dir.glob("*.tsv"))]
    return [(path, delimiter, repair) for path, delimiter, repair in specs if path.is_file()]


def _short_aliases(*names: str) -> set[str]:
    """点分 id 的"尾段"别名：`oracle.basic.invalid_objects` → `basic.invalid_objects` / `invalid_objects`。"""
    out: set[str] = set()
    for name in names:
        if "." in name:
            out.add(name.split(".", 1)[1])
            out.add(name.rsplit(".", 1)[-1])
    return {alias for alias in out if alias}


def parse_df_pt(path: Path) -> list[dict[str, str]]:
    """解析 `df -PT` 的列对齐原文（`evidence/filesystems.txt`）。

    `df -PT` 是 POSIX 定宽输出：前 6 列不含空格，**挂载点在第 7 列且自身可含空格**
    → 只按前 6 个空白切分（`split(None, 6)`），末段整体作挂载点。
    列名固定（`Filesystem Type 1024-blocks Used Available Capacity Mounted on`），不依赖原文表头文字。
    """
    if not path.exists() or path.stat().st_size == 0:
        return []
    rows: list[dict[str, str]] = []
    columns = ["Filesystem", "Type", "1024-blocks", "Used", "Available", "USE%", "Mounted on"]
    with path.open("r", encoding="utf-8", errors="replace") as stream:
        for index, raw in enumerate(stream):
            line = raw.rstrip("\n\r")
            if not line.strip():
                continue
            if index == 0:  # 表头行
                continue
            parts = line.split(None, 6)
            if len(parts) < len(columns):
                continue
            values = list(parts)
            values[5] = values[5].rstrip("%")  # `33%` → `33`
            rows.append(dict(zip(columns, values)))
    return rows


def filesystem_type(row: Any) -> Any:
    """取文件系统行的**类型**列，做键名归一。

    `df -PT` 的解析结果（`parse_df_pt`）用 ``Type``；1.4 老包 / 别名表可能换成别的
    拼法。键名归一放在这里（和产生键名的 `parse_df_pt` 同一个文件），是因为规则层
    （`oracle_rules._check_filesystem_usage`）与展示层（`presentation`）都要按同一个
    判据过滤伪文件系统 —— 两边各写一份键名清单，迟早会分叉。
    """
    if not isinstance(row, dict):
        return ""
    for key in ("Type", "FSTYPE", "type", "fstype", "文件系统类型", "类型"):
        value = row.get(key)
        if value not in (None, ""):
            return value
    return ""


class OraclePackageAdapter:
    """识别并加载 Oracle 结构化采集包（1.4 与 2.0 两套 `tables/` 语义）。"""

    def __init__(self, work_root: Path, index: int = 0) -> None:
        self.target = work_root / f"package_{index:03d}"

    @staticmethod
    def detect(source: Path) -> bool:
        if source.is_file() and source.suffix in {".gz", ".tgz", ".tar"}:
            return True
        if source.is_dir() and (source / "snapshot.json").exists():
            return True
        return source.is_file() and source.suffix == ".md"

    def load(self, source: Path) -> OraclePackageContext:
        if self.target.exists():
            shutil.rmtree(self.target)
        if source.is_file() and source.suffix in {".gz", ".tgz", ".tar"}:
            root = safe_extract_tar(source, self.target)
        elif source.is_dir() and (source / "snapshot.json").exists():
            root = source
        else:
            raise OraclePackageError(f"无法识别的 Oracle 采集包: {source}")

        snapshot = read_json(root / "snapshot.json")
        manifest = read_json(root / "manifest.json") if (root / "manifest.json").exists() else {}
        status_dir = root / "status"
        status = _load_collection_status(status_dir) if status_dir.exists() else {}
        integrity = self.validate_manifest(root, manifest)
        major = collector_major(snapshot)

        ctx = OraclePackageContext(
            source=source, root=root, snapshot=snapshot, status=status,
            manifest=manifest, integrity=integrity, collector_major=major,
        )
        self._load_tables(root, ctx, major)
        for path in (root / "timeseries").glob("*.csv"):
            if path.is_file():
                ctx.timeseries[path.stem] = parse_csv(path)
        for path in (root / "history").glob("*.csv"):
            if path.is_file():
                ctx.history[path.stem] = parse_sadf(path)
        return ctx

    @staticmethod
    def _load_tables(root: Path, ctx: OraclePackageContext, major: str) -> None:
        """把 `tables/` 读进 `ctx.tables`，并把每张表注册到多个别名下。

        ★ 别名策略（下游是按 1.4 的中文标题写的，不改下游就得在这里桥接）：
        `item_id`（`oracle.basic.users`）、**中文标题**（`用户列表`）、尾段（`basic.users` / `users`，
        仅当全局唯一时注册，避免 `usage` / `disk_detail` 这类互相覆盖）、以及少数
        **1.4→2.0 改名的 legacy 键**（见 `LEGACY_ALIASES`）。
        `item_id` 不靠文件名猜 —— 优先用 `status/*.tsv` 的 `output_file` 列反查
        （2.0 的 `tables/kernel_parameters.tsv` 对应的 `item_id` 是 `system.sysctl_selected`，文件名与 id 不同名）。
        """
        titles = load_item_titles()
        by_title = load_item_ids_by_title()
        file_to_item: dict[str, str] = {}
        declared: dict[str, str] = {}
        for item in ctx.status.get("items", []) or []:
            output = str(item.get("output_file") or "")
            item_id = str(item.get("item_id") or "")
            if item_id:
                declared[item_id] = str(item.get("status") or "")
            if item_id and output.startswith("tables/"):
                file_to_item[output[len("tables/"):]] = item_id

        entries: list[tuple[Path, str, bool, str, str, str]] = []
        tables_dir = root / "tables"
        if tables_dir.is_dir():
            for path, delimiter, repair_wrap in _iter_table_specs(tables_dir, major):
                item_id = file_to_item.get(path.name) or path.stem
                if major == LEGACY_COLLECTOR_MAJOR and item_id == path.stem:
                    # 1.4：`status/*.tsv` 是模块级的，反查不到逐项 item_id，
                    # 于是 `item_id` 一直是中文文件名本身 ⇒ **按 2.0 item_id 写的取数点
                    # 在 1.4 包上全部落空且静默**。用 `item_titles.json` 的反向索引补出来。
                    item_id = by_title.get(path.stem) or item_id
                state = declared.get(item_id) or _declared_status(path)
                if not state and major != LEGACY_COLLECTOR_MAJOR:
                    state = "ok"
                entries.append((path, delimiter, repair_wrap, path.stem, item_id, state))

        short_counts: Counter[str] = Counter()
        for _, _, _, stem, item_id, _ in entries:
            short_counts.update(_short_aliases(stem, item_id))

        primary: list[tuple[set[str], list[dict[str, str]], str]] = []
        aliases: list[tuple[set[str], list[dict[str, str]], str]] = []
        for path, delimiter, repair_wrap, stem, item_id, state in entries:
            rows = parse_delimited(path, delimiter, repair_wrap=repair_wrap)
            primary.append(({stem, item_id}, rows, state))
            extra: set[str] = set()
            title = titles.get(item_id)
            if title:
                extra.add(title)
            for alias in _short_aliases(stem, item_id):
                if short_counts[alias] == 1:
                    extra.add(alias)
            for legacy, target in LEGACY_ALIASES.items():
                if target == item_id:
                    extra.add(legacy)
            aliases.append((extra, rows, state))

        # 主键先落，别名只补空缺（主键之间不会撞：文件名与 item_id 各自唯一）
        for names, rows, state in primary:
            for name in names:
                if name:
                    ctx.tables[name] = rows
                    ctx.table_state[name] = state
        for names, rows, state in aliases:
            for name in names:
                if name and name not in ctx.tables:
                    ctx.tables[name] = rows
                    ctx.table_state[name] = state

        # `evidence/*.txt` 里那项"列对齐文本"（N-8 落点变更）—— 已声明的一项回填
        for item_id, (filename, kind) in _EVIDENCE_TABLES.items():
            path = root / "evidence" / filename
            if not path.is_file() or item_id in ctx.tables:
                continue
            rows = parse_df_pt(path) if kind == "df_pt" else []
            names = {item_id, item_id.rsplit(".", 1)[-1]}
            title = titles.get(item_id)
            if title:
                names.add(title)
            state = declared.get(item_id) or ("ok" if rows else "")
            for name in names:
                if name:
                    ctx.tables[name] = rows
                    ctx.table_state[name] = state

    @staticmethod
    def validate_manifest(root: Path, manifest: dict[str, Any]) -> dict[str, Any]:
        if not manifest:
            return {"status": "ok", "files_checked": 0, "failure_count": 0, "failures": []}
        failures: list[dict[str, Any]] = []
        checked = 0
        for item in manifest.get("files", []):
            relative = str(item.get("path", ""))
            if not relative or not is_safe_member(relative):
                failures.append({"path": relative, "reason": "invalid_path"})
                continue
            path = root / relative
            if not path.exists() or not path.is_file():
                failures.append({"path": relative, "reason": "missing"})
                continue
            checked += 1
            expected_size = safe_int(item.get("size_bytes"))
            if expected_size is not None and path.stat().st_size != expected_size:
                failures.append({"path": relative, "reason": "size_mismatch"})
                continue
            expected_hash = str(item.get("sha256", ""))
            if expected_hash and sha256_file(path) != expected_hash:
                failures.append({"path": relative, "reason": "sha256_mismatch"})
        return {
            "status": "ok" if not failures else "failed",
            "files_checked": checked,
            "failure_count": len(failures),
            "failures": failures,
        }

    @staticmethod
    def collection_quality(ctx: OraclePackageContext) -> dict[str, Any]:
        items = ctx.status.get("items", [])
        counts: dict[str, int] = {}
        non_ok: list[dict[str, Any]] = []
        weights = {
            "ok": 1.0, "empty": 1.0, "not_applicable": 1.0,
            "unsupported": 0.8, "not_enabled": 0.8, "partial": 0.6,
            "permission_denied": 0.2, "timeout": 0.0, "error": 0.0, "skipped": 0.5,
        }
        earned = 0.0
        for item in items:
            status = str(item.get("status", "unknown"))
            counts[status] = counts.get(status, 0) + 1
            earned += weights.get(status, 0.0)
            if status not in {"ok", "empty", "not_applicable"}:
                non_ok.append({
                    "item_id": item.get("item_id", item.get("ID", "")),
                    "status": status,
                    "reason": item.get("reason", ""),
                    "duration_ms": item.get("duration_ms", ""),
                })
        total = len(items)
        score = round((earned / total * 100) if total else 0.0, 1)
        grade = "good" if score >= 80 else "warning" if score >= 60 else "critical"
        limitations = [
            f"采集项 {item['item_id']} 状态 {item['status']}: {item['reason'] or '无详情'}"
            for item in non_ok
            if item["status"] in {"permission_denied", "timeout", "error"}
        ]
        return {
            "score": score,
            "grade": grade,
            "status_counts": counts,
            "integrity": ctx.integrity,
            "limitations": limitations,
            "non_ok_items": non_ok,
        }
