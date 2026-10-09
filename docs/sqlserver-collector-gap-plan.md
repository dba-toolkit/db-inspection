# sqlserver_inspection.ps1 ↔ 现场巡检报告的覆盖度缺口清单

- 审查对象：`inspection/sqlserver_inspection.ps1`。**当前版本 v1.3.0 / 1676 行 / 15 个函数 / 79 行注释 = 4.7%**
  （演进：v1.1.0 → 1040 行 / 12 函数 / 19 行注释 1.8%；v1.2.0 → 1632 行 / 15 函数 / 63 行 3.9%。）
- **行号基准**：§1–§8 的 `L###` 是按 **v1.1.0** 数的，§9 按 **v1.2.0**，§10–§11 与 `sqlserver-collector-interface.md` §2 按 **v1.3.0**。
  改脚本后行号会漂 —— **按行号找不到时以符号名（函数名 / 模块名 / 键名）为准**。
- 对照物：现场人工巡检报告（Windows Server 2019 + SQL Server 2019 RTM 15.0.2000.5 + AlwaysOn 双副本；本文按 `db01` / `db02` / `192.168.1.37` 代称，原始包路径见 `docs/baseline-inventory.md`）
- 下游链路：`analyze.py` → `plugins/sqlserver/{cli,analyzer,presentation,report_adapter}.py` → `generate_report.py`（12 章 Word）
- 本文只回答一件事：**报告里写的内容，这个采集脚本撑不撑得住**。不做的判定、不排业务优先级。
- **§8「落地状态」是本清单的执行结果**（F-01…F-19 + E-1 逐条对照）；§9 是新发现（**F-20 已撤回、F-21 已修**）；
  §10 = 第二份对照物（SQL Server 2014 SP2 + Windows Server 2012 R2 报告，G-01…G-19）；§11 = v1.2.0 → v1.3.0 落地状态。

---

## 0. 总览

| 分类 | 条数 | 后果 |
|:---:|:---:|---|
| A 硬缺口（报告有、脚本采不到） | 10 | 对应章节空白、或永远渲染"未采集" |
| B 口径 / 开关不一致（能采但对不上） | 9 | 报告与脚本各说各话，交叉核对时互相打架 |
| C 报告自身的矛盾（脚本正好能证伪） | 4 | 人工填表的问题，不可写入报告交付 |

编号 `F-01` … `F-19`，级别 P0 / P1 / P2。**证据分三档**（沿用本项目既有体例）：

| 档 | 内容 | 可信度 |
|:---:|---|---|
| A | 代码事实（行号 / SQL 原文 / 调用关系）+ 实测输出 | 可直接采信 |
| B | 官方文档原文 | 可直接采信 |
| C | 改法本身、未在本环境实测的路径 | 需现场验证 |

---

## 1. A类 · 硬缺口

### F-01 `os_version` 是写死的 NULL，OS 信息整体不采【P0】

位置 `L211-217`（`$instanceSql`），证据：

```sql
CAST(NULL AS nvarchar(4000)) AS os_version,
```

后果有两层：

1. 报告 2.1「操作系统版本 Windows Server 2019 Standard」无来源；
2. **下游已经在读这个字段** —— `plugins/sqlserver/presentation.py:118` 写作
   `"os_version": instance.get("os_version")`。也就是说渲染位留好了、数据层喂的是 NULL，
   报告里这一格会长期显示空/未采集（本项目已踩过的同类缺陷）。

改法（**先看这个陷阱**）：不要直接用 PowerShell 取 OS 版本。

```powershell
(Get-CimInstance Win32_OperatingSystem).Caption   # ❌ 这是【执行机】的操作系统
```

脚本支持 `-Server 192.168.1.37` 远程连库，此时上面取到的是**运行脚本那台机器**的 OS（在 DBA 笔记本上跑就是 Windows 10）。目标机的真相在 SQL 侧：

```sql
FROM sys.dm_os_sys_info AS osi
CROSS JOIN sys.dm_os_windows_info AS w    -- 需 VIEW SERVER STATE
-- w.windows_version / windows_service_pack_level / windows_sku / language / os_language
```

注意 `w.windows_version` 形如 `10.0 <X64> (Build 17763: )`，**完整产品名（Windows Server 2019 Standard）需要 build→产品名映射**，这段映射放分析层，不要写死在采集脚本里。

验收：采集一次，`snapshot.instance.os_version` 非空；且远程执行时值等于**目标机**版本而非执行机版本。

### F-02 OS 层四大指标零采集：CPU / 内存 / 磁盘 IO / 网络【P1】

证据：全脚本 `Get-Counter` / `Get-CimInstance` / `Get-WmiObject` / `Get-NetAdapter` **0 命中**。
报告 3.1–3.5 五整节全靠 OS 数据，脚本只有 SQL 侧近似（`dm_os_process_memory`、`dm_io_virtual_file_stats` 累计值）。

改法：新增 `Collect-OsCounters` 模块，采样节奏复用现有参数：

```powershell
Get-Counter -Counter `
  '\Processor(_Total)\% Processor Time', '\System\Processor Queue Length',
  '\Memory\Available MBytes', '\Memory\Pages/sec',
  '\LogicalDisk(D:)\Avg. Disk sec/Read', '\LogicalDisk(D:)\Avg. Disk sec/Write',
  '\LogicalDisk(D:)\Disk Reads/sec', '\LogicalDisk(D:)\Disk Writes/sec',
  '\LogicalDisk(D:)\Current Disk Queue Length',
  '\Network Interface(*)\Bytes Total/sec' `
  -SampleInterval $SampleIntervalSeconds -MaxSamples $SampleCount
```

**硬前提（必须写进脚本头备注，否则会污染客户结论）**：这些计数器属于**执行机**。远程连库场景下拿到的不是目标机数据。两种处置：

- 采集脚本在**目标机上本地执行**（`-Server .`）时才采集；
- 远程场景把该模块标 `skipped` 并写明原因 —— **不是采 0 行**（AGENTS.md：未采集 ≠ 0）。

验收：本地执行产出 N 个采样点；远程执行模块状态为 `skipped` + limitation 有原因。

### F-03 磁盘介质类型（HDD/SSD）不采，「D 盘机械硬盘 I/O 瓶颈」无法复现【P2】

报告 3.4 写了「D 盘机械硬盘的 I/O 瓶颈」—— 这是**介质类型 + 延迟**的联合判断。脚本无 `MediaType` / `BusType` / `Get-PhysicalDisk` 任何采集，这条结论在脚本里不可能被独立复现。

改法：本地执行时 `Get-PhysicalDisk | Select FriendlyName,MediaType,BusType,Size,BootFromDisk`，再用 `Get-Partition`/`Get-Disk` 把卷映射到物理盘，与 `volumes` 关联。远程场景同样标 `skipped`。

### F-04 `volumes` 只覆盖数据库文件所在卷，**备份盘不在其中**【P1】

位置 `L366-372`：

```sql
FROM sys.master_files mf CROSS APPLY sys.dm_os_volume_stats(mf.database_id, mf.file_id) vs
```

`dm_os_volume_stats` 按（database_id, file_id）取卷 → 结果集 = **有数据/日志文件的那些卷**。
报告里备份目录是 `E:\DBbackup`，该盘上没有数据库文件 → **它压根不在 volumes 里**。备份盘写满是现场最高频的 P1 事故，而报告 4.7「备份记录」又是一片空白，两个盲区正好叠加。

改法（按可靠性排序，建议 1+3 组合）：

1. `SELECT * FROM sys.dm_os_volume_stats(NULL, NULL)` —— 是否返回实例全部卷**需在 2019 上实测**（C 档）；
2. 兜底 `EXEC master.sys.xp_fixeddrives`（所有固定盘 + 剩余 MB，权限门槛低，但无总量/百分比）；
3. 从 `msdb.dbo.backupmediafamily.physical_device_name` 取最近 N 次备份路径 → 解析盘符 → 与上面合并，顺带得到「备份落在哪个盘」。

验收：目标实例上 volumes 能列出 `E:\`；且新增一项「备份目录剩余空间 < 最近一次完整备份大小」的证据字段。

### F-05 内存组件（`dm_os_memory_clerks`）完全没采【P2】

报告 4.5 那张 20 行组件表（`MEMORYCLERK_SQLBUFFERPOOL` / `CACHESTORE_SQLCP` / …）在脚本里没有对应采集；`buffer_pool` 是按库的、`memory_status` 是进程级单行，都替代不了。

改法：

```sql
SELECT type,
 CAST(SUM(pages_kb)/1024.0 AS decimal(18,2)) AS pages_mb,
 CAST(SUM(virtual_memory_reserved_kb)/1024.0 AS decimal(18,2)) AS reserved_mb,
 CAST(SUM(virtual_memory_committed_kb)/1024.0 AS decimal(18,2)) AS committed_mb
FROM sys.dm_os_memory_clerks
GROUP BY type HAVING SUM(pages_kb) > 0 ORDER BY pages_mb DESC;
```

报告里 `AWE内存(MB)` 一列全 0 —— **64 位实例上 AWE 是废弃概念**，新脚本不要照抄这列。

### F-06 AG 少三个关键维度：`last_redone_time` / `availability_mode` / 连接错误【P1】

位置 `L676-690`，现有列：`ag.name, ar.replica_server_name, DB_NAME(drs.database_id), ars.role_desc, ars.connected_state_desc, drs.synchronization_state_desc, drs.synchronization_health_desc, log_send_queue_mb, redo_queue_mb, drs.last_commit_time`。

缺的列及后果：

| 缺列 | 后果 |
|---|---|
| `drs.last_redone_time` | 报告 4.1 有「最后重做时间」这一列，脚本给不出 |
| **`ar.availability_mode_desc`** | **没有它无法判断 `SYNCHRONIZING` 是否正常**（异步提交下是常态，同步提交下是真延迟）—— 见 C-2 |
| `ar.failover_mode_desc` / `ar.seeding_mode_desc` | 自动/手动故障转移、自动种子设定无法评价 |
| `ars.operational_state_desc` / `ars.recovery_health_desc` | 副本是否真的在服务 |
| `ars.last_connect_error_number` / `last_connect_error_description` | AG 断连排查的第一手证据 |
| `drs.last_received_time` / `last_hardened_time` | 端到端延迟三段（收到→落盘→重做） |

改法：这些列**加进现有 SELECT 即可，不影响下游** —— `presentation.py` 是按列名取值的（`a.get("role_desc")` 这种），加列零成本；延迟口径见 C-2。

### F-07 慢 SQL 少 `max/min_elapsed_time`【P1】

报告 5.1 的表有「最大时间」「最小时间」两列；脚本 `top_queries`（`L447-455`）只有 `total_worker_time` / `total_elapsed_time` / 平均。

改法：四个 `top_queries_*` 的 SELECT 各补

```sql
qs.max_elapsed_time, qs.min_elapsed_time,      -- 单位：微秒
qs.query_hash, qs.query_plan_hash,             -- 同一语句多计划归并
qs.total_spills
```

### F-08 软件目录 / 数据目录 / 服务与启动参数【P2】

报告 2.2 要「软件目录 `…\MSSQL15.MSSQLSERVER\MSSQL\Binn\`」「数据目录」。现状只能从 `database_files.physical_name` 反推数据目录（多目录时会有多个值），**软件目录（Binn，即 `sqlservr.exe` 所在路径）无从得知**。

改法：

```sql
SELECT CAST(SERVERPROPERTY('InstanceDefaultDataPath')   AS nvarchar(512)) instance_default_data_path,
       CAST(SERVERPROPERTY('InstanceDefaultLogPath')    AS nvarchar(512)) instance_default_log_path,
       CAST(SERVERPROPERTY('InstanceDefaultBackupPath') AS nvarchar(512)) instance_default_backup_path;

SELECT servicename, service_account, startup_type_desc, status_desc, process_id,
       last_startup_time, is_clustered, cluster_nodename, filename, version
FROM sys.dm_server_services;   -- 需 VIEW SERVER STATE；filename 即 sqlservr.exe 全路径
```

顺带解决一个常规检查项：**服务 `startup_type_desc` 不是 AUTOMATIC**（重启后数据库起不来）。

### F-09 `replication` 模块引用错库 → 该项**永远返回 0 行**【P0】

位置 `L709-722`：

```sql
IF OBJECT_ID('msdb.dbo.MSpublications') IS NOT NULL
BEGIN SELECT ... FROM msdb.dbo.MSpublications ... SELECT ... FROM msdb.dbo.MSsubscriptions ... END
ELSE SELECT TOP (0) ...
```

**`MSpublications` / `MSarticles` / `MSsubscriptions` 建在分发库（默认 `distribution`）里，不在 `msdb`。** 所以 `OBJECT_ID('msdb.dbo.MSpublications')` 恒为 `NULL` → 永远走 `ELSE` → **任何配置了复制的实例上，这一项都返回 0 行，而且状态是 `success`（"成功但无数据"）**。这正好是报告 4.6 那串 14151 复制错误最需要的解释字段。

改法（按角色探测，伪代码）：

```
@dist   = sys.databases 中 is_distributor=1 或 name='distribution'
@pubdbs = sys.databases 中 is_published=1

if @dist:  查 <dist>.dbo.MSpublications / MSarticles / MSsubscriptions
           查 <dist>.dbo.MSlogreader_agents / MSdistribution_agents / MSrepl_errors  ← 14151 的直接证据
if @pubdbs: 逐库查 sys.publications / sys.articles / sys.subscriptions   （发布库内视图，比系统表稳）
订阅侧:     逐库查 MSreplication_subscriptions / MSsubscription_articles
```

验收：在报告那台实例上跑出非空行；`MSrepl_errors` 里能看到 14151 记录（对应报告 4.6 的现象）。

### F-10 错误日志模块在中文实例上**静默返回 0 行**【P0】

位置 `L771-782`：

```sql
WHERE [Text] LIKE N'%Error%' AND LogDate>=DATEADD(DAY,-7,GETDATE())
TRY_CONVERT(int,SUBSTRING([Text],NULLIF(CHARINDEX('Severity:',[Text]),0)+9,3)) severity
```

三个独立缺陷：

1. **只认英文。** 报告 4.6 的日志原文是中文实例输出 —— `错误: 14151，严重性: 18，状态: 1。`。我把你贴的整段日志逐行核过：**没有任何一行含 `Error` 或 `Severity:`**。所以 `LIKE '%Error%'` 返回 0 行，那条每 60 秒触发一次的 14151 **一条都抓不到**；严重性解析同理恒 `NULL`。
2. **只读当前日志（`xp_readerrorlog 0,1` = Log #0）。** 7 天窗口内的内容可能已轮转到 #1 / #2，脚本没有循环。
3. **聚合键是（日期, severity）** —— 把「备份成功 INFO」和「14151 真错误」混在同一张表里数。报告 4.6 现在就是这个样子：备份日志和真错误穿插排列，导致「需开发检查」的结论指向不清。

改法：

```sql
-- ① 语言无关的筛选 + 剔除噪音
WHERE ([Text] LIKE N'%Error%' OR [Text] LIKE N'%错误%' OR [Text] LIKE N'%failed%')
  AND [Text] NOT LIKE N'%This is an informational message only%'

-- ② 严重性：中英两种写法都试
COALESCE(
  TRY_CONVERT(int, LEFT(LTRIM(SUBSTRING([Text], NULLIF(CHARINDEX(   'Severity:', [Text]),0)+9, 4)),2)),
  TRY_CONVERT(int, LEFT(LTRIM(SUBSTRING([Text], NULLIF(CHARINDEX(N'严重性',   [Text]),0)+4, 4)),2))
) AS severity

-- ③ 错误号独立成列并作为聚合键（让"同类错误每分钟一次"单独成行）
TRY_CONVERT(int, LEFT(LTRIM(SUBSTRING([Text], NULLIF(PATINDEX('%[0-9][0-9][0-9][0-9][0-9]%',[Text]),0),5)),5)) AS error_number
GROUP BY event_date, severity, error_number
```

④ 日志轮转：`for ($i=0; $i -lt 10; $i++) { … xp_readerrorlog $i,1 …; if (该文件最早一行 LogDate < 窗口起点) break }`。

验收：在报告那台实例上跑出 `error_number=14151 / severity=18 / 每天约 1440 次` 这样的独立行。

---

## 2. B类 · 能采但口径 / 开关对不上

| 编号 | 项 | 报告口径 | 脚本实际 | 级别 |
|:---:|---|---|---|:---:|
| F-11 | 大表 TOP 的排序键与截断键 | 按 **TotalSpaceMB** 降序的空间 TOP10 | `L602`：`SELECT TOP (30) * FROM #lg ORDER BY row_count DESC` —— **排序键和 TOP 截断键都是行数**。① 报告里那张 1927 MB 的表只有 12.5 万行，按行数排序会掉到第 7 位左右；② 更严重的是**空间大但行数少的表会被 TOP30 直接挤掉**（逐库已按空间取 TOP50，全局又按行数截断），也就是"空间最大的表可能整表不出现在报告里" | P1 |
| F-12 | 模块名与实际开关不符 | — | `large_tables`（`L591`）与 `tables_without_pk`（`L576`）的 `module_status.name` 都带「（深度）」后缀，但**它们默认就采** —— `if ($IncludeDeepChecks)` 块从 `L724` 才开始，真正受开关控制的只有 `stale_statistics` / `fragmented_indexes` / `orphaned_users`。名字会让人（和下游）误判成"默认没有这一项" | P2 |
| F-13 | 慢 SQL 时间窗口 | `last_execution_time > '2026-02-09'` | 无任何时间过滤（`last_execution_time` 只作为输出列）→ 久远历史被算进总量 | P2 |
| F-14 | `database_summary.user_db_count` | 用户库**个数** | `COUNT(*)` 于 `sys.master_files`（`L341-347`）→ 数出来是**文件数**：tempdb 8 个数据文件、任何多数据文件库都被重复计数 | P1 |
| F-15 | 备份记录 | 近 7 天清单 + 可恢复性 | 有 `backup_history`，但无备份**耗时**（未取 `backup_start_date`）、无备份**文件存在性**（无 `backupmediafamily`）、无 `RESTORE VERIFYONLY` | P2 |
| F-16 | 「日志组数目」 | 报告 2.2 写"21"，口径未定义 | `vlf_summary` 是**每库 VLF 数**（且仅 2017+）、`log_space` 是每库使用率 —— 没有"实例级日志文件数"这个口径，**需先统一口径**再谈采不采 | P2 |
| F-17 | 大表行数 / 空间 | 报告 4.4 的表 | `sys.partitions JOIN allocation_units GROUP BY o.name,p.rows`：非聚集索引的每个分区都带同一份 `p.rows`，`SUM` 后**行数被按索引数重复累加**（报告那份 SQL 有同样问题，两边都错）。正确写法：行数只取堆/聚集索引（`i.index_id IN (0,1)`），空间才 `SUM` 全部索引 | P2 |
| F-18 | 注释密度 | — | 1040 行 / 12 函数 / **19 行注释 = 1.8%**，且 19 行里几乎全是踩坑记录，**没有一个函数写了"用途 / 产出 / 谁消费"**。本项目目标 ≥20%、≤30% | P1 |
| F-19 | 退出码契约 | — | 全脚本只有 `-Help` 分支有 `exit 0`；主流程无退出码，失败模块只进 `limitations`。流水线无法区分「采集成功但有 3 个模块失败」与「全成功」 | P2 |

F-17 的修法示例（行数口径）：

```sql
-- 行数：只算堆或聚集索引；空间：全部索引
SUM(CASE WHEN i.index_id IN (0,1) THEN p.rows ELSE 0 END) AS row_count,
CAST(SUM(a.total_pages)*8.0/1024 AS decimal(18,2)) AS total_mb
```

---

## 3. C类 · 报告自身的矛盾（脚本正好能证伪）

| 编号 | 位置 | 问题 | 脚本侧对应能力 |
|:---:|---|---|---|
| C-1 | 2.2 vs 4.3 | 2.2 写「数据量 2408 G / 日志 255 G」，4.3 结论写「数据文件合计约 2.9 TB」。按 4.3 明细逐行加总：用户库 ≈ 2593 GB，加 tempdb（8 个文件）≈ 517 GB，合计 ≈ 3110 GB —— **4.3 的量级对，2.2 的 2408 G 对不上**。同一份报告两处口径不一致 | `databases`（逐库 data/log MB）+ `database_summary`（合计）能给出权威值；`presentation.py` 的 `total_data_mb/total_log_mb` 就是按 `databases` 求和的 |
| C-2 | 4.1 | ①「同步延迟(秒)」`DATEDIFF(SECOND, last_redone_time, last_commit_time)`：主副本那两列都是 NULL（所以你表里主副本行恒 NULL）；辅助副本行上这个差值恒 0，**不代表业务延迟**。②十行全是 `SYNCHRONIZING` 却写"正常" —— **没有 `availability_mode_desc` 就无法判断**：同步提交下 `SYNCHRONIZING` 是真延迟，异步提交下才是常态（见 F-06） | 补 `availability_mode_desc` + 主副本 `last_commit_time` 之差才是可判定的延迟口径 |
| C-3 | 4.6 | 「可能是发布导致，需开发检查是否改发布已不在使用」—— 但全文没有任何发布/订阅清单，4.1 采的是 AG 不是复制。**这条结论目前没有证据链** | 正是 F-09 修好后该输出的东西（`MSdistribution_agents` / `MSrepl_errors`） |
| C-4 | 3.1–3.5 / 4.7 | 「如下所示：」后面全是空的（无图、无表），4.7 备份记录整节空白 —— 人工漏填 | `volumes` / `backups` / `backup_history` 覆盖 |

C-2 的延迟口径建议（三选一，需实测确认辅助副本能否读到主副本行）：

