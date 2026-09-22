# mysql_inspection_standard.sh 改造清单

- 审查对象：`D:\AI\MYSQL\GPT\1.0\inspection\mysql_inspection_standard.sh`（v1.1.0-standard，1374 行）
- 位置：采集脚本 2026-09-21 由仓库根移入 `inspection/`；本文档下文所有命令均在 `inspection/` 目录内执行
- 行号基准：本次读到的版本；改完后行号会漂，以函数名为准
- 分三批：批 1 数据正确性（必改）、批 2 安全与门禁（必改）、批 3 补采集项（按需）
- 判定口径：**批 1 / 批 2 不改完，脚本不能上生产**——它会输出一份写着 `ok` 的错数据

---

## 执行进度（2026-09-22 更新）

| 批次 | 项 | 状态 |
|---|---|---|
| 批 1 数据正确性 | F-01 / F-02 / F-03 / F-10 / F-16 / F-18 / F-20 / F-21 / F-24 | ✅ **已落地并通过自测**（`tests/selftest/mysql_collector_batch1.sh`，37 passed / 0 failed） |
| 批 1 附带 | 新增 `collect_innodb_status`；删除旧 GTID `sed` 续行补丁；`known_tsv_header` 补 `digest_text` | ✅ 已落地 |
| 批 2 安全与门禁 | F-04 / F-05 / F-06 / F-11 / F-19 | ✅ **已落地并通过自测**（`tests/selftest/mysql_collector_batch2.sh`，40 passed / 0 failed） |
| 批 2 附带 | F-12 随 F-05 一起落（同一段代码，按 F-12 形态实施）；`sanitize_tsv_columns` 重复列 bug（新发现，见下） | ✅ 已落地 |
| 批 3 补采集项 | F-09 / F-13 / F-14 / F-22 / F-23 / F-25 / F-26 | ⬜ 未开始 |
| 批 0 注释 | F-27 | 🟡 L3/L4 函数头注释已随批 1/批 2 落；L1 文件头、L2 阶段分隔、L5 其余函数头待补 |
| 批 4 外部复核回吐 | F-28 / F-29 / F-30 | ✅ **已落地**（2026-09-22，来源见文末「批 4」节）：F-28 `long_transactions.query_sample` 裸 CR → **已补三层 REPLACE**（脚本已改，`bash -n` 通过）；F-29 快照 `Com_*` 缺失（P_S 按设计排除）→ **已定 B：不改采集、写死口径**（契约 §8.4 + 分析层注释）；F-30 `host=''` 语义等于 `%` → **不改采集**，已补契约 §8.4 与分析层判据 |

### 本轮新发现（不在初版清单里）

**`sanitize_tsv_columns` 会输出重复列【P1，已修】。** 白名单里 `Source_UUID` 与 `Master_UUID` **各出现两次**，
而匹配循环 `for(j=1;j<=n;j++) if(h==tolower(w[j])) keep[++k]=i` **没有 `break`** —— 同一个表头列被匹配两次、
索引被追加两次，输出直接多一列。5.7 上 `replica_status.tsv` 实际会多出一个 `Master_UUID` 列。
修法：循环里加 `break`（每个表头列最多命中一次）。验证见 `tests/selftest/mysql_collector_batch2.sh` T6.1。

**占位表头与白名单的一致性已建立不变量校验。** `tests/selftest/mysql_collector_batch2.sh` T7.5 断言
`known_tsv_header` 的每一列都必须是 `sanitize_tsv_columns` 白名单成员——否则空结果时写进去的列名，
在正常结果时会被整列丢掉（schema 漂移）。当前两套命名下的 14 列全部命中。

### 自测怎么跑

```bash
cd D:/AI/MYSQL/GPT/1.0
bash tests/selftest/mysql_collector_batch1.sh   # 批 1：my.cnf 展开 / 表头 / 列集 / 状态分类
bash tests/selftest/mysql_collector_batch2.sh   # 批 2：脱敏闸门 / 错误原文 / 重复列 / 退出码
```

两套都是**只抽取被测函数 + 配桩**，不连数据库、不改生产脚本。
临时目录用脚本同级绝对路径（`.st.XXXXXX`，已进 `.gitignore`）——
本机 MSYS2 下用 `/tmp` 会因进程替换 FIFO 与沙箱交互**偶发卡死**，不是脚本问题。

---

## 0.0 自查修正（第二轮逐条回核代码，2026-09-20）

初版清单有 **2 条误报、3 处给法不完整**。已按下表修正，**以本表为最终口径**：

| 项 | 初审结论 | 复核后 | 证据 |
|---|---|---|---|
| F-15 | P1：snapshot 声明 `history/coverage.json` 但实际只有 `coverage.tsv`，Python 会 404 | **误报，撤回** | `compute_sar_coverage` 同时写了两个文件——`coverage.tsv`（L692）与 `coverage.json`（L696）。snapshot 引用的 `coverage.json` **存在**。只剩一条 P3 口径问题：Python 侧该以哪个为准 |
| F-17 | P2：`system.ip_address` 与 `system.ifconfig` 写同一个 `ip_address.txt`，互相覆盖 | **误报，降级** | L421-428 是 `if has_cmd ip … elif has_cmd ifconfig … else`，**两分支互斥**，`ifconfig` 只在 `ip` 不存在时才执行，不存在覆盖。真实问题小得多：① 输出文件名仍叫 `ip_address.txt`，语义误导；② `ip` 命令存在但运行失败时不会退到 `ifconfig` |
| F-02 | 已给出递归展开的重写代码 | **代码有 bug，已修** | 原 awk 只匹配**下划线**键名，而客户 my.cnf 用的是**连字符**（`transaction-isolation`、`log-bin`、`pid-file`、`default-authentication-plugin`、`default-storage-engine`）；`gsub(/-/,"_",key)` 原只写在正文建议里、没进代码 → 照抄仍然返回近空结果 |
| F-02 | "先处理目录，保证与被包含文件读取顺序一致" | **说法错误，已改** | 我的实现把 include 压到栈尾（BFS），MySQL 是**就地展开**（DFS）。同一个键出现在多个文件时"后者胜"的次序不同 |
| F-04 | 过滤正则 `(^|_)(password\|passwd\|pwd\|secret\|token\|api_key\|auth_token)(_\|$)` | **过度过滤，已修** | 该正则会连带删掉 `default_password_lifetime`、`password_history`、`password_reuse_interval`、`password_require_current`、`validate_password_length` 等**策略类**变量。`default_password_lifetime=0`（密码永不过期）恰恰是安全巡检要找的项，不能被当秘密删掉 |
| F-06 | 只改白名单 + L228/L229 表头 | **漏两处，已补** | ① L218 `replica_status.tsv` 的占位表头也要加那 3 列；② 更关键：`replication_channels` / `replication_workers` 的**列名随 `--include-log-text` 变化**（L868-872 / L877-882：默认分支别名 `LAST_ERROR_MESSAGE_SHA256`，`--include-log-text` 分支是 `LAST_ERROR_MESSAGE`），而 `known_tsv_header` 硬编码前者 → **空结果 + `--include-log-text` 时占位表头必错**。已单列为 **F-18**：一个静态表头不可能同时匹配两种模式，必须按 flag 分支 |

本轮新增的两条事实，已并入对应条目：

- `sanitize_tsv_columns` **全脚本只被调用一次**（L849，且只针对 `replica_status.tsv`）。所以 `global_variables.tsv`、`accounts.tsv` 这些**没有任何列级裁剪**——F-04 新增的过滤是它们**唯一**的保护，不是"多一层保险"。
- `sanitize_tsv_columns` 保留的是**原始列名**（L752 回写 `$keep[x]`），所以 `known_tsv_header` 里必须写 MySQL 的**原生列名**，不能写臆造名。

### 本文件的可靠性边界（重要）

- §0.0 及批 1/2/3 的结论来自**静态读代码**（逐行回核行号）+ **官方手册原文核对**；§0.1 的结论来自**一次真实运行**（MySQL 5.7.44，2026-09-20）。
- 因此证据分三档，请区别对待：

| 档 | 内容 | 可信度 |
|---|---|---|
| A | 代码事实（行号、SQL 文本、函数调用关系）+ **实测输出**（§0.1 全部条目） | **可直接采信** |
| B | 官方手册原文（F-01 的 `--batch`/`--raw` 行为） | **可直接采信** |
| C | 改法本身、以及未实测场景的推断 | **需现场验证** |

- **C 档尚未验证的项**（脚本未在任何目标机上跑过这些路径）：F-02 的 `!include` 实际展开结果、F-13 的 `find -printf` 在 Anolis/RHEL 上的行为、F-04 过滤后 `global_variables.tsv` 的实际剩余键集、F-22 的文件错误日志聚合正则在真实日志上的命中率。

### F-01 的官方依据（已核到原文）

F-01 是全清单里影响最大的一条，改法是否成立完全取决于 mysql 客户端行为，已核对官方手册：

> **`--batch`**：Batch mode results in nontabular output format and escaping of special characters. Escaping may be disabled by using raw mode; see the description for the `--raw` option.
>
> **`--raw`**：For nontabular output (such as is produced in batch mode or when the `--batch` or `--silent` option is given), special characters are escaped in the output so they can be identified easily. Newline, tab, `NUL`, and backslash are written as `\n`, `\t`, `\0`, and `\\`. The `--raw` option disables this character escaping.

结论：去掉 `--raw` 后 `\n` / `\t` / `\0` / `\\` 由客户端负责转义，TSV 行结构不会被自由文本撑破——**F-01 的改法成立**。
来源：MySQL 8.0 Reference Manual — mysql client options（`--batch` / `--raw`）。

---

## 0.1 实测验证（2026-09-20，MySQL 5.7.44 真实运行）

使用者提供了一次真实采集结果：`test-host` / 3306 / **MySQL 5.7.44-log** / 采集器 v1.1.0，总耗时 **94.90 秒**，状态统计：成功 60、成功但无数据 14、不支持 4、超时 2、错误 0。

**这是本文件第一次拿到运行时证据**，用途是区分"静态读代码的推测"与"真实行为"。以下结论均有实测输出支撑。

### 0.1.1 实测推翻一条旧判断（撤回）

| 项 | 此前写的 | 复核后 |
|---|---|---|
| `mysql.unused_indexes` | P2：用 `NOT EXISTS` 关联 pfs 做近似判断，不如 `sys.schema_unused_indexes` 准确 | **错，撤回。** L830-832 用的**就是** `sys.schema_unused_indexes`。该项的真问题不是"写法不准"，而是**它在这个实例上跑不完**（见 F-24） |

### 0.1.2 实测确认"正确"的两项（不是缺陷，别改）

| 实测输出 | 判定 |
|---|---|
| `mysql.data_lock_waits: unsupported，performance_schema.data_lock_waits unavailable` | **正确。** 该表是 MySQL 8.0 才有的，5.7 确实没有；L508 探测与 reason 文案都准确。缺的是**兜底路径**（F-23） |
| `mysql.error_log_summary: unsupported，performance_schema.error_log unavailable` | **正确。** 该表是 8.0.22+ 才有，5.7 没有。同理缺兜底路径（F-22） |

这两条证明：**表级能力探测在"表真的不存在"时工作正常**。它失效的场景是"表存在、列不存在"——正是 F-20。

### 0.1.3 实测新暴露的 7 条（F-20 ～ F-26）

| 编号 | 级别 | 实测输出 | 根因 | 批 |
|:---:|:---:|---|---|:---:|
| F-20 | P1 | `mysql.replication_workers: unsupported，ERROR 1054 Unknown column 'LAST_APPLIED_TRANSACTION'`；`mysql.group_replication_members: unsupported，ERROR 1054 Unknown column 'MEMBER_ROLE'` | SQL 含 8.0 专有列；探测只到表级，5.7 上表存在 → 探测通过 → SQL 1054 → **整项报废** | 1 |
| F-21 | P1 | 上面两项被记成 `unsupported`（与真不支持的 `data_lock_waits` 撞同一状态） | `classify_failure` L206 把 `unknown column` 归入 `unsupported` → 下游分不出"环境不支持"和"脚本 SQL 写错" | 1 |
| F-24 | P1 | `mysql.table_io_top: timeout 30.010s`；`mysql.unused_indexes: timeout 30.010s` | 30 秒全局超时（L26）不够；`sys.schema_unused_indexes` 内部是 `information_schema.statistics` LEFT JOIN pfs index-usage，5.7 的 I_S 要扫 `.frm`，大表基数下必然超时 | 1 |
| F-22 | P1 | `mysql.error_log_summary: unsupported` | 5.7 的错误日志是**文件**（`@@log_error`），脚本只走 pfs 表、**无文件兜底** → 5.7 上该节结构性永久为空 | 3 |
| F-23 | P2 | `mysql.data_lock_waits: unsupported` | 5.7 的锁等待在 `information_schema.innodb_lock_waits`，脚本无兜底 | 3 |
| F-25 | P2 | sar 范围标为 `2026-09-13 16:10:01 **UTC**`，而采集开始为 `2026-09-16T15:12:29**+08:00**` | L663 `sadf -d` 未加 `-t`/`-T`，默认按 UTC 输出；包内无时区声明 | 3 |
| F-26 | P2 | `历史 sar 请求范围: 最近 24 小时；约覆盖 63.00 小时` | 设计使然（L663 无时间过滤，裁剪交 Python，L723 reason 已写明），但 summary 文案（L1144）会让使用者误以为只采了 24 小时 | 3 |

### 0.1.4 关键旁证：这个实例已在超时边缘

同一份实测里，其它重查询的耗时（均未超时）：`mysql.no_primary_key` 6.25s、`mysql.no_primary_key_summary` 5.45s、`mysql.redundant_indexes` 8.33s、`module.mysql_capacity` 15.70s。

这些全在 30 秒内，但**余量只剩一半**——说明该实例表基数较大，F-24 的两个超时不是偶发，而是"必然踩线"。**修掉这两个 timeout 后，总耗时可从 94.9 秒降到约 35 秒**（省下的 60 秒就是两次空等）。

### 0.1.5 这一轮仍需你提供的材料

`成功但无数据: 14`（占 80 项的 17.5%）偏高。**`empty` 在契约里是正常状态**（5.7 上 pfs 数据少、无长事务、无锁等待都会空），但**无法从 summary 判断这 14 项具体是哪些**。请提供 `collection_status.json`，才能确认其中有没有真缺陷。当前不建议靠猜。

---

## 0. 清单汇总

