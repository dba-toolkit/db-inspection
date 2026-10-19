# OS 层跨库对齐清单（MySQL / PostgreSQL / Oracle）

> 目标：**同一台服务器**上的三个实例（db01，MySQL 8.0 + PostgreSQL 17 + Oracle 19c），
> 三份采集包对 OS 的观测口径必须一致 —— 否则同一台机器会在三份报告里得到不同结论
> （设备表多出/少掉行、同一份 sar 历史一份说"可用"一份说"历史不足"）。
> 入口：`inspection/{mysql_inspection_standard.sh, pg_inspection_standard.sh, oracle_inspection.sh}`
> 判据来源：`inspection_core/system_checks.py`（阈值/rule_id 单一来源，本文件不改判据，只对齐采集面）

## 0. 判据尺度

| 档 | 含义 |
|---|---|
| **及格线** | 采集面事实错误：同一台机器两个脚本给出不同事实、或把"没采到"写成"正常" |
| **加分题** | 采集项多少、字段冗余度这类可优化点 |
| **取向差异** | 两个口径都自洽，但**必须选一个**并在三库统一；单选本身不构成缺陷 |

---

## 1. 本轮落地（批 1）

| # | 项 | 现状（改前） | 判定 | 依据 | 影响面 |
|---|---|---|---|---|---|
| A1 | PG `ntp_synchronized` 匹配 | 只匹配 `System clock synchronized`，systemd ≥ 239 输出的是 `NTP synchronized` ⇒ 新系统上 `snapshot.json#time_evidence.ntp_synchronized` **恒为空且不报错** | **及格线** | MySQL 已修（F-31） | PG 侧 `time_evidence`；报告"时间同步"一行 |
| A2 | PG `generate_collection_status_json` | `find \| tr '\n' ' '` 后裸展开 `$files`，路径含空格即 JSON 损坏、items 静默变少 | **及格线** | MySQL 已修（F-10） | PG 包完整度分母 |
| A3 | PG `classify_failure` | 无 `schema_mismatch` 档：psql 报 `column "x" does not exist` 落 `error`，与"环境不支持"混在一起 | **及格线** | MySQL 已修（F-21） | PG 采集台账 + `summary.txt` |
| A3b | PG chrony 失败归类 | 缺 `chronyd/chrony ... not running`、`daemon is not running` 模式 ⇒ 同一台机器 MySQL 报 `not_enabled`、PG 报 `error` | **及格线** | 对齐 MySQL 正则集 | 三库同机结论一致性 |
| A3c | PG `summary.txt` / `collection_status.summary` | 无 `schema_mismatch` 计数 ⇒ 该状态在采集摘要里看不见 | 及格线（A3 的必要配套） | 同 A3 | 现场排障 |
| A4 | `capture_command` 失败落盘 | MySQL/Oracle **无条件保留**半截输出（表格也留）、PG **无条件清空**（文本证据也丢）：三库三种取向 | **取向差异 → 统一** | 见 §2.1 | 三库 evidence/ 与 tables/ |
| A5 | 磁盘设备过滤 | PG 排 `dm-*`，MySQL/Oracle 不排 ⇒ `/sys/block` 里 LVM/多路径逻辑设备与底层物理盘**同一份 IO 被列两次**，报告的"磁盘设备"表三库行数不同 | **及格线**（同机结论不一致） | 见 §2.2 | 报告 OS 章磁盘表 + `disk_util` 取 max 的设备集合 |
| A6 | `system.hugepages` 状态 | 三库**无条件写 `ok`**（`awk 'END{print NR-1}'` 取行数但不用）⇒ 一行都没采到也显示"正常" | **及格线**（三库同形） | PG fix-plan F-27 | 三库大页检查项 |
| A7 | `compute_sar_coverage` | ① PG/MySQL/Oracle 三份实现口径不同（PG 有窗口裁剪与缺口字段，另两库是旧版：全量首尾相减）② **三库都不看连续性**，中间停机 18h 仍可能判 `ok` | **及格线** | PG fix-plan F-04；对齐三库 | `history/coverage.*` → 报告"历史数据可用性" |
| A8 | `STATUS_DISPLAY` 词表 | 缺 `schema_mismatch` 键 ⇒ 该状态会**原样印英文**（漏键不抛异常、测试不红） | 及格线（A3 的必要配套） | `inspection_core/word_engine.py` 单一来源约定 | 附录「数据缺口」表状态列 |
| A9 | PG `collection_quality` 状态集合 | `error_states` 是**判否式**集合（不在集合里就算 ok）⇒ 新状态 `schema_mismatch` 不登记就会**静默把完整度分数抬高** | 及格线（A3 的必要配套） | `plugins/postgresql/package_adapter.py` | PG 采集完整度分 |

### A7 的一个判据修正（对现有 fix-plan 的纠正）

PG fix-plan F-04 给的 after 写法是「`max_gap_seconds > SAR_HISTORY_HOURS*3600/10` → 不许判 ok」。
**这条阈值是死的**：24h 窗口的 10% = 2.4h，而 `covered` 只累加 ≤1h 的间隔 ⇒
只要存在 >2.4h 的空洞，累计覆盖必然 <21.6h（90%），**覆盖率判据已经先判掉它了**，
连续性那条永远不会单独触发 —— 等于立了一条没人验证得了的规则。

本轮的取值：**`max_gap_seconds > 3600`（1 小时）→ 降为 `partial`**，与"covered 只累加 ≤1h 间隔"
同一口径。这条**真的会触发**：夹具 `gap100`（30 分钟采样、挖掉 2 个点 = 90 分钟空洞）
累计覆盖 79200s ≥ 77760s（旧口径判 `ok`），新口径判 `partial`。

---

## 1A. 批 2 第一项：B1 命令缺失登记（2026-10-16 落地）

**缺陷**：`analyzer.collection_quality` 的分母 `total` 是**遍历 `ctx.status["items"]` 累加**的
⇒ 采集脚本里 `has_cmd X && capture … || true`（命令缺失时不写 `record_status`）的项
**连 0.5 的 optional 权重都不占**，直接从分母消失 ⇒ 完整度只升不降 ⇒ **机器越精简分数越高**。
这是「没采到 ≠ 没问题」的**第五种形态**（前四种在规则层/展示层）。