```sql
-- 该库最长提交时间（主副本行的值）
MAX(drs.last_commit_time) OVER (PARTITION BY drs.group_id, drs.database_id)
-- 本副本落后主副本多少秒
DATEDIFF(SECOND, drs.last_commit_time,
         MAX(drs.last_commit_time) OVER (PARTITION BY drs.group_id, drs.database_id)) AS replica_lag_seconds
```

---

## 4. D类 · 脚本强于报告之处（不要为了"对齐"改回去）

- 索引：`missing_indexes`（含 improvement_score / 键列 / 包含列）+ `unused_indexes`，报告只有 1 条手写 `CREATE UNIQUE NONCLUSTERED INDEX` 建议
- 安全：`sa` 禁用 / 空密码登录 / `xp_cmdshell` / Ole Automation / linked server / sysadmin 成员 / 认证模式 —— **报告完全没有这一章**
- 可靠性：`suspect_pages`、每库最后 DBCC 成功时间、`page_verify_option_desc`、备份校验和与加密类型、`log_reuse_wait_desc`
- 存储：VLF 数、日志空间使用率、文件增长方式（% vs MB）、卷可用比例
- 对象：无主键表、索引碎片、陈旧统计、孤立用户（深度）
- 性能：等待统计 TOP30、阻塞链与活动请求、性能采样差分（Batch/TPS/PLE/Memory Grants/连接数）
- 运维：Agent 作业清单与近 24h 失败作业、日志传送、镜像

顺带一提：报告 4.6 里「错误 14151」这一条，脚本修好 F-09/F-10 后能自动聚合出频次与来源，不需要人工翻日志。

---

## 5. E类 · 顺带发现（下游，非采集层）

`plugins/sqlserver/word_report.py:23-36` 的 `section_order` 有 12 项，**没有 `"ha"`**；而 `presentation.py:507` 产出的章节 id 是 `"ha"`（高可用与复制）。
`inspection_core/word_engine.py:880-882`：

```python
order = {section_id: index for index, section_id in enumerate(self.profile.section_order)}
return sorted(sections, key=lambda section: order.get(str(section.get("section_id")), 99))
```

未登记的章节取 **99** → 不会丢，但会被排到**最后**（在「实例关键配置」之后），而不是紧接「安全与身份」。
建议把 `"ha"` 插进 `section_order` 的 `security` 与 `ops` 之间。**改完必须刷新 SQL Server 基线**（章节顺序属于输出契约）。

另：`plugins/sqlserver/inspection_rules.json` 无 `id`/`title` 命中的扁平字段（本次 grep 未命中），如需核对规则覆盖请另跑 `tools/coverage_audit.py`。

---

## 6. 批次与顺序

| 批次 | 项 | 为什么这个批次 |
|:---:|---|---|
| **批 1 数据正确性（不改完不能上生产）** | F-09、F-10、F-11、F-14、F-17、F-19 | 现在这几项会输出**写着 success 的错数据**：复制恒空、错误日志恒空、库数虚高、大表排序错、行数翻倍。报告据此写的结论直接是错的 |
| **批 2 章节撑不住（必改）** | F-01、F-04、F-06、F-07 | 报告 2.1 / 3.1 / 4.1 / 5.1 对应内容没有来源 |
| **批 3 OS 层补全 + 契约整理（需先定执行方式）** | F-02、F-03、F-05、F-08、F-12、F-13、F-15、F-16 | 依赖"本地执行还是远程采集"这个前提，建议先在脚本头写清约束，再决定采不采；F-12 只是模块命名，随手改 |
| **批 0 注释（建议先做）** | F-18 | 零逻辑风险；补注释的过程本身就是契约自检（表头/字段漂移会自己浮出来） |

批 1 的自测要求（可复跑）：在目标实例上跑一次采集，逐项断言
① `replication` 非空且与 `MSrepl_errors` 对得上；
② `error_log_summary` 能出 `error_number=14151` 的行；
③ `large_tables` 首行是空间最大的表；
④ `database_summary.user_db_count` == `sys.databases` 用户库数。

---

## 7. 未验证项（C 档，需现场实测）

| 项 | 待确认 |
|---|---|
| `sys.dm_os_volume_stats(NULL, NULL)` | 在 SQL Server 2019 上是否返回实例全部卷（否则只用 `xp_fixeddrives` + 备份路径兜底） |
| AG 延迟窗口函数 | 在**辅助副本**上查询时，`dm_hadr_database_replica_states` 是否含主副本行（`is_local=0`）并提供 `last_commit_time` |
| 中文实例严重性解析 | 全角冒号 `严重性：18` 与半角 `严重性: 18` 两种形态的命中情况 |
| `Get-Counter` 计数器名 | 中文 Windows 下的本地化情况（如异常，改用 `Win32_PerfFormattedData_PerfOS_Processor` 等已格式化类） |
| `xp_readerrorlog` 权限 | 采集账号（非 sysadmin）能否读全量日志与轮转文件 |

---

## 8. 落地状态（v1.1.0 → v1.2.0）

对照物：`inspection/sqlserver_inspection.ps1` v1.2.0（1632 行 / 15 函数 / 注释密度 3.9%）。
契约细节见 `docs/sqlserver-collector-interface.md`。

| 编号 | 级别 | 状态 | 落地方式 / 未做的原因 |
|---|:---:|:---:|---|
| F-01 | P0 | ✅ | `os_version` 等 5 个 OS 字段改从 `sys.dm_os_windows_info` 取（**不用 `Get-CimInstance`**，那是执行机）；build→产品名映射留分析层 |
| F-02 | P1 | ✅ | 新增 `Get-OsMetrics`：WMI 已格式化类（属性名英文，不受中文计数器名影响）+ N 个采样点；**仅目标为本机时采集**，远程标 `skipped` 并写原因（已在本机/远程两态实测） |
| F-03 | P2 | ✅ | `Get-PhysicalDisk` 取 `MediaType/BusType/SpindleSpeed`，`Get-Partition` 建盘符映射；报告新增「物理磁盘介质」 |
| F-04 | P1 | ✅ | `volumes` 三层合并：`dm_os_volume_stats` → `xp_fixeddrives` → `backupmediafamily.physical_device_name` 解析盘符 |
| F-05 | P2 | ✅ | 新增 `memory_clerks`（TOP40，**不含 AWE 列**）+ `system_memory` |
| F-06 | P1 | ✅ | `availability_replicas` 补 `availability_mode/failover_mode/seeding_mode/operational_state/recovery_health/last_connect_error_*/last_redone_time/secondary_lag_seconds` 等；另加 `ha_status` 单行总览，解决"0 行 = 未启用"与"0 行 = 采集写错"无法区分 |
| F-07 | P1 | ✅ | 四个 `top_queries_*` 统一补齐 `max/min_elapsed_time`、`total_spills`、`query_hash`、`query_plan_hash`（两个 hash **在 SQL 侧转十六进制**，binary(8) 直出 JSON 会变数字数组） |
| F-08 | P2 | ✅ | 新增 `server_services`（Binn 路径 + 启动类型 + 启动账号）；`instance` 补三个 `InstanceDefault*Path` |
| F-09 | P0 | ✅ | `replication` 改为按 `sys.databases.is_distributor` 探测分发库，读 `MSpublications/MSsubscriptions/MSrepl_errors/MSlogreader_history/MSdistribution_history`，压成单结果集用 `record_type` 区分；逐块 TRY/CATCH 记错误 |
| F-10 | P0 | ✅ | 中英双语筛选 + `error_number` 独立成列并入聚合键 + `xp_readerrorlog` 循环 #0..#9 直到早于窗口 |
| F-11 | P1 | ✅ | `large_tables` 内外层都按 `total_mb` 排序（原先按行数，空间大但行数少的表会被 TOP30 挤掉） |
| F-12 | P2 | ✅ | 去掉 `无主键表` / `大表统计` 名字里的「（深度）」；下游 `_coll()` 保留旧名作第二参数，历史包语义不变 |
| F-13 | P2 | ✅ | 新增 `-QueryStatsWindowDays`（默认 7）注入四个 `top_queries` 块；0 = 不限 |
| F-14 | P1 | ✅ | `user_db_count` 改 `COUNT(*)` over `sys.databases` 排除四个系统库；另加 `database_count_all` / `log_file_count` |
| F-15 | P2 | ✅ | `backup_history` 补 `backup_start_date/finish_date/duration_seconds/compressed_size_mb/has_backup_checksums/encryptor_type` + `backupmediafamily` 路径 + LSN 链 |
| F-16 | P2 | ✅ | 报告 2.2 的「日志组数目」口径定为**非系统库 + 排除 tempdb 的日志文件数**（`log_file_count`） |
| F-17 | P2 | ✅ | 行数只取 `index_id IN (0,1)`（堆/聚集索引），空间仍 `SUM` 全部索引 |
| F-18 | P1 | ⚠️ 部分 | 注释 19 行 → 63 行（1.8% → 3.9%）。**未达 ≥20% 目标**，函数级"用途/产出/谁消费"仍缺 |
| F-19 | P2 | ✅ | 阶段 ⑨：有 `failed` 模块 → `exit 2`（包照出），否则 `exit 0`；dot-source 时不 exit |
| E-1（§5） | P0 | ✅ | `section_order` 补 `ha`（security 与 ops 之间），13 项；**同时修掉守门测试的假保证** —— 原测试用"适配器输出集合 == section_order"，而基线包 `ha` 恰好为空、适配器会跳过空章节，于是"没登记"反而成了通过条件 |

### 本次新增的守门测试

| 测试 | 拦什么 |
|---|---|
| `tests/test_sqlserver_plugin.py::test_word_profile_covers_every_emittable_section` | 呈现层能产出的章节必须与 `section_order` **双向**相等（用每个章节能触发的最小快照验，不再依赖基线包） |
| `tests/test_sqlserver_module_names.py::test_every_coll_call_resolves_to_a_registered_module` | 每个 `_coll(主名)` 必须能在采集脚本里解析到，防"改名不同步 → 报告静默显示正常" |
| 同上 `::test_alias_arguments_are_known_legacy_names` | 多参数形式的别名必须是登记过的旧名，防把错名字塞进兜底参数 |
| 同上 `::test_collector_has_no_dead_media_enum_column` | `device_type`（裸数字枚举、下游无人读）不得复活 |

### 采集端新增的判据（4 条）

| rule_id | 级别 | 触发条件 | 依赖字段 |
|---|:---:|---|---|
| `INSTANCE.SERVICE_STARTUP` | critical / medium | 服务未运行（critical）或启动类型非 AUTOMATIC（medium） | `server_services.status_desc / startup_type_desc` |
| `HA.AG_SYNC_MODE` | high | **同步提交**副本 + `SYNCHRONIZING` + 非主副本 | `availability_mode_desc` |
| `HA.AG_LAG` | high | 副本延迟 ≥ `ag_replica_lag_warning_seconds`（默认 30s） | `replica_lag_seconds` / `secondary_lag_seconds` |
| `HA.AG_CONNECT_ERROR` | high | 副本存在连接错误号 | `last_connect_error_number`（按副本去重） |

新增阈值键：`ag_replica_lag_warning_seconds`（30）、`require_service_autostart`（true）。

**判据顺序是判据的一部分**：`synchronization_health_desc` 已把提交模式揉进去了（官方定义：sync 提交副本
`SYNCHRONIZING` 记 `PARTIALLY_HEALTHY`，async 提交副本 `SYNCHRONIZING` 记 `HEALTHY`）。
所以"模式分支"必须排在"健康度分支"之前，否则同步提交 + SYNCHRONIZING 这个最需要单独说清的情形
会被通用分支全部截走，`HA.AG_SYNC_MODE` 就成了永远走不到的死代码（首版正是这么写的，已重排）。

---

## 9. 本轮新发现（不在原 F 列表内；**F-20 已撤回、F-21 已修，见下**）

### F-20 【已撤回】`is_query_store_on` 并未写死 NULL

上一版把 `L328` 的 `CAST(NULL AS bit) is_query_store_on` 判成 P0，结论是
"`DB.QUERY_STORE` 判据永不触发、`require_query_store_for_user_db` 是死旋钮"。**这个判断是错的。**

`databases` 模块之后紧跟一段版本门控的补偿查询（`L342-354`）：

```powershell
if ($productMajor -ge 13) {
    $queryStoreRows = @(Invoke-InspectionQuery "SELECT name,is_query_store_on FROM sys.databases ORDER BY database_id;")
    foreach ($dbRow in $snapshot.databases) {
        $match = $queryStoreRows | Where-Object { $_.name -eq $dbRow.name } | Select-Object -First 1
        if ($null -ne $match) { $dbRow.is_query_store_on = $match.is_query_store_on }
    }
} else {
    [void]$limitations.Add('当前 SQL Server 版本不支持 Query Store，未执行该项。')
}
```

占位列的作用只是让**列形状**在低版本上也存在，真值由后面这段补写。旁证：基线包
（SQL Server 2019）里该字段是 `false`（3/3）而不是 `null` —— 真被写死的话这里应该是 `null`。

**教训（可复用）**：判"某字段永远是 NULL / 永远不触发"之前，必须搜**整份采集脚本里所有对它赋值的地方**，
不能只看 SELECT 列表那一行。上一版就是只看了一行 SELECT 就下的结论。

另一个被顺带推翻的前提：当时说"SQL Server 源采集包已不在仓库、基线无法刷新"。
实际上源包与外部实现都在本机（`D:\inspection\`），只是不在仓库里 ——
`test_sqlserver_analyzer.py` 一直在真跑，不是空跑。

### F-21 `-OutputDir` 默认取脚本自身所在目录【P2，**v1.3.0 已修**】

原写法 `L57` `[string]$OutputDir = $PSScriptRoot`，违反 AGENTS.md
「采集脚本**不得依赖自身所在目录**；输出位置一律由 `--output-dir` 或默认值决定」。
后果：脚本放在仓库 `inspection/` 下被直接执行时，采集包落进仓库。

v1.3.0 修法：默认值改为 `''`，缺省回退
`Join-Path ([System.IO.Path]::GetTempPath()) 'sqlserver_inspection'`（`L65` + `L111-113`），
建目录失败再退到当前目录并 `Write-Warning`（`L114-117`）；`-Help` 文案（`L88`）、脚本头部
`.PARAMETER`、`docs/sqlserver-collector-interface.md` §5 / §9-⑩ 已同步。


---

## 10. 第二份对照物：SQL Server 2014 SP2 + Windows Server 2012 R2

- 对照物：现场人工巡检报告（`HISDB-NODE1` / Windows Server 2012 R2 Datacenter /
  SQL Server 2014 SP2 12.0.x / 64 逻辑核 / 256 GB / AlwaysOn 已启用 / 排序规则 Chinese_PRC_BIN）。
- 报告结构：1.1 实例信息、1.2 启动参数、1.3 运行情况、1.4 文件系统剩余空间、1.5 数据库基本情况、
  1.6 备份、1.7 用户和角色、1.8 bufferpool、1.9 活跃用户进程、1.10 Job、1.11 缺失索引、1.12 表大小、3 总结。
- 编 `G-01` … `G-19`。**与第一份报告取交集**：只出现一次的当客户环境偶然，两份都出现的才算通用缺口。

### 10.1 A 类 · 真缺口（本批已落地，见 §11）

#### G-01 `Convert-DbValue` 把"值为真"的 bit 列全部写成 null【P0】

这是本批最重的一条，而且是**两份报告都不会直接暴露**的那类：它在报告里表现为
"某一格是空的"，看起来像未采集。

`L164` 一带：

```powershell
function Convert-DbValue {
    param($Value)
    if ($Value -eq [DBNull]::Value -or $null -eq $Value) { return $null }   # ❌
    ...
}
```

PowerShell 的 `-eq` 会**先把右侧转成左侧的类型**再比较。左值是 `$true`（bool）时，
`[DBNull]::Value` 被转成 `$true`，于是 `$true -eq [DBNull]::Value` 结果为 **True** → 走进空值分支。

实测（PS 5.1，真实 DataTable 端到端）：

```
true  -eq DBNull => True          Convert-DbValue($true)  -> <null>
false -eq DBNull => False         Convert-DbValue($false) -> False
```

后果不是"少一列"，是**静默的假阴性**：`sys.databases.is_auto_create_stats_on` 是 `bit NOT NULL`，
整包 3/3 全是 `null`；实测的取值分布（基线包）：

| 字段 | 行数 | null | True | False | 说明 |
|---|:---:|:---:|:---:|:---:|---|
| `is_auto_create_stats_on` | 3 | **3** | 0 | 0 | bit NOT NULL，null 只可能来自转换 |
| `is_auto_update_stats_on` | 3 | **3** | 0 | 0 | 同上 |
| `is_dynamic` | 3 | **3** | 0 | 0 | 报告 1.2 问的"哪些参数可以在线修改"直接失答 |
| `is_db_chaining_on` | 3 | **2** | 0 | 1 | 2 个库启用了 DB_CHAINING，判据看不出来 |
| `is_policy_checked` | 1 | **1** | 0 | 0 | 密码策略检查 |
| `is_percent_growth` | 6 | **1** | 0 | 0 | 按百分比增长的文件 |

直接后果（可复算，见 §11 验证记录）：把基线包里那 3 行 `is_db_chaining_on` 的 `null` 还原成 `true`，
`DB.CHAINING` 立刻多出 3 条 finding（24 → 27）——**也就是说这条判据在真实包上一直是哑的**。

改法：判类型用 `-is [System.DBNull]`，判值用 `-eq`，两者不能混。

#### G-02 `_dt()` 恒返回 None，所有"距今多久"的判据一起退化【P0】

`plugins/sqlserver/rules.py`：

```python
def _dt(value: Any) -> datetime | None:
    if not value:
        return None


