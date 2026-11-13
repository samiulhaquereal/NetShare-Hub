/* NetShare Hub — Messenger.
 * Globals: showToast, escapeHtml (common.js), checkPinStatus, withPinToken (pin-lock.js), window.NSH.
 *
 * Layout: chat list + conversation (two panes on desktop, two screens on phones).
 * Server events over one WebSocket (/ws/chat): new_msg, update_msg, presence, typing,
 * read_receipt, system, error, pong. Everything user-supplied is rendered with
 * textContent / DOM nodes — never spliced into HTML. */
'use strict';

const $ = id => document.getElementById(id);
const WS_URL = window.NSH.wsUrl.replace('/ws/stream', '/ws/chat');
const PAGE = 40;
const GROUP_GAP_MS = 5 * 60 * 1000;
const QUICK_REACTIONS = ['👍', '❤️', '😂', '😮', '😢', '🙏'];
const isPhone = () => window.matchMedia('(max-width: 820px)').matches;
const coarse = () => window.matchMedia('(pointer: coarse)').matches;

const ICONS = {
  globe: '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3a14 14 0 0 1 0 18M12 3a14 14 0 0 0 0 18"/></svg>',
  group: '<svg viewBox="0 0 24 24"><path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.9M16 3.1a4 4 0 0 1 0 7.8"/></svg>',
  pin: '<svg viewBox="0 0 24 24"><path d="M12 17v5M9 3h6l-1 6 3 3v2H7v-2l3-3z"/></svg>',
  more: '<svg viewBox="0 0 24 24"><circle cx="12" cy="5" r="1.4"/><circle cx="12" cy="12" r="1.4"/><circle cx="12" cy="19" r="1.4"/></svg>',
  smile: '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M8 14s1.5 2 4 2 4-2 4-2M9 9h.01M15 9h.01"/></svg>',
  copy: '<svg viewBox="0 0 24 24"><rect x="9" y="9" width="12" height="12" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>',
  edit: '<svg viewBox="0 0 24 24"><path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z"/></svg>',
  trash: '<svg viewBox="0 0 24 24"><path d="M3 6h18M8 6V4h8v2M6 6l1 14h10l1-14"/></svg>',
  download: '<svg viewBox="0 0 24 24"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M7 10l5 5 5-5M12 15V3"/></svg>',
  file: '<svg viewBox="0 0 24 24"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/></svg>',
  ban: '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M5.7 5.7l12.6 12.6"/></svg>',
  tick: '<svg viewBox="0 0 24 24"><path d="M5 12.5l4.5 4.5L19 7.5"/></svg>',
  ticks: '<svg viewBox="0 0 24 24"><path d="M2 12.5l4.5 4.5L16 7.5M10.5 16.5l1 1L21 7.5"/></svg>',
  members: '<svg viewBox="0 0 24 24"><path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.9"/></svg>',
  settings: '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z"/></svg>',
  markRead: '<svg viewBox="0 0 24 24"><path d="M2 12.5l4.5 4.5L16 7.5M10.5 16.5l1 1L21 7.5"/></svg>',
};

// ── State ─────────────────────────────────────────────────────────
const S = {
  me: { id: '', name: '', avatar: '' },
  users: new Map(),      // id → {id, name, avatar, online}
  rows: new Map(),       // conversation id → {id, title, type, pinned, unread, members, createdBy, avatar, preview}
  current: null,         // open conversation
  msgs: [],              // its loaded messages, oldest → newest
  hasOlder: false,
  loadingOlder: false,
  unreadFrom: null,      // id of the first unread message (divider)
  newBelow: 0,           // new messages while scrolled up
  lastReadSent: {},      // conversation → last id reported as read
  typing: {},            // conversation → {userId: timeout}
  editing: null,         // message being edited
  drafts: {},            // conversation → unsent text
  menuMsg: null,
  started: false,
};

// ── Formatting helpers ────────────────────────────────────────────
const parseTs = ts => ts ? new Date(String(ts).replace(' ', 'T') + (String(ts).endsWith('Z') ? '' : 'Z')) : null;
const sameDay = (a, b) => a && b && a.toDateString() === b.toDateString();
const fmtTime = d => d ? d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : '';

function fmtDay(d) {
  const now = new Date(), y = new Date(now); y.setDate(now.getDate() - 1);
  if (sameDay(d, now)) return 'Today';
  if (sameDay(d, y)) return 'Yesterday';
  if (now - d < 6 * 864e5) return d.toLocaleDateString([], { weekday: 'long' });
  return d.toLocaleDateString([], { day: 'numeric', month: 'long', year: d.getFullYear() === now.getFullYear() ? undefined : 'numeric' });
}

function fmtListTime(d) {
  if (!d) return '';
  const now = new Date(), y = new Date(now); y.setDate(now.getDate() - 1);
  if (sameDay(d, now)) return fmtTime(d);
  if (sameDay(d, y)) return 'Yesterday';
  if (now - d < 6 * 864e5) return d.toLocaleDateString([], { weekday: 'short' });
  return d.toLocaleDateString([], { day: 'numeric', month: 'short' });
}

function hue(id) {
  let h = 0;
  for (const ch of String(id)) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
  return h % 360;
}
const nameColor = id => `hsl(${hue(id)} 70% 72%)`;
const isRoom = id => id === 'group' || String(id).startsWith('cg_');

function initials(name) {
  const parts = String(name || '?').trim().split(/\s+/).filter(Boolean);
  return ((parts[0] || '?')[0] + (parts.length > 1 ? parts[parts.length - 1][0] : '')).toUpperCase();
}

/** Avatar element: photo, initials on a stable colour, or an icon for rooms. */
function avatar({ id, name, url, kind = 'user', online = false, size = '' }) {
  const el = document.createElement('span');
  el.className = 'avatar' + (size ? ' ' + size : '');
  if (kind === 'global') { el.style.setProperty('--avatar-bg', 'linear-gradient(135deg,#7c3aed,#2563eb)'); el.innerHTML = ICONS.globe; }
  else if (kind === 'cgroup' && !url) { el.style.setProperty('--avatar-bg', `hsl(${hue(id)} 45% 38%)`); el.innerHTML = ICONS.group; }
  else if (url) {
    const img = document.createElement('img');
    img.src = url; img.alt = ''; img.loading = 'lazy';
    img.onerror = () => { img.remove(); el.prepend(initials(name)); el.style.setProperty('--avatar-bg', `hsl(${hue(id)} 45% 42%)`); };
    el.appendChild(img);
  } else {
    el.textContent = initials(name);
    el.style.setProperty('--avatar-bg', `hsl(${hue(id)} 45% 42%)`);
  }
  if (online) { const dot = document.createElement('span'); dot.className = 'online'; el.appendChild(dot); }
  return el;
}

function conversationOf(m) {
  const to = m.to_target || 'group';
  if (isRoom(to)) return to;
  return m.from_mac === S.me.id ? to : m.from_mac;
}

function previewText(m) {
  if (m.is_deleted) return '🚫 Message deleted';
  if (m.msg_type === 'file') {
    const t = m.filetype || '';
    if (t.startsWith('image/')) return '📷 Photo';
    if (t.startsWith('audio/')) return '🎤 Voice message';
    if (t.startsWith('video/')) return '🎬 Video';
    return '📎 ' + (m.filename || 'File');
  }
  return m.content || '';
}