| 编号 | 级别 | 位置 | 问题 | 批次 |
|:---:|:---:|---|---|:---:|
| F-01 | P1 | `mysql_exec` L280 | `--batch --raw` 关闭字段转义，自由文本撑破 TSV 行结构 | 1 |
| F-02 | P1 | `collect_mycnf_allowlist` L471-485 | 不展开 `!includedir`、不分 section、状态硬编码 `ok` | 1 |
| F-03 | P1 | `known_tsv_header` L223-225 | 三处表头与实际列名漂移 | 1 |
| F-04 | P1 | `collect_mysql_basic` L763-765 | 回退 `SHOW GLOBAL VARIABLES` 无敏感键过滤 | 2 |
| F-05 | P1 | 主流程 L1350-1373 | 敏感扫描拦截后仍 `exit 0` | 2 |
| F-06 | P1 | `collect_mysql_replication` L849 | 白名单丢掉复制错误原文三列 | 2 |
| F-07 | P2 | `sanitize_tsv_columns` L741-760 | 按 `-F'\t'` 逐行过滤，续行产错行（随 F-01 消失） | 1 |
| F-08 | P2 | `mysql_query_tsv` L302 | `row_count` 被多行值抬高（随 F-01 消失） | 1 |
| F-09 | P2 | L27 / L28 / L941 / L944 | `TOP_N`、`MIN_FREE_MB`、错误日志窗口无 CLI 开关 | 3 |
| F-10 | P2 | L1040 | `awk ... $files` 未加引号，路径含空格时 JSON 损坏 | 1 |
| F-11 | P2 | L1307 | 明文密码 cnf 落在任务目录内 | 2 |
| F-12 | P2 | L1347-1363 | 全量 awk 统计跑两遍 | 3 |
| F-13 | —— | 无 | 缺备份产物扫描 | 3 |
| F-14 | —— | 无 | 缺慢日志内容 | 3 |
| ~~F-15~~ | ~~P1~~ | L692/L696 vs L1100 | **【已撤回，误报】** 两个覆盖率文件都存在，snapshot 引用正确 | — |
| F-16 | P2 | L532 vs L1099 | `capabilities` 在 tsv 里是 `0/1`，在 snapshot.json 里是 `true/false` | 1 |
| F-17 | P3 | L421-428 | **【已降级】** `ip`/`ifconfig` 是互斥分支，不会覆盖；实为输出文件命名误导 | 1 |
| F-18 | P1 | `known_tsv_header` L228/L229 | 表头硬编码 `LAST_ERROR_MESSAGE_SHA256`，而实际列名随 `--include-log-text` 变化 | 1 |
| F-19 | P2 | `collect_mysql_logs_backup` L945 / `generate_summary` | 错误日志原文未采集时只落一个 `skipped`，下游无法区分"没采"和"没错误" | 2 |
| F-20 | P1 | L879/L882/L888 | 5.7 上 `replication_workers`、`group_replication_members` 因 SQL 含 8.0 专有列而**整项报废**（实测 1054） | 1 |
| F-21 | P1 | `classify_failure` L206 | `unknown column` 被归入 `unsupported`，与"环境真不支持"撞同一状态 | 1 |
| F-22 | P1 | `collect_mysql_logs_backup` L940-947 | 错误日志只走 pfs 表（8.0.22+），5.7 上**无文件兜底** → 该节永久为空 | 3 |
| F-23 | P2 | L815-818 | 锁等待只走 `data_lock_waits`（8.0），5.7 无 `information_schema.innodb_lock_waits` 兜底 | 3 |
| F-24 | P1 | L830-836 + L26 | `unused_indexes` / `table_io_top` 在 5.7 大表基数下必然超 30 秒，**报告核心两节为空 + 白耗 60 秒** | 1 |
| F-25 | P2 | L663 + L675 | sar 时间戳按 UTC 输出、采集时间戳为本地 +08:00，包内无时区声明 → Python 混画会错位 8 小时 | 3 |
| F-26 | P2 | L1144 / L723 | summary"请求 24 小时 / 覆盖 63 小时"文案误导（实为只导全量原始数据、裁剪交 Python） | 3 |
| F-27 | P2 | 全脚本 | 1373 行 / **73 个函数** / 仅 **18 行**注释（**1.3%**），**零函数级说明**；主流程 L1227-1373 是裸代码、无 `main()` 包裹 | **0** |
| F-28 | P2 | `collect_mysql_trx` L1230 | `query_sample` 保留裸 CR(0x0D)：MySQL batch 转义集合是 `\n` / `\t` / `\0` / `\\`，**不含 `\r`**；`processlist`(L1243) 已做 `REPLACE`，此项漏了 | 4 |
| F-29 | P1 | `collect_mysql_basic` L1189 | 主查询走 `performance_schema.global_status`，该表**按设计不含 `Com_xxx`**（手册 10.14 / Bug #87645 = by design）→ 快照缺 TPS 必需项，下游静默出 `nan` | 4 |
| F-30 | —— | `collect_mysql_security_objects` L1342 + 契约 | **【非缺陷 · 契约澄清】** `mysql.user.Host` 可为空字符串，官方语义（8.2.6）**等价于 `%`（any host）**；须在 `INTERFACE.md` 写明，分析层并档处理 | 4 |
| F-31 | P2 | `generate_snapshot_json` L1531 | `ntp_synchronized` 用 `awk '/System clock synchronized/'` 匹配，但 systemd ≥239 的 `timedatectl status` 输出是 `NTP synchronized`，字段名对不上 → 三节点该字段全为空，`snapshot.json#time_evidence` 拿到空值 | 5 |

> 结构调整（不阻塞上线，建议随 schema 2.0 一起做）：
> - `system.mycnf_allowlist` 属于数据库配置，现挂在 `system.static` 下（L452 由 `collect_system_static` 调用）
> - `system.backup_cron/timers/processes` 的 item_id 前缀、category（`mysql.backup`）、模块名（`mysql_logs_backup`）三套不一致
> - `collect_mysql_security_objects` 同时产出 `mysql.security.*` 与 `mysql.objects.*`
> - `collect_mysql_logs_backup` 同时产出 `mysql.logs.*` 与 `mysql.backup.*`
>
> 这四处都涉及 `item_id` 变更，**是破坏性变更**，别在批 1/2 里顺手改。

---

## 0.5 脱敏边界：谁必须脱、谁绝不能脱（新增采集项时照此判断）

包要离开客户机房（tar.gz 回传，走介质 / 网络 / 公司资料库），凭证必须处理；但报告要靠数据立论，所以**"值"可以削，"事实"不能削**。

| 分区 | 内容 | 处理 | 报告依赖度 |
|---|---|---|---|
| 凭证类 | `wsrep_sst_auth`、ssl/rsa 私钥、`-pXXX` / `--password=`、`Authorization: Basic/Bearer` | **必须脱**（F-04 补漏，`security_scan` 兜底） | 零。报告从不写密码，只需"是否存在明文传参"这一事实 |
| 诊断类 | 复制 `Last_Error` / `Last_IO_Error` / `Last_SQL_Error` 原文与错误号 | **必须留**（F-06 现在把它裁掉了——那是 bug，不是脱敏） | 高。报告 4.2 节"主从中断，需重新搭建"的全部依据在此 |
| 结构化与指标 | 库表名、容量、GTID、位点、延迟、慢 SQL digest 与计数、时序指标 | 不脱敏 | 极高，占报告正文约八成 |
| 可能含字面量（日志） | `error_log_samples`、`error_log_tail`、慢日志原文 | **按开关**（沿用 `--include-log-text`，默认关，见 §0.6 决策 3） | 中。报告 4.6 / 5.2 节与故障复盘需要 |
| SQL 原文 | `processlist.SQL_TEXT`（L821）、`long_transactions.query_sample`（L808） | **不脱敏、不加开关**（见 §0.6 决策 1） | 高。阻塞与长事务分析的唯一抓手。注意 `sql_digests_top.digest_text`（L823）取的是 `DIGEST_TEXT`，MySQL 已把字面量归一化为 `?`，**不是原文、无隐私风险** |
| 拓扑标识 | 主机 / IP / 端口 / server_id / UUID | 不脱敏 | 高。拓扑判断与唯一性校验的唯一依据 |

三条判据：

1. **这个值是不是密钥？** 是 → 必脱。
2. **这个值是不是服务器自己生成的诊断文本**（不含用户 SQL 字面量）？是 → 保留明文。`LAST_ERROR_MESSAGE` 属此类，它跟 `error_log_samples` 不该共用一个门槛。
3. **脱掉之后，报告里那句话还写不写得出来？** 写不出来 → 不能脱，改"带外置放"：完整值进 `evidence/`，结构化表里只放截断值。

**现状的两个反向错误**（本次改动就是双向校准，不是"再加一层脱敏"）：

- 该脱的没脱（**F-04**）：回退的 `SHOW GLOBAL VARIABLES` 零过滤，MariaDB/PXC 上 `wsrep_sst_auth` 明文落盘，而 `security_scan` 认不出 `user:pass` 形式 → 门禁放行。
- 不该削的削了（**F-06**）：`sanitize_tsv_columns` 白名单把三个错误原文列直接裁掉（连哈希都没留），复制故障只剩一个 `13114`。

---

## 0.6 三项脱敏决策定论（2026-09-20 与使用者确认）

| # | 决策项 | 定论 | 落点 |
|:---:|---|---|---|
| 1 | `sql_text_included` 恒为 `true` 且无开关 | **保持恒开，不加开关**。与 Oracle 工具链一致——SQL 文本是有意始终采集的。`snapshot.json` L1102 的 `true` 保持字面量写法 | 不改代码。`INTERFACE.md` 需声明"该字段恒为 `true`，Python 不要期待它变化" |
| 2 | 复制错误原文的脱敏粒度 | **明文放全文，500 字符上限**。不做 `LEFT(...,200)` 截断降级 | F-06 第 3 步 |
| 3 | `--include-log-text` 默认值 | **维持默认关**；但 `error_log_samples` 记 `skipped` 时，必须在 `summary.txt` 与报告顶部显式提示"错误日志原文未采集"，避免被读成"本机没有错误" | 新增 **F-19** |

**决策 2 的依据**（写下来避免以后重复讨论）：`sanitize_tsv_columns` 的白名单里**本来就保留了 `Source_Host` / `Master_Host` / `Source_Port` / `Master_Port`**，对端 IP 与端口本来就在包里——卡掉错误原文**一个敏感信息都挡不住**。原文唯一新增的是复制账号名（`'repl@192.168.1.33:3306'` 里的 `repl`），而账号名不是凭证，`password_included:false` 的声明仍然成立。反过来 `LEFT(...,200)` 恰好保留了最敏感的前缀、砍掉的却是真正的错误原因，是净损失。

**决策 3 的依据**：默认关本身是对的（MySQL 会把带数据的 SQL 打进错误日志，客户生产场景默认采原文风险高）。4.6 节"看起来是空的"的真正成因不是默认值，而是**脚本没告诉下游它没采**——所以改提示，不改默认值。

**决策 1 的一个附带事实**：SQL 原文的实际暴露面只有两处（L808 长事务、L821 活动会话），`sql_digests_top` 取的 `DIGEST_TEXT` 已被 MySQL 归一化为 `?`。所以"恒开"带来的隐私面比字面上看小得多。

---

## 批 0：注释补全（零逻辑改动，建议最先做）

### F-27 全脚本注释补全【P2 · 零逻辑改动】

**实测统计（本次核对）**

| 指标 | 数值 |
|---|---|
| 总行数 | 1373 |
| 顶层函数 | **73 个** |
| 纯注释行 | **18 行**（密度 **1.3%**） |
| 有函数级说明的函数 | **0 个** |
| 主流程 | **L1227-1373 是裸代码，无 `main()` 包裹**，7 个阶段只有一个 `# ---- 参数与入口 ----` 标题可辨认 |

那 18 行注释**全部是踩坑记录**，一条都不该删：chronyc 错误流向 stdout、空结果时 mysql 不输出列名、新内核 `/proc/diskstats` 字段追加、GTID 值跨行、sed 转义。

结论：**难点都注释了，"这个函数干什么、输出什么、谁消费"一行没写。** 而这恰好是维护和 Python 对接最需要的那一层——73 个函数对应 74 个 `item_id`，映射关系现在只能靠读 SQL 反推。

**为什么放在批 1 之前（三个理由）**

1. **本次要动 23 条，其中至少 4 条会让现有描述变成假的**：

| 现有注释/写法 | 哪条改动让它失真 | 要做的事 |
|---|---|---|
| L856-859「GTID 续行用 sed 拼接」的补丁说明 | F-01 去掉 `--raw` 后客户端自带转义 | **补丁和注释一起删**，注释写明"F-01 后由客户端转义，此处不再需要" |
| `record_status` 调用处附近关于状态写 `ok` 的口径 | F-02 要求空结果写 `empty` | 注释同步为"rows=0 必须写 empty，写 ok 是假 OK" |
| `sanitize_tsv_columns` 内关于按 `-F'\t'` 逐行过滤的说明 | F-01 后续行消失，过滤语义变化 | 注释改掉，说明它只对 `replica_status.tsv` 生效 |
| L1102 `sql_text_included:true` | §0.6 决策 1 定论恒开 | 旁边写死一句"**有意为之，勿改成变量**"，否则下轮审查（或下一次 AI 会话）会再提一次"加开关" |

2. **零逻辑改动 = 零风险**，不占批次顺序，随时可插
3. **补注释的过程本身就是契约自检**——逐个函数写 `item_id` + 输出文件时，F-03 / F-18 那类表头漂移会自己浮出来

**注释分 5 层，别全铺**

| 层 | 对象 | 数量 | 行/处 | 写什么 |
|---|---|---:|---:|---|
| L1 | 文件头 | 1 | ~50 | 定位（只采集不判定）、7 阶段流程、退出码表（按 F-05 口径）、全局约定（`umask 077` / `LC_ALL=C` / `timeout` 默认值）、依赖命令、隐私声明（§0.6 三条决策）、维护者须知（指向 `INTERFACE.md` §7） |
| **L2** | 阶段分隔 + 13 个 category 分隔 | ~20 | 4~6 | 阶段序号、参与模块、串行/并行、产出 category。**主流程必须补**，现在只有 1 个标题 |
| **L3** | 10 个模块入口函数头 | 10 | ~14 | 照 `INTERFACE.md` §9 模板：category / 产出项 / 依赖能力 / 输出位置 / 注意 / **不做** |
| **L4** | 采集引擎函数头 | 10 | ~8 | `mysql_exec`、`mysql_exec_raw`(F-01 新增)、`mysql_query_tsv`、`mysql_query_fallback`、`record_status`、`capture_command`、`classify_failure`、`known_tsv_header`、`ensure_tsv_header`、`sanitize_tsv_columns`——**这几个改一处影响 74 项**，必须写清参数、stdout 契约、返回码、副作用（写哪个文件） |
| L5 | 其余函数头 | 52 | 2 | 一行：用途 + 输出文件（产 item 的写 `item_id`） |
| — | 行内注释 | — | ~30 | 只写"**为什么**这么写"。禁止写"给变量赋值""循环采样"这类复述代码的注释 |

