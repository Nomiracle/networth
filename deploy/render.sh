#!/usr/bin/env bash
# 把 deploy/ 下的模板按 deploy/local.env 渲染成可直接安装的配置 → deploy/rendered/（不入库）。
#
# 为什么要有这一步：仓库里的 unit 与 vhost 只保留占位符（<PROJECT_ROOT> 这类），
# 机器相关值（真实域名、真实路径、证书路径）只存在于使用者本机的 deploy/local.env。
# 这样「线上跑的东西」与「仓库里的东西」只差一次渲染，且真实值永不入库。
set -euo pipefail
cd "$(dirname "$0")/.."

[ -f deploy/local.env ] || { echo '缺少 deploy/local.env（可复制 deploy/local.env.example）' >&2; exit 1; }
set -a; . deploy/local.env; set +a

: "${PROJECT_ROOT:?deploy/local.env 缺 PROJECT_ROOT}"
: "${DOMAIN:?deploy/local.env 缺 DOMAIN}"
: "${WEB_ROOT:?deploy/local.env 缺 WEB_ROOT}"
: "${BACKUP_DIR:?deploy/local.env 缺 BACKUP_DIR}"
API_PORT="${PORT:-8972}"
NGINX_CONF_DIR="${NGINX_CONF_DIR:-/etc/nginx/conf.d}"
CERT_PATH="${CERT_PATH:-/etc/letsencrypt/live/${DOMAIN}/fullchain.pem}"
KEY_PATH="${KEY_PATH:-/etc/letsencrypt/live/${DOMAIN}/privkey.pem}"

mkdir -p deploy/rendered

render() {  # render <模板> <产物>
  sed -e "s|<PROJECT_ROOT>|${PROJECT_ROOT}|g" \
      -e "s|<BACKUP_DIR>|${BACKUP_DIR}|g" \
      -e "s|<NGINX_CONF_DIR>|${NGINX_CONF_DIR}|g" \
      -e "s|<DOMAIN>|${DOMAIN}|g" \
      -e "s|<WEB_ROOT>|${WEB_ROOT}|g" \
      -e "s|<API_PORT>|${API_PORT}|g" \
      -e "s|<CERT_PATH>|${CERT_PATH}|g" \
      -e "s|<KEY_PATH>|${KEY_PATH}|g" \
      -e "s|__DOMAIN__|${DOMAIN}|g" \
      -e "s|__WEB_ROOT__|${WEB_ROOT}|g" \
      "$1" > "$2"
}

render deploy/nginx.conf.template                deploy/rendered/nginx.conf
render deploy/nginx-networth-mcp.conf.template   deploy/rendered/nginx-networth-mcp.conf
render deploy/networth-api.service               deploy/rendered/networth-api.service
render deploy/networth-mcp.service               deploy/rendered/networth-mcp.service
render deploy/networth-backup.service            deploy/rendered/networth-backup.service
cp deploy/networth-backup.timer deploy/rendered/networth-backup.timer

# 残留占位符必须报错：带 <...> 上线时 systemd / nginx 会静默用到错误路径
if grep -nE '<[A-Z_]+>|__[A-Z_]+__' deploy/rendered/*; then
  echo '渲染后仍残留占位符：检查 deploy/local.env 是否少了对应键' >&2
  exit 1
fi

echo "已渲染到 deploy/rendered/（该目录不入库）"
