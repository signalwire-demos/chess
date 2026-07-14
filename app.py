#!/usr/bin/env python3
"""
Dealer - The SignalWire Blackjack Dealer (Refactored)
An AI-powered blackjack dealer that plays casino-style blackjack via voice/video
Uses stateless architecture with centralized state management
"""

import json
import random
import os
import time
import threading
import warnings
from pathlib import Path
from signalwire import AgentBase, AgentServer
from signalwire.core.function_result import SwaigFunctionResult
from signalwire.rest import RestClient
from dotenv import load_dotenv
from fastapi.responses import JSONResponse, Response

# Load environment variables
load_dotenv()

# Store the SWML handler info for reuse
swml_handler_info = {"id": None, "address_id": None, "address": None}

# Records *why* SWML handler registration didn't happen, surfaced to /get_token
# so a silent skip doesn't become a baffling browser error. Cleared on success.
swml_setup_error = None
# Serializes the lazy re-registration retry in /get_token.
_swml_setup_lock = threading.Lock()


# ─────────────────────────────────────────────────────────────────────────────
# Chess core: python-chess rules + Stockfish opponent, plus the ChessOpponent
# AgentBase agent. Authoritative game state lives in the module-level GAMES
# store (keyed by game_id) so BOTH the voice path (SWAIG make_move) and the
# on-screen board (REST /chess/move) mutate the same position and never desync.
# Run single-worker (see entrypoint) so this in-process store is consistent.
# ─────────────────────────────────────────────────────────────────────────────
import re
import uuid
import chess
import chess.engine

STOCKFISH_PATH = os.environ.get("STOCKFISH_PATH", "/usr/games/stockfish")
START_FEN = chess.STARTING_FEN

# level -> (Stockfish "Skill Level" 0-20, think seconds)
DIFFICULTY = {
    "beginner": (0, 0.05),
    "easy":     (3, 0.10),
    "medium":   (8, 0.20),
    "hard":     (14, 0.40),
    "master":   (20, 0.80),
}
DIFFICULTY_ORDER = ["beginner", "easy", "medium", "hard", "master"]

PIECE_WORD = {chess.PAWN: "pawn", chess.KNIGHT: "knight", chess.BISHOP: "bishop",
              chess.ROOK: "rook", chess.QUEEN: "queen", chess.KING: "king"}
PIECE_VALUE = {chess.PAWN: 1, chess.KNIGHT: 3, chess.BISHOP: 3,
               chess.ROOK: 5, chess.QUEEN: 9, chess.KING: 0}
PIECE_LETTER_NL = {"knight": "N", "bishop": "B", "rook": "R", "queen": "Q", "king": "K", "pawn": ""}


_SPOKEN_PIECE = {"N": "knight", "B": "bishop", "R": "rook", "Q": "queen", "K": "king"}


def spoken_move(san):
    """Turn SAN into a naturally-pronounced phrase so TTS says it right, e.g.
    'Nc4' -> 'knight to c4', 'exd5' -> 'e takes d5', 'Bxf7+' -> 'bishop takes f7, check',
    'O-O' -> 'castle kingside', 'e8=Q' -> 'pawn to e8 promoting to queen'."""
    if not san:
        return ""
    s = str(san).strip()
    suffix = ""
    if s.endswith("#"):
        suffix, s = ", checkmate", s[:-1]
    elif s.endswith("+"):
        suffix, s = ", check", s[:-1]
    low = s.lower()
    if low in ("o-o", "0-0"):
        return "castle kingside" + suffix
    if low in ("o-o-o", "0-0-0"):
        return "castle queenside" + suffix
    promo = ""
    m = re.search(r"=([QRBNqrbn])$", s)
    if m:
        promo = " promoting to " + _SPOKEN_PIECE[m.group(1).upper()]
        s = s[:m.start()]
    capture = "x" in s
    if s[:1] in _SPOKEN_PIECE:                       # piece move
        piece, body = _SPOKEN_PIECE[s[0]], s[1:].replace("x", "")
        dest, dis = body[-2:], body[:-2]
        words = [piece] + ([" ".join(dis)] if dis else []) + ["takes" if capture else "to", dest]
    else:                                            # pawn move
        body = s.replace("x", "")
        dest, dis = body[-2:], body[:-2]
        words = ([dis, "takes", dest] if capture else ["pawn", "to", dest])
    return " ".join(w for w in words if w) + promo + suffix


def nl_to_san(text):
    """Loose natural-language -> SAN, e.g. 'pawn to e4'->'e4', 'knight takes f6'->'Nxf6'."""
    t = text.lower()
    m = re.search(r"\b([a-h])\s*([1-8])\b", t)
    if not m:
        return None
    target = m.group(1) + m.group(2)
    piece = next((l for w, l in PIECE_LETTER_NL.items() if w in t), "")
    capture = ("take" in t) or ("capture" in t) or (" x " in t) or ("x" == t.strip()[:1])
    if piece:
        return f"{piece}{'x' if capture else ''}{target}"
    return target  # pawn push

CHESS_GLOSSARY = {
    "castle": "a special move where the king moves two squares toward a rook and the rook jumps to the king's other side; say 'castle kingside' or 'castle queenside'",
    "castling": "a special move where the king moves two squares toward a rook and the rook jumps to the king's other side",
    "en passant": "a special pawn capture: if a pawn moves two squares and lands beside your pawn, you may capture it as if it moved only one",
    "check": "your king is under attack and you must get out of it this move",
    "checkmate": "the king is in check and cannot escape - the game is over",
    "stalemate": "the player to move has no legal move but is not in check - the game is a draw",
    "pin": "a piece can't move because doing so would expose a more valuable piece behind it",
    "fork": "one piece attacks two enemy pieces at once",
    "promotion": "when a pawn reaches the far side it becomes a queen, rook, bishop, or knight",
    "rank": "a horizontal row of the board, numbered 1 to 8",
    "file": "a vertical column of the board, lettered a to h",
    "fianchetto": "developing a bishop to the long diagonal via g2/b2 (or g7/b7)",
}

# ── module-level authoritative game store ────────────────────────────────────
GAMES = {}                       # game_id -> state dict (live board authority)
GAMES_LOCK = threading.Lock()
_LAST_GAME_ID = {"id": None}     # fallback when a call can't supply its game_id

# ── per-move animated GIFs (rendered server-side, overlaid in the video window) ─
from collections import OrderedDict
from chessgif import render_move_gif
GIFS = OrderedDict()             # gif_id -> rendered GIF bytes (lazy cache)
GIF_JOBS = OrderedDict()         # gif_id -> (fen_before, ucis, player_color) spec
GIFS_LOCK = threading.Lock()
GIF_CAP = 60


def attach_gif(state, fen_before, ucis):
    """Record a render *spec* (not the pixels) and stamp its id on the state so
    every board payload (click/voice/poll) can surface a gif_url. The ~400ms
    Pillow render happens LAZILY on first GET (see get_gif_bytes) so it never
    sits on the move path and delay the board-update event the client needs."""
    if not ucis:
        return
    gid = uuid.uuid4().hex[:12]
    with GIFS_LOCK:
        GIF_JOBS[gid] = (fen_before, list(ucis), state.get("player_color", "white"))
        while len(GIF_JOBS) > GIF_CAP:
            GIF_JOBS.popitem(last=False)
    state["last_gif"] = gid


def get_gif_bytes(gid):
    """Rendered GIF bytes for an id, rendering (and caching) on first request."""
    with GIFS_LOCK:
        if gid in GIFS:
            return GIFS[gid]
        job = GIF_JOBS.get(gid)
    if not job:
        return None
    try:
        data = render_move_gif(*job)
    except Exception:
        data = None
    if data:
        with GIFS_LOCK:
            GIFS[gid] = data
            while len(GIFS) > GIF_CAP:
                GIFS.popitem(last=False)
    return data

