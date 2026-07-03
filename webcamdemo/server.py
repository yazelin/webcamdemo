"""HTTP server: webcam control API plus MJPEG preview. Stdlib only."""

import json
import math
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

DEFAULT_WIDTH = 1280
DEFAULT_HEIGHT = 720
DEFAULT_FPS = 30.0
MAX_BODY_BYTES = 1 << 20  # cap POST bodies; also rejects bogus Content-Length
MAX_CAPTURE_FAILURES = 50  # consecutive read errors before capture gives up

# Backend is resolved lazily so this module imports cleanly even while the
# package __init__ / backend modules are still being written.
_api_cache = None
_api_lock = threading.Lock()


def _load_backend():
    try:
        from . import list_cameras, Camera
        return list_cameras, Camera
    except ImportError:
        pass
    name = "backend_dshow" if sys.platform == "win32" else "backend_v4l2"
    import importlib
    if __package__:
        try:
            mod = importlib.import_module("." + name, __package__)
            return mod.list_cameras, mod.Backend
        except ImportError:
            pass
    mod = importlib.import_module(name)
    return mod.list_cameras, mod.Backend


def _api():
    global _api_cache
    with _api_lock:
        if _api_cache is None:
            _api_cache = _load_backend()
        return _api_cache


def _list_cameras():
    return _api()[0]()


def _open_backend(camera_id):
    return _api()[1](camera_id)


class CameraState:
    """One backend plus its capture thread and most recent JPEG frame."""

    def __init__(self, backend):
        self.backend = backend
        self.lock = threading.Lock()       # serializes stream (re)configuration
        self.cond = threading.Condition()  # guards latest_jpeg / frame_seq
        self.latest_jpeg = None
        self.frame_seq = 0
        self.capture_thread = None
        self.stop_event = None
        self.stream_params = None          # (width, height, fps) last started

    def _default_params(self):
        """Pick a startup mode the camera actually offers (prefer 720p30)."""
        try:
            formats = [f for f in self.backend.list_formats()
                       if f.fourcc == "MJPG"]
        except Exception:
            formats = []
        best = None
        for f in formats:
            if (f.width, f.height) == (DEFAULT_WIDTH, DEFAULT_HEIGHT):
                best = f
                break
            if best is None or f.width * f.height > best.width * best.height:
                best = f
        if best is None:
            return (DEFAULT_WIDTH, DEFAULT_HEIGHT, DEFAULT_FPS)
        fps = min(best.fps, key=lambda r: abs(r - DEFAULT_FPS)) if best.fps else None
        return (best.width, best.height, fps)

    def ensure_stream(self):
        with self.lock:
            if self.capture_thread is not None and self.capture_thread.is_alive():
                return
            self.backend.stop_stream()
            params = self.stream_params or self._default_params()
            self.backend.start_stream(*params)
            self.stream_params = params
            self._start_capture()

    def restart_stream(self, width, height, fps):
        with self.lock:
            old = self.stream_params
            self._stop_capture()
            try:
                self.backend.start_stream(width, height, fps)
            except Exception:
                # Roll back to the previous format so connected MJPEG
                # clients are not left frozen on a dead stream.
                if old is not None:
                    try:
                        self.backend.start_stream(*old)
                        self._start_capture()
                    except Exception:
                        pass
                raise
            self.stream_params = (width, height, fps)
            self._start_capture()

    def shutdown(self):
        with self.lock:
            self._stop_capture()
            self.backend.close()

    def _start_capture(self):
        stop = threading.Event()
        thread = threading.Thread(
            target=self._capture_loop, args=(stop,),
            name="capture-" + str(self.backend), daemon=True)
        self.stop_event = stop
        self.capture_thread = thread
        thread.start()

    def _stop_capture(self):
        thread = self.capture_thread
        self.capture_thread = None
        if thread is not None and thread.is_alive():
            self.stop_event.set()
            self.backend.stop_stream()  # unblocks a pending read_jpeg
            thread.join(timeout=5.0)
        else:
            self.backend.stop_stream()

    def _capture_loop(self, stop):
        failures = 0
        while not stop.is_set():
            try:
                frame = self.backend.read_jpeg()
            except Exception:
                if stop.is_set():
                    return
                failures += 1
                if failures >= MAX_CAPTURE_FAILURES:
                    # Terminal failure (e.g. camera unplugged): drop the
                    # stale frame and exit so waiters see the stream die
                    # and ensure_stream() can retry from scratch.
                    with self.cond:
                        self.latest_jpeg = None
                        self.cond.notify_all()
                    return
                time.sleep(0.1)
                continue
            failures = 0
            with self.cond:
                self.latest_jpeg = frame
                self.frame_seq += 1
                self.cond.notify_all()

    def wait_frame(self, timeout=5.0):
        deadline = time.monotonic() + timeout
        with self.cond:
            while self.latest_jpeg is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self.cond.wait(remaining)
            return self.latest_jpeg

    def frames(self):
        """Yield the newest frame as it arrives; slow consumers skip frames.

        On a stall the last frame is re-sent so dead sockets get detected.
        Ends when no frame is available at all (capture never produced one
        or died), so the handler can close the connection instead of
        spinning forever without ever writing.
        """
        last_seq = 0
        while True:
            with self.cond:
                while self.frame_seq == last_seq:
                    if not self.cond.wait(timeout=5.0):
                        break
                if self.latest_jpeg is None:
                    return
                last_seq = self.frame_seq
                frame = self.latest_jpeg
            yield frame


_states = {}
_registry_lock = threading.Lock()
_default_camera_id = None


def _normalize_cam_id(cam_id):
    # Collapse path aliases (/dev/v4l/by-id/..., /dev/./video0) so one
    # physical device never gets two competing backends.
    if sys.platform != "win32" and os.path.exists(cam_id):
        return os.path.realpath(cam_id)
    return cam_id


