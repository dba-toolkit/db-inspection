"""PostgreSQL presentation and legacy report-model builder.

Converts facts, metrics and rule evaluations into the legacy PG report
envelope.  It does not read archives, calculate metrics, run rules, render
charts or write files.
"""

from __future__ import annotations

import math
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from inspection_core.collection_state import (
    expand_source_names,
    is_no_data,
    merge_section_state,
)
from inspection_core.models import PackageContext
from inspection_core.statistics import summarize
from inspection_core.system_checks import disk_media, is_persistent_fstype
from inspection_core.values import safe_float, safe_int
from .metrics import count_stale_tables, database_max_xid_age, sum_dead_tuples
from .parsers import (
    _parse_df_pt,
    _parse_free_b,
    parse_capabilities,
    parse_ip_addr,
    parse_ip_route,
    parse_numa,
    parse_postgres_processes,
    parse_socket_summary,
)
from .rules import Finding, RuleEvaluation


# ---------------------------------------------------------------------------
# 数据库版本口径（单一来源）
# ---------------------------------------------------------------------------
#
# `version()`/`server_version` 返回的是一整段构建横幅：
#   `PostgreSQL 17.7 on x86_64-pc-linux-gnu, compiled by gcc (GCC) 8.5.0 …  64-bit`
# 直接塞进封面「数据库版本」格子里会把表格撑破（实测 66 字符），也不符合
# MySQL 侧「MySQL 8.0.30」的呈现口径。这里统一压成 `PostgreSQL 17.7`，
# 构建信息另行收进 3.1 的「说明」列，不丢证据。
#
# 只压这一处：封面、2.1 概述、3.1 明细、3.1 结论共用本函数，避免出现
# 「封面短、正文长」的同名两值。
_PG_VERSION_PREFIX = "PostgreSQL "
_PG_VERSION_RE = re.compile(r"^\s*PostgreSQL\s+([0-9][0-9A-Za-z._-]*)")
_PG_ARCH_RE = re.compile(r"^\s*PostgreSQL\s+\S+\s+on\s+([^\s,]+)")
_PG_GCC_RE = re.compile(r"\bgcc\s+\(GCC\)\s*([0-9][0-9._]*)")
_PG_BITS_RE = re.compile(r"(\d+)-bit\b")


def short_pg_version(banner: Any) -> str:
    """`PostgreSQL 17.7 on x86_64-…, compiled by gcc …` → `PostgreSQL 17.7`。

    识别不了就原样返回（fail-safe：宁可长，也不改名）。
    """
    raw = str(banner or "").strip()
    if not raw:
        return ""
    matched = _PG_VERSION_RE.match(raw)
    return f"{_PG_VERSION_PREFIX}{matched.group(1)}" if matched else raw


def pg_build_note(banner: Any) -> str:
    """从构建横幅里挑出对排障有用的三件事：架构 / 编译器 / 位宽。"""
    raw = str(banner or "").strip()
    if not raw:
        return ""
    parts: list[str] = []
    if (matched := _PG_ARCH_RE.match(raw)):
        parts.append(matched.group(1))
    if (matched := _PG_GCC_RE.search(raw)):
        parts.append(f"gcc {matched.group(1)}")
    if (matched := _PG_BITS_RE.search(raw)):
        parts.append(f"{matched.group(1)}-bit")
    return " / ".join(parts)


# ---------------------------------------------------------------------------
# 展示条目 → 采集台账（**两套命名空间，必须显式声明**）
# ---------------------------------------------------------------------------
#
# 展示层按**报告章节**组织（`pg.performance` 一章含 Buffer 命中率、bgwriter 与若
# 干参数），采集台账按**采集项**登记（`pg.database_stats` / `pg.bgwriter` /
# `pg.settings`）。两套 id 不同域，且实测**有 5 个章节 id 与台账的 category 同名**
# （`pg.performance` 不是 item_id，却又是 category）—— 所以"顺手用章节 id 查台账"
# 不但查不到，还可能静默取到另一批项的状态。
#
# 本表是这层映射的**唯一来源**。新增展示条目必须同步登记，否则
# `collection.status` 落 `unmapped`（报告印「未登记」），**不会**默认「正常」。
#
# 值支持 `db.*.table_health` 这类通配：逐库采集的项在台账里一条一个库。
PG_SECTION_SOURCES: dict[str, tuple[str, ...]] = {
    "pg.instance": ("pg.instance", "pg.databases"),
    # B1：`capabilities.tsv` 有 17 个能力键，报告原先只消费 2 个
    # （`pg_stat_statements` / `sar_available`），其余 15 个（`wal_level` /
    # `archive_mode` / `is_in_recovery` / `pg_stat_io` …）采了没人看。
    "pg.capability_probe": ("pg.capability_probe",),
    "system.host": ("system.lscpu", "system.os_release", "system.numa",
                    "system.block_devices", "system.free"),
    "system.time": ("system.time_status", "system.timedatectl",
                    "system.chronyc_tracking", "system.chronyc_sources",
                    "system.ntpq_peers"),
    # B1：这三项采集台账一直 `ok`，报告原先一次没读。聚合在一个展示条目里
    # （接口 / 路由 / 套接字本来就是同一台机器的同一件事），但**三个采集项都要
    # 登记**——只登记 `system.ip_address`，另两项的 `unsupported` 就没人看得见。
    "system.ip_address": ("system.ip_address", "system.ip_route", "system.socket_summary"),
    "system.postgres_processes": ("system.postgres_processes",),
    "system.kernel": ("system.sysctl_selected",),
    "system.mounts": ("system.mounts",),
    "system.dmesg_errors": ("system.dmesg_errors",),
    "system.filesystems": ("system.filesystems", "system.inodes"),
    "system.hugepages": ("system.hugepages",),
    "pg.connections": ("pg.connections", "pg.session_states",
                       "pg.long_transactions", "pg.lock_waits"),
    "pg.space": ("pg.databases", "pg.tablespaces",
                 "db.*.large_tables", "db.*.bloat_estimation"),
    "pg.performance": ("pg.database_stats", "pg.bgwriter", "pg.settings"),
    "pg.io": ("pg.stat_io", "system.iostat"),
    "pg.wal": ("pg.stat_wal", "pg.checkpointer", "pg.control_checkpoint"),
    "pg.vacuum": ("db.*.table_health", "pg.database_ages"),
    "pg.replication": ("pg.stat_replication", "pg.replication_slots", "pg.archiver",
                       "pg.database_conflicts"),
    "pg.indexes": ("db.*.indexes", "db.*.unused_indexes", "db.*.duplicate_indexes",
                   "db.*.missing_fk_indexes"),
    "pg.security": ("pg.hba_rules", "pg.roles"),
    "pg.backup": ("backup.barman", "backup.pgbackrest",
                  "system.backup_cron", "system.backup_timers"),
    "pg.logs": ("pg.log_summary",),
    "pg.settings": ("pg.settings",),
    "pg.objects": ("pg.object_counts", "db.*.partition_summary"),
}


# ---------------------------------------------------------------------------
# 「说明」列口径 —— 模块级**单一来源**
# ---------------------------------------------------------------------------
# 「说明」列回答的是「这一行是什么、该怎么读」。MySQL 报告每一行都填，PG 原先大量
# 留空（`说明=""`），读者只能看到一个裸数字。口径统一收在这里，便于审阅与断言，
# 不允许再散落到各个 `_row(...)` 里现场手写。
SETTING_NOTES: dict[str, str] = {
    "shared_buffers": "共享缓冲区大小（以 8KB 页计）",
    "effective_cache_size": "优化器对 OS 缓存可复用量的估计（不占内存）",
    "work_mem": "单个排序/哈希操作的内存上限",
    "maintenance_work_mem": "VACUUM、索引维护等维护操作的内存上限",
    "wal_level": "WAL 记录级别；replica 及以上才支持复制",
    "archive_mode": "WAL 归档开关；PITR 依赖它",
    "autovacuum": "自动清理总开关；关闭会导致表膨胀",
    "max_connections": "允许的最大并发连接数",
    "max_wal_size": "两次检查点之间 WAL 增长上限；过小会提高检查点频率",
    "min_wal_size": "WAL 文件收缩的下限",
    "checkpoint_timeout": "定时检查点的最长间隔",
    "autovacuum_max_workers": "autovacuum 工作进程上限",
    "autovacuum_naptime": "autovacuum 巡检间隔",
    "idle_in_transaction_session_timeout": "空闲事务超时；过长会长期把持事务阻塞 VACUUM",
    "statement_timeout": "单条语句执行超时",
    "log_min_duration_statement": "慢 SQL 记录阈值",
    "password_encryption": "口令加密算法",
}

#: `capabilities.tsv` 的能力键 → 说明。**17 个键必须全覆盖**：能力表是"这个实例
#: 支持什么"的正面清单（哪些统计视图可用、复制/归档能力如何），键名本身不带语义，
#: 少一条说明就多一行看不懂的 `pg_stat_io=1`。
#: 守门 `CapabilityRemarkTests` 断言实际键集 ⊆ 本表键集 —— 漏键不抛异常、测试也
#: 不会自己红，只能靠这条断言兜住。
CAPABILITY_NOTES: dict[str, str] = {
    "pg_version": "版本横幅原文；含编译环境与位宽",
    "pg_version_num": "数值化版本号（170007 = 17.7），便于脚本比较",
    "pg_major": "主版本号；参数与视图差异按它分支",
    "data_directory": "PGDATA 路径；备份范围与磁盘容量以它为准",
    "is_in_recovery": "t=备库（只读），f=主库；决定能否接受写入",
    "wal_level": "replica/logical 才支持复制与逻辑解码",
    "archive_mode": "on=开启 WAL 归档；PITR 依赖它",
    "max_replication_slots": "复制槽上限；0 表示未配置复制槽",
    "max_wal_senders": "WAL 发送进程上限；0 表示无法流复制",
    "pg_stat_statements": "1=库可用；0 表示 SQL 级统计不可用",
    "pg_stat_statements_extension": "1=已建扩展；0 表示视图在但未 CREATE EXTENSION",
    "pg_stat_wal": "1=PG14+ 的 pg_stat_wal 视图可用",
    "pg_stat_io": "1=PG16+ 的 pg_stat_io 视图可用",
    "pg_stat_checkpointer": "1=PG17+ 的 pg_stat_checkpointer 视图可用",
    "replication_active": "1=存在活跃复制连接；0 表示无复制流量",
    "sar_command": "1=sar 可用，可采历史负载",
    "sadf_command": "1=sadf 可用，可解析 SAR 二进制",
}

OBJECT_NOTES: dict[str, str] = {
    "table": "普通表",
    "partitioned_table": "声明式分区父表",
    "foreign_table": "外部表（FDW）",
    "materialized_view": "物化视图",
    "view": "视图",
    "index": "索引",
    "sequence": "序列（自增/取值对象）",
    "trigger": "触发器",
    "function": "函数",
    "extension": "已安装扩展",
}

LOG_CATEGORY_NOTES: dict[str, str] = {
    "PANIC": "服务级致命错误，通常伴随重启",
    "FATAL": "会话级致命错误，常见于认证失败或连接中断",
    "ERROR": "语句级错误",
    "DEADLOCK": "死锁检测",
    "OOM": "内存不足",
    "WRITE_FAILURE": "写盘失败",
    "FREQUENT_CHECKPOINT": "检查点频繁告警",
}

# 健康分口径 —— 与 `health_summary()` **同源**：改这里就等于改分数，不许两处各写一份。
# 归一后的档位是 high/medium/low；PG 的原始档位 critical/warning/info 一一对应。
SCORE_BASE = 100
SCORE_PENALTY: dict[str, int] = {"high": 25, "medium": 10, "low": 3}


def _state_word(state: Any) -> str:
    """采集侧状态 → 中文词。

    词表**只有一份**（`inspection_core.word_engine.STATUS_DISPLAY`）。延迟 import
    是刻意的：本模块在 import 期不该拉进 python-docx（引擎才需要它）。
    """
    from inspection_core.word_engine import STATUS_DISPLAY

    raw = str(state or "")
    return STATUS_DISPLAY.get(raw.lower(), raw)


def _bracket_value(raw: Any) -> str:
    """`/sys` 三态写法取当前生效值：`always madvise [never] ` → `never`。

    直接从内核文件抄来的字符串**同时列出三种取值**，只带方括号的那个是当前值。
    原样印给客户看会被读成"三种都开着"；取不出来就返回空串（不猜）。
    """
    text = str(raw or "")
    matched = re.search(r"\[([^\]]+)\]", text)
    if matched:
        return matched.group(1).strip()
    return text.strip().split()[0] if text.strip() and text.strip().split()[0] in {
        "always", "madvise", "never"
    } else ""


