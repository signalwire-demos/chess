// SignalWire Chess — web client.
// Board state is authoritative on the server (python-chess + Stockfish). Both
// the voice path (AI make_move) and this board (POST /chess/move) mutate it, and
// the server pushes board_update user_events so voice moves animate here too.

// ---- state ----
let client = null, call = null;
let subscriptions = [];
let currentToken = null, currentDestination = null, agentAddressId = null;
let gameId = null;
let board = null;          // last board_update payload
let selected = null;       // selected square (algebraic) awaiting a target
let isMuted = false, teardownDone = false, busy = false;
let mode = (localStorage.getItem('chessMode') === 'learn') ? 'learn' : 'game';  // 'game' | 'learn'

// U+FE0E (text variation selector) forces monochrome TEXT rendering — without it,
// mobile browsers (iOS/Android) draw these as emoji, which resize squares and can
// degrade to look-alike glyphs. We color them white/black via CSS instead.
const GLYPH = { p:'♟︎', n:'♞︎', b:'♝︎', r:'♜︎', q:'♛︎', k:'♚︎' };

// ---- dom ----
const $ = id => document.getElementById(id);
const boardEl = $('board');
const filesTop = $('files-top'), filesBottom = $('files-bottom'), ranksLeft = $('ranks-left'), ranksRight = $('ranks-right');
const trayTop = $('tray-top'), trayBottom = $('tray-bottom');
const connEl = $('conn'), turnEl = $('turn'), levelEl = $('level'), matEl = $('material');
const historyEl = $('history'), logEl = $('log'), bannerEl = $('banner');
const reconnectBanner = $('reconnectBanner'), reconnectText = $('reconnectText');
const connectBtn = $('connectBtn'), hangupBtn = $('hangupBtn'), muteBtn = $('muteBtn');
const newBtn = $('newBtn'), resignBtn = $('resignBtn');
const difficultySel = $('difficulty'), colorSel = $('color');
const modeToggle = $('modeToggle'), modeHint = $('modeHint');
const evalbar = $('evalbar'), evalfill = $('evalfill'), arrows = $('arrows'), evalChip = $('evalChip'), evalVal = $('evalVal');
const connChip = $('connChip');
const nameChip = $('nameChip'), nameLabel = $('nameLabel'), nameAvatar = $('nameAvatar');
const nameDialog = $('nameDialog'), nameInput = $('nameInput'), nameSave = $('nameSave'), nameSkip = $('nameSkip');

// ---- player name (persisted locally; goes on the leaderboard) ----
function getPlayerName() { return (localStorage.getItem('chessPlayerName') || '').trim(); }
function setPlayerName(n) {
  n = (n || '').trim().slice(0, 32);
  if (n) localStorage.setItem('chessPlayerName', n); else localStorage.removeItem('chessPlayerName');
  refreshNameChip();
}
function refreshNameChip() {
  const stored = getPlayerName(), shown = stored || 'Guest';
  nameLabel.textContent = shown;
  nameAvatar.textContent = (stored ? stored[0] : '?').toUpperCase();
}
function openNameDialog() { nameInput.value = getPlayerName(); nameDialog.classList.add('show'); setTimeout(() => nameInput.focus(), 60); }
function closeNameDialog() { nameDialog.classList.remove('show'); }
// Attach the current name to the live game on the server so it attributes on the
// leaderboard (the game is created anonymously on page load, before the name exists).
async function attributeGame() {
  if (!gameId || !getPlayerName()) return;
  try { await api(`/chess/rename?game_id=${encodeURIComponent(gameId)}&player_name=${encodeURIComponent(getPlayerName())}`, { method: 'POST' }); }
  catch (e) { logEvent('rename failed', { error: e.message }); }
}
function saveNameFromDialog() { setPlayerName(nameInput.value); closeNameDialog(); attributeGame(); }

function logEvent(msg, data) {
  const t = new Date().toLocaleTimeString();
  const line = document.createElement('div');
  line.innerHTML = `<span class="t">${t}</span> ${msg}` + (data ? ` <span style="opacity:.7">${escapeHtml(JSON.stringify(data))}</span>` : '');
  logEl.prepend(line);
}
function escapeHtml(s){return String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));}

