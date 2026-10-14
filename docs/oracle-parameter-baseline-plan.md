# Oracle 参数最佳实践基线 — 实施方案（**待审，本轮不动码**）

> 目的：把「Oracle 19c 通用最佳实践参数清单」吸收成**可判定、可留痕、可展示**的规则基线，并在 Word 报告里成节。
> 状态：**方案稿**。本文不含任何代码改动，确认后再按批落地。
> 关联：`docs/oracle-collector-fix-plan.md`（批次记录）、`docs/oracle-collector-interface.md`（采集契约）。
> 输入材料：客户提供的「Oracle 19c 最佳实践参数」清单（43 条，含 `_` 隐藏参数 33 条）。

---

## 1. 结论先行

| 问题 | 结论 |
|---|---|
| 能不能吸收 | **能**。采集侧只要 **1 条 SQL**；难点不在采集，在**判定**。 |
| 有必要加吗 | **有，但定位是「加分题」而不是「及格线」**。优先级排在 AWR 分析章之后。 |
| 能不能做成「偏离即告警」 | **不能**。这是本方案的核心判断，见 §4。 |
| 放报告哪 | **「Oracle 实例检查」章下的一个小节**（参数=实例配置）。**不建议开独立整章**。 |

**为什么不能"偏离即告警"**——清单给的是**通用最优**，不是**这个库的诊断**。至少三类条目直接照做会出错：

1. **合规冲突**：`audit_trail=none`。医院 HIS 属等保/医疗合规场景，关闭数据库审计**可能违规**；本库当前是 `DB`。
2. **场景相反**：`result_cache_max_size=0` 对纯 OLTP 对，对**报表/BI 库是错的**。
3. **安全冲突**：`_sys_logon_delay=0` 关掉的是**失败登录延迟**（防口令爆破的机制），设 0 = 关闭防护。**不建议按清单设 0。**

---

## 2. 采集侧改造（2 处新增 + 1 处修死项）

### 2.1 新增 `oracle.basic.parameter_baseline`（1 条 SQL）

```sql
-- 基线参数当前值 + 是否默认 + 是否废弃
SELECT name,
       SUBSTR(value, 1, 200)         value,
       SUBSTR(default_value, 1, 200) default_value,
       isdefault, ismodified, isadjusted,
       issys_modifiable, isdeprecated
FROM v$parameter
WHERE name IN ( /* ← 与基线表同源，见 §3；由生成器注入，不手抄 */
       'audit_trail', '_use_adaptive_log_file_sync', ... )
ORDER BY name;
```

**三条必须遵守的约束**：

- ★ **必须采 `isdefault`**。否则分不清「客户显式设过」和「只是版本默认值」——而隐藏参数的默认值**随版本变**，凭文档猜必错。
- ★ **必须采 `isdeprecated`**。废弃项不进判定，只记 `not_applicable` + 原因。
- ★ **参数名返回空 ⇒ 该版本不存在该参数 ⇒ 记 `not_applicable`**，**不得**记成「未设置」或「未采集」。（`v$parameter` 里查不到 = 不存在，这与「查了没有」是两件事。）

### 2.2 修死项：`oracle.control.spfile_params`

现有 SQL（`oracle_inspection.sh` L3497）：

```sql
WHERE name IN ('spfile','*.audit_file_dest','*.control_files', ...) AND value IS NOT NULL
```

`v$spparameter.NAME` 存的是**裸参数名**，`*.` 只是 `ALTER SYSTEM` 的记法 ⇒ 这个 IN 列表**几乎永不匹配** ⇒ 恒返回 0 行、状态 `empty`、报告印「已检查，本次未发现相关记录」。**"坏了"长得像"没问题"的又一例。**

先自查（真库一句）：

```sql
select name, isdefault from v$spparameter where name like '%audit_file_dest%';
```

处理选项：**（a）修好**（去掉 `*.` 前缀；`isdefault='FALSE'` 才是显式写进 spfile 的）；
**（b）直接删除该项**（§2.1 的基线项覆盖面更宽，它已无独立价值）。**推荐（b）。**

### 2.3 顺带：`oracle.control.recent_param_changes` 名不副实

