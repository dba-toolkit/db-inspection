# 公共 Linux 采集模块契约

这些文件是被 `source` 的 Bash 函数库，不含入口逻辑，也不含数据库专属 SQL/字段。

## 依赖全局变量

入口脚本在 source 之前设置：

```text
TASK_DIR         任务根目录
TABLES_DIR       tables/
TIMESERIES_DIR   timeseries/
HISTORY_DIR      history/
EVIDENCE_DIR     evidence/
LOG_DIR          logs/
MODULE_LOG_DIR   logs/modules/
TMP_DIR          临时目录
STATUS_PARTS_DIR 状态明细目录
LOG_FILE         采集日志
OUTPUT_PARENT    输出父目录
MIN_FREE_MB      最小剩余空间(MB)
MANIFEST_FILE    manifest 输出路径
PACKAGE_FILE     回传包路径(可延迟设置)
PACKAGE_VERSION  包版本
COLLECTOR_VERSION 采集器版本
INSTANCE_TAG     实例标识
DATABASE_TYPE    数据库类型
CREATE_PACKAGE   是否打包
AUTH_TMP_PATTERN 需要排除的临时认证文件名(如 .mysql_defaults.*.cnf；无则为空)
BG_NAMES/BG_START_ISO/BG_START_MS/BG_PIDS  后台模块状态数组
```

## 插件 hook

公共模块通过 hook 注入数据库差异，不反向依赖数据库代码：

```text
known_tsv_header <basename>  已知 TSV 列头；返回 0 并打印列头，否则返回 1
cleanup_auth                 清理数据库临时认证文件
```

数据库专属函数（如 `mysql_exec`、`known_tsv_header` 的 MySQL 分支、`collect_mysql_*`）
留在 `collectors/mysql/` 等插件目录，不进入本目录。
