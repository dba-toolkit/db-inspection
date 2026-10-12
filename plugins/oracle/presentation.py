"""Oracle 呈现层：把分析结果组织成报告模型（章节 / 风险台账 / 待确认事项）。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import re
from typing import Any

from .metrics import safe_float
from .package_adapter import filesystem_type, parse_instance_tag
from inspection_core.system_checks import (
    CANONICAL_RULE_IDS,
    format_bytes,
    is_persistent_fstype,
)


VERSION = "2.0.0"
CONTRACT = "oracle_inspection_report_model"
ANALYSIS_SCHEMA_VERSION = "2.0"

# OS check ids live in the shared layer; the report item id, the analysis
# lookup and the rule engine must all spell them the same way.
_RULE_TIME_SYNC = CANONICAL_RULE_IDS["time_sync"]
_RULE_CPU_PRESSURE = CANONICAL_RULE_IDS["cpu_pressure"]
_RULE_IOWAIT_PRESSURE = CANONICAL_RULE_IDS["iowait_pressure"]
_RULE_MEMORY_PRESSURE = CANONICAL_RULE_IDS["memory_pressure"]
_RULE_FILESYSTEM_USAGE = CANONICAL_RULE_IDS["filesystem_usage"]


# 「待客户确认事项」的模板，按**已触发的规则**派生（rule_id → 主题 / 待确认内容）。
# 刻意不重复判定：规则引擎已经回答「这是不是风险」，本表只回答「这件事要客户拍板什么」。
#
# 收录口径（与 MySQL 同一条线，见 plugins/mysql/presentation.py::CONFIRMATION_TEMPLATES）：
#   ① 需要业务语义决策的 —— 未开自动扩展是有意管控还是遗漏、哪些是高权限业务账号、
#      失败作业归谁维护，这些答案只在客户那边；
#   ② 数据库侧无法自证的 —— 备份可恢复性（库里看不到备份平台/存储快照）、坏块修复
#      能容忍多少数据损失；
#   ③ 需要外部资源或停机窗口的 —— 表空间/FRA 扩容要走采购与审批、开归档必须停库。
# **不收**纯技术整改项（redo 尺寸、控制文件冗余、日志成员数、统计信息收集、无效对象）：
# 那些直接改就行，问了只增加沟通成本，还会把清单做成第二个风险台账。
CONFIRMATION_TEMPLATES: dict[str, tuple[str, str]] = {
    "ORA.BACKUP.RECENT": (
        "备份归属与可恢复性",
        "实例内没有可确认的成功备份记录。请确认备份由谁发起（RMAN 本地脚本 / 备份平台 / "
        "存储侧快照），并提供最近 3 次成功记录与至少 1 次恢复演练记录 —— "
        "「有备份」以能恢复为准，不以存在任务配置为准。",
    ),
    "ORA.ARCHIVE.MODE": (
        "归档模式的启用窗口与备份策略",
        "数据库当前运行在 NOARCHIVELOG 模式，无法做在线备份与时间点恢复。开启归档需停机"
        "（MOUNT 状态执行），且归档目的地与保留策略须同步落地。请确认可安排的停机窗口、"
        "归档目的地（本地目录 / 快速恢复区）以及归档保留时长。",
    ),
    "ORA.CONFIG.AUTOEXTEND": (
        "数据文件关闭自动扩展是否有意设计",
        "存在未开启自动扩展的数据文件。请确认这是存储配额管控的有意设计（统一分配、扩容走"
        "审批），还是建库/扩容时的遗漏。若为前者，请给出容量告警阈值与扩容响应时限；"
        "若为后者，建议在业务低峰开启并设定合理的 MAXSIZE。",
    ),
    "ORA.STORAGE.TABLESPACE": (
        "表空间扩容的审批与执行路径",
        "存在使用率偏高的表空间。扩容涉及存储采购或配额调整，须确认可用空间、审批流程与"
        "执行窗口；扩容落地前，请确认业务侧是否有可先行归档或清理的历史数据。",
    ),
    "ORA.STORAGE.FRA": (
        "快速恢复区容量与归档保留策略",
        "快速恢复区使用率告警。FRA 写满会使数据库挂起（所有需要写归档/FRA 的操作阻塞）。"
        "请确认归档保留策略（保留时长、是否配置备份后自动删除）与 FRA 目标容量的调整窗口。",
    ),
    "ORA.SECURITY.DBA_COUNT": (
        "高权限账户的业务归属与回收范围",
        "存在多个拥有 DBA 角色的账户。请确认哪些是应用连接账号、哪些是历史遗留或厂商维护"
        "账号，以及各自是否仍有使用需求 —— 回收范围属于业务语义决策，不由 DBA 单方面执行。",
    ),
    "ORA.SECURITY.PASSWORD_EXPIRY": (
        "账户密码的托管方式与轮换窗口",
        "存在密码临近过期或已锁定的账户。请确认这些账户的口令由谁托管（应用配置中心 / "
        "密码管理系统 / 人工维护），以及轮换时的应用重启窗口 —— 口令变更必须与应用侧"
        "同步，否则会造成连接失败。",
    ),
    "ORA.SECURITY.ACCOUNT_LOCKED": (
        "锁定账户是否为废弃账号",
        "存在处于锁定状态的账户。请确认这些账户是否已停用（历史人员、下线系统、厂商账号）；"
        "若确认废弃，可执行清理或保持锁定并移出巡检白名单，避免每次巡检重复告警。",
    ),
    "COMMON.SYSTEM.TIME_SYNC": (
        "时间源的提供方与同步要求",
        "主机时间未同步。请确认时间源由谁提供（内网 NTP 服务器地址、是否允许访问公网授时），"
        "以及该主机是否需要纳入统一时间同步 —— DataGuard / RAC 环境对时间偏差敏感。",
    ),
    "ORA.JOB.FAILURE": (
        "失败调度作业的归属方",
        "存在执行失败的 Scheduler 作业。请确认这些作业归业务方还是 DBA 维护，以及失败是否"
        "已被业务侧知晓 —— 作业失败往往对应数据未按时生成，属于业务影响面，不只是任务状态。",
    ),
    "ORA.DATA.BLOCK_CORRUPTION": (
        "坏块修复策略与业务可接受窗口",
        "检测到物理坏块。修复方式（RMAN BLOCKRECOVER / 从备库同步 / 跳过坏块）与是否需要"
        "停机，取决于数据重要性与可接受的数据损失。请确认业务侧可接受的修复窗口与"
        "数据丢失容忍度。",
    ),
}


def pending_confirmations(
    instance: dict[str, Any], *, node: str = ""
) -> list[dict[str, Any]]:
    """按已触发的规则派生「待客户确认事项」。

    与风险台账的分工：台账答「是什么风险、怎么改」，本表答「哪几件事必须由客户拍板、
    或必须由客户提供外部证据」。规则未触发则不进表 —— 没触发就说明本节点没有该问题，
    不该把模板空转成一张「欢迎确认」清单。

    ``node`` 默认留空：Oracle 现场以单实例为主，节点列在单实例报告里只是噪音（引擎
    按 ``has_node`` 决定是否渲染该列）。将来接多实例 / RAC 时再按对端 IP 传入。
    """
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for finding in instance.get("findings") or []:
        rule_id = str(finding.get("rule_id") or "").strip()
        template = CONFIRMATION_TEMPLATES.get(rule_id)
        if not template or rule_id in seen:
            continue
        seen.add(rule_id)
        items.append({
            "topic": template[0],
            "question": template[1],
            "node": node,
            "rule_id": rule_id,
            "finding_id": finding.get("finding_id"),
            "evidence": list(finding.get("facts") or []),
        })
    return items


def _merged_confirmations(instances: list[dict[str, Any]]) -> list[dict[str, Any]] | None:
    """跨实例合并「待客户确认事项」。

    只取 ``instances[0]`` 会让其余节点需要客户确认的事项整段消失。同一主题在多个
    节点都触发时**保留各自的行**，不按主题归并 —— 不同节点的证据（容量水位、涉及
    账号、失败作业）通常并不相同。

    返回 ``None`` 表示所有实例都没有该字段（旧分析产物 / 未接入），渲染层据此区分
    「未产出清单」与「本次没有待确认事项」—— 延续「未采集 ≠ 0」。
    """
    merged: list[dict[str, Any]] = []
    present = False
    for instance in instances:
        items = instance.get("pending_confirmations")
        if items is None:
            continue
        present = True
        merged.extend(items)
    if not present:
        return None
    merged.sort(key=lambda item: (str(item.get("topic") or ""), str(item.get("node") or "")))
    return merged


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _colon_map(lines: list[str]) -> dict[str, str]:
    """解析 `lscpu` 的 `key: value` 原文（行由 `ctx.raw_lines` 提供，见批 14）。"""
    result: dict[str, str] = {}
    for raw in lines:
        if ":" not in raw:
            continue
        name, value = raw.split(":", 1)
        result[name.strip()] = value.strip()
    return result


def _kv_map(lines: list[str]) -> dict[str, str]:
    """解析 `key=value` 原文（`time_status.txt`）。"""
    result: dict[str, str] = {}
    for raw in lines:
        if "=" not in raw:
            continue
        name, value = raw.split("=", 1)
        result[name.strip()] = value.strip()
    return result


#: 采集侧 key-value 文件（`kernel_parameters.tsv` / `hugepages.tsv`）的**首行是表头**
#: `path<TAB>value`。它不是参数 —— 落进表里会多出一行「path | value」，读起来像个真参数。
#: 只对**第一条已解析行**判一次，避免误伤真值恰好是 `value` 的参数。
_KV_HEADER_VALUES = {"value", "值"}


def _key_value_rows(lines: list[str]) -> list[dict[str, str]]:
    """解析 key-value 文本（`kernel_parameters.tsv`：TAB 或 `=` 分隔），并跳过首行表头。"""
    rows: list[dict[str, str]] = []
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "\t" in line:
            name, value = line.split("\t", 1)
        elif "=" in line:
            name, value = line.split("=", 1)
        else:
            continue
        name, value = name.strip(), value.strip()
        if not rows and value.lower() in _KV_HEADER_VALUES:
            continue
        rows.append({"参数": name, "值": value})
    return rows


def _persistent_mounts(lines: list[str]) -> tuple[list[dict[str, str]], int]:
    """解析 `mount` 原文（`设备 on 挂载点 type 类型 (选项)`），返回 (持久化挂载点, 排除数)。

    判据来自公共层（`inspection_core.system_checks.is_persistent_fstype`）：proc/sysfs/
    tmpfs/cgroup/光驱等既不是"挂载参数"也不承载数据，列出来只会把真正的持久化挂载点
    淹掉（本实例 39 行里只有 2 行是持久化的）。MySQL 侧用的是同一个判据。
    """
    rows: list[dict[str, str]] = []
    excluded = 0
    pattern = re.compile(r"^(.*?) on (.*?) type (.*?) \((.*)\)$")
    for raw in lines:
        matched = pattern.match(raw.strip())
        if not matched:
            continue
        fstype = matched.group(3).strip()
        if not is_persistent_fstype(fstype):
            excluded += 1
            continue
        rows.append({
            "设备": matched.group(1).strip(),
            "挂载点": matched.group(2).strip(),
            "类型": fstype,
            "挂载选项": matched.group(4).strip(),
        })
    return rows, excluded


def _mount_rows(lines: list[str]) -> list[dict[str, str]]:
    """只要行的入口 —— **与 MySQL 侧同名函数同形**（只回 list）。

    `tests/test_raw_file_access.py` 用的就是这条"四库解析器签名一致"的判据。需要
    排除数时用 `_persistent_mounts`，不要在这里改返回类型。
    """
    return _persistent_mounts(lines)[0]


def _kb_human(value: Any) -> str:
    """`df -PT` 的容量列单位是 1024-blocks（KB）→ 转成人类可读（`64.10 GB`）。

    展示层此前直接把裸 KB 数字印进"可用空间"列，同时又因为键名不匹配取不到值 ——
    两件事叠在一起，客户看到的是空列。这里一次把两件都解决。
    """
    number = safe_float(value)
    if number is None:
        return ""
    return format_bytes(number * 1024) or ""


def _dmesg_rows(lines: list[str], limit: int = 20) -> list[dict[str, str]]:
    """取 `dmesg` 错误原文的前 `limit` 行。"""
    content = [line.strip() for line in lines if line.strip()]
    return [{"内核错误日志": line} for line in content[:limit]]


@dataclass
class Finding:
    finding_id: str
    rule_id: str
    severity: str
    category: str
    title: str
    summary: str
    facts: list[str]
    recommendation: str
    evidence_refs: list[str]
    confidence: float = 0.9
    status: str = "triggered"  # triggered | passed
    triggered: bool = True
    requires_restart: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "finding_id": self.finding_id,
            "rule_id": self.rule_id,
            "severity": self.severity,
            "category": self.category,
            "title": self.title,
            "summary": self.summary,
            "facts": self.facts,
            "recommendation": self.recommendation,
            "evidence_refs": self.evidence_refs,
            "confidence": self.confidence,
            "status": self.status,
            "triggered": self.triggered,
            "requires_restart": self.requires_restart,
        }

def _cell(row: dict[str, Any], *names: str) -> Any:
    """从一行里按候选列名取值，容忍 sqlplus 的表头截断。

    ★ 为什么需要它：sqlplus 会按列的**显示宽度**截断表头（真实采集包实测：
    `SEGMENT_SPACE_MANAGEMENT` → `SEGMEN`、`ADMIN_OPTION` → `ADM`、
    `COMPRESSION` → `COMPRES`、`RESOURCE_TYPE` → `RESOURCE`、`SYSOPER` → `SYSOP`），
    截断长度取决于该列的**数据 / 声明宽度**，换一套库就可能不同。
    `row.get("完整列名")` 在取不到时**既不报错也不告警，只是少一列** —— 静默失真，
    比抛异常更难发现（与规则层 L-2「列名对不上是静默的」同源）。

    匹配顺序：① 精确且非空 → ② 前缀互含（`wanted.startswith(key)` 或 `key.startswith(wanted)`，
    要求该键长度 ≥ 3，避免 `A` 这类单字符误配）→ ③ 精确但空值。都取不到时返回 `""`。
    """
    for name in names:
        if name in row and str(row.get(name) or "").strip():
            return row[name]
    upper = {str(key).upper(): key for key in row}
    for name in names:
        wanted = name.upper()
        for key, original in upper.items():
            if len(key) >= 3 and (wanted.startswith(key) or key.startswith(wanted)):
                return row[original]
    for name in names:
        if name in row:
            return row[name]
    return ""


# ── 「说明」列的口径表（检查项 / 组件名 → 释义）──────────────────────────────
#
# ★ 说明列放的是**口径** —— 这个值是什么、取自哪个视图；**不是建议**。建议有两处：
#   摘要表的「依据与建议」列、每个采集项的「分析结论」列。混用会让同一列出现两种
#   读法（与 MySQL 同一条线，见 plugins/mysql/presentation.py::CONFIG_GROUP_VARIABLES）。
#
# ★ 为什么收成表而不是就地写字符串：① 口径集中一处才审得动；② 才**可被断言** ——
#   守门用例喂真实形态的合成 ctx，逐行断言「说明非空」
#   （tests/test_oracle_report_quality.py::RowNoteCoverageTests）。就地写死的话，
#   "漏了一行"只能靠肉眼，而空格子在报告里**不报错、不抛异常**。
#
# ★ 为什么单独立批：`数据库身份` / `数据库配置概要` 两张表此前**整列为空**
#   （实测 1.4 与 2.0 两包一致：7 + 15 = 22 行），与批 15/16/17 同族 ——
#   **呈现层的错不产生功能性失败信号**。
ORACLE_IDENTITY_NOTES: dict[str, str] = {
    "数据库名": "db_name 参数（快照 snapshot.db_name）",
    "实例名": "instance_name（实例 SID）",
    "版本": "数据库版本（v$instance.version_full）",
    "角色": "DATABASE_ROLE：PRIMARY / STANDBY",
    "RAC": "是否 RAC 集群部署（snapshot.is_rac）",
    "CDB/PDB": "是否多租户容器库（snapshot.is_cdb）",
    "ASM": "是否使用 ASM 存储（snapshot.has_asm）",
}

ORACLE_DB_CONFIG_NOTES: dict[str, str] = {
    "DB_NAME": "数据库名（v$database.name）",
    "DB_UNIQUE_NAME": "库的唯一名（DataGuard 主备识别用）",
    "数据库角色": "DATABASE_ROLE：PRIMARY / STANDBY",
    "打开模式": "OPEN_MODE：读写 / 只读 / 未打开",
    "归档模式": "LOG_MODE：ARCHIVELOG / NOARCHIVELOG",
    "创建时间": "库创建时间（v$database.created）",
    "PGA 总分配": "v$pgastat 的 total PGA allocated（已分配，非当前占用）",
    # ↓ 这四条同时是「概要表展示哪些 NLS 参数」的**单一来源**（见下方 charset 循环）。
    "NLS_CHARACTERSET": "服务端字符集",
    "NLS_NCHAR_CHARACTERSET": "NCHAR / NVARCHAR2 字符集",
    "NLS_LANGUAGE": "服务端语言（报错文本与日期文本）",
    "NLS_DATE_FORMAT": "会话默认日期显示格式",
}

# SGA 组件名由 `v$sga` 给出，不同版本可能多出组件 ⇒ 表 + **兜底**，
# 保证任何组件都拿得到说明，**永不出现空格子**。
ORACLE_SGA_NOTES: dict[str, str] = {
    "Database Buffers": "数据库缓冲区缓存（default buffer cache）",
    "Variable Size": "共享池 / 大型池等可变内存区",
    "Fixed Size": "SGA 固定区开销（内部结构）",
    "Redo Buffers": "日志缓冲区（log buffer）",
    "Log Buffer": "日志缓冲区（log buffer）",
}
ORACLE_SGA_NOTE_FALLBACK = "SGA 组件（v$sga）"
ORACLE_NLS_NOTE_FALLBACK = "数据库级 NLS 参数（nls_database_parameters）"


def build_inspection_sections(ctx: Any, metrics: dict[str, Any],
                               findings: list[Finding],
                               evaluations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build data-driven inspection sections mirroring MySQL's build_inspection_model.

    Each section maps to a report chapter; each item carries display rows + analysis block.
    Findings and rule evaluations are looked up by rule_id to build analysis blocks.
    """
    env = ctx.snapshot
    tag = env.get("instance_tag", "")
    s_hostname, s_ip = parse_instance_tag(tag)
    quality = metrics.get("_quality", {})
    sampling = metrics.get("sampling_context", {})
    ora = metrics.get("oracle_realtime", {})
    sys_rt = metrics.get("system_realtime", {})
    sys_hist = metrics.get("system_history", {})

    # 批 14：这些**非表格式**证据文件的路径一律由 `ctx.raw_lines` 解析，呈现层不再自己拼
    # `root / "evidence" / …`。目录曾在 N-8 从 `tables/` 挪到 `evidence/`，写死目录的后果是
    # **静默少一节内容**（读不到只返回空，规则层不报、测试不红）。目录只认 `_RAW_FILE_DIRS`。
    lscpu = _colon_map(ctx.raw_lines("lscpu.txt"))
    time_info = _kv_map(ctx.raw_lines("time_status.txt"))
    kernel_rows = _key_value_rows(ctx.raw_lines("kernel_parameters.tsv"))
    mount_rows, mount_excluded = _persistent_mounts(ctx.raw_lines("mounts.txt"))
    dmesg_rows = _dmesg_rows(ctx.raw_lines("dmesg_errors.txt"))
    os_name = env.get("os") or env.get("distribution") or ""
    for line in ctx.raw_lines("os_release.txt"):
        if line.startswith("PRETTY_NAME="):
            os_name = line.split("=", 1)[1].strip().strip('"').strip("'")
            break

    # ── build lookup indexes ──
    _finding_by_rule: dict[str, dict[str, Any]] = {}
    for f in findings:
        _finding_by_rule[f.rule_id] = f.to_dict()

    _eval_by_rule: dict[str, dict[str, Any]] = {}
    for e in evaluations:
        _eval_by_rule[e.get("rule_id", "")] = e

    # ── helpers ──
    def _status(item_id: str) -> dict[str, Any]:
        for item in quality.get("non_ok_items", []):
            if item.get("item_id") == item_id:
                return item
        return {"status": "ok"}

    def _val_row(name: str, value: Any, desc: str = "") -> dict[str, Any]:
        return {"检查项": name, "采集值": str(value) if value is not None else "未采集", "说明": desc}

    def _analysis(rule_id: str) -> dict[str, Any]:
        f = _finding_by_rule.get(rule_id)
        ev = _eval_by_rule.get(rule_id)
        ev_status = str(ev.get("status", "")) if ev else ""

        if f:
            sev = f.get("severity", "medium")
            status = "risk" if sev in ("critical", "high") else "attention"
            return {
                "status": status,
                "conclusion": f.get("summary", ""),
                "evidence": f.get("facts", []),
                "recommendation": f.get("recommendation", ""),
            }
        elif ev_status == "passed":
            # ★「通过」不等于「无话可说」：规则调用方是带着实测值来的（如
            #   `最高文件系统使用率：33.0%`），此前只有触发才把 facts 存进 Finding，
            #   通过项的证据被丢掉 → 报告的依据列全落成「未提供补充依据」，
            #   读者无从判断"通过"是基于什么。现在把评测自带的 facts 透出来。
            facts = [str(value).strip() for value in (ev.get("facts") or []) if str(value).strip()]
            return {"status": "normal", "conclusion": "检查通过，未发现异常。",
                    "evidence": facts, "recommendation": ""}
        elif ev_status == "not_applicable":
            return {"status": "not_applicable", "conclusion": "当前架构不适用此项检查。", "evidence": [], "recommendation": ""}
        elif ev_status:  # has evaluation but not triggered/passed/applicable
            # ★「证据不足」必须带上**为什么不足**：规则引擎已经给出 `reason`
            #   （如「无备库（未配置 DataGuard）」）。只印"证据不足"会让读者以为是
            #   采集失败，而事实常常是"这项在当前架构下根本不存在" —— 与
            #   「未采集 ≠ 0」同源：不许让两种相反结论长成一句话。
            reason = str(ev.get("reason") or "").strip()
            return {"status": "not_evaluated", "conclusion": reason or "证据不足，未执行评价。",
                    "evidence": [], "recommendation": ""}
        else:  # no evaluation at all — data-only display item
            return {"status": "", "conclusion": ""}

    def _item(title: str, item_id: str, rows: list[dict[str, Any]] | None = None,
               analysis: dict[str, Any] | None = None) -> dict[str, Any]:
        col = _status(item_id)
        # ★ **规则项也要印真实采集状态**：`_status()` 只查 `quality.non_ok_items`，而那本账
        #   ① 只记失败态（`ok`/`empty` 都不进）、② 按**采集项** id 索引 ⇒ 规则 id 永远查不到，
        #   于是底层表是「查了没有」(`empty`) 还是「根本没采」(`unavailable`)，
        #   报告一律印成「正常」（批 19 的 C 项）。
        #   判据身上带着 `source_status`（随 `RuleEvaluation` 出来），是唯一不会与采集实况
        #   脱钩的来源：规则读什么表、那张表采没采到，写在同一个地方。
        judged = str((_eval_by_rule.get(item_id) or {}).get("source_status") or "")
        return {
            "title": title,
            "source": f"Oracle 采集包 — {item_id}",
            "collection": {
                "status": judged or col.get("status", "ok"),
                "row_count": len(rows) if rows else None,
                "reason": col.get("reason", ""),
            },
            "display": {"rows": rows or [], "note": ""},
            "analysis": analysis if analysis is not None else _analysis(item_id),
        }

    def _data_item(title: str, item_id: str, rows: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        """Data-only display item — no analysis block."""
        return _item(title, item_id, rows=rows, analysis={"status": "", "conclusion": ""})

    # ── 数据展示条目（批 13）─────────────────────────────────────────────
    # `quality.non_ok_items` **只记失败态**（permission_denied / timeout / error…），
    # 「查了没有」(`empty`) 与「根本没采」被它一并吞掉。数据展示条目若只看它，会把两种
    # **相反结论**写成同一句含糊话 —— 于是这里直读 `ctx.status` 的逐项状态。
    # 2.0 包按 `oracle.<域>.<项>` 登记，1.4 包按中文标题登记，两者都要覆盖。
    _real_status: dict[str, str] = {
        str(entry.get("item_id")): str(entry.get("status") or "")
        for entry in (ctx.status.get("items") or [])
        if entry.get("item_id")
    }
    _EMPTY_TEXT = {
        "empty": "已检查，本次未发现相关记录。",
        "not_enabled": "本项未启用，无明细可展示。",
        "not_applicable": "当前架构不适用，无明细可展示。",
        "permission_denied": "采集权限不足，未能取得明细。",
        "timeout": "采集超时，未能取得明细。",
        "error": "采集失败，未能取得明细。",
        "skipped": "本项已跳过，未取得明细。",
        "unsupported": "当前环境不支持该项采集。",
        "partial": "采集不完整，仅取得部分明细。",
    }

    def _real_status_of(*names: str) -> str:
        for name in names:
            if name and name in _real_status and _real_status[name]:
                return _real_status[name]
        for name in names:
            lowered = (name or "").lower()
            if not lowered:
                continue
            for key, value in _real_status.items():
                if value and lowered in key.lower():
                    return value
        return ""

    def _table_seen(*names: str) -> bool:
        """包内是否登记过这张表（**空表也算登记**）。

        1.4 包的逐项状态登记为 `sql.<代号>`（如 `sql.HWM`），与 `item_id` 之间没有
        可用的映射；此时「有没有采过」只能由**表键是否存在**来判定 —— 这一步很关键，
        否则「查了没有」会被写成「没采到」，两种相反结论又长成一样。
        """
        for name in names:
            if name and name in ctx.tables:
                return True
        for name in names:
            lowered = (name or "").lower()
            if not lowered:
                continue
            for key in ctx.tables:
                if lowered in key.lower():
                    return True
        return False

    def _listed(title: str, item_id: str, rows: list[dict[str, Any]] | None = None,
                alias: str = "", analysis: dict[str, Any] | None = None) -> dict[str, Any]:
        """带**真实采集状态**的展示条目；无数据时按采集状态给出准确空态文案。"""
        item = (_data_item(title, item_id, rows=rows) if analysis is None
                else _item(title, item_id, rows=rows, analysis=analysis))
        if not rows:
            real = _real_status_of(item_id, alias)
            if not real and _table_seen(item_id, alias):
                real = "empty"  # 表在包里 ⇒ 采过；无行 ⇒ 查了没有
            if real:
                item["collection"]["status"] = real
            item["display"]["empty_text"] = _EMPTY_TEXT.get(real, "本项未采集到数据，无法呈现明细。")
        return item

    sections: list[dict[str, Any]] = []

    # ── 3. 系统环境检查 (system_environment) ──
    is_rac = int(env.get("is_rac", 0)) == 1
    is_cdb = int(env.get("is_cdb", 0)) == 1
    has_asm = int(env.get("has_asm", 0)) == 1
    # 挂载参数只列**持久化**挂载点，排除数写进 note —— 客户看到行数从 39 掉到 2 会问
    # "是不是漏采了"，注一句就把"过滤"和"缺失"区分开了（与 MySQL 同一判据/同款脚注）。
    mount_item = _data_item("挂载参数", "system.mounts", mount_rows)
    if mount_rows and mount_excluded:
        mount_item["display"]["note"] = (
            f"已排除 {mount_excluded} 个 proc/tmpfs/cgroup/光驱等非持久化挂载点"
        )
    se_items: list[dict[str, Any]] = [
        _item("主机信息", "system.host", [
            # ★ 一律 `or None`，**不要**写 `, ""` 兜底：`""` 在引擎里渲染成 `-`，与
            #   「字段本身为空」长得一样；`None` 渲染成 `未采集`。1.4 采集器不采操作系统层，
            #   4 行会变成 `未采集` 而不是一行看不出所以然的 `-`（MySQL 侧同款语义）。
            _val_row("主机名", s_hostname or env.get("host") or None, "采集目标主机"),
            _val_row("IP 地址", s_ip or env.get("host") or None, "数据库服务地址"),
            _val_row("操作系统", os_name or None, "操作系统发行版"),
            _val_row("CPU 型号", lscpu.get("Model name") or None, "处理器型号"),
            _val_row("CPU 核数", lscpu.get("CPU(s)") or None, "逻辑核心"),
            _val_row("NUMA 节点", lscpu.get("NUMA node(s)") or None, "NUMA 拓扑"),
        ]),
        _item("数据库身份", "oracle.identity", [
            _val_row("数据库名", env.get("db_name"), ORACLE_IDENTITY_NOTES["数据库名"]),
            _val_row("实例名", env.get("instance_name"), ORACLE_IDENTITY_NOTES["实例名"]),
            _val_row("版本", env.get("ora_version"), ORACLE_IDENTITY_NOTES["版本"]),
            _val_row("角色", env.get("db_role"), ORACLE_IDENTITY_NOTES["角色"]),
            _val_row("RAC", "是 (集群)" if is_rac else "否 (单机)", ORACLE_IDENTITY_NOTES["RAC"]),
            _val_row("CDB/PDB", "是" if is_cdb else "否", ORACLE_IDENTITY_NOTES["CDB/PDB"]),
            _val_row("ASM", "是" if has_asm else "否", ORACLE_IDENTITY_NOTES["ASM"]),
        ]),
        _item("时间同步检查", _RULE_TIME_SYNC,
               analysis=_analysis(_RULE_TIME_SYNC)),
        _item("采集完整性", "ORA.COLLECTION.INTEGRITY",
               analysis=_analysis("ORA.COLLECTION.INTEGRITY")),
        _item("采集数据质量", "ORA.COLLECTION.QUALITY",
               analysis=_analysis("ORA.COLLECTION.QUALITY")),
        _data_item("时间与时区", "system.time", [
            _val_row("本地时间", time_info.get("local_time") or None, "采集时刻的主机本地时间"),
            _val_row("时区", time_info.get("timezone") or None, "主机时区设置"),
        ]),
        _data_item("关键内核参数", "system.kernel", kernel_rows),
        mount_item,
        _data_item("内核错误摘要", "system.dmesg_errors", dmesg_rows),
    ]
    # ── DB config info (informational display) ──
    db_info_rows = ctx.tables.get("数据库基本信息", [])
    charset_rows = ctx.tables.get("数据库字符集", [])
    sga_rows = ctx.tables.get("SGA 汇总信息", [])
    pga_rows = ctx.tables.get("PGA 统计", [])
    display_dbconf: list[dict[str, Any]] = [
        _val_row("DB_NAME", env.get("db_name", ""), ORACLE_DB_CONFIG_NOTES["DB_NAME"]),
        _val_row("DB_UNIQUE_NAME", db_info_rows[0].get("DB_UNIQUE_NAME", "").strip() if db_info_rows else "",
                 ORACLE_DB_CONFIG_NOTES["DB_UNIQUE_NAME"]),
        _val_row("数据库角色", env.get("db_role", ""), ORACLE_DB_CONFIG_NOTES["数据库角色"]),
        _val_row("打开模式", env.get("open_mode", "") or (db_info_rows[0].get("OPEN_MODE", "").strip() if db_info_rows else ""),
                 ORACLE_DB_CONFIG_NOTES["打开模式"]),
        _val_row("归档模式", "ARCHIVELOG" if db_info_rows and str(db_info_rows[0].get("LOG_MODE","")).strip() == "ARCHIVELOG" else "NOARCHIVELOG" if db_info_rows else "",
                 ORACLE_DB_CONFIG_NOTES["归档模式"]),
        _val_row("创建时间", (db_info_rows[0].get("CREATED","").strip() if db_info_rows else ""),
                 ORACLE_DB_CONFIG_NOTES["创建时间"]),
    ]
    for row in charset_rows:
        pname = str(row.get("PARAMETER","")).strip()
        # ★ 不在口径表里的 NLS 参数不进概要表（表里有 `NLS_TERRITORY` 等）——
        #   「展示哪些」与「怎么解释」是同一个事实，只能有一处来源。
        note = ORACLE_DB_CONFIG_NOTES.get(pname)
        if note is None or not pname.startswith("NLS_"):
            continue
        display_dbconf.append(_val_row(pname, str(row.get("VALUE","")).strip(), note))
    for row in sga_rows:
        component = str(row.get("NAME","")).strip()
        display_dbconf.append(_val_row(
            "SGA / " + component,
            f"{safe_float(row.get('MB',row.get('     MB','0'))):.0f} MB" if row.get("MB") or row.get("     MB") else "",
            ORACLE_SGA_NOTES.get(component, ORACLE_SGA_NOTE_FALLBACK),
        ))
    # ★ 行名写「PGA 总分配」，就必须取 `total PGA allocated`。原实现取 `pga_rows[0]`，
    #   而 `v$pgastat` 的第一行实测是 `total PGA inuse`（采集 SQL 没有 ORDER BY，顺序由
    #   视图给）⇒ **名字写"总分配"、值却是"当前占用"**（1.4 包 123 MB，而 allocated 266 MB，
    #   差一倍；2.0 是 170 MB vs 305 MB）。按名字取，取不到再退回第一行，保证不因缺行空白。
    #   这一条是补「说明」时发现的 —— 口径写不对，说明就只能写假话。
    pga_row = next(
        (row for row in pga_rows
         if str(row.get("NAME", "")).strip().lower() == "total pga allocated"),
        pga_rows[0] if pga_rows else None,
    )
    pga_val = pga_row.get("VALUE","") if pga_row else ""
    if pga_val:
        try:
            pga_val = f"{float(pga_val)/1024/1024:.0f} MB"
        except (TypeError, ValueError):
            pass
    display_dbconf.append(_val_row("PGA 总分配", pga_val, ORACLE_DB_CONFIG_NOTES["PGA 总分配"]))
    se_items.append(_item("数据库配置概要", "oracle.db_config", display_dbconf))
    sections.append({"section_id": "system_environment", "title": "系统环境检查", "items": se_items})

    # ── 4. 系统性能检查 (system_performance) ──
    local = metrics.get("scope", {}).get("database_target_is_local", False)
    hist_usable = sampling.get("history", {}).get("usable_for_trend_rules", False)

    # Choose best data source
    cpu_src = sys_hist if hist_usable else sys_rt
    cpu_label = "SAR 24h" if hist_usable else "实时采样"
    resource_rows = [
        {
            "指标": "CPU 使用率(%)",
            "平均值": f"{cpu_src.get('cpu_busy_pct', {}).get('average', '-')}",
            "最大值": f"{cpu_src.get('cpu_busy_pct', {}).get('max', '-')}",
            "数据源": cpu_label,
        },
        {
            "指标": "IO Wait(%)",
            "平均值": f"{cpu_src.get('cpu_iowait_pct', {}).get('average', '-')}",
            "最大值": f"{cpu_src.get('cpu_iowait_pct', {}).get('max', '-')}",
            "数据源": cpu_label,
        },
        {
            "指标": "内存使用率(%)",
            "平均值": f"{sys_rt.get('memory_used_pct', {}).get('average', '-')}",
            "最大值": f"{sys_rt.get('memory_used_pct', {}).get('max', '-')}",
            "数据源": "实时采样",
        },
    ]
    sp_items: list[dict[str, Any]] = [
        _item("系统资源概要", "system.resources", resource_rows),
    ]
    if local:
        sp_items.append(_item("CPU 压力评估", _RULE_CPU_PRESSURE,
                               analysis=_analysis(_RULE_CPU_PRESSURE)))
        sp_items.append(_item("IO Wait 评估", _RULE_IOWAIT_PRESSURE,
                               analysis=_analysis(_RULE_IOWAIT_PRESSURE)))
        sp_items.append(_item("内存压力评估", _RULE_MEMORY_PRESSURE,
                               analysis=_analysis(_RULE_MEMORY_PRESSURE)))

    # ── Filesystem data layer ──
    # 键名必须与 `package_adapter.parse_df_pt` 的产出对齐：`Mounted on` / `Filesystem` /
    # `Available`。展示层此前读的是 MOUNTPOINT / FILESYSTEM / AVAILABLE，只有 `USE%`
    # 恰好撞对 —— 结果是 9 行表格里"挂载点/文件系统/可用空间"三列全空、只剩一列使用率。
    # 旧拼法保留作兜底（1.4 老包 / 别名表）。
    fs_rows = ctx.tables.get("filesystems", [])
    display_fs: list[dict[str, Any]] = []
    fs_excluded = 0
    for row in fs_rows:
        if not is_persistent_fstype(filesystem_type(row)):
            fs_excluded += 1
            continue
        raw_used = row.get("USE%")
        if raw_used in (None, ""):
            raw_used = row.get("used_pct")
        used = str(raw_used).strip().rstrip("%") if raw_used not in (None, "") else ""
        display_fs.append({
            "文件系统": row.get("Filesystem") or row.get("FILESYSTEM") or row.get("filesystem", ""),
            "类型": filesystem_type(row),
            "挂载点": row.get("Mounted on") or row.get("MOUNTPOINT") or row.get("mountpoint", ""),
            "使用率": f"{used}%" if used else "",
            "可用空间": _kb_human(row.get("Available") if row.get("Available") not in (None, "") else row.get("AVAILABLE")),
        })
    if display_fs:
        fs_item = _item("文件系统使用情况", "oracle.filesystems", rows=display_fs)
        if fs_excluded:
            fs_item["display"]["note"] = (
                f"已排除 {fs_excluded} 个 tmpfs/devtmpfs/光驱等非持久化挂载点"
            )
        sp_items.append(fs_item)
    elif fs_rows:
        # 全被过滤掉也要出项 —— 让"整机只有 tmpfs/光驱"与"这章漏采了"在报告里长得不一样，
        # 否则一项突然消失，读的人只会当成少了一节内容。规则层同口径（不适用，不告警）。
        sp_items.append(_item(
            "文件系统使用情况", "oracle.filesystems", rows=[],
            analysis={
                "status": "not_applicable",
                "conclusion": f"本次采集到的 {len(fs_rows)} 个挂载点均为 tmpfs/devtmpfs/光驱等非持久化文件系统，无容量可评价。",
                "evidence": [], "recommendation": "",
            },
        ))
    sp_items.append(_item("文件系统分析", _RULE_FILESYSTEM_USAGE,
                           analysis=_analysis(_RULE_FILESYSTEM_USAGE)))

    sections.append({"section_id": "system_performance", "title": "系统性能检查", "items": sp_items})

    # ── 5. 存储与容量检查 (storage_capacity) ──
    ts_rows = ctx.find_table("所有表空间容量使用情况", "表空间", "tablespace")
    display_ts: list[dict[str, Any]] = []
    for row in ts_rows[:30]:
        used_val = safe_float(row.get("USED_MB") or row.get("USED"))
        max_val = safe_float(row.get("MAX_MB"))
        alloc_val = safe_float(row.get("ALLOC_MB") or row.get("TOTAL_MB"))
        # Use MAX_MB as denominator when available; fall back to ALLOC_MB
        pct_base = max_val if max_val and max_val > 0 else alloc_val
        pct_val = f"{used_val / pct_base * 100:.2f}" if used_val is not None and pct_base and pct_base > 0 else ""
        display_ts.append({
            "表空间": row.get("TABLESPACE_NAME") or row.get("TABLESPACE", ""),
            # `if alloc_val` 会把**真的 0** 当成空白格（TEMP 表空间 USED_MB=0 时"已使用"
            # 列显示不出 0），与「未采集 ≠ 0」相悖：0 是采集到的确定值，空白才是缺失。
            "已分配(MB)": f"{alloc_val:.0f}" if alloc_val is not None else "",
            "最大可扩(MB)": f"{max_val:.0f}" if max_val is not None else "",
            "已使用(MB)": f"{used_val:.0f}" if used_val is not None else "",
            "使用率(%)": pct_val,
        })
    sc_items: list[dict[str, Any]] = [
        # ── Data layer ──
        _item("表空间容量一览", "oracle.tablespace_list", rows=display_ts),
    ]

    # Datafile list (data layer)
    df_rows = ctx.tables.get("表空间数据文件列表", [])
    display_df: list[dict[str, Any]] = []
    for row in df_rows[:40]:
        display_df.append({
            "表空间": str(row.get("TABLESPACE_NAME","")).strip(),
            "文件路径": str(row.get("FILE_NAME","")).strip()[-60:],
            "大小(MB)": row.get("BYTES","") if row.get("BYTES") else row.get("SIZE_MB",""),
        })
    if display_df:
        sc_items.append(_item("数据文件列表", "oracle.datafile_list", rows=display_df))

    # ── Analysis layer ──
    sc_items.append(_item("表空间使用风险", "ORA.STORAGE.TABLESPACE",
                           analysis=_analysis("ORA.STORAGE.TABLESPACE")))

    if has_asm:
        asm_rows = ctx.tables.get("asm_diskgroup_summary", [])
        display_asm: list[dict[str, Any]] = []
        for row in asm_rows[:20]:
            display_asm.append({
                "磁盘组": row.get("NAME") or row.get("GROUP_NAME", ""),
                "总大小(MB)": row.get("TOTAL_MB", ""),
                "已用(MB)": row.get("USED_MB", ""),
                "使用率(%)": row.get("USED_PCT") or row.get("PCT", ""),
            })
        sc_items.append(_item("ASM 磁盘组", "ORA.STORAGE.ASM",
                               rows=display_asm,
                               analysis=_analysis("ORA.STORAGE.ASM")))

    # ★ 批 19：这里从前是 `ctx.find_table("FRA", "闪回区", "快速恢复区")` —— `find_table` 的
    #   **子串兜底**先把 "FRA" 命中了 `快速恢复区（FRA）参数`（只有 `NAME`/`VALUE` 两列）⇒
    #   「名称」列印出 `db_recovery_file_dest`，「大小/使用率」两列恒空：一张**参数表**被当成
    #   使用率表展示（读者只会以为"FRA 没用量"）。取数与列名都按真正的使用率表来：
    #   `V$RECOVERY_FILE_DEST` → `LIMIT_GB|USED_GB|PCT_USED|RECLAIMABLE_GB|NUMBER_OF_FILES`
    #   （ASM 版无 `PCT_USED`，由规则层用 used/limit 折算，见 `oracle_rules._fra_percent`）。
    fra_usage = ctx.table_fetch("oracle.flashback.area_usage", "闪回区使用情况")
    fra_asm = ctx.table_fetch("oracle.archive.fra_asm", "ASM 快速恢复区使用情况")
    display_fra: list[dict[str, Any]] = []
    for row in [*fra_usage.rows, *fra_asm.rows][:10]:
        display_fra.append({
            "文件类型": _cell(row, "FILE_TYPE", "NAME"),
            "上限(GB)": _cell(row, "LIMIT_GB"),
            "已用(GB)": _cell(row, "USED_GB"),
            "使用率(%)": _cell(row, "PCT_USED"),
            "可回收(GB)": _cell(row, "RECLAIMABLE_GB"),
            "文件数": _cell(row, "NUMBER_OF_FILES"),
        })
    sc_items.append(_listed("快速恢复区(FRA)", "oracle.flashback.area_usage",
                            display_fra, alias="闪回区使用情况",
                            analysis=_analysis("ORA.STORAGE.FRA")))

    # ── Autoextend display ──
    ae_rows = ctx.tables.get("数据文件自动扩展余量", [])
    display_ae: list[dict[str, Any]] = []
    for row in ae_rows[:20]:
        display_ae.append({
            "表空间": str(row.get("TABLESPACE_NAME","")).strip(),
            "数据文件数": row.get("DATAFILE_COUNT",""),
            "当前(MB)": row.get("CURRENT_MB",""),
            "最大(MB)": row.get("MAX_MB",""),
            "自动扩展文件数": row.get("AUTOEXTEND_FILES",""),
        })
    sc_items.append(_item("数据文件自动扩展", "ORA.CONFIG.AUTOEXTEND",
                           rows=display_ae,
                           analysis=_analysis("ORA.CONFIG.AUTOEXTEND")))

    # ── 自动段空间管理（批 13）：表空间级属性，归容量章 ──
    assm_rows = ctx.find_table("oracle.awr.assm_status", "自动段空间管理")
    display_assm: list[dict[str, Any]] = []
    for row in assm_rows[:20]:
        display_assm.append({
            "表空间": _cell(row, "TABLESPACE_NAME"),
            "段空间管理": _cell(row, "SEGMENT_SPACE_MANAGEMENT"),
            "区管理": _cell(row, "EXTENT_MANAGEMENT"),
        })
    sc_items.append(_listed("自动段空间管理 (ASSM)", "oracle.awr.assm_status",
                            display_assm, alias="自动段空间管理"))

    sections.append({"section_id": "storage_capacity", "title": "存储与容量检查", "items": sc_items})

    # ── 6. Oracle 实例检查 (oracle_instance) ──
    archive_rows = ctx.find_table("归档模式检查", "归档模式", "archive_mode")
    arch_display: list[dict[str, Any]] = []
    for row in archive_rows[:5]:
        # Parse columns regardless of tab/pipe separator
        if "LOG_MODE" in row and "ARCHIVE" in row:
            arch_display.append({"参数": "LOG_MODE", "值": row.get("LOG_MODE", row.get("ARCHIVE", ""))})
            arch_display.append({"参数": "ARCHIVE", "值": row.get("ARCHIVE", "")})
        else:
            # Pipe-delimited: split combined cell
            for rk, rv in row.items():
                if "|" in rk:
                    parts = rk.split("|")
                    vals = rv.split("|") if rv else []
                    for i, p in enumerate(parts):
                        arch_display.append({"参数": p.strip(), "值": vals[i].strip() if i < len(vals) else ""})
                    break
            else:
                arch_display.append({"参数": "归档模式", "值": next(iter(row.values()), "")})
    # ── Redo log display ──
    redo_rows = ctx.tables.get("Redo 日志组信息", [])
    display_redo: list[dict[str, Any]] = []
    for row in redo_rows[:10]:
        display_redo.append({
            "组号": row.get("GROUP#",""),
            "线程": row.get("THREAD#",""),
            "序列号": row.get("SEQUENCE#",""),
            "大小(MB)": row.get("SIZE_MB",""),
            "成员数": row.get("MEMBERS",""),
            "状态": str(row.get("STATUS","")).strip(),
        })
    oi_items: list[dict[str, Any]] = [
        # ── Data layer ──
        _item("关键初始化参数", "oracle.init_params", rows=[
            # ★ 批 16：这里原写死 `[:80]`，把 control_files 的 86 字符值切成 `…/ZYDB/control`，
            #   读起来像"第二个控制文件名叫 control" —— 与取数层 sqlplus 折行（a80）同源的误读。
            #   值列**不截断**：SQL 采集侧已 `SUBSTR(value,1,400)` 收口，Word 单元格自行折行；
            #   同一节的「块校验与丢失写保护参数」本就没有截断，此处属离群写法。
            {"参数": str(r.get("NAME","")).strip(), "值": str(r.get("VALUE","")).strip()}
            for r in ctx.tables.get("关键初始化参数", [])[:40]
        ]),
        _item("Redo 日志组信息", "oracle.redo_logs", rows=display_redo),
        # 控制文件路径在这里构建,避免未定义错误
        _item("控制文件路径", "oracle.control_files", rows=[
            {"路径": str(row.get("NAME","") or row.get("STATUS |NAME","")).strip()}
            for row in ctx.tables.get("控制文件", [])
        ]),
        # ── Analysis layer ──
        _item("归档模式检查", "ORA.ARCHIVE.MODE",
               rows=arch_display,
               analysis=_analysis("ORA.ARCHIVE.MODE")),
        _item("Redo 日志评估", "ORA.CONFIG.REDO_MEMBER",
               analysis=_analysis("ORA.CONFIG.REDO_MEMBER")),
    ]

    # Checksum params
    chk_rows = ctx.tables.get("块校验与丢失写保护参数", [])
    if chk_rows:
        display_chk: list[dict[str, Any]] = []
        for row in chk_rows[:10]:
            display_chk.append({
                "参数": row.get("NAME", ""),
                "值": row.get("VALUE", ""),
            })
        oi_items.append(_item("块校验参数", "ORA.DATA.CHECKSUM",
                               rows=display_chk,
                               analysis=_analysis("ORA.DATA.CHECKSUM")))

    # Scheduler failures
    sched_rows = ctx.find_table("Scheduler 失败", "Scheduler失败", "scheduler_failures")
    display_sched: list[dict[str, Any]] = []
    for row in sched_rows[:20]:
        display_sched.append({
            "作业名": row.get("JOB_NAME") or row.get("NAME", ""),
            "状态": row.get("STATE") or row.get("STATUS", ""),
            "失败时间": row.get("LAST_START_DATE", ""),
        })
    oi_items.append(_item("Scheduler 作业状态", "ORA.JOB.FAILURE",
                           rows=display_sched,
                           analysis=_analysis("ORA.JOB.FAILURE")))

    # ── 自动维护任务 / 管理包访问（批 13）：实例级配置，归实例章 ──
    auto_stats_rows = ctx.find_table("oracle.awr.auto_stats_job", "自动统计信息收集 Job")
    display_auto_stats: list[dict[str, Any]] = []
    for row in auto_stats_rows[:10]:
        display_auto_stats.append({
            "任务": _cell(row, "CLIENT_NAME"),
            "状态": _cell(row, "STATUS"),
            "维护窗口组": _cell(row, "WINDOW_GROUP"),
        })
    oi_items.append(_listed("自动维护任务", "oracle.awr.auto_stats_job",
                            display_auto_stats, alias="自动统计信息收集 Job"))

    pack_rows = ctx.find_table("oracle.awr.package_access", "管理包访问设置")
    display_pack: list[dict[str, Any]] = []
    for row in pack_rows[:10]:
        display_pack.append({
            "参数": _cell(row, "NAME"),
            "值": _cell(row, "VALUE"),
        })
    oi_items.append(_listed("管理包访问设置", "oracle.awr.package_access",
                            display_pack, alias="管理包访问设置"))

    sections.append({"section_id": "oracle_instance", "title": "Oracle 实例检查", "items": oi_items})

    # ── 7. 性能检查 (oracle_performance) ──
    perf_rows = [
        {
            "指标": "Commits/s",
            "平均值": f"{ora.get('commits_per_sec', {}).get('average', '-')}",
            "最大值": f"{ora.get('commits_per_sec', {}).get('max', '-')}",
        },
        {
            "指标": "物理读/s",
            "平均值": f"{ora.get('physical_reads_per_sec', {}).get('average', '-')}",
            "最大值": f"{ora.get('physical_reads_per_sec', {}).get('max', '-')}",
        },
        {
            "指标": "物理写/s",
            "平均值": f"{ora.get('physical_writes_per_sec', {}).get('average', '-')}",
            "最大值": f"{ora.get('physical_writes_per_sec', {}).get('max', '-')}",
        },
        {
            "指标": "逻辑读/s",
            "平均值": f"{ora.get('logical_reads_per_sec', {}).get('average', '-')}",
            "最大值": f"{ora.get('logical_reads_per_sec', {}).get('max', '-')}",
        },
        {
            "指标": "Redo(MB/s)",
            "平均值": "%.2f" % (ora.get('redo_bytes_per_sec', {}).get('average', 0) / 1024 / 1024) if ora.get('redo_bytes_per_sec', {}).get('average') else "-",
            "最大值": "%.2f" % (ora.get('redo_bytes_per_sec', {}).get('max', 0) / 1024 / 1024) if ora.get('redo_bytes_per_sec', {}).get('max') else "-",
        },
    ]
    # ★ 命中率/比例**不能**和上面的速率同表：速率是"平均值+最大值"两列，命中率只有
    #   一个"值"。混在一张表里，渲染层按行的键并集出列，于是每一行都缺两个格子、
    #   全部填成「未采集」—— 读者会以为一半指标没采到，其实只是行形不同。
    #   命中率/比例落到各自的规则项里做单行表（7.2 / 7.4），一张表只留一种行形。
    def _ratio_row(name: str, value: Any) -> list[dict[str, Any]]:
        shown = f"{value}%" if value is not None else ""
        return [{"指标": name, "值": shown}] if shown else []
    # ── Library Cache + Wait Events + Top SQL + IO hotfile displays ──
    lc_rows = ctx.tables.get("Library Cache 命中率", [])
    display_lc: list[dict[str, Any]] = []
    for row in lc_rows[:10]:
        display_lc.append({
            "Namespace": str(row.get("NAMESPACE","")).strip(),
            "GETS": row.get("GETS",""),
            "GETHIT%": row.get("GETHIT_PCT",""),
            "PINS": row.get("PINS",""),
            "PINHIT%": row.get("PINHIT_PCT",""),
            "RELOADS": row.get("RELOADS",""),
        })

    wait_rows = ctx.tables.get("等待事件 TOP10（数据库级）", [])
    display_wait: list[dict[str, Any]] = []
    for row in wait_rows[:10]:
        display_wait.append({
            "等待事件": str(row.get("EVENT","")).strip(),
            "等待次数": row.get("TOTAL_WAITS",""),
            "等待时间(s)": row.get("TIME_WAITED","") if safe_float(row.get("TIME_WAITED")) else row.get("TIME_WAITED",""),
            "平均等待(cs)": row.get("AVG_WAIT_CS",""),
        })

    sql_rows = ctx.tables.get("Top 20 SQL (按逻辑读)", [])
    display_sql: list[dict[str, Any]] = []
    for row in sql_rows[:10]:
        display_sql.append({
            "SQL_ID": row.get("SQL_ID",""),
            "模块": str(row.get("MODULE","")).strip(),
            "逻辑读": row.get("BUFFER_GETS",""),
            "磁盘读": row.get("DISK_READS",""),
            "耗时(min)": row.get("ELAPSED_MIN",""),
        })

    io_rows = ctx.tables.get("IO 消耗最高数据文件 TOP10", [])
    display_io: list[dict[str, Any]] = []
    for row in io_rows[:10]:
        fname = str(row.get("FILE_NAME","")).strip()
        display_io.append({
            "数据文件": fname[-50:] if len(fname) > 50 else fname,
            "物理读": row.get("PHYRDS",""),
            "物理写": row.get("PHYWRTS",""),
            "总IO": row.get("TOTAL_IO",""),
        })

    cpu_rows = ctx.tables.get("CPU 高消耗会话 TOP10", [])
    display_cpu: list[dict[str, Any]] = []
    for row in cpu_rows[:10]:
        display_cpu.append({
            "SID": row.get("SID",""),
            "Serial#": row.get("SERIAL#",""),
            "用户名": str(row.get("USERNAME","")).strip(),
            "CPU(s)": row.get("CPU_SECS",""),
            "机器": str(row.get("MACHINE","")).strip(),
        })

    op_items: list[dict[str, Any]] = [
        _item("Oracle 性能指标", "oracle.performance", perf_rows),
        _item("Buffer Cache 命中率", "ORA.PERFORMANCE.BUFFER_CACHE",
               rows=_ratio_row("Buffer Cache 命中率", ora.get('buffer_cache_hit_pct')),
               analysis=_analysis("ORA.PERFORMANCE.BUFFER_CACHE")),
        _item("Library Cache 命中率", "ORA.PERFORMANCE.LIBRARY_CACHE",
               rows=display_lc,
               analysis=_analysis("ORA.PERFORMANCE.LIBRARY_CACHE")),
        _item("磁盘排序比例", "ORA.PERFORMANCE.SORT_DISK",
               rows=_ratio_row("磁盘排序比例", ora.get('sort_disk_ratio_pct')),
               analysis=_analysis("ORA.PERFORMANCE.SORT_DISK")),
        _item("数据库等待事件", "ORA.PERFORMANCE.WAIT_EVENTS",
               rows=display_wait,
               analysis=_analysis("ORA.PERFORMANCE.WAIT_EVENTS")),
        _item("Top SQL (按逻辑读)", "ORA.PERFORMANCE.SQL_EXECUTION",
               rows=display_sql,
               analysis=_analysis("ORA.PERFORMANCE.SQL_EXECUTION")),
        _item("Top SQL (按执行时间)", "oracle.top_sql_elapsed", rows=[
            {"SQL_ID": r.get("SQL_ID",""),"模块": str(r.get("MODULE","")).strip(),"耗时(min)": r.get("ELAPSED_MIN",""),
             "CPU(s)": r.get("CPU_SEC",""),"执行次数": r.get("EXECUTIONS",""),"逻辑读": r.get("BUFFER_GETS","")}
            for r in ctx.tables.get("Top 20 SQL (按执行时间)", [])[:10]
        ]),
        _item("IO 热点数据文件", "ORA.PERFORMANCE.IO_HOTFILE",
               rows=display_io,
               analysis=_analysis("ORA.PERFORMANCE.IO_HOTFILE")),
        _item("CPU 高消耗会话", "oracle.cpu_top_sessions", rows=display_cpu),
    ]

    # ── UNDO / 临时段 / 共享池（批 13）：报告原先只展"性能指标"与"命中率规则项" ──
    undo_usage_rows = ctx.find_table("oracle.performance.undo_usage", "UNDO 使用统计")
    display_undo_usage: list[dict[str, Any]] = []
    for row in undo_usage_rows[:20]:
        display_undo_usage.append({
            "开始时间": _cell(row, "BEGIN_TIME"),
            "结束时间": _cell(row, "END_TIME"),
            "UNDO 块": _cell(row, "UNDOBLKS"),
            "事务数": _cell(row, "TXNCOUNT"),
            "最长查询(s)": _cell(row, "MAXQUERYLEN"),
            "快照过旧错误": _cell(row, "SSOLDERRCNT"),
            "空间不足错误": _cell(row, "NOSPACEERRCNT"),
        })
    op_items.append(_listed("UNDO 使用统计", "oracle.performance.undo_usage",
                            display_undo_usage, alias="UNDO 使用统计"))

    undo_param_rows = ctx.find_table("oracle.performance.undo_params", "UNDO 参数")
    display_undo_param: list[dict[str, Any]] = []
    for row in undo_param_rows[:10]:
        display_undo_param.append({
            "参数": _cell(row, "NAME"),
            "值": _cell(row, "VALUE"),
        })
    op_items.append(_listed("UNDO 参数", "oracle.performance.undo_params",
                            display_undo_param, alias="UNDO 参数"))

    temp_sort_rows = ctx.find_table("oracle.performance.temp_sort_usage", "临时表空间排序段使用")
    display_temp_sort: list[dict[str, Any]] = []
    for row in temp_sort_rows[:10]:
        display_temp_sort.append({
            "实例": _cell(row, "INSTANCE_NAME"),
            "临时表空间": _cell(row, "TABLESPACE_NAME"),
            "当前用户": _cell(row, "CURRENT_USERS"),
            "总大小(MB)": _cell(row, "TOTAL_MB"),
            "已用(MB)": _cell(row, "USED_MB"),
            "使用率(%)": _cell(row, "PCT_USED"),
            "空闲请求": _cell(row, "FREE_REQUESTS"),
        })
    op_items.append(_listed("临时表空间排序段", "oracle.performance.temp_sort_usage",
                            display_temp_sort, alias="临时表空间排序段使用"))

    spf_rows = ctx.find_table("oracle.performance.shared_pool_frag", "共享池碎片")
    display_spf: list[dict[str, Any]] = []
    for row in spf_rows[:10]:
        display_spf.append({
            "池": _cell(row, "POOL"),
            "空闲(MB)": _cell(row, "FREE_MB"),
            "平均空闲块(KB)": _cell(row, "AVG_FREE_KB"),
            "空闲块数": _cell(row, "FREE_CHUNKS"),
        })
    op_items.append(_listed("共享池碎片", "oracle.performance.shared_pool_frag",
                            display_spf, alias="共享池碎片"))

    plan_rows = ctx.find_table("oracle.awr.sql_plan_baseline", "SQL Plan Baseline")
    display_plan: list[dict[str, Any]] = []
    for row in plan_rows[:20]:
        display_plan.append({
            "SQL Handle": _cell(row, "SQL_HANDLE"),
            "Plan 名": _cell(row, "PLAN_NAME"),
            "来源": _cell(row, "ORIGIN"),
            "启用": _cell(row, "ENABLED"),
            "已接受": _cell(row, "ACCEPTED"),
            "创建时间": _cell(row, "CREATED"),
        })
    op_items.append(_listed("SQL Plan Baseline", "oracle.awr.sql_plan_baseline",
                            display_plan, alias="SQL Plan Baseline"))

    sections.append({"section_id": "oracle_performance", "title": "Oracle 性能检查", "items": op_items})

    # ── 8. 会话与事务检查 (sessions_transactions) ──
    long_txn_rows = ctx.tables.get("长事务检查", [])
    display_txn: list[dict[str, Any]] = []
    for row in long_txn_rows[:20]:
        display_txn.append({
            "SID": row.get("SID", ""),
            "Serial#": row.get("SERIAL#", ""),
            "用户名": row.get("USERNAME", ""),
            "状态": row.get("STATUS", ""),
            "开始时间": row.get("START_TIME", ""),
        })
    lock_rows = ctx.tables.get("锁等待链", [])
    display_lock: list[dict[str, Any]] = []
    for row in lock_rows[:20]:
        display_lock.append({
            "阻塞SID": row.get("BLOCKING_SID") or row.get("BLOCKING_SESSION", ""),
            "等待SID": row.get("WAITING_SID") or row.get("WAITING_SESSION", ""),
            "锁类型": row.get("LOCK_TYPE", ""),
            "等待时间(s)": row.get("WAIT_SECONDS", ""),
        })
    st_items: list[dict[str, Any]] = [
        _item("长时间事务", "ORA.TXN.LONG",
               rows=display_txn,
               analysis=_analysis("ORA.TXN.LONG")),
        _item("锁等待/阻塞", "ORA.LOCK.BLOCKING",
               rows=display_lock,
               analysis=_analysis("ORA.LOCK.BLOCKING")),
    ]
    sections.append({"section_id": "sessions_transactions", "title": "会话与事务检查", "items": st_items})

    # ── 9. 对象检查 (objects) ──
    inv_rows = ctx.tables.get("无效对象检查", [])
    display_inv: list[dict[str, Any]] = []
    for row in inv_rows[:30]:
        display_inv.append({
            "对象名": row.get("OBJECT_NAME", ""),
            "类型": row.get("OBJECT_TYPE", ""),
            "Owner": row.get("OWNER", ""),
            "状态": row.get("STATUS", ""),
        })
    obj_items: list[dict[str, Any]] = [
        _item("无效对象", "ORA.OBJECT.INVALID",
               rows=display_inv,
               analysis=_analysis("ORA.OBJECT.INVALID")),
        _item("不可用索引", "ORA.DATA.INDEX_UNUSABLE",
               analysis=_analysis("ORA.DATA.INDEX_UNUSABLE")),
        _item("统计信息缺失", "ORA.DATA.MISSING_STATS",
               analysis=_analysis("ORA.DATA.MISSING_STATS")),
    ]

    # ── 段 / 统计诊断（批 13）：这批采集项此前「采了有数据、一个字不进报告」──
    bloat_rows = ctx.find_table("oracle.awr.segment_bloat", "数据膨胀预估")
    display_bloat: list[dict[str, Any]] = []
    for row in bloat_rows[:20]:
        display_bloat.append({
            "Owner": _cell(row, "OWNER"),
            "表名": _cell(row, "TABLE_NAME"),
            "行数": _cell(row, "NUM_ROWS"),
            "段大小(MB)": _cell(row, "SEGMENT_MB"),
            "理论大小(MB)": _cell(row, "ESTIMATED_MB"),
            "可回收(MB)": _cell(row, "BLOAT_MB"),
        })
    obj_items.append(_listed("段空间膨胀预估", "oracle.awr.segment_bloat",
                             display_bloat, alias="数据膨胀预估",
                             analysis=_analysis("ORA.SCHEMA.SEGMENT_BLOAT")))

    hwm_rows = ctx.find_table("oracle.awr.hwm_segments", "段级高水位统计")
    display_hwm: list[dict[str, Any]] = []
    for row in hwm_rows[:20]:
        display_hwm.append({
            "Owner": _cell(row, "OWNER"),
            "段名": _cell(row, "SEGMENT_NAME"),
            "类型": _cell(row, "SEGMENT_TYPE"),
            "表空间": _cell(row, "TABLESPACE_NAME"),
            "大小(GB)": _cell(row, "SIZE_GB"),
            "块数": _cell(row, "BLOCKS"),
        })
    obj_items.append(_listed("段级高水位 (HWM)", "oracle.awr.hwm_segments",
                             display_hwm, alias="段级高水位统计"))

    index_usage_rows = ctx.find_table("oracle.awr.index_usage", "索引使用状态")
    display_index_usage: list[dict[str, Any]] = []
    for row in index_usage_rows[:20]:
        display_index_usage.append({
            "索引名": _cell(row, "INDEX_NAME"),
            "表名": _cell(row, "TABLE_NAME"),
            "监控": _cell(row, "MONITORING"),
            "是否使用": _cell(row, "USED"),
            "监控开始": _cell(row, "START_MONITORING"),
        })
    obj_items.append(_listed("索引使用状态", "oracle.awr.index_usage",
                             display_index_usage, alias="索引使用状态"))

    missing_stats_rows = ctx.find_table("oracle.awr.missing_stats", "缺失统计信息表")
    display_missing_stats: list[dict[str, Any]] = []
    for row in missing_stats_rows[:20]:
        display_missing_stats.append({
            "Owner": _cell(row, "OWNER"),
            "表名": _cell(row, "TABLE_NAME"),
            "行数": _cell(row, "NUM_ROWS"),
            "上次分析": _cell(row, "LAST_ANALYZED"),
            "统计陈旧": _cell(row, "STALE_STATS"),
        })
    obj_items.append(_listed("陈旧统计信息 (TOP20)", "oracle.awr.missing_stats",
                             display_missing_stats, alias="缺失统计信息表"))

    sections.append({"section_id": "objects", "title": "对象检查", "items": obj_items})

    # ── 10. 安全与审计检查 (security) ──
    pwd_rows = ctx.tables.get("用户密码过期预警（账户非OPEN或30天内到期）", []) or ctx.tables.get("用户密码过期预警", [])
    display_pwd: list[dict[str, Any]] = []
    for row in pwd_rows[:20]:
        display_pwd.append({
            "用户名": row.get("USERNAME", ""),
            "账户状态": row.get("ACCOUNT_STATUS", ""),
            "过期日期": row.get("EXPIRY_DATE", ""),
        })
    # ── DBA users + locked accounts ──
    dba_rows = ctx.tables.get("特权用户 (DBA角色)", [])
    display_dba: list[dict[str, Any]] = []
    for row in dba_rows[:20]:
        display_dba.append({
            "用户名": row.get("USERNAME",""),
            "账户状态": row.get("ACCOUNT_STATUS",""),
        })
    locked_rows = ctx.tables.get("锁定账户检查", [])
    display_locked: list[dict[str, Any]] = []
    for row in locked_rows[:20]:
        display_locked.append({
            "用户名": row.get("USERNAME",""),
            "账户状态": row.get("ACCOUNT_STATUS",""),
            "锁定日期": row.get("LOCK_DATE",""),
        })
    sec_items: list[dict[str, Any]] = [
        _item("账户密码状态", "ORA.SECURITY.PASSWORD_EXPIRY",
               rows=display_pwd,
               analysis=_analysis("ORA.SECURITY.PASSWORD_EXPIRY")),
        _item("DBA 权限用户", "ORA.SECURITY.DBA_COUNT",
               rows=display_dba,
               analysis=_analysis("ORA.SECURITY.DBA_COUNT")),
        _item("锁定账户", "ORA.SECURITY.ACCOUNT_LOCKED",
               rows=display_locked,
               analysis=_analysis("ORA.SECURITY.ACCOUNT_LOCKED")),
    ]

    # ── 权限画像 / 口令 Profile（批 13）：报告原先只有"账户密码过期 + DBA 用户 + 锁定账户"
    #    三类，`oracle.audit` 的其余 7 项一个字都不进报告。规则 ORA.SECURITY.PASSWORD_PROFILE
    #    在批 11 已就位，这里把它的数据源接到「密码配置文件参数」条目上。
    priv_rows = ctx.find_table("oracle.audit.system_privileges", "系统特权授予")
    display_priv: list[dict[str, Any]] = []
    for row in priv_rows[:30]:
        display_priv.append({
            "被授予者": _cell(row, "GRANTEE"),
            "系统特权": _cell(row, "PRIVILEGE"),
            "可转授": _cell(row, "ADMIN_OPTION"),
            "通用": _cell(row, "COMMON"),
        })
    sec_items.append(_listed("系统特权授予（可转授）", "oracle.audit.system_privileges",
                             display_priv, alias="系统特权授予"))

    profile_rows = ctx.find_table("oracle.audit.profile_params", "密码配置文件参数")
    display_profile: list[dict[str, Any]] = []
    for row in profile_rows[:20]:
        display_profile.append({
            "Profile": _cell(row, "PROFILE"),
            "资源名": _cell(row, "RESOURCE_NAME"),
            "类别": _cell(row, "RESOURCE_TYPE"),
            "限制值": _cell(row, "LIMIT"),
        })
    sec_items.append(_listed("密码配置文件参数", "oracle.audit.profile_params",
                             display_profile, alias="密码配置文件参数",
                             analysis=_analysis("ORA.SECURITY.PASSWORD_PROFILE")))

    profile_nd_rows = ctx.find_table("oracle.audit.profile_nondefault", "默认配置文件非默认设置")
    display_profile_nd: list[dict[str, Any]] = []
    for row in profile_nd_rows[:20]:
        display_profile_nd.append({
            "Profile": _cell(row, "PROFILE"),
            "资源名": _cell(row, "RESOURCE_NAME"),
            "限制值": _cell(row, "LIMIT"),
        })
    sec_items.append(_listed("自定义 Profile 口令设置", "oracle.audit.profile_nondefault",
                             display_profile_nd, alias="默认配置文件非默认设置"))

    pwf_rows = ctx.find_table("oracle.audit.password_file_members", "密码文件成员")
    display_pwf: list[dict[str, Any]] = []
    for row in pwf_rows[:20]:
        display_pwf.append({
            "用户名": _cell(row, "USERNAME"),
            "SYSDBA": _cell(row, "SYSDB", "SYSDBA"),
            "SYSOPER": _cell(row, "SYSOP", "SYSOPER"),
            "SYSASM": _cell(row, "SYSAS", "SYSASM"),
        })
    sec_items.append(_listed("密码文件特权成员", "oracle.audit.password_file_members",
                             display_pwf, alias="密码文件成员"))

    fga_rows = ctx.find_table("oracle.audit.fga", "审计策略检查")
    display_fga: list[dict[str, Any]] = []
    for row in fga_rows[:20]:
        display_fga.append({
            "策略名": _cell(row, "POLICY_NAME"),
            "对象 Schema": _cell(row, "OBJECT_SCHEMA"),
            "对象名": _cell(row, "OBJECT_NAME"),
            "启用": _cell(row, "ENABLED"),
        })
    sec_items.append(_listed("细粒度审计策略 (FGA)", "oracle.audit.fga",
                             display_fga, alias="审计策略检查"))

    obj_audit_rows = ctx.find_table("oracle.audit.object_audit", "对象审计统计")
    display_obj_audit: list[dict[str, Any]] = []
    for row in obj_audit_rows[:20]:
        display_obj_audit.append({
            "Owner": _cell(row, "OWNER"),
            "对象名": _cell(row, "OBJ_NAME"),
            "操作": _cell(row, "ACTION_NAME"),
            "次数": _cell(row, "EXEC_COUNT"),
        })
    sec_items.append(_listed("对象审计统计（近7天）", "oracle.audit.object_audit",
                             display_obj_audit, alias="对象审计统计"))

    failed_login_rows = ctx.find_table("oracle.audit.failed_logins", "失败登录审计统计")
    display_failed: list[dict[str, Any]] = []
    for row in failed_login_rows[:20]:
        display_failed.append({
            "用户": _cell(row, "USERID"),
            "失败次数": _cell(row, "CNT"),
            "最近尝试": _cell(row, "LAST_ATTEMPT"),
        })
    sec_items.append(_listed("失败登录统计（近7天）", "oracle.audit.failed_logins",
                             display_failed, alias="失败登录审计统计"))

    sections.append({"section_id": "security", "title": "安全与审计检查", "items": sec_items})

    # ── 11. 备份与恢复检查 (backup_recovery) ──
    backup_rows = ctx.tables.get("最近RMAN备份任务状态", [])
    display_bk: list[dict[str, Any]] = []
    for row in backup_rows[:20]:
        display_bk.append({
            "备份类型": row.get("BACKUP_TYPE", ""),
            "状态": row.get("STATUS", ""),
            "开始时间": row.get("START_TIME", ""),
            "结束时间": row.get("COMPLETION_TIME", ""),
        })
    # ── ADR/ALERT display ──
    adr_rows = ctx.tables.get("ADR 预警日志路径", [])
    display_adr: list[dict[str, Any]] = []
    for row in adr_rows:
        display_adr.append({"参数": str(row.get("NAME","")).strip(), "值": str(row.get("VALUE","") or "").strip()})
    sql_err_rows = ctx.tables.get("最近 SQL 错误 (近24h)", [])
    display_err: list[dict[str, Any]] = []
    for row in sql_err_rows[:20]:
        display_err.append({
            "时间": str(row.get("ORIGINATING_TIMESTAMP",""))[:19],
            "错误码": str(row.get("ERROR_NUMBER","") or row.get("MESSAGE_GROUP","")).strip(),
            "消息": str(row.get("MESSAGE_TEXT","") or row.get("PROBLEM_KEY",""))[:60].strip(),
        })
    adr_diag_rows = ctx.tables.get("ADR Diag 路径", [])
    display_diag: list[dict[str, Any]] = []
    for row in adr_diag_rows:
        display_diag.append({"参数": str(row.get("NAME","")).strip(), "值": str(row.get("VALUE","") or "").strip()})

    br_items: list[dict[str, Any]] = [
        # ── Data layer ──
        _item("归档趋势（近30天）", "oracle.archive_trend", rows=[
            {"归档日期": str(r.get("ARCHIVE_DATE","") or r.get("DAY","")).strip(),
             "归档数量": r.get("ARCHIVE_COUNT","") or r.get("LOGS",""),
             "大小(GB)": r.get("ARCHIVE_SIZE_GB","") or r.get("SIZE_GB","")}
            for r in ctx.tables.get("近30天每日归档量", [])[:30]
        ]),
        _item("RMAN 备份配置", "oracle.rman_config", rows=[
            {"配置项": str(r.get("NAME","")).strip(), "值": str(r.get("VALUE","")).strip()}
            for r in ctx.tables.get("备份策略参数（RMAN CONFIGURE）", [])[:20]
        ]),
        _item("ADR 诊断路径", "oracle.adr_diag", rows=display_diag),
        _item("近期 SQL 错误（24h）", "oracle.recent_sql_errors", rows=display_err),
        # ── Analysis layer ──
        _item("RMAN 备份检查", "ORA.BACKUP.RECENT",
               rows=display_bk,
               analysis=_analysis("ORA.BACKUP.RECENT")),
    ]

    # Block corruption
    bad_rows = ctx.tables.get("数据库坏块记录", [])
    display_bad: list[dict[str, Any]] = []
    for row in bad_rows[:10]:
        display_bad.append({
            "文件号": row.get("FILE#", ""),
            "块号": row.get("BLOCK#", ""),
            "坏块数": row.get("BLOCKS", ""),
            "变更号": row.get("CORRUPTION_CHANGE#", ""),
        })
    br_items.append(_item("物理坏块", "ORA.DATA.BLOCK_CORRUPTION",
                           rows=display_bad,
                           analysis=_analysis("ORA.DATA.BLOCK_CORRUPTION")))

    # Datafile header
    df_rows = ctx.find_table("数据文件头异常", "datafile_header")
    display_df: list[dict[str, Any]] = []
    for row in df_rows[:20]:
        display_df.append({
            "文件号": row.get("FILE#", ""),
            "状态": row.get("STATUS", ""),
            "错误": row.get("ERROR", ""),
        })
    br_items.append(_item("数据文件头状态", "ORA.DATA.FILE_HEADER",
                           rows=display_df,
                           analysis=_analysis("ORA.DATA.FILE_HEADER")))

    # Control file redundancy
    # ★ 批 16：这里原先读 `NAME` / `VALUE`，而 `oracle.validation.controlfile_multiplex.psv`
    #   的列是 `CONTROLFILE_COUNT` / `CONTROLFILE_PATHS` ⇒ 两列全取空，11.8 整张表不渲染
    #   （报告里只剩一行「原始记录：1 条」）。与批 15 的 P0-3 同源：**展示层的键名必须与
    #   解析层的产出一致**；取值一律走 `_cell()`，别写死单一拼法。
    ctrl_rows = ctx.tables.get("控制文件多路复用", []) or ctx.tables.get("controlfile_redundancy", [])
    display_ctrl: list[dict[str, Any]] = []
    for row in ctrl_rows[:10]:
        display_ctrl.append({
            "控制文件数": _cell(row, "CONTROLFILE_COUNT", "COUNT"),
            "路径": _cell(row, "CONTROLFILE_PATHS", "PATHS"),
        })
    br_items.append(_item("控制文件冗余", "ORA.CONTROL.REDUNDANCY",
                           rows=display_ctrl,
                           analysis=_analysis("ORA.CONTROL.REDUNDANCY")))

    # ── DataGuard / 高可用（批 13）：6 项采集此前全部不进报告。
    #    动线对齐 MySQL —— 复制与高可用**折叠进备份章**，不另开一章（MySQL 的
    #    「日志、备份与复制」就是一章收口；Oracle 原先把"归档模式"放实例章、
    #    "归档趋势"放备份章，同一主题拆两处，读者要在两章之间拼）。
    dg_dest_rows = ctx.find_table("oracle.dg.archive_dest", "日志传输服务状态")
    display_dg_dest: list[dict[str, Any]] = []
    for row in dg_dest_rows[:10]:
        display_dg_dest.append({
            "目的地": _cell(row, "DEST_ID"),
            "名称": _cell(row, "DEST_NAME"),
            "类型": _cell(row, "TYPE"),
            "状态": _cell(row, "STATUS"),
            "错误": _cell(row, "ERROR"),
            "传输模式": _cell(row, "TRANSMIT_MODE"),
            "压缩": _cell(row, "COMPRESSION"),
        })
    br_items.append(_listed("日志传输服务状态", "oracle.dg.archive_dest",
                            display_dg_dest, alias="日志传输服务状态",
                            analysis=_analysis("ORA.DG.TRANSPORT_ERROR")))

    dg_broker_rows: list[dict[str, Any]] = []
    for row in ctx.find_table("oracle.dg.broker_enabled", "DG Broker 启用状态")[:5]:
        dg_broker_rows.append({"参数": _cell(row, "NAME"), "值": _cell(row, "VALUE")})
    for row in ctx.find_table("oracle.dg.broker_params", "DG Broker 配置参数")[:10]:
        dg_broker_rows.append({"参数": _cell(row, "NAME"), "值": _cell(row, "VALUE")})
    br_items.append(_listed("DataGuard Broker 配置", "oracle.dg.broker_params",
                            dg_broker_rows, alias="DG Broker"))

    dg_mode_rows = ctx.find_table("oracle.dg.protection_mode", "保护模式与主库统计")
    display_dg_mode: list[dict[str, Any]] = []
    for row in dg_mode_rows[:20]:
        display_dg_mode.append({"统计项": _cell(row, "NAME"), "值": _cell(row, "VALUE")})
    br_items.append(_listed("保护模式与备库延迟", "oracle.dg.protection_mode",
                            display_dg_mode, alias="保护模式与主库统计",
                            analysis=_analysis("ORA.DG.APPLY_LAG")))

    dg_gap_rows = ctx.find_table("oracle.dg.log_sequence_gap", "主备库日志序差距")
    display_dg_gap: list[dict[str, Any]] = []
    for row in dg_gap_rows[:10]:
        display_dg_gap.append({
            "线程": _cell(row, "THREAD#", "THREAD"),
            "日志序差距": _cell(row, "GAP"),
        })
    br_items.append(_listed("主备库日志序差距", "oracle.dg.log_sequence_gap",
                            display_dg_gap, alias="主备库日志序差距"))

    dg_event_rows = ctx.find_table("oracle.dg.recent_events", "最近 DG 事件")
    display_dg_event: list[dict[str, Any]] = []
    for row in dg_event_rows[:20]:
        display_dg_event.append({
            "时间": _cell(row, "TIMESTAMP"),
            "消息": _cell(row, "MESSAGE"),
            "错误码": _cell(row, "ERROR_CODE"),
        })
    br_items.append(_listed("最近 DataGuard 事件", "oracle.dg.recent_events",
                            display_dg_event, alias="最近 DG 事件",
                            analysis=_analysis("ORA.DG.EVENT_ERROR")))

    sections.append({"section_id": "backup_recovery", "title": "备份、恢复与高可用检查", "items": br_items})

    return sections

def build_report_model(analysis: dict[str, Any], inspection_sections_list: list[list[dict[str, Any]]], output: Path) -> dict[str, Any]:
    """Build the final report_model.json matching MySQL v3 contract."""
    instances = analysis.get("instances", [])
    primary = instances[0] if instances else {}
    identity = primary.get("identity", {})
    generated = analysis.get("analyzer", {}).get("generated_at", now_iso())
    quality = primary.get("collection_quality", {})
    findings_all = [f for inst in instances for f in inst.get("findings", [])]

    # ── cover ──
    db_name = identity.get("db_name") or identity.get("instance_tag", "Oracle Database")
    ora_ver = identity.get("ora_version", "")
    cover = {
        "title": "Oracle 数据库巡检分析报告",
        "inspection_target": db_name,
        "database_version": ora_ver,
        "report_version": "V2.0",
        "inspection_date": (generated or "")[:10],
    }

    # ── document_control ──
    document_control = {
        "customer": "待填写",
        "database": "Oracle",
        "report_version": "V2.0",
        "generated_at": generated,
    }

    # ── overview ──
    tag = identity.get("instance_tag", "")
    hostname, ip_addr = parse_instance_tag(tag)
    overview = {
        "host": hostname or identity.get("host", ""),
        "ip": ip_addr,
        "database_version": ora_ver,
        "collection_time": identity.get("collection_started_at", generated),
        "data_quality": quality,
    }

    # ── topology ──
    nodes = [{
        "hostname": hostname or identity.get("host", ""),
        "ip": ip_addr,
        "port": int(identity.get("port", 1521)),
        "role_observed": identity.get("db_role", "PRIMARY"),
        "version": ora_ver,
        "instance_tag": identity.get("instance_tag", db_name),
    }]
    topology = {
        "mode": "single_instance" if len(instances) <= 1 else "multi_instance",
        "nodes": nodes,
        "edges": [],
    }

    # ── health_assessment ──
    health = primary.get("health_summary", {})
    counts = health.get("counts", {})
    health_assessment = {
        "score": health.get("score", 100),
        "grade": health.get("grade", "good"),
        "counts": {
            "critical": counts.get("critical", 0),
            "high": counts.get("high", 0),
            "medium": counts.get("medium", 0),
            "low": counts.get("low", 0),
        },
        "scoring_policy": health.get("scoring_policy", ""),
    }

    # ── comprehensive_conclusions ──
    comprehensive_conclusions = primary.get("comprehensive_conclusions", [])

    # ── risk_register ──
    risk_register = findings_all

    # ── optimization_plan ──
    priorities: dict[str, list[dict[str, Any]]] = {"P1": [], "P2": [], "P3": []}
    for f in findings_all:
        sev = f.get("severity", "low")
        pri = "P1" if sev in ("critical", "high") else "P2" if sev == "medium" else "P3"
        priorities[pri].append({
            "finding_id": f.get("finding_id", ""),
            "title": f.get("title", ""),
            "recommendation": f.get("recommendation", ""),
        })

    # ── collection_gaps ──
    collection_gaps: list[dict[str, Any]] = []
    gap_status_cn: dict[str, str] = {
        "partial": "部分可用", "permission_denied": "权限不足",
        "timeout": "超时", "error": "失败",
        "insufficient_history": "历史不足",
        "external_evidence_required": "需要外部证据",
    }
    for item in quality.get("non_ok_items", []):
        status = str(item.get("status", ""))
        if status in {"ok", "empty", "not_applicable"}:
            continue
        collection_gaps.append({
            "item_id": item.get("item_id", ""),
            "status": status,
            "reason": item.get("reason", ""),
            "recommended_action": "修复采集条件后重采；已取得的部分数据仍保留为证据。",
            "collector_change_required": False,
        })

    # ── appendix ──
    sampling = primary.get("metrics", {}).get("sampling_context", {})
    appendix = {
        "collection_window": {
            "realtime_window_seconds": sampling.get("realtime", {}).get("sample_count"),
            "oracle_sample_points": sampling.get("realtime", {}).get("oracle_stat_points"),
            "short_window": False,
            "history": sampling.get("history", {}),
        },
        "data_quality": quality,
        "rule_evaluations": primary.get("rule_evaluations", []),
        "disclaimer": "本报告基于采集窗口内可获得的证据自动生成。短时采样不代表全天负载；未采集或证据不足的项目不作通过结论，变更前应完成业务确认、备份与回滚评估。",
    }

    # ── charts ──
    charts_raw = primary.get("charts", [])
    charts_normalized: list[dict[str, Any]] = []
    for c in charts_raw:
        source = str(c.get("path") or "")
        # 报告模型记相对路径（chart 目录随模型一起拷贝才可移植）；
        # 下游 Word 引擎以模型所在目录为根解析，绝对路径同样能命中。
        portable = source
        if source:
            try:
                portable = Path(source).relative_to(output).as_posix()
            except ValueError:
                portable = Path(source).name
        charts_normalized.append({
            "chart_id": c.get("chart_id", ""),
            "status": c.get("status") or ("generated" if source else "skipped"),
            "file": portable,
            "source_scope": c.get("source_scope", ""),
            "source_points": c.get("source_points", 0),
        })

    model = {
        "schema_version": "2.0",
        "generator_contract": "oracle_inspection_report_model",
        "cover": cover,
        "document_control": document_control,
        "overview": overview,
        "topology": topology,
        "health_assessment": health_assessment,
        "comprehensive_conclusions": comprehensive_conclusions,
        "inspection_sections": inspection_sections_list[0] if inspection_sections_list else [],
        "risk_register": risk_register,
        "optimization_plan": priorities,
        "collection_gaps": collection_gaps,
        "charts": charts_normalized,
        "appendix": appendix,
    }
    # 「待客户确认事项」（清单 G1）。**条件注入**：旧基线 report_model.json 里没有这个
    # 键，无条件写入会打穿冻结契约测试（逐字典等值比对）。它派生自已触发的规则，
    # 不新增数据来源 —— 修的是「第 14 章只剩一句『未产出清单』」的空转。
    pending = _merged_confirmations(instances)
    if pending is not None:
        model["pending_confirmations"] = pending
    return model

# ---- Main Pipeline ----