现有 SQL 是 `WHERE ismodified != 'FALSE'` **无排序键** + `ROWNUM <= 30`。实测返回的是**字典序前 30 条**，全是 `nls_*` 会话级参数（`nls_calendar` / `nls_currency` / `nls_date_format` …），**跟"最近更改"无关**。名字与内容不符，建议改名或重写为「**已显式修改的系统级参数**」：

```sql
SELECT name, value, isdefault, ismodified, isadjusted
FROM v$parameter
WHERE name NOT LIKE '\_%' ESCAPE '\'
  AND isdefault = 'FALSE'
ORDER BY name;
```

---

## 3. 基线表（**单一来源**，不进现有 42 条规则命名空间）

**位置建议**：`plugins/oracle/parameter_baseline.py` 里的模块级常量（或同目录 `parameter_baseline.json`）。
**理由**：① 口径表集中可审、**可被断言**（项目既有纪律：MySQL `CONFIG_GROUP_VARIABLES`、Oracle `ORACLE_*_NOTES`）；
② 不污染 `inspection_rules_oracle.json` 的 42 条规则命名空间 —— 符合「AWR/参数单独拆开，不混入现有逻辑」。

> ⚠ **这张表同时就是展示白名单**：加一条 = 报告多一行。改表前先想清楚"客户会不会问这一行为什么在这"。

**每条字段**：

| 字段 | 含义 |
|---|---|
| `name` | 参数名 |
| `advised` | 建议值（字符串，按原样展示） |
| `compare` | `eq` 等值 / `gte` 大于等于 / `show` 仅展示 / `exception` 不建议照做 |
| `scope` | 适用条件：`all` / `rac` / `cdb` / `oltp` / `partitioned` / `sga_gt_gb:100` / `has_dg` |
| `severity` | 偏离时的级别：`high` / `medium` / `low` / `-`（不产风险） |
| `version_sensitive` | `true` ⇒ 落地前必须按目标版本核对（本机无 Oracle，**下表所有名字均未在真库确认存在**） |
| `rationale` | 判定说明（**自己写**，写清"改的前提"，不抄清单原文） |

### 3.1 全量条目（43 条，按清单原序）