**范围（三库同形）**：PG **21** 处 / MySQL **17** 处 / Oracle **12** 处。

| 库 | 补的 item |
|---|---|
| 三库共有 | `timedatectl` `chronyc_tracking` `chronyc_sources` `ntpq_peers` `free` `filesystems` `inodes` `block_devices` `mounts` `ip_address` `ip_route` `ifconfig` `socket_summary` `numa` `sysctl_selected` `dmesg_errors` `backup_timers` |
| PG 追加 | `iostat`、`postgres_processes`、`ha.patroni` / `ha.repmgr`（原 `ha.manager` **废弃**） |
| MySQL 追加 | `mysql_processes` |
| Oracle 已修/本轮补 | N-14/N-20 已修 `ntpq_peers` / `sysctl_selected` / `ifconfig`(ip 分支)；本轮补 `chronyc_tracking` / `chronyc_sources` / `ip_route` / `ifconfig`(末段) |

**口径**：命令缺失 ⇒ `unsupported`；互斥备选（chrony↔ntpq、ip↔ifconfig、patroni↔repmgr）在**未采用**的那条记 `skipped`，
使两个 item 在**所有**路径都出现在台账里 —— 下游据此区分「环境不适用」与「该采却没采到」（对齐 Oracle N-14）。

**为什么不能用 `has_cmd X && capture … || record_skipped …`**：`capture_command` 返回**命令自身的 rc**
⇒ 命令**存在但执行失败**时 `||` 也生效，会用 `unsupported` **覆盖真实的 `error` / `empty`**（把"跑失败了"说成"没这个命令"）。必须 `if/else`。

**尺子判据 =「分段覆盖」**（`_work/b1_scan.py`）：守卫块按 depth==1 的 `else`/`elif` 切成互斥段，
段里每个 item 必须 ① 出现在 **≥2 段**、② 出现在**末段**。第 ② 条是关键 —— 命令全不存在时执行**必然走末段**，
于是把"精简安装"场景降维成**静态可判**，不必真造 PATH 夹具。

**尺子补出原清单没有的 5 项**（fix-plan F-08 那 16 项是人工逐字读出来的）：
`system.ip_route`、`system.ifconfig`、`ha.patroni`、`ha.repmgr`、`ha.manager`；判据 ② 又在实施中抓到 Oracle 一处。

**验证**：三库 **0 违规**；尺子自证 8/8 违规 item 抓到、0 误伤；负控制（变异真文件 → 真实变红 → sha256 逐字节复原 → 回到 0）；`bash -n` 三库通过。

**★ 落地后自查抓到的自身缺陷（同一批，2026-10-16）**：给 Oracle 补 `else` 时，`system.timedatectl` 那行
写成了 `record_skipped …` —— 而 `record_skipped` 只在 **MySQL / PG** 里定义，Oracle 侧是 **10 参数**的 `record_status`。
运行级 A/B（从真文件抽出那个 `if` 块，stub 掉 `has_cmd` / `capture_command` / `record_status`，跑「命令缺失/存在」两条路径）实测：

| 版本 | `timedatectl` 缺失时 | 台账结果 |
|---|---|---|
| HEAD（B1 之前） | 块里没有 `else` | **空** |
| B1 初版（写错函数） | `record_skipped: command not found`（rc=127） | **仍是空** |
| 修好后（当前） | — | `record_status … unsupported` ✓ |

⇒ 文本上「每个 item 都在 `else` 段里出现过」，运行时却**照旧不进台账** —— 正是 B1 要修的病，
却因为形态正确而看起来「已经修好了」。这就是 §5.1 那条同类的坏味道：
**没有能力说出自己没生效，看起来就跟生效了一样**。

**原尺子为什么兜不住**：`_work/b1_scan.py` 只判「item 在 `else` 段里出现过」，
**不判调用的函数存不存在**。判据只覆盖了「有没有写」，没覆盖「写的东西能不能跑」。

**⇒ 新增守门 `tests/test_collector_helpers_defined.py`**：凡 `record_*` / `capture_*` / `mark_*` 形态的调用，
名字必须在**同一文件**里有 `name() {` 定义；规则来源是**命名约定**（不从被测文件自身反推），全行注释除外。
校准方式是**先写守门、再修**——守门在未修的 Oracle 上真实报出 `oracle_inspection.sh:1453 record_skipped`，
修完回绿；负控制把坏行塞回去 ⇒ `_work/b1b_runtime.py` 变红且 stderr 出现 `command not found` ⇒ 逐字节复原后回绿。
⚠ **未做**：真机 PATH 夹具。本机 Windows + MSYS **无法**做 PATH 隔离（exe 依赖 `msys-2.0.dll`，PATH 单目录 ⇒ exec 127；
不可执行同名文件不能遮蔽 PATH 查找；`.exe` 透明映射使 `mkdir` 与 `mkdir.exe` 同一文件）。夹具代码留 `_work/b1_harness/verify.py`，建议在目标机（RHEL 8）跑一次。

## 1B. 批 2 第二项：B5 chronyc/ntpq 互斥判据（2026-10-16 落地）

**问题**（PG 1016 真包实测）：三库的互斥链判据是裸 `has_cmd chronyc`。本机
`chronyc tracking` 实际返回 rc=1 `506 Cannot talk to daemon`（chronyd 没跑），
却把 `system.ntpq_peers` 记 `skipped`、reason 写 `chronyc available` —— **理由与事实矛盾**；
且本机若真正用的是 ntpd，ntpd 的对端列表会**整块丢失**（本包靠 `system.timedatectl` 兜住）。

**修法**：判据从「命令存在」改为「**守护进程在应答**」。`capture_command` 返回的就是
**命令自身的 rc**，直接拿它当判据：

