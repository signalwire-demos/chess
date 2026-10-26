#!/bin/sh
# Run the app, and only mint a tunnel if nothing else supplies the public URL.
#
# SWML_PROXY_URL_BASE is the host the agent ADVERTISES: it self-registers its
# SWML handler at <base>/chess and mints guest tokens against it, so every
# SWAIG callback and the audio negotiation come back to that host. It is not
# cosmetic - get it wrong and the board still works while the agent goes deaf.
#
# Deployed, compose passes the stable public URL (CHESS_URL), which is the same
# route that already serves the board. Unset - running this image by hand -
# fall back to a trycloudflare quick tunnel so the demo still works standalone.
# That fallback is deliberately NOT the deployed path: the URL changes on every
# container start, which re-registers the handler, and it is a second
# unauthenticated front door to the same app.
#
# SINGLE worker on purpose: the authoritative in-memory GAMES store must be
# shared between the voice (SWAIG) path and the board (REST) path.
set -eu
PORT="${PORT:-5000}"

# A trailing slash would produce <base>//chess in the advertised handler URL.
BASE="${SWML_PROXY_URL_BASE:-}"
case "$BASE" in */) BASE="${BASE%/}" ;; esac

if [ -n "$BASE" ]; then
  export SWML_PROXY_URL_BASE="$BASE"
  echo "[entrypoint] =================================================="
  echo "[entrypoint]  PUBLIC URL   : $BASE   (configured; no quick tunnel)"
  echo "[entrypoint]  SWML handler : $BASE/chess"
  echo "[entrypoint] =================================================="
else
  LOG=/tmp/cloudflared.log
  : > "$LOG"

  echo "[entrypoint] SWML_PROXY_URL_BASE unset - falling back to a quick tunnel"
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
fi

exec gunicorn app:app --bind "0.0.0.0:${PORT}" --workers 1 --preload --worker-class uvicorn.workers.UvicornWorker
