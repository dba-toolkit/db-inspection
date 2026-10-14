# oracle_inspection.sh 结构与对接契约

- 对象：`inspection/oracle_inspection.sh`（**3586 行 / 87 个函数 / 注释密度 4.7%**）
- 配套改造清单：`docs/oracle-collector-fix-plan.md`（编号 `OF-NN`，本文引用它）
- 参照契约：`docs/mysql-collector-interface.md`（MySQL 侧已稳定的同构文档）
- **下游唯一对接依据**：Python 分析层只按本文取值；本文与脚本不一致时，**以脚本为准并立刻改本文**。
- 审查日期：2026-10-09。行号基准同上，找不到时**以符号名（函数名 / item_id / 字符串字面量）为准**。

---

## 1. 定位与规模

| 项 | 值 |
|---|---|
| 只做 | 环境检测 → 只读采集 → 时序采样 → 脱敏 → 状态记录 → 打包 |
| 不做 | 风险判断、评分、拓扑合并、图表、Word（全部交 Python） |
| 支持环境 | 单机 / RAC / ASM / CDB(PDB) / ADG，11g–23ai |
| 依赖命令 | `sqlplus`（必需）；`sar`/`sadf`/`lsnrctl`/`crsctl`/`ocrcheck`/`dgmgrl`/`rman`/`opatch`/`timeout`/`mktemp`/`stat`（可选，缺则相应项降级） |
| 行数 / 函数数 / 注释密度 | 3586 / 87 / **4.7%**（MySQL 版 15.8%） |
| 版本号 | **三处不一致**（文件头 v1.5、两个 json 里 1.4、`schema_version` 硬编码 1.0）→ OF-21 |
| 名义采集项 | **186**：`system.static` 20 + `system.history` 1 + `timeseries` 1 + `module.*` 27 + `sql.*` 136 + `package.security_scan` 1 |
| 实际落盘状态文件 | **远少于 186**（item_id 塌陷，OF-01）→ 这是本文档最需要先知道的一条 |

**只采集不判定**这句必须落到代码上：Oracle 有两个地方越界了，属有意为之但要在 INTERFACE 里写明——`collect_health_summary`（L2990-3174）会自己算"表空间 >80% / 无效对象数 / 长事务数 / 锁等待数"并输出 ✅/⚠️/🔴。**这是报告文案层的判定，不是采集器该做的事**，但因为它只写 Markdown、不进结构化包，所以 Python 不受影响。**保留，别改**；但新增的任何判定都不要走这条路。

---

## 2. 先回答"乱不乱"

**骨架不乱，边界乱。**

### 2.1 骨架（值得保留）

- 函数命名有约定：`collect_*` 采集、`detect_*` 环境探测、`get_*`/`ora_*` 执行封装、`generate_*` 打包产物、`_trap_cleanup`/`run_module`/`start_module_bg`/`wait_background_modules` 运行时骨架。
- 运行时设施齐全：模块开关（`--include`/`--exclude`）、后台并行、逐项状态登记、磁盘空间预检、信号处理、打包前安全扫描、manifest（大小 + SHA256）。
- 与 MySQL 版**同源**：`sanitize_*`、`json_escape`/`json_quote`、`sha256_file`/`file_size_bytes`、`classify_failure`、`compute_sar_coverage`、`collect_system_static` 都能一一对上。

### 2.2 边界（四处错位）

| # | 错位 | 证据 | 后果 |
|:---:|---|---|---|
| 1 | **OS 采集两套并存**：`collect_sys_info`（md 版）与 `collect_system_static`（结构化版）职责重叠、口径不一 | 磁盘 `df -HT` vs `df -PT`；`lsblk -d -o name,rota` vs `lsblk -b -o 10 列`；limits / lsnrctl / ORACLE_HOME 只在 md | 同一事实两处数值不同；md 独有的三项 Python 永远拿不到 → OF-15 **✅ 批 2 已收敛**（md 6 段改指引，三项补成结构化） |
| 2 | **item_id 由中文标题派生** | `item_id="sql.$(sanitize_filename "$title")"`（L536）、`record_status "module.${module_name}"`（L758 等） | 48 项塌陷到一个文件，整份状态台账失真 → OF-01 |
| 3 | **category 不成体系** | 取值只有 `system.static` / `system.history` / `timeseries` / `sql` / `module` / `package` | `sql` 与 `module` 是 catch-all（136 项和 27 项挤在两个桶里），MySQL 是 `mysql.basic`/`mysql.capacity`/`mysql.replication`… 按域分区。adapter 无法按 category 分区，只能按 item_id 前缀（而 item_id 又是坏的）→ OF-01 + OF-23 |
| 4 | **工作目录与产物目录混用** | `status/`、`timeseries/`、`history/` 建在 `filepath`（与 TASK_DIR **同级**），打包时才拷进 TASK_DIR；没有 `.tmp/` | 汇总文件落在包外（OF-16）；临时文件在失败分支残留进包（OF-26） |

### 2.3 注释：**难点零星注释了，接口一行没写**

170 行注释 / 3586 行 = 4.7%。逐条读过这些注释后可以给的结论：

- 有"为什么"的注释集中在**排障踩坑**处，质量不低：`# 过滤连接串中可能包含密码的 sqlplus 行`（L870）、`# 限制宽列显示宽度，避免 VARCHAR2(4000) 列撑爆输出`（L557）、`# XML 解析无结果，回退到文本格式扫描`（L1865）、`# 使用 listagg 可能在某些版本失败，加 fallback`（L727）。
- **一行注释都没有的，恰恰是"改一处影响全部 item"的四个底层函数**：`ora_sql_to_md`（支配 136 项）、`record_status`、`capture_command`、`classify_failure`；以及 `detect_*` 六个探测函数和全部 `collect_*` 模块入口。
- 结论：**读者（或下一个 AI 会话）会把这些零星注释当成"已核实记录"来读**，而接口层是空白 → 注释撒谎比没注释更坏。→ OF-24（批 0，零逻辑改动，最先做）

---

## 3. 执行阶段地图

```
parse_args / load_config
  → 交互式/OS 认证密码准备（L3509 起）
  → 定位 sqlplus（L3534 起，找不到则交互式询问路径，`exit 10`）
  → 主机标识与环境探测 detect_env（串行，6 个 detect_*：basic/rac/asm/adg/cdb/pdb）
  → 连接测试（L3619 起，失败 `exit 20`）
  → check_output_space → 建 status/timeseries/history 目录 → 写报告头
  ┌ 串行 run_module ────────────────────────────────────────────────┐
  │ 1  生成AWR性能报告      collect_awr_report        ← 默认空跑（许可开关）  │
  │ 2  采集系统信息          collect_sys_info          ← 只写 Markdown        │
  │ 3  采集系统静态信息      collect_system_static     ← 结构化 OS（20 项）    │
  │ 4  采集Oracle基础信息    collect_oracle_info                               │
  │ 5  采集PDB容器信息       collect_pdb                                       │
  │ 6  采集表空间信息        collect_tablespace                                │
  │ 7  采集ASM磁盘组         collect_asm                ← HAS_ASM=0 时跳过     │
  │ 8  采集归档日志          collect_archive                                   │
  │ 9  采集会话与锁          collect_session_lock                              │
  │ 10 采集性能诊断          collect_performance                               │
  │ 11 采集索引信息          collect_index                                     │
  │ 12 采集ADG状态           collect_adg                                       │
  │ 13 采集DG Broker         collect_dg_broker                                 │
  │ 14 采集闪回数据库        collect_flashback                                 │
  │ 15 采集RAC专项           collect_rac                ← IS_RAC=0 时跳过      │
  │ 16 采集RAC高可用         collect_rac_ha             ← IS_RAC=0 时跳过      │
  │ 17 采集RMAN备份信息      collect_rman                                      │
  │ 18 采集控制文件与SPFILE  collect_control_spfile                            │
  │ 19 采集审计与安全        collect_audit_security                            │
  │ 20 采集AWR与统计信息     collect_awr_stats                                 │
  │ 21 采集故障诊断          collect_diag_top                                  │
  │ 22 生产级完整性验证      collect_production_validation                     │
  │ 23 扫描告警日志          collect_alert_log                                 │
  └────────────────────────────────────────────────────────────────┘
  ┌ 后台并行（start_module_bg，与上面 4-23 同时跑）─────────────────┐
  │     采集SAR历史          collect_sar_history                               │
  │     实时时序采样         collect_oracle_realtime_samples  (~30s sleep)     │
  └────────────────────────────────────────────────────────────────┘
  → wait_background_modules
  → 24 健康检查总结     collect_health_summary        ← 写 Markdown（含判定文案）
  → 25 结构化打包       collect_structured_package    ← 拷目录 + snapshot + manifest + 安全扫描 + tar
  → 统计耗时与状态 → 写 _status_summary_*.txt（**包外**）→ 打印
```

**注意三点**：

1. **后台模块的状态是"两层各一条"，不要按 `item_id` 去重成一条**：`wait_background_modules` 只在子 shell **连状态都没写**（被 kill / 崩在 `record_status` 之前）时补写**模块级**兜底记录；`collect_sar_history` / `collect_oracle_realtime_samples` 写的是**数据级**的 `system.sar_history` / `timeseries.realtime_sampling`。两者 id 不同、语义不同（"模块跑没跑完" vs "数据采到多少"），都保留。
   ★ **批 7/N-16：归档模块是个例外 —— 它只有 `oracle.module.structured_packaging` 一条。** 旧版 `collect_structured_package` 内部还会另写一条 `module.structured_packaging`（不合 `oracle.module.*` 约定），与 `run_module` 那条**重复**；现已收敛为单一记账户。
2. **`collect_sys_info` 是唯一只写 Markdown、不产任何结构化项的模块**（OF-15）—— **✅ 批 2 已改**：该函数 6 段改为"指向结构化包"，新采的 `system.oracle_env` / `system.listener_status` 等已进包。
3. **dry-run 列表（L3402-3427）漏了模块 3**，且顺序与主流程不一致 → OF-25。**读阶段地图请以本节为准，不要以 dry-run 输出为准。**

---

## 4. 采集项总清单

### 4.1 结构化 OS 项（`category = system.static`，**31 项** = 20 项与 MySQL 对齐 + 11 项 Oracle 专属）

| item_id | 输出文件 | 内容 | 备注 |
|---|---|---|---|
| `system.os_release` | `evidence/os_release.txt` | `/etc/os-release` + `uname -a` + `uptime` | 一致 |
| `system.time_status` | `evidence/time_status.txt` | local_time / utc_time / timezone / epoch_seconds | ✅ **已解析进 snapshot `time_evidence`**（批 2 / OF-07） |
| `system.timedatectl` | `evidence/timedatectl.txt` | `timedatectl status` | 一致 |
| `system.chronyc_tracking` | `evidence/chronyc_tracking.txt` | `chronyc tracking` | ✅ **15s 超时**（OF-08） |
| `system.chronyc_sources` | `evidence/chronyc_sources.txt` | `chronyc sources -v` | ✅ **15s 超时**（OF-08） |
| `system.ntpq_peers` | `evidence/ntpq_peers.txt` | `ntpq -pn` ⚠️ **仅当无 `chronyc` 时采集**；有 chronyc 时记 `skipped`（N-14 互斥清单） | 一致 |
| `system.lscpu` | `evidence/lscpu.txt` | CPU 拓扑 | 一致 |
| `system.free` | `evidence/memory_snapshot.txt` | `free -b`（**列对齐文本，不是 TAB 表格**） | ★ 批 6/N-8 改落点 |
| `system.filesystems` | `evidence/filesystems.txt` | `df -PT`（POSIX、1024 进制；**列对齐文本**） | ★ 批 6/N-8 改落点 |
| `system.inodes` | `evidence/inodes.txt` | `df -Pi`（**列对齐文本**） | ★ 批 6/N-8 改落点 |
| `system.block_devices` | `evidence/block_devices.txt` | `lsblk -b -o NAME,KNAME,TYPE,SIZE,FSTYPE,MOUNTPOINT,ROTA,SCHED,MODEL,SERIAL`（**含空格的值无法无损 TSV 化**） | ★ 批 6/N-8 改落点 |
| `system.mounts` | `evidence/mounts.txt` | `mount` | 一致 |
| `system.ip_address` | `evidence/ip_address.txt` | `ip -details addr show`（或 `ifconfig -a`） | 一致 |
| `system.ip_route` | `evidence/ip_route.txt` | `ip route show table all` | 一致 |
| `system.socket_summary` | `evidence/socket_summary.txt` | `ss -s` | 一致 |
| `system.numa` | `evidence/numa.txt` | `numactl --hardware` | ✅ **10s 超时**（OF-08） |
| `system.sysctl_selected` | `tables/kernel_parameters.tsv` | 13 个键（`vm.swappiness` … `net.ipv4.tcp_max_syn_backlog`） | 一致 |
| `system.hugepages` | `tables/hugepages.tsv` | `path\tvalue`：THP enabled / defrag / `nr_hugepages` **+ `/proc/meminfo` 大页实测值**（HugePages_Total / Free / Rsvd / Hugepagesize / AnonHugePages / Hugetlb） | ✅ 批 2 已扩（OF-09） |
| `system.dmesg_errors` | `evidence/dmesg_errors.txt` | `dmesg --level=err,crit,alert,emerg` | ✅ **20s 超时**（OF-08） |
| `system.ifconfig` | `evidence/ip_address.txt` | `ifconfig -a` ⚠️ **仅当无 `ip` 时采集**；有 `ip` 时记 `skipped`（N-14 互斥清单） | 一致 |

**Oracle 专属、MySQL 没有的（11 项，批 2 + 批 1）**：

| item_id | 输出文件 | 内容 | 来源 |
|---|---|---|---|
| `system.ipcs_shm` | `evidence/ipcs_shm.txt` | `ipcs -lm` / `-ls` / `-lu`（SysV SHM 与信号量余量） | OF-09 |
| `system.asm_multipath` | `evidence/asm_multipath.txt` | `multipath -ll` + `lsmod`(oracleasm/oracleaci) + `oracleasm listdisks` + `/dev/oracleasm/disks` | OF-09 |
| `system.oratab` | `evidence/oratab.txt` | `/etc/oratab`（OS 层 SID/ORACLE_HOME 登记） | OF-09 |
| `system.net_error_counters` | `evidence/net_error_counters.txt` | `ip -s link`（RAC 私网 drops/errors） | OF-09 |
| `system.oracle_processes` | `evidence/oracle_processes.txt` | `ps -eo pid,ppid,user,etimes,%cpu,%mem,args`，过滤 `ora_`/`asm_`/`tnslsnr` | OF-12（对齐 MySQL `system.mysql_processes`） |
| `system.oracle_limits` | `tables/oracle_limits.tsv` | 4 列 `source_file / scope / limit / value`，合并 `limits.conf` + `limits.d/*.conf`（字典序） | OF-11（对齐 MySQL `mycnf_allowlist`） |
| `system.oracle_actual_limits` | `evidence/oracle_actual_limits.txt` | 采集会话 `ulimit -a` + `/proc/<ora_pmon_pid>/limits` | OF-11 |
| `system.oracle_env` | `tables/oracle_env.tsv` | `variable\tvalue`：ORACLE_HOME / ORACLE_BASE / ORACLE_SID / GRID_HOME / TNS_ADMIN | OF-15 |
| `system.oracle_env_full` | `evidence/oracle_env_full.txt` | 同上 + `LD_LIBRARY_PATH` / `PATH`（**可能含客户路径**，仅落 evidence） | OF-15 |
| `system.listener_status` | `evidence/listener_status.txt` | `lsnrctl status`（**10s 超时**，避免 hung 拖死整包） | OF-15 |
| `system.capabilities` | `tables/capabilities.tsv` | 能力探测表（命令可用性 + 4 个视图可用性），`capability\tvalue` | **OF-13（批 1 新增）** |

> **注意**：`system.oracle_env` / `system.listener_status` 原先**只在 Markdown 报告里**，Python 拿不到；批 2 起进结构化包。
> `module.capability_probe` 是 `collect_capabilities` 的模块级 status（同一次执行产生 `system.capabilities` 这一项）。

**`tables/capabilities.tsv` 契约（OF-13）**：`capability<TAB>value`，表头 `capability/value`。
布尔统一 **0/1**；字符串类（`sqlplus_bin` / `sqlplus_version` / `oracle_home`）原样写。
覆盖键：`sqlplus_bin` / `sqlplus_version` / `oracle_home` / `diagnostics_pack` / `is_local_target` /
`sadf_command` / `sar_command` / `chronyc_command` / `ntpq_command` / `lsnrctl_command` / `crsctl_command` /
`dgmgrl_command` / `rman_command` / `opatch_command` / `multipath_command` / `oracleasm_command` /
`ipcs_command` / `sqlplus_command` / **`ip_command`** / **`ifconfig_command`** / `has_v_asm_diskgroup` /
`has_v_pdbs` / `has_v_diag_alert_ext` / `has_dba_advisor_findings`。
**Python 只读 `snapshot.json.capabilities`**（同一事实两处不同类型会踩坑，见 MySQL F-16）：json 里布尔是
`true/false`、字符串原样；tsv 仅供人工核对。

> ★ **`sqlplus_version` 是版本号而不是整行**（批 6 / N-9）：取值口径为 `sqlplus -V 2>&1 | sed -n 's/.*Release \([0-9][0-9.]*\).*/\1/p'`
> —— 即 `19.16.0.0.0`。旧写法 `2>/dev/null | head -1 | cut` 在输出走 stderr 的版本上取到**空串**（实测 19c 包即如此），
> 且可能把 `Usage` 提示塞进表里。取不到时为空串（**不是** `-`）。

