"""Unit tests for webcamdemo.cli.

Stdlib unittest only. No hardware, no network. Runs cli.main(argv)
in-process with a FakeCamera patched over the symbols cli imported
(webcamdemo.cli.Camera / webcamdemo.cli.list_cameras -- cli.py does
"from . import Camera, list_cameras" so the module-level names are
the correct patch targets).

Run alone with: python3 -m unittest tests.test_cli
"""

import contextlib
import io
import os
import tempfile
import unittest
from unittest import mock

from webcamdemo import cli
from webcamdemo.model import CameraInfo, Control, FrameFormat

def fake_frame(n):
    """The n-th JPEG the fake camera delivers; frames are numbered so tests
    can assert which frame of a warm-up sequence was kept."""
    return b"\xff\xd8fake-frame-%d\xff\xd9" % n


def make_controls():
    return [
        Control(id="brightness", name="Brightness", type="int",
                min=0, max=255, step=1, default=128, value=100),
        Control(id="power_line_frequency", name="Power Line Frequency",
                type="menu", value=1,
                menu={0: "Disabled", 1: "50 Hz", 2: "60 Hz"}),
        Control(id="exposure_absolute", name="Exposure (Absolute)",
                type="int", min=3, max=2047, step=1, default=250,
                value=250, inactive=True),
    ]


FAKE_FORMATS = [
    FrameFormat(fourcc="MJPG", width=1920, height=1080, fps=[30.0, 15.0]),
    FrameFormat(fourcc="MJPG", width=640, height=480, fps=[30.0]),
    # Larger than any MJPG format: default snapshot size must ignore it.
    FrameFormat(fourcc="YUYV", width=2560, height=1440, fps=[5.0]),
]


class FakeCamera:
    """Stands in for webcamdemo.Camera. Never touches /dev/video*."""

    last = None  # most recently constructed instance, for assertions

    def __init__(self, camera_id=None):
        self.camera_id = camera_id
        self.closed = False
        self.stream_args = None
        self.stream_stopped = False
        self.frames_read = 0
        self._controls = {c.id: c for c in make_controls()}
        FakeCamera.last = self

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.closed = True
        return False

    def list_controls(self):
        return list(self._controls.values())

    def _ctrl(self, ctrl_id):
        try:
            return self._controls[ctrl_id]
        except KeyError:
            raise ValueError("unknown control: %s" % ctrl_id)

    def get_control(self, ctrl_id):
        return self._ctrl(ctrl_id).value

    def set_control(self, ctrl_id, value):
        self._ctrl(ctrl_id).value = int(value)

    def list_formats(self):
        return list(FAKE_FORMATS)

    def start_stream(self, width, height, fps=None):
        self.stream_args = (width, height)

    def read_jpeg(self):
        self.frames_read += 1
        return fake_frame(self.frames_read)

    def stop_stream(self):
        self.stream_stopped = True


class CliTestCase(unittest.TestCase):
    """Base: patches cli's Camera/list_cameras and captures output."""

    def setUp(self):
        FakeCamera.last = None
        for target, repl in (
            ("webcamdemo.cli.Camera", FakeCamera),
            ("webcamdemo.cli.list_cameras",
             lambda: [CameraInfo(id="/dev/video9", name="Fake Cam")]),
        ):
            patcher = mock.patch(target, repl)
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_cli(self, argv):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), \
                contextlib.redirect_stderr(stderr):
            code = cli.main(argv)
        return code, stdout.getvalue(), stderr.getvalue()


class TestList(CliTestCase):
    def test_list_output_format(self):
        code, out, err = self.run_cli(["list"])
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        lines = out.splitlines()
        self.assertGreaterEqual(len(lines), 3)
        self.assertIn("ID", lines[0])
        self.assertIn("NAME", lines[0])
        self.assertTrue(set(lines[1]) <= {"-", " "},
                        "second line should be a dash separator: %r" % lines[1])
        self.assertIn("/dev/video9", lines[2])
        self.assertIn("Fake Cam", lines[2])

    def test_no_cameras_exits_1_with_error(self):
        with mock.patch("webcamdemo.cli.list_cameras", lambda: []):
            code, out, err = self.run_cli(["list"])
        self.assertEqual(code, 1)
        self.assertTrue(err.startswith("error: "), repr(err))
        self.assertIn("no cameras found", err)


class TestControls(CliTestCase):
    def test_controls_table_has_id_type_value(self):
        code, out, err = self.run_cli(["controls", "-d", "/dev/video9"])
        self.assertEqual(code, 0)
        header = out.splitlines()[0]
        for col in ("ID", "TYPE", "VALUE"):
            self.assertIn(col, header)
        self.assertIn("brightness", out)
        self.assertIn("int", out)
        self.assertIn("100", out)
        self.assertIn("power_line_frequency", out)
        self.assertIn("menu", out)
        self.assertIn("0=Disabled", out)
        self.assertIn("1=50 Hz", out)
        self.assertIn("inactive", out)  # flags column for exposure_absolute
        self.assertTrue(FakeCamera.last.closed)


