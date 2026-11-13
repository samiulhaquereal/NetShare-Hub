/* NetShare Hub — adb page. Globals from common.js: showToast, escapeHtml; config: window.NSH */
let currentActiveMirrorSerial = "";
let adbStreamTimer = null, adbFrameCount = 0, adbLastFpsTime = performance.now(), adbCurrentFps = 60, adbCurrentLatency = 12;

async function fetchAdbDevices() {
  const container = document.getElementById("adb-device-list");
  const gridContainer = document.getElementById("multi-stream-grid");
  if (!container) return;
  container.innerHTML = '<div style="color:var(--muted); font-size:13px;">🔄 Scanning for ADB devices...</div>';

  try {
    const res = await fetch("/api/adb/devices");
    const data = await res.json();

    if (gridContainer) {
      gridContainer.innerHTML = `
        <div style="background: var(--card2); border: 1px solid var(--border); border-radius: 14px; overflow: hidden; position: relative;">
          <div style="padding: 12px 14px; background: rgba(0,0,0,0.3); display: flex; justify-content: space-between; align-items: center;">
            <span style="font-size: 13px; font-weight: 700; color: var(--accent2);">🖥️ Host PC Screen</span>
            <span style="font-size: 10px; background: rgba(16,185,129,0.2); color: var(--green); padding: 2px 8px; border-radius: 100px; font-weight: 700;">60 FPS LIVE</span>
          </div>
          <div style="height: 160px; background: #000; display: flex; align-items: center; justify-content: center; position: relative;">
            <span style="font-size: 42px; opacity: 0.5;">📺</span>
          </div>
          <div style="padding: 10px 14px;">
            <a href="/screen" style="display:block; text-align:center; text-decoration:none; padding: 8px; background: var(--accent); color: white; border-radius: 8px; font-weight: 600; font-size: 12px;">Watch Host PC Stream</a>
          </div>
        </div>
      `;
    }

    if (data.status === "ok" && data.devices.length > 0) {
      container.innerHTML = "";
      data.devices.forEach(dev => {
        const encodedSerial = encodeURIComponent(dev.serial);
        const el = document.createElement("div");
        el.style.cssText = "background:#1E1E38; border:1px solid var(--border); border-radius:12px; padding:12px 16px; display:flex; align-items:center; gap:12px; flex:1; min-width:280px; flex-wrap:wrap;";
        el.innerHTML = `
          <span style="font-size:24px;">📱</span>
          <div style="flex:1;">
            <div style="font-weight:700; font-size:14px; color:#FFF;">${dev.model}</div>
            <div style="font-size:11px; color:var(--muted);">${dev.connection} • ${dev.serial}</div>
          </div>
          <div style="display:flex; gap:6px; flex-wrap:wrap;">
            <button onclick="openAdbMirrorModal('${encodedSerial}', '${dev.model}')" style="background:var(--accent); color:white; border:none; padding:8px 12px; border-radius:8px; font-weight:600; font-size:12px; cursor:pointer;">🌐 Web Mirror</button>
            <button onclick="launchScrcpy('${encodedSerial}')" style="background:linear-gradient(135deg, #10B981, #059669); color:white; border:none; padding:8px 12px; border-radius:8px; font-weight:700; font-size:12px; cursor:pointer;">🚀 120 FPS Native</button>
          </div>
        `;
        container.appendChild(el);

        if (gridContainer) {
          const gridCard = document.createElement("div");
          gridCard.style.cssText = "background: var(--card2); border: 1px solid var(--border); border-radius: 14px; overflow: hidden; position: relative;";
          gridCard.innerHTML = `
            <div style="padding: 12px 14px; background: rgba(0,0,0,0.3); display: flex; justify-content: space-between; align-items: center;">
              <span style="font-size: 13px; font-weight: 700; color: #FFF;">📱 ${dev.model}</span>
              <span style="font-size: 10px; background: rgba(124,58,237,0.2); color: var(--accent2); padding: 2px 8px; border-radius: 100px; font-weight: 700;">ADB 60-120 FPS</span>
            </div>
            <div style="height: 160px; background: #000; display: flex; align-items: center; justify-content: center; position: relative; overflow:hidden;">
              <img src="/api/adb/screencap?serial=${encodedSerial}&t=${Date.now()}" style="max-height:100%; max-width:100%; object-fit:contain;" alt="${dev.model}" onerror="this.style.display='none'" />
            </div>
            <div style="padding: 10px 14px; display:flex; gap:8px;">
              <button onclick="openAdbMirrorModal('${encodedSerial}', '${dev.model}')" style="flex:1; padding: 8px; background: var(--accent); color: white; border: none; border-radius: 8px; font-weight: 700; font-size: 11px; cursor: pointer;">🌐 Web View</button>
              <button onclick="launchScrcpy('${encodedSerial}')" style="flex:1; padding: 8px; background: var(--green); color: black; border: none; border-radius: 8px; font-weight: 800; font-size: 11px; cursor: pointer;">🚀 120 FPS Native</button>
            </div>
          `;
          gridContainer.appendChild(gridCard);
        }
      });
    } else {
      container.innerHTML = '<div style="color:var(--muted); font-size:13px; padding:8px 0;">⚠️ No Android devices found via USB or Wi-Fi.</div>';
    }

    // Check iOS AirPlay active stream & render card in multi-stream grid
    try {
      const apRes = await fetch("/api/airplay/status");
      const apData = await apRes.json();
      if (apData.streaming && gridContainer) {
        const apCard = document.createElement("div");
        apCard.style.cssText = "background: var(--card2); border: 1px solid rgba(59, 130, 246, 0.4); border-radius: 14px; overflow: hidden; position: relative;";
        apCard.innerHTML = `
          <div style="padding: 12px 14px; background: rgba(0,0,0,0.4); display: flex; justify-content: space-between; align-items: center;">
            <span style="font-size: 13px; font-weight: 700; color: #60A5FA;">📱 ${apData.title || "iOS AirPlay Stream"}</span>
            <span style="font-size: 10px; background: rgba(59, 130, 246, 0.2); color: #60A5FA; padding: 2px 8px; border-radius: 100px; font-weight: 700;">● 60 FPS AIRPLAY</span>
          </div>
          <div style="height: 160px; background: #000; display: flex; align-items: center; justify-content: center; position: relative; overflow:hidden;">
            <img src="/api/airplay/screencap?t=${Date.now()}" style="max-height:100%; max-width:100%; object-fit:contain;" alt="AirPlay Screen" />
          </div>
          <div style="padding: 10px 14px; display:flex; gap:8px;">
            <button onclick="watchAirPlayStream()" style="flex:1; padding: 8px; background: var(--blue); color: white; border: none; border-radius: 8px; font-weight: 700; font-size: 11px; cursor: pointer;">🌐 Watch Stream on Web</button>
          </div>
        `;
        gridContainer.appendChild(apCard);
      }
    } catch(e) {}
  } catch (e) {
    container.innerHTML = '<div style="color:var(--red); font-size:13px;">Failed to scan ADB devices.</div>';
  }
}