const URL_RE = /\bhttps?:\/\/[^\s<>"']+[^\s<>"'.,;:!?)\]]/gi;
/** Text with clickable http(s) links, built from DOM nodes (never innerHTML). */
function linkify(text) {
  const frag = document.createDocumentFragment();
  let last = 0;
  for (const match of text.matchAll(URL_RE)) {
    if (match.index > last) frag.append(text.slice(last, match.index));
    const a = document.createElement('a');
    a.href = match[0]; a.textContent = match[0]; a.target = '_blank'; a.rel = 'noopener noreferrer';
    frag.append(a);
    last = match.index + match[0].length;
  }
  if (last < text.length) frag.append(text.slice(last));
  return frag;
}

const EMOJI_ONLY = /^(?:\p{Extended_Pictographic}|\p{Emoji_Modifier}|‍|️|\s)+$/u;
const isJumbo = t => t && t.length <= 24 && EMOJI_ONLY.test(t) && ([...t.matchAll(/\p{Extended_Pictographic}/gu)].length <= 3);

function copyToClipboard(text) {
  if (navigator.clipboard && window.isSecureContext) return navigator.clipboard.writeText(text);
  return new Promise((res, rej) => {
    const ta = Object.assign(document.createElement('textarea'), { value: text });
    ta.style.cssText = 'position:fixed;top:0;left:0;opacity:0';
    document.body.appendChild(ta); ta.focus(); ta.select();
    const ok = document.execCommand('copy'); ta.remove();
    ok ? res() : rej(new Error('copy blocked'));
  });
}

// ── Boot ──────────────────────────────────────────────────────────
async function boot() {
  bindUI();
  if (!(await checkPinStatus())) return;
  if ('serviceWorker' in navigator) navigator.serviceWorker.register('/sw.js').catch(() => {});
  let profile = null;
  try { profile = await (await fetch('/api/user')).json(); } catch (e) { /* offline */ }
  if (profile && profile.username) { setMe(profile); start(); }
  else openProfile(true);
}

function setMe(p) {
  S.me = { id: p.mac, name: p.username || '', avatar: p.avatar || '' };
  const el = $('me-avatar');
  el.replaceWith(Object.assign(avatar({ id: S.me.id, name: S.me.name, url: S.me.avatar, size: 'sm' }), { id: 'me-avatar' }));
  $('me-btn').title = `${S.me.name} — edit profile`;
}

async function start() {
  if (S.started) return;
  S.started = true;
  $('chat-input').disabled = false;
  await fetchUsers();
  await fetchSidebar();
  if (!isPhone()) openChat('group', { push: false });
  connectWebSocket();
}

// ── People & chat list ────────────────────────────────────────────
async function fetchUsers() {
  try {
    const list = await (await fetch('/api/users')).json();
    S.users = new Map(list.map(u => [u.mac, { id: u.mac, name: u.username || 'Unknown', avatar: u.avatar, online: u.is_active }]));
  } catch (e) { /* keep what we have */ }
}

async function fetchSidebar() {
  const rows = new Map();
  try {
    for (let page = 0; page < 20; page++) {
      const data = await (await fetch(`/api/sidebar?page=${page}&limit=100`)).json();
      for (const it of data.items) {
        rows.set(it.id, {
          id: it.id, title: it.title || 'Unknown', type: it.type, pinned: it.is_pinned, unread: it.unread || 0,
          members: it.members, createdBy: it.created_by, avatar: it.avatar,
          preview: data.previews[it.id] || null,
        });
      }
      if (!data.has_more) break;
    }
  } catch (e) { return; }
  if (S.current && rows.has(S.current)) rows.get(S.current).unread = 0;  // open chat is being read
  S.rows = rows;
  renderList();
  if (S.current) renderHeader();
}

function rowOrder(a, b) {
  if (a.pinned !== b.pinned) return a.pinned ? -1 : 1;
  const ta = a.preview && a.preview.time_id > 0 ? a.preview.time_id : -1;
  const tb = b.preview && b.preview.time_id > 0 ? b.preview.time_id : -1;
  if (ta !== tb) return tb - ta;
  const oa = a.type === 'user' && S.users.get(a.id)?.online, ob = b.type === 'user' && S.users.get(b.id)?.online;
  if (oa !== ob) return oa ? -1 : 1;
  return a.title.localeCompare(b.title);
}

function renderList() {
  const box = $('chat-list-el');
  const q = $('user-search').value.trim().toLowerCase();
  const rows = [...S.rows.values()].filter(r => !q || r.title.toLowerCase().includes(q)).sort(rowOrder);
  const scroll = box.scrollTop;
  box.replaceChildren(...rows.map(rowEl));
  if (!rows.length) {
    const p = document.createElement('div');
    p.className = 'list-empty';
    p.textContent = q ? 'No chats match your search.' : 'No chats yet.';
    box.appendChild(p);
  }
  box.scrollTop = scroll;
  updateTitle();
}

function rowEl(r) {
  const el = document.createElement('div');
  el.className = 'chat-row' + (r.id === S.current ? ' active' : '') + (r.unread ? ' unread' : '');
  el.setAttribute('role', 'listitem');
  el.tabIndex = 0;
  el.dataset.id = r.id;
  const u = S.users.get(r.id);
  el.appendChild(avatar({ id: r.id, name: r.title, url: r.avatar || u?.avatar, kind: r.type, online: r.type === 'user' && u?.online }));

  const main = document.createElement('div');
  main.className = 'row-main';
  const top = document.createElement('div'); top.className = 'row-top';
  const title = document.createElement('span'); title.className = 'row-title'; title.textContent = r.title;
  const time = document.createElement('span'); time.className = 'row-time';
  time.textContent = r.preview && r.preview.time_id > 0 ? fmtListTime(parseTs(r.preview.ts)) : '';
  top.append(title, time);

  const bottom = document.createElement('div'); bottom.className = 'row-bottom';
  const prev = document.createElement('span'); prev.className = 'row-preview';
  const typers = Object.keys(S.typing[r.id] || {});
  if (typers.length) {
    prev.classList.add('typing');
    prev.textContent = r.type === 'user' ? 'typing…' : `${typers.length === 1 ? (S.users.get(typers[0])?.name || 'Someone') : 'Several people'} typing…`;
  } else if (r.preview && r.preview.time_id > 0) {
    const who = r.preview.mine ? 'You' : (r.type !== 'user' && r.preview.sender ? r.preview.sender.split(' ')[0] : '');
    if (who) { const w = document.createElement('span'); w.className = 'who'; w.textContent = who + ': '; prev.appendChild(w); }
    prev.append(r.preview.text || '');
  } else {
    prev.textContent = r.type === 'user' ? 'Say hello 👋' : 'No messages yet';
  }
  bottom.appendChild(prev);
  if (r.pinned) { const p = document.createElement('span'); p.className = 'row-pin'; p.innerHTML = ICONS.pin; p.title = 'Pinned'; bottom.appendChild(p); }
  if (r.unread) { const b = document.createElement('span'); b.className = 'row-badge'; b.textContent = r.unread > 99 ? '99+' : r.unread; bottom.appendChild(b); }
  main.append(top, bottom);
  el.appendChild(main);

  const more = document.createElement('button');
  more.type = 'button'; more.className = 'icon-btn row-more'; more.innerHTML = ICONS.more;
  more.title = 'More'; more.setAttribute('aria-label', `Options for ${r.title}`);
  more.dataset.act = 'row-menu';
  el.appendChild(more);
  return el;
}

function updateTitle() {
  const n = [...S.rows.values()].reduce((s, r) => s + (r.unread || 0), 0);
  document.title = n ? `(${n}) Messenger — NetShare Hub` : 'Messenger — NetShare Hub';
}

function rowMenu(r, anchor) {
  const items = [
    { icon: ICONS.pin, label: r.pinned ? 'Unpin chat' : 'Pin chat', run: () => togglePin(r) },
  ];
  if (r.unread) items.push({ icon: ICONS.markRead, label: 'Mark as read', run: () => { markRead(r.id, true); r.unread = 0; renderList(); } });
  openMenu(anchor, { items });
}

async function togglePin(r) {
  const res = await fetch('/api/pins', {
    method: r.pinned ? 'DELETE' : 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ target: r.id }),
  });
  if (res.ok) { r.pinned = !r.pinned; renderList(); }
}

// ── Open / close a conversation ───────────────────────────────────
async function openChat(id, { push = true } = {}) {
  if (S.editing) cancelEdit();
  if (S.current) S.drafts[S.current] = $('chat-input').value;
  const r = S.rows.get(id);
  const unread = r ? r.unread : 0;
  if (r) r.unread = 0;
  S.current = id;
  S.msgs = []; S.hasOlder = false; S.newBelow = 0; S.unreadFrom = null;
  updateJump();
  renderHeader();
  renderList();

  const input = $('chat-input');
  input.value = S.drafts[id] || '';
  autoGrow(); updateComposerButtons();

  if (isPhone()) {
    document.body.classList.add('chat-open');
    if (push) history.pushState({ chat: id }, '');
  } else {
    input.focus({ preventScroll: true });
  }

  const box = $('chat-container');
  box.innerHTML = '<div class="loading">Loading…</div>';
  let msgs = [];
  try {
    msgs = await (await fetch(`/api/messages?target=${encodeURIComponent(id)}&limit=${PAGE}`)).json();
  } catch (e) {
    box.innerHTML = '<div class="loading">Couldn\'t load messages.</div>';
    return;
  }
  if (S.current !== id) return;
  S.msgs = msgs;
  S.hasOlder = msgs.length >= PAGE;
  if (unread) {  // the divider goes above the first message I haven't read
    const theirs = msgs.filter(m => m.from_mac !== S.me.id);
    const first = theirs[Math.max(0, theirs.length - unread)];
    S.unreadFrom = first ? first.id : null;
  }
  renderMessages();
  const divider = box.querySelector('.unread-sep');
  if (divider) divider.scrollIntoView({ block: 'center' });
  else box.scrollTop = box.scrollHeight;
  markRead(id);
}

