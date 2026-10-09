# sqlserver_inspection.ps1 结构与对接契约

- 脚本：`inspection/sqlserver_inspection.ps1`
- 版本：v1.3.5 / **1875 行 / 18 个函数 / 147 行注释（注释密度 7.8%）**
- `snapshot.schema_version = "1.1"`、`collector_version = "1.3.5"`（两者关系见 §10-①：schema 只随契约变化，collector 随代码变化）
- 本文档是**下游（`plugins/sqlserver/*`、`analyze.py`）的唯一对接依据**。脚本任何输出变化都必须先改本文档。
- v1.1.0 → v1.2.0 的变更见 `docs/sqlserver-collector-gap-plan.md` §8「落地状态」；v1.2.0 → v1.3.0 的变更见同文档 §10 / §11；**v1.3.0 → v1.3.1 的变更见 §15（批 A：把已采到的数据接通到报告）**；**v1.3.1 → v1.3.2 的变更见 §17（批 C：语句归属库口径修正 + 还原验证 + 作业步骤 + CPU 调度采样）**；**v1.3.2 → v1.3.3 的变更见 §19（真机首跑崩溃修复：三个不存在的列名 + StrictMode 纪律 + 单元素数组拆包）**；**v1.3.3 → v1.3.4 的变更见 §20（真机首跑暴露的另外三个 SQL 缺陷：`dm_exec_sessions.physical_reads` / `dm_server_services.servicetype|version` / 表变量合并声明）**；**v1.3.4 → v1.3.5 的变更见 §21（产物路径回显：`-Help` 与结尾 stdout 各打一次完整路径；默认输出目录**不变**，`%TEMP%\sqlserver_inspection`）**。
- **v1.3.0 是 bit 列语义的分水岭**：`collector_version < 1.3.0` 的采集包里，所有「值为真」的 bit 列都是 `null`，**不是 `false`**。历史包的 `null` 只能当「未知」用，不得当「否」——详见脚本头部 `bit 列语义` 段与 §10-⑨。

---

## 1. 先回答"乱不乱"

**骨架不乱，注释密度仍偏低。**

v1.1 的三个"边界"毛病已经处理掉两个：

| v1.1 的问题 | 现状 |
|---|---|
| 状态只有两态 | 已补为五态：`success` / `success_no_data` / `failed` / `skipped` / `not_applicable`，见 §6 |
| 中文模块名 / 英文键名混用 | 模块名仍在中英混用，但**去掉了两个误导性的「（深度）」后缀**（`无主键表` / `大表统计` 本来就默认采）；下游按名字匹配的风险已由 `tests/test_sqlserver_module_names.py` 拦住 |
| 注释密度 1.8%、无函数级说明 | v1.3.4 为 7.7%（143 行），新增注释集中在"为什么这么采 / 谁消费 / 曾经写错什么"；**仍未达本项目 ≥20% 的目标** |

依然值得保留的骨架：

- 职责干净：只采集 + 落 JSON + 生成 Markdown + 打包。**判定、评分、图表生成全在 Python 侧**，脚本不越界。
- 单一入口 + 参数面清楚（`-Server/-Port/-User/-Password/-OutputDir/-SampleCount/-SampleIntervalSeconds/-QueryStatsWindowDays/-ExcludeSqlText/-IncludeDeepChecks/-NoPackage`）。
- 统一采集引擎 `Invoke-CollectionModule`：单独 try/catch、逐项记 `module_status`、失败进 `limitations` —— **不会因为一项失败丢掉整包**。
- 非 SQL 执行类模块走 `Add-ModuleStatus`（OS 采集 / 版本不适用 / 有意跳过）。**v1.3.1 起它接受 `-Begin`**：
  传了才算 `elapsed_ms`，不传写 `null` 而不是 `0` —— 详见脚本头部 `elapsed_ms 语义` 段。
- 失败模块**保留空数组而不是删键**（`$snapshot[$TargetKey] = @()`），下游 `snapshot.get(key, [])` 不会炸。
- 安全敏感字段处理正确：`sys.sql_logins` **没有** SELECT `password_hash`（只用 `PWDCOMPARE('', password_hash)=1` 判断空密码），`-ExcludeSqlText` 能一键抹掉 6 个模块的 `sql_text`。

---

## 2. 执行阶段地图（v1.3.5 行号）

```
① 参数与帮助          L74-126     参数解析；-Help 直接 exit 0（块在 L93-124，here-string 用**单引号**，见 §10-13）
（工具函数）          L127-326    文件头契约 → Convert-Table / Get-ObjProp / Set-ObjProp / Get-ObjArray / Invoke-InspectionQuery
② 连接与认证          L327-336    Windows 集成 / SQL 登录(SecureString)；连接失败 throw 退出
③ 实例基础信息        L337-399    dm_os_sys_info + dm_os_windows_info；单独 try/catch，不走 Invoke-CollectionModule
④ 目录改名            L400-436    采集到实例名后用 server_name + connection_ip + port 重命名输出目录
⑤ 41 个常规采集模块   L437-1227   全部串行，逐项 Invoke-CollectionModule（默认 90s，库级重活 300s / 碎片 600s）
   └ 深度三项         L1228-1338  仅在 -IncludeDeepChecks 时执行（陈旧统计 / 索引碎片 / 孤立用户）
⑥ OS 指标             L1339-1525  仅当目标就是本机（-Server . 或本机名/IP）时采集；远程标 skipped
⑥-后置 性能采样        L1526-1565  6 个计数器 + 4 个调度器指标 × N 个采样点，差分算 Batch/TPS
⑦ 写快照与清单         L1566-1573  snapshot.json / manifest.json（manifest 写两次，第二次补 inspection_report.md）
⑧ Markdown 报告        L1574-1861  脚本自带简报（Python 侧不使用）；跑完后 L1854-1860 打一次产物路径
⑨ 退出码              L1862-1875  有 failed 模块 → exit 2，否则 exit 0
```

三个容易踩的阶段语义：

