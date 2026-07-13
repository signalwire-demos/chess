# ♞ SignalWire Chess

<p align="center">
  <img src="docs/screenshot.png" alt="SignalWire Chess — interactive board with a live AI opponent" width="900">
</p>

Play live chess against **Sigmond**, a [SignalWire](https://signalwire.com) AI agent, over a real‑time WebRTC audio/video call — **by voice or by moving the pieces on screen**. The board stays in sync with the AI the whole game, moves animate in the video window, and results feed a leaderboard.

> Speak *"pawn to e4"* or just drag the piece — Sigmond (powered by Stockfish) answers out loud and on the board.

---

## Features

- 🎙️ **Voice + click input** — say moves in plain language *or* click/drag on the board; both mutate the same game.
- 🤖 **Real engine opponent** — [Stockfish](https://stockfishchess.org) plays the moves; [python-chess](https://python-chess.readthedocs.io) enforces the rules.
- 🎚️ **Adjustable difficulty** — Beginner → Master, changeable by voice or UI.
- 🎞️ **Animated move GIFs** — each move renders server‑side (Pillow) and overlays the avatar video.
- 🏆 **Leaderboard** — difficulty‑weighted scoring with a speed bonus, plus Sigmond's own house record.
- 🌗 **Dark / light themes** and a **Google/Material‑style UI** in SignalWire brand colors.
- 🗣️ **Voice commands** — new game, change difficulty, resign, explain a term, and hang up.

A full architecture write‑up is served by the app itself at **`/how-it-works.html`** (source: `web/how-it-works.html`).

---

## Prerequisites

- **Docker** + **Docker Compose** (the only hard requirement — Stockfish, cloudflared, and Python all run inside the container).
- A **SignalWire account** and a Space — grab your **Space name**, **Project ID**, and an **API token** from the dashboard. (A free account works.)
- A modern browser with microphone access (Chrome/Edge/Safari). Mic requires HTTPS — the tunnel URL provides it.

No host install of Python or Stockfish is needed.

---

## Quick start

```bash
# 1. Clone
git clone https://github.com/Len-PGH/signalwire_chess.git
cd signalwire_chess

# 2. Configure credentials
cp .env.example .env
#   then edit .env and fill in your SignalWire Space name, Project ID, and token

# 3. Build & run
docker compose up -d --build

# 4. Get the public URL (the container opens a cloudflared quick tunnel)
docker compose logs | grep trycloudflare
```

Open the printed `https://<random>.trycloudflare.com/` URL, enter your name, click **Connect & Play**, and start moving pieces or speaking your moves.

That's it — on startup the app **auto‑registers its SWML handler** with SignalWire (using your API creds) and mints guest tokens for the browser, so there's no manual dashboard wiring.

> The container publishes to `127.0.0.1:5101` locally; public access is via the cloudflared tunnel URL. Each restart mints a **new** tunnel URL (it's an ephemeral quick tunnel) — see [Stable URL](#stable-url) to pin it.

---

## Configuration

All config is via `.env` (see `.env.example`):

| Variable | Required | Description |
|---|---|---|
| `SIGNALWIRE_SPACE_NAME` | ✅ | Your Space host, e.g. `example.signalwire.com` |
| `SIGNALWIRE_PROJECT_ID` | ✅ | Project ID from the dashboard |
| `SIGNALWIRE_TOKEN` | ✅ | API token |
| `SWML_BASIC_AUTH_USER` | ✅ | Username protecting the `/chess` SWML webhook (any value) |
| `SWML_BASIC_AUTH_PASSWORD` | ✅ | Password for the same (any value) |

Other knobs live in `docker-compose.yml` (`environment:`): `AGENT_NAME`, `STOCKFISH_PATH`, `CHESS_DB`. The SQLite move log is persisted in a named Docker volume (`chess-data`), so the leaderboard survives restarts.

---

## How to play

- **By voice:** say your move naturally — *"pawn to e4"*, *"knight takes f6"*, *"castle kingside"*, or the square like *"e2 e4"*.
- **By clicking:** click one of your pieces (legal targets highlight), then click a destination. Promotions prompt for a piece.
- **Commands (voice):** *"new game"*, *"play as black"*, *"make it harder/easier"* / *"set it to master"*, *"resign"*, *"what is en passant?"*, *"hang up"* / *"end the game"*.
- **Leaderboard:** enter a name (top‑right) to appear on it; open **Leaderboard** from the top bar. Guests still count toward Sigmond's record.
- **Theme:** toggle dark/light with the ☀/☾ button.

### Scoring

Every finished game scores; winning fast against a tougher Sigmond scores most:

- **+5** for finishing a game (win *or* lose)
- **Win:** `+ difficulty × 10`
- **Speed bonus** (wins only): `+ round(difficulty × 40 / moves)` — a quick mate is worth much more than a long grind
- **Draw:** `+ difficulty × 3`
- Difficulty weights: Beginner 1 · Easy 2 · Medium 3 · Hard 5 · Master 8

---

## REST + SWAIG surface

The browser talks to a small REST API; the AI calls SWAIG functions. Both mutate the same authoritative game store.

| Route | Purpose |
|---|---|
| `POST /chess/new` | Create a game (`difficulty`, `player_color`, `player_name`) |
| `POST /chess/move` | Apply a clicked move; engine replies; returns the new board |
| `POST /chess/rename` | Tag the current game with the player's name |
| `POST /chess/resign` | Resign the game |
| `GET /chess/state` | Current board (initial render / resync) |
| `GET /chess/gif/{id}` | Lazily‑rendered move animation |
| `GET /chess/stats` | Move statistics (voice vs click, results) |
| `GET /chess/leaderboard` | Player standings + Sigmond's record |
| `GET /get_token` | WebRTC guest token + agent address for the browser |

SWAIG functions the AI can call: `make_move`, `check_board_moves`, `new_game`, `change_difficulty`, `resign`, `get_board`, `explain_term`, `end_call`.

---

## Project layout

```
app.py             # SignalWire AI agent (AgentBase) + REST API + game/engine logic
chessgif.py        # Pillow renderer for per-move animated GIFs
web/
  index.html       # the interactive board UI (Material / SignalWire brand)
  leaderboard.html # leaderboard page
  how-it-works.html# architecture deep-dive (with SDK citations)
  app.js           # browser client (@signalwire/js v4)
  signalwire.js    # the browser SDK bundle
Dockerfile         # python:3.11-slim + stockfish + cloudflared + Pillow font
entrypoint.sh      # opens a cloudflared tunnel, exports its URL, starts the app
docker-compose.yml # single-worker service + persistent chess-data volume
```

---

## Running locally without Docker (advanced)

```bash
sudo apt-get install -y stockfish fonts-dejavu-core         # engine + glyph font
pip install -r requirements.txt
export SIGNALWIRE_SPACE_NAME=... SIGNALWIRE_PROJECT_ID=... SIGNALWIRE_TOKEN=...
export SWML_BASIC_AUTH_USER=chess SWML_BASIC_AUTH_PASSWORD=change-me
export SWML_PROXY_URL_BASE="https://<your-public-url>"       # e.g. a cloudflared/ngrok tunnel
gunicorn app:app --workers 1 --preload --worker-class uvicorn.workers.UvicornWorker --bind 0.0.0.0:5000
```

SignalWire must be able to reach the app's `/chess` webhook over HTTPS, so you need a public URL (`SWML_PROXY_URL_BASE`). Run **one** worker — the authoritative game store is in‑process.

### Stable URL

The Docker setup uses a **quick** cloudflared tunnel, so the URL changes on every restart (and the browser's saved name, kept in `localStorage`, resets with the origin). To pin a hostname, run a **named** cloudflared tunnel and set `SWML_PROXY_URL_BASE` to your fixed domain.

---

## Tech stack

SignalWire AI Agents SDK · `@signalwire/js` (v4 browser SDK) · SWML/SWAIG · python‑chess · Stockfish · Pillow · SQLite · FastAPI/uvicorn · Docker · cloudflared.

Built as a SignalWire demo. The board renders on screen while the WebRTC session is live and stays in sync with the AI via server‑pushed `user_event`s and a deterministic move nudge (`calling.ai_message`).