// ---- REST API ----
async function api(path, opts) {
  const r = await fetch(path, opts);
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.error || `HTTP ${r.status}`);
  return j;
}
let newGameSeq = 0;
async function newGame() {
  const seq = ++newGameSeq;   // guards against a slower earlier new-game clobbering a newer one
  const difficulty = difficultySel.value, player_color = colorSel.value;
  const j = await api(`/chess/new?difficulty=${encodeURIComponent(difficulty)}&player_color=${encodeURIComponent(player_color)}&player_name=${encodeURIComponent(getPlayerName())}&mode=${encodeURIComponent(mode)}`, { method: 'POST' });
  if (seq !== newGameSeq) return;   // a newer newGame() started; discard this stale result
  gameId = j.game_id;
  selected = null;
  logEvent('New game', { game_id: gameId, difficulty, player_color });
  renderBoard(j.board);
}
async function sendMove(uci) {
  if (!gameId) return;
  busy = true;
  try {
    const j = await api(`/chess/move?game_id=${encodeURIComponent(gameId)}&move=${encodeURIComponent(uci)}`, { method: 'POST' });
    if (!j.ok) { logEvent('Illegal', { move: uci, why: j.message }); }
    // Clear busy BEFORE rendering so the new position renders interactive (clickable).
    // The server records the click + Sigmond's reply; the deterministic nudge (or the
    // check_board_moves poll as fallback) makes Sigmond acknowledge it.
    busy = false;
    renderBoard(j.board);
  } catch (e) {
    busy = false;
    logEvent('Move error', { error: e.message });
    if (board) renderBoard(board);   // re-enable the board so a transient error doesn't lock it
  }
}

// ---- board rendering ----
function fenToBoard(fen) {
  // returns rows[0..7] where row 0 = rank 8, each row[0..7] file a..h; value = piece char or null
  const rows = [];
  fen.split(' ')[0].split('/').forEach(rankStr => {
    const row = [];
    for (const ch of rankStr) {
      if (/\d/.test(ch)) { for (let i = 0; i < +ch; i++) row.push(null); }
      else row.push(ch);
    }
    rows.push(row);
  });
  return rows;
}
function sqName(fileIdx, rankFromTop) { // rankFromTop 0=rank8 ... 7=rank1 (board array order)
  return 'abcdefgh'[fileIdx] + (8 - rankFromTop);
}
function fmtMaterial(m) {
  if (!m) return 'even';
  return m > 0 ? `White +${m}` : `Black +${-m}`;
}

