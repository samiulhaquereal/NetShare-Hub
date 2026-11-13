import hashlib
import io
import tempfile
import threading
import unittest
import zipfile
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from netshare.infrastructure import airplay
from netshare.infrastructure.airplay import AirPlayEngine, ArgsFile
from netshare.infrastructure.downloads import DownloadError, Package, install_zip
from tests.helpers import AppTestCase


def _zip(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return buf.getvalue()


class _Quiet(SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass


class DownloadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.www = self.tmp / "www"
        self.www.mkdir()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), partial(_Quiet, directory=str(self.www)))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def _pkg(self, payload: bytes, sha: str | None = None) -> Package:
        (self.www / "pkg.zip").write_bytes(payload)
        url = f"http://127.0.0.1:{self.server.server_port}/pkg.zip"
        return Package("tool", "1.0", url, sha or hashlib.sha256(payload).hexdigest())

    def test_verified_zip_is_unpacked_and_progress_reported(self):
        payload = _zip({"tool.exe": b"MZ", "lib/a.dll": b"x"})
        seen = []
        dest = install_zip(self._pkg(payload), self.tmp / "out" / "1.0", lambda d, t: seen.append((d, t)))
        self.assertEqual((dest / "tool.exe").read_bytes(), b"MZ")
        self.assertTrue((dest / "lib" / "a.dll").exists())
        self.assertEqual(seen[-1], (len(payload), len(payload)))
        self.assertEqual(sorted(p.name for p in dest.parent.iterdir()), ["1.0"], "no temp files left")

    def test_checksum_mismatch_is_rejected_and_nothing_installed(self):
        pkg = self._pkg(_zip({"tool.exe": b"evil"}), sha="0" * 64)
        dest = self.tmp / "out" / "1.0"
        with self.assertRaises(DownloadError):
            install_zip(pkg, dest)
        self.assertFalse(dest.exists())
        self.assertEqual(list(dest.parent.iterdir()), [])

    def test_zip_slip_is_rejected(self):
        dest = self.tmp / "out" / "1.0"
        with self.assertRaises(DownloadError):
            install_zip(self._pkg(_zip({"../../escape.txt": b"x"})), dest)
        self.assertFalse((self.tmp / "escape.txt").exists())
        self.assertFalse(dest.exists())


class ArgsFileTests(unittest.TestCase):
    def test_user_arguments_are_backed_up_and_restored(self):
        path = Path(tempfile.mkdtemp()) / "leapbtw" / "arguments.txt"
        path.parent.mkdir(parents=True)
        path.write_text("-n MyOwnName -nh", encoding="utf-8")
        f = ArgsFile(path)
        f.apply()
        self.assertEqual(path.read_text(encoding="utf-8"), "-n NetShare-Hub -nh")
        f.apply()  # second start must not overwrite the backup with our own args
        f.restore()
        self.assertEqual(path.read_text(encoding="utf-8"), "-n MyOwnName -nh")

    def test_missing_file_is_created(self):
        path = Path(tempfile.mkdtemp()) / "sub" / "arguments.txt"
        f = ArgsFile(path)
        f.apply()
        self.assertEqual(path.read_text(encoding="utf-8"), "-n NetShare-Hub -nh")
        f.restore()  # nothing to restore: our file simply stays
        self.assertTrue(path.exists())


class LocateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.system = self.tmp / "system" / "uxplay-windows.exe"
        self.system.parent.mkdir()
        self.system.write_bytes(b"MZ")

    def _engine(self, **kw):
        return AirPlayEngine(self.tmp / "bin", (str(self.system),), auto_install=False, **kw)

    @mock.patch.object(airplay, "IS_WINDOWS", True)
    @mock.patch.object(airplay, "_EXE", "uxplay-windows.exe")
    def test_pinned_bundle_wins_over_system_install(self):
        eng = self._engine(args_file=ArgsFile(self.tmp / "args.txt"))
        self.assertEqual(eng.locate(), (self.system, "system"))
        eng.bundled_exe.parent.mkdir(parents=True)
        eng.bundled_exe.write_bytes(b"MZ")
        self.assertEqual(eng.locate(), (eng.bundled_exe, "bundled"))
        self.assertIn(airplay.UXPLAY_WINDOWS.version, str(eng.bundled_exe))

    def test_custom_path_is_authoritative(self):
        eng = self._engine(custom_path=self.tmp / "nope.exe")
        self.assertEqual(eng.locate(), (None, ""), "a wrong UXPLAY_PATH must not silently fall back")

    def test_status_shape(self):
        s = self._engine().status()
        for key in ("state", "installed", "running", "source", "progress", "bonjour", "name", "can_download"):
            self.assertIn(key, s)
        self.assertEqual(s["name"], "NetShare-Hub")


class AirPlayRouteTests(AppTestCase):
    def test_install_and_bonjour_are_host_only(self):
        with mock.patch.object(self.c.airplay, "install") as install,              mock.patch.object(self.c.airplay, "setup_bonjour") as bonjour:
            for path in ("/api/airplay/install", "/api/airplay/setup-bonjour"):
                self.assertEqual(self.lan().post(path).status_code, 403, path)
                self.assertEqual(self.tunnel().post(path).status_code, 403, path)
            install.assert_not_called()
            bonjour.assert_not_called()

    def test_status_tells_the_page_who_may_manage(self):
        self.assertTrue(self.host().get("/api/airplay/status").get_json()["can_manage"])
        self.assertFalse(self.lan().get("/api/airplay/status").get_json()["can_manage"])


class PinnedPackageTests(unittest.TestCase):
    def test_pin_is_exact(self):
        p = airplay.UXPLAY_WINDOWS
        self.assertTrue(p.url.startswith("https://github.com/leapbtw/uxplay-windows/releases/download/"))
        self.assertIn(p.version, p.url)
        self.assertRegex(p.sha256, r"^[0-9a-f]{64}$")


if __name__ == "__main__":
    unittest.main()
