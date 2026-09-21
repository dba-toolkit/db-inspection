# 数据库巡检工具（MySQL / PostgreSQL / Oracle / SQL Server）

四库统一巡检：**4 个采集脚本 + 1 个分析入口 + 1 个 Word 生成入口**。分析器按数据库拆成 `plugins/<db>/` 插件，公共核心 `inspection_core/` 和 Word 引擎四库复用。

## 1. 环境准备

推荐用独立虚拟环境：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

验证：

```powershell
python -c "import docx, matplotlib, PIL, yaml; from zoneinfo import ZoneInfo; ZoneInfo('Asia/Shanghai'); print('deps ok')"
```

最后一截专门验时区库：Linux 靠系统 tzdata，Windows 靠 `requirements.txt` 里的 `tzdata` 包。缺了它图轴时区会静默降级成 `UTC+08:00`（数值没错，但报告里的时区名没了），只有真的构造一次 `ZoneInfo` 才会报出来。

如果不想建虚拟环境，也可以直接用本机已装好依赖的 `D:\python\python.exe`（本项目开发时所用）。`tools/run.ps1` 会优先使用 `.venv`，其次 `D:\python\python.exe`，最后系统 `python`。

报告品牌信息（公司、编制人、复核人、logo）统一在 `config/report_config.json` 中配置；生成报告时的命令行参数会覆盖该默认值。

## 2. 最短使用流程

### 第一步：在数据库服务器采集（4 个独立单文件脚本）

```bash
# Linux：MySQL / PostgreSQL / Oracle（下面是仓库内路径；脚本单文件拷到数据库服务器后直接执行，不要带 inspection/）
bash inspection/mysql_inspection_standard.sh ...
bash inspection/pg_inspection_standard.sh ...
bash inspection/oracle_inspection.sh ...

# Windows：SQL Server
powershell -File inspection/sqlserver_inspection.ps1 -Server 10.0.0.10 ...
```

采集端只收集事实，不做评分、风险判断或整改。

> 这四个脚本的源码放在 `inspection/`。**发布物必须保持单文件**——因为它们要能单独拷到数据库服务器上直接运行（零依赖）。仓库内的目录位置只是仓库组织，不是发布约束；单文件形态才是。另外它们不得依赖自身所在目录，输出路径一律走 `--output-dir` 或默认 `/var/tmp`。

### 第二步：本地分析（1 个入口，自动识别数据库类型）

```powershell
# 单实例
python analyze.py <采集包.zip/tar.gz> -o output\<实例名>

# 多实例 / 主从：把同一套环境的采集包一起传，拓扑由分析器自动合并
python analyze.py <主包> <从包1> <从包2> -o output\<环境名>
```

多包输入时，分析器会按 `server_uuid` / `source_uuid` / `source_host` 自动合并出节点与复制关系，
输出 `topology`（`mode` / `nodes` / `edges`），报告 2.2 节据此渲染架构拓扑。
也可以直接传一个**目录**（目录下的 `*.tar.gz` 全部纳入），此时需显式加 `--db-type`。
MySQL / PostgreSQL / Oracle 支持多包；SQL Server 一次只接受一个采集包。

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
python generate_report.py output\<实例名> --output output\<实例名>\报告.docx --customer "客户名称" --target "巡检对象" --company "公司名称" --author "编制人" --reviewer "复核人" --logo assets/logo.png
```

默认生成 `professional` 专业版；需要旧版结构加 `--layout legacy`。

## 3. 四库支持情况

| 数据库 | 采集器（`inspection/`） | 分析插件 | Word |
|---|---|---|---|
| MySQL | `mysql_inspection_standard.sh` | `plugins/mysql/` | 支持 |
| PostgreSQL | `pg_inspection_standard.sh` | `plugins/postgresql/` | 支持 |
| Oracle | `oracle_inspection.sh` | `plugins/oracle/` | 支持 |
| SQL Server | `sqlserver_inspection.ps1` | `plugins/sqlserver/` | 支持 |

## 4. 目录结构

```text
# ── 采集端：4 个独立单文件脚本。源码放 inspection/，发布物必须能单文件运行
inspection/
  mysql_inspection_standard.sh
  pg_inspection_standard.sh
  oracle_inspection.sh
  sqlserver_inspection.ps1

# ── 分析与报告入口：各自唯一
analyze.py                        # 统一分析入口，按数据库分发到插件
generate_report.py                # 统一 Word 入口，按 generator_contract 分发

inspection_core/                  # 公共模型、安全数值、包读取、统计、声明式规则、Word 引擎
plugins/
  mysql/                          # MySQL 插件（analyzer.py 是编排入口）
    analyzer.py                   #   分析流程编排
    package_adapter.py metrics.py rules.py presentation.py charts.py
    word_report.py
    inspection_rules.json         #   MySQL 24 条规则阈值
  postgresql/                     # PG 插件（cli.py 是命令行适配层，analyzer.py 是编排）
  oracle/                         # Oracle 插件（cli.py / analyzer.py 同上）
  sqlserver/                      # SQL Server 插件（cli.py / analyzer.py 同上）

