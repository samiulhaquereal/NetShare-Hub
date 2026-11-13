/* NetShare Hub — screen page. Globals from common.js: showToast, escapeHtml; config: window.NSH */
const STREAM_WS_URL = window.NSH.wsUrl;
const HTTP_URL = window.NSH.httpUrl;

const canvas = document.getElementById('canvas');
const ctx = canvas.getContext('2d', { alpha: false });
const loadingEl = document.getElementById('loading');
const dcEl = document.getElementById('disconnected');
const fsBtnEl = document.getElementById('fs-btn');
const hudFpsEl = document.getElementById('hud-fps');
const hudPingEl = document.getElementById('hud-ping');
const hudEl = document.getElementById('hud');
const streamBadge = document.getElementById('stream-status-badge');

let ws = null, latestBitmap = null, rafId = 0, connected = false;
let frameCount = 0, hudTimer = null;
let audioCtx = null, gainNode = null, scheduleUntil = 0;
let audioMuted = false, audioReady = false, audioUnlocked = false;
let pendingChunks = [], audioCfg = { sample_rate: 44100, channels: 2 };
const AUDIO_LATENCY = 0.12, MAX_PENDING = 25;

function initAudio(sr, ch) { audioCfg = { sample_rate: sr, channels: ch }; audioReady = true; updateMuteBtn(); showAudioPrompt(); }
function unlockAudio() {
  if (audioUnlocked && audioCtx) { if (audioCtx.state === 'suspended') audioCtx.resume(); return; }
  try { audioCtx = new AudioContext({ sampleRate: audioCfg.sample_rate }); } catch (e) { audioCtx = new AudioContext(); }
  gainNode = audioCtx.createGain(); gainNode.gain.value = audioMuted ? 0 : 1; gainNode.connect(audioCtx.destination);
  scheduleUntil = 0; audioUnlocked = true;
  audioCtx.resume().then(() => {
    if (pendingChunks.length) { scheduleUntil = audioCtx.currentTime + 0.02; for (const ab of pendingChunks) _doSchedule(ab); pendingChunks = []; }
  });
  hideAudioPrompt(); updateMuteBtn();
}
function showAudioPrompt() { if (!audioReady || audioUnlocked) return; const el = document.getElementById('audio-prompt'); if (el) el.style.display = 'block'; }
function hideAudioPrompt() { const el = document.getElementById('audio-prompt'); if (el) el.style.display = 'none'; }

function scheduleAudioChunk(ab) {
  if (!audioReady || audioMuted) return;
  if (!audioUnlocked || !audioCtx) { if (pendingChunks.length >= MAX_PENDING) pendingChunks.shift(); pendingChunks.push(ab); return; }
  if (audioCtx.state === 'suspended') { audioCtx.resume(); return; }
  _doSchedule(ab);
}
function _doSchedule(ab) {
  const ch = audioCfg.channels, int16 = new Int16Array(ab), samples = int16.length / ch;
  if (samples < 1) return;
  const buf = audioCtx.createBuffer(ch, samples, audioCfg.sample_rate);
  for (let c = 0; c < ch; c++) { const d = buf.getChannelData(c); for (let i = 0; i < samples; i++) d[i] = int16[i * ch + c] / 32768; }
  const src = audioCtx.createBufferSource(); src.buffer = buf; src.connect(gainNode);
  const now = audioCtx.currentTime; if (scheduleUntil < now + AUDIO_LATENCY) scheduleUntil = now + AUDIO_LATENCY;
  src.start(scheduleUntil); scheduleUntil += buf.duration;
}
function toggleMute() {
  if (!audioUnlocked) { unlockAudio(); return; }
  audioMuted = !audioMuted;
  if (gainNode) gainNode.gain.setTargetAtTime(audioMuted ? 0 : 1, audioCtx.currentTime, 0.05);
  fetch('/audio', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ enabled: !audioMuted }) }).catch(() => {});
  updateMuteBtn();
}
function updateMuteBtn() {
  const btn = document.getElementById('mute-btn'); if (!btn) return;
  if (!audioReady) { btn.textContent = '🔇 No Audio'; btn.classList.add('muted'); }
  else if (!audioUnlocked) { btn.textContent = '🔊 Tap for Audio'; btn.classList.remove('muted'); }
  else if (audioMuted) { btn.textContent = '🔇 Muted'; btn.classList.add('muted'); }
  else { btn.textContent = '🔊 Audio'; btn.classList.remove('muted'); }
}

