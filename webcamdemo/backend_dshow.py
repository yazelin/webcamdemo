"""Windows backend - written against DirectShow docs, not yet exercised on
Windows; see README verification checklist.

Device enumeration and controls use DirectShow COM interfaces via comtypes
(ICreateDevEnum, IAMVideoProcAmp, IAMCameraControl, IAMStreamConfig); frame
capture uses OpenCV with the CAP_DSHOW backend.

Threading model: the HTTP server calls control methods from arbitrary worker
threads while another thread loops read_jpeg(). All COM access is therefore
funneled through one dedicated, lazily started apartment thread (a module
singleton) that calls CoInitialize once and serves requests from a queue; the
queue doubles as the internal lock. COM objects are created, used and
released entirely on that thread, and only plain Python data (or exception
type + message strings) crosses back, so no COM pointer ever leaks into
another apartment. cv2 capture calls are serialized with a separate lock.

comtypes and cv2 are imported lazily so this module imports cleanly on any OS.

Vtable layouts and enum values were cross-checked against the Microsoft Learn
strmif.h reference and the Wine/ReactOS IDL sources; only the leading methods
of each interface (up to the last slot actually called) are declared, which
is safe because COM dispatch is by vtable index.
"""

from __future__ import annotations

import ctypes
import queue
import struct
import threading

from .model import CameraInfo, Control, FrameFormat

# VideoProcAmpProperty / CameraControlProperty values (strmif.h).
_PROCAMP = "procamp"
_CAMCTL = "camctl"

_PROP_TABLE = [
    (_PROCAMP, 0, "brightness"),
    (_PROCAMP, 1, "contrast"),
    (_PROCAMP, 2, "hue"),
    (_PROCAMP, 3, "saturation"),
    (_PROCAMP, 4, "sharpness"),
    (_PROCAMP, 5, "gamma"),
    (_PROCAMP, 6, "color_enable"),
    (_PROCAMP, 7, "white_balance"),
    (_PROCAMP, 8, "backlight_compensation"),
    (_PROCAMP, 9, "gain"),
    (_CAMCTL, 0, "pan"),
    (_CAMCTL, 1, "tilt"),
    (_CAMCTL, 2, "roll"),
    (_CAMCTL, 3, "zoom"),
    (_CAMCTL, 4, "exposure"),
    (_CAMCTL, 5, "iris"),
    (_CAMCTL, 6, "focus"),
]
_PROP_BY_ID = {name: (kind, prop) for kind, prop, name in _PROP_TABLE}

# VideoProcAmpFlags / CameraControlFlags share the same values.
_FLAG_AUTO = 0x1
_FLAG_MANUAL = 0x2

_PINDIR_OUTPUT = 1

# AMPROPERTY_PIN_CATEGORY (strmif.h): property id 0 in AMPROPSETID_Pin.
_AMPROPERTY_PIN_CATEGORY = 0

_FALLBACK_SIZES = [(3840, 2160), (2560, 1440), (1920, 1080), (1280, 720), (640, 480)]

# MEDIASUBTYPE GUIDs whose Data1 is not a printable FOURCC (quartz RGB family).
_RGB_SUBTYPE_FOURCC = {
    0xE436EB7D: "RGB3",  # MEDIASUBTYPE_RGB24
    0xE436EB7E: "RGB4",  # MEDIASUBTYPE_RGB32
}


def _display_name(ctrl_id: str) -> str:
    return ctrl_id.replace("_", " ").title()


def _cv2():
    try:
        import cv2
    except ImportError as exc:
        raise RuntimeError(
            "opencv-python is required for streaming on Windows "
            "(pip install opencv-python)"
        ) from exc
    return cv2


# ---------------------------------------------------------------------------
# Lazy COM interface definitions
# ---------------------------------------------------------------------------

_dshow_ns = None


def _dshow():
    global _dshow_ns
    if _dshow_ns is None:
        _dshow_ns = _build_dshow()
    return _dshow_ns


