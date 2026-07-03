"""Shared dataclasses for webcamdemo."""

from dataclasses import dataclass, field, asdict


@dataclass
class CameraInfo:
    # id: Linux "/dev/videoN"; Windows DirectShow device index as str "0", "1", ...
    id: str
    name: str
    extra: dict = field(default_factory=dict)

    def to_dict(self):
        return asdict(self)


@dataclass
class Control:
    id: str          # stable snake_case key, e.g. "brightness", "focus_absolute"
    name: str        # human display name
    type: str        # "int" | "bool" | "menu" | "button"
    min: 'int|None' = None
    max: 'int|None' = None
    step: 'int|None' = None
    default: 'int|None' = None
    value: 'int|None' = None
    menu: 'dict[int,str]|None' = None   # menu type: value -> label
    inactive: bool = False              # greyed out (e.g. manual ctrl while auto on)

    def to_dict(self):
        d = asdict(self)
        if d.get("menu") is not None:
            d["menu"] = {str(k): v for k, v in d["menu"].items()}
        return d


@dataclass
class FrameFormat:
    fourcc: str
    width: int
    height: int
    fps: 'list[float]'

    def to_dict(self):
        return asdict(self)
