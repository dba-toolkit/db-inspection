"""Deterministic, configuration-driven SQL Server inspection rules."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

from .module_catalog import (
    MODULE_BY_SNAPSHOT_KEY,
    classify_module,
    lookup_status,
    module_status_map,
)


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
    """把快照里的时间字符串解析成 datetime；无法解析返回 None。

    ⚠ 这个函数曾经**恒返回 None**：函数体只剩一句 `if not value: return None`，
    真正的解析代码被 `_elapsed_seconds` 插到了它后面、落在 `return` 之后，成了不可达代码。
    后果不是"少一个时间"，而是**所有"距今多久"的判据一起退化成"时间未知"**：
    有新鲜完整备份的库会被写成"无完整备份记录"，有 CHECKDB 记录的库会被写成
    "未取得成功 CHECKDB 时间"，而且 DB.CHECKDB 因为按"时间为空"取高档，严重级别恒为 high。
    这类缺陷不抛异常、不打红测试，只让交付出去的报告说出与事实相反的话。
    """
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _elapsed_seconds(now: datetime, then: datetime | None) -> float | None:
    if then is None:
        return None
    if now.tzinfo is not None and then.tzinfo is None:
        then = then.replace(tzinfo=now.tzinfo)
    elif now.tzinfo is None and then.tzinfo is not None:
        then = then.replace(tzinfo=None)
    return (now - then).total_seconds()


class RuleEngine:
    def __init__(self, config: dict[str, Any], now: datetime):
        self.t = config.get("thresholds", {})
        self.excluded_databases = {str(x).lower() for x in config.get("excluded_databases", [])}
        # 系统库降级（规则包 system_database_guard）：master/model/msdb 的「建议项」类问题
        # （备份、完整性检查、文件增长、所有权链）按 severity_cap 封顶。
        # 不加这一层，实测一例里 9 个 critical 有 6 个来自系统库（master/model/msdb 无备份），
        # 与业务库问题同级并列 —— 会把真正的业务库风险稀释掉。
        # tempdb 不在此列：它由 excluded_databases 整条不评价，而不是降级。
        guard = config.get("system_database_guard") or {}
        self._system_databases = {str(x).lower() for x in guard.get("databases", [])}
        self._system_severity_cap = str(guard.get("severity_cap") or "").lower()
        self._system_rules = {str(x) for x in guard.get("applies_to", [])}
        self.now = now
        self.findings: list[Finding] = []
        # 规则目录：本规则包**应当求值**的规则全集（规则包的 rules 段）。
        # 它是"通过"数量的分母 —— 引擎只能证明"这条触发了"或"这条我登记了没判"，
        # 剩下的必须靠目录里"本来应该有它"才能算通过。没有目录就只能吐 triggered，
        # 附录那张四行表会有三行在结构上恒为 0（SQL Server 长期如此，见 gap-plan §15.5①）。
        self.catalog: dict[str, dict] = dict(config.get("rules") or {})
        # 求值登记：rule_id -> {"status", "reason"}，只记「触发 / 未判断 / 不适用」三种；
        # 没登记的交给 rule_evaluations() 按采集模块状态判定（模块采到且没命中 = 通过）。
        self._registry: dict[str, dict] = {}
        # 本次 evaluate 的模块状态索引（{模块名: status}），由 evaluate() 填充。
        self._module_status: dict[str, str] = {}

    _SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}

    @staticmethod
    def _object_database(object_name: str) -> str:
        """从 object_name 取库名。

        规则里的 object_name 有两种形状：纯库名（"msdb"）与「库名/对象名」
        （文件增长规则写的是 "msdb/MSDBData"）。取第一段即可同时覆盖两种。
        """
        return str(object_name).split("/")[0].strip().lower()

    def _apply_system_db_guard(self, rule_id: str, object_name: str, severity: str) -> str:
        """把命中系统库的「建议项」降到 severity_cap。

        只对规则包 applies_to 列出的规则生效，不做全局兜底 ——
        像 MEM.CACHE_IMBALANCE（msdb 占用缓冲池过高）反映的是业务库缓冲不足，
        是真实问题，不能因为对象名是系统库就降级。
        """
        if not self._system_rules or not self._system_databases:
            return severity
        if rule_id not in self._system_rules:
            return severity
        if self._object_database(object_name) not in self._system_databases:
            return severity
        here = self._SEVERITY_ORDER.get(severity, 9)
        cap = self._SEVERITY_ORDER.get(self._system_severity_cap, 9)
        return self._system_severity_cap if here < cap else severity

    def add(self, rule_id: str, category: str, title: str, severity: str,
            object_name: str, evidence: str, impact: str, recommendation: str) -> None:
        # 系统库降级只做在 add() 这一处 —— 它是 finding 的唯一出口，
        # 放在这里就不存在"某个规则漏改"的可能。
        severity = self._apply_system_db_guard(rule_id, object_name, severity)
        priority = "P1" if severity in {"critical", "high"} else "P2" if severity == "medium" else "P3"
        self.findings.append(Finding(rule_id, category, title, severity, priority,
                                     object_name, evidence, impact, recommendation))
        # 触发优先：同一条规则若先被登记为未判断、随后命中，以命中为准。
        self._registry[rule_id] = {"status": "triggered", "reason": ""}

    def gap(self, rule_id: str, reason: str) -> None:
        """登记「未判断」：我想判这条规则，但判据输入没拿到（没跑起来）。

        与「不适用」的区别在于成因：这里是**缺证据**（采集包缺该字段、或该模块整版没采），
        下游不得折算成通过。已有登记时不覆盖 —— 触发优先。
        """
        self._registry.setdefault(rule_id, {"status": "not_evaluated", "reason": reason})

    def na(self, rule_id: str, reason: str) -> None:
        """登记「不适用」：本实例形态或本规则包配置不支持这项检查。

        例：未部署 Always On、未配置日志传送、规则包未要求备份压缩。
        """
        self._registry.setdefault(rule_id, {"status": "not_applicable", "reason": reason})

    def _module_ok(self, snapshot_key: str) -> bool:
        """该快照键对应的采集模块是否**成功采到**（"采到但结果为空"也算成功）。

        区分"这个对象确实不存在"和"这项根本没采"只能靠模块状态 —— 快照键在
        normalize_snapshot 里被 setdefault 补齐，两者的形状一模一样。
        """
        module = MODULE_BY_SNAPSHOT_KEY.get(snapshot_key)
        if module is None:
            return False
        return classify_module(lookup_status(self._module_status, module)) == "ok"

    def evaluate(self, s: dict[str, Any]) -> list[dict[str, Any]]:
        self._registry = {}
        self._module_status = module_status_map(s)
        self.instance(s)
        self.databases(s)
        self.backups(s)
        self.no_backup_databases(s)
        self.files(s)
        self.capacity_and_integrity(s)
        self.performance(s)
        self.session_activity(s)
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

    # ------------------------------------------------------------------
    # 求值结果（附录「规则执行状态」的数据源）
    # ------------------------------------------------------------------
    def rule_evaluations(self) -> list[dict[str, Any]]:
        """产出四态完整的规则求值列表。

        判定顺序（前者优先）：
          1. 引擎已登记：add -> triggered / gap -> not_evaluated / na -> not_applicable
          2. 依赖的采集模块整版不存在或采失败 -> not_evaluated（缺证据）
          3. 依赖的采集模块被有意跳过 / 本实例不支持 -> not_applicable
          4. 其余 -> passed（模块采到了、规则跑完、没命中阈值）

        只有第 4 类才是真正的「通过」。旧实现只吐 triggered，于是这条永远算不出来。
        """
        rows: list[dict[str, Any]] = []
        for rule_id, meta in self.catalog.items():
            registered = self._registry.get(rule_id)
            if registered:
                status = registered["status"]
                reason = registered.get("reason") or ""
            else:
                status, reason = self._infer_status(meta)
            rows.append({
                "rule_id": rule_id,
                "category": meta.get("category"),
                "status": status,
                "reason": reason,
                "severity_if_triggered": meta.get("severity"),
                "evidence_refs": [],
                "confidence": 1.0,
            })
        return rows

    def _infer_status(self, meta: dict[str, Any]) -> tuple[str, str]:
        """给"引擎没登记"的规则定归属 —— 全靠它声明的 required_modules。"""
        modules = [str(x) for x in (meta.get("required_modules") or [])]
        if not modules:
            return "not_evaluated", "规则包未声明该规则依赖的采集模块"
        missing = [
            name for name in modules
            if classify_module(lookup_status(self._module_status, name)) == "unavailable"
        ]
        if missing:
            return "not_evaluated", f"未采集到判据输入（模块：{', '.join(missing)}）"
        skipped = [
            name for name in modules
            if classify_module(lookup_status(self._module_status, name)) == "not_applicable"
        ]
        if skipped:
            return "not_applicable", f"采集端未启用或本实例不支持（模块：{', '.join(skipped)}）"
        return "passed", ""

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
        maxdop_limit = self.t.get("max_recommended_maxdop", 8)
        cpu_count = _num(info.get("cpu_count"))
        if maxdop is not None and maxdop > maxdop_limit:
            self.add("INSTANCE.MAXDOP", "实例配置", "MAXDOP 高于通用建议值", "medium", "MAXDOP",
                     f"当前 MAXDOP={maxdop:.0f}", "高并发场景可能出现并行线程争用和查询波动。",
                     "结合 NUMA、CPU 核数和工作负载测试，将 MAXDOP 调整到合适范围；不要直接套用固定值。")
        elif (maxdop == 0 and cpu_count is not None and cpu_count > maxdop_limit
                and self.t.get("flag_unlimited_maxdop_on_many_cores", True)):
            # 0 是"不限制并行度"（用满所有核），不是"没有配置"。
            # 旧判据只看 `> 8`，于是 64 核实例上的 MAXDOP=0 一路绿灯 ——
            # 而这恰好是现场报告 3.1 点名要改的那个取值。
            # 核少的实例（<= 建议上限）保持 0 是合理的，所以加了核数前提。
            self.add("INSTANCE.MAXDOP", "实例配置", "MAXDOP 未限制并行度", "medium", "MAXDOP",
                     f"MAXDOP=0（不限制并行度），实例 CPU 核数={cpu_count:.0f}，通用建议上限={maxdop_limit}",
                     "单个并行查询可以占满所有调度器，高并发时出现并行线程争用与响应时间抖动。",
                     "按 NUMA 布局与峰值负载测试后设置有限值（通常不超过单 NUMA 节点核数，且不超过 8）。")
        cost = _num((cfg.get("cost threshold for parallelism") or {}).get("value_in_use"))
        if cost is not None and cost < self.t.get("min_cost_threshold_parallelism", 25):
            self.add("INSTANCE.COST_THRESHOLD", "实例配置", "并行开销阈值偏低", "medium", "cost threshold for parallelism",
                     f"当前值={cost:.0f}", "较轻查询也可能并行化，增加并发环境调度压力。",
                     "基于 Query Store/高峰工作负载依次提高并行开销阈值并观察回归。")
        self._startup_parameters(s)
        self._version_support(s)
        self._services(s)

    # 依赖服务识别词表：这些服务的启停不影响实例可用性，因此不能与引擎服务同级告警。
    # 中文实例的服务名是「SQL Server 代理 (实例名)」，英文是 "SQL Server Agent"，
    # 两者都要覆盖；全文/浏览器/SSIS 同理。
    _DEPENDENT_SERVICE_HINTS = ("代理", "agent", "full-text", "fulltext", "filter daemon",
                                "launcher", "browser", "ssis")

    def _is_engine_service(self, svc: dict[str, Any]) -> bool:
        """这个服务是不是数据库引擎本体。

        首选 filename（唯一可靠判据）：引擎是 sqlservr.exe，
        代理是 SQLAGENT.EXE、全文是 fdlauncher.exe、浏览器是 sqlbrowser.exe。
        filename 未采到时退回服务名 —— 依赖服务名一定带 _DEPENDENT_SERVICE_HINTS 里的词，
        而引擎服务名是 `SQL Server (实例名)`。
        """
        binary = str(svc.get("filename") or "").lower()
        if binary:
            return "sqlservr.exe" in binary
        name = str(svc.get("servicename") or "").lower()
        if any(hint in name for hint in self._DEPENDENT_SERVICE_HINTS):
            return False
        return name.startswith("sql server")

    def _services(self, s: dict[str, Any]) -> None:
        """服务状态与启动类型（sys.dm_server_services）。

        ⚠ 本表会同时返回引擎、SQL Server 代理、全文筛选器启动器、浏览器等若干行，
        而**只有引擎停止才等于"实例不可用"**。旧版对任何服务 status != Running 一律写
        critical「SQL Server 服务未处于运行状态」—— 实测一例里引擎 Running、代理
        Manual+Stopped（微软出厂默认配置），却被报成实例停机；同一循环还把全文服务的
        Manual 启动类型报成"启动类型非自动"（Manual 正是全文服务的标准配置）。
        代理停止只在实例确实有作业时才有后果，所以要先看 agent_jobs 再决定报不报。

        旧版报告 2.2 要的「软件目录（Binn）」也在这张表里（filename），一并进证据。
        """
        jobs = s.get("agent_jobs") or []
        for svc in s.get("server_services", []):
            name = str(svc.get("servicename") or "SQL Server 服务")
            startup = str(svc.get("startup_type_desc") or "").upper()
            status = str(svc.get("status_desc") or "").upper()
            account = svc.get("service_account") or "未采集"
            binary = svc.get("filename") or "未采集"
            evidence = f"status={status or '未知'}，startup_type={startup or '未知'}，账号={account}，bin={binary}"
            if not self._is_engine_service(svc):
                # 依赖服务：只有"代理停了、且实例确实有作业"才说明后果真实存在。
                # 无作业时代理停止属于未启用该功能，不是故障；其余依赖服务同理（无证据不报）。
                if status and status != "RUNNING" and jobs and ("代理" in name or "agent" in name.lower()):
                    self.add("INSTANCE.SERVICE_STARTUP", "实例配置", "SQL Server 代理服务未运行", "medium", name,
                             f"{evidence}；实例存在 {len(jobs)} 个作业",
                             "代理停止后作业调度与维护计划不会执行，备份、索引维护等自动化任务静默失效。",
                             "确认是否有意停用；需要作业调度时恢复启动，并核查作业与备份任务最近一次执行结果。")
                continue
            if status and status != "RUNNING":
                self.add("INSTANCE.SERVICE_STARTUP", "实例配置", "SQL Server 服务未处于运行状态", "critical", name,
                         evidence,
                         "数据库引擎未运行，实例当前不可用。",
                         "确认是否为人工停用；核查启动类型、依赖服务与错误日志后按变更流程恢复，并在恢复后验证业务连通。")
            elif startup and startup != "AUTOMATIC" and self.t.get("require_service_autostart", True):
                self.add("INSTANCE.SERVICE_STARTUP", "实例配置", "SQL Server 服务启动类型非自动", "medium", name,
                         f"startup_type={startup}，账号={account}，bin={binary}",
                         "主机重启后数据库引擎不会随系统启动，可能长时间不可用且无人察觉。",
                         "与变更管理确认是否有意为之；无特殊原因时改为自动启动，并在重启后验证清单中加入本服务。")

    def _startup_parameters(self, s: dict[str, Any]) -> None:
        """启动参数族（sys.configurations 白名单内的键）。

        与 INSTANCE.MAX_MEMORY / MAXDOP / COST_THRESHOLD 分开写，是因为那三条是"值超标"，
        这三条是"该开的没开"——判定方向相反，混在一个方法里迟早会把 `is not None` 写成真值判断。
        注意：0 是这里的**有效问题值**（blocked process threshold=0 表示永不产生阻塞报告），
        所以每一处都用 `is not None` 兜底，不能用 `if value:`。
        """
        cfg = {str(x.get("name", "")).lower(): x for x in s.get("configurations", [])}

        adhoc = cfg.get("optimize for ad hoc workloads")
        if adhoc is None:
            # 采集端白名单里没有这一项 = 没证据，不是"没开启"。
            self.gap("INSTANCE.ADHOC_WORKLOADS", "采集包未包含 optimize for ad hoc workloads 配置项")
        elif (_num(adhoc.get("value_in_use")) == 0
                and self.t.get("require_adhoc_workloads_optimization", True)):
            self.add("INSTANCE.ADHOC_WORKLOADS", "实例配置", "未启用即席工作负载优化", "medium",
                     "optimize for ad hoc workloads",
                     "value_in_use=0", "一次性即席语句的计划会常驻计划缓存，挤压复用计划的空间。",
                     "启用 optimize for ad hoc workloads（联机生效，不影响现有计划），并观察计划缓存大小变化。")

        bpt = cfg.get("blocked process threshold (s)")
        # 该键在 collector < 1.3.0 的包里根本不存在（采集端白名单没收录）——
        # 采不到就不判、不能当成 0 来告警；同理**也不能算通过**：没采到就是没证据。
        if bpt is None:
            self.gap("INSTANCE.BLOCKED_PROC_THRESHOLD",
                     "采集包未包含 blocked process threshold 配置项")
        else:
            bpt_value = _num(bpt.get("value_in_use"))
            floor = self.t.get("blocked_process_threshold_min_seconds", 30)
            if bpt_value is not None and bpt_value < floor:
                self.add("INSTANCE.BLOCKED_PROC_THRESHOLD", "实例配置", "未配置阻塞进程阈值报告", "low",
                         "blocked process threshold (s)",
                         f"value_in_use={bpt_value:.0f}（建议 >= {floor}）",
                         "超过阈值的阻塞不会写入错误日志，事后只能靠现场抓包或历史等待统计倒推。",
                         f"设置为 {floor} 秒并配置 blocked process report，让长阻塞自动留痕。")

        compression = cfg.get("backup compression default")
        if not self.t.get("require_backup_compression", False):
            # 规则包没要求这项检查 —— 没评价的项不得算作通过。
            self.na("INSTANCE.BACKUP_COMPRESSION", "规则包未要求检查备份压缩默认值")
        elif compression is None:
            self.gap("INSTANCE.BACKUP_COMPRESSION",
                     "采集包未包含 backup compression default 配置项")
        elif _num(compression.get("value_in_use")) == 0:
            self.add("INSTANCE.BACKUP_COMPRESSION", "实例配置", "默认未启用备份压缩", "low",
                     "backup compression default",
                     "value_in_use=0", "备份文件更大、备份窗口更长，且跨网络传输成本更高。",
                     "评估 CPU 余量后启用 backup compression default；已使用压缩备份的关键库无需重复处理。")

    def _version_support(self, s: dict[str, Any]) -> None:
        """版本生命周期。

        现场报告 1.1 的「数据库版本」只写版本号，读者无法判断它还能不能拿到安全补丁 ——
        一份 2014 SP2 的报告和一份 2019 的报告在这一栏长得一模一样。
        生命周期表放在规则包里（官方日期会变，不该写死在代码里）；查不到对应主版本就**不判**，
        因为"我不认识这个版本"和"这个版本已经过期"是两件事。
        """
        info = s.get("instance", {})
        raw_version = str(info.get("product_version") or "").strip()
        if not raw_version:
            self.gap("INSTANCE.VERSION_SUPPORT", "采集包未包含 product_version")
            return
        parts = raw_version.split(".")
        if len(parts) < 2:
            self.gap("INSTANCE.VERSION_SUPPORT", f"版本号 {raw_version} 无法解析主次版本")
            return
        try:
            major, minor = int(parts[0]), int(parts[1])
        except ValueError:
            self.gap("INSTANCE.VERSION_SUPPORT", f"版本号 {raw_version} 主次版本不是数字")
            return
        table = self.t.get("sqlserver_extended_support_end", {}) or {}
        end_text = table.get(f"{major}.{minor}") or table.get(str(major))
        if not end_text:
            # "我不认识这个版本"和"这个版本已经过期"是两件事 —— 前者登记未判断。
            self.gap("INSTANCE.VERSION_SUPPORT", f"生命周期表未收录版本 {raw_version}")
            return
        try:
            end_date = datetime.strptime(str(end_text)[:10], "%Y-%m-%d").replace(
                tzinfo=self.now.tzinfo)
        except ValueError:
            return
        remaining = (end_date - self.now).days
        level = info.get("product_level") or ""
        version_label = f"{raw_version} ({level})" if level else raw_version
        if remaining < 0:
            self.add("INSTANCE.VERSION_SUPPORT", "实例配置", "SQL Server 版本已过扩展支持", "high",
                     version_label,
                     f"扩展支持已于 {end_date.date().isoformat()} 结束（距今 {-remaining} 天）",
                     "不再获得安全更新与功能修复，已知漏洞只能靠架构隔离缓解。",
                     "制定版本升级或迁移计划；过渡期内至少收紧网络暴露面与账号权限，并记录风险接受。")
        elif remaining <= self.t.get("sqlserver_eol_warning_days", 180):
            self.add("INSTANCE.VERSION_SUPPORT", "实例配置", "SQL Server 版本临近扩展支持终点", "medium",
                     version_label,
                     f"扩展支持将于 {end_date.date().isoformat()} 结束（剩余 {remaining} 天）",
                     "到期后不再有安全更新，临时再升级会赶上变更窗口最紧张的时候。",
                     "把版本升级排入年度计划，提前完成兼容性评估与回退演练。")

    def session_activity(self, s: dict[str, Any]) -> None:
        """按登录名聚合的会话资源占用（sys.dm_exec_sessions）。

        connection_stats 只回答"谁连着"，这一条回答"谁在耗"。现场报告 1.9 里
        某业务账号 1265 个会话、累计 CPU 极高，就是这一条要说的东西。
        只在分布**明显倾斜**时提示：单账号占比高本身不等于故障（单应用实例很常见），
        所以还要求总 CPU 和会话数都过了地板值，otherwise 一个小实例上的噪音数据就能触发。
        """
        rows = [r for r in s.get("session_activity", [])]
        if not rows:
            return
        by_login: dict[str, dict[str, float]] = {}
        for row in rows:
            login = str(row.get("login_name") or "Unknown")
            acc = by_login.setdefault(login, {"cpu": 0.0, "sessions": 0.0, "reads": 0.0})
            acc["cpu"] += _num(row.get("total_cpu_ms")) or 0
            acc["sessions"] += _num(row.get("session_count")) or 0
            acc["reads"] += _num(row.get("total_physical_reads")) or 0
        total_cpu = sum(v["cpu"] for v in by_login.values())
        floor = self.t.get("session_cpu_min_total_ms", 600000)
        if total_cpu < floor:
            return
        login, acc = max(by_login.items(), key=lambda kv: kv[1]["cpu"])
        share = acc["cpu"] * 100.0 / total_cpu
        if (share >= self.t.get("session_cpu_share_warning_pct", 80)
                and acc["sessions"] >= self.t.get("session_min_session_count", 10)):
            self.add("SESSION.RESOURCE_HOG", "性能与会话", "单一登录名占用绝大部分会话资源", "low",
                     login,
                     f"累计 CPU 占比 {share:.1f}%（{acc['cpu']:.0f} ms / 全部 {total_cpu:.0f} ms），"
                     f"会话数 {acc['sessions']:.0f}，累计物理读 {acc['reads']:.0f}",
                     "资源集中在单账号上，该账号的慢查询会直接决定整个实例的响应时间。",
                     "按账号拉取高消耗 SQL 与等待类型，先定位语句再考虑限流或资源调控器。")

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
            # 这两处原本写死 {"master","model","msdb","tempdb"}，与规则包里的
            # excluded_databases(tempdb) / system_database_guard(master/model/msdb) 重复维护。
            # 合并后与原硬编码完全等价，但系统库清单从此只有规则包一个来源。
            if (name.lower() not in (self._system_databases | self.excluded_databases)
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
        require_encryption = self.t.get("require_backup_encryption", False)
        if not require_encryption:
            # 规则包没要求这项检查 —— 没评价的项不得算作通过。
            self.na("BACKUP.ENCRYPTION", "规则包未要求检查备份加密")
        # 「从未备份」由 no_backup_databases() 单独负责（BACKUP.NO_BACKUP），这里只管
        # "有记录但超期" —— 两条规则都覆盖"无记录"会让同一个库报出两条 critical（实测 4 个库报 8 条）。
        # 兜底：该库不在 no_backup_databases 名单里时（两模块口径不一致、或该模块没采到），
        # 这里仍要报，避免用"去重"换来"漏报"。
        no_backup_names = {str(r.get("database_name", "")).lower()
                           for r in s.get("no_backup_databases", [])}
        for row in s.get("backups", []):
            name = str(row.get("database_name", "未知数据库"))
            full = _dt(row.get("last_full_backup"))
            elapsed = _elapsed_seconds(self.now, full)
            hours = elapsed / 3600 if elapsed is not None else None
            if hours is not None and hours > full_limit:
                self.add("BACKUP.FULL", "备份恢复", "完整备份超期或缺失", "critical", name,
                         f"完整备份距今 {hours:.1f} 小时",
                         "介质故障或逻辑事故后可能无法按目标恢复。",
                         "立即核查备份任务、落盘位置与可恢复性，并完成一次受控还原验证。")
            elif hours is None and name.lower() not in no_backup_names:
                self.add("BACKUP.FULL", "备份恢复", "完整备份超期或缺失", "critical", name,
                         "无完整备份记录（未被无备份数据库清单覆盖）",
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
            if full and require_encryption and not row.get("full_encryptor_type"):
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
        rows = s.get("no_backup_databases", [])
        if not rows and not self._module_ok("no_backup_databases"):
            # 本条已成为"从未完整备份"的唯一出口（BACKUP.FULL 只管超期），
            # 模块没采到必须显式登记未判断 —— 否则"没采到"会被读成"没有库从未备份"。
            self.gap("BACKUP.NO_BACKUP", "无备份数据库模块未采集到结果")
            return
        for row in rows:
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
            if self._module_ok("replication"):
                # 采到了但没有任何复制记录 = 本实例未配置复制 → 不适用，不是"通过"。
                for rule_id in ("HA.REPLICATION", "HA.REPLICATION_CONFIG",
                                "HA.REPLICATION_ERROR", "HA.REPLICATION_AGENT"):
                    self.na(rule_id, "本实例未配置复制拓扑")
            return
        # 采集端 v1.2 起把复制信息压成单一结果集（record_type 区分发布/订阅/代理/错误），
        # 新形状走独立判据，旧形状保持原逻辑 —— 历史采集包的解释结果不变。
        if any("record_type" in row for row in rep):
            self.replication_collected(rep)
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

    def replication_collected(self, rep: list[dict[str, Any]]) -> None:
        """v1.2 采集形状：REPL_ROLE / PUBLICATION / SUBSCRIPTION / REPL_ERROR / *_HISTORY。

        旧形状只认 publication_name + status_desc=INACTIVE，于是「日志读取器代理失败」
        和「分发库有错误记录」这两类最要命的情况在规则层完全没有出口 —— 报告只能靠人
        翻错误日志（现场那份报告写的就是「可能是发布导致，需开发检查」）。
        """
        for row in rep:
            if str(row.get("record_type")) != "REPL_ERROR":
                continue
            count = _num(row.get("error_count")) or 0
            self.add("HA.REPLICATION_ERROR", "高可用", "分发库存在复制错误记录", "high",
                     str(row.get("name") or "replication"),
                     f"error={row.get('error_id')}，近7天 {count:.0f} 次，首次 {row.get('last_action_time') or '未知'}",
                     "复制链路存在未消除的错误，订阅端数据可能停止更新，事务日志与分发库持续堆积。",
                     "按 error_code 定位来源代理：日志读取器失败先查发布库长事务与 sp_repldone/sp_replcounters 权限，"
                     "分发代理失败先查订阅端连通性与分发清理节奏。")
        for row in rep:
            record_type = str(row.get("record_type") or "")
            if not record_type.endswith("_HISTORY"):
                continue
            if str(row.get("status_desc") or "").upper() != "FAILED":
                continue
            failures = _num(row.get("error_count")) or 0
            self.add("HA.REPLICATION_AGENT", "高可用", f"复制代理执行失败（{record_type}）", "high",
                     f"{record_type} agent {row.get('name')}",
                     f"失败 {failures:.0f} 次，最近 {row.get('last_action_time') or '未知'}",
                     "代理失败期间变更不会传递，延迟持续累积，长期不清会撑爆分发库。",
                     "查看代理作业历史与错误详情，修复后确认延迟归零并清理分发库。")
        for row in rep:
            if str(row.get("record_type")) != "PUBLICATION":
                continue
            self.add("HA.REPLICATION_CONFIG", "高可用", "复制存在发布配置", "info",
                     str(row.get("name") or ""),
                     f"库={row.get('source_db')}，publication_type={row.get('status_desc')}",
                     "复制链条已配置。",
                     "持续监控代理作业状态、复制错误与延迟。")

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
        self._job_overdue(s)

    def _job_overdue(self, s: dict[str, Any]) -> None:
        """计划时间已过却未运行的作业。

        `job_overdue_hours` 原本是规则包里的死键（没有任何代码请求）—— 一个"改了不会报错、
        也不会有反应"的旋钮。这里按声明的键接线：next_run_datetime 为空说明作业没有计划，
        不是"过期"，必须跳过（否则每个按需作业都会变成风险项）。
        """
        horizon = self.t.get("job_overdue_hours", 36)
        for job in s.get("agent_jobs", []):
            if not job.get("enabled"):
                continue
            next_run = _dt(job.get("next_run_datetime"))
            if next_run is None:
                continue
            # 必须走 _elapsed_seconds：采集包里的时间戳可能不带时区（agent_datetime 直出），
            # 而 self.now 带时区，直接相减会 TypeError。这个函数就是为这件事存在的。
            elapsed = _elapsed_seconds(self.now, next_run)
            if elapsed is None:
                continue
            overdue_hours = elapsed / 3600
            if overdue_hours >= horizon:
                name = str(job.get("job_name") or "未知作业")
                self.add("AGENT.JOB_OVERDUE", "SQL Agent", "作业已过计划时间未运行", "medium", name,
                         f"计划时间 {next_run.isoformat(timespec='seconds')}，已超期 {overdue_hours:.1f} 小时",
                         "按业务日历应该跑过的作业没有跑，依赖它的下游数据可能已过期或缺失。",
                         "确认 Agent 服务、作业计划与执行历史，修复后补跑并核对下游数据完整性。")

    def _ag_replicas(self, s: dict[str, Any]) -> None:
        """Always On 副本判据。

        这里刻意用 elif 链而不是四个独立 if：同一个 (AG, 数据库) 上同时"同步异常 + 队列大 +
        延迟高"是同一件事的三种说法，独立触发会在风险台账里堆出三条同源条目。

        **分支顺序是判据的一部分，不能随意调换。** `synchronization_health_desc` 已经把提交
        模式揉进去了（官方定义：sync 提交副本 SYNCHRONIZING 记 PARTIALLY_HEALTHY，async 提交
        副本 SYNCHRONIZING 记 HEALTHY）。所以若把"健康度不等于 HEALTHY"放在前面，
        "同步提交 + SYNCHRONIZING"这个最需要单独说清的情形会被通用分支全部截走，
        下面按 availability_mode_desc 区分的分支就成了永远走不到的死代码。
        先判模式 → 再判健康度 → 再判延迟/队列。
        """
        lag_limit = self.t.get("ag_replica_lag_warning_seconds", 30)
        if not s.get("availability_replicas") and self._module_ok("availability_replicas"):
            # 采到了、一个副本都没有 = 本实例确实没部署 Always On → 不适用，不是"通过"。
            for rule_id in ("HA.AG_CONNECT_ERROR", "HA.AG_SYNC_MODE", "HA.AG_HEALTH",
                            "HA.AG_LAG", "HA.AG_QUEUE"):
                self.na(rule_id, "本实例未部署 Always On 可用性组")
            return
        # 连接错误是副本级字段（每个数据库行重复一份），按副本去重后再判，避免同一副本报 N 次。
        seen_replicas: set[str] = set()
        for ag in s.get("availability_replicas", []):
            replica = str(ag.get("replica_server_name") or "副本")
            if replica in seen_replicas:
                continue
            seen_replicas.add(replica)
            err_no = _num(ag.get("last_connect_error_number"))
            if err_no:
                self.add("HA.AG_CONNECT_ERROR", "高可用", "Always On 副本存在连接错误", "high", replica,
                         f"error={err_no:.0f}，{ag.get('last_connect_error_description') or '无描述'}，"
                         f"时间={ag.get('last_connect_error_timestamp') or '未知'}",
                         "副本间无法建链时日志不再传递，辅助副本会持续落后并最终影响故障切换。",
                         "按错误号检查端点、端口、防火墙、证书/登录与副本服务状态，恢复后确认队列归零。")
        for ag in s.get("availability_replicas", []):
            target = str(ag.get("database_name") or ag.get("availability_group") or "AG")
            health = str(ag.get("synchronization_health_desc", "UNKNOWN")).upper()
            sync = str(ag.get("synchronization_state_desc", "UNKNOWN")).upper()
            mode = str(ag.get("availability_mode_desc") or "").upper()
            role = str(ag.get("role_desc") or "").upper()
            queue = max(_num(ag.get("log_send_queue_mb")) or 0, _num(ag.get("redo_queue_mb")) or 0)
            lag = _num(ag.get("replica_lag_seconds"))
            if lag is None:
                lag = _num(ag.get("secondary_lag_seconds"))
            # 注意别写成 `"SYNCHRONOUS" in mode`：'ASYNCHRONOUS_COMMIT' 里含子串 'SYNCHRONOUS'，
            # 那样会把异步副本（SYNCHRONIZING 本就是常态）错判成"同步提交未追平"。
            sync_commit = mode.startswith("SYNCHRONOUS")
            if sync_commit and sync == "SYNCHRONIZING" and role != "PRIMARY":
                # 异步提交下 SYNCHRONIZING 是常态（官方定义里记 HEALTHY），同步提交下才是真延迟。
                self.add("HA.AG_SYNC_MODE", "高可用", "同步提交副本未能追平主副本", "high", target,
                         f"提交模式={mode}，同步状态={sync}，健康度={health}，"
                         f"延迟={lag if lag is not None else '未知'} 秒，队列={queue:.1f} MB",
                         "同步提交要求辅助副本落盘后才向业务返回成功，状态长期停在 SYNCHRONIZING 说明"
                         "辅助副本 I/O 或网络跟不上提交速率；此时提交延迟已转嫁给业务，"
                         "且故障切换无法保证零丢失。",
                         "对比 last_received_time / last_hardened_time / last_redone_time 三段，"
                         "判断瓶颈在日志发送（网络/带宽）还是重做（辅助副本磁盘），再针对性扩容。")
            elif health != self.t.get("ag_sync_health_required", "HEALTHY") or sync not in {"SYNCHRONIZED", "SYNCHRONIZING"}:
                self.add("HA.AG_HEALTH", "高可用", "Always On 副本同步异常", "critical", target,
                         f"state={sync}, health={health}, mode={mode or '未采集'}",
                         "故障切换时可能无法满足 RPO/RTO。",
                         "立即检查副本连接、日志发送/重做、端点、磁盘和 SQL 错误日志。")
            elif lag is not None and lag >= lag_limit:
                self.add("HA.AG_LAG", "高可用", "Always On 副本延迟超过阈值", "high", target,
                         f"延迟={lag:.0f} 秒（阈值 {lag_limit:.0f}），提交模式={mode or '未采集'}，队列={queue:.1f} MB",
                         "故障切换会丢失窗口内的数据，RPO 无法满足。",
                         "结合 last_sent_time/last_received_time/last_hardened_time/last_redone_time 逐段定位延迟环节。")
            elif queue >= self.t.get("ag_queue_warning_mb", 512):
                self.add("HA.AG_QUEUE", "高可用", "Always On 日志发送或重做队列偏大", "medium", target,
                         f"最大队列={queue:.1f} MB", "潜在数据丢失窗口或故障切换恢复时间增加。",
                         "结合队列增长趋势检查网络、辅助副本 I/O 与重做吞吐。")

    def ha(self, s: dict[str, Any]) -> None:
        self._ag_replicas(s)
        if not s.get("database_mirroring") and self._module_ok("database_mirroring"):
            # 采到了但没有任何镜像会话 = 本实例未配置数据库镜像 → 不适用。
            self.na("HA.MIRRORING", "本实例未配置数据库镜像")
        for row in s.get("database_mirroring", []):
            state = str(row.get("mirroring_state_desc", "")).upper()
            if state and state not in {"SYNCHRONIZED", "SYNCHRONIZING"}:
                self.add("HA.MIRRORING", "高可用", "数据库镜像状态异常", "high", str(row.get("database_name", "镜像数据库")),
                         f"state={state}，safety={row.get('mirroring_safety_level_desc')}", "镜像保护或故障切换能力可能失效。",
                         "检查端点、网络、主体/镜像日志和挂起原因，恢复后验证角色与故障切换流程。")
        if not s.get("log_shipping") and self._module_ok("log_shipping"):
            # 采到了但没有日志传送记录 = 本实例未配置日志传送 → 不适用。
            self.na("HA.LOG_SHIPPING", "本实例未配置日志传送")
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