```sh
    if has_cmd chronyc; then
        if capture_command "system.chronyc_tracking" … chronyc tracking; then
            record_skipped "system.ntpq_peers" … "skipped" "chronyd is answering; …"
        elif has_cmd ntpq; then
            capture_command "system.ntpq_peers" … ntpq -pn
        else
            record_skipped "system.ntpq_peers" … "unsupported" "chronyd is not answering and ntpq is not installed"
        fi
        capture_command "system.chronyc_sources" … chronyc sources -v
    elif has_cmd ntpq; then
        …
```

★ 条件里**不能**带 `|| true`（会让 `if` 恒真）—— Oracle 原先那行有，一并去掉。

| 环境 | `system.ntpq_peers` |
|---|---|
| chronyc 在应答 + 有 ntpq | `skipped`（互斥未采用） |
| chronyc 装着但不应答 + 有 ntpq | **真采一次**（修前的病：这里记 skipped ⇒ 证据丢失） |
| chronyc 不应答 + 无 ntpq | `unsupported` |
| 无 chronyc + 有 ntpq | 真采（原路径） |

`system.chronyc_sources` 仍**无条件采一次**（失败由 `classify_failure` 落 `not_enabled`），
与修前一致 ⇒ 台账项数与完整度分母都不变。

**验证**（`_work/b5_runtime.py`）：从三库真脚本抽出 `if has_cmd chronyc` 块，配 stub 的
`has_cmd` / `capture_command`（按预设 rc 返回）跑上表 4 种组合 ⇒ **3 库 × 4 组全绿**；
负控制把 B5 之前的片段重生成 harness ⇒ B 组给出 `skipped`（旧行为），断言确实敏感 ✓。

## 1C. 批 2 第三项：采集状态贯通（F-12 / F-14 / F-15 / F-22，2026-10-16 落地）

**问题**（PG 1016 真包实测）：「这项到底采到没有」在报告里**没有一层说得出**。三条路都让
「没采到」长得像「没问题」：

1. 展示层读 `ctx.snapshot["collection_status"]` —— **该键从不曾存在**（真台账是包顶层的
   `collection_status.json`，由 `package_adapter` 读进 `ctx.status`）⇒ 查找永不命中，
   退化成 `ok if rendered_rows else empty`，而多数渲染行是**代码合成**的（恒 ≥3）⇒ 逐条印
   「采集状态：正常」。
2. 规则层 `evaluate(rule_id, applicable, available=True, …)` —— 44 处调用点第 3 个参数
   **全写死 True** ⇒ 空表算出 0 就判 `passed`。
3. 展示 item_id（18 个）与采集 item_id（~75 个）**是两套命名空间**，其中 **5 个还与采集层的
   `category` 同名**（`pg.performance` 既是章节名、又是 category）⇒ 一旦有人「顺手用名字查」
   会静默取到另一批项的状态。

**修法（三件同批，缺一即更糟）**

| 层 | 文件 | 内容 |
|---|---|---|
| 判据（**新**）| `inspection_core/collection_state.py` | `state_is_collected` / `is_no_data` / `worst_state` / `merge_section_state` / `expand_source_names`；词表 `DATA_STATES` / `NO_DATA_STATES` / `NOT_APPLICABLE_STATES` **只此一份** |
| 指标 | `plugins/postgresql/metrics.py` | `METRIC_SOURCES`（34 指标 → 采集表 stem）、`collector_table_states()`、`source_status_map()`；`derive_metrics` 多回一个 `source_status` |
| 规则 | `plugins/postgresql/rules.py` | `RULE_METRIC_KEY`（29 条）、`evaluate_metric()`、`RuleEvaluation.source_status`；**漏登记/写错键 ⇒ `KeyError`**（响亮，不许静默假通过） |
| 展示 | `plugins/postgresql/presentation.py` | `PG_SECTION_SOURCES`（18 章节 → 采集 item_id，支持 `db.*.X` 通配）、`collector_status_index()`、`section_collection()`；删掉「按渲染行数猜」 |
| 附录 | `word_engine.STATUS_DISPLAY` | 补 3 个**缺键** `unmapped`/`unavailable`/`missing` → 「未采集」（不在此文件加 PG 专用逻辑） |

**枢纽：`output_file` 就是「指标/章节 ↔ 采集项」的桥。** 不走 Oracle 的 `table_fetch`
（它目前只在 Oracle 落地），改用**采集器自己声明**的 `collection_status.items[].output_file`
basename → `.tsv` stem（真机 128 项全有值，无需猜表名）。

★ **这是契约变更**：`record_skipped` 必须传 outfile，否则该项对报告层不可见（见
`postgresql-collector-interface.md` §6.2 / §9.2）。

**报告侧 `status` 与台账归并原值刻意分开**：`empty`（查了确实没有）与 `partial`（部分有证据）
都归成 `ok` —— 引擎的语义是「非 ok 就压过分析结论」，原样透出 `partial` 会把该节 `risk`
盖成「部分可用」，等于**用「证据不全」抹掉「发现问题」**。缺口明细走 `reason` ＋ 附录缺口表。

**对照实验（同包 + 同核心 + 只换一处）**

| 场景 | `stale_tables` 的 `source_status` | `PG.VACUUM.STALE` |
|---|---|---|
| 真包（未注入） | `partial`（顶层按 B6 设计 skipped） | `passed` |
| 注入（台账改 `skipped` ＋ 表清空） | `skipped` | **`not_evaluated` ＋「证据不足」** |

注入前同样的包判 `passed` —— 即本批要消掉的**假通过**。

**守门（`tests/test_collection_state.py` ＋ `tests/test_postgresql_plugin.py::CollectionStateContractTests`）**

- **值域钉在独立来源**：映射表的值用 `inspection/pg_inspection_standard.sh` 抽（字面 item_id +
  `.tsv` stem），**不从被测对象自己取样本**（自引用会让漏登记静默通过）。
- **尺子自证**：断言抽取器抽到了 `pg.instance` / `table_health` —— 抽取器失效必须红。
  真实踩过：`findall` 返回的捕获组**不含** `.tsv`，再 `[:-3-4]` 截一刀 ⇒ `table_health` 变
  `table_he`，13 条断言全红而名字「看着差不多」。
