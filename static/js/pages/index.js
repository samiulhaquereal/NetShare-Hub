/* RetCore OnLAN — index page. Globals from common.js: showToast, escapeHtml; config: window.NSH */
const HTTP_URL = window.NSH.httpUrl;

function copyHostUrl() {
  if (navigator.clipboard && window.isSecureContext) {
    navigator.clipboard.writeText(HTTP_URL).then(() => showToast('📋 URL copied!', 'success'));
  } else {
    const ta = document.createElement('textarea'); ta.value = HTTP_URL;
    ta.style.cssText = 'position:fixed;opacity:0;'; document.body.appendChild(ta); ta.select();
    try { document.execCommand('copy'); showToast('📋 URL copied!', 'success'); } catch (e) {} document.body.removeChild(ta);
  }
}

(function init() {
  const urlText = document.getElementById('server-url-text');
  if (urlText) urlText.textContent = HTTP_URL + ' (tap to copy)';
  checkPinStatus();
})();