**量级**：新增约 **300~400 行** → 总行数约 1680~1780，密度 **1.3% → 约 20%~23%**。这是 shell 脚本的健康区间；**不建议做到 30% 以上**，注释会淹没逻辑。

不必一次铺满：**L1 + L2 + L3 + L4 是必做**（约 300 行），L5 那 52 个函数头每个只值一行、成本极低，顺手做掉。

**F-27 的硬验收（这条最关键）**

"零逻辑改动"不能靠嘴说，用命令证明：

```bash
# 改之前先留一份（在 inspection/ 目录内执行——脚本已从仓库根移入该目录）
cd inspection
cp mysql_inspection_standard.sh mysql_inspection_standard.sh.bak

# ……只加注释……

# 剥掉所有整行注释与空行，比对剩余逻辑的指纹
strip() { grep -v '^[[:space:]]*#' "$1" | grep -v '^[[:space:]]*$'; }
diff <(strip mysql_inspection_standard.sh.bak) <(strip mysql_inspection_standard.sh) \
  && echo "逻辑零改动 ✓" || echo "夹带了逻辑改动 ✗"
```

- 指纹 **diff 必须为空**。任何一行差异 = 这次不是纯注释批次，退回重做
- `bash -n inspection/mysql_inspection_standard.sh` 通过
- `grep -c '^[[:space:]]*#' 文件 / 总行数` **≥ 0.20**
- 抽查：任取一个模块入口（如 `collect_mysql_replication`），其上方 3 行内必须有 L3 注释块

**与其他条目的关系**

- 与 F-01 ~ F-26 **无代码冲突**（只加注释），顺序上前置收益最大
- F-05 定了退出码（`0` / `20` / `30` / `31`）之后，**L1 文件头的退出码表要照它写**。建议拆两步：先用 F-27 做 L2/L3/L4/L5，**退出码表等 F-05 落地后一次性补进 L1**，避免写两遍
- F-19 与 §0.6 三条决策，都必须在 L3/L4 注释里写明"**有意为之，勿改**"

---

## 批 1：数据正确性　✅ 已完成（2026-09-20）

### F-01 `--batch --raw` 破坏 TSV 行结构【P1】

**现状（L279-281）**

```bash
mysql_exec() {
    run_with_timeout "$MYSQL_TIMEOUT_SECONDS" "$MYSQL_BIN" "${MYSQL_CONN_ARGS[@]}" --batch --raw "$@"
}
```

`--raw` 的作用是**关闭 batch 模式的特殊字符转义**。含 `\n` / `\t` 的值会以裸字符落盘，直接撑破 TSV：

- `SHOW REPLICA STATUS` 的 `Executed_Gtid_Set` —— 客户那份报告里它就跨了 3 行
- `SHOW ENGINE INNODB STATUS` —— 必然多行
- `long_transactions.query_sample`（L808 没做 `REPLACE`，而同脚本 L821 的 processlist 做了）
- `error_log_samples.message`（L944）

连带两处：`sanitize_tsv_columns`（F-07）与 `row_count`（F-08）都会跟着错。

内部证据：L856-859 你已经为 `binary_log_status.tsv` 单独写了续行拼接的 `sed`——说明你见过这个现象，只是漏了 `replica_status.tsv`。

**改法**

```bash
# 普通查询：保留 batch 模式的客户端转义（\t \n \\），确保 TSV 行结构不被自由文本破坏
mysql_exec() {
    run_with_timeout "$MYSQL_TIMEOUT_SECONDS" "$MYSQL_BIN" "${MYSQL_CONN_ARGS[@]}" --batch "$@"
}

# 仅用于 SHOW ENGINE INNODB STATUS：需要保留原始换行，单独落 .txt
mysql_exec_raw() {
    run_with_timeout "$MYSQL_TIMEOUT_SECONDS" "$MYSQL_BIN" "${MYSQL_CONN_ARGS[@]}" --batch --raw "$@"
}
```

给 `mysql_query_tsv` 加一个可选 raw 开关（默认关）：

```bash
mysql_query_tsv() {
    local item_id="$1" category="$2" outfile="$3" query="$4" raw_mode="${5:-}"
    ...
    if [ "$raw_mode" = "raw" ]; then
        mysql_exec_raw --column-names -e "$query" > "$outfile" 2> "$err"; rc=$?
    else
        mysql_exec      --column-names -e "$query" > "$outfile" 2> "$err"; rc=$?
    fi
```

`collect_mysql_basic` 里 InnoDB status 改为 `.txt` 并走 raw：

```bash
mysql_query_tsv "mysql.innodb_status" "mysql.basic" "$EVIDENCE_DIR/innodb_status.txt" \
    "SHOW ENGINE INNODB STATUS" raw || true
```

删除 L856-859 的 `sed` 续行块（改成转义后不再有裸换行，`sed` 变空转）。

**影响 / 风险**

- 落盘语义变化：自由文本值里现在是 `\n` `\t` `\\` 三个字面量。**Python 侧读取时需 unescape**（两个字符 `\n` → 真换行）。不改 Python 会看到字面量 `\n`——**但比现在读到错行更好**：现在 Python 拿到的数据本身就是坏的
- `innodb_status.tsv` → `innodb_status.txt`，Python 侧路径要同步改
- 客户那份报告里 `'S'` / `N` 的字面量替换**不是 mysqldumpslow 的行为**，说明 SQL 进服务器前已被中间件/审计设备做过替换；脚本不需要自己做这一步，原样落盘即可

**验收**：采集后 `awk -F'\t' 'NF!=<列数>' replica_status.tsv` 无输出；`grep -c '' innodb_status.txt` 与文件内实际行数一致。

---

### F-02 my.cnf 白名单：`!includedir` 不展开 + 假 OK【P1】

**现状（L471-485）**

awk 只做的三件事：跳注释、跳 `[section]`、按白名单正则取值。问题有三：

1. **不展开 `!include` / `!includedir`** —— RHEL 家族 `/etc/my.cnf` 的内容就是一行 `!includedir /etc/my.cnf.d`，真实参数在 `/etc/my.cnf.d/mysql-server.cnf`。客户那份报告 4.1 的 `/etc/my.cnf` 结尾正是 `!includedir /etc/my.cnf.d`
2. **不分 section** —— `[client]` 与 `[mysqld]` 同名键（`port`）会输出两行，Python 取值有歧义
3. **状态硬编码** —— L485 无条件写 `"ok"`，白名单为空也报 ok → 假 OK

另外白名单本身漏了现场实际在用的键：`max_allowed_packet`、`max_connect_errors`、`default-authentication-plugin`、`mysqlx_socket`、`replica_skip_errors`、`log-bin`、`relay_log`、`pid-file`、`character-set-client-handshake`、`lower_case_table_names`（已有）。

**改法**：整段替换为递归展开版，输出加 `source_file` 与 `section` 两列。

```bash
collect_mycnf_allowlist() {
    local cnf_path="" defaults_file="${MYSQL_CNF:-}" datadir basedir
    datadir=$(mysql_scalar 'SELECT @@GLOBAL.datadir' 2>/dev/null)
    basedir=$(mysql_scalar 'SELECT @@GLOBAL.basedir' 2>/dev/null)
    local paths=()
    [ -n "$defaults_file" ] && paths+=("$defaults_file")
    [ -n "$datadir" ] && paths+=("${datadir%/}/my.cnf")
    [ -n "$basedir" ] && paths+=("${basedir%/}/etc/my.cnf" "${basedir%/}/my.cnf")
    paths+=(/etc/my.cnf /etc/mysql/my.cnf /usr/local/mysql/etc/my.cnf /usr/local/etc/my.cnf)
    local p
    for p in "${paths[@]}"; do [ -f "$p" ] && [ -r "$p" ] && { cnf_path="$p"; break; }; done
    if [ -z "$cnf_path" ]; then
        printf 'source_file\tsection\tparameter\tconfigured_value\n' > "$TABLES_DIR/mycnf_allowlist.tsv"
        record_status "system.mycnf_allowlist" "system.static" "empty" "$(iso_now)" "$(iso_now)" 0 0 0 \
            "tables/mycnf_allowlist.tsv" "no readable config file found"
        return 0
    fi

    local -a stack=("$cnf_path") seen=()
    local f inc g
    {
      printf 'source_file\tsection\tparameter\tconfigured_value\n'
      while [ ${#stack[@]} -gt 0 ]; do
        f="${stack[0]}"; stack=("${stack[@]:1}")
        # 去重，防 A include B、B include A 死循环
        case " ${seen[*]-} " in *" $f "*) continue ;; esac
        seen+=("$f")
        [ -r "$f" ] || continue

        # 收集 include 目标，压到栈尾（BFS；与 MySQL 的就地展开不同，见下方风险）
        while IFS= read -r inc; do
          [ -z "$inc" ] && continue
          case "$inc" in
            */) for g in "$inc"*.cnf; do [ -f "$g" ] && stack+=("$g"); done ;;
            *)  [ -f "$inc" ] && stack+=("$inc") ;;
          esac
        done < <(awk 'BEGIN{IGNORECASE=1}
                      /^[[:space:]]*[#;]/ {next}
                      /^[[:space:]]*![[:space:]]*include(dir)?[[:space:]]+/ { sub(/^[[:space:]]*![[:space:]]*include(dir)?[[:space:]]+/,""); gsub(/[[:space:]]+$/,""); print }' "$f")

        awk -v src="$f" -v OFS='\t' '
          BEGIN{IGNORECASE=1; sect=""}
          /^[[:space:]]*[#;]/ || /^[[:space:]]*$/ {next}
          /^[[:space:]]*\[/ { s=$0; gsub(/^[[:space:]]*\[/,"",s); gsub(/\].*$/,"",s); sect=tolower(s); next }
          {
            if (sect != "mysqld" && sect != "mysqld_safe" && sect != "server") next
            line=$0; key=line; sub(/[[:space:]]*=.*/,"",key); gsub(/[[:space:]]/,"",key)
            gsub(/-/,"_",key)   # my.cnf 里 log-bin / pid-file / transaction-isolation 混用连字符，必须归一再匹配
            if (key ~ /^(port|socket|basedir|datadir|tmpdir|pid_file|server_id|bind_address|max_connections|max_connect_errors|max_allowed_packet|back_log|open_files_limit|table_open_cache|table_definition_cache|thread_cache_size|wait_timeout|interactive_timeout|skip_name_resolve|character_set_server|collation_server|character_set_client_handshake|lower_case_table_names|sql_mode|transaction_isolation|default_storage_engine|default_authentication_plugin|mysqlx_socket|performance_schema|slow_query_log|long_query_time|log_output|log_error|general_log|log_bin|binlog_format|sync_binlog|binlog_expire_logs_seconds|expire_logs_days|gtid_mode|enforce_gtid_consistency|read_only|super_read_only|relay_log|relay_log_recovery|replica_skip_errors|innodb_buffer_pool_size|innodb_buffer_pool_instances|innodb_redo_log_capacity|innodb_log_file_size|innodb_log_files_in_group|innodb_log_buffer_size|innodb_flush_log_at_trx_commit|innodb_flush_method|innodb_io_capacity|innodb_io_capacity_max|innodb_read_io_threads|innodb_write_io_threads|innodb_page_cleaners|innodb_purge_threads|innodb_file_per_table|innodb_doublewrite|innodb_autoinc_lock_mode|tmp_table_size|max_heap_table_size|sort_buffer_size|join_buffer_size|read_buffer_size|read_rnd_buffer_size)$/) {
              val=line; sub(/^[^=]*=/,"",val); gsub(/^[[:space:]]+|[[:space:]]+$/,"",val)
              print src, sect, key, val
            }
          }' "$f"
      done
    } > "$TABLES_DIR/mycnf_allowlist.tsv"

    printf '%s\n' "$cnf_path" > "$EVIDENCE_DIR/mycnf_path.txt"
    printf '%s\n' "${seen[@]}" > "$EVIDENCE_DIR/mycnf_includes.txt"

    local rows status
    rows=$(awk 'END{print (NR>0?NR-1:0)}' "$TABLES_DIR/mycnf_allowlist.tsv")
    status="ok"; [ "$rows" -eq 0 ] && status="empty"
    record_status "system.mycnf_allowlist" "system.static" "$status" "$(iso_now)" "$(iso_now)" 0 "$rows" 0 \
        "tables/mycnf_allowlist.tsv" "$([ "$rows" -eq 0 ] && printf 'config file(s) readable but no allowlisted key in [mysqld]' || printf '')"
}
```

**`key` 归一（已并入上面的代码，这是本条的要害）**：my.cnf 里连字符与下划线是混用的。客户那份就是 `transaction-isolation`、`default-authentication-plugin`、`default-storage-engine`、`log-bin`、`pid-file` 与 `innodb_buffer_pool_size`、`max_allowed_packet` 混写。上面那行 `gsub(/-/,"_",key)` **必须在白名单匹配之前执行**——否则 `log-bin` 匹配不到 `log_bin`，白名单会大面积落空，而症状和"没展开 include"一模一样，极难定位。

输出口径：`parameter` 列是**归一后的下划线形式**（`log_bin`，不是 `log-bin`），Python 侧按归一名匹配。

**影响 / 风险**

- 表结构从 2 列变 4 列，**Python 侧解析 `mycnf_allowlist.tsv` 必须同步改**（按列名取，别按位置）
- **展开次序与 MySQL 不一致（更正）**：初版这里写成"保证与被包含文件读取顺序一致"，是错的。我的实现是队列（BFS）——根文件的 include 先全量入栈、再依次展开；MySQL 是**就地展开**（DFS，`!include` 出现在哪一行就在哪一行插入）。**只有同一个参数在多个文件里都出现才有影响**（MySQL 后者胜，我这里是栈顺序胜）。本条的用途是"哪些参数被显式配置过"，最终生效值以 `global_variables.tsv` 为准，所以这个差异可接受；但**不要把这里的 `configured_value` 当成最终生效值**，也别让 Python 按文件顺序去推断"最后一个覆盖"。
- `parameter` 列是归一后的下划线名，`configured_value` 保留原样（含引号）
- `!include` 的相对路径 MySQL 是按进程 CWD 解析的；这里只按绝对路径处理，相对路径会 `[ -f ]` 失败并静默跳过——建议在 `mycnf_includes.txt` 里记下来，让 Python 能看到"有 include 没展开"
- 新增 `evidence/mycnf_includes.txt`，需加入 Python 的 evidence 清单（不影响 manifest，manifest 是遍历全目录）