def _parse_inode_rows(path: Path) -> list[dict[str, Any]]:
    """`df -i` 输出 → 持久化文件系统的 inode 使用情况（按使用率降序）。

    空间没满而 inode 先满是生产上常见故障（小文件多、临时表多、WAL 段没清），
    所以这一维必须报出来。判据同样走公共层 `is_persistent_fstype`（`df -i` 的第一列
    对真实挂载是设备名、对伪文件系统是类型名，两边都能判）。
    """
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        parts = raw.split()
        if len(parts) < 6 or parts[4] == "IUse%":
            continue
        if not is_persistent_fstype(parts[0]):
            continue
        pct = safe_float(parts[4].rstrip("%"))
        rows.append({
            "文件系统": parts[0],
            "inode 总数": parts[1],
            "已用": parts[2],
            "使用率": parts[4],
            "使用率数值": pct if pct is not None else 0.0,
            "挂载点": " ".join(parts[5:]),
        })
    rows.sort(key=lambda r: r.get("使用率数值", 0.0), reverse=True)
    return rows


#: iostat 设备表里按**物理盘口径**排除的名称（见 `docs/os-collector-crossdb-alignment.md` §2.2）：
#: LVM 让同一笔 I/O 在 `dm-N` 与 `sdX` 各记一次，报告的判据是"磁盘利用率过高"，
#: 能扩容/能换的是物理盘 ⇒ 排除设备映射层。光驱（`sr*`/`scd*`）一并排除。
_IOSTAT_EXCLUDED_PREFIXES = ("dm-", "sr", "scd")


def _parse_iostat_devices(path: Path, limit: int = 6) -> list[dict[str, Any]]:
    """`iostat` 快照文本 → 每台物理盘的**现场峰值**（`%util` / `await` / tps）。

    快照里同一设备会出现多次（`iostat -xz 1 3` 多组采样），取**峰值**而不是均值：
    均值会把"某一秒打满"抹平，而那正是要看的。
    """
    if not path.exists():
        return []
    agg: dict[str, dict[str, Any]] = {}
    columns: list[str] | None = None
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        parts = raw.split()
        if not parts:
            continue
        if parts[0] == "Device":
            columns = parts
            continue
        if columns is None or len(parts) != len(columns):
            continue
        record = dict(zip(columns, parts))
        device = str(record.get("Device") or "")
        if not device or device.startswith(_IOSTAT_EXCLUDED_PREFIXES):
            continue
        reads = safe_float(record.get("r/s")) or 0.0
        writes = safe_float(record.get("w/s")) or 0.0
        await_ms = max(safe_float(record.get("r_await")) or 0.0,
                       safe_float(record.get("w_await")) or 0.0)
        util = safe_float(record.get("%util")) or 0.0
        slot = agg.setdefault(device, {"device": device, "tps": 0.0, "await": 0.0, "util": 0.0})
        slot["tps"] = max(slot["tps"], reads + writes)
        slot["await"] = max(slot["await"], await_ms)
        slot["util"] = max(slot["util"], util)
    return sorted(agg.values(), key=lambda r: r["util"], reverse=True)[:limit]


def collector_status_index(ctx: PackageContext) -> dict[str, dict[str, Any]]:
    """采集台账 `item_id` → 该登记项。**只从 `ctx.status` 取**。

    ★ 不要再写 `ctx.snapshot["collection_status"]` —— 该键**从不曾存在**（台账是
      包顶层的 `collection_status.json`，由 `package_adapter` 读进 `ctx.status`）。
      写错就是静默的：永远拿到 `{}`，然后落回"按渲染行数猜"。
    """
    index: dict[str, dict[str, Any]] = {}
    for raw in ctx.status.get("items", []) or []:
        item_id = str(raw.get("item_id") or "")
        if item_id:
            index[item_id] = dict(raw)
    return index


def section_collection(ctx: PackageContext, item_id: str, *, rendered_rows: int = 0) -> dict[str, Any]:
    """报告章节的采集状态 —— **唯一来源是采集台账**。

    改动前这里是 `ctx.snapshot.get("collection_status", {})` 恒空 ⇒ 落下游
    `ok if rows else empty`，即**拿渲染行数当采集状态**。真机实测后果：顶层 9 项
    （`pg.table_health` / `pg.indexes` / `pg.sequences` …）在台账里记 `skipped`
    （原因"改由逐库采集"），报告照印「采集状态：正常」。

    ★ 返回给报告的 `status` **不等于**台账的归并原值，两者刻意分开：

      · 报告「采集状态」列的问法是"**这一节能不能看分析结论**"，引擎的语义是
        "非 ok 就压过分析结论"（`word_engine._evidence_index_rows`）。
      · `empty`（查了、确实没有）与 `partial`（部分有证据）都属于"有证据 ⇒ 结论
        有效"，若原样透出，`partial` 会把该节的 `risk` 结论盖成「部分可用」——
        等于用"证据不全"抹掉"发现问题"。
      · 所以这两档归成 `ok`，**缺口明细走 `reason`**（正文元信息行会印）
        **与附录「数据缺口」表**——那才是缺口该出现的地方。

    ★ 但**归并原值**必须单独带出去（返回值里的 `gap_count`）：元信息行直接印 `status`
      会出现「采集状态：正常；说明：本节部分采集项未取得…」这种同行打架的文案。
      渲染层据 `gap_count` 改写**展示词**（`word_engine.GAP_STATUS_TEMPLATE`），
      引擎用值保持不变。

    章节级归并语义见 `inspection_core.collection_state.merge_section_state`。
    """
    index = collector_status_index(ctx)
    names = PG_SECTION_SOURCES.get(item_id, ())
    keys = expand_source_names(index, names)
    states = [str(index[key].get("status") or "") for key in keys]
    merged = merge_section_state(states)
    missing = [key for key in keys if is_no_data(str(index[key].get("status") or ""))]
    gap_detail = "、".join(f"{key}（{_state_word(index[key].get('status'))}）" for key in missing)

    if not names:
        reason, status = "报告层未登记该章节的采集来源", merged
    elif not keys:
        reason = "采集台账未登记该章节的采集项（" + "、".join(names) + "）"
        status = merged
    elif is_no_data(merged):
        reason, status = ("未取得：" + gap_detail) if gap_detail else "", merged
    else:
        # ★ 这里**不再**写"本节部分采集项未取得"——那句话已经由状态词
        #   （`gap_count` ⇒ 「部分（N 项未取得）」）说过了，重述一遍会变成
        #   「采集状态：部分（3 项未取得）；说明：本节部分采集项未取得，…」。
        #   说明列只留"结论还作不作数"和"缺哪几项"。
        reason = ("结论仍基于其余已取得证据；缺口：" + gap_detail) if gap_detail else ""
        status = "ok"

    # 「原始记录」条数取**采集台账自报**的 row_count，不再用渲染行数 —— 章节表格
    # 大多是代码合成的（`pg.vacuum` 恒 4 行、`pg.instance` 恒 10 行），拿它当"原始记录"
    # 会说出与源表不符的数字（真机实测：9.1 印 4 条，而源表只有 2 行）。
    # ★ 只在**同一个证据文件**（同 stem）时给这个数：跨异构源求和会得出无意义的数字
    #   （`system.host` 把 lscpu 22 行 + block_devices 7 行 + free 3 行加成"60 条"），
    #   那种"看着精确其实不是一回事"的数字比不显示更坏。多源章节**不显示**这一行。
    stems = {str(index[key].get("output_file") or "").rsplit("/", 1)[-1] for key in keys}
    reported = [safe_int(index[key].get("row_count")) for key in keys]
    reported = [n for n in reported if n is not None]
    row_count: int | None = sum(reported) if (len(stems) == 1 and reported) else None
    return {
        "status": status,
        "row_count": row_count,
        "reason": reason,
        # ★ **展示用**缺口数（引擎用值仍是 `status`，两者刻意分开）。
        #   背景：`status` 把 `partial` 归成 `ok` 是为了**保住该节的分析结论**
        #   —— 引擎语义是"非 ok 就压过分析结论"（`word_engine._evidence_index_rows`），
        #   原样透出 `partial` 会把该节的 `risk` 盖成「部分可用」，等于用"证据不全"
        #   抹掉"发现问题"。这个归并本身是对的。
        #   但它有个副作用：元信息行直接印 `status` ⇒ 出现
        #   「采集状态：正常；说明：本节部分采集项未取得…」—— **同一行前半句和后半句打架**，
        #   只看前半句的人会以为证据齐了（真机实测 4.2 时间与时区、13.1 备份两处，
        #   备份那节 barman 与 pgbackrest **两个都不支持**，等于整块证据为空却印「正常」）。
        #   ⇒ 把缺口数单独带给渲染层，让**展示词**与**引擎值**各归各位。
        "gap_count": len(missing),
    }


