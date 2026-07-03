"""Hardware integration tests for webcamdemo.

This is the ONLY test file allowed to open /dev/video* devices.

Policy:
- Devices are probed in setUpModule(), never at import/collection time, so
  merely discovering the suite touches no hardware.
- Every test is skipped unless list_cameras() reports at least one camera.
- MX Brio specific tests are additionally skipped unless a camera with
  extra.usb == "046d:0944" is present.
- A device held busy by another process (EBUSY on stream start) means the
  camera is unavailable, not broken: the test skips instead of erroring.
- Every camera setting a test changes is read first and restored in a
  finally block, so the camera is left exactly as it was found.
"""

import errno
import threading
import unittest

from webcamdemo import Camera, list_cameras

MX_BRIO_USB = "046d:0944"

_CAMS = []
_BRIO = None


def setUpModule():
    """Probe hardware once per test run, never at import time."""
    global _CAMS, _BRIO
    try:
        _CAMS = list_cameras()
    except Exception:  # no V4L2 stack at all (e.g. non-Linux CI)
        _CAMS = []
    _BRIO = next((c for c in _CAMS if c.extra.get("usb") == MX_BRIO_USB), None)


def _require_camera(test):
    if not _CAMS:
        test.skipTest("no camera present")


def _require_brio(test):
    if _BRIO is None:
        test.skipTest("MX Brio (usb %s) not present" % MX_BRIO_USB)


def _start_stream_or_skip(test, cam, width, height):
    """start_stream(), skipping when another process holds the device."""
    try:
        cam.start_stream(width, height)
    except OSError as exc:
        if exc.errno == errno.EBUSY:
            test.skipTest("camera busy (held by another process): %s" % exc)
        raise

_SOI = b"\xff\xd8"


def _jpeg_dimensions(data):
    """Parse (width, height) from the first SOF0..SOF3 segment of a JPEG.

    Tiny stdlib-only parser: walks marker segments from SOI until a start
    of frame marker (0xC0..0xC3) and reads the 16-bit height/width fields.
    """
    if data[:2] != _SOI:
        raise ValueError("not a JPEG (missing SOI marker)")
    i = 2
    n = len(data)
    while i + 4 <= n:
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker == 0xFF:  # fill byte
            i += 1
            continue
        if marker in (0x01, 0xD8) or 0xD0 <= marker <= 0xD7:
            i += 2  # markers without a length field
            continue
        if marker == 0xD9:  # EOI before any SOF
            break
        seg_len = (data[i + 2] << 8) | data[i + 3]
        if 0xC0 <= marker <= 0xC3:  # SOF0/SOF1/SOF2/SOF3
            height = (data[i + 5] << 8) | data[i + 6]
            width = (data[i + 7] << 8) | data[i + 8]
            return width, height
        if marker == 0xDA:  # SOS: SOF must have appeared before scan data
            break
        i += 2 + seg_len
    raise ValueError("no SOF marker found in JPEG data")


def _has_mjpg_size(formats, width, height):
    return any(f.fourcc == "MJPG" and (f.width, f.height) == (width, height)
               for f in formats)


class ListCamerasTest(unittest.TestCase):

    def setUp(self):
        _require_camera(self)

    def test_listed_cameras_are_real_capture_nodes(self):
        # Metadata-only /dev/video* nodes enumerate no capture formats;
        # every camera list_cameras() returns must enumerate at least one.
        ids = [c.id for c in _CAMS]
        self.assertEqual(len(ids), len(set(ids)), "duplicate camera ids")
        for cam_info in _CAMS:
            self.assertTrue(cam_info.id.startswith("/dev/video"), cam_info.id)
            with Camera(cam_info.id) as cam:
                self.assertTrue(
                    cam.list_formats(),
                    "%s listed but has no capture formats (metadata node?)"
                    % cam_info.id)

    def test_mx_brio_present_with_usb_id(self):
        _require_brio(self)
        self.assertEqual(_BRIO.extra.get("usb"), MX_BRIO_USB)
        self.assertIn("Brio", _BRIO.name)

    def test_brio_metadata_node_excluded(self):
        _require_brio(self)
        # The MX Brio exposes one capture node and one metadata node with
        # the same USB id; only the capture node may be listed.
        brios = [c for c in _CAMS if c.extra.get("usb") == MX_BRIO_USB]
        self.assertEqual(
            len(brios), 1,
            "expected exactly one MX Brio capture node, got %r"
            % [c.id for c in brios])


