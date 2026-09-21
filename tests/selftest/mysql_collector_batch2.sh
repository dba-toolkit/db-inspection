#!/usr/bin/env bash
# 批 2 改动自测（F-04 脱敏闸门 / F-06 错误原文 / 顺带修的重复列 bug / F-05 退出码）。
# 只抽取被测函数，配桩运行，不动生产脚本、不连数据库。
# 用法: bash tests/selftest/mysql_collector_batch2.sh

# 本脚本位于 tests/selftest/，两级往上是仓库根；被采集脚本在仓库根的 inspection/ 下。
SELF_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SELF_DIR/../.." && pwd)"
SRC="$REPO_ROOT/inspection/mysql_inspection_standard.sh"
# 同批 1：临时目录放脚本同级绝对路径，避开 MSYS2 下 /tmp 进程替换偶发卡死。
W="$(mktemp -d "$SELF_DIR/.st.XXXXXX")"
trap 'rm -rf "$W"' EXIT

# ---------- 抽取被测函数（按函数名 + 大括号配平，不用行号，改脚本不会失效） ----------
_extract_fn() {
    local out="$1"; shift
    : > "$out"
    local fn s e
    for fn in "$@"; do
        s=$(grep -n "^${fn}() {" "$SRC" | head -1 | cut -d: -f1)
        if [ -z "$s" ]; then printf '找不到函数 %s\n' "$fn" >&2; return 1; fi
        e=$(awk -v s="$s" 'NR>s && /^\}$/{print NR; exit}' "$SRC")
        sed -n "${s},${e}p" "$SRC" >> "$out"
        printf '\n' >> "$out"
    done
}

_extract_fn "$W/fns.sh" filter_sensitive_variables sanitize_tsv_columns known_tsv_header

TABLES_DIR="$W/tables"; EVIDENCE_DIR="$W/evidence"; TMP_DIR="$W/tmp"
mkdir -p "$TABLES_DIR" "$EVIDENCE_DIR" "$TMP_DIR"
INCLUDE_LOG_TEXT=0

source "$W/fns.sh"

pass=0; fail=0
ok()   { printf '  \033[32mPASS\033[0m %s\n' "$1"; pass=$((pass+1)); }
bad()  { printf '  \033[31mFAIL\033[0m %s\n' "$1"; fail=$((fail+1)); }
eq()   { if [ "$2" = "$3" ]; then ok "$1"; else bad "$1"; printf '        期望: [%s]\n        实际: [%s]\n' "$3" "$2"; fi; }
has()  { if printf '%s' "$1" | grep -qF "$2"; then ok "$3"; else bad "$3"; printf '        未找到: [%s]\n' "$2"; fi; }
not()  { if printf '%s' "$1" | grep -qF "$2"; then bad "$3"; printf '        不应出现: [%s]\n' "$2"; else ok "$3"; fi; }

echo "==================================================================="
echo " T5  F-04 回退路径脱敏闸门 filter_sensitive_variables"
echo "==================================================================="
GV="$W/tables/global_variables.tsv"
{
  printf 'VARIABLE_NAME\tVARIABLE_VALUE\n'
  # ① 策略类：必须保留（删掉等于把巡检结论一起删了）
  printf 'default_password_lifetime\t0\n'
  printf 'password_history\t0\n'
  printf 'validate_password_policy\tMEDIUM\n'
  printf 'validate_password_length\t8\n'
  # ③ 真凭据：必须删
  printf 'wsrep_sst_auth\trepl:Secret123\n'
  printf 'server_ssl_key\t/etc/mysql/server.key\n'
  printf 'rsa_public_key_file\t/var/lib/mysql/public_key.pem\n'
  # 不是凭据：必须保留
  printf 'wsrep_sst_receive_address\t192.168.1.20:4444\n'
  printf 'max_connections\t800\n'
  printf 'slow_query_log\tON\n'
} > "$GV"
cp "$GV" "$W/gv.before"

filter_sensitive_variables "$GV"
kept=$(cat "$GV")

