"""Whole-host (OS) checks shared by every Linux-hosted database plugin.

MySQL, Oracle and PostgreSQL all inspect the same Linux host the same way, but
each plugin had grown its own copy of the metric keys, data-source choice,
thresholds and table rows.  Divergence was real and measurable: the same
collected package produced iowait thresholds of 20 / 20 / 15, memory judged on
``average`` in one plugin and ``max`` in another, and three different rule-id
spellings for one check.

This module is the single source of truth for that layer:

* ``CANONICAL_RULE_IDS`` / ``RETIRED_RULE_IDS`` — one shipped name per OS check,
  plus the spellings earlier versions of the plugins used (kept so an old report
  model can still be read back).
* ``DEFAULT_THRESHOLDS`` — CPU 90%, IO wait **20% (peak)**, memory **90% (peak)**,
  disk 80%. Every check uses the window **peak**; ``average`` is no longer used
  as a trigger value.
* ``normalize_os_metrics`` — reads both collector shapes (nested
  ``system_history``/``system_realtime`` blocks, or the flattened
  ``*_max`` keys PostgreSQL emits) into one canonical structure.
* ``os_source`` / ``os_pressure_report`` / ``os_pressure_verdicts`` — pick the
  observation window, then compute triggered/available for every OS check in one
  call.  A plugin does not decide any of this itself any more.
* ``os_resource_rows`` / ``format_bytes`` — the shared "系统资源概要" table.

Sunset rule: OS metric keys, thresholds and rule ids must be read from here.
A plugin may keep database-specific thresholds, but it must not re-declare an
OS threshold or invent a fifth rule-id spelling.
"""
from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
import re
from typing import Any

from .models import PackageContext
from .statistics import summarize
from .values import safe_float

__all__ = [
    "CANONICAL_RULE_IDS",
    "DEFAULT_THRESHOLDS",
    "DISK_MEDIA_HDD",
    "DISK_MEDIA_MIXED",
    "DISK_MEDIA_SSD",
    "MEMORY_CRITERION",
    "NON_PERSISTENT_FSTYPES",
    "OS_CHECK_LABELS",
    "OS_RULE_KEYS",
    "RETIRED_RULE_IDS",
    "RULE_FOR_METRIC",
    "canonical_rule_id",
    "disk_media",
    "empty_summary",
    "format_bytes",
    "host_identity",
    "is_persistent_fstype",
    "lsblk_entries",
    "memory_total_kb",
    "normalize_os_metrics",
    "ntp_clock_offset_seconds",
    "ntp_verdict",
    "os_disk_rows",
    "os_memory_available_min_bytes",
    "os_network_rows",
    "os_pressure_report",
    "os_pressure_verdicts",
    "os_resource_rows",
    "os_source",
    "parse_timedatectl",
]

# ---------------------------------------------------------------------------
# rule ids — one canonical spelling per OS check
# ---------------------------------------------------------------------------
CANONICAL_RULE_IDS: dict[str, str] = {
    "cpu_pressure": "COMMON.SYSTEM.CPU_PRESSURE",
    "iowait_pressure": "COMMON.SYSTEM.IOWAIT_PRESSURE",
    "memory_pressure": "COMMON.SYSTEM.MEMORY_PRESSURE",
    "disk_util": "COMMON.SYSTEM.DISK_UTIL",
    "time_sync": "COMMON.SYSTEM.TIME_SYNC",
    "filesystem_usage": "COMMON.CAPACITY.FILESYSTEM_USAGE",
}

# metric key -> rule key (built from CANONICAL_RULE_IDS)
RULE_FOR_METRIC: dict[str, str] = {
    "cpu_busy": "cpu_pressure",
    "cpu_iowait": "iowait_pressure",
    "memory_used": "memory_pressure",
    "disk_util": "disk_util",
}

# Chinese metric name used in the fact line of every OS verdict.
OS_CHECK_LABELS: dict[str, str] = {
    "cpu_busy": "CPU 峰值",
    "cpu_iowait": "IO wait 峰值",
    "memory_used": "内存峰值使用率",
    "disk_util": "磁盘利用率峰值",
}

# Rule keys a plugin may opt into, in report order.
OS_RULE_KEYS: tuple[str, ...] = (
    "cpu_pressure", "iowait_pressure", "memory_pressure", "disk_util",
)

