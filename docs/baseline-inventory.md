# 阶段 0 基线清单

> 冻结日期：2026-08-11。SHA256 用于后续确认输入和代码是否与本次盘点一致。
>
> ⚠️ 本清单是**整理前的历史快照**。阶段 13 仓库整理后部分文件位置已变更：`analyze_inspection_v2.py` → `plugins/mysql/analyzer.py`、`chart_style.py` → `plugins/mysql/chart_style.py`、`inspection_rules.json` → `plugins/mysql/inspection_rules.json`、`enhance_report.py` → `tools/enhance_report.py`、`logo.png` → `assets/logo.png`；根 `rules.py` 兼容壳已删除。表中的字节数与 SHA256 反映整理前状态，未随搬迁改写。

## 1. MySQL 阶段 0 冻结代码基线

| 文件 | 版本/用途 | 字节 | SHA256 |
|---|---:|---:|---|
| `mysql_inspection_standard.sh` | collector 1.1.0 | 96,356 | `3AD1304CEBDB8FF17F8BEA782FB3206DED25D651163A9A463C0D2A67917B0AE8` |
| `analyze_inspection_v2.py` | analyzer 2.1.0 | 152,191 | `822C1E5345C7E259210F9E6EC4954AA4D459DE1C8110E71AA025FA31F5FE9173` |
| `rules.py` | 规则执行器 | 19,025 | `C5EE2488EAC226850D33FCC174F17565D2B00BB710DD7932F227650FED41C2A3` |
| `inspection_rules.json` | rules schema 2.0，24 条规则 | 11,429 | `8CC5DD3C70BA94E8084B30538E1AEC9392E45E08B15AC465D132B3C044DAB157` |
| `generate_report_docx_v3.py` | generator 3.1.0 | 44,085 | `EFDD4E3D1EA509EFBD3694C620C407089D4E797BB12204CCA34F12EE86CC1F6C` |
| `enhance_report.py` | 可选 LLM 增强 | 10,210 | `307079EE6AB4E5B73A4B1B8C6B6C81B4E853CA9666CF127C6B7DBF371CF79238` |
| `chart_style.py` | 图表样式 | 4,414 | `220E61A4B5E2B8A1232D1414C1D79EDBCB73D312C24979E9B87DDC99C2051244` |

以上指纹永久保留，用于证明重构前状态。阶段 1 已有意修改分析器和规则模块，当前指纹如下：

| 文件 | 字节 | SHA256 |
|---|---:|---|
| `analyze_inspection_v2.py` | 142,817 | `249BD145A97C7E15875001C3365B0D3A161C41C099554617E074AF7986B136EE` |
| `rules.py` | 18,978 | `885291CC31AE9E3F1A04E60A09C3E051070596BC9C298D7B44B1486E92A79DB1` |
| `inspection_core/models.py` | 2,800 | `357E1CAA6B108B3EC74C4DB22A5FC60D81688D51CE8C5B43ADCE179DC449252E` |
| `inspection_core/values.py` | 552 | `EE529883C48AAFC4F5E469AA70A663069189E324B1E208DF7B3B7716F9556B27` |
| `inspection_core/package_io.py` | 2,421 | `81EFE5E2EDF07A4237F2EA9A54F3BCD21B56CFECF59A63921ED8BBDD8C372C54` |
| `inspection_core/tabular.py` | 1,965 | `ADC2F439B0971EF79350F0A0ED0463DA329E2F5B676D4E27F062526A0730FBCB` |
| `plugins/mysql/package_adapter.py` | 5,471 | `D6B07C6D443E45585BB4204C75A98AE4BDA1817749A8922872C4A03190F03590` |

collector、规则配置、Word 生成器、增强器和图表样式仍保持阶段 0 指纹。

阶段 2 当前代码指纹：

| 文件 | 字节 | SHA256 |
|---|---:|---|
| `analyze_inspection_v2.py` | 124,495 | `C70C78216E3062D3C8266743E2B38602DFC3F83F869FC5A720E110E4F4764ADF` |
| `inspection_core/statistics.py` | 1,106 | `3DE4042FF40C4BA5FA7ED4B044B330A91E80E14296EBB4BBE0787E5472568937` |
| `inspection_core/sampling.py` | 2,672 | `1762233ED3AEEC5FD5F8BDD968813F8D877DDABA974F7CB1A40E193831437D58` |
| `plugins/mysql/metrics.py` | 16,400 | `8C3A70A0F9568F7806540303E99520A2EE38D30AC2857D729D1B245C3FFE25DB` |
| `tests/test_mysql_metrics.py` | 3,488 | `E980888DBA4FF19B556648BE4B6A82BB43F1AB715144EBF5336D5C27FDFC8125` |