- **阶段 ③ 的 `instance` 与阶段 ⑥ 的 `os_metrics` 分别来自两个不同的数据源。** `instance.os_version` 走 SQL 侧 `sys.dm_os_windows_info`（属于**目标实例**）；`os_metrics.*` 走 WMI/`Get-PhysicalDisk`（属于**执行机**）。远程连库时后者会被整块标 `skipped` —— 这一条是刻意的，见 §6 与 §9-⑨。
- **阶段 ⑥ 的位置在深度检查之后、性能采样之前**，所以本地执行时 `os_metrics.samples` 与 `performance_samples` 是两个不同时刻的序列，不要混算。
- **阶段计数口径**：⑤ 的 41 项常规 + 深度 3 项 = **44 个采集模块**，与 §4 的 1–44 行一一对应；另有 5 个「元数据在 collection、数据在顶层」的键不经 `Invoke-CollectionModule`（见 §3 末）。
- **阶段 ③ 失败不再只是"少一块数据"**：v1.3.3 起该 catch 会 `Write-CollectorLog ... 'ERROR'`（可搜索 `实例基础信息 failed:`）。此前它只写 `limitations` / `module_status`，而这两者都要等包产出才看得见 —— 脚本在下游崩掉时包根本产不出来，于是"哪条 SQL 错了"永远查不出来（§9-⑯）。
- **耗时几乎全部来自两段采样循环，不是那 47 个模块。** 真机实测：47 个模块合计约 **4 秒**，而阶段 ⑥（OS 指标 44.6s）+ 阶段 ⑥-后置（性能采样 25.1s）= **69.8s，占采集全程 73.5s 的 95%**。拆开看：**50s 是纯 `Start-Sleep`**（两段各 6 点 × `-SampleIntervalSeconds 5`），另约 **10s 是一次性 WMI provider 冷启动**（`Win32_PerfFormattedData_PerfOS_*` 首查 8.6s vs 热态 1.4s；`Get-PhysicalDisk` 首查 2.5s vs 40ms —— 都只付一次），余下约 10s 是 30 次例行 CIM 调用（≈280ms/次）。**实测同时否定了两个"看起来该优化"的写法**：`-Filter` 服务端过滤（1396ms / 5 次）与复用 `CimSession`（1409ms / 5 次）相比现状的客户端 `Where-Object` 过滤（1432ms / 5 次）**没有差别**，不值得改。要缩短只能调参数（`-SampleCount 3 -SampleIntervalSeconds 2` → 采样段降到约 4s）；**`-SampleCount 1` 不可用**：格式化 perf 类首点失真（真机首点 `cpu_pct=0` / `cpu_idle_pct=98`），且性能采样的差分算不出来。

**为什么全部串行**：脚本没有并行逻辑，一次只开一条连接（`Invoke-InspectionQuery` 每次 new 一个 `SqlConnection`）—— 对生产库友好，符合只读巡检定位。

---

## 3. snapshot 顶层键契约（49 个采集键 + 4 个元数据键）

**下游解析前必须知道哪些键是"对象"、哪些是"数组"** —— 这一条没写在任何地方，踩了会直接抛 `TypeError`：

| 键 | JSON 类型 | 说明 |
|---|---|---|
| `schema_version` / `collector_version` | 字符串 | 契约版本 |
| `target` | **对象** | `server/port/database/authentication` |
| `collection` | **对象** | `started_at/finished_at/module_status[]/limitations[]`。**`module_status[].elapsed_ms`（v1.3.1 起）**= 该模块自身耗时；未计时的模块为 `null`，不得折算成 0 |
| `instance` | **对象** | 单行；采不到时是 `{}` |
| `tempdb` | **对象** | 空间字段 + **嵌套 `files[]`** |
| `security` | **对象** | 嵌套 `sql_logins[] / sysadmin_members[] / linked_servers[] / security_checks[]` |
| `os_metrics` | **对象** | 嵌套 `identity{} / physical_disks[] / disk_map[] / samples[]`；**仅本机执行时存在** |
| `database_summary` | **数组（单元素）** | SQL 返回单行，仍是数组 |
| `memory_status` | **数组（单元素）** | 同上 |
| `system_memory` | **数组（单元素）** | 同上 |
| `ha_status` | **数组（单元素）** | 同上 |
| 其余 40 个采集项键 + `performance_samples` | **数组** | 采不到时是 `[]` |

**"元数据在 collection 里、数据在顶层"的键共 5 个**：`instance / tempdb / security / performance_samples / os_metrics`。它们不经过 `Invoke-CollectionModule`，`module_status` 里的中文名分别是 `实例基础信息` / `TempDB` / `安全与权限` / `性能采样` / `OS 指标`。

---

## 4. 采集项总清单（47 项）

