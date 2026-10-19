# PostgreSQL 巡检链路改造清单（FIX-PLAN）

> 对象：`inspection/pg_inspection_standard.sh`（采集层）+ `plugins/postgresql/*`（分析层/报告层）+ `inspection_core/word_engine.py`（渲染层，读产物复核）。
> 排序：**采集 → 分析 → Word**，与用户要求的整改顺序一致。
> 配套文档：[`postgresql-collector-interface.md`](./postgresql-collector-interface.md)（Python 对接契约，缺一不可）。

## 0. 行号基准与读法

- **行号基准**：本次审查版本 —— `pg_inspection_standard.sh` = 97609 字节 / 1362 行 / 纯 LF / 无 BOM；`COLLECTOR_VERSION=2.0.1`、`SNAPSHOT_SCHEMA_VERSION=1.0`、`PACKAGE_VERSION=1.0`。
- 分析层行号基准：`plugins/postgresql/` 当前工作树版本。
- **改脚本后行号必然漂**：按行号找不到时，**以符号名（函数名 / item_id / 键名）为准**。

## 1. 证据分档（每条结论按此标注）

| 档 | 含义 | 处置 |
|:---:|---|---|
| **A** | 代码事实 + 本机实测输出（可复跑） | 直接采信 |
| **B** | 官方手册原文 / 权威文档 | 可直接采信，注明出处 |
| **C** | 改法本身 + 未实测路径 | 需现场一条命令升级为 A |

本清单唯一一条 C→A 的升级命令（F-01）：
```bash
psql -A -F $'\t' -c "SELECT E'a\tb' AS t, E'c\nd' AS n" | od -c | head
```
- 若输出里出现**裸的 `\t`（0x09）与裸的 `\n`（0x0a）** ⇒ unaligned 不做转义，F-01 为 P0 成立。
- 若出现字面量 `\t` / `\n`（两个字符）⇒ 转义存在，F-01 降级为 P2（但 F-02 不受影响，仍为 P0）。

## 2. 事实基线（本机实测，A 档）

| 项目 | 值 |
|---|---|
| 脚本行数 / 函数数 / 注释行数 | 1362 / 61 / **31**（注释密度 **2.3%**）|
| 注释构成 | 文件头 5 行、`# ==== 分段 ====` 12 行、参数注释 2 行、**真正的踩坑记录 5 行**、其余为续行 |
| 采集器登记 item_id | **93 个字符串**（含 18 个模板/变量）→ 具体 item_id **约 75 个**（逐库项 `db.<db>.<项>` ×15 模板、系统项 29、pg 项 30、backup/ha 6）|
| `tables/*.tsv` 名（脚本产出） | **49** |
| `known_tsv_header` 覆盖 | **23**（无孤儿条目）⇒ **26 项无占位表头** |
| 规则包 | 44 条规则、8 个 `globals` 阈值键、**全部被代码消费（无死键）** |
| 图表 | 6 张（`os_chart_specs` 4 + `pg_sessions` + `pg_stats`）|
| 报告章节 | `section_order` 14 节；`presentation` 实际登记 item **18 个** |
| 单测 | `tests/test_postgresql_plugin.py` **8 passed**（基线 `tests/baselines/postgresql/legacy_2_0_1/` 本机存在，被 gitignore）|
| 全量 pytest 基线 | 562 passed / 5 failed（全部 `tests/test_mysql_*`，源包不在仓库）|

## 3. 「乱不乱」的判定：**骨架不乱，边界乱**

### 3.1 骨架（值得保留，明确不要回改）

- **职责切分干净**：`parse`（`parsers.py`）/ `package`（`package_adapter.py`）/ `metric`（`metrics.py`）/ `chart`（`charts.py`）/ `rule`（`rules.py`）/ `present`（`presentation.py`）/ `adapt`（`report_adapter.py`）七段，与 MySQL/Oracle 同构。
- **`Analyzer` 只做编排**：有单测钉住（`tests/test_postgresql_plugin.py::test_analyzer_delegates_package_and_rule_boundaries` 断言 `analyzer.py` 里**不得出现** `safe_extract_tar(` / `RuleEngine(` / `fig.savefig(`）。
- **库专属图不碰 matplotlib**：`charts.py` 只产 spec，渲染/配色/断点/Pillow 降级全在 `inspection_core/charts/render.py`（批 20 收口），文件头写了"插件不许再 import matplotlib"。这是**正确的分层**，不要为了加图把它推翻。
- **SAR 用 `parse_sadf` 而不是 `parse_csv`**：`package_adapter.py` L68-74 的注释记录了"用 `parse_csv` 读 sadf 会把每张 history 图读空"——**同一份"两处硬编码必须同步"的教训已经落到代码上**，这条也别动。
- **`per-db` 聚合方式正确**：`package_adapter.py` L53-56 把 `databases/*/*.tsv` 按 basename `extend` 后 `tables.update(...)`，于是 `dead_tuples_total`/`no_pk_count` 是**全部库的合计**，不是双份。
- **口令处理**：`umask 077`、`--password-file` 优先、socket 连接跳过口令、`cleanup_auth` 在 `EXIT`/信号路径都调、`tar --exclude='*/.pgpass.*'`。这套是好的。
- **`PGOPTIONS` 只读保护**：`export PGOPTIONS=... default_transaction_read_only=on -c statement_timeout=... -c lock_timeout=5000`（L1265）——**采集器自身不会写库**，这是本脚本最值得保留的一条设计。

### 3.2 边界（错位清单，逐条见 §4）

| # | 边界问题 | 条 |
|:---:|---|---|
| 1 | 采集原语不保证行结构（值内 TAB/LF 撑破 TSV） | F-01 |
| 2 | 占位表头只覆盖一半表，且逐库版列数多一列 | F-02 |
| 3 | 可选命令缺 `else` ⇒ **缺项不登记 ⇒ 完整度分数虚高**（**实测 21** 个 item，见 F-08） | F-08 |
| 4 | `item_id` 与 `category` 不一一对应（`ha.*` 归进 `pg.backup`） | F-09 |
| 5 | 规则引擎的 `available` 维恒真 ⇒ 采集与判定脱钩 | F-12 |
| 6 | **展示层 item_id 与采集层 item_id 是两套命名空间**，其中 5 个还是采集层的 `category` 名 | F-15 |
| 7 | 同一事实（`stale_tables`）在 `metrics.py` 与 `presentation.py` 各实现一遍且口径不同 | F-16 |
| 8 | 采集状态读的是 `snapshot["collection_status"]`，**而 snapshot.json 里没有这个键** | F-14 |
| 9 | 结论层用**字符串替换**打补丁（`未检测到备份策略` → …）盖住根因 | F-19 |
| 10 | `presentation.py` 有约 250 行与 `analyzer.py` 逐字重复的死代码 | F-20 |
| 11 | `system.hugepages` 无条件记 `ok`（0 行也 ok）+ **PG 侧零消费者** | F-27 |

## 4. 改造项（F 编号）

级别定义：**P0 = 交付物会说错话 / 数据静默失真**；**P1 = 口径分叉、契约不闭合**；**P2 = 可维护性、冗余**。

### 4.1 汇总表

| 编号 | 级别 | 层 | 一句话 | 批次 |
|:---:|:---:|:---:|---|:---:|
| F-01 | P0 | 采集 | psql unaligned 不转义字段分隔符 ⇒ TSV 行结构可被撑破，且只有 `pg_stat_statements.query` 单点修过 | 批 1 |
| F-02 | P0 | 采集 | `known_tsv_header` 覆盖 23/49；逐库版比占位表头多 `database_name` 一列 | 批 1 |
| F-03 | P1 | 采集 | **撤回**：`coverage.tsv` 读写未错配；`coverage.json` 无任何读者 | 批 1 |
| F-04 | P1 | 采集 | SAR `ok` 判据只看累计覆盖，不看 `max_gap_seconds` ⇒ 中间空洞也算 ok | 批 1 |
| F-05 | P1 | 采集 | `INCLUDE_LOG_TEXT` 声明 + 解析 + 帮助都有，**全脚本无人读** | ✅ **批 2 已落地（2026-10-16，删掉旋钮）** |
| F-06 | P1 | 采集 | `LEGACY_PASSWORD` 是死分支（没有 `--password` 选项） | ✅ **批 2 已落地（2026-10-16，删掉死分支）** |
| F-07 | P0 | 采集 | 退出码被**最后一行 `&&` 链**绑架：`--no-package` 与"扫描拦截"都返回 1 | 批 1 |
| F-08 | P1 | 采集 | 21 个 `has_cmd` 采集项在命令缺失时**不登记**（A 8 / B 5 / C 3 + 尺子补出 5）⇒ 分母变小 ⇒ 完整度虚高 | ✅ **批 1 已落地（2026-10-16）** |
| F-09 | P1 | 采集 | `ha.patroni/repmgr/manager`、`system.backup_*` 归进 `pg.backup` 类 | 批 1（只登记）|
| F-10 | P1 | 采集+分析 | 同名表两份落点，靠 `dict.update` 的覆盖语义**偶然正确** | 批 1 |
| F-11 | P2 | 采集 | 注释密度 2.3%，"难点都注释了，接口一行没写" | 批 1（最先做）|
| F-12 | P0 | 分析 | `evaluate()` 的 `available` 维**全部写死 `True`**；`not_evaluated` 一词两义 | 批 2 |
| F-13 | P1 | 分析 | PG 自己定义 `Finding`/`RuleEvaluation`，与 `inspection_core/models.py` 并存 ⇒ 无整改三要素 | 批 2 |
| F-14 | P0 | 分析 | `presentation` 读 `snapshot["collection_status"]`，该键**在 snapshot.json 里不存在** | 批 2 |
| F-15 | P0 | 分析 | 展示层 18 个 item_id 中只有 6 个能对上采集层，5 个撞 `category` 名 | 批 2 |
| F-16 | P1 | 分析 | `stale_tables` / `connection_usage` 两处实现、口径不同 ⇒ 报告内自相矛盾 | ✅ **批 2 已落地（2026-10-16）** |
| F-17 | P1 | 分析 | 空集合 ⇒ **11** 条判据恒 `passed`；`settings` 缺失 ⇒ **5 条假告警 + 4 条假通过** | 批 2 |
| F-18 | P1 | 分析 | 采集侧 10 个状态词 vs 规则侧 3 个；`not_evaluated` 被一律翻成"需要外部证据" | 批 2 |
| F-19 | P1 | 分析 | 备份结论两处补丁（强制改写 + 字符串替换），根因未修 | 批 2 |
| F-20 | P2 | 报告 | `presentation.py` ~250 行重复死代码；`_build_multi_conclusions` **从不产出** | 批 3 |
| F-21 | P2 | 报告 | 3 条阈值硬编码（数据目录 80% / 死元组比 10% / 超户 3）未外置 | 批 3 |
| F-22 | P0 | 报告 | `collection.status` 恒 `ok` 直接印成 `采集状态：正常` | 批 3 |
| F-23 | P2 | 采集 | 顶层 9 个对象类查询（`table_health` / `large_tables` / `no_primary_key` / `indexes` / `sequences` / `duplicate_indexes` / `unused_indexes` / `bloat_estimation` / `partition_summary`）连的是默认 `postgres` 系统库 ⇒ **恒为空** | ✅ **批 2 已落地（2026-10-16）** |
| F-23 | P1 | 报告 | PG profile 没登记 `PROFESSIONAL_ROW_LIMITS`，`mounts` 可能几百行 | 批 3 |
| F-24 | P1 | 报告 | 图表数据不足时返回 `[]`（图**不存在**），而不是 `status="skipped"` ⇒ 无失败信号 | 批 3 |
| F-25 | P2 | 报告 | `default_company` 为既有公开可查串 —— **本轮不改**，仅登记防止反复提 | — |
| F-26 | P2 | 采集 | `history/coverage.json` 无读者（F-03 的下半条） | 批 3 |
| F-27 | P1 | 采集 | `system.hugepages` 无条件记 `ok`（0 行也 ok）+ **PG 侧零消费者**；硬编码形态**三库同形** | 批 1 |

---

## 5. 批 1：采集层

### F-01（P0）采集原语不保证行结构 —— psql unaligned 不转义字段分隔符

**代码事实**（`pg_query_tsv` L313、`pg_query_tsv_db` L342）：
```sh
pg_exec -A -F $'\t' -c "$query" > "$outfile" 2> "$err"; rc=$?
```

**B 档证据**（PostgreSQL 官方手册 `\pset format` / `unaligned`）：
> unaligned format writes all columns of a row on one line, separated by the currently active field separator. This is useful for creating output that might be intended to be read in by other programs, for example, tab-separated or comma-separated format. **However, the field separator character is not treated specially if it appears in a column's value; so CSV format may be better suited for such purposes.**

同段对 `csv` 格式：
> csv format writes column values separated by commas, applying the **quoting rules described in RFC 4180**. … Field separator characters other than comma can be selected with `\pset csv_fieldsep`.

**脚本自己已经承认过这个问题一次**（这就是"打补丁"的证据）：
- L821（会话库）与 L902（逐库）：`left(regexp_replace(query,E'[\\n\\r\\t]+',' ','g'),500) AS query` —— **做了换行/制表符清洗**。
- 但**同一批**还有 4 处查询文本没做：
  - L771 `pg.long_transactions`：`left(query,300) AS query`
  - L773 `pg.idle_transactions`：`left(query,300) AS query`
  - L775 `pg.lock_waits`：`left(blocked.query,240) AS blocked_query, left(blocker.query,240) AS blocking_query`
  - 另：`relid::regclass::text` / `relname` / `pg_get_indexdef(...)` / `gid` / `proconfig` 等自由文本列一律未清洗。

**下游后果**（`inspection_core/tabular.py` L9-21，A 档）：
```python
lines: list[str] = []
for line in stream:                      # ← 先按物理行切开
    if line.startswith("#") or not line.strip(): continue
    lines.append(line.rstrip("\n\r"))
reader = csv.DictReader(lines, delimiter=delimiter)
return [{str(k): (v or "") for k, v in row.items() if k is not None} for row in reader]
```
三种失真路径，**全都静默**：

| 值内字符 | 物理后果 | 逻辑后果 |
|---|---|---|
| `\n` | 多出一条物理行 | DictReader 当新记录，按表头**前 N 列错位映射** ⇒ 该记录后续列全部左移；原记录尾部列丢失 |
| `\t` | 多出一个字段 | 落进 `restkey=None`，被 `if key is not None` **直接丢弃** ⇒ 静默截断 |
| 值以 `"` 开头 | 触发 csv 引号规则 | `csv.reader` 把引号字段**跨物理行吞并** ⇒ 行合并 |

三条路径都**不抛异常、不写 status、`rows` 计数照记** ⇒ 错数据伪装成成功。
落到判据上：`long_transaction_count = len(long_xacts)`、`lock_wait_count = len(lock_waits)`、`max_transaction_seconds` 全部失真 ⇒ `PG.LONG.TRANSACTION` / `PG.LOCK.WAIT` 的触发与事实脱钩。

**before / after（推荐方案 A：单点改成 RFC 4180 CSV）**

`before`
```sh
pg_exec -A -F $'\t' -c "$query" > "$outfile" 2> "$err"; rc=$?
```
`after`（能力探测一次，结果进 snapshot）
```sh
# 主入口，能力探测之前或紧接其后
TSV_OUT_ARGS=(-A -F $'\t')
TSV_ESCAPING="unaligned"
if psql -X --no-psqlrc "${PG_CONN_ARGS[@]}" --csv -P "csv_fieldsep=$(printf '\t')" -c 'SELECT 1' >/dev/null 2>&1; then
    TSV_OUT_ARGS=(--csv -P "csv_fieldsep=$(printf '\t')")
    TSV_ESCAPING="csv"
fi
# 两个原语统一用它
pg_exec "${TSV_OUT_ARGS[@]}" -c "$query" > "$outfile" 2> "$err"; rc=$?
```
分析侧必须同步（`inspection_core/tabular.py`）：
```python
# before：先自己按行切，多行引号字段已经被切碎
# after：把文件句柄交给 csv 模块，由它按 RFC 4180 处理跨行字段
def parse_delimited(path, delimiter="\t"):
    if not path.exists() or path.stat().st_size == 0:
        return []
    with path.open("r", encoding="utf-8", errors="replace", newline="") as stream:
        rows = csv.DictReader(
            (line for line in stream if not line.startswith("#")), delimiter=delimiter
        )
        return [{str(k): (v or "") for k, v in row.items() if k is not None} for row in rows]
```

**为什么推荐 A 而不是逐列打补丁**
- A 是**单点**改动，覆盖"今天还没出现的列"；B（逐列加 `regexp_replace`）**只在见过的列上生效** —— 脚本已经在 `pg_stat_statements` 上犯过一次这个错，再打一次就是同一个坑。
- A 对**干净数据是字节等价的语义**：RFC 4180 只在值含分隔符/引号/换行时才加引号，而 Python 的 `csv` 模块本来就按 RFC 4180 解析 ⇒ 49 张表的解析结果不变。
- **前置条件**：`psql --csv` 是 **PG 12+ 客户端**才有。所以必须能力探测 + 回退，不能硬切（硬切到 PG11 客户端会让**每一个查询**都失败）。

**备选 B（不动输出格式，接受不完整）**：只给 L771/L773/L775 三处补 `regexp_replace(...,E'[\\n\\r\\t]+',' ','g')`。
- 省事，但**新增自由文本列会复发**；且 `relname`/`gid` 这类没覆盖。
- 若同时采纳 A+B：B 降级为"display 友好"（让报告里的 SQL 是单行），不再是结构依赖。

**验收**（C→A 升级 + 结构断言）
1. 现场跑 §1 那条 `od -c`，把结论写回本文档 §1（C 升 A）。
2. 造夹具：`printf 'a\tb\nc\td\n'` 与含 `"` 的值各写一个 `.tsv`，断言 `parse_delimited` 的**物理行数 == 逻辑记录数**、字段数 == 表头列数。
3. **负控制**：把 `parse_delimited` 改回"先按行切"，断言上面第 2 条**必须变红**（否则尺子在骗人）。

### F-02（P0）`known_tsv_header` 覆盖不全 + 逐库版列数漂移

**代码事实**
- `known_tsv_header`（L211-240）只有 **23** 条 `case`；脚本产出 **49** 个 `tables/*.tsv` 名 ⇒ **26 项无占位表头**。
- 23 条里**没有孤儿**（每条都真实产出），这点是好的。
- 列数漂移实例：`no_primary_key.tsv` 占位表头 4 列（L229 `schemaname table_name total_bytes estimated_rows`），而**逐库版** SELECT 是 5 列（L873 首列多 `database_name`）。同类还有 `table_health` / `large_tables` / `indexes` / `duplicate_indexes` / `unused_indexes` / `bloat_estimation` / `partition_summary` / `pg_stat_statements`（逐库版全部多一个 `database_name` 首列）。

