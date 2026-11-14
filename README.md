<p align="center"><img src="static/icons/logo-1024.png" alt="RetCore OnLAN logo" width="120"></p>

# RetCore OnLAN

**Share your screen, files, clipboard and chat with every device on your network — no internet, no app to install.**

Connect a phone, tablet, TV, or another laptop to your PC's screen over Wi-Fi — no app installation needed on the other device. Just open a URL in any browser.

---

## 🚀 Quick Start

### Option A — Double-click to run
1. Double-click **`install.bat`** (first time only — installs dependencies)
2. After that, double-click **`run.bat`** to launch

### Option B — Command line
```bash
pip install -r requirements.txt   # First time only
python server.py              # add --headless to run without the control panel
```

### Option C — Linux (Ubuntu / Debian)
```bash
./install.sh    # first time: apt packages + .venv
./run.sh        # start (./run.sh --headless for servers without a desktop)
```
Or download the prebuilt binary from the **Releases** page:
`tar -xzf RetCore-OnLAN-Linux.tar.gz && ./RetCore-OnLAN-Linux`

| Feature | Linux requirement |
|---------|-------------------|
| Screen + window capture, remote control | An **X11** session (on Ubuntu pick *"Ubuntu on Xorg"* at login — Wayland blocks screen capture by design) |
| System audio | `pulseaudio-utils` (works with PulseAudio **and** PipeWire) |
| Android mirroring | `sudo apt install adb scrcpy` |
| iOS AirPlay | `sudo apt install uxplay avahi-daemon` |

---

### 🍏 iOS screen mirroring (AirPlay)

