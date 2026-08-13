#!/usr/bin/env bash
# PostgreSQL Inspection Collector v2.0.1-standard
# 客户侧只负责：只读采集、时序采样、状态记录和打包。
# 风险判断、图表和报告均由后续 Python 完成。
# 架构对齐 MySQL Inspection Collector v1.1.0

if [ -z "${BASH_VERSION:-}" ] || [ -n "${POSIXLY_CORRECT:-}" ] || { set -o 2>/dev/null | grep -qi '^posix.*on'; }; then
    exec bash "$0" "$@"
fi

set -o pipefail
set +e
umask 077
export LC_ALL=C
export LANG=C

COLLECTOR_VERSION="2.0.1"
SNAPSHOT_SCHEMA_VERSION="1.0"
PACKAGE_VERSION="1.0"

SAR_HISTORY_HOURS=24
SAMPLE_INTERVAL=5
SAMPLE_COUNT=6
PG_TIMEOUT_SECONDS=30
MIN_FREE_MB=200
TOP_N=100
OUTPUT_PARENT="/var/tmp"
CREATE_PACKAGE=1
INCLUDE_LOG_TEXT=0
ALL_DATABASES=1
REDACT_SQL=0

PGHOST_ARG="${PGHOST:-/tmp}"
PGPORT_ARG="${PGPORT:-5432}"
PGUSER_ARG="${PGUSER:-postgres}"
PGDATABASE_ARG="${PGDATABASE:-postgres}"
PASSWORD_FILE=""
LEGACY_PASSWORD=""

show_usage() {
cat <<'EOF'
PostgreSQL 巡检采集器 v2.0.1（标准版）

推荐用法：
  bash pg_inspection_standard.sh --host /tmp --port 5432 --user postgres

认证方式（推荐顺序）：
  --password-file FILE    从权限为 600 的文件读取密码
  未指定时               交互式隐藏输入密码，或依赖 ~/.pgpass / PGPASSWORD

常用参数：
  --host HOST             默认 PGHOST 或 /tmp（Unix socket）
  --port PORT             默认 PGPORT 或 5432
  --user USER             默认 PGUSER 或 postgres
  --database DB           默认 PGDATABASE 或 postgres
  --output-dir DIR        默认 /var/tmp
  --no-package            不生成 tar.gz，仅保留任务目录
  --current-database-only 仅巡检当前数据库（默认逐库巡检）
  --redact-sql            脱敏 SQL 正文，不采集 query 字段

高级参数：
  --sample-interval SEC   默认 5 秒
  --sample-count N        默认 6 次，总时长约 30 秒
  --sar-history-hours N   默认请求最近 24 小时历史 sar
  --pg-timeout SEC        单条 PG 命令超时，默认 30 秒
  --include-log-text      包含有限日志样本（默认关闭）

说明：
  1. 同一份脚本可在主库、备库执行。
  2. 一个 PG 实例生成一个采集包；主从拓扑由后续 Python 使用多个包合并判断。
  3. 客户侧不做风险评级，不生成图表，不生成 Word。
EOF
}

# ============ 工具函数 ============

has_cmd() { command -v "$1" >/dev/null 2>&1; }

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

sanitize_id() {
    printf '%s' "$1" | tr -c 'A-Za-z0-9._-' '_' | sed -e 's/__*/_/g' -e 's/^_//' -e 's/_$//'
}

collect_all_ipv4() {
    if has_cmd ip; then
        ip -o -4 addr show scope global 2>/dev/null | awk '{split($4,a,"/"); if(a[1] != "127.0.0.1") print a[1]}' | sort -u | paste -sd, -
    elif has_cmd hostname; then
        hostname -I 2>/dev/null | tr ' ' '\n' | awk '/^[0-9]+([.][0-9]+){3}$/ && $0 != "127.0.0.1"' | sort -u | paste -sd, -
    fi
}

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

resolve_ipv4() {
    local host="$1" v=""
    case "$host" in 127.*|localhost|::1|/*) printf '127.0.0.1'; return 0 ;; esac
    if printf '%s' "$host" | grep -Eq '^[0-9]+([.][0-9]+){3}$'; then printf '%s' "$host"; return 0; fi
    if has_cmd getent; then v=$(getent ahostsv4 "$host" 2>/dev/null | awk 'NR==1{print $1}'); fi
    [ -z "$v" ] && has_cmd host && v=$(host "$host" 2>/dev/null | awk '/has address/{print $NF; exit}')
    printf '%s' "$v"
}

is_local_connect_target() {
    local host="$1" resolved="$2" short fqdn ips
    case "$host" in 127.*|localhost|::1|/*) return 0 ;; esac
    short=$(hostname -s 2>/dev/null || hostname 2>/dev/null)
    fqdn=$(hostname -f 2>/dev/null || true)
    if [ "$host" = "$short" ] || { [ -n "$fqdn" ] && [ "$host" = "$fqdn" ]; }; then return 0; fi
    ips=",$(collect_all_ipv4),"
    if [ -n "$resolved" ] && printf '%s' "$ips" | grep -Fq ",$resolved,"; then return 0; fi
    return 1
}

sanitize_text() {
    printf '%s' "${1-}" | tr '\t\r\n' '   ' | sed 's/[[:space:]][[:space:]]*/ /g'
}

json_escape() {
    local s="${1-}"
    s=${s//\\/\\\\}
    s=${s//\"/\\\"}
    s=${s//$'\n'/\\n}
    s=${s//$'\r'/\\r}
    s=${s//$'\t'/\\t}
    printf '%s' "$s"
}

json_quote() { printf '"%s"' "$(json_escape "${1-}")"; }

json_number_or_null() {
    local v="${1-}"
    if printf '%s' "$v" | grep -Eq '^-?[0-9]+([.][0-9]+)?$'; then printf '%s' "$v"; else printf 'null'; fi
}

is_uint() {
    case "${1-}" in ''|*[!0-9]*) return 1 ;; *) return 0 ;; esac
}

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

run_with_timeout() {
    local seconds="$1"; shift
    if has_cmd timeout; then timeout --signal=TERM --kill-after=5 "$seconds" "$@"; else "$@"; fi
}

log() {
    local level="$1"; shift
    printf '[%s] [%s] %s\n' "$(date +'%Y-%m-%d %H:%M:%S')" "$level" "$*" | tee -a "$LOG_FILE"
}

log_info() { log INFO "$@"; }
log_warn() { log WARN "$@"; }
log_error() { log ERROR "$@"; }

classify_failure() {
    local rc="$1" err_file="$2"
    if [ "$rc" -eq 124 ] || [ "$rc" -eq 137 ]; then printf 'timeout'; return; fi
    if grep -Eqi 'cannot talk to daemon|could not open connection|service.*not (running|active)|command not found' "$err_file" 2>/dev/null; then
        printf 'not_enabled'
    elif grep -Eqi 'access denied|command denied|permission denied|requires.*privilege|you need.*privilege' "$err_file" 2>/dev/null; then
        printf 'permission_denied'
    elif grep -Eqi "doesn.t exist|unknown table|unknown system variable|unknown column|not supported|unsupported|relation.*does not exist" "$err_file" 2>/dev/null; then
        printf 'unsupported'
    else
        printf 'error'
    fi
}

known_tsv_header() {
    local name
    name=$(basename "$1")
    case "$name" in
      file_settings_errors.tsv) printf 'sourcefile\tsourceline\tname\tsetting\terror' ;;
      long_transactions.tsv) printf 'pid\tusername\tdatabase_name\tclient_addr\tstate\tduration_seconds\twait_event_type\twait_event\tquery' ;;
      idle_transactions.tsv) printf 'pid\tusername\tdatabase_name\tclient_addr\tduration_seconds\tquery' ;;
      lock_waits.tsv) printf 'blocked_pid\tblocked_user\tblocking_pid\tblocking_user\twait_seconds\twait_event_type\twait_event\tblocked_query\tblocking_query' ;;
      vacuum_progress.tsv) printf 'pid\tdatabase_name\trelation\tphase\theap_blks_total\theap_blks_scanned\theap_blks_vacuumed\tindex_vacuum_count' ;;
      stat_replication.tsv) printf 'application_name\tclient_addr\tstate\tsync_state\treply_time\treplay_lag_bytes\treplay_lag_seconds' ;;
      wal_receiver.tsv) printf 'status\tsender_host\tsender_port\tslot_name\tlatest_end_lsn\tlatest_end_time' ;;
      replication_slots.tsv) printf 'slot_name\tslot_type\tactive\tdatabase\trestart_lsn\tconfirmed_flush_lsn\tretained_bytes' ;;
      prepared_xacts.tsv) printf 'transaction\tgid\tprepared\towner\tdatabase' ;;
      stat_wal.tsv) printf 'wal_records\twal_fpi\twal_bytes\twal_buffers_full\twal_write\twal_sync\twal_write_time\twal_sync_time\tstats_reset' ;;
      stat_io.tsv) printf 'backend_type\tobject\tcontext\treads\tread_time\twrites\twrite_time\textends\textend_time\top_bytes\thits\tevictions\treuses\tfsyncs\tfsync_time\tstats_reset' ;;
      checkpointer.tsv) printf 'num_timed\tnum_requested\trestartpoints_timed\trestartpoints_req\trestartpoints_done\twrite_time\tsync_time\tbuffers_written\tstats_reset' ;;
      archiver.tsv) printf 'archived_count\tlast_archived_wal\tlast_archived_time\tfailed_count\tlast_failed_wal\tlast_failed_time\tstats_reset' ;;
      database_conflicts.tsv) printf 'database_name\tconfl_tablespace\tconfl_lock\tconfl_snapshot\tconfl_bufferpin\tconfl_deadlock' ;;
      no_primary_key.tsv) printf 'schemaname\ttable_name\ttotal_bytes\testimated_rows' ;;
      pg_stat_statements.tsv) printf 'queryid\tquery\tcalls\ttotal_exec_ms\tmean_exec_ms\trows\tshared_blks_hit\tshared_blks_read\ttemp_blks_written' ;;
      object_counts.tsv) printf 'object_type\tcount' ;;
      duplicate_indexes.tsv) printf 'schemaname\ttable_name\tindex_def\tduplicate_count\tindex_bytes' ;;
      db_role_settings.tsv) printf 'database\trole\tparameter_value' ;;
      bloat_estimation.tsv) printf 'schemaname\ttable_name\ttotal_bytes\ttable_bytes\tindex_bytes\tn_live_tup\tn_dead_tup\tdead_pct\testimated_dead_bytes\tlast_vacuum\tlast_autovacuum' ;;
      partition_summary.tsv) printf 'schemaname\tpartitioned_table\tpartition_count\ttotal_size\ttotal_bytes' ;;
      unused_indexes.tsv) printf 'schemaname\ttable_name\tindex_name\tindex_size\tidx_scan' ;;
      error_log_summary.tsv) printf 'category\tcount' ;;
      *) return 1 ;;
    esac
}

ensure_tsv_header() {
    local file="$1" header
    [ -s "$file" ] && return 0
    header=$(known_tsv_header "$file") || return 1
    printf '%s\n' "$header" > "$file"
}

# ============ 状态记录 ============

record_status() {
    local item_id="$1" category="$2" status="$3" started_at="$4" finished_at="$5" duration_ms="$6"
    local row_count="$7" exit_code="$8" output_file="$9" reason="${10-}"
    local f
    f="$STATUS_PARTS_DIR/$(sanitize_id "$item_id").tsv"
    printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
      "$item_id" "$category" "$status" "$started_at" "$finished_at" "$duration_ms" "$row_count" "$exit_code" \
      "$output_file" "$(sanitize_text "$reason")" > "$f"
}

record_skipped() {
    local item_id="$1" category="$2" status="$3" reason="$4" outfile="${5-}" output_file=""
    if [ -n "$outfile" ]; then
        mkdir -p "$(dirname "$outfile")"
        ensure_tsv_header "$outfile" || : > "$outfile"
        output_file=$(safe_relpath "$outfile")
    fi
    record_status "$item_id" "$category" "$status" "$(iso_now)" "$(iso_now)" 0 0 0 "$output_file" "$reason"
}

# ============ 采集原语 ============

capture_command() {
    local item_id="$1" category="$2" outfile="$3"; shift 3
    local start_iso end_iso start_ms end_ms rc status rows reason err
    start_iso=$(iso_now); start_ms=$(epoch_ms); err="$TMP_DIR/$(sanitize_id "$item_id").stderr"
    mkdir -p "$(dirname "$outfile")"
    "$@" > "$outfile" 2> "$err"; rc=$?
    end_iso=$(iso_now); end_ms=$(epoch_ms)
    status="ok"; reason=""
    if [ "$rc" -ne 0 ]; then
        if [ ! -s "$err" ] && [ -s "$outfile" ]; then
            head -c 500 "$outfile" >> "$err" 2>/dev/null
        fi
        status=$(classify_failure "$rc" "$err"); reason=$(tail -n 5 "$err" 2>/dev/null | tr '\n' ' ')
        : > "$outfile"
    elif [ ! -s "$outfile" ]; then status="empty"; fi
    rows=$(wc -l < "$outfile" 2>/dev/null | tr -d ' '); rows=${rows:-0}
    cat "$err" >> "$MODULE_LOG_DIR/$(sanitize_id "$category").log" 2>/dev/null
    rm -f "$err"
    record_status "$item_id" "$category" "$status" "$start_iso" "$end_iso" "$((end_ms-start_ms))" "$rows" "$rc" "$(safe_relpath "$outfile")" "$reason"
    return "$rc"
}

pg_exec() {
    run_with_timeout "$PG_TIMEOUT_SECONDS" psql -X --no-psqlrc "${PG_CONN_ARGS[@]}" "$@"
}

pg_scalar() {
    pg_exec -At -c "$1" 2>/dev/null | head -n 1 | tr -d '\r'
}

pg_scalar_db() {
    local db="$1" query="$2"
    pg_exec -d "$db" -At -c "$query" 2>/dev/null | head -n 1 | tr -d '\r'
}

pg_query_tsv() {
    local item_id="$1" category="$2" outfile="$3" query="$4"
    local start_iso end_iso start_ms end_ms rc status rows reason err
    start_iso=$(iso_now); start_ms=$(epoch_ms); err="$TMP_DIR/$(sanitize_id "$item_id").stderr"
    mkdir -p "$(dirname "$outfile")"
    pg_exec -A -F $'\t' -c "$query" > "$outfile" 2> "$err"; rc=$?
    end_iso=$(iso_now); end_ms=$(epoch_ms)
    status="ok"; reason=""
    if [ "$rc" -ne 0 ]; then
        status=$(classify_failure "$rc" "$err")
        reason=$(tail -n 5 "$err" 2>/dev/null | tr '\n' ' ')
        : > "$outfile"
        ensure_tsv_header "$outfile" || true
        rows=0
    else
        rows=$(awk 'END{print (NR>0?NR-1:0)}' "$outfile" 2>/dev/null); rows=${rows:-0}
        if [ "$rows" -eq 0 ]; then
            status="empty"
            case "$outfile" in
              *.tsv) [ -s "$outfile" ] || ensure_tsv_header "$outfile" || printf '# no rows returned\n' > "$outfile" ;;
            esac
        fi
    fi
    cat "$err" >> "$MODULE_LOG_DIR/$(sanitize_id "$category").log" 2>/dev/null
    rm -f "$err"
    record_status "$item_id" "$category" "$status" "$start_iso" "$end_iso" "$((end_ms-start_ms))" "$rows" "$rc" "$(safe_relpath "$outfile")" "$reason"
    return "$rc"
}

