#!/bin/bash
# =============================================================================
# ShoppingLog 一键部署/升级脚本
#   程序源码: $HOME/project/ShoppingLog   （git 仓库，仅代码与文档）
#   运行数据: $HOME/myapp/shoppinglog     （配置/数据库/图片/日志，升级不触碰）
#
# 用法:
#   bash scripts/deploy.sh                升级（默认）：拉取后重启本地服务，数据不动
#   bash scripts/deploy.sh --install      首次部署：建运行目录/生成配置/初始化MySQL/启动
#   bash scripts/deploy.sh --init-db      初始化 MySQL 库与专属用户（需 MYSQL_ROOT_PASS）
#   bash scripts/deploy.sh --restart      同默认升级
#
# 环境变量（可选）:
#   SHOPLOG_SRC      源码目录    默认 $HOME/project/ShoppingLog
#   SHOPLOG_APP      运行目录    默认 $HOME/myapp/shoppinglog
#   SHOPLOG_PORT     服务端口    默认 8787
#   SHOPLOG_PY       解释器路径  默认 PATH 上的 python3（MySQL 模式必须装 pymysql）
#   MYSQL_ROOT_PASS  MySQL root 密码（仅 --init-db 需要）
# =============================================================================
set -euo pipefail

SRC="${SHOPLOG_SRC:-$HOME/project/ShoppingLog}"
APP="${SHOPLOG_APP:-$HOME/myapp/shoppinglog}"
PORT="${SHOPLOG_PORT:-8787}"
PY="${SHOPLOG_PY:-$(command -v python3 || echo python3)}"

log() { echo "[deploy] $*"; }
die() { echo "[deploy] ERROR: $*" >&2; exit 1; }

# ---------- 前置校验 ----------
[ -d "$SRC" ]            || die "源码目录不存在: $SRC"
[ -f "$SRC/scripts/server.py" ] || die "未找到 scripts/server.py，请确认源码路径: $SRC"
[ -f "$SRC/config.example.json" ] || die "缺少 config.example.json"

# ---------- 读取运行配置（JSON 解析，缺文件则回退默认） ----------
read_cfg() {
  "$PY" - "$APP/config.local.json" <<'PYEOF'
import json, sys
try:
    cfg = json.load(open(sys.argv[1]))["db"]
except Exception:
    cfg = {}
print(cfg.get("engine", "sqlite"))
print(cfg.get("host", "127.0.0.1"))
print(cfg.get("port", "3306"))
print(cfg.get("user", ""))
print(cfg.get("password", ""))
print(cfg.get("database", "shoppinglog"))
PYEOF
}

ENGINE=sqlite; DB_HOST=127.0.0.1; DB_PORT=3306; DB_USER=""; DB_PASS=""; DB_NAME="shoppinglog"
if [ -f "$APP/config.local.json" ]; then
  read -r ENGINE DB_HOST DB_PORT DB_USER DB_PASS DB_NAME < <(read_cfg)
fi

# ---------- 模式: --init-db（建库/建专属用户；表由程序启动时自动创建） ----------
if [ "${1:-}" = "--init-db" ]; then
  [ "$ENGINE" = "mysql" ] || die "当前 engine=${ENGINE}，无需初始化 MySQL"
  [ -n "${MYSQL_ROOT_PASS:-}" ] || die "需要 MYSQL_ROOT_PASS（MySQL root 密码）"
  [ -n "$DB_USER" ] || die "config.local.json 缺少 db.user"
  [ -n "$DB_PASS" ] || die "config.local.json 缺少 db.password"
  MYSQL="$(command -v mysql || echo /opt/homebrew/bin/mysql)"
  MYSQL_PWD="$MYSQL_ROOT_PASS" "$MYSQL" -h"$DB_HOST" -P"$DB_PORT" -uroot <<SQL 2>/dev/null