# Spellings retired by the rule-id migration.  Direction is retired -> canonical
# and deliberately one-way: since every plugin now ships the canonical name,
# nothing may translate a canonical id back into a plugin-specific spelling.
# The table exists so an older report model can still be resolved.
RETIRED_RULE_IDS: dict[str, dict[str, str]] = {
    "mysql": {},
    "oracle": {
        "ORA.SYSTEM.CPU_PRESSURE": "COMMON.SYSTEM.CPU_PRESSURE",
        "ORA.SYSTEM.IOWAIT_PRESSURE": "COMMON.SYSTEM.IOWAIT_PRESSURE",
        "ORA.SYSTEM.MEMORY_PRESSURE": "COMMON.SYSTEM.MEMORY_PRESSURE",
        "ORA.SYSTEM.TIME_SYNC": "COMMON.SYSTEM.TIME_SYNC",
        "ORA.CAPACITY.FILESYSTEM_USAGE": "COMMON.CAPACITY.FILESYSTEM_USAGE",
    },
    "postgresql": {
        "COMMON.SYSTEM.CPU": "COMMON.SYSTEM.CPU_PRESSURE",
        "COMMON.SYSTEM.IOWAIT": "COMMON.SYSTEM.IOWAIT_PRESSURE",
        "COMMON.SYSTEM.MEMORY": "COMMON.SYSTEM.MEMORY_PRESSURE",
    },
}

# ---------------------------------------------------------------------------
# thresholds and criteria
# ---------------------------------------------------------------------------
DEFAULT_THRESHOLDS: dict[str, float] = {
    "cpu_peak_warning": 90.0,
    "iowait_peak_warning": 20.0,
    "memory_usage_warning": 90.0,
    "disk_util_warning": 80.0,
    "filesystem_usage_critical": 90.0,
}

# All OS pressure checks read the window peak.  Never switch this back to
# "average": a host that pegs 100% for two minutes inside a 24h window is a
# real problem, and averaging hides it.
MEMORY_CRITERION = "max"
CPU_CRITERION = "max"
IOWAIT_CRITERION = "max"
DISK_CRITERION = "max"

_CRITERIA: dict[str, str] = {
    "cpu_busy": CPU_CRITERION,
    "cpu_iowait": IOWAIT_CRITERION,
    "memory_used": MEMORY_CRITERION,
    "disk_util": DISK_CRITERION,
}

_THRESHOLD_KEYS: dict[str, str] = {
    "cpu_busy": "cpu_peak_warning",
    "cpu_iowait": "iowait_peak_warning",
    "memory_used": "memory_usage_warning",
    "disk_util": "disk_util_warning",
}

# ---------------------------------------------------------------------------
# filesystem filtering — one judgement for "is this mount worth judging"
# ---------------------------------------------------------------------------
# A host reports far more mounts than it has storage.  ``df`` and ``mount`` both
# list kernel pseudo filesystems (proc/tmpfs/hugetlbfs), read-only media and
# container overlays.  Their "usage" says nothing about the database host: an
# optical drive is permanently 100% full with 0 bytes free, so feeding it into a
# capacity check manufactures a risk the customer cannot act on.
#
# Network filesystems are deliberately absent — "the data directory lives on
# NFS/CIFS" is exactly the risk the report exists to surface.
NON_PERSISTENT_FSTYPES: frozenset[str] = frozenset({
    # kernel pseudo filesystems: no capacity concept at all
    "sysfs", "proc", "cgroup", "cgroup2", "tmpfs", "devtmpfs", "devpts",
    "securityfs", "pstore", "bpf", "tracefs", "configfs", "debugfs", "mqueue",
    "autofs", "binfmt_misc", "fusectl", "rpc_pipefs", "hugetlbfs", "ramfs",
    "nsfs", "efivarfs",
    # read-only media and images: always 100% used, never growable
    "iso9660", "squashfs", "udf",
    # container overlay
    "overlay",
    # desktop / hypervisor FUSE mounts
    "fuse.gvfsd-fuse", "fuse.gvfs-fuse-daemon", "fuse.vmware-vmblock",
})


def is_persistent_fstype(fstype: Any) -> bool:
    """True when a mount can hold data whose capacity must be planned for.

    Both the metric layer (which picks the worst usage figure) and the report
    tables (which list mounts) filter on this predicate, so a mount can never be
    excluded from the number while still being shown as the reason for it.
    """
    return str(fstype or "").strip().lower() not in NON_PERSISTENT_FSTYPES