iPhones/iPads mirror through **Control Center → Screen Mirroring → RetCore-OnLAN** — nothing to install on the phone.
On Windows the receiver ([UxPlay](https://github.com/FDH2/UxPlay), GPL-3.0, via the `uxplay-windows`
portable build) is a **pinned version, SHA-256 verified**, downloaded once into `bin/` — no winget,
no installer. An existing `uxplay-windows` install is reused if present. Discovery needs Apple's
Bonjour service; the Mobile page offers a one-time *Set up Bonjour* (admin prompt) when it is missing.

---

## 📱 Connecting a Device

1. Make sure both devices are on the **same Wi-Fi network**
2. Run RetCore OnLAN on your PC
3. **Scan the QR code** that appears in the control panel, OR
4. Open the URL shown (e.g. `http://192.168.1.5:5000`) on any device's browser
5. The stream starts automatically — it works full-screen!

---

## ⚙️ Features

| Feature | Details |
|---------|---------|
| 🖥️ Screen Streaming | Streams your PC screen in real-time (JPEG frames + system audio over WebSocket) |
| 📱 Works on Any Device | Any browser: Chrome, Safari, Firefox, Edge |
| 🎨 Beautiful Client UI | Dark theme, auto-hide HUD, fullscreen support |
| ✂️ Region Select | Select any portion of the screen to stream |
| 📊 Stats | Live FPS counter, client count, quality settings |
| 📋 QR Code | Scan with phone for instant connection |
| ⏸️ Pause/Resume | Pause the stream without disconnecting clients |
| 🔒 Wake Lock | Keeps the client device screen on |

---

## 🎛️ Control Panel

| Control | Action |
|---------|--------|
| **Full Screen** | Stream your entire primary monitor |
| **Select Region** | Draw a rectangle to stream only that area |
| **FPS Slider** | Adjust frame rate (5–60 FPS) |
| **Quality Slider** | Adjust JPEG quality (10–100%) |
| **Pause/Resume** | Temporarily freeze the stream |
| **Open in Browser** | Preview the stream on your own PC |

---

## 🔧 Requirements

- **Python 3.10+**
- **Windows 10/11** or **Linux** with an X11 desktop (tested on Ubuntu 24.04)
- Libraries: see `requirements.txt` (`flask`, `flask-sock`, `mss`, `opencv-python`, `numpy`, `Pillow`, `qrcode`, …)

---

## 🌐 Network Requirements

- Both devices must be on the **same local network** (Wi-Fi or LAN)
- When Windows Firewall asks for permission → click **"Allow"**
- Default port: **5000** (set `NETSHARE_PORT` in `.env` to change)
- Secure port: **5443** — `https://<PC-IP>:5443` (set `NETSHARE_HTTPS_PORT`, `0` turns it off)

### 🔒 Secure link (voice messages)

Browsers allow the microphone only on `https://` (or `localhost`). RetCore OnLAN therefore also
listens on HTTPS with a certificate it creates itself (`certs/`, renewed automatically when
the PC's IP changes). Because no public authority signed it, each device shows a
**one-time "not private" warning** — tap **Advanced → Proceed** (iPhone: **Show Details →
visit this website**). The messenger's 🎤 button offers this link automatically on plain
`http://` pages.

---

## 📐 Performance Tips

| Setting | Recommendation |
|---------|---------------|
| FPS | 24 FPS is smooth for most use cases |
| Quality | 70% gives great visuals at low bandwidth |
| Region | Select a smaller region if performance is poor |
| Wi-Fi | Use 5GHz Wi-Fi for better performance |

### Built-in performance features

| Area | What it does |
|------|--------------|
| Screen capture (Windows) | GPU capture via DXGI Desktop Duplication (`dxcam`), ~4 ms/frame; a static screen costs ~0 % CPU. Falls back to GDI/`mss` automatically (set `NETSHARE_CAPTURE=mss` to force it) |
| JPEG encoding | libjpeg-turbo via `simplejpeg` (~4× faster than OpenCV) |
| Streaming | Capture idles with no viewers; unchanged frames are not re-encoded or re-sent |
| Web pages | Self-hosted fonts (no internet needed), content-hashed asset URLs cached for a year, gzip for HTML/CSS/JS/JSON |

---

## 💡 Use Cases

- 📊 **Presenter mode** — show slides on a TV while viewing notes on your PC
- 🎮 **Game streaming** — show gameplay on a tablet nearby
- 📝 **Extended workspace** — move reference windows to a phone/tablet
- 📺 **Media display** — use an old tablet as a status board
- 🛠️ **Developer dashboard** — show monitoring/logs on a secondary device

---

## 🐛 Troubleshooting

**"Connection refused" on client device:**
- Make sure RetCore OnLAN is running on the PC
- Check Windows Firewall — allow Python through the firewall
- Verify both devices are on the same Wi-Fi network

**Stream is laggy:**
- Lower the FPS (try 15)
- Lower JPEG Quality (try 50%)
- Select a smaller region instead of full screen
- Use 5GHz Wi-Fi instead of 2.4GHz

**Black screen on client:**
- Click "Resume Stream" if it's paused
- Try refreshing the browser page

---

## 📁 Project Structure (clean architecture)

Dependencies only point downwards — the domain knows nothing about Flask, SQLite or Windows.

```
Screen Share/
├── server.py                 # Entry point (run.bat / PyInstaller) -> netshare.main
├── netshare/
│   ├── main.py               # Process startup: settings -> container -> web server + UI
│   ├── config.py             # Paths, ports, env vars (.env), install locations
│   ├── container.py          # Composition root: builds and wires every service
│   ├── domain/               # Pure rules: WS protocol bytes, chat identity
│   ├── services/             # Use-cases: stream fan-out, chat, clipboard, PIN/devices,
│   │                         #   identity, remote control, change notifications
│   ├── infrastructure/       # Adapters: screen/audio capture, SQLite, file store,
│   │                         #   ADB/scrcpy, AirPlay (UxPlay), ngrok, ARP, subprocess
│   ├── web/                  # Flask app factory, request context, route blueprints
│   │   └── routes/           #   pages, auth, admin, files, clipboard, stream, chat, adb
│   └── ui/                   # Tkinter host control panel + region selector
├── templates/
│   ├── base.html             # Shared layout: <head>, fonts, CSS/JS includes, PIN lock
│   ├── <page>.html           # Markup only — `{% extends "base.html" %}`
│   └── partials/             # PIN lock overlay, file table fragment
├── static/
│   ├── css/base.css          # Shared components (every page)
│   ├── css/hub.css           # Shared hub layout: reset, colors, back bar, toasts (not messenger)
│   ├── css/pages/<page>.css  # Page-specific styles
│   ├── js/common.js          # showToast, escapeHtml
│   ├── js/pin-lock.js        # PIN token + lock overlay
│   ├── js/pages/<page>.js    # Page logic (server config via window.NSH)
│   └── sw.js, manifest.json, icon.png
└── tests/                    # python -m unittest discover -s tests -t .
```

### Configuration (`.env`)

| Variable | Default | Purpose |
|----------|---------|---------|
| `SERVER_PIN` | *(empty)* | Require an access PIN from visitors |
| `NETSHARE_PORT` | `5000` | HTTP/WebSocket port |
| `NETSHARE_HTTPS_PORT` | `5443` | HTTPS port (self-signed; needed for voice messages); `0` = off |
| `CHAT_CIPHER_KEY` | built-in | Key for at-rest obfuscation of chat text |
| `NETSHARE_SECRET_KEY` | auto (`.netshare_secret`) | Signs identity cookies and PIN tokens |
| `AIRPLAY_ENABLED` / `AIRPLAY_AUTO_INSTALL` | `true` / `true` (Windows) | iOS AirPlay receiver; auto-download of the pinned UxPlay build |
| `UXPLAY_PATH` | *(auto)* | Use a specific UxPlay executable instead |

### Security model

- The **host PC** (requests from `localhost`) is always trusted; PIN, device blocking and
  "Go Public" are host-only.
- Visitors need the PIN when one is set (5 free attempts, then exponential lock-out).
- Every file name is confined to its folder (no `../` traversal).
- Chat text is rendered as text, never as HTML.

### Running tests

```bash
python -m unittest discover -s tests -t .
python server.py --check      # self-test: imports, templates, static files, routes
```

### CI / Releases (`.github/workflows/build.yml`)

| Event | What runs |
|-------|-----------|
| Pull request into `main` | Tests (Windows + Linux) → build both executables → smoke-test them (`--check`) |
| Merge / push to `main` | Same, then publishes the **`latest`** GitHub Release with both downloads |
| Manual (*Run workflow*) | Tests + builds |

Builds only start when tests pass; a newer push cancels an outdated run.

---

*Made with Python 🐍 | Flask + mss + OpenCV*
