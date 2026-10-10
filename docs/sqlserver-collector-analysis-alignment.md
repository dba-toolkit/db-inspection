# SQL Server 采集脚本 ⇄ 分析层 对账报告

> 范围：`inspection/sqlserver_inspection.ps1`（v1.3.5，采集端）与 `plugins/sqlserver/*`（分析/呈现层）。
> 方法：机器化双向差集 + 真机产物核对（非印象）。可复跑脚本见文末。
> 结论口径：**「采了没出口」= 采集项在报告里既没有表格、也没有判据**；「有分析无判据」= 有定制结论文案但无阈值判定 —— 两者严重程度不同，本报告分列。
> **处置状态（2026-10-09）**：P1① / P1② 已修，P1③（`wait_stats` 判据）已补，`volumes.source` 已补列，
> interface 文档已更正并登记 `command_preview` 豁免。逐项证据与影响面见 **§8**。

## 一句话结论

**骨架完全对得上，边界也干净**；缺的不是"接不上"，而是三类**呈现/判定层的漏点**：

1. 有 1 处**结论被注错检查项**（P1，会读到错误的风险陈述）；
2. 有 1 处**状态与结论自相矛盾**（P1，状态写"正常"、结论写风险）；
3. 有若干**采集了但报告从未展示**的字段（P2，含一个敏感字段）。

**采了却完全没有出口的模块：0 个** —— 49 个采集键全部至少进了报告表格。这一点要先说清楚。

---

## 1. 对得上的部分（四项零差异，有守门测试）

| 对账维度 | 采集端 | 分析层 | 结果 |
|:---|:---|:---|:---:|
| 快照顶层键 | 49 键（脚本实际写出） | `module_catalog.MODULE_BY_SNAPSHOT_KEY` 49 键 | **逐键一致** |
| 规则全集 | 规则包 `rules` 66 条 | `rules.py` 实际请求 66 条 | **无死规则、无幽灵规则** |
| 阈值键 | `thresholds` 47 键 | `rules.py` 请求 47 键 | **无死键、无未声明键** |
| 模块名合法性 | `required_modules` 全部取值 | `known_module_names()` 收录 49 名 | **零非法** |

> 规则包里 5 条 `SEC.EMPTY_PASSWORD_LOGINS` 之类的条目，静态正则看着像"死规则"，实际是
> `rules.py` 用 `f"SEC.{name.upper()}"` 动态拼出的 —— 属于**正则的假阳性**，不是缺陷。

**结论：契约层不需要动。** 下面全是分析/呈现层自己的问题。

---

## 2. 少分析：采了但没有任何阈值判据的模块（16 个）

按 `required_modules` 反查，有 16 个采集模块不被任何规则引用：

| 采集模块 | 建议 | 理由 |
|:---|:---:|:---|
| 等待统计 `wait_stats` | **建议补判据** | 参照 MySQL 有 `MYSQL.LOCK.WAITING` 等；当前只做了 `ignored_wait_types` 过滤，无任何异常等待判定 |
| 高消耗/高逻辑读/高执行/高物理读 SQL（×4） | 不补 | 诊断型数据，硬判阈值必然误报；参照 MySQL 亦只展示 `SQL.NO_INDEX_DIGEST` 一类"线索"而非风险 |
| 缺失索引建议 `missing_indexes` | 不补（可补"线索"） | 索引变更须人工评估，不宜自动定级 |
| 未使用索引 `unused_indexes` | 不补（可补"线索"） | 同上；MySQL 的 `SCHEMA.REDUNDANT_INDEX` 也是线索型 |
| 内存组件分布 `memory_clerks` | 不补 | 归因用；有 `MEM.*` 覆盖进程级结论即可 |
| 系统内存状态 `system_memory` | 不补（见 §3） | 整机内存是 OS 层事实，且当前被误注结论 |
| CPU 按数据库分布 `cpu_by_database` | 不补 | 归因用 |
| 高可用状态 `ha_status` | 不补 | 已被 `availability_replicas` 的 5 条 AG 判据覆盖 |
| 备份历史（近7天） `backup_history` | 不补 | 证据详表；合规性由 `BACKUP.*` 判定 |
| 还原验证历史 `restore_history` | 不补 | 证据详表（"备份存在 ≠ 可还原"已由文案交代） |
| 数据库大小汇总 `database_summary` | 不补 | 汇总口径 |
| 作业步骤 `job_steps` | 不补 | 作业级结果已由 `AGENT.*` 判定 |
| OS 指标 `os_metrics` | **可选补 1 条** | 参照 MySQL 有 `COMMON.SYSTEM.MEMORY_PRESSURE` / `IOWAIT_PRESSURE`；且 OS 采样只在本机执行时才有，口径需先定清 |