# ── SQLite move log: UUID-stamped moves for narration-polling + statistics ────
import sqlite3
CHESS_DB = os.environ.get("CHESS_DB", "/tmp/chess.db")
_db = sqlite3.connect(CHESS_DB, check_same_thread=False)
_db.execute("PRAGMA journal_mode=WAL")
_db.executescript("""
CREATE TABLE IF NOT EXISTS games(
  game_id TEXT PRIMARY KEY, created REAL, difficulty TEXT, player_color TEXT,
  result TEXT, player_name TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS moves(
  move_uuid TEXT PRIMARY KEY, game_id TEXT, ply INTEGER, san TEXT, uci TEXT,
  source TEXT, fen_after TEXT, captured TEXT, narrated INTEGER DEFAULT 0, ts REAL);
CREATE INDEX IF NOT EXISTS idx_moves_game ON moves(game_id, narrated, ply);
""")
# migration: add player_name to games tables created before leaderboards existed
if "player_name" not in [r[1] for r in _db.execute("PRAGMA table_info(games)").fetchall()]:
    _db.execute("ALTER TABLE games ADD COLUMN player_name TEXT DEFAULT ''")
_db.commit()
_DB_LOCK = threading.Lock()

# difficulty -> leaderboard weight (beating a stronger engine is worth more)
DIFF_WEIGHT = {"beginner": 1, "easy": 2, "medium": 3, "hard": 5, "master": 8}


def db_new_game(game_id, difficulty, color, player_name=""):
    with _DB_LOCK:
        _db.execute(
            "INSERT OR REPLACE INTO games"
            "(game_id, created, difficulty, player_color, result, player_name)"
            " VALUES (?,?,?,?,?,?)",
            (game_id, time.time(), difficulty, color, None, (player_name or "").strip()[:32]))
        _db.commit()

def db_record_move(game_id, ply, san, uci, source, fen_after, captured, narrated):
    mid = uuid.uuid4().hex
    with _DB_LOCK:
        _db.execute("INSERT OR REPLACE INTO moves VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (mid, game_id, ply, san, uci, source, fen_after, captured,
                     1 if narrated else 0, time.time()))
        _db.commit()
    return mid

def db_unnarrated(game_id):
    with _DB_LOCK:
        return _db.execute(
            "SELECT move_uuid, ply, san, source FROM moves "
            "WHERE game_id=? AND narrated=0 ORDER BY ply", (game_id,)).fetchall()

def db_mark_narrated(uuids):
    if not uuids:
        return
    with _DB_LOCK:
        _db.executemany("UPDATE moves SET narrated=1 WHERE move_uuid=?", [(u,) for u in uuids])
        _db.commit()

def db_set_result(game_id, result):
    with _DB_LOCK:
        _db.execute("UPDATE games SET result=? WHERE game_id=?", (result, game_id))
        _db.commit()

def db_set_player_name(game_id, name):
    with _DB_LOCK:
        _db.execute("UPDATE games SET player_name=? WHERE game_id=?",
                    ((name or "").strip()[:32], game_id))
        _db.commit()

def db_stats():
    with _DB_LOCK:
        games = _db.execute("SELECT COUNT(*) FROM games").fetchone()[0]
        total = _db.execute("SELECT COUNT(*) FROM moves").fetchone()[0]
        by_source = dict(_db.execute("SELECT source, COUNT(*) FROM moves GROUP BY source").fetchall())
        results = dict(_db.execute(
            "SELECT COALESCE(result,'in_progress'), COUNT(*) FROM games GROUP BY result").fetchall())
    return {"games": games, "total_moves": total, "moves_by_source": by_source, "results": results}


def db_mark_all_narrated(game_id):
    with _DB_LOCK:
        _db.execute("UPDATE moves SET narrated=1 WHERE game_id=? AND narrated=0", (game_id,))
        _db.commit()


def _rest_client():
    """Build a SignalWire RestClient from env, or None if creds are missing."""
    project = os.getenv("SIGNALWIRE_PROJECT_ID")
    token = os.getenv("SIGNALWIRE_TOKEN")
    host = get_signalwire_host()
    if not (project and token and host):
        return None
    return RestClient(project=project, token=token, host=host)


def nudge_ai_move(game_id, call_id, user_san, agent_san):
    """Deterministic push: inject the just-clicked move straight into the live AI
    call (calling.ai_message) so Sigmond narrates it immediately, instead of
    waiting for the check_board_moves silence-poll. Uses absolute USER/AGENT
    labels (not relative you/they) so attribution can't get flipped. On success,
    mark the moves narrated so the poll won't repeat them; on any failure, leave
    them for the poll. Runs in a background thread (never blocks the move)."""
    try:
        client = _rest_client()
        if client is None:
            return
        reply = f" You, the AGENT, already replied {agent_san}." if agent_san else ""
        client.calling.ai_message(
            call_id,
            message_text=(
                "CHESS BOARD UPDATE (this is NOT speech from the user). "
                f"The USER moved {user_san} by clicking the board." + reply + " "
                f"Announce this now: say the USER played {user_san}"
                + (f" and you played {agent_san}" if agent_san else "") + ", then keep "
                "watching the board. Do NOT call make_move - it is already applied."
            ),
            role="system",
        )
        db_mark_all_narrated(game_id)   # delivered -> poll must not re-narrate it
    except Exception:
        pass                            # push failed -> check_board_moves poll covers it


def game_points(difficulty, plies, outcome):
    """Points a single finished game is worth.
      • +5 just for finishing a game (participation — win or lose)
      • win:  + difficulty*10, plus a speed bonus (fewer moves to win = more)
      • draw: + difficulty*3 (holding a draw vs a strong engine counts)
      • loss: participation only
    `plies` is the number of half-moves in the game (used for the speed bonus)."""
    w = DIFF_WEIGHT.get(difficulty, 3)
    pts = 5.0
    if outcome == "win":
        pts += w * 10
        pts += round(w * 40 / max(plies, 1))   # quick mate >> long grind
    elif outcome == "draw":
        pts += w * 3
    return pts


def db_leaderboard():
    """Per-player standings plus Sigmond's (the house) mirrored record across every
    finished game. Scoring: participation + difficulty-weighted wins + a speed bonus
    for quick wins + partial credit for draws (see game_points)."""
    with _DB_LOCK:
        rows = _db.execute(
            "SELECT game_id, player_name, difficulty, player_color, result, created FROM games").fetchall()
        plies_by_game = dict(_db.execute("SELECT game_id, COUNT(*) FROM moves GROUP BY game_id").fetchall())

    def blank(name):
        return {"name": name, "games": 0, "wins": 0, "losses": 0, "draws": 0,
                "in_progress": 0, "points": 0.0, "best_win": None, "last_played": 0}

    agg = {}
    sig = {"name": "Sigmond", "is_ai": True, "games": 0, "wins": 0, "losses": 0,
           "draws": 0, "points": 0.0}
    for gid, name, diff, color, result, created in rows:
        decisive = result in ("1-0", "0-1")
        draw = bool(result) and result.startswith("1/2")
        n = plies_by_game.get(gid, 0)

        if not (decisive or draw):
            if name:                          # in-progress: count the game, no result/points
                a = agg.setdefault(name, blank(name))
                a["games"] += 1
                a["in_progress"] += 1
                a["last_played"] = max(a["last_played"], created or 0)
            continue

        user_won = decisive and ((result == "1-0" and color == "white") or
                                 (result == "0-1" and color == "black"))
        user_outcome = "draw" if draw else ("win" if user_won else "loss")
        sig_outcome = "draw" if draw else ("loss" if user_won else "win")

        # Sigmond's side (every finished game, named or guest)
        sig["games"] += 1
        sig["draws" if draw else ("losses" if user_won else "wins")] += 1
        sig["points"] += game_points(diff, n, sig_outcome)

        if not name:
            continue
        a = agg.setdefault(name, blank(name))
        a["games"] += 1
        a["last_played"] = max(a["last_played"], created or 0)
        a["points"] += game_points(diff, n, user_outcome)
        if user_outcome == "win":
            a["wins"] += 1
            if DIFF_WEIGHT.get(diff, 0) > DIFF_WEIGHT.get(a["best_win"] or "", 0):
                a["best_win"] = diff
        elif user_outcome == "loss":
            a["losses"] += 1
        else:
            a["draws"] += 1

    players = list(agg.values())
    for a in players:
        decided = a["wins"] + a["losses"] + a["draws"]
        a["win_pct"] = round(100 * a["wins"] / decided, 1) if decided else 0.0
        a["points"] = round(a["points"], 1)
    players.sort(key=lambda x: (-x["points"], -x["wins"], -x["win_pct"], -x["games"]))
    for i, p in enumerate(players, 1):
        p["rank"] = i

    sdec = sig["wins"] + sig["losses"] + sig["draws"]
    sig["win_pct"] = round(100 * sig["wins"] / sdec, 1) if sdec else 0.0
    sig["points"] = round(sig["points"], 1)
    return {"players": players, "sigmond": sig, "updated": time.time()}


