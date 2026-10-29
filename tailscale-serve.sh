#!/usr/bin/env bash
# 常駐APIを Tailscale serve で tailnet 内へ HTTPS 公開する。
#
#   ./tailscale-serve.sh [up]   トークンを用意し、serve に登録する（何度実行してもよい）
#   ./tailscale-serve.sh down   serve の登録を外す
#   ./tailscale-serve.sh status 登録状況を表示する
#
# サーバー自体は 127.0.0.1 で待ち受けたまま、tailscaled が TLS を終端して転送する。
# 他の serve 設定には触らない（tailscale serve reset は使わない）。
# 公開先の HTTPS ポートは ICLOUD_LOCATE_SERVE_PORT（既定 8765）で変えられる。
set -euo pipefail
cd "$(dirname "$0")"

CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/icloud-locate"
ENV_FILE="${ICLOUD_LOCATE_ENV_FILE:-$CONFIG_DIR/env}"
TOKEN_FILE_DEFAULT="$CONFIG_DIR/token"
SERVICE=icloud-locate

die() { echo "!! $*" >&2; exit 1; }
say() { echo "→ $*" >&2; }

# 設定ファイルから KEY の値を読む（環境変数が優先）
env_value() {
    local key=$1 default=$2 val=${!1:-}
    if [[ -z $val && -f $ENV_FILE ]]; then
        val=$(sed -nE "s/^[[:space:]]*(export[[:space:]]+)?$key[[:space:]]*=[[:space:]]*//p" "$ENV_FILE" \
              | tail -n1 | sed -E "s/^([\"'])(.*)\\1\$/\\2/")
    fi
    echo "${val:-$default}"
}

API_PORT=$(env_value ICLOUD_LOCATE_PORT 8765)
API_HOST=$(env_value ICLOUD_LOCATE_HOST 127.0.0.1)
SERVE_PORT=$(env_value ICLOUD_LOCATE_SERVE_PORT 8765)
TARGET="http://127.0.0.1:$API_PORT"

command -v tailscale >/dev/null || die "tailscale が見つかりません"

dns_name() {
    tailscale status --json | python3 -c 'import json,sys; print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))'
}

# SERVE_PORT に今つながっている転送先を出す（無ければ空）
current_target() {
    tailscale serve status --json | SERVE_PORT=$SERVE_PORT python3 -c '
import json, os, sys
d = json.load(sys.stdin) or {}
port = os.environ["SERVE_PORT"]
for hostport, web in (d.get("Web") or {}).items():
    if hostport.rsplit(":", 1)[-1] == port:
        for h in (web.get("Handlers") or {}).values():
            print(h.get("Proxy") or h.get("Path") or h.get("Text") or "?")
        sys.exit()
if port in (d.get("TCP") or {}):
    print("(TCP転送)")
'
}

ensure_token() {
    local token_file
    token_file=$(env_value ICLOUD_LOCATE_TOKEN_FILE "")
    if [[ -n $token_file ]]; then
        token_file=${token_file/#\~/$HOME}
        [[ -s $token_file ]] || die "ICLOUD_LOCATE_TOKEN_FILE=$token_file が空か存在しません"
        say "既存のトークンを使います ($token_file)"
        TOKEN_FILE=$token_file
        return 1
    fi
    TOKEN_FILE=$TOKEN_FILE_DEFAULT
    mkdir -p "$CONFIG_DIR"
    if [[ ! -s $TOKEN_FILE ]]; then
        (umask 077; python3 -c 'import secrets; print(secrets.token_urlsafe(32))' > "$TOKEN_FILE")
        say "トークンを作りました ($TOKEN_FILE)"
    fi
    (umask 077; touch "$ENV_FILE")
    printf '\n# tailscale-serve.sh が追加\nICLOUD_LOCATE_TOKEN_FILE=%s\n' "$TOKEN_FILE" >> "$ENV_FILE"
    say "$ENV_FILE に ICLOUD_LOCATE_TOKEN_FILE を追記しました"
    return 0
}

restart_server() {
    if systemctl --user is-active --quiet "$SERVICE" 2>/dev/null; then
        systemctl --user restart "$SERVICE"
        say "トークンを有効にするため $SERVICE サービスを再起動しました"
    else
        say "サーバーが systemd で動いていません。serve.sh を（再）起動してトークンを有効にしてください"
    fi
}

cmd_up() {
    [[ $API_HOST == 127.0.0.1 || $API_HOST == localhost ]] \
        || say "注意: ICLOUD_LOCATE_HOST=$API_HOST です。serve 経由なら 127.0.0.1 のままで構いません"

    local cur
    cur=$(current_target)
    if [[ -n $cur && $cur != "$TARGET" ]]; then
        die "HTTPS $SERVE_PORT 番は既に別の転送先 ($cur) に使われています。ICLOUD_LOCATE_SERVE_PORT で別のポートを指定してください"
    fi

    # tailnet 内の誰でも繋がるので、トークンは必ず有効にする
    if ensure_token; then
        restart_server
    fi

    if [[ $cur == "$TARGET" ]]; then
        say "HTTPS $SERVE_PORT 番は登録済みです"
    else
        tailscale serve --bg --https="$SERVE_PORT" "$TARGET" >/dev/null \
            || die "tailscale serve に失敗しました。権限が無い場合は一度だけ: sudo tailscale set --operator=\$USER"
        say "HTTPS $SERVE_PORT 番 → $TARGET を登録しました"
    fi

    local url="https://$(dns_name):$SERVE_PORT"
    cat <<EOF

tailnet 内の端末から:
  curl -s -H "Authorization: Bearer <トークン>" $url/location
このマシンから試すなら:
  curl -s -H "Authorization: Bearer \$(cat $TOKEN_FILE)" $url/health
トークンは $TOKEN_FILE にあります。呼び出す側の端末へは安全な経路で渡してください。
EOF
}

cmd_down() {
    local cur
    cur=$(current_target)
    if [[ -z $cur ]]; then
        say "HTTPS $SERVE_PORT 番には何も登録されていません"
    elif [[ $cur != "$TARGET" ]]; then
        die "HTTPS $SERVE_PORT 番は別の転送先 ($cur) です。触りません"
    else
        tailscale serve --https="$SERVE_PORT" off
        say "HTTPS $SERVE_PORT 番の登録を外しました（トークンとサーバーはそのまま）"
    fi
}

cmd_status() {
    local cur
    cur=$(current_target)
    if [[ $cur == "$TARGET" ]]; then
        echo "公開中: https://$(dns_name):$SERVE_PORT → $TARGET (tailnet only)"
    elif [[ -n $cur ]]; then
        echo "HTTPS $SERVE_PORT 番は別の転送先 ($cur) に使われています"
    else
        echo "未公開 (HTTPS $SERVE_PORT 番は空き)"
    fi
}

case "${1:-up}" in
    up) cmd_up ;;
    down) cmd_down ;;
    status) cmd_status ;;
    *) die "使い方: $0 [up|down|status]" ;;
esac
