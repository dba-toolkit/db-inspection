# 数据库巡检工具（MySQL + PostgreSQL）

当前版本已经拆成“数据库独立入口 + 数据库插件 + 公共核心 + 公共 Word 引擎”。MySQL 与 PostgreSQL 不合并成巨型分析器，各自独立运行：

1. `mysql_inspection_standard.sh`：在数据库服务器采集。
2. `analyze_inspection_v2.py`：在分析机生成 JSON 和图表。
3. `generate_report_docx_v3.py`：生成专业 Word 报告。

PostgreSQL 对应入口是 `analyze_postgresql.py` 和 `generate_postgresql_report.py`；PG 采集器仍保留在 `D:\AI\PG\GPT\pg_inspection_standard.sh`。

## 1. 最短使用流程

### 安装 Python 依赖

```powershell
python -m pip install -r requirements.txt
```

### 第一步：在 MySQL 服务器采集

推荐先使用 `mysql_config_editor` 保存登录信息，避免密码出现在命令行：

```bash
mysql_config_editor set --login-path=inspection --host=127.0.0.1 --user=inspector --password
bash mysql_inspection_standard.sh --login-path inspection --host 127.0.0.1 --port 3306 --user inspector --output-dir /var/tmp
```

采集器生成一个 `mysql_inspection_v1_*.tar.gz`。采集端只收集事实，不做评分、风险判断或整改。

查看全部采集参数：

```bash
bash mysql_inspection_standard.sh --help
```

### 第二步：在 Windows 分析机分析

在项目目录执行：

```powershell
python analyze_inspection_v2.py mysql_inspection_v1_xxx.tar.gz --output output\analysis_latest
```

主要输出：

```text
output/analysis_latest/
├─ analysis.json          完整事实、指标、规则和风险
├─ report_model.json      Word 生成器的唯一输入
├─ llm_input.json         可选文字增强输入
├─ analyzer_status.json   各处理阶段状态
├─ analysis_summary.txt   简要结果
└─ charts/                6 张报告图表
```

### 第三步：生成专业 Word

```powershell
python generate_report_docx_v3.py output\analysis_latest --output output\mysql_inspection_report.docx --customer "客户名称" --target "核心业务数据库" --company "公司名称" --author "编制人" --reviewer "复核人" --logo logo.png
```

python generate_report_docx_v3.py output\analysis_latest --customer "昆山市第一人民医院" --target "EMR数据库" --company "和信科技" --author "王劲松" --reviewer "邓秋爽" --logo logo.png


默认生成 `professional` 专业版。需要复现旧版结构时增加：

```powershell
--layout legacy
```

### 可选：LLM 文字增强

`enhance_report.py` 只允许增强文字表达，不得改变事实、评分、风险级别、规则状态或证据。没有明确需要时可跳过。

### PostgreSQL 最短流程

先使用 PG 原有独立采集器生成 `pg_inspection_v2_*.tar.gz`，再在本项目目录执行：

```powershell
python analyze_postgresql.py pg_inspection_v2_xxx.tar.gz -o output\postgresql_latest
python generate_postgresql_report.py output\postgresql_latest --output output\postgresql_report.docx --customer "客户名称" --target "生产 PostgreSQL 集群" --company "公司名称" --author "编制人" --reviewer "复核人" --logo logo.png
```

PG 分析目录同时保留：

- `report_model_legacy.json`：旧 PG 2.0 报告模型，用于回归和追溯。
- `report_model.json`：标准 PostgreSQL 报告契约，供公共 Word 引擎使用。

## 2. 当前架构

```text
MySQL / PostgreSQL 独立采集器
       ↓ 各自 tar.gz 采集包

分析机
  analyze_inspection_v2.py（MySQL 独立入口）
       ├─ plugins/mysql/package_adapter.py   解包、校验、兼容
       ├─ plugins/mysql/metrics.py           指标计算
       ├─ plugins/mysql/rules.py             规则判断
       ├─ plugins/mysql/charts.py            图表生成
       └─ plugins/mysql/presentation.py      章节与报告模型
  analyze_postgresql.py（PostgreSQL 独立入口）
       └─ plugins/postgresql/                PG 分析、规则、契约适配
                    ↓ 各自 report_model.json

报告机
  generate_report_docx_v3.py（MySQL 独立入口）
       └─ plugins/mysql/word_report.py       MySQL Word 配置
  generate_postgresql_report.py（PG 独立入口）
       └─ plugins/postgresql/word_report.py  PostgreSQL Word 配置
             └─ inspection_core/word_engine.py
                    ↓ 专业 Word 报告
```

依赖方向是单向的：公共核心不知道具体数据库，数据库插件依赖公共核心；采集、分析和 Word 互不反向调用。

## 3. 目录和文件是否有用

