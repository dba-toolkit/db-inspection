# oracle_inspection.sh 改造清单

- 对象：`inspection/oracle_inspection.sh`（原 3586 行 / 87 个函数 / 注释密度 **4.7%**）
- **当前状态**：批 **0–7 全部落地**（采集脚本，`inspection/oracle_inspection.sh` **4610 行 / 105 个函数 / 注释密度 14.4%**，`COLLECTOR_VERSION` = `SNAPSHOT_SCHEMA_VERSION` = `PACKAGE_VERSION` = **2.0**，批 5–7 均**无新采集项**、版本号不动）；批 **8 / 10 / 11 / 12 / 12b / 13 / 14 在 Python 侧**（采集脚本零改动）：批 8（2026-10-10）让 `plugins/oracle/package_adapter.py` **真正消费 2.0 包**（`collector_version` 分叉 + `.psv` + 1.4→2.0 查找键桥接）；批 10（2026-10-10）把**分析层 / 报告层对齐 MySQL**（补齐「待客户确认事项」+ 修正章节动线）；批 11（2026-10-10）**规则补口** —— 补齐 DataGuard / 对象治理 / 口令 Profile / 大页四个"采了有数据、零判据"的域（规则包 **34 → 42**），并整体刷新 Oracle 基线；批 12（2026-10-10）**图表收口** —— 图的「声明集合 = 可产出集合」（单一来源 `ORACLE_CHART_IDS` + `oracle_txn_rate` 上架 + 共享引擎不再静默 `return False`）；批 13（2026-10-10）**展示补口** —— 把「采了有数据、一个字不进报告」的四个域（AWR 段膨胀/高水位/索引使用/统计缺失、DataGuard、审计画像、undo/temp/共享池）接进报告（检查项 **52 → 76**，章节数不变）；批 14（2026-10-10）**取数口径统一** —— 呈现层不再自拼包内路径，非表格式证据文件一律走共享访问点 `PackageContext.raw_lines`（Oracle 6 处 + MySQL 5 处；规范化比对**零字节变化**，基线未动）。批 12b（2026-10-12）**图表补口** —— 补三张「包内有数据、报告里没有图」的图（表空间使用率 Top-N 条形 / Top SQL 执行时间 Top-N 条形 / 每日归档量趋势折线），并把**条形渲染入口下沉公共层** `render_bar_spec`（纯新增、四库基线零影响）；图 **8 → 10**。批 15（2026-10-12）**报告质量修复** —— 用户拿真实报告（`192.168.100.80_db01`）与**同客户 MySQL 报告**逐条比对后挑出的六处：伪文件系统不进容量判据（P0-1，光驱 `iso9660` 是只读介质、恒定 100% 满 ⇒ 真值 33%）、**表空间图与表 6.1 / 规则层同口径**（P0-2，`USED_MB ÷ MAX_MB`）、4.5 键名对齐 `parse_df_pt`（P0-3）、规则**通过**项也带实测值（P1-2，全文「未提供补充依据」**66 → 4**）、性能表一张只留一种行形（P1-3）、规则项正文不再印通用占位（P1-4，落在**共享引擎** `inspection_core/word_engine.py`、四库同享）。业务结果**不变**（健康分 23 / 规则 42 / 风险 11 / 章节 9 / 检查项 76 / 图 10），图里**只有** `oracle_tablespace_usage.png` 变了（正是 P0-2）。
- **批 8（2026-10-10）不在采集脚本里**：转 **Python 侧** —— `plugins/oracle/package_adapter.py` 此前对所有 `tables/*.tsv` 一律按 `|` 解析，**且完全忽略 `tables/*.psv`** ⇒ 2.0 包只读出 5 张垃圾表，34 条规则里 **16 条 `not_evaluated`**。本批按 `collector_version` 分叉 + 重建 1.4→2.0 的查找键桥接，实测 `not_evaluated` **16 → 2**（余下 2 条是"无 ASM / FRA 未配置"的环境事实）。**collector 侧自此无遗留代码项。**
- `_work/_pkg2_check_report.md` / `_work/_pkg3_check_report.md` = 两轮体检报告（15 条 + 6 条发现 + 批次建议）；验收脚本 `_work/_b1_make_verify.py` → `_work/_verify_b1.sh`（**T1–T26，PASS=139 / FAIL=0**），另有第四道静态守门 `_work/_scan_record_status.py`。
- **仍需现场确认**：① N-4 用同型号实例复采（ORA-00904 消失）；② OF-19 中断路径构造一次；③ 批 7 的复采核对（见「批 7」章节末尾）。
- 参照：`inspection/mysql_inspection_standard.sh`（1863 行 / 80 个函数 / 注释密度 **15.8%**）
- 配套契约：`docs/oracle-collector-interface.md`（**下游唯一对接依据**）
- 审查日期：2026-10-09
- 行号基准：本文所有行号对应 `oracle_inspection.sh` 的**当前工作副本**；按行号找不到时**以符号名（函数名 / 变量名 / 字符串字面量）为准**。

> 用户只问"有什么提升或优化"。但采集脚本的问题清单**必须配一份 Python 对接契约**，否则改完不知道下游怎么改。所以本文 + `oracle-collector-interface.md` 是成对的，缺一不可。

---

## 0. 三句话结论

1. **骨架不乱，边界乱。** 函数命名（`collect_*` / `detect_*`）、模块开关、后台并行、状态登记、打包、安全扫描、信号处理都有，且与 MySQL 版同源，**值得保留**。乱在四处：OS 采集两套并存、item_id 由中文标题派生、category 不成体系、工作目录与产物目录混在一起。
2. **最要命的一条不在 OS 层，在状态登记层**：`record_status` 的 `item_id` 由**中文标题**派生，`sanitize_filename` 把中文全部吃掉 → **136 个 SQL 采集项只剩 66 个唯一 id，其中 48 项共用一个 `sql..tsv`（覆盖写）**；27 个模块名同样塌陷到 `module.` / `module.AWR` / `module.RAC`。整份采集包的状态台账是坏的。
3. **OS 层最大的洞是"这些 OS 数据属于谁"没人能证明。** `AGENTS.md` 明文禁止"远程采集端主机数据参与目标数据库资源评价"，而 Oracle 采集器允许 `--host <远端>`，此时 `collect_system_static` / `collect_sar_history` / 实时采样 采的全是**采集机**的 OS 数据，`snapshot.json` 里**没有任何字段**能让 Python 判断这件事。MySQL 有 `TARGET_IS_LOCAL` + `host_identity.database_target_is_local` + 终端告警，Oracle 全无。

---

## 0.1 可靠性边界（哪条可以直接采信）

| 档     | 含义                                   | 本文适用条目                                                                                                                                                                                        |
| ----- | ------------------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **A** | 代码事实 + **真实包实测** / **构造环境实测**（可直接采信） | OF-01、OF-02、OF-03、OF-06、OF-08、OF-10、OF-11、OF-12、OF-14、OF-15、OF-16、OF-17、OF-20、OF-21、OF-22、OF-23、OF-24、OF-25、OF-27、OF-28，以及 §0.5.3 的实测-1 / 实测-2 / 实测-3；**OF-05**（批 3 已用 stub 构造三分支实测，见 §0.5.5） |
| **B** | 官方手册原文                               | OF-04（sqlplus `COLSEP`/`TRIMSPOOL` 行为）、OF-09（Oracle 大页 / sysv shm 语义）                                                                                                                         |
| **C** | 真实包**未触发**该路径，仍需现场构造验证               | **OF-19**（需构造收集中断）                                                                                                                                                                            |

**已拿到一份真实采集包**（`oracle_inspection_task_ZYDB_…_2026-10-09_22-47-04.tar.gz`，`collector_version`=1.4，与 `inspection/oracle_inspection.sh` 工作副本一致，证据链成立）→ 原 8 条 C 档**全部落定**，其中 **2 条需修正口径**（OF-07 / OF-26），另 2 条**原文准确**（OF-18 / OF-27），详见 **§0.5**。

**已实测的（本机 Git Bash + 真实包）**：

```
sanitize_filename "数据库基本信息"      ->  ""            (item_id 变成 "sql.")
sanitize_filename "生成AWR性能报告"     ->  "AWR"         (item_id 变成 "sql.AWR")
sanitize_filename "AWR 快照状态（确认…）" -> "AWR_AWR"
从脚本抽取 136 条 ora_sql_to_md 标题跑一遍：
  SQL 采集项总数 136   唯一 item_id 66   塌陷到 "sql." 的有 48 条
```

---

## 0.2 及格线 vs 加分题（两个坐标轴，别混着看）

| 坐标轴                     | 定义                    | 条目                                                                                                                                                                                                                                                                                                          |
| ----------------------- | --------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **及格线**（代码正确性缺陷，不改就是错的） | 结论/状态/文件格式与事实不符，下游会算错 | OF-01 状态台账塌陷、OF-02 "TSV" 不是 TSV、OF-03 SQL 错误误分类、OF-04 每条 SQL 跑两遍、OF-05 扫描拦截仍记 ok、OF-06 OS 归属无法证明、OF-07 时区证据缺失、OF-08 命令无超时、OF-10 密码进 argv、OF-16 包内无状态汇总、OF-17 无退出码契约、OF-21 版本号三处不一致、OF-22 row_count 恒为 `-`、实测-1 空结果全记成 `ok`、实测-2 两入口文件名永不相等、实测-3 sql 层 id 不可逆                                                |
| **加分题**（缺采集项/可优化，没有也能跑） | 少采、采得浅、可读性差           | OF-09 缺 Oracle 专属 OS 视图、OF-11 缺 limits 三层口径、OF-12 缺 oracle 进程快照、OF-13 缺能力探测、OF-14 缺 `--output-dir`、OF-15 OS 采集两套并存、OF-18 缺 `sar -w`（coverage 已有）、OF-19 缺 `COLLECTION_INCOMPLETE`、OF-23 无 `collection_status.json`、OF-24 注释接口层空白、OF-25 dry-run 漏项、OF-26 错误原文被删、OF-27 snapshot 字段太薄、OF-28 `safe_relpath` 重复定义 |

**"未采集 ≠ 0"**：Oracle 的 status 取值全集只有 9 个（`ok / empty / error / timeout / unsupported / permission_denied / not_enabled / skipped / partial`），比 MySQL 少 `not_applicable` 与 `schema_mismatch`。**adapter 现在必须自己推断**，这是契约缺口（`docs/cross-database-capability-matrix.md` §采集状态 已登记）。

---


## 0.3 清单汇总

|   编号  | 项目                                                                                                                               |   级别   | 坐标轴 |  批次  | 影响                                                      |
| :---: | -------------------------------------------------------------------------------------------------------------------------------- | :----: | :-: | :--: | ------------------------------------------------------- |
| OF-01 | `record_status` 的 item_id 由中文标题派生 → 48 项塌陷                                                                                       | **P0** | 及格线 | 批1 ✅ | Python 状态台账全废                                           |
| OF-02 | `tables/*.tsv` 实为竖线分隔、中文名、空结果只写注释（无列名行、无占位契约）                                                                                    | **P0** | 及格线 | 批1 ✅ | 下游按 TAB/列名解析必失败                                         |
| OF-06 | OS 数据归属无法证明（缺 `TARGET_IS_LOCAL`）                                                                                                 | **P0** | 及格线 | 批2 ✅ | 违反 AGENTS.md 硬规则                                        |
| OF-07 | 缺 `time_evidence` → 时区无声明（`sadf` 无 `-t` 是四库共性，**不改**）                                                                            | **P0** | 及格线 | 批2 ✅ | 历史 sar(UTC) 与实时采样(本地)无对齐依据                              |
| OF-03 | `ORA-00904` 被归 `unsupported`，无 `schema_mismatch`                                                                                 |   P1   | 及格线 | 批1 ✅ | 脚本 SQL 写错被伪装成环境不支持                                      |
| OF-04 | 每条 SQL 跑两遍 sqlplus（md 一遍 / tables 一遍）                                                                                            |   P1   | 及格线 | 批1 ✅ | 耗时翻倍 + 两处数据不一致                                          |
| OF-05 | 敏感扫描拦截后 `module.structured_packaging` 仍记 ok                                                                                      |   P1   | 及格线 | 批3 ✅ | 流水线分不清"成功"和"被拦下"                                        |
| OF-08 | OS 采集命令无超时包装（全脚本无 `run_with_timeout`）                                                                                            |   P1   | 及格线 | 批2 ✅ | 一条 hung 命令拖死整包                                          |
| OF-10 | 密码走 sqlplus argv，`ps -ef` 可见                                                                                                     |   P1   | 及格线 | 批3 ✅ | 采集期间同机用户可读                                              |
| OF-11 | 资源限制只读 `/etc/security/limits.conf` 一个文件                                                                                          |   P1   | 加分题 | 批2 ✅ | 报"没配 nofile"的假结论                                        |
| OF-12 | 缺结构化 Oracle 进程快照（ps 只进 md）                                                                                                       |   P1   | 加分题 | 批2 ✅ | 判不出残留 PMON / 多 ORACLE_HOME                              |
| OF-13 | 无能力探测表 `capabilities.tsv`                                                                                                        |   P1   | 加分题 | 批1 ✅ | 下游只能按 status 反推能力                                       |
| OF-16 | 包内无 `collection_status.json` / `summary.txt`                                                                                     |   P1   | 及格线 | 批1 ✅ | 已登记在跨库能力矩阵                                              |
| OF-17 | 无退出码契约                                                                                                                           |   P1   | 及格线 | 批3 ✅ | 自动化无法按码判断                                               |
| OF-09 | 缺 Oracle 专属 OS 视图（ipcs / 大页实测 / ASM 共享存储）                                                                                        |   P1   | 加分题 | 批2 ✅ | SGA、ASM 故障无证据                                           |
| OF-14 | 无 `--output-dir`，只能改脚本或写 config 文件                                                                                               |   P1   | 加分题 | 批4 ✅ | 现场体验                                                    |
| OF-15 | OS 采集两套并存，md 那套不进包                                                                                                               |   P2   | 加分题 | 批2 ✅ | 同一事实两处口径                                                |
| OF-18 | sar 缺 `-w`（进程/上下文切换）与 `coverage.json`                                                                                            |   P2   | 加分题 | 批2 ✅ | 与 MySQL 版差 1 类指标                                        |
| OF-19 | 中断无 `COLLECTION_INCOMPLETE` 标记                                                                                                   |   P2   | 加分题 | 批4 ✅ | 下游无法程序化判残缺                                              |
| OF-20 | `ora_value` 不过滤 ORA- → ASM 探测静默误判 + 报错                                                                                           |   P2   | 及格线 | 批1 ✅ | 无权限被读成"没有 ASM"                                          |
| OF-21 | 版本号三处不一致（v1.5 / 1.4 / 硬编码 1.0）                                                                                                   |   P2   | 及格线 | 批1 ✅ | 契约版本无从追踪                                                |
| OF-22 | SQL 项的 `row_count` 恒为 `-`                                                                                                        |   P2   | 及格线 | 批1 ✅ | 下游拿不到行数                                                 |
| OF-23 | 无 `collection_status.json` 聚合（与 OF-16 同源）                                                                                        |   P2   | 加分题 | 批1 ✅ | —                                                       |
| OF-24 | 接口层零注释（核心函数 `ora_sql_to_md` 无注释）                                                                                                 |   P2   | 加分题 | 批0 ✅ | 改一处影响 136 项                                             |
| OF-25 | dry-run 预览漏 `采集系统静态信息`                                                                                                           |   P2   | 加分题 | 批1 ✅ | 预览与实际不一致                                                |
| OF-26 | `capture_command` 删掉错误原文，不落模块日志                                                                                                  |   P2   | 加分题 | 批4 ✅ | 失败原因只剩 5 行截断                                            |
| OF-27 | `snapshot.json` 字段太薄（缺 8 个段）                                                                                                     |   P2   | 加分题 | 批1 ✅ | adapter 归一的前提                                           |
| OF-28 | `safe_relpath` 重复定义两次（L391 / L3202）                                                                                              |   P2   | 加分题 | 批0 ✅ | 无功能影响，纯冗余                                               |
|  实测-1 | `empty` 在 `ora_sql_to_md` 路径永不触发（判据读 stderr，消息在 stdout）                                                                          | **P0** | 及格线 | 批1 ✅ | 空结果全被记成 `ok`                                            |
|  实测-2 | `tables/` 与 `status/` 是**两套文件名规则** → 永不相等                                                                                        | **P0** | 及格线 | 批1 ✅ | 两个入口之间是断的                                               |
|  实测-3 | `module` 层只塌文件名（可恢复）、`sql` 层内容一起塌（不可恢复）                                                                                          |   P1   | 及格线 | 批1 ✅ | id 不可逆推                                                 |
| OF-29 | 新增：**未加引号 heredoc 体内出现反引号**（自测抓出，见 §批1-B1）                                                                                       | **P0** | 及格线 | 批1 ✅ | 正常项状态被静默打成 `error`                                      |
| OF-30 | 新增：`security_scan` 记录的 `status/security_scan_findings.txt` **在包里根本不存在**（findings 建在 STATUS_DIR，而拷贝发生在扫描之前）                       |   P2   | 及格线 | 批4 ✅ | status 指向一个不存在的证据文件                                     |
|  N-1  | 新增（2.0 实包体检）：`ora_sql_to_md` 错误判据**未锚行首** → 告警日志类数据里的 `ORA-` 被当成报错                                                               | **P1** | 及格线 | 批5 ✅ | 正常项被判 `error`，SQL 失败数虚高（**批 1 引入的回归**）                  |
|  N-2  | 新增：失败 SQL 的 `tables/<id>.psv` 落 **sqlplus 报错回显**，`row_count` 记成 3                                                                | **P1** | 及格线 | 批5 ✅ | 下游按首行列名/`\|` 解析必错                                       |
|  N-3  | 新增：包内 `status/` + `collection_status.json` **不含** `package.security_scan`（写于 `cp` 之后），注释却称"随包回传"                                 | **P1** | 及格线 | 批5 ✅ | 包内台账项数 < 控制台，差额无人能解释                                    |
|  N-4  | 新增：`V$RMAN_STATUS` **无 `SESSION_KEY` 列** → ORA-00904，`rman_validate` 项**恒失败**                                                    | **P1** | 及格线 | 批5 ✅ | 该项永远采不到（第一次采集即命中）                                       |
|  N-5  | 新增：`summary.txt` 漏 `partial` 行、控制台另算一遍台账（重复计数）⇒ **包 156 / 控制台 159**                                                              | **P1** | 及格线 | 批5 ✅ | 台账对不上账，且无人能查                                            |
|  N-12 | 新增：`snapshot.artifacts.report_md` 指向**包外**路径，包内没有该文件                                                                             |   P2   | 及格线 | 批5 ✅ | 下游按包根解析必 miss（同 OF-30 类）                                |
|  N-6  | 新增（2.0 实包体检）：控制台「执行模块: 26」< `MODULE_PLAN` 28 —— bg 模块的计数自增丢在**子 shell** 里                                                        |   P2   | 及格线 | 批6 ✅ | 人读计数与模块清单对不上                                            |
|  N-7  | 新增：`status/sar_files.txt` 混进台账目录（非状态记录），且与 `history/source_files.txt` 内容重复                                                       |   P2   | 及格线 | 批6 ✅ | 污染台账遍历                                                  |
|  N-8  | 新增：`tables/` 里 4 个 `free`/`df`/`df -i`/`lsblk` 的**列对齐文本**挂着 `.tsv` 扩展名                                                           |   P2   | 及格线 | 批6 ✅ | 下游按分隔符切静默错位（`lsblk` 值含空格，无法无损 TSV 化）                    |
|  N-9  | 新增：`capabilities.sqlplus_version` 恒为空（`sqlplus -V` 输出走 stderr 被 `2>/dev/null` 丢弃）                                                |   P2   | 及格线 | 批6 ✅ | 能力表出现空值，判不出"没装"还是"没探到"                                  |
|  N-10 | 新增：`host_identity` 里 `is_local_target` 与 `database_target_is_local` 同义重复                                                         |   P2   | 及格线 | 批6 ✅ | 同一个布尔两个键并列，下游不知读哪个                                      |
|  N-11 | 新增：`manifest.json` 不自列（305/306），且没有任何声明                                                                                          |   P2   | 加分题 | 批6 ✅ | 消费者会以为"清单漏列了一个文件"                                       |
|  N-13 | 新增：`system.listener_status` 判 `error`，实为 listener 未运行（本机 bequeath 采集）                                                            |   P2   | 及格线 | 批6 ✅ | 采集质量无端扣分、误导现场以为脚本坏了                                     |
|  N-14 | 新增：`system.ifconfig` / `system.ntpq_peers` 是互斥 `elif` 分支 ⇒ 本机**永不出现**                                                            |   P2   | 加分题 | 批6 ✅ | 下游分不清"环境不适用"与"该采却没采到"                                   |
|  N-16 | 新增（复采包体检）：归档一步写**两条** status（`oracle.module.structured_packaging` 恒 ok + `module.structured_packaging` 真 rc）                     | **P1** | 及格线 | 批7 ✅ | 控制台"包内 = 总数 − 1"其实错（实际 − 2）；tar 失败时两条结论**相反**；多一个不合规 id |
|  N-17 | 新增：`snapshot.collection_summary.total`(158) ≠ `collection_status.json.summary.total`(159) —— snapshot 生成在 `security_scan` **之前** | **P1** | 及格线 | 批7 ✅ | 第一入口的计数不可信；"三处同源"这句话是假的（同一函数 ≠ 同一时点）                    |
|  N-18 | 新增：`record_status` 参数错位 **4 处**（少传 `exit_code`）⇒ 说明文字挤进 `output_file` 列、`reason` 变空                                              | **P1** | 及格线 | 批7 ✅ | `output_file` 指向**不存在**的"文件"；**编译期全绿、运行不报错**（已加第四道守门）   |
|  N-19 | 新增：集合型项的 `output_file` 写**目录**（`history/` / `timeseries/`）                                                                       |   P2   | 及格线 | 批7 ✅ | 违反"必须指向包内存在的文件"；判 empty 时目录里可能根本没有产物                    |
|  N-20 | 新增：`tables/kernel_parameters.tsv` **无表头**（其余真 TSV 都有）；且缺 `sysctl` 时**无任何记录**                                                     |   P2   | 及格线 | 批7 ✅ | "首行即列名行"对它是假的；又一个"永不出现"项（与 N-14 同类）                     |
|  N-21 | 新增：`interface` §7 的 `partial` 行过期（称"目前只有 `timeseries.realtime_sampling`"）                                                        |   P2   | 加分题 | 批7 ✅ | 文档与实测不符（`system.sar_history` 也会 partial）                |

---

## 0.4 明确是"有意为之"、**别改回去**（写给下一轮审查 / 下一个 AI 会话）

这些写法看起来像缺陷，其实是设计选择。不写清楚，下轮会被反复当缺陷提：

1. **AWR / ASH / ADDM 默认全关，必须 `--diagnostics-pack` 显式确认许可** —— 正确，别改成默认开。
2. **`CONFIG[RMAN_VALIDATE]=0` 默认关** —— 正确（`RESTORE DATABASE VALIDATE` 开销大）。
3. **`WHENEVER SQLERROR CONTINUE` + `set +e` + `run_module` 容错** —— 有意：单条 SQL 失败不能中断整包。**代价是必须靠 `status` 把失败暴露出来**，所以 OF-01 的塌陷才会是 P0。
4. **`capture_command` 在 stderr 为空时从 stdout 借前 500 字节做分类** —— 有意（chronyc/ntpq 一类把错误写到 stdout）。
5. **空结果要写占位内容** —— 本意正确（否则下游拿到 0 字节文件）。**错的是格式**（OF-02），不是"该不该写"。
6. **`ps -ef | grep -iE "(ora_|asm_|oracle)" | grep -v sqlplus`** —— 有意排除 sqlplus 行以免连接串泄露；但见 OF-10，正确的修法是别把密码放 argv，而不是靠 grep 过滤。
7. **`db_block_size` 等值在 SQL 里用 `SUBSTR()` 截断 / `COLUMN ... FORMAT`** —— 有意，防止 `VARCHAR2(4000)` 撑爆输出。
8. **采集器只采集事实、不做判定** —— 项目硬约束，任何"顺手加个健康分"的改动都不接受。

---

## 批 0：注释补全（零逻辑改动，最先做）

### OF-24 全脚本注释补全【P2 · 零逻辑改动】

**现状（实测）**：注释密度 4.7%（170 行注释 / 3586 行 / 87 函数），MySQL 版 15.8%。抽查结果符合"难点都注释了、接口一行没写"：

- ✅ 有"为什么"的：`# 过滤连接串中可能包含密码的 sqlplus 行`（L870）、`# 光驱…`（无）、`# 防止…逐行注释`（L557-558 的 COLUMN 说明）、`# XML 解析无结果，回退到文本格式扫描`（L1865）。
- ❌ 零注释的**改一处影响全部 item** 的底层函数：
  - `ora_sql_to_md`（L530-663）—— 支配 **136 个采集项**，无一行 L4 注释（MySQL 的对应函数 `mysql_query_tsv` 有 20+ 行）。
  - `record_status`（L381）、`capture_command`（L393）、`classify_failure`（L366）—— 全部裸代码。
- ❌ `set +e`（L18）/ `umask 077`（L19）/ `NLS_LANG`（L22）无一字解释。

**做**：按 `mysql-collector-interface.md` §9 的五层结构补（L1 文件头 / L2 阶段分隔 / L3 模块入口 / L4 采集引擎 / L5 其余函数头），目标密度 **≥20% 且不超 30%**。顺序放在所有逻辑改动之前——① 零风险；② 补注释的过程就是契约自检，`item_id` 错位、表头漂移会自己浮出来；③ 能提前标出"会被本轮改动打假"的旧注释。

**验收（指纹法，唯一能证明"零逻辑改动"的方式）**：

```bash
cd inspection
cp oracle_inspection.sh oracle_inspection.sh.bak
# ……只加注释……
strip() { grep -v '^[[:space:]]*#' "$1" | grep -v '^[[:space:]]*$'; }
diff <(strip oracle_inspection.sh.bak) <(strip oracle_inspection.sh) && echo "逻辑零改动 ✓" || echo "夹带了逻辑改动 ✗"
bash -n oracle_inspection.sh
```

### OF-28 `safe_relpath` 重复定义两次【P2】

**代码事实**：L391 与 L3202 是**逐字相同**的两份定义。后者覆盖前者，无功能影响，但说明文件是按批次拼接出来的、没人通读过全篇。

**改**：删掉 L3202 那份（保留工具函数区的 L391）；`OF-24` 补注释时顺手做，作为 L3/L5 注释块的印证。

---

## 批 1：数据正确性 + 结构化契约（**不改完不能上生产**）✅ 已完成（2026-10-09，仅动 `inspection/oracle_inspection.sh`；collector / snapshot schema / package 版本**统一升到 2.0**）

### OF-01 `record_status` 的 item_id 由中文标题派生，48 项塌陷到同一个文件【P0】

**代码事实**：

```bash
# L530-536  ora_sql_to_md（支配 136 个采集项）
item_id="sql.$(sanitize_filename "${title:-unnamed}")"
# L751-758  run_module（27 个模块）
record_status "module.${module_name}" "module" ...
# L385-388  record_status：**覆盖写**，不是追加
f="${RUNTIME[STATUS_DIR]}/$(sanitize_filename "$item_id").tsv"
printf '...' ... > "$f"
# L299-301
sanitize_filename() { printf '%s' "$1" | tr -c 'A-Za-z0-9._-' '_' | sed -e 's/__*/_/g' -e 's/^_//' -e 's/_$//'; }
```

`sanitize_filename` 只保留 `A-Za-z0-9._-`，中文全部变成 `_` 再被 `sed` 收成空串。**本机实测**：

| 输入（脚本里的真实标题 / 模块名）                   | `sanitize_filename` 结果 | 落到状态文件名                                        |
| ------------------------------------ | :--------------------: | ---------------------------------------------- |
| `数据库基本信息`、`实例信息`、`用户列表`、…（48 条纯中文标题） |          `""`          | `status/sql..tsv`                              |
| `生成AWR性能报告`、`采集AWR与统计信息`             |          `AWR`         | `status/sql.AWR.tsv` / `status/module.AWR.tsv` |
| `采集RAC专项`、`采集RAC高可用`                 |          `RAC`         | `status/module.RAC.tsv`（两条互覆盖）                 |
| `采集系统信息`、`实时时序采样`、`采集归档日志`…（16 个）    |          `""`          | `status/module..tsv`                           |

抽取脚本里全部 136 条 `ora_sql_to_md "<标题>"` 实测：**唯一 item_id 只有 66 个，48 条标题塌陷到 `sql.`**。

**后果**：`status/` 目录里的台账被大幅覆盖 —— 模块层只剩最后执行的那一个模块的记录；SQL 层 48 项互相覆盖，最后一条赢。于是 `_status_summary_*.txt` 的"采集项总数 / 失败项详情 / 空结果项"三张表全部失真（L3544-3565）。更糟的是它**看起来一切正常**：脚本照常产出包、状态文件非空、没有报错。这与 `docs/cross-database-capability-matrix.md` 里"**不保留 Oracle 报告 item 缺少稳定 item_id 的情况**"是同一条。

**改**（**破坏性变更**，建议随契约主版本一起做；详见 INTERFACE §7 规则 1）：

```bash
# 方案 A（推荐）：item_id 显式传入，不再派生
#   改 oraclesql_to_md 签名为 ora_sql_to_md <item_id> <标题> <SQL>
ora_sql_to_md "oracle.basic.database" "数据库基本信息" "
SELECT name, db_unique_name, ..."
#   item_id 一律 ASCII 点分，形如 oracle.<域>.<项>，与 MySQL 的 mysql.<域>.<项> 对齐
#   状态文件 = status/$(sanitize_filename "$item_id").tsv  -> status/oracle.basic.database.tsv

# 方案 B（最小改动，不推荐）：只把派生源从标题换成稳定 id
rename_map: 标题 -> 英文 id 的关联数组，未登记则回退 "sql.$(printf '%s' "$title" | cksum | awk '{print $1}')"
#   缺点：id 不可读、不可 grep、跨版本不稳定（cksum 会随标题文字变）
```

**同时改**：`tables/` 的文件名目前用中文标题（`tsv_filename` 保留中文），跨系统回传/解压时会踩 locale 与编码问题，也建议一并换成 `item_id`。

**验收**：跑一遍（或 dry-run 到 `collect_structured_package` 前），断言 `ls status/*.tsv | wc -l` **= 名义采集项数（当前 186：20 项 system.static + 1 sar + 1 实时 + 27 模块 + 136 SQL + 1 security_scan）**，且 `awk -F'\t' '{print $1}' status/*.tsv | sort | uniq -d` 为空。

---

### OF-02 `tables/*.tsv` 不是 TSV：`|` 分隔 + 空结果只写注释【P0】

**代码事实**（L637-660）：