pg_query_tsv_db() {
    local db="$1" item_id="$2" category="$3" outfile="$4" query="$5"
    local start_iso end_iso start_ms end_ms rc status rows reason err
    start_iso=$(iso_now); start_ms=$(epoch_ms); err="$TMP_DIR/$(sanitize_id "$item_id").stderr"
    mkdir -p "$(dirname "$outfile")"
    pg_exec -d "$db" -A -F $'\t' -c "$query" > "$outfile" 2> "$err"; rc=$?
    end_iso=$(iso_now); end_ms=$(epoch_ms)
    status="ok"; reason=""
    if [ "$rc" -ne 0 ]; then
        status=$(classify_failure "$rc" "$err")
        reason=$(tail -n 5 "$err" 2>/dev/null | tr '\n' ' ')
        : > "$outfile"
        ensure_tsv_header "$outfile" || true
        rows=0
    else
        rows=$(awk 'END{print (NR>0?NR-1:0)}' "$outfile" 2>/dev/null); rows=${rows:-0}
        if [ "$rows" -eq 0 ]; then status="empty"; fi
    fi
    cat "$err" >> "$MODULE_LOG_DIR/$(sanitize_id "$category").log" 2>/dev/null
    rm -f "$err"
    record_status "$item_id" "$category" "$status" "$start_iso" "$end_iso" "$((end_ms-start_ms))" "$rows" "$rc" "$(safe_relpath "$outfile")" "$reason"
    return "$rc"
}

pg_table_exists() {
    local schema="$1" table="$2" n
    n=$(pg_scalar "SELECT COUNT(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='${schema}' AND c.relname='${table}'")
    [ "${n:-0}" -gt 0 ] 2>/dev/null
}

# ============ 模块系统 ============

run_module() {
    local module_id="$1"; shift
    local start_iso start_ms end_iso end_ms rc status reason
    start_iso=$(iso_now); start_ms=$(epoch_ms)
    log_info "开始模块: $module_id"
    "$@" >> "$MODULE_LOG_DIR/$(sanitize_id "$module_id").log" 2>&1; rc=$?
    end_iso=$(iso_now); end_ms=$(epoch_ms)
    status="ok"; reason=""
    if [ "$rc" -eq 2 ]; then status="partial"; reason="module completed partially"
    elif [ "$rc" -ne 0 ]; then status="error"; reason="module returned non-zero"; fi
    record_status "module.$module_id" "module" "$status" "$start_iso" "$end_iso" "$((end_ms-start_ms))" "0" "$rc" "logs/modules/$(sanitize_id "$module_id").log" "$reason"
    log_info "结束模块: $module_id，耗时 $((end_ms-start_ms)) ms，状态 $status"
    return "$rc"
}

start_module_bg() {
    local module_id="$1"; shift
    BG_NAMES+=("$module_id")
    BG_START_ISO+=("$(iso_now)")
    BG_START_MS+=("$(epoch_ms)")
    run_module "$module_id" "$@" &
    BG_PIDS+=("$!")
}

wait_background_modules() {
    local i rc item_file end_iso end_ms duration reason
    for ((i=0; i<${#BG_PIDS[@]}; i++)); do
        wait "${BG_PIDS[$i]}"; rc=$?
        if [ "$rc" -ne 0 ]; then
            log_warn "后台模块 ${BG_NAMES[$i]} 返回 $rc"
            item_file="$STATUS_PARTS_DIR/$(sanitize_id "module.${BG_NAMES[$i]}").tsv"
            if [ ! -s "$item_file" ]; then
                end_iso=$(iso_now); end_ms=$(epoch_ms); duration=$((end_ms-BG_START_MS[$i]))
                reason="background module terminated before status was recorded"
                record_status "module.${BG_NAMES[$i]}" "module" "error" "${BG_START_ISO[$i]}" "$end_iso" "$duration" 0 "$rc" "logs/modules/$(sanitize_id "${BG_NAMES[$i]}").log" "$reason"
            fi
        fi
    done
}

check_output_space() {
    local free_kb
    free_kb=$(df -Pk "$OUTPUT_PARENT" 2>/dev/null | awk 'NR==2{print $4}')
    if ! is_uint "$free_kb"; then log_warn "无法确认输出目录剩余空间"; return 0; fi
    if [ "$free_kb" -lt $((MIN_FREE_MB*1024)) ]; then
        printf '输出目录剩余空间不足：需要至少 %s MB，当前约 %s MB\n' "$MIN_FREE_MB" "$((free_kb/1024))" >&2
        exit 11
    fi
}

# ============ 系统静态信息采集 ============

collect_system_static() {
    capture_command "system.os_release" "system.static" "$EVIDENCE_DIR/os_release.txt" bash -c 'cat /etc/os-release 2>/dev/null; uname -a; uptime 2>/dev/null' || true
    capture_command "system.time_status" "system.static" "$EVIDENCE_DIR/time_status.txt" bash -c 'printf "local_time="; date --iso-8601=seconds 2>/dev/null || date +%Y-%m-%dT%H:%M:%S%z; printf "utc_time="; date -u --iso-8601=seconds 2>/dev/null || date -u +%Y-%m-%dT%H:%M:%SZ; printf "timezone="; cat /etc/timezone 2>/dev/null || timedatectl show -p Timezone --value 2>/dev/null || date +%Z; printf "epoch_seconds="; date +%s' || true
    if has_cmd timedatectl; then capture_command "system.timedatectl" "system.static" "$EVIDENCE_DIR/timedatectl.txt" timedatectl status || true; fi
    if has_cmd chronyc; then
        capture_command "system.chronyc_tracking" "system.static" "$EVIDENCE_DIR/chronyc_tracking.txt" chronyc tracking || true
        capture_command "system.chronyc_sources" "system.static" "$EVIDENCE_DIR/chronyc_sources.txt" chronyc sources -v || true
    elif has_cmd ntpq; then
        capture_command "system.ntpq_peers" "system.static" "$EVIDENCE_DIR/ntpq_peers.txt" ntpq -pn || true
    fi
    has_cmd lscpu && capture_command "system.lscpu" "system.static" "$EVIDENCE_DIR/lscpu.txt" lscpu || record_status "system.lscpu" "system.static" "unsupported" "$(iso_now)" "$(iso_now)" 0 0 127 "" "lscpu not installed"
    has_cmd free && capture_command "system.free" "system.static" "$TABLES_DIR/memory_snapshot.tsv" free -b || true
    has_cmd df && capture_command "system.filesystems" "system.static" "$TABLES_DIR/filesystems.tsv" df -PT -x fuse.gvfsd-fuse -x fuse.gvfs-fuse-daemon || true
    has_cmd df && capture_command "system.inodes" "system.static" "$TABLES_DIR/inodes.tsv" df -Pi -x fuse.gvfsd-fuse -x fuse.gvfs-fuse-daemon || true
    has_cmd lsblk && capture_command "system.block_devices" "system.static" "$TABLES_DIR/block_devices.tsv" lsblk -b -o NAME,KNAME,TYPE,SIZE,FSTYPE,MOUNTPOINT,ROTA,SCHED,MODEL,SERIAL || true
    has_cmd mount && capture_command "system.mounts" "system.static" "$EVIDENCE_DIR/mounts.txt" mount || true
    if has_cmd ip; then
        capture_command "system.ip_address" "system.static" "$EVIDENCE_DIR/ip_address.txt" ip -details addr show || true
        capture_command "system.ip_route" "system.static" "$EVIDENCE_DIR/ip_route.txt" ip route show table all || true
    elif has_cmd ifconfig; then
        capture_command "system.ifconfig" "system.static" "$EVIDENCE_DIR/ip_address.txt" ifconfig -a || true
    else
        record_status "system.ip_address" "system.static" "unsupported" "$(iso_now)" "$(iso_now)" 0 0 127 "" "ip/ifconfig not installed"
    fi
    has_cmd ss && capture_command "system.socket_summary" "system.static" "$EVIDENCE_DIR/socket_summary.txt" ss -s || true
    has_cmd numactl && capture_command "system.numa" "system.static" "$EVIDENCE_DIR/numa.txt" numactl --hardware || true
    has_cmd sysctl && capture_command "system.sysctl_selected" "system.static" "$TABLES_DIR/kernel_parameters.tsv" bash -c '
      for k in vm.swappiness vm.dirty_ratio vm.dirty_background_ratio vm.dirty_bytes vm.dirty_background_bytes vm.overcommit_memory vm.zone_reclaim_mode fs.file-max fs.aio-max-nr kernel.shmmax kernel.shmall net.core.somaxconn net.ipv4.tcp_max_syn_backlog; do
        v=$(sysctl -n "$k" 2>/dev/null) && printf "%s\t%s\n" "$k" "$v"
      done
      exit 0' || true
    {
        printf 'path\tvalue\n'
        for f in /sys/kernel/mm/transparent_hugepage/enabled /sys/kernel/mm/transparent_hugepage/defrag /proc/sys/vm/nr_hugepages; do
            [ -r "$f" ] && printf '%s\t%s\n' "$f" "$(tr '\n' ' ' < "$f")"
        done
    } > "$TABLES_DIR/hugepages.tsv"
    record_status "system.hugepages" "system.static" "ok" "$(iso_now)" "$(iso_now)" 0 "$(awk 'END{print NR-1}' "$TABLES_DIR/hugepages.tsv")" 0 "tables/hugepages.tsv" ""
    if has_cmd dmesg; then
        capture_command "system.dmesg_errors" "system.static" "$EVIDENCE_DIR/dmesg_errors.txt" dmesg --level=err,crit,alert,emerg 2>/dev/null || true
    fi
    if has_cmd ps; then
        ps -eo pid,ppid,user,etimes,%cpu,%mem,args 2>/dev/null | awk 'BEGIN{IGNORECASE=1} /[p]ostgres/ {print}' > "$EVIDENCE_DIR/postgres_processes.txt"
        record_status "system.postgres_processes" "system.static" "$([ -s "$EVIDENCE_DIR/postgres_processes.txt" ] && echo ok || echo empty)" "$(iso_now)" "$(iso_now)" 0 "$(wc -l < "$EVIDENCE_DIR/postgres_processes.txt")" 0 "evidence/postgres_processes.txt" ""
    fi
    if has_cmd iostat; then
        capture_command "system.iostat" "system.static" "$TABLES_DIR/iostat_snapshot.tsv" iostat -xz 1 3 || true
    fi
}

# ============ PG 能力探测 ============

probe_capabilities() {
    PG_VERSION=$(pg_scalar 'SELECT VERSION()')
    PG_VERSION_NUM=$(pg_scalar "SELECT current_setting('server_version_num')::int")
    PG_MAJOR=$(printf '%s' "$PG_VERSION_NUM" | awk '{print int($1/10000)}')
    if [ "${PG_MAJOR:-0}" -le 12 ]; then
        pgss_total_col="total_time"; pgss_mean_col="mean_time"
    else
        pgss_total_col="total_exec_time"; pgss_mean_col="mean_exec_time"
    fi
    PG_DATA_DIR=$(pg_scalar "SHOW data_directory")
    PG_IS_RECOVERY=$(pg_scalar 'SELECT pg_is_in_recovery()')
    WAL_LEVEL=$(pg_scalar "SHOW wal_level")
    ARCHIVE_MODE=$(pg_scalar "SHOW archive_mode")
    MAX_REPLICATION_SLOTS=$(pg_scalar "SHOW max_replication_slots")
    MAX_WAL_SENDERS=$(pg_scalar "SHOW max_wal_senders")

    PGSS_RELATION=$(pg_scalar "SELECT quote_ident(n.nspname)||'.'||quote_ident(c.relname) FROM pg_extension e JOIN pg_namespace n ON n.oid=e.extnamespace JOIN pg_class c ON c.relnamespace=n.oid AND c.relname='pg_stat_statements' WHERE e.extname='pg_stat_statements' LIMIT 1")
    STAT_STATEMENTS_AVAILABLE=0; [ -n "$PGSS_RELATION" ] && STAT_STATEMENTS_AVAILABLE=1
    PGSS_EXTENSION=0; [ "$(pg_scalar "SELECT count(*) FROM pg_extension WHERE extname='pg_stat_statements'")" -gt 0 ] 2>/dev/null && PGSS_EXTENSION=1

    STAT_WAL_AVAILABLE=0; [ "${PG_MAJOR:-0}" -ge 14 ] && pg_table_exists pg_catalog pg_stat_wal && STAT_WAL_AVAILABLE=1
    STAT_IO_AVAILABLE=0; [ "${PG_MAJOR:-0}" -ge 16 ] && pg_table_exists pg_catalog pg_stat_io && STAT_IO_AVAILABLE=1
    STAT_CHECKPOINTER_AVAILABLE=0; [ "${PG_MAJOR:-0}" -ge 17 ] && pg_table_exists pg_catalog pg_stat_checkpointer && STAT_CHECKPOINTER_AVAILABLE=1

    REPLICATION_ACTIVE=0
    [ "$(pg_scalar "SELECT count(*) FROM pg_stat_replication")" -gt 0 ] 2>/dev/null && REPLICATION_ACTIVE=1

    {
      printf 'capability\tvalue\n'
      printf 'pg_version\t%s\n' "$PG_VERSION"
      printf 'pg_version_num\t%s\n' "$PG_VERSION_NUM"
      printf 'pg_major\t%s\n' "$PG_MAJOR"
      printf 'data_directory\t%s\n' "$PG_DATA_DIR"
      printf 'is_in_recovery\t%s\n' "$PG_IS_RECOVERY"
      printf 'wal_level\t%s\n' "$WAL_LEVEL"
      printf 'archive_mode\t%s\n' "$ARCHIVE_MODE"
      printf 'max_replication_slots\t%s\n' "$MAX_REPLICATION_SLOTS"
      printf 'max_wal_senders\t%s\n' "$MAX_WAL_SENDERS"
      printf 'pg_stat_statements\t%s\n' "$STAT_STATEMENTS_AVAILABLE"
      printf 'pg_stat_statements_extension\t%s\n' "$PGSS_EXTENSION"
      printf 'pg_stat_wal\t%s\n' "$STAT_WAL_AVAILABLE"
      printf 'pg_stat_io\t%s\n' "$STAT_IO_AVAILABLE"
      printf 'pg_stat_checkpointer\t%s\n' "$STAT_CHECKPOINTER_AVAILABLE"
      printf 'replication_active\t%s\n' "$REPLICATION_ACTIVE"
      printf 'sar_command\t%s\n' "$HAS_SAR"
      printf 'sadf_command\t%s\n' "$HAS_SADF"
    } > "$TABLES_DIR/capabilities.tsv"
    record_status "pg.capability_probe" "pg.capabilities" "ok" "$(iso_now)" "$(iso_now)" 0 "$(awk 'END{print NR-1}' "$TABLES_DIR/capabilities.tsv")" 0 "tables/capabilities.tsv" ""
}

# ============ SAR 历史采集 ============

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
        sadf -d "$f" -- "$@" >> "$outfile" 2>> "$MODULE_LOG_DIR/sar_history.log"; rc=$?
        [ "$rc" -eq 0 ] && any=1
    done < "$SAR_FILE_LIST"
    if [ "$any" -eq 1 ] && [ -s "$outfile" ]; then return 0; fi
    return 1
}

compute_sar_coverage() {
    local src="$HISTORY_DIR/sar_cpu.csv" t e cutoff points_file first_epoch="" last_epoch="" first_ts="" last_ts=""
    local coverage_seconds=0 coverage_hours=0 status="empty" point_count=0 discontinuity_count=0 max_gap_seconds=0
    [ -s "$src" ] || return 0
    cutoff=$(( $(date +%s) - SAR_HISTORY_HOURS*3600 ))
    points_file="$TMP_DIR/sar_coverage_epochs.txt"; : > "$points_file"
    while IFS= read -r t; do
        [ -n "$t" ] || continue
        e=$(date -d "$t" +%s 2>/dev/null) || continue
        [ "$e" -ge "$cutoff" ] && printf '%s\n' "$e" >> "$points_file"
    done < <(awk -F';' '!/^#/ && NF>=3 && $3 ~ /^[0-9][0-9][0-9][0-9]-/ {print $3}' "$src")
    sort -nu "$points_file" -o "$points_file"
    point_count=$(wc -l < "$points_file" | tr -d ' '); point_count=${point_count:-0}
    first_epoch=$(head -n 1 "$points_file" 2>/dev/null); last_epoch=$(tail -n 1 "$points_file" 2>/dev/null)
    if [ -n "$first_epoch" ] && [ -n "$last_epoch" ]; then
        read -r coverage_seconds discontinuity_count max_gap_seconds < <(awk '
          NR==1{prev=$1; next}
          {gap=$1-prev; if(gap>maxgap)maxgap=gap; if(gap>0 && gap<=3600)covered+=gap; else if(gap>3600)breaks++; prev=$1}
          END{print covered+0,breaks+0,maxgap+0}' "$points_file")
        coverage_hours=$(awk -v s="$coverage_seconds" 'BEGIN{printf "%.2f",s/3600}')
        first_ts=$(date -u -d "@$first_epoch" '+%Y-%m-%d %H:%M:%S UTC' 2>/dev/null)
        last_ts=$(date -u -d "@$last_epoch" '+%Y-%m-%d %H:%M:%S UTC' 2>/dev/null)
        status="partial"
        [ "$coverage_seconds" -lt $((SAR_HISTORY_HOURS*3600*9/10)) ] && status="partial"
        [ "$coverage_seconds" -ge $((SAR_HISTORY_HOURS*3600*9/10)) ] && status="ok"
    fi
    {
      printf 'status\t%s\n' "$status"
      printf 'requested_hours\t%s\n' "$SAR_HISTORY_HOURS"
      printf 'first_timestamp\t%s\n' "$first_ts"
      printf 'last_timestamp\t%s\n' "$last_ts"
      printf 'coverage_seconds\t%s\n' "$coverage_seconds"
      printf 'coverage_hours\t%s\n' "$coverage_hours"
      printf 'point_count\t%s\n' "$point_count"
      printf 'discontinuity_count\t%s\n' "$discontinuity_count"
      printf 'max_gap_seconds\t%s\n' "$max_gap_seconds"
    } > "$HISTORY_DIR/coverage.tsv"
    {
      printf '{"schema_version":"1.1","status":%s,"requested_hours":%s,"first_timestamp":%s,"last_timestamp":%s,"coverage_seconds":%s,"coverage_hours":%s,"point_count":%s,"discontinuity_count":%s,"max_gap_seconds":%s}\n' \
        "$(json_quote "$status")" "$SAR_HISTORY_HOURS" "$(json_quote "$first_ts")" "$(json_quote "$last_ts")" "$coverage_seconds" "${coverage_hours:-0}" "$point_count" "$discontinuity_count" "$max_gap_seconds"
    } > "$HISTORY_DIR/coverage.json"
}

collect_sar_history() {
    local start_iso start_ms end_iso end_ms status reason count
    start_iso=$(iso_now); start_ms=$(epoch_ms)
    SAR_FILE_LIST="$TMP_DIR/sar_files.txt"; find_sar_files > "$SAR_FILE_LIST"
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
        collect_sadf_metric process "$HISTORY_DIR/sar_process.csv" -w || true
        compute_sar_coverage
        local coverage_status coverage_hours
        coverage_status=$(awk -F'\t' '$1=="status"{print $2}' "$HISTORY_DIR/coverage.tsv" 2>/dev/null)
        coverage_hours=$(awk -F'\t' '$1=="coverage_hours"{print $2}' "$HISTORY_DIR/coverage.tsv" 2>/dev/null)
        [ "$coverage_status" = "partial" ] && status="partial" || status="ok"
        reason="raw sadf data exported; approximate CPU history coverage=${coverage_hours:-unknown} hours"
    fi
    end_iso=$(iso_now); end_ms=$(epoch_ms)
    record_status "system.sar_history" "system.history" "$status" "$start_iso" "$end_iso" "$((end_ms-start_ms))" "${count:-0}" 0 "history/" "$reason"
    return 0
}

# ============ 实时时序采样 ============

_read_cpu_stat() { awk '/^cpu /{print $2,$3,$4,$5,$6,$7,$8,$9; exit}' /proc/stat 2>/dev/null; }

append_cpu_sample() {
    local ts="$1" elapsed_ms="$2" cur user nice system idle iowait irq softirq steal total idleall dtotal didle
    cur=$(_read_cpu_stat) || return 0
    read -r user nice system idle iowait irq softirq steal <<< "$cur"
    total=$((user+nice+system+idle+iowait+irq+softirq+steal)); idleall=$((idle+iowait))
    if [ -n "${PREV_CPU_TOTAL:-}" ]; then
        dtotal=$((total-PREV_CPU_TOTAL)); didle=$((idleall-PREV_CPU_IDLE))
        awk -v ts="$ts" -v em="$elapsed_ms" -v dt="$dtotal" -v du="$((user-PREV_CPU_USER))" -v ds="$((system-PREV_CPU_SYSTEM))" -v dw="$((iowait-PREV_CPU_IOWAIT))" -v dst="$((steal-PREV_CPU_STEAL))" -v di="$didle" 'BEGIN{if(dt>0) printf "%s,%s,%.4f,%.4f,%.4f,%.4f,%.4f,%.4f\n",ts,em,du*100/dt,ds*100/dt,dw*100/dt,dst*100/dt,di*100/dt,(dt-di)*100/dt}' >> "$CPU_CSV"
    fi
    PREV_CPU_TOTAL=$total; PREV_CPU_IDLE=$idleall; PREV_CPU_USER=$user; PREV_CPU_SYSTEM=$system; PREV_CPU_IOWAIT=$iowait; PREV_CPU_STEAL=$steal
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
        if [ -n "${PREV_NET_RX[$iface]+x}" ]; then
            drx=$((rx-PREV_NET_RX[$iface])); dtx=$((tx-PREV_NET_TX[$iface]))
            awk -v ts="$ts" -v em="$elapsed_ms" -v i="$iface" -v r="$drx" -v t="$dtx" -v dm="$delta_ms" 'BEGIN{if(dm<=0)dm=1; printf "%s,%s,%s,%.2f,%.2f,%d,%d\n",ts,em,i,r*1000/dm,t*1000/dm,r,t}' >> "$NET_CSV"
        fi
        PREV_NET_RX[$iface]=$rx; PREV_NET_TX[$iface]=$tx
    done
}

append_disk_sample() {
    local ts="$1" elapsed_ms="$2" delta_ms="$3" p dev s ri rs rt wi ws wt inflight io_ms weighted sector_size
    local dri drs drt dwi dws dwt dio dweighted
    for p in /sys/block/*; do
        [ -r "$p/stat" ] || continue; dev=${p##*/}; case "$dev" in loop*|ram*|zram*|fd*|sr*|dm-*) continue;; esac
        s=$(cat "$p/stat" 2>/dev/null) || continue
        read -r ri rs rt wi ws wt inflight io_ms weighted < <(awk '{print $1,$3,$4,$5,$7,$8,$9,$10,$11}' "$p/stat" 2>/dev/null)
        for v in "$ri" "$rs" "$rt" "$wi" "$ws" "$wt" "$inflight" "$io_ms" "$weighted"; do
            case "$v" in ''|*[!0-9]*) continue 2 ;; esac
        done
        sector_size=$(cat "$p/queue/hw_sector_size" 2>/dev/null || echo 512)
        case "$sector_size" in ''|*[!0-9]*) sector_size=512 ;; esac
        if [ -n "${PREV_DISK_RSECT[$dev]+x}" ]; then
            dri=$((ri-PREV_DISK_RIOS[$dev])); drs=$((rs-PREV_DISK_RSECT[$dev])); drt=$((rt-PREV_DISK_RTICKS[$dev]))
            dwi=$((wi-PREV_DISK_WIOS[$dev])); dws=$((ws-PREV_DISK_WSECT[$dev])); dwt=$((wt-PREV_DISK_WTICKS[$dev]))
            dio=$((io_ms-PREV_DISK_IOTICKS[$dev])); dweighted=$((weighted-PREV_DISK_WEIGHTED[$dev]))
            awk -v ts="$ts" -v em="$elapsed_ms" -v d="$dev" -v dm="$delta_ms" -v bs="$sector_size" -v ri="$dri" -v rs="$drs" -v rt="$drt" -v wi="$dwi" -v ws="$dws" -v wt="$dwt" -v io="$dio" -v wq="$dweighted" 'BEGIN{if(dm<=0)dm=1; printf "%s,%s,%s,%.2f,%.2f,%.2f,%.2f,%.4f,%.4f,%.4f,%.4f\n",ts,em,d,rs*bs*1000/dm,ws*bs*1000/dm,ri*1000/dm,wi*1000/dm,(ri>0?rt/ri:0),(wi>0?wt/wi:0),io*100/dm,wq/dm}' >> "$DISK_CSV"
        fi
        PREV_DISK_RIOS[$dev]=$ri; PREV_DISK_RSECT[$dev]=$rs; PREV_DISK_RTICKS[$dev]=$rt; PREV_DISK_WIOS[$dev]=$wi; PREV_DISK_WSECT[$dev]=$ws; PREV_DISK_WTICKS[$dev]=$wt; PREV_DISK_IOTICKS[$dev]=$io_ms; PREV_DISK_WEIGHTED[$dev]=$weighted
    done
}