def _elapsed_seconds(now, then) -> float | None:
    ...
    return (now - then).total_seconds()
    try:                                                      # ← 死代码
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
```

`_dt` 的函数体只剩一句 `if not value: return None`，真正的解析代码被 `_elapsed_seconds`
插到了它后面、落在 `return` 之后，永远不可达。于是 `_dt(任何输入) == None`
（实测 `_dt("2026-07-28T00:02:24+08:00") is None`）。

影响面是**全部时间比较**，判定方向还被带偏：

| 判据 | 修前（时间恒未知） | 修后 |
|---|---|---|
| `DB.CHECKDB`（huangjinjing） | "未取得成功 CHECKDB 时间"，**high** | "距上次成功 CHECKDB 96.2 天"，medium |
| `DB.CHECKDB`（master/model/msdb） | "未取得成功 CHECKDB 时间"，**high** | "距上次成功 CHECKDB 46229.0 天"，medium |
| `BACKUP.FULL` / `BACKUP.LOG` | 时间为空 → 走"无备份记录"文案 | 有值则给出"距今 N 小时" |

被断言的六项指标（health / findings 数 / 采集完整度 / sections / items / charts）修前修后完全一致，
但**证据文案与 severity 分布变了**（high 9→5、medium 6→10），所以基线期望值必须同步（§11）。

这条也是它抓出来的：新写的 `AGENT.JOB_OVERDUE` 第一版完全不触发，追下去才发现 `_dt` 恒 None。

#### G-03 `blocked process threshold (s)` 不在采集白名单【P1】

`configurations` 模块有一份 16 个参数的 `WHERE name IN (...)` 白名单，报告 1.2 点名要的
`blocked process threshold (s)` **不在其中** —— 采集端整行不存在，分析层连"没配"都判不出来。
已补进白名单并新增判据 `INSTANCE.BLOCKED_PROC_THRESHOLD`（阈值 `blocked_process_threshold_min_seconds`=30）。
判据守卫用 `is not None`：**采不到就不判**，不能把"没这一行"当成 0 来告警。

#### G-04 会话资源视图缺失：只有"谁连着"，没有"谁在耗"【P1】

报告 1.9 的核心是"某业务账号 1265 个会话、累计 CPU 极高、physical_io 数百万次"。
现有 `connection_stats` 按 `(IP, 主机, 程序, 登录名)` 分组**计数**，没有 status 维度、
也没有累计资源量。新增 `session_activity` 模块（按 `login_name × status` 聚合
`cpu_time / physical_reads / logical_reads / reads / writes`）与判据 `SESSION.RESOURCE_HOG`
（单账号 CPU 占比 ≥ 80% **且**总 CPU ≥ 10 分钟 **且**会话数 ≥ 10 才提示 ——
单应用实例上"一个账号占比高"是正常形态，不加地板值会天天误报）。

#### G-05 版本生命周期判据缺失【P1】

报告 1.1 写 `Microsoft SQL Server 2014 (SP2)`、`Windows Server 2012 R2`。
这两个**都已经过扩展支持**（SQL Server 2014：2024-07-09；Windows Server 2012 R2：2023-10-10），
但报告里只是一行版本号，读者无从判断它还能不能拿到安全补丁。
新增 `INSTANCE.VERSION_SUPPORT`：日期表放规则包（`sqlserver_extended_support_end`），
查不到主版本**不判**（"我不认识这个版本"≠"这个版本已过期"）；
已过期 → high，剩余 ≤ `sqlserver_eol_warning_days`(180) → medium。

#### G-06 `MAXDOP=0` 在核多的实例上漏判【P2】

报告 1.2 写「当前值 0 / 建议值 8」，并解释了单 NUMA 与多 NUMA 的取法。
旧判据是 `maxdop > max_recommended_maxdop(8)`，而 `0` 的含义是**不限制并行度**（用满所有核），
于是一台 64 核的实例一路绿灯。已补：`maxdop == 0 且 cpu_count > 上限` → medium
（核数不超过上限时保持 0 是合理的，所以加了核数前提；开关 `flag_unlimited_maxdop_on_many_cores`）。

#### G-07 启动参数族"该开的没开"没有判据【P2】

报告 1.2 列了 8 个参数，其中 3 个方向是"该开的没开"，与既有三条"值超标"判据方向相反：

| 参数 | 报告值 | 报告建议 | 新增判据 | 阈值键（默认） |
|---|---|---|---|---|
| `optimize for ad hoc workloads` | OFF | ON | `INSTANCE.ADHOC_WORKLOADS` | `require_adhoc_workloads_optimization`（true） |
| `backup compression default` | ON | 保持 ON | `INSTANCE.BACKUP_COMPRESSION` | `require_backup_compression`（**false**） |
| `blocked process threshold (s)` | 0 | 30 | `INSTANCE.BLOCKED_PROC_THRESHOLD` | `blocked_process_threshold_min_seconds`（30） |

`require_backup_compression` 默认 **false** 是刻意的：压缩吃 CPU，是否该开取决于实例的 CPU 余量，
不该由工具替客户决定；旋钮存在且可开，但不默认告警。

#### G-08 规则包 4 个死键【P2】

SQL Server 直到本批才被纳入 `tests/test_rule_pack_config.py` 的审计（`TARGETS` 里原来只有
mysql/oracle/postgresql），一纳入就查出 4 个"改了没反应"的键：

| 键 | 处置 | 理由 |
|---|---|---|
| `failed_job_lookback_hours` | 删除 | 失败作业窗口硬编码在采集端 SQL（`DATEADD(HOUR,-24,...)`），规则包管不到 |
| `job_failure_lookback_hours` | 删除 | 与上一个同名不同值，两个都没人读 |
| `file_free_warning_pct` | 删除 | 已被 `volume_free_warning_pct` 取代 |
| `job_overdue_hours` | **接线** | 能力本来就在（`agent_jobs.next_run_datetime`），新增 `AGENT.JOB_OVERDUE` 把它接活 |

#### G-09 采了没渲染的三处【P2】

| 数据 | 一直在哪 | 现在渲染成 |
|---|---|---|
| `agent_jobs.last_run_duration` | `sysjobhistory.run_duration` | 作业清单新增「最近耗时」列 |
| `socket_count` / `cores_per_socket` / `numa_node_count` | `sys.dm_os_sys_info` | 实例基础信息新增「物理CPU/每插槽核数」「NUMA 节点数」 |
| tempdb 的 `num_of_reads` / `num_of_writes` / 延迟 | `database_files`（含 tempdb 行） | TempDB 章节新增累计读写与延迟 |

**`run_duration` 是 HHMMSS 整数，不是秒**，直接渲染会夸大近一个数量级；
新增 `_hhmmss()` 转换（现场 Auto Fee = 1253 秒，`run_duration` = 2053 → 渲染 `20m53s`）。

### 10.2 B 类 · 报告自身的问题（脚本正好能证伪）

| 编号 | 位置 | 问题 | 我们的对应能力 |
|:---:|---|---|---|
| G-10 | 1.1 | **同一张表里两处排序规则不一致**：表体写 `Chinese_PRC_BIN`，紧接着的说明写 `Chinese_PRC_CI_AS`。二者不是同一种排序规则 | `instance.collation`，基线包实测 `Chinese_PRC_CI_AS` |
| G-11 | 1.1 | **CPU 口径混用**：表体「CPU数量 64」，说明里「服务器CPU个数为16」，随后又用"单/多 NUMA"来决定 MAXDOP 建议值 —— 三个口径（逻辑核 / 物理插槽 / NUMA 节点）混在一栏 | `cpu_count` + `socket_count` × `cores_per_socket` + `numa_node_count`，本批已并排渲染 |
| G-12 | 1.2 | **`min server memory` 建议值缺乏依据且有害**：建议由 0 提到 102 GB（物理内存 40%）。`min server memory` 是"不释放到此值以下的**下限**"，不是保留量；调高会让实例在内存压力下无法归还内存，与操作系统和其他服务争抢。微软文档口径是该值默认 0、按需增长，多实例/锁页场景才考虑设置 | 判据侧**不采纳**这条建议（本批明确不写这个判据） |
| G-13 | 1.2 | **自相矛盾**：`cost threshold for parallelism` 一行写"当前值 800 / 建议值 800"又说"不用调整"，建议列等于当前值 | `INSTANCE.COST_THRESHOLD` 阈值 25，800 不触发，结论一致 |
| G-14 | 1.3 / 1.4 | 两整节**只有标题没有内容**（"检查结果如下："下文空白） | `error_log_summary` / `volumes` 全覆盖 |
| G-15 | 1.10 | 一边列 `Auto Fee 1253 秒`（≈20.9 分钟）、`预约1-生成号表 738 秒`，一边结论写"作业耗时整体处于可控范围" | `agent_jobs.last_run_duration`（本批已渲染）+ `failed_jobs` |
| G-16 | 3 总结 | 七个自然段里出现 **5 次**"采用更高 IO 处理能力的承载架构 / 专用数据库一体机"。这一段是硬件销售话术，不是巡检结论，且没有任何采集数据支撑 | 本项目只输出可复算的结论；报告结论章由 `comprehensive_conclusions` 结构化生成 |

### 10.3 D 类 · 我们强于报告之处（不要为了"对齐"改回去）

- 报告 1.11 只给了 1 条缺失索引建议 + "无索引表"清单；我们另有 `unused_indexes`、
  `stale_statistics`、`fragmented_indexes`、`tables_without_pk`（深度）。
- 报告 1.8 只讲 bufferpool 占比；我们另有 `memory_clerks`（组件级）、`system_memory`、
  `MEM.SIGNALING` / `MEM.PRESSURE`。
- 报告 1.7 只介绍角色概念，未列实际成员；我们采 `sql_logins` / `sysadmin_members` /
  `linked_servers` / 5 项安全检测，并有 4 条安全判据。
- 报告 1.6 备份只写"正常"，没有备份链、耗时、校验和、加密、copy-only 标记；我们都有。

### 10.4 记录在案、本批未落地

| 编号 | 项 | 为什么先不做 |
|:---:|---|---|
| G-17 | 「无索引表」与现有「无主键表」是两个口径 | 主键 ≠ 索引：一张表可以有聚集索引而无主键，也可以是完全没有索引的堆。要新增采集（`sys.indexes` 按表判定）与新条目，且"什么算无索引"需要先定口径（是否排除视图、是否只看堆） |
| G-18 | **OS 版本生命周期**（Windows Server 2012 R2 已 EOL 2023-10-10） | `sys.dm_os_windows_info.windows_version` 形如 `10.0 <X64> (Build 17763: )`，**无法区分 Windows 10 1809 与 Windows Server 2019**（同一 build）。要做对必须先建立 build × `windows_sku` → 产品名映射，并且该字段只在本机执行时才拿得到 |
| G-19 | 数据库级角色成员（`db_owner` 等） | 报告 1.7 提到 db_owner 的权限范围，但那是说明文字不是检查项；采集要遍历所有用户库，成本不低，收益待观察 |

---

## 11. 本批落地状态（v1.2.0 → v1.3.0）

对照物：`inspection/sqlserver_inspection.ps1` v1.3.0（1676 行 / 44 个 `Invoke-CollectionModule`）、
`plugins/sqlserver/{rules.py,inspection_rules.json,presentation.py}`。

### 11.1 采集端

| 改动 | 位置 | 性质 |
|---|---|---|
| `Convert-DbValue` 改用 `-is [System.DBNull]` | L164-176 | **修缺陷**：真值 bit 列不再变 null |
| `-OutputDir` 默认值与脚本位置解耦（默认 `%TEMP%\sqlserver_inspection`） | 参数区 + `L103` 一带 | 修 F-21（AGENTS.md：采集脚本不得依赖自身所在目录） |
| `configurations` 白名单补 `blocked process threshold (s)` | 实例配置模块 | 补采集 |
| 新增 `session_activity` 模块 | 连接数统计模块之后 | 补采集 |
| 头部契约新增「bit 列语义」段 | L19-30 | 说明 `collector_version < 1.3.0` 的包 `null ≠ false` |

### 11.2 分析层

| rule_id | 级别 | 触发条件 | 基线包是否触发 |
|---|:---:|---|:---:|
| `INSTANCE.VERSION_SUPPORT` | high / medium | 主版本已过扩展支持 / 剩余 ≤ 180 天 | 否（15.x → 2030） |
| `INSTANCE.ADHOC_WORKLOADS` | medium | `optimize for ad hoc workloads` = 0 | 否（基线为 1） |
| `INSTANCE.BLOCKED_PROC_THRESHOLD` | low | 该键存在且值 < 30 | 否（基线无该行） |
| `INSTANCE.BACKUP_COMPRESSION` | low | 值为 0 且 `require_backup_compression=true` | 否（默认 false） |
| `SESSION.RESOURCE_HOG` | low | 单账号 CPU 占比 ≥ 80% 且过地板值 | 否（无该键） |
| `AGENT.JOB_OVERDUE` | medium | 已启用作业的 `next_run_datetime` 超期 ≥ 36h | 否（`next_run` 为 null） |
| `INSTANCE.MAXDOP`（扩展） | medium | 新增 `maxdop == 0 且 cpu_count > 上限` 分支 | 否（基线 MAXDOP=2） |
| `_dt` 修复（非判据） | — | 证据文案与 severity 分布变化 | 触发集合不变 |

### 11.3 报告层

新增「会话资源占用」条目（waits 章节）；实例基础信息补物理 CPU/NUMA 两行；
作业清单补「最近耗时」列；TempDB 章节补累计读写与延迟三行；
`_inject_analysis` 映射补 4 组（版本/启动参数族/会话资源/作业超期）。

### 11.4 验证记录（可复跑）

`_work/_verify_sqlserver_v13.py` 三部分，全部通过：

1. **基线回归**（真实源包 `D:\inspection\sqlserver_inspection_v1_db-sql-01_20260728_000158.zip`）：
   health 0 / findings 24 / 完整度 100.0 / sections 12 / items 27 / charts 8 ——
   与冻结值逐项一致，且 6 条新判据在基线包上**零触发**。
2. **合成 v1.3 快照**（现场报告 2 的真实取值）：6 条新判据各自触发、3 条该静默的静默、
   `require_backup_compression` 开成 true 后 `INSTANCE.BACKUP_COMPRESSION` 立刻触发、
   版本生命周期三态（2019/2017 不触发，2014/2012 触发）正确、
   新列渲染出 `16 × 4` / `NUMA 4` / `20m53s` / `12m38s` / tempdb 累计读写。
3. **DBNull 影响面**：还原 3 行 `is_db_chaining_on` 的 null → true，findings 24 → 27，
   `DB.CHAINING` 多出 3 条 —— 坐实"判据在真实包上是哑的"。

### 11.5 基线期望值的同步

`_dt` 修复改变了 4 条 `DB.CHECKDB` 的 severity（high → medium），
所以 `tests/baselines/sqlserver/current/baseline.json` 的
`expected_summary.severity_counts` 由 `8/9/6/1` 修正为 `8/5/10/1`，并追加 `refresh_notes` 说明来龙去脉。
`health_score` / `risk_register` / `collection_quality` / `sections` / `items` / `charts` **六项未变**。

目录内的 `analysis.json` / `report_model.json` / `report_model_standard.json` / `*.docx`
是**外部实现**的产物，本次未重新生成，其中的 DB.CHECKDB 文案已与当前代码不一致（已登记在
`refresh_notes` 与基线 README）。

## 12. 两份现场报告的覆盖度矩阵（v1.3.0 现状复核，2026-09-24）

**被问的问题**：当前采集脚本采集的信息，是否覆盖了那两份现场报告？

**核对基准**：两份报告是对话里贴的文本，不在工作区，逐条内容以本文档 §1–§10 的登记项为准
（§1–§8 = 第一份 2019 报告；§9–§10 = 第二份 2014 SP2 报告）。本轮把「报告条目清单」
与「当前脚本的实际采集面」两侧重新对齐跑了一遍，不引用上一批的结论。

### 12.1 核对方法（三条独立证据，可复跑）

| # | 证据 | 实测结果 |
|:---:|---|---|
| ① | **产出面**：42 处 `Invoke-CollectionModule` 调用 + 5 处 `$snapshot.*` 直写（`instance` / `tempdb` / `security` / `os_metrics` / `performance_samples`） | **47 个顶层键**；`collection.module_status` 登记 **47 个模块名** |
| ② | **契约差集**：呈现层 `_coll()` 消费的 40 个模块名 ⊖ 采集端登记名 | 「呈现层要读、采集端未登记」= **空**（47 键全部可解析）；另有 7 项（可疑页面 / 孤立用户 / 性能采样 / 索引碎片 / 陈旧统计 / 高执行次数SQL / 高物理读SQL）走**直接键名**读取，不经过 `_coll`，所以单向按 `_coll` 判会得到假阳性 |
| ③ | **孤儿键扫描**：47 个键在 `plugins/sqlserver` + `inspection_core` 全部源码里的出现次数 | 出现次数最少的 `ha_status`（1 次），**0 个完全无人读的键** |

> ⚠️ **工具限制（本轮实测，必须记住）**：`tools/coverage_audit.py` 按 **MySQL 包结构**
> （`tables/` `evidence/` `timeseries/` `history/` 四个目录）解析采集包；喂它 SQL Server 的
> `snapshot.json` 型包，输出恒为「采集 0 项 / 真读取 0 / 无人提 0」——**看起来像"全部通过"，
> 实际是压根没解析**。这条比"报一个错"更危险：它会让人以为覆盖度没问题。
> 在给它加 SQL Server 布局适配之前，**SQL Server 的覆盖度结论不得引用该工具**；
> 本节的三条证据都是自建核对。`_work/_moddiff2.py`、`_work/_read_surface.py` 是本轮的核对脚本。

### 12.2 报告 B（`HISDB-NODE1` / SQL Server 2014 SP2 / Windows Server 2012 R2 / AlwaysOn）

| 报告节 | 报告要的内容 | 采集来源（键） | 覆盖 |
|---|---|---|:---:|
| 1.1 实例信息 | 实例名 / 版本 / SP / Windows 版本 / CPU / 内存 / 排序规则 / 是否集群 | `instance`（`server_name` `product_version` `product_level` `product_update_level` `edition` `collation` `os_version` `os_service_pack_level` `os_sku` `cpu_count` `socket_count` `cores_per_socket` `numa_node_count` `physical_memory_mb` `is_clustered`）+ `ha_status` | ✅ |
| 1.2 启动参数 | 8 个参数 + 「哪些可在线改」 | `configurations`（17 项白名单，含 `max/min server memory` `max degree of parallelism` `cost threshold for parallelism` `optimize for ad hoc workloads` `backup compression default` `blocked process threshold (s)`；列含 `is_dynamic`） | ✅ |
| 1.3 运行情况 | ——（报告此节只有标题） | 可回答：`error_log_summary` / `volumes` / `wait_stats` | ⬜ |
| 1.4 文件系统剩余空间 | 各盘总量 / 剩余 | `volumes`（`dm_os_volume_stats` → `xp_fixeddrives` → 备份路径解析，三层合并，含备份盘） | ✅ |
| 1.5 数据库基本情况 | 库 / 数据大小 / 日志大小 / 恢复模式 / 兼容级别 / 创建时间 | `databases` + `database_summary` + `database_files` + `log_space` | ✅ |
| 1.6 备份 | 备份是否正常 | `backups` + `backup_history`（耗时 / 压缩 / 校验和 / 加密 / LSN 链）+ `no_backup_databases` | ✅ 强于报告 |
| 1.7 用户和角色 | 角色权限范围 | `security`（`sql_logins` / `sysadmin_members` / `linked_servers` / 5 项安全检测） | ⚠️ 实例级 ✅，**数据库级角色成员未采**（G-19） |
| 1.8 bufferpool | 缓冲池占比 | `buffer_pool` + `memory_clerks` + `memory_status` + `system_memory` | ✅ 强于报告 |
| 1.9 活跃用户进程 | 账号 / 会话数 / 累计 CPU / 物理 IO | `connection_stats` + **`session_activity`** + `active_requests` | ✅ |
| 1.10 Job | 作业清单 / 耗时 / 失败 | `agent_jobs`（含 `last_run_duration`）+ `failed_jobs` | ✅ |
| 1.11 缺失索引 | 缺失索引建议 + **无索引表清单** | `missing_indexes` / `unused_indexes` / `tables_without_pk` / `stale_statistics` / `fragmented_indexes` | ⚠️ 缺失索引 ✅，**「无索引表」零采集**（G-17） |
| 1.12 表大小 | 表空间 TOP | `large_tables` | ✅ |
| 3 总结 | 硬件采购话术 | 结论章由规则结果结构化生成，无采集需求 | ⬜ |

### 12.3 报告 A（第一份 / SQL Server 2019）

| 报告节 | 报告要的内容 | 采集来源（键） | 覆盖 |
|---|---|---|:---:|
| 2.1 操作系统版本 | Windows Server 2019 Standard | `instance.os_version` / `os_sku` / `os_service_pack_level`（取 `dm_os_windows_info`，**不是**执行机的 `Get-CimInstance`） | ⚠️ 原始值 ✅（F-01 已修）；**产品名映射 + OS 生命周期判定未落地**（G-18，分析层缺口） |
| 2.2 软件目录 / 数据目录 | Binn 路径、默认数据 / 日志 / 备份目录 | `server_services.filename` + `instance.instance_default_data_path` / `_log_path` / `_backup_path` | ✅ F-08 |
| 2.2 数据量 / 日志组数目 | 2408 G / 21 | `databases` 逐库求和 + `database_summary` + `log_file_count` | ✅ F-16 口径已定（非系统库且排除 tempdb 的日志文件数） |
| 3.1–3.5 OS 指标 | CPU / 内存 / 磁盘 IO / 网络 / 磁盘介质 | `os_metrics`（WMI 已格式化类 + N 采样点 + `physical_disks` 的 `media_type` / `bus_type` / `spindle_speed`） | ⚠️ **有条件**：仅目标为本机时采；远程一律 `skipped` + 原因 |
| 4.1 AG | 副本 / 同步状态 / 最后重做时间 / 延迟 | `availability_replicas`（含 `availability_mode_desc` `last_redone_time` `secondary_lag_seconds` `last_connect_error_*`）+ `ha_status` | ✅ F-06 |
| 4.3 数据文件合计 | 逐库数据 / 日志 | `databases` + `database_files` | ✅ |
| 4.4 大表 | 空间 TOP10 | `large_tables` | ✅ F-11 |
| 4.5 内存组件 | 20 行 clerks | `memory_clerks`（TOP40，**不含**已废弃的 AWE 列） | ✅ F-05 |
| 4.6 错误日志 14151 | 频次 / 来源 | `error_log_summary`（中英双语筛选 + `error_number` 独立成列并入聚合键 + `xp_readerrorlog` 轮转 #0–#9） | ✅ F-10 |
| 4.6 复制错误 | 发布 / 订阅 / 错误 | `replication`（按 `sys.databases.is_distributor` 探测分发库 + `MSpublications` / `MSrepl_errors`） | ✅ F-09 |
| 4.7 备份记录 | 备份清单 | `backup_history` | ✅ F-15 |
| 5.1 慢 SQL | 最大时间 / 最小时间 | `top_queries` 四块（`max_elapsed_ms` / `min_elapsed_ms` / `total_spills` / `query_hash` / `query_plan_hash`） | ✅ F-07 |

### 12.4 结论：还差什么

**能靠采集回答的部分已全覆盖**，不存在「采了没人读」的字段（孤儿键 = 0）。
真正「报告有、脚本给不出」的只剩 **2 条**：

| 编号 | 报告位置 | 缺什么 | 性质 | 落地成本 |
|:---:|---|---|---|---|
| G-17 | B-1.11 | **无索引表清单**（堆表 / 完全没有索引的表） | 采集缺口：`no_index` / `TableHasIndex` / `heap` / `index_id=0` 在采集脚本里**全 0 命中**；现有 `tables_without_pk` 是「无主键」口径，**主键 ≠ 索引** | 中：加一个模块 + 先定口径（是否排除视图、是否只看堆） |
| G-19 | B-1.7 | **数据库级角色成员**（`db_owner` / `db_datareader` 等实际成员） | 采集缺口：只采了实例级 `sysadmin` 成员；要遍历用户库 | 中高：成本随库数线性增长，收益待观察 |

再加三类"不是覆盖缺口但不能算 ✅"的：

| 编号 | 项 | 为什么不算采集缺口 |
|:---:|---|---|
| G-18 | Windows 产品名 + 生命周期（Win 2012 R2 已 EOL 2023-10-10） | **数据已采齐**（`os_version` / `os_sku` / `os_service_pack_level`）。缺的是分析层的 build × sku → 产品名 / EOL 映射表；`INSTANCE.VERSION_SUPPORT` 目前只判 **SQL Server** 版本，不判 OS |
| §7 条件项 | OS 计数器（A-3.1–3.5） | **有意为之**：远程连库时采到的是执行机数据，写进客户报告等于伪造证据 → 标 `skipped` 并写原因（符合 AGENTS.md「未采集 ≠ 0」）。要覆盖这些节，**必须在目标机上本地执行** |
| F-18 | 注释密度 4.7%（目标 ≥20%） | 代码质量项，与覆盖度无关 |

报告自身空白 / 自相矛盾的节（B-1.3、B-3、A-3.1–3.5 空正文、A-4.6 结论无证据链）不计入覆盖缺口 ——
那些地方**脚本出得来数据、是报告没填**，属于 §4 / §10.3 的「脚本强于报告」。


## 13. 从 MySQL 采集器吸收（2026-09-24 横向对比）

对照物：`inspection/mysql_inspection_standard.sh`（v1.1.0-standard / 1863 行 / 注释 15.8%）。
本轮**只做对比结论，不改代码** —— 每条都给了证据行号与影响面，落地按 §13.6 分三批。

### 13.0 结论：不是「风格差异」，是跨库一致性缺口

横向核对四个采集脚本的机制覆盖（实测 grep，非推测）：

| 机制 | MySQL | PG | Oracle | SQL Server |
|---|:--:|:--:|:--:|:--:|
| manifest 逐文件 sha256 + size | ✅ | ✅ | ✅ | **❌** |
| 信号处理（中断留痕 + 清凭据） | ✅ | ✅ | ✅ | **❌** |
| 打包前敏感扫描（命中即拦截） | ✅ | ✅ | ✅ | **❌** |
| 单调时钟计时 | ✅ | ✅ | ✅ | **❌** |
| 能力探测（capabilities） | ✅ | ✅ | ❌ | **❌** |
| 退出码语义位 | 6 个 | ✅ | ✅ | 2 个 |
| 失败原因分型 | 10 态 | ✅ | ✅ | 4 态（无分型） |

前四条是**同一份交付标准，三个库都做了、SQL Server 没做** —— 不能用「PowerShell 与 Bash 不同」解释掉。
（第 5 条 Oracle 也没有，所以它是"可选"，不是缺口。）
SQL Server 现在注释密度 **4.7%**，MySQL **15.8%**；差距的主要来源不是行数，而是**机制背后的"为什么"没写下来**。

### 13.1 及格线（4 条，缺陷型）

#### A-1 异常不分型：`failed` 把「脚本写错」和「环境不支持」混成一类【P1】

- **现状**：`Invoke-CollectionModule` 的 catch 一律 `status='failed'` + 原始 message（`L231-237`）。
  全脚本 `SqlException.Number` / `Exception.Number` 实测命中 **0 次**。
- **对照**：MySQL `classify_failure`（`L235-250`）分 10 态，其中 **`schema_mismatch`（表在、列不在 = 采集器自身缺陷）
  与 `unsupported`（环境真不支持）必须分开**。原注释写得直白：
  > 两者以前都归 unsupported，于是脚本写错 SQL 时下游判定「该环境采不到此项」，
  > 既不告警也不提示，**缺陷永远不会被发现**。
- **后果**：SQL Server 的 DMV 列集跨版本差异极大（`sys.dm_db_log_info` 2017+、`numa_node_count` 2016+ …），
  当前靠**手写 `$productMajor` 门控逐处规避**。漏一处 → 2014/2016 上报 `Invalid column name` → 落 `failed`，
  与「权限不足」「真不支持」混在一起。报告里看起来像「这台机器采不到」，实际是脚本 bug。
- **建议**：catch 里按 `$_.Exception.InnerException.Number`（SqlException 错误号）分类：
  `207`→`schema_mismatch`、`208`→`object_missing`、`229`→`permission_denied`、`1222/8645`→`timeout`、其余→`failed`。
  **PowerShell 在这条上比 MySQL 有优势** —— 错误号稳定，不像文本会随语言/版本变（MySQL 只能 grep 文本）。
- **影响面**：改 `module_status[].status` 取值集合；下游若按 status 分支需同步；分类结果同时写进 `limitations`。
  **不改则完全静默**：不报错、包照常产出、只是"脚本缺陷"永远不浮出。

#### A-2 `elapsed_ms` 用 wall clock 计时【P2】

- **现状**：`[int]((Get-Date)-$begin).TotalMilliseconds`（`L230`/`L234`/`L1341`/`L1345`…）。
  全脚本 `Stopwatch` / `TickCount` 实测命中 **0 次**。
- **对照**：MySQL `monotonic_ms()`（`L116-118`）读 `/proc/uptime`，PG / Oracle 同样有单调时钟。
- **后果**：Windows 的 w32time 定期校时，跳变时 `elapsed_ms` **会变负或虚高**。
  这个数字会被当成"采集器性能证据"（MySQL 的 summary 直接有"耗时最长的采集项前 10"），排序会错。
- **建议**：`[System.Diagnostics.Stopwatch]::StartNew()`，取 `$sw.ElapsedMilliseconds`。
- **影响面**：只改数值语义；**当前没有任何测试断言 `elapsed_ms`**，可安全改。

#### A-3 性能采样无 requested/actual 对比，缺 `partial` 态【P1】

- **现状**：`$samples.Count` 直接当 `row_count`，状态**恒 `success`**（`L1389`）。
  取不到点的分支是 `if ($null -ne $point)`（`L1374`）—— **空点被静默跳过，循环照跑，状态不受影响**。
- **对照**：MySQL `collect_realtime_samples`（`L944-954`）算 `requested SAMPLE_COUNT` vs
  `actual mysql_points/cpu_points`，不足则 `status=partial` + `rc=2`，
  reason 写清 `requested 6 intervals but collected X`。
  另有 `compute_sar_coverage` 独立算历史**覆盖率**并落 `coverage.json`（请求 24h → 实覆盖几小时）。
- **后果**：请求 6 点、实得 2 点（计数器重置/权限/瞬时错误），状态仍是 `success`，
  `performance_samples` 段里就 2 条。**下游无法区分「本来就只有 2 点」和「这次采样不正常」** ——
  而 Batch/TPS 差分需要至少 2 个有效点，采样不足会直接让报告里的吞吐数据失真。
- **建议**：`performance_samples` 段加 `requested_sample_count` / `actual_sample_count` /
  `actual_elapsed_ms` / `skipped_points`；`actual < requested` 时落 `partial`。
  对齐 MySQL 的 `sampling` 段形状最有价值（下游可复用同一套读法）。
- **影响面**：新增快照字段 + 新增一种 status 取值；需同步 `docs/sqlserver-collector-interface.md` 的快照键契约。

#### A-4 无中断处理：Ctrl+C 会留下「看起来完整」的半包【P1，本清单最隐蔽】

- **现状**：`trap` 实测命中 **0 次**。中断停在哪就在哪，而：
  - `L318-338` **已经把临时目录重命名成正式包名**（`sqlserver_inspection_v1_<ip>_<name>_<port>_<stamp>`）；
  - `L1405` **已经写了 `manifest.json`**（含 `snapshot.json` + `collector.log`）；
  - 只差 `inspection_report.md`（`L1649`）和 ZIP。
- **对照**：MySQL `handle_signal`（`L1691-1696`）中断时 kill 后台任务 + 写
  `COLLECTION_INCOMPLETE` 标记文件 + `exit 130`；`cleanup_auth` 另挂 `trap EXIT` 兜底；
  `exit 31` 专门表达"打包失败但目录可用"。
- **后果**：中断后的目录**有 manifest、有 snapshot、有 collector.log，目录名也是正式格式**，
  看结构完全正常，唯一缺的是 Markdown —— **收包人不会怀疑**。
  且没有 `COLLECTION_INCOMPLETE` 这类标记，也没人知道 `$script:ConnectionString`（含明文密码）还留在内存里。
- **建议**：`try/finally` 包裹主流程（或 `Register-ObjectEvent [Console]::CancelKeyPress`）：
  中断时写 `COLLECTION_INCOMPLETE`、清 `$script:ConnectionString`。
  `Compress-Archive` 失败时也要给"目录仍可用"的明确出口（对应 MySQL 的 `exit 31`）。
- **影响面**：新增一个标记文件；不改现有产物结构。

### 13.2 加分题（5 条，下游可用性 / 合规）

#### B-1 manifest 只有文件名，没有 size/sha256，且被写两次【P1】

- **现状**：`L1405` `files=@('snapshot.json','collector.log')` → `L1652` 追加
  `inspection_report.md` 后**整份重写**。`Get-FileHash` / `sha256` 实测命中 0 次。
- **对照**：MySQL `generate_manifest`（`L1648-1660`）逐文件写 `{path, size_bytes, sha256}`，
  并有一条关键注释：
  > manifest 已生成后不再修改任务目录中的任何文件，确保 SHA256 可复验。
- **后果**：回传包**无法校验完整性** —— 传输截断、部分损坏、被改动都发现不了。**四库唯一**。
- **建议**：`Get-FileHash -Algorithm SHA256`（PS 5.1 内置）+ `(Get-Item).Length`。
  时序上必须在**最后一次写 manifest**（`L1652`）时算；`collector.log` 在 `L1650` 还会被追加一次，
  所以"算哈希"不能早于 `L1651`。**当前顺序恰好正确，但很脆弱 —— 要加注释钉死。**
- **影响面**：`manifest.json.files` 从**字符串数组 → 对象数组**，属**破坏性变更**。
  下游若读 `manifest.json.files` 需同步；接口文档要标版本。

#### B-2 无隐私声明段；`-ExcludeSqlText` 靠字面量枚举字段【P2】

- **现状**：`L1395-1398` 硬编码 6 个键名（`blocking` / `active_requests` / 4 个 `top_queries_*`），
  把 `sql_text` 替换成字符串 `'[excluded]'`。快照里**没有任何字段声明"本次是否隐藏了 SQL 文本"**。
- **对照**：MySQL 快照有 `privacy` 段（`L1561-1562`）：
  `{sql_text_included, log_text_included, full_configuration_included, password_included}`。
- **后果**：① 下游要判断"这个包的 SQL 文本能不能用"，只能扫数据找 `'[excluded]'` 字面量；
  ② **新增一个带 `sql_text` 的采集键就会漏脱敏**（枚举式维护）；
  ③ 客户拿到包无法一眼确认外发范围。
- **建议**：加 `privacy` 段（至少 `sql_text_included` / `sql_text_field_count`）；
  把键名列表集中成一处常量并注明"新增 SQL 类采集键必须登记"，
  或改为遍历 `$snapshot` 全部顶层键按字段名匹配。
- **影响面**：新增快照段；脱敏覆盖范围可能扩大（这本身就是修 bug）。

#### B-3 无打包前敏感扫描与拦截【P2】

- **现状**：`security_scan|敏感` 实测命中 **0 次**。包内高风险面：
  SQL 文本（4 个查询键）、`linked_servers`（`@datasrc` / `@provstr` 可能含连接串与凭据）、
  `error_log_summary.sample_message`、`sql_logins` / `sysadmin_members`（账号名）、
  `server_services.service_account`（服务账户）。
- **对照**：MySQL `security_scan`（`L1633-1646`）打包前扫全目录，命中高置信模式 →
  **阻止打包 + `exit 30`**。扫描**排除 `collection.log` / `manifest.json` / findings 自身**
  （否则日志里的 redact 痕迹会自我命中）。
- **建议**：打包前 `Select-String` 扫 `password\s*=` / `pwd=` / `BEGIN .*PRIVATE KEY` /
  `Authorization:\s*(Basic|Bearer)`；命中则**不打包**并给独立退出码。
- **影响面**：新增一个退出码；不命中时零行为变化。注意 `-ExcludeSqlText` 是"主动开关且默认采"，
  本项是**兜底**，两者不互相替代。

#### B-4 `module_status` 不带 `target_key`【P2，一行可证的"信息在手却没落盘"】

- **现状**：`target_key` 实测命中 **0 次**。条目是 `{name, status, row_count, elapsed_ms, error}`，
  而 `name` 是**中文显示名**（'实例基础信息'、'安全与权限'、'TempDB'）。
  但 `Invoke-CollectionModule` 的签名里**本来就有 `$TargetKey`**（`L221`），**37 处调用全部传了**。
- **对照**：MySQL 的状态行有 `output_file`（`safe_relpath` 相对路径）→ 下游知道"失败的这项，数据本该落在哪"。
- **后果**：下游要知道"哪个**快照键**不可用"，必须维护中文名 → 键名的映射表。
  映射表随显示名改动而悄悄失效（改 `name` 不会报错，只是下游对不上）。
- **建议**：`Invoke-CollectionModule`（`L230`/`L234`）与 `Add-ModuleStatus`（`L244`）的状态对象都加 `target_key`。
  **一处改，37 个模块受益。**
- **影响面**：`module_status` 条目加字段；`tests/test_sqlserver_module_names.py` 可加断言守住。

#### B-5 无输出空间预检【P2】

- **现状**：无。`Get-PSDrive` / `Get-Volume` 用于此目的命中 0 次
  （grep 到的"空间"全是业务语义：`log_space`、`data_size_mb` 等）。
- **对照**：MySQL `check_output_space`（`L558-566`）开跑前检查，不足 `exit 11`，
  且**处理"无法确认剩余空间"的分支**（不阻断，只 warn —— 不把"测不出来"当"不足"）。
- **后果**：磁盘满 → `L1404` `Set-Content` 中途失败 → 因 `$ErrorActionPreference='Stop'`
  直接抛异常，**留下半个 `snapshot.json`**（且与 A-4 叠加时同样没有不完整标记）。
- **建议**：跑任何采集前检查 OutputDir 所在卷可用空间，不足时给明确报错与退出码。
- **影响面**：新增前置检查，零产物变化。

### 13.3 可选（价值中等，看排期）

- **C-1 能力探测集中化**：MySQL `probe_capabilities`（`L778-846`）**表级 + 列级**探测一次，
  写 `capabilities.tsv` + 快照 `capabilities` 段，下游只读快照。
  SQL Server 的可用性事实**散在**十几处 `$productMajor` 门控 + try/catch 里，
  且"这台实例有没有 Query Store / HADR / VLF DMV"在报告里**查不到**（靠 `not_applicable` + 空数组**推断**）。
  注意：**Oracle 也没有这条** → 它是"可选"，不是一致性缺口。
- **C-2 把"为什么串行"写进注释**：MySQL `L1810` 有一行 ——
  > 实时时序、历史 sar、系统静态信息并行；**MySQL SQL 组串行执行，避免对生产库造成过高并发**。
  SQL Server 全串行（**这是对的**，同类业务、无并行需求），但没有这行注释 →
  后人可能 `ForEach-Object -Parallel` 化，把客户生产库打爆。**一行注释的成本。**
- **C-3 退出码语义位补全**：SQL Server 只有 0/2。MySQL 有 10/20/30/31/130，
  其中 **31（打包失败但目录可用）** 与 **20（连接失败）** 对 SQL Server 同样适用
  （现在连接失败是裸 `throw`，打包失败是异常退出，自动化无法区分）。

### 13.4 不适用（不要照搬）

| MySQL 机制 | 为何不适用 |
|---|---|
| `known_tsv_header` / `ensure_tsv_header`（空结果补占位表头） | MySQL 需要是因为 `mysql` 客户端**空结果不输出列名**。SQL Server 走 `DataTable` → `ConvertTo-Json`，空结果就是 `[]`，且 `success_no_data` 已表达同一事实 |
| `sanitize_tsv_columns`（列白名单裁剪） | 对应物是 `sys.configurations` 白名单（已有，17 项）。**已覆盖** |
| `mysql_exec` 禁用 `--raw`（F-01） | SQL Server 无客户端文本转义层。**无对应问题** |
| `umask 077` / `LC_ALL=C` | Windows ACL 与 .NET 编码模型不同。**不适用**（B-3 的扫描替代了它的部分作用） |
| `mysql_query_fallback`（主查询失败自动降级） | SQL Server 的"版本门控 + 二次查询覆盖"（如 `is_query_store_on`）是手工版；**当前无真实失败案例**，等 A-1 落地后按分类结果决定是否统一 |
| `record_skipped` | `Add-ModuleStatus -Status skipped` 已等价 |
| `classify_failure` 里"stderr 空则从 stdout 借 500 字节" | 异常对象总有 message。**不适用** |

### 13.5 反向：SQL Server 更优，别为"对齐"而退化

- **`limitations` 独立列表**：MySQL 只有单列 `reason` 文本；SQL Server 把失败原因单独成表、可读可聚合。**保留。**
- **`Test-LocalTarget` 三条独立证据**（`L1198-1222`：字面量 / **SQL 侧 `machine_name`** / 连接 IP 落在本机网卡）：
  **比 MySQL 的 `is_local_connect_target`（`L154-163`）更强** —— MySQL 只靠本机 hostname/FQDN/IP 列表，
  不利用数据库侧报告的主机名。**保留，可反向输出给 MySQL。**
- **`module_status[].error` 结构化保留**：MySQL 只把 stderr 尾部 5 行拍平成 reason 文本。**保留。**
- **模块状态 4 态含 `success_no_data`**：MySQL 用 `ok`/`empty` 两态表达同一区分。**等价，不用改。**

### 13.6 建议落地批次

按「改动面重合度」分组（同一批只回归一次）：

- **批 1 — `module_status` 层（3 项，只改状态对象与取值）**
  A-1 异常分型 + A-2 单调时钟 + B-4 `target_key`。
  三条都落在 `Invoke-CollectionModule` / `Add-ModuleStatus` 同一段代码，一次改完一次回归。
  A-1 会改 `status` 取值集合 → 需同步接口文档 + 下游分支。
- **批 2 — 运行时健壮性（3 项）**
  A-3 采样 requested/actual + A-4 中断处理 + B-5 空间预检。
- **批 3 — 打包与合规（3 项）**
  B-1 manifest 哈希（**破坏性结构变更**）+ B-2 privacy 段 + B-3 敏感扫描。
- **可选**：C-1 能力探测集中化（与其他库对齐后再定）、C-2 串行注释（可随时单独插）、C-3 退出码补全。

**验证纪律**（照 §11 的做法）：每批落地后跑
`tests/test_sqlserver_module_names.py` + `tests/test_sqlserver_plugin.py` + `tests/test_rule_pack_config.py` +
`tests/test_sqlserver_analyzer.py`，再跑全量 `pytest tests/` 与
`tests/baselines/sqlserver/current/` 对照；改到快照键或 status 取值时**必须同步
`docs/sqlserver-collector-interface.md`**，并说明是否需要刷新基线。

---

## 14. 分析层与报告层提升清单（采集之后的下一段）

§13 比的是「采集器相对 MySQL 采集脚本」。本节换一个坐标：**采集已经拿到的数据，有多少真正走到了客户眼前**。
方法是三条面互相做差集，不看清单记忆：

| 面 | 定义 | 实测值 |
|---|---|---|
| 采集注册面 | `module_status` 里登记的全部模块名 | **47 个** |
| 报告渲染面 | `presentation.py` 各章节 `append` 的条目 | **49 条** |
| 规则消费面 | `rules.py` 里 `self.add(...)` 的调用点 | **66 处 / 62 个不同 `rule_id`** |

> 采集注册名有**四种登记形态**，少覆盖一种就会误报缺口：位置参数 `Invoke-CollectionModule '名' '键'`（37 处）、
> 命名参数 `Invoke-CollectionModule -Name '名' -TargetKey '键'`（5 处）、内联 `moduleStatus.Add(...name='名';...)`
> （分隔符是 **`;` 不是 `,`**，4 个专用块）、变量承载 `$osModuleName = 'OS 指标'`。
> 详见本节末尾「自我纠错」。

### 14.1 P1 — 采集状态被写死成「达标」（3 处）

`presentation.py` 有 **3 个章节条目的 `collection.status` 是硬编码 `"ok"`**，与 `module_status` 里的真实状态无关：

| 位置 | 条目 | 真实状态来源 |
|---|---|---|
| `L489` | 性能采样 | 采集端模块名 `性能采样`（`L1389/L1392`） |
| `L625` | SQL登录 | 采集端模块名 `安全与权限`（`L902/L905`） |
| `L630` | sysadmin成员 | 同上 |

**MySQL 的 `presentation.py` 这一写法是 0 处。** 后果与 AGENTS.md
「检查明细的 `status` 必须由采集数据算出来，不得写死」直接冲突：
这三个条目永远显示「达标」，采集端部分失败（例如 `-Server` 远程时 `os_metrics` 落 `skipped`、
`安全与权限` 的某个子查询超时）在报告里看不出来。

改法：与同章节其它条目一致，改走 `_coll("性能采样")` / `_coll("安全与权限")`。

### 14.2 P1 — 专用块的 `elapsed_ms` 恒为 0（9 处硬编码）

`module_status[].elapsed_ms` 在两个世界里取值完全不同：

- 通用模块（`L230/L234`）与 OS 采样（`L1341/L1345`）：`[int]((Get-Date)-$begin).TotalMilliseconds`
- 4 个专用块（`L298/L301/L810/L813/L902/L905/L1389/L1392`）以及 `Add-ModuleStatus` 的入参默认值（`L244`）：
  **字面量 `0`**

也就是说 `实例基础信息` / `TempDB` / `安全与权限` / `性能采样` 四个块的耗时**永远显示 0 毫秒**。
这与 AGENTS.md 的「未采集 ≠ 0」同构：**「没计时」被当成了「0 毫秒」**。
而 `elapsed_ms` 恰恰是判断「哪个模块拖慢了采集窗口」的唯一现场证据，四个最可能慢的块（实例信息要跑
SERVERPROPERTY 组合、安全要遍历登录名、TempDB 要读文件空间、性能采样要固定间隔多次采样）全是 0。

改法：`Add-ModuleStatus` 内部统一用单调时钟（见 §13-②），专用块传入自己的 `$begin`。

### 14.3 P2 — 采了 4 个维度的 TOP SQL，报告只出 2 个

采集端注册了四个 TOP SQL 模块：`高消耗SQL`（CPU）、`高逻辑读SQL`、`高执行次数SQL`、`高物理读SQL`。
渲染面只有前两个（`s8` 的 `L573` / `L578`）。后两个的出口是：

- `rules.py`：**0 命中**
- `presentation.py` 章节条目：**0 命中**
- 只出现在 `L157 / L211` 两个**无人消费的 dict** 里（见 14.5）

**这是唯一的「完全无出口」类**：另外几项至少在风险册里有对象名，这两项连提都没提。采集 SQL 每次都跑，
数据进了包，客户永远看不到「哪几条 SQL 执行最频繁」「哪几条 SQL 物理读最高」——
而后者正是判断「缓冲池不够还是索引缺失」的直接证据。改法：`s8` 补两个条目，列头复用现有 `L574/L581` 的形态。

### 14.4 P2 — 有规则、无明细表（4 项）

这四项**不是没有出口**，是出口形态单一：规则层会为每一个命中对象单发一条 finding（证据里带
`库/schema.表` 或 `库/用户`），报告里**没有一张汇总表**，因此：

| 采集键 | 规则 | 现象 |
|---|---|---|
| `stale_statistics` | `PERF.STALE_STATS` | 命中多时散成几十上百条 finding，无法按变化率排序一眼看全 |
| `fragmented_indexes` | `PERF.INDEX_FRAGMENTATION` | 同上，无法按碎片率排序 |
| `orphaned_users` | `SEC.ORPHANED_USER` | 同上 |
| `suspect_pages` | `DB.SUSPECT_PAGES` | 规则只统计 `event_type ∈ {1,2,3}` 的**按库计数**，页级明细（file_id/page_id）不落报告 |

对应采集模块名 `索引碎片（深度）` / `陈旧统计信息（深度）` / `孤立用户（深度）` 在 `_coll` 侧**无人引用**。
客户场景里这几类常常是几十到上百条的量级，散在风险册里等于没有。改法：各补一张明细表（可截断 TOP N）。

### 14.5 P2 — 三个只写不读的模型字段

`presentation.py` 造了三个大 dict，**没有任何消费者**：

| 字段 | 定义处 | 消费者 |
|---|---|---|
| `system_analysis` | `presentation.py` | **无**（MySQL 同名字段被 `plugins/mysql/word_report.py` 读，SQL Server 的 profile 不读） |
| `sqlserver_performance` | `presentation.py` | **无** |
| `inventory` | `presentation.py` | **无** |

它们出现过的地方只有：自己的定义行、以及 `tests/baselines/sqlserver/current/report_model.json` 这个产物里。
标准报告路径（`report_adapter.py` → `sqlserver_analysis` → Word profile）完全不经过它们。
这是「渲染位留好了、数据层没喂」的**反面**：「数据层喂了、渲染位没留」。

注意区分：`sqlserver_performance` ≠ `sqlserver_analysis`。后者由 `report_adapter.py:360`
专门为图表契约生成（`{"charts": [...]}`），Word profile 的 `model_field="sqlserver_analysis"` 读的是它，
**图表路径正常**，不要去改这个名字。

### 14.6 P2 — `_coll()` 的静默回退（潜在风险，当前未触发）

```python
def _coll(*names):
    for name in names:
        m = mod_ok.get(name)
        if m:
            return {"status": m.get("status", "ok"), "row_count": m.get("row_count", 0)}
    return {"status": "ok", "row_count": 0}     # ← 一个名字都没匹配上时
