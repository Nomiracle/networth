#!/usr/bin/env bash
# 前端构建：优先复用已有 node_modules；没有则要求先 npm install。
#   npm install
#   NODE_MODULES_DIR=/path/to/node_modules bash scripts/build.sh   # 复用别处已装好的依赖
set -euo pipefail
cd "$(dirname "$0")/.."
if [ ! -e node_modules ]; then
  if [ -n "${NODE_MODULES_DIR:-}" ]; then
    ln -s "$NODE_MODULES_DIR" node_modules
  else
    echo "缺少 frontend/node_modules：请先 npm install，或设置 NODE_MODULES_DIR" >&2
    exit 1
  fi
fi
echo "== 类型检查 =="
node node_modules/vue-tsc/bin/vue-tsc.js --noEmit -p tsconfig.json
echo "== 构建 =="
node node_modules/vite/bin/vite.js build
echo "== 产物 =="
ls -la dist
du -sh dist