function closeChat() {
  if (!isPhone()) return;
  if (S.current) S.drafts[S.current] = $('chat-input').value;
  document.body.classList.remove('chat-open');
  S.current = null;
  renderList();
}

function renderHeader() {
  const id = S.current;
  if (!id) return;
  const r = S.rows.get(id) || { id, title: 'Chat', type: isRoom(id) ? 'cgroup' : 'user' };
  const u = S.users.get(id);
  const av = avatar({ id, name: r.title, url: r.avatar || u?.avatar, kind: r.type, online: r.type === 'user' && u?.online, size: 'lg' });
  av.id = 'chat-head-avatar';
  $('chat-head-avatar').replaceWith(av);
  $('chat-title').textContent = r.title;

  const sub = $('chat-subtitle');
  sub.className = '';
  const typers = Object.keys(S.typing[id] || {});
  if (typers.length) {
    sub.className = 'typing';
    sub.textContent = r.type === 'user' ? 'typing…'
      : `${typers.map(t => (S.users.get(t)?.name || 'Someone').split(' ')[0]).slice(0, 2).join(', ')} ${typers.length > 1 ? 'are' : 'is'} typing…`;
  } else if (r.type === 'user') {
    sub.textContent = u?.online ? 'online' : 'offline';
    if (u?.online) sub.className = 'online';
  } else if (r.type === 'global') {
    const online = [...S.users.values()].filter(x => x.online && x.id !== S.me.id).length + 1;  // + me
    sub.textContent = `Everyone on the network · ${online} online`;
  } else {
    sub.textContent = r.members ? `${r.members} members` : 'Group';
  }

  const tools = $('chat-header-tools');
  tools.replaceChildren();
  if (r.type === 'cgroup') {
    tools.appendChild(toolBtn(ICONS.members, 'Members', () => openMembers(id)));
    if (r.createdBy === S.me.id) tools.appendChild(toolBtn(ICONS.settings, 'Group settings', () => openEditGroup(id)));
  }
}

function toolBtn(icon, label, run) {
  const b = document.createElement('button');
  b.type = 'button'; b.className = 'icon-btn'; b.innerHTML = icon; b.title = label;
  b.setAttribute('aria-label', label); b.addEventListener('click', run);
  return b;
}

// ── Messages ──────────────────────────────────────────────────────
const msgDate = m => parseTs(m.timestamp);

function sameRun(a, b) {
  if (!a || !b || a.from_mac !== b.from_mac) return false;
  const da = msgDate(a), db = msgDate(b);
  return sameDay(da, db) && Math.abs(db - da) < GROUP_GAP_MS;
}

function flagsAt(i) {
  const m = S.msgs[i], prev = S.msgs[i - 1], next = S.msgs[i + 1];
  const joinPrev = sameRun(prev, m) && m.id !== S.unreadFrom;
  const joinNext = sameRun(m, next) && next.id !== S.unreadFrom;
  return { first: !joinPrev, last: !joinNext };
}

function renderMessages() {
  const box = $('chat-container');
  const frag = document.createDocumentFragment();
  if (S.hasOlder) {
    const b = document.createElement('button');
    b.type = 'button'; b.className = 'load-older'; b.textContent = 'Load earlier messages';
    b.addEventListener('click', loadOlder);
    frag.appendChild(b);
  } else {
    const p = document.createElement('div');
    p.className = 'chat-start';
    p.textContent = S.msgs.length ? 'This is the beginning of the conversation' : 'No messages yet — say hello 👋';
    frag.appendChild(p);
  }
  S.msgs.forEach((m, i) => {
    const prev = S.msgs[i - 1];
    if (!prev || !sameDay(msgDate(prev), msgDate(m))) frag.appendChild(daySep(msgDate(m)));
    if (m.id === S.unreadFrom) frag.appendChild(unreadSep());
    frag.appendChild(msgEl(m, flagsAt(i)));
  });
  box.replaceChildren(frag);
}

function daySep(d) {
  const el = document.createElement('div');
  el.className = 'day-sep';
  const s = document.createElement('span'); s.textContent = d ? fmtDay(d) : '';
  el.appendChild(s);
  return el;
}

function unreadSep() {
  const el = document.createElement('div');
  el.className = 'unread-sep';
  const s = document.createElement('span'); s.textContent = 'Unread messages';
  el.appendChild(s);
  return el;
}

function msgEl(m, { first, last }) {
  const mine = m.from_mac === S.me.id;
  const room = isRoom(S.current);
  const jumbo = m.msg_type === 'text' && !m.is_deleted && isJumbo(m.content);
  const row = document.createElement('div');
  row.className = `msg${mine ? ' mine' : ''}${room ? '' : ' dm'}${first ? ' first' : ''}${last ? ' last' : ''}${!first && !last ? ' middle' : ''}${jumbo ? ' jumbo' : ''}`;
  row.id = `msg-${m.id}`;
  row.dataset.id = m.id;

  const slot = document.createElement('div');
  slot.className = 'avatar-slot';
  if (!mine && room && last) {
    const u = S.users.get(m.from_mac);
    slot.appendChild(avatar({ id: m.from_mac, name: m.sender_name || u?.name, url: u?.avatar, size: 'xs' }));
  }
  row.appendChild(slot);

  const col = document.createElement('div');
  col.className = 'msg-col';
  if (!mine && room && first) {
    const n = document.createElement('div');
    n.className = 'msg-sender'; n.textContent = m.sender_name || 'Unknown'; n.style.color = nameColor(m.from_mac);
    col.appendChild(n);
  }
  col.appendChild(bubbleEl(m, mine));
  const rx = reactionsEl(m);
  if (rx) col.appendChild(rx);
  row.appendChild(col);

  if (!m.is_deleted) {
    const acts = document.createElement('div');
    acts.className = 'msg-actions';
    const react = toolBtn(ICONS.smile, 'React', e => openMenu(e.currentTarget, msgMenu(m, true)));
    const more = toolBtn(ICONS.more, 'More', e => openMenu(e.currentTarget, msgMenu(m)));
    acts.append(react, more);
    row.appendChild(acts);
  }
  return row;
}

function metaEl(m, mine) {
  const meta = document.createElement('span');
  meta.className = 'meta';
  const d = msgDate(m);
  meta.title = d ? d.toLocaleString() : '';
  meta.append((m.is_edited && !m.is_deleted ? 'edited · ' : '') + fmtTime(d));
  if (mine && !isRoom(S.current) && !m.is_deleted) {
    const t = document.createElement('span');
    const read = m.status === 'read';
    t.className = 'ticks' + (read ? ' read' : '');
    t.innerHTML = read ? ICONS.ticks : ICONS.tick;
    t.title = read ? 'Read' : 'Sent';
    meta.appendChild(t);
  }
  return meta;
}

function bubbleEl(m, mine) {
  const b = document.createElement('div');
  b.className = 'bubble';
  if (m.is_deleted) {
    b.classList.add('deleted');
    const t = document.createElement('span');
    t.className = 'deleted-text'; t.innerHTML = ICONS.ban;
    t.append(mine ? 'You deleted this message' : 'This message was deleted');
    b.append(t, metaEl(m, mine));
    return b;
  }
  if (m.msg_type === 'text') {
    const t = document.createElement('div');
    t.className = 'msg-text';
    t.appendChild(linkify(m.content || ''));
    t.appendChild(metaEl(m, mine));
    b.appendChild(t);
    return b;
  }
  const url = m.file_url;
  const type = m.filetype || '';
  if (!url) {
    const x = document.createElement('div'); x.className = 'att-missing'; x.textContent = 'Attachment unavailable';
    b.append(x, metaEl(m, mine));
    return b;
  }
  if (type.startsWith('image/')) {
    b.classList.add('media');
    const img = document.createElement('img');
    img.className = 'att-img'; img.src = url; img.alt = m.filename || 'Photo'; img.loading = 'lazy';
    img.addEventListener('click', () => openLightbox(url, m.filename || 'Photo'));
    img.addEventListener('load', () => { if (nearBottom(400)) scrollToBottom(); }, { once: true });
    b.append(img, metaEl(m, mine));
  } else if (type.startsWith('audio/')) {
    const wrap = document.createElement('div'); wrap.className = 'att-audio';
    const audio = document.createElement('audio');
    audio.controls = true; audio.preload = 'metadata'; audio.src = url;
    wrap.appendChild(audio);
    b.append(wrap, metaEl(m, mine));
  } else {
    const a = document.createElement('a');
    a.className = 'att-file'; a.href = url; a.target = '_blank'; a.rel = 'noopener'; a.download = m.filename || '';
    const icon = document.createElement('span'); icon.className = 'file-icon'; icon.innerHTML = ICONS.file;
    const info = document.createElement('span');
    const name = document.createElement('div'); name.className = 'file-name'; name.textContent = m.filename || 'File';
    const sub = document.createElement('div'); sub.className = 'file-sub'; sub.textContent = 'Tap to download';
    info.append(name, sub);
    a.append(icon, info);
    b.append(a, metaEl(m, mine));
  }
  return b;
}

