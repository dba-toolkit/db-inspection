# 原型目录（参考设计，未接入正式流程）

本目录用于验证“声明式规则引擎”的设计，是纯函数原型。

- `rule_engine.py`：规则引擎原型，不 import、也不被任何现有分析/报告代码引用。
- `test_rule_engine.py`：用 `contracts/examples/collection-facts-v1.example.json` + `rules/mysql.yaml` 跑通演示。

当前正式运行仍使用 `plugins/*/rules.py` 与 `inspection_rules.json`，本目录不参与生产流程，可随时删除。
