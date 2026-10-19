#!/bin/bash
#===============================================================================
# Oracle 巡检报告脚本 v2.0 (2026-10-09)
# 用法: bash oracle_inspection.sh [host] [port] [service] [user] [password]
#   或  bash oracle_inspection.sh          (交互式输入)
#
# 支持环境: Oracle 单机 / RAC / ASM / CDB(PDB) / ADG
# 支持版本: Oracle 11g / 12c / 18c / 19c / 21c / 23ai
# 自动检测: RAC节点 / ASM磁盘组 / ADG备库 / CDB/PDB — 有才采集，无则跳过
#
# ── 产物（--structured 默认开）──────────────────────────────────────────
#   Markdown 报告：<prefix>_<tag>_<时间戳>.md（人读）
#   结构化包 tar.gz：
#     snapshot.json           环境/实例画像 —— **下游第一入口**（Python 先读它）
#     collection_status.json  采集项状态聚合（items[] + summary{}）
#     summary.txt             可读摘要（含"耗时最长前 10"）
#     status/<item_id>.tsv    每采集项一行：item_id/category/status/… —— 第二入口
#     tables/<item_id>.psv    SQL 结果（**竖线分隔**，不是 TAB；见 interface §6.3）
#     evidence/*              外部命令原始输出（chrony/ip/lscpu/dmesg/limits…）
#     timeseries/*.csv        实时采样点；history/*.csv  sar 历史
#     logs/modules/<cat>.log  模块错误**完整原文**（OF-26；status 的 reason 只留 5 行摘要）
#     manifest.json           包内文件 SHA256 清单（**无语义状态**）
#     COLLECTION_INCOMPLETE   仅在收集中断时出现（OF-19，首行 partial）—— 包残缺的机器可读标记
#
# 输出目录：默认 /var/tmp，可用 `--output-dir=DIR`（或 `--output-dir DIR`）覆盖（OF-14）。
#
# ⚠ v2.0 破坏性变更（批 1 / OF-01 + OF-02）：item_id 改为**显式 ASCII 点分 id**
#   （oracle.<域>.<项>，例 oracle.basic.instance），不再由中文标题派生；
#   SQL 结果文件由 `tables/<中文标题>.tsv` 改为 `tables/<item_id>.psv`。
#   collector_version 与 snapshot.schema_version **同步升到 2.0** ——
#   Python adapter 必须按 collector_version 分叉（1.4 走旧解析、2.0 走新解析）。
#
# ⚠ 契约（下游对接唯一依据）：docs/oracle-collector-interface.md
# ⚠ 改造清单（本文件当前的已知缺陷）：docs/oracle-collector-fix-plan.md
#
#===============================================================================

# 用户误用 `sh` 时自动切换到 Bash（CentOS sh=bash POSIX 模式、Debian sh=dash 都需要处理）
if [ -z "${BASH_VERSION:-}" ] || [ -n "${POSIXLY_CORRECT:-}" ] || { set -o 2>/dev/null | grep -qi '^posix.*on'; }; then
    exec bash "$0" "$@"
fi

set -o pipefail
set +e
umask 077

# 设置 NLS_LANG 确保 Oracle 输出为英文，避免中文乱码
export NLS_LANG="${NLS_LANG:-AMERICAN_AMERICA.AL32UTF8}"
export LANG="${LANG:-en_US.UTF-8}"

# ==================== 配置与全局变量 ====================

# --- 巡检配置 (可外部覆盖) ---
declare -A CONFIG
CONFIG[REPORT_DIR]="/var/tmp"
CONFIG[REPORT_PREFIX]="oracle_inspection"
CONFIG[VERBOSE]=1           # 0=安静模式 1=详细输出
CONFIG[DRY_RUN]=0           # 0=执行 1=仅预览
CONFIG[PAGESIZE]=50000       # SQL分页大小（设足够大避免列头重复）
CONFIG[AWR_MINUTES]=60      # AWR分析时间范围(分钟)
CONFIG[AWR_HOURS]=1         # AWR报告时间范围(小时)，默认最近1小时
CONFIG[AWR_REPORT]=1        # 是否生成AWR报告；默认关闭，需确认 Diagnostics Pack 许可后显式开启
CONFIG[DIAGNOSTICS_PACK]=1  # 1=已确认拥有 Diagnostics Pack 许可，可采集 AWR/ASH/ADDM
CONFIG[ALERT_DAYS]=7        # 告警日志分析天数
CONFIG[ALERT_LOG]=1         # 是否扫描告警日志 1=是 0=否，默认是
CONFIG[CUTOFF_HOURS]=48     # 备份告警阈值(小时)
CONFIG[RMAN_VALIDATE]=0     # 1=本地执行 RESTORE DATABASE VALIDATE（资源开销较大，默认关闭）
CONFIG[SAMPLE_INTERVAL]=5   # 实时采样间隔（秒）
CONFIG[SAMPLE_COUNT]=6      # 实时采样次数（总时长约 30 秒）
CONFIG[SAR_HOURS]=24        # 请求最近 N 小时历史 sar 数据
CONFIG[SAR_ENABLED]=1       # 是否采集 sar 历史 1=是 0=否
CONFIG[STRUCTURED]=1       # 1=额外输出 TSV/JSON 结构化数据包，不影响 Markdown（默认启用）
CONFIG[PACKAGE]=1          # 1=生成 tar.gz 打包（需 --structured）

# --- 环境标志 (自动检测) ---
declare -A ENV
ENV[IS_RAC]=0
ENV[HAS_ASM]=0
ENV[IS_STANDBY]=0
ENV[IS_CDB]=0
ENV[DB_ROLE]=""
ENV[DB_NAME]=""
ENV[INSTANCE_NAME]=""
ENV[DB_OPEN_MODE]=""
ENV[ORA_VERSION]=""
ENV[RAC_NODE_COUNT]=0
ENV[PDB_COUNT]=0
ENV[PDB_LIST]=""

# --- 数据库连接 ---
declare -A DB_CONN
DB_CONN[HOST]="127.0.0.1"
DB_CONN[PORT]="1521"
DB_CONN[SERVICE]="orcl"
DB_CONN[USER]="sys"
DB_CONN[PASS]=""
DB_CONN[SQLPLUS_BIN]=""

# --- 运行时状态 ---
declare -A RUNTIME
RUNTIME[LOG_FILE]=""
RUNTIME[REPORT_FILE]=""
RUNTIME[STATUS_DIR]=""
RUNTIME[TMP_DIR]=""
RUNTIME[START_TIME]=""
RUNTIME[START_EPOCH]=0
RUNTIME[COLLECTION_STARTED_MS]=0
RUNTIME[MODULES_EXECUTED]=0
RUNTIME[MODULES_FAILED]=0
RUNTIME[SQL_QUERY_COUNT]=0
RUNTIME[SQL_FAIL_COUNT]=0

# --- 终态退出码（OF-17 契约）---
#   0 = 全部成功（包已生成，或 --no-package）
#  10 = 参数/环境/认证准备失败（缺 sqlplus、输出目录不可写、空间不足）
#  20 = Oracle 连接失败
#  30 = 敏感信息扫描拦截，已阻止打包
#  31 = 打包失败，任务目录可用
# 130 = 收到 INT/TERM/HUP 中断（由 _trap_cleanup 直接 exit 130）
# 用法：早失败点**直接 exit N**；"已跑完但结果不完整"（打包）写 FINAL_EXIT_CODE，尾部统一 exit。
FINAL_EXIT_CODE=0

# --- 契约版本（OF-21：**单一来源**，不允许再散落硬编码）---
#   v2.0 = 批 1 破坏性变更（item_id 显式化 + tables/*.psv + snapshot.schema 2.0）
COLLECTOR_VERSION="2.0"
SNAPSHOT_SCHEMA_VERSION="2.0"
PACKAGE_VERSION="2.0"

# --- 采集器环境探测结果（OF-20：ora_value 取值的成败标志）---
#   ora_value 的调用点基本都是 `x=$(ora_value …)`（**子 shell**），函数内的变量赋值传不出来，
#   所以成败标志用**文件**传递：`${RUNTIME[STATUS_DIR]}/.ora_value_flag`，1=取值成功、0=命中
#   ORA-/SP2- 错误。调用方用 `ora_value_ok` 读取。这是「查到了 0」与「没查到」的分界线。
ORA_VALUE_FLAG=""

# --- 包内模块日志目录（OF-26：**单一来源**）---
#   `$TASK_DIR/logs/modules/<category>.log`：capture_command / ora_sql_to_md 分类后把**完整原文**
#   落这里；status 的 reason 列只留 5 行摘要。未启用 --structured 时为空串 → module_log_append 直接跳过。
MODULE_LOG_DIR=""

# --- 模块开关 ---
INCLUDE_MODULES=()
EXCLUDE_MODULES=()

# ==================== 使用说明 ====================
# 打印用法与全部命令行选项（含 --diagnostics-pack 许可确认、--structured/--no-structured）
show_usage() {
cat <<EOF
用法:
  $0 [主机] [端口] [服务名] [用户] [密码]
  $0                         (交互式输入)

参数:
  主机      Oracle主机地址   (默认: 127.0.0.1)
  端口      Oracle监听端口   (默认: 1521)
  服务名    Oracle服务名/SID (默认: orcl)
  用户      连接用户名       (默认: sys)
  密码      连接密码 (本地可省略，使用OS认证)

选项:
  --include=MOD1,MOD2   只执行指定模块 (如: --include=pdb,asm)
  --exclude=MOD1,MOD2  跳过指定模块 (如: --exclude=alert,rman)
  --dry-run             仅预览将执行的模块，不实际采集
  --verbose             输出详细执行信息
  --diagnostics-pack    确认已获 Diagnostics Pack 许可，允许 AWR/ASH/ADDM 采集
  --no-diagnostics-pack 禁用所有 AWR/ASH/ADDM 采集（默认）
  --awr-report          生成AWR性能报告（还需 --diagnostics-pack）
  --no-awr-report       不生成AWR报告
  --awr-hours=N         AWR报告时间范围，默认最近1小时 (如: --awr-hours=4)
                        窗口内不足2个快照时，自动回退到最近两个可用快照（报告内注明实际区间）
  --rman-validate       本地执行 RESTORE DATABASE VALIDATE（耗时/IO较高）
  --no-rman-validate    不执行恢复可用性验证（默认）
  --alert-log           扫描告警日志（默认开启）
  --no-alert-log        不扫描告警日志
  --structured          额外输出结构化数据包 (snapshot.json / collection_status.json / tables/*.psv，默认启用)
  --no-structured       不输出结构化包
  --no-package          不生成 tar.gz 打包
  --sar-hours=N         请求最近 N 小时 sar 历史数据（默认24）
  --no-sar              跳过 sar 历史采集
  --output-dir=DIR      输出目录（报告/任务目录/tar.gz 都落这里，默认 /var/tmp）
                        也接受空格写法: --output-dir DIR
  --config=FILE         从配置文件加载参数

本地免密码登录:
  - 本地主机(127.0.0.1/localhost) + 无密码 + oracle用户或ORACLE_HOME已设置
  - 自动使用 sqlplus / as sysdba 操作系统认证
  - 示例: sudo -u oracle ./oracle_inspection.sh (不传密码)

输出文件（目录默认 /var/tmp，可用 --output-dir=DIR 覆盖）:
  DIR/<prefix>_<主机>_<端口>_<时间>.md              人读 Markdown 报告
  DIR/<prefix>_<主机>_<端口>_<时间>.log             运行日志（**不进包**）
  DIR/<prefix>_task_<tag>_<时间>/                   结构化任务目录，含
      snapshot.json / collection_status.json / summary.txt /
      status/ / tables/ / evidence/ / timeseries/ / history/ / logs/
      evidence/db_reports/awr_report.html           AWR HTML 报告（DB 原生报告原件；仅 --diagnostics-pack 时生成，随包回传）
  DIR/<prefix>_task_<tag>_<时间>.tar.gz             回传包（tar 整个任务目录）

中断语义:
  收集中途被 INT/TERM/HUP 打断时，任务目录内会出现 COLLECTION_INCOMPLETE
  （首行 partial），退出码 130 —— 下游据此**程序化**判定"包残缺"。

退出码:
   0    全部成功（包已生成，或 --no-package）
  10    参数/环境/认证准备失败（缺 sqlplus、输出目录不可写、空间不足）
  20    Oracle 连接失败
  30    敏感信息扫描拦截，已阻止打包
  31    打包失败，任务目录可用
  40    模块策略跳过（**模块内部返回码，不是脚本终态**）：模块跑了、但按许可/前置条件
       没有采集（如 AWR 报告未确认 Diagnostics Pack）。由 run_module 拦下记 not_enabled，
       计入「未启用」分档、**不计入失败数**，也不写「采集异常」。
 130    收到 INT/TERM/HUP 中断
EOF
}

# 解析命令行选项
# 解析命令行与外部环境变量，落到 CONFIG / DB_CONN；处理 --include/--exclude 模块开关。
# 位置参数顺序：host port service user password（缺省走交互式输入）。
parse_args() {
    local remaining_args=()
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --include=*)
                IFS=',' read -ra INCLUDE_MODULES <<< "${1#*=}"
                shift
                ;;
            --exclude=*)
                IFS=',' read -ra EXCLUDE_MODULES <<< "${1#*=}"
                shift
                ;;
            --dry-run)
                CONFIG[DRY_RUN]=1
                CONFIG[VERBOSE]=1
                shift
                ;;
            --verbose)
                CONFIG[VERBOSE]=1
                shift
                ;;
            --awr-hours=*)
                CONFIG[AWR_HOURS]="${1#*=}"
                shift
                ;;
            --diagnostics-pack)
                CONFIG[DIAGNOSTICS_PACK]=1
                shift
                ;;
            --no-diagnostics-pack)
                CONFIG[DIAGNOSTICS_PACK]=0
                CONFIG[AWR_REPORT]=0
                shift
                ;;
            --no-awr-report)
                CONFIG[AWR_REPORT]=0
                shift
                ;;
            --awr-report)
                CONFIG[AWR_REPORT]=1
                shift
                ;;
            --rman-validate)
                CONFIG[RMAN_VALIDATE]=1
                shift
                ;;
            --no-rman-validate)
                CONFIG[RMAN_VALIDATE]=0
                shift
                ;;
            --no-alert-log)
                CONFIG[ALERT_LOG]=0
                shift
                ;;
            --alert-log)
                CONFIG[ALERT_LOG]=1
                shift
                ;;
            --structured)
                CONFIG[STRUCTURED]=1
                shift
                ;;
            --no-structured)
                CONFIG[STRUCTURED]=0
                shift
                ;;
            --no-package)
                CONFIG[PACKAGE]=0
                shift
                ;;
            --sar-hours=*)
                CONFIG[SAR_HOURS]="${1#*=}"
                shift
                ;;
            --no-sar)
                CONFIG[SAR_ENABLED]=0
                shift
                ;;
            # ★ OF-14：输出目录（报告 / 任务目录 / tar.gz 全落这里）。
            #   两种写法都收：`--output-dir=DIR` 与 `--output-dir DIR`（空格写法要防住
            #   "选项在末尾且无值" 的情况，不能无条件 shift）。**不引入 $(dirname "$0")**
            #   自引用 —— 采集脚本不得依赖自身所在目录（AGENTS.md）。
            --output-dir=*)
                CONFIG[REPORT_DIR]="${1#*=}"
                shift
                ;;
            --output-dir)
                shift
                if [ $# -gt 0 ]; then CONFIG[REPORT_DIR]="$1"; shift; fi
                ;;
            --config=*)
                load_config "${1#*=}"
                shift
                ;;
            -h|--help)
                show_usage
                exit 0
                ;;
            *)
                remaining_args+=("$1")
                shift
                ;;
        esac
    done
    if [[ ${#remaining_args[@]} -ge 1 ]]; then
        DB_CONN[HOST]="${remaining_args[0]}"
    fi
    if [[ ${#remaining_args[@]} -ge 2 ]]; then
        DB_CONN[PORT]="${remaining_args[1]}"
    fi
    if [[ ${#remaining_args[@]} -ge 3 ]]; then
        DB_CONN[SERVICE]="${remaining_args[2]}"
    fi
    if [[ ${#remaining_args[@]} -ge 4 ]]; then
        DB_CONN[USER]="${remaining_args[3]}"
    fi
    if [[ ${#remaining_args[@]} -ge 5 ]]; then
        DB_CONN[PASS]="${remaining_args[4]}"
    fi
}

# 读取可选的外部配置文件（若存在）覆盖 CONFIG 默认值
load_config() {
    local cfg_file="$1"
    [[ ! -f "$cfg_file" ]] && return 1
    while IFS='=' read -r key value; do
        [[ "$key" =~ ^#.*$ || -z "$key" ]] && continue
        key=$(echo "$key" | xargs)
        value=$(echo "$value" | xargs)
        if [[ -n "$value" ]]; then
            CONFIG[$key]="$value"
        fi
    done < "$cfg_file"
}

# ==================== 日志与输出函数 ====================

# 写日志文件
# 向运行日志追加一行（带时间戳）
log_write() {
    local level="${1:-INFO}"
    local msg="${2:-}"
    [[ -z "$msg" ]] && return
    local timestamp
    timestamp=$(date +'%Y-%m-%d %H:%M:%S' 2>/dev/null || echo "now")
    printf "[%s] [%s] %s\n" "$timestamp" "$level" "$msg" >> "${RUNTIME[LOG_FILE]}"
}

# 进度输出 (verbose模式同时输出到stdout)
# 向终端打印进度行；CONFIG[VERBOSE]=0 时静默
progress() {
    local msg="$1"
    log_write "INFO" "$msg"
    [[ "${CONFIG[VERBOSE]}" -eq 1 ]] && printf "[%s] %s\n" "$(date +'%Y-%m-%d %H:%M:%S')" "$msg"
}

# 警告输出
# 打印并记录警告
log_warn() {
    local msg="$1"
    log_write "WARN" "$msg"
    [[ "${CONFIG[VERBOSE]}" -eq 1 ]] && printf "[WARN] %s\n" "$msg"
}

# 错误输出
# 打印并记录错误
log_error() {
    local msg="$1"
    log_write "ERROR" "$msg"
    printf "[ERROR] %s\n" "$msg" >&2
}

# ==================== 工具函数 ====================
# 命令是否存在（用于能力探测，避免硬依赖）
has_cmd() { command -v "$1" >/dev/null 2>&1; }

# ★ 给外部命令套超时（OF-08）。`timeout` 不可用时降级为裸跑（但会留痕）。
#   动机：run_module 是**同步**调用，一条 hung 命令会拖死整包（只能靠人工 Ctrl-C 收场）。
#   最危险的是 `lsnrctl status`：监听器 hung / 防火墙 DROP 时会长时间阻塞在 TCP 重传上。
run_with_timeout() {
    local seconds="$1"; shift
    if has_cmd timeout; then timeout --signal=TERM --kill-after=5 "$seconds" "$@"; else "$@"; fi
}

# 向 Markdown 报告追加一行文本
md_line()        { printf '%s\n' "$1" >> "${RUNTIME[REPORT_FILE]}"; }
# Markdown 代码块起始围栏 ```
md_block_start() { printf '%s\n' '```' >> "${RUNTIME[REPORT_FILE]}"; }
# Markdown 代码块结束围栏 ```
md_block_end()   { printf '%s\n' '```' >> "${RUNTIME[REPORT_FILE]}"; }

# ★ 把任意字符串压成 [A-Za-z0-9._-]（其余字符→_）。
#   ⚠ **会吃掉全部中文** → 仅可用于「文件名/日志名」，**禁止用于 item_id**。
#     批 1（OF-01）之后所有 item_id 都是显式 ASCII，本函数对它们恒等；中文只出现在
#     Markdown 标题里，**不再参与任何 id 派生**。
sanitize_filename() {
    printf '%s' "$1" | tr -c 'A-Za-z0-9._-' '_' | sed -e 's/__*/_/g' -e 's/^_//' -e 's/_$//'
}

# ⚠ 已删除的 `tsv_filename()`（v2.0，批 1）：它保留中文，是 `tables/<中文标题>.tsv`
#   命名规则的来源。v2.0 起结构化表一律用 ASCII item_id 命名（tables/<item_id>.psv），
#   该函数已无调用点 —— 保留它只会诱使后来者重新引入"按中文标题派生文件名"的塌陷路径。

# 获取本机所有非回环 IPv4
collect_all_ipv4() {
    if has_cmd ip; then
        ip -o -4 addr show scope global 2>/dev/null | awk '{split($4,a,"/"); if(a[1] != "127.0.0.1") print a[1]}' | sort -u | paste -sd, -
    elif has_cmd hostname; then
        hostname -I 2>/dev/null | tr ' ' '\n' | awk '/^[0-9]+([.][0-9]+){3}$/ && $0 != "127.0.0.1"' | sort -u | paste -sd, -
    fi
}

# 获取本机主 IPv4
detect_primary_ipv4() {
    local v=""
    if has_cmd ip; then
        v=$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="src"){print $(i+1); exit}}')
        case "$v" in 127.*) v="" ;; esac
        [ -z "$v" ] && v=$(ip -o -4 route show default 2>/dev/null | awk 'NR==1{for(i=1;i<=NF;i++) if($i=="src"){print $(i+1); exit}}')
        case "$v" in 127.*) v="" ;; esac
        [ -z "$v" ] && v=$(ip -o -4 addr show scope global 2>/dev/null | awk '{split($4,a,"/"); if(a[1] !~ /^127[.]/){print a[1]; exit}}')
    fi
    [ -z "$v" ] && v=$(collect_all_ipv4 | awk -F, '{print $1}')
    printf '%s' "${v:-unknown_ip}"
}

# 把 --host 解析成 IPv4（OF-06）。
#   · 回环 → 恒返回 127.0.0.1（不返回 ""，否则下游 `[ -n ]` 判断会漏）
#   · 已是点分十进制 → 原样返回
#   · 主机名/FQDN → getent（走 nsswitch，能命中 /etc/hosts），退回 host
#   解析不出返回空串，调用方须用 `:-` 兜底成原字符串。
resolve_ipv4() {
    local host="$1" v=""
    case "$host" in 127.*|localhost|::1) printf '127.0.0.1'; return 0 ;; esac
    if printf '%s' "$host" | grep -Eq '^[0-9]+([.][0-9]+){3}$'; then printf '%s' "$host"; return 0; fi
    if has_cmd getent; then v=$(getent ahostsv4 "$host" 2>/dev/null | awk 'NR==1{print $1}'); fi
    [ -z "$v" ] && has_cmd host && v=$(host "$host" 2>/dev/null | awk '/has address/{print $NF; exit}')
    printf '%s' "$v"
}

# 判定「连接目标是否就是本机」（OF-06 的核心）。
#   三路比对（任一命中即本机）：① 回环；② 主机名/FQDN 与本机一致；③ 解析出的 IP 在本机全局 IPv4 里。
#   **这是 OS 数据能否归属目标库的唯一依据**：返回非 0 时，system.* / timeseries.* 采到的全部是
#   采集机数据，不得参与目标库资源评价（AGENTS.md 硬规则）。
is_local_connect_target() {
    local host="$1" resolved="$2" short fqdn ips
    case "$host" in 127.*|localhost|::1) return 0 ;; esac
    short=$(hostname -s 2>/dev/null || hostname 2>/dev/null)
    fqdn=$(hostname -f 2>/dev/null || true)
    if [ "$host" = "$short" ] || { [ -n "$fqdn" ] && [ "$host" = "$fqdn" ]; }; then return 0; fi
    ips=",$(collect_all_ipv4),"
    if [ -n "$resolved" ] && printf '%s' "$ips" | grep -Fq ",$resolved,"; then return 0; fi
    return 1
}

# 把 TAB/CR/LF 压成空格并压缩多空格（用于 status 的 reason 列，保证 10 列不被撑破）
sanitize_text() {
    printf '%s' "${1-}" | tr '\t\r\n' '   ' | sed 's/[[:space:]][[:space:]]*/ /g'
}

# 命令输出脱敏
redact_command_stream() {
    sed -E \
      -e 's/((--password|--passwd)(=|[[:space:]]+))[^[:space:]]+/\1<REDACTED>/Ig' \
      -e 's/(^|[[:space:]])-p[^[:space:]]+/\1-p<REDACTED>/g' \
      -e 's/((token|secret|access[_-]?key)(=|:))[[:graph:]]+/\1<REDACTED>/Ig' \
      -e 's#(sqlplus|oracle)://([^:/@]+):[^@/]+@#\1://\2:<REDACTED>@#Ig'
}

# 当前时间，ISO8601（带时区）
iso_now() {
    date --iso-8601=seconds 2>/dev/null || date +'%Y-%m-%dT%H:%M:%S%z'
}

# 当前墙钟毫秒（date +%s%3N 不可用时退化为秒×1000）
epoch_ms() {
    local v
    v=$(date +%s%3N 2>/dev/null)
    case "$v" in
        *N*|'') printf '%s000' "$(date +%s)" ;;
        *) printf '%s' "$v" ;;
    esac
}

# 单调毫秒（读 /proc/uptime，用于测时长，不受系统时间跳变影响）
monotonic_ms() {
    awk '{printf "%.0f", $1 * 1000}' /proc/uptime 2>/dev/null || epoch_ms
}

# 判断入参是否为非负整数
is_uint() {
    case "${1-}" in ''|*[!0-9]*) return 1 ;; *) return 0 ;; esac
}

# 自动分类失败原因
# ★ 按 rc / 错误文本把失败归类（状态取值全集 10 个，与 MySQL 对齐）：
#     rc=124|137 → timeout；daemon 未起 → not_enabled；权限类 → permission_denied；
#     ORA-00904 类（列/标识符不存在）→ **schema_mismatch**；环境真不支持 → unsupported；其余 → error。
#   ★ OF-03：`schema_mismatch` 必须排在 `unsupported` **之前** ——
#     ORA-00942（表/视图不存在）= 环境不支持；ORA-00904（无效标识符）+ ORA-01722（无效数字）
#     = **脚本 SQL 与本版本列集不匹配 = 采集器自身缺陷**。两者混为一谈时，脚本写错列名会被
#     伪装成"该环境采不到此项"，既不告警也不提示，缺陷永不浮出水面（MySQL 侧 F-21 同坑）。
classify_failure() {
    local rc="$1" err_file="$2"
    if [ "$rc" -eq 124 ] || [ "$rc" -eq 137 ]; then printf 'timeout'; return; fi
    # ★ N-13：监听器**未运行**（本机 bequeath 采集 + listener 没起）不是"采集失败"，
    #   语义是"该服务未启用"。lsnrctl 的三段式报错是 TNS-12541 / TNS-12560 / TNS-00511。
    #   不认这三行，`system.listener_status` 会判成 error，凭空给采集质量扣分、并误导
    #   现场以为"采集脚本坏了"。
    if grep -Eqi 'cannot talk to daemon|could not open connection|daemon is not running|service.*not (running|active)|No such file|TNS-12541|TNS-00511|no listener' "$err_file" 2>/dev/null; then
        printf 'not_enabled'
    elif grep -Eqi 'ORA-01031|ORA-00942.*insufficient|access denied|command denied|permission denied|requires.*privilege|you need.*privilege' "$err_file" 2>/dev/null; then
        printf 'permission_denied'
    elif grep -Eqi 'ORA-00904|ORA-01722|invalid identifier|invalid number' "$err_file" 2>/dev/null; then
        printf 'schema_mismatch'
    elif grep -Eqi "doesn.t exist|unknown table|unknown system variable|unknown column|not supported|unsupported|ORA-00942|ORA-00957|ORA-01403" "$err_file" 2>/dev/null; then
        printf 'unsupported'
    else
        printf 'error'
    fi
}

# 该 status 取值是否算"失败"（用于 SQL 失败计数与汇总口径，单一来源）
is_failure_status() {
    case "${1-}" in
        error|timeout|permission_denied|unsupported|schema_mismatch|not_enabled) return 0 ;;
        *) return 1 ;;
    esac
}

# 记录单个采集项状态
# ★ 状态登记：写 status/<item_id>.tsv（10 列制表符，**覆盖写** —— 一个 item_id 一个文件）。
#   列：item_id category status started_at finished_at duration_ms row_count exit_code output_file reason。
#   ★ OF-01 之后 item_id 一律由调用方**显式传入 ASCII 点分 id**（oracle.<域>.<项>），
#     与 MySQL 的 mysql.<域>.<项> 对齐；sanitize_filename 对纯 ASCII 是恒等变换。
#   历史教训（v1.4 及以前）：item_id 由中文标题派生 → 48 项塌到 `sql.`、27 模块塌到 `module.`，
#     整份台账被覆盖写而脚本照常出包不报错。**任何新增采集项都必须自带 ASCII item_id。**
record_status() {
    local item_id="$1" category="$2" status="$3" started_at="$4" finished_at="$5" duration_ms="$6"
    local row_count="$7" exit_code="$8" output_file="$9" reason="${10-}"
    local f
    f="${RUNTIME[STATUS_DIR]}/$(sanitize_filename "$item_id").tsv"
    printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
      "$item_id" "$category" "$status" "$started_at" "$finished_at" "$duration_ms" "$row_count" "$exit_code" \
      "$output_file" "$(sanitize_text "$reason")" > "$f"
}

# 把绝对路径转成相对 TASK_DIR 的路径（写进 status 的 output_file 列）
safe_relpath() { printf '%s' "${1#"$TASK_DIR"/}"; }

# ★ N-19 / 批 7：给**集合型采集项**挑一个"存在的**文件**"作为 output_file。
#   契约（见 interface §4 / §6.5）：output_file 必须是**包内真实存在的文件路径**，否则下游按包根
#   解析必然 miss（与 OF-30 同类）。而 `system.sar_history` / `timeseries.realtime_sampling` 这类
#   一次产出多个文件（甚至一个子目录）的采集项，原先直接写目录 `history/` / `timeseries/`
#   —— ① 违反"指向文件"的契约；② 判 empty / unsupported 时那个目录里可能根本没有产物。
#   这里按**候选顺序**取第一个真实存在的文件，全都不存在则返回空串（与 unsupported/skipped
#   写 `""` 的既有惯例一致，下游见空串即知"本项无产物文件"）。
first_existing_relpath() {
    local f
    for f in "$@"; do
        [ -f "$f" ] && { safe_relpath "$f"; return 0; }
    done
    printf ''
}

# ★ 把一条采集项的**完整错误原文**追加进包内模块日志（OF-26）。
#   动机：capture_command / ora_sql_to_md 分类后要把临时错误文件删掉，status 里只留
#   `tail -n 5` 的摘要 —— ORA- 原文、权限报错的完整栈就此丢失，事后排查只剩一句话。
#   参数：<category> <item_id> <rc> <源文件>；源文件为空/不存在则静默跳过。
#   ⚠ 未启用结构化包（--no-structured）时 MODULE_LOG_DIR 为空 → 直接返回，不影响主流程。
module_log_append() {
    local category="$1" item_id="$2" rc="$3" src="$4" lg
    [ -n "${MODULE_LOG_DIR:-}" ] || return 0
    [ -n "$src" ] && [ -s "$src" ] || return 0
    [ -d "$MODULE_LOG_DIR" ] || mkdir -p "$MODULE_LOG_DIR" 2>/dev/null || return 0
    lg="${MODULE_LOG_DIR}/$(sanitize_filename "${category:-misc}").log"
    {
        printf '===== %s | item=%s rc=%s =====\n' "$(date +'%Y-%m-%d %H:%M:%S')" "$item_id" "${rc:-?}"
        cat "$src"
        printf '\n'
    } >> "$lg" 2>/dev/null
    return 0
}

# ★ 执行一条外部命令，原始输出落 evidence/，同时登记一条 status。
#   容错（有意为之）：stderr 为空时**从 stdout 借前 500 字节**做分类（chronyc/ntpq 把错误写 stdout）。
#   ★ OF-26：删除临时错误文件**之前**，先把完整原文追加进包内模块日志 `logs/modules/<category>.log`；
#     status 的 reason 列仍只保留 `tail -n 5` 摘要（下游要一句话，排查要全文，两者都需要）。
capture_command() {
    local item_id="$1" category="$2" outfile="$3"; shift 3
    local start_iso end_iso start_ms end_ms rc status rows reason err
    start_iso=$(iso_now); start_ms=$(epoch_ms)
    err="${RUNTIME[STATUS_DIR]}/.$(sanitize_filename "$item_id").stderr"
    mkdir -p "$(dirname "$outfile")"
    "$@" > "$outfile" 2> "$err"; rc=$?
    end_iso=$(iso_now); end_ms=$(epoch_ms)
    status="ok"; reason=""
    if [ "$rc" -ne 0 ]; then
        if [ ! -s "$err" ] && [ -s "$outfile" ]; then
            head -c 500 "$outfile" >> "$err" 2>/dev/null
        fi
        status=$(classify_failure "$rc" "$err"); reason=$(tail -n 5 "$err" 2>/dev/null | tr '\n' ' ')
        # 失败时的落盘口径（三库统一）：表格类（*.tsv/*.psv/*.csv）残缺比没有更危险 ——
        #   下游按行列取值看不出少了行，故清空，让"没采到"只由 collection_status 表达；
        #   纯文本证据类（evidence/*.txt）保留已输出的半截，便于事后定位。
        case "$outfile" in
          *.tsv|*.psv|*.csv) : > "$outfile" ;;
          *) : ;;
        esac
    elif [ ! -s "$outfile" ]; then status="empty"; fi
    rows=$(wc -l < "$outfile" 2>/dev/null | tr -d ' '); rows=${rows:-0}
    # OF-26：非 ok 且 stderr 非空 → 完整原文进包内模块日志（此后才删临时文件）
    if [ "$status" != "ok" ]; then module_log_append "$category" "$item_id" "$rc" "$err"; fi
    rm -f "$err"
    record_status "$item_id" "$category" "$status" "$start_iso" "$end_iso" "$((end_ms-start_ms))" "$rows" "$rc" "$(safe_relpath "$outfile")" "$reason"
    return "$rc"
}

# ★ OF-14：输出目录可写性检查（提前失败，退出码 10）。
#   --output-dir 让现场可以把产物落到挂载盘/专用目录；目录不存在会先建，建不出或不可写就
#   立即终止，避免后面每个采集项各报一次 "Permission denied"，把真正的失败原因埋掉。
#   返回 0=可用；10=不可用（调用方 `|| exit 10`）。
ensure_output_dir() {
    local dir="$1"
    mkdir -p "$dir" 2>/dev/null
    if [ ! -d "$dir" ] || [ ! -w "$dir" ]; then
        printf "ERROR: 输出目录不存在或不可写: %s（用 --output-dir=DIR 指定可写目录）\n" "$dir" >&2
        return 10
    fi
    return 0
}

# 磁盘空间预检
# 检查输出目录剩余空间，不足时提前告警
check_output_space() {
    local free_kb
    free_kb=$(df -Pk "${CONFIG[REPORT_DIR]:-/var/tmp}" 2>/dev/null | awk 'NR==2{print $4}')
    if is_uint "$free_kb" && [ "$free_kb" -lt $((200*1024)) ]; then
        log_error "输出目录剩余空间不足: 需要至少 200 MB，当前约 $((free_kb/1024)) MB"
        return 1
    fi
    return 0
}

# 信号处理 — 写入未完成标记后退出
declare -a BG_PIDS BG_IDS BG_NAMES BG_START_ISO BG_START_MS
# 信号清理（INT/TERM/HUP）：杀后台模块 → 写未完成标记 → 报告追加提示 → 清临时目录。
#   ★ OF-19：除在 Markdown 里追加一行，还要**程序化**标记包残缺 —— 在 TASK_DIR 内落
#     `COLLECTION_INCOMPLETE`（首行 `partial`，第二行给时间戳与信号），下游据此判"包不完整"。
#   ⚠ trap 在本行安装，而 `TASK_DIR` 要到主流程（约 L4155）才赋值 ⇒ 必须 `:-` 兜底 +
#     目录存在性守卫，否则早期触发（如参数阶段收到 TERM）会往根目录写文件。
#   ⚠ 三个 trap 分别传信号名，不能写成 `trap _trap_cleanup INT TERM HUP`（那样拿不到是哪个信号）。
_trap_cleanup() {
    local sig="${1:-INT}"
    [ -n "${BG_PIDS[*]:-}" ] && kill "${BG_PIDS[@]}" 2>/dev/null || true
    if [ -n "${TASK_DIR:-}" ] && [ -d "$TASK_DIR" ]; then
        {
            printf 'partial\n'
            printf 'interrupted_at=%s signal=%s\n' "$(date +'%Y-%m-%dT%H:%M:%S%z')" "$sig"
        } > "$TASK_DIR/COLLECTION_INCOMPLETE" 2>/dev/null
    fi
    if [ -n "${RUNTIME[REPORT_FILE]:-}" ] && [ -f "${RUNTIME[REPORT_FILE]}" ]; then
        printf '\n> ⚠️ 采集被中断，报告不完整\n' >> "${RUNTIME[REPORT_FILE]}"
    fi
    if [ -n "${RUNTIME[TMP_DIR]:-}" ] && [ -d "${RUNTIME[TMP_DIR]}" ]; then
        rm -rf "${RUNTIME[TMP_DIR]}" 2>/dev/null
    fi
    exit 130
}
trap '_trap_cleanup INT'  INT
trap '_trap_cleanup TERM' TERM
trap '_trap_cleanup HUP'  HUP

