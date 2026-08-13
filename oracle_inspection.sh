#!/bin/bash
#===============================================================================
# Oracle 巡检报告脚本 v1.5 (2026-08-10)
# 用法: bash oracle_inspection.sh [host] [port] [service] [user] [password]
#   或  bash oracle_inspection.sh          (交互式输入)
#
# 支持环境: Oracle 单机 / RAC / ASM / CDB(PDB) / ADG
# 支持版本: Oracle 11g / 12c / 18c / 19c / 21c / 23ai
# 自动检测: RAC节点 / ASM磁盘组 / ADG备库 / CDB/PDB — 有才采集，无则跳过
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
CONFIG[AWR_REPORT]=0        # 是否生成AWR报告；默认关闭，需确认 Diagnostics Pack 许可后显式开启
CONFIG[DIAGNOSTICS_PACK]=0  # 1=已确认拥有 Diagnostics Pack 许可，可采集 AWR/ASH/ADDM
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
RUNTIME[START_TIME]=""
RUNTIME[START_EPOCH]=0
RUNTIME[COLLECTION_STARTED_MS]=0
RUNTIME[MODULES_EXECUTED]=0
RUNTIME[MODULES_FAILED]=0
RUNTIME[SQL_QUERY_COUNT]=0
RUNTIME[SQL_FAIL_COUNT]=0

# --- 模块开关 ---
INCLUDE_MODULES=()
EXCLUDE_MODULES=()

# ==================== 使用说明 ====================
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
  --rman-validate       本地执行 RESTORE DATABASE VALIDATE（耗时/IO较高）
  --no-rman-validate    不执行恢复可用性验证（默认）
  --alert-log           扫描告警日志（默认开启）
  --no-alert-log        不扫描告警日志
  --structured          额外输出 TSV/JSON 结构化数据包 (tables/*.tsv, snapshot.json，默认启用)
  --no-structured       不输出结构化包
  --no-package          不生成 tar.gz 打包
  --sar-hours=N         请求最近 N 小时 sar 历史数据（默认24）
  --no-sar              跳过 sar 历史采集
  --config=FILE         从配置文件加载参数

本地免密码登录:
  - 本地主机(127.0.0.1/localhost) + 无密码 + oracle用户或ORACLE_HOME已设置
  - 自动使用 sqlplus / as sysdba 操作系统认证
  - 示例: sudo -u oracle ./oracle_inspection.sh (不传密码)

输出文件:
  \${CONFIG[REPORT_DIR]}/oracle_inspection_<host>_<port>_<时间>.md
  \${CONFIG[REPORT_DIR]}/oracle_inspection_<host>_<port>_<时间>.log
EOF
}

# 解析命令行选项
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
log_write() {
    local level="${1:-INFO}"
    local msg="${2:-}"
    [[ -z "$msg" ]] && return
    local timestamp
    timestamp=$(date +'%Y-%m-%d %H:%M:%S' 2>/dev/null || echo "now")
    printf "[%s] [%s] %s\n" "$timestamp" "$level" "$msg" >> "${RUNTIME[LOG_FILE]}"
}

# 进度输出 (verbose模式同时输出到stdout)
progress() {
    local msg="$1"
    log_write "INFO" "$msg"
    [[ "${CONFIG[VERBOSE]}" -eq 1 ]] && printf "[%s] %s\n" "$(date +'%Y-%m-%d %H:%M:%S')" "$msg"
}

# 警告输出
log_warn() {
    local msg="$1"
    log_write "WARN" "$msg"
    [[ "${CONFIG[VERBOSE]}" -eq 1 ]] && printf "[WARN] %s\n" "$msg"
}

# 错误输出
log_error() {
    local msg="$1"
    log_write "ERROR" "$msg"
    printf "[ERROR] %s\n" "$msg" >&2
}

# ==================== 工具函数 ====================
has_cmd() { command -v "$1" >/dev/null 2>&1; }

md_line()        { printf '%s\n' "$1" >> "${RUNTIME[REPORT_FILE]}"; }
md_block_start() { printf '%s\n' '```' >> "${RUNTIME[REPORT_FILE]}"; }
md_block_end()   { printf '%s\n' '```' >> "${RUNTIME[REPORT_FILE]}"; }

sanitize_filename() {
    printf '%s' "$1" | tr -c 'A-Za-z0-9._-' '_' | sed -e 's/__*/_/g' -e 's/^_//' -e 's/_$//'
}

# TSV 文件用：保留中文和大部分字符
tsv_filename() {
    printf '%s' "$1" | tr '/:*?"<>|\\' '_' | tr -s '_' | head -c 100
}

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

iso_now() {
    date --iso-8601=seconds 2>/dev/null || date +'%Y-%m-%dT%H:%M:%S%z'
}

epoch_ms() {
    local v
    v=$(date +%s%3N 2>/dev/null)
    case "$v" in
        *N*|'') printf '%s000' "$(date +%s)" ;;
        *) printf '%s' "$v" ;;
    esac
}

monotonic_ms() {
    awk '{printf "%.0f", $1 * 1000}' /proc/uptime 2>/dev/null || epoch_ms
}

is_uint() {
    case "${1-}" in ''|*[!0-9]*) return 1 ;; *) return 0 ;; esac
}

# 自动分类失败原因
classify_failure() {
    local rc="$1" err_file="$2"
    if [ "$rc" -eq 124 ] || [ "$rc" -eq 137 ]; then printf 'timeout'; return; fi
    if grep -Eqi 'cannot talk to daemon|could not open connection|daemon is not running|service.*not (running|active)|No such file' "$err_file" 2>/dev/null; then
        printf 'not_enabled'
    elif grep -Eqi 'access denied|command denied|permission denied|requires.*privilege|you need.*privilege' "$err_file" 2>/dev/null; then
        printf 'permission_denied'
    elif grep -Eqi "doesn.t exist|unknown table|unknown system variable|unknown column|not supported|unsupported|ORA-00942|ORA-00904" "$err_file" 2>/dev/null; then
        printf 'unsupported'
    else
        printf 'error'
    fi
}

# 记录单个采集项状态
record_status() {
    local item_id="$1" category="$2" status="$3" started_at="$4" finished_at="$5" duration_ms="$6"
    local row_count="$7" exit_code="$8" output_file="$9" reason="${10-}"
    local f
    f="${RUNTIME[STATUS_DIR]}/$(sanitize_filename "$item_id").tsv"
    printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
      "$item_id" "$category" "$status" "$started_at" "$finished_at" "$duration_ms" "$row_count" "$exit_code" \
      "$output_file" "$(sanitize_text "$reason")" > "$f"
}

# 磁盘空间预检
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
declare -a BG_PIDS BG_NAMES BG_START_ISO BG_START_MS
_trap_cleanup() {
    [ -n "${BG_PIDS[*]:-}" ] && kill "${BG_PIDS[@]}" 2>/dev/null || true
    if [ -n "${RUNTIME[REPORT_FILE]:-}" ] && [ -f "${RUNTIME[REPORT_FILE]}" ]; then
        printf '\n> ⚠️ 采集被中断，报告不完整\n' >> "${RUNTIME[REPORT_FILE]}"
    fi
    exit 130
}
trap _trap_cleanup INT TERM HUP

# ==================== 模块开关检查 ====================
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

