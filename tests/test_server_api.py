"""HTTP API tests for webcamdemo.server using a fake backend.

Starts the real ThreadingHTTPServer on an ephemeral loopback port with the
backend/list_cameras resolution patched to a FakeBackend. No camera hardware
and no network access required.

Run alone with: python3 -m unittest tests.test_server_api
"""

import http.client
import json
import re
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest import mock

from webcamdemo import server
from webcamdemo.model import CameraInfo, Control, FrameFormat

FAKE_IDS = ("/dev/fake0", "/dev/fake1", "/dev/fake2")
SUPPORTED_SIZES = ((1280, 720), (1920, 1080))

JPEG_SOI = b"\xff\xd8"
JPEG_EOI = b"\xff\xd9"


def fake_list_cameras():
    return [CameraInfo(id=cam_id, name="Fake Camera %s" % cam_id[-1])
            for cam_id in FAKE_IDS]


class FakeBackend:
    """Stand-in for the platform Backend. Never touches /dev/video*.

    Frames are tiny valid-enough JPEGs whose payload encodes the active
    stream size and a per-backend frame counter, so tests can assert which
    mode a frame was captured in and that frames vary.
    """

    def __init__(self, camera_id):
        if camera_id not in FAKE_IDS:
            raise LookupError("no such camera: %s" % camera_id)
        self.camera_id = camera_id
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._streaming = False
        self._size = None
        self._frame_no = 0
        self.start_calls = []
        self.fail_reads = False  # set True to simulate an unplugged camera
        self._controls = {
            "brightness": Control(
                id="brightness", name="Brightness", type="int",
                min=0, max=255, step=1, default=128, value=128),
            "hflip": Control(
                id="hflip", name="Horizontal Flip", type="bool",
                default=0, value=0),
            "power_line_frequency": Control(
                id="power_line_frequency", name="Power Line Frequency",
                type="menu", min=0, max=2, default=2, value=2,
                menu={0: "Disabled", 1: "50 Hz", 2: "60 Hz"}),
            "exposure_absolute": Control(
                id="exposure_absolute", name="Exposure (Absolute)",
                type="int", min=3, max=2047, step=1, default=250, value=250,
                inactive=True),
        }

    # -- controls ------------------------------------------------------

    def list_controls(self):
        return list(self._controls.values())

    def set_control(self, ctrl_id, value):
        ctrl = self._controls.get(ctrl_id)
        if ctrl is None:
            raise ValueError("unknown control: %s" % ctrl_id)
        if ctrl.inactive:
            raise ValueError("control %s is inactive" % ctrl_id)
        if ctrl.type == "menu":
            if value not in ctrl.menu:
                raise ValueError("invalid menu value %r" % (value,))
        elif ctrl.type == "bool":
            if value not in (0, 1):
                raise ValueError("bool control takes 0 or 1")
        else:
            if not (ctrl.min <= value <= ctrl.max):
                raise ValueError("value %d out of range [%d, %d]"
                                 % (value, ctrl.min, ctrl.max))
        ctrl.value = int(value)

    # -- formats / streaming --------------------------------------------

    def list_formats(self):
        return [FrameFormat(fourcc="MJPG", width=w, height=h, fps=[30.0])
                for (w, h) in SUPPORTED_SIZES]

    def start_stream(self, width, height, fps=None):
        if (width, height) not in SUPPORTED_SIZES:
            raise ValueError("unsupported size %dx%d" % (width, height))
        with self._lock:
            self._size = (width, height)
            self._streaming = True
            self.start_calls.append((width, height, fps))
        self._wake.clear()

    def stop_stream(self):
        with self._lock:
            self._streaming = False
        self._wake.set()  # unblock a pending read_jpeg immediately

    def read_jpeg(self):
        self._wake.wait(0.01)  # pace ~100 fps; wakes early on stop_stream
        with self._lock:
            if self.fail_reads:
                raise RuntimeError("simulated capture failure")
            if not self._streaming:
                raise RuntimeError("stream not started")
            self._frame_no += 1
            payload = ("%dx%d#%d" % (self._size[0], self._size[1],
                                     self._frame_no)).encode("ascii")
        return JPEG_SOI + b"\xff\xe0" + payload + JPEG_EOI

    def close(self):
        self.stop_stream()

    def __str__(self):
        return "FakeBackend(%s)" % self.camera_id