```

**当前实测：40 个 `_coll` 主名全部有对应登记，命中 0 次，所以这不是现存缺陷。**
但它的失败方向是危险的那一侧：采集端哪天改模块名而呈现层没跟，报告不会报「模块缺失」，
而是显示**「达标 / 0 行」** —— 与 AGENTS.md 明令禁止的「把未采集转换成正常」是同一类。
`大表统计（深度）` / `无主键表（深度）` / `复制配置` 三个回退备选名在采集端**已不存在**
（采集端当前注册名不带「（深度）」后缀），这是**有意的向后兼容分支**，不必删；但回退到最后一个仍失败时
应落 `not_evaluated` 而不是 `ok`。

### 14.7 P3 — 工程一致性（不影响事实）

- **同一个函数两种调用形态**：`Invoke-CollectionModule` 位置参数 37 处 + 命名参数 5 处
  （后者是为了带 `-TimeoutSeconds 300`）。两种都合法，但混用会让「数模块」的静态检查再次踩坑。
- **注释密度**：`sqlserver_inspection.ps1` 79 行 / **4.7%**；`plugins/sqlserver/` 多数文件 **0.0%**，
  最高的 `cli.py` 5.0%、`presentation.py` 3.3%、`rules.py` 1.8%。MySQL 采集脚本是 **15.8%**。
  §13 已指出差距不在行数，在于机制背后的「为什么」与踩过的坑有没有写下来。
- **`tools/coverage_audit.py` 对 SQL Server 包静默全零**（见 §12 末尾）：它是 MySQL 形状
  （按 `tables/`/`evidence/`/`timeseries/` 解析），喂 `snapshot.json` 型包输出「采集 0 项 / 真读取 0 /
  无人提 0」——**看起来像全部通过**。建议加包结构探测，认不出布局就报错退出。
- **`output_sha256.report_model_standard.json` 长期对不上且无人校验**（现盘 `0B63F92E…`，
  登记 `57C262C6…`）：登记项，非本批引入。

### 14.8 自我纠错：本节推翻的两条自己的怀疑

写这一节时我先有两个判断，都被实测否掉了。记下来是因为**它们都是「看起来像缺陷」的假阳性**：

1. **`severity_override` 在 SQL Server 是 0 次 → 二级档位失效？不是。**
   MySQL 的 `rules.py` 用 6 次 `severity_override`、26 次 `_threshold`；SQL Server 是 0 次 / 47 次 `self.t.get`。
   但 SQL Server 的 `add(rule_id, category, title, severity, ...)` **第 4 个参数本身就是 severity**，
   分档靠判定处算好再传：`sev = "critical" if pct < crit else "high"`。四组二级档位逐一核对**全部成立**：
   `file_io_warning_ms/critical_ms`（`L347-348`）、`log_used_warning_pct/critical_pct`（`L376-377`）、
   `vlf_warning_count/critical_count`（`L384-385`）、`volume_free_warning_pct/critical_pct`（`L363-364, L368`）。
   **AGENTS.md 那条 `severity_override` 规则是针对 `_evaluate/evaluate` 框架的，不适用于 SQL Server 的 `add` 接口。**
2. **6 个模块「呈现层要读、采集端未登记」→ 其实是没采？不是。**
   第一版差集用单引号正则，报出 `实例基础信息 / TempDB / 安全与权限 / OS 指标 / 无主键表 / 大表统计` 六项缺口。
   补齐「`;` 分隔符 + `-Name` 命名参数 + 变量名」三种登记形态后，**缺口归零**。
   差点把三个专用块和两个命名参数调用报成「安全类没采」。

### 14.9 建议分批

| 批 | 内容 | 性质 | 回归面 |
|---|---|---|---|
| **批 1** | 14.1 状态写死（3 处）+ 14.6 回退落 `not_evaluated` | 及格线，改动落在 `presentation.py` | 只影响 `collection.status` 字段，需刷新基线 `items` 相关断言 |
| **批 2** | 14.2 `elapsed_ms` 计时统一（含 §13-② 单调时钟） | 及格线，落在 `sqlserver_inspection.ps1` | 采集端，需重跑端到端验证 |
| **批 3** | 14.3 补两个 TOP SQL 条目 + 14.4 补四张明细表 | 加分题，纯增量 | 新增 sections/items，**必须刷新基线** |
| **批 4** | 14.7 工程一致性（coverage_audit 探测、调用形态统一、注释） | 加分题 | 工具与文档，不动产物 |

**待决策**：14.5 的三个只写不读字段是**删掉**（`report_model.json` 结构变更，外部实现产物的兼容性会断）
还是**接上消费者**（把 `system_analysis` 接进 SQL Server 的 Word profile，与 MySQL 对齐）。我倾向后者，
但这是破坏性结构变更，需要你先定。

---

## 15. 批 A 落地状态（v1.3.0 → v1.3.1：把已采到的数据接通到报告，2026-09-24）

### 15.1 决定与范围

用户选定：**只做批 A 四项**；**三个中转字段（`system_analysis` / `sqlserver_performance` / `inventory`）保留、接报告、不删**。

这一批的目标不是"再补采集"——采集面已经够（47 个顶层键 / 0 个孤儿键）。问题是**数据到了包里就断了**。
四项与 §14 的对应关系：

| # | 落地项 | 依据 | 改动位置 |
|---|---|---|---|
| ① | 补 2 个 TOP SQL 报告条目 | §14.3 / §14-⑪ | `presentation.py` `_build_sections` s8 |
| ② | 补 4 张对象明细表 | §14.4 | s9 / s10 / s2（按语义分散） |
| ③ | 3 处写死 `collection.status` + `_coll` 兜底 | §14.1 / §14.6 | `presentation.py` |
| ④ | 新增「采集执行明细」章节 + 专用块计时 | §14.2 的呈现侧延伸 | `presentation.py` 新增 s14 + `sqlserver_inspection.ps1` |

**顺带修（不在四项内，单独列出以便否决）**：

- `report_adapter.py` 的 `appendix.collection_window.sqlserver_sample_points` 原为**写死的 `1`**，
  而采集包实采 **6** 个采样点 —— 报告附录写"采样点 1"会被读成"只采到一个点"、不足以做趋势结论。
  已改为取 `sqlserver_performance.summary.sample_count`（回退 `len(samples)`）。
- `word_report.py` 的 `section_order` **13 → 14**（登记新章节，未登记会被 `_ordered_sections` 排到 99）。
- 采集器 `collector_version` **1.3.0 → 1.3.1**：`elapsed_ms` 的取值语义变了（恒 0 → 真值 / `null`），
  这是可观测的输出变化，下游需要能按版本判断，故升 patch 版本并在脚本头部补 `elapsed_ms 语义` 段。

### 15.2 四项逐条

**① 两个 TOP SQL 条目。** 采集端按四个维度各取 TOP30（CPU / 逻辑读 / 执行次数 / 物理读），
报告此前只渲染前两个。补「高执行次数SQL」「高物理读SQL」到 `sql_index` 章节。
**物理读那条是判断「缓冲池不足还是索引缺失」的直接证据**：逻辑读高说明索引选择尚可，物理读高才说明数据没在内存里。
此前这两个键只出现在 `inventory` / `sqlserver_performance` 两个中转字段里，无人接。

**② 四张对象明细表。** 按语义分散，不硬塞进同一章：

| 表 | 章节 | 数据源 | 模块名（采集端登记） |
|---|---|---|---|
| 陈旧统计信息 | `db_objects` | `sys.dm_db_stats_properties` | 陈旧统计信息（深度） |
| 索引碎片 | `db_objects` | `sys.dm_db_index_physical_stats` | 索引碎片（深度） |
| 孤立用户 | `security` | `sys.database_principals` | 孤立用户（深度） |
| 可疑页面 | `backup` | `msdb.dbo.suspect_pages` | 可疑页面 |

可疑页面放 `backup` 而不是 `db_objects`：页级损坏直接影响可恢复性，且规则 category 是"完整性"。
三项深度检查的模块名**至今带「（深度）」后缀**（它们确实默认不采），所以 `_coll` 传**单参数**——
不能为它们编一个从未存在过的"旧名"塞进第二个参数（那正是 `test_alias_arguments_are_known_legacy_names` 要拦的）。
同时给 `PERF.STALE_STATS` / `PERF.INDEX_FRAGMENTATION` / `SEC.ORPHANED_USER` / `DB.SUSPECT_PAGES`
四条规则补上 `_inject_analysis` 映射，让规则结论落在对应明细表上。

**③ 状态不再写死。** `presentation.py` 三处字面量 `"ok"` 改为读真实 `module_status`：
「性能采样」取自身登记的模块；「SQL登录」「sysadmin成员」共用 `_cst("安全与权限")` ——
**只取状态、行数各算各的**（安全块是一个 try 包住多类查询，`module_status` 只有一条，状态可共用、行数不能共用，
否则 sysadmin 条目会显示登录名的行数）。
`_coll` 的兜底回退从 `{"status": "ok"}` 改为 `{"status": "not_evaluated"}`。
⚠️ 基线包里这三项均为 `success`，所以**本批对基线输出零影响** —— 这是"平时看不出、出事时救命"的修复。

**④ 采集执行明细 + 计时。** 新增第 14 章 `collection_detail`：逐模块列出 状态 / 行数 / 耗时ms / 错误，
**按耗时降序**（最慢置顶，这正是这张表要回答的问题），未计时的模块耗时列显示 `-`。
前置改动在采集端：`Add-ModuleStatus` 新增 `-Begin`；实例基础信息 / TempDB / 安全与权限 / 性能采样
四个专用块各自记录起始时间。**未计时必须落 `null` 而不是 `0`** —— 否则最可能慢的四个块会显示成最快的。

### 15.3 回归证据

| 项 | 实测 |
|---|---|
| ps1 语法 | `PARSE_OK`（6625 tokens）；**1691 行 / 81 行注释 / 4.8% / 纯 LF + UTF-8 BOM** |
| presentation.py | **892 行 / 43 行注释 / 4.8% / 纯 CRLF**（该文件的既有约定） |
| 定向 pytest | **34 passed / 17 subtests passed**（module_names + plugin + analyzer + rule_pack_config + severity_tiers） |
| 全量 pytest | **5 failed / 203 passed / 34 subtests passed**；5 项与改动前**逐条同名一致**（缺 MySQL 夹具 tar.gz 的预存在失败）→ **零新增回归** |
| 实跑基线包 | `sections` **12 → 13**、`items` **27 → 30**；`health_score=0` / `findings=24` / `charts=8` / `severity_counts=8/5/10/1` **五项均未变** |
| 合成快照验证 | 6 个新条目全部渲染；状态跟随 `failed`；耗时降序正确；未计时显示 `-` → **ALL_PASS** |
| 基线同步 | `expected_summary.sections/items` 已更新并追加 `refresh_notes`；JSON 合法、CRLF 保持 |

**过程中自己踩的两个坑**（记录以免复发）：

1. **改 CRLF 文件时 `open(p, 'w', newline='')` 会静默把换行变成 LF** ——
   读取时的 universal newlines 已把 `\r\n` 折成 `\n`，写入再不转换就丢了 CR。
   `presentation.py` 的 790 行 CRLF 一度变成 LF（已恢复并逐字节校验 892/892）。
   **改这类文件一律按 bytes 读写 + 显式 `\n → \r\n`**。
2. **`_coll` 的第二个参数是"已登记的历史旧名"，不是"备选名"** ——
   我最初写 `_coll("陈旧统计信息（深度）", "陈旧统计信息")` 被
   `test_alias_arguments_are_known_legacy_names` 拦下。契约是：`args[0]` 必须是采集端**已注册的当前名**，
   `args[1:]` 只能是**真实出现过的旧名**（登记在 `LEGACY_ALIASES`）。给深度三项编"去后缀名"是篡改历史。

### 15.4 明确未做（留在后续批次）

- **批 B（采集端工程面）**：异常分型（`Exception.Number` → `schema_mismatch` / `unsupported` / 权限）、
  `target_key` 落盘、单调时钟（`Stopwatch` 替 wall clock）、`trap` 中断处理。
- **破坏性项**：manifest 逐文件 `sha256` + size、`privacy` 段、打包前敏感扫描 ——
  需先定"哪些字段算敏感"的口径，且 manifest 结构变更会断下游解析。
- **§14.5 三个中转字段**：按决策**保留**。其中 `sqlserver_performance` 装着 `top_queries_executions` /
  `top_queries_physical_reads` 两个键，**删掉等于把①的数据来源关掉**；本轮只把它们接到报告渲染，字段本身不动。
- **§14.7 的 `coverage_audit.py` 包结构探测**（认不出 SQL Server 布局时静默全零，比报错更危险）。

### 15.5 批 A 验证过程中新发现的两项（**① v1.3.2 分析层已修，见 §18** / ② 待核）

用批 A 后的代码**实跑基线包**（`analyze.py --db-type sqlserver` → `generate_report.py`，产物 `_work/demo_batchA/`）
并逐表交叉核对原始 `snapshot.json` 时，撞到两条不在批 A 范围内、但已成事实的问题：

**① 附录「规则执行状态」三行恒为 0（跨库不一致）。【P2 / 分析层】** ——✅ **已修（§18）**

- **实际行为**：`report_adapter.py` L219–234 的 `evaluations` **只由 `findings` 反推**，每条都写死 `status="triggered"`；
  `rules.py` 全文只有 `self.add(...)`（触发才产出），**从不产出 `passed` / `not_applicable` 的 evaluation**。
  而 `word_engine.build_appendix_professional` L1597–1603 是**按 `evaluations` 里各状态的条数**填那张表 →
  实测基线包渲染结果固定为「触发 14 / 通过 0 / 未判断 0 / 不适用 0」。
- **不改的后果**：该表有四行、其中三行**结构上永远是 0**，客户会读成"没有一条规则通过"；
  而同一页的免责声明写着"未采集、未评价与不适用项目不得解释为正常" ——
  **这正是 AGENTS.md 禁止的"把未知写成 0"**。MySQL / Oracle / PG 三库都产出 `passed`（
  `mysql/rules.py:190`、`oracle/oracle_rules.py:189`、`postgresql/rules.py:120`）→ SQL Server 是唯一的例外。
- **可选项**：`rules.py` 补一条"未命中即登记 `passed`"的出口（判据不成立也产出 evaluation，`finding_id=None`）；
  或最省事：`report_adapter` 用规则包全集 ⊖ findings 反推 `passed` 条数，而不是只吐 triggered。
- **影响面**：会改 `analysis.json` 的规则评价集合，从而改报告附录表格 → **需重跑基线**。
- **实际采用的修法（§18）**：不选"adapter 反推"（那只是把 0 换成另一个理想数，仍不知道"为什么没判"），
  改为**在分析层真正产出四态**：规则包补 `rules` 目录（66 条，作为"通过"的分母）+
  `RuleEngine` 显式登记 `gap()`/`na()` + 按 `collection.module_status` 收口。
  实跑基线包得到 **触发 14 / 通过 33 / 未判断 6 / 不适用 13 = 66**，未判断与不适用各条都带得出成因而非拍脑袋计 0。

**② TOP SQL 的 `database_name` 在包内 30/30 全为 NULL。【待核 / 采集侧】**

- **实际行为**：四条 TOP SQL 查询都取 `DB_NAME(st.dbid) database_name`（ps1 L632 / L649 / L666 / L683），
  而基线包里 `top_queries*` 四张表的 `database_name` **全部为 `null`** → 报告「数据库」列整列显示"未采集"。
- **机制**：`sys.dm_exec_sql_text.dbid` 对 **ad-hoc / 即席批**返回 NULL，`DB_NAME(NULL)` 自然也是 NULL
  （采集器自己的探测语句正是 ad-hoc）。这不是采集器拼错字段，是取数口径的已知限制。
- **待核（按 AGENTS.md，不凭单包决定改什么）**：需在**第二/第三个有真实业务负载**的包上复核命中率 ——
  存储过程/参数化语句的 `st.dbid` 非 NULL，若那类占比高则影响有限。
- **可选项**：改用 `sys.dm_exec_plan_attributes(plan_handle)` 的 `dbid` 属性
  （`OUTER APPLY ... WHERE attribute='dbid'`）兜底，这是对 ad-hoc 计划的通行取法。
- **影响面**：属采集 SQL 变更 → 必须同步 `sqlserver-collector-interface.md`，且需重采验证。

---

## 16. 与两份老版 SQL Server 巡检脚本的对照（2026-10-08）

对照物（用户提供）：

| 文件 | 行数 | 说明 |
|---|---:|---|
| `DB mssqlcheck2014.sql` | 3955 | 与下面那份相似度 **0.991**，差异只有 `WITH (NOLOCK)` 与 `NVARCHAR→VARCHAR` |
| `MSSQLCheck2012-1.0.sql` | 3922 | **同一份脚本的年份变体** → 两份合起来算 **1 份**参照物 |
| `DB_SQLServer_HC_lhr_v7.0.0.1_2008R2.sql` | 2490 | 另一份（lhr 版） |

**所以是 2 份参照物，不是 3 份。**

### 16.1 前提：形态不同，只能比"检查项覆盖"和"取数口径"

它们是**库内即席脚本**（`#mssql_server` / `html` 表 → `bcp ... queryout .html` → `xp_cmdshell` 导出），
**不是采集器**：不产结构化包、没有"采集状态"概念、不供下游分析。所以：