has "$kept" 'default_password_lifetime	0'  "T5.1 策略变量 default_password_lifetime 保留"
has "$kept" 'password_history	0'           "T5.2 策略变量 password_history 保留"
has "$kept" 'validate_password_policy'      "T5.3 策略变量 validate_password_policy 保留"
not "$kept" 'wsrep_sst_auth'                "T5.4 wsrep_sst_auth（user:pass）已删除"
not "$kept" 'server_ssl_key'                "T5.5 server_ssl_key 已删除"
not "$kept" 'rsa_public_key_file'           "T5.6 rsa_public_key_file 已删除"
has "$kept" 'wsrep_sst_receive_address'     "T5.7 wsrep_sst_receive_address 保留（监听地址不是凭据）"
has "$kept" 'max_connections	800'          "T5.8 普通变量不受影响"
eq "T5.9 行数 11→8（表头 + 7 行）" "$(wc -l < "$GV" | tr -d ' ')" "8"

RF="$EVIDENCE_DIR/redacted_findings.txt"
eq "T5.10 留痕文件带表头" "$(head -1 "$RF")" "variable_name	value_length	contains_colon"
eq "T5.11 留痕 3 条" "$(tail -n +2 "$RF" | wc -l | tr -d ' ')" "3"
has "$(cat "$RF")" 'wsrep_sst_auth	14	yes' "T5.12 wsrep_sst_auth 记长度+含冒号"
not "$(cat "$RF")" 'Secret123'               "T5.13 留痕里**不含**口令明文"

# 二次调用不应重复留痕（幂等）
filter_sensitive_variables "$GV"
eq "T5.14 再跑一次幂等" "$(tail -n +2 "$RF" | wc -l | tr -d ' ')" "3"

echo
echo "==================================================================="
echo " T6  sanitize_tsv_columns 重复列 bug（wanted 里 Source_UUID/Master_UUID 各出现两次）"
echo "==================================================================="
RS="$W/tables/replica_status.tsv"
{
  printf 'Channel_Name\tMaster_Host\tMaster_Port\tMaster_UUID\tSlave_IO_Running\tLast_Errno\tLast_IO_Errno\tLast_IO_Error\tAuto_Position\n'
  printf '""\t192.168.1.10\t3306\tuuid-aaaa\tNo\t0\t13114\tGot fatal error 1236\n\t1\n'
} > "$RS"
WANT="Channel_Name Source_Host Master_Host Source_Port Master_Port Source_UUID Master_UUID Connect_Retry Last_Errno Last_Error Last_IO_Errno Last_IO_Error Last_SQL_Errno Last_SQL_Error Auto_Position"
sanitize_tsv_columns "$RS" "$WANT"
hdr=$(head -1 "$RS")
eq "T6.1 Master_UUID 只出现一次（旧版会出现两次）" "$(printf '%s' "$hdr" | tr '\t' '\n' | grep -cx 'Master_UUID')" "1"
# 命中 8 列：Channel_Name / Master_Host / Master_Port / Master_UUID /
#           Last_Errno / Last_IO_Errno / Last_IO_Error / Auto_Position
eq "T6.2 保留列数 = 8（命中 wanted 的 8 个）" "$(printf '%s' "$hdr" | awk -F'\t' '{print NF}')" "8"
eq "T6.3 数据行列数与表头一致" "$(sed -n 2p "$RS" | awk -F'\t' '{print NF}')" "8"
has "$hdr" 'Last_IO_Error' "T6.4 F-06 白名单已带 Last_IO_Error"
ncols=$(printf '%s' "$WANT" | tr ' ' '\n' | sort -u | wc -l | tr -d ' ')
eq "T6.5 白名单去重后 15 列（原 17 含 2 个重复）" "$ncols" "15"

echo
echo "==================================================================="
echo " T7  F-06 占位表头 & 与**真实**白名单的一致性（Python 侧 schema 不变量）"
echo "==================================================================="
# 从生产脚本里抓真实白名单，别用测试里手写的裁剪版
RS_LINE=$(grep -n 'sanitize_tsv_columns "\$TABLES_DIR/replica_status.tsv"' "$SRC" | head -1 | cut -d: -f1)
REAL_WANT=$(sed -n "${RS_LINE}p" "$SRC" | sed -E 's/.*replica_status\.tsv" "//; s/"[[:space:]]*$//')
if [ -z "$REAL_WANT" ]; then bad "T7.0 没抓到真实白名单"; fi
for c in Last_Error Last_IO_Error Last_SQL_Error; do
    has "$REAL_WANT" "$c" "T7.0b 真实白名单含 $c"