class TestGet(CliTestCase):
    def test_get_prints_value(self):
        code, out, err = self.run_cli(["get", "brightness"])
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), "100")

    def test_get_unknown_control_errors(self):
        code, out, err = self.run_cli(["get", "no_such_control"])
        self.assertEqual(code, 1)
        self.assertTrue(err.startswith("error: "), repr(err))


class TestSet(CliTestCase):
    def test_set_with_int(self):
        code, out, err = self.run_cli(["set", "brightness", "42"])
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), "brightness = 42")
        self.assertEqual(FakeCamera.last.get_control("brightness"), 42)

    def test_set_with_menu_label_case_insensitive(self):
        code, out, err = self.run_cli(
            ["set", "power_line_frequency", "60 hZ"])
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), "power_line_frequency = 2")
        self.assertEqual(
            FakeCamera.last.get_control("power_line_frequency"), 2)

    def test_set_unknown_control_errors(self):
        code, out, err = self.run_cli(["set", "no_such_control", "5"])
        self.assertEqual(code, 1)
        self.assertTrue(err.startswith("error: "), repr(err))

    def test_set_bad_menu_label_lists_choices(self):
        code, out, err = self.run_cli(
            ["set", "power_line_frequency", "75 Hz"])
        self.assertEqual(code, 1)
        self.assertTrue(err.startswith("error: "), repr(err))
        self.assertIn("50 Hz", err)  # choices are listed

    def test_set_non_int_on_non_menu_control_errors(self):
        code, out, err = self.run_cli(["set", "brightness", "bright"])
        self.assertEqual(code, 1)
        self.assertTrue(err.startswith("error: "), repr(err))


class TestFormats(CliTestCase):
    def test_formats_output(self):
        code, out, err = self.run_cli(["formats"])
        self.assertEqual(code, 0)
        header = out.splitlines()[0]
        for col in ("FOURCC", "SIZE", "FPS"):
            self.assertIn(col, header)
        self.assertIn("MJPG", out)
        self.assertIn("1920x1080", out)
        self.assertIn("30, 15", out)
        self.assertIn("YUYV", out)
        self.assertIn("2560x1440", out)


class TestSnapshot(CliTestCase):
    def test_snapshot_writes_fake_jpeg_with_explicit_size(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "snap.jpg")
            code, out, err = self.run_cli(
                ["snapshot", "-o", path, "--size", "640x480"])
            self.assertEqual(code, 0)
            with open(path, "rb") as fh:
                written = fh.read()
            self.assertIn(path, out)
            self.assertIn("640x480", out)
        cam = FakeCamera.last
        # Warm-up contract: read 5 frames (auto-exposure settles) and keep
        # the LAST one, not the dark/green first frame.
        self.assertEqual(cam.frames_read, 5)
        self.assertEqual(written, fake_frame(5))
        self.assertEqual(cam.stream_args, (640, 480))
        self.assertTrue(cam.stream_stopped)
        self.assertTrue(cam.closed)

    def test_snapshot_default_size_prefers_largest_mjpg(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "snap.jpg")
            code, out, err = self.run_cli(["snapshot", "-o", path])
            self.assertEqual(code, 0)
        # Largest MJPG is 1920x1080; the larger 2560x1440 YUYV must lose.
        self.assertEqual(FakeCamera.last.stream_args, (1920, 1080))

    def test_snapshot_malformed_size_errors_and_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "snap.jpg")
            code, out, err = self.run_cli(
                ["snapshot", "-o", path, "--size", "1920x"])
            self.assertEqual(code, 1)
            self.assertTrue(err.startswith("error: "), repr(err))
            self.assertIn("WIDTHxHEIGHT", err)
            self.assertFalse(os.path.exists(path))

    def test_snapshot_size_without_x_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "snap.jpg")
            code, out, err = self.run_cli(
                ["snapshot", "-o", path, "--size", "1080p"])
            self.assertEqual(code, 1)
            self.assertTrue(err.startswith("error: "), repr(err))
            self.assertFalse(os.path.exists(path))


class TestServe(CliTestCase):
    def test_serve_passes_args_and_does_not_start_server(self):
        with mock.patch("webcamdemo.server.serve") as serve:
            code, out, err = self.run_cli(
                ["serve", "-d", "/dev/video9",
                 "--host", "0.0.0.0", "--port", "9999"])
        self.assertEqual(code, 0)
        serve.assert_called_once_with(
            camera_id="/dev/video9", host="0.0.0.0", port=9999)

    def test_serve_defaults(self):
        with mock.patch("webcamdemo.server.serve") as serve:
            code, out, err = self.run_cli(["serve"])
        self.assertEqual(code, 0)
        serve.assert_called_once_with(
            camera_id=None, host="127.0.0.1", port=8600)


class TestParseHelpers(unittest.TestCase):
    def test_parse_size_ok(self):
        self.assertEqual(cli._parse_size("1280x720"), (1280, 720))

    def test_parse_size_uppercase_x(self):
        self.assertEqual(cli._parse_size("1920X1080"), (1920, 1080))

    def test_parse_size_malformed(self):
        for bad in ("1920x", "x720", "1080p", "axb", ""):
            with self.assertRaises(ValueError):
                cli._parse_size(bad)


if __name__ == "__main__":
    unittest.main()