- 能比的是：**检查项覆盖**（40 个 mssqlcheck 锚点 / 46 个 lhr 锚点）与**同一指标的取数口径**；
- 不能比的是：架构、状态语义、可复现性 —— 这些不是它们的职责。

判"哪些可学"用的仍是本项目的老规矩：**多脚本都出现的算交集（优先），单份出现的先记录不动手**。

### 16.2 检查项覆盖对照（判定：有 / 部分 / 无）

| 主题 | mssqlcheck2014 | lhr | 我的 v1.3.1 | 判定 |
|---|---|---|---|---|
| 实例/OS 信息 | `db_base_info`(33 行) + `db_start_config` | 同 + NUMA/在线CPU/并行阈值 | `instance` + `server_services` | **有**（我更细：socket/cores/NUMA） |
| 数据库清单 | `all_db_info` | + `db_real_size` / `db_all_size` | `databases` + `database_summary` | 部分（下表 A-2） |
| 文件与 IO | `all_db_FILE_info` / `_rw` | 同 | `database_files`（含延迟） | **有** |
| 磁盘空间 | — | `disk_free` | `volumes`（三层合并） | **有** |
| TempDB | `tmpdb_info` | `tmpdb_info` | `tempdb`（含累计 IO/延迟） | **有** |
| 对象级 | `table_no_pk` / `table_rows_top` | + `all_db_object` / `object_info` / `table_size_top` | `tables_without_pk` / `large_tables` | 部分（缺**对象计数**） |
| 存储过程级统计 | — | `proc_tj` | — | **无**（单份，记录） |
| 安全/用户 | `user_role_info` / `user_sysadmin_info` / `null_user` | `user_role_info` / `null_user` | `security`（登录/sysadmin/孤立用户） | 部分（缺 **DB 级角色成员** = G-19） |
| 错误日志 | `server_errors` / `_details` | 同 | `error_log_summary`（含轮转 #0..#9、中英双语） | **有**（我更全） |
| 锁/阻塞 | `lock_info` / `_DML` / `_wait` / `session_blocking` | 同 | `blocking` + `active_requests` | 部分（缺**锁对象级**） |
| 会话/请求 | `user_process` | 同 | `active_requests` / `connection_stats` / `session_activity` | **有** |
| SQL Top | `SQL_CACHE_TOP`（计划缓存）/ `SQL_CPU_TOP` / `SQL_Adhoc_TOP` | + `SQL_IO_TOP` / `SQL_RUNNING_TOP` | `top_queries` ×4 | 部分（**缺计划缓存**；`IO_TOP`≈我的高物理读，`RUNNING_TOP`≈我的活动请求） |
| 索引 | `index_info` / `missing_index` / `bad_index_info` | `missing_index` / `max_cost_index` | `missing_indexes` / `unused_indexes` / `fragmented_indexes` | **有** |
| 作业 | `job_details` / `_today` / `job_long` / `job_fail` | `job_details` / `job_long` / `job_fail` | `agent_jobs` / `failed_jobs` | 部分（缺**步骤明细**） |
| CPU/调度 | `cpu_pj` / `cpu_ut` | `cpu_pj` | `cpu_by_database` | **部分，且口径用错 → 见 A-1** |
| 等待 | — | `wait_stats` | `wait_stats` | **有** |
| 内存/缓冲池 | `db_Bufferpool` | `db_Bufferpool` | `buffer_pool` / `memory_status` / `memory_clerks` | **有** |
| 性能统计 | — | `performance_statistics` / `os_stats` | `performance_samples` | 部分（**无 CPU 时间序列**） |
| 备份 | `all_db_backup_info` | + `last_backup_db_info` / `no_backup_db` | `backup_status` / `no_backup_databases` / `backup_history` | **有** |
| 还原验证 | — | `db_restore_info` | — | **无** |
| 高可用 | `db_ha` / `ag_server` / `fci_server` / `mirroring_server` / `logship_info` / `pub_info` | `db_ha` / `mirroring_server` | `ha_status` / `availability_replicas` / `database_mirroring` / `log_shipping` / `replication` | 部分（缺 **AG 监听器 / FCI 节点 / 集群仲裁**） |
| 数据库对象计数 | `all_db_object` | `all_db_object` / `object_info` | — | **无** |

