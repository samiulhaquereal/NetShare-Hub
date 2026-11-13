/* NetShare Hub — files page. Globals from common.js: showToast, escapeHtml */

// ── State ─────────────────────────────────────────────────────────
const state = {
  files: [],            // [{name, size, mtime, kind, icon, ext}] from /api/files
  folders: [],          // [{name, mtime}] sub-folders of the open folder
  path: '',             // open folder relative to the upload folder ('' = top level)
  free: null,           // bytes free on the host disk
  selected: new Set(),  // names — survives list refreshes
  fresh: new Set(),     // names that just appeared (highlighted once)
  loadedOnce: false,
  query: '',
  type: 'all',
  sort: 'mtime',
  dir: 'desc',
  visible: [],          // current filtered + sorted list
  lastChecked: -1,      // for shift-click range selection
  version: 0,           // server list version (enables delta updates)
  shown: 0,             // rows rendered so far (rendered in pages as you scroll)
};
const PAGE = 200;

const TYPE_FILTERS = [
  ['all', 'All'], ['image', '🖼 Images'], ['video', '🎬 Videos'], ['audio', '🎵 Audio'],
  ['doc', '📄 Docs'], ['archive', '📦 Archives'], ['other', '📎 Other'],
];
const typeGroup = kind => (kind === 'pdf' || kind === 'text' || kind === 'doc') ? 'doc' : kind;
const PREVIEWABLE = new Set(['image', 'video', 'audio', 'pdf', 'text']);
const LOW_SPACE = 2 * 1024 ** 3;

// ── Formatting ────────────────────────────────────────────────────
function formatBytes(bytes) {
  if (!bytes || bytes <= 0) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  const i = Math.min(units.length - 1, Math.floor(Math.log(bytes) / Math.log(1024)));
  const v = bytes / 1024 ** i;
  return (i === 0 ? v : v.toFixed(v < 10 ? 1 : 0)) + ' ' + units[i];
}

function relTime(sec) {
  const diff = Date.now() / 1000 - sec;
  if (diff < 45) return 'just now';
  if (diff < 3600) return Math.round(diff / 60) + ' min ago';
  if (diff < 86400) return Math.round(diff / 3600) + ' h ago';
  if (diff < 2 * 86400) return 'yesterday';
  const d = new Date(sec * 1000);
  const sameYear = d.getFullYear() === new Date().getFullYear();
  return d.toLocaleDateString(undefined, sameYear ? { day: 'numeric', month: 'short' } : { day: 'numeric', month: 'short', year: 'numeric' });
}
const absTime = sec => new Date(sec * 1000).toLocaleString();
// Names in state are relative to the open folder; the server wants the full relative path.
const pathOf = (name, base = state.path) => base ? `${base}/${name}` : name;
const encPath = p => p.split('/').map(encodeURIComponent).join('/');
const fileUrl = name => '/file-preview/' + encPath(pathOf(name));
const downloadUrl = name => '/download/' + encPath(pathOf(name));
const byName = name => state.files.find(f => f.name === name);

// ── Loading & rendering ───────────────────────────────────────────
let loading = null, reloadAfter = false;
async function loadFiles() {
  if (loading) { reloadAfter = true; return loading; }  // a change arrived mid-fetch: fetch again after
  const reqPath = state.path;
  loading = (async () => {
    try {
      // With a known version the server answers with just the changes (a few KB),
      // not the whole list — essential with hundreds of devices and huge folders.
      // Sub-folders are listed in full (the browser revalidates them with an ETag).
      const url = reqPath ? `/api/files?dir=${encodeURIComponent(reqPath)}`
        : state.version ? `/api/files?since=${state.version}` : '/api/files';
      const res = await fetch(url, { cache: 'no-cache' });
      if (reqPath !== state.path) return;  // user opened another folder meanwhile
      if (res.status === 404 && reqPath) {
        showToast('📁 That folder no longer exists on the host', 'error');
        return openFolder(reqPath.split('/').slice(0, -1).join('/'));
      }
      if (!res.ok) throw new Error(res.status);
      const data = await res.json();
      if (reqPath !== state.path) return;
      const free = Number(res.headers.get('X-Disk-Free'));
      if (Number.isFinite(free) && res.headers.get('X-Disk-Free') !== null) state.free = free;
      if (data.delta && data.version === state.version) { renderSummary(); return; }  // nothing changed
      const before = new Set(state.files.map(f => f.name));
      if (data.delta) {
        const gone = new Set(data.removed);
        const added = new Map(data.added.map(f => [f.name, f]));
        state.files = state.files.filter(f => !gone.has(f.name) && !added.has(f.name)).concat(data.added);
      } else {
        state.files = data.files;
        state.folders = data.folders || [];
      }
      state.version = data.version;
      if (res.headers.get('X-List-Behind')) scheduleRefresh(400, 600);  // shared snapshot was a bit old
      state.fresh = state.loadedOnce ? new Set(state.files.filter(f => !before.has(f.name)).map(f => f.name)) : new Set();
      state.loadedOnce = true;
      const names = new Set(state.files.map(f => f.name));
      for (const n of [...state.selected]) if (!names.has(n)) state.selected.delete(n);
      render();
    } catch (e) {
      if (!state.loadedOnce) document.getElementById('file-list-container').innerHTML = '<p class="flist-empty" style="color:var(--red)">Failed to load files</p>';
    } finally {
      loading = null;
      if (reqPath !== state.path) { reloadAfter = false; loadFiles(); }
      else if (reloadAfter) { reloadAfter = false; scheduleRefresh(300); }
    }
  })();
  return loading;
}