def _build_dshow():
    from ctypes import (
        HRESULT,
        POINTER,
        Structure,
        c_int,
        c_long,
        c_longlong,
        c_ulong,
        c_ushort,
        c_void_p,
        c_wchar_p,
    )

    import comtypes
    from comtypes import COMMETHOD, GUID, IUnknown, COMError
    from comtypes.automation import VARIANT

    class _SIZE(Structure):
        _fields_ = [("cx", c_long), ("cy", c_long)]

    class AM_MEDIA_TYPE(Structure):
        _fields_ = [
            ("majortype", GUID),
            ("subtype", GUID),
            ("bFixedSizeSamples", c_long),
            ("bTemporalCompression", c_long),
            ("lSampleSize", c_ulong),
            ("formattype", GUID),
            ("pUnk", c_void_p),
            ("cbFormat", c_ulong),
            ("pbFormat", c_void_p),
        ]

    class VIDEO_STREAM_CONFIG_CAPS(Structure):
        _fields_ = [
            ("guid", GUID),
            ("VideoStandard", c_ulong),
            ("InputSize", _SIZE),
            ("MinCroppingSize", _SIZE),
            ("MaxCroppingSize", _SIZE),
            ("CropGranularityX", c_int),
            ("CropGranularityY", c_int),
            ("CropAlignX", c_int),
            ("CropAlignY", c_int),
            ("MinOutputSize", _SIZE),
            ("MaxOutputSize", _SIZE),
            ("OutputGranularityX", c_int),
            ("OutputGranularityY", c_int),
            ("StretchTapsX", c_int),
            ("StretchTapsY", c_int),
            ("ShrinkTapsX", c_int),
            ("ShrinkTapsY", c_int),
            ("MinFrameInterval", c_longlong),
            ("MaxFrameInterval", c_longlong),
            ("MinBitsPerSecond", c_long),
            ("MaxBitsPerSecond", c_long),
        ]

    class BITMAPINFOHEADER(Structure):
        _fields_ = [
            ("biSize", c_ulong),
            ("biWidth", c_long),
            ("biHeight", c_long),
            ("biPlanes", c_ushort),
            ("biBitCount", c_ushort),
            ("biCompression", c_ulong),
            ("biSizeImage", c_ulong),
            ("biXPelsPerMeter", c_long),
            ("biYPelsPerMeter", c_long),
            ("biClrUsed", c_ulong),
            ("biClrImportant", c_ulong),
        ]

    class _RECT(Structure):
        _fields_ = [
            ("left", c_long),
            ("top", c_long),
            ("right", c_long),
            ("bottom", c_long),
        ]

    class VIDEOINFOHEADER(Structure):
        _fields_ = [
            ("rcSource", _RECT),
            ("rcTarget", _RECT),
            ("dwBitRate", c_ulong),
            ("dwBitErrorRate", c_ulong),
            ("AvgTimePerFrame", c_longlong),
            ("bmiHeader", BITMAPINFOHEADER),
        ]

    class VIDEOINFOHEADER2(Structure):
        _fields_ = [
            ("rcSource", _RECT),
            ("rcTarget", _RECT),
            ("dwBitRate", c_ulong),
            ("dwBitErrorRate", c_ulong),
            ("AvgTimePerFrame", c_longlong),
            ("dwInterlaceFlags", c_ulong),
            ("dwCopyProtectFlags", c_ulong),
            ("dwPictAspectRatioX", c_ulong),
            ("dwPictAspectRatioY", c_ulong),
            ("dwControlFlags", c_ulong),
            ("dwReserved2", c_ulong),
            ("bmiHeader", BITMAPINFOHEADER),
        ]

    class IMoniker(IUnknown):
        _iid_ = GUID("{0000000F-0000-0000-C000-000000000046}")

    class IEnumMoniker(IUnknown):
        _iid_ = GUID("{00000102-0000-0000-C000-000000000046}")

    class ICreateDevEnum(IUnknown):
        _iid_ = GUID("{29840822-5B84-11D0-BD3B-00A0C911CE86}")

    class IPropertyBag(IUnknown):
        _iid_ = GUID("{55272A00-42CB-11CE-8135-00AA004BB851}")

    class IPin(IUnknown):
        _iid_ = GUID("{56A86891-0AD4-11CE-B03A-0020AF0BA770}")

    class IEnumPins(IUnknown):
        _iid_ = GUID("{56A86892-0AD4-11CE-B03A-0020AF0BA770}")

    class IBaseFilter(IUnknown):
        _iid_ = GUID("{56A86895-0AD4-11CE-B03A-0020AF0BA770}")

    class IAMStreamConfig(IUnknown):
        _iid_ = GUID("{C6E13340-30AC-11D0-A18C-00A0C9118956}")

    class IAMVideoProcAmp(IUnknown):
        _iid_ = GUID("{C6E13360-30AC-11D0-A18C-00A0C9118956}")

    class IAMCameraControl(IUnknown):
        _iid_ = GUID("{C6E13370-30AC-11D0-A18C-00A0C9118956}")

    class IKsPropertySet(IUnknown):
        _iid_ = GUID("{31EFAC30-515C-11D0-A9AA-00AA0061BE93}")

    # IMoniker vtable: IPersist::GetClassID, IPersistStream::IsDirty/Load/
    # Save/GetSizeMax, then BindToObject/BindToStorage (objidl.idl order).
    # Methods after BindToStorage are never called and left undeclared.
    IMoniker._methods_ = [
        COMMETHOD([], HRESULT, "GetClassID",
                  (["out"], POINTER(GUID), "pClassID")),
        COMMETHOD([], HRESULT, "IsDirty"),
        COMMETHOD([], HRESULT, "Load", (["in"], c_void_p, "pStm")),
        COMMETHOD([], HRESULT, "Save",
                  (["in"], c_void_p, "pStm"),
                  (["in"], c_long, "fClearDirty")),
        COMMETHOD([], HRESULT, "GetSizeMax",
                  (["out"], POINTER(c_longlong), "pcbSize")),
        COMMETHOD([], HRESULT, "BindToObject",
                  (["in"], c_void_p, "pbc"),
                  (["in"], c_void_p, "pmkToLeft"),
                  (["in"], POINTER(GUID), "riidResult"),
                  (["out"], POINTER(POINTER(IUnknown)), "ppvResult")),
        COMMETHOD([], HRESULT, "BindToStorage",
                  (["in"], c_void_p, "pbc"),
                  (["in"], c_void_p, "pmkToLeft"),
                  (["in"], POINTER(GUID), "riid"),
                  (["out"], POINTER(POINTER(IUnknown)), "ppvObj")),
    ]

    IEnumMoniker._methods_ = [
        COMMETHOD([], HRESULT, "Next",
                  (["in"], c_ulong, "celt"),
                  (["out"], POINTER(POINTER(IMoniker)), "rgelt"),
                  (["out"], POINTER(c_ulong), "pceltFetched")),
        COMMETHOD([], HRESULT, "Skip", (["in"], c_ulong, "celt")),
        COMMETHOD([], HRESULT, "Reset"),
        COMMETHOD([], HRESULT, "Clone",
                  (["out"], POINTER(POINTER(IEnumMoniker)), "ppenum")),
    ]

    ICreateDevEnum._methods_ = [
        COMMETHOD([], HRESULT, "CreateClassEnumerator",
                  (["in"], POINTER(GUID), "clsidDeviceClass"),
                  (["out"], POINTER(POINTER(IEnumMoniker)), "ppEnumMoniker"),
                  (["in"], c_int, "dwFlags")),
    ]

    IPropertyBag._methods_ = [
        COMMETHOD([], HRESULT, "Read",
                  (["in"], c_wchar_p, "pszPropName"),
                  (["in"], POINTER(VARIANT), "pVar"),
                  (["in"], c_void_p, "pErrorLog")),
        COMMETHOD([], HRESULT, "Write",
                  (["in"], c_wchar_p, "pszPropName"),
                  (["in"], POINTER(VARIANT), "pVar")),
    ]

    # IPin vtable (strmif/axcore.idl): the first six methods are unused
    # stubs that only reserve their vtable slots.
    IPin._methods_ = [
        COMMETHOD([], HRESULT, "Connect",
                  (["in"], c_void_p, "pReceivePin"),
                  (["in"], c_void_p, "pmt")),
        COMMETHOD([], HRESULT, "ReceiveConnection",
                  (["in"], c_void_p, "pConnector"),
                  (["in"], c_void_p, "pmt")),
        COMMETHOD([], HRESULT, "Disconnect"),
        COMMETHOD([], HRESULT, "ConnectedTo",
                  (["out"], POINTER(POINTER(IUnknown)), "ppPin")),
        COMMETHOD([], HRESULT, "ConnectionMediaType",
                  (["in"], c_void_p, "pmt")),
        COMMETHOD([], HRESULT, "QueryPinInfo", (["in"], c_void_p, "pInfo")),
        COMMETHOD([], HRESULT, "QueryDirection",
                  (["out"], POINTER(c_int), "pPinDir")),
    ]

    IEnumPins._methods_ = [
        COMMETHOD([], HRESULT, "Next",
                  (["in"], c_ulong, "cPins"),
                  (["out"], POINTER(POINTER(IPin)), "ppPins"),
                  (["out"], POINTER(c_ulong), "pcFetched")),
        COMMETHOD([], HRESULT, "Skip", (["in"], c_ulong, "cPins")),
        COMMETHOD([], HRESULT, "Reset"),
        COMMETHOD([], HRESULT, "Clone",
                  (["out"], POINTER(POINTER(IEnumPins)), "ppEnum")),
    ]

    # IBaseFilter vtable: IPersist::GetClassID + IMediaFilter (Stop, Pause,
    # Run, GetState, SetSyncSource, GetSyncSource) + EnumPins; the rest of
    # IBaseFilter is unused and left undeclared.
    IBaseFilter._methods_ = [
        COMMETHOD([], HRESULT, "GetClassID",
                  (["out"], POINTER(GUID), "pClassID")),
        COMMETHOD([], HRESULT, "Stop"),
        COMMETHOD([], HRESULT, "Pause"),
        COMMETHOD([], HRESULT, "Run", (["in"], c_longlong, "tStart")),
        COMMETHOD([], HRESULT, "GetState",
                  (["in"], c_ulong, "dwMilliSecsTimeout"),
                  (["out"], POINTER(c_int), "State")),
        COMMETHOD([], HRESULT, "SetSyncSource",
                  (["in"], c_void_p, "pClock")),
        COMMETHOD([], HRESULT, "GetSyncSource",
                  (["out"], POINTER(c_void_p), "pClock")),
        COMMETHOD([], HRESULT, "EnumPins",
                  (["out"], POINTER(POINTER(IEnumPins)), "ppEnum")),
    ]

    IAMStreamConfig._methods_ = [
        COMMETHOD([], HRESULT, "SetFormat", (["in"], c_void_p, "pmt")),
        COMMETHOD([], HRESULT, "GetFormat",
                  (["out"], POINTER(POINTER(AM_MEDIA_TYPE)), "ppmt")),
        COMMETHOD([], HRESULT, "GetNumberOfCapabilities",
                  (["out"], POINTER(c_int), "piCount"),
                  (["out"], POINTER(c_int), "piSize")),
        COMMETHOD([], HRESULT, "GetStreamCaps",
                  (["in"], c_int, "iIndex"),
                  (["out"], POINTER(POINTER(AM_MEDIA_TYPE)), "ppmt"),
                  (["in"], c_void_p, "pSCC")),
    ]

    # IKsPropertySet (strmif.h): only used to read a pin's category GUID.
    IKsPropertySet._methods_ = [
        COMMETHOD([], HRESULT, "Set",
                  (["in"], POINTER(GUID), "guidPropSet"),
                  (["in"], c_ulong, "dwPropID"),
                  (["in"], c_void_p, "pInstanceData"),
                  (["in"], c_ulong, "cbInstanceData"),
                  (["in"], c_void_p, "pPropData"),
                  (["in"], c_ulong, "cbPropData")),
        COMMETHOD([], HRESULT, "Get",
                  (["in"], POINTER(GUID), "guidPropSet"),
                  (["in"], c_ulong, "dwPropID"),
                  (["in"], c_void_p, "pInstanceData"),
                  (["in"], c_ulong, "cbInstanceData"),
                  (["in"], c_void_p, "pPropData"),
                  (["in"], c_ulong, "cbPropData"),
                  (["out"], POINTER(c_ulong), "pcbReturned")),
        COMMETHOD([], HRESULT, "QuerySupported",
                  (["in"], POINTER(GUID), "guidPropSet"),
                  (["in"], c_ulong, "dwPropID"),
                  (["out"], POINTER(c_ulong), "pTypeSupport")),
    ]

    _amp_methods = [
        COMMETHOD([], HRESULT, "GetRange",
                  (["in"], c_long, "Property"),
                  (["out"], POINTER(c_long), "pMin"),
                  (["out"], POINTER(c_long), "pMax"),
                  (["out"], POINTER(c_long), "pSteppingDelta"),
                  (["out"], POINTER(c_long), "pDefault"),
                  (["out"], POINTER(c_long), "pCapsFlags")),
        COMMETHOD([], HRESULT, "Set",
                  (["in"], c_long, "Property"),
                  (["in"], c_long, "lValue"),
                  (["in"], c_long, "Flags")),
        COMMETHOD([], HRESULT, "Get",
                  (["in"], c_long, "Property"),
                  (["out"], POINTER(c_long), "lValue"),
                  (["out"], POINTER(c_long), "Flags")),
    ]
    IAMVideoProcAmp._methods_ = list(_amp_methods)
    IAMCameraControl._methods_ = list(_amp_methods)

    ole32 = ctypes.WinDLL("ole32")
    ole32.CoTaskMemFree.restype = None
    ole32.CoTaskMemFree.argtypes = [c_void_p]

    class _NS:
        pass

    ns = _NS()
    ns.comtypes = comtypes
    ns.COMError = COMError
    ns.GUID = GUID
    ns.IUnknown = IUnknown
    ns.VARIANT = VARIANT
    ns.ole32 = ole32
    ns.AM_MEDIA_TYPE = AM_MEDIA_TYPE
    ns.VIDEO_STREAM_CONFIG_CAPS = VIDEO_STREAM_CONFIG_CAPS
    ns.VIDEOINFOHEADER = VIDEOINFOHEADER
    ns.VIDEOINFOHEADER2 = VIDEOINFOHEADER2
    ns.IMoniker = IMoniker
    ns.IEnumMoniker = IEnumMoniker
    ns.ICreateDevEnum = ICreateDevEnum
    ns.IPropertyBag = IPropertyBag
    ns.IPin = IPin
    ns.IEnumPins = IEnumPins
    ns.IBaseFilter = IBaseFilter
    ns.IAMStreamConfig = IAMStreamConfig
    ns.IAMVideoProcAmp = IAMVideoProcAmp
    ns.IAMCameraControl = IAMCameraControl
    ns.IKsPropertySet = IKsPropertySet
    ns.AMPROPSETID_Pin = GUID("{9B00F101-1567-11D1-B3F1-00AA003761C5}")
    ns.PIN_CATEGORY_CAPTURE = GUID("{FB6C4281-0353-11D1-905F-0000C0CC16BA}")
    ns.CLSID_SystemDeviceEnum = GUID("{62BE5D10-60EB-11D0-BD3B-00A0C911CE86}")
    ns.CLSID_VideoInputDeviceCategory = GUID(
        "{860BB310-5D01-11D0-BD3B-00A0C911CE86}")
    ns.MEDIATYPE_Video = GUID("{73646976-0000-0010-8000-00AA00389B71}")
    ns.FORMAT_VideoInfo = GUID("{05589F80-C356-11CE-BF01-00AA0055595A}")
    ns.FORMAT_VideoInfo2 = GUID("{F72A76A0-EB0A-11D0-ACE4-0000C0CC16BA}")
    # Tail (bytes 4..15) shared by all FOURCC-derived MEDIASUBTYPE GUIDs.
    ns.FOURCC_GUID_TAIL = _guid_bytes(
        GUID("{00000000-0000-0010-8000-00AA00389B71}"))[4:]
    return ns