**未覆盖清单（26 项）**：`bgwriter` `block_devices` `capabilities` `connections` `control_checkpoint` `control_system` `database_ages` `database_stats` `databases` `extensions` `filesystems` `hba_rules` `hugepages` `indexes` `inodes` `instance` `iostat_snapshot` `kernel_parameters` `large_tables` `memory_snapshot` `roles` `sequences` `session_states` `settings` `table_health` `tablespaces`。

**注意 `bgwriter` / `control_checkpoint` / `stat_*` 用的是 `SELECT *`**：列集**随 PG 版本变**（如 PG17 的 `pg_stat_bgwriter` 去掉了 `buffers_backend` 系列）。所以这些表的占位表头**不能写死**，只能写"运行期未知"标记。

**后果**
- rc != 0 时 `ensure_tsv_header "$outfile" || true`（L320/L349）⇒ 未覆盖的表**保持 0 字节**。
- rc == 0 且 0 行时 psql 自己会打表头 ⇒ 占位路径几乎不触发 ⇒ **占位表头机制事实上半失效**。
- 下游 `parse_delimited` 对 0 字节返回 `[]`，与"查了、确认 0 行"的 `[]` **完全同形**（F-12/F-14 的输入）。

**before / after**
```sh
# before：一条 case 表按 basename 匹配，主版/逐库版共用
no_primary_key.tsv) printf 'schemaname\ttable_name\ttotal_bytes\testimated_rows' ;;
```
```sh
# after：分"主版 / 逐库版"，并给版本相关表打"运行期未知"标记
#   —— 主版 vs 逐库版是**两套列集**，不能共用一条
pg_no_primary_key_主版)  printf 'schemaname\ttable_name\ttotal_bytes\testimated_rows' ;;
pg_no_primary_key_逐库版) printf 'database_name\tschemaname\ttable_name\ttotal_bytes\testimated_rows' ;;
# 版本相关表（SELECT *）：占位时给显式标记，别假装知道列
bgwriter.tsv) printf '# header_unknown: SELECT * 列集随 PG 版本变化（PG17 起移除 buffers_backend 系列）' ;;
```

**验收**
1. 结构断言：**占位表头的每一列都必须是真实 SELECT 的列名的子集**，且**列数相等**（把动态拼接的 `${TOP_N}`/`${pgss_total_col}` 替换掉再数逗号）。这条断言就是"两处硬编码必须同步"的机器化版本。
2. 夹具：对 49 个 basename 各跑一次"空结果"和"失败"两种，断言文件**要么有完整表头、要么有显式失败标记**，**绝不出现 0 字节**。
3. 负控制：故意把某条占位表头删一列，断言断言 **必须变红**。

### F-03（P1）**撤回**：`coverage.tsv` 读写未错配；`coverage.json` 无读者

静态读代码时我先看到 L587 的 `} > "$HISTORY_DIR/coverage.json"`，据此判过"写 json 读 tsv ⇒ SAR 覆盖永远 empty"。**逐字读完 573-587 行后撤回**：
- L573-583 写 `coverage.tsv`，L584-587 写 `coverage.json` —— **同一函数里两份都写**。
- 所有读者（L611-612、L1047-1050、L1108-1111）读的都是 `coverage.tsv` ⇒ **读写一致，无缺陷**。

留下的真问题只有一条：**`coverage.json` 在脚本内没有任何读者**（grep 全文只有 1 处），但 `generate_manifest` 会把它收进清单 ⇒ 契约里多一份"来历不明、无人消费"的文件。
处置：删掉，或在 INTERFACE 里登记用途（见 F-26）。

### F-04（P1）SAR `ok` 判据只看累计覆盖，不看空洞

**代码事实**（L562-571）
```sh
{ gap=$1-prev; if(gap>maxgap)maxgap=gap; if(gap>0 && gap<=3600)covered+=gap; else if(gap>3600)breaks++; prev=$1 }
...
status="partial"
[ "$coverage_seconds" -lt  $((SAR_HISTORY_HOURS*3600*9/10)) ] && status="partial"
[ "$coverage_seconds" -ge $((SAR_HISTORY_HOURS*3600*9/10)) ] && status="ok"
```
- `coverage_seconds` **只累加 `gap<=3600` 的间隔** ⇒ 大断点（>1h）不计数，但也不扣分。
- `ok` 的**唯一**判据是"累计覆盖 ≥ 请求区间的 90%"，**完全没有校验 `max_gap_seconds` 与 `discontinuity_count`**。
- 后果：一台 sar 只有早上 3 小时 + 晚上 3 小时、中间停机 18 小时的主机，只要 `covered ≥ 21.6h` 就判 `ok`；而实际中间是空的，报告把"就绪"给了客户。`report_adapter` L150-158 又用 `coverage_hours >= 0.8*requested` 再判一次"usable" ⇒ **两层判据都只看总量，不看连续性**。

**after**
```sh
status="partial"
if [ "$coverage_seconds" -ge $((SAR_HISTORY_HOURS*3600*9/10)) ]; then status="ok"; fi
# 连续性校验：空洞超过阈值一律不许判 ok
if [ "$max_gap_seconds" -gt $((SAR_HISTORY_HOURS*3600/10)) ]; then status="partial"; fi
if [ "$point_count" -eq 0 ]; then status="empty"; fi      # 现在靠 status 初值兜，容易漏
```
并补一句 reason 带上 `discontinuity_count=${discontinuity_count}`（**当前 reason 里没有它**，L620 只带 coverage）。
- 状态词：不要新造。`inspection_core/word_engine.py` 的 `STATUS_DISPLAY` **已有 `insufficient_history` → "历史不足"**，缺口表能直接吃 ⇒ 展示层零改动。

**验收**：造 3 个 `sar_cpu.csv` 夹具（①连续 24h ②前 3h+后 3h+中间 18h 空洞 ③空文件），断言状态分别是 `ok` / `partial` / `empty`，且**三者文案互不相同**（"两个相反结论不许长成一句话"）。

### F-05（P1）`INCLUDE_LOG_TEXT` 是死旋钮

- L29 `INCLUDE_LOG_TEXT=0`、L1226 `--include-log-text) INCLUDE_LOG_TEXT=1`，`show_usage()` 里也列了它。
- **grep 全文只有这 2 处** ⇒ 没有任何代码读它。
- 与 skill 里那类"死键不报错"同源：客户按文档打开这个开关，**行为零变化，脚本不提任何警告**。
- 处置二选一（**不要留着**）：
  - 接上：`pg.log_summary` 命中 `PANIC|FATAL|DEADLOCK|OOM|WRITE_FAILURE` 时，在 `INCLUDE_LOG_TEXT=1` 下额外输出 `evidence/pg_log_tail.txt`（脱敏后 ≤200 行），并在 `record_status` 的 reason 里注明；
  - 删掉：从变量、参数解析、`show_usage` 三处一起删。
- **验收**：加断言"每个 CLI 开关都必须有 ≥1 个读取点"（静态扫描 `-\\-xxx` → 变量名 → 计数）。

**✅ 落地（2026-10-16，选"删掉"）**

- 实际位置与原记录有出入：`INCLUDE_LOG_TEXT` 共 **3** 处 —— 声明（L29）、`show_usage()`（L66）、
  参数解析（L1331），不是 2 处（原记录漏了 `show_usage` 那一行）。三处一起删。
- 就地留了一行**墓碑注释**，写明"删的是假开关、要恢复请先加读取点"——
  否则下一次 AI 会话或代码审查会把它当"漏项"重新加回来。
- **行为影响**：再传 `--include-log-text` 走兜底分支 → `未知选项: --include-log-text` + `exit 10`。
  这是**故意响亮**：静默接受一个什么都没干的开关，才是原来的病。
- **守门落地成测试**：`tests/test_collector_cli_flags.py`（尺子自证 + 防误伤配对 + 真文件断言），
  覆盖三库采集脚本。用 `git show HEAD:inspection/pg_inspection_standard.sh`（删之前的版本）校准过：
  尺子在该版本上精确报出 `{'--include-log-text': 'INCLUDE_LOG_TEXT'}`，删完回到 `{}`。
- **没有"接上"日志采集**：`pg.log_summary` 仍只有聚合计数，不存在 `evidence/pg_log_tail.txt`。
  这是**有意的**（PG 日志原文含用户 SQL 字面量，接上要先定脱敏口径），不是漏做。
  ⇒ 所以 PG 的 `summary.txt` **不能**照抄 MySQL 那句"错误日志原文未采集（--include-log-text 未开启）"，
  写它就是指向一个已被删除的开关。

### F-06（P1）`LEGACY_PASSWORD` 是死分支

- L38 `LEGACY_PASSWORD=""`；L1283 `elif [ -n "$LEGACY_PASSWORD" ]`（连 `log_warn "检测到命令行密码"` 都写好了）；但参数解析 L1213-1234 **没有 `--password`** ⇒ 变量永远是空串，这条分支不可达。
- 处置：删掉（保留 `--password-file` + 环境变量 `PGPASSWORD` 两条），或补 `--password`（并在 usage 里标"不推荐，会进 shell history"）。
- **注意**：`show_usage()` 里若已写了"支持 `--password`"，属于"撒谎的注释/帮助"，比不写更坏 —— 一并核。

**✅ 落地（2026-10-16，选"删掉"）**

- 实际位置与原记录有出入：`LEGACY_PASSWORD` 是 **L38**（声明）+ **L1388-1389**（`elif` 分支）；
  参数解析里确实没有 `--password`。
- `show_usage()` 也核过了：只写 `--password-file FILE`，**没有**"支持 `--password`"的假承诺 —— 不必额外改。
- **删掉是纯删除**，理由是**不可达性**（不是"看着多余"）：
  全脚本对 `LEGACY_PASSWORD` 只有 1 个赋值点，就是它自己的初始化 `LEGACY_PASSWORD=""`
  ⇒ 谓词 `[ -n "$LEGACY_PASSWORD" ]` 在**任何**执行路径上都是假 ⇒ 分支不可达。
- 反向验证（`_work/b4_equiv.py`）：把删除反向抵消后得到 `pre_b4` 快照，
  断言 ① 赋值点唯一、② 无 `--password` 解析、③ 相对 pre_b4 只少掉那 3 行（变量 + `elif` + 分支体）。
- 剩下的认证路径：`--password-file` → `PGPASSWORD` / `~/.pgpass` → 交互式隐藏输入，三条都在。

### F-07（P0）退出码被最后一行 `&&` 链绑架

**代码事实**（L1355-1362，脚本的最后几行）
```sh
printf '\n采集完成\n'
printf '任务目录: %s\n' "$TASK_DIR"
printf '实例角色观察: %s（最终拓扑由 Python 判断）\n' "$ROLE_OBSERVED"
printf '状态明细: %s\n' "$COLLECTION_STATUS_FILE"
printf '摘要: %s\n' "$SUMMARY_FILE"
[ -n "${PACKAGE_FILE:-}" ] && [ -f "$PACKAGE_FILE" ] && printf '回传包: %s\n' "$PACKAGE_FILE"
```
- 最后一条命令的状态 = 脚本退出码。
- `PACKAGE_FILE` **只在 `create_package()`（L1179）里赋值**。
- `--no-package` ⇒ L1177 `[ "$CREATE_PACKAGE" -eq 1 ] || return 0`，**不赋值** ⇒ `[ -n "" ]` 为假 ⇒ `&&` 短路 ⇒ **退出码 1**（脚本却已经打印了"采集完成"）。
- 安全扫描拦截分支（L1347-1353）同理不赋值 ⇒ 退出码 1 —— 看起来"蒙对了"，但它与"真失败"**同码**，外部编排（cron/流水线）无法区分"我故意拦住不打包"和"脚本崩了"。

**现有退出码**（散在脚本里，**没有任何一处集中登记**）：`10` 参数/环境错误（L1231/1236/1238/1239/1240/1255）、`11` 空间不足（L415）、`20` 连接失败（L1309）、`130` 信号（L1205）。

**after**
```sh
# 主入口顶部集中声明，并在文件头 L1 的注释块里成表（见 F-11 的 L1）
EXIT_OK=0; EXIT_USAGE=10; EXIT_SPACE=11; EXIT_CONNECT=20
EXIT_BLOCKED=21; EXIT_PACKAGE_FAILED=22; EXIT_SIGNAL=130
...
# 收尾
if security_scan; then
    ...
    if create_package; then exit_code=$EXIT_OK; else exit_code=$EXIT_PACKAGE_FAILED; fi
else
    ...
    exit_code=$EXIT_BLOCKED
fi
printf '退出码: %s\n' "$exit_code"
exit "$exit_code"
```
- **不要**用"把跳过写成 ok"的写法（Oracle 批 21 的教训：`EXIT_POLICY_SKIP` 必须在 `rc -ne 0 → error` 分支**之前**拦截；PG 目前连这个机制都没有，见 INTERFACE §8）。
- **验收**：4 条路径（正常打包 / `--no-package` / 扫描拦截 / 打包失败）各跑一次，断言退出码**互不相同**且与文件头注释表一致。

### F-08（P1）依赖命令缺失 ⇒ 采集项**静默消失** ⇒ 完整度分数虚高

**✅ 已落地（2026-10-16）**：三库同形修复完成 —— PG 21 处 / MySQL 17 处 / Oracle 12 处；尺子（`_work/b1_scan.py`，判据=「分段覆盖」）三库 **0 违规**；负控制变异真文件真实变红 + sha256 逐字节复原；`bash -n` 通过。
**实施中被尺子补出的 5 项**（原 16 项清单未列）：`system.ip_route`（ifconfig-only 机器）、`system.ifconfig`（ip 存在时）、`ha.patroni` / `ha.repmgr` / `ha.manager`（三个 id 混在一条互斥链里，每条只在 1 个分支出现）。
**口径**：命令缺失记 `unsupported`；互斥备选（chrony/ntpq、ip/ifconfig、patroni/repmgr）在**未采用**的那条上记 `skipped`，使两方在所有路径都可见。

**代码事实**：共 **16 个**采集项挂在 `has_cmd` 上，其中 **A 档 8 个 + B 档 5 个**在命令缺失时**连一条 `record_status` 都不会写**；**C 档 3 个**只在"两个候选命令同时缺失"时才消失。

| 档 | 形态 | 行 | item_id | 依赖命令 |
|:--:|---|---:|---|---|
| A | `has_cmd X && capture …`（末尾 `true` 兜底）| L432 | `system.free` | `free` |
| A | 同上 | L433 | `system.filesystems` | `df` |
| A | 同上 | L434 | `system.inodes` | `df` |
| A | 同上 | L435 | `system.block_devices` | `lsblk` |
| A | 同上 | L436 | `system.mounts` | `mount` |
| A | 同上 | L445 | `system.socket_summary` | `ss` |
| A | 同上 | L446 | `system.numa` | `numactl` |
| A | 同上 | L447 | `system.sysctl_selected` | `sysctl` |
| B | `if has_cmd X; then …; fi`（无 `else`）| L424 | `system.timedatectl` | `timedatectl` |
| B | 同上 | L459 | `system.dmesg_errors` | `dmesg` |
| B | 同上 | L462 | `system.postgres_processes` | `ps` |
| B | 同上 | L466 | `system.iostat` | `iostat` |
| B | 同上 | L950 | `system.backup_timers` | `systemctl` |
| C | `if X; then …; elif Y; then …; fi`（无终 `else`）| L425 | `system.chronyc_tracking` | `chronyc` |
| C | 同上 | L425 | `system.chronyc_sources` | `chronyc` |
| C | 同上 | L425 | `system.ntpq_peers` | `ntpq` |

**枚举方式与尺子自证**（工具：`_work/_pg_enum.py`，临时，未入库）：逐字读 + 脚本枚举双轨。枚举器**自己坏过两回**：第 1 版正则要求 `if` 紧跟行首 ⇒ 4 个缩进空格就匹配不上，而 `; fi` 照计一次 ⇒ 深度变 **-1**、永不归零、一路扫到文件尾，把后面所有 `if has_cmd` 全吞掉（B 档报 0）；第 2 版又漏了同行 `… ; fi` 写法，并把 `awk` 体里的 `; if(a[1] !~ …)` 当成 shell 的 `if`（`system.chronyc_*` 之后整体错位）。修法 = 计数前先剥单引号段 + 正确处理同行 `; fi`。

**自证（这条不许省）**：把 L948 `system.backup_cron` 的 `else record_skipped …; fi` 摘成裸 `fi`，枚举器**必须从 16 变 17、且新增项恰为 `system.backup_cron`** —— 实测 PASS。前两版枚举器**都能编译、都能跑出"看起来很合理"的表格**，只有变异自证能把它们照出来。**最终数字以人工读过的源码为准，脚本只用来交叉校验。**

**对照（写对的样板）**：L431 `lscpu`（`|| record_status … "unsupported"`）；L933/936/939 的备份三连（`if / elif / else record_skipped … "unsupported"`）。

**后果（这是本项最要紧的地方）**：`collection_quality` 的分母是 `len(items)`：
```python
score = round(ok / total * 100, 1)      # package_adapter.py L135
```
缺项**不进 `items`** ⇒ `total` 变小 ⇒ **score 变高**。"没采到"变成了"提高采集完整度"，而且 `COMMON.COLLECTION.QUALITY`（`quality_score_min=80`）也因此更难触发。
这正是文档里那条判据的反面教材：**没有能力说出自己没采集，看起来就跟"检查通过"一样。**
⚠ 影响面比原先估的大：16 项里 `free` / `df` / `lsblk` / `mount` / `sysctl` / `ps` / `systemctl` 在所有现场**必然存在**（所以这条缺陷在"正常机器"上从不显形），真正会缺的是 `numactl` / `ss` / `iostat` / `dmesg` / `chronyc` / `ntpq` / `timedatectl` —— **恰好是"精简安装 / 容器化 / 老系统"的典型缺项**，也就是说**越不规范的机器，分数虚高得越多**。

**after**（统一形态）
```sh
if has_cmd ss; then
    capture_command "system.socket_summary" "system.static" "$EVIDENCE_DIR/socket_summary.txt" ss -s
else
    record_skipped "system.socket_summary" "system.static" "unsupported" "ss not installed"
fi
```
写成 `has_cmd X && capture … || record_skipped …` **不行**：`capture_command` 在命令存在但**执行失败**时也会让 `||` 生效，从而**用 `unsupported` 覆盖真实的 `error` / `empty`**（把"跑失败了"说成"没这个命令"）。所以必须用 `if/else` 显式写法，或给 `record_skipped` 加"已有记录不覆盖"的保护。**推荐 `if/else`。**
**不要照搬 Oracle 的 `EXIT_POLICY_SKIP=40`** —— 那套机制是为"许可门禁（Diagnostics Pack 没买）"设计的，PG 没有许可门禁，这里只是"命令不存在"，直接用现成的 `record_skipped` + `unsupported` 即可，引入新码只会增加跨库差异。