**验收**：在 RHEL/CentOS 目标机上跑完，`mycnf_allowlist.tsv` 行数 > 1，且 `source_file` 列出现 `/etc/my.cnf.d/*.cnf`。

---

### F-03 表头与实际列名漂移 3 处【P1】

**现状**

| 位置 | `known_tsv_header` 写的 | 实际 SQL 取出的 |
|---|:---:|:---:|
| L223 `long_transactions.tsv` | 末列 `query_sha256` | `query_sample`（L808） |
| L224 `processlist.tsv` | 末列 `SQL_SHA256` | `SQL_TEXT`（L821） |
| L225 `sql_digests_top.tsv` | 9 列，无 `digest_text` | 10 列，末列 `digest_text`（L823） |

只在这三个查询**返回空结果**（`ensure_tsv_header` 写占位表头）时暴露。但那时 Python 拿到的 schema 与正常情况不一致——按列名取值的代码会直接 `KeyError`，而且 `sanitize_tsv_columns` 也会因为找不到 `SQL_TEXT` 而整文件 `exit 2`（L751）导致不裁剪。

**改法**

```bash
      long_transactions.tsv) printf 'trx_id\ttrx_state\tduration_seconds\ttrx_rows_locked\ttrx_rows_modified\ttrx_tables_locked\ttrx_mysql_thread_id\tquery_sample' ;;
      processlist.tsv) printf 'ID\tUSER\tHOST\tDB\tCOMMAND\tTIME\tSTATE\tSQL_TEXT' ;;
      sql_digests_top.tsv) printf 'schema_name\tDIGEST\tCOUNT_STAR\ttotal_seconds\tavg_seconds\tSUM_ROWS_EXAMINED\tSUM_ROWS_SENT\tSUM_NO_INDEX_USED\tSUM_NO_GOOD_INDEX_USED\tdigest_text' ;;
```

**建议加一道防线**：写占位表头的逻辑改成"从实际查询结果推断列名"太麻烦，不如加一个自检——`ensure_tsv_header` 写完之后，如果该文件已有非空内容，比对第一行与 `known_tsv_header` 是否一致，不一致就 `log_warn` 并把这条写进 `collection_status.json`。这样以后改 SQL 忘了改表头会被自己发现。

**验收**：拿一个空 schema 的实例跑一次，三个文件的表头与实际 SQL 的 `information_schema` 列数一致。

---

### F-07 / F-08（随 F-01 自动消失）

修完 F-01 后：

- `sanitize_tsv_columns`（L741-760）不再遇到续行，逐行过滤安全
- `row_count`（L302 `awk 'END{print (NR>0?NR-1:0)}'`）不再被多行值抬高

**不用单独动。** 但建议给 `sanitize_tsv_columns` 的 `exit 2` 分支加一条记录：现在 L751 如果白名单列一个都没命中，函数静默返回，文件**不做裁剪**——即敏感列被原样保留，且 `record_status` 不知道。改成返回非零并让调用方 `log_warn`。

### F-10 `awk ... $files` 未加引号【P2】

**现状（L1040）**

```bash
      }' $files > "$COLLECTION_STATUS_FILE"
```

`--output-dir` 含空格时 `$files` 词分割出错，`collection_status.json` 直接损坏。

**改法**

```bash
      }' $files > "$COLLECTION_STATUS_FILE"        # 改为下面这行
```
把 `$files` 从变量展开改为从这里读：

```bash
      }' ${files:+"$files"} > "$COLLECTION_STATUS_FILE"
```
或者更稳的写法——把 `files` 改成数组，传 `"${files[@]}"`；或者保持字符串但用 `-f -` 走 stdin：

```bash
    cat $files 2>/dev/null | awk '...' > "$COLLECTION_STATUS_FILE"
```
（这里 `$files` 在 `cat` 里展开同样有词分割问题，但因为都是路径、正常情况下无空格，风险低于让它破坏 JSON。**推荐改成数组**，见下：）

```bash
    local -a files=()
    for f in "$STATUS_PARTS_DIR"/*.tsv; do [ -f "$f" ] && files+=("$f"); done
    awk '...' "${files[@]}" > "$COLLECTION_STATUS_FILE"
```

---

### F-15 ~~snapshot.json 声明的覆盖率文件不存在~~【已撤回，误报】

**撤回理由**：`compute_sar_coverage` 同时写了**两个**文件——`coverage.tsv`（L692）与 `coverage.json`（L696）。`snapshot.json` 引用的 `history/coverage.json` **是存在的**，Python 不会 404，也不存在"静默降级成无 sar 历史"。初版把 L692 那行当成了唯一输出，漏看了紧随其后的第二个重定向。

**残留的 P3（文档口径，不是 bug）**：两个文件语义重叠（一个是 TSV 键值对，一个是等价的 JSON 对象）；`snapshot.json` 只登记了 JSON，`history/source_files.txt` 没登记。建议在 `INTERFACE.md` 里写死**以 `coverage.json` 为准**，并把 `source_files.txt` 也登记进 `sar_history` 对象：

```bash
"...\"sar_history\":{\"status\":%s,\"coverage_hours\":%s,\"first_timestamp\":%s,\"last_timestamp\":%s,\"coverage_file\":\"history/coverage.json\",\"source_files\":\"history/source_files.txt\"},..."
```

不进批 1，随 schema 2.0 一起做。

---

### F-16 capabilities 同事实两种类型【P2】

**现状**：`tables/capabilities.tsv` 里 `sar_command` 写 `0` / `1`（L532），`snapshot.json` 的 `capabilities.sar_command` 写 `false` / `true`（L1099）。

**改法**：Python 侧统一只认 `snapshot.json`；脚本侧不动（改 tsv 会连带改 `known_tsv_header` 与 Python 既有解析）。**在 INTERFACE.md 里写死"以 snapshot.json 为准"即可。**

---

### F-17 ~~两个采集项争用同一个输出文件~~【已降级为 P3，前提错误】

**撤回理由**：L421-428 是 `if has_cmd ip; then … elif has_cmd ifconfig; then … else …`，**两个分支互斥**。`ifconfig` 只在 `ip` 命令不存在时才执行，所以"两个命令都存在 → 后跑的把先跑的覆盖掉"这个前提不成立。不存在覆盖，`collection_status.json` 里也不会出现两条 item 指向同一文件。

**残留的真实问题（比原判断小得多，P3）**：

1. **命名误导**：`system.ifconfig` 的输出文件名仍叫 `ip_address.txt`，按文件名找 ifconfig 内容会找不到。
2. **回退不完整**：`ip` 命令存在但**运行失败**（权限、缺 `CAP_NET_ADMIN` 等）时，走的是 `if` 分支，`|| true` 把失败吞掉，不会退到 `ifconfig`。

**改法（可选，随 schema 2.0）**：回退路径改独立文件名。

```bash
        capture_command "system.ifconfig" "system.static" "$EVIDENCE_DIR/ifconfig.txt" ifconfig -a || true
```

---

### F-18 占位表头随运行参数漂移【P1】

**现状（L228/L229）**

```bash
replication_channels.tsv) printf '…\tLAST_ERROR_NUMBER\tLAST_ERROR_MESSAGE_SHA256\tLAST_ERROR_TIMESTAMP' ;;
replication_workers.tsv)  printf '…\tLAST_ERROR_NUMBER\tLAST_ERROR_MESSAGE_SHA256\tLAST_ERROR_TIMESTAMP\t…' ;;
```

而实际列名**取决于运行参数**：

| 分支 | 别名 | 位置 |
|---|---|---|
| 默认（无 `--include-log-text`） | `LAST_ERROR_MESSAGE_SHA256` | L872 / L882 |
| `--include-log-text` | `LAST_ERROR_MESSAGE` | L869 / L879 |

`known_tsv_header` 是**静态函数**，只写了前者。于是：

- `--include-log-text` **且该查询返回空结果** → 占位表头写成 `…_SHA256`，而正常采集写 `LAST_ERROR_MESSAGE` → Python 按列名取值 `KeyError`，按位置取值取错列
- 更麻烦的是：这**不是 F-03 那类"改了 SQL 忘改表头"的静态漂移**——一个静态表头**不可能**同时匹配两种模式。只补表头治不了，必须二选一

**改法（二选一）**

- **推荐**：做 F-06 第 3 步，把默认分支的 SHA2 去掉、两分支别名统一为 `LAST_ERROR_MESSAGE`，`known_tsv_header` 跟着改一次即可，本条自动消解
- 若决定保留 SHA2 分支：`known_tsv_header` 必须改成按 `INCLUDE_LOG_TEXT` 返回两套表头

**验收**：同一实例分别带/不带 `--include-log-text` 各跑一次，`replication_channels.tsv` 的第一行列名（无论是否空结果）一致。

---

## 批 1（续）：实测暴露的三条【2026-09-20 新增】　✅ 已完成

### F-20 5.7 上两个采集项因 8.0 专有列整项报废【P1】

**实测输出**

```
mysql.group_replication_members: unsupported，ERROR 1054 (42S22) at line 1: Unknown column 'MEMBER_ROLE' in 'field list'
mysql.replication_workers:       unsupported，ERROR 1054 (42S22) at line 1: Unknown column 'LAST_APPLIED_TRANSACTION' in 'field list'
```

**现状（L879 / L882 / L888）**

```bash
# replication_workers（默认分支，L882）
"...LAST_ERROR_TIMESTAMP,LAST_APPLIED_TRANSACTION,APPLYING_TRANSACTION FROM performance_schema.replication_applier_status_by_worker..."
# group_replication_members（L888）
"SELECT CHANNEL_NAME,MEMBER_ID,MEMBER_HOST,MEMBER_PORT,MEMBER_STATE,MEMBER_ROLE,MEMBER_VERSION FROM performance_schema.replication_group_members..."
```

`LAST_APPLIED_TRANSACTION` / `APPLYING_TRANSACTION` 是 **MySQL 8.0** 才加入的列；`MEMBER_ROLE` / `MEMBER_VERSION` 同属 8.0 的 MGR 表。**5.7 没有这些列，但这两张表在 5.7 上是存在的**——所以 L876、L510 的表级探测**通过了**，SQL 才走到执行阶段报 1054。

结果：SQL 失败 → `mysql_query_tsv` 清空输出 + 写占位表头 → **整项采集为 0 行**。而 5.7 这两张表的核心列（`CHANNEL_NAME` / `WORKER_ID` / `SERVICE_STATE` / `MEMBER_ID` / `MEMBER_HOST` / `MEMBER_STATE`）**全都有**。**因为多要 4 个列，丢掉两项采集。**

**版本分支的覆盖缺口**：脚本已有 `MYSQL_MAJOR` / `MYSQL_MINOR`（L499-500），但只用在了两处——L844（`SHOW REPLICA STATUS` vs `SHOW SLAVE STATUS`）与 L851（`SHOW BINARY LOG STATUS`，仅 8.4+）。上面这两处**没有版本判断**。

**改法一（推荐）：补列级探测，一次修到位**

```bash
mysql_column_exists() {
    local schema="$1" table="$2" column="$3" n
    n=$(mysql_scalar "SELECT COUNT(*) FROM information_schema.columns WHERE table_schema='${schema}' AND table_name='${table}' AND column_name='${column}'")
    [ "${n:-0}" -gt 0 ] 2>/dev/null
}
```

在 `probe_capabilities` 里新增两个探测（并写进 `capabilities.tsv` / `snapshot.json`）：

```bash
WORKER_APPLIED_TRX_AVAILABLE=0; mysql_column_exists performance_schema replication_applier_status_by_worker LAST_APPLIED_TRANSACTION && WORKER_APPLIED_TRX_AVAILABLE=1
GR_MEMBER_ROLE_AVAILABLE=0;    mysql_column_exists performance_schema replication_group_members      MEMBER_ROLE                 && GR_MEMBER_ROLE_AVAILABLE=1
```

采集时按开关拼列：

```bash
    local extra_worker=""
    [ "$WORKER_APPLIED_TRX_AVAILABLE" -eq 1 ] && extra_worker=",LAST_APPLIED_TRANSACTION,APPLYING_TRANSACTION"
    # 拼进 SELECT 列表末尾即可
```

**改法二（快，但不够稳）**：直接按 `MYSQL_MAJOR -ge 8` 判断。**不建议单独使用**——MariaDB 10.x 复用了同名表但列集合与 MySQL 8.0 不同，纯版本号判断在 MariaDB 上会误判。

**连带影响（Python 侧）**：5.7 上 `replication_workers.tsv` 比 8.0 少 2 列、`group_replication_members.tsv` 少 2 列。**Python 必须按列名取值、不能按下标**——这条已在 `INTERFACE.md` §7 列为硬规则。

**验收**：在 5.7 实例上重跑，这两项状态为 `ok` 或 `empty`（不再出现 1054）；`replication_workers.tsv` 首行不含 `LAST_APPLIED_TRANSACTION`，且数据行数 > 0。

---

### F-21 `unknown column` 被误分类为 `unsupported`【P1】

**现状（L206）**

```bash
elif grep -Eqi "doesn.t exist|unknown table|unknown system variable|unknown column|not supported|unsupported" "$err_file"; then
    printf 'unsupported'
```

`unknown column` 与 `unknown table` 被归进同一状态，但含义完全不同：

- `unknown table`（如 `performance_schema.data_lock_waits`）= **环境确实没有这个功能** → `unsupported` 正确
- `unknown column`（如 F-20 的 `MEMBER_ROLE`）= **表在、脚本 SQL 写错了** → 这是**脚本缺陷**，不是环境限制

实测里 4 项 `unsupported`，其中 **2 项属于后者**。**后果**：Python 侧看到 `unsupported` 会判定"该环境不可采集此项"，既不告警也不提示——**这个缺陷永远不会浮出水面**。

**改法**：把 `unknown column` 单独归类，并收窄 `unsupported` 的定义。

```bash
    elif grep -Eqi 'unknown column' "$err_file" 2>/dev/null; then
        printf 'schema_mismatch'
    elif grep -Eqi "doesn.t exist|unknown table|unknown system variable|does not exist|not supported|unsupported" "$err_file" 2>/dev/null; then
        printf 'unsupported'
```

**连带改动 4 处**（漏一处状态统计就会错）：

1. `record_status` 的状态取值集合新增 `schema_mismatch`（同步 `INTERFACE.md` §5.2 状态语义表）
2. `generate_summary`（L1111-1149）状态计数加一列
3. `generate_collection_status_json`（L1038）的 `summary` 对象加一个键 `schema_mismatch`
4. `error_count`（L1061）的统计口径（现为 `error|timeout|permission_denied`）**建议纳入 `schema_mismatch`**——它同属"需要人处理"的类别