CREATE DATABASE IF NOT EXISTS \`$DB_NAME\` DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER IF NOT EXISTS '$DB_USER'@'%' IDENTIFIED BY '$DB_PASS';
CREATE USER IF NOT EXISTS '$DB_USER'@'localhost' IDENTIFIED BY '$DB_PASS';
GRANT ALL PRIVILEGES ON \`$DB_NAME\`.* TO '$DB_USER'@'%';
GRANT ALL PRIVILEGES ON \`$DB_NAME\`.* TO '$DB_USER'@'localhost';
FLUSH PRIVILEGES;
SQL
  log "MySQL 库/用户已就绪: $DB_NAME / ${DB_USER}（表结构由程序启动时自动创建）"
  exit 0
fi

# ---------- 模式: --install（首次部署） ----------
if [ "${1:-}" = "--install" ]; then
  log "首次部署，创建运行目录: $APP"
  mkdir -p "$APP/data" "$APP/images" "$APP/exports" "$APP/logs"
  if [ ! -f "$APP/config.local.json" ]; then
    cp "$SRC/config.example.json" "$APP/config.local.json"
    log "已生成 $APP/config.local.json，请按需编辑 db 配置后再启动"
  fi
  # 站点图片软链（源码不存图片；git 不跟踪此链接）
  if [ ! -e "$SRC/site/images" ]; then
    ln -s "$APP/images" "$SRC/site/images"
    log "已建立软链 site/images -> $APP/images"
  fi
  if [ "$ENGINE" = "mysql" ] && [ -n "${MYSQL_ROOT_PASS:-}" ]; then
    bash "$0" --init-db
  elif [ "$ENGINE" = "mysql" ]; then
    log "engine=mysql 且未提供 MYSQL_ROOT_PASS，跳过建库；可执行: MYSQL_ROOT_PASS=xxx bash scripts/deploy.sh --init-db"
  fi
fi

# ---------- 升级/重启服务 ----------
# 前置检查：MySQL 模式必须有 pymysql，否则一重启就把线上打挂（ModuleNotFoundError）。
# PATH 上的 python3 未必装了这个包，所以先探测，必要时换解释器，都不行就中止。
if [ "$ENGINE" = "mysql" ]; then
  if ! "$PY" -c "import pymysql" >/dev/null 2>&1; then
    for cand in \
      "$HOME/.local/share/uv/python/cpython-3.14-macos-aarch64-none/bin/python3.14" \
      "$HOME/Library/Application Support/com.tencent.mac.marvis/components/MarvisAgent/Versions/1.0.0.10409/runtime/python311/bin/python3.11" \
      "$(command -v python3.11 || true)" \
      "$(command -v python3.12 || true)"; do
      if [ -n "$cand" ] && [ -x "$cand" ] && "$cand" -c "import pymysql" >/dev/null 2>&1; then
        PY="$cand"; log "当前 python3 缺 pymysql，改用: $PY"
        break
      fi
    done
  fi
  "$PY" -c "import pymysql" >/dev/null 2>&1 || die \
    "engine=mysql 但 $PY 缺少 pymysql。请先 '$PY -m pip install pymysql'，或用 SHOPLOG_PY 指定带该模块的解释器。"
fi

log "升级部署: 重启服务 (port $PORT)"

# LaunchAgent 优先：服务注册过 launchd 时，kill + 自起会把「谁负责托管」搞乱 ——
# 这里交给 launchctl kickstart 重启（开机自启/崩溃拉起的身份保持不变）；
# 没注册（或受限终端里 launchctl 不可用）才退回双 fork 守护进程。
LABEL="com.meria.shoppinglog"
DOMAIN="gui/$(id -u)"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
mkdir -p "$APP/logs"
cd "$SRC"

if launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; then
  log "检测到 LaunchAgent $LABEL 已注册，改用 launchctl kickstart 重启"
  launchctl kickstart -k "$DOMAIN/$LABEL" || die "launchctl kickstart 失败（服务可能未重启）"
  sleep 3
else
  PID=$(lsof -tiTCP:"$PORT" -sTCP:LISTEN 2>/dev/null || true)
  if [ -n "$PID" ]; then
    kill "$PID" && sleep 1
    log "已停止旧进程 $PID"
  fi
  # 用 double-fork 守护进程启动：nohup 只忽略 SIGHUP，父进程组被回收时仍会被带走
  # （那样会出现"DEPLOY_OK 通过、两秒后服务没了"）
  SHOPLOG_ROOT="$APP" SHOPLOG_LOG="$APP/logs/server.log" SHOPLOG_CWD="$SRC" \
    "$PY" scripts/start_daemon.py scripts/server.py --port "$PORT" >/dev/null
  sleep 2
  [ -f "$PLIST" ] && log "提示: 检测到 $PLIST 但 launchd 未注册/不可查，本次按守护进程方式启动；想在开机自启时由 launchd 托管，请在普通终端执行 install-service.sh"
fi

if curl -sf "http://127.0.0.1:$PORT/api/health" >/dev/null; then
  log "DEPLOY_OK  http://127.0.0.1:$PORT/api/health"
else
  log "DEPLOY_FAIL 最近日志:"
  tail -20 "$APP/logs/server.log" >&2
  exit 1
fi

# ---------- 版本号核验（版本号唯一来源：$SRC/VERSION） ----------
EXPECT_V="$(cat "$SRC/VERSION" 2>/dev/null || echo "")"
GOT_V="$(curl -s "http://127.0.0.1:$PORT/api/version" | "$PY" -c 'import json,sys
try: print(json.load(sys.stdin).get("version",""))
except Exception: print("")')"
log "版本: 源码 VERSION=$EXPECT_V / 线上 /api/version=$GOT_V"
[ -n "$EXPECT_V" ] && [ "$EXPECT_V" = "$GOT_V" ] || die "线上版本与源码 VERSION 不一致（源码 $EXPECT_V ≠ 线上 ${GOT_V}），部署未生效"

# ---------- 对外端口（nginx 8088）复核：前端 + MCP 必须都能在 8088 取到 ----------
# 公网只转发 8088，所以前端页面和 MCP 端点都必须挂在 8088 上，否则域名访问不到。
WEB_PORT="${SHOPLOG_WEB_PORT:-8088}"
if ! curl -s -o /dev/null -m 3 "http://127.0.0.1:$WEB_PORT/" 2>/dev/null; then
  log "WARN: $WEB_PORT 无响应（nginx 未监听？），域名将访问不到前端与 MCP，请检查 nginx"
else
  V8088="$(curl -s -m 5 "http://127.0.0.1:$WEB_PORT/api/version" | "$PY" -c 'import json,sys
try: print(json.load(sys.stdin).get("version",""))
except Exception: print("")')"
  [ "$V8088" = "$EXPECT_V" ] || die "8088 上 /api/version=$V8088 与源码 $EXPECT_V 不一致（nginx 反代未生效）"

  # MCP：有 Token 就做真实 initialize，否则只验"端点没被 auth_request 302 吞掉"
  if [ -n "${SHOPLOG_API_TOKEN:-}" ]; then
    MCP_RES="$(curl -s -m 10 -X POST "http://127.0.0.1:$WEB_PORT/api/mcp" \
      -H "Authorization: Bearer $SHOPLOG_API_TOKEN" -H "Content-Type: application/json" \
      -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"deploy","version":"1"}}}')"
    MCP_V="$(printf '%s' "$MCP_RES" | "$PY" -c 'import json,sys
try: print(json.load(sys.stdin)["result"]["serverInfo"]["version"])
except Exception: print("")')"
    [ "$MCP_V" = "$EXPECT_V" ] || die "8088 上 /api/mcp 未返回正确版本（得到 '$MCP_V'），响应: ${MCP_RES:0:200}"
    log "8088 复核 OK: 前端 + /api/version + /api/mcp（MCP serverInfo v${MCP_V}）"
  else
    CODE="$(curl -s -o /tmp/shoplog_mcp_probe.txt -w '%{http_code}' -m 10 -X POST "http://127.0.0.1:$WEB_PORT/api/mcp" \
      -H "Authorization: Bearer probe-invalid-token" -H "Content-Type: application/json" \
      -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"deploy","version":"1"}}}' || echo 000)"
    if [ "$CODE" = "401" ]; then
      log "8088 复核 OK: 前端 + /api/version + /api/mcp 端点可达（无效 Token 正确返回 401 JSON，未被登录页 302 吞掉）"
    else
      die "8088 上 /api/mcp 异常：HTTP ${CODE}（期望 401 JSON；若为 302 说明被 auth_request 拦截，MCP 客户端会收到登录页 HTML）"
    fi
  fi
fi
