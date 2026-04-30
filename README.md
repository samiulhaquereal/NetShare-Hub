# 📺 ScreenShare

**Turn any device into a secondary screen using Python.**

Connect a phone, tablet, TV, or another laptop to your PC's screen over Wi-Fi — no app installation needed on the other device. Just open a URL in any browser.

---

## 🚀 Quick Start

### Option A — Double-click to run
1. Double-click **`install.bat`** (first time only — installs dependencies)
2. After that, double-click **`run.bat`** to launch

### Option B — Command line
```bash
pip install -r requirements.txt   # First time only
python server.py
```

---

## 📱 Connecting a Device

1. Make sure both devices are on the **same Wi-Fi network**
2. Run ScreenShare on your PC
3. **Scan the QR code** that appears in the control panel, OR
4. Open the URL shown (e.g. `http://192.168.1.5:5000`) on any device's browser
5. The stream starts automatically — it works full-screen!

---

## ⚙️ Features

| Feature | Details |
|---------|---------|
| 🖥️ Screen Streaming | Streams your PC screen in real-time via MJPEG |
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
- **Windows** (tested on Windows 10/11)
- Libraries: `flask`, `waitress`, `mss`, `opencv-python`, `numpy`, `Pillow`, `qrcode`

---

## 🌐 Network Requirements

- Both devices must be on the **same local network** (Wi-Fi or LAN)
- When Windows Firewall asks for permission → click **"Allow"**
- Default port: **5000** (changeable in `server.py`)

---

## 📐 Performance Tips

| Setting | Recommendation |
|---------|---------------|
| FPS | 24 FPS is smooth for most use cases |
| Quality | 70% gives great visuals at low bandwidth |
| Region | Select a smaller region if performance is poor |
| Wi-Fi | Use 5GHz Wi-Fi for better performance |

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
- Make sure ScreenShare is running on the PC
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

## 📁 File Structure

```
Screen Share/
├── server.py          # Main application (Flask server + tkinter UI)
├── capture.py         # Screen capture engine (mss + OpenCV)
├── templates/
│   └── index.html    # Browser client (beautiful dark UI)
├── requirements.txt   # Python dependencies
├── install.bat        # First-time setup
├── run.bat            # Quick launch
└── README.md          # This file
```

---

*Made with Python 🐍 | Flask + mss + OpenCV*
