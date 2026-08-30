from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any
from uuid import uuid4


@dataclass(frozen=True)
class CropOption:
    key: str
    creature: str
    resource: str
    shop_x: int
    shop_y: int

    @property
    def label(self) -> str:
        return f"{self.creature} → {self.resource}"


CROP_OPTIONS = [
    CropOption("normal_rock", "普通岩怪", "金屬", 76, 66),
    CropOption("normal_tree", "普通樹精", "木材", 206, 66),
    CropOption("normal_crystal", "普通晶怪", "玉", 335, 66),
    CropOption("normal_spider", "普通絲蛛", "布", 76, 127),
    CropOption("normal_bear", "普通巨熊", "毛皮", 206, 127),
    CropOption("rare_rock", "稀有岩怪", "稀有金屬", 335, 127),
    CropOption("rare_tree", "稀有樹精", "優質木材", 76, 189),
    CropOption("rare_crystal", "稀有晶怪", "水晶", 206, 189),
    CropOption("rare_spider", "稀有絲蛛", "織錦", 335, 189),
    CropOption("rare_bear", "稀有巨熊", "皮革", 76, 250),
]

CROP_BY_KEY = {option.key: option for option in CROP_OPTIONS}
CROP_BY_LABEL = {option.label: option for option in CROP_OPTIONS}


@dataclass
class Profile:
    id: str = field(default_factory=lambda: str(uuid4()))
    shortcut_path: str = ""
    shortcut_name: str = ""
    crop_key: str = CROP_OPTIONS[0].key
    quantity: int = 16
    enabled: bool = True
    window_hwnd: int = 0
    window_pid: int = 0
    window_title: str = ""

    @property
    def crop(self) -> CropOption:
        return CROP_BY_KEY.get(self.crop_key, CROP_OPTIONS[0])

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Profile":
        known = {item.name for item in cls.__dataclass_fields__.values()}
        values = {key: value for key, value in data.items() if key in known}
        result = cls(**values)
        result.quantity = max(1, min(16, int(result.quantity)))
        if result.crop_key not in CROP_BY_KEY:
            result.crop_key = CROP_OPTIONS[0].key
        return result


@dataclass
class AppConfig:
    interval_minutes: int = 60
    retry_minutes: int = 3
    profiles: list[Profile] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "interval_minutes": self.interval_minutes,
            "retry_minutes": self.retry_minutes,
            "profiles": [profile.to_dict() for profile in self.profiles],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AppConfig":
        return cls(
            interval_minutes=max(1, int(data.get("interval_minutes", 60))),
            retry_minutes=max(1, int(data.get("retry_minutes", 3))),
            profiles=[Profile.from_dict(item) for item in data.get("profiles", [])],
        )


@dataclass
class RuntimeState:
    profile_id: str
    status: str = "待機"
    next_due: datetime | None = None
    last_success: datetime | None = None
    paused: bool = False
    running: bool = False


@dataclass(frozen=True)
class RunResult:
    kind: str
    message: str

    @classmethod
    def success(cls, message: str) -> "RunResult":
        return cls("success", message)

    @classmethod
    def retry(cls, message: str) -> "RunResult":
        return cls("retry", message)

    @classmethod
    def pause(cls, message: str) -> "RunResult":
        return cls("pause", message)

    @classmethod
    def stopped(cls, message: str = "已停止") -> "RunResult":
        return cls("stopped", message)