# ==================== 模块开关检查 ====================
# 判断某模块是否被 --include/--exclude 允许执行
is_module_enabled() {
    local module_name="$1"
    # 检查是否在排除列表中
    local ex
    for ex in "${EXCLUDE_MODULES[@]}"; do
        [[ "$module_name" == "$ex" ]] && return 1
    done
    # 检查是否在包含列表中 (如果列表非空)
    if [[ ${#INCLUDE_MODULES[@]} -gt 0 ]]; then
        local inc
        for inc in "${INCLUDE_MODULES[@]}"; do
            [[ "$module_name" == "$inc" ]] && return 0
        done
        return 1
    fi
    return 0
}

# ==================== Oracle SQL 执行辅助 ====================

# 获取连接串
# 支持本地免密码登录 (sqlplus / as sysdba)
# 构造 sqlplus 连接串（本地 OS 认证用 `/ as sysdba`；远端用 user/pass@host:port/service）。
#   注：函数名古怪但**不是笔误**——历史名，保留以免动到调用方。
#   ⚠ OF-10 后本函数**只被 ora_exec 调用一次**（L563），返回值经管道进 sqlplus 的 stdin，
#     **不再作为命令行参数**；全脚本再无 `-S "$(get_ora_conn)"` 形态的调用点。
get_ora_conn() {
    local user="${DB_CONN[USER]}"
    local pass="${DB_CONN[PASS]}"
    local host="${DB_CONN[HOST]}"
    local port="${DB_CONN[PORT]}"
    local svc="${DB_CONN[SERVICE]}"

    # 本地免密码检测: 本地主机 + 无密码 + (ORACLE_HOME已设置 或 用户是oracle)
    local is_local_host=""
    case "$host" in
        127.0.0.1|localhost|"") is_local_host=1 ;;
    esac
    local can_os_auth=0
    if [[ -n "$ORACLE_HOME" ]] || [[ "$(id -un)" == "oracle" ]]; then
        can_os_auth=1
    fi

    # 本地免密码登录条件
    if [[ -n "$is_local_host" ]] && [[ -z "$pass" ]] && [[ "$can_os_auth" -eq 1 ]]; then
        if printf '%s' "$user" | grep -qi "^sys$"; then
            echo "/ as sysdba"
        else
            # 非sys用户也用操作系统认证
            echo "/"
        fi
    else
        # 标准密码认证
        if printf '%s' "$user" | grep -qi "^sys$"; then
            echo "${user}/${pass}@//${host}:${port}/${svc} as sysdba"
        else
            echo "${user}/${pass}@//${host}:${port}/${svc}"
        fi
    fi
}

# ★ 统一 SQL 执行入口：把"连接段"从 **命令行参数** 挪到 **标准输入**（OF-10）。
#   背景：`sqlplus -S "user/pass@…"` 的明文口令会出现在进程 argv 里，
#         采集期间同机任何用户执行 `ps -ef` 都能看到 —— 这也是 MySQL 侧早已规避的问题。
#   做法：`sqlplus -S /nolog` 启动，连接段作为 stdin 的首行 `CONNECT …` 送进去。
#         `-S` 抑制 banner；`SET FEEDBACK OFF` 抑制 CONNECT 成功后的 "Connected." 回显，
#         保证本函数对调用方"零输出污染"（ora_value 依赖 stdout 纯净）。
#   用法：调用方把 SQL 正文用 heredoc 喂进来，重定向与退出码语义**完全不变**：
#        ora_exec > out.txt 2> err.txt <<EOF
#        SET PAGESIZE 0 … ; <SQL> ; EXIT;
#        EOF
#   ⚠ 与 argv 方式的唯一行为差异：CONNECT 失败时 sqlplus 不再立即以非零码退出，
#     而是把 ORA- 错误打到 stdout 后继续。正常流程下连接已在主流程前做过连接测试，
#     采集阶段不会触发该分支。
ora_exec() {
    {
        printf 'SET ECHO OFF\nSET FEEDBACK OFF\nSET HEADING OFF\n'
        printf 'CONNECT %s\n' "$(get_ora_conn)"
        cat
    } | "${DB_CONN[SQLPLUS_BIN]}" -S /nolog
}

# 静默执行，容错版（不影响脚本继续）
# 执行一段 SQL 并把结果原样输出（已设 COLSEP/TRIMSPOOL 等；不关心成败）
ora_try() {
    local sql_log="${RUNTIME[LOG_FILE]:-$(mktemp 2>/dev/null || echo /dev/null)}"
    if [[ "${CONFIG[VERBOSE]}" -eq 1 ]]; then
        ora_exec 2>> "$sql_log" <<EOF
SET PAGESIZE 0
SET FEEDBACK OFF
SET HEADING OFF
SET ECHO OFF
SET TRIMSPOOL ON
SET LINESIZE 32767
WHENEVER SQLERROR CONTINUE
$1
EXIT;
EOF
    else
        ora_exec 2>/dev/null <<EOF
SET PAGESIZE 0
SET FEEDBACK OFF
SET HEADING OFF
SET ECHO OFF
SET TRIMSPOOL ON
SET LINESIZE 32767
WHENEVER SQLERROR CONTINUE
$1
EXIT;
EOF
    fi
}

# 单值查询 (传入完整 SELECT 语句)
# ★ OF-20：ora_try 带 `WHENEVER SQLERROR CONTINUE`，**错误文本会混进 stdout**。
#   原先只 `tr -d ' \r\n'` → `ORA-00942:tableorviewdoesnotexist` 被拿去 `[ -gt 0 ]`，
#   既报 `integer expression expected`，又把「没权限查 v$asm_diskgroup」静默读成「没有 ASM」。
#   现在：命中 ORA-/SP2-/ERROR 行 → 返回空串并把**成败标志写文件**（调用点多在子 shell 里，
#   变量传不出来），调用方用 `ora_value_ok` 区分「查到了 0」与「没查到」。
ora_value() {
    local out
    out=$(ora_try "SELECT $1;" 2>/dev/null | tr -d '\r')
    if printf '%s' "$out" | grep -qEi '^[[:space:]]*(ORA-[0-9]+|SP2-[0-9]+|ERROR[[:space:]:])'; then
        printf '0' > "$(ora_value_flag_file)" 2>/dev/null
        printf ''
        return 0
    fi
    printf '1' > "$(ora_value_flag_file)" 2>/dev/null
    printf '%s' "$out" | tr -d ' \n'
}

# 最近一次 ora_value 的成败标志文件路径
ora_value_flag_file() { printf '%s' "${RUNTIME[STATUS_DIR]:-/tmp}/.ora_value_flag"; }

# 最近一次 ora_value 是否成功取得值（跨子 shell，故读文件）
ora_value_ok() { [ "$(cat "$(ora_value_flag_file)" 2>/dev/null)" = "1" ]; }

# ★ 把 sqlplus 的竖线输出规范化为**可解析 psv**（pipe-separated values）。
#   为什么需要它（OF-02，真实包实测 `tables/实例信息.tsv`）：
#     sqlplus 的实际输出是   空行 / 列名行 / 一行纯 `-` 分隔线 / 数据行 / 空行，
#     且每个字段按 `COLUMN … FORMAT aNN` 右填充 —— 填充是**空格与 TAB 混合**，列宽不固定。
#   本函数做三件事：① 去空行；② 去掉纯横线分隔行（长度>4 才算，避免误删单列值为 `-` 的数据行）；
#   ③ 把竖线两侧的填充空白全部剥掉 → 下游 `awk -F'|'` 直接可用，无需再 strip。
#   ⚠ 已知边界：值内部若含 `|` 会被当分隔符 —— 已由「SQL 字面量不得含竖线」守门杜绝
#     （`tools/check_oracle_psv_widths.py`；2.0 会连两侧空白一起压掉，`a | b` → `a|b`，
#     第二个值被静默丢进 csv 的 None 列；1.4 不压空白所以一直看不出来）。
#     折行则由「宽度 ≥ SQL 截断长度 + 全脚本无 `WORD_WRAPPED`」结构性消除，
#     存量包由解析层 `package_adapter._rejoin_wrapped_rows` 兜底回并。
psv_normalize() {
    sed -e 's/\r$//' "$1" 2>/dev/null \
      | sed -e 's/[[:space:]]*|[[:space:]]*/|/g' -e 's/[[:space:]]*$//' \
      | awk 'NF==0{next} /^[-|]+$/{if(length($0)>4) next} {print}'
}

# ★★ 核心采集引擎（支配 **136 个 SQL 采集项**）——
#   签名：ora_sql_to_md <item_id> <标题> <SQL>
#     · item_id 由调用方**显式传入** ASCII 点分 id（oracle.<域>.<项>）；不再从中文标题派生（OF-01）。
#     · **只开一次 sqlplus 连接**（OF-04）：结果先落临时文件，md 与 psv 由同一份数据渲染，
#       杜绝"报告里 12 个会话 / 包里 9 个"这类两处数字打架（136 项 = 272→136 次连接）。
#     · 结构化产物 = `tables/<item_id>.psv`，且 status 的 output_file 列写**同一路径**（实测-2：
#       原 tables/ 用中文名、status/ 用 sanitize 名，两个入口永不相等 → 断链）。
#     · 空结果写自描述占位 `# … rows=0` 且 status=**empty**（实测-1：原判据 `grep 'no rows selected'`
#       读的是 stderr，而该消息在 stdout → empty 永不触发，34 个空结果全被记成 ok）。
#     · row_count 记真实数据行数（OF-22：原先 136 项恒为 `-`）。
#     · ⚠ 下方 heredoc **故意不加引号**（必须展开 $sql / ${page_size}）⇒ 正文里**禁止出现反引号与裸 `$`**：
#       bash 会对未加引号的 heredoc 做参数展开/命令替换 —— 反引号会被当真命令执行
#       （曾写成 `选 `|` 而非 TAB`，bash 执行了 `|` 并报 `syntax error near unexpected token`，
#       该错误落进 stderr → 正常项的状态从 ok/empty 被打成 error）；`v$parameter` 会被吞成 `v`。
#       SQL / 注释里的 `$` 一律写成 `\$`。静态守门见 _work/_b1_make_verify.py 的 T10。
ora_sql_to_md() {
    local item_id="$1"
    local title="$2"
    local sql="$3"
    local page_size="${CONFIG[PAGESIZE]:-200}"
    local start_iso start_ms end_iso end_ms rc rows status reason
    local tmp err_file structured=0 psv_file="report"

    err_file="${RUNTIME[STATUS_DIR]}/.$(sanitize_filename "$item_id").stderr"
    tmp="${RUNTIME[STATUS_DIR]}/.$(sanitize_filename "$item_id").out"
    start_iso=$(iso_now); start_ms=$(epoch_ms)

    if [ "${CONFIG[STRUCTURED]}" -eq 1 ] && [ -n "$TABLES_DIR" ]; then
        structured=1
        psv_file="tables/${item_id}.psv"
        page_size=50000   # 结构化：一页到底，不重复列头（供 psv 解析）
    fi

    if [ -n "$title" ]; then
        md_line ""
        md_line "### ${title}"
    fi

    # ── 唯一一次 sqlplus 连接：输出落 tmp（md 与 psv 共用）──
    ora_exec > "$tmp" 2> "$err_file" <<EOF
WHENEVER SQLERROR CONTINUE
SET LINESIZE 32767
SET PAGESIZE ${page_size}
SET FEEDBACK OFF
SET HEADING ON
SET ECHO OFF
SET TRIMSPOOL ON
SET TRIMOUT ON
SET WRAP ON
SET NULL '-'
SET COLSEP '|'
-- ★ OF-02：分隔符选竖线而非 TAB —— sqlplus 无法输出裸 TAB，这是绕开限制的
--   近似方案（错在**命名**，不在分隔符选择本身）。文件名后缀随之改为 .psv。
-- 限制宽列显示宽度，避免 VARCHAR2(4000) 列撑爆输出
-- ★★ 宽度必须 **≥ SQL 侧对该列的截断长度**，否则 sqlplus 会把它**折行**——
--    折行后 .psv 里就多出一条「首字段为空」的残段（实测 control_files 107 字符
--    被 a80 断成两行，残段 |02.ctl 被当成一行新参数）。
--    判据：SUBSTR(...,1,N) 对应的 COLUMN <列名> FORMAT aM 必须 **M >= N**；
--    守门 = tools/check_oracle_psv_widths.py（失配即报错）。
-- ★★ **一律不写 WORD_WRAPPED**：它是**按词**折行，折点落在词间 ⇒ 残段与上一行
--    无法用「无分隔符拼接」还原（会粘字）。去掉后 sqlplus 只剩**断词折行**一种模式，
--    解析层 package_adapter._rejoin_wrapped_rows 才能安全兜底。
--    ⚠ 本段在**未加引号的 heredoc 内**：禁止反引号，$ 一律写 \$。
--    注意：v\$parameter.value 已用 SUBSTR() 截断；v\$pgastat/gv\$sysstat.value(NUMBER) SQL内用 COLUMN 覆盖
COLUMN name         FORMAT a50
COLUMN NAME         FORMAT a50
COLUMN value        FORMAT a500
COLUMN VALUE        FORMAT a500
COLUMN description  FORMAT a200
COLUMN DESCRIPTION  FORMAT a200
COLUMN message      FORMAT a500
COLUMN MESSAGE      FORMAT a500
COLUMN host_name    FORMAT a30
COLUMN HOST_NAME    FORMAT a30
COLUMN parameter    FORMAT a42
COLUMN PARAMETER    FORMAT a42
COLUMN destination  FORMAT a70
COLUMN member       FORMAT a100
COLUMN file_name    FORMAT a100
COLUMN path         FORMAT a80
COLUMN message_text FORMAT a500
COLUMN additional_info FORMAT a120
COLUMN obj_name     FORMAT a50
COLUMN username         FORMAT a30
COLUMN USERNAME         FORMAT a30
COLUMN owner            FORMAT a30
COLUMN OWNER            FORMAT a30
COLUMN machine          FORMAT a30
COLUMN MACHINE          FORMAT a30
COLUMN dest_name        FORMAT a30
COLUMN DEST_NAME        FORMAT a30
COLUMN profile          FORMAT a30
COLUMN PROFILE          FORMAT a30
COLUMN grantee          FORMAT a30
COLUMN GRANTEE          FORMAT a30
COLUMN table_name       FORMAT a40
COLUMN TABLE_NAME       FORMAT a40
COLUMN index_name       FORMAT a40
COLUMN INDEX_NAME       FORMAT a40
COLUMN object_name      FORMAT a50
COLUMN OBJECT_NAME      FORMAT a50
COLUMN baseline_name    FORMAT a40
COLUMN BASELINE_NAME    FORMAT a40
COLUMN task_name        FORMAT a50
COLUMN TASK_NAME        FORMAT a50
COLUMN finding_name     FORMAT a80
COLUMN FINDING_NAME     FORMAT a80
COLUMN impact_type      FORMAT a30
COLUMN IMPACT_TYPE      FORMAT a30
COLUMN impact           FORMAT 999999999999990.00
COLUMN IMPACT           FORMAT 999999999999990.00
ALTER SESSION SET nls_date_format='yyyy-mm-dd hh24:mi:ss';
ALTER SESSION SET nls_language='AMERICAN';
$sql
EXIT;
EOF
    rc=$?
    end_iso=$(iso_now); end_ms=$(epoch_ms)
    RUNTIME[SQL_QUERY_COUNT]=$((RUNTIME[SQL_QUERY_COUNT] + 1))

    # ── 状态判定 ──
    # ★ 关键：sqlplus 在 `WHENEVER SQLERROR CONTINUE` 下把 ORA-/SP2- 错误打到 **stdout**，
    #   stderr 常为空 → 只看 stderr 会把"SQL 写错/无权限"全判成 ok。这里把 stdout 里的
    #   错误行并入错误文件，交给 classify_failure 统一分类（OF-03 / 实测-1 的根因）。
    # ★★ N-1（批 5）：判据必须**锚行首**。sqlplus 的报错永远自成一行（`ORA-00904: …`），
    #   而**数据本身**可以长得像报错 —— `oracle.topsql.recent_errors` 查的就是告警日志，
    #   行首是时间戳、行尾是 `… 256125 ORA-27125`；旧判据扫整行 ⇒ 正常项被判 error、
    #   SQL 失败数虚高、logs/modules/sql.log 被无关文本污染（实测包：该 1 项虚高）。
    #   口径与 ora_value 取值的判据一致（都锚行首）。
    if grep -qE '^[[:space:]]*(ORA-[0-9]{5}|SP2-[0-9]{4})' "$tmp" 2>/dev/null; then
        grep -E '^[[:space:]]*(ORA-[0-9]{5}|SP2-[0-9]{4})' "$tmp" >> "$err_file" 2>/dev/null
    fi

    if [ "$rc" -ne 0 ]; then
        status=$(classify_failure "$rc" "$err_file")
    elif [ -s "$err_file" ]; then
        status=$(classify_failure 0 "$err_file")
        [ "$status" = "ok" ] && status="error"
    else
        status="ok"
    fi

    # ── 行数：**只有真取到结果才数** ──
    #   ★ N-2（批 5）：失败时 $tmp 里是 sqlplus 的报错回显（`SELECT …` / `*` /
    #   `ERROR at line 2:` / `ORA-00904: …`），照数会得到"3 行数据"这种假行数。
    rows=0
    if [ "$status" = "ok" ]; then
        rows=$(psv_normalize "$tmp" | wc -l | tr -d ' '); rows=${rows:-0}
        [ "$rows" -gt 0 ] && rows=$((rows - 1))   # 第 1 行是列名行，不算数据行
    fi
    # ★ 实测-1：真正跑通但零行 → empty（不是 ok）。「查了、确认没有」和「没采到」是相反结论。
    if [ "$status" = "ok" ] && [ "$rows" -eq 0 ]; then status="empty"; fi
    is_failure_status "$status" && RUNTIME[SQL_FAIL_COUNT]=$((RUNTIME[SQL_FAIL_COUNT] + 1))

    reason=$(tail -n 3 "$err_file" 2>/dev/null | tr '\n' ' ')

    # ── psv 落盘：只允许**三种形态**（interface §6.3）──
    #   ① 有数据      → 首行即列名行，`|` 分隔（status=ok）
    #   ② 查了没有    → `# oracle-inspection psv-empty …`（status=empty）
    #   ③ 根本没取到  → `# oracle-inspection psv-unavailable …`（其余 status）
    #   ★★ N-2（批 5）：②与③语义**相反**，绝不能让它们长得一样、更不能让失败项落成
    #      sqlplus 回显（首行不是列名 → 下游按 `|` 解析会静默出错）。
    if [ "$structured" -eq 1 ]; then
        mkdir -p "$TABLES_DIR"
        if [ "$status" = "ok" ]; then
            psv_normalize "$tmp" > "${TABLES_DIR}/${item_id}.psv"
        elif [ "$status" = "empty" ]; then
            printf '# oracle-inspection psv-empty item_id=%s sep=| rows=0 status=empty\n' "$item_id" \
                > "${TABLES_DIR}/${item_id}.psv"
        else
            printf '# oracle-inspection psv-unavailable item_id=%s sep=| rows=0 status=%s\n' "$item_id" "$status" \
                > "${TABLES_DIR}/${item_id}.psv"
        fi
    fi

    # ── Markdown 段（人读）：由同一份 tmp 渲染，保证与 psv 同源 ──
    md_block_start
    if [ "$rows" -eq 0 ]; then
        printf '%s\n' "(无数据行；status=${status})" >> "${RUNTIME[REPORT_FILE]}"
    elif has_cmd column; then
        column -t -s'|' < "$tmp" >> "${RUNTIME[REPORT_FILE]}" 2>/dev/null || cat "$tmp" >> "${RUNTIME[REPORT_FILE]}"
    else
        cat "$tmp" >> "${RUNTIME[REPORT_FILE]}"
    fi
    md_block_end

    # 错误原文落两处后清理临时文件：① 运行日志（filepath 下，**不进包**）
    # ② 包内模块日志 logs/modules/sql.log（OF-26）—— status 的 reason 只留结果码/摘要。
    cat "$err_file" >> "${RUNTIME[LOG_FILE]}" 2>/dev/null
    [ -s "$err_file" ] && module_log_append "sql" "$item_id" "$rc" "$err_file"
    rm -f "$err_file" "$tmp"

    record_status "$item_id" "sql" "$status" "$start_iso" "$end_iso" "$((end_ms-start_ms))" "$rows" "$rc" "$psv_file" "$reason"
}

# ==================== 环境检测模块化 ====================

# ==================== 环境探测（有才采，无则跳过） ====================
# 探测 DB 名 / 实例名 / 版本 / 角色 / 打开模式，写入 ENV
detect_db_basic() {
    progress "检测 Oracle 数据库基本信息..."
    ENV[DB_NAME]=$(ora_value "name FROM v\$database")
    ENV[INSTANCE_NAME]=$(ora_value "instance_name FROM v\$instance")
    ENV[DB_ROLE]=$(ora_value "database_role FROM v\$database")
    ENV[DB_OPEN_MODE]=$(ora_value "open_mode FROM v\$database")
    ENV[ORA_VERSION]=$(ora_value "NVL(version_full, version) FROM v\$instance")
}

# 探测是否 RAC 及节点数（写入 ENV[IS_RAC]/ENV[RAC_NODE_COUNT]）
detect_rac() {
    progress "检测 RAC 环境..."
    local cluster_db
    cluster_db=$(ora_value "value FROM v\$parameter WHERE name='cluster_database'")
    if [ "$cluster_db" = "TRUE" ]; then
        ENV[IS_RAC]=1
        ENV[RAC_NODE_COUNT]=$(ora_value "COUNT(*) FROM gv\$instance")
        progress "检测到 RAC 环境，节点数: ${ENV[RAC_NODE_COUNT]}"
    else
        progress "单机环境"
    fi
}

# 探测是否存在 ASM 实例 / 磁盘组（写入 ENV[HAS_ASM]）
# ★ OF-20：必须用 VALUE_OK 区分「查到 0 个盘组」与「根本没权限查」——
#   无 SYSASM/SYSDBA 权限时 v$asm_diskgroup 报 ORA-00942，旧版会走 else 分支说
#   "未检测到 ASM"，客户于是以为盘组不用看；真相是"没权限查"。
detect_asm() {
    progress "检测 ASM 环境..."
    local asm_cnt
    asm_cnt=$(ora_value "COUNT(*) FROM v\$asm_diskgroup")
    if ! ora_value_ok || ! is_uint "${asm_cnt:-}"; then
        progress "ASM 探测未取得结果（无查询权限或视图不可用），按未检测处理"
    elif [ "$asm_cnt" -gt 0 ]; then
        ENV[HAS_ASM]=1
        progress "检测到 ASM 环境，磁盘组数: ${asm_cnt}"
    else
        progress "未检测到 ASM 磁盘组"
    fi
}

# 探测是否 ADG 备库（写入 ENV[IS_STANDBY]）
detect_adg() {
    progress "检测 ADG 环境..."
    if printf '%s' "${ENV[DB_ROLE]}" | grep -qi "STANDBY"; then
        ENV[IS_STANDBY]=1
        progress "检测到 ADG 物理备库 (DB_ROLE=${ENV[DB_ROLE]})"
    else
        progress "主库角色 (DB_ROLE=${ENV[DB_ROLE]:-PRIMARY})"
    fi
}

# 探测是否 CDB 容器库（写入 ENV[IS_CDB]）
detect_cdb() {
    progress "检测 CDB 环境..."
    local cdb_flag
    cdb_flag=$(ora_value "cdb FROM v\$database")
    if [ "$cdb_flag" = "YES" ]; then
        ENV[IS_CDB]=1
        progress "检测到 CDB 容器数据库（12c+），支持PDB采集"
    fi
}

# 探测 PDB 数量与列表（写入 ENV[PDB_COUNT]/ENV[PDB_LIST]）
detect_pdb() {
    if [ "${ENV[IS_CDB]}" -ne 1 ]; then
        return
    fi
    progress "检测 PDB 信息..."
    ENV[PDB_COUNT]=$(ora_value "COUNT(*) FROM v\$pdbs WHERE name != 'PDB\$SEED'")
    # 使用 listagg 可能在某些版本失败，加 fallback
    local pdb_list_raw
    pdb_list_raw=$(ora_try "SELECT listagg(name, ',') WITHIN GROUP(ORDER BY con_id) FROM v\$pdbs WHERE name != 'PDB\$SEED'" 2>/dev/null)
    # 过滤掉错误信息
    ENV[PDB_LIST]=$(printf '%s' "$pdb_list_raw" | grep -vE 'ERROR|ORA-|no rows' | tr -d ' \r\n')
    if [ -z "${ENV[PDB_LIST]}" ]; then
        ENV[PDB_LIST]=$(ora_try "SELECT name FROM v\$pdbs WHERE name != 'PDB\$SEED' ORDER BY con_id" 2>/dev/null | grep -vE 'ERROR|ORA-|no rows' | tr '\n' ',' | sed 's/,$//' | tr -d ' ')
    fi
    progress "检测到 ${ENV[PDB_COUNT]} 个 PDB: ${ENV[PDB_LIST]:-无}"
}

# 编排以上 detect_* 序列，汇总环境标志；决定哪些模块实际执行
detect_env() {
    progress "开始环境自动检测..."
    detect_db_basic
    detect_rac
    detect_asm
    detect_adg
    detect_cdb
    detect_pdb
    progress "环境检测完成: DB=${ENV[DB_NAME]} INSTANCE=${ENV[INSTANCE_NAME]} VERSION=${ENV[ORA_VERSION]}"
    progress "RAC=${ENV[IS_RAC]} ASM=${ENV[HAS_ASM]} STANDBY=${ENV[IS_STANDBY]} CDB=${ENV[IS_CDB]} PDB=${ENV[PDB_COUNT]}"
}

# ==================== 模块化执行封装 ====================
# ==================== 模块执行框架（并行 + 容错 + 状态登记） ====================
# ★ 模块执行封装：计时 → 调用模块函数 → 登记状态。
#   签名：run_module <module_id> <module_name> <func_name>
#     · module_id = 显式 ASCII（oracle.module.<域>）→ 写进 status/<module_id>.tsv（OF-01）。
#     · module_name = **中文名**，仅用于人读输出与 --include/--exclude 的开关匹配。
#   历史教训（实测-3）：v1.4 用 `module.${module_name}`（中文）当 item_id → 16 个模块名塌到
#     `module.`、27 个只剩 11 个文件；module 层内容可恢复，sql 层不可恢复。
#   容错（有意为之）：模块失败不中断整包（配合 set +e 与 WHENEVER SQLERROR CONTINUE）。
#
# ★ EXIT_POLICY_SKIP：模块**策略跳过**的返回码（不是失败，也不是脚本终态退出码）。
#   语义 = 「模块跑了，但按许可/前置条件没有采集」。目前唯一使用者是 collect_awr_report
#   （未确认 Diagnostics Pack）。run_module 在 `rc≠0 → error` 分支**之前**拦下它，
#   记 not_enabled + MODULE_REASON，**不**累加 MODULES_FAILED、**不**写「采集异常」。
#   为什么非要一个专用码：旧版 AWR 跳过走 `return 0` ⇒ 被记成 ok（实测 13ms / reason 空）
#   ⇒ 包内台账与控制台都看不出它被跳过（AGENTS.md 禁止把「未采集」当「正常」）；
#   而若直接返回 1，就又变成「错记失败」。两个极端都不对，必须走第三条路。
EXIT_POLICY_SKIP=40

run_module() {
    local module_id="$1"
    local module_name="$2"
    local func_name="$3"

    # 检查模块开关（开关用中文名，保持现场 --include/--exclude 的既有写法不变）
    if ! is_module_enabled "$module_name"; then
        progress "跳过模块 [${module_name}] (被排除或未在包含列表中)"
        # ★ N-18：record_status 是 **10 个位置参数**（…duration row_count exit_code output_file reason）。
        #   这里原先只给了 9 个 —— 少传 exit_code 后，说明文字被挤进 output_file 列、reason 变空，
        #   且**编译期无感、运行不报错**，只有真包才看得出来。现已加静态守门（实参个数必须 = 10）。
        record_status "$module_id" "module" "skipped" "$(iso_now)" "$(iso_now)" 0 0 0 "" "excluded by user"
        return 0
    fi

    # Dry-run模式
    if [[ "${CONFIG[DRY_RUN]}" -eq 1 ]]; then
        progress "[DRY-RUN] 将执行模块: ${module_name}"
        return 0
    fi

    local start_iso start_ms end_iso end_ms rc=0
    start_iso=$(iso_now); start_ms=$(epoch_ms)
    progress "执行模块: ${module_name}"

    # ★ N-16（批 7）：不再用 `if ! ${func_name}` —— 那会把任何非零返回码**一律压成 1**，
    #   而 `collect_structured_package` 需要把真实 rc（30=安全扫描拦截 / 31=tar 失败）带出去。
    #   改为"先执行、再取 $?"，rc 原样保留（脚本是 `set +e`，不会因非零退出）。
    #   `MODULE_REASON` 是本函数允许被调函数回传的**两个出口之一**（另一个是专用策略跳过码
    #   `EXIT_POLICY_SKIP`，见下方分支）—— 用于给出比 "module returned non-zero" 更精确的
    #   结论；每次进入前先清空，避免上一次的残留串味。
    MODULE_REASON=""
    ${func_name} 2>>"${RUNTIME[LOG_FILE]}"; rc=$?

    # ★ EXIT_POLICY_SKIP：策略跳过 ≠ 失败，必须在下面的 `rc -ne 0 → error` 分支**之前**拦下。
    #   否则会被打成「采集异常」、累加 MODULES_FAILED、还往 Markdown 里写一行 "采集异常" ——
    #   那只是把「错记成功」换成「错记失败」。这里按 not_enabled 记账（进「未启用」分档、
    #   进报告附录的「数据缺口」表），精确 reason 由模块经 MODULE_REASON 给出。
    if [ "$rc" -eq "$EXIT_POLICY_SKIP" ]; then
        end_iso=$(iso_now); end_ms=$(epoch_ms)
        RUNTIME[MODULES_EXECUTED]=$((RUNTIME[MODULES_EXECUTED] + 1))
        record_status "$module_id" "module" "not_enabled" "$start_iso" "$end_iso" \
            "$((end_ms-start_ms))" 0 "$rc" "" "${MODULE_REASON:-policy skip}"
        progress "[SKIP] 模块 ${module_name} 按许可/前置条件未采集: ${MODULE_REASON:-policy skip}"
        return 0
    fi

    if [ "$rc" -ne 0 ]; then
        log_warn "模块 [${module_name}] 执行出现异常，已跳过，继续后续采集"
        md_line ""
        md_line "> ⚠️ 模块 **${module_name}** 采集异常，请查看日志: ${RUNTIME[LOG_FILE]}"
        md_line ""
        RUNTIME[MODULES_FAILED]=$((RUNTIME[MODULES_FAILED] + 1))
    fi

    end_iso=$(iso_now); end_ms=$(epoch_ms)
    RUNTIME[MODULES_EXECUTED]=$((RUNTIME[MODULES_EXECUTED] + 1))

    local status reason
    if [ "$rc" -eq 0 ]; then
        status="ok"; reason="${MODULE_REASON:-}"
        progress "[OK]   模块 ${module_name} 完成，耗时 $(((end_ms-start_ms)/1000))s"
    else
        status="error"; reason="${MODULE_REASON:-module returned non-zero}"
        progress "[FAIL] 模块 ${module_name} 异常，耗时 $(((end_ms-start_ms)/1000))s"
    fi
    record_status "$module_id" "module" "$status" "$start_iso" "$end_iso" "$((end_ms-start_ms))" 0 "$rc" "" "$reason"
}

# 后台并行模块支持
# 以后台子 shell 启动一个模块（并行采集）；签名与 run_module 一致
start_module_bg() {
    local module_id="$1"; local module_name="$2"; local func_name="$3"
    BG_IDS+=("$module_id"); BG_NAMES+=("$module_name"); BG_START_ISO+=("$(iso_now)"); BG_START_MS+=("$(epoch_ms)")
    progress "后台启动模块: $module_name"
    run_module "$module_id" "$module_name" "$func_name" &
    BG_PIDS+=("$!")
}

# 等待所有后台模块结束并汇总状态
#   ★ N-6：bg 模块的 run_module 跑在**子 shell**里（`run_module … &`），它自增的
#     `RUNTIME[MODULES_EXECUTED]` / `RUNTIME[MODULES_FAILED]` 随子 shell 退出而丢失 ——
#     父 shell 只看到"执行模块: 26"，而 MODULE_PLAN 有 28（2 个 bg 模块计数蒸发）。
#     修法：**不看子 shell 的内存变量**，只读它**落盘的 status 文件**来回写计数，
#     口径与同步路径严格一致（skipped 不计入执行数；error 同时计入失败数）。
wait_background_modules() {
    local i rc item_file st end_iso end_ms duration
    for ((i=0; i<${#BG_PIDS[@]}; i++)); do
        wait "${BG_PIDS[$i]}"; rc=$?
        item_file="${RUNTIME[STATUS_DIR]}/$(sanitize_filename "${BG_IDS[$i]}").tsv"
        st=$(awk -F'\t' 'NR==1{print $3}' "$item_file" 2>/dev/null)
        if [ -n "$st" ] && [ "$st" != "skipped" ]; then
            RUNTIME[MODULES_EXECUTED]=$((RUNTIME[MODULES_EXECUTED] + 1))
            [ "$st" = "error" ] && RUNTIME[MODULES_FAILED]=$((RUNTIME[MODULES_FAILED] + 1))
        fi
        if [ "$rc" -ne 0 ]; then
            log_warn "后台模块 ${BG_NAMES[$i]} 返回 ${rc}"
            if [ ! -s "$item_file" ]; then
                # 子 shell 连状态都没来得及写（被 kill / 崩在 record_status 之前）→ 兜底补记
                end_iso=$(iso_now); end_ms=$(epoch_ms); duration=$((end_ms-BG_START_MS[$i]))
                record_status "${BG_IDS[$i]}" "module" "error" "${BG_START_ISO[$i]}" "$end_iso" "$duration" 0 "$rc" "" "background module terminated before status recorded"
                RUNTIME[MODULES_EXECUTED]=$((RUNTIME[MODULES_EXECUTED] + 1))
                RUNTIME[MODULES_FAILED]=$((RUNTIME[MODULES_FAILED] + 1))
            fi
        fi
    done
}

# ==================== 模块1: 系统信息 ====================
# ==================== 采集器：OS ====================
# 展示层：把 OS 事实的**人读摘要**写进 Markdown（主机/内核/内存/CPU/指引）。
#   ★ OF-15 收敛原则：**md 负责展示、包负责数据**，同一事实只有一个采集源。
#     所以本函数**不再裸跑** df/lsblk/ip/vmstat/iostat/limits/ps/lsnrctl —— 它们已由
#     `collect_system_static` 结构化采集（tables//evidence/），这里只保留宿主名 + 内核 +
#     /proc/meminfo + free + nproc，其余段落改写为"详见 evidence/xxx"。
#   ⚠ 结构化只在本机采集时才有意义；归属判定见 `TARGET_IS_LOCAL`（OF-06）。
collect_sys_info() {
    md_line "## 系统信息"
    md_line ""
    md_line "### 主机与内核"
    md_block_start
    has_cmd hostname && hostname -s >> "${RUNTIME[REPORT_FILE]}" 2>&1 || true
    has_cmd uname   && uname -a >> "${RUNTIME[REPORT_FILE]}" 2>&1 || true
    md_block_end

    md_line ""
    md_line "### 网络与磁盘"
    md_block_start
    # ★ OF-15：md 只做展示/指引，不在 md 里重复采集。
    #   原先 `df -HT`（1000 进制、不带 -P）与结构化 `df -PT`（1024 进制）口径打架，客户对不上。
    printf '网络：evidence/ip_address.txt（ip -details addr）+ evidence/ip_route.txt（ip route table all）\n' >> "${RUNTIME[REPORT_FILE]}"
    printf '磁盘：evidence/filesystems.txt（df -PT）、evidence/inodes.txt（df -Pi）、evidence/block_devices.txt（lsblk -b 全字段）\n' >> "${RUNTIME[REPORT_FILE]}"
    md_block_end

    md_line ""
    md_line "### 内存与CPU"
    md_block_start
    [ -f /proc/meminfo ] && cat /proc/meminfo >> "${RUNTIME[REPORT_FILE]}" 2>&1 || true
    has_cmd free    && free -m >> "${RUNTIME[REPORT_FILE]}" 2>&1 || true
    if has_cmd nproc; then
        printf "CPU cores: %s\n" "$(nproc)" >> "${RUNTIME[REPORT_FILE]}"
    elif [ -f /proc/cpuinfo ]; then
        printf "CPU cores: %s\n" "$(grep -c processor /proc/cpuinfo)" >> "${RUNTIME[REPORT_FILE]}"
    fi
    md_block_end

    md_line ""
    md_line "### IO 与性能"
    md_block_start
    # ★ OF-15：vmstat/iostat 属"瞬时观察"，已归**实时时序采样**（timeseries/system_*.csv）；
    #   原先裸跑要白等 10 秒（iostat -x -k 3 2 = 6s + vmstat 2 3 = 4s），且与结构化数据重复。
    printf '实时时序见结构化包：timeseries/system_cpu.csv、system_memory.csv、system_disk.csv、system_network.csv\n' >> "${RUNTIME[REPORT_FILE]}"
    md_block_end

    md_line ""
    md_line "### 系统 limits 配置"
    md_block_start
    # ★ OF-15：结构化包已采（且口径完整），md 只做指引
    printf '配置层：tables/oracle_limits.tsv（limits.conf + limits.d/*.conf，4 列）\n' >> "${RUNTIME[REPORT_FILE]}"
    printf '实际层：evidence/oracle_actual_limits.txt（ulimit -a + /proc/<pmon_pid>/limits）\n' >> "${RUNTIME[REPORT_FILE]}"
    md_block_end

    md_line ""
    md_line "### Oracle 进程信息"
    md_block_start
    # ★ OF-15：结构化包已采（含 %cpu/%mem/etimes，可判 CPU 占用与 PMON 存活时长），md 只做指引
    printf '详见 evidence/oracle_processes.txt（pid/ppid/user/etimes/%%cpu/%%mem/args）\n' >> "${RUNTIME[REPORT_FILE]}"
    md_block_end

    md_line ""
    md_line "### Oracle 监听状态"
    md_block_start
    # ★ OF-15：改为结构化采集（且带 OF-08 超时，避免 hung 的 lsnrctl 拖死整包），md 只做指引
    printf '详见 evidence/listener_status.txt\n' >> "${RUNTIME[REPORT_FILE]}"
    md_block_end

    md_line ""
    md_line "### 环境变量"
    md_block_start
    # ★ OF-15：结构化包已采（PATH/LD_LIBRARY_PATH 含客户路径，只在 evidence 落原值），md 只做指引
    printf '详见 tables/oracle_env.tsv（ORACLE_HOME / ORACLE_BASE / ORACLE_SID / GRID_HOME / TNS_ADMIN）\n' >> "${RUNTIME[REPORT_FILE]}"
    md_block_end
}

# ★ OF-12：结构化 Oracle 进程快照。原先只有 `collect_sys_info` 里 md 的 `ps -ef`
#   （**无 %cpu/%mem/etimes**、不进包、无 status 项）。改用 `-eo … etimes,%cpu,%mem,args` 才能判
#   「哪个进程在吃 CPU」「PMON 已运行多久」；进包后可交叉验证：
#     ① 同机上是否残留了另一个 ORACLE_HOME 的 PMON（升级/双实例并存）；
#     ② ASM 实例（asm_pmon_+ASM）是否真的起来。
#   ⚠ redact_command_stream 是**脚本内函数**，不能塞进 `bash -c '…'`（子 shell 看不到），
#     故独立成本函数，由 capture_command 直接调用（重定向与退出码语义不变，因其在当前 shell 内执行）。
collect_oracle_processes() {
    ps -eo pid,ppid,user,etimes,%cpu,%mem,args 2>/dev/null \
      | awk 'BEGIN{IGNORECASE=1} /[o]ra_|[a]sm_|[t]nslsnr/ {print}' \
      | redact_command_stream
}

# ★ OF-09：ASM / 共享存储的 OS 侧视图。ASM 盘掉了、多路径失效、ASMLib 模块没加载，
#   在 DB 侧只看得到"盘组 OFFLINE"，看不到 OS 侧原因。此为 Oracle 巡检刚需（MySQL 不适用）。
#   脱敏：`multipath -ll` 输出 WWID，按项目规则**保留**（设备标识，非凭证）；`udev rules` 若含
#   密码字段才删（本函数不采 udev）。
#   ⚠ 用当前 shell 的管道 + run_with_timeout（函数），故不走 `bash -c`。
collect_asm_multipath() {
    printf '== multipath -ll ==\n'
    run_with_timeout 15 multipath -ll 2>&1
    printf '\n== lsmod (oracleasm / oracleaci) ==\n'
    lsmod 2>/dev/null | grep -E 'oracleasm|oracleaci' || printf '(no oracleasm/oracleaci module loaded)\n'
    if has_cmd oracleasm; then
        printf '\n== oracleasm listdisks ==\n'
        run_with_timeout 15 oracleasm listdisks 2>&1
    fi
    if [ -d /dev/oracleasm/disks ]; then
        printf '\n== /dev/oracleasm/disks ==\n'
        ls -l /dev/oracleasm/disks 2>&1
    fi
}

# ★ OF-15：lsnrctl status 的采集入口（带 OF-08 超时）。
#   动机：监听器 hung / 防火墙 DROP 时 lsnrctl 会长时间阻塞在 TCP 重传上；原先在 md 里裸跑，
#   而 run_module 是**同步**调用 → 一条 hung 的 lsnrctl 会把整包拖死。
collect_listener_status() {
    run_with_timeout 10 lsnrctl status 2>&1
}

# ★ OF-13：能力探测表 `tables/capabilities.tsv`（capability<TAB>value）。
#   动机：v1.4 只有 ENV[]（IS_RAC/HAS_ASM/…）这种"环境标志"，**没有 per-item 能力表** ——
#   `detect_asm` 失败、`dba_advisor` 权限不足、`v$diag_alert_ext` 不可用、`sadf` 在不在，
#   全部只能从 status 反推。MySQL 有 `tables/capabilities.tsv` + `mysql.capability_probe`。
#   约定（对齐 MySQL，注意 F-16 的教训：同一事实两处不同类型会踩坑）：
#     · 布尔统一写 **0/1**（tsv 侧）；snapshot.json 里的同名字段写 **true/false**；
#     · 字符串类（sqlplus_version / oracle_home）原样写；
#     · **Python 只读 snapshot.json.capabilities**，tsv 仅供人工核对。
#   ⚠ 本函数**必须早于 collect_system_static 之前的 SQL 探测**吗？不必 —— 它自成一模块，
#     顺序在主流程里紧随 collect_system_static 之后即可。
collect_capabilities() {
    [ "${CONFIG[STRUCTURED]}" -eq 1 ] || return 0
    [ -n "$TASK_DIR" ] || return 0
    local f="${TABLES_DIR}/capabilities.tsv"
    local spver=""
    if [ -n "${DB_CONN[SQLPLUS_BIN]}" ]; then
        # ★ N-9：`sqlplus -V` 的输出**随版本走不同流**（10g/部分发行版打到 stderr），
        #   旧写法 `2>/dev/null` 在实测的 19c 包里取到**空串** —— 能力表出现
        #   `sqlplus_version` 为空，人读时无法判断"是没装还是没探到"。
        #   改为 `2>&1` 合并 + 只提取 `Release <版本>` 号（sed 无匹配则输出空，不会把
        #   Usage 提示或整行 "SQL*Plus: Release 19.0.0.0.0 - Production" 塞进表里）。
        spver=$(run_with_timeout 10 "${DB_CONN[SQLPLUS_BIN]}" -V 2>&1 \
                | sed -n 's/.*Release \([0-9][0-9.]*\).*/\1/p' | head -1 | tr -d '\r')
    fi
    # 命令可用性（不连库）
    local c
    declare -A cmd_cap=(
        [sadf_command]=sadf [sar_command]=sar [chronyc_command]=chronyc [ntpq_command]=ntpq
        [lsnrctl_command]=lsnrctl [crsctl_command]=crsctl [dgmgrl_command]=dgmgrl
        [rman_command]=rman [opatch_command]=opatch [multipath_command]=multipath
        [oracleasm_command]=oracleasm [ipcs_command]=ipcs [sqlplus_command]=sqlplus
        [ip_command]=ip [ifconfig_command]=ifconfig
    )
    {
        printf 'capability\tvalue\n'
        printf 'sqlplus_bin\t%s\n' "${DB_CONN[SQLPLUS_BIN]:-}"
        printf 'sqlplus_version\t%s\n' "$spver"
        printf 'oracle_home\t%s\n' "${ORACLE_HOME:-}"
        printf 'diagnostics_pack\t%s\n' "$([ "${CONFIG[DIAGNOSTICS_PACK]:-0}" -eq 1 ] && echo 1 || echo 0)"
        printf 'is_local_target\t%s\n' "$([ "${TARGET_IS_LOCAL:-0}" -eq 1 ] && echo 1 || echo 0)"
        for c in "${!cmd_cap[@]}"; do
            printf '%s\t%s\n' "$c" "$(has_cmd "${cmd_cap[$c]}" && echo 1 || echo 0)"
        done
        # 视图可用性（连库；用 ora_value 的成败标志区分"没权限"与"真没有"）
        printf 'has_v_asm_diskgroup\t%s\n' "$(ora_value "COUNT(*) FROM v\$asm_diskgroup" >/dev/null; ora_value_ok && echo 1 || echo 0)"
        printf 'has_v_pdbs\t%s\n' "$(ora_value "COUNT(*) FROM v\$pdbs" >/dev/null; ora_value_ok && echo 1 || echo 0)"
        printf 'has_v_diag_alert_ext\t%s\n' "$(ora_value "COUNT(*) FROM v\$diag_alert_ext" >/dev/null; ora_value_ok && echo 1 || echo 0)"
        printf 'has_dba_advisor_findings\t%s\n' "$(ora_value "COUNT(*) FROM dba_advisor_findings" >/dev/null; ora_value_ok && echo 1 || echo 0)"
    } > "$f"
    record_status "system.capabilities" "system.static" "ok" "$(iso_now)" "$(iso_now)" 0 \
        "$(awk 'END{print NR-1}' "$f")" 0 "tables/capabilities.tsv" "command + view availability probes"
}

# ==================== 模块: 系统静态信息（结构化） ====================

# ★ 结构化 OS 静态项（与 MySQL 版同源、已对齐）+ v2.0 增补：
#   基础：os_release / time_status / timedatectl / chrony|ntpq / lscpu / free -b / df -PT /
#         df -Pi / lsblk -b / mount / ip / ss -s / numactl / sysctl_selected / hugepages / dmesg_errors
#   批 2 增补：ipcs_shm / asm_multipath / oratab / net_error_counters / hugepages 实测值（OF-09）、
#             oracle_limits + oracle_actual_limits（OF-11）、oracle_processes（OF-12）、
#             oracle_env + listener_status（OF-15 收敛）
#   ⚠ 本函数**不采**：capabilities（独立模块 collect_capabilities，OF-13）。
collect_system_static() {
    [ "${CONFIG[STRUCTURED]}" -eq 1 ] || return 0
    [ -n "$TASK_DIR" ] || return 0

    capture_command "system.os_release" "system.static" "$EVIDENCE_DIR/os_release.txt" bash -c 'cat /etc/os-release 2>/dev/null; uname -a; uptime 2>/dev/null' || true
    capture_command "system.time_status" "system.static" "$EVIDENCE_DIR/time_status.txt" bash -c 'printf "local_time="; date --iso-8601=seconds 2>/dev/null || date +%Y-%m-%dT%H:%M:%S%z; printf "utc_time="; date -u --iso-8601=seconds 2>/dev/null || date -u +%Y-%m-%dT%H:%M:%SZ; printf "timezone="; cat /etc/timezone 2>/dev/null || timedatectl show -p Timezone --value 2>/dev/null || date +%Z; printf "epoch_seconds="; date +%s' || true
    if has_cmd timedatectl; then
        capture_command "system.timedatectl" "system.static" "$EVIDENCE_DIR/timedatectl.txt" timedatectl status
    else
        record_status "system.timedatectl" "system.static" "unsupported" "$(iso_now)" "$(iso_now)" 0 0 127 "" "timedatectl not installed"
    fi
    if has_cmd chronyc; then
        # ★ OF-08：chronyd 无响应时 chronyc 会阻塞 → 套 15s 超时
        # ★ B5：判据不能只看 `has_cmd` —— chronyd **装了 ≠ 在跑**。只看命令存在时会把
        #   `ntpq_peers` 记成 skipped 并写"chronyc available"，而 chronyc 实际返回
        #   "506 Cannot talk to daemon" ⇒ 理由与事实矛盾；若本机真正用的是 ntpd，
        #   ntpd 的对端列表还会整块丢失。`capture_command` 返回命令自身的 rc ⇒ 用它判。
        #   ★ 条件里**不能**带 `|| true`（会让 if 恒真）。
        if capture_command "system.chronyc_tracking" "system.static" "$EVIDENCE_DIR/chronyc_tracking.txt" run_with_timeout 15 chronyc tracking; then
            # ★ N-14：`system.ntpq_peers` 与 chronyc 是**互斥备选**。显式记 skipped 后，
            #   "环境不适用"（互斥未采用）与"该采却没采到"（真缺项）才可区分（见 interface §4）。
            record_status "system.ntpq_peers" "system.static" "skipped" "$(iso_now)" "$(iso_now)" 0 0 0 "" "chronyd is answering; ntpq is the mutually-exclusive fallback"
        elif has_cmd ntpq; then
            # chronyd 不在应答，但本机有 ntpq ⇒ 采一次，别让时间同步证据整块丢失。
            capture_command "system.ntpq_peers" "system.static" "$EVIDENCE_DIR/ntpq_peers.txt" ntpq -pn || true
        else
            record_status "system.ntpq_peers" "system.static" "unsupported" "$(iso_now)" "$(iso_now)" 0 0 127 "" "chronyd is not answering and ntpq is not installed"
        fi
        capture_command "system.chronyc_sources" "system.static" "$EVIDENCE_DIR/chronyc_sources.txt" run_with_timeout 15 chronyc sources -v || true
    elif has_cmd ntpq; then
        capture_command "system.ntpq_peers" "system.static" "$EVIDENCE_DIR/ntpq_peers.txt" ntpq -pn || true
        # F-08：无 chronyc ⇒ 这两个 item 在任何路径都必须留痕（N-14 只兜了 ntpq_peers）
        record_status "system.chronyc_tracking" "system.static" "unsupported" "$(iso_now)" "$(iso_now)" 0 0 127 "" "chronyc not installed"
        record_status "system.chronyc_sources" "system.static" "unsupported" "$(iso_now)" "$(iso_now)" 0 0 127 "" "chronyc not installed"
    else
        record_status "system.chronyc_tracking" "system.static" "unsupported" "$(iso_now)" "$(iso_now)" 0 0 127 "" "chronyc not installed"
        record_status "system.chronyc_sources" "system.static" "unsupported" "$(iso_now)" "$(iso_now)" 0 0 127 "" "chronyc not installed"
        record_status "system.ntpq_peers" "system.static" "unsupported" "$(iso_now)" "$(iso_now)" 0 0 127 "" "neither chronyc nor ntpq installed"
    fi
    if has_cmd lscpu; then capture_command "system.lscpu" "system.static" "$EVIDENCE_DIR/lscpu.txt" lscpu || true; else record_status "system.lscpu" "system.static" "unsupported" "$(iso_now)" "$(iso_now)" 0 0 127 "" "lscpu not installed"; fi
    # ★ N-8：这四项（free / df / df -i / lsblk）的输出是**列对齐的空格填充文本**，不是表格：
    #   ① 用 TAB 切会静默错位（`free` 连表头都不是 TAB）；② `lsblk -o …MODEL,SERIAL` 的
    #   值**自身含空格**，根本无法无损 TSV 化。原先它们落在 `tables/*.tsv`，**扩展名说谎**
    #   —— 下游把 tables/ 当"结构化表格"按分隔符切，拿到的是错位的垃圾（实测：adapter 用 `|`
    #   切出「键=整行」的错位行）。改归 `evidence/*.txt`：tables/ 自此只放
    #   `*.psv`（SQL 结果，`|` 分隔）与 `*.tsv`（**真 TAB 表格**，如 capabilities/hugepages/
    #   oracle_limits/oracle_env/kernel_parameters），契约收窄且不再有歧义。
    if has_cmd free; then
        capture_command "system.free" "system.static" "$EVIDENCE_DIR/memory_snapshot.txt" free -b
    else
        record_status "system.free" "system.static" "unsupported" "$(iso_now)" "$(iso_now)" 0 0 127 "" "free not installed"
    fi
    if has_cmd df; then
        capture_command "system.filesystems" "system.static" "$EVIDENCE_DIR/filesystems.txt" df -PT -x fuse.gvfsd-fuse -x fuse.gvfs-fuse-daemon
    else
        record_status "system.filesystems" "system.static" "unsupported" "$(iso_now)" "$(iso_now)" 0 0 127 "" "df not installed"
    fi
    if has_cmd df; then
        capture_command "system.inodes" "system.static" "$EVIDENCE_DIR/inodes.txt" df -Pi -x fuse.gvfsd-fuse -x fuse.gvfs-fuse-daemon
    else
        record_status "system.inodes" "system.static" "unsupported" "$(iso_now)" "$(iso_now)" 0 0 127 "" "df not installed"
    fi
    if has_cmd lsblk; then
        capture_command "system.block_devices" "system.static" "$EVIDENCE_DIR/block_devices.txt" lsblk -b -o NAME,KNAME,TYPE,SIZE,FSTYPE,MOUNTPOINT,ROTA,SCHED,MODEL,SERIAL
    else
        record_status "system.block_devices" "system.static" "unsupported" "$(iso_now)" "$(iso_now)" 0 0 127 "" "lsblk not installed"
    fi
    if has_cmd mount; then
        capture_command "system.mounts" "system.static" "$EVIDENCE_DIR/mounts.txt" mount
    else
        record_status "system.mounts" "system.static" "unsupported" "$(iso_now)" "$(iso_now)" 0 0 127 "" "mount not installed"
    fi
    if has_cmd ip; then
        capture_command "system.ip_address" "system.static" "$EVIDENCE_DIR/ip_address.txt" ip -details addr show || true
        capture_command "system.ip_route" "system.static" "$EVIDENCE_DIR/ip_route.txt" ip route show table all || true
        # ★ N-14：与 ntpq 同理 —— `system.ifconfig` 是 `ip` 的互斥备选，未采用时显式记 skipped。
        record_status "system.ifconfig" "system.static" "skipped" "$(iso_now)" "$(iso_now)" 0 0 0 "" "ip available; ifconfig is the mutually-exclusive fallback"
    elif has_cmd ifconfig; then
        capture_command "system.ifconfig" "system.static" "$EVIDENCE_DIR/ip_address.txt" ifconfig -a || true
        # F-08：ifconfig 给不出路由表 ⇒ ip_route 在这条路径上确实采不到，如实记 unsupported
        record_status "system.ip_route" "system.static" "unsupported" "$(iso_now)" "$(iso_now)" 0 0 127 "" "ip not installed; ifconfig cannot dump the route table"
    else
        record_status "system.ip_address" "system.static" "unsupported" "$(iso_now)" "$(iso_now)" 0 0 127 "" "ip/ifconfig not installed"
        record_status "system.ip_route" "system.static" "unsupported" "$(iso_now)" "$(iso_now)" 0 0 127 "" "ip not installed; ifconfig cannot dump the route table"
        record_status "system.ifconfig" "system.static" "unsupported" "$(iso_now)" "$(iso_now)" 0 0 127 "" "ip/ifconfig not installed"
    fi
    if has_cmd ss; then
        capture_command "system.socket_summary" "system.static" "$EVIDENCE_DIR/socket_summary.txt" ss -s
    else
        record_status "system.socket_summary" "system.static" "unsupported" "$(iso_now)" "$(iso_now)" 0 0 127 "" "ss not installed"
    fi
    if has_cmd numactl; then
        capture_command "system.numa" "system.static" "$EVIDENCE_DIR/numa.txt" run_with_timeout 10 numactl --hardware
    else
        record_status "system.numa" "system.static" "unsupported" "$(iso_now)" "$(iso_now)" 0 0 127 "" "numactl not installed"
    fi
    # ★ N-20（批 7）：① 补**表头** `path\tvalue` —— 其余真 TSV（capabilities/hugepages/oracle_limits/
    #   oracle_env）都有表头，只有它没有，下游「首行即列名行」的约定在它身上失效；
    #   ② 缺 `sysctl` 时**显式记 unsupported** —— 原先 `has_cmd sysctl && …` 在无 sysctl 的机器上
    #   什么都不记，下游分不清"环境不适用"与"该采却没采到"（与 N-14 同类，属本次顺带发现的同类项）。
    if has_cmd sysctl; then
        capture_command "system.sysctl_selected" "system.static" "$TABLES_DIR/kernel_parameters.tsv" bash -c '
          printf "path\tvalue\n"
          for k in vm.swappiness vm.dirty_ratio vm.dirty_background_ratio vm.dirty_bytes vm.dirty_background_bytes vm.overcommit_memory vm.zone_reclaim_mode fs.file-max fs.aio-max-nr kernel.shmmax kernel.shmall net.core.somaxconn net.ipv4.tcp_max_syn_backlog; do
            v=$(sysctl -n "$k" 2>/dev/null) && printf "%s\t%s\n" "$k" "$v"
          done
          exit 0' || true
    else
        record_status "system.sysctl_selected" "system.static" "unsupported" "$(iso_now)" "$(iso_now)" 0 0 127 "" "sysctl not installed"
    fi
    {
        printf 'path\tvalue\n'
        for f in /sys/kernel/mm/transparent_hugepage/enabled /sys/kernel/mm/transparent_hugepage/defrag /proc/sys/vm/nr_hugepages; do
            [ -r "$f" ] && printf '%s\t%s\n' "$f" "$(tr '\n' ' ' < "$f")"
        done
        # ★ OF-09：/proc/meminfo 的大页**实测值**。只有 nr_hugepages（配置值）+ THP 开关算不出
        #   "配了但没被实例用掉、白占内存"这类最典型的大页坑 —— 必须 HugePages_Free 与 Total 对照。
        if [ -r /proc/meminfo ]; then
            awk -F: '/^(HugePages_Total|HugePages_Free|HugePages_Rsvd|Hugepagesize|AnonHugePages|Hugetlb):/{v=$2; gsub(/^[ \t]+|[ \t]+$/,"",v); printf "/proc/meminfo:%s\t%s\n",$1,v}' /proc/meminfo
        fi
    } > "$TABLES_DIR/hugepages.tsv"
    # F-27（三库同形）：行数为 0 必须写 empty。旧写法**无条件写 ok** —— 一旦 /sys 与 /proc/meminfo
    #   都读不出大页信息，报告看到的是"大页配置正常"，实际一个字都没采到。
    local hp_rows=0 hp_status="ok" hp_reason=""
    hp_rows=$(awk 'END{print (NR>0?NR-1:0)}' "$TABLES_DIR/hugepages.tsv" 2>/dev/null); hp_rows=${hp_rows:-0}
    if [ "$hp_rows" -eq 0 ]; then
        hp_status="empty"; hp_reason="no readable hugepage file under /sys/kernel/mm/transparent_hugepage, /proc/sys/vm or /proc/meminfo"
    fi
    record_status "system.hugepages" "system.static" "$hp_status" "$(iso_now)" "$(iso_now)" 0 "$hp_rows" 0 "tables/hugepages.tsv" "$hp_reason"
    if has_cmd dmesg; then
        # ★ OF-08：ring buffer 很大时 dmesg 会慢 → 套 20s 超时
        capture_command "system.dmesg_errors" "system.static" "$EVIDENCE_DIR/dmesg_errors.txt" run_with_timeout 20 dmesg --level=err,crit,alert,emerg 2>/dev/null
    else
        record_status "system.dmesg_errors" "system.static" "unsupported" "$(iso_now)" "$(iso_now)" 0 0 127 "" "dmesg not installed"
    fi

    # ★ OF-11：资源限制三层口径。原先只 `cat /etc/security/limits.conf`（且只在 md 里，Python 拿不到）。
    #   ① 配置层：limits.conf + limits.d/*.conf（RHEL/OEL 上 Oracle 的 limits 绝大多数在 limits.d，
    #      只读 limits.conf 会得出"没配 nofile"的假结论 —— 与 MySQL F-02 的 `!includedir` 同类陷阱）。
    #      输出 4 列 source_file/scope/limit/value，按「limits.conf 先、limits.d 字典序」合并，
    #      下游取最后一条即 PAM 的最终生效值。
    capture_command "system.oracle_limits" "system.static" "$TABLES_DIR/oracle_limits.tsv" bash -c '
printf "source_file\tscope\tlimit\tvalue\n"
files="/etc/security/limits.conf"
for f in /etc/security/limits.d/*.conf; do [ -r "$f" ] && files="$files $f"; done
for f in $files; do
    [ -r "$f" ] || continue
    awk -v src="$f" "/^[[:space:]]*#/{next} /^[[:space:]]*$/{next} NF>=4{print src \"\t\" \$1 \"\t\" \$2 \" \" \$3 \"\t\" \$4}" "$f"
done
exit 0' || true

    #   ② 实际生效层：采集器会话的 ulimit + **oracle 进程**的真实 limits。
    #      文件写的是"期望值"，进程拿到的是"实际值"——若 PAM 没把 limits 应用到会话，
    #      Oracle 实际仍是 1024，而文件里写着 65536。Oracle 启动告警(`max processes … is less than`)
    #      的根因就在这里，采集器应主动留证。
    capture_command "system.oracle_actual_limits" "system.static" "$EVIDENCE_DIR/oracle_actual_limits.txt" bash -c '
printf "== session ulimit -a (pid %s) ==\n" "$$"
ulimit -a 2>/dev/null
pmon_pid=$(pgrep -u oracle -f "ora_pmon_" 2>/dev/null | head -1)
if [ -n "$pmon_pid" ]; then
    printf "\n== oracle pmon actual limits (/proc/%s/limits) ==\n" "$pmon_pid"
    cat "/proc/$pmon_pid/limits" 2>/dev/null
else
    printf "\n== no ora_pmon_ process found (pgrep -u oracle -f ora_pmon_) ==\n"
fi
exit 0' || true

    # ★ OF-12：结构化 Oracle 进程快照（含 %cpu/%mem/etimes），落 evidence/oracle_processes.txt
    capture_command "system.oracle_processes" "system.static" "$EVIDENCE_DIR/oracle_processes.txt" collect_oracle_processes || true

    # ★ OF-09：Oracle 专属 OS 视图（MySQL 不适用，但 Oracle 巡检刚需；按「上线风险 × 现在完全没证据」排序）
    # ① SysV 共享内存 / 信号量：关掉 AMM（memory_target=0）时 SGA 走 SysV SHM，kernel.shmall/shmmax
    #    不足 = 实例起不来；与已采的 shmmax/shmall 形成「已用/上限」对照，才判得出余量。
    #    信号量同理（processes 调大后 kernel.sem 不够是经典故障）。
    if has_cmd ipcs; then
        capture_command "system.ipcs_shm" "system.static" "$EVIDENCE_DIR/ipcs_shm.txt" bash -c '
            printf "== ipcs -lm (shared memory limits) ==\n"; ipcs -lm 2>&1
            printf "\n== ipcs -ls (semaphore limits) ==\n";    ipcs -ls 2>&1
            printf "\n== ipcs -lu (shared memory status) ==\n"; ipcs -lu 2>&1
            exit 0' || true
    else
        record_status "system.ipcs_shm" "system.static" "unsupported" "$(iso_now)" "$(iso_now)" 0 0 127 "" "ipcs not installed"
    fi

    # ② ASM / 共享存储（多路径 + ASMLib 内核模块 + 设备节点）
    if has_cmd multipath; then
        capture_command "system.asm_multipath" "system.static" "$EVIDENCE_DIR/asm_multipath.txt" collect_asm_multipath || true
    else
        record_status "system.asm_multipath" "system.static" "unsupported" "$(iso_now)" "$(iso_now)" 0 0 127 "" "multipath not installed"
    fi

    # ③ /etc/oratab：OS 层登记的 SID/ORACLE_HOME，用于与 gv$instance 交叉验证「有实例没登记 / 登记了没起」
    if [ -r /etc/oratab ]; then
        capture_command "system.oratab" "system.static" "$EVIDENCE_DIR/oratab.txt" cat /etc/oratab || true
    else
        record_status "system.oratab" "system.static" "not_enabled" "$(iso_now)" "$(iso_now)" 0 0 127 "" "/etc/oratab not found"
    fi

    # ④ 网卡错误计数：RAC 私网丢包/错包是 gc buffer busy 的上游原因，ip -s link 一次就看得见
    if has_cmd ip; then
        capture_command "system.net_error_counters" "system.static" "$EVIDENCE_DIR/net_error_counters.txt" ip -s link || true
    else
        record_status "system.net_error_counters" "system.static" "unsupported" "$(iso_now)" "$(iso_now)" 0 0 127 "" "ip not installed"
    fi

    # ★ OF-15：把原先**只在 md** 的三项补成结构化采集（口径：md 展示、包数据）。补完之后 md 才敢删对应裸命令。
    # ① Oracle 环境变量（表：5 个关键变量）
    capture_command "system.oracle_env" "system.static" "$TABLES_DIR/oracle_env.tsv" bash -c '
printf "variable\tvalue\n"
for v in ORACLE_HOME ORACLE_BASE ORACLE_SID GRID_HOME TNS_ADMIN; do printf "%s\t%s\n" "$v" "${!v}"; done
exit 0' || true
    # ② 同上，落 evidence 完整值 —— PATH / LD_LIBRARY_PATH 可能含客户路径，按项目规则只在 evidence 存原值
    capture_command "system.oracle_env_full" "system.static" "$EVIDENCE_DIR/oracle_env_full.txt" bash -c '
printf "ORACLE_HOME=%s\nORACLE_BASE=%s\nORACLE_SID=%s\nGRID_HOME=%s\nTNS_ADMIN=%s\nLD_LIBRARY_PATH=%s\nPATH=%s\n" \
  "${ORACLE_HOME:-}" "${ORACLE_BASE:-}" "${ORACLE_SID:-}" "${GRID_HOME:-}" "${TNS_ADMIN:-}" "${LD_LIBRARY_PATH:-}" "${PATH:-}"
exit 0' || true
    # ③ 监听器状态（带 OF-08 超时；原先在 md 裸跑，hung 时会拖死整包）
    if has_cmd lsnrctl; then
        capture_command "system.listener_status" "system.static" "$EVIDENCE_DIR/listener_status.txt" collect_listener_status || true
    else
        record_status "system.listener_status" "system.static" "not_enabled" "$(iso_now)" "$(iso_now)" 0 0 127 "" "lsnrctl not in PATH"
    fi
}

# ==================== 模块2: Oracle 基础信息 ====================
# ==================== 采集器：数据库 ====================
# 采集实例/数据库基本信息（版本、启动时间、参数关键项等）
collect_oracle_info() {
    md_line "## Oracle 基础信息"
    md_line ""

    ora_sql_to_md "oracle.basic.database" "数据库基本信息" "
SELECT name, db_unique_name, database_role, open_mode, log_mode,
       created, resetlogs_time, protection_mode
FROM v\$database;"

    ora_sql_to_md "oracle.basic.instance" "实例信息" "
SELECT inst_id, instance_name, host_name, version, startup_time,
       status, database_status, instance_role, active_state
FROM gv\$instance
ORDER BY inst_id;"

    ora_sql_to_md "oracle.basic.charset" "数据库字符集" "
SELECT parameter, value
FROM nls_database_parameters
WHERE parameter IN ('NLS_CHARACTERSET','NLS_NCHAR_CHARACTERSET',
                    'NLS_LANGUAGE','NLS_TERRITORY','NLS_DATE_FORMAT')
ORDER BY parameter;"

    ora_sql_to_md "oracle.basic.parameters" "关键初始化参数" "
SELECT name,
       SUBSTR(value, 1, 400) value
FROM v\$parameter
WHERE name IN (
    'db_name','db_unique_name','db_block_size','sga_target','sga_max_size',
    'pga_aggregate_target','pga_aggregate_limit','memory_target','memory_max_target',
    'shared_pool_size','db_cache_size','log_buffer',
    'processes','sessions','open_cursors','cursor_sharing',
    'undo_tablespace','undo_retention','undo_management',
    'log_archive_dest_1','log_archive_dest_2','log_archive_format',
    'archive_log_target','db_recovery_file_dest','db_recovery_file_dest_size',
    'control_files','db_files','maxlogfiles','maxdatafiles',
    'parallel_max_servers','parallel_min_servers',
    'optimizer_mode','optimizer_index_cost_adj',
    'cluster_database','cluster_database_instances',
    'remote_login_passwordfile','audit_trail',
    'enable_pluggable_database'
)
ORDER BY name;"

    ora_sql_to_md "oracle.basic.sga_components" "SGA 组件大小" "
SELECT * FROM (
SELECT pool, name,
       ROUND(bytes/1024/1024, 2) size_mb
FROM v\$sgastat
WHERE pool IS NOT NULL
ORDER BY pool, bytes DESC
) WHERE ROWNUM <= 30;"

    ora_sql_to_md "oracle.basic.sga_summary" "SGA 汇总信息" "
SELECT name,
       ROUND(value/1024/1024, 2) AS mb
FROM v\$sga
ORDER BY value DESC;"

    ora_sql_to_md "oracle.basic.pga_stats" "PGA 统计" "
COLUMN value FORMAT 999999999999999999
SELECT name, value
FROM v\$pgastat
WHERE name IN (
    'total PGA inuse','total PGA allocated','maximum PGA allocated',
    'total freeable PGA memory','total PGA used for auto workareas',
    'over allocation count','bytes processed','extra bytes read/written'
);"

    ora_sql_to_md "oracle.basic.controlfiles" "控制文件" "
SELECT status, name FROM v\$controlfile ORDER BY name;"

    ora_sql_to_md "oracle.basic.redo_groups" "Redo 日志组信息" "
SELECT l.group#, l.thread#, l.sequence#, l.bytes/1024/1024 size_mb,
       l.members, l.status, lf.member AS filename
FROM v\$log l
JOIN v\$logfile lf ON l.group# = lf.group#
ORDER BY l.thread#, l.group#;"

    ora_sql_to_md "oracle.basic.object_counts" "数据库对象统计" "
SELECT object_type, COUNT(*) obj_count
FROM dba_objects
WHERE status = 'VALID'
GROUP BY object_type
ORDER BY obj_count DESC;"

    ora_sql_to_md "oracle.basic.invalid_objects" "无效对象统计" "
SELECT owner, object_type, object_name, status, last_ddl_time
FROM dba_objects
WHERE status != 'VALID'
ORDER BY owner, object_type, object_name;"

    ora_sql_to_md "oracle.basic.users" "用户列表" "
SELECT username, account_status, lock_date, expiry_date,
       default_tablespace, temporary_tablespace, created, profile
FROM dba_users
ORDER BY username;"

    ora_sql_to_md "oracle.basic.password_expiry" "用户密码过期预警（OPEN 账户 30 天内到期）" "
SELECT username, account_status, expiry_date,
       TRUNC(expiry_date - SYSDATE) days_until_expire,
       profile, default_tablespace
FROM dba_users
WHERE account_status = 'OPEN'
  AND oracle_maintained = 'N'
  AND expiry_date IS NOT NULL
  AND expiry_date - SYSDATE BETWEEN 0 AND 30
ORDER BY expiry_date;"

    ora_sql_to_md "oracle.basic.awr_snapshot_status" "AWR 快照状态（确认 AWR 是否正常运行）" "
SELECT MIN(snap_id) min_snap_id, MAX(snap_id) max_snap_id,
       COUNT(*) snap_count,
       TO_CHAR(MIN(begin_interval_time),'yyyy-mm-dd hh24:mi') earliest,
       TO_CHAR(MAX(end_interval_time)  ,'yyyy-mm-dd hh24:mi') latest
FROM dba_hist_snapshot;"

    ora_sql_to_md "oracle.basic.disabled_constraints" "禁用/失效约束（非SYS用户）" "
SELECT owner, constraint_name, constraint_type,
       table_name, status, last_change
FROM dba_constraints
WHERE status != 'ENABLED'
  AND owner NOT IN ('SYS','SYSTEM','OUTLN','DBSNMP','XDB','ORACLE_OCM')
ORDER BY owner, table_name, constraint_name;"

    ora_sql_to_md "oracle.basic.partitioned_tables" "分区表统计（分区数 TOP20）" "
SELECT * FROM (
SELECT owner, table_name, partitioning_type, subpartitioning_type,
       partition_count, def_subpartition_count
FROM dba_part_tables
WHERE owner NOT IN ('SYS','SYSTEM','OUTLN','DBSNMP','XDB','ORACLE_OCM')
ORDER BY partition_count DESC
) WHERE ROWNUM <= 20;"

    ora_sql_to_md "oracle.basic.large_segments" "大表段 TOP20（按大小排序）" "
SELECT * FROM (
SELECT owner, segment_name AS table_name, tablespace_name,
       ROUND(bytes/1024/1024/1024, 3) size_gb,
       segment_type
FROM dba_segments
WHERE segment_type IN ('TABLE','TABLE PARTITION','TABLE SUBPARTITION')
  AND owner NOT IN ('SYS','SYSTEM','OUTLN','DBSNMP','XDB','ORACLE_OCM')
ORDER BY bytes DESC
) WHERE ROWNUM <= 20;"

    # CDB/PDB 检测 (12c+)
    if [ "${ENV[IS_CDB]}" -eq 1 ]; then
        ora_sql_to_md "oracle.basic.pdb_list" "PDB 状态列表（CDB环境）" "
SELECT con_id, name, open_mode, restricted, open_time
FROM v\$pdbs
ORDER BY con_id;"
    fi
}

# ==================== 模块3: 表空间管理 ====================
# 表空间使用率、自动扩展、临时/UNDO 段
collect_tablespace() {
    md_line "## 表空间管理"
    md_line ""

    ora_sql_to_md "oracle.tablespace.usage" "所有表空间容量使用情况" "
SELECT a.tablespace_name,
       ROUND(a.bytes_alloc / 1024 / 1024) Alloc_MB,
       ROUND(NVL(b.bytes_free, 0) / 1024 / 1024) Free_MB,
       ROUND((a.bytes_alloc - NVL(b.bytes_free, 0)) / 1024 / 1024) Used_MB,
       ROUND((NVL(b.bytes_free, 0) / a.bytes_alloc) * 100, 2) Pct_Free,
       100 - ROUND((NVL(b.bytes_free, 0) / a.bytes_alloc) * 100, 2) Pct_Used,
       ROUND(maxbytes / 1048576) Max_MB
FROM (SELECT f.tablespace_name,
             SUM(f.bytes) bytes_alloc,
             SUM(DECODE(f.autoextensible, 'YES', f.maxbytes, 'NO', f.bytes)) maxbytes
      FROM dba_data_files f
      GROUP BY f.tablespace_name) a,
     (SELECT f.tablespace_name, SUM(f.bytes) bytes_free
      FROM dba_free_space f
      GROUP BY f.tablespace_name) b
WHERE a.tablespace_name = b.tablespace_name(+)
UNION ALL
SELECT h.tablespace_name,
       ROUND(SUM(h.bytes_free + h.bytes_used) / 1048576) Alloc_MB,
       ROUND(SUM((h.bytes_free + h.bytes_used) - NVL(p.bytes_used, 0)) / 1048576) Free_MB,
       ROUND(SUM(NVL(p.bytes_used, 0)) / 1048576) Used_MB,
       ROUND((SUM((h.bytes_free + h.bytes_used) - NVL(p.bytes_used, 0)) /
              SUM(h.bytes_used + h.bytes_free)) * 100, 2) Pct_Free,
       100 - ROUND((SUM((h.bytes_free + h.bytes_used) - NVL(p.bytes_used, 0)) /
              SUM(h.bytes_used + h.bytes_free)) * 100, 2) Pct_Used,
       ROUND(SUM(f.maxbytes) / 1048576) Max_MB
FROM sys.v_\$TEMP_SPACE_HEADER h,
     dba_temp_files f,
     (SELECT file_id, tablespace_name, SUM(bytes_used) bytes_used
      FROM sys.v_\$Temp_extent_pool
      GROUP BY file_id, tablespace_name) p
WHERE f.file_id = h.file_id
  AND f.tablespace_name = h.tablespace_name
  AND p.file_id(+) = h.file_id
  AND p.tablespace_name(+) = h.tablespace_name
GROUP BY h.tablespace_name
ORDER BY 6 DESC;"

    ora_sql_to_md "oracle.tablespace.datafiles" "表空间数据文件列表" "
SELECT tablespace_name, file_name,
       ROUND(bytes/1024/1024,2) size_mb,
       autoextensible,
       ROUND(maxbytes/1024/1024,2) max_mb,
       status
FROM dba_data_files
ORDER BY tablespace_name, file_name;"

    ora_sql_to_md "oracle.tablespace.tempfiles" "临时表空间数据文件" "
SELECT tablespace_name, file_name,
       ROUND(bytes/1024/1024,2) size_mb,
       autoextensible,
       ROUND(maxbytes/1024/1024,2) max_mb,
       status
FROM dba_temp_files
ORDER BY tablespace_name, file_name;"

    ora_sql_to_md "oracle.tablespace.growth" "表空间增长趋势（AWR，近30天每日汇总）" "
SELECT TO_CHAR(TO_DATE(r.rtime, 'mm/dd/yyyy hh24:mi:ss'), 'yyyy-mm-dd') snap_date,
       ROUND(SUM(r.tablespace_usedsize * t.block_size)/1024/1024/1024, 2) used_gb,
       ROUND(SUM(r.tablespace_size * t.block_size)/1024/1024/1024, 2) total_gb,
       ROUND(SUM(r.tablespace_size * t.block_size)/1024/1024/1024, 2)
           - LAG(ROUND(SUM(r.tablespace_usedsize * t.block_size)/1024/1024/1024, 2), 1, 0)
             OVER(ORDER BY MAX(TO_DATE(r.rtime, 'mm/dd/yyyy hh24:mi:ss'))) diff_gb
FROM dba_hist_tbspc_space_usage r,
     v\$tablespace g,
     dba_tablespaces t
WHERE r.tablespace_id = g.TS#
  AND g.name = t.tablespace_name
  AND t.contents NOT IN ('TEMPORARY','UNDO')
  AND TO_DATE(r.rtime, 'mm/dd/yyyy hh24:mi:ss') >= TRUNC(SYSDATE - 30)
GROUP BY TO_CHAR(TO_DATE(r.rtime, 'mm/dd/yyyy hh24:mi:ss'), 'yyyy-mm-dd')
ORDER BY snap_date DESC;"

    ora_sql_to_md "oracle.tablespace.high_usage" "使用率超过 80% 的表空间（⚠️ 预警）" "
SELECT
    a.tablespace_name,
    ROUND(a.total_bytes / 1024 / 1024 / 1024, 2) AS total_gb,
    ROUND(a.used_bytes / 1024 / 1024 / 1024, 2) AS used_gb,
    ROUND((a.used_bytes / a.total_bytes) * 100, 2) AS pct_used
FROM (
    SELECT
        df.tablespace_name,
        SUM(CASE WHEN df.autoextensible = 'YES' THEN df.maxbytes ELSE df.bytes END) AS total_bytes,
        SUM(df.bytes - COALESCE(fs.bytes, 0)) AS used_bytes
    FROM dba_data_files df
    LEFT JOIN (SELECT tablespace_name, SUM(bytes) bytes FROM dba_free_space GROUP BY tablespace_name) fs
          ON df.tablespace_name = fs.tablespace_name
    GROUP BY df.tablespace_name
) a
WHERE ROUND((a.used_bytes / a.total_bytes) * 100, 2) > 80
ORDER BY pct_used DESC;"
}

# ==================== 模块4: ASM 磁盘组（有才采集）====================
# ASM 磁盘组容量与状态
collect_asm() {
    if [ "${ENV[HAS_ASM]}" -ne 1 ]; then
        md_line "## ASM 磁盘组"
        md_line ""
        md_line "> 未检测到 ASM 磁盘组，跳过本模块。"
        return 0
    fi

    md_line "## ASM 磁盘组"
    md_line ""

    ora_sql_to_md "oracle.asm.diskgroup_summary" "ASM 磁盘组汇总" "
SELECT group_number, name, state, type,
       CASE WHEN total_mb >= 1024
            THEN ROUND(total_mb/1024, 2) || 'G'
            ELSE total_mb || 'M' END AS total_size,
       CASE WHEN free_mb >= 1024
            THEN ROUND(free_mb/1024, 2) || 'G'
            ELSE free_mb || 'M' END AS free_size,
       CASE WHEN (total_mb-free_mb) >= 1024
            THEN ROUND((total_mb-free_mb)/1024, 2) || 'G'
            ELSE (total_mb-free_mb) || 'M' END AS used_size,
       CASE WHEN total_mb = 0 THEN '0%'
            ELSE ROUND(((total_mb-free_mb)/total_mb*100), 2) || '%' END AS used_pct
FROM v\$asm_diskgroup
ORDER BY name;"

    ora_sql_to_md "oracle.asm.disk_detail" "ASM 磁盘详情" "
SELECT dg.name diskgroup,
       d.disk_number, d.name disk_name,
       ROUND(d.total_mb/1024, 2) total_gb,
       ROUND(d.free_mb/1024, 2)  free_gb,
       d.state, d.mode_status, d.path,
       d.header_status
FROM v\$asm_disk d
LEFT JOIN v\$asm_diskgroup dg ON d.group_number = dg.group_number
ORDER BY dg.name, d.disk_number;"

    ora_sql_to_md "oracle.asm.dbfile_count" "ASM 磁盘组中的数据库文件数" "
SELECT group_number, type, COUNT(*) file_count,
       ROUND(SUM(bytes)/1024/1024/1024, 2) total_gb
FROM v\$asm_file
GROUP BY group_number, type
ORDER BY group_number, type;"
}

# ==================== 模块5: 归档日志 ====================
# 归档模式、归档目的地、FRA 使用率
collect_archive() {
    md_line "## 归档日志"
    md_line ""

    ora_sql_to_md "oracle.archive.mode" "归档模式检查" "
SELECT (SELECT log_mode FROM v\$database) log_mode,
       (SELECT archiver FROM v\$instance) archiver
FROM DUAL;"

    ora_sql_to_md "oracle.archive.destinations" "归档目标参数" "
SELECT dest_id, dest_name, status, target, archiver,
       schedule, destination, db_unique_name
FROM v\$archive_dest
WHERE status != 'INACTIVE'
ORDER BY dest_id;"

    ora_sql_to_md "oracle.archive.hourly_3d" "近3天每小时归档量" "
SELECT thread# AS rac_thread,
       TO_CHAR(first_time,'YYYY-MM-DD HH24') AS hour_period,
       COUNT(*) archive_count,
       ROUND(SUM(blocks * block_size)/1024/1024, 2) total_size_mb
FROM v\$archived_log
WHERE first_time >= SYSDATE - 3
  AND archived = 'YES'
GROUP BY thread#, TO_CHAR(first_time,'YYYY-MM-DD HH24')
ORDER BY hour_period, thread# ASC;"

    ora_sql_to_md "oracle.archive.daily_30d" "近30天每日归档量" "
SELECT * FROM (
SELECT TRUNC(completion_time) AS day,
       COUNT(*) log_count,
       ROUND(SUM(blocks*block_size)/1024/1024/1024, 3) day_gb
FROM v\$archived_log
WHERE completion_time >= TRUNC(SYSDATE) - 30
  AND archived = 'YES'
GROUP BY TRUNC(completion_time)
ORDER BY day DESC
) WHERE ROWNUM <= 31;"

    ora_sql_to_md "oracle.archive.log_switch_rate" "重做日志切换频率（近24小时）" "
SELECT TRUNC(first_time,'hh24') switch_time,
       COUNT(*) switch_count
FROM v\$log_history
WHERE first_time > SYSDATE - 1
GROUP BY TRUNC(first_time,'hh24')
ORDER BY switch_time DESC;"

    if [ "${ENV[HAS_ASM]}" -eq 1 ]; then
        ora_sql_to_md "oracle.archive.fra_asm" "ASM 快速恢复区使用情况" "
SELECT name, space_limit/1024/1024/1024 limit_gb,
       space_used/1024/1024/1024  used_gb,
       space_reclaimable/1024/1024/1024 reclaimable_gb,
       number_of_files
FROM v\$recovery_file_dest;"
    fi

    # FRA 使用情况（无ASM时也检查）
    ora_sql_to_md "oracle.archive.fra_params" "快速恢复区（FRA）参数" "
SELECT name, SUBSTR(value, 1, 500) value
FROM v\$parameter
WHERE name IN ('db_recovery_file_dest','db_recovery_file_dest_size',
               'log_archive_dest_1','log_archive_format')
ORDER BY name;"
}

# ==================== 模块6: 会话与锁管理 ====================
# 会话与锁：阻塞链、长时间运行会话、等待事件
collect_session_lock() {
    md_line "## 会话与锁管理"
    md_line ""

    ora_sql_to_md "oracle.session.active" "当前活跃会话" "
SELECT s.inst_id, s.sid, s.serial# s_num, p.spid,
       NVL(s.username, SUBSTR(p.program, LENGTH(p.program)-6)) username,
       s.machine, s.event,
       s.p1 || '/' || s.p2 || '/' || s.p3 p123,
       s.wait_time wt,
       s.seconds_in_wait sec_wait,
       NVL(s.sql_id, s.prev_sql_id) sql_id
FROM gv\$process p, gv\$session s
WHERE p.addr = s.paddr
  AND p.inst_id = s.inst_id
  AND s.status = 'ACTIVE'
  AND p.background IS NULL
ORDER BY s.inst_id, s.seconds_in_wait DESC;"

    ora_sql_to_md "oracle.session.wait_events" "当前会话等待事件（非空闲）" "
SELECT s.inst_id, s.sid || ',' || s.serial# sess,
       s.machine, s.sql_id,
       w.wait_class, w.event,
       w.p1, w.p2, w.p3,
       w.wait_time, w.seconds_in_wait, w.state
FROM gv\$session_wait w, gv\$session s
WHERE w.sid = s.sid
  AND w.inst_id = s.inst_id
  AND w.wait_class != 'Idle'
ORDER BY w.inst_id, w.seconds_in_wait DESC;"

    ora_sql_to_md "oracle.session.locks" "会话锁信息" "
SELECT s.inst_id, s.sql_id,
       lo.session_id AS sid, s.serial#,
       s.blocking_session,
       NVL(lo.oracle_username,'(oracle)') AS username,
       o.owner AS object_owner,
       lo.object_id, o.object_name,
       DECODE(lo.locked_mode,
              0,'None', 1,'Null (NULL)', 2,'Row-S (SS)',
              3,'Row-X (SX)', 4,'Share (S)',
              5,'S/Row-X (SSX)', 6,'Exclusive (X)',
              lo.locked_mode) locked_mode,
       lo.os_user_name
FROM gv\$locked_object lo, dba_objects o, gv\$session s
WHERE lo.object_id = o.object_id
  AND lo.session_id = s.sid
  AND lo.inst_id = s.inst_id
ORDER BY s.inst_id, lo.session_id, o.object_name;"

    ora_sql_to_md "oracle.session.lock_chain" "锁等待链（阻塞者 → 被阻塞者）" "
SELECT h.sid hold_sid,
       r.sid wait_sid,
       DECODE(h.type,'TX','Transaction','TM','DML','UL','PL/SQL User Lock',h.type) type,
       DECODE(h.lmode,0,'None',1,'Null',2,'Row-S (SS)',
                      3,'Row-X (SX)',4,'Share',5,'S/Row-X (SSX)',
                      6,'Exclusive',TO_CHAR(h.lmode)) hold,
       DECODE(r.request,0,'None',1,'Null',2,'Row-S (SS)',
                        3,'Row-X (SX)',4,'Share',5,'S/Row-X (SSX)',
                        6,'Exclusive',TO_CHAR(r.request)) request,
       r.id1, r.id2, r.ctime
FROM v\$lock h, v\$lock r
WHERE h.block = 1 AND r.request > 0
  AND h.sid != r.sid
  AND h.type != 'MR' AND r.type != 'MR'
  AND h.id1 = r.id1 AND h.id2 = r.id2 AND h.type = r.type
  AND h.lmode > 0 AND r.request > 0
ORDER BY h.sid, r.sid;"

    ora_sql_to_md "oracle.session.long_transactions" "长事务（运行超过1小时）" "
SELECT s.sid, s.serial#, s.username,
       t.start_time, t.status,
       t.used_ublk, t.used_urec,
       s.sql_id,
       ROUND((SYSDATE - TO_DATE(t.start_time,'MM/DD/YY HH24:MI:SS')) * 24 * 60, 1) duration_min
FROM v\$transaction t
JOIN v\$session s ON t.ses_addr = s.saddr
WHERE (SYSDATE - TO_DATE(t.start_time,'MM/DD/YY HH24:MI:SS')) * 24 > 1
ORDER BY duration_min DESC;"

    # 杀会话 SQL 生成（有阻塞时辅助输出）
    md_line ""
    md_line "### 生成杀阻塞会话语句（参考）"
    md_block_start
    ora_exec >> "${RUNTIME[REPORT_FILE]}" 2>>"${RUNTIME[LOG_FILE]}" <<'SQLEOF'
SET PAGESIZE 50000
SET FEEDBACK OFF
SET HEADING ON
SET ECHO OFF
SET LINESIZE 32767
SET COLSEP ' | '
SET NULL '-'
SELECT q'[ALTER SYSTEM KILL SESSION ']' || s.sid || ',' || s.serial#
       || q'[ IMMEDIATE; --]' || ' blocker, blocked by session ' || s.blocking_session AS kill_sql
FROM v$session s
WHERE s.blocking_session IS NOT NULL
  AND s.status = 'ACTIVE';
EXIT;
SQLEOF
    md_block_end
}

# ==================== 模块7: 性能诊断 ====================
# 性能：Top SQL（逻辑读/执行次数/耗时）、等待事件等
collect_performance() {
    md_line "## 性能诊断"
    md_line ""

    if [ "${CONFIG[DIAGNOSTICS_PACK]:-0}" -eq 1 ]; then
      ora_sql_to_md "oracle.performance.sql_top10_ash" "SQL 资源消耗 TOP10（近1小时 ASH）" "
SELECT * FROM (
  SELECT ash.sql_id, aud.name type,
         SUM(DECODE(ash.session_state,'ON CPU',1,0)) AS cpu_samples,
         SUM(DECODE(ash.session_state,'WAITING',DECODE(wait_class,'User I/O',0,1),0)) AS wait_samples,
         SUM(DECODE(ash.session_state,'WAITING',DECODE(wait_class,'User I/O',1,0),0)) AS io_samples,
         SUM(DECODE(ash.session_state,'ON CPU',1,1)) AS total_samples
  FROM v\$active_session_history ash
  LEFT JOIN sys.audit_actions aud ON ash.sql_opcode = aud.action
  WHERE ash.sql_id IS NOT NULL
    AND ash.sample_time > SYSDATE - 60/1440
  GROUP BY ash.sql_id, aud.name
  ORDER BY total_samples DESC
) t WHERE ROWNUM <= 10;"
    else
      md_line "### ASH 性能采集"
      md_line ""
      md_line "> 已跳过 ASH：未通过 --diagnostics-pack 显式确认许可。"
    fi

    ora_sql_to_md "oracle.performance.cpu_sessions" "CPU 高消耗会话 TOP10" "
SELECT * FROM (
SELECT s.sid, s.serial#, s.username, s.machine, s.sql_id,
       ROUND(SUM(ss.value)/100, 2) cpu_secs
FROM v\$session s
JOIN v\$sesstat ss ON s.sid = ss.sid
JOIN v\$statname sn ON ss.statistic# = sn.statistic#
WHERE sn.name = 'CPU used by this session'
  AND s.status = 'ACTIVE'
GROUP BY s.sid, s.serial#, s.username, s.machine, s.sql_id
ORDER BY cpu_secs DESC
) WHERE ROWNUM <= 10;"

    ora_sql_to_md "oracle.performance.io_files" "IO 消耗最高数据文件 TOP10" "
SELECT * FROM (
SELECT df.file_name,
       f.phyrds, f.phywrts, f.phyblkrd, f.phyblkwrt,
       ROUND((f.phyrds+f.phywrts), 0) total_io
FROM v\$filestat f
JOIN dba_data_files df ON f.file# = df.file_id
ORDER BY total_io DESC
) WHERE ROWNUM <= 10;"

    ora_sql_to_md "oracle.performance.wait_top10" "等待事件 TOP10（数据库级）" "
SELECT * FROM (
  SELECT event, total_waits, time_waited,
         ROUND(time_waited/NULLIF(total_waits,0), 2) avg_wait_cs
  FROM v\$system_event
  WHERE wait_class NOT IN ('Idle','System I/O')
  ORDER BY time_waited DESC
) t WHERE ROWNUM <= 10;"

    if [ "${CONFIG[DIAGNOSTICS_PACK]:-0}" -eq 1 ]; then
      ora_sql_to_md "oracle.performance.top_sql_waits" "TOP SQL 等待事件（近1小时）" "
SELECT * FROM (
  SELECT sql_id, event,
         SUM(cnt) total_samples,
         ROUND(SUM(cnt)*100/SUM(SUM(cnt)) OVER(), 2) pct
  FROM (SELECT sql_id, event, COUNT(*) cnt
        FROM v\$active_session_history
        WHERE sample_time > SYSDATE - 1/24
          AND sql_id IS NOT NULL
          AND wait_class != 'Idle'
        GROUP BY sql_id, event) a
  GROUP BY sql_id, event
  ORDER BY total_samples DESC
) t WHERE ROWNUM <= 10;"
    fi

    ora_sql_to_md "oracle.performance.shared_pool_frag" "共享池碎片" "
SELECT pool,
       ROUND(SUM(bytes)/1024/1024, 2) free_mb,
       ROUND(AVG(bytes)/1024, 2) avg_free_kb,
       COUNT(*) free_chunks
FROM v\$sgastat
WHERE pool IN ('shared pool','large pool')
  AND name = 'free memory'
GROUP BY pool;"

    ora_sql_to_md "oracle.performance.library_cache_hit" "Library Cache 命中率" "
SELECT namespace,
       gets, gethits,
       ROUND(gethitratio * 100, 4) gethit_pct,
       pins, pinhits,
       ROUND(pinhitratio * 100, 4) pinhit_pct,
       reloads, invalidations
FROM v\$librarycache
WHERE namespace IN ('SQL AREA','TABLE/PROCEDURE','BODY','TRIGGER')
ORDER BY namespace;"

    ora_sql_to_md "oracle.performance.buffer_cache_hit" "Buffer Cache 命中率" "
SELECT ROUND((1 - phyrds/(NULLIF(blkgets+consistentgets,0))) * 100, 4) hit_ratio_pct
FROM (SELECT SUM(physical_reads) phyrds,
             SUM(db_block_gets) blkgets,
             SUM(consistent_gets) consistentgets
      FROM v\$buffer_pool_statistics);"

    ora_sql_to_md "oracle.performance.temp_sort_usage" "临时表空间排序段使用" "
SELECT i.instance_name, t.tablespace_name,
       t.current_users,
       ROUND(t.total_blocks * b.value / 1024/1024, 2) total_mb,
       ROUND(t.used_blocks  * b.value / 1024/1024, 2) used_mb,
       TRUNC(ROUND((t.used_blocks / NULLIF(t.total_blocks,0)) * 100)) pct_used,
       t.free_requests
FROM gv\$instance i, gv\$sort_segment t,
     (SELECT value FROM v\$parameter WHERE name='db_block_size') b
WHERE t.inst_id = i.inst_id
ORDER BY i.instance_name, t.tablespace_name;"

    ora_sql_to_md "oracle.performance.undo_usage" "UNDO 使用统计 (v\$undostat)" "
SELECT * FROM (
SELECT begin_time, end_time, undotsn,
       undoblks, txncount, maxquerylen, maxqueryid,
       ssolderrcnt, nospaceerrcnt
FROM v\$undostat
ORDER BY begin_time DESC
) WHERE ROWNUM <= 20;"

    ora_sql_to_md "oracle.performance.undo_params" "UNDO 参数" "
SELECT name, SUBSTR(value, 1, 400) value
FROM v\$parameter
WHERE name IN ('undo_management','undo_tablespace','undo_retention');"
}

# ==================== 模块8: 索引管理 ====================
# 索引与失效对象（无效索引/不可用索引）
collect_index() {
    md_line "## 索引管理"
    md_line ""

    ora_sql_to_md "oracle.index.invalid" "无效/不可用索引" "
SELECT owner, index_name, table_name, index_type, status
FROM dba_indexes
WHERE status NOT IN ('VALID','N/A')
  AND owner NOT IN ('SYS','SYSTEM','OUTLN','DBSNMP','XDB')
ORDER BY owner, table_name, index_name;"

    ora_sql_to_md "oracle.index.unusable_partitioned" "失效分区索引" "
SELECT index_owner, index_name, partition_name, status
FROM dba_ind_partitions
WHERE status NOT IN ('USABLE','N/A')
ORDER BY index_owner, index_name, partition_name;"

    ora_sql_to_md "oracle.index.unusable_subpartitioned" "失效子分区索引" "
SELECT index_owner, index_name, partition_name, subpartition_name, status
FROM dba_ind_subpartitions
WHERE status NOT IN ('USABLE','N/A')
ORDER BY index_owner, index_name, subpartition_name;"

    ora_sql_to_md "oracle.index.large_segments" "大索引段 TOP20" "
SELECT * FROM (
SELECT owner, segment_name AS index_name, tablespace_name,
       ROUND(bytes/1024/1024, 2) size_mb
FROM dba_segments
WHERE segment_type IN ('INDEX','INDEX PARTITION','INDEX SUBPARTITION')
  AND owner NOT IN ('SYS','SYSTEM','OUTLN','DBSNMP','XDB')
ORDER BY bytes DESC
) WHERE ROWNUM <= 20;"
}

# ==================== 模块9: ADG 备库（有才采集）====================
# ADG 备库状态：MRP 应用、延迟、Gap
collect_adg() {
    md_line "## ADG 数据守护"
    md_line ""

    # 前置检测：是否配置了 DG（主库有有效STANDBY目标，或本库是备库）
    local dg_cnt
    dg_cnt=$(ora_try "SELECT COUNT(*) FROM v\$archive_dest WHERE target='STANDBY' AND status!='INACTIVE';" | tr -d ' \r\n')
    if [ "${dg_cnt:-0}" -eq 0 ] && [ "${ENV[IS_STANDBY]}" -ne 1 ]; then
        md_line "> 未配置 ADG（无STANDBY归档目标），跳过本模块。"
        return 0
    fi

    # 无论主备都采集基础信息
    ora_sql_to_md "oracle.adg.sync_stats" "DataGuard 同步统计" "
SELECT name, value FROM v\$dataguard_stats
ORDER BY name;"

    ora_sql_to_md "oracle.adg.params" "DataGuard 配置参数" "
SELECT name, value
FROM v\$parameter
WHERE name IN (
    'log_archive_dest_1','log_archive_dest_2','log_archive_dest_state_1',
    'log_archive_dest_state_2','fal_server','fal_client','db_file_name_convert',
    'log_file_name_convert','standby_file_management',
    'archive_lag_target','log_archive_max_processes'
)
ORDER BY name;"

    ora_sql_to_md "oracle.adg.mrp_status" "Managed Standby 进程状态" "
SELECT process, status, client_process, sequence#, thread#, block#, blocks
FROM v\$managed_standby
ORDER BY process;"

    ora_sql_to_md "oracle.adg.transport_apply" "日志传输/应用状态" "
SELECT dest_id, type, status, error,
       archived_seq#, applied_seq#
FROM v\$archive_dest_status
WHERE status != 'INACTIVE'
ORDER BY dest_id;"

    if [ "${ENV[IS_STANDBY]}" -eq 1 ]; then
        md_line ""
        md_line "### 备库专项检查"

        ora_sql_to_md "oracle.adg.file_sync" "备库文件同步状态" "
SELECT name, status, bytes/1024/1024 size_mb
FROM v\$datafile
ORDER BY name;"

        ora_sql_to_md "oracle.adg.log_gap" "主备日志差距（已归档 vs 已应用）" "
SELECT a.thread#,
       a.max_seq_primary,
       b.max_seq_standby,
       a.max_seq_primary - b.max_seq_standby gap
FROM (SELECT thread#, MAX(sequence#) max_seq_primary
      FROM v\$archived_log
      WHERE archived = 'YES' GROUP BY thread#) a,
     (SELECT thread#, MAX(sequence#) max_seq_standby
      FROM v\$archived_log
      WHERE applied = 'YES' GROUP BY thread#) b
WHERE a.thread# = b.thread#;"

        ora_sql_to_md "oracle.adg.switchover_check" "备库角色切换检查" "
SELECT switchover_status, database_role, db_unique_name
FROM v\$database;"

    fi
}

# ==================== 模块10: RAC 专项（有才采集）====================
# RAC 全局视图：实例/节点/服务分布
collect_rac() {
    if [ "${ENV[IS_RAC]}" -ne 1 ]; then
        md_line "## RAC 专项"
        md_line ""
        md_line "> 非 RAC 环境，跳过本模块。"
        return 0
    fi

    md_line "## RAC 专项"
    md_line ""

    ora_sql_to_md "oracle.rac.instance_status" "RAC 各节点实例状态" "
SELECT inst_id, instance_name, host_name, version,
       startup_time, status, database_status, instance_role
FROM gv\$instance
ORDER BY inst_id;"

    ora_sql_to_md "oracle.rac.node_perf" "RAC 各节点性能对比" "
COLUMN value FORMAT 99999999999999999
SELECT inst_id, name,
       ROUND(value, 0) value
FROM gv\$sysstat
WHERE name IN ('CPU used by this session','physical reads',
               'physical writes','user commits','user rollbacks',
               'redo size','parse count (total)','execute count')
ORDER BY name, inst_id;"

    ora_sql_to_md "oracle.rac.channel_waits" "RAC 通道等待分析" "
SELECT inst_id, event, COUNT(*) sessions,
       ROUND(AVG(wait_time), 2) avg_wait,
       ROUND(AVG(seconds_in_wait), 2) avg_sec_in_wait,
       SUM(CASE WHEN state='WAITING' THEN 1 ELSE 0 END) active_waiting
FROM gv\$session_wait
WHERE wait_class != 'Idle'
  AND event LIKE '%channel%'
GROUP BY inst_id, event
ORDER BY inst_id, sessions DESC;"

    ora_sql_to_md "oracle.rac.node_waits" "RAC 各节点等待事件（超10个会话）" "
SELECT inst_id, event, COUNT(*) cnt
FROM gv\$session_wait
GROUP BY inst_id, event
HAVING COUNT(*) > 10
ORDER BY inst_id ASC, cnt DESC;"

    ora_sql_to_md "oracle.rac.temp_sql" "RAC 消耗临时空间 SQL（>1GB）" "
SELECT * FROM (
SELECT inst_id, session_id, session_serial#, sql_exec_id, sql_exec_start,
       sql_id, sql_plan_hash_value,
       MIN(sample_time) min_time,
       MAX(sample_time) max_time,
       ROUND(MAX(temp_space_allocated)/1024/1024, 2) temp_mb
FROM gv\$active_session_history
WHERE temp_space_allocated >= 1024*1024*1024
GROUP BY inst_id, session_id, session_serial#, sql_exec_id, sql_exec_start,
         sql_id, sql_plan_hash_value
ORDER BY temp_mb DESC
) WHERE ROWNUM <= 20;"

    ora_sql_to_md "oracle.rac.ges_gc_waits" "RAC GES/GC 锁等待" "
SELECT event, total_waits, time_waited,
       ROUND(average_wait, 2) avg_wait_cs
FROM v\$system_event
WHERE wait_class = 'Cluster'
ORDER BY time_waited DESC;"
}

# ==================== 模块11: RMAN 备份检查 ====================
# RMAN 备份历史与保留策略（近 N 天）
collect_rman() {
    md_line "## RMAN 备份检查"
    md_line ""

    ora_sql_to_md "oracle.rman.backup_summary" "最近备份集汇总" "
SELECT * FROM (
SELECT bs.recid,
       DECODE(bs.backup_type,'D','Full','I','Incremental','L','Archivelog',bs.backup_type) bk_type,
       bs.incremental_level,
       bs.start_time, bs.completion_time,
       ROUND(bs.elapsed_seconds/60, 1) elapsed_min,
       ROUND(SUM(bp.bytes)/1024/1024/1024, 3) size_gb
FROM v\$backup_set bs
JOIN v\$backup_piece bp ON bs.set_stamp = bp.set_stamp AND bs.set_count = bp.set_count
WHERE bs.start_time >= SYSDATE - 30
GROUP BY bs.recid, bs.backup_type, bs.incremental_level,
         bs.start_time, bs.completion_time, bs.elapsed_seconds
ORDER BY bs.start_time DESC
) WHERE ROWNUM <= 20;"

    ora_sql_to_md "oracle.rman.policy" "备份策略参数（RMAN CONFIGURE）" "
SELECT name, value
FROM v\$rman_configuration
ORDER BY name;"

    ora_sql_to_md "oracle.rman.history" "RMAN 备份历史状态（近7天）" "
SELECT session_key, input_type, status,
       start_time, end_time,
       ROUND(output_bytes/1024/1024/1024, 3) output_gb,
       ROUND(elapsed_seconds/60, 1) elapsed_min
FROM v\$rman_backup_job_details
WHERE start_time >= SYSDATE - 7
ORDER BY start_time DESC;"

    # 检查 os 层是否有备份相关 cron
    md_line ""
    md_line "### OS层备份任务"
    md_block_start
    local cron_bk=""
    if has_cmd crontab; then
        cron_bk=$(crontab -l 2>/dev/null | grep -iE "(rman|backup|orabackup|dbbackup|archivelog|bkup)" | grep -v "^#" | head -10)
    fi
    if [ -n "$cron_bk" ]; then
        printf "# 用户 crontab 中检测到备份任务:\n" >> "${RUNTIME[REPORT_FILE]}"
        printf "%s\n" "$cron_bk" >> "${RUNTIME[REPORT_FILE]}"
    else
        local sys_cron
        sys_cron=$(grep -rhiE "(rman|backup|orabackup)" /etc/cron.d/ /etc/cron.daily/ /etc/cron.weekly/ 2>/dev/null | grep -v "^#" | head -10)
        if [ -n "$sys_cron" ]; then
            printf "# 系统 cron 中检测到备份任务:\n" >> "${RUNTIME[REPORT_FILE]}"
            printf "%s\n" "$sys_cron" >> "${RUNTIME[REPORT_FILE]}"
        else
            printf "# 未在 crontab 和 /etc/cron.* 中检测到 RMAN 备份任务，请确认备份策略\n" >> "${RUNTIME[REPORT_FILE]}"
        fi
    fi
    md_block_end

    # 检查备份目录
    md_line ""
    md_line "### 备份目录检查"
    md_line "| 目录 | 状态 | 说明 |"
    md_line "| --- | --- | --- |"
    local BACKUP_DIRS=(/backup /data/backup /oradata/backup /rman_backup /opt/oracle/backup)
    local found_bk=0
    for bdir in "${BACKUP_DIRS[@]}"; do
        if [ -d "$bdir" ]; then
            local lfile ltime age48h
            lfile=$(ls -t "$bdir" 2>/dev/null | head -1)
            age48h=$(find "$bdir" -maxdepth 1 -mtime -2 2>/dev/null | wc -l)
            if [ -n "$lfile" ]; then
                ltime=$(stat -c "%y" "${bdir}/${lfile}" 2>/dev/null || stat -f "%Sm" -t "%Y-%m-%d %H:%M:%S" "${bdir}/${lfile}" 2>/dev/null | cut -d'.' -f1)
                if [ "${age48h:-0}" -eq 0 ]; then
                    md_line "| ${bdir} | ⚠️ 警告 | 最新: ${lfile} (${ltime:-未知})，近48h无新备份 |"
                else
                    md_line "| ${bdir} | ✅ 正常 | 最新: ${lfile} (${ltime:-未知}) |"
                fi
                found_bk=1
            else
                md_line "| ${bdir} | ⚠️ 目录为空 | - |"
            fi
        fi
    done
    [ "$found_bk" -eq 0 ] && md_line "| 未找到备份目录 | ⚠️ 请确认备份路径 | 已检查: ${BACKUP_DIRS[*]} |" || true
}

# ==================== 模块12: 告警日志扫描 ====================
# 告警日志扫描：ORA-/错误行归类（裸 timeout 30s 保护）
collect_alert_log() {
    # 开关判断
    if [ "${CONFIG[ALERT_LOG]}" -ne 1 ]; then
        md_line "## 告警日志扫描"
        md_line ""
        md_line "> 告警日志扫描已关闭（--no-alert-log），跳过。"
        return 0
    fi
    md_line "## 告警日志扫描"
    md_line ""

    # 尝试从 v$diag_info 获取告警日志路径
    local diag_dest
    diag_dest=$(ora_try "SELECT value FROM v\$diag_info WHERE name='Diag Trace';" | tr -d ' \r\n')
    if [ -z "$diag_dest" ]; then
        diag_dest=$(ora_try "SELECT value FROM v\$parameter WHERE name='background_dump_dest';" | tr -d ' \r\n')
    fi

    md_line "### 诊断目录信息"
    ora_sql_to_md "oracle.alert.diag_path" "ADR Diag 路径" "
SELECT name, value FROM v\$diag_info ORDER BY name;"

    # 构建告警日志路径
    local diag_home
    diag_home=$(ora_try "SELECT value FROM v\$diag_info WHERE name='ADR Home';" | tr -d ' \r\n')

    local ALERT_LOG=""
    if [ -n "$diag_home" ]; then
        ALERT_LOG="${diag_home}/alert/log.xml"
        [ ! -f "$ALERT_LOG" ] && ALERT_LOG="${diag_home}/trace/alert_${ENV[DB_NAME]:-oracle}.log"
    fi
    if [ -z "$ALERT_LOG" ] || [ ! -f "$ALERT_LOG" ]; then
        # 传统路径
        if [ -n "$diag_dest" ]; then
            ALERT_LOG="${diag_dest}/alert_${ENV[DB_NAME]:-oracle}.log"
        fi
    fi

    if [ -n "$ALERT_LOG" ] && [ -f "$ALERT_LOG" ] && [ -r "$ALERT_LOG" ]; then
        md_line ""
        md_line "告警日志路径: \`${ALERT_LOG}\`"
        md_line ""
        md_line "### 最近关键异常（ERROR/WARNING/ORA-，最近1000行/2000行）"
        md_block_start
        local err_count=0
        local _tmp
        local is_xml=0
        # 检测是否为 XML 格式（ADR 路径中 log.xml）
        [[ "$ALERT_LOG" = */alert/log.xml ]] && is_xml=1
        [ "$is_xml" -eq 0 ] && head -1 "$ALERT_LOG" 2>/dev/null | grep -q '^<?xml' && is_xml=1
        if has_cmd mktemp; then
            _tmp="$(mktemp /tmp/oracle_alert.XXXXXX 2>/dev/null)" || _tmp="/tmp/oracle_alert_$$"
        else
            # Windows 或无 mktemp 的环境
            _tmp="/tmp/oracle_alert_$$"
        fi
        touch "$_tmp" 2>/dev/null || { md_line "> 无法创建临时文件，跳过告警日志扫描"; return 0; }
        if [ "$is_xml" -eq 1 ]; then
            # XML 格式（ADR 11g+ 默认）：先取最近2000行再搜索，限制时间
            progress "扫描 XML 告警日志（最近2000行）..."
            if has_cmd timeout; then
                timeout 30s tail -2000 "$ALERT_LOG" 2>/dev/null | grep -iE "(ORA-|ERROR|FATAL|CORRUPT|CRASH|ABORT)" | tail -300 > "$_tmp"
            else
                tail -2000 "$ALERT_LOG" 2>/dev/null | grep -iE "(ORA-|ERROR|FATAL|CORRUPT|CRASH|ABORT)" | tail -300 > "$_tmp"
            fi
            # 如果 XML 解析无结果，回退到文本方式
            if [ ! -s "$_tmp" ]; then
                is_xml=0
                progress "XML 解析无结果，回退到文本格式扫描"
            fi
        fi
        if [ "$is_xml" -eq 0 ]; then
            # 文本格式：取最近1000行，限制处理时间
            progress "扫描文本告警日志（最近1000行）..."
            # 使用 iconv 转码（如果可用）
            if has_cmd iconv; then
                if has_cmd timeout; then
                    # 尝试 GBK 转码
                    timeout 30s iconv -f GBK -t UTF-8 "$ALERT_LOG" 2>/dev/null | tail -1000 | grep -E "(ORA-|ERROR|FATAL|CORRUPT|CRASH|ABORT)" > "$_tmp" 2>/dev/null || \
                    # 尝试 GB18030 转码
                    timeout 30s iconv -f GB18030 -t UTF-8 "$ALERT_LOG" 2>/dev/null | tail -1000 | grep -E "(ORA-|ERROR|FATAL|CORRUPT|CRASH|ABORT)" > "$_tmp" 2>/dev/null || \
                    # 直接处理
                    timeout 30s tail -1000 "$ALERT_LOG" 2>/dev/null | grep -E "(ORA-|ERROR|FATAL|CORRUPT|CRASH|ABORT)" > "$_tmp" 2>/dev/null
                else
                    # 无 timeout 命令
                    iconv -f GBK -t UTF-8 "$ALERT_LOG" 2>/dev/null | tail -1000 | grep -E "(ORA-|ERROR|FATAL|CORRUPT|CRASH|ABORT)" > "$_tmp" 2>/dev/null || \
                    iconv -f GB18030 -t UTF-8 "$ALERT_LOG" 2>/dev/null | tail -1000 | grep -E "(ORA-|ERROR|FATAL|CORRUPT|CRASH|ABORT)" > "$_tmp" 2>/dev/null || \
                    tail -1000 "$ALERT_LOG" 2>/dev/null | grep -E "(ORA-|ERROR|FATAL|CORRUPT|CRASH|ABORT)" > "$_tmp" 2>/dev/null
                fi
            else
                # 无 iconv 命令
                if has_cmd timeout; then
                    timeout 30s tail -1000 "$ALERT_LOG" 2>/dev/null | grep -E "(ORA-|ERROR|FATAL|CORRUPT|CRASH|ABORT)" > "$_tmp" 2>/dev/null
                else
                    tail -1000 "$ALERT_LOG" 2>/dev/null | grep -E "(ORA-|ERROR|FATAL|CORRUPT|CRASH|ABORT)" > "$_tmp" 2>/dev/null
                fi
            fi
        fi
        while IFS= read -r line; do
            # 过滤 ORA-00942、ORA-01403 等常见无害错误
            if printf '%s' "$line" | grep -qiE "(ORA-00942|ORA-01403|ORA-06512)"; then
                continue
            fi
            local safe_line
            safe_line=$(printf '%s' "$line" | tr '|' ';' | cut -c1-250)
            printf "%s\n" "$safe_line" >> "${RUNTIME[REPORT_FILE]}"
            err_count=$((err_count + 1))
        done < "$_tmp"
        rm -f "$_tmp"
        [ "$err_count" -eq 0 ] && printf "最近1000行（文本）或2000行（XML）未发现 ORA-/ERROR/FATAL/CORRUPT 关键词\n" >> "${RUNTIME[REPORT_FILE]}"
        md_block_end
        md_line ""
        [ "$err_count" -gt 0 ] && md_line "> 共发现 **${err_count}** 条告警记录" || true
    else
        md_line ""
        md_line "> ⚠️ 告警日志路径无法访问（路径: ${ALERT_LOG:-未知}），请手动检查。"
        md_line "> 通常路径: \`\$ORACLE_BASE/diag/rdbms/\$DBNAME/\$INSTANCE/trace/alert_\$INSTANCE.log\`"
    fi
}

# ==================== 模块13: PDB 容器数据库巡检 ====================
# PDB 详情（打开模式、大小、资源限制）
collect_pdb() {
    if [ "${ENV[IS_CDB]}" -ne 1 ]; then
        return 0
    fi

    md_line "## PDB 容器数据库巡检"
    md_line ""

    ora_sql_to_md "oracle.pdb.status" "PDB 状态详情" "
SELECT con_id, name, open_mode, restricted, creation_time,
       total_size/1024/1024/1024 total_size_gb,
       used_space/1024/1024/1024 used_size_gb
FROM v\$pdbs
ORDER BY con_id;"

    ora_sql_to_md "oracle.pdb.resource_limits" "PDB 资源限制" "
SELECT c.name AS pdb_name,
       r.resource_name, r.current_utilization, r.max_utilization, r.limit_value
FROM v\$resource_limit r
JOIN v\$containers c ON r.con_id = c.con_id
WHERE c.name != 'CDB\$ROOT'
ORDER BY c.name, r.resource_name;"

    ora_sql_to_md "oracle.pdb.tablespace_usage" "PDB 表空间使用率" "
SELECT c.name AS pdb_name,
       df.tablespace_name,
       ROUND(SUM(df.bytes)/1024/1024, 2) total_mb,
       ROUND(SUM(df.bytes - NVL(fs.bytes, 0))/1024/1024, 2) used_mb,
       ROUND(CASE WHEN SUM(df.bytes)=0 THEN 0
             ELSE (SUM(df.bytes - NVL(fs.bytes, 0))/SUM(df.bytes)) * 100
             END, 2) pct_used,
       ROUND(MAX(df.maxbytes)/1024/1024, 2) max_mb
FROM cdb_data_files df
LEFT JOIN cdb_free_space fs
  ON df.tablespace_name = fs.tablespace_name AND df.con_id = fs.con_id
JOIN v\$containers c ON df.con_id = c.con_id
WHERE c.name NOT IN ('CDB\$ROOT', 'PDB\$SEED')
GROUP BY c.name, df.tablespace_name
ORDER BY c.name, pct_used DESC;"

    ora_sql_to_md "oracle.pdb.sessions" "PDB 会话数统计" "
SELECT c.name AS pdb_name,
       COUNT(s.sid) session_count,
       SUM(DECODE(s.status, 'ACTIVE', 1, 0)) active_count
FROM v\$session s
JOIN v\$containers c ON s.con_id = c.con_id
WHERE c.name != 'CDB\$ROOT'
GROUP BY c.name
ORDER BY session_count DESC;"

    ora_sql_to_md "oracle.pdb.services" "PDB 服务配置" "
SELECT pdb, name service_name, creation_date,
       enabled, goal, clb_goal
FROM v\$services
WHERE pdb IS NOT NULL AND pdb != 'CDB\$ROOT'
ORDER BY pdb, name;"

    ora_sql_to_md "oracle.pdb.flashback" "PDB 闪回配置" "
SELECT name, open_mode,
       log_mode
FROM v\$pdbs
ORDER BY con_id;"

    ora_sql_to_md "oracle.pdb.cdb_flashback" "CDB 级别闪回配置" "
SELECT flashback_on, log_mode, force_logging, supplemental_log_data_min
FROM v\$database;"

    ora_sql_to_md "oracle.pdb.role_status" "PDB 角色与切换状态" "
SELECT name AS pdb_name, open_mode, restricted
FROM v\$containers
WHERE name != 'CDB\$ROOT';"
}

# ==================== 模块14: RAC 高可用检查 ====================
# RAC HA：GV$ 资源/服务/集群状态
collect_rac_ha() {
    if [ "${ENV[IS_RAC]}" -ne 1 ]; then
        return 0
    fi

    md_line "## RAC 高可用检查"
    md_line ""

    # CRS 资源状态 — 优先用 crsctl（需 grid 用户），sqlplus 查询 Grid 视图可能无权限
    # ★ OF-08：RAC 上 crsctl/ocrcheck 可慢可 hang → 全部套 15s 超时
    if has_cmd crsctl; then
        md_line "### CRS 资源状态 (crsctl)"
        md_block_start
        run_with_timeout 15 crsctl query crs activeversion 2>/dev/null >> "${RUNTIME[REPORT_FILE]}" || echo "crsctl activeversion failed" >> "${RUNTIME[REPORT_FILE]}"
        run_with_timeout 15 crsctl stat res -t 2>/dev/null | head -100 >> "${RUNTIME[REPORT_FILE]}" || echo "crsctl stat res failed" >> "${RUNTIME[REPORT_FILE]}"
        md_block_end

        md_line "### CRS 守护进程状态 (crsctl)"
        md_block_start
        run_with_timeout 15 crsctl check crs 2>/dev/null >> "${RUNTIME[REPORT_FILE]}" || echo "crsctl check crs failed" >> "${RUNTIME[REPORT_FILE]}"
        md_block_end
    fi

    # OCR 表决盘 — 用 crsctl/ocrcheck 命令（oracle 用户可执行）
    md_line "### OCR 表决盘信息 (crsctl)"
    md_block_start
    if has_cmd ocrcheck; then
        run_with_timeout 15 ocrcheck 2>/dev/null >> "${RUNTIME[REPORT_FILE]}" || echo "ocrcheck failed" >> "${RUNTIME[REPORT_FILE]}"
    else
        echo "ocrcheck not found" >> "${RUNTIME[REPORT_FILE]}"
    fi
    if has_cmd crsctl; then
        echo "" >> "${RUNTIME[REPORT_FILE]}"
        run_with_timeout 15 crsctl query css votedisk 2>/dev/null >> "${RUNTIME[REPORT_FILE]}" || echo "crsctl query css votedisk failed" >> "${RUNTIME[REPORT_FILE]}"
    fi
    md_block_end

    # GPNP 检查
    if has_cmd gpnptool; then
        md_line ""
        md_line "### GPNP Profile 状态"
        md_block_start
        # ★ OF-08：gpnptool 在 RAC 上可慢可 hang → 套 15s 超时
        if run_with_timeout 15 gpnptool get 2>/dev/null | head -50 >> "${RUNTIME[REPORT_FILE]}"; then
            :
        else
            echo "GPNP tool not accessible" >> "${RUNTIME[REPORT_FILE]}"
        fi
        md_block_end
    fi

    # 服务配置检查
    ora_sql_to_md "oracle.service.config" "Service 服务配置" "
SELECT * FROM (
SELECT name, failover_method, failover_type,
       goal, clb_goal, edition
FROM dba_services
ORDER BY name
) WHERE ROWNUM <= 50;"
}

# ==================== 模块15: DG Broker 巡检 ====================
# Data Guard Broker 配置与状态
collect_dg_broker() {
    # 检查是否配置了 DG Broker
    local dg_broker_config
    dg_broker_config=$(ora_try "SELECT COUNT(*) FROM v\$parameter WHERE name='dg_broker_start' AND value='TRUE';" | tr -d ' \r\n')

    md_line "## Data Guard Broker 巡检"
    md_line ""

    if [ "${dg_broker_config:-0}" -eq 0 ]; then
        md_line "> DG Broker 未启用 (dg_broker_start != TRUE)，跳过详细检查。"
    else
        # v$dataguard_config 在某些版本可能不可用，添加错误处理
        ora_sql_to_md "oracle.dg.broker_overview" "DG Broker 配置概览" "
SELECT config_seq_num, database_role, flush_seq#, flush_error,
       CAST(last_sent AS DATE) last_sent,
       CAST(last_received AS DATE) last_received
FROM v\$dataguard_config
WHERE ROWNUM <= 10;"
    fi

    ora_sql_to_md "oracle.dg.broker_enabled" "DG Broker 启用状态" "
SELECT name, SUBSTR(value, 1, 100) value
FROM v\$parameter
WHERE name = 'dg_broker_start';"

    ora_sql_to_md "oracle.dg.archive_dest" "日志传输服务状态 (v\$archive_dest)" "
SELECT dest_id, dest_name, type, status, error,
       transmit_mode, compression
FROM v\$archive_dest
WHERE status != 'INACTIVE'
ORDER BY dest_id;"

    ora_sql_to_md "oracle.dg.protection_mode" "保护模式与主库统计" "
SELECT name, value
FROM v\$dataguard_stats;"

    # Fast-Start Failover 检查 (依赖 DGMGRL 配置)
    ora_sql_to_md "oracle.dg.broker_params" "DG Broker 配置参数" "
SELECT name, value
FROM v\$parameter
WHERE name LIKE 'dg_broker%%'
ORDER BY name;"

    ora_sql_to_md "oracle.dg.recent_events" "最近 DG 事件 (v\$dataguard_status)" "
SELECT * FROM (
SELECT timestamp, message, error_code
FROM v\$dataguard_status
WHERE timestamp > SYSDATE - 1
ORDER BY timestamp DESC
) WHERE ROWNUM <= 20;"

    ora_sql_to_md "oracle.dg.log_sequence_gap" "主备库日志序差距" "
SELECT al.thread#,
       MAX(al.sequence#) - MAX(bl.sequence#) gap
FROM v\$archived_log al
JOIN (SELECT thread#, MAX(sequence#) sequence# FROM v\$archived_log WHERE applied='YES' GROUP BY thread#) bl
  ON al.thread# = bl.thread#
WHERE al.archived = 'YES'
GROUP BY al.thread#;"

    # 物理备库专项
    if [ "${ENV[IS_STANDBY]}" -eq 1 ]; then
        ora_sql_to_md "oracle.dg.rfs_status" "备库 RFS 进程状态" "
SELECT process, status, thread#, sequence#, block#, blocks, delay_mins
FROM v\$managed_standby
WHERE process IN ('RFS','MRP0','MRP','LSP')
ORDER BY process;"

        ora_sql_to_md "oracle.dg.apply_lag" "备库应用延迟监控" "
SELECT name, value, unit, time_computed
FROM v\$dataguard_stats
WHERE name IN ('apply lag', 'transport lag', 'estimated startup time');"
    fi
}

# ==================== 模块16: 闪回数据库巡检 ====================
# 闪回区/闪回能力（恢复区使用率、闪回日志）
collect_flashback() {
    md_line "## 闪回数据库巡检"
    md_line ""

    ora_sql_to_md "oracle.flashback.log_usage" "闪回日志使用统计" "
SELECT ROUND(flashback_data/1024/1024, 2) fbdata_mb,
       ROUND(db_data/1024/1024, 2) dbdata_mb,
       ROUND(redo_data/1024/1024, 2) redo_mb,
       ROUND(estimated_flashback_size/1024/1024/1024, 3) est_size_gb
FROM v\$flashback_database_stat
WHERE end_time = (SELECT MAX(end_time) FROM v\$flashback_database_stat);"

    ora_sql_to_md "oracle.flashback.area_usage" "闪回区使用情况" "
SELECT space_limit/1024/1024/1024 limit_gb,
       space_used/1024/1024/1024 used_gb,
       space_used/space_limit * 100 pct_used,
       space_reclaimable/1024/1024/1024 reclaimable_gb,
       number_of_files
FROM v\$recovery_file_dest;"

    ora_sql_to_md "oracle.flashback.guaranteed_rp" "Guaranteed Restore Point" "
SELECT name, storage_size/1024/1024/1024 size_gb,
       restore_point_time, guarantee_flashback_database
FROM v\$restore_point
WHERE guarantee_flashback_database = 'YES'
ORDER BY restore_point_time DESC;"

    ora_sql_to_md "oracle.flashback.normal_rp" "普通 Restore Point" "
SELECT * FROM (
SELECT name, scn, storage_size/1024/1024 size_mb,
       restore_point_time, guarantee_flashback_database
FROM v\$restore_point
WHERE guarantee_flashback_database = 'NO'
ORDER BY restore_point_time DESC
) WHERE ROWNUM <= 20;"

    # PDB 闪回检查
    if [ "${ENV[IS_CDB]}" -eq 1 ]; then
        ora_sql_to_md "oracle.flashback.pdb_flashback" "PDB 级别闪回配置" "
SELECT con_id, name, open_mode, restricted
FROM v\$pdbs;"
    fi
}

# ==================== 模块17: 审计与安全巡检 ====================
# 审计与安全参数（审计开关、口令策略）
collect_audit_security() {
    md_line "## 审计与安全巡检"
    md_line ""

    ora_sql_to_md "oracle.audit.fga" "审计策略检查 (FGA)" "
SELECT policy_name, object_schema, object_name, enabled
FROM dba_audit_policies;"

    ora_sql_to_md "oracle.audit.failed_logins" "失败登录审计统计" "
SELECT userid, COUNT(*) cnt, MAX(ntimestamp#) last_attempt
FROM sys.aud$
WHERE action# = 100 AND returncode != 0 AND ntimestamp# > SYSDATE - 7
GROUP BY userid
ORDER BY cnt DESC;"

    ora_sql_to_md "oracle.audit.profile_params" "密码配置文件参数" "
SELECT profile, resource_name, resource_type, limit
FROM dba_profiles
WHERE profile = 'DEFAULT'
  AND resource_name IN ('PASSWORD_VERIFY_FUNCTION','PASSWORD_LIFE_TIME',
                        'PASSWORD_REUSE_TIME','PASSWORD_REUSE_MAX',
                        'FAILED_LOGIN_ATTEMPTS','PASSWORD_LOCK_TIME')
ORDER BY resource_name;"

    ora_sql_to_md "oracle.audit.profile_nondefault" "默认配置文件非默认设置" "
SELECT profile, resource_name, limit
FROM dba_profiles
WHERE profile != 'DEFAULT'
  AND resource_name LIKE 'PASSWORD%'
ORDER BY profile, resource_name;"

    ora_sql_to_md "oracle.audit.locked_accounts" "锁定账户检查" "
SELECT username, account_status, lock_date,
       TRUNC(lock_date - SYSDATE) days_locked
FROM dba_users
WHERE account_status LIKE '%LOCKED%'
ORDER BY username;"

    ora_sql_to_md "oracle.audit.privileged_users" "特权用户 (DBA角色)" "
SELECT grantee, granted_role, admin_option, common
FROM dba_role_privs
WHERE granted_role = 'DBA'
  AND grantee NOT IN ('SYS','SYSTEM','OUTLN')
ORDER BY grantee;"

    ora_sql_to_md "oracle.audit.system_privileges" "系统特权授予 (非标准用户)" "
SELECT grantee, privilege, admin_option, common
FROM dba_sys_privs
WHERE grantee NOT IN ('SYS','SYSTEM','OUTLN','DBSNMP','ORACLE_OCM')
  AND admin_option = 'YES'
ORDER BY grantee, privilege;"

    ora_sql_to_md "oracle.audit.password_file_members" "密码文件成员" "
SELECT username, sysdba, sysoper, sysasm
FROM v\$pwfile_users
ORDER BY username;"

    ora_sql_to_md "oracle.audit.object_audit" "对象审计统计" "
SELECT * FROM (
SELECT owner, obj_name, action_name, count(*) exec_count
FROM dba_audit_trail
WHERE extended_timestamp > SYSDATE - 7
  AND obj_name IS NOT NULL
GROUP BY owner, obj_name, action_name
ORDER BY exec_count DESC
) WHERE ROWNUM <= 20;"
}

# ==================== 模块18: AWR 与统计信息巡检 ====================
# AWR 快照与统计（需 Diagnostics Pack 许可）
collect_awr_stats() {
    md_line "## AWR 与统计信息巡检"
    md_line ""

    ora_sql_to_md "oracle.awr.package_access" "管理包访问设置" "
SELECT name, value
FROM v\$parameter
WHERE name IN ('control_management_pack_access','statistics_level');"

    if [ "${CONFIG[DIAGNOSTICS_PACK]:-0}" -eq 1 ]; then
      ora_sql_to_md "oracle.awr.baselines" "AWR 基线列表" "
SELECT baseline_id, baseline_name, start_snap_id, end_snap_id,
       baseline_type, moving_window_size
FROM dba_hist_baseline
ORDER BY baseline_id;"

    ora_sql_to_md "oracle.awr.recent_snapshots" "最近 AWR 快照" "
SELECT * FROM (
SELECT snap_id, instance_number, startup_time,
       CAST(begin_interval_time AS DATE) begin_time,
       CAST(end_interval_time AS DATE) end_time,
       flush_elapsed
FROM dba_hist_snapshot
WHERE end_interval_time > SYSDATE - 1
ORDER BY snap_id DESC
) WHERE ROWNUM <= 20;"
    else
      md_line ""
      md_line "> AWR/ASH/ADDM 已按许可保护策略跳过；统计信息与对象检查继续执行。"
    fi

    ora_sql_to_md "oracle.awr.sql_plan_baseline" "SQL Plan Baseline" "
SELECT * FROM (
SELECT sql_handle, plan_name, origin, enabled, accepted,
       fixed, autopurge, created
FROM dba_sql_plan_baselines
ORDER BY created DESC
) WHERE ROWNUM <= 20;"

    ora_sql_to_md "oracle.awr.auto_stats_job" "自动统计信息收集 Job" "
SELECT client_name, status, window_group
FROM dba_autotask_client
ORDER BY client_name;"

    ora_sql_to_md "oracle.awr.missing_stats" "缺失统计信息表 (TOP20)" "
SELECT * FROM (
SELECT owner, table_name, num_rows, last_analyzed,
       stale_stats
FROM dba_tab_statistics
WHERE stale_stats = 'YES'
  AND owner NOT IN ('SYS','SYSTEM','OUTLN','DBSNMP')
ORDER BY last_analyzed NULLS FIRST
) WHERE ROWNUM <= 20;"

    ora_sql_to_md "oracle.awr.hwm_segments" "段级高水位统计 (HWM)" "
SELECT * FROM (
SELECT owner, segment_name, segment_type, tablespace_name,
       bytes/1024/1024/1024 size_gb,
       blocks, header_block
FROM dba_segments
WHERE owner NOT IN ('SYS','SYSTEM')
  AND segment_type IN ('TABLE','TABLE PARTITION')
  AND bytes > 100*1024*1024
ORDER BY bytes DESC
) WHERE ROWNUM <= 20;"

    ora_sql_to_md "oracle.awr.segment_bloat" "数据膨胀预估 (DBA_SEGMENTS vs DBA_TABLES)" "
SELECT * FROM (
SELECT t.owner, t.table_name, t.num_rows,
       s.bytes/1024/1024 segment_mb,
       t.avg_row_len * t.num_rows / 1024 / 1024 estimated_mb,
       (s.bytes - t.avg_row_len * t.num_rows) / 1024 / 1024 bloat_mb
FROM dba_tables t
JOIN dba_segments s ON t.owner = s.owner AND t.table_name = s.segment_name
WHERE t.num_rows > 10000
  AND s.bytes > t.avg_row_len * t.num_rows * 1.5
  AND t.owner NOT IN ('SYS','SYSTEM','OUTLN','DBSNMP')
ORDER BY bloat_mb DESC
) WHERE ROWNUM <= 20;"

    ora_sql_to_md "oracle.awr.assm_status" "自动段空间管理 (ASSM) 状态" "
SELECT tablespace_name, segment_space_management, extent_management
FROM dba_tablespaces
WHERE segment_space_management = 'AUTO'
ORDER BY tablespace_name;"

    ora_sql_to_md "oracle.awr.index_usage" "索引使用状态 (未使用/使用中)" "
SELECT * FROM (
SELECT index_name, table_name, monitoring, used, start_monitoring, end_monitoring
FROM v\$object_usage
ORDER BY used, start_monitoring DESC
) WHERE ROWNUM <= 100;" 2>/dev/null || md_line "> v\$object_usage 不可用（需 ALTER INDEX ... MONITORING USAGE 启用监控）"
}

# ==================== 模块 SYS-HIST: SAR 历史数据采集 ====================

# ==================== 采集器：sar 历史与实时采样 ====================
# 定位 /var/log/sa/sa<DD> 近 N 小时内的 sar 数据文件
find_sar_files() {
    local d
    for d in /var/log/sa /var/log/sysstat; do
        [ -d "$d" ] || continue
        find "$d" -maxdepth 1 -type f \( -name 'sa[0-9][0-9]' -o -name 'sa[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]' \) -mtime -2 -readable 2>/dev/null
    done | sort -u
}

# 用 sadf 导出某一类历史指标为 CSV。
#   ★ OF-07：**保持不带 `-t`**，与 MySQL/四库归一。`sadf -d` 默认按 UTC 输出且时间戳**自带 `UTC` 后缀**
#     （实测真实包：`2026-10-09 14:32:22 UTC`），Python 可据后缀自校正；`timeseries/*.csv` 是本地时间
#     `+08:00`。两套时钟的**对齐依据是 snapshot 的 time_evidence**（本批新增），而非改 sadf 参数 ——
#     若在此处加 `-t`，Oracle 的 sar 会变成本地时间，与 MySQL 的 UTC 形态不一致，破坏四库归一。
collect_sadf_metric() {
    # 第 1 个参数（metric 名）保留：三库调用点保持同形，跨库同步补丁不必改调用点；
    #   本函数自身不需要它（B4，2026-10-16）。
    local outfile="$2"; shift 2
    local f any=0 rc=0
    : > "$outfile"
    while IFS= read -r f; do
        [ -r "$f" ] || continue
        printf '# source_file=%s\n' "$f" >> "$outfile"
        sadf -d "$f" -- "$@" >> "$outfile" 2>> "${RUNTIME[LOG_FILE]}"; rc=$?
        [ "$rc" -eq 0 ] && any=1
    done < "$SAR_FILE_LIST"
    if [ "$any" -eq 1 ] && [ -s "$outfile" ]; then return 0; fi
    return 1
}

# 计算 sar 历史覆盖区间，写 history/coverage.tsv **与 coverage.json**（OF-18）。
#   coverage.json 字段与 MySQL 完全对齐：schema_version/status/requested_hours/first_timestamp/
#   last_timestamp/coverage_seconds/coverage_hours —— 下游可直接 json.load，无需再解析 TSV。
compute_sar_coverage() {
    local src="$HISTORY_DIR/sar_cpu.csv" t e cutoff points_file first_epoch="" last_epoch="" first_ts="" last_ts=""
    local coverage_seconds=0 coverage_hours=0 cov_status="empty" point_count=0 discontinuity_count=0 max_gap_seconds=0
    [ -s "$src" ] || return 0
    # 只统计**请求窗口内**的点（CONFIG[SAR_HOURS]）：sadf 导出的是原始历史全量，
    #   旧写法拿全量首尾相减算覆盖，会把窗口外的点算进来 ⇒ 覆盖率虚高。
    cutoff=$(( $(date +%s) - CONFIG[SAR_HOURS]*3600 ))
    points_file="${RUNTIME[TMP_DIR]}/sar_coverage_epochs.txt"; : > "$points_file"
    while IFS= read -r t; do
        [ -n "$t" ] || continue
        e=$(date -d "$t" +%s 2>/dev/null) || continue
        [ "$e" -ge "$cutoff" ] && printf '%s\n' "$e" >> "$points_file"
    done < <(awk -F';' '!/^#/ && NF>=3 && $3 ~ /^[0-9][0-9][0-9][0-9]-/ {print $3}' "$src")
    sort -nu "$points_file" -o "$points_file"
    point_count=$(wc -l < "$points_file" | tr -d ' '); point_count=${point_count:-0}
    first_epoch=$(head -n 1 "$points_file" 2>/dev/null); last_epoch=$(tail -n 1 "$points_file" 2>/dev/null)
    if [ -n "$first_epoch" ] && [ -n "$last_epoch" ]; then
        # covered 只累加 <=1h 的间隔；>1h 的算一次"断点"。二者都写进 coverage.tsv，
        #   让下游能区分"覆盖够但中间断过"与"一直连续"（F-04 类判据的依据）。
        read -r coverage_seconds discontinuity_count max_gap_seconds < <(awk '
          NR==1{prev=$1; next}
          {gap=$1-prev; if(gap>maxgap)maxgap=gap; if(gap>0 && gap<=3600)covered+=gap; else if(gap>3600)breaks++; prev=$1}
          END{print covered+0,breaks+0,maxgap+0}' "$points_file")
        coverage_hours=$(awk -v s="$coverage_seconds" 'BEGIN{printf "%.2f",s/3600}')
        first_ts=$(date -u -d "@$first_epoch" '+%Y-%m-%d %H:%M:%S UTC' 2>/dev/null)
        last_ts=$(date -u -d "@$last_epoch" '+%Y-%m-%d %H:%M:%S UTC' 2>/dev/null)
        cov_status="partial"
        if [ "$coverage_seconds" -ge $((CONFIG[SAR_HOURS]*3600*9/10)) ]; then cov_status="ok"; fi
        # F-04：窗口内出现 **>1 小时的点间空洞**时，即使累计覆盖达标也不判 ok ——
        #   一台早上 3h + 晚上 3h、中间停机的主机不该被写成"历史可用"。
        #   阈值取 3600s（与上面"covered 只累加 <=1h 的间隔"同一口径），**不要**改成
        #   "窗口的 10%"：24h 窗口的 10% = 2.4h，而空洞 >2.4h 时累计覆盖必然 <21.6h(90%)
        #   ⇒ 两条判据互斥、连续性那条永远不触发，等于给自己立了一条验证不了的规则。
        if [ "$max_gap_seconds" -gt 3600 ]; then cov_status="partial"; fi
    fi
    {
      printf 'status\t%s\n' "$cov_status"
      printf 'requested_hours\t%s\n' "${CONFIG[SAR_HOURS]}"
      printf 'first_timestamp\t%s\n' "$first_ts"
      printf 'last_timestamp\t%s\n' "$last_ts"
      printf 'coverage_seconds\t%s\n' "$coverage_seconds"
      printf 'coverage_hours\t%s\n' "$coverage_hours"
      printf 'point_count\t%s\n' "$point_count"
      printf 'discontinuity_count\t%s\n' "$discontinuity_count"
      printf 'max_gap_seconds\t%s\n' "$max_gap_seconds"
    } > "$HISTORY_DIR/coverage.tsv"
    # ★ OF-18：同时写 coverage.json（字段与 MySQL 对齐，下游 json.load 即可）
    {
      printf '{"schema_version":"1.1","status":%s,"requested_hours":%s,"first_timestamp":%s,"last_timestamp":%s,"coverage_seconds":%s,"coverage_hours":%s,"point_count":%s,"discontinuity_count":%s,"max_gap_seconds":%s}\n' \
        "$(json_quote "$cov_status")" "${CONFIG[SAR_HOURS]}" "$(json_quote "$first_ts")" "$(json_quote "$last_ts")" "$coverage_seconds" "${coverage_hours:-0}" "$point_count" "$discontinuity_count" "$max_gap_seconds"
    } > "$HISTORY_DIR/coverage.json"
}

# 采集多类 sar 历史（cpu/memory/swap/load/disk/network/io/process；OF-18 已补 -w 进程类）
collect_sar_history() {
    if [ "${CONFIG[SAR_ENABLED]}" -ne 1 ]; then
        progress "SAR 历史采集已禁用"
        record_status "system.sar_history" "system.history" "skipped" "$(iso_now)" "$(iso_now)" 0 0 0 "" "disabled by config"
        return 0
    fi
    local start_iso start_ms end_iso end_ms status reason count
    start_iso=$(iso_now); start_ms=$(epoch_ms)
    # ★ N-7：清单落 RUNTIME[TMP_DIR]（**不在包树内** ⇒ 天然不进包）。
    #   原先写 `${STATUS_DIR}/sar_files.txt`：① status/ 的契约是"采集项台账"，混进一个
    #   非状态记录会污染下游遍历；② 它与 `history/source_files.txt` 内容完全重复。
    #   现在 `history/source_files.txt` 是**唯一留档**（仅在真的找到 sar 文件时写）。
    SAR_FILE_LIST="${RUNTIME[TMP_DIR]:-${RUNTIME[STATUS_DIR]}}/sar_files.txt"
    mkdir -p "$(dirname "$SAR_FILE_LIST")" 2>/dev/null
    find_sar_files > "$SAR_FILE_LIST"
    count=$(wc -l < "$SAR_FILE_LIST" | tr -d ' ')
    if ! has_cmd sadf; then
        status="unsupported"; reason="sadf/sysstat not installed"
    elif [ "${count:-0}" -eq 0 ]; then
        status="empty"; reason="no readable sar history files found"
    else
        cp "$SAR_FILE_LIST" "$HISTORY_DIR/source_files.txt"
        collect_sadf_metric cpu "$HISTORY_DIR/sar_cpu.csv" -u ALL || true
        collect_sadf_metric memory "$HISTORY_DIR/sar_memory.csv" -r || true
        collect_sadf_metric swap "$HISTORY_DIR/sar_swap.csv" -S || true
        collect_sadf_metric load "$HISTORY_DIR/sar_load.csv" -q || true
        collect_sadf_metric disk "$HISTORY_DIR/sar_disk.csv" -d -p || true
        collect_sadf_metric network "$HISTORY_DIR/sar_network.csv" -n DEV || true
        collect_sadf_metric io "$HISTORY_DIR/sar_io.csv" -b || true
        collect_sadf_metric process "$HISTORY_DIR/sar_process.csv" -w || true   # ★ OF-18：进程创建/上下文切换
        compute_sar_coverage
        local coverage_status
        coverage_status=$(awk -F'\t' '$1=="status"{print $2}' "$HISTORY_DIR/coverage.tsv" 2>/dev/null)
        # 直接映射 coverage.tsv 的判定结果：导出成功不等于有覆盖，
        # empty（读不到历史区间）必须如实记为 empty，不能默认 ok。
        case "${coverage_status:-}" in
            ok)       status="ok" ;;
            empty|"") status="empty" ;;
            *)        status="partial" ;;
        esac
        reason="raw sadf data exported; declared coverage status=${coverage_status:-unknown}"
    fi
    end_iso=$(iso_now); end_ms=$(epoch_ms)
    # ★ N-19：output_file 指向**真实存在的那个代表文件**，不再是目录 `history/`
    #   （coverage.json 是本项的结论文件；判 empty/unsupported 时它不存在 → 留空）。
    record_status "system.sar_history" "system.history" "$status" "$start_iso" "$end_iso" "$((end_ms-start_ms))" "${count:-0}" 0 \
        "$(first_existing_relpath "$HISTORY_DIR/coverage.json" "$HISTORY_DIR/source_files.txt")" "$reason"
    progress "SAR 历史采集完成: status=${status} files=${count}"
    return 0
}

# ==================== 模块 SYS-RT: 实时时序差分采样 ====================

# 读 /proc/stat 的 `cpu` 汇总行（实时 CPU 采样用）
_read_cpu_stat() { awk '/^cpu /{print $2,$3,$4,$5,$6,$7,$8,$9; exit}' /proc/stat 2>/dev/null; }

# 追加一条实时 CPU 采样点 → timeseries/system_cpu.csv
append_cpu_sample() {
    local ts="$1" elapsed_ms="$2" cur user nice system idle iowait irq softirq steal total idleall dtotal didle
    cur=$(_read_cpu_stat) || return 0
    read -r user nice system idle iowait irq softirq steal <<< "$cur"
    total=$((user+nice+system+idle+iowait+irq+softirq+steal)); idleall=$((idle+iowait))
    if [ -n "${_PREV_CPU_TOTAL:-}" ]; then
        dtotal=$((total-_PREV_CPU_TOTAL)); didle=$((idleall-_PREV_CPU_IDLE))
        awk -v ts="$ts" -v em="$elapsed_ms" -v dt="$dtotal" -v du="$((user-_PREV_CPU_USER))" -v ds="$((system-_PREV_CPU_SYSTEM))" -v dw="$((iowait-_PREV_CPU_IOWAIT))" -v dst="$((steal-_PREV_CPU_STEAL))" -v di="$didle" 'BEGIN{if(dt>0) printf "%s,%s,%.4f,%.4f,%.4f,%.4f,%.4f,%.4f\n",ts,em,du*100/dt,ds*100/dt,dw*100/dt,dst*100/dt,di*100/dt,(dt-di)*100/dt}' >> "$CPU_CSV"
    fi
    _PREV_CPU_TOTAL=$total; _PREV_CPU_IDLE=$idleall; _PREV_CPU_USER=$user; _PREV_CPU_SYSTEM=$system; _PREV_CPU_IOWAIT=$iowait; _PREV_CPU_STEAL=$steal
}

# 追加一条实时内存采样点 → timeseries/system_memory.csv
append_memory_sample() {
    local ts="$1" elapsed_ms="$2" vals mt ma st sf cached dirty writeback usedpct
    vals=$(awk '$1=="MemTotal:"{mt=$2}$1=="MemAvailable:"{ma=$2}$1=="SwapTotal:"{st=$2}$1=="SwapFree:"{sf=$2}$1=="Cached:"{ca=$2}$1=="Dirty:"{d=$2}$1=="Writeback:"{w=$2}END{print mt+0,ma+0,st+0,sf+0,ca+0,d+0,w+0}' /proc/meminfo 2>/dev/null)
    read -r mt ma st sf cached dirty writeback <<< "$vals"
    usedpct=$(awk -v t="$mt" -v a="$ma" 'BEGIN{if(t>0)printf "%.4f",(t-a)*100/t;else print 0}')
    printf '%s,%s,%s,%s,%s,%s,%s,%s,%s\n' "$ts" "$elapsed_ms" "$((mt*1024))" "$((ma*1024))" "$usedpct" "$((st*1024))" "$(((st-sf)*1024))" "$((cached*1024))" "$(((dirty+writeback)*1024))" >> "$MEM_CSV"
}

# 追加一条实时网络采样点 → timeseries/system_network.csv
append_network_sample() {
    local ts="$1" elapsed_ms="$2" delta_ms="$3" p iface rx tx drx dtx
    for p in /sys/class/net/*; do
        [ -d "$p" ] || continue; iface=${p##*/}; [ "$iface" = lo ] && continue
        rx=$(cat "$p/statistics/rx_bytes" 2>/dev/null || echo 0); tx=$(cat "$p/statistics/tx_bytes" 2>/dev/null || echo 0)
        if [ -n "${_PREV_NET_RX[$iface]+x}" ]; then
            drx=$((rx-_PREV_NET_RX[$iface])); dtx=$((tx-_PREV_NET_TX[$iface]))
            awk -v ts="$ts" -v em="$elapsed_ms" -v i="$iface" -v r="$drx" -v t="$dtx" -v dm="$delta_ms" 'BEGIN{if(dm<=0)dm=1; printf "%s,%s,%s,%.2f,%.2f,%d,%d\n",ts,em,i,r*1000/dm,t*1000/dm,r,t}' >> "$NET_CSV"
        fi
        _PREV_NET_RX[$iface]=$rx; _PREV_NET_TX[$iface]=$tx
    done
}

# 追加一条实时磁盘采样点 → timeseries/system_disk.csv
append_disk_sample() {
    local ts="$1" elapsed_ms="$2" delta_ms="$3" p dev s ri rs rt wi ws wt inflight io_ms weighted sector_size
    local dri drs drt dwi dws dwt dio dweighted
    for p in /sys/block/*; do
        # 设备过滤（三库统一）：loop/ram/zram/fd/sr 是伪设备或只读介质；`dm-*` 是 device-mapper
        #   逻辑设备（LVM / 多路径 / ASM 之上的 dm），它与底层物理盘**统计同一份 IO** ——
        #   两层都列会让报告的"磁盘设备"表出现两行描述同一个盘，util 取 max 时口径也含糊。
        #   报告按**物理盘**口径；多路径/ASM 视图由 `system.asm_multipath`（multipath -ll）单独取证。
        [ -r "$p/stat" ] || continue; dev=${p##*/}; case "$dev" in loop*|ram*|zram*|fd*|sr*|dm-*) continue;; esac
        read -r ri rs rt wi ws wt inflight io_ms weighted < <(awk '{print $1,$3,$4,$5,$7,$8,$9,$10,$11}' "$p/stat" 2>/dev/null)
        for v in "$ri" "$rs" "$rt" "$wi" "$ws" "$wt" "$inflight" "$io_ms" "$weighted"; do
            case "$v" in ''|*[!0-9]*) continue 2 ;; esac
        done
        sector_size=$(cat "$p/queue/hw_sector_size" 2>/dev/null || echo 512)
        case "$sector_size" in ''|*[!0-9]*) sector_size=512 ;; esac
        if [ -n "${_PREV_DISK_RSECT[$dev]+x}" ]; then
            dri=$((ri-_PREV_DISK_RIOS[$dev])); drs=$((rs-_PREV_DISK_RSECT[$dev])); drt=$((rt-_PREV_DISK_RTICKS[$dev]))
            dwi=$((wi-_PREV_DISK_WIOS[$dev])); dws=$((ws-_PREV_DISK_WSECT[$dev])); dwt=$((wt-_PREV_DISK_WTICKS[$dev]))
            dio=$((io_ms-_PREV_DISK_IOTICKS[$dev])); dweighted=$((weighted-_PREV_DISK_WEIGHTED[$dev]))
            awk -v ts="$ts" -v em="$elapsed_ms" -v d="$dev" -v dm="$delta_ms" -v bs="$sector_size" -v ri="$dri" -v rs="$drs" -v rt="$drt" -v wi="$dwi" -v ws="$dws" -v wt="$dwt" -v io="$dio" -v wq="$dweighted" 'BEGIN{if(dm<=0)dm=1; printf "%s,%s,%s,%.2f,%.2f,%.2f,%.2f,%.4f,%.4f,%.4f,%.4f\n",ts,em,d,rs*bs*1000/dm,ws*bs*1000/dm,ri*1000/dm,wi*1000/dm,(ri>0?rt/ri:0),(wi>0?wt/wi:0),io*100/dm,wq/dm}' >> "$DISK_CSV"
        fi
        _PREV_DISK_RIOS[$dev]=$ri; _PREV_DISK_RSECT[$dev]=$rs; _PREV_DISK_RTICKS[$dev]=$rt; _PREV_DISK_WIOS[$dev]=$wi; _PREV_DISK_WSECT[$dev]=$ws; _PREV_DISK_WTICKS[$dev]=$wt; _PREV_DISK_IOTICKS[$dev]=$io_ms; _PREV_DISK_WEIGHTED[$dev]=$weighted
    done
}