- **配对反误伤**：`empty` 必须照常评价（不是 `not_evaluated`）；`partial` 章节必须留 `ok`。
- **负控制 8 组**（跑在 `_work/negctl_arm_*` **工作树副本**，真文件自始不被写）：
  混合归并返 `ok` / 空输入返 `ok` / `skipped` 也算采到 / 章节退回按行数猜 / 规则 `available`
  恒 True / 映射表漏一项 / 映射值不存在 / 规则漏登记 —— **8/8 真实变红**。

**未做（本批边界）**：`collection_state.py` 是**跨库的家**，但 **MySQL / SQLServer / Oracle
尚未迁入**（它们各自还有一份口径）。Oracle 已有 `source_status` / `state_is_collected` 的
等价实现（F-12 的原型就是从那儿来的）；MySQL / SQLServer 是**下一个候选批**。

## 1D. 批 2D：11 张"采了却在包里躺着"的表（2026-10-16 落地）

**问题**（PG 1016 真包逐表核对）：`stat_io`(35) / `iostat_snapshot`(23) / `inodes`(9) /
`block_devices`(6) / `database_conflicts`(4) / `hugepages`(3) / `stat_wal`(1) /
`checkpointer`(1) / `control_checkpoint`(1) —— `presentation.py` **从不引用这些键**。
后果：第 8 章「性能」只有一条 Buffer 命中率，**零 I/O 指标**、无检查点/WAL 统计、无大页检查、
无 inode 维度、无恢复冲突、无磁盘介质行。

**落地**（只动 PG 展示层，不新增采集）：新增 4.6 透明大页(THP) 与第 17 章「I/O 与 WAL」
（`pg.io` / `pg.wal`）；`system.host` 增磁盘介质行（公共层 `disk_media`）；`system.filesystems`
note 增 inode 最高使用率；`pg.replication` 增恢复冲突行。

### ★★ 两条对**所有库**都成立的判据

1. **引擎 `data_table` 按所有行的键取并集出列** ⇒ 同一个 item 的行**键集必须一致**。
   `pg.io` 初版把 3 列摘要行与 7 列明细行放一个 item，渲染成 **10 列稀疏表**（后 6 列全是
   「未采集」，读起来像"这 6 列没采到"）。守门写成**结构性断言**
   （`test_every_item_rows_share_one_column_set`）。**这类缺陷不报错、不抛异常、测试也不红。**
2. **共享的 `word_engine.PROFESSIONAL_ROW_LIMITS` 不能加 `system.*` 键** —— MySQL 与 Oracle
   用的是**同一批** `system.mounts` / `system.kernel` item_id（两边 `presentation.py` 都有），
   加键会连带改它们的产物。库专属截断要放**插件本地** + note 明示「展示前 N 条，共 M 条」。
   ⇒ 又一次印证「改共享引擎前先问另外三个库会怎样」。

### 口径落点：iostat 设备表

按 §2.2 的**物理盘口径**排除 `dm-*` 与光驱；同一台盘的多组采样取**峰值**
（均值会把"某一秒打满"抹平，而那正是要看的）。已在 `pg.io` 的 note 里写明排除口径。


## 1E. 批 3A：让"没采到"不再有机会长成"没问题"（2026-10-16 落地）

批 2C 把采集状态**贯通**了（桥建起来了），但审计发现它还有**三个漏口**和**一条真漏报**。
这一节只记**跨库通用**的判据 —— PG 侧的具体改法见 `postgresql-collector-fix-plan.md` F-35~F-38。

### ★★★ 判据一：状态词归并 & 展示词，必须是**两个东西**

章节的「采集状态」在**引擎**里的问法是"这一节能不能看分析结论"，语义是"非 ok 就压过分析结论"。
所以 `partial`（部分有证据）**必须归成 `ok`** —— 否则该节的 `risk` 结论会被盖成「部分可用」，
那是**用"证据不全"抹掉"发现问题"**。这个归并是对的。

**但它有个副作用**：元信息行直接印这个引擎用值 ⇒ 出现
「采集状态：**正常**；说明：本节**部分采集项未取得**，…」——**同一行前半句和后半句打架**。
只看前半句的人会以为证据齐了。真机实测 2 处，其中备份那节 `barman` 与 `pgbackrest`
**两个都不支持**（整块证据为空）却印「正常」。

这是「没采到」变「没问题」的**第三条路**：不靠编造数字（那是 `or <默认值>`），
而是靠**状态词归并把缺口洗掉**。前两条是 `if x` 吞掉真 0、`or <默认值>` 主动编造。

⇒ **通用改法**：判据层把**归并原值**（缺口数）单独带出去，渲染层改写**展示词**，引擎用值不动。
必须做成 **opt-in**（键不存在时行为不变）—— 共享引擎改一个字都可能动到另外三个库的产物。

### ★★★ 判据二：同名 basename 碰撞时，要按**路径层级**分优先级，不能按文件名分桶

同一个 stem 会**跨目录碰撞**：`tables/X.tsv`（顶层遗留采集点）与 `databases/<库>/X.tsv`（逐库活体）。
按 basename 分桶会把两层混在一起归并 ⇒ **废弃项的 `skipped` 把"已经采到"稀释成 `partial`**。
真机实测 **10 个指标**中招（`stale_tables` / `dead_tuples_total` / `unused_index_count` / …）。

**它当时没出错，纯属侥幸**（`partial` 恰好也在 `DATA_STATES` 里）。这是**埋在桥里的雷**：
哪天有人把 `partial` 移出 `DATA_STATES`，这 10 个指标会**一起变「证据不足」**，全线假阴性。

⇒ **通用改法**：按**目录层级**取优先级最高的那一层（越具体的路径越可信）。
★ 负控制必须配"**过度修正**"那一组：改成"任一层有 `ok` 就整表算 `ok`"会把逐库 `empty` 洗成 `ok` ——
所以还要有一条"逐库全空、顶层有 ok 时必须报 `empty`"的配对断言。

### ★★★ 判据三：完整度的**分母**，与"这项采到没有"是两把尺子

