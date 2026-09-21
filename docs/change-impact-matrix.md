# 变更影响矩阵

> 目的：修改前先判断影响范围，修改后按最小充分集合回归。

## 1. 当前实现的影响矩阵

| 变更类型 | 必查文件 | 必查输出 | 最低验证要求 |
|---|---|---|---|
| 调整现有规则阈值 | `plugins/mysql/inspection_rules.json`、对应 `plugins/mysql/rules.py` 方法 | `analysis.json`、`report_model.json` | 阈值边界值、缺失值、正常值、触发值各一例 |
| 新增纯展示 SQL | 采集脚本、MySQL adapter、`plugins/mysql/presentation.py` | `collection_status.json`、报告 item、Word | 有数据、空结果、无权限、不支持四种状态 |
| 新增风险规则 | 采集脚本、`plugins/mysql/metrics.py`、`plugins/mysql/rules.py`、规则 JSON、报告章节 | 规则评价、风险登记册、整改计划 | 规则 ID 唯一；未采集不得判正常 |
| 修改 SQL 输出列 | 采集脚本、`plugins/mysql/package_adapter.py`、`plugins/mysql/metrics.py`、规则、报告 item | JSON 和 Word | 旧采集包仍可分析，或明确拒绝不兼容版本 |
| 删除检查项 | 采集脚本、分析器、规则配置、报告 item、README | 覆盖矩阵、采集质量 | 先废弃再删除；旧包不能静默误判 |
| 修改采集文件名 | 采集脚本、manifest、分析器路径、证据引用 | 完整性校验、报告来源 | 必须提供兼容别名或提升契约主版本 |
| 修改评分 | 分析器 health summary、规则严重性权重、报告摘要 | 健康评分、风险数量 | 数据质量与健康评分必须分开 |
| 修改 MySQL 图表 | `plugins/mysql/charts.py`（仅 QPS/TPS、连接与线程两张） | chart metadata、PNG、`.docx` | `--strict-charts`；同环境PNG数量、名称和指纹可解释 |
| 修改公共 OS 图或 OS 判定 | `inspection_core/charts/`（`style`/`history`/`specs`/`render`）、`inspection_core/system_checks.py` | 四库 OS 图 PNG、OS 资源表、OS 规则评价（rule_id / 阈值 / 理由 / 置信度） | 与改动前逐字节/逐字段对拍；受影响库各回归一次 |
| 修改公共 Word 样式或布局 | `inspection_core/word_engine.py`、必要时对应库的 `plugins/<db>/word_report.py` | 四库 `.docx` | 旧版内容哈希；专业版事实守恒、先事实后分析；表格几何、标题、图片、可访问性及逐页渲染检查 |
| 修改 MySQL Word 映射 | `plugins/mysql/word_report.py` | MySQL `.docx` | 契约、章节顺序、图表数量、目录编号、两种布局入口一致 |
| 修改 PostgreSQL 旧模型适配 | `plugins/postgresql/report_adapter.py` | PG 标准 `report_model.json`、`.docx` | 旧模型继续保留；评分、规则状态、风险事实守恒；缺失证据不变正常 |
| 修改 PostgreSQL Word 映射 | `plugins/postgresql/word_report.py` | PostgreSQL `.docx` | 14 章节、6 张图（4 张公共 OS 图 + 2 张 PG 专属）、目录编号、专业版分页和逐页视觉检查 |
| 修改 PostgreSQL 采集包结构或文件名 | `plugins/postgresql/package_adapter.py` | PG `PackageContext` 及全部下游 | 在 adapter 内兼容旧版本；不得在指标、规则或 Word 中直接解包；`history/sar_*.csv` 必须走 `parse_sadf` |
| 修改 PostgreSQL 规则编排 | `plugins/postgresql/rule_provider.py`、`plugins/postgresql/rules.py`、PG 规则 JSON | PG 规则评价、风险登记册 | Provider 只负责编排；规则事实和阈值仍须做边界测试 |
| 修改 PostgreSQL 指标算法 | `plugins/postgresql/metrics.py` | PG 规则输入、分析 JSON、报告结论 | 不修改包读取或 Word；回归51项指标及36条规则；SAR 峰值与有效覆盖一律取公共层 |
| 修改 SAR 历史解析或有效覆盖口径 | `inspection_core/charts/history.py`、`inspection_core/sampling.py`、`inspection_core/tabular.py::parse_sadf` | 四库系统指标与图表 | 缺失、空结果和解析失败必须保持可区分；插件不得再自建一份 SAR 解析 |
| 修改 PostgreSQL 图表 | `plugins/postgresql/charts.py`（系统四张走 `inspection_core/charts/`） | PG `charts/*.png` | 业务 JSON 常规回归外，必须增加 `--strict-charts`；文件名必须带实例前缀 |
| 修改 Oracle 报告契约适配 | `plugins/oracle/report_adapter.py` | Oracle `report_model_standard.json`、`.docx` | 不重算评分/规则/严重性；item_id 唯一；纯事实项归一为 normal，无记录项保留 not_evaluated；finding 保留 source_severity |
| 修改 Oracle Word 映射 | `plugins/oracle/word_report.py` | Oracle `.docx` | 9 章节、52 项、8 张图（4 张公共 OS 图 + 4 张 Oracle 专属）、目录编号、专业版分页和逐页视觉检查 |
| 修改公共 OS 图规格或渲染 | `inspection_core/charts/` | 四库 `charts/*.png`、`.docx` | 规格逐字段对拍 + PNG 指纹可解释；MySQL 侧对拍脚本须仍逐字节相同 |
| 修改 OS 指标键/阈值/判据/rule_id | `inspection_core/system_checks.py` | 四库规则评价与报告结论 | 阈值边界、缺失值、退役 rule_id 解析各一例；OS 阈值只从 `DEFAULT_THRESHOLDS` 读，插件规则包不得再声明，判定必须走 `os_pressure_report()`，`OsThresholdOwnershipTests` 6 项守门 |
| 新增/改名/删除规则包阈值键 | 三份 `plugins/<db>/inspection_rules*.json`、对应 `<db>_rules.py` 或 `rules.py` | 规则评价、报告结论 | 键必须真被代码请求：新增的要接线、代码硬编码的常量要改成读键、删除的要确认无引用。`tests/test_rule_pack_config.py` 6 项守门（声明必被请求 / 请求必有声明 / 已收口死键不得复活），改完跑它；删死键属零输出变化，不必刷基线 |
| 启用 severity 二级分级（二级阈值） | 三库 `_evaluate`/`evaluate` 的 `severity_override` 形参 + `plugins/oracle/oracle_rules.py` 的接线点 | finding 的 `severity`、健康分、finding 编号与顺序、风险登记册、优化计划 P1/P2 分布 | 不传 `severity_override` 时零输出变化（MySQL/PG 目前即此状态）。一旦某库启用，severity 变化会连锁：findings 按严重度重排并重编号、`comprehensive_conclusions` 位移、健康分（severity 计数加权 `critical:20/high:10/medium:3/low:1`）跳动，**必须刷新该库基线**。`tests/test_severity_tiers.py` 11 项守门，核心是「改旋钮行为跟着变」 |
| 修改 SQL Server 报告契约适配 | `plugins/sqlserver/report_adapter.py` | SQL Server `report_model_standard.json`、`.docx` | 以 sections 为唯一章节来源；headers+二维 rows 归一；success→ok；item_id 唯一；不重算评分/规则/严重性；owner/window/verification 保留到整改计划 |
| 修改 SQL Server Word 映射 | `plugins/sqlserver/word_report.py` | SQL Server `.docx` | 12 章节、27 项、8 张技术图表映射、目录编号、专业版分页和逐页视觉检查 |
| 修改报告章节 | `plugins/mysql/presentation.py`、`plugins/mysql/word_report.py` | `report_model.json`、目录、页码 | 章节数、item 数和风险引用一致；编号动态生成 |
| 修改 LLM prompt | `enhance_report.py` | 增强前后 JSON | 事实、评分、状态、证据字段完全不变 |
| 修改采集安全策略 | 采集脚本、security scan、README | tar.gz、manifest、日志 | 密码/token 不落包；SQL 文本策略明确 |

