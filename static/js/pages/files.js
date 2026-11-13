/* NetShare Hub — files page. Globals from common.js: showToast, escapeHtml; config: window.NSH */
let selectedFilesForModal = [];

function handlePreviewClick(el) {
  if (!el) return;
  const fn = el.getAttribute('data-filename');
  const ft = el.getAttribute('data-ftype');
  if (fn) previewMedia(fn, ft || 'other');
}

function handleDeleteClick(el) {
  if (!el) return;
  const fn = el.getAttribute('data-filename');
  if (fn) deleteFile(fn);
}

function previewMedia(filename, type) {
  const modal = document.getElementById('preview-modal');
  const title = document.getElementById('preview-filename-text');
  const body = document.getElementById('preview-body-content');
  const dlBtn = document.getElementById('preview-download-btn');
  if (!modal || !body) return;
  title.textContent = filename;
  dlBtn.href = '/download/' + encodeURIComponent(filename);
  const fileUrl = '/file-preview/' + encodeURIComponent(filename);
  if (type === 'image') body.innerHTML = `<img src="${fileUrl}" alt="${filename}" loading="lazy"/>`;
  else if (type === 'video') body.innerHTML = `<video src="${fileUrl}" controls autoplay style="width:100%;max-height:55vh;"></video>`;
  else if (type === 'audio') body.innerHTML = `<div style="text-align:center;width:100%;padding:30px;"><div style="font-size:48px;margin-bottom:16px;">🎵</div><audio src="${fileUrl}" controls autoplay style="width:100%;max-width:480px;"></audio></div>`;
  else body.innerHTML = `<div style="text-align:center;padding:40px;"><div style="font-size:56px;margin-bottom:12px;">📄</div><div style="font-size:15px;color:var(--text2);margin-bottom:8px;">${filename}</div></div>`;
  modal.classList.add('show');
}

function closePreviewModal(e) {
  if (e && e.target && !e.target.classList.contains('modal-overlay') && !e.target.classList.contains('modal-close-x') && e.target.tagName !== 'BUTTON') return;
  const modal = document.getElementById('preview-modal');
  if (modal) { modal.classList.remove('show'); const body = document.getElementById('preview-body-content'); if (body) body.innerHTML = ''; }
}

async function loadFiles() {
  try {
    const html = await (await fetch('/files')).text();
    document.getElementById('file-list-container').innerHTML = html;
    document.querySelectorAll('.dl-btn').forEach(btn => {
      const f = btn.getAttribute('href').replace('/download/', '');
      btn.href = '/download/' + encodeURIComponent(decodeURIComponent(f));
    });
    const rows = document.querySelectorAll('.file-row');
    document.getElementById('file-count-badge').textContent = rows.length + (rows.length === 1 ? ' file' : ' files');
  } catch (e) {
    document.getElementById('file-list-container').innerHTML = '<p style="color:var(--red);text-align:center;padding:50px 0">Failed to load files</p>';
  }
}

function filterFiles() {
  const q = document.getElementById('file-search').value.toLowerCase();
  document.querySelectorAll('.file-row').forEach(row => {
    const name = (row.querySelector('.file-name-text')?.textContent || '').toLowerCase();
    row.style.display = name.includes(q) ? '' : 'none';
  });
}

function updateBulkActions() {
  const n = document.querySelectorAll('.file-checkbox:checked').length;
  document.getElementById('dl-multi-btn').style.display = n ? 'inline-flex' : 'none';
  document.getElementById('del-multi-btn').style.display = n ? 'inline-flex' : 'none';
}

function toggleAll(src) {
  document.querySelectorAll('.file-checkbox').forEach(c => c.checked = src.checked);
  updateBulkActions();
}

let deleteTarget = null;

function getFileEmoji(filename) {
  const ext = (filename || '').split('.').pop().toLowerCase();
  const images = ['jpg', 'jpeg', 'png', 'gif', 'webp', 'svg', 'bmp'];
  const videos = ['mp4', 'mov', 'webm', 'mkv', 'avi'];
  const audios = ['mp3', 'wav', 'flac', 'm4a', 'aac'];
  if (images.includes(ext)) return '🖼️';
  if (videos.includes(ext)) return '🎬';
  if (audios.includes(ext)) return '🎵';
  if (ext === 'pdf') return '📄';
  if (['zip', 'rar', '7z', 'tar', 'gz'].includes(ext)) return '📦';
  return '📄';
}

