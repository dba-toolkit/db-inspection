# 统一契约说明

这些 Schema 是目标架构 v1 设计，不代表当前分析器已经原生输出该格式。

| 文件 | 用途 |
|---|---|
| `collection-package-v1.schema.json` | adapter 输出的统一采集包语义 |
| `collection-facts-v1.schema.json` | 分析层唯一读取的标准事实模型（static/sysctl/os_perf/sar/db） |
| `rule-model-v1.schema.json` | 每条规则的统一评价结果 |
| `report-model-v1.schema.json` | 公共 Word/其他 renderer 的唯一输入 |
| `examples/*.example.json` | 可通过对应 Schema 校验的最小完整示例 |

`collection-package-v1` 面向“原始采集包”（含 checks/artifacts/integrity），`collection-facts-v1` 面向“归一化事实”（分析层只认这一份）。现有四种采集包先由 adapter 转成 `collection-facts-v1`，后续新采集器可直接原生输出该格式。

## 迁移原则

1. 现有 MySQL/Oracle/PostgreSQL/SQL Server 原始包不直接要求符合新 Schema。
2. 每个数据库先实现 adapter，将旧格式转换为 collection contract。
3. 公共 rule engine 输出 rule model。
4. presentation builder 输出 report model。
5. 旧报告模型在公共 renderer 上线前继续保留。

## 敏感数据

契约允许保存完整主机、账号、Schema、SQL 摘要和路径。项目当前内部基线不脱敏，但这些数据不得未经确认上传公共仓库、外部服务或 LLM API。