| # | module_status.name | snapshot 键 | 内容 | 深度开关 |
|---:|---|---|---|---|
| 1 | 数据库清单与完整性 | `databases` | sys.databases + 文件大小 + 最后 DBCC | |
| 2 | 备份状态 | `backups` | 最后全/差/日志备份、校验和、加密类型、大小 | |
| 3 | 备份历史（近7天） | `backup_history` | type ≠ 'F'；**含耗时、压缩后大小、备份文件路径、LSN 链** | |
| 4 | 无备份数据库 | `no_backup_databases` | 无 type='D' 记录的库 | |
| 5 | 还原验证历史 | `restore_history` | msdb.dbo.restorehistory 按库聚合：还原次数 / 最近还原 / 最近实际还原 / 最近仅验证（**v1.3.2 新增**） | |
| 6 | 数据库RCSI状态 | `database_rcsi` | RCSI / Snapshot Isolation | |
| 7 | 数据库大小汇总 | `database_summary` | 用户库数、实例库数、日志文件数、数据/日志合计 | |
| 8 | 数据库文件与IO | `database_files` | 文件路径、增长、max_size、累计 IO 延迟 | |
| 9 | 存储卷容量 | `volumes` | 三层合并：`dm_os_volume_stats` + `xp_fixeddrives` + 备份路径推导（**含备份盘**） | |
| 10 | 事务日志空间 | `log_space` | DBCC SQLPERF(LOGSPACE) | |
| 11 | VLF摘要 | `vlf_summary` | 每库 VLF 数 / 活动 VLF（仅 2017+，否则 `not_applicable`） | |
| 12 | 可疑页面 | `suspect_pages` | msdb.dbo.suspect_pages | |
| 13 | 等待统计 | `wait_stats` | TOP30 累计等待 | |
| 14 | 阻塞链 | `blocking` | 瞬时阻塞快照 | |
| 15 | 活动请求 | `active_requests` | TOP50 | |
| 16 | 连接数统计 | `connection_stats` | 按 IP/主机/程序/登录名分组 | |
| 17 | 会话资源占用 | `session_activity` | 按 login_name × status 聚合：会话数、累计 CPU(ms)、物理读（源列是 `es.reads`，`dm_exec_sessions` 没有 `physical_reads`）/逻辑读、写次数、最早登录时间 | |
| 18 | 高消耗SQL | `top_queries` | 按 total_worker_time；含 max/min 耗时、spills、query_hash | |
| 19 | 高逻辑读SQL | `top_queries_logical_reads` | 同上列集合 | |
| 20 | 高执行次数SQL | `top_queries_executions` | 同上列集合 | |
| 21 | 高物理读SQL | `top_queries_physical_reads` | 同上列集合 | |
| 22 | 缺失索引建议 | `missing_indexes` | dm_db_missing_index_* | |
| 23 | 未使用索引 | `unused_indexes` | dm_db_index_usage_stats | |
| 24 | 实例配置 | `configurations` | sys.configurations 白名单 17 项 | |
| 25 | SQL Server 服务与启动参数 | `server_services` | dm_server_services；**Binn 路径 + 启动类型** | |
| 26 | 缓冲池使用分布 | `buffer_pool` | 按数据库 | |
| 27 | 进程内存状态 | `memory_status` | dm_os_process_memory | |
| 28 | 内存组件分布 | `memory_clerks` | dm_os_memory_clerks TOP40（**不含 AWE 列**） | |
| 29 | 系统内存状态 | `system_memory` | dm_os_sys_memory | |
| 30 | CPU按数据库分布 | `cpu_by_database` | TOP15 | |
| 31 | SQL Agent失败作业 | `failed_jobs` | 近 24h run_status=0 | |
| 32 | SQL Agent作业清单 | `agent_jobs` | 全量 + 上次执行状态 | |
| 33 | SQL Agent作业步骤 | `job_steps` | msdb.dbo.sysjobsteps TOP300：步骤名 / 子系统 / 失败动作 / 重试 / 上次结果与耗时；**command 前 400 字符**（**v1.3.2 新增**） | |
| 34 | 高可用状态 | `ha_status` | IsHadrEnabled / AG 数 / 副本数 / AG 库数（单行） | |
| 35 | Always On | `availability_replicas` | dm_hadr_*；**含 availability_mode / last_redone_time / 延迟秒 / 连接错误** | |
| 36 | 数据库镜像 | `database_mirroring` | | |
| 37 | 日志传送 | `log_shipping` | | |
| 38 | 复制拓扑 | `replication` | **分发库** `MS*` 表 + 订阅侧视图，压成单结果集（`record_type` 区分） | |
| 39 | 错误日志摘要 | `error_log_summary` | 中英双语筛选 + 轮转遍历 + `error_number` 聚合键 | |
| 40 | 无主键表 | `tables_without_pk` | 逐库 INFORMATION_SCHEMA（**默认采**） | |
| 41 | 大表统计 | `large_tables` | 逐库 TOP50 → 全局 TOP30（**按空间排序**，行数只算堆/聚集索引） | |
| 42 | 陈旧统计信息（深度） | `stale_statistics` | | ✅ |
| 43 | 索引碎片（深度） | `fragmented_indexes` | page_count ≥ 1000 | ✅ |
| 44 | 孤立用户（深度） | `orphaned_users` | | ✅ |
| — | 实例基础信息 | `instance` | 单行（含 `dm_os_windows_info` 的 OS 版本、默认数据/日志/备份路径） | |
| — | TempDB | `tempdb` | 空间 + files[] | |
| — | 安全与权限 | `security` | 4 个子键 | |
| — | 性能采样 | `performance_samples` | 6 个计数器 + **4 个 CPU 调度器指标**（`runnable_tasks_count`/`runnable_tasks_max`/`work_queue_count`/`pending_disk_io_count`，v1.3.2 起）时序 | |
| — | OS 指标 | `os_metrics` | 身份 + 物理盘介质 + 盘符映射 + N 个采样点（**仅本机**） | |
**`会话资源占用`（#17）与 `连接数统计`（#16）不是重复采集**：后者回答「谁连着」（client_ip × 主机 × 程序 × 登录名），前者回答「谁在耗资源、处于什么状态」（login_name × status × 累计 CPU/IO），维度不同。
现场报告 1.9 节「活跃的用户进程及数量」问的是后者之外的那一层——1265 个会话绝大多数 `sleeping`，只看连接数看不出「哪个账号在真耗资源」。累计值随会话存活时间增长，故同时给 `earliest_login_time`。

**两个「（深度）」后缀已去掉**：`无主键表` / `大表统计` 在 v1.1 里名字带「（深度）」但默认就采，会让人（和下游）误判成"默认没有这一项"。`presentation.py` 的 `_coll()` 保留旧名作为第二参数，历史包的解释结果不变。

---

## 5. 输出目录与文件

```
<OutputDir>/sqlserver_inspection_v1_<ip>_<server>_<port>_<stamp>/
├── snapshot.json           ← Python 唯一入口
├── manifest.json           ← files[] + sha256（最终 3 项：snapshot.json / collector.log / inspection_report.md）
├── inspection_report.md    ← 脚本自带 Markdown，Python 侧不使用
├── collector.log
└── <同名>.zip              ← 除非 -NoPackage
```

注意四点：