def _read_multipart_frames(resp, boundary, count):
    """Read `count` parts from an open multipart/x-mixed-replace response."""
    delim = b"--" + boundary.encode("ascii")
    frames = []
    for _ in range(count):
        line = resp.readline()
        while line in (b"\r\n", b"\n"):  # tolerate inter-part CRLF
            line = resp.readline()
        if line.rstrip(b"\r\n") != delim:
            raise AssertionError("bad boundary line: %r" % line)
        headers = {}
        while True:
            line = resp.readline()
            if line in (b"\r\n", b"\n", b""):
                break
            name, _, value = line.decode("latin-1").partition(":")
            headers[name.strip().lower()] = value.strip()
        length = int(headers["content-length"])
        body = b""
        while len(body) < length:
            chunk = resp.read(length - len(body))
            if not chunk:
                raise AssertionError("stream ended mid-frame")
            body += chunk
        frames.append((headers, body))
    return frames


class ServerTestBase(unittest.TestCase):
    """Real ThreadingHTTPServer + FakeBackend; subclasses pick the default."""

    maxDiff = None
    default_camera_id = "/dev/fake1"

    @classmethod
    def setUpClass(cls):
        cls._saved_api = server._api_cache
        cls._saved_default = server._default_camera_id
        server._api_cache = (fake_list_cameras, FakeBackend)
        server._default_camera_id = cls.default_camera_id
        with server._registry_lock:
            server._states.clear()
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0),
                                        server.RequestHandler)
        cls.port = cls.httpd.server_address[1]
        cls.base = "http://127.0.0.1:%d" % cls.port
        cls.server_thread = threading.Thread(target=cls.httpd.serve_forever,
                                             daemon=True)
        cls.server_thread.start()
        # No proxies: tests must work without network access.
        cls.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}))

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.server_thread.join(timeout=10)
        cls.httpd.server_close()
        with server._registry_lock:
            states = list(server._states.values())
            server._states.clear()
        for state in states:
            state.shutdown()
        server._api_cache = cls._saved_api
        server._default_camera_id = cls._saved_default

    # -- helpers ---------------------------------------------------------

    def _get(self, path, timeout=10):
        return self.opener.open(self.base + path, timeout=timeout)

    def _request_json(self, path, data=None, method=None):
        """Return (status, content_type, parsed_json); no raise on 4xx/5xx."""
        if method is None:
            method = "POST" if data is not None else "GET"
        body = None
        headers = {}
        if data is not None:
            body = json.dumps(data).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.base + path, data=body,
                                     headers=headers, method=method)
        try:
            with self.opener.open(req, timeout=10) as resp:
                return (resp.status, resp.headers.get("Content-Type"),
                        json.loads(resp.read()))
        except urllib.error.HTTPError as err:
            raw = err.read()
            ctype = err.headers.get("Content-Type")
            err.close()
            return err.code, ctype, json.loads(raw)

    def _open_stream(self, cam):
        resp = self._get("/stream.mjpg?cam=" + cam)
        ctype = resp.headers.get("Content-Type", "")
        self.assertIn("multipart/x-mixed-replace", ctype)
        match = re.search(r"boundary=([\w.-]+)", ctype)
        self.assertIsNotNone(match, "no boundary in %r" % ctype)
        return resp, match.group(1)

    def _assert_jpeg(self, data):
        self.assertTrue(data.startswith(JPEG_SOI), repr(data[:8]))
        self.assertTrue(data.endswith(JPEG_EOI), repr(data[-8:]))


