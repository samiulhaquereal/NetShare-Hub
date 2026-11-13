import sys
import time
import unittest

from netshare.infrastructure.airplay import _is_airplay_title
from netshare.infrastructure.audio import AudioCapture, PulseMonitor
from netshare.infrastructure.network import parse_proc_arp
from netshare.infrastructure.windows import list_windows

PROC_ARP = """IP address       HW type     Flags       HW address            Mask     Device
192.168.0.1      0x1         0x2         a4:2b:b0:11:22:33     *        wlan0
192.168.0.50     0x1         0x0         00:00:00:00:00:00     *        wlan0
192.168.0.77     0x1         0x2         de:ad:be:ef:00:01     *        eth0
"""


class ArpTests(unittest.TestCase):
    def test_parses_linux_arp_table(self):
        self.assertEqual(parse_proc_arp(PROC_ARP, "192.168.0.77"), "DE:AD:BE:EF:00:01")

    def test_incomplete_and_unknown_entries(self):
        self.assertIsNone(parse_proc_arp(PROC_ARP, "192.168.0.50"))
        self.assertIsNone(parse_proc_arp(PROC_ARP, "10.0.0.1"))


class FakeBackend:
    sample_rate, channels, device_name = 48000, 2, "Fake [Monitor]"

    def __init__(self, chunks, fail_open=False):
        self.chunks, self.fail_open, self.closed = list(chunks), fail_open, False

    def open(self):
        if self.fail_open:
            raise RuntimeError("no sound server")

    def read(self):
        if not self.chunks:
            raise EOFError("done")
        return self.chunks.pop(0)

    def close(self):
        self.closed = True


class AudioCaptureTests(unittest.TestCase):
    def _run(self, backend):
        cap = AudioCapture(lambda: backend)
        got = []
        cap.on_chunk = got.append
        cap.start()
        deadline = time.time() + 2
        while cap.is_running and time.time() < deadline:
            time.sleep(0.01)
        return cap, got

    def test_publishes_all_chunks_and_reports_format(self):
        backend = FakeBackend([b"a", b"b", b"c"])
        cap, got = self._run(backend)
        self.assertEqual(got, [b"a", b"b", b"c"])
        self.assertTrue(cap.available)
        self.assertEqual((cap.sample_rate, cap.channels, cap.device_name), (48000, 2, "Fake [Monitor]"))
        self.assertTrue(backend.closed)

    def test_open_failure_disables_audio_cleanly(self):
        backend = FakeBackend([], fail_open=True)
        cap, got = self._run(backend)
        self.assertFalse(cap.available)
        self.assertFalse(cap.is_running)
        self.assertTrue(backend.closed)

    def test_parec_command(self):
        cmd = PulseMonitor(44100, 2).command()
        self.assertEqual(cmd[0], "parec")
        self.assertIn("--device=@DEFAULT_MONITOR@", cmd)
        self.assertIn("--format=s16le", cmd)
        self.assertIn("--rate=44100", cmd)


class AirPlayTests(unittest.TestCase):
    def test_window_title_matching(self):
        for title in ("UxPlay@host", "AirPlay Mirror", "NetShare-Hub"):
            self.assertTrue(_is_airplay_title(title), title)
        self.assertFalse(_is_airplay_title("NetShare Hub — Control Panel"))


class WindowListTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == "win32" or __import__("os").environ.get("DISPLAY"), "needs a desktop")
    def test_windows_have_expected_shape(self):
        for w in list_windows():
            self.assertEqual(set(w), {"hwnd", "title", "x", "y", "width", "height"})
            self.assertIsInstance(w["hwnd"], int)
            self.assertGreaterEqual(w["width"], 150)