// ── Video pipeline ──────────────────────────────────────────────────
// • Only the newest frame is decoded; frames that arrive while a decode is running
//   replace each other, so slow devices skip frames instead of building a backlog.
// • The canvas is redrawn only when a new frame was decoded (not 60×/s).
// • Motion frames may be downscaled; when the screen goes still the server sends one
//   full-resolution frame. The canvas keeps the largest size seen for the current
//   aspect ratio, so that sharp frame is shown pixel-for-pixel.
let pendingFrame = null, decoding = false, needsDraw = false;
let lastFrameAt = 0, lastIdleAt = 0, paused = false;

function queueFrame(payload) {
  pendingFrame = payload;
  if (!decoding) decodeNext();
}

async function decodeNext() {
  decoding = true;
  while (pendingFrame) {
    const data = pendingFrame; pendingFrame = null;
    const t0 = performance.now();
    try {
      const bm = await createImageBitmap(new Blob([data], { type: 'image/jpeg' }));
      const old = latestBitmap; latestBitmap = bm; if (old) old.close();
      needsDraw = true; frameCount++; lastFrameAt = performance.now();
      if (hudPingEl) hudPingEl.textContent = Math.round(lastFrameAt - t0) + ' ms';
    } catch (_) {}
  }
  decoding = false;
}

function fitCanvasTo(bm) {
  const sameAspect = canvas.width && Math.abs(canvas.width / canvas.height - bm.width / bm.height) < 0.01;
  if (sameAspect && bm.width <= canvas.width) return;
  canvas.width = bm.width; canvas.height = bm.height;
  ctx.imageSmoothingQuality = 'high';
  resizeCanvas();
}

function renderLoop() {
  if (needsDraw && latestBitmap) {
    needsDraw = false;
    fitCanvasTo(latestBitmap);
    ctx.drawImage(latestBitmap, 0, 0, canvas.width, canvas.height);
  }
  rafId = requestAnimationFrame(renderLoop);
}

function setPaused(value) {
  paused = !!value;
  const el = document.getElementById('paused-banner');
  if (el) el.classList.toggle('show', paused);
  streamBadge.textContent = paused ? '⏸ PAUSED' : '● LIVE';
}

function updateHudFps() {
  if (!hudFpsEl) return;
  const now = performance.now();
  if (paused) hudFpsEl.textContent = 'Paused';
  else if (frameCount === 0 && now - lastIdleAt < 2500 && now - lastFrameAt > 1000) hudFpsEl.textContent = 'Still · HD';
  else hudFpsEl.textContent = frameCount + ' FPS';
  frameCount = 0;
}
setInterval(updateHudFps, 1000);
function resizeCanvas() {
  if (!canvas.width || !canvas.height) return;
  const body = document.getElementById('screen-body');
  const scale = Math.min(body.clientWidth / canvas.width, body.clientHeight / canvas.height);
  canvas.style.width = (canvas.width * scale) + 'px'; canvas.style.height = (canvas.height * scale) + 'px';
}
window.addEventListener('resize', resizeCanvas);

