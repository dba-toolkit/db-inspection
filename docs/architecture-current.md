# 当前架构说明（As-Is）

> 阶段 0 于 2026-08-11 冻结；本文已补充阶段 1—8 的落地状态，原始指纹见 `docs/baseline-inventory.md`。

## 1. 系统边界

系统分为三个运行环境：

1. 数据库服务器：运行 Bash 采集器，只读采集主机和 MySQL 证据。
2. 分析机：离线解包、校验、解析、计算规则并生成 JSON 和图表。
3. 报告机：读取报告模型和图表生成 Word；可选调用 LLM 改写文字。

这种“采集端不做风险判断、分析端不连接数据库”的边界是正确的，应在后续重构中保留。

## 2. 当前采集包契约

采集器版本为 `1.1.0`，package schema 和 snapshot schema 为 `1.0`。主要内容：

```text
snapshot.json
collection_status.json
manifest.json
summary.txt
tables/*.tsv
timeseries/*.csv
history/*
evidence/*
logs/*
```

优点：

- 每个采集项具有稳定 item_id、分类、状态、耗时、行数、文件和失败原因。
- manifest 记录文件大小和 SHA256，分析器会校验完整性。
- 能区分 `ok`、`empty`、`unsupported`、`not_applicable`、`skipped`、`partial`、`permission_denied`、`timeout`、`error`。
- MySQL 5.7/8.0、sys schema、Performance Schema、复制和日志能力可以按 capability 跳过。

不足：

- 表格字段没有逐检查项 schema_version。
- 分析器仍直接依赖 `tables/*.tsv` 文件名和列名。
- 新增文件不会自动进入报告；删除或改名可能静默减少分析覆盖。
- manifest 解决文件完整性，但没有解决语义兼容性。

## 3. 当前分析契约

分析器版本 `2.1.0`，analysis schema 为 `2.0`，主要输出：

- `analysis.json`：完整实例、指标、发现、采集质量、规则评价和拓扑。
- `report_model.json`：Word 生成器唯一输入，contract 为 `mysql_inspection_report_model`，schema `2.0`。
- `llm_input.json`：适合文字增强的精简数据。
- `charts/*.png`：系统和 MySQL 指标图表。

当前职责已经变为：

1. `plugins/mysql/package_adapter.py`：采集包兼容、安全解压和原始文件解析。
2. `plugins/mysql/metrics.py`：指标派生。
3. `inspection_core/statistics.py`、`sampling.py`：公共统计与 SAR 质量判断。
4. `plugins/mysql/presentation.py`：报告 item、章节、管理摘要和 report model 构造。
5. `plugins/mysql/rules.py`：规则执行和 Rule Provider。
6. `plugins/mysql/charts.py`：MySQL 专属图表（QPS/TPS、连接与线程）+ 调用公共层产出四张 OS 图。
7. `AnalyzerV2`：流程编排、采集质量、拓扑、健康汇总和输出落盘。
8. `inspection_core/word_engine.py`：公共 Word 渲染能力。
9. `plugins/mysql/word_report.py`：MySQL Word 契约、章节顺序和图表映射。

数据库专属采集布局、指标、规则、图表、报告构造和 Word 配置均已离开流程编排器；Word 引擎不导入数据库插件。

### 公共 OS 层（已落地，尚未接线）

Linux 主机的分析与画图对四个库是同一件事。此前每个插件各写一份，已经出现可度量的分叉：同一检查的 iowait 阈值分别是 20/20/15，内存判据一处用 `average` 一处用 `max`，同一个检查有三套 rule_id 拼写。公共层把这些收口，插件只保留库专属部分。

- `inspection_core/system_checks.py`：OS 指标归一化（同时吃嵌套 `system_history`/`system_realtime` 与 PG 的扁平 `*_max`）、数据源选择与置信度、`os_pressure_report()`（选窗口 + 逐条判定）、`CANONICAL_RULE_IDS`/`RETIRED_RULE_IDS`、`DEFAULT_THRESHOLDS`、系统资源概要行。
- `inspection_core/charts/style.py`：调色板、14 个语义序列别名、严重度色、rcParams（matplotlib 可选）。
- `inspection_core/charts/history.py`：时间解析、展示时区、断点检测、序列抽取、`CPU_SUMMARY_VALUES`。
- `inspection_core/charts/specs.py`：四类 OS 图的纯数据规格（CPU/内存/磁盘/网络），不渲染。
- `inspection_core/charts/render.py`：规格 → PNG，matplotlib 后端 + Pillow 降级后端，结果始终记录真实渲染器。