| 路径 | 是否保留 | 用途 |
|---|---|---|
| `inspection_core/` | 必须 | 四库可复用的模型、解析辅助、统计、采样和 Word 引擎 |
| `plugins/mysql/` | 必须 | MySQL 专属适配、指标、规则、图表、章节和 Word Profile |
| `plugins/postgresql/` | 必须 | PostgreSQL 分析插件、36 条规则、标准报告适配和 Word Profile |
| `plugins/postgresql/package_adapter.py` | 必须 | 读取 PG 采集包、校验 manifest、建立公共上下文和计算采集质量；不做风险判断 |
| `plugins/postgresql/rule_provider.py` | 必须 | PG 规则执行边界；只接收公共上下文、指标和采集质量 |
| `plugins/postgresql/parsers.py` | 必须 | SAR、文件系统和内存快照的低层证据解析；不做阈值判断 |
| `plugins/postgresql/metrics.py` | 必须 | 从 PG 事实派生51项标准指标；不运行规则、不生成文字 |
| `plugins/postgresql/charts.py` | 必须 | PG 图表生成及时间轴处理；不参与评分或风险判断 |
| `contracts/` | 必须 | 采集包、规则、报告模型的目标契约和示例 |
| `tests/` | 必须 | 自动测试和冻结基线，防止改一个文件破坏其他环节 |
| `docs/` | 必须 | 架构、检查目录、变更影响、采集层目标和阶段记录 |
| `output/` | 保留 | 正式分析结果和 Word 报告 |
| `mysql_inspection_standard.sh` | 必须 | MySQL 独立采集入口 |
| `analyze_inspection_v2.py` | 必须 | MySQL 独立分析入口 |
| `generate_report_docx_v3.py` | 必须 | MySQL 独立 Word 入口 |
| `analyze_postgresql.py` | 必须 | PostgreSQL 独立分析入口；保留旧模型并输出标准模型 |
| `generate_postgresql_report.py` | 必须 | PostgreSQL 独立 Word 入口 |
| `inspection_rules.json` | 必须 | 24 条规则的阈值、严重性和建议配置 |
| `chart_style.py`、`logo.png` | 必须 | 图表样式和报告品牌资源 |
| `rules.py` | 暂时保留 | 旧导入路径兼容层，不在这里增加新规则 |
| `enhance_report.py` | 可选保留 | LLM 文字增强；正常生成报告不依赖它 |
| `AGENTS.md`、`PROJECT_MAP.md` | 必须 | AI/开发维护边界和文件关系地图 |
| 根目录真实采集包 | 基线必须 | 当前自动测试使用的内部完整 MySQL 样例，不得上传外部服务 |

可以随时删除且不影响功能的内容只有：`__pycache__/`、`*.pyc`、`.pytest_cache/`、空的 `tmp/` 和临时分析输出。

## 4. 修改时从哪里下手

| 需求 | 主要修改位置 |
|---|---|
| 增删采集 SQL | `mysql_inspection_standard.sh`，随后检查 adapter 和检查目录 |
| 兼容采集字段/文件变化 | `plugins/mysql/package_adapter.py`、`plugins/mysql/metrics.py` |
| 修改阈值 | `inspection_rules.json` |
| 新增规则 | `inspection_rules.json` + `plugins/mysql/rules.py` + 边界测试 |
| 修改报告内容和分析措辞 | `plugins/mysql/presentation.py` |
| 修改图表 | `plugins/mysql/charts.py`、`chart_style.py` |
| 修改公共 Word 版式 | `inspection_core/word_engine.py` |
| 修改 MySQL 章节顺序和图表映射 | `plugins/mysql/word_report.py` |
| 修改 PostgreSQL 分析/规则 | `plugins/postgresql/analyzer.py`、`plugins/postgresql/rules.py`、PG 规则 JSON |
| 修改 PostgreSQL 指标口径 | `plugins/postgresql/metrics.py`；底层格式变化再检查 `plugins/postgresql/parsers.py` |
| 修改 PostgreSQL 图表 | `plugins/postgresql/charts.py`、`plugins/postgresql/chart_style.py` |
| 修改 PostgreSQL 报告契约 | `plugins/postgresql/report_adapter.py` |
| 修改 PostgreSQL 章节顺序和图表映射 | `plugins/postgresql/word_report.py` |

修改后至少运行：

```powershell
python -m unittest discover -s tests -v
```

更详细的文件关系见 `PROJECT_MAP.md`，变更检查范围见 `docs/change-impact-matrix.md`。

## 5. 数据安全

- 原始采集包、分析 JSON、SQL 文本和报告可能包含客户敏感信息。
- 未经明确确认，不上传公开仓库、不发送外部 API。
- `empty`、无权限、不支持、不适用和未采集不能转换为 `0` 或“正常”。
- LLM 只能改写文字，不得更改事实、风险、评分、严重性和证据。