台账里登记了"本环境根本不适用"的项（`skipped` 按设计跳过 / `unsupported` 工具未装）。
**登记是对的**（读者要知道为什么没有这一项），但**算进分母是错的**：
真机 113 项里 16 项属此类 ⇒ 完整度 83.2%，而**真正"该采未采"的只有 3 项**。
照原口径，**目标机装的东西越少、完整度越低** —— 而真实结论恰恰相反。

⇒ **通用改法**：分母剔除"不适用"，且**缺口清单一项不少**（剔出分母是统计口径，不是"别让读者看见"）。
同时把 `in_scope_items` / `out_of_scope` 显式带出 —— **口径可见才可争辩**。

### ★★★ 判据四：打分一律**正着数**，禁用判否式

`ok = total - len(非ok)` 这种写法，在新状态词忘了登记时**不抛异常、不报错、分数反而变高**。
实测过的具体漏洞：**缺状态（空串）不在任何失败集合里 ⇒ 被算成「到位」**。

⇒ **通用改法**：`status in SCORE_OK_STATES`（正着数）。最坏后果是"新状态被算成缺口"
（分数偏低、缺口表多一行），**方向是安全的**。与批 24 记的 `error_states` 是**同一种坏味道**。

### ★★ 判据五：「不适用」与「没采到」必须有**两个名字**

`is_no_data()` 答"能不能下分析结论"；`is_out_of_scope()` 答"该不该进完整度分母"。
两者对 `skipped`/`unsupported` 都为真，**语义却不同**。混用会同时踩上面两条坑。
★ 缺状态（空串）**只算没采到、不算不适用** —— 否则分母会被"没登记"悄悄掏空。

### ★★ 判据六：**"采到了真问题却没报"** 要用台账反查，不要靠读报告猜

真机包里 `db.mydb.missing_fk_indexes` 状态 `ok`、1 行（外键 `orders.user_id` 无支撑索引），
而整份 DOCX **连"外键"二字都没有**。这类漏报读报告是**看不出来**的 —— 空章节看起来像"没问题"。

⇒ **通用做法**：拿**台账**（不是报告）反查 `status=ok 且有数据行` 但**插件里搜不到该 stem** 的项。
本批用它一次就捞出 1 条真漏报 + 4 项有数据的证据（`ip_address` / `ip_route` /
`socket_summary` / `postgres_processes`，见下批候选）。
★ 另一种同类毛病：**模板话术压过事实**（冗余索引为 0 却写"建议清理冗余索引"）——
与"没采到印正常"**同源、方向相反**：那边是掩盖，这边是**无中生有**。

### 批 3B 候选（本批未做）

网络侧证据（`ip_address` 17 行 / `ip_route` 16 行 / `socket_summary` 10 行）与
`postgres_processes`（16 行）**采到了但报告里没有网络章与进程章**（MySQL 有网卡 + SAR 网络历史）；
另有 10 张"本机是 empty、换客户大概率不是"的高价值表：
`idle_transactions` / `prepared_xacts`（**都挡 vacuum ⇒ 表膨胀，PG 最经典的隐性故障**）、
`vacuum_progress` / `invalid_constraints` / `subscriptions` / `wal_receiver` /
`public_privileges` / `security_definer_functions` / `db_role_settings` /
`file_settings_errors`（`postgresql.conf` 里的**无效参数**）。



## 2. 两条口径决策

### 2.1 采集失败时，半截输出留不留

| 情形 | 改动前 | 改动后（三库统一） |
|---|---|---|
| 表格类（`*.tsv` / `*.psv` / `*.csv`） | MySQL/Oracle 保留半截、PG 清空 | **清空** —— 残缺表格比没有更危险：下游按行列取值看不出少了行；"没采到"这件事只由 `collection_status` 表达 |
| 纯文本证据（`evidence/*.txt`） | MySQL/Oracle 保留、PG 清空 | **保留** —— 半截仍能定位（dmesg 部分输出、chronyc 原文），状态已标 `error`/`timeout` |

理由：两个极端各有隐患 ——"全清"丢掉诊断线索，"全留"让不完整表格混进解析。
按扩展名分流是唯一同时成立的写法，与既有惯例一致（`mysql_query_tsv` 早就在失败时
补 `# no rows returned; see collection_status.json`）。

### 2.2 `dm-*` 设备算不算"磁盘设备"

`/sys/block/dm-N` 是 device-mapper 逻辑设备（LVM LV / multipath / dm-crypt）。
它与底层物理盘统计**同一份 IO**：

- 对 LVM：同一笔 IO 在 `dm-N` 与 `sdX` 各记一次，报告磁盘表出现两行描述同一个盘；
- 对 multipath/ASM：`dm-N` 才是业务实际走的聚合设备，`sdX` 是单条路径。

**采用"物理盘口径"**：三库统一排除 `dm-*`，理由是报告的判据是"磁盘利用率过高"
（物理盘才是能扩容/能换的对象），且 LVM 场景下的重复计数更普遍。
代价（已知且接受）：多路径场景下只剩单路径视图 —— 该视图由 `multipath -ll` 单独取证
（Oracle 的 `system.asm_multipath` 已在采），不在实时磁盘表里表达。

若将来要改成"业务设备口径"，**三库一起改**，且判据要移到 `inspection_core/system_checks.py`
（不能在采集层各排一次）。

---

## 3. 本轮未做（批 2 候选，按价值排序）

