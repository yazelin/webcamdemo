"""Pure unit tests for webcamdemo.backend_v4l2 (no camera, no /dev/video*).

Covers: control name normalization, ctypes struct ABI sizes and ioctl
numbers, fourcc round-trips, XU unit-id discovery against a synthetic
sysfs tree, JPEG SOI validation, and control value validation paths of
Backend without opening a video device.
"""

import ctypes
import errno
import os
import sys
import tempfile
import threading
import unittest

try:
    from webcamdemo import backend_v4l2 as b
except ImportError as exc:  # no fcntl/V4L2 stack (e.g. Windows CI)
    raise unittest.SkipTest("V4L2 backend not importable: %s" % exc)


# ---------------------------------------------------------------------------
# control name normalization (v4l2-ctl name2var rules)

class NameToIdTest(unittest.TestCase):
    def test_simple_words(self):
        self.assertEqual(b._name_to_id("Brightness"), "brightness")
        self.assertEqual(b._name_to_id("White Balance Temperature"),
                         "white_balance_temperature")

    def test_comma_and_space_collapse_to_single_underscore(self):
        self.assertEqual(b._name_to_id("Exposure, Auto"), "exposure_auto")
        self.assertEqual(b._name_to_id("Focus, Automatic Continuous"),
                         "focus_automatic_continuous")

    def test_parentheses(self):
        self.assertEqual(b._name_to_id("Exposure (Absolute)"),
                         "exposure_absolute")

    def test_no_leading_or_trailing_underscore(self):
        self.assertEqual(b._name_to_id("  Gain  "), "gain")
        self.assertEqual(b._name_to_id("(test)"), "test")

    def test_digits_kept(self):
        self.assertEqual(b._name_to_id("H264 Level"), "h264_level")
        self.assertEqual(b._name_to_id("LED1 Mode"), "led1_mode")

    def test_punctuation_runs_collapse(self):
        self.assertEqual(b._name_to_id("a--b"), "a_b")
        self.assertEqual(b._name_to_id("a - , b"), "a_b")

    def test_empty_and_all_punctuation(self):
        self.assertEqual(b._name_to_id(""), "")
        self.assertEqual(b._name_to_id("---"), "")


# ---------------------------------------------------------------------------
# ctypes struct ABI + ioctl numbers
#
# Expected values verified on this machine by compiling against
# /usr/include/linux/videodev2.h and /usr/include/linux/uvcvideo.h
# (x86_64, LP64). They hold for any 64-bit little-endian Linux ABI
# (x86_64/aarch64), so guard on pointer size + byteorder + platform.

_IS_LINUX_64 = (
    sys.platform.startswith("linux")
    and ctypes.sizeof(ctypes.c_void_p) == 8
    and ctypes.sizeof(ctypes.c_long) == 8
    and sys.byteorder == "little"
)

EXPECTED_SIZES = {
    "v4l2_capability": 104,
    "v4l2_fmtdesc": 64,
    "v4l2_frmsizeenum": 44,
    "v4l2_frmivalenum": 52,
    "v4l2_queryctrl": 68,
    "v4l2_query_ext_ctrl": 232,
    "v4l2_querymenu": 44,       # __attribute__((packed)) in the header
    "v4l2_control": 8,
    "v4l2_format": 208,
    "v4l2_streamparm": 204,
    "v4l2_buffer": 88,
    "v4l2_requestbuffers": 20,
    "uvc_xu_control_query": 16,
}

