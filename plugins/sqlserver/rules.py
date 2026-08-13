"""Deterministic, configuration-driven SQL Server inspection rules."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any


@dataclass
class Finding:
    rule_id: str
    category: str
    title: str
    severity: str
    priority: str
    object_name: str
    evidence: str
    impact: str
    recommendation: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _num(value: Any) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _dt(value: Any) -> datetime | None:
    if not value:
        return None


def _elapsed_seconds(now: datetime, then: datetime | None) -> float | None:
    if then is None:
        return None
    if now.tzinfo is not None and then.tzinfo is None:
        then = then.replace(tzinfo=now.tzinfo)
    elif now.tzinfo is None and then.tzinfo is not None:
        then = then.replace(tzinfo=None)
    return (now - then).total_seconds()
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


class RuleEngine:
    def __init__(self, config: dict[str, Any], now: datetime):
        self.t = config.get("thresholds", {})
        self.excluded_databases = {str(x).lower() for x in config.get("excluded_databases", [])}
        self.now = now
        self.findings: list[Finding] = []

    def add(self, rule_id: str, category: str, title: str, severity: str,
            object_name: str, evidence: str, impact: str, recommendation: str) -> None:
        priority = "P1" if severity in {"critical", "high"} else "P2" if severity == "medium" else "P3"
        self.findings.append(Finding(rule_id, category, title, severity, priority,
                                     object_name, evidence, impact, recommendation))

    def evaluate(self, s: dict[str, Any]) -> list[dict[str, Any]]:
        self.instance(s)
        self.databases(s)
        self.backups(s)
        self.no_backup_databases(s)
        self.files(s)
        self.capacity_and_integrity(s)
        self.performance(s)
        self.tempdb(s)
        self.memory_status(s)
        self.database_objects(s)
        self.security(s)
        self.deep_health(s)
        self.jobs(s)
        self.ha(s)
        self.replication(s)
        self.errors(s)
        return [f.to_dict() for f in self.findings]

    def instance(self, s: dict[str, Any]) -> None:
        info = s.get("instance", {})
        cfg = {str(x.get("name", "")).lower(): x for x in s.get("configurations", [])}
        max_mem = _num((cfg.get("max server memory (mb)") or {}).get("value_in_use"))
        physical = _num(info.get("physical_memory_mb"))
        if max_mem and physical and max_mem > physical * self.t.get("max_memory_of_physical_pct", 90) / 100:
            evidence = f"max server memory={max_mem:.0f} MB（{'默认值，未配置' if max_mem > 1073741824 else '手动设置'}），物理内存={physical:.0f} MB"
            self.add("INSTANCE.MAX_MEMORY", "实例配置", "SQL Server 最大内存配置过高", "high", "max server memory",
                     evidence,
                     "可能挤压操作系统、备份代理和监控进程内存，造成换页或实例不稳定。",
                     "结合同机服务预留操作系统内存，评估后使用 sp_configure 调整最大内存。")
        maxdop = _num((cfg.get("max degree of parallelism") or {}).get("value_in_use"))
        if maxdop is not None and maxdop > self.t.get("max_recommended_maxdop", 8):
            self.add("INSTANCE.MAXDOP", "实例配置", "MAXDOP 高于通用建议值", "medium", "MAXDOP",
                     f"当前 MAXDOP={maxdop:.0f}", "高并发场景可能出现并行线程争用和查询波动。",
                     "结合 NUMA、CPU 核数和工作负载测试，将 MAXDOP 调整到合适范围；不要直接套用固定值。")
        cost = _num((cfg.get("cost threshold for parallelism") or {}).get("value_in_use"))
        if cost is not None and cost < self.t.get("min_cost_threshold_parallelism", 25):
            self.add("INSTANCE.COST_THRESHOLD", "实例配置", "并行开销阈值偏低", "medium", "cost threshold for parallelism",
                     f"当前值={cost:.0f}", "较轻查询也可能并行化，增加并发环境调度压力。",
                     "基于 Query Store/高峰工作负载逐步提高并行开销阈值并观察回归。")

    def databases(self, s: dict[str, Any]) -> None:
        for db in s.get("databases", []):
            name = str(db.get("name", "未知数据库"))
            excluded = name.lower() in self.excluded_databases
            state = str(db.get("state_desc", "")).upper()
            if state and state != "ONLINE":
                self.add("DB.STATE", "数据库", "数据库未处于 ONLINE", "critical", name,
                         f"state={state}", "应用可能无法访问，或数据库处于恢复/可疑/离线状态。",
                         "立即核查错误日志、存储与恢复状态，确认业务影响并按故障流程处理。")
            if bool(db.get("is_auto_close_on")) or bool(db.get("is_auto_shrink_on")):
                enabled = ", ".join(k for k in ("AUTO_CLOSE" if db.get("is_auto_close_on") else "", "AUTO_SHRINK" if db.get("is_auto_shrink_on") else "") if k)
                self.add("DB.AUTO_OPTIONS", "数据库", "生产不推荐的自动选项已启用", "medium", name,
                         enabled, "可能导致反复关闭数据库、文件收缩和增长，产生性能抖动与碎片。",
                         "评估后关闭 AUTO_CLOSE/AUTO_SHRINK，并以容量规划和计划维护替代。")
            if bool(db.get("is_trustworthy_on")):
                self.add("DB.TRUSTWORTHY", "数据库安全", "数据库 TRUSTWORTHY 已启用", "high", name,
                         "is_trustworthy_on=true", "与高权限数据库所有者或不安全模块组合时可能形成实例提权路径。",
                         "确认模块签名和跨库依赖；无明确需要时关闭 TRUSTWORTHY，并复核数据库所有者。")
            if bool(db.get("is_db_chaining_on")):
                self.add("DB.CHAINING", "数据库安全", "跨数据库所有权链已启用", "medium", name,
                         "is_db_chaining_on=true", "可能绕过对象级授权边界，扩大横向访问范围。",
                         "确认业务依赖，优先使用模块签名或显式授权；无依赖时关闭 DB_CHAINING。")
            if (name.lower() not in {"master", "model", "msdb", "tempdb"}
                    and self.t.get("require_query_store_for_user_db", True)
                    and db.get("is_query_store_on") is not None
                    and not bool(db.get("is_query_store_on"))):
                self.add("DB.QUERY_STORE", "性能治理", "用户数据库未启用 Query Store", "low", name,
                         "is_query_store_on=false", "缺少查询性能历史，发生计划回归时取证和回退能力受限。",
                         "评估版本、空间上限和采集模式后启用 Query Store，并建立容量与清理策略。")
            if str(db.get("page_verify_option_desc", "")).upper() not in {"", "CHECKSUM"}:
                self.add("DB.PAGE_VERIFY", "数据库", "页校验未使用 CHECKSUM", "high", name,
                         f"page_verify={db.get('page_verify_option_desc')}", "降低发现存储层静默损坏的能力。",
                         "评估后设置 PAGE_VERIFY CHECKSUM，并安排完整 DBCC CHECKDB 基线检查。")
            last_good = _dt(db.get("last_good_checkdb_time"))
            elapsed = _elapsed_seconds(self.now, last_good)
            age_days = elapsed / 86400 if elapsed is not None else None
            if not excluded and (age_days is None or age_days > self.t.get("checkdb_max_age_days", 14)):
                sev = "high" if last_good is None else "medium"
                evidence = "未取得成功 CHECKDB 时间" if last_good is None else f"距上次成功 CHECKDB {age_days:.1f} 天"
                self.add("DB.CHECKDB", "完整性", "数据库完整性检查不满足周期", sev, name, evidence,
                         "页损坏可能长期不被发现，缩短可恢复窗口。",
                         "在可控窗口对备份还原副本或生产库执行 DBCC CHECKDB，并保留结果。")

    def backups(self, s: dict[str, Any]) -> None:
        full_limit = self.t.get("backup_full_max_age_hours", 168)
        log_limit = self.t.get("backup_log_max_age_minutes", 60)
        for row in s.get("backups", []):
            name = str(row.get("database_name", "未知数据库"))
            full = _dt(row.get("last_full_backup"))
            elapsed = _elapsed_seconds(self.now, full)
            hours = elapsed / 3600 if elapsed is not None else None
            if hours is None or hours > full_limit:
                evidence = "无完整备份记录" if hours is None else f"完整备份距今 {hours:.1f} 小时"
                self.add("BACKUP.FULL", "备份恢复", "完整备份超期或缺失", "critical", name, evidence,
                         "介质故障或逻辑事故后可能无法按目标恢复。",
                         "立即核查备份任务、落盘位置与可恢复性，并完成一次受控还原验证。")
            if str(row.get("recovery_model_desc", "")).upper() in {"FULL", "BULK_LOGGED"}:
                log = _dt(row.get("last_log_backup"))
                elapsed = _elapsed_seconds(self.now, log)
                minutes = elapsed / 60 if elapsed is not None else None
                if minutes is None or minutes > log_limit:
                    evidence = "无日志备份记录" if minutes is None else f"日志备份距今 {minutes:.0f} 分钟"
                    self.add("BACKUP.LOG", "备份恢复", "日志备份超期或缺失", "high", name, evidence,
                             "恢复点目标无法保障，且事务日志可能持续增长。",
                             "核查日志备份链与作业，恢复合适频率并执行还原链验证。")
            if full and self.t.get("require_backup_checksum", True) and not bool(row.get("full_has_checksum")):
                self.add("BACKUP.CHECKSUM", "备份恢复", "最近完整备份未记录 CHECKSUM", "medium", name,
                         f"最近完整备份={row.get('last_full_backup')}，has_backup_checksums={row.get('full_has_checksum')}",
                         "备份过程中对页校验与备份介质错误的发现能力降低。",
                         "核查备份产品参数，启用 WITH CHECKSUM，并通过 RESTORE VERIFYONLY 与定期真实还原验证。")
            if full and self.t.get("require_backup_encryption", False) and not row.get("full_encryptor_type"):
                self.add("BACKUP.ENCRYPTION", "备份恢复", "最近完整备份未加密", "medium", name,
                         "encryptor_type 为空", "备份文件泄露时可能直接暴露业务数据。",
                         "结合密钥托管和恢复演练启用备份加密，妥善备份证书及私钥。")

    def files(self, s: dict[str, Any]) -> None:
        warn = self.t.get("file_io_warning_ms", 20)
        crit = self.t.get("file_io_critical_ms", 50)
        for f in s.get("database_files", []):
            name = f"{f.get('database_name', '?')}/{f.get('logical_name', '?')}"
            latency = max(_num(f.get("avg_read_latency_ms")) or 0, _num(f.get("avg_write_latency_ms")) or 0)
            if latency > warn:
                sev = "high" if latency >= crit else "medium"
                self.add("FILE.IO_LATENCY", "存储与文件", "数据库文件 I/O 延迟偏高", sev, name,
                         f"最大平均延迟={latency:.1f} ms", "查询、日志写入或检查点可能受存储瓶颈影响。",
                         "结合存储监控和业务高峰复核延迟，区分读写路径并排查热点 SQL 与队列。")
            if bool(f.get("is_percent_growth")):
                self.add("FILE.PERCENT_GROWTH", "存储与文件", "数据库文件使用百分比自动增长", "medium", name,
                         f"growth={f.get('growth_value')}", "大文件的增长幅度不可预测，可能造成长时间停顿。",
                         "按容量与增长速度设置固定 MB 增量，并提前扩容。")

    def capacity_and_integrity(self, s: dict[str, Any]) -> None:
        warn = self.t.get("volume_free_warning_pct", 20)
        crit = self.t.get("volume_free_critical_pct", 10)
        for v in s.get("volumes", []):
            pct = _num(v.get("free_pct"))
            if pct is not None and pct < warn:
                sev = "critical" if pct < crit else "high"
                self.add("STORAGE.FREE_SPACE", "存储与容量", "数据库所在卷剩余空间不足", sev,
                         str(v.get("volume_mount_point") or v.get("logical_volume_name") or "卷"),
                         f"剩余={pct:.1f}%（{_num(v.get('available_mb')) or 0:,.0f} MB）",
                         "文件自动增长、备份或 tempdb 扩展可能失败并引发业务中断。",
                         "立即核对增长趋势和同卷其他占用，释放或扩容后设置容量告警与预测。")
        for row in s.get("log_space", []):
            pct = _num(row.get("log_used_pct"))
            if pct is not None and pct >= self.t.get("log_used_warning_pct", 80):
                sev = "high" if pct >= self.t.get("log_used_critical_pct", 90) else "medium"
                self.add("LOG.SPACE", "事务日志", "事务日志使用率偏高", sev, str(row.get("database_name", "数据库")),
                         f"日志大小={_num(row.get('log_size_mb')) or 0:,.0f} MB，已用={pct:.1f}%",
                         "日志空间耗尽可能导致事务失败或数据库不可写。",
                         "检查 log_reuse_wait、长事务、复制/AG 和日志备份链；禁止把收缩作为常规处理。")
        for row in s.get("vlf_summary", []):
            count = _num(row.get("vlf_count"))
            if count is not None and count >= self.t.get("vlf_warning_count", 200):
                sev = "high" if count >= self.t.get("vlf_critical_count", 1000) else "medium"
                self.add("LOG.VLF", "事务日志", "VLF 数量过多", sev, str(row.get("database_name", "数据库")),
                         f"VLF={count:.0f}", "可能增加恢复、启动、备份和日志扫描时间。",
                         "先确认日志增长史和业务窗口，再通过受控收缩一次、合理预分配和固定增长量重建 VLF 布局。")
        suspect = [x for x in s.get("suspect_pages", []) if (_num(x.get("event_type")) or 0) in {1, 2, 3}]
        if suspect:
            by_db: dict[str, int] = {}
            for row in suspect:
                name = str(row.get("database_name", row.get("database_id", "数据库")))
                by_db[name] = by_db.get(name, 0) + 1
            for name, count in by_db.items():
                self.add("DB.SUSPECT_PAGES", "完整性", "msdb 记录了可疑或损坏页面", "critical", name,
                         f"严重事件页面数={count}", "可能存在真实数据页损坏、I/O 子系统问题或历史未闭环故障。",
                         "立即保全证据，核查 823/824/825 错误和存储日志，执行受控 CHECKDB，并按恢复策略处理。")

    def performance(self, s: dict[str, Any]) -> None:
        for b in s.get("blocking", []):
            sec = _num(b.get("wait_seconds")) or 0
            if sec >= self.t.get("blocking_warning_seconds", 60):
                self.add("PERF.BLOCKING", "性能与会话", "存在持续阻塞", "high", str(b.get("session_id", "会话")),
                         f"阻塞者={b.get('blocking_session_id')}，等待={sec:.0f}s，类型={b.get('wait_type')}",
                         "业务请求延迟并可能形成阻塞链或超时。",
                         "保存阻塞链与执行计划，优先缩短事务、补充有效索引并检查应用提交逻辑。")
        for r in s.get("active_requests", []):
            sid = _num(r.get("session_id")) or 0
            status = str(r.get("status", "")).lower()
            if sid < 51 or status == "background":
                continue
            sec = _num(r.get("elapsed_seconds")) or 0
            if sec >= self.t.get("long_request_seconds", 300):
                self.add("PERF.LONG_REQUEST", "性能与会话", "存在长时间运行请求", "medium", str(r.get("session_id", "会话")),
                         f"运行={sec:.0f}s，数据库={r.get('database_name')}", "可能持续占用 CPU、I/O、内存授权或锁。",
                         "结合执行计划、等待类型和业务预期判断是否优化或终止；禁止仅凭耗时直接 kill。")
        for c in s.get("connection_stats", []):
            count = _num(c.get("connection_count")) or 0
            if count >= self.t.get("single_source_connection_warning", 50):
                self.add("PERF.CONNECTION_FLOOD", "性能与会话", "单一来源连接数偏高", "medium",
                         f"{c.get('client_ip','未知')}/{c.get('program_name','未知程序')}",
                         f"连接数={int(count)}，登录={c.get('login_name')}",
                         "可能是连接池配置不当、应用进程泄露或连接风暴，可能导致工作线程耗尽。",
                         "检查应用连接池大小策略，确认连接释放逻辑，评估实例最大连接数配置。")
        samples = s.get("performance_samples", [])
        if samples:
            ple = min((_num(x.get("page_life_expectancy")) for x in samples), default=None)
            grants = max((_num(x.get("memory_grants_pending")) for x in samples), default=None)
            if ple is not None and ple < self.t.get("ple_warning_seconds", 300):
                self.add("PERF.PLE", "性能与内存", "Page Life Expectancy 偏低", "medium", "Buffer Manager",
                         f"采样最小 PLE={ple:.0f}s", "可能存在缓冲池压力或大范围扫描。",
                         "结合各 NUMA 节点 PLE、内存授权、物理内存与高读 SQL 联合判断，避免孤立调参。")
            if grants is not None and grants > self.t.get("memory_grants_pending_warning", 0):
                self.add("PERF.MEMORY_GRANTS", "性能与内存", "存在等待内存授权的查询", "high", "Memory Manager",
                         f"采样最大 Memory Grants Pending={grants:.0f}", "查询可能排队或溢写 tempdb。",
                         "检查大内存授权查询、统计信息和并发度，并评估实例内存配置。")

    def tempdb(self, s: dict[str, Any]) -> None:
        t = s.get("tempdb", {})
        used = _num(t.get("used_pct"))
        if used is not None and used >= self.t.get("tempdb_used_warning_pct", 80):
            self.add("TEMPDB.USAGE", "TempDB", "TempDB 使用率偏高", "high", "tempdb",
                     f"已用={used:.1f}%", "排序、哈希、版本存储或临时对象可能耗尽空间。",
                     "定位占用会话和版本存储，确认磁盘余量，并按峰值预分配文件。")
        files = [x for x in t.get("files", []) if str(x.get("type_desc", "")).upper() == "ROWS"]
        sizes = {_num(x.get("size_mb")) for x in files}
        growths = {str(x.get("growth_value")) for x in files}
        if len(files) > 1 and (len(sizes) > 1 or len(growths) > 1):
            self.add("TEMPDB.FILE_BALANCE", "TempDB", "TempDB 数据文件大小或增长不一致", "medium", "tempdb",
                     f"数据文件数={len(files)}，大小种类={len(sizes)}，增长配置种类={len(growths)}",
                     "比例填充可能失衡，产生单文件热点。",
                     "在维护窗口统一数据文件初始大小和固定增长量，结合 CPU 与争用决定文件数。")

    def security(self, s: dict[str, Any]) -> None:
        sec = s.get("security", {})
        # Security checks now handled by dedicated security_checks module
        for check in sec.get("security_checks", []):
            name = str(check.get("check_name", ""))
            result = str(check.get("result", "PASS")).upper()
            detail = str(check.get("detail", ""))
            if result == "FAIL":
                severity_map = {
                    "empty_password_logins": "critical",
                    "sa_account_disabled": "high",
                    "xp_cmdshell_enabled": "high",
                    "ole_automation_enabled": "medium",
                    "authentication_mode": "info"
                }
                title_map = {
                    "empty_password_logins": "存在空密码登录",
                    "sa_account_disabled": "sa 账号未禁用",
                    "xp_cmdshell_enabled": "xp_cmdshell 已启用",
                    "ole_automation_enabled": "Ole Automation 已启用",
                    "authentication_mode": "认证模式提示"
                }
                sev = severity_map.get(name, "medium")
                self.add(f"SEC.{name.upper()}", "安全", title_map.get(name, name), sev, name, detail,
                         "扩大攻击面或增加凭据风险。",
                         "确认业务依赖后按最小权限原则处理；对高风险项立即禁用或加固。")
        sysadmins = [x for x in sec.get("sysadmin_members", []) if not x.get("is_disabled")]
        if len(sysadmins) > self.t.get("max_sysadmin_logins", 5):
            self.add("SEC.SYSADMIN_COUNT", "安全", "sysadmin 有效成员较多", "medium", "sysadmin",
                     f"有效成员数={len(sysadmins)}", "高权限账号面扩大，增加误操作与凭据风险。",
                     "逐一确认责任人和用途，移除不必要成员，使用最小权限角色并纳入定期复核。")
        for login in sec.get("sql_logins", []):
            if str(login.get("name", "")).lower() == "sa" and not login.get("is_disabled"):
                self.add("SEC.SA_ENABLED", "安全", "sa 登录名处于启用状态", "medium", "sa",
                         "is_disabled=false", "内置高权限账号是常见攻击目标。",
                         "确认兼容性后禁用或重命名 sa，使用具名管理账号并实施强认证与审计。")
        for server in sec.get("linked_servers", []):
            if bool(server.get("is_rpc_out_enabled")) or bool(server.get("is_data_access_enabled")):
                self.add("SEC.LINKED_SERVER", "安全", "链接服务器启用了远程访问能力", "low", str(server.get("name", "链接服务器")),
                         f"data_access={server.get('is_data_access_enabled')}，rpc_out={server.get('is_rpc_out_enabled')}",
                         "错误的登录映射或远端权限可能形成横向访问路径。",
                         "逐一复核业务用途、登录映射、RPC OUT 和远端账号权限，移除闲置链接服务器。")

    def no_backup_databases(self, s: dict[str, Any]) -> None:
        for row in s.get("no_backup_databases", []):
            name = str(row.get("database_name", "未知数据库"))
            self.add("BACKUP.NO_BACKUP", "备份恢复", "数据库从未进行过完整备份", "critical", name,
                     f"恢复模式={row.get('recovery_model_desc')}，创建于 {row.get('create_date')}",
                     "介质故障或逻辑事故后数据完全无法恢复。",
                     "立即对目标数据库执行完整备份并至少保留一份异地副本，完成首次还原验证。")

    def memory_status(self, s: dict[str, Any]) -> None:
        mem_list = s.get("memory_status", [])
        if not mem_list:
            return
        m = mem_list[0]
        util = _num(m.get("memory_utilization_percentage"))
        if util is not None and util >= self.t.get("memory_utilization_warning_pct", 95):
            self.add("MEM.SIGNALING", "性能与内存", "SQL Server 内存利用率极高", "high", "SQL Server Process",
                     f"memory_utilization={util:.1f}%", "进程内存可能接近上限，存在内存无响应或 VLF 饥饿风险。",
                     "评估 max server memory 配置和外部内存压力，结合 PLE、授权、锁页策略进行调优。")
        if bool(m.get("process_physical_memory_low")) or bool(m.get("process_virtual_memory_low")):
            self.add("MEM.PRESSURE", "性能与内存", "SQL Server 报告内存资源不足信号", "critical", "SQL Server Process",
                     f"physical_low={m.get('process_physical_memory_low')}, virtual_low={m.get('process_virtual_memory_low')}",
                     "操作系统向 SQL Server 发出内存不足通知，可能导致工作线程饥饿或服务中断。",
                     "立即检查外部内存压力和锁定页，验证 max server memory 是否正确配置；必要时联系系统管理员。")
        pool = s.get("buffer_pool", [])
        if pool:
            top = pool[0]
            total_cache = sum((_num(x.get("cache_size_mb")) or 0) for x in pool)
            top_pct = (_num(top.get("cache_pct")) or 0) * 100 if total_cache > 0 else 0
            if top_pct >= self.t.get("single_db_cache_warning_pct", 60):
                self.add("MEM.CACHE_IMBALANCE", "性能与内存", "单一数据库占用缓冲池比例过高", "medium",
                         str(top.get("database_name", "未知库")),
                         f"缓存={_num(top.get('cache_size_mb')) or 0:.0f} MB，占比={top_pct:.1f}%",
                         "可能由少量大查询或索引导致，抢占其他数据库内存。",
                         "结合高逻辑读 SQL 和缺失索引定位热点对象，评估是否需要拆分或加内存。")

    def database_objects(self, s: dict[str, Any]) -> None:
        for row in s.get("tables_without_pk", []):
            count = _num(row.get("table_count")) or 0
            if count > 0:
                name = str(row.get("database_name", "未知库"))
                self.add("OBJ.NO_PK", "数据库对象", "数据库存在无主键表", "medium", name,
                         f"无主键表数={int(count)}", "缺少主键影响复制、CDC、变更捕获和行级定位，增加误操作风险。",
                         "复核业务影响后为所有生产表添加逻辑主键或业务唯一约束。")
        for row in s.get("large_tables", []):
            size = _num(row.get("total_mb")) or 0
            rows = _num(row.get("row_count")) or 0
            threshold = self.t.get("large_table_warning_mb", 10240)
            if size >= threshold:
                self.add("OBJ.LARGE_TABLE", "数据库对象", "大表超过容量告警阈值", "low",
                         f"{row.get('database_name','?')}/{row.get('schema_name','?')}.{row.get('table_name','?')}",
                         f"大小={size:.0f} MB，行数={rows:,.0f}",
                         "大表占用较多空间并影响维护操作耗时。",
                         "评估分区、归档或清理策略，规划在线重建与统计维护窗口。")
        for db in s.get("database_rcsi", []):
            if bool(db.get("is_read_only", False)):
                continue
            name = str(db.get("database_name", ""))
            excluded = name.lower() in self.excluded_databases
            if not excluded and not db.get("RCSI_enabled") and self.t.get("recommend_rcsi_for_user_db", True):
                self.add("DB.RCSI", "数据库", "数据库未启用 RCSI (Read Committed Snapshot)", "medium", name,
                         "RCSI_enabled=false", "读取可能被写入阻塞，增加 tempdb 中的版本存储压力也完全不同。",
                         "评估业务读写冲突后启用 RCSI，注意 tempdb 版本存储空间规划。")

    def replication(self, s: dict[str, Any]) -> None:
        rep = s.get("replication", [])
        if not rep:
            return
        for pub in rep:
            name = str(pub.get("publication_name", ""))
            if not name:
                continue
            status = str(pub.get("status_desc", "")).upper()
            if status == "INACTIVE":
                self.add("HA.REPLICATION", "高可用", "复制发布处于非活跃状态", "high", name,
                         f"status={status}，type={pub.get('type_desc')}",
                         "业务变更无法同步到订阅者，影响数据一致性。",
                         "检查分发作业、订阅者连通性与日志读取器代理状态，确认是否需要重建或移除。")
            elif not status and pub.get("subscriber_server"):
                self.add("HA.REPLICATION_CONFIG", "高可用", "复制存在订阅配置", "info",
                         f"{name} -> {pub.get('subscriber_server')}.{pub.get('subscriber_db')}",
                         f"type={pub.get('publication_type')}，streams={pub.get('subscription_streams')}",
                         "复制链条正常工作中。",
                         "持续监控复制延迟、分发清理和代理作业状态。")

    def deep_health(self, s: dict[str, Any]) -> None:
        stale_days = self.t.get("stale_stats_days", 14)
        mod_pct_limit = self.t.get("stale_stats_modification_pct", 20)
        for st in s.get("stale_statistics", []):
            rows = _num(st.get("row_count")) or 0
            mods = _num(st.get("modification_counter")) or 0
            modified_pct = mods * 100 / rows if rows > 0 else 0
            updated = _dt(st.get("last_updated"))
            elapsed = _elapsed_seconds(self.now, updated)
            age = elapsed / 86400 if elapsed is not None else None
            if modified_pct >= mod_pct_limit and (age is None or age >= stale_days):
                self.add("PERF.STALE_STATS", "性能治理", "统计信息陈旧且数据变化较大", "medium",
                         f"{st.get('database_name')}/{st.get('schema_name')}.{st.get('table_name')}/{st.get('stats_name')}",
                         f"变化={modified_pct:.1f}% ，距更新={age:.1f}天" if age is not None else f"变化={modified_pct:.1f}% ，无更新时间",
                         "基数估算偏差可能导致错误连接顺序、内存授权或访问路径。",
                         "结合自动统计配置和业务窗口更新目标统计信息，避免无差别全库 FULLSCAN。")
        for idx in s.get("fragmented_indexes", []):
            frag = _num(idx.get("avg_fragmentation_in_percent")) or 0
            pages = _num(idx.get("page_count")) or 0
            if pages >= self.t.get("index_min_page_count", 1000) and frag >= self.t.get("index_fragmentation_warning_pct", 30):
                self.add("PERF.INDEX_FRAGMENTATION", "性能治理", "大型索引碎片较高", "low",
                         f"{idx.get('database_name')}/{idx.get('schema_name')}.{idx.get('table_name')}/{idx.get('index_name')}",
                         f"碎片={frag:.1f}% ，页数={pages:,.0f}", "范围扫描可能增加读取和预读成本；影响取决于存储与访问模式。",
                         "先确认该索引确有扫描负载，再选择 REORGANIZE/REBUILD，并同步评估日志、AG 和维护窗口。")
        for user in s.get("orphaned_users", []):
            self.add("SEC.ORPHANED_USER", "安全", "数据库存在孤立用户", "medium",
                     f"{user.get('database_name')}/{user.get('user_name')}", "数据库用户 SID 无对应服务器登录",
                     "可能影响访问或留下身份治理盲区。",
                     "确认账号用途后使用 ALTER USER ... WITH LOGIN 修复映射，或删除已废弃用户。")

    def jobs(self, s: dict[str, Any]) -> None:
        for job in s.get("failed_jobs", []):
            self.add("AGENT.JOB_FAILED", "SQL Agent", "SQL Agent 作业最近失败", "high", str(job.get("job_name", "未知作业")),
                     f"时间={job.get('run_datetime')}，消息={str(job.get('message', ''))[:160]}",
                     "备份、维护、同步或业务批处理可能未按计划完成。",
                     "核查作业步骤、代理账号、磁盘空间与依赖服务，修复后补跑并验证结果。")
        for job in s.get("agent_jobs", []):
            if not bool(job.get("enabled")):
                self.add("AGENT.JOB_DISABLED", "SQL Agent", "SQL Agent 作业处于禁用状态", "low", str(job.get("job_name", "未知作业")),
                         f"最近结果={job.get('last_run_status_desc')}，下次运行={job.get('next_run_datetime')}",
                         "若作业承担备份、维护或同步职责，禁用可能造成控制缺口。",
                         "确认禁用是否有审批和替代机制；废弃作业应归档后删除，必要作业应恢复并验证。")

    def ha(self, s: dict[str, Any]) -> None:
        for ag in s.get("availability_replicas", []):
            health = str(ag.get("synchronization_health_desc", "UNKNOWN")).upper()
            sync = str(ag.get("synchronization_state_desc", "UNKNOWN")).upper()
            queue = max(_num(ag.get("log_send_queue_mb")) or 0, _num(ag.get("redo_queue_mb")) or 0)
            if health != self.t.get("ag_sync_health_required", "HEALTHY") or sync not in {"SYNCHRONIZED", "SYNCHRONIZING"}:
                self.add("HA.AG_HEALTH", "高可用", "Always On 副本同步异常", "critical", str(ag.get("database_name", "AG")),
                         f"state={sync}, health={health}", "故障切换时可能无法满足 RPO/RTO。",
                         "立即检查副本连接、日志发送/重做、端点、磁盘和 SQL 错误日志。")
            elif queue >= self.t.get("ag_queue_warning_mb", 512):
                self.add("HA.AG_QUEUE", "高可用", "Always On 日志发送或重做队列偏大", "medium", str(ag.get("database_name", "AG")),
                         f"最大队列={queue:.1f} MB", "潜在数据丢失窗口或故障切换恢复时间增加。",
                         "结合队列增长趋势检查网络、辅助副本 I/O 与重做吞吐。")
        for row in s.get("database_mirroring", []):
            state = str(row.get("mirroring_state_desc", "")).upper()
            if state and state not in {"SYNCHRONIZED", "SYNCHRONIZING"}:
                self.add("HA.MIRRORING", "高可用", "数据库镜像状态异常", "high", str(row.get("database_name", "镜像数据库")),
                         f"state={state}，safety={row.get('mirroring_safety_level_desc')}", "镜像保护或故障切换能力可能失效。",
                         "检查端点、网络、主体/镜像日志和挂起原因，恢复后验证角色与故障切换流程。")
        for row in s.get("log_shipping", []):
            minutes = _num(row.get("minutes_since_last_action"))
            if minutes is None or minutes >= self.t.get("log_shipping_warning_minutes", 60):
                self.add("HA.LOG_SHIPPING", "高可用", "日志传送延迟或状态不可确认", "high", str(row.get("database_name", "日志传送")),
                         f"角色={row.get('monitor_role')}，距最近操作={minutes if minutes is not None else '未知'} 分钟，状态={row.get('status')}",
                         "备用库的恢复点可能落后，灾备 RPO 无法满足。",
                         "核查备份、复制、还原作业及共享路径，清除积压后执行灾备可用性验证。")

    def errors(self, s: dict[str, Any]) -> None:
        severe = [x for x in s.get("error_log_summary", []) if (_num(x.get("severity")) or 0) >= 20]
        if severe:
            self.add("ERRORLOG.SEVERE", "错误日志", "错误日志存在严重级别事件", "high", "SQL Server Error Log",
                     f"severity>=20 事件数={sum(int(_num(x.get('event_count')) or 1) for x in severe)}",
                     "可能表示连接终止、资源、I/O 或数据库一致性问题。",
                     "按时间线核对错误号、堆栈和 Windows 事件日志，确认是否仍在发生并制定根因修复。")
