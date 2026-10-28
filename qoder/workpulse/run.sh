#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# WorkPulse one-command local start.
#
#   ./run.sh          # check seekdb, start PowerContext, start WorkPulse
#   ./run.sh --seed   # ... and load the demo dataset afterwards
#
# Requirements: bash, python3.11+, docker (only if you still need seekdb).
# ---------------------------------------------------------------------------
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

PY="${PYTHON:-python3}"
SEEKDB_CONTAINER="${SEEKDB_CONTAINER:-seekdb}"
SEEKDB_PORT="${WP_SEEKDB_PORT:-2881}"
PC_PORT="${WP_PC_PORT:-8000}"
APP_PORT="${WP_PORT:-8077}"
PC_TOKEN="${WP_PC_TOKEN:-workpulse-local-token}"
PC_HOME="${POWERCONTEXT_HOME:-$HOME/.workpulse/powercontext}"
LOG_DIR="$ROOT/logs"
mkdir -p "$LOG_DIR" "$PC_HOME"

say() { printf '\033[1m[workpulse]\033[0m %s\n' "$*"; }
ok()  { printf '  \033[32m✓\033[0m %s\n' "$*"; }
bad() { printf '  \033[31m✗\033[0m %s\n' "$*"; }

# --- 1. seekdb -------------------------------------------------------------
say "1/3 检查 seekdb"
if (exec 3<>"/dev/tcp/127.0.0.1/$SEEKDB_PORT") 2>/dev/null; then
  exec 3>&- 3<&-
  ok "seekdb 已在 127.0.0.1:$SEEKDB_PORT 监听"
else
  if command -v docker >/dev/null 2>&1; then
    say "    seekdb 未运行，尝试启动容器 $SEEKDB_CONTAINER"
    docker start "$SEEKDB_CONTAINER" >/dev/null 2>&1 || \
      docker run -d --name "$SEEKDB_CONTAINER" -p "$SEEKDB_PORT:2881" \
        quay.io/oceanbase/seekdb:latest >/dev/null
    for _ in $(seq 1 60); do
      (exec 3<>"/dev/tcp/127.0.0.1/$SEEKDB_PORT") 2>/dev/null && { exec 3>&- 3<&-; break; }
      sleep 1
    done
    ok "seekdb 容器已启动"
  else
    bad "seekdb 未运行，且没有找到 docker。请先启动 seekdb（见 README 第 2 节）"
    exit 1
  fi
fi

# --- 2. PowerContext ------------------------------------------------------
say "2/3 检查 PowerContext"
if curl -fsS -m 2 "http://127.0.0.1:$PC_PORT/health/live" >/dev/null 2>&1; then
  ok "PowerContext 已在 http://127.0.0.1:$PC_PORT 运行"
else
  if ! command -v powercontext >/dev/null 2>&1; then
    bad "未安装 powercontext。请执行：pip install \"powercontext[cli,server]==1.2.0\""
    exit 1
  fi
  say "    启动 PowerContext（basic memory 模式，无需模型 API）"
  POWERCONTEXT_HOME="$PC_HOME" \
  POWERCONTEXT_SERVER_HTTP_HOST=127.0.0.1 \
  POWERCONTEXT_SERVER_HTTP_PORT="$PC_PORT" \
  POWERCONTEXT_SERVER_DASHBOARD_ENABLED=true \
  POWERCONTEXT_SERVER_ACCESS_MODE=enforced \
  POWERCONTEXT_SERVER_AUTH_TOKEN="$PC_TOKEN" \
    nohup powercontext server run > "$LOG_DIR/powercontext.log" 2>&1 &
  for _ in $(seq 1 60); do
    curl -fsS -m 2 "http://127.0.0.1:$PC_PORT/health/live" >/dev/null 2>&1 && break
    sleep 1
  done
  curl -fsS -m 2 "http://127.0.0.1:$PC_PORT/health/live" >/dev/null 2>&1 \
    && ok "PowerContext 已启动（Dashboard: http://127.0.0.1:$PC_PORT/dashboard/home，token 见 .env）" \
    || { bad "PowerContext 启动失败，日志：$LOG_DIR/powercontext.log"; exit 1; }
fi

# --- 3. WorkPulse ---------------------------------------------------------
say "3/3 启动 WorkPulse"
[ -f .env ] || cp .env.example .env
"$PY" -c "import fastapi, aiomysql, httpx, numpy" 2>/dev/null || {
  bad "缺少 Python 依赖，请先执行：pip install -r requirements.txt"
  exit 1
}
nohup "$PY" -m uvicorn app.main:app --host "${WP_HOST:-127.0.0.1}" --port "$APP_PORT" \
  > "$LOG_DIR/server.log" 2>&1 &
for _ in $(seq 1 30); do
  curl -fsS -m 2 "http://127.0.0.1:$APP_PORT/api/health" >/dev/null 2>&1 && break
  sleep 1
done
curl -fsS -m 3 "http://127.0.0.1:$APP_PORT/api/health" | tee /dev/stderr | grep -q '"ok":true' \
  && ok "WorkPulse 已启动：http://127.0.0.1:$APP_PORT" \
  || { bad "WorkPulse 启动失败，日志：$LOG_DIR/server.log"; exit 1; }

if [ "${1:-}" = "--seed" ]; then
  say "导入演示数据"
  WP_BASE="http://127.0.0.1:$APP_PORT" "$PY" scripts/seed_demo.py --reset
fi

say "完成。停止服务：pkill -f 'app.main:app'; pkill -f 'powercontext server run'"