EXPECTED_IOCTLS = {
    "VIDIOC_QUERYCAP": 0x80685600,
    "VIDIOC_ENUM_FMT": 0xC0405602,
    "VIDIOC_S_FMT": 0xC0D05605,
    "VIDIOC_REQBUFS": 0xC0145608,
    "VIDIOC_QUERYBUF": 0xC0585609,
    "VIDIOC_QBUF": 0xC058560F,
    "VIDIOC_DQBUF": 0xC0585611,
    "VIDIOC_STREAMON": 0x40045612,
    "VIDIOC_STREAMOFF": 0x40045613,
    "VIDIOC_S_PARM": 0xC0CC5616,
    "VIDIOC_G_CTRL": 0xC008561B,
    "VIDIOC_S_CTRL": 0xC008561C,
    "VIDIOC_QUERYCTRL": 0xC0445624,
    "VIDIOC_QUERYMENU": 0xC02C5625,
    "VIDIOC_ENUM_FRAMESIZES": 0xC02C564A,
    "VIDIOC_ENUM_FRAMEINTERVALS": 0xC034564B,
    "VIDIOC_QUERY_EXT_CTRL": 0xC0E85667,
    "UVCIOC_CTRL_QUERY": 0xC0107521,
}


@unittest.skipUnless(_IS_LINUX_64, "kernel ABI values verified for 64-bit Linux")
class StructAbiTest(unittest.TestCase):
    def test_struct_sizes_match_kernel_headers(self):
        for name, size in EXPECTED_SIZES.items():
            with self.subTest(struct=name):
                self.assertEqual(ctypes.sizeof(getattr(b, name)), size)

    def test_ioctl_numbers(self):
        for name, value in EXPECTED_IOCTLS.items():
            with self.subTest(ioctl=name):
                self.assertEqual(getattr(b, name) & 0xFFFFFFFF, value)

    def test_uvcioc_ctrl_query_exact(self):
        self.assertEqual(b.UVCIOC_CTRL_QUERY, 0xC0107521)

    def test_ioc_helper_encoding(self):
        # dir<<30 | size<<16 | type<<8 | nr
        self.assertEqual(b._IOC(0, 'V', 0, 0), ord('V') << 8)
        self.assertEqual(b._IOW('V', 18, ctypes.c_int),
                         (1 << 30) | (4 << 16) | (ord('V') << 8) | 18)


# ---------------------------------------------------------------------------
# fourcc encode/decode

class FourccTest(unittest.TestCase):
    def test_mjpg_encodes_little_endian(self):
        self.assertEqual(b._str_to_fourcc("MJPG"), 0x47504A4D)

    def test_round_trip_str_int_str(self):
        for code in ("MJPG", "YUYV", "H264", "NV12"):
            with self.subTest(code=code):
                self.assertEqual(b._fourcc_to_str(b._str_to_fourcc(code)), code)

    def test_round_trip_int_str_int(self):
        v = 0x56595559  # YUYV
        self.assertEqual(b._str_to_fourcc(b._fourcc_to_str(v)), v)
        self.assertEqual(b._fourcc_to_str(v), "YUYV")


class CstrTest(unittest.TestCase):
    def test_stops_at_nul(self):
        arr = (ctypes.c_uint8 * 8)(*b"abc\x00xyz\x00")
        self.assertEqual(b._cstr(arr), "abc")

    def test_no_nul_uses_whole_buffer(self):
        arr = (ctypes.c_uint8 * 3)(*b"abc")
        self.assertEqual(b._cstr(arr), "abc")


# ---------------------------------------------------------------------------
# JPEG SOI validation

class IsJpegTest(unittest.TestCase):
    def test_valid_soi_accepted(self):
        self.assertTrue(b._is_jpeg(b"\xff\xd8\xff\xe0" + b"\x00" * 16))
        self.assertTrue(b._is_jpeg(b"\xff\xd8"))

    def test_garbage_rejected(self):
        self.assertFalse(b._is_jpeg(b"\x00\x00\x00\x00"))
        self.assertFalse(b._is_jpeg(b"\xd8\xff swapped"))
        self.assertFalse(b._is_jpeg(b"RIFFxxxxWEBP"))

    def test_short_and_empty_rejected(self):
        self.assertFalse(b._is_jpeg(b""))
        self.assertFalse(b._is_jpeg(b"\xff"))