已收口的口径（不再允许插件各自声明）：

| 项目 | 结论 | 理由 |
| --- | --- | --- |
| iowait 判据 | 窗口**峰值** ≥ 20% | 原 PG 用 15 且口径不同，统一到 MySQL/Oracle 已有的 20 |
| 内存判据 | 窗口**峰值** ≥ 90% | 原 MySQL/Oracle 用 `average`，24h 里 2 分钟打满会被平均掉 |
| 规则命名 | `COMMON.SYSTEM.{CPU,IOWAIT,MEMORY}_PRESSURE`、`COMMON.SYSTEM.DISK_UTIL` | 原三套拼写；三库现已全部改名，旧拼写只留在 `RETIRED_RULE_IDS` 供读取历史报告 |
| 阈值归属 | 全部从 `DEFAULT_THRESHOLDS` 读，插件规则包不再声明 OS 阈值 | 同一检查曾有 20/20/15 三个值，且规则包里的 OS 阈值一旦被误当热更新项会静默失效 |
| 判定入口 | 一律 `os_pressure_report(metrics, include=(...))` | 选窗口与判数字必须来自同一窗口；插件只决定评价哪几条 |
| 覆盖披露 | SAR 覆盖 < 80% × 24h 时给出低置信度理由 | 50 分钟的采样不能当一天用；此前把「小时」和比例常量 0.8 直接比较，单位是错的 |


接线顺序（每步独立可回归）：①加公共模块不接线（全量 96 项测试零回归）→ ②MySQL 切（已删除 `plugins/mysql/chart_style.py`，6 张图规格与 PNG 逐字节不变，测试 96→99 项零回归）→ ③Oracle 切（已删除 `plugins/oracle/chart_style.py`，OS 章节 5 张并成 4 张公共图并改中文标题，Oracle 基线同步刷新，测试 99→109 项零回归）→ ④PG 切（已删除 `plugins/postgresql/chart_style.py` 与 `parsers.py` 里的 8 个私有 SAR 解析函数，系统章节 3 张并成 4 张公共图，PG 基线刷新，测试 109→117 项零回归）→ ⑤OS 判定/阈值/rule_id 收口（三库的 `_check_system_resources` 改为 `os_pressure_report()` 单次调用，插件规则包删除 OS 阈值，OS rule_id 统一为 `COMMON.*`，方案清除 2 条死规则，Oracle/PG 基线刷新，测试 117→**133** 项零回归）→ ⑥规则包配置键消费面收口（删 6 个没人读的键，零输出变化、不必刷基线，测试 133→139 项零回归）→ ⑦severity 二级分级（三库 `_evaluate` 新增 `severity_override` 形参，Oracle 三处接线二级阈值，报告侧 finding 排序与健康分随 severity 变化，Oracle 基线刷新，测试 139→151 项零回归）。SQL Server 不并入：其图表是 Pillow-only、Windows 字体与性能计数器语义都不同，仅共享调色板与 Pillow 后端。


Oracle 这步与 MySQL 不同：它的图表文件名被 `word_report.py` 的 `section_charts` 和 `report_adapter.py` 的图题表按名字绑定，所以切公共规格必然改动报告外观与文件名 —— 这是有意变更交付物，已按 §22.7 逐项记录。Oracle 专属四张（物理 I/O / 逻辑读与物理读 / Redo / 解析率）仍由插件用 matplotlib 绘制，但配色与时间处理取自公共层，不再各留一份 `chart_style.py`。

PG 这步暴露了一个更早就存在的缺陷：`package_adapter.py` 一直用逗号 CSV 解析器读 `history/sar_*.csv`，而那是 `sadf` 的分号格式，于是 `ctx.history` 里的 SAR 数据始终是空的，插件只能自己去 `root/history/` 重读一遍。改成 `parse_sadf` 后，历史数据进入公共上下文，私有解析整块删除。同批修掉的还有 `metrics.py` 里 `label == "idle"` 的死分支（解析结果里没有 `idle` 这一条，CPU 历史峰值从未参与合并）与 chart→section 的双份映射（analyzer 那份值错为 `filesystem_capacity`，且被 adapter 覆盖）。详见 `docs/PROJECT_MAP.md` §22.9。

