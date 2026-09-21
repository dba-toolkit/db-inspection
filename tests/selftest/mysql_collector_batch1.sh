#!/usr/bin/env bash
# 批 1 改动自测：只抽取被测函数，配桩运行，不动生产脚本、不连数据库。
# 用法: bash tests/selftest/mysql_collector_batch1.sh

# 本脚本位于 tests/selftest/，两级往上是仓库根；被采集脚本在仓库根的 inspection/ 下。
SELF_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SELF_DIR/../.." && pwd)"
SRC="$REPO_ROOT/inspection/mysql_inspection_standard.sh"
# 用**脚本同级的绝对路径**做临时目录：本机 MSYS2 下 /tmp 的进程替换 FIFO 与沙箱交互
# 会偶发卡死；项目内绝对路径稳定，且目录名满足被测函数对"绝对路径"的判断（/ 开头）。
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

_extract_fn "$W/cnf.sh" _cnf_parse_includes _cnf_expand_chain collect_mycnf_allowlist
grep '^CNF_ALLOWLIST_RE=' "$SRC" >> "$W/cnf.sh"
_extract_fn "$W/hdr.sh" known_tsv_header ensure_tsv_header

# ---------- 桩 ----------
TABLES_DIR="$W/tables"; EVIDENCE_DIR="$W/evidence"; TMP_DIR="$W/tmp"
mkdir -p "$TABLES_DIR" "$EVIDENCE_DIR" "$TMP_DIR"
STATUS_OUT="$W/status.tsv"; : > "$STATUS_OUT"

iso_now() { date +'%Y-%m-%dT%H:%M:%S'; }
sanitize_text() { printf '%s' "$1"; }
mysql_scalar() { printf ''; }                      # 不连库
record_status() { printf '%s|%s|%s|rows=%s\n' "$1" "$3" "${10-}" "$7" >> "$STATUS_OUT"; }

source "$W/cnf.sh"
source "$W/hdr.sh"

pass=0; fail=0
ok()   { printf '  \033[32mPASS\033[0m %s\n' "$1"; pass=$((pass+1)); }
bad()  { printf '  \033[31mFAIL\033[0m %s\n' "$1"; fail=$((fail+1)); }
eq()   { if [ "$2" = "$3" ]; then ok "$1"; else bad "$1"; printf '        期望: [%s]\n        实际: [%s]\n' "$3" "$2"; fi; }

echo "==================================================================="
echo " T1  客户那份 /etc/my.cnf 结构（!includedir + [mysql]/[mysqld] 同名 key）"
echo "==================================================================="
ROOT="$W/cnftest"
mkdir -p "$ROOT/etc/my.cnf.d"

# 完全照客户现场的结构写：/etc/my.cnf 里只有 [mysql]/[mysqld]/[client] + !includedir
cat > "$ROOT/etc/my.cnf" <<'EOF'
[mysql]
#default-character-set=utf8mb4
socket=/data/mysql-var/mysql.sock

[mysqld]
user=mysql
port=3306
socket=/data/mysql-var/mysql.sock
basedir=/usr/local/mysql
datadir=/data/mysql-data
lower_case_table_names=1
server-id=34
log-bin=/data/mysql-data/mysql-bin
relay_log=relay_bin
#log_slave_updates=on
gtid_mode=on
enforce_gtid_consistency=on
binlog_format=row
skip-name-resolve
character-set-server=utf8mb4
collation-server=utf8mb4_general_ci
transaction-isolation=READ-UNCOMMITTED
max_connections=16000
max_allowed_packet=200M
default-authentication-plugin=mysql_native_password
replica_skip_errors=1032,1062
read_only = 0
pid-file=/data/mysql-var/mysqld.pid
NOT_IN_WHITELIST=whatever

[client]
port= 3306
socket=/data/mysql-var/mysql.sock
!includedir /etc/my.cnf.d
EOF
# 把 includedir 指向测试目录
sed -i "s#!includedir /etc/my.cnf.d#!includedir $ROOT/etc/my.cnf.d#" "$ROOT/etc/my.cnf"

