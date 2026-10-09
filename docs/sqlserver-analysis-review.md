# SQL Server 分析层复查清单（v1.3.5 真机包）

> **修订记录（2026-10-08）**：**P0 三条已修**（`rules.py` + 规则包，详见 `sqlserver-collector-gap-plan.md` §22）。
> 真机包实测：findings 35 → 30，**critical 9 → 1**、high 6 → 4、medium 18 → 9、low 2 → 16。
> 守门测试 `tests/test_sqlserver_rule_semantics.py`（19 条）；反向验证破坏三处分别触发 4/2/1 条失败；
> 全量 pytest **5 failed / 245 passed**（5 项预存在、同名）→ 零新增回归。
>
> ⚠️ **P1-5（健康分无下界）因此升级为下一优先项**：P0 修完后真机包扣分 = 1×18 + 4×10 + 9×4 + 16×1 = **110**，
> `max(0, 100-110)` 仍是 **0 分** —— 修复的效果在报告首页看不出来。

复查对象：`sqlserver_inspection_v1_192.168.100.129_db-sql-01_1433_20260924_013803.zip`
（collector 1.3.5 / 46 个采集模块 / 采集包完整度 100%）

复查方法：真包跑完整链路 `analyze.py` → `generate_report.py`，逐层核对**采集键 → 分析层引用 → 呈现层渲染 → Word 嵌入**，并逐张图读图核验。

---

## 一、三个直接问题的结论

| 问题 | 结论 | 证据 |
|---|---|---|
| 采集的信息分析层是否都有？ | **是，零缺口** | 53 个顶层键在分析层**无孤儿**（最少 2 处引用）；46 个采集模块被呈现层 `_coll()` **全覆盖** |
| 出的图是否合适、是否必要？ | **数量与定位合适，但实现有 5 个真问题** | 8 张图全部生成并真实嵌入 Word；问题见 §3 |
| Word 生产脚本是统一的？需要改吗？ | **统一的，不需要改** | `generate_report.py` 按 `generator_contract` 分发；`plugins/sqlserver/word_report.py` 只是 `inspection_core.word_engine.WordReportEngine` 的一个 profile（62 行），与 MySQL/Oracle/PG 共用同一引擎 |

### 覆盖度实测明细

```
采集键总数 53        → 分析层引用命中：最少 2 处（ha_status / memory_clerks），最多 75 处（collection）
采集模块   46 个     → 呈现层 _coll() 覆盖 49 个名（46 实采 + 3 深度模块），缺口 = 0
图表       8 张 PNG  → Word 内 <w:drawing> × 9、r:embed × 9（8 图 + 1 logo），全部真实嵌入
规则求值   66 条     → passed 31 / triggered 19 / not_applicable 13 / not_evaluated 3（四态齐全）
```

> 结论：**不需要补采集，也不需要补渲染位**。这份包的链路是通的，问题在**判据质量**与**图表实现**。

---

## 二、P0：判据误报（及格线，必须先修）

真包跑到 `health.score = 0`、`severity_counts = {critical: 9, high: 6, medium: 18, low: 2}`。
逐条回溯发现，**9 个 critical 里只有 1 个是真的**。

### P0-1　`INSTANCE.SERVICE_STARTUP`：把「代理服务停止」误报成「SQL Server 服务未运行」critical

**代码实际行为**（`plugins/sqlserver/rules.py:236-251`）：

```python
for svc in s.get("server_services", []):
    name  = svc.get("servicename")
    status = svc.get("status_desc").upper()
    if status and status != "RUNNING":
        self.add("INSTANCE.SERVICE_STARTUP", ..., "SQL Server 服务未处于运行状态", "critical", name, ...)
```

循环**不区分服务角色**，任何服务 `status != Running` 都写同一条 critical。

**实测数据**（包内 `server_services`）：

| 服务 | startup_type | status | 规则行为 |
|---|---|---|---|
| SQL Server (MSSQLSERVER) | Automatic | **Running** | 不报（正确） |
| SQL Server 代理 (MSSQLSERVER) | Manual | **Stopped** | ❌ 报 critical「SQL Server 服务未处于运行状态」 |
| SQL Full-text Filter Daemon Launcher | Manual | Running | ❌ 报 medium「启动类型非自动」 |

**结论：三个服务全部误报**。
- 引擎在跑（能连上、能采集 46 个模块），Agent 停止被当成"实例当前不可用"；
- SQL Server 代理的**出厂默认启动类型就是 Manual**，全文服务 Manual 也是标准配置 —— 都不该按"非自动"报警。

**不修的后果**：报告首页出现「SQL Server 服务未处于运行状态」这一假 critical，客户第一眼就会质疑整份报告；同时扣掉 18 分。

### P0-2　`BACKUP.FULL` 与 `BACKUP.NO_BACKUP` 对同一数据库重复计数