# ---------------------------------------------------------------------------
# 磁盘介质（HDD / SSD）
# ---------------------------------------------------------------------------
#
# 数据目录落在旋转盘还是固态盘，决定 innodb_io_capacity、IO 延迟这类结论怎么
# 落点：机械盘上 await 偏高属正常，SSD 上同样数字才是真问题。lsblk 已经把
# 答案采了下来（ROTA：1 = 旋转盘/HDD，0 = 非旋转盘/SSD），但采集端落盘的是
# 人眼对齐的空格表格，不是制表符分隔——通用 ``parse_delimited`` 只能把整行
# 塞进"唯一一个键"下，按列访问根本拿不到 ROTA。
#
# 还原列**不能**按表头列宽切位：SIZE 是右对齐数字，会侵占前一列的空白
# （`disk  107374182400` 按 TYPE 列宽切出来是 `disk  10737418`）。这里改用
# **TYPE 锚点**：NAME/KNAME 恒非空、TYPE 恒紧随其后，于是 TYPE 之后的第一个
# token 是 SIZE，之后首个裸 `0`/`1` 就是 ROTA（FSTYPE、MOUNTPOINT 里不会出现
# 裸的 0/1，SERIAL 之类是多字符）。
_LSBLK_DEVICE_TYPES: frozenset[str] = frozenset({
    "disk", "part", "lvm", "rom", "loop", "crypt", "mpath", "raid0", "raid1",
    "raid4", "raid5", "raid6", "raid10",
})

DISK_MEDIA_HDD = "机械磁盘（HDD）"
DISK_MEDIA_SSD = "固态硬盘（SSD）"
DISK_MEDIA_MIXED = "混合介质（HDD+SSD）"

_ROTA_LABELS: dict[str, str] = {"1": DISK_MEDIA_HDD, "0": DISK_MEDIA_SSD}


def lsblk_entries(rows: Any) -> list[dict[str, str]]:
    """把 lsblk 采集结果规范成 ``[{NAME, KNAME, TYPE, SIZE, ROTA, MOUNTPOINT}]``。

    两种形态都接受：采集端当前落盘的空格对齐整行（整行文本是行里唯一的值），
    以及将来改用 ``lsblk -P`` 后已按字段拆开的行。解析不出来的行直接丢弃——
    "读不出来"必须是缺席，不能默认成某个介质。

    设备名请用 ``KNAME``：``NAME`` 在 lsblk 里带树形前缀（``|-sda1``、
    ``|-ao-root``），只有 ``KNAME`` 是干净设备名（``sda1``、``dm-0``）。
    """
    entries: list[dict[str, str]] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        if "ROTA" in row and ("NAME" in row or "KNAME" in row):
            entries.append({
                key: str(row.get(key) or "").strip()
                for key in ("NAME", "KNAME", "TYPE", "SIZE", "ROTA", "MOUNTPOINT")
            })
            continue
        line = next((value for value in row.values() if isinstance(value, str)), "")
        tokens = line.split()
        if len(tokens) < 4:
            continue
        type_index = next(
            (index for index, token in enumerate(tokens) if token in _LSBLK_DEVICE_TYPES), None
        )
        if type_index is None:
            continue
        tail = tokens[type_index + 1:]
        entries.append({
            "NAME": tokens[type_index - 2] if type_index >= 2 else tokens[0],
            "KNAME": tokens[type_index - 1] if type_index >= 1 else "",
            "TYPE": tokens[type_index],
            "SIZE": tail[0] if tail else "",
            "ROTA": next((token for token in tail if token in _ROTA_LABELS), ""),
            "MOUNTPOINT": next((token for token in tail if token.startswith("/")), ""),
        })
    return entries


def _mount_is_prefix(mountpoint: str, path: str) -> bool:
    mount = mountpoint.rstrip("/") or "/"
    if mount == "/":
        # 根挂载能覆盖所有绝对路径，但优先级最低——调用处取最长匹配。
        return path.startswith("/")
    return path == mount or path.startswith(mount + "/")


