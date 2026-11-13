/*
 * NetShare Hub — helpers shared by every page (loaded after pin-lock.js).
 */

/** Show a transient toast in #toast-container. type: 'success' | 'info' | 'error'. */
function showToast(msg, type = 'success', duration = 2600) {
  const container = document.getElementById('toast-container');
  if (!container) return;
  const el = document.createElement('div');
  el.className = 'toast ' + type;
  el.textContent = msg;
  container.appendChild(el);
  setTimeout(() => el.remove(), duration);
}

/**
 * Escape untrusted text (usernames, messages, window titles, file names)
 * before it is interpolated into innerHTML.
 */
const HTML_ESCAPES = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, ch => HTML_ESCAPES[ch]);
}