function reactionsEl(m) {
  const entries = Object.entries(m.reactions || {}).filter(([, who]) => who.length);
  if (!entries.length || m.is_deleted) return null;
  const box = document.createElement('div');
  box.className = 'reactions';
  for (const [emoji, who] of entries) {
    const mine = who.includes(S.me.id);
    const chip = document.createElement('button');
    chip.type = 'button';
    chip.className = 'reaction' + (mine ? ' mine' : '');
    chip.title = who.map(id => id === S.me.id ? 'You' : (S.users.get(id)?.name || 'Someone')).join(', ');
    chip.append(emoji);
    if (who.length > 1) { const n = document.createElement('b'); n.textContent = who.length; chip.appendChild(n); }
    chip.addEventListener('click', () => react(m, emoji));
    box.appendChild(chip);
  }
  return box;
}

/** A new message in the open chat: add it without re-rendering (keeps audio playing). */
function appendMessage(m) {
  if (S.msgs.some(x => x.id === m.id)) return updateMessage(m);
  const box = $('chat-container');
  const prev = S.msgs[S.msgs.length - 1];
  S.msgs.push(m);
  const i = S.msgs.length - 1;
  if (prev) {  // the previous message may stop being the last of its run
    const node = $(`msg-${prev.id}`);
    if (node) node.replaceWith(msgEl(prev, flagsAt(i - 1)));
  } else {
    const start = box.querySelector('.chat-start');
    if (start) start.textContent = 'This is the beginning of the conversation';
  }
  if (!prev || !sameDay(msgDate(prev), msgDate(m))) box.appendChild(daySep(msgDate(m)));
  box.appendChild(msgEl(m, flagsAt(i)));
}

function updateMessage(m) {
  const i = S.msgs.findIndex(x => x.id === m.id);
  if (i < 0) return;
  const old = S.msgs[i];
  S.msgs[i] = m;
  const node = $(`msg-${m.id}`);
  if (!node) return;
  const sameBody = old.content === m.content && old.is_deleted === m.is_deleted && old.file_url === m.file_url
    && old.is_edited === m.is_edited && old.status === m.status;
  if (sameBody && !m.is_deleted) {  // only reactions changed: keep the bubble (an audio may be playing)
    const col = node.querySelector('.msg-col');
    col.querySelector('.reactions')?.remove();
    const rx = reactionsEl(m);
    if (rx) col.appendChild(rx);
  } else {
    node.replaceWith(msgEl(m, flagsAt(i)));
  }
}

async function loadOlder() {
  if (S.loadingOlder || !S.hasOlder || !S.msgs.length) return;
  S.loadingOlder = true;
  const id = S.current, box = $('chat-container');
  try {
    const older = await (await fetch(`/api/messages?target=${encodeURIComponent(id)}&limit=${PAGE}&before_id=${S.msgs[0].id}`)).json();
    if (S.current !== id) return;
    const before = box.scrollHeight - box.scrollTop;
    S.msgs = older.concat(S.msgs);
    S.hasOlder = older.length >= PAGE;
    renderMessages();
    box.scrollTop = box.scrollHeight - before;
  } catch (e) { showToast('Couldn\'t load earlier messages', 'error'); }
  finally { S.loadingOlder = false; }
}

const nearBottom = (px = 150) => { const b = $('chat-container'); return b.scrollHeight - b.scrollTop - b.clientHeight < px; };
function scrollToBottom(smooth = false) {
  const b = $('chat-container');
  b.scrollTo({ top: b.scrollHeight, behavior: smooth ? 'smooth' : 'auto' });
}
function updateJump() {
  const show = S.current && !nearBottom(300);
  $('jump-btn').hidden = !show;
  $('jump-count').hidden = !S.newBelow;
  $('jump-count').textContent = S.newBelow > 99 ? '99+' : S.newBelow;
}

function markRead(id = S.current, force = false) {
  if (!id || (!force && document.hidden)) return;
  let last = 0;
  if (id === S.current && S.msgs.length) last = S.msgs[S.msgs.length - 1].id;
  else last = S.rows.get(id)?.preview?.time_id || 0;
  if (!force && last <= (S.lastReadSent[id] || 0)) return;
  S.lastReadSent[id] = last;
  wsSend({ type: 'read', to_target: id, last_id: last || undefined });
}

// ── Message menu: react, copy, edit, delete ───────────────────────
function msgMenu(m, reactionsOnly = false) {
  const mine = m.from_mac === S.me.id;
  const myReaction = Object.entries(m.reactions || {}).find(([, who]) => who.includes(S.me.id))?.[0];
  const spec = { reactions: m.is_deleted ? [] : QUICK_REACTIONS, current: myReaction, onReact: e => react(m, e), items: [] };
  if (reactionsOnly) return spec;
  if (m.msg_type === 'text' && !m.is_deleted)
    spec.items.push({ icon: ICONS.copy, label: 'Copy text', run: () => copyToClipboard(m.content || '').then(() => showToast('Copied')).catch(() => showToast('Couldn\'t copy', 'error')) });
  if (m.msg_type === 'file' && m.file_url)
    spec.items.push({ icon: ICONS.download, label: 'Download', run: () => { const a = document.createElement('a'); a.href = m.file_url; a.download = m.filename || ''; a.click(); } });
  if (mine && m.msg_type === 'text' && !m.is_deleted) spec.items.push({ icon: ICONS.edit, label: 'Edit', run: () => startEdit(m) });
  if (mine && !m.is_deleted) spec.items.push({ icon: ICONS.trash, label: 'Delete for everyone', danger: true, run: () => askDelete(m) });
  return spec;
}

let menuOpenedAt = 0;
function openMenu(anchor, spec) {
  closeMenu();
  menuOpenedAt = Date.now();
  const menu = $('msg-menu');
  const rx = $('menu-reactions');
  rx.replaceChildren(...(spec.reactions || []).map(e => {
    const b = document.createElement('button');
    b.type = 'button'; b.textContent = e; b.className = e === spec.current ? 'on' : '';
    b.setAttribute('aria-label', `React ${e}`);
    b.addEventListener('click', () => { closeMenu(); spec.onReact(e); });
    return b;
  }));
  const items = $('menu-items');
  items.replaceChildren(...(spec.items || []).map(it => {
    const b = document.createElement('button');
    b.type = 'button'; b.className = it.danger ? 'danger' : ''; b.setAttribute('role', 'menuitem');
    b.innerHTML = it.icon;
    b.append(it.label);
    b.addEventListener('click', () => { closeMenu(); it.run(); });
    return b;
  }));
  items.hidden = !(spec.items || []).length;
  menu.hidden = false; $('menu-scrim').hidden = false;
  // place next to the anchor (element or {x, y}), kept on screen
  const r = anchor.getBoundingClientRect ? anchor.getBoundingClientRect() : { left: anchor.x, right: anchor.x, top: anchor.y, bottom: anchor.y };
  const w = menu.offsetWidth, h = menu.offsetHeight, pad = 8;
  let x = Math.min(Math.max(pad, r.left), window.innerWidth - w - pad);
  let y = r.bottom + 6;
  if (y + h > window.innerHeight - pad) y = Math.max(pad, r.top - h - 6);
  menu.style.left = x + 'px'; menu.style.top = y + 'px';
  (menu.querySelector('button') || menu).focus({ preventScroll: true });
}

function closeMenu() { $('msg-menu').hidden = true; $('menu-scrim').hidden = true; }

async function react(m, emoji) {
  const mineNow = (m.reactions?.[emoji] || []).includes(S.me.id);
  try {
    const res = await fetch(`/api/messages/${m.id}/react`, {
      method: mineNow ? 'DELETE' : 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ emoji }),
    });
    if (!res.ok) throw new Error(res.status);
  } catch (e) { showToast('Couldn\'t react', 'error'); }
}