function renderBoard(p) {
  board = p;
  const flip = (p.player_color === 'black');

  // status
  const yourTurn = (p.turn === p.player_color);
  const me = getPlayerName() || 'You';
  turnEl.textContent = p.game_over ? 'game over' : `${p.turn} · ${yourTurn ? me : 'Sigmond'}`;
  levelEl.textContent = p.difficulty;
  matEl.textContent = fmtMaterial(p.material);

  // captured trays: opponent's losses on the far side, yours near you
  const myCaps = (p.player_color === 'white') ? p.captured_by_white : p.captured_by_black;
  const oppCaps = (p.player_color === 'white') ? p.captured_by_black : p.captured_by_white;
  renderTray(trayTop, oppCaps);
  renderTray(trayBottom, myCaps);

  // find king-in-check square
  let checkSq = null;
  if (p.in_check && !p.game_over) {
    const rows = fenToBoard(p.fen);
    const kc = (p.turn === 'white') ? 'K' : 'k';
    for (let r = 0; r < 8; r++) for (let f = 0; f < 8; f++) if (rows[r][f] === kc) checkSq = sqName(f, r);
  }

  // legal targets for the selected square
  const targets = new Set();
  if (selected) for (const uci of (p.legal_moves || [])) if (uci.slice(0, 2) === selected) targets.add(uci.slice(2, 4));

  const rows = fenToBoard(p.fen);
  const lastFrom = p.last_move ? p.last_move.slice(0, 2) : null;
  const lastTo = p.last_move ? p.last_move.slice(2, 4) : null;

  boardEl.innerHTML = '';
  const order = flip ? [...Array(8).keys()].reverse() : [...Array(8).keys()];
  const fileOrder = flip ? [...Array(8).keys()].reverse() : [...Array(8).keys()];

  // coordinate labels in the frame gutter (files top+bottom, ranks left+right)
  const filesArr = flip ? [...'hgfedcba'] : [...'abcdefgh'];
  const ranksArr = flip ? [1,2,3,4,5,6,7,8] : [8,7,6,5,4,3,2,1];
  const spans = arr => arr.map(v => `<span>${v}</span>`).join('');
  filesTop.innerHTML = filesBottom.innerHTML = spans(filesArr);
  ranksLeft.innerHTML = ranksRight.innerHTML = spans(ranksArr);

  for (const r of order) {
    for (const f of fileOrder) {
      const name = sqName(f, r);
      const piece = rows[r][f];
      const light = (r + f) % 2 === 0;
      const sq = document.createElement('div');
      sq.className = 'sq ' + (light ? 'light' : 'dark');
      sq.dataset.sq = name;
      if (name === lastFrom || name === lastTo) sq.classList.add('last');
      if (name === checkSq) sq.classList.add('check');
      if (name === selected) sq.classList.add('sel');

      if (piece) {
        const isWhite = piece === piece.toUpperCase();
        const pc = document.createElement('span');
        pc.className = 'pc ' + (isWhite ? 'w' : 'b');
        pc.textContent = GLYPH[piece.toLowerCase()];
        sq.appendChild(pc);
      }
      if (targets.has(name)) {
        const marker = document.createElement('span');
        marker.className = piece ? 'ring' : 'dot';
        sq.appendChild(marker);
      }

      // interactivity: only when connected, your turn, not busy, not over
      const myPieceHere = piece && ((p.player_color === 'white') === (piece === piece.toUpperCase()));
      // Interactivity depends only on it being your turn in a live call — NOT on `busy`.
      // `busy` (an in-flight move) is enforced in onSquareClick instead, so a render that
      // happens mid-move can never leave the board permanently locked.
      const canInteract = call && yourTurn && !p.game_over;
      if (canInteract && (myPieceHere || targets.has(name))) {
        sq.classList.add('playable');
        sq.addEventListener('click', () => onSquareClick(name, piece));
      }
      boardEl.appendChild(sq);
    }
  }

  // history
  renderHistory(p.history || []);

  // banner
  if (p.game_over) {
    let txt = 'Game over.';
    if (p.status === 'checkmate') txt = '♚ Checkmate!';
    else if (p.status === 'stalemate') txt = 'Stalemate — draw.';
    else if (p.status === 'resigned') txt = 'You resigned.';
    else if (p.status && p.status.startsWith('draw')) txt = 'Draw.';
    if (p.result) txt += `  (${p.result})`;
    bannerEl.textContent = txt + '  —  press New game for a rematch.';
    bannerEl.classList.add('show');
  } else if (p.status === 'check') {
    bannerEl.textContent = 'Check!';
    bannerEl.classList.add('show');
  } else {
    bannerEl.classList.remove('show');
  }

  newBtn.disabled = !gameId;
  resignBtn.disabled = !gameId || p.game_over;

  playMoveGif(p);
  updateAnalysis(p);   // learn-mode eval bar + best-move arrow (no-op in game mode)
}