```bash
local tsv_file="${TABLES_DIR}/$(tsv_filename "${title:-unnamed}").tsv"
"${DB_CONN[SQLPLUS_BIN]}" -S "$(get_ora_conn)" > "$tsv_file" 2>/dev/null <<EOF
...
SET HEADING ON
SET COLSEP '|'          # ← 分隔符是竖线，不是 TAB（sqlplus 无法输出裸 TAB）
...
EOF
if [ ! -s "$tsv_file" ]; then
    printf '# no rows returned\n' > "$tsv_file"   # ← 占位内容是注释，连列名都没有
fi
```

三个独立问题：

1. **分隔符是 `|` 而不是 TAB**，扩展名却叫 `.tsv`。下游按 TAB 切分得到 1 列。
2. **空结果占位是 `# no rows returned` 一行注释**，不含任何列名。MySQL 的做法是 `known_tsv_header` 补一份**真实列名**（L269-322），空结果时下游至少知道 schema。
3. **列名行由 sqlplus 按 `COLUMN ... FORMAT aNN` 的宽度输出**（L559-604 定义了几十个 `FORMAT a30/a50/...`），因此列名与值都会带**内部填充空格**；`TRIMSPOOL ON` 只裁行尾。B 档（sqlplus 手册行为），需现场用真实包确认一眼。

**参考对照**：MySQL 侧是 `mysql_exec --batch`（客户端负责 `\t` / `\n` / `\\` 转义）+ `known_tsv_header` 占位表头，**真 TSV + 真有列名**。

**改**（两条路线，选一条，别混）：

```bash
# 路线 1（推荐，改动最小、最稳）：承认它不是 TSV，就别叫 TSV
#   a. 文件名后缀 .tsv -> .psv（pipe-separated values），INTERFACE 里写明分隔符是 '|'
#   b. 空结果占位改成真正的"列名行"（pipe 分隔、无填充），补一个 known_psv_header <item_id>
#      返回该 item 的列名清单（与各 SQL 的 SELECT 列表一一对应，改 SQL 必须同步改这里）
if [ ! -s "$tsv_file" ]; then
    printf '%s\n' "$(known_psv_header "$item_id")" > "$tsv_file"   # 例："NAME|DB_UNIQUE_NAME|DATABASE_ROLE|..."
fi

# 路线 2（真 TSV，仅在确认客户端 >= 12.2 时可用）：sqlplus 的 CSV/MARKUP 模式
SET MARKUP CSV ON DELIMITER '|' QUOTE ON
#   sqlplus 仍然不产出裸 TAB，需再串一层 awk 把 '|' 转 '\t' 并逐列 strip 掉填充空格
```

**验收**：`file tables/*.psv` 与 `head -1` 目视；写一条断言 —— **每个非空 psv 文件的列数必须等于该 SQL 的 SELECT 列数**，`known_psv_header` 的列数与对应 SQL 的列数必须一致（这条断言是"两处硬编码必须同步"的机器化版本，MySQL 侧靠 `tests/test_*.py` 守，Oracle 侧现在没有）。**注意"有意为之"**：`COLUMN ... FORMAT` 的截断不能删（删了 `VARCHAR2(4000)` 会撑爆输出）。

---

### OF-03 `ORA-00904`（列不存在）被归成 `unsupported`【P1】

**代码事实**（L366-378）：

```bash
elif grep -Eqi "doesn.t exist|unknown table|unknown system variable|unknown column|not supported|unsupported|ORA-00942|ORA-00904" "$err_file"; then
    printf 'unsupported'
```

`ORA-00942`（表或视图不存在）= **环境真不支持**，`ORA-00904`（无效标识符 / 列名不对）= **脚本 SQL 与本版本列集不匹配 = 采集器自身缺陷**。两条混在一起，后果是：脚本写错列名时，下游判定"该环境采不到此项"，**既不告警也不提示，缺陷永不浮出水面**。这与 MySQL 侧已修的 `F-21`（`unknown column` 误归 `unsupported`）是同一个坑，Oracle 版**未修**。

另外：Oracle 全脚本 `schema_mismatch` 出现 **0 次**，即状态取值全集比 MySQL 少 `schema_mismatch` 与 `not_applicable`（实测 9 个 vs MySQL 11 个）。

**改**（照抄 MySQL L235-250 的判定顺序，`schema_mismatch` 必须排在 `unsupported` **之前**）：

```bash
classify_failure() {
    local rc="$1" err_file="$2"
    if [ "$rc" -eq 124 ] || [ "$rc" -eq 137 ]; then printf 'timeout'; return; fi
    if grep -Eqi 'ORA-01031|ORA-00942.*insufficient|access denied|permission denied|requires.*privilege' "$err_file"; then
        printf 'permission_denied'
    elif grep -Eqi 'ORA-00904|invalid identifier|ORA-01722' "$err_file"; then
        # 必须在 unsupported 之前：ORA-00904 的文本是 "ORA-00904: \"X\": invalid identifier"
        printf 'schema_mismatch'
    elif grep -Eqi 'ORA-00942|ORA-00957|ORA-01403|not supported|unsupported' "$err_file"; then
        printf 'unsupported'
    else
        printf 'error'
    fi
}
```

同时 `_finalize` 的统计口径要加 `schema_mismatch`（L3545-3565）。

**验收**：造一份 stderr 夹具（只放 `ORA-00904: "FOO": invalid identifier`），断言 `classify_failure` 输出 `schema_mismatch` 而不是 `unsupported`。这类"配置/分类键消费面"的守门测试 Oracle 侧一条都没有，建议随本批补最小的一组。

---

### OF-04 每条 SQL 跑两遍 sqlplus，md 与包可能不一致【P1】

**代码事实**：`ora_sql_to_md` 里对同一条 SQL 开**两次** sqlplus 连接 —— 一次 `>> "${RUNTIME[REPORT_FILE]}"`（L546，写 Markdown），一次 `> "$tsv_file"`（L641，写结构化包），两次之间间隔取决于前面所有 `COLUMN` 与 `SET` 的执行时间，且**没有事务保证**。

**后果**：① 采集耗时与连接数翻倍（136 项 = 272 次 sqlplus 连接）；② md 里显示"当前活跃会话 12 个"、包里却是 9 个 —— 客户对着报告和包两处数字打架；③ 两次都可能失败但只有第二次的 rc 被用来分类（第一次的 rc 在 L610 之后只用于 status，实际 `record_status` 用的是第二次）。

**改**：结构化模式下**只跑一次**，落 `tables/`，Markdown 段由同一份数据渲染（`column -t -s'|'` 或直接内联）；`report` 模式（`--no-structured`）保留原 md 路径。**注意别把 md 砍掉**——报告仍要人可读。

**验收**：跑一遍，断言 `grep -c "sqlplus" <日志>` 中 SQL 执行次数约为原来的一半；随机抽 3 项，md 与 psv 的同一数值一致。

---

### OF-13 无能力探测表【P1 · 加分题但属批 1】

**现状**：Oracle 只有 `ENV[]`（IS_RAC / HAS_ASM / IS_STANDBY / IS_CDB / PDB_COUNT），**没有 per-item 能力表**。`detect_asm` 失败、`dba_advisor` 权限不足、`v$diag_alert_ext` 不可用、`sadf` 是否存在，全部只能从 `status` 反推。MySQL 有 `tables/capabilities.tsv` + `mysql.capability_probe` 状态项。

**改**：新增 `tables/capabilities.tsv`（`capability\tvalue`，布尔统一 0/1），至少覆盖：`sqlplus_version`、`oracle_home`、`diagnostics_pack`、`sadf_command`、`sar_command`、`chronyc_command`、`ntpq_command`、`lsnrctl_command`、`crsctl_command`、`dgmgrl_command`、`rman_command`、`opatch_command`、`has_v_diag_alert_ext`、`has_dba_advisor_findings`、`has_v_asm_diskgroup`、`is_local_target`（见 OF-06）。同时登记一条 `module.capability_probe` 状态。

**验收**：`tables/capabilities.tsv` 存在且非空；`snapshot.json.capabilities` 与它逐键一致（**注意 MySQL F-16 的教训：同一事实两处不同类型会踩坑，json 写 true/false、tsv 写 0/1，并在 INTERFACE 里写明"Python 只读 snapshot.json"**）。

---

### OF-16 / OF-23 包内无 `collection_status.json` / `summary.txt`【P1】

**代码事实**：Oracle 只有 per-item 的 `status/*.tsv`（还被 OF-01 合并），而汇总文件是：

```bash
# L3552 —— 落在 filepath，**不在 TASK_DIR 里**
_status_file="${filepath}/${CONFIG[REPORT_PREFIX]}_status_summary_${_ts_tag}_${qctime}.txt"
```

→ **汇总不进包**。`collect_structured_package`（L3287）只拷 `timeseries/`、`history/`、`status/`。

`docs/cross-database-capability-matrix.md` 已登记："包内未发现同结构 `collection_status.json`，分析器需推断部分状态"。

**改**：照 MySQL L1467-1490 / L1569-1631 补两个文件，**都放进 TASK_DIR**：

```bash
COLLECTION_STATUS_FILE="$TASK_DIR/collection_status.json"   # items[] + summary{}
SUMMARY_FILE="$TASK_DIR/summary.txt"                        # 可读摘要，含"耗时最长前 10"
```

（OF-01 修好后才能得到正确的 items[] —— 顺序不能反。）

---

### OF-20 `ora_value` 不过滤 ORA- 错误 → ASM 探测静默误判【P2】

**代码事实**：

```bash
# L525-527
ora_value() { ora_try "SELECT $1;" | tr -d ' \r\n'; }
# L692-698  detect_asm
asm_cnt=$(ora_value "COUNT(*) FROM v\$asm_diskgroup" 2>/dev/null | tr -d ' \r\n')
if [ "${asm_cnt:-0}" -gt 0 ]; then ENV[HAS_ASM]=1; else progress "未检测到 ASM（或无查询权限）"; fi
```

`ora_try` 带 `WHENEVER SQLERROR CONTINUE`，错误文本会出现在 **stdout**。`tr -d ' \r\n'` 把 `ORA-00942: table or view does not exist` 压成 `ORA-00942:tableorviewdoesnotexist`，然后 `[ "ORA-..." -gt 0 ]` 报 `integer expression expected`（`set +e` 掩盖，但错误打到终端），并按"没有 ASM"走 `else` 分支。

**不一致点**：`detect_pdb`（L729-733）**有** `grep -vE 'ERROR|ORA-|no rows'` 过滤，`detect_asm` / `detect_rac` / `detect_db_basic` **没有**。同一件事三种写法。

**后果**：无 `SYSASM`/`SYSDBA` 权限的现场，报告会说"未检测到 ASM"，而真相是"没权限查"。ASM 模块整块消失（`collect_asm` 走 L1193 的跳过分支），客户会以为盘组不用看。

**改**：在 `ora_value` 统一过滤并区分失败：

```bash
ora_value() {
    local out
    out=$(ora_try "SELECT $1;" 2>/dev/null | tr -d '\r')
    # 剔除 ORA-/SP2-/ERROR 行；命中则视为查询失败（返回空，由调用方决定 status）
    printf '%s' "$out" | grep -vEi '^(ORA-[0-9]+|SP2-[0-9]+|ERROR[:\s])' | tr -d ' \n'
}
```

`VALUE_OK` 标志位由调用方读取，`detect_asm` 失败时记 `permission_denied` / `unsupported`，不再写"未检测到 ASM"。

---

### OF-22 SQL 项的 `row_count` 恒为 `-`【P2】

**代码事实**：L662

```bash
record_status "$item_id" "sql" "$status" "$start_iso" "$end_iso" "$((end_ms-start_ms))" "-" "$rc" "report" "$reason"
#                                                                                ^^^ row_count
```

136 个 SQL 项的 `row_count` 都是字符串 `-`。MySQL 记录真实行数。后果：下游算不出"这项到底查到几行"，也无法用行数交叉验证 OF-02 的解析。

**改**：从 `tsv_file` 数行（扣掉占位表头）；md-only 模式下数 code block 内非空行。

---

### OF-27 `snapshot.json` 字段太薄【P2】

**现状**（L3204-3233）只有平铺字段：`schema_version` / `database_type` / `collector_version` / `instance_tag` / `host` / `port` / `service` / `db_name` / `instance_name` / `db_role` / `open_mode` / `ora_version` / `is_rac` / `rac_node_count` / `has_asm` / `is_standby` / `is_cdb` / `pdb_count` / `pdb_list` / `collection_started_at` / `diagnostics_pack`。

**缺**（MySQL 都有，也是四库 adapter 能归一的前提）：`collector{}`、`host_identity{}`（含 OF-06 的 `database_target_is_local`）、`time_evidence{}`（OF-07）、`capabilities{}`（OF-13）、`sampling{}`（时序文件路径 + 实际点数）、`privacy{}`、`collection_summary{}`、`artifacts{}`。

**改**：按 `mysql-collector-interface.md` §5.1 的段结构补齐，字段语义逐条对齐；`database_type` 保持 `"oracle"`。

---

### 批 1 落地记录（2026-10-09）

**验收结果（`_work/_verify_b1.sh`，假 sqlplus + stub 环境，不连真库）：PASS=34 / FAIL=0**（批 1 当时；同一支脚本在批 4 扩到 T13 后为 **56 / 0**，见「批 4 落地记录」）

| 项             | 落地内容                                                                                                                                                                                                                            | 验证证据                                                                           |
| ------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------ |
| OF-01         | `ora_sql_to_md` 重签名为 `ora_sql_to_md <item_id> <标题> <SQL>`，136 个调用点全部显式传 ASCII 点分 id；`tsv_filename()` 删除                                                                                                                         | T4：5 项 = 5 个唯一文件；无 `sql..` / `module..` 名；文件名全 ASCII                           |
| OF-02         | 新增 `psv_normalize()`；`tables/<item_id>.psv` = 竖线分隔、去填充空白、去空行、去纯横线分隔行                                                                                                                                                            | T6 单元：`NAME\|VALUE@ZYDB\|x`                                                    |
| 实测-1          | `empty` 判据改为「rc=0 且 0 数据行」；空结果写自描述占位 `# oracle-inspection psv-empty item_id=… sep=\| rows=0 status=…`                                                                                                                           | T2：`status=empty`、`row_count=0`、占位行自描述                                         |
| 实测-2          | status 的 `output_file` 列写 `tables/<item_id>.psv`（与 psv 落盘路径**同一表达式**，不再是两套命名规则）                                                                                                                                                 | T1：`output_file 指向 psv = tables/oracle.t.one.psv`                              |
| OF-03         | `classify_failure` 重排 + 新增 `schema_mismatch`（`ORA-00904`/`ORA-01722`/`invalid identifier`/`invalid number`），且**排在 `unsupported` 之前**                                                                                            | T3：00904→schema_mismatch / 00942→unsupported / 01031→permission_denied         |
| OF-04         | 单次 sqlplus 连接：先落 `$tmp`，md 与 psv 由**同一份**输出渲染（136 项 = 272→136 次连接）                                                                                                                                                              | T5：`sqlplus 调用次数=1`                                                            |
| OF-13         | 新增 `collect_capabilities()` → `tables/capabilities.tsv` + `snapshot.json.capabilities`（tsv 布尔 `0/1`、json `true/false`；字符串原样）                                                                                                    | T9：`has_v_asm_diskgroup=false`、`oracle_home=/d/app/oracle`                     |
| OF-16 / OF-23 | 新增 `generate_collection_status()` → 包内 `collection_status.json`（items[] + summary{}）与 `summary.txt`                                                                                                                             | T9：`len(items)=7`、summary 可解析                                                  |
| OF-20         | `ora_value` 命中 `ORA-`/`SP2-` 时写**标志文件** `${RUNTIME[STATUS_DIR]}/.ora_value_flag`（函数跑在子 shell 里，变量传不出来）；新增 `ora_value_ok` 读取                                                                                                     | T7：错误时返回空且 `ora_value_ok=假`；正常时取到 `1` 且为真                                      |
| OF-22         | SQL 项 `row_count` 记真实数据行数（首行是列名行，故 `rows-1`）                                                                                                                                                                                    | T1：`row_count=2`                                                               |
| OF-27         | `snapshot.json` 补 8 段：`collector` / `host_identity` / `capabilities` / `time_evidence` / `sampling` / `privacy` / `collection_summary` / `artifacts`；`database_target_is_local` 由顶层**移入 `host_identity`**（破坏性变更，顶层 v1.4 平铺字段保留） | T9：`schema_version=2.0`、`host_identity.database_target_is_local=True`、顶层该字段已移除 |
| OF-21         | 版本单一来源：`COLLECTOR_VERSION` / `SNAPSHOT_SCHEMA_VERSION` / `PACKAGE_VERSION` 三个全局统一 `2.0`                                                                                                                                         | T9 + 脚本头部                                                                      |
| OF-25         | 新增 `MODULE_PLAN` 数组（`module_id\|中文名\|函数\|run\|bg`，28 项），dry-run 与真实执行**同源**                                                                                                                                                     | dry-run 与实际一致（静态：28 项）                                                         |

**批 1 期间自测抓出的新缺陷（OF-29，已修）**

- **现象**：T1（正常项）与 T2（空结果）状态双双被判成 `error`，而 psv / status 文件内容完全正确。
- **根因**：`ora_sql_to_md` 的 SQL heredoc 是**未加引号**的（必须展开 `$sql` / `${page_size}`），  
  而 bash 会对未加引号的 heredoc 做**参数展开与命令替换**。批 1 为解释 OF-02 写进 heredoc 的注释里带了反引号：  
  `-- ★ OF-02：分隔符选 `|` 而非 TAB`。bash 于是把这对反引号当**命令**执行 → 报  
  `syntax error near unexpected token '|'` → 该错误落进 `err_file` → `classify_failure` 把**正常项**判成 `error`。
- **为什么编译期无感**：`bash -n` 全绿（反引号在语法上完全合法），只在运行期往 stderr 留一行。  
  同一处的 `v$parameter.value` 也被静默吞成 `v.value`（不报错，只是注释被打散）。
- **修法**：注释去掉反引号；SQL / 注释里的 `$` 一律写 `\$`；在 `ora_sql_to_md` 上方写明该 heredoc **故意不加引号 ⇒ 正文禁止反引号与裸 `$`**。
- **守门（两道）**：① `_work/_b1_make_verify.py` 生成前静态扫描真实脚本，未加引号的 heredoc 体内出现反引号即**拒绝生成**；② 自测 T10 用纯 bash 状态机复扫同一约束（必须 = 0 行）。
- **教训（跨库通用）**：只要 stderr 参与状态判定，就必须先保证**stderr 只可能来自被测命令**。  
  本项目的 harness 先污染了 stderr，于是把一个真缺陷伪装成了"harness 假失败"；反过来，  
  harness 没有把 `error` 当噪音忽略，才把这个真缺陷翻了出来。  
  同源教训：**静态注册表（声明了什么）与运行时覆盖（真的渲染/真的判定了吗）是两件事**，见 §22.x 记录。

**同一批做的接线与自洽检查**

- `collect_structured_package()` 顺序：复制 timeseries/history/status → `generate_collection_status` → `generate_snapshot_json` → `generate_manifest` → `security_scan` → tar。
- `run_module` / `start_module_bg` 重签名为带 `module_id`；后台任务统一登记进 `BG_IDS`，`wait_background_modules` 按 `BG_IDS` 收口（不再靠 `jobs -p`）。
- 尾部摘要新增 `schema_mismatch` 计数；`refresh_status_tally()` 成为 summary 的**唯一**统计来源。
- `docs/oracle-collector-item-ids.md`（新增，136 行）是 item_id 的**唯一清单**；生成器 `_work/_b1_apply_ids.py` 按**标题字符串**匹配（不按行号），避免改一处行号全漂移。
- `docs/oracle-collector-interface.md` 同步：§4.1 OS 采集项 30→**31**（+`system.capabilities`）、§4.3 **136 个唯一 item_id**（含语法规则与 v1.4 塌陷对照）、§4.4 模块 27→**28**（附 `module_id` ↔ 中文名 ↔ 函数 ↔ run/bg 全表）。

---

## 批 2：OS 采集（**用户重点关注**）✅ 已完成（2026-10-09，仅动 `inspection/oracle_inspection.sh`）

**验收结果（构造验证，`_work/_verify_b2os.sh`：PASS=20 / FAIL=0）**

| 项           | 落地内容                                                                                                                                                                                                       | 验证证据                                                                            |
| ----------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------- |
| **OF-06** ✅ | 移植 `resolve_ipv4` / `is_local_connect_target`；主流程加 `TARGET_RESOLVED_IP` / `TARGET_IS_LOCAL`；`snapshot.json` 加 `database_target_is_local` / `collector_hostname` / `target_resolved_ip`；非本机时**终端 + 报告头双披露** | 受控 `collect_all_ipv4` 下：本机 IP → LOCAL；**远端 IP → REMOTE（关键）**                    |
| **OF-07** ✅ | 补 `time_evidence{}`（字段名与 MySQL 一致）；**不改 sadf 参数**（见下方决策）                                                                                                                                                   | 真实包 4 字段解析正确；新旧 systemd NTP 字段名都命中                                              |
| **OF-08** ✅ | 移植 `run_with_timeout`；**13 处**外部命令套超时                                                                                                                                                                      | `run_with_timeout 2 sleep 10` → rc=124、耗时 ≈2s                                   |
| **OF-11** ✅ | 新增 `system.oracle_limits`（limits.conf + limits.d，4 列）+ `system.oracle_actual_limits`（ulimit + `/proc/<pmon_pid>/limits`）                                                                                   | 构造 `limits.d/99-oracle.conf` → 采到 `nofile 65536`（旧版只读 limits.conf 采不到）          |
| **OF-12** ✅ | 新增 `system.oracle_processes`（`-eo … etimes,%cpu,%mem,args`），独立成函数以复用 `redact_command_stream`                                                                                                               | stub ps：只留 `ora_`/`asm_`，排除 sshd                                                |
| **OF-09** ✅ | 新增 5 项：`ipcs_shm` / `asm_multipath` / `oratab` / `net_error_counters` + `hugepages.tsv` 扩 `/proc/meminfo` 实测值                                                                                              | 构造 meminfo：6 个白名单键命中，MemTotal 被排除                                               |
| **OF-15** ✅ | `collect_sys_info` 6 段改引用；新增 `oracle_env` / `oracle_env_full` / `listener_status` 结构化                                                                                                                      | `collect_sys_info` 内 `df/lsblk/ip addr/vmstat/iostat/lsnrctl/ps -ef` 残留 = **0** |
| **OF-18** ✅ | `collect_sadf_metric process … -w` + `coverage.json`（7 字段，对齐 MySQL）                                                                                                                                        | 真实包 `coverage.json` 可 `json.load`                                               |

**⚠️ 实施中否掉了计划里的一半（OF-07 的 `-t`）**：计划原写「sadf 加 `-t` 按本地时间输出」。实测发现 `sadf -d` 默认输出**自带 `UTC` 后缀**（真实包：`2026-10-09 14:32:22 UTC`），且 **MySQL 也不带 `-t`**（四库共性，OF-07 标题已修正过）。若 Oracle 单独加 `-t`，sar 会变本地时间，**与 MySQL 的 UTC 形态不一致，破坏四库归一**。故**只补 `time_evidence`**（Oracle 真正缺的那一半），`sadf` 参数保持原样。

> 先把结论说清楚：Oracle 的**结构化 OS 采集**（`collect_system_static`，L895-939）与 MySQL 版基本是同一份代码（`os_release` / `time_status` / `timedatectl` / `chrony|ntpq` / `lscpu` / `free -b` / `df -PT` / `df -Pi` / `lsblk -b` / `mount` / `ip|ifconfig` / `ss -s` / `numactl` / `sysctl_selected` / `hugepages` / `dmesg_errors`，共 **20 项**），这部分**不用对齐，已经对齐了**。  
> 真正的差距是**归属、时间基准、超时、以及 Oracle 专属的那几样**。

### OF-06 OS 数据归属无法证明【P0 · 违反 AGENTS.md 硬规则】

**AGENTS.md 原文**：「OS 指标必须证明属于数据库目标主机；远程采集端主机数据不得参与目标数据库资源评价。」

**代码事实**（实测：以下符号在 `oracle_inspection.sh` 中出现次数**全部为 0**）：

| 符号                         | Oracle | MySQL | 作用                                      |
| -------------------------- | :----: | :---: | --------------------------------------- |
| `resolve_ipv4`             |  **0** |   2   | 把 `--host` 解析成 IP（DNS / 直连 IP）          |
| `is_local_connect_target`  |  **0** |   2   | 判断目标是否本机（主机名 / FQDN / 本机所有 IP 三路比对）     |
| `TARGET_IS_LOCAL`          |  **0** |   4   | 归属标志                                    |
| `database_target_is_local` |  **0** |   1   | 写进 `snapshot.json.host_identity` 的那一个字段 |
| `time_evidence`            |  **0** |   1   | ——                                      |

Oracle 只在生成 tag 时做了一次粗略替换（L3367-3370）：

```bash
case "${DB_CONN[HOST]}" in
    127.*|localhost|::1) _INSTANCE_ADDRESS="$_COLLECTOR_PRIMARY_IP" ;;
    *) _INSTANCE_ADDRESS="${DB_CONN[HOST]}" ;;      # ← 远端 host 不做解析，直接用原字符串
esac
```

**后果**：

1. `--host 10.0.0.9`（远端库）时，`collect_system_static` / `collect_sar_history` / `collect_oracle_realtime_samples` 采的全部是**执行采集的这台机器**的数据，`snapshot.json` 里**没有任何字段**能让 Python 判断这件事 → OS 层结论（内存不足、文件系统将满、IO 打满、大页未配）会被原样写进**目标库**的巡检报告。这正是 AGENTS.md 禁止的。
2. `instance_tag` 里 `_INSTANCE_ADDRESS` 可能是主机名而非 IP，报告文件名与实例台账不一致（MySQL 侧统一是 IP）。
3. MySQL 还会在 `summary.txt` 里打一句显式警告（`连接目标不是本机地址…`），Oracle 一句都没有。

**改**（照 MySQL L145-163 / L1745-1747 / L1602 移植）：

```bash
# 1) 移植 resolve_ipv4 / collect_all_ipv4 / is_local_connect_target（L309-329 已有 collect_all_ipv4，
#    缺 resolve_ipv4 与 is_local_connect_target）
TARGET_RESOLVED_IP=$(resolve_ipv4 "${DB_CONN[HOST]}")
TARGET_IS_LOCAL=0; is_local_connect_target "${DB_CONN[HOST]}" "$TARGET_RESOLVED_IP" && TARGET_IS_LOCAL=1
case "${DB_CONN[HOST]}" in 127.*|localhost|::1) _INSTANCE_ADDRESS="$COLLECTOR_PRIMARY_IP" ;; *) _INSTANCE_ADDRESS="${TARGET_RESOLVED_IP:-${DB_CONN[HOST]}}" ;; esac

# 2) 非本机时：所有 OS 类采集项（system.static / system.history / timeseries.系统三件套）
#    的 status 记 not_applicable（暂用 "skipped" + reason 明确，待 OF-03 补齐取值），
#    或照采但**在 snapshot 与 summary 里显式声明归属**
[ "$TARGET_IS_LOCAL" -eq 1 ] || progress "[警告] 连接目标不是本机；OS 数据属于采集主机 $_COLLECTOR_HOSTNAME，不代表数据库服务器"

# 3) snapshot.json 加 "database_target_is_local": true/false
```

**这条与"要不要采"是两件事**：远端执行时 OS 数据仍值得采（用于判"是不是在 DB 服务器上跑的采集器"），**关键是必须能证明归属**，让下游决定用不用。

**验收**：① `snapshot.json` 里有 `database_target_is_local`；② 用 `--host 127.0.0.1` 与 `--host <本机另一个 IP>` 两次跑，前者 true、后者按 `collect_all_ipv4` 判定也为 true（本机 IP 应判本机）；③ 传一个远端 IP，终端出现警告且标志为 false。

---

### OF-07 缺 `time_evidence`（`sadf -d` 无 `-t` 是四库共性，不是 Oracle 独有）→ 时区错位【P0】

**代码事实**：

```bash
# L2353  collect_sadf_metric —— sadf 默认输出 UTC，没有 -t/-T
sadf -d "$f" -- "$@" >> "$outfile" 2>> "${RUNTIME[LOG_FILE]}"
```

`collect_system_static` **采了** `evidence/time_status.txt`（local_time / utc_time / timezone / epoch_seconds，L900），但 `generate_snapshot_json` **一个字段都不解析**（MySQL 的 `generate_snapshot_json` L1528-1534 会把它解析成 `time_evidence{host_local_time, host_utc_time, timezone, ntp_synchronized}`）。

**后果**：`history/sar_cpu.csv` 的时间戳是 **UTC**，而 `timeseries/system_cpu.csv` 是**本地时间**，两者画在同一张 x 轴上会**差 8 小时**（东八区）。覆盖率（求差值）不受影响，**只有出图会错**。MySQL 靠 `time_evidence.timezone` 让 Python 自行补偏移（见 AGENTS.md："`time_evidence` 缺失时按时间戳自带偏移定展示时区"）。Oracle **只缺 `time_evidence` 这一条**：`sadf -d` 的 UTC 时间戳**自带 `UTC` 后缀**（真实包实测），故"按时间戳自带偏移"这条退路本身是通的；但**没有显式声明**，下游无从取"采集机时区"。

**改**（**只做 b**；原 a 项已在批 2 实施中否掉，见下方「为什么不动 `-t`」）：

```bash
# b) snapshot.json 增加 time_evidence，字段名与 MySQL 完全一致（四库归一）
"time_evidence": {"host_local_time":..., "host_utc_time":..., "timezone":..., "ntp_synchronized":...,
                  "timedatectl_file":"evidence/timedatectl.txt", "chrony_tracking_file":"evidence/chronyc_tracking.txt"}
# ntp_synchronized 的字段名随 systemd 版本变：旧版 "System clock synchronized"、新版 "NTP synchronized"，
# 两种都要匹配并锚定行首（MySQL F-31 踩过）
```

**为什么不动 `-t`（批 2 实施中的决策，推翻本条目原 a 项）**：

1. `sadf -d` 的时间戳**自带 `UTC` 后缀**（真实包：`2026-10-09 14:32:22 UTC`），加 `-t` 不是"补上缺失的时区信息"，只是**换一种表示**；
2. **MySQL 也不带 `-t`** —— 四库共用同一形态；Oracle 单独加 `-t` 会让 sar 变"本地时间"，**破坏四库归一**，Python adapter 反要分库处理；
3. Oracle 真正缺的是 `time_evidence`（显式声明），不是 sadf 参数。

**验收**：`snapshot.json.time_evidence.timezone` 非空；`ntp_synchronized` 在旧/新 systemd 字段名下都能取到值。（✅ 批 2 已按此落地，构造实测见批 2 章节）

---

### OF-08 OS 采集命令无超时包装【P1】

**代码事实**：`run_with_timeout` 在 Oracle 脚本中**不存在**（实测 0 次引用），MySQL 有 3 处引用。Oracle 只有告警日志扫描用了裸 `timeout 30s`（L1858/1875/1889）。裸跑的 OS 命令：