append_pg_activity_sample() {
    local ts="$1" elapsed_ms="$2"
    pg_exec -At -F ',' -c "SELECT count(*) AS total, count(*) FILTER (WHERE state='active') AS active, count(*) FILTER (WHERE state='idle in transaction') AS idle_in_xact, count(*) FILTER (WHERE wait_event IS NOT NULL) AS waiting FROM pg_stat_activity" 2>> "$MODULE_LOG_DIR/realtime_sampling.log" \
    | awk -v ts="$ts" -v em="$elapsed_ms" -F',' '{printf "%s,%s,%s,%s,%s,%s\n",ts,em,$1,$2,$3,$4}' >> "$PG_ACTIVITY_CSV" || true

    pg_exec -At -F ',' -c "SELECT coalesce(sum(xact_commit),0),coalesce(sum(xact_rollback),0),coalesce(sum(blks_read),0),coalesce(sum(blks_hit),0),coalesce(sum(tup_returned),0),coalesce(sum(tup_fetched),0),coalesce(sum(tup_inserted),0),coalesce(sum(tup_updated),0),coalesce(sum(tup_deleted),0),coalesce(sum(temp_files),0),coalesce(sum(temp_bytes),0),coalesce(sum(deadlocks),0) FROM pg_stat_database" 2>> "$MODULE_LOG_DIR/realtime_sampling.log" \
    | awk -v ts="$ts" -v em="$elapsed_ms" -F',' '{printf "%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s\n",ts,em,$1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12}' >> "$PG_STATS_CSV" || true
}

collect_realtime_samples() {
    printf 'timestamp,elapsed_ms,user_pct,system_pct,iowait_pct,steal_pct,idle_pct,busy_pct\n' > "$CPU_CSV"
    printf 'timestamp,elapsed_ms,mem_total_bytes,mem_available_bytes,mem_used_pct,swap_total_bytes,swap_used_bytes,cached_bytes,dirty_writeback_bytes\n' > "$MEM_CSV"
    printf 'timestamp,elapsed_ms,interface,rx_bytes_per_sec,tx_bytes_per_sec,rx_bytes_delta,tx_bytes_delta\n' > "$NET_CSV"
    printf 'timestamp,elapsed_ms,device,read_bytes_per_sec,write_bytes_per_sec,read_iops,write_iops,read_await_ms,write_await_ms,util_pct,avg_queue_size\n' > "$DISK_CSV"
    printf 'timestamp,elapsed_ms,total_sessions,active_sessions,idle_in_xact,waiting\n' > "$PG_ACTIVITY_CSV"
    printf 'timestamp,elapsed_ms,xact_commit,xact_rollback,blks_read,blks_hit,tup_returned,tup_fetched,tup_inserted,tup_updated,tup_deleted,temp_files,temp_bytes,deadlocks\n' > "$PG_STATS_CSV"

    declare -gA PREV_NET_RX PREV_NET_TX PREV_DISK_RIOS PREV_DISK_RSECT PREV_DISK_RTICKS PREV_DISK_WIOS PREV_DISK_WSECT PREV_DISK_WTICKS PREV_DISK_IOTICKS PREV_DISK_WEIGHTED
    local sampling_started_at sampling_started_ms sampling_finished_at sampling_finished_ms
    local start_mono prev_mono now_mono elapsed delta ts i pg_fail=0
    sampling_started_at=$(iso_now); sampling_started_ms=$(epoch_ms)
    start_mono=$(monotonic_ms); prev_mono=$start_mono; ts=$(iso_now)
    append_cpu_sample "$ts" 0; append_memory_sample "$ts" 0; append_network_sample "$ts" 0 1; append_disk_sample "$ts" 0 1; append_pg_activity_sample "$ts" 0 || pg_fail=$((pg_fail+1))
    for ((i=1; i<=SAMPLE_COUNT; i++)); do
        sleep "$SAMPLE_INTERVAL"
        now_mono=$(monotonic_ms); elapsed=$((now_mono-start_mono)); delta=$((now_mono-prev_mono)); ts=$(iso_now)
        append_cpu_sample "$ts" "$elapsed"
        append_memory_sample "$ts" "$elapsed"
        append_network_sample "$ts" "$elapsed" "$delta"
        append_disk_sample "$ts" "$elapsed" "$delta"
        append_pg_activity_sample "$ts" "$elapsed" || pg_fail=$((pg_fail+1))
        prev_mono=$now_mono
        log_info "实时采样 ${i}/${SAMPLE_COUNT}"
    done
    [ "$pg_fail" -eq 0 ] || log_warn "实时采样中 PG 状态读取失败 ${pg_fail} 次"
    local pg_points cpu_points disk_points status reason rc
    pg_points=$(awk 'END{print (NR>0?NR-1:0)}' "$PG_ACTIVITY_CSV" 2>/dev/null); pg_points=${pg_points:-0}
    cpu_points=$(awk 'END{print (NR>0?NR-1:0)}' "$CPU_CSV" 2>/dev/null); cpu_points=${cpu_points:-0}
    disk_points=$(awk 'END{print (NR>0?NR-1:0)}' "$DISK_CSV" 2>/dev/null); disk_points=${disk_points:-0}
    status="ok"; reason="completed ${SAMPLE_COUNT} intervals; pg_points=${pg_points}; cpu_points=${cpu_points}; disk_rows=${disk_points}"; rc=0
    if [ "$pg_points" -lt $((SAMPLE_COUNT+1)) ] || [ "$cpu_points" -lt "$SAMPLE_COUNT" ]; then
        status="partial"; reason="requested ${SAMPLE_COUNT} intervals but collected pg_points=${pg_points}, cpu_points=${cpu_points}, disk_rows=${disk_points}; pg_failures=${pg_fail}"; rc=2
    elif [ "$pg_fail" -gt 0 ]; then
        status="partial"; reason="sampling completed with ${pg_fail} PG status read failures"; rc=2
    fi
    sampling_finished_at=$(iso_now); sampling_finished_ms=$(epoch_ms)
    record_status "timeseries.realtime_sampling" "timeseries" "$status" "$sampling_started_at" "$sampling_finished_at" "$((sampling_finished_ms-sampling_started_ms))" "$pg_points" "$rc" "timeseries/" "$reason"
    return "$rc"
}

