"""Smoke test for webcamdemo against a real camera. Plain script, no pytest.

Skips (exit 0) when no camera is attached. Restores every setting it touches.
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from webcamdemo import Camera, list_cameras

CHECKS = 0


def check(cond, what):
    global CHECKS
    if not cond:
        print("FAIL %s" % what, file=sys.stderr)
        sys.exit(1)
    CHECKS += 1


def open_camera():
    # Another process may hold the device; retry once before giving up.
    try:
        return Camera()
    except OSError:
        time.sleep(2)
        return Camera()


def pick_writable_int(controls):
    candidates = [c for c in controls
                  if c.type == "int" and not c.inactive
                  and c.min is not None and c.max is not None
                  and c.default is not None]
    for c in candidates:
        if c.id == "brightness":
            return c
    return candidates[0] if candidates else None


def pick_stream_size(formats):
    mjpg = [f for f in formats if f.fourcc.upper() in ("MJPG", "MJPEG", "JPEG")]
    pool = mjpg or formats
    best = min(pool, key=lambda f: f.width * f.height)
    return best.width, best.height


def main():
    cams = list_cameras()
    if not cams:
        print("SKIP no camera")
        return 0
    check(len(cams) >= 1, "list_cameras nonempty")

    with open_camera() as cam:
        info = cam.info()
        check(bool(info.id) and bool(info.name), "info has id and name")

        controls = cam.list_controls()
        check(len(controls) >= 10, ">=10 controls (got %d)" % len(controls))

        ctrl = pick_writable_int(controls)
        check(ctrl is not None, "found a writable int control")
        original = cam.get_control(ctrl.id)
        try:
            cam.set_control(ctrl.id, ctrl.min)
            check(cam.get_control(ctrl.id) == ctrl.min,
                  "set %s to min %d" % (ctrl.id, ctrl.min))
            cam.set_control(ctrl.id, ctrl.default)
            check(cam.get_control(ctrl.id) == ctrl.default,
                  "set %s to default %d" % (ctrl.id, ctrl.default))
        finally:
            cam.set_control(ctrl.id, original)
        check(cam.get_control(ctrl.id) == original,
              "restored %s to %d" % (ctrl.id, original))

        formats = cam.list_formats()
        check(len(formats) >= 1, "list_formats nonempty")

        width, height = pick_stream_size(formats)
        cam.start_stream(width, height)
        try:
            for i in range(3):
                frame = cam.read_jpeg()
                check(frame[:2] == b"\xff\xd8",
                      "frame %d is JPEG (%d bytes)" % (i, len(frame)))
        finally:
            cam.stop_stream()
        cam.stop_stream()  # idempotent

    print("PASS %d checks" % CHECKS)
    return 0


if __name__ == "__main__":
    sys.exit(main())