// ---- Learn-mode eval bar + best-move arrow (fed by /chess/analysis) ----
let analysisSeq = 0;
function sqToXY(sq, flip) {            // 'e2' -> [x,y] in the board's 0..8 space
  const f = sq.charCodeAt(0) - 97, r = parseInt(sq[1], 10);
  const col = flip ? 7 - f : f;
  const rowFromTop = flip ? r - 1 : 8 - r;
  return [col + 0.5, rowFromTop + 0.5];
}
function clearArrow() { arrows.innerHTML = ''; }
function drawArrow(from, to) {
  const [x1, y1] = from, [x2, y2] = to;
  const dx = x2 - x1, dy = y2 - y1, len = Math.hypot(dx, dy) || 1, ux = dx / len, uy = dy / len;
  const head = 0.6, w = 0.34;
  const bx = x2 - ux * head, by = y2 - uy * head, px = -uy * w, py = ux * w;
  arrows.innerHTML =
    `<line x1="${x1}" y1="${y1}" x2="${bx.toFixed(3)}" y2="${by.toFixed(3)}"></line>` +
    `<polygon class="ah" points="${x2},${y2} ${(bx + px).toFixed(3)},${(by + py).toFixed(3)} ${(bx - px).toFixed(3)},${(by - py).toFixed(3)}"></polygon>`;
}
function hideAnalysis() { evalbar.style.display = 'none'; evalChip.style.display = 'none'; clearArrow(); }
async function updateAnalysis(p) {
  if (mode !== 'learn' || !gameId) { hideAnalysis(); return; }
  const seq = ++analysisSeq;
  evalbar.style.display = 'block';
  let a;
  try { a = await api(`/chess/analysis?game_id=${encodeURIComponent(gameId)}`); }
  catch (e) { return; }
  if (seq !== analysisSeq) return;                 // superseded by a newer position
  // eval -> White's share of the bar (0..100) via a soft sigmoid
  let whitePct = 50, label = '—';
  if (a.mate != null) { whitePct = a.mate > 0 ? 100 : 0; label = (a.mate > 0 ? 'M' : '-M') + Math.abs(a.mate); }
  else if (a.cp != null) { whitePct = 50 + 50 * (2 / (1 + Math.exp(-0.004 * a.cp)) - 1); label = (a.cp >= 0 ? '+' : '') + (a.cp / 100).toFixed(1); }
  // orient the bar to the board: the player's colour sits at the bottom
  const flip = (p.player_color === 'black');
  if (!flip) { evalbar.style.background = '#26303b'; evalfill.style.background = '#eef2f8'; evalfill.style.height = whitePct + '%'; }
  else { evalbar.style.background = '#eef2f8'; evalfill.style.background = '#26303b'; evalfill.style.height = (100 - whitePct) + '%'; }
  evalChip.style.display = '';
  evalVal.textContent = label;
  // best-move arrow: only when it's the player's turn (so we're advising them)
  const yourTurn = (a.turn === p.player_color);
  if (a.best_move && yourTurn && !a.game_over && !p.game_over)
    drawArrow(sqToXY(a.best_move.slice(0, 2), flip), sqToXY(a.best_move.slice(2, 4), flip));
  else clearArrow();
}

// ---- per-move animated GIF overlay (server-rendered; fades back to avatar) ----
let lastGifId = null, gifEl = null, gifTimer = null;
function playMoveGif(p) {
  if (!p || !p.gif_url || !p.gif_id || p.gif_id === lastGifId) return;
  lastGifId = p.gif_id;
  const c = $('video-container');
  if (!c) return;
  c.style.display = 'block';                 // reveal even if the avatar has no video
  if (!gifEl) {
    gifEl = document.createElement('img');
    gifEl.className = 'move-gif';
    gifEl.alt = 'move animation';
    c.appendChild(gifEl);
  }
  gifEl.src = p.gif_url + '?t=' + p.gif_id;   // unique id -> reloads the GIF from frame 0
  gifEl.classList.add('show');
  if (gifTimer) clearTimeout(gifTimer);
  gifTimer = setTimeout(() => gifEl && gifEl.classList.remove('show'), 2600);
}

function renderTray(el, caps) {
  el.innerHTML = '<span class="lbl">Captured</span>';
  (caps || []).forEach(sym => {
    const s = document.createElement('span');
    const isWhite = sym === sym.toUpperCase();
    s.className = 'cap ' + (isWhite ? 'w' : 'b');
    s.textContent = GLYPH[sym.toLowerCase()];
    el.appendChild(s);
  });
}

function renderHistory(hist) {
  if (!hist.length) { historyEl.innerHTML = '<span class="hint">No moves yet.</span>'; return; }
  let html = '';
  for (let i = 0; i < hist.length; i += 2) {
    const n = i / 2 + 1;
    const w = hist[i] || '', b = hist[i + 1] || '';
    html += `<div class="row"><span class="mv">${n}.</span>${escapeHtml(w)} ${escapeHtml(b)}</div>`;
  }
  historyEl.innerHTML = html;
  historyEl.scrollTop = historyEl.scrollHeight;
}

