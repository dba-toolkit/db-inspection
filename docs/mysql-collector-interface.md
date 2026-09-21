# mysql_inspection_standard.sh 结构与对接契约

- 脚本：`D:\AI\MYSQL\GPT\1.0\inspection\mysql_inspection_standard.sh`
- 位置：2026-09-21 由仓库根移入 `inspection/`；本文档下文所有命令均在该目录内执行
- 版本：v1.1.0-standard / 1373 行 / 76 个函数 / 18 行注释（注释密度 1.3%）
- `SNAPSHOT_SCHEMA_VERSION = 1.0`，`PACKAGE_VERSION` 见脚本头
- 本文档是 **Python 侧的唯一对接依据**。脚本任何输出变化都必须先改本文档。

---

## 1. 先回答"乱不乱"

**骨架不乱，边界乱。**

不乱的部分（值得保留，别动）：

- 职责切得干净：**只采集 + 脱敏 + 留痕 + 打包**，不做判定、不做图、不做 Word
- 一实例一包，主从拓扑交给 Python 用多个包合并
- 命名有两套一致的约定：`item_id`（`模块.对象`）和 `category`（`模块.子域`）
- 输出分四类目录，语义清晰：`tables/`（结构化）、`timeseries/`（自己采的时序）、`history/`（sar 历史）、`evidence/`（原始文本证据）
- 有四层工程保护：超时包装、失败分类、状态留痕、SHA256 清单

乱的部分（4 处，都是"模块边界 ≠ category 边界"）：

| 问题 | 表现 |
|---|---|
| ① my.cnf 归属错位 | `collect_mycnf_allowlist`（L452）被 `collect_system_static` 调用，item_id 是 `system.mycnf_allowlist`。但 my.cnf 是**数据库配置**，不是 OS 配置。放在 `system.static` 下，Python 按 category 分组时会把它归到"系统"而不是"数据库参数" |
| ② 备份三项三重错位 | `system.backup_cron` / `backup_timers` / `backup_processes` 定义在 `collect_mysql_logs_backup` 里，**item_id 前缀是 `system.`、category 是 `mysql.backup`、模块名是 `mysql_logs_backup`** —— 三套命名互不相同 |
| ③ security 与 objects 混在一个函数 | `collect_mysql_security_objects` 同时产出 `mysql.security.*`（3 项）和 `mysql.objects.*`（3 项）两个 category |
| ④ 日志与备份混在一个函数 | `collect_mysql_logs_backup` 同时产出 `mysql.logs.*`（4 项）和 `mysql.backup.*`（3 项） |

**"没注释"的量化**：18 行注释里，2 行 shebang/说明，3 行头部设计声明，剩下 13 行**全是踩坑记录**（chronyc 错误流向 stdout、空结果不输出列名、新内核磁盘字段追加、GTID 换行）。也就是说：**难点都注释了，但"这个函数干什么、输出什么、谁消费"一行没写**。76 个函数里没有一个有函数级说明。

结论：不必重写，需要一份契约文档 + 给 9 个模块入口函数各加一段说明。**本文档就是那份契约。**

---

## 2. 执行阶段地图

```
① 环境与参数        L1227-1272   解析参数、建目录、检查磁盘空间、Bash 版本
② 认证与连通        L1290-1324   login-path > password-file(600) > 交互隐藏输入 > 位置参数
                                 连接失败 exit 20
③ 能力探测          probe_capabilities        串行，必须先跑（决定后面哪些项可采）
④ 后台三路并行      ┌ realtime_sampling   30 秒时序采样（CPU/内存/网络/磁盘/MySQL 计数器）
   L1331-1333       ├ sar_history          最近 24h sadf 导出（8 个维度）
                    └ system_static        OS 静态信息 + my.cnf 白名单
⑤ MySQL 串行六组    mysql_basic → mysql_capacity → mysql_performance
   L1335-1340       → mysql_replication → mysql_security_objects → mysql_logs_backup
⑥ 收尾              derive_role_evidence → cleanup_auth
⑦ 门禁与出包        security_scan → collection_status.json / snapshot.json
                    → summary.txt → manifest.json → tar.gz
```

**并行策略是刻意的**：只有 OS 侧三路并行，MySQL 侧六组**全串行**——避免对生产库产生并发压力（L1330 注释明说了）。

---