**互斥采集项清单（N-14）** —— 下列 item_id 是**二选一**的备选实现，未采用的那一支会显式登记 `status=skipped`
（`reason` = `… mutually-exclusive fallback`），下游据此区分"环境不适用"与"该采却没采到"：

| 主选 | 备选（未采用时记 `skipped`） | 判定依据 |
|---|---|---|
| `system.ip_address` / `system.ip_route`（`ip`） | `system.ifconfig` | `ip_command` = 1 |
| `system.chronyc_tracking` / `system.chronyc_sources`（`chronyc`） | `system.ntpq_peers` | `chronyc_command` = 1 |

> 两支**都不存在**时，`system.ntpq_peers` 记 `unsupported`（reason: `neither chronyc nor ntpq installed`），
> `system.ip_address` 记 `unsupported`（reason: `ip/ifconfig not installed`）。**任何情况下这三个 id 至少有一个会出现在台账里**
> —— 不会再出现"整个 id 凭空消失、下游无从判断"的情况（批 6 之前 `system.ntpq_peers` 在装有 chrony 的主机上确实永不出现）。

### 4.2 历史与时序

| item_id | category | 输出 | 现状 |
|---|---|---|---|
| `system.sar_history` | `system.history` | `history/sar_{cpu,memory,swap,load,disk,network,io,**process**}.csv` + `source_files.txt` + `coverage.tsv` + **`coverage.json`** | ✅ 批 2 已补 `-w` 与 `coverage.json`（OF-18）；`sadf` **有意不带 `-t`**（四库归一，OF-07）；★ 批 6/N-7：中间清单落 `RUNTIME[TMP_DIR]`（**不进包**），`history/source_files.txt` 是唯一留档；★ 批 7/N-19：`output_file` = `history/coverage.json`（不再是目录 `history/`），无产物时为空串 |
| `timeseries.realtime_sampling` | `timeseries` | `timeseries/system_{cpu,memory,network,disk}.csv` + `oracle_sysstat.csv` | 与 MySQL 版一致（间隔 5s × 6 次 ≈ 30s），见 INTERFACE §5.1 的 `_PREV_*` 差分口径；★ 批 7/N-19：`output_file` = `timeseries/oracle_sysstat.csv`（不再是目录 `timeseries/`） |

时序 CSV 表头（**下游按列名取值，不要按列号**）：

```
system_cpu.csv      : timestamp,elapsed_ms,user_pct,system_pct,iowait_pct,steal_pct,idle_pct,busy_pct
system_memory.csv   : timestamp,elapsed_ms,mem_total_bytes,mem_available_bytes,mem_used_pct,swap_total_bytes,swap_used_bytes,cached_bytes,dirty_writeback_bytes
system_network.csv  : timestamp,elapsed_ms,interface,rx_bytes_per_sec,tx_bytes_per_sec,rx_bytes_delta,tx_bytes_delta
system_disk.csv     : timestamp,elapsed_ms,device,read_bytes_per_sec,write_bytes_per_sec,read_iops,write_iops,read_await_ms,write_await_ms,util_pct,avg_queue_size
oracle_sysstat.csv  : timestamp,elapsed_ms,user_commits,user_rollbacks,execute_count,parse_count_total,parse_count_hard,
                      physical_reads,physical_writes,redo_size,sorts_memory,sorts_disk,table_scans_long,table_scan_rows,
                      consistent_gets,db_block_gets,redo_writes,redo_entries,session_logical_reads,
                      physical_reads_direct,physical_writes_direct
```

**与 MySQL 的关键差异（adapter 必须知道）**：Oracle 的 `oracle_sysstat.csv` 是 **19 个 `v$sysstat` 计数器的绝对值**（不是差值），Python 侧需**自行作差**再算 TPS/QPS/每秒物理读；MySQL 的 `mysql_status.csv` 同理。`timestamp` 是**本地时间**（`iso_now()`），与 `history/*.csv` 的 **UTC** 不同 —— 批 2 已补 `snapshot.time_evidence`（含 `timezone`）作为对齐依据；sar 侧**不改 `-t`**（四库归一，见 OF-07）。

### 4.3 SQL 项（`category = sql`，136 项）

**136 项，136 个唯一 item_id** —— 命名规则 `oracle.<域>.<项>`（全 ASCII 点分），**由调用点显式传入**
（`ora_sql_to_md <item_id> <标题> <SQL>`），不再由中文标题派生。
> **v1.4 的塌陷（已修，OF-01）**：item_id 曾是 `sql.$(sanitize_filename "<中文标题>")`，中文被吃空 →
> 136 项只剩 66 个唯一 id、48 项挤在 `sql.`；真实包里 `status/` 只有 79 个文件
> （`sql.*` 47 / `module.*` 11 / `system.*` 20 / `sar_files.txt` 1）。**v2.0 起唯一。**
> ★ **批 6/N-7 起 `status/` 里不再有 `sar_files.txt`** —— 它不是状态记录，只是一份过程清单（且与
> `history/source_files.txt` 完全重复），已移到 `RUNTIME[TMP_DIR]`（tmp 在包树之外，天然不进包）。
> 域清单：`basic / tablespace / asm / archive / session / performance / index / adg / rac / rman /
> alert / pdb / service / dg / flashback / audit / awr / control / topsql / validation`
> （共 136 个 id，完整对照表见 `docs/oracle-collector-item-ids.md`）。

按模块分布：

| 模块 | SQL 项数 | 备注 |
|---|:---:|---|
| `collect_oracle_info` | 16 | 基础信息 / 字符集 / 参数 / SGA / PGA / 控制文件 / Redo / 对象 / 用户 / 密码过期 / AWR 快照 / 约束 / 分区 / 大表（+ CDB 时的 PDB 列表） |
| `collect_tablespace` | 5 | 含**表空间增长趋势（AWR 30 天）**与 **>80% 预警**（后者是 SQL 侧判定，属有意为之） |
| `collect_asm` | 3 | `HAS_ASM=0` 时整模块跳过 |
| `collect_archive` | 5~6 | +ASM 时多一项 FRA |
| `collect_session_lock` | 5 | 含 1 段**裸 here-doc**（杀会话语句生成，L1389-1403）**不经 `ora_sql_to_md`，因此没有 status 记录** ← 契约缺口 |
| `collect_performance` | 9~11 | 4 项受 `DIAGNOSTICS_PACK` 开关控制 |
| `collect_index` | 4 | |
| `collect_adg` | 4~7 | 无 DG 配置时整模块跳过 |
| `collect_dg_broker` | 5~7 | |
| `collect_flashback` | 4~5 | |
| `collect_rac` | 5 | `IS_RAC=0` 跳过 |
| `collect_rac_ha` | 1 + 3 段命令证据 | `crsctl`/`ocrcheck`/`gpnptool` 输出**直接进 Markdown，无 status** |
| `collect_rman` | 3 + OS 层检查 | OS 层 `crontab`/`/etc/cron.d`/备份目录（5 个固定路径）**只进 Markdown** |
| `collect_audit_security` | 9 | |
| `collect_awr_stats` | 2 + 1~6 | 大部分受 `DIAGNOSTICS_PACK` 控制 |
| `collect_control_spfile` | 3 | |
| `collect_diag_top` | 5~6 + 2 段 | 含 1 段 `DBMS_XPLAN` 裸 here-doc（L2775-2809）**无 status** |
| `collect_production_validation` | 13~14 + 2 段 | 含 OPatch / dgmgrl / rman 三段裸命令证据 |

**六处"裸 here-doc"不等于零风险**：`collect_session_lock` 的 KILL 语句生成、`collect_rac_ha` 的 crsctl/ocrcheck/gpnptool、`collect_diag_top` 的 XPLAN、`collect_production_validation` 的 OPatch/dgmgrl/rman —— **这些执行结果只在 Markdown 里，不进结构化包，也没有 status**。下游要做"证据清单完整性"校验时会漏掉它们。

### 4.4 模块项（`category = module`，**28 项**）

`run_module <module_id> <中文名> <函数>` 写入，`module_id` 形如 `oracle.module.<域>`（**全 ASCII**）；
中文名只用于人读输出与 `--include`/`--exclude` 的开关匹配。只有 `status` / `duration_ms` 有意义
（`row_count=0`）。取值：`ok` / `error` / `skipped`（被 `--exclude` 排除时写 `skipped`，
reason=`excluded by user`）、`not_enabled`（`collect_sar_history` 被 `--no-sar` 关时）。
★ **批 6/N-6**：控制台的「执行模块: N」由 `wait_background_modules` **按落盘的 status 文件统一回写** ——
bg 模块（`sar_history` / `realtime_sampling`）跑在**子 shell** 里，其内存自增随子 shell 退出蒸发
（批 5 实测：控制台印 26，而 `MODULE_PLAN` 有 28）。**口径与同步路径一致**：`skipped` 不计入执行数，`error` 同时计入失败数。

> **v1.4 的塌陷（已修，实测-3）**：模块 item_id 曾是 `module.${中文名}`，16 个模块名被吃成空 →
> `module..tsv` 互相覆盖，27 个模块只剩 11 个文件。v2.0 起唯一。

| module_id | 中文名 | 函数 | 方式 |
|---|---|---|---|
| `oracle.module.awr_report` | 生成AWR性能报告 | `collect_awr_report` | run |
| `oracle.module.sys_info` | 采集系统信息 | `collect_sys_info` | run |
| `oracle.module.system_static` | 采集系统静态信息 | `collect_system_static` | run |
| `oracle.module.capability_probe` | 采集能力探测 | `collect_capabilities` | run |
| `oracle.module.sar_history` | 采集SAR历史 | `collect_sar_history` | **bg** |
| `oracle.module.realtime_sampling` | 实时时序采样 | `collect_oracle_realtime_samples` | **bg** |
| `oracle.module.basic` | 采集Oracle基础信息 | `collect_oracle_info` | run |
| `oracle.module.pdb` | 采集PDB容器信息 | `collect_pdb` | run |
| `oracle.module.tablespace` | 采集表空间信息 | `collect_tablespace` | run |
| `oracle.module.asm` | 采集ASM磁盘组 | `collect_asm` | run |
| `oracle.module.archive` | 采集归档日志 | `collect_archive` | run |
| `oracle.module.session_lock` | 采集会话与锁 | `collect_session_lock` | run |
| `oracle.module.performance` | 采集性能诊断 | `collect_performance` | run |
| `oracle.module.index` | 采集索引信息 | `collect_index` | run |
| `oracle.module.adg` | 采集ADG状态 | `collect_adg` | run |
| `oracle.module.dg_broker` | 采集DG Broker | `collect_dg_broker` | run |
| `oracle.module.flashback` | 采集闪回数据库 | `collect_flashback` | run |
| `oracle.module.rac` | 采集RAC专项 | `collect_rac` | run |
| `oracle.module.rac_ha` | 采集RAC高可用 | `collect_rac_ha` | run |
| `oracle.module.rman` | 采集RMAN备份信息 | `collect_rman` | run |
| `oracle.module.control_spfile` | 采集控制文件与SPFILE | `collect_control_spfile` | run |
| `oracle.module.audit_security` | 采集审计与安全 | `collect_audit_security` | run |
| `oracle.module.awr_stats` | 采集AWR与统计信息 | `collect_awr_stats` | run |
| `oracle.module.diag_top` | 采集故障诊断 | `collect_diag_top` | run |
| `oracle.module.production_validation` | 生产级完整性验证 | `collect_production_validation` | run |
| `oracle.module.alert_log` | 扫描告警日志 | `collect_alert_log` | run |
| `oracle.module.health_summary` | 健康检查总结 | `collect_health_summary` | run |
| `oracle.module.structured_packaging` | 结构化打包 | `collect_structured_package` | run |

**执行顺序的单一来源**：脚本里的 `MODULE_PLAN` 数组（`module_id|中文名|函数|run|bg`）——
dry-run 预览与真实执行**共用它**（OF-25 提前落地）。`wait_background_modules` 在
`health_summary` **之前**收口两个 bg 模块。

### 4.5 打包项

`package.security_scan`（`category = package`）与 `module.structured_packaging`。**原先是两者口径矛盾**（OF-05）—— **批 3 已修**：安全扫描被拦时，`module.structured_packaging` 现在如实登记 `status=error`、`exit_code=30`；tar 失败记 `exit_code=31`。两者不再互相打架。

---

## 5. 输出目录树（Python 的输入）

**当前实况（v2.0；`filepath = ${CONFIG[REPORT_DIR]}`，默认 `/var/tmp`，可用 `--output-dir=DIR` 覆盖 —— 批 4 / OF-14）**：

```
DIR/                                                                    ← --output-dir（默认 /var/tmp）
├── oracle_inspection_<dbname>_<host>_<ip>_<port>_<inst>_<qctime>.md    ← 人读报告
├── oracle_inspection_<ts_tag>_<qctime>.log                             ← 采集日志（**不进包**）
├── oracle_inspection_status_summary_<ts_tag>_<qctime>.txt              ← 状态汇总（操作员副本，**不进包**；机器可读版在包内）
├── .oracle_inspection_tmp_<ts_tag>_<qctime>/                           ← 临时工作目录（OF-26）：**不在 TASK_DIR 内**，
│                                                                         正常结束与中断都会删 → 包/manifest 永远看不到它
├── oracle_inspection_timeseries_<ts_tag>_<qctime>/                     ← 时序（打包时拷进 TASK_DIR）
│   ├── system_cpu.csv  system_memory.csv  system_network.csv  system_disk.csv
│   └── oracle_sysstat.csv
├── oracle_inspection_history_<ts_tag>_<qctime>/                        ← sar 历史（打包时拷进 TASK_DIR）
│   ├── sar_cpu.csv … sar_io.csv + sar_process.csv（**8 类**，批2 补 -w）  source_files.txt  coverage.tsv  **coverage.json**（批2）
├── oracle_inspection_status_<ts_tag>_<qctime>/                         ← 逐项状态（打包时拷进 TASK_DIR）
│   └── <item_id>.tsv（制表符 10 列，见 §6.2）
└── oracle_inspection_task_<instance_tag>_<qctime>/                     ← ★★ Python 的唯一输入 ★★
    ├── snapshot.json                      ← 第一入口（§6.1，**schema 2.0**）
    ├── collection_status.json             ← 状态聚合（items[] + summary{}，OF-16/23）
    ├── summary.txt                        ← 可读摘要（含"耗时最长前 10" + **分档自校验行**，批 5/N-5）
    ├── packaging_status.json              ← **归档前自检结论**（批 5/N-3，§6.5）：`phase` / `security_scan` / `archive_planned` / `note`
    ├── manifest.json                      ← package_version/collector_version/database_type/instance_tag/created_at/files[{path,size_bytes,sha256}]
    ├── tables/                            ← ★ **v2.0 起只放两类**（批 6/N-8 收窄，见 §6.3）：
    │     <item_id>.psv                    ←   ① SQL 结果（ASCII item_id 命名 + `|` 分隔）
    │     <name>.tsv                       ←   ② **真 TAB 表格**，共 5 个：capabilities / oracle_env / oracle_limits / hugepages / kernel_parameters
    ├── evidence/*.txt                     ← 命令证据（**含 free / df -PT / df -Pi / lsblk 的列对齐文本** —— N-8 起从 tables/ 归此）
    ├── logs/modules/<category>.log        ← 模块错误**完整原文**（OF-26；status 的 reason 只留 5 行摘要）
    ├── timeseries/                        ← 上面 timeseries 目录的拷贝
    ├── history/                           ← 上面 history 目录的拷贝
    ├── status/<item_id>.tsv               ← 上面 status 目录的拷贝
    └── COLLECTION_INCOMPLETE              ← **仅在收集中断时出现**（OF-19，首行 `partial`）→ 包残缺的机器可读标记
```

**Python 侧的三个入口顺序**：`snapshot.json`（实例/环境）→ `status/*.tsv`（或包内 `collection_status.json` 聚合，逐项状态）→ `tables|timeseries|history`（数据）。

**中断判定（OF-19）**：TASK_DIR 内出现 `COLLECTION_INCOMPLETE` ⇔ 采集被 INT/TERM/HUP 打断（同时退出码 130）。**正常收尾的包里没有这个文件** —— 所以下游可以直接 `os.path.exists(...)` 判残缺，不必猜。
**注意 `manifest.json` 里只有 `path` / `size_bytes` / `sha256`，没有 `role` / `kind` 标注**，下游无法只靠 manifest 判断某个文件是"数据"还是"证据"。

---

## 6. 入口文件的字段契约

### 6.1 `snapshot.json`（第一入口，**v2.0 = 8 段结构**）