class BrioControlsTest(unittest.TestCase):

    def setUp(self):
        _require_brio(self)
        self.cam = Camera(_BRIO.id)
        self.addCleanup(self.cam.close)

    def _controls_by_id(self):
        return {c.id: c for c in self.cam.list_controls()}

    def test_at_least_15_controls(self):
        controls = self.cam.list_controls()
        self.assertGreaterEqual(
            len(controls), 15,
            "expected >= 15 controls on MX Brio, got %d: %r"
            % (len(controls), sorted(c.id for c in controls)))

    def test_brightness_min_max_default_roundtrip(self):
        ctrl = self._controls_by_id().get("brightness")
        self.assertIsNotNone(ctrl, "brightness control missing")
        orig = self.cam.get_control("brightness")
        try:
            for target in (ctrl.min, ctrl.max, ctrl.default):
                self.cam.set_control("brightness", target)
                self.assertEqual(self.cam.get_control("brightness"), target)
        finally:
            self.cam.set_control("brightness", orig)
        self.assertEqual(self.cam.get_control("brightness"), orig,
                         "brightness not restored")

    def test_menu_control_set_by_value(self):
        ctrl = self._controls_by_id().get("power_line_frequency")
        self.assertIsNotNone(ctrl, "power_line_frequency control missing")
        self.assertEqual(ctrl.type, "menu")
        self.assertGreaterEqual(len(ctrl.menu), 2)
        orig = self.cam.get_control("power_line_frequency")
        self.assertIn(orig, ctrl.menu)
        target = next(v for v in sorted(ctrl.menu) if v != orig)
        try:
            self.cam.set_control("power_line_frequency", target)
            self.assertEqual(self.cam.get_control("power_line_frequency"),
                             target)
        finally:
            self.cam.set_control("power_line_frequency", orig)
        self.assertEqual(self.cam.get_control("power_line_frequency"), orig,
                         "power_line_frequency not restored")

    def test_unknown_control_id_raises_valueerror(self):
        with self.assertRaises(ValueError):
            self.cam.get_control("definitely_not_a_control")
        with self.assertRaises(ValueError):
            self.cam.set_control("definitely_not_a_control", 1)

    def test_focus_absolute_inactive_follows_autofocus(self):
        by_id = self._controls_by_id()
        self.assertIn("focus_absolute", by_id)
        self.assertIn("focus_automatic_continuous", by_id)
        orig_auto = self.cam.get_control("focus_automatic_continuous")
        orig_focus = self.cam.get_control("focus_absolute")
        try:
            self.cam.set_control("focus_automatic_continuous", 0)
            self.assertFalse(
                self._controls_by_id()["focus_absolute"].inactive,
                "focus_absolute should be active in manual focus mode")
            self.cam.set_control("focus_automatic_continuous", 1)
            self.assertTrue(
                self._controls_by_id()["focus_absolute"].inactive,
                "focus_absolute should be inactive while autofocus is on")
        finally:
            self.cam.set_control("focus_automatic_continuous", orig_auto)
            if orig_auto == 0:
                # focus position only settable in manual mode
                try:
                    self.cam.set_control("focus_absolute", orig_focus)
                except ValueError:
                    pass
        self.assertEqual(self.cam.get_control("focus_automatic_continuous"),
                         orig_auto, "autofocus setting not restored")


class BrioFovTest(unittest.TestCase):

    def setUp(self):
        _require_brio(self)
        self.cam = Camera(_BRIO.id)
        self.addCleanup(self.cam.close)

    def test_fov_control_present(self):
        ctrl = next((c for c in self.cam.list_controls()
                     if c.id == "logitech_brio_fov"), None)
        self.assertIsNotNone(ctrl, "logitech_brio_fov control missing")
        self.assertEqual(ctrl.type, "menu")
        self.assertEqual(set(ctrl.menu), {0, 1, 2})

    def test_fov_get_returns_valid_value(self):
        self.assertIn(self.cam.get_control("logitech_brio_fov"), (0, 1, 2))

    def test_fov_set_to_current_value_succeeds(self):
        # Setting the FOV to its current value exercises the XU write path
        # without persistently changing anything on the camera.
        cur = self.cam.get_control("logitech_brio_fov")
        self.cam.set_control("logitech_brio_fov", cur)
        self.assertEqual(self.cam.get_control("logitech_brio_fov"), cur)