### 16.3 A 类 —— 老脚本的**取数口径是对的，我用错了**（最该学，只有一条根因）

**A-1 `sys.dm_exec_sql_text.dbid` 对 ad-hoc 计划恒为 NULL，我拿它当数据库名。**

- **我的写法**（`sqlserver_inspection.ps1` L792–803，`CPU按数据库分布`）：
  ```sql
  SELECT TOP (15) DB_NAME(st.dbid) database_name, SUM(qs.total_worker_time)/1000 total_cpu_ms, ...
  FROM sys.dm_exec_query_stats qs CROSS APPLY sys.dm_exec_sql_text(qs.sql_handle) st
  WHERE st.dbid IS NOT NULL AND DB_NAME(st.dbid) IS NOT NULL     -- ← 把 ad-hoc 全部丢掉
  GROUP BY st.dbid ORDER BY total_cpu_ms DESC
  ```
- **后果（实测）**：基线包里该项 `采集执行明细` 显示 **`成功 / 0 行`**，报告 5.x 章节**根本没有这张表**。
  同一个根因还打在另外四个模块上：`top_queries` / `_logical_reads` / `_executions` / `_physical_reads`
  的 `database_name` **30/30 全为 NULL**，报告「数据库」列整列"未采集"（§15.5 ②）。
  **一处口径错，两个模块出问题**。
- **老脚本的正确写法**（mssqlcheck L540–559，用于同一件事「CPU 按数据库分布」）：
  ```sql
  FROM sys.dm_exec_query_stats AS qs
  CROSS APPLY (SELECT CONVERT(INT, value) AS DatabaseID
               FROM sys.dm_exec_plan_attributes(qs.plan_handle)
               WHERE attribute = N'dbid') AS pa
  ...
  WHERE DatabaseID <> 32767      -- 排除 ResourceDB
  ```
  `dm_exec_plan_attributes(plan_handle)` 的 `dbid` 属性**对 ad-hoc 计划同样有效**（无库上下文时落 32767=ResourceDB）。
- **判断**：**这条直接学**，且它同时就是 §15.5 ② 的解法 —— 不是"猜的可行方案"，是**在别人生产脚本里跑了十年的写法**。
- **影响面**：改 `CPU按数据库分布`（1 处）+ 四个 TOP SQL（4 处）的取数口径；报告会从"0 行 / 整列未采集"变成有真实数据 →
  `sections`/`items` 可能变化，**需重跑基线**。属采集 SQL 变更 → 必须同步 `sqlserver-collector-interface.md`。

> **A-2（待核，非口径错）**：lhr 有 `db_all_size` / `db_real_size` 两个概念（分配大小 vs 实际占用）。
> 我的 `database_summary` 走 `sys.master_files`（文件分配大小）。是否要拆成"分配 / 已用"两列，
> 需先看基线包里 `dm_db_file_space_usage` 类型的字段是否够（我有 `tempdb` 用了它，用户库没采）。**记录待核**。

### 16.4 B 类 —— 真缺的检查项（两份都缺/单份但价值明确）

按价值排序，**"两份老脚本都有"** 的排前面：

| # | 项 | 来源 | 补它的理由 | 建议 |
|:--:|---|---|---|---|
| B-1 | `msdb.dbo.restorehistory` | lhr | **"备份存在" ≠ "备份可还原"**。我现在只有 `backupset`（备份文件），证明不了这台实例上真的还原验证过。这是备份章节唯一缺的一环 | **批 C** |
| B-2 | `sys.dm_tran_locks` JOIN `sys.dm_os_waiting_tasks` | 两份 | 实时**锁对象级**明细（资源类型/库/表/请求模式/等待时长）。我只有会话级阻塞链 | 批 D（瞬时 DMV，语义要设计） |
| B-3 | `sys.dm_os_schedulers` | lhr | `runnable_tasks_count` / `pending_disk_io_count` / `work_queue_count` = **CPU 压力最直接的现场证据**。我的 `performance_samples` 只有 Batch/TPS/PLE/Grants/Connections，**没有任何 CPU 时间序列** | **批 C**（加进采样循环，成本极低） |
| B-4 | `sys.database_role_members` | mssqlcheck | 数据库级角色成员 = 已登记缺口 **G-19**，此处获外部印证 | 批 D |
| B-5 | `sys.dm_exec_cached_plans` | 两份 | 计划缓存膨胀（Adhoc 占比 / 一次性计划）。我在 v1.3.0 采了 `optimize for ad hoc workloads` 这个**配置**，却**没有对应的现场证据** —— 正好配成一对 | 批 D |
| B-6 | `availability_group_listeners` | mssqlcheck | AG 的**客户端连接入口**（VIP:端口）。没有它，报告讲完 AG 配置，客户会问"那应用连哪" | 批 D |
| B-7 | `sys.dm_os_cluster_nodes` | 两份 | FCI 节点清单 + up/down + current owner；我只有 `IsClustered` 一个开关。**注意这是实例级 DMV**（返回目标实例所属集群），不存在 `os_metrics` 那种"执行机串数据"的问题 | 批 D |
| B-8 | `dm_hadr_cluster` | mssqlcheck | 集群名 + quorum 状态，AG 仲裁判断依据 | 批 D |
| B-9 | `msdb.dbo.sysjobsteps` | 两份 | 作业**步骤**明细（失败在哪一步、命令、重试次数）。我只有作业名 + 历史结果 | **批 C** |
| B-10 | `sys.dm_exec_procedure_stats` | lhr | **过程级** TOP。单份出现按规矩只记录，但客户是医院 HIS（存储过程为主），语句级 TOP 不如过程级贴业务 | 记录，等第二个 HIS 类客户包 |
| B-11 | `sys.dm_os_ring_buffers`(`RING_BUFFER_SCHEDULER_MONITOR`) | mssqlcheck | SQL 2019+ 的 CPU 使用率时间序列。解析 ring buffer XML 脆弱（列名随版本变）且与 B-3 重叠 | 记录 |
| B-12 | `dm_tcp_listener_states` | mssqlcheck | 监听 IP/端口/状态，多 IP 与防火墙场景有用 | 记录 |
| B-13 | `xp_fileexist` | 两份 | `ErrorLogsLocation` 指的文件是否真存在（路径配错 / 被清理） | 记录 |
| B-14 | `dm_os_memory_nodes` | lhr | NUMA **per-node** 内存分布（我有节点数，无分布） | 记录 |
| B-15 | 对象计数（`all_db_object` / `object_info`） | 两份 | 各库 表/视图/存储过程/触发器/索引 的个数。属"报告概览"性质，价值一般 | 记录 |
| B-16 | `sysjobservers` / `sysjobactivity` | mssqlcheck | MSX 多服务器作业 / 作业当前活动请求；只在多服务器环境有意义 | 记录不补 |

### 16.5 C 类 —— **明确不该学**（学了是倒退）

1. **`xp_cmdshell` + `bcp` 导出 HTML**：脚本结尾会 `sp_configure 'xp_cmdshell',1` + `RECONFIGURE WITH OVERRIDE`
   → `xp_cmdshell mkdir` / `bcp ... queryout` → 再关回去。**两个问题**：① 违反 AGENTS.md
   「采集器只采集事实、不在客户服务器上执行整改」；② `xp_cmdshell` 是客户安全基线必查项，
   巡检脚本自己把它打开就是制造风险（哪怕随后关掉，中间窗口 + 配置变更留痕都在）。
   我的**只读 + 结构化包**是正确形态，**不学**。
2. **老式兼容视图**：`sysdatabases` / `sysprocesses` / `sysobjects` / `syssessions`。2005 时代写法，
   字段少、无 schema 维度。我有 `sys.databases` / `dm_exec_sessions` / `sys.objects`。**不学**。
3. **`sys.dm_exec_query_plan` 取执行计划 XML**：体积大、无法进结构化包做分析 → 采了也没人读
   （AGENTS.md 明令禁止长期静默堆积）。**不学**。
4. **`xp_msver`**：我用 `dm_os_sys_info` + `dm_os_windows_info` + `physical_memory_kb`，更准更全。**不学**。
5. **`sp_configure`**：我用 `sys.configurations`（含 min/max/is_dynamic，报告第 14 章在用）。**不学**。
6. **`xp_regread`**：老脚本只用它读 `Software\Microsoft\Microsoft SQL Server\Instance Names\SQL`
   解析命名实例的注册表名 —— `SERVERPROPERTY('InstanceName')` 已能表达。**不学**。
7. **`fn_get_file_available` / `@FreeSpace` 选盘**：为找剩余空间最大的盘来写输出；
   我的 `volumes` 三层合并更完整。**不学**。

### 16.6 净结论

- **覆盖面我整体不弱、深度反超**：它们是把数十个 SELECT 的结果拼成 HTML 表；
  我是结构化包 + 62 条判据 + 报告模型。典型对比：**Always On** 我采
  `dm_hadr_database_replica_states`（redo queue / log send queue / secondary_lag_seconds / 副本级连接错误），
  它们只列 `dm_hadr_*` 的状态描述；**复制** 我会探分发库并取 7 天 log reader / distribution 历史，
  它们只列 publications。
- **它们有两处比我严谨**：① A-1 的 `plan_attributes` 取 dbid 口径（我只在 TOP SQL 里也踩了 `DB_NAME(st.dbid)`）；
  ② 备份章节带**还原验证**（B-1）。
- **最该学的一条不是"加检查项"，是 A-1 那个口径** —— 一个改动修两个模块，
  且是别人生产里跑了十年的写法，不是猜的。

### 16.7 建议批次

- **批 C（小、收益高）**：A-1 口径修正（`dm_exec_plan_attributes` 取 dbid + 排除 32767，覆盖 1+4 处）；
  B-3 `dm_os_schedulers` 进采样循环；B-1 `restorehistory` → 备份章节第 4 段「还原验证」；B-9 `sysjobsteps`。
- **批 D**：B-2 锁对象级（瞬时语义）；B-4 DB 级角色成员（G-19）；B-5 计划缓存；B-6 AG 监听器；
  B-7 FCI 节点；B-8 集群仲裁。
- **记录不实现**：B-10 ~ B-16（见上表"建议"列）。

---

## 17. 批 C 落地状态（v1.3.1 → v1.3.2，2026-10-08）

**范围**：§16.7 定的批 C 四项。批 A 解决"已采到的数据走到报告"，批 C 解决的是
"**采到的口径是错的 / 还缺几项现场证据**" —— 采集 SQL 与呈现层同时动。

| # | 项 | 前 | 后 |
|:--:|---|---|---|
| C-1 | 语句归属库的口径（A-1） | `DB_NAME(st.dbid)` 对 ad-hoc 恒 NULL；`cpu_by_database` 还自带 `IS NOT NULL` 过滤把 ad-hoc 全丢 | 改取 `sys.dm_exec_plan_attributes(plan_handle)` 的 `attribute=N'dbid'`；TOP SQL 用 `OUTER APPLY` + 32767 标名，`cpu_by_database` 用 `CROSS APPLY` + 排除 32767 |
| C-2 | CPU 调度压力进采样（B-3） | `performance_samples` 只有 Batch / TPS / PLE / Grants / Connections，**没有任何 CPU 时间序列** | 每个采样点加 `runnable_tasks_count` / `runnable_tasks_max` / `work_queue_count` / `pending_disk_io_count` / `current_tasks_count`（`scheduler_id<255`）；报告新增「CPU 调度压力采样」时间序列表，性能采样表补 3 行峰值 |
| C-3 | 还原验证（B-1） | 只有 `backupset`（备份**文件存在**） | 新增 `restore_history`（`msdb.dbo.restorehistory` 按库聚合：还原次数 / 最近还原 / 最近实际还原 / 最近仅验证）；报告「备份与恢复」新增「还原验证历史」 |
| C-4 | 作业步骤（B-9） | 只有作业名 + 整体历史结果 | 新增 `job_steps`（`msdb.dbo.sysjobsteps` TOP300，最近失败优先）；报告「作业与错误日志」新增「步骤明细」 |

采集器模块数 42 → **44**，顶层采集键 47 → **49**（+4 元数据键）；注释 81 → **101 行（5.7%）**；
`collector_version` 1.3.1 → **1.3.2**；`schema_version` 保持 1.1（只增键、未删改名，无破坏性变更）。

### 17.1 落地要点（换实现时最容易踩的四处）

- **C-1 是纯 SQL 口径修正，不改键名、不加列** → 下游零改动。但它**必须重新采集才生效**：
  既有基线包（v1.3.1 及更早采集的）里 `cpu_by_database` 仍是 `[]`、四个 TOP SQL 的
  `database_name` 仍是 `null` —— **"改了代码"≠"包里有数据"**，别拿老包验证跑到「没生效」的误判。
- **C-2 的峰值在老包上必须落 `None`，报告不加行**。`perf_summary` 的 4 个新键走 `_sched_peak()`，
  无数据时返回 `None`；`_build_sections` **只在非 None 时才 append 行**。
  「可运行任务 = 0」与「没采到」是两件事，补 0 就是 AGENTS.md 禁止的"把未知写成 0"。
- **C-3 / C-4 的新键在老包里不存在** → `s.get(key, [])` 为空 → 两张新表不出现（同样不编造结论）。
- **`job_steps.command_preview` 必须纳入 `-ExcludeSqlText`**：作业步骤命令里常见明文口令 / 连接串。
  实现上把原来"6 个键写死 `sql_text`"的循环改成 `键 -> 字段名` 的映射表（7 项）。
  这是新增采集项时最容易漏的一步 —— 采了敏感字段却不在抹除名单里。
- **`_inject_analysis` 的 `('ops','作业')` 收紧成 `('ops','作业清单')`**：否则新表「步骤明细」
  会被误挂 `AGENT.JOB_DISABLED` / `AGENT.JOB_OVERDUE` 的分析框（那是作业级判据，已在「作业清单」上出现过）。
  教训：**往已有章节里加条目时，先看 `_inject_analysis` 是按子串匹配标题的，新标题可能被旧规则误命中。**

### 17.2 证据（可复跑）

| 检查 | 结果 |
|---|---|
| PowerShell 解析（`Language.Parser::ParseFile`） | `PARSE_OK tokens=6752` |
| ps1 形态 | **1762 行 / UTF-8 BOM + 纯 LF / `collector_version='1.3.2'`** |
| 新增 `_coll` 名字能被采集端解析 | `tests/test_sqlserver_module_names.py` **7 passed** |
| 呈现层形态 | `presentation.py` 保持**纯 CRLF**（951/951） |
| 报告模型对**老包**的输出 | `tests/test_sqlserver_analyzer.py` **passed** —— `expected_summary` 未变（sections 13 / items 30 / charts 9）→ **无需刷新基线** |
| 新链路是否真的接通 | `_work/_verify_pres_c.py` **12 项 ALL_PASS**：新包三张新表都出现；老包三张都不出现、且性能采样表无 CPU 行 |
| 全量回归 | **5 failed / 203 passed / 34 subtests passed**，5 项与改前**逐条同名**（MySQL 夹具 `mysql_inspection_v1_db01_...tar.gz` 缺失）→ **零新增回归** |

### 17.3 未做 / 待现场验证

- **顺带发现（非本批引入，登记待核）**：做演示时，基线包（`collector_version = 1.1.0`，外部实现产物）
  的 `buffer_pool.cache_pct` 5 行合计 **800%**（单行 227.04% / 183.13% …），反推其分母约为
  真实总页数的 **1/8** —— 该老包的占比算错了。**当前仓库脚本的公式是正确的**
  （`COUNT_BIG(*)*100.0/NULLIF(SUM(COUNT_BIG(*)) OVER(),0)`，窗口聚合在 GROUP BY 之后求值），
  所以这是历史包数据缺陷，不是现网缺陷。
  但**呈现层对 `>100%` 的占比目前原样渲染**，读者会当成报告 bug → 下一个有真实负载的新包核对：
  新包占比正常 → 只需在呈现层对老包加脚注；新包也超 100% → 说明公式在真实实例上另有问题，需现场复现。
- **C-1 的 SQL 无法在本机执行验证**（本机无 SQL Server 实例、无 sqlcmd）。目前只有三层间接证据：
  ① PowerShell 解析通过；② 写法来自别人生产跑了十年的脚本（mssqlcheck L540–559）；
  ③ 呈现侧已用合成快照证明能正确渲染。**下次真实采集时重点核对**：
  `cpu_by_database` 是否非空、四个 TOP SQL 的 `database_name` 是否非空、是否出现 `ResourceDB` 行。
- **批 D 六项未做**：B-2 锁对象级（瞬时语义要设计）/ B-4 DB 级角色成员（G-19）/ B-5 计划缓存 /
  B-6 AG 监听器 / B-7 FCI 节点 / B-8 集群仲裁。
- **B-10 ~ B-16 按 §16.4 只记录、不实现。**
- ~~**§15.5 ①（附录「规则执行状态」三行恒 0）仍未修**：那是分析层缺口（`report_adapter.py` 的
  `evaluations` 只由 findings 反推、每条写死 `status="triggered"`），改它会动附录 → 需重跑基线，
  且主题与批 C 的"采集口径"不同，单独成批更清楚。~~ → **已单独成批落地，见 §18**（2026-10-08）。

---

## 18. 附录「规则执行状态」四态收口（v1.3.2 分析层，2026-10-08）

**范围**：只修 §15.5 ①。**采集器一字未动**（`sqlserver_inspection.ps1` 仍是 v1.3.2 / 1762 行），
改动全在分析层与报告层。批 A 修的是"采到的数据被渲染层断链"、批 C 修的是"采到的口径是错的"，
这一批修的是第三类：**分析层只产出"风险"，不产出"通过 / 不适用"的完整评价**。

### 18.1 根因：不是渲染层写错，是引擎没有"分母"

附录那张表由 `inspection_core/word_engine.py` 按 `evaluations` 里各状态的**条数**填：

| 状态 | 报告表里的行 | 含义 |
|---|---|---|
| `triggered` | 触发 | 形成风险项 |
| `passed` | 通过 | 现有证据未触发阈值 |
| `not_evaluated` | 未判断 | 证据不足，不作通过结论 |
| `not_applicable` | 不适用 | 当前架构或环境不适用 |

而 SQL Server 侧的 `evaluations` **只由 findings 反推**（`report_adapter.py` L219–234），
每条写死 `status="triggered"` —— 规则引擎（`rules.py` 的 `RuleEngine`）全文只有 `self.add(...)`，
**命中才产出**，未命中的规则根本不进集合。于是那张表**结构上只可能填出「触发 N / 通过 0 / 未判断 0 / 不适用 0」**。

MySQL / Oracle / PG 三库走声明式引擎（`inspection_core/declarative_rules.py`），天然四态 → **SQL Server 是唯一例外**。
所以问题不在 Word，也不在 adapter 的计数写法，在于**"通过"没有分母、"不适用"没有出口**。

### 18.2 修法（三层，缺一层都不成立）

1. **规则包补 `rules` 目录（分母）** —— `plugins/sqlserver/inspection_rules.json` 新增 `rules` 段 **66 条**
   （60 条静态 + 5 条 `SEC.*` 动态 + 1 条 `HA.REPLICATION_AGENT`），每条含
   `category` / `severity` / `title` / `required_modules`。`schema_version` 1.0 → **1.1**；
   既有 47 条 `thresholds` **一条未丢**。severity 分布 `high 26 / medium 22 / critical 8 / low 8 / info 2`。
2. **`plugins/sqlserver/module_catalog.py`（新建）= 快照键 / 规则 → 采集模块的唯一真值来源** ——
   `LEGACY_MODULE_ALIASES`（旧名回退）+ `MODULE_BY_SNAPSHOT_KEY`（49 项）+ 三组状态集合 +
   `module_status_map()` / `lookup_status()` / `classify_module()` / `known_module_names()`。
   规则层与测试共用同一份，不再各存一份别名表。
3. **`RuleEngine` 产出四态** —— 新增 `gap()`（缺输入）/ `na()`（不适用）两个**登记出口**（触发优先，`setdefault`），
   在原本"判据不成立就静默跳过"的地方补 **13 处登记点**（其中两处循环分别覆盖复制 4 条 / AG 5 条）；
   `evaluate()` 收尾调 `rule_evaluations()`，按下面的顺序给每条规则定态：

   > **已登记（触发 / 缺输入 / 不适用）→ 模块 `unavailable` → 未判断 → 模块 `not_applicable` → 不适用 → 通过**

   判"输入在不在"**只能看 `collection.module_status`，不能看快照里有没有这个键** ——
   `normalize_snapshot` 对 30+ 个键 `setdefault(key, [])`，"这一版没采"与"采到了但结果为空"形状完全相同。
