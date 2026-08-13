# 四种数据库能力对照与吸收决策

> 基于 2026-08-11 的真实采集包、分析输出和代码检查。目标不是评选“最好项目”，而是决定公共核心采用什么设计。

## 1. 真实模型差异

| 维度 | MySQL | Oracle | PostgreSQL | SQL Server | 统一决策 |
|---|---|---|---|---|---|
| 原始采集格式 | tar.gz，多 TSV/CSV/JSON/证据文件 | tar.gz，多表格、命令证据和 Oracle 专属输出 | tar.gz，多 TSV/CSV/JSON | zip，核心为单一 `snapshot.json` | 不强制统一物理布局，由 adapter 转成统一语义模型 |
| manifest | 文件大小和 SHA256 | 文件大小和 SHA256 | 文件大小和 SHA256 | 主要是文件清单，缺少同等级哈希语义 | 公共契约采用 MySQL/Oracle/PG 的完整性模型 |
| 采集状态 | 79 个状态记录，字段完整 | 包内未发现同结构 `collection_status.json`，分析器需推断部分状态 | 108 个状态记录，字段完整 | module status 位于 snapshot 内 | 统一为 check 级状态，Oracle/SQL Server 由 adapter 补齐 |
| 能力探测 | P_S、sys、锁、复制、日志等 | RAC、ASM、CDB/PDB、DG、AWR 许可等 | 版本、扩展、复制、系统能力 | 版本、Agent、Always On、深度检查等 | capability 是一等对象，规则必须声明适用条件 |
| 规则状态 | triggered/passed/not_evaluated/not_applicable | 实际样例含 triggered/passed/not_evaluated | 实际样例含 triggered/passed/not_evaluated | 只有 findings，没有完整规则覆盖台账 | 采用 MySQL/Oracle 的四态模型，所有规则都必须有评价记录 |
| 缺失值策略 | 明确 null 不得显示为 0 | 能披露未评价和证据不足 | 能披露未评价 | 部分格式化函数会把缺失转成 `-` 或 0 | 核心模型只保留 null；显示层决定中文占位符 |
| 报告 item | 33 项，`collection/display/analysis` 完整 | 48 项，但部分缺少稳定 item_id，analysis 字段不齐 | 14 项，结构最接近 MySQL | 27 项，常缺 item_id/analysis，表格采用 headers+二维 rows | 采用 MySQL/PG item 外壳，补充统一数据列定义和 rule links |
| 报告章节 | `inspection_sections` | `inspection_sections` | `inspection_model.sections` | 同时存在说明性 `inspection_sections` 和数据 `sections` | 统一为唯一 `sections`，禁止双重章节来源 |
| 风险模型 | finding + severity + facts + evidence | 与 MySQL 接近 | 与 MySQL接近 | 风险登记册管理字段最丰富 | 技术证据采用 MySQL/Oracle/PG，owner/window/verification 采用 SQL Server |
| 管理语义 | 健康、风险、整改优先级 | 覆盖矩阵、许可和限制较强 | 六领域结论、多节点 | 管理摘要、整改计划、验收字段较强 | 报告模型同时包含技术评价和管理闭环 |
| 图表 | matplotlib | matplotlib | matplotlib，可缺省 | Pillow，无 matplotlib 依赖 | 图表接口统一，backend 可替换，报告只引用 chart descriptor |
| LLM 增强 | 独立脚本 | 高度重复 | 高度重复 | 相似但模型不同 | 报告模型稳定后合并成公共增强器 |
| 多节点 | 支持多输入，当前基线单实例 | RAC/DG 语义复杂 | 显式支持主备/集群多包 | Always On 等 | target/node/topology 进入公共模型，不假设单实例 |

## 2. 需要立即吸收的优点

这些设计在 MySQL 第一次解耦时就实施：

1. **MySQL 的完整性和缺失值策略**：manifest 哈希、采集状态、null 保真。
2. **PostgreSQL 的依赖方向**：分析器依赖规则公共模型，不允许规则反向导入分析器。
3. **Oracle 的适用性和证据边界**：not_evaluated/not_applicable、许可、权限和能力不足必须显式披露。
4. **SQL Server 的管理闭环**：风险必须能够携带 owner、target_window、verification 和 status。
5. **MySQL/PG 的 item 外壳**：事实展示与分析判断分开。

## 3. 分阶段吸收的优点

| 能力 | 实施阶段 | 原因 |
|---|---|---|
| 公共 Word 组件 | 报告模型稳定后 | 先统一输入，再统一渲染，否则只会复制更多条件判断 |
| SQL Server 单 JSON 采集体验 | 各 collector 下一大版本 | 当前三套多文件包已有稳定能力，不宜为统一格式重写采集器 |
| Oracle RAC/ASM/CDB/PDB 专项模型 | Oracle adapter 接入时 | 应作为 extensions/capabilities，不污染所有数据库公共字段 |
| PostgreSQL 多节点合并 | PostgreSQL 第二个接入时 | 用来验证 target/node/topology 是否真正通用 |
| Pillow 图表 backend | 公共 chart API 完成后 | backend 是实现细节，不应进入报告契约 |

## 4. 不直接照搬的做法

- 不照搬 SQL Server 将大量业务域同时平铺在 report model 顶层的做法；统一使用 sections 和 extensions。
- 不照搬 PostgreSQL 当前缺少明确 `generator_contract/schema_version` 的报告顶层。
- 不保留 SQL Server 的双章节来源。
- 不保留 Oracle 报告 item 缺少稳定 item_id 的情况。
- 不把任何数据库的显示占位符 `-`、`未采集` 写入核心事实值。
- 不把规则 Python 方法继续绑定到具体 Analyzer 类。

## 5. 接入顺序

1. MySQL：实现公共模型和 adapter 的第一个版本。
2. PostgreSQL：验证同类数据库、多节点和不同 report envelope。
3. Oracle：验证复杂拓扑、许可、能力和大量不适用分支。
4. SQL Server：验证单 JSON collector、管理整改闭环和 Windows 采集端。

只有第二个数据库接入后，公共接口才能被认为“已验证”，不能仅凭 MySQL 自己宣布通用。