// ── Folders ───────────────────────────────────────────────────────
// The open folder lives in the URL hash (#/Class%20Notes/Week%201), so the browser's
// Back button and shared links work.
const pathFromHash = () => {
  try { return decodeURIComponent(location.hash.replace(/^#\/?/, '')).replace(/^\/+|\/+$/g, ''); }
  catch (e) { return ''; }
};

function openFolder(path) {
  const hash = path ? '#/' + encPath(path) : '';
  if (location.hash !== hash) history.pushState(null, '', hash || location.pathname + location.search);
  applyPath(path);
}

function applyPath(path) {
  if (path === state.path) return;
  Object.assign(state, { path, files: [], folders: [], version: 0, loadedOnce: false, query: '', type: 'all', lastChecked: -1 });
  state.selected.clear();
  document.getElementById('file-search').value = '';
  renderCrumbs();
  document.getElementById('file-list-container').innerHTML = '<p class="flist-empty">Loading files...</p>';
  loadFiles();
}

function renderCrumbs() {
  const el = document.getElementById('crumbs');
  const parts = state.path ? state.path.split('/') : [];
  if (!parts.length && !state.folders.length) { el.hidden = true; return; }
  el.hidden = false;
  const crumbs = [`<button type="button" data-path="" ${parts.length ? '' : 'aria-current="page"'}>🏠 All files</button>`];
  parts.forEach((part, i) => {
    const p = parts.slice(0, i + 1).join('/');
    const last = i === parts.length - 1;
    crumbs.push(`<span class="crumb-sep">›</span><button type="button" data-path="${escapeHtml(p)}" ${last ? 'aria-current="page"' : ''}>📁 ${escapeHtml(part)}</button>`);
  });
  const hint = parts.length ? `<span class="crumb-hint">Uploads go into this folder</span>` : '';
  el.innerHTML = `<nav class="crumb-trail" aria-label="Folder">${crumbs.join('')}</nav>${hint}`;
}

function visibleFolders() {
  if (state.type !== 'all') return [];
  const q = state.query.trim().toLowerCase();
  const list = state.folders.filter(f => !q || f.name.toLowerCase().includes(q));
  if (state.sort === 'mtime') list.sort((a, b) => (state.dir === 'asc' ? 1 : -1) * (a.mtime - b.mtime));
  else if (state.sort === 'name' && state.dir === 'desc') list.reverse();
  return list;
}

function folderRowHtml(f) {
  const name = escapeHtml(f.name), rel = relTime(f.mtime), abs = escapeHtml(absTime(f.mtime));
  return `<div class="frow folder-row" data-folder="${name}" title="Open ${name}">
    <span class="fcheck-spacer"></span>
    <div class="fname">
      <div class="file-thumb-wrap folder-thumb"><span class="file-type-badge">📁</span></div>
      <div class="file-name-info">
        <span class="file-name-text">${name}</span>
        <span class="file-ext-tag">FOLDER</span>
        <span class="fsub" title="${abs}">Folder · ${rel}</span>
      </div>
    </div>
    <span class="fdate" title="${abs}">${rel}</span>
    <span class="fsize">Folder</span>
    <div class="file-actions">
      <button type="button" class="action-btn open-btn" data-act="open" title="Open" aria-label="Open ${name}">➜</button>
      <button type="button" class="action-btn dl-btn" data-act="zip" title="Download folder as ZIP" aria-label="Download ${name} as ZIP">⬇</button>
    </div>
  </div>`;
}

function openFolderModal() {
  document.getElementById('folder-where').textContent = 'Inside: ' + (state.path ? state.path.split('/').join(' › ') : 'All files');
  document.getElementById('folder-error').textContent = '';
  const input = document.getElementById('folder-name');
  input.value = '';
  document.getElementById('folder-modal').classList.add('show');
  setTimeout(() => input.focus(), 50);
}
function closeFolderModal() { document.getElementById('folder-modal')?.classList.remove('show'); }

async function createFolder(e) {
  e.preventDefault();
  const name = document.getElementById('folder-name').value.trim();
  const err = document.getElementById('folder-error'), btn = document.getElementById('folder-create-btn');
  if (!name) { err.textContent = 'Type a folder name'; return; }
  const parent = state.path;
  btn.disabled = true;
  try {
    const res = await fetch('/api/folders', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ parent, name }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) { err.textContent = data.error || `Could not create the folder (HTTP ${res.status})`; return; }
    closeFolderModal();
    showToast(`📁 Folder "${data.path.split('/').pop()}" created`, 'success');
    if (state.path === parent) {
      state.folders = state.folders.filter(f => f.name !== data.path.split('/').pop())
        .concat({ name: data.path.split('/').pop(), mtime: Date.now() / 1000 });  // instant feedback
      render();
    }
    scheduleRefresh(200);
  } catch (ex) {
    err.textContent = 'Network error — try again';
  } finally {
    btn.disabled = false;
  }
}

let refreshTimer = null;
function scheduleRefresh(delay = 600, jitter = 0) {
  if (refreshTimer) return;  // one pending refresh is enough; it picks up everything
  refreshTimer = setTimeout(() => { refreshTimer = null; loadFiles(); }, delay + Math.random() * jitter);
}

function computeVisible() {
  const q = state.query.trim().toLowerCase();
  const dir = state.dir === 'asc' ? 1 : -1;
  const key = state.sort;
  const list = state.files.filter(f =>
    (state.type === 'all' || typeGroup(f.kind) === state.type) && (!q || f.name.toLowerCase().includes(q)));
  list.sort((a, b) => {
    const r = key === 'name' ? a.name.localeCompare(b.name, undefined, { numeric: true, sensitivity: 'base' }) : a[key] - b[key];
    return r * dir || b.mtime - a.mtime;
  });
  return list;
}

function thumbHtml(f) {
  if (f.kind === 'image')
    return `<img src="${fileUrl(f.name)}?thumb=1&v=${f.mtime}" class="file-thumb-img" alt="" loading="lazy" decoding="async" data-icon="${escapeHtml(f.icon)}">`;
  if (f.kind === 'video') return `<span class="file-type-badge">🎬</span><span class="play-overlay">▶</span>`;
  return `<span class="file-type-badge">${escapeHtml(f.icon)}</span>`;
}

function rowHtml(f, i) {
  const name = escapeHtml(f.name);
  const sel = state.selected.has(f.name);
  const size = formatBytes(f.size), rel = relTime(f.mtime), abs = escapeHtml(absTime(f.mtime));
  return `<div class="frow${sel ? ' selected' : ''}${state.fresh.has(f.name) ? ' fresh' : ''}" data-i="${i}">
    <input type="checkbox" class="fcheck" ${sel ? 'checked' : ''} aria-label="Select ${name}">
    <div class="fname">
      <div class="file-thumb-wrap${f.kind === 'video' ? ' video-thumb' : ''}" data-act="preview">${thumbHtml(f)}</div>
      <div class="file-name-info">
        <span class="file-name-text" data-act="preview" title="${name}">${name}</span>
        <span class="file-ext-tag">${escapeHtml(f.ext)}</span>
        <span class="fsub" title="${abs}">${size} · ${rel}</span>
      </div>
    </div>
    <span class="fdate" title="${abs}">${rel}</span>
    <span class="fsize">${size}</span>
    <div class="file-actions">
      <button type="button" class="action-btn prev-btn" data-act="preview" title="Preview" aria-label="Preview ${name}">👁</button>
      <a class="action-btn dl-btn" href="${downloadUrl(f.name)}" download title="Download" aria-label="Download ${name}">⬇</a>
      <button type="button" class="action-btn del-btn" data-act="delete" title="Delete" aria-label="Delete ${name}">🗑</button>
    </div>
  </div>`;
}

function sortHeader(key, label) {
  const active = state.sort === key;
  const aria = active ? (state.dir === 'asc' ? 'ascending' : 'descending') : 'none';
  const arrow = active ? (state.dir === 'asc' ? '▲' : '▼') : '';
  return `<button type="button" data-sort="${key}" aria-sort="${aria}">${label} ${arrow}</button>`;
}

function render({ reset = false } = {}) {
  const box = document.getElementById('file-list-container');
  state.visible = computeVisible();
  // Only a page of rows is in the DOM; more are added as you scroll (200k files stay smooth).
  state.shown = Math.min(state.visible.length, reset ? PAGE : Math.max(PAGE, state.shown));
  if (reset) document.getElementById('file-list-wrap').scrollTop = 0;
  renderChips();
  renderSummary();
  renderCrumbs();
  const folders = visibleFolders();

  if (!state.files.length && !state.folders.length) {
    box.innerHTML = state.path
      ? '<p class="flist-empty">This folder is empty.<br>Files you drop now are uploaded into it.</p>'
      : '<p class="flist-empty">No files yet.<br>Drop files above — they appear on every connected device.</p>';
  } else if (!state.visible.length && !folders.length) {
    box.innerHTML = '<p class="flist-empty">No files match your search.</p>';
  } else {
    box.innerHTML = `<div class="flist-head">
        <input type="checkbox" id="select-all" aria-label="Select all shown files">
        ${sortHeader('name', 'File name')}${sortHeader('mtime', 'Modified')}${sortHeader('size', 'Size')}
        <span style="text-align:right">Actions</span>
      </div>` + folders.map(folderRowHtml).join('') + state.visible.slice(0, state.shown).map(rowHtml).join('') + moreHtml();
    observeMore();
  }
  state.fresh.clear();
  updateBulkActions();
}

function moreHtml() {
  const left = state.visible.length - state.shown;
  return left > 0 ? `<div id="flist-more" class="flist-more">Showing ${state.shown} of ${state.visible.length} — scroll for more</div>` : '';
}

let moreObserver = null;
function observeMore() {
  const sentinel = document.getElementById('flist-more');
  if (!sentinel) return;
  moreObserver ??= new IntersectionObserver(entries => {
    if (!entries.some(e => e.isIntersecting)) return;
    const start = state.shown;
    state.shown = Math.min(state.visible.length, start + PAGE);
    const old = document.getElementById('flist-more');
    old.insertAdjacentHTML('beforebegin', state.visible.slice(start, state.shown).map((f, k) => rowHtml(f, start + k)).join(''));
    moreObserver.unobserve(old);
    old.remove();
    document.getElementById('file-list-container').insertAdjacentHTML('beforeend', moreHtml());
    observeMore();
    updateBulkActions();
  }, { rootMargin: '600px' });  // viewport root: works whether the list or the page scrolls
  moreObserver.observe(sentinel);
}

function renderChips() {
  const counts = { all: state.files.length };
  for (const f of state.files) { const g = typeGroup(f.kind); counts[g] = (counts[g] || 0) + 1; }
  document.getElementById('type-chips').innerHTML = TYPE_FILTERS
    .filter(([key]) => key === 'all' || counts[key] || state.type === key)
    .map(([key, label]) => `<button type="button" class="type-chip" data-type="${key}" aria-pressed="${state.type === key}">${label}<span class="n">${counts[key] || 0}</span></button>`)
    .join('');
}

function renderSummary() {
  const total = state.files.reduce((s, f) => s + f.size, 0);
  const n = state.files.length, nf = state.folders.length;
  let html = (nf ? `${nf} folder${nf === 1 ? '' : 's'} · ` : '') + `${n} file${n === 1 ? '' : 's'} · ${formatBytes(total)}`;
  if (state.free != null) {
    const low = state.free < LOW_SPACE;
    html += ` · <span class="${low ? 'space-low' : ''}">${low ? '⚠️ ' : ''}${formatBytes(state.free)} free on host</span>`;
  }
  document.getElementById('file-summary').innerHTML = html;
  document.getElementById('file-count-badge').textContent = `${n} file${n === 1 ? '' : 's'}`;
}

function updateBulkActions() {
  const n = state.selected.size;
  const dl = document.getElementById('dl-multi-btn'), del = document.getElementById('del-multi-btn');
  dl.style.display = del.style.display = n ? 'inline-flex' : 'none';
  dl.textContent = `⬇ Download (${n})`;
  del.textContent = `🗑 Delete (${n})`;
  const all = document.getElementById('select-all');
  if (all) {
    const shown = state.visible.filter(f => state.selected.has(f.name)).length;
    all.checked = shown > 0 && shown === state.visible.length;
    all.indeterminate = shown > 0 && shown < state.visible.length;
  }
}

function setChecked(i, checked) {
  const f = state.visible[i];
  if (!f) return;
  checked ? state.selected.add(f.name) : state.selected.delete(f.name);
  const row = document.querySelector(`.frow[data-i="${i}"]`);
  if (row) { row.classList.toggle('selected', checked); row.querySelector('.fcheck').checked = checked; }
}

// ── List interactions (one delegated listener) ────────────────────
function initList() {
  const box = document.getElementById('file-list-container');

  box.addEventListener('click', e => {
    const sortBtn = e.target.closest('[data-sort]');
    if (sortBtn) {
      const key = sortBtn.dataset.sort;
      state.dir = state.sort === key ? (state.dir === 'asc' ? 'desc' : 'asc') : (key === 'name' ? 'asc' : 'desc');
      state.sort = key;
      document.getElementById('sort-select').value = `${state.sort}:${state.dir}`;
      render({ reset: true });
      return;
    }
    if (e.target.id === 'select-all') {
      // Update the Set for every match, but touch only the rows actually in the DOM.
      const on = e.target.checked;
      for (const f of state.visible) on ? state.selected.add(f.name) : state.selected.delete(f.name);
      for (const row of box.querySelectorAll('.frow[data-i]')) {
        row.classList.toggle('selected', on);
        row.querySelector('.fcheck').checked = on;
      }
      updateBulkActions();
      return;
    }
    const folderRow = e.target.closest('.folder-row');
    if (folderRow) {
      const target = pathOf(folderRow.dataset.folder);
      if (e.target.closest('[data-act]')?.dataset.act === 'zip') return downloadAsZip([target], true);
      return openFolder(target);
    }
    const row = e.target.closest('.frow');
    if (!row) return;
    const i = Number(row.dataset.i), f = state.visible[i];
    if (e.target.classList.contains('fcheck')) {
      if (e.shiftKey && state.lastChecked >= 0) {
        const [a, b] = [Math.min(i, state.lastChecked), Math.max(i, state.lastChecked)];
        for (let k = a; k <= b; k++) setChecked(k, e.target.checked);
      } else {
        setChecked(i, e.target.checked);
      }
      state.lastChecked = i;
      updateBulkActions();
      return;
    }
    if (e.target.closest('a')) return;  // download link does its own thing
    const act = e.target.closest('[data-act]')?.dataset.act;
    if (act === 'delete') return openDeleteModal([f.name]);
    if (act === 'preview' || window.matchMedia('(max-width: 700px)').matches) previewFile(f.name);
  });

  // broken thumbnail → fall back to the type icon (error doesn't bubble, so capture)
  box.addEventListener('error', e => {
    const img = e.target;
    if (img.tagName === 'IMG' && img.classList.contains('file-thumb-img')) {
      const span = document.createElement('span');
      span.className = 'file-type-badge';
      span.textContent = img.dataset.icon || '🖼';
      img.replaceWith(span);
    }
  }, true);

  document.getElementById('crumbs').addEventListener('click', e => {
    const btn = e.target.closest('[data-path]');
    if (btn) openFolder(btn.dataset.path);
  });
  document.getElementById('folder-form').addEventListener('submit', createFolder);
  document.getElementById('folder-modal').addEventListener('click', e => { if (e.target.id === 'folder-modal') closeFolderModal(); });
  window.addEventListener('popstate', () => applyPath(pathFromHash()));
  window.addEventListener('hashchange', () => applyPath(pathFromHash()));

  document.getElementById('type-chips').addEventListener('click', e => {
    const chip = e.target.closest('[data-type]');
    if (!chip) return;
    state.type = chip.dataset.type;
    render({ reset: true });
  });

  let searchTimer;
  document.getElementById('file-search').addEventListener('input', e => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => { state.query = e.target.value; render({ reset: true }); }, 150);
  });

  document.getElementById('sort-select').addEventListener('change', e => {
    [state.sort, state.dir] = e.target.value.split(':');
    render({ reset: true });
  });
}