async function watchAirPlayStream() {
  showToast("📱 Connecting Web View to iOS AirPlay Stream...", "info");
  try {
    const res = await fetch("/api/airplay/watch", { method: "POST" });
    const data = await res.json();
    if (data.status === "ok") {
      window.location.href = "/screen";
    } else {
      alert("⚠️ AirPlay Stream:\n\n" + (data.error || "Failed to switch source"));
    }
  } catch (e) { showToast("❌ Request error", "error"); }
}

function startAdbFramePump(serial) {
  stopAdbFramePump();
  const img = document.getElementById("adb-mirror-img");
  const fpsPill = document.getElementById("adb-fps-pill");
  const pingPill = document.getElementById("adb-ping-pill");
  if (!img) return;

  let active = true; adbFrameCount = 0; adbLastFpsTime = performance.now();

  async function pump() {
    if (!active || !currentActiveMirrorSerial) return;
    const t0 = performance.now();
    const tempImg = new Image();
    const rawSerial = decodeURIComponent(serial);
    const src = "/api/adb/screencap?serial=" + encodeURIComponent(rawSerial) + "&t=" + Date.now();

    tempImg.onload = () => {
      if (!active) return;
      const t1 = performance.now();
      adbCurrentLatency = Math.max(Math.round(t1 - t0), 1);
      img.src = tempImg.src; adbFrameCount++;
      const now = performance.now();
      if (now - adbLastFpsTime >= 1000) {
        adbCurrentFps = Math.round((adbFrameCount * 1000) / (now - adbLastFpsTime));
        adbFrameCount = 0; adbLastFpsTime = now;
        if (fpsPill) fpsPill.textContent = "⚡ " + adbCurrentFps + " FPS";
        if (pingPill) pingPill.textContent = "📡 " + adbCurrentLatency + " ms";
      }
      setTimeout(pump, 5);
    };
    tempImg.onerror = () => { if (active) setTimeout(pump, 200); };
    tempImg.src = src;
  }

  pump(); adbStreamTimer = { stop: () => { active = false; } };
}