**代码实际行为**：
- `rules.py:440-444`（`BACKUP.FULL`）：`hours is None or hours > full_limit` → critical「完整备份超期或缺失」
- `rules.py:616-622`（`BACKUP.NO_BACKUP`）：直接遍历 `no_backup_databases` → critical「数据库从未进行过完整备份」

**两条规则都覆盖"从未备份"**，语义重叠，同一批库各报一次。

**实测数据**（包内 `backups` / `no_backup_databases`，均 4 行，库名完全相同）：

| 数据库 | 恢复模式 | 上次完整备份 | BACKUP.FULL | BACKUP.NO_BACKUP |
|---|---|---|---|---|
| huangjinjing（业务库） | FULL | 无 | critical | critical |
| master | SIMPLE | 无 | critical | critical |
| model | FULL | 无 | critical | critical |
| msdb | SIMPLE | 无 | critical | critical |

→ **8 个 critical 实际只对应 4 个库的同一个事实**。

**不修的后果**：critical 数虚高一倍，健康分被重复扣分。

### P0-3　备份 / CHECKDB / 文件增长等规则缺「系统库守卫」

**代码实际行为**：整份 `rules.py` 里唯一的系统库排除在 `rules.py:407`，且**只作用于 Query Store 一条规则**：

```python
if (name.lower() not in {"master", "model", "msdb", "tempdb"}   # ← 仅此处
        and self.t.get("require_query_store_for_user_db", True)
        and ...):
    self.add("DB.QUERY_STORE", ...)
```

备份、CHECKDB、文件增长、TRUSTWORTHY、DB_CHAINING 等规则**都没有这个守卫**。

**实测后果**（本次包）：

| 规则 | 报出对象 | 其中系统库 | 性质 |
|---|---|---|---|
| `BACKUP.FULL` | 4 | master / model / msdb | 噪音 |
| `BACKUP.NO_BACKUP` | 4 | master / model / msdb | 噪音 |
| `BACKUP.LOG` | 2 | model | 噪音 |
| `DB.CHECKDB` | 4 | master / model / msdb | 噪音 |
| `FILE.PERCENT_GROWTH` | 4 | 含系统库 | 噪音 |
| `DB.CHAINING` | 3 | 含系统库 | 噪音 |

**真实缺陷只剩 1 个**：业务库 `huangjinjing` **FULL 恢复模式、从未完整备份、日志也未备份** —— 日志会无限增长且无法恢复到任意时间点。这条本该被突出，却被埋在 8 条系统库噪音里。

**不修的后果**：客户报告里出现"master/model/msdb 没有备份"这类无意义告警，真正的业务库风险被稀释；图表 `09_backup_age` 也把系统库画进去了（见 §3-5）。

---

## 三、图表实测：5 个真问题

8 张图定位是合理的（活动采样 / 内存压力 / 连接阻塞 / 等待类型 / 库容量 / 文件延迟 / 卷空间 / 备份年龄），全部生成并嵌入。**但逐张读图发现**：

### P1-1　图表绕过共享层，永远是 Pillow 手绘版

- MySQL / Oracle / PG 用共享层 `inspection_core.charts`：`render_spec()` **优先 matplotlib**，缺 matplotlib 才退回 Pillow，并标记 `renderer = pillow_fallback`。
- SQL Server 的 `plugins/sqlserver/*.py` **完全不引用 `inspection_core.charts`**（只引 `package_io` 与 `word_engine`），自带 `chart_style.py`（131 行，直接 `from PIL import Image, ImageDraw`）。

**实测**：本机 `matplotlib 3.11.1` **可用**，但 SQL Server 的 8 张图仍全部是 Pillow 手绘。
`inspection_core/preflight.py` 自己描述 Pillow 版的现象是「刻度可能重叠、Y 轴标签贴边，与 matplotlib 版差异明显」—— **`05_waits.png` 正是这个现象**（见 P1-3）。

### P1-2　双量级指标画在同一 Y 轴，小值被压成贴地直线

| 图 | 大值指标 | 小值指标 | 实际画面 |
|---|---|---|---|
| `01_activity` | — | Batch 0.19/s、TPS 0/s | 两条贴底直线，信息量≈0 |
| `02_memory` | PLE ≈ 30,000 s | Grants Pending 0 | Grants 贴地 |
| `03_connections` | 连接数 | 阻塞数 0 | 阻塞贴地 |

### P1-3　`05_waits.png` 文字重叠、量级吞没

- 首项 `SOS_WORK_DISPATCHER ≈ 1.0Ms`（100 万毫秒），第 3 项起只有几十毫秒 → X 轴 0–1.2M，**除第一项外全部柱子不可见**；
- 图例框与数值标签**重叠在图上**，文字互相压盖，无法阅读。

### P1-4　未剔除首采样点（MySQL 已有解决办法）

- MySQL：`_drop_warmup()` 明确丢掉第一点 —— 注释写明「first sample spans the collector's own startup, so it reads low and drags the scale」。
- SQL Server：`charts.py:22` 直接 `labels = [... for x in samples]`，**全部点进图**。