function startEdit(m) {
  S.editing = m;
  $('edit-bar').hidden = false;
  $('edit-preview').textContent = m.content || '';
  const input = $('chat-input');
  if (S.current) S.drafts[S.current] = input.value;
  input.value = m.content || '';
  autoGrow(); updateComposerButtons();
  input.focus();
  input.setSelectionRange(input.value.length, input.value.length);
}

function cancelEdit() {
  S.editing = null;
  $('edit-bar').hidden = true;
  $('chat-input').value = S.current ? (S.drafts[S.current] || '') : '';
  autoGrow(); updateComposerButtons();
}

let confirmAction = null;
function askDelete(m) {
  confirmAction = () => { if (!wsSend({ type: 'delete', msg_id: m.id })) showToast('Not connected — try again in a moment', 'error'); };
  $('confirm-title').textContent = 'Delete message?';
  $('confirm-text').textContent = 'It will be deleted for everyone in this chat.';
  openModal('confirm-modal');
}

// ── Composer ──────────────────────────────────────────────────────
function autoGrow() {
  const el = $('chat-input');
  el.style.height = 'auto';
  el.style.height = Math.min(el.scrollHeight, 160) + 'px';
}

function updateComposerButtons() {
  const hasText = !!$('chat-input').value.trim();
  const recording = !!voice;
  $('send-btn').hidden = !(hasText || S.editing || recording);
  $('mic-btn').hidden = hasText || !!S.editing || recording;
}

function submitComposer() {
  if (voice) return stopVoiceRecording(false);
  const input = $('chat-input');
  const text = input.value.trim();
  if (S.editing) {
    const m = S.editing;
    if (text && text !== m.content) {
      if (!wsSend({ type: 'edit', msg_id: m.id, text })) return showToast('Not connected — try again in a moment', 'error');
    }
    S.drafts[S.current] = '';
    cancelEdit();
    return;
  }
  if (!text || !S.current) return;
  sendOrQueue({ type: 'text', to_target: S.current, text });
  input.value = '';
  S.drafts[S.current] = '';
  autoGrow(); updateComposerButtons();
  stopTyping();
  if (S.unreadFrom) { S.unreadFrom = null; $('chat-container').querySelector('.unread-sep')?.remove(); }
}

let typingSentAt = 0, typingStopTimer = null;
function onComposerInput() {
  autoGrow(); updateComposerButtons();
  if (S.editing || !S.current) return;
  if (!$('chat-input').value.trim()) return stopTyping();
  const now = Date.now();
  if (now - typingSentAt > 2500) { wsSend({ type: 'typing', target: S.current, is_typing: true }); typingSentAt = now; }
  clearTimeout(typingStopTimer);
  typingStopTimer = setTimeout(stopTyping, 3000);
}
function stopTyping() {
  clearTimeout(typingStopTimer);
  if (typingSentAt && S.current) wsSend({ type: 'typing', target: S.current, is_typing: false });
  typingSentAt = 0;
}

// ── Files: attach, paste, drag & drop ─────────────────────────────
const uploads = [];
let uploading = false;

function queueFiles(files) {
  if (!S.current) return;
  for (const f of files) uploads.push({ file: f, target: S.current });
  pumpUploads();
}

async function pumpUploads() {
  if (uploading) return;
  uploading = true;
  const total = uploads.length;
  let n = 0;
  while (uploads.length) {
    const job = uploads.shift();
    n++;
    $('upload-bar').hidden = false;
    $('upload-name').textContent = `Sending ${job.file.name}${total > 1 ? ` (${n}/${total})` : ''}`;
    $('upload-fill').style.width = '0%';
    try {
      const result = await uploadWithProgress(job.file, p => { $('upload-fill').style.width = Math.round(p * 100) + '%'; });
      sendOrQueue({ type: 'file', to_target: job.target, filename: result.filename, url: result.url, filetype: job.file.type || '' });
    } catch (e) {
      showToast(`❌ Couldn't send ${job.file.name}: ${e.message}`, 'error', 4500);
    }
  }
  $('upload-bar').hidden = true;
  uploading = false;
}

function uploadWithProgress(file, onProgress, extra = {}) {
  return new Promise((resolve, reject) => {
    const fd = new FormData();
    fd.append('file', file, file.name);
    for (const [k, v] of Object.entries(extra)) fd.append(k, v);
    const xhr = new XMLHttpRequest();
    xhr.open('POST', '/chat/upload');
    const token = localStorage.getItem('pin_token');
    if (token) xhr.setRequestHeader('X-Server-PIN-Token', token);
    xhr.upload.onprogress = e => { if (e.lengthComputable) onProgress(e.loaded / e.total); };
    xhr.onload = () => {
      let data = {};
      try { data = JSON.parse(xhr.responseText); } catch (e) { /* not JSON */ }
      if (xhr.status >= 200 && xhr.status < 300 && data.status === 'ok') resolve(data);
      else reject(new Error(data.message || data.error || `HTTP ${xhr.status}`));
    };
    xhr.onerror = () => reject(new Error('network error'));
    xhr.send(fd);
  });
}

// ── Photo viewer ──────────────────────────────────────────────────
function openLightbox(url, name) {
  $('lightbox-img').src = url;
  $('lightbox-name').textContent = name;
  $('lightbox-download').href = url;
  $('lightbox-download').setAttribute('download', name);
  $('lightbox').hidden = false;
}
function closeLightbox() { $('lightbox').hidden = true; $('lightbox-img').removeAttribute('src'); }

// ── Dialogs ───────────────────────────────────────────────────────
function openModal(id) { $(id).hidden = false; setTimeout(() => $(id).querySelector('input:not([type=hidden]):not([hidden]), .btn.primary')?.focus(), 30); }
function closeModal(id) { $(id).hidden = true; }
const openModals = () => [...document.querySelectorAll('.modal:not([hidden])')];

let profileFirstRun = false;
function openProfile(firstRun = false) {
  profileFirstRun = firstRun;
  $('modal-title').textContent = firstRun ? 'Welcome to Messenger' : 'Your profile';
  $('modal-sub').textContent = firstRun ? 'Choose the name other people will see.' : 'Your name and photo are visible to everyone.';
  $('modal-btn').textContent = firstRun ? 'Join' : 'Save';
  $('profile-cancel').hidden = firstRun;
  $('username-input').value = S.me.name;
  const prev = avatar({ id: S.me.id || 'me', name: S.me.name || '?', url: S.me.avatar, size: 'xl' });
  prev.id = 'profile-avatar-preview';
  $('profile-avatar-preview').replaceWith(prev);
  $('avatar-btn').hidden = firstRun;
  openModal('username-modal');
}

async function saveProfile() {
  const name = $('username-input').value.trim();
  if (!name) return $('username-input').focus();
  try {
    const data = await (await fetch('/api/user', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ username: name }),
    })).json();
    setMe({ mac: data.mac, username: data.username, avatar: S.me.avatar });
    closeModal('username-modal');
    if (profileFirstRun) start();
  } catch (e) { showToast('Couldn\'t save your name', 'error'); }
}

async function uploadAvatar() {
  const input = $('avatar-input');
  const file = input.files[0];
  input.value = '';
  if (!file) return;
  if (file.size > 8 * 1024 * 1024) return showToast('Picture is too large (max 8 MB)', 'error');
  const fd = new FormData();
  fd.append('file', file);
  try {
    const res = await fetch('/api/avatar', { method: 'POST', body: fd });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
    setMe({ mac: S.me.id, username: S.me.name, avatar: data.avatar });
    const prev = avatar({ id: S.me.id, name: S.me.name, url: data.avatar, size: 'xl' });
    prev.id = 'profile-avatar-preview';
    $('profile-avatar-preview').replaceWith(prev);
    showToast('✅ Profile photo updated');
  } catch (e) { showToast('❌ ' + e.message, 'error', 4000); }
}

function personRow(u, { checkbox = false, checked = false, tag = '' } = {}) {
  const row = document.createElement(checkbox ? 'label' : 'div');
  row.className = 'person';
  row.appendChild(avatar({ id: u.id, name: u.name, url: u.avatar, online: u.online, size: 'sm' }));
  const name = document.createElement('div');
  name.className = 'name';
  name.textContent = u.id === S.me.id ? `${u.name} (you)` : u.name;
  const sub = document.createElement('div');
  sub.className = 'sub'; sub.textContent = u.online ? 'online' : 'offline';
  name.appendChild(sub);
  row.appendChild(name);
  if (tag) { const t = document.createElement('span'); t.className = 'tag'; t.textContent = tag; row.appendChild(t); }
  if (checkbox) {
    const cb = document.createElement('input');
    cb.type = 'checkbox'; cb.value = u.id; cb.checked = checked;
    row.appendChild(cb);
  }
  return row;
}