# 静默执行，容错版（不影响脚本继续）
ora_try() {
    local sql_log="${RUNTIME[LOG_FILE]:-$(mktemp 2>/dev/null || echo /dev/null)}"
    if [[ "${CONFIG[VERBOSE]}" -eq 1 ]]; then
        "${DB_CONN[SQLPLUS_BIN]}" -S "$(get_ora_conn)" 2>> "$sql_log" <<EOF
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
        "${DB_CONN[SQLPLUS_BIN]}" -S "$(get_ora_conn)" 2>/dev/null <<EOF
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
ora_value() {
    ora_try "SELECT $1;" | tr -d ' \r\n'
}

# 将SQL查询结果以 sqlplus 明文表格输出到 Markdown code block
ora_sql_to_md() {
    local title="$1"
    local sql="$2"
    local page_size="${CONFIG[PAGESIZE]:-200}"
    local item_id status start_iso start_ms end_iso end_ms rc rows err_file reason

    item_id="sql.$(sanitize_filename "${title:-unnamed}")"
    err_file="${RUNTIME[STATUS_DIR]}/.$(sanitize_filename "$item_id").stderr"
    start_iso=$(iso_now); start_ms=$(epoch_ms)

    if [ -n "$title" ]; then
        md_line ""
        md_line "### ${title}"
    fi

    md_block_start
    "${DB_CONN[SQLPLUS_BIN]}" -S "$(get_ora_conn)" >> "${RUNTIME[REPORT_FILE]}" 2> "$err_file" <<EOF
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
-- 限制宽列显示宽度，避免 VARCHAR2(4000) 列撑爆输出
-- 注意：v$parameter.value 已用 SUBSTR() 截断；v$pgastat/gv$sysstat.value(NUMBER) SQL内用 COLUMN 覆盖
COLUMN name         FORMAT a50
COLUMN NAME         FORMAT a50
COLUMN value        FORMAT a80
COLUMN VALUE        FORMAT a80
COLUMN description  FORMAT a80
COLUMN DESCRIPTION  FORMAT a80
COLUMN message      FORMAT a120 WORD_WRAPPED
COLUMN MESSAGE      FORMAT a120 WORD_WRAPPED
COLUMN host_name    FORMAT a30
COLUMN HOST_NAME    FORMAT a30
COLUMN parameter    FORMAT a42
COLUMN PARAMETER    FORMAT a42
COLUMN destination  FORMAT a70
COLUMN member       FORMAT a100
COLUMN file_name    FORMAT a100
COLUMN path         FORMAT a80
COLUMN message_text FORMAT a120 WORD_WRAPPED
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
COLUMN finding_name     FORMAT a80 WORD_WRAPPED
COLUMN FINDING_NAME     FORMAT a80 WORD_WRAPPED
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
    md_block_end

    end_iso=$(iso_now); end_ms=$(epoch_ms)
    RUNTIME[SQL_QUERY_COUNT]=$((RUNTIME[SQL_QUERY_COUNT] + 1))

    # 分类状态
    if [ "$rc" -eq 0 ]; then
        if [ -s "$err_file" ]; then
            if grep -qi 'no rows selected' "$err_file" 2>/dev/null; then
                status="empty"
            else
                status=$(classify_failure "$rc" "$err_file")
            fi
        else
            status="ok"
        fi
    else
        status=$(classify_failure "$rc" "$err_file")
        RUNTIME[SQL_FAIL_COUNT]=$((RUNTIME[SQL_FAIL_COUNT] + 1))
    fi
    reason=$(tail -n 3 "$err_file" 2>/dev/null | tr '\n' ' '; [ -s "$err_file" ] || echo "")

    # 追加错误日志到模块日志
    cat "$err_file" >> "${RUNTIME[LOG_FILE]}" 2>/dev/null
    rm -f "$err_file"

    # 结构化输出：同时写入 TSV（仅在 --structured 模式下，不改动 Markdown 输出）
    if [ "${CONFIG[STRUCTURED]}" -eq 1 ] && [ -n "$TABLES_DIR" ]; then
        local tsv_file="${TABLES_DIR}/$(tsv_filename "${title:-unnamed}").tsv"
        # 用 TAB 分隔符输出 TSV，格式对齐 MySQL 的 mysql_exec --column-names
        "${DB_CONN[SQLPLUS_BIN]}" -S "$(get_ora_conn)" > "$tsv_file" 2>/dev/null <<EOF
WHENEVER SQLERROR CONTINUE
SET PAGESIZE 50000
SET FEEDBACK OFF
SET HEADING ON
SET ECHO OFF
SET TRIMSPOOL ON
SET TRIMOUT ON
SET LINESIZE 32767
SET COLSEP '|'
ALTER SESSION SET nls_date_format='yyyy-mm-dd hh24:mi:ss';
ALTER SESSION SET nls_language='AMERICAN';
$sql
EXIT;
EOF
EXIT;
EOF
        # 头保证：空结果时确保至少有一行header注释
        if [ ! -s "$tsv_file" ]; then
            printf '# no rows returned\n' > "$tsv_file"
        fi
    fi

    record_status "$item_id" "sql" "$status" "$start_iso" "$end_iso" "$((end_ms-start_ms))" "-" "$rc" "report" "$reason"
}

# ==================== 环境检测模块化 ====================

detect_db_basic() {
    progress "检测 Oracle 数据库基本信息..."
    ENV[DB_NAME]=$(ora_value "name FROM v\$database")
    ENV[INSTANCE_NAME]=$(ora_value "instance_name FROM v\$instance")
    ENV[DB_ROLE]=$(ora_value "database_role FROM v\$database")
    ENV[DB_OPEN_MODE]=$(ora_value "open_mode FROM v\$database")
    ENV[ORA_VERSION]=$(ora_value "NVL(version_full, version) FROM v\$instance")
}

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

detect_asm() {
    progress "检测 ASM 环境..."
    local asm_cnt
    asm_cnt=$(ora_value "COUNT(*) FROM v\$asm_diskgroup" 2>/dev/null | tr -d ' \r\n')
    if [ "${asm_cnt:-0}" -gt 0 ]; then
        ENV[HAS_ASM]=1
        progress "检测到 ASM 环境，磁盘组数: ${asm_cnt}"
    else
        progress "未检测到 ASM（或无查询权限）"
    fi
}

detect_adg() {
    progress "检测 ADG 环境..."
    if printf '%s' "${ENV[DB_ROLE]}" | grep -qi "STANDBY"; then
        ENV[IS_STANDBY]=1
        progress "检测到 ADG 物理备库 (DB_ROLE=${ENV[DB_ROLE]})"
    else
        progress "主库角色 (DB_ROLE=${ENV[DB_ROLE]:-PRIMARY})"
    fi
}

detect_cdb() {
    progress "检测 CDB 环境..."
    local cdb_flag
    cdb_flag=$(ora_value "cdb FROM v\$database")
    if [ "$cdb_flag" = "YES" ]; then
        ENV[IS_CDB]=1
        progress "检测到 CDB 容器数据库（12c+），支持PDB采集"
    fi
}

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
run_module() {
    local module_name="$1"
    local func_name="$2"

    # 检查模块开关
    if ! is_module_enabled "$module_name"; then
        progress "跳过模块 [${module_name}] (被排除或未在包含列表中)"
        record_status "module.${module_name}" "module" "skipped" "$(iso_now)" "$(iso_now)" 0 0 "" "excluded by user"
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

    if ! ${func_name} 2>>"${RUNTIME[LOG_FILE]}"; then
        rc=1
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
        status="ok"; reason=""
        progress "[OK]   模块 ${module_name} 完成，耗时 $(((end_ms-start_ms)/1000))s"
    else
        status="error"; reason="module returned non-zero"
        progress "[FAIL] 模块 ${module_name} 异常，耗时 $(((end_ms-start_ms)/1000))s"
    fi
    record_status "module.${module_name}" "module" "$status" "$start_iso" "$end_iso" "$((end_ms-start_ms))" 0 "$rc" "" "$reason"
}

# 后台并行模块支持
start_module_bg() {
    local module_name="$1"; local func_name="$2"
    BG_NAMES+=("$module_name"); BG_START_ISO+=("$(iso_now)"); BG_START_MS+=("$(epoch_ms)")
    progress "后台启动模块: $module_name"
    run_module "$module_name" "$func_name" &
    BG_PIDS+=("$!")
}

wait_background_modules() {
    local i rc
    for ((i=0; i<${#BG_PIDS[@]}; i++)); do
        wait "${BG_PIDS[$i]}"; rc=$?
        if [ "$rc" -ne 0 ]; then
            log_warn "后台模块 ${BG_NAMES[$i]} 返回 ${rc}"
            local item_file="${RUNTIME[STATUS_DIR]}/$(sanitize_filename "module.${BG_NAMES[$i]}").tsv"
            if [ ! -s "$item_file" ]; then
                local end_iso end_ms duration
                end_iso=$(iso_now); end_ms=$(epoch_ms); duration=$((end_ms-BG_START_MS[$i]))
                record_status "module.${BG_NAMES[$i]}" "module" "error" "${BG_START_ISO[$i]}" "$end_iso" "$duration" 0 "$rc" "" "background module terminated before status recorded"
            fi
        fi
    done
}

# ==================== 模块1: 系统信息 ====================
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
    if has_cmd ip; then
        ip addr show >> "${RUNTIME[REPORT_FILE]}" 2>&1
    elif has_cmd ifconfig; then
        ifconfig >> "${RUNTIME[REPORT_FILE]}" 2>&1
    fi
    has_cmd df      && df -HT >> "${RUNTIME[REPORT_FILE]}" 2>&1 || true
    has_cmd lsblk   && lsblk -d -o name,rota >> "${RUNTIME[REPORT_FILE]}" 2>&1 || true
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
    has_cmd vmstat  && vmstat 2 3 >> "${RUNTIME[REPORT_FILE]}" 2>&1 || true
    has_cmd iostat  && iostat -x -k 3 2 >> "${RUNTIME[REPORT_FILE]}" 2>&1 || true
    md_block_end

    md_line ""
    md_line "### 系统 limits 配置"
    md_block_start
    [ -f /etc/security/limits.conf ] && cat /etc/security/limits.conf >> "${RUNTIME[REPORT_FILE]}" 2>&1 || true
    md_block_end

    md_line ""
    md_line "### Oracle 进程信息"
    md_block_start
    # 过滤连接串中可能包含密码的 sqlplus 行，只保留 ora_/asm_ 后台进程
    has_cmd ps && ps -ef | grep -iE "(ora_|asm_|oracle)" | grep -v grep | grep -v "sqlplus" | redact_command_stream >> "${RUNTIME[REPORT_FILE]}" 2>&1 || true
    md_block_end

    md_line ""
    md_line "### Oracle 监听状态"
    md_block_start
    if has_cmd lsnrctl; then
        lsnrctl status >> "${RUNTIME[REPORT_FILE]}" 2>&1
    else
        printf "lsnrctl 不在 PATH，请检查 ORACLE_HOME\n" >> "${RUNTIME[REPORT_FILE]}"
    fi
    md_block_end

    md_line ""
    md_line "### 环境变量"
    md_block_start
    printf "ORACLE_HOME=%s\n"   "${ORACLE_HOME:-未设置}" >> "${RUNTIME[REPORT_FILE]}"
    printf "ORACLE_SID=%s\n"    "${ORACLE_SID:-未设置}" >> "${RUNTIME[REPORT_FILE]}"
    printf "LD_LIBRARY_PATH=%s\n" "${LD_LIBRARY_PATH:-未设置}" >> "${RUNTIME[REPORT_FILE]}"
    printf "PATH=%s\n"          "${PATH}" >> "${RUNTIME[REPORT_FILE]}"
    md_block_end
}

# ==================== 模块2: Oracle 基础信息 ====================
collect_oracle_info() {
    md_line "## Oracle 基础信息"
    md_line ""

    ora_sql_to_md "数据库基本信息" "
SELECT name, db_unique_name, database_role, open_mode, log_mode,
       created, resetlogs_time, protection_mode
FROM v\$database;"

    ora_sql_to_md "实例信息" "
SELECT inst_id, instance_name, host_name, version, startup_time,
       status, database_status, instance_role, active_state
FROM gv\$instance
ORDER BY inst_id;"

    ora_sql_to_md "数据库字符集" "
SELECT parameter, value
FROM nls_database_parameters
WHERE parameter IN ('NLS_CHARACTERSET','NLS_NCHAR_CHARACTERSET',
                    'NLS_LANGUAGE','NLS_TERRITORY','NLS_DATE_FORMAT')
ORDER BY parameter;"

    ora_sql_to_md "关键初始化参数" "
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

    ora_sql_to_md "SGA 组件大小" "
SELECT * FROM (
SELECT pool, name,
       ROUND(bytes/1024/1024, 2) size_mb
FROM v\$sgastat
WHERE pool IS NOT NULL
ORDER BY pool, bytes DESC
) WHERE ROWNUM <= 30;"

    ora_sql_to_md "SGA 汇总信息" "
SELECT name,
       ROUND(value/1024/1024, 2) AS mb
FROM v\$sga
ORDER BY value DESC;"

    ora_sql_to_md "PGA 统计" "
COLUMN value FORMAT 999999999999999999
SELECT name, value
FROM v\$pgastat
WHERE name IN (
    'total PGA inuse','total PGA allocated','maximum PGA allocated',
    'total freeable PGA memory','total PGA used for auto workareas',
    'over allocation count','bytes processed','extra bytes read/written'
);"

    ora_sql_to_md "控制文件" "
SELECT status, name FROM v\$controlfile ORDER BY name;"

    ora_sql_to_md "Redo 日志组信息" "
SELECT l.group#, l.thread#, l.sequence#, l.bytes/1024/1024 size_mb,
       l.members, l.status, lf.member AS filename
FROM v\$log l
JOIN v\$logfile lf ON l.group# = lf.group#
ORDER BY l.thread#, l.group#;"

    ora_sql_to_md "数据库对象统计" "
SELECT object_type, COUNT(*) obj_count
FROM dba_objects
WHERE status = 'VALID'
GROUP BY object_type
ORDER BY obj_count DESC;"

    ora_sql_to_md "无效对象统计" "
SELECT owner, object_type, object_name, status, last_ddl_time
FROM dba_objects
WHERE status != 'VALID'
ORDER BY owner, object_type, object_name;"

    ora_sql_to_md "用户列表" "
SELECT username, account_status, lock_date, expiry_date,
       default_tablespace, temporary_tablespace, created, profile
FROM dba_users
ORDER BY username;"

    ora_sql_to_md "用户密码过期预警（账户非OPEN或30天内到期）" "
SELECT username, account_status, expiry_date,
       TRUNC(expiry_date - SYSDATE) days_until_expire,
       profile, default_tablespace
FROM dba_users
WHERE account_status != 'OPEN'
   OR (expiry_date IS NOT NULL AND expiry_date - SYSDATE < 30)
ORDER BY expiry_date NULLS LAST;"

    ora_sql_to_md "AWR 快照状态（确认 AWR 是否正常运行）" "
SELECT MIN(snap_id) min_snap_id, MAX(snap_id) max_snap_id,
       COUNT(*) snap_count,
       TO_CHAR(MIN(begin_interval_time),'yyyy-mm-dd hh24:mi') earliest,
       TO_CHAR(MAX(end_interval_time)  ,'yyyy-mm-dd hh24:mi') latest
FROM dba_hist_snapshot;"

    ora_sql_to_md "禁用/失效约束（非SYS用户）" "
SELECT owner, constraint_name, constraint_type,
       table_name, status, last_change
FROM dba_constraints
WHERE status != 'ENABLED'
  AND owner NOT IN ('SYS','SYSTEM','OUTLN','DBSNMP','XDB','ORACLE_OCM')
ORDER BY owner, table_name, constraint_name;"

    ora_sql_to_md "分区表统计（分区数 TOP20）" "
SELECT * FROM (
SELECT owner, table_name, partitioning_type, subpartitioning_type,
       partition_count, def_subpartition_count
FROM dba_part_tables
WHERE owner NOT IN ('SYS','SYSTEM','OUTLN','DBSNMP','XDB','ORACLE_OCM')
ORDER BY partition_count DESC
) WHERE ROWNUM <= 20;"

    ora_sql_to_md "大表段 TOP20（按大小排序）" "
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
        ora_sql_to_md "PDB 状态列表（CDB环境）" "
SELECT con_id, name, open_mode, restricted, open_time
FROM v\$pdbs
ORDER BY con_id;"
    fi
}

# ==================== 模块3: 表空间管理 ====================
collect_tablespace() {
    md_line "## 表空间管理"
    md_line ""

    ora_sql_to_md "所有表空间容量使用情况" "
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

    ora_sql_to_md "表空间数据文件列表" "
SELECT tablespace_name, file_name,
       ROUND(bytes/1024/1024,2) size_mb,
       autoextensible,
       ROUND(maxbytes/1024/1024,2) max_mb,
       status
FROM dba_data_files
ORDER BY tablespace_name, file_name;"

    ora_sql_to_md "临时表空间数据文件" "
SELECT tablespace_name, file_name,
       ROUND(bytes/1024/1024,2) size_mb,
       autoextensible,
       ROUND(maxbytes/1024/1024,2) max_mb,
       status
FROM dba_temp_files
ORDER BY tablespace_name, file_name;"

    ora_sql_to_md "表空间增长趋势（AWR，近30天每日汇总）" "
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

    ora_sql_to_md "使用率超过 80% 的表空间（⚠️ 预警）" "
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
collect_asm() {
    if [ "${ENV[HAS_ASM]}" -ne 1 ]; then
        md_line "## ASM 磁盘组"
        md_line ""
        md_line "> 未检测到 ASM 磁盘组，跳过本模块。"
        return 0
    fi

    md_line "## ASM 磁盘组"
    md_line ""

    ora_sql_to_md "ASM 磁盘组汇总" "
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

    ora_sql_to_md "ASM 磁盘详情" "
SELECT dg.name diskgroup,
       d.disk_number, d.name disk_name,
       ROUND(d.total_mb/1024, 2) total_gb,
       ROUND(d.free_mb/1024, 2)  free_gb,
       d.state, d.mode_status, d.path,
       d.header_status
FROM v\$asm_disk d
LEFT JOIN v\$asm_diskgroup dg ON d.group_number = dg.group_number
ORDER BY dg.name, d.disk_number;"

    ora_sql_to_md "ASM 磁盘组中的数据库文件数" "
SELECT group_number, type, COUNT(*) file_count,
       ROUND(SUM(bytes)/1024/1024/1024, 2) total_gb
FROM v\$asm_file
GROUP BY group_number, type
ORDER BY group_number, type;"
}

# ==================== 模块5: 归档日志 ====================
collect_archive() {
    md_line "## 归档日志"
    md_line ""

    ora_sql_to_md "归档模式检查" "
SELECT (SELECT log_mode FROM v\$database) log_mode,
       (SELECT archiver FROM v\$instance) archiver
FROM DUAL;"

    ora_sql_to_md "归档目标参数" "
SELECT dest_id, dest_name, status, target, archiver,
       schedule, destination, db_unique_name
FROM v\$archive_dest
WHERE status != 'INACTIVE'
ORDER BY dest_id;"

    ora_sql_to_md "近3天每小时归档量" "
SELECT thread# AS rac_thread,
       TO_CHAR(first_time,'YYYY-MM-DD HH24') AS hour_period,
       COUNT(*) archive_count,
       ROUND(SUM(blocks * block_size)/1024/1024, 2) total_size_mb
FROM v\$archived_log
WHERE first_time >= SYSDATE - 3
  AND archived = 'YES'
GROUP BY thread#, TO_CHAR(first_time,'YYYY-MM-DD HH24')
ORDER BY hour_period, thread# ASC;"

    ora_sql_to_md "近30天每日归档量" "
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

    ora_sql_to_md "重做日志切换频率（近24小时）" "
SELECT TRUNC(first_time,'hh24') switch_time,
       COUNT(*) switch_count
FROM v\$log_history
WHERE first_time > SYSDATE - 1
GROUP BY TRUNC(first_time,'hh24')
ORDER BY switch_time DESC;"

    if [ "${ENV[HAS_ASM]}" -eq 1 ]; then
        ora_sql_to_md "ASM 快速恢复区使用情况" "
SELECT name, space_limit/1024/1024/1024 limit_gb,
       space_used/1024/1024/1024  used_gb,
       space_reclaimable/1024/1024/1024 reclaimable_gb,
       number_of_files
FROM v\$recovery_file_dest;"
    fi

    # FRA 使用情况（无ASM时也检查）
    ora_sql_to_md "快速恢复区（FRA）参数" "
SELECT name, SUBSTR(value, 1, 500) value
FROM v\$parameter
WHERE name IN ('db_recovery_file_dest','db_recovery_file_dest_size',
               'log_archive_dest_1','log_archive_format')
ORDER BY name;"
}

# ==================== 模块6: 会话与锁管理 ====================
collect_session_lock() {
    md_line "## 会话与锁管理"
    md_line ""

    ora_sql_to_md "当前活跃会话" "
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

    ora_sql_to_md "当前会话等待事件（非空闲）" "
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

    ora_sql_to_md "会话锁信息" "
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

    ora_sql_to_md "锁等待链（阻塞者 → 被阻塞者）" "
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

    ora_sql_to_md "长事务（运行超过1小时）" "
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
    "${DB_CONN[SQLPLUS_BIN]}" -S "$(get_ora_conn)" >> "${RUNTIME[REPORT_FILE]}" 2>>"${RUNTIME[LOG_FILE]}" <<'SQLEOF'
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
collect_performance() {
    md_line "## 性能诊断"
    md_line ""

    if [ "${CONFIG[DIAGNOSTICS_PACK]:-0}" -eq 1 ]; then
      ora_sql_to_md "SQL 资源消耗 TOP10（近1小时 ASH）" "
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

    ora_sql_to_md "CPU 高消耗会话 TOP10" "
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

    ora_sql_to_md "IO 消耗最高数据文件 TOP10" "
SELECT * FROM (
SELECT df.file_name,
       f.phyrds, f.phywrts, f.phyblkrd, f.phyblkwrt,
       ROUND((f.phyrds+f.phywrts), 0) total_io
FROM v\$filestat f
JOIN dba_data_files df ON f.file# = df.file_id
ORDER BY total_io DESC
) WHERE ROWNUM <= 10;"

    ora_sql_to_md "等待事件 TOP10（数据库级）" "
SELECT * FROM (
  SELECT event, total_waits, time_waited,
         ROUND(time_waited/NULLIF(total_waits,0), 2) avg_wait_cs
  FROM v\$system_event
  WHERE wait_class NOT IN ('Idle','System I/O')
  ORDER BY time_waited DESC
) t WHERE ROWNUM <= 10;"

    if [ "${CONFIG[DIAGNOSTICS_PACK]:-0}" -eq 1 ]; then
      ora_sql_to_md "TOP SQL 等待事件（近1小时）" "
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

    ora_sql_to_md "共享池碎片" "
SELECT pool,
       ROUND(SUM(bytes)/1024/1024, 2) free_mb,
       ROUND(AVG(bytes)/1024, 2) avg_free_kb,
       COUNT(*) free_chunks
FROM v\$sgastat
WHERE pool IN ('shared pool','large pool')
  AND name = 'free memory'
GROUP BY pool;"

    ora_sql_to_md "Library Cache 命中率" "
SELECT namespace,
       gets, gethits,
       ROUND(gethitratio * 100, 4) gethit_pct,
       pins, pinhits,
       ROUND(pinhitratio * 100, 4) pinhit_pct,
       reloads, invalidations
FROM v\$librarycache
WHERE namespace IN ('SQL AREA','TABLE/PROCEDURE','BODY','TRIGGER')
ORDER BY namespace;"

    ora_sql_to_md "Buffer Cache 命中率" "
SELECT ROUND((1 - phyrds/(NULLIF(blkgets+consistentgets,0))) * 100, 4) hit_ratio_pct
FROM (SELECT SUM(physical_reads) phyrds,
             SUM(db_block_gets) blkgets,
             SUM(consistent_gets) consistentgets
      FROM v\$buffer_pool_statistics);"

    ora_sql_to_md "临时表空间排序段使用" "
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

    ora_sql_to_md "UNDO 使用统计 (v\$undostat)" "
SELECT * FROM (
SELECT begin_time, end_time, undotsn,
       undoblks, txncount, maxquerylen, maxqueryid,
       ssolderrcnt, nospaceerrcnt
FROM v\$undostat
ORDER BY begin_time DESC
) WHERE ROWNUM <= 20;"

    ora_sql_to_md "UNDO 参数" "
SELECT name, SUBSTR(value, 1, 400) value
FROM v\$parameter
WHERE name IN ('undo_management','undo_tablespace','undo_retention');"
}

# ==================== 模块8: 索引管理 ====================
collect_index() {
    md_line "## 索引管理"
    md_line ""

    ora_sql_to_md "无效/不可用索引" "
SELECT owner, index_name, table_name, index_type, status
FROM dba_indexes
WHERE status NOT IN ('VALID','N/A')
  AND owner NOT IN ('SYS','SYSTEM','OUTLN','DBSNMP','XDB')
ORDER BY owner, table_name, index_name;"

    ora_sql_to_md "失效分区索引" "
SELECT index_owner, index_name, partition_name, status
FROM dba_ind_partitions
WHERE status NOT IN ('USABLE','N/A')
ORDER BY index_owner, index_name, partition_name;"

    ora_sql_to_md "失效子分区索引" "
SELECT index_owner, index_name, partition_name, subpartition_name, status
FROM dba_ind_subpartitions
WHERE status NOT IN ('USABLE','N/A')
ORDER BY index_owner, index_name, subpartition_name;"

    ora_sql_to_md "大索引段 TOP20" "
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
    ora_sql_to_md "DataGuard 同步统计" "
SELECT name, value FROM v\$dataguard_stats
ORDER BY name;"

    ora_sql_to_md "DataGuard 配置参数" "
SELECT name, value
FROM v\$parameter
WHERE name IN (
    'log_archive_dest_1','log_archive_dest_2','log_archive_dest_state_1',
    'log_archive_dest_state_2','fal_server','fal_client','db_file_name_convert',
    'log_file_name_convert','standby_file_management',
    'archive_lag_target','log_archive_max_processes'
)
ORDER BY name;"

    ora_sql_to_md "Managed Standby 进程状态" "
SELECT process, status, client_process, sequence#, thread#, block#, blocks
FROM v\$managed_standby
ORDER BY process;"

    ora_sql_to_md "日志传输/应用状态" "
SELECT dest_id, type, status, error,
       archived_seq#, applied_seq#
FROM v\$archive_dest_status
WHERE status != 'INACTIVE'
ORDER BY dest_id;"

    if [ "${ENV[IS_STANDBY]}" -eq 1 ]; then
        md_line ""
        md_line "### 备库专项检查"

        ora_sql_to_md "备库文件同步状态" "
SELECT name, status, bytes/1024/1024 size_mb
FROM v\$datafile
ORDER BY name;"

        ora_sql_to_md "主备日志差距（已归档 vs 已应用）" "
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

        ora_sql_to_md "备库角色切换检查" "
SELECT switchover_status, database_role, db_unique_name
FROM v\$database;"

    fi
}

# ==================== 模块10: RAC 专项（有才采集）====================
collect_rac() {
    if [ "${ENV[IS_RAC]}" -ne 1 ]; then
        md_line "## RAC 专项"
        md_line ""
        md_line "> 非 RAC 环境，跳过本模块。"
        return 0
    fi

    md_line "## RAC 专项"
    md_line ""

    ora_sql_to_md "RAC 各节点实例状态" "
SELECT inst_id, instance_name, host_name, version,
       startup_time, status, database_status, instance_role
FROM gv\$instance
ORDER BY inst_id;"

    ora_sql_to_md "RAC 各节点性能对比" "
COLUMN value FORMAT 99999999999999999
SELECT inst_id, name,
       ROUND(value, 0) value
FROM gv\$sysstat
WHERE name IN ('CPU used by this session','physical reads',
               'physical writes','user commits','user rollbacks',
               'redo size','parse count (total)','execute count')
ORDER BY name, inst_id;"

    ora_sql_to_md "RAC 通道等待分析" "
SELECT inst_id, event, COUNT(*) sessions,
       ROUND(AVG(wait_time), 2) avg_wait,
       ROUND(AVG(seconds_in_wait), 2) avg_sec_in_wait,
       SUM(CASE WHEN state='WAITING' THEN 1 ELSE 0 END) active_waiting
FROM gv\$session_wait
WHERE wait_class != 'Idle'
  AND event LIKE '%channel%'
GROUP BY inst_id, event
ORDER BY inst_id, sessions DESC;"

    ora_sql_to_md "RAC 各节点等待事件（超10个会话）" "
SELECT inst_id, event, COUNT(*) cnt
FROM gv\$session_wait
GROUP BY inst_id, event
HAVING COUNT(*) > 10
ORDER BY inst_id ASC, cnt DESC;"

    ora_sql_to_md "RAC 消耗临时空间 SQL（>1GB）" "
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

    ora_sql_to_md "RAC GES/GC 锁等待" "
SELECT event, total_waits, time_waited,
       ROUND(average_wait, 2) avg_wait_cs
FROM v\$system_event
WHERE wait_class = 'Cluster'
ORDER BY time_waited DESC;"
}

# ==================== 模块11: RMAN 备份检查 ====================
collect_rman() {
    md_line "## RMAN 备份检查"
    md_line ""

    ora_sql_to_md "最近备份集汇总" "
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

    ora_sql_to_md "备份策略参数（RMAN CONFIGURE）" "
SELECT name, value
FROM v\$rman_configuration
ORDER BY name;"

    ora_sql_to_md "RMAN 备份历史状态（近7天）" "
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
    ora_sql_to_md "ADR Diag 路径" "
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
collect_pdb() {
    if [ "${ENV[IS_CDB]}" -ne 1 ]; then
        return 0
    fi

    md_line "## PDB 容器数据库巡检"
    md_line ""

    ora_sql_to_md "PDB 状态详情" "
SELECT con_id, name, open_mode, restricted, creation_time,
       total_size/1024/1024/1024 total_size_gb,
       used_space/1024/1024/1024 used_size_gb
FROM v\$pdbs
ORDER BY con_id;"

    ora_sql_to_md "PDB 资源限制" "
SELECT c.name AS pdb_name,
       r.resource_name, r.current_utilization, r.max_utilization, r.limit_value
FROM v\$resource_limit r
JOIN v\$containers c ON r.con_id = c.con_id
WHERE c.name != 'CDB\$ROOT'
ORDER BY c.name, r.resource_name;"

    ora_sql_to_md "PDB 表空间使用率" "
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

    ora_sql_to_md "PDB 会话数统计" "
SELECT c.name AS pdb_name,
       COUNT(s.sid) session_count,
       SUM(DECODE(s.status, 'ACTIVE', 1, 0)) active_count
FROM v\$session s
JOIN v\$containers c ON s.con_id = c.con_id
WHERE c.name != 'CDB\$ROOT'
GROUP BY c.name
ORDER BY session_count DESC;"

    ora_sql_to_md "PDB 服务配置" "
SELECT pdb, name service_name, creation_date,
       enabled, goal, clb_goal
FROM v\$services
WHERE pdb IS NOT NULL AND pdb != 'CDB\$ROOT'
ORDER BY pdb, name;"

    ora_sql_to_md "PDB 闪回配置" "
SELECT name, open_mode,
       log_mode
FROM v\$pdbs
ORDER BY con_id;"

    ora_sql_to_md "CDB 级别闪回配置" "
SELECT flashback_on, log_mode, force_logging, supplemental_log_data_min
FROM v\$database;"

    ora_sql_to_md "PDB 角色与切换状态" "
SELECT name AS pdb_name, open_mode, restricted
FROM v\$containers
WHERE name != 'CDB\$ROOT';"
}

# ==================== 模块14: RAC 高可用检查 ====================
collect_rac_ha() {
    if [ "${ENV[IS_RAC]}" -ne 1 ]; then
        return 0
    fi

    md_line "## RAC 高可用检查"
    md_line ""

    # CRS 资源状态 — 优先用 crsctl（需 grid 用户），sqlplus 查询 Grid 视图可能无权限
    if has_cmd crsctl; then
        md_line "### CRS 资源状态 (crsctl)"
        md_block_start
        crsctl query crs activeversion 2>/dev/null >> "${RUNTIME[REPORT_FILE]}" || echo "crsctl activeversion failed" >> "${RUNTIME[REPORT_FILE]}"
        crsctl stat res -t 2>/dev/null | head -100 >> "${RUNTIME[REPORT_FILE]}" || echo "crsctl stat res failed" >> "${RUNTIME[REPORT_FILE]}"
        md_block_end

        md_line "### CRS 守护进程状态 (crsctl)"
        md_block_start
        crsctl check crs 2>/dev/null >> "${RUNTIME[REPORT_FILE]}" || echo "crsctl check crs failed" >> "${RUNTIME[REPORT_FILE]}"
        md_block_end
    fi

    # OCR 表决盘 — 用 crsctl/ocrcheck 命令（oracle 用户可执行）
    md_line "### OCR 表决盘信息 (crsctl)"
    md_block_start
    if has_cmd ocrcheck; then
        ocrcheck 2>/dev/null >> "${RUNTIME[REPORT_FILE]}" || echo "ocrcheck failed" >> "${RUNTIME[REPORT_FILE]}"
    else
        echo "ocrcheck not found" >> "${RUNTIME[REPORT_FILE]}"
    fi
    if has_cmd crsctl; then
        echo "" >> "${RUNTIME[REPORT_FILE]}"
        crsctl query css votedisk 2>/dev/null >> "${RUNTIME[REPORT_FILE]}" || echo "crsctl query css votedisk failed" >> "${RUNTIME[REPORT_FILE]}"
    fi
    md_block_end

    # GPNP 检查
    if has_cmd gpnptool; then
        md_line ""
        md_line "### GPNP Profile 状态"
        md_block_start
        if gpnptool get 2>/dev/null | head -50 >> "${RUNTIME[REPORT_FILE]}"; then
            :
        else
            echo "GPNP tool not accessible" >> "${RUNTIME[REPORT_FILE]}"
        fi
        md_block_end
    fi

    # 服务配置检查
    ora_sql_to_md "Service 服务配置" "
SELECT * FROM (
SELECT name, failover_method, failover_type,
       goal, clb_goal, edition
FROM dba_services
ORDER BY name
) WHERE ROWNUM <= 50;"
}

# ==================== 模块15: DG Broker 巡检 ====================
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
        ora_sql_to_md "DG Broker 配置概览" "
SELECT config_seq_num, database_role, flush_seq#, flush_error,
       CAST(last_sent AS DATE) last_sent,
       CAST(last_received AS DATE) last_received
FROM v\$dataguard_config
WHERE ROWNUM <= 10;"
    fi

    ora_sql_to_md "DG Broker 启用状态" "
SELECT name, SUBSTR(value, 1, 100) value
FROM v\$parameter
WHERE name = 'dg_broker_start';"

    ora_sql_to_md "日志传输服务状态 (v\$archive_dest)" "
SELECT dest_id, dest_name, type, status, error,
       transmit_mode, compression
FROM v\$archive_dest
WHERE status != 'INACTIVE'
ORDER BY dest_id;"

    ora_sql_to_md "保护模式与主库统计" "
SELECT name, value
FROM v\$dataguard_stats;"

    # Fast-Start Failover 检查 (依赖 DGMGRL 配置)
    ora_sql_to_md "DG Broker 配置参数" "
SELECT name, value
FROM v\$parameter
WHERE name LIKE 'dg_broker%%'
ORDER BY name;"

    ora_sql_to_md "最近 DG 事件 (v\$dataguard_status)" "
SELECT * FROM (
SELECT timestamp, message, error_code
FROM v\$dataguard_status
WHERE timestamp > SYSDATE - 1
ORDER BY timestamp DESC
) WHERE ROWNUM <= 20;"

    ora_sql_to_md "主备库日志序差距" "
SELECT al.thread#,
       MAX(al.sequence#) - MAX(bl.sequence#) gap
FROM v\$archived_log al
JOIN (SELECT thread#, MAX(sequence#) sequence# FROM v\$archived_log WHERE applied='YES' GROUP BY thread#) bl
  ON al.thread# = bl.thread#
WHERE al.archived = 'YES'
GROUP BY al.thread#;"

    # 物理备库专项
    if [ "${ENV[IS_STANDBY]}" -eq 1 ]; then
        ora_sql_to_md "备库 RFS 进程状态" "
SELECT process, status, thread#, sequence#, block#, blocks, delay_mins
FROM v\$managed_standby
WHERE process IN ('RFS','MRP0','MRP','LSP')
ORDER BY process;"

        ora_sql_to_md "备库应用延迟监控" "
SELECT name, value, unit, time_computed
FROM v\$dataguard_stats
WHERE name IN ('apply lag', 'transport lag', 'estimated startup time');"
    fi
}

# ==================== 模块16: 闪回数据库巡检 ====================
collect_flashback() {
    md_line "## 闪回数据库巡检"
    md_line ""

    ora_sql_to_md "闪回日志使用统计" "
SELECT ROUND(flashback_data/1024/1024, 2) fbdata_mb,
       ROUND(db_data/1024/1024, 2) dbdata_mb,
       ROUND(redo_data/1024/1024, 2) redo_mb,
       ROUND(estimated_flashback_size/1024/1024/1024, 3) est_size_gb
FROM v\$flashback_database_stat
WHERE end_time = (SELECT MAX(end_time) FROM v\$flashback_database_stat);"

    ora_sql_to_md "闪回区使用情况" "
SELECT space_limit/1024/1024/1024 limit_gb,
       space_used/1024/1024/1024 used_gb,
       space_used/space_limit * 100 pct_used,
       space_reclaimable/1024/1024/1024 reclaimable_gb,
       number_of_files
FROM v\$recovery_file_dest;"

    ora_sql_to_md "Guaranteed Restore Point" "
SELECT name, storage_size/1024/1024/1024 size_gb,
       restore_point_time, guarantee_flashback_database
FROM v\$restore_point
WHERE guarantee_flashback_database = 'YES'
ORDER BY restore_point_time DESC;"

    ora_sql_to_md "普通 Restore Point" "
SELECT * FROM (
SELECT name, scn, storage_size/1024/1024 size_mb,
       restore_point_time, guarantee_flashback_database
FROM v\$restore_point
WHERE guarantee_flashback_database = 'NO'
ORDER BY restore_point_time DESC
) WHERE ROWNUM <= 20;"

    # PDB 闪回检查
    if [ "${ENV[IS_CDB]}" -eq 1 ]; then
        ora_sql_to_md "PDB 级别闪回配置" "
SELECT con_id, name, open_mode, restricted
FROM v\$pdbs;"
    fi
}

# ==================== 模块17: 审计与安全巡检 ====================
collect_audit_security() {
    md_line "## 审计与安全巡检"
    md_line ""

    ora_sql_to_md "审计策略检查 (FGA)" "
SELECT policy_name, object_schema, object_name, enabled
FROM dba_audit_policies;"

    ora_sql_to_md "失败登录审计统计" "
SELECT userid, COUNT(*) cnt, MAX(ntimestamp#) last_attempt
FROM sys.aud$
WHERE action# = 100 AND returncode != 0 AND ntimestamp# > SYSDATE - 7
GROUP BY userid
ORDER BY cnt DESC;"

    ora_sql_to_md "密码配置文件参数" "
SELECT profile, resource_name, resource_type, limit
FROM dba_profiles
WHERE profile = 'DEFAULT'
  AND resource_name IN ('PASSWORD_VERIFY_FUNCTION','PASSWORD_LIFE_TIME',
                        'PASSWORD_REUSE_TIME','PASSWORD_REUSE_MAX',
                        'FAILED_LOGIN_ATTEMPTS','PASSWORD_LOCK_TIME')
ORDER BY resource_name;"

    ora_sql_to_md "默认配置文件非默认设置" "
SELECT profile, resource_name, limit
FROM dba_profiles
WHERE profile != 'DEFAULT'
  AND resource_name LIKE 'PASSWORD%'
ORDER BY profile, resource_name;"

    ora_sql_to_md "锁定账户检查" "
SELECT username, account_status, lock_date,
       TRUNC(lock_date - SYSDATE) days_locked
FROM dba_users
WHERE account_status LIKE '%LOCKED%'
ORDER BY username;"

    ora_sql_to_md "特权用户 (DBA角色)" "
SELECT grantee, granted_role, admin_option, common
FROM dba_role_privs
WHERE granted_role = 'DBA'
  AND grantee NOT IN ('SYS','SYSTEM','OUTLN')
ORDER BY grantee;"

    ora_sql_to_md "系统特权授予 (非标准用户)" "
SELECT grantee, privilege, admin_option, common
FROM dba_sys_privs
WHERE grantee NOT IN ('SYS','SYSTEM','OUTLN','DBSNMP','ORACLE_OCM')
  AND admin_option = 'YES'
ORDER BY grantee, privilege;"

    ora_sql_to_md "密码文件成员" "
SELECT username, sysdba, sysoper, sysasm
FROM v\$pwfile_users
ORDER BY username;"

    ora_sql_to_md "对象审计统计" "
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
collect_awr_stats() {
    md_line "## AWR 与统计信息巡检"
    md_line ""

    ora_sql_to_md "管理包访问设置" "
SELECT name, value
FROM v\$parameter
WHERE name IN ('control_management_pack_access','statistics_level');"

    if [ "${CONFIG[DIAGNOSTICS_PACK]:-0}" -eq 1 ]; then
      ora_sql_to_md "AWR 基线列表" "
SELECT baseline_id, baseline_name, start_snap_id, end_snap_id,
       baseline_type, moving_window_size
FROM dba_hist_baseline
ORDER BY baseline_id;"

    ora_sql_to_md "最近 AWR 快照" "
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

    ora_sql_to_md "SQL Plan Baseline" "
SELECT * FROM (
SELECT sql_handle, plan_name, origin, enabled, accepted,
       fixed, autopurge, created
FROM dba_sql_plan_baselines
ORDER BY created DESC
) WHERE ROWNUM <= 20;"

    ora_sql_to_md "自动统计信息收集 Job" "
SELECT client_name, status, window_group
FROM dba_autotask_client
ORDER BY client_name;"

    ora_sql_to_md "缺失统计信息表 (TOP20)" "
SELECT * FROM (
SELECT owner, table_name, num_rows, last_analyzed,
       stale_stats
FROM dba_tab_statistics
WHERE stale_stats = 'YES'
  AND owner NOT IN ('SYS','SYSTEM','OUTLN','DBSNMP')
ORDER BY last_analyzed NULLS FIRST
) WHERE ROWNUM <= 20;"

    ora_sql_to_md "段级高水位统计 (HWM)" "
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

    ora_sql_to_md "数据膨胀预估 (DBA_SEGMENTS vs DBA_TABLES)" "
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

    ora_sql_to_md "自动段空间管理 (ASSM) 状态" "
SELECT tablespace_name, segment_space_management, extent_management
FROM dba_tablespaces
WHERE segment_space_management = 'AUTO'
ORDER BY tablespace_name;"

    ora_sql_to_md "索引使用状态 (未使用/使用中)" "
SELECT * FROM (
SELECT index_name, table_name, monitoring, used, start_monitoring, end_monitoring
FROM v\$object_usage
ORDER BY used, start_monitoring DESC
) WHERE ROWNUM <= 100;" 2>/dev/null || md_line "> v\$object_usage 不可用（需 ALTER INDEX ... MONITORING USAGE 启用监控）"
}

# ==================== 模块 SYS-HIST: SAR 历史数据采集 ====================

find_sar_files() {
    local d
    for d in /var/log/sa /var/log/sysstat; do
        [ -d "$d" ] || continue
        find "$d" -maxdepth 1 -type f \( -name 'sa[0-9][0-9]' -o -name 'sa[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]' \) -mtime -2 -readable 2>/dev/null
    done | sort -u
}

collect_sadf_metric() {
    local metric_id="$1" outfile="$2"; shift 2
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

compute_sar_coverage() {
    local src="$HISTORY_DIR/sar_cpu.csv" t first_epoch="" last_epoch="" first_ts="" last_ts="" coverage_seconds=0 coverage_hours=0 cov_status="empty"
    [ -s "$src" ] || return 0
    while IFS= read -r t; do
        [ -n "$t" ] || continue
        e=$(date -d "$t" +%s 2>/dev/null) || continue
        if [ -z "$first_epoch" ] || [ "$e" -lt "$first_epoch" ]; then first_epoch=$e; first_ts=$t; fi
        if [ -z "$last_epoch" ] || [ "$e" -gt "$last_epoch" ]; then last_epoch=$e; last_ts=$t; fi
    done < <(awk -F';' '!/^#/ && NF>=3 && $3 ~ /^[0-9][0-9][0-9][0-9]-/ {print $3}' "$src")
    if [ -n "$first_epoch" ] && [ -n "$last_epoch" ]; then
        coverage_seconds=$((last_epoch-first_epoch)); [ "$coverage_seconds" -lt 0 ] && coverage_seconds=0
        coverage_hours=$(awk -v s="$coverage_seconds" 'BEGIN{printf "%.2f",s/3600}')
        cov_status="ok"
        [ "$coverage_seconds" -lt $((CONFIG[SAR_HOURS]*3600*9/10)) ] && cov_status="partial"
    fi
    {
      printf 'status\t%s\n' "$cov_status"
      printf 'requested_hours\t%s\n' "${CONFIG[SAR_HOURS]}"
      printf 'first_timestamp\t%s\n' "$first_ts"
      printf 'last_timestamp\t%s\n' "$last_ts"
      printf 'coverage_seconds\t%s\n' "$coverage_seconds"
      printf 'coverage_hours\t%s\n' "$coverage_hours"
    } > "$HISTORY_DIR/coverage.tsv"
}

collect_sar_history() {
    if [ "${CONFIG[SAR_ENABLED]}" -ne 1 ]; then
        progress "SAR 历史采集已禁用"
        record_status "system.sar_history" "system.history" "skipped" "$(iso_now)" "$(iso_now)" 0 0 "" "disabled by config"
        return 0
    fi
    local start_iso start_ms end_iso end_ms status reason count
    start_iso=$(iso_now); start_ms=$(epoch_ms)
    SAR_FILE_LIST="${RUNTIME[STATUS_DIR]}/sar_files.txt"; find_sar_files > "$SAR_FILE_LIST"
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
        compute_sar_coverage
        local coverage_status
        coverage_status=$(awk -F'\t' '$1=="status"{print $2}' "$HISTORY_DIR/coverage.tsv" 2>/dev/null)
        [ "$coverage_status" = "partial" ] && status="partial" || status="ok"
        reason="raw sadf data exported"
    fi
    end_iso=$(iso_now); end_ms=$(epoch_ms)
    record_status "system.sar_history" "system.history" "$status" "$start_iso" "$end_iso" "$((end_ms-start_ms))" "${count:-0}" 0 "history/" "$reason"
    progress "SAR 历史采集完成: status=${status} files=${count}"
    return 0
}

# ==================== 模块 SYS-RT: 实时时序差分采样 ====================

_read_cpu_stat() { awk '/^cpu /{print $2,$3,$4,$5,$6,$7,$8,$9; exit}' /proc/stat 2>/dev/null; }

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

append_memory_sample() {
    local ts="$1" elapsed_ms="$2" vals mt ma st sf cached dirty writeback usedpct
    vals=$(awk '$1=="MemTotal:"{mt=$2}$1=="MemAvailable:"{ma=$2}$1=="SwapTotal:"{st=$2}$1=="SwapFree:"{sf=$2}$1=="Cached:"{ca=$2}$1=="Dirty:"{d=$2}$1=="Writeback:"{w=$2}END{print mt+0,ma+0,st+0,sf+0,ca+0,d+0,w+0}' /proc/meminfo 2>/dev/null)
    read -r mt ma st sf cached dirty writeback <<< "$vals"
    usedpct=$(awk -v t="$mt" -v a="$ma" 'BEGIN{if(t>0)printf "%.4f",(t-a)*100/t;else print 0}')
    printf '%s,%s,%s,%s,%s,%s,%s,%s,%s\n' "$ts" "$elapsed_ms" "$((mt*1024))" "$((ma*1024))" "$usedpct" "$((st*1024))" "$(((st-sf)*1024))" "$((cached*1024))" "$(((dirty+writeback)*1024))" >> "$MEM_CSV"
}

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

append_disk_sample() {
    local ts="$1" elapsed_ms="$2" delta_ms="$3" p dev s ri rs rt wi ws wt inflight io_ms weighted sector_size
    local dri drs drt dwi dws dwt dio dweighted
    for p in /sys/block/*; do
        [ -r "$p/stat" ] || continue; dev=${p##*/}; case "$dev" in loop*|ram*|zram*|fd*|sr*) continue;; esac
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

append_oracle_sysstat_sample() {
    local ts="$1" elapsed_ms="$2" tmp
    tmp="${RUNTIME[STATUS_DIR]}/oracle_stat_${elapsed_ms}.tsv"
    "${DB_CONN[SQLPLUS_BIN]}" -S "$(get_ora_conn)" > "$tmp" 2>> "${RUNTIME[LOG_FILE]}" <<'EOSQL' || return 1
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
    awk -v ts="$ts" -v em="$elapsed_ms" '
      BEGIN{n=split("user_commits user_rollbacks execute_count parse_count_total parse_count_hard physical_reads physical_writes redo_size sorts_memory sorts_disk table_scans_long table_scan_rows consistent_gets db_block_gets redo_writes redo_entries session_logical_reads physical_reads_direct physical_writes_direct",k," ")}
      {v[$1]=$2}
      END{printf "%s,%s",ts,em;for(i=1;i<=n;i++)printf ",%s",(k[i] in v?v[k[i]]:"");printf "\n"}' "$tmp" >> "$ORACLE_CSV"
    rm -f "$tmp"
}

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
    record_status "timeseries.realtime_sampling" "timeseries" "$status" "$sampling_started_at" "$sampling_finished_at" "$((sampling_finished_ms-sampling_started_ms))" "$oracle_points" "$rc" "timeseries/" "$reason"
    progress "实时采样完成: status=${status} oracle_points=${oracle_points}"
    return "$rc"
}

# ==================== 模块18b: AWR 性能报告 ====================
collect_awr_report() {
    # 开关判断
    if [ "${CONFIG[AWR_REPORT]}" -ne 1 ] || [ "${CONFIG[DIAGNOSTICS_PACK]:-0}" -ne 1 ]; then
        md_line "## AWR 性能报告"
        md_line ""
        md_line "> AWR 报告已跳过。生成报告必须同时指定 --diagnostics-pack 和 --awr-report。"
        return 0
    fi

    md_line "## AWR 性能报告"
    md_line ""

    local awr_hours="${CONFIG[AWR_HOURS]:-1}"

    # 查找最近两个完整快照（一次查询，省掉一个 sqlplus 连接）
    local begin_snap end_snap
    local snap_data
    snap_data=$("${DB_CONN[SQLPLUS_BIN]}" -S "$(get_ora_conn)" <<AWREOF 2>/dev/null
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
    if [ -z "$begin_snap" ] || [ -z "$end_snap" ] || ! [[ "$begin_snap" =~ ^[0-9]+$ ]] || ! [[ "$end_snap" =~ ^[0-9]+$ ]] || [ "$begin_snap" -ge "$end_snap" ]; then
        md_line "> ⚠️ 近 ${awr_hours} 小时内快照不足2个，无法生成 AWR 报告"
        md_line "> 请确认: AWR 快照是否正常采集（默认每小时一次）"
        return 0
    fi

    # 获取 dbid 和 instance_number
    local dbid inst_num
    dbid=$(ora_value "dbid FROM v\$database")
    dbid=${dbid##*:}; dbid=${dbid//[!0-9]/}
    inst_num=$(ora_value "instance_number FROM v\$instance")
    inst_num=${inst_num##*:}; inst_num=${inst_num//[!0-9]/}

    # 生成 AWR HTML 报告
    local awr_file
    awr_file="${CONFIG[REPORT_DIR]}/${CONFIG[REPORT_PREFIX]}_awr_${instance_tag:-report}_${qctime}.html"

    "${DB_CONN[SQLPLUS_BIN]}" -S "$(get_ora_conn)" > "$awr_file" <<AWREOF
SET PAGESIZE 0 FEEDBACK OFF HEADING OFF ECHO OFF TRIMSPOOL ON
SET LINESIZE 10000 LONG 10000000 LONGCHUNKSIZE 1000000 TRIM ON
WHENEVER SQLERROR CONTINUE
SELECT output FROM TABLE(DBMS_WORKLOAD_REPOSITORY.AWR_REPORT_HTML(
    $dbid, $inst_num, $begin_snap, $end_snap));
EXIT;
AWREOF

    local awr_basename
    awr_basename=$(basename "$awr_file")

    # 检查报告是否成功生成（内容必须包含 HTML 标签，而不是 Oracle 错误）
    if [ -s "$awr_file" ] && grep -q '<!DOCTYPE\|^<html' "$awr_file" 2>/dev/null; then
        md_line "AWR 报告已生成: [\`${awr_basename}\`](${awr_basename})"
        md_line ""
        md_line "| 项目 | 值 |"
        md_line "| --- | --- |"
        md_line "| 时间范围 | 近 ${awr_hours} 小时 |"
        md_line "| 起始快照 | ${begin_snap} |"
        md_line "| 结束快照 | ${end_snap} |"
        md_line "| DBID | ${dbid} |"
        md_line "| 实例号 | ${inst_num} |"
        md_line "| 报告文件 | ${awr_basename} |"
    else
        md_line "> ⚠️ AWR 报告生成失败，请确认 Diagnostics Pack 许可"
    fi
}

# ==================== 模块19: 控制文件与SPFILE 巡检 ====================
collect_control_spfile() {
    md_line "## 控制文件与 SPFILE 巡检"
    md_line ""

    ora_sql_to_md "控制文件序列号" "
SELECT status, file_size_blks, block_size, name
FROM v\$controlfile
ORDER BY name;"

    ora_sql_to_md "SPFILE 当前值 (关键参数)" "
SELECT sid, name, value, isspecified
FROM v\$spparameter
WHERE name IN ('spfile','*.audit_file_dest','*.control_files',
               '*.db_recovery_file_dest','*.log_archive_dest_1',
               '*.log_archive_dest_2','*.log_archive_format')
  AND value IS NOT NULL
ORDER BY sid, name;"

    ora_sql_to_md "最近参数更改" "
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
collect_diag_top() {
    md_line "## 故障诊断与 Top Events"
    md_line ""

    ora_sql_to_md "Top 20 SQL (按逻辑读)" "
SELECT * FROM (
  SELECT sql_id, plan_hash_value, module,
         ROUND(elapsed_time/1000000/60, 2) elapsed_min,
         ROUND(cpu_time/1000000, 2) cpu_sec,
         buffer_gets, disk_reads, sorts,
         ROW_NUMBER() OVER(ORDER BY buffer_gets DESC) rn
  FROM v\$sql
  WHERE buffer_gets > 0 AND module IS NOT NULL
) t WHERE rn <= 20;"

    ora_sql_to_md "Top 20 SQL (按执行时间)" "
SELECT * FROM (
  SELECT sql_id, plan_hash_value, module,
         ROUND(elapsed_time/1000000/60, 2) elapsed_min,
         ROUND(cpu_time/1000000, 2) cpu_sec,
         executions, buffer_gets,
         ROW_NUMBER() OVER(ORDER BY elapsed_time DESC) rn
  FROM v\$sql
  WHERE elapsed_time > 0 AND executions > 0
) t WHERE rn <= 20;"

    ora_sql_to_md "Top 20 SQL (按物理读)" "
  SELECT * FROM (
  SELECT sql_id, plan_hash_value, module,
         disk_reads, buffer_gets, executions,
         ROUND(elapsed_time/1000000/60, 2) elapsed_min
  FROM v\$sql
  WHERE disk_reads > 0
  ORDER BY disk_reads DESC
  ) WHERE ROWNUM <= 20;"

    ora_sql_to_md "ADR 预警日志路径" "
SELECT name, value
FROM v\$diag_info
WHERE name IN ('Diag Trace','Diag Alert','ADR Home','Trace')
ORDER BY name;"

    # v$diag_alert_ext 需要特定权限，添加权限检测
    ora_sql_to_md "最近 SQL 错误 (近24h)" "
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
        ora_sql_to_md "Database Health Check (ADDM)" "
SELECT * FROM (
SELECT task_name, finding_name, impact_type,
       ROUND(impact, 2) impact, message
FROM dba_advisor_findings
WHERE impact > 10
ORDER BY impact DESC
) WHERE ROWNUM <= 30;"
    fi

    if [ "${CONFIG[DIAGNOSTICS_PACK]:-0}" -eq 1 ]; then
      ora_sql_to_md "Top Segment 访问 (ASH)" "
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
    "${DB_CONN[SQLPLUS_BIN]}" -S "$(get_ora_conn)" 2>/dev/null <<'PLANEOF' >> "${RUNTIME[REPORT_FILE]}"
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
collect_production_validation() {
    md_line "## 生产级完整性与可恢复性验证"
    md_line ""
    local dg_configured=0
    local dg_dest_count
    dg_dest_count=$(ora_try "SELECT COUNT(*) FROM v\$archive_dest WHERE target='STANDBY' AND destination IS NOT NULL;" | tr -cd '0-9')
    if [ "${ENV[IS_STANDBY]:-0}" -eq 1 ] || [ "${dg_dest_count:-0}" -gt 0 ]; then
        dg_configured=1
    fi

    ora_sql_to_md "数据库组件状态" "
SELECT comp_id, comp_name, version, status
FROM dba_registry
ORDER BY status, comp_id;"

    ora_sql_to_md "SQL 补丁应用状态" "
SELECT patch_id, patch_type, action, status,
       TO_CHAR(action_time,'YYYY-MM-DD HH24:MI:SS') action_time,
       SUBSTR(description,1,100) description
FROM dba_registry_sqlpatch
ORDER BY action_time DESC;"

    ora_sql_to_md "数据库坏块记录" "
SELECT file#, block#, blocks, corruption_change#, corruption_type
FROM v\$database_block_corruption
ORDER BY file#, block#;"

    ora_sql_to_md "数据文件头异常" "
SELECT file#, status, error, recover, fuzzy,
       checkpoint_change#, checkpoint_time
FROM v\$datafile_header
WHERE status NOT IN ('ONLINE','SYSTEM')
   OR error IS NOT NULL
   OR recover = 'YES'
   OR fuzzy = 'YES'
ORDER BY file#;"

    ora_sql_to_md "块校验与丢失写保护参数" "
SELECT name, SUBSTR(value,1,200) value, isdefault
FROM v\$parameter
WHERE name IN ('db_block_checksum','db_block_checking','db_lost_write_protect',
               'db_ultra_safe','filesystemio_options')
ORDER BY name;"

    ora_sql_to_md "数据文件自动扩展余量" "
SELECT tablespace_name,
       COUNT(*) datafile_count,
       ROUND(SUM(bytes)/1024/1024) current_mb,
       ROUND(SUM(CASE WHEN autoextensible='YES' THEN maxbytes ELSE bytes END)/1024/1024) max_mb,
       ROUND((SUM(CASE WHEN autoextensible='YES' THEN maxbytes ELSE bytes END)-SUM(bytes))/1024/1024) headroom_mb,
       SUM(CASE WHEN autoextensible='YES' THEN 1 ELSE 0 END) autoextend_files
FROM dba_data_files
GROUP BY tablespace_name
ORDER BY headroom_mb;"

    ora_sql_to_md "Scheduler 失败作业（近7天）" "
SELECT * FROM (
SELECT owner, job_name, status, error#,
       TO_CHAR(actual_start_date,'YYYY-MM-DD HH24:MI:SS') start_time,
       run_duration, SUBSTR(additional_info,1,120) additional_info
FROM dba_scheduler_job_run_details
WHERE log_date >= SYSTIMESTAMP - INTERVAL '7' DAY
  AND status NOT IN ('SUCCEEDED','STOPPED')
ORDER BY log_date DESC
) WHERE ROWNUM <= 50;"

    ora_sql_to_md "Scheduler 长时间运行作业" "
SELECT owner, job_name, session_id, running_instance,
       TO_CHAR(elapsed_time) elapsed_time,
       TO_CHAR(cpu_used) cpu_used
FROM dba_scheduler_running_jobs
WHERE elapsed_time > INTERVAL '1' HOUR
ORDER BY elapsed_time DESC;"

    ora_sql_to_md "最近 RMAN VALIDATE 记录" "
SELECT * FROM (
SELECT session_key, operation, status,
       TO_CHAR(start_time,'YYYY-MM-DD HH24:MI:SS') start_time,
       TO_CHAR(end_time,'YYYY-MM-DD HH24:MI:SS') end_time,
       object_type
FROM v\$rman_status
WHERE UPPER(operation) LIKE '%VALIDATE%'
ORDER BY start_time DESC
) WHERE ROWNUM <= 20;"

    ora_sql_to_md "RMAN 备份片异常状态" "
SELECT status, device_type, COUNT(*) piece_count,
       MIN(completion_time) oldest_time,
       MAX(completion_time) newest_time
FROM v\$backup_piece_details
WHERE completion_time >= SYSDATE - 30
  AND status <> 'A'
GROUP BY status, device_type
ORDER BY status, device_type;"

    ora_sql_to_md "控制文件多路复用" "
SELECT COUNT(*) controlfile_count,
       LISTAGG(name, ' | ') WITHIN GROUP (ORDER BY name) controlfile_paths
FROM v\$controlfile;"

    ora_sql_to_md "Redo 日志多路复用检查" "
SELECT l.thread#, l.group#, l.bytes/1024/1024 size_mb,
       COUNT(lf.member) member_count,
       l.status
FROM v\$log l
LEFT JOIN v\$logfile lf ON l.group#=lf.group#
GROUP BY l.thread#, l.group#, l.bytes, l.status
HAVING COUNT(lf.member) < 2
ORDER BY l.thread#, l.group#;"

    ora_sql_to_md "Unified Auditing 与 TDE 状态" "
SELECT 'Unified Auditing' item,
       CASE WHEN EXISTS (SELECT 1 FROM v\$option WHERE parameter='Unified Auditing' AND value='TRUE')
            THEN 'ENABLED' ELSE 'DISABLED' END status
FROM dual
UNION ALL
SELECT 'TDE Wallet', NVL(MAX(status),'NOT_CONFIGURED')
FROM v\$encryption_wallet;"

    if [ "$dg_configured" -eq 1 ]; then
      ora_sql_to_md "Standby Redo Log 配置覆盖" "
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

json_escape() {
    local s="${1-}"
    s=${s//\\/\\\\}; s=${s//\"/\\\"}
    s=${s//$'\n'/\\n}; s=${s//$'\r'/\\r}; s=${s//$'\t'/\\t}
    printf '%s' "$s"
}

json_quote() { printf '"%s"' "$(json_escape "${1-}")"; }

file_size_bytes() {
    if stat -c '%s' "$1" >/dev/null 2>&1; then stat -c '%s' "$1"
    elif stat -f '%z' "$1" >/dev/null 2>&1; then stat -f '%z' "$1"
    else wc -c < "$1" | tr -d ' '
    fi
}

sha256_file() {
    if has_cmd sha256sum; then sha256sum "$1" | awk '{print $1}'
    elif has_cmd shasum; then shasum -a 256 "$1" | awk '{print $1}'
    elif has_cmd openssl; then openssl dgst -sha256 "$1" | awk '{print $NF}'
    else printf 'UNAVAILABLE'
    fi
}

safe_relpath() { printf '%s' "${1#"$TASK_DIR"/}"; }

generate_snapshot_json() {
    [ "${CONFIG[STRUCTURED]}" -eq 1 ] || return 0
    [ -n "$TASK_DIR" ] || return 0
    {
        printf '{\n'
        printf '  "schema_version":"1.0",\n'
        printf '  "database_type":"oracle",\n'
        printf '  "collector_version":"1.4",\n'
        printf '  "instance_tag":%s,\n' "$(json_quote "$instance_tag")"
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
        printf '  "diagnostics_pack":%s\n' "${CONFIG[DIAGNOSTICS_PACK]}"
        printf '}\n'
    } > "$SNAPSHOT_FILE"
    progress "snapshot.json 已生成"
}

generate_manifest() {
    [ "${CONFIG[STRUCTURED]}" -eq 1 ] || return 0
    [ -n "$TASK_DIR" ] || return 0
    local f rel first=1
    {
        printf '{\n  "package_version":"1.0",\n  "collector_version":"1.4",\n  "database_type":"oracle",\n  "instance_tag":%s,\n  "created_at":%s,\n  "files":[\n' \
            "$(json_quote "$instance_tag")" "$(json_quote "$(iso_now)")"
        # 列出 TASK_DIR 下的数据文件
        find "$TASK_DIR" -type f ! -name 'manifest.json' -print | sort | while IFS= read -r f; do
            rel=$(safe_relpath "$f")
            [ "$first" -eq 1 ] || printf ',\n'; first=0
            printf '    {"path":%s,"size_bytes":%s,"sha256":%s}' "$(json_quote "$rel")" "$(file_size_bytes "$f")" "$(json_quote "$(sha256_file "$f")")"
        done
        printf '\n  ]\n}\n'
    } > "$MANIFEST_FILE"
    progress "manifest.json 已生成"
}

security_scan() {
    [ "${CONFIG[STRUCTURED]}" -eq 1 ] || return 0
    [ -n "$TASK_DIR" ] || return 0
    local findings="${RUNTIME[STATUS_DIR]}/security_scan_findings.txt"
    : > "$findings"
    grep -RInE --exclude='collection.log' --exclude='manifest.json' --exclude='security_scan_findings.txt' \
      '(password[[:space:]]*=[[:space:]]*"?[^<[:space:]";]+|--password(=|[[:space:]])[^<[:space:]|]+|BEGIN[[:space:]].*PRIVATE KEY|Authorization:[[:space:]]*(Basic|Bearer)[[:space:]]+[A-Za-z0-9._-]+)' \
      "$TASK_DIR" > "$findings" 2>/dev/null
    if [ -s "$findings" ]; then
        log_error "安全扫描发现 ${BASH_SOURCE:-$0} 输出中可能包含敏感信息，已阻止打包"
        record_status "package.security_scan" "package" "error" "$(iso_now)" "$(iso_now)" 0 "$(wc -l < "$findings")" 1 "status/security_scan_findings.txt" "sensitive data detected; package blocked"
        return 1
    fi
    record_status "package.security_scan" "package" "ok" "$(iso_now)" "$(iso_now)" 0 0 0 "" ""
    return 0
}

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

collect_structured_package() {
    [ "${CONFIG[STRUCTURED]}" -eq 1 ] || return 0
    local start_iso start_ms end_iso end_ms
    start_iso=$(iso_now); start_ms=$(epoch_ms)

    # 将时序和历史数据复制到 TASK_DIR
    [ -d "$TIMESERIES_DIR" ] && cp -r "$TIMESERIES_DIR" "$TASK_DIR/timeseries" 2>/dev/null
    [ -d "$HISTORY_DIR" ] && cp -r "$HISTORY_DIR" "$TASK_DIR/history" 2>/dev/null
    [ -d "${RUNTIME[STATUS_DIR]}" ] && cp -r "${RUNTIME[STATUS_DIR]}" "$TASK_DIR/status" 2>/dev/null

    generate_snapshot_json
    generate_manifest

    # 打包前安全扫描
    if security_scan; then
        create_package
    else
        log_error "打包前安全扫描未通过，已跳过 tar.gz 打包。请检查 TASK_DIR 中是否有敏感信息。"
    fi

    end_iso=$(iso_now); end_ms=$(epoch_ms)
    local status="ok"; local reason="structured package completed"
    if [ -n "${PACKAGE_FILE:-}" ] && [ -f "$PACKAGE_FILE" ]; then
        reason="package created: $(basename "$PACKAGE_FILE")"
    fi
    record_status "module.structured_packaging" "module" "$status" "$start_iso" "$end_iso" "$((end_ms-start_ms))" 0 0 "" "$reason"
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
        exit 1
    fi
    DB_CONN[SQLPLUS_BIN]="$sqlplus_path"
fi

# 主机标识（仿 MySQL：localhost 时解析真实 IP）
_COLLECTOR_HOSTNAME=$(hostname -s 2>/dev/null || hostname 2>/dev/null || echo "unknown")
_COLLECTOR_PRIMARY_IP=$(detect_primary_ipv4)
case "${DB_CONN[HOST]}" in
    127.*|localhost|::1) _INSTANCE_ADDRESS="$_COLLECTOR_PRIMARY_IP" ;;
    *) _INSTANCE_ADDRESS="${DB_CONN[HOST]}" ;;
esac
_ts_tag=$(sanitize_filename "${_COLLECTOR_HOSTNAME}_${_INSTANCE_ADDRESS}_${DB_CONN[PORT]}")

# ==================== 初始化日志路径 ====================
qctime=$(date +'%Y-%m-%d_%H-%M-%S')
filepath="${CONFIG[REPORT_DIR]:-/var/tmp}"
mkdir -p "$filepath"
RUNTIME[START_TIME]="$qctime"
RUNTIME[START_EPOCH]=$(date +%s)
RUNTIME[LOG_FILE]="${filepath}/${CONFIG[REPORT_PREFIX]}_${_ts_tag}_${qctime}.log"
RUNTIME[STATUS_DIR]="${filepath}/${CONFIG[REPORT_PREFIX]}_status_${_ts_tag}_${qctime}"
RUNTIME[COLLECTION_STARTED_MS]=$(epoch_ms)
mkdir -p "${RUNTIME[STATUS_DIR]}" || { printf "ERROR: 无法创建状态目录\n" >&2; exit 1; }

# 时序采样和历史数据目录
TIMESERIES_DIR="${filepath}/${CONFIG[REPORT_PREFIX]}_timeseries_${_ts_tag}_${qctime}"
HISTORY_DIR="${filepath}/${CONFIG[REPORT_PREFIX]}_history_${_ts_tag}_${qctime}"
mkdir -p "$TIMESERIES_DIR" "$HISTORY_DIR"

# 时序 CSV 输出路径
CPU_CSV="${TIMESERIES_DIR}/system_cpu.csv"
MEM_CSV="${TIMESERIES_DIR}/system_memory.csv"
NET_CSV="${TIMESERIES_DIR}/system_network.csv"
DISK_CSV="${TIMESERIES_DIR}/system_disk.csv"
ORACLE_CSV="${TIMESERIES_DIR}/oracle_sysstat.csv"
printf "Oracle Inspection Start: %s\n" "$(date)" >> "${RUNTIME[LOG_FILE]}"

# Dry-run 模式预览
if [[ "${CONFIG[DRY_RUN]}" -eq 1 ]]; then
    printf "========================================\n"
    printf " Oracle 巡检 Dry-Run 模式\n"
    printf " 将执行的模块:\n"
    run_module "生成AWR性能报告"           collect_awr_report
    run_module "采集系统信息"              collect_sys_info
    run_module "采集SAR历史"               collect_sar_history
    run_module "实时时序采样"              collect_oracle_realtime_samples
    run_module "采集Oracle基础信息"        collect_oracle_info
    run_module "采集PDB容器信息"           collect_pdb
    run_module "采集表空间信息"            collect_tablespace
    run_module "采集ASM磁盘组"             collect_asm
    run_module "采集归档日志"              collect_archive
    run_module "采集会话与锁"              collect_session_lock
    run_module "采集性能诊断"              collect_performance
    run_module "采集索引信息"              collect_index
    run_module "采集ADG状态"               collect_adg
    run_module "采集DG Broker"             collect_dg_broker
    run_module "采集闪回数据库"            collect_flashback
    run_module "采集RAC专项"               collect_rac
    run_module "采集RAC高可用"              collect_rac_ha
    run_module "采集RMAN备份信息"          collect_rman
    run_module "采集控制文件与SPFILE"      collect_control_spfile
    run_module "采集审计与安全"            collect_audit_security
    run_module "采集AWR与统计信息"         collect_awr_stats
    run_module "采集故障诊断"              collect_diag_top
    run_module "生产级完整性验证"          collect_production_validation
    run_module "扫描告警日志"              collect_alert_log
    run_module "健康检查总结"              collect_health_summary
    run_module "结构化打包"                collect_structured_package
    printf "========================================\n"
    printf " 巡检预览完成，实际未执行任何采集\n"
    exit 0
fi

# ==================== 连接测试 ====================
CONN_TEST_ERR=$("${DB_CONN[SQLPLUS_BIN]}" -S "$(get_ora_conn)" <<'EOF' 2>&1
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
    exit 1
fi

# 磁盘空间预检
check_output_space || { printf "ERROR: 磁盘空间不足，无法继续\n" >&2; exit 1; }

# ==================== 环境检测 ====================
detect_env

# ==================== 生成文件名 ====================
instance_tag="$(sanitize_filename "${ENV[DB_NAME]:-oracle}_${_COLLECTOR_HOSTNAME}_${_INSTANCE_ADDRESS}_${DB_CONN[PORT]}_${ENV[INSTANCE_NAME]:-inst}")"
RUNTIME[REPORT_FILE]="${filepath}/${CONFIG[REPORT_PREFIX]}_${instance_tag}_${qctime}.md"

# 结构化任务目录（复用报告命名，与 MySQL 风格一致）
TASK_DIR=""; TABLES_DIR=""; EVIDENCE_DIR=""
if [ "${CONFIG[STRUCTURED]}" -eq 1 ]; then
    TASK_DIR="${filepath}/${CONFIG[REPORT_PREFIX]}_task_${instance_tag}_${qctime}"
    TABLES_DIR="${TASK_DIR}/tables"
    EVIDENCE_DIR="${TASK_DIR}/evidence"
    MANIFEST_FILE="${TASK_DIR}/manifest.json"
    SNAPSHOT_FILE="${TASK_DIR}/snapshot.json"
    mkdir -p "$TABLES_DIR" "$EVIDENCE_DIR"
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
printf "========================================\n"

# ==================== 执行各模块 ====================
# 后台并行：SAR 历史 + 实时时序（两者独立于 Oracle 主流程，耗时约 30s）
run_module "生成AWR性能报告"           collect_awr_report
run_module "采集系统信息"              collect_sys_info
start_module_bg "采集SAR历史"          collect_sar_history
start_module_bg "实时时序采样"         collect_oracle_realtime_samples
run_module "采集Oracle基础信息"        collect_oracle_info
run_module "采集PDB容器信息"           collect_pdb
run_module "采集表空间信息"            collect_tablespace
run_module "采集ASM磁盘组"             collect_asm
run_module "采集归档日志"              collect_archive
run_module "采集会话与锁"              collect_session_lock
run_module "采集性能诊断"              collect_performance
run_module "采集索引信息"              collect_index
run_module "采集ADG状态"               collect_adg
run_module "采集DG Broker"             collect_dg_broker
run_module "采集闪回数据库"            collect_flashback
run_module "采集RAC专项"               collect_rac
run_module "采集RAC高可用"              collect_rac_ha
run_module "采集RMAN备份信息"          collect_rman
run_module "采集控制文件与SPFILE"      collect_control_spfile
run_module "采集审计与安全"            collect_audit_security
run_module "采集AWR与统计信息"         collect_awr_stats
run_module "采集故障诊断"              collect_diag_top
run_module "生产级完整性验证"          collect_production_validation
run_module "扫描告警日志"              collect_alert_log
# 等待后台模块完成后再生成总结
wait_background_modules
run_module "健康检查总结"              collect_health_summary
run_module "结构化打包"                collect_structured_package

# ==================== 完成 ====================
# 计算总耗时
_start_ep=${RUNTIME[START_EPOCH]:-0}
_end_ep=$(date +%s)
_total_sec=$((_end_ep - _start_ep))
_total_min=$((_total_sec / 60))
_total_sec_rem=$((_total_sec % 60))

# 汇总采集状态
_total_items=$(ls -1 "${RUNTIME[STATUS_DIR]}"/*.tsv 2>/dev/null | wc -l | tr -d ' ')
_ok_count=$(awk -F'\t' '$3=="ok"{n++}END{print n+0}' "${RUNTIME[STATUS_DIR]}"/*.tsv 2>/dev/null)
_empty_count=$(awk -F'\t' '$3=="empty"{n++}END{print n+0}' "${RUNTIME[STATUS_DIR]}"/*.tsv 2>/dev/null)
_err_count=$(awk -F'\t' '$3=="error"||$3=="timeout"||$3=="permission_denied"||$3=="unsupported"{n++}END{print n+0}' "${RUNTIME[STATUS_DIR]}"/*.tsv 2>/dev/null)
_skip_count=$(awk -F'\t' '$3=="skipped"{n++}END{print n+0}' "${RUNTIME[STATUS_DIR]}"/*.tsv 2>/dev/null)
_not_enabled_count=$(awk -F'\t' '$3=="not_enabled"{n++}END{print n+0}' "${RUNTIME[STATUS_DIR]}"/*.tsv 2>/dev/null)

# 生成状态报告
_status_file="${filepath}/${CONFIG[REPORT_PREFIX]}_status_summary_${_ts_tag}_${qctime}.txt"
{
    printf 'Oracle 巡检状态汇总\n'
    printf '====================\n'
    printf '巡检时间: %s\n' "$(date +'%Y-%m-%d %H:%M:%S')"
    printf '总耗时: %dm%ds\n' "$_total_min" "$_total_sec_rem"
    printf '采集项总数: %s  成功: %s  空结果: %s  失败: %s  跳过/未启用: %s\n' \
      "$_total_items" "$_ok_count" "$_empty_count" "$_err_count" "$((_skip_count+_not_enabled_count))"
    printf 'SQL 查询总数: %s  SQL 失败: %s\n' "${RUNTIME[SQL_QUERY_COUNT]}" "${RUNTIME[SQL_FAIL_COUNT]}"
    printf '\n失败项详情\n'
    awk -F'\t' '$3=="error"||$3=="timeout"||$3=="permission_denied"||$3=="unsupported"{printf "  %-50s [%s] %s\n",$1,$3,$10}' "${RUNTIME[STATUS_DIR]}"/*.tsv 2>/dev/null
    printf '\n空结果项\n'
    awk -F'\t' '$3=="empty"{printf "  %-50s\n",$1}' "${RUNTIME[STATUS_DIR]}"/*.tsv 2>/dev/null
} > "$_status_file"

printf "\n========================================\n"
printf " Oracle 巡检完成\n"
printf " 总耗时: %dm%ds\n" "$_total_min" "$_total_sec_rem"
printf " 执行模块: %d  失败: %d\n" "${RUNTIME[MODULES_EXECUTED]}" "${RUNTIME[MODULES_FAILED]}"
printf " SQL查询: %d  SQL失败: %d\n" "${RUNTIME[SQL_QUERY_COUNT]}" "${RUNTIME[SQL_FAIL_COUNT]}"
printf " 采集项: %d (成功 %d 空 %d 错误 %d)\n" "$_total_items" "$_ok_count" "$_empty_count" "$_err_count"
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