// ── Preview ───────────────────────────────────────────────────────
let previewName = null;
const TEXT_PREVIEW_BYTES = 256 * 1024;

async function previewFile(name) {
  const f = byName(name);
  if (!f) return;
  previewName = name;
  const modal = document.getElementById('preview-modal');
  const body = document.getElementById('preview-body-content');
  document.getElementById('preview-filename-text').textContent = f.name;
  document.getElementById('preview-download-btn').href = downloadUrl(f.name);
  body.replaceChildren();
  body.classList.toggle('tall', f.kind === 'pdf' || f.kind === 'text');
  const url = fileUrl(f.name);

  if (f.kind === 'image') {
    const img = new Image();
    img.src = url; img.alt = f.name; img.decoding = 'async';
    body.append(img);
  } else if (f.kind === 'video' || f.kind === 'audio') {
    const el = document.createElement(f.kind);
    el.src = url; el.controls = true; el.autoplay = true; el.preload = 'metadata';
    if (f.kind === 'video') { el.playsInline = true; el.style.width = '100%'; } else { el.style.width = 'min(480px, 100%)'; }
    body.append(el);
  } else if (f.kind === 'pdf') {
    const frame = document.createElement('iframe');
    frame.src = url; frame.title = f.name;
    body.append(frame);
  } else if (f.kind === 'text') {
    const pre = document.createElement('pre');
    pre.textContent = 'Loading…';
    body.append(pre);
    try {
      const res = await fetch(url, { headers: { Range: `bytes=0-${TEXT_PREVIEW_BYTES - 1}` } });
      const text = await res.text();
      if (previewName !== name) return;
      pre.textContent = text;
      if (f.size > TEXT_PREVIEW_BYTES) {
        const note = document.createElement('div');
        note.className = 'preview-note';
        note.textContent = `Showing the first ${formatBytes(TEXT_PREVIEW_BYTES)} of ${formatBytes(f.size)} — download for the full file.`;
        body.append(note);
      }
    } catch (e) { pre.textContent = 'Could not load preview.'; }
  } else {
    const box = document.createElement('div');
    box.style.cssText = 'text-align:center;padding:40px 20px';
    box.innerHTML = `<div style="font-size:56px;margin-bottom:12px">${escapeHtml(f.icon)}</div>
      <div style="font-size:14px;color:var(--text2);margin-bottom:6px;word-break:break-all">${escapeHtml(f.name)}</div>
      <div style="font-size:12px;color:var(--muted)">${formatBytes(f.size)} · ${escapeHtml(absTime(f.mtime))}<br>No preview for this file type — download to open it.</div>`;
    body.append(box);
  }
  modal.classList.add('show');
}