class BrioFormatsTest(unittest.TestCase):

    def setUp(self):
        _require_brio(self)
        self.cam = Camera(_BRIO.id)
        self.addCleanup(self.cam.close)

    def test_mjpg_format_present(self):
        formats = self.cam.list_formats()
        self.assertTrue(any(f.fourcc == "MJPG" for f in formats),
                        "no MJPG format on MX Brio")

    def test_4k_frame_has_real_4k_dimensions(self):
        if not _has_mjpg_size(self.cam.list_formats(), 3840, 2160):
            self.skipTest("camera on USB2 link")
        _start_stream_or_skip(self, self.cam, 3840, 2160)
        try:
            frame = self.cam.read_jpeg()
        finally:
            self.cam.stop_stream()
        self.assertEqual(frame[:2], _SOI)
        self.assertEqual(_jpeg_dimensions(frame), (3840, 2160),
                         "camera did not deliver a real 4K frame")


class BrioStreamingTest(unittest.TestCase):

    def setUp(self):
        _require_brio(self)
        self.cam = Camera(_BRIO.id)
        self.addCleanup(self.cam.close)
        # never leave the device streaming, whatever the test outcome
        self.addCleanup(self.cam.stop_stream)

    def test_720p_five_frames_soi_and_distinct(self):
        _start_stream_or_skip(self, self.cam, 1280, 720)
        frames = [self.cam.read_jpeg() for _ in range(5)]
        for frame in frames:
            self.assertEqual(frame[:2], _SOI)
        self.assertEqual(_jpeg_dimensions(frames[0]), (1280, 720))
        self.assertGreater(len(set(frames)), 1,
                           "all 5 frames byte-identical; stream looks frozen")

    def test_stop_idempotent_and_read_after_stop_raises(self):
        _start_stream_or_skip(self, self.cam, 1280, 720)
        self.assertEqual(self.cam.read_jpeg()[:2], _SOI)
        self.cam.stop_stream()
        self.cam.stop_stream()  # second stop must be a no-op
        with self.assertRaises(RuntimeError):
            self.cam.read_jpeg()

    def test_restart_at_vga_after_720p(self):
        _start_stream_or_skip(self, self.cam, 1280, 720)
        self.cam.read_jpeg()
        self.cam.stop_stream()
        _start_stream_or_skip(self, self.cam, 640, 480)
        frame = self.cam.read_jpeg()
        self.assertEqual(_jpeg_dimensions(frame), (640, 480))

    def test_concurrent_control_read_during_streaming(self):
        _start_stream_or_skip(self, self.cam, 1280, 720)
        errors = []
        stop = threading.Event()

        def poll_control():
            try:
                while not stop.is_set():
                    self.cam.get_control("brightness")
            except Exception as exc:  # re-raised in the main thread
                errors.append(exc)

        worker = threading.Thread(target=poll_control)
        worker.start()
        try:
            for _ in range(5):
                self.assertEqual(self.cam.read_jpeg()[:2], _SOI)
        finally:
            stop.set()
            worker.join(timeout=10)
        self.assertFalse(worker.is_alive(), "control poll thread hung")
        self.assertEqual(errors, [],
                         "control read failed during streaming: %r" % errors)


class CameraReleaseTest(unittest.TestCase):

    def setUp(self):
        _require_camera(self)

    def test_context_manager_releases_device(self):
        cam_info = _BRIO or _CAMS[0]
        with Camera(cam_info.id) as cam:
            can_stream = _has_mjpg_size(cam.list_formats(), 640, 480)
            if can_stream:
                _start_stream_or_skip(self, cam, 640, 480)
                cam.read_jpeg()
                # deliberately no stop_stream(): close() must release the
                # queued buffers, otherwise the reopen below gets EBUSY
        with Camera(cam_info.id) as cam2:
            self.assertEqual(cam2.info().id, cam_info.id)
            if can_stream:
                _start_stream_or_skip(self, cam2, 640, 480)
                try:
                    self.assertEqual(cam2.read_jpeg()[:2], _SOI)
                finally:
                    cam2.stop_stream()


if __name__ == "__main__":
    unittest.main()