| # | 参数 | 建议值 | 比较 | 适用 | 级别 | 版本敏感 | 判定说明 |
|---|---|---|---|---|---|---|---|
| 1 | `_optimizer_adaptive_cursor_sharing` | `false` | eq | all | medium | | 关闭 ACS，避免绑定变量值变化引起计划波动；与 #33 配套 |
| 2 | `_optimizer_extended_cursor_sharing` | `none` | show | all | - | ✔ | 11g 起语义被 `_optimizer_extended_cursor_sharing_rel` 取代，部分版本写入不生效 ⇒ **只记录** |
| 3 | `_optimizer_extended_cursor_sharing_rel` | `none` | eq | all | low | ✔ | 同上族的现行开关；按版本核对 |
| 4 | `_optimizer_use_feedback` | `false` | eq | all | medium | | 关闭基数反馈，避免执行计划不稳 |
| 5 | `_optimizer_gather_feedback` | `false` | eq | all | low | ✔ | 与 #4 配套；12c+ 另有自适应计划相关开关需一并核对 |
| 6 | `_gc_policy_time` | `0` | eq | **rac** | medium | | 关闭 DRM |
| 7 | `_gc_undo_affinity` | `false` | eq | **rac** | medium | | 关闭 UNDO 的 DRM |
| 8 | `_undo_autotune` | `false` | eq | all | medium | | ★**连带条件**：关闭后 `undo_retention` 不再自适应，**必须显式设定**，否则回落默认值 |
| 9 | `_use_adaptive_log_file_sync` | `false` | eq | all | **high** | | 关闭 LGWR 自适应模式（已知会造成 `log file sync` 远高于日志写延迟） |
| 10 | `_optimizer_mjc_enabled` | `false` | eq | all | medium | | 关闭 MERGE JOIN CARTESIAN |
| 11 | `_b_tree_bitmap_plans` | `false` | eq | **oltp** | medium | | 关闭 B-tree→bitmap 转换；**报表/数仓库不建议关** |
| 12 | `_partition_large_extents` | `false` | show | partitioned | low | | 避免空分区浪费；★对**大分区表**是权衡项（大扩展减少 extent 数），且**对已存在分区无效** ⇒ 只提示 |
| 13 | `_index_partition_large_extents` | `false` | show | partitioned | low | | 同 #12 |
| 14 | `audit_trail` | — | **exception** | — | - | | ★**合规例外**：等保/医疗场景**不得建议关闭**；恒输出「按客户合规要求确认」，**不产风险** |
| 15 | `parallel_force_local` | `true` | eq | **rac** | medium | | 并行进程不跨实例启动 |
| 16 | `deferred_segment_creation` | `false` | eq | all | medium | ✔ | 关闭延迟段创建（该特性历史缺陷多）；11.2.0.2+ 默认值已变 |
| 17 | `_cursor_obsolete_threshold` | `200` | eq | all | medium | ✔★ | **风险最高的一条**：12.2+ 官方默认已提高，**降到 200 必须按目标版本评估**，评估不当会加剧子游标问题 |
| 18 | `_optimizer_cartesian_enabled` | `false` | eq | all | low | | 关闭笛卡尔积优化 |
| 19 | `control_file_record_keep_time` | `31` | **gte** | all | medium | | ★判据是**范围**不是等值；且应 **≥ RMAN 保留策略天数**，否则归档/备份记录会被过早覆盖 |
| 20 | `result_cache_max_size` | — | **exception** | oltp | - | | ★报表/BI 库**不适用**（关掉反而有害）⇒ 按业务负载判定，不默认告警 |
| 21 | `_datafile_write_errors_crash_instance` | `false` | show | all | - | ✔ | ★这是**「可用性 vs 一致性」策略选择**，不是对错题 ⇒ 只展示 + 说明影响 |
| 22 | `_clusterwide_global_transactions` | `false` | eq | **rac** | low | | 本地事务按多阶段提交协调 |
| 23 | `_keep_remote_column_size` | `true` | eq | all | low | | 避免 DBLINK 建对象时按字符集自动扩列长 |
| 24 | `_cleanup_rollback_entries` | `2000` | **gte** | all | low | ✔ | 加快死事务回滚；★**是「至少」不是「等于」** |
| 25 | `_rollback_segment_count` | `2000` | eq | **oltp** | low | ✔ | 降低回滚段争用；需与并发事务量匹配 |
| 26 | `_sql_plan_directive_mgmt_control` | `0` | eq | all | low | ✔ | 关 SPM directive 自动管理；与自适应统计反馈有耦合 |
| 27 | `autotask_max_active_pdbs` | `4` | eq | **cdb** | - | | **本库非 CDB ⇒ `not_applicable`** |
| 28 | `awr_pdb_autoflush_enabled` | `true` | eq | **cdb** | - | | **本库非 CDB ⇒ `not_applicable`** |
| 29 | `enable_ddl_logging` | `true` | eq | all | low | ✔ | 开 DDL 日志（变更留痕） |
| 30 | `job_queue_processes` | `300` | **show** | all | - | | ★300 是"够大"而非最优；无 job 负载时无意义 ⇒ **不判定** |
| 31 | `_ash_size` | `250M` | show | all | low | ✔ | 与 SGA 成比例；12c+ 已按活动量自动计算 ⇒ 只提示，按 SGA 评估 |
| 32 | `_lm_tickets` | `5000` | eq | **rac** | low | | GES 消息 ticket |
| 33 | `_optim_peek_user_binds` | `false` | eq | all | medium | | 关闭用户绑定变量窥探；与 #1 配套 |
| 34 | `_report_capture_cycle_time` | `0` | show | all | - | ✔ | ⚠ 语义未经核实 ⇒ 暂不进判定 |
| 35 | `_dlm_stats_collect` | `0` | eq | **rac** | low | | 关闭 DLM 统计采集 |
| 36 | `_drop_stat_segment` | `1` | show | all | - | ✔ | ⚠ 语义未经核实 ⇒ 暂不进判定 |
| 37 | `_cursor_reload_failure_threshold` | `36` | show | all | - | ✔ | ⚠ 语义未经核实 ⇒ 暂不进判定 |
| 38 | `_sys_logon_delay` | — | **exception** | — | - | | ★★**与安全基线冲突**：该参数是失败登录延迟（防爆破），设 0 = 关闭防护 ⇒ **不建议照做** |
| 39 | `_use_single_log_writer` | `true` | eq | all | low | ✔★ | 12.2+ 该参数语义/默认已变，Oracle 内部行为已调整 ⇒ 落地前必须核对 |
| 40 | `_serial_direct_read` | `NEVER` | eq | all | **high** | | 关闭自动 serial direct path read（否则全表扫产生 `direct path read`）；19c 取值 `NEVER` |
| 41 | `_part_access_version_by_number` | `FALSE` | eq | partitioned | low | ✔★ | 规避 11.2.0.4「分区 TRUNCATE 后 ORA-8103」；**19c 是否仍需设置需核对** |
| 42 | `_lm_sync_timeout` | `1200` | eq | **rac + sga_gt_gb:100** | low | | 仅大 SGA（>100G）RAC 需要 |
| 43 | `LOG_ARCHIVE_MAX_PROCESSES` | `4` | eq | has_dg | medium | | 避免 standby 产生 gap 时归档突发打满网络带宽 |