| # | 项 | 现状 | 为什么这轮没做 |
|---|---|---|---|
| ~~B1~~ | `has_cmd` 缺失时**不登记**采集项 | 真正会缺的恰是 `numactl`/`ss`/`iostat`/`dmesg`/`chronyc`/`ntpq`/`timedatectl`（精简安装、容器化、老系统），**越不规范的机器分数越高** | ✅ **2026-10-16 已落地**（三库 21/17/12 处，见 §1A）|
| B2 | PG 多采 `iostat -xz 1 3` 快照、`kernel.shmmax/shmall` | 只有 PG 有 | `iostat` 与实时磁盘采样（bytes/iops/util/await）重复；`shmmax/shmall` 对 MySQL 无意义（Oracle 已采）。**建议：不搬**，仅在 interface 文档标注差异属"库专属"，避免下次又当成漏项 |
| ~~B3a~~ | PG `--include-log-text` 死旋钮 | 声明+解析+帮助三处齐全，**全脚本无人读** | ✅ **2026-10-16 已落地**：按"删掉"处置（三处一起删 + 墓碑注释）；守门 `tests/test_collector_cli_flags.py` 覆盖三库，用删除前的真版本校准过 |
| B3b | `summary.txt` 文案（`samples/*.csv` 完成情况提示） | MySQL 有、PG 无 | **仍未做**。PG 侧动作：`summary.txt` 增一行"本次样本采集完成情况 x/y"。⚠ 注意 MySQL 那句"错误日志原文未采集（--include-log-text 未开启）"在 PG **不适用** —— PG 现在根本没有日志原文采集，写这句会指向一个不存在的开关，属"撒谎的提示" |
| ~~B4~~ | `record_skipped` 位置 / `collect_sadf_metric` 遗留变量 | 三库定义顺序与未使用变量略有出入 | ✅ **2026-10-16 已落地**（零行为差异，`_work/b4_equiv.py` 三向等价性证明）：① MySQL 的 `record_skipped` 从 collectors 区（L946）前移到 `record_status` 之后（L282），与 PG 同序；② 三库 `collect_sadf_metric` 删掉「赋值后从未被读」的局部变量（PG/Oracle 是 `metric_id`，MySQL 还多一个 `tmp`）；③ 顺带处置 **F-06**：PG 删掉不可达的 `LEGACY_PASSWORD` 分支 |

---

## 4. 验收（可复跑）

```bash
# 1) 三个采集脚本语法
bash -n inspection/mysql_inspection_standard.sh
bash -n inspection/pg_inspection_standard.sh
bash -n inspection/oracle_inspection.sh

# 2) OS 层跨库对齐自测（生成期从三个脚本抽取真函数，分节跑）
C:/Users/wangjs/.workbuddy/binaries/python/versions/3.11.9/python.exe _work/os_selftest/gen.py

# 3) 采集脚本静态门（可单跑，也会被全量 pytest 带上）
python -m pytest tests/test_collector_cli_flags.py tests/test_collector_helpers_defined.py -q
#    · cli_flags       ：每个 CLI 开关必须有 ≥1 个读取点（防假旋钮，F-05）
#    · helpers_defined ：record_* / capture_* / mark_* 必须在同一文件里有定义（防 B1 那次写错函数）

# 4) 运行级探针：抽出真实 if 块跑「命令缺失 / 存在」两条路径，并与 HEAD 对照
python _work/b1b_runtime.py
python _work/b1b_negctl.py        # 负控制：坏行塞回去必须变红，再逐字节复原

# 5) 基线回归（分析层未改判据，业务结论应逐项不变）
python -m pytest -q
python tests/compare_analysis_outputs.py
```

自测覆盖：`classify_failure` 六类归类（含负控制"不许把 socket 不存在误判"）、
`capture_command` 三种落盘、三库 `compute_sar_coverage` 同一组夹具（连续 / 90 分钟空洞 / 大空洞）、
**负控制**（同一份 gap100 夹具：旧口径 `ok` vs 新口径 `partial`）、`hugepages` 分档。

**结果（2026-10-15）：`PASS=40 / FAIL=0`**（A 7 / B 6 / C1 5 / C2 5 / C3 5 / D 2 / E 10）

- **C1/C2/C3** = 三库各跑同一组夹具，结论**逐项相同**（`contiguous→ok` / `hole→partial` / `gap100→partial`）；
- **D** = 负控制成立：`gap100` 的 `max_gap=5400s`、累计覆盖仍 `79200s ≥ 77760s` ⇒ 旧口径必给 `ok`、新口径 `partial`；
- **E** = `hugepages` 判据段**从三库真脚本抽取后执行**（首版自写 `hp_verdict()` 手抄判据 = 测替身，已废），并断言三库行数算式逐字同形。

> ⚠️ 自测工具自身踩到的两个坑（都在 `_work/os_selftest/gen.py` 里修掉，记下来免得误判成采集脚本的 bug）：
> ① **两个 gen.py 实例并发**会共享 `$TMP_DIR/sar_coverage_epochs.txt`，Windows 文件锁让
> `sort -nu <f> -o <f>` **直接阻塞** —— 现象是"某节跑十几分钟没动静"；现已让每个用例独享 tmp + 加并发锁。
> ② `subprocess.run(timeout=…)` 抛 `TimeoutExpired` 时没兜住 ⇒ 整个生成器崩掉、已缓冲 stdout 丢失，
> 现象酷似"日志只写到 A 节、后面几节没跑"（实际产物文件时间戳证明跑过）。

> 自测落在 `_work/os_selftest/`（临时，不入库）；固化进 `tests/` 的做法沿用
> `tests/selftest/mysql_collector_batch*.sh` 的形态，等批 2 一起收口，避免现在
> 就为"只改了 PG/三库各一行"的判据新增测试文件。

---

## 5. Python 侧配套（批 24 追加：采集脚本之外的两处改动）

采集面的四项改动在 Python 侧只需两处配套，**每处都补了守门 + 负控制**：

| # | 改动 | 守门 | 负控制结果 |
|---|---|---|---|
| P1 | `plugins/postgresql/package_adapter.py`：`error_states` 增加 `schema_mismatch` | 新增 `test_schema_mismatch_counts_as_error_not_ok`（含**防误伤配对**：`empty` 不许算进 `errors`） | 从集合里删掉该词 → `AssertionError: 0 != 1 : schema_mismatch 被静默算成 ok 了`；逐字节复原 |
| P2 | `inspection_core/word_engine.py`：`STATUS_DISPLAY` 增加 `"schema_mismatch": "与版本不匹配"` | 既有 `test_no_known_status_renders_with_ascii_letters` | 同上方式变异 → **见 §5.1：原本居然还是绿的** |

### 5.1 尺子自证抓到的真实缺陷：那条守门**兜不住漏键**