function fillPeople(listId, searchId, checkedIds = new Set()) {
  const people = [...S.users.values()].filter(u => u.id !== S.me.id).sort((a, b) => (b.online - a.online) || a.name.localeCompare(b.name));
  const render = () => {
    const q = $(searchId).value.trim().toLowerCase();
    const checked = new Set([...$(listId).querySelectorAll('input:checked')].map(c => c.value).concat([...checkedIds]));
    checkedIds = checked;
    const shown = people.filter(u => !q || u.name.toLowerCase().includes(q));
    $(listId).replaceChildren(...shown.map(u => personRow(u, { checkbox: true, checked: checked.has(u.id) })));
    if (!shown.length) { const p = document.createElement('div'); p.className = 'list-empty'; p.textContent = 'Nobody found.'; $(listId).appendChild(p); }
  };
  $(listId).onchange = e => { if (e.target.type === 'checkbox') e.target.checked ? checkedIds.add(e.target.value) : checkedIds.delete(e.target.value); };
  $(searchId).value = '';
  $(searchId).oninput = render;
  render();
  return () => [...checkedIds];
}

let groupPick = null;
function openNewGroup() {
  $('group-name-input').value = '';
  groupPick = fillPeople('group-user-list', 'group-member-search');
  openModal('group-modal');
}

async function createGroup() {
  const name = $('group-name-input').value.trim();
  const members = groupPick();
  if (!name) return $('group-name-input').focus();
  if (!members.length) return showToast('Pick at least one person', 'info');
  const res = await fetch('/api/groups', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name, members }) });
  if (!res.ok) return showToast('Couldn\'t create the group', 'error');
  const { id } = await res.json();
  closeModal('group-modal');
  await fetchSidebar();
  openChat(id);
}

let editPick = null;
async function openEditGroup(id) {
  const r = S.rows.get(id);
  if (!r) return;
  closeModal('view-members-modal');
  let current = [];
  try { current = await (await fetch(`/api/groups/${encodeURIComponent(id)}/members`)).json(); } catch (e) { /* empty */ }
  $('edit-group-id-hidden').value = id;
  $('edit-group-name-input').value = r.title;
  editPick = fillPeople('edit-group-user-list', 'edit-member-search', new Set(current.filter(x => x !== S.me.id)));
  openModal('edit-group-modal');
}

async function saveGroup() {
  const id = $('edit-group-id-hidden').value;
  const name = $('edit-group-name-input').value.trim();
  const members = editPick();
  if (!name) return $('edit-group-name-input').focus();
  if (!members.length) return showToast('Keep at least one other member', 'info');
  const res = await fetch(`/api/groups/${encodeURIComponent(id)}`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name, members }) });
  if (!res.ok) return showToast('Couldn\'t save the group', 'error');
  closeModal('edit-group-modal');
  await fetchSidebar();
}

async function openMembers(id) {
  const r = S.rows.get(id);
  if (!r) return;
  let ids = [];
  try { ids = await (await fetch(`/api/groups/${encodeURIComponent(id)}/members`)).json(); } catch (e) { return; }
  const people = ids.map(x => x === S.me.id ? { id: x, name: S.me.name, avatar: S.me.avatar, online: true } : (S.users.get(x) || { id: x, name: 'Unknown', online: false }));
  people.sort((a, b) => (a.id === r.createdBy ? -1 : b.id === r.createdBy ? 1 : (b.online - a.online) || a.name.localeCompare(b.name)));
  $('members-title').textContent = `${r.title} · ${people.length} members`;
  $('view-members-list').replaceChildren(...people.map(u => personRow(u, { tag: u.id === r.createdBy ? 'Admin' : '' })));
  $('members-edit').hidden = r.createdBy !== S.me.id;
  $('members-edit').onclick = () => openEditGroup(id);
  openModal('view-members-modal');
}

// ── Search across chats ───────────────────────────────────────────
let searchTimer = null;
function openSearch() {
  $('global-search-input').value = '';
  $('search-results-list').replaceChildren();
  openModal('search-modal');
}

async function runSearch() {
  const q = $('global-search-input').value.trim();
  const list = $('search-results-list');
  if (q.length < 2) { list.replaceChildren(); return; }
  let hits = [];
  try { hits = await (await fetch(`/api/search?q=${encodeURIComponent(q)}`)).json(); } catch (e) { return; }
  if ($('global-search-input').value.trim() !== q) return;
  if (!hits.length) {
    const p = document.createElement('div'); p.className = 'list-empty'; p.textContent = 'No messages found.';
    return list.replaceChildren(p);
  }
  list.replaceChildren(...hits.map(m => {
    const conv = conversationOf(m);
    const b = document.createElement('button');
    b.type = 'button'; b.className = 'search-hit';
    const top = document.createElement('div'); top.className = 'hit-top';
    const where = document.createElement('b');
    const chatName = S.rows.get(conv)?.title || 'Chat';
    where.textContent = isRoom(conv) ? `${m.sender_name || 'Someone'} · ${chatName}` : chatName;
    const when = document.createElement('span'); when.textContent = fmtListTime(parseTs(m.timestamp));
    top.append(where, when);
    const text = document.createElement('div'); text.className = 'hit-text';
    const content = m.content || '';
    const at = content.toLowerCase().indexOf(q.toLowerCase());
    const from = Math.max(0, at - 40);
    const snippet = (from ? '…' : '') + content.slice(from);
    const pos = at - from + (from ? 1 : 0);
    if (at >= 0) {
      const mark = document.createElement('mark'); mark.textContent = snippet.slice(pos, pos + q.length);
      text.append(snippet.slice(0, pos), mark, snippet.slice(pos + q.length));
    } else text.textContent = snippet;
    b.append(top, text);
    b.addEventListener('click', async () => {
      closeModal('search-modal');
      await openChat(conv);
      const node = $(`msg-${m.id}`);
      if (node) { node.scrollIntoView({ block: 'center' }); node.classList.add('flash'); }
    });
    return b;
  }));
}

// ── Voice messages ────────────────────────────────────────────────
// Browsers expose the microphone only on https:// or localhost. Devices on the LAN
// use http://192.168.x.x, so there the mic button offers the app's own https link.
const VOICE_MAX_SECONDS = 300;
const VOICE_TYPES = [  // MP4/AAC plays everywhere (incl. iPhone); Safari records only MP4
  ['audio/mp4;codecs=mp4a.40.2', 'm4a'], ['audio/mp4', 'm4a'], ['audio/aac', 'aac'],
  ['audio/webm;codecs=opus', 'webm'], ['audio/webm', 'webm'], ['audio/ogg;codecs=opus', 'ogg'],
];
let voice = null;

const micSupported = () => !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia && window.MediaRecorder);
function secureUrl() {
  const port = (window.NSH && window.NSH.httpsPort) || 0;
  return port ? `https://${location.hostname}:${port}${location.pathname}${location.search}` : null;
}

function initVoiceButton() {
  if (micSupported()) return;
  const btn = $('mic-btn');
  btn.classList.add('mic-locked');
  btn.title = window.isSecureContext ? 'This browser can\'t record audio'
    : (secureUrl() ? 'Voice messages need the secure link — tap for details' : 'Voice messages need HTTPS');
}

function showSecureLinkHelp() {
  const url = secureUrl();
  if (!url) return showToast('🎤 Voice messages need a secure (https) page.', 'info', 5000);
  $('voice-secure-url').textContent = url;
  $('voice-secure-open').onclick = () => { location.href = url; };
  openModal('voice-secure-modal');
}