function openDeleteModal(type, payload) {
  deleteTarget = { type, payload };
  const modal = document.getElementById('delete-modal');
  const titleEl = document.getElementById('del-modal-title');
  const descEl = document.getElementById('del-modal-desc');
  const nameEl = document.getElementById('del-target-name');
  const iconEl = document.getElementById('del-target-icon');
  const btnEl = document.getElementById('del-confirm-btn');

  if (!modal) return;

  if (type === 'single') {
    const filename = payload;
    titleEl.textContent = 'Delete File?';
    descEl.textContent = 'This file will be permanently deleted from NetShare Hub and cannot be recovered.';
    nameEl.textContent = filename;
    nameEl.title = filename;
    iconEl.textContent = getFileEmoji(filename);
    btnEl.innerHTML = '<span>🗑️ Delete File</span>';
  } else {
    const files = payload;
    titleEl.textContent = `Delete ${files.length} Files?`;
    descEl.textContent = `Are you sure you want to permanently delete all ${files.length} selected files?`;
    nameEl.textContent = files.length === 1 ? files[0] : `${files.length} files selected (${files.slice(0, 2).join(', ')}${files.length > 2 ? '...' : ''})`;
    nameEl.title = files.join(', ');
    iconEl.textContent = '📦';
    btnEl.innerHTML = `<span>🗑️ Delete (${files.length})</span>`;
  }

  btnEl.disabled = false;
  modal.classList.add('show');
}

function closeDeleteModal(e) {
  if (e && e.target && !e.target.classList.contains('modal-overlay') && !e.target.classList.contains('modal-close-x') && !e.target.classList.contains('del-btn-cancel')) return;
  const modal = document.getElementById('delete-modal');
  if (modal) modal.classList.remove('show');
  deleteTarget = null;
}

async function confirmDeleteAction() {
  if (!deleteTarget) return;
  const btnEl = document.getElementById('del-confirm-btn');
  if (btnEl) {
    btnEl.disabled = true;
    btnEl.innerHTML = '<span>⏳ Deleting...</span>';
  }

  const modal = document.getElementById('delete-modal');

  if (deleteTarget.type === 'single') {
    const filename = deleteTarget.payload;
    try {
      const r = await fetch('/delete/' + encodeURIComponent(filename), { method: 'DELETE' });
      if (r.ok) {
        showToast('🗑️ Deleted ' + filename, 'success');
        loadFiles();
      } else {
        showToast('❌ Delete failed', 'error');
      }
    } catch (e) {
      showToast('❌ Network error', 'error');
    }
  } else {
    const files = deleteTarget.payload;
    let errs = 0;
    for (const f of files) {
      try {
        const r = await fetch('/delete/' + encodeURIComponent(f), { method: 'DELETE' });
        if (!r.ok) errs++;
      } catch (e) {
        errs++;
      }
    }
    showToast(errs ? `⚠️ ${files.length - errs} deleted, ${errs} failed` : `🗑️ Deleted ${files.length} file(s)`, errs ? 'error' : 'success');
    loadFiles();
  }

  if (modal) modal.classList.remove('show');
  deleteTarget = null;
}

function deleteFile(filename) {
  openDeleteModal('single', filename);
}

function deleteSelected() {
  const sel = Array.from(document.querySelectorAll('.file-checkbox:checked')).map(c => c.value);
  if (!sel.length) {
    showToast('Select files to delete first', 'info');
    return;
  }
  openDeleteModal('multi', sel);
}

function openDownloadModal() {
  const sel = Array.from(document.querySelectorAll('.file-checkbox:checked')).map(c => c.value);
  if (!sel.length) { showToast('Select files first', 'info'); return; }
  selectedFilesForModal = sel; document.getElementById('download-modal').classList.add('show');
}
function closeDownloadModal() { document.getElementById('download-modal').classList.remove('show'); }
function chooseZip() { closeDownloadModal(); downloadAsZip(selectedFilesForModal); }
function chooseIndividual() { closeDownloadModal(); downloadIndividually(selectedFilesForModal); }