- `manifest.json` **写了两次**（`L1571` 先写 2 项，`L1839` 在 Markdown 生成后追加 `inspection_report.md`）。新增输出文件必须同步加在第二次写入之前，否则 manifest 与实际内容不符。
- **未指定 `-OutputDir` 时默认写到 `%TEMP%\sqlserver_inspection`**（`L143-147`），与脚本自身位置无关；建目录失败再退到当前目录并 `Write-Warning`（见 §9-⑩）。**这是刻意的，不要改回"脚本同目录"**——原因见 §9-⑩ 与 gap-plan §F-21（AGENTS.md「采集脚本不得依赖自身所在目录」）。
- **跑完会把最终产物路径单独打一遍**（`L1854-1860`，v1.3.5 新增）。默认目录在 RDP / 多会话机器上会带 `\2\` `\3\` 段，现场翻 AppData 很费劲；想让它落到顺手的位置就加 `-OutputDir`，例如 `-OutputDir "$env:USERPROFILE\Desktop"`（`-Help` 里有同一条示例）。⚠ `OUTPUT_DIR=` / `OUTPUT_PACKAGE=` 两行是**机器可读**的，新增的人类提示排在其后，**不要改变这两行的相对位置与格式**。
- 目录名里的 IP 来自 `dm_exec_connections.local_net_address`，**本地连接时为 NULL**，脚本会用 `Get-NetIPAddress` 兜底（取 InterfaceMetric 最小的非回环 IPv4）—— 多网卡机器上可能取到心跳/管理网卡。

---

## 6. status 语义（五态）

| 取值 | 含义 | 下游应如何处理 |
|---|---|---|
| `success` | SQL 执行成功且**有行** | 正常取数 |
| `success_no_data` | SQL 跑通但**0 行** | 计入"已评价"，但**不得当成"无风险"**；`presentation.py` 的采集完整度把它算作 ok |
| `failed` | 异常被 catch，已写入 `limitations` | 该模块结论必须标"未评价"，不得当 0；脚本退出码变 2 |
| `skipped` | **有意不采**（远程执行时的 OS 指标；深度检查未开） | 不进采集完整度的分母；报告应显示"未采集"而非"正常" |
| `not_applicable` | **环境不适用**（AG 未启用 / 复制未配置 / 版本不支持 VLF） | 同上，且**不得折叠成"达标"** |

`plugins/sqlserver/report_adapter.py` 的 `_COLLECTION_STATUS_MAP` 把 `success` 与 `success_no_data` 都映射成标准模型的 `ok`，其余原样透传。

---

## 7. 退出码

| 场景 | 行为 |
|---|---|
| `-Help` | `exit 0` |
| 连接失败 | `throw`，**不产出任何包**（无证据留档），PowerShell 退出码非 0 |
| 采集过程有模块失败（`failed`） | **照常出包**，退出码 **2** |
| 全部成功 / 仅有 skipped / not_applicable | 退出码 0，stdout 打印 `OUTPUT_PACKAGE=<zip>` 或 `OUTPUT_DIR=<dir>` |
| 被 dot-source 调用（无 `$MyInvocation`） | 不 exit，只把结果对象留在会话里 |

区分「全成功」与「成功但有 3 个模块失败」现在可以只看退出码。

---

## 8. 下游消费映射

`plugins/sqlserver/presentation.py` 把 snapshot 映射成报告模型，Word 14 章的来源如下：

| 报告章节 | section_id | 主要消费键 |
|---|---|---|
| 实例与数据库概览 | `instance` | `instance.*`、`databases`、`database_summary`、`server_services`、`os_metrics.identity` |
| 备份与恢复 | `backup` | `backups`、`backup_history`、`no_backup_databases`、`suspect_pages`、`restore_history` |
| 内存、缓冲池与CPU | `memory` | `buffer_pool`、`memory_status`、`memory_clerks`、`system_memory`、`cpu_by_database` |
| 容量、存储与文件 | `capacity` | `volumes`、`log_space`、`vlf_summary`、`database_files`、`os_metrics.physical_disks` |
| 性能基线 | `perf_baseline` | `performance_samples`、`os_metrics.samples` |
| 等待、阻塞与会话 | `waits` | `wait_stats`、`blocking`、`active_requests`、`connection_stats`、`session_activity` |
| TempDB | `tempdb` | `tempdb`（含 `files`） |
| SQL 与索引 | `sql_index` | `top_queries`、`top_queries_logical_reads`、`missing_indexes`、`unused_indexes` |
| 数据库对象 | `db_objects` | `tables_without_pk`、`large_tables`、`database_rcsi` |
| 安全与身份 | `security` | `security.*`（`orphaned_users` 只进附录 inventory） |
| **高可用与复制** | **`ha`** | `availability_replicas`、`ha_status`、`database_mirroring`、`log_shipping`、`replication` |
| 作业与错误日志 | `ops` | `failed_jobs`、`agent_jobs`、`job_steps`、`error_log_summary` |
| 实例关键配置 | `config` | `configurations` |
| 实例与数据库概览 / 实例关键配置 / 作业与错误日志 | 同左 | v1.3.0 起另接 `INSTANCE.VERSION_SUPPORT`、`INSTANCE.ADHOC_WORKLOADS` / `INSTANCE.BLOCKED_PROC_THRESHOLD` / `INSTANCE.BACKUP_COMPRESSION`、`AGENT.JOB_OVERDUE` |

`ha` 已在 v1.2 加进 `word_report.py` 的 `section_order`（原先漏登记，会被排到附录之前）。当前 `section_order` 共 14 项，双向契约由 `tests/test_sqlserver_plugin.py::test_word_profile_covers_every_emittable_section` 守住。

**【v1.3.1 已修】~~`top_queries_executions` / `top_queries_physical_reads` 只进附录 inventory，没有独立报告条目~~**
采集端本就各采 TOP30，`presentation.py` 的 `inventory` 里也有它们，但此前没有报告条目、也没有人查它们的
`module_status`。v1.3.1 补了两个条目：「高执行次数SQL」「高物理读SQL」（`sql_index` 章节，
数据源同一份 `sys.dm_exec_query_stats`），`module_status` 随之被 `_coll` 查到。

**v1.3.1 另新增第 14 章 `collection_detail`（采集执行明细）**：逐模块列出 状态 / 行数 / 耗时ms / 错误，
按耗时降序。它要回答的是「这次采集哪一块没成、哪一块拖慢了窗口」——
此前这些信息只体现为第 1 章的一个完整度百分比，交包被问起只能回去翻 `collector.log`。

---

## 9. 已知契约缺陷

**① 【已撤回 / v1.3.0 复核】`is_query_store_on` 并非恒为 NULL**
v1.2.0 曾在此登记"`CAST(NULL AS bit)` 占位导致 `DB.QUERY_STORE` 永不触发"。复核源码后确认**该判断有误**：
主查询里确有一处 `CAST(NULL AS bit) is_query_store_on` 作旧版列形状占位，但紧随其后有一条按
`$productMajor` 版本门控的二次查询（SQL Server 2016+）会**覆盖**该列；基线快照里 `is_query_store_on`
的实际取值是 `false`（不是 `null`），说明覆盖路径真实生效。阈值键 `require_query_store_for_user_db`
有真实消费路径，不是死旋钮。此条撤回，不再列为待修项。
（保留记录是为了固化一条排查习惯：**看到 `CAST(NULL AS ...)` 先往下读几行再下结论**。）

**② `replication` 的两个等价形态并存**
`record_type` 单结果集（v1.2）与 `publication_name` 扁平形态（v1.1）。`presentation.py` 与
`rules.py` 都按 `"record_type" in row` 分派，历史包解释结果不变；但**新增列只加在新形态上**，
旧包永远缺这些列（渲染成 `-`，不是 0）。

**③ `error_log_summary` 依赖 `xp_readerrorlog` 权限**
采集账号非 sysadmin 时可能读不到轮转文件；此时 `row_count=0` 且状态是 `success_no_data`，
**与"真的没有错误"无法区分**。报告侧靠 `limitations` 与采集完整度提示，不能只看这一项下结论。

**④ `volumes` 的三种来源精度不同**
`dm_os_volume_stats`（有总量/可用率）→ `xp_fixeddrives`（只有剩余 MB）→ 备份路径推导（只有盘符）。
同一份报告里 `E:\` 会同时出现两行（`XP_FIXEDDRIVES` 与 `BACKUP_MEDIA`），
`total_mb` / `free_pct` 为 `None`。按 `source` 列区分，不要简单去重。

**⑤ `volumes` 里的备份盘容量可能缺失**
备份盘没有数据库文件时进不了 `dm_os_volume_stats`，只剩"剩余 MB"，
所以"备份盘剩余空间 < 最近一次完整备份大小"这类判断**只能在有 `total_mb` 的卷上做**。

**⑥ `object_name` 重复的连接错误已去重，但延迟类判据未去重**
`HA.AG_CONNECT_ERROR` 按 `replica_server_name` 去重；`HA.AG_SYNC_MODE` / `HA.AG_LAG` / `HA.AG_QUEUE`
是**按 (AG, 数据库) 逐条判定**，同一副本承载多个 AG 数据库时会出多条 —— 这是刻意的（延迟是库级事实），
但要避免在报告摘要里重复计数。

**⑦ 注释密度 4.8%（81 行），仍无完整的函数级"用途 / 产出 / 谁消费"。**

**⑧ `_coll()` 找不到模块名时静默回退成 `ok`**【P1，**v1.3.1 已修**】
原实现返回 `{"status": "ok", "row_count": 0}` —— **改名不同步会让报告静默显示"正常"**。
v1.3.1 改为回退 `{"status": "not_evaluated", "row_count": 0}`，经适配层渲染为「未判断」。
静态拦（每个 `_coll` 的主名必须能在采集端解析到）仍由 `tests/test_sqlserver_module_names.py` 负责。

**⑨ 远程执行时 OS 章节整体缺失，这是设计而非缺陷**
`os_metrics` 属于**执行机**，远程连库时把本机数据写进客户报告会得出完全错误的结论，
所以整块标 `skipped` 并写明原因。需要 OS 章节时必须 `-Server .` 在目标机本地执行。

**⑩ `-OutputDir` 默认值取自脚本自身所在目录**【P2，v1.3.0 **已修**】
v1.2.0 的 `[string]$OutputDir = $PSScriptRoot` 违反 AGENTS.md「采集脚本不得依赖自身所在目录」——
脚本放到仓库 `inspection/` 下被直接执行时，采集包会落进仓库。
v1.3.0 改为 `[string]$OutputDir = ''`（`L65`）+ 缺省回退
`Join-Path ([System.IO.Path]::GetTempPath()) 'sqlserver_inspection'`（`L111-113`），
建目录失败再退到当前目录并 `Write-Warning`（`L114-117`）。
**同步面**：`-Help` 文案（`L88`）、脚本头部 `.PARAMETER`、README 与本文档 §5 均已改为 `%TEMP%\sqlserver_inspection`。

**⑪ 两个 `top_queries_*` 模块采了但只进附录**【**v1.3.1 已修**】
`高执行次数SQL` / `高物理读SQL` 采集一直正常，`presentation.py` 的 `inventory` 里也有它们，
但**没有独立的报告条目，也没人查它们的 `module_status`**。v1.3.1 补齐两个报告条目（见 §8）。

**⑫ `replication.RECORD` 的 LSN 字段为取证留存**
`backup_history.database_backup_lsn / first_lsn / last_lsn` 供人工比对日志备份链断点，
当前**没有任何自动化判据**（无 `BACKUP.LSN_CHAIN` 规则），按 AGENTS.md
"确属取证留存的字段写明豁免理由即可"登记在此，不强行补分析。

**⑬ 采集状态曾被写死成"达标"（3 处）**【P1，**v1.3.1 已修**】
`presentation.py` 的「性能采样」「SQL登录」「sysadmin成员」三个条目的 `collection.status`
原为字面量 `"ok"`，与 `module_status` 里的真实状态无关 —— 采集端部分失败时报告仍显示达标，
直接违反 AGENTS.md「status 必须由采集数据算出来」。v1.3.1 改为读真实模块：
前两者取各自专用块登记的状态；后两者共用一个 `_cst("安全与权限")` 只取状态、行数仍各算各的
（安全块是一个 try 包住多类查询，`module_status` 只有一条，状态可共用、行数不能共用）。

**⑭ 采集耗时曾对四个专用块恒为 0**【P1，**v1.3.1 已修**】
`elapsed_ms` 是判断"谁拖慢了采集窗口"的唯一现场证据，而实例基础信息 / TempDB / 安全与权限 /
性能采样四个专用块与 `Add-ModuleStatus` 一律写死 `0` —— 最可能慢的块反而显示最快。
v1.3.1 各自计时（详见脚本头部 `elapsed_ms 语义` 段）。

**⑮ `sys.dm_exec_sql_text.dbid` 对 ad-hoc 计划恒为 NULL，曾被当作数据库名**【P1，**v1.3.2 已修**】
「CPU按数据库分布」与四个 TOP SQL 都用 `DB_NAME(st.dbid)` 取库名，两处都错：
- `sys.dm_exec_sql_text.dbid` **只对存储过程 / 预编译批次有值**，ad-hoc（即席）计划恒为 `NULL`；
- 更糟的是「CPU按数据库分布」还自带 `WHERE ... DB_NAME(st.dbid) IS NOT NULL` 过滤，
  等于把 ad-hoc 计划的执行统计**全部丢弃** → 基线包里该模块 `成功 / 0 行`、报告 5.x 根本没有这张表；
  四个 TOP SQL 的「数据库」列则 30/30 全空。**一处口径错，两个模块出问题。**
v1.3.2 统一改用 `sys.dm_exec_plan_attributes(plan_handle)` 的 `attribute=N'dbid'`（对 ad-hoc 同样有效）：
TOP SQL 用 `OUTER APPLY`（个别计划无该属性时不能把整行丢掉），32767=ResourceDB 单独标名；
「CPU按数据库分布」用 `CROSS APPLY` + `WHERE pa.dbid<>32767`（ResourceDB 不参与按库分布）。
口径已写入脚本头部「语句归属数据库的口径（v1.3.2 起）」段。
**注意**：这是采集 SQL 变更，**既有基线包（v1.3.1 及更早采的）不受影响** ——
包里的 `cpu_by_database` 仍是 `[]`、TOP SQL 的 `database_name` 仍是 `null`，
只有用 v1.3.2 重新采集才会变。

**⑯ 实例查询里三个列名根本不存在 → 整脚本崩溃、零产出**【P0，**v1.3.3 已修**】
这是 **v1.3.2 真机首跑 100% 复现的崩溃**，现场控制台只有两行：

```
[2026-09-10 03:45:33][INFO] Collector 1.3.2 started for .
... : 在此对象上找不到属性"connection_ip"。请确认该属性存在。
FullyQualifiedErrorId : PropertyNotFoundStrict,sqlserver_inspection.ps1
```

错误信息**不含变量名**，根因要三步才能连起来：

1. 阶段 ③ 的 SQL 写了 `w.windows_version` / `w.language` / `w.os_language` 三个列 ——
   **`sys.dm_os_windows_info` 一个都没有**。官方列自 2008 R2 SP1 起至今（含 SQL 2022+）恒为四列：
   `windows_release` / `windows_service_pack_level` / `windows_sku` / `os_language_version`。
   → 整条 SQL 报 `Invalid column name`。
2. 该 SQL 外面是 `try/catch`，异常被吞；**而那个 catch 当时连日志都不写**（只写 `limitations` /
   `module_status`，两者都要等包产出才看得见）→ 控制台完全无痕。
3. 于是 `$snapshot.instance` 落成空 `[ordered]@{}`；紧接着的
   `$snapshot.instance.connection_ip` 在 `Set-StrictMode -Version Latest` 下对不存在的键点取，
   抛 `PropertyNotFoundException` → 脚本终止、**一个包都产不出来**。

**放大器效应才是这条缺陷的真正教训**：一个模块失败之所以能升级成"整脚本崩溃"，
是因为"静默吞异常" + "裸点取属性"两件事同时存在 —— 前者藏住原因，后者把后果放大到全局。

修法（v1.3.3）：
- R1 列名改回官方三列（`CAST(w.windows_release AS nvarchar(256)) AS os_version` 等，`L366`），并在 SQL 上方写 6 行注释
  「列名以官方文档为准 / 这三个列名都不存在 / 改这里先核文档」；
- R2 该 catch 补 `Write-CollectorLog "实例基础信息 failed: ..." 'ERROR'`（`L387`，**失败必须留痕**）；
- R3/R4 + 下游全部改走 `Get-ObjProp` / `Set-ObjProp`（`L393-408`，见 §10-10）。
- 副作用：`os_language` / `os_locale_language` **全仓零消费**，删掉不影响任何下游（`presentation.py` 只读 `os_version`）。
- **`schema_version` 不 bump**：§3 的键类型表不枚举 `instance` 的子键，删掉的是从未进入契约的列。

**⑰ OS 章节：单元素数组被拆包 + StrictMode 下 `.Count` 抛异常**【P1，**v1.3.3 已修，由本批的等价冒烟抓出**】
`Get-ObjProp` **不能用来取数组型字段**，两个行为相乘：

| 行为 | 实测（PS 5.1） |
|---|---|
| `return $value` 走管道，**单元素数组被拆包成标量** | 传 1 元素数组进去，出来是 `PSCustomObject`（`isArray=False`） |
| StrictMode 下对 PSCustomObject 取 `.Count` 抛异常 | `[pscustomobject]@{name='C:'}.Count` → `PropertyNotFoundStrict` |

后果是**机器上恰好只有 1 块盘 / 1 张网卡就崩，2 块盘反而正常** —— 典型的"只在特定环境复现"。
（这条是 R3 把 `$last.disks` 改成 `Get-ObjProp $last 'disks' @()` 时**自己引入**的：
裸点取 `$last.disks` 走属性访问、不会拆包，换成语义更严的 `Get-ObjProp` 反而丢了数组性。）

修法：新增第三个访问器 **`Get-ObjArray`（恒返回数组，缺键 / `$null` 都返回空数组）**，
`$lastDisks` / `$lastNet` / `$osDisks` 三处一律走它；并在 `tests/test_sqlserver_collector_strict_safety.py`
补两条静态守门（数组型字段必须走 `Get-ObjArray`；不许对 `Get-ObjProp` 的结果直接取 `.Count`）。

**⑱ 另外三个 SQL 缺陷：两处列名不存在 + 一处表变量合并声明**【P0×3，**v1.3.4 已修，真机首跑暴露**】

v1.3.3 修完 StrictMode 之后脚本能跑完了，但真机首跑（2026-09-10）又暴露**另外三个模块 failed**：

| 模块 | 报错 | 根因（官方文档） |
|---|---|---|
| 会话资源占用 | `列名 'physical_reads' 无效` | `sys.dm_exec_sessions` **没有** `physical_reads`。该 DMV 的 `reads` 列**本身就是物理读**（官方描述：*Number of physical reads performed, by requests in this session*）—— 原 SQL 同时写了 `SUM(es.reads) total_reads` 和 `SUM(es.physical_reads) total_physical_reads`，同一个量写了两遍、其中一遍名字是错的 |
| SQL Server 服务与启动参数 | `列名 'servicetype' 无效` | `sys.dm_server_services` 没有 `servicetype`；同一条 SELECT 里**还跟着一个同样不存在的 `version`**（SQL Server 只报第一个无效列名，所以修掉第一个会立刻冒出第二个） |
| 复制拓扑 | `关键字 'table' 附近有语法错误` | `DECLARE @dist sysname, @blocks table(...)` —— T-SQL **不允许把普通变量与表变量合并声明**，表变量必须单独一条 `DECLARE`。该模块里 `table` 关键字只有三处（`DROP TABLE #repl` / `CREATE TABLE #repl` 合法），报错只能来自这一行 |

