#!/bin/bash
# =============================================================================
# ShoppingLog 本地启动脚本（运行数据与源码分离）
#   源码: $HOME/project/ShoppingLog ；运行数据: $HOME/myapp/shoppinglog
#   日常升级推荐直接用: bash scripts/deploy.sh
# =============================================================================
set -euo pipefail

SRC="${SHOPLOG_SRC:-$HOME/project/ShoppingLog}"
APP="${SHOPLOG_APP:-$HOME/myapp/shoppinglog}"
PORT="${SHOPLOG_PORT:-8787}"
# MySQL 模式必须用装了 pymysql 的解释器；PATH 上的 python3 未必有
PY="${SHOPLOG_PY:-$(command -v python3 || echo python3)}"
if [ -f "$APP/config.local.json" ] && grep -q '"engine"[[:space:]]*:[[:space:]]*"mysql"' "$APP/config.local.json"; then
  if ! "$PY" -c "import pymysql" >/dev/null 2>&1; then
    for cand in \
      "$HOME/.local/share/uv/python/cpython-3.14-macos-aarch64-none/bin/python3.14" \
      "$HOME/Library/Application Support/com.tencent.mac.marvis/components/MarvisAgent/Versions/1.0.0.10409/runtime/python311/bin/python3.11" \
      "$(command -v python3.11 || true)"; do
      if [ -n "$cand" ] && [ -x "$cand" ] && "$cand" -c "import pymysql" >/dev/null 2>&1; then PY="$cand"; break; fi
    done
  fi
  "$PY" -c "import pymysql" >/dev/null 2>&1 || { echo "ERROR: engine=mysql 但 $PY 缺少 pymysql，请先安装或设置 SHOPLOG_PY" >&2; exit 1; }
fi

export SHOPLOG_ROOT="$APP"
cd "$SRC"

PID=$(lsof -tiTCP:"$PORT" -sTCP:LISTEN 2>/dev/null || true)
[ -n "$PID" ] && kill "$PID" && sleep 1

mkdir -p "$APP/logs"
SHOPLOG_ROOT="$APP" SHOPLOG_LOG="$APP/logs/server.log" SHOPLOG_CWD="$SRC" \
  "$PY" scripts/start_daemon.py scripts/server.py --port "$PORT" >/dev/null
sleep 2

if curl -sf "http://127.0.0.1:$PORT/api/health" >/dev/null; then
  echo " OK  http://127.0.0.1:$PORT/api/health"
else
  echo " FAIL"
  tail -20 "$APP/logs/server.log" >&2
fi