def _get_state(cam_id):
    cam_id = _normalize_cam_id(cam_id)
    with _registry_lock:
        state = _states.get(cam_id)
        if state is None:
            state = CameraState(_open_backend(cam_id))
            _states[cam_id] = state
        return state


def _resolve_cam(cam):
    if cam:
        return cam
    if _default_camera_id:
        return _default_camera_id
    cameras = _list_cameras()
    if not cameras:
        raise LookupError("no cameras found")
    return cameras[0].id


class RequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    # Bound stuck socket reads/writes (e.g. a client that never sends the
    # body it promised) so handler threads cannot leak forever.
    timeout = 60

    def log_message(self, format, *args):
        if os.environ.get("WEBCAMDEMO_DEBUG") == "1":
            sys.stderr.write("%s - %s\n" % (self.address_string(), format % args))

    def _send_json(self, obj, status=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        try:
            self._route_get(parsed.path, query)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except LookupError as exc:
            self._send_json({"ok": False, "error": str(exc)}, 404)
        except ValueError as exc:
            self._send_json({"ok": False, "error": str(exc)}, 400)
        except Exception as exc:
            self._send_json({"ok": False, "error": str(exc)}, 500)

    def _route_get(self, path, query):
        if path == "/":
            self._send_index()
        elif path == "/api/cameras":
            cams = [c.to_dict() for c in _list_cameras()]
            # serve(-d) default first so the UI preselects it
            cams.sort(key=lambda c: c["id"] != _default_camera_id)
            self._send_json(cams)
        elif path == "/api/controls":
            state = _get_state(_resolve_cam(query.get("cam")))
            self._send_json([c.to_dict() for c in state.backend.list_controls()])
        elif path == "/api/formats":
            state = _get_state(_resolve_cam(query.get("cam")))
            self._send_json([f.to_dict() for f in state.backend.list_formats()])
        elif path == "/stream.mjpg":
            self._handle_stream(query)
        elif path == "/snapshot.jpg":
            self._handle_snapshot(query)
        else:
            self._send_json({"ok": False, "error": "not found"}, 404)

    def _send_index(self):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "static", "index.html")
        with open(path, "rb") as fh:
            body = fh.read()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle_snapshot(self, query):
        state = _get_state(_resolve_cam(query.get("cam")))
        state.ensure_stream()
        frame = state.wait_frame(timeout=5.0)
        if frame is None:
            self._send_json({"ok": False, "error": "no frame available"}, 503)
            return
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(frame)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(frame)

    def _handle_stream(self, query):
        state = _get_state(_resolve_cam(query.get("cam")))
        state.ensure_stream()
        self.close_connection = True
        self.send_response(200)
        self.send_header("Content-Type",
                         "multipart/x-mixed-replace; boundary=frame")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            for frame in state.frames():
                self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\n"
                                 b"Content-Length: %d\r\n\r\n" % len(frame))
                self.wfile.write(frame)
                self.wfile.write(b"\r\n")
        except (BrokenPipeError, ConnectionResetError, TimeoutError, OSError):
            pass

    def do_POST(self):
        parsed = urlparse(self.path)
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length < 0 or length > MAX_BODY_BYTES:
                raise ValueError("bad Content-Length")
            body = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(body, dict):
                raise ValueError("body must be a JSON object")
        except ValueError:
            self._send_json({"ok": False, "error": "invalid request body"}, 400)
            return
        try:
            if parsed.path == "/api/control":
                self._handle_set_control(body)
            elif parsed.path == "/api/stream":
                self._handle_set_stream(body)
            else:
                self._send_json({"ok": False, "error": "not found"}, 404)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except LookupError as exc:
            self._send_json({"ok": False, "error": str(exc)}, 404)
        except ValueError as exc:
            self._send_json({"ok": False, "error": str(exc)}, 400)
        except Exception as exc:
            self._send_json({"ok": False, "error": str(exc)}, 500)

    def _handle_set_control(self, body):
        state = _get_state(_resolve_cam(body.get("cam")))
        ctrl_id = body.get("id")
        value = body.get("value")
        if not isinstance(ctrl_id, str) or not isinstance(value, (bool, int)):
            raise ValueError('body must have "id" (string) and "value" (integer)')
        try:
            state.backend.set_control(ctrl_id, int(value))
        except ValueError as exc:
            fresh = [c.to_dict() for c in state.backend.list_controls()]
            self._send_json({"ok": False, "error": str(exc), "controls": fresh}, 400)
            return
        fresh = [c.to_dict() for c in state.backend.list_controls()]
        self._send_json({"ok": True, "controls": fresh})

    def _handle_set_stream(self, body):
        state = _get_state(_resolve_cam(body.get("cam")))
        try:
            width = int(body["width"])
            height = int(body["height"])
        except (KeyError, TypeError):
            raise ValueError('body must have integer "width" and "height"')
        fps = body.get("fps")
        if fps is not None:
            try:
                fps = float(fps)
            except (TypeError, ValueError):
                raise ValueError('"fps" must be a number')
            if not math.isfinite(fps) or fps <= 0:
                raise ValueError('"fps" must be a positive finite number')
        state.restart_stream(width, height, fps)
        self._send_json({"ok": True})


def serve(camera_id=None, host="127.0.0.1", port=8600):
    global _default_camera_id
    _default_camera_id = camera_id
    httpd = ThreadingHTTPServer((host, port), RequestHandler)
    sys.stderr.write("webcamdemo server on http://%s:%d\n" % (host, port))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        with _registry_lock:
            states = list(_states.values())
            _states.clear()
        for state in states:
            try:
                state.shutdown()
            except Exception:
                pass


if __name__ == "__main__":
    serve()