## 2. 未来目标状态

完成契约化后，影响范围应收敛为：

| 变更类型 | 目标修改范围 |
|---|---|
| 新增纯展示 SQL | 数据库插件中的 check 定义、SQL 和 fixture；公共分析器及 Word 不改 |
| 新增普通阈值规则 | 规则 YAML/JSON 和测试；复杂派生指标才增加插件代码 |
| 修改 Word 主题 | 公共 renderer/theme；采集和分析不改 |
| 修改字段 | 对应 check schema 和版本适配器；规则和报告继续使用标准字段 |
| 接入新数据库 | 新增 collector/adapter/checks；复用规则协议和 Word 引擎 |

## 3. 新增检查项的完成定义

一个检查项只有同时满足以下条件才算完成：

1. 有稳定、不可随意复用的 `check_id`。
2. 说明适用版本、权限和性能影响。
3. 声明输出字段、类型、单位和 schema_version。
4. 定义成功、空结果、无权限、不支持、不适用和失败的含义。
5. 有按内部敏感资料管理的完整 fixture。
6. 如有判断，定义阈值、严重性、证据、影响和建议。
7. 报告能先展示事实，再展示分析；未采集不会显示为 0 或正常。
8. 通过 contract、规则和报告回归检查。

## 4. 删除检查项的完成定义

1. 先标记 deprecated，并记录替代 check_id。
2. 至少保留一个契约兼容周期。
3. 分析器能识别旧采集包。
4. 规则覆盖矩阵明确显示 retired/not_applicable，而不是 passed。
5. README、项目录和样例同步更新。