## 3. 采集项总清单（74 项 / 13 个 category）

13 个 category 按"层"归一下，落在四层里。**注意 `category` 不等于"层"**，有 4 处错位（见 §1）。

| 层 | category | 项数 | 谁产它 |
|---|---|:---:|---|
| ① 主机 / OS | `system.static` | 22 | `collect_system_static`（含 `collect_mycnf_allowlist`） |
| ① 主机 / OS | `system.history` | 1 | `collect_sar_history`（sar 24h，8 维度） |
| ② 时序（画图数据源） | `timeseries` | 1 | `collect_realtime_samples`（5 个 CSV，30s × 6 点） |
| ③ 数据库 | `mysql.capabilities` | 1 | `probe_capabilities` |
| ③ 数据库 | `mysql.basic` | 7 | `collect_mysql_basic` |
| ③ 数据库 | `mysql.capacity` | 9 | `collect_mysql_capacity` |
| ③ 数据库 | `mysql.performance` | 10 | `collect_mysql_performance` |
| ③ 数据库 | `mysql.replication` | 9 | `collect_mysql_replication` |
| ③ 数据库 | `mysql.security` | 3 | `collect_mysql_security_objects`（同函数还产 objects） |
| ③ 数据库 | `mysql.objects` | 3 | 同上 |
| ③ 数据库 | `mysql.logs` | 4 | `collect_mysql_logs_backup`（同函数还产 backup） |
| ③ 数据库 | `mysql.backup` | 3 | 同上，但 item_id 前缀是 `system.` |
| ④ 出厂门禁 | `package` | 1 | `security_scan` |

合计 **13 个 category / 74 项**。四处错位：`system.mycnf_allowlist` 是数据库配置却挂在 `system.static`；`system.backup_*` 三项 item_id 前缀 `system.` 而 category 是 `mysql.backup`；`collect_mysql_security_objects` 同产 2 个 category；`collect_mysql_logs_backup` 同产 2 个 category。

### 3.1 系统侧（23 项）

| item_id | category | 输出 | 内容 |
|---|---|---|---|
| system.os_release | system.static | evidence/os_release.txt | os-release + uname -a + uptime |
| system.time_status | system.static | evidence/time_status.txt | 本地时间/UTC/时区/epoch |
| system.timedatectl | system.static | evidence/timedatectl.txt | NTP 同步状态 |
| system.chronyc_tracking | system.static | evidence/chronyc_tracking.txt | chrony 跟踪 |
| system.chronyc_sources | system.static | evidence/chronyc_sources.txt | chrony 源列表 |
| system.ntpq_peers | system.static | evidence/ntpq_peers.txt | ntp 对端 |
| system.lscpu | system.static | evidence/lscpu.txt | CPU 拓扑 |
| system.free | system.static | **tables/memory_snapshot.tsv** | 内存快照（`free -b`） |
| system.filesystems | system.static | **tables/filesystems.tsv** | `df -PT` 文件系统 |
| system.inodes | system.static | **tables/inodes.tsv** | `df -Pi` inode |
| system.block_devices | system.static | **tables/block_devices.tsv** | `lsblk -b` 块设备 |
| system.mounts | system.static | evidence/mounts.txt | mount 全量 |
| system.ip_address | system.static | evidence/ip_address.txt | `ip -details addr` |
| system.ip_route | system.static | evidence/ip_route.txt | `ip route table all` |
| system.ifconfig | system.static | evidence/ip_address.txt | ip 缺失时的回退，**与上一项同文件** |
| system.socket_summary | system.static | evidence/socket_summary.txt | `ss -s` |
| system.numa | system.static | evidence/numa.txt | NUMA 硬件拓扑 |
| system.sysctl_selected | system.static | **tables/kernel_parameters.tsv** | 内核参数（白名单） |
| system.hugepages | system.static | **tables/hugepages.tsv** | 大页配置 |
| system.dmesg_errors | system.static | evidence/dmesg_errors.txt | err/crit/alert/emerg |
| system.mysql_processes | system.static | evidence/mysql_processes.txt | mysqld 进程（已脱敏） |
| system.mycnf_allowlist | system.static | **tables/mycnf_allowlist.tsv** | my.cnf 白名单参数 |
| system.sar_history | system.history | history/sar_*.csv + coverage.tsv + source_files.txt | 24h 历史（8 维度） |

