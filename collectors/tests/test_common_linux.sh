#!/usr/bin/env bash
set -u

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMMON="$HERE/common/linux"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

TASK_DIR="$WORK/task"
TABLES_DIR="$TASK_DIR/tables"
TIMESERIES_DIR="$TASK_DIR/timeseries"
HISTORY_DIR="$TASK_DIR/history"
EVIDENCE_DIR="$TASK_DIR/evidence"
LOG_DIR="$TASK_DIR/logs"
MODULE_LOG_DIR="$LOG_DIR/modules"
TMP_DIR="$TASK_DIR/.tmp"
STATUS_PARTS_DIR="$TASK_DIR/.status_parts"
LOG_FILE="$LOG_DIR/collection.log"
OUTPUT_PARENT="$WORK"
MIN_FREE_MB=1
MANIFEST_FILE="$TASK_DIR/manifest.json"
PACKAGE_FILE=""
PACKAGE_VERSION="1.0"
COLLECTOR_VERSION="1.0"
INSTANCE_TAG="test"
DATABASE_TYPE="mysql"
CREATE_PACKAGE=0
AUTH_TMP_PATTERN=".mysql_defaults.*.cnf"

mkdir -p "$TABLES_DIR" "$TIMESERIES_DIR" "$HISTORY_DIR" "$EVIDENCE_DIR" "$MODULE_LOG_DIR" "$TMP_DIR" "$STATUS_PARTS_DIR"
: > "$LOG_FILE"
BG_NAMES=(); BG_START_ISO=(); BG_START_MS=(); BG_PIDS=()

known_tsv_header() { return 1; }
cleanup_auth() { :; }

source "$COMMON/util.sh"
source "$COMMON/runtime.sh"
source "$COMMON/status.sh"
source "$COMMON/security.sh"
source "$COMMON/package.sh"

PASS=0
FAIL=0

expect_eq() {
    local desc="$1" expected="$2" actual="$3"
    if [ "$expected" = "$actual" ]; then
        PASS=$((PASS + 1))
    else
        FAIL=$((FAIL + 1))
        printf 'FAIL: %s\n  expected: %s\n  actual:   %s\n' "$desc" "$expected" "$actual"
    fi
}

expect_ok() {
    local desc="$1" rc="$2"
    if [ "$rc" -eq 0 ]; then PASS=$((PASS + 1)); else FAIL=$((FAIL + 1)); printf 'FAIL: %s (rc=%s)\n' "$desc" "$rc"; fi
}

# util
expect_eq "sanitize_id" "MySQL_System_Host" "$(sanitize_id 'MySQL System/Host')"
expect_eq "json_quote" '"x"' "$(json_quote 'x')"
expect_eq "json_number_or_null int" "3" "$(json_number_or_null 3)"
expect_eq "json_number_or_null null" "null" "$(json_number_or_null abc)"
is_uint 123; expect_ok "is_uint valid" "$?"
is_uint '1a'; [ $? -ne 0 ]; expect_ok "is_uint invalid" "$?"

echo hello > "$WORK/f.txt"
expect_eq "sha256_file" "$(sha256sum "$WORK/f.txt" | awk '{print $1}')" "$(sha256_file "$WORK/f.txt")"

# status
record_status "system.host" "system.static" "ok" "t0" "t1" 5 2 0 "evidence/host.txt" "reason here"
grep -q "system.host" "$STATUS_PARTS_DIR/system.host.tsv"; expect_ok "record_status writes item_id" "$?"
grep -q "reason here" "$STATUS_PARTS_DIR/system.host.tsv"; expect_ok "record_status sanitizes reason" "$?"

# sanitize_tsv_columns keeps only requested columns (case-insensitive)
printf 'A\tB\tC\n1\t2\t3\n' > "$WORK/t.tsv"
sanitize_tsv_columns "$WORK/t.tsv" "a c"
grep -q "^A	C$" "$WORK/t.tsv"; expect_ok "sanitize_tsv_columns header" "$?"
grep -q "^1	3$" "$WORK/t.tsv"; expect_ok "sanitize_tsv_columns rows" "$?"

# security scan
security_scan >/dev/null 2>&1; expect_ok "security_scan clean" "$?"
printf 'password="secret"\n' > "$TASK_DIR/leak.txt"
security_scan >/dev/null 2>&1; [ $? -ne 0 ]; expect_ok "security_scan blocks sensitive hit" "$?"
rm -f "$TASK_DIR/leak.txt"

# manifest
printf 'data\n' > "$TASK_DIR/data.txt"
generate_manifest
grep -q '"database_type":"mysql"' "$MANIFEST_FILE"; expect_ok "manifest database_type" "$?"
grep -q '"sha256"' "$MANIFEST_FILE"; expect_ok "manifest records sha256" "$?"

printf '\nPASS=%s FAIL=%s\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