**验收**：
1. 静态断言：`has_cmd` 的每个分支都必须有 `else`（配对扫描；**尺子本身也要用变异自证** —— 摘掉任一 `else` 必须变红）。
2. 对 16 条各造"命令不存在"夹具（`PATH` 里放替身脚本 `exit 127`），断言 `collection_status.json` 里**每一项都在**且状态为 `unsupported`。
3. **防误伤配对断言**：把命令**放回来**再跑一次，同一批条目必须全部回到 `ok`，且 `total` 恢复 —— 只测"变红"不测"能变回绿"，会把"永远报 unsupported"的错实现放过去。

### F-09（P1）`item_id` ↔ `category` 错位（破坏性，批 1 只登记）

| item_id | 现 category | 应归 |
|---|---|---|
| `backup.pgbackrest` / `backup.barman` | `pg.backup` | 备份（对）|
| `ha.patroni` / `ha.repmgr` / `ha.manager` | **`pg.backup`** | 高可用（错位）|
| `system.backup_cron` / `system.backup_timers` / `system.backup_processes` | **`pg.backup`** | `system.backup`（前缀与 category 不同域）|
| `pg.capability_probe` | `pg.capabilities` | 保持，但要在 INTERFACE 单列一格（其它 `pg.*` 都在 `pg.basic`/`pg.performance`/`pg.replication`）|

**后果**：分析层一旦按 category 聚合（"备份类采集完整度""高可用采到了吗"），`patroni` 会算进备份；`system.backup_*` 会把**主机 cron** 混进**数据库备份**的结论 —— 而 `plugins/postgresql/metrics.py` L175-184 的 `has_backup_tool` / `has_backup_cron` 恰恰就是在做这个区分。

**处置**：改 `category` 属**破坏性变更**（见 §6），**批 1 内只登记不改**。

### F-10（P1）同名表两份落点，靠 `dict.update` 的覆盖语义**偶然正确**

**代码事实**
- 采集侧：`collect_pg_performance`（L786-813）采**当前库**；`collect_pg_objects`（L853-905）`ALL_DATABASES=1` 时**逐库**再采，同名文件分别落在 `tables/` 与 `databases/<db>/`。
- 分析侧 `plugins/postgresql/package_adapter.py` L46-56：
```python
tables[path.stem] = parse_delimited(path)            # ① tables/ 先落
...
database_tables.setdefault(path.stem, []).extend(parse_delimited(path))   # ② databases/ 聚合
tables.update(database_tables)                       # ③ update = **覆盖**，不是合并
```
**先说结论，避免误判**：`dict.update` 是覆盖 ⇒ 同名时 **`tables/` 那份被丢弃、只留逐库副本**。
- 逐库副本**包含**当前库（`SELECT datname ... WHERE datallowconn` 含连接库）⇒ **没有丢数据，也没有双份计数**（我最初怀疑 `dead_tuples_total` 双计，逐字读完 46-56 行后**撤回**）。
- 留下的真问题两条：
  1. `--current-database-only` 时两份内容相同，**白采一遍**（7 张表 × 全表扫描）；
  2. 一旦有人在 `collect_pg_performance` 里**加了逐库版没有的列**，报告会**静默少那一列** —— 因为覆盖是隐式的，没有任何提示。

**after（只在分析层改，不动采集）**
```python
# 显式写"逐库优先"，并在注释里点明为什么不能依赖 update 的偶然语义
_PER_DATABASE_PREFERRED = {"table_health", "large_tables", "indexes", "no_primary_key",
                           "duplicate_indexes", "unused_indexes", "bloat_estimation",
                           "partition_summary", "extensions", "pg_stat_statements"}
for stem, rows in database_tables.items():
    if stem in _PER_DATABASE_PREFERRED:
        tables[stem] = rows              # 逐库副本整体替换主版
```
**验收**：造"`tables/` 有列 A、`databases/` 没有列 A"的夹具，断言取到的是逐库副本**且**（新增断言）列集合不一致时必须**报错或留痕**，不能静默。

### F-11（P2，但**批 1 最先做**）注释分层到 L1–L5

**现状量化**：总 1362 行 / 61 函数 / 注释 **31 行** = **2.3%**。
- 其中文件头 5 行、`# ==== 分段 ====` 12 行、参数区 2 行、**真正的"为什么"只有 5 行**：L613-614（sar empty 语义）、L258（域名归一）、L288（socket 免密）、L1324（PG 模块串行的原因）、L1336（先扫描后打包）。
- 结论：**难点都注释了，接口一行没写**。那 5 行"为什么"质量很高（比如 L1324 写了"避免对生产库造成过高并发"），但 61 个函数**没有一个**写了参数/产出/不做。

**分层要求**
| 层 | 位置 | 必写 |
|---|---|---|
| L1 | 文件头 | 定位（**只采集不判定**）、N 阶段流程、**退出码表**（F-07）、全局约定（`umask 077` / `LC_ALL=C` / `PG_TIMEOUT_SECONDS`）、依赖命令（`psql` 必需，`sar/sadf/lscpu/ss/numactl/iostat/crontab/systemctl/pgbackrest/barman/patronictl/repmgr` 可选）、隐私声明（`PGOPTIONS` 只读、口令不进包、`security_scan`）、维护者须知（指向 `docs/postgresql-collector-interface.md`）|
| L2 | 阶段与 category 分隔 | 12 处 `# ====` 已有 → 每处补一行"本段产出哪些 item_id、落哪个目录"；**主流程是裸代码、无 `main()` 包裹，必须在 L1210 的 `# ==================== 主入口 ====================` 下补"阶段串/并行"说明** |
| L3 | 模块入口函数头 | 11 个 `collect_*` / `probe_*` / `generate_*`：category、产出 item_id、依赖能力、输出位置、注意、**不做** |
| L4 | 采集引擎函数头 | `pg_exec`/`pg_scalar`/`pg_scalar_db`/`pg_query_tsv`/`pg_query_tsv_db`/`capture_command`/`record_status`/`record_skipped`/`classify_failure`/`known_tsv_header`/`ensure_tsv_header`/`run_module` —— 必须写**参数、stdout 契约、返回码、副作用**（这几个"改一处影响全部 item"）|
| L5 | 其余函数 | 一行：用途 + 输出文件（产 item 的写 `item_id`）|

**"有意为之"必须写明**（不写会被下轮审查反复当缺陷提）：
1. `PGOPTIONS` 强制 `default_transaction_read_only=on`；
2. `ALL_DATABASES=1` 默认逐库采集（代价 = 大库慢，收益 = 不遗漏业务库）；
3. `REDACT_SQL=0` 默认**保留 SQL 明文**，由 `snapshot.privacy.sql_text_included` 声明；
4. `pg.log_summary` 只扫**最后 20000 行**（不是全量），`row_count` 恒 7（7 个类别）；
5. 时间统一 `date -u` 输出 UTC（`compute_sar_coverage` L567-568 也是 UTC）—— 出图时区归一在 Python 侧；
6. 逐库对象采集**每个库单独连一次**（不是一条 SQL 跨库）；
7. `record_status` 是**覆盖写**（同名 item 后写胜）—— 所以同一个 item 不要记两次（当前 `pg.stat_statements` 在 L817/L820 是 if/else，安全；`package.security_scan` 在 L1155/L1158 也是 if/else，安全）。

**验收 —— 指纹法（唯一能证明"零逻辑改动"的方式）**
```bash
cp inspection/pg_inspection_standard.sh /tmp/pg.bak
strip() { grep -v '^[[:space:]]*#' "$1" | grep -v '^[[:space:]]*$'; }
diff <(strip /tmp/pg.bak) <(strip inspection/pg_inspection_standard.sh) && echo "逻辑零改动 ✓" || echo "夹带了逻辑改动 ✗"
```
- 指纹 diff **必须为空**（注释期不允许任何代码行变化，连空行增删都不要 —— 所以 `strip` 要同时过滤空行）。
- 外加：`bash -n` 通过；注释密度 ≥20% 且 ≤30%（**不要超 30%**，会淹没逻辑）；抽查 `pg_query_tsv` 上方有 L4 注释块、`collect_pg_objects` 上方有 L3 注释块。
- **顺序**：F-11 排在所有逻辑改动**之前**。理由：① 零风险；② 补注释的过程就是契约自检 —— F-02 的表头漂移、F-09 的 item/category 错位会**自己浮出来**；③ 能提前标出"会被本轮改动打假"的旧注释（如 F-05/F-06 那两个死旋钮，注释期就该标成待删）。

---

### F-27（P1，批 1）`system.hugepages`：**无条件记 `ok`**，且 PG 侧**无任何消费者**

> 编号排在最后：本节是复核 F-08 时**顺带发现**的，但归属**批 1（采集层）**。

**A. 无条件 `ok`（"查了 0 行"被记成成功）** —— `pg_inspection_standard.sh` L452-458：
```sh
{
    printf 'path\tvalue\n'
    for f in /sys/kernel/mm/transparent_hugepage/enabled \
             /sys/kernel/mm/transparent_hugepage/defrag /proc/sys/vm/nr_hugepages; do
        [ -r "$f" ] && printf '%s\t%s\n' "$f" "$(tr '\n' ' ' < "$f")"
    done
} > "$TABLES_DIR/hugepages.tsv"
record_status "system.hugepages" "system.static" "ok" "$(iso_now)" "$(iso_now)" \
    0 "$(awk 'END{print NR-1}' "$TABLES_DIR/hugepages.tsv")" 0 "tables/hugepages.tsv" ""
```
状态**硬编码 `ok`**。三个 `/sys` 文件**一个都读不到**时（容器里未挂 `/sys/kernel/mm`、内核未开 `CONFIG_TRANSPARENT_HUGEPAGE`、`/proc/sys/vm/nr_hugepages` 被隐藏），文件里只剩表头 ⇒ **0 数据行仍然记 `ok`**，`row_count` 记 0 也**没有任何人拿它当信号**。正确形态是 `empty`（"查了、确认没有"）或 `permission_denied`，**不能是 `ok`**。

> ⚠ 复核时我一度把这一类数成 4 处（`hugepages` / `capability_probe` / `log_summary` / `security_scan`），逐字读完**撤回 3 处**：`capability_probe` 是脚本自己 `printf` 出来的固定键值，不会"查不到"；`log_summary` 的 `7` 是**固定的 7 个类别计数**、且不可读分支已有 `record_skipped permission_denied`；`security_scan` 的 `ok` 正确等于"无命中"。**只剩 `hugepages` 一处是真缺陷。**

**B. 跨库同形 —— 不是 PG 独有**
| 库 | 位置 | 形态 |
|---|---|---|
| MySQL | `mysql_inspection_standard.sh` L605 | `record_status "system.hugepages" … "ok" … awk 'END{print NR-1}'` |
| Oracle | `oracle_inspection.sh` L1507 | 同上 |
| PG | `pg_inspection_standard.sh` L458 | 同上 |
三处是**同一份写法**（连 `awk 'END{print NR-1}'` 的算式都一样）。只修 PG ⇒ 等于在另外两个库里留着同样会说谎的代码，**报告之间也不可比**。按项目纪律"改共享引擎前先问另外三个库会怎样"，这里要**反过来用**：同形缺陷同批一起修。

**C. PG 侧零消费者 —— 采了没人用**
- `plugins/postgresql/` **全目录**搜 `hugepage`：**0 命中**；
- `docs/postgresql-check-catalog.yaml` 的 `system.*` 只登记了 `system.host` / `system.filesystems`，**没有** `system.hugepages`；
- 对照：MySQL 有 `MYSQL.SYSTEM.TRANSPARENT_HUGEPAGE`（`plugins/mysql/rules.py` L789），Oracle 有 `ORA.SYSTEM.HUGEPAGES`。
⇒ 这条采集项的产出**从采集到报告全程无人读**，却**仍计入完整度分母**：采不到不扣分、采到了没人看。**它属于"死采集项"，不是"缺采集项"** —— 加分题与及格线别混着答。

**处置建议**
1. **批 1 内**改状态判定（`ok` → `empty` / `permission_denied`），**三库同一批做**；
2. 死采集项的取舍并入 **D-3** 一起拍板：**要么给 PG 补一条 THP 规则（向 MySQL/Oracle 看齐），要么从采集项登记里摘掉**。**不要维持现状** —— 现在它占着分母却不产出任何结论。

**验收**：造"三个 `/sys` 文件全不可读"夹具（指纹向空目录），断言 `system.hugepages` 状态**不是 `ok`**；**配反断言**：文件可读时必须回到 `ok`（防"永远报 empty"的错实现蒙混过关）。

---

## 6. 批 2：分析层

### F-12（P0）规则引擎的 `available` 维恒真 + `not_evaluated` 一词两义

**代码事实**
- `plugins/postgresql/rules.py` L82-107：
```python
def evaluate(rule_id, applicable, available, triggered, reason, facts, ...):
    if not applicable:
        ... status="not_evaluated" ...   # ← 与下面同词
        return
    if not available:
        ... status="not_evaluated" ...   # ← 与上面同词
        return
```
- **逐条核对**：44 个 `evaluate(...)` 调用点，**第 3 个参数全部写死 `True`**，唯一例外是 L142 把 `verdict["available"]` 传进去（OS 压力那 4 条来自 `inspection_core/system_checks`）。
- `RuleEvaluation`（L42-59）只有 `not_evaluated` / `passed` / `triggered` 三态 ⇒ **缺"采集到没有"这一维**。
- 更隐蔽的一半在**反向**：`evaluate(rule_id, metric is not None, True, ...)` 把"有没有数据"塞进了 **`applicable`** ⇒ "本版本不适用（PG12 没有 `pg_stat_io`）" 与 "这张表没采到" **长成同一句话** `not_evaluated`。

**after（照 Oracle 的三件套）**
```python
# ① inspection_core/models.py 的 RuleEvaluation 增加一维（PG 侧应改为复用它，见 F-13）
@dataclass
class RuleEvaluation:
    ...
    source_status: str = ""      # 采集侧状态：ok/empty/unavailable/permission_denied/...

# ② metrics.derive_metrics 里每个被判据消费的指标都登记来源
source_status: dict[str, str] = metrics.setdefault("source_status", {})
conn_fetch = ctx.table_fetch("connections")
source_status["connection_usage"] = fetch_state(conn_fetch)          # ok / empty / unavailable
source_status["current_connections"] = fetch_state(conn_fetch)

# ③ rules.evaluate 用 state_is_collected 决定 available
evaluate("PG.CONNECTION.USAGE",
         applicable=True,
         available=state_is_collected(metrics["source_status"]["connection_usage"]),
         triggered=...)
```
- `fetch_state` / `state_is_collected` / `TableFetch` 已在 `plugins/oracle/package_adapter.py` L186-249 实现，语义是**通用**的（`empty` 算采集过；缺状态 `""` 一律按**没采到**处理）—— 应下沉到 `inspection_core`（与 `mysql`/`sqlserver` 共用），而不是在 `plugins/postgresql` 里再写一份。
- `PackageContext` 需要 `table_fetch(*names)`：**精确键匹配，禁止子串兜底**（Oracle `package_adapter.py` L274-285 的注释记录了子串兜底把 FRA 使用率兜到 FRA 参数表上的事故）。

**验收（四种钉法，缺一不可）**
1. **防漏判 + 防误伤配对**：`unavailable` ⇒ 必须 `not_evaluated`；`empty` ⇒ **不得** `not_evaluated`（那是"查了没有"，是结论）；`ok` + 触发条件成立 ⇒ `triggered`。
2. **两个相反结论必须长得不一样**：`not_applicable`（版本不支持）与 `unavailable`（没采到）在报告里的文案必须**不同**。
3. **尺子自证**：把 `state_is_collected` 改成恒 `True`，断言上面第 1 条**必须变红**。
4. **负控制**：把某个 `source_status[key]` 去掉，断言该判据从 `passed` 变成 `not_evaluated`（而不是仍然 `passed`）。

**✅ 落地（2026-10-16 下午，批 2C）**

- **新增 `inspection_core/collection_state.py`**（纯 LF，跨库共用）：`state_is_collected` /
  `is_no_data` / `worst_state` / `merge_section_state` / `expand_source_names`。判据词表**只此一份**：
  `DATA_STATES = {ok, empty, partial}`、`NO_DATA_STATES = {unavailable, permission_denied, timeout,
  error, missing, not_enabled, skipped, unsupported, schema_mismatch, unknown, unmapped}`、
  `NOT_APPLICABLE_STATES = {not_applicable}`。
- **`metrics.py`**：新增 `METRIC_SOURCES`（指标键 → 采集表 stem，**34 项**）＋ `collector_table_states()`
  ＋ `source_status_map()`；`derive_metrics` 返回值新增 `"source_status"`。
- **`rules.py`**：新增 `RULE_METRIC_KEY`（规则 id → 指标键，**29 条**）＋ `evaluate_metric()`；
  `RuleEvaluation` 新增 `source_status` 字段（进 `to_dict()`，落到 `report_model.appendix.rule_evaluations`）。
  29 处 `evaluate(..., True, ...)` 改成 `evaluate_metric(...)`；**保留 `evaluate` 原路径的只剩 3 条**
  （`COMMON.COLLECTION.QUALITY` / `COMMON.COLLECTION.ERRORS` / `PG.BACKUP.CONFIGURED`，都不依赖采集表）。
- **漏登记一律响亮失败**：`RULE_METRIC_KEY` 里没有该 rule_id ⇒ `KeyError`；映射到 `derive_metrics`
  不返回的键 ⇒ `KeyError`。改动前这两条都是**静默**的（前者恒 `available=True` 假通过，后者恒
  `not_evaluated` 无声）——实测就抓到过一次：`password_encryption` 这个键**看着像但其实不存在**。

**与原方案的偏离（重要，不要照抄原 after 代码）**

原方案要走 `ctx.table_fetch()` ＋ Oracle 的 `TableFetch` 三件套。实际改用的是**台账桥**：
`collection_status.items[].output_file` 的 basename → `.tsv` stem → 指标键。

- 理由：`output_file` 是**采集器自己声明**的权威映射（报告层不必猜表名），真机 128 项**全部有值**
  （已实测无空值）；而 `table_fetch` **目前只有 Oracle 落地**，PG 要用得先给 `PackageContext` 新造一遍。
- 边界（已实测、已知）：`db.*.X` 与顶层 `pg.X` 落**同一个 stem** ⇒ B6 **按设计跳过**的顶层项会把该
  stem 归并成 `partial`。`partial` 仍算「采集过」（在 `DATA_STATES` 里），所以**不会**把「没采到」
  说成「没问题」；反向（任一库 `error`/`skipped`）按最严重者上报。`partial` 的确切含义保留在规则
  评估的 `source_status` 里，可审。

**对照实验（真包 vs 注入包，`_work/e2e_*`）**

| 场景 | `stale_tables` 的 `source_status` | `PG.VACUUM.STALE` |
|---|---|---|
| 真包（未注入） | `partial`（顶层按设计 skipped） | `passed` |
| 注入（台账改 `skipped` ＋ 表清空） | `skipped` | **`not_evaluated` ＋「证据不足：本判据所需采集项未采集」** |

注入前同样的包判 `passed`（空表算出 0）—— 这正是 F-12 要消掉的**假通过**。
### F-13（P1）PG 自己定义 `Finding`/`RuleEvaluation`，与 core 并存