done
ph=$(known_tsv_header /x/replica_status.tsv)
eq "T7.1 replica_status 占位表头 14 列" "$(printf '%s' "$ph" | awk -F'\t' '{print NF}')" "14"
has "$ph" 'Last_Error'    "T7.2 占位表头含 Last_Error"
has "$ph" 'Last_IO_Error' "T7.3 占位表头含 Last_IO_Error"
has "$ph" 'Last_SQL_Error' "T7.4 占位表头含 Last_SQL_Error"

# 核心不变量：占位表头的每一列都必须能在真实白名单里找到，
# 否则空结果时写进去的列名，在正常结果时会被 sanitize 整列丢掉 —— schema 漂移。
miss=$(printf '%s' "$ph" | tr '\t' '\n' | while IFS= read -r c; do
    printf '%s' "$REAL_WANT" | tr ' ' '\n' | grep -qix "$c" || printf '%s ' "$c"
done)
eq "T7.5 占位表头 ⊆ 真实白名单（无漂移）" "$miss" ""
# 反向多确认一次：真实白名单里属于这两套命名的列，加起来正好是占位表头那 14 列
eq "T7.5b 占位表头 14 列且在白名单中唯一" "$(printf '%s' "$ph" | tr '\t' '\n' | sort -u | wc -l | tr -d ' ')" "14"

for t in replication_channels replication_workers group_replication_members; do
    bh=$(known_tsv_header "/x/$t.tsv")
    m=$(printf '%s' "$bh" | tr '\t' '\n' | while IFS= read -r c; do
        case " $WANT " in *" $c "*) ;; *) printf '%s ' "$c" ;; esac
    done)
    printf '  (info) %-28s 列数=%s\n' "$t" "$(printf '%s' "$bh" | awk -F'\t' '{print NF}')"
done
wc=$(known_tsv_header /x/replication_workers.tsv | awk -F'\t' '{print NF}')
eq "T7.6 5.7 workers 占位表头 7 列（无 LAST_SEEN/APPLY）" "$wc" "7"
REPL_WORKER_LAST_SEEN_AVAILABLE=1
wc=$(known_tsv_header /x/replication_workers.tsv | awk -F'\t' '{print NF}')
eq "T7.7 5.7+LAST_SEEN 占位表头 8 列" "$wc" "8"
REPL_WORKER_APPLY_COLS_AVAILABLE=1; REPL_WORKER_LAST_SEEN_AVAILABLE=0
wc=$(known_tsv_header /x/replication_workers.tsv | awk -F'\t' '{print NF}')
eq "T7.8 8.0 占位表头 9 列" "$wc" "9"

echo
echo "==================================================================="
echo " T8  F-05 退出码契约（静态核查）"
echo "==================================================================="
body=$(cat "$SRC")
has "$body" 'exit 30' "T8.1 敏感拦截 exit 30"
has "$body" 'exit 31' "T8.2 打包失败 exit 31"
has "$body" 'trap cleanup_auth EXIT' "T8.3 EXIT 兜底 trap"
has "$body" 'AUTH_TMP_DIR=$(mktemp -d' "T8.4 认证目录建在任务目录之外"
not "$body" 'mktemp "$TMP_DIR/.mysql_defaults' "T8.5 旧的任务目录内 mktemp 已移除"
not "$body" 'SHA2(' "T8.6 SHA2 已彻底移除"

# security_scan 必须在 generate_collection_status_json 之前调用
ls_line=$(grep -n '^security_scan; SCAN_RC=' "$SRC" | head -1 | cut -d: -f1)
gc_line=$(grep -n '^generate_collection_status_json$' "$SRC" | head -1 | cut -d: -f1)
if [ -n "$ls_line" ] && [ -n "$gc_line" ] && [ "$ls_line" -lt "$gc_line" ]; then
    ok "T8.7 security_scan（L$ls_line）在 generate_collection_status_json（L$gc_line）之前，单遍统计成立"
else
    bad "T8.7 security_scan 未前置（scan=L${ls_line:-?} gen=L${gc_line:-?}）"
fi
eq "T8.8 generate_collection_status_json 全脚本只调用一次" "$(grep -c '^generate_collection_status_json$' "$SRC")" "1"
eq "T8.9 generate_summary 全脚本只调用一次" "$(grep -c '^generate_summary$' "$SRC")" "1"

echo
echo "==================================================================="
printf ' 结果: \033[32m%d passed\033[0m, %s failed\n' "$pass" "$([ "$fail" -eq 0 ] && printf '\033[32m0\033[0m' || printf '\033[31m%s\033[0m' "$fail")"
echo "==================================================================="
[ "$fail" -eq 0 ]