# 追加一条 Oracle v$sysstat 采样点 → timeseries/oracle_sysstat.csv。
#   ⚠ `|| return 1` 时会跳过 rm 临时文件，tmp 会被拷进包并登记进 manifest（OF-26 顺带项）。
append_oracle_sysstat_sample() {
    local ts="$1" elapsed_ms="$2" tmp
    # ★ OF-26：tmp 建在 TMP_DIR（**不是** STATUS_DIR），且**两条返回路径都删** ——
    #   否则失败路径的 tmp 会被 `cp -r STATUS_DIR` 带进包、并登记进 manifest（v1.5 实际发生过）。
    tmp="${RUNTIME[TMP_DIR]:-/tmp}/oracle_stat_${elapsed_ms}.tsv"
    if ! ora_exec > "$tmp" 2>> "${RUNTIME[LOG_FILE]}" <<'EOSQL'
SET PAGESIZE 0 FEEDBACK OFF HEADING OFF ECHO OFF TRIMSPOOL ON
WHENEVER SQLERROR CONTINUE
SELECT 'user_commits', VALUE FROM v$sysstat WHERE name='user commits'
UNION ALL SELECT 'user_rollbacks', VALUE FROM v$sysstat WHERE name='user rollbacks'
UNION ALL SELECT 'execute_count', VALUE FROM v$sysstat WHERE name='execute count'
UNION ALL SELECT 'parse_count_total', VALUE FROM v$sysstat WHERE name='parse count (total)'
UNION ALL SELECT 'parse_count_hard', VALUE FROM v$sysstat WHERE name='parse count (hard)'
UNION ALL SELECT 'physical_reads', VALUE FROM v$sysstat WHERE name='physical reads'
UNION ALL SELECT 'physical_writes', VALUE FROM v$sysstat WHERE name='physical writes'
UNION ALL SELECT 'redo_size', VALUE FROM v$sysstat WHERE name='redo size'
UNION ALL SELECT 'sorts_memory', VALUE FROM v$sysstat WHERE name='sorts (memory)'
UNION ALL SELECT 'sorts_disk', VALUE FROM v$sysstat WHERE name='sorts (disk)'
UNION ALL SELECT 'table_scans_long', VALUE FROM v$sysstat WHERE name='table scans (long tables)'
UNION ALL SELECT 'table_scan_rows', VALUE FROM v$sysstat WHERE name='table scan rows gotten'
UNION ALL SELECT 'consistent_gets', VALUE FROM v$sysstat WHERE name='consistent gets'
UNION ALL SELECT 'db_block_gets', VALUE FROM v$sysstat WHERE name='db block gets'
UNION ALL SELECT 'redo_writes', VALUE FROM v$sysstat WHERE name='redo writes'
UNION ALL SELECT 'redo_entries', VALUE FROM v$sysstat WHERE name='redo entries'
UNION ALL SELECT 'session_logical_reads', VALUE FROM v$sysstat WHERE name='session logical reads'
UNION ALL SELECT 'physical_reads_direct', VALUE FROM v$sysstat WHERE name='physical reads direct'
UNION ALL SELECT 'physical_writes_direct', VALUE FROM v$sysstat WHERE name='physical writes direct';
EXIT;
EOSQL
    then
        module_log_append "sysstat" "oracle.timeseries.sysstat" 1 "$tmp"
        rm -f "$tmp"
        return 1
    fi
    awk -v ts="$ts" -v em="$elapsed_ms" '
      BEGIN{n=split("user_commits user_rollbacks execute_count parse_count_total parse_count_hard physical_reads physical_writes redo_size sorts_memory sorts_disk table_scans_long table_scan_rows consistent_gets db_block_gets redo_writes redo_entries session_logical_reads physical_reads_direct physical_writes_direct",k," ")}
      {v[$1]=$2}
      END{printf "%s,%s",ts,em;for(i=1;i<=n;i++)printf ",%s",(k[i] in v?v[k[i]]:"");printf "\n"}' "$tmp" >> "$ORACLE_CSV"
    rm -f "$tmp"
}