**为什么静态审计没抓到**：前两条是"列名凭记忆写"，与 §9-⑯ 同源 —— 那次审计覆盖的是 DMV 的**对象名**，没有覆盖**列名**；
第三条是**语法级**错误，任何"列名对照"式审计都看不见，只有真跑或 SQL 解析才能发现。

修法（v1.3.4）：
- **会话资源占用**（`L704-718`）：删掉 `SUM(es.physical_reads) total_physical_reads` 与重复的 `SUM(es.reads) total_reads`，
  合并为 `SUM(es.reads) total_physical_reads` —— **列名不变**（`presentation.py:638` 与 `rules.py:369` 都在读它），只把来源换成真实存在的列；
- **服务与启动参数**（`L846-850`）：删掉 `servicetype` 与 `version` 两列。**下游必须同步**：
  `presentation.py` 的服务表也从 7 列收为 6 列（去掉"类型"表头），否则报告里会多出一个永远为空的列；
- **复制拓扑**（`L1157-1158`）：拆成 `DECLARE @dist sysname;` + `DECLARE @blocks table(...);` 两条，并补 3 行注释说明为什么不能合并。
- **`schema_version` 不 bump**：删掉的三列从未进入 §3 键类型表，`session_activity` 的列名集合也没变。

> 附带发现（**未修，留待决策**）：`os_metrics.samples[0]` 的 `Win32_PerfFormattedData_*` 值不可靠 ——
> 真机包里首点是 `cpu_pct=0` 而 `cpu_idle_pct=98`（本应为 2%），而 `presentation.py:581-586`
> 把 6 个采样点**全部**渲染进报告。快速巡检若只看第一行会误判"CPU 空闲"。