**代码事实**
| | `plugins/postgresql/rules.py` L15-59 | `inspection_core/models.py` L17-90 |
|---|---|---|
| `Finding` 字段 | rule_id/severity/title/category/summary/facts/recommendation/evidence_refs/finding_id | 上述 + **requires_restart / status / confidence / owner / apply_mode / verification** |
| `Finding.to_dict()` | 写 `"status": "triggered"`（硬编码）| 写 `status` + `evaluation_status` + `triggered`（F-34 注释说明为何条目级条件注入）|
| `RuleEvaluation` | 无 `confidence`、无 `source_status` | 有 `confidence` |

**后果**：PG 的风险登记册**没有整改三要素** ⇒ `report_adapter._build_plan`（L49-62）只能把 `owner` 写 `None`、`planned_window` 写 `None`、`verification` 写死一句"按原检查项重新采集并复核规则状态"。而 Oracle 侧是从规则包逐条声明的（`inspection_core/models.py` L33-39 的注释：`owner` 写**角色**不写人名，跨客户才不会错）。

**after**
```python
# plugins/postgresql/rules.py
from inspection_core.models import Finding, RuleEvaluation     # 删掉本地两个 dataclass
# inspection_rules.json 每条 rule 追加：
"owner": "DBA", "apply_mode": "在线可变更", "verification": "复核 pg_settings 中该参数的 source 与 pending_restart"
```
**影响面**：`rules.py` 44 处构造点、`analyzer.py` L32 import、`presentation.py` L20 import、`tests/test_postgresql_plugin.py`。
**基线核对（必须做）**：`tests/baselines/postgresql/legacy_2_0_1/report_model.json` 的 `risk_register` 会多出 3 个键；`test_adapter_preserves_business_facts` 只数长度、`severity_counts` 只看 `severity` ⇒ 预期不红，但**要真跑一遍确认**，不能靠推断。

### F-14（P0）采集状态从未进入报告：读的键**在 snapshot.json 里不存在**

**代码事实**
- `plugins/postgresql/presentation.py` L54-55：`collection_status = ctx.snapshot.get("collection_status", {})`
- L76-87 `_coll_status(item_id, rows)`：先查 `collection_status[item_id]`，**查不到**再用 `rows` 推断。
- `inspection/pg_inspection_standard.sh` L1056-1089 写出的 `snapshot.json` 键**只有**：
  `schema_version` / `package_version` / `collector` / `instance_identity` / `host_identity` / `time_evidence` / `role_evidence` / `capabilities` / `sampling` / `privacy` / `collection_summary` / `artifacts`
  ⇒ **没有 `collection_status`**。

**三个连带的后果**
1. 那次查找**永远不命中**。
2. `collection.reason` **永远为空字符串** ⇒ 报告里"采集状态：X"后面**永远不会出现失败原因**。
3. `collection.status` 退化成"**用展示行数猜**"，而 PG 的展示行里**多数是代码合成的行**：
   | item | 合成行 | 行数 |
   |---|---|---|
   | `pg.instance` | `inst_rows` | 恒 10 |
   | `system.host` | `os_rows`（来自 snapshot）| 恒 7~8 |
   | `pg.connections` | `conn_rows` | ≥3 |
   | `pg.performance` | `perf_rows` | 恒 5 |
   | `pg.vacuum` | `vac_rows` | 恒 4 |
   | `pg.indexes` | `idx_rows` | 恒 3 |
   | `pg.security` | `sec_rows` | 恒 5 |
   | `pg.replication` | `rep_rows`（**无数据时补一行"无复制配置"**）| ≥1 |
   ⇒ 这些 item 的 `collection.status` **恒 `ok`**，与真实采集状态完全脱钩。

**真正的数据源**：`ctx.status`（= `collection_status.json`，`package_adapter.py` L40-41 已读入 `PackageContext.status`）。Oracle 用的是 `ctx.status.get("items")`（`plugins/oracle/presentation.py` L498-502）。

**after**
```python
# presentation.build_inspection_model 开头
_real_status = {
    str(e.get("item_id")): str(e.get("status") or "")
    for e in (ctx.status.get("items") or []) if e.get("item_id")
}
_real_reason = {
    str(e.get("item_id")): str(e.get("reason") or "")
    for e in (ctx.status.get("items") or []) if e.get("item_id")
}
# _coll_status 改为按 F-15 的别名表查这张真表；查不到时**不是**猜，而是 unavailable
```
并套用 Oracle 的 `_EMPTY_TEXT`（`plugins/oracle/presentation.py` L503-513）与 `display.empty_text` —— **`word_engine.py` 已支持 `display.empty_text`**（L835 / L848-852），渲染层零改动。

**验收**
1. 造三态夹具（①表缺失 ②查了 0 行 ③采到有数据），断言同一 item 在 DOCX 里印出**三句不同的话**。
2. **"表缺失"那一句不得包含"正常"二字**（这条要写成断言，因为它正是 F-22 的本质）。
3. 负控制：把 `_coll_status` 改回 `rows` 推断，断言第 2 条**必须变红**。

**✅ 落地（2026-10-16 下午，批 2C）**

`presentation.py` 新增三个**模块级**函数（可被断言，不再藏在闭包里）：

```python
PG_SECTION_SOURCES: dict[str, tuple[str, ...]]   # 18 个章节 → 采集侧 item_id（见 F-15）
collector_status_index(ctx)                     # ctx.status.items → {item_id: 登记项}
section_collection(ctx, item_id, *, rendered_rows=0)  # 章节采集状态（唯一来源=台账）
```

- `_coll_status(item_id, rows)` 的旧实现（`ctx.snapshot.get("collection_status", {})` 恒空 →
  落回 `ok if rows else empty`）**整段删除**，改为转发 `section_collection`。
- **报告侧 `status` 与台账归并原值刻意分开**：`empty`（查了确实没有）与 `partial`（部分有证据）
  都归成 `ok`，因为引擎的语义是「非 ok 就压过分析结论」（`word_engine._evidence_index_rows`）——
  原样透出 `partial` 会把该节的 `risk` 盖成「部分可用」，等于**用"证据不全"抹掉"发现问题"**。
  缺口明细改走 `reason`（正文元信息行会印）＋ 附录「数据缺口」表，那才是缺口该出现的地方。
- `word_engine.STATUS_DISPLAY` 补 `unmapped`/`unavailable`/`missing` → 「未采集」（**词表仍只一份**）。

**验收**：真机 `report_model.json` 逐章 `collection.status` 已能反映台账；注入包实测
`pg.settings` 印 `skipped`、理由 `未取得：pg.settings（已跳过）`；`pg.vacuum` 印 `ok` ＋
`本节部分采集项未取得，结论基于其余已取得证据：db.mydb.table_health（已跳过）、…`。
### F-15（P0）展示层 item_id 与采集层 item_id 是两套命名空间

`presentation.py` 共登记 **18** 个 item_id。与采集层（约 75 个具体 item_id）对照：

| 展示层 item_id | 状态 | 说明 |
|---|---|---|
| `pg.instance` | ✅ 精确命中 | |
| `pg.connections` | ✅ 精确命中 | |
| `pg.settings` | ✅ 精确命中 | |
| `system.filesystems` | ✅ 精确命中 | |
| `system.mounts` | ✅ 精确命中 | |
| `system.dmesg_errors` | ✅ 精确命中 | |
| `pg.performance` | ⚠ **撞采集层 `category`** | 采集层是 category；item_id 是 `pg.connections`/`pg.database_stats`/`pg.bgwriter`/… |
| `pg.replication` | ⚠ **撞 `category`** | item_id 是 `pg.stat_replication`/`pg.replication_slots`/`pg.archiver`/`pg.wal_receiver` |
| `pg.backup` | ⚠ **撞 `category`** | item_id 是 `backup.pgbackrest`/`backup.barman`/`system.backup_cron`/… |
| `pg.logs` | ⚠ **撞 `category`** | item_id 是 `pg.log_summary` |
| `pg.objects` | ⚠ **撞 `category`** | item_id 是 `pg.object_counts` + 逐库 `db.<db>.*` |
| `system.host` | ❌ 无对应 | → `system.os_release` / `system.lscpu` / `system.free` / `system.ip_address` |
| `system.time` | ❌ 无对应 | → `system.time_status` / `system.timedatectl` / `system.chronyc_tracking` / `system.ntpq_peers` |
| `system.kernel` | ❌ 无对应 | → `system.sysctl_selected` |
| `pg.space` | ❌ 无对应 | → `pg.databases` / `pg.tablespaces` |
| `pg.vacuum` | ❌ 无对应 | → `pg.table_health` / `pg.database_ages` / `pg.vacuum_progress` |
| `pg.indexes` | ❌ 无对应 | → `pg.indexes` / `pg.unused_indexes` / `pg.duplicate_indexes` |
| `pg.security` | ❌ 无对应 | → `pg.hba_rules` / `pg.roles` / `pg.settings` |

**为什么"撞 category 名"比"无对应"更危险**：朴素 join（`if key in _real_status`）在 `_real_status` 只按 item_id 索引时不会命中；但任何人一旦为了"提高命中率"改成**按 category 建索引**或加**子串兜底**，`pg.performance` 就会命中一个**看起来对**的对象（比如 30 个 `pg.performance` 类 item 里的任意一条），误差不可见。

**after（单一来源 + 禁止子串兜底）**
```python
# 展示条目 → 采集侧 item_id（**单一来源**，加成员必须改这里）
DISPLAY_ITEM_ALIASES: dict[str, tuple[str, ...]] = {
    "pg.instance":        ("pg.instance",),
    "system.host":        ("system.os_release", "system.lscpu", "system.free", "system.ip_address"),
    "system.time":        ("system.time_status", "system.timedatectl", "system.chronyc_tracking"),
    "system.kernel":      ("system.sysctl_selected",),
    "system.mounts":      ("system.mounts",),
    "system.dmesg_errors":("system.dmesg_errors",),
    "system.filesystems": ("system.filesystems",),
    "pg.connections":     ("pg.connections",),
    "pg.space":           ("pg.databases", "pg.tablespaces"),
    "pg.performance":     ("pg.database_stats", "pg.bgwriter"),
    "pg.vacuum":          ("pg.table_health", "pg.database_ages", "pg.vacuum_progress"),
    "pg.replication":     ("pg.stat_replication", "pg.replication_slots", "pg.archiver"),
    "pg.indexes":         ("pg.indexes", "pg.unused_indexes", "pg.duplicate_indexes"),
    "pg.security":        ("pg.hba_rules", "pg.roles", "pg.settings"),
    "pg.backup":          ("backup.pgbackrest", "backup.barman",
                           "system.backup_cron", "system.backup_timers", "system.backup_processes"),
    "pg.logs":            ("pg.log_summary",),
    "pg.settings":        ("pg.settings",),
    "pg.objects":         ("pg.object_counts",),
}
```
解析顺序（照 Oracle）：**精确 → 别名 → `ctx.tables` 表键存在性（表在包里 ⇒ 采过）→ 缺状态按"没采到"**。取"最坏的一个"而不是"最好的一个"：多个别名里只要有一个是 `unavailable`，本条就不能印"正常"。
- 断言：**别名表里每个成员都必须真实存在于采集侧 item_id 全集里**（把脚本里的 item_id 抽出来做集合校验）。这条能防"别名写错一个字母、静默退化成没采到"。

**验收**：对 18 条逐一造"别名成员全缺失"的夹具，断言 **18 条全部印"未采集"**（当前是 18 条全部印"正常"）。

### F-16（P1）同一事实两处实现、口径不同

| 事实 | `metrics.py` | `presentation.py` | 报告后果 |
|---|---|---|---|
| `stale_tables`（从未 VACUUM 的表） | L47-55：**带门槛**（`n_live_tup+n_dead_tup≥10000` 或 `total_bytes≥100MB` 或 `n_mod_since_analyze≥10000`）| L416：`sum(1 for r in thealth if not last_autovacuum and not last_vacuum)`，**无门槛** | §VACUUM 与维护 印的 N 与 `PG.VACUUM.STALE` 的 N **不是同一个数** |
| `connection_usage` | L34-35：`cur_conn / max_conn`，**任一缺失返回 `None`** | L309-311：`or 0` / `or 1` ⇒ 采集不到时印 **`0.0%`** | 采集失败时报告写"连接数 0/1，使用率 0.0%。未发现长事务、锁等待或连接压力。"——**编出来的正常** |
| `cache_hit_ratio` | L39-41 | L380-382 | 两处公式相同，暂时一致，但**仍是两份实现** |
| `dead_tuples_total` | L45 | L415 | 同上 |
| `bloat_table_count` | L75-80（≥10% 且 ≥100MB）| L359-361（同门槛）| 一致，但两处硬编码 10% / 100MB |
| `inv_idx` | L208（`r.get("indisvalid")=="f"`）| L492（同）| 一致 |

**后果**：同一份报告里出现**互相矛盾的两个数字**（客户一定会问），且 `connection_usage` 在采集失败时**主动撒谎**。

**after**：展示层**只读 metrics**（把 `metrics` 传进 `build_inspection_model`，或 builder 内部调同一个 `PostgreSQLMetricProvider`），删掉 `presentation.py` 里的重复计算。
- 断言："报告中出现的 `stale_tables` 数字必须等于 `metrics['stale_tables']`"（这条在基线包上就能跑）。
- 注意：`presentation._build_conclusions` 也读 metrics（L699-796，与 `analyzer._build_conclusions` 重复，见 F-20），一并收口。

**落地（2026-10-16，批 2）**：`stale_tables` / `dead_tuples_total` / `max_xid_age` 三处口径收成
`metrics.py` 的**模块级单一来源**（`count_stale_tables` / `sum_dead_tuples` / `database_max_xid_age`），
`presentation.py` 第 9 章改为调用它们 ⇒ 与第 1 章、规则层同源。`connection_usage`（第 4 章）同样
把 `or 0` / `or 1` 去掉：缺数据时印「未采集」，不再编出「连接数 0/1，使用率 0.0%。未发现长事务、
锁等待或连接压力。」

真包实测：修前 `report_model.json` 里第 1 章「0 张/正常」与第 9 章「2 张/关注」并存，修后第 9 章
同步为「0 张」。`connection_usage` 那条对**有数据的包零影响**（只对缺数据生效）。
验证：`_work/p1_verify.py`（三处同源断言）+ `_work/b7_verify.py`（无数据夹具 + 被禁文本断言），
两条都配负控制（注入旧实现 ⇒ 变红 ⇒ sha256 逐字节复原）。

**注**：规则 `PG.VACUUM.STALE` 读的是 `metrics`，与第 1 章同源、**不读**第 9 章的自算值，
所以"规则判 passed"与"第 9 章印 2 张"并不矛盾 —— 矛盾是**同一份报告内部两个数字对不上**（客户可见）。

**✅ 落地（2026-10-16 下午，批 2C）**

别名表就是 `presentation.py` **模块级**的 `PG_SECTION_SOURCES`（**21** 个章节，含通配 `db.*.X`；
批 2D 追加 `system.hugepages` / `pg.io` / `pg.wal`）；
`expand_source_names` 负责把 `db.*.table_health` 展开成台账里**实际存在**的每一条（逐库采集）。

**值域钉在独立来源上（这是本批最容易做假的地方）**

- 候选集**不从被测对象自己取**（那种自引用会让映射表漏一个、候选集跟着漏，「全绿」毫无意义）。
- 值域来自**采集脚本** `inspection/pg_inspection_standard.sh`：字面 `item_id` 由正则抽
  `\b((?:pg|system|db|backup|ha|module|package|timeseries)\.\w+)`，表名由 `\b([a-z_]+)\.tsv\b` 抽。
- **尺子自证**：断言 `pg.instance` 在字面集里、`table_health` 在表名集里 —— 抽取器一旦失效
  （真实踩过：`findall` 返回的捕获组**不含** `.tsv`，又 `[:-4]` 截了一刀 ⇒ `table_health` 变
  `table_he`）必须红，而不是「因为没抽到东西所以全绿」。
- 无法从脚本字面抽到的只剩 `backup.barman` / `backup.pgbackrest`（「探测不到就不登记」的可选能力），
  写成显式白名单 `COLLECTOR_ID_ALLOWLIST`，**加一项必须写理由**。
### F-23（P2）顶层 9 个对象类查询恒为空

**事实**：`PGDATABASE_ARG="${PGDATABASE:-postgres}"` ⇒ 顶层连接默认是 `postgres` 系统库；而
`pg_stat_user_tables` / `pg_stat_user_indexes` / `pg_class`（过滤后）只反映**当前库**的对象。
真包实测：顶层 `tables/object_counts.tsv` = 0 表 0 索引；`table_health` / `indexes` /
`large_tables` / `unused_indexes` / `bloat_estimation` / `no_primary_key` / `partition_summary` /
`duplicate_indexes` / `sequences` **全部 empty**；真实数据在 `databases/mydb/`（orders / users）。

**为什么没出错**：`package_adapter.py` 的 `tables.update(database_tables)` 用**逐库**同名表
**覆盖**顶层表 ⇒ 下游读到的是全集。报告是对的，但**是靠"覆盖语义"偶然对的**
（`ALL_DATABASES=0` 时顶层与逐库是同一个库，`=1` 时逐库是全集）。

**后果**：① 台账恒记 `empty`，语义失真（看着像"实例没有未使用索引"）；② 白跑 9 个必然为空的
查询；③ 依赖 `update` 的覆盖顺序。

**落地（2026-10-16，批 2）**：9 项改成 `record_skipped`（reason 指向 `db.<name>.<x>`）——
项**仍在台账里**（不消失），文件仍是 header（`record_skipped` 会 `ensure_tsv_header`）⇒
`update` 覆盖逻辑与报告层读到的表内容都不变；顺带给 `known_tsv_header` 补了 4 条缺失映射
（`table_health` / `large_tables` / `indexes` / `sequences`），否则这 4 项会落成 **0 字节**
文件而非 header。另：`sequences` 在逐库侧没有对应项，补了 `db.<name>.sequences`（每库 15 → 16 项）。

**验证**：`_work/b6_verify.py`（抽真 `record_skipped` / `ensure_tsv_header` / `known_tsv_header`
跑 9 项 ⇒ 文件均为 header-only + 台账 9 条 `skipped`；负控制摘掉 4 条映射 ⇒ 那 4 个文件变 0 字节）；
`_work/b6_endpoint.py` 构造"9 项 empty→skipped + manifest 同步"的包重出报告 ⇒ 与改前**只差
4 段 source 字符串 + 时间戳**，数据缺口表与完整度**零变化**。

### F-17（P1）空集合 ⇒ 11 条判据恒 `passed`；`settings`+`hba_rules` 缺失 ⇒ 5 条假告警 + 4 条假通过

