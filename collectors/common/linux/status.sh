# Common Linux status recording and module orchestration.
#
# Required globals:
#   STATUS_PARTS_DIR, TMP_DIR, MODULE_LOG_DIR, LOG_FILE, TASK_DIR
# Required shared functions:
#   sanitize_id, iso_now, epoch_ms, classify_failure, safe_relpath, log_info, log_warn
# Plugin hook (database-specific TSV headers):
#   known_tsv_header <basename>   -> prints header and returns 0, or returns 1
# Background module state arrays:
#   BG_NAMES, BG_START_ISO, BG_START_MS, BG_PIDS

record_status() {
    local item_id="$1" category="$2" status="$3" started_at="$4" finished_at="$5" duration_ms="$6"
    local row_count="$7" exit_code="$8" output_file="$9" reason="${10-}"
    local f
    f="$STATUS_PARTS_DIR/$(sanitize_id "$item_id").tsv"
    printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
      "$item_id" "$category" "$status" "$started_at" "$finished_at" "$duration_ms" "$row_count" "$exit_code" \
      "$output_file" "$(sanitize_text "$reason")" > "$f"
}

ensure_tsv_header() {
    local file="$1" header
    [ -s "$file" ] && return 0
    header=$(known_tsv_header "$file") || return 1
    printf '%s\n' "$header" > "$file"
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
    elif [ ! -s "$outfile" ]; then status="empty"; fi
    rows=$(wc -l < "$outfile" 2>/dev/null | tr -d ' '); rows=${rows:-0}
    cat "$err" >> "$MODULE_LOG_DIR/$(sanitize_id "$category").log" 2>/dev/null
    rm -f "$err"
    record_status "$item_id" "$category" "$status" "$start_iso" "$end_iso" "$((end_ms-start_ms))" "$rows" "$rc" "$(safe_relpath "$outfile")" "$reason"
    return "$rc"
}

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