阶段 2 回归：12 项自动测试通过；真实 MySQL 包生成的三份业务 JSON 与阶段 0 基线一致。

阶段 3 当前代码指纹：

| 文件 | 字节 | SHA256 |
|---|---:|---|
| `analyze_inspection_v2.py` | 56,123 | `3EAF5CAF127272B2802C3AD3052754FDF2DEB93B8546EEAE256AA837A7462B3C` |
| `inspection_core/reporting.py` | 2,306 | `09F84480B9D111EA764180156576556B6EFBDC290E6F8A95CF64897C144113EC` |
| `plugins/mysql/presentation.py` | 67,971 | `93B6548A56CCF1C0A15A3AE381725405CD186A47AB5AD68077ABBDB8E30B9298` |
| `tests/test_mysql_presentation.py` | 3,135 | `1770604B34621CB2F903F6BCD1E153F38ADF8D47846C14D41D700F175D59769A` |

阶段 3 回归：15 项自动测试通过；9个章节、33个 item、7条风险及三份业务 JSON 与阶段 0 基线一致。

阶段 4 当前代码指纹：

| 文件 | 字节 | SHA256 |
|---|---:|---|
| `analyze_inspection_v2.py` | 29,008 | `A8184F8500660BB723EAF8A8FA88DA81CA993E81478C04D7CC335C2D1062B0DF` |
| `rules.py`（兼容入口） | 539 | `426A89BA6E5D8208B78165C2ABB40195AF0D9779424EF28E2D21F18BE192838C` |
| `plugins/mysql/charts.py` | 27,683 | `21D264DB36046B8F305CD29E88BBDF400C0A3AB382839801B5FACD3FF7C70760` |
| `plugins/mysql/rules.py` | 19,429 | `088D6947BE574F73F0569D5ED53A90DE9B023A435B44C51640225ADDE2CAF98E` |
| `tests/test_mysql_providers.py` | 2,241 | `5B2095F2D45203D24E9AB966409880433516EF457DE287E691406407E6D1B6CB` |

阶段 4 回归：18项测试通过；业务 JSON 匹配阶段0基线；同一绘图环境的严格图表元数据和6张PNG SHA256全部匹配。

阶段 5 当前代码指纹：

| 文件 | 字节 | SHA256 |
|---|---:|---|
| `generate_report_docx_v3.py`（MySQL兼容入口） | 3,397 | `14B02CB52F1DF404D1D6ABEDBCA67432088790698190DAA78D15CE9B83F4D4B0` |
| `inspection_core/word_engine.py` | 42,383 | `74501C5332DED227B92A43350E62E4ACF2862723C17DA3B55CBDEAFBDC208AE3` |
| `plugins/mysql/word_report.py` | 2,112 | `38C82F31431BC65976F1BB82A90851729038E1C7C7FAE58AC77F30FC670022EA` |
| `tests/test_word_engine.py` | 3,152 | `DE9F11505C443E1B54FFEA535A315E6B9BFBB68469D9F318EC0A02FF1F6900C4` |
| `plugins/mysql/charts.py`（补齐 Pillow fallback 的 `math` 导入） | 27,694 | `FF04FE0A1EF2073CF72F4F64DF2FA7B12DD676385D0666DAB10C9843F1975535` |

阶段 5 回归：22项测试通过；三份业务 JSON 匹配；与基线相同的 Pillow 环境下严格图表回归匹配；Word 拆分前后去除图片辅助属性后的 `word/document.xml` SHA256 均为 `C825B99396B1C43DE7899345BAF546B10CBD22DE3295C6B263AAB8544D23E028`。45张表格几何、标题层级、页面设置和7张图片通过结构审计，可访问性检查为0项问题。当前机器缺少 LibreOffice，未完成逐页 PNG 视觉验收。

阶段 6 当前代码与专业样稿指纹：

