"""webcamdemo: cross-platform webcam control and streaming."""

import sys

from .model import CameraInfo, Control, FrameFormat

__version__ = "0.1.0"

if sys.platform.startswith("linux"):
    from . import backend_v4l2 as _backend
elif sys.platform == "win32":
    from . import backend_dshow as _backend
else:
    raise ImportError(
        "webcamdemo has no backend for platform %r (supported: linux, win32)"
        % sys.platform
    )


def list_cameras():
    return _backend.list_cameras()


class Camera:
    """Context-manager wrapper around the platform Backend.

    camera_id=None picks the first camera reported by list_cameras().
    """

    def __init__(self, camera_id=None):
        if camera_id is None:
            cams = list_cameras()
            if not cams:
                raise RuntimeError("no cameras found")
            camera_id = cams[0].id
        self._backend = _backend.Backend(camera_id)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False

    def close(self):
        self._backend.close()

    def info(self):
        return self._backend.info()

    def list_controls(self):
        return self._backend.list_controls()

    def get_control(self, ctrl_id):
        return self._backend.get_control(ctrl_id)

    def set_control(self, ctrl_id, value):
        self._backend.set_control(ctrl_id, value)

    def list_formats(self):
        return self._backend.list_formats()

    def start_stream(self, width, height, fps=None):
        self._backend.start_stream(width, height, fps)

    def read_jpeg(self):
        return self._backend.read_jpeg()

    def stop_stream(self):
        self._backend.stop_stream()


__all__ = [
    "__version__",
    "CameraInfo",
    "Control",
    "FrameFormat",
    "Camera",
    "list_cameras",
]