contracts/                        # collection / rule / report 三类契约 schema 与样例
docs/                             # 项目地图、架构现状与目标、变更影响矩阵、采集器契约
  PROJECT_MAP.md                  #   入口文档，改代码前先读
  mysql-collector-interface.md    #   mysql 采集脚本的 Python 对接契约
  mysql-collector-fix-plan.md     #   mysql 采集脚本改造清单与进度
rules/                            # 声明式规则 YAML 示例（当前未被任何代码加载）
tests/
  selftest/                       # MySQL 采集脚本改动自测（bash，只抽函数配桩，不连库）
config/report_config.json         # 报告品牌默认值
assets/logo.png                   # 默认 Logo
tools/run.ps1                     # 一键「分析 + 出报告」封装
tools/coverage_audit.py           # 采集包消费面自检（采了什么 / 读了多少 / 什么没人读）
requirements.txt
.gitignore
AGENTS.md                         # AI/自动化维护规则
```

依赖方向单向：公共核心不 import 数据库插件；采集、分析、Word 互不反向调用。

## 5. 常用修改点

| 需求 | 主要修改位置 |
|---|---|
| 修改 MySQL 阈值 | `plugins/mysql/inspection_rules.json`（**不含 OS 压力类**，见下一行） |
| 修改 OS 压力阈值/判据/规则名 | `inspection_core/system_checks.py` 的 `DEFAULT_THRESHOLDS` / `CANONICAL_RULE_IDS`；插件规则包与判定代码都不许再声明一份 |
| 新增/改名/删除规则包阈值键 | 对应 `plugins/<db>/inspection_rules*.json` + 规则实现。键必须真的被代码请求（`tests/test_rule_pack_config.py` 守门）：没人读的键是死配置，改了没效果也不报错 |
| 修改 severity 二级阈值（告警档 / 严重档） | 判定处必须传 `severity_override`（三库 `_evaluate` / `evaluate` 已有该形参）；只改规则包里的数字不会生效。severity 一变会连锁重排 finding 编号、改变健康分，需刷新该库基线 |
| 新增 MySQL 规则 | `plugins/mysql/inspection_rules.json` + `plugins/mysql/rules.py` + 测试 |
| 修改报告章节/措辞 | `plugins/<db>/presentation.py` |
| 修改图表 | `plugins/<db>/charts.py`；四张 OS 图（CPU/内存/磁盘/网络）在 `inspection_core/charts/`，配色在 `inspection_core/charts/style.py` |
| 修改公共 Word 版式 | `inspection_core/word_engine.py` |
| 修改 Word 章节顺序/图表映射 | `plugins/<db>/word_report.py` |
| 修改采集 SQL/字段 | `inspection/` 下对应采集脚本 + 对应 `plugins/<db>/package_adapter.py` / `metrics.py` |
| 修改分析流程编排 | `plugins/mysql/analyzer.py`，或 `plugins/<db>/analyzer.py` |
| 修改报告品牌默认值 | `config/report_config.json` |

声明式规则引擎已在 `inspection_core/declarative_rules.py`，未来加简单阈值规则可用它加 `rules/*.yaml`，不必写 Python。

## 6. 测试与回归

```powershell
python -m unittest discover -s tests -v
```

MySQL 采集脚本的自测（只抽取被测函数 + 配桩，不连数据库、不改生产脚本）：

```bash
bash tests/selftest/mysql_collector_batch1.sh
bash tests/selftest/mysql_collector_batch2.sh
```

敏感采集包、`output/`、`analysis_output*/`、`tests/baselines/` 已加入 `.gitignore`，不入库。**MySQL 相关夹具测试需要自备真实采集包** `mysql_inspection_v1_<host>_<port>_<ts>.tar.gz` 放在仓库根；没有它时这几个用例会报「registered MySQL baseline package is missing」，其余用例不受影响。

改完代码后建议跑全量测试并 `git commit`。

## 7. 数据安全

- 原始采集包、分析 JSON、SQL 文本和报告可能包含客户敏感信息，未经确认不上传公开仓库或外部 API。
- `empty`、无权限、不支持、不适用、未采集不得转换为 `0` 或"正常"。
- LLM 只能改写文字，不得更改事实、风险、评分、严重性和证据。

## 8. 升级注意（旧命令已失效）

分析器与兼容壳已收进插件目录，旧的直接调用方式不再可用，请统一走 `analyze.py`：

| 旧命令 | 现在 |
|---|---|
| `python analyze_inspection_v2.py <包> --output <dir>` | `python analyze.py <包> -o <dir>` |
| `python analyze_postgresql.py <包> -o <dir>` | 同上（自动识别，或 `--db-type postgresql`） |
| `python analyze_oracle.py <包> --output <dir>` | 同上（`--db-type oracle`） |
| `python analyze_sqlserver.py <包> --output <dir>` | 同上（`--db-type sqlserver`） |
| `import rules` | `from plugins.mysql import rules` |

生成 Word 的入口本来就是 `generate_report.py`，命令未变，仅 `--logo` 默认值改为 `assets/logo.png`（相对路径现在按项目根解析，不再依赖当前工作目录）。