### 建议的推进顺序

1. ~~**修 ⑧**（`_coll()` 找不到名字时静态回退成 `ok`，应改成"未采集"语义）~~ —— **v1.3.1 已修**；
2. ~~**补 ⑪**（把两个 `top_queries_*` 接成报告条目，或从采集端移除）~~ —— **v1.3.1 已修**；
3. **修 ③ / ⑤ 的残余不可判定风险**（`xp_readerrorlog` 无权限时"真没错误"与"读不到"不可区分；
   备份盘缺 `total_mb` 时无法做"剩余空间 < 最近完整备份"这类判断）；
4. **补注释**（文件头 → 阶段分隔 → 模块函数头，目标 ≥20%）。

**已闭环**：**① 已撤回**（复核后确认判断有误）；**⑩ 已修**（默认输出目录与脚本位置无关）；
② 保留为"历史包不可回溯补列"的成形差异，不再列为待修。

---

## 10. 变更规则

1. **动输出就 bump `schema_version`。** 目录结构 / 文件增删改名 / 键增删 / 字段语义 / status 取值 / 退出码任一变化 → 必须 bump。只新增独立文件 = 次版本；删键、改名、改语义 = 主版本。**当前关系未定义，按此约定执行：`schema_version` 只随契约变化，`collector_version` 随代码变化。**
2. **加列是安全的，改列名 / 删列是破坏性的。** `presentation.py` 全部按列名取值（`row.get("role_desc")` 形态），所以往 SELECT 里**追加列零成本**；一旦改名或删除，对应章节直接变空格。破坏性变更单独成批，不要夹在修复批里。
3. **`SELECT TOP (0) CAST(NULL AS ...)` 占位列已全部清除。** 改用 `WHERE` 按环境（HADR 开关 / 版本）直接返回空结果集，列形状只维护一处 —— 旧写法在未启用/不支持时才暴露问题，极易漏。
   注意区分两种写死：**`SELECT TOP (0) CAST(NULL AS ...)`（整句占位）已清除**；而主查询里为旧版本保留的**单列占位**（如 `CAST(NULL AS bit) is_query_store_on`，随后被版本门控的二次查询覆盖）属于另一种模式，判它是否有害要先看有没有覆盖路径 —— 见 §9-①。
