"""Unit tests for webcamdemo.backend_dshow that run without COM or Windows.

Covers the pure logic of the DirectShow backend: the control property table,
control id resolution, GUID -> FOURCC decoding, frame-interval -> fps
conversion, GetRange -> Control mapping, the COM worker's error marshalling,
the cv2 capture layer (start/read/stop, size verification, format probing)
against a stub cv2 module, the capture-pin preference of _list_formats_impl,
and the documented error behavior when comtypes / opencv are absent.

The mocking used simulates the IAMVideoProcAmp / IAMCameraControl property
surface, the IPin/IAMStreamConfig enumeration surface, and cv2.VideoCapture,
so that the backend's real control-emission, validation, streaming and
format-selection code paths run. No test touches real COM, real cv2
internals, or camera hardware.
"""

import ctypes
import struct
import subprocess
import sys
import threading
import types
import unittest
from unittest import mock

from webcamdemo import backend_dshow
from webcamdemo.model import CameraInfo


def _module_absent(name):
    try:
        __import__(name)
    except ImportError:
        return True
    return False


_COMTYPES_ABSENT = _module_absent("comtypes")
_CV2_ABSENT = _module_absent("cv2")

# Byte tail (bytes 4..15) shared by all FOURCC-derived MEDIASUBTYPE GUIDs:
# {xxxxxxxx-0000-0010-8000-00AA00389B71} in x86 GUID memory layout
# (Data2 LE, Data3 LE, Data4 verbatim).
FOURCC_TAIL = struct.pack("<HH", 0x0000, 0x0010) + bytes(
    [0x80, 0x00, 0x00, 0xAA, 0x00, 0x38, 0x9B, 0x71])


def make_guid(raw16):
    """Build a ctypes object with GUID size/layout from 16 raw bytes."""
    assert len(raw16) == 16
    return (ctypes.c_ubyte * 16)(*raw16)


def fourcc_guid(fourcc):
    return make_guid(fourcc.encode("ascii") + FOURCC_TAIL)


def guid_from_fields(data1, data2, data3, data4):
    return make_guid(struct.pack("<IHH", data1, data2, data3) + bytes(data4))


# Fake namespace standing in for the _dshow() result where only
# FOURCC_GUID_TAIL is needed.
FAKE_D = types.SimpleNamespace(FOURCC_GUID_TAIL=FOURCC_TAIL)


class PropTableTest(unittest.TestCase):
    """The control table must match the Microsoft strmif.h enums."""

    def test_procamp_ordinals_match_videoprocampproperty_enum(self):
        expected = {
            "brightness": 0,
            "contrast": 1,
            "hue": 2,
            "saturation": 3,
            "sharpness": 4,
            "gamma": 5,
            "color_enable": 6,
            "white_balance": 7,
            "backlight_compensation": 8,
            "gain": 9,
        }
        actual = {name: prop
                  for kind, prop, name in backend_dshow._PROP_TABLE
                  if kind == backend_dshow._PROCAMP}
        self.assertEqual(actual, expected)

    def test_camctl_ordinals_match_cameracontrolproperty_enum(self):
        expected = {
            "pan": 0,
            "tilt": 1,
            "roll": 2,
            "zoom": 3,
            "exposure": 4,
            "iris": 5,
            "focus": 6,
        }
        actual = {name: prop
                  for kind, prop, name in backend_dshow._PROP_TABLE
                  if kind == backend_dshow._CAMCTL}
        self.assertEqual(actual, expected)

    def test_table_has_exactly_the_17_documented_controls(self):
        self.assertEqual(len(backend_dshow._PROP_TABLE), 17)

    def test_ids_are_snake_case_and_unique(self):
        names = [name for _, _, name in backend_dshow._PROP_TABLE]
        self.assertEqual(len(names), len(set(names)))
        for name in names:
            self.assertRegex(name, r"^[a-z]+(_[a-z]+)*$")

    def test_prop_by_id_index_is_consistent_with_table(self):
        self.assertEqual(len(backend_dshow._PROP_BY_ID),
                         len(backend_dshow._PROP_TABLE))
        for kind, prop, name in backend_dshow._PROP_TABLE:
            self.assertEqual(backend_dshow._PROP_BY_ID[name], (kind, prop))

    def test_flag_values_match_cameracontrolflags(self):
        # CameraControl_Flags_Auto / VideoProcAmp_Flags_Auto == 0x1,
        # *_Flags_Manual == 0x2 (strmif.h).
        self.assertEqual(backend_dshow._FLAG_AUTO, 0x1)
        self.assertEqual(backend_dshow._FLAG_MANUAL, 0x2)