# includedir 里两个文件，字典序 abc 先于 zzz；zzz 覆盖 innodb_buffer_pool_size
cat > "$ROOT/etc/my.cnf.d/aaa-server.cnf" <<'EOF'
[mysqld]
innodb_buffer_pool_size=170G
innodb_redo_log_capacity=2G
innodb_log_file_size=100M
tmp_table_size=256M
EOF
cat > "$ROOT/etc/my.cnf.d/zzz-override.cnf" <<'EOF'
[mysqld]
innodb_buffer_pool_size=64G
sql_mode='STRICT_TRANS_TABLES,NO_ZERO_IN_DATE'
EOF

# 让被测函数去读测试路径（只替换"探测路径列表"这一句，awk 程序保持原样）
sed "s#^    paths+=(/etc/my.cnf.*#    paths+=(\"$ROOT/etc/my.cnf\")#" "$W/cnf.sh" > "$W/cnf2.sh"
source "$W/cnf2.sh"

collect_mycnf_allowlist
OUT="$TABLES_DIR/mycnf_allowlist.tsv"

echo "-- mycnf_includes.txt（include 展开顺序）--"
cat -A "$EVIDENCE_DIR/mycnf_includes.txt" | sed 's/\$$//'
echo "-- mycnf_allowlist.tsv --"
cat "$OUT"

hdr=$(head -1 "$OUT")
eq "T1.1 表头为 4 列 source_file/section/parameter/configured_value" \
   "$hdr" "$(printf 'source_file\tsection\tparameter\tconfigured_value')"

n_bp=$(awk -F'\t' 'NR>1 && $3=="innodb_buffer_pool_size"{print $4}' "$OUT" | tr '\n' ',')
eq "T1.2 includedir 里的 innodb_buffer_pool_size 被采到（旧版为 0 行）" "$n_bp" "170G,64G,"

n_redo=$(awk -F'\t' 'NR>1 && $3=="innodb_redo_log_capacity"{print $4}' "$OUT")
eq "T1.3 includedir 里的 innodb_redo_log_capacity 被采到（旧版为 0 行）" "$n_redo" "2G"

n_port=$(awk -F'\t' 'NR>1 && $3=="port"{c++}END{print c+0}' "$OUT")
eq "T1.4 [client] 与 [mysqld] 的 port 不再重复输出（只留 [mysqld] 一行）" "$n_port" "1"

sec_port=$(awk -F'\t' 'NR>1 && $3=="port"{print $2}' "$OUT")
eq "T1.5 保留的 port 来自 [mysqld]" "$sec_port" "mysqld"

n_sock=$(awk -F'\t' 'NR>1 && $3=="socket"{c++}END{print c+0}' "$OUT")
eq "T1.6 socket 同理只留 1 行（[mysql] 的那行被过滤）" "$n_sock" "1"

n_ti=$(awk -F'\t' 'NR>1 && $3=="transaction_isolation"{print $4}' "$OUT")
eq "T1.7 连字符 key 归一：transaction-isolation → transaction_isolation" "$n_ti" "READ-UNCOMMITTED"

n_snr=$(awk -F'\t' 'NR>1 && $3=="skip_name_resolve"{print $4}' "$OUT")
eq "T1.8 无值开关 skip-name-resolve 记为 1" "$n_snr" "1"

n_ro=$(awk -F'\t' 'NR>1 && $3=="read_only"{print $4}' "$OUT")
eq "T1.9 带空格的 read_only = 0 取值正确" "$n_ro" "0"

n_pid=$(awk -F'\t' 'NR>1 && $3=="pid_file"{print $4}' "$OUT")
eq "T1.10 pid-file → pid_file" "$n_pid" "/data/mysql-var/mysqld.pid"

n_dap=$(awk -F'\t' 'NR>1 && $3=="default_authentication_plugin"{print $4}' "$OUT")
eq "T1.11 default-authentication-plugin（旧白名单漏项）被采到" "$n_dap" "mysql_native_password"

n_map=$(awk -F'\t' 'NR>1 && $3=="max_allowed_packet"{print $4}' "$OUT")
eq "T1.12 max_allowed_packet（旧白名单漏项）被采到" "$n_map" "200M"

n_rse=$(awk -F'\t' 'NR>1 && $3=="replica_skip_errors"{print $4}' "$OUT")
eq "T1.13 replica_skip_errors（旧白名单漏项）被采到" "$n_rse" "1032,1062"

n_lbin=$(awk -F'\t' 'NR>1 && $3=="log_bin"{print $4}' "$OUT")
eq "T1.14 log-bin → log_bin" "$n_lbin" "/data/mysql-data/mysql-bin"

