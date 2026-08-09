"""Read role IDs only from visible game-character text."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol

from adapters.windows_background_capture import (
    CaptureSample,
    Win32PrintWindowProvider,
)
from adapters.windows_role_id_ocr import (
    WindowsRoleIdOcrReader,
)


# The HUD permits names wider than the old 58-pixel sample.  Keep the entire
# visible name band here; the OCR preprocessor excludes the coloured envelope
# icon by its pixels instead of truncating a real name before that icon.
# 以已保存的完整遊戲視窗 1347×933 真圖核對：第一列角色名稱位於
# 左上人物框右側 x=118..280、y=68..104。左界若再往左，會把等級
# 數字一起交給辨識器；舊座標則落在上方狀態列。
ROLE_ID_REFERENCE_SIZE = (1347, 933)
ROLE_ID_REGION = (118, 68, 280, 104)

# 本遊戲為繁體介面；保存真圖的「級」會被本機辨識模型固定回傳成
# 簡體「级」。只校正這個已有真圖逐字證據的字，不猜測其他字。
_CONFIRMED_OCR_TRADITIONAL = str.maketrans({"级": "級"})


class RoleIdOcrReader(Protocol):
    def read(self, sample: CaptureSample) -> str:
        """Return text read from the captured game-character name."""


class RoleIdCaptureProvider(Protocol):
    def capture(self, window_handle: int) -> CaptureSample | None:
        """Capture a game window without changing its foreground state."""


def role_id_region_sample(sample: CaptureSample) -> CaptureSample | None:
    left, top, right, bottom = ROLE_ID_REGION
    reference_width, reference_height = ROLE_ID_REFERENCE_SIZE
    scale_x = sample.width / reference_width
    scale_y = sample.height / reference_height
    # 只接受同一個完整遊戲視窗比例的縮放擷取；未知裁切不猜座標。
    if not (0.90 <= scale_x / scale_y <= 1.10):
        return None
    left, top, right, bottom = (
        round(left * scale_x),
        round(top * scale_y),
        round(right * scale_x),
        round(bottom * scale_y),
    )
    if (
        sample.width < right
        or sample.height < bottom
        or len(sample.pixels) < sample.width * sample.height * 4
    ):
        return None
    width, height = right - left, bottom - top
    pixels = bytearray()
    for y in range(top, bottom):
        start = (y * sample.width + left) * 4
        pixels.extend(sample.pixels[start : start + width * 4])
    return CaptureSample(width, height, bytes(pixels), sample.api_succeeded)


def clean_role_id_text(value: object) -> str:
    if not isinstance(value, str):
        return ""
    raw = value.strip().translate(_CONFIRMED_OCR_TRADITIONAL)
    if (
        not raw
        or "\\" in raw
        or "/" in raw
        or raw.casefold().endswith(".lnk")
        or "..." in raw
        or "…" in raw
    ):
        # A shortcut name or an ellipsized HUD label is not a complete
        # in-game role name.  Never turn either into a saved identity.
        return ""
    accepted = re.compile(r"[0-9A-Za-z_\-\u3040-\u30ff\u4e00-\u9fff]")
    cleaned: list[str] = []
    for character in raw:
        if character.isspace():
            continue
        if accepted.fullmatch(character):
            cleaned.append(character)
        else:
            # Do not replace an unreadable glyph with a guessed character.
            # Retain its position as the user-visible blank requested for
            # partially readable names.
            cleaned.append(" ")
    result = "".join(cleaned).strip()[:24]
    # 單一字元不足以代表完整且穩定的遊戲角色身分。
    return result if len(result) >= 2 else ""


@dataclass(frozen=True, slots=True)
class RoleIdReadResult:
    success: bool
    role_id: str = ""
    message: str = ""


class RoleIdTemplateService:
    """Compatibility name for the role-ID service.

    Older versions used a shortcut label and an image signature as a template.
    This service deliberately ignores that old data: every successful value
    comes from the current game's visible character-name pixels.
    """

    def __init__(
        self,
        *,
        capture_provider: RoleIdCaptureProvider | None = None,
        ocr_reader: RoleIdOcrReader | None = None,
    ) -> None:
        self._capture_provider = (
            capture_provider or Win32PrintWindowProvider()
        )
        self._ocr_reader = ocr_reader or WindowsRoleIdOcrReader()

    def _read_game_role_id(self, window_handle: int) -> RoleIdReadResult:
        values: list[str] = []
        for _attempt in range(2):
            whole_window = self._capture_provider.capture(window_handle)
            sample = (
                role_id_region_sample(whole_window)
                if whole_window is not None
                else None
            )
            if sample is None or not sample.api_succeeded:
                return RoleIdReadResult(False, message="無法讀取遊戲畫面的角色ID；原本資料保持不變。")
            role_id = clean_role_id_text(self._ocr_reader.read(sample))
            if not role_id:
                return RoleIdReadResult(False, message="角色名稱不完整或可信度不足；原本資料保持不變。")
            values.append(role_id)
        if len(set(values)) != 1:
            return RoleIdReadResult(False, message="角色名稱前後不一致；原本資料保持不變。")
        role_id = values[0]
        return RoleIdReadResult(
            True,
            role_id=role_id,
            message=f"已讀取遊戲內角色ID：{role_id}",
        )

    def calibrate(
        self,
        window_handle: int,
        *,
        entry_id: object = "",
    ) -> RoleIdReadResult:
        del entry_id
        return self._read_game_role_id(window_handle)

    def read(
        self,
        window_handle: int,
        *,
        entry_id: object = "",
    ) -> RoleIdReadResult:
        del entry_id
        return self._read_game_role_id(window_handle)

    def read_if_missing(
        self,
        window_handle: int,
        *,
        existing_role_id: object,
    ) -> RoleIdReadResult:
        """Read only an empty role name for the automatic background pass."""
        if isinstance(existing_role_id, str) and existing_role_id.strip():
            return RoleIdReadResult(
                False,
                message="已有已保存的角色名稱，自動讀取不覆寫。",
            )
        return self._read_game_role_id(window_handle)