class ResolveCtrlTest(unittest.TestCase):
    def test_plain_ids_resolve_to_kind_and_ordinal(self):
        self.assertEqual(backend_dshow._resolve_ctrl("brightness"),
                         (backend_dshow._PROCAMP, 0, False))
        self.assertEqual(backend_dshow._resolve_ctrl("gain"),
                         (backend_dshow._PROCAMP, 9, False))
        self.assertEqual(backend_dshow._resolve_ctrl("pan"),
                         (backend_dshow._CAMCTL, 0, False))
        self.assertEqual(backend_dshow._resolve_ctrl("focus"),
                         (backend_dshow._CAMCTL, 6, False))

    def test_auto_suffix_maps_to_base_property(self):
        self.assertEqual(backend_dshow._resolve_ctrl("brightness_auto"),
                         (backend_dshow._PROCAMP, 0, True))
        self.assertEqual(backend_dshow._resolve_ctrl("focus_auto"),
                         (backend_dshow._CAMCTL, 6, True))

    def test_unknown_id_raises_value_error_listing_known_ids(self):
        with self.assertRaises(ValueError) as cm:
            backend_dshow._resolve_ctrl("bogus")
        msg = str(cm.exception)
        self.assertIn("bogus", msg)
        self.assertIn("brightness", msg)
        self.assertIn("focus", msg)

    def test_bare_or_dangling_auto_raises_value_error(self):
        with self.assertRaises(ValueError):
            backend_dshow._resolve_ctrl("_auto")
        with self.assertRaises(ValueError):
            backend_dshow._resolve_ctrl("auto")


class DisplayNameTest(unittest.TestCase):
    def test_snake_case_becomes_title_case(self):
        self.assertEqual(backend_dshow._display_name("brightness"),
                         "Brightness")
        self.assertEqual(backend_dshow._display_name("white_balance"),
                         "White Balance")
        self.assertEqual(backend_dshow._display_name("backlight_compensation"),
                         "Backlight Compensation")


class GuidHelpersTest(unittest.TestCase):
    def test_guid_bytes_returns_raw_memory(self):
        raw = bytes(range(16))
        self.assertEqual(backend_dshow._guid_bytes(make_guid(raw)), raw)

    def test_guid_equal_compares_by_content(self):
        raw = bytes(range(16))
        self.assertTrue(backend_dshow._guid_equal(make_guid(raw),
                                                  make_guid(raw)))
        other = bytes([255]) + raw[1:]
        self.assertFalse(backend_dshow._guid_equal(make_guid(raw),
                                                   make_guid(other)))


class FourccFromSubtypeTest(unittest.TestCase):
    def test_mjpg_guid_decodes(self):
        self.assertEqual(
            backend_dshow._fourcc_from_subtype(FAKE_D, fourcc_guid("MJPG")),
            "MJPG")

    def test_yuy2_guid_decodes(self):
        self.assertEqual(
            backend_dshow._fourcc_from_subtype(FAKE_D, fourcc_guid("YUY2")),
            "YUY2")

    def test_nv12_guid_decodes(self):
        self.assertEqual(
            backend_dshow._fourcc_from_subtype(FAKE_D, fourcc_guid("NV12")),
            "NV12")

    def test_rgb24_quartz_guid_maps_to_rgb3(self):
        # MEDIASUBTYPE_RGB24 {E436EB7D-524F-11CE-9F53-0020AF0BA770}
        rgb24 = guid_from_fields(
            0xE436EB7D, 0x524F, 0x11CE,
            [0x9F, 0x53, 0x00, 0x20, 0xAF, 0x0B, 0xA7, 0x70])
        self.assertEqual(
            backend_dshow._fourcc_from_subtype(FAKE_D, rgb24), "RGB3")

    def test_rgb32_quartz_guid_maps_to_rgb4(self):
        rgb32 = guid_from_fields(
            0xE436EB7E, 0x524F, 0x11CE,
            [0x9F, 0x53, 0x00, 0x20, 0xAF, 0x0B, 0xA7, 0x70])
        self.assertEqual(
            backend_dshow._fourcc_from_subtype(FAKE_D, rgb32), "RGB4")

    def test_random_guid_returns_none(self):
        random_guid = guid_from_fields(
            0x12345678, 0xABCD, 0xEF01,
            [0x11, 0x22, 0x33, 0x44, 0x55, 0x66, 0x77, 0x88])
        self.assertIsNone(
            backend_dshow._fourcc_from_subtype(FAKE_D, random_guid))

    def test_printable_fourcc_with_wrong_tail_returns_none(self):
        wrong_tail = make_guid(b"MJPG" + bytes(12))
        self.assertIsNone(
            backend_dshow._fourcc_from_subtype(FAKE_D, wrong_tail))

    def test_fourcc_tail_with_non_printable_data1_returns_none(self):
        non_printable = make_guid(b"\x00\x01\x02\x03" + FOURCC_TAIL)
        self.assertIsNone(
            backend_dshow._fourcc_from_subtype(FAKE_D, non_printable))

    def test_printable_boundaries(self):
        # 0x20 (space) is the lowest printable byte, 0x7F is excluded.
        self.assertEqual(
            backend_dshow._fourcc_from_subtype(
                FAKE_D, make_guid(b" Y16" + FOURCC_TAIL)),
            " Y16")
        self.assertIsNone(
            backend_dshow._fourcc_from_subtype(
                FAKE_D, make_guid(b"\x7fY16" + FOURCC_TAIL)))