def disk_media(rows: Any, *, data_path: str | None = None) -> str | None:
    """判定数据库数据目录所在磁盘的介质。

    给了 ``data_path`` 且能定位到它的挂载点时，按该设备判定；定位不到时，只有
    在所有物理盘（``TYPE == disk``）介质一致的情况下才下结论，混合则显式报
    "混合"。任何一步读不出来都返回 ``None``——报告里宁可不显示这一行，也不把
    "没读出来"写成"正常"（见 analysis contracts 的缺失值策略）。
    """
    entries = lsblk_entries(rows)
    if not entries:
        return None
    path = str(data_path or "").strip()
    if path:
        candidates = [
            entry for entry in entries
            if entry.get("MOUNTPOINT") and _mount_is_prefix(entry["MOUNTPOINT"], path)
        ]
        if candidates:
            target = max(candidates, key=lambda entry: len(entry["MOUNTPOINT"].rstrip("/")))
            return _ROTA_LABELS.get(target.get("ROTA", ""))
    labels = {
        _ROTA_LABELS.get(entry.get("ROTA", ""))
        for entry in entries
        if entry.get("TYPE") == "disk"
    }
    labels.discard(None)
    if len(labels) == 1:
        return labels.pop()
    if len(labels) > 1:
        return DISK_MEDIA_MIXED
    return None


HISTORY_LABEL = "SAR 24h 历史"
REALTIME_LABEL = "现场短时采样"
MERGED_LABEL = "SAR 历史 + 实时采样"

HISTORY_REASON = "使用有效 SAR 历史"
REALTIME_REASON = "仅使用现场短时样本，结论置信度较低"
SHORT_HISTORY_REASON = "SAR 历史覆盖不足，结论置信度较低"

HISTORY_CONFIDENCE = 0.9
REALTIME_CONFIDENCE = 0.65

# SAR is collected over a day.  ``sar_effective_coverage_hours`` is measured in
# HOURS, so it must be compared against a share of the 24h window — comparing it
# to the bare ratio 0.8 would call a 50-minute sample "usable 24h history".
SAR_WINDOW_HOURS = 24.0
MIN_COVERAGE_RATIO = 0.8

_METRIC_ALIASES: dict[str, tuple[str, ...]] = {
    "cpu_busy": ("cpu_busy_percent", "cpu_busy_pct"),
    "cpu_iowait": ("cpu_iowait_percent", "cpu_iowait_pct"),
    "memory_used": ("memory_used_percent", "memory_used_pct"),
    "disk_util": ("disk_util_percent", "disk_util_pct"),
}

_FLAT_KEYS: dict[str, str] = {
    "cpu_busy": "cpu_busy_max",
    "cpu_iowait": "iowait_max",
    "memory_used": "memory_used_max",
    "disk_util": "disk_util_max",
}


def canonical_rule_id(database: str, rule_id: str) -> str:
    """Resolve a retired spelling of an OS rule id to its canonical name.

    Only needed when reading artefacts produced before the migration; every
    plugin writes the canonical name now, so a canonical input is returned
    unchanged.
    """
    return RETIRED_RULE_IDS.get(database, {}).get(rule_id, rule_id)


def empty_summary() -> dict[str, Any]:
    """A summary for "not collected" — all None, never zeros."""
    return summarize([])


def host_identity(ctx: PackageContext) -> dict[str, Any]:
    """Return the host block regardless of which key the collector used."""
    for key in ("host_identity", "host"):
        block = ctx.snapshot.get(key)
        if isinstance(block, dict) and block:
            return block
    return {}


def memory_total_kb(ctx: PackageContext,
                    sar_rows: list[dict[str, Any]] | None = None) -> float | None:
    """Total RAM in KiB, however this collector happened to record it.

    MySQL's snapshot states it outright; Oracle's flat snapshot does not, so the
    value is recovered from the realtime sample, and failing that from a SAR
    memory row (``kbmemused`` against ``%memused``).  Returning None is honest —
    callers must then omit byte/percentage-of-total derived series instead of
    dividing by a made-up total.
    """
    explicit = safe_float(host_identity(ctx).get("memory_total_bytes"))
    if explicit is None:
        explicit = safe_float(ctx.snapshot.get("mem_total_bytes"))
    if explicit:
        return explicit / 1024
    for row in ctx.timeseries.get("system_memory", []):
        total = safe_float(row.get("mem_total_bytes"))
        if total:
            return total / 1024
    for row in sar_rows if sar_rows is not None else ctx.history.get("sar_memory", []):
        used = safe_float(row.get("kbmemused"))
        percent = safe_float(row.get("%memused"))
        if used and percent:
            return used / (percent / 100.0)
    return None