4. **接线** —— `rule_provider.evaluate()` 把 `engine.rule_evaluations()` 存进 `self.evaluations`
   → `analyzer` 写进 `analysis.json` 顶层 `rule_evaluations` 与报告模型
   （`presentation.build_model(..., evaluations=...)`，**新参数带默认值，向后兼容**）
   → `report_adapter._build_appendix` **优先读 `source["rule_evaluations"]`**；
   旧模型（无此键）回退到原来的"findings 反推"行为，历史模型不抛错。

### 18.3 模块状态 → 三态判定表

| `collection.module_status` | 判定 | 理由 |
|---|---|---|
| `success` / `success_no_data` | **可判**（进"通过 / 触发"） | 采到就是采到，空结果也是采到 |
| `failed` / `error` / `permission_denied` / `timeout` / `partial` | **未判断** | 想采没采到 |
| `skipped` / `unsupported` / `not_applicable` / `not_enabled` | **不适用** | 有意不采 / 本实例不支持 |
| 模块整版不存在（`classify_module(None)`） | **未判断** | 输入没有 ≠ 通过 |

### 18.4 实跑基线包的四态（可复跑）

`_work/probe_rule_eval.py` 跑基线包（`collector_version = 1.1.0`，外部实现产物）：

```
规则目录条数: 66
引擎产出 findings = 24
求值条数 = 66   四态 = {triggered: 14, passed: 33, not_evaluated: 6, not_applicable: 13}
校验：四态之和 == 目录条数 ? True ；findings 的 rule_id 集合 ⊆ triggered ? True
```

`_work/demo_rules.py` 端到端（`analyze.py` → `generate_report.py`）后读回 Word 附录表：

| 状态 | 数量 | 含义 |
|---|---:|---|
| 触发 | 14 | 形成风险项 |
| 通过 | 33 | 现有证据未触发阈值 |
| 未判断 | 6 | 证据不足，不作通过结论 |
| 不适用 | 13 | 当前架构或环境不适用 |

**未判断 6 条逐条成因**（都是"老包确实没采这一项"，不是判据写错）：

| rule_id | 成因 |
|---|---|
| `SESSION.RESOURCE_HOG` | 老包无「会话资源占用」模块 |
| `INSTANCE.SERVICE_STARTUP` | 老包无「SQL Server 服务与启动参数」模块 |
| `PERF.STALE_STATS` | 老包无「陈旧统计信息（深度）」模块 |
| `PERF.INDEX_FRAGMENTATION` | 老包无「索引碎片（深度）」模块 |
| `SEC.ORPHANED_USER` | 老包无「孤立用户（深度）」模块 |
| `INSTANCE.BLOCKED_PROC_THRESHOLD` | 老包 `configurations` 白名单 16 项不含 `blocked process threshold (s)` |

**不适用 13 条**：Always On 5（未部署）/ 数据库镜像 1 / 日志传送 1 / 复制 4（老包这四个模块均 `success` + 0 行）/
规则包未要求备份压缩、备份加密 2（`require_backup_compression` / `require_backup_encryption` 默认 `false`）。

### 18.5 落地要点（换实现时最容易踩的四处）

- **"通过"的分母必须来自规则包目录，不能由 findings 反推**。反推只能得到"触发几条"，
  永远不知道"规则一共几条、其余各是什么状态"——那正是原来那张表恒 0 的原因。
- **`normalize_snapshot` 的 `setdefault(key, [])` 让"没采"与"采到空"同形** → 判输入可用性一律走
  `collection.module_status`。这是 AGENTS.md「未采集 ≠ 0/正常」在代码里的唯一落点。
- **旧名回退必须有**：模块名经过一次漂移（`无主键表（深度）` / `大表统计（深度）` / `复制配置` → 去后缀 / 改名），
  老采集包里仍是旧名，无 `LEGACY_MODULE_ALIASES` 会把"能判的"误判成"未判断"。
- **adapter 要留向后兼容分支**：`report_model.json` 无 `rule_evaluations` 键时必须回退，
  否则历史模型一进报告层就抛错（`build_model` 的新参数同理，必须带默认值）。

### 18.6 证据（可复跑）

| 检查 | 结果 |
|---|---|
| 规则包形态 | `inspection_rules.json` **17564 字节 / `rules` 66 条 / `thresholds` 47 条（未丢）/ `schema_version` 1.1** |
| 新守门测试 | `tests/test_sqlserver_rule_catalog.py` **12 passed / 66 subtests passed**（含"空快照不得判出通过"） |
| 定向回归 | `pytest -k sqlserver` **16 passed / 192 deselected** |
| 端到端 Word 表 | 附录「规则执行状态」= 触发 14 / 通过 33 / 未判断 6 / 不适用 13 |
| 基线同步 | `baseline.json` 的 `expected_summary` 四态入册（`rules:66 / triggered:14 / passed:33 / not_evaluated:6 / not_applicable:13`）；其余六项（health_score 0 / collection_quality 100.0 / sections 13 / items 30 / charts 8 / risk_register 24 / severity_counts）**未动**；`refresh_notes` 追加 15 条 |
| 全量回归 | **5 failed / 215 passed / 100 subtests passed**，5 项与改前逐条同名（MySQL 夹具缺失的预存在失败）→ **零新增回归** |

### 18.7 未做

- **批 D 六项仍未动**：B-2 锁对象级 / B-4 DB 级角色成员（G-19）/ B-5 计划缓存 / B-6 AG 监听器 /
  B-7 FCI 节点 / B-8 集群仲裁。
- **`buffer_pool.cache_pct > 100%` 的呈现处理**：老包 5 行合计 800%（见 §17.3），
  等下一个有真实负载的新包定口径后再决定"加脚注"还是"改公式"。
- **C-1 的采集 SQL 仍待现场验证**（同 §17.3）：本机无 SQL Server 实例，目前只有三层间接证据。

---

## 19. 真机首跑崩溃修复（采集器 v1.3.2 → v1.3.3，2026-10-08）

**这一批的起点是用户真机首跑采集器的现场输出 —— 脚本 100% 崩溃、零产出：**

```
PS C:\Users\Administrator> C:\Users\Administrator\Desktop\sqlserver_inspection.ps1
[2026-09-10 03:45:33][INFO] Collector 1.3.2 started for .
C:\Users\Administrator\Desktop\sqlserver_inspection.ps1 : 在此对象上找不到属性"connection_ip"。请确认该属性存在。
所在位置 行:1 字符: 1
+ C:\Users\Administrator\Desktop\sqlserver_inspection.ps1
+ ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    + CategoryInfo          : NotSpecified: (:) [sqlserver_inspection.ps1], PropertyNotFoundException
    + FullyQualifiedErrorId : PropertyNotFoundStrict,sqlserver_inspection.ps1
```

批 A（§15）修的是"采到的数据被渲染层断链"、批 C（§17）修的是"采到的口径是错的"、
§18 修的是"分析层不产出四态" —— 这一批是第四类：**采集器根本跑不起来**。
下面两个缺陷一个 P0（崩溃）、一个 P1（环境相关的隐藏崩溃），都由本批新建的
**等价冒烟**（抽出真实代码段在 StrictMode 下跑）抓出并钉死。

### 19.1 P0：三个"不存在的列名" → 整脚本崩溃

`PropertyNotFoundStrict` 的错误信息**不含变量名**（只说"找不到属性 connection_ip"，不说对象是谁），
所以根因要三步反推：

| 步 | 事实 | 证据 |
|---|---|---|
| ① | 阶段 ③ 实例查询写了 `w.windows_version` / `w.language` / `w.os_language` **三个列** | 源码 |
| ② | `sys.dm_os_windows_info` **一个都没有** —— 官方列自 2008 R2 SP1 起至今恒为 `windows_release` / `windows_service_pack_level` / `windows_sku` / `os_language_version` | 官方文档（含 2008 R2 归档页、2014 页）逐页核对；外部实现包 `snapshot.instance` 只有 `os_version`，**没有** `os_language` / `os_locale_language` |
| ③ | 整条 SQL `Invalid column name` → 被 catch 吞（**且该 catch 当时不写日志**）→ `$snapshot.instance` 落成空 `[ordered]@{}` → 下一行 `$snapshot.instance.connection_ip` 在 StrictMode 下抛异常 → 脚本终止、无包 | 见下「静默吞异常的放大器效应」 |

**放大器效应（本批最重要的结论）**：一个模块失败能升级成"整脚本崩溃"，
是因为**"静默吞异常" + "裸点取属性"同时存在** —— 前者藏住原因，后者把后果放大到全局。
两者单独看都只是坏味道，合在一起就是 P0。

### 19.2 P1：单元素数组被拆包 + StrictMode 下 `.Count` 抛异常

**由本批的等价冒烟抓出，且是 R3/R4 改造自己引入的回归。**

两个实测行为（PS 5.1，见 `_work/count_probe.txt`）：

| 行为 | 探针结果 |
|---|---|
| `return $value` 走管道，**单元素数组被拆包成标量** | 1 元素 → `isArray=False`（`PSCustomObject`）；2 元素 → `isArray=True` |
| StrictMode 下对 PSCustomObject 取 `.Count` 抛异常 | `[pscustomobject]@{name='C:'}.Count` → `PropertyNotFoundStrict` |

后果：**机器上恰好只有 1 块盘 / 1 张网卡就崩，2 块盘反而正常**。
（裸点取 `$last.disks` 走属性访问、不拆包；改成语义更严的 `Get-ObjProp $last 'disks' @()` 反而丢了数组性。）

### 19.3 修法（R1–R6）

| 编号 | 改动 | 位置（v1.3.3 行号） |
|---|---|---|
| R1 | 实例查询列名改回官方三列 + 6 行「改这里先核文档」注释 | `w.windows_release` L366 |
| R2 | 实例查询 catch 补 `Write-CollectorLog "实例基础信息 failed: ..." 'ERROR'` | L387 |
| R3/R4 | 连接 IP/端口、目录命名、本机判定全部改走 `Get-ObjProp` / `Set-ObjProp` | L393-408 |
| R5 | 新增 `Get-ObjProp` / `Set-ObjProp` 两个严格模式安全访问器 | L221 / L239 |
| **R6** | **新增第三个访问器 `Get-ObjArray`（恒返回数组），`$lastDisks` / `$lastNet` / `$osDisks` 三处改用** | L248 / L1740-1743 |
| — | 下游：`product_version`、安全块 `row_count`、Markdown 实例块、存储卷 `free_pct`、OS 指标段全部改走访问器 | L455 / L1028 / L1583 / L1718 / L1724-1763 |

版本 bump `1.3.2 → 1.3.3`（`$collectorVersion` L133 + `-Help` 文案），
并在 `Set-StrictMode` 上方补 8 行契约注释。**`schema_version` 保持 `1.1`**：
§3 的键类型表不枚举 `instance` 子键，删掉的是从未进入契约的列。

### 19.4 证据（全部可复跑）

| # | 证据 | 结果 |
|:---:|---|---|
| ① | StrictMode 行为探针 `_work/strict_probe.txt` / `null_probe.txt` / `helper_probe.txt` / `psobj_probe.txt` | 缺失键 / PSCustomObject 缺属性 / `$null` 取属性，三者都抛 `PropertyNotFoundStrict` |
| ② | **数组拆包 + `.Count` 探针** `_work/count_probe.txt` | 1 元素数组出标量；PSCustomObject 的 `.Count` 抛异常 |
| ③ | **等价冒烟** `_work/_smoke_strict.ps1` → `_work/smoke_strict.txt`，**7 用例全绿** | A 空 instance（**当初的崩溃输入**）/ B 正常实例 / C 采样点缺全部条件键 / **D 恰 1 盘 1 网卡** / E OS 模块失败兜底 / F 真实 SQL 行的 instance / G 多盘多网卡 |
| ④ | **反向验证（两轮）** | ① 把 `w.windows_release` 换回 `w.windows_version` → 1 failed；② 把 `$lastDisks` 换回 `Get-ObjProp ... @()` → **冒烟恰好在用例 D 抛 `Count`、用例 G 照常通过**，且两条静态守门同时失败 → 证明证据有牙 |
| ⑤ | 全脚本 SQL 标识符审计 | 53 个 `sys.*` + 12 个 `msdb.dbo.*` + 7 个 DMV 函数调用，除已修三列外无其他拼写缺陷；`os_language` / `os_locale_language` **全仓零消费** |
| ⑥ | 解析校验 | AST `PARSE_OK`，`PARSE_ERRORS=0`、`TOKENS=7139`、UTF-8 BOM + 纯 LF（1856 行 / 109465 B） |
| ⑦ | 新增守门测试 | `tests/test_sqlserver_collector_strict_safety.py` **9 passed**（新增 3 条：数组访问器存在且保数组 / 三个数组型字段走 `Get-ObjArray` / 不许对 `Get-ObjProp` 结果取 `.Count`） |
| ⑧ | 全量回归 | **5 failed / 224 passed / 100 subtests passed**，5 项与改前逐条同名（MySQL 夹具缺失的预存在失败）→ **零新增回归** |

### 19.5 换实现时最容易踩的四处

1. **`PropertyNotFoundStrict` 不含变量名** → 排查口诀：**"某处崩溃 + 错误里只有属性名" ⇒ 先怀疑"上一步把某个对象留空了"**，
   而不是去改那一行的写法。本次的"上一步"就是阶段 ③ 被吞掉的实例查询。
2. **静默吞异常必须先修留痕，再修根因** —— 否则你连"哪条 SQL 错了"都看不到（控制台当时只有 `started` 一行）。
3. **`Set-StrictMode -Version Latest` 下 `.Count` 不是万能安全的**，且 `Get-ObjProp` 会拆包单元素数组 ——
   两者相乘即"只有 1 块盘就崩"。数组型字段一律走 `Get-ObjArray`。
4. **冒烟要抽"真实代码段"而不是手抄一份等价逻辑** —— 本批的 P1 正是抽真实代码段才抓到的；
   手抄会把引入回归的那次改写一并"抄对"，从而漏掉。

### 19.6 未做

- **批 D 六项**（B-2 锁对象级 / B-4 DB 级角色成员 G-19 / B-5 计划缓存 / B-6 AG 监听器 / B-7 FCI 节点 / B-8 集群仲裁）仍未动。
- **`buffer_pool.cache_pct > 100%` 呈现口径**：等有真实负载的新包再定（同 §17.3 / §18.7）。
- **C-1 采集 SQL 仍待现场验证**：本机无 SQL Server 实例，只有 §16 的三层间接证据。
- **`presentation.py` 首行中文 docstring 丢失**（批 A 遗留，见 §15）。
- **`_smoke_strict.ps1` 的打桩仍需手工跟随主脚本变化**（`Add-MdH2` / `Split-Path -Leaf` 各漏过一次）——
  若后续再出现"桩跟不上"的情况，考虑改成"只抽被调用的顶层函数 + 用 Pester 风格 mock"。

---

## 20. 真机首跑暴露的另外三个 SQL 缺陷（采集器 v1.3.3 → v1.3.4，2026-10-08）

### 20.1 背景

v1.3.3 修掉"整脚本崩溃"之后**包能产出了**（53 个顶层键、46 个模块、`inspection_report.md` 正常），
但 `collection.module_status` 里 **3 个模块 `failed`**，且都是 SQL 被 `Invoke-CollectionModule` 的 catch 吞下来的。

| 模块 | 报错（原样） | 根因 |
|---|---|---|
| 会话资源占用 | 列名 `'physical_reads'` 无效 | `sys.dm_exec_sessions` **没有这一列** |
| SQL Server 服务与启动参数 | 列名 `'servicetype'` 无效 | `sys.dm_server_services` **没有这一列**（且同一 SELECT 里还跟着一个同样不存在的 `version`）|
| 复制拓扑 | 关键字 `'table'` 附近有语法错误 | `DECLARE @dist sysname, @blocks table(...)` —— 表变量不能与普通变量合并声明 |

### 20.2 逐条根因（官方文档已核）

**① `dm_exec_sessions.physical_reads` 不存在，而 `reads` 本身就是物理读。**
官方 `sys.dm_exec_sessions` 列清单里没有 `physical_reads`，但有
`reads`（描述原文：*Number of physical reads performed, by requests in this session, during this session*）。
原 SQL 同时写了 `SUM(es.reads) total_reads` 与 `SUM(es.physical_reads) total_physical_reads` ——
**同一个量写了两遍，其中一遍的名字是错的**。所以这不算"少采了一个指标"，只是命名错误。

**② `dm_server_services.servicetype` / `.version` 都不存在。**
官方列只有：`servicename` / `startup_type` / `startup_type_desc` / `status` / `status_desc` / `process_id` /
`last_startup_time` / `service_account` / `filename` / `is_clustered` / `cluster_nodename` /
`instant_file_initialization_enabled`。**SQL Server 只报第一个无效列名**，所以只看到 `servicetype` 的人
把第一个改完就会立刻撞上 `version` —— 这是"修一个冒一个"的典型。

**③ `DECLARE @a sysname, @b table(...)` 是语法错误。**
T-SQL 的表变量声明是**独立的语法分支**，不能与标量变量写进同一条 `DECLARE`。
定位证据不需要外部文档：该模块里 `table` 关键字只出现三处 —— `DROP TABLE #repl` / `CREATE TABLE #repl`（合法）
与这一行，报错位置唯一。

> **为什么 §9 那轮"列名审计"没抓到**：那次审计覆盖的是 **DMV 对象名**（确认 `MSpublications` 在分发库不在 msdb 等），
> 没有逐列比对；而第三条是**语法级**错误，任何"列名对照"式审计都看不见它。
> 结论：**列名与语法都要变成可执行的守门，不能靠"记得核对"。**

### 20.3 修法

| # | 位置（v1.3.4 行号） | 改动 |
|---|---|---|
| S1 | `session_activity` `L704-718` | 删掉 `SUM(es.physical_reads) total_physical_reads` 与重复的 `SUM(es.reads) total_reads`，合并为 `SUM(es.reads) total_physical_reads`（**输出列名不变**，下游两个消费点继续可用）|
| S2 | `server_services` `L846-850` | 删掉 `servicetype` 与 `version` 两列 |
| S3 | `presentation.py` 服务表 | 同步从 7 列收为 6 列（去掉"类型"表头）—— 采集端删了列，呈现端不同步就会永远输出一个空列 |
| S4 | `replication` `L1157-1158` | 拆成 `DECLARE @dist sysname;` + `DECLARE @blocks table(...);`，并补 3 行注释说明为什么不能合并 |
| S5 | 守门 | `tests/test_sqlserver_collector_strict_safety.py` 新增两条（DMV 陷阱列名黑名单 **别名感知** + `DECLARE` 混合声明检测），两条都先剥 SQL 的 `--` 行注释（脚本里刻意记录了"曾经写错什么"）|

**`schema_version` 不 bump**：删掉的三列从未进入 interface §3 的键类型表，`session_activity` 的输出列名集合也没变。

### 20.4 证据（全部可复跑）

| # | 证据 | 实测 |
|---|---|---|
| ① | **反向验证** | `python _work/_revcheck2.py break` 退回三处修复 → 两条新守门失败，命中项精确为 `dm_exec_sessions.physical_reads（命中 'es.physical_reads'）`、`dm_server_services.servicetype`、**`dm_server_services.version`**（隐藏雷也被抓到）、`DECLARE @dist sysname, @blocks table(...)`；`restore` 后全绿 |
| ② | 守门测试 | `test_sqlserver_collector_strict_safety.py` **11 passed**（+2 条）|
| ③ | 全量回归 | **5 failed / 226 passed / 100 subtests passed**，5 项与改前逐条同名 → **零新增回归** |
| ④ | 呈现层列数验证 | 用真机包 + 官方列名注入两行服务记录，跑 `presentation._build_sections`：**服务表 6 列 / 会话表 7 列 / OS 表 2 列 / 物理盘 6 列，headers 与每行长度全部一致**（`_work/_verify_svc_render.py`）|
| ⑤ | 解析与编码 | AST `PARSE_ERRORS=0`、`TOKENS=7139`、UTF-8 BOM + 纯 LF、**1859 行 / 143 行注释（7.7%）** |

### 20.5 附带结论：采集耗时的 95% 在两段采样循环（v1.3.4 未改，属设计）

真机包 `collection.module_status` 的 `elapsed_ms` 合计 **73,532 ms**，其中：

| 模块 | elapsed_ms | 说明 |
|---|---|---|
| OS 指标 | **44,625** | 6 个采样点 |
| 性能采样 | **25,125** | 6 个采样点 |
| 其余 44 个模块合计 | ≈ 3,800 | 最慢的 `错误日志摘要` 328ms |

拆解 44.6s：**25s 是纯 `Start-Sleep`**（5 × 5s）＋ 约 **10s 一次性 WMI provider 冷启动** ＋ 约 10s 例行 CIM 调用。
冷启动实测（`_work/_wmi_bench.ps1` → `wmi_bench.txt`，本机 PS 5.1）：

| 操作 | 耗时 |
|---|---|
| `PerfOS_Processor` ×5（首次，provider 冷）| 8,620 ms |
| 同上（第二次，provider 热）| 1,432 ms |
| 同上（改用 `-Filter` 服务端过滤）| 1,396 ms |
| 同上（复用 `CimSession` + `-Filter`）| 1,409 ms |
| `Get-PhysicalDisk` ×1（首次，Storage 模块）| 2,501 ms |
| `Get-PhysicalDisk` ×1（第二次）| 40 ms |

**两个反直觉结论**（本来打算"优化"，实测后放弃）：
1. **"过滤下推"与"会话复用"没有收益** —— 1432 / 1396 / 1409 ms 三者无差别，不值得改代码；
2. 真正的固定开销是 **provider 冷启动**（约 10s），它每次进程只付一次，**缩短采样点也省不掉**。