class FpsFromIntervalsTest(unittest.TestCase):
    def test_common_intervals(self):
        self.assertEqual(backend_dshow._fps_from_intervals(333333), {30.0})
        self.assertEqual(backend_dshow._fps_from_intervals(166667), {60.0})
        self.assertEqual(backend_dshow._fps_from_intervals(400000), {25.0})
        self.assertEqual(backend_dshow._fps_from_intervals(10000000), {1.0})

    def test_ntsc_interval_rounds_to_two_decimals(self):
        self.assertEqual(backend_dshow._fps_from_intervals(333667), {29.97})

    def test_min_and_max_intervals_merge_into_set(self):
        self.assertEqual(
            backend_dshow._fps_from_intervals(333333, 166667), {30.0, 60.0})
        self.assertEqual(
            backend_dshow._fps_from_intervals(333333, 333333), {30.0})

    def test_non_positive_intervals_ignored(self):
        self.assertEqual(backend_dshow._fps_from_intervals(0, -5), set())
        self.assertEqual(backend_dshow._fps_from_intervals(0, 333333), {30.0})
        self.assertEqual(backend_dshow._fps_from_intervals(), set())


class ControlsFromRangeTest(unittest.TestCase):
    AUTO = backend_dshow._FLAG_AUTO
    MANUAL = backend_dshow._FLAG_MANUAL

    def test_manual_only_range_emits_single_int_control(self):
        controls = backend_dshow._controls_from_range(
            "brightness", (-64, 64, 1, 0, self.MANUAL), 10, self.MANUAL)
        self.assertEqual(len(controls), 1)
        c = controls[0]
        self.assertEqual(c.id, "brightness")
        self.assertEqual(c.name, "Brightness")
        self.assertEqual(c.type, "int")
        self.assertEqual((c.min, c.max, c.step, c.default, c.value),
                         (-64, 64, 1, 0, 10))
        self.assertFalse(c.inactive)

    def test_auto_capable_range_emits_bool_companion(self):
        controls = backend_dshow._controls_from_range(
            "white_balance", (2800, 6500, 10, 4600, self.AUTO | self.MANUAL),
            4600, self.AUTO)
        self.assertEqual([c.id for c in controls],
                         ["white_balance", "white_balance_auto"])
        main, auto = controls
        self.assertTrue(main.inactive)  # greyed out while auto is on
        self.assertEqual(auto.name, "White Balance Auto")
        self.assertEqual(auto.type, "bool")
        self.assertEqual((auto.min, auto.max, auto.step), (0, 1, 1))
        self.assertEqual(auto.value, 1)

    def test_auto_capable_but_auto_off(self):
        controls = backend_dshow._controls_from_range(
            "focus", (0, 250, 5, 0, self.AUTO | self.MANUAL), 100, self.MANUAL)
        main, auto = controls
        self.assertFalse(main.inactive)
        self.assertEqual(auto.id, "focus_auto")
        self.assertEqual(auto.value, 0)

    def test_zero_step_is_normalized_to_one(self):
        c = backend_dshow._controls_from_range(
            "gamma", (100, 500, 0, 300, self.MANUAL), 300, self.MANUAL)[0]
        self.assertEqual(c.step, 1)

    def test_none_value_survives_as_none(self):
        # Matches the Get-failed path in _list_controls_impl.
        c = backend_dshow._controls_from_range(
            "contrast", (0, 100, 1, 50, self.MANUAL), None, 0)[0]
        self.assertIsNone(c.value)
        self.assertFalse(c.inactive)


class FakeCOMError(Exception):
    """Stands in for comtypes.COMError in the fake _dshow namespace."""


class FakeAmp:
    """Simulates the IAMVideoProcAmp / IAMCameraControl property surface.

    props: {ordinal: {"rng": (min, max, step, default, caps),
                      "value": int, "flags": int,
                      "get_fails": bool, "ignore_set": bool}}
    """

    def __init__(self, props):
        self.props = props
        self.set_calls = []

    def GetRange(self, prop):
        if prop not in self.props:
            raise FakeCOMError("property %d not supported" % prop)
        return self.props[prop]["rng"]

    def Get(self, prop):
        p = self.props[prop]
        if p.get("get_fails"):
            raise FakeCOMError("cannot read property %d" % prop)
        return p["value"], p["flags"]

    def Set(self, prop, value, flags):
        self.set_calls.append((prop, value, flags))
        p = self.props[prop]
        if not p.get("ignore_set"):
            p["value"] = value
            p["flags"] = flags


class FakeFilter:
    """QueryInterface returns a per-interface object or raises FakeCOMError."""

    def __init__(self, by_iface):
        self.by_iface = by_iface

    def QueryInterface(self, iface_cls):
        try:
            return self.by_iface[iface_cls]
        except KeyError:
            raise FakeCOMError("interface not supported")


class _AmpSimBase(unittest.TestCase):
    """Patches _dshow/_bind_filter so the real control code paths run
    against a simulated property interface."""

    AUTO = backend_dshow._FLAG_AUTO
    MANUAL = backend_dshow._FLAG_MANUAL

    def install(self, procamp=None, camctl=None):
        d = types.SimpleNamespace(
            COMError=FakeCOMError,
            IAMVideoProcAmp=object(),
            IAMCameraControl=object(),
        )
        by_iface = {}
        if procamp is not None:
            by_iface[d.IAMVideoProcAmp] = procamp
        if camctl is not None:
            by_iface[d.IAMCameraControl] = camctl
        filt = FakeFilter(by_iface)
        patcher_d = mock.patch.object(backend_dshow, "_dshow", return_value=d)
        patcher_b = mock.patch.object(
            backend_dshow, "_bind_filter", return_value=filt)
        patcher_d.start()
        patcher_b.start()
        self.addCleanup(patcher_d.stop)
        self.addCleanup(patcher_b.stop)
        return d