def format_bytes(value: Any) -> str | None:
    """Human-readable byte size; returns None when the value is missing."""
    number = safe_float(value)
    if number is None:
        return None
    units = ("B", "KB", "MB", "GB", "TB", "PB")
    index = 0
    while abs(number) >= 1024 and index < len(units) - 1:
        number /= 1024
        index += 1
    return f"{number:.2f} {units[index]}"


def _as_summary(value: Any) -> dict[str, Any]:
    """Accept either a pre-computed summary dict or a bare scalar."""
    if isinstance(value, dict):
        base = empty_summary()
        base.update({key: value.get(key) for key in base})
        base["count"] = value.get("count", base["count"] or 0)
        return base
    number = safe_float(value)
    if number is None:
        return empty_summary()
    return summarize([number])


def _block_from_aliases(source: dict[str, Any], aliases: dict[str, tuple[str, ...]]) -> dict[str, Any]:
    block: dict[str, Any] = {}
    for metric, names in aliases.items():
        found = empty_summary()
        for name in names:
            if name in source:
                found = _as_summary(source[name])
                break
        block[metric] = found
    return block


def _flat_block(metrics: dict[str, Any]) -> dict[str, Any]:
    block: dict[str, Any] = {}
    for metric, key in _FLAT_KEYS.items():
        block[metric] = _as_summary(metrics.get(key))
    return block


def normalize_os_metrics(metrics: dict[str, Any]) -> dict[str, Any]:
    """Normalise either collector shape into history / realtime / flat blocks.

    Returns a new dict so callers can pass it around without mutating the
    analyzer's metrics.  Passing an already-normalised dict returns it as is.
    """
    if isinstance(metrics, dict) and metrics.get("_os_normalized"):
        return metrics
    history = _block_from_aliases(metrics.get("system_history") or {}, _METRIC_ALIASES)
    realtime = _block_from_aliases(metrics.get("system_realtime") or {}, _METRIC_ALIASES)
    nested = bool(metrics.get("system_history") or metrics.get("system_realtime"))
    flat = None if nested else _flat_block(metrics)
    return {
        "_os_normalized": True,
        "history": history,
        "realtime": realtime,
        "flat": flat,
        "shape": "nested" if nested else "flat",
    }


def _has_values(block: dict[str, Any]) -> bool:
    return any((block.get(metric) or {}).get("max") is not None for metric in _METRIC_ALIASES)


def os_source(metrics: dict[str, Any]) -> dict[str, Any]:
    """Pick the observation window an OS verdict may claim to describe.

    A three-minute sample is not evidence about a day.  When usable SAR history
    exists it wins; otherwise the verdict is marked low-confidence so the report
    can say so instead of presenting a short window as if it were the norm.
    """
    normalized = normalize_os_metrics(metrics)
    if normalized["shape"] == "flat":
        coverage = safe_float(metrics.get("sar_effective_coverage_hours")) or 0.0
        ratio = min(1.0, coverage / SAR_WINDOW_HOURS) if SAR_WINDOW_HOURS > 0 else 0.0
        merged = str(metrics.get("system_metric_source", "")) == "sar+realtime"
        usable = merged and ratio >= MIN_COVERAGE_RATIO
        if not merged:
            reason = REALTIME_REASON
        elif usable:
            reason = HISTORY_REASON
        else:
            reason = SHORT_HISTORY_REASON
        return {
            "block": normalized["flat"] or {},
            "scope": "history" if merged else "realtime",
            "label": MERGED_LABEL if merged else REALTIME_LABEL,
            "reason": reason,
            "confidence": HISTORY_CONFIDENCE if usable else REALTIME_CONFIDENCE,
            "usable_history": usable,
            "coverage_hours": coverage,
            "coverage_ratio": round(ratio, 4),
        }
    usable = bool(
        metrics.get("sampling_context", {})
        .get("history", {})
        .get("usable_for_trend_rules", False)
    )
    if usable and _has_values(normalized["history"]):
        return {
            "block": normalized["history"],
            "scope": "history",
            "label": HISTORY_LABEL,
            "reason": HISTORY_REASON,
            "confidence": HISTORY_CONFIDENCE,
            "usable_history": True,
            "coverage_hours": None,
        }
    return {
        "block": normalized["realtime"],
        "scope": "realtime",
        "label": REALTIME_LABEL,
        "reason": REALTIME_REASON,
        "confidence": REALTIME_CONFIDENCE,
        "usable_history": False,
        "coverage_hours": None,
    }