async function toggleVoiceRecording() {
  if (voice) return stopVoiceRecording(false);
  if (!S.current) return;
  if (!micSupported()) {
    if (!window.isSecureContext) return showSecureLinkHelp();
    return showToast('🎤 This browser can\'t record audio', 'error', 4000);
  }
  let stream;
  try {
    stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } });
  } catch (e) {
    const msg = e.name === 'NotAllowedError' ? 'Microphone permission is blocked — allow it in the browser\'s site settings'
      : e.name === 'NotFoundError' ? 'No microphone found on this device'
      : e.name === 'NotReadableError' ? 'The microphone is being used by another app' : 'Could not start the microphone';
    return showToast('🎤 ' + msg, 'error', 5000);
  }
  let mime = '', ext = 'webm';
  for (const [m, x] of VOICE_TYPES) if (MediaRecorder.isTypeSupported?.(m)) { mime = m; ext = x; break; }
  let recorder;
  try { recorder = mime ? new MediaRecorder(stream, { mimeType: mime }) : new MediaRecorder(stream); }
  catch (e) { stream.getTracks().forEach(t => t.stop()); return showToast('🎤 Could not start recording', 'error'); }
  voice = { recorder, stream, chunks: [], ext, started: Date.now(), cancelled: false, target: S.current };
  const v = voice;
  recorder.ondataavailable = e => { if (e.data && e.data.size) v.chunks.push(e.data); };
  recorder.onstop = () => {
    v.stream.getTracks().forEach(t => t.stop());
    if (v.cancelled || !v.chunks.length) return;
    const type = (recorder.mimeType || mime || 'audio/webm').split(';')[0];
    const blob = new Blob(v.chunks, { type });
    const file = new File([blob], `voice_${Date.now()}.${v.ext}`, { type });
    uploadWithProgress(file, () => {}, { msg_type: 'voice' })
      .then(r => sendOrQueue({ type: 'file', to_target: v.target, filename: 'Voice message', url: r.url, filetype: type }))
      .catch(e => showToast('❌ Couldn\'t send the voice message: ' + e.message, 'error', 4000));
  };
  recorder.start(1000);
  setRecordingUI(true);
  v.timer = setInterval(() => {
    const secs = Math.floor((Date.now() - v.started) / 1000);
    $('recording-time').textContent = `${Math.floor(secs / 60)}:${String(secs % 60).padStart(2, '0')}`;
    if (secs >= VOICE_MAX_SECONDS) stopVoiceRecording(false);
  }, 250);
}

function stopVoiceRecording(cancel) {
  const v = voice;
  if (!v) return;
  voice = null;
  clearInterval(v.timer);
  v.cancelled = cancel || Date.now() - v.started < 600;
  setRecordingUI(false);
  try { v.recorder.stop(); } catch (e) { v.stream.getTracks().forEach(t => t.stop()); }
  if (cancel) showToast('Recording discarded', 'info');
}

function setRecordingUI(on) {
  $('mic-btn').classList.toggle('recording-active', on);
  $('recording-bar').hidden = !on;
  $('chat-input').hidden = on;
  $('attach-btn').disabled = on;
  $('recording-time').textContent = '0:00';
  updateComposerButtons();
}

// ── Connection ────────────────────────────────────────────────────
// One socket; reconnects with backoff, pings so a dead connection is noticed,
// and after a reconnect fetches whatever was missed while it was down.
let ws = null, wsEverOpened = false, lastHeard = 0, reconnectDelay = 1000, reconnectTimer = null;
const outbox = [];

function wsSend(obj) {
  if (ws && ws.readyState === WebSocket.OPEN) { ws.send(JSON.stringify(obj)); return true; }
  return false;
}

function sendOrQueue(obj) {
  if (wsSend(obj)) return;
  outbox.push(obj);
  showToast('📴 Not connected — it will be sent when the connection is back', 'info', 3000);
}

function flushOutbox() { while (outbox.length && wsSend(outbox[0])) outbox.shift(); }

function setConnectionStatus(ok) {
  $('connection-status').classList.toggle('ok', ok);
  $('connection-status').title = ok ? 'Connected' : 'Disconnected — reconnecting…';
}

function connectWebSocket() {
  if (ws && ws.readyState <= WebSocket.OPEN) return;
  clearTimeout(reconnectTimer);
  const sock = new WebSocket(typeof withPinToken === 'function' ? withPinToken(WS_URL) : WS_URL);
  ws = sock;
  sock.onopen = () => {
    if (ws !== sock) return;
    setConnectionStatus(true);
    lastHeard = Date.now();
    reconnectDelay = 1000;
    const again = wsEverOpened;
    wsEverOpened = true;
    flushOutbox();
    if (again) catchUp();
  };
  sock.onclose = () => {
    if (ws !== sock) return;
    setConnectionStatus(false);
    reconnectTimer = setTimeout(connectWebSocket, reconnectDelay);
    reconnectDelay = Math.min(10000, reconnectDelay * 2);
  };
  sock.onmessage = e => {
    lastHeard = Date.now();
    let data;
    try { data = JSON.parse(e.data); } catch (err) { return; }
    handleEvent(data);
  };
}

function reconnectNow() {
  reconnectDelay = 1000;
  if (!ws || ws.readyState === WebSocket.CLOSED) connectWebSocket();
}

async function catchUp() {
  await fetchUsers();
  await fetchSidebar();
  if (!S.current) return;
  const id = S.current;
  const last = S.msgs.length ? S.msgs[S.msgs.length - 1].id : 0;
  try {
    const missed = await (await fetch(`/api/messages?target=${encodeURIComponent(id)}&after_id=${last}&limit=200`)).json();
    if (S.current !== id) return;
    const stick = nearBottom();
    missed.forEach(appendMessage);
    if (stick) scrollToBottom();
    markRead();
  } catch (e) { /* next reconnect tries again */ }
}

let usersRefreshTimer = null;
function scheduleRefresh() {  // names / photos / new people: refresh once, spread over devices
  clearTimeout(usersRefreshTimer);
  usersRefreshTimer = setTimeout(async () => { await fetchUsers(); await fetchSidebar(); }, 300 + Math.random() * 1200);
}

function handleEvent(d) {
  switch (d.type) {
    case 'pong': return;
    case 'presence': {
      const u = S.users.get(d.user);
      if (!u) return scheduleRefresh();
      u.online = d.online;
      renderList();
      if (S.current === d.user || S.current === 'group') renderHeader();
      return;
    }
    case 'typing': return onTyping(d);
    case 'new_msg': return onNewMessage(d.msg);
    case 'update_msg': return onUpdate(d.msg);
    case 'read_receipt':
      if (d.reader !== S.current) return;
      for (const m of S.msgs) if (m.from_mac === S.me.id && m.status !== 'read') { m.status = 'read'; updateMessage({ ...m }); }
      return;
    case 'system':
      if (d.action === 'update_users') scheduleRefresh();
      else if (d.action === 'update_groups') refreshGroups();
      return;
    case 'error':
      showToast('⚠️ ' + (d.message || 'Something went wrong'), 'error', 4000);
      if (d.code === 'not_member') refreshGroups();
  }
}

async function refreshGroups() {
  await fetchSidebar();
  if (S.current && S.current.startsWith('cg_') && !S.rows.has(S.current)) {
    showToast('👥 You were removed from this group', 'info', 3500);
    if (isPhone()) closeChat(); else openChat('group', { push: false });
  }
}

function onTyping(d) {
  const conv = d.target, from = d.from;
  S.typing[conv] = S.typing[conv] || {};
  clearTimeout(S.typing[conv][from]);
  if (d.is_typing) S.typing[conv][from] = setTimeout(() => { delete S.typing[conv][from]; refreshTyping(conv); }, 4000);
  else delete S.typing[conv][from];
  refreshTyping(conv);
}

function refreshTyping(conv) {
  const row = document.querySelector(`.chat-row[data-id="${CSS.escape(conv)}"]`);
  const r = S.rows.get(conv);
  if (row && r) row.replaceWith(rowEl(r));
  if (conv === S.current) renderHeader();
}

function onNewMessage(m) {
  const conv = conversationOf(m);
  const mine = m.from_mac === S.me.id;
  if (S.typing[conv] && S.typing[conv][m.from_mac]) { clearTimeout(S.typing[conv][m.from_mac]); delete S.typing[conv][m.from_mac]; }

  let r = S.rows.get(conv);
  if (!r) {
    const u = S.users.get(conv);
    r = { id: conv, title: u?.name || m.sender_name || 'Chat', type: isRoom(conv) ? (conv === 'group' ? 'global' : 'cgroup') : 'user', pinned: false, unread: 0, avatar: u?.avatar };
    S.rows.set(conv, r);
    scheduleRefresh();
  }
  r.preview = { text: previewText(m), ts: m.timestamp, mine, sender: m.sender_name, time_id: m.id };

  if (conv === S.current) {
    const stick = mine || nearBottom();
    appendMessage(m);
    if (stick) { scrollToBottom(!mine); S.newBelow = 0; }
    else if (!mine) S.newBelow++;
    updateJump();
    if (!mine && !document.hidden && nearBottom()) markRead();
    else if (!mine && document.hidden) r.unread = (r.unread || 0) + 1;
  } else if (!mine) {
    r.unread = (r.unread || 0) + 1;
  }
  renderList();
  if (conv === S.current) renderHeader();
}

function onUpdate(m) {
  const conv = conversationOf(m);
  if (conv === S.current) updateMessage(m);
  const r = S.rows.get(conv);
  if (r && r.preview && r.preview.time_id === m.id) {
    r.preview.text = previewText(m);
    renderList();
  }
}

