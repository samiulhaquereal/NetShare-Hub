/* RetCore OnLAN — clipboard page. Globals from common.js: showToast; pin-lock.js: withPinToken.
 *
 * Live sync over one WebSocket (/ws/clipboard): every edit is a single message and
 * reaches the other devices straight away.
 *
 * Several people can type at once: each edit says which server version it was based
 * on, and edits that raced each other are merged (server and here, same merge3) as
 * long as they touch different parts of the text. One save is in flight at a time;
 * whatever is typed meanwhile is merged on top of the server's answer, so no
 * keystroke is lost. Edits made while offline are kept and merged on reconnect. */

const MAX_BYTES = 1_000_000;  // same limit as the server
const SEND_DELAY = 60;        // ms: batch keystrokes, still feels instant
const PING_EVERY = 25000;     // keeps Wi-Fi/NAT from dropping an idle connection
const DEAD_AFTER = 60000;     // nothing heard for this long → reconnect

const clip = {
  ws: null, open: false, retry: 1000, retryTimer: null, lastHeard: 0, connectStarted: 0,
  seq: 0,
  confirmed: '',        // server text at `version` as far as this page knows
  version: 0,
  inflight: null,       // {seq, text, base} — the one save waiting for its ack
  pendingRemote: null,  // newest other-device text that arrived while saving
  sendTimer: null,
  tooLarge: false,
};

const CLIP_ID = document.getElementById('clip-app')?.dataset.clipId || 'shared';
const box = () => document.getElementById('clipboard-box');
const utf8Bytes = s => new TextEncoder().encode(s).length;
const hasLocalEdits = () => box().value !== (clip.inflight ? clip.inflight.text : clip.confirmed);

// ── Merging (same algorithm as merge3 in netshare/services/clipboard.py) ──
function splice(a, b) {
  const n = Math.min(a.length, b.length);
  let p = 0;
  while (p < n && a[p] === b[p]) p++;
  let s = 0;
  while (s < n - p && a[a.length - 1 - s] === b[b.length - 1 - s]) s++;
  return { p, e: a.length - s, ins: b.slice(p, b.length - s) };
}

function merge3(base, mine, theirs) {
  if (mine === base || mine === theirs) return theirs;
  if (theirs === base) return mine;
  const m = splice(base, mine), t = splice(base, theirs);
  if (m.e <= t.p) return theirs.slice(0, m.p) + m.ins + theirs.slice(m.e);
  if (m.p >= t.e) {
    const d = theirs.length - base.length;
    return theirs.slice(0, m.p + d) + m.ins + theirs.slice(m.e + d);
  }
  return null;  // both changed the same spot
}

/** The server moved to `text`; keep anything typed here that it hasn't seen yet. */
function adoptServer(text, version, localBase, flash) {
  clip.confirmed = text;
  clip.version = version;
  const local = box().value;
  if (local === localBase) { applyRemote(text, flash); return; }
  const merged = merge3(localBase, local, text);
  applyRemote(merged ?? local, flash);  // overlap: keep what this person is typing; it is sent next
  if (box().value !== clip.confirmed) scheduleSend();
}
// ── Connection ────────────────────────────────────────────────────
function connect() {
  clearTimeout(clip.retryTimer);
  if (clip.ws && clip.ws.readyState <= 1) return;
  setStatus('connecting');
  const url = `${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/ws/clipboard?id=${encodeURIComponent(CLIP_ID)}`;
  const ws = new WebSocket(typeof withPinToken === 'function' ? withPinToken(url) : url);
  clip.ws = ws;
  clip.connectStarted = Date.now();
  // An attempt made while the network is down can hang for a long time: give up and retry.
  setTimeout(() => { if (ws.readyState === WebSocket.CONNECTING) ws.close(); }, 5000);
  ws.onopen = () => { clip.open = true; clip.retry = 1000; clip.lastHeard = Date.now(); };
  ws.onmessage = e => { clip.lastHeard = Date.now(); onMessage(JSON.parse(e.data)); };
  ws.onclose = () => {
    if (clip.ws !== ws || clip.deleted) return;
    clip.open = false;
    setStatus('offline');
    clip.retryTimer = setTimeout(connect, clip.retry);
    clip.retry = Math.min(10000, clip.retry * 2);
  };
}

function reconnectNow() {
  if (clip.open) return;
  clip.retry = 1000;
  if (clip.ws && clip.ws.readyState === WebSocket.CONNECTING) {
    if (Date.now() - clip.connectStarted < 1500) return;  // a fresh attempt: let it finish
    const stale = clip.ws;
    clip.ws = null;  // its onclose is ignored; start over right away
    stale.close();
  }
  connect();
}