### P2-5　系统库混入备份图 + `999h` 哨兵值

`09_backup_age.png` 4 个条全是 `999h` 深红：
- `999.0` 是 `charts.py:77` 的**兜底哨兵值**（`hours = 999.0`），含义是"无备份记录"，却在图上被画成"999 小时前备份过"；
- `master` / `model` / `msdb` 与业务库并列，视觉上"满屏红"，误导性强。

### P2-6　`04_risk_distribution` 编号空缺（**有意移除，仅剩死代码**）

`baseline.json` 的 `removed_charts` 明确记录：

```
"04_risk_distribution（风险等级分布与风险登记册重复）"
```

→ **这张图是被有意删除的**，理由是「风险等级分布与风险登记册重复」，不是缺口、不需要补。编号 04 的空缺是删除后的正常结果。

但 `report_adapter.py:200` 留下的特判**没跟着一起删**：

```python
"source_scope": "derived" if chart_id == "04_risk_distribution" else "realtime_snapshot",
```

由于 `charts.py` 已不再生成该 ID，这个三元表达式**永远走 else 分支** → 确属死代码，应清理（纯净化，无功能影响）。

---

## 四、与 MySQL 对比：可直接吸收的 5 项

| # | MySQL 的做法 | SQL Server 现状 | 吸收方式 | 收益 |
|---|---|---|---|---|
| 1 | 图表走 `inspection_core.charts`（matplotlib + Pillow 双后端 + `renderer` 标记） | 自建 `chart_style.py`，永远 Pillow | 删 `chart_style.py`，改用 `render_spec` + `os_chart_specs` | 图质量与其他三库一致；降级可见 |
| 2 | `_drop_warmup()` 剔首点 | 无 | 在 `add_line` 里加 | 首点失真不再拖累坐标轴 |
| 3 | `os_chart_specs()` 4 张 OS 图（CPU/内存/磁盘/网络），**支持 realtime 模式** | **未使用**；`os_metrics.samples` 只以表格呈现 | 把 `os_metrics.samples` 映射进 realtime spec | **花 35.7 秒采的 OS 数据终于有图** |
| 4 | `inspection_core.charts.history.gap_indices()` 采样点缺失检测 | 无 | 复用 | 采样点不足时在图上标注，不假装连续 |
| 5 | 产出 `llm_input.json`（MySQL/Oracle/PG 都有） | **唯一不产出的库** | 加一个 `build_llm_input` | 可接 LLM 叙事增强 |

---

## 五、汇总清单

### P0（及格线：报告里有假结论，必须先修）

| 编号 | 问题 | 位置 | 影响面 |
|---|---|---|---|
| P0-1 | 服务规则不区分角色，Agent 停止报 critical「实例不可用」 | `rules.py:236-251` | 假 critical ×1、假 medium ×1 |
| P0-2 | `BACKUP.FULL` 与 `BACKUP.NO_BACKUP` 重复计数 | `rules.py:440-444` / `616-622` | critical 虚高一倍 |
| P0-3 | 备份/CHECKDB/文件增长等规则缺系统库守卫 | `rules.py:407` 之外全部 | 每类规则多出 2–3 条噪音 |

### P1（加分题：质量与一致性）

| 编号 | 问题 | 位置 | 影响面 |
|---|---|---|---|
| P1-1 | 图表绕过共享层，永远 Pillow 手绘 | `charts.py` / `chart_style.py` | 与其他三库视觉不一致 |
| P1-2 | 双量级指标同轴，小值贴地 | `charts.py:34-39` | 3 张折线图信息量≈0 |
| P1-3 | `05_waits` 文字重叠、量级吞没 | `chart_style.py` | 图不可读 |
| P1-4 | 未剔首采样点 | `charts.py:22` | 首点失真进图 |
| P1-5 | 健康分线性扣分无下界（9×18 分直接归零） | `metrics.py:16-21` | 真 1 个 vs 9 个 critical 都是 0 分 |

### P2（可选）

| 编号 | 问题 | 位置 |
|---|---|---|
| P2-1 | `04_risk_distribution` 渲染位空置 + 死代码 | `report_adapter.py:200` |
| P2-2 | `999h` 哨兵值进图 | `charts.py:77` |
| P2-3 | 无 `llm_input.json` | `analyzer.py` |

---

## 六、本次未验证 / 待确认

- **`错误日志摘要` = `success_no_data`（0 行 / 177 ms）**：需确认是该实例确实干净，还是中英双语筛选口径过严。换一个有历史错误的实例复采即可判定。
- **健康分算法**：P1-5 是"两库共有"的设计（MySQL 也是 `max(0, 100 - Σweight)`），不是 SQL Server 独有。列出仅供决策，不代表必须改。
- **`os_metrics` 仅本机采集**：远程执行（`-Server <ip>`）时 OS 章节为空是**有意设计**（执行机数据不得写入客户报告），不是缺陷。
