"""SQL Server 报告的检查项口径说明与分析结论文案。

professional 布局下，每个检查项在正文里只有「数据来源」一行小字，分析结论文字
集中在章末「本章分析结论」表。SQL Server 此前那一列全部是同一句兜底文案
（"已采集并展示本项巡检事实，不单独形成风险判断。"）—— 42 项里 27 项如此，
报告读起来像表格导出的清单，而不是巡检结论。

本模块给每个检查项补两样东西：

1. ``analysis``：说清「拿到了什么（带实测数字）、这份数据回答什么问题、
   边界在哪」——即 MySQL 呈现层 ``_item(..., conclusion=...)`` 的同一职责；
2. ``display.note``：口径说明（取数范围、TOP N 截断、与相邻指标的口径差异）。

风险判断不在这里写。 命中规则的项由 ``_inject_analysis`` 覆盖成风险结论；
本模块只写事实与口径。两者若都写判断，会出现「结论说正常、风险登记册说有风险」
的自相矛盾 —— 这正是把默认文案写成中立口径句的原因。
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, Callable

# 采集端登记为这三个状态时，数据是可用的（success_no_data 表示 SQL 跑通、结果为空）。
_OK_STATUS = {"ok", "success", "success_no_data"}
_STATUS_CN = {
    "ok": "正常", "success": "成功", "success_no_data": "成功（无数据）", "failed": "失败",
    "error": "失败", "partial": "部分可用", "empty": "无记录", "not_applicable": "不适用",
    "not_evaluated": "未判断", "skipped": "已跳过", "unsupported": "不支持",
    "permission_denied": "权限不足", "timeout": "超时", "not_enabled": "未启用",
}


# --------------------------------------------------------------------------- 取值工具

def _f(v: Any) -> float | None:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _n(v: Any, d: int = 0) -> str:
    """按位数格式化数字；取不到值时给 ``-``，绝不写成 0。"""
    x = _f(v)
    if x is None:
        return "-"
    return f"{x:,.{d}f}".rstrip("0").rstrip(".") if d else f"{x:,.0f}"


def _pct(v: Any, d: int = 1) -> str:
    x = _f(v)
    return "-" if x is None else f"{x:,.{d}f}%"


def _parse(v: Any) -> datetime | None:
    if not v:
        return None
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _dt(v: Any) -> str:
    t = _parse(v)
    return t.strftime("%Y-%m-%d %H:%M") if t else (str(v)[:19] if v else "-")


def _days_since(v: Any, ref: datetime | None) -> int | None:
    t = _parse(v)
    if t is None:
        return None
    now = ref or datetime.now(timezone.utc)
    if t.tzinfo is None and now.tzinfo is not None:
        t = t.replace(tzinfo=now.tzinfo)
    elif t.tzinfo is not None and now.tzinfo is None:
        now = now.replace(tzinfo=t.tzinfo)
    try:
        return int((now - t).total_seconds() // 86400)
    except (TypeError, ValueError, OverflowError):
        return None


def _sum(rows: Any, key: str) -> float:
    return sum(v for v in (_f((r or {}).get(key)) for r in (rows or [])) if v is not None)


def _top(rows: Any, key: str, *, lowest: bool = False) -> dict[str, Any] | None:
    """按 ``key`` 取最大（或最小）的一行；全部取不到值时返回 None。"""
    best: dict[str, Any] | None = None
    best_v: float | None = None
    for r in rows or []:
        v = _f((r or {}).get(key))
        if v is None:
            continue
        if best_v is None or (v < best_v if lowest else v > best_v):
            best, best_v = r, v
    return best


def _dist(rows: Any, key: str) -> str:
    """把某列的值分布压成「A 3、B 2」这种短串，用于结论里的构成描述。"""
    counts: dict[str, int] = {}
    for r in rows or []:
        label = str((r or {}).get(key) or "未知")
        counts[label] = counts.get(label, 0) + 1
    return "、".join(f"{k} {v}" for k, v in sorted(counts.items(), key=lambda kv: -kv[1]))


def _system_db(name: Any) -> bool:
    return str(name or "").lower() in {"master", "model", "msdb", "tempdb"}


def _user_dbs(snap: dict[str, Any]) -> list[dict[str, Any]]:
    return [x for x in snap.get("databases", []) if not _system_db(x.get("name"))]


# --------------------------------------------------------------------------- 注册表

_SPECS: dict[tuple[str, str], Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]]] = {}


def _spec(section_id: str, title: str) -> Callable:
    def deco(fn: Callable) -> Callable:
        _SPECS[(section_id, title)] = fn
        return fn
    return deco


def spec_keys() -> set[tuple[str, str]]:
    """供守门测试断言「注册表里的每一条都真的被某个检查项用上」。"""
    return set(_SPECS)


# --------------------------------------------------------------------------- 1 实例与数据库概览

@_spec("instance", "实例基础信息")
def _s_instance_basic(snap, item):
    inst = snap.get("instance", {})
    ref = _parse(snap.get("collection", {}).get("started_at"))
    uptime = _days_since(inst.get("sqlserver_start_time"), ref)
    cpu = inst.get("cpu_count")
    mem_gb = _f(inst.get("physical_memory_mb"))
    bits = [
        f"{inst.get('product_version') or '未知版本'}"
        + (f" {inst.get('edition')}" if inst.get("edition") else ""),
        f"{_n(cpu)} 核 CPU / {_n((mem_gb or 0) / 1024, 1)} GB 物理内存" if mem_gb else None,
        f"服务启动于 {_dt(inst.get('sqlserver_start_time'))}" + (f"（已运行 {uptime} 天）" if uptime is not None else ""),
        "群集实例" if inst.get("is_clustered") else "非群集实例",
    ]
    conclusion = (
        "已取得实例身份与资源基线：" + "，".join([b for b in bits if b]) + "。"
        "该基线用于判定版本支持周期与资源上限；MAXDOP、最大服务器内存等运行参数"
        "在「实例关键配置」章节单独判断，本项不重复给结论。"
    )
    return {
        "conclusion": conclusion,
        "evidence": [
            f"product_version={inst.get('product_version') or '未采集'}",
            f"cpu_count={_n(cpu)}，physical_memory_mb={_n(inst.get('physical_memory_mb'))}",
            f"物理插槽 × 每插槽核数 = {_n(inst.get('socket_count'))} × {_n(inst.get('cores_per_socket'))}，NUMA 节点 {_n(inst.get('numa_node_count'))}",
        ],
        "note": (
            "「物理内存」为 SERVERPROPERTY 报告的服务器物理内存总量，与实例可用上限"
            "（max server memory）不是同一口径；「CPU 核数」为实例可见的逻辑处理器数，"
            "并行度规划要看 物理插槽 × 每插槽核数 与 NUMA 节点数。"
        ),
    }


@_spec("instance", "数据库清单")
def _s_instance_dbs(snap, item):
    dbs = snap.get("databases", [])
    user = _user_dbs(snap)
    non_online = [d for d in dbs if str(d.get("state_desc") or "").upper() != "ONLINE"]
    levels = _dist(dbs, "compatibility_level")
    conclusion = (
        f"该实例共 {len(dbs)} 个数据库，其中用户库 {len(user)} 个；"
        f"恢复模式为 {_dist(dbs, 'recovery_model_desc') or '未采集'}，兼容级别为 {levels or '未采集'}。"
        + (f"有 {len(non_online)} 个数据库当前不处于 ONLINE 状态，需优先确认。" if non_online else "所有数据库当前均处于 ONLINE 状态。")
        + "本表用于核对每库的恢复模式、兼容级别与最近一次 DBCC CHECKDB 时间。"
    )
    return {
        "conclusion": conclusion,
        "evidence": [
            f"数据库总数 {len(dbs)}（用户库 {len(user)}、系统库 {len(dbs) - len(user)}）",
            f"数据文件合计 {_n(_sum(dbs, 'data_size_mb'))} MB，日志文件合计 {_n(_sum(dbs, 'log_size_mb'))} MB",
        ],
        "note": (
            "「数据MB / 日志MB」取自 sys.master_files 的分配页数，是文件的分配尺寸，"
            "不等于实际数据占用量；非 ONLINE 状态的库其空间统计仅供参考。"
        ),
    }


@_spec("instance", "数据库大小汇总")
def _s_instance_dbsize(snap, item):
    ds = (snap.get("database_summary") or [{}])[0]
    return {
        "conclusion": (
            f"实例数据文件合计 {_n(ds.get('total_data_mb'))} MB、日志文件合计 {_n(ds.get('total_log_mb'))} MB，"
            f"总计 {_n(ds.get('total_size_mb'))} MB；数据库总数 {_n(ds.get('database_count_all'))} 个，"
            f"其中用户数据库 {_n(ds.get('user_db_count'))} 个，日志文件 {_n(ds.get('log_file_count'))} 个。"
            "该汇总只反映当前时点的容量分布，不含增长趋势——容量预测需要同口径的历史采样。"
        ),
        "evidence": [
            f"total_size_mb={_n(ds.get('total_size_mb'))}",
            f"user_db_count={_n(ds.get('user_db_count'))}，log_file_count={_n(ds.get('log_file_count'))}",
        ],
        "note": "汇总口径为 sys.master_files 的分配尺寸求和，已含系统库与 tempdb；用户库口径见「用户数据库数」。",
    }


# 装饰器按 (section_id, title) 匹配，标题必须与 presentation.py 的 item title 逐字
# 一致，否则本项会静默退回默认文案。采集模块名仍叫「SQL Server 服务与启动参数」，
# 那是采集端契约，与本标题无关。
@_spec("instance", "服务运行状态")
def _s_instance_services(snap, item):
    svcs = snap.get("server_services", [])
    summary = "；".join(
        f"{v.get('servicename')}（{v.get('startup_type_desc') or '启动类型未采集'}/"
        f"{v.get('status_desc') or '状态未采集'}）" for v in svcs[:4]
    )
    return {
        "conclusion": (
            f"已取得 {len(svcs)} 个 SQL Server 相关服务的启动类型、运行状态、服务账号与二进制路径：{summary}。"
            "服务启动类型决定主机重启后的可用性，运行状态反映当前是否被人工停用；"
            "引擎与依赖服务（代理、全文、浏览器）的正常态判定不同，本项只陈述服务层事实。"
        ),
        "evidence": [
            f"服务 {len(svcs)} 个：{'、'.join(str(v.get('servicename')) for v in svcs[:4])}",
            "实例二进制目录：" + str(next((v.get("filename") for v in svcs if "sqlservr.exe" in str(v.get("filename") or "").lower()), "未采集")),
        ],
        "note": (
            "sys.dm_server_services 只覆盖数据库引擎、SQL Server 代理、全文与浏览器服务，"
            "不含 SSRS / SSIS / MDS 等其他 SQL 组件；SQL Server 代理出厂默认即为「手动」启动类型，"
            "其是否应予启动取决于本实例是否配置了需定时执行的作业。"
        ),
    }


@_spec("instance", "操作系统与主机")
def _s_instance_host(snap, item):
    osid = (snap.get("os_metrics") or {}).get("identity") or {}
    return {
        "conclusion": (
            f"目标主机操作系统为 {osid.get('os_caption') or '未采集'}（Build {osid.get('os_build') or '-'}），"
            f"{_n(osid.get('logical_processors'))} 个逻辑处理器、可见内存 {_n((_f(osid.get('total_visible_memory_mb')) or 0) / 1024, 1)} GB，"
            f"当前内存使用率 {_pct(osid.get('memory_used_pct'))}，页面文件 {_n(osid.get('page_file_total_mb'))} MB"
            f"（可用 {_n(osid.get('page_file_free_mb'))} MB）。"
            "该数据用于确认实例所处的操作系统版本与主机资源上限，是解读 CPU / 内存 / 磁盘采样的前提。"
        ),
        "evidence": [
            f"os_caption={osid.get('os_caption') or '未采集'}，os_build={osid.get('os_build') or '未采集'}",
            f"logical_processors={_n(osid.get('logical_processors'))}，total_visible_memory_mb={_n(osid.get('total_visible_memory_mb'))}",
            f"os_install_date={_dt(osid.get('os_install_date'))}，last_boot_up_time={_dt(osid.get('last_boot_up_time'))}",
        ],
        "note": (
            "OS 层指标只有在采集器于目标主机本地执行（-Server .）时才采集；"
            "远程连库采集时本项不出现，采集端已把该限制登记为 limitation。"
            "「内存使用率」为 OS 层 commit 口径，包含文件系统缓存，与 SQL Server 进程内存不是同一口径。"
        ),
    }


# --------------------------------------------------------------------------- 2 备份与恢复

@_spec("backup", "备份状态")
def _s_backup_status(snap, item):
    backs = snap.get("backups", [])
    full = _top(backs, "last_full_backup")
    user = [b for b in backs if not _system_db(b.get("database_name"))]
    return {
        "conclusion": (
            f"已取得 {len(backs)} 个数据库（用户库 {len(user)} 个）的最近完整、差异与日志备份时间及备份集大小。"
            + (f"最近一次完整备份是 {full.get('database_name')}，完成于 {_dt(full.get('last_full_backup'))}。" if full else "")
            + "本表回答「备份任务有没有在跑、跑的是哪一档」；备份文件能否还原由下方「还原验证历史」证明，本项不作可恢复性判断。"
        ),
        "evidence": [
            f"有备份记录的数据库 {len(backs)} 个，其中 FULL 恢复模式 {sum(1 for b in backs if str(b.get('recovery_model_desc') or '').upper() == 'FULL')} 个",
            f"完整备份合计 {_n(_sum(backs, 'full_backup_size_mb'))} MB（压缩后 {_n(_sum(backs, 'full_compressed_size_mb'))} MB）",
        ],
        "note": (
            "备份记录来自 msdb.dbo.backupset，只覆盖在本实例上发起并写入 msdb 的备份操作；"
            "由外部备份平台直连存储、未写 msdb 的备份不会出现在这张表里，需以备份平台的结果为准。"
        ),
    }


@_spec("backup", "近7天备份历史")
def _s_backup_history(snap, item):
    hist = snap.get("backup_history", [])
    copy_only = [h for h in hist if h.get("is_copy_only") in (True, 1, "1", "True", "true")]
    return {
        "conclusion": (
            f"近 7 天共 {len(hist)} 条备份记录，类型分布为 {_dist(hist, 'type') or '未采集'}，"
            f"涉及 {len({str(h.get('database_name')) for h in hist})} 个数据库。"
            + (f"其中 {len(copy_only)} 条为 copy-only 备份。" if copy_only else "")
            + "类型列标注 copy-only / snapshot 的备份不参与备份链，不能用它证明链完整；"
            "耗时与压缩率可用来判断备份窗口是否在变紧。"
        ),
        "evidence": [
            f"备份记录 {len(hist)} 条，数据库 {len({str(h.get('database_name')) for h in hist})} 个",
            f"压缩后合计 {_n(_sum(hist, 'compressed_size_mb'))} MB（原始 {_n(_sum(hist, 'backup_size_mb'))} MB）",
        ],
        "note": (
            "「类型」列的字母含义为 D=完整、I=差异、L=日志；带 /copy-only 或 /snapshot 后缀的备份"
            "不参与日志链，不能用于还原链推进。「耗时秒」为备份作业实际执行时长。"
        ),
    }


@_spec("backup", "无备份数据库")
def _s_backup_none(snap, item):
    nobk = snap.get("no_backup_databases", [])
    full_mode = [x for x in nobk if str(x.get("recovery_model_desc") or "").upper() == "FULL"]
    old = [x for x in nobk if (_f(x.get("days_since_created")) or 0) > 7]
    return {
        "conclusion": (
            f"{len(nobk)} 个数据库在 msdb 中没有任何完整备份记录，其中 {len(full_mode)} 个为 FULL 恢复模式"
            + (f"，{len(old)} 个库创建已超过 7 天" if old else "")
            + "。FULL 恢复模式下缺少完整备份意味着既不能做时间点恢复，日志文件也会持续增长无法截断；"
            "本清单同时是备份链断点的定位结果。"
        ),
        "evidence": [
            f"无备份数据库 {len(nobk)} 个：" + "、".join(str(x.get("database_name")) for x in nobk[:6]),
            f"其中 FULL 恢复模式 {len(full_mode)} 个，SIMPLE {sum(1 for x in nobk if str(x.get('recovery_model_desc') or '').upper() == 'SIMPLE')} 个",
        ],
        "note": (
            "判定口径为「sys.databases 中的库在 msdb.dbo.backupset 无任何 type='D' 记录」；"
            "刚创建尚未到首个备份窗口的库也会落进本表，「距创建天数」列用于区分这类情形。"
        ),
    }


@_spec("backup", "可疑页面")
def _s_backup_suspect(snap, item):
    sp = snap.get("suspect_pages", [])
    worst = _top(sp, "error_count")
    return {
        "conclusion": (
            f"msdb.dbo.suspect_pages 中有 {len(sp)} 条页级可疑记录"
            + (f"，错误次数最多的一条位于 {worst.get('database_name')}（错误 {_n(worst.get('error_count'))} 次）" if worst else "")
            + "。可疑页意味着该页在读取或校验时出错，处理方式取决于错误类型："
            "824/825 类多为 IO 子系统返回错误，823 类需结合硬件检查与 DBCC 结果判断。"
        ),
        "evidence": [
            f"可疑页记录 {len(sp)} 条，涉及 {len({str(x.get('database_name')) for x in sp})} 个数据库",
            f"累计错误次数 {_n(_sum(sp, 'error_count'))}",
        ],
        "note": (
            "suspect_pages 是「记录过页级错误」的账本，不代表数据当前一定损坏；"
            "清空前必须完成 DBCC CHECKDB 并保留结果，否则会丢失后续排查依据。"
        ),
    }


@_spec("backup", "还原验证历史")
def _s_backup_restore(snap, item):
    rest = snap.get("restore_history", [])
    applied = [x for x in rest if x.get("last_applied_restore_date")]
    latest = _top(rest, "last_restore_date")
    return {
        "conclusion": (
            f"{len(rest)} 个数据库存在还原或还原验证记录，其中 {len(applied)} 个数据库被实际还原过"
            + (f"；最近一次操作是 {latest.get('database_name')}（{_dt(latest.get('last_restore_date'))}）" if latest else "")
            + "。备份存在不等于备份可还原——还原记录是本实例上唯一能证明「备份被实际使用过」的证据，"
            "只有 VERIFYONLY 的库仍需补一次真实还原演练。"
        ),
        "evidence": [
            f"有还原记录 {len(rest)} 个库，其中实际还原 {len(applied)} 个、仅 VERIFYONLY {len(rest) - len(applied)} 个",
            f"累计还原次数 {_n(_sum(rest, 'restore_count'))}",
        ],
        "note": (
            "数据来自 msdb.dbo.restorehistory；「仅验证」列记录 RESTORE VERIFYONLY 的时间，"
            "它只校验备份集可读性与校验和，不验证业务可用性。"
        ),
    }


# --------------------------------------------------------------------------- 3 内存、缓冲池与 CPU

@_spec("memory", "缓冲池分布")
def _s_mem_buffer(snap, item):
    bp = snap.get("buffer_pool", [])
    top = _top(bp, "cache_size_mb")
    return {
        "conclusion": (
            f"缓冲池共缓存 {len(bp)} 个数据库的数据页，合计 {_n(_sum(bp, 'cache_size_mb'))} MB"
            + (f"，占比最高的是 {top.get('database_name')}（{_n(top.get('cache_size_mb'))} MB）" if top else "")
            + "。缓存分布反映当前热数据落在哪些库：分布与业务访问分布明显不符时，"
            "说明缓存被少数库占满，其他库的查询需频繁回读磁盘。"
        ),
        "evidence": [
            f"缓存数据库 {len(bp)} 个，合计 {_n(_sum(bp, 'cache_size_mb'))} MB",
            "缓存占比前三位：" + "、".join(
                f"{r.get('database_name')} {_pct(r.get('cache_pct'))}"
                for r in sorted(bp, key=lambda r: -(_f(r.get("cache_size_mb")) or 0))[:3]
            ),
        ],
        "note": (
            "「占比」= 该库缓存页数 / 缓冲池总页数。同一个库可能同时出现在多个缓存对象里"
            "（系统基表、对象缓存等），因此各行占比之和可以超过 100%。"
            "数据来自 sys.dm_os_buffer_descriptors，是采集时点的瞬时分布。"
        ),
    }


@_spec("memory", "进程内存状态")
def _s_mem_process(snap, item):
    m = (snap.get("memory_status") or [{}])[0]
    return {
        "conclusion": (
            f"SQL Server 进程占用物理内存 {_n(m.get('physical_memory_used_mb'))} MB，"
            f"其中锁定页（LPIM）{_n(m.get('locked_page_mb'))} MB；内存利用率（分配 / 目标）{_pct(m.get('memory_utilization_percentage'))}。"
            f"操作系统内存不足信号：物理内存 {'已触发' if m.get('process_physical_memory_low') else '未触发'}、"
            f"虚拟内存 {'已触发' if m.get('process_virtual_memory_low') else '未触发'}。"
        ),
        "evidence": [
            f"physical_memory_used_mb={_n(m.get('physical_memory_used_mb'))}，locked_page_mb={_n(m.get('locked_page_mb'))}",
            f"total_vas_gb={_n(m.get('total_vas_gb'), 1)}，vas_committed_mb={_n(m.get('vas_committed_mb'))}",
        ],
        "note": (
            "「内存利用率」为 dm_os_process_memory.memory_utilization_percentage，是分配页与提交上限之比，"
            "不是操作系统任务管理器里的内存占用率；「锁定页」只有在启用「锁定内存页」策略后才会出现非零值。"
        ),
    }


@_spec("memory", "CPU 按数据库分布")
def _s_mem_cpu_by_db(snap, item):
    cpu = snap.get("cpu_by_database", [])
    top = _top(cpu, "total_cpu_ms")
    return {
        "conclusion": (
            f"按数据库聚合的计划缓存统计中，累计 CPU 消耗最高的为 "
            f"{top.get('database_name') if top else '未采集'}（{_n(top.get('total_cpu_ms')) if top else '-'} ms / "
            f"{_n(top.get('execution_count')) if top else '-'} 次执行），"
            f"共覆盖 {len(cpu)} 个数据库。本项用于把实例级 CPU 压力归因到具体业务库，"
            "是容量规划与迁移评估的直接输入。"
        ),
        "evidence": [
            f"覆盖数据库 {len(cpu)} 个，累计 CPU {_n(_sum(cpu, 'total_cpu_ms'))} ms",
            "CPU 前三位：" + "、".join(
                f"{r.get('database_name')} {_n(r.get('total_cpu_ms'))} ms"
                for r in sorted(cpu, key=lambda r: -(_f(r.get("total_cpu_ms")) or 0))[:3]
            ),
        ],
        "note": (
            "取数口径为 dm_exec_query_stats 按计划属性中的 dbid 归属数据库；即席（ad-hoc）语句不归属任何数据库，"
            "不计入本表，因此各行合计会小于实例总 CPU。数据来自计划缓存，实例重启或计划被逐出后计数归零。"
        ),
    }


@_spec("memory", "内存组件分布")
def _s_mem_clerks(snap, item):
    clerks = snap.get("memory_clerks", [])
    top = _top(clerks, "pages_mb")
    return {
        "conclusion": (
            f"内存 clerk 前 {_n(min(len(clerks), 10))} 项合计占用 {_n(_sum(clerks[:10], 'pages_mb'))} MB"
            + (f"，占用最高的是 {top.get('type')}（{_n(top.get('pages_mb'), 1)} MB）" if top else "")
            + "。clerk 是 SQLOS 内部的内存分配账本，用于定位内存被哪类组件持有——"
            "当缓冲池可用内存不足时，先看是不是某个非缓冲池组件（计划缓存、锁、连接）把内存占住了。"
        ),
        "evidence": [
            f"clerk 条目 {len(clerks)} 个，前 10 项 pages_mb 合计 {_n(_sum(clerks[:10], 'pages_mb'))}",
            "占用前三位：" + "、".join(f"{r.get('type')} {_n(r.get('pages_mb'), 1)} MB"
                                    for r in sorted(clerks, key=lambda r: -(_f(r.get("pages_mb")) or 0))[:3]),
        ],
        "note": "clerk 的 pages_mb 是当前提交页，reserved_mb / committed_mb 为虚拟地址预留与提交量；三者口径不同，不要相加。",
    }


@_spec("memory", "系统内存状态")
def _s_mem_system(snap, item):
    sm = (snap.get("system_memory") or [{}])[0]
    total = _f(sm.get("total_physical_memory_mb"))
    avail = _f(sm.get("available_physical_memory_mb"))
    pct = (avail / total * 100) if total and avail is not None else None
    return {
        "conclusion": (
            f"系统物理内存总量 {_n(total)} MB，可用 {_n(avail)} MB"
            + (f"（{_pct(pct)}）" if pct is not None else "")
            + f"，系统缓存 {_n(sm.get('system_cache_mb'))} MB；内存状态为 {sm.get('system_memory_state_desc') or '未采集'}。"
            "该数据是 SQLOS 观察到的操作系统内存视图，用于交叉验证「进程内存状态」——"
            "进程占用高但系统内存充足时，说明内存被 SQL Server 有效利用；系统内存同样吃紧时，才需要外部干预。"
        ),
        "evidence": [
            f"total_physical_memory_mb={_n(total)}，available_physical_memory_mb={_n(avail)}",
            f"system_memory_state_desc={sm.get('system_memory_state_desc') or '未采集'}",
        ],
        "note": (
            "「可用物理内存」已扣除文件系统缓存之外的实际可用部分；"
            "Windows 会把空闲内存用作文件缓存，因此该值偏低本身不等于内存不足，"
            "判定应看 SQLOS 内存状态描述与进程内存不足信号。"
        ),
    }


# --------------------------------------------------------------------------- 4 容量、存储与文件

@_spec("capacity", "存储卷容量")
def _s_cap_volumes(snap, item):
    vols = snap.get("volumes", [])
    tight = _top(vols, "free_pct", lowest=True)
    low = [v for v in vols if (_f(v.get("free_pct")) or 100) < 15]
    return {
        "conclusion": (
            f"共采集 {len(vols)} 个卷，剩余空间最紧张的是 {tight.get('volume_mount_point') if tight else '未采集'}"
            f"（可用 {_n(tight.get('available_mb')) if tight else '-'} MB / 共 {_n(tight.get('total_mb')) if tight else '-'} MB，"
            f"{_pct(tight.get('free_pct')) if tight else '-'}）。"
            + (f"{len(low)} 个卷剩余不足 15%，需优先确认是否为数据/日志/备份落盘卷。" if low else "各卷剩余空间均在 15% 以上。")
            + "本项用于判断增长中的数据库还有多少余量，以及备份是否落在与数据同一物理卷上。"
        ),
        "evidence": [
            f"卷 {len(vols)} 个：" + "、".join(f"{v.get('volume_mount_point')} {_pct(v.get('free_pct'))}" for v in vols[:6]),
            "卷来源：" + "、".join(sorted({str(v.get("source") or "未标注") for v in vols})),
        ],
        "note": (
            "卷清单由三层合并而成：dm_os_volume_stats（含总量与可用率）、xp_fixeddrives（仅剩剩余 MB）、"
            "以及从备份介质路径解析出的盘符（备份盘只在这层出现）。后两类缺总量时相应列为空，显示为 -。"
        ),
    }


@_spec("capacity", "事务日志空间")
def _s_cap_logspace(snap, item):
    lsp = snap.get("log_space", [])
    top = _top(lsp, "log_used_pct")
    full = [x for x in lsp if (_f(x.get("log_used_pct")) or 0) >= 80]
    return {
        "conclusion": (
            f"{len(lsp)} 个数据库的日志文件使用率已采集，最高的是 {top.get('database_name') if top else '未采集'}"
            f"（{_pct(top.get('log_used_pct')) if top else '-'}，日志文件 {_n(top.get('log_size_mb')) if top else '-'} MB）。"
            + (f"有 {len(full)} 个库日志使用率已达 80% 以上，需结合日志复用等待原因判断是否会持续增长。" if full else "各库日志使用率均在 80% 以下。")
            + "日志使用率高本身不是故障——关键是日志能否被截断复用。"
        ),
        "evidence": [
            "使用率前三位：" + "、".join(f"{x.get('database_name')} {_pct(x.get('log_used_pct'))}"
                                    for x in sorted(lsp, key=lambda r: -(_f(r.get("log_used_pct")) or 0))[:3]),
            f"日志文件合计 {_n(_sum(lsp, 'log_size_mb'))} MB",
        ],
        "note": (
            "使用率来自 DBCC SQLPERF(LOGSPACE)，是日志文件内部已用比例；"
            "判断日志是否会撑爆磁盘，需结合「数据库清单」中的 log_reuse_wait_desc —— "
            "处于 LOG_BACKUP / REPLICATION 等待的日志无法截断，使用率会持续上升。"
        ),
    }


@_spec("capacity", "VLF 摘要")
def _s_cap_vlf(snap, item):
    vlf = snap.get("vlf_summary", [])
    top = _top(vlf, "vlf_count")
    many = [x for x in vlf if (_f(x.get("vlf_count")) or 0) > 1000]
    return {
        "conclusion": (
            f"{len(vlf)} 个数据库的虚拟日志文件（VLF）数量已采集，最多的是 {top.get('database_name') if top else '未采集'}"
            f"（{_n(top.get('vlf_count')) if top else '-'} 个，其中活动 {_n(top.get('active_vlf_count')) if top else '-'} 个）。"
            + (f"有 {len(many)} 个库的 VLF 超过 1000 个，建议评估日志文件重建。" if many else "各库 VLF 数量均在 1000 以内。")
            + "VLF 数量过多会显著拖慢日志备份、还原与启动恢复。"
        ),
        "evidence": [
            "VLF 前三位：" + "、".join(f"{x.get('database_name')} {_n(x.get('vlf_count'))}"
                                   for x in sorted(vlf, key=lambda r: -(_f(r.get("vlf_count")) or 0))[:3]),
            f"VLF 合计 {_n(_sum(vlf, 'vlf_count'))} 个，活动 VLF 合计 {_n(_sum(vlf, 'active_vlf_count'))} 个",
        ],
        "note": (
            "数据来自 sys.dm_db_log_info。VLF 数量由日志文件的增长方式决定："
            "按固定小 MB 增量自动增长会快速产生大量小 VLF，按固定 GB 增量或一次性预分配则不会。"
        ),
    }


@_spec("capacity", "数据库文件与IO")
def _s_cap_files(snap, item):
    files = snap.get("database_files", [])
    rmax = _top(files, "avg_read_latency_ms")
    wmax = _top(files, "avg_write_latency_ms")
    growth = [f for f in files if not f.get("is_percent_growth")]
    return {
        "conclusion": (
            f"共 {len(files)} 个数据 / 日志文件，平均读延迟最高 {_n(rmax.get('avg_read_latency_ms'), 1) if rmax else '-'} ms"
            f"（{rmax.get('database_name') if rmax else '-'}），写延迟最高 {_n(wmax.get('avg_write_latency_ms'), 1) if wmax else '-'} ms"
            f"（{wmax.get('database_name') if wmax else '-'}）。"
            f"其中 {len(growth)} 个文件按固定 MB 增长、{len(files) - len(growth)} 个按百分比增长。"
            "文件级延迟是判断 IO 子系统能否支撑当前负载的直接依据；"
            "按百分比增长的文件在库变大后单次增长量会失控，通常是需要改为固定增量或预分配的线索。"
        ),
        "evidence": [
            f"文件 {len(files)} 个，合计 {_n(_sum(files, 'size_mb'))} MB",
            f"按固定MB增长 {len(growth)} 个，按百分比增长 {len(files) - len(growth)} 个",
        ],
        "note": (
            "延迟为 dm_io_virtual_file_stats 的累计耗时除以累计 IO 次数，统计窗口自实例启动起算，"
            "不反映当前瞬时 IO 压力；要定位瞬时尖峰需看「磁盘繁忙率采样」。"
        ),
    }


@_spec("capacity", "物理磁盘介质")
def _s_cap_disks(snap, item):
    disks = (snap.get("os_metrics") or {}).get("physical_disks") or []
    kinds = _dist(disks, "media_type")
    return {
        "conclusion": (
            f"目标主机共 {len(disks)} 块物理磁盘，介质类型为 {kinds or '未采集'}。"
            "介质类型决定随机 IO 能力（SSD/NVMe 与机械盘相差一个数量级），"
            "它是判断「文件级延迟多少算正常」的前提——同样的 20 ms 在机械盘上正常、在 NVMe 上就是异常。"
        ),
        "evidence": [
            f"物理磁盘 {len(disks)} 块：" + "、".join(f"{d.get('friendly_name')}（{d.get('media_type') or '-'}）" for d in disks[:4]),
            f"容量合计 {_n(_sum(disks, 'size_gb'), 1)} GB，健康状态：" + "、".join(sorted({str(d.get('health_status') or '未采集') for d in disks})),
        ],
        "note": (
            "数据来自 os_metrics（仅目标机本地执行时采集）；"
            "「健康」为 WMI 报告的介质状态，不等于文件系统或阵列的健康状态，阵列层需通过厂商工具确认。"
        ),
    }


# --------------------------------------------------------------------------- 5 性能基线

@_spec("perf_baseline", "性能采样")
def _s_perf_sample(snap, item):
    ps = snap.get("performance_samples", [])
    n = len(ps)

    def _peak(key):
        vals = [_f(x.get(key)) for x in ps if x.get(key) is not None]
        return max([v for v in vals if v is not None]) if vals else None

    ple_vals = [_f(x.get("page_life_expectancy")) for x in ps if _f(x.get("page_life_expectancy")) is not None]
    ple_min = min(ple_vals) if ple_vals else None
    sched = []
    if _peak("runnable_tasks_count") is not None:
        sched.append(f"可运行任务峰值 {_n(_peak('runnable_tasks_count'))}")
    if _peak("work_queue_count") is not None:
        sched.append(f"工作队列峰值 {_n(_peak('work_queue_count'))}")
    if _peak("pending_disk_io_count") is not None:
        sched.append(f"待处理 IO 峰值 {_n(_peak('pending_disk_io_count'))}")
    return {
        "conclusion": (
            f"在 {n} 个采样点上，Batch 请求峰值 {_n(_peak('batch_requests_per_sec'), 1)}/s、"
            f"事务峰值 {_n(_peak('transactions_per_sec'), 1)}/s、页生命周期（PLE）最小 {_n(ple_min)} 秒、"
            f"用户连接峰值 {_n(_peak('user_connections'))}、阻塞会话峰值 {_n(_peak('blocked_session_count'))}"
            + ("；" + "、".join(sched) if sched else "")
            + "。本项给出的是采集窗口内的负载量级，用于和业务高峰口径比对；"
            "单点数值不代表趋势——速率型计数器需要至少两个点做差分才有意义。"
        ),
        "evidence": [
            f"采样点 {n} 个，窗口 {_dt(ps[0].get('timestamp')) if ps else '-'} ~ {_dt(ps[-1].get('timestamp')) if ps else '-'}",
            f"PLE 最小 {_n(ple_min)} 秒，Memory Grants Pending 峰值 {_n(_peak('memory_grants_pending'))}",
        ],
        "note": (
            f"采样点数为 {n}，间隔与时长由采集参数 -SampleCount / -SampleIntervalSeconds 决定。"
            "首个采样点因性能计数器冷启动会出现 0 值（格式化 WMI/性能计数器类的固有行为），"
            "趋势判断应跳过首点；PLE 越低说明缓冲池被换出的压力越大，持续低于 300 秒需要关注。"
        ),
    }


@_spec("perf_baseline", "CPU 调度压力采样")
def _s_perf_sched(snap, item):
    ps = [x for x in snap.get("performance_samples", []) if x.get("runnable_tasks_count") is not None]
    peak_run = max((_f(x.get("runnable_tasks_count")) or 0) for x in ps) if ps else None
    peak_wq = max((_f(x.get("work_queue_count")) or 0) for x in ps) if ps else None
    peak_io = max((_f(x.get("pending_disk_io_count")) or 0) for x in ps) if ps else None
    return {
        "conclusion": (
            f"各调度器的可运行任务峰值 {_n(peak_run)}、工作队列峰值 {_n(peak_wq)}、待处理 IO 峰值 {_n(peak_io)}，"
            f"共 {len(ps)} 个采样点。可运行任务数是「已就绪但没有 CPU 可跑」的请求数，"
            "持续大于 0 说明 CPU 核心不足以吸收当前并发；"
            "与之对比，「待处理 IO」持续大于 0 则说明瓶颈在存储而非 CPU——两者必须分开看。"
        ),
        "evidence": [
            f"采样点 {len(ps)} 个，可运行任务峰值 {_n(peak_run)}，单调度器最多 {_n(max((_f(x.get('runnable_tasks_max')) or 0) for x in ps) if ps else None)}",
            f"工作队列峰值 {_n(peak_wq)}，待处理 IO 峰值 {_n(peak_io)}",
        ],
        "note": (
            "数据为 dm_os_schedulers 的瞬时值按调度器求和（scheduler_id < 255，不含 DAC）；"
            "该字段自采集器 v1.3.2 起才进入采样点，更早的采集包本项不出现。"
        ),
    }


@_spec("perf_baseline", "操作系统采样（CPU 与内存）")
def _s_perf_os(snap, item):
    samples = (snap.get("os_metrics") or {}).get("samples") or []
    cpus = [_f(x.get("cpu_pct")) for x in samples if _f(x.get("cpu_pct")) is not None]
    avails = [_f(x.get("available_mb")) for x in samples if _f(x.get("available_mb")) is not None]
    queues = [_f(x.get("processor_queue_length")) for x in samples if _f(x.get("processor_queue_length")) is not None]
    return {
        "conclusion": (
            f"操作系统层共 {len(samples)} 个采样点：CPU 峰值 {_pct(max(cpus) if cpus else None)}、"
            f"可用内存最低 {_n(min(avails) if avails else None)} MB、处理器队列峰值 {_n(max(queues) if queues else None)}。"
            "OS 层采样与实例层采样是两条独立证据："
            "实例视角看到的是 SQLOS 调度压力，OS 视角看到的是整机资源占用——"
            "两者都吃紧才是真正的资源不足，实例视角吃紧而 OS 空闲则更可能是实例内部配置问题。"
        ),
        "evidence": [
            f"采样点 {len(samples)} 个，CPU 峰值 {_pct(max(cpus) if cpus else None)}",
            f"可用内存最低 {_n(min(avails) if avails else None)} MB，处理器队列峰值 {_n(max(queues) if queues else None)}",
        ],
        "note": (
            "仅在目标主机本地执行（-Server .）时采集；"
            "首个采样点因 WMI 格式化性能类冷启动会出现 CPU 0% 与 idle 偏高的失真值，趋势判断应跳过首点。"
        ),
    }


@_spec("perf_baseline", "磁盘繁忙率采样")
def _s_perf_disk(snap, item):
    vols: dict[str, float] = {}
    for p in (snap.get("os_metrics") or {}).get("samples") or []:
        for d in p.get("disks") or []:
            v = _f(d.get("pct_disk_time"))
            name = str(d.get("name") or "-")
            if v is not None and v > vols.get(name, -1):
                vols[name] = v
    peak = max(vols.values()) if vols else None
    busiest = max(vols, key=lambda k: vols[k]) if vols else None
    return {
        "conclusion": (
            f"共采集 {len(vols)} 个卷的磁盘繁忙率，峰值 {_pct(peak)}（{busiest or '-'}）。"
            "磁盘时间占比表示设备在采样窗口内的忙碌比例，"
            "持续高于 70% 通常意味着 IO 已成为瓶颈；"
            "该指标的价值在于区分「延迟高但设备不忙」（多为阵列缓存命中或单次 IO 大）与"
            "「延迟高且设备忙」（真实 IO 饱和）——两者的处置方向完全不同。"
        ),
        "evidence": [
            "各卷磁盘时间峰值：" + "、".join(f"{k} {_pct(v)}" for k, v in sorted(vols.items(), key=lambda kv: -kv[1])[:5]),
            "平均队列峰值：" + _n(max([(_f(d.get('avg_disk_queue_length')) or 0)
                                   for p in (snap.get('os_metrics') or {}).get('samples') or []
                                   for d in p.get('disks') or []] or [None]), 2),
        ],
        "note": (
            "数据来自 OS 层性能计数器（仅目标机本地执行时采集）；"
            "卷名来自计数器实例名，与「存储卷容量」的挂载点可能不同名，需按容量或盘符对应。"
        ),
    }


@_spec("perf_baseline", "网络吞吐采样")
def _s_perf_net(snap, item):
    nics: dict[str, float] = {}
    err = 0.0
    for p in (snap.get("os_metrics") or {}).get("samples") or []:
        for n in p.get("network") or []:
            v = _f(n.get("bytes_total_per_sec"))
            name = str(n.get("name") or "-")
            if v is not None and v > nics.get(name, -1):
                nics[name] = v
            err += (_f(n.get("packets_received_errors")) or 0) + (_f(n.get("packets_outbound_errors")) or 0)
    peak = max(nics.values()) if nics else None
    busiest = max(nics, key=lambda k: nics[k]) if nics else None
    return {
        "conclusion": (
            f"共采集 {len(nics)} 个网络接口的吞吐，峰值 {_n(peak)} 字节/秒（{busiest or '-'}），"
            f"累计错误包 {_n(err)} 个。"
            "对数据库实例而言，网络吞吐主要影响客户端连接、备份落盘与高可用日志传输；"
            "错误包计数非零需要结合网卡与交换机的统计确认是否为持续性丢包。"
        ),
        "evidence": [
            "各接口吞吐峰值：" + "、".join(f"{k} {_n(v)} B/s" for k, v in sorted(nics.items(), key=lambda kv: -kv[1])[:5]),
            f"累计错误包 {_n(err)} 个",
        ],
        "note": (
            "数据来自 OS 层性能计数器（仅目标机本地执行时采集）；"
            "错误包为采样窗口内的累计计数，非速率值，需跨多个采样点观察是否持续增长。"
        ),
    }


# --------------------------------------------------------------------------- 6 等待、阻塞与会话

@_spec("waits", "主要等待")
def _s_waits_wait(snap, item):
    waits = snap.get("wait_stats", [])
    top = _top(waits, "wait_time_ms")
    total = _sum(waits, "wait_time_ms")
    share = (_f(top.get("wait_time_ms")) or 0) / total * 100 if top and total else None
    return {
        "conclusion": (
            f"等待时间最高的等待类型为 {top.get('wait_type') if top else '未采集'}"
            f"（累计 {_n(top.get('wait_time_ms')) if top else '-'} ms、{_n(top.get('waiting_tasks_count')) if top else '-'} 次"
            + (f"，占已列等待的 {_pct(share)}" if share is not None else "") + "）。"
            "等待统计回答「实例的时间花在哪」：累计值排序能暴露主要等待来源，但累计值本身受运行时长影响，"
            "需要用占比与等待类型性质来读——CXPACKET、SOS_SCHEDULER_YIELD 这类在正常高并发下也会很高。"
        ),
        "evidence": [
            f"等待类型 {len(waits)} 项，累计等待 {_n(total)} ms",
            "前三位：" + "、".join(f"{w.get('wait_type')} {_n(w.get('wait_time_ms'))} ms"
                                 for w in sorted(waits, key=lambda r: -(_f(r.get("wait_time_ms")) or 0))[:3]),
        ],
        "note": (
            "等待统计自实例启动起累计（部分类型可用 DBCC SQLPERF('sys.dm_os_wait_stats', CLEAR) 清零），"
            "因此重启后不久的实例累计值天然偏低；判定应结合运行时长与业务窗口。"
        ),
    }


@_spec("waits", "活动阻塞")
def _s_waits_blocking(snap, item):
    blk = snap.get("blocking", [])
    worst = _top(blk, "wait_seconds")
    return {
        "conclusion": (
            f"采集时点存在 {len(blk)} 条阻塞链，最长等待 {_n(worst.get('wait_seconds')) if worst else '-'} 秒"
            f"（SPID {worst.get('session_id') if worst else '-'} 被 {worst.get('blocking_session_id') if worst else '-'} 阻塞）。"
            "阻塞链是「谁在等谁」的瞬时快照：短时阻塞是并发控制的正常表现，"
            "需要关注的是等待时长与阻塞源头事务的持续时间。"
        ),
        "evidence": [
            f"阻塞链 {len(blk)} 条，涉及数据库：" + "、".join(sorted({str(b.get('database_name') or '-') for b in blk[:6]})),
            f"最长等待 {_n(worst.get('wait_seconds')) if worst else '-'} 秒，等待类型 {worst.get('wait_type') if worst else '-'}",
        ],
        "note": "数据为采集时点 sys.dm_exec_requests 的瞬时快照；采样窗口内的阻塞链可能未被捕捉，持续阻塞需结合锁等待统计确认。",
    }


@_spec("waits", "活动请求")
def _s_waits_active(snap, item):
    active = snap.get("active_requests", [])
    longest = _top(active, "elapsed_seconds")
    running = [a for a in active if str(a.get("status") or "").lower() == "running"]
    return {
        "conclusion": (
            f"采集时点有 {len(active)} 个活动请求，其中处于 running 状态 {len(running)} 个；"
            f"耗时最长的已运行 {_n(longest.get('elapsed_seconds')) if longest else '-'} 秒"
            f"（SPID {longest.get('session_id') if longest else '-'}，库 {longest.get('database_name') if longest else '-'}）。"
            "活动请求是瞬时快照：个别长查询在批处理窗口内属正常，"
            "需要关注的是长耗时请求是否伴随高 CPU 或阻塞他人。"
        ),
        "evidence": [
            f"活动请求 {len(active)} 个（running {len(running)} 个）",
            f"累计 CPU {_n(_sum(active, 'cpu_time'))} ms，累计逻辑读 {_n(_sum(active, 'logical_reads'))}",
        ],
        "note": "数据为 sys.dm_exec_requests 在采集瞬间的结果（TOP 50）；单次快照不能代表负载全貌，趋势需对比多个时点。",
    }


@_spec("waits", "连接数统计")
def _s_waits_conn(snap, item):
    conn = snap.get("connection_stats", [])
    top = _top(conn, "connection_count")
    total = _sum(conn, "connection_count")
    progs = _dist(conn, "program_name")
    return {
        "conclusion": (
            f"客户端连接共 {_n(total)} 个，按来源分为 {len(conn)} 组；"
            f"连接数最多的是 {top.get('client_ip') or '未提供'}/{top.get('host_name') or '-'}"
            f"（{_n(top.get('connection_count'))} 个连接，程序 {top.get('program_name') or '未提供'}）。"
            f"程序名分布为 {progs or '未采集'}。"
            "连接数突增常见于连接池配置不足、应用未释放连接或异常重试；"
            "按来源分组能把「哪台应用服务器在占用连接」直接定位出来。"
        ),
        "evidence": [
            f"连接总数 {_n(total)}，来源分组 {len(conn)} 组",
            "连接数前三：" + "、".join(f"{r.get('client_ip') or '未提供'} {_n(r.get('connection_count'))}"
                                   for r in sorted(conn, key=lambda r: -(_f(r.get("connection_count")) or 0))[:3]),
        ],
        "note": (
            "按 client_ip / host_name / program_name / login_name 四列分组聚合；"
            "本地连接（共享内存或命名管道）没有 client_ip，该列会显示为空，不代表采集失败。"
        ),
    }


@_spec("waits", "会话资源占用")
def _s_waits_session(snap, item):
    sess = snap.get("session_activity", [])
    cpu_top = _top(sess, "total_cpu_ms")
    reads_top = _top(sess, "total_physical_reads")
    return {
        "conclusion": (
            f"按登录名与状态聚合为 {len(sess)} 组会话，累计 CPU 最高的为 "
            f"{cpu_top.get('login_name') if cpu_top else '未采集'}（{_n(cpu_top.get('total_cpu_ms')) if cpu_top else '-'} ms）；"
            f"累计物理读最高的为 {reads_top.get('login_name') if reads_top else '-'}"
            f"（{_n(reads_top.get('total_physical_reads')) if reads_top else '-'} 次）。"
            "本项把资源消耗归因到登录账号——"
            "某个账号的累计物理读远高于他人，通常说明它执行了未命中缓存的扫描型操作。"
        ),
        "evidence": [
            f"会话分组 {len(sess)} 组，会话总数 {_n(_sum(sess, 'session_count'))}",
            f"累计 CPU 合计 {_n(_sum(sess, 'total_cpu_ms'))} ms，累计物理读合计 {_n(_sum(sess, 'total_physical_reads'))} 次",
        ],
        "note": (
            "物理读列取自 sys.dm_exec_sessions.reads（官方的 physical reads performed），"
            "为会话自建立以来的累计值，会话结束即清零 —— 因此它反映的是当前在线会话的累计消耗，"
            "不是历史总量。"
        ),
    }


# --------------------------------------------------------------------------- 7 TempDB

@_spec("tempdb", "TempDB 空间")
def _s_tempdb(snap, item):
    t = snap.get("tempdb") or {}
    files = t.get("files") or []
    return {
        "conclusion": (
            f"tempdb 当前总空间 {_n(t.get('total_mb'))} MB、已用 {_n(t.get('used_mb'))} MB"
            f"（{_pct(t.get('used_pct'))}），共 {len(files)} 个文件"
            f"（{'按固定MB增长' if files and not files[0].get('is_percent_growth') else '按百分比增长'}）。"
            "tempdb 是实例级共享资源：排序、哈希连接、行版本（RCSI/SI）、临时表都消耗它，"
            "使用率需结合业务高峰窗口看，单点值偏低不能说明无压力。"
        ),
        "evidence": [
            f"total_mb={_n(t.get('total_mb'))}，used_mb={_n(t.get('used_mb'))}，used_pct={_pct(t.get('used_pct'))}",
            f"文件 {len(files)} 个：" + "、".join(str(f.get('logical_name')) for f in files[:6]),
        ],
        "note": (
            "数据来自 tempdb.sys.dm_db_file_space_usage，只反映 tempdb 自身空间；"
            "若启用了 RCSI，行版本占用会体现在使用率里，判断是否与「数据库清单」中的 RCSI 状态有关联。"
        ),
    }


# --------------------------------------------------------------------------- 8 SQL 与索引

_WORKLOAD_NOTE = (
    "取数口径为 dm_exec_query_stats 按计划缓存聚合，实例重启或计划被逐出后计数归零；"
    "「语句指纹」为 query_hash，可用于跨时段比对是否为同一条语句。"
    "数据库列取自计划属性的 dbid —— 即席（ad-hoc）语句不归属任何数据库，该列可能整列为空。"
)


@_spec("sql_index", "高消耗SQL(CPU)")
def _s_sql_cpu(snap, item):
    tq = snap.get("top_queries", [])
    top = _top(tq, "total_cpu_ms")
    return {
        "conclusion": (
            f"按累计 CPU 排序取前 {len(tq)} 条语句，最高为 {top.get('database_name') or '即席语句' if top else '未采集'}"
            f"（累计 {_n(top.get('total_cpu_ms')) if top else '-'} ms / {_n(top.get('execution_count')) if top else '-'} 次执行，"
            f"平均 {_n(top.get('avg_cpu_ms'), 1) if top else '-'} ms/次）。"
            "累计 CPU 高有两类成因：单次代价极高，或单次很轻但执行极频繁——"
            "要区分它们需对照「高执行次数SQL」；前者优化执行计划，后者更像缺少缓存或调用方式不当。"
        ),
        "evidence": [
            f"语句 {len(tq)} 条，累计 CPU 合计 {_n(_sum(tq, 'total_cpu_ms'))} ms",
            "CPU 前三位：" + "、".join(f"{r.get('query_hash') or '-'} {_n(r.get('total_cpu_ms'))} ms"
                                   for r in sorted(tq, key=lambda r: -(_f(r.get("total_cpu_ms")) or 0))[:3]),
        ],
        "note": _WORKLOAD_NOTE,
    }


@_spec("sql_index", "高逻辑读SQL")
def _s_sql_logical(snap, item):
    tql = snap.get("top_queries_logical_reads", [])
    top = _top(tql, "total_logical_reads")
    return {
        "conclusion": (
            f"按累计逻辑读排序取前 {len(tql)} 条语句，最高为 {top.get('database_name') or '即席语句' if top else '未采集'}"
            f"（累计 {_n(top.get('total_logical_reads')) if top else '-'} 次，平均 {_n(top.get('avg_logical_reads')) if top else '-'} 次/执行）。"
            "逻辑读高说明扫描的数据页多，是索引缺失或 WHERE 条件无法走索引最直接的线索；"
            "平均逻辑读比累计值更能说明单次执行的效率问题。"
        ),
        "evidence": [
            f"语句 {len(tql)} 条，累计逻辑读 {_n(_sum(tql, 'total_logical_reads'))} 次",
            "前三位：" + "、".join(f"{r.get('query_hash') or '-'} {_n(r.get('total_logical_reads'))} 次"
                                 for r in sorted(tql, key=lambda r: -(_f(r.get("total_logical_reads")) or 0))[:3]),
        ],
        "note": _WORKLOAD_NOTE,
    }


@_spec("sql_index", "高执行次数SQL")
def _s_sql_exec(snap, item):
    tqe = snap.get("top_queries_executions", [])
    top = _top(tqe, "execution_count")
    return {
        "conclusion": (
            f"按执行次数排序取前 {len(tqe)} 条语句，最高为 {top.get('database_name') or '即席语句' if top else '未采集'}"
            f"（{_n(top.get('execution_count')) if top else '-'} 次执行，累计 CPU {_n(top.get('total_cpu_ms')) if top else '-'} ms）。"
            "高频语句即使单次开销很小，累计消耗也可能超过少数几个大查询；"
            "这类语句的优化收益通常来自减少调用次数（批量化、加缓存）而不是改写 SQL 本身。"
        ),
        "evidence": [
            f"语句 {len(tqe)} 条，执行次数合计 {_n(_sum(tqe, 'execution_count'))} 次",
            "前三位：" + "、".join(f"{r.get('query_hash') or '-'} {_n(r.get('execution_count'))} 次"
                                 for r in sorted(tqe, key=lambda r: -(_f(r.get("execution_count")) or 0))[:3]),
        ],
        "note": _WORKLOAD_NOTE,
    }


@_spec("sql_index", "高物理读SQL")
def _s_sql_physical(snap, item):
    tqp = snap.get("top_queries_physical_reads", [])
    top = _top(tqp, "total_physical_reads")
    return {
        "conclusion": (
            f"按累计物理读排序取前 {len(tqp)} 条语句，最高为 {top.get('database_name') or '即席语句' if top else '未采集'}"
            f"（累计 {_n(top.get('total_physical_reads')) if top else '-'} 次物理读，累计 CPU {_n(top.get('total_cpu_ms')) if top else '-'} ms）。"
            "物理读高说明数据没有驻留内存："
            "既可能是缓冲池偏小，也可能是语句在扫描本就不常访问的数据——"
            "与「缓冲池分布」对照即可区分：缓冲池接近上限时偏向前者，缓冲池空闲时偏向后者。"
        ),
        "evidence": [
            f"语句 {len(tqp)} 条，累计物理读 {_n(_sum(tqp, 'total_physical_reads'))} 次",
            "前三位：" + "、".join(f"{r.get('query_hash') or '-'} {_n(r.get('total_physical_reads'))} 次"
                                 for r in sorted(tqp, key=lambda r: -(_f(r.get("total_physical_reads")) or 0))[:3]),
        ],
        "note": _WORKLOAD_NOTE,
    }


@_spec("sql_index", "缺失索引候选")
def _s_sql_missing(snap, item):
    mi = snap.get("missing_indexes", [])
    top = _top(mi, "improvement_score")
    return {
        "conclusion": (
            f"优化器给出 {len(mi)} 条缺失索引候选，改善分最高的是 "
            f"{top.get('database_name') if top else '-'}.{top.get('table_name') if top else '-'}"
            f"（改善分 {_n(top.get('improvement_score')) if top else '-'}）。"
            "缺失索引建议是优化器在编译期估算的「如果建这个索引能省多少」，"
            "它只看到单条语句的收益、看不到写入放大与索引维护成本，因此必须与现有索引比对后再决定。"
        ),
        "evidence": [
            f"候选 {len(mi)} 条，涉及表 {len({str(x.get('table_name')) for x in mi})} 张",
            f"改善分合计 {_n(_sum(mi, 'improvement_score'))}",
        ],
        "note": (
            "数据来自 sys.dm_db_missing_index_*，只覆盖最近一次编译期的估算："
            "同一语句再次编译后该建议可能消失或被替换；键列 / 包含列的先后顺序由优化器给出，"
            "建索引前需核对列的基数与选择性，并评估对写入的影响。"
        ),
    }


@_spec("sql_index", "未使用索引")
def _s_sql_unused(snap, item):
    ui = snap.get("unused_indexes", [])
    top = _top(ui, "user_updates")
    return {
        "conclusion": (
            f"有 {len(ui)} 个索引自统计窗口开始以来未被读取，其中写维护次数最多的是 "
            f"{top.get('table_name') if top else '-'}.{top.get('index_name') if top else '-'}"
            f"（{_n(top.get('user_updates')) if top else '-'} 次更新）。"
            "未被使用不等于可以删除——"
            "可能是业务周期未覆盖到，也可能是该索引承载的是唯一性约束或外键需要。"
            "这类索引的收益是省下写维护开销，风险是删掉后某个低频但重要的查询退化。"
        ),
        "evidence": [
            f"未使用索引 {len(ui)} 个，涉及表 {len({str(x.get('table_name')) for x in ui})} 张",
            f"累计写维护 {_n(_sum(ui, 'user_updates'))} 次",
        ],
        "note": (
            "使用统计来自 sys.dm_db_index_usage_stats，窗口自实例重启起算；"
            "观察窗口不足一个完整业务周期（如月度报表）时，本项不能作为删除索引的唯一依据。"
        ),
    }


# --------------------------------------------------------------------------- 9 数据库对象

@_spec("db_objects", "无主键表")
def _s_obj_nopk(snap, item):
    nopk = snap.get("tables_without_pk", [])
    total = _sum(nopk, "table_count")
    return {
        "conclusion": (
            f"{len(nopk)} 个用户库存在无主键表，合计 {_n(total)} 张"
            + ("：" + "、".join(f"{x.get('database_name')} {_n(x.get('table_count'))} 张" for x in nopk[:5]) if nopk else "")
            + "。无主键表无法参与事务复制、CDC 与日志读取捕获，"
            "且缺少聚集索引会让行定位退化为堆扫描——它既是功能限制，也是性能隐患。"
        ),
        "evidence": [
            f"涉及数据库 {len(nopk)} 个，无主键表合计 {_n(total)} 张",
            "数量前三：" + "、".join(f"{x.get('database_name')} {_n(x.get('table_count'))} 张"
                                 for x in sorted(nopk, key=lambda r: -(_f(r.get("table_count")) or 0))[:3]),
        ],
        "note": (
            "口径为「用户库中不存在任何聚集/非聚集索引的表」；"
            "采集端按库聚合为表数量，具体表名需查采集包内的明细结果。"
        ),
    }


@_spec("db_objects", "大表统计")
def _s_obj_large(snap, item):
    lt = snap.get("large_tables", [])
    top = _top(lt, "total_mb")
    return {
        "conclusion": (
            f"共识别 {len(lt)} 张占用空间较大的表，最大的是 "
            f"{top.get('database_name') if top else '-'}.{top.get('schema_name') if top else '-'}.{top.get('table_name') if top else '-'}"
            f"（{_n(top.get('total_mb')) if top else '-'} MB / {_n(top.get('row_count')) if top else '-'} 行）。"
            "大表的关键不在「大」本身，而在于它的维护动作（索引重建、统计信息更新、分区切换）"
            "需要更长窗口或在线方案；本项用于识别哪些表必须走特殊维护流程。"
        ),
        "evidence": [
            f"大表 {len(lt)} 张，合计 {_n(_sum(lt, 'total_mb'))} MB，最少的也有 {_n(min([_f(x.get('total_mb')) or 0 for x in lt] or [0]))} MB",
            "空间前三：" + "、".join(f"{r.get('database_name')}.{r.get('table_name')} {_n(r.get('total_mb'))} MB"
                                  for r in sorted(lt, key=lambda r: -(_f(r.get("total_mb")) or 0))[:3]),
        ],
        "note": (
            "空间为表及其索引的总分配尺寸（含已用与未使用页）；"
            "行数来自分区统计的行数汇总，是统计信息估算值，与精确 count(*) 可能有偏差。"
        ),
    }


@_spec("db_objects", "快照隔离")
def _s_obj_rcsi(snap, item):
    rcsi = snap.get("database_rcsi", [])
    on = [r for r in rcsi if str(r.get("RCSI_enabled")).strip() in ("1", "True", "true")]
    si = [r for r in rcsi if str(r.get("SI_state") or "").strip().upper() in ("ON", "1", "TRUE")]
    return {
        "conclusion": (
            f"已取得 {len(rcsi)} 个数据库的行版本隔离配置：启用 RCSI 的 {len(on)} 个，启用快照隔离（SI）的 {len(si)} 个。"
            "RCSI 让读操作不再阻塞写、写不再阻塞读，代价是版本记录落在 tempdb；"
            "启用它会改变应用可观察到的阻塞行为，必须与应用侧的隔离级别预期保持一致 —— "
            "特别是依赖「读加锁」来保证一致性的老应用。"
        ),
        "evidence": [
            f"数据库 {len(rcsi)} 个，RCSI 启用 {len(on)} 个，SI 启用 {len(si)} 个",
            "启用 RCSI 的库：" + ("、".join(str(r.get("database_name")) for r in on[:6]) or "无"),
        ],
        "note": "RCSI 为数据库级配置（sys.databases.is_read_committed_snapshot_on）；开启后 tempdb 的版本存储压力会上升，两者需联合观察。",
    }


@_spec("db_objects", "陈旧统计信息")
def _s_obj_stale(snap, item):
    st = snap.get("stale_statistics", [])
    top = _top(st, "modification_counter")
    return {
        "conclusion": (
            f"{len(st)} 项统计信息的更新滞后或累计变更次数偏高，变更次数最多的是 "
            f"{top.get('database_name') if top else '-'}.{top.get('table_name') if top else '-'}"
            f"（{_n(top.get('modification_counter')) if top else '-'} 次变更，行数 {_n(top.get('row_count')) if top else '-'}）。"
            "统计信息陈旧会让优化器低估或高估行数，进而选错连接方式或访问路径；"
            "这类问题不会报错，只表现为「某些查询偶发变慢」。"
        ),
        "evidence": [
            f"统计项 {len(st)} 项，涉及表 {len({str(x.get('table_name')) for x in st})} 张",
            f"变更次数合计 {_n(_sum(st, 'modification_counter'))}",
        ],
        "note": (
            "数据来自 sys.dm_db_stats_properties（需启用 -IncludeDeepChecks）；"
            "「变更次数」为统计信息上次更新后的累计修改行数，与「行数」对比可判断相对变动比例。"
        ),
    }


@_spec("db_objects", "索引碎片")
def _s_obj_frag(snap, item):
    fr = snap.get("fragmented_indexes", [])
    top = _top(fr, "avg_fragmentation_in_percent")
    return {
        "conclusion": (
            f"{len(fr)} 个索引的碎片率超过阈值，最高 {_pct(top.get('avg_fragmentation_in_percent')) if top else '-'}"
            f"（{top.get('database_name') if top else '-'}.{top.get('table_name') if top else '-'}"
            f".{top.get('index_name') if top else '-'}）。"
            "碎片率只是一半依据：页数少的索引重建收益有限，"
            "是否处理要同时看碎片率与页数——通常页数上万且碎片率高的索引才值得重建。"
        ),
        "evidence": [
            f"超阈值索引 {len(fr)} 个，涉及表 {len({str(x.get('table_name')) for x in fr})} 张",
            f"碎片率 ≥30% 的 {sum(1 for x in fr if (_f(x.get('avg_fragmentation_in_percent')) or 0) >= 30)} 个，"
            f"页数 ≥1000 的 {sum(1 for x in fr if (_f(x.get('page_count')) or 0) >= 1000)} 个",
        ],
        "note": (
            "数据来自 sys.dm_db_index_physical_stats（需启用 -IncludeDeepChecks）；"
            "碎片率判定受页数影响：小表（页数少）的碎片率天然偏高，重建后很快又会变高。"
        ),
    }


# --------------------------------------------------------------------------- 10 安全与身份

@_spec("security", "安全检查")
def _s_sec_checks(snap, item):
    sc = (snap.get("security") or {}).get("security_checks", [])
    passed = [c for c in sc if str(c.get("result") or "").upper() == "PASS"]
    return {
        "conclusion": (
            f"共执行 {len(sc)} 项安全基线检查，通过 {len(passed)} 项、未通过 {len(sc) - len(passed)} 项。"
            "检查覆盖 sa 账号状态、空密码登录、xp_cmdshell 与 OLE 自动化等高风险面；"
            "未通过项的判定标准是「该能力已开启」，是否可接受取决于业务是否真的需要它 —— "
            "需要时应有明确的例外审批与访问控制。"
        ),
        "evidence": [
            f"检查项 {len(sc)} 项，通过 {len(passed)} 项",
            "未通过项：" + ("、".join(str(c.get("check_name")) for c in sc if str(c.get("result") or "").upper() != "PASS") or "无"),
        ],
        "note": "检查结果为配置快照判定，不代表这些能力已被实际调用；具体调用痕迹需查 SQL Server 错误日志与审计记录。",
    }


@_spec("security", "SQL登录")
def _s_sec_logins(snap, item):
    logins = (snap.get("security") or {}).get("sql_logins", [])
    disabled = [x for x in logins if x.get("is_disabled")]
    sql_auth = [x for x in logins if str(x.get("type_desc") or "").upper().startswith("SQL")]
    return {
        "conclusion": (
            f"实例共有 {len(logins)} 个登录账号，其中 SQL 认证账号 {len(sql_auth)} 个、已禁用 {len(disabled)} 个。"
            "账号清单是权限复核的起点："
            "应重点确认 SQL 认证账号的密码策略（is_policy_checked / is_expiration_checked）"
            "与默认数据库是否正确，以及是否存在不该保留的历史账号。"
        ),
        "evidence": [
            f"登录账号 {len(logins)} 个（SQL 认证 {len(sql_auth)} 个、已禁用 {len(disabled)} 个）",
            "账号示例：" + "、".join(str(x.get("name")) for x in logins[:8]),
        ],
        "note": (
            "sys.sql_logins 只包含 SQL 认证登录，Windows 认证登录需查 sys.server_principals；"
            "本表展示前 50 条，完整清单以采集包内结果为准。"
        ),
    }


@_spec("security", "sysadmin成员")
def _s_sec_sysadmin(snap, item):
    sa = (snap.get("security") or {}).get("sysadmin_members", [])
    enabled = [x for x in sa if not x.get("is_disabled")]
    return {
        "conclusion": (
            f"sysadmin 固定服务器角色共有 {len(sa)} 个成员，其中启用状态 {len(enabled)} 个。"
            "该角色等价于实例完全控制权（可读写任意数据、执行任意命令、关闭实例）；"
            "成员应保持最小集——尤其需要确认是否存在业务应用账号、离职人员账号"
            "或 BUILTIN\\Administrators 这类宽泛的 Windows 组。"
        ),
        "evidence": [
            f"sysadmin 成员 {len(sa)} 个（启用 {len(enabled)} 个）",
            "成员：" + ("、".join(f"{x.get('name')}（{x.get('type_desc') or '-'}）" for x in sa[:8]) or "无"),
        ],
        "note": "成员清单来自 sys.server_role_members；SQL Server 2008 起 BUILTIN\\Administrators 不再默认加入 sysadmin，但可能被手工加回。",
    }


@_spec("security", "孤立用户")
def _s_sec_orphan(snap, item):
    orp = snap.get("orphaned_users", [])
    dbs = {str(x.get("database_name")) for x in orp}
    return {
        "conclusion": (
            f"发现 {len(orp)} 个孤立用户，分布在 {len(dbs)} 个数据库中。"
            "孤立用户是指数据库内存在用户、但对应的服务器级登录已被删除或改名，"
            "此时该用户无法登录，却仍保留着库内权限条目。"
            "常见成因是数据库还原到其他实例，或登录改名后未同步（sp_change_users_login / ALTER USER）。"
        ),
        "evidence": [
            f"孤立用户 {len(orp)} 个，涉及数据库 " + "、".join(sorted(dbs)[:6]),
            "示例：" + "、".join(f"{x.get('database_name')}.{x.get('user_name')}" for x in orp[:6]),
        ],
        "note": (
            "数据来自 sys.database_principals 与 sys.server_principals 的 SID 比对（需启用 -IncludeDeepChecks）；"
            "孤立用户不一定是安全问题——若其权限很小可以保留，但需要知道它的存在。"
        ),
    }


# --------------------------------------------------------------------------- 11 高可用与复制

@_spec("ha", "AlwaysOn可用性组")
def _s_ha_ag(snap, item):
    ag = snap.get("availability_replicas", [])
    groups = {str(a.get("availability_group")) for a in ag}
    healthy = [a for a in ag if str(a.get("synchronization_health_desc") or "").upper() == "HEALTHY"]
    return {
        "conclusion": (
            f"实例参与 {len(groups)} 个可用性组、共 {len(ag)} 条副本-数据库映射，"
            f"同步健康为 HEALTHY 的 {len(healthy)} 条。"
            "可用性组的健康度由官方口径给出：同步提交副本追平为 HEALTHY，"
            "仍在同步中则为 PARTIALLY_HEALTHY（异步提交副本处于同步中仍算 HEALTHY）。"
            "本表是多副本架构下判断「某库是否真正有冗余」的第一入口。"
        ),
        "evidence": [
            f"可用性组 {len(groups)} 个：" + "、".join(sorted(groups)[:6]),
            f"数据库 {len({str(a.get('database_name')) for a in ag})} 个，副本 {len({str(a.get('replica_server_name')) for a in ag})} 个",
        ],
        "note": (
            "健康度字段 synchronization_health_desc 已把提交模式揉进判定："
            "同步提交 + SYNCHRONIZING 会落 PARTIALLY_HEALTHY，异步提交 + SYNCHRONIZING 仍落 HEALTHY。"
            "因此判断延迟必须看「副本同步与延迟」里的延迟秒与发送/重做队列，不能只看本表。"
        ),
    }


@_spec("ha", "副本同步与延迟")
def _s_ha_sync(snap, item):
    ag = snap.get("availability_replicas", [])
    lagged = [a for a in ag if (_f(a.get("replica_lag_seconds") if a.get("replica_lag_seconds") is not None else a.get("secondary_lag_seconds")) or 0) > 0]
    async_repl = [a for a in ag if str(a.get("availability_mode_desc") or "").upper().startswith("ASYNCHRONOUS")]
    return {
        "conclusion": (
            f"{len(ag)} 条副本数据库映射中，提交模式为异步的 {len(async_repl)} 条；"
            f"延迟大于 0 的有 {len(lagged)} 条"
            + ("，最大延迟 " + _n(max((_f(a.get('replica_lag_seconds') if a.get('replica_lag_seconds') is not None else a.get('secondary_lag_seconds')) or 0) for a in ag)) + " 秒" if lagged else "")
            + "。提交模式决定延迟的含义："
            "同步提交下延迟意味着主库提交要等副本确认（直接影响业务响应时间），"
            "异步提交下延迟只代表灾备的数据丢失窗口（RPO）——两者完全不是一回事，必须分开读。"
        ),
        "evidence": [
            f"异步提交 {len(async_repl)} 条 / 同步提交 {len(ag) - len(async_repl)} 条",
            "发送队列与重做队列（MB）：" + ("、".join(
                f"{a.get('database_name')} {_n(a.get('log_send_queue_mb'), 1)}/{_n(a.get('redo_queue_mb'), 1)}" for a in ag[:5]) or "无"),
        ],
        "note": (
            "延迟字段优先取 replica_lag_seconds，缺值时回退 secondary_lag_seconds（不同版本字段名不同）；"
            "发送队列持续非零说明网络或副本写入跟不上，重做队列持续非零说明副本端的重做线程是瓶颈。"
        ),
    }


@_spec("ha", "副本连接与状态")
def _s_ha_conn(snap, item):
    ag = snap.get("availability_replicas", [])
    bad = [a for a in ag if str(a.get("connected_state_desc") or "").upper() not in ("CONNECTED", "")]
    return {
        "conclusion": (
            f"{len(ag)} 条副本记录中，连接状态非 CONNECTED 的 {len(bad)} 条"
            + ("：" + "、".join(f"{a.get('replica_server_name')}（{a.get('connected_state_desc')}）" for a in bad[:4]) if bad else "，全部副本连接正常")
            + "。连接状态为 DISCONNECTED 的副本无法接收主库日志，"
            "此时该副本的同步延迟会持续增长，直到连接恢复后追赶。"
        ),
        "evidence": [
            f"副本记录 {len(ag)} 条，连接异常 {len(bad)} 条",
            "运行状态分布：" + (_dist(ag, "operational_state_desc") or "未采集"),
        ],
        "note": "运行状态（operational_state_desc）反映副本自身的运行情况（如 ONLINE / PENDING_FAILOVER），与同步健康度是两个不同维度。",
    }


@_spec("ha", "数据库镜像")
def _s_ha_mirror(snap, item):
    mi = snap.get("database_mirroring", [])
    active = [m for m in mi if str(m.get("mirroring_state_desc") or "").upper() not in ("", "NULL", "DISABLED")]
    return {
        "conclusion": (
            f"{len(mi)} 个数据库配置了数据库镜像，其中处于活动镜像状态的 {len(active)} 个。"
            "数据库镜像在 SQL Server 2012 起已被标记为弃用特性，新架构应评估迁移到 AlwaysOn 可用性组；"
            "若仍在生产使用，需确认见证服务器（Witness）与安全级别设置符合当前的自动故障转移预期。"
        ),
        "evidence": [
            f"镜像数据库 {len(mi)} 个，活动 {len(active)} 个",
            "角色分布：" + (_dist(mi, "mirroring_role_desc") or "未采集"),
        ],
        "note": "安全级别为 FULL 时需见证服务器才能自动故障转移，OFF 则为异步高性能模式（无自动转移）。",
    }


@_spec("ha", "日志传送")
def _s_ha_logship(snap, item):
    ls = snap.get("log_shipping", [])
    delayed = [x for x in ls if (_f(x.get("minutes_since_last_action")) or 0) > 60]
    return {
        "conclusion": (
            f"{len(ls)} 条日志传送监控记录，其中最近一次动作距今超过 60 分钟的 {len(delayed)} 条。"
            "日志传送是单向的异步方案（主库备份 → 复制 → 副本还原三段作业），"
            "任一环节中断都会导致副本数据陈旧；延迟分钟数是判断链路是否健康的直接指标。"
        ),
        "evidence": [
            f"监控记录 {len(ls)} 条，延迟 >60 分钟 {len(delayed)} 条",
            "角色分布：" + (_dist(ls, "monitor_role") or "未采集"),
        ],
        "note": "数据来自 msdb.dbo.log_shipping_monitor_*；日志传送的告警默认由各段作业写入 msdb，需确认作业本身在正常运行。",
    }


@_spec("ha", "高可用状态")
def _s_ha_status(snap, item):
    h = (snap.get("ha_status") or [{}])[0]
    on = str(h.get("hadr_enabled")).strip() in ("1", "True", "true")
    return {
        "conclusion": (
            f"实例 HADR {('已启用' if on else '未启用')}，可用性组 {_n(h.get('availability_group_count'))} 个、"
            f"副本 {_n(h.get('availability_replica_count'))} 个、组内数据库 {_n(h.get('ag_database_count'))} 个；"
            f"实例{'处于' if str(h.get('is_clustered')).strip() in ('1','True','true') else '不处于'}群集。"
            "HADR 未启用时本实例不具备 AlwaysOn 能力；"
            "已启用但没有可用性组，说明功能已打开但未实际部署，"
            "此时该实例的容灾能力仍依赖备份与外部方案。"
        ),
        "evidence": [
            f"hadr_enabled={h.get('hadr_enabled')}，availability_group_count={_n(h.get('availability_group_count'))}",
            f"availability_replica_count={_n(h.get('availability_replica_count'))}，ag_database_count={_n(h.get('ag_database_count'))}",
        ],
        "note": "数据来自 SERVERPROPERTY('IsHadrEnabled') 与 sys.availability_* 视图，是实例级能力与配置的汇总。",
    }


@_spec("ha", "复制拓扑")
def _s_ha_repl(snap, item):
    repl = snap.get("replication", [])
    kinds = _dist(repl, "record_type")
    err = [r for r in repl if (_f(r.get("error_count")) or 0) > 0]
    return {
        "conclusion": (
            f"分发库中共有 {len(repl)} 条复制元数据记录，类型分布为 {kinds or '未采集'}；"
            f"其中记录过错误的 {len(err)} 条。"
            "复制元数据保存在分发库里（MSpublications / MSsubscriptions / MSrepl_errors 等），"
            "不在 msdb；只有本实例被配置为分发服务器时这些表才有内容。"
        ),
        "evidence": [
            f"记录 {len(repl)} 条，错误记录 {len(err)} 条",
            "状态分布：" + (_dist(repl, "status_desc") or "未采集"),
        ],
        "note": "本项只反映分发库中的元数据与错误记录；复制链路的实时延迟需查分发代理会话历史（复制监视器）。",
    }


@_spec("ha", "复制配置")
def _s_ha_repl_old(snap, item):
    repl = snap.get("replication", [])
    return {
        "conclusion": (
            f"msdb 中登记了 {len(repl)} 条复制发布/订阅配置。"
            "注意：较新版本的采集器改为从分发库读取复制元数据（分发库才是元数据的实际存放处），"
            "本项为旧采集包格式的回退渲染路径。"
        ),
        "evidence": [f"复制配置 {len(repl)} 条"],
        "note": "本项为 v1.2 之前的采集包格式；若新包仍出现本项，说明复制元数据采集未命中分发库。",
    }


# --------------------------------------------------------------------------- 12 作业与错误日志

@_spec("ops", "失败作业(近24h)")
def _s_ops_failed(snap, item):
    fj = snap.get("failed_jobs", [])
    return {
        "conclusion": (
            f"近 24 小时内 {len(fj)} 个作业运行失败，涉及 "
            f"{len({str(j.get('job_name')) for j in fj})} 个作业。"
            "作业失败通常意味着对应的运维动作（备份、索引维护、数据同步）没有真正执行——"
            "它比「作业没配置」更隐蔽，因为调度器本身是正常的。"
        ),
        "evidence": [
            f"失败记录 {len(fj)} 条，作业 {len({str(j.get('job_name')) for j in fj})} 个",
            "示例：" + "、".join(str(j.get("job_name")) for j in fj[:5]),
        ],
        "note": "失败判定基于 msdb.dbo.sysjobhistory 中最近一次运行结果；重试成功的作业仍会留下失败记录，需看最终结果与重试次数。",
    }


@_spec("ops", "作业清单")
def _s_ops_jobs(snap, item):
    jobs = snap.get("agent_jobs", [])
    enabled = [j for j in jobs if j.get("enabled")]
    failed = [j for j in jobs if str(j.get("last_run_status_desc") or "").upper() not in ("SUCCEEDED", "")]
    return {
        "conclusion": (
            f"实例共 {len(jobs)} 个 SQL Agent 作业，启用 {len(enabled)} 个、禁用 {len(jobs) - len(enabled)} 个；"
            f"最近一次运行结果为失败或异常的 {len(failed)} 个。"
            "作业清单的价值在于核对「该有的运维作业是否都存在且启用」——"
            "备份、索引维护、统计更新、`CHECKDB` 这些动作如果只存在于文档而不在作业里，"
            "实际上就没有人在执行；同时注意作业依赖 SQL Server 代理服务处于运行状态。"
        ),
        "evidence": [
            f"作业 {len(jobs)} 个（启用 {len(enabled)} 个、禁用 {len(jobs) - len(enabled)} 个）",
            "最近运行结果分布：" + (_dist(jobs, "last_run_status_desc") or "未采集"),
        ],
        "note": (
            "「最近耗时」列取自 sysjobhistory.run_duration，该字段是 HHMMSS 整数格式而非秒数，"
            "报告已按 h/m/s 换算。作业清单来自 msdb.dbo.sysjobs，只覆盖本实例的 Agent 作业。"
        ),
    }


@_spec("ops", "错误日志")
def _s_ops_errorlog(snap, item):
    errs = snap.get("error_log_summary", [])
    top = _top(errs, "event_count")
    sev_high = [e for e in errs if (_f(e.get("severity")) or 0) >= 20]
    return {
        "conclusion": (
            f"错误日志中共汇总 {len(errs)} 类错误记录，出现次数最多的是错误 "
            f"{top.get('error_number') if top else '-'}（{_n(top.get('event_count')) if top else '-'} 次）"
            + (f"，其中 Severity ≥20 的 {len(sev_high)} 类" if sev_high else "")
            + "。Severity 20 及以上属于系统级错误（如页校验失败、IO 错误），"
            "通常伴随数据损坏或硬件隐患，需要立即结合 DBCC 与硬件日志排查；"
            "低 Severity 的重复记录更多是应用或连接层的噪音，用于估算噪声基线。"
        ),
        "evidence": [
            f"错误类型 {len(errs)} 类，累计出现 {_n(_sum(errs, 'event_count'))} 次",
            "次数前三：" + "、".join(f"错误 {r.get('error_number') or '-'} × {_n(r.get('event_count'))}"
                                  for r in sorted(errs, key=lambda r: -(_f(r.get("event_count")) or 0))[:3]),
        ],
        "note": (
            "采集端遍历轮转的日志文件（#0 ~ #9）并做中英双语筛选（中文实例写「错误: 14151，严重性: 18」），"
            "因此中文实例不会漏采。若不启用 -IncludeDeepChecks，本项只汇总当前活动日志。"
        ),
    }


@_spec("ops", "步骤明细")
def _s_ops_steps(snap, item):
    steps = snap.get("job_steps", [])
    failed = [s for s in steps if str(s.get("last_run_outcome_desc") or "").upper() not in ("SUCCEEDED", "")]
    return {
        "conclusion": (
            f"共 {len(steps)} 个作业步骤，按「最近失败优先」排序；其中上次运行结果非成功的 {len(failed)} 个。"
            "作业清单只给出作业级结果，「失败在哪一步」答不了——"
            "跨多步的作业（备份 → 校验 → 上传）某一步失败后，后续步骤是否继续执行由「失败动作」列决定，"
            "这一列直接决定问题是会放大还是会被自动兜住。"
        ),
        "evidence": [
            f"步骤 {len(steps)} 个，非成功状态 {len(failed)} 个",
            "涉及作业：" + "、".join(sorted({str(s.get('job_name')) for s in steps})[:6]),
        ],
        "note": "「上次耗时」同样是 sysjobhistory.run_duration（HHMMSS 整数），报告已换算；「失败动作」为该步骤失败后 Agent 采取的流转动作。",
    }


# --------------------------------------------------------------------------- 13 实例关键配置

@_spec("config", "关键配置项")
def _s_config(snap, item):
    cfg = snap.get("configurations", [])
    dyn = [c for c in cfg if c.get("is_dynamic")]
    adv = [c for c in cfg if c.get("is_advanced")]
    return {
        "conclusion": (
            f"共采集 {len(cfg)} 项关键配置，其中动态生效 {len(dyn)} 项、需重启实例 {len(cfg) - len(dyn)} 项"
            + (f"，标记为「高级选项」的 {len(adv)} 项" if adv else "")
            + "。动态与静态的区别直接决定变更方式："
            "动态项用 sp_configure 改完立即生效，静态项改完必须重启实例才起作用——"
            "把静态项当成改完就生效，是配置类变更最常见的误解。"
        ),
        "evidence": [
            f"配置项 {len(cfg)} 项（动态 {len(dyn)} 项、静态 {len(cfg) - len(dyn)} 项）",
            "涉及：" + "、".join(str(c.get("name")) for c in cfg[:8]),
        ],
        "note": (
            "本表只列采集端预置的关键配置项（内存上限、并行度、开销阈值、备份压缩等），"
            "不是 sys.configurations 的全量转储；未列入的配置项需按需查询。"
        ),
    }


# --------------------------------------------------------------------------- 14 采集执行明细

@_spec("collection_detail", "采集执行明细")
def _s_collect_detail(snap, item):
    mods = snap.get("collection", {}).get("module_status", [])
    ok = [m for m in mods if str(m.get("status") or "").lower() in _OK_STATUS]
    nodata = [m for m in mods if str(m.get("status") or "").lower() == "success_no_data"]
    bad = [m for m in mods if str(m.get("status") or "").lower() not in _OK_STATUS]
    elapsed = [_f(m.get("elapsed_ms")) for m in mods]
    total_ms = sum(v for v in elapsed if v is not None)
    slow = _top([{"n": m.get("name"), "e": _f(m.get("elapsed_ms")) or 0} for m in mods], "e")
    return {
        "conclusion": (
            f"本次采集共执行 {len(mods)} 个模块：成功 {len(ok)} 个（其中无数据 {len(nodata)} 个）、"
            f"未成功 {len(bad)} 个，累计耗时 {_n(total_ms / 1000, 1)} 秒"
            + (f"，最慢的是 {slow.get('n')}（{_n(slow.get('e'))} ms）" if slow else "") + "。"
            "逐模块状态是判断报告可信边界的基础："
            "「成功（无数据）」说明该项确实查了、只是没有记录（如无阻塞、无失败作业），"
            "与「失败」是完全不同的两件事，不能合并成「没问题」。"
        ),
        "evidence": [
            f"模块 {len(mods)} 个：成功 {len(ok)} 个 / 无数据 {len(nodata)} 个 / 未成功 {len(bad)} 个",
            "未成功模块：" + ("、".join(f"{m.get('name')}（{_STATUS_CN.get(str(m.get('status') or '').lower(), m.get('status'))}）" for m in bad[:6]) or "无"),
        ],
        "note": (
            "逐模块状态由采集端登记；「耗时ms」为 - 表示该模块未计时，不按 0 秒处理。"
            "完整度百分比只把 success / success_no_data 计入分子，"
            "skipped 与 not_applicable（如远程采集时的 OS 指标）不计入分母，避免把「有意不采」误报成采集不完整。"
        ),
    }


# --------------------------------------------------------------------------- 装配

def _merge_note(note: str, item: dict[str, Any]) -> str:
    """把 spec 给的口径说明与「正文展示前 N 条，共 M 条」的截断提示拼在一起。

    截断提示只在「模块原始行数 > 正文实际渲染行数」时才加 —— 采集端已按 TOP N 取数的模块，
    原始行数就等于展示行数，此时不加提示（加了反而是误导：读者会以为还有更多明细可查）。
    """
    rc = (item.get("collection") or {}).get("row_count")
    shown = len((item.get("display") or {}).get("rows") or [])
    extra = ""
    if isinstance(rc, int) and isinstance(shown, int) and rc > shown > 0:
        extra = f"正文展示前 {shown} 条，共 {rc} 条；完整明细保留在分析结果中。"
    return " ".join(x for x in (note.strip(), extra) if x)


def decorate_sections(sections: list[dict[str, Any]], snapshot: dict[str, Any],
                      perf_summary: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """给每个检查项补默认的「口径说明 + 分析结论」。

    只处理注册表里登记过的检查项；未登记的项保持原样（不硬塞套话，缺哪条会被守门测试抓出来）。
    采集未成功时一律落 ``not_evaluated`` 并说明原因 —— 这一条优先于任何文案，
    否则会出现「采集失败却写着已取得数据」的假陈述（AGENTS.md 禁止把未知写成已知）。
    """
    for section in sections or []:
        section_id = str(section.get("section_id") or "")
        for item in section.get("items") or []:
            title = str(item.get("title") or "")
            spec = _SPECS.get((section_id, title))
            if spec is None:
                continue
            display = item.setdefault("display", {})
            collection = item.get("collection") or {}

            status_key = str(collection.get("status") or "").lower()
            if status_key not in _OK_STATUS:
                display["note"] = _merge_note(
                    f"本项采集状态为「{_STATUS_CN.get(status_key, status_key or '未知')}」，未取得可用证据。",
                    item,
                )
                item["analysis"] = {
                    "status": "not_evaluated",
                    "conclusion": (
                        f"本项采集未取得有效结果（采集状态：{_STATUS_CN.get(status_key, status_key or '未知')}），"
                        "本次不作判断；缺口已登记在本报告「采集缺口」章节。"
                    ),
                    "evidence": [f"模块状态={status_key or '未知'}"],
                    "recommendation": "排查该模块的采集失败原因后重新采集，再按同一口径复核本项。",
                }
                continue

            try:
                payload = spec(snapshot, item) or {}
            except Exception as exc:  # noqa: BLE001 —— 文案生成失败不能连累整份报告，但必须留痕
                item["analysis"] = {
                    "status": "not_evaluated",
                    "conclusion": f"本项分析结论生成失败（{type(exc).__name__}: {exc}），本次不作判断。",
                    "evidence": [],
                    "recommendation": "",
                }
                continue

            conclusion = str(payload.get("conclusion") or "").strip()
            if not conclusion:
                continue
            display["note"] = _merge_note(str(payload.get("note") or ""), item)
            item["analysis"] = {
                "status": str(payload.get("status") or "normal"),
                "conclusion": conclusion,
                "evidence": [str(x) for x in (payload.get("evidence") or []) if str(x).strip()],
                "recommendation": str(payload.get("recommendation") or ""),
            }
    return sections


# --------------------------------------------------------------------------- 待客户确认事项

# 与风险登记册的分工：登记册答「是什么风险、怎么改」，本表答「哪几件事必须由客户拍板、
# 或必须由客户提供外部证据」——共享引擎 build_pending_confirmations_professional 的
# 口径注释写得很明确。因此这里只收两类规则：
#   1) 判断本身依赖客户侧信息（备份由谁发起、有没有定时作业、合规对备份加密的要求）；
#   2) 整改涉及业务语义或变更窗口，DBA 不能单方面决定（sa 收敛、扩展存储过程去留、
#      隔离级别、版本升级、镜像迁移）。
# 容量、参数调优、对象治理属于技术性整改项，一律不进本表 —— 它们在风险登记册与
# 分级整改计划里已有位置，重复登记只会稀释「这件事必须客户拍板」的分量。
CONFIRMATION_TEMPLATES: dict[str, tuple[str, str]] = {
    "BACKUP.NO_BACKUP": (
        "备份归属与恢复演练",
        "本实例存在从未备份的数据库。请确认备份由谁发起（本地作业 / 备份平台 / 存储侧快照）、"
        "备份文件落在何处，并提供最近 3 次成功记录与至少 1 次恢复演练记录。"
        "「有备份」以能恢复为准，不以存在备份任务为准；若备份由外部平台负责，"
        "请一并说明该平台是否会在本实例登记备份记录（未登记会让巡检每次重复告警）。",
    ),
    "BACKUP.FULL": (
        "完整备份周期与保留要求",
        "存在完整备份超期或缺失的数据库。请确认业务可接受的 RPO（最多可丢多少数据）"
        "与备份保留期要求，据此确定完整 / 差异 / 日志备份的频率组合。"
        "FULL 恢复模式下缺少完整备份会导致日志无法截断，磁盘占用会持续增长 —— "
        "这不只是恢复能力问题，也是容量问题。",
    ),
    "BACKUP.LOG": (
        "日志备份链的归属",
        "存在日志备份超期或缺失的数据库。请确认日志备份是否由外部平台执行；"
        "若确实由本实例作业执行，请提供可安排的维护窗口。"
        "日志备份中断后，数据库会停在崩溃恢复点上，无法做时间点恢复。",
    ),
    "BACKUP.CHECKSUM": (
        "备份校验策略",
        "存在未启用备份校验和的备份。校验和能在备份时发现页级损坏，是「备份可恢复」的"
        "前置条件。请确认是否要求所有备份开启校验和（开启会带来额外 CPU 开销），"
        "以及是否需要同步开启备份后自动验证。",
    ),
    "BACKUP.ENCRYPTION": (
        "备份加密与合规要求",
        "存在未加密的备份文件。请确认客户侧是否存在数据离场、介质外送或等保 / 行业合规"
        "对备份加密的强制要求；若需要，请同时说明证书或非对称密钥的保管责任方 —— "
        "密钥丢失等同于备份不可恢复。",
    ),
    "DB.CHECKDB": (
        "完整性检查窗口与频次",
        "数据库完整性检查（DBCC CHECKDB）不满足周期。请确认可安排的检查窗口与可接受的"
        "执行时长；对无法整体检查的大库，需要确认是否接受按物理分区拆分检查的方案。"
        "完整性检查是发现静默数据损坏的唯一手段，未做不等于没有损坏。",
    ),
    "DB.TRUSTWORTHY": (
        "TRUSTWORTHY 是否有意启用",
        "存在启用 TRUSTWORTHY 的数据库。该选项允许数据库内的模块以更高权限执行（可模拟"
        "数据库所有者跨越边界），请确认业务是否确实依赖它；若不是，关闭前需确认无 CLR、"
        "无跨库所有权链依赖。",
    ),
    "DB.QUERY_STORE": (
        "Query Store 是否启用",
        "存在未启用 Query Store 的用户库。Query Store 是事后定位「某个查询什么时候开始变慢」"
        "的唯一内建手段，代价是少量写入与空间开销。请确认是否统一启用，以及是否需要"
        "设定保留期与空间上限。",
    ),
    "DB.RCSI": (
        "隔离级别是否为应用侧要求",
        "存在未启用 RCSI（读已提交快照）的数据库。RCSI 让读不阻塞写，但会改变应用可观察到的"
        "阻塞语义并使用 tempdb 版本存储。请确认是否要求开启，以及应用是否存在依赖读锁的"
        "历史逻辑 —— 这属于业务语义决策，不由 DBA 单方面变更。",
    ),
    "DB.PAGE_VERIFY": (
        "页校验方式变更窗口",
        "存在页校验设置可优化的数据库。从 TORN_PAGE_DETECTION 改为 CHECKSUM 需在数据库"
        "离线期间完成一次全库扫描，请确认可安排的维护窗口；该变更能提升对页级损坏的检出能力。",
    ),
    "DB.AUTO_OPTIONS": (
        "自动关闭/自动收缩等选项的取舍",
        "存在启用自动关闭、自动收缩或自动更新统计异步等选项的数据库。"
        "自动关闭会反复冷启缓存、自动收缩会造成文件碎片与并发开销，通常建议关闭；"
        "但若为特定业务（如极低频使用的库）有意保留，请确认后按现状登记，避免下次巡检重复提出。",
    ),
    "INSTANCE.SERVICE_STARTUP": (
        "SQL Server 代理服务是否需要常驻",
        "SQL Server 代理服务的启动类型与运行状态需要确认。代理负责执行所有 Agent 作业"
        "（备份、索引维护、CHECKDB）；若本实例确实配置了定时作业，代理必须为自动启动并"
        "保持运行，否则作业静默不执行。请确认本实例是否依赖任何定时作业。",
    ),
    "INSTANCE.VERSION_SUPPORT": (
        "版本与补丁升级窗口",
        "当前 SQL Server 版本不在主流支持周期内或缺少累积更新。请确认客户的升级策略与"
        "可安排的停机窗口，以及是否有业务系统对版本有兼容性约束（如第三方 ISV 认证版本）。",
    ),
    "SEC.SA_ENABLED": (
        "sa 账号的使用与收敛",
        "sa 账号处于启用状态或缺少限制。请确认是否有业务 / 第三方工具仍在用 sa 连接；"
        "若否，建议禁用 sa 并改用具名账号 + 最小权限。这类收敛会改变连接配置，"
        "需要应用侧配合确认。",
    ),
    "SEC.SA_ACCOUNT_DISABLED": (
        "sa 账号的替代方案",
        "sa 账号的安全配置存在改进项。请确认现有应用的连接账号，"
        "以便在收敛 sa 的同时保证应用不受影响；变更前需要一份完整的账号清单核对。",
    ),
    "SEC.XP_CMDSHELL_ENABLED": (
        "xp_cmdshell 是否业务必需",
        "xp_cmdshell 处于启用状态，它允许在数据库内执行操作系统命令，是提权的主要路径。"
        "请确认是否有业务确实依赖它；若不是，建议关闭。若确需保留，请确认已有"
        "调用来源限制与审计记录。",
    ),
    "SEC.OLE_AUTOMATION_ENABLED": (
        "OLE 自动化是否业务必需",
        "OLE 自动化存储过程处于启用状态，它允许数据库进程加载并执行 COM 对象，"
        "历史上被用于提权。请确认是否有业务依赖；若无需，建议关闭。",
    ),
    "SEC.SYSADMIN_COUNT": (
        "sysadmin 成员的最小集核对",
        "sysadmin 固定服务器角色成员较多。该角色等价于实例完全控制权，请客户侧核对"
        "成员清单（特别是业务账号、离职人员账号与宽泛的 Windows 组），确认哪些必须保留。"
        "成员判定涉及人员与组织关系，不能由 DBA 单方面增减。",
    ),
    "SEC.LINKED_SERVER": (
        "链接服务器的用途与凭据",
        "存在链接服务器配置。请确认每个链接服务器的业务用途、对端归属与凭据保管方式；"
        "无用的链接服务器是横向移动的现成通道，建议按用途核对后清理。",
    ),
    "SEC.AUTHENTICATION_MODE": (
        "认证模式是否符合要求",
        "实例认证模式需要确认。混合模式允许 SQL 认证登录，便于运维但扩大了口令攻击面；"
        "请确认是否为业务（如第三方工具、跨平台应用）所必需。",
    ),
    "AGENT.JOB_FAILED": (
        "作业失败的业务影响",
        "存在最近失败的 SQL Agent 作业。请确认这些作业承载的运维动作是否已在别处补偿"
        "（手工执行、外部平台执行），以及是否需要在变更窗口内重跑以补齐。",
    ),
    "AGENT.JOB_DISABLED": (
        "停用作业的归属",
        "存在已停用的 SQL Agent 作业。请确认停用是有意为之（如业务下线、替代方案上线）"
        "还是历史遗留；对确认无用的作业建议按变更流程归档删除，避免下次巡检重复提出。",
    ),
    "AGENT.JOB_OVERDUE": (
        "作业超期未运行的排期",
        "存在长期未运行的 SQL Agent 作业。请确认其预期执行周期，以及是否存在"
        "「配了但没人看」的情况 —— 这类作业通常在真正需要时（如灾备切换当天）才会被发现不生效。",
    ),
    "HA.MIRRORING": (
        "数据库镜像的迁移计划",
        "存在数据库镜像配置。数据库镜像自 SQL Server 2012 起已被标记为弃用，"
        "请确认是否有迁移到 AlwaysOn 可用性组的计划与时间表；若短期保留，"
        "需确认见证服务器与安全级别符合当前的自动故障转移预期。",
    ),
    "HA.LOG_SHIPPING": (
        "日志传送的运维归属",
        "存在日志传送配置。请确认三段作业（备份 / 复制 / 还原）的运维责任方，"
        "以及副本数据允许的最大陈旧时间（RPO）；这决定延迟告警的阈值该怎么设。",
    ),
    "HA.REPLICATION_CONFIG": (
        "复制拓扑的归属与用途",
        "存在复制配置。请确认发布 / 订阅的业务用途（报表分流、数据下发、跨系统同步）、"
        "责任方与可接受的延迟范围，作为后续监控与告警的基础。",
    ),
    "HA.REPLICATION_AGENT": (
        "复制代理异常的处理窗口",
        "复制代理存在异常记录。请确认是否可以安排窗口做一次完整的初始化 / 重做，"
        "以及业务侧对复制中断的容忍时长；复制链路修复往往需要暂停分发任务。",
    ),
    "HA.REPLICATION_ERROR": (
        "复制错误的取舍",
        "复制链路上存在错误记录。请确认这些错误是否已知并已被业务接受，"
        "或需要清理历史错误后重新观察；持续性错误会导致订阅端数据静默落后。",
    ),
    "ERRORLOG.SEVERE": (
        "系统级错误的硬件与 OS 侧核实",
        "错误日志中存在 Severity 20 及以上的系统级错误。这类错误通常指向存储、内存或"
        "文件系统层问题，需要客户侧提供同一时间段的**操作系统日志与存储阵列日志**"
        "才能定位；仅靠 SQL Server 侧日志无法区分是数据库还是硬件发起的问题。",
    ),
    "MEM.PRESSURE": (
        "实例内存上限的目标值",
        "存在内存利用率偏高或内存不足信号。请确认本实例是否与其它实例或应用共用主机、"
        "是否已安装其他 SQL Server 实例 —— 这决定 max server memory 该留多少给操作系统。"
        "参数取值取决于主机上还有什么，属于部署决策。",
    ),
    "TEMPDB.USAGE": (
        "tempdb 容量与增长规划",
        "tempdb 的使用特征需要确认。请确认是否有业务依赖大量排序、临时表或行版本隔离；"
        "这决定 tempdb 是否应按峰值预分配到固定大小（避免运行中自动增长造成等待），"
        "以及文件数与 CPU 核数的匹配方案。",
    ),
    "PERF.CONNECTION_FLOOD": (
        "连接来源与连接池配置",
        "连接数异常偏高。请确认连接来源（哪台应用服务器 / 哪个中间件）与连接池配置，"
        "以便区分是应用未释放连接还是确有并发需求；这类问题的处置在应用侧，"
        "不是数据库参数能解决的。",
    ),
}


def pending_confirmations(findings: list[dict[str, Any]],
                          instance: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """按已触发的规则派生「待客户确认事项」。

    规则未触发则不进表 —— 没触发就说明本实例没有该问题，不该把模板空转成一张
    「欢迎确认」清单。同一规则在多处命中时只登记一次。
    """
    node = str((instance or {}).get("server_name") or (instance or {}).get("instance_name") or "").strip()
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for finding in findings or []:
        rule_id = str(finding.get("rule_id") or "").strip()
        template = CONFIRMATION_TEMPLATES.get(rule_id)
        if not template or rule_id in seen:
            continue
        seen.add(rule_id)
        evidence = str(finding.get("evidence") or "").strip()
        items.append({
            "topic": template[0],
            "question": template[1],
            "node": node,
            "rule_id": rule_id,
            "evidence": [evidence] if evidence else [],
        })
    return items