function stepPreview(delta) {
  const list = state.visible.filter(f => PREVIEWABLE.has(f.kind));
  const i = list.findIndex(f => f.name === previewName);
  if (i < 0 || !list.length) return;
  previewFile(list[(i + delta + list.length) % list.length].name);
}

function closePreviewModal(e) {
  if (e && e.target && !e.target.classList.contains('modal-overlay') && !e.target.classList.contains('modal-close-x') && e.target.tagName !== 'BUTTON') return;
  const modal = document.getElementById('preview-modal');
  if (!modal) return;
  modal.classList.remove('show');
  document.getElementById('preview-body-content').replaceChildren();  // stops video/audio
  previewName = null;
}

// ── Delete ────────────────────────────────────────────────────────
let deleteTarget = null;

function openDeleteModal(names) {
  deleteTarget = names;
  const one = names.length === 1;
  document.getElementById('del-modal-title').textContent = one ? 'Delete File?' : `Delete ${names.length} Files?`;
  document.getElementById('del-modal-desc').textContent = one
    ? 'This file will be permanently deleted from NetShare Hub and cannot be recovered.'
    : `All ${names.length} selected files will be permanently deleted.`;
  const nameEl = document.getElementById('del-target-name');
  nameEl.textContent = one ? names[0] : `${names.slice(0, 2).join(', ')}${names.length > 2 ? ` and ${names.length - 2} more` : ''}`;
  nameEl.title = names.join(', ');
  document.getElementById('del-target-icon').textContent = one ? (byName(names[0])?.icon || '📄') : '📦';
  const btn = document.getElementById('del-confirm-btn');
  btn.innerHTML = `<span>🗑️ Delete${one ? ' File' : ` (${names.length})`}</span>`;
  btn.disabled = false;
  document.getElementById('delete-modal').classList.add('show');
}