> **v2.0 现状（批 1 / OF-27）**：文件被组织成 **8 个段**，`schema_version` = `2.0`，`collector_version` = `2.0`：
>
> | 段 | 内容 |
> |---|---|
> | `collector` | `name` / `version` / `hostname` / `primary_ip` / `collection_started_at` |
> | `host_identity` | ★ **`database_target_is_local`（true/false）** + `collector_hostname` + `target_resolved_ip` —— **批 1 把 `database_target_is_local` 从顶层移到这里**；★ 批 6（N-10）删掉了与之**同义重复**的 `is_local_target`（同一个布尔曾以两个键并列出现，下游不知该读哪个）。**唯一权威键 = `database_target_is_local`** |
> | `capabilities` | 与 `tables/capabilities.tsv` 同源；**布尔在这里是 `true/false`**（tsv 里是 `0/1`），字符串原样 |
> | `time_evidence` | `host_local_time` / `host_utc_time` / `timezone` / `ntp_synchronized` + 两个证据文件路径 |
> | `sampling` | `source` / `interval_seconds` / `sample_count` / `files[]`（时序文件名） |
> | `privacy` | `sql_text` / `redacted[]` / `notes`（声明"口令不进 argv、明文 SQL 是有意为之"） |
> | `collection_summary` | 按 **10 个 status** 的计数 + `sql_query_count` / `sql_fail_count`（与 `collection_status.json` 的 `summary{}` 同源） |
> | `artifacts` | `snapshot` / `collection_status` / `summary` / `manifest` / **`packaging_status`** / `tables_dir` / `evidence_dir`，加上 **`report_md_external` + `report_md_scope`**（★ 批 5 / 方案 B：人读 md 落在**包外**，见 §6.5） |
>
> **顶层 v1.4 平铺字段仍然保留**（`host` / `port` / `service` / `db_name` / `instance_name` / `is_rac` / … / `diagnostics_pack`），旧解析器仍可读；但**新的 adapter 请只读段内字段**。
> 下表 JSON 是 **v1.4 实测样本**，保留用于对照。

**v1.4 样本（21 字段 → 批 2 后 25 字段）**：

```json
{
  "schema_version": "1.0",              // v2.0 起 = "2.0"（OF-21 已抽全局变量）
  "database_type": "oracle",
  "collector_version": "1.4",           // v2.0 起 = "2.0"（与文件头同源，OF-21）
  "instance_tag": "…",
  "host": "…", "port": "…", "service": "…",
  "database_target_is_local": true|false,   // ★ 批2/OF-06（**v2.0 起移到 host_identity 段**）
  "collector_hostname": "…",                // ★ 批2/OF-06
  "target_resolved_ip": "…",                // ★ 批2/OF-06
  "db_name": "…", "instance_name": "…", "db_role": "…", "open_mode": "…", "ora_version": "…",
  "is_rac": 0|1, "rac_node_count": N, "has_asm": 0|1, "is_standby": 0|1, "is_cdb": 0|1,
  "pdb_count": N, "pdb_list": "…",
  "collection_started_at": "…",
  "time_evidence": {                        // ★ 批2/OF-07（字段名与 MySQL 完全一致）
    "host_local_time": "…", "host_utc_time": "…", "timezone": "…", "ntp_synchronized": "…",
    "timedatectl_file": "evidence/timedatectl.txt",
    "chrony_tracking_file": "evidence/chronyc_tracking.txt"
  },
  "diagnostics_pack": 0|1
}
```

**实测样本**（2026-10-09 真实包，改造前 21 字段；批 2 后新增 4 个）：

```json
{"schema_version":"1.0","database_type":"oracle","collector_version":"1.4",
 "instance_tag":"ZYDB_db01_192.168.100.80_1521_zydb1",
 "host":"127.0.0.1","port":"1521","service":"orcl",           // ← host 是连接串，可能与本机不符
 "db_name":"ZYDB","instance_name":"zydb1","db_role":"PRIMARY","open_mode":"READWRITE",
 "ora_version":"19.16.0.0.0",                                 // ← 与 tables/实例信息.tsv 的 19.0.0.0.0 不同（§0.5.4 口径-1）
 "is_rac":0,"rac_node_count":0,"has_asm":0,"is_standby":0,"is_cdb":0,
 "pdb_count":0,"pdb_list":"","collection_started_at":"2026-10-09_22-47-04","diagnostics_pack":0}
```

> ⚠️ **`collection_started_at` 的格式是 `YYYY-MM-DD_HH-MM-SS`**（下划线分隔，**非 ISO 8601**）——与 `status` / `manifest.json` 里用的 `2026-10-09T22:47:04+08:00` **不一致**，下游对这两个来源要分别解析。

**类型口径（v2.0 实况）**：**顶层**布尔位（`is_rac`/`has_asm`/`is_standby`/`is_cdb`）仍是 **0/1 整数**；**新段**里的是真正的 `true/false` —— `host_identity.database_target_is_local`、`capabilities.*` 的布尔探针（含 `capabilities.is_local_target`，**注意它与 `host_identity.database_target_is_local` 同值**：前者是能力表里的探测位、后者是契约名的权威字段；批 6/N-10 只删掉了 `host_identity` 内那个重复键）。adapter 必须**按段分别处理**（顶层读 int、段内读 bool）。

**批 2 新增的 4 个字段（OF-06 / OF-07）**：

| 字段 | 类型 | 语义 | 下游怎么用 |
|---|---|---|---|
| `database_target_is_local` | bool | `true` = 本次是**本机采集**，OS 数据属于目标库；`false` = OS 数据属于采集机 | **`false` 时 system.* / timeseries.* 一律不得用于评价目标库资源**（AGENTS.md 硬规则） |
| `collector_hostname` | string | 采集执行机主机名 | `false` 时用于在报告里说明「数据属于哪台机器」 |
| `target_resolved_ip` | string | `--host` 解析后的 IPv4（回环恒为 `127.0.0.1`；解析失败为空串） | 与 `instance_tag` 交叉核对 |
| `time_evidence` | object | 主机本地时间/UTC/时区/NTP 同步状态 | **校正 `history/sar_*.csv`（UTC）与 `timeseries/*.csv`（本地）两套时钟**；`ntp_synchronized` 兼容新旧 systemd 字段名 |

**仍缺失的段（OF-27，四库 adapter 归一的前提）**：`collector{}`、`host_identity{}`（`database_target_is_local` 的可选归属，批 2 已单独落到顶层）、`capabilities{}`（OF-13）、`sampling{}`、`privacy{}`、`collection_summary{}`、`artifacts{}`。

### 6.2 `status/*.tsv`（第二入口，制表符 10 列）

> **v2.0 现状（批 1）**：文件名 = `sanitize_filename(item_id)`，而 item_id 现在是**显式 ASCII 点分 id** ⇒ `sanitize_filename` 对它是**恒等变换**，`sql` 层与 `module` 层都**不再塌陷**（136 项 = 136 个文件）。`row_count` 记**真实数据行数**（OF-22）；SQL 项的 `output_file` 指向 `tables/<item_id>.psv`（实测-2 已修）。
> 下表的"现况与坑"是 **v1.4 实测基线**，保留用于对照与回归。

**文件名 = `sanitize_filename(item_id)`**；文件内容第 1 列 = `item_id` 原样。

```
1 item_id    2 category   3 status   4 started_at   5 finished_at
6 duration_ms 7 row_count 8 exit_code 9 output_file 10 reason
```

| 列 | **v1.4 基线**现况与坑（2026-10-09 实测；v2.0 修复见本段上方提示） |
|---|---|
| `item_id` | ~~**塌陷**（OF-01）~~ **v2.0 已修**。v1.4：136 个 SQL 项 → **66 个唯一 id**，11 组塌陷 / 70 条被覆盖丢弃；最大组 `sql.` ← 48 条。**`sql` 层的 id 是 sanitize 后的值**（`sql.80` / `sql.RMAN_7`），无法反推中文标题；`module` 层相反（id 是原始中文，只塌文件名） |
| `category` | 只 6 个取值，`sql`/`module` 是 catch-all（§2.2 #3） |
| `duration_ms` | 可用 |
| `row_count` | ~~SQL 项恒为字符串 `"-"`（OF-22）~~ **v2.0 已修**：SQL 项记真实数据行数（首行是列名行 ⇒ `rows-1`）；**module 项仍恒为 `0`**；`capture_command` 项是文件行数（含表头行，**+1 偏差**，实测 `system.time_status` = 4） |
| `output_file` | ~~**SQL 项恒为 `"report"`**~~ **v2.0 已修**：SQL 项指向 `tables/<item_id>.psv`；`capture_command` 项是真实证据路径（`evidence/*.txt`）；`module` 项为空串。★ **批 7/N-19：列值必须是"包内真实存在的**文件**"**（可被 `os.path.isfile(包根/值)` 命中）。一次产出多个文件的"集合型"项（`sar_history` / `realtime_sampling`）取一个**代表文件**（`history/coverage.json` / `timeseries/oracle_sysstat.csv`），**不再写目录**；无产物时写空串 |
| `reason` | `sanitize_text` 处理过（TAB/CR/LF → 空格，多空格压缩），最多 5 行截断。**v2.0 起完整原文另落包内 `logs/modules/<category>.log`**（OF-26）—— status 里只留摘要 |

**v1.4 实测数量对账（保留作对照；v2.0 后 status 文件数应等于 136 + 31 + 28 + 打包项）**：

```
status/sql.*.tsv      = 47   vs   tables/ SQL 结果文件 = 98   → 错位 51
status/module.*.tsv   = 11   vs   主流程 run_module    = 27   → 丢失 16
status 状态分布        = 75 ok / 2 not_enabled / 1 partial
                      → 无 error / timeout / empty / skipped（34 个空结果项全记 ok，见 §0.5.3 实测-1）
```

### 6.3 `tables/<item_id>.psv`（SQL 结果：`|` 分隔，**不是 TAB**）

> **v2.0 现状（批 1 / OF-01 + OF-02，批 5 / N-2 收口）**：文件名 = **`<item_id>.psv`**（ASCII 点分 id，**不再是中文标题**）；内容经 `psv_normalize()` 规范化 —— **竖线分隔、两侧填充空白剥净、丢空行、丢 `---` 分隔行**；**首行即列名行**。
>
> ★★ **批 5 起只允许三种形态**（下游按第 1 行分派，**别猜**）：
>
> | 形态 | 第 1 行 | 含义 | `status` |
> |---|---|---|:---:|
> | **数据** | 列名行（`|` 分隔，无 `#` 前缀） | 真的查到行 → 第 2 行起是数据 | `ok` |
> | **`psv-empty`** | `# oracle-inspection psv-empty item_id=… sep=| rows=0 status=empty` | **查了、确认没有**（与"没采到"是**相反结论**，不得当 0 用） | `empty` |
> | **`psv-unavailable`** | `# oracle-inspection psv-unavailable item_id=… sep=| rows=0 status=<status>` | **根本没取到**（SQL 报错 / 权限 / 超时 / 列不存在…） | 除 `ok`/`empty` 外的任一 | 
>
> ⚠️ **`psv-unavailable` 是批 5 新增**（N-2）。此前失败项的 `.psv` 里落的是 **sqlplus 的报错回显**（`SELECT …` / `*` / `ERROR at line …` / `ORA-00904: …`，`row_count` 还记成 3）—— 既不是数据也不是占位，**下游按列名解析会静默出错**。现在：失败项一律写占位、`row_count=0`，**报错原文只进 `status/*.tsv` 的 `reason`（5 行摘要）与包内 `logs/modules/<category>.log`（完整）**。
> 两种占位的区别**只差一个词**（`psv-empty` / `psv-unavailable`），下游请**按词判定**，不要只按"首字符是 `#`"判定。
> **注意**：批 6（N-8）起 `tables/` **只放两类** —— `*.psv`（SQL 结果，`|` 分隔）与 **真 TAB 表格** `*.tsv`（5 个：`capabilities` / `hugepages` / `kernel_parameters` / `oracle_limits` / `oracle_env`）。原先混在 `tables/` 里的 `block_devices` / `filesystems` / `inodes` / `memory_snapshot` 实际是 **`free` / `df` / `lsblk` 的列对齐文本**（列内空格填充、`lsblk` 的 `MODEL`/`SERIAL` 自身含空格），**扩展名说谎** —— 已改归 `evidence/*.txt`（`memory_snapshot.txt` / `filesystems.txt` / `inodes.txt` / `block_devices.txt`）。**下游按扩展名切表格的假设自此成立**；要读这 4 项请按文本处理（或先做一次字段抽取）。

| 属性 | 实况（**以下均为 2026-10-09 真实包实测**） |
|---|---|
| 分隔符 | **`|`**（`SET COLSEP '|'`），不是 TAB |
| 文件名 | **中文标题**（`tsv_filename` L304 保留中文），如 `tables/数据库基本信息.tsv`。**不受 OF-01 影响、不塌陷** |
| 列名行 | sqlplus 按 `COLUMN … FORMAT aNN` 宽度输出，**列内补空格到定宽**；**首行是一个空行** |
| 空结果 | 单行 `# no rows returned`（**注释，无列名**）。本包内 **34 个**（多为"实例刚启动无历史数据"） |
| 列宽截断 | `name a50` / `value a80` / `message a120 WORD_WRAPPED` / … 共约 40 条 `COLUMN` 声明（L559-604），超长值**被静默截断** |
| **格式不统一** | 同目录内至少 **3 种形态**：① sqlplus 定宽 `\|` 表（`数据库基本信息.tsv`）；② 命令原样输出（`filesystems.tsv`= `df`、`memory_snapshot.tsv`= `free`、`inodes.tsv`）；③ `key<TAB>value` 两列（`hugepages.tsv`、`kernel_parameters.tsv`）。**下游不能假设统一格式** |

**实测样本**（`cat -A "tables/数据库基本信息.tsv" | head -2`）：

```
$
NAME^I |DB_UNIQUE_NAME^I^I|DATABASE_ROLE^I |OPEN_MODE^I      |LOG_MODE    |CREATED^I       |RESETLOGS_TIME^I   |PROTECTION_MODE$
```

即：**首行空行 → 表头行 → `---` 分隔行 → 数据行**，字段内混有 TAB 与空格填充。`pd.read_csv(sep='\t')` 必然解析错。

`tables/` 在 2026-10-09 真实包内共 104 个文件：**98 个 SQL 结果 + 6 个 OS 结构化**（`block_devices` / `filesystems` / `hugepages` / `inodes` / `kernel_parameters` / `memory_snapshot`，均为英文名）。
★ **v2.0 / 批 6 起**：`block_devices` / `filesystems` / `inodes` / `memory_snapshot` 这 4 个**已改归 `evidence/*.txt`**（原因见本节开头）。所以 **v2.0 的 `tables/` = 98 个 `.psv` + 5 个真 `.tsv`**（`capabilities` / `hugepages` / `kernel_parameters` / `oracle_limits` / `oracle_env`）。

**批 2 新增的表格（2 个，英文名，`key<TAB>value` 形态）**：

| 文件 | 列 | 说明 |
|---|---|---|
| `tables/oracle_limits.tsv` | `source_file \t scope \t limit \t value`（4 列） | `scope` = `*` / `oracle` / `@dba`；`limit` = `soft nofile` / `hard nproc`…；按「limits.conf 先、`limits.d/*.conf` 字典序」合并 → **下游取同一 `(scope,limit)` 的最后一条即 PAM 最终生效值**（OF-11） |
| `tables/oracle_env.tsv` | `variable \t value`（5 行） | ORACLE_HOME / ORACLE_BASE / ORACLE_SID / GRID_HOME / TNS_ADMIN（OF-15） |

`tables/hugepages.tsv` 也在批 2 扩展：除原有 3 行 `path`（THP enabled / defrag / `nr_hugepages`）外，新增 `/proc/meminfo:<KEY>` 形式的**实测值**行（`HugePages_Total` / `HugePages_Free` / `HugePages_Rsvd` / `Hugepagesize` / `AnonHugePages` / `Hugetlb`）——只有 Total 与 Free 对照才算得出「配了但没被用掉的大页」。

> ★ **Python 侧怎么读（批 8 定稿）**：
> ① **按 `collector_version` 分派分隔符** —— `1` ⇒ 只读 `*.tsv` 并按 `|`；其余 ⇒ 读 `*.psv`（`|`）**与** `*.tsv`（TAB）。
> ② **`item_id` 靠 `status/*.tsv` 的 `output_file` 反查，不靠文件名**（`kernel_parameters.tsv` ↔ `system.sysctl_selected`）。
> ③ 首行是列名行（`psv_normalize` 保证）；`# … psv-empty` / `# … psv-unavailable` **占位一律返回空表**，
>    "查了没有"与"没采到"的区别在 `status` 列，**不在表格里**。
> ④ `kernel_parameters.tsv` 的**表头是批 7（N-20）才补的** —— 用 ≤批 6 采的老包时首行是数据，
>    按 `DictReader` 会把第 1 行当表头（**静默少一行**）；该表无下游消费者，故 Python 侧未做兜底。

### 6.4 `manifest.json`

`{package_version, collector_version, database_type, instance_tag, created_at, self_excluded, self_excluded_reason, files:[{path,size_bytes,sha256}]}`。`path` 是**相对 TASK_DIR 的路径**。**无 `role` 字段**；`security_scan` 失败时 manifest 已生成但 tar.gz 不生成 —— 这是**设计如此**（`manifest` 是 TASK_DIR 的内容物，先于打包）。判定"包到底成没成"请看 `module.structured_packaging` 的 `status`/`exit_code`（OF-05，批 3 已修）。
⚠️ `manifest.json` **不自列**（它列的是"除自己以外"的 TASK_DIR 内容物），校验时不要判它"缺"。
★ **批 6（N-11）起这件事被显式声明**：头部有 `"self_excluded":["manifest.json"]` + `self_excluded_reason`（自引用无法包含自身 sha256）。校验脚本据此跳过即可 —— 不必再靠"数出来少一个"去反推。
```json
{"package_version":"2.0","collector_version":"2.0","database_type":"oracle","instance_tag":"…","created_at":"…",
 "self_excluded":["manifest.json"],
 "self_excluded_reason":"manifest 无法包含自身的 sha256（自引用）；校验时请显式排除本文件",
 "files":[{"path":"snapshot.json","size_bytes":1234,"sha256":"…"}, …]}
```

### 6.5 `packaging_status.json`（归档前自检结论，**批 5 / N-3 新增**）+ 两条台账口径说明

