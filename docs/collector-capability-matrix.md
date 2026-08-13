# 四种数据库采集器能力对照

> 目标：统一系统层面的采集语义、状态、字段和安全边界，同时保留四个数据库独立入口和专属 SQL。

## 1. 代码重复度实测

| 对比 | 公共函数 | 归一化代码行 Jaccard | 结论 |
|---|---:|---:|---|
| MySQL vs PostgreSQL | 55 个 | 0.385 | 同源程度很高，应首先抽公共 Linux 采集层 |
| MySQL vs Oracle | 36 个 | 0.073 | 基础框架和系统采样可共用，Oracle 数据库模块保持独立 |
| PostgreSQL vs Oracle | 35 个 | 0.069 | 同上 |

MySQL 与 PostgreSQL 的真实采集包共有 23 个系统/基础状态 ID：

```text
module.system_static
package.security_scan
system.backup_cron
system.backup_processes
system.backup_timers
system.block_devices
system.chronyc_sources
system.chronyc_tracking
system.filesystems
system.free
system.hugepages
system.inodes
system.ip_address
system.ip_route
system.lscpu
system.numa
system.os_release
system.sar_history
system.socket_summary
system.sysctl_selected
system.time_status
system.timedatectl
timeseries.realtime_sampling
```

这说明公共系统采集不是理论设想，而是已经存在、只是被复制在两份脚本中。

## 2. Linux 公共系统采集

| 能力 | MySQL | PostgreSQL | Oracle | 统一方案 |
|---|---|---|---|---|
| OS/内核/uptime | 有 | 有 | 有 | 统一 `system.os_release` schema |
| 主机/IP/本地目标判断 | 有 | 有 | 有基础识别 | 统一 host identity，并强制记录数据库目标是否为本机 |
| 时间、时区、NTP/Chrony | 有 | 有 | 部分 | 使用 MySQL/PG 的完整时间证据模型 |
| CPU/内存现场采样 | 有 | 有 | 有 | 合并采样函数，数据库活动采样作为 callback |
| 磁盘 util/await | 有 | 有 | 有 | 统一列名、单位和设备过滤 |
| 网络吞吐 | 有 | 有 | 有 | 统一排除 lo 和虚拟接口策略 |
| 文件系统/ inode | 有 | 有 | 有 | 统一过滤和容量 schema |
| block device | 有 | 有 | 有部分信息 | 统一旋转盘、调度器、型号等字段 |
| NUMA/THP/sysctl | 有 | 有 | 有部分 | 统一 Linux 基础项，数据库规则决定是否评价 |
| SAR 历史与覆盖率 | 有 | 有 | 有 | 采用相同 coverage schema 和质量判断 |
| iostat 快照 | 无 | 有 | 有部分 | 将 PostgreSQL 的实现提升到公共 Linux 层 |
| dmesg 错误摘要 | 无 | 有 | 告警日志为主 | 将 PostgreSQL 的安全过滤实现提升到公共 Linux 层 |
| mounts 详情 | 无 | 有 | 有部分 | 提升到公共 Linux 层，用于挂载参数分析 |
| 备份进程/cron/timer 线索 | 有 | 有 | RMAN 专属更强 | OS 线索共用；数据库备份验证仍由插件负责 |
| 敏感内容扫描 | 有 | 有 | 有 | 统一扫描器、阻断级别和允许列表 |
| manifest SHA256 | 有 | 有 | 有 | 完全共用 |
| collection status | 有统一 JSON | 有统一 JSON | 使用 status TSV，命名不稳定 | Oracle adapter 先转换，后续 collector 原生对齐 |

## 3. 数据库专属采集边界

以下内容不进入公共系统采集模块：

### MySQL

- global variables/status、InnoDB、Performance Schema。
- MGR/异步复制/GTID/wsrep。
- Schema、索引、SQL digest、账号和 MySQL 日志。
- `my.cnf` 白名单和 mysqld 进程细节。

### PostgreSQL

- settings、pg_stat_*、VACUUM、事务年龄和膨胀。
- WAL、复制槽、流复制和归档。
- pg_hba、安全、扩展和 PostgreSQL 进程细节。

### Oracle

- RAC、ASM、CDB/PDB、Data Guard、DG Broker。
- 表空间、SGA/PGA、Redo/Undo、RMAN、ADR、AWR。
- Diagnostics Pack 许可和本地 OS 认证边界。

### SQL Server

- DMV、TempDB、VLF、DBCC、备份历史和 Agent。
- Always On、镜像、日志传送、复制。
- SQL Server 配置、等待、计划缓存、索引和安全身份。

## 4. 当前需要纠正的问题

### Oracle 状态 ID

真实包中出现：

```text
status/module._AWR.tsv
status/module..tsv
status/sql..tsv
```

这表示状态 ID 可能由中文标题或非稳定文本生成。状态标识必须改为稳定英文 ID，例如：

```text
module.oracle.awr
module.oracle.asm
oracle.performance.sga
oracle.backup.rman
```

显示标题与 check_id 必须分开。

### SQL Server 状态 ID

当前 38 个 module status 使用中文 `name`，例如“数据库清单与完整性”“备份状态”。中文标题适合显示，不适合作为长期接口键。应增加：

```text
check_id: sqlserver.database.inventory
title: 数据库清单与完整性
```

同时补齐：category、schema_version、started_at、finished_at、error_code、artifact 和 reason。

### SQL Server manifest

当前 manifest 主要保存文件名。目标应与其他三套一致，至少记录：

- path
- bytes
- sha256
- media_type/format

### 远程主机归属

采集器运行主机不一定等于数据库服务器。所有 OS 指标必须记录：

```text
database_target_is_local
observed_host
target_host
collection_method
```

如果不能证明指标属于目标数据库主机，报告只能展示为“采集端主机信息”，不得参与数据库服务器资源规则。

## 5. 取长补短结论

1. MySQL/PG 的系统采集作为公共 Linux 层初始基线。
2. 把 PG 的 iostat、dmesg、mounts 补入公共层。
3. 把 Oracle 的许可、超时、模块开关和复杂环境能力探测保留为可复用框架能力。
4. 把 MySQL/PG/Oracle 的 manifest 哈希和安全扫描标准推广到 SQL Server。
5. 把 SQL Server 的结构化 snapshot 经验用于公共 snapshot envelope，但不要求 Linux collector 立即改成单 JSON。
6. 所有数据库使用稳定 check_id、统一状态和逐检查项 schema_version。