# ============ PG 基础采集 ============

collect_pg_basic() {
    pg_query_tsv "pg.instance" "pg.basic" "$TABLES_DIR/instance.tsv" \
      "SELECT version() AS version, current_setting('server_version_num')::int AS version_num, current_database() AS database, current_user AS inspection_user, pg_postmaster_start_time() AS started_at, now()-pg_postmaster_start_time() AS uptime, pg_is_in_recovery() AS is_standby, inet_server_addr()::text AS server_addr, inet_server_port() AS server_port" || true
    pg_query_tsv "pg.control_system" "pg.basic" "$TABLES_DIR/control_system.tsv" \
      "SELECT system_identifier, pg_control_version, catalog_version_no FROM pg_control_system()" || true
    pg_query_tsv "pg.control_checkpoint" "pg.basic" "$TABLES_DIR/control_checkpoint.tsv" \
      "SELECT * FROM pg_control_checkpoint()" || true
    pg_query_tsv "pg.settings" "pg.basic" "$TABLES_DIR/settings.tsv" \
      "SELECT name, setting, unit, source, pending_restart, vartype, context FROM pg_settings ORDER BY name" || true
    pg_query_tsv "pg.file_settings_errors" "pg.basic" "$TABLES_DIR/file_settings_errors.tsv" \
      "SELECT sourcefile, sourceline, name, setting, error FROM pg_file_settings WHERE error IS NOT NULL ORDER BY sourcefile, sourceline" || true
    pg_query_tsv "pg.hba_rules" "pg.basic" "$TABLES_DIR/hba_rules.tsv" \
      "SELECT line_number, type, database, user_name, address, netmask, auth_method, options, error FROM pg_hba_file_rules ORDER BY line_number" || true
    pg_query_tsv "pg.databases" "pg.basic" "$TABLES_DIR/databases.tsv" \
      "SELECT datname AS database_name, pg_database_size(oid) AS size_bytes, datconnlimit AS connection_limit, age(datfrozenxid) AS xid_age, pg_encoding_to_char(encoding) AS encoding, datcollate AS collation FROM pg_database WHERE datallowconn ORDER BY pg_database_size(oid) DESC" || true
    pg_query_tsv "pg.database_ages" "pg.basic" "$TABLES_DIR/database_ages.tsv" \
      "SELECT datname AS database_name, age(datfrozenxid) AS xid_age, mxid_age(datminmxid) AS multixact_age FROM pg_database WHERE datallowconn ORDER BY greatest(age(datfrozenxid),mxid_age(datminmxid)) DESC" || true
    pg_query_tsv "pg.extensions" "pg.basic" "$TABLES_DIR/extensions.tsv" \
      "SELECT extname AS extension_name, extversion AS version FROM pg_extension ORDER BY extname" || true
    pg_query_tsv "pg.tablespaces" "pg.basic" "$TABLES_DIR/tablespaces.tsv" \
      "SELECT spcname AS tablespace, pg_tablespace_location(oid) AS location, pg_tablespace_size(oid) AS size_bytes FROM pg_tablespace ORDER BY pg_tablespace_size(oid) DESC" || true
    pg_query_tsv "pg.roles" "pg.basic" "$TABLES_DIR/roles.tsv" \
      "SELECT rolname AS role_name, rolsuper AS is_superuser, rolcreaterole, rolcreatedb, rolcanlogin, rolreplication, rolconnlimit, rolvaliduntil FROM pg_roles ORDER BY rolname" || true
}

# ============ PG 性能采集 ============

collect_pg_performance() {
    pg_query_tsv "pg.connections" "pg.performance" "$TABLES_DIR/connections.tsv" \
      "SELECT current_setting('max_connections')::int AS max_connections, current_setting('superuser_reserved_connections')::int AS reserved_connections, count(*) AS current_connections, count(*) FILTER (WHERE state='active') AS active_connections, count(*) FILTER (WHERE state='idle in transaction') AS idle_in_transaction FROM pg_stat_activity" || true
    pg_query_tsv "pg.session_states" "pg.performance" "$TABLES_DIR/session_states.tsv" \
      "SELECT coalesce(state,'unknown') AS state, count(*) AS sessions FROM pg_stat_activity GROUP BY 1 ORDER BY 2 DESC" || true
    pg_query_tsv "pg.long_transactions" "pg.performance" "$TABLES_DIR/long_transactions.tsv" \
      "SELECT pid, usename AS username, datname AS database_name, client_addr::text, state, extract(epoch FROM now()-xact_start)::bigint AS duration_seconds, wait_event_type, wait_event, left(query,300) AS query FROM pg_stat_activity WHERE xact_start IS NOT NULL AND now()-xact_start > interval '5 minutes' AND pid<>pg_backend_pid() ORDER BY xact_start" || true
    pg_query_tsv "pg.idle_transactions" "pg.performance" "$TABLES_DIR/idle_transactions.tsv" \
      "SELECT pid, usename AS username, datname AS database_name, client_addr::text, extract(epoch FROM now()-xact_start)::bigint AS duration_seconds, left(query,300) AS query FROM pg_stat_activity WHERE state='idle in transaction' AND now()-xact_start > interval '5 minutes' ORDER BY xact_start" || true
    pg_query_tsv "pg.lock_waits" "pg.performance" "$TABLES_DIR/lock_waits.tsv" \
      "SELECT blocked.pid AS blocked_pid, blocked.usename AS blocked_user, blocker.pid AS blocking_pid, blocker.usename AS blocking_user, extract(epoch FROM now()-blocked.query_start)::bigint AS wait_seconds, blocked.wait_event_type, blocked.wait_event, left(blocked.query,240) AS blocked_query, left(blocker.query,240) AS blocking_query FROM pg_stat_activity blocked CROSS JOIN LATERAL unnest(pg_blocking_pids(blocked.pid)) AS x(pid) JOIN pg_stat_activity blocker ON blocker.pid=x.pid ORDER BY blocked.query_start" || true
    pg_query_tsv "pg.database_stats" "pg.performance" "$TABLES_DIR/database_stats.tsv" \
      "SELECT datname AS database_name, numbackends, xact_commit, xact_rollback, blks_read, blks_hit, tup_returned, tup_fetched, tup_inserted, tup_updated, tup_deleted, temp_files, temp_bytes, deadlocks, blk_read_time, blk_write_time, stats_reset FROM pg_stat_database WHERE datname IS NOT NULL ORDER BY datname" || true
    pg_query_tsv "pg.database_conflicts" "pg.performance" "$TABLES_DIR/database_conflicts.tsv" \
      "SELECT datname AS database_name, confl_tablespace, confl_lock, confl_snapshot, confl_bufferpin, confl_deadlock FROM pg_stat_database_conflicts ORDER BY datname" || true
    pg_query_tsv "pg.vacuum_progress" "pg.performance" "$TABLES_DIR/vacuum_progress.tsv" \
      "SELECT pid, datname AS database_name, relid::regclass::text AS relation, phase, heap_blks_total, heap_blks_scanned, heap_blks_vacuumed, index_vacuum_count FROM pg_stat_progress_vacuum" || true
    pg_query_tsv "pg.bgwriter" "pg.performance" "$TABLES_DIR/bgwriter.tsv" \
      "SELECT * FROM pg_stat_bgwriter" || true
    pg_query_tsv "pg.table_health" "pg.performance" "$TABLES_DIR/table_health.tsv" \
      "SELECT schemaname, relname AS table_name, n_live_tup, n_dead_tup, CASE WHEN n_live_tup+n_dead_tup=0 THEN 0 ELSE round(n_dead_tup::numeric/(n_live_tup+n_dead_tup),4) END AS dead_tuple_ratio, last_vacuum, last_autovacuum, last_analyze, last_autoanalyze, n_mod_since_analyze, pg_total_relation_size(relid) AS total_bytes FROM pg_stat_user_tables ORDER BY n_dead_tup DESC NULLS LAST LIMIT ${TOP_N}" || true
    pg_query_tsv "pg.large_tables" "pg.performance" "$TABLES_DIR/large_tables.tsv" \
      "SELECT schemaname, relname AS table_name, pg_relation_size(relid) AS table_bytes, pg_indexes_size(relid) AS index_bytes, pg_total_relation_size(relid) AS total_bytes, n_live_tup FROM pg_stat_user_tables ORDER BY pg_total_relation_size(relid) DESC LIMIT 30" || true
    pg_query_tsv "pg.no_primary_key" "pg.performance" "$TABLES_DIR/no_primary_key.tsv" \
      "SELECT n.nspname AS schemaname, c.relname AS table_name, pg_total_relation_size(c.oid) AS total_bytes, c.reltuples::bigint AS estimated_rows FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE c.relkind IN ('r','p') AND n.nspname NOT IN ('pg_catalog','information_schema','pg_toast') AND NOT EXISTS (SELECT 1 FROM pg_constraint x WHERE x.conrelid=c.oid AND x.contype='p') ORDER BY pg_total_relation_size(c.oid) DESC LIMIT ${TOP_N}" || true
    pg_query_tsv "pg.indexes" "pg.performance" "$TABLES_DIR/indexes.tsv" \
      "SELECT s.schemaname, s.relname AS table_name, s.indexrelname AS index_name, s.idx_scan, pg_relation_size(s.indexrelid) AS index_bytes, i.indisvalid, i.indisready, i.indisunique, i.indisprimary FROM pg_stat_user_indexes s JOIN pg_index i ON i.indexrelid=s.indexrelid ORDER BY pg_relation_size(s.indexrelid) DESC LIMIT ${TOP_N}" || true
    pg_query_tsv "pg.sequences" "pg.performance" "$TABLES_DIR/sequences.tsv" \
      "SELECT schemaname, sequencename AS sequence_name, last_value, max_value, CASE WHEN last_value IS NULL OR max_value=0 THEN NULL ELSE round(last_value::numeric/max_value,4) END AS usage_ratio FROM pg_sequences ORDER BY usage_ratio DESC NULLS LAST LIMIT 50" || true
    pg_query_tsv "pg.prepared_xacts" "pg.performance" "$TABLES_DIR/prepared_xacts.tsv" \
      "SELECT transaction, gid, prepared, owner, database FROM pg_prepared_xacts ORDER BY prepared" || true

    pg_query_tsv "pg.object_counts" "pg.performance" "$TABLES_DIR/object_counts.tsv" \
      "SELECT 'table' AS object_type, count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE c.relkind='r' AND n.nspname NOT IN ('pg_catalog','information_schema','pg_toast') UNION ALL SELECT 'partitioned_table', count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE c.relkind='p' AND n.nspname NOT IN ('pg_catalog','information_schema','pg_toast') UNION ALL SELECT 'index', count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE c.relkind='i' AND n.nspname NOT IN ('pg_catalog','information_schema','pg_toast') UNION ALL SELECT 'view', count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE c.relkind='v' AND n.nspname NOT IN ('pg_catalog','information_schema','pg_toast') UNION ALL SELECT 'sequence', count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE c.relkind='S' AND n.nspname NOT IN ('pg_catalog','information_schema','pg_toast') UNION ALL SELECT 'trigger', count(*) FROM pg_trigger WHERE tgisinternal=false UNION ALL SELECT 'function', count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname NOT IN ('pg_catalog','information_schema') UNION ALL SELECT 'extension', count(*) FROM pg_extension UNION ALL SELECT 'materialized_view', count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE c.relkind='m' AND n.nspname NOT IN ('pg_catalog','information_schema','pg_toast') UNION ALL SELECT 'foreign_table', count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE c.relkind='f' AND n.nspname NOT IN ('pg_catalog','information_schema','pg_toast') ORDER BY 2 DESC" || true

    pg_query_tsv "pg.duplicate_indexes" "pg.performance" "$TABLES_DIR/duplicate_indexes.tsv" \
      "WITH idx AS (SELECT n.nspname AS schemaname, t.relname AS table_name, i.indrelid, i.indkey::text AS indkey, i.indcollation::text AS indcollation, i.indclass::text AS indclass, i.indoption::text AS indoption, i.indexprs::text AS indexprs, i.indpred::text AS indpred, i.indnkeyatts, i.indisunique, i.indisprimary, i.indisexclusion, ic.relam, pg_get_indexdef(i.indexrelid) AS index_def, pg_relation_size(i.indexrelid) AS index_bytes FROM pg_index i JOIN pg_class t ON t.oid=i.indrelid JOIN pg_class ic ON ic.oid=i.indexrelid JOIN pg_namespace n ON n.oid=t.relnamespace WHERE i.indisvalid AND i.indisready AND n.nspname NOT IN ('pg_catalog','information_schema','pg_toast')) SELECT schemaname, table_name, min(index_def) AS index_def, count(*) AS duplicate_count, sum(index_bytes) AS index_bytes FROM idx GROUP BY schemaname, table_name, indrelid, indkey, indcollation, indclass, indoption, indexprs, indpred, indnkeyatts, indisunique, indisprimary, indisexclusion, relam HAVING count(*)>1 ORDER BY sum(index_bytes) DESC LIMIT ${TOP_N}" || true

    pg_query_tsv "pg.unused_indexes" "pg.performance" "$TABLES_DIR/unused_indexes.tsv" \
      "SELECT s.schemaname, s.relname AS table_name, s.indexrelname AS index_name, pg_size_pretty(pg_relation_size(s.indexrelid)) AS index_size, s.idx_scan FROM pg_stat_user_indexes s JOIN pg_index i ON i.indexrelid=s.indexrelid CROSS JOIN (SELECT stats_reset FROM pg_stat_database WHERE datname=current_database()) d WHERE s.idx_scan=0 AND i.indisvalid AND i.indisready AND NOT i.indisprimary AND NOT i.indisunique AND NOT i.indisexclusion AND pg_relation_size(s.indexrelid)>65536 AND s.indexrelname NOT LIKE 'pg_toast_%' AND s.schemaname NOT IN ('pg_catalog','information_schema') AND greatest(coalesce(d.stats_reset,'epoch'::timestamptz),pg_postmaster_start_time()) < now()-interval '7 days' ORDER BY pg_relation_size(s.indexrelid) DESC LIMIT ${TOP_N}" || true

    pg_query_tsv "pg.db_role_settings" "pg.performance" "$TABLES_DIR/db_role_settings.tsv" \
      "SELECT coalesce(d.datname,'(all)') AS database, coalesce(r.rolname,'(all)') AS role, unnest(setconfig) AS parameter_value FROM pg_db_role_setting s LEFT JOIN pg_database d ON d.oid=s.setdatabase LEFT JOIN pg_roles r ON r.oid=s.setrole ORDER BY database, role" || true

    pg_query_tsv "pg.bloat_estimation" "pg.performance" "$TABLES_DIR/bloat_estimation.tsv" \
      "SELECT schemaname, relname AS table_name, pg_total_relation_size(relid) AS total_bytes, pg_relation_size(relid) AS table_bytes, pg_indexes_size(relid) AS index_bytes, n_live_tup, n_dead_tup, CASE WHEN n_live_tup+n_dead_tup>0 THEN round(100.0*n_dead_tup/(n_live_tup+n_dead_tup),1) ELSE 0 END AS dead_pct, CASE WHEN n_live_tup+n_dead_tup>0 AND n_live_tup>0 THEN (pg_relation_size(relid)*n_dead_tup/(n_live_tup+n_dead_tup))::bigint ELSE 0 END AS estimated_dead_bytes, last_vacuum, last_autovacuum FROM pg_stat_user_tables WHERE n_dead_tup>0 ORDER BY estimated_dead_bytes DESC LIMIT ${TOP_N}" || true

    pg_query_tsv "pg.partition_summary" "pg.performance" "$TABLES_DIR/partition_summary.tsv" \
      "SELECT n.nspname AS schemaname, p.relname AS partitioned_table, count(c.oid) AS partition_count, pg_size_pretty(sum(pg_total_relation_size(c.oid))) AS total_size, sum(pg_total_relation_size(c.oid)) AS total_bytes FROM pg_class p JOIN pg_namespace n ON n.oid=p.relnamespace JOIN pg_inherits i ON i.inhparent=p.oid JOIN pg_class c ON c.oid=i.inhrelid WHERE p.relkind='p' AND n.nspname NOT IN ('pg_catalog','information_schema','pg_toast') GROUP BY n.nspname,p.relname,p.oid ORDER BY sum(pg_total_relation_size(c.oid)) DESC LIMIT ${TOP_N}" || true

    if [ "$STAT_STATEMENTS_AVAILABLE" -eq 1 ]; then
        if [ "$REDACT_SQL" -eq 1 ]; then
            pg_query_tsv "pg.stat_statements" "pg.performance" "$TABLES_DIR/pg_stat_statements.tsv" \
              "SELECT queryid, NULL::text AS query, calls, round(${pgss_total_col}::numeric,2) AS total_exec_ms, round(${pgss_mean_col}::numeric,2) AS mean_exec_ms, rows, shared_blks_hit, shared_blks_read, temp_blks_written FROM ${PGSS_RELATION} ORDER BY ${pgss_total_col} DESC LIMIT ${TOP_N}" || true
        else
            pg_query_tsv "pg.stat_statements" "pg.performance" "$TABLES_DIR/pg_stat_statements.tsv" \
              "SELECT queryid, left(regexp_replace(query,E'[\\n\\r\\t]+',' ','g'),500) AS query, calls, round(${pgss_total_col}::numeric,2) AS total_exec_ms, round(${pgss_mean_col}::numeric,2) AS mean_exec_ms, rows, shared_blks_hit, shared_blks_read, temp_blks_written FROM ${PGSS_RELATION} ORDER BY ${pgss_total_col} DESC LIMIT ${TOP_N}" || true
        fi
    else record_skipped "pg.stat_statements" "pg.performance" "unsupported" "pg_stat_statements unavailable" "$TABLES_DIR/pg_stat_statements.tsv"; fi

    if [ "$STAT_WAL_AVAILABLE" -eq 1 ]; then
        pg_query_tsv "pg.stat_wal" "pg.performance" "$TABLES_DIR/stat_wal.tsv" "SELECT * FROM pg_stat_wal" || true
    else record_skipped "pg.stat_wal" "pg.performance" "not_applicable" "requires PostgreSQL 14+" "$TABLES_DIR/stat_wal.tsv"; fi

    if [ "$STAT_IO_AVAILABLE" -eq 1 ]; then
        pg_query_tsv "pg.stat_io" "pg.performance" "$TABLES_DIR/stat_io.tsv" "SELECT * FROM pg_stat_io" || true
    else record_skipped "pg.stat_io" "pg.performance" "not_applicable" "requires PostgreSQL 16+" "$TABLES_DIR/stat_io.tsv"; fi

    if [ "$STAT_CHECKPOINTER_AVAILABLE" -eq 1 ]; then
        pg_query_tsv "pg.checkpointer" "pg.performance" "$TABLES_DIR/checkpointer.tsv" "SELECT * FROM pg_stat_checkpointer" || true
    else record_skipped "pg.checkpointer" "pg.performance" "not_applicable" "requires PostgreSQL 17+" "$TABLES_DIR/checkpointer.tsv"; fi
}

