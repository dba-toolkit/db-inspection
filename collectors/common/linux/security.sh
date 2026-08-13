# Common sensitive-content scanner that blocks packaging on high-confidence hits.
#
# Required globals:
#   TASK_DIR, LOG_DIR, TMP_DIR
# Required shared functions:
#   record_status, iso_now

security_scan() {
    local findings="$TMP_DIR/security_scan_findings.txt"
    : > "$findings"
    grep -RInE --exclude='collection.log' --exclude='manifest.json' --exclude='security_scan_findings.txt' \
      '(password[[:space:]]*=[[:space:]]*"?[^<[:space:]";]+|--password(=|[[:space:]])[^<[:space:]]+|BEGIN[[:space:]].*PRIVATE KEY|Authorization:[[:space:]]*(Basic|Bearer)[[:space:]]+[A-Za-z0-9._-]+)' \
      "$TASK_DIR" > "$findings" 2>/dev/null
    if [ -s "$findings" ]; then
        cp "$findings" "$LOG_DIR/security_scan_findings.txt"
        record_status "package.security_scan" "package" "error" "$(iso_now)" "$(iso_now)" 0 "$(wc -l < "$findings")" 1 "logs/security_scan_findings.txt" "high-confidence sensitive pattern detected; package creation blocked"
        return 1
    fi
    record_status "package.security_scan" "package" "ok" "$(iso_now)" "$(iso_now)" 0 0 0 "" ""
    return 0
}