4. **新增采集项五件事一起做**：写 SQL → 决定"未启用/不支持"时返回什么（不能是写死 NULL）→ 更新本文档 §4 清单 → 更新 §3 键类型表 → **在 `presentation.py` 里真的读它**（AGENTS.md：采了却没人读的字段要么补分析、要么从采集端移除）。
5. **二进制列必须在 SQL 侧转换。** `query_hash` / `query_plan_hash` 在 `sys.dm_exec_query_stats` 里是 `binary(8)`，ADO.NET 读出来是 `byte[]`，`ConvertTo-Json` 会写成 `[0,0,...]` 数字数组 —— 已统一转成 `CONVERT(varchar(18), col, 1)` 的十六进制串。
6. **注释与代码同改。** 本项目注释密度低，那 79 行注释会被当成"已核实记录"来读 —— **撒谎的注释比没注释更坏**。
7. **隐私边界**：`snapshot.json` 默认包含 6 个模块的 `sql_text`（`blocking` / `active_requests` / 4 个 top_queries），**v1.3.2 起**再含 `job_steps.command_preview`（作业步骤命令），全部**可能含明文口令、连接串、业务 SQL**。`-ExcludeSqlText` 是唯一开关（7 个字段一起抹）；原始包与基线输出**未经确认不得上传或外发**（AGENTS.md 硬规则）。
8. **bit 列的 JSON 序列化禁止 `-eq [DBNull]::Value`，必须用 `-is [System.DBNull]`。**
   PowerShell 的 `-eq` 会按**左操作数**类型强制转换右操作数，`$true -eq [DBNull]::Value` 结果是 `$true`——
   后果是**所有「值为真」的 bit 列被静默序列化成 `null`**，脚本不报错、包照常产出，只有下游判据集体失效。
   正确写法见 `Convert-DbValue`（`L162-178`）；`tests/test_sqlserver_module_names.py` 静态守门。
   连带语义：**bit 列在系统视图里是 `bit NOT NULL`，所以 `null` 只可能来自序列化缺陷，不可能来自数据库。**
   历史包（`collector_version < 1.3.0`）里的 `null` = 「值未知（可能为真）」，与 `false` 不等价。