n_bad=$(awk -F'\t' 'NR>1 && $3=="not_in_whitelist"{c++}END{print c+0}' "$OUT")
eq "T1.15 白名单外的参数不进包" "$n_bad" "0"

n_all=$(awk -F'\t' 'END{print NR-1}' "$OUT")
if [ "$n_all" -ge 15 ]; then ok "T1.16 总行数 $n_all（旧版在 RHEL 结构下为 0）"; else bad "T1.16 总行数仅 $n_all"; fi

st=$(grep '^system.mycnf_allowlist|' "$STATUS_OUT" | tail -1)
case "$st" in *"|ok|"*"rows=$n_all") ok "T1.17 有数据时状态为 ok（rows=$n_all）" ;; *) bad "T1.17 状态异常: $st" ;; esac

echo
echo "==================================================================="
echo " T2  空白名单必须写 empty（旧版写 ok = 假 OK）"
echo "==================================================================="
ROOT2="$W/empty"; mkdir -p "$ROOT2/etc"
cat > "$ROOT2/etc/my.cnf" <<'EOF'
[mysqld]
not_a_whitelisted_param=1
another_unknown=2
EOF
: > "$STATUS_OUT"
sed "s#^    paths+=(/etc/my.cnf.*#    paths+=(\"$ROOT2/etc/my.cnf\")#" "$W/cnf.sh" > "$W/cnf3.sh"
source "$W/cnf3.sh"
collect_mycnf_allowlist
st=$(cat "$STATUS_OUT")
case "$st" in *"|empty|"*) ok "T2.1 rows=0 时状态写 empty 而不是 ok" ;; *) bad "T2.1 状态应为 empty，实际: $st" ;; esac
case "$st" in *"rows=0") ok "T2.2 rows 计数为 0" ;; *) bad "T2.2 rows 计数错误: $st" ;; esac
grep -q 'mycnf_includes.txt' <<<"$st" && ok "T2.3 reason 指路 evidence/mycnf_includes.txt" || bad "T2.3 reason 未指路"

echo
echo "==================================================================="
echo " T3  include 指令解析（!include vs !includedir 不能混淆）"
echo "==================================================================="
cat > "$W/inc.cnf" <<EOF
# comment
; semicolon comment
!include $ROOT/etc/my.cnf.d/aaa-server.cnf
!includedir $ROOT/etc/my.cnf.d    # 行尾注释
!include /tmp/relative-test.cnf
EOF
got=$(_cnf_parse_includes "$W/inc.cnf" | tr '\n' '|')
eq "T3.1 !includedir 不被误判成 !include，且剥掉行尾注释" \
   "$got" "$(printf 'file\t%s|dir\t%s|file\t/tmp/relative-test.cnf|' "$ROOT/etc/my.cnf.d/aaa-server.cnf" "$ROOT/etc/my.cnf.d")"

echo
echo "T3.2 数据行的字段分隔符必须是 tab（OFS 漏设会让整张表变成空格分隔）"
bad_sep=$(awk -F'\t' 'NR>1 && NF<4{c++}END{print c+0}' "$OUT")
eq "T3.2 mycnf_allowlist.tsv 每行按 tab 分隔应恰好 4 段以上" "$bad_sep" "0"

echo
echo "==================================================================="
echo " T4  known_tsv_header 分支（F-03 / F-18 / F-20）"
echo "==================================================================="
INCLUDE_LOG_TEXT=0; GR_MEMBER_ROLE_AVAILABLE=0; GR_MEMBER_VERSION_AVAILABLE=0
REPL_WORKER_APPLY_COLS_AVAILABLE=0; REPL_WORKER_LAST_SEEN_AVAILABLE=0

