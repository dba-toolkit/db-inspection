"""MySQL presentation and report-model builder.

The builder receives normalized facts and metrics.  It does not read archives,
execute rules, generate charts, or render Word documents.  Database-specific
section wording stays here while the report contract remains stable.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from inspection_core import (
    EvidenceDisclosure,
    PackageContext,
    RemediationAction,
    safe_float,
    safe_int,
)
from inspection_core.system_checks import (
    disk_media,
    is_persistent_fstype,
    ntp_clock_offset_seconds,
    ntp_verdict,
)
from plugins.mysql.metrics import (
    backup_task_visibility,
    binlog_totals,
    config_runtime_drift,
    local_host_names,
    log_file_entries,
    mysql_uptime_seconds,
    replica_threads_running,
    row_number,
    split_self_referencing_replica_rows,
    time_evidence,
)


# MySQL 自带的系统库。判定"实例上有没有业务库"必须靠这份显式名单，
# 不能靠"名字看起来像不像系统库"，否则新建的元数据库会被误判成业务库、
# 或者业务库被误判成系统库（后者会直接把结论写成"无业务对象可评价"）。
MYSQL_SYSTEM_SCHEMAS = frozenset({
    "information_schema",
    "mysql",
    "performance_schema",
    "sys",
})

# 对象候选项明细每类的展示条数。汇总表只给计数，客户拿到报告还得回采集包翻 TSV
# 才知道是哪几张表；这里把各类前 N 项直接落到正文，其余留在证据文件里。
OBJECT_DETAIL_PER_KIND = 5

# 「日志、持久性与复制参数」组的参数清单。报告表格与跨节点对比共用这一份定义，
# 避免展示行与判定行各写一遍而错位。
DURABILITY_VARIABLES: tuple[tuple[str, str], ...] = (
    ("log_bin", "Binary Log"),
    ("binlog_format", "Binlog 格式"),
    ("gtid_mode", "GTID 模式"),
    ("enforce_gtid_consistency", "GTID 一致性"),
    ("sync_binlog", "Binlog 同步策略"),
    ("innodb_flush_log_at_trx_commit", "事务日志刷盘策略"),
    ("binlog_expire_logs_seconds", "Binlog 保留秒数"),
)


# 四组配置检查项的**唯一参数清单**：报告表格、跨节点对比、判定列全部读这一份，
# 避免「展示的行」和「判定的行」各写一遍而错位。
CONFIG_GROUP_VARIABLES: dict[str, tuple[tuple[str, str], ...]] = {
    "mysql.config.memory": (
        ("innodb_buffer_pool_size", "InnoDB Buffer Pool"),
        ("innodb_buffer_pool_instances", "Buffer Pool 实例数"),
        ("innodb_redo_log_capacity", "Redo Log 容量（变量值）"),
        ("innodb_log_file_size", "单个 Redo 文件大小"),
        ("innodb_log_files_in_group", "Redo 文件个数"),
        ("innodb_log_buffer_size", "Redo Log Buffer"),
        ("innodb_flush_method", "InnoDB 刷盘方式"),
        ("innodb_io_capacity", "InnoDB IO 容量"),
        ("innodb_io_capacity_max", "InnoDB IO 容量上限"),
        ("innodb_adaptive_hash_index", "自适应哈希索引"),
    ),
    "mysql.config.connection": (
        ("max_connections", "最大连接数"),
        ("thread_cache_size", "线程缓存"),
        ("table_open_cache", "表打开缓存"),
        ("table_definition_cache", "表定义缓存"),
        ("tmp_table_size", "内存临时表上限"),
        ("max_heap_table_size", "MEMORY 表上限"),
        ("open_files_limit", "打开文件上限"),
        ("back_log", "连接等待队列"),
        ("max_connect_errors", "连接错误上限"),
    ),
    "mysql.config.durability": DURABILITY_VARIABLES,
    "mysql.config.charset": (
        ("character_set_server", "服务端字符集"),
        ("collation_server", "服务端排序规则"),
        ("lower_case_table_names", "表名大小写策略"),
        ("sql_mode", "SQL Mode"),
        ("time_zone", "默认时区"),
    ),
}

CONFIG_GROUP_TITLES: tuple[tuple[str, str], ...] = (
    ("mysql.config.memory", "内存与 InnoDB 核心参数"),
    ("mysql.config.connection", "连接、线程与缓存参数"),
    ("mysql.config.durability", "日志、持久性与复制参数"),
    ("mysql.config.charset", "字符集与 SQL 模式"),
)

# 「待客户确认事项」的模板，按**已触发的规则**派生（rule_id → 主题 / 问题）。
# 刻意不重复判定：规则引擎已经回答"这是不是风险"，本表只回答"这件事要客户确认什么"。
# 只收两类规则 —— 需要业务语义决策的（副本定时事件归属、脏读依赖）与数据库侧无法
# 自证的（备份可恢复性）。纯技术整改项（日志轮转、redo 容量、无主键表）不进表：
# 那些直接改就行，问了只增加沟通成本，还会把清单做成第二个风险台账。
CONFIRMATION_TEMPLATES: dict[str, tuple[str, str]] = {
    "MYSQL.BACKUP.TASK_VISIBILITY": (
        "备份归属与恢复演练",
        "本机没有可确认的生效备份任务。请确认备份由谁发起（本地脚本 / 备份平台 / "
        "存储侧快照），并提供最近 3 次成功记录与至少 1 次恢复演练记录 —— "
        "「有备份」以能恢复为准，不以存在任务配置为准。",
    ),
    "MYSQL.REPLICATION.REPLICA_WRITABLE": (
        "副本定时事件的业务归属",
        "副本上仍有 ENABLED 的写入型定时事件。请确认这些作业是否依赖「每个节点各自执行一份」"
        "（例如自动生成费用、医嘱到期处理、病历拆分）；若否，应在副本侧收敛为 "
        "SLAVESIDE_DISABLED 或改由外部调度统一发起。这属于业务语义决策，不由 DBA 单方面变更。",
    ),
    "MYSQL.REPLICATION.SKIP_ERRORS": (
        "跳过复制错误的取舍与校验窗口",
        "复制链路上配置了跳过错误，主从数据可能在无人察觉的情况下分叉。关闭前必须先完成一次"
        "全量一致性校验并修复已积累的差异（否则复制会停在历史差异点上）。请确认该配置的引入"
        "原因，以及可安排的一致性校验与修复窗口。",
    ),
    "MYSQL.TRANSACTION.ISOLATION_LEVEL": (
        "隔离级别是否为应用侧要求",
        "当前隔离级别允许读到未提交数据。请确认该取值是否为应用侧主动要求、是否存在依赖读未"
        "提交数据的历史逻辑；否则建议调整为 READ-COMMITTED（动态参数，可先在低峰观察）。",
    ),
    "MYSQL.SYSTEM.IDENTITY_CONFLICT": (
        "主机标识重复是否已知",
        "集群内存在主机名或 machine-id 重复。请确认是否为模板克隆后未重置（会影响监控串数据、"
        "systemd 实例标识、DHCP 标识与授权绑定），以及监控 / 备份侧是否已做过规避。",
    ),
    "MYSQL.CONFIG.RUNTIME_DRIFT": (
        "配置值与运行值以哪一侧为准",
        "配置文件与实例运行值存在不一致，下次重启会把线上行为改成配置文件里的版本。"
        "请逐项确认期望值，并告知可安排的重启窗口（若需要）。",
    ),
    "MYSQL.RUNTIME.CONNECTION_ERRORS": (
        "连接建立失败的来源",
        "连接建立失败率偏高。请确认失败来源（连接池配置 / 防火墙 / SYN 半连接 / 口令错误）"
        "是否已有记录，以便区分应用侧与网络侧问题。",
    ),
    "MYSQL.REPLICATION.RETENTION_DRIFT": (
        "副本 Binlog 保留期与下游用途",
        "副本的 Binlog 保留期长于源端。请确认副本是否需要保留 Binlog（是否存在级联复制或"
        "下游消费者）；若不需要，可显著回收磁盘占用。",
    ),
    "MYSQL.REPLICATION.RESIDUAL_CHANNEL": (
        "残留复制通道与历史角色互换",
        "存在指向自身的残留复制通道，且 GTID 执行集可能包含非本集群的 UUID。请确认历史上是否"
        "发生过主从角色互换；确认后清理残留通道，并在文档中固化当前角色基线，避免下次巡检重复误判。",
    ),
}


def business_schema_names(rows: list[dict[str, Any]]) -> list[str]:
    """Return the non-system schema names present in ``tables/schemas.tsv``."""
    names: list[str] = []
    for row in rows:
        name = str(row.get("SCHEMA_NAME") or "").strip()
        if name and name.lower() not in MYSQL_SYSTEM_SCHEMAS and name not in names:
            names.append(name)
    return names


def pending_confirmations(
    instance: dict[str, Any], *, drift_note: str = ""
) -> list[dict[str, Any]]:
    """按已触发的规则派生「待客户确认事项」。

    与风险台账的分工：台账答「是什么风险、怎么改」，本表答「哪几件事必须由客户拍板、
    或必须由客户提供外部证据」。规则未触发则不进表 —— 没触发就说明本节点没有该问题，
    不该把模板空转成一张「欢迎确认」清单。
    """
    node = str((instance.get("identity") or {}).get("ip") or "").strip()
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
    if drift_note:
        items.append({
            "topic": "节点间参数漂移是否有意设计",
            "question": (
                "集群内存在多组参数取值不一致。请确认这些差异是有意设计（例如按节点硬件规格或"
                "读负载分别调优），还是缺少统一配置模板；若为后者，建议先收敛成基线模板再逐节点"
                "下发，否则故障切换后性能表现不可预期。"
            ),
            "node": node,
            "rule_id": "",
            "finding_id": "",
            "evidence": [drift_note],
        })
    return items


class MySQLPresentationBuilder:
    """Build MySQL inspection sections and the report model."""

    def __init__(self) -> None:
        # 跨实例对比需要其它节点的运行参数。build_inspection_model 是逐实例调用的，
        # 这里顺手记下各实例的 global_variables，供 attach_config_comparison 使用；
        # 手工构造的 analysis 没有这层缓存，届时回退 facts.key_variables。
        self._variables_by_instance: dict[str, dict[str, str]] = {}

    @staticmethod
    def _row_number(row: dict[str, str], *keys: str) -> float | None:
        return row_number(row, *keys)

    @staticmethod
    def _percent(value: Any) -> float | None:
        number = safe_float(str(value or "").replace("%", ""))
        return number

    @staticmethod
    def _format_bytes(value: Any) -> str | None:
        number = safe_float(value)
        if number is None:
            return None
        units = ("B", "KB", "MB", "GB", "TB", "PB")
        index = 0
        while abs(number) >= 1024 and index < len(units) - 1:
            number /= 1024
            index += 1
        return f"{number:.2f} {units[index]}"

    @staticmethod
    def _format_host_time(value: Any) -> Any:
        """Format collected local time for readers without changing stored evidence."""
        raw = str(value or "").strip()
        matched = re.match(
            r"^(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2})(?:\.\d+)?\s*([+-])(\d{2}):?(\d{2})$",
            raw,
        )
        if not matched:
            return value
        base, sign, hours, minutes = matched.groups()
        return f"{base.replace('T', ' ')}（UTC{sign}{hours}:{minutes}）"

    @staticmethod
    def _status_index(ctx: PackageContext) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        for raw in ctx.status.get("items", []):
            item = dict(raw)
            item_id = str(item.get("item_id", ""))
            status = str(item.get("status", "unknown"))
            reason = str(item.get("reason", ""))
            if item_id.startswith("system.chronyc_") and status == "error" and not reason.strip():
                status = "not_enabled"
                reason = "Chrony 服务或命令未启用"
            if item_id in {"system.filesystems", "system.inodes"} and status == "error":
                table_name = "filesystems" if item_id.endswith("filesystems") else "inodes"
                if ctx.tables.get(table_name):
                    status = "partial"
            item["status"] = status
            item["reason"] = reason
            result[item_id] = item
        return result

    @staticmethod
    def _raw_key_value_rows(path: Path) -> list[dict[str, str]]:
        if not path.exists():
            return []
        rows: list[dict[str, str]] = []
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
            rows.append({"参数": name.strip(), "值": value.strip()})
        return rows

    @staticmethod
    def _colon_key_values(path: Path) -> dict[str, str]:
        if not path.exists():
            return {}
        values: dict[str, str] = {}
        for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if ":" not in raw:
                continue
            name, value = raw.split(":", 1)
            values[name.strip()] = value.strip()
        return values

    @staticmethod
    def _mount_rows(path: Path) -> list[dict[str, str]]:
        # 判据来自公共层（inspection_core.system_checks）：本地维护的黑名单
        # 曾在 hugetlbfs 上漏过一次，/dev/hugepages 因此被当成"真实块设备挂载"
        # 写进报告。仍是黑名单而非白名单——白名单会把 nfs/cifs 一并藏掉，
        # 而"数据目录落在网络文件系统"正是要报的风险。
        if not path.exists():
            return []
        rows: list[dict[str, str]] = []
        pattern = re.compile(r"^(.*?) on (.*?) type (.*?) \((.*)\)$")
        for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
            matched = pattern.match(raw.strip())
            if not matched:
                continue
            fstype = matched.group(3).strip()
            if not is_persistent_fstype(fstype):
                continue
            rows.append({
                "设备": matched.group(1).strip(),
                "挂载点": matched.group(2).strip(),
                "类型": fstype,
                "挂载选项": matched.group(4).strip(),
            })
        return rows

    @staticmethod
    def _dmesg_error_rows(path: Path, limit: int = 20) -> list[dict[str, str]]:
        if not path.exists():
            return []
        lines = [
            line.strip()
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines()
            if line.strip()
        ]
        return [{"内核错误日志": line} for line in lines[:limit]]

    @staticmethod
    def _kernel_recommendation(ctx: "PackageContext") -> str:
        """Check OS kernel parameters for database-specific recommendations."""
        path = ctx.root / "tables/kernel_parameters.tsv"
        if not path.exists():
            return ""
        # kernel_parameters.tsv is a key-value TSV: name \t value
        kernel: dict[str, str] = {}
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "\t" in line:
                parts = line.split("\t", 1)
                if len(parts) == 2:
                    kernel[parts[0].strip()] = parts[1].strip()
        parts: list[str] = []
        # vm.swappiness: 1-10 for DB servers, default 60 is too aggressive
        swappiness_str = kernel.get("vm.swappiness", "")
        if swappiness_str:
            try:
                swappiness = int(swappiness_str)
                if swappiness > 10:
                    parts.append(
                        f"vm.swappiness={swappiness}（默认 60）→ 数据库服务器建议设为 1-10，"
                        "避免操作系统在尚有可用内存时主动换出 MySQL 内存页到 Swap"
                    )
            except ValueError:
                pass
        if parts:
            return "；".join(parts) + "。"
        return ""

    @staticmethod
    def _config_recommendation(item_id: str, ctx: "PackageContext") -> str:
        """Generate parameter-specific recommendations based on collected values."""
        base = "对缺失参数确认版本适用性；涉及参数变更时先完成容量测算和回滚方案"
        specific: list[str] = []
        v = ctx.variables

        if item_id == "mysql.config.memory":
            flush = str(v.get("innodb_flush_method", "")).strip().lower()
            if flush == "fsync":
                specific.append(
                    "innodb_flush_method=fsync → 建议改为 O_DIRECT：fsync 导致 InnoDB 与 OS 页缓存双重缓冲，浪费内存并降低性能。O_DIRECT 绕过 OS 缓存直写磁盘，变更需重启 MySQL（SAN/NFS 需验证兼容性）"
                )
            elif flush == "fdatasync":
                specific.append(
                    "innodb_flush_method=fdatasync → 仅跳过元数据刷新，仍有双缓冲问题，建议评估切换 O_DIRECT"
                )
            doublewrite = str(v.get("innodb_doublewrite", "")).strip().upper()
            if doublewrite in ("OFF", "0"):
                specific.append(
                    "innodb_doublewrite=OFF → 存在部分��损坏风险（页断裂）；ZFS/BTRFS 可例外，否则建议开启"
                )
            stats_persist = str(v.get("innodb_stats_persistent", "")).strip().upper()
            if stats_persist in ("OFF", "0"):
                specific.append(
                    "innodb_stats_persistent=OFF → 重启后统计信息丢失，可能触发全表扫描，建议开启"
                )
            tmp_size = v.get("tmp_table_size")
            max_heap = v.get("max_heap_table_size")
            if tmp_size is not None and max_heap is not None and str(tmp_size) != str(max_heap):
                specific.append(
                    "tmp_table_size 与 max_heap_table_size 不一致 → MySQL 以较小者为准，可能导致非预期的磁盘临时表，建议设为相同值"
                )
            io_cap = safe_int(v.get("innodb_io_capacity"))
            if io_cap is not None and io_cap <= 200:
                specific.append(
                    f"innodb_io_capacity={io_cap}（默认值）→ 如使用 SSD/云盘建议提升至 2000-4000，"
                    "默认值针对机械盘设计，会限制 InnoDB 后台 IO 吞吐；HDD 环境可维持不变"
                )

        elif item_id == "mysql.config.connection":
            skip_resolve = str(v.get("skip_name_resolve", "")).strip().upper()
            if skip_resolve in ("OFF", "0", ""):
                specific.append(
                    "skip_name_resolve=OFF → DNS 解析开销大，DNS 不可用时连接超时；建议开启并在授权表使用 IP 或 IP 段"
                )
            have_ssl = str(v.get("have_ssl", "")).strip().upper()
            if have_ssl in ("DISABLED", "NO"):
                specific.append(
                    "have_ssl=DISABLED → 建议部署证书启用 SSL 加密传输，满足安全合规基线要求"
                )
            wait_timeout_val = safe_int(v.get("wait_timeout"))
            if wait_timeout_val is not None and wait_timeout_val >= 28800:
                specific.append(
                    f"wait_timeout={wait_timeout_val}（默认 8 小时）→ 空闲连接超时过长，"
                    "长时间未使用的连接占用内存和文件描述符，建议设为 600-1800 秒（10-30 分钟）"
                )

        elif item_id in ("mysql.config.durability", "mysql.config.persistence"):
            flush_log = str(v.get("innodb_flush_log_at_trx_commit", "")).strip()
            sync_binlog = str(v.get("sync_binlog", "")).strip()
            if flush_log != "1" or sync_binlog != "1":
                parts = []
                if flush_log != "1":
                    parts.append("innodb_flush_log_at_trx_commit≠1")
                if sync_binlog != "1":
                    parts.append("sync_binlog≠1")
                specific.append(
                    f"{'/'.join(parts)} → 当前设置牺牲部分持久性换取写入性能；金融、交易类 ACID 严格场景建议均设为 1"
                )
            slow_log = str(v.get("slow_query_log", "")).strip().upper()
            if slow_log in ("OFF", "0"):
                specific.append(
                    "slow_query_log=OFF → 慢查询日志是定位 SQL 性能问题的关键入口，建议开启并设置合理的 long_query_time"
                )
            binlog_format_val = str(v.get("binlog_format", "")).strip().upper()
            if binlog_format_val not in ("ROW",):
                specific.append(
                    f"binlog_format={binlog_format_val} → 生产环境建议 ROW 格式以确保数据一致性和 GTID 正常运作"
                )
            skip_err = (str(v.get("replica_skip_errors", "")).strip() or str(v.get("slave_skip_errors", "")).strip())
            if skip_err and skip_err not in ("OFF", "0", ""):
                specific.append(
                    f"replica_skip_errors={skip_err} → 跳过复制错误会导致主从数据静默不一致，强烈建议设为 OFF"
                )
            expire = v.get("binlog_expire_logs_seconds")
            if expire is not None and int(expire) == 0:
                specific.append(
                    "binlog_expire_logs_seconds=0 → Binlog 永不过期，建议设为 604800（7天）或 1296000（15天）"
                )

        elif item_id == "mysql.config.charset":
            char_server = str(v.get("character_set_server", "")).lower()
            if char_server and not char_server.startswith("utf8"):
                specific.append(
                    f"character_set_server={char_server} → MySQL 8.0 建议 utf8mb4 以支持完整 Unicode 和 emoji"
                )
            tz = str(v.get("time_zone", "")).strip()
            if tz in ("SYSTEM", ""):
                specific.append(
                    "time_zone=SYSTEM → 受 OS 时区影响，跨国部署或容器化环境建议显式设置（如 +08:00）"
                )
            lctn = str(v.get("lower_case_table_names", "")).strip()
            if lctn == "0":
                specific.append(
                    "lower_case_table_names=0 → Linux 区分大小写，跨平台迁移时可能表名找不到，建议评估调整"
                )

        if specific:
            return base + "。" + "。".join(specific) + "。"
        return base + "。"

    @staticmethod
    def _select_rows(
        rows: list[dict[str, Any]],
        columns: Sequence[tuple[str, str]],
        limit: int = 20,
        preserve_empty: Sequence[str] = (),
    ) -> list[dict[str, Any]]:
        """把原始表行投影成报告列。

        同一个中文列名可以配多个来源拼写（复制状态表里 `Replica_IO_Running` 与
        `Slave_IO_Running` 是 MySQL 8.0.22 改名前后的两种写法，只有一种会真的
        出现在表里）。语义是「首个命中优先」：某个拼写已经取到值之后，后面的拼写
        只做兜底，**不得把已找到的值覆盖成空**——否则新名有值、旧名不存在，同一列
        会被回写成 None，报告里就出现一整行"未采集"。
        """
        selected: list[dict[str, Any]] = []
        preserve_set = set(preserve_empty)
        for raw in rows[:limit]:
            lowered = {str(key).lower(): value for key, value in raw.items()}
            item: dict[str, Any] = {}
            for source, label in columns:
                if item.get(label) is not None:
                    continue
                value = raw.get(source)
                if value is None:
                    value = lowered.get(source.lower())
                if label in preserve_set:
                    item[label] = value
                else:
                    item[label] = value if value not in {"", "NULL"} else None
            selected.append(item)
        return selected

    @staticmethod
    def _item(
        item_id: str,
        title: str,
        source: str,
        rows: list[dict[str, Any]],
        conclusion: str,
        *,
        status: str = "normal",
        recommendation: str = "",
        evidence: Sequence[str] = (),
        collection: dict[str, Any] | None = None,
        total_rows: int | None = None,
        display_type: str = "table",
        note: str = "",
    ) -> dict[str, Any]:
        collection = collection or {}
        return {
            "item_id": item_id,
            "title": title,
            "source": source,
            "collection": {
                "status": collection.get("status", "ok" if rows else "empty"),
                "reason": collection.get("reason", ""),
                "row_count": collection.get("row_count", total_rows if total_rows is not None else len(rows)),
            },
            "display": {
                "type": display_type,
                "rows": rows,
                "shown_rows": len(rows),
                "total_rows": total_rows if total_rows is not None else len(rows),
                "note": note,
            },
            "analysis": {
                "status": status,
                "conclusion": conclusion,
                "evidence": list(evidence),
                "recommendation": recommendation,
            },
        }

    def build_inspection_model(
        self, ctx: PackageContext, metrics: dict[str, Any]
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Build the report-ready evidence model while the extracted package is available."""
        self._variables_by_instance[ctx.instance_id] = dict(ctx.variables)
        status = self._status_index(ctx)
        snapshot = ctx.snapshot
        identity = snapshot.get("instance_identity", {})
        host = snapshot.get("host_identity", {})
        time_info = time_evidence(ctx)
        role = snapshot.get("role_evidence", {})
        mysql = metrics.get("mysql_realtime", {})
        sampling = metrics.get("sampling_context", {})
        history_sampling = sampling.get("history") or {}
        if history_sampling.get("usable_for_trend_rules"):
            resource_conclusion = (
                "现场短时窗口内 CPU、IO wait 和内存未显示持续资源瓶颈；"
                "同时已取得可用 SAR 历史，趋势图优先采用历史数据，现场采样仅用于即时确认。"
            )
            resource_recommendation = (
                "继续保留 SAR 或连续监控用于高峰与趋势复核；现场短采样仅作为即时状态证据。"
            )
            resource_evidence = [
                f"实时窗口 {sampling.get('realtime_window_seconds')} 秒",
                (
                    f"SAR 覆盖 {history_sampling.get('coverage_hours')} / "
                    f"{history_sampling.get('requested_hours')} 小时"
                ),
            ]
        else:
            resource_conclusion = (
                "现场短时窗口内 CPU、IO wait 和内存未显示持续资源瓶颈；"
                "有效 SAR 历史不足，本结论不代表全天或业务高峰。"
            )
            resource_recommendation = (
                "优先补充连续监控或新鲜 SAR 历史评价趋势；现场短采样保留用于即时异常确认。"
            )
            resource_evidence = [f"实时窗口 {sampling.get('realtime_window_seconds')} 秒"]

        def collection(item_id: str) -> dict[str, Any]:
            return status.get(item_id, {})

        def value_row(name: str, value: Any, description: str = "") -> dict[str, Any]:
            return {"检查项": name, "采集值": value, "说明": description}

        # 备份：采集端只提供"任务配置可见性"（cron / systemd timer / 备份进程），
        # 现场证据无法证明"最近一次备份成功"或"可恢复"。四态分类与规则
        # MYSQL.BACKUP.TASK_VISIBILITY 共用 metrics.backup_task_visibility ——
        # 尤其是"文件存在但 0 字节"必须落"无法判定"，不能被写成"没有备份任务"。
        backup = backup_task_visibility(ctx)
        if backup["state"] == "active":
            backup_status = "attention"
            backup_conclusion = (
                f"已取得 {backup['active_lines']} 条生效的备份任务配置线索，"
                "但任务配置不等同于最近一次备份成功；"
                "备份可恢复性仍需备份平台结果与恢复演练证据确认。"
            )
            backup_evidence = [
                f"生效的备份任务配置 {backup['active_lines']} 条（cron / systemd timer / 备份进程）"
            ]
        elif backup["state"] == "disabled":
            backup_status = "attention"
            backup_conclusion = (
                "备份任务配置存在但全部处于注释状态，本机没有生效的备份任务；"
                "备份有效性仍需备份平台结果与恢复演练证据确认。"
            )
            backup_evidence = [f"备份任务配置 {backup['commented_lines']} 条，全部被注释"]
        elif backup["state"] == "empty":
            backup_status = "not_evaluated"
            backup_conclusion = (
                "已取得备份任务配置文件但内容为空（0 字节），既无法判定备份任务是否存在，"
                "也不能判定备份有效。"
            )
            backup_evidence = ["备份任务证据文件存在但内容为空；空文件不能推出\"没有备份任务\""]
        else:
            backup_status = "not_evaluated"
            backup_conclusion = "采集包未包含可验证的最近备份成功记录与恢复演练证据，因此不能判定备份有效。"
            backup_evidence = ["未采集到 evidence/backup_*.txt"]

        lscpu = self._colon_key_values(ctx.root / "evidence/lscpu.txt")
        # 磁盘介质（HDD/SSD）：数据目录落在哪块盘上，决定 io_capacity、IO 延迟这类
        # 结论怎么落点——机械盘上 await 偏高是常态，SSD 上同样数字才是真问题。
        # 采集端已用 lsblk 记录 ROTA，但落盘的是空格对齐表格（见
        # system_checks.lsblk_entries）；读不出来就不加这一行，不把"没读到"写成某种介质。
        disk_media_label = disk_media(
            ctx.tables.get("block_devices") or [], data_path=ctx.variables.get("datadir")
        )
        host_rows = [
            value_row("主机名", host.get("hostname"), "数据库所在主机"),
            value_row("主机 IP", host.get("primary_ip"), "采集识别的主机地址"),
            value_row("操作系统", host.get("os"), "操作系统版本"),
            value_row("内核版本", host.get("kernel"), "Linux 内核"),
            value_row("CPU 型号", lscpu.get("Model name"), "处理器型号"),
            value_row("逻辑 CPU", host.get("cpu_count"), "逻辑处理器数量"),
            value_row("CPU Socket", lscpu.get("Socket(s)"), "物理处理器插槽"),
            value_row("每 Socket 核数", lscpu.get("Core(s) per socket"), "物理核心"),
            value_row("NUMA 节点", lscpu.get("NUMA node(s)"), "NUMA 拓扑"),
            value_row("物理内存", self._format_bytes(host.get("memory_total_bytes")), "主机总内存"),
            *([value_row("磁盘介质", disk_media_label, "数据目录所在磁盘的介质类型")] if disk_media_label else []),
            value_row("本地目标", "是" if host.get("database_target_is_local") else "否", "主机指标是否适用于数据库实例"),
        ]
        # 时间同步按节点分档（判据见公共层 ntp_verdict）：
        # `synchronized=no` 且有 RTC 偏移实锤 → risk；`enabled=no` 但已对齐 → attention。
        # 原来只看 `ntp_synchronized` 是否等于 yes，采集侧该字段为空串时三台全判 risk，
        # 把两台时间其实正确的主机也算进风险。
        ntp = ntp_verdict(time_info)
        ntp_tier = ntp["tier"]
        offset_seconds = ntp_clock_offset_seconds(time_info)
        time_rows = [
            value_row("本地时间", self._format_host_time(time_info.get("host_local_time")), "采集时主机时间"),
            value_row("时区", time_info.get("time_zone") or time_info.get("timezone"), "主机时区"),
            value_row("NTP 同步", time_info.get("ntp_synchronized"), "systemd-timedatectl 的 NTP synchronized 读数"),
            value_row("NTP 已启用", time_info.get("ntp_enabled"), "是否配置了持续同步源（NTP enabled）"),
            value_row("RTC 时间", time_info.get("rtc_time"), "硬件时钟读数"),
            value_row(
                "RTC 偏移",
                None if offset_seconds is None else f"{offset_seconds:.0f} 秒",
                "RTC 与参考时钟的绝对偏差；超过 10 秒判风险",
            ),
        ]
        time_recommendation = {
            "risk": "启用并验证企业时间同步服务（chrony / ntpd），统一数据库节点时区。",
            "attention": "确认该主机的时间对齐方式（虚拟化平台校时或手工校时），必要时启用持续同步服务。",
            "unknown": "本次未采集到 timedatectl 证据，请补充采集后再判定时间同步。",
        }.get(ntp_tier, "")
        capacity = metrics.get("capacity", {})
        # 指标层已按公共判据过滤过一次；这里再筛一次，是为了让"旧分析产物"
        # （analysis.json 里仍带着全量挂载点）重新生成报告时同样干净。
        # 两处调用的是同一个判据，因此不会出现"数字剔了、表格没剔"的口径分叉。
        fs_rows = [
            {
                "文件系统": row.get("filesystem"),
                "类型": row.get("type"),
                "挂载点": row.get("mountpoint"),
                "使用率": f"{row['usage_percent']:.1f}%" if row.get("usage_percent") is not None else None,
                "可用空间": self._format_bytes((row.get("available_kb") or 0) * 1024),
            }
            for row in capacity.get("filesystems", [])
            if is_persistent_fstype(row.get("type"))
        ]
        excluded_fs_count = len(capacity.get("excluded_filesystems") or [])
        if not excluded_fs_count:
            # 旧产物没有 excluded_filesystems 键，退回按行数差反推剔除数量。
            excluded_fs_count = max(len(capacity.get("filesystems", [])) - len(fs_rows), 0)
        kernel_rows = self._raw_key_value_rows(ctx.root / "tables/kernel_parameters.tsv")
        mount_rows = self._mount_rows(ctx.root / "evidence/mounts.txt")
        dmesg_rows = self._dmesg_error_rows(ctx.root / "evidence/dmesg_errors.txt")
        realtime = metrics.get("system_realtime", {})
        resource_rows = [
            {
                "指标": "CPU 使用率",
                "平均值": f"{realtime.get('cpu_busy_percent', {}).get('average')}%",
                "峰值": f"{realtime.get('cpu_busy_percent', {}).get('max')}%",
                "说明": "现场短时采样",
            },
            {
                "指标": "CPU IO wait",
                "平均值": f"{realtime.get('cpu_iowait_percent', {}).get('average')}%",
                "峰值": f"{realtime.get('cpu_iowait_percent', {}).get('max')}%",
                "说明": "现场短时采样",
            },
            {
                "指标": "内存使用率",
                "平均值": f"{realtime.get('memory_used_percent', {}).get('average')}%",
                "峰值": f"{realtime.get('memory_used_percent', {}).get('max')}%",
                "说明": "现场短时采样",
            },
            {
                "指标": "可用内存",
                "平均值": self._format_bytes(realtime.get("memory_available_bytes", {}).get("average")),
                "峰值": self._format_bytes(realtime.get("memory_available_bytes", {}).get("min")),
                "说明": "峰值列表示窗口内最低可用内存",
            },
            {
                "指标": "已用 Swap",
                "平均值": self._format_bytes(realtime.get("swap_used_bytes", {}).get("average")),
                "峰值": self._format_bytes(realtime.get("swap_used_bytes", {}).get("max")),
                "说明": "现场短时采样",
            },
        ]
        disk_rows: list[dict[str, Any]] = []
        for device, device_metrics in realtime.get("disk_devices", {}).items():
            disk_rows.append({
                "设备": device,
                "平均 util": f"{device_metrics.get('util', {}).get('average')}%",
                "峰值 util": f"{device_metrics.get('util', {}).get('max')}%",
                "平均读 await": f"{device_metrics.get('read_await', {}).get('average')} ms",
                "平均写 await": f"{device_metrics.get('write_await', {}).get('average')} ms",
            })
        network_rows: list[dict[str, Any]] = []
        for interface, interface_metrics in realtime.get("network_interfaces", {}).items():
            if interface == "lo":
                continue
            network_rows.append({
                "网卡": interface,
                "平均接收": self._format_bytes(interface_metrics.get("rx_bps", {}).get("average")) + "/s"
                if interface_metrics.get("rx_bps", {}).get("average") is not None else None,
                "峰值接收": self._format_bytes(interface_metrics.get("rx_bps", {}).get("max")) + "/s"
                if interface_metrics.get("rx_bps", {}).get("max") is not None else None,
                "平均发送": self._format_bytes(interface_metrics.get("tx_bps", {}).get("average")) + "/s"
                if interface_metrics.get("tx_bps", {}).get("average") is not None else None,
                "峰值发送": self._format_bytes(interface_metrics.get("tx_bps", {}).get("max")) + "/s"
                if interface_metrics.get("tx_bps", {}).get("max") is not None else None,
            })

        basic_rows = [
            value_row("数据库类型", identity.get("database_type"), "数据库产品"),
            value_row("数据库版本", identity.get("version"), "服务端版本"),
            value_row("实例标识", identity.get("instance_tag"), "主机、IP 与端口组合"),
            value_row("监听地址", identity.get("bind_address"), "bind_address"),
            value_row("端口", identity.get("port"), "服务端口"),
            value_row("Server UUID", identity.get("server_uuid"), "实例唯一标识"),
            value_row("Server ID", identity.get("server_id"), "复制标识"),
            value_row("观测角色", role.get("role_observed"), "依据只读、复制和集群证据推断"),
        ]
        schemas = self._select_rows(
            ctx.tables.get("schemas", []),
            [
                ("SCHEMA_NAME", "Schema"),
                ("DEFAULT_CHARACTER_SET_NAME", "默认字符集"),
                ("DEFAULT_COLLATION_NAME", "默认排序规则"),
            ],
        )
        business_schemas = business_schema_names(ctx.tables.get("schemas", []))
        business_schema_text = (
            "、".join(business_schemas[:6]) + (" 等" if len(business_schemas) > 6 else "")
            if business_schemas else ""
        )
        engines = self._select_rows(
            [row for row in ctx.tables.get("engines", []) if str(row.get("Support", "")).upper() in {"YES", "DEFAULT"}],
            [("Engine", "存储引擎"), ("Support", "支持状态"), ("Transactions", "事务"), ("XA", "XA"), ("Savepoints", "保存点")],
        )
        plugins = self._select_rows(
            [row for row in ctx.tables.get("plugins", []) if str(row.get("Status", "")).upper() == "ACTIVE"],
            [("Name", "插件"), ("Type", "类型"), ("Status", "状态"), ("License", "许可")],
            30,
        )

        # 参数清单与跨节点对比共用 CONFIG_GROUP_VARIABLES，两者不可能错位。
        variable_groups = [
            (item_id, title, list(CONFIG_GROUP_VARIABLES[item_id]))
            for item_id, title in CONFIG_GROUP_TITLES
        ]
        config_items: list[dict[str, Any]] = []
        config_issues = 0
        config_issue_summaries: list[str] = []
        for item_id, title, variables in variable_groups:
            rows = [
                {"参数": name, "采集值": ctx.variables.get(name), "说明": label}
                for name, label in variables
            ]
            available = sum(1 for row in rows if row["采集值"] is not None)
            recommendation = self._config_recommendation(item_id, ctx)
            # Determine if there are specific parameter findings (not just the generic base advice)
            has_specific = bool(recommendation.split("。")[1].strip()) if recommendation.startswith("对缺失参数") and recommendation.count("。") >= 2 else False
            if has_specific:
                config_issues += 1
                # Extract first specific finding for summary (between first and second 。)
                parts = recommendation.split("。")
                if len(parts) >= 2:
                    config_issue_summaries.append(parts[1].strip())
            param_status = "attention" if has_specific else ("normal" if available == len(rows) else "attention")
            conclusion = f"已展示 {available} 项核心运行参数"
            if has_specific:
                conclusion += "；部分参数值偏离建议最佳实践，详见下方建议"
            else:
                conclusion += "；参数适配性需结合内存、并发、数据规模和持久性目标判断"
            conclusion += "。"
            config_items.append(self._item(
                item_id, title, "tables/global_variables.tsv", rows,
                conclusion,
                status=param_status,
                recommendation=recommendation,
                evidence=[f"可用参数 {available}/{len(rows)} 项"],
                collection=collection("mysql.global_variables"),
            ))
        mycnf_rows = self._select_rows(
            ctx.tables.get("mycnf_allowlist", []),
            [("parameter", "配置项"), ("configured_value", "配置文件值")],
            35,
        )
        # 配置文件值 vs 运行值（B11）：采集包里两张表都在，但原先只在同一章里各展一张表，
        # 读者要自己对。这里把不一致项直接写进结论并点名参数与两侧取值。
        if "mycnf_allowlist" not in ctx.tables:
            config_file_status = "not_evaluated"
            config_file_conclusion = "未采集到配置文件参数，无法与运行值核对。"
        else:
            drift = config_runtime_drift(ctx)
            if drift:
                drift_brief = "；".join(
                    "%s（配置文件 %s，运行值 %s）"
                    % (item["parameter"], "、".join(item["configured_raw"]), item["runtime"])
                    for item in drift
                )
                config_file_status = "attention"
                config_file_conclusion = (
                    f"已采集 {len(ctx.tables.get('mycnf_allowlist', []))} 项白名单参数；"
                    f"其中 {len(drift)} 处与当前运行值不一致：{drift_brief}。"
                    "这类不一致说明配置改过之后没有重启或没有生效，重启会让线上行为回退到配置文件版本。"
                )
            else:
                config_file_status = "normal"
                config_file_conclusion = (
                    f"已采集 {len(ctx.tables.get('mycnf_allowlist', []))} 项白名单参数，"
                    "已采集到的参数与当前运行值一致。"
                )
        config_items.append(self._item(
            "mysql.config.file",
            "配置文件白名单参数",
            "tables/mycnf_allowlist.tsv",
            mycnf_rows,
            config_file_conclusion,
            status=config_file_status,
            recommendation=(
                "逐项确认哪一侧才是期望值：保留运行值就把配置文件同步过去，采用配置文件值则安排重启窗口。"
                if config_file_status == "attention" else
                "对关键参数同时核对配置文件值和运行值，避免重启后参数回退。"
            ),
            collection=collection("system.mycnf_allowlist"),
            total_rows=len(ctx.tables.get("mycnf_allowlist", [])),
            note="最多展示 35 行；不包含密码及完整配置文件。",
        ))

        runtime_rows = [
            {"指标": "QPS", "平均值": mysql.get("qps", {}).get("average"), "峰值": mysql.get("qps", {}).get("max"), "说明": "每秒查询数"},
            {"指标": "TPS", "平均值": mysql.get("tps", {}).get("average"), "峰值": mysql.get("tps", {}).get("max"), "说明": "每秒事务数"},
            {"指标": "Threads_connected", "平均值": mysql.get("threads_connected", {}).get("average"), "峰值": mysql.get("threads_connected", {}).get("max"), "说明": "已连接线程"},
            {"指标": "Threads_running", "平均值": mysql.get("threads_running", {}).get("average"), "峰值": mysql.get("threads_running", {}).get("max"), "说明": "运行中线程"},
            {"指标": "临时表落盘比例", "平均值": f"{(mysql.get('tmp_disk_ratio') or 0) * 100:.2f}%" if mysql.get("tmp_disk_ratio") is not None else None, "峰值": "", "说明": "采样窗口计数器增量"},
            {"指标": "Buffer Pool 读未命中", "平均值": f"{(mysql.get('buffer_pool_read_miss_ratio') or 0) * 100:.4f}%" if mysql.get("buffer_pool_read_miss_ratio") is not None else None, "峰值": "", "说明": "采样窗口计数器增量"},
            {"指标": "表缓存未命中比例", "平均值": f"{(mysql.get('table_open_cache_miss_ratio') or 0) * 100:.2f}%" if mysql.get("table_open_cache_miss_ratio") is not None else None, "峰值": "", "说明": "采样窗口计数器增量"},
        ]
        runtime_evidence = [
            f"采样窗口 {sampling.get('realtime_window_seconds')} 秒",
            f"MySQL 样本点 {sampling.get('mysql_sample_points')} 个",
        ]
        process_rows = self._select_rows(
            ctx.tables.get("processlist", []),
            [("ID", "会话 ID"), ("USER", "用户"), ("HOST", "来源"), ("DB", "Schema"), ("COMMAND", "命令"), ("TIME", "持续秒数"), ("STATE", "状态"), ("SQL_TEXT", "SQL 摘要"), ("SQL_SHA256", "SQL 摘要")],
            20,
        )
        lock_rows = [
            {"检查项": "长事务", "记录数": len(ctx.tables.get("long_transactions", [])), "证据文件": "tables/long_transactions.tsv"},
            {"检查项": "数据锁等待", "记录数": len(ctx.tables.get("data_lock_waits", [])), "证据文件": "tables/data_lock_waits.tsv"},
            {"检查项": "待授予元数据锁", "记录数": len(ctx.tables.get("metadata_locks_pending", [])), "证据文件": "tables/metadata_locks_pending.tsv"},
        ]
        # 结论与证据必须和本页表格、风险台账同源。这里曾把结论和证据写死成
        # 「未发现长事务…」「长事务 0 条」，而同一章表格里长事务是 270 条、
        # MYSQL.TRANSACTION.LONG_RUNNING 也据此报了 high —— 报告自相矛盾，
        # 客户对着表格就能看出结论在撒谎。
        lock_counts = {str(row["检查项"]): int(row.get("记录数") or 0) for row in lock_rows}
        lock_total = sum(lock_counts.values())
        longest_lock_seconds = (metrics.get("activity") or {}).get("max_long_transaction_seconds")
        lock_status = "risk" if lock_total else "normal"
        lock_conclusion = (
            "采集时未发现长事务、数据锁等待或待授予元数据锁；该结论仅代表采集时点。"
            if not lock_total else
            "采集时发现 " + "、".join(
                f"{name} {count} 条" for name, count in lock_counts.items() if count
            ) + "；长事务会放大锁等待、阻塞链和 Undo 膨胀，需先定位会话与业务调用链再处置。"
        )
        lock_evidence = [f"{name} {count} 条" for name, count in lock_counts.items()]
        if lock_total and longest_lock_seconds is not None:
            lock_evidence.append(f"最长事务 {float(longest_lock_seconds):.0f} 秒")
        lock_recommendation = (
            "定位会话和业务调用链，确认长事务来源后尽快提交或回滚；"
            "处置前先确认对业务的影响，避免直接盲目终止会话。"
            if lock_total else ""
        )

        db_sizes = self._select_rows(
            ctx.tables.get("database_sizes", []),
            [("database_name", "Schema"), ("total_mb", "总 MB"), ("table_count", "表数量")],
            20,
        )
        # 采集侧 object_counts.tsv 的 `no_engine_objects` 是"没有存储引擎的对象"，
        # 在 MySQL 里就等于视图（information_schema.TABLES 对视图不填 ENGINE），
        # 实测三包逐个 Schema 都恰等于 views。直接把它当"非 InnoDB 表"展示会与
        # non_innodb_tables.tsv（实测每节点 1 张 MEMORY 表）自相矛盾，因此改用后者
        # 按 Schema 真实计数，标签也改回"非 InnoDB 表"。
        non_innodb_by_schema: dict[str, int] = {}
        for row in ctx.tables.get("non_innodb_tables", []):
            key = row.get("TABLE_SCHEMA") or row.get("table_schema")
            if key:
                non_innodb_by_schema[str(key)] = non_innodb_by_schema.get(str(key), 0) + 1
        object_rows: list[dict] = []
        for row in ctx.tables.get("object_counts", []):
            merged = dict(row)
            merged["non_innodb_tables"] = non_innodb_by_schema.get(str(row.get("table_schema") or ""), 0)
            object_rows.append(merged)
        object_counts = self._select_rows(
            object_rows,
            [("table_schema", "Schema"), ("base_tables", "表"), ("views", "视图"), ("innodb_tables", "InnoDB 表"), ("non_innodb_tables", "非 InnoDB 表")],
            20,
        )
        schema_metrics = metrics.get("schema", {})
        # 自增容量与碎片**不进候选项合计**：采集端给的是"按某列倒序的前 N 条明细"，
        # 把条数当风险数会把 100 条明细算成 100 个风险对象（实测自增最高使用率仅
        # 22.79%、碎片明细 TOP 全是"分配 0.02 MB"的极小表）。它们的判定在规则层按
        # 外部化阈值完成；这里只如实展示明细条数与可核对的极值。
        object_risk_rows = [
            {"检查项": "无主键表", "数量": schema_metrics.get("tables_without_primary_key"), "证据文件": "tables/no_primary_key_summary.tsv"},
            {"检查项": "非 InnoDB 表", "数量": schema_metrics.get("non_innodb_table_count"), "证据文件": "tables/non_innodb_tables.tsv"},
            {"检查项": "冗余索引候选", "数量": schema_metrics.get("redundant_index_count"), "证据文件": "tables/redundant_indexes.tsv"},
            {"检查项": "未使用索引候选", "数量": schema_metrics.get("unused_index_candidate_count"), "证据文件": "tables/unused_indexes.tsv"},
        ]
        object_risk_total = sum(int(row.get("数量") or 0) for row in object_risk_rows)
        object_risk_detail = "、".join(
            f"{row['检查项']} {int(row.get('数量') or 0)}" for row in object_risk_rows
        )
        detail_notes: list[str] = []
        auto_detail = schema_metrics.get("auto_increment_collected_count")
        if auto_detail is not None:
            top_pct = schema_metrics.get("auto_increment_max_used_pct")
            detail_notes.append(
                f"自增容量明细 {auto_detail} 条（采集端按使用率倒序，属明细条数而非风险数）"
                + (f"，最高使用率 {top_pct:.2f}%" if top_pct is not None else "")
            )
        frag_detail = schema_metrics.get("fragmentation_collected_count")
        if frag_detail is not None:
            top_free = schema_metrics.get("fragmentation_max_data_free_mb")
            detail_notes.append(
                f"碎片明细 {frag_detail} 条（采集端按碎片率倒序，本表按可回收空间重排）"
                + (f"，最大可回收 {top_free:,.2f} MB" if top_free is not None else "")
            )
        detail_note = ("；".join(detail_notes) + "。") if detail_notes else ""
        if business_schemas:
            capacity_risk_status = "attention" if object_risk_total else "normal"
            capacity_risk_conclusion = (
                (
                    f"业务 Schema {len(business_schemas)} 个；对象检查候选项合计 {object_risk_total} 项"
                    f"（{object_risk_detail}），需结合业务确认后纳入整改。"
                )
                if object_risk_total else
                f"业务 Schema {len(business_schemas)} 个；无主键、非 InnoDB 与索引类检查均未发现候选项。"
            ) + detail_note
        else:
            capacity_risk_status = "not_applicable"
            capacity_risk_conclusion = "本实例未发现业务 Schema；无主键、非 InnoDB、碎片和自增容量检查没有业务对象可评价。"

        digest_source = ctx.tables.get("sql_digests_top", [])
        has_sql_text = digest_source and any(
            str(r.get("digest_text", "")).strip() for r in digest_source[:1]
        )
        digest_col = ("digest_text", "SQL 摘要") if has_sql_text else ("DIGEST", "SQL 摘要")
        digest_rows = self._select_rows(
            digest_source,
            [
                ("schema_name", "Schema"), digest_col,
                ("COUNT_STAR", "执行次数"), ("total_seconds", "总耗时秒"),
                ("avg_seconds", "平均秒"), ("SUM_ROWS_EXAMINED", "扫描行数"),
                ("SUM_ROWS_SENT", "返回行数"), ("SUM_NO_INDEX_USED", "未用索引次数"),
            ],
            15,
            preserve_empty=["Schema"],
        )
        digest_note = "已采集 SQL 正文；按总耗时排序，最多展示 15 行。" if has_sql_text else "SQL 正文未采���；按总耗时排序，最多展示 15 行。"
        no_index_exec = sum(
            int(self._row_number(row, "SUM_NO_INDEX_USED") or 0) for row in digest_source
        )

        wait_source = ctx.tables.get("wait_events_top", [])
        wait_rows = self._select_rows(
            wait_source,
            [("EVENT_NAME", "等待事件"), ("COUNT_STAR", "次数"), ("total_wait_seconds", "总等待秒"), ("avg_wait_seconds", "平均等待秒")],
            15,
        )
        file_io_source = ctx.tables.get("file_io_top", [])
        file_io_rows = self._select_rows(
            file_io_source,
            [
                ("FILE_NAME", "文件"), ("EVENT_NAME", "事件"),
                ("COUNT_READ", "读次数"), ("COUNT_WRITE", "写次数"),
                ("SUM_NUMBER_OF_BYTES_READ", "读取字节"), ("SUM_NUMBER_OF_BYTES_WRITE", "写入字节"),
                ("total_wait_seconds", "总等待秒"),
            ],
            15,
        )

        accounts_source = ctx.tables.get("accounts", [])
        accounts = self._select_rows(
            accounts_source,
            [
                ("user", "用户"), ("host", "来源"), ("plugin", "认证插件"),
                ("account_locked", "锁定"), ("password_expired", "密码过期"),
                ("password_lifetime", "密码有效期"), ("Super_priv", "SUPER"),
                ("Grant_priv", "GRANT"), ("Create_user_priv", "CREATE USER"),
            ],
            50,
            preserve_empty=["密码有效期"],
        )
        remote_roots = [
            row for row in accounts_source
            if str(row.get("user", "")).lower() == "root"
            and row.get("host") not in {"localhost", "127.0.0.1", "::1"}
        ]
        privilege_source = ctx.tables.get("user_privileges", [])
        privilege_summary: dict[str, dict[str, Any]] = {}
        high_privileges = {"SUPER", "SYSTEM_USER", "FILE", "SHUTDOWN", "CREATE USER", "GRANT OPTION"}
        for row in privilege_source:
            grantee = str(row.get("GRANTEE", ""))
            privilege = str(row.get("PRIVILEGE_TYPE", ""))
            summary = privilege_summary.setdefault(
                grantee, {"授权主体": grantee, "权限数量": 0, "高权限": []}
            )
            summary["权限数量"] += 1
            if privilege.upper() in high_privileges:
                summary["高权限"].append(privilege)
        privilege_rows = [
            {
                "授权主体": value["授权主体"],
                "权限数量": value["权限数量"],
                "高权限": "、".join(sorted(set(value["高权限"]))) or "",
            }
            for value in privilege_summary.values()
        ]

        log_rows = self._select_rows(
            ctx.tables.get("log_files", []),
            [("log_type", "日志类型"), ("path", "路径"), ("exists", "存在"), ("readable", "可读"), ("size_bytes", "大小字节"), ("modified_at", "修改时间")],
            20,
        )
        # 日志轮转（A6）：表里原先只有"大小字节"，读者得自己换算，也看不出该不该处理。
        # 这里把原始字节换成人类可读大小（列序不变），并把判定写进结论
        # （超阈值文件 + 折合日增速率）——绝对值只说明"大"，日增速率才说明"该不该马上处理"。
        log_rows = [
            {
                "日志类型": row.get("日志类型"),
                "路径": row.get("路径"),
                "存在": row.get("存在"),
                "可读": row.get("可读"),
                "大小": self._format_bytes(row.get("大小字节")),
                "修改时间": self._format_host_time(row.get("修改时间")),
            }
            for row in log_rows
        ]
        log_entries = log_file_entries(ctx)
        biggest_log = max(log_entries, key=lambda item: item["size_bytes"], default=None)
        log_uptime = mysql_uptime_seconds(ctx)
        if biggest_log:
            over_logs = [item for item in log_entries if item["size_bytes"] > 1024 ** 3]
            log_rotation_status = "attention" if over_logs else "normal"
            log_rotation_note = (
                f"最大日志文件为 {biggest_log['log_type']} "
                f"{self._format_bytes(biggest_log['size_bytes'])}（{biggest_log['path']}）"
            )
            if log_uptime and log_uptime > 0:
                log_rotation_note += (
                    f"，折合日增约 {self._format_bytes(biggest_log['size_bytes'] / (log_uptime / 86400.0))}/天"
                    f"（实例已运行约 {log_uptime / 86400:.0f} 天）"
                )
            log_rotation_note += (
                "；已超过 1 GiB 提醒档："
                + "、".join(f"{i['log_type']} {self._format_bytes(i['size_bytes'])}" for i in over_logs)
                + "，需确认 logrotate 是否真的在执行"
                if over_logs else
                "；全部日志文件均未超过 1 GiB 提醒档"
            )
            log_rotation_note += "。"
        else:
            log_rotation_status = "not_evaluated"
            log_rotation_note = "未采集到日志文件大小，无法判断轮转是否正常。"
        error_rows = self._select_rows(
            ctx.tables.get("error_log_summary", []),
            [("PRIO", "级别"), ("ERROR_CODE", "错误代码"), ("occurrence_count", "次数"), ("first_seen", "首次出现"), ("last_seen", "最后出现")],
            30,
        )
        binary_status = self._select_rows(
            ctx.tables.get("binary_log_status", []),
            [("File", "当前 Binlog"), ("Position", "位置"), ("Executed_Gtid_Set", "已执行 GTID 集")],
            5,
        )
        # Binlog 数量与容量必须从**全量行**算：表格只投影前 20 条用于展示，
        # 用 len(投影) 当文件数会把 74 / 129 / 131 个文件写成"已配置 20 个"。
        binary_log_source = ctx.tables.get("binary_logs", [])
        binary_logs = [
            {
                "Binlog 文件": row.get("Log_name"),
                "大小": self._format_bytes(row.get("File_size")),
                "加密": row.get("Encrypted"),
            }
            for row in binary_log_source[:20]
        ]
        # 口径统一：文件数 / 合计容量 / 未加密数都走 metrics.binlog_totals，
        # 规则层（MYSQL.SECURITY.BINLOG_UNENCRYPTED、MYSQL.CAPACITY.BINLOG_SIZE）
        # 用的是同一个函数，避免"表格说 74 个、规则说 20 个"这种两处口径。
        _binlog = binlog_totals(ctx)
        binary_log_count = _binlog["count"]
        binary_log_bytes = _binlog["bytes"]
        binary_log_unencrypted = _binlog["unencrypted"]
        replica_rows = self._select_rows(
            ctx.tables.get("replica_status", []),
            [
                ("Channel_Name", "通道"), ("Channel_name", "通道"),
                ("Source_Host", "源主机"), ("Master_Host", "源主机"),
                ("Replica_IO_Running", "IO 线程"), ("Slave_IO_Running", "IO 线程"),
                ("Replica_SQL_Running", "SQL 线程"), ("Slave_SQL_Running", "SQL 线程"),
                ("Seconds_Behind_Source", "延迟秒"), ("Seconds_Behind_Master", "延迟秒"),
                ("Auto_Position", "自动定位"),
            ],
            10,
        )
        # 复制结论同样由采集到的行算出来。指向自身的行（Source_UUID 为空、
        # Source_Host 指向本机）是残留通道，既不算"副本在跑"也不算"复制异常"——
        # 判据与拓扑层、规则层共用 plugins.mysql.metrics，避免同一份数据三处口径。
        replica_source = ctx.tables.get("replica_status", [])
        real_replica_rows, residual_replica_rows = split_self_referencing_replica_rows(
            replica_source, local_host_names(ctx)
        )
        replica_states = [replica_threads_running(row) for row in real_replica_rows]
        stopped_channels = sum(1 for io_ok, sql_ok in replica_states if io_ok is False or sql_ok is False)
        residual_note = (
            f"另有 {len(residual_replica_rows)} 条指向自身的残留复制通道，不构成真实主从关系。"
            if residual_replica_rows else ""
        )
        if stopped_channels:
            replica_status = "risk"
            replica_conclusion = (
                f"已配置 {len(real_replica_rows)} 条复制通道，其中 {stopped_channels} 条 IO/SQL 线程未运行；"
                "复制中断会直接削弱高可用与数据保护能力。" + residual_note
            )
            replica_recommendation = "检查复制错误、网络和源端状态，制定可回滚的恢复步骤。"
            replica_evidence = [
                f"复制通道 {len(real_replica_rows)} 条",
                f"线程未运行 {stopped_channels} 条",
            ]
        elif real_replica_rows:
            replica_status = "normal"
            replica_conclusion = (
                f"已配置 {len(real_replica_rows)} 条复制通道，IO/SQL 线程均运行中。" + residual_note
            )
            replica_recommendation = "持续监控复制延迟和错误日志；变更前确认切换机制与演练记录。"
            replica_evidence = [f"复制通道 {len(real_replica_rows)} 条，线程运行中"]
        elif residual_replica_rows:
            replica_status = "attention"
            replica_conclusion = (
                f"存在 {len(residual_replica_rows)} 条指向自身的复制状态行"
                "（Source_UUID 为空、Source_Host 指向本机），属残留或未启动的通道，"
                "不构成真实主从关系；本实例按复制源端处理。"
            )
            replica_recommendation = (
                "确认该通道为历史配置遗留后清理（RESET REPLICA ALL）或补全上游信息；"
                "清理前确认不影响现有下游复制链路。"
            )
            replica_evidence = [f"指向自身的残留复制通道 {len(residual_replica_rows)} 条"]
        else:
            replica_status = "attention"
            replica_conclusion = "未发现下游复制、Group Replication 或 Galera 运行证据；当前按单实例或复制源端处理。"
            replica_recommendation = "如业务要求高可用，应补充架构设计、下游节点采集包、切换机制和演练记录。"
            replica_evidence = []

        plugin_source = ctx.tables.get("plugins", [])
        active_plugin_count = sum(
            1 for row in plugin_source if str(row.get("Status", "")).upper() == "ACTIVE"
        )

        sections = [
            {
                "section_id": "system_environment",
                "title": "系统与环境检查",
                "items": [
                    self._item("system.host", "主机与操作系统信息", "snapshot.json#host_identity", host_rows,
                               "已取得数据库主机、操作系统、CPU 和内存基本信息；本实例为本地采集，系统指标可用于关联分析。",
                               evidence=[f"CPU {host.get('cpu_count')} Core", f"内存 {self._format_bytes(host.get('memory_total_bytes'))}"]),
                    self._item("system.time", "时间与时区", "snapshot.json#time_evidence", time_rows,
                               ntp["reason"] + "。" if not ntp["reason"].endswith("。") else ntp["reason"],
                               status={"risk": "risk", "attention": "attention", "unknown": "not_evaluated"}.get(
                                   ntp_tier, "normal"
                               ),
                               recommendation=time_recommendation,
                               evidence=list(ntp["facts"]),
                               collection=collection("system.time_status")),
                    self._item("system.filesystems", "文件系统容量", "tables/filesystems.tsv", fs_rows,
                               "已取得主要文件系统容量；个别失效挂载点读取失败，不影响已展示挂载点。" if collection("system.filesystems").get("status") == "partial" else "已取得文件系统容量信息。",
                               status="attention" if collection("system.filesystems").get("status") == "partial" else "normal",
                               recommendation="清理或卸载失效挂载点，并确认 MySQL 数据目录所在文件系统的容量告警。",
                               note=(f"已排除 {excluded_fs_count} 个 tmpfs/devtmpfs/光驱等非持久化挂载点") if excluded_fs_count else "",
                               collection=collection("system.filesystems"), total_rows=len(fs_rows)),
                    self._item("system.kernel", "关键内核参数", "tables/kernel_parameters.tsv", kernel_rows,
                               "已取得数据库相关内核参数；参数值需结合数据库内存预算和操作系统基线复核。",
                               recommendation=self._kernel_recommendation(ctx),
                               evidence=[f"已采集内核参数 {len(kernel_rows)} 项"],
                               collection=collection("system.sysctl_selected"), total_rows=len(kernel_rows)),
                    self._item("system.mounts", "挂载参数", "evidence/mounts.txt", mount_rows,
                               "已取得挂载参数，可用于检查数据库数据目录所在文件系统的持久性选项。" if mount_rows else "未采集到挂载参数。",
                               evidence=[f"持久化挂载点 {len(mount_rows)} 项：" + "、".join(str(row.get("挂载点", "")) for row in mount_rows)] if mount_rows else [],
                               note="已排除 proc/tmpfs/大页内存/光驱等虚拟或只读挂载点",
                               collection=collection("system.mounts"), total_rows=len(mount_rows)),
                    self._item("system.dmesg_errors", "内核错误摘要", "evidence/dmesg_errors.txt", dmesg_rows,
                               "存在内核错误级别日志，建议结合硬件与系统日志复核。" if dmesg_rows else "未发现内核错误级别日志。",
                               status="attention" if dmesg_rows else "normal",
                               evidence=([f"内核错误日志 {len(dmesg_rows)} 条"] + [str(row.get("内核错误日志", ""))[:80] for row in dmesg_rows[:3]]) if dmesg_rows else [],
                               collection=collection("system.dmesg_errors"), total_rows=len(dmesg_rows)),
                ],
            },
            {
                "section_id": "mysql_instance",
                "title": "MySQL 实例与组件检查",
                "items": [
                    self._item("mysql.basic", "实例基本信息", "snapshot.json#instance_identity", basic_rows,
                               f"实例版本为 MySQL {identity.get('version')}，当前观测角色为 {role.get('role_observed')}。",
                               evidence=[f"{identity.get('mysql_hostname')}:{identity.get('port')}", f"Server UUID={identity.get('server_uuid')}"]),
                    self._item("mysql.schemas", "Schema 与默认字符集", "tables/schemas.tsv", schemas,
                               (
                                   f"共 {len(schemas)} 个 Schema，其中业务 Schema {len(business_schemas)} 个"
                                   f"（{business_schema_text}），字符集与排序规则见下表。"
                               ) if business_schemas else (
                                   f"共 {len(schemas)} 个 Schema，均为系统 Schema，未发现业务 Schema；"
                                   "因此容量、对象结构等检查没有业务对象可评价。"
                               ),
                               status="normal" if business_schemas else "not_applicable",
                               evidence=[f"Schema 数量 {len(schemas)}", f"业务 Schema {len(business_schemas)} 个"],
                               collection=collection("mysql.schemas"), total_rows=len(ctx.tables.get("schemas", []))),
                    self._item("mysql.engines", "可用存储引擎", "tables/engines.tsv", engines,
                               "InnoDB 等受支持存储引擎已加载；业务表引擎合规性见「容量与对象检查」。" if business_schemas
                               else "InnoDB 等受支持存储引擎已加载；未发现业务 Schema，业务表引擎合规性不适用。",
                               evidence=[f"已加载存储引擎 {len(ctx.tables.get('engines', []))} 个"],
                               collection=collection("mysql.engines"), total_rows=len(ctx.tables.get("engines", []))),
                    self._item("mysql.plugins", "活动插件", "tables/plugins.tsv", plugins,
                               f"已识别 {active_plugin_count} 个活动插件，报告仅展示活动组件。",
                               evidence=[f"活动插件 {active_plugin_count} 个", f"插件记录共 {len(plugin_source)} 条"],
                               collection=collection("mysql.plugins"), total_rows=len(plugin_source),
                               note="仅展示 ACTIVE 插件，最多 30 行。"),
                ],
            },
            {
                "section_id": "system_performance",
                "title": "操作系统性能检查",
                "items": [
                    self._item(
                        "system.performance.resources",
                        "CPU 与内存现场指标",
                        "timeseries/system_cpu.csv; timeseries/system_memory.csv",
                        resource_rows,
                        resource_conclusion,
                        status="attention",
                        recommendation=resource_recommendation,
                        evidence=resource_evidence,
                        collection=collection("timeseries.realtime_sampling"),
                    ),
                    self._item(
                        "system.performance.disk",
                        "磁盘现场指标",
                        "timeseries/system_disk.csv",
                        disk_rows,
                        "现场短时窗口内磁盘 util 和 await 整体较低，未发现持续 I/O 饱和；需结合数据目录映射与高峰历史复核。",
                        status="attention",
                        evidence=[f"设备数量 {len(disk_rows)}"],
                        collection=collection("timeseries.realtime_sampling"),
                    ),
                    self._item(
                        "system.performance.network",
                        "网络现场指标",
                        "timeseries/system_network.csv",
                        network_rows,
                        "现场短时窗口内网络吞吐较低，未发现明显带宽压力；该结论不评价丢包、重传和链路质量。",
                        status="attention",
                        recommendation="如需网络质量结论，应补充重传、错误包、丢包及交换机侧监控。",
                        collection=collection("timeseries.realtime_sampling"),
                    ),
                ],
            },
            {
                "section_id": "mysql_configuration",
                "title": "MySQL 参数配置检查",
                "items": config_items,
            },
            {
                "section_id": "mysql_runtime",
                "title": "MySQL 运行状态检查",
                "items": [
                    self._item("mysql.runtime.metrics", "工作负载与缓存指标", "timeseries/mysql_status.csv", runtime_rows,
                               f"本次为约 {sampling.get('realtime_window_seconds')} 秒短时现场采样，可用于发现即时异常，不代表全天或业务高峰趋势。",
                               status="attention" if sampling.get("short_window") else "normal",
                               recommendation="容量判断应优先接入连续历史监控；短时采样仅作为现场补充证据。",
                               evidence=runtime_evidence, collection=collection("timeseries.realtime_sampling")),
                    self._item("mysql.runtime.sessions", "当前会话", "tables/processlist.tsv", process_rows,
                               f"采集时共取得 {len(process_rows)} 条会话记录；系统会话（system user/event_scheduler）无当前 SQL 属正常现象。",
                               evidence=[f"会话记录 {len(process_rows)} 条"],
                               collection=collection("mysql.processlist"), total_rows=len(ctx.tables.get("processlist", []))),
                    self._item("mysql.runtime.locks", "事务与锁等待", "tables/long_transactions.tsv; tables/data_lock_waits.tsv; tables/metadata_locks_pending.tsv", lock_rows,
                               lock_conclusion,
                               status=lock_status,
                               recommendation=lock_recommendation,
                               evidence=lock_evidence),
                ],
            },
            {
                "section_id": "capacity_objects",
                "title": "容量与对象检查",
                "items": [
                    self._item("mysql.capacity.schemas", "Schema 容量", "tables/database_sizes.tsv", db_sizes,
                               "已采集各 Schema 数据与索引大小；请结合磁盘使用率和增长趋势评估容量规划。" if db_sizes else "未发现业务 Schema 容量记录，本项不适用。",
                               status="ok" if db_sizes else "not_applicable",
                               evidence=[f"Schema 容量记录 {len(db_sizes)} 条"],
                               collection=collection("mysql.database_sizes"),
                               total_rows=len(ctx.tables.get("database_sizes", []))),
                    self._item("mysql.capacity.objects", "数据库对象统计", "tables/object_counts.tsv", object_counts,
                               "已采集各 Schema 表、视图及引擎分布。" if object_counts else "未发现业务数据库对象，本项不适用。",
                               status="ok" if object_counts else "not_applicable",
                               evidence=[f"对象统计记录 {len(object_counts)} 条"],
                               collection=collection("mysql.object_counts"),
                               total_rows=len(ctx.tables.get("object_counts", []))),
                    self._item("mysql.capacity.risks", "对象结构与容量候选项", "tables/*capacity*.tsv", object_risk_rows,
                               capacity_risk_conclusion,
                               status=capacity_risk_status,
                               evidence=[
                                   f"业务 Schema {len(business_schemas)} 个",
                                   f"候选项合计 {object_risk_total} 项",
                                   *detail_notes,
                               ],
                               collection=collection("mysql.object_counts"), total_rows=len(object_risk_rows)),
                    *self._object_detail_item(ctx),
                    *self._programmable_objects_item(ctx, collection),
                ],
            },
            {
                "section_id": "sql_io",
                "title": "SQL、等待与文件 I/O 检查",
                "items": [
                    self._item("mysql.sql.digest", "SQL 摘要 Top", "tables/sql_digests_top.tsv", digest_rows,
                               (
                                   f"已采集脱敏 SQL 摘要 {len(digest_source)} 条；其中未用索引累计执行 {no_index_exec} 次，"
                                   "需结合业务 SQL 正文和执行计划复核，不能直接认定为问题 SQL。"
                               ) if no_index_exec else (
                                   f"已采集脱敏 SQL 摘要 {len(digest_source)} 条；本次窗口内未出现未用索引的执行记录，"
                                   "该结论仅覆盖采集窗口的摘要统计。"
                               ),
                               status="attention" if no_index_exec else "normal",
                               recommendation="按总耗时、扫描行数和未用索引次数筛选摘要，再由授权人员结合 SQL 正文和 EXPLAIN 验证。",
                               collection=collection("mysql.sql_digests"), total_rows=len(digest_source),
                               note=digest_note),
                    self._item("mysql.waits", "等待事件 Top", "tables/wait_events_top.tsv", wait_rows,
                               "等待事件以 idle 为主，采集时未见明显锁等待；短窗口不能排除业务高峰期阻塞。",
                               evidence=([f"等待事件 {len(wait_source)} 类", f"Top1：{wait_rows[0].get('等待事件')}（{wait_rows[0].get('次数')} 次）"] if wait_rows else []),
                               collection=collection("mysql.wait_events_top"), total_rows=len(wait_source)),
                    self._item("mysql.file_io", "文件 I/O Top", "tables/file_io_top.tsv", file_io_rows,
                               "已取得 Performance Schema 文件 I/O 累计统计；该数据为实例启动以来累计值，不等同于当前实时吞吐。",
                               recommendation="将累计等待时间较高的文件与实时磁盘 await、util 及存储监控关联分析。",
                               collection=collection("mysql.file_io_top"), total_rows=len(file_io_source),
                               note="最多展示 15 行。"),
                ],
            },
            {
                "section_id": "security",
                "title": "账号与权限检查",
                "items": [
                    self._item("mysql.security.accounts", "数据库账号", "tables/accounts.tsv", accounts,
                               "发现 root 允许从非本地地址登录，高权限账号暴露范围过大。" if remote_roots else "未发现 root 远程登录来源。",
                               status="risk" if remote_roots else "normal",
                               recommendation="创建具名管理账号并限制 root 登录来源；变更前确认自动化和应急运维依赖。" if remote_roots else "",
                               evidence=["root 来源：" + ", ".join(sorted({str(row.get('host')) for row in remote_roots}))] if remote_roots else [],
                               collection=collection("mysql.accounts"), total_rows=len(accounts_source)),
                    self._item("mysql.security.privileges", "全局权限汇总", "tables/user_privileges.tsv", privilege_rows,
                               "已按授权主体汇总全局权限；高权限账号应结合岗位、来源限制和审计要求逐一复核。",
                               status="attention",
                               recommendation="建立具名账号和最小权限基线，定期复核长期未使用及高权限授权。",
                               collection=collection("mysql.user_privileges"), total_rows=len(privilege_source)),
                ],
            },
            {
                "section_id": "logs_backup_replication",
                "title": "日志、备份与复制检查",
                "items": [
                    self._item("mysql.logs.files", "日志文件元数据", "tables/log_files.tsv", log_rows,
                               "日志文件路径、可读性、大小和修改时间已取得；默认未采集日志正文。" + log_rotation_note,
                               status=log_rotation_status,
                               recommendation=(
                                   "确认 logrotate 配置与定时任务是否真的执行，补齐 size+周期双条件轮转；"
                                   "已超大的文件先压缩归档再重建，不要直接删除正在写入的句柄。"
                                   if log_rotation_status == "attention" else ""
                               ),
                               evidence=[f"日志文件 {len(log_rows)} 个"]
                               + ([f"最大文件 {biggest_log['log_type']} {self._format_bytes(biggest_log['size_bytes'])}"] if biggest_log else []),
                               collection=collection("mysql.log_file_metadata"), total_rows=len(ctx.tables.get("log_files", []))),
                    self._item("mysql.logs.summary", "错误日志汇总", "tables/error_log_summary.tsv", error_rows,
                               f"采集窗口内错误日志汇总未发现 Error/Critical/System 级事件；共展示 {len(error_rows)} 类事件。",
                               evidence=[f"错误事件类型 {len(error_rows)} 类"],
                               collection=collection("mysql.error_log_summary"), total_rows=len(ctx.tables.get("error_log_summary", []))),
                    self._item("mysql.replication.binlog", "Binary Log 状态", "tables/binary_log_status.tsv", binary_status,
                               f"Binary Log 已启用；当前 Binlog {binary_status[0].get('当前 Binlog', '-') if binary_status else '-'}，GTID 模式 {role.get('gtid_mode')}。",
                               evidence=[f"log_bin={role.get('log_bin')}", f"gtid_mode={role.get('gtid_mode')}"]),
                    self._item("mysql.replication.binlog.files", "Binlog 文件列表", "tables/binary_logs.tsv", binary_logs,
                               (
                                   f"共 {binary_log_count} 个 Binlog 文件，合计 {self._format_bytes(binary_log_bytes)}"
                                   + (f"，其中 {binary_log_unencrypted} 个未加密" if binary_log_unencrypted else "")
                                   + (
                                       "；容量已超过 50 GiB 参考线，请核对保留期与实际恢复需求是否匹配"
                                       if binary_log_bytes > 50 * 1024 ** 3 else ""
                                   )
                                   + f"；下表为前 {len(binary_logs)} 条明细，完整清单见证据文件。"
                               ),
                               status="attention" if (binary_log_unencrypted or binary_log_bytes > 50 * 1024 ** 3) else "normal",
                               recommendation=(
                                   "启用 binlog 加密（binlog_encryption=ON）需先确认下游复制链路与备份工具兼容性。"
                                   if binary_log_unencrypted else ""
                               ),
                               evidence=[
                                   f"Binlog 文件 {binary_log_count} 个",
                                   f"合计容量 {self._format_bytes(binary_log_bytes)}",
                                   f"expire_logs_days={role.get('expire_logs_days') or role.get('binlog_expire_logs_seconds')}",
                               ] + (
                                   [f"未加密 {binary_log_unencrypted} 个"] if binary_log_unencrypted else []
                               )),
                    self._item("mysql.replication.status", "复制与高可用状态", "tables/replica_status.tsv; tables/group_replication_members.tsv", replica_rows,
                               replica_conclusion,
                               status=replica_status,
                               recommendation=replica_recommendation,
                               evidence=replica_evidence,
                               collection=collection("mysql.replica_status"), total_rows=len(replica_source)),
                    self._item("mysql.backup", "备份可恢复性证据", "evidence/backup_*.txt", [],
                               backup_conclusion,
                               status=backup_status,
                               evidence=backup_evidence,
                               recommendation="接入备份平台任务结果、备份保留策略以及最近一次恢复演练记录。",
                               collection=collection("system.backup_cron")),
                ],
            },
        ]

        bp_ratio = mysql.get("buffer_pool_to_memory_ratio")
        bp_miss = mysql.get("buffer_pool_read_miss_ratio")
        lock_count = (
            len(ctx.tables.get("long_transactions", []))
            + len(ctx.tables.get("data_lock_waits", []))
            + len(ctx.tables.get("metadata_locks_pending", []))
        )
        no_index_exec = sum(
            int(self._row_number(row, "SUM_NO_INDEX_USED") or 0)
            for row in digest_source
        )
        no_index_seconds = sum(
            self._row_number(row, "total_seconds") or 0
            for row in digest_source
            if (self._row_number(row, "SUM_NO_INDEX_USED") or 0) > 0
        )
        # 「复制与高可用」主题原先是写死的"未发现副本或集群成员运行证据"，
        # 与同章「复制与高可用状态」表（源端有残留通道、从库有运行通道）自相矛盾。
        # 改由本实例自己的复制行推导：线程停 → risk；通道在跑 → normal；
        # 只有指向自身的残留通道 → attention（源端）；一条都没有 → attention（单实例）。
        observed_role = role.get("role_observed")
        if stopped_channels:
            replication_topic_status = "risk"
            replication_topic_conclusion = (
                f"已启用 Binary Log 和 GTID，观测角色为 {observed_role}；"
                f"配置 {len(real_replica_rows)} 条复制通道，其中 {stopped_channels} 条 IO/SQL 线程未运行。"
            )
        elif real_replica_rows:
            replication_topic_status = "normal"
            replication_topic_conclusion = (
                f"已启用 Binary Log 和 GTID，观测角色为 {observed_role}；"
                f"本节点作为副本运行 {len(real_replica_rows)} 条复制通道，IO/SQL 线程均正常。"
            )
        elif residual_replica_rows:
            replication_topic_status = "attention"
            replication_topic_conclusion = (
                f"已启用 Binary Log 和 GTID，观测角色为 {observed_role}；"
                f"仅存在 {len(residual_replica_rows)} 条指向自身的残留复制通道，"
                "本节点未见上游连接、按复制源端处理；确认下游节点后需合并核对完整拓扑。"
            )
        else:
            replication_topic_status = "attention"
            replication_topic_conclusion = (
                f"已启用 Binary Log 和 GTID，观测角色为 {observed_role}；"
                "本节点未发现复制通道或集群成员运行证据，按单实例处理。"
            )
        conclusions = [
            {
                "topic": "参数配置合规",
                "status": "attention" if config_issues > 0 else "normal",
                "conclusion": (
                    f"已检查 4 组核心参数，{config_issues} 组存在可优化项："
                    + "；".join(config_issue_summaries)
                    if config_issues > 0 else
                    "已检查 4 组核心参数，未发现明显配置偏差。"
                ),
                "evidence": ["tables/global_variables.tsv"],
            },
            {
                "topic": "InnoDB 缓存运行",
                "status": "normal" if bp_miss is not None and bp_miss <= 0.01 else "attention",
                "conclusion": (
                    f"Buffer Pool 约占主机内存 {(bp_ratio or 0) * 100:.1f}%，"
                    f"采样窗口读未命中比例 {(bp_miss or 0) * 100:.4f}%；现场样本未显示明显缓存读压力。"
                ),
                "evidence": ["tables/global_variables.tsv", "timeseries/mysql_status.csv"],
            },
            {
                "topic": "事务与并发锁",
                "status": "normal" if lock_count == 0 else "risk",
                "conclusion": (
                    "本节点采集时点未发现长事务、数据锁等待或元数据锁等待。"
                    if lock_count == 0 else
                    f"本节点采集时点发现 {lock_count} 条事务或锁等待记录。"
                ),
                "evidence": ["tables/long_transactions.tsv", "tables/data_lock_waits.tsv", "tables/metadata_locks_pending.tsv"],
            },
            {
                "topic": "SQL 执行特征",
                "status": "attention" if no_index_exec else "normal",
                "conclusion": f"本节点采集窗口内，SQL 摘要中未用索引累计执行 {no_index_exec} 次、累计耗时约 {no_index_seconds:.1f} 秒；需要结合业务 SQL 正文和执行计划复核，不能直接认定为问题 SQL。",
                "evidence": ["tables/sql_digests_top.tsv"],
            },
            {
                "topic": "复制与高可用",
                "status": replication_topic_status,
                "conclusion": replication_topic_conclusion,
                "evidence": ["snapshot.json#role_evidence", "tables/replica_status.tsv"],
            },
            {
                "topic": "安全与可恢复性",
                "status": "risk" if remote_roots else "attention",
                "conclusion": "存在 root 远程登录来源，且缺少可验证的备份恢复证据，应优先收敛高权限访问并补充恢复验证。",
                "evidence": ["tables/accounts.tsv", "evidence/backup_*.txt"],
            },
        ]
        return sections, conclusions

    # 跨节点配置对比：本轮只作用于「日志、持久性与复制参数」组，作为机制验证。
    # 多实例巡检里配置漂移是最高价值产出，而「参数 / 采集值 / 说明」的单实例结构
    # 从根上无法呈现它。判定三态（达标 / 不达标 / 不适用）的语义见《可沉淀清单》J 节：
    # 期望值随角色变化，单实例下不具复制语义的参数必须落「不适用」，不得伪装成通过。
    CONFIG_VERDICT_STATES = ("达标", "不达标", "不适用")

    @staticmethod
    def _short_node_label(node: dict[str, Any]) -> str:
        """节点短标签：取 IPv4 末位（与报告正文 .33 / .34 / .125 的写法一致）。"""
        ip = str(node.get("ip") or "").strip()
        if ip.count(".") == 3:
            label = f".{ip.rsplit('.', 1)[-1]}"
        else:
            label = ip or str(node.get("hostname") or "节点")
        if str(node.get("role_effective") or "").strip().lower() == "source":
            label += "（源）"
        return label

    @staticmethod
    def _instance_node_label(instance: dict[str, Any]) -> str:
        """实例在风险台账里的节点标识。

        优先用完整 IP —— 风险登记册是独立章节，读者要能据此直接定位机器，
        不像配置对比表那样有"列名为 IPv4 末位"的表下说明。
        """
        identity = instance.get("identity") or {}
        ip = str(identity.get("ip") or "").strip()
        if ip:
            return ip
        return str(identity.get("hostname") or identity.get("instance_tag") or "本机")

    def _merged_findings(self, instances: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """把各实例的 findings 合成一份风险台账，并标注来源节点。

        原先只取 ``instances[0]``（``primary``）的 findings —— 多实例巡检时
        其余节点的风险完全不出现在风险登记册、安全章节和整改计划里，而
        PostgreSQL（``analyzer.py`` 的 ``all_findings``）与 Oracle
        （``findings_all``）都是全实例合并，MySQL 是唯一只取主体的插件。
        实测三节点巡检：``overall_health_summary`` 统计到 27 条风险，
        风险台账里只有 8 条。

        编号在这里统一重排，保证全报告唯一可引用（各实例的 finding_id 都从
        R001 起算，直接拼接会撞号）；单实例输入时逐条结果与原先一致。
        """
        merged: list[dict[str, Any]] = []
        multi = len(instances) >= 2
        for instance in instances:
            label = self._instance_node_label(instance)
            for finding in instance.get("findings") or []:
                item = dict(finding)
                item["finding_id"] = f"R{len(merged) + 1:03d}"
                if multi:
                    # 只有多实例才标节点：单实例正文本来就是它，加列没有信息量，
                    # 且会让冻结的单实例报告契约（frozen report contract）漂移。
                    item["node"] = label
                merged.append(item)
        return merged

    @staticmethod
    def _merged_confirmations(
        instances: list[dict[str, Any]]
    ) -> list[dict[str, Any]] | None:
        """跨节点合并「待客户确认事项」（与 _merged_findings 同理）。

        只取 ``instances[0]`` 会让其余节点需要客户确认的事项整段消失。同一主题在多个
        节点都触发时**保留各自的行、各带节点**，不按主题归并 —— 不同节点的证据
        （参数取值、涉及对象、失败来源）通常并不相同，归并会把差异吃掉。
        返回 ``None`` 表示所有实例都没有该字段（旧分析产物），渲染层据此区分
        「未产出清单」与「本次没有待确认事项」。
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

    # 矩阵列的档位顺序（严格档优先）。这里只声明顺序，**不声明中文名** ——
    # 严重度中文词汇表只有一处（`inspection_core.word_engine.SEVERITY_CN`），
    # 渲染层用它翻译列头，避免同一份映射在两层各写一遍后各自漂移。
    RISK_MATRIX_SEVERITIES = ("critical", "high", "medium", "low", "info")

    @classmethod
    def _risk_matrix(
        cls,
        findings: list[dict[str, Any]],
        topology: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        """风险分级 × 节点矩阵（《可沉淀清单》F2）。

        行 = 节点，列 = 严重度，单元格 = 该节点该级别的风险检出数。「风险压在哪台」
        原先只能靠通读几十条台账自己数，这里给一张汇总表。

        数据源就是风险台账本身（``_merged_findings`` 的产物），**不另算一份口径**
        —— 矩阵必须与登记册逐条对得上，否则同一章里两张表自相矛盾。
        单实例的 findings 不带 ``node``（见 ``_merged_findings``），没有第二个节点
        可对比，返回 ``None``；模型不注入该键、渲染层跳过，单实例输出保持不变。

        行序取拓扑层已排好的节点序（复制源端第一，见 AGENTS.md「顺序也由拓扑层定」），
        不得依赖调用者传包的先后；拓扑未解析时退化为 findings 首次出现顺序。
        **零风险的节点同样占一行**（计数为 0）——「哪台干净」本身就是读者要的信息，
        按 findings 反推行集合会把干净的节点整行丢掉。
        """
        counts: dict[str, dict[str, int]] = {}
        for finding in findings:
            node = str(finding.get("node") or "").strip()
            if not node:
                continue
            bucket = counts.setdefault(node, {})
            severity = str(finding.get("severity") or "").strip().lower()
            bucket[severity] = bucket.get(severity, 0) + 1
        if not counts:
            return None
        severities = [s for s in cls.RISK_MATRIX_SEVERITIES
                      if any(s in bucket for bucket in counts.values())]
        # 规则包将来新增的档位（未登记在本表里的严重度）也要成列，否则各列之和
        # 小于合计，读者会觉得算错了。按名称补在末尾。
        severities += sorted({s for bucket in counts.values() for s in bucket} - set(severities))

        order: list[str] = []
        roles: dict[str, str] = {}
        for node in (topology or {}).get("nodes") or []:
            ip = str(node.get("ip") or "").strip()
            if not ip:
                continue
            order.append(ip)
            roles[ip] = str(node.get("role_effective") or "").strip().lower()
        outside = sorted(ip for ip in counts if ip not in order)
        rows: list[dict[str, Any]] = []
        for ip in [*order, *outside]:
            bucket = counts.get(ip) or {}
            label = f"{ip}（源）" if roles.get(ip) == "source" else ip
            rows.append({
                "label": label,
                "counts": {s: bucket.get(s, 0) for s in severities},
                "total": sum(bucket.values()),
            })
        return {
            "severity_columns": severities,
            "rows": rows,
            "note": (
                "每一格是该节点在该级别上的风险检出数，与风险登记册逐条对应；"
                "标注（源）的是复制源端，其余为副本。同一规则在多个节点触发时分别"
                "计入各自节点，各行合计之和等于登记册条目总数。"
            ),
        }

    @staticmethod
    def _binlog_enabled(value: Any) -> bool:
        return str(value or "").strip().upper() in {"ON", "1", "TRUE"}

    @staticmethod
    def _format_seconds(value: Any) -> str:
        """把秒数渲染成人类可读的保留期（能整除到天/小时才转换，否则原样）。"""
        number = safe_int(value)
        if number is None:
            return "未采集"
        if number and number % 86400 == 0:
            return f"{number // 86400} 天"
        if number and number % 3600 == 0:
            return f"{number // 3600} 小时"
        return f"{number} 秒"

    def _instance_parameters(self, instance: dict[str, Any]) -> dict[str, str]:
        """取某个实例的全局参数。

        优先用 build_inspection_model 逐实例记录的完整 global_variables；
        手工构造的 analysis（单测、旧产物）没有这层缓存，回退 facts.key_variables。
        """
        cached = self._variables_by_instance.get(str(instance.get("instance_id") or ""))
        if cached:
            return cached
        return dict((instance.get("facts") or {}).get("key_variables") or {})

    def _config_comparison_nodes(
        self, analysis: dict[str, Any]
    ) -> tuple[list[dict[str, Any]], str]:
        """按拓扑排出参与对比的节点；判据不足时退化为单节点并说明原因。"""
        instances = analysis.get("instances") or []
        topology = analysis.get("topology") or {}
        nodes = topology.get("nodes") or []
        edges = topology.get("edges") or []
        instances_by_id = {str(item.get("instance_id")): item for item in instances}
        usable = (
            len(instances) >= 2
            and len(nodes) == len(instances)
            and bool(edges)
            and all(str(node.get("node_id")) in instances_by_id for node in nodes)
        )
        if usable:
            records: list[dict[str, Any]] = []
            labels: list[str] = []
            for index, node in enumerate(nodes, 1):
                instance = instances_by_id[str(node.get("node_id"))]
                label = self._short_node_label(node)
                if label in labels:  # 同名主机等导致的标签冲突，补序号避免覆盖同名列
                    label = f"{label}#{index}"
                labels.append(label)
                records.append({
                    "label": label,
                    "role": str(node.get("role_effective") or ""),
                    "values": self._instance_parameters(instance),
                })
            return records, ""
        reason = (
            "拓扑未解析，本表按单节点取值呈现，未生成跨节点对比"
            if len(instances) >= 2
            else "单实例巡检，本表按单节点取值呈现"
        )
        return [
            {
                "label": "采集值",
                "role": "",
                "values": self._instance_parameters(instances[0]),
            }
        ], reason

    @staticmethod
    def _format_size(value: Any) -> str:
        """字节数渲染成人类可读单位（1024 进制）。"""
        number = safe_float(value)
        if number is None:
            return "未采集"
        units = ("B", "KiB", "MiB", "GiB", "TiB")
        index = 0
        while abs(number) >= 1024 and index < len(units) - 1:
            number /= 1024
            index += 1
        return f"{number:.2f} {units[index]}"

    @staticmethod
    def _node_value_groups(
        records: list[dict[str, Any]], name: str, formatter: Any = None
    ) -> str:
        """把各节点取值按同值分组渲染，形如「`.33`=3 天；`.34`、`.125`=5 天」。

        只写「节点间不一致」读者不知道去改哪一台，判定依据必须落到节点。
        """
        groups: dict[str, list[str]] = {}
        for record in records:
            raw = str(record["values"].get(name) or "").strip()
            display = formatter(raw) if (formatter and raw) else (raw or "未采集")
            groups.setdefault(display, []).append(str(record["label"]))
        return "；".join(f"{'、'.join(labels)}={value}" for value, labels in groups.items())

    def _drift_verdict(
        self, records: list[dict[str, Any]], name: str, reason: str, formatter: Any = None
    ) -> tuple[str, str]:
        """通用「节点间取值应当一致」判定。"""
        values = [str(record["values"].get(name) or "").strip() for record in records]
        if any(value == "" for value in values):
            return "不适用", f"{name} 在部分节点未采集，未参与对比"
        if len(set(values)) > 1:
            return "不达标", reason.format(values=self._node_value_groups(records, name, formatter))
        return "达标", ""

    def _memory_config_verdict(
        self, name: str, records: list[dict[str, Any]]
    ) -> tuple[str, str]:
        if name == "innodb_redo_log_capacity":
            # 该变量值**不代表实际容量**：容量由旧参数 innodb_log_file_size ×
            # innodb_log_files_in_group 算出时不回写变量（MySQL 8.0 手册 §17.6.5）。
            # 三节点变量值都是 100MB，而 .125 实际是 4GB —— 只看变量会得出假结论。
            # 这里显式落「不适用」，实际容量由规则 MYSQL.INNODB.REDO_CAPACITY
            # 读状态变量 Innodb_redo_log_capacity_resized 判定并进风险台账。
            return "不适用", "变量值不代表实际容量（由旧参数算出时不回写），实际容量见风险台账 MYSQL.INNODB.REDO_CAPACITY"
        if name == "innodb_flush_method":
            bad = [
                str(record["label"]) for record in records
                if str(record["values"].get(name) or "").strip().lower() in {"fsync", "fdatasync"}
            ]
            if bad:
                return "不达标", "、".join(bad) + " 取 fsync/fdatasync，InnoDB 与操作系统页缓存双重缓冲，写入路径变长"
            return "达标", ""
        if name == "innodb_io_capacity":
            low: list[str] = []
            for record in records:
                number = safe_float(str(record["values"].get(name) or "").strip())
                if number is not None and number <= 200:
                    low.append(f"{record['label']}={int(number)}")
            if low:
                return "不达标", "、".join(low) + "（仍为默认值，未针对存储调优），SSD/云盘场景建议 2000-4000（HDD 需结合 await 判断）"
            return "达标", ""
        if name == "innodb_buffer_pool_size":
            return self._drift_verdict(
                records, name,
                "节点间不一致（{values}），副本缓存能力与源端不同；若为容量规划刻意设置请在文档注明",
                self._format_size,
            )
        if name in {"innodb_log_file_size", "innodb_log_buffer_size"}:
            return self._drift_verdict(
                records, name,
                "节点间不一致（{values}）；这些旧参数参与 redo 实际容量的计算，"
                "各节点取值不同会让实际容量也不一致（见风险台账 MYSQL.INNODB.REDO_CAPACITY）",
                self._format_size,
            )
        if name == "innodb_log_files_in_group":
            # 该参数是「个数」不是字节数，不能套 _format_size（否则 2 会渲染成 2.00 B）。
            return self._drift_verdict(
                records, name,
                "节点间不一致（{values}）；该参数是 redo 文件个数，与 innodb_log_file_size 相乘决定 "
                "redo 实际容量，各节点取值不同会让实际容量也不一致（见风险台账 MYSQL.INNODB.REDO_CAPACITY）",
            )
        if name == "innodb_io_capacity_max":
            return self._drift_verdict(
                records, name,
                "节点间不一致（{values}），同型号存储上的写突发能力不同",
            )
        if name == "innodb_adaptive_hash_index":
            return self._drift_verdict(
                records, name,
                "节点间不一致（{values}），副本与源端的自适应哈希策略不同；"
                "写密集场景 AHI 可能反而成为瓶颈，应统一后再评估",
            )
        return self._drift_verdict(records, name, "节点间不一致（{values}）")

    def _connection_config_verdict(
        self, name: str, records: list[dict[str, Any]]
    ) -> tuple[str, str]:
        if name in {"tmp_table_size", "max_heap_table_size"}:
            # 两者不等时以较小者为准，超出部分会静默落成磁盘临时表。
            unequal: dict[tuple[str, str], list[str]] = {}
            for record in records:
                low = safe_float(str(record["values"].get("tmp_table_size") or "").strip())
                high = safe_float(str(record["values"].get("max_heap_table_size") or "").strip())
                if low is None or high is None or low == high:
                    continue
                key = (self._format_size(low), self._format_size(high))
                unequal.setdefault(key, []).append(str(record["label"]))
            if unequal:
                detail = "；".join(
                    f"{'、'.join(labels)}：tmp_table_size {low} vs max_heap_table_size {high}"
                    for (low, high), labels in unequal.items()
                )
                return "不达标", detail + " 两项取值不等，实际以较小者为准，会打出非预期的磁盘临时表"
            return "达标", ""
        if name == "max_connections":
            return self._drift_verdict(
                records, name,
                "节点间不一致（{values}），切换后承载能力不同，故障转移时可能出现连接被拒",
            )
        return self._drift_verdict(records, name, "节点间不一致（{values}），属于配置漂移")

    def _charset_config_verdict(
        self, name: str, records: list[dict[str, Any]]
    ) -> tuple[str, str]:
        if name == "lower_case_table_names":
            return self._drift_verdict(
                records, name,
                "节点间不一致（{values}），副本的表名解析策略与源端不同，切换后可能找不到表",
            )
        if name == "collation_server":
            values = [str(record["values"].get(name) or "").strip() for record in records]
            if any(value == "" for value in values):
                return "不适用", "部分节点未采集 collation_server，未参与对比"
            if len(set(values)) == 1:
                return "达标", ""
            families = {value.split("_", 1)[0] for value in values}
            flavors = {
                "0900" if "_0900_" in value else ("general" if "_general_" in value else "other")
                for value in values
            }
            groups = self._node_value_groups(records, name)
            if len(families) == 1 and len(flavors) > 1:
                return "不达标", f"同字符集混用不同排序规则（{groups}），字符串比较与排序结果跨节点不一致"
            return "不达标", f"节点间不一致（{groups}），跨节点比较与排序语义不同"
        if name == "time_zone":
            values = [str(record["values"].get(name) or "").strip() for record in records]
            if any(value == "" for value in values):
                return "不适用", "部分节点未采集 time_zone，未参与对比"
            if len(set(values)) > 1:
                groups = self._node_value_groups(records, name)
                return "不达标", f"节点间不一致（{groups}），同一时刻各节点写入的时间戳会不同"
            if values[0].upper() == "SYSTEM":
                return "不达标", "取值为 SYSTEM，依赖操作系统时区；各节点系统时区不一致时日志时间线会错位"
            return "达标", ""
        return self._drift_verdict(
            records, name,
            "节点间不一致（{values}），跨节点写入的字符集解释可能不同",
        )

    def _config_verdict(
        self,
        item_id: str,
        name: str,
        records: list[dict[str, Any]],
        has_replication: bool,
    ) -> tuple[str, str]:
        if item_id == "mysql.config.memory":
            return self._memory_config_verdict(name, records)
        if item_id == "mysql.config.connection":
            return self._connection_config_verdict(name, records)
        if item_id == "mysql.config.charset":
            return self._charset_config_verdict(name, records)
        return self._durability_verdict(name, records, has_replication)

    def _durability_verdict(
        self,
        name: str,
        records: list[dict[str, Any]],
        has_replication: bool,
    ) -> tuple[str, str]:
        """给出单个参数的判定状态与依据。

        期望值随角色与拓扑形态变化：源端与从库的要求不同，单实例下部分参数
        没有复制语义（应落「不适用」而不是伪装成达标）。缺失值一律保持「未采集」，
        不得当成 0 参与判定（见 analysis contracts.missing_value_policy）。
        """
        values = {
            str(record["label"]): str(record["values"].get(name) or "").strip()
            for record in records
        }
        if any(value == "" for value in values.values()):
            return "不适用", "部分节点缺少该参数，未参与对比，不计为达标"
        binlog_on = [
            record for record in records
            if self._binlog_enabled(record["values"].get("log_bin"))
        ]
        if name == "log_bin":
            if not has_replication:
                return "不适用", "单实例无复制链路；是否开启取决于按时间点恢复的需求"
            if any(not self._binlog_enabled(value) for value in values.values()):
                return "不达标", "关闭 binlog 的节点无法为下游提供变更流，也无法按时间点恢复"
            return "达标", ""
        if name == "gtid_mode":
            if not has_replication:
                return "不适用", "单实例无 GTID 复制语义"
            if any(value.upper() != "ON" for value in values.values()):
                return "不达标", "GTID 未在所有节点开启，切换与故障转移依赖 GTID 连续性"
            return "达标", ""
        if name == "enforce_gtid_consistency":
            gtid_off = any(
                str(record["values"].get("gtid_mode") or "").strip().upper() != "ON"
                for record in records
            )
            if not has_replication:
                return "不适用", "单实例无 GTID 复制语义"
            if gtid_off:
                return "不适用", "gtid_mode 未开启时该参数不生效"
            if any(value.upper() != "ON" for value in values.values()):
                return "不达标", "一致性校验关闭，不安全语句可能进入 binlog 并破坏复制"
            return "达标", ""
        if name == "innodb_flush_log_at_trx_commit":
            if any(value != "1" for value in values.values()):
                return "不达标", "非 1 时崩溃可丢失已提交事务，持久性下降"
            return "达标", ""
        if not binlog_on:
            return "不适用", "所有节点均未开启 binlog，该参数不生效"
        if name == "binlog_format":
            if any(
                str(record["values"].get(name) or "").strip().upper() != "ROW"
                for record in binlog_on
            ):
                return "不达标", "非 ROW 格式可能造成主从数据不一致"
            return "达标", ""
        if name == "sync_binlog":
            if any(
                str(record["values"].get(name) or "").strip() != "1"
                for record in binlog_on
            ):
                return "不达标", "sync_binlog≠1 时崩溃可能丢失已提交事务的 binlog"
            return "达标", ""
        if name == "binlog_expire_logs_seconds":
            parsed: list[int] = []
            for record in binlog_on:
                number = safe_int(str(record["values"].get(name) or "").strip())
                if number is None:
                    return "不适用", "保留期取值无法解析，未参与对比"
                parsed.append(number)
            if any(number == 0 for number in parsed):
                return "不达标", "0 表示 binlog 永不过期，磁盘空间存在耗尽风险"
            if len(set(parsed)) > 1:
                return "不达标", (
                    "节点间保留期不一致（"
                    + self._node_value_groups(records, name, self._format_seconds)
                    + "），按时间点恢复的窗口不同"
                )
            return "达标", ""
        return "达标", ""

    def attach_config_comparison(self, analysis: dict[str, Any]) -> None:
        """把配置检查组改写为「跨节点并排 + 行级判定」。

        11.6 原先只渲染本实例的 global_variables，多实例巡检时读不出配置漂移。
        这里按拓扑把各节点取值并排（列名取 IP 末位），并逐行给出
        「达标 / 不达标 / 不适用」。本轮只作用于耐久性参数组，作为机制验证；
        拓扑判据不足时退化为单节点取值，绝不造出空节点列。
        """
        records, degraded = self._config_comparison_nodes(analysis)
        instances = analysis.get("instances") or []
        if not instances or not records:
            return
        multi = not degraded
        primary = instances[0]
        items_by_id: dict[str, dict[str, Any]] = {}
        for section in primary.get("inspection_sections") or []:
            for item in section.get("items") or []:
                items_by_id[str(item.get("item_id"))] = item
        for item_id, _title in CONFIG_GROUP_TITLES:
            item = items_by_id.get(item_id)
            if item is not None:
                self._rewrite_config_item(item, item_id, records, degraded, multi)

    def _rewrite_config_item(
        self,
        item: dict[str, Any],
        item_id: str,
        records: list[dict[str, Any]],
        degraded: str,
        multi: bool,
    ) -> None:
        """把一组配置项改写成「跨节点并排 + 行级判定」。

        判定三态与角色守卫的语义见《可沉淀清单》J 节；节点列名取 IP 末位，
        判定依据与结论里一律用完整节点标识，读者据此可以直接定位机器。
        """
        variables = CONFIG_GROUP_VARIABLES[item_id]
        base_values = records[0]["values"] if records else {}
        rows: list[dict[str, Any]] = []
        failed: list[str] = []
        skipped: list[str] = []
        failed_nodes: dict[str, list[str]] = {}
        for name, label in variables:
            row: dict[str, Any] = {"参数": f"{label}（{name}）"}
            if multi:
                for record in records:
                    value = record["values"].get(name)
                    row[str(record["label"])] = value if str(value or "").strip() else "未采集"
            else:
                value = records[0]["values"].get(name) if records else None
                row["采集值"] = value if str(value or "").strip() else "未采集"
            state, reason = self._config_verdict(item_id, name, records, multi)
            row["判定"] = f"{state}（{reason}）" if reason else state
            if state == "不达标":
                failed.append(name)
                baseline = str(base_values.get(name) or "").strip()
                for record in records:
                    value = str(record["values"].get(name) or "").strip()
                    if value and value != baseline:
                        failed_nodes.setdefault(str(record["label"]), []).append(name)
            elif state == "不适用":
                skipped.append(name)
            rows.append(row)
        notes: list[str] = []
        if degraded:
            notes.append(degraded)
        if multi:
            notes.append("列名为节点 IPv4 末位，标注（源）的是复制源端")
        notes.append(
            "判定口径为 达标 / 不达标 / 不适用，「不适用」表示该参数在当前角色或拓扑形态下"
            "没有判定语义（例如单实例的复制相关参数），不等于通过"
        )
        evidence = list(item["analysis"].get("evidence") or [])
        evidence.append(f"跨节点对比节点数 {len(records)}")
        if failed:
            evidence.append("不达标参数：" + "、".join(failed))
        if skipped:
            evidence.append("不适用参数：" + "、".join(skipped))
        conclusion = str(item["analysis"].get("conclusion") or "")
        if failed:
            # 结论必须点名节点：只写「有不一致」，读者不知道去改哪一台。
            located = "；".join(
                f"{node} 的 {'、'.join(names)}" for node, names in failed_nodes.items()
            )
            conclusion += (
                f"跨节点逐项判定发现 {len(failed)} 项不达标（{'、'.join(failed)}）"
                + (f"，涉及 {located}" if located else "")
                + "，逐行判定见判定列。"
            )
        elif skipped:
            conclusion += f"另有 {len(skipped)} 项在当前拓扑形态下不适用（非缺陷）。"
        item["display"]["rows"] = rows
        item["display"]["shown_rows"] = len(rows)
        item["display"]["total_rows"] = len(rows)
        item["display"]["note"] = "；".join(notes) + "。"
        if multi:
            item["source"] = "tables/global_variables.tsv（各节点）"
        item["analysis"]["evidence"] = evidence
        item["analysis"]["conclusion"] = conclusion
        if failed:
            item["analysis"]["status"] = "attention"

    def attach_topology_replication(self, analysis: dict[str, Any]) -> None:
        """把源端实例的复制状态从「本机上游行」改写为「下游从库」。

        11.5 原先只渲染本实例的 ``SHOW REPLICA STATUS``。当本实例是复制源端时，
        本机没有真实的上游复制行（那条 ``Source_Host`` 指向自己的是残留通道），
        真正要评价的「下游从库是否在运行、延迟多少」记录在其它节点的
        ``facts.role_evidence`` 里。这里按拓扑把下游节点补进复制状态表，
        否则报告只显示一条无意义的残留通道，读不到真实的主从健康。
        """
        topology = analysis.get("topology") or {}
        nodes = topology.get("nodes") or []
        edges = topology.get("edges") or []
        instances = analysis.get("instances") or []
        if not (nodes and edges and instances):
            return
        primary = instances[0]
        primary_id = primary.get("instance_id")
        by_id = {node.get("node_id"): node for node in nodes}
        primary_node = by_id.get(primary_id) if primary_id else None
        if not primary_node:
            return
        # 只在源端改写：从库/级联中间节点保留本机观测到的上游行。
        if str(primary_node.get("role_effective") or "").strip().lower() != "source":
            return
        targets = [
            by_id[edge.get("target_node_id")]
            for edge in edges
            if edge.get("source_node_id") == primary_id
            and edge.get("target_node_id") in by_id
        ]
        if not targets:
            return
        instances_by_id = {item.get("instance_id"): item for item in instances}
        rows: list[dict[str, Any]] = []
        stopped = 0
        lags: list[float] = []
        for node in targets:
            role = (
                (instances_by_id.get(node.get("node_id")) or {}).get("facts") or {}
            ).get("role_evidence") or {}
            io_state = role.get("replica_io_running")
            sql_state = role.get("replica_sql_running")
            lag = role.get("replica_lag_seconds")
            lag_value = float(lag) if isinstance(lag, (int, float)) and not isinstance(lag, bool) else None
            lag_display: Any = "未采集"
            if lag_value is not None:
                lag_display = int(lag_value) if lag_value.is_integer() else lag_value
            if str(io_state).strip().upper() != "YES" or str(sql_state).strip().upper() != "YES":
                stopped += 1
            if lag_value is not None:
                lags.append(lag_value)
            address = ":".join(
                str(value) for value in (node.get("ip"), node.get("port")) if value not in (None, "")
            )
            rows.append({
                "从库主机": node.get("hostname") or node.get("instance_tag") or "未采集",
                "地址": address or "未采集",
                "IO 线程": str(io_state) if io_state not in (None, "") else "未采集",
                "SQL 线程": str(sql_state) if sql_state not in (None, "") else "未采集",
                # 缺失值保持"未采集"，不得显示成 0（见 analysis contracts.missing_value_policy）。
                "延迟秒": lag_display,
            })
        residual = [
            edge for edge in topology.get("self_reference_edges") or []
            if edge.get("target_node_id") == primary_id
        ]
        residual_note = (
            f"本机 SHOW REPLICA STATUS 中另有 {len(residual)} 条指向自身的残留通道，不构成真实主从关系。"
            if residual else ""
        )
        lag_txt = f"，最大延迟 {max(lags):.0f} 秒" if lags else ""
        if stopped:
            status = "risk"
            conclusion = (
                f"本实例为复制源端，下游 {len(rows)} 个从库中有 {stopped} 个 IO/SQL 线程未运行；"
                "复制中断会直接削弱高可用与数据保护能力。" + residual_note
            )
            recommendation = "检查复制错误、网络和源端状态，制定可回滚的恢复步骤。"
        elif residual:
            status = "attention"
            conclusion = (
                f"本实例为复制源端，下游 {len(rows)} 个从库 IO/SQL 线程均在运行{lag_txt}；"
                + residual_note
                + "残留通道应确认后清理或补全上游信息。"
            )
            recommendation = "持续监控复制延迟和错误日志；确认残留通道为历史遗留后清理（RESET REPLICA ALL）。"
        else:
            status = "normal"
            conclusion = f"本实例为复制源端，下游 {len(rows)} 个从库 IO/SQL 线程均在运行{lag_txt}。"
            recommendation = "持续监控复制延迟和错误日志；变更前确认切换机制与演练记录。"
        evidence = [f"下游从库 {len(rows)} 个"]
        if residual:
            evidence.append(f"本机残留复制通道 {len(residual)} 条")
        note = "本实例按复制源端处理；表中为声明以本实例为上游的从库。"
        for section in primary.get("inspection_sections") or []:
            for item in section.get("items") or []:
                if item.get("item_id") != "mysql.replication.status":
                    continue
                item["display"]["rows"] = rows
                item["display"]["shown_rows"] = len(rows)
                item["display"]["total_rows"] = len(rows)
                item["display"]["note"] = note
                # 表里是拓扑推导出的下游从库，不是本机表行；来源与计数同步改写，
                # 否则图注会出现"数据来源：本机 replica_status；原始记录：1 条"却列 2 行。
                item["source"] = "topology.edges; 各节点 tables/replica_status.tsv"
                item["collection"]["row_count"] = len(rows)
                item["analysis"]["status"] = status
                item["analysis"]["conclusion"] = conclusion
                item["analysis"]["recommendation"] = recommendation
                item["analysis"]["evidence"] = evidence
        for entry in primary.get("comprehensive_conclusions") or []:
            if entry.get("topic") == "复制与高可用":
                entry["status"] = status
                entry["conclusion"] = conclusion
                entry["evidence"] = ["tables/replica_status.tsv", "topology.edges"]

    @staticmethod
    def _metric_commentary(instance: dict[str, Any]) -> dict[str, str]:
        """Generate data-driven commentary based on actual metric values.

        Returns dict with keys: cpu, memory, mysql, io
        """
        metrics = instance.get("metrics", {})
        system = metrics.get("system_realtime", {})
        mysql = metrics.get("mysql_realtime", {})
        hist = metrics.get("system_history", {})
        sampling = metrics.get("sampling_context", {})
        history = sampling.get("history", {})
        findings = instance.get("findings", [])

        short = sampling.get("short_window", True)
        window_label = "短时采样期间" if short else "巡检窗口内"

        def _pct(v: Any) -> str:
            return f"{v:.1f}%" if v is not None else "未采集"

        # --- CPU ---
        cpu = system.get("cpu_busy_percent", {})
        cpu_avg = cpu.get("average")
        cpu_max = cpu.get("max")
        cpu_p95 = cpu.get("p95")
        iowait = system.get("cpu_iowait_percent", {})
        iowait_avg = iowait.get("average")
        iowait_max = iowait.get("max")

        cpu_parts = [f"{window_label} CPU 使用率平均 {_pct(cpu_avg)}，峰值 {_pct(cpu_max)}"]
        if cpu_p95 is not None and not short:
            cpu_parts.append(f"P95 {_pct(cpu_p95)}")
        if iowait_avg is not None and iowait_avg > 5:
            cpu_parts.append(f"IO wait 平均 {_pct(iowait_avg)}，峰值 {_pct(iowait_max)}")

        if cpu_avg is not None:
            if cpu_avg >= 90:
                cpu_parts.append("CPU 持续高负载，建议定位高消耗 SQL 或进程并评估扩容。")
            elif cpu_avg >= 70:
                cpu_parts.append("CPU 有一定负载，建议关注高峰期趋势。")
            else:
                cpu_parts.append("CPU 负载处于健康水平，建议结合更长时间窗口确认。")
        else:
            cpu_parts.append("该结果仅代表现场快照，不代表全天趋势。")

        cpu_text = "；".join(cpu_parts)

        # --- Memory ---
        mem = system.get("memory_used_percent", {})
        mem_avg = mem.get("average")
        mem_available = system.get("memory_available_bytes", {})
        avail_min = mem_available.get("min")
        total_mem = (instance.get("facts", {}).get("host_identity", {}) or {}).get("memory_total_bytes")

        mem_parts = [f"{window_label} 内存使用率平均 {_pct(mem_avg)}"]
        if avail_min is not None and total_mem:
            avail_pct = avail_min / total_mem * 100
            mem_parts.append(f"最低可用内存 {avail_pct:.1f}%")
        if mem_avg is not None:
            if mem_avg >= 95:
                mem_parts.append("可用内存严重不足，存在 SWAP/OOM 风险。")
            elif mem_avg >= 85:
                mem_parts.append("内存使用较高，建议核算连接和缓存内存预算。")
            elif mem_avg >= 70:
                mem_parts.append("内存使用处于中等水平，结合 SWAP 活动与高峰期变化综合评估。")
            else:
                mem_parts.append("内存使用水平健康。")
        else:
            mem_parts.append("需结合可用内存与更长历史窗口判断。")

        mem_text = "；".join(mem_parts)

        # --- MySQL QPS/TPS ---
        qps = mysql.get("qps", {})
        tps = mysql.get("tps", {})
        qps_avg = qps.get("average")
        qps_max = qps.get("max")
        tps_avg = tps.get("average")
        connects = mysql.get("threads_connected", {})
        connects_max = connects.get("max")

        mysql_parts = [f"{window_label} QPS 平均 {qps_avg:.0f}" if qps_avg is not None else "QPS 未采集"]
        if qps_max is not None:
            mysql_parts.append(f"峰值 {qps_max:.0f}")
        if tps_avg is not None:
            mysql_parts.append(f"TPS 平均 {tps_avg:.0f}")
        if connects_max is not None:
            mysql_parts.append(f"并发连接峰值 {connects_max:.0f}")

        buffer_pool = mysql.get("buffer_pool_to_memory_ratio")
        if buffer_pool is not None:
            mysql_parts.append(f"Buffer Pool 占内存 {buffer_pool * 100:.1f}%")

        # find related findings
        finding_titles = [f.get("title", "") for f in findings]
        if any("连接使用率" in t for t in finding_titles):
            mysql_parts.append("⚠ 连接数已接近上限，建议排查连接泄漏并优化连接池配置。")
        if any("临时表落盘" in t for t in finding_titles):
            mysql_parts.append("⚠ 临时表落盘比例偏高，建议检查关联 SQL 和 tmp 参数。")
        if any("Buffer Pool" in t and "物理读" in t for t in finding_titles):
            mysql_parts.append("⚠ Buffer Pool 读未命中率偏高，建议增加缓存或优化 SQL。")

        if not short:
            mysql_parts.append("短窗口 QPS/TPS 不用于容量规划。")
        else:
            mysql_parts.append("短窗口数据仅供趋势参考。")

        mysql_text = "；".join(mysql_parts)

        # --- Disk IO ---
        disk_devices = system.get("disk_devices", {})
        io_parts: list[str] = []
        for dev, data in disk_devices.items():
            util = data.get("util", {})
            await_ = data.get("read_await", {})
            util_p95 = util.get("p95")
            if util_p95 is not None and util_p95 > 10:
                await_p95 = await_.get("p95")
                detail = f"，读延迟 P95 {await_p95:.1f}ms" if await_p95 else ""
                io_parts.append(
                    f"设备 {dev} 利用率 P95 {_pct(util_p95)}{detail}"
                )
        if io_parts:
            io_text = "；".join(io_parts)
            io_text += "，建议关注磁盘 I/O 延迟与数据库物理读写的关系。"
        else:
            io_text = "实时采样未观察到显著磁盘 I/O 压力。"

        return {"cpu": cpu_text, "memory": mem_text, "mysql": mysql_text, "io": io_text}

    def _object_detail_item(self, ctx: PackageContext) -> list[dict[str, Any]]:
        """把对象候选项落成可定位的明细行；无数据时返回空列表（不产出这张表）。

        措辞严格贴合采集口径：冗余索引取自 ``sys.schema_redundant_indexes``（自带
        ``sql_drop_index``）；未使用索引取自 ``sys.schema_unused_indexes``，其语义是
        "**实例启动以来**未见使用"，不等于"永远不该存在"——所以只写"未见使用"，
        不写成删除结论，删除留给业务确认后的整改环节。
        """
        per_kind = OBJECT_DETAIL_PER_KIND

        def pick(record: dict[str, Any], *keys: str) -> Any:
            lowered = {str(key).lower(): value for key, value in record.items()}
            for key in keys:
                value = record.get(key)
                if value in (None, ""):
                    value = lowered.get(key.lower())
                if value not in (None, ""):
                    return value
            return None

        def safe(value: Any, suffix: str = "") -> str:
            return f"{value}{suffix}" if value not in (None, "") else "未采集"

        def number(value: Any) -> str:
            try:
                return f"{float(value):.2f}"
            except (TypeError, ValueError):
                return safe(value)

        def count(value: Any) -> str:
            """自增剩余量这类大整数：带千分位，别用科学计数法。"""
            try:
                return f"{int(float(value)):,}"
            except (TypeError, ValueError):
                return safe(value)

        rows: list[dict[str, Any]] = []

        def add(kind: str, records: Any, describe: Any, index_key: str = "") -> None:
            for record in (records or [])[:per_kind]:
                if not isinstance(record, dict):
                    continue
                schema = pick(record, "TABLE_SCHEMA", "object_schema")
                table = pick(record, "TABLE_NAME", "object_name")
                if not schema or not table:
                    continue
                label = f"{schema}.{table}"
                index_name = pick(record, index_key) if index_key else None
                if index_name:
                    label = f"{label}（{index_name}）"
                rows.append({"类型": kind, "对象": label, "关键信息": describe(record)})

        add("无主键表", ctx.tables.get("no_primary_key_top"),
            lambda r: f"行数 {safe(pick(r, 'TABLE_ROWS'))}，共 {safe(pick(r, 'total_mb'), ' MB')}")
        add("非 InnoDB 表", ctx.tables.get("non_innodb_tables"),
            lambda r: f"引擎 {safe(pick(r, 'ENGINE'))}，行数 {safe(pick(r, 'TABLE_ROWS'))}")
        # 碎片明细按**可回收空间**重排：采集端是碎片率倒序，TOP 会被"分配 0.02 MB /
        # 空闲 18 MB"的极小表占满（99.91%），真正值得回收的表反而排在后面。
        fragmentation_rows = sorted(
            ctx.tables.get("fragmentation_top") or [],
            key=lambda record: (
                row_number(record, "data_free_mb") is None,
                -(row_number(record, "data_free_mb") or 0.0),
            ),
        )
        add("高碎片表", fragmentation_rows,
            lambda r: (
                f"可回收 {safe(pick(r, 'data_free_mb'), ' MB')}"
                f"（分配 {safe(pick(r, 'allocated_mb'), ' MB')}，"
                f"碎片率 {safe(pick(r, 'fragmentation_pct'), '%')}）"
            ))
        add("冗余索引", ctx.tables.get("redundant_indexes"),
            lambda r: f"被 {safe(pick(r, 'dominant_index_name'))} 覆盖，可用 sql_drop_index 删除",
            index_key="redundant_index_name")
        add("未使用索引", ctx.tables.get("unused_indexes"),
            lambda r: "实例启动以来未见使用", index_key="index_name")
        add("自增容量", ctx.tables.get("auto_increment_usage"),
            lambda r: (
                f"已用 {number(pick(r, 'used_pct'))}%（{safe(pick(r, 'COLUMN_TYPE'))}）"
                # 剩余量用绝对值而不是"剩余百分比"：100-22.79 这种数字没有行动价值，
                # 而"还能写多少行"才是判断要不要扩容的依据。采集缺失时整段不出现。
                + (
                    f"，剩余 {count(pick(r, 'remaining'))}"
                    if pick(r, "remaining") not in (None, "") else ""
                )
            ))

        if not rows:
            return []
        counts: dict[str, int] = {}
        for row in rows:
            counts[row["类型"]] = counts.get(row["类型"], 0) + 1
        summary = "、".join(f"{kind} {count}" for kind, count in counts.items())
        return [self._item(
            "mysql.capacity.risk_details",
            "对象候选项明细",
            "tables/no_primary_key_top.tsv; tables/non_innodb_tables.tsv; "
            "tables/fragmentation_top.tsv; tables/redundant_indexes.tsv; "
            "tables/unused_indexes.tsv; tables/auto_increment_usage.tsv",
            rows,
            f"按类别列出对象候选项明细（{summary}）；完整清单见同目录证据文件，纳入整改前需结合业务确认。",
            status="attention",
            recommendation=(
                "无主键表补主键前先确认业务写入路径；非 InnoDB 表转 InnoDB 需评估锁与容量；"
                "冗余与未使用索引先确认非唯一约束或业务依赖，再按 sql_drop_index 逐条删除。"
            ),
            evidence=[f"候选项明细 {len(rows)} 条", summary],
            total_rows=len(rows),
            note=(
                f"每类最多展示前 {per_kind} 项；完整清单保留在采集包 tables/ 目录。"
                "碎片明细已按可回收空间重排（采集端原序为碎片率倒序），因此本表不保证涵盖"
                "全部「空闲空间大但碎片率低」的表。"
                "碎片率仅指可回收空间占比，本表不构成任何删除或重建判定。"
            ),
        )]

    def _programmable_objects_item(
        self, ctx: PackageContext, collection: Any
    ) -> list[dict[str, Any]]:
        """存储程序与定时事件清单。

        ``tables/routines.tsv`` 与 ``tables/events.tsv`` 一直是采集项，但报告从未呈现
        （见 ``docs/check-catalog.yaml`` 的 ``known_mapping_gaps``）。事件必须按 STATUS
        **三态**呈现：``SLAVESIDE_DISABLED`` 的语义是「源端执行、副本侧不执行」，是副本上
        正确的收敛做法，不能与 ``ENABLED`` 混在一起计数 —— 否则会把运维已经做对的部分
        当成没做。角色相关的风险判定仍归规则 ``MYSQL.REPLICATION.REPLICA_WRITABLE``，
        本项只摆事实，避免同一个问题出现两份判据。
        """
        events = ctx.tables.get("events") or []
        routines = ctx.tables.get("routines") or []
        if not events and not routines:
            return []
        status_cn = {
            "ENABLED": "已启用",
            "SLAVESIDE_DISABLED": "副本侧禁用",
            "DISABLED": "已禁用",
        }
        rows: list[dict[str, Any]] = []
        event_counts: dict[str, int] = {}
        for record in events:
            raw_status = str(record.get("STATUS") or "").strip().upper()
            event_counts[raw_status] = event_counts.get(raw_status, 0) + 1
            interval = " ".join(
                str(part) for part in (
                    record.get("INTERVAL_VALUE"), record.get("INTERVAL_FIELD"),
                ) if str(part or "").strip()
            )
            rows.append({
                "Schema": record.get("EVENT_SCHEMA"),
                "事件名": record.get("EVENT_NAME"),
                "状态": status_cn.get(raw_status, raw_status or "未采集"),
                "执行周期": interval or "-",
                "最后执行": record.get("LAST_EXECUTED") or "未执行",
                "定义者": record.get("DEFINER"),
            })
        routine_counts: dict[str, int] = {}
        for record in routines:
            kind = str(record.get("ROUTINE_TYPE") or "").strip().upper() or "UNKNOWN"
            routine_counts[kind] = routine_counts.get(kind, 0) + 1
        routine_total = sum(routine_counts.values())
        if not rows:
            # 没有事件对象时仍要让 routines 的统计有落脚点，否则整项渲染成"无记录"。
            rows = [
                {
                    "对象类型": {"PROCEDURE": "存储过程", "FUNCTION": "函数"}.get(kind, kind),
                    "数量": value,
                }
                for kind, value in sorted(routine_counts.items(), key=lambda pair: -pair[1])
            ]
        conclusion_parts: list[str] = []
        if routine_total:
            label = {"PROCEDURE": "存储过程", "FUNCTION": "函数"}
            detail = "、".join(
                f"{label.get(kind, kind)} {value} 个"
                for kind, value in sorted(routine_counts.items(), key=lambda pair: -pair[1])
            )
            conclusion_parts.append(
                f"本节点采集到存储程序 {routine_total} 个（{detail}）；存储程序密集的实例，"
                "慢查询往往来自过程内部的语句，优化需结合定义正文定位。"
            )
        if events:
            enabled = event_counts.get("ENABLED", 0)
            slaveside = event_counts.get("SLAVESIDE_DISABLED", 0)
            conclusion_parts.append(
                f"本节点定时事件 {len(events)} 个，其中已启用 {enabled} 个"
                + (f"、副本侧禁用 {slaveside} 个" if slaveside else "")
                + "。「副本侧禁用」表示该事件只在源端执行，是副本上正确的收敛做法；"
                "其余已启用事件在副本上是否应当执行属业务语义决策 —— "
                "角色相关的风险判定见风险台账的 MYSQL.REPLICATION.REPLICA_WRITABLE 与报告末尾的待确认事项。"
            )
        evidence: list[str] = []
        if routine_total:
            evidence.append(f"存储程序 {routine_total} 个")
        for status, value in sorted(event_counts.items(), key=lambda pair: -pair[1]):
            evidence.append(f"事件 {status_cn.get(status, status)} {value} 个")
        return [self._item(
            "mysql.capacity.programmable_objects",
            "存储程序与定时事件",
            "tables/routines.tsv; tables/events.tsv",
            rows,
            "".join(conclusion_parts) or "本次未采集到存储程序或定时事件对象。",
            evidence=evidence,
            collection=collection("mysql.events") if events else collection("mysql.routines"),
            total_rows=len(events) or len(routines),
        )]

    def _node_role_text(self, analysis: dict[str, Any], instance: dict[str, Any]) -> str:
        nodes = {
            str(node.get("node_id")): node
            for node in (analysis.get("topology") or {}).get("nodes") or []
        }
        node = nodes.get(str(instance.get("instance_id"))) or {}
        role = str(node.get("role_effective") or (instance.get("identity") or {}).get("role_observed") or "")
        if role == "source":
            return "复制源端"
        if role == "replica":
            return "副本"
        return "角色未判定"

    def _node_health_entry(self, analysis: dict[str, Any], instance: dict[str, Any]) -> dict[str, Any]:
        summary = instance.get("health_summary") or {}
        counts = summary.get("counts") or {}
        return {
            "node": self._instance_node_label(instance),
            "hostname": (instance.get("identity") or {}).get("hostname"),
            "role": self._node_role_text(analysis, instance),
            "score": summary.get("score"),
            "grade": summary.get("grade"),
            "high": counts.get("high", 0),
            "medium": counts.get("medium", 0),
            "low": counts.get("low", 0),
        }

    def _node_attributed_conclusions(
        self, analysis: dict[str, Any], primary: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """把综合结论落到具体节点。

        分析层的综合结论原先只有 6 个主题，且完全不提节点 —— 多实例巡检时读者
        看到「2 组参数存在可优化项」却不知道是哪一台。多实例输入时在原有主题之前
        补「节点风险分布」与逐节点风险清单，并给原有主题标注主体节点；
        单实例输入保持原样（冻结契约不漂移）。
        """
        base = [dict(item) for item in (primary.get("comprehensive_conclusions") or [])]
        instances = analysis.get("instances") or []
        if len(instances) < 2:
            return base

        rows: list[dict[str, Any]] = []
        digests: list[str] = []
        has_high = False
        for instance in instances:
            label = self._instance_node_label(instance)
            identity = instance.get("identity") or {}
            summary = instance.get("health_summary") or {}
            counts = summary.get("counts") or {}
            instance_findings = instance.get("findings") or []
            highs = [f for f in instance_findings if f.get("severity") == "high"]
            has_high = has_high or bool(highs)
            digests.append(
                f"{label}（{identity.get('hostname') or '主机名未采集'}，{self._node_role_text(analysis, instance)}）"
                f"健康分 {summary.get('score')}/100，风险 {len(instance_findings)} 项"
                f"（高 {len(highs)} / 中 {counts.get('medium', 0)} / 低 {counts.get('low', 0)}）"
            )
        rows.append({
            "topic": "节点风险分布",
            "node": "全部节点",
            "scope": "cluster",
            "status": "risk" if has_high else "attention",
            # 只保留「跨节点对比」这一句结论，不再把高风险条目的标题与事实抄进来：
            # 那是明细，第 12 章风险登记册逐条列着，抄一遍会让这一格涨到 758 字，
            # 又重新变成"全堆在一起"。
            "conclusion": "；".join(digests) + "。逐条事实、建议与证据见第 12 章风险登记册。",
            "evidence": ["risk_register"],
        })
        for instance in instances:
            label = self._instance_node_label(instance)
            identity = instance.get("identity") or {}
            summary = instance.get("health_summary") or {}
            instance_findings = instance.get("findings") or []
            if not instance_findings:
                rows.append({
                    "topic": f"{label} 节点风险清单",
                    "node": label,
                    "scope": "node_detail",
                    "status": "normal",
                    "conclusion": (
                        f"{identity.get('hostname') or '主机名未采集'}（{label}，"
                        f"{self._node_role_text(analysis, instance)}）健康分 {summary.get('score')}/100，未发现风险项。"
                    ),
                    "evidence": ["risk_register"],
                })
                continue
            highs = [f for f in instance_findings if f.get("severity") == "high"]
            meds = [f for f in instance_findings if f.get("severity") == "medium"]
            lows = [f for f in instance_findings if f.get("severity") == "low"]
            rows.append({
                "topic": f"{label} 节点风险清单",
                "node": label,
                # scope=node_detail：这是「明细」而不是「结论」。摘要位（1.1 综合结论）
                # 会跳过它，明细由第 12 章风险登记册承载。此前把每个节点的全部条目
                # 拼进 conclusion，三节点实测最长一条 1988 字，整坨挤在摘要表格的
                # 一个单元格里——读者根本读不下去，且与登记册逐条重复。
                "scope": "node_detail",
                "status": "risk" if highs else ("attention" if meds else "normal"),
                "conclusion": (
                    f"{identity.get('hostname') or '主机名未采集'}（{label}，"
                    f"{self._node_role_text(analysis, instance)}）健康分 {summary.get('score')}/100，"
                    f"风险 {len(instance_findings)} 项（高 {len(highs)} / 中 {len(meds)} / 低 {len(lows)}）；"
                    f"逐条事实、建议与证据见第 12 章风险登记册。"
                ),
                "evidence": ["risk_register"],
            })
        primary_label = self._instance_node_label(primary)
        for item in base:
            entry = dict(item)
            entry["node"] = f"{primary_label}（主体）"
            entry["scope"] = "cluster"
            entry["conclusion"] = f"【{primary_label}】" + str(item.get("conclusion") or "")
            rows.append(entry)
        return rows

    def build_report_model(self, analysis: dict[str, Any]) -> dict[str, Any]:
        instances = analysis.get("instances", [])
        primary = instances[0] if instances else {}
        identity = primary.get("identity", {})
        collector = primary.get("collector", {})
        facts = primary.get("facts", {})
        host = facts.get("host_identity", {}) or {}
        metrics = primary.get("metrics", {})
        # 风险台账跨节点合并（见 _merged_findings）：多实例巡检时只取
        # instances[0] 会让其余节点的风险整段消失。安全章节、整改计划与
        # 附录风险索引都由这一份派生，所以四处同源。
        findings = self._merged_findings(instances)
        health = dict(primary.get("health_summary") or {})
        if len(instances) >= 2:
            # 单看主体节点会把"集群健康"讲成"主机 .33 健康"。多实例时补一份
            # 分节点明细：读者要能一眼看出是哪台在拉低整体。
            health["nodes"] = [self._node_health_entry(analysis, instance) for instance in instances]
            worst = min(
                (entry for entry in health["nodes"] if entry.get("score") is not None),
                key=lambda entry: entry["score"],
                default=None,
            )
            health["scope_note"] = (
                "总分反映报告主体节点（复制源端）；各节点独立评分见下表，"
                "集群可用性由最弱节点决定"
                + (f"（当前最弱为 {worst['node']}，{worst['score']}/100）" if worst else "")
                + "。"
            )
        generated = analysis.get("analyzer", {}).get("generated_at")
        collection_date = str(collector.get("started_at") or generated or "")[:10]
        comments = self._metric_commentary(primary) if primary else {}
        mysql_report_metrics = {
            key: value for key, value in (metrics.get("mysql_realtime") or {}).items()
            if key != "derived_rate_series"
        }
        priorities = {"P1": [], "P2": [], "P3": []}
        for finding in findings:
            priority = "P1" if finding["severity"] == "high" else "P2" if finding["severity"] == "medium" else "P3"
            priorities[priority].append(RemediationAction(
                finding_id=finding["finding_id"],
                title=finding["title"],
                recommendation=finding["recommendation"],
                priority=priority,
            ).to_legacy_dict())
        collection_gaps: list[dict[str, Any]] = []
        quality = primary.get("collection_quality", {})
        for item in quality.get("non_ok_items", []):
            if item.get("status") not in {"partial", "permission_denied", "timeout", "error"}:
                continue
            if item.get("item_id") == "system.sar_history":
                continue
            collection_gaps.append(EvidenceDisclosure(
                check_id=str(item.get("item_id") or ""),
                status=str(item.get("status") or "unknown"),
                reason=item.get("normalization") or item.get("reason") or "采集未完整完成",
                recommended_action="修复采集条件后重采；已取得的部分数据仍保留为证据。",
            ).to_legacy_gap())
        history = metrics.get("sampling_context", {}).get("history", {})
        if not history.get("usable_for_trend_rules"):
            collection_gaps.append(EvidenceDisclosure(
                check_id="system.sar_history",
                status="insufficient_history",
                reason="；".join(history.get("reasons", [])) or "SAR 历史不可用于趋势判断",
                recommended_action="检查 sysstat 留存与轮转配置，确保巡检前已有连续、最新的历史数据。",
            ).to_legacy_gap())
        if any(
            item.get("item_id") == "mysql.backup"
            and item.get("analysis", {}).get("status") in {"not_evaluated", "attention"}
            for section in primary.get("inspection_sections", [])
            for item in section.get("items", [])
        ):
            collection_gaps.append(EvidenceDisclosure(
                check_id="mysql.backup_verification",
                status="external_evidence_required",
                reason="数据库现场采集不能证明备份任务成功或备份可恢复",
                recommended_action="补充备份平台任务结果、保留策略与恢复演练记录。",
            ).to_legacy_gap())
        model = {
            "schema_version": "2.0",
            "generator_contract": "mysql_inspection_report_model",
            "cover": {
                "title": "MySQL数据库巡检分析报告",
                "inspection_target": identity.get("hostname") or identity.get("instance_tag"),
                "database_version": identity.get("version"),
                "report_version": "V1.0",
                "inspection_date": collection_date,
            },
            "document_control": {
                "customer": "待填写", "database": "MySQL", "report_version": "V1.0",
                "generated_at": generated,
            },
            "overview": {
                "host": identity.get("hostname"), "ip": identity.get("ip"),
                "database_version": identity.get("version"), "collection_time": collector.get("started_at"),
                "data_quality": primary.get("collection_quality"),
            },
            "environment": {
                "cpu_cores": host.get("cpu_count"), "memory_bytes": host.get("memory_total_bytes"),
                "os": host.get("os"), "kernel": host.get("kernel"), "mysql_version": identity.get("version"),
                "database_target_is_local": host.get("database_target_is_local"),
            },
            "topology": analysis.get("topology"),
            "health_assessment": health,
            "system_analysis": {
                "metrics": metrics.get("system_realtime"), "sampling": metrics.get("sampling_context"),
                "charts": [c for c in primary.get("charts", []) if str(c.get("chart_id", "")).startswith("SYSTEM_")],
                "commentary": {"cpu": comments.get("cpu"), "memory": comments.get("memory")},
            },
            "mysql_performance": {
                "metrics": mysql_report_metrics, "activity": metrics.get("activity"),
                "charts": [c for c in primary.get("charts", []) if str(c.get("chart_id", "")).startswith("MYSQL_")],
                "commentary": comments.get("mysql"),
            },
            "security": [f for f in findings if f.get("category") == "security"],
            "capacity": metrics.get("capacity"),
            "risk_register": findings,
            "optimization_plan": priorities,
            "comprehensive_conclusions": self._node_attributed_conclusions(analysis, primary),
            "inspection_sections": primary.get("inspection_sections", []),
            "collection_gaps": collection_gaps,
            "appendix": {
                "collection_window": metrics.get("sampling_context"),
                "data_quality": primary.get("collection_quality"),
                "rule_evaluations": primary.get("rule_evaluations"),
                "disclaimer": "本报告基于采集窗口内可获得的证据自动生成。短时采样不代表全天负载；未采集或证据不足的项目不作通过结论，变更前应完成业务确认、备份与回滚评估。",
            },
        }
        pending = self._merged_confirmations(instances)
        if pending is not None:
            # 条件注入：旧基线 analysis.json 里没有这个键，无条件写入会打穿
            # test_report_builder_matches_frozen_report_contract（逐字典等值比对）。
            model["pending_confirmations"] = pending
        # 风险分级 × 节点矩阵（清单 F2）。单实例返回 None（无 node 可分组），
        # 同样走条件注入 —— 它由已合并的 findings 派生，不新增数据来源。
        matrix = self._risk_matrix(findings, analysis.get("topology"))
        if matrix is not None:
            model["risk_matrix"] = matrix
        return model