| 位置         | 命令                                | 风险                                              |
| ---------- | --------------------------------- | ----------------------------------------------- |
| L877-878   | `lsnrctl status`                  | 监听器 hung / 防火墙 DROP 时**长时间阻塞**（TCP 重传），这是最危险的一条 |
| L858       | `iostat -x -k 3 2`                | 固定 6 秒                                          |
| L857       | `vmstat 2 3`                      | 固定 4 秒                                          |
| L937       | `dmesg --level=...`               | ring buffer 大时很慢                                |
| L923       | `numactl --hardware`              | 大机器上枚举慢                                         |
| L903-906   | `chronyc tracking/sources`        | daemon 无响应时阻塞                                   |
| L2004-2024 | `crsctl stat res -t` / `ocrcheck` | RAC 上可慢可 hang                                   |

而 `run_module` 是**同步调用**（L772 `if ! ${func_name} ...`），一条 hung 命令 = 整包卡死，只能靠 `trap _trap_cleanup` 人工 Ctrl-C。

**改**：移植 MySQL L207-210：

```bash
run_with_timeout() {
    local seconds="$1"; shift
    if has_cmd timeout; then timeout --signal=TERM --kill-after=5 "$seconds" "$@"; else "$@"; fi
}
# 逐个 OS 项包上：lsnrctl 10s / dmesg 20s / iostat、vmstat 30s / crsctl、ocrcheck 15s
```

**验收**：故意把 `lsnrctl` 指向一个不存在的监听（或用 `nc -l` 占端口模拟 hang），断言 `collect_sys_info` 在 10 秒内返回并记 `timeout`。

---

### OF-11 资源限制只读一个文件，口径不完整【P1】

**代码事实**（L862-865，且只在 Markdown 里）：

```bash
md_line "### 系统 limits 配置"
[ -f /etc/security/limits.conf ] && cat /etc/security/limits.conf >> "${RUNTIME[REPORT_FILE]}" 2>&1 || true
```

**问题有三层**：

1. **只读 `/etc/security/limits.conf`，不读 `/etc/security/limits.d/*.conf`**。RHEL/OEL 上 Oracle 的 limits 绝大多数写在 `/etc/security/limits.d/`（安装文档与 `preinstall` 包都是往 `limits.d` 里丢文件）。这与 MySQL 侧已修的 `F-02`（`!includedir` 不展开 → `/etc/my.cnf` 只有一行 → 白名单基本为空 → 还写 `ok`）是**同一类陷阱**：只读一个文件会得出"没配 nofile"的假结论。实测 `limits.d` 在 Oracle 脚本里出现 **0 次**。
2. **文件写的是"期望值"，进程拿到的是"实际值"**。`limits.conf` 里 `oracle soft nofile 65536` 若没被 PAM 应用到会话，Oracle 拿到的仍是 1024；反之 `oracle` 用户起库时若被 ulimit 覆盖，文件也看不出来。Oracle 自己的告警日志会警告 `max processes ... is less than ...`，但采集器应该主动留证。
3. **内容只进 Markdown，不进包**，Python 用不了（见 OF-15）。

**改**：新增两项结构化采集（`limits.conf` 内容 + 实际生效值）：

```bash
# item: system.oracle_limits      -> tables/oracle_limits.tsv (4 列: source_file / scope / limit / value)
#   展开 /etc/security/limits.conf 与 /etc/security/limits.d/*.cnf —— 注意 limits.d 里是 *.conf，
#   按文件名字典序合并（与 PAM 的读取顺序一致），并记下"最后生效的那条"
# item: system.oracle_actual_limits -> evidence/oracle_actual_limits.txt
#   ① 当前会话：ulimit -a（或 prlimit --pid $$）
#   ② 真正重要的：oracle 进程的实际 limits
#      pmon_pid=$(pgrep -u oracle -f 'ora_pmon_' | head -1)
#      [ -n "$pmon_pid" ] && cat /proc/$pmon_pid/limits
#   ③ 若存在 ulimit 差异，在 summary 里显式点名（这是 Oracle 启动告警的根因）
```

**验收**：在只有 `limits.d/99-oracle.conf` 的机器上跑，断言 `oracle_limits.tsv` 能采到 `nofile 65536`（旧版采不到）；断言 `oracle_actual_limits.txt` 含 `Max open files` 一行。

---

### OF-12 缺结构化 Oracle 进程快照【P1】

**代码事实**（L868-872，只写 Markdown）：

```bash
has_cmd ps && ps -ef | grep -iE "(ora_|asm_|oracle)" | grep -v grep | grep -v "sqlplus" | redact_command_stream >> "${RUNTIME[REPORT_FILE]}"
```

对照 MySQL（L610-613）：

```bash
ps -eo pid,ppid,user,etimes,%cpu,%mem,args | awk 'BEGIN{IGNORECASE=1} /[m]ysqld/ {print}' | redact_command_stream > "$EVIDENCE_DIR/mysql_processes.txt"
record_status "system.mysql_processes" ...
```

**差距**：① Oracle 用 `-ef`，**没有 `%cpu` / `%mem` / `etimes`**，判不了"哪个进程在吃 CPU""PMON 已运行多久"；② **不进包**，只在 Markdown；③ 没有对应的 status 项，下游看不出这项采没采到。

**改**：

```bash
capture_command "system.oracle_processes" "system.static" "$EVIDENCE_DIR/oracle_processes.txt" \
    bash -c 'ps -eo pid,ppid,user,etimes,%cpu,%mem,args 2>/dev/null | awk '\''BEGIN{IGNORECASE=1} /[o]ra_|[a]sm_|[t]nslsnr/ {print}'\'' | redact_command_stream'
```

**为什么值得**：Oracle 现场最常见的两个 OS 层问题都靠这个判 —— ① **同一台机器上残留了另一个 `ORACLE_HOME` 的 PMON**（升级/双实例并存）；② **ASM 实例是否真的起来**（`asm_pmon_+ASM` 在不在）。报告里"实例信息"只有 DB 侧 `gv$instance`，补上 OS 侧进程才是交叉验证。

**验收**：包内 `evidence/oracle_processes.txt` 非空；含 `ora_pmon_<SID>` 一行。

---

### OF-09 缺 Oracle 专属 OS 视图【P1 · 加分题】

这几项**不是"对齐 MySQL"**（MySQL 侧也没有，因为不适用），是 Oracle 巡检本来就该有的。按"上线风险 × 现在完全没证据"排序：

| 项                   | 采集命令                                                                                                                                            | 为什么 Oracle 必须有                                                                                                                                                                                                         |
| ------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **SysV 共享内存 / 信号量** | `ipcs -lm`、`ipcs -ls`、`ipcs -lu`、`ipcmk`（不执行）                                                                                                   | 关掉 AMM（`memory_target=0`）时 SGA 走 SysV SHM；`kernel.shmall` / `shmmax` 不足 = **实例起不来**，而 `sysctl_selected` 采了 `shmmax/shmall` 却没有 `ipcs -lm` 的"已用/上限"对照，判不出余量。信号量同理（`processes` 调大后 `kernel.sem` 不够是经典故障）                 |
| **大页实测值**           | `/proc/meminfo` 的 `HugePages_Total` / `HugePages_Free` / `Hugepagesize` / `AnonHugePages`                                                       | 现在只采了 `/proc/sys/vm/nr_hugepages`（配置值）与 THP 开关（`hugepages.tsv`）。**没有 `HugePages_Free` 就算不出"分配了但没被用掉的大页占用多少内存"** —— 这是 Oracle 大页配置最典型的坑（HugePages 配了、实例没起来，内存被白占）。实测 `HugePages_Total` / `AnonHugePages` 在脚本中出现 **0 次** |
| **ASM / 共享存储**      | `multipath -ll`、`oracleasm listdisks`（或 `ls -l /dev/oracleasm/disks`）、`/etc/udev/rules.d/*asm*.rules`、`lsmod \| grep -E 'oracleasm\|oracleaci'` | ASM 盘掉了、多路径失效、ASMLib 内核模块没加载 —— 这三样在 DB 侧只看得到"盘组 OFFLINE"，看不到 **OS 侧原因**。实测 `multipath` / `oracleasm` / `lsmod` 全部 0 次                                                                                                 |
| **实例清单**            | `/etc/oratab`                                                                                                                                   | OS 层登记的 SID/ORACLE_HOME，用于和 `gv$instance` 交叉验证"是不是有实例没登记 / 登记了但没起"。实测 `oratab` 0 次                                                                                                                                     |
| **网卡错误计数**          | `ip -s link`（或 `sar -n EDEV`）                                                                                                                   | 现在只采 `ss -s` 与 `ip -details addr`。RAC 私网丢包/错包是 `gc buffer busy` 的上游原因，`ip -s link` 一次就能看到 drops/errors                                                                                                                 |

**改**：全部走 `capture_command` + 独立 item_id（`system.ipcs_shm` / `system.hugepages_meminfo`（并入现有 `hugepages.tsv` 更省事）/ `system.asm_multipath` / `system.oratab` / `system.net_error_counters`），并按 OF-08 加超时。**注意脱敏**：`multipath -ll` 会输出 WWID（设备全球唯一标识），`oratab` 含路径 —— 这两样按项目规则**保留**（是诊断所需，不是凭证），但 `udev rules` 里若含 `OWNER=`/`GROUP=` 一律保留、含密码字段则删。

**验收**：每项在包内有对应文件且 status 为 `ok`；在无 ASM 的机器上对应项应为 `unsupported` 或 `empty`（**不能是 `ok` + 空内容**）。

---

### OF-15 OS 采集两套并存，Markdown 那套不进包【P2】

**代码事实**：同一个 OS 事实被采两遍，口径还不一样：

| 事实        | `collect_sys_info`（md，L821-892）                           | `collect_system_static`（结构化，L895-939）                                        |
| --------- | --------------------------------------------------------- | ---------------------------------------------------------------------------- |
| 磁盘        | `df -HT`（**1000 进制**，且不带 `-P`）                            | `df -PT`（POSIX、1024 进制）+ `df -Pi` 还有 inode                                   |
| 块设备       | `lsblk -d -o name,rota`（只 2 列，且 `-d` 跳过分区）                | `lsblk -b -o NAME,KNAME,TYPE,SIZE,FSTYPE,MOUNTPOINT,ROTA,SCHED,MODEL,SERIAL` |
| 网络        | `ip addr show`                                            | `ip -details addr show` + `ip route show table all` + `ss -s`                |
| 中断/CPU    | `vmstat 2 3` + `iostat -x -k 3 2`（**阻塞 10 秒**）            | 无（靠实时时序采样）                                                                   |
| limits    | `cat /etc/security/limits.conf`                           | 无（OF-11 要补）                                                                  |
| 监听器       | `lsnrctl status`（**无超时**）                                 | 无（OF-09 之外建议补 `system.listener_status`）                                      |
| 环境变量      | `ORACLE_HOME` / `ORACLE_SID` / `LD_LIBRARY_PATH` / `PATH` | 无                                                                            |
| Oracle 进程 | `ps -ef \| grep ora_`                                     | 无（OF-12）                                                                     |

**后果**：报告里 `df -HT` 的"已用%"与包里 `df -PT` 的数值不同（进制差），客户对不上；`iostat -x -k 3 2` 让 md 模块白等 6 秒；最关键的是 **limits / lsnrctl / ORACLE_HOME 这三样只在 md，Python 永远拿不到**。

**改**（收敛原则：**md 负责展示、包负责数据**，同一事实只有一个采集源）：

1. 删掉 `collect_sys_info` 里与结构化重复的裸命令（`df -HT`、`lsblk`、`ip addr show`），md 段改为**引用**结构化结果（或保留标题+一句"详见 evidence/…"）。
2. 保留 md 独有而结构化缺的内容，并**为它们补结构化采集项**：
   - `system.oracle_env` → `tables/oracle_env.tsv`（`ORACLE_HOME` / `ORACLE_BASE` / `ORACLE_SID` / `GRID_HOME` / `TNS_ADMIN`）；`PATH` / `LD_LIBRARY_PATH` 只在 evidence 里落原值并在 INTERFACE 注明"可能含客户路径"。
   - `system.listener_status` → `evidence/listener_status.txt`（`lsnrctl status`，**必须带 OF-08 的超时**）。
3. `vmstat` / `iostat` 这类"瞬时观察"归实时时序采样（已有 CPU/mem/net/disk 四表），不要再用裸命令占 10 秒。

**验收**：`grep -cE '^(df|lsblk|ip addr)' collect_sys_info 函数体` 归零；`evidence/` 与 `tables/` 里能覆盖原 md 段的所有事实。

---

### OF-18 sar 历史缺 `-w` 与 `coverage.json`【P2】

**代码事实**：

```bash
# L2401-2407  Oracle：7 类
collect_sadf_metric cpu ... -u ALL ; memory ... -r ; swap ... -S ; load ... -q ; disk ... -d -p ; network ... -n DEV ; io ... -b
# MySQL L1020-1027：8 类，多一条
collect_sadf_metric process "$HISTORY_DIR/sar_process.csv" -w
```

另外 `compute_sar_coverage`（L2360-2383）只写 `coverage.tsv`，**不写 `coverage.json`**（MySQL L1003-1006 两份都写，snapshot 里还带 `coverage_file` 路径）。

**改**：补 `-w`（进程创建与上下文切换，Oracle 上用于对照 `processes` / `sessions` 与 OS 调度压力），补 `coverage.json`（字段与 MySQL 完全一致：`schema_version/status/requested_hours/first_timestamp/last_timestamp/coverage_seconds/coverage_hours`）。

**验收**：`history/sar_process.csv` 存在；`history/coverage.json` 可 `json.load`。

---

## 批 3：安全与门禁 ✅ 已完成（2026-10-09，仅动 `inspection/oracle_inspection.sh`）

### OF-05 敏感扫描拦截后仍记 `ok`【P1】

**代码事实**（L3287-3313）：

```bash
local status="ok"; local reason="structured package completed"
if [ -n "${PACKAGE_FILE:-}" ] && [ -f "$PACKAGE_FILE" ]; then
    reason="package created: $(basename "$PACKAGE_FILE")"
fi
record_status "module.structured_packaging" "module" "$status" ... "$reason"
```

`status` 恒为 `"ok"`，`security_scan` 返回 1 时**只改了 reason**（`collect_structured_package` 里 L3301 的分支只 `log_error`）。且脚本末尾没有按扫描结果设置退出码。对照 MySQL 的 `F-05`（同样是"拦截后仍 exit 0"，已修）。

**后果**：流水线拿到的是"采集成功、结构化打包 ok"，实际包没生成（或被拦下）。`package.security_scan` 那条 status 是 `error`，但 `module.structured_packaging` 是 `ok`，两个互相矛盾的记录同时存在，下游按哪个走全凭运气。

**改**：

```bash
if security_scan; then
    create_package && status="ok" || { status="error"; reason="tar failed; task dir available"; rc=31; }
else
    status="error"; reason="sensitive data detected; package blocked"; rc=30
fi
record_status "module.structured_packaging" "module" "$status" ... "$rc" ... "$reason"
```

**验收**：在包里放一个含 `password=abc123` 的假文件，断言 `package.security_scan` = `error`、`module.structured_packaging` = `error`、脚本退出码 = 30。

---

### OF-10 密码走 sqlplus 命令行参数【P1】

**代码事实**（L459-492）：

```bash
echo "${user}/${pass}@//${host}:${port}/${svc} as sysdba"     # 作为 sqlplus 的 argv
# 调用处（14 处）： "${DB_CONN[SQLPLUS_BIN]}" -S "$(get_ora_conn)" ...
```

**后果**：采集期间（几十秒到几分钟）**同机任何用户 `ps -ef` 都能看到明文密码**。MySQL 专门用 `--defaults-extra-file` + `chmod 600` + 临时目录放在任务目录之外来规避（MySQL F-11）。Oracle 脚本里 `redact_command_stream` 只作用于 `ps` 输出那一行，**对 sqlplus 自身的 argv 没有任何保护**。

**改**（凭证走 stdin，不进 argv）：

```bash
# sqlplus -S /nolog 启动，连接串从 here-doc 里的 CONNECT 语句进 stdin
"${DB_CONN[SQLPLUS_BIN]}" -S /nolog <<EOF
CONNECT ${user}/${pass}@//${host}:${port}/${svc} as sysdba
SET ...
$sql
EXIT;
EOF
# 需要 14 处都改；若嫌改动面大，至少引入一个 ora_exec() 统一入口（顺便解决 OF-04 的双跑问题）
```

**验收**：采集进行中，在另一个会话 `ps -ef | grep sqlplus`，断言**看不到**密码（只看到 `sqlplus -S /nolog`）。

---

### OF-17 无退出码契约【P1】

**代码事实**：脚本没有终态 exit code（末条命令是 `printf >> "${RUNTIME[REPORT_FILE]}"`，恒返回 0）；`show_usage`（L91-134）**没有退出码表**。MySQL 定义了 0/10/11/20/30/31/130 并在 `--help` 与文件头写明。

**后果**：自动化无法区分"全成功""连接失败""被安全扫描拦下""打包失败""被中断"，只能去数文件。

**改**：定义并落 `--help` + INTERFACE §5.4：

|  码  | 含义                                       |
| :-: | ---------------------------------------- |
|  0  | 全部成功（包已生成，或 `--no-package`）              |
|  10 | 参数 / 环境 / 认证准备失败（缺 sqlplus、输出目录不可写、空间不足） |
|  20 | Oracle 连接失败                              |
|  30 | 敏感信息扫描拦截，已阻止打包                           |
|  31 | 打包失败，任务目录可用                              |
| 130 | 收到 INT/TERM/HUP 中断                       |

**验收（2026-10-09，本机 Git Bash）**：

| 项     | 证明                                                                            | 证据                        |
| ----- | ----------------------------------------------------------------------------- | ------------------------- |
| OF-10 | 抽真实函数 + 假 sqlplus：`argv = "-S /nolog"`（不含口令），`CONNECT …` 只经 stdin 送入          | `_work/_verify_b3.sh`     |
| OF-05 | stub 控三分支：`ok/0`、`error/30`、`error/31`；`module.structured_packaging` 不再恒 `ok` | `_work/_verify_b3_pkg.sh` |
| OF-17 | 隔离 PATH → `EXIT=10`；连接失败 → `EXIT=20`；打包码经 `FINAL_EXIT_CODE` 传出 → 30/31        | 同上一行 + 本机隔离 PATH 实跑       |
| 回归    | `bash -n` 通过；全脚本 `-S "$(get_ora_conn)"` 残留 **0 处**；`exit` 语句全部在契约表内           | grep                      |

---

## 批 4：一致性与体验（P2）✅ 已完成（2026-10-10，仅动 `inspection/oracle_inspection.sh`）

### OF-14 缺 `--output-dir`

**现状**：输出目录 `CONFIG[REPORT_DIR]="/var/tmp"` 只能靠 `--config=<file>` 改（实测 `--output-dir` / `OUTPUT_PARENT` 引用数均为 0）。现场常用 `/tmp/insp_$(hostname)` 或挂载盘。**改**：加 `--output-dir DIR`（同时把 `show_usage` 第 130-132 行的输出说明从写死 `$CONFIG[REPORT_DIR]` 改成如实描述）。**注意**：不要引入 `$(dirname "$0")` 自引用（AGENTS.md：采集脚本不得依赖自身所在目录）。

### OF-19 中断无 `COLLECTION_INCOMPLETE` 标记

**现状**（L427-434）：`_trap_cleanup` 只往 Markdown 追加一行 `> ⚠️ 采集被中断，报告不完整`。MySQL 还会写 `$TASK_DIR/COLLECTION_INCOMPLETE`（值 `partial`），下游可**程序化**判断包残缺。**改**：加标记 + `[ -n "${TASK_DIR:-}" ] && [ -d "$TASK_DIR" ]` 守卫（`trap` 在 L434 安装，而 `TASK_DIR` 到 L3463 才赋值，直接引用会空）。

### OF-21 版本号三处不一致

| 位置                    | 值                                                      |
| --------------------- | ------------------------------------------------------ |
| 文件头 L3                | `v1.5 (2026-08-10)`                                    |
| `snapshot.json` L3211 | `"collector_version":"1.4"`                            |
| `manifest.json` L3240 | `"collector_version":"1.4"`                            |
| `schema_version`      | 硬编码 `"1.0"` 两处，无 `SNAPSHOT_SCHEMA_VERSION` 变量（MySQL 有） |

**改**：抽全局变量 `COLLECTOR_VERSION` / `SNAPSHOT_SCHEMA_VERSION` / `PACKAGE_VERSION`，文件头与两个 json 全部引用它。

### OF-25 dry-run 预览漏项

dry-run 列表（L3402-3427）列了 26 个模块，**漏了 `collect_system_static`**（主流程 L3507 有）；且顺序与主流程不一致。**改**：把模块清单抽成一个数组，dry-run 与真实执行**共用同一份**，从根上消除漂移。

### OF-26 `capture_command` 只保留 5 行截断、且不落模块日志（口径修正见 §0.5.1）

**代码事实**（L393-412）：分类后 `rm -f "$err"`，只把 `tail -n 5` 截断进 `reason`。MySQL 会 `cat "$err" >> "$MODULE_LOG_DIR/<category>.log"` 后保留 5 行摘要（L358）。**后果**：`ORA-` 原文、权限报错的完整栈只剩 5 行，排查时拿不到。**改**：加 `logs/modules/<category>.log`（Oracle 目前**连 `logs/` 目录都没有**，日志单文件在 `filepath` 下、不进包）。

**顺带一条**：`append_oracle_sysstat_sample`（L2484-2516）在 `|| return 1` 时会**跳过 `rm -f "$tmp"`**，临时文件留在 `${RUNTIME[STATUS_DIR]}` 里，随后被拷进 `$TASK_DIR/status/` 并登记进 `manifest.json`（包里多出 N 个 `oracle_stat_<elapsed_ms>.tsv`）。改用独立 tmp 目录（如 `$TASK_DIR/.tmp`）并在 manifest 里排除。

### 批 4 落地记录（2026-10-10，仅动 `inspection/oracle_inspection.sh`）

| 项                    | 动手点                                                                        | 实际做法                                                                                                                                                                                                                                                                                                                               |
| -------------------- | -------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **OF-14**            | `parse_args` / `show_usage` / `ensure_output_dir`（新）                       | 新增 `--output-dir=DIR` 与 `--output-dir DIR` **两种写法**（空格写法要防"选项在末尾无值"，不能无条件 `shift`）；抽 `ensure_output_dir <dir>` 做可写性检查，主流程 `ensure_output_dir "$filepath" \|\| exit 10`。`show_usage` 的"输出文件"段改为**如实描述**（默认 `/var/tmp` + `--output-dir`），并新增"中断语义"段。**没有**引入 `$(dirname "$0")` 自引用。                                                  |
| **OF-19**            | `_trap_cleanup` + 三个 trap                                                  | 从 `trap _trap_cleanup INT TERM HUP`（**拿不到是哪个信号**）改为三条 `trap '_trap_cleanup INT/TERM/HUP'`；函数在 `TASK_DIR` 存在时写 `$TASK_DIR/COLLECTION_INCOMPLETE`（首行 `partial`，第二行 `interrupted_at=… signal=…`）；守卫用 `${TASK_DIR:-}` + `[ -d ]`（trap 安装点早于 TASK_DIR 赋值）。                                                                              |
| **OF-26**            | `module_log_append`（新）/ `capture_command` / `ora_sql_to_md` / TASK_DIR 初始化 | 新增 `logs/` + `logs/modules/`；`module_log_append <category> <item_id> <rc> <src>` 把**完整原文**追加进 `logs/modules/<category>.log`（带 `item/rc` 表头）。`capture_command` 在 `rm -f "$err"` **之前**调用（仅 `status != ok` 时）；`ora_sql_to_md` 的 `err_file` 除进运行日志外也进 `sql.log`。status 的 `reason` 仍只留 5 行摘要 —— **下游要一句话，排查要全文，两者都要**。                 |
| **OF-26 顺带（tmp 泄漏）** | `RUNTIME[TMP_DIR]`（新）/ `append_oracle_sysstat_sample`                      | ⚠️ **实际做法与原计划的 `$TASK_DIR/.tmp` 不同**：把临时工作目录放到 `TASK_DIR` / `STATUS_DIR` **之外**（`DIR/.oracle_inspection_tmp_<tag>_<qctime>`）。理由：`cp -r STATUS_DIR` 与 `find TASK_DIR` 都发生在收口阶段，**只要 tmp 不在 TASK_DIR 里就"漏不进包"，不需要再维护一份 manifest 排除规则**。函数改用 `if ! ora_exec … then … rm -f; return 1; fi`，**两条路径都删**。目录在正常结束（尾部）与中断（trap）时都 `rm -rf`。 |
| **OF-30（本批新发现）**     | `security_scan`                                                            | `findings` 原先建在 `RUNTIME[STATUS_DIR]`，但**拷贝到 `TASK_DIR/status/` 发生在此之前** ⇒ `record_status` 写的 `status/security_scan_findings.txt` **在包里永远不存在**（status 指向一个不存在的证据文件）。改为：findings 先落 `TMP_DIR`（**不**在建包树内，避免扫描结果自己进包），**命中时才 `cp` 进 `TASK_DIR/status/`** —— 那条路径从此是真的。                                                               |

**构造实测（可复跑）**：`python _work/_b1_make_verify.py` → `bash _work/_verify_b1.sh` → **PASS=56 / FAIL=0**（约 1 分钟，不连真库）。批 1 的 T1–T10 之外新增：

| 用例     | 覆盖                                                                                                                                                                          |
| ------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| T10（扩） | 静态守门**两道**：① 未加引号 heredoc 体内**反引号**行数；② 未加引号 heredoc 体&#x5185;**"词内美元"**（`v$xxx` / `x$bh`）行数                                                                                |
| T11    | `--output-dir` 两种写法生效 / 末尾无值不炸 / 可建目录返回 0 / **不可写返回 10** / usage 提到该选项                                                                                                      |
| T12    | `_trap_cleanup TERM` 在子 shell 里退 **130**、落 `COLLECTION_INCOMPLETE`（首行 `partial`、含 `signal=TERM`）、清 `TMP_DIR`；`TASK_DIR` 未设时**不炸且仍退 130**                                    |
| T13    | `capture_command` 失败 → `logs/modules/chrony.log` 有**完整原文**与 `item/rc` 表头（status 分类仍是 `not_enabled`）；`append_oracle_sysstat_sample` 的 tmp **不留** `STATUS_DIR`、`TMP_DIR` 用完即空 |

> ★★ **本批把批 1 的教训制度化：静态守门从 1 道加到 2 道。** 批 1 抓到"未加引号 heredoc 体内的反引号"，本批补第二道 —— **"词内美元"**。判据只有一条：`$` **紧跟在字母/数字/下划线之后且后面不是 `{`**（`v$parameter` / `x$bh`），因为合法的 shell 展开（`$dbid`、`$sql`、`${begin_snap}`）前面一定是空白或标点。**这道守门第一次跑就抓到真问题**：AWR heredoc 里的 `$dbid, $inst_num, $begin_snap, $end_snap` 被初版过窄的允许清单误报 → 说明"允许清单"方向不如"词内判据"稳（前者要不断补名单，后者只认结构）。两道守门**都在生成自测脚本之前**跑，命中即**拒绝生成**。  
> ⚠️ **T12/T13 三个 harness 假失败也记一下**，都是"测试写错不是代码错"：① 用**不存在的父目录**测"不可写" —— `mkdir -p` 会一路建出来，返回 0（改用**普通文件**当父目录，必然 `ENOTDIR`）；② `grep -c -- '--output-dir'` 期望 1，实际 9（该选项在 usage 里出现多次 —— 断言应写"≥1"）；③ `append_oracle_sysstat_sample` 用例里 `ORACLE_CSV` 的**父目录没建**，重定向失败导致行数为空（不是函数没写）。

---

## 批 5：2.0 实包体检发现的 P1（N-1～N-5 + md 报告归属）✅ 已完成（2026-10-10，仅动 `inspection/oracle_inspection.sh`）

**触发**：用户提交一份**新采的真实 2.0 包**（`oracle_inspection_task_ZYDB_db01_192.168.100.80_1521_zydb1_2026-10-10_09-40-37.tar.gz`，61 KB，156 项），要求体检"采集脚本是否需要优化"。  
**结论**：批 1–4 的改造**全部生效**（156 个唯一 `item_id` 零重名、34 项 `empty` 真实触发、156 条 `output_file` 无一指向不存在的文件、`manifest.json` 305 个文件 sha256 **全对**、`logs/modules/*.log` 有完整原文、snapshot 8 段齐全），同时暴露 **5 个 P1**。  
体检报告：`_work/_pkg2_check_report.md`；校验脚本：`_work/_chk2.py`（manifest 完整性 + hash）、`_work/_chk3.py`（`tables/*.psv` 形态分类）。