```json
{"schema_version":"2.0","collector_version":"2.0","phase":"pre-archive","created_at":"…",
 "security_scan":"ok|blocked","archive_planned":"<basename>.tar.gz","note":"…"}
```

- **它答什么问题**：拿到包的人不必回头找操作员副本，就知道"这一包**有没有通过打包前的安全扫描**、归档文件该叫什么"。
- **它答不了什么**：`tar` 本身的成败（退出码 30=安全扫描拦截 / 31=tar 失败）。**这是不变量**：归档的结论产生于**包封口之后**，结构上无法装进它自己封装的归档。要看它请读**操作员副本** `DIR/oracle_inspection_status_<ts_tag>_<qctime>/module.structured_packaging.tsv`（该文件**不在包内**，`collection_status.json.items[]` 里也没有它）。
- **包内台账项数 = 控制台"采集项总数" − 1**：差的正是 `module.structured_packaging`。控制台现在会把这句话直接印出来（批 5 / N-5），不再让人自己猜。`package.security_scan` **在包内**（批 5 起：扫描完就 `sync_status_to_package` 补拷进 `TASK_DIR/status/`，同时进 `items[]` 与 `manifest`）。
- **`summary.txt` 有分档自校验行**：`分档合计 / 总数 : N / N`。10 个 status 取值 + `skipped`… 一律**相加必须等于总数**，不等时该行会追加 `← ⚠ 不一致`。批 5 之前这里漏了 `partial` 行（相加 155 ≠ 156）、控制台另算一遍台账且重复计数（`schema_mismatch` 算进"错误"、`not_enabled` 又算进"跳过/未启用"）—— 现在是**同一处算式**（`refresh_status_tally()`，尾部 / `snapshot.collection_summary` / `collection_status.json.summary` 三处同源）。

---

## 7. `status` 语义表（Python 必须按这张表分支）

**v2.0 起 Oracle 有 10 个取值**（批 1 / OF-03 补上 `schema_mismatch`），仍比 MySQL 少 `not_applicable`（Oracle 按 `snapshot.json` 的 `is_rac` / `has_asm` / `is_cdb` **自行推断**）。下表"当前能否产生"列的括号内计数是 **v1.4 实测基线**，保留用于对照。

| status | 含义 | Python 应如何处理 | 当前能否产生 |
|---|---|---|:---:|
| `ok` | 命令退出 0 且输出非空 | 正常采信 | ✅ **v1.4: 75 个** |
| `empty` | 命令退出 0 但**输出为空** | **不是错误**，是"查了、确实没有"。**不得当 0 用** | ✅ **v2.0 全路径可用**（批 1 修实测-1：原判据读 stderr 而消息在 stdout，SQL 路径永不触发） |
| `error` | 其它失败 | 计入失败，看 `reason`；完整原文在包内 `logs/modules/<cat>.log`（OF-26） | ✅（v1.4: 0 个） |
| `timeout` | rc = 124 / 137 | 计入失败；**若 OS 采集项超时，不得降级成"无风险"** | ✅（仅告警日志用了 `timeout`；v1.4: 0 个） |
| `unsupported` | 表/视图/功能不存在（`ORA-00942`…） | 环境真不支持，可忽略但要披露 | ✅（v1.4: 0 个） |
| `permission_denied` | 权限不足（`ORA-01031`…） | **不得当作 unsupported**；须在报告里显式披露"权限不足" | ✅（v1.4: 0 个） |
| `not_enabled` | 服务/daemon 未运行、文件不存在 | 须区分"客户没开"与"没采到" | ✅ **v1.4: 2 个**（`chronyc_sources` / `chronyc_tracking`，`506 Cannot talk to daemon`）。★ 批 6/N-13 起 `lsnrctl` 的 `TNS-12541` / `TNS-00511` / `no listener` 也归此（listener 未运行 ≠ 采集失败） |
| `skipped` | 用户 `--exclude` / `--no-sar` 主动跳过；★ 批 6/N-14 起**互斥备选未采用**也记此（`system.ifconfig` / `system.ntpq_peers`，见 §4.1 互斥清单） | 按设计跳过 | ✅（v1.4: 0 个） |
| `partial` | 部分成功：`timeseries.realtime_sampling`（采样点数不足 / Oracle 统计读取偶发失败，rc=2）、★ 批 7 起 `system.sar_history`（`coverage` 介于"有覆盖"与"读不到历史区间"之间，如只命中部分 sa 文件） | 按 `reason` 里的实际点数 / `history/coverage.json` 决定可用性 | ✅ **v1.4: 1 个**；v2.0 实测两者都可能出现 |
| `schema_mismatch` | 表在、列不在 = **采集器缺陷**（`ORA-00904`/`ORA-01722`） | **必须单独计数并让人看见**，不得混进 `unsupported` | ✅ **v2.0 新增**（批 1 / OF-03；分类顺序在 `unsupported` **之前**） |
| `not_applicable` | 该环境不适用（非 RAC 的 RAC 项、无 ASM 的 ASM 项） | **Oracle 不产出这个值**，adapter 需按 `snapshot.json` 的 `is_rac`/`has_asm`/`is_cdb` **自行推断** | ❌ |

**"未采集 ≠ 0"的硬约束**（AGENTS.md）：上表里 `empty` / `unsupported` / `permission_denied` / `not_enabled` / `skipped` / `timeout` **一律不得被转换成 0 或"正常"**。v2.0 起 `row_count` 是**真实数据行数**（不再恒为 `-`，OF-22），但 `empty` 项的 `row_count=0` 仍**不得**当成"该指标为 0"。

---

## 8. 退出码表

**状态：已落地（批 3 / OF-17，2026-10-09）。** 契约同时写进脚本 `--help`（"退出码"段）与本节。

| 码 | 含义 | 触发点 |
|:---:|---|---|
| 0 | 全部成功（包已生成，或 `--no-package`） | 正常走完；`-h`/`--help`；`--dry-run` 预览完成 |
| 10 | 参数 / 环境 / 认证准备失败 | sqlplus 路径不可执行、状态目录创建失败、**输出目录不可写**（`ensure_output_dir`，OF-14）、输出目录空间不足 |
| 20 | **Oracle 连接失败** | 连接测试未返回 `CONNECTED` |
| 30 | 敏感信息扫描拦截，已阻止打包 | `collect_structured_package` → `security_scan` 返回 1 |
| 31 | 打包失败，任务目录可用 | `collect_structured_package` → `create_package` 返回非 0 |
| 130 | 收到 INT/TERM/HUP 中断 | `_trap_cleanup`（三个 trap 各传信号名）；**同时在 TASK_DIR 落 `COLLECTION_INCOMPLETE`**（OF-19） |

**实现机制（改 Python 时按这个理解）**：

- **10 / 20** 属"早失败点"，脚本在这些位置**直接 `exit N`**，不会产出任何包；
- **30 / 31** 属"已跑完但结果不完整"：`collect_structured_package` 把码写进全局 `FINAL_EXIT_CODE`，
  脚本最尾部 `exit "$FINAL_EXIT_CODE"` 统一退出；
- **130** 由 `_trap_cleanup` 直接 `exit 130`，**优先于**打包结果（中断即中断）；同一次 trap 里还会在
  `$TASK_DIR/COLLECTION_INCOMPLETE` 落一行 `partial`（OF-19）—— **但只在 TASK_DIR 已存在时**（`trap` 的安装点
  早于 TASK_DIR 赋值），所以参数阶段就收到 TERM 时不会有这个文件，此时也**没有包**可谈；
- 因此"包存在 + 退出码 31"是**可能且合理**的组合（tar 失败但 TASK_DIR 可用）；
  "退出码 30"时 **tar.gz 不生成**，但 TASK_DIR 目录仍在（含 `status/security_scan_findings.txt`）。

**实测证据**（2026-10-09，本机 Git Bash，假 sqlplus + stub）：

| 场景 | 结果 |
|---|---|
| 隔离 PATH（无 sqlplus） | `EXIT=10` ✓ |
| 假 sqlplus 不返回 `CONNECTED` | `EXIT=20` ✓ |
| `security_scan` stub 返回 1（拦截） | `module.structured_packaging` = `error`/rc=`30`，`FINAL_EXIT_CODE=30` ✓ |
| `create_package` stub 返回 1（tar 失败） | `module.structured_packaging` = `error`/rc=`31`，`FINAL_EXIT_CODE=31` ✓ |
| 扫描过 + 打包成功 / `--no-package` | `status=ok`/rc=`0`，`FINAL_EXIT_CODE=0` ✓ |

**过渡期注意**：本契约**从批 3 起生效**。批 3 之前采集的包（含 ZYDB 那份，`collector_version`=1.4）**没有**退出码语义，别按本表回填。

---

## 9. 变更规则

### 规则 1：动输出就 bump `SNAPSHOT_SCHEMA_VERSION`（OF-21 先补这个变量）

目录结构 / 文件增删改名 / 列增删 / 字段语义 / status 取值 / 退出码 **任一变化** → 必须 bump。
**只新增独立文件 = 次版本**；**删列 / 改名 / 改语义 = 主版本**。

**待做的两处破坏性变更都属主版本**（OF-01 改 item_id 与 tables 文件名、OF-02 改扩展名 `tsv`→`psv`），**必须同一版一起做**，别分两次。

### 规则 2：新增采集项四件事一起做

① 写代码 → ② 登记输出文件与列名（Oracle 需要补一个 `known_psv_header` 等价物，因为现在**没有**占位表头机制）→ ③ 更新本文 §4 清单 → ④ 更新 §5 目录树。

### 规则 3：改 SQL 的 SELECT 列表 → 同步改占位列名表

Oracle 现在**没有** `known_tsv_header` 这类东西（实测 0 处），空结果只写一行注释 → 改 SELECT 列表时**没有任何机制会提醒你同步**。这是 OF-02 的一部分。落 `known_psv_header` 之后，规则 3 才真正可执行。

### 规则 4：注释与代码同改

4.7% 的注释密度意味着那 170 行会被当成"已核实记录"读。**撒谎的注释比没注释更坏** —— 改逻辑时若旧注释不再成立，必须同时改注释（OF-24）。

### 规则 5：Python 侧"按列名取值，不按下标"

`tables/*.psv` 与 `timeseries/*.csv` 的列集**可能随版本变**（尤其 OF-02 改造后）。**不要按固定列号取值**。

### 规则 6：改采集形态时，先想"两个库要不要在公共层归一"

已归一的例子见 AGENTS.md（SAR CPU 列名别名、时区、内存总量反推）。Oracle 侧新增 OS 项时，先对照 §4.1 的"与 MySQL 差异"列，能归一的在公共层做，**不要在 Oracle 插件里再绕一次**。

---

## 10. 已知契约缺陷 + 建议的重构顺序

### 10.1 Python 侧现在就会踩的（按严重度）

| 缺陷 | 现象 | 对应 | 状态 |
|---|---|---|---|
| ~~状态台账塌陷~~ | ~~136 项 → 66 个 id，48 项共用一个文件；模块只剩最后一条~~ | OF-01 | ✅ **批 1** |
| ~~"TSV" 不是 TSV~~ | ~~用竖线分隔 + 填充空格 + 空结果无列名~~ | OF-02 | ✅ **批 1**（改 `psv` + 规范化 + 占位） |
| ~~`output_file` 恒为 `report`~~ | ~~从状态记录找不到对应的 tables 文件~~ | §6.2 | ✅ **批 1** |
| ~~`row_count` 恒为 `-`~~ | ~~拿不到行数~~ | OF-22 | ✅ **批 1** |
| ~~无 `collection_status.json` / `summary.txt`~~ | ~~只能自己遍历 `status/*.tsv`（还被塌陷）；汇总文件在包外~~ | OF-16 / OF-23 | ✅ **批 1** |
| ~~无 `database_target_is_local`~~ | ~~无法判断 OS 数据是否属于目标库~~ → 顶层有 `database_target_is_local` + `collector_hostname` + `target_resolved_ip`；**批 1 起移入 `host_identity` 段** | OF-06 | ✅ 批 2（批 1 改归属） |
| ~~无 `time_evidence` + sar 是 UTC~~ | ~~时区错位无从校正~~ → 有 `time_evidence{}`（含 `timezone`）；sar 保持 UTC 但**时间戳自带 `UTC` 后缀**，Python 可自校正 | OF-07 | ✅ 批 2 |
| ~~`snapshot.json` 无 `sampling{}`~~ | ~~拿不到时序文件路径与实际采样点数，只能硬编码文件名~~ | OF-27 | ✅ **批 1**（**8 段**结构） |
| ~~`schema_mismatch` 缺失~~ | ~~脚本 SQL 写错被读成"环境不支持"~~ | OF-03 | ✅ **批 1** |
| ~~无退出码~~ | ~~只能数文件判断成败~~ → 按 §8 退出码分支（10/20/30/31/130） | OF-17 | ✅ 批 3 |
| ~~汇总与打包 state 矛盾~~ | ~~`package.security_scan=error` 但 `module.structured_packaging=ok`~~ → 同步记 `error`/`exit_code=30` | OF-05 | ✅ 批 3 |
| ~~两入口之间是断的~~ | ~~`status` 的 id 与 `tables` 的文件名**永不相等**，且 `output_file` 不指向 tables~~ | 实测-2 | ✅ **批 1** |
| ~~空结果全被记成 `ok`~~ | ~~34 个空结果项 status 全是 `ok`，`empty` 在 SQL 路径永不触发~~ | 实测-1 | ✅ **批 1** |
| ~~`sql` 层 id 不可逆~~ | ~~`sql` 的 id 是 sanitize 后的值（`sql.80`）~~ | 实测-3 | ✅ **批 1** |
| ~~无 `--output-dir`~~ | ~~现场不能把产物落到挂载盘/专用目录~~ | OF-14 | ✅ **批 4** |
| ~~中断后包残缺无从程序化判断~~ | ~~只能在 md 里追加一行提示~~ → 现落 `COLLECTION_INCOMPLETE` | OF-19 | ✅ **批 4** |
| ~~错误原文只留 5 行、不落模块日志~~ | ~~ORA- 原文 / 权限栈丢失~~ → 完整原文进 `logs/modules/<cat>.log` | OF-26 | ✅ **批 4** |
| ~~`security_scan` 记录的 findings 路径在包里不存在~~ | ~~`record_status` 写的 `status/security_scan_findings.txt` 从不落盘到包内~~ | OF-30（批 4 发现） | ✅ **批 4** |
| ~~`ora_sql_to_md` 错误判据未锚行首~~ | ~~告警日志类**数据**里的 `ORA-27125` 被当成报错 → 正常项判 `error`、失败数虚高~~（**批 1 引入的回归**） | N-1（2.0 实包体检） | ✅ **批 5** |
| ~~失败 SQL 的 `.psv` 落 sqlplus 报错回显、`row_count` 是假行数~~ | ~~首行不是列名、`row_count=3`~~ → 一律写 `psv-unavailable` 占位 + `row_count=0` | N-2 | ✅ **批 5** |
| ~~包内 `status/` 与 `items[]` 缺 `package.security_scan`~~ | ~~它写在 `cp -r STATUS_DIR` 之后；注释却称"随包回传"~~ → 扫描提前 + `sync_status_to_package` + `packaging_status.json` | N-3 | ✅ **批 5** |
| ~~`V$RMAN_STATUS` 用 `SESSION_KEY` → ORA-00904，`rman_validate` 项恒失败~~ | ~~该列属于 `V$RMAN_BACKUP_JOB_DETAILS`~~ → 改 `session_recid, session_stamp` | N-4 | ✅ **批 5** |
| ~~`summary.txt` 漏 `partial` 行 / 控制台另算一遍台账~~ | ~~各档相加 ≠ 总数；「包 156 / 控制台 159」没人说得清~~ → 三处同源 + 分档自校验行 + 包内差额显式说明 | N-5 | ✅ **批 5** |
| ~~`artifacts.report_md` 指向包外、包内不存在的文件~~ | ~~下游按包根解析必 miss（同 OF-30 类）~~ → **方案 B**：`report_md_external` + `report_md_scope` | N-12 | ✅ **批 5** |
| ~~控制台「执行模块: 26」< `MODULE_PLAN` 的 28（bg 模块计数丢在子 shell）~~ | ~~`run_module … &` 里的内存自增不回传~~ → `wait_background_modules` **按落盘 status 文件统一回写**（口径与同步路径一致） | N-6 | ✅ **批 6** |
| ~~`status/sar_files.txt` 混进台账目录~~ | ~~非状态记录，且与 `history/source_files.txt` 内容重复~~ → 中间清单落 `RUNTIME[TMP_DIR]`（不进包） | N-7 | ✅ **批 6** |
| ~~`tables/` 里 4 个"扩展名说谎"的对齐文本~~ | ~~`free`/`df`/`df -i`/`lsblk` 落成 `.tsv`，下游按分隔符切会静默错位（`lsblk` 值含空格无法无损 TSV 化）~~ → 改归 `evidence/*.txt` | N-8 | ✅ **批 6** |
| ~~`capabilities.sqlplus_version` 恒为空~~ | ~~`sqlplus -V` 输出走 stderr 被 `2>/dev/null` 丢弃~~ → `2>&1` + 只提取 `Release <版本>` 号 | N-9 | ✅ **批 6** |
| ~~`host_identity` 里 `is_local_target` 与 `database_target_is_local` 同义重复~~ | ~~同一个布尔两个键并列~~ → 删重复键，唯一权威 = `database_target_is_local` | N-10 | ✅ **批 6** |
| ~~`manifest.json` 不自列（305/306）却无声明~~ | ~~消费者会以为"漏列了一个文件"~~ → 头部显式 `self_excluded` + 原因 | N-11 | ✅ **批 6** |
| ~~`system.listener_status` 判 `error`，实为 listener 未运行~~ | ~~本机 bequeath 采集下 listener 没起 ≠ 采集失败~~ → `TNS-12541`/`TNS-00511`/`no listener` 归 `not_enabled` | N-13 | ✅ **批 6** |
| ~~`system.ifconfig` / `system.ntpq_peers` 是 `elif` 分支 ⇒ 本机永不出现~~ | ~~下游分不清"环境不适用"与"没采到"~~ → 未采用的备选显式记 `skipped` + capabilities 补 `ip_command`/`ifconfig_command` | N-14 | ✅ **批 6** |
| ~~归档一步写**两条** status（`oracle.module.structured_packaging` 恒 ok + `module.structured_packaging` 真 rc）~~ | ~~① 控制台"包内台账 = 总数 − 1"其实错（实际 − 2）；② 该 id 不合 `oracle.module.*` 约定；③ tar 失败时两条结论**相反**~~ → `run_module` 成**唯一记账者**，`collect_structured_package` 只 `return` 真实 rc（30/31）+ 回传 `MODULE_REASON` | N-16（复采包体检） | ✅ **批 7** |
| ~~`snapshot.collection_summary.total`(158) ≠ `collection_status.json.summary.total`(159)~~ | ~~snapshot 生成在 `security_scan` **之前** ⇒ 少一条 `package.security_scan`；"三处同源"这句话是假的~~ → snapshot 挪到台账**之后**，三处真正同时点同口径 | N-17 | ✅ **批 7** |
| ~~`record_status` 参数错位 4 处（少传 `exit_code`）~~ | ~~说明文字挤进 `output_file` 列、`reason` 空，`output_file` 指向**不存在**的"文件"。**编译期全绿、运行不报错**~~ → 补 `0` + **生成期 arity 守门**（`_scan_record_status.py`：实参 ≠ 10 即拒绝生成自测）+ T25 运行期复跑 | N-18 | ✅ **批 7** |
| ~~集合型项的 `output_file` 写**目录**（`history/` / `timeseries/`）~~ | ~~违反"必须指向包内存在的文件"；判 empty 时目录里可能根本没有产物~~ → `first_existing_relpath` 取**代表文件**，无产物写空串 | N-19 | ✅ **批 7** |
| ~~`tables/kernel_parameters.tsv` 无表头~~ | ~~其余真 TSV 都有、只有它没有 ⇒ "首行即列名行"对它是假的~~ → 补 `path\tvalue` 表头；**顺带修**：缺 `sysctl` 时**无任何记录** → 显式 `unsupported`（与 N-14 同类） | N-20 | ✅ **批 7** |
| ~~interface 的 `partial` 行过期（"目前只有 `timeseries.realtime_sampling`"）~~ | ~~实测 `system.sar_history` 也会 `partial`~~ | N-21 | ✅ **批 7**（本文档自身修正） |

