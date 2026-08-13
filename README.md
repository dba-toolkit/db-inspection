# 数据库巡检工具（MySQL / PostgreSQL / Oracle / SQL Server）

四库统一巡检：**4 个采集脚本 + 1 个分析入口 + 1 个 Word 生成入口**。分析器按数据库拆成插件，公共核心和 Word 引擎四库复用。

## 1. 环境准备

推荐用独立虚拟环境：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

验证：

```powershell
python -c "import docx, matplotlib, PIL, yaml; print('deps ok')"
```

如果不想建虚拟环境，也可以直接用本机已装好依赖的 `D:\python\python.exe`（本项目开发时所用）。`run.ps1` 会优先使用 `.venv`，其次 `D:\python\python.exe`，最后系统 `python`。

报告品牌信息（公司、编制人、复核人、logo）统一在 `report_config.json` 中配置；生成报告时的命令行参数会覆盖该默认值。

## 2. 最短使用流程

### 第一步：在数据库服务器采集（4 个独立单文件脚本）

```bash
# Linux：MySQL / PostgreSQL / Oracle
bash mysql_inspection_standard.sh ...
bash pg_inspection_standard.sh ...
bash oracle_inspection.sh ...

# Windows：SQL Server
powershell -File sqlserver_inspection.ps1 -Server 10.0.0.10 ...
```

采集端只收集事实，不做评分、风险判断或整改。

### 第二步：本地分析（1 个入口，自动识别数据库类型）

```powershell
python analyze.py <采集包.zip/tar.gz> -o output\<实例名>
```

主要输出：

```text
output/<实例名>/
├─ analysis.json          完整事实、指标、规则和风险
├─ report_model.json      Word 生成器的唯一输入
├─ llm_input.json         可选文字增强输入
├─ analyzer_status.json   各阶段状态
├─ charts/                报告图表
└─ ...
```

### 第三步：生成 Word（1 个入口，自动识别报告契约）

```powershell
python generate_report.py output\<实例名> --output output\<实例名>\报告.docx --customer "客户名称" --target "巡检对象" --company "公司名称" --author "编制人" --reviewer "复核人" --logo logo.png
```

默认生成 `professional` 专业版；需要旧版结构加 `--layout legacy`。

## 3. 四库支持情况

| 数据库 | 采集器 | 分析插件 | Word |
|---|---|---|---|
| MySQL | `mysql_inspection_standard.sh` | `plugins/mysql/` | 支持 |
| PostgreSQL | `pg_inspection_standard.sh` | `plugins/postgresql/` | 支持 |
| Oracle | `oracle_inspection.sh` | `plugins/oracle/` | 支持 |
| SQL Server | `sqlserver_inspection.ps1` | `plugins/sqlserver/` | 支持 |

## 4. 目录结构

```text
mysql_inspection_standard.sh / pg_inspection_standard.sh /
oracle_inspection.sh / sqlserver_inspection.ps1     # 4 个采集入口
analyze.py                                           # 统一分析入口
generate_report.py                                   # 统一 Word 入口
analyze_inspection_v2.py / analyze_postgresql.py
analyze_oracle.py / analyze_sqlserver.py             # 各库分析器
inspection_core/                                     # 公共模型、Word 引擎、声明式规则引擎
plugins/mysql/  plugins/postgresql/
plugins/oracle/ plugins/sqlserver/                   # 四库插件
contracts/  docs/  tests/  rules/  collectors/       # 契约、文档、测试、规则示例、采集层参考
inspection_rules.json                                # MySQL 24 条规则阈值
```

依赖方向单向：公共核心不 import 数据库插件；采集、分析、Word 互不反向调用。

## 5. 常用修改点

| 需求 | 主要修改位置 |
|---|---|
| 修改 MySQL 阈值 | `inspection_rules.json` |
| 新增 MySQL 规则 | `inspection_rules.json` + `plugins/mysql/rules.py` + 测试 |
| 修改报告章节/措辞 | `plugins/<db>/presentation.py` |
| 修改图表 | `plugins/<db>/charts.py` |
| 修改公共 Word 版式 | `inspection_core/word_engine.py` |
| 修改 Word 章节顺序/图表映射 | `plugins/<db>/word_report.py` |
| 修改采集 SQL/字段 | 对应采集脚本 + 对应 `package_adapter.py` / `metrics.py` |

声明式规则引擎已在 `inspection_core/declarative_rules.py`，未来加简单阈值规则可用它加 `rules/*.yaml`，不必写 Python。

## 6. 测试与回归

```powershell
python -m unittest discover -s tests -v
```

敏感采集包、`output/`、`tests/baselines/` 已加入 `.gitignore`，不入库。改完代码后建议跑全量测试并 `git commit`。

## 7. 数据安全

- 原始采集包、分析 JSON、SQL 文本和报告可能包含客户敏感信息，未经确认不上传公开仓库或外部 API。
- `empty`、无权限、不支持、不适用、未采集不得转换为 `0` 或“正常”。
- LLM 只能改写文字，不得更改事实、风险、评分、严重性和证据。
