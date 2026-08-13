# 采集层模块化目录

> 状态：参考设计，未接入正式运行流程。客户侧当前仍以四个独立单文件采集脚本（`mysql_inspection_standard.sh` 等）作为正式交付物；本目录只是后续采集层重构的方向验证，不参与现有采集流程。

按 `docs/collector-target.md` 分阶段抽取公共 Linux/Windows 采集层。数据库 SQL、许可和专属字段留在各自插件，客户侧仍交付四个独立单文件入口。

## 目录

```text
collectors/
  common/
    contract/linux-common-checks.yaml   # C0 冻结的公共系统检查目录
    linux/*.sh                          # C1 公共 Linux 模块
  mysql/
  postgresql/
  oracle/
  sqlserver/
  build/                                # C4 统一发布构建
  tests/
```

## 实施顺序

- C0 规范冻结：`common/contract/linux-common-checks.yaml`，当前采集脚本不改。
- C1 抽取 MySQL/PG 公共 Linux 源码（util/runtime/status/security/package，后续补 system_static/sampling/sar）。
- C2 Oracle 对齐。
- C3 SQL Server Windows 公共层。
- C4 从模块源码构建四个独立单文件 collector。

## 公共模块契约

公共模块是被 source 的 Bash 文件，只定义函数，不执行入口逻辑。它们依赖入口脚本预先设置的全局变量，并通过插件 hook 注入数据库差异。详见 `common/linux/README.md`。