function closeDeleteModal(e) {
  if (e && e.target && !e.target.classList.contains('modal-overlay') && !e.target.classList.contains('modal-close-x') && !e.target.classList.contains('del-btn-cancel')) return;
  document.getElementById('delete-modal')?.classList.remove('show');
  deleteTarget = null;
}

async function confirmDeleteAction() {
  if (!deleteTarget) return;
  const names = deleteTarget;
  const btn = document.getElementById('del-confirm-btn');
  btn.disabled = true;
  btn.innerHTML = '<span>⏳ Deleting...</span>';
  try {
    const res = await fetch('/delete-multiple', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ files: names.map(n => pathOf(n)) }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || res.status);
    const failed = new Set((data.failed || []).map(p => p.split('/').pop()));
    state.files = state.files.filter(f => !names.includes(f.name) || failed.has(f.name));  // instant feedback
    names.forEach(n => failed.has(n) || state.selected.delete(n));
    render();
    showToast(failed.size ? `⚠️ ${data.deleted} deleted, ${failed.size} failed` : `🗑️ Deleted ${data.deleted} file${data.deleted === 1 ? '' : 's'}`,
      failed.size ? 'error' : 'success');
  } catch (e) {
    showToast('❌ Delete failed', 'error');
  }
  document.getElementById('delete-modal').classList.remove('show');
  deleteTarget = null;
  scheduleRefresh(300);
}

function deleteSelected() {
  if (!state.selected.size) return showToast('Select files to delete first', 'info');
  openDeleteModal([...state.selected]);
}

// ── Download ──────────────────────────────────────────────────────
function openDownloadModal() {
  if (!state.selected.size) return showToast('Select files first', 'info');
  if (state.selected.size === 1) return downloadIndividually([...state.selected]);
  const bytes = [...state.selected].reduce((s, n) => s + (byName(n)?.size || 0), 0);
  document.querySelector('#download-modal .modal-sub').textContent =
    `${state.selected.size} files · ${formatBytes(bytes)} — choose how to download them`;
  document.getElementById('download-modal').classList.add('show');
}
function closeDownloadModal() { document.getElementById('download-modal')?.classList.remove('show'); }
function chooseZip() { closeDownloadModal(); downloadAsZip([...state.selected].map(n => pathOf(n))); }
function chooseIndividual() { closeDownloadModal(); downloadIndividually([...state.selected]); }

function downloadAsZip(paths, folder = false) {
  // A real form POST: the browser streams the ZIP straight to disk with its own progress UI.
  const form = document.getElementById('zip-form');
  form.replaceChildren(...paths.map(n => Object.assign(document.createElement('input'), { type: 'hidden', name: 'files', value: n })));
  form.submit();
  showToast(folder ? '📦 Folder ZIP download started — see your browser downloads' : '📦 ZIP download started — see your browser downloads', 'info', 3500);
}

function downloadIndividually(names) {
  names.forEach((n, i) => setTimeout(() => {
    const a = document.createElement('a');
    a.href = downloadUrl(n); a.download = n;
    document.body.appendChild(a); a.click(); a.remove();
  }, i * 400));
  showToast(`⬇ Downloading ${names.length} file${names.length === 1 ? '' : 's'}`);
}

// ── Upload engine ─────────────────────────────────────────────────
const MAX_CONCURRENT_UPLOADS = 3;
const MAX_ATTEMPTS = 4;
const STALL_MS = 45000;

const up = {
  queue: [], active: new Map(), failed: [], nextId: 0,
  total: 0, done: 0, failedCount: 0, bytesTotal: 0, bytesDone: 0,
  running: false, fadeTimer: null, speedSamples: [], fatal: null, gen: 0,
  offline: false, probeTimer: null, probeDelay: 1000,  // connection lost → wait, don't fail
};

async function filesFromDrop(dt) {
  // Entries must be read synchronously inside the drop event, before any await.
  const entries = [...(dt.items || [])].filter(it => it.kind === 'file').map(it => it.webkitGetAsEntry?.()).filter(Boolean);
  if (!entries.some(e => e.isDirectory)) return [...dt.files];
  const out = [];
  const readAll = reader => new Promise((res, rej) => reader.readEntries(res, rej));
  async function walk(entry) {
    if (entry.isFile) {
      out.push(await new Promise((res, rej) => entry.file(res, rej)));
    } else if (entry.isDirectory) {
      const reader = entry.createReader();
      for (let batch = await readAll(reader); batch.length; batch = await readAll(reader)) {
        for (const child of batch) await walk(child);
      }
    }
  }
  for (const e of entries) await walk(e);
  return out;
}