### 10.2 建议的重构顺序

1. **批 0**（零逻辑改动）：OF-24 注释补全 + OF-28。—— ✅ **已完成（2026-10-09）**。
2. **批 3**（不动契约）：OF-05、OF-10、OF-17 —— 安全与门禁。—— ✅ **已完成（2026-10-09）**。Python 解析逻辑无需改动；**但退出码语义从本批起可用**（见 §8）。
3. **批 2**（只增量）：OF-06、OF-07、OF-08、OF-11、OF-12、OF-09、OF-15、OF-18 —— 新增文件/字段，不改既有文件名与列名。—— ✅ **已完成（2026-10-09）**。构造实测 PASS=20/FAIL=0（见 §11.5）；`snapshot.json` +4 字段、结构化 OS 项 20→30、md 报告变薄。
4. **批 1**（**主版本，一把做掉**）：OF-01、OF-02、OF-03、OF-04、OF-13、OF-16/23、OF-20、OF-22、OF-27（并把 OF-21 版本单一来源、OF-25 模块清单单一来源**前置**到本批）→ `SNAPSHOT_SCHEMA_VERSION` 升到 **2.0** → 同步本文 → 改 Python adapter。—— ✅ **已完成（2026-10-09）**。构造实测**当时 PASS=34 / FAIL=0**（同一支脚本批 4 后扩到 T13 → **56**，见 §11.6）；`collector/schema/package_version` 统一 **2.0**。
5. **批 4**（一致性与体验）：OF-14、OF-19、OF-26（+ 本批新发现 OF-30）。—— ✅ **已完成（2026-10-10）**。构造实测 **PASS=56 / FAIL=0**（同一支 `_work/_verify_b1.sh`，已扩到 T13）。
6. **批 5**（**数据正确性回归 + 台账单一口径**）：N-1、N-2、N-3、N-4、N-5 + md 报告归属（**方案 B**）。—— ✅ **已完成（2026-10-10）**。构造实测 **PASS=84 / FAIL=0**（同一支脚本，扩到 **T17**）。**N-4 必须用同型号实例复采一份**确认 ORA-00904 消失（本机无 Oracle 可验）。
7. **批 6**（P2 一致性收尾）：N-6、N-7、N-8、N-9、N-10、N-11、N-13、N-14（+ N-15 文档说明）。—— ✅ **已完成（2026-10-10）**。构造实测 **PASS=107 / FAIL=0**（同一支脚本，扩到 **T22**）。**唯一需要现场复核的是 N-8 的落点变更对 Python 侧的可见影响**（见 §11.8 末尾）。
8. **批 7**（**台账口径收口 + 参数错位守门**，来自第二次复采包的体检）：N-16、N-17、N-18、N-19、N-20、N-21。—— ✅ **已完成（2026-10-10）**。构造实测 **PASS=139 / FAIL=0**（同一支脚本，扩到 **T26**）。**新增第四道静态守门**：`record_status` 实参个数必须 = 10（完备判定在生成期，运行期 T25 复跑一次）。**仍需现场确认**：用同型号实例复采一份，核对控制台「执行模块: 28 / 包内台账 = 总数 − 1」与 `snapshot.collection_summary.total == collection_status.json.summary.total`。
9. **批 8**（**未做**）：`plugins/oracle/package_adapter.py` 按 `collector_version` 分叉 —— 它现在对**所有** `tables/*.tsv` 一律 `parse_delimited(path, "|")`，2.0 的 5 个**真 TAB** 表（`capabilities` / `hugepages` / `kernel_parameters` / `oracle_limits` / `oracle_env`）同样被错解（见 §11.8 末尾的"给 Python 侧的提醒"）。

**顺序的硬约束**：批 1 里 OF-16/23（生成 `collection_status.json`）**必须排在 OF-01 之后** —— 台账还塌陷着，生成的 items[] 就是错的（**已遵守**：批 1 内先改 id 再生成台账）。

### 10.3 落 `collection_status.json` 时的目标结构（与 MySQL 同构）

```json
{
  "schema_version": "2.0",
  "items": [
    {"item_id":"oracle.basic.database","category":"oracle.basic","status":"ok",
     "started_at":"…","finished_at":"…","duration_ms":123,"row_count":1,
     "exit_code":0,"output_file":"tables/oracle.basic.database.psv","reason":""}
  ],
  "summary": {"ok":0,"empty":0,"unsupported":0,"not_enabled":0,"not_applicable":0,
              "permission_denied":0,"timeout":0,"error":0,"skipped":0,"partial":0,"schema_mismatch":0}
}
```

**`items[]` 的顺序**：按 `started_at`（等值时按 `item_id` 字典序）**排序输出**，不要依赖 `ls` 的返回顺序 —— 文件系统顺序在不同机器上不一致，会让同一份采集包在不同机器上产出不同的 json。

---

## 11. 实测验证包（2026-10-09 基线）

本节把一份**真实 Oracle 采集包**的实测结果固化下来，作为后续改造的**前后对照基线**。

**包**：`oracle_inspection_task_ZYDB_db01_192.168.100.80_1521_zydb1_2026-10-09_22-47-04.tar.gz`
**规模**：211 文件 / 438 KB / `collector_version` = **1.4**（与 `inspection/oracle_inspection.sh` 工作副本一致）

### 11.1 目录实况（与 §5 对照）

```
oracle_inspection_task_ZYDB_…_2026-10-09_22-47-04/
├── snapshot.json           21 字段（§6.1）
├── manifest.json           210 个 files[] 条目，仅 {path,size_bytes,sha256}
├── tables/                 104 个：98 SQL 结果（中文名）+ 6 OS 结构化（英文名）
├── evidence/                12 个：chronyc_sources / chronyc_tracking / dmesg_errors /
│                                   ip_address / ip_route / lscpu / mounts / numa /
│                                   os_release / socket_summary / time_status / timedatectl
├── status/                  79 个：78 个 *.tsv + sar_files.txt
│                                 ├─ sql.*.tsv       47   ← 应 136+
│                                 ├─ module.*.tsv    11   ← 应 27
│                                 ├─ system.*.tsv    19
│                                 └─ timeseries.*.tsv 1
├── timeseries/               5 个：oracle_sysstat / system_cpu / system_disk /
│                                   system_memory / system_network
└── history/                  9 个：coverage.tsv / source_files.txt / sar_*.csv ×7
```

### 11.2 关键计数（改造后必须逐项对照）

| 指标 | 本包实测 | 改造后应为 |
|---|---:|---|
| `status/sql.*.tsv` 数 | **47** | 136（或等于实际执行的 SQL 项数） |
| `status/module.*.tsv` 数 | **11** | 27 |
| `tables/` SQL 结果文件数 | **98** | 与 status 的 sql 项数一致 |
| 空结果文件数（`# no rows returned`） | **34** | 同，但 status 应记 `empty` 而非 `ok` |
| `status` 里 `empty` 的数量 | **0** | 34 |
| `awk -F'\t' '{print $1}' status/*.tsv \| sort \| uniq -d` | **有输出**（塌陷） | 空 |

### 11.3 环境事实（判读这份包的前提）

| 项 | 值 |
|---|---|
| 实例 | ZYDB / zydb1 / db01 / `19.16.0.0.0` |
| 形态 | 单机（`is_rac=0`）/ **无 ASM**（`has_asm=0`）/ 非 CDB / PRIMARY |
| `v$instance.startup_time` | `2026-10-09 22:45:17`（**采集时刚启动 2 分钟**） |
| 采集耗时 | 42 秒（22:47:04 → 22:47:46） |
| 采集方式 | 本地（`host=127.0.0.1`，OS 认证） |
| OS | 4 vCPU / 4.08 GB RAM（使用率 88%）/ swap 4 GB（用 24 MB） |
| sar 源 | 仅 `/var/log/sa/sa09` 一个文件（`coverage=partial`） |
| 大页 | `nr_hugepages=0`（未启用） |

> ⚠️ **实例刚启动** 是本包 34 个空结果的**主因**（RMAN 历史、Scheduler 作业、归档量等确实为空），
> **不是采集缺陷**。评估"采集覆盖度"时要扣掉这一层；评估"状态台账 / 文件格式"不受影响。

### 11.4 由本包新发现的契约缺陷

| 编号 | 缺陷 | 位置 |
|---|---|---|
| 实测-1 | `status="empty"` 在 `ora_sql_to_md` 路径**永不触发**（判据读 stderr，消息在 stdout） | FIX-PLAN §0.5.3 |
| 实测-2 | `tables/` 用 `tsv_filename()`、`status/` 用 `sanitize_filename()`，**两套规则永不相等** | FIX-PLAN §0.5.3 |
| 实测-3 | `module` 层只塌文件名（内容可恢复），`sql` 层内容一起塌（不可恢复） | FIX-PLAN §0.5.3 |
| 口径-1 | `ora_version` 两处不一致（`snapshot`=19.16.0.0.0 vs `实例信息.tsv`=19.0.0.0.0） | FIX-PLAN §0.5.4 |
| 口径-2 | `collection_started_at` 用 `_` 分隔，`status`/`manifest` 用 ISO 8601 | 本文 §6.1 |

### 11.5 批 2 改造后的增量（2026-10-09）

批 2（OS 采集）改完后，**用同型号实例再采一份**应看到下列增量。本机无 Oracle，新增文件与解析逻辑经**构造验证**（`_work/_verify_b2os.sh`：**PASS=20 / FAIL=0**）：

| 增量 | 内容 |
|---|---|
| `snapshot.json` **+4 字段** | `database_target_is_local`（bool）/ `collector_hostname` / `target_resolved_ip` / `time_evidence{}`（`timezone` / `ntp_synchronized` …）→ 21 变 **25** |
| `tables/` **+2 表** | `oracle_limits.tsv`（4 列）、`oracle_env.tsv`；`hugepages.tsv` 增 6 行 `/proc/meminfo:` 实测值 |
| `evidence/` **+7 文件** | `oracle_actual_limits.txt` / `oracle_processes.txt` / `asm_multipath.txt` / `oratab.txt` / `net_error_counters.txt` / `ipcs_shm.txt` / `listener_status.txt` |
| `status/` **+10 项** | 上述新增 item_id（均 `category = system.static`），合计 **30 项** |
| `history/` **+2 文件** | `sar_process.csv`（`sadf -w`）+ `coverage.json`（7 字段，与 MySQL 完全对齐） |
| Markdown 报告 | `collect_sys_info` 的「网络与磁盘 / IO / limits / 进程 / 监听 / 环境变量」6 段由**原始命令输出**改为**指向结构化包的指引**（OF-15：md 展示、包数据） |
| 超时 | 13 处外部命令套 `run_with_timeout`（lsnrctl 10s；chronyc / crsctl / ocrcheck / gpnptool / multipath / oracleasm 15s；dmesg 20s；numactl 10s） |

> ⚠️ **Markdown 报告的取舍（要留意）**：`collect_sys_info` 从「贴原始命令输出」改为「贴指引」，**只看 md 报告的客户会少看到网络/磁盘/limits/进程明细**（明细现在只在结构化包里）。若现场只交 md，需要回头评估是否保留一份摘要。

**验收命令（可复跑）**：

```bash
bash _work/_verify_b2os.sh                                  # 批 2 综合（OF-06/07/09/11/12/18）
bash -n inspection/oracle_inspection.sh                     # 语法
grep -c 'run_with_timeout' inspection/oracle_inspection.sh  # 13 处调用 + 1 处定义
```

### 11.6 批 1 + 批 4 改造后的增量（2026-10-10，collector/schema/package 统一 2.0）

批 1（数据正确性 + 结构化契约）与批 4（一致性与体验）改完后，**契约层面**的净变化如下。**这是 1.4 → 2.0 的破坏性变更清单，Python adapter 必须按 `collector_version` 分叉**。

| 增量 | 内容 |
|---|---|
| `tables/*` → **`tables/<item_id>.psv`** | 文件名 = ASCII 点分 id（不再中文标题）；内容 = `psv_normalize` 规范化（竖线分隔、两侧空白剥净、无空行/无 `---` 行）；首行即列名行；空结果写自描述占位 `# oracle-inspection psv-empty …` |
| `status/*.tsv` 不再塌陷 | `item_id` 显式 ASCII；`sql` 项 `row_count` = 真实数据行数、`output_file` = `tables/<item_id>.psv` |
| **`collection_status.json`**（新） | `items[]`（每项 `item_id`/`category`/`status`/`duration_ms`/`row_count`/`exit_code`/`output_file`/`reason`）+ `summary{}`（按 10 个 status 计数） |
| **`summary.txt`**（新） | 可读摘要，含"耗时最长前 10" |
| **`tables/capabilities.tsv`**（新） | 命令可用性 + 4 个视图探针；**tsv 里布尔写 `0/1`，`snapshot.json.capabilities` 里写 `true/false`**，字符串原样 |
| **`snapshot.json` 改为 8 段** | `collector` / `host_identity` / `capabilities` / `time_evidence` / `sampling` / `privacy` / `collection_summary` / `artifacts`。★ **`database_target_is_local` 由顶层移入 `host_identity`** |
| **`logs/modules/<category>.log`**（新） | 模块错误**完整原文**（OF-26）；status 的 `reason` 仍只 5 行摘要 |
| **`COLLECTION_INCOMPLETE`**（新） | 仅中断时出现，首行 `partial`（OF-19） |
| `status` 取值 9 → **10** | 新增 `schema_mismatch`（`ORA-00904` / `ORA-01722` 等，分类顺序在 `unsupported` **之前**） |
| 退出码 | 批 3 起生效（§8）；批 4 把"输出目录不可写"并入 **10** |
| CLI | 新增 `--output-dir=DIR` / `--output-dir DIR`（默认 `/var/tmp`，OF-14） |
| 中间文件不外泄 | 临时工作目录移到 `DIR/.oracle_inspection_tmp_*`（**TASK_DIR 之外**），正常结束与中断都会删 —— 包/manifest 里不会再出现 `oracle_stat_*.tsv` |

**验收命令（可复跑，本机无 Oracle 亦可）**：

```bash
python _work/_b1_make_verify.py            # 生成自测脚本（内含两道静态守门）
bash _work/_verify_b1.sh                   # 批 0–21 综合：T1–T27，PASS=154 / FAIL=0（不连真库；Windows Git Bash 下较慢）
python _work/_scan_record_status.py inspection/oracle_inspection.sh   # 第四道守门（record_status arity，须 rc=0）
bash -n inspection/oracle_inspection.sh    # 语法
```

### 11.7 批 5 改造后的增量（2026-10-10，**来自一份新采的真实 2.0 包**的体检）

