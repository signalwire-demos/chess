#!/bin/sh
# Quick cloudflared tunnel -> SWML_PROXY_URL_BASE (agent self-registers its
# handler at <tunnel>/chess and mints guest tokens), then run the app.
# SINGLE worker on purpose: the authoritative in-memory GAMES store must be
# shared between the voice (SWAIG) path and the board (REST) path.
set -eu
PORT="${PORT:-5000}"
LOG=/tmp/cloudflared.log
: > "$LOG"

echo "[entrypoint] starting cloudflared quick tunnel -> http://localhost:${PORT}"
cloudflared tunnel --no-autoupdate --url "http://localhost:${PORT}" >"$LOG" 2>&1 &
CF_PID=$!

echo "[entrypoint] waiting for trycloudflare.com URL ..."
URL=""; i=0
while [ "$i" -lt 60 ]; do
  URL=$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' "$LOG" | head -1 || true)
  [ -n "$URL" ] && break
  kill -0 "$CF_PID" 2>/dev/null || { echo "[entrypoint] cloudflared died:" >&2; cat "$LOG" >&2; exit 1; }
  i=$((i+1)); sleep 1
done
[ -n "$URL" ] || { echo "[entrypoint] no tunnel URL after 60s" >&2; cat "$LOG" >&2; exit 1; }

export SWML_PROXY_URL_BASE="$URL"
echo "[entrypoint] =================================================="
echo "[entrypoint]  RANDOM TUNNEL URL : $URL"
echo "[entrypoint]  web client        : $URL/"
echo "[entrypoint]  SWML handler      : $URL/chess"
echo "[entrypoint] =================================================="

exec gunicorn app:app --bind "0.0.0.0:${PORT}" --workers 1 --preload --worker-class uvicorn.workers.UvicornWorker