# 驱动实时采样循环（SAMPLE_INTERVAL × SAMPLE_COUNT，约 30 秒）
collect_oracle_realtime_samples() {
    printf 'timestamp,elapsed_ms,user_pct,system_pct,iowait_pct,steal_pct,idle_pct,busy_pct\n' > "$CPU_CSV"
    printf 'timestamp,elapsed_ms,mem_total_bytes,mem_available_bytes,mem_used_pct,swap_total_bytes,swap_used_bytes,cached_bytes,dirty_writeback_bytes\n' > "$MEM_CSV"
    printf 'timestamp,elapsed_ms,interface,rx_bytes_per_sec,tx_bytes_per_sec,rx_bytes_delta,tx_bytes_delta\n' > "$NET_CSV"
    printf 'timestamp,elapsed_ms,device,read_bytes_per_sec,write_bytes_per_sec,read_iops,write_iops,read_await_ms,write_await_ms,util_pct,avg_queue_size\n' > "$DISK_CSV"
    printf 'timestamp,elapsed_ms,user_commits,user_rollbacks,execute_count,parse_count_total,parse_count_hard,physical_reads,physical_writes,redo_size,sorts_memory,sorts_disk,table_scans_long,table_scan_rows,consistent_gets,db_block_gets,redo_writes,redo_entries,session_logical_reads,physical_reads_direct,physical_writes_direct\n' > "$ORACLE_CSV"

    local sample_interval="${CONFIG[SAMPLE_INTERVAL]:-5}"
    local sample_count="${CONFIG[SAMPLE_COUNT]:-6}"
    local sampling_started_at sampling_started_ms sampling_finished_at sampling_finished_ms
    local start_mono prev_mono now_mono elapsed delta ts i oracle_fail=0

    # 声明全局关联数组（按接口/磁盘名索引的差值追踪）
    declare -gA _PREV_NET_RX _PREV_NET_TX
    declare -gA _PREV_DISK_RIOS _PREV_DISK_RSECT _PREV_DISK_RTICKS
    declare -gA _PREV_DISK_WIOS _PREV_DISK_WSECT _PREV_DISK_WTICKS
    declare -gA _PREV_DISK_IOTICKS _PREV_DISK_WEIGHTED

    sampling_started_at=$(iso_now); sampling_started_ms=$(epoch_ms)
    start_mono=$(monotonic_ms); prev_mono=$start_mono; ts=$(iso_now)

    # 基准采样（elapsed=0）
    append_cpu_sample "$ts" 0; append_memory_sample "$ts" 0; append_network_sample "$ts" 0 1; append_disk_sample "$ts" 0 1
    append_oracle_sysstat_sample "$ts" 0 || oracle_fail=$((oracle_fail+1))

    for ((i=1; i<=sample_count; i++)); do
        sleep "$sample_interval"
        now_mono=$(monotonic_ms); elapsed=$((now_mono-start_mono)); delta=$((now_mono-prev_mono)); ts=$(iso_now)
        append_cpu_sample "$ts" "$elapsed"
        append_memory_sample "$ts" "$elapsed"
        append_network_sample "$ts" "$elapsed" "$delta"
        append_disk_sample "$ts" "$elapsed" "$delta"
        append_oracle_sysstat_sample "$ts" "$elapsed" || oracle_fail=$((oracle_fail+1))
        prev_mono=$now_mono
        progress "实时采样 ${i}/${sample_count}"
    done

    [ "$oracle_fail" -eq 0 ] || log_warn "实时采样中 Oracle 统计读取失败 ${oracle_fail} 次"

    local oracle_points cpu_points disk_points status reason rc
    oracle_points=$(awk 'END{print (NR>0?NR-1:0)}' "$ORACLE_CSV" 2>/dev/null); oracle_points=${oracle_points:-0}
    cpu_points=$(awk 'END{print (NR>0?NR-1:0)}' "$CPU_CSV" 2>/dev/null); cpu_points=${cpu_points:-0}
    disk_points=$(awk 'END{print (NR>0?NR-1:0)}' "$DISK_CSV" 2>/dev/null); disk_points=${disk_points:-0}
    status="ok"; reason="completed ${sample_count} intervals; oracle_points=${oracle_points}; cpu_points=${cpu_points}; disk_rows=${disk_points}"; rc=0
    if [ "$oracle_points" -lt $((sample_count+1)) ] || [ "$cpu_points" -lt "$sample_count" ]; then
        status="partial"; reason="requested ${sample_count} intervals but oracle_points=${oracle_points}, cpu_points=${cpu_points}, disk_rows=${disk_points}"; rc=2
    elif [ "$oracle_fail" -gt 0 ]; then
        status="partial"; reason="sampling completed with ${oracle_fail} Oracle status read failures"; rc=2
    fi
    sampling_finished_at=$(iso_now); sampling_finished_ms=$(epoch_ms)
    # ★ N-19：同上 —— 取真实存在的代表文件（Oracle 侧时序 CSV），不是目录 `timeseries/`。
    record_status "timeseries.realtime_sampling" "timeseries" "$status" "$sampling_started_at" "$sampling_finished_at" "$((sampling_finished_ms-sampling_started_ms))" "$oracle_points" "$rc" \
        "$(first_existing_relpath "$ORACLE_CSV" "$CPU_CSV")" "$reason"
    progress "实时采样完成: status=${status} oracle_points=${oracle_points}"
    return "$rc"
}