def _new_state(difficulty="medium", player_color="white", player_name="", mode="game"):
    return {
        "fen": START_FEN,
        "difficulty": difficulty if difficulty in DIFFICULTY else "medium",
        "player_color": "black" if str(player_color).lower().startswith("b") else "white",
        "player_name": (player_name or "").strip()[:32],
        "mode": "learn" if str(mode).lower().startswith("learn") else "game",
        "history": [],               # SAN strings, in order
        "captured_by_white": [],     # piece symbols White has captured (Black's lost pieces)
        "captured_by_black": [],
        "game_over": False,
        "result": None,
    }


def create_game(difficulty="medium", player_color="white", player_name="", mode="game"):
    gid = uuid.uuid4().hex[:12]
    st = _new_state(difficulty, player_color, player_name, mode)
    with GAMES_LOCK:
        GAMES[gid] = st
        _LAST_GAME_ID["id"] = gid
    db_new_game(gid, st["difficulty"], st["player_color"], st["player_name"])
    return gid, st


def resolve_player_name(raw_data):
    """Pull the player's name from global_data / userVariables (browser passes it)."""
    rd = raw_data or {}
    gd = rd.get("global_data") or {}
    cand = gd.get("player_name") or rd.get("player_name")
    if not cand:
        for holder in (rd, gd):
            for k in ("vars", "userVariables", "meta_data", "params"):
                v = holder.get(k) if isinstance(holder, dict) else None
                if isinstance(v, dict) and v.get("player_name"):
                    cand = v["player_name"]
                    break
            if cand:
                break
    return (cand or "").strip()[:32]


def resolve_mode(raw_data):
    """Pull the game mode ('game' or 'learn') from global_data / userVariables."""
    rd = raw_data or {}
    gd = rd.get("global_data") or {}
    cand = gd.get("mode") or rd.get("mode")
    if not cand:
        for holder in (rd, gd):
            for k in ("vars", "userVariables", "meta_data", "params"):
                v = holder.get(k) if isinstance(holder, dict) else None
                if isinstance(v, dict) and v.get("mode"):
                    cand = v["mode"]
                    break
            if cand:
                break
    if not cand:
        return None   # not specified -> caller keeps the game's existing mode
    return "learn" if str(cand).lower().startswith("learn") else "game"


def resolve_game_id(raw_data):
    """Best-effort: find the caller's game_id, else fall back to the most recent game."""
    rd = raw_data or {}
    gd = rd.get("global_data") or {}
    cand = gd.get("game_id") or rd.get("game_id")
    if not cand:
        for holder in (rd, gd):
            for k in ("vars", "userVariables", "meta_data", "params"):
                v = holder.get(k) if isinstance(holder, dict) else None
                if isinstance(v, dict) and v.get("game_id"):
                    cand = v["game_id"]
                    break
            if cand:
                break
    if not cand or cand not in GAMES:
        cand = _LAST_GAME_ID["id"]
    return cand


# ── engine + rules helpers ───────────────────────────────────────────────────
def engine_reply(board, level):
    """Return Stockfish's move for the side to move; fall back to a legal move."""
    skill, movetime = DIFFICULTY.get(level, DIFFICULTY["medium"])
    try:
        with chess.engine.SimpleEngine.popen_uci(STOCKFISH_PATH) as eng:
            try:
                eng.configure({"Skill Level": int(skill)})
            except Exception:
                pass
            return eng.play(board, chess.engine.Limit(time=movetime)).move
    except Exception:
        import random as _r
        legal = list(board.legal_moves)
        if not legal:
            return None
        caps = [m for m in legal if board.is_capture(m)]
        return _r.choice(caps or legal)


ANALYSIS_TIME = float(os.environ.get("CHESS_ANALYSIS_TIME", "0.35"))  # seconds per analyse


def _pov_cp(score, color):
    """Centipawns from `color`'s perspective; mate mapped to a large magnitude."""
    s = score.pov(color)
    if s.is_mate():
        m = s.mate() or 0
        return (30000 - abs(m) * 10) * (1 if m > 0 else -1)
    return s.score() or 0


def best_moves(fen, n=3):
    """Top-n candidate moves for the side to move: list of (san, cp)."""
    try:
        board = chess.Board(fen)
        if board.is_game_over():
            return []
        with chess.engine.SimpleEngine.popen_uci(STOCKFISH_PATH) as eng:
            info = eng.analyse(board, chess.engine.Limit(time=ANALYSIS_TIME), multipv=n)
        info = info if isinstance(info, list) else [info]
        return [(board.san(i["pv"][0]), _pov_cp(i["score"], board.turn)) for i in info if i.get("pv")]
    except Exception:
        return []


def position_eval(fen):
    """Evaluate the current position for the eval bar + best-move arrow.
    Returns {cp (white-relative), mate (signed, or None), best_move (uci for the side
    to move), best_san, turn, game_over}."""
    try:
        board = chess.Board(fen)
        turn = "white" if board.turn == chess.WHITE else "black"
        if board.is_game_over():
            return {"cp": None, "mate": None, "best_move": None, "best_san": None,
                    "turn": turn, "game_over": True}
        with chess.engine.SimpleEngine.popen_uci(STOCKFISH_PATH) as eng:
            info = eng.analyse(board, chess.engine.Limit(time=ANALYSIS_TIME))
        score = info["score"].white()          # always from White's perspective for the bar
        best = info["pv"][0] if info.get("pv") else None
        mate = score.mate() if score.is_mate() else None
        return {"cp": (None if mate is not None else score.score()),
                "mate": mate,
                "best_move": best.uci() if best else None,
                "best_san": board.san(best) if best else None,
                "turn": turn, "game_over": False}
    except Exception:
        return None


def analyze_move(fen_before, played_uci):
    """Grade the played move against the engine's best. Returns a dict or None.
    grade ∈ {best, good, inaccuracy, mistake, blunder}; delta is centipawns lost."""
    try:
        board = chess.Board(fen_before)
        played = chess.Move.from_uci(played_uci)
        if played not in board.legal_moves:
            return None
        mover = board.turn
        played_san = board.san(played)
        with chess.engine.SimpleEngine.popen_uci(STOCKFISH_PATH) as eng:
            info = eng.analyse(board, chess.engine.Limit(time=ANALYSIS_TIME), multipv=3)
            info = info if isinstance(info, list) else [info]
            best_move = info[0]["pv"][0]
            best_cp = _pov_cp(info[0]["score"], mover)
            best_san = board.san(best_move)
            tops = [(board.san(i["pv"][0]), _pov_cp(i["score"], mover)) for i in info if i.get("pv")]
            after = board.copy(); after.push(played)
            if after.is_game_over():
                played_cp = _pov_cp(chess.engine.PovScore(chess.engine.Mate(0), after.turn) if after.is_checkmate()
                                    else chess.engine.PovScore(chess.engine.Cp(0), after.turn), mover)
            else:
                info2 = eng.analyse(after, chess.engine.Limit(time=ANALYSIS_TIME))
                played_cp = -_pov_cp(info2["score"], after.turn)   # negate: opponent POV -> mover POV
        delta = max(0, best_cp - played_cp)
        if played.uci() == best_move.uci() or delta <= 15:
            grade = "best"
        elif delta <= 60:
            grade = "good"
        elif delta <= 130:
            grade = "inaccuracy"
        elif delta <= 300:
            grade = "mistake"
        else:
            grade = "blunder"
        return {"played_san": played_san, "best_san": best_san, "grade": grade,
                "delta": delta, "tops": tops}
    except Exception:
        return None