def _guid_bytes(guid) -> bytes:
    # Field-name independent: compare GUIDs by raw memory.
    return ctypes.string_at(ctypes.byref(guid), ctypes.sizeof(guid))


def _guid_equal(a, b) -> bool:
    return _guid_bytes(a) == _guid_bytes(b)


def _fourcc_from_subtype(d, subtype) -> "str|None":
    raw = _guid_bytes(subtype)
    if raw[4:] == d.FOURCC_GUID_TAIL and all(0x20 <= c < 0x7F for c in raw[:4]):
        return raw[:4].decode("ascii")
    return _RGB_SUBTYPE_FOURCC.get(struct.unpack("<I", raw[:4])[0])


# ---------------------------------------------------------------------------
# Dedicated COM apartment thread
# ---------------------------------------------------------------------------


class _ComWorker:
    def __init__(self):
        self._tasks = queue.SimpleQueue()
        self._init_error = None
        thread = threading.Thread(
            target=self._run, name="webcamdemo-dshow-com", daemon=True)
        thread.start()

    def _run(self):
        try:
            import comtypes
            comtypes.CoInitialize()
        except Exception as exc:
            self._init_error = "%s: %s" % (type(exc).__name__, exc)
        # No CoUninitialize: the apartment lives for the process (daemon
        # thread); Windows tears it down at thread/process exit.
        while True:
            fn, box, done = self._tasks.get()
            if self._init_error is not None:
                box["error"] = (RuntimeError,
                                "COM initialization failed: " + self._init_error)
                done.set()
                continue
            try:
                box["result"] = fn()
            except Exception as exc:
                # Only exception class + message cross the thread boundary,
                # so no COM pointer (via tracebacks) leaves the apartment.
                cls = ValueError if isinstance(exc, ValueError) else RuntimeError
                if isinstance(exc, (ValueError, RuntimeError)):
                    msg = str(exc)
                else:
                    msg = "%s: %s" % (type(exc).__name__, exc)
                box["error"] = (cls, msg)
            done.set()

    def call(self, fn):
        box = {}
        done = threading.Event()
        self._tasks.put((fn, box, done))
        done.wait()
        if "error" in box:
            cls, msg = box["error"]
            raise cls(msg)
        return box["result"]