async function downloadAsZip(files) {
  showToast('📦 Preparing ZIP...', 'info');
  const res = await fetch('/download-multiple', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ files }) });
  const blob = await res.blob(); const url = URL.createObjectURL(blob);
  const a = document.createElement('a'); a.href = url; a.download = 'files.zip'; a.click(); URL.revokeObjectURL(url); showToast('✅ ZIP downloaded');
}

function downloadIndividually(files) {
  files.forEach((f, i) => setTimeout(() => {
    const a = document.createElement('a'); a.href = '/download/' + encodeURIComponent(f); a.download = f;
    document.body.appendChild(a); a.click(); document.body.removeChild(a);
  }, i * 350));
  showToast(`⬇ Downloading ${files.length} file(s)`);
}

// ── High Performance Concurrency Upload Engine (Supports 300+ files) ──
const MAX_CONCURRENT_UPLOADS = 3;
let uploadQueue = [];
let activeUploads = 0;
const activeXHRs = new Map();
let uploadTaskId = 0;

// Batch Metrics
let batchTotalCount = 0;
let batchCompletedCount = 0;
let batchFailedCount = 0;
let batchTotalBytes = 0;
let batchUploadedBytes = 0;
let isBatchActive = false;
let batchFadeTimer = null;
let debouncedReloadTimer = null;

function formatBytes(bytes) {
  if (!bytes || bytes <= 0) return '0 B';
  const k = 1024, dm = 1;
  const sizes = ['B', 'KB', 'MB', 'GB'];
  const i = Math.floor(Math.log(bytes) / Math.log(k));
  return parseFloat((bytes / Math.pow(k, i)).toFixed(dm)) + ' ' + sizes[i];
}

function initUpload() {
  const zone = document.getElementById('drop-zone'), input = document.getElementById('file-input');
  if (!zone || !input) return;
  zone.addEventListener('dragover', e => { e.preventDefault(); zone.classList.add('drag-over'); });
  zone.addEventListener('dragleave', () => zone.classList.remove('drag-over'));
  zone.addEventListener('drop', e => {
    e.preventDefault();
    zone.classList.remove('drag-over');
    if (e.dataTransfer && e.dataTransfer.files) uploadFiles(e.dataTransfer.files);
  });
  input.addEventListener('change', () => {
    if (input.files && input.files.length) uploadFiles(input.files);
    input.value = '';
  });
}

function uploadFiles(fileList) {
  const files = Array.from(fileList);
  if (!files.length) return;

  if (batchFadeTimer) { clearTimeout(batchFadeTimer); batchFadeTimer = null; }

  // If previous batch was completed and idle, reset counters
  if (!isBatchActive && uploadQueue.length === 0 && activeUploads === 0) {
    batchTotalCount = 0;
    batchCompletedCount = 0;
    batchFailedCount = 0;
    batchTotalBytes = 0;
    batchUploadedBytes = 0;
  }

  isBatchActive = true;

  for (const file of files) {
    const id = ++uploadTaskId;
    const task = {
      id: id,
      file: file,
      name: file.name,
      size: file.size || 0,
      loaded: 0,
      status: 'queued',
      retryCount: 0
    };
    uploadQueue.push(task);
    batchTotalCount++;
    batchTotalBytes += (file.size || 0);
  }

  renderBatchContainer();
  updateBatchUI();
  pumpUploadQueue();
}

function renderBatchContainer() {
  const container = document.getElementById('upload-queue');
  if (!container) return;
  container.classList.add('active');

  if (!document.getElementById('batch-panel-wrap')) {
    container.innerHTML = `
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
            <button type="button" class="batch-cancel-btn" id="batch-cancel-btn" onclick="cancelAllUploads()">Cancel All</button>
          </div>
        </div>
        <div class="batch-progress-track">
          <div class="batch-progress-bar" id="batch-progress-bar"></div>
        </div>
        <div class="batch-subtext">
          <span id="batch-bytes-text">0 B / 0 B</span>
          <span id="batch-pct-text">0%</span>
        </div>
        <div class="active-items-list" id="active-items-list"></div>
      </div>
    `;
  }
}

