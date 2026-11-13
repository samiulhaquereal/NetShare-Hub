/* NetShare Hub — saved clipboards grid. Globals from common.js: showToast, escapeHtml. */

const grid = {
  clips: [],          // [{id, preview, chars, created, updated, live, shared}]
  query: '',
  known: null,        // ids seen before (to highlight new ones)
  deleteId: null,
};

function relTime(sec) {
  const diff = Date.now() / 1000 - sec;
  if (diff < 45) return 'just now';
  if (diff < 3600) return Math.round(diff / 60) + ' min ago';
  if (diff < 86400) return Math.round(diff / 3600) + ' h ago';
  if (diff < 2 * 86400) return 'yesterday';
  return new Date(sec * 1000).toLocaleDateString(undefined, { day: 'numeric', month: 'short' });
}
const count = n => n < 1000 ? `${n} chars` : `${(n / 1000).toFixed(n < 10000 ? 1 : 0)}K chars`;

function splitPreview(text) {
  const t = text.replace(/^\s+/, '');
  const nl = t.indexOf('\n');
  return nl < 0 ? [t, ''] : [t.slice(0, nl), t.slice(nl + 1)];
}

// ── Loading ───────────────────────────────────────────────────────
let loading = false, again = false;
async function loadClips() {
  if (loading) { again = true; return; }
  loading = true;
  try {
    const res = await fetch('/api/clips', { cache: 'no-store' });
    if (!res.ok) throw new Error(res.status);
    const data = await res.json();
    const before = grid.known;
    grid.clips = data.clips;
    grid.known = new Set(data.clips.map(c => c.id));
    render(before);
  } catch (e) {
    if (!grid.known) showToast('❌ Could not load clipboards', 'error');
  } finally {
    loading = false;
    if (again) { again = false; setTimeout(loadClips, 200); }
  }
}

let refreshTimer = null;
function scheduleRefresh() {
  clearTimeout(refreshTimer);
  refreshTimer = setTimeout(loadClips, 250 + Math.random() * 500);  // spread many devices out
}

// ── Rendering ─────────────────────────────────────────────────────
function cardHtml(c, fresh) {
  const [first, rest] = splitPreview(c.preview);
  const title = first.trim() ? escapeHtml(first.slice(0, 120)) : (c.shared ? 'Shared clipboard' : 'Empty clipboard');
  const badges = (c.shared ? '<span class="clip-badge" title="Also synced with screen-share viewers and the /clipboard API">📌 Shared</span>' : '')
    + (c.live ? `<span class="clip-live" title="${c.live} device${c.live === 1 ? '' : 's'} editing now">● ${c.live}</span>` : '');
  const del = '<button type="button" class="clip-act del" data-act="delete" title="Delete clipboard" aria-label="Delete clipboard">🗑</button>';
  return `<div class="clip-card${fresh ? ' fresh' : ''}" role="link" tabindex="0" data-id="${escapeHtml(c.id)}">
    <div class="clip-head"><div class="clip-title${first.trim() ? '' : ' empty'}">${title}</div>${badges}</div>
    <div class="clip-body">${escapeHtml(rest.slice(0, 400))}</div>
    <div class="clip-foot">
      <span class="grow" title="${escapeHtml(new Date(c.updated * 1000).toLocaleString())}">${relTime(c.updated)} · ${count(c.chars)}</span>
      <div class="clip-actions"><button type="button" class="clip-act" data-act="copy" title="Copy text" aria-label="Copy text">📋</button>${del}</div>
    </div>
  </div>`;
}

function render(before) {
  const q = grid.query.trim().toLowerCase();
  const shown = grid.clips.filter(c => !q || c.preview.toLowerCase().includes(q));
  const box = document.getElementById('clips-grid');
  const newCard = document.getElementById('clip-new');
  box.replaceChildren(newCard);
  box.insertAdjacentHTML('beforeend', shown.map(c => cardHtml(c, before && !before.has(c.id))).join(''));
  const n = grid.clips.length;
  document.getElementById('clip-count').textContent = `${n} saved`;
  const empty = document.getElementById('clips-empty');
  empty.hidden = !(q && !shown.length);
  empty.textContent = 'No clipboard matches your search.';
}