function initUpload() {
  const zone = document.getElementById('drop-zone'), input = document.getElementById('file-input');
  // Whole-page drop target; a depth counter avoids flicker when crossing child elements.
  let depth = 0;
  const hasFiles = e => [...(e.dataTransfer?.types || [])].includes('Files');
  document.addEventListener('dragenter', e => { if (hasFiles(e)) { depth++; zone.classList.add('drag-over'); } });
  document.addEventListener('dragleave', e => { if (hasFiles(e) && --depth <= 0) { depth = 0; zone.classList.remove('drag-over'); } });
  document.addEventListener('dragover', e => { if (hasFiles(e)) e.preventDefault(); });
  document.addEventListener('drop', async e => {
    if (!hasFiles(e)) return;
    e.preventDefault();
    depth = 0; zone.classList.remove('drag-over');
    try { uploadFiles(await filesFromDrop(e.dataTransfer)); }
    catch (err) { showToast('❌ Could not read the dropped folder', 'error'); }
  });
  zone.addEventListener('keydown', e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); input.click(); } });
  input.addEventListener('change', () => { uploadFiles(input.files); input.value = ''; });

  // Ctrl+V a screenshot (or copied files) anywhere outside a text field
  document.addEventListener('paste', e => {
    if (e.target.closest?.('input, textarea, [contenteditable]')) return;
    const files = [...(e.clipboardData?.files || [])];
    if (!files.length) return;
    e.preventDefault();
    const d = new Date(), pad = n => String(n).padStart(2, '0');  // local time, not UTC
    const stamp = `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}-${pad(d.getMinutes())}-${pad(d.getSeconds())}`;
    uploadFiles(files.map((f, i) => /^image\.\w+$/i.test(f.name)
      ? new File([f], `Screenshot ${stamp}${files.length > 1 ? `-${i + 1}` : ''}.${f.name.split('.').pop()}`, { type: f.type })
      : f));
  });

  window.addEventListener('beforeunload', e => {
    if (up.running) { e.preventDefault(); e.returnValue = ''; }
  });
  setInterval(watchdog, 5000);
  // Back online / phone unlocked: check right away instead of waiting for the next probe.
  const wake = () => { if (up.offline) { up.probeDelay = 1000; scheduleProbe(0); } };
  window.addEventListener('online', wake);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) wake(); });
}

function uploadFiles(fileList, folder = state.path) {
  const files = [...(fileList || [])].filter(f => f && f.size >= 0);
  if (!files.length) return;
  const tasks = files.map(file => ({
    id: ++up.nextId, file, name: file.name, folder, size: file.size, uploadId: newUploadId(),
    offset: 0, loaded: 0, attempts: 0, lastProgress: 0, needsCheck: false,
  }));
  const added = files.reduce((s, f) => s + f.size, 0);
  if (state.free != null && added > state.free) {
    showToast(`⚠️ These files (${formatBytes(added)}) are larger than the free space on the host (${formatBytes(state.free)})`, 'error', 5000);
  }
  enqueue(tasks);
}

function enqueue(tasks) {
  clearTimeout(up.fadeTimer);
  if (!up.running) {
    Object.assign(up, { total: 0, done: 0, failedCount: 0, bytesTotal: 0, bytesDone: 0, failed: [], speedSamples: [], fatal: null });
  }
  up.running = true;
  for (const t of tasks) { up.queue.push(t); up.total++; up.bytesTotal += t.size; }
  renderBatchPanel();
  pump();
}

const newUploadId = () => [...crypto.getRandomValues(new Uint8Array(16))].map(b => b.toString(16).padStart(2, '0')).join('');

function pump() {
  // While the connection is down nothing new starts: queued files wait instead of failing one by one.
  while (!up.offline && up.active.size < MAX_CONCURRENT_UPLOADS && up.queue.length) startUpload(up.queue.shift());
  updateBatchPanel();
}

function setCounted(task, bytes) {  // keep the batch byte counter in step with this task
  up.bytesDone += bytes - task.loaded;
  task.loaded = bytes;
}

async function startUpload(task) {
  task.lastProgress = Date.now();
  task.xhr = null;
  addTaskRow(task);
  up.active.set(task.id, task);
  const gen = up.gen;
  if (task.needsCheck) {
    // After a dropout: ask how much already arrived and continue from there.
    try {
      const ctl = new AbortController();
      const timer = setTimeout(() => ctl.abort(), 8000);
      const res = await fetch(`/upload-stream/${task.uploadId}`, { cache: 'no-store', signal: ctl.signal });
      clearTimeout(timer);
      if (gen !== up.gen) return;
      if (!res.ok) throw new Error(res.status);
      const data = await res.json();
      if (data.done) return finishTask(task, true);  // finished, only the reply was lost
      task.offset = Math.min(task.size, data.offset || 0);
      task.needsCheck = false;
    } catch (e) {
      if (gen === up.gen) connectionLost(task);
      return;
    }
  }
  sendBody(task);
}

function sendBody(task) {
  task.attempts++;
  task.lastProgress = Date.now();
  setCounted(task, task.offset);
  setTaskProgress(task, task.size ? task.offset / task.size : 0);
  const xhr = new XMLHttpRequest();
  task.xhr = xhr;

  xhr.upload.addEventListener('progress', e => {
    task.lastProgress = Date.now();
    setCounted(task, task.offset + e.loaded);
    up.speedSamples.push([Date.now(), up.bytesDone]);
    setTaskProgress(task, task.size ? task.loaded / task.size : 1);
    updateBatchPanel();
  });

  xhr.onload = () => {
    let data = {};
    try { data = JSON.parse(xhr.responseText); } catch (e) {}
    if (xhr.status === 200 || xhr.status === 201) return finishTask(task, true);
    if (xhr.status === 202) { task.offset = data.offset; return sendBody(task); }
    if (xhr.status === 409 && data.status === 'resume' && ++task.conflicts <= 5) {
      task.offset = data.offset;  // server holds a different amount: continue from its count
      return sendBody(task);
    }
    if (xhr.status === 409 && data.status === 'busy') {  // previous attempt still closing on the server
      task.needsCheck = true;
      return requeueLater(task, 2000);
    }
    const msg = data.error || `HTTP ${xhr.status}`;
    if (xhr.status === 507) {  // disk full: no point sending the rest
      up.fatal = msg;
      up.queue.forEach(t => { up.failed.push(t); up.failedCount++; });
      up.queue = [];
      return finishTask(task, false, msg);
    }
    if (xhr.status === 400 && msg === 'Upload interrupted') return connectionLost(task);
    const retryable = xhr.status >= 500 || xhr.status === 408 || xhr.status === 429;
    retryable ? retryTask(task, msg) : finishTask(task, false, msg);
  };
  xhr.onerror = () => connectionLost(task);
  xhr.onabort = () => { if (task.stalled) connectionLost(task); };

  xhr.open('PUT', '/upload-stream');
  xhr.setRequestHeader('X-File-Name', encodeURIComponent(task.name));
  if (task.folder) xhr.setRequestHeader('X-Folder', encodeURIComponent(task.folder));
  xhr.setRequestHeader('X-Upload-Id', task.uploadId);
  xhr.setRequestHeader('X-Upload-Offset', String(task.offset));
  xhr.setRequestHeader('X-File-Size', String(task.size));
  xhr.setRequestHeader('Content-Type', 'application/octet-stream');
  const token = localStorage.getItem('pin_token');
  if (token) xhr.setRequestHeader('X-Server-PIN-Token', token);
  xhr.timeout = 0;  // big files over Wi-Fi can take a long time; the watchdog handles real stalls
  xhr.send(task.offset ? task.file.slice(task.offset) : task.file);
}