**验收**：故意写一个不存在的列名，该项状态应为 `schema_mismatch`；`summary.txt` 与 `collection_status.json` 都能看到这个新状态。

---

### F-24 两项 30 秒超时让报告核心两节为空【P1】

**实测输出**

```
mysql.table_io_top      30.010 秒  timeout
mysql.unused_indexes    30.010 秒  timeout
```

**根因不是写法错，是超时值不够**

`unused_indexes` 用的是 `sys.schema_unused_indexes`（L831-832，**写法本身没问题**），但该视图的内部定义是：

```
information_schema.statistics  LEFT JOIN  performance_schema.table_io_waits_summary_by_index_usage
```

- 5.7 的 `information_schema.statistics` 是**虚拟表**，读取时要打开每张表的 `.frm`
- `table_io_waits_summary_by_index_usage` 在 pfs 中按 `表数 × 索引数` 存行

两者 JOIN 再排序，在表基数较大的实例上**超过 30 秒是常态而非偶发**。

**旁证**（同一次实测的其它重查询）：`no_primary_key` 6.25s、`no_primary_key_summary` 5.45s、`redundant_indexes` 8.33s、`mysql_capacity` 模块 15.70s——全在 30 秒内，但余量只剩一半，说明该实例已贴在超时边缘。

**影响两层**

1. **报告"未使用索引""表 IO TOP"两节为空**——这两项是 MySQL 巡检的核心产出，空掉等于报告少一半价值
2. **总耗时 94.9 秒里有 60 秒是空等**——修掉后约 35 秒

**改法：按项覆盖超时，不要调大全局值**

`MYSQL_TIMEOUT_SECONDS=30`（L26）是**全局单一值**。直接调大会让全部 74 项都变慢（失败项要等更久）。应让 `mysql_query_tsv` 支持逐项超时：

```bash
# 第 5 个参数可选：本项超时秒数，缺省用全局
# 【与 F-01 合并实施】这里同时把 --raw 去掉
mysql_query_tsv() {
    local item_id="$1" category="$2" outfile="$3" query="$4"
    local item_timeout="${5:-$MYSQL_TIMEOUT_SECONDS}"
    ...
    run_with_timeout "$item_timeout" "$MYSQL_BIN" "${MYSQL_CONN_ARGS[@]}" --batch --column-names -e "$query" > "$outfile" 2> "$err"; rc=$?
```

调用点给这两项单独放大：

```bash
    mysql_query_tsv "mysql.unused_indexes" "mysql.performance" "$TABLES_DIR/unused_indexes.tsv" \
      "SELECT object_schema,object_name,index_name FROM sys.schema_unused_indexes ORDER BY object_schema,object_name LIMIT 500" 300 || true

    mysql_query_tsv "mysql.table_io_top" "mysql.performance" "$TABLES_DIR/table_io_top.tsv" \
      "SELECT ... FROM performance_schema.table_io_waits_summary_by_table ..." 300 || true
```

配套新增 CLI 开关（并入 F-09）：`--slow-query-timeout SEC`，默认 300。

> **不要**改写成手写 `information_schema.statistics` JOIN —— 会丢掉 `sys` 视图里的对象类型过滤，且 5.7/8.0 对 `PRIMARY` 索引的处理有差异。**放大超时是风险最低、收益最直接的修法。**
>
> 若坚持要降低绝对耗时：给这两项加 `--slow-query-timeout 0`（视为不限时）或在报告侧接受长耗时。**不要**用 `LIMIT` 收窄——`sys` 视图的 `LIMIT` 是在 JOIN 之后生效的，不会减少扫描量。

**验收**：同实例重跑，这两项状态为 `ok`/`empty`（不再是 `timeout`）；`collection_status.json` 中可见 `duration_ms > 30000`；总耗时比 94.9 秒下降 55～60 秒。

---

## 批 2：安全与门禁　✅ 已完成（2026-09-20；F-12 随 F-05 一并落地）

### F-04 回退路径不过滤敏感变量【P1】

**现状（L763-765）**

```bash
    mysql_query_fallback "mysql.global_variables" "mysql.basic" "$TABLES_DIR/global_variables.tsv" \
      "SELECT VARIABLE_NAME,VARIABLE_VALUE FROM performance_schema.global_variables WHERE VARIABLE_NAME NOT LIKE '%rsa_public_key%' AND VARIABLE_NAME NOT IN ('wsrep_sst_auth') ORDER BY VARIABLE_NAME" \
      "SHOW GLOBAL VARIABLES" || true
```

主查询过滤了两个敏感键，**回退的 `SHOW GLOBAL VARIABLES` 一行过滤都没有**。在没有 `performance_schema.global_variables` 的 MariaDB 10.x / PXC 上：

- `wsrep_sst_auth` = SST 账号:密码明文 → 落盘

而 `security_scan`（L1167-1168）的规则是：

```
password[[:space:]]*=[[:space:]]*"?[^<[:space:]";]+
--password(=|[[:space:]])[^<[:space:]]+
BEGIN[[:space:]].*PRIVATE KEY
Authorization:[[:space:]]*(Basic|Bearer)[[:space:]]+[A-Za-z0-9._-]+
```

`user:pass` 形式**匹配不到** → 门禁放行。

**改法**：加一个统一的过滤函数，在 fetch 之后**无条件**调用（两条路径都覆盖）。

```bash
# 落到 global_variables.tsv 后统一过滤敏感变量，主查询与回退路径都覆盖
# 原则：删"真秘密"，不删"名字里带 password 的键"——后者大多是策略变量
filter_sensitive_variables() {
    local f="$1" tmp="$1.filtered"
    [ -s "$f" ] || return 0
    awk -F'\t' -v OFS='\t' '
      NR==1 {print; next}
      {
        k=tolower($1)

        # ① 策略类变量一律保留。名字里带 password，但值是策略不是秘密。
        #    default_password_lifetime=0 = 密码永不过期，正是巡检要找的结论；
        #    被当秘密删掉，等于把结论一起删了。（初版正则含裸 password，会误删这一批）
        if (k ~ /^(default_password_lifetime|password_history|password_reuse_interval|password_require_current|disconnect_on_expired_password|validate_password)/) { print; next }

        # ② 密钥 / 证书 / 公钥路径
        if (k ~ /(^|_)(rsa_public_key|private_key|public_key_path|server_public_key_path)(_|$)/) next
        if (k ~ /(^|_)ssl_(key|ca|capath|cert|crl|crlpath)(_|$)/) next

        # ③ 明文凭据。只认 passwd/pwd，不认裸 password ——
        #    MySQL 没有一个承载明文口令的变量名含裸 "password"，
        #    含它的全是策略变量，已在 ① 放行。
        if (k == "wsrep_sst_auth" || k == "wsrep_sst_receive_address") next
        if (k ~ /(^|_)(passwd|pwd|secret|token|api_key|auth_token|access_key|secret_key)(_|$)/) next

        print
      }' "$f" > "$tmp" && mv "$tmp" "$f"
}
```

调用点在 `collect_mysql_basic` 第一段之后：

```bash
    filter_sensitive_variables "$TABLES_DIR/global_variables.tsv"
```

同时**把 `security_scan` 的规则补上 `user:pass` 形态**（防的是别的文件里再出现）：

```
[[:alnum:]_.-]+:[[:alnum:]_.$@!%^&*()+-]{6,}
```
——这条误报率高（`host:port`、`key:value` 都会命中），**不建议直接加**。更稳的做法是：在 `filter_sensitive_variables` 里把 `wsrep_sst_auth` 的值单独记一条 `evidence/redacted_findings.txt`（写"变量名 + 值长度 + 是否含冒号"，不写值），让 Python 侧能验证门禁确实生效过。

**影响 / 风险**：`global_variables.tsv` 会少几个键。Python 侧如果按变量名硬取这些键，要改成读不到就跳过。

**验收**：在 MariaDB 实例上跑，`grep -i wsrep_sst_auth tables/global_variables.tsv` 无输出，且 `logs/security_scan_findings.txt` 不存在。

**实施记录（2026-09-20，与上面给的代码有 1 处偏差）**

- ✅ 已按"取回后统一过闸"落地：`filter_sensitive_variables "$TABLES_DIR/global_variables.tsv"` 放在 `collect_mysql_basic` 第一段之后，主查询与回退路径都覆盖。
- ⚠️ **偏差：`wsrep_sst_receive_address` 保留、没删。** 上面给的 awk 把它和 `wsrep_sst_auth` 一起 `next` 掉了，但它是 SST **监听地址**（`host:port`），不是凭证——Galera/PXC 巡检要看它。删它属于过度脱敏，与本条自己的原则（"删真秘密，不删名字像秘密的键"）矛盾。**实施时只删 `wsrep_sst_auth`。**
- ✅ 留痕落 `evidence/redacted_findings.txt`：`variable_name` / `value_length` / `contains_colon` 三列，**不写值**；只在真的删了键时才创建文件；幂等（重复调用不重复追加）。
- ✅ 未采纳"给 security_scan 加 `user:pass` 正则"的建议（误报率高：`host:port`、`key:value` 全命中），改用留痕让 Python 侧能验证门禁生效过。

---

### F-05 拦截后仍 `exit 0`【P1】

**现状（L1346-1373）**

```bash
if security_scan; then
    ...
    create_package || log_warn "回传包生成失败，可直接回传任务目录"
else
    ...
    log_error "敏感信息扫描未通过，已阻止打包；请查看 logs/security_scan_findings.txt"
fi
FINALIZED=1
printf '\n采集完成\n'
...
exit 0
```

两个分支都落到 `exit 0`。自动化无法区分"采集成功"和"被拦下没打包"。另外 `create_package` 失败只 `log_warn`，也报 0。

**改法**

```bash
if security_scan; then
    generate_collection_status_json
    generate_snapshot_json
    generate_summary
    generate_manifest
    rm -rf "$TMP_DIR" "$STATUS_PARTS_DIR"
    if ! create_package; then
        log_warn "回传包生成失败，可直接回传任务目录"
        FINALIZED=1
        exit 31
    fi
else
    generate_collection_status_json
    generate_snapshot_json
    generate_summary
    rm -rf "$TMP_DIR" "$STATUS_PARTS_DIR"
    log_error "敏感信息扫描未通过，已阻止打包；请查看 logs/security_scan_findings.txt"
    FINALIZED=1
    exit 30
fi

FINALIZED=1
printf '\n采集完成\n'
...
exit 0
```

注意 `FINALIZED=1` 必须在 `exit` 之前置位，否则 `trap` 的清理逻辑（如果它在 `FINALIZED=0` 时会重跑）会误动作。

> **注意：F-05 与 F-12 改的是同一段代码（L1346-1373），两段别都改。** F-12 是 F-05 的完整形态——它把 `security_scan` 提到 `generate_collection_status_json` / `generate_snapshot_json` / `generate_summary` 之前，顺带省掉重复的全量统计。**实施时按 F-12 的版本落。**

**退出码约定**（写进 `show_usage()` 和脚本头注释）

| 码 | 含义 |
|:---:|---|
| 0 | 全部成功，回传包已生成（或 `--no-package`） |
| 10 | 参数错误 / 认证材料准备失败 / 环境不满足（Bash 版本、缺少 mysql 客户端） |
| 20 | MySQL 连接失败 |
| 30 | 敏感信息扫描拦截，已阻止打包 |
| 31 | 打包失败（任务目录可用） |

**验收**：故意在任务目录放一个含 `password=xxx` 的文件，跑到结束，`echo $?` 应为 30 且无 `.tar.gz`。

---

### F-06 复制错误原文被白名单丢掉【P1】

**现状（L849）**

`sanitize_tsv_columns` 的白名单里只有：

```
... Last_Errno Last_IO_Errno Last_SQL_Errno ...
```

`SHOW REPLICA STATUS` 里**最有诊断价值的三列 `Last_Error` / `Last_IO_Error` / `Last_SQL_Error` 被丢掉**。客户那份报告里"主从中断，需重新搭建"的结论，唯一依据就是：

```
Last_IO_Errno: 13114
Last_IO_Error: Got fatal error 1236 from master when reading data from binary log:
               'A slave with the same server_uuid/server_id as this slave has connected to the master; ...'
```

只剩 `13114` 写不出任何结论——13114 本身不代表任何可行动信息，真正指向根因（两台从库 `server_id` 都是 34）的是那段 1236 原文。

同时 `replication_channels`（L872）默认把 `LAST_ERROR_MESSAGE` 换成 `SHA2(...,256)`。**哈希一个错误消息的诊断价值为零**（不可逆、无法比对），等于只有多源复制时连错误号之外什么都看不到。

**改法（共 4 处，初版漏了第 2、4 处）**

1. **L849 白名单补三列**（F-01 修完后这些值是单行转义，不会撑破结构）：

```
... Last_Errno Last_Error Last_IO_Errno Last_IO_Error Last_SQL_Errno Last_SQL_Error ...
```

2. **L218 占位表头同步补三列**（初版漏项）。`sanitize_tsv_columns` 保留的是**原始列名**（L752 回写 `$keep[x]`），所以占位表头必须与白名单一致，否则空结果时 schema 又对不上：

```bash
replica_status.tsv) printf 'Channel_Name\tSource_Host\tSource_Port\tSource_UUID\tReplica_IO_Running\tReplica_SQL_Running\tSeconds_Behind_Source\tLast_Errno\tLast_Error\tLast_IO_Errno\tLast_IO_Error\tLast_SQL_Errno\tLast_SQL_Error\tAuto_Position' ;;
```

3. **L872 / L882 去掉 SHA2，改截断明文**：

```bash
# 默认分支（原 SHA2 分支）改为：
"... CASE WHEN LAST_ERROR_MESSAGE='' THEN '' ELSE LEFT(LAST_ERROR_MESSAGE,500) END AS LAST_ERROR_MESSAGE, ..."
```

理由：`LAST_ERROR_MESSAGE` 是**服务器自己生成的诊断文本**，不含用户 SQL 字面量；而 `error_log_samples.message` 才是可能含 SQL 原文的那个（那个保留 `--include-log-text` 门禁是对的）。两者不该同一门槛。

改完后两个分支的别名统一为 `LAST_ERROR_MESSAGE`，**列名不再随 `--include-log-text` 变化**。

4. **同步改 `known_tsv_header` L228/L229**：`LAST_ERROR_MESSAGE_SHA256` → `LAST_ERROR_MESSAGE`。