class ListControlsImplTest(_AmpSimBase):
    def test_emits_only_supported_props_in_table_order(self):
        procamp = FakeAmp({
            0: {"rng": (-64, 64, 1, 0, self.MANUAL),
                "value": 10, "flags": self.MANUAL},
            7: {"rng": (2800, 6500, 10, 4600, self.AUTO | self.MANUAL),
                "value": 4600, "flags": self.AUTO},
        })
        self.install(procamp=procamp, camctl=None)  # no IAMCameraControl
        controls = backend_dshow._list_controls_impl(0)
        self.assertEqual([c.id for c in controls],
                         ["brightness", "white_balance", "white_balance_auto"])
        brightness = controls[0]
        self.assertEqual((brightness.min, brightness.max, brightness.value),
                         (-64, 64, 10))
        self.assertFalse(brightness.inactive)
        wb, wb_auto = controls[1], controls[2]
        self.assertTrue(wb.inactive)
        self.assertEqual(wb_auto.value, 1)

    def test_procamp_controls_precede_camctl_controls(self):
        procamp = FakeAmp({
            9: {"rng": (0, 255, 1, 64, self.MANUAL),
                "value": 64, "flags": self.MANUAL},
        })
        camctl = FakeAmp({
            6: {"rng": (0, 250, 5, 0, self.AUTO | self.MANUAL),
                "value": 0, "flags": self.AUTO},
        })
        self.install(procamp=procamp, camctl=camctl)
        controls = backend_dshow._list_controls_impl(0)
        self.assertEqual([c.id for c in controls],
                         ["gain", "focus", "focus_auto"])

    def test_get_failure_yields_control_with_none_value(self):
        procamp = FakeAmp({
            1: {"rng": (0, 100, 1, 50, self.MANUAL),
                "value": 50, "flags": self.MANUAL, "get_fails": True},
        })
        self.install(procamp=procamp)
        controls = backend_dshow._list_controls_impl(0)
        self.assertEqual([c.id for c in controls], ["contrast"])
        self.assertIsNone(controls[0].value)

    def test_no_interfaces_yields_empty_list(self):
        self.install(procamp=None, camctl=None)
        self.assertEqual(backend_dshow._list_controls_impl(0), [])


class GetSetControlImplTest(_AmpSimBase):
    def _wb_amp(self, **extra):
        props = {7: dict({"rng": (2800, 6500, 10, 4600,
                                  self.AUTO | self.MANUAL),
                          "value": 4600, "flags": self.AUTO}, **extra)}
        return FakeAmp(props)

    def test_get_control_returns_int_value(self):
        amp = FakeAmp({0: {"rng": (-64, 64, 1, 0, self.MANUAL),
                           "value": 12, "flags": self.MANUAL}})
        self.install(procamp=amp)
        self.assertEqual(backend_dshow._get_control_impl(0, "brightness"), 12)

    def test_get_auto_control_reflects_flags(self):
        self.install(procamp=self._wb_amp())
        self.assertEqual(
            backend_dshow._get_control_impl(0, "white_balance_auto"), 1)

    def test_get_unsupported_property_raises_value_error(self):
        self.install(procamp=FakeAmp({}))
        with self.assertRaises(ValueError) as cm:
            backend_dshow._get_control_impl(0, "hue")
        self.assertIn("not supported", str(cm.exception))

    def test_get_without_interface_names_the_missing_interface(self):
        self.install(procamp=FakeAmp({}), camctl=None)
        with self.assertRaises(ValueError) as cm:
            backend_dshow._get_control_impl(0, "pan")
        self.assertIn("IAMCameraControl", str(cm.exception))

    def test_set_out_of_range_raises_before_any_set_call(self):
        amp = FakeAmp({0: {"rng": (-64, 64, 1, 0, self.MANUAL),
                           "value": 0, "flags": self.MANUAL}})
        self.install(procamp=amp)
        with self.assertRaises(ValueError) as cm:
            backend_dshow._set_control_impl(0, "brightness", 100)
        self.assertIn("out of range", str(cm.exception))
        self.assertEqual(amp.set_calls, [])

    def test_set_manual_value_uses_manual_flag_and_verifies(self):
        amp = FakeAmp({0: {"rng": (-64, 64, 1, 0, self.MANUAL),
                           "value": 0, "flags": self.MANUAL}})
        self.install(procamp=amp)
        backend_dshow._set_control_impl(0, "brightness", 20)
        self.assertEqual(amp.set_calls,
                         [(0, 20, backend_dshow._FLAG_MANUAL)])

    def test_set_readback_mismatch_raises(self):
        amp = FakeAmp({0: {"rng": (-64, 64, 1, 0, self.MANUAL),
                           "value": 0, "flags": self.MANUAL,
                           "ignore_set": True}})
        self.install(procamp=amp)
        with self.assertRaises(ValueError) as cm:
            backend_dshow._set_control_impl(0, "brightness", 20)
        self.assertIn("device reports", str(cm.exception))

    def test_set_auto_on_device_without_auto_caps_raises(self):
        amp = FakeAmp({0: {"rng": (-64, 64, 1, 0, self.MANUAL),
                           "value": 0, "flags": self.MANUAL}})
        self.install(procamp=amp)
        with self.assertRaises(ValueError) as cm:
            backend_dshow._set_control_impl(0, "brightness_auto", 1)
        self.assertIn("no auto mode", str(cm.exception))
        self.assertEqual(amp.set_calls, [])

    def test_set_manual_on_auto_only_device_raises(self):
        amp = FakeAmp({7: {"rng": (2800, 6500, 10, 4600, self.AUTO),
                           "value": 4600, "flags": self.AUTO}})
        self.install(procamp=amp)
        with self.assertRaises(ValueError) as cm:
            backend_dshow._set_control_impl(0, "white_balance", 5000)
        self.assertIn("auto-only", str(cm.exception))
        self.assertEqual(amp.set_calls, [])

    def test_set_auto_toggle_keeps_current_value(self):
        amp = self._wb_amp()
        amp.props[7]["flags"] = self.MANUAL  # start in manual mode
        self.install(procamp=amp)
        backend_dshow._set_control_impl(0, "white_balance_auto", 1)
        self.assertEqual(amp.set_calls,
                         [(7, 4600, backend_dshow._FLAG_AUTO)])

    def test_set_auto_off_switches_to_manual_flag(self):
        amp = self._wb_amp()
        self.install(procamp=amp)
        backend_dshow._set_control_impl(0, "white_balance_auto", 0)
        self.assertEqual(amp.set_calls,
                         [(7, 4600, backend_dshow._FLAG_MANUAL)])


