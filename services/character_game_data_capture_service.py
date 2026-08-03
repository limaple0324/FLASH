"""安全背景擷取與四項遊戲資料的部分更新接點。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from threading import RLock
from typing import Callable, Protocol

from adapters.windows_background_capture import (
    CaptureSample,
    WindowCaptureProvider,
)
from adapters.windows_launch_fingerprint import normalize_launch_fingerprint
from domain.character_game_data import (
    ArtifactSnapshot,
    ObsidianSnapshot,
    PetLifeSoulSnapshot,
    PetTalentPageSnapshot,
    PetTalentSnapshot,
)
from services.character_game_data_update_service import (
    CharacterGameDataUpdateResult,
    CharacterGameDataUpdateService,
)


class GameDataPageKind(str, Enum):
    PET_TALENT = "pet_talent"
    OBSIDIAN = "obsidian"
    LIFE_SOUL = "life_soul"
    ARTIFACT = "artifact"


@dataclass(frozen=True, slots=True)
class RegisteredGameDataWindow:
    """呼叫端已確認的視窗、匿名啟動指紋與角色身分綁定。"""

    window_handle: int
    character_id: str
    launch_fingerprint: str

    def __post_init__(self) -> None:
        if (
            isinstance(self.window_handle, bool)
            or not isinstance(self.window_handle, int)
            or self.window_handle <= 0
        ):
            raise ValueError("window_handle must be a positive integer.")
        if not isinstance(self.character_id, str) or not self.character_id.strip():
            raise ValueError("character_id must be a non-empty string.")
        fingerprint = normalize_launch_fingerprint(self.launch_fingerprint)
        if fingerprint is None:
            raise ValueError("launch_fingerprint must be a complete SHA-256 value.")
        object.__setattr__(self, "character_id", self.character_id.strip())
        object.__setattr__(self, "launch_fingerprint", fingerprint)


@dataclass(frozen=True, slots=True)
class VerifiedGameDataPage:
    """由獨立頁面辨識器確認後才可交給保存層的資料。"""

    page_kind: GameDataPageKind
    logical_page_id: str
    content_signature: str
    data: (
        PetTalentPageSnapshot
        | PetTalentSnapshot
        | ObsidianSnapshot
        | PetLifeSoulSnapshot
        | ArtifactSnapshot
    )

    def __post_init__(self) -> None:
        if not isinstance(self.page_kind, GameDataPageKind):
            raise TypeError("page_kind must be GameDataPageKind.")
        if not isinstance(self.logical_page_id, str) or not self.logical_page_id.strip():
            raise ValueError("logical_page_id must be a non-empty string.")
        if not isinstance(self.content_signature, str) or not self.content_signature.strip():
            raise ValueError("content_signature must be a non-empty string.")
        expected_types = {
            GameDataPageKind.PET_TALENT: (PetTalentPageSnapshot, PetTalentSnapshot),
            GameDataPageKind.OBSIDIAN: (ObsidianSnapshot,),
            GameDataPageKind.LIFE_SOUL: (PetLifeSoulSnapshot,),
            GameDataPageKind.ARTIFACT: (ArtifactSnapshot,),
        }
        if not isinstance(self.data, expected_types[self.page_kind]):
            raise TypeError("data does not match page_kind.")
        object.__setattr__(self, "logical_page_id", self.logical_page_id.strip())
        object.__setattr__(self, "content_signature", self.content_signature.strip())


class GameDataPageRecognizer(Protocol):
    def read(self, sample: CaptureSample) -> VerifiedGameDataPage | None:
        """只從目前擷取畫面回傳可靠頁面資料；不產出角色身分。"""


class GameDataReadStatus(str, Enum):
    UPDATED = "updated"
    UNCHANGED = "unchanged"
    TARGET_NOT_ELIGIBLE = "target_not_eligible"
    CAPTURE_UNAVAILABLE = "capture_unavailable"
    PAGE_UNRECOGNIZED = "page_unrecognized"
    UPDATE_REJECTED = "update_rejected"


@dataclass(frozen=True, slots=True)
class GameDataReadResult:
    status: GameDataReadStatus
    page: VerifiedGameDataPage | None = None
    update: CharacterGameDataUpdateResult | None = None
    error: str | None = None


class CharacterGameDataCaptureService:
    """使用不切前景、不送輸入的擷取器，將可靠頁面交給部分更新服務。"""

    def __init__(
        self,
        capture_provider: WindowCaptureProvider,
        page_recognizer: GameDataPageRecognizer,
        update_service: CharacterGameDataUpdateService,
        registered_target_resolver: Callable[[int], RegisteredGameDataWindow | None],
    ) -> None:
        if not hasattr(capture_provider, "capture"):
            raise TypeError("capture_provider must provide capture().")
        if not callable(getattr(page_recognizer, "read", None)):
            raise TypeError("page_recognizer must provide read().")
        if not isinstance(update_service, CharacterGameDataUpdateService):
            raise TypeError("update_service must be CharacterGameDataUpdateService.")
        if not callable(registered_target_resolver):
            raise TypeError("registered_target_resolver must be callable.")
        self._capture_provider = capture_provider
        self._page_recognizer = page_recognizer
        self._update_service = update_service
        self._registered_target_resolver = registered_target_resolver
        self._signatures: dict[tuple[int, str, GameDataPageKind, str], str] = {}
        self._signature_lock = RLock()

    def read(self, window_handle: int) -> GameDataReadResult:
        if (
            isinstance(window_handle, bool)
            or not isinstance(window_handle, int)
            or window_handle <= 0
        ):
            return GameDataReadResult(
                GameDataReadStatus.TARGET_NOT_ELIGIBLE,
                error="已註冊遊戲視窗識別無效。",
            )
        try:
            target = self._registered_target_resolver(window_handle)
        except Exception as error:
            return GameDataReadResult(
                GameDataReadStatus.TARGET_NOT_ELIGIBLE,
                error=str(error) or "目標視窗可靠綁定檢查失敗。",
            )
        if (
            not isinstance(target, RegisteredGameDataWindow)
            or target.window_handle != window_handle
        ):
            return GameDataReadResult(
                GameDataReadStatus.TARGET_NOT_ELIGIBLE,
                error="視窗未通過已註冊身分與匿名啟動指紋綁定檢查。",
            )
        try:
            sample = self._capture_provider.capture(window_handle)
        except Exception as error:
            return GameDataReadResult(
                GameDataReadStatus.CAPTURE_UNAVAILABLE,
                error=str(error) or "視窗擷取失敗。",
            )
        if sample is None or not sample.api_succeeded:
            return GameDataReadResult(
                GameDataReadStatus.CAPTURE_UNAVAILABLE,
                error="視窗擷取未成功。",
            )
        try:
            page = self._page_recognizer.read(sample)
        except Exception as error:
            return GameDataReadResult(
                GameDataReadStatus.PAGE_UNRECOGNIZED,
                error=str(error) or "頁面辨識失敗。",
            )
        if not isinstance(page, VerifiedGameDataPage):
            return GameDataReadResult(
                GameDataReadStatus.PAGE_UNRECOGNIZED,
                error="目前畫面不是已確認的資料頁。",
            )
        signature_key = (
            window_handle,
            target.character_id,
            page.page_kind,
            page.logical_page_id,
        )
        with self._signature_lock:
            unchanged = (
                self._signatures.get(signature_key) == page.content_signature
            )
        if unchanged:
            return GameDataReadResult(GameDataReadStatus.UNCHANGED, page=page)
        try:
            update = self._apply(target.character_id, page)
        except (TypeError, ValueError) as error:
            return GameDataReadResult(
                GameDataReadStatus.UPDATE_REJECTED,
                page=page,
                error=str(error) or "資料保存被拒絕。",
            )
        with self._signature_lock:
            self._signatures[signature_key] = page.content_signature
        if not update.changed:
            return GameDataReadResult(
                GameDataReadStatus.UNCHANGED,
                page=page,
                update=update,
            )
        return GameDataReadResult(
            GameDataReadStatus.UPDATED,
            page=page,
            update=update,
        )

    def clear_window(self, window_handle: int) -> None:
        prefix = int(window_handle)
        with self._signature_lock:
            self._signatures = {
                key: value
                for key, value in self._signatures.items()
                if key[0] != prefix
            }

    def _apply(
        self,
        character_id: str,
        page: VerifiedGameDataPage,
    ) -> CharacterGameDataUpdateResult:
        if page.page_kind is GameDataPageKind.PET_TALENT:
            return self._update_service.update(character_id, pet_talent=page.data)
        if page.page_kind is GameDataPageKind.OBSIDIAN:
            return self._update_service.update(character_id, obsidian=page.data)
        if page.page_kind is GameDataPageKind.LIFE_SOUL:
            return self._update_service.update(character_id, life_souls=page.data)
        return self._update_service.update(character_id, artifact=page.data)
