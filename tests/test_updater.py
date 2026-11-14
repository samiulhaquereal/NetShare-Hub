"""In-app updater: check, download + verify, unpack, install (file swap), relaunch, rollback."""

import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from netshare.infrastructure import updater as up
from netshare.infrastructure.updater import BuildInfo, Release, UpdateCancelled, UpdateError, Updater

ROOT = Path(__file__).resolve().parents[1]


class FakeResponse(io.BytesIO):
    def __init__(self, data: bytes):
        super().__init__(data)
        self.headers = {"Content-Length": str(len(data))}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


class FakeGitHub:
    """Serves releases/latest/download/<file> from a dict; records requested URLs."""

    def __init__(self, files: dict[str, bytes]):
        self.files, self.urls = files, []

    def __call__(self, url, timeout):
        self.urls.append(url)
        name = url.rsplit("/", 1)[-1]
        if name not in self.files:
            raise OSError("404")
        return FakeResponse(self.files[name])


def manifest(build=60, name="RetCore-OnLAN-Windows.exe", payload=b"new exe", platform="windows", **over):
    m = {"version": "2.2.0", "build": build, "commit": "abc1234", "built_at": "2026-11-14T10:00:00Z",
         "assets": {platform: {"name": name, "sha256": hashlib.sha256(payload).hexdigest(), "size": len(payload)}}}
    m.update(over)
    return json.dumps(m).encode()


class UpdaterTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.exe = self.dir / "app" / "RetCore-OnLAN-Windows.exe"
        self.exe.parent.mkdir()
        self.exe.write_bytes(b"old exe")

    def make(self, files, build=58, platform="windows"):
        gh = FakeGitHub(files)
        return Updater(self.dir / "updates", BuildInfo("2.1.0", build, "old"), self.exe, "me/repo", platform, gh), gh

    # check
    def test_reports_only_newer_builds(self):
        u, gh = self.make({"version.json": manifest(60)})
        rel = u.check()
        self.assertEqual((rel.build, rel.version, rel.asset), (60, "2.2.0", "RetCore-OnLAN-Windows.exe"))
        self.assertEqual(gh.urls[0], "https://github.com/me/repo/releases/latest/download/version.json")
        self.assertIsNone(self.make({"version.json": manifest(58)})[0].check(), "same build")
        self.assertIsNone(self.make({"version.json": manifest(50)})[0].check(), "older build")

    def test_bad_or_missing_manifest_is_a_clear_error(self):
        with self.assertRaises(UpdateError):
            self.make({})[0].check()
        with self.assertRaises(UpdateError):
            self.make({"version.json": b"not json"})[0].check()
        with self.assertRaises(UpdateError):  # no file for this platform
            self.make({"version.json": manifest(60, platform="linux")})[0].check()
        with self.assertRaises(UpdateError):  # path tricks in the file name
            self.make({"version.json": manifest(60, name="../evil.exe")})[0].check()

    def test_unsupported_when_not_an_official_build(self):
        self.assertIsNone(self.make({}, build=60)[0].unsupported_reason)
        self.assertIn("local build", self.make({}, build=0)[0].unsupported_reason)
        src = Updater(self.dir, BuildInfo("2.1.0", 60), exe=None)
        if not getattr(sys, "frozen", False):
            self.assertIn("source", src.unsupported_reason)

    # download
    def test_download_reports_progress_and_verifies_the_hash(self):
        payload = os.urandom(700_000)
        u, _ = self.make({"version.json": manifest(60, payload=payload), "RetCore-OnLAN-Windows.exe": payload})
        seen = []
        path = u.download(u.check(), lambda done, total: seen.append((done, total)))
        self.assertEqual(path.read_bytes(), payload)
        self.assertEqual(seen[-1], (len(payload), len(payload)))
        self.assertGreater(len(seen), 1, "several progress updates")

    def test_damaged_download_is_rejected_and_removed(self):
        u, _ = self.make({"version.json": manifest(60, payload=b"good"), "RetCore-OnLAN-Windows.exe": b"evil"})
        with self.assertRaises(UpdateError):
            u.download(u.check())
        self.assertEqual(list((self.dir / "updates").glob("*")), [], "no partial file left")

    def test_download_can_be_cancelled(self):
        payload = os.urandom(600_000)
        u, _ = self.make({"version.json": manifest(60, payload=payload), "RetCore-OnLAN-Windows.exe": payload})
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(UpdateCancelled):
            u.download(u.check(), cancel=cancel)

    # unpack (Linux release is a tar.gz with one binary)
    def test_linux_archive_unpacked_safely(self):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            data = b"\x7fELF new linux binary"
            info = tarfile.TarInfo("RetCore-OnLAN-Linux")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        archive = self.dir / "RetCore-OnLAN-Linux.tar.gz"
        archive.write_bytes(buf.getvalue())
        u = Updater(self.dir / "updates", BuildInfo("2.1.0", 58), self.exe, "me/repo", "linux")
        new = u.prepare(archive)
        self.assertEqual(new.read_bytes(), b"\x7fELF new linux binary")
        evil = io.BytesIO()
        with tarfile.open(fileobj=evil, mode="w:gz") as tar:
            info = tarfile.TarInfo("../../escape")
            info.size = 1
            tar.addfile(info, io.BytesIO(b"x"))
        archive.write_bytes(evil.getvalue())
        with self.assertRaises(UpdateError):
            u.prepare(archive)

    def test_self_test_must_pass(self):
        u, _ = self.make({})
        with mock.patch.object(up.subprocess, "run", return_value=subprocess.CompletedProcess([], 1)):
            with self.assertRaises(UpdateError):
                u.self_test(self.exe)
        with mock.patch.object(up.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as run:
            u.self_test(self.exe)
        self.assertEqual(run.call_args[0][0][1:], ["--check"])
        self.assertEqual(run.call_args[1]["env"]["PYINSTALLER_RESET_ENVIRONMENT"], "1")

    # install + relaunch
    def test_windows_style_install_keeps_the_old_file(self):
        new = self.dir / "downloaded.exe"
        new.write_bytes(b"new exe")
        u, _ = self.make({})
        u.install(new)
        self.assertEqual(self.exe.read_bytes(), b"new exe")
        self.assertEqual(self.exe.with_name(self.exe.name + ".old").read_bytes(), b"old exe")

    def test_linux_style_install_is_atomic_and_executable(self):
        new = self.dir / "new-bin"
        new.write_bytes(b"new bin")
        u = Updater(self.dir / "updates", BuildInfo("2.1.0", 58), self.exe, "me/repo", "linux")
        u.install(new)
        self.assertEqual(self.exe.read_bytes(), b"new bin")
        self.assertTrue(self.exe.with_name(self.exe.name + ".old").exists())

    def test_relaunch_passes_pid_and_clean_environment(self):
        u, _ = self.make({})
        with mock.patch.dict(os.environ, {"_MEIPASS2": "x", "_PYI_APPLICATION_HOME_DIR": "y"}), \
                mock.patch.object(up.subprocess, "Popen") as popen:
            u.relaunch(["--headless"])
        cmd = popen.call_args[0][0]
        self.assertEqual(cmd[:2], [str(self.exe), "--headless"])
        self.assertEqual(cmd[-2:], ["--after-update", str(os.getpid())])
        env = popen.call_args[1]["env"]
        self.assertNotIn("_MEIPASS2", env)
        self.assertNotIn("_PYI_APPLICATION_HOME_DIR", env)

    def test_rollback_restores_the_previous_version(self):
        old = self.exe.with_name(self.exe.name + ".old")
        old.write_bytes(b"previous")
        self.exe.write_bytes(b"broken new")
        with mock.patch.object(up.subprocess, "Popen") as popen:
            self.assertTrue(up.rollback(self.exe))
        self.assertEqual(self.exe.read_bytes(), b"previous")
        popen.assert_called_once()
        self.assertFalse(up.rollback(self.exe), "nothing left to roll back to")

    def test_wait_for_exit_of_a_finished_process(self):
        p = subprocess.Popen([sys.executable, "-c", "pass"])
        p.wait()
        self.assertTrue(up.wait_for_exit(p.pid, timeout=5))


class CiToolsTests(unittest.TestCase):
    def test_manifest_and_build_info_scripts(self):
        rel = Path(tempfile.mkdtemp())
        (rel / "RetCore-OnLAN-Windows.exe").write_bytes(b"win")
        (rel / "RetCore-OnLAN-Linux.tar.gz").write_bytes(b"lin")
        subprocess.run([sys.executable, str(ROOT / "tools" / "write_manifest.py"), str(rel), "61", "abcdef1"],
                       check=True, capture_output=True)
        m = json.loads((rel / "version.json").read_text())
        self.assertEqual(m["build"], 61)
        self.assertEqual(m["assets"]["windows"]["sha256"], hashlib.sha256(b"win").hexdigest())
        self.assertEqual(m["assets"]["linux"]["size"], 3)
        # the manifest the CI writes is exactly what the app accepts
        u = Updater(rel / "u", BuildInfo("2.1.0", 60), rel / "x.exe", "me/repo", "windows",
                    FakeGitHub({"version.json": (rel / "version.json").read_bytes()}))
        self.assertEqual(u.check().build, 61)

    def test_build_info_rejects_bad_commit(self):
        r = subprocess.run([sys.executable, str(ROOT / "tools" / "write_build_info.py"), "5", "not-a-sha; rm -rf"],
                           capture_output=True)
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse((ROOT / "netshare" / "_build_info.py").exists())
