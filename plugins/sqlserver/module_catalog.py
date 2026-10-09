"""SQL Server 采集模块目录 —— 「快照键 / 规则 → 采集模块」的唯一真值来源。

为什么必须要有这个目录
----------------------
判定一条规则该落「通过」还是「未判断」，唯一可靠的依据是采集端在
``collection.module_status`` 里登记的状态。

**不能看快照键是否存在。** ``package_adapter.normalize_snapshot`` 对 30+ 个键做
``setdefault(key, [])``，把「这个键根本没采」和「采了、结果为空」抹成同一个形状；
「采集失败被吞掉」和「这台实例确实没有该对象」在快照里长得一模一样。
拿它当判据，会把"没采到"读成"检查通过" —— 正是 AGENTS.md 禁止的
「把未知写成 0/正常」。

模块名会漂移
------------
采集端改过名（``无主键表（深度）`` → ``无主键表``、``大表统计（深度）`` → ``大表统计``、
``复制配置`` → ``复制拓扑``）。旧采集包里仍是旧名，查状态时必须把旧名一起查，
否则老包上这些规则会被误判成"未采集"。旧名清单与 ``tests/test_sqlserver_module_names.py``
共用（测试从本模块 import，不再各存一份）。

快照键 → 模块名的映射来自 ``inspection/sqlserver_inspection.ps1`` 的
``Invoke-CollectionModule '<模块名>' '<快照键>'`` 与实际注册点，改动采集端时需同步。
"""
from __future__ import annotations

from typing import Any

# 采集端改名后保留的旧名（旧采集包里真实注册过的名字，不是凭空的别名）。
LEGACY_MODULE_ALIASES: dict[str, tuple[str, ...]] = {
    "无主键表": ("无主键表（深度）",),
    "大表统计": ("大表统计（深度）",),
    "复制拓扑": ("复制配置",),
}

# 快照顶层键 → 采集端登记的模块名。
MODULE_BY_SNAPSHOT_KEY: dict[str, str] = {
    "instance": "实例基础信息",
    "databases": "数据库清单与完整性",
    "backups": "备份状态",
    "backup_history": "备份历史（近7天）",
    "no_backup_databases": "无备份数据库",
    "restore_history": "还原验证历史",
    "database_rcsi": "数据库RCSI状态",
    "database_summary": "数据库大小汇总",
    "database_files": "数据库文件与IO",
    "volumes": "存储卷容量",
    "log_space": "事务日志空间",
    "vlf_summary": "VLF摘要",
    "suspect_pages": "可疑页面",
    "wait_stats": "等待统计",
    "blocking": "阻塞链",
    "active_requests": "活动请求",
    "connection_stats": "连接数统计",
    "session_activity": "会话资源占用",
    "top_queries": "高消耗SQL",
    "top_queries_logical_reads": "高逻辑读SQL",
    "top_queries_executions": "高执行次数SQL",
    "top_queries_physical_reads": "高物理读SQL",
    "missing_indexes": "缺失索引建议",
    "unused_indexes": "未使用索引",
    "configurations": "实例配置",
    "server_services": "SQL Server 服务与启动参数",
    "buffer_pool": "缓冲池使用分布",
    "memory_status": "进程内存状态",
    "memory_clerks": "内存组件分布",
    "system_memory": "系统内存状态",
    "cpu_by_database": "CPU按数据库分布",
    "failed_jobs": "SQL Agent失败作业",
    "agent_jobs": "SQL Agent作业清单",
    "job_steps": "SQL Agent作业步骤",
    "ha_status": "高可用状态",
    "availability_replicas": "Always On",
    "database_mirroring": "数据库镜像",
    "log_shipping": "日志传送",
    "replication": "复制拓扑",
    "error_log_summary": "错误日志摘要",
    "tables_without_pk": "无主键表",
    "large_tables": "大表统计",
    "stale_statistics": "陈旧统计信息（深度）",
    "fragmented_indexes": "索引碎片（深度）",
    "orphaned_users": "孤立用户（深度）",
    "tempdb": "TempDB",
    "security": "安全与权限",
    "performance_samples": "性能采样",
    "os_metrics": "OS 指标",
}

# 采集端登记状态 → 它意味着"这条规则的输入算不算拿到了"。
# success / success_no_data：SQL 跑通（后者结果为空）→ 输入可用。
# failed / permission_denied / timeout / error：想采但没采到 → 输入不可用（未判断）。
# skipped / unsupported / not_applicable / not_enabled：有意不采或本实例不具备该特性 → 不适用。
MODULE_STATUS_OK = frozenset({"success", "success_no_data", "ok"})
MODULE_STATUS_UNAVAILABLE = frozenset(
    {"failed", "error", "permission_denied", "timeout", "partial"}
)
MODULE_STATUS_NOT_APPLICABLE = frozenset(
    {"skipped", "unsupported", "not_applicable", "not_enabled"}
)


def module_status_map(snapshot: dict[str, Any]) -> dict[str, str]:
    """把 ``collection.module_status`` 摊平成 {模块名: 状态小写}。

    同名模块出现多次时保留第一条（采集端不会重复登记同名模块；重复说明有缺陷，
    取第一条比取最后一条更不容易被尾部噪声覆盖）。
    """
    result: dict[str, str] = {}
    collection = snapshot.get("collection") or {}
    for item in collection.get("module_status") or []:
        name = str(item.get("name") or "").strip()
        if not name or name in result:
            continue
        result[name] = str(item.get("status") or "").strip().lower()
    return result


def lookup_status(status_map: dict[str, str], module_name: str) -> str | None:
    """按当前名查模块状态；查不到再依次试登记过的旧名。

    返回 None 表示**该模块在整个 module_status 里都不存在** —— 对旧采集包而言
    这意味着"那一版还没有这个采集模块"，属于未采集，不是失败。
    """
    if module_name in status_map:
        return status_map[module_name]
    for alias in LEGACY_MODULE_ALIASES.get(module_name, ()):
        if alias in status_map:
            return status_map[alias]
    return None


def classify_module(status: str | None) -> str:
    """把模块状态归成三态之一：``ok`` / ``unavailable`` / ``not_applicable``。

    模块名在 module_status 里找不到（``None``）也算 ``unavailable`` —— 这一版
    采集器没有这个模块，输入就是没有，不能当成"检查通过"。
    """
    if status is None:
        return "unavailable"
    if status in MODULE_STATUS_OK:
        return "ok"
    if status in MODULE_STATUS_NOT_APPLICABLE:
        return "not_applicable"
    if status in MODULE_STATUS_UNAVAILABLE:
        return "unavailable"
    # 未知状态是新引入的采集状态，按"不确定"处理最安全。
    return "unavailable"


def known_module_names() -> frozenset[str]:
    """规则包里 required_modules 允许出现的模块名全集。"""
    return frozenset(MODULE_BY_SNAPSHOT_KEY.values())