class FakeCv2Cap:
    """Simulates cv2.VideoCapture for one device.

    The 'device' echoes a requested frame size only when it is in
    ``supported``; otherwise it keeps delivering the current mode -- the
    exact behavior start_stream's size verification exists to catch.
    """

    def __init__(self, cv2mod, supported=(), opened=True,
                 initial=(640, 480), fps=30.0, frames=()):
        self._cv2 = cv2mod
        self.supported = set(supported)
        self.opened = opened
        self.current = initial
        self.fps = fps
        self.frames = list(frames)
        self.released = False
        self.requested_fourcc = None
        self.requested_fps = None
        self._pending_w = None

    def isOpened(self):
        return self.opened

    def set(self, prop, value):
        c = self._cv2
        if prop == c.CAP_PROP_FOURCC:
            self.requested_fourcc = value
        elif prop == c.CAP_PROP_FRAME_WIDTH:
            self._pending_w = int(value)
        elif prop == c.CAP_PROP_FRAME_HEIGHT:
            size = (self._pending_w, int(value))
            if size in self.supported:
                self.current = size
        elif prop == c.CAP_PROP_FPS:
            self.requested_fps = float(value)
        return True

    def get(self, prop):
        c = self._cv2
        if prop == c.CAP_PROP_FRAME_WIDTH:
            return float(self.current[0])
        if prop == c.CAP_PROP_FRAME_HEIGHT:
            return float(self.current[1])
        if prop == c.CAP_PROP_FPS:
            return self.fps
        return 0.0

    def read(self):
        if self.frames:
            return True, self.frames.pop(0)
        return False, None

    def release(self):
        self.released = True


class _FakeCv2Base(unittest.TestCase):
    """Installs a stub cv2 module in sys.modules (shadowing any real cv2)
    and builds Backends without touching COM."""

    def setUp(self):
        fake = types.ModuleType("cv2")
        fake.CAP_DSHOW = 700
        fake.CAP_PROP_FRAME_WIDTH = 3
        fake.CAP_PROP_FRAME_HEIGHT = 4
        fake.CAP_PROP_FPS = 5
        fake.CAP_PROP_FOURCC = 6
        fake.IMWRITE_JPEG_QUALITY = 1
        fake.VideoWriter_fourcc = lambda *ch: struct.unpack(
            "<I", "".join(ch).encode("ascii"))[0]

        class _Buf:
            def __init__(self, data):
                self._data = data

            def tobytes(self):
                return self._data

        fake.imencode = lambda ext, frame, params: (True, _Buf(b"jpeg:" + frame))

        self.opens = []   # (index, api) per VideoCapture construction
        self.serve = []   # caps handed out in order

        def video_capture(index, api):
            self.opens.append((index, api))
            return self.serve.pop(0)

        fake.VideoCapture = video_capture
        self.cv2 = fake

        original = sys.modules.get("cv2")

        def restore():
            if original is None:
                sys.modules.pop("cv2", None)
            else:
                sys.modules["cv2"] = original

        sys.modules["cv2"] = fake
        self.addCleanup(restore)

    def make_backend(self):
        cams = [CameraInfo(id="0", name="Cam A")]
        with mock.patch.object(backend_dshow, "list_cameras",
                               return_value=cams):
            return backend_dshow.Backend("0")

    def make_cap(self, **kwargs):
        cap = FakeCv2Cap(self.cv2, **kwargs)
        self.serve.append(cap)
        return cap


