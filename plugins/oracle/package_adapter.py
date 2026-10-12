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
from dataclasses import dataclass
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


class OraclePackageError(RuntimeError):
    """Oracle 采集包无法安全读取。"""


@dataclass
class OraclePackageContext(BasePackageContext):
    # 本包按哪套 `tables/` 语义解析（"1" = 1.4，"2" = 2.0）；空串 = 未知，按 2.0 处理
    collector_major: str = ""

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
        file_to_item: dict[str, str] = {}
        for item in ctx.status.get("items", []) or []:
            output = str(item.get("output_file") or "")
            item_id = str(item.get("item_id") or "")
            if item_id and output.startswith("tables/"):
                file_to_item[output[len("tables/"):]] = item_id

        entries: list[tuple[Path, str, bool, str, str]] = []
        tables_dir = root / "tables"
        if tables_dir.is_dir():
            for path, delimiter, repair_wrap in _iter_table_specs(tables_dir, major):
                item_id = file_to_item.get(path.name) or path.stem
                entries.append((path, delimiter, repair_wrap, path.stem, item_id))

        short_counts: Counter[str] = Counter()
        for _, _, _, stem, item_id in entries:
            short_counts.update(_short_aliases(stem, item_id))

        primary: list[tuple[set[str], list[dict[str, str]]]] = []
        aliases: list[tuple[set[str], list[dict[str, str]]]] = []
        for path, delimiter, repair_wrap, stem, item_id in entries:
            rows = parse_delimited(path, delimiter, repair_wrap=repair_wrap)
            primary.append(({stem, item_id}, rows))
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
            aliases.append((extra, rows))

        # 主键先落，别名只补空缺（主键之间不会撞：文件名与 item_id 各自唯一）
        for names, rows in primary:
            for name in names:
                if name:
                    ctx.tables[name] = rows
        for names, rows in aliases:
            for name in names:
                if name and name not in ctx.tables:
                    ctx.tables[name] = rows

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
            for name in names:
                if name:
                    ctx.tables[name] = rows

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
