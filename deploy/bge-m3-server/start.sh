#!/usr/bin/env bash
# 后台启动 / 停止 / 查看 bge-m3-server
#   ./start.sh          启动（日志写 server.log）
#   ./start.sh stop     停止
#   ./start.sh status   状态
set -euo pipefail
cd "$(dirname "$0")"
PORT="${PORT:-8100}"

pid() { lsof -t -nP -iTCP:"$PORT" -sTCP:LISTEN 2>/dev/null || true; }

case "${1:-start}" in
  stop)
    p=$(pid); [ -n "$p" ] && kill $p && echo "已停止 (pid $p)" || echo "未运行" ;;
  status)
    p=$(pid); [ -n "$p" ] && { echo "运行中 (pid $p)"; curl -s "http://127.0.0.1:$PORT/health"; echo; } || echo "未运行" ;;
  start)
    [ -n "$(pid)" ] && { echo "已在运行 (pid $(pid))"; exit 0; }
    nohup uv run server.py > server.log 2>&1 &
    echo "启动中，日志：$(pwd)/server.log"
    for _ in $(seq 60); do
      grep -q "Uvicorn running" server.log 2>/dev/null && { echo "就绪：http://127.0.0.1:$PORT"; exit 0; }
      grep -q "Traceback" server.log 2>/dev/null && { tail -20 server.log; exit 1; }
      sleep 1
    done
    echo "60 秒内未就绪，请查看 server.log" ;;
esac