> 第 4 步与 **F-18** 是同一件事的两面：只要第 3 步做了，列名就固定下来，F-18 随之消解。**先做 F-06，F-18 自动关掉**；若决定保留 SHA2 分支，则必须做 F-18。

**影响 / 风险**

- `known_tsv_header` 的 `replication_channels.tsv` / `replication_workers.tsv` 列名变更，Python 侧要同步
- 复制错误文本里会带对端主机 / IP / 端口 / 复制账号名。**已确认不做降级**（见 §0.6 决策 2）：白名单本来就保留 `Source_Host` / `Source_Port`，对端 IP 与端口本来就在包里，卡掉原文一个敏感信息都挡不住；而 `LEFT(...,200)` 恰好保留最敏感的前缀（`'repl@192.168.1.33:3306'` 在开头）、砍掉的却是真正的错误原因，是净损失。原文唯一新增的是账号名，账号名不是凭证，`password_included:false` 的声明仍然成立

**验收**：在客户那台 `server_id=34` 的从库上跑，`replica_status.tsv` 的 `Last_IO_Error` 列能看到完整 1236 文本。

---

### F-11 明文密码 cnf 落在任务目录内【P2】

**现状（L1307）**

```bash
MYSQL_CNF=$(mktemp "$TMP_DIR/.mysql_defaults.XXXXXX.cnf")
```

`$TMP_DIR` = `$TASK_DIR/.tmp`。正常退出（含 `INT/TERM/HUP`）会 `cleanup_auth`，但 **SIGKILL / OOM killer 不会**，而 L74-75 的说明写着"可直接回传任务目录"。

**改法**：放到任务目录之外，并加 `EXIT` trap 兜底。

```bash
AUTH_TMP_DIR=$(mktemp -d "${TMPDIR:-/tmp}/mysql_insp_auth.XXXXXX") || exit 10
chmod 700 "$AUTH_TMP_DIR"
MYSQL_CNF=$(mktemp "$AUTH_TMP_DIR/.mysql_defaults.XXXXXX.cnf") || exit 10
```
`cleanup_auth` 里加 `[ -n "${AUTH_TMP_DIR:-}" ] && rm -rf "$AUTH_TMP_DIR"`。
信号 trap 里加一次 `trap cleanup_auth EXIT`（若当前只 trap 了 `INT/TERM/HUP`）。

---

### F-19 错误日志未采集时要显式提示【P2】

**现状（L939-945）**

`error_log_summary`（按 `PRIO` + `ERROR_CODE` 聚合的计数、首次/末次时间）是**无条件采集**的，不受开关影响；但 `error_log_samples`（原文样本）在 `INCLUDE_LOG_TEXT=0` 时只落一行：

```bash
record_skipped "mysql.error_log_samples" "mysql.logs" "skipped" "log text disabled by default" "$TABLES_DIR/error_log_samples.tsv"
```

结果：默认跑一遍，包里只有聚合计数。你能看到"`ERROR_CODE=1045` 出现 37 次"，但不知道具体是什么错。更麻烦的是——**下游无从区分"本机真没有错误"和"压根没采原文"**：`collection_status.json` 里 `error_log_samples` 是 `skipped`，而 `skipped` 在其他二十多个采集项里也表示"按设计跳过"，语义被淹没。

**决策**（见 §0.6 决策 3）：**默认值不动，改提示。**

**改法**

1. `record_skipped` 的 reason 换成能自解释的句子（脚本内 reason 一律英文，与现有风格一致）：

```bash
record_skipped "mysql.error_log_samples" "mysql.logs" "skipped" \
    "log text not collected: pass --include-log-text (default off, keeps SQL literals inside the customer site)" \
    "$TABLES_DIR/error_log_samples.tsv"
```

2. `generate_summary` 里加一条显式提示，位置放在"状态统计"之前，比统计表更靠前：

```bash
if [ "$INCLUDE_LOG_TEXT" -ne 1 ]; then
    printf '\n注意：错误日志原文未采集（--include-log-text 未开启），本次仅有按 PRIO/ERROR_CODE 的聚合计数。\n'
fi
```

3. `snapshot.json` 的 `privacy.log_text_included` 已经是变量（L1102-1103），脚本侧无需改；但 **`INTERFACE.md` 要写明**：Python 读到 `log_text_included:false` 时，报告 4.6 节必须写"本次未采集原文"，**不能留空、也不能写"无异常"**。

**影响 / 风险**

- `summary.txt` 纯新增一行，不破坏格式
- `collection_status.json` 的 `reason` 文案变化——若 Python 侧有按 `reason` 字符串做精确匹配的逻辑需同步；按 `item_id` + `status` 判定则不受影响

**验收**：不带 `--include-log-text` 跑一次，`summary.txt` 出现该提示；`error_log_samples` 的 reason 换成了新句子。

---

### 批 2 实施记录（2026-09-20）

| 项 | 落地情况 |
|---|---|
| F-04 | 见上（含 1 处偏差说明） |
| F-05 | ✅ 已落。`security_scan` 移到 `generate_collection_status_json` **之前**（即 F-12 的形态），`SCAN_RC` 承接返回码；`create_package` 失败 `exit 31`、扫描拦截 `exit 30`；退出码表同时写进 `show_usage()` 与脚本头注释 |
| F-06 | ✅ 4 处全落：① 白名单补 `Last_Errno Last_Error Last_IO_Errno Last_IO_Error Last_SQL_Errno Last_SQL_Error`；② `replica_status.tsv` 占位表头 10 → 14 列；③ `replication_channels` / `replication_workers` 去掉 `SHA2` 与 `--include-log-text` 分叉，统一 `LEFT(LAST_ERROR_MESSAGE,500)`；④ `known_tsv_header` 的 `err_col` 变量删除，列名恒为 `LAST_ERROR_MESSAGE`。**F-18 随之自动关闭** |
| F-11 | ✅ 已落。`AUTH_TMP_DIR=$(mktemp -d "${TMPDIR:-/tmp}/mysql_insp_auth.XXXXXX")`（`chmod 700`），cnf 建在其内；`cleanup_auth` 一并 `rm -rf` 该目录；新增 `trap cleanup_auth EXIT` 兜底 |
| F-19 | ✅ 已落。`record_skipped` 的 reason 换成自解释英文句；`generate_summary` 在"状态统计"**之前**加一行中文提示。`INTERFACE.md` §5.1.1 已写明 Python 侧对 `log_text_included:false` 的处理要求 |
| F-12 | ✅ 随 F-05 落地。全量统计从 3 遍降到 1 遍（`generate_collection_status_json` / `generate_summary` 现各只出现 1 次调用） |

**一处需要你知道的取舍**：F-12 把 `security_scan` 提到了 `generate_*` 之前，因此
`snapshot.json` / `summary.txt` / `collection_status.json` **本身不再被 grep 扫描**（旧版会扫）。
判断为可接受，理由：这三个文件是**派生文件**，内容全部来自已被扫描的 TSV 与 `evidence/`，
含凭证的 `global_variables.tsv` 反而是在 F-04 之后先被脱敏、再被扫描。若你认为仍应把这几个
派生文件纳入扫描范围，代价是统计要跑两遍（放弃 F-12）。

---

## 批 3：补采集项与体验　⬜ 未开始

### F-13 备份产物扫描【缺·优先级最高】

**现状**：`collect_mysql_logs_backup` 只查 crontab（L955）、systemd timer（L960）、运行中备份进程（L964）。**完全不看备份目录。**

客户那份报告 4.7 是 `ls -l` 出来的列表，里面 `xtrfullbackup-*.tar.gz` 从 0129 到 0210 **全部是 4.0K**——"备份异常，需进一步处理"这条结论脚本抓不到。而这条恰恰是巡检报告里客户最认的一条。

**改法**：加 `--backup-dir DIR`（可重复）+ `--backup-min-bytes N`（默认 1048576）。

```bash
collect_backup_artifacts() {
    [ -n "${BACKUP_DIRS[*]-}" ] || { record_skipped "system.backup_files" "mysql.backup" "not_applicable" "no --backup-dir given" "$TABLES_DIR/backup_files.tsv"; return 0; }
    local d
    {
      printf 'backup_dir\tfile_name\tsize_bytes\tmodified_at\tage_hours\tsuspicious\n'
      for d in "${BACKUP_DIRS[@]}"; do
        [ -d "$d" ] || continue
        find "$d" -maxdepth 2 -type f \( -name '*.tar.gz' -o -name '*.xb' -o -name '*.xbstream' -o -name '*.sql' -o -name '*.gz' -o -name '*.dump' \) -printf '%p\t%s\t%T@\n' 2>/dev/null |
        while IFS=$'\t' read -r p sz ts; do
          [ -z "$p" ] && continue
          local age susp="no"
          age=$(awk -v t="$ts" 'BEGIN{printf "%.2f", (systime()-t)/3600}')
          [ "$sz" -lt "$BACKUP_MIN_BYTES" ] && susp="yes(size_below_min)"
          printf '%s\t%s\t%s\t%s\t%s\t%s\n' "$d" "$(basename "$p")" "$sz" \
            "$(date -d "@${ts%%.*}" --iso-8601=seconds 2>/dev/null || printf '%s' "$ts")" "$age" "$susp"
        done
      done
    } > "$TABLES_DIR/backup_files.tsv"
    local rows status
    rows=$(awk 'END{print (NR>0?NR-1:0)}' "$TABLES_DIR/backup_files.tsv")
    status="ok"; [ "$rows" -eq 0 ] && status="empty"
    record_status "system.backup_files" "mysql.backup" "$status" "$(iso_now)" "$(iso_now)" 0 "$rows" 0 "tables/backup_files.tsv" ""
}
```

**注意**：`awk 'systime()'` 是 gawk 扩展，Anolis/RHEL 的 `awk` 是 gawk，可用；但 `age_hours` 建议交给 Python 算，脚本只落 `modified_at` 与 `size_bytes`，减少对 `systime()` 的依赖。

**验收**：指向客户那台机的备份目录，`backup_files.tsv` 里 0210 那批应该全部 `suspicious=yes`。

---

### F-14 慢日志内容采集【缺】

**现状**：`log_files.tsv` 只有路径/大小/mtime（L924-936），`Slow_queries` 只有计数，PFS digest 没有慢日志上下文。客户报告 5.2 整节（`mysqldumpslow` 输出）无法生成。

**改法**：加 `--slow-log-tail N`（默认 0 = 不采）。脱敏后落盘：

```bash
    if [ "${SLOW_LOG_TAIL:-0}" -gt 0 ] && [ -n "$slow_log_path" ] && [ -r "$slow_log_path" ]; then
        tail -n "$SLOW_LOG_TAIL" "$slow_log_path" \
          | sed -E 's/(password|passwd|pwd|token|secret|identified by)([=: ]+)[[:graph:]]+/\1\2<REDACTED>/Ig' \
          > "$EVIDENCE_DIR/slow_log_tail.txt"
        record_status "mysql.slow_log_tail" "mysql.logs" "ok" "$(iso_now)" "$(iso_now)" 0 \
            "$(wc -l < "$EVIDENCE_DIR/slow_log_tail.txt")" 0 "evidence/slow_log_tail.txt" "tail ${SLOW_LOG_TAIL} lines"
    fi
```

**别做的事**：不要试图在脚本里复刻客户那份报告里的 `'S'` / `N` 字面量替换——那不是 `mysqldumpslow` 的行为，是 SQL 在到达服务器前已被中间件/审计设备处理过的痕迹。脚本原样落盘即可，替换留给 Python 侧做（要给 Python 加"是否已脱敏"的探测：如果 SQL 里 `'S'`/`N` 比例异常高，说明上游已脱敏，别重复处理）。

---

### F-09 关键参数补 CLI 开关【P2】

现状：`TOP_N`（L28）、`MIN_FREE_MB`（L27）、错误日志窗口（L941/L944 硬编码 `INTERVAL 24 HOUR`）都没有 CLI 入口，改要动脚本。

**改法**

```bash
  --top-n N               列表类采集的 LIMIT，默认 100
  --min-free-mb N         输出目录最小剩余空间，默认 200
  --error-log-hours N     error_log 聚合窗口小时数，默认 24
  --backup-dir DIR        备份产物目录，可重复
  --backup-min-bytes N    备份文件可疑大小下限，默认 1048576
  --slow-log-tail N       慢日志末尾行数，0 表示不采，默认 0
```

参数字符串拼接：

```bash
ERROR_LOG_INTERVAL="INTERVAL ${ERROR_LOG_HOURS} HOUR"
```

然后 L941 / L944 的 SQL 里用 `NOW() - ${ERROR_LOG_INTERVAL}` 替换字面量。

### F-12 去掉重复的全量统计【P2】

**现状（L1347-1363）**：先算一遍 `generate_collection_status_json` / `generate_snapshot_json` / `generate_summary`，随后 if/else 两个分支里**又各算一遍**——唯一的差别只是把 `package.security_scan` 这条状态纳入。等于每轮多跑一次全量 awk 统计。

**改法**：把 `security_scan` 移到这三个函数之前；`generate_collection_status_json` 内部本来就遍历 `$STATUS_PARTS_DIR/*.tsv`，只要 scan 先写状态、后生成，一遍就够。

```bash
cleanup_auth
security_scan; SCAN_RC=$?

generate_collection_status_json
generate_snapshot_json
generate_summary

if [ "$SCAN_RC" -eq 0 ]; then
    generate_manifest
    rm -rf "$TMP_DIR" "$STATUS_PARTS_DIR"
    create_package || { log_warn "回传包生成失败，可直接回传任务目录"; FINALIZED=1; exit 31; }
else
    rm -rf "$TMP_DIR" "$STATUS_PARTS_DIR"
    log_error "敏感信息扫描未通过，已阻止打包；请查看 logs/security_scan_findings.txt"
    FINALIZED=1
    exit 30
fi
```

注意 `security_scan` 的 `$findings` 写在 `$TMP_DIR` 里，而 `TMP_DIR` 会在最后被 `rm -rf`——L1171 已经 `cp` 到 `$LOG_DIR` 了，顺序保持不变即可。

---

### F-22 5.7 错误日志只走 pfs 表，无文件兜底【P1】

**实测输出**

```
mysql.error_log_summary: unsupported，performance_schema.error_log unavailable
```

**现状（L940-947）**：只走 `performance_schema.error_log`。该表是 **MySQL 8.0.22+** 才有，**5.7 及更早版本的错误日志是文件**，路径在 `@@GLOBAL.log_error` 里。

**这是结构性问题**——不是超时也不是权限：5.7 上该项**永远**记 `unsupported`，报告 4.6 节永久为空。而 `log_error` 变量脚本通过 `global_variables.tsv` **是能拿到的**：信息在包里，只是没拿它去读文件。

