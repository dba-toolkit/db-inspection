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
6. `plugins/mysql/charts.py`：图表生成。
7. `AnalyzerV2`：流程编排、采集质量、拓扑、健康汇总和输出落盘。
8. `inspection_core/word_engine.py`：公共 Word 渲染能力。
9. `plugins/mysql/word_report.py`：MySQL Word 契约、章节顺序和图表映射。

数据库专属采集布局、指标、规则、图表、报告构造和 Word 配置均已离开流程编排器；Word 引擎不导入数据库插件。

Word 生成器 4.2.0 提供两种布局：`professional` 为默认专业版，按每章“事实/图表在前、分析在后”组织；`legacy` 保留 4.0 的兼容结构。两种布局消费同一份报告模型，不改变评分、规则、状态或证据。专业版对正文高密度明细设置展示上限，完整数据仍保留在分析结果中。

图表元数据现在明确记录 `source_scope`、`axis_mode`、`time_zone`、`renderer` 和启动点排除数量。主机 CPU、内存、磁盘在 SAR 可用时使用历史证据；MySQL 与网络短时图使用真实时钟标签，并明确披露采集启动点处理。内存和磁盘图不再把不兼容单位放在同一纵轴。

## 4. 当前规则契约

`inspection_rules.json` 定义24条规则的元数据、阈值、严重性、证据说明和建议；`plugins/mysql/rules.py` 为每类规则编写 Python 判断方法。根目录 `rules.py` 只保留旧导入兼容。

规则配置目前是“配置驱动阈值”，不是“完全声明式规则”：

- 修改已有阈值通常只改 JSON。
- 新增规则必须同时在 JSON 和 `rules.py` 中实现。
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

每一步都必须能用同一基线采集包重新生成输出，并解释所有差异。
