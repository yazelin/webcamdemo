"""Command line interface for webcamdemo."""

import argparse
import sys

from . import Camera, list_cameras, __version__


def _fmt_table(rows, headers):
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    lines = []
    lines.append("  ".join(h.ljust(widths[i]) for i, h in enumerate(headers)))
    lines.append("  ".join("-" * widths[i] for i in range(len(headers))))
    for row in rows:
        lines.append("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)))
    return "\n".join(lines)


def _ctrl_range(ctrl):
    if ctrl.type == "menu" and ctrl.menu:
        return ", ".join("%d=%s" % (k, v) for k, v in sorted(ctrl.menu.items()))
    if ctrl.type in ("int", "bool") and ctrl.min is not None and ctrl.max is not None:
        s = "%d..%d" % (ctrl.min, ctrl.max)
        if ctrl.step not in (None, 0, 1):
            s += " step %d" % ctrl.step
        if ctrl.default is not None:
            s += " (default %d)" % ctrl.default
        return s
    return ""


def cmd_list(args):
    cams = list_cameras()
    if not cams:
        raise RuntimeError("no cameras found")
    rows = [(c.id, c.name) for c in cams]
    print(_fmt_table(rows, ("ID", "NAME")))
    return 0


def cmd_controls(args):
    with Camera(args.device) as cam:
        controls = cam.list_controls()
    rows = []
    for c in controls:
        value = "" if c.value is None else str(c.value)
        flags = "inactive" if c.inactive else ""
        rows.append((c.id, c.type, value, _ctrl_range(c), flags))
    print(_fmt_table(rows, ("ID", "TYPE", "VALUE", "RANGE/MENU", "FLAGS")))
    return 0


def cmd_formats(args):
    with Camera(args.device) as cam:
        formats = cam.list_formats()
    rows = []
    for f in formats:
        fps = ", ".join(("%g" % r) for r in f.fps)
        rows.append((f.fourcc, "%dx%d" % (f.width, f.height), fps))
    print(_fmt_table(rows, ("FOURCC", "SIZE", "FPS")))
    return 0


def cmd_get(args):
    with Camera(args.device) as cam:
        print(cam.get_control(args.ctrl_id))
    return 0


def _resolve_value(cam, ctrl_id, raw):
    try:
        return int(raw)
    except ValueError:
        pass
    # Not an int: try matching a menu label case-insensitively.
    for c in cam.list_controls():
        if c.id == ctrl_id and c.menu:
            for k, label in c.menu.items():
                if label.lower() == raw.lower():
                    return k
            raise ValueError(
                "%r does not match any menu label for %s (choices: %s)"
                % (raw, ctrl_id, ", ".join(c.menu.values()))
            )
    raise ValueError("value must be an integer (or a menu label for menu controls)")


def cmd_set(args):
    with Camera(args.device) as cam:
        value = _resolve_value(cam, args.ctrl_id, args.value)
        cam.set_control(args.ctrl_id, value)
        print("%s = %d" % (args.ctrl_id, cam.get_control(args.ctrl_id)))
    return 0


def _parse_size(size):
    try:
        w, h = size.lower().split("x", 1)
        return int(w), int(h)
    except ValueError:
        raise ValueError("--size must look like WIDTHxHEIGHT, e.g. 1280x720")


def _pick_size(cam):
    # Default snapshot size: largest MJPG format, else largest of any format.
    formats = cam.list_formats()
    if not formats:
        raise RuntimeError("camera reports no frame formats")
    mjpg = [f for f in formats if f.fourcc.upper() in ("MJPG", "MJPEG", "JPEG")]
    pool = mjpg or formats
    best = max(pool, key=lambda f: f.width * f.height)
    return best.width, best.height


def cmd_snapshot(args):
    with Camera(args.device) as cam:
        if args.size:
            width, height = _parse_size(args.size)
        else:
            width, height = _pick_size(cam)
        cam.start_stream(width, height)
        try:
            # A few warm-up frames let auto-exposure settle; keep the last.
            frame = b""
            for _ in range(5):
                frame = cam.read_jpeg()
        finally:
            cam.stop_stream()
    with open(args.output, "wb") as fh:
        fh.write(frame)
    print("wrote %s (%dx%d, %d bytes)" % (args.output, width, height, len(frame)))
    return 0


def cmd_serve(args):
    from . import server
    server.serve(camera_id=args.device, host=args.host, port=args.port)
    return 0


def build_parser():
    parser = argparse.ArgumentParser(
        prog="webcamdemo",
        description="Webcam control and streaming demo.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)

    def add_device(p):
        p.add_argument("-d", "--device", default=None,
                       help="camera id (default: first camera)")

    p = sub.add_parser("list", help="list cameras")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("controls", help="list controls")
    add_device(p)
    p.set_defaults(func=cmd_controls)

    p = sub.add_parser("formats", help="list frame formats")
    add_device(p)
    p.set_defaults(func=cmd_formats)

    p = sub.add_parser("get", help="get a control value")
    add_device(p)
    p.add_argument("ctrl_id", metavar="CTRL_ID")
    p.set_defaults(func=cmd_get)

    p = sub.add_parser("set", help="set a control value")
    add_device(p)
    p.add_argument("ctrl_id", metavar="CTRL_ID")
    p.add_argument("value", metavar="VALUE",
                   help="integer value, or a menu label for menu controls")
    p.set_defaults(func=cmd_set)

    p = sub.add_parser("snapshot", help="capture a single JPEG frame")
    add_device(p)
    p.add_argument("-o", "--output", default="webcam.jpg",
                   help="output file (default: webcam.jpg)")
    p.add_argument("--size", default=None, help="frame size as WIDTHxHEIGHT")
    p.set_defaults(func=cmd_snapshot)

    p = sub.add_parser("serve", help="run the HTTP server")
    add_device(p)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8600)
    p.set_defaults(func=cmd_serve)

    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        return 1
    except (ValueError, RuntimeError, OSError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
