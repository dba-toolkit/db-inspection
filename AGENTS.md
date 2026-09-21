# AI/自动化维护规则

在修改本项目之前，必须先阅读：

1. `docs/PROJECT_MAP.md`
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
- **OS 层的指标键、阈值、判据和 rule_id 只能从 `inspection_core/system_checks.py` 读取。** 插件不得再自行声明一份 OS 阈值，也不得新增第五种 rule_id 拼写；判定统一走 `os_pressure_report()`（窗口选择 + 逐条判定一次拿到），插件只声明自己评价哪几条（`include=`）。旧拼写登记在 `RETIRED_RULE_IDS`，方向只有「旧 → 规范」一种（读取历史报告用），不存在把规范名翻译回插件的反向函数。OS 画图一律走 `inspection_core/charts/`（规格 + 渲染），插件只保留库专属图表。
- **两库采集形态不一致时，在公共层归一，不要在插件里各绕一次。** 已归一的例子：SAR CPU 列名 `%usr`/`%sys` vs `%user`/`%system`（`SAR_CPU_ALIASES`）、`time_evidence` 缺失时按时间戳自带偏移定展示时区、内存总量缺 `host_identity` 时从实时采样或 `kbmemused/%memused` 反推。新发现的差异照此办理，并补一条覆盖两种形态的测试。
- 新增采集项或分析项后，用 `python tools/coverage_audit.py <采集包>` 复核「无人提及 / 仅声明未读取」清单：采集了却没人读的字段要么补进分析、要么从采集端移除，不允许长期静默堆积。
- **规则包里声明的每一个阈值键，都必须有代码真的去请求它。** 请求形态各库不同（MySQL/Oracle 是 `self._threshold(rule, "key", default)`，PG 是内层函数 `threshold("RULE.ID", "key", default)`），所以按「调用里出现的小写字符串字面量」判定，不能按固定参数位置。死键的后果是隐性的：运维照规则包调一个数字，代码不看它，**改完没效果也不报错**。三种情形一律收口——没人读的键删掉；代码硬编码的常量改成读声明键；代码请求了但包里没声明的键必须在包里补上（否则实际生效的是代码默认值）。`tests/test_rule_pack_config.py` 会拦住复活，改规则包后跑它。
- **二级严重度（告警档 + 严重档）只有走 `severity_override` 才写得通。** `_evaluate` / `evaluate` 的 severity 默认取规则包 JSON 里写死的那个值，不传 override 时二级档位无处表达——配在包里也只是个没人读的数字。要分级就在判定处传 `severity_override=<命中的档位 or None>`。注意 severity 一变会连锁：findings 按严重度重排（`oracle_rules.run()` 结尾的 `_findings.sort`）并统一重编号、`comprehensive_conclusions` 跟着位移、健康分（severity 计数加权 `critical:20/high:10/medium:3/low:1`）跳动，**必须刷新该库基线**。`tests/test_severity_tiers.py` 守门，写新接线时照它的形状补一条「改旋钮行为跟着变」的用例。

## 分析缺口的推进节奏

- **不要凭单个采集包决定补什么分析。** 单包里"无人提及"的字段多数是客户环境偶然性（未启用 MGR、无分区表、无业务 Schema）。先积累**至少 3 个不同客户**的包，各自跑 `tools/coverage_audit.py` 后**取交集**；反复出现的缺口才动手补，只在单包出现的先记录、不实现。
- **未消费不等于必须消费。** 确属取证留存的字段，写明豁免理由即可，不要为把清单清零而强行补分析。
- **声明式规则引擎暂不接入流水线**（`inspection_core/declarative_rules.py` + `rules/<db>.yaml`）。触发条件：覆盖度清单连续 3 个包基本稳定，且出现「想调规则但不想改 Python」的实际需求 ≥ 2 次。迁移必须走影子模式（两种规则同时执行、只比对结果、不产出两份 finding）。
- **新增报告章节前先确认跨库一致性。** `section_order` 各库独立，但四库结构差异是负债；新增库专属章节要能说清必要性，不接受"为了展示内部状态"。

## 目录约定（整理后固定）

- 采集脚本源码放 `inspection/`（仓库内目录可自由组织，位置不是发布约束）；真正的约束是**发布物必须仍是单个文件、可独立拷到数据库服务器直接运行**——不得拆成多文件发布，不得要求客户机保留目录结构或安装额外运行时。
- **采集脚本不得依赖自身所在目录。** 输出位置一律由 `--output-dir` 或默认值（`/var/tmp`）决定，不得用 `$(dirname "$0")` 推导产物路径。这是"脚本可以在仓库里任意摆放"的前提；搬迁前必须确认这条成立，搬迁后不得引入新的自引用。
- 分析入口只有根目录 `analyze.py`，报告入口只有根目录 `generate_report.py`；**不要再新增根级入口脚本**，各库分析器一律放 `plugins/<db>/`（MySQL 是 `analyzer.py` 编排本体，其余三库另有 `cli.py` 命令行适配层）。
- 数据库专属的规则配置、图表样式、章节构造放 `plugins/<db>/`；跨库共用能力放 `inspection_core/`。
- 品牌与资源默认值放 `config/` 与 `assets/`，不要再散落在根目录。
- 过程性文档放 `docs/`，采集脚本自测放 `tests/selftest/`；根目录不放一次性清单、自测输出和手工备份。

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
10. 抽取公共 OS 分析与画图层（`inspection_core/charts/` + `system_checks.py`），按 ②MySQL → ③Oracle → ④PG → ⑤判定/阈值/rule_id 收口 的顺序接线。第①②③④⑤步均已完成（Oracle 与 PG 这步都改动了 OS 图表集合与文件名，第⑤步改动了三库的 OS rule_id 与报告附录，属预期的交付物变更，见 `docs/PROJECT_MAP.md` §22.7 / §22.9 / §22.11）；每一步都要单独回归，SQL Server 不并入。
11. 规则包配置键的消费面收口（第⑥步，已完成）：清掉 6 个没有任何代码路径请求的死键，并把这类检查固化成 `tests/test_rule_pack_config.py`。输出零变化（三库 PNG 逐字节相同），基线无需刷新。见 §22.13。

任何代码重构完成后，都必须使用 `tests/baselines/mysql/current/` 对比关键统计和报告模型差异。默认运行 `tests/compare_analysis_outputs.py` 检查业务数据；图表渲染改动另加 `--strict-charts` 检查。