**这 16 项都有 `analysis_text` 定制文案**（不是"已采集并展示本项巡检事实"那句套话），所以严格讲是
「**有分析、无判据**」。真正值得动的只有 `wait_stats` 一条 —— 其余按诊断型数据保留展示即可。

---

## 3. 结论被注错的检查项（P1）

`presentation.py:_inject_analysis` 的映射键是 `(section_id 子串, title 子串)`，其中
`('memory', '内存')` 这个 **title 子串命中了同一章节里的三个项**：

| 检查项 | 实际讲什么 | 被注入的规则 | 对不对 |
|:---|:---|:---|:---:|
| 进程内存状态 | `dm_os_process_memory`（进程内存利用率/低内存信号） | `MEM.SIGNALING` / `MEM.PRESSURE` | ✅ 正确 |
| 内存组件分布 | `dm_os_memory_clerks`（各 clerk 占用） | `MEM.SIGNALING` / `MEM.PRESSURE` | ❌ **错配** |
| 系统内存状态 | `dm_os_sys_memory`（整机内存） | `MEM.SIGNALING` / `MEM.PRESSURE` | ❌ **错配** |

真机产物实测：三项的结论**逐字相同** ——
`SQL Server 内存利用率极高（SQL Server Process）`。也就是说，「内存组件分布」这张表
讲的是 clerk 占用，结论却在说"进程内存利用率极高"；同一句风险陈述在一章里出现三次。

**改**：把 `('memory', '内存')` 收窄（改为精确声明到「进程内存状态」），后两项回落到
`analysis_text` 已有的定制文案（L459 / L479，已存在，无需新写）。

---

## 4. 状态与结论自相矛盾（P1）

真机产物里「数据库清单」一项：**状态列 = 正常，结论列 = 三条风险陈述**
（`数据库完整性检查不满足周期(master)` / `数据库 TRUSTWORTHY 已启用(msdb)` / `用户数据库未启用 Query Store(appdb)`）。

根因是**两个算式不同源**：

- 三条 finding 都被 `system_database_guard`（`severity_cap: low`，`applies_to` 含 `DB.CHECKDB`/`DB.TRUSTWORTHY`）
  降到 `low`，或本身就是 `low`（`DB.QUERY_STORE`）；
- 而 `_inject_analysis` 的 status 算式是
  `risk`（有 critical/high）/ `attention`（有 medium）/ `normal`（**只剩 low**）→ 判为 `normal`；
- 但结论列直接把 finding 的 title 拼了进去，**不区分档位**。

于是"正常"和"风险陈述"并排出现在同一行。同类现象会出现在任何"命中的全是 low 档"的检查项上。

**三个可选方向**（本报告推荐 A）：

| 方案 | 做法 | 代价 |
|:---:|:---|:---|
| **A（推荐）** | status 算式补一档：命中的全是 `low` 时判 `attention`（"提示"而非"正常"） | 报告里"关注"项会变多；需同步共享引擎的颜色表 |
| B | 结论列按最高档位措辞（全 low 时改写成"存在改进项"） | 只改文案，状态列不动，仍留"正常/风险"并排 |
| C | 不动 | 客户可能追问"到底正常不正常" |

---

## 5. 采集了但报告从未展示的字段（P2，字段级漏展示）