def coach_note(fen_before, played_uci):
    """A concise coaching instruction for Sigmond to deliver, or None."""
    a = analyze_move(fen_before, played_uci)
    if not a:
        return None
    ps, bs, g = spoken_move(a["played_san"]), spoken_move(a["best_san"]), a["grade"]
    if g == "best":
        return (f"COACHING (learn mode): the player's move {ps} is the engine's top choice — excellent. "
                "Praise it warmly and in one sentence say why it's strong. Say moves naturally (e.g. 'knight to c4').")
    label = {"good": "a solid move", "inaccuracy": "a slight inaccuracy",
             "mistake": "a mistake", "blunder": "a blunder"}[g]
    return (f"COACHING (learn mode): the player's move {ps} is {label} (about {a['delta']} centipoints "
            f"worse than best). The stronger move was {bs}. Kindly point this out, explain in one short "
            f"sentence what {bs} achieves that {ps} misses, and encourage them. Say moves naturally (e.g. 'knight to c4').")


def parse_move(board, text):
    """Accept UCI (e2e4/e7e8q), SAN (Nf3, exd5, O-O), or loose natural language."""
    if not text:
        return None
    t = str(text).strip()
    nl = t.lower()
    # natural-language castling
    if "castle" in nl or nl in ("o-o", "oo", "0-0", "o-o-o", "ooo", "0-0-0"):
        want_long = ("queen" in nl) or ("long" in nl) or nl in ("o-o-o", "ooo", "0-0-0")
        for c in (("O-O-O",) if want_long else ("O-O",)):
            try:
                return board.parse_san(c)
            except Exception:
                pass
    # UCI
    compact = nl.replace(" ", "").replace("-", "")
    try:
        mv = chess.Move.from_uci(compact)
        if mv in board.legal_moves:
            return mv
    except Exception:
        pass
    # SAN variants
    for cand in (t, t.replace(" ", ""), t.replace("takes", "x").replace(" ", ""),
                 t.replace("to", "").replace(" ", "")):
        try:
            return board.parse_san(cand)
        except Exception:
            pass
    # natural language fallback ("pawn to e4", "knight takes f6")
    san = nl_to_san(t)
    if san:
        try:
            return board.parse_san(san)
        except Exception:
            pass
    return None


def material_balance(board):
    bal = 0
    for pt, val in PIECE_VALUE.items():
        bal += val * (len(board.pieces(pt, chess.WHITE)) - len(board.pieces(pt, chess.BLACK)))
    return bal  # positive => White ahead


def position_status(board):
    if board.is_checkmate():
        return "checkmate"
    if board.is_stalemate():
        return "stalemate"
    if board.is_insufficient_material():
        return "draw_insufficient"
    if board.can_claim_threefold_repetition():
        return "draw_repetition"
    if board.is_check():
        return "check"
    return "playing"


def apply_move(state, board, move, game_id=None, source=None, narrated=False):
    """Push a move, record SAN + captured piece into state, and (if game_id given)
    log it to the DB stamped with source/narrated. Returns SAN."""
    san = board.san(move)
    mover_white = board.turn == chess.WHITE
    captured = None
    if board.is_en_passant(move):
        captured = "p" if mover_white else "P"
    elif board.is_capture(move):
        cp = board.piece_at(move.to_square)
        if cp:
            captured = cp.symbol()
    board.push(move)
    if captured:
        (state["captured_by_white"] if mover_white else state["captured_by_black"]).append(captured)
    state["history"].append(san)
    state["fen"] = board.fen()
    if game_id:
        db_record_move(game_id, len(state["history"]), san, move.uci(),
                       source or "?", board.fen(), captured or "", narrated)
    return san


def board_payload(state, board, last_move=None):
    """The UI board-update event payload (also the /chess REST response body)."""
    over = board.is_game_over()
    gif = state.get("last_gif")
    return {
        "type": "board_update",
        "fen": board.fen(),
        "last_move": last_move.uci() if last_move else None,
        "gif_id": gif,
        "gif_url": f"/chess/gif/{gif}" if gif else None,
        "turn": "white" if board.turn == chess.WHITE else "black",
        "legal_moves": [m.uci() for m in board.legal_moves],
        "status": position_status(board),
        "in_check": board.is_check(),
        "game_over": over,
        "result": board.result() if over else None,
        "captured_by_white": state["captured_by_white"],
        "captured_by_black": state["captured_by_black"],
        "material": material_balance(board),
        "history": state["history"],
        "player_color": state["player_color"],
        "player_name": state.get("player_name", ""),
        "difficulty": state["difficulty"],
    }


def describe_end(board):
    if board.is_checkmate():
        winner = "White" if board.turn == chess.BLACK else "Black"
        return f"Checkmate — {winner} wins."
    if board.is_stalemate():
        return "Stalemate — it's a draw."
    if board.is_insufficient_material():
        return "Draw — insufficient material."
    if board.can_claim_threefold_repetition():
        return "Draw by repetition."
    return "The game is over."


def do_player_then_engine(state, move_text, source="voice", game_id=None):
    """Core shared by voice (SWAIG) and board (REST): apply the human move, then
    the engine's reply. `source` is 'voice' or 'click'; voice moves are logged
    narrated (the AI is speaking them now), click moves un-narrated (so the AI
    can catch up on them via check_board_moves). Returns (ok, message, payload)."""
    fen_before = state["fen"]
    board = chess.Board(state["fen"])
    if state.get("game_over") or board.is_game_over():
        return False, "The game is already over. Start a new game to play again.", board_payload(state, board)

    move = parse_move(board, move_text)
    if move is None or move not in board.legal_moves:
        sample = ", ".join(board.san(m) for m in list(board.legal_moves)[:10])
        return False, f"'{move_text}' isn't a legal move here. Legal options include: {sample}.", board_payload(state, board)

    narrate = (source == "voice")
    ucis = [move.uci()]
    # Learn mode: grade the player's move (Stockfish) so Sigmond can coach it. Stored on
    # the state so make_move (voice) or check_board_moves (click) can deliver it.
    if state.get("mode") == "learn":
        state["pending_coach"] = coach_note(fen_before, move.uci())
    human_san = apply_move(state, board, move, game_id, source, narrated=narrate)
    msg = f"You played {spoken_move(human_san)}. "

    if board.is_game_over():
        state["game_over"] = True
        state["result"] = board.result()
        if game_id:
            db_set_result(game_id, state["result"])
        attach_gif(state, fen_before, ucis)
        return True, msg + describe_end(board), board_payload(state, board, move)

    emove = engine_reply(board, state["difficulty"])
    if emove is None:
        attach_gif(state, fen_before, ucis)
        return True, msg, board_payload(state, board, move)
    ucis.append(emove.uci())
    engine_san = apply_move(state, board, emove, game_id, "engine", narrated=narrate)
    msg += f"I play {spoken_move(engine_san)}."
    if board.is_check() and not board.is_game_over():
        msg += " Check!"
    if board.is_game_over():
        state["game_over"] = True
        state["result"] = board.result()
        if game_id:
            db_set_result(game_id, state["result"])
        msg += " " + describe_end(board)
    attach_gif(state, fen_before, ucis)
    return True, msg, board_payload(state, board, emove)


# ── TTS voice selection ──────────────────────────────────────────────────────
# The web UI lets the player pick Sigmond's voice (grouped by vendor). The choice
# is sent to /get_token, stored here, and applied per-call in on_swml_request when
# SignalWire fetches the SWML for the /chess handler. Single worker → a module
# global is sufficient (the game store is in-process for the same reason).
DEFAULT_VOICE = "elevenlabs.adam"
_selected_voice = {"voice": DEFAULT_VOICE}

def get_selected_voice():
    return _selected_voice.get("voice") or DEFAULT_VOICE