样本：`oracle_inspection_task_ZYDB_db01_192.168.100.80_1521_zydb1_2026-10-10_09-40-37.tar.gz`（61 KB / 306 文件 / 156 项）。**批 1–4 的成果在这份包上逐条被证实**（156 个唯一 `item_id` 零重名、34 项 `empty` 真实触发、156 条 `output_file` 无一指向不存在的文件、98 个 `.psv` 与 98 条 SQL 项 100% 同名、`manifest.json` 305 文件 sha256 **全对**、snapshot 8 段齐全、`logs/modules/*.log` 有完整原文），同时暴露 5 个 P1 → 批 5。**契约层面**的净变化：

| 增量 | 内容 |
|---|---|
| **`psv-unavailable`**（新形态） | 失败项不再落 sqlplus 报错回显，改落 `# oracle-inspection psv-unavailable item_id=… sep=\| rows=0 status=<status>`，且 `row_count=0`（N-2）。**`.psv` 自此只有三种形态**（§6.3 表） |
| `row_count` 语义收紧 | `ok` = 真实数据行数；`empty` = 0；**其余状态一律 0**（不再出现"报错回显的行数"）。**`row_count=0` 仍然不等于"该指标为 0"** |
| **`packaging_status.json`**（新） | 归档前自检结论（`phase`/`security_scan`/`archive_planned`/`note`），见 §6.5 |
| `status/` 里多了 `package.security_scan.tsv` | 批 5 起它**真的在包里**（`sync_status_to_package`），且进 `collection_status.json.items[]` 与 `manifest.json`（N-3） |
| `summary.txt` 有**分档自校验行** | `分档合计 / 总数 : N / N`（不等时标 `⚠ 不一致`）；补上了 `partial` 行（N-5） |
| 控制台输出格式变了 | `采集项: N (成功 a 空结果 b 失败 c 部分成功 d 跳过 e)` + `失败细分: error … timeout … 权限不足 … 环境不支持 … 脚本/版本不匹配 … 未启用 …` + 一行**包内台账 = 总数 − 1** 的说明。**下游若要解析控制台文本，请注意格式已变**（更稳的做法：只读包内 `summary.txt` / `collection_status.json`） |
| `snapshot.artifacts` 字段改名 | `report_md` → **`report_md_external`**（值为**采集机上的绝对路径**）+ 新增 **`report_md_scope:"outside-package"`** + 新增 `packaging_status`（方案 B） |
| 台账口径单一来源 | 尾部 / `snapshot.collection_summary` / `collection_status.json.summary` 三处**复用 `refresh_status_tally()`**；控制台不再另算一遍（N-5） |

> **`collector_version` / `schema_version` 仍为 2.0**：批 5 没有破坏"文件在哪、叫什么"的契约（唯一字段改名是 `report_md` → `report_md_external`，且**仓库内无消费者**，已 `grep` 确认）。**但 adapter 若读过 `artifacts.report_md`，必须同步改名。**

> **两道静态守门（OF-29 的教训制度化）**：`_b1_make_verify.py` 在生成自测脚本**之前**扫真实脚本的**未加引号 heredoc 体**，
> ① 出现**反引号**即拒绝生成（会被 bash 当命令执行 → 正常项状态被打成 `error`）；
> ② 出现**"词内美元"**（`v$xxx` / `x$bh`，即 `$` 紧跟在字母/数字/下划线之后且后面不是 `{`）即拒绝生成
> （会被静默展开成空 → 批 1 里 `v$parameter.value` 被吞成 `v.value`）。
> 这两类问题 `bash -n` **全绿**，只在运行期留痕，所以必须静态守。

### 11.8 批 6 改造后的增量（2026-10-10，P2 一致性收尾）

本轮**不含新采集项**（`item_id` 数量不变：136 SQL + 31 OS + 28 模块），全部是**落点 / 口径 / 声明**层面的一致性修正；
`collector_version` / `schema_version` / `package_version` **仍为 2.0**。契约层面的净变化：

| 增量 | 内容 | 影响下游？ |
|---|---|:---:|
| **`tables/` 契约收窄**（N-8） | 4 个"扩展名说谎"的对齐文本改归 `evidence/*.txt`：`memory_snapshot` / `filesystems` / `inodes` / `block_devices`。`tables/` 只剩 `.psv`（SQL 结果）与 5 个**真 TAB** `.tsv` | ★ **是** |
| `status/` 更纯（N-7） | 不再有 `sar_files.txt`（移入 `RUNTIME[TMP_DIR]`，不进包）；`history/source_files.txt` 是唯一留档 | 否 |
| 控制台的「执行模块」变准（N-6） | 2 个 bg 模块（`sar_history` / `realtime_sampling`）也计入 → **26 → 28**（= `MODULE_PLAN` 长度） | 否（人读输出） |
| `capabilities.sqlplus_version` 有值了（N-9） | 口径 = 纯版本号（如 `19.16.0.0.0`）；取不到时为空串（**不是** `-`） | 是 |
| `host_identity` 去重（N-10） | 删 `is_local_target`，**唯一权威 = `database_target_is_local`**。注意 `capabilities.is_local_target` 仍在（能力位，同值） | ★ **是** |
| `manifest.json` 显式声明自排除（N-11） | 头部新增 `self_excluded:["manifest.json"]` + `self_excluded_reason` | 否（新增字段） |
| `system.listener_status` 语义修正（N-13） | listener 未运行时判 `not_enabled`（不再是 `error`） | 是 |
| 互斥项显式登记（N-14） | `system.ifconfig` / `system.ntpq_peers` 未采用时记 `skipped`（reason 含 `mutually-exclusive fallback`）；capabilities 新增 `ip_command` / `ifconfig_command` | 是 |
| `summary.txt` 多三行说明（N-15） | "耗时最长前 10"下方注明 `oracle.module.realtime_sampling` 与 `timeseries.realtime_sampling` 是同一动作的两个视图 | 否（人读） |

> ★★ **给 Python 侧的提醒（批 6 唯一的"可见行为变化"）** ——
> `plugins/oracle/package_adapter.py` 现在是：
> ```python
> for path in (root / "tables").glob("*.tsv"):
>     ctx.tables[path.stem] = parse_delimited(path, "|")
> ```
> 也就是说 `ctx.tables["filesystems"]` 此前是**用 `|` 去切 `df` 的列对齐文本**得到的错位行
> （实测：键 = 整行、值 = 下一行；`row.get("USE%")` 恒 `None`），而 `oracle_rules.py::_check_filesystem_usage`
> 又用 `bool(fs_rows)` 当"有没有数据"。批 6 之后这个键**不再存在** → 该处 `bool(fs_rows)` 由 `True` 变 `False`。
> **两者都不可信** —— 这不是"改坏了"，而是把问题**暴露到明面上**。对接 2.0 包时应：
> ① 按 `status/*.tsv` 的 `output_file` 列去读 `evidence/filesystems.txt` 并做一次字段抽取；或
> ② 先用 `status/system.filesystems.tsv` 的 `status`/`row_count` 判可用性，`evidence/` 原文仅作展示。
> **另注**：同一个 adapter 对 `tables/*.tsv`（**TAB 分隔**）也一律用 `|` 解析 —— 那是**另一处待修**
> （属"adapter 按 `collector_version` 分叉"这项独立工作），批 6 未动，已在 `fix-plan` 记为**批 8 待办**
> （**编号说明**：原记为"批 7"，后批 7 被 §11.9 的 N-16…N-21 占用，故顺延为批 8）。

### 11.9 批 7 改造后的增量（2026-10-10，第二次复采包的体检 → 台账口径收口）

样本：`oracle_inspection_task_ZYDB_db01_192.168.100.80_1521_zydb1_2026-10-10_10-43-30.tar.gz`
（**批 5 / 批 6 修完后的第一次现场复采**，316 条目 / 161 项）。体检报告 = `_work/_pkg3_check_report.md`；
解包目录 = `_work/_pkg3/`。**批 5 与批 6 的修复在这份包上逐条被证实生效**（三项关键行为
`recent_errors` error→ok、`rman_validate` schema_mismatch→empty、`listener_status` error→not_enabled；
另 8 项落点/口径也全部到位），但**又暴露 3 个 P1 + 3 个 P2** → 批 7。

本轮**不新增采集项**（`item_id` 数量不变：136 SQL + 31 OS + 28 模块），`collector_version` /
`schema_version` / `package_version` **仍为 2.0**（无破坏性字段改名）。契约层面的净变化：

| 增量 | 内容 | 影响下游？ |
|---|---|:---:|
| **归档模块只剩一条 status**（N-16） | `module.structured_packaging`（不合规 id）**消失**；`oracle.module.structured_packaging` 是唯一记录，且带上真实 `exit_code`（30/31）与 `reason` | ★ **是**（`status/` 里少一个文件） |
| 控制台差额文案恢复正确（N-16） | 「包内台账 = 总数 − 1」由**错的**变**对的**（旧版实际差 2） | 否（人读） |
| **`snapshot.collection_summary.total` 与 `collection_status.json.summary.total` 相等**（N-17） | snapshot 挪到台账之后生成；不再是 158 vs 159 | ★ **是**（第一入口的计数终于可信） |
| 4 处 `output_file` 从"说明文字"变回**空串**（N-18） | `system.ntpq_peers` / `system.ifconfig` / `system.sar_history`(skipped) / 模块 skipped 的 `output_file` 不再是 reason 文案 | 是（列值语义修正） |
| 集合型项 `output_file` 指向**文件**（N-19） | `system.sar_history` → `history/coverage.json`；`timeseries.realtime_sampling` → `timeseries/oracle_sysstat.csv`；无产物时空串 | ★ **是**（不再有目录值） |
| `kernel_parameters.tsv` 有表头（N-20） | 首行 `path<TAB>value`；缺 `sysctl` 时新增一条 `system.sysctl_selected = unsupported`（旧版**什么都不记**） | 是 |

> ★★ **给 Python 侧的提醒（本轮唯一"看着像改坏了、其实是修好了"的地方）** ——
> 上一轮（批 6）把 `tables/*.tsv` 收窄为 5 个真 TAB 表，本轮又把两个**集合型项**的 `output_file`
> 从目录改成文件。**这两处都在"减少下游按旧假设能读到的东西"**，但旧假设本身就是错的：
> `ctx.tables["filesystems"]` 是**用 `|` 切 `df` 对齐文本**得到的错位行（详见 §11.8 末尾），
> `output_file="history/"` 是**目录**、`os.path.isfile` 命不中。对接 2.0 包时请统一按
> §6.2 的 `output_file` 语义（"包内真实存在的文件"）判可用性，不要靠扩展名或"非空即文件"猜。
>
> ★ **本轮新增的守门（第四道，制度化）**：`record_status` 的**位置参数个数必须 = 10**。
> 判据是**按 shell 引号规则切词后数实参**（`_work/_scan_record_status.py`），在**生成期**强制
> —— 命中即拒绝生成 `_work/_verify_b1.sh`。之所以不能靠"编译/运行"发现：少传一个参数
> **`bash -n` 全绿、运行不抛异常**，只是把后面实参整体串位；只有真包或静态对账能看出来。
> ⚠ 编写廉价特征断言时注意：合法的 10 参形态 `… 0 0 0 "" "reason"` 其**子串**里也含 `0 0 ""`
> （末三个 token），所以"`grep -c '0 0 ""' == 0`"是**假阳性**判据（本守门初版实测误报 5 处）。

### 11.10 批 8 改造后的增量（2026-10-10，**Python 侧**；采集脚本零改动）

本轮**只动 Python**（`plugins/oracle/package_adapter.py` + 新增 `item_titles.json` / 生成器 / 测试），
`collector_version` / `schema_version` / `package_version` **仍为 2.0**，**契约本身一字未改** ——
改的是"下游之前**没有真正**按这份契约读包"。这张表写给"下一个接手 Python 侧的人"：

| 上下文 | 你应当知道的 |
|---|---|
| **`.psv` 必须读** | 改前 `glob("*.tsv")` 把 **98 个 `.psv` 全漏了**；1.4→2.0 只改了扩展名，下游没跟着改 |
| **`.tsv` 是 TAB** | 2.0 的 5 个 `.tsv`（§6.3）是真 TAB；1.4 的 `.tsv` 才是 `\|` —— 按 `collector_version` 分派，别按扩展名猜 |
| **`item_id` 别靠文件名** | `tables/kernel_parameters.tsv` ↔ `system.sysctl_selected` **不同名**；用 `status/*.tsv` 的 `output_file` 反查 |
| **占位 = 空表** | `# … psv-empty` 与 `# … psv-unavailable` 都返回空表；两者区别只在 `status` 列 |
| **标题表的单一来源** | `plugins/oracle/item_titles.json` 由 `tools/gen_oracle_item_titles.py` **从 `inspection/oracle_inspection.sh` 生成**；测试 `test_generated_map_is_in_sync_with_collector` 守门。**改采集脚本的标题后必须重生成** |
| **`LEGACY_ALIASES` 的用途** | 15 个 1.4 键在 2.0 改了名（`asm_diskgroup_summary` / `最近RMAN备份任务状态` / `无效_不可用索引` …）→ 桥接。测试断言每个 target 都在标题表里 |
| **`evidence/` 只回填了一项** | `system.filesystems` ← `evidence/filesystems.txt`（`df -PT`）；其余 22 项按契约仍只作原文展示 |
| **实测收益** | 同一个真实 2.0 包：`ctx.tables` 键数 **5 → 416**、规则 `not_evaluated` **16 → 2**、覆盖率 **53% → 94%**、findings **2 → 12** |

> ★★ **给"下一个接手人"的一条元教训**：**改适配器之前先量一遍**。
> `len(ctx.tables)` / 规则 `not_evaluated` 计数 / 下游查找键的命中率，这三个数一打出来，
> "改什么、改完算不算好"就不用猜了。本轮差点只按文档改分隔符（那是"半修"，`not_evaluated` 仍是 16）。
>
> ★ 另一条：**验证脚本自己会说谎**。第一版覆盖检查写成 `key in ctx.tables and ctx.tables[key]`，
> 把 21 处"键已注册但表为空（`psv-empty`）"误报成"未注册"。**空表是正确状态，不是缺失** ——
> 判"查找键能不能命中"只看键在不在，行数交给规则判。

### 11.11 批 10 改造后的增量（2026-10-10，**Python 侧分析 / 报告层**；契约本身一字未改）

本轮**只动 Python**（`plugins/oracle/presentation.py` / `analyzer.py` / `word_report.py` + 新增
`tests/test_oracle_confirmations.py`），**采集包契约、目录树、`status` 语义全部不变** ——
改的是"下游**产出的报告**少了什么"。这张表写给"下一个接手报告侧的人"：

| 你应当知道的 | 说明 |
|---|---|
| **报告模型的顶层键又多了一个** | `pending_confirmations`（**条件注入**：旧基线模型没有该键，无条件写入会打穿冻结契约）。它由 `presentation._merged_confirmations(instances)` 从**实例上**的 `pending_confirmations` 合并，而实例上的键由 `analyzer.attach_pending_confirmations()` 挂上 |
| **它派生自「已触发的规则」** | 模板 `presentation.CONFIRMATION_TEMPLATES`（11 条 `rule_id → (主题, 待确认内容)`）。规则未触发则不进表 —— 不把模板空转成一张"欢迎确认"清单 |
| **只收「须客户拍板 / 须客户提供外部证据」的** | 业务语义决策（未开自动扩展是有意还是遗漏、哪些是高权限业务账号）、无法自证的（备份可恢复性、坏块修复容忍度）、要外部资源或停机窗口的（扩容、开归档）。**纯技术整改项不进表** |
| **`None` 与 `[]` 必须可区分** | `None` = 未产出清单（旧模型）；`[]` = 本次没有待确认事项。延续「未采集 ≠ 0」 |
| **章节动线变了** | `word_report.ORACLE_WORD_PROFILE.section_order`：`oracle_instance` 提到 `storage_capacity` **之前**（先知道"实例是什么"，再看"它装了什么"），对齐 MySQL 的 实例 → 配置 → 运行 → 容量。**目录页与正文 Heading1 逐条一致** |
| **守门** | `tests/test_oracle_confirmations.py`（9 用例）。★ 其中 `test_every_rule_id_exists_in_the_rule_set` 钉住"模板引用的 rule_id 必须真实存在于 `inspection_rules_oracle.json`" —— 写了不存在的 rule_id 会让该模板**永不触发**，而没有任何测试会红 |

> ★★ **给"下一个接手人"的一条元教训**：**「报告里没看到某章」不等于「这项没问题」** ——
> 改前 DOCX 第 14 章只有一句"本次分析未产出待确认事项清单"，读起来像"本次没有"，实际是**上游根本没产这个键**。
> 判断方法：**端点跑一遍再拆 DOCX**（`cli.py` → `generate_report.py` → `python-docx` 读回），
> 而不是只看 `report_model.json` —— 后者是 presentation 的**原始信封**，Word 走的是
> `report_adapter.adapt_report_model` 之后的 `report_model_standard.json`。
>
> ★ 另一条：**适配器是「`deepcopy(source)` + 补」，不是透传也不是重建**。source 里没有的键它**不会造** ——
> 所以"新增一个报告章节"必须**从 presentation 的信封开始改**，改 adapter 是白改。

### 11.12 批 11 改造后的增量（2026-10-10，**Python 侧规则层**；契约本身一字未改）