_worker = None
_worker_lock = threading.Lock()


def _get_worker() -> _ComWorker:
    global _worker
    with _worker_lock:
        if _worker is None:
            try:
                import comtypes  # noqa: F401
            except ImportError as exc:
                raise RuntimeError(
                    "comtypes is required for the DirectShow backend "
                    "(pip install comtypes)"
                ) from exc
            _worker = _ComWorker()
        return _worker


# ---------------------------------------------------------------------------
# COM-side implementation (all functions below run on the worker thread)
# ---------------------------------------------------------------------------


def _iter_monikers(d):
    dev_enum = d.comtypes.CoCreateInstance(
        d.CLSID_SystemDeviceEnum,
        interface=d.ICreateDevEnum,
        clsctx=d.comtypes.CLSCTX_INPROC_SERVER,
    )
    enum_mon = dev_enum.CreateClassEnumerator(
        d.CLSID_VideoInputDeviceCategory, 0)
    # CreateClassEnumerator returns S_FALSE + NULL when the category is empty.
    if not enum_mon:
        return
    while True:
        mon, fetched = enum_mon.Next(1)
        if not fetched or not mon:
            break
        yield mon


def _read_bag_prop(d, moniker, prop_name):
    try:
        unk = moniker.BindToStorage(None, None, d.IPropertyBag._iid_)
        bag = unk.QueryInterface(d.IPropertyBag)
        var = d.VARIANT()
        bag.Read(prop_name, var, None)
        return var.value
    except d.COMError:
        return None