// ── Wiring ────────────────────────────────────────────────────────
function bindUI() {
  // list
  $('user-search').addEventListener('input', renderList);
  $('chat-list-el').addEventListener('click', e => {
    const row = e.target.closest('.chat-row');
    if (!row) return;
    const r = S.rows.get(row.dataset.id);
    if (e.target.closest('[data-act="row-menu"]')) return rowMenu(r, e.target.closest('button'));
    openChat(row.dataset.id);
  });
  $('chat-list-el').addEventListener('keydown', e => {
    if ((e.key === 'Enter' || e.key === ' ') && e.target.classList.contains('chat-row')) { e.preventDefault(); openChat(e.target.dataset.id); }
  });
  $('chat-list-el').addEventListener('contextmenu', e => {
    const row = e.target.closest('.chat-row');
    if (!row) return;
    e.preventDefault();
    rowMenu(S.rows.get(row.dataset.id), { x: e.clientX, y: e.clientY });
  });
  $('me-btn').addEventListener('click', () => openProfile(false));
  $('search-open').addEventListener('click', openSearch);
  $('new-group-btn').addEventListener('click', openNewGroup);
  $('back-btn').addEventListener('click', () => (history.state && history.state.chat ? history.back() : closeChat()));
  window.addEventListener('popstate', () => { if (document.body.classList.contains('chat-open')) closeChat(); });
  $('chat-head-who').addEventListener('click', () => { if (S.current && S.current.startsWith('cg_')) openMembers(S.current); });

  // conversation
  const box = $('chat-container');
  box.addEventListener('scroll', () => {
    if (box.scrollTop < 80 && S.hasOlder) loadOlder();
    if (nearBottom()) { if (S.newBelow) { S.newBelow = 0; } markRead(); }
    updateJump();
  }, { passive: true });
  $('jump-btn').addEventListener('click', () => { S.newBelow = 0; scrollToBottom(true); updateJump(); });
  box.addEventListener('contextmenu', e => {
    const node = e.target.closest('.msg');
    if (!node || e.target.closest('a, audio')) return;
    const m = S.msgs.find(x => x.id === Number(node.dataset.id));
    if (!m || m.is_deleted) return;
    e.preventDefault();
    openMenu({ x: e.clientX, y: e.clientY }, msgMenu(m));
  });
  // long-press on touch screens (iOS has no contextmenu event)
  let pressTimer = null, pressXY = null, pressFired = false;
  box.addEventListener('touchstart', e => {
    const node = e.target.closest('.bubble');
    pressFired = false;
    if (!node || e.target.closest('a, audio')) return;
    const t = e.touches[0];
    pressXY = { x: t.clientX, y: t.clientY };
    pressTimer = setTimeout(() => {
      pressTimer = null;
      const m = S.msgs.find(x => x.id === Number(node.closest('.msg').dataset.id));
      if (m && !m.is_deleted) { pressFired = true; navigator.vibrate?.(15); openMenu(node, msgMenu(m)); }
    }, 450);
  }, { passive: true });
  const cancelPress = e => {
    if (e.type === 'touchend' && pressFired) { e.preventDefault(); pressFired = false; }  // no click on the menu scrim
    if (!pressTimer) return;
    if (e.type === 'touchmove') {
      const t = e.touches[0];
      if (Math.hypot(t.clientX - pressXY.x, t.clientY - pressXY.y) < 10) return;
    }
    clearTimeout(pressTimer); pressTimer = null;
  };
  box.addEventListener('touchmove', cancelPress, { passive: true });
  box.addEventListener('touchcancel', cancelPress, { passive: true });
  box.addEventListener('touchend', cancelPress, { passive: false });

  // composer
  const input = $('chat-input');
  input.addEventListener('input', onComposerInput);
  input.addEventListener('keydown', e => {
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing && !coarse()) { e.preventDefault(); submitComposer(); }
    else if (e.key === 'Escape' && S.editing) { e.preventDefault(); cancelEdit(); }
    else if (e.key === 'ArrowUp' && !input.value && !S.editing) {  // edit my last message
      const last = [...S.msgs].reverse().find(m => m.from_mac === S.me.id && m.msg_type === 'text' && !m.is_deleted);
      if (last) { e.preventDefault(); startEdit(last); }
    }
  });
  input.addEventListener('paste', e => {
    const files = [...(e.clipboardData?.files || [])];
    if (!files.length) return;
    e.preventDefault();
    queueFiles(files);
  });
  $('send-btn').addEventListener('click', submitComposer);
  $('mic-btn').addEventListener('click', toggleVoiceRecording);
  $('attach-btn').addEventListener('click', () => $('file-input').click());
  $('file-input').addEventListener('change', e => { queueFiles([...e.target.files]); e.target.value = ''; });
  $('edit-cancel').addEventListener('click', cancelEdit);
  $('recording-cancel').addEventListener('click', () => stopVoiceRecording(true));

  // drag & drop files onto the conversation
  const pane = $('chat-pane');
  let depth = 0;
  const hasFiles = e => [...(e.dataTransfer?.types || [])].includes('Files');
  pane.addEventListener('dragenter', e => { if (hasFiles(e) && S.current) { depth++; $('drop-hint').hidden = false; } });
  pane.addEventListener('dragleave', e => { if (hasFiles(e) && --depth <= 0) { depth = 0; $('drop-hint').hidden = true; } });
  pane.addEventListener('dragover', e => { if (hasFiles(e)) e.preventDefault(); });
  pane.addEventListener('drop', e => {
    if (!hasFiles(e)) return;
    e.preventDefault(); depth = 0; $('drop-hint').hidden = true;
    queueFiles([...e.dataTransfer.files]);
  });

  // menus, viewer, dialogs
  $('menu-scrim').addEventListener('click', () => { if (Date.now() - menuOpenedAt > 350) closeMenu(); });
  $('menu-scrim').addEventListener('contextmenu', e => { e.preventDefault(); closeMenu(); });
  $('lightbox-close').addEventListener('click', closeLightbox);
  $('lightbox').addEventListener('click', e => { if (e.target.id === 'lightbox' || e.target.id === 'lightbox-img') closeLightbox(); });
  document.querySelectorAll('.modal').forEach(mo => {
    mo.addEventListener('click', e => {
      if (e.target === mo && mo.id !== 'username-modal') closeModal(mo.id);
      if (e.target.closest('[data-close]')) closeModal(mo.id);
    });
  });
  $('modal-btn').addEventListener('click', saveProfile);
  $('username-input').addEventListener('keydown', e => { if (e.key === 'Enter') saveProfile(); });
  $('profile-cancel').addEventListener('click', () => closeModal('username-modal'));
  $('avatar-btn').addEventListener('click', () => $('avatar-input').click());
  $('avatar-input').addEventListener('change', uploadAvatar);
  $('group-save').addEventListener('click', createGroup);
  $('edit-group-save').addEventListener('click', saveGroup);
  $('confirm-ok').addEventListener('click', () => { closeModal('confirm-modal'); confirmAction && confirmAction(); confirmAction = null; });
  $('global-search-input').addEventListener('input', () => { clearTimeout(searchTimer); searchTimer = setTimeout(runSearch, 250); });

  document.addEventListener('keydown', e => {
    if (e.key !== 'Escape') return;
    if (!$('lightbox').hidden) return closeLightbox();
    if (!$('msg-menu').hidden) return closeMenu();
    const m = openModals().filter(x => x.id !== 'username-modal' || !profileFirstRun).pop();
    if (m) return closeModal(m.id);
    if (voice) return stopVoiceRecording(true);
  });

  // keep alive, wake up, read state
  setInterval(() => {
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    if (Date.now() - lastHeard > 60000) { ws.close(); return; }
    wsSend({ type: 'ping' });
  }, 25000);
  setInterval(() => { if (!document.hidden && S.started) renderList(); }, 60000);  // "Yesterday", times
  window.addEventListener('online', reconnectNow);
  document.addEventListener('visibilitychange', () => {
    if (document.hidden || !wsEverOpened) return;
    if (ws && ws.readyState === WebSocket.OPEN && Date.now() - lastHeard > 30000) ws.close();
    else reconnectNow();
    if (S.current && nearBottom()) { markRead(); const r = S.rows.get(S.current); if (r && r.unread) { r.unread = 0; renderList(); } }
  });
  window.addEventListener('resize', () => {
    if (!isPhone() && !S.current && S.started) openChat('group', { push: false });
  });
  initVoiceButton();
}

boot();