`test_no_known_status_renders_with_ascii_letters` 原本这样取候选集：

```python
candidates = set(STATUS_DISPLAY) | set(_DATA_STATES) | set(_NO_DATA_STATES) | {...}
```

**这是自我引用**：被测对象（`STATUS_DISPLAY`）自己漏了键，候选集里也就没有这个键，
于是测试**照样绿**。

实测（批 24 尺子自证）：把 `"schema_mismatch"` 从 `STATUS_DISPLAY` 里删掉 → 该测试
**`1 passed`**。换句话说，批 19 修掉的那类缺陷（附录状态列原样印英文）**将来再犯一次，
这条守门抓不到**。

**修法**：候选集改为**外部清单**驱动 —— 在测试里显式维护「四个采集器 + 规则层写得出来的状态」，
先断言 `STATUS_DISPLAY` **覆盖**它，再逐个渲染：

```python
missing = (collector_states | rule_states) - set(STATUS_DISPLAY)
self.assertEqual(missing, set(), f"STATUS_DISPLAY 漏了这些状态的中文名：{sorted(missing)}")
```

改完重跑尺子自证：删掉 `schema_mismatch` → **FAILED**，指名
`漏了这些状态的中文名（会原样印进交付物）：['schema_mismatch']`。

> 代价要说清：外部清单会随状态词演进，**新增状态词时必须同步改这份清单**。
> 这正是想要的 —— 状态词表变更属于**要人审**的改动，不该静默通过。

### 5.2 结论：这两条用同一把尺子量

`error_states`（P1）和 `STATUS_DISPLAY`（P2）是**同一种坏味道** ——
「**不在集合里就算通过**」的判否式判定。它俩的漏点长得一模一样：

- 新增一个状态词 → **不抛异常、不报错、测试不红**；
- 在 P1 里后果是**采集完整度分数被抬高**（越不规范越好看）；
- 在 P2 里后果是**英文枚举印进交付物**（客户看到 `schema_mismatch`）。

所以两条都配了「变异实现 → 守门必须变红」的自证，而不是只写断言就完事。

## 1F. 批 5：「缺席」被读成「没问题」的两个**新形态**

批 3A 收口了采集层的四条路径（见 §1E）。批 4 / 批 5 在**报告首页**又挖出两处 ——
它们既不在采集层也不在规则层，而在**呈现层**，所以同样不报错、不抛异常、测试不红：

| # | 形态 | 跨库通用判据 |
|:--:|---|---|
| ① | **口径分叉**：同一份台账，完整度用「剔出不适用项后的分母」，首页那句话却用 `len(gaps)` | 凡"某个数字在报告里出现两次"，必须**同源**；`is_out_of_scope()` 是公共层唯一判据 |
| ② | **静默缺席**：某节因渲染判据不成立而整节消失，读者分不清"没问题"与"没渲染" | 判据不成立的**可见原因**必须落进成品，且**只在该判据可证时才写** |

### 1F.1 口径分叉（①）

- **实测**：真机台账 20 项，其中 **16 项是"本环境不适用"**（未安装对应组件、或该采集项
  已由逐库路径覆盖）。完整度 96.9%（分母已剔掉不适用项），而首页那句话写作
  「另有 **20** 项证据缺口」⇒ **同一页两个数字打架**。
- **修法**：句子里把 **总数 / 真缺口 / 不适用** 三个数字分开说，判据一律走
  `inspection_core.collection_state.is_out_of_scope()`（与完整度分母**同一份实现**）。
- **★ 落地守则**：**只有确实存在不适用项时才改句**，否则**原句逐字保留**。
  这样 MySQL（台账只有 1 条 `external_evidence_required`）的文档 hash **不变** ——
  「改共享引擎先问另外三个库会怎样」由此从口号变成**一条 hash 门**。

### 1F.2 静默缺席（②）

- **实测**：首页「1.2 高风险项速览」的判据是 `severity in {critical, high}`。某库本机
  **8 条 critical 规则全部 `passed`** ⇒ 0 条高风险 ⇒ **整节消失**。读者对照另一库的报告
  （1.1 / 1.2 速览 / 1.3 管理关注）会以为"少了一节"。
- **修法**：引擎加一条 **opt-in** 分支 —— 模型声明 `declare_no_highlights` **且**
  **风险登记册非空**时，渲染该节并说明「未发现严重或高风险项，登记册共 N 条」。
- **★ 两条硬约束（缺一不可）**：
  1. **必须 opt-in**：无此键的库走原路径，产物逐字节不变（同上，由 hash 门兜住）；
  2. **登记册为空时不许声明** —— 那是把"没评价"说成"没问题"。
     负控制里专门留了这条**过度修正配对**，它**必须变红**。

### 1F.3 这把尺子怎么复用（四库通用）

出现下面任一征兆时，按本节查：

1. **同一个数字在报告里出现两次、值还不一样** → 找两处实现，收敛到公共层；
2. **某节"有时候有、有时候没有"** → 找它的渲染判据；若判据是"有数据才渲染"，
   就问：**"没有数据"与"没渲染"在成品里能不能区分**？
3. **任何新增的"显式声明"分支** → 必须 opt-in，且必须配一条"无从证明时闭嘴"的配对守门。

> 与 §1E 的关系：**§1E 管采集层的"没采到"，§1F 管呈现层的"没呈现"。**
> 两者合起来才是「凡缺口必可见、凡可见必同源」。

## 1G. 批 3B-B1：「采了没报」——台账 `ok`、文件有内容、报告零引用（2026-10-19 落地）

§1D 讲"采了却在包里躺着"（11 张表），§1E 讲"没采到被读成没问题"。
本节是第三种，也是最容易漏的一种：**采集与台账都正常，坏在报告层根本不读**。

PG 侧实测 5 项：`system.ip_address`(17 行) / `system.ip_route`(16) /
`system.socket_summary`(10) / `system.postgres_processes`(16) / `pg.capability_probe`(17 键)，
台账全是 `ok`，而 `presentation.py` grep 这五个名字**一次都不出现**。
另有 `system.inodes`(9 行) 只把最高值拼进 note 一句话、`numa.txt`(7 行)
登记了来源但没有任何一行来自它。