|       编号      | 位置                                      | 实测现象（真实包）                                                                                                                                                                                                                                       | 修复                                                                                                                                                                                                                                                         |
| :-----------: | --------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
|    **N-1**    | `ora_sql_to_md` 错误判据                    | 判据**未锚行首**：`oracle.topsql.recent_errors` 查的就是告警日志，**行首是时间戳、行尾才是 `… 256125 ORA-27125`** ⇒ 正常项被判 `error`、`sql_fail_count` 虚高 1、`logs/modules/sql.log` 被无关文本污染。**这是批 1 引入的回归**（v1.4 只读 stderr，不会误判）                                                | 判据改 `^[[:space:]]*(ORA-[0-9]{5}\|SP2-[0-9]{4})` —— 与 `ora_value` 取值的判据**同一口径**                                                                                                                                                                             |
|    **N-2**    | `ora_sql_to_md` psv 落盘                  | 失败 SQL 的 `tables/<id>.psv` 落的是 **sqlplus 报错回显**（`SELECT session_key, …` / `*` / `ERROR at line 2:` / `ORA-00904: …`），`row_count` 记成 **3** —— 既非数据（首行不是列名）也非占位。实测仅 `oracle.validation.rman_validate` 一例                                          | **先定状态、再决定 psv 形态**：只有 `ok` 才落数据；`empty` 落 `psv-empty`；**其余落新占位 `psv-unavailable`**，且 `row_count=0`（不拿报错文本算行数）                                                                                                                                             |
|    **N-3**    | `collect_structured_package` 执行顺序       | 包内 `status/` 与 `collection_status.json.items[]` **都不含** `package.security_scan`（它写在 `cp -r STATUS_DIR` **之后**）；而顶部注释写着这两条"随包回传"，**与事实相反**                                                                                                       | ① `security_scan` **提前到台账生成之前**，并新增 `sync_status_to_package <item_id>` 把它补拷进包（`status/` + `items[]` + `manifest` 三处同时到位）；② 新增 **`packaging_status.json`**（归档前自检结论，进包）；③ 注释改写为**不变量**：`module.structured_packaging` 由归档动作本身产生，**晚于包的封口时间**，结构上不可能进入它自己封装的归档 |
|    **N-4**    | `oracle.validation.rman_validate` 的 SQL | `V$RMAN_STATUS` **没有 `SESSION_KEY` 列**（该列属于 `V$RMAN_BACKUP_JOB_DETAILS` / `V$RMAN_OUTPUT`；`V$RMAN_STATUS` 里表示同一次 RMAN 会话的一对列是 `SESSION_RECID` + `SESSION_STAMP`）⇒ **ORA-00904，该项恒失败**（第一次采集就被 OF-03 的 `schema_mismatch` 抓到 —— 该状态上线第一炮即命中真 bug） | 改用 `session_recid, session_stamp`                                                                                                                                                                                                                          |
|    **N-5**    | 尾部汇总 / `summary.txt`                    | `summary.txt` **漏 `partial` 行** ⇒ 10 档相加 **155 ≠ 156**；控制台那行又把 `schema_mismatch` 重复计入"错误"、把 `not_enabled` 重复计入"跳过/未启用" ⇒ **「包 156 / 控制台 159」没人说得清**。根因：**控制台与包内各算一遍台账**                                                                         | 尾部改为**复用 `refresh_status_tally()`**（与 `snapshot.collection_summary`、`collection_status.json.summary` **三处同源**）；`summary.txt` 补 `partial` 行 + 新增**分档自校验行**（相加 ≠ 总数时当场标 `⚠ 不一致`）；控制台把"包内比总数少 1 项"的原因**直接印出来**                                                |
| **B（原 N-12）** | `snapshot.artifacts.report_md`          | 人读 md 报告落在 **DIR（包外）**，包内没有它；字段却指向包根 ⇒ 下游按包根解析必然 miss（与 OF-30 同类：字段指向不存在的文件）                                                                                                                                                                    | **方案 B（用户拍板）**：字段改名 **`report_md_external`** + 值给**采集机上的绝对路径** + 新增 **`report_md_scope:"outside-package"`** 自证；`artifacts` 另登记 `packaging_status`。报告本身由 Python 侧从包内数据重新生成（工具链：采集 → 分析 → DOCX）                                                              |

### 批 5 落地记录（2026-10-10，仅动 `inspection/oracle_inspection.sh`）

| 项       | 动手点                                                                                     | 实际做法                                                                                                                                                                                                                                        |
| ------- | --------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **N-1** | `ora_sql_to_md` 状态判定                                                                    | 两处 `grep`（探测 + 抽取）都改成 `^[[:space:]]*` 锚行首。**顺带把"判据行为"钉进自测**：`ALERTPROBE`（数据里含 `ORA-27125`）必须判 `ok`、`INDENTPROBE`（**带前导空白**的真报错）必须仍被判 `schema_mismatch` —— 一条防漏判、一条防误伤。                                                                      |
| **N-2** | `ora_sql_to_md` 行数与 psv 落盘                                                              | 调换了两段的**先后**：① 先定 `status`（rc + err_file）；② **只有 `status=ok` 才数行数**（失败时 stdout 是报错回显，数出来的 3 行是假的）→ 再按"ok 且 0 行 ⇒ empty"降级；③ 最后按状态落 psv：`ok`→数据 / `empty`→`psv-empty` / 其余→**`psv-unavailable`**。**旧代码是"先落 psv 再判状态"**，所以失败项必然先写下一份错的东西。     |
| **N-3** | `collect_structured_package` / `sync_status_to_package`（新）/ `write_packaging_status`（新） | 顺序改为：① 拷贝 ② `snapshot.json` ③ **`security_scan` → `sync_status_to_package`** ④ `collection_status.json` + `summary.txt` ⑤ **`packaging_status.json`** ⑥ `manifest.json` ⑦ `tar.gz`。`sync_status_to_package` **只对"定论早于封包"的项有效**，函数头上写明了这条边界。 |
| **N-4** | `oracle.validation.rman_validate`                                                       | 列名 `session_key` → `session_recid, session_stamp`；调用点上方留注释说明"哪张视图有这一列、为什么不能用"。                                                                                                                                                              |
| **N-5** | 尾部汇总 + `generate_collection_status`                                                     | 尾部删掉 6 个 `awk` 直查，改 `refresh_status_tally` + `is_failure_status` 分桶（**11 档互为补集、不重不漏**）；`summary.txt` 与操作员副本都新增 **`分档合计 / 总数`** 自校验行；控制台新增一行解释"包内台账 = 总数 − 1"。                                                                               |
| **B**   | `generate_snapshot_json` 的 `artifacts` 段                                                | `report_md` → `report_md_external`（值改为**绝对路径**）+ 新增 `report_md_scope`；`artifacts` 增加 `packaging_status`。**Python 侧无消费者**（已 `grep` 确认 `plugins/oracle/*` 不读该字段），改动零风险。                                                                       |

**构造实测（可复跑）**：`python _work/_b1_make_verify.py` → `bash _work/_verify_b1.sh` → **PASS=84 / FAIL=0**（约 1 分钟，不连真库）。批 1–4 的 T1–T13 之外新增：

| 用例  | 覆盖                                                                                                                                                                                                                                                                                                                                                          |
| --- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| T14 | N-1 锚行首：数据里的 `ORA-27125` 仍判 `ok`（`row_count=1`、数据原样进 psv）；**带前导空白的真报错**仍判 `schema_mismatch`；`logs/modules/sql.log` **不含** `ORA-27125`（不污染）                                                                                                                                                                                                                  |
| T15 | N-2：失败项的 `status=schema_mismatch`、**`row_count=0`**、psv 是 `# oracle-inspection psv-unavailable …` 占位、首字符 `#`、**不含** sqlplus 回显、**不与 `psv-empty` 同形**；失败原文仍完整进模块日志                                                                                                                                                                                           |
| T16 | N-3：`security_scan` 的 status **补拷进包**（`TASK_DIR/status/…`）且出现在 `collection_status.json.items[]`；`packaging_status.json` 可解析（`phase=pre-archive`/`security_scan=ok`）。N-4：`rman_validate` **段内**无 `session_key`、有 `session_recid, session_stamp`、`v\$rman_status` 仍转义。B：`artifacts` 里 `report_md` 不在、`report_md_external` 在、`report_md_scope=outside-package` |
| T17 | N-5：`partial` 计入台账；`summary.txt` 有 `部分成功 partial  : 1`；**`分档合计 == 总数`** 且无 `⚠ 不一致`；尾部确实**复用** `refresh_status_tally` 且**不再**自行 `ls` 计台账                                                                                                                                                                                                                     |

> ★ **本批的三条方法论**（写给下一轮审查）：  
> ① **"看着像报错的数据"比"看着像数据的报错"更常见**。判据必须锚**结构位置**（行首/列位），不能只匹配**内容**——`ORA-` 在行首是报错，在行尾是数据；  
> ② **断言必须覆盖"两个相反结论长得不一样"**。`psv-empty`（查了没有）与 `psv-unavailable`（没采到）是**相反结论**，只断言"首字符是 `#`"两条都能过 —— 必须显式断言**不同形**；  
> ③ **"包内的数"和"控制台的数"必须是同一处算的**。同一个台账在三个地方各写一遍 `awk`，迟早对不上；对不上时还没有任何测试会红（**呈现层/台账层没有功能性失败信号**）。本批把"分档相加 == 总数"做成**运行期自校验行**，把"包内 = 总数 − 1"做成**控制台显式说明**，把"尾部复用 `refresh_status_tally`"做成**静态断言**。  
> ⚠️ **仍然只能靠现场确认的两条**：① N-4 改完要用**同型号实例复采一份**确认 ORA-00904 消失（本机无 Oracle）；② OF-19 的中断路径仍需在真实环境构造一次。

## 批 6：P2 一致性收尾（N-6～N-11、N-13、N-14 + N-15）✅ 已完成（2026-10-10，仅动 `inspection/oracle_inspection.sh`）

**触发**：批 5 修完 5 个 P1 后，用户说"继续批 6"，把体检报告里剩下的 P2 一并做掉。  
**性质**：**不含新采集项**（`item_id` 数量不变：136 SQL + 31 OS + 28 模块），全部是**落点 / 口径 / 声明**层面的一致性修正；  
`COLLECTOR_VERSION` / `SNAPSHOT_SCHEMA_VERSION` / `PACKAGE_VERSION` **仍为 2.0**。

**构造实测（可复跑）**：`python _work/_b1_make_verify.py` → `bash _work/_verify_b1.sh` → **PASS=107 / FAIL=0**（约 1 分钟，不连真库）。  
批 5 的 T1–T17 之外新增：

| 用例  | 覆盖                                                                                                                                                                                                                                                                      |
| --- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| T18 | N-6：2 个 bg 模块**都计入**执行数（26 → 28）；失败模块同时计入失败数；**`skipped` 不计入执行数**（用 `--exclude` 造一条 `skipped` 验证，与同步路径同口径）                                                                                                                                                              |
| T19 | N-7：`SAR_FILE_LIST` 落 `RUNTIME[TMP_DIR]`、脚本里再无 `STATUS_DIR]}/sar_files`。N-8：`tables/` 里那 4 个 `.tsv` 归零、`evidence/` 下 4 个 `.txt` 齐全；`capture_command` 的 `output_file` **跟随新落点**                                                                                          |
| T20 | N-9：`sqlplus_version` 探到 `19.16.0.0.0`（假 sqlplus 的 `-V` 走 stderr）；capabilities 出现 `ip_command`/`ifconfig_command`。N-10：`host_identity` 里 `is_local_target` 不在、`database_target_is_local` 在。N-11：`manifest.self_excluded == ["manifest.json"]` 且**确实不自列**自身、`files[]` 非空 |
| T21 | N-13：`TNS-12541`/`TNS-12560`/`TNS-00511` → `not_enabled`（**且普通致命错误仍判 `error`** —— 防把判据整体放宽）；`capture_command` 全链路                                                                                                                                                       |
| T22 | N-14：两处互斥分支的 `skipped` 已落地（`mutually-exclusive fallback`）、capabilities 的 `ip_command`/`ifconfig_command` 已加。N-15：`summary.txt` 有实时采样双视图说明                                                                                                                               |

**批 6 落地记录（2026-10-10，仅动 `inspection/oracle_inspection.sh`）**

| 项        | 动手点                                              | 实际做法                                                                                                                                                                                                                |
| -------- | ------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **N-6**  | `wait_background_modules`                        | **不看子 shell 的内存变量**，改为读它**落盘的 status 文件**回写 `MODULES_EXECUTED`/`MODULES_FAILED`。口径与同步路径严格对齐：`skipped` 不计、`error` 计入失败；子 shell 连状态都没写（被 kill）时兜底补记 `error` 并计数。                                                      |
| **N-7**  | `collect_sar_history`                            | `SAR_FILE_LIST` 改为 `${RUNTIME[TMP_DIR]}/sar_files.txt`（tmp 在**包树之外** ⇒ 天然不进包）+ `mkdir -p` 兜底。`history/source_files.txt` 成为唯一留档。                                                                                     |
| **N-8**  | `collect_system_static` + md 指引行                 | 4 项落点 `$TABLES_DIR/*.tsv` → `$EVIDENCE_DIR/*.txt`（`memory_snapshot` / `filesystems` / `inodes` / `block_devices`），并把"为什么不能放 `tables/`、为什么不能转真 TSV"写进注释（`lsblk` 的 `MODEL`/`SERIAL` 自身含空格，无法无损 TSV 化）。md 报告里的路径指引同步改。 |
| **N-9**  | `collect_capabilities`                           | 探测改 `sqlplus -V 2>&1 \| sed -n 's/.*Release \([0-9][0-9.]*\).*/\1/p' \| head -1 \| tr -d '\r'` —— 取**纯版本号**（如 `19.16.0.0.0`）；取不到时为空串。                                                                               |
| **N-10** | `generate_snapshot_json`                         | `host_identity` 删 `is_local_target`（保留契约名 `database_target_is_local`）。`capabilities.is_local_target` **保留** —— 它是 OF-13 能力表里的一项，不是"同一处的重复键"。                                                                        |
| **N-11** | `generate_manifest`                              | 头部新增 `self_excluded:["manifest.json"]` + `self_excluded_reason`；`find … ! -name 'manifest.json'` 的隐式排除保留（它才是实现），注释点明两者的关系。                                                                                          |
| **N-13** | `classify_failure`                               | `not_enabled` 分支补 `TNS-12541` / `TNS-00511` / `no listener`（listener 未运行 ≠ 采集失败）。                                                                                                                                   |
| **N-14** | `collect_system_static` + `collect_capabilities` | `ip` 存在时记 `system.ifconfig = skipped`；`chronyc` 存在时记 `system.ntpq_peers = skipped`；**两支都没有**时记 `unsupported`。capabilities 新增 `ip_command` / `ifconfig_command`。                                                     |
| **N-15** | `generate_collection_status`（`summary.txt`）      | "耗时最长前 10"下加两行说明：`oracle.module.realtime_sampling` 与 `timeseries.realtime_sampling` 是同一动作的**模块视图 / 数据文件视图**。                                                                                                        |

> ★ **本批的四条方法论**（写给下一轮审查）：  
> ① **"落点"也是契约**。`tables/` 的承诺是"结构化表格"，把一个 `df` 对齐文本塞进 `.tsv` 是**扩展名说谎** ——  
> 下游不会报错，只会静默切错。**改落点必须在 interface 写明"为什么"**，否则下一个人会把它改回去。  
> ② **修"分支不产生记录"要顺手补反例**。N-14 加的是 `skipped`，但要同时保证"两支都没有"时记 `unsupported`  
> —— 否则换一台没有 `ip`/`ifconfig` 的机器，这个 id 又凭空消失了。  
> ③ **放宽判据必须配一条"不许放宽"的断言**。N-13 往 `not_enabled` 里加了三条 TNS 文本，  
> T21 专门断言"普通致命错误仍判 `error`" —— 否则哪天有人再补两条，`classify_failure` 就整体退化成"什么都算未启用"。  
> ④ **"不自列"要么自列、要么显式声明**。manifest 的自引用是**结构性的**（算不出自己的 sha256），  
> 与其伪造一个，不如把事实写成字段（`self_excluded`）—— 让校验脚本有据可依，而不是靠"数出来少一个"去猜。  
> ⚠️ **N-8 有一处对 Python 侧的可见影响**（`ctx.tables` 不再有 `filesystems` 等键），已在 `interface` §11.8 写明处置建议；  
> 这属"adapter 按 `collector_version` 分叉"工作的一部分，批 6 **未动 Python 侧代码**。

---

## 批 7：第二次复采包体检的 P1 + P2（N-16～N-21）✅ 已完成（2026-10-10，仅动 `inspection/oracle_inspection.sh`）

**触发**：用户交回**批 5 / 批 6 修完后的第一次现场复采包**（`…_2026-10-10_10-43-30.tar.gz`，161 项）问"采集脚本是否需要整改"。  
对账**先证实批 5 / 批 6 的修复全部生效**（三项关键行为 `topsql.recent_errors` error→ok、`rman_validate` schema_mismatch→empty、  
`listener_status` error→not_enabled；另 8 项落点/口径全到位；manifest 307 文件 sha256 **全对**），  
**再暴露出 3 个 P1 + 3 个 P2**。体检报告 = `_work/_pkg3_check_report.md`；解包目录 = `_work/_pkg3/`。  
**性质**：**不含新采集项**（136 SQL + 31 OS + 28 模块不变），三个版本号仍为 **2.0**。

**构造实测（可复跑）**：`python _work/_b1_make_verify.py` → `bash _work/_verify_b1.sh` → **PASS=139 / FAIL=0**（约 1 分钟，不连真库）。  
批 6 的 T1–T22 之外新增：

| 用例  | 覆盖                                                                                                                                                                                                        |
| --- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| T23 | N-16：源码层断言"不合规 id 的 `record_status` 归零"、`collect_structured_package` 段内**不再自记 status**；运行时用假模块验证**非零 rc 原样带出（31，不被 `if !` 压成 1）**、`reason` 来自 `MODULE_REASON`、失败计入失败数；**再验成功路径 `MODULE_REASON` 被清空**（防串味） |
| T24 | N-17：段内断言 `generate_snapshot_json` **排在 `generate_collection_status` 之后**；运行时断言 `snapshot.collection_summary.total == collection_status.summary.total`                                                    |
| T25 | N-18：4 处补参的源码断言 + **生成期 arity 守门脚本可跑且当前全绿** + 运行时 10 参的落列（`output_file` 空、`reason` 落第 10 列、字段总数 = 10）                                                                                                     |
| T26 | N-19：`output_file` 不再写目录、改 `first_existing_relpath` 取代表文件（含"候选全缺 → 空串"单元）。N-20：`kernel_parameters` 有表头、缺 `sysctl` 记 `unsupported`                                                                         |

**批 7 落地记录**

| 项        | 动手点                                                       | 实际做法                                                                                                                                                                                                                                                                                                                                                                                                                                                                |
| -------- | --------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **N-16** | `run_module` + `collect_structured_package`               | 归档一步写了**两条** status（`run_module` 记合规 id，但 `func_name` 恒返回 0 ⇒ **恒 ok**；`collect_structured_package` 又记一条不合规的 `module.structured_packaging` 带真 rc）。修法：**`run_module` 成为唯一记账者** —— ① `if ! ${func_name}` 改 `func; rc=$?`，**rc 原样保留**（30/31 不再被压成 1）；② 新增 `MODULE_REASON` 出口（每次进入前清空），让被调函数回传精确原因；③ `collect_structured_package` 删自记、只 `return "$rc"` + 设 `MODULE_REASON`；④ 控制台 / summary / `packaging_status.json.note` 的 id 文案改 `oracle.module.structured_packaging` |
| **N-17** | `collect_structured_package` 顺序                           | `generate_snapshot_json` 从 `security_scan` **之前**挪到 `generate_collection_status` **之后**。旧顺序下 snapshot 少一条 `package.security_scan`（158 vs 159）。**"同一个函数" ≠ "同一个时点"**                                                                                                                                                                                                                                                                                                 |
| **N-18** | 4 处 `record_status`                                       | 补 `exit_code`：模块 skipped 行、`system.ntpq_peers` skipped 行、`system.ifconfig` skipped 行、`system.sar_history` skipped 行。另新增**第四道静态守门** `_work/_scan_record_status.py`（按 shell 引号规则切词数实参，≠ 10 即拒绝生成自测）                                                                                                                                                                                                                                                                   |
| **N-19** | `collect_sar_history` / `collect_oracle_realtime_samples` | 新增 `first_existing_relpath()`；`system.sar_history` → `history/coverage.json`、`timeseries.realtime_sampling` → `timeseries/oracle_sysstat.csv`；无产物写空串（与 `unsupported`/`skipped` 惯例一致）                                                                                                                                                                                                                                                                                |
| **N-20** | `collect_system_static`                                   | `kernel_parameters.tsv` 补 `path\tvalue` 表头（与 `capabilities`/`hugepages`/`oracle_limits`/`oracle_env` 对齐）；**顺带修**：缺 `sysctl` 时改用 `if/else` 显式记 `unsupported`（旧版 `has_cmd sysctl && …` 什么都不记 → 又一个"永不出现"项，与 N-14 同类）                                                                                                                                                                                                                                                  |
| **N-21** | `interface` §7                                            | `partial` 行更新（`system.sar_history` 也会 `partial`）                                                                                                                                                                                                                                                                                                                                                                                                                    |

> ★ **本批的三条方法论**（写给下一轮审查）：  
> ① **"同源" ≠ "同时点"**。`refresh_status_tally()` 是同一处算式、被三处调用，但只要**取样时刻**不同，  
> 观察集合就不同（snapshot 少一条 → 158 vs 159）。**凡说"三处同源"，必须同时约束"三处在同一时点取样"。**  
> ② **位置参数 ≥ 8 个的 shell 函数必须有机械守门**。`record_status` 少传一个参数**编译期全绿、运行不报错**，  
> 只是把后面实参整体串位；本次靠临时写分词器才发现 4 处。守门放在**生成期**（完备判定）+ 运行期（复跑）。  
> ⚠ 廉价特征断言要当心**子串误命中**：合法的 10 参形态 `… 0 0 0 "" "reason"` 里也含 `0 0 ""`（末三个 token），  
> 所以 `grep -c '0 0 ""' == 0` 是**假阳性**判据（本守门初版实测误报 5 处）。  
> ③ **判定"某状态是否合理"要落到环境事实**。3 项 `not_enabled`（chrony 未跑 / listener 未跑）与  
> `sar_history=partial`（只 2 个 sadf 文件、覆盖 12.13h/24h）**都判对了** —— 别把"客户环境没开"当 bug 修。

**批 8 已完成（2026-10-10，Python 侧）** —— 原记的"adapter 按 `collector_version` 分叉"已落地，**而且范围比原记的更大**：实测发现不只是 5 个真 TAB 表被错解，**98 个 `.psv` 根本没被读**（`glob("*.tsv")` 漏掉），所以照原记只改分隔符是"半修"。详见下面的「批 8」章节。

**仍未做 / 需要下一轮决策**：

| 项                               | 说明                                                                                                                                                                                                                                                                                                          |
| ------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| ★ 采集器**把 `item_id` → 标题写进包**    | 现在 Python 侧的标题表是 `plugins/oracle/item_titles.json`，由 `tools/gen_oracle_item_titles.py` **从采集脚本生成**（测试守门 `test_generated_map_is_in_sync_with_collector` 防漂移）。更彻底的做法是采集器在 `collection_status.json` / `snapshot.json` 里直接带 `title` 字段 —— 那样 Python 侧一份硬编码映射都不需要，但对**已采的包无效**（要等下一次现场采集）。**属采集器改动 → 记为批 9 候选。** |
| `evidence/*.txt` 的其余项           | 本批只回填了 `system.filesystems`（`df -PT`，被 `_check_filesystem_usage` 消费）。其余 22 个 evidence 项按契约只作原文展示，未做通用抽取                                                                                                                                                                                                     |
| `find_table()` 的模糊匹配顺序          | `find_table("FRA", …)` 会把 `oracle.archive.fra_params`（NAME/VALUE 参数表）排在 `oracle.flashback.area_usage`（使用率表）之前命中 —— 本包恰好两者都空所以不影响结论，但这是**隐藏的顺序依赖**，值得单独一轮                                                                                                                                                    |
| `_check_filesystem_usage` 的规则质量 | 回填后本包触发 `COMMON.CAPACITY.FILESYSTEM_USAGE`，命中的是 `/dev/sr0 iso9660 100%`（挂载的 ISO 镜像）。**iso9660 / squashfs 这类只读挂载应排除**，属规则口径问题，不是 adapter 缺陷                                                                                                                                                                  |

> ⚠️ **仍然只能靠现场确认的三条**（前两条自批 5 起挂账）：① N-4 改完要用**同型号实例复采一份**确认 ORA-00904 消失（本机无 Oracle）；  
> ② OF-19 的中断路径仍需在真实环境构造一次；③ **批 7 的复采核对**：控制台「执行模块: 28 / 包内台账 = 总数 − 1」、  
> `snapshot.collection_summary.total == collection_status.json.summary.total`，以及 `status/` 里**不再有** `module.structured_packaging.tsv`。

---

## 批 8：Python 侧真正消费 2.0 包（按 `collector_version` 分叉）✅ 已完成（2026-10-10）

**动的文件**：`plugins/oracle/package_adapter.py`（唯一逻辑改动）、`plugins/oracle/item_titles.json`（新增，生成物）、  
`tools/gen_oracle_item_titles.py`（新增，生成器）、`tests/test_oracle_package_adapter.py`（+8 用例）。  
**采集脚本一行没动** —— 所以 `inspection/oracle_inspection.sh` 仍是 **4610 行 / 105 函数 / 2.0**。

### 起因：实测证明"原记的范围不足以修好"

原记只写"5 个真 TAB 表被 `|` 错解"。拿真实 2.0 包（`…_2026-10-10_10-43-30`）跑一遍才发现更严重：

| 实测（改前）          | 值                                                              |
| --------------- | -------------------------------------------------------------- |
| `ctx.tables` 键数 | **5**，且全是垃圾（整行当键，如 `{'capability\tvalue': 'sqlplus_bin\t/…'}`） |
| 98 个 `.psv`     | **一个都没读**（`glob("*.tsv")` 漏掉）                                  |
| 34 条规则的评估       | triggered 2 / passed 16 / **not_evaluated 16**（覆盖率 53%）        |
| 报告结论            | 2 条 finding、健康分 87 —— 这不是"少看到点东西"，是**报告基本为空**                  |

### 改了什么