| 文件 | 字节 | SHA256 |
|---|---:|---|
| `generate_report_docx_v3.py`（MySQL 独立入口） | 3,642 | `8F0029E7ECF2106DD3700F1B2F3BBE9E6A853E7C3E6EEEE704FE045251DDA2D0` |
| `inspection_core/word_engine.py` | 59,394 | `06067ADC7A10CDB709AE2EEA87FB0EC07DFD9EC63DFB17A6F5B24425F879AE60` |
| `plugins/mysql/word_report.py` | 2,112 | `38C82F31431BC65976F1BB82A90851729038E1C7C7FAE58AC77F30FC670022EA` |
| `tests/test_word_engine.py` | 5,854 | `FD21E4FF72C7B92C0269C6AEDFED215FA325830B4A3ED39855BF1DE85C608A5E` |
| `output/mysql_professional_report.docx` | 258,672 | `B5BFD4F57B711459B7B49B35199257CECF47116EDD6FC65016F02B9F20C7EFAC` |

阶段 6 回归：24项测试通过；三份业务 JSON 和严格图表元数据均匹配冻结基线；旧版 Word 去除图片辅助属性后的 `word/document.xml` SHA256 仍为 `C825B99396B1C43DE7899345BAF546B10CBD22DE3295C6B263AAB8544D23E028`。专业版包含15个业务章节、59张表、7张行内图片和页码/总页数字段，表格几何与可访问性审计通过。由于当前机器缺少 LibreOffice，逐页 PNG 视觉验收仍待补做。

阶段 7 当前代码与专业报告第二版指纹：

| 文件 | 字节 | SHA256 |
|---|---:|---|
| `inspection_core/word_engine.py` | 63,126 | `1354AC5569ED7D801058151079B2BF52E3C547809D8E6B72791DD006EF5D92A1` |
| `plugins/mysql/charts.py` | 24,386 | `1C1F53E749B69B0591557DF164C2A24E394F79CD9B3FE6CCF94193AD9F760E10` |
| `plugins/mysql/presentation.py` | 69,758 | `4A67318700293A38C9001E4F75736986993F5A3B763100B95BCB182F85A5C220` |
| `tests/test_mysql_providers.py` | 3,727 | `DB3CE2624641FFAB85258E443A0ED6A05AC2B061566250F2059684474FC53585` |
| `tests/test_mysql_presentation.py` | 3,576 | `B03957587DE3325ADB9526C2AE56C385037C95966E6EE7FCD401AEAE76F41B22` |
| `tests/test_word_engine.py` | 7,425 | `AEF66DCDFD25825B90F63DE9CA91CE8E440699382D830D40B16898DB885E8A61` |
| `output/mysql_professional_report_v2.docx` | 470,237 | `E0F240BF88D4C98362118A1639CA778BBC7EEE9EA73490B9C234E6A9BA73388F` |

阶段 7 回归：27项测试通过；旧版 Word 归一化内容哈希保持 `C825B99396B1C43DE7899345BAF546B10CBD22DE3295C6B263AAB8544D23E028`。默认基线仅有5项预期展示差异：3项用于纠正“可用 SAR 历史”结论、依据和建议，2项用于把主机时间和日志修改时间格式化为 `UTC+08:00`；`llm_input.json` 匹配，健康分45、7条风险及2高/3中/2低不变。严格图表差异为有意升级：删除旧 `ALL` 跳过占位、6张图改用中文安全渲染、SAR历史来源、真实时间轴、分单位面板和启动点披露。第二版含15个业务章节、59张表、7张图片；经本机 Word 导出为34页 PDF并逐页渲染检查，未发现空白页、乱码、方框字、裁切或重叠。

## 2. 回归样例状态

| 数据库 | 采集包 | analysis/report model | Word | 状态 |
|---|---|---|---|---|
| MySQL | 已有完整真实采集包 | 已生成 | 已生成 | 已建立 `tests/baselines/mysql/current/` 基线 |
| Oracle | 有真实环境 tar.gz | 已生成 | 已生成并验收 | 已建立 `tests/baselines/oracle/current/` 契约适配器与 Word 基线 |
| PostgreSQL | 有真实环境 tar.gz | 已生成（旧模型与标准模型） | 已生成并验收 | 已冻结旧流程，并建立 `tests/baselines/postgresql/current_v2/` 公共核心接入基线 |
| SQL Server | 有真实环境 zip | 有 | 已生成并验收 | 已复制到 `tests/baselines/sqlserver/current/` 并建立契约适配器与 Word 基线 |

## 3. 外部样例登记（可能含敏感数据）