本轮**只动 Python**（`plugins/oracle/inspection_rules_oracle.json` / `oracle_rules.py` + 新增
`tests/test_oracle_rule_gaps.py`，并整体刷新 `tests/baselines/oracle/current/`），
`collector_version` / `schema_version` / `package_version` **仍为 2.0**，**采集脚本一行没动** ——
改的是"下游**对已有的包数据没判**"（采了有数据、零判据）。这张表写给"下一个接手规则侧的人"：

| 你应当知道的 | 说明 |
|---|---|
| **新规则消费的表 = 包里本来就有** | 8 条规则全部落在**已采集**的域上，**没有新增采集项、没有改 item_id**；规则读的表键一律**同时给 `item_id`（2.0）与中文标题（1.4）**，两套包都命中 |
| **DataGuard 规则的取数键** | `oracle.dg.archive_dest`（`ERROR` 列）/ `oracle.dg.dataguard_stats`（`VALUE` 列是 `+DD HH:MM:SS` 间隔串，需解析成秒）/ `oracle.dg.log_sequence_gap`（`GAP` 列）/ `oracle.dg.recent_events`（`ERROR_CODE` 列） |
| **★ 无备库 ≠ 延迟正常** | 未配 DG 的库上 `v$dataguard_stats` / `v$log_sequence_gap` **为空** ⇒ `ORA.DG.APPLY_LAG` / `ORA.DG.LOG_GAP` 落 **`not_evaluated`**（延续「未采集 ≠ 0」），**不是 `passed`** —— 否则报告会宣称"备库延迟正常"，而事实是没有备库 |
| **对象治理按「入包行」判** | `status/*.tsv` 第 6 列（`row_count`）是**库内真实行数**、第 7 列是**入包行数**（TOP-N 截断）。`awr.segment_bloat` 库内 802 行、入包仅 4 行 —— 规则按入包行判，并把"N 条候选"写进 fact，**别把 802 当成缺陷** |
| **`ORA.CAPACITY.LARGE_SEGMENT` 的列名差异** | 表段用 `SIZE_MB`、索引段用 `SIZE_GB`；规则把索引的 `SIZE_MB` 折算成 GB 再与阈值比 |
| **★ 大页口径与 MySQL 相反** | MySQL 只要求**关掉** THP（`enabled == "always"` 才触发）；Oracle 还要把 SGA 放进 HugePages，故 `ORA.SYSTEM.HUGEPAGES` 多出**半条**判据：`HugePages_Total > 0 且 Free/Total > hugepage_free_ratio_warning` = "配了却闲置"。照抄 MySQL 会漏掉 Oracle 最典型的那半 |
| **`hugepages` 表是「路径 → 值」形态** | 由 `_hugepage_states(ctx)` 归一：`nr_hugepages` / `meminfo:HugePages_Total` / `meminfo:HugePages_Free` / `transparent_hugepage/{enabled,defrag}`（THP 取 `[方括号]` 标注的当前值） |
| **新增 5 个阈值键** | `dg_lag_seconds_warning`(900) / `dg_log_gap_warning`(5) / `segment_bloat_mb_warning`(1024) / `large_segment_gb_warning`(100) / `hugepage_free_ratio_warning`(0.5)，全部**有代码请求**，受 `tests/test_rule_pack_config.py` 守门 |
| **实测收益** | 真实 2.0 包：规则 **34 → 42**、触发 10 → 13 / 通过 20 → 25、findings 12 → 13（新触发的只有 `ORA.SECURITY.PASSWORD_PROFILE`）、健康分 13 → 10；`sections` 9 / `items` 52 **不变**（新规则只加 finding，不改报告结构） |

> ★★ **给"下一个接手人"的一条元教训**：**「采了但没判」是机械可查的缺口，不靠感觉。**
> 判据两条：① 该采集项在包内**有数据**（`status=ok` / 有行）；② 在 `oracle_rules.py` 里**找不到任何消费者**。
> 写规则前先用 `_work/_b11_probe.py` 把 `status` + `output_file` + **真实列名** + 样例行打出来 ——
> 列名对不上是静默的（`row.get("SIZE_GB")` 返回 `None`，规则照样"通过"），先核实再写。
>
> ★ 另一条：**阈值键声明与请求必须成对**（`tests/test_rule_pack_config.py`）——
> 声明了却没人请求会变成死键，请求了却没声明会静默退回默认值。二级阈值（告警档 + 严重档）两键必须同时具备。

### 11.13 批 12 改造后的增量（2026-10-10，**Python 侧图表层**；契约本身一字未改）

本轮**只动 Python**（`plugins/oracle/charts.py` / `plugins/oracle/word_report.py` /
`inspection_core/word_engine.py` + 新增 `tests/test_oracle_chart_coverage.py`），
**采集包契约、目录树、`status` 语义全部不变** —— 改的是"下游**产图 / 上图**这件事本身的清单与沉默"。
这张表写给"下一个接手图表侧的人"：

| 你应当知道的 | 说明 |
|---|---|
| **图的 id 只有一份** | `plugins/oracle/charts.py::ORACLE_CHART_IDS`（5 张 Oracle 专属图）。`word_report.py` 的 `section_charts` **从它派生**，别再手抄第二份 |
| **`_figure()` 用 chart_id 命名 PNG** | 不再手写文件名；未在 `ORACLE_CHART_IDS` 注册的 id **当场 `ValueError`**（不静默产出一张没人声明的图） |
| **声明集合 = 可产出集合** | **9 = 9**（4 OS + 5 Oracle）。`tests/test_oracle_chart_coverage.py` 断言的是**列表相等（含顺序）**，不是集合相等 —— 顺序即报告里的上图次序 |
| **「数据不足 ⇒ 不产出」≠「从声明里删掉」** | 声明集合**不许跟着数据漂**。单机空闲库上 `oracle_txn_rate`（提交/回滚全零）**不产出但仍被声明** |
| **图一张都没产出时留一行** | `inspection_core.word_engine.render_section_charts`：声明了图却过滤后为空 ⇒ 输出「图表未生成：本章设计有 N 张趋势图（…），本次一份也没产出：…」，**不改小节号**、不渲染空的"趋势图"标题 |
| **跳过原因要翻人话** | `word_engine.CHART_SKIP_REASONS` 把 `insufficient_data_points` / `no_renderer_available` / `matplotlib_not_installed` 翻成中文；**未登记的代码原样带出**（不吞信息） |
| **★ 这条落在四库共用的引擎里** | `inspection_core/word_engine.py` 被 MySQL / PG / Oracle / SQL Server 共用，所以"图全跳过不再静默"对**四个库一起生效**（有意为之 —— 那是同一个病） |
| **对既有报告零影响** | 数据齐全时（本仓库全部基线包都是）该函数走原路径，**一个字节都不变**；MySQL 基线文档的规范化 XML hash 测试原样通过可证 |
| **未做** | `_work/oracle-analysis-gap-review.md` §2.3 的 3 张"有数据没画"的图（表空间使用率 Top-N 条形 / 归档量趋势 / Top SQL Top-N）—— 需要**新图型**（公共层渲染器只有时序折线）→ 记在批 12b |

> ★★ **给"下一个接手人"的一条元教训**：**"两处清单看起来一样"不等于它们同源。**
> 图只有 9 张、名字又都由 `oracle_*` 开头，肉眼比对完全看不出少了一张 —— 少的那张
> （`oracle_txn_rate`）还偏偏在手上所有包里都因"提交速率恰好为零"而不产出，
> **藏在数据巧合后面**。判据只有一个：**两份清单能不能从同一个常量派生**；
> 不能派生，就必须有一条断言去比（而且要比**顺序**，不是集合）。
>
> ★ 另一条：**改共享引擎前先问"另外三个库会怎样"。** `render_section_charts` 四库共用，
> 加一行提示看着无害，但 MySQL 的文档 hash 测试正是靠"一个字节都不变"兜底的 ——
> 这类改动必须同步确认三个库的基线产物未动，不能只看 Oracle 自己。

### 11.14 批 13 改造后的增量（2026-10-10，**Python 侧展示层**；契约本身一字未改）

本轮**只动 Python**（`plugins/oracle/presentation.py` + 新增 `tests/test_oracle_display_coverage.py`
+ `tests/test_oracle_plugin.py` 断言更新），**采集包契约、目录树、`status` 语义全部不变** ——
改的是"下游**展示**这件事本身的覆盖面"。这张表写给"下一个接手展示层的人"：

| 你应当知道的 | 说明 |
|---|---|
| **presentation 层的条目不带 `item_id`** | 它由 `plugins/oracle/report_adapter.py::_extract_item_id(source, section_id, index)` 从 `source` **尾段**派生（按 `" — "` / `" – "` / `" - "` 切分）。要在这一层对账，得自己复刻同一套切分 |
| **① 本文件里的"未展示项数"≠ 采集项总数** | gap-review 写的"`oracle.dg` 6 项 / `oracle.audit` 7 项"是**未展示项数**，不是整域采集项数。落地时按实际包逐项核，别把"部分补上"当成"整域补上" |
| **② 判"这项采过没有"看表键，不看 `non_ok_items`** | `non_ok_items` **只记失败态**，`status=empty`（查了没有）与"根本没采"被它一并吞掉；空表在包里**也是一次登记**（`.psv` 存在、`row_count=0`）⇒ 判据是**表键在不在 `ctx.tables`** |
| **`empty` / `not_evaluated` / `permission_denied` 文案必须分叉** | `presentation.py::_EMPTY_TEXT` 按真实 `collection.status` 给不同话术：`success_no_data`（"已检查，本次未发现…"）与"没采到"（"采集权限不足…"）**是相反结论**，不许长得一样、更不许一起消失 |
| **★ sqlplus 会按列宽截断表头** | `SEGMENT_SPACE_MANAGEMENT`→`SEGMEN`、`ADMIN_OPTION`→`ADM`、`COMPRESSION`→`COMPRES`、`RESOURCE_TYPE`→`RESOURCE`、`SIZ`。`row.get(完整列名)` 取不到时**不报错、只静默少一列** ⇒ 一律走 `presentation.py::_cell(row, *names)`（精确 → 大小写无关前缀/后缀 → 兜底） |
| **`not_evaluated` 要带规则引擎的 `reason`** | `presentation.py::_analysis()` 的 `not_evaluated` 分支输出 `reason or "证据不足，未执行评价。"` —— 别把引擎算出的"无备库（未配置 DataGuard）"丢掉 |
| **检查项数 52 → 76，章节数仍 9** | 24 个新展示条目分散进已有章节；**唯一章名改动** `备份与恢复检查` → `备份、恢复与高可用检查`。目录 / 正文编号算式**从 `len(sections)` 派生** ⇒ 加章会连带改两处编号，本批**没有加章，也就没动编号** |
| **基线只有 `items` 变** | `tests/baselines/oracle/current/baseline.json` 的 `expected_summary` 里仅 `items` 52→76，其余（health / rules / triggered / passed / not_evaluated / risk_register / severity_counts）**全同** ⇒ 「纯展示层、不触发新判据」 |
| **未做** | `oracle.basic.large_segments` / `oracle.index.large_segments` / `system.hugepages` 同样"有判据、无展示"，留待下一轮 |

> ★★ **给"下一个接手人"的一条元教训**：**对着冻结基线断言，抓不到实现回归。**
> 本批的章名断言首版只读 `tests/baselines/oracle/current/report_model.json`（一份**死文件**），
> 于是负控制"把章名改回去"**rc=0、没变红**。补一条**走实现路径**的用例后才 5/5 全红。
> 这与 AST 对账盲区**同源**：**断言的数据源与被守对象不在同一层，绿色不代表正确** ——
> 守实现行为，必须在**实现这一层**断言；冻结基线只该用来钉"历史产物不被误改"。
>
> ★ 另一条：**"采了有数据"才是判"没展示"的前提。** 先从包内逐项确认 `status=ok` / 有行，
> 再回头找展示层有没有消费者 —— 顺序反了会把"环境本就空"的项误当缺口，
> 又会把"根本没采"的项误当"采了没展"。

### 11.15 批 14 改造后的增量（2026-10-10，**Python 侧取数层**；契约本身一字未改）

本轮**只动 Python**（`inspection_core/models.py` 新增访问点 + 两库 `presentation.py` 改调用），
**采集包契约、目录树、`status` 语义全部不变** —— 改的是"下游**从包里取非表格式证据文件**这件事"。
这张表写给"下一个接手取数层的人"：

| 你应当知道的 | 说明 |
|---|---|
| **非表格式文件一律走 `ctx.raw_lines(name)`** | 按**契约文件名**在 `tables/`、`evidence/` 两处查找并返回原始行；缺文件返回 `[]`。**别自己拼 `root / "evidence" / …`** |
| **目录名只在一处** | `inspection_core.models._RAW_FILE_DIRS = ("tables", "evidence")`，顺序即查找顺序 |
| **★ 为什么不能写死目录** | 批 6 / N-8 把 4 个「列对齐文本」从 `tables/` 挪到 `evidence/`。写死的读法移动后**只返回空** —— 报告静默少一节，规则层不报、测试不红 |
| **解析器收「行」不收 `Path`** | 5 + 4 个解析器签名已改为 `list[str]`。喂 `Path` 会当场 `TypeError` ⇒ "必须走 `raw_lines`"在**运行期**也成立 |
| **两套语义别混** | `raw_lines` 只读 `.txt` / key-value `.tsv`；**表格式数据（`tables/*.psv`）走 `ctx.tables`**（PSV 用竖线分隔、key-value TSV 用 TAB） |
| **呈现层消费的契约文件名** | Oracle：`lscpu.txt` / `time_status.txt` / `kernel_parameters.tsv` / `mounts.txt` / `dmesg_errors.txt` / `os_release.txt`；MySQL：`lscpu.txt` / `kernel_parameters.tsv` / `mounts.txt` / `dmesg_errors.txt`（**受守门约束，改名会红**） |
| **对既有报告零影响** | 同一 1.4 包重跑：8 张 PNG 逐字节相同、`llm_input.json` 完全相同、其余 JSON 与 DOCX 规范化后**实质差异 0** ⇒ **基线未动** |
| **未做** | PostgreSQL 的 `presentation.py` 还有同一模式 **9 处**（`111/119/173/175/176/177/560/563/567`），未纳入本批守门 → 记在批 14b |

> ★★ **给"下一个接手人"的一条元教训**：**"读不到"和"没有"在报告里长得一样，才是真危险。**
> 这些文件的路径写在呈现层时，目录一挪就**静默返回空** —— 报告少一节、没有任何信号。
> 统一口径的价值不在"代码好看"，而在于**把"文件去哪了"这个知识收进一个地方**，
> 于是"改了目录"这件事只有一处需要同步。
>
> ★ 另一条：**结构性断言必须配"尺子自证"。** 本批的 AST 守门（"没有 `ctx.root / …` 拼接"）
> 如果尺子本身是坏的，它会**永远绿**。所以每个结构断言都配一条"把故意违规的源码喂给同一个函数、
> 必须抓到"的用例 —— 这是批 13「断言形式与被测对象不匹配时绿色不代表正确」的直接延伸。


### 11.16 批 12b 改造后的增量（2026-10-12，**Python 侧图表层**；契约本身一字未改）

本轮补三张「包内有数据、报告里没有图」的图，并把**条形渲染入口**下沉公共层。
**采集包契约、目录树、`status` 语义全部不变** —— 数据早就采到了。

| 你应当知道的 | 说明 |
|---|---|
| **条形图走 `render_bar_spec()`** | `inspection_core/charts/render.py` 新增（matplotlib 优先 / Pillow 降级）。**纯新增入口**，`render_spec`（时序折线）与 `spec_has_data` 一字未动 ⇒ 四库基线零影响 |
| **★ 两种轴别混** | 折线：x = 时间戳、`spec_has_data` 要求 **≥2 点**；条形：x = **类别**、一个类别就该能画。`bar_spec_has_data` 是独立判据，别复用折线那套 |
| **图定义仍在插件** | `plugins/oracle/charts.py`（`charts/__init__.py` 明确「表空间等 database-specific chart 归插件」）；公共层只提供**画法** |
| **新图取数走 `ctx.tables`** | 这三张读的是**包内的表**（排行 / 汇总），不是 `ctx.timeseries` —— 与既有五张 Oracle 图的取数形态不同 |
| **图题与 id 同源** | `plugins/oracle/charts.py::ORACLE_CHART_CAPTIONS` / `OS_CHART_CAPTIONS`；`word_report` 与 `report_adapter` **都从它取**，别再抄第二份 |
| **★ 为什么** | `report_adapter` 是「**重建**」模型而非透传：没登记的 id 会把 caption 静默落回 `chart_id`（报告里图题变英文 id），没有任何测试会红 |
| **新增 `table_snapshot` 来源词汇** | 报告来源原有 `historical` / `realtime_snapshot`；表数据图是**一次快照**，既非 SAR 历史也非实时采样。词汇不全时会被兜底成 `realtime_snapshot`，让只有排行图的章节被误加「短时采样」说明 |
| **章节归属跨三章** | 表空间→`storage_capacity`、Top SQL→`oracle_performance`、归档→`backup_recovery`；单一来源仍是 `ORACLE_CHART_IDS`，守门钉「跨章节声明的并集 == 它」 |
| **对既有图零影响** | 同一 1.4 包重跑：原有 8 张 PNG **逐字节相同**；`expected_summary` 只有 `charts` 8 → 10 |

> ★★ **一条元教训（与批 13 同源）**：**适配器"重建"模型时，任何"两边各抄一份"的映射都会静默漂。**
> 本批的图题表就是第三份拷贝 —— 新图一上线，`word_report` 侧有（中文明细），
> `report_adapter` 侧没有 ⇒ caption 静默退化成 id。**收口到与 id 同源**才是终点。
> 凡见 `X.get(key, key)` 这种"取不到就落回 key"的写法，先问"这个 key 会不会新增"。