### 3.2 分档统计

- **判定方式**：等值 29 / **范围(gte) 2** / 仅展示 9 / **例外 3**
- **适用条件过滤**：**9 条对本库不适用**（RAC 专属 7 条：#6 #7 #15 #22 #32 #35 #42；CDB 专属 2 条：#27 #28）
- **版本敏感需核对**：**16 条**（含 3 条"语义未核实"：#34 #36 #37，落地前先在真库确认参数名是否存在）
- **不建议照做（进例外，不产风险）**：3 条（#14 `audit_trail`、#20 `result_cache_max_size`、#38 `_sys_logon_delay`）

---

## 4. 判定规则（三档 + 判定顺序）

**判定顺序（不可颠倒）**：

```
① 参数在当前版本存在吗？  v$parameter 查不到 / isdeprecated=Y   → not_applicable（写原因）
② 适用条件成立吗？        scope 不匹配（非 RAC/非 CDB/非 OLTP/非分区/SGA 未达）→ not_applicable（写原因）
③ 进例外名单吗？          → exception（输出"按客户合规/业务确认"，不产风险）
④ compare=show 吗？       → 只展示当前值 + isdefault + 说明，不判定
⑤ 值满足 advised 吗？     满足 → passed；不满足 → triggered
```

| 档 | 条件 | 去向 |
|---|---|---|
| **建议调整** | 值偏离 **且** 适用条件成立 **且** 后果明确（级别 ≥ low） | 进**风险台账**（带严重级别） |
| **可选优化** | 适用但不紧急（如隐藏参数仍是默认值） | 只进本章表格 + 综合结论，**不进台账、不扣分** |
| **不适用 / 例外** | 架构或业务不适用，或合规/安全例外 | 记 `not_applicable` / `exception` + 原因，**必须留痕** |

★ 两条硬约束（沿用项目既有纪律）：

- **「未采到」≠「值正常」**：参数没采到（`not_evaluated`）不得输出"符合最佳实践"。
- **判据写成范围/唯一性，不要用 `max`/`min` 兜**：`control_file_record_keep_time` 与 `_cleanup_rollback_entries` 都是「至少」，用等值比对会把"设得更大"误判成偏离。

---

## 5. 判定结果 → 展示（Word）

**落点**：`oracle_instance`（「Oracle 实例检查」）章下**新增一个条目**，位置放在「管理包访问设置」之后。

★ **与现有 `oracle.basic.parameters`（36 个白名单参数）的重叠只有 1 个**：`audit_trail`。它是唯一同时出现在两处的参数 ⇒ 报告里会出现两次（一次在「关键初始化参数」表、一次在基线表）。处理建议：基线表**照旧列出**（口径是"最佳实践判定"，与"参数清单"不同问），但在说明列注明「值见『关键初始化参数』表」，避免读者以为是两回事。

**表格列**（每列都要能回答一个客户会问的问题）：

| 参数 | 当前值 | 是否默认 | 建议值 | 判定 | 说明 |
|---|---|---|---|---|---|
| `_use_adaptive_log_file_sync` | FALSE | 是 | false | 符合 | 已关闭 LGWREF 自适应模式 |
| `control_file_record_keep_time` | 7 | 是 | ≥31 | **建议调整** | 低于 RMAN 保留策略天数，归档/备份记录可能被提前覆盖 |
| `_gc_policy_time` | — | — | 0 | 不适用 | 当前为单实例（非 RAC），本项不适用 |
| `audit_trail` | DB | 否 | — | 按客户确认 | 需按等保/行业合规要求确认，本报告不作建议 |