def os_pressure_verdicts(
    block: dict[str, Any],
    thresholds: dict[str, Any] | None = None,
    *,
    include: Iterable[str] | None = None,
) -> list[dict[str, Any]]:
    """Evaluate the OS pressure checks against one metric block.

    Every check compares the window **peak** to the threshold, and "no data" is
    reported as ``available=False`` rather than as a pass.

    ``include`` narrows the result to the rule keys a plugin actually ships in
    its rule pack (``OS_RULE_KEYS`` order); the plugins differ here — MySQL and
    Oracle judge three checks, PostgreSQL judges four — but each one judges them
    through this function so the verdict semantics cannot drift.
    """
    limits = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
    wanted = set(include) if include is not None else None
    verdicts: list[dict[str, Any]] = []
    for metric, rule_key in RULE_FOR_METRIC.items():
        if wanted is not None and rule_key not in wanted:
            continue
        summary = block.get(metric) or {}
        value = summary.get(_CRITERIA[metric])
        limit = float(limits[_THRESHOLD_KEYS[metric]])
        label = OS_CHECK_LABELS[metric]
        verdicts.append({
            "rule_key": rule_key,
            "rule_id": CANONICAL_RULE_IDS[rule_key],
            "metric": metric,
            "criterion": _CRITERIA[metric],
            "label": label,
            "value": value,
            "threshold": limit,
            "available": value is not None,
            "triggered": value is not None and value >= limit,
            "fact": None if value is None else f"{label}：{value:.1f}%",
        })
    return verdicts