**改法**：加文件兜底，沿用 `--include-log-text` 同一门禁。

```bash
    if [ "$ERROR_LOG_TABLE_AVAILABLE" -eq 1 ]; then
        ... 现有 8.0.22+ 逻辑 ...
    else
        local log_error_file
        log_error_file=$(mysql_scalar "SELECT @@GLOBAL.log_error" 2>/dev/null)
        if [ -n "$log_error_file" ] && [ -r "$log_error_file" ]; then
            [ "$INCLUDE_LOG_TEXT" -eq 1 ] && \
              tail -n "$ERROR_LOG_TAIL_LINES" "$log_error_file" | redact_command_stream > "$EVIDENCE_DIR/error_log_tail.txt" 2>/dev/null || true
            awk '<按 5.7 日志格式解析>' "$log_error_file" > "$TABLES_DIR/error_log_summary.tsv" || true
        else
            record_skipped "mysql.error_log_summary" "mysql.logs" "unsupported" \
              "error log file not readable: ${log_error_file:-unknown}" "$TABLES_DIR/error_log_summary.tsv"
        fi
    fi
```

> **注意**：5.7 的错误日志是**文本行**格式，与 8.0 pfs 表的 `PRIO` / `ERROR_CODE` / `SUBSYSTEM` / `DATA` 列结构不同，**聚合规则必须另写**，不能套用现有 pfs 版本。此条属 §0 可靠性边界的 **C 档（需现场验证）**。
>
> **更省事的替代**：只采 `error_log_tail`（尾部 N 行 + 脱敏），不做聚合。回答"最近有没有报错"时，尾部原文的信息量高于聚合计数。

**验收**：5.7 上该项不再是 `unsupported`；带 `--include-log-text` 时 `evidence/error_log_tail.txt` 非空且密码已被替换。

---

### F-23 5.7 锁等待缺 `innodb_lock_waits` 兜底【P2】

**实测输出**：`mysql.data_lock_waits: unsupported，performance_schema.data_lock_waits unavailable`

**现状（L815-818）**：只走 `performance_schema.data_lock_waits`（8.0 的表）。该项的 `unsupported` **判定本身是正确的**（5.7 确实没有这张表），但 **5.7 的锁等待信息在 `information_schema.innodb_lock_waits`**，脚本没有用它。

**改法**：加一个兜底分支。

```bash
    if [ "$DATA_LOCK_WAITS_AVAILABLE" -eq 1 ]; then
        ... 现有 8.0 逻辑 ...
    elif mysql_table_exists information_schema innodb_lock_waits; then
        mysql_query_tsv "mysql.data_lock_waits" "mysql.performance" "$TABLES_DIR/data_lock_waits.tsv" \
          "SELECT requesting_trx_id,blocking_trx_id,requesting_thread_id,blocking_thread_id FROM information_schema.innodb_lock_waits LIMIT 500" || true
    else
        record_skipped "mysql.data_lock_waits" "mysql.performance" "unsupported" \
          "no lock-wait table available" "$TABLES_DIR/data_lock_waits.tsv"
    fi
```

> **列集合不同**：5.7 的 `innodb_lock_waits` 只有 4 个 ID 列，**没有 `ENGINE`**（8.0 的 `data_lock_waits` 有）。因此 `known_tsv_header`（L222）里 `data_lock_waits.tsv` 的占位表头**无法用一个静态值同时覆盖两种模式**——**这与 F-18 是同一类问题**。建议 F-23 与 F-18 合并实施：把 `known_tsv_header` 改成接受运行时参数的形式，而不是纯静态 `case`。

**验收**：5.7 上有锁等待时能采到行；无锁等待时为 `empty`，且表头与 8.0 分支可区分。

---

### F-25 sar 时间戳是 UTC，包内未声明时区【P2】

**实测证据**

```
历史 sar 初步范围: 2026-09-13 16:10:01 UTC ～ 2026-09-16 07:10:01 UTC
采集开始:         2026-09-16T15:12:29+08:00
```

**现状**：L663 `sadf -d "$f" -- "$@"` **未加 `-t` / `-T`**。`sadf -d` 的默认行为是**按 UTC 输出时间戳**（`-t` 输出本地时间，`-T` 输出本地时间并带时区偏移）。而 `snapshot.json` 的 `collection_started_at` 是本地时间（+08:00）——**同一包里两种时间基准**。

**影响分两层**

| 层 | 影响 |
|---|---|
| 覆盖率计算 | **不受影响。** `compute_sar_coverage`（L670-697）的 first/last 取自同一文件，**差值正确**，63.00 小时是准的。（L675 `date -d` 因系统 +08:00 会把 UTC 字符串当本地时间解析，epoch 整体偏移 8 小时，但两点同时偏移，差值不变） |
| Python 出图 | **会错位。** `history/sar_*.csv` 是 UTC，`timeseries/*.csv` 是脚本自己采的本地时间。两者画在同一张图上**相差 8 小时** |

**改法一（推荐，基准统一）**：给 `sadf` 加 `-t`。

```bash
        sadf -d -t "$f" -- "$@" >> "$outfile" 2>> "$MODULE_LOG_DIR/sar_history.log"; rc=$?
```

> **这是契约变更**：`-t` 输出格式与默认相同（`YYYY-MM-DD HH:MM:SS`，`compute_sar_coverage` 取 `$3` 的解析不受影响），但解析出的 epoch 不再偏移 8 小时 → `coverage.tsv` 的 `first_timestamp` / `last_timestamp` 语义从 UTC 变为**本地时间**。需同步 `INTERFACE.md`。

**改法二（保守）**：不动 `sadf`，只在 `snapshot.json` 显式声明时区：

```bash
"sampling": { ... "sar_history": {"timezone":"UTC", ...} }
```

改动更小，**但 Python 必须真的读这个字段并做换算**，否则问题依旧存在。

**验收**：`coverage.tsv` 的 `first_timestamp` 与 `timeseries` 首个时间戳换算到同一时区后，应落在同一时间窗口内。

---

### F-26 summary「请求 24 小时 / 覆盖 63 小时」文案误导【P2】

**实测输出**：`历史 sar 请求范围: 最近 24 小时；初步状态 ok，约覆盖 63.00 小时`

**这不是 bug，是设计使然**：L663 的 `sadf` 不加时间过滤，把 `find_sar_files` 找到的**全部** sar 文件导出，裁剪交给 Python——L723 的 reason 已写明 `raw sadf data exported; Python will perform final filtering and coverage calculation`。

**但文案会误导**："请求最近 24 小时"读起来像"只采了 24 小时"，与"覆盖 63 小时"并列自相矛盾。`coverage.tsv` 的 `requested_hours` 字段名同样会被误读为"实际采集范围"。

**改法**：只改文案，不动逻辑。

```bash
      printf '历史 sar 导出: 全部可读 sar 文件（参考上限 %s 小时，最终裁剪由 Python 执行）；初步状态 %s，实际跨度约 %s 小时\n' \
        "$SAR_HISTORY_HOURS" "$sar_coverage_status" "$sar_coverage_hours"
```

`coverage.tsv` 字段名 `requested_hours` → `reference_hours` 属契约变更；**若不想动契约**，至少在 `INTERFACE.md` 给该字段补一句语义说明即可。

**验收**：summary 不再出现"请求范围 / 覆盖"并列自相矛盾的表述。

---

## 批 4：外部独立复核回吐（2026-09-22，三套 MySQL 8.0.30 生产采集包）

来源：对三套 8.0.30 生产包（一主两从）绕过分析层、直读原始字节做独立复核，得到 3 条与采集层直接相关的结论。**其中两条的初版判断经核实后被推翻**，一并留档，避免以后重复讨论。

### F-28 `long_transactions.query_sample` 保留裸 CR，通用换行读取会拆行【P2】

**现象**：`tables/long_transactions.tsv` 用「通用换行」读取时会出现"270 行、其中 244 行是 0 个 TAB 的碎片"的假象（Python 文本模式、`csv` 模块、多数 ETL/BI 导入都会这样）。

**核实（二进制模式逐字节读）**：

| 包 | 文件大小 | LF 行数 | 数据行（7 个 TAB） | 裸 CR 字节数 |
|---|---:|---:|---:|---:|
| 主库 | 10,882 B | 28 | **26** | **244** |
| 从库 A | 119 B | 2 | 0 | 0 |
| 从库 B | 2,056 B | 6 | 4 | 0 |

**结论：F-01 的修复是生效的。** `\n` / `\t` 已被客户端转义成字面量，行结构完好，**不存在碎片行**。剩下的真缺陷是：源 SQL 文本里的 `\r\n`，只转义了 `\n`，**`\r`(0x0D) 原样落盘**。

**依据**：MySQL 客户端 batch 模式的转义集合是 `\n` / `\t` / `\0` / `\\`，**不含 `\r`**（`--raw` 才关闭全部转义）。所以这不是脚本参数问题，是客户端行为。

**影响**：任何做通用换行归一化的读取方都会把一行拆成多行 → 行数虚高、长事务统计失真。本次三包里只有主库命中（只有它的长事务 SQL 含 Windows 换行），**属于偶发但会静默发生的一类**。

**修法**：与 `processlist` 对齐即可 —— 两个采集项本来是同一类，只有一处做了清洗：

- `collect_mysql_performance` 里的 processlist 项（L1243）已经是
  `LEFT(REPLACE(REPLACE(IFNULL(INFO,''),'\n',' '),'\r',' '),500)`
- 同一函数里的 long_transactions 项（L1230）的 `query_sample` 只有 `LEFT(IFNULL(trx_query,''),500)`，**没有 REPLACE**

**改法（已落地，2026-09-22）** —— 与 `processlist` 对齐：

```sql
LEFT(REPLACE(REPLACE(REPLACE(IFNULL(trx_query,''),'\\r',' '),'\\n',' '),'\\t',' '),500) AS query_sample
```

> 顺带把 TAB 也换掉更稳妥：SQL 文本里的制表符会污染 TSV 的列语义。**这一点已一并落地**（三层 REPLACE）。

**验收**：`LC_ALL=C grep -c $'\r' tables/long_transactions.tsv` 为 0；`awk -F'\t' 'NF!=8' tables/long_transactions.tsv` 无输出。

---

### F-29 单点快照走 `performance_schema.global_status`，**该表按设计不含 `Com_*`**【P1】

**现象**：三包 `tables/global_status.tsv` 均为 **317 行**，`Com_` 开头的只有 `Com_stmt_reprepare` 一项。分析层实测 TPS 正常（51.87 / 17.66 / 5.07，走 `timeseries` 差分），所以风险不在"算不出"，而在**契约没写清**：任何下游都可能误拿快照去算，然后静默拿到空值。

**根因（已核到官方原文）**：主查询（L1189）是

```sql
SELECT VARIABLE_NAME,VARIABLE_VALUE FROM performance_schema.global_status ORDER BY VARIABLE_NAME
```

而 **`performance_schema.global_status` 按设计排除 `Com_xxx`**：

- MySQL 手册 *10.14 Performance Schema Status Variable Tables*：**"The Performance Schema does not collect statistics for Com_xxx status variables in the status variable tables."**
- Bug #87645（官方判定 **not a bug / by design**，引 WL#6629）：**"Existing status counters named 'COM_' are excluded from the performance schema status tables."**

**这不是白名单问题** —— 脚本对 `global_status` 没有任何列白名单，是源表里就没有这些行。`Innodb_*` 等其余变量都在，所以报告里 redo 容量一类的结论不受影响。

**影响**：凡依赖 `Com_commit` / `Com_rollback` / `Com_select` / `Com_insert` / `Com_update` / `Com_delete` 的派生指标（TPS、读写比、语句构成）在单点快照里**必然拿不到**。

**修法（二选一，已定 B，2026-09-22）**：

- **A（改采集，未采纳）**：主查询改走 `SHOW GLOBAL STATUS`（客户端语句），或在 P_S 查询之后**补一条** `SHOW GLOBAL STATUS LIKE 'Com\_%'` 追加/另存。`SHOW GLOBAL STATUS` 约 495 行，增量不大。
  - 若想保留 P_S 路径，官方给的替代是 `events_statements_summary_global_by_event_name` 里 `statement/sql/%` 的 `COUNT_STAR`，**但那是另一套口径，不要混进 `global_status.tsv`**。
- **B（不改采集）← 已采纳（2026-09-22）**：在 `docs/mysql-collector-interface.md` §8.4 写死口径 —— **"派生 TPS / 读写比一律用 `timeseries/mysql_status.csv` 首末行差分，不要用 `global_status.tsv`"**，并在 `inspection_core/models.py` 与 `plugins/mysql/package_adapter.py` 的字段旁加注释。**分析层无分支可删** —— `ctx.global_status` 本就是"只读入、无消费方"的字段（全仓只有赋值处、无读取处）。

> 原本的风险是"采集端不报错、下游静默拿到空值"，读报告的人看不出是缺数据还是真为 0。**选 B 后用契约 §8.4 + 分析层字段注释双重拦截**。

**联动**：选 A 时 `global_status.tsv` 行数由 317 → 约 500（**只增行、不增列**），`known_tsv_header` 不用改；若 `snapshot.json` 里有"status 变量总数"一类统计需同步。

---

### F-30 【非缺陷 · 契约澄清】`mysql.user.Host` 可以是空字符串，语义等价 `%`

**现象**：`tables/accounts.tsv` 的 `host` 列在 28 行中有 **10 行为空**（`CDRReplication`、`cpoe_to_cdss_hm`、`cpoe_to_cgzx`、`cpoe_to_hlyy_phhc`、`docaremssd`、`emr`、`emr_to_jc`、`nis_to_ydhl_lx`、`nis_to_zzsys_mdsd`、`ygxhRead`）。第一反应是"采集把 host 弄丢了"。

**核实：不是采集问题，源端即如此。** 旁证就在同一个采集包里 —— `tables/schema_privileges.tsv` 的 `GRANTEE` 列把这些账号渲染成 **`'cpoe_to_cgzx'@''`**。`information_schema.SCHEMA_PRIVILEGES` 的 GRANTEE 由 `mysql.user` 的 `User` / `Host` 拼出，host 为空才会得到 `@''`；且这些账号确实在 `processlist.tsv` 里有活动连接（否则会和"空 host 不可连"的直觉冲突）。

**官方语义**：MySQL 8.0 手册 8.2.6 *Access Control, Stage 1: Connection Verification* ——

> "The pattern `'%'` means 'any host' and is least specific. **The empty string `''` also means 'any host' but sorts after `'%'`.**"

