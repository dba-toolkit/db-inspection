# PostgreSQL 采集器 → Python 对接契约（INTERFACE）

> 对象：`inspection/pg_inspection_standard.sh`（`COLLECTOR_VERSION=2.0.1`，`SNAPSHOT_SCHEMA_VERSION=1.0`，`PACKAGE_VERSION=1.0`）
> 对应下游：`plugins/postgresql/*` + `inspection_core/*`
> 配套文档：[`postgresql-collector-fix-plan.md`](./postgresql-collector-fix-plan.md)（改造清单，缺一不可）
>
> **行号基准**：`pg_inspection_standard.sh` = 1362 行 / 97609 字节 / 纯 LF / 无 BOM。**改脚本后行号必漂，找不到就按符号名（函数名 / item_id / 键名）搜。**

---

## 目录

1. [定位与规模](#1-定位与规模)
2. [「乱不乱」的判定 + 错位清单](#2-乱不乱的判定--错位清单)
3. [执行阶段地图](#3-执行阶段地图)
4. [采集项总清单](#4-采集项总清单)
5. [输出目录树（标注 Python 入口）](#5-输出目录树标注-python-入口)
6. [四个入口文件的字段表](#6-四个入口文件的字段表)
7. [status 语义表](#7-status-语义表)
8. [退出码表](#8-退出码表)
9. [变更规则](#9-变更规则)
10. [已知契约缺陷 + 建议的重构顺序](#10-已知契约缺陷--建议的重构顺序)

---

## 1. 定位与规模

| 项 | 值 |
|---|---|
| 定位 | **只读采集 + 时序采样 + 状态登记 + 打包**。**不做**风险判断、不做图表、不生成报告 |
| 规模 | 1362 行 / 61 函数 / 注释 **31 行（2.3%）** / 49 张 `tables/*.tsv` / 93 个 item_id 字符串（约 75 个具体项）|
| 版本号 | `COLLECTOR_VERSION=2.0.1`、`SNAPSHOT_SCHEMA_VERSION=1.0`、`PACKAGE_VERSION=1.0` |
| 必需依赖 | `bash ≥ 4.0`、`psql` |
| 可选依赖 | `sar` `sadf` `lscpu` `free` `df` `lsblk` `mount` `ip`/`ifconfig` `ss` `numactl` `sysctl` `dmesg` `iostat` `crontab` `systemctl` `timedatectl` `chronyc`/`ntpq` `pgbackrest` `barman` `patronictl`/`repmgr` |
| 默认输出 | `OUTPUT_PARENT`（默认 `/var/tmp`）下 `pg_inspection_v2_<host>_<addr>_<port>_<YYYYmmdd_HHMMSS>/` |
| 全局约定 | `umask 077`、`LC_ALL=C`、`LANG=C`、`set -o pipefail`、**`set +e`**（**故意**：所有失败必须走到 `record_status` 落账，不能直接 abort）|
| 只读保证 | `PGOPTIONS="-c default_transaction_read_only=on -c statement_timeout=<N>000 -c lock_timeout=5000"`，`PGAPPNAME=pg_inspection_v<ver>` |

## 2. 「乱不乱」的判定 + 错位清单

**结论：骨架不乱，边界乱。**

### 2.1 骨架（做得好的，明确保留）

- **职责切分清楚**：`capture`/`pg_query_*`（原语）→ `record_status`（状态）→ `run_module`（模块）→ `collect_*`（领域）→ `generate_*`（产物）→ `security_scan`/`create_package`（交付）。
- **失败不 abort**：`set +e` + `classify_failure` + `record_status` ⇒ 单项失败不影响整体采集。这是**对的**，不要为了"早点发现错误"改成 `set -e`。
- **只读会话级兜底**：`PGOPTIONS` 三重保护（只读 + statement_timeout + lock_timeout），**不是靠 DBA 自觉**。
- **口令不进包**：`--password-file` 优先、`umask 077`、`cleanup_auth` 在正常/信号两条路径都调、`tar --exclude='*/.pgpass.*'`、`generate_manifest` 排除 `.pgpass.*`、`security_scan` 兜底。
- **时间全 UTC 对齐**：`iso_now` 用 `date +%Y-%m-%dT%H:%M:%S%z`，SAR 时间戳统一转 UTC（L567-568），避免 Python 混画两条差 8 小时的曲线。
- **SAR 用 `sadf -d` + Python 用 `parse_sadf`**：`package_adapter.py` L68-74 注释记录了"用 `parse_csv` 读 sadf 会把每张 history 图读空"——**下游已经按分隔符语义正确接线**。

### 2.2 边界错位（逐条对应 FIX-PLAN）

| # | 错位 | F |
|:---:|---|:---:|
| 1 | 采集原语不保证行结构（值内 TAB/LF 撑破 TSV，官方手册明确不转义） | F-01 |
| 2 | 占位表头只覆盖 **23/49** 张表；逐库版比占位表头多一个 `database_name` | F-02 |
| 3 | 可选命令缺 `else` ⇒ 缺项**不登记** ⇒ 完整度分母变小、分数虚高 | ✅ **已修**（F-08，2026-10-16）|
| 4 | `item_id` 与 `category` 不一一对应（`ha.*` 挂进 `pg.backup`） | F-09 |
| 5 | 同名表两份落点，靠 `dict.update` 的覆盖语义**偶然正确** | F-10 |
| 6 | 注释密度 2.3%：**难点都注释了，接口一行没写** | F-11 |
| 7 | `snapshot.json` **没有 `collection_status` 键**，而展示层在读它 | F-14 |
| 8 | 展示层 item_id（18 个）与采集层 item_id 是两套命名空间，5 个撞 `category` 名 | F-15 |
| 9 | 规则引擎的 `available` 维恒真 ⇒ 采集与判定脱钩 | F-12 |
| 10 | 末尾退出码被最后一行 `&&` 链决定 | F-07 |

## 3. 执行阶段地图

```
[参数解析 / 校验]                                  L1213-1240   串行
   ├─ is_uint 校验端口与时间参数（L1236-1237）
   ├─ bash >= 4.0 校验（L1238）
   ├─ has_cmd psql 校验（L1239）
   ├─ mkdir -p OUTPUT_PARENT（L1240）
   └─ check_output_space（L1241 → 不足则 exit 11）

[环境初始化]                                       L1243-1268  串行
   ├─ 探测 主机名/FQDN/全部IPv4/主IPv4（L1244-1247）
   ├─ resolve_ipv4 + is_local_connect_target（L1248-1249）
   ├─ INSTANCE_ADDRESS 归一（L1250，socket/127.*/localhost → 采集端主 IP）
   ├─ TASK_DIR / 6 个目录 建立（L1252-1255）
   ├─ 产物路径常量（L1256-1259）
   ├─ PGOPTIONS / PGAPPNAME 注入（L1265-1266）
   └─ HAS_SAR / HAS_SADF（L1267-1268）

[认证]                                             L1275-1299  串行
   ├─ --password-file → PGPASSWORD → 交互（仅 socket 连接跳过交互）
   └─ export PGPASSWORD（若设）

[连通性检查]                                       L1301-1312  串行（失败 exit 20）
   └─ pg_exec -c 'SELECT 1'

[能力探测]                                         run_module "pg.capabilities" probe_capabilities  串行
   └─ 版本/版本号/主版本、data_directory、is_in_recovery、wal_level、archive_mode
      max_replication_slots、max_wal_senders
      pg_stat_statements（扩展 + 表）、pg_stat_wal(≥14)、pg_stat_io(≥16)、pg_stat_checkpointer(≥17)
      replication_active、HAS_SAR/HAS_SADF
      → tables/capabilities.tsv + item pg.capability_probe

[后台并行 3 个]                                    start_module_bg ×3    并行
   ├─ realtime_sampling  → collect_realtime_samples     （采样 SAMPLE_COUNT × SAMPLE_INTERVAL 秒）
   ├─ sar_history        → collect_sar_history          （sadf 导 8 类指标 + coverage 计算）
   └─ system_static      → collect_system_static        （29 类静态项 + 实时快照）

[前台串行 5 个]                                    run_module ×5         串行（**故意**，避免并发压力）
   ├─ pg_basic         → collect_pg_basic              （11 项）
   ├─ pg_performance   → collect_pg_performance        （23 项）
   ├─ pg_replication   → collect_pg_replication        （4 项）
   ├─ pg_objects       → collect_pg_objects            （**逐库循环**，每库 15 项）
   └─ pg_logs_backup   → collect_pg_logs_backup        （8 项）

[等待后台并收账]                                   wait_background_modules  L1330

[角色推导 / 清理认证]                              L1333-1334  串行

[产物生成]                                         L1337-1339  串行
   ├─ generate_collection_status_json
   ├─ generate_snapshot_json
   └─ generate_summary

[安全扫描 + 打包]                                  L1340-1353  串行（分支）
   ├─ 通过 → 重生成三项 + generate_manifest + rm -rf .tmp/.status_parts + create_package
   └─ 拦截 → 重生成三项 + log_error + rm -rf .tmp/.status_parts（**不打包**）

[收尾打印]                                         L1357-1362
```

**串/并行的设计意图（必须保留）**：PG 数据模块**串行**（L1324 注释："避免对生产库造成过高并发"）；主机侧的三项**并行**（不碰数据库）。

## 4. 采集项总清单

`category` 取值共 **9 个**：`system.static` `system.history` `timeseries` `pg.basic` `pg.performance` `pg.replication` `pg.objects` `pg.logs` `pg.backup` `pg.capabilities` `package` `module`（其中 `module` 由 `run_module` 统一登记）。

> ⚠ **`pg.backup` 这个 category 混装了三类东西**：备份工具（`backup.*`）、高可用（`ha.*`）、主机定时任务（`system.backup_*`）——见 FIX-PLAN F-09。

### 4.1 系统静态（`system.static`）

| item_id | 输出文件 | 依赖 | 缺失时 |
|---|---|---|---|
| `system.os_release` | `evidence/os_release.txt` | — | — |
| `system.time_status` | `evidence/time_status.txt` | `date` | — |
| `system.timedatectl` | `evidence/timedatectl.txt` | `timedatectl` | ✅ **已修**（F-08）⇒ `unsupported` |
| `system.chronyc_tracking` | `evidence/chronyc_tracking.txt` | `chronyc` | ✅ **已修**（F-08）⇒ `unsupported` |
| `system.chronyc_sources` | `evidence/chronyc_sources.txt` | `chronyc` | ✅ **已修**（F-08）⇒ `unsupported` |
| `system.ntpq_peers` | `evidence/ntpq_peers.txt` | `ntpq` | ✅ **已修**（F-08）⇒ `unsupported` |
| `system.lscpu` | `evidence/lscpu.txt` | `lscpu` | `unsupported` ✅ |
| `system.free` | `tables/memory_snapshot.tsv` | `free` | ✅ **已修**（F-08）⇒ `unsupported` |
| `system.filesystems` | `tables/filesystems.tsv` | `df` | ✅ **已修**（F-08）⇒ `unsupported` |
| `system.inodes` | `tables/inodes.tsv` | `df` | ✅ **已修**（F-08）⇒ `unsupported` |
| `system.block_devices` | `tables/block_devices.tsv` | `lsblk` | ✅ **已修**（F-08）⇒ `unsupported` |
| `system.mounts` | `evidence/mounts.txt` | `mount` | ✅ **已修**（F-08）⇒ `unsupported` |
| `system.ip_address` | `evidence/ip_address.txt` | `ip` / `ifconfig` | `unsupported` ✅ |
| `system.ip_route` | `evidence/ip_route.txt` | `ip` | ✅ **已修**（F-08）⇒ `unsupported` |
| `system.ifconfig` | `evidence/ip_address.txt`（同上文件）| `ifconfig` | — |
| `system.socket_summary` | `evidence/socket_summary.txt` | `ss` | ✅ **已修**（F-08）⇒ `unsupported` |
| `system.numa` | `evidence/numa.txt` | `numactl` | ✅ **已修**（F-08）⇒ `unsupported` |
| `system.sysctl_selected` | `tables/kernel_parameters.tsv` | `sysctl` | ✅ **已修**（F-08）⇒ `unsupported` |
| `system.hugepages` | `tables/hugepages.tsv` | — | ~~**恒登记 `ok`，但 0 行也是 `ok`**（F-27）~~ **已修（2026-10-15，三库同形）**：行数为 0 ⇒ `empty`（`/sys` 与 `/proc/sys/vm` 都读不到时旧写法显示"大页配置正常"）；且 **PG 侧零消费者** |
| `system.dmesg_errors` | `evidence/dmesg_errors.txt` | `dmesg` | ✅ **已修**（F-08）⇒ `unsupported` |
| `system.postgres_processes` | `evidence/postgres_processes.txt` | `ps` | ✅ **已修**（F-08）⇒ `unsupported` |
| `system.iostat` | `tables/iostat_snapshot.tsv` | `iostat` | ✅ **已修**（F-08）⇒ `unsupported` |
| `system.timedatectl` | `evidence/timedatectl.txt` | `timedatectl` | ✅ **已修**（F-08）⇒ `unsupported` |
| `system.backup_timers` | `evidence/backup_timers.txt` | `systemctl` | ✅ **已修**（F-08）⇒ `unsupported` |
| `system.chronyc_tracking` / `system.chronyc_sources` / `system.ntpq_peers` | `evidence/chronyc_*.txt` / `evidence/ntpq_peers.txt` | `chronyc` / `ntpq` | ✅ **已修**（F-08）：未采用的备选记 `skipped`，都缺记 `unsupported` |

> ⚠ **F-08 合计 16 个 item** 会在依赖命令缺失时"连一行记录都不写"（A 档 8 / B 档 5 / C 档 3）—— 逐条见 FIX-PLAN §F-08 的完整表。**"不登记"三个字在这里的后果是：该 item 不进 `collection_status.json`、不进完整度分母 ⇒ 缺得越多、分数越高。**

> ⚠ **`system.free` / `system.filesystems` / `system.inodes` / `system.block_devices` / `system.iostat` 的输出是"列对齐文本"，不是 TSV**（`free -b` / `df -PT` / `df -Pi` / `lsblk` / `iostat`）。下游必须用 `inspection_core/models.py::PackageContext.raw_lines()` 或专用解析器（`plugins/postgresql/parsers.py::_parse_df_pt` / `_parse_free_b`）读，**不要**走 `parse_delimited`。

### 4.2 PG 基础（`pg.basic`，`tables/*.tsv`）

| item_id | 文件 | 列（首行为 psql 表头）|
|---|---|---|
| `pg.instance` | `instance.tsv` | version, version_num, database, inspection_user, started_at, uptime, is_standby, server_addr, server_port |
| `pg.control_system` | `control_system.tsv` | system_identifier, pg_control_version, catalog_version_no |
| `pg.control_checkpoint` | `control_checkpoint.tsv` | **`SELECT *`**（列集随版本变）|
| `pg.settings` | `settings.tsv` | name, setting, unit, source, pending_restart, vartype, context |
| `pg.file_settings_errors` | `file_settings_errors.tsv` | sourcefile, sourceline, name, setting, error |
| `pg.hba_rules` | `hba_rules.tsv` | line_number, type, database, user_name, address, netmask, auth_method, options, error |
| `pg.databases` | `databases.tsv` | database_name, size_bytes, connection_limit, xid_age, encoding, collation |
| `pg.database_ages` | `database_ages.tsv` | database_name, xid_age, multixact_age |
| `pg.extensions` | `extensions.tsv` | extension_name, version |
| `pg.tablespaces` | `tablespaces.tsv` | tablespace, location, size_bytes |
| `pg.roles` | `roles.tsv` | role_name, is_superuser, rolcreaterole, rolcreatedb, rolcanlogin, rolreplication, rolconnlimit, rolvaliduntil |

### 4.3 PG 性能（`pg.performance`，23 项）

⚠ 其中 9 项（`table_health` / `large_tables` / `no_primary_key` / `indexes` / `sequences` /
`duplicate_indexes` / `unused_indexes` / `bloat_estimation` / `partition_summary`）在**顶层连接**上
恒为空（默认连 `postgres` 系统库，而这些视图只反映当前库）⇒ **2026-10-16 起改记 `skipped`**，
真实数据来自 §4.5 的逐库采集（见 FIX-PLAN F-23）。

| item_id | 文件 | 备注 |
|---|---|---|
| `pg.connections` | `connections.tsv` | 单行聚合 |
| `pg.session_states` | `session_states.tsv` | 按 state 分组 |
| `pg.long_transactions` | `long_transactions.tsv` | `left(query,300)` **未清洗换行**（F-01）|
| `pg.idle_transactions` | `idle_transactions.tsv` | `left(query,300)` **未清洗**（F-01）|
| `pg.lock_waits` | `lock_waits.tsv` | `left(...,240)` ×2 **未清洗**（F-01）|
| `pg.database_stats` | `database_stats.tsv` | 17 列 |
| `pg.database_conflicts` | `database_conflicts.tsv` | 6 列 |
| `pg.vacuum_progress` | `vacuum_progress.tsv` | |
| `pg.bgwriter` | `bgwriter.tsv` | **`SELECT *`** |
| `pg.table_health` | `table_health.tsv` | `LIMIT TOP_N`（100）|
| `pg.large_tables` | `large_tables.tsv` | `LIMIT 30`（**硬编码**，不是 TOP_N）|
| `pg.no_primary_key` | `no_primary_key.tsv` | `LIMIT TOP_N` |
| `pg.indexes` | `indexes.tsv` | `LIMIT TOP_N` |
| `pg.sequences` | `sequences.tsv` | `LIMIT 50`（**硬编码**）|
| `pg.prepared_xacts` | `prepared_xacts.tsv` | |
| `pg.object_counts` | `object_counts.tsv` | 10 类对象 `UNION ALL` |
| `pg.duplicate_indexes` | `duplicate_indexes.tsv` | `LIMIT TOP_N` |
| `pg.unused_indexes` | `unused_indexes.tsv` | `LIMIT TOP_N`；有 7 天观察期门槛 |
| `pg.db_role_settings` | `db_role_settings.tsv` | `unnest(setconfig)` ⇒ **每角色多行** |
| `pg.bloat_estimation` | `bloat_estimation.tsv` | `LIMIT TOP_N` |
| `pg.partition_summary` | `partition_summary.tsv` | `LIMIT TOP_N` |
| `pg.stat_statements` | `pg_stat_statements.tsv` | 条件项；`REDACT_SQL=1` 时 query 为 NULL |
| `pg.stat_wal` | `stat_wal.tsv` | 条件项；PG≥14 否则 `not_applicable` |
| `pg.stat_io` | `stat_io.tsv` | 条件项；PG≥16 否则 `not_applicable` |
| `pg.checkpointer` | `checkpointer.tsv` | 条件项；PG≥17 否则 `not_applicable` |

### 4.4 PG 复制（`pg.replication`，4 项）

`pg.stat_replication` → `stat_replication.tsv`；`pg.wal_receiver` → `wal_receiver.tsv`；`pg.replication_slots` → `replication_slots.tsv`；`pg.archiver` → `archiver.tsv`。

### 4.5 逐库对象（`pg.objects`，**每个库 16 项**）

> 2026-10-16：新增 `db.<name>.sequences`（顶层那份已改 `skipped`，见 §4.3），故 15 → 16 项。

`ALL_DATABASES=1`（默认）时遍历 `SELECT datname FROM pg_database WHERE datallowconn AND NOT datistemplate`；`--current-database-only` 时只取 `current_database()`。

| item_id 模板 | 文件 | 有 `database_name` 首列？ |
|---|---|:---:|
| `db.<db>.extensions` | `databases/<db>/extensions.tsv` | ✅ |
| `db.<db>.table_health` | `…/table_health.tsv` | ✅ |
| `db.<db>.large_tables` | `…/large_tables.tsv` | ✅ `LIMIT 30` |
| `db.<db>.no_primary_key` | `…/no_primary_key.tsv` | ✅ |
| `db.<db>.indexes` | `…/indexes.tsv` | ✅ `LIMIT 200` |
| `db.<db>.duplicate_indexes` | `…/duplicate_indexes.tsv` | ✅ |
| `db.<db>.unused_indexes` | `…/unused_indexes.tsv` | ✅ |
| `db.<db>.bloat_estimation` | `…/bloat_estimation.tsv` | ✅ |
| `db.<db>.partition_summary` | `…/partition_summary.tsv` | ✅ |
| `db.<db>.invalid_constraints` | `…/invalid_constraints.tsv` | ✅ |
| `db.<db>.missing_fk_indexes` | `…/missing_fk_indexes.tsv` | ✅ |
| `db.<db>.public_privileges` | `…/public_privileges.tsv` | ✅ |
| `db.<db>.security_definer_functions` | `…/security_definer_functions.tsv` | ✅ |
| `db.<db>.subscriptions` | `…/subscriptions.tsv` | ✅ |
| `db.<db>.pg_stat_statements` | `…/pg_stat_statements.tsv` | ✅ 条件项 |

`<db>` = `datname` 经 `tr -c 'A-Za-z0-9._-' '_'` 处理（**只用于文件名，不用于 SQL**）。

> ⚠ **8 个 basename 与 `tables/` 重名**（`table_health` `large_tables` `no_primary_key` `indexes` `duplicate_indexes` `unused_indexes` `bloat_estimation` `partition_summary`）⇒ 分析侧 `tables.update()` **覆盖**，只留逐库副本（F-10）。
> ⚠ **逐库版的列集比 `tables/` 版多一个 `database_name` 首列** ⇒ `known_tsv_header` 不能共用一条（F-02）。

### 4.6 日志与备份（`pg.logs` / `pg.backup`）

| item_id | 类别 | 输出 | 说明 |
|---|---|---|---|
| `pg.log_summary` | `pg.logs` | `tables/error_log_summary.tsv` | `category`/`count` 两列，**恒 7 行**（7 个类别）；只扫**最后 20000 行**；`row_count` 记 7 |
| `backup.pgbackrest` | `pg.backup` | `evidence/pgbackrest_info.json` | 条件项 |
| `backup.barman` | `pg.backup` | `evidence/barman_check.txt` | 条件项 |
| `ha.patroni` | `pg.backup` | `evidence/patroni_cluster.json` | 条件项（**分类错位**，F-09）|
| `ha.repmgr` | `pg.backup` | `evidence/repmgr_cluster.txt` | 条件项（**分类错位**）|
| ~~`ha.manager`~~ | `pg.backup` | — | **已废弃（F-08，2026-10-16）**：原为"patroni/repmgr 都不在"时的兜底 id，与两个真实 item **不同域**（有 patronictl 的机器上它根本不存在）⇒ 改为 `ha.patroni` / `ha.repmgr` 在所有路径各自留痕。Python 侧零消费，无功能影响 |
| `system.backup_cron` | `pg.backup` | `evidence/backup_cron.txt` | `crontab -l` 按 `pg` / `postgres` / `backup` / `pgbackrest` / … 过滤（`grep -Ei`）|
| `system.backup_timers` | `pg.backup` | `evidence/backup_timers.txt` | 仅 `has_cmd systemctl` 才登记（F-08）|
| `system.backup_processes` | `pg.backup` | `evidence/backup_processes.txt` | `ps` 过滤 |

### 4.7 时序与历史

| item_id | 类别 | 输出 |
|---|---|---|
| `timeseries.realtime_sampling` | `timeseries` | `timeseries/{system_cpu,system_memory,system_network,system_disk,pg_activity,pg_stats}.csv` |
| `system.sar_history` | `system.history` | `history/sar_{cpu,memory,swap,load,disk,network,io,process}.csv` + `history/coverage.tsv` + `history/coverage.json`（F-26）+ `history/source_files.txt` | ★ **2026-10-15 三库同形**：`coverage.json` 升 `schema_version=1.1`，新增 `point_count` / `discontinuity_count` / `max_gap_seconds`（`coverage.tsv` 同增三行）；`coverage_seconds` 语义改为**累计覆盖**（只累加 ≤1h 的相邻间隔，旧版是首尾相减）、`status` 增加**连续性判据**（`max_gap_seconds>3600` ⇒ `partial`） |
| `pg.capability_probe` | `pg.capabilities` | `tables/capabilities.tsv` |
| `package.security_scan` | `package` | `logs/security_scan_findings.txt`（仅命中时）|
| `module.<id>` | `module` | `logs/modules/<id>.log` |

## 5. 输出目录树（标注 Python 入口）

```
pg_inspection_v2_<host>_<addr>_<port>_<ts>/
├── snapshot.json              ← ★ Python 入口 1（实例/主机/时间/角色/能力/采样/隐私/汇总）
├── collection_status.json     ← ★ Python 入口 2（**逐项状态 + 耗时 + 原因**）
├── summary.txt                ← 人读摘要（**Python 未消费**，可用于对账）
├── manifest.json              ← ★ Python 入口 3（文件清单 + 大小 + sha256 ⇒ 完整性校验）
├── tables/                    ← ★ Python 入口 4（静态结构化数据）
│   ├── *.tsv                  49 张；**大部分是 psql -A 输出（首行是列名）**
│   ├── memory_snapshot.tsv    ⚠ 非 TSV：`free -b` 原文（走 _parse_free_b）
│   ├── filesystems.tsv        ⚠ 非 TSV：`df -PT` 原文（走 _parse_df_pt）
│   ├── inodes.tsv             ⚠ 非 TSV：`df -Pi` 原文
│   ├── block_devices.tsv      ⚠ 非 TSV：`lsblk` 原文
│   ├── iostat_snapshot.tsv    ⚠ 非 TSV：`iostat -xz 1 3` 原文
│   ├── kernel_parameters.tsv  ⚠ **key\tvalue，无表头行**
│   ├── hugepages.tsv          key/value，**有**表头 `path\tvalue`
│   └── capabilities.tsv       key/value，**有**表头 `capability\tvalue`
├── timeseries/                ← CSV，**首行是自写的表头**
│   ├── system_cpu.csv          timestamp,elapsed_ms,user_pct,system_pct,iowait_pct,steal_pct,idle_pct,busy_pct
│   ├── system_memory.csv       timestamp,elapsed_ms,mem_total_bytes,mem_available_bytes,mem_used_pct,swap_total_bytes,swap_used_bytes,cached_bytes,dirty_writeback_bytes
│   ├── system_network.csv      timestamp,elapsed_ms,interface,rx_bytes_per_sec,tx_bytes_per_sec,rx_bytes_delta,tx_bytes_delta
│   ├── system_disk.csv         timestamp,elapsed_ms,device,read_bytes_per_sec,write_bytes_per_sec,read_iops,write_iops,read_await_ms,write_await_ms,util_pct,avg_queue_size
│   ├── pg_activity.csv         timestamp,elapsed_ms,total_sessions,active_sessions,idle_in_xact,waiting
│   └── pg_stats.csv            timestamp,elapsed_ms,xact_commit,xact_rollback,blks_read,blks_hit,tup_returned,tup_fetched,tup_inserted,tup_updated,tup_deleted,temp_files,temp_bytes,deadlocks
├── history/                   ← sadf -d 分号格式，**首行是 # hostname;… 注释头**（走 parse_sadf）
│   ├── sar_{cpu,memory,swap,load,disk,network,io,process}.csv
│   ├── coverage.tsv           ⚠ **key\tvalue 两列，非表格数据**；Python 未直接读，但 snapshot 里有它的摘录
│   ├── coverage.json          ⚠ **无读者**（F-26）
│   └── source_files.txt       ⚠ 原始 sar 文件路径清单（**可能含客户主机路径**）
├── databases/<db>/*.tsv       逐库对象（15 项/库），**首列 database_name**
├── evidence/                  ← 原文证据（.txt / .json），走 raw_lines() 读
│   ├── os_release.txt  time_status.txt  timedatectl.txt  chronyc_*.txt  ntpq_peers.txt
│   ├── lscpu.txt  mounts.txt  dmesg_errors.txt  postgres_processes.txt
│   ├── ip_address.txt  ip_route.txt  socket_summary.txt  numa.txt
│   ├── backup_cron.txt  backup_timers.txt  backup_processes.txt
│   ├── pgbackrest_info.json  barman_check.txt  patroni_cluster.json  repmgr_cluster.txt
│   └── security_scan_findings.txt（**仅命中敏感模式时**，同时也在 logs/ 下）
├── logs/
│   ├── collection.log         采集器运行日志（`security_scan` **排除**它）
│   ├── security_scan_findings.txt
│   └── modules/<module_id>.log
├── .tmp/                      采集期临时（**收尾必删**）
└── .status_parts/<item_id>.tsv 逐项状态暂存（**收尾必删**；`collection_status.json` 的来源）
```

**Python 入口就是 4 个**：`snapshot.json`、`collection_status.json`、`manifest.json`、`tables/`（+ `databases/`、`timeseries/`、`history/` 三个数据目录）。
**`summary.txt` 不被 Python 消费**，它的定位是"给人看 + 对账"。

## 6. 四个入口文件的字段表

### 6.1 `snapshot.json`（`generate_snapshot_json`，L1022-1090）

| 键 | 类型 | 说明 | Python 消费点 |
|---|---|---|---|
| `schema_version` | str | `"1.0"` | — |
| `package_version` | str | `"1.0"` | — |
| `collector` | obj | `{name, version, platform, started_at, finished_at}` | `analyzer.py` → `instance.collector`；`report_adapter` 的 `collection_time` 取 `finished_at` |
| `instance_identity` | obj | `database_type` `version` `version_num` `is_recovery` `wal_level` `archive_mode` `data_directory` `connect_host` `connect_ip` `instance_ip` `port` `instance_tag` | `analyzer.build_report_model`、`topology`（`is_recovery`）、`PackageContext.instance_id`（`instance_tag`）|
| `host_identity` | obj | `hostname` `short_hostname` `primary_ip` `all_ipv4` `database_target_is_local` `machine_id` `os` `kernel` `cpu_count` `memory_total_bytes` | `presentation` 的 `system.host` / `system_info` 段 |
| `time_evidence` | obj | `host_local_time` `host_utc_time` `timezone` `ntp_synchronized` | `presentation` 的 `system.time` |
| `role_evidence` | obj | `role_observed` `confidence` `is_recovery` `replication_source_count` `wal_receiver_status` `subscription_count` | `analyzer`/`presentation` 的 `identity.role_observed` |
| `capabilities` | obj | `pg_stat_statements` `pg_stat_wal` `pg_stat_io` `pg_stat_checkpointer` `sar_command` `sadf_command`（**全 bool**）| `analyzer` 的 `instance.capabilities` |
| `sampling` | obj | `status` `interval_seconds` `requested_sample_count` `requested_duration_seconds` `actual_pg_points` `actual_cpu_points` `actual_elapsed_ms` `sar_history_requested_hours` `sar_history{status,coverage_hours,first_timestamp,last_timestamp}` `realtime_files{...}` `history_dir` | `analyzer.collection_window`、`report_adapter` 的 `appendix.collection_window` |
| `privacy` | obj | `sql_text_included`（= `REDACT_SQL==0`）`settings_included`（恒 true）`password_included`（恒 false）| 目前**无人消费**（建议接入报告声明）|
| `collection_summary` | obj | `successful_or_empty_items` `warning_items` `failed_items` `collection_status_file` | 目前**无人消费**（`quality` 走的是 `collection_status.json`）|
| `artifacts` | obj | `tables_dir` `timeseries_dir` `history_dir` `evidence_dir` `databases_dir` `summary` `log`（相对路径字符串）| 目前**无人消费** |

> ⚠ **`snapshot.json` 里没有 `collection_status` 键**。展示层若写 `ctx.snapshot.get("collection_status")` 会**永远拿到 `{}`**（F-14）。逐项状态只能从 `collection_status.json`（即 `PackageContext.status`）取。
> ⚠ `collection_summary.successful_or_empty_items` 把 `ok` 与 `empty` **合并计数**（L1039），`warning_items` = `unsupported|not_enabled|partial`，`failed_items` = `error|timeout|permission_denied`。**`skipped` 与 `not_applicable` 不在任何一栏**（既不算成功也不算警告）—— 用它做对账会漏项，**建议改读 `collection_status.json`**。

### 6.2 `collection_status.json`（`generate_collection_status_json`，L998-1020）

```json
{
  "schema_version": "1.0",
  "items": [
    {"item_id":"pg.instance","category":"pg.basic","status":"ok",
     "started_at":"…","finished_at":"…","duration_ms":123,"row_count":1,
     "exit_code":0,"output_file":"tables/instance.tsv","reason":""}
  ],
  "summary": {"ok":38,"empty":6,"unsupported":5,"not_enabled":2,"not_applicable":5,
              "permission_denied":0,"timeout":0,"error":0,"skipped":0,"partial":1}
}
```

| 字段 | 说明 |
|---|---|
| `item_id` | 采集项 id（**不是规则 id**，两套命名空间见 §10-③）|
| `category` | 见 §4 的 11 个取值 |
| `status` | 见 §7 |
| `started_at` / `finished_at` | ISO 8601 带时区 |
| `duration_ms` | 整数；`record_skipped` 写的恒为 0 |
| `row_count` | **注意语义分叉**：`pg_query_tsv` 给的是 `NR-1`（去掉 psql 表头）；`capture_command` 给的是 `wc -l`（**含**首行）；`record_skipped` 恒 0；`pg.log_summary` 恒 7 |
| `exit_code` | 进程退出码；`record_skipped` 恒 0 |
| `output_file` | **相对 `TASK_DIR` 的路径**（`safe_relpath`）；`record_skipped` 未给 outfile 时为空串 |
| ↑ 同上 | **它同时是报告层定位采集项的键**（basename → `.tsv` stem → 指标/章节，见 §10-③ 落地块）。
给 `record_skipped` 传 outfile 因此从「可选」变成**契约要求**。 |
| `reason` | 失败/跳过原因（`sanitize_text` 净化过 TAB/LF）；**成功时为空串** |

**生成机制的坑**（`generate_collection_status_json`）：
- 来源是 `.status_parts/<sanitize_id(item_id)>.tsv` 的 `cat` 拼接，AWK 过滤条件 **`NF>=9`**（实际每行 10 个字段）。
- **`record_status` 是覆盖写**：同一个 `item_id` 记两次，**后写胜**，前一次静默丢失。所以同一 item 绝不能记两次。
- 文件按 `sanitize_id(item_id)` 命名 ⇒ **不同 item_id 若净化后同形会互相覆盖**（当前无此情况，但新增 id 时要注意）。
- `.status_parts/` 在收尾被 `rm -rf` ⇒ 该文件是**唯一**的逐项状态来源。

### 6.3 `manifest.json`（`generate_manifest`，L1162-1174）

```json
{"package_version":"1.0","collector_version":"2.0.1","database_type":"postgresql",
 "instance_tag":"…","created_at":"…",
 "files":[{"path":"snapshot.json","size_bytes":1234,"sha256":"…"}]}
```

- 收录 `TASK_DIR` 下**所有**文件，排除 `.tmp/`、`.status_parts/`、`manifest.json` 自身、`.pgpass.*`。
- Python 侧：`PostgreSQLPackageAdapter.verify_manifest`（`package_adapter.py` L89-113）→ `PackageContext.integrity = {status, checked_files, failure_count, failures[]}`；`status ∈ {ok, failed, missing}`。
- ⚠ `report_adapter.py` L239 把 `integrity.status` **写死成 `"not_evaluated"`** 并给理由"旧 PG 报告模型未携带 manifest 校验明细"——**这是硬编码**，四个库基线都吃这一句。改它要同步改基线。

### 6.4 `tables/`（`PostgreSQLPackageAdapter.load`，L46-56）

- 键 = `path.stem`（**不含目录、不含扩展名**）⇒ `tables/pg_stat_statements.tsv` 与 `databases/x/pg_stat_statements.tsv` **键相同**。
- `databases/*/*.tsv` 按 stem **`extend` 聚合**后 `tables.update(...)` ⇒ **覆盖** `tables/` 的同名键（F-10）。
- `settings` 额外派生一份 dict：`{row["name"]: row["setting"]}`。
- ⚠ **空文件的 `[]` 与"查了 0 行"的 `[]` 完全同形** ⇒ 判"采没采到"必须回查 `ctx.status`，不能看 `[]`。

## 7. status 语义表

采集侧共 **11 个**取值（`generate_collection_status_json` 的 summary 计数键；`schema_mismatch` 于 2026-10-15 加入，对齐 MySQL F-21）。

| 值 | 何时写入 | 含义 | **Python 应如何处理** |
|---|---|---|---|
| `ok` | `rc==0` 且 `rows>0` | 正常采到数据 | 可用于结论 |
| `empty` | `rc==0` 且 `rows==0` | **采过了，确认 0 行** | **算"采集过"**。可下结论"本次无相关记录"，**不得**说"没采到"，**更不得**说"正常/通过" |
| `partial` | `run_module` 见 `rc==2`；`collect_sar_history` 见 coverage 非 ok/empty；`collect_realtime_samples` 采样点不足 | 采到但不完整 | 可用但必须**带限制声明**（`report_adapter.appendix.data_quality.limitations`） |
| `unsupported` | `classify_failure` 命中 `does not exist / unknown table / unknown system variable / not supported / relation does not exist`；或显式 `record_skipped` | 环境不支持 | 与 `not_applicable` 同级：**不评价**，说明理由 |
| `schema_mismatch` | `classify_failure` 命中 `column .* does not exist / unknown column / unknown field / unknown identifier` | **表在、列不在** = 采集 SQL 与目标版本列集不符 | **不是"环境不支持"，是采集器缺陷**：进 `error_states`（完整度扣分），报告状态列显示「与版本不匹配」，须改脚本而不是忽略 |
| `not_applicable` | 显式 `record_skipped`（`pg.stat_wal` PG<14 / `pg.stat_io` PG<16 / `pg.checkpointer` PG<17）| 版本/架构不适用 | **不评价**，且**不得**与"没采到"共用文案 |
| `not_enabled` | `classify_failure` 命中 `cannot talk to daemon / could not open connection / service not running / command not found`；或 `pg.log_summary` 见 `pg_current_logfile()` 为空 | 功能未启用 | 不评价 + 给"如何启用"的指引 |
| `permission_denied` | `classify_failure` 命中 `access denied / permission denied / requires.*privilege`；或日志文件不可读 | 权限不足 | **不评价**，报告须提示"补权限后重采" |
| `timeout` | `rc ∈ {124, 137}` | 超时 | **不评价**，并给出"该项耗时超 `PG_TIMEOUT_SECONDS`" |
| `error` | `rc != 0` 且未命中以上模式；`run_module` 见模块非零退出；`package.security_scan` 命中敏感模式 | 真失败 | **不评价**，进 `COMMON.COLLECTION.ERRORS` 判据 |
| `skipped` | 预留。**当前脚本从不写这个值** | — | 兼容用 |

**当前脚本实际会写出的取值**（grep `record_status`/`record_skipped`/`classify_failure` 实测）：
`ok` `empty` `partial` `unsupported` `schema_mismatch` `not_applicable` `not_enabled` `permission_denied` `timeout` `error`。
⇒ **`skipped` 是纯契约占位**（`summary` 里统计它，但它恒为 0）。

**规则侧只有 3 个状态**（`plugins/postgresql/rules.py`）：`not_evaluated` / `passed` / `triggered`。
**两套词表不对齐**（F-18）："没采到（`unavailable`）"与"本版本不适用（`not_applicable`）"在规则侧**都**是 `not_evaluated`，再被 `report_adapter.py` L165-170 一律翻成 `external_evidence_required`。

**`inspection_core/word_engine.py::STATUS_DISPLAY` 是状态词中文名的唯一来源**，已含：
`ok→正常` `empty→无记录` `not_applicable→不适用` `not_evaluated→未判断` `skipped→已跳过` `unsupported→不支持` `schema_mismatch→与版本不匹配` `permission_denied→权限不足` `timeout→超时` `error→失败` `not_enabled→未启用` `unavailable→未采集` `missing→未采集` `partial→部分可用` `insufficient_history→历史不足` `external_evidence_required→需要外部证据`。
⇒ 新增状态词**必须**先加进这张表，否则报告里会**原样印英文枚举**（批 19 的旧伤，不抛异常、测试不红）。

## 8. 退出码表

| 码 | 触发 | 位置 | 备注 |
|:---:|---|---|---|
| `0` | 正常完成 | — | ⚠ **当前不可靠**：见下 |
| `10` | 参数/环境错误：未知选项、端口/时间参数非正整数、bash < 4.0、无 `psql`、`mkdir -p OUTPUT_PARENT` 失败、建目录失败 | L1231/1236/1237/1238/1239/1240/1255 | |
| `11` | 输出目录剩余空间 < `MIN_FREE_MB`(200) | L415 | |
| `20` | PostgreSQL 连接失败（`SELECT 1` 失败） | L1309 | |
| `130` | 收到 `INT`/`TERM`/`HUP` | L1205 `handle_signal` | 同时写 `COLLECTION_INCOMPLETE` 标记 |
| **`1`** | **`--no-package` 成功但 `PACKAGE_FILE` 未赋值** | L1362 `&&` 链 | **缺陷（F-07）**：成功路径返回 1 |
| **`1`** | **安全扫描拦截**（未生成回传包） | L1347-1353 | **缺陷（F-07）**：与"真失败"同码，外部编排无法区分 |
| `1` | 打包失败（`tar` 非零） | `create_package` 返回值 | 但**不**被 `exit` 显式使用，只在日志里体现 |
| 其他 | 未定义 | — | `set +e` ⇒ 未捕获的命令失败不会改变退出码 |

**建议的目标表**（FIX-PLAN F-07）：
`0` 成功 / `10` 用法与环境 / `11` 空间不足 / `20` 连接失败 / **`21` 敏感信息拦截** / **`22` 打包失败** / `130` 信号，并在脚本末尾**显式 `exit`**。

⚠ **另有一条契约缺口**：PG 脚本**没有** Oracle 那样的"策略性跳过"机制（Oracle 用 `EXIT_POLICY_SKIP=40`，在 `rc -ne 0 → error` 分支**之前**拦截，把它映射成 `not_enabled` 而不是 `error`）。PG 目前靠**显式 `record_skipped`** 表达"不适用/未启用"，**方向是对的**，但要注意：任何"提前 `return 0` 的静默跳过"都会**同时**逃过 `record_status` 和 `record_skipped` ⇒ 该 item 在 `collection_status.json` 里**不存在**（这正是 **F-08** 那 7 条的表现）。

## 9. 变更规则

### 9.1 四条硬规则

1. **动输出就 bump `SNAPSHOT_SCHEMA_VERSION`**。
   - **次版本**（`1.0 → 1.1`）：新增独立文件、新增 `tables/*.tsv` 的**占位表头条目**、新增 status 取值、新增退出码、`pg_query_tsv` 换输出格式（`unaligned → csv`，见 FIX-PLAN F-01）。
   - **主版本**（`1.x → 2.0`）：删/改列名、改 `item_id`、改 `category`、改 `status` 语义、改文件名。
2. **改 SQL 的 SELECT 列表 → 必须同步改 `known_tsv_header`**。
   **正常采集时表头来自 psql 输出，看不出问题；只有空结果/失败时占位表头才暴露** ⇒ 极易漏。**并且**：逐库版与主版是**两套列集**（逐库多 `database_name`），不要共用一条。
3. **新增采集项四件事一起做**：写代码 → 登记占位表头（主版 + 逐库版）→ 更新本文件 §4 → 更新 §5 目录树。
4. **注释与代码同改**。注释密度低时那几行注释会被当成"已核实记录"来读，**撒谎的注释比没注释更坏**。
   两个死旋钮现状（2026-10-16 复核）：`INCLUDE_LOG_TEXT` **已删**（F-05，三处一起删）；
   两个死旋钮**都已处置**（2026-10-16）：`INCLUDE_LOG_TEXT` 已删（F-05，三处一起删）；
   `LEGACY_PASSWORD` 已删（F-06：`LEGACY_PASSWORD=""` 声明 + `elif [ -n "$LEGACY_PASSWORD" ]` 分支，
   参数解析里没有 `--password` ⇒ 分支不可达，属纯删除；`show_usage()` 已核，无假承诺）。
   读代码的人会以为"命令行密码是支持的、只是不推荐"。

### 9.2 新增采集项 Checklist

- [ ] 函数放进正确的 `collect_*`（决定它是串行还是后台并行）
- [ ] `item_id` 命名：`<域>.<对象>`，域与 `category` **不要同名混用**（`pg.performance` 是 category，**不是** item_id）
- [ ] `category` 在 §4 的 11 个取值里，且**不串味**（不要往 `pg.backup` 里再塞高可用）
- [ ] 缺命令/缺权限时**必须**有 `else record_skipped`（F-08）
- [ ] `known_tsv_header` 加占位表头（主版 / 逐库版分开），**列数 == SELECT 列数**
- [ ] 自由文本列（SQL 原文、`relname`、`gid`）**必须**做 `regexp_replace(...,E'[\\n\\r\\t]+',' ','g')`，或整体启用 `--csv`（F-01）
- [ ] `row_count` 语义与本文件 §6.2 的表一致（去掉表头？含首行？）
- [ ] 若引用 `TASK_DIR` 外路径 ⇒ 必须在 `TASK_DIR` 内落一份副本（否则不进包）
- [ ] 更新本文件 §4 / §5 / §7（若引入新 status）
- [ ] bump `SNAPSHOT_SCHEMA_VERSION`（次版本）
- [ ] 用 §10 的"文件而非表"判据自测：**包内有没有这个文件**、**`collection_status.json` 里有没有这一项**

### 9.3 删除采集项 Checklist

- [ ] 从 `known_tsv_header` 删对应条目（**主版和逐库版都要**）
- [ ] 从本文件 §4 / §5 删
- [ ] 检查是否有下游按 `output_file` 路径硬编码读取（`presentation.py` 的 `root / "tables" / xxx.tsv`、`root / "evidence" / xxx`）
- [ ] bump **主版本**

## 10. 已知契约缺陷 + 建议的重构顺序

### ① 采集原语不保证行结构（F-01，P0）

`pg_query_tsv` / `pg_query_tsv_db` 用 `psql -A -F $'\t'`。PostgreSQL 官方手册 `\pset format` 的 `unaligned` 条目明确：
> **However, the field separator character is not treated specially if it appears in a column's value; so CSV format may be better suited for such purposes.**

脚本只在 `pg_stat_statements.query` 上做了清洗（L821/L902），**`long_transactions` / `idle_transactions` / `lock_waits` 三条没做**。
下游 `inspection_core/tabular.py::parse_delimited` 先按物理行切开再交 `csv.DictReader` ⇒ 值内换行导致**列错位**、值内制表符被 `restkey=None` **静默丢弃**、值以 `"` 开头会**跨行吞并**。三者都不报错。

### ② 占位表头覆盖 23/49 且列数漂移（F-02，P0）

`known_tsv_header` 只认 23 个 basename；**逐库版比主版多一个 `database_name`**（`no_primary_key` 是明确实例）。rc!=0 时未覆盖的表留下 **0 字节文件**，与"查了 0 行"的 `[]` 同形。

### ③ 采集状态与规则判定是两套 id 域、两套词表（F-12 / F-14 / F-15 / F-18，P0）

- **采集项 id**（如 `pg.database_stats`）↔ **规则 id**（如 `PG.CACHE.HIT_RATIO`）：**没有任何映射表**。
- **展示层 item_id**（18 个，如 `pg.performance`）**既不是采集项 id，也不是规则 id**，其中 **5 个还与采集层的 `category` 同名**。（批 2D 后为 **21** 个。）
- 展示层读的状态源是 `ctx.snapshot["collection_status"]`，**而 snapshot.json 里根本没这个键** ⇒ 查找永不命中，退化成"用展示行数猜"，而多数展示行是**代码合成的**。
- 结果：报告逐条印 **`采集状态：正常`**（`word_engine.py` L811），**而没有任何一层有能力说出"我没采到"**。

**建议的接线（照 Oracle，三件事必须同批做）**：
1. `RuleEvaluation` 增加 `source_status`；`metrics` 为每个被判据消费的指标登记 `source_status[key] = fetch_state(fetch)`；`rules.evaluate` 的 `available` 由 `state_is_collected` 决定。
2. 展示层引入 `DISPLAY_ITEM_ALIASES`（单一来源）把 18 个展示 item 映射到采集项 id，**禁止子串兜底**；无数据时按真实状态给 `display.empty_text`。
3. `word_engine.py` 的 `STATUS_DISPLAY` 补新词（**不要**在引擎里加 PG 专用逻辑——它是四库共享的）。

**✅ 已落地（2026-10-16，批 2C）** —— 上面三件事已同批做完，但**改用了另一条桥**：

- 新增 `inspection_core/collection_state.py`：`state_is_collected` / `merge_section_state` /
  `expand_source_names`（判据与归并**只此一份**，跨库共用）。
- 三张**模块级**映射表（都可被测试断言，不再藏在闭包里）：
  · `presentation.PG_SECTION_SOURCES`（**21** 章节 → 采集 item_id，支持通配 `db.*.X`；
    批 2D 追加 `system.hugepages` / `pg.io` / `pg.wal`）
  · `metrics.METRIC_SOURCES`（34 个指标 → 采集表 stem）
  · `rules.RULE_METRIC_KEY`（29 条规则 → 指标键）
- 桥不是 `table_fetch`，而是 **`collection_status.items[].output_file` 的 basename → `.tsv` stem**。
  这条对本文件是**契约级**的：`output_file` 从「给人看的路径」变成「报告层定位采集项的唯一定位键」
  （真机 128 项全有值）。⇒ **新增采集项时必须给 `record_skipped` 传 outfile**，否则该项在台账里
  没有 basename、**对报告层不可见**（同时又因它是 `skipped` 而不会进 `DATA_STATES`，两侧判据一致，
  故不会把「没采到」说成「没问题」；但它也**不会出现在缺口表里**——所以 Checklist §9.2 第 3 条要照做）。
- `STATUS_DISPLAY` 只补了 3 个**缺键**：`unmapped` / `unavailable` / `missing` → 「未采集」。
- 附录「数据缺口」表改为**合并**规则侧（`not_evaluated`）与采集侧（`non_ok_items`），去重后同列。

### ④ 版本专有列 / 条件项的表达方式不统一

- "版本不支持"有**三种**写法：`record_skipped ... "not_applicable"`（`pg.stat_wal` 等）、`record_skipped ... "unsupported"`（`pg.stat_statements`/`backup.*`）、以及 `classify_failure` 从错误文本**猜**出来的 `unsupported`。
- ~~且 `pg_table_exists` 是**表级**探测（不是列级）⇒ 表在、列不在时 SQL 会报 1054/42703 类错误，被 `classify_failure` 归类为 `unsupported` ⇒ **"脚本 SQL 写错"伪装成"环境不支持"**。~~
  **已修（2026-10-15）**：`column ... does not exist` / `unknown column` 改归 **`schema_mismatch`**（对齐 MySQL F-21），与"环境不支持"分开表达；`schema_mismatch` 计入 `error_states`，不再静默算通过。仍需补的是**列级探测**（`information_schema.columns`），那属下一批。
- 建议：条件项统一走 `record_skipped` + 明确 `status`，并在探针里加**列级**检查（`information_schema.columns`）。

### ⑤ `summary.txt` 的"状态统计"把 `skipped` 与 `not_applicable` 合并（L1130）

`printf '  不适用/跳过: %s\n' "$skipped"`（`skipped` = `skipped` + `not_applicable`，L1104）⇒ 人读摘要里两种语义合并成一栏，而 `collection_status.json` 里是分开的。**两份产物口径不一致**，对账时会多出/少掉一项。

### ⑥ `snapshot.collection_summary` 的计数口径与 `collection_status.json` 不同

- `snapshot.collection_summary`：`ok+empty+not_applicable+skipped` 算"successful_or_empty"、`unsupported+not_enabled+partial` 算 warning、`error+timeout+permission_denied` 算 failed。
- `plugins/postgresql/package_adapter.py::collection_quality` 的 `error_states` **另含 `schema_mismatch`**（2026-10-15）：两个集合是**判否式**的（不在集合里就算 `ok`），新状态词忘了登记就会**静默把完整度分数抬高**。
- `collection_status.json.summary`：**十个状态各自一栏**。
- ⇒ 两者**无法互相校验**。建议 Python 一律只信 `collection_status.json`（`ctx.status`）。

### ⑦ `report_adapter` 里的硬编码（`plugins/postgresql/report_adapter.py`）

| 位置 | 硬编码 | 风险 |
|---|---|---|
| L145-147 | `source_scope`：`HISTORY_CHART_IDS` 走 `"historical"`，其余走 `"realtime_snapshot"` | 新增 history 图必须**同时**改 `HISTORY_CHART_IDS` 和 `chart_section`，否则标错口径 |
| L179-186 | `item_id == "pg.backup"` 强行改写 analysis | 症状补丁（F-19）|
| L83-98 | 字符串替换 `"未检测到备份策略"` | 上游文案一改就**静默失效**（F-19）|
| L239 | `integrity.status` 写死 `"not_evaluated"` | ✅ **已修（2026-10-16）**：改读 `collection_quality.integrity`（由 `package_adapter.collection_quality` 带出真值，与 Oracle 同构）；只有旧模型缺该键时才回落 `not_evaluated` |
| L57-60 | `owner=None` / `planned_window=None` / `verification="按原检查项重新采集并复核规则状态"` | 因为 PG 的 `Finding` 没有整改三要素（F-13）|

### 建议的重构顺序

| 序 | 内容 | 为什么这个顺序 |
|:---:|---|---|
| 0 | **注释分层 L1–L5**（FIX-PLAN F-11）| 零风险；补注释的过程会让 ②③ 的错位**自己浮出来**；并提前标出要删的死旋钮 |
| 1 | **零风险三项**：死代码清理（F-05/F-06）、退出码显式化（F-07）、占位表头补全（F-02） | 纯增量/纯删除，验完即可上生产 |
| 2 | **行结构防护**（F-01）**单独一批** | 动 `inspection_core/tabular.py`（**四库共享**），必须四库各出一份报告比对 |
| 3 | **状态接线三件套**（F-12/F-14/F-15 + F-18）**同批** | 缺任何一段，报告都会更糟（没有数据可印）|
| 4 | 判据口径收口（F-16/F-17 + F-13 + F-19） | 依赖第 3 步的 `source_status` |
| 5 | 报告层收尾（F-20/F-21/F-23/F-24/F-26） | 可维护性 + 图表信号完整性 |
