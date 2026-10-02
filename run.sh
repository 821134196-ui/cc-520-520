#!/usr/bin/env bash
# 一条命令启动商品真伪受理系统（自动建库、种子重复码案例）。
set -euo pipefail
cd "$(dirname "$0")"

PORT="${PORT:-8000}"
HOST="${HOST:-127.0.0.1}"

echo "启动中…… 首次运行会自动建库并生成演示案例与虚构凭证图片。"
exec python3 -m app.app --host "$HOST" --port "$PORT" "$@"