| 字段 | 出处 | 现状 | 影响 |
|:---|:---|:---|:---|
| `instance_default_data_path` / `_log_path` / `_backup_path` | `instance` | 采集 SQL 有，报告「实例基础信息」表**无这三行** | 契约文档 §4 声称"含默认数据/日志/备份路径"，实际未展示；判断"备份是否落数据盘"缺直接证据 |
| `is_integrated_security_only` | `instance` | 未展示、无判据 | 认证模式（仅 Windows 认证 = 更安全）是该层安全结论的输入 |
| `product_update_level` / `version_banner` | `instance` | 未展示 | CU 级别与完整版本串；`version_banner` 的 build 号本是 OS 产品名映射（G-18）的输入 |
| `os_service_pack_level` / `os_sku` | `instance` | 未展示 | 报告 OS 栏只有 `10.0`，缺产品名 |
| `engine_edition` | `instance` | 未展示 | 与已展示的 `edition` 重复，可留可删 |
| `volumes.source` | `volumes` | **表里缺列**（`DATABASE_FILE`/`XP_FIXEDDRIVES`/`BACKUP_MEDIA`/`ERROR`） | `analysis_text` 已在用该字段写结论，但表里看不出同一挂载点的两行来自不同来源 → 会被读成"重复行" |
| `job_steps.command_preview` | `job_steps` | **零消费** | ⚠ 该字段是采集端登记的**隐私敏感字段**（可能含明文口令/连接串，`-ExcludeSqlText` 负责抹除）。采了却从不读取 = 只承担风险没有收益 → 要么脱敏后展示，要么从采集端移除 |

---

## 6. 契约与文档一致性（P2）

| # | 问题 | 事实 | 建议 |
|:---:|:---|:---|:---|
| 1 | `package_adapter.normalize_snapshot` 的 `setdefault` 列表**漏 8 个键** | 缺 `session_activity` / `server_services` / `memory_clerks` / `system_memory` / `ha_status` / `os_metrics` / `job_steps` / `restore_history`。全部有 `s.get(k, [])` 兜底，**不会崩** | 补齐 8 键，让"契约声明的键集"名副其实。不补的代价：将来任一处改写成 `snapshot["k"]` 即 KeyError |
| 2 | `docs/sqlserver-collector-interface.md` §5 写 manifest = **`files[] + sha256`** | 采集脚本实际只写**文件名列表**（`files=@('snapshot.json','collector.log')`），**无 sha256** | 改文档（这是"撒谎的注释比没注释更坏"那一类）。若想让完整性校验可做，则反过来给采集端补 sha256 |
| 3 | `report_adapter` 写"采集包未提供 manifest 哈希校验明细" | 这句**准确** | 与上一条二选一：补采集端 sha256，或保留现状 |

---

## 7. 建议推进顺序

| 优先级 | 项 | 落点 | 工作量 |
|:---:|:---|:---|:---:|
| **P1** | 修「内存组件分布 / 系统内存状态」结论错配 | `plugins/sqlserver/presentation.py` 的 `_map` | 改 1 行 |
| **P1** | 修「状态正常 / 结论风险」自相矛盾 | `presentation.py` 的 `_inject_analysis` status 算式 | 改 1 处 |
| **P1** | 补 `wait_stats` 第一条异常等待判据 | `rules.py` + 规则包 | 新增 1 条 |
| **P2** | 补漏展示字段（默认路径 / 认证模式 / `volumes.source` / CU 级别） | `presentation.py` 表头与行 | 4 处 |
| **P2** | 处置 `job_steps.command_preview`（脱敏展示或从采集端移除） | 采集端或呈现层 | 一处决策 |
| **P2** | `normalize_snapshot` 补 8 键 | `package_adapter.py` | 改 1 处 |
| **P2** | 修正 interface 文档 §5 的 manifest 描述 | `docs/` | 改 1 行 |
| P3 | OS 层压力判据（可选） | `rules.py` + 规则包 | 新增 1 条 |

