# Common Linux collection utilities (database-agnostic).
#
# Sourced by a database collector entry.  This module has no plugin hooks and
# only relies on standard POSIX/GNU tools available on a Linux target.

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
    case "$host" in 127.*|localhost|::1) printf '127.0.0.1'; return 0 ;; esac
    if printf '%s' "$host" | grep -Eq '^[0-9]+([.][0-9]+){3}$'; then printf '%s' "$host"; return 0; fi
    if has_cmd getent; then v=$(getent ahostsv4 "$host" 2>/dev/null | awk 'NR==1{print $1}'); fi
    [ -z "$v" ] && has_cmd host && v=$(host "$host" 2>/dev/null | awk '/has address/{print $NF; exit}')
    printf '%s' "$v"
}

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

tsv_first_value() {
    local file="$1"; shift
    [ -s "$file" ] || return 0
    awk -F'\t' -v names="$*" '
      NR==1 {n=split(names,w," "); for(i=1;i<=NF;i++) for(j=1;j<=n;j++) if(tolower($i)==tolower(w[j])) col=i; next}
      NR==2 && col>0 {print $col; exit}' "$file"
}

sanitize_tsv_columns() {
    local file="$1" wanted="$2" tmp
    tmp="$file.safe"
    [ -s "$file" ] || return 0
    awk -F'\t' -v OFS='\t' -v wanted="$wanted" '
      BEGIN{n=split(wanted,w," ")}
      NR==1{
        for(i=1;i<=NF;i++){
          h=tolower($i)
          for(j=1;j<=n;j++) if(h==tolower(w[j])) keep[++k]=i
        }
        if(k==0) exit 2
        for(x=1;x<=k;x++) printf "%s%s",(x>1?OFS:""),$keep[x]
        printf "\n"; next
      }
      {
        for(x=1;x<=k;x++) printf "%s%s",(x>1?OFS:""),$keep[x]
        printf "\n"
      }' "$file" > "$tmp"
    if [ $? -eq 0 ] && [ -s "$tmp" ]; then mv "$tmp" "$file"; else rm -f "$tmp"; fi
}
