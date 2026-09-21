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
- **文件系统容量只看持久化挂载点，判据是 `inspection_core.system_checks.is_persistent_fstype(fstype)`。** 指标层（挑最高使用率的那一行）和报告表格（列出挂载点）必须调同一个判据，否则会出现"用被隐藏的挂载点报出来的风险"：光驱 `/dev/sr0`（iso9660）恒为 100% 使用、可用 0 字节，既不是数据目录也无法扩容，把它算进"文件系统使用率过高"就是给客户报一个不存在的风险，还会连带生成一条没法执行的整改项。`tmpfs`、`devtmpfs`、`proc`、`hugetlbfs` 同属此类。**`nfs`/`cifs` 等网络文件系统不在黑名单里**——"数据目录落在网络文件系统"正是报告要报的风险，白名单会把它一并藏掉。
- **拓扑里"上游指向自身"的上游声明不是边。** `source_uuid` 等于自己、或 `source_host` 就是本机时，该条复制状态行是残留/未启动的通道，必须丢弃并记入 `self_reference_edges`（也不能混进 `unresolved_edges`）——否则报告的关系表会出现"源节点 = 目标节点"这种客户一眼能看出的错。构建边时**按主机名/IP 匹配候选节点必须排除自身**。
- **角色值分两个字段：`role_observed` 是采集端原始观测，只能留档不能改写；`role_effective` 是分析侧依证据校正后的值，报告渲染用这个。** 校正必须基于跨包证据（例：两个下游节点的 `source_uuid` 都指向它，而它自己的上游声明自指 → 判为 `source`），并写 `role_note` 说明理由；**有真实上游的级联中间节点不得改判**。校正后要在拓扑表附近给出说明框——否则 2.2 节写 `source`、后文实例信息仍写观测值 `replica`，读者会以为报告自相矛盾。`tests/test_topology_master_slave.py` 守这几条。
- **检查明细的 `status` 和结论文案必须由采集数据算出来，不得写死。** 曾经有两个检查项常年写死 `not_applicable` + “本实例未发现业务 Schema”：`mysql.schemas` 的 `evidence` 却写着 `Schema 数量 18`，`mysql.capacity.risks` 的 `evidence` 干脆是硬编码字符串 `业务 Schema 0 个`，而同一实例的 `metrics.schema` 是「无主键表 94 / 非 InnoDB 表 1 / 冗余索引候选 156 / 未使用索引候选 309」，风险台账 R003–R007 报的全是这些对象 —— 报告第 3 章说“没有业务对象可评价”、第 5 章列出 94 张无主键表，客户一眼就能看出自相矛盾。同一批还撞到 `mysql.sql.digests` 写死“存在未使用索引计数的摘要”，而包内 `SUM_NO_INDEX_USED` 89 行全为 0。判定“有没有业务库”必须走 `business_schema_names()` + `MYSQL_SYSTEM_SCHEMAS` 的显式名单，不许按名字“看起来像不像系统库”猜。审报告的手法：**把每个检查项的 `conclusion` 与同一项的 `evidence`/表格行并排读**——结论说“没有”而证据里有数，就是写死的。
- **拓扑节点必须自带渲染所需的字段，顺序也由拓扑层定。** 2.2 节点表固定四列 `节点 | 地址 | 角色 | 版本`，直接按 `nodes` 顺序渲染，所以：`version` 必须挂在节点上（`instance_identity` 里有 `version` 但节点不带，渲染出来就是“未采集”，与封面/2.1 节的版本自相矛盾）；顺序必须在拓扑层排好（复制源端第一、其余按 IP 数值序），**不得依赖调用者传包的先后**——否则同样的三台机器换个传参顺序，报告就换了个样子。（PG 是在 `report_adapter` 里补 `version`/`role_observed`，MySQL 由 `topology()` 直接带上；新增库照 MySQL 的做法，让数据源头就完整。）
- **入口必须自检解释器依赖，缺 `matplotlib` 不许静默降级。** 图表渲染器优先 matplotlib、导入不到才退回 Pillow 手绘版，**退回不报错**——报告照常生成，只有打开 Word 才看得出来（X 轴末尾刻度叠在一起、Y 轴标签贴边）。实测同一份代码在 A 解释器出 matplotlib 图、在 B 解释器出降级图，报告里除 `charts[*].renderer` 外没有任何提示。`analyze.py` / `generate_report.py` 启动时调 `inspection_core.preflight.preflight_report_dependencies()`：缺 `python-docx`/`Pillow` 直接阻断并打印当前解释器路径与安装命令，缺 `matplotlib` 只告警放行。**发现图变丑，先看启动时有没有这行告警，再怀疑渲染代码。**

## 分析缺口的推进节奏

- **不要凭单个采集包决定补什么分析。** 单包里"无人提及"的字段多数是客户环境偶然性（未启用 MGR、无分区表、无业务 Schema）。先积累**至少 3 个不同客户**的包，各自跑 `tools/coverage_audit.py` 后**取交集**；反复出现的缺口才动手补，只在单包出现的先记录、不实现。
- **未消费不等于必须消费。** 确属取证留存的字段，写明豁免理由即可，不要为把清单清零而强行补分析。
- **声明式规则引擎暂不接入流水线**（`inspection_core/declarative_rules.py` + `rules/<db>.yaml`）。触发条件：覆盖度清单连续 3 个包基本稳定，且出现「想调规则但不想改 Python」的实际需求 ≥ 2 次。迁移必须走影子模式（两种规则同时执行、只比对结果、不产出两份 finding）。
- **新增报告章节前先确认跨库一致性。** `section_order` 各库独立，但四库结构差异是负债；新增库专属章节要能说清必要性，不接受"为了展示内部状态"。

## 目录约定（整理后固定）

- 采集脚本源码放 `inspection/`（仓库内目录可自由组织，位置不是发布约束）；真正的约束是**发布物必须仍是单个文件、可独立拷到数据库服务器直接运行**——不得拆成多文件发布，不得要求客户机保留目录结构或安装额外运行时。
- **采集脚本不得依赖自身所在目录。** 输出位置一律由 `--output-dir` 或默认值（`/var/tmp`）决定，不得用 `$(dirname "$0")` 推导产物路径。这是"脚本可以在仓库里任意摆放"的前提；搬迁前必须确认这条成立，搬迁后不得引入新的自引用。
- 分析入口只有根目录 `analyze.py`，报告入口只有根目录 `generate_report.py`；**不要再新增根级入口脚本**，各库分析器一律放 `plugins/<db>/`（MySQL 是 `analyzer.py` 编排本体，其余三库另有 `cli.py` 命令行适配层）。
- **统一入口的参数面必须是各库本体的超集。** `analyze.py` 只做分发，不许在分发时收窄能力：本体是 `nargs="+"` 就得多包透传，本体接受的参数入口也得能表达。已经踩过一次——统一入口按单包写，而 MySQL/PG/Oracle 本体都支持多包合并（多实例/主从拓扑），导致"能力明明在、命令却报 `unrecognized arguments`"，读文档和读报错都看不出来。**报错里出现 `unrecognized arguments` 时先怀疑壳，不要先怀疑本体。** 单包调用必须保持向后兼容；库型不匹配（如混传 MySQL + PG 包、SQL Server 传多包）在入口就拦下并给明确文案，不要丢给子进程去报 argparse 错。
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