# ---------------------------------------------------------------------------
# XU unit-id discovery over a synthetic sysfs tree

BRIO_GUID = b.LOGITECH_BRIO_GUID


def _xu_descriptor(unit_id, guid):
    """CS_INTERFACE (0x24) / VC_EXTENSION_UNIT (0x06) descriptor."""
    body = bytes([0x24, 0x06, unit_id]) + guid + bytes([1, 1, 2, 0, 0])
    return bytes([len(body) + 1]) + body


def _descriptor_blob(unit_id, guid):
    device_desc = bytes([18, 0x01]) + b"\x00" * 16
    config_desc = bytes([9, 0x02]) + b"\x00" * 7
    return device_desc + config_desc + _xu_descriptor(unit_id, guid)


def _make_usb_device(base, name, vid, pid, blob=None, child_blob=None):
    d = os.path.join(base, name)
    os.makedirs(d)
    with open(os.path.join(d, "idVendor"), "w") as f:
        f.write(vid + "\n")
    with open(os.path.join(d, "idProduct"), "w") as f:
        f.write(pid + "\n")
    if blob is not None:
        with open(os.path.join(d, "descriptors"), "wb") as f:
            f.write(blob)
    if child_blob is not None:
        iface = os.path.join(d, name + ":1.0")
        os.makedirs(iface)
        with open(os.path.join(iface, "descriptors"), "wb") as f:
            f.write(child_blob)
    return d


class FindXuUnitIdTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = self._tmp.name
        self.addCleanup(self._tmp.cleanup)

    def test_found_for_matching_vid_pid(self):
        _make_usb_device(self.base, "1-2", "046d", "085e",
                         blob=_descriptor_blob(12, BRIO_GUID))
        self.assertEqual(
            b._find_xu_unit_id("046d:085e", BRIO_GUID, base=self.base), 12)

    def test_found_in_interface_child_dir(self):
        _make_usb_device(self.base, "1-3", "046d", "085e",
                         child_blob=_descriptor_blob(7, BRIO_GUID))
        self.assertEqual(
            b._find_xu_unit_id("046d:085e", BRIO_GUID, base=self.base), 7)

    def test_not_found_for_non_matching_vid_pid(self):
        # device exists and carries the GUID, but ids differ from query
        _make_usb_device(self.base, "1-4", "dead", "beef",
                         blob=_descriptor_blob(12, BRIO_GUID))
        self.assertEqual(
            b._find_xu_unit_id("046d:085e", BRIO_GUID, base=self.base), 0)

    def test_not_found_when_guid_absent(self):
        other_guid = bytes(range(16))
        _make_usb_device(self.base, "1-5", "046d", "085e",
                         blob=_descriptor_blob(12, other_guid))
        self.assertEqual(
            b._find_xu_unit_id("046d:085e", BRIO_GUID, base=self.base), 0)

    def test_skips_earlier_xu_with_other_guid(self):
        other_guid = bytes(range(16))
        blob = (_descriptor_blob(3, other_guid)
                + _xu_descriptor(9, BRIO_GUID))
        _make_usb_device(self.base, "1-6", "046d", "085e", blob=blob)
        self.assertEqual(
            b._find_xu_unit_id("046d:085e", BRIO_GUID, base=self.base), 9)

    def test_ignores_plain_files_and_dirs_without_ids(self):
        with open(os.path.join(self.base, "notadir"), "w") as f:
            f.write("x")
        os.makedirs(os.path.join(self.base, "usb1"))  # no idVendor/idProduct
        _make_usb_device(self.base, "1-7", "046d", "085e",
                         blob=_descriptor_blob(5, BRIO_GUID))
        self.assertEqual(
            b._find_xu_unit_id("046d:085e", BRIO_GUID, base=self.base), 5)

    def test_none_usb_ids_returns_zero(self):
        self.assertEqual(b._find_xu_unit_id(None, BRIO_GUID, base=self.base), 0)

    def test_truncated_descriptor_does_not_crash(self):
        # bLength runs past the end of the blob; also a bLength < 3 tail
        blob = _descriptor_blob(12, BRIO_GUID)[:-10] + b"\x02\x24"
        _make_usb_device(self.base, "1-8", "046d", "085e", blob=blob)
        self.assertEqual(
            b._find_xu_unit_id("046d:085e", BRIO_GUID, base=self.base), 0)


