# Common manifest and tar.gz packaging.
#
# Required globals:
#   TASK_DIR, STATUS_PARTS_DIR, TMP_DIR, MANIFEST_FILE, PACKAGE_FILE
#   PACKAGE_VERSION, COLLECTOR_VERSION, INSTANCE_TAG, DATABASE_TYPE, CREATE_PACKAGE
#   AUTH_TMP_PATTERN   (e.g. '.mysql_defaults.*.cnf'; empty when not applicable)
# Required shared functions:
#   safe_relpath, json_quote, file_size_bytes, sha256_file, iso_now, epoch_ms

generate_manifest() {
    local list="$TMP_DIR/manifest_files.txt" f rel first=1
    local exclude_auth=()
    [ -n "${AUTH_TMP_PATTERN:-}" ] && exclude_auth=(! -name "$AUTH_TMP_PATTERN")
    find "$TASK_DIR" -type f ! -path "$TMP_DIR/*" ! -path "$STATUS_PARTS_DIR/*" ! -name 'manifest.json' "${exclude_auth[@]}" -print | sort > "$list"
    {
      printf '{\n  "package_version":%s,\n  "collector_version":%s,\n  "database_type":%s,\n  "instance_tag":%s,\n  "created_at":%s,\n  "files":[\n' \
        "$(json_quote "$PACKAGE_VERSION")" "$(json_quote "$COLLECTOR_VERSION")" "$(json_quote "$DATABASE_TYPE")" "$(json_quote "$INSTANCE_TAG")" "$(json_quote "$(iso_now)")"
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
    local exclude_tar=()
    parent=$(dirname "$TASK_DIR"); base=$(basename "$TASK_DIR"); PACKAGE_FILE="${parent}/${base}.tar.gz"
    err_file="${PACKAGE_FILE}.stderr.tmp"
    start_ms=$(epoch_ms)
    [ -n "${AUTH_TMP_PATTERN:-}" ] && exclude_tar=(--exclude="*/$AUTH_TMP_PATTERN")
    tar "${exclude_tar[@]}" -C "$parent" -czf "$PACKAGE_FILE" "$base" 2> "$err_file"; rc=$?
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
