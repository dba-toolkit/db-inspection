"""Oracle 图表生成：集中生成系统与 Oracle 指标图，不参与风险判断。"""

from __future__ import annotations

import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from .metrics import safe_float


def _duration_ms(start_ns: int) -> int:
    return int((time.monotonic_ns() - start_ns) / 1_000_000)


class OracleChartProvider:
    def __init__(self, charts_dir: Path) -> None:
        self.charts_dir = charts_dir

    def generate(self, ctx: Any, metrics: dict[str, Any]) -> list[dict[str, Any]]:
        self.charts_dir.mkdir(parents=True, exist_ok=True)
        try:
            import matplotlib
            matplotlib.use("Agg")
            import matplotlib.pyplot as plt
            import matplotlib.dates as mdates
            from .chart_style import COLOR_MAP, apply_style
            apply_style()
        except ImportError:
            return [{"chart_id": "ALL", "status": "skipped", "reason": "matplotlib_not_installed"}]

        charts: list[dict[str, Any]] = []
        tag = ctx.snapshot.get("instance_tag", "oracle")
        start_ns = time.monotonic_ns()

        def _parse_ts(value: Any):
            text = str(value or "").strip()
            if not text:
                return None
            normalized = re.sub(r"\s+UTC$", "+00:00", text, flags=re.IGNORECASE)
            if normalized.endswith("Z"):
                normalized = normalized[:-1] + "+00:00"
            try:
                return datetime.fromisoformat(normalized)
            except ValueError:
                pass
            try:
                return datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                return None

        def _ts(rows, key="timestamp"):
            result = []
            for row in rows:
                parsed = _parse_ts(row.get(key))
                if parsed is not None:
                    result.append(parsed)
            return result

        def _save(fig, name, pts=0):
            path = self.charts_dir / name
            fig.savefig(path, dpi=180, bbox_inches="tight")
            plt.close(fig)
            charts.append({
                "chart_id": name.replace(".png", ""),
                "path": str(path.resolve()),
                "source_points": pts,
                "duration_ms": _duration_ms(start_ns),
            })

        def _time_line(rows, y_keys, labels, title, filename, ylabel, ylim=None,
                       fill=False, bar=False, stackbar=False, dual_y=None):
            timestamps = _ts(rows)
            if not timestamps or len(timestamps) < 2:
                return
            fig, ax = plt.subplots(figsize=(10, 4.8))
            colors = [COLOR_MAP[c] for c in ["blue", "red", "orange", "green", "purple", "teal"]]
            ax2 = None
            if stackbar:
                bottoms = None
                y_data = [[safe_float(r.get(k)) or 0 for r in rows] for k in y_keys]
                for i, (k, lab) in enumerate(zip(y_keys, labels)):
                    vals = y_data[i]
                    ax.bar(timestamps, vals, bottom=bottoms, color=colors[i], alpha=0.7, label=lab, width=0.0003 * len(timestamps))
                    if bottoms is None:
                        bottoms = vals[:]
                    else:
                        bottoms = [b + v for b, v in zip(bottoms, vals)]
            elif bar:
                y_data = [[safe_float(r.get(k)) or 0 for r in rows] for k in y_keys]
                for i, (k, lab) in enumerate(zip(y_keys, labels)):
                    ax.bar(timestamps, y_data[i], color=colors[i], alpha=0.5, label=lab)
            elif dual_y:
                y1 = [safe_float(r.get(y_keys[0])) or 0 for r in rows]
                y2 = [safe_float(r.get(dual_y[0])) or 0 for r in rows]
                ax.bar(timestamps, y1, color=colors[0], alpha=0.4, label=labels[0])
                ax2 = ax.twinx()
                ax2.plot(timestamps, y2, color=colors[1], linewidth=2, marker="D", markersize=4,
                         label=labels[1] if len(labels) > 1 else dual_y[1])
                ax2.set_ylabel(dual_y[1] if len(dual_y) > 1 else "")
                lines1, labels1 = ax.get_legend_handles_labels()
                lines2, labels2 = ax2.get_legend_handles_labels()
                ax.legend(lines1 + lines2, labels1 + labels2, loc="upper left", frameon=False)
            elif fill:
                y_vals = [safe_float(r.get(y_keys[0])) or 0 for r in rows]
                ax.fill_between(timestamps, y_vals, alpha=0.3, color=colors[0])
                ax.plot(timestamps, y_vals, color=colors[0], linewidth=1.5, label=labels[0] if labels else "")
            else:
                for i, (k, lab) in enumerate(zip(y_keys, labels)):
                    vals = [safe_float(r.get(k)) or 0 for r in rows]
                    ax.plot(timestamps, vals, color=colors[i % len(colors)], linewidth=1.5, label=lab)

            ax.set_title(title)
            ax.set_ylabel(ylabel)
            if not dual_y and not stackbar:
                ax.legend(frameon=False, fontsize=9)
            if ylim:
                ax.set_ylim(*ylim)
            ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M:%S"))
            fig.autofmt_xdate(rotation=0)
            fig.tight_layout()
            _save(fig, filename, pts=len(timestamps))

        sar_cpu_all = ctx.history.get("sar_cpu", [])
        sar_cpu = [r for r in sar_cpu_all if r.get("CPU") in ("-1", "all")] or sar_cpu_all
        cpu_rows = ctx.timeseries.get("system_cpu", [])

        if sar_cpu and len(sar_cpu) >= 2:
            ts_data = _ts(sar_cpu)
            if ts_data and len(ts_data) >= 2:
                users = [safe_float(r.get("%usr")) or 0 for r in sar_cpu]
                syss = [safe_float(r.get("%sys")) or 0 for r in sar_cpu]
                iow = [safe_float(r.get("%iowait")) or 0 for r in sar_cpu]
                fig, ax = plt.subplots(figsize=(10, 4.8))
                ax.stackplot(ts_data, users, syss, iow,
                             labels=["User", "System", "IOWait"],
                             colors=[COLOR_MAP["blue"], COLOR_MAP["teal"], COLOR_MAP["red"]], alpha=0.7)
                ax.set_title(f"CPU Usage (SAR 24h) — {tag}")
                ax.set_ylabel("%"); ax.legend(loc="upper right", frameon=False, fontsize=9)
                ax.set_ylim(0, 105)
                ax.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d %H:%M"))
                ax.xaxis.set_major_locator(mdates.AutoDateLocator())
                fig.autofmt_xdate(rotation=0); fig.tight_layout()
                _save(fig, "system_cpu_sar.png", pts=len(ts_data))
        elif cpu_rows:
            ts_data = _ts(cpu_rows)
            if ts_data and len(ts_data) >= 2:
                busy = [safe_float(r.get("busy_pct")) or 0 for r in cpu_rows]
                iowait = [safe_float(r.get("iowait_pct")) or 0 for r in cpu_rows]
                fig, ax = plt.subplots(figsize=(10, 4.8))
                ax.plot(ts_data, busy, color=COLOR_MAP["blue"], linewidth=1.5, label="Busy%")
                ax.plot(ts_data, iowait, color=COLOR_MAP["red"], linewidth=1.5, label="IOWait%")
                ax.set_title(f"CPU Usage (Sampling) — {tag}")
                ax.set_ylabel("%"); ax.legend(frameon=False, fontsize=9); ax.set_ylim(0, 105)
                ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M:%S"))
                fig.autofmt_xdate(rotation=0); fig.tight_layout()
                _save(fig, "system_cpu_realtime.png", pts=len(ts_data))

        sar_mem = ctx.history.get("sar_memory", [])
        mem_rows = ctx.timeseries.get("system_memory", [])
        if sar_mem and len(sar_mem) >= 2:
            ts_data = _ts(sar_mem)
            if ts_data and len(ts_data) >= 2:
                used = [safe_float(r.get("%memused")) or 0 for r in sar_mem]
                fig, ax = plt.subplots(figsize=(10, 4.8))
                ax.fill_between(ts_data, used, alpha=0.3, color=COLOR_MAP["green"])
                ax.plot(ts_data, used, color=COLOR_MAP["green"], linewidth=1.5, label="Memory %")
                ax.set_title(f"Memory Usage (SAR 24h) — {tag}")
                ax.set_ylabel("%"); ax.set_ylim(0, 105); ax.legend(frameon=False, fontsize=9)
                ax.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d %H:%M"))
                ax.xaxis.set_major_locator(mdates.AutoDateLocator())
                fig.autofmt_xdate(rotation=0); fig.tight_layout()
                _save(fig, "system_memory_sar.png", pts=len(ts_data))
        elif mem_rows:
            ts_data = _ts(mem_rows)
            if ts_data and len(ts_data) >= 2:
                used = [(safe_float(r.get("mem_used_pct")) or 0) for r in mem_rows]
                fig, ax = plt.subplots(figsize=(10, 4.8))
                ax.fill_between(ts_data, used, alpha=0.3, color=COLOR_MAP["green"])
                ax.plot(ts_data, used, color=COLOR_MAP["green"], linewidth=1.5, label="Memory %")
                ax.set_title(f"Memory Usage (Sampling) — {tag}")
                ax.set_ylabel("%"); ax.set_ylim(0, 105); ax.legend(frameon=False, fontsize=9)
                ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M:%S"))
                fig.autofmt_xdate(rotation=0); fig.tight_layout()
                _save(fig, "system_memory.png", pts=len(ts_data))

        net_rows = ctx.timeseries.get("system_network", [])
        if net_rows:
            iface_data: dict[str, dict] = {}
            for r in net_rows:
                iface = r.get("interface", "unknown")
                ts = _parse_ts(r.get("timestamp"))
                rx = safe_float(r.get("rx_bytes_per_sec"))
                tx = safe_float(r.get("tx_bytes_per_sec"))
                if iface not in iface_data:
                    iface_data[iface] = {"ts": [], "rx": [], "tx": []}
                if ts is not None:
                    iface_data[iface]["ts"].append(ts)
                    iface_data[iface]["rx"].append(rx or 0)
                    iface_data[iface]["tx"].append(tx or 0)
            valid = {k: v for k, v in iface_data.items() if len(v["ts"]) >= 2}
            if valid:
                fig, ax = plt.subplots(figsize=(10, 4.8))
                for iface, data in valid.items():
                    ax.plot(data["ts"], [v / 1024 / 1024 for v in data["rx"]],
                            linewidth=1.2, label=f"{iface} RX")
                    ax.plot(data["ts"], [v / 1024 / 1024 for v in data["tx"]],
                            linewidth=1.2, linestyle="--", label=f"{iface} TX")
                ax.set_title(f"Network Throughput (Sampling) — {tag}")
                ax.set_ylabel("MB/s"); ax.legend(loc="upper right", frameon=False, fontsize=8)
                ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M:%S"))
                fig.autofmt_xdate(rotation=0)
                fig.tight_layout()
                _save(fig, "system_network.png", pts=len(net_rows))

        disk_rows = ctx.timeseries.get("system_disk", [])
        if disk_rows:
            dev_data: dict[str, dict] = {}
            for r in disk_rows:
                dev = r.get("device", "unknown")
                ts = _parse_ts(r.get("timestamp"))
                v = safe_float(r.get("util_pct"))
                if dev not in dev_data:
                    dev_data[dev] = {"ts": [], "vals": []}
                if ts is not None and v is not None:
                    dev_data[dev]["ts"].append(ts)
                    dev_data[dev]["vals"].append(v)
            valid = {k: v for k, v in dev_data.items() if len(v["ts"]) >= 2}
            if valid:
                fig, ax = plt.subplots(figsize=(10, 4.8))
                for dev, data in valid.items():
                    ax.plot(data["ts"], data["vals"], linewidth=1.5, label=dev)
                ax.set_title(f"Disk Utilization (Sampling) — {tag}")
                ax.set_ylabel("%"); ax.set_ylim(0, 105)
                ax.legend(loc="upper right", frameon=False, fontsize=8)
                ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M:%S"))
                fig.autofmt_xdate(rotation=0)
                fig.tight_layout()
                _save(fig, "system_disk_util.png", pts=len(disk_rows))

        if sar_cpu and len(sar_cpu) >= 2:
            ts_data = _ts(sar_cpu)
            if ts_data and len(ts_data) >= 2:
                iow_vals = [safe_float(r.get("%iowait")) or 0 for r in sar_cpu]
                if max(iow_vals) > 0:
                    fig, ax = plt.subplots(figsize=(10, 4.8))
                    ax.fill_between(ts_data, iow_vals, alpha=0.3, color=COLOR_MAP["red"])
                    ax.plot(ts_data, iow_vals, color=COLOR_MAP["red"], linewidth=1.0)
                    ax.set_title(f"CPU IOWait (SAR 24h) — {tag}")
                    ax.set_ylabel("%")
                    ax.axhline(y=20, color=COLOR_MAP["orange"], linestyle="--", linewidth=0.8, label="20% warning")
                    ax.legend(frameon=False, fontsize=9)
                    ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
                    fig.autofmt_xdate(rotation=0)
                    fig.tight_layout()
                    _save(fig, "sar_iowait_trend.png", pts=len(ts_data))

        ora_rows = ctx.timeseries.get("oracle_sysstat", [])
        ora = metrics.get("oracle_realtime", {})
        rates = ora.get("derived_rate_series", [])
        if ora_rows and len(ora_rows) >= 2 and rates:
            ts_data = _ts(ora_rows)
            if ts_data and len(ts_data) >= 2:
                x_axis = ts_data[1:]
                commits_r = [safe_float(r.get("user_commits_per_sec")) or 0 for r in rates]
                rollbacks_r = [safe_float(r.get("user_rollbacks_per_sec")) or 0 for r in rates]
                if max(commits_r) > 0 or max(rollbacks_r) > 0:
                    fig, ax = plt.subplots(figsize=(10, 4.8))
                    ax.plot(x_axis, commits_r, color=COLOR_MAP["teal"], linewidth=1.5, marker="o", markersize=4, label="commits/s")
                    ax.plot(x_axis, rollbacks_r, color=COLOR_MAP["red"], linewidth=1.5, marker="s", markersize=4, label="rollbacks/s")
                    ax.set_title(f"Oracle Transaction Rate — {tag}")
                    ax.set_ylabel("txn/s"); ax.legend(frameon=False, fontsize=9)
                    ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M:%S"))
                    fig.autofmt_xdate(rotation=0); fig.tight_layout()
                    _save(fig, "oracle_txn_rate.png", pts=len(x_axis))

                reads_r = [safe_float(r.get("physical_reads_per_sec")) or 0 for r in rates]
                writes_r = [safe_float(r.get("physical_writes_per_sec")) or 0 for r in rates]
                if max(reads_r) > 0 or max(writes_r) > 0:
                    fig, ax = plt.subplots(figsize=(10, 4.8))
                    ax.plot(x_axis, reads_r, color=COLOR_MAP["blue"], linewidth=1.5, marker="o", markersize=4, label="reads/s")
                    ax.plot(x_axis, writes_r, color=COLOR_MAP["red"], linewidth=1.5, marker="s", markersize=4, label="writes/s")
                    ax.set_title(f"Oracle Physical IO Rate — {tag}")
                    ax.set_ylabel("IO/s"); ax.legend(frameon=False, fontsize=9)
                    ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M:%S"))
                    fig.autofmt_xdate(rotation=0); fig.tight_layout()
                    _save(fig, "oracle_physical_io.png", pts=len(x_axis))

                logical_r = [safe_float(r.get("session_logical_reads_per_sec")) or 0 for r in rates]
                if max(logical_r) > 0:
                    fig, ax = plt.subplots(figsize=(10, 4.8))
                    ax.plot(x_axis, logical_r, color=COLOR_MAP["teal"], linewidth=1.5, marker="o", markersize=4, label="logical reads/s")
                    ax.plot(x_axis, reads_r, color=COLOR_MAP["red"], linewidth=1.5, marker="s", markersize=4, label="physical reads/s")
                    ax.set_title(f"Oracle Reads: Logical vs Physical — {tag}")
                    ax.set_ylabel("reads/s"); ax.legend(frameon=False, fontsize=9)
                    ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M:%S"))
                    fig.autofmt_xdate(rotation=0); fig.tight_layout()
                    _save(fig, "oracle_logical_vs_physical.png", pts=len(x_axis))

                redo_r = [safe_float(r.get("redo_size_per_sec")) or 0 for r in rates]
                if max(redo_r) > 0:
                    fig, ax = plt.subplots(figsize=(10, 4.8))
                    ax.fill_between(x_axis, [v / 1024 / 1024 for v in redo_r], alpha=0.3, color=COLOR_MAP["orange"])
                    ax.plot(x_axis, [v / 1024 / 1024 for v in redo_r], color=COLOR_MAP["orange"], linewidth=1.5, marker="o", markersize=4, label="redo MB/s")
                    ax.set_title(f"Oracle Redo Rate — {tag}")
                    ax.set_ylabel("MB/s"); ax.legend(frameon=False, fontsize=9)
                    ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M:%S"))
                    fig.autofmt_xdate(rotation=0); fig.tight_layout()
                    _save(fig, "oracle_redo_rate.png", pts=len(x_axis))

                execs_r = [safe_float(r.get("execute_count_per_sec")) or 0 for r in rates]
                hard_r = [safe_float(r.get("parse_count_hard_per_sec")) or 0 for r in rates]
                if max(execs_r) >= 1:
                    ratio = [h / (e or 1) * 100 for h, e in zip(hard_r, execs_r)]
                    fig, ax1 = plt.subplots(figsize=(10, 4.8))
                    ax1.plot(x_axis, execs_r, color=COLOR_MAP["teal"], linewidth=1.5, marker="o", markersize=4, label="exec/s")
                    ax2 = ax1.twinx()
                    ax2.plot(x_axis, ratio, color=COLOR_MAP["red"], linewidth=2, marker="D", markersize=4, label="hard parse %")
                    ax1.set_title(f"Oracle Exec Rate & Hard Parse % — {tag}")
                    ax1.set_ylabel("exec/s"); ax2.set_ylabel("hard parse %")
                    lines1, labels1 = ax1.get_legend_handles_labels()
                    lines2, labels2 = ax2.get_legend_handles_labels()
                    ax1.legend(lines1 + lines2, labels1 + labels2, loc="upper left", frameon=False, fontsize=9)
                    ax1.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M:%S"))
                    fig.autofmt_xdate(rotation=0); fig.tight_layout()
                    _save(fig, "oracle_parse_ratio.png", pts=len(x_axis))

        risk_data = metrics.get("_risk_counts", {})
        if risk_data:
            labels = [x for x in ["critical", "high", "medium", "low"] if risk_data.get(x)]
            values = [risk_data.get(x, 0) for x in labels]
            if labels:
                cn_names = {"critical": "严重", "high": "高", "medium": "中", "low": "低"}
                fig, ax = plt.subplots(figsize=(6.4, 3.3))
                ax.bar([cn_names[x] for x in labels], values, color=[COLOR_MAP[x] for x in labels], width=0.58)
                ax.set_title(f"Risk Severity — {tag}")
                ax.set_ylabel("数量")
                fig.tight_layout()
                _save(fig, "risk_severity.png")

        return charts