# ---------------------------------------------------------------------------
# Backend control validation without opening a video device
#
# ioctl on /dev/null deterministically fails with ENOTTY, so every path
# that would touch the driver surfaces as an error we can assert on,
# while pure validation errors are raised before any ioctl happens.

def _make_backend(fd, fov_unit=0, led_unit=0):
    be = b.Backend.__new__(b.Backend)
    be._path = os.devnull
    be._lock = threading.Lock()
    be._fd = fd
    be._streaming = False
    be._maps = []
    be._cid_map = {}
    be._usb_ids = None
    be._fov_unit = fov_unit
    be._led_unit = led_unit
    return be


class BackendValidationTest(unittest.TestCase):
    def setUp(self):
        self.fd = os.open(os.devnull, os.O_RDWR)
        self.addCleanup(os.close, self.fd)

    def test_set_control_unknown_id_raises_value_error(self):
        be = _make_backend(self.fd)
        with self.assertRaisesRegex(ValueError, "unknown control id"):
            be.set_control("no_such_control", 1)

    def test_get_control_unknown_id_raises_value_error(self):
        be = _make_backend(self.fd)
        with self.assertRaisesRegex(ValueError, "unknown control id"):
            be.get_control("no_such_control")

    def test_fov_menu_labels(self):
        self.assertEqual(b._FOV_MENU, {0: "90", 1: "78", 2: "65"})

    def test_led_menu_labels(self):
        self.assertEqual(b._LED_MENU, {0: "off", 1: "on", 2: "blink", 3: "auto"})

    def test_set_fov_value_outside_menu_rejected_before_io(self):
        be = _make_backend(self.fd, fov_unit=12)
        for bad in (-1, 3, 99):
            with self.subTest(value=bad):
                with self.assertRaisesRegex(ValueError, "invalid value"):
                    be.set_control("logitech_brio_fov", bad)

    def test_set_fov_valid_value_passes_validation_gate(self):
        # value 1 is in the menu, so validation passes and the failure
        # comes from the (absent) device instead
        be = _make_backend(self.fd, fov_unit=12)
        with self.assertRaisesRegex(ValueError, "FOV XU query failed"):
            be.set_control("logitech_brio_fov", 1)

    def test_set_led_value_outside_menu_rejected_before_io(self):
        be = _make_backend(self.fd, led_unit=9)
        for bad in (-1, 4, 255):
            with self.subTest(value=bad):
                with self.assertRaisesRegex(ValueError, "invalid value"):
                    be.set_control("logitech_led1_mode", bad)

    def test_set_led_valid_value_passes_validation_gate(self):
        be = _make_backend(self.fd, led_unit=9)
        with self.assertRaisesRegex(ValueError, "LED XU query failed"):
            be.set_control("logitech_led1_mode", 1)

    def test_xu_ids_ignored_when_unit_not_discovered(self):
        # with no XU unit id the names fall through to the v4l2 cid map
        be = _make_backend(self.fd, fov_unit=0, led_unit=0)
        with self.assertRaisesRegex(ValueError, "unknown control id"):
            be.set_control("logitech_brio_fov", 1)

    def test_set_control_driver_error_wrapped_as_value_error(self):
        be = _make_backend(self.fd)
        be._cid_map["brightness"] = 0x00980900
        with self.assertRaisesRegex(ValueError, "driver rejected"):
            be.set_control("brightness", 128)


if __name__ == "__main__":
    unittest.main()