**A. 空集合被当成"检查通过"**（`metrics.py` 里这些键**恒不为 None**，空表时给 0）：
| 指标 | 定义 | 恒为 0 的来源 | 对应判据 | 结果 |
|---|---|---|---|---|
| `dead_tuples_total` | `sum(n_dead_tup)` | L45 `sum(...)` | `PG.VACUUM.DEAD_TUPLE` | passed |
| `stale_tables` | `sum(1 for ...)` | L47 | `PG.VACUUM.STALE` | passed |
| `unused_index_count` | `len()` | L85 | `PG.INDEX.UNUSED` | passed |
| `duplicate_index_count` | `len()` | L87 | `PG.INDEX.DUPLICATE` | passed |
| `long_transaction_count` | `len()` | L91 | `PG.LONG.TRANSACTION` | passed |
| `lock_wait_count` | `len()` | L95 | `PG.LOCK.WAIT` | passed |
| `max_partitions` | `max(...,default=0)` | L99 | `PG.PARTITION.COUNT` | passed |
| `max_xid_age` | `max(...,default=0)` | L69 | `PG.AGE.XID` | passed |
| `no_pk_count` | `len()` | L205 | `PG.STORAGE.NO_PRIMARY_KEY` | passed |
| `invalid_index_count` | `sum(1 for ...)` | L208 | `PG.INDEX.INVALID` | passed |
| `superuser_count` | `sum(1 for ...)` | L225 | `PG.ROLE.SUPERUSER_COUNT` | passed |

⇒ 底层表**根本没采到**时，11 条判据一起报"通过"。

**B. 默认值兜底造成假告警**（`settings.tsv` 缺失时）：
| 参数 | 取值 | 判据 | 结果 |
|---|---|---|---|
| `ssl` | `settings.get("ssl", "off")`（L210）| `PG.SECURITY.SSL` | **假告警** |
| `idle_in_transaction_session_timeout` | `get(..., "0")`（L211）| `PG.SETTINGS.IDLE_TIMEOUT` | **假告警** |
| `statement_timeout` | `get(..., "0")`（L213）| `PG.SETTINGS.STATEMENT_TIMEOUT` | **假告警** |
| `log_min_duration_statement` | `get(..., "-1")`（L215）| `PG.SETTINGS.SLOW_QUERY_LOG` | **假告警** |
| `logging_collector` | `get(..., "off")`（L217）| `PG.LOG.CONFIG` | **假告警** |
| `autovacuum` | `get(..., "on")`（L201）| `PG.SETTINGS.AUTOVACUUM` | 假通过 |
| `wal_level` | `get(..., "")`（L199）| `PG.SETTINGS.WAL_LEVEL` | 假通过 |
| `archive_mode` | `get(..., "")`（L200）| `PG.SETTINGS.ARCHIVE` | 假通过 |
| `hba_rules` 空 | `auth_methods = set()` ⇒ `has_trust=False` | `PG.SECURITY.AUTH` | 假通过 |

⇒ 一个"`settings.tsv` + `hba_rules.tsv` 采集失败"的环境，会同时得到 **5 条假告警 + 4 条假通过**，而且它们**都不是 `not_evaluated`**。

**after**
- 依赖 F-12 的 `source_status`：`available = state_is_collected(source_status["settings"])`。
- **去掉默认值兜底**：`settings.get("ssl")` 而不是 `settings.get("ssl", "off")`（"取不到就是取不到"，不要替数据库假设一个值）。
- 加**配对断言**：缺表夹具下，上述 9 条**必须全部 `not_evaluated`**，**既不得 `triggered` 也不得 `passed`**。
- 负控制：把 `settings` 的 `source_status` 改回"恒 ok"，断言配对断言**必须变红**。

### F-18（P1）两套状态词表不统一

- **采集侧 10 个**（`generate_collection_status_json` L1017）：`ok` / `empty` / `unsupported` / `not_enabled` / `not_applicable` / `permission_denied` / `timeout` / `error` / `skipped` / `partial`
- **规则侧 3 个**（`plugins/postgresql/rules.py` L42-59）：`not_evaluated` / `passed` / `triggered`
- **`report_adapter.py` L165-170**：把 `not_evaluated` **一律**翻成缺口 `external_evidence_required` + 固定 `recommended_action` ⇒ "本条规则**不适用于此版本**（PG12 无 `pg_stat_io`）" 与 "这张表**没采到**" **又长成同一句话**。

**after**
```python
# 规则侧引入两词，与采集侧对齐
GAP_BY_EVAL_STATUS = {
    "not_applicable":         ("not_applicable",          "当前版本/架构不适用，无需评价"),
    "unavailable":            ("unavailable",             "该项未采集，结论无从谈起；补充证据后重新评价"),
    "insufficient_history":   ("insufficient_history",    "历史窗口不足，不做趋势结论"),
}
```
- 展示层**零改动**：`inspection_core/word_engine.py` 的 `STATUS_DISPLAY`（L234-244）里已经有 `not_applicable→不适用`、`unavailable→未采集`、`insufficient_history→历史不足`、`external_evidence_required→需要外部证据`。
- 断言："缺口表的每个状态词都必须能在 `STATUS_DISPLAY` 里找到中文"（防"漏一个键就原样印英文"——这是批 19 的旧伤）。

### F-19（P1）备份结论的两处补丁盖住根因

**代码事实**
- `report_adapter.py` L179-186：对 `item_id == "pg.backup"` **强行改写** analysis 为 `not_evaluated` + 固定话术（并保留原 analysis 到 `extensions.source_analysis`）。
- `report_adapter.py` L83-98 `_normalized_conclusions`：对"安全与可恢复性"结论做**字符串替换** `"未检测到备份策略"` → `"备份策略证据不足，未作通过结论"`。
- 而 `plugins/postgresql/rules.py` L245 已经写对了：
```python
evaluate("PG.BACKUP.CONFIGURED", backup_assessed is True, True, not has_tool and not has_cron, ...)
```
（`backup_assessment_complete` 为 False 时判据**不评价**——这是"证据不足不判"的**正确模板**，应推广到 F-17 的全部分支。）

**后果**：① 两处补丁是**症状修复**，F-12/F-14 修好后它们是多余的；② `_normalized_conclusions` 用**字符串匹配**改文案 —— `analyzer._build_conclusions`（L788-789）那句话一旦改字（加个空格、换个标点），替换**静默失效**，报告又回到"未检测到备份策略"（等于替客户断言"你没有备份"）。

**after**
- F-12/F-14 落地后，**删除** L179-186 与 `_normalized_conclusions` 整个函数。
- 若暂时保留，至少改成**按 `topic` + `status` 判定**而不是字符串匹配。
- 断言："`report_adapter.py` 里不得出现对业务文案的字面量字符串替换"（静态断言，防回归）。

---

## 7. 批 3：报告层（Word）

### F-20（P2）`presentation.py` ~250 行重复死代码；`_build_multi_conclusions` 从不产出

> **✅ 部分落地（2026-10-16 二轮）**：装配路径的 4 个方法（`build_report_model` / `_build_conclusions` / `health_summary` / `topology`）已收口成**单一来源** —— 保留 `presentation` 那份（与 MySQL 侧同构：MySQL 也是 `analyzer` 委托 `presentation_builder`），`analyzer` 侧改为委托 + 删除 `_build_conclusions` 副本（`analyzer.py` 500 → 311 行）。
> ★ 方向与原计划（删 presentation 那份）相反，理由是**保留参数化、可注入 clock 的那一份**，且与 MySQL 一致。
> ★ 守门：`test_report_model_assembly_is_delegated_not_duplicated` —— **静态**（`def _build_conclusions` 不得再现 + 必须出现委托调用）**+ 运行时**（把 `presentation_builder.build_report_model` 换成哨兵 lambda，调用必须原样返回哨兵）。只有静态那一条挡不住「重新内联」。
> **仍未做**：`build_llm_input`（analyzer / presentation 各一份，且 `analyzer` 实际在 L213 内联重建 —— 三处，全死）；`_build_multi_conclusions`（两份，**全死** ⇒ 多实例报告的拓扑概况/严重风险/整体状态**确实没产出**，`analyzer.py` 只取 `all_instances[0]` 的 6 个域结论）。

**逐字重复清单**（左 = `presentation.py`，右 = `analyzer.py`）：
| 方法 | presentation | analyzer | 谁在用 |
|---|:---:|:---:|---|
| `health_summary` | L29-42 | L82-95 | **analyzer 的**（L304/L317）|
| `build_report_model` | L649-697 | L100-148 | **analyzer 的**（L305）|
| `_build_conclusions` | L699-796 | L150-247 | **analyzer 的**（L135/L147）|
| `topology` | L810-854 | L402-446 | **analyzer 的**（L314）|
| `build_llm_input` | L798-808 | L249-259 | **analyzer 的**（L361 附近）|
| `_build_multi_conclusions` | L856-879 | L448-471 | **两个都没人调** |

`Analyzer` 对 `presentation_builder` 的唯一调用是 `build_inspection_model`（L104 / L653）⇒ `presentation.py` 的那 6 个方法**无调用点**（可用 `grep -n "presentation_builder\."` 证明）。

**顺带一个真缺陷**：`_build_multi_conclusions`（多实例的"拓扑概况/严重风险/整体状态"）**在两个文件里都无人调用** ⇒ 多实例报告永远只出 `all_instances[0]` 的 6 个域结论（`analyzer.py` L354），**拓扑章节其实没产出**。

**after**：删 `presentation.py` 的 6 个重复方法（只留 `build_inspection_model`）；`_build_multi_conclusions` 要么接上（`len(contexts) > 1` 时用），要么删。
- 断言："同一概念在全仓只有一处实现"——用文本/AST 计数断言 `def health_summary` 之类只出现 N 次（防下次又抄一份）。

### F-21（P2）阈值外置不完整

`inspection_rules.json` 的 8 个 `globals` 键**全部被消费**（已逐条核对 `rules.py` 的 7 个 `threshold(...)` 调用 + `quality_score_min` 直读）⇒ **当前无死键**，这点是好的。但 3 条阈值**硬编码在代码里**：

| 判据 | 位置 | 硬编码值 | 建议键 |
|---|---|---|---|
| `PG.FILESYSTEM.DATA_DIR` | `rules.py` L315 | `>= 80` | `data_dir_usage_warning` |
| `PG.VACUUM.DEAD_RATIO` | `rules.py` L173 | `> 10` | `dead_ratio_warning` |
| `PG.ROLE.SUPERUSER_COUNT` | `rules.py` L328 | `> 3` | `superuser_count_warning` |

**after**：三个键进 `globals`，代码改走 `threshold(...)`。
- 断言（**双向**，现在只有单向）："规则包里的每个阈值键都必须被代码请求" **且** "代码请求的每个键都必须在包里"，取键正则要**锚定宿主对象**（PG 是 `threshold("RULE.ID", "key", ...)` 形态，`inspection_core/system_checks.py` 的 `DEFAULT_THRESHOLDS` 是另一套，别混）。

### F-22（P0）`采集状态：正常` 恒印

**代码事实**（`inspection_core/word_engine.py`）
```python
L809:  collection = item.get("collection") or {}
L810:  status = self.display_status(collection.get("status"))
L811:  source_line = f"数据来源：{source or '报告模型'}；采集状态：{status}"
L814:  reason = str(collection.get("reason") or "").strip()
L815:  if reason: source_line += f"；说明：{reason}"
```
- F-14 修好前，PG 的 `collection.status` 对上述 8 个 item 恒 `ok` ⇒ 报告**逐条印"采集状态：正常"**；`reason` 恒空 ⇒ **永远看不到失败原因**。
- L1755-1758（状态汇总表）还进一步 `if status in {"", "ok"}: status = analysis.status` ⇒ **规则项的采集状态列取的是"分析结论"**，于是"没采到（analysis=not_evaluated）"会印成"未判断"——**看起来像"我们没判"，实际是"我们没采"**。

**注意**：`word_engine.py` 是**四库共享**的（MySQL 的文档 hash 测试靠"一字节都不变"兜底）⇒ **不要在这里加 PG 专用逻辑**。正确的修法在**上游**：F-14 + F-15 把真实状态喂进 `collection.status`。

**验收（呈现层必须读渲染产物，不能只看 rules 单测）**
1. 造"某表缺失"的包，出 DOCX，解 XML 数 `采集状态：` 后紧跟的状态词，断言对应 item **不是"正常"**。
2. 对四库**各**出一份 DOCX，断言本次改动**没有**改变 MySQL/Oracle/SQLServer 的产物（hash 或逐段比对）—— 因为改的是 PG 侧上游。
3. 负控制：把某个 `collection.status` 改回 `"ok"`，断言第 1 条**必须变红**。

**✅ 落地（2026-10-16 下午，批 2C）**

按本节自己写的修法办：**不动 `word_engine.py`**（四库共享，MySQL 的文档 hash 测试靠「一字节都不变」），
只在上游把真值喂进 `collection.status` —— 即 F-12 + F-14 + F-15。

两处**必须一起**的补充：

1. `word_engine.STATUS_DISPLAY` 只加了 3 个**缺键**（`unmapped`/`unavailable`/`missing` → 「未采集」）。
   漏键**不抛异常**、测试也不红 —— 正是「状态词表漏键」这类静默缺陷，所以补的是**词表本身**而不是
   某个渲染分支。
2. `report_adapter.collection_gaps` 改为**合并**规则侧（`not_evaluated`）与采集侧（`non_ok_items`），
   见 F-30。

**呈现层验收（只读渲染产物）**：真包 DOCX 的元信息行已能印出
`数据来源：…；采集状态：已跳过；说明：未取得：pg.settings（已跳过）`；对正常包**零影响**
（逐项 `status` 仍是 `ok`、`reason` 仍是空串）。
### F-23（P1）PG profile 没登记 `PROFESSIONAL_ROW_LIMITS`

- `inspection_core/word_engine.py` L87-94 只有 6 个 **`mysql.*`** 键 ⇒ PG 走 `row_limit = None`（L825）。
- 现状**大多已被 presentation 的切片兜住**：`real_fs[:15]`（L264）、`db_rows[:10]`（L348）、`tablespaces[:5]`（L351）、`large_tables[:5]`（L354）、`states[:5]`（L318）、`rep_status_list[:5]`（L449）、`obj[:12]`（L635）、`log_summary[:8]`（L590）、`dmesg_rows[:20]`（L171）。
- **但 `mount_rows`（L176）与 `kernel_rows`（L175）没有任何上限** —— 一台挂载点几百个的主机（容器/K8s 节点）会把 §系统信息 撑成几十页。

**after**：给 PG 加 `PROFESSIONAL_ROW_LIMITS` 键（如 `system.mounts: 30`），**或**在 `presentation._item` 里统一 `rows[:N]` 并补 `note`（说明"已截断，共 N 条"）。
- 注意：截断必须**明示**（`note`），否则客户看到行数变少会问"是不是漏采了"（Oracle 侧同款教训：`mount_excluded` 要写进 note）。

**✅ 落地（2026-10-16，批 2D）—— 改法与本节原方案不同，原因很重要**

原方案说「给 PG 加 `PROFESSIONAL_ROW_LIMITS` 键」。**实测不能这么做**：
`plugins/mysql/presentation.py` 与 `plugins/oracle/presentation.py` 用的是**同一批**
`system.mounts` / `system.kernel` item_id ⇒ 往 `inspection_core/word_engine.py` 的**共享**字典里
加键会**连带改它们的产物**，而 MySQL 的报告是靠「文档 hash 一字节不变」兜底的。

改为 **PG 本地截断 + 明示**（`presentation.py`）：

| 位置 | 上限 |
|---|---|
| `system.mounts` | 20 |
| `system.kernel` | 30 |
| `system.filesystems` | 15 |
| `pg.space`（库 / 表空间 / 大表） | 10 / 5 / 5 |
| `system.host` 会话状态 · `pg.logs` 分类 · `pg.objects` 类型 · `pg.io` 后端分组 | 5 / 8 / 12 / 8 |

截断一律写进 `display.note`：`正文展示前 N 个 X，共 M 个；完整明细保留在分析结果中`。
不写的后果是客户看到行数变少会问「是不是漏采了」（Oracle 侧 `mount_excluded` 的同款教训）。

**验收**：`_limit_note()` 只有 `total > shown` 时才产出文字（真机上挂载 39 条全列、内核 13 条全列，
此时**不出现**这句）；`tests/test_postgresql_plugin.py` 里有 `pg.io` 截断的配对断言。
### F-24（P1）图表"缺失"而不是"skipped"，没有失败信号

**代码事实**（`plugins/postgresql/charts.py`）
```python
L61:  if not sessions and not stats:
L62:      return []                      # ← 这两张图**完全不存在**
```
- `report_adapter.py` L136-139：只对 `status == "skipped"` 的记录保留原样，其它一律 `chart["status"] = "generated"` ⇒ 因为 `[]` 里没有记录，Word 侧看到的是"**模型里没这张图**"。
- `word_engine.picture()` L856-859 有失败提示能力（`note_box("图表未生成", f"{caption}：{reason}")`），但**要求记录存在且带 `status`/`reason`**。
- 另：`L65` 的判据是 `any(row.get("active_sessions") for row in sessions)` —— 用的是**真值判断**，`active_sessions` 为字符串 `"0"` 时是**真**、为整数 0 时是**假** ⇒ 同一份数据换一种类型就换一条分支（`parse_csv` 返回的是 `str`，所以现状恒真；但这是**脆的**）。

**after**
```python
if not sessions and not stats:
    return [
        {"chart_id": "pg_sessions", "status": "skipped",
         "reason": "timeseries/pg_activity.csv 无数据点（实时采样未取得 PG 会话统计）"},
        {"chart_id": "pg_stats", "status": "skipped",
         "reason": "timeseries/pg_stats.csv 无数据点（实时采样未取得事务统计）"},
    ]
```
并把 L65 的真值判断改成显式比较（`safe_float(row.get("active_sessions")) or 0 > 0`）。

**验收**
1. 造"无 timeseries"的包，断言 `report_model.json` 的 `charts` 里 **6 张图全部存在**，其中全部 `status == "skipped"` 且 `reason` 非空。
2. **列 chart_id 清单对账**（判"图数变了"的第一步永远是列清单，不是看报告截图）：改前后各导出一次 chart_id 集合，diff 必须**只是从"缺 2 张"变成"6 张"**，不能多出别的。

### F-25（P2）`default_company` 既有公开发布串 —— **本轮不改**

- `plugins/postgresql/word_report.py` L53：`default_company="苏州工业园区和信计算机系统工程有限公司"`。
- 该串在 **public 仓库已推送的既成事实**里存在（`docs/PROJECT_MAP.md` 有登记），要清须改写全量历史 + force-push。
- **本轮不动、不引入新的**。写在这里的目的只有一个：下次审查（或下一个 AI 会话）别把它当"新发现的缺陷"反复提。

### F-26（P2）`history/coverage.json` 无读者

见 F-03 下半条：`compute_sar_coverage` 写两份（`coverage.tsv` 给脚本自己读、`coverage.json` 给……没人），而 `generate_manifest` 会把它收进清单。
- 处置：删，或在 INTERFACE 的"输出目录树"里注明"供外部/历史版本消费"。

### F-28（P1）4.4 挂载参数**完全不过滤**，39 行里 37 行是伪文件系统 —— ✅ 已落地（2026-10-16 二轮）

