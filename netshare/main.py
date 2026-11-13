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
                    "/files", "/api/auth/status", "/static/js/pin-lock.js", "/static/style.css", "/sw.js",
                    "/manifest.json", "/qr.png"):
            status = client.get(url).status_code
            if status != 200:
                failures.append(f"{url} -> {status}")
        import importlib
        for mod in ("netshare.ui.control_panel", "netshare.infrastructure.audio", "netshare.infrastructure.windows"):
            importlib.import_module(mod)  # GUI/audio deps must be bundled even if not started here
    except Exception as exc:  # pragma: no cover - reported to CI
        failures.append(f"{type(exc).__name__}: {exc}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    for f in failures:
        print(f"[check] FAIL {f}")
    print("[check] OK" if not failures else f"[check] {len(failures)} failure(s)")
    return 1 if failures else 0


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

    from werkzeug.serving import make_server

    from .config import load_settings
    from .container import build_container
    from .infrastructure.network import get_local_ip, is_port_free
    from .web.app import create_app

    settings = load_settings()
    port = settings.http_port
    if not is_port_free(port):
        print(f"❌ ERROR: Port {port} is already in use! Close the other application and try again.")
        sys.exit(1)

    c = build_container(settings)
    app = create_app(c)

    ip = get_local_ip()
    print("=" * 54)
    print("   NetShare Hub — Screen + Files + Clipboard over Wi-Fi")
    print("=" * 54)
    print(f"  Stream URL : http://{ip}:{port}")
    print(f"  WebSockets : ws://{ip}:{port}/ws/stream & /ws/chat")
    print(f"  Localhost  : http://localhost:{port}")
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
    server = make_server("0.0.0.0", port, app, threaded=True)
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
    from .ui.control_panel import ControlPanel
    ControlPanel(c).mainloop()


if __name__ == "__main__":
    main()