def _list_cameras_impl():
    d = _dshow()
    cameras = []
    for index, moniker in enumerate(_iter_monikers(d)):
        name = _read_bag_prop(d, moniker, "FriendlyName")
        path = _read_bag_prop(d, moniker, "DevicePath")
        extra = {"path": str(path)} if path else {}
        cameras.append(CameraInfo(
            id=str(index),
            name=str(name) if name else "Camera %d" % index,
            extra=extra,
        ))
    return cameras


def _bind_filter(d, index):
    for i, moniker in enumerate(_iter_monikers(d)):
        if i == index:
            unk = moniker.BindToObject(None, None, d.IBaseFilter._iid_)
            return unk.QueryInterface(d.IBaseFilter)
    raise ValueError("camera %r not found (device list may have changed)" % index)


def _query_amp(d, filt, kind):
    iface_cls = d.IAMVideoProcAmp if kind == _PROCAMP else d.IAMCameraControl
    try:
        return filt.QueryInterface(iface_cls)
    except d.COMError:
        return None


def _list_controls_impl(index):
    d = _dshow()
    filt = _bind_filter(d, index)
    controls = []
    for kind in (_PROCAMP, _CAMCTL):
        iface = _query_amp(d, filt, kind)
        if iface is None:
            continue
        for prop_kind, prop, ctrl_id in _PROP_TABLE:
            if prop_kind != kind:
                continue
            try:
                vmin, vmax, step, default, caps = iface.GetRange(prop)
            except d.COMError:
                continue
            try:
                value, flags = iface.Get(prop)
            except d.COMError:
                value, flags = None, 0
            auto_capable = bool(caps & _FLAG_AUTO)
            auto_on = bool(flags & _FLAG_AUTO)
            controls.append(Control(
                id=ctrl_id,
                name=_display_name(ctrl_id),
                type="int",
                min=int(vmin),
                max=int(vmax),
                step=int(step) if step else 1,
                default=int(default),
                value=int(value) if value is not None else None,
                inactive=auto_capable and auto_on,
            ))
            if auto_capable:
                controls.append(Control(
                    id=ctrl_id + "_auto",
                    name=_display_name(ctrl_id) + " Auto",
                    type="bool",
                    min=0,
                    max=1,
                    step=1,
                    value=1 if auto_on else 0,
                ))
    return controls


