#!/usr/bin/env bash
# 把 deploy/ 下的模板按 deploy/local.env 渲染成可直接安装的配置 → deploy/rendered/（不入库）。
#
# 为什么要有这一步：仓库里的 unit 与 vhost 只保留占位符（<PROJECT_ROOT> 这类），
# 机器相关值（真实域名、真实路径、证书路径）只存在于使用者本机的 deploy/local.env。
# 这样「线上跑的东西」与「仓库里的东西」只差一次渲染，且真实值永不入库。
#
# 占位符：<PROJECT_ROOT> <BACKUP_DIR> <NGINX_CONF_FILE> <DOMAIN> <BASE_DOMAIN>
#        <WEB_ROOT> <API_PORT> <HTTP_PORT> <CERT_PATH> <KEY_PATH>
#        <SSL_EXTRA_1> <SSL_EXTRA_2>（整行占位：值为空则整行被删掉，不残留空行）
# 注意：值里不要出现 sed 的分隔符 `|`，也不要用替换串里的 `&`。
set -euo pipefail
cd "$(dirname "$0")/.."

[ -f deploy/local.env ] || { echo '缺少 deploy/local.env（可复制 deploy/local.env.example）' >&2; exit 1; }
set -a; . deploy/local.env; set +a

: "${PROJECT_ROOT:?deploy/local.env 缺 PROJECT_ROOT}"
: "${DOMAIN:?deploy/local.env 缺 DOMAIN}"
: "${WEB_ROOT:?deploy/local.env 缺 WEB_ROOT}"
: "${BACKUP_DIR:?deploy/local.env 缺 BACKUP_DIR}"
API_PORT="${PORT:-8972}"
HTTP_PORT="${HTTP_PORT:-80}"
BASE_DOMAIN="${BASE_DOMAIN:-example.com}"
NGINX_CONF_FILE="${NGINX_CONF_FILE:-/etc/nginx/conf.d/${DOMAIN}.conf}"
CERT_PATH="${CERT_PATH:-/etc/letsencrypt/live/${DOMAIN}/fullchain.pem}"
KEY_PATH="${KEY_PATH:-/etc/letsencrypt/live/${DOMAIN}/privkey.pem}"
SSL_EXTRA_1="${SSL_EXTRA_1:-}"
SSL_EXTRA_2="${SSL_EXTRA_2:-}"

mkdir -p deploy/rendered

# 整行占位（<SSL_EXTRA_*>）：有值 → 按 nginx 缩进插入；空值 → 整行删掉，不残留空行。
# 用数组而不是 $(...) 拼参数：值里含空格时命令替换会按空白拆开，sed 表达式会被拆坏。
EXTRA_SED=()
extra_rule() {  # extra_rule <占位符> <值>
  if [ -n "$2" ]; then
    EXTRA_SED+=(-e "s|^[[:space:]]*$1\$|    $2|")
  else
    EXTRA_SED+=(-e "/^[[:space:]]*$1\$/d")
  fi
}
extra_rule '<SSL_EXTRA_1>' "$SSL_EXTRA_1"
extra_rule '<SSL_EXTRA_2>' "$SSL_EXTRA_2"

render() {  # render <模板> <产物>
  sed -e "s|<PROJECT_ROOT>|${PROJECT_ROOT}|g" \
      -e "s|<BACKUP_DIR>|${BACKUP_DIR}|g" \
      -e "s|<NGINX_CONF_FILE>|${NGINX_CONF_FILE}|g" \
      -e "s|<BASE_DOMAIN>|${BASE_DOMAIN}|g" \
      -e "s|<DOMAIN>|${DOMAIN}|g" \
      -e "s|<WEB_ROOT>|${WEB_ROOT}|g" \
      -e "s|<API_PORT>|${API_PORT}|g" \
      -e "s|<HTTP_PORT>|${HTTP_PORT}|g" \
      -e "s|<CERT_PATH>|${CERT_PATH}|g" \
      -e "s|<KEY_PATH>|${KEY_PATH}|g" \
      "${EXTRA_SED[@]}" \
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