function updateBatchUI() {
  const titleEl = document.getElementById('batch-title-el');
  const queuedEl = document.getElementById('badge-queued');
  const compEl = document.getElementById('badge-completed');
  const failEl = document.getElementById('badge-failed');
  const barEl = document.getElementById('batch-progress-bar');
  const bytesEl = document.getElementById('batch-bytes-text');
  const pctEl = document.getElementById('batch-pct-text');
  const spinnerEl = document.getElementById('batch-spinner-el');
  const cancelBtn = document.getElementById('batch-cancel-btn');

  if (!titleEl) return;

  const remainingInQueue = uploadQueue.length;
  const inFlight = activeUploads;
  const finished = batchCompletedCount + batchFailedCount;

  if (remainingInQueue === 0 && inFlight === 0) {
    if (spinnerEl) spinnerEl.style.display = 'none';
    if (cancelBtn) cancelBtn.style.display = 'none';
    if (batchFailedCount === 0) {
      titleEl.textContent = `🎉 Successfully uploaded all ${batchCompletedCount} file${batchCompletedCount === 1 ? '' : 's'}!`;
      if (barEl) { barEl.style.width = '100%'; barEl.style.background = 'var(--green)'; }
      if (pctEl) pctEl.textContent = '100%';
    } else {
      titleEl.textContent = `Completed: ${batchCompletedCount} uploaded, ${batchFailedCount} failed`;
      if (pctEl) pctEl.textContent = 'Finished';
    }
  } else {
    if (spinnerEl) spinnerEl.style.display = 'block';
    if (cancelBtn) cancelBtn.style.display = 'inline-block';
    titleEl.textContent = `Uploading batch: ${finished + 1} of ${batchTotalCount} files (${inFlight} active)`;

    let overallPct = 0;
    if (batchTotalBytes > 0) {
      overallPct = Math.min(99, Math.round((batchUploadedBytes / batchTotalBytes) * 100));
    } else if (batchTotalCount > 0) {
      overallPct = Math.min(99, Math.round((finished / batchTotalCount) * 100));
    }

    if (barEl) barEl.style.width = overallPct + '%';
    if (pctEl) pctEl.textContent = overallPct + '%';
  }

  if (queuedEl) queuedEl.textContent = `${remainingInQueue} queued`;
  if (compEl) compEl.textContent = `${batchCompletedCount} done`;
  if (failEl) {
    if (batchFailedCount > 0) {
      failEl.style.display = 'inline-block';
      failEl.textContent = `${batchFailedCount} failed`;
    } else {
      failEl.style.display = 'none';
    }
  }

  if (bytesEl) {
    bytesEl.textContent = `${formatBytes(batchUploadedBytes)} / ${formatBytes(batchTotalBytes)}`;
  }
}

function pumpUploadQueue() {
  while (activeUploads < MAX_CONCURRENT_UPLOADS && uploadQueue.length > 0) {
    const task = uploadQueue.shift();
    activeUploads++;
    startSingleUpload(task);
  }
  updateBatchUI();
}

