/* NetShare Hub — clipboard page. Globals from common.js: showToast, escapeHtml; config: window.NSH */
let lastSentText = '', lastReceivedText = '', clipTimer = null;

function initClipboard() {
  const box = document.getElementById('clipboard-box'); if (!box) return;
  box.addEventListener('input', () => { clearTimeout(clipTimer); clipTimer = setTimeout(autoSendClipboard, 150); });
}

async function autoSendClipboard() {
  const text = document.getElementById('clipboard-box').value;
  if (text === lastSentText) return; lastSentText = text;
  await fetch('/clipboard', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ text }) });
}

function updateClipboardSmoothly(text) {
  if (text === lastReceivedText) return;
  lastReceivedText = text;
  const box = document.getElementById('clipboard-box');
  if (!box) return;
  if (document.activeElement === box) {
    const start = box.selectionStart, end = box.selectionEnd;
    if (box.value === text) return;
    box.value = text; lastSentText = text; box.setSelectionRange(start, end);
  } else {
    box.value = text;
  }
}

async function fetchClipboard() {
  try {
    const data = await (await fetch('/clipboard')).json();
    if (data.text !== lastReceivedText) {
      lastReceivedText = data.text;
      const box = document.getElementById('clipboard-box');
      if (box && document.activeElement !== box) box.value = data.text;
    }
  } catch (e) {}
}

function copyClipboard() {
  const text = document.getElementById('clipboard-box').value;
  if (!text) { showToast('Nothing to copy', 'info'); return; }
  copyText(text).then(() => showToast('✅ Copied!')).catch(() => showToast('❌ Copy failed', 'error'));
}

function copyText(text) {
  if (navigator.clipboard && window.isSecureContext) return navigator.clipboard.writeText(text);
  return new Promise((res, rej) => {
    const ta = document.createElement('textarea'); ta.value = text;
    ta.style.cssText = 'position:fixed;opacity:0;'; document.body.appendChild(ta); ta.select();
    try { document.execCommand('copy'); res(); } catch (e) { rej(e); } document.body.removeChild(ta);
  });
}

function clearClipboard() {
  document.getElementById('clipboard-box').value = ''; lastSentText = ''; autoSendClipboard();
  showToast('🗑 Clipboard cleared');
}

(function init() {
  checkPinStatus(); initClipboard(); fetchClipboard();
  const es = new EventSource('/events');
  es.onmessage = e => { if (e.data === 'clipboard') fetchClipboard(); };
})();