→ 想快只能减采样：`-SampleCount 3 -SampleIntervalSeconds 2`（采样段从 50s 降到约 4s）。
**`-SampleCount 1` 不能用**：`Win32_PerfFormattedData_*` 是"两次采样间隔"计算的格式化类，
真机首点 `cpu_pct=0` 而 `cpu_idle_pct=98`（本应 2%）；且性能采样的 `batch_requests_per_sec` 需要至少两点做差分。

### 20.6 换实现时最容易踩的三处

1. **DMV 列名不要凭记忆写** —— 尤其"看起来该有"的列（`servicetype`、`version`、`physical_reads`）。
   `dm_exec_sessions.reads` 已经是物理读，别再找 `physical_reads`。
2. **一条 SELECT 报错只报第一个无效列名** —— 修完要**整段重核**，别只改报出来的那一个（本批 `version` 就是这么藏着的）。
3. **T-SQL 里表变量必须单独 `DECLARE`** —— 与标量变量合并声明是语法错误，而这类错误**任何列名审计都抓不到**。

### 20.7 未做

- **`os_metrics.samples[0]` 首点失真**：`presentation.py` 把 6 个点全部渲染，未标注/丢弃首点（已在 interface §9-⑱ 留记，待决策）。
- **批 D 六项**与其余遗留同 §19.6。
- **C-1 采集 SQL 仍待现场验证**：本机无 SQL Server 实例（同 §19.6）。

---

## 21. 产物路径回显（采集器 v1.3.4 → v1.3.5，2026-10-08）

### 21.1 触发：用户问"为什么包不在脚本同目录"

真机跑出 v1.3.3 的包之后，用户的第一个问题是"为什么他采集后的包，不是和我的采集脚本在同一个目录"。

排查结论：**这不是缺陷，是 v1.3.0 刻意修的 P2（§9 · F-21）** ——
v1.2.0 的写法就是 `[string]$OutputDir = $PSScriptRoot`（脚本同目录），
修的依据是 AGENTS.md「采集脚本不得依赖自身所在目录；输出位置一律由 `--output-dir` 或默认值决定」，
而立法理由就写在同一句的括号里：**「这是"脚本可以在仓库里任意摆放"的前提」**。

也就是说：这条规矩防的是**开发机上产物落进仓库**；用户的现场工作流是"脚本离机部署到目标机"，
那时脚本根本不在仓库里 —— 规矩要保护的对象不存在，代价（RDP 会话下多一层 `\2\`、
得进 `AppData\Local\Temp\2\` 里翻）却照样付了。

### 21.2 决定：维持 `%TEMP%`，只把路径回显做显眼

给用户的三个候选（改回 `$PSScriptRoot` / 改成当前目录 / 维持 `%TEMP%`）里，用户选了**维持 `%TEMP%`**。
理由成立：规矩不该为一次不便推翻，而"找不到包"可以用提示解决。

### 21.3 改了什么

| # | 改动 | 位置（v1.3.5 行号） |
|---|---|---|
| ① | `-Help` 的 `-OutputDir` 说明由 1 行扩成 3 行：指出 RDP / 多会话机器会带 `\2\` `\3\` 段，并指向示例 | L105-107 |
| ② | `-Help` 示例补一条 `-OutputDir "$env:USERPROFILE\Desktop"` | L121 |
| ③ | 跑完在 `OUTPUT_DIR=` / `OUTPUT_PACKAGE=` **之后**加人类提示框（5 行） | L1854-1860 |
| ④ | `-Help` 的 here-string 由**双引号**改**单引号**（`@"` → `@'`） | L93 / L123 |

④ 是本批自己踩的坑：写成双引号 here-string 时，`$env:USERPROFILE` 会在**运行脚本那台机器上**被展开，
`-Help` 于是打出 `C:\Users\wangjs\Desktop`（开发机的用户名）；改单引号后原样显示，用户复制即可用。
已固化成 `interface` §10-13。

⚠ ③ 的位置有约束：`OUTPUT_DIR=` / `OUTPUT_PACKAGE=` 是**机器可读**的两行（流水线按前缀取值），
人类提示必须排在其**后**，且不得改动这两行的格式。

**默认输出目录不变**（`%TEMP%\sqlserver_inspection`），`schema_version` 不变
（`-Help` 文案与 stdout 提示都不进 `snapshot.json`）。

### 21.4 为什么不改默认值（完整推演）

| 候选 | 实际行为 | 否掉的理由 |
|---|---|---|
| 缺省 = `$PSScriptRoot`（= v1.2.0 原写法） | 脚本放桌面 → 包落桌面 | 直接推翻 F-21；脚本在仓库 `inspection\` 下被执行时包落进仓库，`git status` 立刻变脏（只能靠 `.gitignore` 兜） |
| 缺省 = `Get-Location`（当前目录） | 用户 `cd` 到哪就落哪 | 现场最常见的两种调用（双击运行、RDP 里敲完整路径）的 CWD 分别是 `C:\Windows\System32` 与 `C:\Users\Administrator` —— 比 `%TEMP%` 更难找 |
| **维持 `%TEMP%`（采纳）** | 现状；`-OutputDir` 可显式覆盖 | 不改规矩；用路径回显 + `-Help` 示例把"找不到"的成本降下来 |
| （附带观察）现有产物形态 | 每次跑在 OutputDir 下留**目录 + zip 两份**（`Compress-Archive` 之后不清理） | 在 `%TEMP%` 下无害（系统会清）；**若要改默认位置必须先治理这一条**，否则桌面会逐次变脏 |

### 21.5 证据（全部可复跑）

| # | 检查 | 结果 |
|---|---|---|
| ① | `-Help` 实跑 | `_work/_help_out.txt` —— v1.3.5 文案完整、`$env:USERPROFILE` 原样显示、示例行不再折行 |
| ② | 结尾回显块等价冒烟 | `_work/_echo_probe.txt` —— `-NoPackage:$false` → zip / `$true` → 目录，两用例全绿 |
| ③ | AST | `PARSE_ERRORS=0`、`TOKENS=7178`、UTF-8 BOM + 纯 LF（1875 行 / 110926 B） |
| ④ | 全量 pytest | **5 failed / 226 passed / 100 subtests**，5 项与改前逐条同名（MySQL 夹具缺失的预存在失败）→ 零新增回归 |

**取证方式（可复跑）**：`_work/_mk_probes.py` 从主脚本**按标记抽取**生成两个探针，不手抄 ——

- `_help_sandbox.ps1`：主脚本副本，**只**把 `-Help` 块里的 `exit 0` 换成 `return`。
  为什么需要：`& script.ps1` 时脚本内的 `exit` 会终止整个 runspace，stdout 无法被同一会话里的
  `Out-File` 捕获；`return` 在脚本顶层等价于"结束脚本"但不杀宿主。
- `_echo_probe.ps1`：抽取 `$finalArtifact = $packageRoot` 到最后一个提示 `Write-Host` 之间的
  **真实代码行**，在 `Set-StrictMode -Version Latest` 下跑两个 `$NoPackage` 用例。
  两个产物都保持 BOM + 纯 LF，与主脚本契约一致。

### 21.6 未做

- **默认输出目录不动**（用户定）。
- **`Compress-Archive` 成功后不清理未压缩目录** —— 现有行为在 `%TEMP%` 下无害；
  只有改默认位置时才会变成问题，届时与本条一起做。
- 批 D 六项与 C-1 现场验证同 §20.7。


---

## 22. 分析层 P0 批：判据误报三条（2026-10-08）

### 22.1 为什么有这一批

真机包（采集器 v1.3.5，46 个采集模块）跑完整链路后 `health.score = 0`、`critical = 9`。
逐条回溯 findings 的真实性后发现：**9 个 critical 里只有 1 个是真的**（业务库 `huangjinjing`
FULL 恢复模式、从未完整备份、日志亦无备份），其余 8 个分属两类系统性缺陷 ——
把"依赖服务停止"当成"实例停机"，以及"同一件事被两条规则各报一次"叠加"系统库噪音混入 critical"。

本批只修判据语义：**不动采集端、不动 Word 引擎、不动图表**。

### 22.2 P0-1　服务规则不区分引擎与依赖服务

**原行为**（`plugins/sqlserver/rules.py` 的 `_services()`）：

```python
for svc in s.get("server_services", []):
    status = str(svc.get("status_desc")).upper()
    if status and status != "RUNNING":
        self.add("INSTANCE.SERVICE_STARTUP", ..., "SQL Server 服务未处于运行状态", "critical", name, ...)
```

`sys.dm_server_services` 会同时返回引擎、SQL Server 代理、全文筛选器启动器、浏览器若干行，
而循环**不区分角色**。

**实测数据**：

| 服务 | startup_type | status | 旧行为 |
|---|---|---|---|
| SQL Server (MSSQLSERVER) | Automatic | Running | 不报（正确） |
| SQL Server 代理 (MSSQLSERVER) | Manual | **Stopped** | ❌ critical「实例不可用」 |
| SQL Full-text Filter Daemon Launcher | Manual | Running | ❌ medium「启动类型非自动」 |

两个误报的成因都是基础事实被忽略：**SQL Server 代理的出厂默认启动类型就是 Manual**，
**Manual 也是全文服务的标准配置**；代理停止只影响作业调度，与实例可用性无关。

**修法**：

- 新增 `_is_engine_service()`，**首选 `filename` 是否含 `sqlservr.exe`**
  （引擎 `sqlservr.exe` / 代理 `SQLAGENT.EXE` / 全文 `fdlauncher.exe` / 浏览器 `sqlbrowser.exe`
  互斥且唯一）；`filename` 未采到时按服务名回退 —— 依赖服务名必含
  `代理 / agent / full-text / launcher / browser / ssis` 之一，引擎名以 `SQL Server (` 开头。
- 只有**引擎**停止才报 critical「SQL Server 服务未处于运行状态」；
- **代理**停止且 `agent_jobs` 非空时降为 medium（"代理未运行 → 作业不会执行"）；
  实例没有作业时报了等于噪音，故不报；
- 其余依赖服务停止、以及**依赖服务的 Manual 启动类型**一律不判（无证据表明实例启用了该功能）。

### 22.3 P0-2　`BACKUP.FULL` 与 `BACKUP.NO_BACKUP` 重复计数

两条规则都覆盖"从未备份"：`backups()` 的 `hours is None or hours > full_limit`，
与 `no_backup_databases()` 的整表遍历。实测 4 个库（`huangjinjing` / `master` / `model` / `msdb`）
在 `backups` 与 `no_backup_databases` 里**库名完全相同**，于是同一件事报出 8 条 critical。

**修法**：

- `BACKUP.FULL` 只管"**有记录但超期**"（`hours is not None and hours > full_limit`）；
- "无记录"统一由 `BACKUP.NO_BACKUP` 出口；
- **兜底**：某库不在 `no_backup_databases` 清单里时（两模块口径不一致、或该模块没采到），
  `BACKUP.FULL` 仍然报 —— 不能拿"去重"换"漏报"；
- `no_backup_databases()` 在清单为空且模块未采到时 `gap()` 登记未判断，
  否则"没采到"会被读成"没有库从未备份"。

### 22.4 P0-3　备份 / 完整性 / 文件增长等规则缺系统库守卫

整份 `rules.py` 里唯一的系统库排除在 `DB.QUERY_STORE` 一处**硬编码**
（`name.lower() not in {"master","model","msdb","tempdb"}`），其余同类规则都没有。
本次真机包有 **19 条 finding 落在 master/model/msdb 上，其中 6 条是 critical**。

**修法（配置驱动，不做全局兜底）** —— 规则包新增段：

```json
"system_database_guard": {
  "databases": ["master", "model", "msdb"],
  "severity_cap": "low",
  "applies_to": ["BACKUP.FULL", "BACKUP.NO_BACKUP", "BACKUP.LOG", "DB.CHECKDB",
                 "DB.CHAINING", "FILE.PERCENT_GROWTH", "DB.TRUSTWORTHY"]
}
```

- 由 `RuleEngine.add()` 统一施加 —— 它是 finding 的**唯一出口**，放在这里就不存在"某个规则漏改"；
- `object_name` 支持两种形状：纯库名，以及 `库名/对象名`（`FILE.PERCENT_GROWTH` 写的是 `msdb/MSDBData`）；
- **只降不升**，且**只对 `applies_to` 列出的规则生效** —— `MEM.CACHE_IMBALANCE`（msdb 占缓冲池
  过高）反映的是业务库缓冲不足，是真实问题，不能因为对象名是系统库就降级；
- `tempdb` 不在此列：它由既有的 `excluded_databases` **整条不评价**，两者语义不同；
- 顺带把 `DB.QUERY_STORE` 的硬编码列表改为 `system_database_guard ∪ excluded_databases`
  （行为完全等价，系统库清单收敛到规则包一个来源）。

### 22.5 效果（真机包 v1.3.5，46 模块）

| | 改前 | 改后 |
|---|---:|---:|
| findings | 35 | 30 |
| critical | 9 | **1** |
| high | 6 | 4 |
| medium | 18 | 9 |
| low | 2 | 16 |

改后唯一的 critical 是**业务库 `huangjinjing` 从未备份** —— 正是本该被突出、
却被 8 条噪音淹没的那条。

### 22.6 验证

- **守门测试**：`tests/test_sqlserver_rule_semantics.py`（**19 条**，纯构造快照、不依赖真机包；
  覆盖引擎/依赖服务分流、备份去重与兜底、系统库降级范围与只降不升、规则包清单本身）。
- **反向验证**：逐一破坏三处修复 → 分别触发 **4 / 2 / 1** 条失败；恢复后**逐字节一致**且 19 全绿。
- **全量回归**：**5 failed / 245 passed**（5 项与改前逐条同名，均为 MySQL 侧预存在）→ **零新增回归**。
- **基线刷新**：`baselines/sqlserver/current/baseline.json` 的 `expected_summary`
  `health_score 0 → 6`、`risk_register 24 → 20`、`severity_counts 8/5/10/1 → 1/4/7/8`、
  `triggered 14 → 13`、`passed 33 → 34`；`collection_quality / sections / items / charts` 四项未变。
  该基线包是外部实现 collector **1.1.0**，没有「SQL Server 服务与启动参数」模块，
  故 P0-1 对它无影响，变化全部来自 P0-2 与 P0-3（已写进 `refresh_notes`）。

### 22.7 未做

- **健康分算法**：`metrics.py` 的 `score()` 是 `max(0, 100 - Σweight)`，**线性叠加、无下界**。
  真机包改后扣分 = 1×18 + 4×10 + 9×4 + 16×1 = **110** → 仍是 0 分，
  **修复的效果在报告首页看不出来**。该口径与 MySQL 相同（MySQL 也是 `max(0, 100 - Σ)`），
  要改需先定口径，列为下一批候选。
- **图表链路**：绕过共享层 `inspection_core.charts`（永远 Pillow 手绘）、未剔首采样点、
  双量级同轴、`os_metrics` 采样有表无图、SQL Server 是四库中唯一不产 `llm_input.json` 的 —— 全部未动。
- **`04_risk_distribution`**：`baseline.json` 的 `removed_charts` 已记录该图是**有意移除**
  （与风险登记册重复）；`report_adapter.py` 里遗留的 `chart_id == "04_risk_distribution"` 特判
  已成死代码，待清理（纯净化，无功能影响）。
- **`excluded_databases` 守卫不统一**：`DB.CHECKDB` / `DB.RCSI` 用了它（排除 tempdb），
  而 `DB.CHAINING` / `DB.TRUSTWORTHY` / `DB.AUTO_OPTIONS` / `DB.PAGE_VERIFY` 没用 ——
  于是 `tempdb` 的 `DB_CHAINING`（默认 ON）仍会报 medium。属原有不一致，待评估。

详见 `docs/sqlserver-analysis-review.md`（分析层复查清单：P0×3 已修 / P1×5 / P2×3）。

## 23. 分析层报告详细度批：检查项结论 / 口径说明 / 扣分口径 / 待确认事项（2026-10-08）

**触发**：用户对照 MySQL 现场报告（`192.168.1.11-EMR MYSQL数据库巡检报告.docx`）指出
「SQL Server 出的报告没有 MySQL 详细」。对照分析见 `docs/sqlserver-report-detail-gap.md`。

**结论先行**：**不是采集缺口、不是 Word 引擎问题** —— 同一个共享引擎、同一个 `professional`
布局。差距全在 SQL Server 呈现层「没给检查项写结论与口径」，外加适配器把新字段丢掉。

### 23.1 落地四条

| 档 | 内容 | 落点 |
|:---|:---|:---|
| P0-1 | 60 条 `(section_id, title) → 分析结论` 注册项，每条带实测数字与口径边界 | 新增 `plugins/sqlserver/analysis_text.py` |
| P0-2 | 每条注册项同时产出 `display.note`；自动补「正文展示前 N 条，共 M 条」截断提示 | 同上 |
| P0-3 | `SEVERITY_WEIGHTS` 提为单一来源；模型补 `counts` + `scoring_policy`；适配器透传 | `metrics.py` / `presentation.py` / `report_adapter.py` |
| P1-2 | 「待客户确认事项」模板表（30 条规则）+ `pending_confirmations()`，按已触发规则派生 | `analysis_text.py` + adapter 透传 |

### 23.2 共享层唯一改动：critical 必须单独一档

`word_engine.build_summary_v31` 原先恒把 `critical` 并入 `high` 参与扣分算式：

```python
high_n = (counts.get("high") or 0) + (counts.get("critical") or 0)
deduction = high_n * p_high + ...
```

MySQL 的 severity 体系里没有 critical，所以一直没问题；**SQL Server 的 critical 权重是 18、
high 是 10**，合并会让首页印出的算式与实际分数对不上 —— 而首页那段话的全部价值就是
「读者能拿它把分数核对一遍」。改为：`policy` 里有 `critical` 键就单独计，没有则退回合并。
MySQL 路径输出**逐字不变**（`tests/test_word_engine.py` 的 hash 校验通过即为证）。

### 23.3 新根因：链路断在适配器（第二次出现）

`report_adapter.adapt_report_model()` 是**重建**模型而不是透传。它自己拼 `health_assessment`，
只取 `severity_counts`，于是：

- 分析层刚写的 `scoring_policy` 被丢弃 → 首页「结论速览」**仍然不出现**；
- `pending_confirmations` 根本没被读取 → 第 19 章仍是空章。

第一次出报告时这两个症状都观察到了（标题在、内容不在），逐层回溯才定位到适配器。
与 §18 那条「附录规则执行状态三行恒 0」（adapter 由 findings 反推 evaluations）**同类**：
适配器不认识的键被静默丢弃，分析层写得再对也到不了报告。

→ 新增守门测试 `AdapterPassthroughTests`（透传 + 「缺字段必须保持 None」）。

### 23.4 落地实测（同一份真机包）

| 指标 | MySQL 参考件 | 改前 | 改后 |
|:---|---:|---:|---:|
| 正文文字量 | 5,090 | 3,070 | **7,848**（+156%） |
| 表格文字量 | 62,081 | 23,654 | **30,375** |
| 总文本量 | 67,171 | 26,724 | **38,223**（+43%） |

总文本量仍低于 MySQL 是**结构性**的：MySQL 那份是三节点集群，风险登记册 / 整改计划 /
证据索引都按节点数放大；按单节点折算约 22,390 字，**低于** SQL Server 单实例的 38,223 字。

### 23.5 验证

- 新增 `tests/test_sqlserver_report_text.py`：20 项 / 30 subtests。四类断言 ——
  **静态结构对账**（`presentation.py` 声明的检查项 ⇄ 文案注册表，双向无缺无余；
  用 AST 提取，不依赖运行数据，所以没被走到的分支也参与对账）；
  **运行时覆盖**（真包每个渲染项都要有结论 + 依据 + 口径说明，且不得出现旧兜底套话
  `不单独形成风险判断`）；**口径一致**（`scoring_policy` 必须等于 `SEVERITY_WEIGHTS`）；
  **待确认清单**（模板规则号须真实存在 / 技术性整改项不得进表 / 同规则去重 / 无 findings 给空列表）。
- **反向验证** 13 处破坏全部被抓住、文件逐字节复原（`_work/_revcheck_text.py`）。
  其中「适配器不透传」一处**初次未被抓住** —— 暴露了测试盲区，据此补了 `AdapterPassthroughTests`。
- 全量 pytest **5 failed / 265 passed / 130 subtests**，5 项与改前逐条同名 → 零新增回归。
- 首页实测输出可逐项核对：`以 100 分为基线，按严重 1 项、高风险 4 项、中风险 9 项、
  低风险 16 项分别扣 18、10、4、1 分，合计扣 110 分，扣分已触及 0 分下限 ——
  该分数表示风险项数量超出评分区间，不代表系统当前不可用。`

### 23.6 未做

- **P1-1** 单实例「高风险项速览」：`word_engine.py` 要求 finding 带 `node` 键（多实例才注入），
  单实例直接跳过 —— 共享层改动，影响四个库。
- 图表未统一到 `inspection_core.charts`（SQL Server 仍 Pillow 手绘、未剔首采样点）。
- 命中规则的检查项，其结论被 findings 文案覆盖，口径信息只留在 `display.note`。
  当前是刻意设计（避免「结论说正常、风险册说有风险」），可考虑改「风险陈述 + 口径补充」两段式。

### 23.7 文件换行契约补充

`plugins/sqlserver/analysis_text.py` 为 **LF**；`metrics.py` / `presentation.py` /
`report_adapter.py`（LF）/ `inspection_rules.json`（CRLF）维持原状。
本轮所有对 CRLF 文件的改动都走「归一 LF 匹配 → 转回 CRLF 写出 → 断言 `裸LF==0` 且无 `\r\r\n`」，
`report_adapter.py` 与 `analysis_text.py` 是 LF、可直接编辑。