function stopAdbFramePump() { if (adbStreamTimer && adbStreamTimer.stop) { adbStreamTimer.stop(); adbStreamTimer = null; } }

function openAdbMirrorModal(serial, model) {
  currentActiveMirrorSerial = serial;
  const modal = document.getElementById("adb-mirror-modal"), title = document.getElementById("adb-mirror-title");
  if (!modal) return; if (title) title.textContent = "📱 Live Mirror — " + (model || "Android Device");
  modal.style.display = "flex"; startAdbFramePump(serial);
}

function closeAdbMirrorModal(event) {
  if (event && event.target.id !== "adb-mirror-modal") return;
  stopAdbFramePump(); currentActiveMirrorSerial = "";
  const modal = document.getElementById("adb-mirror-modal"), img = document.getElementById("adb-mirror-img");
  if (modal) modal.style.display = "none"; if (img) img.src = "";
  if (document.fullscreenElement) document.exitFullscreen().catch(() => {});
}

function refreshAdbMirrorStream() { if (currentActiveMirrorSerial) startAdbFramePump(currentActiveMirrorSerial); }

function toggleAdbFullscreen() {
  const container = document.getElementById("adb-mirror-container"), exitBtn = document.getElementById("adb-fs-exit-btn");
  if (!container) return;
  if (!document.fullscreenElement) {
    if (container.requestFullscreen) container.requestFullscreen();
    else if (container.webkitRequestFullscreen) container.webkitRequestFullscreen();
    if (exitBtn) exitBtn.style.display = "block";
  } else {
    if (document.exitFullscreen) document.exitFullscreen();
    else if (document.webkitExitFullscreen) document.webkitExitFullscreen();
    if (exitBtn) exitBtn.style.display = "none";
  }
}

async function launchScrcpy(serial) {
  const isMobile = /mobile|android|iphone|ipad|tablet|ipod|blackberry|opera mini|iemobile/i.test(navigator.userAgent);
  if (isMobile) {
    alert("📱 Native 120 FPS scrcpy Mirroring opens a native window directly on the Host PC/Laptop screen.\n\nSince you are currently using a Mobile/Tablet browser, please tap '🌐 Web View' or '🌐 Web Mirror' to watch the live screen stream right inside your mobile browser!");
    return;
  }
  showToast("⚡ Checking PC for scrcpy... (auto-downloading if missing)", "info", 5000);
  try {
    const res = await fetch("/api/adb/launch-scrcpy", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ serial: decodeURIComponent(serial) }) });
    const data = await res.json();
    if (data.status === "ok") showToast("🚀 " + data.message, "success", 6000);
    else alert("⚠️ Native 60-120 FPS scrcpy Mirror:\n\n" + (data.error || "Failed to launch scrcpy"));
  } catch (e) { showToast("❌ Request error", "error"); }
}

async function pairAdbWifi() {
  const ip = document.getElementById("adb-pair-ip").value.trim(), code = document.getElementById("adb-pair-code").value.trim();
  if (!ip || !code) { showToast("Enter both Pairing IP:Port and 6-digit PIN code", "info"); return; }
  showToast("⚡ Pairing Wireless ADB...", "info", 5000);
  try {
    const res = await fetch("/api/adb/pair", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ ip, code }) });
    const data = await res.json();
    if (data.status === "ok") {
      showToast("✅ " + data.message, "success", 8000);
      const ipOnly = ip.split(":")[0], wifiIpInput = document.getElementById("adb-wifi-ip");
      if (wifiIpInput && ipOnly) { wifiIpInput.value = ipOnly + ":"; wifiIpInput.focus(); }
      alert("✅ Pairing Successful!\n\n🔑 Device paired! Now enter the main 'Wireless Debugging' IP:Port in Section 2 (Connect)!");
      fetchAdbDevices();
    } else alert("❌ Pairing Failed:\n\n" + (data.error || "Pairing failed"));
  } catch (e) { showToast("❌ Pairing request error", "error"); }
}