def set_selected_voice(voice):
    if voice:
        _selected_voice["voice"] = voice


# ── Session audio recording (opt-in from the UI) ─────────────────────────────
# The player chooses whether this session's audio is recorded. Like the voice
# pick, the choice is sent to /get_token and applied per-call in on_swml_request.
_record_audio = {"on": True}

def get_record_audio():
    return bool(_record_audio.get("on", True))

def set_record_audio(on):
    _record_audio["on"] = bool(on)


class ChessOpponent(AgentBase):
    """Sigmond - your AI chess opponent (voice + interactive board)."""

    def __init__(self):
        super().__init__(name="Chess", route="/chess", record_call=True, record_format="mp3")

        self.prompt_add_section(
            "Personality",
            "You are Sigmond, a sharp, charismatic chess master playing a live game against the "
            "player over video. You are encouraging but competitive, explain ideas clearly, and "
            "sprinkle in subtle references to SignalWire, AI Agents, and real-time communication. "
            "The player sees a live chess board on their screen that stays in sync with the game."
        )

        contexts = self.define_contexts()
        game = contexts.add_context("default") \
            .add_section("Goal", "Play an engaging game of chess against the player and narrate the action. "
                         "The player may speak their moves, OR play the ENTIRE game silently by clicking the board - "
                         "both are equally valid. Never wait to be spoken to before checking the board.")
        game.add_step("play") \
            .add_section("Current Task", "Play chess with the player, one move at a time.") \
            .add_bullets("CRITICAL RULES - FOLLOW EXACTLY", [
                "Right after you greet the player, START WATCHING THE BOARD: if they are silent, immediately call check_board_moves - many players make their very first move (and every move) by clicking, without ever speaking. Do NOT wait for them to talk first.",
                "The player speaks their move in plain language (e.g. 'pawn to e4', 'knight takes f6', 'castle kingside').",
                "Convert it to standard notation and call make_move with move set to SAN (like 'e4', 'Nf3', 'exd5', 'O-O') or UCI (like 'e2e4', 'e7e8q' for promotion).",
                "NEVER decide legality yourself and NEVER invent the board - make_move validates the move and returns exactly what happened, including MY reply move.",
                "After make_move returns, announce to the player what they played and what I played, plus check/checkmate if stated. Then WAIT for their next move.",
                "ALWAYS say moves in natural spoken English, never as raw notation: say 'knight to c4' (not 'N c 4'), 'pawn takes d5', 'castle kingside', 'e four' for e4, 'queen takes e7, check'. The function results already give you the spoken form — use it.",
                "If make_move says the move is illegal, tell the player and ask them to try another move - do NOT call make_move again until they give a new move.",
                "Call make_move only ONCE per SPOKEN move.",
                "If the player asks to start over, call new_game. If they ask to make it harder/easier or set a level, call change_difficulty. If they resign, call resign. If they ask what a term means, call explain_term.",
                "If the player says to hang up, end, end the game, quit, stop, that they're done, or goodbye, call end_call to say goodbye and hang up the call.",
                "The player can ALSO move by CLICKING the on-screen board. Those clicks are applied automatically and are NOT spoken to you - the ONLY way you learn about them is by calling check_board_moves.",
                "THEREFORE: any time the player is not speaking - after you finish talking, and repeatedly during every quiet moment - call check_board_moves. This is the one function you should call proactively without being asked. Keep calling it again and again while it is the player's turn; never assume the board hasn't changed.",
                "If check_board_moves returns new moves, acknowledge the PLAYER's move and comment on the position - but do NOT call make_move for them (they are already applied). If it returns nothing, that's fine - just call check_board_moves again on the next quiet moment. Do not go idle.",
                "If the board ever seems ahead of you, call get_board to resync before commenting.",
                "LEARN MODE: some games are coaching sessions. When they are, the make_move and "
                "check_board_moves results include a 'COACHING (learn mode)' note grading the player's "
                "move and naming a stronger option — deliver that note warmly and briefly as their coach. "
                "If the player asks for a hint or 'what should I play?', call hint and suggest the move "
                "without playing it for them. In normal Game mode you will get no coaching notes — just play.",
            ]) \
            .set_step_criteria("The game has ended (checkmate, stalemate, draw, or resignation).") \
            .set_functions(["make_move", "check_board_moves", "new_game", "change_difficulty", "resign", "get_board", "explain_term", "hint", "end_call"]) \
            .set_valid_steps(["play"])

        self.add_language(name="English", code="en-US", voice="elevenlabs.adam")

        # Help ASR with chess vocabulary + square names.
        squares = [f"{f}{r}" for f in "abcdefgh" for r in range(1, 9)]
        self.add_hints([
            "chess", "check", "checkmate", "stalemate", "castle", "castle kingside",
            "castle queenside", "en passant", "promote", "queen", "rook", "bishop",
            "knight", "pawn", "king", "takes", "captures", "resign", "draw",
            "hang up", "end game", "end the game", "quit", "goodbye", "I'm done",
        ] + squares)

        self.set_params({
            "vad_config": "75",
            "end_of_speech_timeout": 500,
            # Arm silence-timeout function calls from the very first turn so Sigmond
            # can poll check_board_moves for clicked moves even before you speak.
            "functions_on_speaker_timeout": True,
            # This game is played largely in SILENCE (the player clicks moves). Keep the AI
            # responsive (default short attention so nudged moves are voiced live + polling
            # stays frequent), but stop it ending the session during a long game:
            "inactivity_timeout": 1800000,   # 30 min before ending the session for inactivity
        })

        self.set_global_data({
            "assistant_name": "Sigmond",
            "game": "Chess",
            "note": "Authoritative board state is server-side; functions return the true position.",
        })

        # ── SWAIG tools ──────────────────────────────────────────────────────
        def _state_for(raw_data):
            gid = resolve_game_id(raw_data)
            if not gid or gid not in GAMES:
                gid, _ = create_game(player_name=resolve_player_name(raw_data))
            st = GAMES[gid]
            cid = (raw_data or {}).get("call_id")   # learn the live call so clicks can nudge it
            if cid:
                st["call_id"] = cid
            m = resolve_mode(raw_data)              # keep game/learn mode in sync with the client
            if m:
                st["mode"] = m
            return gid, st

        @self.tool(
            name="make_move",
            description="Make the player's chess move; the engine replies automatically.",
            parameters={
                "type": "object",
                "properties": {
                    "move": {
                        "type": "string",
                        "description": "The player's move in SAN (e4, Nf3, exd5, O-O, O-O-O, e8=Q) or UCI (e2e4, e7e8q).",
                    }
                },
                "required": ["move"],
            },
        )
        def make_move(args, raw_data):
            gid, state = _state_for(raw_data)
            ok, msg, payload = do_player_then_engine(state, args.get("move", ""), source="voice", game_id=gid)
            coach = state.pop("pending_coach", None)   # learn-mode coaching for this move
            if ok and coach:
                msg = msg + " " + coach
            result = SwaigFunctionResult(msg)
            result.swml_user_event(payload)            # push the new board to the browser now
            result.enable_functions_on_timeout(True)   # keep polling for board clicks armed
            result.wait_for_user(timeout=4)            # yield the turn so the board goes live immediately
            return result

        @self.tool(
            name="check_board_moves",
            description="Check whether the player made any moves on the on-screen board that you have not acknowledged yet. Call this whenever the player has been silent for a moment.",
            parameters={"type": "object", "properties": {}, "required": []},
        )
        def check_board_moves(args, raw_data):
            gid, state = _state_for(raw_data)
            rows = db_unnarrated(gid)   # (move_uuid, ply, san, source)
            board = chess.Board(state["fen"])
            if not rows:
                r = SwaigFunctionResult("No new board moves yet. Stay quiet, and call check_board_moves again shortly - keep polling while it is the player's turn.")
                r.enable_functions_on_timeout(True)
                r.wait_for_user(timeout=4)
                return r
            db_mark_narrated([row[0] for row in rows])
            # Attribute by source: click/voice moves are the PLAYER's, engine moves are YOURS.
            user_moves = [san for (_id, _ply, san, src) in rows if src != "engine"]
            agent_moves = [san for (_id, _ply, san, src) in rows if src == "engine"]
            um = ", ".join(spoken_move(s) for s in user_moves)
            am = ", ".join(spoken_move(s) for s in agent_moves)
            if user_moves and agent_moves:
                body = (f"The USER moved {um} on the board, and you (the AGENT, Sigmond) already "
                        f"replied {am}. Announce the USER's move, then state your reply.")
            elif agent_moves and not user_moves:
                # engine's opening move in a game where the player is Black
                body = (f"You (the AGENT, Sigmond) have OPENED the game with {am} — the player is "
                        f"playing Black, so it's their move now. Announce that you opened with {am} "
                        f"and invite them to make their move.")
            else:
                body = f"The USER moved {um} on the board. Acknowledge their move."
            coach = state.pop("pending_coach", None)   # learn-mode coaching for the clicked move
            coach_txt = (" " + coach) if coach else ""
            r = SwaigFunctionResult(
                "CHESS BOARD UPDATE (not speech from the user). " + body + coach_txt +
                " These moves are ALREADY applied — do NOT call make_move. Never swap who made "
                "which move (USER moves are the player's, AGENT moves are yours). "
                "Briefly comment, then keep watching the board.")
            r.swml_user_event(board_payload(state, board))
            r.enable_functions_on_timeout(True)
            r.wait_for_user(timeout=4)
            return r

        @self.tool(
            name="new_game",
            description="Start a brand new game of chess.",
            parameters={
                "type": "object",
                "properties": {
                    "player_color": {
                        "type": "string",
                        "description": "Which color the player takes: 'white' (default, moves first) or 'black'.",
                    }
                },
                "required": [],
            },
        )
        def new_game(args, raw_data):
            gid = resolve_game_id(raw_data)
            color = args.get("player_color", "white")
            prev = GAMES.get(gid, {})
            diff = prev.get("difficulty", "medium")
            name = prev.get("player_name") or resolve_player_name(raw_data)
            mode = resolve_mode(raw_data) or prev.get("mode", "game")
            state = _new_state(diff, color, name, mode)
            if not gid:
                gid = uuid.uuid4().hex[:12]
            with GAMES_LOCK:
                GAMES[gid] = state
                _LAST_GAME_ID["id"] = gid
            db_new_game(gid, state["difficulty"], state["player_color"], state["player_name"])
            board = chess.Board(state["fen"])
            msg = "New game! You're playing White. Your move." if state["player_color"] == "white" \
                  else "New game! You're playing Black, so I'll open."
            last = None
            if state["player_color"] == "black":
                emove = engine_reply(board, state["difficulty"])
                if emove:
                    san = apply_move(state, board, emove, gid, "engine", narrated=True)
                    msg = f"New game! You're Black. I open with {spoken_move(san)}. Your move."
                    last = emove
                    attach_gif(state, START_FEN, [emove.uci()])
            result = SwaigFunctionResult(msg)
            result.swml_user_event(board_payload(state, board, last))
            result.enable_functions_on_timeout(True)
            return result

        @self.tool(
            name="change_difficulty",
            description="Change the engine strength. Accepts a level name or 'harder'/'easier'.",
            parameters={
                "type": "object",
                "properties": {
                    "level": {
                        "type": "string",
                        "description": "One of beginner, easy, medium, hard, master, or 'harder'/'easier'.",
                    }
                },
                "required": ["level"],
            },
        )
        def change_difficulty(args, raw_data):
            gid, state = _state_for(raw_data)
            lvl = str(args.get("level", "")).strip().lower()
            cur = state.get("difficulty", "medium")
            if lvl in ("harder", "stronger", "up"):
                idx = min(len(DIFFICULTY_ORDER) - 1, DIFFICULTY_ORDER.index(cur) + 1)
                lvl = DIFFICULTY_ORDER[idx]
            elif lvl in ("easier", "weaker", "down"):
                idx = max(0, DIFFICULTY_ORDER.index(cur) - 1)
                lvl = DIFFICULTY_ORDER[idx]
            if lvl not in DIFFICULTY:
                return SwaigFunctionResult(
                    f"I can play at these levels: {', '.join(DIFFICULTY_ORDER)}. Which would you like?")
            state["difficulty"] = lvl
            board = chess.Board(state["fen"])
            result = SwaigFunctionResult(f"Difficulty set to {lvl}. It's still your move.")
            result.swml_user_event(board_payload(state, board))
            return result

        @self.tool(
            name="resign",
            description="The player resigns the current game.",
            parameters={"type": "object", "properties": {}, "required": []},
        )
        def resign(args, raw_data):
            gid, state = _state_for(raw_data)
            state["game_over"] = True
            state["result"] = "0-1" if state["player_color"] == "white" else "1-0"
            db_set_result(gid, state["result"])
            board = chess.Board(state["fen"])
            payload = board_payload(state, board)
            payload["game_over"] = True
            payload["status"] = "resigned"
            payload["result"] = state["result"]
            result = SwaigFunctionResult("You resign. Good game! Say 'new game' whenever you'd like a rematch.")
            result.swml_user_event(payload)
            return result

        @self.tool(
            name="end_call",
            description="End the session and hang up the call. Use whenever the player says to hang up, "
                        "end, end the game, quit, stop, that they're done, or goodbye.",
            parameters={"type": "object", "properties": {}, "required": []},
        )
        def end_call(args, raw_data):
            _state_for(raw_data)   # capture call context if present
            result = SwaigFunctionResult("Thanks for playing! Good game — hanging up now. Goodbye.")
            result.swml_user_event({"type": "call_ended", "reason": "agent"})  # client: do NOT auto-reconnect
            result.hangup()
            return result

        @self.tool(
            name="get_board",
            description="Describe the current position (use to resync if unsure).",
            parameters={"type": "object", "properties": {}, "required": []},
        )
        def get_board(args, raw_data):
            gid, state = _state_for(raw_data)
            board = chess.Board(state["fen"])
            turn = "your" if (board.turn == chess.WHITE) == (state["player_color"] == "white") else "my"
            bal = material_balance(board)
            who = "even material" if bal == 0 else (f"White up {abs(bal)}" if bal > 0 else f"Black up {abs(bal)}")
            last = spoken_move(state["history"][-1]) if state["history"] else "no moves yet"
            result = SwaigFunctionResult(
                f"It's {turn} move. Last move: {last}. Material: {who}. "
                f"Move {len(state['history'])} of the game.")
            result.swml_user_event(board_payload(state, board))
            return result

        @self.tool(
            name="explain_term",
            description="Explain a chess term in plain language.",
            parameters={
                "type": "object",
                "properties": {"term": {"type": "string", "description": "The chess term to explain."}},
                "required": ["term"],
            },
        )
        def explain_term(args, raw_data):
            term = str(args.get("term", "")).strip().lower()
            for key, val in CHESS_GLOSSARY.items():
                if key in term or term in key:
                    return SwaigFunctionResult(f"{key.title()}: {val}. Now, back to the game.")
            return SwaigFunctionResult(
                "I'm not sure of that exact term, but ask me about castling, en passant, "
                "check, pins, forks, or promotion.")

        @self.tool(
            name="hint",
            description="Suggest a strong move for the PLAYER to consider. Use in Learn mode or "
                        "whenever the player asks 'what should I play?', 'give me a hint', or for advice.",
            parameters={"type": "object", "properties": {}, "required": []},
        )
        def hint(args, raw_data):
            gid, state = _state_for(raw_data)
            board = chess.Board(state["fen"])
            your_turn = (board.turn == chess.WHITE) == (state["player_color"] == "white")
            if board.is_game_over():
                return SwaigFunctionResult("The game is over — nothing to suggest. Start a new game to play on.")
            if not your_turn:
                return SwaigFunctionResult("It's my move right now — I'll play, then I can suggest your reply.")
            tops = best_moves(state["fen"], 3)
            if not tops:
                return SwaigFunctionResult("Let me look at the board — try again in a moment.")
            best = spoken_move(tops[0][0])
            alts = ", ".join(spoken_move(s) for s, _cp in tops[1:3])
            extra = f" Other reasonable tries: {alts}." if alts else ""
            return SwaigFunctionResult(
                f"HINT: a strong move here is {best}.{extra} Suggest {best} to the player and explain "
                "in one short sentence why it's a good idea — but let THEM make the move.")

    def on_swml_request(self, request_data=None, callback_path=None, request=None):
        """Apply per-call choices (TTS voice + audio recording) set via /get_token."""
        voice = get_selected_voice()
        # Rebuild the single language so the voice reflects the latest selection
        # (add_language appends, so clear first to avoid accumulation across calls).
        if hasattr(self, "_languages"):
            self._languages = []
        self.add_language(name="English", code="en-US", voice=voice)
        # Honor the player's recording choice (the record_call verb is emitted at
        # render time based on self._record_call).
        self._record_call = get_record_audio()
        print(f"chess: using voice {voice}, record={self._record_call}", flush=True)
        return super().on_swml_request(request_data, callback_path, request)