这些文件以及 MySQL 新基线均按内部完整数据处理，不应直接提交到公开仓库或发送外部服务。

| 类型 | 原始位置 | 字节 | SHA256 |
|---|---|---:|---|
| Oracle 采集包 | `D:\AI\ORACLE\GPT\1.0\oracle_inspection_task_ZYDB_db01_192.168.100.80_1521_zydb1_2026-08-10_20-45-41.tar.gz` | 44,351 | `9EDE9635A440908FEF77F919B46134ACAE515F245430415788A453C72E68A82C` |
| PostgreSQL 采集包 | `D:\AI\PG\GPT\pg_inspection_v2_db01_192.168.100.80_6000_20260810_140043.tar.gz` | 35,083 | `99FD8CACC7956121A583FEE18D9E570DA992C3CC43366B8B5430B4744B8E5DE3` |
| SQL Server 采集包 | `D:\AI\SQLserver\GPT\sqlserver_inspection_v1_WIN-OAR8MD5G54N_20260728_000158.zip` | 35,349 | `F7EB3FC1F0CC735E4EB180836EBE7C1CBA93AE7E2657BA827800D86A8E2D6DBA` |
| SQL Server analysis | `D:\AI\SQLserver\GPT\test_final\analysis.json` | 201,470 | `B2EDF9E49B6016CD53577609F585F5ED8CCBBF8C62E3ED790293E98BA2BD9061` |
| SQL Server report model | `D:\AI\SQLserver\GPT\test_final\report_model.json` | 434,546 | `C7B98A1D51BA4DCE077655B2CB9776C72ABC75CD9546AFA3245D587C1E1DBDE9` |
| SQL Server Word | `D:\AI\SQLserver\GPT\test_final\WIN-OAR8MD5G54N.docx` | 214,437 | `E5A75149A6EA70775B0B1513ABD230704C127309AA3AB1D5B98F4A3AC7D9D96F` |
| 专业报告参考 PDF | `D:\AI\2026年第二季度_广州塔_官网售票系统_数据库巡检报告.pdf` | 1,815,473 | `084002BFF4CEB53562DEAFF8EBEFA0DDD1E39C17F7902D1CBAD70F5B9E5D8A57` |

## 4. MySQL 基线验收清单

已收到并登记：

- 原始包：`mysql_inspection_v1_db01_192.168.100.80_3306_20260811_160102.tar.gz`
- 大小：49,426 字节
- SHA256：`35DB55CEA9468C785F87501722E6739D762A32F710EB518BD670FB5DDC4068C3`
- 输出：`tests/baselines/mysql/current/`
- 结果：采集完整度 99.8%，健康评分 45，7 条规则触发，2 高/3 中/2 低风险。

本基线保留完整信息，不制作脱敏替代。后续仍应补齐：

1. 原始包路径、大小、SHA256、采集器版本和 schema 版本。
2. 敏感数据边界说明；原始包和生成结果保持内部使用。
3. 使用当前分析器生成的 `analysis.json`、`report_model.json`、`llm_input.json` 和图表。
4. 使用当前生成器生成的 Word。
5. 记录规则评价数量、风险数量、报告 item 数、采集状态分布和健康分。
6. 如未来需要公开演示，另建公开演示 fixture，不覆盖当前完整基线。

## 5. PostgreSQL 阶段 8 冻结与接入基线

- 原始包：`D:\AI\PG\GPT\pg_inspection_v2_db01_192.168.100.80_6000_20260810_140043.tar.gz`
- 大小：35,083 字节；SHA256：`99FD8CACC7956121A583FEE18D9E570DA992C3CC43366B8B5430B4744B8E5DE3`
- 旧流程冻结：`tests/baselines/postgresql/legacy_2_0_1/`
- 公共核心接入基线：`tests/baselines/postgresql/current_v2/`
- 结果：1 个实例、完整度 93.5%、健康分 **80**、36 条规则（2 触发/32 通过/2 未评价）、14 章节、14 项、6 张图（阶段 14 第④步由 5 张改为 6 张：系统章节 3 张并成 4 张公共 OS 图）。健康分旧记录写的是 90，那是从过期的 `current/` 基线抄来的值，第⑤步按实测修正为 80。
- 阶段 14 第⑤步：OS 四条（CPU/IO wait/内存/磁盘 util）的判据、阈值、理由与置信度改由 `inspection_core/system_checks.py` 统一给出，rule_id 由 `COMMON.SYSTEM.{CPU,IOWAIT,MEMORY}` 收口为 `*_PRESSURE`；本基线已就地重跑刷新。
- Word：公共 4.2.0 专业引擎生成 23 页报告；逐页视觉检查和可访问性审计通过。（阶段 14 第④步改为 6 张图后未重新逐页复核页数，详见 `current_v2/baseline.json` 的 `professional_word_pages_note`。）
- 详细输出指纹、代码指纹和预期差异见 `tests/baselines/postgresql/current_v2/baseline.json`。
- `report_model_legacy.json` 永久保留旧 PG envelope；`report_model.json` 使用 `postgresql_inspection_report_model` 标准契约。