async function connectAdbWifi() {
  const ip = document.getElementById("adb-wifi-ip").value.trim();
  if (!ip) { showToast("Enter Connect IP:Port first", "info"); return; }
  showToast("🔌 Connecting Wireless ADB...", "info", 5000);
  try {
    const res = await fetch("/api/adb/connect-wifi", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ ip }) });
    const data = await res.json();
    if (data.status === "ok") { showToast("⚡ " + data.message, "success"); fetchAdbDevices(); }
    else alert("❌ Connection Failed:\n\n" + (data.error || "Connect failed"));
  } catch (e) { showToast("❌ ADB Connection error", "error"); }
}

let airplayPoll = null;

function openAirPlayModal() {
  const modal = document.getElementById("airplay-modal");
  if (modal) modal.style.display = "flex";
  refreshAirPlayStatus();
}
function closeAirPlayModal(event) {
  if (event && event.target && !event.target.classList.contains('modal-overlay') && !event.target.classList.contains('modal-close-x')) return;
  const modal = document.getElementById("airplay-modal");
  if (modal) modal.style.display = "none";
  clearTimeout(airplayPoll);
}

function describeAirPlay(s) {
  const src = s.source === "bundled" ? `built-in v${s.version}` : s.source === "system" ? "installed copy" : s.source;
  if (s.state === "downloading") return ["info", `📥 Downloading AirPlay receiver… ${Math.round(s.progress * 100)}%`];
  if (s.state === "error") return ["error", `⚠️ ${s.error || "AirPlay receiver failed"}`];
  if (!s.installed) return ["warn", s.can_download
    ? "⚠️ AirPlay receiver is not installed yet (one-time ~110 MB download, verified)."
    : "⚠️ UxPlay is not installed. On the host run: sudo apt install uxplay avahi-daemon"];
  if (s.bonjour === false) return ["warn", "⚠️ Bonjour service is off — iPhones can't find this PC. Set it up once (admin)."];
  if (s.running) return ["ok", `🟢 Receiver is live as “${s.name}” (${src}).`];
  return ["warn", `⏸ Receiver is installed (${src}) but not running.`];
}

async function refreshAirPlayStatus() {
  clearTimeout(airplayPoll);
  const box = document.getElementById("airplay-status");
  const bar = document.getElementById("airplay-progress");
  const actions = document.getElementById("airplay-actions");
  if (!box) return;
  let s;
  try { s = await (await fetch("/api/airplay/status")).json(); }
  catch (e) { box.className = "airplay-status error"; box.textContent = "❌ Could not reach the host"; return; }

  const [tone, text] = describeAirPlay(s);
  box.className = `airplay-status ${tone}`;
  box.textContent = text;
  document.getElementById("airplay-name").textContent = s.name || "NetShare-Hub";

  bar.hidden = s.state !== "downloading";
  bar.firstElementChild.style.width = `${Math.round((s.progress || 0) * 100)}%`;

  const buttons = [];
  if (!s.can_manage && (!s.installed || s.bonjour === false))
    buttons.push(`<span class="airplay-hint">Open this page on the host PC to fix this.</span>`);
  else if (s.can_download && s.state !== "downloading" && (!s.installed || s.state === "error"))
    buttons.push(`<button class="modal-btn primary" onclick="airplayAction('install')">📥 Install AirPlay receiver</button>`);
  if (s.can_manage && s.installed && s.bonjour === false)
    buttons.push(`<button class="modal-btn primary" onclick="airplayAction('setup-bonjour')">🛡 Set up Bonjour</button>`);
  actions.innerHTML = buttons.join("");

  const modal = document.getElementById("airplay-modal");
  if (modal && modal.style.display === "flex")
    airplayPoll = setTimeout(refreshAirPlayStatus, s.state === "downloading" ? 700 : 4000);
}

async function airplayAction(kind) {
  if (kind === "setup-bonjour") showToast("🛡 Approve the Windows admin prompt on the host PC", "info");
  try {
    const res = await fetch(`/api/airplay/${kind}`, { method: "POST" });
    const data = await res.json();
    showToast(data.status === "ok" ? `✅ ${data.message}` : `⚠️ ${data.error}`, data.status === "ok" ? "success" : "error");
  } catch (e) { showToast("❌ Request error", "error"); }
  refreshAirPlayStatus();
}

(function init() { checkPinStatus(); fetchAdbDevices(); })();
