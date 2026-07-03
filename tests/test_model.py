"""Unit tests for webcamdemo.model dataclasses.

Stdlib unittest only. No hardware, no network.
Run alone with: python3 -m unittest tests.test_model
"""

import json
import unittest

from webcamdemo.model import CameraInfo, Control, FrameFormat


class TestCameraInfo(unittest.TestCase):
    def test_construction_all_fields(self):
        info = CameraInfo(id="/dev/video0", name="Fake Cam",
                          extra={"driver": "uvcvideo"})
        self.assertEqual(info.id, "/dev/video0")
        self.assertEqual(info.name, "Fake Cam")
        self.assertEqual(info.extra, {"driver": "uvcvideo"})

    def test_extra_defaults_to_empty_dict(self):
        info = CameraInfo(id="0", name="Cam")
        self.assertEqual(info.extra, {})

    def test_extra_default_not_shared_between_instances(self):
        a = CameraInfo(id="0", name="A")
        b = CameraInfo(id="1", name="B")
        a.extra["k"] = "v"
        self.assertEqual(b.extra, {})

    def test_to_dict_covers_every_field(self):
        info = CameraInfo(id="/dev/video1", name="Cam", extra={"bus": "usb"})
        d = info.to_dict()
        self.assertEqual(d, {"id": "/dev/video1", "name": "Cam",
                             "extra": {"bus": "usb"}})

    def test_to_dict_json_round_trip(self):
        info = CameraInfo(id="/dev/video0", name="Cam", extra={"n": 3})
        d = info.to_dict()
        self.assertEqual(json.loads(json.dumps(d)), d)


class TestControl(unittest.TestCase):
    def test_construction_all_fields(self):
        ctrl = Control(id="brightness", name="Brightness", type="int",
                       min=0, max=255, step=1, default=128, value=100,
                       menu=None, inactive=False)
        self.assertEqual(ctrl.id, "brightness")
        self.assertEqual(ctrl.name, "Brightness")
        self.assertEqual(ctrl.type, "int")
        self.assertEqual(ctrl.min, 0)
        self.assertEqual(ctrl.max, 255)
        self.assertEqual(ctrl.step, 1)
        self.assertEqual(ctrl.default, 128)
        self.assertEqual(ctrl.value, 100)
        self.assertIsNone(ctrl.menu)
        self.assertFalse(ctrl.inactive)

    def test_defaults(self):
        ctrl = Control(id="x", name="X", type="button")
        self.assertIsNone(ctrl.min)
        self.assertIsNone(ctrl.max)
        self.assertIsNone(ctrl.step)
        self.assertIsNone(ctrl.default)
        self.assertIsNone(ctrl.value)
        self.assertIsNone(ctrl.menu)
        self.assertFalse(ctrl.inactive)

    def test_to_dict_covers_every_field(self):
        ctrl = Control(id="focus_absolute", name="Focus", type="int",
                       min=0, max=1023, step=5, default=0, value=42,
                       menu=None, inactive=True)
        d = ctrl.to_dict()
        self.assertEqual(d, {
            "id": "focus_absolute", "name": "Focus", "type": "int",
            "min": 0, "max": 1023, "step": 5, "default": 0, "value": 42,
            "menu": None, "inactive": True,
        })

    def test_to_dict_menu_int_keys_become_str(self):
        ctrl = Control(id="power_line_frequency", name="Power Line", type="menu",
                       menu={0: "Disabled", 1: "50 Hz", 2: "60 Hz"}, value=1)
        d = ctrl.to_dict()
        self.assertEqual(d["menu"], {"0": "Disabled", "1": "50 Hz", "2": "60 Hz"})
        for k in d["menu"]:
            self.assertIsInstance(k, str)

    def test_to_dict_does_not_mutate_original_menu(self):
        menu = {0: "Off", 1: "On"}
        ctrl = Control(id="m", name="M", type="menu", menu=menu, value=0)
        ctrl.to_dict()
        self.assertEqual(ctrl.menu, {0: "Off", 1: "On"})
        for k in ctrl.menu:
            self.assertIsInstance(k, int)

    def test_to_dict_menu_none_stays_none(self):
        ctrl = Control(id="b", name="B", type="bool", min=0, max=1, value=1)
        self.assertIsNone(ctrl.to_dict()["menu"])

    def test_to_dict_json_round_trip_with_menu(self):
        ctrl = Control(id="power_line_frequency", name="Power Line", type="menu",
                       menu={0: "Disabled", 1: "50 Hz"}, value=1)
        d = ctrl.to_dict()
        # Round-trip equality only holds when menu keys are already str:
        # json.dumps would coerce int keys, so loads(dumps(d)) != d then.
        self.assertEqual(json.loads(json.dumps(d)), d)

    def test_to_dict_json_round_trip_without_menu(self):
        ctrl = Control(id="brightness", name="Brightness", type="int",
                       min=0, max=255, step=1, default=128, value=100)
        d = ctrl.to_dict()
        self.assertEqual(json.loads(json.dumps(d)), d)


class TestFrameFormat(unittest.TestCase):
    def test_construction_all_fields(self):
        fmt = FrameFormat(fourcc="MJPG", width=1920, height=1080,
                          fps=[30.0, 15.0])
        self.assertEqual(fmt.fourcc, "MJPG")
        self.assertEqual(fmt.width, 1920)
        self.assertEqual(fmt.height, 1080)
        self.assertEqual(fmt.fps, [30.0, 15.0])

    def test_to_dict_covers_every_field(self):
        fmt = FrameFormat(fourcc="YUYV", width=640, height=480, fps=[30.0])
        self.assertEqual(fmt.to_dict(), {
            "fourcc": "YUYV", "width": 640, "height": 480, "fps": [30.0],
        })

    def test_to_dict_json_round_trip(self):
        fmt = FrameFormat(fourcc="MJPG", width=1280, height=720,
                          fps=[60.0, 30.0, 15.0])
        d = fmt.to_dict()
        self.assertEqual(json.loads(json.dumps(d)), d)

    def test_to_dict_fps_is_a_copy(self):
        fps = [30.0]
        fmt = FrameFormat(fourcc="MJPG", width=640, height=480, fps=fps)
        d = fmt.to_dict()
        d["fps"].append(999.0)
        self.assertEqual(fmt.fps, [30.0])


if __name__ == "__main__":
    unittest.main()