## 6. Oracle 阶段 10 冻结与接入基线

- 原始包：`D:\AI\ORACLE\GPT\1.0\oracle_inspection_task_ZYDB_db01_192.168.100.80_1521_zydb1_2026-08-10_20-45-41.tar.gz`
- 大小：44,351 字节；SHA256：`9EDE9635A440908FEF77F919B46134ACAE515F245430415788A453C72E68A82C`
- 外部分析器/规则指纹见 `tests/baselines/oracle/current/baseline.json`。
- 契约适配器与 Word 基线：`tests/baselines/oracle/current/`
- 结果：1 个实例、完整度 100%、健康分 26、34 条规则（10 触发/20 通过/4 未评价，与规则包条数一致）、9 章节、**52 项**、**8 张图**、10 条风险（2 critical/2 high/4 medium/2 low）。健康分与 critical 数在第⑦步 severity 分级后变化（43 → 26、1 → 2）；旧记录写「48 项 / 9 张图」，是阶段 14 第③步（IO Wait 图并回 CPU 图、系统环境章节补 4 项）之前的值。
- 阶段 14 第⑤步：OS 三条（CPU/IO wait/内存）改由公共层判定，rule_id 由 `ORA.SYSTEM.*` / `ORA.CAPACITY.FILESYSTEM_USAGE` 收口为 `COMMON.*`；同时删除从未被上报的死规则 `ORA.SYSTEM.SAR_CPU_PEAK` / `ORA.SYSTEM.SAR_IOWAIT_PEAK`（规则包 36 → 34 条）。本基线已就地重跑刷新。
- 阶段 14 第⑦步：`_evaluate` 新增 `severity_override` 形参，二级严重度阈值真正落地。`ORA.PERFORMANCE.LIBRARY_CACHE` 命中率 79.2% 低于严重档，severity `medium` → `critical`，findings 按严重度重排（F-006 → F-002），健康分 **43 → 26**、critical 1 → 2；`ORA.CONFIG.REDO_MEMBER` 判定改读 `redo_member_min`（触发集合不变，文案与 fact 键更新）。8 张图 PNG 逐字节不变。本基线已就地重跑刷新。
- Word：公共 4.2.0 专业引擎生成 Oracle 专业版报告（含 9 张技术图表）；逐页 PNG 视觉验收待具备 LibreOffice 的环境补做。
- `report_model.json` 保留外部事实来源；`report_model_standard.json` 为 `plugins/oracle/report_adapter.py` 适配后的标准报告模型。

## 7. SQL Server 阶段 11 冻结与接入基线

- 原始包：`D:\AI\SQLserver\GPT\sqlserver_inspection_v1_WIN-OAR8MD5G54N_20260728_000158.zip`
- 大小：35,349 字节；SHA256：`F7EB3FC1F0CC735E4EB180836EBE7C1CBA93AE7E2657BA827800D86A8E2D6DBA`
- 外部分析器/规则指纹见 `tests/baselines/sqlserver/current/baseline.json`。
- 契约适配器与 Word 基线：`tests/baselines/sqlserver/current/`
- 结果：1 个实例、完整度 100%、健康分 0、14 条规则全部触发、12 章节、27 项、24 条风险（8 critical/9 high/6 medium/1 low）、9 张图。
- Word：公共 4.2.0 专业引擎生成 SQL Server 专业版报告（含 8 张技术图表）；逐页 PNG 视觉验收待具备 LibreOffice 的环境补做。
- `report_model.json` 保留外部事实来源；`report_model_standard.json` 为 `plugins/sqlserver/report_adapter.py` 适配后的标准报告模型；`sqlserver_legacy_report.docx` 保留旧版 Word。
