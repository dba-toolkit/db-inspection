# 目标采集器架构

> 落地状态：阶段 C0 已冻结 `collectors/common/contract/linux-common-checks.yaml`；阶段 C1 已抽取 `collectors/common/linux/{util,runtime,status,security,package}.sh` 并通过 `collectors/tests/test_common_linux.sh`。system_static/sampling/sar 与 Oracle/SQL Server 对齐、统一发布构建仍待后续阶段。

## 1. 总体原则

最终形态：

> 公共采集规范 + Linux/Windows 公共实现 + 四个数据库采集插件 + 四个独立入口。

不建立一个包含四种数据库全部 SQL 的巨型 collector。

## 2. 源码与交付形态分离

开发源码建议拆分：

```text
collectors/
  common/
    contract/
    linux/
      runtime.sh
      status.sh
      system_static.sh
      sampling.sh
      sar.sh
      security.sh
      package.sh
    windows/
      Runtime.ps1
      Status.ps1
      SystemStatic.ps1
      Sampling.ps1
      Security.ps1
      Package.ps1
  mysql/
    entry.sh
    capabilities.sh
    database_checks.sh
  postgresql/
    entry.sh
    capabilities.sh
    database_checks.sh
  oracle/
    entry.sh
    capabilities.sh
    database_checks.sh
  sqlserver/
    entry.ps1
    capabilities.ps1
    database_checks.ps1
  build/
```

客户侧仍交付四个独立、可单文件运行的入口：

```text
mysql_inspection.sh
pg_inspection.sh
oracle_inspection.sh
sqlserver_inspection.ps1
```

构建过程可以把公共模块与数据库插件组合成单文件，避免客户服务器必须保留复杂目录或安装额外运行时。

## 3. 公共 Linux 模块

首批公共模块：

1. runtime：日志、时间、超时、退出码、信号和临时目录。
2. status：稳定 check_id、状态、耗时、行数、错误和 artifact 登记。
3. system_static：OS、CPU、内存、文件系统、inode、block device、网络、NUMA、THP、sysctl、mounts、iostat、dmesg。
4. sampling：CPU、内存、磁盘、网络同步采样；数据库插件提供活动指标 callback。
5. sar：历史文件发现、CSV 导出、覆盖时间、断点和数据质量。
6. security：命令输出清理、敏感模式扫描、打包前阻断。
7. package：snapshot、collection status、manifest、SHA256 和压缩包。

## 4. 公共 Windows 模块

Windows 实现提供相同语义，但使用 PowerShell/CIM/系统 API：

- OS、CPU、内存、卷、页文件和系统启动时间。
- 时间同步状态。
- 网络和基础性能采样。
- 目标主机归属判断。
- 稳定状态记录。
- manifest SHA256 和安全扫描。

数据库远程连接时，除非通过授权远程采集确认目标主机，否则本机 OS 数据不得冒充 SQL Server 所在主机。

## 5. 稳定 ID 和字段

可跨 Linux/Windows 复用的语义使用相同 ID，例如：

```text
system.os_release
system.time_status
system.cpu
system.memory
system.filesystems
system.block_devices
system.network
system.sar_history
timeseries.realtime_sampling
package.security_scan
```

数据库专属项使用数据库前缀：

```text
mysql.*
postgresql.*
oracle.*
sqlserver.*
```

每个检查必须声明：

- check_id
- title
- category
- schema_version
- applicable versions/capabilities
- expected privilege
- performance cost
- timeout
- artifact format and columns
- empty result semantics
- sensitive fields

## 6. 公共模块晋升规则

满足以下条件才能进入 common：

1. 至少两个数据库实际需要，或属于所有采集器必需的 contract/security/package 能力。
2. 输入输出不含某个数据库专属字段。
3. 有独立 contract 测试和至少两个插件 fixture。
4. 不要求插件通过条件分支修改公共函数内部逻辑；差异通过 callback/adapter 注入。

否则继续留在数据库插件。

## 7. 实施顺序

### 阶段 C0：规范冻结

- 完成系统检查目录、字段 schema 和四套现状基线。
- 当前采集脚本不改。

### 阶段 C1：MySQL/PG 公共 Linux 源码

- 抽取两套已有的 55 个公共函数。
- 吸收 PG 的 iostat、dmesg 和 mounts。
- 构建后输出必须与现有包语义兼容。

### 阶段 C2：Oracle 对齐

- 复用 runtime、sampling、SAR、security、package。
- 修复状态 ID，补统一 collection status。
- Oracle SQL 和许可逻辑不进入 common。

### 阶段 C3：SQL Server Windows 公共层

- 增加稳定英文 ID、详细状态和 manifest 哈希。
- 明确本机/远程主机证据边界。
- 保留 PowerShell 独立入口。

### 阶段 C4：统一发布构建

- 从模块源码生成四个独立单文件 collector。
- 每个生成物包含版本、构建清单和自检命令。

## 8. 为什么不立即改采集脚本

当前先完成 analyzer adapter 和 contract，是为了让分析器同时支持：

- 旧 collector 包。
- 新公共模块 collector 包。

如果采集和分析同时大改，报告差异出现时难以判断是采集变化、规则变化还是渲染变化。按层迁移可以使用现有基线逐步证明行为一致。
