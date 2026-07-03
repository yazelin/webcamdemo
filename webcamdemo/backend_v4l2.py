"""Linux V4L2 backend, pure stdlib (ctypes + fcntl + mmap).

Struct layouts follow /usr/include/linux/videodev2.h and
/usr/include/linux/uvcvideo.h on x86_64 Linux.
"""

import ctypes
import errno
import fcntl
import glob
import mmap
import os
import select
import threading
from fractions import Fraction

from .model import CameraInfo, Control, FrameFormat


# ---------------------------------------------------------------------------
# ioctl number construction (asm-generic/ioctl.h)

_IOC_NONE, _IOC_WRITE, _IOC_READ = 0, 1, 2


def _IOC(direction, ioc_type, nr, size):
    return (direction << 30) | (size << 16) | (ord(ioc_type) << 8) | nr


def _IOR(t, nr, st):
    return _IOC(_IOC_READ, t, nr, ctypes.sizeof(st))


def _IOW(t, nr, st):
    return _IOC(_IOC_WRITE, t, nr, ctypes.sizeof(st))


def _IOWR(t, nr, st):
    return _IOC(_IOC_READ | _IOC_WRITE, t, nr, ctypes.sizeof(st))


# ---------------------------------------------------------------------------
# videodev2.h structs

class v4l2_capability(ctypes.Structure):
    _fields_ = [
        ("driver", ctypes.c_uint8 * 16),
        ("card", ctypes.c_uint8 * 32),
        ("bus_info", ctypes.c_uint8 * 32),
        ("version", ctypes.c_uint32),
        ("capabilities", ctypes.c_uint32),
        ("device_caps", ctypes.c_uint32),
        ("reserved", ctypes.c_uint32 * 3),
    ]


class v4l2_fmtdesc(ctypes.Structure):
    _fields_ = [
        ("index", ctypes.c_uint32),
        ("type", ctypes.c_uint32),
        ("flags", ctypes.c_uint32),
        ("description", ctypes.c_uint8 * 32),
        ("pixelformat", ctypes.c_uint32),
        ("mbus_code", ctypes.c_uint32),
        ("reserved", ctypes.c_uint32 * 3),
    ]


class v4l2_frmsize_discrete(ctypes.Structure):
    _fields_ = [("width", ctypes.c_uint32), ("height", ctypes.c_uint32)]


class v4l2_frmsize_stepwise(ctypes.Structure):
    _fields_ = [
        ("min_width", ctypes.c_uint32), ("max_width", ctypes.c_uint32),
        ("step_width", ctypes.c_uint32), ("min_height", ctypes.c_uint32),
        ("max_height", ctypes.c_uint32), ("step_height", ctypes.c_uint32),
    ]


class _frmsize_union(ctypes.Union):
    _fields_ = [
        ("discrete", v4l2_frmsize_discrete),
        ("stepwise", v4l2_frmsize_stepwise),
    ]