function startSingleUpload(task) {
  task.status = 'uploading';
  const activeList = document.getElementById('active-items-list');

  let itemEl = document.getElementById(`up-task-${task.id}`);
  if (!itemEl && activeList) {
    itemEl = document.createElement('div');
    itemEl.className = 'upload-item';
    itemEl.id = `up-task-${task.id}`;
    itemEl.innerHTML = `
      <div class="upload-item-top">
        <span class="upload-item-name" title="${task.name}">📄 ${task.name}</span>
        <span class="upload-item-pct" id="up-pct-${task.id}">0%</span>
      </div>
      <div class="progress-track">
        <div class="progress-bar" id="up-pb-${task.id}"></div>
      </div>
    `;
    activeList.appendChild(itemEl);
  }

  const xhr = new XMLHttpRequest();
  const fd = new FormData();
  fd.append('file', task.file);

  let lastLoadedForTask = 0;

  xhr.upload.addEventListener('progress', e => {
    if (e.lengthComputable) {
      const delta = e.loaded - lastLoadedForTask;
      lastLoadedForTask = e.loaded;
      batchUploadedBytes += delta;

      const pct = Math.min(100, Math.round((e.loaded / e.total) * 100));
      const pb = document.getElementById(`up-pb-${task.id}`);
      const pTxt = document.getElementById(`up-pct-${task.id}`);
      if (pb) pb.style.width = pct + '%';
      if (pTxt) pTxt.textContent = pct + '%';
      updateBatchUI();
    }
  });

  const onFinish = (isSuccess) => {
    activeXHRs.delete(task.id);
    activeUploads--;

    const remainingBytes = task.size - lastLoadedForTask;
    if (isSuccess && remainingBytes > 0) {
      batchUploadedBytes += remainingBytes;
    }

    const el = document.getElementById(`up-task-${task.id}`);
    if (el) {
      if (isSuccess) {
        const pTxt = document.getElementById(`up-pct-${task.id}`);
        if (pTxt) { pTxt.textContent = '✅'; pTxt.style.color = 'var(--green)'; }
        setTimeout(() => { if (el && el.parentNode) el.remove(); }, 600);
      } else {
        const pTxt = document.getElementById(`up-pct-${task.id}`);
        if (pTxt) { pTxt.textContent = '❌ Failed'; pTxt.style.color = 'var(--red)'; }
        setTimeout(() => { if (el && el.parentNode) el.remove(); }, 2000);
      }
    }

    if (isSuccess) {
      batchCompletedCount++;
      debouncedLoadFiles(1200);
    } else {
      batchFailedCount++;
    }

    checkBatchCompleted();
    pumpUploadQueue();
  };

  xhr.onload = () => {
    if (xhr.status === 200) {
      onFinish(true);
    } else {
      if (task.retryCount < 1) {
        task.retryCount++;
        activeXHRs.delete(task.id);
        activeUploads--;
        uploadQueue.unshift(task);
        const el = document.getElementById(`up-task-${task.id}`);
        if (el) el.remove();
        setTimeout(pumpUploadQueue, 800);
      } else {
        onFinish(false);
      }
    }
  };

  xhr.onerror = () => {
    if (task.retryCount < 1) {
      task.retryCount++;
      activeXHRs.delete(task.id);
      activeUploads--;
      uploadQueue.unshift(task);
      const el = document.getElementById(`up-task-${task.id}`);
      if (el) el.remove();
      setTimeout(pumpUploadQueue, 800);
    } else {
      onFinish(false);
    }
  };

  xhr.ontimeout = () => {
    onFinish(false);
  };

  xhr.open('POST', '/upload');
  const savedToken = localStorage.getItem("pin_token");
  if (savedToken) {
    xhr.setRequestHeader('X-Server-PIN-Token', savedToken);
  }
  xhr.timeout = 300000;
  activeXHRs.set(task.id, xhr);
  xhr.send(fd);
}

function checkBatchCompleted() {
  if (uploadQueue.length === 0 && activeUploads === 0 && isBatchActive) {
    isBatchActive = false;
    batchUploadedBytes = batchTotalBytes;
    updateBatchUI();

    loadFiles();

    if (batchFailedCount === 0) {
      showToast(`✅ All ${batchCompletedCount} files uploaded successfully!`, 'success', 3500);
    } else {
      showToast(`⚠️ ${batchCompletedCount} uploaded, ${batchFailedCount} failed`, 'error', 4000);
    }

    batchFadeTimer = setTimeout(() => {
      const container = document.getElementById('upload-queue');
      if (container && !isBatchActive && uploadQueue.length === 0 && activeUploads === 0) {
        container.innerHTML = '';
        container.classList.remove('active');
      }
    }, 5000);
  }
}

function cancelAllUploads() {
  activeXHRs.forEach(xhr => {
    try { xhr.abort(); } catch (e) {}
  });
  activeXHRs.clear();
  uploadQueue = [];
  activeUploads = 0;
  isBatchActive = false;

  const container = document.getElementById('upload-queue');
  if (container) {
    container.innerHTML = '';
    container.classList.remove('active');
  }
  showToast('🛑 Upload cancelled', 'info');
  loadFiles();
}

function debouncedLoadFiles(delay = 1200) {
  if (debouncedReloadTimer) return;
  debouncedReloadTimer = setTimeout(() => {
    debouncedReloadTimer = null;
    loadFiles();
  }, delay);
}

(function init() {
  checkPinStatus(); initUpload(); loadFiles();
  const es = new EventSource('/events');
  es.onmessage = e => {
    // Suppress SSE table reloads during an active batch upload to avoid DOM stutter
    if (e.data === 'update' && !isBatchActive) loadFiles();
  };

  document.addEventListener('keydown', e => {
    if (e.key === 'Escape') {
      closeDeleteModal();
      closeDownloadModal();
      closePreviewModal();
    }
  });
})();