# ============ PG 复制采集 ============

collect_pg_replication() {
    pg_query_tsv "pg.stat_replication" "pg.replication" "$TABLES_DIR/stat_replication.tsv" \
      "SELECT application_name, client_addr::text, state, sync_state, reply_time, pg_wal_lsn_diff(pg_current_wal_lsn(), replay_lsn)::bigint AS replay_lag_bytes, extract(epoch FROM coalesce(replay_lag, interval '0'))::bigint AS replay_lag_seconds FROM pg_stat_replication" || true
    pg_query_tsv "pg.wal_receiver" "pg.replication" "$TABLES_DIR/wal_receiver.tsv" \
      "SELECT status, sender_host, sender_port, slot_name, latest_end_lsn, latest_end_time FROM pg_stat_wal_receiver" || true
    pg_query_tsv "pg.replication_slots" "pg.replication" "$TABLES_DIR/replication_slots.tsv" \
      "SELECT slot_name, slot_type, active, database, restart_lsn, confirmed_flush_lsn, CASE WHEN restart_lsn IS NULL THEN 0 ELSE pg_wal_lsn_diff(pg_current_wal_lsn(),restart_lsn)::bigint END AS retained_bytes FROM pg_replication_slots" || true
    pg_query_tsv "pg.archiver" "pg.replication" "$TABLES_DIR/archiver.tsv" \
      "SELECT archived_count, last_archived_wal, last_archived_time, failed_count, last_failed_wal, last_failed_time, stats_reset FROM pg_stat_archiver" || true
}

# ============ PG 对象逐库采集 ============

collect_pg_objects() {
    log_info "逐库采集对象和安全信息"
    local db safe_db base db_pgss_relation db_list_query
    if [ "$ALL_DATABASES" -eq 1 ]; then
        db_list_query="SELECT datname FROM pg_database WHERE datallowconn AND NOT datistemplate ORDER BY datname"
    else
        db_list_query="SELECT current_database()"
    fi
    while IFS= read -r db; do
        [ -z "$db" ] && continue
        safe_db="$(printf '%s' "$db" | tr -c 'A-Za-z0-9._-' '_')"
        base="$TASK_DIR/databases/$safe_db"

        pg_query_tsv_db "$db" "db.${safe_db}.extensions" "pg.objects" "$base/extensions.tsv" \
          "SELECT current_database() AS database_name, extname AS extension_name, extversion AS version FROM pg_extension ORDER BY extname" || true
        pg_query_tsv_db "$db" "db.${safe_db}.table_health" "pg.objects" "$base/table_health.tsv" \
          "SELECT current_database() AS database_name, schemaname, relname AS table_name, n_live_tup, n_dead_tup, CASE WHEN n_live_tup+n_dead_tup=0 THEN 0 ELSE round(n_dead_tup::numeric/(n_live_tup+n_dead_tup),4) END AS dead_tuple_ratio, last_vacuum, last_autovacuum, last_analyze, last_autoanalyze, n_mod_since_analyze, pg_total_relation_size(relid) AS total_bytes FROM pg_stat_user_tables ORDER BY n_dead_tup DESC NULLS LAST LIMIT ${TOP_N}" || true
        pg_query_tsv_db "$db" "db.${safe_db}.large_tables" "pg.objects" "$base/large_tables.tsv" \
          "SELECT current_database() AS database_name, schemaname, relname AS table_name, pg_relation_size(relid) AS table_bytes, pg_indexes_size(relid) AS index_bytes, pg_total_relation_size(relid) AS total_bytes, n_live_tup FROM pg_stat_user_tables ORDER BY pg_total_relation_size(relid) DESC LIMIT 30" || true
        pg_query_tsv_db "$db" "db.${safe_db}.no_primary_key" "pg.objects" "$base/no_primary_key.tsv" \
          "SELECT current_database() AS database_name, n.nspname AS schemaname, c.relname AS table_name, pg_total_relation_size(c.oid) AS total_bytes, c.reltuples::bigint AS estimated_rows FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE c.relkind IN ('r','p') AND n.nspname NOT IN ('pg_catalog','information_schema','pg_toast') AND NOT EXISTS (SELECT 1 FROM pg_constraint x WHERE x.conrelid=c.oid AND x.contype='p') ORDER BY pg_total_relation_size(c.oid) DESC LIMIT ${TOP_N}" || true
        pg_query_tsv_db "$db" "db.${safe_db}.indexes" "pg.objects" "$base/indexes.tsv" \
          "SELECT current_database() AS database_name, s.schemaname, s.relname AS table_name, s.indexrelname AS index_name, s.idx_scan, pg_relation_size(s.indexrelid) AS index_bytes, i.indisvalid, i.indisready, i.indisunique, i.indisprimary FROM pg_stat_user_indexes s JOIN pg_index i ON i.indexrelid=s.indexrelid ORDER BY pg_relation_size(s.indexrelid) DESC LIMIT 200" || true
        pg_query_tsv_db "$db" "db.${safe_db}.duplicate_indexes" "pg.objects" "$base/duplicate_indexes.tsv" \
          "WITH idx AS (SELECT current_database() AS database_name, n.nspname AS schemaname, t.relname AS table_name, i.indrelid, i.indkey::text AS indkey, i.indcollation::text AS indcollation, i.indclass::text AS indclass, i.indoption::text AS indoption, i.indexprs::text AS indexprs, i.indpred::text AS indpred, i.indnkeyatts, i.indisunique, i.indisprimary, i.indisexclusion, ic.relam, pg_get_indexdef(i.indexrelid) AS index_def, pg_relation_size(i.indexrelid) AS index_bytes FROM pg_index i JOIN pg_class t ON t.oid=i.indrelid JOIN pg_class ic ON ic.oid=i.indexrelid JOIN pg_namespace n ON n.oid=t.relnamespace WHERE i.indisvalid AND i.indisready AND n.nspname NOT IN ('pg_catalog','information_schema','pg_toast')) SELECT database_name, schemaname, table_name, min(index_def) AS index_def, count(*) AS duplicate_count, sum(index_bytes) AS index_bytes FROM idx GROUP BY database_name, schemaname, table_name, indrelid, indkey, indcollation, indclass, indoption, indexprs, indpred, indnkeyatts, indisunique, indisprimary, indisexclusion, relam HAVING count(*)>1 ORDER BY sum(index_bytes) DESC LIMIT ${TOP_N}" || true
        pg_query_tsv_db "$db" "db.${safe_db}.unused_indexes" "pg.objects" "$base/unused_indexes.tsv" \
          "SELECT current_database() AS database_name, s.schemaname, s.relname AS table_name, s.indexrelname AS index_name, pg_size_pretty(pg_relation_size(s.indexrelid)) AS index_size, s.idx_scan FROM pg_stat_user_indexes s JOIN pg_index i ON i.indexrelid=s.indexrelid CROSS JOIN (SELECT stats_reset FROM pg_stat_database WHERE datname=current_database()) d WHERE s.idx_scan=0 AND i.indisvalid AND i.indisready AND NOT i.indisprimary AND NOT i.indisunique AND NOT i.indisexclusion AND pg_relation_size(s.indexrelid)>65536 AND s.indexrelname NOT LIKE 'pg_toast_%' AND s.schemaname NOT IN ('pg_catalog','information_schema') AND greatest(coalesce(d.stats_reset,'epoch'::timestamptz),pg_postmaster_start_time()) < now()-interval '7 days' ORDER BY pg_relation_size(s.indexrelid) DESC LIMIT ${TOP_N}" || true
        pg_query_tsv_db "$db" "db.${safe_db}.bloat_estimation" "pg.objects" "$base/bloat_estimation.tsv" \
          "SELECT current_database() AS database_name, schemaname, relname AS table_name, pg_total_relation_size(relid) AS total_bytes, pg_relation_size(relid) AS table_bytes, pg_indexes_size(relid) AS index_bytes, n_live_tup, n_dead_tup, CASE WHEN n_live_tup+n_dead_tup>0 THEN round(100.0*n_dead_tup/(n_live_tup+n_dead_tup),1) ELSE 0 END AS dead_pct, CASE WHEN n_live_tup+n_dead_tup>0 AND n_live_tup>0 THEN (pg_relation_size(relid)*n_dead_tup/(n_live_tup+n_dead_tup))::bigint ELSE 0 END AS estimated_dead_bytes, last_vacuum, last_autovacuum FROM pg_stat_user_tables WHERE n_dead_tup>0 ORDER BY estimated_dead_bytes DESC LIMIT ${TOP_N}" || true
        pg_query_tsv_db "$db" "db.${safe_db}.partition_summary" "pg.objects" "$base/partition_summary.tsv" \
          "SELECT current_database() AS database_name, n.nspname AS schemaname, p.relname AS partitioned_table, count(c.oid) AS partition_count, pg_size_pretty(sum(pg_total_relation_size(c.oid))) AS total_size, sum(pg_total_relation_size(c.oid)) AS total_bytes FROM pg_class p JOIN pg_namespace n ON n.oid=p.relnamespace JOIN pg_inherits x ON x.inhparent=p.oid JOIN pg_class c ON c.oid=x.inhrelid WHERE p.relkind='p' AND n.nspname NOT IN ('pg_catalog','information_schema','pg_toast') GROUP BY n.nspname,p.relname,p.oid ORDER BY sum(pg_total_relation_size(c.oid)) DESC LIMIT ${TOP_N}" || true
        pg_query_tsv_db "$db" "db.${safe_db}.invalid_constraints" "pg.objects" "$base/invalid_constraints.tsv" \
          "SELECT current_database() AS database_name, n.nspname AS schemaname, c.relname AS table_name, con.conname AS constraint_name, con.contype AS constraint_type FROM pg_constraint con JOIN pg_class c ON c.oid=con.conrelid JOIN pg_namespace n ON n.oid=c.relnamespace WHERE NOT con.convalidated AND n.nspname NOT IN ('pg_catalog','information_schema')" || true
        pg_query_tsv_db "$db" "db.${safe_db}.missing_fk_indexes" "pg.objects" "$base/missing_fk_indexes.tsv" \
          "SELECT current_database() AS database_name, n.nspname AS schemaname, c.relname AS table_name, con.conname AS constraint_name, pg_get_constraintdef(con.oid) AS definition FROM pg_constraint con JOIN pg_class c ON c.oid=con.conrelid JOIN pg_namespace n ON n.oid=c.relnamespace WHERE con.contype='f' AND NOT EXISTS (SELECT 1 FROM pg_index i WHERE i.indrelid=con.conrelid AND i.indisvalid AND i.indisready AND i.indpred IS NULL AND con.conkey <@ (i.indkey::smallint[])[0:i.indnkeyatts-1]) ORDER BY pg_total_relation_size(c.oid) DESC LIMIT ${TOP_N}" || true
        pg_query_tsv_db "$db" "db.${safe_db}.public_privileges" "pg.objects" "$base/public_privileges.tsv" \
          "SELECT current_database() AS database_name, n.nspname AS schemaname, c.relname AS table_name, a.privilege_type FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace CROSS JOIN LATERAL aclexplode(c.relacl) a WHERE a.grantee=0 AND c.relkind IN ('r','p','v','m','f') AND n.nspname NOT IN ('pg_catalog','information_schema','pg_toast') ORDER BY n.nspname,c.relname,a.privilege_type" || true
        pg_query_tsv_db "$db" "db.${safe_db}.security_definer_functions" "pg.objects" "$base/security_definer_functions.tsv" \
          "SELECT current_database() AS database_name, n.nspname AS schemaname, p.proname AS function_name, r.rolname AS owner, pg_get_function_identity_arguments(p.oid) AS arguments, p.proconfig FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace JOIN pg_roles r ON r.oid=p.proowner WHERE p.prosecdef AND n.nspname NOT IN ('pg_catalog','information_schema') ORDER BY n.nspname,p.proname" || true
        pg_query_tsv_db "$db" "db.${safe_db}.subscriptions" "pg.objects" "$base/subscriptions.tsv" \
          "SELECT current_database() AS database_name, s.subname AS subscription_name, s.subenabled, st.pid, st.received_lsn, st.latest_end_lsn, st.latest_end_time FROM pg_subscription s LEFT JOIN pg_stat_subscription st ON st.subid=s.oid" || true

        db_pgss_relation=$(pg_scalar_db "$db" "SELECT quote_ident(n.nspname)||'.'||quote_ident(c.relname) FROM pg_extension e JOIN pg_namespace n ON n.oid=e.extnamespace JOIN pg_class c ON c.relnamespace=n.oid AND c.relname='pg_stat_statements' WHERE e.extname='pg_stat_statements' LIMIT 1")
        if [ -n "$db_pgss_relation" ]; then
            if [ "$REDACT_SQL" -eq 1 ]; then
                pg_query_tsv_db "$db" "db.${safe_db}.pg_stat_statements" "pg.objects" "$base/pg_stat_statements.tsv" \
                  "SELECT current_database() AS database_name, queryid, NULL::text AS query, calls, round(${pgss_total_col}::numeric,2) AS total_exec_ms, round(${pgss_mean_col}::numeric,2) AS mean_exec_ms, rows, shared_blks_hit, shared_blks_read, temp_blks_written FROM ${db_pgss_relation} ORDER BY ${pgss_total_col} DESC LIMIT ${TOP_N}" || true
            else
                pg_query_tsv_db "$db" "db.${safe_db}.pg_stat_statements" "pg.objects" "$base/pg_stat_statements.tsv" \
                  "SELECT current_database() AS database_name, queryid, left(regexp_replace(query,E'[\\n\\r\\t]+',' ','g'),500) AS query, calls, round(${pgss_total_col}::numeric,2) AS total_exec_ms, round(${pgss_mean_col}::numeric,2) AS mean_exec_ms, rows, shared_blks_hit, shared_blks_read, temp_blks_written FROM ${db_pgss_relation} ORDER BY ${pgss_total_col} DESC LIMIT ${TOP_N}" || true
            fi
        fi
    done < <(pg_exec -At -c "$db_list_query" 2>>"$LOG_FILE")
}

