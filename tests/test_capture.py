import time
import unittest

import numpy as np

from netshare.infrastructure import capture as capture_mod
from netshare.infrastructure.capture import ScreenCapture, encode_jpeg
from netshare.infrastructure.grabbers import DxgiGrabber, Unsupported


def _has_screen() -> bool:
    try:
        import mss
        with mss.mss() as s:
            s.grab(s.monitors[1])
        return True
    except Exception:
        return False


class FakeCam:
    def __init__(self, name):
        self.name, self.calls = name, []

    def grab(self, region, new_frame_only=True):
        self.calls.append((region, new_frame_only))
        return np.zeros((region[3] - region[1], region[2] - region[0], 4), np.uint8)


class DxgiMappingTests(unittest.TestCase):
    """mss and DXGI number monitors differently — mapping must use desktop coordinates."""

    def setUp(self):
        self.g = object.__new__(DxgiGrabber)
        self.left, self.right = FakeCam("left"), FakeCam("right")
        self.g._outputs = [((1920, 0, 3840, 1080), self.right), ((0, 0, 1920, 1080), self.left)]
        self.g._failures, self.g._last_cam = 0, None

    def test_rect_maps_to_the_right_output_with_relative_region(self):
        frame, changed = self.g.grab((1920, 0, 1920, 1080))
        self.assertEqual(frame.shape, (1080, 1920, 4))
        self.assertEqual(self.right.calls[-1][0], (0, 0, 1920, 1080))
        self.g.grab((100, 200, 640, 480))
        self.assertEqual(self.left.calls[-1][0], (100, 200, 740, 680))

    def test_switching_output_forces_a_fresh_frame(self):
        self.g.grab((0, 0, 1920, 1080))
        self.g.grab((0, 0, 1920, 1080))
        self.assertEqual(self.left.calls[-1][1], True, "same output: only new frames")
        self.g.grab((1920, 0, 1920, 1080))
        self.assertEqual(self.right.calls[-1][1], False, "new output: must return a frame")

    def test_spanning_rect_is_unsupported(self):
        with self.assertRaises(Unsupported):
            self.g.grab((1800, 0, 400, 300))


class FakeFast:
    """First call returns a frame, then 'nothing changed' forever."""
    name = "fake"

    def __init__(self, fail=False):
        self.calls, self.fail = 0, fail

    def grab(self, rect, force=False):
        self.calls += 1
        if self.fail:
            raise RuntimeError("device lost")
        if self.calls == 1 or force:
            x, y, w, h = rect
            return np.full((min(h, 64), min(w, 64), 4), 128, np.uint8), True
        return None, False

    def close(self):
        pass


@unittest.skipUnless(_has_screen(), "needs a desktop")
class CaptureLoopTests(unittest.TestCase):
    def _run(self, fast, seconds, max_width=0, refine=True):
        orig = capture_mod.make_fast_grabber
        capture_mod.make_fast_grabber = lambda: fast
        try:
            cap = ScreenCapture(target_fps=30, jpeg_quality=70, max_width=max_width)
            cap.refine_idle = refine
            got, beats = [], []
            cap.on_frame = got.append
            cap.on_idle = lambda: beats.append(time.time())
            cap.start()
            time.sleep(seconds)
            cap.stop()
            return cap, got, beats
        finally:
            capture_mod.make_fast_grabber = orig

    @staticmethod
    def _size(jpeg):
        import cv2
        img = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
        return img.shape[1], img.shape[0]

    def test_still_screen_gets_one_full_resolution_refine_then_heartbeats(self):
        cap, got, beats = self._run(FakeFast(), 2.6, max_width=32)
        self.assertEqual(len(got), 2, "one motion frame + one sharp refine frame, nothing more")
        self.assertEqual(self._size(got[0])[0], 32, "motion frame honours max_width")
        self.assertEqual(self._size(got[1])[0], 64, "refine frame is full resolution")
        self.assertGreaterEqual(len(beats), 1, "1-byte heartbeats instead of resending frames")

    def test_refine_can_be_disabled(self):
        cap, got, beats = self._run(FakeFast(), 1.6, refine=False)
        self.assertEqual(len(got), 1)
        self.assertGreaterEqual(len(beats), 1)

    def test_failing_dxgi_falls_back_to_mss(self):
        cap, got, _ = self._run(FakeFast(fail=True), 1.5)
        self.assertEqual(cap.backend, "mss")
        self.assertGreater(len(got), 0)

    def test_pause_and_resume_are_announced(self):
        states = []
        cap = ScreenCapture()
        cap.has_consumers = lambda: False
        cap.on_state = states.append
        cap.start()
        cap.stop()
        cap.stop()  # idempotent: no second "paused"
        self.assertEqual(states, [True, False])
        self.assertEqual(cap.actual_fps, 0.0)


class EncoderTests(unittest.TestCase):
    def test_encode_jpeg_from_bgra(self):
        img = np.zeros((90, 160, 4), np.uint8)
        img[..., 2] = 255  # red in BGRA
        jpeg = encode_jpeg(img, 70)
        self.assertEqual(jpeg[:2], b"\xff\xd8")
        import cv2
        dec = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
        self.assertEqual(dec.shape, (90, 160, 3))
        self.assertGreater(dec[..., 2].mean(), 200)  # still red (channel order correct)
        self.assertLess(dec[..., 0].mean(), 60)