HOST = "0.0.0.0"
PORT = int(os.environ.get('PORT', 5000))


def get_signalwire_host():
    """Get the full SignalWire host from space name."""
    space = os.getenv("SIGNALWIRE_SPACE_NAME", "")
    if not space:
        return None
    # If it's already a full domain, use it as-is
    if "." in space:
        return space
    # Otherwise append .signalwire.com
    return f"{space}.signalwire.com"


def find_resource_address(addresses, agent_name):
    """
    Find the resource address matching /public/{agent_name} from a list of addresses.

    When phone numbers are attached to a handler, multiple addresses exist.
    We want the resource address (e.g., /public/chess) not the phone number address.
    """
    expected_address = f"/public/{agent_name}"

    # First, try to find exact match for /public/{agent_name}
    for addr in addresses:
        audio_channel = addr.get("channels", {}).get("audio", "")
        if audio_channel == expected_address:
            return addr

    # Fallback: find any address that looks like a resource address (not a phone number)
    for addr in addresses:
        audio_channel = addr.get("channels", {}).get("audio", "")
        if audio_channel.startswith("/public/") and not any(c.isdigit() for c in audio_channel.split("/")[-1][:3]):
            return addr

    # Last resort: return first address
    return addresses[0] if addresses else None


def find_existing_handler(client, agent_name):
    """Find an existing SWML handler by name via RestClient.

    Response shapes are identical to the legacy raw Fabric endpoints, so the
    parsing below is a 1:1 port: list items carry
    {id, display_name, swml_webhook: {name, primary_request_url}}, and
    list_addresses() returns {data: [{id, channels: {audio: "/public/..."}}]}.
    """
    try:
        # swml_webhooks is the current path for an External SWML Handler.
        handlers = client.fabric.swml_webhooks.list().get("data", [])

        for handler in handlers:
            # The name is nested in swml_webhook object
            swml_webhook = handler.get("swml_webhook", {})
            handler_name = swml_webhook.get("name") or handler.get("display_name")

            # Check if this handler matches our agent name
            if handler_name == agent_name:
                handler_id = handler.get("id")
                handler_url = swml_webhook.get("primary_request_url", "")
                # Get the address for this handler
                addresses = client.fabric.swml_webhooks.list_addresses(handler_id).get("data", [])
                resource_addr = find_resource_address(addresses, agent_name)
                if resource_addr:
                    return {
                        "id": handler_id,
                        "name": handler_name,
                        "url": handler_url,
                        "address_id": resource_addr["id"],
                        "address": resource_addr["channels"]["audio"]
                    }
    except Exception as e:
        print(f"Error checking existing handlers: {e}")
    return None


