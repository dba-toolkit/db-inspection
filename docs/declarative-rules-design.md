# 声明式规则设计（规则阈值外置）

目标：改阈值、加普通规则不再改 Python；Python 只负责“复杂派生指标计算”。规则引擎四库共享，只读取标准事实模型 + 派生指标。

## 1. 原则

1. 规则判定全部在 YAML 中声明，Python 不写规则判断。
2. 未采集/缺失/不适用不得判定为通过：`not_evaluated` / `not_applicable` 与 `passed` 严格区分。
3. 数组结构（表空间、复制槽、无主键表、数据库列表）必须支持逐元素评估。
4. 时序数据（sar）必须支持趋势与基线偏离判断。
5. 复杂派生指标仍由各库 `metric_provider` 计算，但必须声明输入依赖。

## 2. 文件结构与版本

```yaml
rules_version: "1.0"
database_type: mysql          # mysql/postgresql/oracle/sqlserver
rules:
  - ...
```

## 3. 规则元数据

```yaml
- id: "MYSQL.CONNECTION.USAGE"      # 唯一、稳定、不可复用
  name: "MySQL 连接使用率"           # 人类可读名称
  category: "PERFORMANCE"           # SYSTEM/CAPACITY/PERFORMANCE/SCHEMA/SECURITY/AVAILABILITY/BACKUP
  severity: "HIGH"                  # CRITICAL/HIGH/MEDIUM/LOW/INFO
  enabled: true                     # 可临时禁用，不改代码
  tags: ["mysql", "connection"]
  description: "连接使用率接近上限时告警。"
  applicability:
    required_capabilities: []       # 不满足 -> not_applicable
    required_inputs: ["db.total_connections", "db.max_connections"]  # 缺失 -> not_evaluated
  predicate: { ... }
  output: { ... }
```

## 4. 谓词类型

| type | 用途 |
|---|---|
| `threshold` | 单值/数组逐元素数值比较 |
| `range` | 值落在 [min,max] 之外触发 |
| `trend` | 时序趋势：最近 N 点/窗口内持续上升/下降/波动 |
| `deviation` | 与历史基线偏离：高于均值 X 倍或超出带宽 |
| `exists` | 事实存在且满足比较（如“无主键表数 > 0”） |
| `missing` | 关键事实缺失 -> not_evaluated（不判通过） |
| `compound` | 显式 AND/OR 组合多个子谓词 |

### 4.1 threshold（含数组遍历）

```yaml
# 单值
predicate:
  type: threshold
  fact: "os_perf.memory.usage_pct"
  op: ">="
  value: 90

# 数组逐元素：每个超阈值的元素独立产生一条告警
predicate:
  type: threshold
  fact: "db.extensions.tablespaces[*].usage_pct"
  op: ">="
  value: 90
  where:                          # 可选过滤
    field: "name"
    not_in: ["SYSTEM", "SYSAUX"]
```

数组语义：

- `fact_path` 中出现 `[*]` 表示“遍历数组元素”。
- 每个元素独立评估，输出进入 `details.violations`，不在同一规则里合并成一个值。
- `where` 可选：`in` / `not_in` / `op` + `value`，用于排除系统对象或按条件筛选。
- `[*.name]` 之类嵌套取值时，先展开数组，再在元素上取字段。

### 4.2 trend

```yaml
predicate:
  type: trend
  fact: "sar.cpu"                 # 时序序列
  window_points: 5                # 最近 5 个采样点
  direction: "up"                 # up / down / volatile
  min_slope: 2.0                  # 每点平均变化（百分比点）
```

输出 `details.slope`、`details.points`。`window_points` 不足时判 `not_evaluated`。

### 4.3 deviation

```yaml
predicate:
  type: deviation
  fact: "db.total_connections"
  baseline_fact: "sar.db_connections"   # 历史基线序列（或 provider 提供的基线指标）
  window_points: 24
  op: ">"
  factor: 1.5                          # 高于历史均值 50%
```

无基线数据时判 `not_evaluated`，不得直接判正常。

### 4.4 compound

```yaml
predicate:
  type: compound
  logic: AND                       # 显式 AND / OR
  items:
    - {type: threshold, fact: "os_perf.memory.swap.used_bytes", op: ">", value: 0}
    - {type: threshold, fact: "os_perf.memory.swap.usage_pct", op: ">", value: 20}
```

`correlation` 不单列类型，用 `compound` 的 AND 表达。

## 5. metric 声明

派生指标必须声明输入，方便引擎校验、依赖分析和缓存：

```yaml
predicate:
  type: threshold
  metric:
    name: "mysql.connection_usage_pct"
    inputs: ["db.active_sessions", "db.total_connections"]
    formula: "active_sessions / total_connections * 100"   # 可选，仅简单四则
    # 复杂派生指标改由 provider 提供，formula 省略：
    # provider: "mysql"
  op: ">="
  value: 85
```

规则：

- `inputs` 必填；缺失任一输入 -> `not_evaluated`。
- `formula` 仅支持 `+ - * / ( )` 与已声明的输入名、数字字面量；不允许任意代码。
- 复杂指标（计数器速率、窗口聚合）仍走 `metric_provider`，不写 `formula`。

## 6. 输出结构

引擎输出统一评价对象，状态/级别沿用既有契约：

```json
{
  "rule_id": "MYSQL.CONNECTION.USAGE",
  "status": "triggered",
  "severity": "high",
  "fact_value": 85.5,
  "threshold": 85,
  "evaluated_at": "2026-08-13T10:30:00+08:00",
  "details": {
    "instance": "mysql-0-108",
    "message": "连接使用率 85.5% 超过阈值 85%"
  }
}
```

状态：`triggered / passed / not_evaluated / not_applicable`。
级别：`critical / high / medium / low / info`。
`details` 为灵活对象，按谓词类型不同：

- threshold 单值：`{fact_value, threshold, op}`
- threshold 数组：`{violations: [{element, value, threshold}]}`
- trend：`{slope, direction, points}`
- deviation：`{baseline_mean, observed_value, factor}`
- compound：`{matched: ["子条件1", "子条件2"]}`

## 7. 迁移现状

- 现有 `inspection_rules.json` 的元数据/阈值迁移到 YAML；复杂判断从 `plugins/*/rules.py` 移除，只保留派生指标计算。
- 迁移顺序：先 MySQL 简单阈值 + 公共系统规则，再覆盖数组规则（无主键表/表空间）、时序规则、备份/复制复杂规则。
- 每迁移一条：保留 rule_id、补阈值/边界/数组/趋势测试，并与现有 `analysis.json` 基线对比，确保规则状态与风险数量一致。
