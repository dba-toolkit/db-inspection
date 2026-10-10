# Oracle 采集项 item_id 对照表（v2.0）

- 来源：`inspection/oracle_inspection.sh` 的 136 个 `ora_sql_to_md` 调用点，**由脚本自动生成**（勿手改）。
- 规则：item_id = `oracle.<域>.<项>`（全 ASCII 点分）；**同一 id 只能出现一次**。
- 输出文件 = `tables/<item_id>.psv`（竖线分隔）；status = `status/<item_id>.tsv`。
- 生成器：`_work/_b1_apply_ids.py`（映射表在该文件里，是 id 的唯一来源）。

| # | item_id | Markdown 标题 |
|:--:|---|---|
| 1 | `oracle.basic.database` | 数据库基本信息 |
| 2 | `oracle.basic.instance` | 实例信息 |
| 3 | `oracle.basic.charset` | 数据库字符集 |
| 4 | `oracle.basic.parameters` | 关键初始化参数 |
| 5 | `oracle.basic.sga_components` | SGA 组件大小 |
| 6 | `oracle.basic.sga_summary` | SGA 汇总信息 |
| 7 | `oracle.basic.pga_stats` | PGA 统计 |
| 8 | `oracle.basic.controlfiles` | 控制文件 |
| 9 | `oracle.basic.redo_groups` | Redo 日志组信息 |
| 10 | `oracle.basic.object_counts` | 数据库对象统计 |
| 11 | `oracle.basic.invalid_objects` | 无效对象统计 |
| 12 | `oracle.basic.users` | 用户列表 |
| 13 | `oracle.basic.password_expiry` | 用户密码过期预警（账户非OPEN或30天内到期） |
| 14 | `oracle.basic.awr_snapshot_status` | AWR 快照状态（确认 AWR 是否正常运行） |
| 15 | `oracle.basic.disabled_constraints` | 禁用/失效约束（非SYS用户） |
| 16 | `oracle.basic.partitioned_tables` | 分区表统计（分区数 TOP20） |
| 17 | `oracle.basic.large_segments` | 大表段 TOP20（按大小排序） |
| 18 | `oracle.basic.pdb_list` | PDB 状态列表（CDB环境） |
| 19 | `oracle.tablespace.usage` | 所有表空间容量使用情况 |
| 20 | `oracle.tablespace.datafiles` | 表空间数据文件列表 |
| 21 | `oracle.tablespace.tempfiles` | 临时表空间数据文件 |
| 22 | `oracle.tablespace.growth` | 表空间增长趋势（AWR，近30天每日汇总） |
| 23 | `oracle.tablespace.high_usage` | 使用率超过 80% 的表空间（⚠️ 预警） |
| 24 | `oracle.asm.diskgroup_summary` | ASM 磁盘组汇总 |
| 25 | `oracle.asm.disk_detail` | ASM 磁盘详情 |
| 26 | `oracle.asm.dbfile_count` | ASM 磁盘组中的数据库文件数 |
| 27 | `oracle.archive.mode` | 归档模式检查 |
| 28 | `oracle.archive.destinations` | 归档目标参数 |
| 29 | `oracle.archive.hourly_3d` | 近3天每小时归档量 |
| 30 | `oracle.archive.daily_30d` | 近30天每日归档量 |
| 31 | `oracle.archive.log_switch_rate` | 重做日志切换频率（近24小时） |
| 32 | `oracle.archive.fra_asm` | ASM 快速恢复区使用情况 |
| 33 | `oracle.archive.fra_params` | 快速恢复区（FRA）参数 |
| 34 | `oracle.session.active` | 当前活跃会话 |
| 35 | `oracle.session.wait_events` | 当前会话等待事件（非空闲） |
| 36 | `oracle.session.locks` | 会话锁信息 |
| 37 | `oracle.session.lock_chain` | 锁等待链（阻塞者 → 被阻塞者） |
| 38 | `oracle.session.long_transactions` | 长事务（运行超过1小时） |
| 39 | `oracle.performance.sql_top10_ash` | SQL 资源消耗 TOP10（近1小时 ASH） |
| 40 | `oracle.performance.cpu_sessions` | CPU 高消耗会话 TOP10 |
| 41 | `oracle.performance.io_files` | IO 消耗最高数据文件 TOP10 |
| 42 | `oracle.performance.wait_top10` | 等待事件 TOP10（数据库级） |
| 43 | `oracle.performance.top_sql_waits` | TOP SQL 等待事件（近1小时） |
| 44 | `oracle.performance.shared_pool_frag` | 共享池碎片 |
| 45 | `oracle.performance.library_cache_hit` | Library Cache 命中率 |
| 46 | `oracle.performance.buffer_cache_hit` | Buffer Cache 命中率 |
| 47 | `oracle.performance.temp_sort_usage` | 临时表空间排序段使用 |
| 48 | `oracle.performance.undo_usage` | UNDO 使用统计 (v\$undostat) |
| 49 | `oracle.performance.undo_params` | UNDO 参数 |
| 50 | `oracle.index.invalid` | 无效/不可用索引 |
| 51 | `oracle.index.unusable_partitioned` | 失效分区索引 |
| 52 | `oracle.index.unusable_subpartitioned` | 失效子分区索引 |
| 53 | `oracle.index.large_segments` | 大索引段 TOP20 |
| 54 | `oracle.adg.sync_stats` | DataGuard 同步统计 |
| 55 | `oracle.adg.params` | DataGuard 配置参数 |
| 56 | `oracle.adg.mrp_status` | Managed Standby 进程状态 |
| 57 | `oracle.adg.transport_apply` | 日志传输/应用状态 |
| 58 | `oracle.adg.file_sync` | 备库文件同步状态 |
| 59 | `oracle.adg.log_gap` | 主备日志差距（已归档 vs 已应用） |
| 60 | `oracle.adg.switchover_check` | 备库角色切换检查 |
| 61 | `oracle.rac.instance_status` | RAC 各节点实例状态 |
| 62 | `oracle.rac.node_perf` | RAC 各节点性能对比 |
| 63 | `oracle.rac.channel_waits` | RAC 通道等待分析 |
| 64 | `oracle.rac.node_waits` | RAC 各节点等待事件（超10个会话） |
| 65 | `oracle.rac.temp_sql` | RAC 消耗临时空间 SQL（>1GB） |
| 66 | `oracle.rac.ges_gc_waits` | RAC GES/GC 锁等待 |
| 67 | `oracle.rman.backup_summary` | 最近备份集汇总 |
| 68 | `oracle.rman.policy` | 备份策略参数（RMAN CONFIGURE） |
| 69 | `oracle.rman.history` | RMAN 备份历史状态（近7天） |
| 70 | `oracle.alert.diag_path` | ADR Diag 路径 |
| 71 | `oracle.pdb.status` | PDB 状态详情 |
| 72 | `oracle.pdb.resource_limits` | PDB 资源限制 |
| 73 | `oracle.pdb.tablespace_usage` | PDB 表空间使用率 |
| 74 | `oracle.pdb.sessions` | PDB 会话数统计 |
| 75 | `oracle.pdb.services` | PDB 服务配置 |
| 76 | `oracle.pdb.flashback` | PDB 闪回配置 |
| 77 | `oracle.pdb.cdb_flashback` | CDB 级别闪回配置 |
| 78 | `oracle.pdb.role_status` | PDB 角色与切换状态 |
| 79 | `oracle.service.config` | Service 服务配置 |
| 80 | `oracle.dg.broker_overview` | DG Broker 配置概览 |
| 81 | `oracle.dg.broker_enabled` | DG Broker 启用状态 |
| 82 | `oracle.dg.archive_dest` | 日志传输服务状态 (v\$archive_dest) |
| 83 | `oracle.dg.protection_mode` | 保护模式与主库统计 |
| 84 | `oracle.dg.broker_params` | DG Broker 配置参数 |
| 85 | `oracle.dg.recent_events` | 最近 DG 事件 (v\$dataguard_status) |
| 86 | `oracle.dg.log_sequence_gap` | 主备库日志序差距 |
| 87 | `oracle.dg.rfs_status` | 备库 RFS 进程状态 |
| 88 | `oracle.dg.apply_lag` | 备库应用延迟监控 |
| 89 | `oracle.flashback.log_usage` | 闪回日志使用统计 |
| 90 | `oracle.flashback.area_usage` | 闪回区使用情况 |
| 91 | `oracle.flashback.guaranteed_rp` | Guaranteed Restore Point |
| 92 | `oracle.flashback.normal_rp` | 普通 Restore Point |
| 93 | `oracle.flashback.pdb_flashback` | PDB 级别闪回配置 |
| 94 | `oracle.audit.fga` | 审计策略检查 (FGA) |
| 95 | `oracle.audit.failed_logins` | 失败登录审计统计 |
| 96 | `oracle.audit.profile_params` | 密码配置文件参数 |
| 97 | `oracle.audit.profile_nondefault` | 默认配置文件非默认设置 |
| 98 | `oracle.audit.locked_accounts` | 锁定账户检查 |
| 99 | `oracle.audit.privileged_users` | 特权用户 (DBA角色) |
| 100 | `oracle.audit.system_privileges` | 系统特权授予 (非标准用户) |
| 101 | `oracle.audit.password_file_members` | 密码文件成员 |
| 102 | `oracle.audit.object_audit` | 对象审计统计 |
| 103 | `oracle.awr.package_access` | 管理包访问设置 |
| 104 | `oracle.awr.baselines` | AWR 基线列表 |
| 105 | `oracle.awr.recent_snapshots` | 最近 AWR 快照 |
| 106 | `oracle.awr.sql_plan_baseline` | SQL Plan Baseline |
| 107 | `oracle.awr.auto_stats_job` | 自动统计信息收集 Job |
| 108 | `oracle.awr.missing_stats` | 缺失统计信息表 (TOP20) |
| 109 | `oracle.awr.hwm_segments` | 段级高水位统计 (HWM) |
| 110 | `oracle.awr.segment_bloat` | 数据膨胀预估 (DBA_SEGMENTS vs DBA_TABLES) |
| 111 | `oracle.awr.assm_status` | 自动段空间管理 (ASSM) 状态 |
| 112 | `oracle.awr.index_usage` | 索引使用状态 (未使用/使用中) |
| 113 | `oracle.control.sn_sequence` | 控制文件序列号 |
| 114 | `oracle.control.spfile_params` | SPFILE 当前值 (关键参数) |
| 115 | `oracle.control.recent_param_changes` | 最近参数更改 |
| 116 | `oracle.topsql.logical_reads` | Top 20 SQL (按逻辑读) |
| 117 | `oracle.topsql.elapsed_time` | Top 20 SQL (按执行时间) |
| 118 | `oracle.topsql.physical_reads` | Top 20 SQL (按物理读) |
| 119 | `oracle.topsql.adr_alert_path` | ADR 预警日志路径 |
| 120 | `oracle.topsql.recent_errors` | 最近 SQL 错误 (近24h) |
| 121 | `oracle.topsql.addm_findings` | Database Health Check (ADDM) |
| 122 | `oracle.topsql.segment_ash` | Top Segment 访问 (ASH) |
| 123 | `oracle.validation.components` | 数据库组件状态 |
| 124 | `oracle.validation.sql_patches` | SQL 补丁应用状态 |
| 125 | `oracle.validation.corruption` | 数据库坏块记录 |
| 126 | `oracle.validation.datafile_header` | 数据文件头异常 |
| 127 | `oracle.validation.block_checksum` | 块校验与丢失写保护参数 |
| 128 | `oracle.validation.autoextend` | 数据文件自动扩展余量 |
| 129 | `oracle.validation.scheduler_failed` | Scheduler 失败作业（近7天） |
| 130 | `oracle.validation.scheduler_longrun` | Scheduler 长时间运行作业 |
| 131 | `oracle.validation.rman_validate` | 最近 RMAN VALIDATE 记录 |
| 132 | `oracle.validation.backup_piece_status` | RMAN 备份片异常状态 |
| 133 | `oracle.validation.controlfile_multiplex` | 控制文件多路复用 |
| 134 | `oracle.validation.redo_multiplex` | Redo 日志多路复用检查 |
| 135 | `oracle.validation.auditing_tde` | Unified Auditing 与 TDE 状态 |
| 136 | `oracle.validation.srl_coverage` | Standby Redo Log 配置覆盖 |