// ---- interaction ----
function onSquareClick(name, piece) {
  if (!board || board.game_over || busy) return;
  const myPieceHere = piece && ((board.player_color === 'white') === (piece === piece.toUpperCase()));

  if (selected && selected !== name) {
    // is this a legal target?
    const base = selected + name;
    const promo = (board.legal_moves || []).filter(u => u.startsWith(base));
    if (promo.length) {
      let uci = base;
      if (promo.some(u => u.length === 5)) {
        // promotion — default queen, offer choice
        const pick = (prompt('Promote to (q, r, b, n)?', 'q') || 'q').toLowerCase();
        uci = base + (['q', 'r', 'b', 'n'].includes(pick) ? pick : 'q');
      }
      selected = null;
      sendMove(uci);
      return;
    }
  }
  // (re)select own piece
  if (myPieceHere) { selected = (selected === name) ? null : name; renderBoard(board); }
  else { selected = null; renderBoard(board); }
}

// ---- SignalWire connection (mirrors blackjack-v4) ----
async function ensureMicrophone() {
  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia)
    throw new Error('No microphone API (needs HTTPS + a mic).');
  let stream;
  try { stream = await navigator.mediaDevices.getUserMedia({ audio: true }); }
  catch (e) {
    const n = e && e.name || '';
    if (n === 'NotAllowedError' || n === 'SecurityError') throw new Error('Microphone blocked — allow it for this URL in the address bar, then reconnect.');
    if (n === 'NotFoundError') throw new Error('No microphone found — connect one, then reconnect.');
    throw new Error('Microphone unavailable (' + (n || e.message) + ').');
  }
  const tracks = stream.getAudioTracks();
  const ok = tracks.length && tracks.some(t => t.readyState === 'live');
  tracks.forEach(t => { try { t.stop(); } catch (_) {} });
  if (!ok) throw new Error('Microphone produced no audio track.');
}

async function fetchGuestToken() {
  const r = await fetch('/get_token');
  let data = await r.json();
  if (Array.isArray(data)) data = data[0] || {};
  if (!r.ok || data.error) throw new Error(data.error || `HTTP ${r.status}`);
  if (!data.token || !data.address) throw new Error('token response missing token/address');
  return data;
}

let intentionalEnd = false;   // user pressed Hang up, or Sigmond ended the call for real
let wasConnected = false;     // did we ever reach 'connected' this session?
let reconnectAttempts = 0;
const MAX_RECONNECT = 3;

async function connectToCall() {
  teardownDone = false; intentionalEnd = false; wasConnected = false; reconnectAttempts = 0;
  try {
    connectBtn.disabled = true; connectBtn.innerHTML = '<span class="material-symbols-outlined">hourglass_top</span> Connecting…';
    connEl.textContent = 'requesting mic';
    await ensureMicrophone();
    // Start a fresh game reflecting the chosen color/difficulty. This is where the game
    // actually begins (engine opens if Black) — never before Connect. Pre-connect the
    // board is only a preview.
    connEl.textContent = 'starting game';
    await newGame();
    await attributeGame();   // tag this game with the player's name before play
    await openCall();
  } catch (e) {
    logEvent('Connection error', { error: e.message });
    connEl.textContent = 'failed: ' + e.message;
    finalTeardown();
  }
}

