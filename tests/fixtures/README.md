# 回归样例说明

## 1. 目录目的

本目录用于保存四种数据库的采集包契约样例。完整分析输出和 Word 基线放在 `tests/baselines/`。

```text
tests/fixtures/mysql/
tests/fixtures/oracle/
tests/fixtures/postgresql/
tests/fixtures/sqlserver/
tests/baselines/<database>/<baseline-name>/
```

## 2. 数据策略

根据项目所有者决定，内部基线允许保留完整主机名、IP、账号、Schema、SQL 摘要、路径和日志内容，以保证分析和 Word 展示完整。

约束：

- 原始包和基线输出视为敏感运维资料。
- 未经明确确认不得提交到公开仓库、上传公共服务或对外发送。
- LLM 增强不是回归测试前置条件；默认不向外部 API 发送基线内容。
- 如未来需要公开演示，再单独制作演示 fixture，不覆盖内部完整基线。

## 3. 每个基线必须登记

- 数据库类型和版本。
- 采集器版本、package/schema 版本。
- 原始包文件名、大小、SHA256、采集时间。
- 分析器、规则配置和生成器的 SHA256。
- 采集状态分布和完整度。
- 规则 triggered/passed/not_evaluated/not_applicable 数量。
- 风险数量、健康评分、报告章节数和 item 数。
- 生成命令和是否完成 Word 视觉检查。

## 4. 最低场景集合

后续每种数据库至少应准备：

1. 正常单实例。
2. 缺权限或模块失败。
3. 不支持/不适用能力。
4. 空结果但正常。
5. 至少一个高风险触发。
6. 旧采集器版本兼容样例。
7. 字段缺失或新增字段样例。

当前 MySQL 完整基线位于 `tests/baselines/mysql/current/`，原始采集包保留在项目根目录。

