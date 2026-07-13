FROM python:3.11-slim

WORKDIR /app

# curl (healthcheck), stockfish (AI opponent), cloudflared (ephemeral tunnel)
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates stockfish fonts-dejavu-core \
 && curl -fsSL https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 \
      -o /usr/local/bin/cloudflared \
 && chmod +x /usr/local/bin/cloudflared \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENV PORT=5000 HOME=/tmp STOCKFISH_PATH=/usr/games/stockfish CHESS_DB=/data/chess.db
EXPOSE 5000

# Persistent SQLite move log lives in /data (a named volume). Create it owned by
# appuser BEFORE the volume is declared so the volume inherits that ownership on
# first mount — the non-root process must be able to write the DB.
RUN useradd -r -s /bin/false appuser \
 && mkdir -p /data \
 && chown appuser /data
VOLUME /data
USER appuser

ENTRYPOINT ["/entrypoint.sh"]