def os_pressure_report(
    metrics: dict[str, Any],
    thresholds: dict[str, Any] | None = None,
    *,
    include: Iterable[str] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Pick the observation window, then judge ``include`` inside it.

    The two halves belong together: choosing the window and judging the numbers
    out of a different window is exactly the bug this layer exists to prevent.
    """
    source = os_source(metrics)
    return source, os_pressure_verdicts(source["block"], thresholds, include=include)


def _realtime_block(metrics: dict[str, Any]) -> dict[str, Any]:
    return metrics.get("system_realtime") or {}


def _summary_value(block: dict[str, Any], key: str, field: str) -> Any:
    summary = block.get(key)
    if not isinstance(summary, dict):
        return None
    return summary.get(field)


def os_memory_available_min_bytes(metrics: dict[str, Any]) -> float | None:
    """窗口内**最低可用内存**（``MemAvailable``，字节）；未采集时返回 None。

    ``%memused``（SAR 与 /proc/meminfo 都给）把页缓存算作"已用"，单看它会把
    "缓存占满、可用内存充足"的正常主机判成内存压力——这是内存规则长期误报的根因。
    ``MemAvailable`` 是内核给出的可用于新分配的估计值，但**只有实时采样块里有**
    （SAR 历史不采集该字段）。因此这个取值必然来自实时窗口；它只用于给历史窗口的
    ``%memused`` 判定做复核，不参与挑选判定窗口。

    取值收口在这里，插件不得自己去翻 ``system_realtime``。
    """
    return safe_float(_summary_value(_realtime_block(metrics), "memory_available_bytes", "min"))


def os_resource_rows(metrics: dict[str, Any], *, source_label: str | None = None) -> list[dict[str, Any]]:
    """Build the shared 系统资源概要 table from the chosen OS window.

    Columns are 指标 / 平均值 / 峰值 / 数据源.  Byte-based rows are only added
    when the collector actually produced byte metrics, so an Oracle package does
    not gain an empty "可用内存" row.
    """
    source = os_source(metrics)
    block = source["block"]
    label = source_label or source["label"]

    def pct(value: Any) -> str:
        return "-" if value is None else f"{value}%"

    rows: list[dict[str, Any]] = [
        {
            "指标": "CPU 使用率",
            "平均值": pct(_summary_value(block, "cpu_busy", "average")),
            "峰值": pct(_summary_value(block, "cpu_busy", "max")),
            "数据源": label,
        },
        {
            "指标": "CPU IO wait",
            "平均值": pct(_summary_value(block, "cpu_iowait", "average")),
            "峰值": pct(_summary_value(block, "cpu_iowait", "max")),
            "数据源": label,
        },
        {
            "指标": "内存使用率",
            "平均值": pct(_summary_value(block, "memory_used", "average")),
            "峰值": pct(_summary_value(block, "memory_used", "max")),
            "数据源": label,
        },
    ]

    realtime = _realtime_block(metrics)
    available = _summary_value(realtime, "memory_available_bytes", "average")
    available_min = os_memory_available_min_bytes(metrics)
    if available is not None:
        rows.append({
            "指标": "可用内存",
            "平均值": format_bytes(available),
            "峰值": format_bytes(available_min),
            "数据源": "峰值列表示窗口内最低可用内存",
        })
    swap = _summary_value(realtime, "swap_used_bytes", "average")
    if swap is not None:
        rows.append({
            "指标": "已用 Swap",
            "平均值": format_bytes(swap),
            "峰值": format_bytes(_summary_value(realtime, "swap_used_bytes", "max")),
            "数据源": REALTIME_LABEL,
        })
    return rows


def os_disk_rows(metrics: dict[str, Any]) -> list[dict[str, Any]]:
    """Per-device realtime disk table (device / util / await)."""
    rows: list[dict[str, Any]] = []
    for device, block in (_realtime_block(metrics).get("disk_devices") or {}).items():
        if not isinstance(block, dict):
            continue
        rows.append({
            "设备": device,
            "平均 util": f"{_summary_value(block, 'util', 'average')}%",
            "峰值 util": f"{_summary_value(block, 'util', 'max')}%",
            "平均读 await": f"{_summary_value(block, 'read_await', 'average')} ms",
            "平均写 await": f"{_summary_value(block, 'write_await', 'average')} ms",
        })
    return rows


def os_network_rows(metrics: dict[str, Any]) -> list[dict[str, Any]]:
    """Per-interface realtime network table, loopback excluded."""
    rows: list[dict[str, Any]] = []
    for interface, block in (_realtime_block(metrics).get("network_interfaces") or {}).items():
        if interface == "lo" or not isinstance(block, dict):
            continue
        rows.append({
            "网卡": interface,
            "平均接收": _rate(block, "rx_bps", "average"),
            "峰值接收": _rate(block, "rx_bps", "max"),
            "平均发送": _rate(block, "tx_bps", "average"),
            "峰值发送": _rate(block, "tx_bps", "max"),
        })
    return rows


def _rate(block: dict[str, Any], key: str, field: str) -> str | None:
    value = _summary_value(block, key, field)
    if value is None:
        return None
    rendered = format_bytes(value)
    return None if rendered is None else f"{rendered}/s"


# ---------------------------------------------------------------------------
# 时间同步（NTP）证据
# ---------------------------------------------------------------------------
# `timedatectl` 是判断"主机时间能不能信"的唯一实锤，而采集侧的
# `snapshot.time_evidence.ntp_synchronized` 在部分采集版本里是空串（解析缺失），
# `evidence/timedatectl.txt` 却一直有原文。判定口径收口在这里，插件不得各自
# 从原文再解析一遍，也不得把"未启用持续同步源但已经对齐"与"真的没同步"
# 混成同一条风险 —— 前者只需确认，后者才影响日志关联与复制诊断。

_TIMEDATECTL_FIELDS = {
    "Local time": "local_time",
    "Universal time": "universal_time",
    "RTC time": "rtc_time",
    "Time zone": "time_zone",
    "NTP enabled": "ntp_enabled",
    "NTP synchronized": "ntp_synchronized",
    "RTC in local TZ": "rtc_in_local_tz",
    "DST active": "dst_active",
}

# `timedatectl` 的时钟写法：Mon 2026-09-21 10:34:22
# `Local time` / `Universal time` 行尾还带时区缩写（CST / UTC），因此只截取
# 时钟本体，不把后缀当输入 —— 否则 `strptime` 会因多余 token 整体失败。
_TIMEDATECTL_CLOCK = "%a %Y-%m-%d %H:%M:%S"
_TIMEDATECTL_CLOCK_RE = re.compile(r"(\w{3}\s+\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})")

_TRUTHY = {"yes", "true", "1", "active", "on"}
_FALSY = {"no", "false", "0", "inactive", "off"}


def parse_timedatectl(text: Any) -> dict[str, Any]:
    """把 ``timedatectl`` 原文解析成白名单键值对，其余行忽略。

    只认已知字段：未知行不进结果，避免把发行版差异（新增/改名的行）当成判据输入。
    """
    evidence: dict[str, Any] = {}
    for raw_line in str(text or "").splitlines():
        if ":" not in raw_line:
            continue
        label, _, value = raw_line.partition(":")
        key = _TIMEDATECTL_FIELDS.get(label.strip())
        value = value.strip()
        if key is None or not value:
            continue
        evidence[key] = value
    return evidence


def _timedatectl_clock(value: Any) -> datetime | None:
    match = _TIMEDATECTL_CLOCK_RE.search(str(value or ""))
    if match is None:
        return None
    try:
        return datetime.strptime(match.group(1), _TIMEDATECTL_CLOCK)
    except ValueError:
        return None


def ntp_clock_offset_seconds(evidence: dict[str, Any]) -> float | None:
    """RTC 与参考时钟的偏移（秒）；取不到返回 None。

    ``RTC in local TZ: yes`` 时 RTC 存的是本地时间，必须与 ``Local time`` 比；
    否则与 ``Universal time`` 比。只解析到分钟精度，因此结果精度也是分钟。
    """
    rtc = _timedatectl_clock(evidence.get("rtc_time"))
    if rtc is None:
        return None
    reference_key = "local_time" if str(
        evidence.get("rtc_in_local_tz", "")
    ).strip().lower() in _TRUTHY else "universal_time"
    reference = _timedatectl_clock(evidence.get(reference_key))
    if reference is None:
        return None
    return abs((rtc - reference).total_seconds())


def ntp_verdict(
    evidence: dict[str, Any], *, offset_warning_seconds: float = 10.0
) -> dict[str, Any]:
    """按单个节点给时间同步结论，四档：``ok`` / ``attention`` / ``risk`` / ``unknown``。

    - ``synchronized=no`` 且 RTC 偏移超过阈值 → ``risk``（有实锤）
    - ``synchronized=no`` 但拿不到偏移 → ``attention``（未同步，但无法量化后果）
    - ``enabled=no`` 且 ``synchronized=yes`` → ``attention``（未配持续同步源，需确认）
    - ``synchronized=yes`` 且已启用同步源 → ``ok``
    - 两个字段都拿不到 → ``unknown``（不判定，也不折叠成"通过"）

    返回值里的 ``triggered`` 只把 ``risk`` 记为真：``attention`` 需要人工确认，
    不具备"必须整改"的定性证据，规则引擎里落 ``passed``、呈现层落"提示"。
    """
    synchronized = str(evidence.get("ntp_synchronized", "")).strip().lower()
    enabled = str(evidence.get("ntp_enabled", "")).strip().lower()
    offset = ntp_clock_offset_seconds(evidence)
    offset_rendered = None if offset is None else f"{offset:.0f} 秒"
    facts: list[str] = [f"NTP synchronized={synchronized or 'unknown'}"]
    if enabled:
        facts.append(f"NTP enabled={enabled}")
    if offset is not None:
        facts.append(f"RTC 与参考时钟偏移 {offset_rendered}")

    if synchronized in _FALSY:
        if offset is not None and offset > offset_warning_seconds:
            return {
                "tier": "risk", "triggered": True,
                "reason": f"主机时间未与 NTP 同步，RTC 偏移 {offset_rendered}",
                "facts": facts,
            }
        return {
            "tier": "attention", "triggered": False,
            "reason": "主机时间未与 NTP 同步（无法量化偏移）",
            "facts": facts,
        }
    if synchronized in _TRUTHY:
        if enabled in _FALSY:
            return {
                "tier": "attention", "triggered": False,
                "reason": "已对齐但未启用持续同步源，需确认同步方式",
                "facts": facts,
            }
        return {
            "tier": "ok", "triggered": False,
            "reason": "主机时间同步状态正常", "facts": facts,
        }
    return {
        "tier": "unknown", "triggered": False,
        "reason": "本次未采集到可判读的时间同步证据",
        "facts": facts if evidence else [],
    }