**代码事实**（`plugins/postgresql/presentation.py`）
- `_mount_rows()` 逐行正则匹配后**原样收录**，没有任何过滤。
- 真机 `evidence/mounts.txt` 39 行里只有 `/`（xfs）与 `/boot`（xfs）是块设备，其余 37 行是
  `sysfs`/`proc`/`cgroup`×13/`tmpfs`×5/`devtmpfs`/`devpts`/`hugetlbfs`/`mqueue`/`bpf`/`tracefs`/
  `configfs`/`debugfs`/`fusectl`/`rpc_pipefs`/`autofs`/`pstore`/`fuse.*`/`iso9660`。
- **同章节的 5.1 文件系统容量却在过滤** —— 用的是本地黑名单 `_SKIP_FS_TYPES = {"iso9660","devtmpfs","tmpfs","squashfs","overlay"}`
  （漏 `sysfs`/`proc`/`cgroup`/`hugetlbfs`/`fuse`）：同一份报告里两套过滤口径，且都不完整。
- 客户视角：4.4 表 39 行、5.1 表 2 行，第一反应是"这报告没做过滤"。

**after（与 MySQL 3.5 / Oracle §3 同源）**
1. `_mount_rows()` 返回 `(rows, excluded)`，判据改成公共层 `inspection_core.system_checks.is_persistent_fstype`
   （MySQL `presentation._mount_rows` 与 Oracle 都用它；本地黑名单正式作废）。
2. `system.mounts` 的 `display.note` 写 **`已排除 37 个 proc/tmpfs/cgroup/光驱等非持久化挂载点`**
   —— 「过滤」与「缺失」靠这一句区分（客户看到行数从 39 掉到 2 会问"是不是漏采了"）。
3. `_SKIP_FS_TYPES` 一并换成同一判据，note 改成动态计数（`已排除 6 个 tmpfs/devtmpfs/光驱等非持久化文件系统`）。

**验收（`tests/test_postgresql_plugin.py`）**
- 夹具走**真适配器 + 真展示层**（不抄被测逻辑）；断言 4.4 只剩 `/`、`/boot`，`类型` 集合 `== {"xfs"}`（防误伤：
  真块设备不许被一起藏掉），note 含 `已排除 6 个` + `非持久化`（防漏判）。
- 5.1 断言 `len(rows) == 2` 且 note 含 `已排除 2 个`。
- 负控制：把判据短路成 `if False:` ⇒ 用例必须变红（已实测变红，随后逐字节复原）。

---

### F-29（P1）封面/概述/正文的数据库版本串是 `version()` 全串 —— ✅ 已落地（2026-10-16 二轮）

**代码事实**
- `snapshot.instance_identity.version` = `PostgreSQL 17.7 on x86_64-pc-linux-gnu, compiled by gcc (GCC) 8.5.0 20210514
  (Red Hat 8.5.0-10), 64-bit`（66 字符），被原样塞进**封面「数据库版本」格子**与 2.1 概述格子（撑破表格）。
- 3.1 明细同样全串；3.1 的结论还做了 `[:40]` 硬截断 ⇒ 印出
  `实例版本 PostgreSQL 17.7 on x86_64-pc-linux-gnu,  已识别`（**尾部带一个孤零零的逗号**）。
- 对照 MySQL：封面印 `MySQL 8.0.30`（`word_report.database_version_prefix="MySQL "` + 服务端给的就是短串）。

**after**
- 新增模块级 `short_pg_version(banner)` / `pg_build_note(banner)`（单一来源）；
  封面、2.1 概述、3.1 明细、3.1 结论**四处共用**同一个返回值。
- 封面/2.1/3.1 均印 `PostgreSQL 17.7`；构建信息不丢，落进 3.1 的「说明」列：
  `x86_64-pc-linux-gnu / gcc 8.5.0 / 64-bit`。
- `identity` 额外带出 `version_full` 原串（追溯用，不参与渲染），`[:40]` 截断删除。
- fail-safe：横幅认不出来就**原样返回**（不截断、不改名）。

**★ 这条同时暴露了 F-20 的真实杀伤力**：第一次只改了 `presentation` 那份，**封面照旧是全串**
（3.1 已变短）—— 因为实际在跑的是 `analyzer.Analyzer.build_report_model`。见 F-20 的状态补充。

**验收**
- `test_database_version_is_shortened_and_build_detail_is_kept`（含 fail-safe 两例）。
- `test_cover_and_overview_get_the_same_short_version`：装配 → 适配**端到端**，断言 `cover` / `overview` /
  `identity.version` 三处一致。
- 负控制：`short_pg_version` 短路成 `return raw` ⇒ 用例变红（已实测，随后逐字节复原）。

---

### F-30（P2）附录「数据缺口」表只列规则侧缺口，采集侧的 19 项缺口**不出现**

**代码事实**
- `plugins/postgresql/package_adapter.py::collection_quality` 只回 `{score, total_items, ok, errors,
  warnings, unsupported, not_enabled, integrity}` —— **没有 `non_ok_items`**。
- `report_adapter.collection_gaps` 于是只能从 `rule_evaluations` 里挑 `not_evaluated` 拼缺口。
- 后果：真机 113 项里 **10 skipped / 6 unsupported / 2 not_enabled / 1 partial** 在附录缺口表里
  **一条都不出现**；报告只说「完整度 83.2%」，不说少的是哪 19 项。

**after**：`collection_quality` 增 `non_ok_items`（与完整度分子**同判据**：`not ok_count`），
`collection_gaps` 合并两路并**去重**（规则侧 `external_evidence_required` 优先）。
另补两条 `GAP_REASON_NOTES`（`collector skipped` / `collector unsupported`）与一条
`GAP_ACTION_NOTES`（`按设计跳过`），否则缺口表的「原因/建议」两列是空的。

**验收（配对断言）**：`quality["total_items"] - quality["ok"] == len(quality["non_ok_items"])` ——
完整度说「没到位」的项数必须**等于**缺口表列出的项数，不许一个说齐、一个列缺。
### F-31（P2）11 张表采了却**完全没进报告**：第 8 章因此零 I/O 指标（批 2D 已落地）

**代码事实**（真机 1016 包逐表核对，`presentation.py` 从不引用这些键）

| 表 | 行数 | 后果 |
|---|---|---|
| `stat_io` | 35 | 第 8 章「性能」只有一条 Buffer 命中率，**零 I/O 指标** |
| `iostat_snapshot` | 23 | 同上；设备级 `%util` / `await` 从未出现 |
| `stat_wal` / `checkpointer` / `control_checkpoint` | 1 / 1 / 1 | 无检查点与 WAL 统计（**请求检查点占比**是判断 `max_wal_size` 的第一手证据）|
| `hugepages` | 3 | **无大页检查** —— THP=always 是 PG 性能配置的必查项 |
| `inodes` | 9 | 无 inode 维度（「空间没满、inode 先满」是常见故障）|
| `database_conflicts` | 4 | 无恢复冲突（「备库查不动」最常见的解释）|
| `block_devices` | 6 | 无磁盘介质行（HDD/SSD 直接决定 I/O 延迟结论怎么读）|

**落地**

- 新增 **4.6 透明大页(THP)**：三态写法取**方括号**内的当前值（`_bracket_value`；`always` ⇒ attention）。
- 新增 **第 17 章「I/O 与 WAL」**：
  · `pg.io` = `stat_io` 汇总 + 读写量最高的 8 个 `(backend, object, context)` + `iostat` 设备峰值；
  · `pg.wal` = `stat_wal` + `checkpointer` + `control_checkpoint`，含**检查点强制占比**。
- `system.host` 增「磁盘介质」行（公共层 `disk_media`，与 MySQL 同源；读不出来就不加这一行）。
- `system.filesystems` 的 note 增 inode 最高使用率（伪文件系统按 `is_persistent_fstype` 过滤）。
- `pg.replication` 增「恢复冲突」行 + 逐库明细（明细进结论文字，主表只留合计）。
- `iostat` 设备表按**物理盘口径**排除 `dm-*` 与光驱（见 `os-collector-crossdb-alignment.md` §2.2）；
  多组采样取**峰值**（均值会把「某一秒打满」抹平，而那正是要看的）。
- `pg.objects` 的 note 写明「计数来自顶层连接所在库（默认库，通常为 postgres），不含业务库」。

**★ 顺带修掉一个真机缺陷：稀疏表**

`pg.io` 初版把 **3 列**摘要行与 **7 列**明细行放进同一个 item，而引擎 `data_table` 按所有行的键取
**并集**出列 ⇒ 渲染成 **10 列稀疏表**，前 5 行后面 6 列全是「未采集」，读起来像「这 6 列没采到」。
守门写成**结构性断言**：`test_every_item_rows_share_one_column_set`（同一 item 的所有行键集必须一致）。
这类缺陷**不报错、不抛异常、测试也不红**，只有渲染产物里看得见。

**验收**：真机报告新增 4.6 / 17.1 / 17.2 三处 + 4.1 磁盘介质行；逐章核对渲染产物；
`_work/negctl_d.py` **8 组负控制全部真实变红**（含「取最后一次采样而非峰值」与「明细行混入第 4 列」）。

**未做（本批边界）**：`pg.object_counts` 仍连默认库 ⇒ 第 16 章只反映该库计数（已用 note 明示，
逐库汇总留待下一批）；`extensions`（2 个库各 1 行 `plpgsql`）无检查价值，刻意不展示；
`capabilities` 是能力探测台账（17 行），属调试信息，不进客户报告。

### F-35（P1）元信息行「采集状态」与「缺口说明」**同处一行自相矛盾** —— ✅ 已落地（批 3A）

**真机症状**（本批审计发现，2 处）：

```
数据来源：snapshot.json#time_evidence；采集状态：正常；说明：本节部分采集项未取得，结论基于其余已取得证据：
  system.chronyc_tracking（未启用）、system.chronyc_sources（未启用）、system.ntpq_peers（不支持）
数据来源：evidence/backup_*；采集状态：正常；说明：本节部分采集项未取得，结论基于其余已取得证据：
  backup.barman（不支持）、backup.pgbackrest（不支持）
```

**备份那节最要命**：`barman` 与 `pgbackrest` **两个都不支持**，等于整块证据为空，元信息行却印「正常」。
结论层其实是对的（"备份策略证据不足，未作通过结论"），但**元信息行在说谎** —— 只看前半句的人会以为证据齐了。

**根因**：`presentation.section_collection` 的 `else` 分支是

```python
reason = "本节部分采集项未取得，结论基于其余已取得证据：" + gap_detail if gap_detail else ""
status = "ok"          # ← 无条件赋值：只要不是"全没采到"就写 ok
```

`status` 走的是**引擎语义**（"非 ok 就压过分析结论"，见 `word_engine._evidence_index_rows`），
所以把 `partial` 归成 `ok` 是**对的**（否则该节的 `risk` 会被盖成「部分可用」＝ 用"证据不全"抹掉"发现问题"）。
问题在**元信息行直接印这个引擎用值**。

这是「没采到」变「没问题」的**第三条路**：不靠编造数字，靠**状态词归并把缺口洗掉**。

**改法**：`section_collection` 额外返回 `gap_count`（归并原值），
`word_engine` 用 `gap_status_override()`（新，模块级、可单测）把展示词改写成
`GAP_STATUS_TEMPLATE`（`部分（N 项未取得）`）。**引擎用值 `status` 保持不变。**

★ 刻意 **opt-in**：没有 `gap_count` 键的插件（MySQL / Oracle / SQL Server）永远走不到这条分支，
产物逐字节不变（MySQL 的文档 hash 守门照旧绿）。
★ 说明列同时**去掉重复**：状态词已经说了「部分（N 项未取得）」，说明只留
「结论仍基于其余已取得证据；缺口：…」。

**验收**：真机报告元信息行自相矛盾 **2 处 → 0 处**；负控制 `G1-a` / `G1-b` / `G1-c` 三组真实变红。

### F-36（P1）采集状态桥被**废弃采集点**污染：10 个指标的状态被拉成 `partial` —— ✅ 已落地（批 3A）

**实际行为**：`metrics.collector_table_states` 按 `output_file` 的 **basename** 分桶，
于是 `tables/table_health.tsv`（顶层遗留采集点，台账记 `skipped`，原因写明
"改由逐库采集…顶层连接默认 postgres，无用户对象"）与 `databases/mydb/table_health.tsv`（`ok`）
**落进同一个桶**，归并成 `partial`。

真机实测被污染的 **10 个指标**：`stale_tables` / `dead_tuples_total` / `max_dead_ratio` /
`bloat_table_count` / `max_dead_bytes` / `unused_index_count` / `duplicate_index_count` /
`invalid_index_count` / `no_pk_count` / `max_partitions`。

**现在没出错纯属侥幸**（`partial` 恰好也在 `DATA_STATES` 里 ⇒ 规则照常评估）。
**这是埋在桥里的雷**：哪天有人把 `partial` 移出 `DATA_STATES`（觉得"证据不全不该下通过结论"），
这 10 个指标会**一起变「证据不足」**，报告全线假阴性。

**改法**：新增 `_SOURCE_LAYER_RANK`（`databases/` = 0，`tables/` = 1，其它 = 2），
按**目录层级**取优先级最高的那一层参与归并 —— 越具体的路径越可信。
某一 stem 只存在于低优先级层时，就只用那一层（"只有顶层、顶层又跳过了"仍然是没采到）。

**验收**：真机 10 个指标 `partial → ok`；负控制 `G2-a`（改回不分层）与
`G2-b`（**过度修正**：任一层有 ok 就整表算 ok）两组都真实变红。

### F-37（P1）完整度 83.2% 的**分母口径失真**：16/113 是"设计上就不适用" —— ✅ 已落地（批 3A）

**实际行为**：`collection_quality` 的分母是"台账里登记过的**全部**项"，于是把两类东西算成了缺口：

| 类别 | 项数 | 真实语义 |
|---|---:|---|
| 顶层已改由逐库采集（`skipped`） | 10 | **数据采到了**，只是落在 `databases/<库>/` |
| 环境根本没有（`unsupported`） | 6 | barman / pgbackrest / patroni / repmgr / ntpq / pg_stat_statements **未安装** |
| 时钟守护进程未运行（`not_enabled`） | 2 | 真缺口 |
| sar 历史覆盖不足（`partial`） | 1 | 真缺口 |

⇒ **真正"该采未采"的只有 3 项**，报告却印 **83.2%**。客户一定会问"剩下 17% 呢"。
照原口径，**目标机装的东西越少、完整度越低** —— 而真实结论恰恰相反。

**改法**：`inspection_core/collection_state.py` 新增 `OUT_OF_SCOPE_STATES`
（`not_applicable` / `skipped` / `unsupported`）+ `is_out_of_scope()`；分母剔除它们。
`collection_quality` 同时带出 `in_scope_items` / `out_of_scope`，让"这个百分比是拿几项算的"**口径可见**。

★ **剔除的是统计口径，不是透明度**：这些项**逐条保留在 `non_ok_items`**（附录「数据缺口」表照印），
并带 `out_of_scope` 标记；`report_adapter` 的「推荐动作」列改为**按状态给**
（对 `unsupported` 说"重采"是错的语气 —— 它把**环境事实**说成了**待修复的失败**）。

**验收**：真机完整度 **83.2% → 96.9%**（94/97）。

### F-38（P1）**「采到了真问题却没报」**：`missing_fk_indexes` 全链路无人消费 —— ✅ 已落地（批 3A）

**真机事实**：`db.mydb.missing_fk_indexes` 状态 `ok`、**1 行**：

```
mydb | public | orders | orders_user_id_fkey | FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
```

外键 `orders.user_id` **没有支撑索引**。而改动前整份 DOCX **连"外键"二字都没有**（grep 0 命中）。

**为什么是硬问题**：PG **不会**替外键自动建索引（官方文档 `ddl-constraints.html` 的
"Foreign Keys" 一节说明引用侧检查需要 "such a scan"）。父表 `users` 删/改行时，
需对 `orders` **全表扫描并加锁** —— 高并发下直接表现为锁等待与写放大。

这是本轮审计里**唯一一条"采到真问题却没报"**。

**改法（五处接线，少一处就静默失效）**：

| 位置 | 改动 |
|---|---|
| `plugins/postgresql/inspection_rules.json` | 新增 `PG.INDEX.FK_NO_INDEX`（category `index`，severity `warning`） |
| `plugins/postgresql/rules.py` | `RULE_METRIC_KEY` 登记 + `evaluate_metric("PG.INDEX.FK_NO_INDEX", …)` |
| `plugins/postgresql/metrics.py` | `METRIC_SOURCES["missing_fk_index_count"]` + `derive_metrics` 里数行 |
| `plugins/postgresql/presentation.py` | `PG_SECTION_SOURCES["pg.indexes"]` 加 `db.*.missing_fk_indexes`；11.1 加「外键缺索引」行 |
| 11.1 结论/说明/建议 | 点名到具体对象（`orders` / `orders_user_id_fkey`），不只给个数 |

★ **顺带修掉一处"模板话术压过事实"**：真机 `未使用 0 / 重复 0 / 无效 0`，只有外键缺索引 1 个，
结论却写"建议清理冗余索引以释放空间提升写入"。改为**按实际问题**给建议
（无冗余索引就不提清理；有外键缺索引才提补建）。
这与"没采到印正常"是**同一类毛病、方向相反**：那边是掩盖，这边是**无中生有**。

★ 顺便删掉一处**死变量** `idx_items`（赋值后全函数无读取）。

**验收**：规则评估 **36 → 37 条**，真机触发该规则（`triggered`）并进风险登记册；
11.1 渲染出「外键缺索引 1」行并点名对象；负控制 `P1-a` / `P1-b` / `P1-c` / `P1-d` 四组真实变红。

**## 附：批 3A 的两把"额外的尺子"（写进守门，不只写在这里）**

1. **打分一律正着数**。`package_adapter.collection_quality` 原先的 `ok` 是**判否式**
   （"不在 error/warning 集合里就算通过"）—— 新状态词忘了登记时**不抛异常、不报错、分数反而变高**。
   实测过的具体漏洞：**缺状态（空串）不在任何失败集合里 ⇒ 被算成「到位」**。
   现改为 `SCORE_OK_STATES = DATA_STATES - {partial}` 的**正着数**（`scores_as_ok()`）。
   正着数的最坏后果是"新状态被算成缺口"（分数偏低、缺口表多一行），**方向是安全的**。
2. **「不适用」与「没采到」是两个问题，必须两个名字**：`is_no_data()` 答"能不能下分析结论"，
   `is_out_of_scope()` 答"该不该进完整度分母"。缺状态（空串）**只算没采到，不算不适用**
   —— 否则分母会被"没登记"悄悄掏空。


### F-39（P0）首页「结论速览」**整段静默消失**：`health_assessment` 缺 `scoring_policy` —— ✅ 已落地（批 4）

