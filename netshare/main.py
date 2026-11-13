"""Process entry point: build services, start the web server and the host UI."""

from __future__ import annotations

import argparse
import io
import logging
import sys
import threading


def _fix_console_encoding() -> None:
    if sys.stdout and hasattr(sys.stdout, "buffer"):
        try:
            sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace", line_buffering=True)
        except (AttributeError, ValueError):
            pass


def self_check() -> int:
    """Build the full app against a throw-away data dir and exercise key routes.
    Catches packaging mistakes (missing templates/static, hidden imports) in the
    frozen executable without needing a display or network."""
    import shutil
    import tempfile
    from pathlib import Path

    from .config import load_settings
    from .container import build_container
    from .web.app import create_app

    tmp = Path(tempfile.mkdtemp(prefix="netshare-check-"))
    failures = []
    try:
        client = create_app(build_container(load_settings(app_dir=tmp))).test_client()
        for url in ("/", "/screen", "/messenger", "/files-page", "/clipboard-page", "/adb-hub",
                    "/api/files", "/api/auth/status", "/static/js/pin-lock.js", "/static/js/common.js", "/static/css/base.css",
                    "/static/css/pages/messenger.css", "/static/js/pages/messenger.js", "/sw.js",
                    "/manifest.json", "/qr.png"):
            status = client.get(url).status_code
            if status != 200:
                failures.append(f"{url} -> {status}")
        import importlib
        mods = ["netshare.ui.control_panel", "netshare.infrastructure.audio", "netshare.infrastructure.windows",
                "simplejpeg", "netshare.infrastructure.tls", "cryptography.x509"]
        if sys.platform == "win32":
            mods += ["dxcam", "comtypes"]
        for mod in mods:
            importlib.import_module(mod)  # GUI/audio/capture deps must be bundled even if not started here
        from .infrastructure.tls import ensure_certificate, server_context
        server_context(*ensure_certificate(tmp / "certs", ["localhost", "127.0.0.1"]))  # HTTPS works in the bundle
        from .infrastructure.capture import encode_jpeg
        import numpy as np
        if not (encode_jpeg(np.zeros((16, 16, 4), np.uint8), 70) or b"").startswith(bytes([0xFF, 0xD8])):
            failures.append("JPEG encoder broken")
    except Exception as exc:  # pragma: no cover - reported to CI
        failures.append(f"{type(exc).__name__}: {exc}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    for f in failures:
        print(f"[check] FAIL {f}")
    print("[check] OK" if not failures else f"[check] {len(failures)} failure(s)")
    return 1 if failures else 0


def _make_https_server(settings, app):
    """Second listener on HTTPS with a self-made certificate. Browsers allow the
    microphone (voice messages) only on https:// or localhost. Never fatal."""
    if not settings.https_port:
        return None
    try:
        from .infrastructure.network import get_local_ip
        from .infrastructure.tls import ensure_certificate, local_hosts, server_context
        from .web.server import make_wsgi_server
        cert, key = ensure_certificate(settings.cert_dir, local_hosts([get_local_ip()]))
        return make_wsgi_server("0.0.0.0", settings.https_port, app, tls=server_context(cert, key))
    except Exception as exc:  # missing cryptography, port taken, unreadable cert…
        print(f"[HTTPS] disabled: {exc}")
        return None


def main(argv: list[str] | None = None) -> None:
    from .infrastructure.winproc import install_silent_popen
    install_silent_popen()  # before anything spawns a subprocess
    _fix_console_encoding()

    parser = argparse.ArgumentParser(description="NetShare Hub")
    parser.add_argument("--public", action="store_true", help="Start with public (ngrok) access")
    parser.add_argument("--token", help="Ngrok authtoken (defaults to the saved one)")
    parser.add_argument("--headless", action="store_true", help="Run without the Tkinter control panel")
    parser.add_argument("--check", action="store_true",
                        help="Self-test (imports, bundled templates/static, routes) and exit 0/1 — used by CI")
    args = parser.parse_args(argv)

    if args.check:
        sys.exit(self_check())

    import dataclasses

    from .config import load_settings
    from .container import build_container
    from .infrastructure.network import find_free_port, get_local_ip
    from .web.app import create_app
    from .web.server import make_wsgi_server

    settings = load_settings()
    port = find_free_port(settings.http_port)
    if port is None:
        print(f"❌ ERROR: Ports {settings.http_port}-{settings.http_port + 19} are all in use.")
        sys.exit(1)
    if port != settings.http_port:
        print(f"⚠️  Port {settings.http_port} is used by another program — using port {port} instead.")
        settings = dataclasses.replace(settings, http_port=port)

    if settings.https_port:
        https_port = find_free_port(settings.https_port)
        if https_port is not None and https_port == port:  # never share the HTTP port
            https_port = find_free_port(port + 1)
        settings = dataclasses.replace(settings, https_port=https_port or 0)

    c = build_container(settings)
    app = create_app(c)
    https_server = _make_https_server(settings, app)
    if https_server is None and settings.https_port:  # pages must not offer a dead https link
        settings = dataclasses.replace(settings, https_port=0)
        c.settings = settings

    ip = get_local_ip()
    print("=" * 54)
    print("   NetShare Hub — Screen + Files + Clipboard over Wi-Fi")
    print("=" * 54)
    print(f"  Stream URL : http://{ip}:{port}")
    print(f"  WebSockets : ws://{ip}:{port}/ws/stream & /ws/chat")
    print(f"  Localhost  : http://localhost:{port}")
    if https_server:
        print(f"  Secure URL : https://{ip}:{settings.https_port}  (voice messages; accept the warning once)")
    print("=" * 54)

    if args.public:
        token = args.token or c.prefs.get("ngrok_token")
        if token:
            try:
                c.tunnel.start(token)
            except Exception as exc:
                print(f"[Ngrok] {exc}")
        else:
            print("[Ngrok] --public needs --token (or a saved token).")

    c.capture.start()  # idles until the first viewer connects
    c.audio.start()
    if settings.airplay_enabled:
        c.airplay.start_async()

    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    server = make_wsgi_server("0.0.0.0", port, app)
    if https_server:
        threading.Thread(target=https_server.serve_forever, daemon=True, name="HTTPSServer").start()
    if args.headless:
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            c.capture.stop()
            c.audio.stop()
            c.airplay.stop()
        return

    threading.Thread(target=server.serve_forever, daemon=True, name="HTTPServer").start()
    if sys.platform == "win32":
        # Own taskbar identity: otherwise `python server.py` is grouped under
        # python.exe and the taskbar shows the Python logo instead of our icon.
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("NetShareHub.ControlPanel")
        except (AttributeError, OSError):
            pass
    from .ui.control_panel import ControlPanel
    ControlPanel(c).mainloop()


if __name__ == "__main__":
    main()