def _resolve_ctrl(ctrl_id):
    is_auto = ctrl_id.endswith("_auto")
    base = ctrl_id[:-5] if is_auto else ctrl_id
    entry = _PROP_BY_ID.get(base)
    if entry is None:
        raise ValueError(
            "unknown control id %r; known ids: %s"
            % (ctrl_id, ", ".join(name for _, _, name in _PROP_TABLE)))
    kind, prop = entry
    return kind, prop, is_auto


def _open_ctrl(d, index, ctrl_id):
    kind, prop, is_auto = _resolve_ctrl(ctrl_id)
    filt = _bind_filter(d, index)
    iface = _query_amp(d, filt, kind)
    if iface is None:
        raise ValueError(
            "control %r is not supported by this device (no %s interface)"
            % (ctrl_id,
               "IAMVideoProcAmp" if kind == _PROCAMP else "IAMCameraControl"))
    try:
        rng = iface.GetRange(prop)
    except d.COMError:
        raise ValueError("control %r is not supported by this device" % ctrl_id)
    return iface, prop, is_auto, rng


def _get_control_impl(index, ctrl_id):
    d = _dshow()
    iface, prop, is_auto, _rng = _open_ctrl(d, index, ctrl_id)
    try:
        value, flags = iface.Get(prop)
    except d.COMError as exc:
        raise ValueError("failed to read control %r: %s" % (ctrl_id, exc))
    if is_auto:
        return 1 if flags & _FLAG_AUTO else 0
    return int(value)