class Cv2StreamingTest(_FakeCv2Base):
    def test_start_stream_opens_dshow_device_and_requests_mjpg(self):
        b = self.make_backend()
        cap = self.make_cap(supported={(1280, 720)})
        b.start_stream(1280, 720, fps=30)
        self.assertEqual(self.opens, [(0, self.cv2.CAP_DSHOW)])
        self.assertEqual(cap.requested_fourcc,
                         self.cv2.VideoWriter_fourcc(*"MJPG"))
        self.assertEqual(cap.requested_fps, 30.0)
        self.assertFalse(cap.released)
        self.assertIs(b._cap, cap)

    def test_start_stream_size_mismatch_releases_cap_and_raises(self):
        # Device ignores the request and stays at 640x480: start_stream must
        # release the capture and raise instead of streaming the wrong size.
        b = self.make_backend()
        cap = self.make_cap(supported=set(), initial=(640, 480))
        with self.assertRaises(ValueError) as cm:
            b.start_stream(1280, 720)
        msg = str(cm.exception)
        self.assertIn("1280x720", msg)
        self.assertIn("640x480", msg)
        self.assertTrue(cap.released, "mismatched capture must be released")
        with self.assertRaises(RuntimeError):
            b.read_jpeg()  # backend must not be left 'streaming'

    def test_start_stream_open_failure_raises_runtime_error(self):
        b = self.make_backend()
        self.make_cap(opened=False)
        with self.assertRaises(RuntimeError) as cm:
            b.start_stream(1280, 720)
        self.assertIn("failed to open", str(cm.exception))

    def test_restart_releases_previous_capture(self):
        b = self.make_backend()
        first = self.make_cap(supported={(1280, 720)})
        second = self.make_cap(supported={(1920, 1080)})
        b.start_stream(1280, 720)
        b.start_stream(1920, 1080)
        self.assertTrue(first.released)
        self.assertFalse(second.released)
        self.assertIs(b._cap, second)

    def test_read_jpeg_encodes_frame(self):
        b = self.make_backend()
        self.make_cap(supported={(1280, 720)}, frames=[b"raw-frame"])
        b.start_stream(1280, 720)
        self.assertEqual(b.read_jpeg(), b"jpeg:raw-frame")

    def test_read_jpeg_failure_raises(self):
        b = self.make_backend()
        self.make_cap(supported={(1280, 720)}, frames=[])
        b.start_stream(1280, 720)
        with self.assertRaises(RuntimeError) as cm:
            b.read_jpeg()
        self.assertIn("failed to read frame", str(cm.exception))

    def test_read_before_start_raises(self):
        b = self.make_backend()
        with self.assertRaises(RuntimeError):
            b.read_jpeg()

    def test_stop_idempotent_and_read_after_stop_raises(self):
        b = self.make_backend()
        cap = self.make_cap(supported={(1280, 720)}, frames=[b"x"])
        b.start_stream(1280, 720)
        b.stop_stream()
        self.assertTrue(cap.released)
        b.stop_stream()  # second stop must be a no-op
        with self.assertRaises(RuntimeError):
            b.read_jpeg()


class Cv2ProbeFormatsTest(_FakeCv2Base):
    def test_busy_device_reports_active_mode_without_reopening(self):
        b = self.make_backend()
        cap = self.make_cap(supported={(1920, 1080)})
        b.start_stream(1920, 1080)
        del self.opens[:]
        formats = b._probe_formats_cv2()
        self.assertEqual(len(formats), 1)
        f = formats[0]
        self.assertEqual((f.fourcc, f.width, f.height, f.fps),
                         ("MJPG", 1920, 1080, [30.0]))
        self.assertEqual(self.opens, [],
                         "busy device must not be opened a second time")
        self.assertFalse(cap.released)

    def test_probe_reports_only_sizes_the_device_delivers(self):
        b = self.make_backend()
        cap = self.make_cap(supported={(1920, 1080), (640, 480)})
        formats = b._probe_formats_cv2()
        self.assertEqual([(f.width, f.height) for f in formats],
                         [(1920, 1080), (640, 480)])  # _FALLBACK_SIZES order
        for f in formats:
            self.assertEqual(f.fourcc, "MJPG")
        self.assertTrue(cap.released, "probe capture must be released")

    def test_probe_unopenable_device_yields_no_formats(self):
        b = self.make_backend()
        self.make_cap(opened=False)
        self.assertEqual(b._probe_formats_cv2(), [])

    def test_list_formats_falls_back_to_cv2_when_com_path_fails(self):
        b = self.make_backend()
        self.make_cap(supported={(1280, 720)})
        worker = mock.Mock()
        worker.call.side_effect = RuntimeError("COM enumeration failed")
        with mock.patch.object(backend_dshow, "_get_worker",
                               return_value=worker):
            formats = b.list_formats()
        self.assertEqual([(f.width, f.height) for f in formats],
                         [(1280, 720)])


class FakeStreamConfig:
    def __init__(self, caps):
        self.caps = caps  # entries: parsed tuples, None, or FakeCOMError

    def GetNumberOfCapabilities(self):
        return len(self.caps), 8

    def GetStreamCaps(self, i, caps_addr):
        entry = self.caps[i]
        if isinstance(entry, FakeCOMError):
            raise entry
        return entry  # opaque 'pmt' handed back to the patched parser