function watchdog() {
  const now = Date.now();
  for (const task of up.active.values()) {
    if (task.xhr && now - task.lastProgress > STALL_MS) { task.stalled = true; task.xhr.abort(); }
  }
}

function requeueLater(task, delay) {
  up.active.delete(task.id);
  const gen = up.gen;
  setTimeout(() => { if (gen !== up.gen) return; up.queue.unshift(task); pump(); }, delay);
  updateBatchPanel();
}

// ── Connection loss: pause, wait for the network, resume where each file stopped ──
function connectionLost(task) {
  up.active.delete(task.id);
  task.stalled = false; task.xhr = null; task.needsCheck = true; task.conflicts = 0;
  setCounted(task, 0);
  up.queue.unshift(task);
  const row = document.getElementById(`up-task-${task.id}`);
  if (row) { row.classList.add('retrying'); row.querySelector('.upload-item-pct').textContent = '⏸ waiting'; }
  if (!up.offline) {
    up.offline = true;
    up.probeDelay = 1000;
    scheduleProbe();
  }
  updateBatchPanel();
}

function scheduleProbe(delay = up.probeDelay) {
  clearTimeout(up.probeTimer);
  const gen = up.gen;
  up.probeTimer = setTimeout(() => probeServer(gen), delay);
}

async function probeServer(gen) {
  if (gen !== up.gen || !up.offline) return;
  try {
    const ctl = new AbortController();
    const timer = setTimeout(() => ctl.abort(), 5000);
    await fetch('/api/auth/status', { cache: 'no-store', signal: ctl.signal });
    clearTimeout(timer);
    if (gen !== up.gen) return;
    up.offline = false;  // reachable again: carry on
    pump();
  } catch (e) {
    if (gen !== up.gen) return;
    up.probeDelay = Math.min(15000, up.probeDelay * 2);
    scheduleProbe();
    updateBatchPanel();
  }
}

function retryTask(task, reason) {  // server-side errors (5xx): limited attempts
  up.active.delete(task.id);
  setCounted(task, 0);
  task.stalled = false; task.needsCheck = true;
  if (task.attempts >= MAX_ATTEMPTS) return finishTask(task, false, reason, true);
  const row = document.getElementById(`up-task-${task.id}`);
  if (row) {
    row.classList.add('retrying');
    row.querySelector('.upload-item-pct').textContent = `↻ retry ${task.attempts}/${MAX_ATTEMPTS - 1}`;
  }
  requeueLater(task, 1000 * 2 ** (task.attempts - 1));
}

function finishTask(task, ok, reason = '', alreadyRemoved = false) {
  if (!alreadyRemoved) up.active.delete(task.id);
  const row = document.getElementById(`up-task-${task.id}`);
  if (ok) {
    up.done++;
    setCounted(task, task.size);
    if (row) { setTaskProgress(task, 1, '✅'); setTimeout(() => row.remove(), 700); }
    scheduleRefresh(800);
  } else {
    up.failedCount++;
    up.failed.push(task);
    setCounted(task, 0);
    if (row) {
      row.classList.remove('retrying');
      const pct = row.querySelector('.upload-item-pct');
      pct.textContent = '❌ ' + reason; pct.style.color = 'var(--red)';
      pct.title = reason;
    }
  }
  if (!up.active.size && !up.queue.length) batchFinished(); else pump();
}

function batchFinished() {
  up.running = false;
  updateBatchPanel();
  loadFiles();
  if (up.fatal) showToast('⚠️ ' + up.fatal, 'error', 6000);
  else if (!up.failedCount) showToast(`✅ ${up.done} file${up.done === 1 ? '' : 's'} uploaded`, 'success', 3000);
  else showToast(`⚠️ ${up.done} uploaded, ${up.failedCount} failed`, 'error', 4500);
  if (!up.failedCount) up.fadeTimer = setTimeout(() => {
    if (up.running) return;
    const c = document.getElementById('upload-queue');
    c.innerHTML = ''; c.classList.remove('active');
  }, 5000);
}

function retryFailed() {
  const tasks = up.failed.splice(0);
  if (!tasks.length) return;
  document.getElementById('active-items-list').replaceChildren();
  // Same upload ids: whatever already reached the host is kept, each file resumes.
  tasks.forEach(t => Object.assign(t, { attempts: 0, conflicts: 0, loaded: 0, needsCheck: true, stalled: false }));
  enqueue(tasks);
}

function cancelAllUploads() {
  up.gen++;  // drops pending retry timers and connection probes
  up.queue = [];
  clearTimeout(up.probeTimer);
  up.offline = false;
  for (const t of up.active.values()) { t.stalled = false; if (t.xhr) { t.xhr.onabort = null; t.xhr.abort(); } }
  up.active.clear();
  up.running = false;
  const c = document.getElementById('upload-queue');
  c.innerHTML = ''; c.classList.remove('active');
  showToast('🛑 Upload cancelled', 'info');
  loadFiles();
}