def _set_control_impl(index, ctrl_id, value):
    d = _dshow()
    iface, prop, is_auto, rng = _open_ctrl(d, index, ctrl_id)
    vmin, vmax, step, _default, caps = rng
    if is_auto:
        want = 1 if value else 0
        if want and not caps & _FLAG_AUTO:
            raise ValueError("control %r: device has no auto mode" % ctrl_id)
        if not want and not caps & _FLAG_MANUAL:
            raise ValueError(
                "control %r: device does not allow manual mode" % ctrl_id)
        current, _flags = iface.Get(prop)
        flag = _FLAG_AUTO if want else _FLAG_MANUAL
        try:
            iface.Set(prop, current, flag)
        except d.COMError as exc:
            raise ValueError(
                "device rejected %s=%d: %s" % (ctrl_id, want, exc))
        _value, flags = iface.Get(prop)
        got = 1 if flags & _FLAG_AUTO else 0
        if got != want:
            raise ValueError(
                "set %s=%d but device reports %d" % (ctrl_id, want, got))
    else:
        value = int(value)
        if value < vmin or value > vmax:
            raise ValueError(
                "%s value %d out of range [%d, %d] (step %d)"
                % (ctrl_id, value, vmin, vmax, step or 1))
        if not caps & _FLAG_MANUAL:
            raise ValueError("control %r is auto-only on this device" % ctrl_id)
        try:
            iface.Set(prop, value, _FLAG_MANUAL)
        except d.COMError as exc:
            raise ValueError(
                "device rejected %s=%d: %s" % (ctrl_id, value, exc))
        got, _flags = iface.Get(prop)
        if int(got) != value:
            raise ValueError(
                "set %s=%d but device reports %d (step is %d)"
                % (ctrl_id, value, got, step or 1))


def _free_media_type(d, pmt):
    if not pmt:
        return
    mt = pmt.contents
    if mt.cbFormat and mt.pbFormat:
        d.ole32.CoTaskMemFree(mt.pbFormat)
    if mt.pUnk:
        punk = ctypes.cast(mt.pUnk, ctypes.POINTER(d.IUnknown))
        del punk  # comtypes Release()s the reference held by the media type
    d.ole32.CoTaskMemFree(ctypes.cast(pmt, ctypes.c_void_p))


def _parse_stream_cap(d, pmt, caps_buf):
    mt = pmt.contents
    if not _guid_equal(mt.majortype, d.MEDIATYPE_Video):
        return None
    fourcc = _fourcc_from_subtype(d, mt.subtype)
    if fourcc is None or not mt.pbFormat:
        return None
    if (_guid_equal(mt.formattype, d.FORMAT_VideoInfo)
            and mt.cbFormat >= ctypes.sizeof(d.VIDEOINFOHEADER)):
        header_cls = d.VIDEOINFOHEADER
    elif (_guid_equal(mt.formattype, d.FORMAT_VideoInfo2)
            and mt.cbFormat >= ctypes.sizeof(d.VIDEOINFOHEADER2)):
        header_cls = d.VIDEOINFOHEADER2
    else:
        return None
    header = ctypes.cast(mt.pbFormat, ctypes.POINTER(header_cls)).contents
    width = int(header.bmiHeader.biWidth)
    height = abs(int(header.bmiHeader.biHeight))
    if width <= 0 or height <= 0:
        return None
    caps = ctypes.cast(
        caps_buf, ctypes.POINTER(d.VIDEO_STREAM_CONFIG_CAPS)).contents
    fps = set()
    for interval in (caps.MaxFrameInterval, caps.MinFrameInterval):
        if interval > 0:
            fps.add(round(1e7 / interval, 2))
    return fourcc, width, height, fps


def _pin_category(d, pin):
    """Return the pin's category GUID as raw bytes, or None if unreadable."""
    try:
        ks = pin.QueryInterface(d.IKsPropertySet)
        category = d.GUID()
        returned = ks.Get(d.AMPROPSETID_Pin, _AMPROPERTY_PIN_CATEGORY,
                          None, 0, ctypes.byref(category),
                          ctypes.sizeof(category))
    except d.COMError:
        return None
    if returned < ctypes.sizeof(d.GUID):
        return None
    return _guid_bytes(category)


def _list_formats_impl(index):
    d = _dshow()
    filt = _bind_filter(d, index)
    enum_pins = filt.EnumPins()
    capture_guid = _guid_bytes(d.PIN_CATEGORY_CAPTURE)
    merged = None
    while True:
        pin, fetched = enum_pins.Next(1)
        if not fetched or not pin:
            break
        try:
            if pin.QueryDirection() != _PINDIR_OUTPUT:
                continue
            cfg = pin.QueryInterface(d.IAMStreamConfig)
            count, size = cfg.GetNumberOfCapabilities()
        except d.COMError:
            continue
        caps_buf = ctypes.create_string_buffer(
            max(size, ctypes.sizeof(d.VIDEO_STREAM_CONFIG_CAPS)))
        pin_formats = {}
        for i in range(count):
            try:
                pmt = cfg.GetStreamCaps(i, ctypes.addressof(caps_buf))
            except d.COMError:
                continue
            try:
                parsed = _parse_stream_cap(d, pmt, caps_buf)
            finally:
                _free_media_type(d, pmt)
            if parsed is None:
                continue
            fourcc, width, height, fps = parsed
            pin_formats.setdefault((fourcc, width, height), set()).update(fps)
        if not pin_formats:
            continue
        # Pin enumeration order is not guaranteed and still-image/preview
        # pins also expose IAMStreamConfig: prefer the PIN_CATEGORY_CAPTURE
        # pin, falling back to the first video pin if no category is readable.
        if _pin_category(d, pin) == capture_guid:
            merged = pin_formats
            break
        if merged is None:
            merged = pin_formats
    return [
        FrameFormat(fourcc=fourcc, width=width, height=height,
                    fps=sorted(fps) if fps else [30.0])
        for (fourcc, width, height), fps in (merged or {}).items()
    ]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def list_cameras() -> "list[CameraInfo]":
    return _get_worker().call(_list_cameras_impl)