class FakeFormatsPin:
    def __init__(self, caps=(), direction=backend_dshow._PINDIR_OUTPUT,
                 category=None, no_stream_config=False):
        self.caps = list(caps)
        self.direction = direction
        self.category = category
        self.no_stream_config = no_stream_config

    def QueryDirection(self):
        return self.direction

    def QueryInterface(self, iface_cls):
        if self.no_stream_config:
            raise FakeCOMError("IAMStreamConfig not supported")
        return FakeStreamConfig(self.caps)


class FakeEnumPins:
    def __init__(self, pins):
        self._pins = list(pins)

    def Next(self, n):
        if self._pins:
            return self._pins.pop(0), 1
        return None, 0


class FakeFormatsFilter:
    def __init__(self, pins):
        self._pins = pins

    def EnumPins(self):
        return FakeEnumPins(self._pins)


class ListFormatsPinPreferenceTest(unittest.TestCase):
    """_list_formats_impl must prefer the PIN_CATEGORY_CAPTURE pin over
    still-image/preview pins that also expose IAMStreamConfig.

    The COM plumbing (_dshow, _bind_filter, _parse_stream_cap,
    _free_media_type, _pin_category) is patched; the pin-enumeration,
    merging and preference logic under test is the real code.
    """

    CAPTURE_GUID = bytes(range(16))
    PREVIEW_GUID = bytes(range(16, 32))

    def install(self, pins):
        d = types.SimpleNamespace(
            COMError=FakeCOMError,
            IAMStreamConfig=object(),
            VIDEO_STREAM_CONFIG_CAPS=ctypes.c_ubyte * 8,
            PIN_CATEGORY_CAPTURE=make_guid(self.CAPTURE_GUID),
        )
        self.freed = []
        patchers = [
            mock.patch.object(backend_dshow, "_dshow", return_value=d),
            mock.patch.object(backend_dshow, "_bind_filter",
                              return_value=FakeFormatsFilter(pins)),
            mock.patch.object(backend_dshow, "_parse_stream_cap",
                              side_effect=lambda d, pmt, buf: pmt),
            mock.patch.object(backend_dshow, "_free_media_type",
                              side_effect=lambda d, pmt: self.freed.append(pmt)),
            mock.patch.object(backend_dshow, "_pin_category",
                              side_effect=lambda d, pin: pin.category),
        ]
        for p in patchers:
            p.start()
            self.addCleanup(p.stop)

    def test_capture_pin_wins_over_earlier_preview_pin(self):
        preview = FakeFormatsPin(caps=[("MJPG", 640, 480, {30.0})],
                                 category=self.PREVIEW_GUID)
        capture = FakeFormatsPin(
            caps=[("MJPG", 1280, 720, {30.0}), ("MJPG", 1280, 720, {60.0})],
            category=self.CAPTURE_GUID)
        self.install([preview, capture])
        formats = backend_dshow._list_formats_impl(0)
        self.assertEqual(len(formats), 1, formats)
        f = formats[0]
        self.assertEqual((f.fourcc, f.width, f.height), ("MJPG", 1280, 720))
        self.assertEqual(f.fps, [30.0, 60.0])  # merged across caps, sorted

    def test_first_video_pin_used_when_no_category_readable(self):
        first = FakeFormatsPin(caps=[("YUY2", 640, 480, {30.0})])
        second = FakeFormatsPin(caps=[("MJPG", 1920, 1080, {30.0})])
        self.install([first, second])
        formats = backend_dshow._list_formats_impl(0)
        self.assertEqual([(f.fourcc, f.width, f.height) for f in formats],
                         [("YUY2", 640, 480)])

    def test_skips_input_configless_and_unparsable_entries(self):
        input_pin = FakeFormatsPin(caps=[("MJPG", 999, 999, {30.0})],
                                   direction=0)  # PINDIR_INPUT
        audio_pin = FakeFormatsPin(no_stream_config=True)
        video_pin = FakeFormatsPin(
            caps=[None,                           # non-video media type
                  FakeCOMError("GetStreamCaps failed"),
                  ("YUY2", 320, 240, set())])     # no fps advertised
        self.install([input_pin, audio_pin, video_pin])
        formats = backend_dshow._list_formats_impl(0)
        self.assertEqual(len(formats), 1, formats)
        f = formats[0]
        self.assertEqual((f.fourcc, f.width, f.height), ("YUY2", 320, 240))
        self.assertEqual(f.fps, [30.0])  # unknown fps defaults to 30
        # every successfully returned media type was freed (None included)
        self.assertEqual(self.freed, [None, ("YUY2", 320, 240, set())])

    def test_no_usable_pins_yields_empty_list(self):
        self.install([FakeFormatsPin(no_stream_config=True)])
        self.assertEqual(backend_dshow._list_formats_impl(0), [])