第⑤步把"画图"和"判定"彻底对齐：三个插件原先各自决定用历史还是实时窗口、各自算置信度、各自拼结论文案，现在只剩一次 `os_pressure_report()` 调用，返回值里带窗口、理由、置信度、阈值和成句事实。阈值从插件规则包撤出（撤掉的还有 `filesystem_usage_critical`），规则包只留 title/summary/recommendation/severity 这类文案。判定一次顺手暴露了两个真缺陷：`os_source` 的扁平分支把 `sar_effective_coverage_hours`（小时）直接和比例常量 `0.8` 比较，50 分钟的 SAR 也会被当成可用日历史；PG `metrics.py` 的 `round(x, 2) if x else None` 把真实的 `0.0`（空闲主机 CPU/iowait）写成 `None`，规则层读作"未采集"。详见 `docs/PROJECT_MAP.md` §22.11 / §22.12。

第⑥步把「阈值归属」推到规则包内部：不光 OS 阈值，规则包里**每一个**阈值键都要有代码真的去读它。判定方法不能按固定参数位置抽——MySQL/Oracle 是 `self._threshold(rule, "key", default)`，PG 是内层函数 `threshold("RULE.ID", "key", default)`，所以按「调用里出现的小写字符串字面量」当请求集合，与「`globals` ∪ per-rule `threshold`」的声明集合做差集。清出 6 个死键（运维照它调数字，改完没效果也不报错），`tests/test_rule_pack_config.py` 6 项拦住复活。

第⑦步补的是这些死键背后暴露的机制缺口：`_evaluate()` 只有 `triggered`/`passed` 两态，`severity` 直接取规则包 JSON 里写死的值，所以「命中率低于 95% 报警告、低于 90% 报严重」这种两档判据在代码里根本表达不出来。三库 `_evaluate` 新增 `severity_override` 形参（不传时行为与旧版完全一致），Oracle 三处接线：`LIBRARY_CACHE` 按二级阈值由 `medium` 升到 `critical`、`REDO_MEMBER` 的硬编码 `< 2` 改读 `redo_member_min`、`TABLESPACE` 那个只用于拼中文措辞的局部变量换成真覆盖。severity 一动就连锁 —— findings 按严重度排名重排并统一重编号、`comprehensive_conclusions` 位移、健康分（severity 计数加权）由 43 掉到 26。详见 `docs/PROJECT_MAP.md` §22.13 / §22.14。

公共层还消化了两处采集形态差异（详见 `docs/PROJECT_MAP.md` §22.8）：Oracle 采集脚本的 SAR CPU 列名是 `%usr`/`%sys`（MySQL 走 `sadf` 原样列 `%user`/`%system`），已由 `SAR_CPU_ALIASES` 归一；Oracle 的扁平 snapshot 没有 `time_evidence`，时区改按「采集器记录 → 时间戳自带偏移 → UTC」回落，否则实时图会按 UTC 少 8 小时显示。

`inspection_core/__init__.py` 刻意保持最小：只暴露模型与标量工具，不导入 `charts`，避免 `import inspection_core` 就拉起 matplotlib。插件按 `from inspection_core.charts import ...` / `from inspection_core.system_checks import ...` 取用。


Word 生成器 4.2.0 提供两种布局：`professional` 为默认专业版，按每章“事实/图表在前、分析在后”组织；`legacy` 保留 4.0 的兼容结构。两种布局消费同一份报告模型，不改变评分、规则、状态或证据。专业版对正文高密度明细设置展示上限，完整数据仍保留在分析结果中。

图表元数据现在明确记录 `source_scope`、`axis_mode`、`time_zone`、`renderer` 和启动点排除数量。主机 CPU、内存、磁盘在 SAR 可用时使用历史证据；MySQL 与网络短时图使用真实时钟标签，并明确披露采集启动点处理。内存和磁盘图不再把不兼容单位放在同一纵轴。

## 4. 当前规则契约

`plugins/mysql/inspection_rules.json` 定义24条规则的元数据、阈值、严重性、证据说明和建议；`plugins/mysql/rules.py` 为每类规则编写 Python 判断方法，并按 `inspection_core` 的 Finding / RuleEvaluation 契约输出。根目录的旧导入兼容壳 `rules.py` 已在阶段 13 删除，`import rules` 请改用 `from plugins.mysql import rules`。

规则配置目前是“配置驱动阈值”，不是“完全声明式规则”：

