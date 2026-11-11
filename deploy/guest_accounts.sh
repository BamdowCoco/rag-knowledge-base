#!/usr/bin/env bash
#
# 公网演示账号管理（在 VPS 仓库根目录执行）。
#
#   bash deploy/guest_accounts.sh init-owner   生成站主账号
#   bash deploy/guest_accounts.sh add          新增一个岗位访客账号
#   bash deploy/guest_accounts.sh list         列出访客用户名
#   bash deploy/guest_accounts.sh remove <用户> 撤销一个访客账号
#
# 账号文件：
#   deploy/caddy/guests.caddy  访客：只能访问 /demo/ 与 /demo/about.html
#   deploy/caddy/owner.caddy   站主：完整前端、运维页、摄入接口
#
# 两个文件都被 gitignore；公司名和密码只写本地台账，不写进仓库。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CADDY_DIR="$ROOT/deploy/caddy"
GUESTS="$CADDY_DIR/guests.caddy"
OWNER="$CADDY_DIR/owner.caddy"
CONTAINER="rag-proxy"

usage() {
  cat <<'EOF'
用法：
  bash deploy/guest_accounts.sh init-owner
  bash deploy/guest_accounts.sh add
  bash deploy/guest_accounts.sh list
  bash deploy/guest_accounts.sh remove <用户名>
EOF
}

ensure_guests() {
  if [[ ! -f "$GUESTS" ]]; then
    echo "缺少 $GUESTS"
    echo "先执行：cp deploy/caddy/guests.example.caddy deploy/caddy/guests.caddy"
    exit 1
  fi
}

hash_password() {
  docker run --rm caddy:2-alpine caddy hash-password --plaintext "$1"
}

reload_proxy() {
  if docker ps --format '{{.Names}}' | grep -qx "$CONTAINER"; then
    docker exec "$CONTAINER" caddy reload --config /etc/caddy/Caddyfile
  else
    echo "提示：$CONTAINER 未运行，配置将在下次启动时生效。"
  fi
}

new_user() {
  printf 'u%s' "$(openssl rand -hex 4)"
}

new_password() {
  # 24 位十六进制：无特殊字符，方便复制到邮件/聊天窗口
  openssl rand -hex 12
}

case "${1:-}" in
  init-owner)
    mkdir -p "$CADDY_DIR"
    if [[ -f "$OWNER" ]]; then
      echo "$OWNER 已存在，拒绝覆盖。要重置请先手工删除。"
      exit 1
    fi
    password="$(new_password)"
    hash="$(hash_password "$password")"
    printf 'owner %s\n' "$hash" > "$OWNER"
    chmod 600 "$OWNER" 2>/dev/null || true
    printf '\n站主账号已写入 %s\n用户名：owner\n密码：%s\n请立即记入本地台账。\n' "$OWNER" "$password"
    reload_proxy
    ;;

  add)
    ensure_guests
    user="$(new_user)"
    password="$(new_password)"
    hash="$(hash_password "$password")"
    printf '%s %s\n' "$user" "$hash" >> "$GUESTS"
    chmod 600 "$GUESTS" 2>/dev/null || true
    printf '\n访客账号已添加\n用户名：%s\n密码：%s\n入口：/demo/\n请记入本地台账。\n' "$user" "$password"
    reload_proxy
    ;;

  list)
    ensure_guests
    awk 'NF && $1 !~ /^#/ {print $1}' "$GUESTS"
    ;;

  remove)
    ensure_guests
    user="${2:-}"
    if [[ -z "$user" ]]; then
      usage
      exit 2
    fi
    if ! awk -v u="$user" 'NF && $1 == u {found=1} END {exit !found}' "$GUESTS"; then
      echo "未找到用户名：$user"
      exit 1
    fi
    remaining="$(awk 'NF && $1 !~ /^#/ {count++} END {print count+0}' "$GUESTS")"
    if [[ "$remaining" -le 1 ]]; then
      echo "拒绝删除最后一个访客账号：Caddy 的 basic_auth 不允许空账号列表。"
      echo "先 add 一个新账号，再删旧的。"
      exit 1
    fi
    tmp="$(mktemp)"
    awk -v u="$user" 'NF && $1 == u {next} {print}' "$GUESTS" > "$tmp"
    mv "$tmp" "$GUESTS"
    chmod 600 "$GUESTS" 2>/dev/null || true
    reload_proxy
    echo "已删除访客账号：$user"
    ;;

  *)
    usage
    exit 2
    ;;
esac