class Backend:
    def __init__(self, camera_id: str):
        try:
            self._index = int(str(camera_id))
        except ValueError:
            raise ValueError(
                "invalid Windows camera id %r; expected a DirectShow device "
                "index like '0'" % (camera_id,))
        cameras = list_cameras()
        for cam in cameras:
            if cam.id == str(self._index):
                self._camera_info = cam
                break
        else:
            available = ", ".join(
                "%s (%s)" % (c.id, c.name) for c in cameras) or "none"
            raise ValueError(
                "camera %r not found; available: %s" % (camera_id, available))
        self._stream_lock = threading.Lock()
        self._cap = None

    def close(self) -> None:
        self.stop_stream()

    def info(self) -> CameraInfo:
        return self._camera_info

    def list_controls(self) -> "list[Control]":
        return _get_worker().call(lambda: _list_controls_impl(self._index))

    def get_control(self, ctrl_id: str) -> int:
        return _get_worker().call(
            lambda: _get_control_impl(self._index, ctrl_id))

    def set_control(self, ctrl_id: str, value: int) -> None:
        _get_worker().call(
            lambda: _set_control_impl(self._index, ctrl_id, value))

    def list_formats(self) -> "list[FrameFormat]":
        try:
            formats = _get_worker().call(
                lambda: _list_formats_impl(self._index))
        except (ValueError, RuntimeError):
            formats = []
        if formats:
            return formats
        return self._probe_formats_cv2()

    def _probe_formats_cv2(self) -> "list[FrameFormat]":
        cv2 = _cv2()
        with self._stream_lock:
            if self._cap is not None:
                # Device is busy streaming; report the active mode only.
                width = int(round(self._cap.get(cv2.CAP_PROP_FRAME_WIDTH)))
                height = int(round(self._cap.get(cv2.CAP_PROP_FRAME_HEIGHT)))
                fps = self._cap.get(cv2.CAP_PROP_FPS) or 30.0
                return [FrameFormat(fourcc="MJPG", width=width, height=height,
                                    fps=[round(float(fps), 2)])]
            cap = cv2.VideoCapture(self._index, cv2.CAP_DSHOW)
            if not cap.isOpened():
                return []
            formats = []
            seen = set()
            try:
                for width, height in _FALLBACK_SIZES:
                    cap.set(cv2.CAP_PROP_FOURCC,
                            cv2.VideoWriter_fourcc(*"MJPG"))
                    cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
                    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
                    actual_w = int(round(cap.get(cv2.CAP_PROP_FRAME_WIDTH)))
                    actual_h = int(round(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)))
                    if (actual_w, actual_h) == (width, height) \
                            and (width, height) not in seen:
                        seen.add((width, height))
                        formats.append(FrameFormat(
                            fourcc="MJPG", width=width, height=height,
                            fps=[30.0]))
            finally:
                cap.release()
            return formats

    def start_stream(self, width: int, height: int,
                     fps: "float|None" = None) -> None:
        cv2 = _cv2()
        with self._stream_lock:
            if self._cap is not None:
                self._cap.release()
                self._cap = None
            cap = cv2.VideoCapture(self._index, cv2.CAP_DSHOW)
            if not cap.isOpened():
                raise RuntimeError(
                    "failed to open camera %d for streaming" % self._index)
            cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, int(width))
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, int(height))
            if fps:
                cap.set(cv2.CAP_PROP_FPS, float(fps))
            actual_w = int(round(cap.get(cv2.CAP_PROP_FRAME_WIDTH)))
            actual_h = int(round(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)))
            if (actual_w, actual_h) != (int(width), int(height)):
                cap.release()
                raise ValueError(
                    "requested %dx%d but device delivers %dx%d"
                    % (width, height, actual_w, actual_h))
            self._cap = cap

    def read_jpeg(self) -> bytes:
        cv2 = _cv2()
        with self._stream_lock:
            if self._cap is None:
                raise RuntimeError("not streaming; call start_stream() first")
            ok, frame = self._cap.read()
        if not ok or frame is None:
            raise RuntimeError("failed to read frame from camera %d"
                               % self._index)
        ok, buf = cv2.imencode(
            ".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 85])
        if not ok:
            raise RuntimeError("JPEG encoding failed")
        return buf.tobytes()

    def stop_stream(self) -> None:
        with self._stream_lock:
            if self._cap is not None:
                self._cap.release()
                self._cap = None