# ============ PG 日志与备份 ============

collect_pg_logs_backup() {
    local log_rel log_path log_summary log_tail spec category pattern count
    log_rel=$(pg_scalar "SELECT pg_current_logfile()")
    if [ -n "$log_rel" ]; then
        if [[ "$log_rel" = /* ]]; then log_path="$log_rel"; else log_path="$PG_DATA_DIR/$log_rel"; fi
        if [ -r "$log_path" ]; then
            log_summary="$TABLES_DIR/error_log_summary.tsv"
            printf 'category\tcount\n' > "$log_summary"
            log_tail="$(mktemp)"; tail -n 20000 "$log_path" > "$log_tail" 2>>"$LOG_FILE" || true
            for spec in 'PANIC|PANIC' 'FATAL|FATAL' 'ERROR|ERROR' 'DEADLOCK|deadlock detected' 'OOM|out of memory' 'WRITE_FAILURE|could not write|no space left on device' 'FREQUENT_CHECKPOINT|checkpoints are occurring too frequently'; do
                category="${spec%%|*}"; pattern="${spec#*|}"
                count="$(grep -Eic "$pattern" "$log_tail" 2>/dev/null || true)"
                printf '%s\t%s\n' "$category" "${count:-0}" >> "$log_summary"
            done
            rm -f "$log_tail"
            record_status "pg.log_summary" "pg.logs" "ok" "$(iso_now)" "$(iso_now)" 0 7 0 "tables/error_log_summary.tsv" "last 20000 lines"
        else
            record_skipped "pg.log_summary" "pg.logs" "permission_denied" "current log file unreadable"
        fi
    else
        record_skipped "pg.log_summary" "pg.logs" "not_enabled" "pg_current_logfile unavailable or logging_collector disabled"
    fi

    if has_cmd pgbackrest; then
        capture_command "backup.pgbackrest" "pg.backup" "$EVIDENCE_DIR/pgbackrest_info.json" pgbackrest info --output=json || true
    else record_skipped "backup.pgbackrest" "pg.backup" "unsupported" "pgbackrest unavailable"; fi
    if has_cmd barman; then
        capture_command "backup.barman" "pg.backup" "$EVIDENCE_DIR/barman_check.txt" barman check all || true
    else record_skipped "backup.barman" "pg.backup" "unsupported" "barman unavailable"; fi
    if has_cmd patronictl; then
        capture_command "ha.patroni" "pg.backup" "$EVIDENCE_DIR/patroni_cluster.json" patronictl list --format json || true
    elif has_cmd repmgr; then
        capture_command "ha.repmgr" "pg.backup" "$EVIDENCE_DIR/repmgr_cluster.txt" repmgr cluster show --csv || true
    else record_skipped "ha.manager" "pg.backup" "unsupported" "patronictl/repmgr unavailable"; fi

    if has_cmd crontab; then
        crontab -l 2>/dev/null | grep -Ei 'pg|postgres|backup|pgbackrest|barman|pg_dump|pgdump|wal|repmgr|patroni' > "$EVIDENCE_DIR/backup_cron.txt"
        record_status "system.backup_cron" "pg.backup" "$([ -s "$EVIDENCE_DIR/backup_cron.txt" ] && echo ok || echo empty)" "$(iso_now)" "$(iso_now)" 0 "$(wc -l < "$EVIDENCE_DIR/backup_cron.txt")" 0 "evidence/backup_cron.txt" ""
    else record_skipped "system.backup_cron" "pg.backup" "unsupported" "crontab command unavailable"; fi

    if has_cmd systemctl; then
        systemctl list-timers --all --no-pager 2>/dev/null | grep -Ei 'pg|postgres|backup|pgbackrest|barman|dump|wal' > "$EVIDENCE_DIR/backup_timers.txt"
        record_status "system.backup_timers" "pg.backup" "$([ -s "$EVIDENCE_DIR/backup_timers.txt" ] && echo ok || echo empty)" "$(iso_now)" "$(iso_now)" 0 "$(wc -l < "$EVIDENCE_DIR/backup_timers.txt")" 0 "evidence/backup_timers.txt" ""
    fi

    ps -eo pid,user,etimes,args 2>/dev/null | grep -Ei '[p]gbackrest|[b]arman|[p]g_dump|[w]al-g|[p]g_basebackup' > "$EVIDENCE_DIR/backup_processes.txt"
    record_status "system.backup_processes" "pg.backup" "$([ -s "$EVIDENCE_DIR/backup_processes.txt" ] && echo ok || echo empty)" "$(iso_now)" "$(iso_now)" 0 "$(wc -l < "$EVIDENCE_DIR/backup_processes.txt")" 0 "evidence/backup_processes.txt" ""
}

# ============ 角色推导 ============

tsv_first_value() {
    local file="$1"; shift
    [ -s "$file" ] || return 0
    awk -F'\t' -v names="$*" '
      NR==1 {n=split(names,w," "); for(i=1;i<=NF;i++) for(j=1;j<=n;j++) if(tolower($i)==tolower(w[j])) col=i; next}
      NR==2 && col>0 {print $col; exit}' "$file"
}

derive_role_evidence() {
    REPLICATION_SOURCE_COUNT=0
    [ -s "$TABLES_DIR/stat_replication.tsv" ] && REPLICATION_SOURCE_COUNT=$(awk 'END{print (NR>0?NR-1:0)}' "$TABLES_DIR/stat_replication.tsv" 2>/dev/null)
    REPLICATION_SOURCE_COUNT=${REPLICATION_SOURCE_COUNT:-0}

    WAL_RECEIVER_STATUS=""
    [ -s "$TABLES_DIR/wal_receiver.tsv" ] && WAL_RECEIVER_STATUS=$(tsv_first_value "$TABLES_DIR/wal_receiver.tsv" status)

    SUBSCRIPTION_COUNT=0
    for f in "$TASK_DIR"/databases/*/subscriptions.tsv; do
        [ -f "$f" ] && [ -s "$f" ] && SUBSCRIPTION_COUNT=$((SUBSCRIPTION_COUNT + $(awk 'END{print (NR>0?NR-1:0)}' "$f" 2>/dev/null)))
    done
    SUBSCRIPTION_COUNT=${SUBSCRIPTION_COUNT:-0}

    if [ "$REPLICATION_SOURCE_COUNT" -gt 0 ]; then
        ROLE_OBSERVED="primary"; ROLE_CONFIDENCE="high"
    elif [ -n "$WAL_RECEIVER_STATUS" ] && [ "$WAL_RECEIVER_STATUS" != "" ]; then
        ROLE_OBSERVED="standby"; ROLE_CONFIDENCE="high"
    elif [ "$SUBSCRIPTION_COUNT" -gt 0 ]; then
        ROLE_OBSERVED="logical_subscriber"; ROLE_CONFIDENCE="medium"
    elif [ "$PG_IS_RECOVERY" = "t" ]; then
        ROLE_OBSERVED="standby"; ROLE_CONFIDENCE="high"
    else
        ROLE_OBSERVED="standalone"; ROLE_CONFIDENCE="medium"
    fi
}

# ============ 输出生成 ============

generate_collection_status_json() {
    local files
    files=$(find "$STATUS_PARTS_DIR" -type f -name '*.tsv' | sort | tr '\n' ' ')
    if [ -z "$files" ]; then
        printf '{"schema_version":"1.0","items":[],"summary":{}}\n' > "$COLLECTION_STATUS_FILE"
        return 0
    fi
    awk -F'\t' '
      function esc(s,   t){
        t=s; gsub(/\\/,"\\\\",t); gsub(/"/,"\\\"",t); gsub(/\r/,"\\r",t); gsub(/\n/,"\\n",t); gsub(/\t/,"\\t",t); return t
      }
      BEGIN{print "{"; print "  \"schema_version\": \"1.0\","; print "  \"items\": ["; first=1}
      NF>=9{
        if(!first) print ","; first=0
        printf "    {\"item_id\":\"%s\",\"category\":\"%s\",\"status\":\"%s\",\"started_at\":\"%s\",\"finished_at\":\"%s\",\"duration_ms\":%s,\"row_count\":%s,\"exit_code\":%s,\"output_file\":\"%s\",\"reason\":\"%s\"}",esc($1),esc($2),esc($3),esc($4),esc($5),($6~/^[0-9]+$/?$6:"null"),($7~/^[0-9]+$/?$7:"null"),($8~/^-?[0-9]+$/?$8:"null"),esc($9),esc($10)
        c[$3]++
      }
      END{
        print ""; print "  ],";
        printf "  \"summary\": {\"ok\":%d,\"empty\":%d,\"unsupported\":%d,\"not_enabled\":%d,\"not_applicable\":%d,\"permission_denied\":%d,\"timeout\":%d,\"error\":%d,\"skipped\":%d,\"partial\":%d}\n",c["ok"]+0,c["empty"]+0,c["unsupported"]+0,c["not_enabled"]+0,c["not_applicable"]+0,c["permission_denied"]+0,c["timeout"]+0,c["error"]+0,c["skipped"]+0,c["partial"]+0
        print "}"
      }' $files > "$COLLECTION_STATUS_FILE"
}

generate_snapshot_json() {
    local os_name="" kernel="" host_fqdn="" machine_id="" cpu_count="0" mem_total_kb="0" collected_end
    local actual_pg_points actual_cpu_points actual_elapsed_ms sampling_status
    local sar_coverage_status sar_coverage_hours sar_first_timestamp sar_last_timestamp
    local host_local_time host_utc_time host_timezone ntp_synchronized
    local ok_count error_count warning_count

    [ -r /etc/os-release ] && os_name=$(awk -F= '$1=="PRETTY_NAME"{gsub(/^"|"$/,"",$2);print $2}' /etc/os-release)
    kernel=$(uname -r 2>/dev/null)
    host_fqdn=$(hostname -f 2>/dev/null || hostname 2>/dev/null)
    [ -r /etc/machine-id ] && machine_id=$(tr -d '\r\n' < /etc/machine-id)
    [ -z "$machine_id" ] && machine_id=$(printf '%s' "$host_fqdn" | sha256sum 2>/dev/null | awk '{print $1}')
    cpu_count=$(getconf _NPROCESSORS_ONLN 2>/dev/null || nproc 2>/dev/null || echo 0)
    mem_total_kb=$(awk '$1=="MemTotal:"{print $2}' /proc/meminfo 2>/dev/null)
    collected_end=$(iso_now)
    derive_role_evidence

    ok_count=$(awk -F'\t' '$3=="ok"||$3=="empty"||$3=="not_applicable"||$3=="skipped"{n++}END{print n+0}' "$STATUS_PARTS_DIR"/*.tsv 2>/dev/null)
    error_count=$(awk -F'\t' '$3=="error"||$3=="timeout"||$3=="permission_denied"{n++}END{print n+0}' "$STATUS_PARTS_DIR"/*.tsv 2>/dev/null)
    warning_count=$(awk -F'\t' '$3=="unsupported"||$3=="not_enabled"||$3=="partial"{n++}END{print n+0}' "$STATUS_PARTS_DIR"/*.tsv 2>/dev/null)

    actual_pg_points=$(awk 'END{print (NR>0?NR-1:0)}' "$PG_ACTIVITY_CSV" 2>/dev/null); actual_pg_points=${actual_pg_points:-0}
    actual_cpu_points=$(awk 'END{print (NR>0?NR-1:0)}' "$CPU_CSV" 2>/dev/null); actual_cpu_points=${actual_cpu_points:-0}
    actual_elapsed_ms=$(awk -F, 'NR>1{v=$2}END{print v+0}' "$PG_ACTIVITY_CSV" 2>/dev/null); actual_elapsed_ms=${actual_elapsed_ms:-0}
    sampling_status=$(awk -F'\t' '$1=="timeseries.realtime_sampling"{print $3}' "$STATUS_PARTS_DIR"/*.tsv 2>/dev/null | tail -1); sampling_status=${sampling_status:-error}
    sar_coverage_status=$(awk -F'\t' '$1=="status"{print $2}' "$HISTORY_DIR/coverage.tsv" 2>/dev/null); sar_coverage_status=${sar_coverage_status:-empty}
    sar_coverage_hours=$(awk -F'\t' '$1=="coverage_hours"{print $2}' "$HISTORY_DIR/coverage.tsv" 2>/dev/null); sar_coverage_hours=${sar_coverage_hours:-0}
    sar_first_timestamp=$(awk -F'\t' '$1=="first_timestamp"{print $2}' "$HISTORY_DIR/coverage.tsv" 2>/dev/null)
    sar_last_timestamp=$(awk -F'\t' '$1=="last_timestamp"{print $2}' "$HISTORY_DIR/coverage.tsv" 2>/dev/null)
    host_local_time=$(awk -F= '$1=="local_time"{print substr($0,index($0,"=")+1)}' "$EVIDENCE_DIR/time_status.txt" 2>/dev/null)
    host_utc_time=$(awk -F= '$1=="utc_time"{print substr($0,index($0,"=")+1)}' "$EVIDENCE_DIR/time_status.txt" 2>/dev/null)
    host_timezone=$(awk -F= '$1=="timezone"{print substr($0,index($0,"=")+1)}' "$EVIDENCE_DIR/time_status.txt" 2>/dev/null)
    ntp_synchronized=$(awk -F: '/System clock synchronized/{gsub(/^[ \t]+|[ \t]+$/,"",$2);print $2}' "$EVIDENCE_DIR/timedatectl.txt" 2>/dev/null)

    {
      printf '{\n'
      printf '  "schema_version": %s,\n' "$(json_quote "$SNAPSHOT_SCHEMA_VERSION")"
      printf '  "package_version": %s,\n' "$(json_quote "$PACKAGE_VERSION")"
      printf '  "collector": {"name":"pg_inspection","version":%s,"platform":"linux-bash","started_at":%s,"finished_at":%s},\n' \
        "$(json_quote "$COLLECTOR_VERSION")" "$(json_quote "$COLLECTION_STARTED_AT")" "$(json_quote "$collected_end")"
      printf '  "instance_identity": {"database_type":"postgresql","version":%s,"version_num":%s,"is_recovery":%s,"wal_level":%s,"archive_mode":%s,"data_directory":%s,"connect_host":%s,"connect_ip":%s,"instance_ip":%s,"port":%s,"instance_tag":%s},\n' \
        "$(json_quote "$PG_VERSION")" "$(json_quote "$PG_VERSION_NUM")" "$([ "$PG_IS_RECOVERY" = "t" ] && echo true || echo false)" \
        "$(json_quote "$WAL_LEVEL")" "$(json_quote "$ARCHIVE_MODE")" "$(json_quote "$PG_DATA_DIR")" \
        "$(json_quote "$PGHOST_ARG")" "$(json_quote "$TARGET_RESOLVED_IP")" "$(json_quote "$INSTANCE_ADDRESS")" \
        "$(json_number_or_null "$PGPORT_ARG")" "$(json_quote "$INSTANCE_TAG")"
      printf '  "host_identity": {"hostname":%s,"short_hostname":%s,"primary_ip":%s,"all_ipv4":%s,"database_target_is_local":%s,"machine_id":%s,"os":%s,"kernel":%s,"cpu_count":%s,"memory_total_bytes":%s},\n' \
        "$(json_quote "$host_fqdn")" "$(json_quote "$COLLECTOR_HOSTNAME")" "$(json_quote "$COLLECTOR_PRIMARY_IP")" "$(json_quote "$COLLECTOR_ALL_IPV4")" "$([ "$TARGET_IS_LOCAL" -eq 1 ] && echo true || echo false)" \
        "$(json_quote "$machine_id")" "$(json_quote "$os_name")" "$(json_quote "$kernel")" "$(json_number_or_null "$cpu_count")" "$(json_number_or_null "$(( ${mem_total_kb:-0} * 1024 ))")"
      printf '  "time_evidence": {"host_local_time":%s,"host_utc_time":%s,"timezone":%s,"ntp_synchronized":%s},\n' \
        "$(json_quote "$host_local_time")" "$(json_quote "$host_utc_time")" "$(json_quote "$host_timezone")" "$(json_quote "$ntp_synchronized")"
      printf '  "role_evidence": {"role_observed":%s,"confidence":%s,"is_recovery":%s,"replication_source_count":%s,"wal_receiver_status":%s,"subscription_count":%s},\n' \
        "$(json_quote "$ROLE_OBSERVED")" "$(json_quote "$ROLE_CONFIDENCE")" \
        "$([ "$PG_IS_RECOVERY" = "t" ] && echo true || echo false)" "$REPLICATION_SOURCE_COUNT" \
        "$(json_quote "$WAL_RECEIVER_STATUS")" "$SUBSCRIPTION_COUNT"
      printf '  "capabilities": {"pg_stat_statements":%s,"pg_stat_wal":%s,"pg_stat_io":%s,"pg_stat_checkpointer":%s,"sar_command":%s,"sadf_command":%s},\n' \
        "$([ "$STAT_STATEMENTS_AVAILABLE" -eq 1 ] && echo true || echo false)" \
        "$([ "$STAT_WAL_AVAILABLE" -eq 1 ] && echo true || echo false)" \
        "$([ "$STAT_IO_AVAILABLE" -eq 1 ] && echo true || echo false)" \
        "$([ "$STAT_CHECKPOINTER_AVAILABLE" -eq 1 ] && echo true || echo false)" \
        "$([ "${HAS_SAR:-0}" -eq 1 ] && echo true || echo false)" "$([ "${HAS_SADF:-0}" -eq 1 ] && echo true || echo false)"
      printf '  "sampling": {"status":%s,"interval_seconds":%s,"requested_sample_count":%s,"requested_duration_seconds":%s,"actual_pg_points":%s,"actual_cpu_points":%s,"actual_elapsed_ms":%s,"sar_history_requested_hours":%s,"sar_history":{"status":%s,"coverage_hours":%s,"first_timestamp":%s,"last_timestamp":%s},"realtime_files":{"cpu":"timeseries/system_cpu.csv","memory":"timeseries/system_memory.csv","disk":"timeseries/system_disk.csv","network":"timeseries/system_network.csv","pg_activity":"timeseries/pg_activity.csv","pg_stats":"timeseries/pg_stats.csv"},"history_dir":"history"},\n' \
        "$(json_quote "$sampling_status")" "$SAMPLE_INTERVAL" "$SAMPLE_COUNT" "$((SAMPLE_INTERVAL*SAMPLE_COUNT))" "$actual_pg_points" "$actual_cpu_points" "$actual_elapsed_ms" "$SAR_HISTORY_HOURS" "$(json_quote "$sar_coverage_status")" "$sar_coverage_hours" "$(json_quote "$sar_first_timestamp")" "$(json_quote "$sar_last_timestamp")"
      printf '  "privacy": {"sql_text_included":%s,"settings_included":true,"password_included":false},\n' \
        "$([ "$REDACT_SQL" -eq 1 ] && echo false || echo true)"
      printf '  "collection_summary": {"successful_or_empty_items":%s,"warning_items":%s,"failed_items":%s,"collection_status_file":"collection_status.json"},\n' "$ok_count" "$warning_count" "$error_count"
      printf '  "artifacts": {"tables_dir":"tables","timeseries_dir":"timeseries","history_dir":"history","evidence_dir":"evidence","databases_dir":"databases","summary":"summary.txt","log":"logs/collection.log"}\n'
      printf '}\n'
    } > "$SNAPSHOT_FILE"
}

generate_summary() {
    local total_ms ok empty unsupported not_enabled permission timeout error skipped partial
    local actual_pg_points actual_elapsed_ms sampling_status sar_coverage_status sar_coverage_hours sar_first sar_last
    total_ms=$(( $(epoch_ms) - COLLECTION_STARTED_MS ))
    ok=$(awk -F'\t' '$3=="ok"{n++}END{print n+0}' "$STATUS_PARTS_DIR"/*.tsv 2>/dev/null)
    empty=$(awk -F'\t' '$3=="empty"{n++}END{print n+0}' "$STATUS_PARTS_DIR"/*.tsv 2>/dev/null)
    unsupported=$(awk -F'\t' '$3=="unsupported"{n++}END{print n+0}' "$STATUS_PARTS_DIR"/*.tsv 2>/dev/null)
    not_enabled=$(awk -F'\t' '$3=="not_enabled"{n++}END{print n+0}' "$STATUS_PARTS_DIR"/*.tsv 2>/dev/null)
    permission=$(awk -F'\t' '$3=="permission_denied"{n++}END{print n+0}' "$STATUS_PARTS_DIR"/*.tsv 2>/dev/null)
    timeout=$(awk -F'\t' '$3=="timeout"{n++}END{print n+0}' "$STATUS_PARTS_DIR"/*.tsv 2>/dev/null)
    error=$(awk -F'\t' '$3=="error"{n++}END{print n+0}' "$STATUS_PARTS_DIR"/*.tsv 2>/dev/null)
    partial=$(awk -F'\t' '$3=="partial"{n++}END{print n+0}' "$STATUS_PARTS_DIR"/*.tsv 2>/dev/null)
    skipped=$(awk -F'\t' '$3=="skipped"||$3=="not_applicable"{n++}END{print n+0}' "$STATUS_PARTS_DIR"/*.tsv 2>/dev/null)
    actual_pg_points=$(awk 'END{print (NR>0?NR-1:0)}' "$PG_ACTIVITY_CSV" 2>/dev/null); actual_pg_points=${actual_pg_points:-0}
    actual_elapsed_ms=$(awk -F, 'NR>1{v=$2}END{print v+0}' "$PG_ACTIVITY_CSV" 2>/dev/null); actual_elapsed_ms=${actual_elapsed_ms:-0}
    sampling_status=$(awk -F'\t' '$1=="timeseries.realtime_sampling"{print $3}' "$STATUS_PARTS_DIR"/*.tsv 2>/dev/null | tail -1); sampling_status=${sampling_status:-error}
    sar_coverage_status=$(awk -F'\t' '$1=="status"{print $2}' "$HISTORY_DIR/coverage.tsv" 2>/dev/null); sar_coverage_status=${sar_coverage_status:-empty}
    sar_coverage_hours=$(awk -F'\t' '$1=="coverage_hours"{print $2}' "$HISTORY_DIR/coverage.tsv" 2>/dev/null); sar_coverage_hours=${sar_coverage_hours:-0}
    sar_first=$(awk -F'\t' '$1=="first_timestamp"{print $2}' "$HISTORY_DIR/coverage.tsv" 2>/dev/null)
    sar_last=$(awk -F'\t' '$1=="last_timestamp"{print $2}' "$HISTORY_DIR/coverage.tsv" 2>/dev/null)
    {
      printf 'PostgreSQL 巡检采集摘要\n'
      printf '=========================\n'
      printf '采集器版本: %s\n' "$COLLECTOR_VERSION"
      printf '实例标识: %s\n' "$INSTANCE_TAG"
      printf 'PG 版本: %s\n' "$PG_VERSION"
      printf '实例地址: %s:%s\n' "$INSTANCE_ADDRESS" "$PGPORT_ARG"
      printf '连接地址: %s:%s\n' "$PGHOST_ARG" "$PGPORT_ARG"
      printf '采集主机: %s（主IP %s）\n' "$COLLECTOR_HOSTNAME" "$COLLECTOR_PRIMARY_IP"
      [ "$TARGET_IS_LOCAL" -eq 1 ] || printf '警告: 连接目标不是本机地址，系统数据属于采集器所在主机\n'
      printf '角色观察: %s（最终拓扑由 Python 合并判断）\n' "$ROLE_OBSERVED"
      printf '采集开始: %s\n' "$COLLECTION_STARTED_AT"
      printf '总耗时: %.2f 秒\n' "$(awk -v ms="$total_ms" 'BEGIN{print ms/1000}')"
      printf '历史 sar 请求范围: 最近 %s 小时；初步状态 %s，约覆盖 %s 小时\n' "$SAR_HISTORY_HOURS" "$sar_coverage_status" "$sar_coverage_hours"
      [ -n "$sar_first" ] && printf '历史 sar 初步范围: %s ~ %s\n' "$sar_first" "$sar_last"
      printf '实时同步采样请求: %s 秒一次，共 %s 次，约 %s 秒\n' "$SAMPLE_INTERVAL" "$SAMPLE_COUNT" "$((SAMPLE_INTERVAL*SAMPLE_COUNT))"
      printf '实时同步采样实际: 状态 %s，PG 数据点 %s，实际跨度 %.2f 秒\n' "$sampling_status" "$actual_pg_points" "$(awk -v ms="$actual_elapsed_ms" 'BEGIN{print ms/1000}')"
      printf '\n状态统计\n'
      printf '  成功: %s\n  成功但无数据: %s\n  部分成功: %s\n  不支持: %s\n  未启用: %s\n  不适用/跳过: %s\n  权限不足: %s\n  超时: %s\n  错误: %s\n' "$ok" "$empty" "$partial" "$unsupported" "$not_enabled" "$skipped" "$permission" "$timeout" "$error"
      printf '\n耗时最长的采集项（前 10）\n'
      cat "$STATUS_PARTS_DIR"/*.tsv 2>/dev/null | awk -F'\t' -v total="$total_ms" '$4!="" && $6 ~ /^[0-9]+$/ && $6>=0 && $6<=total*2' | sort -t$'\t' -k6,6nr | head -10 | awk -F'\t' '{printf "  %-45s %8.3f 秒  %s\n",$1,$6/1000,$3}'
      printf '\n非成功项\n'
      cat "$STATUS_PARTS_DIR"/*.tsv 2>/dev/null | awk -F'\t' '$3!="ok"&&$3!="empty"&&$3!="skipped"&&$3!="not_applicable"&&$3!="not_enabled"{printf "  %s: %s，%s\n",$1,$3,$10}'
      printf '\n正式数据文件\n'
      printf '  snapshot.json             实例、主机、能力和角色证据\n'
      printf '  collection_status.json    每个采集项的状态、耗时和失败原因\n'
      printf '  tables/*.tsv              静态结构化数据\n'
      printf '  timeseries/*.csv          实时同步时序\n'
      printf '  history/*.csv             历史 sar/sadf 数据（若存在）\n'
      printf '  manifest.json             文件大小和 SHA256\n'
    } > "$SUMMARY_FILE"
}

# ============ 安全扫描与打包 ============

security_scan() {
    local findings="$TMP_DIR/security_scan_findings.txt"
    : > "$findings"
    grep -RInE --exclude='collection.log' --exclude='manifest.json' --exclude='security_scan_findings.txt' \
      '(password[[:space:]]*=[[:space:]]*"?[^<[:space:]";]+|PGPASSWORD=[^[:space:]]+|BEGIN[[:space:]].*PRIVATE KEY|Authorization:[[:space:]]*(Basic|Bearer)[[:space:]]+[A-Za-z0-9._-]+)' \
      "$TASK_DIR" > "$findings" 2>/dev/null
    if [ -s "$findings" ]; then
        cp "$findings" "$LOG_DIR/security_scan_findings.txt"
        record_status "package.security_scan" "package" "error" "$(iso_now)" "$(iso_now)" 0 "$(wc -l < "$findings")" 1 "logs/security_scan_findings.txt" "high-confidence sensitive pattern detected; package creation blocked"
        return 1
    fi
    record_status "package.security_scan" "package" "ok" "$(iso_now)" "$(iso_now)" 0 0 0 "" ""
    return 0
}

generate_manifest() {
    local list="$TMP_DIR/manifest_files.txt" f rel first=1
    find "$TASK_DIR" -type f ! -path "$TMP_DIR/*" ! -path "$STATUS_PARTS_DIR/*" ! -name 'manifest.json' ! -name '.pgpass.*' -print | sort > "$list"
    {
      printf '{\n  "package_version":%s,\n  "collector_version":%s,\n  "database_type":"postgresql",\n  "instance_tag":%s,\n  "created_at":%s,\n  "files":[\n' \
        "$(json_quote "$PACKAGE_VERSION")" "$(json_quote "$COLLECTOR_VERSION")" "$(json_quote "$INSTANCE_TAG")" "$(json_quote "$(iso_now)")"
      while IFS= read -r f; do
        rel=$(safe_relpath "$f"); [ "$first" -eq 1 ] || printf ',\n'; first=0
        printf '    {"path":%s,"size_bytes":%s,"sha256":%s}' "$(json_quote "$rel")" "$(file_size_bytes "$f")" "$(json_quote "$(sha256_file "$f")")"
      done < "$list"
      printf '\n  ]\n}\n'
    } > "$MANIFEST_FILE"
}

create_package() {
    [ "$CREATE_PACKAGE" -eq 1 ] || return 0
    local parent base start_ms end_ms rc err_file
    parent=$(dirname "$TASK_DIR"); base=$(basename "$TASK_DIR"); PACKAGE_FILE="${parent}/${base}.tar.gz"
    err_file="${PACKAGE_FILE}.stderr.tmp"
    start_ms=$(epoch_ms)
    tar --exclude='*/.pgpass.*' -C "$parent" -czf "$PACKAGE_FILE" "$base" 2> "$err_file"; rc=$?
    end_ms=$(epoch_ms)
    if [ "$rc" -eq 0 ]; then
        rm -f "$err_file"
        printf '[INFO] 回传包生成完成，耗时 %s ms: %s\n' "$((end_ms-start_ms))" "$PACKAGE_FILE"
        return 0
    fi
    printf '[ERROR] 回传包生成失败，耗时 %s ms\n' "$((end_ms-start_ms))" >&2
    cat "$err_file" >&2 2>/dev/null
    rm -f "$err_file"
    return "$rc"
}

cleanup_auth() {
    [ -n "${PGPASSWORD_SET:-}" ] && unset PGPASSWORD
    if [ "${ORIGINAL_PGOPTIONS_SET:-0}" -eq 1 ]; then export PGOPTIONS="$ORIGINAL_PGOPTIONS"; else unset PGOPTIONS; fi
    if [ "${ORIGINAL_PGAPPNAME_SET:-0}" -eq 1 ]; then export PGAPPNAME="$ORIGINAL_PGAPPNAME"; else unset PGAPPNAME; fi
}

handle_signal() {
    cleanup_auth
    if [ -n "${BG_PIDS[*]:-}" ]; then kill "${BG_PIDS[@]}" 2>/dev/null || true; fi
    [ -n "${TASK_DIR:-}" ] && [ -d "$TASK_DIR" ] && printf 'partial\n' > "$TASK_DIR/COLLECTION_INCOMPLETE"
    exit 130
}

trap handle_signal INT TERM HUP

# ==================== 主入口 ====================

POSITIONAL=()
while [ $# -gt 0 ]; do
    case "$1" in
      -h|--help) show_usage; exit 0 ;;
      --host) PGHOST_ARG="${2-}"; shift 2 ;;
      --port) PGPORT_ARG="${2-}"; shift 2 ;;
      --user) PGUSER_ARG="${2-}"; shift 2 ;;
      --database) PGDATABASE_ARG="${2-}"; shift 2 ;;
      --password-file) PASSWORD_FILE="${2-}"; shift 2 ;;
      --output-dir) OUTPUT_PARENT="${2-}"; shift 2 ;;
      --sample-interval) SAMPLE_INTERVAL="${2-}"; shift 2 ;;
      --sample-count) SAMPLE_COUNT="${2-}"; shift 2 ;;
      --sar-history-hours) SAR_HISTORY_HOURS="${2-}"; shift 2 ;;
      --pg-timeout) PG_TIMEOUT_SECONDS="${2-}"; shift 2 ;;
      --include-log-text) INCLUDE_LOG_TEXT=1; shift ;;
      --redact-sql) REDACT_SQL=1; shift ;;
      --current-database-only) ALL_DATABASES=0; shift ;;
      --no-package) CREATE_PACKAGE=0; shift ;;
      --) shift; while [ $# -gt 0 ]; do POSITIONAL+=("$1"); shift; done ;;
      -*) printf '未知选项: %s\n' "$1" >&2; show_usage; exit 10 ;;
      *) POSITIONAL+=("$1"); shift ;;
    esac