**「综合结论」自动生成口径**（不喊口号，只报可与表格对上的数）：

> 基线共 43 条：适用 N 条，其中符合 A 条、建议调整 B 条、可选优化 C 条；不适用 D 条（RAC 专属 7 / CDB 专属 2）；按客户合规/业务确认 E 条。
> 建议调整项：`<参数1>`（当前 x → 建议 y，理由 …）、…

★ **三档必须长得不一样**：三档文案不得使用同一句式（沿用项目既有判据：「两个相反结论不许长成一模一样的一句话」）。

---

## 6. 验证计划（怎么证明它是对的）

| # | 手段 | 判据 |
|---|---|---|
| 1 | **双向同源断言** | 基线表里的每个 `name` 必须出现在采集 SQL 的白名单里；采集白名单里的每个 `name` 必须出现在基线表里。**两边不等就红**（防"表加了没采"和"采了没人判"） |
| 2 | **真包回放** | 用 `..._15-03-19.tar.gz` 跑一遍：43 条按 §4 顺序落档，`not_applicable` 条数 == 9 + 版本不存在的条数 |
| 3 | **防漏判 + 防误伤配对** | 「把 `_gc_policy_time` 塞一个偏离值 → 必须变"建议调整"／塞 `not_applicable` 场景 → 必须不变告警」成对写 |
| 4 | **尺子自证** | 把 `compare` 从 `gte` 改成 `eq`，`control_file_record_keep_time=31` 的用例应从 passed 变 **failed**（证明范围判据真的在起作用） |
| 5 | **负控制 + 逐字节复原** | 改实现（非改 model）→ 守门必须真实变红 → `finally` 逐字节复原 + sha256 断言 |
| 6 | 既有守门 | `check_oracle_psv_widths.py` / `check_oracle_psv_records.py <包>` 全过 |

---

## 7. 未决项（需你定的）

| # | 事项 | 选项 | 我的建议 |
|---|---|---|---|
| 1 | 基线表落点 | (a) `plugins/oracle/parameter_baseline.json` 独立文件 (b) 收进 `inspection_rules_oracle.json` 新键 | **(a)** —— 独立文件才不混进 42 条规则命名空间 |
| 2 | 「建议调整」是否进风险台账 | (a) 进（会影响健康分）(b) 只进本章 | **(a)**，但只限 `severity ≥ medium`；`low` 归"可选优化" |
| 3 | 不适用项在报告里怎么显示 | (a) 逐条列出 (b) 折叠成一行汇总 | **(b) 一行汇总**（「另 9 条为 RAC/CDB 专属，本实例不适用」），避免 40 行表里 9 行是废话 |
| 4 | 交付物里怎么表述参考来源 | (a) 写"参考某厂商最佳实践" (b) 写"行业通用基线" | **(b)** —— 别把对家名字写进交付物 |
| 5 | 采集范围 | (a) 只采基线 43 个名字 (b) 顺带采"所有非默认的隐藏参数" | **(a)**，先可控；`(b)` 作为后续独立项 |
| 6 | 与 AWR 章的先后 | (a) AWR 先 (b) 参数先 (c) 并行 | **(a)**；但参数采集只有 1 条 SQL，想先看 Word 效果也可先做 |

---

## 8. 工作量与影响面

| 位置 | 改动 | 影响 |
|---|---|---|
| `inspection/oracle_inspection.sh` | +1 采集项；修/删 1 死项；重写 1 项 | 新采的包才生效；旧包与基线不受影响 |
| `plugins/oracle/parameter_baseline.json`（新） | 基线表 43 条 | 新增文件，不改现有逻辑 |
| `plugins/oracle/`（新分析文件） | 独立判定模块 | 不进 `oracle_rules.py` / 不进 42 条规则配置 |
| `plugins/oracle/presentation.py` | `oracle_instance` 章 +1 条目 | 报告多 1 个小节；**章节数不变**，目录不动 |
| `summary.txt` / 采集质量分 | 无变化 | 新增项为 `ok`，不进"失败明细" |
| 风险台账 / 健康分 | 有变化 | 仅当有 `severity ≥ medium` 的偏离项时 |