// ── Actions ───────────────────────────────────────────────────────
async function createClip() {
  const btn = document.getElementById('clip-new');
  btn.disabled = true;
  try {
    const res = await fetch('/api/clips', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
    if (!res.ok) throw new Error(res.status);
    const { id } = await res.json();
    location.href = '/clipboard-page/' + encodeURIComponent(id);
  } catch (e) {
    btn.disabled = false;
    showToast('❌ Could not create a clipboard', 'error');
  }
}

async function copyClip(id) {
  try {
    const res = await fetch('/api/clips/' + encodeURIComponent(id), { cache: 'no-store' });
    if (!res.ok) throw new Error(res.status);
    const { text } = await res.json();
    if (!text) return showToast('This clipboard is empty', 'info');
    if (navigator.clipboard && window.isSecureContext) await navigator.clipboard.writeText(text);
    else {
      const ta = Object.assign(document.createElement('textarea'), { value: text });
      ta.style.cssText = 'position:fixed;top:0;left:0;opacity:0';
      document.body.appendChild(ta); ta.focus(); ta.select(); ta.setSelectionRange(0, text.length);
      const ok = document.execCommand('copy');
      ta.remove();
      if (!ok) throw new Error('copy blocked');
    }
    showToast('✅ Copied!');
  } catch (e) {
    showToast('Couldn\'t copy here — open the clipboard to copy it', 'info', 3500);
  }
}

function askDelete(id) {
  const c = grid.clips.find(x => x.id === id);
  if (!c) return;
  grid.deleteId = id;
  document.getElementById('clip-del-preview').textContent = c.preview.trim().slice(0, 200) || 'Empty clipboard';
  document.getElementById('clip-del-confirm').disabled = false;
  document.getElementById('clip-delete-modal').classList.add('show');
  document.getElementById('clip-del-cancel').focus();
}

function closeDelete() {
  document.getElementById('clip-delete-modal').classList.remove('show');
  grid.deleteId = null;
}

async function confirmDelete() {
  const id = grid.deleteId;
  if (!id) return;
  document.getElementById('clip-del-confirm').disabled = true;
  try {
    const res = await fetch('/api/clips/' + encodeURIComponent(id), { method: 'DELETE' });
    if (!res.ok && res.status !== 404) throw new Error(res.status);
    grid.clips = grid.clips.filter(c => c.id !== id);  // instant feedback
    render(grid.known);
    showToast('🗑 Clipboard deleted');
  } catch (e) {
    showToast('❌ Delete failed', 'error');
  }
  closeDelete();
}

// ── Boot ──────────────────────────────────────────────────────────
(function init() {
  checkPinStatus();
  document.getElementById('clip-new').addEventListener('click', createClip);
  const box = document.getElementById('clips-grid');
  box.addEventListener('click', e => {
    const card = e.target.closest('.clip-card[data-id]');
    if (!card) return;
    const act = e.target.closest('[data-act]')?.dataset.act;
    if (act === 'copy') return copyClip(card.dataset.id);
    if (act === 'delete') return askDelete(card.dataset.id);
    location.href = '/clipboard-page/' + encodeURIComponent(card.dataset.id);
  });
  box.addEventListener('keydown', e => {
    const card = e.target.closest('.clip-card[data-id]');
    if (card && e.target === card && (e.key === 'Enter' || e.key === ' ')) { e.preventDefault(); card.click(); }
  });
  let t;
  document.getElementById('clip-search').addEventListener('input', e => {
    clearTimeout(t);
    t = setTimeout(() => { grid.query = e.target.value; render(grid.known); }, 120);
  });
  document.getElementById('clip-del-cancel').addEventListener('click', closeDelete);
  document.getElementById('clip-del-confirm').addEventListener('click', confirmDelete);
  document.getElementById('clip-delete-modal').addEventListener('click', e => { if (e.target.id === 'clip-delete-modal') closeDelete(); });
  document.addEventListener('keydown', e => { if (e.key === 'Escape') closeDelete(); });

  loadClips();
  // Live: another device creates, edits or deletes a clipboard → refresh the grid.
  const es = new EventSource('/events');
  es.onmessage = e => { if (e.data === 'clipboard') scheduleRefresh(); };
  setInterval(() => { if (!document.hidden) render(grid.known); }, 60000);  // keep "x min ago" fresh
  document.addEventListener('visibilitychange', () => { if (!document.hidden) scheduleRefresh(); });
  window.addEventListener('pageshow', e => { if (e.persisted) loadClips(); });  // back button from an editor
})();
