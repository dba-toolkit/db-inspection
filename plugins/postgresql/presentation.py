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

from inspection_core.models import PackageContext
from inspection_core.statistics import summarize
from inspection_core.values import safe_float, safe_int
from .parsers import _parse_df_pt, _parse_free_b
from .rules import Finding, RuleEvaluation


class PostgreSQLPresentationBuilder:
    def __init__(self, analyzer_version: str, analysis_schema: str, clock: Callable[[], str]) -> None:
        self.analyzer_version = analyzer_version
        self.analysis_schema = analysis_schema
        self.clock = clock

    def health_summary(self, findings: list[Finding]) -> dict[str, Any]:
        critical = sum(1 for f in findings if f.severity == "critical")
        warning = sum(1 for f in findings if f.severity == "warning")
        info = sum(1 for f in findings if f.severity == "info")
        score = max(0, 100 - critical * 25 - warning * 10 - info * 3)
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
        collection_status = ctx.snapshot.get("collection_status", {})

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

        def _coll_status(item_id: str, rows: list | None = None) -> dict[str, Any]:
            # Look up from collection_status if available
            if item_id in collection_status:
                cs = collection_status[item_id]
                return {"status": cs.get("status", "ok"), "row_count": cs.get("row_count", 0), "reason": cs.get("reason", "")}
            # Otherwise infer from rows
            if rows is not None:
                count = len(rows)
                if count > 0:
                    return {"status": "ok", "row_count": count, "reason": ""}
                return {"status": "empty", "row_count": 0, "reason": "无采集数据"}
            return {"status": "ok", "row_count": 0, "reason": ""}

        def _item(item_id: str, title: str, source: str, rows: list[dict[str, Any]],
                  conclusion: str, *, status: str = "normal",
                  recommendation: str = "", evidence: list[str] | None = None,
                  note: str = "", coll_override: dict | None = None) -> dict[str, Any]:
            return {
                "item_id": item_id,
                "title": title,
                "source": source,
                "collection": coll_override or _coll_status(item_id, rows),
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
        _SKIP_FS_TYPES = {"iso9660", "devtmpfs", "tmpfs", "squashfs", "overlay"}
        real_fs = [r for r in fs_rows if str(r.get("fstype", "")).lower() not in _SKIP_FS_TYPES]
        skipped_100s = [r for r in fs_rows if str(r.get("fstype", "")).lower() in _SKIP_FS_TYPES and (r.get("usage_percent") or 0) >= 95]

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

        def _mount_rows(path: Path) -> list[dict[str, Any]]:
            if not path.exists():
                return []
            rows: list[dict[str, Any]] = []
            pattern = re.compile(r"^(.*?) on (.*?) type (.*?) \((.*)\)$")
            for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
                matched = pattern.match(raw.strip())
                if not matched:
                    continue
                rows.append(_row(
                    设备=matched.group(1).strip(),
                    挂载点=matched.group(2).strip(),
                    类型=matched.group(3).strip(),
                    挂载选项=matched.group(4).strip(),
                ))
            return rows

        def _dmesg_rows(path: Path, limit: int = 20) -> list[dict[str, Any]]:
            if not path.exists():
                return []
            lines = [l.strip() for l in path.read_text(encoding="utf-8", errors="replace").splitlines() if l.strip()]
            return [_row(内核错误日志=l) for l in lines[:limit]]

        lscpu = _colon_map(root / "evidence" / "lscpu.txt")
        time_info = ctx.snapshot.get("time_evidence", {})
        kernel_rows = _key_value_rows(root / "tables" / "kernel_parameters.tsv")
        mount_rows = _mount_rows(root / "evidence" / "mounts.txt")
        dmesg_rows = _dmesg_rows(root / "evidence" / "dmesg_errors.txt")

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

        inst_rows = [
            _row(检查项="数据库版本", 采集值=ident.get("version", ""), 说明=""),
            _row(检查项="实例标识", 采集值=ident.get("instance_tag", ""), 说明=""),
            _row(检查项="运行时间", 采集值=str(inst.get("uptime", ""))[:19] if inst.get("uptime") else "", 说明=""),
            _row(检查项="观测角色", 采集值=role.get("role_observed", ""), 说明="依据只读/复制证据推断"),
            _row(检查项="WAL 级别", 采集值=settings.get("wal_level", ""), 说明="replica/logical 可支持复制"),
            _row(检查项="归档模式", 采集值=settings.get("archive_mode", ""), 说明="PITR 需要 archive_mode=on"),
            _row(检查项="autovacuum", 采集值="开启" if autovac_ok else "⚠ 关闭", 说明="自动清理与维护"),
            _row(检查项="max_connections", 采集值=str(settings.get("max_connections", "")), 说明="最大连接数"),
            _row(检查项="数据库数量", 采集值=str(len(db_rows)), 说明=""),
            _row(检查项="数据目录", 采集值=settings.get("data_directory", ""), 说明="PGDATA"),
        ]
        _add_section("system_environment", "实例基础信息", [
            _item("pg.instance", "实例基本信息", "snapshot.json#instance_identity", inst_rows,
                  conclusion="实例版本 {} 已识别，观测角色 {}；{}".format(
                      ident.get("version", "未知")[:40], role.get("role_observed", "standalone"),
                      "配置无异常" if not issues else "关注：" + "、".join(issues)),
                  status="attention" if issues else "normal",
                  evidence=[f"端口 {ident.get('port', '?')}", f"PGDATA={settings.get('data_directory', '?')}"],
                  recommendation="；".join(issues) if issues else ""),
        ])

        # =====================================================================
        # Section 2: 操作系统信息
        # =====================================================================
        os_rows = [
            _row(检查项="主机名", 采集值=host.get("hostname", ""), 说明=""),
            _row(检查项="IP 地址", 采集值=host.get("primary_ip", ""), 说明=""),
            _row(检查项="操作系统", 采集值=host.get("os", ""), 说明=""),
            _row(检查项="内核版本", 采集值=host.get("kernel", ""), 说明=""),
            _row(检查项="CPU 型号", 采集值=lscpu.get("Model name", ""), 说明=""),
            _row(检查项="CPU 核数", 采集值=str(host.get("cpu_count", "")), 说明=""),
            _row(检查项="内存总量", 采集值=_fmt_bytes(host.get("memory_total_bytes")), 说明=""),
        ]
        if mem_snap and mem_snap.get("available"):
            os_rows.append(_row(检查项="可用内存", 采集值=_fmt_bytes(mem_snap["available"]), 说明="现场快照"))

        ntp_value = str(time_info.get("ntp_synchronized", "")).lower()
        ntp_ok = ntp_value in {"yes", "true", "1", "active"}
        time_rows = [
            _row(检查项="本地时间", 采集值=time_info.get("host_local_time", ""), 说明=""),
            _row(检查项="时区", 采集值=time_info.get("timezone", ""), 说明=""),
            _row(检查项="NTP 同步", 采集值=time_info.get("ntp_synchronized", ""), 说明=""),
        ]

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
                  evidence=[f"参数 {len(kernel_rows)} 项"]),
            _item("system.mounts", "挂载参数", "evidence/mounts.txt", mount_rows,
                  conclusion="已取得挂载参数，可用于检查数据目录文件系统的持久性选项。" if mount_rows else "未采集到挂载参数。",
                  evidence=[f"挂载点 {len(mount_rows)} 个"]),
            _item("system.dmesg_errors", "内核错误摘要", "evidence/dmesg_errors.txt", dmesg_rows,
                  conclusion="存在内核错误级别日志，建议结合硬件与系统日志复核。" if dmesg_rows else "未发现内核错误级别日志。",
                  status="attention" if dmesg_rows else "normal"),
        ])

        # =====================================================================
        # Section 3: 文件系统容量
        # =====================================================================
        fs_display_rows: list[dict] = []
        for r in real_fs[:15]:
            pct = r.get("usage_percent", 0) or 0
            flag = "⚠" if pct >= 80 else ""
            used_kb = (r.get("used") or 0) * 1024
            total_kb = (r.get("blocks") or 0) * 1024
            fs_display_rows.append(_row(
                文件系统=r.get("filesystem", ""),
                类型=r.get("fstype", ""),
                挂载点=r.get("mountpoint", ""),
                使用率=f"{flag}{pct:.0f}%",
                可用空间=_fmt_bytes((r.get("available") or 0) * 1024),
            ))

        critical_fs = [r for r in real_fs if (r.get("usage_percent") or 0) >= 80]
        fs_use_max = max((r.get("usage_percent") or 0 for r in real_fs), default=0)

        if critical_fs:
            fs_status = "risk"
            fs_conclusion = f"存在 {len(critical_fs)} 个挂载点使用率 ≥80%：{', '.join(r.get('mountpoint','?') for r in critical_fs)}。"
            fs_recommendation = "清理日志/WAL 归档释放空间，或在线扩容文件系统。"
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

        _add_section("filesystem_capacity", "文件系统容量", [
            _item("system.filesystems", "文件系统容量", "tables/filesystems.tsv", fs_display_rows,
                  conclusion=fs_conclusion, status=fs_status, recommendation=fs_recommendation,
                  evidence=[f"挂载点 {len(real_fs)} 个", f"最高使用率 {fs_use_max:.0f}%"],
                  note="已过滤 iso9660/devtmpfs/tmpfs 等非持久化文件系统"),
        ])

        # =====================================================================
        # Section 4: 连接与会话
        # =====================================================================
        conns = tables.get("connections", [])
        conn = conns[0] if conns else {}
        states = tables.get("session_states", [])
        cur_conn = safe_int(conn.get("current_connections")) or 0
        max_conn = safe_int(conn.get("max_connections")) or 1
        conn_usage = cur_conn / max_conn * 100 if max_conn > 0 else 0

        conn_rows = [
            _row(检查项="当前连接", 采集值=f"{cur_conn} / {max_conn}", 说明="使用率 {:.1f}%".format(conn_usage)),
            _row(检查项="活跃连接", 采集值=str(conn.get("active_connections", "")), 说明=""),
            _row(检查项="idle in transaction", 采集值=str(conn.get("idle_in_transaction", "") or "0"), 说明="需关注是否堆积"),
        ]
        for s in states[:5]:
            conn_rows.append(_row(检查项=f"  状态: {s.get('state','')}", 采集值=str(s.get("sessions", "")), 说明=""))

        long_tx = tables.get("long_transactions", [])
        lock_waits = tables.get("lock_waits", [])
        conn_issues = []
        if long_tx: conn_issues.append(f"长事务 {len(long_tx)} 个")
        if lock_waits: conn_issues.append(f"锁等待 {len(lock_waits)} 个")
        if conn_usage > 70: conn_issues.append(f"连接使用率 {conn_usage:.0f}%")

        conn_status = "attention" if conn_issues else "normal"
        conn_conclusion = "连接数 {}/{}，使用率 {:.1f}%。".format(cur_conn, max_conn, conn_usage)
        if conn_issues:
            conn_conclusion += "关注：" + "、".join(conn_issues)
        else:
            conn_conclusion += "未发现长事务、锁等待或连接压力。"

        _add_section("connections", "连接与会话", [
            _item("pg.connections", "连接与会话", "tables/connections.tsv", conn_rows,
                  conclusion=conn_conclusion, status=conn_status,
                  evidence=[f"连接 {cur_conn}/{max_conn}",
                            f"长事务 {len(long_tx)}",
                            f"锁等待 {len(lock_waits)}"],
                  recommendation="清理长事务，检查连接池配置" if conn_issues else ""),
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
                                   采集值=_fmt_bytes(r.get("total_bytes")), 说明=""))

        bloat_rows = tables.get("bloat_estimation", [])
        bloat_count = sum(1 for r in bloat_rows
                          if (safe_float(r.get("dead_pct")) or 0) >= 10
                          and (safe_float(r.get("estimated_dead_bytes")) or 0) >= 100 * 1024 * 1024)
        space_note = ""
        if bloat_count:
            space_rows.append(_row(检查项="⚠ 疑似膨胀表", 采集值=f"{bloat_count} 张",
                                   说明=_fmt_bytes(max((safe_float(r.get("estimated_dead_bytes")) or 0 for r in bloat_rows), default=0))))
            space_note = f"检测到 {bloat_count} 张疑似膨胀表"

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
        # Section 7: VACUUM 与维护
        # =====================================================================
        thealth = tables.get("table_health", [])
        dead_total = sum(safe_int(r.get("n_dead_tup")) or 0 for r in thealth)
        stale = sum(1 for r in thealth if not r.get("last_autovacuum") and not r.get("last_vacuum"))
        db_ages = tables.get("database_ages", [])
        max_age = max((safe_int(r.get("xid_age")) or 0 for r in db_ages), default=0)

        vac_issues = []
        if stale > 0: vac_issues.append(f"从未 VACUUM 的表 {stale} 张")
        if dead_total > 100000: vac_issues.append(f"死元组 {dead_total}")
        if max_age > 100000000: vac_issues.append(f"事务年龄 {max_age}")

        vac_rows = [
            _row(检查项="死元组总数", 采集值=str(dead_total), 说明="全库累计"),
            _row(检查项="从未 VACUUM 的表", 采集值=str(stale), 说明="需要关注" if stale > 0 else "正常"),
            _row(检查项="最大事务年龄", 采集值=str(max_age), 说明="接近 20 亿需警惕回卷" if max_age > 100000000 else "正常"),
            _row(检查项="autovacuum_max_workers", 采集值=str(settings.get("autovacuum_max_workers", "")), 说明=""),
        ]
        vac_status = "attention" if vac_issues else "normal"
        vac_conclusion = "死元组 {}，从未 VACUUM 的表 {} 张，最大事务年龄 {}。".format(dead_total, stale, max_age)
        if vac_issues:
            vac_conclusion += "关注：" + "、".join(vac_issues)

        _add_section("vacuum", "VACUUM 与维护", [
            _item("pg.vacuum", "VACUUM 与事务年龄", "tables/table_health.tsv", vac_rows,
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
            rep_rows.append(_row(检查项="复制槽", 采集值=f"共 {len(slots)} 个，非活跃 {inactive}", 说明=""))

        arch = tables.get("archiver", [])
        arch_failed = 0
        if arch:
            a = arch[0]
            arch_failed = int(a.get("failed_count", "0") or "0")
            rep_rows.append(_row(检查项="归档", 采集值=f"成功 {a.get('archived_count','')}", 说明=f"失败 {arch_failed}" if arch_failed else "正常"))

        rep_issues = []
        if any(r.get("state") not in ("streaming",) for r in rep_status_list): rep_issues.append("备库状态异常")
        if slots and sum(1 for r in slots if r.get("active") == "f") > 0: rep_issues.append("存在非活跃复制槽")
        if arch_failed > 0: rep_issues.append(f"归档失败 {arch_failed} 次")

        if not rep_rows:
            rep_rows.append(_row(检查项="复制状态", 采集值="无复制配置", 说明=""))

        rep_status = "attention" if rep_issues else "normal"
        rep_conclusion = f"复制槽 {len(slots)} 个，归档失败 {arch_failed} 次。" if slots else "未配置复制。"
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
        idx_items = [unused, dup, inv_idx if inv_idx else None]

        idx_rows: list[dict] = [
            _row(检查项="未使用索引", 采集值=str(len(unused)), 说明="占用空间且影响写入"),
            _row(检查项="重复索引", 采集值=f"{len(dup)} 组", 说明=""),
            _row(检查项="无效索引", 采集值=str(inv_idx), 说明="CONCURRENTLY 失败后遗留" if inv_idx else "正常"),
        ]
        idx_issues = []
        if unused: idx_issues.append(f"未使用索引 {len(unused)} 个")
        if dup: idx_issues.append(f"重复索引 {len(dup)} 组")
        if inv_idx: idx_issues.append(f"无效索引 {inv_idx} 个")
        idx_status = "attention" if idx_issues else "normal"

        _add_section("indexes", "索引分析", [
            _item("pg.indexes", "索引分析", "tables/indexes.tsv", idx_rows,
                  conclusion="未使用索引 {} 个，重复 {} 组，无效 {} 个。{}".format(
                      len(unused), len(dup), inv_idx,
                      "建议清理冗余索引以释放空间提升写入" if idx_issues else "索引状态正常"),
                  status=idx_status,
                  evidence=idx_issues,
                  recommendation="使用 DROP INDEX CONCURRENTLY 评估清理" if idx_issues else ""),
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
            _row(检查项="认证方式", 采集值=", ".join(sorted(auth_set)) if auth_set else "未知", 说明=""),
            _row(检查项="超级用户", 采集值=str(len(superusers)), 说明=f"可登录 {sum(1 for r in superusers if r.get('rolcanlogin')=='t')}" if len(superusers) >= 2 else ""),
            _row(检查项="可登录角色", 采集值=str(sum(1 for r in roles if r.get("rolcanlogin") == "t")), 说明=""),
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
                log_rows.append(_row(检查项=r.get("category", ""), 采集值=str(r.get("count", "")), 说明=""))
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
                  recommendation="检查 FATAL 日志原因，排查认证失败或连接问题" if fatal_count > 0 else ""),
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
                set_rows.append(_row(检查项=k, 采集值=str(v), 说明=""))

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
            obj_rows.append(_row(检查项=r.get("object_type", ""), 采集值=str(r.get("count", "")), 说明=""))
        parts = tables.get("partition_summary", [])
        if parts:
            max_p = max((safe_int(r.get("partition_count")) or 0 for r in parts), default=0)
            obj_rows.append(_row(检查项="分区表", 采集值=str(len(parts)), 说明=f"最大分区 {max_p}" if max_p else ""))

        _add_section("objects", "对象统计", [
            _item("pg.objects", "数据库对象统计", "tables/object_counts.tsv", obj_rows,
                  conclusion=f"共记录 {len(obj_rows)} 类数据库对象。"),
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
                "version": ident.get("version", ""),
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