- **实际行为**：共享引擎 `word_engine.build_summary_v31` 的判据是 `if score is not None and base:`，
  而 `base` 只能来自 `health_assessment.scoring_policy`。MySQL 的适配器一直在写这一键
  （`{base:100, penalty_per_finding:{high:15,medium:7,low:2}, minimum:0}`），**PG 从来没写**
  ⇒ PG 报告第一页**没有「结论速览」**，MySQL 有。它**不报错、不抛异常、测试也不红**。
- **不改的后果**：首页只剩一张四个数字的表，读者看不到「分数从哪来」；两份报告放一起对比时
  PG 明显少一块（本项由用户拿 MySQL 报告对照时直接发现）。
- **改法**：`report_adapter` 补 `scoring_policy`，值取 `presentation` 的模块级常量
  `SCORE_BASE` / `SCORE_PENALTY`（25/10/3）—— **口径只有一份**，适配器声明的与算分函数
  用的是同一份常量，不许各写一遍数字。
- **影响面**：PG 首页新增一段；MySQL/Oracle/SQLServer 各有自己的适配器，不受影响。

### F-40（P0）「说明」列**整列空白**：占该列行数的 72% —— ✅ 已落地（批 4）

- **实际行为**：`presentation.build_inspection_model` 里大量行写作
  `_row(检查项=..., 采集值=..., 说明="")` —— **字面量空串写死**。真机有「说明」列的 116 行里
  **83 行空白**。
- **不改的后果**：读者看到一排"只有前两列有值、第三列全空"的表。用户原话：
  「全文有大量说明都是空的，分析层面没填充和建议吗?」MySQL 的等价表第三列是有内容的。
- **改法**：为每张表补一份**模块级口径表**（`SETTING_NOTES` / `OBJECT_NOTES` /
  `LOG_CATEGORY_NOTES` / `SESSION_STATE_NOTES` …），按行标签取值；行内拼接出来的标签
  （`大表: schema.table`、`  状态: xxx`）用带前缀的映射命中。映射必须**覆盖该表出现的
  每一个标签**，漏一个就仍有空白 —— 这条写成了守门。
- **影响面**：只改 PG 展示层，Word 引擎不动。守门 `test_every_remark_cell_is_filled`
  与 `test_remark_maps_cover_every_declared_label`（配对：漏键必红、键在但值为空串也必红）。

### F-41（P1）结论速览把「本环境不适用」也算成「证据缺口」：同一页两个口径 —— ✅ 已落地（批 5）

- **实际行为**：`build_summary_v31` 直接拿 `len(collection_gaps)` 说「另有 N 项证据缺口」。
  而台账里既有**真缺口**，也有**本环境不适用**（未装对应组件、或该采集项已由逐库路径覆盖）。
  真机「20 项采集限制」= **4 项真缺口 + 16 项不适用** ⇒ 同一页同时出现
  「20 项证据缺口」与「数据质量 96.9%」，读者无法判断到底缺了多少。
- **不改的后果**：F-37 修正的是完整度的**分母**，**首页那句话**没跟上 —— 等于两个口径各说
  各话，正是本项目反复出现的"同一指标两处实现 = 两个数字"。
- **改法**：判据走公共层 `is_out_of_scope()`（与完整度分母**同源**），句子里把
  总数 / 真缺口 / 不适用 三个数字分开说。
  ★ **只有确实存在不适用项时才改句**，否则原句逐字保留 —— MySQL 的台账只有 1 条
  `external_evidence_required`（落进"无不适用"分支）⇒ **文档 hash 不变**。
- **影响面**：动的是共享引擎，但三个库在各自输入下**产物逐字节不变** —— 由 MySQL 的
  `test_generated_document_matches_pre_split_content_and_has_image_alt_text` 的
  document-xml hash 门兜住（已复跑通过）。

### F-42（P1）「1.2 高风险项速览」**整节消失**：读者分不清"没有高风险"与"这节没渲染" —— ✅ 已落地（批 5）

- **实际行为**：该节判据是 `severity in {critical, high}`。PG 的 severity 体系里
  `critical → high`，而本机 **8 条 critical 规则全部 `passed`**（唯一 `not_evaluated` 的是
  `PG.BACKUP.CONFIGURED`）⇒ 触发的 3 条全是 `warning`（→ medium）⇒ 速览**整节不渲染**。
- **不改的后果**：这是「没采到 vs 没问题」的**第三种形态** —— **没东西可报** 与
  **没渲染出来**在成品里长得一模一样。用户对照 MySQL（1.1 / 1.2 速览 / 1.3 管理关注）
  时会以为 PG 少了一节。
- **改法**：引擎新增一条 **opt-in** 分支 —— `model["declare_no_highlights"]` 为真**且**
  **风险登记册非空**时，渲染「1.2 高风险项速览」并说明「未发现严重或高风险项，登记册共
  N 条（中 N / 低 N）」。PG 适配器置该键。
  ★ 登记册为空**不许**声明 —— 那是把"没评价"说成"没问题"，本项目最忌讳的错。
  ★ 无此键的库（MySQL/Oracle/SQLServer）走原路径，产物逐字节不变。
- **影响面**：PG 首页变 1.1 / 1.2 / 1.3 三段，与 MySQL 结构对齐；其余三库不变。
- **守门**：`SummaryGapSplitAndEmptyHighlightTests`（6 条）+ PG 适配器侧
  `test_adapter_asks_the_engine_to_declare_an_empty_high_risk_section`。
  负控制 **5/5 真实变红**，含 3 组"过度修正"配对（无不适用也硬拆 / 登记册为空也声明 /
  真高风险项被那句说明顶掉）。

### F-43（P1）网络配置采了没报：`system.ip_address` / `ip_route` / `socket_summary` 三项**零消费** —— ✅ 已落地（批 3B-B1）

- **实际行为**：采集台账里三项全是 `ok`（`system.ip_address` 17 行、`system.ip_route` 16 行、
  `system.socket_summary` 10 行），落盘在 `evidence/ip_address.txt` / `ip_route.txt` /
  `socket_summary.txt`；而 `presentation.py` 里 `ip_address` / `ip_route` / `socket_summary`
  这三个词**一次都没出现过**。报告因此说不出"主机有几张网卡、默认网关是谁、监听在什么地址"。
- **不改的后果**：与批 3A 的 F-38（`missing_fk_indexes` 全链路无人消费）是**同一类缺陷**
  —— 「采到了却没报」。它不报错、不抛异常、既有测试全绿，只能靠结构性断言钉住。
- **改法**：新增展示条目 `system.ip_address`（标题「网络配置」），**不入新章** ——
  MySQL 标杆同样没有独立网络章（主机信息在 3.1，网络只在 SAR 实时指标里），
  凭空多一章会让四库章节结构不可比。解析器 `parse_ip_addr` / `parse_ip_route` /
  `parse_socket_summary` 放在 `plugins/postgresql/parsers.py`，入参是
  `ctx.raw_lines(...)` 的**行列表**（不让调用方拼 `root/tables`，避免采集器移目录后静默读空）。
  ★ `ip_address` 与 `ifconfig` **两种形态都认**（采集器按 `has_cmd` 二选一，
    `system.ifconfig` 是互斥备选）—— 只认一种，换台老机器就静默变空。
  ★ 同网段多 IP **按 cidr 合并**（本机 `192.168.100.0/24` 同时挂 `.80` 与 `.130`）：
    逐行铺开会让人以为配了两条路由。
- **影响面**：PG 4.7 新增一节（本机 7 行）；其余三库不变。
- **守门**：`test_network_rows_carry_interface_route_and_socket`、
  `test_network_section_degrades_to_not_evaluated_without_evidence`（无证据时必须是
  `not_evaluated` + 「未采集」，不许印「0 张网卡」）。
  ★ 负控制 **M5** 抓出一条**假设门**：原先断言的「`table local` 不能进直连网段」
    在 iproute2 现有格式下**恒真**（这些行首列是 `local`/`broadcast` 关键字，
    本来就命中不了 `"/" in parts[0]`）⇒ 去掉"按 cidr 合并"它照样绿。
    改断言「直连网段恰好 1 条」后，M5 真实变红。解析器里的 `table local` 排除降级为
    **防御性判断**并在 docstring 里写明"别拿它当守门断言"。

### F-44（P1）实例进程采了没报，**而且采脏了**：16 行里 8 行是采集脚本自己 —— ✅ 已落地（批 3B-B1）

- **实际行为**：`system.postgres_processes` 台账 `ok / row_count=16`，文件
  `evidence/postgres_processes.txt` 16 行；但**其中 8 行是采集脚本自己的进程树** ——
  `su - postgres`、`-bash`、3 个 `bash pg_inspection_standard.sh`、`sleep 5`、
  `ps -eo ...`、`awk ...`。根因是采集侧判据 `awk '/[p]ostgres/'` 对**整行**匹配，
  而 `su - postgres` 之后整条 shell 树的 **USER 列都是 `postgres`**，全部命中。
  报告层则**一次都没读这个文件**。
- **不改的后果**：① 报告里没有进程章，看不出后台进程齐不齐、archiver 有没有卡住；
  ② 若照抄台账行数，报告会说出"**16 个 postgres 进程**"——把采集时的 shell 当成实例后台进程，
  这是**用噪声冒充证据**，比缺这一节更坏。
- **改法**：分两层。
  - **采集侧**：判据落到 **COMMAND 第一列**（`$7`）——
    `awk '$7 == "postgres" || $7 == "postgres:" || $7 ~ /\/postgres$/'`。
    真机同一份数据实测 **16 行 → 8 行**，剔除的正好是那 8 行噪声。
  - **展示侧**：`parse_postgres_processes` 按命令行**重判**（兼容旧包），把
    postmaster / 后台角色 / client backend 分开，并**把剔掉的行数印进 note**
    （「快照共 16 行，其中 8 行经命令行判定不属于本实例…已剔除」）——
    「已过滤」与「没采到」靠这个数字区分。
  - **期望集合按能力裁剪**：`archive_mode=off` 时不该有 `archiver`、备库才有
    `walreceiver`（判据取 `capabilities.tsv`）。拿固定清单比会把"本环境不适用"报成"缺失"。
- **影响面**：PG 4.8 新增一节（本机 4 行）；新采集包 `row_count` 由 16 降为 8。
- **守门**：`test_process_rows_exclude_the_collector_own_process_tree`（断言
  `su - postgres` / `bash ...sh` / `sleep 5` / `awk` **不得出现在任何行**里，
  且 note 必须写「已剔除 8 行」）+ `test_missing_worker_is_reported_as_attention`
  （配对：上一条防"把噪声当进程"，这一条防"把缺进程当齐全"）。
  负控制 M2（噪声全算进程）/ M3（**过度修正**：全部剔除、workers 归零）**都红**。

### F-45（P2）能力探测 17 个键只消费了 2 个 —— ✅ 已落地（批 3B-B1）

- **实际行为**：`tables/capabilities.tsv` 有 17 个键（`pg_version` / `is_in_recovery` /
  `wal_level` / `archive_mode` / `max_wal_senders` / `pg_stat_io` / `pg_stat_checkpointer` …），
  报告只用了 `pg_stat_statements` 与 `sar_available` 两个（`instance.capabilities`），
  其余 15 个采了没人看。
- **不改的后果**：读者看不到"这台实例支持什么"——例如 `pg_stat_io` / `pg_stat_checkpointer`
  是否可用，直接决定 I/O 与检查点那两节的结论**能不能成立**；
  `pg_stat_statements=0` 时报告不说，读者会以为「SQL TOP 分析」是分析漏了。
- **改法**：新增展示条目 `pg.capability_probe`（标题「能力与特性探测」）放在
  **实例基础信息章** —— `is_in_recovery` / `wal_level` / `archive_mode` 本来就决定这一节的结论。
  17 个键的口径收在模块级 `CAPABILITY_NOTES`（单一来源）。结论只用**已探明**的键下判断，
  缺的键不猜（"没探到"不等于"不支持"）。
- **影响面**：PG 3.2 新增一节（17 行）；其余三库不变。
- **守门**：`test_capability_notes_cover_every_collector_key` —— 候选集是**独立来源**
  （从采集脚本的 `printf` 抽键），不从 `CAPABILITY_NOTES` 自己取；从被测对象取样本的话，
  实现漏了键、候选集跟着漏、测试照样绿（批 24 踩过）。负控制 M1（删一个说明）真实变红。

### F-46（P2）inode 使用率只进了 note 一句话，**没进表格** —— ✅ 已落地（批 3B-B1）

- **实际行为**：`system.inodes` 台账 `ok`、9 行；`presentation.py` 里已有
  `_parse_inode_rows`，但只把「inode 使用率最高 N%（/）」拼进 `note`，
  5.1 表里只有「使用率 / 可用空间」两列。空间没满、inode 先满是生产上典型的
  "磁盘没满却写不进去"（小文件多 / 临时表多 / WAL 段没清），藏在一句 note 里没人看得到。
- **不改的后果**：容量风险只判了空间一个维度；inode 到 100% 时报告仍印「容量正常」。
- **改法**：`system.filesystems` 每行**并列一列 `inode 使用率`**（按挂载点 join）；
  风险判据改为**两个维度取最短**（任一 ≥80% 即 `risk`），且处置建议分开 ——
  inode 满要"清小文件/提高 inode 上限"，空间满才是"清日志/WAL 或扩容"。
  ★ 取不到时必须写「未采集」，**不许写 0%** —— 「没取到」与「用了 0%」必须能区分。
- **影响面**：PG 5.1 表由 5 列变 6 列；其余三库不变。
- **守门**：`test_inode_column_is_present_and_missing_reads_as_not_collected`
  （成对：有 inodes.tsv 时不许印「未采集」；**没有时不许印 0%**）。负控制 M4 真实变红。

### F-47（P2）NUMA 拓扑采了 7 行，报告里没有 —— ✅ 已落地（批 3B-B1）

- **实际行为**：`numa.txt`（`numactl --hardware`）7 行，`PG_SECTION_SOURCES["system.host"]`
  里**已经登记了** `system.numa`（状态算进了这一节），但 `os_rows` 里没有任何一行来自它。
- **不改的后果**：多路机器上"内存落在哪个 node"直接决定访存延迟与绑核策略；
  单节点也必须报出来，否则读者分不清「单节点」与「这项没采到」。
- **改法**：`parse_numa` 解析节点数 / 每节点 CPU 与内存，在 4.1 加一行「NUMA 拓扑」。
- **影响面**：PG 4.1 多 1 行。
- **守门**：并入 `test_new_items_have_uniform_row_keys` 与 4.1 的产物核对。

### 批 3B-B1 的一把额外的尺子：**「采了没报」要按 F-38 同一把尺子量**

F-38（外键索引）当时判的是**及格线**，因为它"采到了真问题却没报"。本批这五项是同一性质：
采集台账 `ok`、文件有内容、报告零引用。所以它们**不该被归进「加分题（缺采集项）」** ——
加分题的定义是"少采、采得浅"，这些是"采了不用"。两者混在一起答，会让"加分题"这个桶
装进本该立刻修的缺陷。

判据：**报告层 grep 不到某个采集项的任何引用**，而台账状态是 `ok` ⇒ 及格线。
工具：`_work/scan_b1_unused.py`（把采集包文件清单与 `raw_lines` / `tables.get` /
`find_table` / `model.src` 四处消费点对账）。

## 8. Python 联动表（改采集就必须同步改这些）

| F | 采集侧改动 | 必须同步的 Python |
|:---:|---|---|
| F-01 | `pg_query_tsv`/`pg_query_tsv_db` 改用 `--csv -P csv_fieldsep` | `inspection_core/tabular.py::parse_delimited`（**改法在这一侧**，且影响四库）|
| F-02 | 占位表头扩到 49 项 + 分成主版/逐库版 | 无（`parse_delimited` 按表头取值，天然兼容）；但要**新增断言** |
| F-04 | `ok` 判据加 `max_gap_seconds`；reason 带 `discontinuity_count` | `plugins/postgresql/report_adapter.py` L150-158 的 `sar_usable` 判据要一起看；`inspection_core/sampling.py::effective_coverage_hours` |
| F-05 | ✅ 已删旋钮 ⇒ **无新增文件** | 无（原计划里的 `metrics.py` / `snapshot.privacy` 改动随之取消）|
| F-06 | ✅ 已删死分支 | 无 |
| F-07 | 新增 `EXIT_BLOCKED=21` / `EXIT_PACKAGE_FAILED=22`，末尾 `exit` | 外层编排（`tools/` 里的批处理）按新码分支；INTERFACE §8 |
| F-08 | **21** 个 `has_cmd` 补 `else record_skipped`（已落地三库 21/17/12 处） | `package_adapter.collection_quality` 的 `total` 会**变大** ⇒ `score` 会**下降**（这是修对了）⇒ **基线要刷新** |
| F-09 | 只登记 | `plugins/postgresql/report_adapter.py` 若按 category 聚合要改；`docs/postgresql-collector-interface.md` §4 |
| F-10 | 无 | `plugins/postgresql/package_adapter.py` L46-56 显式化逐库优先 |
| F-11 | 注释分层 | 无 |
| F-12 | 无 | `inspection_core/models.py`（`RuleEvaluation.source_status`）、`inspection_core/`（下沉 `TableFetch`/`fetch_state`/`state_is_collected`）、`plugins/postgresql/{metrics,rules,presentation}.py` |
| F-13 | 无 | `plugins/postgresql/rules.py`（删本地 dataclass）、`inspection_rules.json`（加 owner/apply_mode/verification）、`analyzer.py`/`presentation.py` 的 import、基线 |
| F-14 | 可选：把 `collection_status` 摘要也塞进 snapshot（**不推荐**，直接读 `ctx.status` 更干净）| `plugins/postgresql/presentation.py` L54-87 |
| F-15 | 无 | `plugins/postgresql/presentation.py`（新增 `DISPLAY_ITEM_ALIASES`）|
| F-16 | 无 | `plugins/postgresql/presentation.py` L306-317 / L414-418（改为读 metrics）|
| F-17 | 无 | `plugins/postgresql/{metrics,rules}.py` |
| F-18 | 无 | `plugins/postgresql/rules.py` + `report_adapter.py` L165-170 |
| F-19 | 无 | `plugins/postgresql/report_adapter.py` L83-98 / L179-186（删除）|
| F-20 | 无 | `plugins/postgresql/presentation.py`（删 6 个方法）|
| F-21 | 无 | `plugins/postgresql/{rules.py,inspection_rules.json}` |
| F-22 | 无 | `plugins/postgresql/presentation.py`（就是 F-14/F-15）；**不要动 `word_engine.py`** |
| F-23 | 无 | `plugins/postgresql/{presentation.py,word_report.py}` |
| F-24 | 无 | `plugins/postgresql/charts.py` L61-77 |
| F-26 | 删 `coverage.json` | 无 |

## 9. 批次顺序与"哪一批不改完不能上生产"

