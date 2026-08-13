# 目标架构（Target）

> 状态：阶段 8。公共模型与公共 Word 引擎已落地；MySQL 已完成分层，PostgreSQL 已通过独立插件、标准报告适配器和 Word Profile 接入。PostgreSQL 分析器内部继续拆分，以及 Oracle、SQL Server 接入仍为后续目标。

## 1. 目标

目标不是减少文件数量，而是让每个变化只影响应该负责的层：

- 采集 SQL 变化不要求修改 Word。
- Word 样式变化不要求修改采集和规则。
- 数据库差异保留在插件中。
- 公共状态、风险、报告和测试使用统一契约。
- 旧采集包通过版本 adapter 继续可分析。

## 2. 分层结构

```mermaid
flowchart LR
    C["数据库专属 Collector"] --> RP["原始采集包"]
    RP --> A["Package Adapter"]
    A --> CP["Collection Contract"]
    CP --> N["Normalizer / Metric Providers"]
    N --> F["Canonical Facts"]
    F --> RE["Common Rule Engine"]
    RD["Rule Definitions"] --> RE
    RE --> EV["Rule Evaluations"]
    F --> PB["Presentation Builder"]
    EV --> PB
    PB --> RM["Report Model Contract"]
    RM --> WR["Common Word Renderer"]
    RM --> LE["Optional LLM Enhancer"]
    LE --> RM2["Validated Enhanced Model"]
    RM2 --> WR
```

依赖只能从左向右。公共模型不能导入数据库插件，规则不能导入分析器，renderer 不能读取原始采集包。

## 3. 建议目录

```text
src/
  inspection_core/
    contracts/
    models/
      collection.py
      facts.py
      rules.py
      report.py
    rule_engine/
    reporting/
      model_builder.py
      renderers/docx/
      charts/
    validation/
  plugins/
    mysql/
      package_adapter.py
      parsers/
      metrics/
      rules/
      presentation.py
    postgresql/
    oracle/
    sqlserver/
collectors/
  mysql/
  postgresql/
  oracle/
  sqlserver/
contracts/
tests/
  fixtures/
  baselines/
  contract/
  rules/
  reports/
```

采集器仍可发布为一个 Bash 或 PowerShell 文件。源码仓库是否拆分模块，不应改变客户侧“一条命令采集”的使用体验。

## 4. 三个核心契约

### 4.1 Collection Package

统一的是语义，不是压缩包内的物理文件布局：

- database/target/node 身份。
- collector 和 schema 版本。
- capability。
- 每个 check 的状态、artifact、行数、时间和错误原因。
- 完整性校验。

现有 collector 先由 adapter 映射到契约；以后再决定是否让 collector 原生输出新契约。

### 4.2 Rule Evaluation

每条已注册规则必须产生且只产生一条评价：

- `triggered`
- `passed`
- `not_evaluated`
- `not_applicable`

Finding 只是 triggered evaluation 的管理视图，不再是唯一结果。这样才能证明规则覆盖率，并避免“没发现风险”等于“全部正常”。

### 4.3 Report Model

报告模型只包含渲染所需的稳定语义：

- 封面和文档控制。
- 范围、拓扑、数据质量和健康状态。
- 管理摘要。
- sections/items。
- rule evaluations 和 risk register。
- remediation plan。
- 图表描述和附录。

数据库专属内容进入 section/item 或 `extensions`，不再为每个数据库增加一批顶层字段。

## 5. 标准检查 item

```text
check_id + title + domain
applicability
collection
evidence
data
evaluation_refs
analysis
presentation
```

强制规则：

1. 先保存事实，再保存判断。
2. 原始值使用标准类型和 null，不保存中文占位符。
3. 单位单独保存，不拼到数值中。
4. 表格列定义与行数据分开。
5. item 只引用 rule evaluation，不复制另一套矛盾的判断。
6. 原始明细可标记为 appendix，renderer 决定主文/附录布局。

## 6. 插件接口

每个数据库插件至少实现：

```text
PackageAdapter.detect(source) -> bool
PackageAdapter.load(source) -> CollectionPackage
FactProvider.build(collection) -> FactSet
RuleProvider.definitions() -> list[RuleDefinition]
PresentationProvider.sections(facts, evaluations) -> list[Section]
```

复杂规则可以提供 evaluator 函数；普通阈值、计数、存在性和时间窗口规则优先声明式配置。

## 7. 版本兼容

- 契约版本使用 `major.minor.patch`。
- 新增可选字段提升 minor。
- 修改含义、删除字段或改变状态语义提升 major。
- adapter 必须显式声明支持的 collector major 版本。
- 未识别的新字段进入 extensions 或被安全忽略。
- 未识别的 major 版本必须明确拒绝，不能尽力猜测后继续出报告。

## 8. MySQL 第一次解耦边界

第一次代码改动已完成以下事情：

1. 新建公共模型模块，放置 Finding、RuleEvaluation、CollectionStatus 等。
2. `rules.py` 改为依赖公共模型，不再导入 `analyze_inspection_v2.py`。
3. Analyzer 输出内容、规则数量、风险、评分、report_model 和 Word 保持基线一致。
4. 为公共模型和 MySQL adapter 增加 contract 测试。

落地位置：`inspection_core/`、`plugins/mysql/package_adapter.py`、
`tests/test_inspection_core.py` 和 `tests/test_mysql_package_adapter.py`。完整真实包回归的
`analysis.json`、`report_model.json`、`llm_input.json` 业务内容与阶段 0 基线一致。

第一次改动不做：

- 不重写采集脚本。
- 不统一四种数据库代码。
- 不改 Word 版式。
- 不改变规则阈值或评分。
- 不调用 LLM。

这能用最小行为变化建立正确的依赖方向，再继续拆解析器和报告模型。

## 9. 采集端架构

采集端采用与分析端相同的“公共核心 + 插件”原则，但按操作系统分实现：

- Linux common：MySQL、PostgreSQL、Oracle 共用系统采集、采样、SAR、状态、安全和打包模块。
- Windows common：SQL Server 使用等价语义的 PowerShell 实现。
- database plugin：仅保存数据库连接、能力探测和专属 SQL。
- delivery entry：继续生成四个独立单文件入口。

详细方案见 `docs/collector-capability-matrix.md` 和 `docs/collector-target.md`。