// Establish (or re-establish) the SignalWire client + call for the CURRENT gameId.
// Shared by the initial connect and by auto-reconnect (never creates a new game).
async function openCall() {
  connEl.textContent = 'getting token';
  const td = await fetchGuestToken();
  currentToken = td.token; currentDestination = td.address; agentAddressId = td.address_id || null;

  const SW = window.SignalWire || SignalWire;
  if (!SW || typeof SW.SignalWire !== 'function') throw new Error('SignalWire v4 SDK not loaded');

  let used = false;
  const credentialProvider = { authenticate: async () => {
    if (!used) { used = true; return { token: currentToken }; }
    const fresh = await fetchGuestToken(); currentToken = fresh.token; currentDestination = fresh.address; return { token: currentToken };
  }};

  connEl.textContent = 'connecting';
  client = new SW.SignalWire(credentialProvider);
  subscriptions.push(client.warnings$.subscribe(w => { if (w?.code !== 'credential_refresh_fallback') logEvent('SDK warning', { code: w?.code }); }));
  subscriptions.push(client.errors$.subscribe(e => logEvent('SDK error', { code: e?.code, message: e?.message })));

  await new Promise((resolve, reject) => {
    let settled = false;
    const timer = setTimeout(() => { if (!settled) { settled = true; reject(new Error('connection timed out')); } }, 15000);
    const sub = client.isConnected$.subscribe(c => { if (c && !settled) { settled = true; clearTimeout(timer); setTimeout(() => sub.unsubscribe(), 0); resolve(); } });
  });

  connEl.textContent = 'dialing';
  call = await client.dial(currentDestination, {
    audio: true, video: false, receiveAudio: true, receiveVideo: true,
    userVariables: { game_id: gameId, player_name: getPlayerName(), mode: mode, interface: 'chess-web' }
  });

  subscriptions.push(call.remoteStream$.subscribe(stream => attachRemoteStream(stream)));
  subscriptions.push(call.subscribe('user_event').subscribe(evt => {
    const payload = (evt && evt.params !== undefined) ? evt.params : evt;
    if (payload && payload.type === 'call_ended') intentionalEnd = true;   // agent ended -> no reconnect
    else if (payload && payload.type === 'board_update') { selected = null; renderBoard(payload); }
    logEvent('user_event', payload && payload.type);
  }));
  subscriptions.push(call.status$.subscribe({ next: st => {
    logEvent('call.status', { status: st });
    if (st === 'connected') {
      wasConnected = true; reconnectAttempts = 0;
      hideReconnectBanner();
      connEl.textContent = 'connected';
      connChip.classList.add('live');
      connectBtn.style.display = 'none';
      hangupBtn.style.display = 'inline-flex';
      muteBtn.style.display = 'inline-grid';
      renderBoard(board); // re-render to enable interactivity now that call exists
    } else if (['disconnected', 'failed', 'destroyed'].includes(st)) handleDisconnect();
  }}));
}

// Quietly drop the current client/call (no UI reset) — used before a reconnect attempt.
function cleanupCall() {
  subscriptions.forEach(s => { try { s.unsubscribe(); } catch (_) {} });
  subscriptions = [];
  try { if (call) call.hangup(); } catch (_) {}
  try { if (client) client.disconnect(); } catch (_) {}
  call = null; client = null;
}

// Auto-reconnect after an UNEXPECTED drop — resumes the SAME game (server-side state).
function showReconnectBanner(txt) { reconnectText.textContent = txt; reconnectBanner.classList.add('show'); }
function hideReconnectBanner() { reconnectBanner.classList.remove('show'); }

async function reconnect() {
  reconnectAttempts++;
  connChip.classList.remove('live');
  connEl.textContent = `reconnecting… (${reconnectAttempts}/${MAX_RECONNECT})`;
  showReconnectBanner(`Connection lost — reconnecting… (${reconnectAttempts}/${MAX_RECONNECT})`);
  logEvent('Connection dropped — reconnecting', { attempt: reconnectAttempts });
  cleanupCall();
  try {
    await openCall();   // same gameId, no new game
  } catch (e) {
    logEvent('Reconnect attempt failed', { error: e.message });
    if (reconnectAttempts >= MAX_RECONNECT) finalTeardown();
    else setTimeout(reconnect, 1200 * reconnectAttempts);
  }
}

// On disconnect: reconnect only if it was UNEXPECTED (not user Hang up, not Sigmond's
// end_call, not a failed initial connect, and within the retry budget); else tear down.
function handleDisconnect() {
  if (teardownDone) return;
  if (!intentionalEnd && wasConnected && reconnectAttempts < MAX_RECONNECT) {
    reconnect();
    return;
  }
  finalTeardown();
}

function finalTeardown() {
  if (teardownDone) return; teardownDone = true;
  hideReconnectBanner();
  cleanupCall();
  if (remoteVideoEl && remoteVideoEl.srcObject) { try { remoteVideoEl.srcObject.getTracks().forEach(t => t.stop()); } catch (_) {} }
  connEl.textContent = 'disconnected';
  connChip.classList.remove('live');
  connectBtn.style.display = 'inline-flex'; connectBtn.disabled = false;
  connectBtn.innerHTML = '<span class="material-symbols-outlined">mic</span> Connect &amp; Play';
  hangupBtn.style.display = 'none'; muteBtn.style.display = 'none';
  if (board) renderBoard(board);
}