# ==================== 模块18b: AWR 性能报告 ====================
# 生成 AWR 文本/HTML 报告（需 --diagnostics-pack 显式确认许可）
collect_awr_report() {
    # 开关判断
    #   ⚠ 注意这里的**不对称**：CONFIG[AWR_REPORT] 默认就是 1，所以「加 --awr-report」其实
    #     什么都没改变；真正决定跑不跑的是 DIAGNOSTICS_PACK（默认 0，须显式 --diagnostics-pack）。
    #     （反方向另有不对称：--no-diagnostics-pack 会顺手把 AWR_REPORT 也置 0，见 parse_args。）
    if [ "${CONFIG[AWR_REPORT]}" -ne 1 ] || [ "${CONFIG[DIAGNOSTICS_PACK]:-0}" -ne 1 ]; then
        md_line "## AWR 性能报告"
        md_line ""
        md_line "> AWR 报告已跳过。生成报告必须同时指定 --diagnostics-pack 和 --awr-report。"
        # ★ 许可未确认 ≠ 采集成功。旧版这里 `return 0` ⇒ run_module 记成 ok（实测
        #   status/oracle.module.awr_report.tsv = ok / 13ms / reason 空）⇒ 包内台账与控制台
        #   **都看不出它被跳过**，与 AGENTS.md「不得把未采集、无权限、不支持、不适用、
        #   空结果转换为 0 或正常」冲突。改为回传策略跳过码，由 run_module 记 not_enabled。
        MODULE_REASON="AWR 报告未启用：需同时指定 --diagnostics-pack 与 --awr-report（本次 DIAGNOSTICS_PACK=${CONFIG[DIAGNOSTICS_PACK]:-0}，AWR_REPORT=${CONFIG[AWR_REPORT]}）"
        return "$EXIT_POLICY_SKIP"
    fi

    md_line "## AWR 性能报告"
    md_line ""

    local awr_hours="${CONFIG[AWR_HOURS]:-1}"

    # 查找最近两个完整快照（一次查询，省掉一个 sqlplus 连接）
    local begin_snap end_snap
    local snap_data
    snap_data=$(ora_exec <<AWREOF 2>/dev/null
SET PAGESIZE 0 FEEDBACK OFF HEADING OFF ECHO OFF TRIMSPOOL ON LINESIZE 100
WHENEVER SQLERROR CONTINUE
SELECT MIN(snap_id)||','||MAX(snap_id) FROM dba_hist_snapshot
WHERE end_interval_time > SYSDATE - ${awr_hours}/24
  AND instance_number = (SELECT instance_number FROM v\$instance);
EXIT;
AWREOF
)
    snap_data=${snap_data//[$'\t\r\n ']/}
    begin_snap="${snap_data%%,*}"
    end_snap="${snap_data##*,}"
    begin_snap=${begin_snap##*:}        # 去除可能的 ERROR: 前缀，只取数字
    begin_snap=${begin_snap//[!0-9]/}
    end_snap=${end_snap##*:}
    end_snap=${end_snap//[!0-9]/}

    # 检查快照数据是否有效
    #   ★ 批 22：窗口（--awr-hours，默认 1 小时）内不足 2 个快照时**回退到最近两个可用快照**。
    #     实测起因：目标实例 AWR 快照间隔约 6 小时（19 个快照摊在 106 小时里），默认 1 小时
    #     窗口内通常只有 0 或 1 个 ⇒ 旧版这里是 `return 0`：**模块被记成 ok、且不写任何状态行**，
    #     包里既无产物也看不出原因（与 AGENTS.md「不得把未采集/不适用/空结果转换为 0 或正常」
    #     冲突，也和批 21 修的许可闸门同属一类「没采到长得像没问题」）。现在三分：
    #       ① 窗口内有 ≥2 个 → 按请求窗口出报告；
    #       ② 窗口内不足、但全库有 ≥2 个 → 回退到最近两个并**注明实际区间**（仍出报告）；
    #       ③ 全库都不足 2 个 → 走策略跳过码，模块记 not_enabled + reason（留痕，不伪装成 ok）。
    local awr_window_note=""
    if [ -z "$begin_snap" ] || [ -z "$end_snap" ] || ! [[ "$begin_snap" =~ ^[0-9]+$ ]] || ! [[ "$end_snap" =~ ^[0-9]+$ ]] || [ "$begin_snap" -ge "$end_snap" ]; then
        local fb_data fb_begin fb_end fb_bt fb_et
        fb_data=$(ora_exec <<FBEOF 2>/dev/null
SET PAGESIZE 0 FEEDBACK OFF HEADING OFF ECHO OFF TRIMSPOOL ON LINESIZE 200
WHENEVER SQLERROR CONTINUE
SELECT MIN(snap_id)||','||MAX(snap_id)||','||
       TO_CHAR(MIN(CAST(end_interval_time AS DATE)),'YYYY-MM-DD"T"HH24:MI')||','||
       TO_CHAR(MAX(CAST(end_interval_time AS DATE)),'YYYY-MM-DD"T"HH24:MI')
FROM (
  SELECT snap_id, end_interval_time FROM (
    -- awr fallback: latest two available snapshots
    SELECT snap_id, end_interval_time FROM dba_hist_snapshot
    WHERE instance_number = (SELECT instance_number FROM v\$instance)
    ORDER BY snap_id DESC
  ) WHERE ROWNUM <= 2
);
EXIT;
FBEOF
)
        fb_data=${fb_data//[$'\t\r\n ']/}
        IFS=',' read -r fb_begin fb_end fb_bt fb_et <<< "$fb_data"
        fb_begin=${fb_begin##*:}; fb_begin=${fb_begin//[!0-9]/}
        fb_end=${fb_end##*:};     fb_end=${fb_end//[!0-9]/}
        # 时间串**不能**去冒号（`##*:` 会把 03:05 砍成 05），用正则校验，不合法就留空
        [[ "$fb_bt" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}$ ]] || fb_bt=""
        [[ "$fb_et" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}$ ]] || fb_et=""

        if [ -n "$fb_begin" ] && [ -n "$fb_end" ] && [ "$fb_begin" -lt "$fb_end" ]; then
            begin_snap="$fb_begin"; end_snap="$fb_end"
            awr_window_note="窗口内（近 ${awr_hours} 小时）不足 2 个快照，已回退到最近两个可用快照（${fb_bt:-?} → ${fb_et:-?}）"
        else
            md_line "## AWR 性能报告"
            md_line ""
            md_line "> ⚠️ 本实例可用 AWR 快照不足 2 个：近 ${awr_hours} 小时窗口内不足 2 个，全库亦不足 2 个，无法生成 AWR 报告。"
            md_line "> 请确认：AWR 是否启用、快照保留期是否过短、实例是否刚启动（自动快照尚未产生第二个）。"
            MODULE_REASON="AWR 报告未生成：dba_hist_snapshot 可用快照不足 2 个（近 ${awr_hours} 小时窗口内不足 2 个，实例内亦不足 2 个）——环境条件不满足，非许可问题"
            return "$EXIT_POLICY_SKIP"
        fi
    fi

    # 获取 dbid 和 instance_number
    local dbid inst_num
    dbid=$(ora_value "dbid FROM v\$database")
    dbid=${dbid##*:}; dbid=${dbid//[!0-9]/}
    inst_num=$(ora_value "instance_number FROM v\$instance")
    inst_num=${inst_num##*:}; inst_num=${inst_num//[!0-9]/}

    # 生成 AWR HTML 报告
    #   ★ 落点必须在 **TASK_DIR 内**。旧版写 `${CONFIG[REPORT_DIR]}/…`，而 REPORT_DIR 与
    #     TASK_DIR **同级**，打包又是 `tar -C <parent> -czf … <basename TASK_DIR>`（只打 TASK_DIR）
    #     ⇒ AWR HTML **永远不进包**，只在包外留一个「无 status 登记、不进 manifest、不随包回传」
    #     的孤儿文件（实测 2.0 包 316 条目里没有任何 AWR 报告）。放到 evidence/ 就随包走。
    #     未开结构化包（--no-structured）时没有 EVIDENCE_DIR，回退 REPORT_DIR 保持原行为。
    #   ★ 批 23：DB **原生报告**（AWR，及将来的 ASH / ADDM）落 `evidence/db_reports/` ——
    #     与 evidence/ 下 25 个 <4KB 的 OS 原始输出分开存放。**顶层目录
    #     （tables/evidence/timeseries/history/logs）是四库统一契约，不动**；只在 evidence
    #     下加一层子目录，故跨库零影响（mysql/pg 采集器同构，无需同步）。
    #     ★ 文件名用**稳定短名** `awr_report.html`：instance + 时间已经写在包名里，包内再
    #     带一遍是重复；稳定名才能让「按约定去找」成立（人看 / 将来做附件都靠它）。
    #     回退到 REPORT_DIR（包外）时稳定名还顺带解决**孤儿文件堆积**（旧版带时间戳不覆盖）。
    #     ★ 契约：本目录是「人看的原件 + 随包交付物」，**分析层不解析它** —— AWR HTML 的排版
    #     跟 Oracle 版本/patchset 绑定，解析失配不会报错；结构化数据一律走 SQL 采集项
    #     （oracle.topsql.* / oracle.performance.* / oracle.session.wait_events）。
    local awr_dir awr_file
    if [ -n "${EVIDENCE_DIR:-}" ]; then
        awr_dir="${EVIDENCE_DIR}/db_reports"
        mkdir -p "$awr_dir" 2>/dev/null || awr_dir="$EVIDENCE_DIR"
    else
        awr_dir="${CONFIG[REPORT_DIR]}"
    fi
    awr_file="${awr_dir}/awr_report.html"

    local awr_start_iso awr_start_ms awr_end_iso awr_end_ms
    awr_start_iso=$(iso_now); awr_start_ms=$(epoch_ms)

    ora_exec > "$awr_file" <<AWREOF
SET PAGESIZE 0 FEEDBACK OFF HEADING OFF ECHO OFF TRIMSPOOL ON
SET LINESIZE 10000 LONG 10000000 LONGCHUNKSIZE 1000000 TRIM ON
WHENEVER SQLERROR CONTINUE
SELECT output FROM TABLE(DBMS_WORKLOAD_REPOSITORY.AWR_REPORT_HTML(
    $dbid, $inst_num, $begin_snap, $end_snap));
EXIT;
AWREOF

    awr_end_iso=$(iso_now); awr_end_ms=$(epoch_ms)

    local awr_basename awr_link out_rel=""
    awr_basename=$(basename "$awr_file")
    [ -n "${TASK_DIR:-}" ] && out_rel="$(safe_relpath "$awr_file")"
    # 报告里显示**包内相对路径**（`evidence/db_reports/awr_report.html`）而不是裸文件名 ——
    # 落点挪进子目录后再只印 basename，读者按包根找会落空。
    awr_link="${out_rel:-$awr_file}"

    # 检查报告是否成功生成（内容必须包含 HTML 标签，而不是 Oracle 错误）
    if [ -s "$awr_file" ] && grep -q '<!DOCTYPE\|^<html' "$awr_file" 2>/dev/null; then
        md_line "AWR 报告已生成: [\`${awr_link}\`](${awr_link})"
        md_line ""
        md_line "| 项目 | 值 |"
        md_line "| --- | --- |"
        if [ -n "$awr_window_note" ]; then
            md_line "| 时间范围 | 回退到最近两个可用快照（请求为近 ${awr_hours} 小时） |"
        else
            md_line "| 时间范围 | 近 ${awr_hours} 小时 |"
        fi
        md_line "| 起始快照 | ${begin_snap} |"
        md_line "| 结束快照 | ${end_snap} |"
        md_line "| DBID | ${dbid} |"
        md_line "| 实例号 | ${inst_num} |"
        md_line "| 报告文件 | ${awr_link} |"
        # ★ 批 22：走了窗口回退就必须写明「实际区间是哪个」——否则报告表头写着
        #   「近 1 小时」、快照却是 6 小时前那对，读者会以为漏采了中间的数据。
        if [ -n "$awr_window_note" ]; then
            md_line ""
            md_line "> ℹ️ ${awr_window_note}"
        fi
        # ★ 产物登记：output_file 必须是**包内真实存在的文件路径**（契约见 interface §4）；
        #   未开结构化包时无 TASK_DIR，按「本项无产物文件」写空串。
        record_status "oracle.awr.report" "sql" "ok" "$awr_start_iso" "$awr_end_iso" \
            "$((awr_end_ms-awr_start_ms))" 1 0 "$out_rel" \
            "AWR HTML 报告；快照 ${begin_snap}-${end_snap}，DBID ${dbid}，实例号 ${inst_num}${awr_window_note:+；${awr_window_note}}；DB 原生报告原件（人看，分析层不解析）"
    else
        md_line "> ⚠️ AWR 报告生成失败，请确认 Diagnostics Pack 许可"
        # ★ 生成失败必须留痕（否则「没生成」与「没采集」又长得一样）。reason 只取 ORA- 编号，
        #   不整段落盘 —— 输出里可能夹带连接串痕迹。
        local awr_diag
        awr_diag=$(grep -Eo 'ORA-[0-9]{5}[^|]{0,60}' "$awr_file" 2>/dev/null | head -2 | tr '\n' ' ')
        record_status "oracle.awr.report" "sql" "error" "$awr_start_iso" "$awr_end_iso" \
            "$((awr_end_ms-awr_start_ms))" 0 0 "" \
            "AWR HTML 未生成或内容非 HTML（DBMS_WORKLOAD_REPOSITORY.AWR_REPORT_HTML 无输出或报错）${awr_diag:+：$awr_diag}"
    fi
}

# ==================== 模块19: 控制文件与SPFILE 巡检 ====================
# 控制文件 / 参数文件（spfile/pfile）信息
collect_control_spfile() {
    md_line "## 控制文件与 SPFILE 巡检"
    md_line ""

    ora_sql_to_md "oracle.control.sn_sequence" "控制文件序列号" "
SELECT status, file_size_blks, block_size, name
FROM v\$controlfile
ORDER BY name;"

    ora_sql_to_md "oracle.control.spfile_params" "SPFILE 当前值 (关键参数)" "
SELECT sid, name, value, isspecified
FROM v\$spparameter
WHERE name IN ('spfile','*.audit_file_dest','*.control_files',
               '*.db_recovery_file_dest','*.log_archive_dest_1',
               '*.log_archive_dest_2','*.log_archive_format')
  AND value IS NOT NULL
ORDER BY sid, name;"

    ora_sql_to_md "oracle.control.recent_param_changes" "最近参数更改" "
SELECT * FROM (
SELECT name, SUBSTR(value, 1, 300) value,
       isses_modifiable, issys_modifiable,
       ismodified, isadjusted
FROM v\$parameter
WHERE ismodified != 'FALSE'
ORDER BY name
) WHERE ROWNUM <= 30;"
}

# ==================== 模块20: 故障诊断与 Top Events ====================
# 诊断 Top 段 / Top 对象等（需许可时）
collect_diag_top() {
    md_line "## 故障诊断与 Top Events"
    md_line ""

    ora_sql_to_md "oracle.topsql.logical_reads" "Top 20 SQL (按逻辑读)" "
SELECT * FROM (
  SELECT sql_id, plan_hash_value, module,
         ROUND(elapsed_time/1000000/60, 2) elapsed_min,
         ROUND(cpu_time/1000000, 2) cpu_sec,
         buffer_gets, disk_reads, sorts,
         ROW_NUMBER() OVER(ORDER BY buffer_gets DESC) rn
  FROM v\$sql
  WHERE buffer_gets > 0 AND module IS NOT NULL
) t WHERE rn <= 20;"

    ora_sql_to_md "oracle.topsql.elapsed_time" "Top 20 SQL (按执行时间)" "
SELECT * FROM (
  SELECT sql_id, plan_hash_value, module,
         ROUND(elapsed_time/1000000/60, 2) elapsed_min,
         ROUND(cpu_time/1000000, 2) cpu_sec,
         executions, buffer_gets,
         ROW_NUMBER() OVER(ORDER BY elapsed_time DESC) rn
  FROM v\$sql
  WHERE elapsed_time > 0 AND executions > 0
) t WHERE rn <= 20;"

    ora_sql_to_md "oracle.topsql.physical_reads" "Top 20 SQL (按物理读)" "
  SELECT * FROM (
  SELECT sql_id, plan_hash_value, module,
         disk_reads, buffer_gets, executions,
         ROUND(elapsed_time/1000000/60, 2) elapsed_min
  FROM v\$sql
  WHERE disk_reads > 0
  ORDER BY disk_reads DESC
  ) WHERE ROWNUM <= 20;"

    ora_sql_to_md "oracle.topsql.adr_alert_path" "ADR 预警日志路径" "
SELECT name, value
FROM v\$diag_info
WHERE name IN ('Diag Trace','Diag Alert','ADR Home','Trace')
ORDER BY name;"

    # v$diag_alert_ext 需要特定权限，添加权限检测
    ora_sql_to_md "oracle.topsql.recent_errors" "最近 SQL 错误 (近24h)" "
SELECT * FROM (
SELECT originating_timestamp, message_text
FROM v\$diag_alert_ext
WHERE originating_timestamp > SYSDATE - 1
  AND message_text LIKE '%ORA-%'
ORDER BY originating_timestamp DESC
) WHERE ROWNUM <= 30;" 2>/dev/null || md_line "> v\$diag_alert_ext 权限不足或不可用"

    # ADDM 需要显式确认 Diagnostics Pack 许可；功能可用不等同于已授权
    if [ "${CONFIG[DIAGNOSTICS_PACK]:-0}" -ne 1 ]; then
        md_line ""
        md_line "### Database Health Check (ADDM)"
        md_line ""
        md_line "> ⚠️ ADDM 需要 Oracle Diagnostics Pack 许可，当前环境未启用。"
    else
        ora_sql_to_md "oracle.topsql.addm_findings" "Database Health Check (ADDM)" "
SELECT * FROM (
SELECT task_name, finding_name, impact_type,
       ROUND(impact, 2) impact, message
FROM dba_advisor_findings
WHERE impact > 10
ORDER BY impact DESC
) WHERE ROWNUM <= 30;"
    fi

    if [ "${CONFIG[DIAGNOSTICS_PACK]:-0}" -eq 1 ]; then
      ora_sql_to_md "oracle.topsql.segment_ash" "Top Segment 访问 (ASH)" "
SELECT * FROM (
SELECT ash.current_obj#, o.object_name, o.object_type,
       COUNT(*) sample_count,
       SUM(DECODE(ash.session_state,'ON CPU',1,0)) cpu_samples,
       SUM(DECODE(ash.session_state,'WAITING',1,0)) wait_samples
FROM v\$active_session_history ash
LEFT JOIN dba_objects o ON ash.current_obj# = o.object_id AND ash.current_obj# > 0
WHERE ash.sample_time > SYSDATE - 1/24
  AND ash.sql_id IS NOT NULL
GROUP BY ash.current_obj#, o.object_name, o.object_type
ORDER BY sample_count DESC
) WHERE ROWNUM <= 20;"
    fi

    # Top 5 SQL 执行计划 (DBMS_XPLAN.DISPLAY_CURSOR)
    md_line ""
    md_line "### Top 5 SQL 执行计划 (按CPU)"
    md_block_start
    ora_exec 2>/dev/null <<'PLANEOF' >> "${RUNTIME[REPORT_FILE]}"
SET PAGESIZE 0 FEEDBACK OFF HEADING OFF ECHO OFF TRIMSPOOL ON
SET LINESIZE 200 LONG 1000000 LONGCHUNKSIZE 1000000
SET SERVEROUTPUT ON SIZE UNLIMITED VERIFY OFF
WHENEVER SQLERROR CONTINUE
DECLARE
  v_count NUMBER := 0;
BEGIN
  FOR rec IN (
    SELECT * FROM (
      SELECT sql_id, child_number, ROUND(cpu_time/1000000,2) cpu_sec, executions, sql_text
      FROM v$sql WHERE cpu_time > 0 AND executions > 0
      ORDER BY cpu_time DESC
    ) WHERE ROWNUM <= 5
  ) LOOP
    v_count := v_count + 1;
    DBMS_OUTPUT.PUT_LINE('');
    DBMS_OUTPUT.PUT_LINE('--- [' || v_count || '] SQL_ID: ' || rec.sql_id
      || '  CPU: ' || rec.cpu_sec || 's  Exec: ' || rec.executions
      || '  Text: ' || SUBSTR(rec.sql_text,1,100) || ' ---');
    FOR p IN (SELECT plan_table_output FROM TABLE(DBMS_XPLAN.DISPLAY_CURSOR(rec.sql_id, rec.child_number, 'TYPICAL'))) LOOP
      DBMS_OUTPUT.PUT_LINE(p.plan_table_output);
    END LOOP;
    DBMS_OUTPUT.PUT_LINE('');
  END LOOP;
  IF v_count = 0 THEN
    DBMS_OUTPUT.PUT_LINE('暂无高CPU SQL可获取执行计划');
  END IF;
EXCEPTION
  WHEN OTHERS THEN
    DBMS_OUTPUT.PUT_LINE('执行计划获取失败: ' || SQLERRM);
END;
/
EXIT;
PLANEOF
    md_block_end
}

# ==================== 模块22: 生产级验证 ====================
# 生产可运行性校验（关键配置与风险点）
collect_production_validation() {
    md_line "## 生产级完整性与可恢复性验证"
    md_line ""
    local dg_configured=0
    local dg_dest_count
    dg_dest_count=$(ora_try "SELECT COUNT(*) FROM v\$archive_dest WHERE target='STANDBY' AND destination IS NOT NULL;" | tr -cd '0-9')
    if [ "${ENV[IS_STANDBY]:-0}" -eq 1 ] || [ "${dg_dest_count:-0}" -gt 0 ]; then
        dg_configured=1
    fi

    ora_sql_to_md "oracle.validation.components" "数据库组件状态" "
SELECT comp_id, comp_name, version, status
FROM dba_registry
ORDER BY status, comp_id;"

    ora_sql_to_md "oracle.validation.sql_patches" "SQL 补丁应用状态" "
SELECT patch_id, patch_type, action, status,
       TO_CHAR(action_time,'YYYY-MM-DD HH24:MI:SS') action_time,
       SUBSTR(description,1,100) description
FROM dba_registry_sqlpatch
ORDER BY action_time DESC;"

    ora_sql_to_md "oracle.validation.corruption" "数据库坏块记录" "
SELECT file#, block#, blocks, corruption_change#, corruption_type
FROM v\$database_block_corruption
ORDER BY file#, block#;"

    ora_sql_to_md "oracle.validation.datafile_header" "数据文件头异常" "
SELECT file#, status, error, recover, fuzzy,
       checkpoint_change#, checkpoint_time
FROM v\$datafile_header
WHERE status NOT IN ('ONLINE','SYSTEM')
   OR error IS NOT NULL
   OR recover = 'YES'
   OR fuzzy = 'YES'
ORDER BY file#;"

    ora_sql_to_md "oracle.validation.block_checksum" "块校验与丢失写保护参数" "
SELECT name, SUBSTR(value,1,200) value, isdefault
FROM v\$parameter
WHERE name IN ('db_block_checksum','db_block_checking','db_lost_write_protect',
               'db_ultra_safe','filesystemio_options')
ORDER BY name;"

    ora_sql_to_md "oracle.validation.autoextend" "数据文件自动扩展余量" "
SELECT tablespace_name,
       COUNT(*) datafile_count,
       ROUND(SUM(bytes)/1024/1024) current_mb,
       ROUND(SUM(CASE WHEN autoextensible='YES' THEN maxbytes ELSE bytes END)/1024/1024) max_mb,
       ROUND((SUM(CASE WHEN autoextensible='YES' THEN maxbytes ELSE bytes END)-SUM(bytes))/1024/1024) headroom_mb,
       SUM(CASE WHEN autoextensible='YES' THEN 1 ELSE 0 END) autoextend_files
FROM dba_data_files
GROUP BY tablespace_name
ORDER BY headroom_mb;"

    ora_sql_to_md "oracle.validation.scheduler_failed" "Scheduler 失败作业（近7天）" "
SELECT * FROM (
SELECT owner, job_name, status, error#,
       TO_CHAR(actual_start_date,'YYYY-MM-DD HH24:MI:SS') start_time,
       run_duration, SUBSTR(additional_info,1,120) additional_info
FROM dba_scheduler_job_run_details
WHERE log_date >= SYSTIMESTAMP - INTERVAL '7' DAY
  AND status NOT IN ('SUCCEEDED','STOPPED')
ORDER BY log_date DESC
) WHERE ROWNUM <= 50;"

    ora_sql_to_md "oracle.validation.scheduler_longrun" "Scheduler 长时间运行作业" "
SELECT owner, job_name, session_id, running_instance,
       TO_CHAR(elapsed_time) elapsed_time,
       TO_CHAR(cpu_used) cpu_used
FROM dba_scheduler_running_jobs
WHERE elapsed_time > INTERVAL '1' HOUR
ORDER BY elapsed_time DESC;"

    # ★ N-4（批 5）：`V$RMAN_STATUS` **没有 `SESSION_KEY` 列**（该列属于 `V$RMAN_BACKUP_JOB_DETAILS`
    #   / `V$RMAN_OUTPUT`）；`V$RMAN_STATUS` 里表示"同一次 RMAN 会话"的一对列是
    #   `SESSION_RECID` + `SESSION_STAMP`。旧写法 100% 报 ORA-00904 → 这一项在真机上**恒失败**
    #   （实测包第一次采集即命中，被 OF-03 的 schema_mismatch 抓到）。
    ora_sql_to_md "oracle.validation.rman_validate" "最近 RMAN VALIDATE 记录" "
SELECT * FROM (
SELECT session_recid, session_stamp, operation, status,
       TO_CHAR(start_time,'YYYY-MM-DD HH24:MI:SS') start_time,
       TO_CHAR(end_time,'YYYY-MM-DD HH24:MI:SS') end_time,
       object_type
FROM v\$rman_status
WHERE UPPER(operation) LIKE '%VALIDATE%'
ORDER BY start_time DESC
) WHERE ROWNUM <= 20;"

    ora_sql_to_md "oracle.validation.backup_piece_status" "RMAN 备份片异常状态" "
SELECT status, device_type, COUNT(*) piece_count,
       MIN(completion_time) oldest_time,
       MAX(completion_time) newest_time
FROM v\$backup_piece_details
WHERE completion_time >= SYSDATE - 30
  AND status <> 'A'
GROUP BY status, device_type
ORDER BY status, device_type;"

    ora_sql_to_md "oracle.validation.controlfile_multiplex" "控制文件多路复用" "
-- ★ 分隔符**不得用竖线**：COLSEP 就是 |，值里再出现 | 会被下游当成列边界
--   （2.0 的 psv_normalize 还会把两侧空白压掉，`a | b` 直接变成 `a|b`）
--   ⇒ 第二个控制文件路径被静默丢进 csv 的 None 列。1.4 没压空白所以看不出来。
--   守门 = tools/check_oracle_psv_widths.py 的「SQL 字面量不得含竖线」。
SELECT COUNT(*) controlfile_count,
       LISTAGG(name, '; ') WITHIN GROUP (ORDER BY name) controlfile_paths
FROM v\$controlfile;"

    ora_sql_to_md "oracle.validation.redo_multiplex" "Redo 日志多路复用检查" "
SELECT l.thread#, l.group#, l.bytes/1024/1024 size_mb,
       COUNT(lf.member) member_count,
       l.status
FROM v\$log l
LEFT JOIN v\$logfile lf ON l.group#=lf.group#
GROUP BY l.thread#, l.group#, l.bytes, l.status
HAVING COUNT(lf.member) < 2
ORDER BY l.thread#, l.group#;"

    ora_sql_to_md "oracle.validation.auditing_tde" "Unified Auditing 与 TDE 状态" "
SELECT 'Unified Auditing' item,
       CASE WHEN EXISTS (SELECT 1 FROM v\$option WHERE parameter='Unified Auditing' AND value='TRUE')
            THEN 'ENABLED' ELSE 'DISABLED' END status
FROM dual
UNION ALL
SELECT 'TDE Wallet', NVL(MAX(status),'NOT_CONFIGURED')
FROM v\$encryption_wallet;"

    if [ "$dg_configured" -eq 1 ]; then
      ora_sql_to_md "oracle.validation.srl_coverage" "Standby Redo Log 配置覆盖" "
SELECT thread#,
       SUM(CASE WHEN type='ONLINE' THEN 1 ELSE 0 END) online_groups,
       SUM(CASE WHEN type='STANDBY' THEN 1 ELSE 0 END) standby_groups,
       MAX(CASE WHEN type='ONLINE' THEN bytes END)/1024/1024 online_max_mb,
       MIN(CASE WHEN type='STANDBY' THEN bytes END)/1024/1024 standby_min_mb
FROM (
  SELECT thread#, bytes, 'ONLINE' type FROM v\$log
  UNION ALL
  SELECT thread#, bytes, 'STANDBY' type FROM v\$standby_log
)
GROUP BY thread#
ORDER BY thread#;"
    fi

    md_line ""
    md_line "### OPatch 清单"
    md_block_start
    if [ -n "${ORACLE_HOME:-}" ] && [ -x "${ORACLE_HOME}/OPatch/opatch" ]; then
        "${ORACLE_HOME}/OPatch/opatch" lspatches 2>&1 | tail -n 80
    else
        printf '%s\n' 'OPatch 不可用或 ORACLE_HOME 未设置'
    fi >> "${RUNTIME[REPORT_FILE]}"
    md_block_end

    if [ "${CONFIG[RMAN_VALIDATE]:-0}" -eq 1 ]; then
        md_line ""
        md_line "### RMAN 恢复可用性验证"
        md_block_start
        if has_cmd rman && [[ "${DB_CONN[HOST]}" =~ ^(127\.0\.0\.1|localhost)$ ]] && [ -z "${DB_CONN[PASS]}" ]; then
            rman target / >> "${RUNTIME[REPORT_FILE]}" 2>> "${RUNTIME[LOG_FILE]}" <<'RMANEOF'
RESTORE DATABASE VALIDATE;
EXIT;
RMANEOF
        else
            printf '%s\n' '仅支持本地 OS 认证执行；未满足条件，跳过 RESTORE DATABASE VALIDATE。' >> "${RUNTIME[REPORT_FILE]}"
        fi
        md_block_end
    else
        md_line "> RESTORE DATABASE VALIDATE 默认不执行；维护窗口可使用 --rman-validate 显式开启。"
    fi

    md_line ""
    md_line "### Data Guard Broker 深度验证"
    md_block_start
    if has_cmd dgmgrl && [ "$dg_configured" -eq 1 ]; then
        local dg_unique_name
        dg_unique_name=$(ora_value "db_unique_name FROM v\$database")
        printf 'show configuration verbose;\nvalidate database verbose %s;\nexit;\n' "${dg_unique_name:-${ENV[DB_NAME]}}" | dgmgrl -silent / 2>&1 | tail -n 160
    else
        printf '%s\n' '未检测到 Data Guard 配置或 DGMGRL 不可用，跳过 Broker VALIDATE DATABASE VERBOSE。'
    fi >> "${RUNTIME[REPORT_FILE]}"
    md_block_end
}

# ==================== 模块21: 健康总结 ====================
# 汇总各模块结果为健康项 / 风险项
collect_health_summary() {
    md_line "## 健康检查总结"
    md_line ""
    md_line "| 检查项 | 状态 | 建议 |"
    md_line "| --- | --- | --- |"

    # 1. 归档模式
    local log_mode
    log_mode=$(ora_try "SELECT log_mode FROM v\$database;" | tr -d ' \r\n')
    if [ "$log_mode" = "ARCHIVELOG" ]; then
        md_line "| 归档模式 | ✅ ARCHIVELOG | - |"
    else
        md_line "| 归档模式 | 🔴 NOARCHIVELOG | 生产环境强烈建议开启归档模式 |"
    fi

    # 2. 表空间使用率预警
    local ts_warn
    ts_warn=$(ora_try "
SELECT COUNT(*) FROM (
  SELECT df.tablespace_name,
         ROUND((SUM(df.bytes - NVL(fs.bytes, 0)) /
                SUM(CASE WHEN df.autoextensible='YES' THEN df.maxbytes ELSE df.bytes END)) * 100, 2) pct_used
  FROM dba_data_files df
  LEFT JOIN (SELECT tablespace_name, SUM(bytes) bytes FROM dba_free_space GROUP BY tablespace_name) fs
       ON df.tablespace_name = fs.tablespace_name
  GROUP BY df.tablespace_name
  HAVING ROUND((SUM(df.bytes - NVL(fs.bytes, 0)) /
                SUM(CASE WHEN df.autoextensible='YES' THEN df.maxbytes ELSE df.bytes END)) * 100, 2) > 80
) x;" | tr -d ' \r\n')
    if [ "${ts_warn:-0}" -gt 0 ]; then
        md_line "| 表空间使用率 | ⚠️ ${ts_warn} 个表空间超80% | 请查看表空间管理模块，及时扩容 |"
    else
        md_line "| 表空间使用率 | ✅ 正常 | 所有表空间使用率 ≤ 80% |"
    fi

    # 3. 无效对象
    local inv_cnt
    inv_cnt=$(ora_try "
SELECT COUNT(*) FROM dba_objects
WHERE status != 'VALID'
  AND owner NOT IN ('SYS','SYSTEM','OUTLN','DBSNMP','XDB','ORACLE_OCM');" | tr -d ' \r\n')
    if [ "${inv_cnt:-0}" -gt 0 ]; then
        md_line "| 无效对象 | ⚠️ ${inv_cnt} 个无效对象 | 请查看对象统计，必要时重编译 |"
    else
        md_line "| 无效对象 | ✅ 无无效对象 | - |"
    fi

    # 4. 无效/不可用索引
    local inv_idx
    inv_idx=$(ora_try "
SELECT COUNT(*) FROM dba_indexes
WHERE status NOT IN ('VALID','N/A')
  AND owner NOT IN ('SYS','SYSTEM','OUTLN','DBSNMP','XDB');" | tr -d ' \r\n')
    if [ "${inv_idx:-0}" -gt 0 ]; then
        md_line "| 无效索引 | ⚠️ ${inv_idx} 个 | 请重建无效索引，避免全表扫描 |"
    else
        md_line "| 无效索引 | ✅ 正常 | - |"
    fi

    # 5. 长事务
    local long_trx
    long_trx=$(ora_try "
SELECT COUNT(*) FROM v\$transaction t
JOIN v\$session s ON t.ses_addr = s.saddr
WHERE (SYSDATE - TO_DATE(t.start_time,'MM/DD/YY HH24:MI:SS')) * 24 > 1;" | tr -d ' \r\n')
    if [ "${long_trx:-0}" -gt 0 ]; then
        md_line "| 长事务 | ⚠️ ${long_trx} 个超1小时事务 | 请排查长事务，避免 UNDO 积压 |"
    else
        md_line "| 长事务 | ✅ 正常 | 无超1小时未提交事务 |"
    fi

    # 6. 锁等待
    local lock_cnt
    lock_cnt=$(ora_try "
SELECT COUNT(*) FROM v\$lock h, v\$lock r
WHERE h.block=1 AND r.request>0 AND h.sid!=r.sid
  AND h.type!='MR' AND r.type!='MR'
  AND h.id1=r.id1 AND h.id2=r.id2 AND h.type=r.type;" | tr -d ' \r\n')
    if [ "${lock_cnt:-0}" -gt 0 ]; then
        md_line "| 锁等待 | ⚠️ 存在 ${lock_cnt} 组锁等待 | 请查看会话锁模块，及时处理阻塞 |"
    else
        md_line "| 锁等待 | ✅ 无锁等待 | - |"
    fi

    # 7. ADG 状态（有ADG配置才检查）
    local adg_cnt
    adg_cnt=$(ora_try "
SELECT COUNT(*) FROM v\$archive_dest
WHERE status='VALID' AND target='STANDBY';" | tr -d ' \r\n')
    if [ "${adg_cnt:-0}" -gt 0 ]; then
        local adg_error
        adg_error=$(ora_try "
SELECT COUNT(*) FROM v\$archive_dest
WHERE status='ERROR' AND target='STANDBY';" | tr -d ' \r\n')
        if [ "${adg_error:-0}" -gt 0 ]; then
            md_line "| ADG 同步 | 🔴 存在错误 | 请检查 ADG 模块，归档传输异常 |"
        else
            md_line "| ADG 同步 | ✅ 正常 | 备库归档传输 VALID |"
        fi
    else
        md_line "| ADG 状态 | - | 未配置 ADG 备库 |"
    fi

    # 8. ASM 状态 — CONNECTED/MOUNTED 均为正常状态
    if [ "${ENV[HAS_ASM]}" -eq 1 ]; then
        local asm_abnormal
        asm_abnormal=$(ora_try "
SELECT COUNT(*) FROM v\$asm_diskgroup
WHERE state NOT IN ('MOUNTED','CONNECTED');" | tr -d ' \r\n')
        if [ "${asm_abnormal:-0}" -gt 0 ]; then
            md_line "| ASM 磁盘组 | ⚠️ ${asm_abnormal} 个磁盘组状态异常 | 请检查 ASM 模块 |"
        else
            local asm_high_use
            asm_high_use=$(ora_try "
SELECT COUNT(*) FROM v\$asm_diskgroup
WHERE state='MOUNTED' AND total_mb > 0
  AND (total_mb-free_mb)/total_mb*100 > 80;" | tr -d ' \r\n')
            if [ "${asm_high_use:-0}" -gt 0 ]; then
                md_line "| ASM 磁盘组 | ⚠️ ${asm_high_use} 个磁盘组使用率超80% | 请扩容 ASM 磁盘 |"
            else
                md_line "| ASM 磁盘组 | ✅ 正常 | - |"
            fi
        fi
    fi

    # 9. RMAN 最近备份
    local bk_recent
    bk_recent=$(ora_try "
SELECT COUNT(*) FROM v\$rman_backup_job_details
WHERE start_time >= SYSDATE - 2
  AND status = 'COMPLETED';" | tr -d ' \r\n')
    if [ "${bk_recent:-0}" -gt 0 ]; then
        md_line "| RMAN 备份 | ✅ 近48h有成功备份 | - |"
    else
        md_line "| RMAN 备份 | ⚠️ 近48h无成功备份 | 请检查 RMAN 备份任务及日志 |"
    fi

    # 10. 数据完整性与组件
    local corrupt_cnt component_bad job_failed control_cnt
    corrupt_cnt=$(ora_try "SELECT COUNT(*) FROM v\$database_block_corruption;" | tr -cd '0-9')
    component_bad=$(ora_try "SELECT COUNT(*) FROM dba_registry WHERE status <> 'VALID';" | tr -cd '0-9')
    job_failed=$(ora_try "SELECT COUNT(*) FROM dba_scheduler_job_run_details WHERE log_date >= SYSTIMESTAMP-INTERVAL '7' DAY AND status NOT IN ('SUCCEEDED','STOPPED');" | tr -cd '0-9')
    control_cnt=$(ora_try "SELECT COUNT(*) FROM v\$controlfile;" | tr -cd '0-9')
    if [ "${corrupt_cnt:-0}" -gt 0 ]; then
        md_line "| 数据库坏块 | 🔴 ${corrupt_cnt} 条坏块记录 | 立即执行 RMAN 校验并确认恢复路径 |"
    else
        md_line "| 数据库坏块 | ✅ 未发现 | - |"
    fi
    if [ "${component_bad:-0}" -gt 0 ]; then
        md_line "| 数据库组件 | ⚠️ ${component_bad} 个组件非 VALID | 检查升级/补丁日志与 datapatch |"
    else
        md_line "| 数据库组件 | ✅ 全部 VALID | - |"
    fi
    if [ "${job_failed:-0}" -gt 0 ]; then
        md_line "| Scheduler 作业 | ⚠️ 近7天失败 ${job_failed} 次 | 检查失败作业并补跑 |"
    else
        md_line "| Scheduler 作业 | ✅ 近7天无失败 | - |"
    fi
    if [ "${control_cnt:-0}" -lt 2 ]; then
        md_line "| 控制文件冗余 | ⚠️ 仅 ${control_cnt:-0} 份 | 建议跨故障域多路复用 |"
    else
        md_line "| 控制文件冗余 | ✅ ${control_cnt} 份 | 请继续确认是否跨故障域 |"
    fi
    if [ "${CONFIG[DIAGNOSTICS_PACK]:-0}" -eq 1 ]; then
        md_line "| Diagnostics Pack | 已显式确认 | 已启用 AWR/ASH/ADDM 采集 |"
    else
        md_line "| Diagnostics Pack | 未启用 | 已按许可保护策略跳过 AWR/ASH/ADDM |"
    fi

    md_line ""
    md_line "| 环境信息 | 值 |"
    md_line "| --- | --- |"
    md_line "| DB Name | ${ENV[DB_NAME]:-N/A} |"
    md_line "| Instance | ${ENV[INSTANCE_NAME]:-N/A} |"
    md_line "| DB Role | ${ENV[DB_ROLE]:-N/A} |"
    md_line "| Open Mode | ${ENV[DB_OPEN_MODE]:-N/A} |"
    md_line "| Version | ${ENV[ORA_VERSION]:-N/A} |"
    md_line "| RAC | $([ "${ENV[IS_RAC]}" -eq 1 ] && echo "是（${ENV[RAC_NODE_COUNT]}节点）" || echo "否（单机）") |"
    md_line "| ASM | $([ "${ENV[HAS_ASM]}" -eq 1 ] && echo "是" || echo "否") |"
    md_line "| ADG | $([ "${ENV[IS_STANDBY]}" -eq 1 ] && echo "备库" || echo "主库/未配置") |"
    md_line "| CDB | $([ "${ENV[IS_CDB]}" -eq 1 ] && echo "是（PDB已采集）" || echo "否") |"
    md_line "| PDB | ${ENV[PDB_COUNT]:-0} (${ENV[PDB_LIST]:-无}) |"
    md_line ""
    md_line "> 巡检完成时间: $(date +'%Y-%m-%d %H:%M:%S')"
}

# ==================== 模块 STR: 结构化打包（仅在 --structured 模式下生效） ====================

# ==================== 输出：JSON / manifest / 打包 ====================
# JSON 字符串转义（", \, 控制字符）
json_escape() {
    local s="${1-}"
    s=${s//\\/\\\\}; s=${s//\"/\\\"}
    s=${s//$'\n'/\\n}; s=${s//$'\r'/\\r}; s=${s//$'\t'/\\t}
    printf '%s' "$s"
}

# 把值转成带引号的 JSON 字符串字面量
json_quote() { printf '"%s"' "$(json_escape "${1-}")"; }

# 文件字节数
file_size_bytes() {
    if stat -c '%s' "$1" >/dev/null 2>&1; then stat -c '%s' "$1"
    elif stat -f '%z' "$1" >/dev/null 2>&1; then stat -f '%z' "$1"
    else wc -c < "$1" | tr -d ' '
    fi
}

# 文件 SHA256（sha256sum / shasum / openssl 三选一；都无则 UNAVAILABLE）
sha256_file() {
    if has_cmd sha256sum; then sha256sum "$1" | awk '{print $1}'
    elif has_cmd shasum; then shasum -a 256 "$1" | awk '{print $1}'
    elif has_cmd openssl; then openssl dgst -sha256 "$1" | awk '{print $NF}'
    else printf 'UNAVAILABLE'
    fi
}

# 采集项状态汇总（全局，供 snapshot / collection_status / summary 三处共用，口径单一来源）
declare -gA STATUS_TALLY
# 重算 status/ 各状态计数：
#   TOTAL + 每个取值一个键（ok/empty/error/timeout/permission_denied/unsupported/
#   schema_mismatch/not_enabled/skipped/partial）。**只认 status/*.tsv**，不推断、不补 0。
refresh_status_tally() {
    STATUS_TALLY=()
    local sd="${RUNTIME[STATUS_DIR]:-}" st
    [ -n "$sd" ] || return 0
    STATUS_TALLY[TOTAL]=$(ls -1 "$sd"/*.tsv 2>/dev/null | wc -l | tr -d ' ')
    for st in ok empty error timeout permission_denied unsupported schema_mismatch not_enabled skipped partial; do
        STATUS_TALLY[$st]=$(awk -F'\t' -v s="$st" '$3==s{n++}END{print n+0}' "$sd"/*.tsv 2>/dev/null | tail -1)
    done
    return 0
}

# 把 tables/capabilities.tsv（0/1 与字符串混排）转成 JSON 对象体（**不带外层花括号**）。
#   OF-13 约定：布尔 tsv 写 0/1、json 写 true/false；字符串（oracle_home / sqlplus_version）原样。
caps_json_body() {
    local f="${TABLES_DIR}/capabilities.tsv" first=1 k v line
    [ -f "$f" ] || return 0
    while IFS= read -r line; do
        k="${line%%$'\t'*}"; v="${line#*$'\t'}"
        [ "$k" = "capability" ] && continue
        [ -n "$k" ] && [ "$k" != "$line" ] || continue
        case "$v" in
            0) v="false" ;; 1) v="true" ;; *) v="$(json_quote "$v")" ;;
        esac
        [ "$first" -eq 1 ] || printf ','
        first=0
        printf '"%s":%s' "$(json_escape "$k")" "$v"
    done < "$f"
}

# ★ 生成 snapshot.json（下游第一入口）。
#   v1.4 只有 21 个平铺字段；**OF-27** 按 MySQL 契约补齐 8 个段：
#     collector / host_identity / capabilities / time_evidence / sampling / privacy /
#     collection_summary / artifacts。
#   ⚠ 破坏性变更（schema 2.0）：`database_target_is_local` 由**顶层移入 `host_identity`**，
#     与 MySQL 的 `host_identity.database_target_is_local` 同路径（批 2 曾落在顶层）。
#     顶层 host/port/service/db_* 等 1.4 平铺字段**保持不变**，旧解析器仍可读。
generate_snapshot_json() {
    [ "${CONFIG[STRUCTURED]}" -eq 1 ] || return 0
    [ -n "$TASK_DIR" ] || return 0

    # ★ OF-07：把已采集的 time_status / timedatectl 解析成 time_evidence（字段名与 MySQL 四库归一）。
    #   动机：history/sar_*.csv 是 UTC（sadf 默认），timeseries/*.csv 是本地时间 —— 两套时钟。
    #   Python 靠本段还原/校正展示时区；缺了它两份数据画在同一 x 轴会差 8 小时（东八区）。
    local host_local_time host_utc_time host_timezone ntp_synchronized
    host_local_time=$(awk -F= '$1=="local_time"{print substr($0,index($0,"=")+1)}' "$EVIDENCE_DIR/time_status.txt" 2>/dev/null)
    host_utc_time=$(awk -F= '$1=="utc_time"{print substr($0,index($0,"=")+1)}' "$EVIDENCE_DIR/time_status.txt" 2>/dev/null)
    host_timezone=$(awk -F= '$1=="timezone"{print substr($0,index($0,"=")+1)}' "$EVIDENCE_DIR/time_status.txt" 2>/dev/null)
    # timedatectl 输出字段随 systemd 版本变：旧版 "System clock synchronized"、新版 "NTP synchronized"。
    # 两种都匹配并锚定行首，避免新系统上恒为空（MySQL F-31 踩过的坑）。
    ntp_synchronized=$(awk -F: '/^(System clock|NTP) synchronized:/{gsub(/^[ \t]+|[ \t]+$/,"",$2);print $2}' "$EVIDENCE_DIR/timedatectl.txt" 2>/dev/null)

    # OF-27：时序采样段
    local s_files="" s_sep="" f
    for f in "$CPU_CSV" "$MEM_CSV" "$NET_CSV" "$DISK_CSV" "$ORACLE_CSV"; do
        [ -f "$f" ] || continue
        s_files="${s_files}${s_sep}\"timeseries/$(basename "$f")\""; s_sep=","
    done

    refresh_status_tally
    {
        printf '{\n'
        printf '  "schema_version":"%s",\n' "$SNAPSHOT_SCHEMA_VERSION"
        printf '  "database_type":"oracle",\n'
        printf '  "collector_version":"%s",\n' "$COLLECTOR_VERSION"
        printf '  "instance_tag":%s,\n' "$(json_quote "$instance_tag")"
        printf '  "collector":{"name":"oracle_inspection.sh","version":%s,"hostname":%s,"primary_ip":%s,"collection_started_at":%s},\n' \
            "$(json_quote "$COLLECTOR_VERSION")" "$(json_quote "${_COLLECTOR_HOSTNAME:-}")" \
            "$(json_quote "${_COLLECTOR_PRIMARY_IP:-}")" "$(json_quote "${RUNTIME[START_TIME]}")"
        printf '  "host_identity":{"database_target_is_local":%s,"collector_hostname":%s,"target_resolved_ip":%s},\n' \
            "$([ "${TARGET_IS_LOCAL:-0}" -eq 1 ] && echo true || echo false)" \
            "$(json_quote "${_COLLECTOR_HOSTNAME:-}")" "$(json_quote "${TARGET_RESOLVED_IP:-}")"
        printf '  "host":%s,\n' "$(json_quote "${DB_CONN[HOST]}")"
        printf '  "port":%s,\n' "$(json_quote "${DB_CONN[PORT]}")"
        printf '  "service":%s,\n' "$(json_quote "${DB_CONN[SERVICE]}")"
        printf '  "db_name":%s,\n' "$(json_quote "${ENV[DB_NAME]:-}")"
        printf '  "instance_name":%s,\n' "$(json_quote "${ENV[INSTANCE_NAME]:-}")"
        printf '  "db_role":%s,\n' "$(json_quote "${ENV[DB_ROLE]:-}")"
        printf '  "open_mode":%s,\n' "$(json_quote "${ENV[DB_OPEN_MODE]:-}")"
        printf '  "ora_version":%s,\n' "$(json_quote "${ENV[ORA_VERSION]:-}")"
        printf '  "is_rac":%s,\n' "${ENV[IS_RAC]}"
        printf '  "rac_node_count":%s,\n' "${ENV[RAC_NODE_COUNT]}"
        printf '  "has_asm":%s,\n' "${ENV[HAS_ASM]}"
        printf '  "is_standby":%s,\n' "${ENV[IS_STANDBY]}"
        printf '  "is_cdb":%s,\n' "${ENV[IS_CDB]}"
        printf '  "pdb_count":%s,\n' "${ENV[PDB_COUNT]}"
        printf '  "pdb_list":%s,\n' "$(json_quote "${ENV[PDB_LIST]:-}")"
        printf '  "collection_started_at":%s,\n' "$(json_quote "${RUNTIME[START_TIME]}")"
        printf '  "time_evidence":{"host_local_time":%s,"host_utc_time":%s,"timezone":%s,"ntp_synchronized":%s,"timedatectl_file":"evidence/timedatectl.txt","chrony_tracking_file":"evidence/chronyc_tracking.txt"},\n' \
            "$(json_quote "$host_local_time")" "$(json_quote "$host_utc_time")" "$(json_quote "$host_timezone")" "$(json_quote "$ntp_synchronized")"
        printf '  "capabilities":{%s},\n' "$(caps_json_body)"
        printf '  "sampling":{"source":"timeseries","interval_seconds":%s,"sample_count":%s,"files":[%s]},\n' \
            "${CONFIG[SAMPLE_INTERVAL]:-0}" "${CONFIG[SAMPLE_COUNT]:-0}" "$s_files"
        printf '  "privacy":{"sql_text":"plaintext-by-design","redacted":["sqlplus_password_argv","os_credential_like_fields"],"notes":"口令不进 argv（走 stdin，OF-10）；security_scan 覆盖 password/token/PRIVATE KEY/Authorization 五类"},\n'
        printf '  "collection_summary":{"total":%s,"ok":%s,"empty":%s,"error":%s,"timeout":%s,"permission_denied":%s,"unsupported":%s,"schema_mismatch":%s,"not_enabled":%s,"skipped":%s,"partial":%s,"sql_query_count":%s,"sql_fail_count":%s},\n' \
            "${STATUS_TALLY[TOTAL]:-0}" "${STATUS_TALLY[ok]:-0}" "${STATUS_TALLY[empty]:-0}" "${STATUS_TALLY[error]:-0}" \
            "${STATUS_TALLY[timeout]:-0}" "${STATUS_TALLY[permission_denied]:-0}" "${STATUS_TALLY[unsupported]:-0}" \
            "${STATUS_TALLY[schema_mismatch]:-0}" "${STATUS_TALLY[not_enabled]:-0}" "${STATUS_TALLY[skipped]:-0}" \
            "${STATUS_TALLY[partial]:-0}" "${RUNTIME[SQL_QUERY_COUNT]}" "${RUNTIME[SQL_FAIL_COUNT]}"
        # ★ N-12 / 方案 B（批 5）：人读 md 报告落在 **DIR（包外）**，包内**没有**它 ——
        #   字段名必须把这件事说清楚，否则下游按包根解析必然 miss（与 OF-30 同类：字段指向不存在的文件）。
        #   故：① 改名 `report_md_external`（不再叫 report_md）；② 值给**采集机上的绝对路径**；
        #   ③ 另加 `report_md_scope:outside-package`。报告本身由 Python 侧从包内数据重新生成。
        printf '  "artifacts":{"snapshot":"snapshot.json","collection_status":"collection_status.json","summary":"summary.txt","manifest":"manifest.json","packaging_status":"packaging_status.json","report_md_external":%s,"report_md_scope":"outside-package","tables_dir":"tables","evidence_dir":"evidence"},\n' \
            "$(json_quote "${RUNTIME[REPORT_FILE]:-}")"
        printf '  "diagnostics_pack":%s\n' "${CONFIG[DIAGNOSTICS_PACK]}"
        printf '}\n'
    } > "$SNAPSHOT_FILE"
    progress "snapshot.json 已生成（schema ${SNAPSHOT_SCHEMA_VERSION}）"
}

# ★ OF-16 / OF-23：把状态台账**放进包内**。
#   v1.4 只有 per-item 的 status/*.tsv，汇总 `_status_summary_*.txt` 落在 filepath **不进包**
#   （`docs/cross-database-capability-matrix.md` 已登记"包内未发现同结构 collection_status.json"）。
#   现在产出两个文件，**都在 TASK_DIR**，由 manifest 登记、随 tar.gz 一起回传：
#     collection_status.json  items[] + summary{}（机器读，字段与 status/*.tsv 一一对应）
#     summary.txt             人读摘要（含"耗时最长前 10"）
generate_collection_status() {
    [ "${CONFIG[STRUCTURED]}" -eq 1 ] || return 0
    [ -n "$TASK_DIR" ] || return 0
    local sd="${RUNTIME[STATUS_DIR]}" f
    local items_file="${TASK_DIR}/.cs_items.json"
    : > "$items_file"
    local first=1 v_id v_cat v_st v_sa v_fa v_dur v_rw v_ec v_of v_rs
    for f in "$sd"/*.tsv; do
        [ -s "$f" ] || continue
        IFS=$'\t' read -r v_id v_cat v_st v_sa v_fa v_dur v_rw v_ec v_of v_rs < "$f" || continue
        [ -n "$v_id" ] || continue
        [ "$first" -eq 1 ] || printf ',' >> "$items_file"
        first=0
        printf '\n    {"item_id":%s,"category":%s,"status":%s,"started_at":%s,"finished_at":%s,"duration_ms":%s,"row_count":%s,"exit_code":%s,"output_file":%s,"reason":%s}' \
            "$(json_quote "$v_id")" "$(json_quote "$v_cat")" "$(json_quote "$v_st")" "$(json_quote "$v_sa")" \
            "$(json_quote "$v_fa")" "$(is_uint "$v_dur" && echo "$v_dur" || echo 0)" "$(json_quote "$v_rw")" \
            "$(is_uint "$v_ec" && echo "$v_ec" || echo 0)" "$(json_quote "$v_of")" "$(json_quote "$v_rs")" >> "$items_file"
    done

    refresh_status_tally
    # 分档自校验用（N-5）：10 个取值相加必须 == TOTAL
    local _tally_sum=0 _st
    for _st in ok empty error timeout permission_denied unsupported schema_mismatch not_enabled skipped partial; do
        _tally_sum=$((_tally_sum + ${STATUS_TALLY[$_st]:-0}))
    done
    unset _st
    {
        printf '{\n'
        printf '  "schema_version":"%s",\n' "$SNAPSHOT_SCHEMA_VERSION"
        printf '  "database_type":"oracle",\n'
        printf '  "collector_version":"%s",\n' "$COLLECTOR_VERSION"
        printf '  "instance_tag":%s,\n' "$(json_quote "$instance_tag")"
        printf '  "generated_at":%s,\n' "$(json_quote "$(iso_now)")"
        printf '  "summary":{"total":%s,"ok":%s,"empty":%s,"error":%s,"timeout":%s,"permission_denied":%s,"unsupported":%s,"schema_mismatch":%s,"not_enabled":%s,"skipped":%s,"partial":%s,"sql_query_count":%s,"sql_fail_count":%s},\n' \
            "${STATUS_TALLY[TOTAL]:-0}" "${STATUS_TALLY[ok]:-0}" "${STATUS_TALLY[empty]:-0}" "${STATUS_TALLY[error]:-0}" \
            "${STATUS_TALLY[timeout]:-0}" "${STATUS_TALLY[permission_denied]:-0}" "${STATUS_TALLY[unsupported]:-0}" \
            "${STATUS_TALLY[schema_mismatch]:-0}" "${STATUS_TALLY[not_enabled]:-0}" "${STATUS_TALLY[skipped]:-0}" \
            "${STATUS_TALLY[partial]:-0}" "${RUNTIME[SQL_QUERY_COUNT]}" "${RUNTIME[SQL_FAIL_COUNT]}"
        printf '  "items":[%s\n  ]\n' "$(cat "$items_file")"
        printf '}\n'
    } > "${TASK_DIR}/collection_status.json"
    rm -f "$items_file"

    {
        printf 'Oracle 巡检采集摘要（summary）\n'
        printf '====================================\n'
        printf '实例标签    : %s\n' "$instance_tag"
        printf '采集器版本  : %s（snapshot schema %s）\n' "$COLLECTOR_VERSION" "$SNAPSHOT_SCHEMA_VERSION"
        printf '采集开始    : %s\n' "${RUNTIME[START_TIME]}"
        printf '生成时间    : %s\n' "$(date +'%Y-%m-%d %H:%M:%S')"
        printf 'OS 数据归属 : %s\n' "$([ "${TARGET_IS_LOCAL:-0}" -eq 1 ] && echo "属于目标主机（本机采集）" || echo "⚠ 属于采集机 ${_COLLECTOR_HOSTNAME}，不代表目标库")"
        printf '采集项总数  : %s\n' "${STATUS_TALLY[TOTAL]:-0}"
        printf '  成功 ok           : %s\n' "${STATUS_TALLY[ok]:-0}"
        printf '  空结果 empty      : %s\n' "${STATUS_TALLY[empty]:-0}"
        printf '  失败 error        : %s\n' "${STATUS_TALLY[error]:-0}"
        printf '  超时 timeout      : %s\n' "${STATUS_TALLY[timeout]:-0}"
        printf '  权限不足          : %s\n' "${STATUS_TALLY[permission_denied]:-0}"
        printf '  环境不支持        : %s\n' "${STATUS_TALLY[unsupported]:-0}"
        printf '  脚本/版本不匹配   : %s\n' "${STATUS_TALLY[schema_mismatch]:-0}"
        printf '  未启用            : %s\n' "${STATUS_TALLY[not_enabled]:-0}"
        printf '  跳过              : %s\n' "${STATUS_TALLY[skipped]:-0}"
        printf '  部分成功 partial  : %s\n' "${STATUS_TALLY[partial]:-0}"
        # ★ N-5：分档相加必须等于总数 —— 旧版这里漏了 partial 行，于是 10 档相加比总数少 1，
        #   但报告读起来毫无破绽（缺的那行不在，谁也看不出）。现在把这笔账当场算出来。
        printf '分档合计 / 总数     : %s / %s%s\n' "$_tally_sum" "${STATUS_TALLY[TOTAL]:-0}" \
          "$([ "$_tally_sum" = "${STATUS_TALLY[TOTAL]:-0}" ] && printf '' || printf '   ← ⚠ 不一致（台账或分档口径有漏项）')"
        printf 'SQL 查询 / 失败     : %s / %s\n' "${RUNTIME[SQL_QUERY_COUNT]}" "${RUNTIME[SQL_FAIL_COUNT]}"
        printf '\n失败项明细（error/timeout/permission_denied/unsupported/schema_mismatch/not_enabled）\n'
        awk -F'\t' '$3=="error"||$3=="timeout"||$3=="permission_denied"||$3=="unsupported"||$3=="schema_mismatch"||$3=="not_enabled"{printf "  %-42s [%-16s] %s\n",$1,$3,$10}' "$sd"/*.tsv 2>/dev/null
        printf '\n空结果项（empty：查过了、确认没有 —— 与"没采到"是相反结论）\n'
        awk -F'\t' '$3=="empty"{printf "  %-42s\n",$1}' "$sd"/*.tsv 2>/dev/null
        printf '\n耗时最长前 10\n'
        # ★ N-15：`oracle.module.realtime_sampling`（模块）与 `timeseries.realtime_sampling`
        #   （时序文件）是**同一动作的两个视图**，各记一份耗时、会同时进榜。不是重复采集，
        #   无需去重；此处显式说明，避免读者误判"采了两遍"。
        awk -F'\t' '{print $6"\t"$1"\t"$3}' "$sd"/*.tsv 2>/dev/null | sort -k1,1nr | head -10 | awk -F'\t' '{printf "  %8s ms  %-42s %s\n",$1,$2,$3}'
        printf '   （注）`oracle.module.realtime_sampling` 与 `timeseries.realtime_sampling` 是\n'
        printf '        同一实时采样动作的模块视图与数据文件视图，耗时相近属正常，非重复采集。\n'
    } > "${TASK_DIR}/summary.txt"
    progress "collection_status.json / summary.txt 已生成"
}

# 生成 manifest.json：包内每个文件的 {path,size_bytes,sha256}（**无语义状态**）
#   ★ N-11：manifest **不自列** —— 它不能包含自身的 sha256（自引用）。原先只在 `find` 里
#   用 `! -name 'manifest.json'` 隐式排除，消费者数到 305/306 会以为"清单漏列了一个文件"。
#   现在把该事实**显式声明**在头部：`self_excluded` + 原因，校验脚本据此跳过即可。
generate_manifest() {
    [ "${CONFIG[STRUCTURED]}" -eq 1 ] || return 0
    [ -n "$TASK_DIR" ] || return 0
    local f rel first=1
    {
        printf '{\n  "package_version":"%s",\n  "collector_version":"%s",\n  "database_type":"oracle",\n  "instance_tag":%s,\n  "created_at":%s,\n  "self_excluded":["manifest.json"],\n  "self_excluded_reason":"manifest 无法包含自身的 sha256（自引用）；校验时请显式排除本文件",\n  "files":[\n' \
            "$PACKAGE_VERSION" "$COLLECTOR_VERSION" "$(json_quote "$instance_tag")" "$(json_quote "$(iso_now)")"
        # 列出 TASK_DIR 下的数据文件（★ N-11：manifest 自身见头部 self_excluded 声明）
        find "$TASK_DIR" -type f ! -name 'manifest.json' -print | sort | while IFS= read -r f; do
            rel=$(safe_relpath "$f")
            [ "$first" -eq 1 ] || printf ',\n'; first=0
            printf '    {"path":%s,"size_bytes":%s,"sha256":%s}' "$(json_quote "$rel")" "$(file_size_bytes "$f")" "$(json_quote "$(sha256_file "$f")")"
        done
        printf '\n  ]\n}\n'
    } > "$MANIFEST_FILE"
    progress "manifest.json 已生成"
}

# 敏感信息扫描（5 类：password=/--password/token=/PRIVATE KEY/Authorization）
security_scan() {
    [ "${CONFIG[STRUCTURED]}" -eq 1 ] || return 0
    [ -n "$TASK_DIR" ] || return 0
    # OF-30：findings 先建在 TMP_DIR（**不**在建包树内，避免扫描结果自己进包），
    #   命中时才复制到 TASK_DIR/status/ —— 否则 record_status 记的
    #   `status/security_scan_findings.txt` 在包里**根本不存在**（v1.5 实际如此）。
    local findings="${RUNTIME[TMP_DIR]:-${RUNTIME[STATUS_DIR]}}/security_scan_findings.txt"
    : > "$findings"
    grep -RInE --exclude='collection.log' --exclude='manifest.json' --exclude='security_scan_findings.txt' \
      '(password[[:space:]]*=[[:space:]]*"?[^<[:space:]";]+|--password(=|[[:space:]])[^<[:space:]|]+|BEGIN[[:space:]].*PRIVATE KEY|Authorization:[[:space:]]*(Basic|Bearer)[[:space:]]+[A-Za-z0-9._-]+)' \
      "$TASK_DIR" > "$findings" 2>/dev/null
    if [ -s "$findings" ]; then
        mkdir -p "$TASK_DIR/status" 2>/dev/null
        cp -f "$findings" "$TASK_DIR/status/security_scan_findings.txt" 2>/dev/null
        log_error "安全扫描发现 ${BASH_SOURCE:-$0} 输出中可能包含敏感信息，已阻止打包"
        record_status "package.security_scan" "package" "error" "$(iso_now)" "$(iso_now)" 0 "$(wc -l < "$findings")" 1 "status/security_scan_findings.txt" "sensitive data detected; package blocked"
        return 1
    fi
    record_status "package.security_scan" "package" "ok" "$(iso_now)" "$(iso_now)" 0 0 0 "" ""
    return 0
}

# 把产物打成 tar.gz 结构化包
create_package() {
    [ "${CONFIG[STRUCTURED]}" -eq 1 ] || return 0
    [ "${CONFIG[PACKAGE]}" -eq 1 ] || return 0
    [ -n "$TASK_DIR" ] || return 0
    local parent base start_ms end_ms rc
    parent=$(dirname "$TASK_DIR"); base=$(basename "$TASK_DIR"); PACKAGE_FILE="${parent}/${base}.tar.gz"
    start_ms=$(epoch_ms)
    tar -C "$parent" -czf "$PACKAGE_FILE" "$base" 2>/dev/null; rc=$?
    end_ms=$(epoch_ms)
    if [ "$rc" -eq 0 ]; then
        progress "回传包已生成: ${PACKAGE_FILE} (耗时 $((end_ms-start_ms))ms)"
    else
        log_error "回传包生成失败"
    fi
    return "$rc"
}

# ★ N-3（批 5）：把一条**在 `cp -r STATUS_DIR` 之后**才写出的 status 补同步进包。
#   动机：包内 `status/` 是 cp 那一刻的快照，而 `package.security_scan` 必须等整棵数据树
#   扫完才能定论 → 不补这一步，它就只存在于操作员副本里，包内 `status/`、
#   `collection_status.json.items[]`、`summary{}` 三处**同时少一项**（旧版即如此）。
#   ⚠ 只适用于"定论时间早于封包"的项；由归档动作本身产生的结论见 collect_structured_package 的不变量。
sync_status_to_package() {
    local item_id="$1" src
    [ "${CONFIG[STRUCTURED]}" -eq 1 ] || return 0
    [ -n "${TASK_DIR:-}" ] || return 0
    src="${RUNTIME[STATUS_DIR]}/$(sanitize_filename "$item_id").tsv"
    [ -s "$src" ] || return 0
    mkdir -p "$TASK_DIR/status" 2>/dev/null
    cp -f "$src" "$TASK_DIR/status/" 2>/dev/null
    return 0
}

# 归档前自检结论落包（N-3）：让包**自描述自己的封口状态** —— 拿到包的人不必回头找操作员副本，
#   就知道"这一包有没有通过打包前的安全扫描、归档文件叫什么"。
#   ⚠ tar 本身的成败**写不进来**（本文件在 tar 之前生成）—— 见 collect_structured_package 的不变量。
write_packaging_status() {
    [ "${CONFIG[STRUCTURED]}" -eq 1 ] || return 0
    [ -n "${TASK_DIR:-}" ] || return 0
    local scan_ok="$1" scan_json="blocked"
    [ "$scan_ok" -eq 1 ] && scan_json="ok"
    {
        printf '{\n'
        printf '  "schema_version":%s,\n' "$(json_quote "$SNAPSHOT_SCHEMA_VERSION")"
        printf '  "collector_version":%s,\n' "$(json_quote "$COLLECTOR_VERSION")"
        printf '  "phase":"pre-archive",\n'
        printf '  "created_at":%s,\n' "$(json_quote "$(iso_now)")"
        printf '  "security_scan":%s,\n' "$(json_quote "$scan_json")"
        printf '  "archive_planned":%s,\n' "$(json_quote "$(basename "$TASK_DIR").tar.gz")"
        printf '  "note":%s\n' "$(json_quote "本文件在归档写入之前生成，只反映归档前的自检结论。归档本身的成败（tar 返回码 / 退出码 30=安全扫描拦截、31=tar 失败）记在操作员副本 status/oracle.module.structured_packaging.tsv —— 归档的结论晚于包的封口时间，结构上无法装进它自己封装的归档。")"
        printf '}\n'
    } > "${TASK_DIR}/packaging_status.json"
    return 0
}

# ★ 收口：把 timeseries/ history/ status/ 拷进 TASK_DIR，生成包内台账与快照，然后打包。
#   顺序（每一步都被后一步依赖，不能换）：
#     ① 拷 timeseries/history/status
#     ② security_scan → **同步进包**（★ 批 5 / N-3：扫描只能在 cp 之后做，所以必须显式补拷，
#        否则包内 status/、collection_status.json 永远少它一项）
#     ③ collection_status.json + summary.txt（OF-16/23，在 ② 之后 → items[] 含 package.security_scan）
#     ④ snapshot.json（OF-27）—— ★ 批 7 / N-17：挪到 ③ **之后**，与台账同一时点取样，
#        否则 `collection_summary.total` 会比 `items[]` 少 1（旧版实测 158 vs 159）
#     ⑤ packaging_status.json  ⑥ manifest.json（列全 TASK_DIR）  ⑦ tar.gz
#   ⚠ **不变量**：`oracle.module.structured_packaging` 由归档动作**本身**产生（tar 的成败 /
#     退出码 30/31），按定义晚于包的封口时间 ⇒ 它不可能出现在它自己封装的包里。它只存在于操作员
#     副本 `status/`、终态退出码与操作员摘要里。**这不是缺陷**：任何"包是否封成功"的结论都晚于封口。
#     （旧注释把这条写反了——说它"随包回传"，而实际它写在 cp 之后。）
#   ⚠ ★ 批 7 / N-16：本函数**不再自记 status** —— 旧版这里又记了一条 `module.structured_packaging`
#     （不合 `oracle.module.*` 约定），与 `run_module` 记的合规条目**重复**（同一动作两条记录）。
#     现在 `run_module` 是唯一记账者，本函数只 `return` 真实 rc（30/31）+ 回传 `MODULE_REASON`。
#     包的完整性口径因此回到"包内 = 总数 − 1"（只差归档自己的那一条）。
collect_structured_package() {
    [ "${CONFIG[STRUCTURED]}" -eq 1 ] || return 0
    local scan_ok=1

    # 将时序和历史数据复制到 TASK_DIR
    [ -d "$TIMESERIES_DIR" ] && cp -r "$TIMESERIES_DIR" "$TASK_DIR/timeseries" 2>/dev/null
    [ -d "$HISTORY_DIR" ] && cp -r "$HISTORY_DIR" "$TASK_DIR/history" 2>/dev/null
    # ora_value 的成败标志文件是内部状态，不进包（OF-20）
    rm -f "$(ora_value_flag_file)" 2>/dev/null
    [ -d "${RUNTIME[STATUS_DIR]}" ] && cp -r "${RUNTIME[STATUS_DIR]}" "$TASK_DIR/status" 2>/dev/null

    # 打包前安全扫描（★ OF-05：被拦 / tar 失败都必须如实登记，原先 status 恒为 "ok"）
    security_scan || scan_ok=0
    sync_status_to_package "package.security_scan"

    generate_collection_status
    # ★ N-17（批 7）：snapshot 从"security_scan 之前"挪到**台账之后**。旧顺序下 snapshot 先算
    #   （那时 `package.security_scan` 这条 status 还没生成）→ `collection_summary.total` 比
    #   `collection_status.json.summary.total` 少 1（实测 158 vs 159）。"同一个函数"不等于
    #   "同一个时点"：`refresh_status_tally()` 是同一处算式，但取样时刻不同、观察集合就不同。
    #   挪到此处后 snapshot / collection_status / 尾部控制台**三处同时点同口径**（真正同源）。
    generate_snapshot_json
    write_packaging_status "$scan_ok"
    generate_manifest

    local status="ok" reason="structured package completed" rc=0
    if [ "$scan_ok" -eq 1 ]; then
        if create_package; then
            if [ -n "${PACKAGE_FILE:-}" ] && [ -f "$PACKAGE_FILE" ]; then
                reason="package created: $(basename "$PACKAGE_FILE")"
            else
                reason="structured package completed (no package: --no-package)"
            fi
        else
            status="error"; rc=31; reason="tar failed; task dir available"
            log_error "回传包生成失败，任务目录仍可用: ${TASK_DIR}"
        fi
    else
        status="error"; rc=30; reason="sensitive data detected; package blocked"
        log_error "打包前安全扫描未通过，已跳过 tar.gz 打包。请检查 TASK_DIR 中是否有敏感信息。"
    fi
    FINAL_EXIT_CODE=$rc   # OF-17：把打包结果带到终态退出码

    # ★ N-16（批 7）：本函数**不再自记 status**。旧版这里又记了一条 `module.structured_packaging`
    #   （不合 `oracle.module.*` 约定），而 `run_module` 早已用合规 id `oracle.module.structured_packaging`
    #   记过一条（且因 `func_name` 恒返回 0 而**恒 ok**）。同一动作两条记录 ⇒ ① 控制台
    #   "包内台账 = 总数 − 2"（文案却写 − 1）；② tar 失败时两条结论**相反**；③ 多出一个不合规 id。
    #   现在改为：**`run_module` 是唯一记账者**，这里只把真实 rc 与精确 reason 回传给它。
    MODULE_REASON="$reason"
    return "$rc"
}

# ==================== 入口: 参数输入 ====================
parse_args "$@"

[[ -z "${DB_CONN[HOST]}" ]] && DB_CONN[HOST]="127.0.0.1"
[[ -z "${DB_CONN[PORT]}" ]] && DB_CONN[PORT]="1521"
[[ -z "${DB_CONN[SERVICE]}" ]] && DB_CONN[SERVICE]="orcl"
[[ -z "${DB_CONN[USER]}" ]] && DB_CONN[USER]="sys"

# 如果密码为空，先判断是否能本地OS认证，不能才提示输入
if [ -z "${DB_CONN[PASS]}" ]; then
    # 检测本地OS认证条件：127.0.0.1/localhost + oracle用户或ORACLE_HOME已设置
    _os_auth=0
    case "${DB_CONN[HOST]}" in
        127.0.0.1|localhost|"") _os_auth=1 ;;
    esac
    if [[ "$_os_auth" -eq 1 ]] && { [[ -n "$ORACLE_HOME" ]] || [[ "$(id -un)" == "oracle" ]]; }; then
        # 满足本地OS认证条件，留空密码，get_ora_conn 会自动用 / as sysdba
        :
    else
        printf '密码: '
        _pass=""
        if [ -t 0 ]; then
            stty -echo 2>/dev/null
            read -r _pass
            stty echo 2>/dev/null
            printf "\n"
        else
            read -r _pass
        fi
        DB_CONN[PASS]="$_pass"
    fi
fi

# ==================== 查找 sqlplus 客户端 ====================
if has_cmd sqlplus; then
    DB_CONN[SQLPLUS_BIN]="$(command -v sqlplus)"
elif [ -n "$ORACLE_HOME" ] && [ -x "${ORACLE_HOME}/bin/sqlplus" ]; then
    DB_CONN[SQLPLUS_BIN]="${ORACLE_HOME}/bin/sqlplus"
else
    printf "ERROR: 未找到 sqlplus，请确认 ORACLE_HOME 已设置并加入 PATH\n"
    printf "可手动指定 sqlplus 路径: "
    read -r sqlplus_path
    if [ ! -x "$sqlplus_path" ]; then
        printf "ERROR: %s 不可执行，退出\n" "$sqlplus_path"
        exit 10   # OF-17：环境准备失败
    fi
    DB_CONN[SQLPLUS_BIN]="$sqlplus_path"
fi

# 主机标识（仿 MySQL：localhost 时解析真实 IP）★ OF-06
#   TARGET_RESOLVED_IP / TARGET_IS_LOCAL 是「OS 数据归属」的唯一依据：TARGET_IS_LOCAL=0 时，
#   本机采到的 system.* / timeseries.* 属于**采集机**而非目标库，下游不得据此评价目标库资源。
_COLLECTOR_HOSTNAME=$(hostname -s 2>/dev/null || hostname 2>/dev/null || echo "unknown")
_COLLECTOR_PRIMARY_IP=$(detect_primary_ipv4)
TARGET_RESOLVED_IP=$(resolve_ipv4 "${DB_CONN[HOST]}")
TARGET_IS_LOCAL=0; is_local_connect_target "${DB_CONN[HOST]}" "$TARGET_RESOLVED_IP" && TARGET_IS_LOCAL=1
case "${DB_CONN[HOST]}" in
    127.*|localhost|::1) _INSTANCE_ADDRESS="$_COLLECTOR_PRIMARY_IP" ;;
    *) _INSTANCE_ADDRESS="${TARGET_RESOLVED_IP:-${DB_CONN[HOST]}}" ;;
esac
_ts_tag=$(sanitize_filename "${_COLLECTOR_HOSTNAME}_${_INSTANCE_ADDRESS}_${DB_CONN[PORT]}")

# ==================== 初始化日志路径 ====================
qctime=$(date +'%Y-%m-%d_%H-%M-%S')
filepath="${CONFIG[REPORT_DIR]:-/var/tmp}"
# ★ OF-14：输出目录可写性检查（退出码 10），实现见 ensure_output_dir
ensure_output_dir "$filepath" || exit 10
RUNTIME[START_TIME]="$qctime"
RUNTIME[START_EPOCH]=$(date +%s)
RUNTIME[LOG_FILE]="${filepath}/${CONFIG[REPORT_PREFIX]}_${_ts_tag}_${qctime}.log"
RUNTIME[STATUS_DIR]="${filepath}/${CONFIG[REPORT_PREFIX]}_status_${_ts_tag}_${qctime}"
RUNTIME[COLLECTION_STARTED_MS]=$(epoch_ms)
mkdir -p "${RUNTIME[STATUS_DIR]}" || { printf "ERROR: 无法创建状态目录\n" >&2; exit 10; }   # OF-17

# 时序采样和历史数据目录
TIMESERIES_DIR="${filepath}/${CONFIG[REPORT_PREFIX]}_timeseries_${_ts_tag}_${qctime}"
HISTORY_DIR="${filepath}/${CONFIG[REPORT_PREFIX]}_history_${_ts_tag}_${qctime}"
mkdir -p "$TIMESERIES_DIR" "$HISTORY_DIR"

# ★ 临时工作目录（OF-26）：**刻意放在 TASK_DIR / STATUS_DIR 之外**。
#   动机：v1.5 的 `append_oracle_sysstat_sample` 在 `|| return 1` 时跳过了 `rm -f "$tmp"`，
#   而它的 tmp 建在 STATUS_DIR 里 → 整个 STATUS_DIR 被 `cp -r` 进包 → 包里凭空多出 N 个
#   `oracle_stat_<elapsed_ms>.tsv` 且被登记进 manifest.json。放到这里就"漏不进包"了。
#   用点号前缀（`.oracle_inspection_tmp_…`），不与报告/包同名匹配。正常结束与中断都会删。
RUNTIME[TMP_DIR]="${filepath}/.${CONFIG[REPORT_PREFIX]}_tmp_${_ts_tag}_${qctime}"
mkdir -p "${RUNTIME[TMP_DIR]}" 2>/dev/null

# 时序 CSV 输出路径
CPU_CSV="${TIMESERIES_DIR}/system_cpu.csv"
MEM_CSV="${TIMESERIES_DIR}/system_memory.csv"
NET_CSV="${TIMESERIES_DIR}/system_network.csv"
DISK_CSV="${TIMESERIES_DIR}/system_disk.csv"
ORACLE_CSV="${TIMESERIES_DIR}/oracle_sysstat.csv"
printf "Oracle Inspection Start: %s\n" "$(date)" >> "${RUNTIME[LOG_FILE]}"

# ★ OF-25：模块清单**单一来源**（`module_id|中文名|函数|执行方式`）。
#   dry-run 预览与真实执行**共用同一份**，从根上消除"预览列了 26 个、实际跑 27 个"这类漂移
#   （v1.4 的 dry-run 就漏了 `采集系统静态信息`，且顺序与主流程不一致）。
#   执行方式：run = 同步执行；bg = 后台启动（结果在 wait_background_modules 处收口）。
#   ⚠ 顺序有意义：sar/实时 两个 bg 模块要在长耗时的 DB 模块**之前**启动，才能真正并行。
MODULE_PLAN=(
    "oracle.module.awr_report|生成AWR性能报告|collect_awr_report|run"
    "oracle.module.sys_info|采集系统信息|collect_sys_info|run"
    "oracle.module.system_static|采集系统静态信息|collect_system_static|run"
    "oracle.module.capability_probe|采集能力探测|collect_capabilities|run"
    "oracle.module.sar_history|采集SAR历史|collect_sar_history|bg"
    "oracle.module.realtime_sampling|实时时序采样|collect_oracle_realtime_samples|bg"
    "oracle.module.basic|采集Oracle基础信息|collect_oracle_info|run"
    "oracle.module.pdb|采集PDB容器信息|collect_pdb|run"
    "oracle.module.tablespace|采集表空间信息|collect_tablespace|run"
    "oracle.module.asm|采集ASM磁盘组|collect_asm|run"
    "oracle.module.archive|采集归档日志|collect_archive|run"
    "oracle.module.session_lock|采集会话与锁|collect_session_lock|run"
    "oracle.module.performance|采集性能诊断|collect_performance|run"
    "oracle.module.index|采集索引信息|collect_index|run"
    "oracle.module.adg|采集ADG状态|collect_adg|run"
    "oracle.module.dg_broker|采集DG Broker|collect_dg_broker|run"
    "oracle.module.flashback|采集闪回数据库|collect_flashback|run"
    "oracle.module.rac|采集RAC专项|collect_rac|run"
    "oracle.module.rac_ha|采集RAC高可用|collect_rac_ha|run"
    "oracle.module.rman|采集RMAN备份信息|collect_rman|run"
    "oracle.module.control_spfile|采集控制文件与SPFILE|collect_control_spfile|run"
    "oracle.module.audit_security|采集审计与安全|collect_audit_security|run"
    "oracle.module.awr_stats|采集AWR与统计信息|collect_awr_stats|run"
    "oracle.module.diag_top|采集故障诊断|collect_diag_top|run"
    "oracle.module.production_validation|生产级完整性验证|collect_production_validation|run"
    "oracle.module.alert_log|扫描告警日志|collect_alert_log|run"
    "oracle.module.health_summary|健康检查总结|collect_health_summary|run"
    "oracle.module.structured_packaging|结构化打包|collect_structured_package|run"
)

# Dry-run 模式预览（与主流程共用 MODULE_PLAN，不再单独维护列表）
if [[ "${CONFIG[DRY_RUN]}" -eq 1 ]]; then
    printf "========================================\n"
    printf " Oracle 巡检 Dry-Run 模式\n"
    printf " collector=%s  schema=%s\n" "$COLLECTOR_VERSION" "$SNAPSHOT_SCHEMA_VERSION"
    printf " 将执行的模块 (%d 个):\n" "${#MODULE_PLAN[@]}"
    for _e in "${MODULE_PLAN[@]}"; do
        IFS='|' read -r _mid _mname _mfunc _mmode <<< "$_e"
        printf "  [%-3s] %-24s %s\n" "$_mmode" "$_mname" "$_mid"
    done
    printf "========================================\n"
    printf " 巡检预览完成，实际未执行任何采集\n"
    exit 0
fi

# ==================== 连接测试 ====================
CONN_TEST_ERR=$(ora_exec <<'EOF' 2>&1
SET PAGESIZE 0
SET FEEDBACK OFF
SET HEADING OFF
SELECT 'CONNECTED' FROM DUAL;
EXIT;
EOF
)
if ! printf '%s' "$CONN_TEST_ERR" | grep -q "CONNECTED"; then
    printf "ERROR: Oracle 连接失败，请检查主机/端口/服务名/用户名/密码\n"
    printf "连接串: %s/***@//%s:%s/%s\n" "${DB_CONN[USER]}" "${DB_CONN[HOST]}" "${DB_CONN[PORT]}" "${DB_CONN[SERVICE]}"
    # 脱敏错误详情（替换密码为 ***）
    safe_err=$(printf '%s' "$CONN_TEST_ERR" | sed -E 's/([a-zA-Z0-9._]+)\/([^@]+)@/\1\/***@/g')
    printf "错误详情:\n%s\n" "$safe_err"
    printf "日志: %s\n" "${RUNTIME[LOG_FILE]}"
    exit 20   # OF-17：Oracle 连接失败
fi

# 磁盘空间预检
check_output_space || { printf "ERROR: 磁盘空间不足，无法继续\n" >&2; exit 10; }   # OF-17

# ==================== 环境检测 ====================
detect_env

# ==================== 生成文件名 ====================
instance_tag="$(sanitize_filename "${ENV[DB_NAME]:-oracle}_${_COLLECTOR_HOSTNAME}_${_INSTANCE_ADDRESS}_${DB_CONN[PORT]}_${ENV[INSTANCE_NAME]:-inst}")"
RUNTIME[REPORT_FILE]="${filepath}/${CONFIG[REPORT_PREFIX]}_${instance_tag}_${qctime}.md"

# 结构化任务目录（复用报告命名，与 MySQL 风格一致）
TASK_DIR=""; TABLES_DIR=""; EVIDENCE_DIR=""; LOGS_DIR=""
if [ "${CONFIG[STRUCTURED]}" -eq 1 ]; then
    TASK_DIR="${filepath}/${CONFIG[REPORT_PREFIX]}_task_${instance_tag}_${qctime}"
    TABLES_DIR="${TASK_DIR}/tables"
    EVIDENCE_DIR="${TASK_DIR}/evidence"
    # OF-26：包内模块日志目录（完整错误原文），与 MySQL 的 logs/modules 对齐
    LOGS_DIR="${TASK_DIR}/logs"
    MODULE_LOG_DIR="${LOGS_DIR}/modules"
    MANIFEST_FILE="${TASK_DIR}/manifest.json"
    SNAPSHOT_FILE="${TASK_DIR}/snapshot.json"
    mkdir -p "$TABLES_DIR" "$EVIDENCE_DIR" "$MODULE_LOG_DIR"
    progress "结构化输出启用: ${TASK_DIR}"
fi

# ==================== 报告头 ====================
{
printf "# Oracle 巡检报告\n\n"
printf "| 项目 | 值 |\n"
printf "| --- | --- |\n"
printf "| 生成时间 | %s |\n" "$(date +'%Y-%m-%d %H:%M:%S')"
printf "| 目标地址 | %s:%s |\n" "${DB_CONN[HOST]}" "${DB_CONN[PORT]}"
printf "| 服务名   | %s |\n" "${DB_CONN[SERVICE]}"
printf "| 采集主机 | %s（主IP %s）|\n" "$_COLLECTOR_HOSTNAME" "$_COLLECTOR_PRIMARY_IP"
printf "| OS数据归属 | %s |\n" "$([ "$TARGET_IS_LOCAL" -eq 1 ] && echo "属于目标主机（本机采集）" || echo "⚠ 属于采集主机 ${_COLLECTOR_HOSTNAME}，不代表目标库")"
printf "| DB Name  | %s |\n" "${ENV[DB_NAME]:-N/A}"
printf "| 实例名   | %s |\n" "${ENV[INSTANCE_NAME]:-N/A}"
printf "| 数据库角色 | %s |\n" "${ENV[DB_ROLE]:-N/A}"
printf "| 打开模式 | %s |\n" "${ENV[DB_OPEN_MODE]:-N/A}"
printf "| Oracle版本 | %s |\n" "${ENV[ORA_VERSION]:-N/A}"
printf "| 是否RAC  | %s |\n" "$([ "${ENV[IS_RAC]}" -eq 1 ] && echo "是（${ENV[RAC_NODE_COUNT]}节点）" || echo "否")"
printf "| 是否ASM  | %s |\n" "$([ "${ENV[HAS_ASM]}" -eq 1 ] && echo "是" || echo "否")"
printf "| 是否备库 | %s |\n" "$([ "${ENV[IS_STANDBY]}" -eq 1 ] && echo "是（ADG Standby）" || echo "否")"
printf "| 是否CDB  | %s |\n" "$([ "${ENV[IS_CDB]}" -eq 1 ] && echo "是（容器数据库）" || echo "否")"
printf "| PDB数量  | %s |\n" "${ENV[PDB_COUNT]:-0}"
printf "\n"
} >> "${RUNTIME[REPORT_FILE]}"

printf "========================================\n"
printf " Oracle 巡检开始: %s\n" "$(date +'%Y-%m-%d %H:%M:%S')"
printf " 实例: %s:%s/%s  DB=%s  Instance=%s\n" "${DB_CONN[HOST]}" "${DB_CONN[PORT]}" "${DB_CONN[SERVICE]}" "${ENV[DB_NAME]:-?}" "${ENV[INSTANCE_NAME]:-?}"
printf " RAC=%s  ASM=%s  Standby=%s  CDB=%s  PDB=%s\n" "${ENV[IS_RAC]}" "${ENV[HAS_ASM]}" "${ENV[IS_STANDBY]}" "${ENV[IS_CDB]}" "${ENV[PDB_COUNT]}"
printf " 报告: %s\n" "${RUNTIME[REPORT_FILE]}"
printf " 日志: %s\n" "${RUNTIME[LOG_FILE]}"
# ★ OF-06：非本机采集时**无条件**在终端披露（log_warn 只在 VERBOSE 下可见，不够）
if [ "$TARGET_IS_LOCAL" -ne 1 ]; then
    printf " ⚠ 连接目标 %s 不是本机：OS 数据（内存/磁盘/IO/大页/进程）属于采集机 %s，不代表数据库服务器\n" "${DB_CONN[HOST]}" "$_COLLECTOR_HOSTNAME"
    log_warn "连接目标 ${DB_CONN[HOST]} 不是本机；OS 数据属于采集机 ${_COLLECTOR_HOSTNAME}，不代表数据库服务器"
fi
# ★ 许可开关**不对称**，必须在开场说清（批 21）：CONFIG[AWR_REPORT] 默认就是 1，所以
#   「打开了 AWR 报告开关」这个动作往往等于什么都没做，真正卡住的是 DIAGNOSTICS_PACK（默认 0）。
#   旧版这种情形一路静默跑到收尾，操作员只在 summary 里看到一个被记成 ok 的模块，无从察觉。
if [ "${CONFIG[AWR_REPORT]}" -eq 1 ] && [ "${CONFIG[DIAGNOSTICS_PACK]:-0}" -ne 1 ]; then
    printf " ⚠ AWR_REPORT=1 但 DIAGNOSTICS_PACK=0：本次**不会**生成 AWR 报告（已按许可保护策略跳过）\n"
    printf "   需要 AWR 报告请追加 --diagnostics-pack（须先确认已获 Diagnostics Pack 许可）。\n"
    log_warn "AWR_REPORT=1 但 DIAGNOSTICS_PACK=0：AWR 报告将被跳过（加 --diagnostics-pack 才会生成）"
fi
printf "========================================\n"

# ==================== 执行各模块 ====================
# ★ 按 MODULE_PLAN 单一来源依次执行（dry-run 预览用的是同一份清单，见上方 OF-25）。
#   run = 同步；bg = 后台（SAR 历史 + 实时时序独立于 Oracle 主流程，历史约 30s）。
for _e in "${MODULE_PLAN[@]}"; do
    IFS='|' read -r _mid _mname _mfunc _mmode <<< "$_e"
    # 健康检查总结之后才允许收口后台模块（否则总结会在 SAR/时序 落盘前生成）
    if [ "$_mid" = "oracle.module.health_summary" ]; then
        wait_background_modules
    fi
    if [ "$_mmode" = "bg" ]; then
        start_module_bg "$_mid" "$_mname" "$_mfunc"
    else
        run_module "$_mid" "$_mname" "$_mfunc"
    fi
done
unset _e _mid _mname _mfunc _mmode

# ==================== 完成 ====================
# 计算总耗时
_start_ep=${RUNTIME[START_EPOCH]:-0}
_end_ep=$(date +%s)
_total_sec=$((_end_ep - _start_ep))
_total_min=$((_total_sec / 60))
_total_sec_rem=$((_total_sec % 60))

# 汇总采集状态（★ N-5 / 批 5：**口径单一来源** —— 复用 refresh_status_tally()，与包内
#   collection_status.json / snapshot.collection_summary 完全同源。旧版这里另写一遍 awk，
#   结果是：① 漏掉 `partial` ⇒ 各档相加 ≠ 总数；② `错误` 桶把 schema_mismatch 重复计入、
#   `跳过/未启用` 又把 not_enabled 重复计入 ⇒ 「包 156 / 控制台 159」这种差额没人说得清。）
#   11 个分桶 = ok / empty / partial / skipped + 6 个失败态（见 is_failure_status），互为补集、不重不漏。
refresh_status_tally
_total_items="${STATUS_TALLY[TOTAL]:-0}"
_ok_count="${STATUS_TALLY[ok]:-0}"
_empty_count="${STATUS_TALLY[empty]:-0}"
_partial_count="${STATUS_TALLY[partial]:-0}"
_skip_count="${STATUS_TALLY[skipped]:-0}"
_err_count=0
for _st in error timeout permission_denied unsupported schema_mismatch not_enabled; do
    is_failure_status "$_st" && _err_count=$((_err_count + ${STATUS_TALLY[$_st]:-0}))
done
unset _st
_schema_mismatch_count="${STATUS_TALLY[schema_mismatch]:-0}"
_not_enabled_count="${STATUS_TALLY[not_enabled]:-0}"
_bucket_sum=$((_ok_count + _empty_count + _partial_count + _skip_count + _err_count))
# 包内台账（collection_status.json.items[]）比操作员副本少恰好 **1** 项 ——
# `oracle.module.structured_packaging` 由归档动作本身产生，晚于封包（不变量，见 collect_structured_package 注释）。
#   ★ 批 7 / N-16：旧版这里是"少 2 项"（归档一步写了两条 status），文案却写 − 1，于是数对不上没人能解释；
#   收敛成唯一记账户后，差额回到 1，这句文案与事实一致。
_pkg_items=$_total_items
[ "${CONFIG[STRUCTURED]}" -eq 1 ] && _pkg_items=$((_total_items - 1))

# 生成状态报告（**操作员副本**，落在 filepath，不进包；包内那份由 generate_collection_status 产出）
_status_file="${filepath}/${CONFIG[REPORT_PREFIX]}_status_summary_${_ts_tag}_${qctime}.txt"
{
    printf 'Oracle 巡检状态汇总\n'
    printf '====================\n'
    printf '巡检时间: %s\n' "$(date +'%Y-%m-%d %H:%M:%S')"
    printf '总耗时: %dm%ds\n' "$_total_min" "$_total_sec_rem"
    printf '采集项总数: %s\n' "$_total_items"
    printf '  成功 ok            : %s\n' "$_ok_count"
    printf '  空结果 empty       : %s\n' "$_empty_count"
    printf '  失败               : %s（error %s / timeout %s / permission_denied %s / unsupported %s / schema_mismatch %s / not_enabled %s）\n' \
      "$_err_count" "${STATUS_TALLY[error]:-0}" "${STATUS_TALLY[timeout]:-0}" "${STATUS_TALLY[permission_denied]:-0}" \
      "${STATUS_TALLY[unsupported]:-0}" "$_schema_mismatch_count" "$_not_enabled_count"
    printf '  部分成功 partial   : %s\n' "$_partial_count"
    printf '  跳过 skipped       : %s\n' "$_skip_count"
    printf '分档合计 / 总数     : %s / %s%s\n' "$_bucket_sum" "$_total_items" \
      "$([ "$_bucket_sum" = "$_total_items" ] && printf '' || printf '   ← ⚠ 不一致（台账或分桶口径有漏项）')"
    if [ "${CONFIG[STRUCTURED]}" -eq 1 ]; then
        printf '包内台账项数        : %s（= 总数 − 1；差的那 1 项是 oracle.module.structured_packaging，\n' "$_pkg_items"
        printf '                       由归档动作本身产生，晚于封包时间，结构上无法进入它自己封装的归档）\n'
    fi
    printf 'SQL 查询总数: %s  SQL 失败: %s\n' "${RUNTIME[SQL_QUERY_COUNT]}" "${RUNTIME[SQL_FAIL_COUNT]}"
    printf '\n失败项详情\n'
    awk -F'\t' '$3=="error"||$3=="timeout"||$3=="permission_denied"||$3=="unsupported"||$3=="schema_mismatch"||$3=="not_enabled"{printf "  %-46s [%-16s] %s\n",$1,$3,$10}' "${RUNTIME[STATUS_DIR]}"/*.tsv 2>/dev/null
    printf '\n空结果项\n'
    awk -F'\t' '$3=="empty"{printf "  %-46s\n",$1}' "${RUNTIME[STATUS_DIR]}"/*.tsv 2>/dev/null
    printf '\n（提示）同一份状态台账的机器可读版在包内：collection_status.json + summary.txt\n'
} > "$_status_file"

printf "\n========================================\n"
printf " Oracle 巡检完成\n"
printf " 总耗时: %dm%ds\n" "$_total_min" "$_total_sec_rem"
printf " 执行模块: %d  失败: %d\n" "${RUNTIME[MODULES_EXECUTED]}" "${RUNTIME[MODULES_FAILED]}"
printf " SQL查询: %d  SQL失败: %d\n" "${RUNTIME[SQL_QUERY_COUNT]}" "${RUNTIME[SQL_FAIL_COUNT]}"
printf " 采集项: %d (成功 %d 空结果 %d 失败 %d 部分成功 %d 跳过 %d)\n" \
    "$_total_items" "$_ok_count" "$_empty_count" "$_err_count" "$_partial_count" "$_skip_count"
printf "   失败细分: error %d timeout %d 权限不足 %d 环境不支持 %d 脚本/版本不匹配 %d 未启用 %d\n" \
    "${STATUS_TALLY[error]:-0}" "${STATUS_TALLY[timeout]:-0}" "${STATUS_TALLY[permission_denied]:-0}" \
    "${STATUS_TALLY[unsupported]:-0}" "$_schema_mismatch_count" "$_not_enabled_count"
# ★ N-5：把"包内比控制台少 1 项"当场讲清楚，不再让操作员自己猜（旧版是 156 vs 159 无人能解释）。
if [ "${CONFIG[STRUCTURED]}" -eq 1 ]; then
    printf " 包内台账: %d 项（= 总数 − 1：oracle.module.structured_packaging 由归档动作本身产生，\n" "$_pkg_items"
    printf "           晚于封包时间，结构上无法进入它自己封装的归档；见包内 packaging_status.json）\n"
fi
printf " 报告文件: %s\n" "${RUNTIME[REPORT_FILE]}"
printf " 报告大小: %s\n" "$(du -h "${RUNTIME[REPORT_FILE]}" 2>/dev/null | awk '{print $1}' || echo '未知')"
printf " 日志文件: %s\n" "${RUNTIME[LOG_FILE]}"
printf " 状态文件: %s\n" "$_status_file"
if [ "${CONFIG[STRUCTURED]}" -eq 1 ] && [ -n "${PACKAGE_FILE:-}" ] && [ -f "$PACKAGE_FILE" ]; then
    printf " 数据包:   %s\n" "$PACKAGE_FILE"
elif [ "${CONFIG[STRUCTURED]}" -eq 1 ] && [ -n "$TASK_DIR" ]; then
    printf " 数据目录: %s\n" "$TASK_DIR"
fi
printf "========================================\n"

# 写入总耗时到报告
printf "\n---\n" >> "${RUNTIME[REPORT_FILE]}"
printf "> 巡检总耗时: %dm%ds\n" "$_total_min" "$_total_sec_rem" >> "${RUNTIME[REPORT_FILE]}"

# ==================== 终态退出码（OF-17）====================
# 0=全部成功（含 --no-package）；30=敏感扫描拦截；31=打包失败。
# 这三档由 collect_structured_package 写入 FINAL_EXIT_CODE；早失败点（10/20）与中断（130）已各自 exit。
# 正常结束顺手清掉临时工作目录（OF-26；中断路径由 _trap_cleanup 负责）。
[ -n "${RUNTIME[TMP_DIR]:-}" ] && [ -d "${RUNTIME[TMP_DIR]}" ] && rm -rf "${RUNTIME[TMP_DIR]}" 2>/dev/null
exit "$FINAL_EXIT_CODE"