def setup_swml_handler():
    """Set up SWML handler on startup (via the unified RestClient)."""
    global swml_setup_error
    sw_host = get_signalwire_host()
    project = os.getenv("SIGNALWIRE_PROJECT_ID", "")
    token = os.getenv("SIGNALWIRE_TOKEN", "")
    agent_name = os.getenv("AGENT_NAME", "chess")
    proxy_url = os.getenv("SWML_PROXY_URL_BASE", os.getenv("APP_URL", ""))
    auth_user = os.getenv("SWML_BASIC_AUTH_USER", "signalwire")
    auth_pass = os.getenv("SWML_BASIC_AUTH_PASSWORD", "")

    if not all([sw_host, project, token]):
        swml_setup_error = "SignalWire credentials not configured (SIGNALWIRE_SPACE_NAME/PROJECT_ID/TOKEN)"
        print(f"{swml_setup_error} - skipping SWML handler setup")
        return

    if not proxy_url:
        swml_setup_error = "SWML_PROXY_URL_BASE/APP_URL not set"
        print(f"{swml_setup_error} - skipping SWML handler setup")
        return

    # Build SWML URL with basic auth credentials
    if auth_user and auth_pass and "://" in proxy_url:
        scheme, rest = proxy_url.split("://", 1)
        swml_url = f"{scheme}://{auth_user}:{auth_pass}@{rest}/chess"
    else:
        swml_url = proxy_url + "/chess"

    # RestClient() no-args reads SIGNALWIRE_API_TOKEN/SIGNALWIRE_SPACE, which do NOT
    # match this demo's SIGNALWIRE_TOKEN/SIGNALWIRE_SPACE_NAME convention — pass explicitly.
    client = RestClient(project=project, token=token, host=sw_host)

    # Look for an existing handler by name
    existing = find_existing_handler(client, agent_name)
    if existing:
        swml_handler_info["id"] = existing["id"]
        swml_handler_info["address_id"] = existing["address_id"]
        swml_handler_info["address"] = existing["address"]

        # Always update the URL to ensure credentials are current
        try:
            client.fabric.swml_webhooks.update(
                existing["id"],
                primary_request_url=swml_url,
                primary_request_method="POST",
            )
            print(f"Updated SWML handler: {existing['name']}")
        except Exception as e:
            print(f"Failed to update handler URL: {e}")

        print(f"Call address: {existing['address']}")
        swml_setup_error = None
    else:
        # Create a new external SWML handler with the agent name.
        try:
            # create() emits a DeprecationWarning steering phone-number setups toward
            # phone_numbers.set_swml_webhook; a standalone *dialable* handler (guest
            # tokens dial its /public/{name} address) is intentional here.
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", DeprecationWarning)
                handler = client.fabric.swml_webhooks.create(
                    name=agent_name,
                    used_for="calling",
                    primary_request_url=swml_url,
                    primary_request_method="POST",
                )
            handler_id = handler.get("id")
            swml_handler_info["id"] = handler_id

            # Get the address for this handler
            addresses = client.fabric.swml_webhooks.list_addresses(handler_id).get("data", [])
            resource_addr = find_resource_address(addresses, agent_name)
            if resource_addr:
                swml_handler_info["address_id"] = resource_addr["id"]
                swml_handler_info["address"] = resource_addr["channels"]["audio"]
                print(f"Created SWML handler: {agent_name}")
                print(f"Call address: {swml_handler_info['address']}")
                swml_setup_error = None
            else:
                swml_setup_error = "No address found for created handler"
                print(swml_setup_error)
        except Exception as e:
            swml_setup_error = f"Failed to create SWML handler: {e}"
            print(swml_setup_error)
            # Retry finding existing handler (another worker may have just created it)
            time.sleep(0.5)
            existing = find_existing_handler(client, agent_name)
            if existing:
                swml_handler_info["id"] = existing["id"]
                swml_handler_info["address_id"] = existing["address_id"]
                swml_handler_info["address"] = existing["address"]
                print(f"Found existing SWML handler after retry: {existing['name']}")
                print(f"Call address: {existing['address']}")
                swml_setup_error = None