9. **编辑本脚本必须保持 UTF-8 带 BOM + 纯 LF。** PowerShell 5.1 会把不带 BOM 的 UTF-8 当 ANSI 读，中文注释会吞掉下一行；CRLF 在 RHEL 侧的 shell 场景会因 `\r` 报错（脚本本身只在 Windows 执行，但仓库统一 LF）。
10. **`Set-StrictMode -Version Latest` 下取属性只有三种合法写法**（脚本 L134 开了严格模式，实测 PS 5.1）：

    | 场景 | 必须用 | 反例（会抛 `PropertyNotFoundStrict`） |
    |---|---|---|
    | 标量字段 / 可能缺键 | `Get-ObjProp $obj 'key' [默认值]` | `$obj.key`、`$snapshot.a.b` |
    | 写回可能缺键的键 | `Set-ObjProp $obj 'key' $v` | `$obj['k'] = v`（PSCustomObject 不支持） |
    | **数组型字段**（要取 `.Count` 或按数组遍历） | `Get-ObjArray $obj 'key'` | `Get-ObjProp`（单元素会被拆包成标量）、`$obj.key.Count` |

    实测边界（PS 5.1 + StrictMode Latest，`_work/nullcount_probe.txt`）：`.Count` **只在数组（含空数组）和哈希表上安全**；
    对 `$null` / 字符串 / 数值 / **任何构造方式的 PSCustomObject**（`[ordered]` 转、普通哈希转、`New-Object psobject`、
    `ConvertFrom-Json`）**一律抛 `PropertyNotFoundStrict`**。注意反直觉的两点：`$null.Count` 也抛（不是 0）；
    而 `return $value` 走管道会**把单元素数组拆包成标量**（1 元素 → `PSCustomObject`）。
    两者相乘 = "只有 1 块盘 / 1 张网卡就崩"（§9-⑰）。
    三个访问器定义在 `Convert-Table` 之后（`Get-ObjProp` L221 / `Set-ObjProp` L239 / `Get-ObjArray` L248），
    必须**先定义后调用**（PowerShell 按执行顺序解析）。静态守门：`tests/test_sqlserver_collector_strict_safety.py`。
11. **`catch` 必须留痕：`Write-CollectorLog ... 'ERROR'` 或 `'WARN'`，不许只写 `limitations` / `module_status`。**
    后两者要等包产出才看得见，而脚本完全可能在下游崩掉、包产不出来 —— 那样"哪条 SQL 错了"就永远查不出来（§9-⑯）。
12. **DMV 的列名与语法要静态守门，不能靠"记得核对"。** 采集端 47 个模块里每一个 SELECT 都是一次真机才暴露的机会：
    列名写错 → 该模块 failed；**T-SQL 语法写错 → 同样 failed，而且 SQL Server 只报第一个错**（修一个冒一个，§9-⑱ 就是实例）。
    v1.3.4 起 `tests/test_sqlserver_collector_strict_safety.py` 加两条守门：
    - **`DMV_FORBIDDEN_COLUMNS`**：已知 DMV 的"陷阱列名"黑名单（`dm_exec_sessions.physical_reads`、
      `dm_server_services.servicetype` / `.version`、`dm_os_windows_info.windows_version`）。
      检测**别名感知** —— 只在 `FROM/JOIN <该 DMV>` 所在的 here-string 块里查，且只认 `<该 DMV 的别名>.<列>`
      与裸列名，所以不会误伤别的表上的同名后缀列（如 `qs.total_physical_reads` 来自 `dm_exec_query_stats`）。
    - **`DECLARE` 混合声明检测**：`table(` **之前**出现逗号即违规（表变量定义体内部的逗号不算）。
    - 两条检测都先剥掉 SQL 的 `--` 行注释 —— 脚本里刻意用注释记录了"曾经写错什么"，那是说明、不是违规。
    这是"静默吞异常会把模块级失败放大成整脚本崩溃"的直接对策。
13. **给用户看的文案不许依赖运行环境。** `-Help` 块的 here-string 必须用**单引号**（`@'...'@`，L93 起）；
    结尾 stdout 提示里的示例路径同样要**转义**成字面量 `` `$env:USERPROFILE ``。
    本批自己踩过：写成双引号 here-string 时，`$env:USERPROFILE` 会在**运行脚本那台机器上**被展开，
    帮助里于是打出 `C:\Users\wangjs\Desktop`（开发机的用户名）—— 用户看到会以为串了环境。
    文案里的路径一律写成 `$env:USERPROFILE\...`，让用户自己复制过去展开（在 PowerShell 里会正常求值）。