function connect() {
  if (ws) { try { ws.close(); } catch (_) {} }
  let wsUrl = STREAM_WS_URL;
  const pinToken = localStorage.getItem("pin_token");
  if (pinToken) wsUrl += (wsUrl.includes('?') ? '&' : '?') + 'pin_token=' + encodeURIComponent(pinToken);
  ws = new WebSocket(wsUrl);
  ws.binaryType = 'arraybuffer';
  ws.onopen = () => {
    if (!connected) {
      connected = true; loadingEl.classList.add('fade'); setTimeout(() => loadingEl.style.display = 'none', 600);
      dcEl.classList.remove('show'); streamBadge.textContent = '● LIVE';
      needsDraw = true; rafId = requestAnimationFrame(renderLoop); showHud();
    }
  };
  ws.onmessage = (ev) => {
    const ab = ev.data, type = new Uint8Array(ab, 0, 1)[0];
    if (type === 0x01) { queueFrame(ab.slice(1)); return; }
    if (type === 0x02) { scheduleAudioChunk(ab.slice(1)); return; }
    if (type === 0x05) { lastIdleAt = performance.now(); return; }  // screen unchanged heartbeat
    if (type === 0x00) {
      try {
        const cfg = JSON.parse(new TextDecoder().decode(ab.slice(1)));
        if ('audio_ok' in cfg) cfg.audio_ok ? initAudio(cfg.sample_rate, cfg.channels) : (audioReady = false, updateMuteBtn());
        if ('paused' in cfg) setPaused(cfg.paused);
      } catch (_) {}
    }
  };
  ws.onclose = () => {
    if (connected) { connected = false; dcEl.classList.add('show'); cancelAnimationFrame(rafId); streamBadge.textContent = '⚠ Disconnected'; }
  };
  ws.onerror = () => ws.close();
}

function reconnect() {
  dcEl.classList.remove('show'); loadingEl.style.cssText = 'display:flex;opacity:1;'; loadingEl.classList.remove('fade');
  connected = false; setTimeout(connect, 500);
}
function showHud() { hudEl.classList.add('show'); fsBtnEl.style.opacity = '1'; clearTimeout(hudTimer); hudTimer = setTimeout(hideHud, 3500); }
function hideHud() { hudEl.classList.remove('show'); fsBtnEl.style.opacity = '0'; }
document.getElementById('screen-body').addEventListener('mousemove', showHud);
document.getElementById('screen-body').addEventListener('touchstart', showHud, { passive: true });

const sourcePanel = document.getElementById('source-panel');
const panelBackdrop = document.getElementById('panel-backdrop');
const panelBody = document.getElementById('panel-body');
function openPanel() { sourcePanel.classList.add('show'); panelBackdrop.classList.add('show'); fetchSources(); clearTimeout(hudTimer); }
function closePanel() { sourcePanel.classList.remove('show'); panelBackdrop.classList.remove('show'); }
async function fetchSources() {
  try { const data = await (await fetch('/sources')).json(); renderPanel(data); } catch (e) { panelBody.innerHTML = '<div style="color:var(--red);padding:24px;text-align:center">Failed to load sources</div>'; }
}
function renderPanel(data) {
  const cur = data.current;
  let html = '<div class="section-label">Monitors</div>';
  for (const m of data.monitors) {
    const idx = Number(m.index);
    const a = cur.type === 'monitor' && cur.monitor_index === idx;
    html += `<div class="source-item${a ? ' active' : ''}" onclick="selectMonitor(${idx})" id="src-mon-${idx}">
  <img class="source-thumb" src="/thumbnail/monitor/${idx}" loading="lazy" onerror="this.style.display='none'"/>
  <div class="source-info"><div class="source-name">${escapeHtml(m.name)}</div><div class="source-meta">${Number(m.width)}×${Number(m.height)}px</div></div></div>`;
  }
  html += '<div class="section-label">Application Windows</div>';
  if (!data.windows.length) html += '<div style="color:var(--muted);font-size:12px;padding:8px 2px">No windows found</div>';
  for (const w of data.windows) {
    // Window titles come from arbitrary apps (e.g. a web page title): escape, never inline into JS.
    const hwnd = Number(w.hwnd);
    const a = cur.type === 'window' && cur.window_hwnd === hwnd;
    const t = w.title.length > 44 ? w.title.slice(0, 44) + '…' : w.title;
    html += `<div class="source-item${a ? ' active' : ''}" data-title="${escapeHtml(w.title)}" onclick="selectWindow(${hwnd}, this.dataset.title)" id="src-win-${hwnd}">
  <img class="source-thumb" src="/thumbnail/window/${hwnd}" loading="lazy" onerror="this.style.display='none'"/>
  <div class="source-info"><div class="source-name">${escapeHtml(t)}</div><div class="source-meta">${Number(w.width)}×${Number(w.height)}px</div></div></div>`;
  }
  panelBody.innerHTML = html;
}
async function selectMonitor(idx) {
  await fetch('/source', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ type: 'monitor', index: idx }) });
  showToast('📺 Monitor ' + idx + ' selected', 'info');
}
async function selectWindow(hwnd, title) {
  await fetch('/source', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ type: 'window', hwnd, title }) });
  showToast('🪟 ' + title.slice(0, 22), 'info');
}
async function toggleFS() {
  if (!document.fullscreenElement) await document.getElementById('screen-body').requestFullscreen().catch(() => {});
  else document.exitFullscreen();
}