// ── Upload panel UI ───────────────────────────────────────────────
function renderBatchPanel() {
  const c = document.getElementById('upload-queue');
  c.classList.add('active');
  if (document.getElementById('batch-panel-wrap')) return;
  c.innerHTML = `
    <div class="batch-panel" id="batch-panel-wrap">
      <div class="batch-header">
        <div class="batch-title-wrap">
          <div class="batch-spinner" id="batch-spinner-el"></div>
          <div class="batch-title" id="batch-title-el">Preparing upload...</div>
        </div>
        <div style="display:flex;align-items:center;gap:10px;">
          <div class="batch-badges">
            <span class="batch-badge success" id="badge-completed">0 done</span>
            <span class="batch-badge queued" id="badge-queued">0 queued</span>
            <span class="batch-badge failed" id="badge-failed" style="display:none;">0 failed</span>
          </div>
          <button type="button" class="batch-cancel-btn" id="batch-retry-btn" style="display:none;color:var(--amber);border-color:rgba(245,158,11,.35);background:rgba(245,158,11,.12)" onclick="retryFailed()">↻ Retry failed</button>
          <button type="button" class="batch-cancel-btn" id="batch-cancel-btn" onclick="cancelAllUploads()">Cancel All</button>
        </div>
      </div>
      <div class="batch-progress-track"><div class="batch-progress-bar" id="batch-progress-bar"></div></div>
      <div class="batch-subtext">
        <span id="batch-bytes-text">0 B / 0 B</span>
        <span id="batch-pct-text">0%</span>
      </div>
      <div class="active-items-list" id="active-items-list"></div>
    </div>`;
}

function addTaskRow(task) {
  const list = document.getElementById('active-items-list');
  let row = document.getElementById(`up-task-${task.id}`);
  if (!row && list) {
    row = document.createElement('div');
    row.className = 'upload-item';
    row.id = `up-task-${task.id}`;
    row.innerHTML = `<div class="upload-item-top"><span class="upload-item-name"></span><span class="upload-item-pct">0%</span></div>
      <div class="progress-track"><div class="progress-bar"></div></div>`;
    const nameEl = row.querySelector('.upload-item-name');
    nameEl.textContent = `📄 ${task.folder ? task.folder + '/' : ''}${task.name} · ${formatBytes(task.size)}`;
    nameEl.title = task.name;
    list.appendChild(row);
  }
  row?.classList.remove('retrying');
}

function setTaskProgress(task, frac, label) {
  const row = document.getElementById(`up-task-${task.id}`);
  if (!row) return;
  const pct = Math.min(100, Math.round(frac * 100));
  row.querySelector('.progress-bar').style.width = pct + '%';
  const p = row.querySelector('.upload-item-pct');
  p.textContent = label || pct + '%';
  if (label) p.style.color = 'var(--green)';
}

function speedAndEta() {
  const now = Date.now();
  up.speedSamples = up.speedSamples.filter(([t]) => now - t < 4000);
  if (up.speedSamples.length < 2) return '';
  const [t0, b0] = up.speedSamples[0], [t1, b1] = up.speedSamples[up.speedSamples.length - 1];
  const bps = (b1 - b0) / Math.max(0.001, (t1 - t0) / 1000);
  if (bps <= 0) return '';
  const left = (up.bytesTotal - up.bytesDone) / bps;
  const eta = left < 60 ? `${Math.ceil(left)} s` : left < 3600 ? `${Math.ceil(left / 60)} min` : `${(left / 3600).toFixed(1)} h`;
  return ` · ${formatBytes(bps)}/s · ${eta} left`;
}

let panelFrame = 0;
function updateBatchPanel() {
  if (panelFrame) return;  // coalesce progress events to one paint per frame
  panelFrame = requestAnimationFrame(() => { panelFrame = 0; paintBatchPanel(); });
}

function paintBatchPanel() {
  const title = document.getElementById('batch-title-el');
  if (!title) return;
  const finished = up.done + up.failedCount;
  const busy = up.running;
  document.getElementById('batch-spinner-el').style.display = busy ? 'block' : 'none';
  document.getElementById('batch-cancel-btn').style.display = busy ? 'inline-block' : 'none';
  document.getElementById('batch-retry-btn').style.display = !busy && up.failed.length && !up.fatal ? 'inline-block' : 'none';
  const bar = document.getElementById('batch-progress-bar');
  const pctEl = document.getElementById('batch-pct-text');
  if (busy) {
    title.textContent = up.offline && !up.active.size
      ? `⏸ Connection lost — ${up.total - finished} file${up.total - finished === 1 ? '' : 's'} waiting. Keep this page open; it resumes by itself.`
      : `Uploading ${Math.min(finished + 1, up.total)} of ${up.total} (${up.active.size} active)`;
    const pct = up.bytesTotal ? Math.min(99, Math.round(up.bytesDone / up.bytesTotal * 100)) : Math.round(finished / up.total * 100);
    bar.style.width = pct + '%'; bar.style.background = '';
    pctEl.textContent = pct + '%';
  } else if (!up.failedCount) {
    title.textContent = `🎉 Uploaded ${up.done} file${up.done === 1 ? '' : 's'}`;
    bar.style.width = '100%'; bar.style.background = 'var(--green)';
    pctEl.textContent = '100%';
  } else {
    title.textContent = `${up.done} uploaded, ${up.failedCount} failed`;
    pctEl.textContent = 'Finished';
  }
  document.getElementById('badge-queued').textContent = `${up.queue.length} queued`;
  document.getElementById('badge-completed').textContent = `${up.done} done`;
  const failEl = document.getElementById('badge-failed');
  failEl.style.display = up.failedCount ? 'inline-block' : 'none';
  failEl.textContent = `${up.failedCount} failed`;
  document.getElementById('batch-bytes-text').textContent =
    `${formatBytes(Math.max(0, up.bytesDone))} / ${formatBytes(up.bytesTotal)}${busy ? speedAndEta() : ''}`;
}

// ── Boot ──────────────────────────────────────────────────────────
(function init() {
  checkPinStatus();
  initList();
  initUpload();
  state.path = pathFromHash();
  renderCrumbs();
  loadFiles();
  // Sub-folders are filled by hand on the host: re-check now and then (an ETag makes it a tiny 304).
  setInterval(() => { if (state.path && !document.hidden && !loading) loadFiles(); }, 10000);
  const es = new EventSource('/events');
  // Random spread so hundreds of devices don't all hit the server in the same millisecond.
  es.onmessage = e => { if (e.data === 'update') scheduleRefresh(up.running ? 1000 : 100, 1200); };
  setInterval(() => { if (!document.hidden && state.files.length) render(); }, 60000);  // keep "x min ago" fresh

  document.addEventListener('keydown', e => {
    if (e.key === 'Escape') { closeDeleteModal(); closeDownloadModal(); closePreviewModal(); closeFolderModal(); return; }
    if (previewName && (e.key === 'ArrowRight' || e.key === 'ArrowLeft')) stepPreview(e.key === 'ArrowRight' ? 1 : -1);
  });
})();
