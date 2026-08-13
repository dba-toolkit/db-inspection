# AI/自动化维护规则

在修改本项目之前，必须先阅读：

1. `PROJECT_MAP.md`
2. `docs/architecture-current.md`
3. `docs/check-catalog.yaml`
4. `docs/change-impact-matrix.md`
5. `docs/baseline-inventory.md`

## 变更规则

- 不得把“未采集、无权限、不支持、不适用、空结果”转换为 0 或正常。
- 采集器只采集事实，不在客户服务器上执行整改或风险判断。
- LLM 不得改变事实、指标、采集状态、规则状态、严重性、评分或证据。
- 修改采集 SQL、字段、文件名或 item_id 前，先检查 `docs/check-catalog.yaml` 的消费者。
- 修改报告章节时，同时检查报告模型和 Word 生成器的目录/排序。
- 新增规则必须有唯一 rule_id、配置、执行逻辑和边界测试。
- 删除检查项必须先标记废弃并说明替代项，不能让旧采集包静默误判。
- 原始采集包和基线输出含敏感信息，未经明确确认不得上传、公开提交或发送外部 API。
- 保留用户已有文件和输出；不要覆盖原始采集包。
- 公共采集模块只接收跨数据库能力；数据库 SQL、许可和专属字段留在插件。
- 采集器源码可以模块化，但发布物必须继续提供四个独立、可单文件运行的入口。
- OS 指标必须证明属于数据库目标主机；远程采集端主机数据不得参与目标数据库资源评价。

## 当前重构顺序

1. 先补齐基线和契约测试。
2. 抽离公共模型，消除 `rules.py` 与分析器的循环依赖。（已完成）
3. 拆出 MySQL package adapter 和 metrics。（已完成）
4. 拆出报告模型构造。（已完成）
5. 拆出规则编排和图表生成。（已完成）
6. 抽取公共 Word 引擎和 MySQL Word Profile。（已完成）
7. 在不改变事实与规则的前提下，版本化升级专业报告结构和版式。（已完成）
8. 接下来依次接入其他数据库的 Word Profile 和报告契约适配器。
9. analyzer adapter 稳定后，按 `docs/collector-target.md` 分阶段抽取公共采集层。

任何代码重构完成后，都必须使用 `tests/baselines/mysql/current/` 对比关键统计和报告模型差异。默认运行 `tests/compare_analysis_outputs.py` 检查业务数据；图表渲染改动另加 `--strict-charts` 检查。