class ServerApiTest(ServerTestBase):

    def _snapshot_until(self, cam, marker, attempts=100):
        """Poll /snapshot.jpg until `marker` shows up (capture is async, so
        the frame right after a stream switch may still be the stale one)."""
        data = b""
        for _ in range(attempts):
            with self._get("/snapshot.jpg?cam=" + cam) as resp:
                self.assertEqual(resp.status, 200)
                data = resp.read()
            if marker in data:
                return data
            time.sleep(0.05)
        return data

    def test_index_serves_html(self):
        with self._get("/") as resp:
            self.assertEqual(resp.status, 200)
            self.assertIn("text/html", resp.headers.get("Content-Type", ""))
            body = resp.read().decode("utf-8")
        self.assertIn("<title>webcamdemo", body)

    def test_api_cameras_lists_default_camera_first(self):
        status, ctype, cams = self._request_json("/api/cameras")
        self.assertEqual(status, 200)
        self.assertEqual(ctype, "application/json")
        ids = [c["id"] for c in cams]
        # default (/dev/fake1) first; the rest keep list_cameras() order
        self.assertEqual(ids, ["/dev/fake1", "/dev/fake0", "/dev/fake2"])
        self.assertEqual(cams[0]["name"], "Fake Camera 1")

    def test_api_controls_shapes(self):
        status, ctype, controls = self._request_json(
            "/api/controls?cam=/dev/fake0")
        self.assertEqual(status, 200)
        self.assertEqual(ctype, "application/json")
        by_id = {c["id"]: c for c in controls}
        self.assertEqual(
            set(by_id),
            {"brightness", "hflip", "power_line_frequency",
             "exposure_absolute"})
        brightness = by_id["brightness"]
        self.assertEqual(brightness["type"], "int")
        self.assertEqual(
            (brightness["min"], brightness["max"], brightness["step"],
             brightness["default"]),
            (0, 255, 1, 128))
        self.assertFalse(brightness["inactive"])
        self.assertEqual(by_id["hflip"]["type"], "bool")
        # menu keys must be JSON-safe strings (Control.to_dict contract)
        self.assertEqual(by_id["power_line_frequency"]["menu"],
                         {"0": "Disabled", "1": "50 Hz", "2": "60 Hz"})
        self.assertTrue(by_id["exposure_absolute"]["inactive"])

    def test_api_formats(self):
        status, ctype, formats = self._request_json(
            "/api/formats?cam=/dev/fake0")
        self.assertEqual(status, 200)
        self.assertEqual(ctype, "application/json")
        self.assertEqual(formats, [
            {"fourcc": "MJPG", "width": 1280, "height": 720, "fps": [30.0]},
            {"fourcc": "MJPG", "width": 1920, "height": 1080,
             "fps": [30.0]},
        ])

    def test_api_control_set_ok_returns_refreshed_controls(self):
        status, _, body = self._request_json(
            "/api/control",
            {"cam": "/dev/fake0", "id": "brightness", "value": 200})
        self.assertEqual(status, 200)
        self.assertIs(body["ok"], True)
        by_id = {c["id"]: c for c in body["controls"]}
        self.assertEqual(by_id["brightness"]["value"], 200)
        # change persisted: a later GET sees the same value
        status, _, controls = self._request_json(
            "/api/controls?cam=/dev/fake0")
        self.assertEqual(status, 200)
        by_id = {c["id"]: c for c in controls}
        self.assertEqual(by_id["brightness"]["value"], 200)

    def test_api_control_bad_value_400_with_controls(self):
        status, ctype, body = self._request_json(
            "/api/control",
            {"cam": "/dev/fake0", "id": "brightness", "value": 999})
        self.assertEqual(status, 400)
        self.assertEqual(ctype, "application/json")
        self.assertIs(body["ok"], False)
        self.assertIn("out of range", body["error"])
        by_id = {c["id"]: c for c in body["controls"]}
        self.assertIsInstance(by_id["brightness"]["value"], int)
        self.assertNotEqual(by_id["brightness"]["value"], 999)

    def test_api_control_unknown_camera_4xx_json(self):
        status, ctype, body = self._request_json(
            "/api/control",
            {"cam": "/dev/nope", "id": "brightness", "value": 1})
        self.assertGreaterEqual(status, 400)
        self.assertLess(status, 500)
        self.assertEqual(ctype, "application/json")
        self.assertIs(body["ok"], False)
        self.assertIn("/dev/nope", body["error"])

    def test_unknown_path_returns_404_json(self):
        status, ctype, body = self._request_json("/definitely/not/here")
        self.assertEqual(status, 404)
        self.assertEqual(ctype, "application/json")
        self.assertEqual(body, {"ok": False, "error": "not found"})
        status, _, body = self._request_json("/api/nope", {})
        self.assertEqual(status, 404)
        self.assertIs(body["ok"], False)

    def test_snapshot_lazily_starts_stream(self):
        with server._registry_lock:
            self.assertNotIn("/dev/fake2", server._states)
        with self._get("/snapshot.jpg?cam=/dev/fake2") as resp:
            self.assertEqual(resp.status, 200)
            self.assertEqual(resp.headers.get("Content-Type"), "image/jpeg")
            data = resp.read()
        self._assert_jpeg(data)
        backend = server._states["/dev/fake2"].backend
        self.assertTrue(backend._streaming)
        # exactly one lazy start, at the preferred default mode
        self.assertEqual(backend.start_calls, [(1280, 720, 30.0)])

    def test_stream_mjpg_serves_multipart_jpeg_frames(self):
        resp, boundary = self._open_stream("/dev/fake1")
        try:
            frames = _read_multipart_frames(resp, boundary, 2)
        finally:
            resp.close()
        self.assertEqual(len(frames), 2)
        for headers, body in frames:
            self.assertEqual(headers["content-type"], "image/jpeg")
            self.assertEqual(int(headers["content-length"]), len(body))
            self._assert_jpeg(body)
        # read_jpeg varies per frame, so consecutive parts must differ
        self.assertNotEqual(frames[0][1], frames[1][1])

    def test_stream_two_concurrent_clients_both_get_frames(self):
        results = {}
        errors = {}

        def client(idx):
            try:
                resp, boundary = self._open_stream("/dev/fake1")
                try:
                    results[idx] = _read_multipart_frames(resp, boundary, 2)
                finally:
                    resp.close()
            except Exception as exc:  # surfaced via assertions below
                errors[idx] = exc

        threads = [threading.Thread(target=client, args=(i,))
                   for i in (0, 1)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)
            self.assertFalse(t.is_alive(), "stream client thread hung")
        self.assertEqual(errors, {})
        for idx in (0, 1):
            self.assertEqual(len(results[idx]), 2)
            for _, body in results[idx]:
                self._assert_jpeg(body)

    def test_post_stream_switch_changes_snapshot_size(self):
        # establish a running stream at the known default mode
        with self._get("/snapshot.jpg?cam=/dev/fake0") as resp:
            before = resp.read()
        self.assertIn(b"1280x720#", before)
        status, _, body = self._request_json(
            "/api/stream",
            {"cam": "/dev/fake0", "width": 1920, "height": 1080, "fps": 30})
        self.assertEqual(status, 200)
        self.assertIs(body["ok"], True)
        after = self._snapshot_until("/dev/fake0", b"1920x1080#")
        self._assert_jpeg(after)
        self.assertIn(b"1920x1080#", after)
        # leave fake0 back at the default mode for other tests
        status, _, _ = self._request_json(
            "/api/stream",
            {"cam": "/dev/fake0", "width": 1280, "height": 720, "fps": 30})
        self.assertEqual(status, 200)

    def test_post_stream_invalid_size_400_and_stream_survives(self):
        # pin a known-good mode first
        status, _, _ = self._request_json(
            "/api/stream",
            {"cam": "/dev/fake0", "width": 1280, "height": 720, "fps": 30})
        self.assertEqual(status, 200)
        status, ctype, body = self._request_json(
            "/api/stream", {"cam": "/dev/fake0", "width": 640, "height": 480})
        self.assertEqual(status, 400)
        self.assertEqual(ctype, "application/json")
        self.assertIs(body["ok"], False)
        self.assertIn("unsupported size", body["error"])
        # rollback: snapshot and MJPEG stream still serve at the old mode
        with self._get("/snapshot.jpg?cam=/dev/fake0") as resp:
            self.assertEqual(resp.status, 200)
            data = resp.read()
        self._assert_jpeg(data)
        self.assertIn(b"1280x720#", data)
        resp, boundary = self._open_stream("/dev/fake0")
        try:
            frames = _read_multipart_frames(resp, boundary, 1)
        finally:
            resp.close()
        self.assertIn(b"1280x720#", frames[0][1])

    def test_post_stream_rejects_bad_fps(self):
        for bad in (0, -5, "abc"):
            status, _, body = self._request_json(
                "/api/stream",
                {"cam": "/dev/fake0", "width": 1280, "height": 720,
                 "fps": bad})
            self.assertEqual(status, 400, "fps=%r" % (bad,))
            self.assertIs(body["ok"], False)
            self.assertIn("fps", body["error"])

    def test_post_oversized_content_length_400(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.putrequest("POST", "/api/stream")
            conn.putheader("Content-Type", "application/json")
            conn.putheader("Content-Length",
                           str(server.MAX_BODY_BYTES + 1))
            conn.endheaders()
            resp = conn.getresponse()
            self.assertEqual(resp.status, 400)
            body = json.loads(resp.read())
        finally:
            conn.close()
        self.assertIs(body["ok"], False)
        self.assertIn("invalid request body", body["error"])


class CaptureFailureTest(ServerTestBase):
    """Contract when read_jpeg keeps failing (e.g. camera unplugged):
    the capture thread gives up after MAX_CAPTURE_FAILURES, drops the stale
    frame and notifies waiters; streams end; snapshot returns 503; and
    ensure_stream() can restart the dead capture thread once reads recover."""

    def setUp(self):
        patcher = mock.patch.object(server, "MAX_CAPTURE_FAILURES", 3)
        patcher.start()
        self.addCleanup(patcher.stop)
        self._reset_states()
        self.addCleanup(self._reset_states)

    @staticmethod
    def _reset_states():
        with server._registry_lock:
            states = list(server._states.values())
            server._states.clear()
        for state in states:
            state.shutdown()

    def _start_and_kill_capture(self, cam):
        """Start a stream via snapshot, then make every read fail until the
        capture thread gives up. Returns the CameraState."""
        with self._get("/snapshot.jpg?cam=" + cam) as resp:
            self.assertEqual(resp.status, 200)
        state = server._states[cam]
        thread = state.capture_thread
        state.backend.fail_reads = True
        thread.join(timeout=10)
        self.assertFalse(thread.is_alive(),
                         "capture thread did not give up after repeated "
                         "read failures")
        return state

    def test_giveup_drops_stale_frame_and_notifies(self):
        state = self._start_and_kill_capture("/dev/fake0")
        with state.cond:
            self.assertIsNone(
                state.latest_jpeg,
                "terminal capture failure must clear latest_jpeg so clients "
                "do not re-receive the stale frame forever")

    def test_snapshot_503_while_capture_keeps_failing(self):
        self._start_and_kill_capture("/dev/fake0")
        # Reads still fail: the restarted capture dies again, no frame ever
        # arrives, and snapshot must answer 503 instead of hanging.
        status, ctype, body = self._request_json("/snapshot.jpg?cam=/dev/fake0")
        self.assertEqual(status, 503)
        self.assertEqual(ctype, "application/json")
        self.assertIs(body["ok"], False)
        self.assertIn("no frame", body["error"])

    def test_stream_ends_when_capture_dies(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        self.addCleanup(conn.close)
        conn.request("GET", "/stream.mjpg?cam=/dev/fake0")
        sock = conn.sock  # keep a handle: getresponse() may drop conn.sock
        resp = conn.getresponse()
        self.assertEqual(resp.status, 200)
        first = resp.readline()  # first multipart boundary line
        self.assertTrue(first.startswith(b"--frame"), repr(first))
        server._states["/dev/fake0"].backend.fail_reads = True
        # After capture gives up the server must close the stream (EOF)
        # instead of re-sending the stale frame forever.
        deadline = time.monotonic() + 15
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self.fail("stream did not end after capture died; clients "
                          "would re-receive the stale frame forever")
            sock.settimeout(remaining)
            try:
                chunk = resp.read1(65536)
            except TimeoutError:
                self.fail("stream neither delivered data nor ended after "
                          "capture died")
            if not chunk:
                break  # server closed the stream: contract holds

    def test_ensure_stream_restarts_dead_capture_thread(self):
        state = self._start_and_kill_capture("/dev/fake1")
        state.backend.fail_reads = False  # camera came back
        with self._get("/snapshot.jpg?cam=/dev/fake1") as resp:
            self.assertEqual(resp.status, 200)
            self._assert_jpeg(resp.read())
        self.assertTrue(state.capture_thread.is_alive(),
                        "ensure_stream must start a fresh capture thread")


class NoDefaultCameraTest(ServerTestBase):
    """Plain `webcamdemo serve` (no -d): _default_camera_id is None."""

    default_camera_id = None

    def test_api_cameras_keeps_enumeration_order(self):
        status, _, cams = self._request_json("/api/cameras")
        self.assertEqual(status, 200)
        self.assertEqual([c["id"] for c in cams], list(FAKE_IDS))

    def test_missing_cam_falls_back_to_first_camera(self):
        status, _, controls = self._request_json("/api/controls")
        self.assertEqual(status, 200)
        self.assertTrue(controls)
        with server._registry_lock:
            self.assertIn("/dev/fake0", server._states,
                          "request without cam= must open the first "
                          "enumerated camera")

    def test_no_cameras_found_is_404(self):
        saved = server._api_cache
        server._api_cache = (lambda: [], FakeBackend)
        try:
            status, ctype, body = self._request_json("/api/controls")
        finally:
            server._api_cache = saved
        self.assertEqual(status, 404)
        self.assertEqual(ctype, "application/json")
        self.assertIs(body["ok"], False)
        self.assertIn("no cameras found", body["error"])


if __name__ == "__main__":
    unittest.main()
