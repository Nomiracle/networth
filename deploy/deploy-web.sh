#!/usr/bin/env bash
# 前端发布：构建 dist 并同步到 nginx 静态目录。
# 静态资源更新不需要 nginx reload；只有 vhost 变更才需要 reload。
#   WEB_ROOT=/var/www/networth bash deploy/deploy-web.sh
set -euo pipefail

PROJ="${PROJECT_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
FE="$PROJ/frontend"
DEST="${WEB_ROOT:?请设置 WEB_ROOT（nginx 静态目录）}"

cd "$FE"
if [ ! -e node_modules ]; then
  if [ -n "${NODE_MODULES_DIR:-}" ]; then
    ln -s "$NODE_MODULES_DIR" node_modules
  else
    echo "缺少 frontend/node_modules：请先 npm install，或设置 NODE_MODULES_DIR" >&2
    exit 1
  fi
fi

echo "== build =="
node node_modules/vite/bin/vite.js build

echo "== publish → $DEST =="
mkdir -p "$DEST"
rsync -a --delete dist/ "$DEST/"
find "$DEST" -maxdepth 2 -type f | head -12
du -sh "$DEST"
echo "done: $(date -Is)"
