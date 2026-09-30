#!/usr/bin/env bash
# networth 后端启动脚本（零第三方依赖，仅标准库）。
#
# 读取的环境变量（见 docs/m1-spec.md 第 6 节）：
#   NETWORTH_DB           数据库路径，默认 backend/data/networth.db
#   NETWORTH_SECRET       服务端密钥（未设置时使用本地默认值）
#   NETWORTH_PORT         监听端口，默认 8972
#   NETWORTH_BIND         监听地址，默认 127.0.0.1
#   ALLOW_REGISTRATION    1/0，是否允许注册新账号
#   NETWORTH_M0_OUT       M0 产物目录（自行导入时使用）
#   NETWORTH_WEB_DIST     前端构建产物目录，默认 ../frontend/dist
#   NETWORTH_SKIP_IMPORT  1 = 首次启动不自动生成演示数据
set -euo pipefail

cd "$(dirname "$0")"

: "${NETWORTH_PORT:=8972}"
: "${NETWORTH_BIND:=127.0.0.1}"
export NETWORTH_PORT NETWORTH_BIND

DB_PATH="${NETWORTH_DB:-$(pwd)/data/networth.db}"
export NETWORTH_DB="$DB_PATH"

# 首次启动（数据库不存在）时自动生成演示数据（也可自行导入 M0 产物）
if [ ! -f "$DB_PATH" ] && [ "${NETWORTH_SKIP_IMPORT:-0}" != "1" ]; then
  echo "[run.sh] 数据库不存在，生成演示数据 -> $DB_PATH"
  /usr/bin/python3 ../tools/seed_demo.py --db "$DB_PATH"
fi

exec /usr/bin/python3 -m app.server