function onMessage(msg) {
  if (msg.type === 'state') {  // first message after (re)connecting
    setStatus('live');
    clip.inflight = null; clip.pendingRemote = null;  // anything unconfirmed is re-sent as a merge
    adoptServer(msg.text, msg.version, clip.confirmed, false);
  } else if (msg.type === 'text') {  // another device changed it
    if (msg.version <= clip.version) return;
    if (clip.inflight) {  // decide once our own save is answered
      if (!clip.pendingRemote || msg.version > clip.pendingRemote.version) clip.pendingRemote = msg;
      return;
    }
    adoptServer(msg.text, msg.version, clip.confirmed, true);
  } else if (msg.type === 'ack') {
    const sent = clip.inflight;
    if (!sent || sent.seq !== msg.seq) return;
    clip.inflight = null;
    let text = msg.text ?? sent.text, version = msg.version;
    const r = clip.pendingRemote;
    clip.pendingRemote = null;
    if (r && r.version > version) { text = r.text; version = r.version; }  // newer, already includes ours
    if (version >= clip.version) adoptServer(text, version, sent.text, text !== sent.text);
    setStatus(hasLocalEdits() ? 'saving' : 'live');
    if (hasLocalEdits() && !clip.sendTimer) sendNow();
  } else if (msg.type === 'deleted') {  // someone deleted this clipboard
    clip.deleted = true;
    clip.ws.close();
    showToast('🗑 This clipboard was deleted', 'error', 3000);
    setTimeout(() => { location.href = '/clipboard-page'; }, 1500);
  } else if (msg.type === 'error') {
    if (clip.inflight && clip.inflight.seq === msg.seq) clip.inflight = null;
    showToast('⚠️ ' + msg.error, 'error', 5000);
  }
}

// ── Sending ───────────────────────────────────────────────────────
function scheduleSend() {
  clearTimeout(clip.sendTimer);
  clip.sendTimer = setTimeout(() => { clip.sendTimer = null; sendNow(); }, SEND_DELAY);
}

function sendNow() {
  clearTimeout(clip.sendTimer); clip.sendTimer = null;
  const text = box().value;
  if (utf8Bytes(text) > MAX_BYTES) {
    clip.tooLarge = true;
    setStatus('too-large');
    return;
  }
  clip.tooLarge = false;
  if (!clip.open) { setStatus('offline'); return; }  // kept in the box; merged and sent on reconnect
  if (clip.inflight) { setStatus('saving'); return; }  // sent when the current save is answered
  if (text === clip.confirmed) { setStatus('live'); return; }
  const seq = ++clip.seq;
  clip.inflight = { seq, text, base: clip.version };
  clip.ws.send(JSON.stringify({ type: 'set', text, seq, base: clip.version }));
  setStatus('saving');
}

// ── Showing another device's text without disturbing the cursor ───
function applyRemote(text, flash = false) {
  const el = box();
  if (el.value === text) return;
  const old = el.value;
  if (document.activeElement === el) {
    const { selectionStart: s, selectionEnd: e, scrollTop } = el;
    let p = 0;
    while (p < old.length && p < text.length && old[p] === text[p]) p++;
    let q = 0;
    while (q < old.length - p && q < text.length - p && old[old.length - 1 - q] === text[text.length - 1 - q]) q++;
    const delta = text.length - old.length;
    const map = i => i <= p ? i : i >= old.length - q ? i + delta : Math.min(p + (text.length - p - q), text.length);
    el.value = text;
    el.setSelectionRange(map(s), map(e));
    el.scrollTop = scrollTop;
  } else {
    el.value = text;
  }
  if (flash) {
    el.classList.remove('remote-flash');
    void el.offsetWidth;  // restart the animation
    el.classList.add('remote-flash');
  }
}

// ── Status pill ───────────────────────────────────────────────────
function setStatus(state) {
  if (clip.tooLarge) state = 'too-large';
  const el = document.getElementById('sync-indicator');
  const text = {
    connecting: 'Connecting…',
    live: 'Live — changes appear instantly on every device',
    saving: 'Syncing…',
    offline: hasLocalEdits() ? 'Offline — your edits will sync when the connection is back' : 'Offline — reconnecting…',
    'too-large': 'Too long to sync (limit 1 MB) — shorten the text',
  }[state];
  el.dataset.state = state;
  document.getElementById('sync-text').textContent = text;
}

// ── Buttons ───────────────────────────────────────────────────────
async function copyClipboard() {
  const text = box().value;
  if (!text) { showToast('Nothing to copy', 'info'); return; }
  try {
    await copyText(text);
    showToast('✅ Copied!');
  } catch (e) {
    // Plain http:// on phones often blocks scripted copy; the text is left selected.
    showToast('Text selected — use your device\'s Copy to finish', 'info', 4000);
  }
}

async function copyText(text) {
  if (navigator.clipboard && window.isSecureContext) return navigator.clipboard.writeText(text);
  const el = box();
  el.focus();
  el.select();
  el.setSelectionRange(0, text.length);  // iOS ignores select()
  let ok = false;
  try { ok = document.execCommand('copy'); } catch (e) { ok = false; }
  if (!ok) throw new Error('copy blocked');  // execCommand reports failure by returning false
}

function clearClipboard() {
  if (!box().value) return;
  box().value = '';
  sendNow();  // immediately, for every device
  showToast('🗑 Cleared on all devices');
}

// ── Boot ──────────────────────────────────────────────────────────
(function init() {
  checkPinStatus();
  box().addEventListener('input', scheduleSend);
  box().addEventListener('blur', () => { if (clip.sendTimer) sendNow(); });
  connect();
  setInterval(() => {
    if (!clip.open) return;
    if (Date.now() - clip.lastHeard > DEAD_AFTER) { clip.ws.close(); return; }  // silently dead (phone slept)
    clip.ws.send('{"type":"ping"}');
  }, PING_EVERY);
  window.addEventListener('online', reconnectNow);
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) return;
    if (clip.open && Date.now() - clip.lastHeard > PING_EVERY + 5000) clip.ws.close();  // stale after sleep
    else reconnectNow();
  });
})();
