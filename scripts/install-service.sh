#!/bin/bash
# =============================================================================
# ShoppingLog 常驻服务安装脚本（macOS LaunchAgent）
#
# 为什么需要它：
#   `run.sh` 用 nohup 启动，退出终端 / 重启机器后进程就没了。
#   这个脚本把服务注册成 LaunchAgent：开机自启、崩溃自动拉起。
#
# 用法（**必须在普通终端里跑**，不要在受限沙箱里跑）：
#   bash scripts/install-service.sh             安装并立即启动
#   bash scripts/install-service.sh --status    查看状态
#   bash scripts/install-service.sh --uninstall 卸载（服务停止，不再自启）
#
# 说明：
#   - 只写 ~/Library/LaunchAgents/com.meria.shoppinglog.plist，不动系统目录
#   - 解释器自动挑选「装了 pymysql 的那个」（MySQL 模式必需）
# =============================================================================
set -euo pipefail

SRC="${SHOPLOG_SRC:-$HOME/project/ShoppingLog}"
APP="${SHOPLOG_APP:-$HOME/myapp/shoppinglog}"
PORT="${SHOPLOG_PORT:-8787}"
LABEL="com.meria.shoppinglog"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

die() { echo "ERROR: $*" >&2; exit 1; }
log() { echo "[service] $*"; }

# ---------- 挑解释器：优先 SHOPLOG_PY，其次找装了 pymysql 的 ----------
pick_python() {
  if [ -n "${SHOPLOG_PY:-}" ] && [ -x "$SHOPLOG_PY" ]; then echo "$SHOPLOG_PY"; return; fi
  local cands=(
    "$(command -v python3 || true)"
    "$HOME/.local/share/uv/python/cpython-3.14-macos-aarch64-none/bin/python3.14" \
    "$HOME/Library/Application Support/com.tencent.mac.marvis/components/MarvisAgent/Versions/1.0.0.10409/runtime/python311/bin/python3.11"
    "$(command -v python3.11 || true)"
    "$(command -v python3.12 || true)"
  )
  local mysql=0
  if [ -f "$APP/config.local.json" ] && grep -q '"engine"[[:space:]]*:[[:space:]]*"mysql"' "$APP/config.local.json"; then
    mysql=1
  fi
  for c in "${cands[@]}"; do
    [ -n "$c" ] && [ -x "$c" ] || continue
    if [ "$mysql" = "1" ]; then
      "$c" -c "import pymysql" >/dev/null 2>&1 && { echo "$c"; return; }
    else
      echo "$c"; return
    fi
  done
  return 1
}

case "${1:-}" in
  --uninstall)
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
    rm -f "$PLIST"
    log "已卸载 ${LABEL}（服务已停止，不会自启）"
    exit 0
    ;;
  --status)
    launchctl print "gui/$(id -u)/$LABEL" 2>/dev/null | grep -E "^\s+(state|pid|last exit code) =" || echo "未安装"
    exit 0
    ;;
esac

PY="$(pick_python)" || die "找不到能用的解释器。MySQL 模式需要 pymysql：用 SHOPLOG_PY=<路径> 指定"
[ -d "$SRC" ] || die "源码目录不存在: $SRC"
[ -f "$SRC/scripts/server.py" ] || die "未找到 $SRC/scripts/server.py"
mkdir -p "$APP/logs" "$HOME/Library/LaunchAgents"

log "源码: $SRC"
log "数据: $APP"
log "端口: $PORT"
log "解释器: $PY"

cat > "$PLIST" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>$PY</string>
        <string>scripts/server.py</string>
        <string>--port</string>
        <string>$PORT</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$SRC</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>SHOPLOG_ROOT</key>
        <string>$APP</string>
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>$APP/logs/server.log</string>
    <key>StandardErrorPath</key>
    <string>$APP/logs/server.log</string>
    <key>ProcessType</key>
    <string>Background</string>
</dict>
</plist>
PLISTEOF

plutil -lint "$PLIST" >/dev/null || die "plist 语法校验失败"

# 先停掉占用端口的旧进程，避免新服务起不来
OLD=$(lsof -tiTCP:"$PORT" -sTCP:LISTEN 2>/dev/null | head -1 || true)
[ -n "$OLD" ] && kill "$OLD" 2>/dev/null && sleep 1 && log "已停止旧进程 $OLD"

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST" || die "launchctl bootstrap 失败（请在普通终端里运行，不要在沙箱/受限环境）"

sleep 3
if curl -sf "http://127.0.0.1:$PORT/api/health" >/dev/null; then
  log "SERVICE_OK  http://127.0.0.1:$PORT/api/health"
  log "开机自启 + 崩溃自动拉起已生效。查看状态: bash scripts/install-service.sh --status"
else
  log "健康检查失败，最近日志:"
  tail -20 "$APP/logs/server.log" >&2
  exit 1
fi