class v4l2_frmsizeenum(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [
        ("index", ctypes.c_uint32),
        ("pixel_format", ctypes.c_uint32),
        ("type", ctypes.c_uint32),
        ("u", _frmsize_union),
        ("reserved", ctypes.c_uint32 * 2),
    ]


class v4l2_fract(ctypes.Structure):
    _fields_ = [("numerator", ctypes.c_uint32), ("denominator", ctypes.c_uint32)]


class v4l2_frmival_stepwise(ctypes.Structure):
    _fields_ = [("min", v4l2_fract), ("max", v4l2_fract), ("step", v4l2_fract)]


class _frmival_union(ctypes.Union):
    _fields_ = [("discrete", v4l2_fract), ("stepwise", v4l2_frmival_stepwise)]


class v4l2_frmivalenum(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [
        ("index", ctypes.c_uint32),
        ("pixel_format", ctypes.c_uint32),
        ("width", ctypes.c_uint32),
        ("height", ctypes.c_uint32),
        ("type", ctypes.c_uint32),
        ("u", _frmival_union),
        ("reserved", ctypes.c_uint32 * 2),
    ]


class v4l2_queryctrl(ctypes.Structure):
    _fields_ = [
        ("id", ctypes.c_uint32),
        ("type", ctypes.c_uint32),
        ("name", ctypes.c_uint8 * 32),
        ("minimum", ctypes.c_int32),
        ("maximum", ctypes.c_int32),
        ("step", ctypes.c_int32),
        ("default_value", ctypes.c_int32),
        ("flags", ctypes.c_uint32),
        ("reserved", ctypes.c_uint32 * 2),
    ]


V4L2_CTRL_MAX_DIMS = 4


class v4l2_query_ext_ctrl(ctypes.Structure):
    _fields_ = [
        ("id", ctypes.c_uint32),
        ("type", ctypes.c_uint32),
        ("name", ctypes.c_char * 32),
        ("minimum", ctypes.c_int64),
        ("maximum", ctypes.c_int64),
        ("step", ctypes.c_uint64),
        ("default_value", ctypes.c_int64),
        ("flags", ctypes.c_uint32),
        ("elem_size", ctypes.c_uint32),
        ("elems", ctypes.c_uint32),
        ("nr_of_dims", ctypes.c_uint32),
        ("dims", ctypes.c_uint32 * V4L2_CTRL_MAX_DIMS),
        ("reserved", ctypes.c_uint32 * 32),
    ]


class _querymenu_union(ctypes.Union):
    _pack_ = 1
    _fields_ = [("name", ctypes.c_uint8 * 32), ("value", ctypes.c_int64)]


class v4l2_querymenu(ctypes.Structure):
    # __attribute__((packed)) in the header
    _pack_ = 1
    _anonymous_ = ("u",)
    _fields_ = [
        ("id", ctypes.c_uint32),
        ("index", ctypes.c_uint32),
        ("u", _querymenu_union),
        ("reserved", ctypes.c_uint32),
    ]


class v4l2_control(ctypes.Structure):
    _fields_ = [("id", ctypes.c_uint32), ("value", ctypes.c_int32)]


class v4l2_pix_format(ctypes.Structure):
    _fields_ = [
        ("width", ctypes.c_uint32),
        ("height", ctypes.c_uint32),
        ("pixelformat", ctypes.c_uint32),
        ("field", ctypes.c_uint32),
        ("bytesperline", ctypes.c_uint32),
        ("sizeimage", ctypes.c_uint32),
        ("colorspace", ctypes.c_uint32),
        ("priv", ctypes.c_uint32),
        ("flags", ctypes.c_uint32),
        ("ycbcr_enc", ctypes.c_uint32),
        ("quantization", ctypes.c_uint32),
        ("xfer_func", ctypes.c_uint32),
    ]


class _format_union(ctypes.Union):
    # raw_data is 200 bytes; the kernel union holds pointers (v4l2_window),
    # so it is 8-byte aligned on x86_64 -> total struct size 208
    _fields_ = [
        ("pix", v4l2_pix_format),
        ("raw_data", ctypes.c_uint8 * 200),
        ("_align", ctypes.c_uint64 * 25),
    ]


class v4l2_format(ctypes.Structure):
    _fields_ = [("type", ctypes.c_uint32), ("fmt", _format_union)]


class v4l2_captureparm(ctypes.Structure):
    _fields_ = [
        ("capability", ctypes.c_uint32),
        ("capturemode", ctypes.c_uint32),
        ("timeperframe", v4l2_fract),
        ("extendedmode", ctypes.c_uint32),
        ("readbuffers", ctypes.c_uint32),
        ("reserved", ctypes.c_uint32 * 4),
    ]


class _streamparm_union(ctypes.Union):
    _fields_ = [("capture", v4l2_captureparm), ("raw_data", ctypes.c_uint8 * 200)]


class v4l2_streamparm(ctypes.Structure):
    _fields_ = [("type", ctypes.c_uint32), ("parm", _streamparm_union)]


class timeval(ctypes.Structure):
    _fields_ = [("tv_sec", ctypes.c_long), ("tv_usec", ctypes.c_long)]


class v4l2_timecode(ctypes.Structure):
    _fields_ = [
        ("type", ctypes.c_uint32),
        ("flags", ctypes.c_uint32),
        ("frames", ctypes.c_uint8),
        ("seconds", ctypes.c_uint8),
        ("minutes", ctypes.c_uint8),
        ("hours", ctypes.c_uint8),
        ("userbits", ctypes.c_uint8 * 4),
    ]


class _buffer_m_union(ctypes.Union):
    _fields_ = [
        ("offset", ctypes.c_uint32),
        ("userptr", ctypes.c_ulong),
        ("planes", ctypes.c_void_p),
        ("fd", ctypes.c_int32),
    ]


class v4l2_buffer(ctypes.Structure):
    _fields_ = [
        ("index", ctypes.c_uint32),
        ("type", ctypes.c_uint32),
        ("bytesused", ctypes.c_uint32),
        ("flags", ctypes.c_uint32),
        ("field", ctypes.c_uint32),
        ("timestamp", timeval),
        ("timecode", v4l2_timecode),
        ("sequence", ctypes.c_uint32),
        ("memory", ctypes.c_uint32),
        ("m", _buffer_m_union),
        ("length", ctypes.c_uint32),
        ("reserved2", ctypes.c_uint32),
        ("request_fd", ctypes.c_int32),
    ]


class v4l2_requestbuffers(ctypes.Structure):
    _fields_ = [
        ("count", ctypes.c_uint32),
        ("type", ctypes.c_uint32),
        ("memory", ctypes.c_uint32),
        ("capabilities", ctypes.c_uint32),
        ("flags", ctypes.c_uint8),
        ("reserved", ctypes.c_uint8 * 3),
    ]


class uvc_xu_control_query(ctypes.Structure):
    _fields_ = [
        ("unit", ctypes.c_uint8),
        ("selector", ctypes.c_uint8),
        ("query", ctypes.c_uint8),
        ("size", ctypes.c_uint16),
        ("data", ctypes.POINTER(ctypes.c_uint8)),
    ]


# ---------------------------------------------------------------------------
# ioctl numbers

VIDIOC_QUERYCAP = _IOR('V', 0, v4l2_capability)
VIDIOC_ENUM_FMT = _IOWR('V', 2, v4l2_fmtdesc)
VIDIOC_S_FMT = _IOWR('V', 5, v4l2_format)
VIDIOC_REQBUFS = _IOWR('V', 8, v4l2_requestbuffers)
VIDIOC_QUERYBUF = _IOWR('V', 9, v4l2_buffer)
VIDIOC_QBUF = _IOWR('V', 15, v4l2_buffer)
VIDIOC_DQBUF = _IOWR('V', 17, v4l2_buffer)
VIDIOC_STREAMON = _IOW('V', 18, ctypes.c_int)
VIDIOC_STREAMOFF = _IOW('V', 19, ctypes.c_int)
VIDIOC_S_PARM = _IOWR('V', 22, v4l2_streamparm)
VIDIOC_G_CTRL = _IOWR('V', 27, v4l2_control)
VIDIOC_S_CTRL = _IOWR('V', 28, v4l2_control)
VIDIOC_QUERYCTRL = _IOWR('V', 36, v4l2_queryctrl)
VIDIOC_QUERYMENU = _IOWR('V', 37, v4l2_querymenu)
VIDIOC_ENUM_FRAMESIZES = _IOWR('V', 74, v4l2_frmsizeenum)
VIDIOC_ENUM_FRAMEINTERVALS = _IOWR('V', 75, v4l2_frmivalenum)
VIDIOC_QUERY_EXT_CTRL = _IOWR('V', 103, v4l2_query_ext_ctrl)
UVCIOC_CTRL_QUERY = _IOWR('u', 0x21, uvc_xu_control_query)


V4L2_CAP_VIDEO_CAPTURE = 0x00000001
V4L2_CAP_DEVICE_CAPS = 0x80000000
V4L2_BUF_TYPE_VIDEO_CAPTURE = 1
V4L2_FIELD_ANY = 0
V4L2_MEMORY_MMAP = 1
V4L2_FRMSIZE_TYPE_DISCRETE = 1
V4L2_FRMIVAL_TYPE_DISCRETE = 1

V4L2_CTRL_TYPE_INTEGER = 1
V4L2_CTRL_TYPE_BOOLEAN = 2
V4L2_CTRL_TYPE_MENU = 3
V4L2_CTRL_TYPE_BUTTON = 4
V4L2_CTRL_TYPE_CTRL_CLASS = 6
V4L2_CTRL_TYPE_INTEGER_MENU = 9

V4L2_CTRL_FLAG_DISABLED = 0x0001
V4L2_CTRL_FLAG_INACTIVE = 0x0010
V4L2_CTRL_FLAG_WRITE_ONLY = 0x0040
V4L2_CTRL_FLAG_NEXT_CTRL = 0x80000000
V4L2_CTRL_FLAG_NEXT_COMPOUND = 0x40000000

_TYPE_MAP = {
    V4L2_CTRL_TYPE_INTEGER: "int",
    V4L2_CTRL_TYPE_BOOLEAN: "bool",
    V4L2_CTRL_TYPE_MENU: "menu",
    V4L2_CTRL_TYPE_INTEGER_MENU: "menu",
    V4L2_CTRL_TYPE_BUTTON: "button",
}

# UVC class-specific request codes (linux/usb/video.h A.8)
UVC_SET_CUR = 0x01
UVC_GET_CUR = 0x81
UVC_GET_MIN = 0x82
UVC_GET_MAX = 0x83
UVC_GET_LEN = 0x85
UVC_GET_DEF = 0x87

# Logitech BRIO FoV XU: GUID 49e40215-f434-47fe-b158-0e885023e51b
LOGITECH_BRIO_GUID = b'\x15\x02\xe4\x49\x34\xf4\xfe\x47\xb1\x58\x0e\x88\x50\x23\xe5\x1b'
LOGITECH_BRIO_FOV_SEL = 0x05
LOGITECH_BRIO_FOV_DEV_MATCH = {
    "046d:085e", "046d:0943", "046d:0946", "046d:0919", "046d:086b", "046d:0944",
}
_FOV_MENU = {0: "90", 1: "78", 2: "65"}
_FOV_REFUSED_MSG = (
    "camera firmware refused FOV change - disable Show Mode and RightSight "
    "once via Logi Options+ on Windows; the flag persists in the camera"
)

# Logitech peripheral XU: GUID ffe52d21-8030-4e2c-82d9-f587d00540bd
# (bytes as in cameractrls LOGITECH_PERIPHERAL_GUID)
LOGITECH_PERIPHERAL_GUID = b'\x21\x2d\xe5\xff\x30\x80\x2c\x4e\x82\xd9\xf5\x87\xd0\x05\x40\xbd'
LOGITECH_PERIPHERAL_LED1_SEL = 0x09
LOGITECH_PERIPHERAL_LED1_LEN = 5
LOGITECH_PERIPHERAL_LED1_MODE_OFFSET = 1
_LED_MENU = {0: "off", 1: "on", 2: "blink", 3: "auto"}


def _name_to_id(name):
    # matches v4l2-ctl name2var(): alnum lowercased, non-alnum runs -> "_"
    out = []
    pending = False
    for ch in name:
        if ch.isalnum():
            if pending and out:
                out.append("_")
            pending = False
            out.append(ch.lower())
        else:
            pending = True
    return "".join(out)


def _cstr(arr):
    raw = bytes(arr)
    end = raw.find(b"\x00")
    if end >= 0:
        raw = raw[:end]
    return raw.decode(errors="replace")


def _fourcc_to_str(v):
    return bytes([v & 0xFF, (v >> 8) & 0xFF, (v >> 16) & 0xFF, (v >> 24) & 0xFF]).decode(errors="replace")


def _str_to_fourcc(s):
    b = s.encode()
    return b[0] | (b[1] << 8) | (b[2] << 16) | (b[3] << 24)


def _sysfs_usb_ids(dev_path):
    # /sys/class/video4linux/videoN/device -> USB interface dir; the USB
    # device dir with idVendor/idProduct is one of its ancestors
    name = os.path.basename(dev_path)
    p = os.path.realpath(f"/sys/class/video4linux/{name}/device")
    for _ in range(4):
        try:
            with open(os.path.join(p, "idVendor")) as f:
                vid = f.read().strip()
            with open(os.path.join(p, "idProduct")) as f:
                pid = f.read().strip()
            return f"{vid}:{pid}"
        except OSError:
            p = os.path.dirname(p)
            if p in ("/", ""):
                break
    return None


def _find_xu_unit_id(usb_ids, guid, base="/sys/bus/usb/devices"):
    """Walk USB config descriptors of the device with matching vid:pid and
    return the bUnitID of the VC Extension Unit carrying the given GUID."""
    if not usb_ids:
        return 0
    vid, pid = usb_ids.split(":")
    for d in glob.glob(os.path.join(base, "*")):
        try:
            with open(os.path.join(d, "idVendor")) as f:
                dvid = f.read().strip()
            with open(os.path.join(d, "idProduct")) as f:
                dpid = f.read().strip()
        except (OSError, NotADirectoryError):
            continue
        if (dvid, dpid) != (vid, pid):
            continue
        for df in glob.glob(d + "/*/descriptors") + [d + "/descriptors"]:
            try:
                with open(df, "rb") as f:
                    blob = f.read()
            except OSError:
                continue
            i = 0
            while i + 2 < len(blob):
                ln, dt = blob[i], blob[i + 1]
                if ln < 3:
                    break
                # CS_INTERFACE(0x24) + VC_EXTENSION_UNIT(0x06):
                # bUnitID at +3, guidExtensionCode at +4
                if dt == 0x24 and blob[i + 2] == 0x06 and blob[i + 4:i + 20] == guid:
                    return blob[i + 3]
                i += ln
    return 0


def _is_jpeg(data):
    """True if data starts with the JPEG SOI marker (0xFFD8)."""
    return data[:2] == b"\xff\xd8"


def _open_ok(path):
    try:
        return os.open(path, os.O_RDWR | os.O_NONBLOCK)
    except OSError:
        return None


def _query_cap(fd):
    cap = v4l2_capability()
    fcntl.ioctl(fd, VIDIOC_QUERYCAP, cap)
    return cap


def _has_capture_format(fd):
    fmt = v4l2_fmtdesc()
    fmt.index = 0
    fmt.type = V4L2_BUF_TYPE_VIDEO_CAPTURE
    try:
        fcntl.ioctl(fd, VIDIOC_ENUM_FMT, fmt)
        return True
    except OSError:
        return False


def _camera_info(path, fd):
    cap = _query_cap(fd)
    extra = {
        "driver": _cstr(cap.driver),
        "bus_info": _cstr(cap.bus_info),
    }
    usb = _sysfs_usb_ids(path)
    if usb:
        extra["usb"] = usb
    return CameraInfo(id=path, name=_cstr(cap.card), extra=extra)


def list_cameras():
    cameras = []
    paths = sorted(glob.glob("/dev/video*"),
                   key=lambda p: int("".join(filter(str.isdigit, p)) or 0))
    for path in paths:
        fd = _open_ok(path)
        if fd is None:
            continue
        try:
            cap = _query_cap(fd)
            caps = cap.device_caps if cap.capabilities & V4L2_CAP_DEVICE_CAPS else cap.capabilities
            if not caps & V4L2_CAP_VIDEO_CAPTURE:
                continue
            # metadata-only nodes report the capture cap but enum no formats
            if not _has_capture_format(fd):
                continue
            cameras.append(_camera_info(path, fd))
        except OSError:
            continue
        finally:
            os.close(fd)
    return cameras


class Backend:
    def __init__(self, camera_id):
        self._path = camera_id
        self._lock = threading.Lock()
        self._fd = os.open(camera_id, os.O_RDWR | os.O_NONBLOCK)
        self._streaming = False
        self._maps = []
        # v4l2 numeric control id by snake_case ctrl_id
        self._cid_map = {}
        self._usb_ids = _sysfs_usb_ids(camera_id)
        self._fov_unit = 0
        self._led_unit = 0
        if self._usb_ids in LOGITECH_BRIO_FOV_DEV_MATCH:
            self._fov_unit = _find_xu_unit_id(self._usb_ids, LOGITECH_BRIO_GUID)
        led_unit = _find_xu_unit_id(self._usb_ids, LOGITECH_PERIPHERAL_GUID)
        if led_unit and self._xu_get_len(led_unit, LOGITECH_PERIPHERAL_LED1_SEL) == LOGITECH_PERIPHERAL_LED1_LEN:
            self._led_unit = led_unit

    # -- lifecycle ----------------------------------------------------------

    def close(self):
        self.stop_stream()
        with self._lock:
            if self._fd is not None:
                os.close(self._fd)
                self._fd = None

    def info(self):
        with self._lock:
            return _camera_info(self._path, self._fd)

    # -- controls -----------------------------------------------------------

    def _query_menu(self, cid, minimum, maximum, is_int_menu):
        menu = {}
        for idx in range(minimum, maximum + 1):
            qm = v4l2_querymenu()
            qm.id = cid
            qm.index = idx
            try:
                fcntl.ioctl(self._fd, VIDIOC_QUERYMENU, qm)
            except OSError:
                continue  # hole in the menu
            menu[idx] = str(qm.value) if is_int_menu else _cstr(qm.name)
        return menu

    def _get_raw(self, cid):
        ctl = v4l2_control()
        ctl.id = cid
        fcntl.ioctl(self._fd, VIDIOC_G_CTRL, ctl)
        return ctl.value

    def _make_control(self, cid, ctype, name, minimum, maximum, step, default, flags):
        kind = _TYPE_MAP[ctype]
        menu = None
        if kind == "menu":
            menu = self._query_menu(cid, minimum, maximum,
                                    ctype == V4L2_CTRL_TYPE_INTEGER_MENU)
        value = None
        if kind != "button" and not flags & V4L2_CTRL_FLAG_WRITE_ONLY:
            try:
                value = self._get_raw(cid)
            except OSError:
                value = None
        ctrl_id = _name_to_id(name)
        self._cid_map[ctrl_id] = cid
        return Control(
            id=ctrl_id,
            name=name,
            type=kind,
            min=minimum if kind != "button" else None,
            max=maximum if kind != "button" else None,
            step=step if kind == "int" else None,
            default=default if kind != "button" else None,
            value=value,
            menu=menu,
            inactive=bool(flags & V4L2_CTRL_FLAG_INACTIVE),
        )

    def _enum_ext_ctrls(self):
        controls = []
        cid = 0
        while True:
            qec = v4l2_query_ext_ctrl()
            qec.id = cid | V4L2_CTRL_FLAG_NEXT_CTRL | V4L2_CTRL_FLAG_NEXT_COMPOUND
            try:
                fcntl.ioctl(self._fd, VIDIOC_QUERY_EXT_CTRL, qec)
            except OSError as e:
                if e.errno in (errno.ENOTTY, errno.ENOSYS) and not controls:
                    return None  # ioctl unsupported: caller falls back
                break
            cid = qec.id
            if qec.flags & V4L2_CTRL_FLAG_DISABLED:
                continue
            if qec.type == V4L2_CTRL_TYPE_CTRL_CLASS or qec.type not in _TYPE_MAP:
                continue
            controls.append(self._make_control(
                qec.id, qec.type, qec.name.decode(errors="replace"),
                int(qec.minimum), int(qec.maximum), int(qec.step),
                int(qec.default_value), qec.flags))
        return controls

    def _enum_legacy_ctrls(self):
        controls = []
        cid = 0
        while True:
            qc = v4l2_queryctrl()
            qc.id = cid | V4L2_CTRL_FLAG_NEXT_CTRL
            try:
                fcntl.ioctl(self._fd, VIDIOC_QUERYCTRL, qc)
            except OSError:
                break
            cid = qc.id
            if qc.flags & V4L2_CTRL_FLAG_DISABLED:
                continue
            if qc.type == V4L2_CTRL_TYPE_CTRL_CLASS or qc.type not in _TYPE_MAP:
                continue
            controls.append(self._make_control(
                qc.id, qc.type, _cstr(qc.name),
                qc.minimum, qc.maximum, qc.step, qc.default_value, qc.flags))
        return controls

    def list_controls(self):
        with self._lock:
            controls = self._enum_ext_ctrls()
            if controls is None:
                controls = self._enum_legacy_ctrls()
            if self._fov_unit:
                ctrl = self._fov_control()
                if ctrl is not None:
                    controls.append(ctrl)
            if self._led_unit:
                ctrl = self._led_control()
                if ctrl is not None:
                    controls.append(ctrl)
        return controls

    def get_control(self, ctrl_id):
        with self._lock:
            if ctrl_id == "logitech_brio_fov" and self._fov_unit:
                return self._xu_get(self._fov_unit, LOGITECH_BRIO_FOV_SEL, 1)[0]
            if ctrl_id == "logitech_led1_mode" and self._led_unit:
                buf = self._xu_get(self._led_unit, LOGITECH_PERIPHERAL_LED1_SEL,
                                   LOGITECH_PERIPHERAL_LED1_LEN)
                return buf[LOGITECH_PERIPHERAL_LED1_MODE_OFFSET]
            cid = self._resolve_cid(ctrl_id)
            try:
                return self._get_raw(cid)
            except OSError as e:
                raise ValueError(f"cannot read control '{ctrl_id}': {e.strerror}") from e

    def set_control(self, ctrl_id, value):
        with self._lock:
            if ctrl_id == "logitech_brio_fov" and self._fov_unit:
                self._set_fov(value)
                return
            if ctrl_id == "logitech_led1_mode" and self._led_unit:
                self._set_led(value)
                return
            cid = self._resolve_cid(ctrl_id)
            ctl = v4l2_control()
            ctl.id = cid
            ctl.value = value
            try:
                fcntl.ioctl(self._fd, VIDIOC_S_CTRL, ctl)
            except OSError as e:
                if e.errno == errno.ERANGE:
                    raise ValueError(f"value {value} out of range for '{ctrl_id}'") from e
                if e.errno == errno.EBUSY:
                    raise ValueError(f"control '{ctrl_id}' is busy (another control owns it)") from e
                if e.errno == errno.EACCES:
                    raise ValueError(f"control '{ctrl_id}' is read-only or inactive") from e
                raise ValueError(f"driver rejected {value} for '{ctrl_id}': {e.strerror}") from e

    def _resolve_cid(self, ctrl_id):
        if ctrl_id not in self._cid_map:
            # id map is built during enumeration; refresh once
            controls = self._enum_ext_ctrls()
            if controls is None:
                self._enum_legacy_ctrls()
        if ctrl_id not in self._cid_map:
            raise ValueError(f"unknown control id '{ctrl_id}'")
        return self._cid_map[ctrl_id]

    # -- Logitech XU controls ------------------------------------------------

    def _xu_query(self, unit, selector, query, data):
        buf = (ctypes.c_uint8 * len(data))(*data)
        q = uvc_xu_control_query(unit, selector, query, len(data), buf)
        fcntl.ioctl(self._fd, UVCIOC_CTRL_QUERY, q)
        return bytes(buf)

    def _xu_get(self, unit, selector, length):
        return self._xu_query(unit, selector, UVC_GET_CUR, bytes(length))

    def _xu_get_len(self, unit, selector):
        length = ctypes.c_uint16(0)
        q = uvc_xu_control_query(unit, selector, UVC_GET_LEN, 2,
                                 ctypes.cast(ctypes.pointer(length),
                                             ctypes.POINTER(ctypes.c_uint8)))
        try:
            fcntl.ioctl(self._fd, UVCIOC_CTRL_QUERY, q)
        except OSError:
            return 0
        return length.value

    def _fov_control(self):
        try:
            cur = self._xu_get(self._fov_unit, LOGITECH_BRIO_FOV_SEL, 1)[0]
        except OSError:
            return None
        try:
            default = self._xu_query(self._fov_unit, LOGITECH_BRIO_FOV_SEL,
                                     UVC_GET_DEF, b"\x00")[0]
        except OSError:
            default = None
        return Control(
            id="logitech_brio_fov", name="Field of View", type="menu",
            min=0, max=2, default=default, value=cur, menu=dict(_FOV_MENU))

    def _set_fov(self, value):
        if value not in _FOV_MENU:
            raise ValueError(f"invalid value {value} for 'logitech_brio_fov' (expected 0..2)")
        try:
            self._xu_query(self._fov_unit, LOGITECH_BRIO_FOV_SEL, UVC_SET_CUR,
                           bytes([value]))
            cur = self._xu_get(self._fov_unit, LOGITECH_BRIO_FOV_SEL, 1)[0]
        except OSError as e:
            raise ValueError(f"FOV XU query failed: {e.strerror}") from e
        if cur != value:
            raise ValueError(_FOV_REFUSED_MSG)

    def _led_control(self):
        try:
            buf = self._xu_get(self._led_unit, LOGITECH_PERIPHERAL_LED1_SEL,
                               LOGITECH_PERIPHERAL_LED1_LEN)
        except OSError:
            return None
        return Control(
            id="logitech_led1_mode", name="LED1 Mode", type="menu",
            min=0, max=3, value=buf[LOGITECH_PERIPHERAL_LED1_MODE_OFFSET],
            menu=dict(_LED_MENU))

    def _set_led(self, value):
        if value not in _LED_MENU:
            raise ValueError(f"invalid value {value} for 'logitech_led1_mode' (expected 0..3)")
        try:
            buf = bytearray(self._xu_get(self._led_unit, LOGITECH_PERIPHERAL_LED1_SEL,
                                         LOGITECH_PERIPHERAL_LED1_LEN))
            buf[LOGITECH_PERIPHERAL_LED1_MODE_OFFSET] = value
            self._xu_query(self._led_unit, LOGITECH_PERIPHERAL_LED1_SEL,
                           UVC_SET_CUR, bytes(buf))
            cur = self._xu_get(self._led_unit, LOGITECH_PERIPHERAL_LED1_SEL,
                               LOGITECH_PERIPHERAL_LED1_LEN)
        except OSError as e:
            raise ValueError(f"LED XU query failed: {e.strerror}") from e
        if cur[LOGITECH_PERIPHERAL_LED1_MODE_OFFSET] != value:
            raise ValueError(
                f"camera did not accept LED mode {value} "
                f"(current {cur[LOGITECH_PERIPHERAL_LED1_MODE_OFFSET]})")

    # -- formats --------------------------------------------------------------

    def list_formats(self):
        with self._lock:
            formats = []
            fidx = 0
            while True:
                fmt = v4l2_fmtdesc()
                fmt.index = fidx
                fmt.type = V4L2_BUF_TYPE_VIDEO_CAPTURE
                try:
                    fcntl.ioctl(self._fd, VIDIOC_ENUM_FMT, fmt)
                except OSError:
                    break
                fidx += 1
                fourcc = _fourcc_to_str(fmt.pixelformat)
                sidx = 0
                while True:
                    fs = v4l2_frmsizeenum()
                    fs.index = sidx
                    fs.pixel_format = fmt.pixelformat
                    try:
                        fcntl.ioctl(self._fd, VIDIOC_ENUM_FRAMESIZES, fs)
                    except OSError:
                        break
                    sidx += 1
                    if fs.type != V4L2_FRMSIZE_TYPE_DISCRETE:
                        continue
                    w, h = fs.discrete.width, fs.discrete.height
                    fps = []
                    iidx = 0
                    while True:
                        fi = v4l2_frmivalenum()
                        fi.index = iidx
                        fi.pixel_format = fmt.pixelformat
                        fi.width = w
                        fi.height = h
                        try:
                            fcntl.ioctl(self._fd, VIDIOC_ENUM_FRAMEINTERVALS, fi)
                        except OSError:
                            break
                        iidx += 1
                        if fi.type != V4L2_FRMIVAL_TYPE_DISCRETE:
                            continue
                        if fi.discrete.numerator:
                            fps.append(round(fi.discrete.denominator / fi.discrete.numerator, 3))
                    formats.append(FrameFormat(fourcc=fourcc, width=w, height=h,
                                               fps=sorted(fps, reverse=True)))
            return formats

    # -- streaming -------------------------------------------------------------

    def start_stream(self, width, height, fps=None):
        self.stop_stream()
        with self._lock:
            fmt = v4l2_format()
            fmt.type = V4L2_BUF_TYPE_VIDEO_CAPTURE
            fmt.fmt.pix.width = width
            fmt.fmt.pix.height = height
            fmt.fmt.pix.pixelformat = _str_to_fourcc("MJPG")
            fmt.fmt.pix.field = V4L2_FIELD_ANY
            fcntl.ioctl(self._fd, VIDIOC_S_FMT, fmt)
            got_w, got_h = fmt.fmt.pix.width, fmt.fmt.pix.height
            got_fourcc = _fourcc_to_str(fmt.fmt.pix.pixelformat)
            if (got_w, got_h) != (width, height) or got_fourcc != "MJPG":
                raise ValueError(
                    f"driver does not support MJPG {width}x{height} "
                    f"(offered {got_fourcc} {got_w}x{got_h})")

            if fps is not None:
                interval = 1 / Fraction(fps).limit_denominator(10000)
                parm = v4l2_streamparm()
                parm.type = V4L2_BUF_TYPE_VIDEO_CAPTURE
                parm.parm.capture.timeperframe.numerator = interval.numerator
                parm.parm.capture.timeperframe.denominator = interval.denominator
                fcntl.ioctl(self._fd, VIDIOC_S_PARM, parm)

            req = v4l2_requestbuffers()
            req.count = 4
            req.type = V4L2_BUF_TYPE_VIDEO_CAPTURE
            req.memory = V4L2_MEMORY_MMAP
            fcntl.ioctl(self._fd, VIDIOC_REQBUFS, req)
            if req.count < 1:
                raise RuntimeError("driver did not allocate capture buffers")

            try:
                for i in range(req.count):
                    buf = v4l2_buffer()
                    buf.index = i
                    buf.type = V4L2_BUF_TYPE_VIDEO_CAPTURE
                    buf.memory = V4L2_MEMORY_MMAP
                    fcntl.ioctl(self._fd, VIDIOC_QUERYBUF, buf)
                    self._maps.append(mmap.mmap(
                        self._fd, buf.length,
                        flags=mmap.MAP_SHARED, prot=mmap.PROT_READ,
                        offset=buf.m.offset))
                    fcntl.ioctl(self._fd, VIDIOC_QBUF, buf)
                fcntl.ioctl(self._fd, VIDIOC_STREAMON,
                            ctypes.c_int(V4L2_BUF_TYPE_VIDEO_CAPTURE))
            except OSError:
                self._release_buffers()
                raise
            self._streaming = True

    def _release_buffers(self):
        for m in self._maps:
            try:
                m.close()
            except (OSError, ValueError):
                pass
        self._maps = []
        req = v4l2_requestbuffers()
        req.count = 0
        req.type = V4L2_BUF_TYPE_VIDEO_CAPTURE
        req.memory = V4L2_MEMORY_MMAP
        try:
            fcntl.ioctl(self._fd, VIDIOC_REQBUFS, req)
        except OSError:
            pass

    def read_jpeg(self):
        if not self._streaming:
            raise RuntimeError("not streaming; call start_stream() first")
        for _ in range(2):
            data = self._dequeue_frame()
            if _is_jpeg(data):
                return data
        raise RuntimeError("camera produced no valid JPEG frame (bad SOI marker)")

    def _dequeue_frame(self):
        deadline_tries = 0
        while True:
            with self._lock:
                if not self._streaming or self._fd is None:
                    raise RuntimeError("stream stopped while reading")
                fd = self._fd
            # wait outside the lock so control ioctls stay responsive
            try:
                r, _, _ = select.select([fd], [], [], 5.0)
            except OSError as e:
                # fd closed by a concurrent close()
                raise RuntimeError("stream stopped while reading") from e
            if not r:
                deadline_tries += 1
                if deadline_tries >= 2:
                    raise RuntimeError("timed out waiting for a frame")
                continue
            with self._lock:
                if not self._streaming or self._fd is None:
                    raise RuntimeError("stream stopped while reading")
                buf = v4l2_buffer()
                buf.type = V4L2_BUF_TYPE_VIDEO_CAPTURE
                buf.memory = V4L2_MEMORY_MMAP
                try:
                    fcntl.ioctl(self._fd, VIDIOC_DQBUF, buf)
                except OSError as e:
                    if e.errno == errno.EAGAIN:
                        continue
                    raise RuntimeError(f"DQBUF failed: {e.strerror}") from e
                data = bytes(self._maps[buf.index][:buf.bytesused])
                fcntl.ioctl(self._fd, VIDIOC_QBUF, buf)
                return data

    def stop_stream(self):
        with self._lock:
            if not self._streaming and not self._maps:
                return
            self._streaming = False
            try:
                fcntl.ioctl(self._fd, VIDIOC_STREAMOFF,
                            ctypes.c_int(V4L2_BUF_TYPE_VIDEO_CAPTURE))
            except OSError:
                pass
            self._release_buffers()