| 批 | 内容 | 能不能上生产 |
|:---:|---|---|
| **0** | F-11 注释分层（零逻辑改动，指纹法证明）| 能（不停机、无行为变化）|
| **1** | F-01 F-02 F-04 F-05 F-06 F-07 F-08 F-10 F-27（+F-09 只登记）| **不改完不能上生产**：F-01 会让报告里的长事务/锁等待数字失真；F-07 会让外部编排分不清成功与失败；F-08 会让"采集完整度"分数虚高；F-27 会让不适用项报"正常" |
| **2** | F-12 F-13 F-14 F-15 F-16 F-17 F-18 F-19 | **不改完，报告里"采集状态：正常"不可信** ⇒ F-14/F-15/F-22 三者是一组，必须同批 |
| **3** | F-20 F-21 F-23 F-24 F-26 | 能上生产（可维护性 + 图表信号完整性）|
| **3B-B1** | F-43 F-44 F-45 F-46 F-47（网络 / 进程 / 能力 / inode / NUMA —— 采了没报）| 能上生产：纯展示层，另含一处采集判据修正（`ps ... | awk` 落 COMMAND 列）。**不改评分口径**，分数与规则触发数不变 |

**跨批硬约束**：
- F-14 / F-15 / F-22 是**同一件事的三段**，**任一段单独上都会让报告更糟**（比如只做 F-22 不改上游 ⇒ 没有数据可印）。
- F-01 改输出格式 ⇒ **必须同时改 `parse_delimited`**。⚠ 爆炸半径**需要更正**：core 的 `inspection_core/tabular.py:parse_delimited` 只被 **MySQL + PG** 使用（`mysql/package_adapter.py:97`、`postgresql/package_adapter.py:49,55`）；**Oracle 有自己一份带 `repair_wrap` 的实现**（`oracle/package_adapter.py:388`）⇒ 不受影响。所以**不是"四库全挂"，而是"MySQL / PG 两库必须一起验，Oracle 另行确认它那份没被动到"**；受影响的测试是 `tests/test_oracle_package_adapter.py:107,115,116`（它直接测 core 的那个 `parse_delimited`）。
- F-27 是**三库同形**（MySQL L605 / Oracle L1507 / PG L458）⇒ 要么三库同批改，要么在提交说明里**显式写"本轮只改 PG、另两库未改"**，否则三份报告之间不可比。
- `SNAPSHOT_SCHEMA_VERSION` 的 bump 跟着 §10 的规则走。

**🔸 复核后对批次顺序的补充建议（供 D-7 参考）**：F-11（注释分层）**不建议单独占一个批** —— 它与数据语义无关，却要动上千行、把后续每个 diff 都泡在大段注释噪声里，还给 `git blame` 添乱。建议降级为"文件头补一张 L1 契约表 + 关键函数各补 1 行前条件/后条件"（约 40 行增量），**并进批 1**。批次顺序改为 **1（低风险止血）→ 1C（F-01，动 core，单独一个窗口）→ 1B（F-08 + F-27，改分数口径须拍板 D-3 并刷基线）→ 2（最值钱、最大，须拍板 D-6）→ 3（可维护性）**。

## 10. 回归验证步骤

### 10.1 每批通用

```bash
# 1) 语法
bash -n inspection/pg_inspection_standard.sh

# 2) 注释期专用：指纹法（批 0）
cp inspection/pg_inspection_standard.sh /tmp/pg.bak
strip() { grep -v '^[[:space:]]*#' "$1" | grep -v '^[[:space:]]*$'; }
diff <(strip /tmp/pg.bak) <(strip inspection/pg_inspection_standard.sh) && echo "逻辑零改动 ✓"

# 3) PG 单测
"C:/Users/wangjs/.workbuddy/binaries/python/versions/3.11.9/python.exe" -m pytest tests/test_postgresql_plugin.py -q

# 4) 全量回归 —— 必须与改动前**逐条同名**对齐（当前基线：575 passed / 5 failed / 350 subtests，5 条全是 tests/test_mysql_*）
"C:/Users/wangjs/.workbuddy/binaries/python/versions/3.11.9/python.exe" -m pytest -q
```
- **多出来的失败先当回归查**（本项目实测：多出来的都是真缺陷）。
- pytest **启动收集时**新测试文件尚未落盘时会少算 ⇒ 新增测试文件后**重跑一次**确认数量。
- 临时目录**不要用 `/tmp`**（本机 MSYS2 下进程替换 FIFO 与沙箱交互偶发卡死），用脚本同级绝对路径 `.st.XXXXXX/` 并加进 `.gitignore`。

### 10.2 不连库的自测桩（落地每批时写）

按**函数名**抽取被测函数（**不要用行号** —— 你正在改这个文件，行号必然漂）：
```bash
_extract_fn() {
    local out="$1"; shift
    : > "$out"
    local fn s e
    for fn in "$@"; do
        s=$(grep -n "^${fn}() {" "$SRC" | head -1 | cut -d: -f1)
        [ -z "$s" ] && { printf '找不到函数 %s\n' "$fn" >&2; return 1; }
        e=$(awk -v s="$s" 'NR>s && /^\}$/{print NR; exit}' "$SRC")
        sed -n "${s},${e}p" "$SRC" >> "$out"
    done
}
_extract_fn _pg_fixture.sh pg_query_tsv pg_query_tsv_db record_status record_skipped known_tsv_header ensure_tsv_header classify_failure
```
配桩要点：
- 把 `pg_exec` 打桩成 `printf '%b' "$FAKE_OUT"`（夹具内容从环境变量灌），**不要**调真 psql。
- `record_status` 改成**追加**写一个计数文件（真实现是覆盖写，覆盖了就看不出"同一 item 记两次"）。
- `SRC` 用**绝对路径**且**不要把自测脚本拷到别处跑**（否则 `$0` 解析到错目录，表现为"找不到函数 / 全部 FAIL"）。
- **`case` 模式的坑**：如果夹具里要用 `case`，**裸空格是词分隔符** ⇒ `*a b*)` 会 `syntax error`，而整段桩解析失败会**让所有探针一起返回空**，现象酷似"采集器查不到数据"（Oracle 批 22 实测：14/11 全是假失败）。生成期必须把 here-string 正文**单独抽出来 `bash -n`**，并用**变异生成器**自证（塞回坏模式 ⇒ 生成器必须 `exit≠0`）。
- here-string 一律用**单引号**（`<<'EOF'`），双引号会吃掉 `\1`/`\r`。

### 10.3 端到端（每批收尾）

```bash
PKG=<现场包>.tar.gz
OUT=_work/verify_pg
rm -rf "$OUT"
python plugins/postgresql/cli.py "$PKG" -o "$OUT"
python generate_report.py "$OUT"            # 注意：cli.py 已把 report_model.json 换成 adapter 之后的形态
python - <<'PY'
# 解 DOCX XML，数「采集状态：」后跟的状态词（呈现层必须读渲染产物）
import zipfile, re, collections, sys
z = zipfile.ZipFile("_work/verify_pg/*.docx")
xml = z.read("word/document.xml").decode("utf-8")
print(collections.Counter(re.findall(r"采集状态：(\S+?)[；<]", xml)))
PY
```
- **两条链路别混**：`plugins/postgresql/cli.py` 把 `report_model.json` **就地**换成 adapter 之后的形态（`legacy_copy` 是 adapter 之前的那份）。判"报告有没有某章/某图"必须**端点跑一遍再拆 DOCX**，不要只看 `presentation.py`。
- **等价性证明的判据是"产物逐项相同"**，不是"测试绿"。比对时抹掉 `generated_at` / `duration_ms` / 图表 `path` / 封面时间戳。

### 10.4 基线刷新（F-08 / F-12 / F-13 / F-14 / F-22 / F-27 落地后**必须**做）

- 基线位置：`tests/baselines/postgresql/legacy_2_0_1/`（**本机存在、被 `.gitignore` 忽略**，是本地产物，不是仓库资产）。
  `git check-ignore -v` 实测：`.gitignore:15:tests/baselines/`。
- 刷新方式：用**真实包**整体重出（`analysis.json` / `report_model.json` / `llm_input.json` / `charts/` / `.docx`），并重算 `expected_summary` / `output_sha256`。
- **业务判据不变时 `expected_summary` 必须逐项相同** —— 这是最强证据。本轮 F-08 会**故意**改变 `total_items` / `score`（缺项进账了）⇒ **那一项的变化必须在 `refresh_notes` 里写明原因**，其余项不得变。
- `source_implementation.*`（外部实现指纹）**刻意不重写**。

## 11. 待决策项（每项：实际行为 → 不改的后果 → 可选项 → 推荐 → 影响面）

### D-1 F-01 用方案 A（`--csv`）还是方案 B（逐列 `regexp_replace`）？

- **实际行为**：现在 `pg_query_tsv` 用 `psql -A -F $'\t'`，官方手册明确"字段分隔符出现在值里**不做特殊处理**"，而脚本只对 `pg_stat_statements.query` 做了清洗。
- **不改的后果**：`long_transactions` / `idle_transactions` / `lock_waits` 三张表的查询文本一旦含换行（Java/JPA 应用的多行 SQL 很常见），整张表**列错位**，报告里的长事务数、最长事务时长、锁等待全部失真，且**静默**。
- **可选项**：
  - **A**：`psql --csv -P csv_fieldsep=<TAB>` + 能力探测回退 + `parse_delimited` 改流式。**单点**、覆盖未来新增列；对干净数据语义等价。
  - **B**：只给 3-4 处自由文本列补 `regexp_replace`。代价小；但**新增列会复发**（脚本已经犯过一次）。
- **推荐**：**A 为主，B 不撤、保留为回退路径的安全网**。理由：`--csv` 是 **PG12+ 客户端**才有的选项，能力探测失败时会回退到 `-A -F TAB` —— **回退后的那条路上，只有 B 能保护行结构**。所以 B 不是"先止血后废弃的临时工"，而是回退分支的必要组成；两者都要，只是 A 是主路径。
- **影响面（⚠ 此处修正）**：不是"四库全挂"。`inspection_core/tabular.py:parse_delimited` 的调用方只有 **MySQL**（`mysql/package_adapter.py:97`）与 **PG**（`postgresql/package_adapter.py:49,55`）；**Oracle 有自己的 `parse_delimited`（`oracle/package_adapter.py:388`，带 `repair_wrap`）**，不走 core 那份 ⇒ 只需**两库各出一份报告比对**，另加确认 Oracle 那份没被误动。受影响的测试：`tests/test_oracle_package_adapter.py:107,115,116`（名字叫 oracle，实际直接测 core 的函数）。

### D-2 F-07 新增退出码 21/22 会不会打断现有 cron/流水线？

- **实际行为**：脚本现在用最后一行 `&&` 链决定退出码；`--no-package` 与"扫描拦截"都返回 1。
- **不改的后果**：外部编排把"故意不打包"当成"失败"重试，或者把"扫描拦截"当成成功（取决于怎么判 1）。
- **可选项**：① 加 21/22 并改末尾 `exit`；② 只修 `--no-package` 的 0，拦截仍用 1；③ 什么都不改，靠 status 文件判。
- **推荐**：①。**已核实**：全仓库扫描 `pg_inspection_standard.sh` 的引用，**只有文档与脚本自身**（`README.md` / `docs/*.md` / `docs/*.yaml` / 脚本自己），**没有任何 cron / 流水线 / 包装脚本在仓库里读它的退出码** ⇒ 加 21/22 在仓库范围内**零风险**。**唯一要你确认的是仓库外**：现场是怎么调它的（人工敲 / 现场自己的定时任务 / 别的编排）。若现场只看 `status` 文件，① 完全无风险；若现场按 `rc!=0` 重试，加新码**反而是好事**（不再把"故意不打包"当失败重试）。**这一条不要凭推测放行** —— 仓库内无调用方 ≠ 现场无调用方。
- **影响面**：只有仓库外的调用方；脚本内部无依赖。

### D-3 F-08 补 `else` 后 `collection_quality.score` 会**下降**，报告"数据质量"分会变难看

- **实际行为**：现在 **16 个**依赖命令的采集项缺失时**不登记**，分母变小 ⇒ 分数虚高。
- **不改的后果**：报告印出的"采集完整度 95%"是**因为少算了这些项**；真实覆盖度可能只有 80%。`COMMON.COLLECTION.QUALITY` 也因此更难触发。且**越不规范的机器虚高越多**（16 项里 `free`/`df`/`lsblk`/`mount`/`sysctl`/`ps`/`systemctl` 到处都有，真正会缺的正好是 `numactl`/`ss`/`iostat`/`dmesg`/`chronyc`/`ntpq`/`timedatectl` —— 精简安装 / 容器 / 老系统的典型缺项）。
- **可选项**：① 补 `else` 并**接受**分数下降（真实）；② 补 `else` 但把 `unsupported`/`not_applicable` 从分母**剔除**（`score = ok / (total - unsupported - not_applicable)`），分数不大变；③ 不改。
- **推荐**：**②，但必须配"双指标可见"**。理由：客户现场必然有若干项"本机就是不适用"（没有 `numactl`、没有 `patroni`），把它们算进分母是在**惩罚环境而不是惩罚脚本**。但**只剔除不展示，会把一个谎言换成另一个谎言**（"这台机器没几项可采"的客户会得到虚高的分数）。所以要求：报告里**同时印三个数** —— `ok` / `可采信基数 = total - unsupported - not_applicable` / `total`，并把 `not_enabled` / `permission_denied` / `error` / `timeout` **明确算作扣分项**（这几类才是脚本或权限的失职）。
- **影响面**：`plugins/postgresql/package_adapter.py` 的 `collection_quality`、报告里的"数据质量"一栏、基线。
- **附带（F-27）**：`system.hugepages` 这条**死采集项**也要在这里一起定 —— 它既然 PG 侧无人消费，就**不该占着分母**：要么补一条 THP 规则让它产出结论，要么从登记里摘掉。**保持现状（占分母、零结论、还记 `ok`）是最差的选项。**
- ⚠ **这条我明确请你拍板**：② 会改变报告里客户看到的那个百分数，我不替你决定。

### D-4 F-09 改 `category` 要不要现在做？

- **实际行为**：`ha.patroni/repmgr/manager`、`system.backup_*` 都挂在 category `pg.backup`。
- **不改的后果**：按 category 聚合的场景（备份完整度、高可用完整性）会串味。
- **可选项**：① 本轮只登记（推荐）；② 随 schema 主版本一起改（`2.0 → 3.0`）。
- **推荐**：①。改 `category` 是**破坏性变更**，会让已有的包和基线同时失效。
- **影响面**：②的情况下，所有按 category 分组的地方 + 老包兼容分支。

### D-5 F-13 给规则包补 `owner/apply_mode/verification` 时，`owner` 写什么？

- **实际行为**：现在 PG 的 `_build_plan` 里 `owner=None`、`planned_window=None`，`verification` 是一句通用话。
- **不改的后果**：整改方案表没有责任人/变更方式/验证方式，客户拿不到可执行清单。
- **可选项**：① 按角色写（`DBA` / `系统管理员` / `应用负责人`）；② 留空由人工填；③ 写具体人名。
- **推荐**：①。`inspection_core/models.py` L33-36 已经定了规矩：**写角色不写人名**（跨客户才不会错）。
- **影响面**：`inspection_rules.json` 44 条 rule、报告"优化建议/整改方案"表。

### D-6 F-17 去掉 `settings.get(k, 默认值)` 的默认值，会不会让报告少一批"建议项"？

- **实际行为**：`ssl`/`statement_timeout`/`log_min_duration_statement` 等 5 条判据在参数取不到时会**触发**（因为默认值是"关闭/未设置"）。
- **不改的后果**：`settings.tsv` 采集失败的环境会收到 5 条**假告警**，客户按它去改参数、改完没效果。
- **可选项**：① 去掉默认值 + 走 `source_status`（推荐）；② 保留默认值，只在 reason 里注明"取自默认值"；③ 不改。
- **推荐**：①。理由：这是"没采到 ≠ 有问题"的直接应用；额外好处是 **5 条假告警 + 4 条假通过**也一起消失（全部变成"证据不足"）。
- **影响面**：`metrics.py` L191-217、`rules.py` 对应 9 条、基线里 `rule_evaluations` 的状态分布会变（`not_evaluated` 变多）⇒ **基线刷新时必须逐项说明**。

### D-7 批 1 怎么切？（复核后更新，推荐已改）

- **实际行为**：批 1 内部各子项的风险差异**极大** —— F-11 要动上千行但零语义变化；F-02/F-04/F-05/F-06/F-10 是几十行、不碰输出口径；**F-08/F-27 会改 `collection_quality` 的分母（客户可见的百分数）**；**F-01 要动 MySQL/PG 共用的 `inspection_core/tabular.py`**。三类混在一个批里，一旦回归，**定位成本远高于拆批**。
- **可选项**：
  - **①** 先做"零风险三项"（F-11 注释 / F-05 F-06 死代码 / F-07 退出码）+ F-02；
  - **②** 上面这版，但**把 F-11 降级**（不再单独占批，只补文件头契约表 + 关键函数各 1 行）并**并进批 1**；
  - **③** 直接做全批 1（含 F-01、F-08、F-27）。
- **推荐**：**②**，并按四段推进：
  | 段 | 内容 | 为什么单独一段 |
  |:--:|---|---|
  | 1A | F-02 F-04 F-05 F-06 F-07 F-10（+ F-09 只登记 + F-11 降级版）| 不碰任何输出口径，验完即可上生产 |
  | 1C | F-01 | 动 MySQL/PG 共享的 `tabular.py` ⇒ 需单独一个客户少的窗口 + 两库各出一份报告比对 |
  | 1B | F-08 + F-27 | 改 `collection_quality` 分母 ⇒ **须先拍板 D-3**，且**要刷基线** |
  | 2 | F-12…F-19 + F-22 | 最值钱也最大；三个"采集状态"缺陷必须同批 |
- **不推荐 ① 的原因（我改变了原先的推荐）**：F-11 全量注释分层**与数据语义无关**，却要动上千行 —— 后续每个功能 diff 都会被泡在大段注释噪声里，`git blame` 也失去意义；而它想达成的效果（让契约可见）**用 40 行就能达成**。**先做它的收益是"看得舒服"，成本是"后面每一批都更难 review"，不划算。**
- **不推荐 ③ 的原因**：F-01 与 F-08/F-27 的风险等级差一个数量级，混批后回归信号会互相掩盖 —— 本项目实测过这类情况（批 20 用不同采集包做对照，凭空多出一张图，看着像代码改了行为，其实是输入不同）。
- **影响面**：①/②/③ 都不影响生产；差别在**改完能否立刻定位问题**。

## 12. 交付物清单

| 文件 | 内容 |
|---|---|
| `docs/postgresql-collector-fix-plan.md` | 本文件：**27 个 F 项** + 3 批 + Python 联动表 + 回归步骤 + 7 个待决策 |
| `docs/postgresql-collector-interface.md` | 采集脚本 ↔ Python 的对接契约（10 节，见该文件目录）|
| `inspection_core/collection_state.py` | **批 2C 新增**：「这项采到没有」的判据 + 章节归并（跨库共用，纯 LF）|
| `tests/test_collection_state.py` | **批 2C 新增**：上述判据的守门（词表互斥性 + 归并矩阵 + 尺子自证）|