done

for n in "$PGPORT_ARG" "$SAMPLE_INTERVAL" "$SAMPLE_COUNT" "$SAR_HISTORY_HOURS" "$PG_TIMEOUT_SECONDS"; do is_uint "$n" || { printf '端口和时间参数必须是正整数\n' >&2; exit 10; }; done
[ "$SAMPLE_INTERVAL" -ge 1 ] && [ "$SAMPLE_COUNT" -ge 1 ] || exit 10
[ "${BASH_VERSINFO[0]:-0}" -ge 4 ] || { printf '需要 Bash 4.0 或更高版本\n' >&2; exit 10; }
has_cmd psql || { printf '未找到 psql 客户端\n' >&2; exit 10; }
mkdir -p "$OUTPUT_PARENT" || exit 10
check_output_space

COLLECTION_STARTED_AT=$(iso_now); COLLECTION_STARTED_MS=$(epoch_ms)
COLLECTOR_HOSTNAME=$(hostname -s 2>/dev/null || hostname 2>/dev/null || echo unknown_host)
COLLECTOR_FQDN=$(hostname -f 2>/dev/null || printf '%s' "$COLLECTOR_HOSTNAME")
COLLECTOR_ALL_IPV4=$(collect_all_ipv4)
COLLECTOR_PRIMARY_IP=$(detect_primary_ipv4)
TARGET_RESOLVED_IP=$(resolve_ipv4 "$PGHOST_ARG")
TARGET_IS_LOCAL=0; is_local_connect_target "$PGHOST_ARG" "$TARGET_RESOLVED_IP" && TARGET_IS_LOCAL=1
case "$PGHOST_ARG" in 127.*|localhost|::1|/*) INSTANCE_ADDRESS="$COLLECTOR_PRIMARY_IP" ;; *) INSTANCE_ADDRESS="${TARGET_RESOLVED_IP:-$PGHOST_ARG}" ;; esac
qctime=$(date +'%Y%m%d_%H%M%S'); pre_tag=$(sanitize_id "${COLLECTOR_HOSTNAME}_${INSTANCE_ADDRESS}_${PGPORT_ARG}")
TASK_DIR="${OUTPUT_PARENT%/}/pg_inspection_v2_${pre_tag}_${qctime}"
TABLES_DIR="$TASK_DIR/tables"; TIMESERIES_DIR="$TASK_DIR/timeseries"; HISTORY_DIR="$TASK_DIR/history"; EVIDENCE_DIR="$TASK_DIR/evidence"
LOG_DIR="$TASK_DIR/logs"; MODULE_LOG_DIR="$LOG_DIR/modules"; TMP_DIR="$TASK_DIR/.tmp"; STATUS_PARTS_DIR="$TASK_DIR/.status_parts"
mkdir -p "$TABLES_DIR" "$TIMESERIES_DIR" "$HISTORY_DIR" "$EVIDENCE_DIR" "$MODULE_LOG_DIR" "$TMP_DIR" "$STATUS_PARTS_DIR" || exit 10
LOG_FILE="$LOG_DIR/collection.log"; SNAPSHOT_FILE="$TASK_DIR/snapshot.json"; COLLECTION_STATUS_FILE="$TASK_DIR/collection_status.json"
SUMMARY_FILE="$TASK_DIR/summary.txt"; MANIFEST_FILE="$TASK_DIR/manifest.json"
CPU_CSV="$TIMESERIES_DIR/system_cpu.csv"; MEM_CSV="$TIMESERIES_DIR/system_memory.csv"; NET_CSV="$TIMESERIES_DIR/system_network.csv"; DISK_CSV="$TIMESERIES_DIR/system_disk.csv"
PG_ACTIVITY_CSV="$TIMESERIES_DIR/pg_activity.csv"; PG_STATS_CSV="$TIMESERIES_DIR/pg_stats.csv"
: > "$LOG_FILE"
BG_PIDS=(); BG_NAMES=(); BG_START_ISO=(); BG_START_MS=(); FINALIZED=0; PG_CONN_ARGS=(); PGPASSWORD_SET=0
ORIGINAL_PGOPTIONS_SET=0; ORIGINAL_PGOPTIONS=""; ORIGINAL_PGAPPNAME_SET=0; ORIGINAL_PGAPPNAME=""
if [ "${PGOPTIONS+x}" = x ]; then ORIGINAL_PGOPTIONS_SET=1; ORIGINAL_PGOPTIONS="$PGOPTIONS"; fi
if [ "${PGAPPNAME+x}" = x ]; then ORIGINAL_PGAPPNAME_SET=1; ORIGINAL_PGAPPNAME="$PGAPPNAME"; fi
export PGOPTIONS="${PGOPTIONS:+$PGOPTIONS }-c default_transaction_read_only=on -c statement_timeout=$((PG_TIMEOUT_SECONDS*1000)) -c lock_timeout=5000"
export PGAPPNAME="pg_inspection_v${COLLECTOR_VERSION}"
HAS_SAR=0; has_cmd sar && HAS_SAR=1
HAS_SADF=0; has_cmd sadf && HAS_SADF=1

log_info "PG 巡检采集器 v${COLLECTOR_VERSION} 启动"
log_info "目标实例 ${PGHOST_ARG}:${PGPORT_ARG}，标准实时采样约 $((SAMPLE_INTERVAL*SAMPLE_COUNT)) 秒"
log_info "采集主机 ${COLLECTOR_HOSTNAME}，主IP ${COLLECTOR_PRIMARY_IP}，输出标识 ${pre_tag}"
[ "$TARGET_IS_LOCAL" -eq 1 ] || log_warn "连接目标 ${PGHOST_ARG} 看起来不是本机；系统数据属于采集器主机 ${COLLECTOR_HOSTNAME}"

# 密码处理
PGPASS=""
if [ -n "$PASSWORD_FILE" ]; then
    [ -r "$PASSWORD_FILE" ] || { printf '无法读取密码文件\n' >&2; exit 10; }
    if has_cmd stat; then
        mode=$(stat -c '%a' "$PASSWORD_FILE" 2>/dev/null); [ -n "$mode" ] && [ "$mode" -gt 600 ] 2>/dev/null && log_warn "密码文件权限建议设置为 600"
    fi
    PGPASS=$(head -n 1 "$PASSWORD_FILE")
elif [ -n "$LEGACY_PASSWORD" ]; then
    PGPASS="$LEGACY_PASSWORD"; log_warn "检测到命令行密码，建议改用 --password-file"
elif [ -n "${PGPASSWORD:-}" ]; then
    PGPASS="$PGPASSWORD"
else
    # Unix socket 连接不需要密码（peer 认证）
    case "$PGHOST_ARG" in
        /*) : ;;  # socket 路径，跳过密码输入
        *)
            if [ -t 0 ]; then
                printf 'Enter PostgreSQL password for %s@%s:%s/%s: ' "$PGUSER_ARG" "$PGHOST_ARG" "$PGPORT_ARG" "$PGDATABASE_ARG"
                stty -echo 2>/dev/null; IFS= read -r PGPASS; stty echo 2>/dev/null; printf '\n'
            fi
            ;;
    esac
fi
if [ -n "$PGPASS" ]; then export PGPASSWORD="$PGPASS"; PGPASSWORD_SET=1; fi

PG_CONN_ARGS=(-h "$PGHOST_ARG" -p "$PGPORT_ARG" -U "$PGUSER_ARG" -d "$PGDATABASE_ARG" -v ON_ERROR_STOP=1 -P footer=off)

conn_err="$TMP_DIR/connect.stderr"
pg_exec -c 'SELECT 1' > /dev/null 2> "$conn_err"; rc=$?
if [ "$rc" -ne 0 ]; then
    log_error "PostgreSQL 连接失败: ${PGUSER_ARG}@${PGHOST_ARG}:${PGPORT_ARG}/${PGDATABASE_ARG}"
    cleanup_auth
    cat "$conn_err" >&2
    exit 20
fi
rm -f "$conn_err"
log_info "PostgreSQL 连接成功"

# 能力探测
run_module "pg.capabilities" probe_capabilities
INSTANCE_TAG=$(sanitize_id "${COLLECTOR_HOSTNAME}_${INSTANCE_ADDRESS}_${PGPORT_ARG}")
log_info "实例标识: $INSTANCE_TAG，版本: $PG_VERSION (${PG_VERSION_NUM})"

# 并行：实时采样 + sar 历史 + 系统静态
start_module_bg "realtime_sampling" collect_realtime_samples
start_module_bg "sar_history" collect_sar_history
start_module_bg "system_static" collect_system_static

# 串行：PG 数据模块（避免对生产库造成过高并发）
run_module "pg_basic" collect_pg_basic
run_module "pg_performance" collect_pg_performance
run_module "pg_replication" collect_pg_replication
run_module "pg_objects" collect_pg_objects
run_module "pg_logs_backup" collect_pg_logs_backup
wait_background_modules

# 角色推导 & 清理
derive_role_evidence
cleanup_auth

# 生成输出，安全扫描通过后才打包
generate_collection_status_json
generate_snapshot_json
generate_summary
if security_scan; then
    generate_collection_status_json
    generate_snapshot_json
    generate_summary
    generate_manifest
    rm -rf "$TMP_DIR" "$STATUS_PARTS_DIR"
    create_package || log_warn "回传包生成失败，可直接回传任务目录"
else
    generate_collection_status_json
    generate_snapshot_json
    generate_summary
    log_error "敏感信息扫描未通过，已阻止打包；请查看 logs/security_scan_findings.txt"
    rm -rf "$TMP_DIR" "$STATUS_PARTS_DIR"
fi

FINALIZED=1

printf '\n采集完成\n'
printf '任务目录: %s\n' "$TASK_DIR"
printf '实例角色观察: %s（最终拓扑由 Python 判断）\n' "$ROLE_OBSERVED"
printf '状态明细: %s\n' "$COLLECTION_STATUS_FILE"
printf '摘要: %s\n' "$SUMMARY_FILE"
[ -n "${PACKAGE_FILE:-}" ] && [ -f "$PACKAGE_FILE" ] && printf '回传包: %s\n' "$PACKAGE_FILE"