### 3.2 时序采样（1 项）

| item_id | category | 输出 |
|---|---|---|
| timeseries.realtime_sampling | timeseries | **timeseries/system_cpu.csv**、system_memory.csv、system_network.csv、system_disk.csv、mysql_status.csv |

5 个 CSV 的列定义在 L609-613（表头硬编码），其中 `mysql_status.csv` 有 37 个计数器列，用于 Python 侧算 QPS/TPS 与差值。

### 3.3 数据库侧（49 项）

| category | 模块函数 | 项数 | item_id（输出均为 `tables/<名>.tsv`，另注除外） |
|---|:---:|:---:|---|
| mysql.capabilities | `probe_capabilities` | 1 | capability_probe |
| mysql.basic | `collect_mysql_basic` | 7 | global_variables、global_status、engines、plugins、schemas、**innodb_status→evidence/**、open_tables |
| mysql.capacity | `collect_mysql_capacity` | 9 | database_sizes、object_counts、large_tables→large_tables_top、fragmentation→fragmentation_top、no_primary_key_summary、no_primary_key→no_primary_key_top、auto_increment_usage、partition_summary、non_innodb_tables |
| mysql.performance | `collect_mysql_performance` | 10 | long_transactions、metadata_locks→metadata_locks_pending、data_lock_waits、processlist、sql_digests→sql_digests_top、redundant_indexes、unused_indexes、table_io_top、file_io_top、wait_events_top |
| mysql.replication | `collect_mysql_replication` | 9 | replica_status、binary_log_status、binary_logs、replication_channels、replication_workers、group_replication_members、group_replication_stats、wsrep_variables、wsrep_status |
| mysql.security | `collect_mysql_security_objects` | 3 | accounts、schema_privileges、user_privileges |
| mysql.objects | `collect_mysql_security_objects` | 3 | events、routines、triggers |
| mysql.logs | `collect_mysql_logs_backup` | 4 | log_file_metadata→log_files、error_log_summary、error_log_samples、error_log_tail→evidence/ |
| mysql.backup | `collect_mysql_logs_backup` | 3 | backup_cron、backup_timers、backup_processes（**均为 evidence/*.txt**） |

### 3.4 打包（1 项）

| item_id | category | 说明 |
|---|---|---|
| package.security_scan | package | 敏感信息扫描结果，失败则**阻止打包** |

---

## 4. 输出目录树（Python 的输入）

```
mysql_inspection_v1_<host>_<ip>_<port>_<YYYYmmdd_HHMMSS>/
├── snapshot.json              实例/主机/能力/角色/采样/隐私 总览    ★Python 第一入口
├── collection_status.json     74 项的逐项状态与耗时               ★Python 第二入口
├── manifest.json              每个文件的 size + SHA256           ★Python 第三入口
├── summary.txt                人读摘要（状态统计、最慢项、非成功项）
├── tables/*.tsv               静态结构化数据（约 45 个）
├── timeseries/*.csv           5 个实时时序
├── history/                   sar_*.csv + coverage.tsv + source_files.txt
├── evidence/*.txt             原始文本证据（OS 命令输出、innodb_status、日志尾部）
└── logs/
    ├── collection.log         主日志
    └── modules/*.log          各模块 stderr
```

同名压缩包在同级目录：`<任务目录>.tar.gz`。

---

## 5. 四个入口文件的字段契约

### 5.1 `snapshot.json`（Python 第一入口）

| 顶层键 | 内容 |
|---|---|
| `schema_version` | `"1.0"` —— **契约版本，改动输出必须 bump** |
| `package_version` / `collector` | 采集器名称/版本/平台/起止时间 |
| `instance_identity` | `database_type` `database_family` `product_comment` `version` `server_uuid` `server_id` `mysql_hostname` `connect_host` `connect_ip` `instance_ip` `bind_address` `report_host` `port` `instance_tag` |
| `host_identity` | `hostname` `short_hostname` `primary_ip` `all_ipv4` `database_target_is_local` `machine_id` `os` `kernel` `cpu_count` `memory_total_bytes` |
| `time_evidence` | `host_local_time` `host_utc_time` `timezone` `ntp_synchronized` + 两个证据文件路径 |
| `role_evidence` | `role_observed` `confidence` `read_only` `super_read_only` `log_bin` `gtid_mode` `replica_status_present` `source_host` `source_port` `source_uuid` `replica_io_running` `replica_sql_running` `replica_lag_seconds` `group_replication_role` `group_replication_state` `wsrep_on` |
| `capabilities` | `performance_schema` `sys_schema` `data_locks` `data_lock_waits` `metadata_locks` `group_replication_members` `replication_connection_status` `performance_schema_error_log` `sar_command` `sadf_command`（**布尔**） |
| `sampling` | `interval_seconds` `requested_sample_count` `actual_mysql_points` `actual_cpu_points` `actual_elapsed_ms` `sar_history.*` `realtime_files.*` |
| `privacy` | `sql_text_included`（恒 true）`log_text_included` `full_configuration_included`（恒 false）`password_included`（恒 false） |
| `collection_summary` | `successful_or_empty_items` `warning_items` `failed_items` |
| `artifacts` | 各目录相对路径 |

**`role_observed` 是观察值不是判定值**——脚本明确写"最终拓扑由 Python 判断"（L1369）。

### 5.1.1 `privacy` 的取值约束（2026-09-20 定案，依据 `FIX-PLAN.md` §0.6）

| 字段 | 实际取值 | Python 侧要做什么 |
|---|---|---|
| `sql_text_included` | **恒 `true`，永不变化** | **什么都不做**。不要为它写分支——它没有开关，也不会变成 `false`。真正含 SQL 原文的只有两处：`long_transactions.query_sample`（L808）与 `processlist.SQL_TEXT`（L821）；`sql_digests_top.digest_text`（L823）取的是 `DIGEST_TEXT`，MySQL 已把字面量归一化为 `?`，不含隐私数据 |
| `log_text_included` | 随 `--include-log-text` 变化，**默认 `false`** | 为 `false` 时，报告 4.6 节**必须写"本次未采集错误日志原文"，不能留空、也不能写"无异常"**。`error_log_summary` 的聚合计数仍在包里（按 `PRIO`/`ERROR_CODE` 的计数与首末时间），所以"有没有错"判断得了，"是什么错"判断不了 |
| `full_configuration_included` · `password_included` | 恒 `false` | 同理，不要写分支 |

### 5.2 `collection_status.json`（Python 第二入口）

```json
{
  "schema_version": "1.0",
  "items": [
    {"item_id":"mysql.replica_status","category":"mysql.replication","status":"ok",
     "started_at":"...","finished_at":"...","duration_ms":123,
     "row_count":1,"exit_code":0,"output_file":"tables/replica_status.tsv","reason":""}
  ],
  "summary": {"ok":n,"empty":n,"unsupported":n,"not_enabled":n,"not_applicable":n,
              "permission_denied":n,"timeout":n,"error":n,"skipped":n,"partial":n,
              "schema_mismatch":n}
}
```

> `schema_mismatch`（F-21，批 1 已落）：表在、列不在 → **采集器缺陷**，不是"环境不支持"。
> 计数规则 `ok_count += empty/not_applicable/skipped`，`warning_count += unsupported/not_enabled/partial/schema_mismatch`，
> `error_count += error/timeout/permission_denied`。Python 请逐项读 `items[].status`，别只看这三个计数。

`output_file` 是**任务目录相对路径**；空结果时仍会写入占位表头（`known_tsv_header`），所以 Python 可以无脑打开。

### 5.3 `status` 语义表（Python 必须按这张表分支）

| status | 含义 | Python 应如何处理 |
|---|---|---|
| `ok` | 成功且有数据 | 正常消费 |
| `empty` | 查询成功但 0 行 | **正常**，不是故障。对应"该功能在本实例不适用/无数据" |
| `not_applicable` | 功能本身不适用（如未开 binlog） | 跳过，不进报告异常项 |
| `not_enabled` | 服务/特性未启用（如 chronyd 未运行） | 提示级 |
| `skipped` | 被开关主动跳过（如 `--include-log-text` 关闭） | 提示级。**必须读 `reason` 判断跳过原因**：F-19 之后 `mysql.error_log_samples` 的 `reason` 会写成自解释句"log text not collected: pass --include-log-text …"，报告里要把这句转成客户能懂的话，别丢掉 |
| `unsupported` | **表 / 变量 / 功能确实不存在**（如 5.7 无 `performance_schema.data_lock_waits`、无 `performance_schema.error_log`） | 提示级。**定论是"该环境采不到此项"，不是脚本问题**。⚠️ 脚本修正前（F-21），"列不存在"也被错误归入此项——见下一行 |
| `schema_mismatch` | **表存在、但列不存在**（如 5.7 的 `replication_applier_status_by_worker` 没有 `LAST_APPLIED_TRANSACTION`） | **需处理，不是提示级。** 这是脚本 SQL 与目标版本的列集合不匹配，属**脚本缺陷**，不能当"环境不支持"忽略。状态计数与 `summary` 对象均含此项（F-21 新增） |
| `permission_denied` | **权限不足** | 需在报告顶部提示"DBA 权限不全" |
| `timeout` | 超过该采集项的超时值（默认 `--mysql-timeout` 30s；大表查询类项可用 `--slow-query-timeout` 单独放大，F-24） | 需关注（可能 MDL 阻塞、慢查询，或**超时值设小了**——5.7 大表基数下 `sys.schema_unused_indexes` 必然超 30s） |
| `partial` | 部分成功（如采样点数不足） | 需关注 |
| `error` | 其他失败 | 需关注 |

这个分类是脚本 `classify_failure`（L199-211）用 stderr 关键词做的，**不是猜的**。修正后 `unknown column` 必须落 `schema_mismatch`，`unsupported` 只剩"表/变量/功能不存在"一种来源。

### 5.4 退出码

**2026-09-20 起，F-05 已落地：失败不再落到 `exit 0`，自动化必须按退出码分支。**

| 码 | 含义 | Python / 调度侧应做什么 |
|:---:|---|---|
| 0 | 成功，回传包已生成（或用了 `--no-package`） | 正常解包 |
| 10 | 参数错误 / 认证材料准备失败 / 环境不满足（Bash < 4、缺 mysql 客户端、目录不可写） | 判定为"采集器未启动"，重跑前先修参数 |
| 20 | MySQL 连接失败 | 判定为"连不上库"，**不要**当成"采集成功但无数据" |
| 30 | 敏感信息扫描拦截，**已阻止打包**（`logs/security_scan_findings.txt`） | **禁止**尝试解包；`collection_status.json` 里 `package.security_scan` 为 `error` 且给出命中行数 |
| 31 | 打包失败，但**任务目录可用** | 直接回传任务目录，按正常包解析（无 `manifest.json`） |
| 130 | 收到 INT/TERM/HUP 中断 | 任务目录里有 `COLLECTION_INCOMPLETE`，判定为不完整采集 |

> 30 与 31 都会在任务目录里留下 `snapshot.json` / `collection_status.json` / `summary.txt`，
> 差别是 **30 明确禁止使用**，31 可正常使用。旧版两种情况都是 0，无法区分。

---

## 6. 脚本明确不做的事（不是遗漏）

| 不做 | 由谁做 |
|---|---|
| 风险评级、健康判定 | Python 规则引擎 |
| 主从拓扑合并 | Python（跨多个包） |
| 图表 | Python |
| Word/DOCX | Python |
| 采集 SQL 结果集内容 | 不采（只采元数据与计数器） |
| my.cnf 全文 | 只采白名单（`full_configuration_included=false`） |
| 合并多实例 | 一实例一包 |

---

## 7. 变更规则（防止"改了脚本 Python 不知道"）

### 规则 1：契约版本号必须跟着输出变

- 输出的**目录结构、文件增删改名、列增删、字段语义、status 取值、退出码**发生任何变化 → **必须 bump `SNAPSHOT_SCHEMA_VERSION`**
- 只是新增一个独立文件、不动既有文件 → bump 次版本（`1.0` → `1.1`）
- 删列/改名/改语义 → bump 主版本（`1.x` → `2.0`），Python 侧必须显式适配

脚本里目前把版本号写死在头部（截图 L1079 用的是 `$SNAPSHOT_SCHEMA_VERSION`），改一个变量即可，**别散落在各处**。

### 规则 2：新增采集项必须四件事一起做

1. 用 `mysql_query_tsv` / `capture_command` 采集，`item_id` 与 `category` 命名与现有一致
2. 如果会返回空结果 → **必须在 `known_tsv_header` 登记表头**，否则空结果时 Python 拿到 0 字节文件
3. 在本文档 §3 的清单里加一行
4. 如果是新文件 → 在本文档 §4 目录树里加一行

### 规则 3：改 SQL 的 SELECT 列表 → 同步改 `known_tsv_header`

这是已经踩过的坑：3 处表头与实际列名漂移（`query_sha256`/`query_sample`、`SQL_SHA256`/`SQL_TEXT`、缺 `digest_text`）。**正常采集时表头来自 mysql 输出、看不出来；只有空结果时占位表头才暴露**，所以极易漏。

建议在 `ensure_tsv_header` 后加一道自检：文件非空时比对实际首行与 `known_tsv_header`，不一致就 `log_warn` 并写入 status。这样以后忘改会被自己发现。

### 规则 4：FIX-PLAN 里的改动逐条对照

`D:\AI\MYSQL\GPT\1.0\FIX-PLAN.md` 每条都标了"是否影响 Python"。执行时按该表的联动清单同步改。

### 规则 5：注释与代码同改（**注释撒谎比没注释更坏**）

脚本只有 18 行注释，但正因如此，**这 18 行会被当成"已经过核实的记录"来读**。所以：

1. **改代码必须同时改注释**。改动导致某段注释不再成立时，改注释或被误导——没有第三种选择
2. 已知会被本轮改造打假的 4 处，改动时必须一起处理：

| 位置 | 会被哪条打假 | 处理 |
|---|---|---|
| L856-859「GTID 续行用 sed 拼接」 | F-01 去掉 `--raw`，客户端自带转义 | 补丁与注释一起删 |
| `record_status` 附近关于写 `ok` 的口径 | F-02 要求 rows=0 写 `empty` | 注释改为"写 ok 是假 OK" |
| `sanitize_tsv_columns` 内逐行过滤说明 | F-01 后续行消失，语义变化 | 注释说明它只对 `replica_status.tsv` 生效 |
| L1102 `sql_text_included:true` | §0.6 决策 1 定案**恒开** | 旁边写死"**有意为之，勿改成变量**"，避免被反复提议加开关 |

3. **"有意为之"必须在代码里写明**。以下几处是设计选择不是缺陷，不写清楚会被下轮审查（或下一次 AI 会话）反复当成缺陷提出：不做风险评级、my.cnf 只取白名单、不合并主从拓扑、不采 SQL 结果集、`sql_text_included` 恒开、`--include-log-text` 默认关
4. 注释语言用中文，与现有 18 行保持一致

---

## 8. 待修的契约缺陷（Python 侧现在就会踩）

### 8.1 已修复（2026-09-20，批 1 / 批 2 落地，Python 需同步适配）

| 原级别 | 内容 | 现状 |
|:---:|---|---|
| P1 | `--raw` 导致自由文本撑破 TSV 行结构（F-01） | **已修**。`--raw` 只剩 `mysql_exec_raw` 一个入口（仅 InnoDB status 那段整文本证据用）。**所有 TSV 现在保证"一行 = 一条记录"**；字段内换行是字面量 `\n`、制表符是 `\t`，Python 侧按需 unescape |
| P1 | `replication_channels` / `replication_workers` 错误列随 `--include-log-text` 变名（F-18） | **已修**（随 F-06 一起消解）。列名**恒为 `LAST_ERROR_MESSAGE`**，值 = `LEFT(LAST_ERROR_MESSAGE,500)` 明文，不再有 `SHA2`。Python 不要再按 `LAST_ERROR_MESSAGE_SHA256` 取列 |
| P1 | `sanitize_tsv_columns` 3 处表头漂移（F-03） | **已修**（`query_sample` / `SQL_TEXT` / 补 `digest_text`） |
| P1 | `replica_status.tsv` 丢掉 `Last_Error` / `Last_IO_Error` / `Last_SQL_Error`（F-06） | **已修**。白名单与占位表头同时补齐；`Last_IO_Error` 里能看到完整的 1236 原文 |
| P1 | `unknown column` 被误分类成 `unsupported`（F-21） | **已修**。新增 `schema_mismatch` 状态，`unknown column` 归此 |
| P1 | 复制错误消息被 `SHA2` 哈希（F-06） | **已修**。哈希错误消息诊断价值为零，改为截断明文 |
| P2 | `capabilities` 同事实两种类型（snapshot `true/false` vs tsv `0/1`） | **已声明**。`tables/capabilities.tsv` 里加了注释写明 0/1 是 tsv 口径，**Python 只读 `snapshot.json` 的布尔值** |
| P2 | `security_scan` 拦截后仍 `exit 0`（F-05） | **已修**。见 §5.4 |
| P2 | 回退路径 `SHOW GLOBAL VARIABLES` 一行都不脱敏（F-04） | **已修**。新增 `filter_sensitive_variables()`，主查询与回退路径**取回后统一过闸** |
| P2 | 明文密码 cnf 落在任务目录内（F-11） | **已修**。`AUTH_TMP_DIR` 建在任务目录之外 + `EXIT` trap 兜底 |
| P2 | 错误日志未采集时下游无从区分（F-19） | **已修**。`error_log_samples` 的 `reason` 改为自解释句子；`summary.txt` 显式打印一行提示 |

### 8.2 仍未修（Python 侧继续避坑）

| 级别 | 内容 |
|:---:|---|
| P3 | `snapshot.json` 声明 `history/coverage.json`，同时存在 `history/coverage.tsv`（`compute_sar_coverage` 两个都写）。**两个都在**，Python 该以哪个为准尚未定 |
| P3 | `evidence/ip_address.txt` 文件名语义误导：`ip` 命令存在时只可能是 `ip addr` 输出，`ifconfig` 只在 `ip` 不存在时兜底；且 `ip` 存在但执行失败时不会退到 `ifconfig` |
| P3 | `system.backup_*` 三项的 `item_id` 前缀（system）/ `category`（mysql.backup）/ 模块名（mysql_logs_backup）三套不一致，按 category 分组时会与 `mysql.logs.*` 混在一起 |

### 8.3 批 1 / 批 2 新增或改名的文件、列（Python 必须同步）

| 变化 | 内容 |
|---|---|
| 新增文件 | `evidence/redacted_findings.txt` —— F-04 脱敏留痕。表头 `variable_name\tvalue_length\tcontains_colon`，**只在真的删了键时才出现**（文件不存在 = 本次没有需要删的键，不是漏采）。**不含任何口令值** |
| 新增文件 | `evidence/mycnf_includes.txt` —— F-02 的 `!include` 展开链（批 1） |
| 新增文件 | `evidence/innodb_status.txt` —— F-01 之后 InnoDB status 走 `mysql_exec_raw`，落 `.txt` 保留原始换行（批 1） |
| 列增 | `tables/replica_status.tsv` 新增 `Last_Error` / `Last_IO_Error` / `Last_SQL_Error` |
| 列名变更 | `replication_channels.tsv` / `replication_workers.tsv`：`LAST_ERROR_MESSAGE_SHA256`（或明文分支）→ **统一为 `LAST_ERROR_MESSAGE`** |
| 列集随版本变 | `replication_workers.tsv` 列数 7/8/9、`group_replication_members.tsv` 列数 5/6 —— 取决于目标版本/列集探测（F-20）。Python 请**按表头动态取列**，不要按固定列号取值 |
| 表头变更 | `mycnf_allowlist.tsv` 改为 4 列 `source_file` / `section` / `parameter` / `configured_value`（F-02，原来是 2 列） |
| 语义变更 | `global_variables.tsv` 会**少几个键**（`wsrep_sst_auth`、`*_ssl_key`、`rsa_public_key*` 等）。Python 按变量名硬取这些键时要改成"取不到就跳过"。**策略类变量（`default_password_lifetime` / `password_history` / `validate_password*`）一定保留**，没有被删 |

---

## 9. 注释规范（对应 `FIX-PLAN.md` 批 0 / F-27）

**现状**：1373 行 / 73 个顶层函数 / 18 行纯注释（密度 **1.3%**）/ 0 个函数有说明；主流程 L1227-1373 是裸代码，无 `main()` 包裹。

**目标**：注释密度 **≥ 20%**，新增约 300~400 行。**不改一行业务逻辑**，用 §9.4 的指纹比对命令证明。

### 9.1 五层结构

| 层 | 对象 | 数量 | 行/处 | 写什么 |
|---|---|---:|---:|---|
| L1 | 文件头 | 1 | ~50 | 定位（只采集不判定）、7 阶段流程、退出码表、全局约定（`umask 077` / `LC_ALL=C` / `timeout`）、依赖命令、隐私声明、维护者须知（指向本文档 §7） |
| L2 | 阶段分隔 + 13 个 category 分隔 | ~20 | 4~6 | 阶段序号、参与模块、串行/并行、产出 category。**主流程必须补** |
| L3 | 10 个模块入口函数头 | 10 | ~14 | 见 §9.2 模板 |
| L4 | 采集引擎函数头 | 10 | ~8 | `mysql_exec` / `mysql_exec_raw` / `mysql_query_tsv` / `mysql_query_fallback` / `record_status` / `capture_command` / `classify_failure` / `known_tsv_header` / `ensure_tsv_header` / `sanitize_tsv_columns`——**改一处影响 74 项**，须写参数、stdout 契约、返回码、副作用（写哪个文件） |
| L5 | 其余函数头 | ~52 | 2 | 一行：用途 + 输出文件（产 item 的写 `item_id`） |

行内注释只写"**为什么**"，禁止复述代码。

### 9.2 L3 模块入口模板

```bash
# ---------------------------------------------------------------------------
# collect_mysql_replication —— 采集复制相关证据
#
# category : mysql.replication
# 产出项   : replica_status / binary_log_status / binary_logs /
#            replication_channels / replication_workers /
#            group_replication_* / wsrep_*
# 依赖能力 : LOG_BIN, REPL_CONN_STATUS_AVAILABLE, GR_MEMBERS_AVAILABLE
# 输出位置 : tables/*.tsv
# 注意     : 8.0.4+ 用 SHOW REPLICA STATUS / SHOW BINARY LOG STATUS，
#            低版本回退 SHOW SLAVE STATUS / SHOW MASTER STATUS
# 不做     : 不合并拓扑、不判断主从健康（交 Python）
# ---------------------------------------------------------------------------
collect_mysql_replication() {
```

### 9.3 L4 采集引擎模板

```bash
# ---------------------------------------------------------------------------
# mysql_query_tsv <item_id> <category> <outfile> <sql...>
#
# 作用   : 执行 SQL 并把结果落成 TSV，逐项登记 collection_status.json
# stdout : 无（结果写 $outfile）
# 返回码 : 0 成功 / 非 0 = mysql 客户端退出码，由 classify_failure 归类
# 副作用 : ① 写 $outfile（空结果时用 known_tsv_header 补占位表头）
#          ② 追加一行到 $STATUS_PARTS_DIR
# 注意   : F-01 修完后**不要**加 --raw，靠客户端转义保住 TSV 行结构
# ---------------------------------------------------------------------------
```

### 9.4 验收（"零逻辑改动"必须用命令证明）

```bash
cd inspection                                                      # 采集脚本所在目录
cp mysql_inspection_standard.sh mysql_inspection_standard.sh.bak   # 改之前留底
# ……只加注释……
strip() { grep -v '^[[:space:]]*#' "$1" | grep -v '^[[:space:]]*$'; }
diff <(strip mysql_inspection_standard.sh.bak) <(strip mysql_inspection_standard.sh) \
  && echo "逻辑零改动 ✓" || echo "夹带了逻辑改动 ✗"
bash -n mysql_inspection_standard.sh
```

指纹 diff **必须为空**；`grep -c '^[[:space:]]*#' 脚本 / 总行数` **≥ 0.20**。

### 9.5 必做覆盖的 10 个模块入口

`collect_system_static`、`collect_realtime_samples`、`collect_sar_history`、`probe_capabilities`、`collect_mysql_basic`、`collect_mysql_capacity`、`collect_mysql_performance`、`collect_mysql_replication`、`collect_mysql_security_objects`、`collect_mysql_logs_backup`。

---

## 10. 建议的重构顺序（不动逻辑，先立契约）

1. **冻结契约**：本文档定稿，`SNAPSHOT_SCHEMA_VERSION` 明确为 `1.0`，Python 侧按本文档写测试
2. **补注释（批 0 / F-27）**：按 §9 的五层结构补，**零逻辑改动**，用 §9.4 的指纹比对自证。L1 文件头里的退出码表等 F-05 落地后再补，避免写两遍
3. **修契约缺陷**：FIX-PLAN 批 1 + 批 2（含 §8 的两条 P1）
4. **修边界错位**：可选。把 `mycnf_allowlist` 挪到数据库侧需要改 `item_id`，**属于破坏性变更**，留到 schema 2.0 一起做
5. **补采集项**：FIX-PLAN 批 3，新增项全部是**增量**，不破坏既有契约