| 项                                    | 改前                                     | 改后                                                                                                                           |             |
| ------------------------------------ | -------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------- | ----------- |
| `.psv` 读取                            | 完全忽略                                   | 读，按 \`                                                                                                                       | \`          |
| `.tsv` 读取                            | 读，按 \`                                 | \`（错）                                                                                                                        | 读，按 **TAB** |
| 版本判定                                 | 无（`SUPPORTED_COLLECTOR_MAJOR` 定义了但没人用） | `collector_major(snapshot)`：优先 `collector.version`，退回顶层 `collector_version`；`1` = 1.4 老语义，其余 = 2.0                           |             |
| 表键                                   | 仅文件名 stem                              | `item_id` + **中文标题** + 尾段（`basic.users` / `users`，**仅全局唯一时**）+ 少数 legacy 别名                                                  |             |
| `item_id` 来源                         | `path.stem`（猜）                         | 优先 `status/*.tsv` 的 **`output_file` 反查** —— 2.0 的 `tables/kernel_parameters.tsv` 对应 `system.sysctl_selected`，**文件名与 id 不同名** |             |
| `# … psv-empty / psv-unavailable` 占位 | 已被丢弃（`#` 行）                            | 语义显式：**占位 = 空表**，"查了没有"与"没采到"的区别只去 `status` 列看                                                                               |             |
| `evidence/filesystems.txt`           | 无                                      | 回填 `system.filesystems`（`df -PT`，`split(None, 6)` 保住含空格的挂载点）                                                                 |             |

**为什么需要标题表**：下游 `metrics.py` / `oracle_rules.py` / `presentation.py` 是**按 1.4 的中文标题**写的查找键（66 个调用点）。  
2.0 把文件名换成 ASCII `item_id`，标题只存在于采集脚本与人读 md。所以标题表的**单一来源 = `inspection/oracle_inspection.sh`**，  
`tools/gen_oracle_item_titles.py` 只做抽取，测试守门比对（脚本改了标题而 JSON 没重生成 → 测试红）。

**为什么还要 `LEGACY_ALIASES`**：有 15 个 1.4 键在 2.0 里**改了名**（`asm_diskgroup_summary` / `最近RMAN备份任务状态` /  
`无效_不可用索引` / `lock_wait_chain` / `block_corruption` / `checksum_params` / `controlfile_redundancy` / `archive_mode` …）。  
桥接表只有 15 行，且**测试断言每个 target id 都真实存在于标题表**（防手误）。1.4 包不受影响（命中不了 item_id 就不注册）。

### 落地后的实测（同一个包，改前 → 改后）

| 指标                 | 改前                | 改后                                                                |
| ------------------ | ----------------- | ----------------------------------------------------------------- |
| `ctx.tables` 键数    | 5（垃圾）             | **416**                                                           |
| 规则 `not_evaluated` | 16                | **2**                                                             |
| 规则覆盖率              | 53%               | **94%**                                                           |
| findings           | 2（High 1 / Med 1） | **12（Critical 2 / High 3 / Med 5 / Low 2）**                       |
| 下游 66 个调用点解析成功     | 少量                | **64**（余 2 个是 `asm_diskgroup_summary`，本实例 `has_asm=0`、包里确无 ASM 表） |

改后新出现的结论（说明报告真的吃到数据了）：`ORA.ARCHIVE.MODE` = **数据库未开启归档模式（critical）**、  
`ORA.PERFORMANCE.LIBRARY_CACHE` 命中率偏低（critical）、`ORA.BACKUP.RECENT` 近期无成功备份（high）等。

余下 2 条 `not_evaluated` **判定正确、不是缺陷**：`ORA.STORAGE.ASM`（`has_asm=0`）、  
`ORA.STORAGE.FRA`（`db_recovery_file_dest = -`、FRA 未配置）。

### 批 8 的三条方法论

> ① **"改前先量一遍"比读计划可靠**。计划里写的是"5 个 TSV 表分隔符错"，实测是"98 个 psv 全丢"——  
> 量出来的破坏面才是真范围。**凡"适配器按版本分叉"这类改动，先拿真实包打印 `len(ctx.tables)`，别信文档。**  
> ② **别名注册必须"主键先落、别名补缺、短别名仅全局唯一"**。否则 `usage` / `disk_detail` 这类尾段会在  
> 两张表之间互相覆盖，而且**覆盖是静默的**（注册顺序 = 字母序）。本批实测"同键多表 = 无"。  
> ③ **校验脚本自己也会说谎**。第一版验证脚本写的是 `key in ctx.tables and ctx.tables[key]`，  
> 于是把 21 处"键已注册但表为空（`psv-empty`）"误报成"未注册"——  
> **空表是正确状态，不是缺失**。判"覆盖"只看键在不在，行数交给规则去判。

---

## 批 10：分析层 / 报告层对齐 MySQL（Python 侧）✅ 已完成（2026-10-10）

**动的文件**：`plugins/oracle/presentation.py`（模板 + 派生 + 条件注入）、`plugins/oracle/analyzer.py`（挂实例）、  
`plugins/oracle/word_report.py`（`section_order`）、`tests/test_oracle_confirmations.py`（新增，9 用例）。  
**采集脚本一行没动** —— `inspection/oracle_inspection.sh` 仍是 **4610 行 / 105 函数 / 2.0**。  
**完整审查报告**：`_work/oracle-analysis-gap-review.md`；端到端素材 `_work/_o10/`（含 `ZYDB_oracle_report.docx`）。

### 方法：端点跑一遍再拆 DOCX（别读代码猜）

`plugins/oracle/cli.py` → `generate_report.py` → **python-docx 把生成的报告读回来**对着看。参照系 = MySQL。

★ 这条链上最容易搞错的两件事（先记牢，否则会误判"图不渲染"）：

| 事实                         | 说明                                                                                                                                                                                                                                                                  |
| -------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **两条链路别混**                 | `cli.py` 只产 `analysis.json` + `report_model.json`（presentation 原始信封）；**Word 走 `generate_report.py`**，那里 `adapt_if_needed()` 才调 `report_adapter.adapt_report_model` 注入 `oracle_analysis={"charts":[…]}`。**只看 `report_model.json` 会误判"`oracle_analysis` 缺失 ⇒ 图不渲染"。** |
| **适配器是「deepcopy + 补」不是透传** | source 里没有的键它**不会造** ⇒ presentation 不产的键（`pending_confirmations` / `risk_matrix`）到 Word 就没有                                                                                                                                                                         |

### 及格线只有 1 条：`pending_confirmations` 缺失 → 第 14 章空转

改前 DOCX 正文第 14 章**全部内容**就一句：

> 1. 待客户确认事项 / 数据状态　　**本次分析未产出待确认事项清单，不对是否存在此类事项作结论。**

MySQL 同一章实测 **16 条**真事项。成因：`presentation.build_report_model` 的信封里没这个键，  
`adapt_report_model` 又是 `deepcopy(source)` 起手 —— 上游不产，下游不造。

**落地（与 MySQL 同一条线，四处同改）**：

| # | 位置                                                         | 内容                                                                                                          |
| - | ---------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------- |
| 1 | `presentation.CONFIRMATION_TEMPLATES`                      | **11 条** `rule_id → (主题, 待确认内容)`；只收"须客户拍板 / 须客户提供外部证据"的                                                     |
| 2 | `presentation.pending_confirmations(instance, *, node="")` | 按**已触发的 finding** 派生，同 rule_id 去重；`node` 单实例留空（引擎按 `has_node` 决定列数）                                         |
| 3 | `analyzer.attach_pending_confirmations(analysis)`          | 挂在每个实例上（排在 findings 定型之后）                                                                                   |
| 4 | `presentation.build_report_model`                          | **条件注入** `model["pending_confirmations"] = _merged_confirmations(instances)`；返回 `None`（旧模型）时**不写键**，不打穿冻结契约 |

**收录口径**（与 MySQL 同一条线，写进代码注释防止下一个人加错）：

- ✅ 需要**业务语义决策**的：未开自动扩展是有意管控还是遗漏、哪些是高权限业务账号、失败作业归谁维护；
- ✅ 数据库侧**无法自证**的：备份可恢复性（库里看不到备份平台 / 存储快照）、坏块修复能容忍多少数据损失；
- ✅ 需要**外部资源或停机窗口**的：表空间 / FRA 扩容（采购与审批）、开归档（必须停库）；
- ❌ **不收**纯技术整改项（redo 尺寸、控制文件冗余、日志成员数、统计信息收集、无效对象）——  
  那些直接改就行，问了只增加沟通成本，还会把清单做成**第二个风险台账**。

**实测**（真实 2.0 包 `…_2026-10-10_14-30-48`）：第 14 章由**空转** → **5 条**真事项：

| # | 主题             | 触发规则                           | 依据                      |
| - | -------------- | ------------------------------ | ----------------------- |
| 1 | 备份归属与可恢复性      | `ORA.BACKUP.RECENT`            | （finding 无 facts）       |
| 2 | 归档模式的启用窗口与备份策略 | `ORA.ARCHIVE.MODE`             | `LOG_MODE=NOARCHIVELOG` |
| 3 | 时间源的提供方与同步要求   | `COMMON.SYSTEM.TIME_SYNC`      | `NTP synchronized：no`   |
| 4 | 账户密码的托管方式与轮换窗口 | `ORA.SECURITY.PASSWORD_EXPIRY` | `count=35`              |
| 5 | 锁定账户是否为废弃账号    | `ORA.SECURITY.ACCOUNT_LOCKED`  | `locked_accounts=34`    |

### 章节动线：`oracle_instance` 提到 `storage_capacity` 之前

改前 `section_order` 是「… 系统环境 → 系统性能 → **存储与容量** → **Oracle 实例** → …」。  
第 5 章的内容（表空间 / 数据文件 / FRA / 自动扩展）**全是实例内部的概念** —— 读者还没看到  
"实例是什么"，就先看"它装了什么"。MySQL 的动线是 **实例 → 配置 → 运行 → 容量**。

只换这两项的相对位置（`word_report.py` 一个元组），改后 DOCX：

```
 5. Oracle 实例检查          ← 前移
 6. 存储与容量检查          ← 后移
 …
16. 附录
```

**目录页 16 条与正文 Heading1 逐条一致**（逐条比对：差异 0）。历史那个"目录少一章、19 章起全差 1"未复现。

### 新增守门：`tests/test_oracle_confirmations.py`（9 用例）

| 用例                                                                            | 钉住什么                                                                                                                                   |
| ----------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------- |
| ★ `test_every_rule_id_exists_in_the_rule_set`                                 | 模板引用的 `rule_id` **必须真实存在于 `inspection_rules_oracle.json`**。写了个不存在的 rule_id ⇒ **模板永不触发、报告永远显示"本次没有待确认事项"，而没有任何测试会红** —— 与"写了没人用"同源的静默退步 |
| `test_templates_are_answerable_by_the_customer`                               | 主题非空、问题 > 40 字（太短等于没问）、且不得出现技术整改句式                                                                                                     |
| `test_only_triggered_templated_rules_are_collected`                           | 只认已触发 + 在模板里的规则；同 rule_id 去重                                                                                                           |
| `test_no_findings_yields_empty_list_not_none`                                 | 有键但本次没事 ⇒ `[]`（报告写"本次没有"）                                                                                                              |
| `test_node_defaults_to_blank_for_single_instance`                             | 单实例不渲染节点列（刻意的）                                                                                                                         |
| `test_missing_key_returns_none` / `test_present_but_empty_returns_empty_list` | ★ **`None`（未产出清单）与 `[]`（本次没有）必须可区分** —— 延续「未采集 ≠ 0」                                                                                    |
| `test_merges_every_instance_and_sorts_stably`                                 | 跨实例**逐行保留**（不按主题归并：不同节点证据不一样），按 `(topic, node)` 稳定排序                                                                                   |
| ★ `test_instance_section_precedes_storage_section`                            | **章节顺序**。`test_oracle_plugin.py` 只有 `set(section_order) == section_ids` 的**集合**断言，对顺序**完全不敏感** ⇒ 顺序被改回去也没有任何测试会红，所以必须单独比下标             |

### 明确**不动**的（审查里判为"不是缺陷"）

- `risk_matrix` 缺席：**单实例 MySQL 也不产**（`_risk_matrix` 无 node 可分组就 `return None`）⇒ 当前不算缺口，RAC / 多实例才暴露。
- presentation 里 6 处**直读包内硬编码路径**（`root/"evidence/…"`、`root/"tables/kernel_parameters.tsv"`）绕过 adapter——★ **MySQL 同样 5 处**，是共享层继承来的模式，**要与 MySQL 一起收**，本批不动。
- 2 条 `not_evaluated`（无 ASM / FRA 未配置）、`chronyc` / `listener_status` 的 `not_enabled` —— 全是环境事实。

### 留给下一批（价值排序）

| 批次                 | 内容                                                                                                                                                                                        | 为什么值                                          |
| ------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------- |
| **批 11** ✅ **已完成** | **规则补口**：`oracle.dg` **6 项采集 0 条规则**（MySQL 有 5 条复制规则，最该补）；对象治理 5 项（含 **802 行**的 `awr.segment_bloat`）；审计画像 9 项里 7 项孤儿；`system.hugepages`                                                   | 见下一章「批 11」                                    |
| **批 12**           | **图表收口**：`charts.py` 可产 5 张、`word_report.py` 声明 8 个 ⇒ `oracle_txn_rate` **生成即丢弃**、`oracle_physical_io` **声明但空闲库恒不生成**；并把 `render_section_charts` 在"一张都没生成"时的**静默 `return False`** 改成留一行原因 | 与「未采集 ≠ 0」同源                                  |
| **批 13** ✅ **已完成**           | **展示补口**：`oracle.awr` **8 项一个都不进报告**（AWR 是 Oracle 最有价值的诊断数据）、`oracle.dg` 6 项、`oracle.audit` 7 项；`undo_usage` / `temp_sort_usage` / `shared_pool_frag`                                     | 见下一章「批 13」；报告条目数（53）**已多于 MySQL（37）**—— 不是"展示少"，是**分布偏** |

> ★ **学 MySQL 的机制，不学它的键堆积**。值得学：`pending_confirmations` 模板、新键**一律条件注入**、  
> `section_order` 叙述动线、`_risk_matrix`（多实例）。**不要学**：MySQL 顶层 `environment` / `security` /  
> `capacity` 与 `system_analysis.metrics`、`mysql_performance.commentary` —— `word_engine.py` 里  
> **grep 不到任何一处读它们**，是死键。Oracle 顶层 14 个键**每个都被引擎真读**，这条线要保持。  
> **「写了没人用」比「没写」更贵**，会误导下一个维护者。

---

## 批 11：规则补口 —— 「采了有数据、零判据」的四个域（Python 侧）✅ 已完成（2026-10-10）

**动的文件**：`plugins/oracle/inspection_rules_oracle.json`（规则包 **34 → 42**）、  
`plugins/oracle/oracle_rules.py`（8 个 `_check_*` + 2 个模块级辅助函数）、  
`tests/test_oracle_rule_gaps.py`（新增，16 用例）、`tests/baselines/oracle/current/`（整体刷新）。  
**采集脚本一行没动** —— `inspection/oracle_inspection.sh` 仍是 **4610 行 / 105 函数 / 2.0**。

### 出发点：缺口是机械可查的，不靠"感觉"

判据两条：① 该采集项在包内**有数据**（`status=ok` / 有行）；② 在 `oracle_rules.py` 里**找不到任何消费者**。  
右侧是 MySQL 已有的同类判据 —— 说明这是"能做到但没做"，不是"无法做"。

| 域             | 采集项                    | 本包数据                                                                                               | MySQL 对应判据                                       |
| ------------- | ---------------------- | -------------------------------------------------------------------------------------------------- | ------------------------------------------------ |
| **DataGuard** | 6 项全无判据                | `broker_enabled` / `archive_dest` / `recent_events` / `protection_mode`(空) / `log_sequence_gap`(空) | `REPLICATION.HEALTH` / `RESIDUAL_CHANNEL` / …5 条 |
| **对象治理**      | 5 项                    | `awr.segment_bloat`（**库内 802 行**，入包 TOP4）/ `basic.large_segments`(21) / `index.large_segments`(21) | `SCHEMA.FRAGMENTATION` / `CAPACITY.LARGE_TABLE`  |
| **口令画像**      | `audit.profile_params` | 6 行（DEFAULT profile）                                                                               | `SECURITY.*` 一族                                  |
| **大页**        | `system.hugepages`     | 9 行（THP + `/proc/meminfo` 实测值）                                                                     | `SYSTEM.TRANSPARENT_HUGEPAGE`                    |

> ⚠️ 统计口径：`status/*.tsv` 的第 6 列（`row_count`）是**数据库侧真实行数**，第 7 列是**入包行数**  
> （采集器截断到 TOP-N）。`segment_bloat` 的 "802" 是前者，包里只有 4 行 —— **不是缺陷**，  
> 规则要按入包行判，并把"N 条候选"写进 fact。

### 新增的 8 条规则

| rule_id                         | 判据                                                               | 严重度    | 阈值键                                 |
| ------------------------------- | ---------------------------------------------------------------- | ------ | ----------------------------------- |
| `ORA.DG.TRANSPORT_ERROR`        | `v$archive_dest` 有 `ERROR` 非空的目的地（**主库也适用**）                     | high   | —                                   |
| `ORA.DG.APPLY_LAG`              | `v$dataguard_stats` 的 transport/apply lag 超阈值（解析 `+DD HH:MM:SS`） | medium | `dg_lag_seconds_warning` = 900      |
| `ORA.DG.LOG_GAP`                | 主备归档日志序差距 > 阈值                                                   | high   | `dg_log_gap_warning` = 5            |
| `ORA.DG.EVENT_ERROR`            | `v$dataguard_status` 近 24h 有非零 `ERROR_CODE`                      | medium | —                                   |
| `ORA.SCHEMA.SEGMENT_BLOAT`      | `BLOAT_MB`（实际 − 估算）超阈值                                           | medium | `segment_bloat_mb_warning` = 1024   |
| `ORA.CAPACITY.LARGE_SEGMENT`    | 最大表/索引段超阈值（索引 `SIZE_MB` 折算 GB）                                   | low    | `large_segment_gb_warning` = 100    |
| `ORA.SECURITY.PASSWORD_PROFILE` | `PASSWORD_VERIFY_FUNCTION` 为空 / 失败次数或口令有效期 `UNLIMITED`           | medium | —                                   |
| `ORA.SYSTEM.HUGEPAGES`          | THP 未关 **或** HugePages 配了却闲置（Free 占比高）                           | medium | `hugepage_free_ratio_warning` = 0.5 |

### ★ 两条最容易照抄错的地方

1. **大页口径与 MySQL 相反。** MySQL 只要求**关掉** THP；Oracle 还要把 SGA 放进 HugePages（静态大页）——  
   同一份 `hugepages.tsv`，Oracle 侧多出"配了但没被用掉"这个判据（`Total > 0 且 Free/Total > 0.5`）。  
   照抄 MySQL 会漏掉 Oracle 最典型的那半。
2. **无备库 ≠ 延迟正常。** `v$dataguard_stats` / `v$log_sequence_gap` 在未配置 DG 的库上是空的 ——  
   两条备库规则都落 `not_evaluated`（延续「未采集 ≠ 0」），**不能落 `passed`**，否则报告会宣称  
   "备库延迟正常"，而事实是没有备库。

### 实测（真实 2.0 包 `…_2026-10-10_14-30-48`）

| 指标            | 批 10 后      | 批 11 后                                      |
| ------------- | ----------- | ------------------------------------------- |
| 规则数           | 34          | **42**                                      |
| 触发 / 通过 / 未评价 | 10 / 20 / 4 | **13 / 25 / 4**（覆盖率 90%）                    |
| findings      | 12          | **13**（Critical 2 / High 3 / Med 6 / Low 2） |
| 健康分           | 13          | **10**                                      |

- ✅ **新触发 1 条**：`ORA.SECURITY.PASSWORD_PROFILE` —— DEFAULT profile 的 `PASSWORD_VERIFY_FUNCTION = NULL`  
  （库内 6 行 Profile 参数，无复杂度校验）。
- ✅ 判定正确、**故意不触发**：`DG.TRANSPORT_ERROR`（`ERROR = '-'`）、`DG.EVENT_ERROR`（2 条挂载期信息事件，  
  `ERROR_CODE = 0`）、`SEGMENT_BLOAT`（最大 0.2MB）、`LARGE_SEGMENT`（最大 0.009GB）、`HUGEPAGES`（THP 已是 `never`）。
- ⬜ `DG.APPLY_LAG` / `DG.LOG_GAP` 落 `not_evaluated`：本实例单机无备库 —— **环境事实，不是缺陷**。

### 基线刷新（`tests/baselines/oracle/current/`）

加规则会改变"我方分析器在真实包上的输出"，所以**基线必须整体重出**。刷新前先用结构化 diff 证明  
该目录确是**本仓库流水线**的产物（除规则与时间戳相关的 **9 处标量叶子**外，其余逐字节相同）：

| 字段                                                 | 旧                | 新                    |
| -------------------------------------------------- | ---------------- | -------------------- |
| `health_score`                                     | 26               | **23**               |
| `rules` / `triggered` / `passed` / `not_evaluated` | 34 / 10 / 20 / 4 | **42 / 11 / 24 / 7** |
| `risk_register` / `severity_counts`                | 10 / 2-2-4-2     | **11** / 2-2-**5**-2 |
| `sections` / `items` / `charts`                    | 9 / 52 / 8       | 不变；8 张 PNG **逐字节不变** |

- 刷新**顺带纳入**批 10 的两处遗留：`pending_confirmations`（5 条）首次进基线；章节动线调整后  
  docx 目录由「5. 存储与容量 / 6. Oracle 实例」变为「5. Oracle 实例 / 6. 存储与容量」。
- `tests/test_oracle_plugin.py` 只需改 4 个数（26→23、10→11、34→42 ×2）；`items` 52 / `sections` 9 /  
  各 item 状态分布（normal 35 / not_evaluated 8 / risk 4 / attention 5）**全部不变** —— 说明新规则  
  没有偷偷改动报告结构，只新增了一条 finding。
- **`source_implementation.*` 未改写**：它记录的是**来源快照**（外部参照实现）的指纹，不是当前工作树。

### 新增守门：`tests/test_oracle_rule_gaps.py`（16 用例）

每条规则都用"**改数据 / 改阈值 → 结论跟着变**"证明旋钮真被读（防"默认值恰好相同"的假绿）：  
`test_dg_transport_error_flags_only_a_real_error` / `test_dg_lag_without_a_standby_is_not_evaluated` /  
`test_dg_lag_reads_the_threshold` / `test_dg_log_gap_threshold_and_reason` /  
`test_dg_events_only_error_codes_trigger` / `test_segment_bloat_uses_mb_threshold` /  
`test_large_segment_folds_index_mb_into_gb` / `test_password_profile_*`（3 条）/  
`test_hugepages_thp_never_is_ok` / `test_hugepages_thp_always_triggers` /  
★ `test_hugepages_configured_but_unused_triggers`（Oracle 特有半条）/ `test_parse_interval_seconds` 等。

**负控制自证**（`_work/_b11_negctl.py`）：故意破坏 4 处实现 ——  
① 无备库改判 passed；② 忽略"HugePages 配了没用掉"（退化成 MySQL 口径）；③ 延迟阈值写死 900；  
④ 间隔解析丢掉"天" —— **4/4 全部真实变红**（各只红 1 条，证明不是靠别的用例顶数），  
`finally` 逐字节复原并校验 sha256，复原后复跑全绿。

### 回归

全量 `pytest` = **5 failed / 326 passed / 159 subtests**（含本批 16 用例 + 批 10 的 9 + 批 8 的 12；  
5 个 FAIL 仍是 MySQL 源包缺失的长期问题，**无新增** —— 实测回执见下）。

### 留给下一批

| 批次                 | 内容                                                                                                                        |            |
| ------------------ | ------------------------------------------------------------------------------------------------------------------------- | ---------- |
| **批 12** ✅ **已完成** | **图表收口**：`charts.py` 可产 9 张、`word_report.py` 曾只声明 8 个 ⇒ `oracle_txn_rate` **生成即丢弃**；图全跳过时引擎**静默 `return False`**          | 见下一章「批 12」 |
| **批 12b** ✅ **已完成** | **图表补口**：补三张「有数据没画」的图 —— 表空间使用率 Top-N（条形）/ Top SQL 执行时间 Top-N（条形）/ 每日归档量趋势（折线）；条形渲染入口下沉公共层 `render_bar_spec`（纯新增）；图 **8 → 10** |
| **批 13** ✅ **已完成**           | **展示补口**：`oracle.awr` 8 项一个都不进报告、`oracle.dg` 6 项、`oracle.audit` 7 项；`undo_usage` / `temp_sort_usage` / `shared_pool_frag` |            |
| **批 14** ✅ **已完成**       | 取数口径统一：presentation 里 6 处直读包内硬编码路径（**MySQL 也有 5 处，一起收**）                                                                  |            |

---

## 批 12：图表收口 —— 「声明集合 = 可产出集合」（Python 侧）✅ 已完成（2026-10-10）

**动的文件**：`plugins/oracle/charts.py`（新增单一来源 `ORACLE_CHART_IDS` + 产出侧守门）、  
`plugins/oracle/word_report.py`（声明**从 id 清单派生**）、`inspection_core/word_engine.py`  
（`render_section_charts` 不再静默 + `_missing_charts_note()`）、`tests/test_oracle_chart_coverage.py`  
（新增 7 用例）、`tests/test_word_engine.py`（新增 4 用例）。  
**采集脚本一行没动** —— `inspection/oracle_inspection.sh` 仍是 **4610 行 / 105 函数 / 2.0**。  
判据来源：`_work/oracle-analysis-gap-review.md` §2.1 / §2.4（本批已在该文件 §9 闭环）。

### 出发点：两个集合分叉会同时制造两种**静默**缺陷

`charts.py` 能产出 9 张（4 OS + 5 Oracle），`word_report.py` 只声明了 8 张 —— **两个方向都错**：

| 方向          | 现象                            | 读者看到什么                             |
| ----------- | ----------------------------- | ---------------------------------- |
| **产出了没人声明** | `oracle_txn_rate` 画出来、落盘、进模型  | **什么都没有**（生成即丢弃，图算了白算）             |
| **声明了产不出**  | `oracle_physical_io` 在空闲库恒不生成 | 也是什么都没有 —— 分不清"这章本就没有图"和"本该有图没画出来" |

第二种还把第三种病带出来：`word_engine.render_section_charts` 在过滤后集合为空时**静默  
`return False`**，连"趋势图"小节都不出现、也不留任何说明。

### 落地四件事

| # | 改动                                                                      | 落点               |
| - | ----------------------------------------------------------------------- | ---------------- |
| 1 | **单一来源** `ORACLE_CHART_IDS`（5 张，顺序即上图次序）；`_figure()` 改按 chart_id 命名 PNG | `charts.py`      |
| 2 | 两个章节的 `ChartDefinition` **全部由 id 清单派生**（图题留在报告层，属呈现）                    | `word_report.py` |
| 3 | 声明了图却一张都没产出 ⇒ **留一行原因**（不改小节号、不渲染空的"趋势图"标题）                             | `word_engine.py` |
| 4 | 产出侧防呆：未注册的 chart_id **当场 `ValueError`**                                 | `charts.py`      |

> ★ **`inspection_core/word_engine.py` 是四库共用**，第 3 条对 MySQL / PG / SQL Server 一起生效。  
> 这是有意的：那三库同样存在"声明了图但没画出来"的静默。改动只**新增**一行提示，  
> 数据齐全时**一个字节都不变**（MySQL 基线文档的规范化 XML hash 测试原样通过可证）。

### ★ 两条最容易做错的地方

1. **"单一来源"不等于"两边各写一份碰巧相等"** —— 必须真的**派生**：`word_report` 从  
   `ORACLE_CHART_IDS` 取 id，而不是再抄一遍。抄的那份一旦漏一个（正是 `oracle_txn_rate` 的病因），  
   静态读代码看不出来、报告里也看不出来。守门用例断言的是**列表相等（含顺序）**，不是集合相等。
2. **"声明了产不出"必须与"没声明"区分开**：数据不足导致某张不产出是**运行期数据事实**，  
   不许把声明集合改成跟着数据漂（否则 `oracle_txn_rate` 在单机空闲库上会被"修"成永久不存在）。  
   用例 `test_flat_series_drops_one_chart_but_not_the_declaration` 同时钉住这两点。

### 实测（真实 1.4 包 ZYDB，`…_2026-08-10_20-45-41`）

| 指标                              | 批 11 后                 | 批 12 后         |
| ------------------------------- | ---------------------- | -------------- |
| 图 PNG                           | 8                      | **8（逐字节不变）**   |
| 声明集合                            | 8（漏 `oracle_txn_rate`） | **9 = 可产出 9**  |
| 正文段落                            | 729                    | **729（逐段相同）**  |
| `sections` / `items` / `charts` | 9 / 52 / 8             | 9 / 52 / 8（不变） |

- 本包在这批上是**严格无操作**：`user_commits` 在采样窗口内**恒为 64** ⇒ 提交速率为 0 ⇒  
  `oracle_txn_rate` 本就未产出；两个绘制章节各有图 ⇒ 新提示不出现。实测正文与改动前  
  **逐段相同**，唯一差异是封面"生成时间"戳（`15:32:50` → `15:35:04`）。
- 两条新行为各由**合成数据**的用例钉住（喂足速率 ⇒ 5 张全出；提交全零 ⇒ 只少 txn_rate、  
  声明不动），**不依赖这份包的数据巧合**。

### 新增守门

- `tests/test_oracle_chart_coverage.py`（**7 用例**）：声明 == 单一来源（含顺序）/ OS 侧同样；  
  `oracle_txn_rate` 已在声明里；每张都有图题；喂足数据 ⇒ 产出集合 == 注册集合；  
  提交全零 ⇒ 少一张但声明不动；**未注册 id 当场报错**（monkeypatch 常量验证，`finally` 复原）。
- `tests/test_word_engine.py::ChartSkippingTests`（**4 用例**）：提示文字含图题与**翻译后**的原因、  
  未登记代码**原样带出**、无记录时给通用解释、端到端渲染出提示且**不新增"趋势图"标题**  
  （只数正文 Heading 2 —— 目录页也会列同名条目，混进来会数出 3 个）。

**负控制自证**（`_work/_b12_negctl.py`，只改实现分支/常量）：① 声明侧手抄清单漏掉 `oracle_txn_rate`；  
② 恢复静默 `return False`；③ 清空原因翻译表；④ 摘掉产出侧 id 守门 —— **4/4 全部真实变红**  
（各只红 1～3 条，目标用例确在失败清单里）。`finally` 逐字节复原 + sha256 比对，复原后复跑 **25 passed**。

### 回归

全量 `pytest` = **5 failed / 337 passed / 159 subtests**（含本批 11 用例；5 个 FAIL 仍是  
MySQL 源包缺失的长期问题，**无新增**）。MySQL 基线文档 hash 测试原样通过 —— 证明共享引擎的  
改动在"数据齐全"路径上**零字节影响**。

### 留给下一批

| 批次           | 内容                                                                                                                                                                                          |
| ------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **批 12b** ✅ **已完成** | **图表补口**：补三张「有数据没画」的图 —— 表空间使用率 Top-N（条形）/ Top SQL 执行时间 Top-N（条形）/ 每日归档量趋势（折线）；条形渲染入口下沉公共层 `render_bar_spec`（纯新增）；图 **8 → 10** |
| **批 13** ✅ **已完成**     | **展示补口**：`oracle.awr` 8 项一个都不进报告、`oracle.dg` 6 项、`oracle.audit` 7 项；`undo_usage` / `temp_sort_usage` / `shared_pool_frag`                                                                   |
| **批 14** ✅ **已完成** | 取数口径统一：presentation 里 6 处直读包内硬编码路径（**MySQL 也有 5 处，一起收**）                                                                                                                                    |

---

## 批 13：展示补口 —— 「采了有数据、一个字不进报告」的四个域（Python 侧）✅ 已完成（2026-10-10）

**动的文件**：`plugins/oracle/presentation.py`（新增 `_cell()` + 24 个展示条目 + 章名收口）、
`tests/test_oracle_display_coverage.py`（**新增，16 用例 / 9 subtests**）、
`tests/test_oracle_plugin.py`（4 处断言更新）、`tests/baselines/oracle/current/`（整体刷新）、
`_work/oracle-analysis-gap-review.md`（§10 闭环）。
**采集脚本一行没动** —— `inspection/oracle_inspection.sh` 仍是 **4610 行 / 105 函数 / 2.0**。
判据来源：`_work/oracle-analysis-gap-review.md` §4（本批已在该文件 §10 闭环）。

### 出发点：先把「展示少」证伪，再把「分布偏」变成清单

批 10 已经把报告层与 MySQL 对齐过一次，当时的印象是"报告条目已经比 MySQL 多"（53 > 37）。
但条目数多**不等于**该展的都展了 —— 真判据是**逐项**的：

| 机械判据 | 含义 |
|---|---|
| 该采集项在包内**有数据**（`status=ok` / 有行） | 数据不是问题 |
| **且**在 `presentation.py` 里**找不到任何消费者** | 展示是问题 |

按此判据扫一遍，命中 **27 项**；其中 `oracle.awr` / `oracle.dg` / `oracle.audit` 三个域
**整域零展示**（AWR 是 Oracle 最有价值的诊断数据，却一条都不进报告）。
`_work/_b13_probe.py` 把这 27 项的 `status` + `output_file` + **真实列名** + 样例行全部打印出来，
再用 `ctx.tables` 逐项确认**数据早就在包里**（`oracle.awr.segment_bloat` 4 行、`undo_usage` 20 行、
`system_privileges` 19 行…）—— 缺的**只是展示层的引用**。

> ★ 一处口径纠偏：gap-review 里写的"`oracle.dg` 6 项 / `oracle.audit` 7 项"是**未展示项数**，
> 不是这两个域的**采集项总数**。落地时按实际包逐项核，避免把"部分补上"当成"整域补上"。

### 落地：24 个展示条目，分散进已有章节（9 章不变）

| 章节（`section_id`） | 新增展示条目 | 数据来源（`item_id`） |
|---|---|---|
| 对象检查 `objects` | 段空间膨胀预估 / 高水位段 / 索引使用 / 统计信息缺失 | `oracle.awr.segment_bloat`（接规则 `ORA.SCHEMA.SEGMENT_BLOAT`）、`oracle.awr.hwm_segments`、`oracle.awr.index_usage`、`oracle.awr.missing_stats` |
| 存储与容量 `storage_capacity` | 自动段空间管理 | `oracle.awr.assm_status` |
| Oracle 实例 `oracle_instance` | 自动维护任务 / 管理包访问设置 | `oracle.awr.auto_stats_job`、`oracle.awr.package_access` |
| Oracle 性能 `oracle_performance` | undo 使用 / undo 参数 / 临时表空间排序段 / 共享池碎片 / SQL 计划基线 | `oracle.performance.undo_usage`、`undo_params`、`temp_sort_usage`、`shared_pool_frag`、`oracle.awr.sql_plan_baseline` |
| 安全与审计 `security` | 系统权限 / Profile 参数 / 非默认 Profile / 口令文件成员 / FGA / 对象审计 / 失败登录 | `oracle.audit.system_privileges`、`profile_params`（接 `ORA.SECURITY.PASSWORD_PROFILE`）、`profile_nondefault`、`password_file_members`、`fga`、`object_audit`、`failed_logins` |
| 备份、恢复与高可用 `backup_recovery` | 归档路径 / broker 参数 / 保护模式 / 日志序列缺口 / 近期事件 | `oracle.dg.archive_dest`（接 `ORA.DG.TRANSPORT_ERROR`）、`broker_params`（`broker_enabled` 折叠进来）、`protection_mode`（接 `ORA.DG.APPLY_LAG`）、`log_sequence_gap`、`recent_events`（接 `ORA.DG.EVENT_ERROR`） |

**唯一一处章名改动**：`备份与恢复检查` → **`备份、恢复与高可用检查`**。
DataGuard 的 5 项从这里开始展示，把"高可用"写进章名才名实相符（学 MySQL 的
「日志、备份与复制」一章收口）；**章节数仍为 9**，目录与正文编号算式（都从 `len(sections)` 派生）不受影响。

### ★ 三条最容易做错的地方

1. **`presentation.py` 的条目不带 `item_id`。** 它由 `report_adapter._extract_item_id(source, section_id, index)`
   从 `source` **尾段**派生（按 `" — "` / `" – "` / `" - "` 切分）。在 presentation 层做覆盖对账，
   必须自己复刻同一套切分才能把 `item_id` 拿回来 —— 直接 `item["item_id"]` 会 `KeyError`。
2. **"这项采过没有"只能看表键是否在 `ctx.tables`。** `non_ok_items` **只记失败态**
   （`permission_denied` / `timeout` / `error`…），`status=empty`（查了没有）与"根本没采"被它一并吞掉；
   而空表在包里**也是一次登记**（`.psv` 存在、`row_count=0`）。
3. **★ 对着冻结基线断言，抓不到实现回归。** 章名断言（`test_baseline_has_new_items_and_renamed_chapter`）
   只读 `tests/baselines/oracle/current/report_model.json` 这份**死文件**，改实现它当然不动 ——
   负控制第 5 项（把章名改回去）**第一次 rc=0 / 15 passed，没变红**。补一条**走实现路径**的用例
   （`test_backup_chapter_title_reflects_dataguard`）后才 5/5 全红。这与 AST 对账盲区**同源**：
   **断言的数据源与被守对象不在同一层，绿色不代表正确**。

### 三条机制性改动

| # | 改动 | 落点 | 为什么 |
|---|---|---|---|
| 1 | 模块级 `_cell(row, *names)` —— 按候选列名取值，**容忍 sqlplus 表头截断** | `presentation.py` | `SEGMENT_SPACE_MANAGEMENT`→`SEGMEN`、`ADMIN_OPTION`→`ADM`、`COMPRESSION`→`COMPRES`、`RESOURCE_TYPE`→`RESOURCE`、`SIZ`；`row.get(完整列名)` 取不到时**不报错、只少一列** ⇒ 静默失真 |
| 2 | `_listed(...)` —— 空数据项**按真实 `collection.status` 分叉文案** | `presentation.py` | `success_no_data`（查了、确认没有）与 `not_evaluated` / `permission_denied`（没采到）是**相反结论**，不许在报告里长得一样、更不许一起消失 |
| 3 | `_analysis()` 的 `not_evaluated` 分支**透出 `reason`** | `presentation.py` | 原先一律印"证据不足，未执行评价。"，把规则引擎算出的"无备库（未配置 DataGuard）"丢了 ⇒ 读者以为引擎没跑 |

> ★ 第 1 条是**本轮新发现**的失真源：sqlplus 会**按列的显示宽度截断表头**，截断长度取决于该列的
> 数据/声明宽度。列名不能靠猜 —— 2.0 包里 10 张目标表是空的（`psv-empty` 占位行无表头）、
> 1.4 包里这 10 张表 `rows=0`，于是**回采集脚本读 SQL 原文**
> （`inspection/oracle_inspection.sh:2706-2745` / `2810-2875` / `2910-2970` / `2106-2160`）拿到契约列名。

### 实测（两份真实包）

| 指标 | 1.4 包 ZYDB（`…_2026-08-10_20-45-41`） | 2.0 包（`…_2026-10-10_14-30-48`） |
|---|---|---|
| 检查项 `items` | 52 → **76** | 逐项核对 |
| 章节数 `sections` | 9 → **9（不变）** | 9 |
| 8 张 PNG | **逐字节不变** | — |
| 健康分 `health_score` | **23（不变）** | 10 |
| findings | **11（不变）** | — |
| 新条目渲染 | **24 个全部渲染**，9 条"已检查，本次未发现相关记录。"空态 | 全部 `col=ok` 或 `col=empty`，**无一条误标** |
| 目录 / 正文 H1 | **1—16 逐条对齐** | — |

### 新增守门与负控制

- `tests/test_oracle_display_coverage.py`（**16 用例 / 9 subtests**，**合成 `FakeContext`**，
  不连库、不读采集包）：① `DisplayCoverageTests`（24 项都在、归属章节对、有数据就出行、章节数仍 9、
  **备份章标题走实现路径**）；② `CellToleranceTests`（`SAMPLE_ROWS` 列名**故意混入截断形态**）；
  ③ `EmptyItemHonestyTests`（空 / 未采 / 权限不足三种文案**各不相同**）；
  ④ `DataGuardWiringTests`（无备库落 `not_evaluated` 且 `conclusion` 含 `reason`）；
  ⑤ `FrozenBaselineTests`（基线 9 章 / 76 项 / 章名 / 24 个 `item_id` 都在 —— **先过 `adapt_report_model`**）。
- 负控制 `_work/_b13_negctl.py` **5/5 全部真实变红**，`finally` 逐字节复原 + sha256 校验，
  复原后复跑 **16 passed**。

### 基线刷新

`tests/baselines/oracle/current/` **整体重出**：`analysis.json` / `analyzer_status.json` / `llm_input.json` /
`report_model.json` / `report_model_standard.json` / `oracle_professional_report.docx` 全部替换
（`charts/*.png` 逐字节不变）；`baseline.json` 的 `expected_summary.items` 52 → 76、
`baseline_version` → `"batch-13-display-coverage"`、旧记录压入 `refreshed_history`、
`intentional_differences_from_source` 追加 1 条。**`source_implementation.*` 刻意不重写**。
换行契约：`baseline.json` **CRLF=122 / lone LF=0**。

### 留给下一批

| 批次 | 内容 |
|---|---|
| **批 12b** ✅ **已完成** | **图表补口**：补三张「有数据没画」的图 —— 表空间使用率 Top-N（条形）/ Top SQL 执行时间 Top-N（条形）/ 每日归档量趋势（折线）；条形渲染入口下沉公共层 `render_bar_spec`（纯新增）；图 **8 → 10** |
| **批 14** ✅ | 取数口径统一（见下一章） |
| **批 13 有意未做** | `oracle.basic.large_segments` / `oracle.index.large_segments`（`ORA.CAPACITY.LARGE_SEGMENT` 的数据源）与 `system.hugepages`（`ORA.SYSTEM.HUGEPAGES` 的数据源）同样"有判据、无展示"，留待下一轮 |

---

## 批 14：取数口径统一 —— 非表格式证据文件的路径只在一处写（Python 侧）✅ 已完成（2026-10-10）

**动的文件**：`inspection_core/models.py`（`PackageContext.raw_lines` + `_RAW_FILE_DIRS`）、
`plugins/oracle/presentation.py`（6 处调用点 + 5 个解析器改签名）、
`plugins/mysql/presentation.py`（5 处调用点 + 4 个解析器改签名）、
`tests/test_raw_file_access.py`（**新增，17 用例**）、`tests/test_oracle_display_coverage.py`（FakeContext 补 `raw_lines`）。
**采集脚本一行没动**；**基线一行没动**（见"等价性证明"）。

### 出发点：同一份文件，两套互不相干的读取实现

呈现层此前**不走 adapter**，自己拼包内路径再自己解析：

| 位置 | 直读的包内文件 |
|---|---|
| Oracle `presentation.py` | `evidence/lscpu.txt` / `evidence/time_status.txt` / `tables/kernel_parameters.tsv` / `evidence/mounts.txt` / `evidence/dmesg_errors.txt` / `evidence/os_release.txt` |
| MySQL `presentation.py` | `tables/kernel_parameters.tsv`（2 处）/ `evidence/lscpu.txt` / `evidence/mounts.txt` / `evidence/dmesg_errors.txt` |

而 `package_adapter.py` 那一侧**已经在干同一件事**（把 `tables/*.psv` 注册进 `ctx.tables`，
还专门回填了 `evidence/filesystems.txt`）。于是同一份 `kernel_parameters.tsv` 有了**两份解析实现**，
改一处忘另一处 —— **没有任何测试会红**。

> ★ 真风险不是"代码不好看"，是**静默少一节内容**：批 6 / N-8 就把 4 个「列对齐文本」从 `tables/`
> 挪到了 `evidence/`。目录写死的读法在移动后**只返回空**：报告少一节、规则层不报、测试不红。

### 落地：一个访问点，目录只写一次

| # | 改动 | 落点 |
|---|---|---|
| 1 | 新增 `PackageContext.raw_lines(name)` —— 按**契约文件名**在 `tables/`、`evidence/` 两处查找并返回原始行；缺文件返回 `[]` | `inspection_core/models.py` |
| 2 | 目录集中到模块级 `_RAW_FILE_DIRS = ("tables", "evidence")`（**目录名全仓库只出现这一处**） | `inspection_core/models.py` |
| 3 | 11 处调用点改走 `ctx.raw_lines("…")`；5 + 4 个解析器从"收 `Path`"改成"收 `list[str]`" | 两库 `presentation.py` |

★ **"收行"而不是"收路径"是有意的**：解析器拿到 `Path` 会直接 `TypeError`（`Path` 不可迭代），
所以"必须走 `ctx.raw_lines`"这个口径**在运行期也成立**，不只写在注释里。

### ★ 三条最容易做错的地方

1. **PG 还有同一模式 9 处**（`plugins/postgresql/presentation.py:111/119/173/175/176/177/560/563/567`），
   本批**未纳入**（守门只覆盖 Oracle + MySQL）。纳入会当场红 —— 那是**待办**，不是缺陷。
2. **`raw_lines` 只读非表格式文件**。表格式数据（`tables/*.psv`）走 `ctx.tables`；两套语义不能混
   （PSV 用 `|`、key-value TSV 用 TAB），混了就会把整行塞进"唯一一个键"下。
3. **`ctx.root` 本身没被禁** —— 禁的是"用 `ctx.root` 去拼**非表格式**文件在包内的路径"。
   守门只看 `BinOp(Div)` 的**左操作数**是不是 `root`，避免误伤数值除法。

### 等价性证明（同一 1.4 包，改造前 vs 改造后）

判据不是"测试绿"，而是**产物逐项相同**（重跑 `plugins/oracle/cli.py` + `generate_report.py`）：

| 产物 | 结果 |
|---|---|
| 8 张 PNG | **逐字节相同** |
| `llm_input.json` | **完全相同** |
| `analysis.json` / `analyzer_status.json` / `report_model.json` / `report_model_standard.json` | 抹掉 `generated_at` / `duration_ms` / 图表 `path` 等运行时字段后**完全相同，实质差异 0** |
| `oracle_professional_report.docx` | `word/document.xml` 抹掉时间戳后**完全相同** |

⇒ **Oracle 基线一个字都不用改**（本批对报告产物是严格无操作）。

- **跨包形态复核（2.0 包 `…_2026-10-10_14-30-48`）**：8 张 PNG 逐字节相同；`analyzer_status.json` /
  `llm_input.json` 完全相同；`analysis.json` / `report_model.json` 只有 **2 处**差异 —— 都是**批 13**
  顺手修的 `_analysis()` 透出规则引擎 `reason`（`无 FRA 记录` / `无备库统计（未配置 DataGuard）`），
  **与取数路径无关**（`_b13_20` 是那次修改**之前**的产物）。且 2.0 包里 4 类原始文件的内容
  **全部正常渲染**（关键内核参数 14 行 / 挂载参数 39 行 / 内核错误摘要 4 行 / 时间与时区 2 行）
  ⇒ `raw_lines` 在 2.0 的目录布局下**同样命中**。

### 新增守门与负控制

- `tests/test_raw_file_access.py`（**17 用例**）：① 访问点行为（两处都能命中 / 缺文件返回空 /
  目录顺序是契约 / 目录顺序单一来源）；② **继承而非覆写**（子类改了 `raw_lines` ⇒ 两份口径，当场红）；
  ③ **结构**：两库 `presentation.py` 里零 `ctx.root / …` 拼接；④ **契约文件名集合**
  （改名会静默返回空）—— 从源码 AST 取 `.raw_lines("…")` 的实参比对。
- ★ 每条结构断言都配**尺子自证**（把一段"故意违规"的源码喂给**同一个函数**，必须抓到）。
  没有它，结构性断言可能是"永远绿"的空断言（批 13 的教训）。
- 负控制 `_work/_b14_negctl.py` **4/4 全部真实变红**（`_RAW_FILE_DIRS` 去掉 `tables/` /
  重新拼 `root / …` / 子类覆写 `raw_lines` / 契约文件名改名），`finally` 逐字节复原 + sha256 校验，
  复原后 **17 passed**。

### 回归

全量 `pytest` = **5 failed / 370 passed / 168 subtests**（5 个 FAIL 全是 MySQL 源包缺失的长期问题，**无新增**）。
★ **MySQL 侧没有端到端覆盖**（`test_mysql_*` 因源包不在仓库而失败）—— 本批为该侧补了直接打解析器的
用例（"收行不收路径"），但**真实 MySQL 包上的端到端验证仍未做**。

### 留给下一批

| 批次 | 内容 |
|---|---|
| **批 14b（候选）** | 把同一收口推到 **PostgreSQL**（`presentation.py` 9 处）—— 共享访问点已就位，是纯机械替换 |

---

## 批 12b：图表补口 —— 「包内有数据、报告里没有图」的三张图（Python 侧）✅ 已完成（2026-10-12）

**范围**：只动 Python（`inspection_core/charts/render.py` + `plugins/oracle/{charts,word_report,report_adapter}.py`
+ 测试 + 基线），`inspection/oracle_inspection.sh` **零改动**（数据早就采到了，缺的是展示）。

### 病与判据

批 12 把「图的**声明集合** = **可产出集合**」钉死之后，还剩一类缺口没覆盖：
**包里有数据、报告里却连图都没有**。逐项扫「`status=ok` / 有行 **且** 展示层无消费者」，得到三张：

| 图 | 数据源 | 包内形态 | 图型 |
|---|---|---|---|
| 表空间使用率 Top-N | `oracle.tablespace.usage`（`TABLESPACE_NAME` / `PCT_USED`） | 1.4 中文标题 TSV / 2.0 `.psv` | **条形** |
| Top SQL 执行时间 Top-N | `oracle.topsql.elapsed_time`（`SQL_ID` / `ELAPSED_MIN`） | 同上 | **条形** |
| 每日归档量趋势 | `oracle.archive.daily_30d`（`day` / `day_gb`） | 同上 | 折线（可复用） |

★ **公共层此前只有时序折线**（`render_spec`：x = 时间戳、`spec_has_data` 要求 ≥2 点），
三张里两张要**类别轴**（x = 表空间名 / SQL_ID，一个类别就该能画）。

### 三条机制性改动

1. **条形渲染入口下沉公共层**：`inspection_core/charts/render.py` 新增 `render_bar_spec()`
   （+ `bar_spec_has_data()`），matplotlib 优先、Pillow 降级 —— **纯新增**，
   不动 `render_spec` / `spec_has_data` ⇒ 四库基线与四张 OS 图零影响（实测原有 8 张 PNG **逐字节不变**）。
   ★ **为什么不塞进同一个 spec**：折线要求 ≥2 点、条形一个类别即可，塞一起会让判据对条形失真。
   图**定义**仍在 `plugins/oracle/charts.py`（`charts/__init__.py` 明确「表空间等 database-specific chart 归插件」）。
2. **新图并入单一来源**：3 个 id（`oracle_tablespace_usage` / `oracle_topsql_elapsed` / `oracle_archive_daily`）
   加进 `ORACLE_CHART_IDS`；声明按**章节**挂（表空间→`storage_capacity`、Top SQL→`oracle_performance`、
   归档→`backup_recovery`）⇒ 声明不再等于单一章节，守门 `test_oracle_chart_coverage.py` 改为钉
   「**跨章节声明的并集 == `ORACLE_CHART_IDS`**（不重不漏）」。
3. ★★ **顺带根治两处「适配器重建模型」的静默坑**（本批踩出来的）：
   - **图题**原在 `word_report.py` 与 `report_adapter.py` **各抄一份** ⇒ 新图在适配器侧
     `_CHART_CAPTIONS.get(id, id)` **静默落回英文 chart_id**（报告里图题变成 `oracle_tablespace_usage`）。
     现收口到 `charts.py`（`ORACLE_CHART_CAPTIONS` / `OS_CHART_CAPTIONS`，与 id 同源），两处都从它取。
   - **报告来源词汇**只有 `historical` / `realtime_snapshot` ⇒ 表数据图被兜底成 `realtime_snapshot`，
     而 `word_engine` 会对含 `realtime_snapshot` 图的章节加「短时采样」说明 ⇒
     **只有排行图的 storage / backup 章会被误加"现场短时采样"提示**。现新增 `table_snapshot` 词汇。

### 验证

- **回归**：全量 `pytest` = **5 failed / 381 passed / 168 subtests**（370 + 新增 11；5 个 FAIL 仍是
  MySQL 源包缺失的长期问题，**无新增**）。
- **实测两份真实包**：
  - 2.0 包：新增 `oracle_tablespace_usage.png`（5 类，SYSTEM 99.98% 标红）+ `oracle_topsql_elapsed.png`（10 类）；
    **归档图按数据空 `skipped`**（该实例近 30 天无归档），备份章出现「图表未生成：数据点不足」说明。
  - 1.4 包：同样两张出新图（Oracle 专属时序图还多出一张 `oracle_physical_io`）；**列对齐文本解析正常**。
- ★ **原有 8 张 PNG 逐字节相同**（`_work/_b13_14/charts` vs `_work/_b12b_14/charts`）；`expected_summary` 里
  **只有 `charts` 从 8 → 10**，其余（health 23 / rules 42 / triggered 11 / passed 24 / not_evaluated 7 /
  risk_register 11 / items 76 / sections 9 / severity_counts）**全同** ⇒ 纯展示层、不触发新判据。
- **守门**：`tests/test_oracle_chart_coverage.py`（9 用例）+ 新增 `tests/test_chart_bar_spec.py`（9 用例，
  公共层 bar：判据 / 缺值丢弃 / 阈值着色 / matplotlib 与 Pillow 双后端 / 无渲染器不静默成功）。
- **负控制** `_work/_b12b_negctl.py` **4/4 全部真实变红**（`ORACLE_CHART_IDS` 少登记 / `storage_capacity` 组清空 /
  `_bar_pairs` 把缺值当 0 / `bar_spec_has_data` 不看值），逐字节复原 + sha256 校验。
  ★ **负控制抓到一条测试自身的漏洞**：初版「单一来源」断言写的是
  `if item.chart_id in ORACLE_CHART_IDS` —— 这个过滤会把「声明了但没登记」的图**静默滤掉**，
  于是变异①照常绿。改为「**排除 OS 图后取全量**再比对」才把不一致暴露出来
  （延续批 13「断言形式与被测对象不匹配时绿色不代表正确」）。

### 留给下一批

| 批次 | 内容 |
|---|---|
| **批 14b（候选）** | 把取数收口推到 **PostgreSQL**（`presentation.py` 9 处）—— 共享访问点已就位，纯机械替换 |
| **Top SQL 另两个维度** | 包内还有 `oracle.topsql.logical_reads` / `physical_reads`（各 20 行），本批只画了「执行时间」一张；要再出图时按同一 `render_bar_spec` 追加即可 |

---

## 批 15：报告质量修复 —— 拿真实报告逐条核对（Python 侧）✅ 已完成（2026-10-12）

**范围**：只动 Python（`plugins/oracle/{oracle_rules,package_adapter,presentation,charts,report_adapter}.py`
+ 共享引擎 `inspection_core/word_engine.py` + 测试 + 基线），`inspection/oracle_inspection.sh` **零改动**
（数据早就采到了，错的是**读法 / 口径 / 文案**）。

### 病与判据

本批起点**不是读代码猜**，是用户拿生成的 `192.168.100.80_db01_Oracle数据库巡检报告.docx` **逐条挑错**，
再拿一份**同客户的 MySQL 报告**（`192.168.1.20-db02-MySQL数据库巡检报告.docx`，脱敏名）当参照系比对。
六处全是**客户一眼能看出不对**的：

| # | 现象（报告里原样长这样） | 真因 |
|---|---|---|
| P0-1 | 「文件系统使用率过高 最高文件系统使用率：**100.0%**」 | 规则层遍历**全部** `df` 行取最大值 —— 光驱 `/dev/sr0 iso9660` 是**只读介质**，恒定 100% 满且 0 可用；真值（`/`）只有 **33%**。`tmpfs`/`devtmpfs`/`proc`/`sysfs` 更**没有容量概念** |
| P0-2 | 图 6-1「表空间使用率排行」SYSTEM **99.98%**（标红），表 6.1 里同一表空间 **3.39%** | 图取 `PCT_USED`（分母 = **已分配** `ALLOC_MB`），表与规则层取 `USED_MB ÷ MAX_MB`（分母 = **最大可扩**）。**同一份报告、同一个对象、两个数**，客户只会先信更吓人的那个 |
| P0-3 | 4.5「文件系统使用情况」9 行表，除使用率外**三列全空** | 展示层读 `MOUNTPOINT`/`FILESYSTEM`/`AVAILABLE`，而 `parse_df_pt` 产出的是 `Mounted on`/`Filesystem`/`Available` —— **只有 `USE%` 恰好撞对** |
| P1-2 | 章末「依据与建议」列 **66 处**「未提供补充依据」 | `RuleEvaluation` 只有**触发**才把 `facts` 存进 `Finding`，**通过**项的证据被丢弃 |
| P1-3 | 7.1 性能表**每行都缺两个格子**、填成「未采集」 | 7.1 只画「平均值 + 最大值」的**速率**行，却把 Buffer Cache 命中率 / 磁盘排序**比例**行混进同一张表；渲染层按行的**键并集**出列 |
| P1-4 | 规则判定项（"CPU 压力评估""无效对象""物理坏块"…）正文只有「本项没有可展示的结构化记录。」 | 这些项**本就没有明细表**，其全部产出就是一条判定结论。落回通用空数据文案 ⇒ 读者把「这是规则项」读成「采集失败 / 数据缺失」 |

★ 三处**同源**病灶一并收口：
① 3.8「挂载参数」把 39 行 `mount` 全列出来（proc/tmpfs/cgroup/光驱），**真正的持久化挂载点只有 2 行**被淹掉
（MySQL 侧早已过滤 —— **四库只有 Oracle 漏了**）；
② `df -PT` 容量列是 **1024-blocks（KB）裸数字**直接印进"可用空间"；
③ 表 6.1「已使用(MB)」用 `if used_val` 判空 ⇒ 真 0 被当成空白格（与「未采集 ≠ 0」相悖）。

### 六条机制性改动

1. **P0-1 伪文件系统不进容量判据**（`oracle_rules._check_filesystem_usage`）：
   过滤判据用**公共层** `inspection_core.system_checks.is_persistent_fstype`（**MySQL 早就在用**），
   键名归一用新增的 `package_adapter.filesystem_type(row)`（**与产生键名的 `parse_df_pt` 同一文件**）。
   ★ **不在规则层再写一份黑名单** —— 规则层写一份、展示层写一份，两边迟早分叉。
   口径串里带上「**已排除 N 个 tmpfs/devtmpfs/光驱等非持久化挂载点**」，让客户**能自证**
   （否则看到"最高 33%"会问"我 `df` 有 9 行，凭什么只算这个"）。
2. **P0-2 表空间图改口径**（`charts._tablespace_chart`）：图 × 表 6.1 × 规则层**三处同一算式**
   `USED_MB ÷ MAX_MB`；`MAX_MB` 缺失（老包）才退回 `PCT_USED`。图题改「表空间**容量**使用率」、
   x 轴标签写明「已用率（已使用 ÷ 最大可扩，%）」—— 口径写进图里，读者不用翻源码。
   图片内标题与 **docx 图题**同出 `charts.ORACLE_CHART_CAPTIONS`（单源），一并改为「表空间容量使用率排行」
   —— 此前图内标题已带口径、图题没带，读者得对照两处才知道分母是最大可扩。
3. **P0-3 4.5 键名对齐**（`presentation`）：读 `Mounted on`/`Filesystem`/`Available`（旧拼法留作兜底），
   补「类型」列，新增 `_kb_human()` 把 KB 换算成 `64.30 GB`。同一处还加了 **`elif fs_rows:` 分支**：
   **全被过滤掉也要出项**（写 `not_applicable` + 一句说明），让"整机只有 tmpfs/光驱"与"这章漏采了"
   在报告里**长得不一样** —— 否则一项突然消失，读者只会当成少了一节内容。
4. **P1-2 规则"通过"也带实测值**：`RuleEvaluation` 新增 `facts: list[str]`（`to_dict` 一并输出），
   `_evaluate` 无论触发与否都留证据；`presentation._analysis` 的 `passed` 分支把它透出。
   同时把**「查了、确认没有」与「没采到」在章末小结里分开** —— `report_adapter._normalize_analysis`
   新增 `collection_status == "empty"` 分支（结论「已检查，本次未发现相关记录。」+ 依据
   「已按本项口径查询，本次无匹配记录。」），不再与 `not_evaluated` 一起压成「未采集到结构化记录」
   （**同一件事两种说法，读者只会认为报告自相矛盾**）。事实项补依据：展示层 `note` 优先
   （如「已排除 7 个非持久化挂载点」），否则用结构化记录条数。
5. **P1-3 一张表只留一种行形**（`presentation`）：7.1 只留速率行（平均值 + 最大值），
   Buffer Cache 命中率 / 磁盘排序比例落到**各自的规则项**做单值行（`_ratio_row()`）。
6. **P1-4 规则项正文给结论**（**共享引擎** `inspection_core/word_engine.py::render_inspection_item`）：
   空数据分支先看 `item.analysis.conclusion` —— 有结论且当前布局**不内联分析**（professional 会把
   完整分析后置到章末小结）时，正文落 `note_box("分析结论", …)`；legacy 布局紧随其后就渲染完整
   分析块，正文**不再重复**。★ 这条**四库共用**（MySQL/PG/SQL Server 同样病灶），
   数据齐全时**零字节影响** —— 由控制变量自证钉住（见下）。

### 验证

- **回归**：全量 `pytest` = **5 failed / 401 passed / 168 subtests**（5 个 FAIL 仍是 `tests/test_mysql_*`
  的 **MySQL 源包缺失**长期问题，**无新增**；上一批 370 passed → 本批 401，含新增 17 用例与实际差异）。
- **实测两份真实包**（用 `cli.py` → `generate_report.py` 端点跑再读回 DOCX 文本）：
  - 4.5 由**空表**变 **2 行真数据**（`/` **33%**、可用 **64.30 GB**）+ 「已排除 …」脚注；
  - 3.8 挂载参数由 **39 行 → 2 行** + 「已排除 37 个 …」脚注；
  - 「未提供补充依据」**66 → 4**；「本项没有可展示的结构化记录。」**22 → 0**；
    「已检查，本次未发现相关记录。」**9 处**（「查了没有」与「没采到」自此分开）；
  - 图 6-1 SYSTEM 由 **99.98% → 3.39%**，与表 6.1 一致。
- ★ **控制变量自证**（`_work/_b15_ctrl_hash.py`）：把引擎那一段**逐字改回旧形态**、`report_model` **一字不动**
  重渲染，MySQL legacy 文档 hash **精确回到冻结值 `D34B7A1B…`**；新形态 = `CB8653E5…`。
  ★ **两次渲染必须在独立子进程里跑** —— `word_engine` 一旦被 import 就驻留内存，同进程改文件无效
  （第一次复现就踩了：两次 hash 一模一样）。`finally` 逐字节复原 + sha256 断言。
- **负控制** `_work/_b15_negctl.py` **8/8 全部真实变红**（规则层不再过滤 / 展示层改回旧键名 /
  图表改回 `PCT_USED` / `RuleEvaluation` 不透出 facts / 比例行塞回速率表 / 引擎回落通用占位 /
  把「查了没有」压回 `not_evaluated` / 事实项不补依据），逐字节复原 + sha256 校验。
- **基线**（`_work/_b15_refresh_baseline.py`）：`expected_summary` **逐项不变**
  （`health 23` / `rules 42` / `triggered 11` / `passed 24` / `not_evaluated 7` / `risk_register 11` /
  `items 76` / `sections 9` / `severity_counts`）—— 「口径 / 文案修复、**不触发新判据**」的强证据；
  10 张 PNG 里**只有** `oracle_tablespace_usage.png` 变了（正是 P0-2），其余 9 张**逐字节不变**。
  `source_implementation.*` **刻意不重写**。
- **守门测试**：新增 `tests/test_oracle_report_quality.py` **15 用例 / 7 个测试类**
  （`FilesystemRuleTests` / `MountRowsTests` / `FilesystemDisplayTests` / `TablespaceChartTests` /
  `RuleEvidenceTests` / `PerformanceTableShapeTests` / `EmptyStateAdapterTests`，用合成 `FakeContext` +
  monkeypatch 抓 `render_bar_spec` 的 spec）；`tests/test_word_engine.py` 新增 `RowsLessRuleItemTests`
  **2 用例**（professional 正文给「分析结论」、legacy 不重复）并**更新控制变量 hash 冻结值**；
  `tests/test_oracle_plugin.py::test_missing_evidence_is_not_normalized_to_passed` 期望值随
  「empty / not_evaluated 分叉」同步（`normal` **49 → 57** / `not_evaluated` **17 → 9**）。

### 踩到的坑

1. ★ **`_mount_rows` 改签名会打穿"四库解析器同形"的守门**。为了拿到"排除数"，初版把
   `_mount_rows` 改成返回 `(rows, excluded)` ⇒ `tests/test_raw_file_access.py` 的
   `assertEqual(_mount_rows([...]), 1)` 立刻变 `2 != 1`。修法：**保留 `_mount_rows` 只回 `list`**
   （与 MySQL 侧同名函数同形），排除数由新的 `_persistent_mounts` 提供 —— **要加返回值时先想
   "这个函数名有没有被跨库同形守门盯着"**。
2. ★ **`if x` 与「未采集 ≠ 0」**：表 6.1 的 `if alloc_val` / `if used_val` 把**真 0** 也吞成空白。
   判空一律用 `is not None`。
3. **`word_engine` 的模块缓存**（见上「控制变量自证」）—— 子进程隔离是这类自证的前提。

### 有意未做（留给下一批）

| 批次 | 内容 |
|---|---|
| **`control_files` 折行** | 5.1 里 `control_files` 显示成两行（`…/control01.ctl, /u01/…/control` / `- 02.ctl`）—— 那是 **SQL\*Plus 按 80 列宽折行**，不是真有两个文件。**本批未修**（属**取数层**：要么调 `SET LINESIZE`/`COLSEP`，要么在展示层按 `ctl` 后缀重接续行）。★ 先确认"它到底是采集时折的行、还是展示时断的"再动手 |
| **批 14b（候选）** | 把取数收口推到 **PostgreSQL**（`presentation.py` 9 处）—— 共享访问点已就位，纯机械替换 |
| **Top SQL 另两个维度** | 包内还有 `oracle.topsql.logical_reads` / `physical_reads`（各 20 行），本批只画了「执行时间」一张；要再出图时按同一 `render_bar_spec` 追加即可 |

---

## 批 16：`control_files` 折行链 —— 「一行一记录」契约（采集层 + 解析层 + 展示层）✅ 已完成（2026-10-12）

**范围**：`inspection/oracle_inspection.sh`（列宽声明 / 一个分隔符）+ `plugins/oracle/package_adapter.py`（解析层）
+ `plugins/oracle/presentation.py`（展示层）+ 守门 `tools/check_oracle_psv_widths.py` + 测试 + 基线。
本章把批 15「有意未做」表里的第一条（`control_files` 折行）做掉 —— 它横跨**三层**，
是「报了个看起来像数据问题的现象、修完却看不出变化」的典型。

### 病（用户原话）

> 5.1 control_files 折行 —— 那是 SQL\*Plus 按 80 列宽断的，不是真有两个文件；属取数层，
> 动手前得先确认"是采集时折的还是展示时断的"。

报告里长这样：

```
control_files | /u01/app/oracle/oradata/ZYDB/control01.ctl, /u01/app/oracle/oradata/ZYDB/control
```

读起来像**两个控制文件、第二个叫 `control`**。

### 先取证再动手：「采集时折的」还是「展示时断的」？

拆真实包字节（`tables/oracle.basic.parameters.psv`，10-10 包）：

```
[4] 94  'control_files|/u01/app/oracle/oradata/ZYDB/control01.ctl, /u01/app/oracle/oradata/ZYDB/control'
[5]  7  '|02.ctl'
```

第 4 行停在 `…/control`、第 5 行是残段 `|02.ctl` ⇒ **采集时就断了**，包内已经是两行
（`COLUMN value FORMAT a80` 折的）。结论决定动手位置：**取数层为主、解析层兜存量**。

### 根因四层

| 层 | 病灶 |
|---|---|
| ① 采集 | SQL 是 `SUBSTR(value, 1, 400) value`，而 `COLUMN value FORMAT a80` ⇒ 差值 320 字符被 sqlplus 折行 |
| ② 采集 | 3 处 `WORD_WRAPPED` 是**按词**折行，残段无法用「无分隔符续接」还原（会粘字） |
| ③ 采集 | `LISTAGG(name, ' \| ')` 的分隔符与 `SET COLSEP '\|'` 撞；2.0 的 `psv_normalize` 还会把 ` \| ` 压成 `\|`（1.4 不压空白，所以一直没暴露） |
| ④ 展示 | 值列写死 `[:80]`，**正好也切在第 80 个字符** ⇒ ①②③ 全修好，报告里**一个字符都不变** |

★ 第 ④ 层是本批最贵的发现：修完 ①②③、重出报告、肉眼核对 —— **看起来完全没修**。
两处截断都落在第 80 个字符上，截图与改前一致。
**「改完看不出变化」要先怀疑还有第二处同形截断，别急着怀疑自己没生效**；判"修没修好"**看数据、不看截图**。

### 四处改动

1. **采集层：列宽按「≥ SQL 截断长度」重设，并撤掉按词折行**（`ora_sql_to_md` 的 `COLUMN` 声明区）
   —— `value`/`VALUE` `a80→a500`（SQL 是 `SUBSTR(value,1,400)`）、`description`/`DESCRIPTION` `a200`、
   `message`/`MESSAGE` `a500`、`message_text` `a500`、`finding_name` 去掉 `WORD_WRAPPED`，
   新增 `COLUMN additional_info FORMAT a120`（SQL 侧正好截 120）。
   **`WORD_WRAPPED` 全撤（3 处）**，让"断词折行"成为唯一模式，回并才有定义。
2. **采集层：`LISTAGG` 分隔符改 `'; '`**（`oracle.validation.controlfile_multiplex`）—— 别再与 COLSEP 撞。
3. **解析层：`_rejoin_wrapped_rows` 兜底存量包**（`package_adapter`）—— 判据是**结构化**的：
   2.0 会话设了 `SET NULL '-'` ⇒ 空单元格根本不存在 ⇒ 「首字段为空」只能是折行残段。
   ★ 见下节，这个前提**只对 2.0 成立**。
4. **展示层两处**：`5.1` 值列去掉 `[:80]`（这是让前三条「看得见」的那一刀）；
   `11.8 控制文件冗余` 的键名对齐 —— 旧代码读 `NAME`/`VALUE`，而该表的真实列是
   `CONTROLFILE_COUNT`/`CONTROLFILE_PATHS` ⇒ 整表两个格子恒空（与批 15 P0-3 同家族：
   **展示层读的键名必须与解析层产出一致**）。

### ★★ 一个真实踩过的坑：前提写错，就会把数据并进表头

第一版把回并写成「**分隔符是 `|`** 就回并」（1.4 的 `.tsv` 与 2.0 的 `.psv` 都是 `|`）。
刷 08-10 基线时立刻暴露：

| 表 | 回并前 | 回并后 | 后果 |
|---|---|---|---|
| `控制文件.tsv` | 3 行 | **1 行** | 表头 `STATUS \|NAME` 把两行数据吸进去 ⇒ 报告里 `控制文件路径` **由 2 行塌成 0 行**（`csv.DictReader` 不再有数据行） |
| `控制文件序列号.tsv` | 3 行 | 1 行 | 同上 |
| `最近 SQL 错误 (近24h).tsv` | 15 行 | 14 行 | 把相邻两条告警并成一条 |

原因：**1.4 没有 `SET NULL '-'` 保证** —— 它的第一列空白是 **NULL 落在定宽列里**，
下面这两行是**真实数据行**，不是残段：

```
STATUS |NAME
       |/u01/app/oracle/oradata/ZYDB/control01.ctl
       |/u01/app/oracle/oradata/ZYDB/control02.ctl
```

⇒ **判据的前提是「采集器版本保证空单元格不存在」，不是「分隔符是 `|`」**。
修法：开关由 `_iter_table_specs` 按**版本**给出（1.4 关 / 2.0 开）——
全仓库**唯一**把「版本」翻译成「能不能回并」的地方；`parse_delimited(repair_wrap=…)` **默认关**
（忘了传参的代价是「折行没修」= 报告里看得见，而不是「数据被静默吃掉」）。

### 机械守门：`tools/check_oracle_psv_widths.py`（四类判据）

| # | 判据 | 动机 |
|---|---|---|
| ① | `SUBSTR(…,1,N) <别名>` 必须有 `COLUMN <别名> FORMAT aM` 且 `M ≥ N` | 本批主病；未声明也算违规（宽度交由 sqlplus 从表达式推导、不可控） |
| ② | SQL 字面量里不许含 `\|` | COLSEP 是 `\|`；**用引号状态机**取字面量，不能用朴素正则 |
| ③ | 不许 `COLUMN … WORD_WRAPPED` | 按词折行的残段无法回并 |
| ④ | **同名别名宽度必须唯一** | ★ **负控制实测抓出来的漏洞**（见下） |

★ 判①②两处**误报**是修出来的，并各配了一条「误伤」用例钉住：

- 朴素正则 `'[^']*\|[^']*'` 会从**前一个字面量的收尾引号**匹配到**后一个字面量的起始引号**
  （命中 `' ELSE x \|\| '`）⇒ 6 处假阳性；改成**引号状态机**（含 `''` 转义）。
- 注释里提到 `WORD_WRAPPED` 不算违规 ⇒ 判③ 锚定 `^\s*COLUMN\b`。

★ 判④ 的来历：负控制里「把采集层 `COLUMN value FORMAT a500` 改回 `a80`」**仍然全绿**。
查因：判① 的宽度表按「同名取**最大值**」，而脚本里 `value`/`VALUE` **各声明一条**
（sqlplus 列名大小写不敏感，两类都写是常规做法）⇒ `max(500, 80) = 500`，改窄被抹平。
补上「同名宽度必须唯一」后才真实变红。**全脚本 45 条声明 / 26 个去重别名，零冲突 ⇒ 这条零误报。**
—— 这正是「绿测试不等于有效测试」的又一次现场复现。

### 验证

- 新增 `tests/test_oracle_psv_wrapping.py`（**26 用例 / 4 类**：尺子自证 12 / 回并用例 8 / 端到端 4 / 版本开关 2）
  + `tests/test_oracle_report_quality.py::InitParameterValueDisplayTests`（展示层不截断）。
- **尺子自证**：把故意违规的源码喂给**同一个** `check_script`，必须抓到（含两条误伤反例）。
- **负控制** `_work/_b16_negctl.py` **13/13 真实变红**（批 15 的 8 条一并复跑，确认老守门没被本批改动带坏），
  逐字节复原 + sha256 校验。
- **影响面实测**：10-10 包 103 张表里回并只动 **1 张 1 行**（正是 `parameters.psv` 的 `|02.ctl`）；
  08-10 包 98 张表里 2.0 语义下同样只动 1 张。
- **基线**：`expected_summary` **逐项不变**（health 23 / rules 42 / triggered 11 / passed 24 /
  not_evaluated 7 / risk_register 11 / items 76 / sections 9），10 张 PNG **零差异**，
  `llm_input.json` **零差异**；新旧产物逐项 diff 后**只剩两处** —— `control_files` 值 80→86 字符、
  11.8 两个空格子变成 `控制文件数 2` / `路径 …`。
- **实测（10-10 包）**：5.1 的 `control_files` 在 docx 单元格里是完整 86 字符（`…/control02.ctl`）；
  11.8 由空表 → `控制文件数 2` / `路径 /u01/…/control01.ctl`；业务指标不变（items 77 / findings 12 / charts 9 / score 20）。
- 回归：全量 `pytest` = **5 failed / 428 passed / 168 subtests**（5 个 FAIL 仍是 `tests/test_mysql_*`
  源包不在仓库），**无新增失败**。

### 有意未做

| 批次 | 内容 |
|---|---|
| **批 14b（候选）** | 把取数收口推到 **PostgreSQL**（`plugins/postgresql/presentation.py` 9 处）—— 共享访问点已就位，纯机械替换 |
| **Top SQL 另两个维度** | 包内还有 `oracle.topsql.logical_reads` / `physical_reads`（各 20 行），按同一 `render_bar_spec` 追加即可 |
| **`最近 SQL 错误` 的 60 字符截断** | 展示层 `MESSAGE_TEXT[:60]`。本批未动：它是**告警消息预览**（不是路径/标识），且截断后另有 `PROBLEM_KEY` 兜底；要改应连"加省略号"一起做 |

---

## 批 17：空值语义 —— 「没采到」不许长得像「空」（Python 侧）✅ 已完成（2026-10-12）

**范围**：`plugins/oracle/presentation.py`（展示层两处）+ 测试 + 基线。**采集脚本一字未改。**

起因是用户拿着报告问的三句话：

> 为什么很多都没有值，是没采集到吗？不应该。还是分析的问题？很多表格的说明都是空的？
> 不能填充？还是不能给建议，分析层？…… 这报告问题很多，很多都没展示？

### 先定位：用户看的是哪一份报告

用户当时打开的是 `tests/baselines/oracle/current/oracle_professional_report.docx` —— 它是
**回归基线样本**，源包是 **08-10 的 1.4 采集包**（`find` 全包：98 张 `.tsv` 全是 Oracle 内部视图，
**没有** `lscpu` / `kernel_parameters.tsv` / `time_status.txt`）。用户贴的 `SGA / Database Buffers 1200 MB`
正是 1.4 报告的值（2.0 包是 1168 MB），据此确认。

**结论：不是分析层丢的，是 1.4 采集器根本不采操作系统层。** 同一台机器 10-10 的 2.0 包出的报告里，
`操作系统` / `CPU 型号` / `CPU 核数` / `NUMA 节点` / `本地时间` / `时区` / `关键内核参数` **全部有值**。

### 但报告里确实有两处真缺陷（本批做掉）

**① `关键内核参数` 的首行 `path | value` 是表头被当参数渲染。**

- **病根是「契约变更后消费端没跟上」**：采集侧 N-20（批 7）给 `tables/kernel_parameters.tsv` 补了表头
  `path<TAB>value`（理由：`tables/*.tsv` 都是真 TSV、首行即列名行），**但 `_key_value_rows` 仍把首行当参数收下**
  ⇒ 报告里多出一行「`path | value`」，读起来像一个真内核参数。
- MySQL / PG 的采集器**不写表头**（同一个 `printf 'path\tvalue\n'` 块只喂 `hugepages.tsv`），
  所以只有 Oracle 暴露 —— 这也解释了用户"MySQL 都有的"的直觉：**不是 Oracle 少采了，是 Oracle 多渲染了一行。**
- **修在消费端**（首行表头跳过一次），**采集层契约不动**；只对**首条已解析行**判定，避免误伤真值恰为 `value` 的参数。

**② 没采到的字段渲染成 `-`，与「字段为空」长得一样。**

- **引擎本身没错**：`inspection_core/word_engine.text()` 里 `None → 未采集`、`"" → -`，
  这个区分是**有意**的（见「未采集 ≠ 0」老判据）。错在调用点写满了 `env.get("x", "")` /
  `lscpu.get("y", "")` —— **`""` 把「没有」伪装成了「空」**。
- 1.4 包上于是 `主机信息` 4 行、`时间与时区` 2 行全是看不出所以然的 `-`。
  改为 `or None` 后读作 `未采集`。**MySQL 侧本来就是 None 语义 ⇒ 本次是四库对齐。**
- 判据：**「没采到」与「字段为空」是两个相反结论，不许长得一样** —— 与「空数据项不许消失」同源。

### ★ 为什么值得单独立批

这两处都属**静默**：不报错、不抛异常、测试不红，报告照出，只是读起来要么像真数据、要么像字段为空。
与批 15/16 同一类 —— **呈现层的错不产生功能性失败信号**，只能靠「把渲染结果拉出来对账」发现。
批 17 的发现方式正是「用户贴报告 → 逐表 dump DOCX → 与 `analysis.json` 对账」。

### 改动

| 文件 | 改动 |
|---|---|
| `plugins/oracle/presentation.py` | 新增 `_KV_HEADER_VALUES` + `_key_value_rows` 跳过首行表头 |
| `plugins/oracle/presentation.py` | `主机信息` / `时间与时区` 的 `…, ""` 兜底改成 `or None`，并补上「说明」口径 |

### 验证

- 新增 `tests/test_oracle_report_quality.py::KernelParameterHeaderTests`（3 条：表头不落表 /
  **无表头形态不受影响**（防误伤）/ **只有首行能是表头**（防误伤））
  + `MissingEvidenceValueTests`（3 条：未采集字段必须是 `未采集` / 时间项同理 /
  **有采集时真值不许被吞**（防误伤））。
- **负控制** `_work/_b17_negctl.py` **1/1 真实变红**（逐字节复原 + sha256）。
- 基线 `expected_summary` **逐项不变**（健康分 23 / 规则 42（11/24/7）/ 风险 11 / 章节 9 / 检查项 76 / 图 10），
  10 张 PNG **零差异**，`llm_input.json` **零差异**；新旧产物逐项 diff 后**只有本批这两处**（`analysis.json` 9 处）。
- 回归：全量 `pytest` = **5 failed / 434 passed / 168 subtests**（5 个 FAIL 仍是 `tests/test_mysql_*`
  源包不在仓库），**无新增失败**。
- 实测（1.4 包重出）：`主机信息` 4 行由 `-` → `未采集`；`时间与时区` 2 行同理。
  实测（2.0 包重出）：`关键内核参数` 由 14 行（含假行 `path|value`）→ **13 行真参数**。

### 有意未做（→ 待决策）

| 项 | 现状 | 影响面 |
|---|---|---|
| **`说明` 列大面积为空** | Oracle 侧 4 张表 30 行里 **25 行为空**（`数据库配置概要` 15/15 全空）。这是**设计默认**（`_val_row(name, value)` 第三参缺省），**不是不能填** —— MySQL 侧是填的，填的是**口径/释义**（"每秒查询数""现场短时采样"），不是建议 | 改 `presentation.py` 的构造点，属文案层，需先定「说明」列定位（口径 vs 建议） |
| **`主机信息` 半采集仍判「正常」** | 该项目 6 行是 `_val_row` **合成行** ⇒ 源数据缺一半也照样"有 6 条结构化记录"，适配器据此判 `正常`、依据写「本项为事实展示，共 6 条结构化记录」 | 需让「合成行」按**源证据是否到位**给结论，属适配器/展示层的口径改动 |

---

## 0.5 实测复核：真实采集包逐条验证（2026-10-09）

**复核对象**：`oracle_inspection_task_ZYDB_db01_192.168.100.80_1521_zydb1_2026-10-09_22-47-04.tar.gz`  
（211 文件 / 438 KB / `collector_version` = **1.4**，与 `inspection/oracle_inspection.sh` 工作副本一致）。  
完整实测基线见 `oracle-collector-interface.md` §11；本节只记录**改动了本文结论**的部分。

### 0.5.1 原 C 档条目：8 条全部落定，其中 2 条需修正口径（OF-07 / OF-26）

| 条目                                | 实测结论                                                                                                                                                                                                                    | 处理                                                             |
| --------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------- |
| OF-05 扫描拦截仍记 ok                   | 真实包**未触发**该路径（无 `security_scan=error`）→ 批 3 改用 **stub 构造**三分支实测（见 §0.5.5）                                                                                                                                               | **已升 A 档**（批 3 已修）                                             |
| OF-07 时区证据缺失                      | ✅ 实锤：`snapshot.json` **无 `time_evidence`**；`history/sar_*.csv` 是 **UTC**（`14:32`）而 `timeseries/*.csv` 是**本地**（`22:47`）——**同一个包里两套时钟**。**但"`sadf -d` 不带 `-t`"不是 Oracle 独有**（MySQL L973 同样不带）→ 真差距只是缺 `time_evidence` 结构化 | **修正口径**                                                       |
| OF-14 缺 `--output-dir`            | ✅ 确认输出默认落临时区（`-OutputDir` 之外的路径）                                                                                                                                                                                        | ✅ **批 4 已修**（`--output-dir=DIR` / `--output-dir DIR`）          |
| OF-15 OS 采集两套并存                   | ✅ 实锤：`evidence/`(12 个文本) 与 `tables/`(6 个英文结构化表) 并存；`limits.conf` **只在 md 里，`evidence/` 无此文件**                                                                                                                           | 确认                                                             |
| OF-18 sar 缺 `-w`/coverage         | ✅ **原文准确**：`history/coverage.tsv` **存在且 6 字段完整**（status/requested_hours/first_timestamp/last_timestamp/coverage_seconds/coverage_hours），但**无 `.json` 形态**；`-w`（进程/上下文切换）确实缺 —— 与 MySQL L1020-1027 差 1 类指标               | 确认                                                             |
| OF-19 中断无 `COLLECTION_INCOMPLETE` | 真实包**正常收尾**，未触发中断路径                                                                                                                                                                                                     | ✅ **批 4 已实现**（T12 构造验证：退 130 + 落 `partial`）；**真实环境中断一次仍待现场确认** |
| OF-26 `capture_command` 删错误原文     | ⚠️ **原口径需修正**：实测 `status/system.chronyc_tracking.tsv` 第 10 列 `reason = "506 Cannot talk to daemon"` —— **错误原文保留了**。丢的只是"第 5 行之后"与"不落模块日志"                                                                               | ✅ **批 4 已修**（完整原文进 `logs/modules/<category>.log`）              |
| OF-27 snapshot 字段太薄               | ✅ 实测 **21 个字段**（`len()` 数过），确认缺 `time_evidence` / `capabilities` / `host_identity` / `sampling{}`                                                                                                                       | 确认                                                             |

### 0.5.2 工具实测（本机 Git Bash 复跑）

```
sanitize_filename "数据库基本信息"      -> ""            (item_id 变成 "sql.")
sanitize_filename "生成AWR性能报告"     -> "AWR"         (item_id 变成 "sql.AWR")
从脚本抽取 136 条 ora_sql_to_md 标题跑一遍：
  SQL 采集项总数 136   唯一 item_id 66   塌陷到 "sql." 的有 48 条
真实包 status/ 实测：sql.*.tsv 47 个（应 136+）、module.*.tsv 11 个（应 27）、
  awk -F'\t' '{print $1}' status/*.tsv | sort | uniq -d  → 有输出（塌陷）
```

### 0.5.3 由真实包新发现的契约断点（三条，全进批 1）

| 编号   | 缺陷                                                                                      | 要点                                                                                                   |
| ---- | --------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------- |
| 实测-1 | `status="empty"` 在 `ora_sql_to_md` 路径**永不触发**                                           | 判据 `grep 'no rows selected' "$err_file"` 读的是 **stderr**，而该消息实际在 **stdout** → 34 个空结果项 status 全为 `ok` |
| 实测-2 | `tables/` 用 `tsv_filename()`（保留中文）、`status/` 用 `sanitize_filename()`（吃中文）→ **两套规则永不相等** | 且 `record_status` 第 9 列对 SQL 项恒为 `report` → **两个入口之间是断的**，下游无法从 status 找到 tables 文件                  |
| 实测-3 | `module` 层只塌文件名（内容第 1 列保留中文，**可恢复**）；`sql` 层**内容一起塌**（不可恢复）                             | 两条代码路径：`run_module` 写原始中文、`ora_sql_to_md` **在源头** sanitize                                           |

**处置**：批 1 里 **OF-01 与实测-2 必须同一次改完**；**实测-1 并入 OF-02**（空结果占位与 `empty` 判定同源）。

### 0.5.4 数据口径坑（供 Python adapter 直接消费）

| 编号   | 坑                                                                                                          | 位置             |
| ---- | ---------------------------------------------------------------------------------------------------------- | -------------- |
| 口径-1 | `ora_version` 两处不一致：`snapshot`=19.16.0.0.0（来自 `version_full`）vs `tables/实例信息.tsv`=19.0.0.0.0（来自 `version`） | interface §6   |
| 口径-2 | `collection_started_at` 用 `_` 分隔、非 ISO 8601（`status` / `manifest` 是 ISO 8601）                              | interface §6.1 |

**判读前提（重要）**：该实例 `v$instance.startup_time` 距采集仅 **2 分钟** → 包内 34 个空结果**主要是"新实例无历史数据"，不是采集缺陷**。评"采集覆盖度"要扣掉这层；评"状态台账 / 文件格式"不受影响。

### 0.5.5 批 3 改造后的构造实测（2026-10-09，非真实包）

批 3 的三条（OF-05 / OF-10 / OF-17）都不依赖真实 Oracle 库，用**假 sqlplus + stub** 在本机复现：

| 项              | 实测做法                                                                          | 结果                                                                    |
| -------------- | ----------------------------------------------------------------------------- | --------------------------------------------------------------------- |
| OF-10          | 从真实脚本抽 `get_ora_conn` + `ora_exec`，假 sqlplus 记录 argv 与 stdin                  | **argv = `-S /nolog`**（不含口令）；`CONNECT scott/tiger@…` 经 **stdin** 送入 ✓ |
| OF-17（10）      | 隔离 PATH（无 sqlplus）跑真实脚本                                                       | `EXIT=10` ✓                                                           |
| OF-17（20）      | 假 sqlplus 不返回 `CONNECTED`                                                     | `EXIT=20` ✓                                                           |
| OF-17（0/30/31） | 抽 `collect_structured_package`，`security_scan` / `create_package` 用 stub 控三分支 | status/rc/FINAL：`ok/0/0`、`error/30/30`、`error/31/31` ✓                |

证据脚本：`_work/_verify_b3.sh`（OF-10）、`_work/_verify_b3_pkg.sh`（OF-05 + FINAL_EXIT_CODE）。  
**未跑完整脚本**（本机无真库；整跑约 8 分钟且被系统命令缺失拖慢）—— 完整流程验证留待现场。

---

## 0.6 脱敏边界（新增采集项时照此判断）

沿用 MySQL 侧 `FIX-PLAN.md` §0.5 的原则，Oracle 的对应判断：

| 必须脱                                                                                                                              | 依据                 | 绝不能脱                                                    |
| -------------------------------------------------------------------------------------------------------------------------------- | ------------------ | ------------------------------------------------------- |
| sqlplus 连接串里的密码（含 argv，见 OF-10）                                                                                                  | 明文凭证               | `ORA-` 错误原文（诊断价值最高）                                     |
| 含 `password=` / `--password` / `token=` / `BEGIN PRIVATE KEY` / `Authorization: Basic\|Bearer` 的行（`security_scan` 已覆盖 5 类，L3259） | 真秘密                | `db_link` 的 `HOST`、`tnsnames.ora` 的 `HOST`/`PORT`（诊断必需） |
| 数据库/PDB 名、用户名                                                                                                                    | **不进对外文档**（本机文档可留） | 表空间/数据文件路径（容量结论必需）                                      |
| ——                                                                                                                               | ——                 | `multipath -ll` 的 WWID、`oratab` 的路径、`lsnrctl` 的监听地址     |

**注意"名字里带 password 的键"不等于秘密**（MySQL §0.6 的定论）：`v$parameter` 里若出现 `*.password_...` 之类的策略参数，**删真要删的值，不要按名字删整行**。

---


## 0.7 建议的实施顺序（分批，别一次上）

|       批次       | 内容                                                                                                                                                                                                                                                                                              | 能不能上生产                                                                     |
| :------------: | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------- |
|  **批 0** ✅ 已完成 | OF-24 注释补全（零逻辑改动）+ OF-28                                                                                                                                                                                                                                                                        | 能，且应最先做（补注释过程本身就是契约自检）                                                     |
|  **批 1** ✅ 已完成 | OF-01、OF-02、OF-03、OF-04、OF-16/23、OF-13、OF-20、OF-22、OF-27，**+ 实测-1 / 实测-2 / 实测-3（§0.5.3）**，**+ OF-21 / OF-25（随批 1 前置）**                                                                                                                                                                          | **不改完不能上生产** —— 这一批全是"包里的数据和事实不符"（已随 `SNAPSHOT_SCHEMA_VERSION` → 2.0 一起落地） |
|  **批 2** ✅ 已完成 | OF-06、OF-07、OF-08、OF-11、OF-12、OF-09、OF-15、OF-18                                                                                                                                                                                                                                                 | OS 层，与批 1 无依赖，可并行（OF-06 放在最前，它是硬规则）                                        |
|  **批 3** ✅ 已完成 | OF-05、OF-10、OF-17                                                                                                                                                                                                                                                                               | 安全与门禁                                                                      |
|  **批 4** ✅ 已完成 | OF-14、OF-19、OF-26（含 tmp 泄漏）**+ 本批新发现 OF-30**（`security_scan` 记录的 findings 路径在包里不存在）—— OF-21 / OF-25 已随批 1 完成                                                                                                                                                                                    | 体验与一致性                                                                     |
|  **批 5** ✅ 已完成 | N-1、N-2、N-3、N-4、N-5（**2.0 实包体检**发现的 5 个 P1）+ **B 方案**（`report_md` → `report_md_external` + `report_md_scope`）                                                                                                                                                                                   | 数据正确性回归 + 台账单一口径                                                           |
|  **批 6** ✅ 已完成 | N-6、N-7、N-8、N-9、N-10、N-11、N-13、N-14（P2）+ N-15（文档说明）                                                                                                                                                                                                                                             | 一致性收尾（落点 / 口径 / 声明；**无新采集项**，版本号不动）                                        |
|  **批 7** ✅ 已完成 | N-16、N-17、N-18（**复采包体检**发现的 3 个 P1）+ N-19、N-20、N-21（P2）                                                                                                                                                                                                                                         | 台账口径收口（单一记账者 / 同时点同口径 / `record_status` arity 守门；**无新采集项**，版本号不动）          |
|  **批 8** ✅ 已完成 | **Python 侧**：`plugins/oracle/package_adapter.py` 按 `collector_version` 分叉 + 真正读 `.psv` + 重建 1.4→2.0 查找键桥接（含 `item_titles.json` 生成器与守门测试）                                                                                                                                                        | Python 侧对接（**采集脚本未动**）                                                     |
| **批 10** ✅ 已完成 | **Python 侧（分析/报告层对齐 MySQL）**：G1 = 补齐「待客户确认事项」（`CONFIRMATION_TEMPLATES` 11 条 + `pending_confirmations()` + analyzer 挂实例 + presentation 条件注入，第 14 章由**空转**变 5 条真事项）+ 修正章节动线（`oracle_instance` 提到 `storage_capacity` 之前）；新增 `tests/test_oracle_confirmations.py`（9 用例，含"模板 rule_id 必须存在于规则集"防漂移守门） | Python 侧（**采集脚本未动**；见 `_work/oracle-analysis-gap-review.md`）               |
| **批 11** ✅ 已完成 | **Python 侧（规则补口）**：补「**采了有数据、零判据**」的四个域 —— DataGuard 6 项（4 条规则）、对象治理（`SEGMENT_BLOAT` / `LARGE_SEGMENT`）、口令画像（`PASSWORD_PROFILE`）、系统大页（`HUGEPAGES`，口径 = THP 关闭 **且** HugePages 真被用掉）；规则 **34 → 42**、新增 5 个阈值键 + `tests/test_oracle_rule_gaps.py`（16 用例）                                          | Python 侧（**采集脚本未动**；见「批 11」章与 `_work/oracle-analysis-gap-review.md` §3）    |
| **批 12** ✅ 已完成 | **Python 侧（图表收口）**：图的**声明集合 = 可产出集合** —— 新增单一来源 `ORACLE_CHART_IDS`、`word_report` 从它派生（`oracle_txn_rate` 由「生成即丢弃」变**上架**）；产出侧未注册 id 当场报错；共享引擎"一张图都没产出"由**静默**改为留一行原因（**四库共用**，数据齐全时零字节影响）                                                                                                        | Python 侧（**采集脚本未动**；见「批 12」章）                                              |

**破坏性变更单独隔离**：OF-01（改 item_id 与 tables 文件名）和 OF-02（改扩展名 `tsv`→`psv`）**必须随契约主版本一起做**，别在批 1 里顺手夹带 —— 否则 Python 全线崩。建议顺序：先落批 0 → 批 3（安全，不影响契约）→ 批 2（OS 增量，新增文件不影响既有解析）→ **批 1 一把做掉并同步 bump `SNAPSHOT_SCHEMA_VERSION` 到 2.0**，同步改 `oracle-collector-interface.md`，再改 Python adapter。

**实施进度**：

- ✅ **批 0 已完成（2026-10-09）** —— OF-28（`safe_relpath` 去重，定义 87→86 个函数）+ OF-24（补注释，密度 4.7%→8.3%）。做法为「只新增 `#` 行」，指纹法证明**逻辑零改动**（`bash -n` 通过）。详见 `PROJECT_MAP.md` `### 22.47`。
- ✅ **批 3 已完成（2026-10-09）** —— OF-05（打包结果如实登记 status/exit_code）+ OF-10（口令走 stdin，新增 `ora_exec` 统一入口，10 处调用点收口）+ OF-17（退出码契约 0/10/20/30/31/130，同时写入 `--help` 与 interface §8）。**改动全在 `inspection/oracle_inspection.sh`**，Python 解析逻辑无需改；但**退出码语义自此可用**。构造实测见 §0.5.5 与批 3 章节的验收表。
- ✅ **批 2 已完成（2026-10-09）** —— OF-06（归属判定：`resolve_ipv4`/`is_local_connect_target` + snapshot 三字段 + 非本机披露）+ OF-07（`time_evidence{}`；**否掉了计划里的 `-t`**，见批 2 章节）+ OF-08（`run_with_timeout`，13 处）+ OF-09（5 项 Oracle 专属 OS 视图）+ OF-11（limits 三层）+ OF-12（进程快照）+ OF-15（md/包收敛，md 内重复裸命令归零）+ OF-18（`sar -w` + `coverage.json`）。**改动全在 `inspection/oracle_inspection.sh`**；新增采集项**不破坏既有解析**（只增文件），但 `snapshot.json` **新增 4 字段**、`collect_sys_info` 的 md 内容**变薄**（改引用）。构造实测见 `_work/_verify_b2os.sh`（PASS=20/FAIL=0）与批 2 章节验收表。
- ✅ ~~⬜~~ **批 1 / 批 4 未做** 这条已过期，保留仅作历史：当时建议"批 1 一把做掉并 bump `SNAPSHOT_SCHEMA_VERSION` → 2.0，随后批 4"。**两条均已按此执行**。
- ✅ **批 1 已完成（2026-10-09）** —— OF-01（item_id 显式 ASCII，136 项不再塌陷；`tsv_filename()` 删除）+ OF-02/实测-1/实测-2/实测-3（`psv_normalize` + `tables/<item_id>.psv` + `empty` 真实触发 + status 的 `output_file` 指向同一路径）+ OF-03（新增 `schema_mismatch`）+ OF-04（单次连接）+ OF-13（`capabilities.tsv` / `snapshot.capabilities`）+ OF-16/23（`collection_status.json` + `summary.txt` 进包）+ OF-20（`ora_value` 标志文件 + `ora_value_ok`）+ OF-22（真实 `row_count`）+ OF-27（snapshot 8 段，`database_target_is_local` 移入 `host_identity`）；并把 OF-21（版本单一来源）与 OF-25（`MODULE_PLAN`）**前置到本批**。**破坏性变更随 `SNAPSHOT_SCHEMA_VERSION` → 2.0 一起落地**，`oracle-collector-interface.md` 同步（§4.1 / §4.3 / §4.4）并新增 `oracle-collector-item-ids.md`。构造实测 `_work/_verify_b1.sh`：**PASS=34 / FAIL=0**（批 1 当时；批 4 扩到 T13 后为 **56 / 0**）。本批**自测抓出一条新的 P0（OF-29：未加引号 heredoc 体内出现反引号 → 正常项被判 `error`）**，已修并加两道静态守门，详见「批 1 落地记录」。
- ⬜ ~~批 4 剩余：OF-14（`--output-dir`）、OF-19（`COLLECTION_INCOMPLETE`，需现场构造中断验证）、OF-26（`capture_command` 错误原文与模块日志）。~~ 见下。
- ✅ **批 4 已完成（2026-10-10）** —— OF-14（`--output-dir=DIR` / `--output-dir DIR` 两种写法 + `ensure_output_dir` 提前失败退 10）+ OF-19（`_trap_cleanup` 在 TASK_DIR 落 **`COLLECTION_INCOMPLETE`**，三个 trap 各传信号名）+ OF-26（新增 `module_log_append`：`capture_command` / `ora_sql_to_md` 的**完整错误原文**进包内 `logs/modules/<category>.log`，status 仍只留 5 行摘要；`append_oracle_sysstat_sample` 的 tmp 移出 `STATUS_DIR` 到 `RUNTIME[TMP_DIR]`，**两条返回路径都删**，中断/正常结束都清目录）+ **OF-30**（本批发现：`security_scan` 写的 `status/security_scan_findings.txt` 原先从不进包 —— findings 先落 TMP_DIR，命中时才 `cp` 进 `TASK_DIR/status/`，让 status 里那条路径变成真的）。构造实测 `_work/_verify_b1.sh`（扩到 **T13**）：**PASS=56 / FAIL=0**。自测顺带把静态守门从 1 道加到 **2 道**（反引号 + "词内美元"），见 §批 4 落地记录。
- ✅ **批 5 已完成（2026-10-10）** —— 来自**一份新采的真实 2.0 包**（61 KB / 306 文件 / 156 项）的体检：先**证明批 1–4 确已生效**（156 个唯一 `item_id`、34 项 `empty`、`output_file` 无悬空、98 `.psv` 与 98 SQL 项同名、manifest 305 文件 sha256 全对、snapshot 8 段齐全），再修 **N-1～N-5** 五个 P1：N-1（错误判据**锚行首**，修掉批 1 引入的"告警日志数据被当成报错"回归）、N-2（失败项 psv 落 **`psv-unavailable`** 占位、`row_count=0`，`.psv` 自此**三种形态**）、N-3（`security_scan` 提前 + `sync_status_to_package` + **`packaging_status.json`**）、N-4（`V$RMAN_STATUS` 的 `session_key` → `session_recid, session_stamp`）、N-5（尾部复用 `refresh_status_tally`、`summary.txt` 补 `partial` + **分档自校验行**）；并按 **B 方案**把 `artifacts.report_md` → **`report_md_external`** + `report_md_scope`（人读 md 在包外，**仓库内无消费者**）。构造实测（扩到 **T17**）：**PASS=84 / FAIL=0**。
- ✅ **批 6 已完成（2026-10-10）** —— 把体检报告余下的 P2 全部收尾（**N-6～N-11、N-13、N-14** + N-15 文档）：N-6（bg 模块计数由 `wait_background_modules` **按落盘 status 回写**，"执行模块: 26 → 28"）、N-7（`sar_files.txt` 移出 `status/` 到 `TMP_DIR`）、N-8（`free`/`df`/`df -i`/`lsblk` 四个**列对齐文本**从 `tables/*.tsv` 改归 `evidence/*.txt`，`tables/` 契约收窄为 `.psv` + 5 个真 TAB 表）、N-9（`sqlplus_version` 探测改 `2>&1` + 取 `Release` 号）、N-10（删 `host_identity.is_local_target` 重复键）、N-11（manifest 头部显式 `self_excluded`）、N-13（`TNS-12541`/`TNS-00511`/`no listener` → `not_enabled`）、N-14（互斥备选显式记 `skipped` + capabilities 补 `ip_command`/`ifconfig_command`）、N-15（summary 说明实时采样双视图）。构造实测（扩到 **T22**）：**PASS=107 / FAIL=0**。**唯一对 Python 侧的可见影响**（`ctx.tables` 少了 4 个键）已在 `interface` §11.8 写明处置；Python adapter 按 `collector_version` 分叉记为**批 8 待办**（本项原记为"批 7 待办"，后批 7 被 N-16…N-21 占用，故顺延）。
- ✅ **批 7 已完成（2026-10-10）** —— 来自**第二次复采包**（`…_2026-10-10_10-43-30.tar.gz` / 316 条目 / 161 项）的体检：先**证明批 5 / 批 6 的修复确已生效**（三项关键行为 `topsql.recent_errors` error→ok、`rman_validate` schema_mismatch→empty、`listener_status` error→not_enabled；另 8 项落点/口径全到位；manifest 307 文件 sha256 全对），再修 **N-16～N-21**：N-16（**`run_module` 成为唯一记账者** —— `if ! func` 改 `func; rc=$?` 让真实 rc 30/31 不再被压成 1、新增 `MODULE_REASON` 出口、`collect_structured_package` 删自记、控制台文案 id 改合规名，控制台"包内 = 总数 − 1"由错的变对的）、N-17（`generate_snapshot_json` 挪到 `generate_collection_status` **之后**，`snapshot.total` 与 `summary.total` 不再 158 vs 159）、N-18（4 处 `record_status` 补 `exit_code` + **第四道静态守门** `_work/_scan_record_status.py`：实参 ≠ 10 即拒绝生成自测）、N-19（`first_existing_relpath`：集合型项 `output_file` 给代表文件而非目录）、N-20（`kernel_parameters.tsv` 补表头 + 缺 `sysctl` 记 `unsupported`）、N-21（`interface` 的 `partial` 行更新）。构造实测（扩到 **T26**）：**PASS=139 / FAIL=0**。
- ✅ **批 8 已完成（2026-10-10，Python 侧；采集脚本零改动）** —— 把"adapter 按 `collector_version` 分叉"落地，并**把范围扩到实测的真实破坏面**：改前对真实 2.0 包只读出 **5 张垃圾表**（98 个 `.psv` 因 `glob("*.tsv")` 全被忽略、5 个真 TAB 表被 `|` 错解）→ 34 条规则 **16 条 `not_evaluated`**、报告只有 2 条 finding。改后：`.psv`（`|`）与 `.tsv`（TAB）按版本分派、`item_id` 靠 `status/*.tsv` 的 `output_file` **反查**（`kernel_parameters.tsv` ↔ `system.sysctl_selected`）、每张表注册到 `item_id` + **中文标题** + 唯一尾段 + 15 行 `LEGACY_ALIASES` 桥接，并回填 `evidence/filesystems.txt`（`df -PT`）。实测：`ctx.tables` 键数 **5 → 416**、`not_evaluated` **16 → 2**（余 2 条 = 无 ASM / FRA 未配置，**判定正确**）、覆盖率 **53% → 94%**、findings **2 → 12**、下游 66 个调用点 **64 个解析成功**。新增 `tools/gen_oracle_item_titles.py`（标题表单一来源 = 采集脚本）+ 守门测试防漂移，`tests/test_oracle_package_adapter.py` **4 → 12 用例全绿**；全量回归 **5 failed / 292 passed / 148 subtests**（5 个 FAIL 仍是 MySQL 源包缺失的老问题，无新增）。
- ✅ **批 10 已完成（2026-10-10，Python 侧分析/报告层；采集脚本零改动）** —— 起因是"采集脚本没问题了，反查**分析层**少判了什么、**报告层**少展了什么、图与目录顺序合不合适"。方法不是读代码猜，而是**端点跑一遍再拆 DOCX**（`plugins/oracle/cli.py` → `generate_report.py` → python-docx 读回），参照系 = MySQL。结论：及格线（有渲染后果的缺陷）**只有 1 条**——**G1「待客户确认事项」整章空转**（DOCX 第 14 章全部内容只有一句"本次分析未产出待确认事项清单"，而 MySQL 同一章有 16 条真事项；成因是 presentation 信封里没这个键，而 `report_adapter` 是 `deepcopy(source)` **补**而不是造）。本批落地：`CONFIRMATION_TEMPLATES`（**11 条**，只收"须客户拍板 / 须客户提供外部证据"的）+ `pending_confirmations()`（按**已触发的规则**派生）+ `analyzer.attach_pending_confirmations()`（挂实例）+ `build_report_model` **条件注入**（旧基线无此键 → 不打穿冻结契约）。实测第 14 章由空转 → **5 条**（备份归属 / 归档模式 / 时间源 / 密码托管 / 锁定账号）。另一条：**章节动线** —— `storage_capacity` 原排在 `oracle_instance` **之前**（读者没看到"实例是什么"就先看"它装了什么"），改为 `oracle_instance` 在前，对齐 MySQL 的 **实例 → 配置 → 运行 → 容量**；改后 DOCX 目录页 16 条与正文 Heading1 **逐条一致**。新增 `tests/test_oracle_confirmations.py` **9 用例**（含 ★**"模板引用的 rule_id 必须真实存在于规则集"** —— 写了不存在的 rule_id 则模板永不触发、报告永远显示"没有待确认事项"，而没有任何测试会红）。**未做（记为批 11+）**：规则补口（DataGuard 6 项采集 0 条规则 / 对象治理 / 审计画像 / 大页）、图表"可产集合 = 声明集合"收口、展示补口。详见 `_work/oracle-analysis-gap-review.md`。
- ✅ **批 11 已完成（2026-10-10，Python 侧规则层；采集脚本零改动）** —— 承接批 10 的"未做"第一条，把「**采了有数据、零判据**」从感觉变成清单：判据 = 该采集项在包内有数据（`status=ok` / 有行）**且**在 `oracle_rules.py` 里找不到任何消费者。方法不是读代码猜，而是先用 `_work/_b11_probe.py` 逐项打印 `status` + `output_file` + **真实列名** + 样例行，再照真实列名写规则。落地 8 条规则覆盖四个域：**DataGuard 4 条**（`ORA.DG.TRANSPORT_ERROR` 主库亦适用 / `ORA.DG.APPLY_LAG` 解析 `+DD HH:MM:SS` / `ORA.DG.LOG_GAP` / `ORA.DG.EVENT_ERROR`）、**对象治理 2 条**（`ORA.SCHEMA.SEGMENT_BLOAT` 按入包行判并把"N 条候选"写进 fact / `ORA.CAPACITY.LARGE_SEGMENT` 索引 `SIZE_MB` 折算 GB）、**口令画像 1 条**（`ORA.SECURITY.PASSWORD_PROFILE`）、**系统大页 1 条**（`ORA.SYSTEM.HUGEPAGES`）。★ 两条最容易照抄错的：① **大页口径与 MySQL 相反** —— MySQL 只要求**关掉** THP，Oracle 还要把 SGA 放进 HugePages，故多出"配了却闲置（`Total > 0 且 Free/Total > 0.5`）"半条，照抄 MySQL 会漏掉 Oracle 最典型的那半；② **无备库 ≠ 延迟正常** —— 未配 DG 的库上 `v$dataguard_stats` / `v$log_sequence_gap` 为空，两条备库规则必须落 `not_evaluated`（延续「未采集 ≠ 0」），**不能落 `passed`**。规则包 **34 → 42**、新增 5 个阈值键（`dg_lag_seconds_warning`=900 / `dg_log_gap_warning`=5 / `segment_bloat_mb_warning`=1024 / `large_segment_gb_warning`=100 / `hugepage_free_ratio_warning`=0.5）。`tests/test_oracle_rule_gaps.py` **16 用例**（每条都用"改数据 / 改阈值 → 结论跟着变"证明旋钮真被读，含 ★`test_hugepages_configured_but_unused_triggers` 与 `test_dg_lag_without_a_standby_is_not_evaluated`）；负控制 `_work/_b11_negctl.py` **4/4 全部真实变红**（各只红 1 条，`finally` 逐字节复原 + sha256 校验）。基线**整体重出**（`health_score` 26→23、`rules` 34→42、`triggered` 10→11、`passed` 20→24、`not_evaluated` 4→7、`risk_register` 10→11、`severity_counts` medium 4→5；`sections` 9 / `items` 52 / 8 张 PNG **逐字节不变**）；`source_implementation.*` **刻意不重写**。实测真实 2.0 包：规则 42、触发 13 / 通过 25 / 未评价 4、findings 13、健康分 10，新触发仅 `ORA.SECURITY.PASSWORD_PROFILE`（DEFAULT profile 无复杂度校验）。**有意不做**：`oracle.control.recent_param_changes`（`ISSES`/`ISSYS_MOD` 语义需先核实采集 SQL）与 `oracle.basic.object_counts`（缺明确阈值口径）—— 宁少不错。详见「批 11」章。
- ✅ **批 12 已完成（2026-10-10，Python 侧图表层；采集脚本零改动）** —— 承接 `_work/oracle-analysis-gap-review.md` §2.1/§2.4，修的是「图的**声明集合 ≠ 可产出集合**」这一对**静默**缺陷：`charts.py` 能产 9 张、`word_report.py` 只声明 8 张，两个方向都错 —— `oracle_txn_rate` **画出来没人声明（生成即丢弃）**、`oracle_physical_io` **声明了却可能一张不出**，而且 `word_engine.render_section_charts` 在过滤后集合为空时**静默 `return False`**（连"趋势图"小节都不出现、也不留说明，读者分不清"本章本就没有图"和"本该有图没画出来"）。落地四件事：① **单一来源** `ORACLE_CHART_IDS`（顺序即上图次序），`_figure()` 改按 chart_id 命名 PNG；② `word_report` 两个章节的 `ChartDefinition` **全部由 id 清单派生**（图题留在报告层）；③ 声明了图却一张都没产出 ⇒ **留一行原因**（不改小节号、不渲染空的"趋势图"标题），原因代码串由 `CHART_SKIP_REASONS` 翻成人话、**未登记代码原样带出**；④ 产出侧防呆：未注册 chart_id **当场 `ValueError`**。★ 第③条落在 `inspection_core/word_engine.py`，**四库共用**（MySQL/PG/SQL Server 同样病灶），数据齐全时**零字节影响**（MySQL 基线文档的规范化 XML hash 测试原样通过可证）。★ 两条最容易做错：**① "单一来源"必须真派生**，不是两边各写一份碰巧相等（漏一个正是 `oracle_txn_rate` 的病因，静态读代码与报告都看不出来）；**② 数据不足导致不产出 ≠ 从声明里删掉**，否则单机空闲库上 `oracle_txn_rate` 会被"修"成永久不存在。实测真实 1.4 包 ZYDB：**8 张图逐字节不变、正文 729 段逐段相同**（唯一差异是封面"生成时间"戳）—— 本包 `user_commits` 采样窗口内恒为 64 ⇒ 提交速率 0 ⇒ `oracle_txn_rate` 本就未产出，**这批在该包上是严格无操作**；两条新行为改由**合成数据**用例钉住，不依赖数据巧合。新增守门 `tests/test_oracle_chart_coverage.py`（7 用例）+ `tests/test_word_engine.py::ChartSkippingTests`（4 用例）；负控制 `_work/_b12_negctl.py` **4/4 全部真实变红**（声明侧漏 id / 恢复静默 / 清空原因翻译 / 摘掉产出守门），`finally` 逐字节复原 + sha256 校验，复原后 25 passed。**有意留到下一批**：§2.3 的 3 张"有数据没画"的图（表空间 Top-N 条形 / 归档量趋势 / Top SQL Top-N）—— 需要**新图型**（公共层渲染器只有时序折线），且 `oracle.archive.daily_30d` 手上两份包**全空**、补出来**无法用真实数据验证**。详见「批 12」章。
- ✅ **批 13 已完成（2026-10-10，Python 侧展示层；采集脚本零改动）** —— 承接批 11/12 的口径，修「**采了有数据、一个字不进报告**」：判据 = 采集项在包内**有数据**（`status=ok` / 有行）却在 `plugins/oracle/presentation.py` 里**找不到任何消费者**（**机械可枚举**）。落地 **24 个展示条目**（`oracle.awr` 5 + `oracle.dg` 5 + `oracle.audit` 7 + `undo/temp/共享池` 4 + 其余 3），**检查项 52 → 76**（**9 章不变**）。三条机制性改动：① `_cell()` 容忍 **sqlplus 按列宽截断表头**（`SEGMENT_SPACE_MANAGEMENT`→`SEGMEN`、`ADM`、`COMPRES`、`SIZ`；`row.get(完整列名)` 取不到时**不报错、只静默少一列**）；② `_listed()` 让空 / 未采条目按真实 `collection.status` **分叉文案**（`success_no_data` 与 `not_evaluated` / `permission_denied` **是相反结论**，延续「未采集 ≠ 0」）；③ `_analysis()` 的 `not_evaluated` 分支**透出规则引擎 `reason`**（原先一律印"证据不足，未执行评价。"，把"无备库（未配置 DataGuard）"丢了）。唯一章名改动：`备份与恢复检查` → **`备份、恢复与高可用检查`**（DataGuard 折叠进来后名实相符）。★ 三条坑：**presentation 层条目不带 `item_id`**（由 `report_adapter._extract_item_id` 从 `source` 尾段派生）；判"这项采过没有"**看表键在不在 `ctx.tables`**（**空表也算登记**），`non_ok_items` **只记失败态**；**对着冻结基线断言抓不到实现回归**（章名断言首版只读死文件，负控制第 5 项因此**没变红**，补一条**走实现路径**的用例后才 5/5 全红）。守门 `tests/test_oracle_display_coverage.py` **16 用例 / 9 subtests**（合成 `FakeContext`）；负控制 `_work/_b13_negctl.py` **5/5 真实变红**。基线整体重出：`expected_summary.items` **52 → 76** 是**唯一**业务结果变化（health 23 / rules 42 / triggered 11 / passed 24 / not_evaluated 7 / risk_register 11 / severity_counts **全同**，8 张 PNG 逐字节不变）—— 「纯展示层、**不触发新判据**」的强证据；`source_implementation.*` **刻意不重写**。回归 **5 failed / 353 passed / 168 subtests**。详见「批 13」章。
- ✅ **批 14 已完成（2026-10-10，Python 侧取数层；采集脚本与基线零改动）** —— 修「**同一份包内文件、两套互不相干的读取实现**」：呈现层此前不走 adapter，自己拼 `root / "evidence/…"` 再自己解析，而 `package_adapter.py` 已经在干同一件事（把 `tables/*.psv` 注册进 `ctx.tables`、还专门回填了 `evidence/filesystems.txt`）⇒ 同一份 `kernel_parameters.tsv` 有**两份解析实现**，改一处忘另一处**没有任何测试会红**。★ 真风险不是「代码不好看」，是**静默少一节内容**：批 6 / N-8 就把 4 个「列对齐文本」从 `tables/` 挪到了 `evidence/`，路径写死的读法移动后**只返回空**——报告少一节、规则层不报、测试不红。落地：① 新增 **`PackageContext.raw_lines(name)`** —— 按**契约文件名**在 `tables/`、`evidence/` 两处查找并返回原始行（缺文件返回 `[]`）；② 目录集中到模块级 `_RAW_FILE_DIRS = ("tables", "evidence")`（**全仓库只此一处**）；③ Oracle **6 处** + MySQL **5 处**调用点改走它，5 + 4 个解析器从「收 `Path`」改成「收 `list[str]`」。★ **「收行」不是「收路径」是有意的**：解析器拿到 `Path` 会直接 `TypeError`（`Path` 不可迭代）⇒「必须走 `ctx.raw_lines`」这个口径**在运行期也成立**，不只写在注释里。★ **PG 还有同一模式 9 处、本批未纳入**（守门只覆盖 Oracle + MySQL）= **待办，不是缺陷**。**等价性证明**（同一 1.4 包重跑 `cli.py` + `generate_report.py`）：8 张 PNG **逐字节相同**、`llm_input.json` **完全相同**、其余 JSON 与 DOCX 规范化后**实质差异 0** ⇒ **Oracle 基线一个字没改**（本批对报告产物严格无操作）。守门 `tests/test_raw_file_access.py` **17 用例**（访问点行为 / 继承而非覆写 / 零 `ctx.root` 拼接 / 契约文件名集合，每条结构断言配**尺子自证**）；负控制 `_work/_b14_negctl.py` **4/4 真实变红**，逐字节复原 + sha256 校验。回归 **5 failed / 370 passed / 168 subtests**（5 个 FAIL 是 MySQL 源包缺失的老问题，无新增）。★ **MySQL 侧没有端到端覆盖**（`test_mysql_*` 因源包不在仓库而失败）—— 本批为该侧补了直接打解析器的用例，但**真实 MySQL 包上的端到端验证仍未做**。详见「批 14」章。
- ✅ **批 15 已完成（2026-10-12，Python 侧报告质量；采集脚本零改动）** —— 起因是用户**拿生成的 DOCX 逐条挑错** + 拿一份**同客户 MySQL 报告**当参照系比对（不是读代码猜）。六处**客户一眼能看出不对**的：① **P0-1 伪文件系统进容量判据** —— 光驱 `iso9660` 是只读介质、恒定 100% 满且 0 可用，`tmpfs`/`devtmpfs`/`proc`/`sysfs` 更没有容量概念，规则层却遍历**全部** `df` 行取最大值 ⇒ 单机库被判「文件系统使用率过高 100%」，真值（`/`）只有 **33%**；过滤判据统一用**公共层** `is_persistent_fstype`（**MySQL 早就在用，四库只有 Oracle 漏了**），键名归一同源 `package_adapter.filesystem_type`，规则层 / 4.5 文件系统表 / 3.8 挂载参数**三处同源**，口径串带「**已排除 N 个…**」好让客户能自证。② **P0-2 表空间图与表 6.1 同口径** —— 图取 `PCT_USED`（分母 = 已分配 `ALLOC_MB`）、表与规则层取 `USED_MB ÷ MAX_MB`，同一份报告里 SYSTEM 图上是 **99.98%**（标红）、表里是 **3.39%**；改后**三处同一算式**，`MAX_MB` 缺失才退回已分配口径（老包）。③ **P0-3 4.5 键名对齐** `parse_df_pt`（`Mounted on`/`Filesystem`/`Available`，展示层原读 `MOUNTPOINT`/`FILESYSTEM`/`AVAILABLE`，**只有 `USE%` 恰好撞对** ⇒ 9 行表三列全空），补「类型」列 + `_kb_human()` 把 KB 换算成 `64.30 GB`；并加 `elif fs_rows:` 分支 —— **全被过滤掉也要出项**（`not_applicable` + 说明），让"整机只有 tmpfs"与"这章漏采了"长得不一样。④ **P1-2 规则「通过」也带实测值** —— `RuleEvaluation` 新增 `facts` 字段，`_evaluate` 无论触发与否都留证据，`presentation._analysis` 的 `passed` 分支透出；全文「未提供补充依据」**66 → 4**。同时把**「查了、确认没有」与「没采到」在章末小结里分开**（`report_adapter` 新增 `collection_status == "empty"` 分支），事实项补依据（展示层 `note` 优先，否则记录条数）。⑤ **P1-3 一张表只留一种行形** —— 7.1 只留速率行（平均值 + 最大值），命中率 / 比例落到各自规则项做单值行；此前混表后渲染层按行的**键并集**出列 ⇒ 每行缺两格全填「未采集」。⑥ **P1-4 规则项正文给结论**（**共享引擎** `word_engine.render_inspection_item`）—— 无明细表的规则项原先正文只印「本项没有可展示的结构化记录。」，读者把「规则项」读成「采集失败」；现 professional 正文落一行「分析结论 <该规则结论>」，legacy 紧随其后渲染完整分析块、正文不再重复（**四库共用**，MySQL legacy 文档 hash 随之变化，已做控制变量自证）。附带修 6.1「已使用(MB)」真 0 不再显示成空白（`if x` → `is not None`）。**验证**：新增 `tests/test_oracle_report_quality.py`（**15 用例 / 7 类**）+ `test_word_engine.py::RowsLessRuleItemTests`（2 用例）+ `test_oracle_plugin.py` 期望值随 empty/not_evaluated 分叉同步（`normal` 49→57 / `not_evaluated` 17→9）；负控制 `_work/_b15_negctl.py` **8/8 真实变红**；控制变量自证 **`CB8653E5…`（新）/ `D34B7A1B…`（旧）两条都命中**（★ 两次渲染必须在**独立子进程**里跑 —— `word_engine` 一旦 import 就驻留内存，同进程改文件无效）；基线 `expected_summary` **逐项不变**、10 张 PNG **只有 `oracle_tablespace_usage.png` 变**（正是 P0-2）；回归 **5 failed / 401 passed / 168 subtests**（5 个 FAIL 仍是 MySQL 源包缺失，无新增）。★ **有意未做**：5.1 `control_files` 的**折行**（SQL\*Plus 按 80 列宽折行，不是两个真文件）—— 属**取数层**，留待下一批。详见「批 15」章。

---

Oracle 侧原**一个测试都没有**；**批 1 起已建立构造自测**（`_work/_b1_make_verify.py` → `_work/_verify_b1.sh`）。落地时至少要做到：

1. `bash -n inspection/oracle_inspection.sh` 通过。
2. **契约不变量断言**（已落在 `_work/_verify_b1.sh`，形态是"从真实脚本**逐字抽取**函数 + 假 sqlplus + 假目录树"）：
   - `ls status/*.tsv | wc -l` == 名义采集项数（OF-01）→ 现由 T4 用 5 项小样验证"不塌陷 + 无重复 + 全 ASCII"；
   - `awk -F'\t' '{print $1}' status/*.tsv | sort | uniq -d` 为空（OF-01）；
   - psv 首行列名 + 填充空白剥净 + 无 `---` 行（OF-02，T1/T6）；
   - `coverage.json` / `collection_status.json` / `snapshot.json` 三个 json 都能 `json.load`（OF-16/18/27，T9）；
   - `snapshot.json.host_identity.database_target_is_local` 存在且为布尔（OF-06；**★ 批 1 起落在 `host_identity` 段，不再是顶层**）；
   - `snapshot.json.time_evidence.timezone` 非空（OF-07）。
3. **指纹法**证明批 0 零逻辑改动（见 OF-24 的验收命令）。
4. 用一份**真实 Oracle 采集包**核对：✅ **已拿到**（`oracle_inspection_task_ZYDB_…_2026-10-09_22-47-04.tar.gz`，`collector_version`=1.4）。原 C 档 8 条已全部落定（§0.5）；**OF-05 已用 stub 构造验证（§0.5.5 已升 A 档）**。⚠️ **OF-19（收集中断）已实现并有 T12 构造验证，但"真实环境中断一次"仍未做** —— 改造后用**同型号实例**再采一份，逐项对照 `oracle-collector-interface.md` §11.2 的计数表。
5. 与 MySQL 版做**逐项对照表**（`docs/oracle-collector-interface.md` §3 已给出对照），确认"采集项名 / category / 文件路径 / status 取值"四件事的差异清单，交给 Python 侧更新 adapter。
6. **各批可复跑证据**：
   - 批 1 + 批 4：`python _work/_b1_make_verify.py` → `bash _work/_verify_b1.sh` → **PASS=56 / FAIL=0**（T1–T13；含两道静态守门）。
   - **批 5**：`python _work/_b1_make_verify.py` → `bash _work/_verify_b1.sh` → **PASS=84 / FAIL=0**（**T1–T17**；+28 条新断言）。
   - **批 6**：→ **PASS=107 / FAIL=0**（**T1–T22**；+23 条新断言，仍是约 1 分钟、不连真库；生成前两道静态守门照跑）。新增 T18（bg 计数回写 + skipped 不计）、T19（N-7/N-8 落点）、T20（N-9/N-10/N-11）、T21（N-13 + **不许放宽**的反例）、T22（N-14/N-15）。
   - **批 7（最新，同一支脚本）**：→ **PASS=139 / FAIL=0**（**T1–T26**；+32 条新断言）。新增 T23（N-16：非零 rc 原样带出 + `MODULE_REASON` 双向）、T24（N-17：段内顺序 + 两处 total 相等）、T25（N-18：4 处补参 + 守门可跑 + 10 参落列）、T26（N-19/N-20）。**并有第四道静态守门**：生成期 `_work/_scan_record_status.py`（`record_status` 实参必须 = 10，命中即拒绝生成）。
   - **Python 全量测试**（与采集器改动无关，用于确认没被牵连）：`python -m pytest -q` → **5 failed / 292 passed / 148 subtests**，5 个 FAIL 全是 `tests/test_mysql_*`（**MySQL 源包 `mysql_inspection_v1_db01_*.tar.gz` 缺失**，长期存在，非本次引入）。
   - 批 2：`_work/_verify_b2os.sh`（OF-06/07/09/11/12/18 综合 → **PASS=20 / FAIL=0**）、`_work/_verify_b2.sh`（OF-06 独立）。
   - 批 3：`_work/_verify_b3.sh` + `_work/_verify_b3_pkg.sh`。
   - 批 2 后 `bash -n` 通过、`run_with_timeout` 13 处调用、`collect_sys_info` 内重复裸命令归零。

---

## 附：本文件里"撤回/误判"的纪律

静态读代码一定会误判，所以记录两条**本轮已自查**的：

1. **`get_ora_conn` 不是拼错的函数名。** 初看它在脚本里"既被调用、又搜不到定义"，差点判"未定义函数导致全部 SQL 失败"。实测：定义在 **L512**，调用点用的是同一个名字。**批 3 后它只被 `ora_exec` 调用 1 处**（10 个采集入口统一改走 `ora_exec`，见 OF-10）。**这一条是误报，不进清单。**
2. **`ora_sql_to_md` 里 `.tsv` 后缀 + `SET COLSEP '|'` 不是笔误。** 它是有意绕过"sqlplus 无法输出裸 TAB"这一限制的近似方案，错在命名与空结果占位，不在分隔符选择本身。修法（OF-02 路线 1）保留 `|` 只改后缀，不要试图让 sqlplus 吐 TAB。