> **P1 三条改完必须重跑真机包核对**（两步：`analyze.py <包> -o out` → `generate_report.py out`），
> 并跑全量 `pytest`，把失败项与改前**逐条同名**对齐（本项目常态化 5 项 MySQL 源包缺失的预存在失败）。

---

## 附：可复跑的核对脚本

| 脚本 | 作用 |
|:---|:---|
| `_work/_crosscheck.py` | 规则包 66 条 ⇄ `rules.py` 引用；`thresholds` 死键；`required_modules` 合法性；采集模块有无判据；`_inject_analysis` 映射是否指向未声明规则 |
| `_work/_field_diff.py` | 采集端 `AS` 别名 ⇄ 分析层 `.get("x")`/`["x"]` 的字段差集 |
| `_work/_dump2.py <产物目录>` | 读 `report_model.json`，逐项打印 标题/状态/结论字数/展示列/行数 + 规则四态分布 |

> 脚本只读，不产报告；均在 `_work/`（gitignore）。

---

## 8. 本批处置与验证（2026-10-09）

四处改动全部落到代码/文档，并用**控制变量自证**（同一包、只把改动那一行改回旧形态、其余一字不动）
隔离差异来源。

| # | 改动 | 落点 | 验证结论 |
|:---:|:---|:---|:---|
| ① | 内存章结论错配 | `presentation.py` 的 `_map`：`('memory','内存')` → `('memory','进程内存')` | 9/24 包（46 模块、含 `memory_clerks`）：修复前「内存组件分布」「系统内存状态」status=`risk`、结论与「进程内存状态」**逐字相同**；修复后回落 `normal` + 各自文案；**内存章以外 43 项差异 0** |
| ② | 状态与结论矛盾 | `presentation.py` 的 status 算式补 low 档 | 同包 A/B：48 项中仅 **2 项**变化（`数据库清单`、`数据库文件与IO`：normal → attention），normal 34→32 / attention 6→8 |
| ③ | `wait_stats` 无判据 | 新增 `PERF.WAIT_TOP`（`rules.py` + `inspection_rules.json` + `wait_attention_min_ms` 阈值 + `_map` 挂到「主要等待」） | 白名单 + 60s 累计门槛；两个真机包均**不触发**（候选只有秒级零头：`LCK_M_X` 3.6s / `PAGEIOLATCH_SH` 2.4s），触发路径由 10 项构造快照守门测试证明 |
| ④ | `volumes.source` 漏展示 | 呈现层「存储卷容量」表加「来源」列 | 真机包表头 = `挂载点 / 来源 / 卷名 / 文件系统 / 总量MB / 可用MB / 可用率` |

**文档更正**：`docs/sqlserver-collector-interface.md` §5 的 manifest 由 `files[] + sha256` 改为实情
（`schema_version + collector_version + created_at + files[]`，**不含哈希**）；§4-33 与 §10-7 登记
`job_steps.command_preview` 为「有意保留采集、报告不展示」。

**★ 一处必须记下的坑**：回归基线用的 **7/28 包（38 模块）没有 `memory_clerks` / `system_memory`**，
内存章只渲染 2 项 —— 在这份包上验证 ① 会得出"没有差异"的错误结论。出现"三项逐字相同"的是
**9/24 包（46 模块）**。判据类/呈现类改动，**先确认手上包有没有那条数据**，再谈修没修好。

**回归**：全量 pytest **5 failed / 292 passed / 148 subtests**，5 项与改前逐条同名（MySQL 源包缺失）
→ 零新增回归；`tests/test_sqlserver_analyzer.py` 用 7/28 包实跑对照 `expected_summary`，`risk_register` 仍 20。

**未纳入本批**（用户未选，保留在清单）：其余 15 个诊断型模块补判据、`normalize_snapshot` 补 8 键、
其余漏展示字段、逐域重写综合结论。

**证据脚本**（`_work/`，gitignore）：`_crosscheck.py` / `_field_diff.py` / `_dump2.py`（对账）、
`_apply_changes.py`（补丁，含 dry-run）、`_ab_verify.py`（①）、`_status_ab.py`（②）。