### 1G.1 对账法：文件清单 × 四个消费点

判"某项采了没报"不要靠肉眼翻报告，按下面四路各查一遍（缺一路就会漏）：

| 消费通道 | 形态 | 查法 |
|---|---|---|
| `ctx.raw_lines("x.txt")` | 非表格式原文证据 | grep 文件名 |
| `ctx.tables.get("x")` | `tables/*.tsv` | grep 表 stem |
| `ctx.find_table(...)` | 带子串兜底的查找 | 注意**兜到错表且不报错**（见 §1E） |
| `model["...source"]` | 报告模型的 `items[].source` 字面串 | grep `tables/` / `evidence/` |

工具：`_work/scan_b1_unused.py`（PG 侧）。**四路都没命中 + 台账 `ok` ⇒ 及格线，不是加分题。**

> ★ 「加分题（缺采集项）」的定义是"少采、采得浅"；"采了不用"不属于它。
>   F-38（外键索引全链路无人消费）当时判的就是及格线，这里必须用同一把尺子。

### 1G.2 进程快照：判据必须落在 COMMAND 列，不能对整行匹配

```bash
# ✗ 错：`su - postgres` 之后整条 shell 树的 USER 列都是 postgres，全部命中
ps -eo pid,ppid,user,etimes,%cpu,%mem,args | awk 'BEGIN{IGNORECASE=1} /[p]ostgres/ {print}'

# ✓ 对：只看 COMMAND 的第一个词
ps -eo pid,ppid,user,etimes,%cpu,%mem,args \
  | awk 'BEGIN{IGNORECASE=1} $7 == "postgres" || $7 == "postgres:" || $7 ~ /\/postgres$/ {print}'
```

PG 真机同一份数据：**16 行 → 8 行**，剔掉的 8 行是 `su - postgres`、`-bash`、
3 个 `bash xx_inspection.sh`、`sleep`、`ps`、`awk`。

两条纪律：
1. **展示层还要按命令行重判一遍**（旧包已经采脏了，改采集器救不回来）；
2. **剔掉的行数必须印进 note** —— 「已过滤 8 行」与「只采到 8 行」是两件事，
   不说清楚读者会把前者当成后者。

判"后台进程齐不齐"时，期望集合要**按能力裁剪**（`archive_mode=off` 时不该有 `archiver`，
备库才有 `walreceiver`）。拿固定清单比会把"本环境不适用"报成"缺失"——与 §1E 同一条纪律。

### 1G.3 容量有两个维度：空间与 inode，木桶取最短

`df -h` 与 `df -i` 是两个独立维度，任一满都会写不进去。只判空间会漏掉
"磁盘还有 60%、inode 100%"这种典型故障（小文件多 / 临时表多 / WAL 段没清）。

- 两个维度**并列成列**，不要塞进 note（note 里的一句话没人看得到）；
- 风险判据取**最短的那块板**（任一 ≥80% 即 `risk`）；
- 处置建议分开：inode 满要"清小文件 / 提高 inode 上限"，空间满才是"清日志/WAL 或扩容"；
- **取不到时写「未采集」，不许写 `0%`** —— 「没取到」与「用了 0%」必须能区分（§1E 同源）。

### 1G.4 同网段多 IP 要按 cidr 合并

`ip route show table all` 对同一网段的每个地址各出一行（本机 `192.168.100.0/24`
同时挂 `.80` 与 `.130`）。逐行铺开会让人以为配了两条网段。按 cidr 分组、把 `src` 聚成列表。

> `table local` 的排除是**防御性**的：iproute2 6.x 下这些行首列是 `local` / `broadcast`
> 关键字，本来就命中不了 CIDR 判据。**别拿它当守门断言**（见 1G.6）。

### 1G.5 能力表的键必须有口径，候选集从采集脚本取

能力表的值是 `0/1` 或裸字符串（`wal_level=replica`），没有说明列就读不懂
`pg_stat_io=0` 意味着什么。两件事：

1. 口径收在**模块级单一表**（PG 的 `CAPABILITY_NOTES`），不许散落到各处手写；
2. 守门的候选集要**从采集脚本抽**（`printf 'xxx\t%s'`），不能从口径表自己取 ——
   从被测对象取样本的话，实现漏了键、候选集跟着漏、测试照样绿。

### 1G.6 负控制会抓出「假设门」：一条恒真的断言比没有断言更坏

批 3B-B1 的负控制 **M5** 是这么暴露的：原先断言「`table local` 不能进直连网段」，
而这类行在现有 iproute2 格式下**本来就进不来** ⇒ 把"按 cidr 合并"整段删掉，测试照样绿。
**它给人一种"已覆盖"的错觉。** 改断言「直连网段恰好 1 条」之后 M5 才真实变红。

判据：**每组变异都要能指出"是哪条断言把它抓住的"**。指不出来的，那条断言就是假设门，
要么换一条能测出变异的，要么删掉并在代码里写明"这是防御性判断，不作守门"。

### 1G.7 四库落地状态

| 库 | 网络/进程/能力/inode | 状态 |
|---|---|---|
| PostgreSQL | 5 项采了没报 + 进程采集判据脏 | ✅ 2026-10-19（批 3B-B1，F-43~F-47）|
| MySQL | 采集端有 `ip_address`/`ip_route`/`socket_summary`/`processes`；报告章未见独立网络/进程节 | ⬜ 待按 1G.1 对账 |
| Oracle | 采集端有 `system.ip_address` / `system.ip_route` / `system.socket_summary`（L1517-1533）| ⬜ 待按 1G.1 对账 |
| SQL Server | 待核 | ⬜ 待按 1G.1 对账 |

> 三库都**不要照抄 PG 的章节位置**：PG 把网络/进程放进「系统信息」章（4.7 / 4.8），
> 是因为 MySQL 标杆本身没有独立网络章。各库应以自己的标杆报告结构为准，
> 本节只约束**判据与披露口径**，不约束章节编号。
