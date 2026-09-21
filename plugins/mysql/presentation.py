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
from inspection_core.system_checks import disk_media, is_persistent_fstype
from plugins.mysql.metrics import (
    local_host_names,
    replica_threads_running,
    row_number,
    split_self_referencing_replica_rows,
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


def business_schema_names(rows: list[dict[str, Any]]) -> list[str]:
    """Return the non-system schema names present in ``tables/schemas.tsv``."""
    names: list[str] = []
    for row in rows:
        name = str(row.get("SCHEMA_NAME") or "").strip()
        if name and name.lower() not in MYSQL_SYSTEM_SCHEMAS and name not in names:
            names.append(name)
    return names


class MySQLPresentationBuilder:
    """Build MySQL inspection sections and the report model."""

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

        elif item_id == "mysql.config.persistence":
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
        status = self._status_index(ctx)
        snapshot = ctx.snapshot
        identity = snapshot.get("instance_identity", {})
        host = snapshot.get("host_identity", {})
        time_info = snapshot.get("time_evidence", {})
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

        def evidence_line_count(name: str) -> int:
            path = ctx.root / "evidence" / name
            if not path.exists():
                return 0
            return sum(
                1
                for line in path.read_text(encoding="utf-8", errors="replace").splitlines()
                if line.strip()
            )

        # 备份：采集端只提供"任务配置可见性"（cron / systemd timer / 备份进程），
        # 现场证据无法证明"最近一次备份成功"或"可恢复"。三者按实际内容分档，
        # 与规则 MYSQL.BACKUP.TASK_VISIBILITY 保持同一事实来源。
        backup_sources = ("backup_cron.txt", "backup_processes.txt", "backup_timers.txt")
        backup_lines = sum(evidence_line_count(name) for name in backup_sources)
        backup_seen = any((ctx.root / "evidence" / name).exists() for name in backup_sources)
        if backup_lines:
            backup_status = "attention"
            backup_conclusion = (
                "已取得备份任务配置线索，但任务配置不等同于最近一次备份成功；"
                "备份可恢复性仍需备份平台结果与恢复演练证据确认。"
            )
            backup_evidence = [f"备份任务配置 {backup_lines} 条（cron / systemd timer / 备份进程）"]
        elif backup_seen:
            backup_status = "not_evaluated"
            backup_conclusion = "已取得备份任务配置文件但内容为空，既无法判定备份任务是否存在，也不能判定备份有效。"
            backup_evidence = ["备份任务配置文件存在但无有效内容（cron / systemd timer / 备份进程）"]
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
        ntp_value = str(time_info.get("ntp_synchronized", "")).lower()
        ntp_ok = ntp_value in {"yes", "true", "1", "active"}
        time_rows = [
            value_row("本地时间", self._format_host_time(time_info.get("host_local_time")), "采集时主机时间"),
            value_row("时区", time_info.get("timezone"), "主机时区"),
            value_row("NTP 同步", time_info.get("ntp_synchronized"), "系统时间同步状态"),
        ]
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

        variable_groups = [
            (
                "mysql.config.memory",
                "内存与 InnoDB 核心参数",
                [
                    ("innodb_buffer_pool_size", "InnoDB Buffer Pool"),
                    ("innodb_buffer_pool_instances", "Buffer Pool 实例数"),
                    ("innodb_redo_log_capacity", "Redo Log 容量"),
                    ("innodb_log_file_size", "单个 Redo 文件大小"),
                    ("innodb_log_buffer_size", "Redo Log Buffer"),
                    ("innodb_flush_method", "InnoDB 刷盘方式"),
                ],
            ),
            (
                "mysql.config.connection",
                "连接、线程与缓存参数",
                [
                    ("max_connections", "最大连接数"),
                    ("thread_cache_size", "线程缓存"),
                    ("table_open_cache", "表打开缓存"),
                    ("table_definition_cache", "表定义缓存"),
                    ("tmp_table_size", "内存临时表上限"),
                    ("max_heap_table_size", "MEMORY 表上限"),
                ],
            ),
            (
                "mysql.config.durability",
                "日志、持久性与复制参数",
                [
                    ("log_bin", "Binary Log"),
                    ("binlog_format", "Binlog 格式"),
                    ("gtid_mode", "GTID 模式"),
                    ("enforce_gtid_consistency", "GTID 一致性"),
                    ("sync_binlog", "Binlog 同步策略"),
                    ("innodb_flush_log_at_trx_commit", "事务日志刷盘策略"),
                    ("binlog_expire_logs_seconds", "Binlog 保留秒数"),
                ],
            ),
            (
                "mysql.config.charset",
                "字符集与 SQL 模式",
                [
                    ("character_set_server", "服务端字符集"),
                    ("collation_server", "服务端排序规则"),
                    ("lower_case_table_names", "表名大小写策略"),
                    ("sql_mode", "SQL Mode"),
                    ("time_zone", "会话默认时区"),
                ],
            ),
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
        config_items.append(self._item(
            "mysql.config.file",
            "配置文件白名单参数",
            "tables/mycnf_allowlist.tsv",
            mycnf_rows,
            "已采集允许范围内的配置文件参数，用于核对启动配置；敏感配置及完整配置文件未纳入采集包。",
            recommendation="对关键参数同时核对配置文件值和运行值，避免重启后参数回退。",
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
        object_counts = self._select_rows(
            ctx.tables.get("object_counts", []),
            [("table_schema", "Schema"), ("base_tables", "表"), ("views", "视图"), ("innodb_tables", "InnoDB 表"), ("no_engine_objects", "非 InnoDB")],
            20,
        )
        object_risk_rows = [
            {"检查项": "无主键表", "数量": metrics.get("schema", {}).get("tables_without_primary_key"), "证据文件": "tables/no_primary_key_summary.tsv"},
            {"检查项": "非 InnoDB 表", "数量": metrics.get("schema", {}).get("non_innodb_table_count"), "证据文件": "tables/non_innodb_tables.tsv"},
            {"检查项": "自增容量候选", "数量": metrics.get("schema", {}).get("auto_increment_warning_count"), "证据文件": "tables/auto_increment_usage.tsv"},
            {"检查项": "碎片候选表", "数量": metrics.get("schema", {}).get("fragmentation_candidate_count"), "证据文件": "tables/fragmentation_top.tsv"},
            {"检查项": "冗余索引候选", "数量": metrics.get("schema", {}).get("redundant_index_count"), "证据文件": "tables/redundant_indexes.tsv"},
            {"检查项": "未使用索引候选", "数量": metrics.get("schema", {}).get("unused_index_candidate_count"), "证据文件": "tables/unused_indexes.tsv"},
        ]
        object_risk_total = sum(int(row.get("数量") or 0) for row in object_risk_rows)
        object_risk_detail = "、".join(
            f"{row['检查项']} {int(row.get('数量') or 0)}" for row in object_risk_rows
        )
        if business_schemas:
            capacity_risk_status = "attention" if object_risk_total else "normal"
            capacity_risk_conclusion = (
                f"业务 Schema {len(business_schemas)} 个；对象检查候选项合计 {object_risk_total} 项"
                f"（{object_risk_detail}），需结合业务确认后纳入整改。"
                if object_risk_total else
                f"业务 Schema {len(business_schemas)} 个；无主键、非 InnoDB、碎片、自增容量与索引类检查均未发现候选项。"
            )
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
        for row in log_rows:
            row["修改时间"] = self._format_host_time(row.get("修改时间"))
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
        binary_logs = self._select_rows(
            ctx.tables.get("binary_logs", []),
            [("Log_name", "Binlog 文件"), ("File_size", "大小字节"), ("Encrypted", "加密")],
            20,
        )
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
                               "主机时间未与 NTP 同步，日志关联、复制诊断和故障时间线存在偏差风险。" if not ntp_ok else "主机时间同步状态正常。",
                               status="risk" if not ntp_ok else "normal",
                               recommendation="启用并验证企业时间同步服务，统一数据库节点时区。" if not ntp_ok else "",
                               evidence=[f"NTP synchronized={time_info.get('ntp_synchronized')}"],
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
                               evidence=[f"业务 Schema {len(business_schemas)} 个", f"候选项合计 {object_risk_total} 项"],
                               collection=collection("mysql.object_counts"), total_rows=len(object_risk_rows)),
                    *self._object_detail_item(ctx),
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
                               "日志文件路径、可读性、大小和修改时间已取得；默认未采集日志正文。",
                               evidence=[f"日志文件 {len(log_rows)} 个"],
                               collection=collection("mysql.log_file_metadata"), total_rows=len(ctx.tables.get("log_files", []))),
                    self._item("mysql.logs.summary", "错误日志汇总", "tables/error_log_summary.tsv", error_rows,
                               f"采集窗口内错误日志汇总未发现 Error/Critical/System 级事件；共展示 {len(error_rows)} 类事件。",
                               evidence=[f"错误事件类型 {len(error_rows)} 类"],
                               collection=collection("mysql.error_log_summary"), total_rows=len(ctx.tables.get("error_log_summary", []))),
                    self._item("mysql.replication.binlog", "Binary Log 状态", "tables/binary_log_status.tsv", binary_status,
                               f"Binary Log 已启用；当前 Binlog {binary_status[0].get('当前 Binlog', '-') if binary_status else '-'}，GTID 模式 {role.get('gtid_mode')}。",
                               evidence=[f"log_bin={role.get('log_bin')}", f"gtid_mode={role.get('gtid_mode')}"]),
                    self._item("mysql.replication.binlog.files", "Binlog 文件列表", "tables/binary_logs.tsv", binary_logs,
                               f"已配置 {len(binary_logs)} 个 Binlog 文件。",
                               evidence=[f"expire_logs_days={role.get('expire_logs_days') or role.get('binlog_expire_logs_seconds')}"]),
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
                "conclusion": "采集时未发现长事务、数据锁等待或元数据锁等待；结果仅代表现场时点。" if lock_count == 0 else f"采集时发现 {lock_count} 条事务或锁等待记录。",
                "evidence": ["tables/long_transactions.tsv", "tables/data_lock_waits.tsv", "tables/metadata_locks_pending.tsv"],
            },
            {
                "topic": "SQL 执行特征",
                "status": "attention" if no_index_exec else "normal",
                "conclusion": f"SQL 摘要中未用索引累计执行 {no_index_exec} 次、累计耗时约 {no_index_seconds:.1f} 秒；需要结合业务 SQL 正文和执行计划复核，不能直接认定为问题 SQL。",
                "evidence": ["tables/sql_digests_top.tsv"],
            },
            {
                "topic": "复制与高可用",
                "status": "attention",
                "conclusion": f"已启用 Binary Log 和 GTID，观测角色为 {role.get('role_observed')}；未发现副本或集群成员运行证据。",
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
        add("高碎片表", ctx.tables.get("fragmentation_top"),
            lambda r: f"碎片率 {safe(pick(r, 'fragmentation_pct'), '%')}，可回收 {safe(pick(r, 'data_free_mb'), ' MB')}")
        add("冗余索引", ctx.tables.get("redundant_indexes"),
            lambda r: f"被 {safe(pick(r, 'dominant_index_name'))} 覆盖，可用 sql_drop_index 删除",
            index_key="redundant_index_name")
        add("未使用索引", ctx.tables.get("unused_indexes"),
            lambda r: "实例启动以来未见使用", index_key="index_name")
        add("自增容量", ctx.tables.get("auto_increment_usage"),
            lambda r: f"已用 {number(pick(r, 'used_pct'))}%（{safe(pick(r, 'COLUMN_TYPE'))}）")

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
                "碎片率仅指可回收空间占比，本表不构成任何删除或重建判定。"
            ),
        )]

    def build_report_model(self, analysis: dict[str, Any]) -> dict[str, Any]:
        instances = analysis.get("instances", [])
        primary = instances[0] if instances else {}
        identity = primary.get("identity", {})
        collector = primary.get("collector", {})
        facts = primary.get("facts", {})
        host = facts.get("host_identity", {}) or {}
        metrics = primary.get("metrics", {})
        findings = primary.get("findings", [])
        health = primary.get("health_summary", {})
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
        return {
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
            "comprehensive_conclusions": primary.get("comprehensive_conclusions", []),
            "inspection_sections": primary.get("inspection_sections", []),
            "collection_gaps": collection_gaps,
            "appendix": {
                "collection_window": metrics.get("sampling_context"),
                "data_quality": primary.get("collection_quality"),
                "rule_evaluations": primary.get("rule_evaluations"),
                "disclaimer": "本报告基于采集窗口内可获得的证据自动生成。短时采样不代表全天负载；未采集或证据不足的项目不作通过结论，变更前应完成业务确认、备份与回滚评估。",
            },
        }