- 修改已有阈值通常只改 JSON。
- 新增规则必须同时在 JSON 和 `plugins/mysql/rules.py` 中实现。
- 新增派生指标修改 `plugins/mysql/metrics.py`。
- 规则模型位于 `inspection_core/models.py`，规则层不再反向依赖分析器。

阶段 1 已完成公共模型抽离和循环依赖消除；阶段 2 已完成 MySQL adapter 与 metrics 拆分。

## 5. 当前报告契约

每个 MySQL 报告检查项已经采用以下结构：

```text
item_id
title
source
collection
  status / reason / row_count
display
  type / rows / shown_rows / total_rows / note
analysis
  status / conclusion / evidence / recommendation
```

这是后续统一四种数据库报告的最佳基础。

PostgreSQL 已在阶段 8 接入：独立入口读取 PG v2 包，保留旧报告模型，并由契约适配器生成标准报告模型；公共 Word 引擎通过 PG Word Profile 渲染14个技术章节。

阶段 9 已继续拆分 PG 单体分析器：采集包读取、manifest 校验和采集质量位于 `package_adapter.py`；51项指标位于 `metrics.py`；SAR/OS证据解析位于 `parsers.py`；5张图位于 `charts.py`；规则调用边界位于 `rule_provider.py`。旧报告模型构建仍由 `analyzer.py` 编排，后续单独迁移。

首批边界拆分后，PG 三份业务 JSON 与阶段 8 基线一致；34 项自动测试通过。MySQL 双基线回归没有新增差异。

指标和图表拆分后追加执行 `--strict-charts` 回归，PG业务JSON和图表全部匹配；MySQL仍仅有阶段7登记的5项展示差异。

当前不足：

- 公共 Word 引擎已落地，MySQL、PostgreSQL、Oracle 与 SQL Server 均已接入 Word Profile。
- MySQL 报告 item 已迁移到 presentation builder；Oracle、SQL Server 报告 item 均已由各自契约适配器补齐稳定 `item_id`；四库统一 report contract 仍待进一步收敛。
- 专业版已限制正文展示行数并增加证据索引，但更细的主文/附录分流仍应在四库统一报告契约时标准化。
- 报告模型未使用 JSON Schema 做自动校验。
- Oracle 报告模型已由 `plugins/oracle/report_adapter.py` 归一化；SQL Server 报告模型已由 `plugins/sqlserver/report_adapter.py` 归一化（以结构化 sections 为唯一来源）。
- MySQL 34 页和 PostgreSQL 23 页专业版均已使用本机 Word 导出 PDF 并完成逐页 PNG 视觉验收；后续数据库仍需分别执行相同验收。

## 6. LLM 的正确边界

LLM 只能增强以下文字：

- 管理摘要措辞。
- 风险影响描述。
- 整改建议的表达和步骤化。

LLM 不得修改：

- 原始事实和指标值。
- 采集状态。
- 规则状态和严重性。
- 健康评分。
- 证据引用。
- 是否通过、未评价或不适用。

增强前后应保留可追溯版本，并通过 JSON contract 验证。

## 7. 保持兼容的重构顺序

1. 保存可重复的内部完整 MySQL 采集包和现有输出。
2. 增加 contract 测试，不先改变输出字段。
3. 抽离公共模型，消除分析器与规则循环依赖。（已完成）
4. 抽离采集包解析器和指标派生层。（已完成）
5. 将报告模型构造从分析器移出。（已完成）
6. 将 Word 渲染器改为数据库无关组件。（已完成）
7. 版本化升级专业报告结构和版式。（已完成）
8. 接入 PostgreSQL 的 Word Profile 和报告契约适配器。（已完成）
9. 接入 Oracle 的报告契约适配器和 Word Profile。（已完成）
10. 接入 SQL Server 的报告契约适配器和 Word Profile。（已完成）
11. 在分析插件稳定后抽取 MySQL/PG 公共 Linux 采集源码，并按 `docs/collector-target.md` 分阶段推进。（已开始 C0/C1：冻结公共检查目录并抽取 util/runtime/status/security/package 模块）
12. 抽取公共 OS 分析与画图层，逐步替换四库私有实现。（①公共模块已落地且不接线，96 项测试零回归；②MySQL 切 → ③Oracle 切 → ④PG 切 → ⑤判定与规则下沉、阈值与 rule_id 收口 → ⑥规则包配置键的消费面收口 → ⑦severity 二级分级；七步全部完成，151 项测试零回归）

每一步都必须能用同一基线采集包重新生成输出，并解释所有差异。