h=$(known_tsv_header /x/processlist.tsv)
eq "T4.1 processlist 表头末列为 SQL_TEXT（旧版写 SQL_SHA256）" "${h##*	}" "SQL_TEXT"
h=$(known_tsv_header /x/long_transactions.tsv)
eq "T4.2 long_transactions 表头末列为 query_sample（旧版写 query_sha256）" "${h##*	}" "query_sample"
h=$(known_tsv_header /x/sql_digests_top.tsv)
eq "T4.3 sql_digests_top 补上 digest_text" "${h##*	}" "digest_text"
h=$(known_tsv_header /x/replication_channels.tsv)
eq "T4.4 末列仍是 LAST_ERROR_TIMESTAMP（列序未被 F-06 打乱）" "${h##*	}" "LAST_ERROR_TIMESTAMP"
# F-06 之后两条分支合并：无论开关如何，错误消息列名**恒为** LAST_ERROR_MESSAGE。
# 原先"关闭态用 SHA256 别名"的断言已按设计作废（哈希错误消息的诊断价值为零）。
case "$h" in
  *LAST_ERROR_MESSAGE_SHA256*) bad "T4.5 关闭态仍出现 SHA256 别名（F-06 未生效）" ;;
  *LAST_ERROR_MESSAGE*)         ok  "T4.5 关闭态列名为明文 LAST_ERROR_MESSAGE" ;;
  *)                            bad "T4.5 关闭态列名错误: $h" ;;
esac

INCLUDE_LOG_TEXT=1
h=$(known_tsv_header /x/replication_channels.tsv)
case "$h" in *LAST_ERROR_MESSAGE_SHA256*) bad "T4.6 开启态仍含 SHA256 别名（F-18 未修好）" ;; *) ok "T4.6 开启态改为明文 LAST_ERROR_MESSAGE" ;; esac
eq "T4.6b 开关不影响列名（两次表头完全一致）" "$h" "$(INCLUDE_LOG_TEXT=0 known_tsv_header /x/replication_channels.tsv)"
INCLUDE_LOG_TEXT=0

n=$(known_tsv_header /x/replication_workers.tsv | awk -F'\t' '{print NF}')
eq "T4.7 5.7 列集（无 LAST_SEEN 无 APPLY）：7 列" "$n" "7"
REPL_WORKER_LAST_SEEN_AVAILABLE=1
n=$(known_tsv_header /x/replication_workers.tsv | awk -F'\t' '{print NF}')
eq "T4.8 5.7 列集（+LAST_SEEN）：8 列" "$n" "8"
REPL_WORKER_LAST_SEEN_AVAILABLE=0; REPL_WORKER_APPLY_COLS_AVAILABLE=1
n=$(known_tsv_header /x/replication_workers.tsv | awk -F'\t' '{print NF}')
eq "T4.9 8.0 列集（+APPLY_COLS）：9 列" "$n" "9"

echo
echo "T4.12 SQL 列数必须与表头列数一致（F-03/F-18/F-20 的核心不变量）"
sqlraw=$(grep -o 'SELECT CHANNEL_NAME,WORKER_ID,THREAD_ID,SERVICE_STATE\${w_mid},LAST_ERROR_NUMBER\${w_err},LAST_ERROR_TIMESTAMP\${w_post} FROM' "$SRC" | head -1)
if [ -z "$sqlraw" ]; then bad "T4.12 没抓到 workers 的 SELECT 列表（SQL 可能已被改动）"; else
  chk_sql() {
    local s="${sqlraw}"
    s=${s//'${w_mid}'/$1}; s=${s//'${w_post}'/$2}; s=${s//'${w_err}'/,ERR}
    s=${s% FROM}
    local n; n=$(printf '%s' "$s" | awk -F, '{print NF}')
    eq "T4.12 workers SQL 列数=$3（mid=[$1] post=[$2]）" "$n" "$3"
  }
  chk_sql "" "" 7
  chk_sql ",LAST_SEEN_TRANSACTION" "" 8
  chk_sql "" ",LAST_APPLIED_TRANSACTION,APPLYING_TRANSACTION" 9
fi

n=$(known_tsv_header /x/group_replication_members.tsv | awk -F'\t' '{print NF}')
eq "T4.10 5.7 GR members：5 列（旧版硬编码 7 列）" "$n" "5"
GR_MEMBER_ROLE_AVAILABLE=1
n=$(known_tsv_header /x/group_replication_members.tsv | awk -F'\t' '{print NF}')
eq "T4.11 8.0 GR members：6 列" "$n" "6"

echo
echo "==================================================================="
printf ' 结果: \033[32m%d passed\033[0m, %s failed\n' "$pass" "$([ "$fail" -eq 0 ] && printf '\033[32m0\033[0m' || printf '\033[31m%s\033[0m' "$fail")"
echo "==================================================================="
[ "$fail" -eq 0 ]