let rcEnabled = false, lastMMTime = 0;
function toggleRC(active) {
  rcEnabled = active;
  if (active) { showToast('🖱 Remote Control Active', 'info'); bindRCEvents(); }
  else { showToast('🖱 Remote Control Disabled', 'info'); unbindRCEvents(); }
}
function sendControl(data) {
  if (!ws || ws.readyState !== WebSocket.OPEN) return;
  const msg = new TextEncoder().encode(JSON.stringify(data));
  const packet = new Uint8Array(1 + msg.length); packet[0] = 0x04; packet.set(msg, 1); ws.send(packet);
}
function getCanvasCoords(e) {
  const rect = canvas.getBoundingClientRect();
  return { x: Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width)), y: Math.max(0, Math.min(1, (e.clientY - rect.top) / rect.height)) };
}
const rcHandlers = {
  mm: (e) => { const now = performance.now(); if (now - lastMMTime < 30) return; lastMMTime = now; sendControl({ type: 'mm', ...getCanvasCoords(e) }); },
  md: (e) => { const btn = e.button === 0 ? 'left' : (e.button === 2 ? 'right' : 'middle'); sendControl({ type: 'md', ...getCanvasCoords(e), btn }); },
  mu: (e) => { const btn = e.button === 0 ? 'left' : (e.button === 2 ? 'right' : 'middle'); sendControl({ type: 'mu', ...getCanvasCoords(e), btn }); },
  clk: (e) => e.preventDefault(),
  whl: (e) => { e.preventDefault(); sendControl({ type: 'sc', v: e.deltaY > 0 ? -100 : 100 }); },
  kd: (e) => { if (['Alt', 'Control', 'Shift', 'Meta'].includes(e.key)) return; sendControl({ type: 'kd', key: e.key }); if (e.key.length > 1) e.preventDefault(); },
  ku: (e) => sendControl({ type: 'ku', key: e.key })
};
function bindRCEvents() {
  canvas.addEventListener('mousemove', rcHandlers.mm); canvas.addEventListener('mousedown', rcHandlers.md); canvas.addEventListener('mouseup', rcHandlers.mu);
  canvas.addEventListener('contextmenu', rcHandlers.clk); canvas.addEventListener('wheel', rcHandlers.whl, { passive: false });
  window.addEventListener('keydown', rcHandlers.kd); window.addEventListener('keyup', rcHandlers.ku);
}
function unbindRCEvents() {
  canvas.removeEventListener('mousemove', rcHandlers.mm); canvas.removeEventListener('mousedown', rcHandlers.md); canvas.removeEventListener('mouseup', rcHandlers.mu);
  canvas.removeEventListener('contextmenu', rcHandlers.clk); canvas.removeEventListener('wheel', rcHandlers.whl);
  window.removeEventListener('keydown', rcHandlers.kd); window.removeEventListener('keyup', rcHandlers.ku);
}

(function init() { checkPinStatus(); connect(); })();