let remoteVideoEl = null, lastSig = '';
function attachRemoteStream(stream) {
  if (!stream) return;
  const sig = stream.getTracks().map(t => t.kind + ':' + t.id).sort().join(',');
  if (sig === lastSig && remoteVideoEl && remoteVideoEl.srcObject === stream) return;
  lastSig = sig;
  const c = $('video-container');
  if (!remoteVideoEl) {
    remoteVideoEl = document.createElement('video');
    remoteVideoEl.autoplay = true; remoteVideoEl.playsInline = true;
    c.appendChild(remoteVideoEl); c.style.display = 'block';
  }
  remoteVideoEl.srcObject = stream;
  remoteVideoEl.play().catch(() => {});
}

async function toggleMute() {
  try {
    if (call && call.self && typeof call.self.mute === 'function') {
      if (isMuted) await call.self.unmute(); else await call.self.mute();
      isMuted = !isMuted;
      muteBtn.querySelector('.material-symbols-outlined').textContent = isMuted ? 'mic_off' : 'mic';
      muteBtn.title = isMuted ? 'Unmute' : 'Mute';
    }
  } catch (e) { logEvent('mute failed', { error: e.message }); }
}

// ---- wire up controls ----
connectBtn.addEventListener('click', connectToCall);
hangupBtn.addEventListener('click', () => { intentionalEnd = true; finalTeardown(); });
muteBtn.addEventListener('click', toggleMute);
newBtn.addEventListener('click', () => newGame().catch(e => logEvent('new game error', { error: e.message })));
resignBtn.addEventListener('click', async () => {
  if (!gameId || !board || board.game_over) return;
  if (!confirm('Resign this game? It counts as a loss on the leaderboard.')) return;
  try {
    const j = await api(`/chess/resign?game_id=${encodeURIComponent(gameId)}`, { method: 'POST' });
    renderBoard(j.board);
    logEvent('Resigned', { result: j.board.result });
  } catch (e) { logEvent('resign error', { error: e.message }); }
});
// Color/difficulty are pre-game settings. Changing one before connecting starts a
// fresh game with those settings (so picking Black actually gives you Black — and
// Color/difficulty are pre-game settings applied when you press Connect & Play.
// Changing color before connecting only PREVIEWS the orientation (flips the board) —
// it must NOT start a game or let the engine move until you actually connect.
difficultySel.addEventListener('change', () => { levelEl.textContent = difficultySel.value; });
colorSel.addEventListener('change', () => {
  if (!call && board) { board.player_color = colorSel.value; selected = null; renderBoard(board); }
});

// ---- mode (Game | Learn) — a pre-game setting applied at Connect ----
function applyMode() {
  [...modeToggle.querySelectorAll('.seg')].forEach(b => b.classList.toggle('active', b.dataset.mode === mode));
  modeHint.textContent = (mode === 'learn')
    ? 'Sigmond coaches you: grades each of your moves and suggests better ones. Ask for a hint anytime.'
    : 'Play a normal game against Sigmond.';
}
modeToggle.addEventListener('click', e => {
  const btn = e.target.closest('.seg'); if (!btn) return;
  mode = btn.dataset.mode === 'learn' ? 'learn' : 'game';
  localStorage.setItem('chessMode', mode);
  applyMode();
  if (board) updateAnalysis(board); else if (mode !== 'learn') hideAnalysis();
});
applyMode();

// ---- name capture ----
nameChip.addEventListener('click', openNameDialog);
nameSave.addEventListener('click', saveNameFromDialog);
nameSkip.addEventListener('click', closeNameDialog);
nameInput.addEventListener('keydown', e => { if (e.key === 'Enter') saveNameFromDialog(); });
refreshNameChip();
if (!getPlayerName()) openNameDialog();

// initial empty board so the page isn't blank before connecting
newGame().catch(e => logEvent('init error', { error: e.message }));