### 11.17 批 15 改造后的增量（2026-10-12，**Python 侧报告质量**；契约本身一字未改）

本轮**不动采集包**：`status` 取值、目录树、`tables/*.psv` 列名**全部不变**。
修的是**下游怎么读这些数据**（口径 / 键名 / 文案）。有两条**下游会直接看到差别**，记在此：

| 你应当知道的 | 说明 |
|---|---|
| **★ 伪文件系统一律不进容量类判据** | 判据 = 公共层 `inspection_core.system_checks.is_persistent_fstype()`。光驱（`iso9660`/`squashfs`/`udf`）、`tmpfs`/`devtmpfs`、`proc`/`sysfs`/`cgroup`、`overlay`、FUSE 都不算。Oracle 此前**只在规则层漏了这一过滤**（MySQL 早就在用）⇒ 单机库因光驱被判「文件系统使用率过高 100%」。**你若自己写消费者，请用同一个函数，不要自建黑名单** |
| **`df -PT` 行的键名是 `Mounted on` / `Filesystem` / `Available` / `Type` / `USE%`** | 由 `package_adapter.parse_df_pt` 产出（空格而非下划线）。展示层曾按 `MOUNTPOINT`/`FILESYSTEM`/`AVAILABLE` 取值 ⇒ **只有 `USE%` 恰好撞对**，另外三列恒空。**消费 `ctx.tables["filesystems"]` 时按这组键名取**（旧拼法仅作 1.4 老包兜底） |
| **`Available` 的单位是 1024-blocks（KB）** | 不是字节、不是 MB。展示层新增 `_kb_human()` 换算（`format_bytes(n * 1024)`）。**别把裸数字当 MB 用** |
| **表空间"使用率"只有一个口径** | `USED_MB ÷ MAX_MB`（分母 = **最大可扩**）。`PCT_USED` 的分母是**已分配** `ALLOC_MB`，**不是**同一个数 —— 图 6-1 原先用 `PCT_USED`，与表 6.1 / 规则层对不上（SYSTEM 99.98% vs 3.39%）。图已改口径；`MAX_MB` 缺失（老包）才退回 `PCT_USED` |
| **规则"通过"项也带 `facts`** | `RuleEvaluation` 新增字段 `facts: list[str]`，`to_dict()` 一并输出（**无论规则触发与否**）。下游若自己渲染"依据"，读它，不要再去猜 |
| **`collection.status == "empty"` ⇒ 结论「未发现」，不是「未采集」** | `report_adapter._normalize_analysis` 据此把「查了、确认没有」与「没采到」**分开**：前者 `status=normal` + 「已检查，本次未发现相关记录。」，后者仍是 `not_evaluated`。★ 两者是**相反结论**，别再一起压成"未采集到结构化记录" |
| **`display.note` 会进章末小结的"依据"列** | 事实项无自身依据时，`report_adapter` 用 `display.note`（如「已排除 37 个 proc/tmpfs/cgroup/光驱等非持久化挂载点」）兜底，否则用「共 N 条结构化记录」。**你要加脚注就写 `display.note`**，别另造一个键 |
| **`word_engine` 空数据分支优先看 `analysis.conclusion`** | 无明细表的**规则项**（"CPU 压力评估"/"无效对象"…）在 professional 布局正文落一行「分析结论 …」；legacy 布局紧随其后渲染完整分析块、正文不重复。**这条是共享引擎（四库共用）**，数据齐全时零字节影响（控制变量自证） |

> ★ **本批的元教训（两条）**：
> ① **`if x` 碰上有意义的 0 就会撒谎** —— 表 6.1 的 `if used_val` 把 `USED_MB=0` 判成空白格，
> 与「未采集 ≠ 0」相悖。判空一律写 `is not None`。
> ② **"消除重复"不等于"消除分叉"** —— 同一条判据（持久化文件系统）若在规则层与展示层各写一份，
> 短期一致、长期必分叉。**归一到"谁产生键名谁提供取键函数"**（`filesystem_type` 与 `parse_df_pt` 同文件）
> 才是终点，与 §11.16 的图题表同源。

### 11.18 批 16 改造后的增量（2026-10-12，**采集层 + 解析层 + 展示层**；列名/目录/status 一字未改）

本批**动了采集脚本**（若干 `COLUMN` 宽度声明 + 一个 `LISTAGG` 分隔符），
但 `tables/*.psv` 的**列名、目录树、`status` 取值、`item_id` 全部不变** —— 下游无需适配。
下面是**下游会直接看到差别**的几条：

| 你应当知道的 | 说明 |
|---|---|
| **★ `.psv` 的「一行 = 一条记录」是契约，且有**两套**机械守门（缺一不可）** | **写法侧** `tools/check_oracle_psv_widths.py` 查四类破坏它的写法：① `SUBSTR(…,1,N) <别名>` 的宽度声明必须存在且 `M ≥ N`；② SQL 字面量里不许有 `\|`（COLSEP 就是 `\|`）；③ 不许 `WORD_WRAPPED`（按词折行的残段无法回并）；④ 同名别名（含大小写两份）宽度必须唯一。**数据侧** `tools/check_oracle_psv_records.py` 查**真实产物**：A 字段数 / B 首列非空 / C 触顶（仅警告，不影响退出码）。★ 为什么必须两套：**静态那份结构性地看不到「运行期数据本身含分隔符」** —— 实测 10-10 包 `LISTAGG` 的分隔符用了竖线，第二个控制文件路径被静默丢弃，而写法侧当时**全绿**。**改采集 SQL 前后 + 每次采集后各跑一次**（`--include-tsv` 会自动关掉判据 B：1.4 的 `.tsv` 首列空白是**合法 NULL**，不是残段） |
| **长值列宽度 = SQL 截断长度** | `value`/`VALUE` → `a500`（SQL 是 `SUBSTR(value,1,400)`）、`description` → `a200`、`message`/`message_text` → `a500`、新增 `COLUMN additional_info FORMAT a120`。**规矩：`SUBSTR(x,1,N)` 的值列，`COLUMN x FORMAT aM` 必须 `M ≥ N`**，否则 sqlplus 按 M 折行、残段成"新行" |
| **`LISTAGG` 分隔符不要用 `\|`** | `oracle.validation.controlfile_multiplex` 的 `LISTAGG(name,' \| ')` 改为 `'; '`：分隔符与 `SET COLSEP '\|'` 撞，且 2.0 的 `psv_normalize` 会把 ` \| ` 压成 `\|` ⇒ 第二个路径被当列边界丢掉 |
| **★ 折行续行回并只对 2.0 的 `.psv` 生效** | 判据是「2.0 会话设了 `SET NULL '-'` ⇒ 空单元格不存在 ⇒ 首字段为空只能是残段」。**1.4 没有这个保证** —— 它的第一列空白是 NULL 落在定宽列里、是**真实数据行**（1.4 上开回并会把两行并进表头、整表 0 行）。开关由 `package_adapter._iter_table_specs` 按**采集器版本**给出，**不按分隔符猜**；`parse_delimited(repair_wrap=…)` **默认关**。★ 你若自己写 `.psv` 消费者，`|` 分隔的表格请显式 `repair_wrap=True` |
| **`CONTROLFILE_PATHS` 是"多值"列** | 11.8 控制文件冗余的列名是 `CONTROLFILE_COUNT` / `CONTROLFILE_PATHS`（**不是** `NAME`/`VALUE`）。**多个路径由 `'; '` 连接**（本批由 `\|` 改来）—— 你若按 `\|` 切会只剩一个 |
| **5.1 `关键初始化参数` 的值不再截断** | 展示层原先 `[:80]` 砍值（`control_files` 的 86 字符被切成 `…/control`，像"第二个控制文件叫 control"）；现在整值进 Word，单元格自行折行。SQL 侧仍有 `SUBSTR(…,1,400)` 上限 |

> ★ **本批的元教训（两条）**：
> ① **「改完看不出变化」要先找第二处同形截断** —— 取数层 `a80` 与展示层 `[:80]` 都落在第 80 个字符上，
> 修好一层、另一层会让报告**逐像素不变**。判"修没修好"要看**数据**（`analysis.json` 里的值），不要看截图。
> ② **判据的前提要写清"靠什么成立"** —— 回并的前提是「**采集器版本**保证空单元格不存在」，
> 不是「**分隔符**是 `\|`」。前提写错，1.4 上就会把真实数据行并进表头（本批真实发生过，08-10 基线当场暴露：
> `控制文件路径` 2 行 → **0 行**）。

### 11.19 批 17 改造后的增量（2026-10-12，**仅 Python 侧展示层**；采集脚本/契约一字未改）

本批**没动采集脚本**，`tables/*.tsv|psv` 的列名、目录树、`status` 取值、`item_id` 全部不变。
但有两条**消费约定**值得写下来，因为它们解释了两个"报告里看起来是数据、其实是解析"的现象：

| 你应当知道的 | 说明 |
|---|---|
| **`tables/kernel_parameters.tsv` 有表头** | 首行是 `path<TAB>value`（N-20 / 批 7 补的）。**它不是参数** —— 读数时跳过。Oracle 侧展示层原先没跳 ⇒ 报告里多一行「`path \| value`」。MySQL/PG 的采集器**不写表头**，所以同一文件在四库形态不同，**消费者要两边都能吃** |
| **`未采集` 与 `-` 是两个不同结论** | `主机信息` / `时间与时区` 这类**合成行**（值由展示层拼、不来自表格）里，源证据缺失时给 `未采集`、字段本身为空才给 `-`。判据：**"没采到"不许长得像"空"**。1.4 采集器不采操作系统层，这些行现在读作 `未采集` 而不是 `-` |
| **`tables/hugepages.tsv` 同样带表头** | 与 `kernel_parameters.tsv` 同款（`path<TAB>value`），采集侧三库一致。当前展示层未消费它；若将来要展示，同样要跳首行 |

> ★ **本批的元教训**：**呈现层的错不产生功能性失败信号**。表头混进数据、`""` 与 `None` 混用，
> 都不会报错、不会让测试变红，报告照出 —— 只能靠"把渲染结果拉出来与 `analysis.json` 对账"发现。
> 与批 15/16 同源。

### 11.20 批 18 改造后的增量（2026-10-12，**仅 Python 侧展示层**；采集脚本/契约一字未改）

| 项 | 结论 |
| --- | --- |
| 采集脚本 | **一字未改**（`.psv` / `.tsv` 的形态、列名、目录、`status` 全不动） |
| 契约 | **一字未改**（`snapshot.json` / `collection_status.json` / `tables/*` 的字段与形态不变） |
| 改的是 | `plugins/oracle/presentation.py` 的**说明列**与 `PGA 总分配` 的取值口径 |

- `数据库身份` / `数据库配置概要` 两张表的「说明」列由**空**变为**口径**（22 行）：写的是
  「这个值是什么、取自哪个视图」（如 `DB_NAME` → 「数据库名（v$database.name）」），
  **不是建议**。与 MySQL `CONFIG_GROUP_VARIABLES` 同一条线。
- `PGA 总分配` 改为按名字取 `total PGA allocated`（此前取 `pga_rows[0]`，实测是 `total PGA inuse`
  —— 采集 SQL 没有 `ORDER BY`，行序由视图给）。**这是读法修正，不是契约变更**：
  Python 侧读的仍是同一张表、同一列。
- ★ 展示层侧的一条纪律：`ORACLE_DB_CONFIG_NOTES` 同时是「概要表展示哪些 NLS 参数」的单一来源 ——
  加一条口径 = 多展示一行，两者不是两件事（`NLS_TERRITORY` 因此仍不进表）。

### 11.21 批 19 改造后的增量（2026-10-12，**仅 Python 侧**；采集脚本与 `.psv`/`.tsv` 形态一字未改）

| 项 | 结论 |
| --- | --- |
| 采集脚本 | **一字未改**（`.psv` / `.tsv` 的形态、列名、目录、`status` 全不动） |
| 采集包契约 | **一字未改**（`snapshot.json` / `collection_status.json` / `tables/*` 字段与形态不变） |
| 分析产物契约 | **加字段**：`appendix.rule_evaluations[*]` 新增 `source_status`（字符串，可为 `""`） |
| 改的是 | Oracle 适配器取数 API、规则层取数键与判据、展示层采集状态、公共 Word 引擎的状态词表 |

**`source_status` 的取值域**（与 `tables/*` 首行声明的采集侧状态同域）：

| 取值 | 含义 | 算「采集过」吗 |
| --- | --- | --- |
| `ok` | 取到内容 | ✅ |
| `empty` | **查了、确认 0 行** | ✅ **算**（与「没采到」是相反结论） |
| `partial` | 部分可用 | ✅ |
| `unavailable` | 键在包里不存在（别名对不上 / 这一项本来没采） | ❌ |
| `timeout` / `permission_denied` / `error` | 采集失败 | ❌ |
| `not_enabled` / `skipped` / `unsupported` | 采集器按条件跳过 | ❌ |
| `missing` | 声明了但文件不在 | ❌ |
| `""` | **缺状态** | ❌ **按没采到处理**（宁可「证据不足」，不可「检查通过」） |

- **取数 API**：`ctx.table_fetch(*names) -> TableFetch`。**不用** `ctx.find_table(...)` 的子串兜底 ——
  那是「取不到就挑一个名字像的」，实测会把 FRA 使用率兜到 `快速恢复区（FRA）参数`。
  要兜底就在**调用点把名字列全**，让「名字」这件事保持可审计。
- **判据**：`fetch_state(TableFetch) -> str`（取数 → 状态）、`state_is_collected(str) -> bool`
  （状态 → 这项采集过吗）、`prefer_state(*states) -> str`（同一事实多个落点 → 一个状态；
  优先级「有内容 > 查了没有 > 没采到」）。
- **展示层**：`display_status()` 与附录两处「数据缺口」表共用模块级 `STATUS_DISPLAY` ——
  状态词只此一份。**加状态值必须加在这一份里**，否则会原样印出英文枚举（这类错不抛异常、
  测试也不红，见 `tests/test_word_engine.py::CollectionStatusVocabularyTests`）。
- ★ 展示层纪律：`_item()` 的 `collection.status` **优先取规则项自己的 `source_status`**，
  取不到才回落到采集层的失败清单。规则 id 与采集 item_id 不同域，回落路线会**恒印「正常」**。

**新增静态守门**：`tools/check_oracle_lookup_keys.py`（`SOURCES = metrics.py + oracle_rules.py`）。
按**语句**聚合取数点，四档输出，`✗ 含未登记名` 退出码非 0。
★ 它认得的取数 API 列在 `_TABLE_ATTRS` / `_TABLE_GETTERS` —— **加新 API 必须同步这里**，
否则新调用点对尺子不可见（静默漏审）。

### 11.22 批 21 改造后的增量（2026-10-14，**采集层**；新增一个「产物型」采集项）

**背景**：现场 2.0 包（ZYDB / 2026-10-14）里 AWR 报告既没生成、包里也翻不到。查出两个独立缺陷
（许可闸门把「跳过」记成 `ok`；AWR HTML 落在 `TASK_DIR` 之外 ⇒ 永不进包）。
详见 `docs/oracle-collector-fix-plan.md` 的 §批 21（OF-31 / OF-32）。

| # | 契约变化 | 落点 | Python 侧要知道的事 |
|:--:|---|---|---|
| 1 | **新增采集项 `oracle.awr.report`**（`category = sql`），产物型 | `status/oracle.awr.report.tsv`；文件 `evidence/<prefix>_awr_<tag>_<qctime>.html` | 本项**不是表**，别去 `tables/` 找。`output_file` 是**相对包根**的真实路径；注意 `package_adapter._load_tables` 目前**只对 `tables/` 前缀**做「文件 → item_id」反查，`evidence/` 前缀暂无消费者（`coverage_audit` 的 evidence 只认 `.txt`，故这张 `.html` 不会进「无人提及」清单） |
| 2 | **模块级新增一种「非失败的非 ok」状态**：`oracle.module.awr_report` 在许可未确认时记 **`not_enabled`**（原先恒 `ok`） | `status/oracle.module.awr_report.tsv` | 它会进 `quality.non_ok_items` ⇒ 报告附录「数据缺口」多一行（状态词走 `STATUS_DISPLAY` → 「未启用」）；`quality.score` 按既有权重 `not_enabled=0.8` 计（原先按 `ok=1.0`） |
| 3 | **新增模块内部返回码 `EXIT_POLICY_SKIP=40`** | `run_module` | 语义 = 「模块跑了，但按许可/前置条件没采集」。记账为 `not_enabled`、**不计入失败数**、不写「采集异常」。§8 的退出码表**不含 40** —— status 里的 `exit_code=40` 是模块内部码，不是脚本终态退出码 |

**不变量（本批没动）**：

- 许可未确认时**不产生** `oracle.awr.report` 这一行 —— 「没生成」不许伪装成「空结果」或「0」。
- 生成失败记 `error`，reason **只取 `ORA-` 编号**（不整段落盘，避免输出里夹带连接串痕迹）。
- 旧包（脚本不重跑）**不会**出现该行 ⇒ 历史包的分析结论与基线不受影响。

**一处容易误判的地方**：`AWR_REPORT` 的**默认值是 1**，所以命令行加不加 `--awr-report` 对结果
没有区别；真正决定跑不跑的是 `--diagnostics-pack`（默认 0）。现场「打开了 AWR 报告却没采到」
的根因就是这个不对称 —— 开场现在会显式告警，不再静默。