class BackendConstructorTest(unittest.TestCase):
    def test_non_numeric_id_raises_value_error_before_any_com_use(self):
        with self.assertRaises(ValueError) as cm:
            backend_dshow.Backend("abc")
        msg = str(cm.exception)
        self.assertIn("'abc'", msg)
        self.assertIn("invalid Windows camera id", msg)

    def test_known_camera_id_binds_info(self):
        cams = [CameraInfo(id="0", name="Cam A"),
                CameraInfo(id="1", name="Cam B")]
        with mock.patch.object(backend_dshow, "list_cameras",
                               return_value=cams):
            b = backend_dshow.Backend("1")
        self.assertEqual(b.info().name, "Cam B")
        b.close()  # no stream started; must be a safe no-op

    def test_missing_camera_id_lists_available(self):
        cams = [CameraInfo(id="0", name="Cam A")]
        with mock.patch.object(backend_dshow, "list_cameras",
                               return_value=cams):
            with self.assertRaises(ValueError) as cm:
                backend_dshow.Backend("3")
        msg = str(cm.exception)
        self.assertIn("'3'", msg)
        self.assertIn("0 (Cam A)", msg)

    def test_no_cameras_says_none(self):
        with mock.patch.object(backend_dshow, "list_cameras",
                               return_value=[]):
            with self.assertRaises(ValueError) as cm:
                backend_dshow.Backend("0")
        self.assertIn("none", str(cm.exception))


@unittest.skipUnless(_COMTYPES_ABSENT,
                     "comtypes importable; missing-dependency path untestable")
class ComtypesAbsentTest(unittest.TestCase):
    def test_module_imports_cleanly_and_lazily_without_comtypes(self):
        code = ("import sys; import webcamdemo.backend_dshow; "
                "print('comtypes' in sys.modules, 'cv2' in sys.modules)")
        proc = subprocess.run([sys.executable, "-c", code],
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.split(), ["False", "False"])

    def test_list_cameras_raises_runtime_error_mentioning_comtypes(self):
        with self.assertRaises(RuntimeError) as cm:
            backend_dshow.list_cameras()
        self.assertIn("comtypes", str(cm.exception))

    def test_backend_ctor_raises_runtime_error_mentioning_comtypes(self):
        with self.assertRaises(RuntimeError) as cm:
            backend_dshow.Backend("0")
        self.assertIn("comtypes", str(cm.exception))

    def test_invalid_id_fails_without_starting_com_worker(self):
        with self.assertRaises(ValueError):
            backend_dshow.Backend("abc")
        self.assertIsNone(backend_dshow._worker)


@unittest.skipUnless(_COMTYPES_ABSENT and _CV2_ABSENT,
                     "requires both comtypes and cv2 to be absent")
class ListFormatsFallbackTest(unittest.TestCase):
    def test_list_formats_falls_back_to_cv2_probe_error(self):
        cams = [CameraInfo(id="0", name="Cam A")]
        with mock.patch.object(backend_dshow, "list_cameras",
                               return_value=cams):
            b = backend_dshow.Backend("0")
        # COM path fails (no comtypes) -> swallowed; cv2 probe then raises
        # the documented opencv RuntimeError.
        with self.assertRaises(RuntimeError) as cm:
            b.list_formats()
        self.assertIn("opencv-python", str(cm.exception))


class ComWorkerTest(unittest.TestCase):
    """Exercises _ComWorker's queue round-trip and error marshalling with a
    stub comtypes module (only CoInitialize is stubbed)."""

    def _install_fake_comtypes(self, co_initialize=None):
        fake = types.ModuleType("comtypes")
        fake.CoInitialize = co_initialize or (lambda: None)
        original = sys.modules.get("comtypes")

        def restore():
            if original is None:
                sys.modules.pop("comtypes", None)
            else:
                sys.modules["comtypes"] = original

        sys.modules["comtypes"] = fake
        self.addCleanup(restore)

    def test_call_returns_result_from_worker_thread(self):
        self._install_fake_comtypes()
        w = backend_dshow._ComWorker()
        self.assertEqual(w.call(lambda: 41 + 1), 42)
        worker_thread = w.call(threading.current_thread)
        self.assertEqual(worker_thread.name, "webcamdemo-dshow-com")
        self.assertIsNot(worker_thread, threading.current_thread())

    def test_value_error_crosses_thread_as_value_error(self):
        self._install_fake_comtypes()
        w = backend_dshow._ComWorker()

        def boom():
            raise ValueError("bad value here")

        with self.assertRaises(ValueError) as cm:
            w.call(boom)
        self.assertEqual(str(cm.exception), "bad value here")

    def test_other_exceptions_become_runtime_error_with_type_name(self):
        self._install_fake_comtypes()
        w = backend_dshow._ComWorker()

        def boom():
            raise KeyError("missing")

        with self.assertRaises(RuntimeError) as cm:
            w.call(boom)
        self.assertIn("KeyError", str(cm.exception))
        self.assertIn("missing", str(cm.exception))

    def test_coinitialize_failure_surfaces_on_every_call(self):
        def failing_coinit():
            raise OSError("no COM apartment on this OS")

        self._install_fake_comtypes(co_initialize=failing_coinit)
        w = backend_dshow._ComWorker()
        for _ in range(2):  # init error must persist across calls
            with self.assertRaises(RuntimeError) as cm:
                w.call(lambda: 1)
            msg = str(cm.exception)
            self.assertIn("COM initialization failed", msg)
            self.assertIn("no COM apartment", msg)


if __name__ == "__main__":
    unittest.main()
