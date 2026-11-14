/*
 * RetCore OnLAN — shared PIN lock (used by every page).
 *  - attaches the saved PIN token to every fetch() call
 *  - checkPinStatus(): shows/hides the lock overlay, returns true when unlocked
 *  - submitServerPin(): verifies the PIN and stores the token
 * Markup lives in templates/partials/pin_lock.html.
 */
(function () {
  const TOKEN_KEY = "pin_token";
  const HEADER = "X-Server-PIN-Token";
  const origFetch = window.fetch.bind(window);

  function savedToken() {
    try { return localStorage.getItem(TOKEN_KEY) || ""; } catch (_) { return ""; }
  }

  window._origFetch = origFetch;
  window.fetch = function (url, options) {
    options = options || {};
    const token = savedToken();
    if (token) {
      const headers = new Headers(options.headers || {});
      if (!headers.has(HEADER)) headers.set(HEADER, token);
      options.headers = headers;
    }
    return origFetch(url, options);
  };

  function modal() { return document.getElementById("pin-lock-modal"); }

  window.checkPinStatus = async function () {
    try {
      const token = savedToken();
      const res = await origFetch("/api/auth/status", { headers: token ? { [HEADER]: token } : {} });
      const data = await res.json();
      if (data.is_blocked) { window.location.reload(); return false; }
      const locked = data.pin_required && !data.is_authenticated;
      const m = modal();
      if (m) m.style.display = locked ? "flex" : "none";
      return !locked;
    } catch (e) {
      console.warn("Auth status check failed:", e);
      return true;
    }
  };

  window.submitServerPin = async function () {
    const input = document.getElementById("pin-input");
    const errorEl = document.getElementById("pin-error-msg");
    const pin = input ? input.value.trim() : "";
    if (!pin) return;
    const showError = (msg) => { if (errorEl) { errorEl.textContent = msg; errorEl.style.display = "block"; } };
    try {
      const res = await origFetch("/api/auth/verify", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ pin }),
      });
      const data = await res.json();
      if (res.ok && data.status === "ok") {
        if (data.token) {
          try { localStorage.setItem(TOKEN_KEY, data.token); } catch (_) {}
          document.cookie = TOKEN_KEY + "=" + data.token + "; path=/; max-age=864000; samesite=lax";
        }
        const m = modal();
        if (m) m.style.display = "none";
        window.location.reload();
      } else {
        showError(data.error || "Invalid Access PIN");
      }
    } catch (e) {
      showError("Failed to verify PIN. Please try again.");
    }
  };

  /** Append the PIN token to a WebSocket URL (browsers can't set WS headers). */
  window.withPinToken = function (url) {
    const token = savedToken();
    return token ? url + (url.includes("?") ? "&" : "?") + "pin_token=" + encodeURIComponent(token) : url;
  };
})();