class PostgreSQLPresentationBuilder:
    def __init__(self, analyzer_version: str, analysis_schema: str, clock: Callable[[], str]) -> None:
        self.analyzer_version = analyzer_version
        self.analysis_schema = analysis_schema
        self.clock = clock

    def health_summary(self, findings: list[Finding]) -> dict[str, Any]:
        critical = sum(1 for f in findings if f.severity == "critical")
        warning = sum(1 for f in findings if f.severity == "warning")
        info = sum(1 for f in findings if f.severity == "info")
        score = max(0, SCORE_BASE - critical * SCORE_PENALTY["high"]
                   - warning * SCORE_PENALTY["medium"] - info * SCORE_PENALTY["low"])
        if score >= 90:
            grade = "A"
        elif score >= 75:
            grade = "B"
        elif score >= 60:
            grade = "C"
        else:
            grade = "D"
        return {"score": score, "grade": grade, "critical": critical, "warning": warning, "info": info}

    def build_inspection_model(self, ctx: PackageContext) -> dict[str, Any]:
        """Build rich inspection model matching MySQL's _item pattern.

        Each item includes: source, collection quality, display rows, and analysis.
        """
        tables = ctx.tables
        settings = ctx.settings
        ident = ctx.snapshot.get("instance_identity", {})
        host = ctx.snapshot.get("host_identity", {})
        role = ctx.snapshot.get("role_evidence", {})
        root = ctx.root

        sections: list[dict[str, Any]] = []

        def _fmt_bytes(b: Any) -> str:
            n = safe_float(b)
            if n is None:
                return "N/A"
            for unit in ("B", "KB", "MB", "GB", "TB"):
                if n < 1024:
                    return f"{n:.1f} {unit}"
                n /= 1024
            return f"{n:.1f} PB"

        def _fmt_pct(v: Any, digits: int = 1) -> str:
            n = safe_float(v)
            return f"{n:.{digits}f}%" if n is not None else "N/A"

        def _row(**kwargs: Any) -> dict[str, Any]:
            return dict(kwargs)

        def _item(item_id: str, title: str, source: str, rows: list[dict[str, Any]],
                  conclusion: str, *, status: str = "normal",
                  recommendation: str = "", evidence: list[str] | None = None,
                  note: str = "", coll_override: dict | None = None) -> dict[str, Any]:
            return {
                "item_id": item_id,
                "title": title,
                "source": source,
                # 采集状态**只从台账取**（见 `section_collection`）—— 此前按渲染
                # 行数猜，台账记 `skipped` 的项在报告里印「正常」。
                "collection": coll_override or section_collection(ctx, item_id, rendered_rows=len(rows)),
                "display": {"type": "table", "rows": rows, "note": note},
                "analysis": {
                    "status": status,
                    "conclusion": conclusion,
                    "evidence": evidence or [],
                    "recommendation": recommendation,
                },
            }

        def _add_section(section_id: str, title: str, items: list[dict[str, Any]]) -> None:
            sections.append({"section_id": section_id, "title": title, "items": items})

        # === Pre-parse shared data ===
        fs_rows = _parse_df_pt(ctx.root / "tables" / "filesystems.tsv")
        # 判据走公共层 `is_persistent_fstype`（与挂载参数、MySQL / Oracle 同源），
        # 本地不再维护第二份黑名单——原先那份只有 5 个类型，漏了 sysfs / proc /
        # cgroup / hugetlbfs / fuse，一旦 `df` 把伪文件系统列出来就会漏网。
        real_fs = [r for r in fs_rows if is_persistent_fstype(r.get("fstype"))]
        fs_excluded = len(fs_rows) - len(real_fs)
        skipped_100s = [
            r for r in fs_rows
            if not is_persistent_fstype(r.get("fstype")) and (r.get("usage_percent") or 0) >= 95
        ]

        instance_rows = tables.get("instance", [])
        inst = instance_rows[0] if instance_rows else {}
        db_rows = tables.get("databases", [])
        mem_snap = _parse_free_b(ctx.root / "tables" / "memory_snapshot.tsv")
        dbstats = tables.get("database_stats", [])

        def _colon_map(path: Path) -> dict[str, str]:
            if not path.exists():
                return {}
            result: dict[str, str] = {}
            for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
                if ":" not in raw:
                    continue
                name, value = raw.split(":", 1)
                result[name.strip()] = value.strip()
            return result

        def _key_value_rows(path: Path) -> list[dict[str, Any]]:
            if not path.exists():
                return []
            rows: list[dict[str, Any]] = []
            for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                if "\t" in line:
                    name, value = line.split("\t", 1)
                elif "=" in line:
                    name, value = line.split("=", 1)
                else:
                    continue
                rows.append(_row(参数=name.strip(), 值=value.strip()))
            return rows

        def _key_value_map(path: Path) -> dict[str, str]:
            """`key<TAB>value` / `key=value` 表 → 字典（`hugepages.tsv` 这类两列表）。"""
            return {str(r.get("参数") or ""): str(r.get("值") or "")
                    for r in _key_value_rows(path)}

        def _mount_rows(path: Path) -> tuple[list[dict[str, Any]], int]:
            """只返回**持久化**挂载点，同时给出被排除的条数。

            判据来自公共层 `is_persistent_fstype`（与 MySQL / Oracle 同源）。
            原先本地不维护任何黑名单 ⇒ `/proc`、`/sys`、各 cgroup、tmpfs、
            hugetlbfs、fuse、光驱共 37 条噪声全进报告，客户看到 39 行里只有 2
            行是真实块设备，第一反应是"这报告没做过滤"。
            """
            if not path.exists():
                return [], 0
            rows: list[dict[str, Any]] = []
            excluded = 0
            pattern = re.compile(r"^(.*?) on (.*?) type (.*?) \((.*)\)$")
            for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
                matched = pattern.match(raw.strip())
                if not matched:
                    continue
                fstype = matched.group(3).strip()
                if not is_persistent_fstype(fstype):
                    excluded += 1
                    continue
                rows.append(_row(
                    设备=matched.group(1).strip(),
                    挂载点=matched.group(2).strip(),
                    类型=fstype,
                    挂载选项=matched.group(4).strip(),
                ))
            return rows, excluded

        def _dmesg_rows(path: Path, limit: int = 20) -> list[dict[str, Any]]:
            if not path.exists():
                return []
            lines = [l.strip() for l in path.read_text(encoding="utf-8", errors="replace").splitlines() if l.strip()]
            return [_row(内核错误日志=l) for l in lines[:limit]]

        lscpu = _colon_map(root / "evidence" / "lscpu.txt")
        time_info = ctx.snapshot.get("time_evidence", {})
        kernel_rows_all = _key_value_rows(root / "tables" / "kernel_parameters.tsv")
        mount_rows_all, mount_excluded = _mount_rows(root / "evidence" / "mounts.txt")
        dmesg_rows = _dmesg_rows(root / "evidence" / "dmesg_errors.txt")

        # 截断必须**明示**（F-23）：一台挂载点几百个的主机（容器 / K8s 节点）会把系统
        # 信息撑成几十页；但客户看到行数变少会问"是不是漏采了"，所以 note 里写清总数。
        # ★ 不用共享的 `PROFESSIONAL_ROW_LIMITS` —— MySQL / Oracle 用的是**同一批**
        #   `system.mounts` / `system.kernel` item_id，往共享字典里加键会连带改它们的
        #   产物（违反"改共享引擎前先问另外三个库会怎样"）。
        _MOUNT_LIMIT, _KERNEL_LIMIT = 20, 30
        mount_rows = mount_rows_all[:_MOUNT_LIMIT]
        kernel_rows = kernel_rows_all[:_KERNEL_LIMIT]

        def _limit_note(shown: int, total: int, what: str) -> str:
            return f"正文展示前 {shown} 个{what}，共 {total} 个；完整明细保留在分析结果中" \
                if total > shown else ""

        # =====================================================================
        # Section 1: 实例基础信息
        # =====================================================================
        arch_ok = settings.get("archive_mode", "off") == "on"
        wal_ok = settings.get("wal_level", "minimal") != "minimal"
        autovac_ok = settings.get("autovacuum", "on") == "on"
        issues = []
        if not arch_ok: issues.append("归档未开启")
        if not wal_ok: issues.append("wal_level=minimal")
        if not autovac_ok: issues.append("autovacuum 关闭")

        # 能力探测：`capabilities.tsv` 有 17 个键，报告原先只消费了 2 个
        # （`pg_stat_statements` / `sar_available`）。放在「实例基础信息」章 ——
        # `is_in_recovery` / `wal_level` / `archive_mode` 本来就决定这一节的结论，
        # 后面「网络配置」「实例进程」也要用 `caps_map` 裁剪期望集合，所以先解析。
        capability_rows_raw = parse_capabilities(ctx.raw_lines("capabilities.tsv"))
        caps_map = {row["capability"]: row["value"] for row in capability_rows_raw}
        inst_rows = [
            _row(检查项="数据库版本", 采集值=short_pg_version(ident.get("version")),
                 说明=pg_build_note(ident.get("version"))),
            _row(检查项="实例标识", 采集值=ident.get("instance_tag", ""), 说明="主机、IP 与端口组合"),
            _row(检查项="运行时间", 采集值=str(inst.get("uptime", ""))[:19] if inst.get("uptime") else "", 说明="实例启动至今"),
            _row(检查项="观测角色", 采集值=role.get("role_observed", ""), 说明="依据只读/复制证据推断"),
            _row(检查项="WAL 级别", 采集值=settings.get("wal_level", ""), 说明="replica/logical 可支持复制"),
            _row(检查项="归档模式", 采集值=settings.get("archive_mode", ""), 说明="PITR 需要 archive_mode=on"),
            _row(检查项="autovacuum", 采集值="开启" if autovac_ok else "⚠ 关闭", 说明="自动清理与维护"),
            _row(检查项="max_connections", 采集值=str(settings.get("max_connections", "")), 说明="最大连接数"),
            _row(检查项="数据库数量", 采集值=str(len(db_rows)), 说明="实例内数据库数"),
            _row(检查项="数据目录", 采集值=settings.get("data_directory", ""), 说明="PGDATA"),
        ]
        # 能力表逐键铺开：值本身是 `0/1` 或裸字符串，没有说明列读者读不懂
        # 「pg_stat_statements=0」意味着什么。口径全部来自 `CAPABILITY_NOTES`。
        cap_rows: list[dict[str, Any]] = []
        for cap in capability_rows_raw:
            cap_rows.append(_row(
                检查项=cap["capability"],
                采集值=cap["value"] or "（空）",
                说明=CAPABILITY_NOTES.get(cap["capability"]) or "新增能力键，口径待补",
            ))

        # 只用**已探明**的键下结论；缺的键不猜（"没探到"不等于"不支持"）。
        stat_views = [k for k in ("pg_stat_wal", "pg_stat_io", "pg_stat_checkpointer")
                      if caps_map.get(k) == "1"]
        if not cap_rows:
            cap_conclusion = "未采集到能力探测表（capabilities.tsv 缺失或未登记）。"
            cap_status = "not_evaluated"
        else:
            cap_bits = [f"共 {len(cap_rows)} 项能力已探测"]
            if caps_map.get("pg_stat_statements") != "1":
                # 这是"证据受限"而不是"实例有故障"：不升风险等级，但必须说清楚，
                # 否则读者会以为「SQL TOP 分析」这一节是分析漏了。
                cap_bits.append("pg_stat_statements 不可用 ⇒ 无法给出 SQL 级 TOP 分析")
            if caps_map.get("is_in_recovery") == "t":
                cap_bits.append("当前实例为备库（is_in_recovery=t）")
            if stat_views:
                cap_bits.append("可用统计视图：" + "、".join(stat_views))
            cap_conclusion = "；".join(cap_bits) + "。"
            cap_status = "normal"

        _add_section("system_environment", "实例基础信息", [
            _item("pg.instance", "实例基本信息", "snapshot.json#instance_identity", inst_rows,
                  conclusion="实例版本 {} 已识别，观测角色 {}；{}".format(
                      short_pg_version(ident.get("version")) or "未知",
                      role.get("role_observed", "standalone"),
                      "配置无异常" if not issues else "关注：" + "、".join(issues)),
                  status="attention" if issues else "normal",
                  evidence=[f"端口 {ident.get('port', '?')}", f"PGDATA={settings.get('data_directory', '?')}"],
                  recommendation="；".join(issues) if issues else ""),
            _item("pg.capability_probe", "能力与特性探测",
                  "tables/capabilities.tsv", cap_rows,
                  conclusion=cap_conclusion,
                  status=cap_status,
                  evidence=([f"pg_major={caps_map.get('pg_major', '?')}",
                             f"wal_level={caps_map.get('wal_level', '?')}"] if cap_rows else [])),
        ])

        # =====================================================================
        # Section 2: 操作系统信息
        # =====================================================================
        os_rows = [
            _row(检查项="主机名", 采集值=host.get("hostname", ""), 说明="数据库所在主机"),
            _row(检查项="IP 地址", 采集值=host.get("primary_ip", ""), 说明="采集识别的主机地址"),
            _row(检查项="操作系统", 采集值=host.get("os", ""), 说明="操作系统版本"),
            _row(检查项="内核版本", 采集值=host.get("kernel", ""), 说明="Linux 内核"),
            _row(检查项="CPU 型号", 采集值=lscpu.get("Model name", ""), 说明="处理器型号"),
            _row(检查项="CPU 核数", 采集值=str(host.get("cpu_count", "")), 说明="逻辑处理器数量"),
            _row(检查项="内存总量", 采集值=_fmt_bytes(host.get("memory_total_bytes")), 说明="主机物理内存"),
        ]
        if mem_snap and mem_snap.get("available"):
            os_rows.append(_row(检查项="可用内存", 采集值=_fmt_bytes(mem_snap["available"]), 说明="现场快照"))

        # 磁盘介质（HDD/SSD/NVMe）：数据目录落在哪块盘上，决定 I/O 延迟这类结论怎么读
        # —— 机械盘上 await 偏高是常态，SSD 上同样数字才是真问题。判据走公共层
        # `disk_media`（与 MySQL 同源）；读不出来就不加这一行，不把"没读到"写成某种介质。
        disk_media_label = disk_media(
            tables.get("block_devices") or [],
            data_path=str(settings.get("data_directory") or ""),
        )
        if disk_media_label:
            os_rows.append(_row(检查项="磁盘介质", 采集值=disk_media_label,
                                说明="数据目录所在磁盘的介质类型"))

        # NUMA 拓扑：`numa.txt` 采了 7 行，原先只在台账里躺着。多路机器上"内存落在
        # 哪个 node"直接决定访存延迟与绑定策略；单节点也必须报出来 —— 否则读者分不清
        # 「单节点」与「这项没采到」。
        numa = parse_numa(ctx.raw_lines("numa.txt"))
        if numa.get("nodes"):
            node_bits: list[str] = []
            for idx in sorted(numa["per_node"]):
                info = numa["per_node"][idx]
                seg = f"node {idx}"
                if info.get("cpus"):
                    seg += f"：CPU {info['cpus']}"
                if info.get("size_mb") is not None:
                    seg += f"，内存 {info['size_mb']} MB"
                    if info.get("free_mb") is not None:
                        seg += f"（空闲 {info['free_mb']} MB）"
                node_bits.append(seg)
            os_rows.append(_row(
                检查项="NUMA 拓扑",
                采集值=f"{numa['nodes']} 个节点",
                说明="；".join(node_bits) + (
                    "；单节点无需跨节点绑核" if numa["nodes"] == 1 else "；建议核对内存分配是否跨节点"
                ),
            ))

        # 透明大页（THP）：`always` 在 PG 上会引起分配延迟抖动与内存碎片，官方与社区
        # 一致推荐 `never`（或 `madvise` + 明确使用方）；这是 PG 性能配置的必查项，
        # 此前 3 行原始数据采了但完全没进报告。
        thp = _key_value_map(root / "tables" / "hugepages.tsv")
        thp_enabled = _bracket_value(thp.get("/sys/kernel/mm/transparent_hugepage/enabled", ""))
        thp_defrag = _bracket_value(thp.get("/sys/kernel/mm/transparent_hugepage/defrag", ""))
        thp_rows = []
        if thp_enabled or thp_defrag:
            thp_rows = [
                _row(检查项="透明大页(THP)", 采集值=thp_enabled or "未采集",
                     说明="建议 never；always 会造成分配延迟抖动"),
                _row(检查项="THP 碎片整理", 采集值=thp_defrag or "未采集",
                     说明="建议 never 或 defer+madvise"),
            ]
        thp_risk = thp_enabled == "always"

        ntp_value = str(time_info.get("ntp_synchronized", "")).lower()
        ntp_ok = ntp_value in {"yes", "true", "1", "active"}
        time_rows = [
            _row(检查项="本地时间", 采集值=time_info.get("host_local_time", ""), 说明="采集时主机时间"),
            _row(检查项="时区", 采集值=time_info.get("timezone", ""), 说明="主机时区"),
            _row(检查项="NTP 同步", 采集值=time_info.get("ntp_synchronized", ""),
                 说明="systemd-timedatectl 的 NTP synchronized 读数"),
        ]

        # ---------------------------------------------------------------------
        # B1：网络配置（ip_address.txt + ip_route.txt + socket_summary.txt）
        # ---------------------------------------------------------------------
        # 三项在台账里都是 `ok`（17 / 16 / 10 行），此前报告层**一次都没读** ——
        # 于是报告说不出"主机有几张网卡、默认网关是谁、监听地址归谁"。
        # 不入新章：MySQL 标杆同样没有独立网络章（主机信息在 3.1，网络只在 SAR 实时
        # 指标里），凭空多一章会让四库章节结构不可比。
        ip_ifaces = parse_ip_addr(ctx.raw_lines("ip_address.txt"))
        ip_routes = parse_ip_route(ctx.raw_lines("ip_route.txt"))
        sock_summary = parse_socket_summary(ctx.raw_lines("socket_summary.txt"))

        net_rows: list[dict[str, Any]] = []
        secondary_ips: list[str] = []
        for iface in ip_ifaces:
            addrs = [str(item["addr"]) for item in iface["ipv4"]]
            for item in iface["ipv4"]:
                if item["secondary"]:
                    secondary_ips.append(f"{iface['name']} {item['addr']}")
            value = "、".join(addrs) if addrs else "未配置 IPv4"
            if iface["ipv6"]:
                value += "；IPv6 " + "、".join(str(x) for x in iface["ipv6"])
            bits = ["UP" if iface["up"] else "DOWN"]
            if iface["mtu"]:
                bits.append(f"MTU {iface['mtu']}")
            if iface["mac"]:
                bits.append(f"MAC {iface['mac']}")
            if iface["source"] == "ifconfig":
                bits.append("取自 ifconfig（ip 命令不可用）")
            net_rows.append(_row(
                检查项=f"网卡 {iface['name']}",
                采集值=value,
                说明="；".join(bits),
            ))

        for route in ip_routes["default"]:
            net_rows.append(_row(
                检查项="默认网关",
                采集值=f"{route['gateway'] or '未配置'}（{route['dev'] or '—'}）",
                说明="出网流量走此网关" + (f"；metric {route['metric']}" if route["metric"] else ""),
            ))

        # 直连网段按 cidr 合并：同一网段配多个地址时 `ip route` 会出多行（本机
        # 192.168.100.0/24 就同时挂了 .80 与 .130），逐行铺开会让人以为有两条网段。
        grouped_links: dict[str, dict[str, Any]] = {}
        for link in ip_routes["links"]:
            entry = grouped_links.setdefault(str(link["cidr"]), {"dev": link["dev"], "src": []})
            if link["src"]:
                entry["src"].append(str(link["src"]))
        for cidr in sorted(grouped_links):
            entry = grouped_links[cidr]
            srcs = sorted(set(entry["src"]))
            net_rows.append(_row(
                检查项=f"直连网段 {cidr}",
                采集值=f"经 {entry['dev'] or '—'}",
                说明=(f"源地址 {'、'.join(srcs)}" if srcs else "内核直连路由，未指定源地址"),
            ))

        if sock_summary.get("tcp") is not None:
            detail: list[str] = []
            for key, label in (("estab", "已建立"), ("timewait", "TIME_WAIT")):
                if sock_summary.get(key) is not None:
                    detail.append(f"{label} {sock_summary[key]}")
            sock_value = f"TCP {sock_summary['tcp']}"
            if detail:
                sock_value += f"（{'，'.join(detail)}）"
            if sock_summary.get("udp") is not None:
                sock_value += f"；UDP {sock_summary['udp']}"
            net_rows.append(_row(
                检查项="套接字",
                采集值=sock_value,
                说明="ss -s 汇总；TIME_WAIT 长期偏高说明短连接过多",
            ))

        if not ip_ifaces:
            net_conclusion = "未采集到网络接口信息（ip/ifconfig 均不可用，或台账未登记）。"
            net_status = "not_evaluated"
        else:
            net_up = [i for i in ip_ifaces if i["up"]]
            gateway = ip_routes["default"][0]["gateway"] if ip_routes["default"] else ""
            net_conclusion = "共 {} 张网卡（启用 {} 张），默认网关 {}。".format(
                len(ip_ifaces), len(net_up), gateway or "未配置")
            net_status = "normal"
            # 同网段多 IP 不等于故障（VIP/多业务地址是常见做法），所以只提示不升风险。
            if secondary_ips:
                net_conclusion += "注意同网段多 IP：{}，需确认为有意配置。".format("、".join(secondary_ips))

        # ---------------------------------------------------------------------
        # B1：实例进程（postgres_processes.txt）
        # ---------------------------------------------------------------------
        # ★ 采集端判据是脏的（`awk '/[p]ostgres/'` 匹配整行 ⇒ USER=postgres 的
        #   su/bash/sleep/ps/awk 全进包，真机 16 行里 8 行是采集脚本自己）。
        #   解析器按命令行重判，剔掉的行数**必须印出来** —— 「已过滤」与「没采到」
        #   靠这个数字区分，否则读者看到"8 个进程"会以为采集不全。
        procs = parse_postgres_processes(ctx.raw_lines("postgres_processes.txt"))
        proc_rows: list[dict[str, Any]] = []
        proc_missing: list[str] = []

        # 期望集合按**能力**裁剪：archive_mode=off 时不该有 archiver，备库才有
        # walreceiver。拿固定清单比会把"本环境不适用"报成"缺失"。
        expected_workers = ["logger", "checkpointer", "background writer", "walwriter",
                            "autovacuum launcher", "logical replication launcher"]
        if caps_map.get("archive_mode") == "on":
            expected_workers.append("archiver")
        if caps_map.get("is_in_recovery") == "t":
            expected_workers.append("walreceiver")
        proc_missing = [role for role in expected_workers if role not in procs["roles"]]

        if procs["postmaster"]:
            pmaster = procs["postmaster"][0]
            hours = (pmaster["etimes"] or 0) / 3600
            proc_rows.append(_row(
                检查项="postmaster",
                采集值=f"PID {pmaster['pid']}，已运行 {hours:.2f} 小时",
                说明=("主进程；PPID=1 说明由 init/systemd 托管" if pmaster["ppid"] == "1"
                      else f"主进程；PPID={pmaster['ppid']}，非 init 直接托管"),
            ))
            if len(procs["postmaster"]) > 1:
                proc_rows.append(_row(
                    检查项="多 postmaster",
                    采集值=f"共 {len(procs['postmaster'])} 个",
                    说明="一机多实例或存在残留主进程，需按数据目录核对",
                ))

        if procs["roles"]:
            proc_rows.append(_row(
                检查项=f"后台进程（{len(procs['roles'])} 个）",
                采集值="、".join(procs["roles"]),
                说明=("与 PostgreSQL 期望集合比对：齐全" if not proc_missing
                      else "缺少：" + "、".join(proc_missing)),
            ))
        if procs["archive_last"]:
            proc_rows.append(_row(
                检查项="最近归档 WAL 段",
                采集值=str(procs["archive_last"]),
                说明="archiver 自报的 last was；长期不变说明归档停滞",
            ))
        if procs["backends"]:
            proc_rows.append(_row(
                检查项=f"客户端连接进程（{len(procs['backends'])} 个）",
                采集值="每个连接一个后端进程",
                说明="仅快照时刻可见；常态连接数请看「连接与会话」章",
            ))
        capped = procs["postmaster"] + procs["workers"]
        if capped:
            top = max(capped, key=lambda item: safe_float(item["mem"]) or 0)
            proc_rows.append(_row(
                检查项="内存占用",
                采集值=f"%MEM 最高 {top['mem']}%（{top.get('role') or 'postmaster'}）",
                说明="ps 的 %MEM；共享内存会被多个进程重复计入，不能相加当实例内存",
            ))

        snapshot_lines = (len(procs["postmaster"]) + len(procs["workers"])
                          + len(procs["backends"]) + len(procs["excluded"]))
        proc_note = ""
        if procs["excluded"]:
            proc_note = (f"快照共 {snapshot_lines} 行，其中 {len(procs['excluded'])} 行经命令行判定"
                         "不属于本实例（su/bash/sleep/ps/awk 等采集期进程），已剔除。")

        if not proc_rows:
            proc_conclusion = "未采集到 postgres 进程快照（ps 不可用，或台账未登记）。"
            proc_status = "not_evaluated"
            proc_recommendation = ""
        elif len(procs["postmaster"]) > 1:
            proc_conclusion = (f"发现 {len(procs['postmaster'])} 个 postmaster 进程，"
                               "可能是一机多实例或残留进程。")
            proc_status = "attention"
            proc_recommendation = "按数据目录（PGDATA）核对每个 postmaster 的归属，清理残留进程。"
        elif proc_missing:
            proc_conclusion = "后台进程不齐：缺少 {}。".format("、".join(proc_missing))
            proc_status = "attention"
            proc_recommendation = ("核对 PostgreSQL 日志中对应进程的启动失败原因"
                                   "（共享内存、目录权限、磁盘空间等）。")
        else:
            proc_conclusion = "postmaster 与 {} 个后台进程齐全，未见重复主进程。".format(len(procs["roles"]))
            proc_status = "normal"
            proc_recommendation = ""

        _add_section("system_info", "系统信息", [
            _item("system.host", "主机与操作系统信息", "snapshot.json#host_identity", os_rows,
                  conclusion="{} {}，{}核/{}，现场可用 {}。".format(
                      host.get("os", "未知")[:30], host.get("kernel", "")[:20],
                      host.get("cpu_count", "?"),
                      _fmt_bytes(host.get("memory_total_bytes")),
                      _fmt_bytes(mem_snap.get("available", 0)) if mem_snap and mem_snap.get("available") else "未知"),
                  evidence=[f"OS: {host.get('os', '')}", f"CPU: {host.get('cpu_count', '')}核"],
                  recommendation="" if (host.get("cpu_count") or 0) >= 2 else "建议至少 2 核用于生产环境"),
            _item("system.time", "时间与时区", "snapshot.json#time_evidence", time_rows,
                  conclusion="主机时间未与 NTP 同步，日志关联和故障时间线存在偏差风险。" if not ntp_ok else "主机时间同步状态正常。",
                  status="risk" if not ntp_ok else "normal",
                  evidence=[f"NTP synchronized={time_info.get('ntp_synchronized')}"],
                  recommendation="启用并验证企业时间同步服务。" if not ntp_ok else ""),
            _item("system.kernel", "关键内核参数", "tables/kernel_parameters.tsv", kernel_rows,
                  conclusion="已取得数据库相关内核参数，参数值需结合操作系统基线复核。",
                  evidence=[f"参数 {len(kernel_rows_all)} 项"],
                  note=_limit_note(len(kernel_rows), len(kernel_rows_all), "内核参数")),
            _item("system.mounts", "挂载参数", "evidence/mounts.txt", mount_rows,
                  conclusion=(
                      f"已取得 {len(mount_rows_all)} 个持久化挂载参数；数据目录所在文件系统的持久性选项需重点核对。"
                      if mount_rows_all else "未采集到持久化挂载参数。"
                  ),
                  evidence=(
                      ["持久化挂载点：{}".format("、".join(str(r.get("挂载点", "")) for r in mount_rows))]
                      if mount_rows else []
                  ),
                  note="；".join(x for x in (
                      f"已排除 {mount_excluded} 个 proc/tmpfs/cgroup/光驱等非持久化挂载点"
                      if mount_excluded else "只列持久化挂载点，未发现需排除的虚拟/只读挂载",
                      _limit_note(len(mount_rows), len(mount_rows_all), "挂载点"),
                  ) if x),
                  status="attention" if mount_excluded and not mount_rows else "normal"),
            _item("system.dmesg_errors", "内核错误摘要", "evidence/dmesg_errors.txt", dmesg_rows,
                  conclusion="存在内核错误级别日志，建议结合硬件与系统日志复核。" if dmesg_rows else "未发现内核错误级别日志。",
                  status="attention" if dmesg_rows else "normal"),
            _item("system.hugepages", "透明大页(THP)", "tables/hugepages.tsv", thp_rows,
                  conclusion=(
                      "透明大页为 always —— PostgreSQL 上会引起内存分配延迟抖动与碎片，建议置为 never。"
                      if thp_risk else
                      f"透明大页当前 {thp_enabled or '未采集'}，符合 PostgreSQL 建议。" if thp_rows
                      else "未采集到透明大页配置。"
                  ),
                  status="attention" if thp_risk else ("normal" if thp_rows else "not_evaluated"),
                  evidence=([f"THP enabled={thp_enabled}", f"THP defrag={thp_defrag}"] if thp_rows else []),
                  recommendation="执行 `echo never > /sys/kernel/mm/transparent_hugepage/enabled`（写入 rc.local/systemd）" if thp_risk else ""),
            _item("system.ip_address", "网络配置",
                  "evidence/ip_address.txt + ip_route.txt + socket_summary.txt", net_rows,
                  conclusion=net_conclusion,
                  status=net_status,
                  evidence=([f"网卡 {len(ip_ifaces)} 张",
                             f"默认路由 {len(ip_routes['default'])} 条"] if ip_ifaces else [])),
            _item("system.postgres_processes", "实例进程",
                  "evidence/postgres_processes.txt", proc_rows,
                  conclusion=proc_conclusion,
                  status=proc_status,
                  recommendation=proc_recommendation,
                  evidence=([f"后台进程 {len(procs['roles'])} 个",
                             f"剔除非本实例进程 {len(procs['excluded'])} 行"] if proc_rows else []),
                  note=proc_note),
        ])

        # =====================================================================
        # Section 3: 文件系统容量
        # =====================================================================
        # inode 容量：空间没满、inode 先满是生产上常见的"磁盘没满却写不进去"
        # （小文件多 / 临时表多 / WAL 段没清）。`system.inodes` 台账 `ok`、9 行，
        # 原先只把最高值写进 note 的一句话，表格里看不到 —— 现在并列成一列。
        inode_rows = _parse_inode_rows(root / "tables" / "inodes.tsv")
        inode_by_mp = {str(x.get("挂载点", "")): x for x in inode_rows}

        fs_display_rows: list[dict] = []
        for r in real_fs[:15]:
            pct = r.get("usage_percent", 0) or 0
            flag = "⚠" if pct >= 80 else ""
            used_kb = (r.get("used") or 0) * 1024
            total_kb = (r.get("blocks") or 0) * 1024
            hit = inode_by_mp.get(str(r.get("mountpoint", "")))
            inode_pct = safe_float(hit.get("使用率数值")) if hit is not None else None
            # 取不到就写「未采集」，**不写 0%** —— 「没取到」与「用了 0%」必须能区分。
            inode_cell = (f"{'⚠' if (inode_pct or 0) >= 80 else ''}{inode_pct:.0f}%"
                          if inode_pct is not None else "未采集")
            fs_display_rows.append({
                "文件系统": r.get("filesystem", ""),
                "类型": r.get("fstype", ""),
                "挂载点": r.get("mountpoint", ""),
                "使用率": f"{flag}{pct:.0f}%",
                "inode 使用率": inode_cell,
                "可用空间": _fmt_bytes((r.get("available") or 0) * 1024),
            })

        critical_fs = [r for r in real_fs if (r.get("usage_percent") or 0) >= 80]
        fs_use_max = max((r.get("usage_percent") or 0 for r in real_fs), default=0)
        # inode 与空间是**两个独立维度**：任一满都会导致写失败，木桶取最短。
        inode_critical = [x for x in inode_rows if (safe_float(x.get("使用率数值")) or 0) >= 80]

        if critical_fs or inode_critical:
            fs_status = "risk"
            risk_parts: list[str] = []
            if critical_fs:
                risk_parts.append("{} 个挂载点空间使用率 ≥80%：{}".format(
                    len(critical_fs), "、".join(str(r.get("mountpoint", "?")) for r in critical_fs)))
            if inode_critical:
                risk_parts.append("{} 个挂载点 inode 使用率 ≥80%：{}".format(
                    len(inode_critical), "、".join(str(x.get("挂载点", "?")) for x in inode_critical)))
            fs_conclusion = "存在容量风险 —— " + "；".join(risk_parts) + "。"
            # inode 满与空间满的处置手段不同，不能共用一句"清理日志/WAL"。
            fs_recommendation = (
                "清理小文件（临时文件、未清理的 WAL 段、日志），或提高文件系统 inode 上限。"
                if inode_critical and not critical_fs else
                "清理日志/WAL 归档释放空间，或在线扩容文件系统。"
            )
        elif fs_use_max >= 60:
            fs_status = "attention"
            fs_conclusion = f"文件系统最高使用率 {fs_use_max:.0f}%，建议关注趋势。"
            fs_recommendation = "持续监控使用率增长趋势，提前规划扩容。"
        else:
            fs_status = "normal"
            fs_conclusion = f"文件系统容量正常，最高使用率 {fs_use_max:.0f}%。"
            fs_recommendation = ""

        if skipped_100s:
            fs_conclusion += "（已过滤光驱/tmpfs 等非持久化挂载点）"

        fs_note = (f"已排除 {fs_excluded} 个 tmpfs/devtmpfs/光驱等非持久化文件系统"
                   if fs_excluded else "只列持久化文件系统")
        if inode_rows:
            fs_note += "；inode 最高 {}%（{}），与空间容量并列判断".format(
                f"{inode_rows[0].get('使用率数值', 0):.0f}", inode_rows[0].get("挂载点", ""))
        else:
            # 「没采到 inode」必须说出来：表格里的「未采集」只覆盖了有挂载点的那几行，
            # 整表缺失时读者会以为"inode 都是 0%"。
            fs_note += "；未采集到 inode 使用率（system.inodes 缺失）"
        fs_limit_note = _limit_note(15, len(real_fs), "文件系统")
        if fs_limit_note:
            fs_note += "；" + fs_limit_note

        _add_section("filesystem_capacity", "文件系统容量", [
            _item("system.filesystems", "文件系统容量", "tables/filesystems.tsv", fs_display_rows,
                  conclusion=fs_conclusion, status=fs_status, recommendation=fs_recommendation,
                  evidence=[f"持久化文件系统 {len(real_fs)} 个", f"最高使用率 {fs_use_max:.0f}%"],
                  note=fs_note),
        ])

        # =====================================================================
        # Section 4: 连接与会话
        # =====================================================================
        conns = tables.get("connections", [])
        conn = conns[0] if conns else {}
        states = tables.get("session_states", [])
        # ★ B7：原写 `or 0` / `or 1` ⇒ 采集不到时这一段会印
        #   "连接数 0/1，使用率 0.0%。未发现长事务、锁等待或连接压力。"
        #   —— 把"没采到"写成"没问题"。与 metrics.connection_usage 同口径：缺则 None。
        cur_conn = safe_int(conn.get("current_connections"))
        max_conn = safe_int(conn.get("max_connections"))
        conn_usage = (cur_conn / max_conn * 100) if (cur_conn is not None and max_conn) else None

        conn_rows = [
            _row(检查项="当前连接",
                 采集值=f"{cur_conn} / {max_conn}" if conn_usage is not None else "未采集",
                 说明="使用率 {:.1f}%".format(conn_usage) if conn_usage is not None else "未采集"),
            _row(检查项="活跃连接", 采集值=str(conn.get("active_connections", "")), 说明="当前正在执行语句的连接"),
            _row(检查项="idle in transaction", 采集值=str(conn.get("idle_in_transaction", "") or "0"), 说明="需关注是否堆积"),
        ]
        for s in states[:5]:
            conn_rows.append(_row(检查项=f"  状态: {s.get('state','')}", 采集值=str(s.get("sessions", "")),
                                  说明="会话状态分布"))
        conn_note = _limit_note(5, len(states), "会话状态")

        long_tx = tables.get("long_transactions", [])
        lock_waits = tables.get("lock_waits", [])
        conn_issues = []
        if long_tx: conn_issues.append(f"长事务 {len(long_tx)} 个")
        if lock_waits: conn_issues.append(f"锁等待 {len(lock_waits)} 个")
        if conn_usage is not None and conn_usage > 70:
            conn_issues.append(f"连接使用率 {conn_usage:.0f}%")

        conn_status = "attention" if conn_issues else "normal"
        if conn_usage is None:
            conn_conclusion = "连接数未采集（connections 无记录）。"
        else:
            conn_conclusion = "连接数 {}/{}，使用率 {:.1f}%。".format(cur_conn, max_conn, conn_usage)
        if conn_issues:
            conn_conclusion += "关注：" + "、".join(conn_issues)
        elif conn_usage is not None:
            conn_conclusion += "未发现长事务、锁等待或连接压力。"

        _add_section("connections", "连接与会话", [
            _item("pg.connections", "连接与会话", "tables/connections.tsv", conn_rows,
                  conclusion=conn_conclusion, status=conn_status,
                  evidence=[f"连接 {cur_conn}/{max_conn}" if conn_usage is not None else "连接数未采集",
                            f"长事务 {len(long_tx)}",
                            f"锁等待 {len(lock_waits)}"],
                  recommendation="清理长事务，检查连接池配置" if conn_issues else "",
                  note=conn_note),
        ])

        # =====================================================================
        # Section 5: 空间使用
        # =====================================================================
        space_rows: list[dict] = []
        for d in db_rows[:10]:
            space_rows.append(_row(检查项=f"数据库", 采集值=d.get("database_name", ""),
                                   说明=_fmt_bytes(d.get("size_bytes"))))
        for t in tables.get("tablespaces", [])[:5]:
            space_rows.append(_row(检查项=f"表空间", 采集值=t.get("tablespace", ""),
                                   说明=_fmt_bytes(t.get("size_bytes"))))
        for r in tables.get("large_tables", [])[:5]:
            space_rows.append(_row(检查项=f"大表: {r.get('schemaname','')}.{r.get('table_name','')}",
                                   采集值=_fmt_bytes(r.get("total_bytes")), 说明="单表总大小（含索引与 TOAST）"))

        bloat_rows = tables.get("bloat_estimation", [])
        bloat_count = sum(1 for r in bloat_rows
                          if (safe_float(r.get("dead_pct")) or 0) >= 10
                          and (safe_float(r.get("estimated_dead_bytes")) or 0) >= 100 * 1024 * 1024)
        space_notes: list[str] = []
        if bloat_count:
            space_rows.append(_row(检查项="⚠ 疑似膨胀表", 采集值=f"{bloat_count} 张",
                                   说明=_fmt_bytes(max((safe_float(r.get("estimated_dead_bytes")) or 0 for r in bloat_rows), default=0))))
            space_notes.append(f"检测到 {bloat_count} 张疑似膨胀表")
        space_notes += [n for n in (
            _limit_note(10, len(db_rows), "数据库"),
            _limit_note(5, len(tables.get("tablespaces", [])), "表空间"),
            _limit_note(5, len(tables.get("large_tables", [])), "大表"),
        ) if n]
        space_note = "；".join(space_notes)

        _add_section("space_usage", "空间使用", [
            _item("pg.space", "数据库与表空间容量", "tables/databases.tsv", space_rows,
                  conclusion=f"共 {len(db_rows)} 个数据库，{len(tables.get('tablespaces',[]))} 个表空间。{'检测到疑似膨胀表。' if bloat_count else '空间使用正常。'}",
                  status="attention" if bloat_count else "normal",
                  evidence=[f"数据库 {len(db_rows)} 个"] + ([f"疑似膨胀表 {bloat_count} 张"] if bloat_count else []),
                  recommendation="关注膨胀表，必要时执行 pg_repack 或 VACUUM FULL" if bloat_count else "",
                  note=space_note),
        ])

        # =====================================================================
        # Section 6: 性能
        # =====================================================================
        tot_hit = sum(safe_int(r.get("blks_hit")) or 0 for r in dbstats)
        tot_read = sum(safe_int(r.get("blks_read")) or 0 for r in dbstats)
        hit_ratio = round(tot_hit / (tot_hit + tot_read) * 100, 2) if tot_hit + tot_read > 0 else None
        bg = tables.get("bgwriter", [])
        bg_row = bg[0] if bg else {}

        hit_ok = hit_ratio is not None and hit_ratio >= 95
        perf_rows = [
            _row(检查项="Buffer 命中率", 采集值=_fmt_pct(hit_ratio) if hit_ratio else "N/A",
                 说明="目标 ≥95%"),
            _row(检查项="shared_buffers", 采集值=str(settings.get("shared_buffers", "")),
                 说明=f"当前 {(safe_int(settings.get('shared_buffers')) or 0) * 8 // 1024} MB"),
            _row(检查项="effective_cache_size", 采集值=str(settings.get("effective_cache_size", "")),
                 说明=f"当前 {(safe_int(settings.get('effective_cache_size')) or 0) * 8 // 1024} MB"),
            _row(检查项="work_mem", 采集值=str(settings.get("work_mem", "")),
                 说明="单操作排序内存"),
            _row(检查项="maintenance_work_mem", 采集值=str(settings.get("maintenance_work_mem", "")),
                 说明="VACUUM/索引维护内存"),
        ]
        perf_conclusion = "Buffer 命中率 {:.1f}%，{}。".format(
            hit_ratio or 0,
            "缓存效率良好" if hit_ok else "建议评估 shared_buffers 是否偏小")
        perf_status = "normal" if hit_ok else "attention"

        _add_section("performance", "性能", [
            _item("pg.performance", "缓存与性能参数", "tables/database_stats.tsv", perf_rows,
                  conclusion=perf_conclusion, status=perf_status,
                  evidence=[f"Buffer 命中率 {hit_ratio:.1f}%" if hit_ratio else "未采集"],
                  recommendation="" if hit_ok else "评估增大 shared_buffers（建议物理内存的 25%-40%）"),
        ])

        # =====================================================================
        # Section 6B: I/O 与 WAL
        # =====================================================================
        # 这两张表此前**采了但完全没进报告**：`stat_io` 35 行、`stat_wal` 1 行、
        # `checkpointer` 1 行、`iostat_snapshot` 23 行 ⇒ 第 8 章只有一条 Buffer 命中率，
        # **零 I/O 指标、无检查点/WAL 统计**。"后端在读什么盘、一次 WAL 写多少"
        # 恰恰是判断"高延迟是盘的问题还是配置的问题"的第一手证据。
        io_rows_raw = tables.get("stat_io", [])

        def _io_int(row: dict[str, Any], key: str) -> int:
            return safe_int(row.get(key)) or 0

        io_reads = sum(_io_int(r, "reads") for r in io_rows_raw)
        io_writes = sum(_io_int(r, "writes") for r in io_rows_raw)
        io_hits = sum(_io_int(r, "hits") for r in io_rows_raw)
        io_evictions = sum(_io_int(r, "evictions") for r in io_rows_raw)
        io_fsyncs = sum(_io_int(r, "fsyncs") for r in io_rows_raw)
        io_rw = sorted(io_rows_raw, key=lambda r: _io_int(r, "reads") + _io_int(r, "writes"),
                       reverse=True)
        io_rows = [
            _row(检查项="后端读次数(reads)", 采集值=str(io_reads), 说明="pg_stat_io 合计"),
            _row(检查项="后端写次数(writes)", 采集值=str(io_writes), 说明="pg_stat_io 合计"),
            _row(检查项="缓冲区命中(hits)", 采集值=str(io_hits), 说明="共享缓冲区命中"),
            _row(检查项="缓冲区淘汰(evictions)", 采集值=str(io_evictions), 说明="淘汰越多越需要调 shared_buffers"),
            _row(检查项="fsync 次数", 采集值=str(io_fsyncs), 说明="落盘同步次数"),
        ]
        # 明细按 (backend_type, object, context) 分组取前 8 条 —— 逐行列出 35 条会把
        # 正文淹掉，客户真正要看的是"谁在读写最多"。
        # ★ 明细**必须与摘要同列形**（都是 检查项/采集值/说明）：引擎的 `data_table`
        #   按所有行的键取**并集**出列，混入 4 列的行会把整张表撑成 10 列稀疏表。
        for r in io_rw[:8]:
            reads, writes = _io_int(r, "reads"), _io_int(r, "writes")
            if not (reads + writes):
                continue
            io_rows.append(_row(
                检查项="后端 I/O {} / {} / {}".format(r.get("backend_type", ""), r.get("object", ""),
                                                      r.get("context", "")).replace(" /  /", ""),
                采集值=f"读 {reads} / 写 {writes}",
                说明=f"命中 {_io_int(r, 'hits')}，淘汰 {_io_int(r, 'evictions')}"))
        iostat = tables.get("iostat_snapshot", [])
        io_devices = _parse_iostat_devices(root / "tables" / "iostat_snapshot.tsv")
        for d in io_devices:
            io_rows.append(_row(
                检查项=f"设备 {d['device']}",
                采集值=f"tps {d['tps']:.2f}，%util {d['util']:.2f}%",
                说明=f"await {d['await']:.2f} ms（现场峰值）"))
        io_util_max = max((d["util"] for d in io_devices), default=0.0)
        io_attention = bool(io_evictions and io_hits and io_evictions / max(io_hits + io_evictions, 1) > 0.05) \
            or io_util_max >= 80
        io_conclusion = (
            "采集到 {} 条后端 I/O 统计：读 {} 次 / 写 {} 次 / 命中 {} 次 / 淘汰 {} 次；"
            "现场 iostat 样本覆盖 {} 台物理盘，最高利用率 {:.2f}%。".format(
                len(io_rows_raw), io_reads, io_writes, io_hits, io_evictions,
                len(io_devices), io_util_max)
            if io_rows_raw else "未采集到后端 I/O 统计（pg_stat_io 需要 PostgreSQL 16+）。"
        )
        if io_attention:
            io_conclusion += " 缓冲区淘汰率或磁盘利用率偏高，建议评估 shared_buffers 与 I/O 能力。"

        wal_raw = tables.get("stat_wal", [])
        wal = wal_raw[0] if wal_raw else {}
        cp_raw = tables.get("checkpointer", [])
        cp = cp_raw[0] if cp_raw else {}
        ctl_raw = tables.get("control_checkpoint", [])
        ctl = ctl_raw[0] if ctl_raw else {}
        wal_bytes = safe_int(wal.get("wal_bytes")) or 0
        cp_timed = safe_int(cp.get("num_timed")) or 0
        cp_req = safe_int(cp.get("num_requested")) or 0
        cp_forced_ratio = (cp_req / (cp_timed + cp_req)) if (cp_timed + cp_req) else 0.0
        wal_rows = [
            _row(检查项="WAL 记录数", 采集值=str(safe_int(wal.get("wal_records")) or 0), 说明="自统计重置以来"),
            _row(检查项="WAL 写入量", 采集值=_fmt_bytes(wal_bytes), 说明="决定归档/复制带宽需求"),
            _row(检查项="WAL 全页写(fpi)", 采集值=str(safe_int(wal.get("wal_fpi")) or 0),
                 说明="越高说明检查点/崩溃恢复越频繁"),
            _row(检查项="WAL buffer 满(wal_buffers_full)", 采集值=str(safe_int(wal.get("wal_buffers_full")) or 0),
                 说明="非 0 建议增大 wal_buffers"),
            _row(检查项="定时检查点", 采集值=str(cp_timed), 说明="checkpointer 统计"),
            _row(检查项="请求检查点", 采集值=str(cp_req), 说明="非定时触发（wal 空间/关机等）"),
            _row(检查项="检查点强制占比", 采集值=f"{cp_forced_ratio * 100:.1f}%",
                 说明="建议 <10%；偏高说明 max_wal_size 偏小"),
            _row(检查项="检查点写缓冲", 采集值=str(safe_int(cp.get("buffers_written")) or 0), 说明="checkpointer 写出的缓冲页数"),
            _row(检查项="当前时间线", 采集值=str(ctl.get("timeline_id", "")), 说明="control_checkpoint"),
            _row(检查项="最近检查点 LSN", 采集值=str(ctl.get("checkpoint_lsn", "")), 说明="最新检查点写入位置"),
        ]
        wal_attention = cp_forced_ratio > 0.10 or (safe_int(wal.get("wal_buffers_full")) or 0) > 0
        wal_conclusion = (
            "WAL 记录 {} 条 / {:.1f} MB，定时检查点 {} 次、请求检查点 {} 次（强制占比 {:.1f}%）。".format(
                safe_int(wal.get("wal_records")) or 0, wal_bytes / 1024 / 1024, cp_timed, cp_req,
                cp_forced_ratio * 100)
            if (wal_raw or cp_raw) else "未采集到 WAL / 检查点统计。"
        )
        if wal_attention:
            wal_conclusion += " 检查点以请求触发为主，建议评估增大 max_wal_size。"

        _add_section("io_wal", "I/O 与 WAL", [
            _item("pg.io", "后端 I/O 统计", "tables/stat_io.tsv + tables/iostat_snapshot.tsv",
                  io_rows,
                  conclusion=io_conclusion,
                  status="attention" if io_attention else ("normal" if io_rows_raw else "not_evaluated"),
                  evidence=[f"pg_stat_io 分组 {len(io_rows_raw)} 条",
                            f"物理盘 {len(io_devices)} 台，最高利用率 {io_util_max:.2f}%"],
                  recommendation="评估增大 shared_buffers 或减少无效扫描" if io_attention else "",
                  note="；".join(x for x in (
                      _limit_note(8, len(io_rows_raw), "后端 I/O 分组"),
                      "设备表已排除 dm-*（LVM 映射层）与光驱，按物理盘口径统计",
                  ) if x)),
            _item("pg.wal", "WAL 与检查点", "tables/stat_wal.tsv + tables/checkpointer.tsv",
                  wal_rows,
                  conclusion=wal_conclusion,
                  status="attention" if wal_attention else ("normal" if (wal_raw or cp_raw) else "not_evaluated"),
                  evidence=([f"WAL 写入 {wal_bytes / 1024 / 1024:.1f} MB", f"检查点 {cp_timed + cp_req} 次"]
                            if (wal_raw or cp_raw) else []),
                  recommendation="增大 max_wal_size 以减少请求检查点" if wal_attention else ""),
        ])

        # =====================================================================
        # Section 7: VACUUM 与维护
        # =====================================================================
        # 口径必须与 metrics.py 同源（见该模块顶部的说明）——此处若自行重算，
        # 会与第 1 章综合结论 / 规则层给出两个数字。
        thealth = tables.get("table_health", [])
        dead_total = sum_dead_tuples(thealth)
        stale = count_stale_tables(thealth)
        db_ages = tables.get("database_ages", [])
        max_age = database_max_xid_age(db_ages)

        vac_issues = []
        if stale > 0: vac_issues.append(f"从未 VACUUM 的表 {stale} 张")
        if dead_total > 100000: vac_issues.append(f"死元组 {dead_total}")
        if max_age > 100000000: vac_issues.append(f"事务年龄 {max_age}")

        vac_rows = [
            _row(检查项="死元组总数", 采集值=str(dead_total), 说明="全库累计"),
            _row(检查项="从未 VACUUM 的表", 采集值=str(stale), 说明="需要关注" if stale > 0 else "正常"),
            _row(检查项="最大事务年龄", 采集值=str(max_age), 说明="接近 20 亿需警惕回卷" if max_age > 100000000 else "正常"),
            _row(检查项="autovacuum_max_workers", 采集值=str(settings.get("autovacuum_max_workers", "")),
                 说明="并发 autovacuum 工作进程上限"),
        ]
        vac_status = "attention" if vac_issues else "normal"
        vac_conclusion = "死元组 {}，从未 VACUUM 的表 {} 张，最大事务年龄 {}。".format(dead_total, stale, max_age)
        if vac_issues:
            vac_conclusion += "关注：" + "、".join(vac_issues)

        _add_section("vacuum", "VACUUM 与维护", [
            _item("pg.vacuum", "VACUUM 与事务年龄", "databases/*/table_health.tsv", vac_rows,
                  conclusion=vac_conclusion, status=vac_status,
                  evidence=vac_issues,
                  recommendation="检查 autovacuum 配置，必要时手工 VACUUM" if vac_issues else ""),
        ])

        # =====================================================================
        # Section 8: 复制
        # =====================================================================
        rep_rows: list[dict] = []
        rep_status_list = tables.get("stat_replication", [])
        if rep_status_list:
            for r in rep_status_list[:5]:
                lag = _fmt_bytes((safe_float(r.get("replay_lag_bytes")) or 0)) if r.get("replay_lag_bytes") else "0"
                rep_rows.append(_row(检查项=f"备库: {r.get('application_name','')}",
                                     采集值=f"{r.get('state','')} lag={lag}",
                                     说明=r.get("sync_state", "")))

        slots = tables.get("replication_slots", [])
        if slots:
            inactive = sum(1 for r in slots if r.get("active") == "f")
            rep_rows.append(_row(检查项="复制槽", 采集值=f"共 {len(slots)} 个，非活跃 {inactive}",
                                 说明="非活跃复制槽会滞留 WAL、阻塞清理"))

        arch = tables.get("archiver", [])
        arch_failed = 0
        if arch:
            a = arch[0]
            arch_failed = int(a.get("failed_count", "0") or "0")
            rep_rows.append(_row(检查项="归档", 采集值=f"成功 {a.get('archived_count','')}", 说明=f"失败 {arch_failed}" if arch_failed else "正常"))

        # 恢复冲突（`pg_stat_database_conflicts`）：只读备库上被查询取消/终止的次数。
        # 采了 4 行却一直没进报告 —— 而"备库查不动"最常见的解释就在这几个计数里。
        conflicts = tables.get("database_conflicts", [])
        _CONFLICT_KEYS = ("confl_tablespace", "confl_lock", "confl_snapshot",
                          "confl_bufferpin", "confl_deadlock")

        def _conflict_total(row: dict[str, Any]) -> int:
            return sum(safe_int(row.get(k)) or 0 for k in _CONFLICT_KEYS)

        confl_total = sum(_conflict_total(r) for r in conflicts)
        # 逐库明细**单独成表**会与 `rep_rows` 列不同（引擎按并集出列 ⇒ 一张 9 列稀疏表），
        # 所以明细进结论文字，主表只留一行合计。
        confl_detail = "；".join(
            "{db}（锁 {lock}、快照 {snap}、缓冲钉住 {buf}、死锁 {dead}）".format(
                db=r.get("database_name", "") or "未知库",
                lock=safe_int(r.get("confl_lock")) or 0,
                snap=safe_int(r.get("confl_snapshot")) or 0,
                buf=safe_int(r.get("confl_bufferpin")) or 0,
                dead=safe_int(r.get("confl_deadlock")) or 0)
            for r in conflicts if _conflict_total(r))
        if conflicts:
            rep_rows.append(_row(检查项="恢复冲突",
                                 采集值=f"合计 {confl_total} 次",
                                 说明="只读备库上的查询被取消/终止" if confl_total else "无冲突"))

        rep_issues = []
        if any(r.get("state") not in ("streaming",) for r in rep_status_list): rep_issues.append("备库状态异常")
        if slots and sum(1 for r in slots if r.get("active") == "f") > 0: rep_issues.append("存在非活跃复制槽")
        if arch_failed > 0: rep_issues.append(f"归档失败 {arch_failed} 次")
        if confl_total > 0: rep_issues.append(f"恢复冲突 {confl_total} 次")

        if not rep_rows:
            rep_rows.append(_row(检查项="复制状态", 采集值="无复制配置", 说明="本实例未配置流复制/逻辑复制"))

        rep_status = "attention" if rep_issues else "normal"
        rep_conclusion = f"复制槽 {len(slots)} 个，归档失败 {arch_failed} 次。" if slots else "未配置复制。"
        if conflicts:
            rep_conclusion += f" 恢复冲突合计 {confl_total} 次"
            rep_conclusion += f"（{confl_detail}）。" if confl_detail else "。"
        if rep_issues:
            rep_conclusion += "关注：" + "、".join(rep_issues)

        _add_section("replication", "复制", [
            _item("pg.replication", "流复制与 WAL 归档", "tables/stat_replication.tsv", rep_rows,
                  conclusion=rep_conclusion, status=rep_status,
                  evidence=rep_issues,
                  recommendation="清理无效复制槽，检查归档命令和空间" if rep_issues else ""),
        ])

        # =====================================================================
        # Section 9: 索引分析
        # =====================================================================
        unused = tables.get("unused_indexes", [])
        dup = tables.get("duplicate_indexes", [])
        inv_idx = sum(1 for r in tables.get("indexes", []) if r.get("indisvalid") == "f")
        # 外键引用侧缺索引（`db.<库>.missing_fk_indexes`，逐库采集）。
        # ★ 这一项**此前采到了却没有任何规则与章节消费**：真机 `mydb` 有 1 条
        #   （`public.orders` 的 `orders_user_id_fkey`），报告里连"外键"二字都没出现
        #   —— 是"采到了真问题却没报"的典型。PG **不会**替外键自动建索引，
        #   父表删/改行时对子表全表扫描并加锁，高并发下直接是锁等待与写放大。
        fk_missing = tables.get("missing_fk_indexes", [])
        fk_targets = [
            "{}.{}".format(r.get("table_name", ""), r.get("constraint_name", ""))
            for r in fk_missing if r.get("table_name") or r.get("constraint_name")
        ]

        _FK_NOTE_LIMIT = 3

        def _fk_note() -> str:
            if not fk_targets:
                return "正常"
            shown = "、".join(fk_targets[:_FK_NOTE_LIMIT])
            more = f" 等 {len(fk_targets)} 条" if len(fk_targets) > _FK_NOTE_LIMIT else ""
            return f"父表删改时子表需全表扫描并加锁：{shown}{more}"

        idx_rows: list[dict] = [
            _row(检查项="未使用索引", 采集值=str(len(unused)), 说明="占用空间且影响写入"),
            _row(检查项="重复索引", 采集值=f"{len(dup)} 组", 说明="前导列相同的冗余索引"),
            _row(检查项="无效索引", 采集值=str(inv_idx), 说明="CONCURRENTLY 失败后遗留" if inv_idx else "正常"),
            _row(检查项="外键缺索引", 采集值=str(len(fk_missing)), 说明=_fk_note()),
        ]
        idx_issues = []
        if unused: idx_issues.append(f"未使用索引 {len(unused)} 个")
        if dup: idx_issues.append(f"重复索引 {len(dup)} 组")
        if inv_idx: idx_issues.append(f"无效索引 {inv_idx} 个")
        if fk_missing: idx_issues.append(f"外键缺索引 {len(fk_missing)} 个")
        idx_status = "attention" if idx_issues else "normal"

        # 建议按**实际问题**给：冗余索引为 0 却写"建议清理冗余索引"是"模板话术压过事实"，
        # 与"没采到印正常"同一类毛病（只是方向相反 —— 这里是无中生有）。
        # ★ 用列表拼，**不写"并…"开头** —— 去掉前一项后"并"就成了孤零零的连词
        #   （真机出现过「未使用 0 / 重复 0 / 无效 0，外键缺索引 1 个。并为外键引用列补建索引」）。
        idx_advice = []
        if unused or dup or inv_idx:
            idx_advice.append("建议清理冗余索引以释放空间、提升写入")
        if fk_missing:
            idx_advice.append("为外键引用列补建索引")

        rec_advice = []
        if unused or dup or inv_idx:
            rec_advice.append("使用 DROP INDEX CONCURRENTLY 评估清理冗余索引")
        if fk_missing:
            rec_advice.append("为外键引用列创建索引（CREATE INDEX CONCURRENTLY），改后复核锁等待")

        _add_section("indexes", "索引分析", [
            _item("pg.indexes", "索引分析", "databases/*/indexes.tsv", idx_rows,
                  conclusion="未使用索引 {} 个，重复 {} 组，无效 {} 个，外键缺索引 {} 个。{}".format(
                      len(unused), len(dup), inv_idx, len(fk_missing),
                      "；".join(idx_advice) if idx_advice else "索引状态正常"),
                  status=idx_status,
                  evidence=idx_issues + [
                      "外键缺索引：{}（{}）".format(
                          r.get("table_name", ""),
                          str(r.get("definition") or r.get("constraint_name") or "").strip())
                      for r in fk_missing[:_FK_NOTE_LIMIT]
                  ],
                  recommendation="；".join(rec_advice)),
        ])

        # =====================================================================
        # Section 10: 安全配置
        # =====================================================================
        auth_set = set()
        for r in tables.get("hba_rules", []):
            auth_set.add(r.get("auth_method", ""))
        roles = tables.get("roles", [])
        superusers = [r for r in roles if r.get("is_superuser") == "t"]
        ssl_on = settings.get("ssl", "off") == "on"
        pw_ok = "scram" in str(settings.get("password_encryption", "")).lower()
        weak_auth = auth_set & {"trust", "password"}

        sec_rows = [
            _row(检查项="SSL", 采集值="开启" if ssl_on else "⚠ 关闭", 说明="生产环境应开启"),
            _row(检查项="密码加密", 采集值=settings.get("password_encryption", ""), 说明="推荐 scram-sha-256" if pw_ok else "⚠ 建议升级到 scram-sha-256"),
            _row(检查项="认证方式", 采集值=", ".join(sorted(auth_set)) if auth_set else "未知",
                 说明="pg_hba.conf 中出现的认证方法"),
            _row(检查项="超级用户", 采集值=str(len(superusers)),
                 说明=f"可登录 {sum(1 for r in superusers if r.get('rolcanlogin')=='t')}" if len(superusers) >= 2 else "具备超级用户权限的账号数"),
            _row(检查项="可登录角色", 采集值=str(sum(1 for r in roles if r.get("rolcanlogin") == "t")),
                 说明="允许登录的角色数"),
        ]
        sec_issues = []
        if not ssl_on: sec_issues.append("SSL 未开启")
        if weak_auth: sec_issues.append(f"弱认证: {', '.join(weak_auth)}")
        if not pw_ok: sec_issues.append("密码加密算法需升级")
        if len(superusers) > 3: sec_issues.append(f"超级用户 {len(superusers)} 个偏多")
        sec_status = "attention" if sec_issues else "normal"

        _add_section("security", "安全配置", [
            _item("pg.security", "安全配置", "tables/hba_rules.tsv", sec_rows,
                  conclusion="SSL {}, 密码加密 {}, 弱认证 {} 种。{}".format(
                      "已开启" if ssl_on else "未开启",
                      settings.get("password_encryption", "未知"),
                      len(weak_auth),
                      "安全配置良好" if not sec_issues else "关注：" + "、".join(sec_issues)),
                  status=sec_status,
                  evidence=sec_issues,
                  recommendation="启用 SSL，升级密码加密为 scram-sha-256，移除 trust/password 认证" if sec_issues else ""),
        ])

        # =====================================================================
        # Section 11: 备份策略
        # =====================================================================
        bak_configured = False
        bak_details: list[str] = []
        for f in ["pgbackrest_info.json", "barman_check.txt"]:
            if (root / "evidence" / f).exists():
                bak_configured = True
                bak_details.append(f.replace("_", " ").replace(".json", "").replace(".txt", ""))
        cron_f = root / "evidence" / "backup_cron.txt"
        if cron_f.exists() and cron_f.stat().st_size > 0:
            bak_configured = True
            bak_details.append("crontab 定时任务")
        timer_f = root / "evidence" / "backup_timers.txt"
        if timer_f.exists() and timer_f.stat().st_size > 0:
            bak_configured = True
            bak_details.append("systemd timer")

        bak_rows = [_row(检查项="备份配置", 采集值="已检测到" if bak_configured else "⚠ 未检测到",
                         说明=", ".join(bak_details) if bak_details else "无备份工具或定时任务")]
        bak_status = "normal" if bak_configured else "risk"
        bak_conclusion = "备份配置 {}。" + ("已检测到：{}。".format(', '.join(bak_details)) if bak_configured else "⚠ 生产环境强烈建议配置定期备份。")

        _add_section("backup", "备份策略", [
            _item("pg.backup", "备份策略", "evidence/backup_*", bak_rows,
                  conclusion=bak_conclusion, status=bak_status,
                  evidence=bak_details,
                  recommendation="" if bak_configured else "配置 pgbackrest 或 pg_dump cron 定期备份"),
        ])

        # =====================================================================
        # Section 12: 日志摘要
        # =====================================================================
        log_summary = tables.get("error_log_summary", [])
        log_rows: list[dict] = []
        if log_summary:
            for r in log_summary[:8]:
                _cat = str(r.get("category", ""))
                log_rows.append(_row(检查项=_cat, 采集值=str(r.get("count", "")),
                                     说明=LOG_CATEGORY_NOTES.get(_cat, "")))
        else:
            log_rows.append(_row(检查项="错误日志", 采集值="不可用", 说明="日志未收集或未配置"))

        log_status = "normal"
        log_conclusion = f"日志事件分类 {len(log_summary)} 项。"
        fatal_count = sum(int(r.get("count", "0") or "0") for r in log_summary if r.get("category") == "FATAL")
        if fatal_count > 0:
            log_conclusion += f"关注：FATAL {fatal_count} 次。"
            log_status = "attention"

        _add_section("logs", "日志摘要", [
            _item("pg.logs", "日志摘要", "tables/error_log_summary.tsv", log_rows,
                  conclusion=log_conclusion, status=log_status,
                  evidence=[f"FATAL {fatal_count} 次"] if fatal_count > 0 else [],
                  recommendation="检查 FATAL 日志原因，排查认证失败或连接问题" if fatal_count > 0 else "",
                  note=_limit_note(8, len(log_summary), "分类")),
        ])

        # =====================================================================
        # Section 13: 关键参数
        # =====================================================================
        key_params = ["shared_buffers", "effective_cache_size", "work_mem", "maintenance_work_mem",
                      "wal_level", "archive_mode", "autovacuum", "max_connections",
                      "max_wal_size", "min_wal_size", "checkpoint_timeout",
                      "autovacuum_max_workers", "autovacuum_naptime",
                      "idle_in_transaction_session_timeout", "statement_timeout",
                      "log_min_duration_statement", "password_encryption"]
        set_rows: list[dict] = []
        for k in key_params:
            v = settings.get(k, "")
            if v:
                set_rows.append(_row(检查项=k, 采集值=str(v), 说明=SETTING_NOTES.get(k, "")))

        _add_section("settings", "关键参数", [
            _item("pg.settings", "关键参数快照", "tables/settings.tsv", set_rows,
                  conclusion=f"已检查 {len(set_rows)} 个关键参数。",
                  note=f"共 {len(key_params)} 个关注参数，已采集 {len(set_rows)} 个"),
        ])

        # =====================================================================
        # Section 14: 对象统计
        # =====================================================================
        obj = tables.get("object_counts", [])
        obj_rows: list[dict] = []
        for r in obj[:12]:
            _otype = str(r.get("object_type", ""))
            obj_rows.append(_row(检查项=_otype, 采集值=str(r.get("count", "")),
                                 说明=OBJECT_NOTES.get(_otype, "")))
        parts = tables.get("partition_summary", [])
        if parts:
            max_p = max((safe_int(r.get("partition_count")) or 0 for r in parts), default=0)
            obj_rows.append(_row(检查项="分区表", 采集值=str(len(parts)), 说明=f"最大分区 {max_p}" if max_p else ""))

        _add_section("objects", "对象统计", [
            _item("pg.objects", "数据库对象统计", "tables/object_counts.tsv", obj_rows,
                  conclusion=f"共记录 {len(obj_rows)} 类数据库对象。",
                  # ★ 顶层 `pg.object_counts` 连的是**默认库**（PGDATABASE，通常是 postgres），
                  #   不是业务库 ⇒ 这个计数只反映该库，不能当成"实例全部对象"。写出来，
                  #   免得客户拿它对业务库的实际情况。
                  note="；".join(x for x in (
                      "计数来自顶层连接所在库（默认库，通常为 postgres），不含业务库；"
                      "逐库对象数见各库明细",
                      _limit_note(12, len(obj), "对象类型"),
                  ) if x)),
        ])

        return {"sections": sections}

    def build_report_model(self, ctx: PackageContext, metrics: dict[str, Any],
                           findings: list[Finding], evaluations: list[RuleEvaluation],
                           quality: dict[str, Any], charts: list[dict[str, Any]],
                           health: dict[str, Any]) -> dict[str, Any]:
        inspection_model = self.build_inspection_model(ctx)
        ident = ctx.snapshot.get("instance_identity", {})
        host = ctx.snapshot.get("host_identity", {})
        role = ctx.snapshot.get("role_evidence", {})
        caps = ctx.snapshot.get("capabilities", {})
        sampling = ctx.snapshot.get("sampling", {})

        instance = {
            "instance_id": ctx.instance_id,
            "identity": {
                "version": short_pg_version(ident.get("version", "")),
                "version_full": ident.get("version", ""),
                "hostname": host.get("hostname", ""),
                "ip": host.get("primary_ip", ""),
                "port": ident.get("port", ""),
                "role_observed": role.get("role_observed", ""),
                "data_directory": ident.get("data_directory", ""),
            },
            "metrics": metrics,
            "health_summary": health,
            "collection_quality": quality,
            "capabilities": {
                "pg_stat_statements": caps.get("pg_stat_statements", False),
                "sar_available": caps.get("sar_command", False),
            },
            "sampling": {
                "status": sampling.get("status", ""),
                "duration_seconds": sampling.get("requested_duration_seconds", 0),
                "sample_points": len(ctx.timeseries.get("pg_activity", [])),
            },
            "collector": ctx.snapshot.get("collector", {}),
            "time_evidence": ctx.snapshot.get("time_evidence", {}),
            "comprehensive_conclusions": self._build_conclusions(findings, metrics),
        }

        return {
            "analyzer_version": self.analyzer_version,
            "analysis_schema": self.analysis_schema,
            "analyzed_at": self.clock(),
            "instance": instance,
            "inspection_model": inspection_model,
            "risk_register": [f.to_dict() for f in findings],
            "rule_evaluations": [e.to_dict() for e in evaluations],
            "charts": charts,
            "comprehensive_conclusions": self._build_conclusions(findings, metrics),
        }

    def _build_conclusions(self, findings: list[Finding], metrics: dict[str, Any]) -> list[dict[str, Any]]:
        """Build domain-level comprehensive conclusions (like MySQL's 6-domain pattern)."""
        conclusions: list[dict[str, Any]] = []

        # Domain 1: 配置合规
        config_issues = []
        if metrics.get("autovacuum") == "off":
            config_issues.append("autovacuum 已关闭")
        if metrics.get("wal_level") == "minimal":
            config_issues.append("wal_level=minimal")
        if metrics.get("archive_mode") == "off":
            config_issues.append("archive_mode=off")
        if metrics.get("has_idle_timeout") is False:
            config_issues.append("未设置 idle_in_transaction_session_timeout")
        if metrics.get("has_stmt_timeout") is False:
            config_issues.append("未设置 statement_timeout")
        cfg_status = "attention" if config_issues else "normal"
        cfg_conclusion = (f"已检查关键参数，{len(config_issues)} 项存在可优化项：" + "；".join(config_issues)
                          if config_issues else "已检查关键参数，未发现明显配置偏差。")
        conclusions.append({"topic": "配置合规", "status": cfg_status, "conclusion": cfg_conclusion,
                           "evidence": config_issues})

        # Domain 2: 缓存与性能
        cache = metrics.get("cache_hit_ratio")
        if cache is not None:
            cache_ok = cache >= 95
            conclusions.append({
                "topic": "缓存与性能", "status": "normal" if cache_ok else "attention",
                "conclusion": f"Buffer 命中率 {cache:.1f}%{'，缓存效率良好' if cache_ok else '，建议评估增大 shared_buffers'}。",
                "evidence": [f"命中率 {cache:.1f}%"],
            })

        # Domain 3: 事务与并发
        long_tx = metrics.get("long_transaction_count", 0)
        locks = metrics.get("lock_wait_count", 0)
        deadlocks = metrics.get("deadlocked_count", 0)
        conn_usage = metrics.get("connection_usage", 0)
        tx_issues = []
        if long_tx > 0: tx_issues.append(f"长事务 {long_tx} 个")
        if locks > 0: tx_issues.append(f"锁等待 {locks} 个")
        if deadlocks and deadlocks > 0: tx_issues.append(f"死锁 {deadlocks}")
        if conn_usage and conn_usage > 80: tx_issues.append(f"连接使用率 {conn_usage:.0f}%")
        tx_status = "risk" if (long_tx > 0 or locks > 0) else "attention" if conn_usage and conn_usage > 70 else "normal"
        tx_conclusion = (f"采集时发现连接使用率 {conn_usage:.1f}%，连接数 {metrics.get('current_connections','')}/{metrics.get('max_connections','')}。"
                         + ("关注：" + "、".join(tx_issues) if tx_issues else "未发现长事务或锁等待。结果仅代表现场时点。"))
        conclusions.append({"topic": "事务与并发", "status": tx_status, "conclusion": tx_conclusion,
                           "evidence": tx_issues})

        # Domain 4: VACUUM 与维护
        stale = metrics.get("stale_tables", 0)
        dead_tup = metrics.get("dead_tuples_total", 0)
        xid_age = metrics.get("max_xid_age", 0)
        bloat = metrics.get("bloat_table_count", 0)
        vac_issues = []
        if stale > 0: vac_issues.append(f"从未 VACUUM 的表 {stale} 张")
        if dead_tup > 100000: vac_issues.append(f"死元组 {dead_tup}")
        if xid_age > 100000000: vac_issues.append(f"事务年龄 {xid_age}（接近回卷阈值）")
        if bloat > 0: vac_issues.append(f"疑似膨胀表 {bloat} 张")
        vac_status = "risk" if xid_age > 100000000 else "attention" if vac_issues else "normal"
        vac_conclusion = (f"死元组 {dead_tup}，最大事务年龄 {xid_age}，从未 VACUUM 的表 {stale} 张。"
                         + ("关注：" + "、".join(vac_issues) if vac_issues else "VACUUM 维护状态正常。"))
        conclusions.append({"topic": "VACUUM 与维护", "status": vac_status, "conclusion": vac_conclusion,
                           "evidence": vac_issues})

        # Domain 5: 复制与高可用
        rep_lag = metrics.get("max_replication_lag_bytes", 0)
        inactive_slots = metrics.get("inactive_slots", 0)
        arch_failed = metrics.get("archive_failed_count", 0)
        rep_issues = []
        if inactive_slots > 0: rep_issues.append(f"非活跃复制槽 {inactive_slots} 个")
        if arch_failed > 0: rep_issues.append(f"归档失败 {arch_failed} 次")
        if rep_lag and rep_lag > 100 * 1024 * 1024: rep_issues.append("备库延迟 >100MB")
        is_standby = metrics.get("role_observed") == "standby"
        rep_status = "risk" if (inactive_slots > 0 or arch_failed > 0) else "attention" if is_standby else "normal"
        rep_conclusion = (f"复制槽 {metrics.get('active_replication_slots',0)} 个活跃，归档失败 {arch_failed} 次。"
                         + ("关注：" + "、".join(rep_issues) if rep_issues else "复制状态正常。"))
        conclusions.append({"topic": "复制与高可用", "status": rep_status, "conclusion": rep_conclusion,
                           "evidence": rep_issues})

        # Domain 6: 安全与可恢复性
        sec_issues = []
        if metrics.get("ssl_enabled") is False:
            sec_issues.append("SSL 未开启")
        if metrics.get("has_trust_auth") is True:
            sec_issues.append("存在 trust/password 认证")
        if metrics.get("weak_password_encryption") is True:
            sec_issues.append("密码加密算法不安全")
        if (metrics.get("superuser_count") or 0) > 3:
            sec_issues.append(f"超级用户 {metrics.get('superuser_count')} 个偏多")
        if not metrics.get("has_backup_tool") and not metrics.get("has_backup_cron"):
            sec_issues.append("未检测到备份策略")
        sec_status = "risk" if not metrics.get("has_backup_tool") else "attention" if sec_issues else "normal"
        sec_conclusion = ("巡检发现以下安全与备份问题：" + "；".join(sec_issues)
                         if sec_issues else "未发现明显安全隐患，备份策略需持续验证。")
        conclusions.append({"topic": "安全与可恢复性", "status": sec_status, "conclusion": sec_conclusion,
                           "evidence": sec_issues})

        return conclusions

    def build_llm_input(self, ctx: PackageContext, metrics: dict[str, Any],
                        report: dict[str, Any]) -> dict[str, Any]:
        return {
            "instances": [{
                "identity": report["instance"]["identity"],
                "pg_metrics": metrics,
                "health_summary": report["instance"]["health_summary"],
            }],
            "topology": {},
            "inspection_model": report.get("inspection_model", {}),
        }

    def topology(self, contexts: list[PackageContext]) -> dict[str, Any]:
        """Detect PG primary-standby topology across multiple packages."""
        if len(contexts) < 2:
            return {"nodes": [{"instance_id": c.instance_id, "role": "standalone"} for c in contexts]}

        nodes = []
        for ctx in contexts:
            ident = ctx.snapshot.get("instance_identity", {})
            is_rec = ident.get("is_recovery", False)
            ctrl = ctx.tables.get("control_system", [])
            sys_id = ctrl[0].get("system_identifier", "") if ctrl else ""
            role = ctx.snapshot.get("role_evidence", {}).get("role_observed", "unknown")
            host = ctx.snapshot.get("host_identity", {})
            nodes.append({
                "instance_id": ctx.instance_id,
                "hostname": host.get("hostname", ""),
                "ip": host.get("primary_ip", ""),
                "port": ident.get("port", ""),
                "is_recovery": is_rec,
                "role": role,
                "system_identifier": sys_id,
            })

        # Match primaries to standbys by system_identifier
        edges = []
        primaries = [n for n in nodes if not n["is_recovery"]]
        standbys = [n for n in nodes if n["is_recovery"]]

        for p in primaries:
            for s in standbys:
                if s["system_identifier"] and p["system_identifier"] and s["system_identifier"] == p["system_identifier"]:
                    edges.append({
                        "source": p["instance_id"],
                        "target": s["instance_id"],
                        "type": "streaming_replication",
                    })

        # Unresolved standbys (primary not in collected packages)
        unresolved = [s for s in standbys if not any(e["target"] == s["instance_id"] for e in edges)]

        return {
            "nodes": nodes,
            "edges": edges,
            "unresolved_standbys": unresolved,
        }

    def _build_multi_conclusions(self, findings: list[Finding],
                                  contexts: list[PackageContext]) -> list[dict[str, Any]]:
        """Build aggregated domain conclusions across multiple instances."""
        conclusions: list[dict[str, Any]] = []
        criticals = [f for f in findings if f.severity == "critical"]
        warnings = [f for f in findings if f.severity == "warning"]

        if len(contexts) > 1:
            roles = [ctx.snapshot.get("role_evidence", {}).get("role_observed", "standalone") for ctx in contexts]
            conclusions.append({"topic": "拓扑概况", "status": "ok",
                "conclusion": f"共 {len(contexts)} 个节点: {', '.join(f'{r}' for r in roles)}"})

        if criticals:
            conclusions.append({"topic": "严重风险", "status": "critical",
                "conclusion": f"发现 {len(criticals)} 个严重风险项需立即处理",
                "evidence": [f.title for f in criticals]})
        if warnings:
            conclusions.append({"topic": "需关注事项", "status": "warning",
                "conclusion": f"发现 {len(warnings)} 个需关注的问题",
                "evidence": [f.title for f in warnings]})
        if not criticals and not warnings:
            conclusions.append({"topic": "整体状态", "status": "ok",
                "conclusion": f"共检查 {len(contexts)} 个节点，未发现严重风险或警告项，数据库运行状态良好"})
        return conclusions


# ---------------------------------------------------------------------------