def create_server(port=None):
    """Create AgentServer with static file mounting and API endpoints."""
    server = AgentServer(host=HOST, port=port or PORT)
    server.register(ChessOpponent(), "/chess")

    # Serve static files using SDK's built-in method
    web_dir = Path(__file__).parent / "web"
    if web_dir.exists():
        server.serve_static_files(str(web_dir))

    # Add /get_token endpoint for WebRTC calls
    @server.app.get('/get_token')
    def get_token(voice: str = "", record: str = ""):
        """Get a guest token for the web client to call the agent.

        `voice` (optional) is the TTS voice the player picked in the UI, and
        `record` (optional, "true"/"false") is whether to record this session's
        audio. Both are stored and applied when SignalWire fetches the SWML.
        """
        if voice:
            set_selected_voice(voice)
            print(f"chess: stored voice selection {voice}", flush=True)
        if record != "":
            on = record.strip().lower() in ("1", "true", "yes", "on")
            set_record_audio(on)
            print(f"chess: stored recording choice {on}", flush=True)
        sw_host = get_signalwire_host()
        project = os.getenv("SIGNALWIRE_PROJECT_ID", "")
        token = os.getenv("SIGNALWIRE_TOKEN", "")

        if not all([sw_host, project, token]):
            return JSONResponse({"error": "SignalWire credentials not configured"}, status_code=500)

        # If startup registration didn't complete (stray extra worker, or creds/
        # tunnel arrived late), lazily retry once under a lock before giving up.
        if not swml_handler_info["address_id"]:
            with _swml_setup_lock:
                if not swml_handler_info["address_id"]:
                    setup_swml_handler()

        if not swml_handler_info["address_id"]:
            print(f"get_token failed: swml_handler_info = {swml_handler_info}")
            print(f"  SIGNALWIRE_SPACE_NAME={os.getenv('SIGNALWIRE_SPACE_NAME', '(not set)')}")
            print(f"  SIGNALWIRE_PROJECT_ID={'set' if os.getenv('SIGNALWIRE_PROJECT_ID') else '(not set)'}")
            print(f"  SIGNALWIRE_TOKEN={'set' if os.getenv('SIGNALWIRE_TOKEN') else '(not set)'}")
            print(f"  SWML_PROXY_URL_BASE={os.getenv('SWML_PROXY_URL_BASE', '(not set)')}")
            print(f"  APP_URL={os.getenv('APP_URL', '(not set)')}")
            return JSONResponse(
                {"error": f"SWML handler not registered: {swml_setup_error or 'unknown reason - check startup logs'}"},
                status_code=500,
            )

        try:
            # Create a guest token with access to this address (valid 24 hours).
            client = RestClient(project=project, token=token, host=sw_host)
            expire_at = int(time.time()) + 3600 * 24
            guest = client.fabric.tokens.create_guest_token(
                allowed_addresses=[swml_handler_info["address_id"]],
                expire_at=expire_at,
            )
            guest_token = guest.get("token", "")

            return {
                "token": guest_token,
                "address": swml_handler_info["address"],
                "address_id": swml_handler_info["address_id"],
            }

        except Exception as e:
            print(f"Token request failed: {e}")
            return JSONResponse({"error": str(e)}, status_code=500)

    # Add /get_resource_info endpoint for dashboard links
    @server.app.get('/get_resource_info')
    def get_resource_info():
        """Get SWML handler resource info for linking to SignalWire dashboard."""
        sw_host = get_signalwire_host()
        return {
            "space_name": os.getenv("SIGNALWIRE_SPACE_NAME", ""),
            "resource_id": swml_handler_info["id"],
            "dashboard_url": f"https://{sw_host}/neon/resources/{swml_handler_info['id']}/edit?t=addresses" if sw_host and swml_handler_info["id"] else None
        }

    # ── Interactive-board REST API (shares the GAMES store with the voice path) ──
    @server.app.post('/chess/new')
    def chess_new(difficulty: str = "medium", player_color: str = "white", player_name: str = "", mode: str = "game"):
        """Create a game for the on-screen board; returns game_id + initial board."""
        gid, state = create_game(difficulty, player_color, player_name, mode)
        board = chess.Board(state["fen"])
        last = None
        if state["player_color"] == "black":
            emove = engine_reply(board, state["difficulty"])
            if emove:
                # narrated=False so check_board_moves catches it on connect and Sigmond
                # announces his opening move (this game is created before the AI call starts).
                apply_move(state, board, emove, gid, "engine", narrated=False)
                last = emove
                attach_gif(state, START_FEN, [emove.uci()])
        return {"game_id": gid, "board": board_payload(state, board, last)}

    @server.app.post('/chess/move')
    def chess_move(game_id: str, move: str):
        """Apply a board (click/drag) move; engine replies. Returns the new board."""
        state = GAMES.get(game_id)
        if state is None:
            return JSONResponse({"error": "unknown game_id"}, status_code=404)
        before = len(state["history"])
        ok, message, payload = do_player_then_engine(state, move, source="click", game_id=game_id)
        # Deterministic nudge: if we know the live call, push the move to Sigmond now
        # so he acknowledges it immediately (the check_board_moves poll is the fallback).
        cid = state.get("call_id")
        if ok and cid:
            added = state["history"][before:]          # SANs added this turn: [user, engine?]
            user_san = added[0] if added else move
            agent_san = added[1] if len(added) > 1 else None
            threading.Thread(target=nudge_ai_move,
                             args=(game_id, cid, user_san, agent_san), daemon=True).start()
        return {"ok": ok, "message": message, "board": payload}

    @server.app.get('/chess/state')
    def chess_state(game_id: str):
        """Current board for initial render / resync."""
        state = GAMES.get(game_id)
        if state is None:
            return JSONResponse({"error": "unknown game_id"}, status_code=404)
        board = chess.Board(state["fen"])
        return {"board": board_payload(state, board)}

    @server.app.get('/chess/analysis')
    def chess_analysis(game_id: str):
        """Stockfish eval + best move for the current position (Learn-mode eval bar +
        best-move arrow). Off the move path, so moves stay instant."""
        state = GAMES.get(game_id)
        if state is None:
            return JSONResponse({"error": "unknown game_id"}, status_code=404)
        return position_eval(state["fen"]) or {"error": "analysis unavailable"}

    @server.app.post('/chess/rename')
    def chess_rename(game_id: str, player_name: str = ""):
        """Attach/update the player's name on the current game (so it attributes on
        the leaderboard even though the game was created before the name was entered)."""
        nm = (player_name or "").strip()[:32]
        state = GAMES.get(game_id)
        if state is not None:
            state["player_name"] = nm
        db_set_player_name(game_id, nm)
        return {"ok": True, "player_name": nm}

    @server.app.post('/chess/resign')
    def chess_resign(game_id: str):
        """Resign the on-screen game; records the loss for the leaderboard."""
        state = GAMES.get(game_id)
        if state is None:
            return JSONResponse({"error": "unknown game_id"}, status_code=404)
        state["game_over"] = True
        state["result"] = "0-1" if state["player_color"] == "white" else "1-0"
        db_set_result(game_id, state["result"])
        board = chess.Board(state["fen"])
        payload = board_payload(state, board)
        payload["game_over"] = True
        payload["status"] = "resigned"
        payload["result"] = state["result"]
        return {"ok": True, "board": payload}

    @server.app.get('/chess/stats')
    def chess_stats():
        """UUID-stamped move statistics across all games (voice vs click, results)."""
        return db_stats()

    @server.app.get('/chess/leaderboard')
    def chess_leaderboard():
        """Per-player standings (difficulty-weighted points, W/L/D, win rate)."""
        return db_leaderboard()

    @server.app.get('/chess/gif/{gif_id}')
    def chess_gif(gif_id: str):
        """Serve a rendered move animation (overlaid in the video window).
        Rendered lazily here so the move path stays instant."""
        data = get_gif_bytes(gif_id)
        if data is None:
            return JSONResponse({"error": "unknown gif_id"}, status_code=404)
        return Response(content=data, media_type="image/gif",
                        headers={"Cache-Control": "public, max-age=86400"})

    # Set up SWML handler on startup
    @server.app.on_event("startup")
    async def on_startup():
        setup_swml_handler()

    return server


# Create server and expose app for gunicorn
server = create_server()
app = server.app

if __name__ == "__main__":
    server.run()