**所以这 10 个账号是"任意主机可连"**，与 `root@%` 同级。比显式 `%` 更隐蔽的地方在于：排序在 `%` 之后，且**极易被读成"来源未知 / 待补齐"** —— 本次复核第一遍就这么误读了，把暴露面反向低估。

**要做的（都不改 SQL）**：

1. **契约文档（`INTERFACE.md`）加一句**：`accounts.tsv` 的 `host` 列**允许为空字符串**，语义等于 `%`（any host），不得当作"列缺失 / 错位 / 未采集"处理。
2. **`known_tsv_header` 不改**（列集没变）；**SQL 不改**（`SELECT user,host,...` 已把真值取回来了）。
3. **分析层加一条判据**（属分析层，登记在此仅作提醒）：来源限制统计必须把 `host IN ('%','')` 合并计入"任意主机可连"，并单独列出 `host=''` 的账号 —— 它们大概率是历史 `GRANT ... TO 'user'@''` 或迁移丢 host 留下的治理漏项。
4. **不要把空值落盘成 `<empty>` 之类占位符**：那会改掉真值，违反"采集器只采事实"。

**验收**：`awk -F'\t' 'NR>1 && $2==""' tables/accounts.tsv | wc -l` 与源端 `SELECT COUNT(*) FROM mysql.user WHERE host=''` 一致。

---

### 批 4 汇总

| 编号 | 是什么 | 要不要改采集 |
|---|---|---|
| F-28 | `query_sample` 裸 CR 未清洗（与 `processlist` 不一致） | ✅ **已改**（2026-09-22，三层 `REPLACE`，脚本已落盘 + `bash -n` 通过） |
| F-29 | 快照 `Com_*` 缺失，根因是 P_S 表按设计排除 | ❌ **不改采集**（已定 B：写死口径 —— 契约 §8.4 + 分析层注释） |
| F-30 | `host=''` 是源端事实、语义等于 `%` | ❌ **不改采集**（✅ 已补契约 §8.4 + 分析层判据） |

> **一条方法论**：本次三条里有两条第一版判断是错的（F-28 被当成"行结构被撑破"，F-30 被当成"采集丢字段"）。两处栽在同一个动作上 —— **没按字节/原值去读**，而是先用了会做换行归一化的读取方式，或"空即缺失"的直觉。复核采集包时先用 `open(p,'rb').read()` 看字节，再决定怎么解析。


### 批 4 实施记录（2026-09-22）

| 项 | 落地情况 |
|---|---|
| F-28 | ✅ **已改脚本**。`inspection/mysql_inspection_standard.sh` 的 `collect_mysql_performance` 内，`mysql.long_transactions` 的 `query_sample` 由 `LEFT(IFNULL(trx_query,''),500)` 改为**三层 REPLACE**（CR / LF / TAB 各清一次），与同函数的 processlist 项对齐。文件 129,663 → 129,720 字节（+57）；`bash -n` rc=0。列名未变 → `known_tsv_header` 不需要动 |
| F-29 | ❌ **不改采集（选 B）**。契约 `docs/mysql-collector-interface.md` 新增 **§8.4** 写死口径；`inspection_core/models.py` 的 `global_status` 字段与 `plugins/mysql/package_adapter.py` 的赋值处各加注释，指明“该字段不含 `Com_*`、勿用于 TPS”。**分析层无分支可删** —— `ctx.global_status` 全仓只有赋值、无读取 |
| F-30 | ❌ **不改采集**。契约 §8.4 写明 `accounts.tsv` 的 `host` 允许空串且语义等于 `%`（any host），禁止当“缺失 / 错位 / 未采集”处理 |

**一处事实更正**：F-29 初版把影响写成“分析层派生 TPS = `nan`（既有报告实测如此）”，复核 `report_model.json` / `analysis.json` 后**确认是误判** —— 分析层本就取 `timeseries` 的 `Com_commit` / `Com_rollback` 逐点差分（`plugins/mysql/metrics.py` 的 `MYSQL_COUNTERS`），实测 `mysql_realtime.tps.average` 为 **51.87 / 17.66 / 5.07**（`.34` / `.125` / `.33`），完全正常。快照缺 `Com_*` 的后果因此从“下游算不出”降级为“**契约没写清、下游可能误用**”，处置随之从“改采集”改判为“写死口径”。

### 批 5 回吐：F-31（2026-09-22，清单 C4 落地）

| 项 | 落地情况 |
|---|---|
| F-31 | ✅ **已改脚本**。`generate_snapshot_json` L1531 的 `ntp_synchronized` 由 `awk '/System clock synchronized/'` 改为 `awk '/^(System clock\|NTP) synchronized:/'`，同时兼容 systemd 新旧两种字段名。实测三节点 `timedatectl.txt` 输出均为新版 `NTP synchronized:`，旧正则匹配不到 → `ntp_synchronized` 全为空；修后 `.33`=`yes`、`.34`=`yes`、`.125`=`no`。**字段结构与 status 取值未变**（只是值从空串变回真实 yes/no），故**不 bump `SNAPSHOT_SCHEMA_VERSION`、不改 `known_tsv_header`** |

**配套说明**：分析层早已不依赖 `snapshot.json#ntp_synchronized` —— 批 C 的 `parse_timedatectl` / `ntp_verdict`
直接从 `evidence/timedatectl.txt` 原文解析，所以此 bug 不影响分析层的三档时间判定（`.125` 偏移 131s 仍正确判 `risk`）。
修它只为让 `snapshot.json` 与 `llm_input.json` 的该字段不再误导性为空，属“契约字段回填正确性”而非“功能缺失”。

## 改完之后仍然"缺"的（有意为之，别改）

这四条是设计决策，不是遗漏：

- 不做风险评级与建议（交 Python）
- my.cnf 只取白名单（F-02 修完才是"真的白名单"）
- 不合并主从拓扑（一实例一包）
- 不采 SQL 结果集内容

## 契约同步规则（配套文档 `INTERFACE.md`）

结构说明与 Python 对接契约见同目录 `D:\AI\MYSQL\GPT\1.0\INTERFACE.md`。**改本清单里任何一项之前，先读那份文档的第 7 节。**

三条硬规则：

1. **动输出就 bump 版本号**：目录结构、文件增删改名、列增删、字段语义、status 取值、退出码任一变化 → 必须 bump `SNAPSHOT_SCHEMA_VERSION`。只新增独立文件=次版本（`1.0`→`1.1`）；删列/改名/改语义=主版本（`1.x`→`2.0`，Python 必须显式适配）。
2. **改 SQL 的 SELECT 列表 → 同步改 `known_tsv_header`**。这是已踩过的坑（F-03 三处漂移）：正常采集时表头来自 mysql 输出，看不出问题；**只有空结果时占位表头才暴露**，所以极易漏。
3. **新增采集项四件事一起做**：写代码 → 登记 `known_tsv_header` → 更新 `INTERFACE.md` §3 清单 → 更新 §4 目录树。
4. **注释与代码同改**：脚本只有 18 行注释，正因为少，那 18 行会被当成"已经过核实的记录"来读，**注释撒谎比没注释更坏**。`INTERFACE.md` §7 规则 5 列了本轮会被打假的 4 处注释（GTID 续行补丁、写 `ok` 的口径、逐行过滤说明、`sql_text_included` 恒开），改对应条目时必须一起处理。

本清单里 **F-01 / F-03 / F-06 / F-11 / F-16 / F-19 / F-20 / F-21 / F-22 / F-23 / F-25 会影响 Python**，逐条见下面联动表；批 4 的 F-28 / F-29 / F-30 见该表末尾三行。

## 与 Python 侧的联动清单

改完后需要同步的地方，别漏：

| 文件 | 变更 |
|---|---|
| `tables/mycnf_allowlist.tsv` | 2 列 → 4 列（`source_file` `section` `parameter` `configured_value`） |
| `evidence/innodb_status.tsv` | 改名 `innodb_status.txt`，内容为原始多行文本 |
| `tables/replication_channels.tsv` | 末列 `LAST_ERROR_MESSAGE_SHA256` → `LAST_ERROR_MESSAGE`（**明文 500 字符，不再哈希**；列名两个分支统一，F-18 随之消解） |
| `tables/replication_workers.tsv` | 同上 |
| `tables/replica_status.tsv` | 新增 `Last_Error` / `Last_IO_Error` / `Last_SQL_Error`（明文全文，不截断） |
| `tables/global_variables.tsv` | 少若干 `*_key` / `*_secret` / `wsrep_sst_*` 键；**`default_password_lifetime`、`password_history`、`password_reuse_interval`、`validate_password*` 等策略变量必须保留**（F-04 修正后） |
| 所有 TSV | 自由文本值内的换行/制表符现在是 `\n` / `\t` 字面量，**读取时需 unescape** |
| `tables/long_transactions.tsv` | `query_sample` 内的 CR 已被 REPLACE 清成空格（F-28，2026-09-22 落地）；**读取方仍不要启用通用换行归一化**，否则历史上产生的旧包会行数虚高 |
| `tables/global_status.tsv` | **F-29 已定不改采集**：行数保持 317，`Com_*` 依然缺席。**派生 TPS / 读写比一律用 `timeseries/mysql_status.csv` 差分**（契约 §8.4）；分析层不要再用它算 TPS（`ctx.global_status` 无消费方） |
| `tables/accounts.tsv` | `host` 列**允许空字符串**，语义等于 `%`（any host）；下游不得当"缺失 / 错位"处理（F-30） |
| `evidence/mycnf_includes.txt`（新） | 实际展开的配置文件清单 |
| `evidence/slow_log_tail.txt`（新，可选） | 慢日志末尾 |
| `tables/backup_files.tsv`（新，可选） | 备份产物清单 |
| 退出码 | 0 / 10 / 20 / 30 / 31 |
| `summary.txt` | 未开 `--include-log-text` 时**新增一行**"错误日志原文未采集"提示（F-19） |
| `collection_status.json` | `mysql.error_log_samples` 的 `reason` 由 `log text disabled by default` 改为自解释英文句（F-19）。按 `reason` 精确匹配的解析要同步；按 `item_id` + `status` 判定则不受影响 |
| `snapshot.json` | `privacy.sql_text_included` **恒为 `true`、不会变化**（§0.6 决策 1）——Python 侧不要为它写分支适配 |
| `tables/replication_workers.tsv` | **5.7 上少 2 列**（`LAST_APPLIED_TRANSACTION` / `APPLYING_TRANSACTION`）。**必须按列名取值，不能按下标**（F-20） |
| `tables/group_replication_members.tsv` | **5.7 上少 2 列**（`MEMBER_ROLE` / `MEMBER_VERSION`）。同上（F-20） |
| `tables/data_lock_waits.tsv` | **5.7 走 `innodb_lock_waits` 兜底，列数与 8.0 不同**（无 `ENGINE`，只有 4 个 ID）。按列名取值（F-23） |
| `collection_status.json` / `summary.txt` | **新增状态取值 `schema_mismatch`**：`summary` 对象与状态计数各加一项；`error_count` 口径是否纳入该状态需一并定（F-21） |
| `history/sar_*.csv` | 时间戳语义**由 UTC 改为本地时间**（采纳 F-25 改法一时）；若采改法二，则 `snapshot.json` 新增 `sampling.sar_history.timezone`，Python 必须真的做换算 |
| `evidence/error_log_tail.txt`（新） | 5.7 上 pfs 表不可用时的文件兜底产物（F-22） |

## 回归验证

改完按这个顺序验：

1. **结构**：`awk -F'\t' 'NF!=<期望列数>{print FILENAME": "NR}'` 遍历 `tables/*.tsv`，应无输出
2. **转义**：`grep -c '\\n' tables/innodb_status.*` 应为 0（已挪到 `.txt`）；`replica_status.tsv` 行数应等于实例数 × 通道数
   - **注意 CR 例外**：`long_transactions.tsv` 在 F-28 修掉之前，用通用换行读取会虚增行数。校验行数请用二进制模式或 `LC_ALL=C awk`（awk 默认只按 `\n` 分行，不受 `\r` 影响）。
3. **my.cnf**：`awk -F'\t' 'NR>1{print $1}' tables/mycnf_allowlist.tsv | sort -u`，应包含 `/etc/my.cnf.d/` 下的文件
4. **门禁**：故意落一份含 `password=abc123` 的文件，验证 `$?` = 30 且无 `.tar.gz`
5. **退出码**：断网跑一次，验证 `$?` = 20
6. **幂等**：连跑两次，`manifest.json` 的 `files[].path` 集合应一致（哈希当然不同）
7. **脱敏决策落地**（§0.6）：
   - 不带任何开关跑一次：`summary.txt` 顶部应出现"错误日志原文未采集"提示（F-19）
   - 同一实例带 `--include-log-text` 再跑一次：`replication_channels.tsv` 第一行列名与上一次**完全一致**（F-18 应已消解）
   - `replica_status.tsv` 的 `Last_IO_Error` 列在客户那台 `server_id=34` 的从库上能看到完整 1236 文本（F-06，不截断）
   - `grep -c 'sql_text_included":true' snapshot.json` = 1（决策 1：保持恒开，未被改成变量）
8. **5.7 兼容**（复用本轮实测的同一环境，MySQL 5.7.44）：
   - `mysql.replication_workers` / `mysql.group_replication_members` 不再是 `unsupported`，`.tsv` 有数据行（F-20）
   - `mysql.table_io_top` / `mysql.unused_indexes` 不再是 `timeout`；**总耗时应从 94.9 秒降到 40 秒以内**（F-24）
   - `mysql.error_log_summary` 不再因 pfs 表缺失而 `unsupported`（F-22）
9. **状态语义**（F-21）：确认 `unsupported` 只剩"表/功能真不存在"的场景；故意写错列名的项必须记为 `schema_mismatch`
10. **时间基准**（F-25）：`history/sar_cpu.csv` 的首个时间戳与 `timeseries` 首个时间戳换算到同一时区后，落在同一时间窗口内
11. **注释**（F-27，批 0）：
   - `strip()` 指纹比对 **diff 为空**（证明批 0 是纯注释、零逻辑改动）
   - `bash -n inspection/mysql_inspection_standard.sh` 通过；注释密度 `grep -c '^[[:space:]]*#' / 总行数` **≥ 0.20**
   - 抽一个模块入口（如 `collect_mysql_replication`）确认其上方 3 行内有 L3 注释块
   - 确认 L856-859 的 GTID 续行 sed 补丁**已随 F-01 一起删除**；L1102 旁已注明"有意恒开、勿改成变量"
   - 确认"有意为之"的 6 处（不做评级 / my.cnf 白名单 / 不合并拓扑 / 不采结果集 / `sql_text_included` 恒开 / `--include-log-text` 默认关）在代码里都有注明
