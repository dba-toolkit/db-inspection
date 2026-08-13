# Common Linux runtime helpers (logging, timeout, failure classification).
#
# Required globals (set by entry before sourcing):
#   LOG_FILE, OUTPUT_PARENT, MIN_FREE_MB, TASK_DIR
# Plugin hook used by handle_signal:
#   cleanup_auth

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
    if grep -Eqi 'cannot talk to daemon|could not open connection|chronyd.*not running|chrony.*not running|daemon is not running|service.*not (running|active)|No such file or directory.*chrony|command not found.*chrony' "$err_file" 2>/dev/null; then
        printf 'not_enabled'
    elif grep -Eqi 'access denied|command denied|permission denied|requires.*privilege|you need.*privilege' "$err_file" 2>/dev/null; then
        printf 'permission_denied'
    elif grep -Eqi "doesn.t exist|unknown table|unknown system variable|unknown column|not supported|unsupported" "$err_file" 2>/dev/null; then
        printf 'unsupported'
    else
        printf 'error'
    fi
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

handle_signal() {
    cleanup_auth
    if [ -n "${BG_PIDS[*]:-}" ]; then kill "${BG_PIDS[@]}" 2>/dev/null || true; fi
    [ -n "${TASK_DIR:-}" ] && [ -d "$TASK_DIR" ] && printf 'partial\n' > "$TASK_DIR/COLLECTION_INCOMPLETE"
    exit 130
}
