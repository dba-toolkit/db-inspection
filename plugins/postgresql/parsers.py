"""Low-level parsers for PostgreSQL collector evidence.

SAR history parsing lives in ``inspection_core`` — the series come from
``charts.history`` and the coverage estimate from ``sampling``.  This module only
keeps the parsers that read PostgreSQL-specific collector files.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from inspection_core.values import safe_float


def _parse_df_pt(path: Path) -> list[dict[str, Any]]:
    """Parse df -PT output (space-separated with multi-word mountpoints)."""
    result: list[dict[str, Any]] = []
    if not path.exists():
        return result
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    if len(lines) < 2:
        return result
    # Skip header, parse each line from the end (mountpoint may contain spaces)
    for line in lines[1:]:
        parts = line.split()
        if len(parts) < 7:
            continue
        # Last field is mountpoint; second-to-last is capacity (NN%)
        cap_str = parts[-2].rstrip("%")
        mp = parts[-1]
        result.append({
            "filesystem": parts[0],
            "fstype": parts[1],
            "blocks": safe_float(parts[2]),
            "used": safe_float(parts[3]),
            "available": safe_float(parts[4]),
            "usage_percent": safe_float(cap_str),
            "mountpoint": mp,
        })
    return result


def _parse_free_b(path: Path) -> dict[str, Any] | None:
    """Parse free -b output, returning Mem: row with bytes values."""
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8", errors="replace")
    for line in text.splitlines():
        if line.startswith("Mem:"):
            parts = line.split()
            if len(parts) >= 7:
                return {
                    "total": safe_float(parts[1]),
                    "used": safe_float(parts[2]),
                    "free": safe_float(parts[3]),
                    "available": safe_float(parts[6]),
                }
    return None


# ---------------------------------------------------------------------------
# 网络 / 进程 / 能力 —— 采集器一直在采、报告层原先**一次都没读**
# ---------------------------------------------------------------------------
#
# 判据背景（`docs/os-collector-crossdb-alignment.md` §1G）：`system.ip_address` /
# `system.ip_route` / `system.socket_summary` / `system.postgres_processes` /
# `pg.capability_probe` 在台账里全是 `ok`，行数也都够（17 / 16 / 10 / 16 / 15），
# 但 `presentation.py` 里没有任何一处引用它们 —— 报告因此说不出「主机有几张网卡、
# 监听在哪、后台进程齐不齐、哪些统计视图可用」。**这不是"缺采集"，是"采了没报"**。
#
# 解析器一律吃**行列表**而不是 `Path`：由调用方传 `ctx.raw_lines("xxx.txt")`，
# 「在 tables/ 还是 evidence/ 里找」由上下文统一负责。路径写死的后果是采集器一移
# 目录就**静默读空**（见 `PackageContext.raw_lines` 的注释）。

#: `ip -details addr show` 的接口首行：`2: ens160: <BROADCAST,...> mtu 1500 ...`
_IP_IFACE_RE = re.compile(r"^(\d+):\s+([^\s:]+):\s+<([^>]*)>\s*(.*)$")
#: `ifconfig -a` 的接口首行：`ens160: flags=4163<UP,BROADCAST,...>  mtu 1500`
_IFCONFIG_IFACE_RE = re.compile(r"^([^\s:]+):\s+flags=[0-9a-fx]+<([^>]*)>\s+mtu\s+(\d+)")
#: `postgres: <role ...>` 形式的进程命令行
_PG_PROC_RE = re.compile(r"^postgres:\s+(.+)$")

#: PG 后台进程的角色前缀。**顺序有意义**：长的放前面，避免 `walwriter` 先命中
#: `wal` 之类的短前缀（本表里没有 `wal`，但新增条目时容易踩）。
#:
#: ★ 这张表同时承担"**把 client backend 排除掉**"的判据：backend 的命令行是
#: `postgres: <db> <user> <host>(<port>) <state>`，首词是库名，不会命中任何前缀。
_BG_ROLE_PREFIXES: tuple[str, ...] = (
    "background writer",
    "logical replication launcher",
    "logical replication worker",
    "autovacuum launcher",
    "autovacuum worker",
    "parallel worker",
    "stats collector",
    "checkpointer",
    "walreceiver",
    "walsender",
    "walwriter",
    "archiver",
    "startup",
    "logger",
    "io worker",
)


def parse_ip_addr(rows: list[str]) -> list[dict[str, Any]]:
    """`ip -details addr show`（或 `ifconfig -a`）→ 每张网卡一条。

    两种形态都要认：采集器按 `has_cmd` 二选一（`system.ifconfig` 是 `ip` 缺失时的
    互斥备选），只认一种的话换台老机器就**静默变空**。

    返回字段：``name`` / ``up`` / ``mtu`` / ``mac`` / ``ipv4``（``[{addr, secondary}]``）
    / ``ipv6``（``[addr]``）/ ``source``（``ip`` 或 ``ifconfig``，用于说明列披露）。
    """
    ifaces: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None

    for raw in rows:
        line = raw.rstrip()
        if not line.strip():
            continue

        matched = _IP_IFACE_RE.match(line)
        if matched:
            current = {
                "name": matched.group(2),
                "up": "UP" in matched.group(3).split(","),
                "mtu": "",
                "mac": "",
                "ipv4": [],
                "ipv6": [],
                "source": "ip",
            }
            mtu = re.search(r"\bmtu\s+(\d+)", line)
            if mtu:
                current["mtu"] = mtu.group(1)
            ifaces.append(current)
            continue

        matched = _IFCONFIG_IFACE_RE.match(line)
        if matched:
            current = {
                "name": matched.group(1),
                "up": "UP" in matched.group(2).split(","),
                "mtu": matched.group(3),
                "mac": "",
                "ipv4": [],
                "ipv6": [],
                "source": "ifconfig",
            }
            ifaces.append(current)
            continue

        if current is None:
            continue

        token = line.strip()
        # MAC：`ip` 是 `link/ether aa:bb:..`，`ifconfig` 是 `ether aa:bb:..`
        if token.startswith("link/ether "):
            current["mac"] = token.split()[1]
        elif token.startswith("ether "):
            current["mac"] = token.split()[1]
        # IPv4：`ip` 带 `/24`，`ifconfig` 不带（netmask 另给）
        elif token.startswith("inet "):
            parts = token.split()
            if len(parts) >= 2:
                current["ipv4"].append({
                    "addr": parts[1],
                    # `ip addr` 会在 scope global 之后打 `secondary`
                    "secondary": "secondary" in parts,
                })
        elif token.startswith("inet6 "):
            parts = token.split()
            if len(parts) >= 2:
                current["ipv6"].append(parts[1])

    return ifaces


def parse_ip_route(rows: list[str]) -> dict[str, list[dict[str, str]]]:
    """`ip route show table all` → 默认路由 + main 表的直连网段。

    ★ 排除 ``table local`` 这一层是**防御性**的，别拿它当守门断言：那是内核为本机
    地址自动生成的条目，但实测（iproute2 6.x）这些行的首列是 ``local`` / ``broadcast``
    关键字，本来就命中不了下面的 ``"/" in parts[0]`` 判据 —— 批 3B-B1 的负控制 M5
    正是这么暴露出一条"永远绿"的假设门。留着它是为了某天出现 "CIDR 开头 +
    table local" 的格式时，不至于把 127.0.0.0/8 当成"人配的网段"。

    返回 ``{"default": [{gateway, dev, metric}], "links": [{cidr, dev, src}]}``。
    """
    default: list[dict[str, str]] = []
    links: list[dict[str, str]] = []

    for raw in rows:
        line = raw.strip()
        if not line or "table local" in line:
            continue
        parts = line.split()
        if not parts:
            continue

        def _after(key: str) -> str:
            return parts[parts.index(key) + 1] if key in parts and parts.index(key) + 1 < len(parts) else ""

        if parts[0] == "default":
            default.append({
                "gateway": _after("via"),
                "dev": _after("dev"),
                "metric": _after("metric"),
            })
        elif "/" in parts[0] and "dev" in parts:
            links.append({"cidr": parts[0], "dev": _after("dev"), "src": _after("src")})

    return {"default": default, "links": links}


def parse_socket_summary(rows: list[str]) -> dict[str, int | None]:
    """`ss -s` → 套接字总数 / TCP 状态分布 / UDP 与 RAW 计数。

    认不出的键留 ``None`` 而不是 0 —— 「没取到」与「确实是 0」在报告里必须能区分。
    """
    out: dict[str, int | None] = {
        "total": None, "tcp": None, "estab": None,
        "timewait": None, "udp": None, "raw": None,
    }

    for raw in rows:
        line = raw.strip()
        matched = re.match(r"^Total:\s*(\d+)", line)
        if matched:
            out["total"] = int(matched.group(1))
            continue

        matched = re.match(r"^TCP:\s*(\d+)", line)
        if matched:
            out["tcp"] = int(matched.group(1))
            for key in ("estab", "timewait"):
                sub = re.search(key + r"\s+(\d+)", line)
                if sub:
                    out[key] = int(sub.group(1))
            continue

        parts = line.split()
        # 表体：`UDP\t4\t3\t1`（`Transport Total IP IPv6`）
        if len(parts) >= 2 and parts[0] in ("UDP", "RAW"):
            try:
                out[parts[0].lower()] = int(parts[1])
            except ValueError:
                pass

    return out


def parse_postgres_processes(rows: list[str]) -> dict[str, Any]:
    """`ps -eo pid,ppid,user,etimes,%cpu,%mem,args` → 本实例进程与后台角色集合。

    ★★ **采集端判据是脏的，这里必须重判**：采集器写的是
    ``ps ... | awk '/[p]ostgres/'``，那是对**整行**做匹配 —— USER 列是 ``postgres``
    的进程全部命中，于是 ``su - postgres`` 之后整条 shell 树（``-bash``、
    ``bash pg_inspection_standard.sh``、``sleep``、``ps``、``awk``）都被采了进来。
    真机实测 16 行里 **8 行是采集脚本自己**，台账却记 ``ok / row_count=16``，
    报告若照抄就会说出"16 个 postgres 进程"。

    这里按 **COMMAND** 重判：只有 ``postgres: <role>``（后台/后端进程）与
    ``<...>/postgres``（postmaster）算本实例进程，其余进 ``excluded`` —— 调用方
    要把 ``len(excluded)`` 印进说明列，让「已过滤」与「没采到」区分得开。

    返回：``postmaster`` / ``workers``（每条带 ``role``）/ ``backends`` /
    ``excluded`` / ``roles`` / ``archive_last``。
    """
    postmaster: list[dict[str, Any]] = []
    workers: list[dict[str, Any]] = []
    backends: list[dict[str, Any]] = []
    excluded: list[str] = []
    archive_last: str | None = None

    for raw in rows:
        # args 里可能有空格，所以只切前 6 个字段
        parts = raw.split(None, 6)
        if len(parts) < 7:
            continue
        pid, ppid, user, etimes, cpu, mem, cmd = parts
        cmd = cmd.strip()
        if not cmd:
            continue

        record = {
            "pid": pid, "ppid": ppid, "user": user,
            "etimes": safe_float(etimes), "cpu": cpu, "mem": mem, "cmd": cmd,
        }

        matched = _PG_PROC_RE.match(cmd)
        if matched:
            role_raw = matched.group(1).strip()
            if role_raw.startswith("archiver last was "):
                archive_last = role_raw[len("archiver last was "):].strip().split()[0] if role_raw[len("archiver last was "):].strip() else None
            for prefix in _BG_ROLE_PREFIXES:
                if role_raw == prefix or role_raw.startswith(prefix + " "):
                    record["role"] = prefix
                    workers.append(record)
                    break
            else:
                # `postgres: <db> <user> <host>(<port>) <state>` —— client backend
                record["role"] = role_raw
                backends.append(record)
            continue

        # postmaster：可执行文件本身，且不带 `postgres:` 前缀
        if re.search(r"(^|/)postgres$", cmd):
            postmaster.append(record)
            continue

        excluded.append(cmd)

    return {
        "postmaster": postmaster,
        "workers": workers,
        "backends": backends,
        "excluded": excluded,
        "roles": [str(w["role"]) for w in workers],
        "archive_last": archive_last,
    }


def parse_capabilities(rows: list[str]) -> list[dict[str, str]]:
    """`capabilities.tsv`（两列 ``capability`` / ``value``）→ 能力键值对。

    首行是表头，跳过；空值的行保留（``value=""`` 本身是"探测到了但没取到"的证据）。
    """
    out: list[dict[str, str]] = []
    for raw in rows:
        parts = raw.split("\t")
        if len(parts) < 2:
            continue
        key = parts[0].strip()
        if not key or key == "capability":
            continue
        out.append({"capability": key, "value": parts[1].strip()})
    return out


def parse_numa(rows: list[str]) -> dict[str, Any]:
    """`numactl --hardware` → NUMA 节点数与每节点的 CPU / 内存。

    单节点时 ``nodes`` 为 1；**取不到时返回 ``nodes: 0``**（调用方据此不出该行，
    而不是印"1 个节点"）。
    """
    nodes = 0
    per_node: dict[int, dict[str, Any]] = {}

    for raw in rows:
        line = raw.strip()
        matched = re.match(r"^available:\s*(\d+)\s+nodes", line)
        if matched:
            nodes = int(matched.group(1))
            continue
        matched = re.match(r"^node\s+(\d+)\s+cpus:\s*(.*)$", line)
        if matched:
            per_node.setdefault(int(matched.group(1)), {})["cpus"] = matched.group(2).strip()
            continue
        matched = re.match(r"^node\s+(\d+)\s+size:\s*(\d+)\s*MB", line)
        if matched:
            per_node.setdefault(int(matched.group(1)), {})["size_mb"] = int(matched.group(2))
            continue
        matched = re.match(r"^node\s+(\d+)\s+free:\s*(\d+)\s*MB", line)
        if matched:
            per_node.setdefault(int(matched.group(1)), {})["free_mb"] = int(matched.group(2))

    return {"nodes": nodes, "per_node": per_node}
